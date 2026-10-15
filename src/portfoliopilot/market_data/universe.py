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
"""


@dataclass(frozen=True)
class Member:
    symbol: str
    name: str
    sector: str
    sub_industry: str
    date_added: date | None


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
