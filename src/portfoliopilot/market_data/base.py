"""Market-data adapter interface. Any provider implements this Protocol and
registers under the `portfoliopilot.market_data` entry point."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class Quote(BaseModel):
    ticker: str
    price: Decimal
    as_of: datetime
    source: str


class Bar(BaseModel):
    ticker: str
    ts: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    source: str


@runtime_checkable
class MarketDataAdapter(Protocol):
    name: str

    def quotes(self, tickers: list[str]) -> dict[str, Quote]: ...

    def history(self, ticker: str, start: date, end: date) -> list[Bar]: ...


class StaticAdapter:
    """Deterministic adapter for tests and backtests: prices supplied up front."""

    name = "static"

    def __init__(self, prices: dict[str, Decimal], bars: dict[str, list[Bar]] | None = None):
        self._prices = {k.upper(): v for k, v in prices.items()}
        self._bars = bars or {}

    def quotes(self, tickers: list[str]) -> dict[str, Quote]:
        now = datetime.now(UTC)
        return {
            t.upper(): Quote(ticker=t.upper(), price=self._prices[t.upper()], as_of=now, source=self.name)
            for t in tickers if t.upper() in self._prices
        }

    def history(self, ticker: str, start: date, end: date) -> list[Bar]:
        return [b for b in self._bars.get(ticker.upper(), []) if start <= b.ts <= end]
