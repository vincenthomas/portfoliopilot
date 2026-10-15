"""Price store: persistent daily bars in DuckDB with Parquet export/import.

The store is dumb on purpose: it holds whatever bars it is given and answers
range queries. Deciding what to fetch lives in `market_data.service`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

import duckdb

from portfoliopilot.market_data.base import Bar, Dividend, History, Split

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_bars (
    ticker TEXT NOT NULL,
    ts     DATE NOT NULL,
    open   DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    volume BIGINT,
    source TEXT,
    adj_close DOUBLE,               -- split + dividend adjusted (total-return series)
    PRIMARY KEY (ticker, ts)
);
CREATE TABLE IF NOT EXISTS dividends (
    ticker TEXT NOT NULL, ex_date DATE NOT NULL, amount DOUBLE NOT NULL, source TEXT,
    PRIMARY KEY (ticker, ex_date)
);
CREATE TABLE IF NOT EXISTS splits (
    ticker TEXT NOT NULL, split_date DATE NOT NULL, ratio DOUBLE NOT NULL, source TEXT,
    PRIMARY KEY (ticker, split_date)
);
CREATE TABLE IF NOT EXISTS price_unavailable (
    ticker TEXT PRIMARY KEY, checked_on DATE NOT NULL, reason TEXT
);
CREATE TABLE IF NOT EXISTS price_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS price_probes (
    ticker TEXT PRIMARY KEY,
    probed_from DATE NOT NULL   -- we asked the provider from this date; nothing exists before `first`
);
"""


@dataclass(frozen=True)
class Coverage:
    ticker: str
    first: date | None
    last: date | None
    count: int
    probed_from: date | None = None  # earliest start already asked of the provider

    def missing(self, start: date, end: date) -> list[tuple[date, date]]:
        """Date ranges inside [start, end] not yet covered (assumes contiguous coverage).
        A left gap already probed (the security was not listed yet) is not a gap."""
        if self.first is None or self.last is None:
            return [(start, end)]
        gaps: list[tuple[date, date]] = []
        if start < self.first and (self.probed_from is None or start < self.probed_from):
            gaps.append((start, self.first - timedelta(days=1)))
        if end > self.last:
            gaps.append((self.last + timedelta(days=1), end))
        return gaps


