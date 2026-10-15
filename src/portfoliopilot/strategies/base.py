"""Strategy interface. Every strategy, built-in or user-supplied, implements `propose`.
The backtester and the live advice engine call exactly the same method."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from portfoliopilot.core.models import Household, Trade
from portfoliopilot.tax.engine import TaxEngine


@dataclass
class HouseholdState:
    household: Household
    prices: dict[str, Decimal]
    as_of: date
    tax_engine: TaxEngine


class Strategy(Protocol):
    name: str

    def propose(self, state: HouseholdState) -> list[Trade]: ...


_counter = 0


def next_trade_id(prefix: str) -> str:
    global _counter
    _counter += 1
    return f"{prefix}-{_counter:05d}"
