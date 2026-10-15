"""Tax engine: prices a proposed sale for a given TaxProfile.

Marginal-rate model: the gain is stacked on top of projected income and
year-to-date gains, so a sale that crosses a bracket boundary is taxed in
two pieces. State and NIIT are added on top. Collectibles (precious-metal
ETFs) use the collectibles cap instead of the LT bracket.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import yaml

from portfoliopilot.core.models import Lot, Money, TaxProfile

RATE_TABLE_DIR = Path(__file__).parent / "rate_tables"

COLLECTIBLE_TICKERS = {"GLD", "IAU", "SLV", "PPLT", "PALL", "SGOL", "GLDM", "BAR", "OUNZ"}


@dataclass(frozen=True)
class TaxEstimate:
    gain: Money
    is_long_term: bool
    federal_tax: Money
    niit_tax: Money
    state_tax: Money
    is_collectible: bool = False

    @property
    def total(self) -> Money:
        return self.federal_tax + self.niit_tax + self.state_tax

    @property
    def effective_rate(self) -> Decimal:
        return (self.total / self.gain) if self.gain else Decimal(0)


class TaxEngine:
    def __init__(self, tax_year: int):
        self.year = tax_year
        year_dir = RATE_TABLE_DIR / str(tax_year)
        if not year_dir.exists():
            raise FileNotFoundError(f"No rate tables for {tax_year}; add {year_dir}")
        self.federal = yaml.safe_load((year_dir / "federal.yaml").read_text())
        self.states = yaml.safe_load((year_dir / "states.yaml").read_text())["states"]

    # ---- bracket helpers -------------------------------------------------

    @staticmethod
    def _stacked_tax(brackets: list[dict], floor: Decimal, amount: Decimal) -> Decimal:
        """Tax on `amount` stacked on top of `floor` income across `brackets`."""
        tax = Decimal(0)
        remaining = amount
        lower = Decimal(0)
        for b in brackets:
            upper = Decimal(b["up_to"]) if b["up_to"] is not None else None
            rate = Decimal(str(b["rate"]))
            # portion of [floor, floor+amount] inside [lower, upper]
            seg_lo = max(lower, floor)
            seg_hi = floor + amount if upper is None else min(upper, floor + amount)
            if seg_hi > seg_lo:
                tax += (seg_hi - seg_lo) * rate
                remaining -= seg_hi - seg_lo
            if upper is None or remaining <= 0:
                break
            lower = upper
        return tax

    def lt_headroom(self, profile: TaxProfile) -> tuple[Money, Decimal]:
        """Dollars of additional LT gain before the next federal LT bracket, and that bracket's rate."""
        brackets = self.federal["long_term_brackets"][profile.filing_status.value]
        floor = profile.projected_ordinary_income + profile.projected_lt_gains_ytd
        for b in brackets:
            if b["up_to"] is not None and floor < Decimal(b["up_to"]):
                return Decimal(b["up_to"]) - floor, Decimal(str(b["rate"]))
        return Decimal("Infinity"), Decimal(str(brackets[-1]["rate"]))

    # ---- main entry ------------------------------------------------------

    def estimate_sale(
        self, lot: Lot, qty: Decimal, price: Money, profile: TaxProfile, as_of: date
    ) -> TaxEstimate:
        gain = (price - lot.basis_per_share) * qty
        lt = lot.is_long_term(as_of)
        collectible = lot.ticker.upper() in COLLECTIBLE_TICKERS
        fs = profile.filing_status.value

        if gain <= 0:
            return TaxEstimate(gain, lt, Decimal(0), Decimal(0), Decimal(0), collectible)

        ordinary_floor = profile.projected_ordinary_income + profile.projected_st_gains_ytd
        if lt and not collectible:
            floor = ordinary_floor + profile.projected_lt_gains_ytd
            fed = self._stacked_tax(self.federal["long_term_brackets"][fs], floor, gain)
        elif lt and collectible:
            ord_tax = self._stacked_tax(self.federal["ordinary_brackets"][fs], ordinary_floor, gain)
            fed = min(ord_tax, gain * Decimal(str(self.federal["collectibles_max_rate"])))
        else:
            fed = self._stacked_tax(self.federal["ordinary_brackets"][fs], ordinary_floor, gain)

        # NIIT: 3.8% on the lesser of net investment income or MAGI over threshold.
        threshold = Decimal(self.federal["niit_threshold"][fs])
        magi_before = ordinary_floor + profile.projected_lt_gains_ytd
        over = max(Decimal(0), magi_before + gain - threshold)
        niit = min(gain, over) * Decimal(str(self.federal["niit_rate"]))

        st = self.states.get(profile.state.upper(), self.states["DEFAULT"])
        state_tax = Decimal(0) if st["mode"] == "none" else gain * Decimal(str(st["rate"]))

        return TaxEstimate(gain, lt, fed, niit, state_tax, collectible)
