from datetime import date
from decimal import Decimal

import pytest

from portfoliopilot.core.models import FilingStatus, Lot, TaxProfile
from portfoliopilot.tax.engine import TaxEngine


@pytest.fixture
def engine():
    return TaxEngine(2026)


def lot(ticker="ABC", basis="100", acquired=date(2020, 1, 15)):
    return Lot(id="L", account_id="A", ticker=ticker, qty=Decimal(100),
               acquired_date=acquired, basis_per_share=Decimal(basis))


def test_long_term_boundary_is_more_than_one_year():
    lt = lot(acquired=date(2025, 10, 14))
    assert not lt.is_long_term(date(2026, 10, 14))  # exactly one year: still short-term
    assert lt.is_long_term(date(2026, 10, 15))


def test_lt_gain_in_15_percent_bracket_mfj_no_state(engine):
    p = TaxProfile(tax_year=2026, filing_status=FilingStatus.MFJ, state="TX",
                   projected_ordinary_income=Decimal(200000))
    est = engine.estimate_sale(lot(), Decimal(100), Decimal(200), p, date(2026, 10, 14))
    assert est.gain == Decimal(10000)
    assert est.is_long_term
    assert est.federal_tax == Decimal(1500)  # 15%
    assert est.state_tax == 0
    # NIIT: MAGI 210k vs 250k threshold -> no NIIT
    assert est.niit_tax == 0


def test_lt_gain_straddling_15_to_20_boundary(engine):
    # Floor sits 4,000 below the 613,700 boundary; a 10,000 gain splits 4,000 @15% + 6,000 @20%.
    p = TaxProfile(tax_year=2026, filing_status=FilingStatus.MFJ, state="NONE",
                   projected_ordinary_income=Decimal(609700))
    est = engine.estimate_sale(lot(), Decimal(100), Decimal(200), p, date(2026, 10, 14))
    assert est.federal_tax == Decimal(600) + Decimal(1200)
    assert est.niit_tax == Decimal(380)  # fully over threshold


def test_california_taxes_gains_as_ordinary(engine):
    p = TaxProfile(tax_year=2026, filing_status=FilingStatus.MFJ, state="CA",
                   projected_ordinary_income=Decimal(300000))
    est = engine.estimate_sale(lot(), Decimal(100), Decimal(200), p, date(2026, 10, 14))
    assert est.state_tax == Decimal(10000) * Decimal("0.133")


def test_collectible_capped_at_28(engine):
    p = TaxProfile(tax_year=2026, filing_status=FilingStatus.MFJ, state="NONE",
                   projected_ordinary_income=Decimal(900000))  # 37% ordinary bracket
    est = engine.estimate_sale(lot("GLD"), Decimal(100), Decimal(200), p, date(2026, 10, 14))
    assert est.is_collectible
    assert est.federal_tax == Decimal(10000) * Decimal("0.28")


def test_short_term_gain_uses_ordinary_brackets(engine):
    p = TaxProfile(tax_year=2026, filing_status=FilingStatus.SINGLE, state="NONE",
                   projected_ordinary_income=Decimal(300000))  # 35% bracket for single
    est = engine.estimate_sale(lot(acquired=date(2026, 6, 1)), Decimal(100), Decimal(200), p,
                               date(2026, 10, 14))
    assert not est.is_long_term
    assert est.federal_tax == Decimal(3500)


def test_loss_has_no_tax(engine):
    p = TaxProfile(tax_year=2026, filing_status=FilingStatus.MFJ, state="CA")
    est = engine.estimate_sale(lot(), Decimal(100), Decimal(50), p, date(2026, 10, 14))
    assert est.gain == Decimal(-5000)
    assert est.total == 0


def test_headroom(engine):
    p = TaxProfile(tax_year=2026, filing_status=FilingStatus.MFJ, state="CA",
                   projected_ordinary_income=Decimal(480000), projected_lt_gains_ytd=Decimal(20000))
    room, rate = engine.lt_headroom(p)
    assert room == Decimal(113700)
    assert rate == Decimal("0.15")
