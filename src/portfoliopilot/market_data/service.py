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
from portfoliopilot.market_data.base import Bar, MarketDataAdapter, Quote

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
            bars = self.adapter.history(ticker, lo, hi)
            if bars:
                report.fetched += self.store.upsert(bars)
                report.ranges.append((lo, hi))
        cov = self.store.coverage(ticker)
        report.stored_first, report.stored_last, report.stored_count = cov.first, cov.last, cov.count
        return report

    def sync_many(self, tickers: list[str], start: date, end: date,
                  progress: Callable[[SyncReport], None] | None = None) -> list[SyncReport]:
        """Sync many tickers; batches the fully-missing ones through `history_many` if the
        adapter offers it, gap-fills the rest one by one, and retries anything that came
        back empty (providers throttle silently inside batches)."""
        reports: dict[str, SyncReport] = {}
        batch_fetch = getattr(self.adapter, "history_many", None)
        fresh = [t for t in tickers if self.store.coverage(t).count == 0]
        if batch_fetch and len(fresh) > 1:
            try:
                fetched = batch_fetch(fresh, start, end)
            except Exception as exc:  # noqa: BLE001 — fall back to per-ticker below
                log.warning("batched history failed (%s); falling back to per-ticker", exc)
                fetched = {}
            for t, bars in fetched.items():
                if not bars:
                    continue  # provider returned nothing (often throttling): retry singly below
                r = SyncReport(ticker=t.upper(), fetched=self.store.upsert(bars))
                r.ranges.append((start, end))
                cov = self.store.coverage(t)
                r.stored_first, r.stored_last, r.stored_count = cov.first, cov.last, cov.count
                reports[t.upper()] = r
                if progress:
                    progress(r)
        for t in tickers:
            if t.upper() in reports:
                continue
            r = self._sync_with_retry(t, start, end)
            reports[t.upper()] = r
            if progress:
                progress(r)
        return [reports[t.upper()] for t in tickers]

    def _sync_with_retry(self, ticker: str, start: date, end: date) -> SyncReport:
        """Per-ticker sync; a still-empty result is retried with backoff (throttling)."""
        r = self.sync(ticker, start, end)
        if r.stored_count:
            return r
        for attempt, delay in enumerate(self.retry_delays, start=1):
            log.info("%s returned no bars; retry %d in %.0fs", ticker, attempt, delay)
            self.sleep(delay)
            r = self.sync(ticker, start, end)
            if r.stored_count:
                return r
        r.empty = True
        return r

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
