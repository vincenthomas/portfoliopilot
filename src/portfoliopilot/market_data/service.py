"""MarketDataService: cache-first access to history and quotes.

`history()` fetches only the date ranges the store does not already hold,
persists what the adapter returns, and serves everything from the store.
`quotes()` goes to the adapter and falls back to the last stored close.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from portfoliopilot.ledger.prices import PriceStore
from portfoliopilot.market_data.base import Bar, History, MarketDataAdapter, Quote

# Yahoo-style sources return the last complete session; a fetch that ends today
# often returns nothing new. Treat coverage within this many days of `end` as current.
STALE_TOLERANCE = timedelta(days=4)
log = logging.getLogger(__name__)


@dataclass
class SyncReport:
    ticker: str
    fetched: int = 0
    ranges: list[tuple[date, date]] = field(default_factory=list)
    stored_first: date | None = None
    stored_last: date | None = None
    stored_count: int = 0
    empty: bool = False  # still nothing after retries


class MarketDataService:
    def __init__(self, adapter: MarketDataAdapter, store: PriceStore,
                 retry_delays: tuple[float, ...] = (2, 5, 15),
                 sleep: Callable[[float], None] = time.sleep):
        self.adapter = adapter
        self.store = store
        self.retry_delays = retry_delays
        self.sleep = sleep

    def history(self, ticker: str, start: date, end: date) -> list[Bar]:
        self.sync(ticker, start, end)
        return self.store.bars(ticker, start, end)

    def sync(self, ticker: str, start: date, end: date) -> SyncReport:
        """Fill gaps in the store for [start, end]; return what was fetched."""
        report = SyncReport(ticker=ticker.upper())
        cov = self.store.coverage(ticker)
        for lo, hi in cov.missing(start, end):
            # Skip a trailing gap that is only weekend/holiday noise.
            if cov.last is not None and lo > cov.last and hi - cov.last <= STALE_TOLERANCE:
                continue
            bars = self._fetch_one(ticker, lo, hi)
            if bars:
                report.ranges.append((lo, hi))
                report.fetched += len(bars)
            if bars or (cov.first is not None and hi < cov.first):
                # We asked from `lo` and got everything the provider has from there; a left gap
                # that came back empty means the security was not listed yet. (An empty reply
                # on a never-seen ticker is NOT recorded: it may be throttling.)
                self.store.mark_probed(ticker, lo)
        cov = self.store.coverage(ticker)
        report.stored_first, report.stored_last, report.stored_count = cov.first, cov.last, cov.count
        return report

    def _fetch_one(self, ticker: str, lo: date, hi: date) -> list[Bar]:
        """Fetch and persist one range; prefers the adapter's full (bars+actions) method."""
        full = getattr(self.adapter, "history_full", None)
        if full:
            h: History = full(ticker, lo, hi)
            self.store.upsert_history(h)
            return h.bars
        bars = self.adapter.history(ticker, lo, hi)
        self.store.upsert(bars)
        return bars

    def _fetch_many(self, tickers: list[str], start: date, end: date) -> dict[str, list[Bar]]:
        full = getattr(self.adapter, "history_many_full", None)
        if full:
            got: dict[str, History] = full(tickers, start, end)
            for h in got.values():
                self.store.upsert_history(h)
            return {t: h.bars for t, h in got.items()}
        many = getattr(self.adapter, "history_many", None)
        if many:
            got_b = many(tickers, start, end)
            for bars in got_b.values():
                self.store.upsert(bars)
            return got_b
        return {}

    def sync_many(self, tickers: list[str], start: date, end: date,
                  progress: Callable[[SyncReport], None] | None = None,
                  retry_empty: bool = True) -> list[SyncReport]:
        """Sync many tickers; batches the fully-missing ones through `history_many` if the
        adapter offers it, gap-fills the rest one by one, and retries anything that came
        back empty (providers throttle silently inside batches)."""
        reports: dict[str, SyncReport] = {}
        can_batch = any(hasattr(self.adapter, m) for m in ("history_many_full", "history_many"))
        fresh = [t for t in tickers if self.store.coverage(t).count == 0]
        if can_batch and len(fresh) > 1:
            try:
                fetched = self._fetch_many(fresh, start, end)
            except Exception as exc:  # noqa: BLE001 — fall back to per-ticker below
                log.warning("batched history failed (%s); falling back to per-ticker", exc)
                fetched = {}
            for t, bars in fetched.items():
                if not bars:
                    continue  # provider returned nothing (often throttling): retry singly below
                r = SyncReport(ticker=t.upper(), fetched=len(bars))
                r.ranges.append((start, end))
                self.store.mark_probed(t, start)
                cov = self.store.coverage(t)
                r.stored_first, r.stored_last, r.stored_count = cov.first, cov.last, cov.count
                reports[t.upper()] = r
                if progress:
                    progress(r)
        for t in tickers:
            if t.upper() in reports:
                continue
            r = self._sync_with_retry(t, start, end, retry_empty)
            reports[t.upper()] = r
            if progress:
                progress(r)
        return [reports[t.upper()] for t in tickers]

    def _sync_with_retry(self, ticker: str, start: date, end: date,
                         retry_empty: bool = True) -> SyncReport:
        """Per-ticker sync; a still-empty result is retried with backoff (throttling)."""
        r = self.sync(ticker, start, end)
        if r.stored_count:
            self.store.mark_probed(ticker, start)
            self.store.clear_unavailable(ticker)
            return r
        if not retry_empty:
            r.empty = True
            return r
        for attempt, delay in enumerate(self.retry_delays, start=1):
            log.info("%s returned no bars; retry %d in %.0fs", ticker, attempt, delay)
            self.sleep(delay)
            r = self.sync(ticker, start, end)
            if r.stored_count:
                self.store.mark_probed(ticker, start)
                self.store.clear_unavailable(ticker)
                return r
        r.empty = True
        return r

    def upgrade(self, tickers: list[str] | None = None,
                progress: Callable[[SyncReport], None] | None = None) -> list[SyncReport]:
        """One-time full re-download for tickers stored before adjustments/actions existed.
        Keeps each ticker's current date range; replaces its rows in place."""
        todo = tickers if tickers is not None else self.store.tickers_without_adjustments()
        reports = []
        for t in todo:
            cov = self.store.coverage(t)
            if cov.first is None:
                continue
            start = cov.probed_from or cov.first
            self.store.drop_ticker(t)
            r = self._sync_with_retry(t, start, cov.last, retry_empty=True)
            reports.append(r)
            if progress:
                progress(r)
        return reports

    def sync_delisted(self, symbols: list[str], end_dates: dict[str, date], start: date,
                      checked_on: date, progress: Callable[[SyncReport], None] | None = None,
                      ) -> tuple[list[SyncReport], list[str]]:
        """Fetch history for symbols that left a universe, through their last member day.
        No backoff (dead symbols are the common case); unserved ones are recorded so
        `verify` can list exactly where a paid feed would be needed. Returns (reports, unserved)."""
        unserved: list[str] = []
        reports: list[SyncReport] = []
        for sym in symbols:
            end = end_dates.get(sym.upper()) or checked_on
            r = self._sync_with_retry(sym, start, end, retry_empty=False)
            reports.append(r)
            if r.empty or not r.stored_count:
                self.store.mark_unavailable(sym, checked_on, "delisted; provider has no history")
                unserved.append(sym.upper())
            if progress:
                progress(r)
        return reports, unserved

    def quotes(self, tickers: list[str]) -> dict[str, Quote]:
        try:
            live = self.adapter.quotes(tickers)
        except Exception as exc:  # noqa: BLE001 — any provider failure degrades to cache
            log.warning("live quotes unavailable (%s); using cached closes", exc)
            live = {}
        out = dict(live)
        for t in tickers:
            if t.upper() in out:
                continue
            last = self.store.latest(t)
            if last:
                out[t.upper()] = Quote(
                    ticker=t.upper(), price=last.close,
                    as_of=datetime.combine(last.ts, datetime.min.time(), tzinfo=UTC),
                    source=f"cache:{last.source}",
                )
        return out

    def prices(self, tickers: list[str]) -> dict[str, Decimal]:
        return {t: q.price for t, q in self.quotes(tickers).items()}
