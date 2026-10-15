from datetime import date, timedelta
from decimal import Decimal

import duckdb
import pytest

from portfoliopilot.ledger.prices import PriceStore
from portfoliopilot.market_data.base import Bar
from portfoliopilot.market_data.service import MarketDataService
from portfoliopilot.market_data.universe import (
    Interval,
    UniverseStore,
    intervals_from_snapshots,
    sp500_history_snapshot,
    sp500_snapshot,
    yahoo_symbol,
)


def make_bars(ticker: str, start: date, n: int, source: str = "test") -> list[Bar]:
    out = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:  # trading days only
            px = Decimal(100) + Decimal(len(out)) / 10
            out.append(Bar(ticker=ticker, ts=d, open=px, high=px + 1, low=px - 1, close=px,
                           volume=1000 + len(out), source=source))
        d += timedelta(days=1)
    return out


class CountingAdapter:
    """Serves synthetic bars and counts how often it is asked."""

    name = "counting"

    def __init__(self, bars: list[Bar]):
        self._bars = bars
        self.calls: list[tuple[str, date, date]] = []

    def quotes(self, tickers):
        return {}

    def history(self, ticker, start, end):
        self.calls.append((ticker, start, end))
        return [b for b in self._bars if b.ticker == ticker.upper() and start <= b.ts <= end]


@pytest.fixture
def store():
    return PriceStore(duckdb.connect(":memory:"))


def test_upsert_is_idempotent_and_ordered(store):
    bars = make_bars("ABC", date(2024, 1, 1), 50)
    assert store.upsert(bars) == 50
    assert store.upsert(bars) == 50  # replace, not duplicate
    got = store.bars("abc", date(2024, 1, 1), date(2024, 12, 31))
    assert len(got) == 50
    assert [b.ts for b in got] == sorted(b.ts for b in got)
    assert got[-1].close == bars[-1].close
    assert store.latest("ABC").ts == bars[-1].ts


def test_parquet_round_trip_1000_bars(store, tmp_path):
    bars = make_bars("XYZ", date(2020, 1, 1), 1000)
    store.upsert(bars)
    path = tmp_path / "bars.parquet"
    assert store.export_parquet(path) == 1000

    fresh = PriceStore(duckdb.connect(":memory:"))
    assert fresh.import_parquet(path) == 1000
    assert fresh.coverage("XYZ").count == 1000
    assert fresh.bars("XYZ", bars[0].ts, bars[-1].ts) == bars


def test_sync_fetches_only_missing_ranges_and_cache_hit_avoids_network(store):
    bars = make_bars("ABC", date(2024, 1, 1), 260)  # ~ one year of sessions
    adapter = CountingAdapter(bars)
    svc = MarketDataService(adapter, store)

    first = svc.sync("ABC", date(2024, 1, 1), date(2024, 6, 30))
    assert first.fetched > 0 and len(adapter.calls) == 1

    # Fully covered request: no adapter call at all.
    svc.history("ABC", date(2024, 2, 1), date(2024, 5, 31))
    assert len(adapter.calls) == 1

    # Extending to the right fetches only the tail.
    second = svc.sync("ABC", date(2024, 1, 1), date(2024, 12, 31))
    assert len(adapter.calls) == 2
    lo, hi = adapter.calls[-1][1], adapter.calls[-1][2]
    assert lo > first.stored_last and hi == date(2024, 12, 31)
    assert second.stored_count == 260

    # Extending to the left fetches only the head.
    svc.sync("ABC", date(2023, 12, 1), date(2024, 12, 31))
    assert adapter.calls[-1][1] == date(2023, 12, 1) and adapter.calls[-1][2] < date(2024, 1, 2)


def test_trailing_weekend_gap_does_not_refetch(store):
    bars = make_bars("ABC", date(2024, 1, 1), 5)  # Mon..Fri Jan 1-5
    adapter = CountingAdapter(bars)
    svc = MarketDataService(adapter, store)
    svc.sync("ABC", date(2024, 1, 1), date(2024, 1, 5))
    svc.sync("ABC", date(2024, 1, 1), date(2024, 1, 7))  # Sunday
    assert len(adapter.calls) == 1


