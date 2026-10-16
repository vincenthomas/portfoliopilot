import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from portfoliopilot.core.models import (
    FilingStatus,
    Household,
    TaxProfile,
    Transaction,
    TxType,
)
from portfoliopilot.ledger import transactions as tl

EX = Path(__file__).parent.parent / "examples"


def hh() -> Household:
    return Household(id="h", name="h",
                     tax_profile=TaxProfile(tax_year=2026, filing_status=FilingStatus.SINGLE, state="NONE"))


def tx(id, type, qty, price="0", d=date(2026, 1, 2), acct="tax", ticker="ABC", **kw) -> Transaction:
    return Transaction(id=id, account_id=acct, ticker=ticker, type=TxType(type), date=d,
                       qty=Decimal(qty), price=Decimal(price), **kw)


# ---- exit gate: replay reproduces the sample household ----------------------

def test_replay_reproduces_sample_household_lots_exactly():
    base = Household.model_validate_json((EX / "sample_household.json").read_text())
    txs = [Transaction.model_validate(t) for t in json.loads((EX / "sample_transactions.json").read_text())]
    rebuilt = tl.replay(base, txs)
    key = lambda lot: lot.id
    fields = ("id", "account_id", "ticker", "qty", "acquired_date", "basis_per_share", "basis_source", "status")
    expected = [{f: getattr(lot, f) for f in fields} for lot in sorted(base.lots, key=key)]
    got = [{f: getattr(lot, f) for f in fields} for lot in sorted(rebuilt.lots, key=key)]
    assert got == expected
    assert rebuilt.realized == []  # no sells in the sample log


# ---- opening lots ----------------------------------------------------------

def test_buy_capitalizes_fees_and_sets_basis_source():
    h = hh()
    r = tl.apply(h, tx("b1", "buy", "100", "50", fees=Decimal(10)))
    lot = r.opened[0]
    assert lot.basis_per_share == Decimal("50.1") and lot.basis_source.value == "trade"
    assert tl.apply(h, tx("v1", "vest", "10", "200", acct="plan")).opened[0].basis_source.value == "rsu_vest"
    with pytest.raises(tl.LedgerError):
        tl.apply(h, tx("b1", "buy", "1", "1"))  # duplicate id


# ---- selling: methods, partial, realized ------------------------------------

def three_lots() -> Household:
    h = hh()
    tl.apply(h, tx("b1", "buy", "100", "10", d=date(2024, 1, 10), lot_id="old-cheap"))
    tl.apply(h, tx("b2", "buy", "100", "30", d=date(2025, 1, 10), lot_id="mid-pricey"))
    tl.apply(h, tx("b3", "buy", "100", "20", d=date(2026, 6, 1), lot_id="new-mid"))
    return h


def test_fifo_sells_oldest_first_and_splits_the_lot():
    h = three_lots()
    r = tl.apply(h, tx("s1", "sell", "150", "40", d=date(2026, 7, 1)), method="fifo")
    assert [(g.lot_id, g.qty) for g in r.realized] == [("old-cheap", Decimal(100)), ("mid-pricey", Decimal(50))]
    assert {lot.id: lot.status for lot in h.lots} == {"old-cheap": "closed", "mid-pricey": "partial", "new-mid": "open"}
    assert next(lot for lot in h.lots if lot.id == "mid-pricey").qty == Decimal(50)
    assert [g.long_term for g in r.realized] == [True, True]
    assert sum(g.gain for g in r.realized) == Decimal(100) * 30 + Decimal(50) * 10


def test_hifo_minimizes_gain_and_lifo_takes_newest():
    h = three_lots()
    r = tl.apply(h, tx("s1", "sell", "100", "40", d=date(2026, 7, 1)), method="hifo")
    assert r.realized[0].lot_id == "mid-pricey" and r.realized[0].gain == Decimal(1000)
    h = three_lots()
    r = tl.apply(h, tx("s1", "sell", "100", "40", d=date(2026, 7, 1)), method="lifo")
    assert r.realized[0].lot_id == "new-mid" and not r.realized[0].long_term  # held < 1y: short-term


