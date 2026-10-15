"""Default adapter: Yahoo Finance via `yfinance`. No API key required.
Suitable for daily bars and delayed quotes; swap for Polygon/Alpaca for real-time."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from portfoliopilot.market_data.base import Bar, Quote


class YFinanceAdapter:
    name = "yfinance"

    def __init__(self) -> None:
        import yfinance as yf  # imported lazily so tests don't need network

        self._yf = yf

    def quotes(self, tickers: list[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        data = self._yf.Tickers(" ".join(tickers))
        for t in tickers:
            info = data.tickers[t].fast_info
            price = info.get("last_price") or info.get("previous_close")
            if price is None:
                continue
            out[t.upper()] = Quote(
                ticker=t.upper(), price=Decimal(str(price)), as_of=datetime.now(UTC), source=self.name
            )
        return out

    def history(self, ticker: str, start: date, end: date) -> list[Bar]:
        df = self._yf.Ticker(ticker).history(start=start, end=end, auto_adjust=False)
        return [
            Bar(
                ticker=ticker.upper(), ts=ts.date(),
                open=Decimal(str(r["Open"])), high=Decimal(str(r["High"])),
                low=Decimal(str(r["Low"])), close=Decimal(str(r["Close"])),
                volume=int(r["Volume"]), source=self.name,
            )
            for ts, r in df.iterrows()
        ]