def test_quotes_fall_back_to_cached_close(store):
    bars = make_bars("ABC", date(2024, 1, 1), 3)
    store.upsert(bars)

    class Broken:
        name = "broken"

        def quotes(self, tickers):
            raise ConnectionError("offline")

        def history(self, *a):
            return []

    q = MarketDataService(Broken(), store).quotes(["ABC", "NOPE"])
    assert q["ABC"].price == bars[-1].close
    assert q["ABC"].source.startswith("cache:")
    assert "NOPE" not in q


def test_closes_matrix(store):
    store.upsert(make_bars("AAA", date(2024, 1, 1), 3) + make_bars("BBB", date(2024, 1, 1), 3))
    m = store.closes(["aaa", "bbb"], date(2024, 1, 1), date(2024, 1, 31))
    assert set(m) == {"AAA", "BBB"} and len(m["AAA"]) == 3


def test_open_creates_parent_directory(tmp_path):
    s = PriceStore.open(tmp_path / "nested" / "dir" / "prices.duckdb")
    assert s.coverage("ABC").count == 0


# ---- universes ----------------------------------------------------------


def test_sp500_snapshot_is_complete_and_clean():
    members = sp500_snapshot()
    assert 495 <= len(members) <= 510
    assert len({m.symbol for m in members}) == len(members)
    assert all(m.sector for m in members)
    assert yahoo_symbol("BRK.B") == "BRK-B"


def test_universe_store_round_trip(store):
    us = UniverseStore(store.con)
    n = us.replace("sp500", sp500_snapshot(), "test", as_of=date(2026, 10, 15))
    assert us.symbols("sp500")[:2] == ["A", "AAPL"] and len(us.symbols("sp500")) == n
    assert us.as_of("sp500") == date(2026, 10, 15)
    assert us.sectors("sp500")["AAPL"] == "Information Technology"


def test_sync_many_uses_batched_history_for_fresh_tickers(store):
    bars = make_bars("AAA", date(2024, 1, 1), 10) + make_bars("BBB", date(2024, 1, 1), 10)

    class Batched(CountingAdapter):
        def __init__(self, bars):
            super().__init__(bars)
            self.batch_calls = 0

        def history_many(self, tickers, start, end):
            self.batch_calls += 1
            return {t: self.history(t, start, end) for t in tickers}

    adapter = Batched(bars)
    svc = MarketDataService(adapter, store)
    reports = svc.sync_many(["AAA", "BBB"], date(2024, 1, 1), date(2024, 1, 31))
    assert adapter.batch_calls == 1 and [r.stored_count for r in reports] == [10, 10]
    # second run: nothing fresh, nothing fetched
    reports = svc.sync_many(["AAA", "BBB"], date(2024, 1, 1), date(2024, 1, 31))
    assert adapter.batch_calls == 1 and sum(r.fetched for r in reports) == 0


def test_batch_empties_are_retried_singly_with_backoff(store):
    """A throttled symbol comes back empty from the batch; the per-ticker retry recovers it."""
    bars = make_bars("AAA", date(2024, 1, 1), 10) + make_bars("BBB", date(2024, 1, 1), 10)

    class Throttling(CountingAdapter):
        def __init__(self, bars):
            super().__init__(bars)
            self.single_calls_bbb = 0

        def history_many(self, tickers, start, end):
            return {"AAA": self.history("AAA", start, end), "BBB": []}  # BBB throttled

        def history(self, ticker, start, end):
            if ticker == "BBB":
                self.single_calls_bbb += 1
                if self.single_calls_bbb < 3:
                    return []  # still throttled on first two single tries
            return super().history(ticker, start, end)

    slept: list[float] = []
    svc = MarketDataService(Throttling(bars), store, retry_delays=(1, 2, 3), sleep=slept.append)
    reports = svc.sync_many(["AAA", "BBB"], date(2024, 1, 1), date(2024, 1, 31))
    assert [r.stored_count for r in reports] == [10, 10]
    assert slept == [1, 2] and not reports[1].empty