def test_specific_id_overrides_method_and_validates():
    h = three_lots()
    r = tl.apply(h, tx("s1", "sell", "100", "40", d=date(2026, 7, 1), lot_ids=["new-mid"]), method="fifo")
    assert r.realized[0].lot_id == "new-mid"
    with pytest.raises(tl.LedgerError):
        tl.apply(h, tx("s2", "sell", "10", "40", d=date(2026, 7, 2), lot_ids=["nope"]))


def test_oversell_is_refused_and_fees_reduce_proceeds():
    h = three_lots()
    with pytest.raises(tl.LedgerError):
        tl.apply(h, tx("s1", "sell", "301", "40", d=date(2026, 7, 1)))
    r = tl.apply(h, tx("s2", "sell", "100", "40", d=date(2026, 7, 1), fees=Decimal(5)))
    assert r.realized[0].proceeds == Decimal(4000) - 5


def test_sell_cannot_use_lots_bought_after_the_sale_date():
    h = three_lots()
    with pytest.raises(tl.LedgerError):  # only 200 shares existed on 2026-01-15
        tl.apply(h, tx("s1", "sell", "250", "40", d=date(2026, 1, 15)))


# ---- splits and transfers --------------------------------------------------

def test_split_adjusts_every_open_lot_across_accounts_without_a_taxable_event():
    h = hh()
    tl.apply(h, tx("b1", "buy", "30", "950", d=date(2023, 5, 10), ticker="NVDA", acct="a"))
    tl.apply(h, tx("b2", "buy", "10", "900", d=date(2023, 6, 1), ticker="NVDA", acct="b"))
    r = tl.apply(h, tx("sp", "split", "10", d=date(2024, 6, 10), ticker="NVDA", acct="a"))
    assert len(r.adjusted) == 2 and not r.realized
    assert [(lot.qty, lot.basis_per_share) for lot in h.lots] == [(Decimal(300), Decimal(95)), (Decimal(100), Decimal(90))]
    assert all(lot.acquired_date.year == 2023 for lot in h.lots)


def test_transfer_moves_lot_keeping_basis_and_date():
    h = hh()
    lot = tl.apply(h, tx("b1", "buy", "100", "10", d=date(2020, 1, 1), acct="old")).opened[0]
    tl.apply(h, tx("t1", "transfer", "0", d=date(2026, 1, 1), acct="new", lot_ids=[lot.id]))
    assert lot.account_id == "new" and lot.acquired_date == date(2020, 1, 1) and lot.basis_per_share == 10


# ---- reporting ---------------------------------------------------------------

def test_realized_by_year_and_position_on():
    h = three_lots()
    tl.apply(h, tx("s1", "sell", "100", "40", d=date(2025, 12, 1)))  # old-cheap, LT +3000
    tl.apply(h, tx("s2", "sell", "50", "15", d=date(2026, 7, 1)))    # mid-pricey, LT -750
    by = tl.realized_by_year(h)
    assert by[2025].long_term == Decimal(3000) and by[2025].short_term == 0
    assert by[2026].long_term == Decimal(-750)
    assert tl.net_by_term(h, 2026) == (Decimal(0), Decimal(-750))
    assert tl.position_on(h, "ABC", date(2025, 6, 1)) == Decimal(200)   # before s1, before b3
    assert tl.position_on(h, "ABC", date(2026, 6, 15)) == Decimal(200)  # after s1 and b3, before s2
    assert tl.position_on(h, "ABC", date(2026, 12, 1)) == Decimal(150)


def test_ledger_keeps_lots_ids_stable_and_is_append_only():
    h = three_lots()
    ids_before = [lot.id for lot in h.lots]
    tl.apply(h, tx("s1", "sell", "150", "40", d=date(2026, 7, 1)))
    assert [lot.id for lot in h.lots] == ids_before
    assert [t.id for t in h.transactions] == ["b1", "b2", "b3", "s1"]
