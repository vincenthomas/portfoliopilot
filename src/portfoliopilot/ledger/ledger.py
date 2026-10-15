"""Household ledger: single source of truth, netted across accounts.

Backed by DuckDB (one local file). The in-memory Household model is the API;
this module persists it and answers the aggregate questions every other
module asks (positions, concentration, realized P&L).
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import duckdb

from portfoliopilot.core.models import Household, Lot

SCHEMA = """
CREATE TABLE IF NOT EXISTS households (id TEXT PRIMARY KEY, name TEXT, payload JSON);
CREATE TABLE IF NOT EXISTS price_bars (
    ticker TEXT, ts DATE, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    volume BIGINT, source TEXT, PRIMARY KEY (ticker, ts)
);
"""


class Ledger:
    def __init__(self, db_path: str | Path = "portfoliopilot.duckdb"):
        self.con = duckdb.connect(str(db_path))
        self.con.execute(SCHEMA)

    # ---- persistence ----------------------------------------------------

    def save(self, h: Household) -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO households VALUES (?, ?, ?)",
            [h.id, h.name, h.model_dump_json()],
        )

    def load(self, household_id: str) -> Household:
        row = self.con.execute(
            "SELECT payload FROM households WHERE id = ?", [household_id]
        ).fetchone()
        if row is None:
            raise KeyError(household_id)
        return Household.model_validate_json(row[0])

    # ---- aggregates (pure functions on the model) -----------------------

    @staticmethod
    def open_lots(h: Household, ticker: str | None = None) -> list[Lot]:
        return [
            lot for lot in h.lots
            if lot.status != "closed" and (ticker is None or lot.ticker.upper() == ticker.upper())
        ]

    @staticmethod
    def positions(h: Household) -> dict[str, Decimal]:
        """Net shares per ticker across ALL accounts."""
        pos: dict[str, Decimal] = defaultdict(Decimal)
        for lot in Ledger.open_lots(h):
            pos[lot.ticker.upper()] += lot.qty
        return dict(pos)

    @staticmethod
    def market_values(h: Household, prices: dict[str, Decimal]) -> dict[str, Decimal]:
        return {t: q * prices[t] for t, q in Ledger.positions(h).items() if t in prices}

    @staticmethod
    def concentration(h: Household, prices: dict[str, Decimal]) -> dict[str, float]:
        """Weight of each ticker in total household investable assets."""
        mv = Ledger.market_values(h, prices)
        total = sum(mv.values())
        if not total:
            return {}
        return {t: float(v / total) for t, v in sorted(mv.items(), key=lambda kv: -kv[1])}