def test_gives_up_after_retries_and_flags_empty(store):
    class Dead(CountingAdapter):
        def history(self, *a):
            return []

    slept: list[float] = []
    svc = MarketDataService(Dead([]), store, retry_delays=(1, 1), sleep=slept.append)
    r = svc.sync_many(["ZZZ"], date(2024, 1, 1), date(2024, 1, 31))[0]
    assert r.empty and r.stored_count == 0 and slept == [1, 1]


def test_pre_listing_gap_is_probed_once_and_never_again(store):
    """A security listed in 2019 asked from 1990: the left gap is requested once, then remembered."""
    bars = make_bars("NEW", date(2019, 3, 13), 50)
    end = bars[-1].ts
    adapter = CountingAdapter(bars)
    svc = MarketDataService(adapter, store)
    svc.sync("NEW", date(1990, 1, 1), end)
    first_calls = len(adapter.calls)
    svc.sync("NEW", date(1990, 1, 1), end)
    svc.sync("NEW", date(2000, 1, 1), end)  # inside the probed range: no call
    assert len(adapter.calls) == first_calls
    svc.sync("NEW", date(1927, 1, 1), end)  # earlier than ever probed: one more call
    assert len(adapter.calls) == first_calls + 1
    assert store.coverage("NEW").probed_from == date(1927, 1, 1)


# ---- point-in-time membership --------------------------------------------


def test_intervals_from_snapshots_handles_rejoiners():
    snaps = [
        (date(2020, 1, 1), {"A", "B"}),
        (date(2020, 2, 1), {"A"}),          # B leaves
        (date(2020, 3, 1), {"A", "B", "C"}),  # B rejoins, C joins
    ]
    got = intervals_from_snapshots(snaps)
    assert got == [
        Interval("A", date(2020, 1, 1), None),
        Interval("B", date(2020, 1, 1), date(2020, 2, 1)),
        Interval("B", date(2020, 3, 1), None),
        Interval("C", date(2020, 3, 1), None),
    ]


def test_bundled_history_golden_dates(store):
    us = UniverseStore(store.con)
    n = us.replace_history("sp500", sp500_history_snapshot(), "test")
    assert n > 1200
    lo, hi = us.history_range("sp500")
    assert lo == date(1996, 1, 2) and hi >= date(2026, 8, 18)
    for d in [date(1996, 1, 2), date(2001, 1, 16), date(2008, 9, 15), date(2010, 1, 1),
              date(2020, 3, 16)]:
        n = len(us.members_on("sp500", d))
        assert 487 <= n <= 507, (d, n)
    m2010 = set(us.members_on("sp500", date(2010, 1, 1)))
    today = set(us.members_on("sp500", hi))
    assert "AAPL" in m2010 and "AAPL" in today
    # Lehman (source uses its final symbol LEHMQ throughout) and Bear Stearns: in before, gone after.
    assert "LEHMQ" in set(us.members_on("sp500", date(2008, 9, 1)))
    assert "BSC" in set(us.members_on("sp500", date(2008, 3, 1)))
    assert "LEHMQ" not in m2010 and "BSC" not in m2010
    assert "TSLA" not in m2010 and "TSLA" in today  # added Dec 2020
    assert len(m2010 - today) > 150  # survivorship bias is real: many 2010 names are gone
    amd = [i for i in sp500_history_snapshot() if i.symbol == "AMD"]
    assert len(amd) == 2  # left in 2013, rejoined in 2017


def test_extend_history_reconciles_with_current_list(store):
    us = UniverseStore(store.con)
    us.replace_history("u", [Interval("A", date(2020, 1, 1), None),
                             Interval("B", date(2020, 1, 1), None)], "test")
    added, removed = us.extend_history("u", ["A", "C"], date(2026, 10, 15))
    assert added == ["C"] and removed == ["B"]
    assert us.members_on("u", date(2026, 10, 15)) == ["A", "C"]
    assert us.members_on("u", date(2026, 10, 14)) == ["A", "B"]
    assert us.changes("u", date(2026, 1, 1), date(2026, 12, 31)) == [
        (date(2026, 10, 15), "add", "C"), (date(2026, 10, 15), "remove", "B")]
