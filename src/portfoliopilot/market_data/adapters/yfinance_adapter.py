"""Default adapter: Yahoo Finance via `yfinance`. No API key required.
Suitable for daily bars and delayed quotes; swap for Polygon/Alpaca for real-time."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal

from portfoliopilot.market_data.base import Bar, Quote


class YFinanceAdapter:
    name = "yfinance"

    def __init__(self) -> None:
        import yfinance as yf  # imported lazily so tests don't need network

        self._yf = yf
        # yfinance prints "$X: Data doesn't exist for startDate=..." for pre-listing ranges;
        # the service handles empties itself, so keep the console clean.
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)

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
        df = self._yf.Ticker(ticker).history(start=start, end=end, auto_adjust=False,
                                             raise_errors=False)
        return self._frame_to_bars(ticker, df)

    def history_many(self, tickers: list[str], start: date, end: date) -> dict[str, list[Bar]]:
        """One request per ~100 tickers instead of one per ticker."""
        out: dict[str, list[Bar]] = {}
        for i in range(0, len(tickers), 100):
            chunk = tickers[i:i + 100]
            df = self._yf.download(chunk, start=start, end=end, auto_adjust=False,
                                   group_by="ticker", progress=False, threads=True)
            for t in chunk:
                sub = df[t] if len(chunk) > 1 else df
                out[t.upper()] = self._frame_to_bars(t, sub.dropna(subset=["Close"]))
        return out

    def _frame_to_bars(self, ticker: str, df) -> list[Bar]:
        bars = []
        for ts, r in df.iterrows():
            if r.get("Close") is None or r["Close"] != r["Close"]:  # NaN
                continue
            bars.append(Bar(
                ticker=ticker.upper(), ts=ts.date(),
                open=Decimal(str(round(float(r["Open"]), 6))),
                high=Decimal(str(round(float(r["High"]), 6))),
                low=Decimal(str(round(float(r["Low"]), 6))),
                close=Decimal(str(round(float(r["Close"]), 6))),
                volume=int(r["Volume"]) if r["Volume"] == r["Volume"] else 0,
                source=self.name,
            ))
        return bars
