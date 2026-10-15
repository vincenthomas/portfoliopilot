"""Wash-sale guard: IRC §1091.

A loss on a sale is disallowed to the extent substantially identical securities
were acquired within 30 days before or after the sale, in ANY account the
household controls (including IRAs, RSU vests, ESPP purchases and dividend
reinvestment). Disallowed loss is added to the basis of the replacement shares.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from portfoliopilot.core.models import ComplianceResult, Household, Lot, TxType

ACQUIRING_TYPES = {TxType.BUY, TxType.VEST, TxType.ESPP, TxType.DRIP}
WINDOW = timedelta(days=30)

# Minimal "substantially identical" map: same ticker, plus known share-class twins.
# Different index ETFs tracking the same index are NOT treated as identical here
# (the IRS has not ruled); users can extend this map in their ConstraintsProfile.
IDENTICAL_GROUPS: list[set[str]] = [
    {"GOOG", "GOOGL"},
    {"BRK.A", "BRK.B"},
]


def substantially_identical(a: str, b: str) -> bool:
    a, b = a.upper(), b.upper()
    if a == b:
        return True
    return any(a in g and b in g for g in IDENTICAL_GROUPS)


def check_loss_sale(
    household: Household, lot: Lot, qty: Decimal, price: Decimal, sale_date: date
) -> ComplianceResult:
    """Return a ComplianceResult; ok=False means the ticket is blocked until cured."""
    loss = (lot.basis_per_share - price) * qty
    if loss <= 0:
        return ComplianceResult(ok=True)

    lo, hi = sale_date - WINDOW, sale_date + WINDOW
    replacement_qty = Decimal(0)
    conflicts: list[str] = []
    for tx in household.transactions:
        if tx.type not in ACQUIRING_TYPES:
            continue
        if not substantially_identical(tx.ticker, lot.ticker):
            continue
        if lo <= tx.date <= hi:
            replacement_qty += tx.qty
            conflicts.append(f"{tx.type.value} {tx.qty} {tx.ticker} on {tx.date} in {tx.account_id}")
    for v in household.vest_events:
        if v.status != "unvested" or not substantially_identical(v.ticker, lot.ticker):
            continue
        if lo <= v.vest_date <= hi:
            replacement_qty += v.qty
            conflicts.append(f"scheduled vest {v.qty} {v.ticker} on {v.vest_date} in {v.account_id}")

    if not conflicts:
        return ComplianceResult(ok=True)

    disallowed_shares = min(qty, replacement_qty)
    disallowed = loss * (disallowed_shares / qty)
    last_conflict = max(
        [tx.date for tx in household.transactions if tx.type in ACQUIRING_TYPES
         and substantially_identical(tx.ticker, lot.ticker) and lo <= tx.date <= hi]
        + [v.vest_date for v in household.vest_events if v.status == "unvested"
           and substantially_identical(v.ticker, lot.ticker) and lo <= v.vest_date <= hi]
    )
    cure_date = max(last_conflict, sale_date) + timedelta(days=31)
    return ComplianceResult(
        ok=False,
        reasons=[f"Wash sale: ${disallowed:,.0f} of ${loss:,.0f} loss disallowed by: {'; '.join(conflicts)}"],
        cures=[
            (f"Sell the entire {lot.ticker} position across all accounts and buy nothing "
             f"substantially identical until {cure_date}"),
            "Or sell and buy a non-identical replacement (different index or issuer) immediately",
        ],
        disallowed_loss=disallowed,
    )