class PriceStore:
    def __init__(self, con: duckdb.DuckDBPyConnection):
        self.con = con
        self.con.execute(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Bring an older file up to the current schema (additive only)."""
        cols = {r[1] for r in self.con.execute("PRAGMA table_info('price_bars')").fetchall()}
        if "adj_close" not in cols:
            self.con.execute("ALTER TABLE price_bars ADD COLUMN adj_close DOUBLE")
        self.con.execute("INSERT OR REPLACE INTO price_meta VALUES ('schema_version', ?)",
                         [str(SCHEMA_VERSION)])

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
             float(b.close), int(b.volume), b.source,
             float(b.adj_close) if b.adj_close is not None else None)
            for b in bars
        ]
        self.con.executemany(
            "INSERT OR REPLACE INTO price_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
        )
        return len(rows)

    def upsert_history(self, h: History) -> int:
        n = self.upsert(h.bars)
        if h.dividends:
            self.con.executemany(
                "INSERT OR REPLACE INTO dividends VALUES (?, ?, ?, ?)",
                [(d.ticker.upper(), d.ex_date, float(d.amount), d.source) for d in h.dividends])
        if h.splits:
            self.con.executemany(
                "INSERT OR REPLACE INTO splits VALUES (?, ?, ?, ?)",
                [(x.ticker.upper(), x.date, float(x.ratio), x.source) for x in h.splits])
        return n

    # ---- availability ledger (delisted / unserved symbols) --------------

    def mark_unavailable(self, ticker: str, checked_on: date, reason: str) -> None:
        self.con.execute("INSERT OR REPLACE INTO price_unavailable VALUES (?, ?, ?)",
                         [ticker.upper(), checked_on, reason])

    def clear_unavailable(self, ticker: str) -> None:
        self.con.execute("DELETE FROM price_unavailable WHERE ticker = ?", [ticker.upper()])

    def unavailable(self) -> dict[str, tuple[date, str]]:
        return {r[0]: (r[1], r[2]) for r in self.con.execute(
            "SELECT ticker, checked_on, reason FROM price_unavailable ORDER BY ticker").fetchall()}

    def tickers_without_adjustments(self) -> list[str]:
        """Tickers synced before adj_close existed (need a one-time full re-download)."""
        return [r[0] for r in self.con.execute(
            "SELECT DISTINCT ticker FROM price_bars WHERE adj_close IS NULL ORDER BY ticker"
        ).fetchall()]

    def drop_ticker(self, ticker: str) -> None:
        for tbl in ("price_bars", "dividends", "splits", "price_probes"):
            self.con.execute(f"DELETE FROM {tbl} WHERE ticker = ?", [ticker.upper()])

    # ---- reads ----------------------------------------------------------

    _COLS = "ticker, ts, open, high, low, close, volume, source, adj_close"

    def bars(self, ticker: str, start: date, end: date) -> list[Bar]:
        rows = self.con.execute(
            f"SELECT {self._COLS} FROM price_bars WHERE ticker = ? AND ts BETWEEN ? AND ? "
            "ORDER BY ts", [ticker.upper(), start, end],
        ).fetchall()
        return [self._to_bar(r) for r in rows]

    def latest(self, ticker: str) -> Bar | None:
        row = self.con.execute(
            f"SELECT {self._COLS} FROM price_bars WHERE ticker = ? ORDER BY ts DESC LIMIT 1",
            [ticker.upper()],
        ).fetchone()
        return self._to_bar(row) if row else None

    def dividends(self, ticker: str, start: date, end: date) -> list[Dividend]:
        return [Dividend(ticker=r[0], ex_date=r[1], amount=Decimal(str(r[2])), source=r[3] or "")
                for r in self.con.execute(
                    "SELECT ticker, ex_date, amount, source FROM dividends "
                    "WHERE ticker = ? AND ex_date BETWEEN ? AND ? ORDER BY ex_date",
                    [ticker.upper(), start, end]).fetchall()]

    def splits(self, ticker: str) -> list[Split]:
        return [Split(ticker=r[0], date=r[1], ratio=Decimal(str(r[2])), source=r[3] or "")
                for r in self.con.execute(
                    "SELECT ticker, split_date, ratio, source FROM splits WHERE ticker = ? "
                    "ORDER BY split_date", [ticker.upper()]).fetchall()]

    def coverage(self, ticker: str) -> Coverage:
        first, last, n = self.con.execute(
            "SELECT min(ts), max(ts), count(*) FROM price_bars WHERE ticker = ?",
            [ticker.upper()],
        ).fetchone()
        row = self.con.execute(
            "SELECT probed_from FROM price_probes WHERE ticker = ?", [ticker.upper()]).fetchone()
        return Coverage(ticker.upper(), first, last, n, row[0] if row else None)

    def mark_probed(self, ticker: str, probed_from: date) -> None:
        """Record that the provider was asked from `probed_from` and has nothing earlier."""
        self.con.execute(
            "INSERT INTO price_probes VALUES (?, ?) ON CONFLICT (ticker) DO UPDATE "
            "SET probed_from = least(price_probes.probed_from, excluded.probed_from)",
            [ticker.upper(), probed_from],
        )

    def tickers(self) -> list[str]:
        return [r[0] for r in self.con.execute(
            "SELECT DISTINCT ticker FROM price_bars ORDER BY ticker").fetchall()]

    def closes(self, tickers: list[str], start: date, end: date,
               kind: Literal["price", "total_return"] = "price") -> dict[str, dict[date, Decimal]]:
        """{ticker: {date: value}} — the shape backtests and factor models consume.
        kind='price'        -> split-adjusted close (lots, basis, tickets)
        kind='total_return' -> adj_close, split+dividend adjusted (returns, CAGR, drawdown);
                               falls back to close where adj_close is missing."""
        col = "coalesce(adj_close, close)" if kind == "total_return" else "close"
        out: dict[str, dict[date, Decimal]] = {t.upper(): {} for t in tickers}
        rows = self.con.execute(
            f"SELECT ticker, ts, {col} FROM price_bars WHERE ticker IN "
            f"({','.join('?' * len(tickers))}) AND ts BETWEEN ? AND ? ORDER BY ts",
            [t.upper() for t in tickers] + [start, end],
        ).fetchall()
        for t, ts, v in rows:
            out[t][ts] = Decimal(str(v))
        return out

    # ---- parquet --------------------------------------------------------

    _SIDE_TABLES = ("dividends", "splits")

    def export_parquet(self, path: str | Path) -> int:
        """Write bars to `path` and dividends/splits beside it (<stem>.dividends.parquet, ...)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con.execute(
            f"COPY (SELECT * FROM price_bars ORDER BY ticker, ts) TO '{path}' (FORMAT PARQUET)"
        )
        for tbl in self._SIDE_TABLES:
            self.con.execute(f"COPY (SELECT * FROM {tbl}) TO '{self._side(path, tbl)}' (FORMAT PARQUET)")
        return self.con.execute("SELECT count(*) FROM price_bars").fetchone()[0]

    def import_parquet(self, path: str | Path) -> int:
        path = Path(path)
        before = self.con.execute("SELECT count(*) FROM price_bars").fetchone()[0]
        cols = ", ".join(c for c in self._COLS.split(", "))
        src_cols = {r[0] for r in self.con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()}
        sel = ", ".join(c if c in src_cols else f"NULL AS {c}" for c in cols.split(", "))
        self.con.execute(
            f"INSERT OR REPLACE INTO price_bars ({cols}) SELECT {sel} FROM read_parquet('{path}')"
        )
        for tbl in self._SIDE_TABLES:
            side = self._side(path, tbl)
            if side.exists():
                self.con.execute(f"INSERT OR REPLACE INTO {tbl} SELECT * FROM read_parquet('{side}')")
        after = self.con.execute("SELECT count(*) FROM price_bars").fetchone()[0]
        return after - before

    @staticmethod
    def _side(path: Path, tbl: str) -> Path:
        return path.with_name(f"{path.stem}.{tbl}{path.suffix}")

    # ---- helpers --------------------------------------------------------

    @staticmethod
    def _to_bar(r: tuple) -> Bar:
        return Bar(
            ticker=r[0], ts=r[1],
            open=Decimal(str(r[2])), high=Decimal(str(r[3])),
            low=Decimal(str(r[4])), close=Decimal(str(r[5])),
            volume=int(r[6] or 0), source=r[7] or "unknown",
            adj_close=Decimal(str(r[8])) if len(r) > 8 and r[8] is not None else None,
        )
