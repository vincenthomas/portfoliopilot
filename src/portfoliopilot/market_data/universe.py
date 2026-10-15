"""Universes: named lists of tickers (S&P 500 today, more later), persisted with an as-of date.

Source for S&P 500 membership: the `datasets/s-and-p-500-companies` project
(https://github.com/datasets/s-and-p-500-companies, ODC-PDDL), itself derived
from Wikipedia. A snapshot ships in the package so the universe works offline.
"""

from __future__ import annotations

import csv
import io
import logging
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime
from importlib.resources import files

import duckdb

log = logging.getLogger(__name__)

SP500_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"

SCHEMA = """
CREATE TABLE IF NOT EXISTS universe_members (
    universe TEXT NOT NULL,
    symbol   TEXT NOT NULL,
    name     TEXT,
    sector   TEXT,
    sub_industry TEXT,
    date_added DATE,
    as_of    DATE NOT NULL,
    source   TEXT,
    PRIMARY KEY (universe, symbol)
);
CREATE TABLE IF NOT EXISTS universe_history (
    universe TEXT NOT NULL,
    symbol   TEXT NOT NULL,
    start_date DATE NOT NULL,       -- first day the symbol was a member
    end_date   DATE,                -- first day it was NOT a member; NULL = still a member
    source   TEXT,
    PRIMARY KEY (universe, symbol, start_date)
);
"""

SP500_HISTORY_URL = ("https://raw.githubusercontent.com/fja05680/sp500/master/"
                     "S%26P%20500%20Historical%20Components%20%26%20Changes%20(Updated).csv")


@dataclass(frozen=True)
class Member:
    symbol: str
    name: str
    sector: str
    sub_industry: str
    date_added: date | None


@dataclass(frozen=True)
class Interval:
    symbol: str
    start: date
    end: date | None  # exclusive; None = open

    def contains(self, d: date) -> bool:
        return self.start <= d and (self.end is None or d < self.end)


def intervals_from_snapshots(snapshots: list[tuple[date, set[str]]]) -> list[Interval]:
    """Diff consecutive point-in-time member sets into per-symbol intervals.
    Symbols may leave and rejoin, so a symbol can have several intervals."""
    open_: dict[str, date] = {}
    out: list[Interval] = []
    prev: set[str] = set()
    for d, cur in sorted(snapshots, key=lambda x: x[0]):
        for sym in cur - prev:
            open_[sym] = d
        for sym in prev - cur:
            out.append(Interval(sym, open_.pop(sym), d))
        prev = cur
    out += [Interval(sym, st, None) for sym, st in open_.items()]
    return sorted(out, key=lambda i: (i.symbol, i.start))


def _parse_history(text: str) -> list[Interval]:
    snaps = []
    for r in csv.DictReader(io.StringIO(text)):
        syms = {t.strip().upper() for t in r["tickers"].split(",") if t.strip()}
        snaps.append((date.fromisoformat(r["date"][:10]), syms))
    return intervals_from_snapshots(snaps)


def sp500_history_snapshot() -> list[Interval]:
    text = files("portfoliopilot.market_data.data").joinpath("sp500_membership.csv").read_text()
    return [
        Interval(r["symbol"], date.fromisoformat(r["start"]),
                 date.fromisoformat(r["end"]) if r["end"] else None)
        for r in csv.DictReader(io.StringIO(text))
    ]


def sp500_history_live(timeout: int = 60) -> list[Interval]:
    with urllib.request.urlopen(SP500_HISTORY_URL, timeout=timeout) as resp:
        return _parse_history(resp.read().decode())


def sp500_history(prefer_live: bool = True) -> tuple[list[Interval], str]:
    if prefer_live:
        try:
            return sp500_history_live(), "fja05680/sp500@github"
        except Exception as exc:  # noqa: BLE001 — any network failure falls back to snapshot
            log.warning("live S&P 500 history unavailable (%s); using packaged snapshot", exc)
    return sp500_history_snapshot(), "packaged-snapshot"


def yahoo_symbol(symbol: str) -> str:
    """Share-class dots (BRK.B) become dashes (BRK-B) for Yahoo-style providers."""
    return symbol.replace(".", "-")


def _parse(rows: list[dict[str, str]]) -> list[Member]:
    out = []
    for r in rows:
        added = r.get("date_added") or r.get("Date added") or ""
        try:
            d = date.fromisoformat(added[:10]) if added else None
        except ValueError:
            d = None
        out.append(Member(
            symbol=(r.get("symbol") or r.get("Symbol") or "").strip().upper(),
            name=r.get("name") or r.get("Security") or "",
            sector=r.get("sector") or r.get("GICS Sector") or "",
            sub_industry=r.get("sub_industry") or r.get("GICS Sub-Industry") or "",
            date_added=d,
        ))
    return [m for m in out if m.symbol]


def sp500_snapshot() -> list[Member]:
    text = files("portfoliopilot.market_data.data").joinpath("sp500_constituents.csv").read_text()
    return _parse(list(csv.DictReader(io.StringIO(text))))


