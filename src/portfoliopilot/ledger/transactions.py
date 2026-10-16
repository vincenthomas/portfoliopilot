"""Transaction log: apply events to lots, record realized gains.

The log is append-only. Lots keep their ids for life: a partial sell reduces
`qty` and marks the lot `partial`; a full sell marks it `closed`. Every closed
slice becomes a `RealizedGain` so Schedule D and the harvest tally never have to
reach back to a broker.

Lot selection for sells: specific-ID when the transaction names `lot_ids`,
otherwise the household's default method (FIFO by default; brokers default to
FIFO for stocks). HIFO minimizes the realized gain; LIFO is offered for completeness.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

from portfoliopilot.core.models import (
    BasisSource,
    Household,
    Lot,
    RealizedGain,
    Transaction,
    TxType,
)

LotMethod = Literal["fifo", "lifo", "hifo", "specific"]

OPENING_TYPES: dict[TxType, BasisSource] = {
    TxType.BUY: BasisSource.TRADE,
    TxType.VEST: BasisSource.RSU_VEST,
    TxType.ESPP: BasisSource.ESPP,
    TxType.DRIP: BasisSource.DRIP,
    TxType.GIFT: BasisSource.GIFT,
}


class LedgerError(ValueError):
    pass


@dataclass
class ApplyResult:
    tx: Transaction
    opened: list[Lot] = field(default_factory=list)
    realized: list[RealizedGain] = field(default_factory=list)
    adjusted: list[Lot] = field(default_factory=list)  # splits / transfers


# ---- lot selection ------------------------------------------------------

def _candidates(h: Household, tx: Transaction) -> list[Lot]:
    return [lot for lot in h.lots
            if lot.account_id == tx.account_id and lot.ticker.upper() == tx.ticker.upper()
            and lot.status != "closed" and lot.acquired_date <= tx.date]


def select_lots(h: Household, tx: Transaction, method: LotMethod) -> list[Lot]:
    if tx.lot_ids:
        by_id = {lot.id: lot for lot in h.lots}
        missing = [i for i in tx.lot_ids if i not in by_id]
        if missing:
            raise LedgerError(f"{tx.id}: unknown lot ids {missing}")
        chosen = [by_id[i] for i in tx.lot_ids]
        bad = [lot.id for lot in chosen if lot.status == "closed"
               or lot.ticker.upper() != tx.ticker.upper() or lot.account_id != tx.account_id]
        if bad:
            raise LedgerError(f"{tx.id}: lots {bad} are closed or not {tx.ticker} in {tx.account_id}")
        return chosen
    lots = _candidates(h, tx)
    if method == "fifo":
        return sorted(lots, key=lambda lot: (lot.acquired_date, lot.id))
    if method == "lifo":
        return sorted(lots, key=lambda lot: (lot.acquired_date, lot.id), reverse=True)
    if method == "hifo":
        return sorted(lots, key=lambda lot: (-lot.basis_per_share, lot.acquired_date, lot.id))
    raise LedgerError(f"{tx.id}: method 'specific' requires lot_ids")


# ---- apply --------------------------------------------------------------

def apply(h: Household, tx: Transaction, method: LotMethod = "fifo") -> ApplyResult:
    """Apply one transaction to the household in place and append it to the log."""
    if any(t.id == tx.id for t in h.transactions):
        raise LedgerError(f"{tx.id}: already applied")
    res = ApplyResult(tx=tx)

    if tx.type in OPENING_TYPES:
        lot_id = tx.lot_id or tx.id
        if any(lot.id == lot_id for lot in h.lots):
            raise LedgerError(f"{tx.id}: lot id {lot_id} already exists")
        # Fees on a purchase are capitalized into basis.
        basis = tx.price + (tx.fees / tx.qty if tx.qty else Decimal(0))
        lot = Lot(id=lot_id, account_id=tx.account_id, ticker=tx.ticker.upper(), qty=tx.qty,
                  acquired_date=tx.date, basis_per_share=basis, basis_source=OPENING_TYPES[tx.type])
        h.lots.append(lot)
        res.opened.append(lot)

    elif tx.type == TxType.SELL:
        remaining = tx.qty
        lots = select_lots(h, tx, method)
        available = sum(lot.qty for lot in lots)
        if available < remaining:
            raise LedgerError(f"{tx.id}: selling {tx.qty} {tx.ticker} but only {available} "
                              f"available in {tx.account_id}")
        fee_per_share = tx.fees / tx.qty if tx.qty else Decimal(0)
        for lot in lots:
            if remaining <= 0:
                break
            take = min(lot.qty, remaining)
            rg = RealizedGain(
                tx_id=tx.id, lot_id=lot.id, account_id=lot.account_id, ticker=lot.ticker,
                qty=take, acquired_date=lot.acquired_date, sold_date=tx.date,
                proceeds=take * (tx.price - fee_per_share), basis=take * lot.basis_per_share,
                long_term=lot.is_long_term(tx.date),
            )
            h.realized.append(rg)
            res.realized.append(rg)
            lot.qty -= take
            lot.status = "closed" if lot.qty == 0 else "partial"
            remaining -= take

    elif tx.type == TxType.SPLIT:
        ratio = tx.qty
        if ratio <= 0:
            raise LedgerError(f"{tx.id}: split ratio must be positive")
        for lot in h.lots:  # splits apply to the security in every account
            if lot.ticker.upper() == tx.ticker.upper() and lot.status != "closed":
                lot.qty = lot.qty * ratio
                lot.basis_per_share = lot.basis_per_share / ratio
                res.adjusted.append(lot)

    elif tx.type == TxType.TRANSFER:
        by_id = {lot.id: lot for lot in h.lots}
        for lid in tx.lot_ids:
            lot = by_id.get(lid)
            if lot is None:
                raise LedgerError(f"{tx.id}: unknown lot {lid}")
            lot.account_id = tx.account_id  # basis and acquired_date carry over (no gain)
            res.adjusted.append(lot)

    else:
        raise LedgerError(f"{tx.id}: unsupported type {tx.type}")

    h.transactions.append(tx)
    return res


def apply_all(h: Household, txs: list[Transaction], method: LotMethod = "fifo") -> list[ApplyResult]:
    return [apply(h, tx, method) for tx in sorted(txs, key=lambda t: (t.date, t.id))]


def replay(base: Household, txs: list[Transaction], method: LotMethod = "fifo") -> Household:
    """Rebuild lots and realized gains from scratch on a copy of `base` (lots/log cleared)."""
    h = base.model_copy(deep=True)
    h.lots, h.transactions, h.realized = [], [], []
    apply_all(h, txs, method)
    return h


# ---- reporting ----------------------------------------------------------

@dataclass
class YearSummary:
    year: int
    short_term: Decimal = Decimal(0)
    long_term: Decimal = Decimal(0)
    proceeds: Decimal = Decimal(0)
    trades: int = 0

    @property
    def net(self) -> Decimal:
        return self.short_term + self.long_term


def realized_by_year(h: Household) -> dict[int, YearSummary]:
    out: dict[int, YearSummary] = defaultdict(lambda: YearSummary(0))
    for rg in h.realized:
        y = out[rg.sold_date.year]
        y.year = rg.sold_date.year
        if rg.long_term:
            y.long_term += rg.gain
        else:
            y.short_term += rg.gain
        y.proceeds += rg.proceeds
        y.trades += 1
    return dict(sorted(out.items()))


def net_by_term(h: Household, year: int) -> tuple[Decimal, Decimal]:
    """(net short-term, net long-term) for a tax year, before carryforward netting."""
    y = realized_by_year(h).get(year)
    return (y.short_term, y.long_term) if y else (Decimal(0), Decimal(0))


def position_on(h: Household, ticker: str, as_of: date) -> Decimal:
    """Household-wide shares held on a date, reconstructed from the log (for backtests and
    wash-sale windows). Ignores lots opened after `as_of`; subtracts sells through `as_of`."""
    qty = sum(lot.qty for lot in h.lots if lot.ticker.upper() == ticker.upper()
              and lot.acquired_date <= as_of)
    # add back shares sold after as_of (they were still held on as_of)
    qty += sum(rg.qty for rg in h.realized if rg.ticker.upper() == ticker.upper()
               and rg.sold_date > as_of and rg.acquired_date <= as_of)
    return qty
