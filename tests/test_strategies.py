import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from portfoliopilot.core.models import Household
from portfoliopilot.ledger.ledger import Ledger
from portfoliopilot.strategies.base import HouseholdState
from portfoliopilot.strategies.glide_path import GlidePath
from portfoliopilot.strategies.tax_loss_harvest import TaxLossHarvest
from portfoliopilot.tax.engine import TaxEngine

EX = Path(__file__).parent.parent / "examples"


def state():
    h = Household.model_validate_json((EX / "sample_household.json").read_text())
    prices = {k: Decimal(str(v)) for k, v in json.loads((EX / "sample_prices.json").read_text()).items()}
    return HouseholdState(household=h, prices=prices, as_of=date(2026, 10, 14), tax_engine=TaxEngine(2026))


def test_concentration_is_netted_across_accounts():
    s = state()
    w = Ledger.concentration(s.household, s.prices)
    # VTI held in IRA and brokerage: 1900 shares, not two separate positions
    assert Ledger.positions(s.household)["VTI"] == Decimal(1900)
    assert w["MSFT"] > 0.4


def test_glide_path_sells_highest_basis_lot_first_and_pairs_a_buy():
    trades = GlidePath(years=4).propose(state())
    sells = [t for t in trades if t.side == "sell" and t.ticker == "MSFT"]
    assert sells, "MSFT is over target; expected a sell"
    assert sells[0].lot_ids == ["L2"]  # basis 405 before basis 135
    assert sells[0].requires_scheduled_plan  # MSFT is restricted
    buy = next(t for t in trades if t.id == sells[0].paired_trade_id)
    assert buy.ticker == "VTI" and buy.side == "buy"


def test_tlh_blocks_the_wash_sale_and_flags_it():
    trades = TaxLossHarvest().propose(state())
    xyz = next(t for t in trades if t.ticker == "XYZ")
    assert xyz.compliance is not None and not xyz.compliance.ok
    assert "BLOCKED" in xyz.rationale
