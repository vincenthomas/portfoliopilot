"""Each broker fixture reproduces that custodian's documented export layout. The expected
results are hand-built; a parser is correct when it matches them exactly."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from portfoliopilot.core.models import BasisSource, TxType
from portfoliopilot.ingest import registry
from portfoliopilot.ingest.base import money, parse_date

FX = Path(__file__).parent / "fixtures" / "brokers"
AS_OF = date(2026, 10, 15)


def lots(res):
    return sorted((lot.ticker, lot.acquired_date, lot.qty, lot.basis_per_share, lot.basis_source) for lot in res.lots)


def txs(res):
    return [(t.date, t.type, t.ticker, t.qty, t.price, t.fees) for t in res.transactions]


# ---- helpers ------------------------------------------------------------------

def test_money_and_date_helpers():
    assert money("$1,234.56") == Decimal("1234.56")
    assert money("($12.30)") == Decimal("-12.30")
    assert money("--") is None and money("n/a") is None and money("+$4800.00") == Decimal(4800)
    assert parse_date("03/15/2021") == date(2021, 3, 15)
    assert parse_date("2026-10-15") == date(2026, 10, 15)
    assert parse_date("10/15/2026 as of 4pm") == date(2026, 10, 15)


# ---- detection ----------------------------------------------------------------

@pytest.mark.parametrize("file,expected", [
    ("schwab_positions_lots.csv", "schwab_positions"),
    ("schwab_positions_only.csv", "schwab_positions"),
    ("fidelity_positions.csv", "fidelity_positions"),
    ("fidelity_lots.csv", "fidelity_positions"),
    ("fidelity_history.csv", "fidelity_history"),
    ("etrade_portfolio.csv", "etrade_portfolio"),
    ("etrade_lots.csv", "etrade_portfolio"),
    ("vanguard_download.csv", "vanguard"),
    ("robinhood_activity.csv", "robinhood_activity"),
    ("statement.qfx", "ofx"),
])
def test_detects_each_fixture(file, expected):
    text = (FX / file).read_text()
    assert registry.detect(text)[0] == expected


# ---- Schwab -------------------------------------------------------------------

def test_schwab_lot_details():
    res = registry.parse(FX / "schwab_positions_lots.csv", "schwab-1", as_of=AS_OF)
    assert not res.approximate
    assert lots(res) == [
        ("AAPL", date(2021, 3, 15), Decimal(100), Decimal(110), BasisSource.TRADE),
        ("AAPL", date(2022, 9, 2), Decimal(50), Decimal(150), BasisSource.TRADE),
        ("BRK.B", date(2020, 1, 10), Decimal(20), Decimal(300), BasisSource.TRADE),
    ]
    assert {lot.id for lot in res.lots} == {"schwab-1-AAPL-20210315-1", "schwab-1-AAPL-20220902-2", "schwab-1-BRK.B-20200110-1"}


def test_schwab_positions_only_builds_average_cost_lots_and_warns():
    res = registry.parse(FX / "schwab_positions_only.csv", "roth", as_of=AS_OF)
    assert res.approximate and res.warnings
    assert lots(res) == [("VTI", AS_OF, Decimal(300), Decimal("200.0000"), BasisSource.TRADE)]


# ---- Fidelity -----------------------------------------------------------------

def test_fidelity_positions_skips_money_market_and_uses_average_cost():
    res = registry.parse(FX / "fidelity_positions.csv", "fid", as_of=AS_OF)
    assert res.approximate
    assert lots(res) == [
        ("GLD", AS_OF, Decimal(25), Decimal("160.0000"), BasisSource.TRADE),
        ("MSFT", AS_OF, Decimal(40), Decimal("400.0000"), BasisSource.TRADE),
    ]
    assert res.skipped == 1  # SPAXX**


def test_fidelity_lots():
    res = registry.parse(FX / "fidelity_lots.csv", "fid", as_of=AS_OF)
    assert not res.approximate
    assert lots(res) == [
        ("MSFT", date(2024, 2, 15), Decimal(25), Decimal(405), BasisSource.TRADE),
        ("MSFT", date(2025, 8, 15), Decimal(15), Decimal("391.67"), BasisSource.TRADE),
    ]


def test_fidelity_history_maps_actions_and_skips_cash_dividends():
    res = registry.parse(FX / "fidelity_history.csv", "fid")
    assert txs(res) == [
        (date(2026, 10, 1), TxType.BUY, "NVDA", Decimal(10), Decimal(180), Decimal(0)),
        (date(2026, 9, 15), TxType.SELL, "GLD", Decimal(5), Decimal(305), Decimal("0.04")),
        (date(2026, 9, 10), TxType.DRIP, "MSFT", Decimal("0.064"), Decimal("518.75"), Decimal(0)),
        (date(2026, 8, 15), TxType.VEST, "MSFT", Decimal(15), Decimal("391.67"), Decimal(0)),
    ]
    assert res.skipped == 1  # the cash dividend row


# ---- E*TRADE ------------------------------------------------------------------

def test_etrade_portfolio_is_average_cost():
    res = registry.parse(FX / "etrade_portfolio.csv", "et", as_of=AS_OF)
    assert res.approximate
    assert lots(res) == [
        ("AMZN", AS_OF, Decimal(80), Decimal(95), BasisSource.TRADE),
        ("AVGO", AS_OF, Decimal(12), Decimal(480), BasisSource.TRADE),
    ]


def test_etrade_lots_have_dates():
    res = registry.parse(FX / "etrade_lots.csv", "et")
    assert not res.approximate
    assert lots(res) == [
        ("AMZN", date(2019, 6, 1), Decimal(50), Decimal(90), BasisSource.TRADE),
        ("AMZN", date(2022, 3, 15), Decimal(30), Decimal("103.33"), BasisSource.TRADE),
    ]


# ---- Vanguard -----------------------------------------------------------------

def test_vanguard_transactions_only_share_moving_rows():
    res = registry.parse(FX / "vanguard_download.csv", "vg")
    assert res.lots == []  # no cost basis in positions block
    assert txs(res) == [
        (date(2021, 1, 4), TxType.BUY, "VTI", Decimal(60), Decimal(190), Decimal(0)),
        (date(2025, 6, 1), TxType.BUY, "VTI", Decimal(40), Decimal(270), Decimal(0)),
        (date(2025, 9, 26), TxType.DRIP, "VTI", Decimal("0.31"), Decimal("306.45"), Decimal(0)),
    ]
    assert res.skipped == 2  # Dividend (cash) and Sweep


# ---- Robinhood ----------------------------------------------------------------

def test_robinhood_activity():
    res = registry.parse(FX / "robinhood_activity.csv", "rh")
    assert txs(res) == [
        (date(2026, 10, 14), TxType.BUY, "AAPL", Decimal(10), Decimal(230), Decimal(0)),
        (date(2026, 9, 30), TxType.SELL, "TSLA", Decimal(3), Decimal(410), Decimal(0)),
    ]
    assert any("REC" in w for w in res.warnings)  # received shares need a basis
    assert res.skipped == 3  # CDIV, ACH, REC


# ---- OFX / QFX ----------------------------------------------------------------

def test_ofx_maps_cusips_positions_and_trades():
    res = registry.parse(FX / "statement.qfx", "ofx-acct")
    assert res.approximate
    assert lots(res) == [
        ("AAPL", date(2026, 10, 15), Decimal(60), Decimal("230.10"), BasisSource.TRADE),
        ("MSFT", date(2026, 10, 15), Decimal(36), Decimal("520.00"), BasisSource.TRADE),
    ]
    assert txs(res) == [
        (date(2026, 9, 5), TxType.BUY, "AAPL", Decimal(10), Decimal("228.50"), Decimal(0)),
        (date(2026, 10, 1), TxType.SELL, "MSFT", Decimal(4), Decimal("515.00"), Decimal("0.05")),
    ]
    assert res.transactions[0].id == "ofx-acct-T1001"


# ---- end to end: transactions from a parser feed the ledger ------------------

def test_vanguard_history_replays_into_lots():
    from portfoliopilot.core.models import FilingStatus, Household, TaxProfile
    from portfoliopilot.ledger import transactions as tl

    res = registry.parse(FX / "vanguard_download.csv", "vg")
    h = Household(id="h", name="h", tax_profile=TaxProfile(tax_year=2026, filing_status=FilingStatus.SINGLE, state="NONE"))
    tl.apply_all(h, res.transactions)
    assert [(lot.qty, lot.basis_per_share) for lot in h.lots] == [
        (Decimal(60), Decimal(190)), (Decimal(40), Decimal(270)), (Decimal("0.31"), Decimal("306.45"))]
    assert h.lots[2].basis_source == BasisSource.DRIP


def test_unknown_file_raises():
    p = FX.parent / "nope.csv"
    p.write_text("a,b,c\n1,2,3\n")
    try:
        with pytest.raises(ValueError, match="no parser recognizes"):
            registry.parse(p, "x")
    finally:
        p.unlink()
