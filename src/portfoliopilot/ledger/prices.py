"""Price store: persistent daily bars in DuckDB with Parquet export/import.

The store is dumb on purpose: it holds whatever bars it is given and answers
range queries. Deciding what to fetch lives in `market_data.service`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import duckdb

from portfoliopilot.market_data.base import Bar

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_bars (
    ticker TEXT NOT NULL,
    ts     DATE NOT NULL,
    open   DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    volume BIGINT,
    source TEXT,
    PRIMARY KEY (ticker, ts)
);
"""


@dataclass(frozen=True)
class Coverage:
    ticker: str
    first: date | None
    last: date | None
    count: int

    def missing(self, start: date, end: date) -> list[tuple[date, date]]:
        """Date ranges inside [start, end] not yet covered (assumes contiguous coverage)."""
        if self.first is None or self.last is None:
            return [(start, end)]
        gaps: list[tuple[date, date]] = []
        if start < self.first:
            gaps.append((start, self.first - timedelta(days=1)))
        if end > self.last:
            gaps.append((self.last + timedelta(days=1), end))
        return gaps


class PriceStore:
    def __init__(self, con: duckdb.DuckDBPyConnection):
        self.con = con
        self.con.execute(SCHEMA)

    @classmethod
    def open(cls, db_path: str | Path = "portfoliopilot.duckdb") -> PriceStore:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return cls(duckdb.connect(str(db_path)))

    # ---- writes ---------------------------------------------------------

    def upsert(self, bars: list[Bar]) -> int:
        if not bars:
            return 0
        rows = [
            (b.ticker.upper(), b.ts, float(b.open), float(b.high), float(b.low),
             float(b.close), int(b.volume), b.source)
            for b in bars
        ]
        self.con.executemany(
            "INSERT OR REPLACE INTO price_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows
        )
        return len(rows)

    # ---- reads ----------------------------------------------------------

    def bars(self, ticker: str, start: date, end: date) -> list[Bar]:
        rows = self.con.execute(
            "SELECT ticker, ts, open, high, low, close, volume, source FROM price_bars "
            "WHERE ticker = ? AND ts BETWEEN ? AND ? ORDER BY ts",
            [ticker.upper(), start, end],
        ).fetchall()
        return [self._to_bar(r) for r in rows]

    def latest(self, ticker: str) -> Bar | None:
        row = self.con.execute(
            "SELECT ticker, ts, open, high, low, close, volume, source FROM price_bars "
            "WHERE ticker = ? ORDER BY ts DESC LIMIT 1",
            [ticker.upper()],
        ).fetchone()
        return self._to_bar(row) if row else None

    def coverage(self, ticker: str) -> Coverage:
        first, last, n = self.con.execute(
            "SELECT min(ts), max(ts), count(*) FROM price_bars WHERE ticker = ?",
            [ticker.upper()],
        ).fetchone()
        return Coverage(ticker.upper(), first, last, n)

    def tickers(self) -> list[str]:
        return [r[0] for r in self.con.execute(
            "SELECT DISTINCT ticker FROM price_bars ORDER BY ticker").fetchall()]

    def closes(self, tickers: list[str], start: date, end: date) -> dict[str, dict[date, Decimal]]:
        """{ticker: {date: close}} — the shape backtests and factor models consume."""
        out: dict[str, dict[date, Decimal]] = {t.upper(): {} for t in tickers}
        rows = self.con.execute(
            "SELECT ticker, ts, close FROM price_bars WHERE ticker IN "
            f"({','.join('?' * len(tickers))}) AND ts BETWEEN ? AND ? ORDER BY ts",
            [t.upper() for t in tickers] + [start, end],
        ).fetchall()
        for t, ts, close in rows:
            out[t][ts] = Decimal(str(close))
        return out

    # ---- parquet --------------------------------------------------------

    def export_parquet(self, path: str | Path) -> int:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con.execute(
            f"COPY (SELECT * FROM price_bars ORDER BY ticker, ts) TO '{path}' (FORMAT PARQUET)"
        )
        return self.con.execute("SELECT count(*) FROM price_bars").fetchone()[0]

    def import_parquet(self, path: str | Path) -> int:
        before = self.con.execute("SELECT count(*) FROM price_bars").fetchone()[0]
        self.con.execute(
            f"INSERT OR REPLACE INTO price_bars SELECT * FROM read_parquet('{Path(path)}')"
        )
        after = self.con.execute("SELECT count(*) FROM price_bars").fetchone()[0]
        return after - before

    # ---- helpers --------------------------------------------------------

    @staticmethod
    def _to_bar(r: tuple) -> Bar:
        return Bar(
            ticker=r[0], ts=r[1],
            open=Decimal(str(r[2])), high=Decimal(str(r[3])),
            low=Decimal(str(r[4])), close=Decimal(str(r[5])),
            volume=int(r[6] or 0), source=r[7] or "unknown",
        )