def sp500_live(timeout: int = 20) -> list[Member]:
    with urllib.request.urlopen(SP500_URL, timeout=timeout) as resp:
        text = resp.read().decode()
    return _parse(list(csv.DictReader(io.StringIO(text))))


def sp500(prefer_live: bool = True) -> tuple[list[Member], str]:
    """Return (members, source). Falls back to the shipped snapshot when offline."""
    if prefer_live:
        try:
            return sp500_live(), "datasets/s-and-p-500-companies@github"
        except Exception as exc:  # noqa: BLE001 — any network failure falls back to snapshot
            log.warning("live S&P 500 list unavailable (%s); using packaged snapshot", exc)
    return sp500_snapshot(), "packaged-snapshot"


class UniverseStore:
    def __init__(self, con: duckdb.DuckDBPyConnection):
        self.con = con
        self.con.execute(SCHEMA)

    def replace(self, universe: str, members: list[Member], source: str,
                as_of: date | None = None) -> int:
        as_of = as_of or datetime.now(UTC).date()
        self.con.execute("DELETE FROM universe_members WHERE universe = ?", [universe])
        self.con.executemany(
            "INSERT INTO universe_members VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(universe, m.symbol, m.name, m.sector, m.sub_industry, m.date_added, as_of, source)
             for m in members],
        )
        return len(members)

    def symbols(self, universe: str) -> list[str]:
        return [r[0] for r in self.con.execute(
            "SELECT symbol FROM universe_members WHERE universe = ? ORDER BY symbol", [universe]
        ).fetchall()]

    def as_of(self, universe: str) -> date | None:
        row = self.con.execute(
            "SELECT max(as_of) FROM universe_members WHERE universe = ?", [universe]).fetchone()
        return row[0] if row else None

    def sectors(self, universe: str) -> dict[str, str]:
        return dict(self.con.execute(
            "SELECT symbol, sector FROM universe_members WHERE universe = ?", [universe]).fetchall())

    # ---- point-in-time history --------------------------------------------

    def replace_history(self, universe: str, intervals: list[Interval], source: str) -> int:
        self.con.execute("DELETE FROM universe_history WHERE universe = ?", [universe])
        self.con.executemany(
            "INSERT INTO universe_history VALUES (?, ?, ?, ?, ?)",
            [(universe, i.symbol, i.start, i.end, source) for i in intervals],
        )
        return len(intervals)

    def extend_history(self, universe: str, current: list[str], as_of: date,
                       source: str = "observed") -> tuple[list[str], list[str]]:
        """Reconcile open intervals with today's membership: close intervals for symbols
        that left, open new ones for symbols that joined. Dates are approximate (the day
        we looked), so these rows are tagged `observed`. Returns (added, removed)."""
        cur = {s.upper() for s in current}
        open_now = {r[0] for r in self.con.execute(
            "SELECT symbol FROM universe_history WHERE universe = ? AND end_date IS NULL",
            [universe]).fetchall()}
        removed = sorted(open_now - cur)
        added = sorted(cur - open_now)
        for sym in removed:
            self.con.execute(
                "UPDATE universe_history SET end_date = ?, source = ? "
                "WHERE universe = ? AND symbol = ? AND end_date IS NULL",
                [as_of, source, universe, sym])
        self.con.executemany(
            "INSERT OR REPLACE INTO universe_history VALUES (?, ?, ?, NULL, ?)",
            [(universe, sym, as_of, source) for sym in added])
        return added, removed

    def members_on(self, universe: str, d: date) -> list[str]:
        return [r[0] for r in self.con.execute(
            "SELECT symbol FROM universe_history WHERE universe = ? AND start_date <= ? "
            "AND (end_date IS NULL OR end_date > ?) ORDER BY symbol",
            [universe, d, d]).fetchall()]

    def history_range(self, universe: str) -> tuple[date | None, date | None]:
        row = self.con.execute(
            "SELECT min(start_date), max(coalesce(end_date, start_date)) "
            "FROM universe_history WHERE universe = ?", [universe]).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def all_historical_symbols(self, universe: str) -> list[str]:
        return [r[0] for r in self.con.execute(
            "SELECT DISTINCT symbol FROM universe_history WHERE universe = ? ORDER BY symbol",
            [universe]).fetchall()]

    def changes(self, universe: str, start: date, end: date) -> list[tuple[date, str, str]]:
        """(date, 'add'|'remove', symbol) events inside [start, end]."""
        rows = self.con.execute(
            "SELECT start_date, 'add', symbol FROM universe_history "
            "WHERE universe = ? AND start_date BETWEEN ? AND ? "
            "UNION ALL SELECT end_date, 'remove', symbol FROM universe_history "
            "WHERE universe = ? AND end_date BETWEEN ? AND ? ORDER BY 1, 2, 3",
            [universe, start, end, universe, start, end]).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]
