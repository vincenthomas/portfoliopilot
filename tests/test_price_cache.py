from datetime import date, timedelta
from decimal import Decimal

import duckdb
import pytest

from portfoliopilot.ledger.prices import PriceStore
from portfoliopilot.market_data.base import Bar
from portfoliopilot.market_data.service import MarketDataService


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
