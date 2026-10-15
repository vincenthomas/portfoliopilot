from datetime import date
from decimal import Decimal

from portfoliopilot.compliance.wash_sale import check_loss_sale
from portfoliopilot.core.models import (
    FilingStatus,
    Household,
    Lot,
    TaxProfile,
    Transaction,
    TxType,
    VestEvent,
)


def household(transactions=(), vests=()):
    return Household(
        id="h", name="h",
        tax_profile=TaxProfile(tax_year=2026, filing_status=FilingStatus.SINGLE, state="NONE"),
        lots=[Lot(id="L1", account_id="taxable", ticker="XYZ", qty=Decimal(500),
                  acquired_date=date(2026, 3, 2), basis_per_share=Decimal(62))],
        transactions=list(transactions), vest_events=list(vests),
    )


def test_clean_loss_sale_passes():
    h = household()
    r = check_loss_sale(h, h.lots[0], Decimal(500), Decimal(38), date(2026, 10, 14))
    assert r.ok


def test_purchase_in_other_account_within_window_blocks_partially():
    # 100 replacement shares bought in an IRA 16 days before the sale.
    h = household(transactions=[Transaction(
        id="T", account_id="ira", ticker="XYZ", type=TxType.BUY, date=date(2026, 9, 28),
        qty=Decimal(100), price=Decimal(40))])
    r = check_loss_sale(h, h.lots[0], Decimal(500), Decimal(38), date(2026, 10, 14))
    assert not r.ok
    # loss = (62-38)*500 = 12,000; 100/500 disallowed = 2,400
    assert r.disallowed_loss == Decimal(2400)
    assert "2026-11-14" in r.cures[0]  # 31 days after the later of sale and last conflicting purchase


def test_scheduled_rsu_vest_inside_window_blocks():
    h = household(vests=[VestEvent(grant_id="G", account_id="plan", ticker="XYZ",
                                    vest_date=date(2026, 11, 1), qty=Decimal(500))])
    r = check_loss_sale(h, h.lots[0], Decimal(500), Decimal(38), date(2026, 10, 14))
    assert not r.ok
    assert r.disallowed_loss == Decimal(12000)  # fully disallowed


def test_purchase_outside_window_is_fine():
    h = household(transactions=[Transaction(
        id="T", account_id="ira", ticker="XYZ", type=TxType.BUY, date=date(2026, 9, 1),
        qty=Decimal(100), price=Decimal(40))])
    r = check_loss_sale(h, h.lots[0], Decimal(500), Decimal(38), date(2026, 10, 14))
    assert r.ok


def test_gain_sale_never_washes():
    h = household(transactions=[Transaction(
        id="T", account_id="ira", ticker="XYZ", type=TxType.BUY, date=date(2026, 10, 10),
        qty=Decimal(100), price=Decimal(40))])
    r = check_loss_sale(h, h.lots[0], Decimal(500), Decimal(90), date(2026, 10, 14))
    assert r.ok
