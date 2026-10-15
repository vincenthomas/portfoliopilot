"""Default adapter: Yahoo Finance via `yfinance`. No API key required.
Suitable for daily bars and delayed quotes; swap for Polygon/Alpaca for real-time."""

from __future__ import annotations

import logging
import math
from datetime import UTC, date, datetime
from decimal import Decimal

from portfoliopilot.market_data.base import Bar, Dividend, History, Quote, Split


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
        return self.history_full(ticker, start, end).bars

    def history_full(self, ticker: str, start: date, end: date) -> History:
        """Bars with Adj Close, plus dividends and splits, in one request."""
        df = self._yf.Ticker(ticker).history(start=start, end=end, auto_adjust=False,
                                             actions=True, raise_errors=False)
        return self._frame_to_history(ticker, df)

    def history_many(self, tickers: list[str], start: date, end: date) -> dict[str, list[Bar]]:
        return {t: h.bars for t, h in self.history_many_full(tickers, start, end).items()}

    def history_many_full(self, tickers: list[str], start: date, end: date) -> dict[str, History]:
        """One request per ~100 tickers instead of one per ticker."""
        out: dict[str, History] = {}
        for i in range(0, len(tickers), 100):
            chunk = tickers[i:i + 100]
            df = self._yf.download(chunk, start=start, end=end, auto_adjust=False, actions=True,
                                   group_by="ticker", progress=False, threads=True)
            for t in chunk:
                sub = df[t] if len(chunk) > 1 else df
                out[t.upper()] = self._frame_to_history(t, sub.dropna(subset=["Close"]))
        return out

    @staticmethod
    def _num(v) -> float | None:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return None if math.isnan(f) else f

    def _frame_to_history(self, ticker: str, df) -> History:
        h = History()
        T = ticker.upper()
        for ts, r in df.iterrows():
            close = self._num(r.get("Close"))
            if close is None:
                continue
            d = ts.date()
            adj = self._num(r.get("Adj Close"))
            vol = self._num(r.get("Volume"))
            h.bars.append(Bar(
                ticker=T, ts=d,
                open=Decimal(str(round(self._num(r.get("Open")) or close, 6))),
                high=Decimal(str(round(self._num(r.get("High")) or close, 6))),
                low=Decimal(str(round(self._num(r.get("Low")) or close, 6))),
                close=Decimal(str(round(close, 6))),
                adj_close=Decimal(str(round(adj, 6))) if adj is not None else None,
                volume=int(vol or 0), source=self.name,
            ))
            div = self._num(r.get("Dividends"))
            if div:
                h.dividends.append(Dividend(ticker=T, ex_date=d, amount=Decimal(str(round(div, 6))),
                                            source=self.name))
            sp = self._num(r.get("Stock Splits"))
            if sp:
                h.splits.append(Split(ticker=T, date=d, ratio=Decimal(str(round(sp, 6))),
                                      source=self.name))
        return h
