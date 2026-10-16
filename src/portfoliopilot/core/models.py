"""Core domain models. Lots are the atomic unit; nothing is modeled at ticker level."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, computed_field

Money = Decimal  # stored and computed in Decimal; never float


class FilingStatus(str, Enum):
    SINGLE = "single"
    MFJ = "married_filing_jointly"
    MFS = "married_filing_separately"
    HOH = "head_of_household"


class AccountType(str, Enum):
    TAXABLE = "taxable"
    RETIREMENT = "retirement"  # 401k, IRA: no capital-gains tax on trades
    EQUITY_COMP = "equity_comp"  # RSU/ESPP plan accounts


class BasisSource(str, Enum):
    TRADE = "trade"
    RSU_VEST = "rsu_vest"
    ESPP = "espp"
    GIFT = "gift"
    INHERITED = "inherited"
    DRIP = "drip"


class TxType(str, Enum):
    BUY = "buy"
    SELL = "sell"
    VEST = "vest"
    ESPP = "espp"
    DRIP = "drip"
    GIFT = "gift"
    TRANSFER = "transfer"
    SPLIT = "split"


class TaxProfile(BaseModel):
    """Everything the tax engine needs about the user. Fully user-editable."""

    tax_year: int
    filing_status: FilingStatus
    state: str = Field(description="Two-letter US state code, e.g. 'CA', or 'NONE'")
    projected_ordinary_income: Money = Decimal(0)
    projected_lt_gains_ytd: Money = Decimal(0)
    projected_st_gains_ytd: Money = Decimal(0)


class ConstraintsProfile(BaseModel):
    """What the user considers acceptable. Drives guardrails and strategy targets."""

    max_single_name_pct: float = 0.10
    core_floor_pct: float = 0.0
    permanent_core_tickers: list[str] = []
    restricted_tickers: list[str] = Field(
        default_factory=list,
        description="Employer stock or anything that must go through a scheduled plan",
    )
    income_correlated_tickers: list[str] = Field(
        default_factory=list, description="Tickers correlated with the user's paycheck"
    )
    annual_lt_gain_budget: Money | None = None
    cash_reserve: Money = Decimal(0)
    diversified_core_ticker: str = "VTI"


class Account(BaseModel):
    id: str
    household_id: str
    owner: str
    custodian: str
    type: AccountType
    restricted: bool = False


class Lot(BaseModel):
    id: str
    account_id: str
    ticker: str
    qty: Decimal
    acquired_date: date
    basis_per_share: Money
    basis_source: BasisSource = BasisSource.TRADE
    status: Literal["open", "partial", "closed"] = "open"
    parent_lot_id: str | None = None

    @computed_field  # type: ignore[misc]
    @property
    def basis_total(self) -> Money:
        return self.qty * self.basis_per_share

    def is_long_term(self, as_of: date) -> bool:
        """IRS rule: held more than one year (day after acquisition counts as day 1)."""
        anniversary = self.acquired_date.replace(year=self.acquired_date.year + 1)
        return as_of > anniversary

    def unrealized_gain(self, price: Money) -> Money:
        return (price - self.basis_per_share) * self.qty


class Transaction(BaseModel):
    """One ledger event. Semantics by type:
    buy/vest/espp/drip/gift : opens a lot of `qty` at `price` (gift: donor's basis)
    sell                    : closes `qty` shares; `lot_ids` = specific-ID selection, else method
    split                   : `qty` is the ratio (new shares per old), e.g. 4 for 4-for-1
    transfer                : moves `lot_ids` into `account_id`; basis and dates carry over
    """

    id: str
    account_id: str
    ticker: str
    type: TxType
    date: date
    qty: Decimal
    price: Money = Decimal(0)
    fees: Money = Decimal(0)
    lot_ids: list[str] = []
    lot_id: str | None = Field(default=None, description="Id for the lot this event opens")
    note: str = ""


class RealizedGain(BaseModel):
    """One closed slice of a lot. The tax engine and Schedule D consume these."""

    tx_id: str
    lot_id: str
    account_id: str
    ticker: str
    qty: Decimal
    acquired_date: date
    sold_date: date
    proceeds: Money  # net of fees
    basis: Money
    long_term: bool

    @computed_field  # type: ignore[misc]
    @property
    def gain(self) -> Money:
        return self.proceeds - self.basis


class VestEvent(BaseModel):
    grant_id: str
    account_id: str
    ticker: str
    vest_date: date
    qty: Decimal
    est_price: Money | None = None
    status: Literal["unvested", "vested", "sold"] = "unvested"


class Household(BaseModel):
    id: str
    name: str
    tax_profile: TaxProfile
    constraints: ConstraintsProfile = ConstraintsProfile()
    accounts: list[Account] = []
    lots: list[Lot] = []
    transactions: list[Transaction] = []
    vest_events: list[VestEvent] = []
    realized: list[RealizedGain] = []


class ComplianceResult(BaseModel):
    ok: bool
    reasons: list[str] = []
    cures: list[str] = []
    disallowed_loss: Money = Decimal(0)


class Trade(BaseModel):
    """A proposed trade, always priced and gated before it reaches the advice engine."""

    id: str
    strategy_id: str
    account_id: str
    ticker: str
    lot_ids: list[str]
    side: Literal["buy", "sell"]
    qty: Decimal
    est_price: Money
    est_gain: Money = Decimal(0)
    est_tax: Money = Decimal(0)
    rationale: str = ""
    paired_trade_id: str | None = None
    compliance: ComplianceResult | None = None
    requires_scheduled_plan: bool = False

    @computed_field  # type: ignore[misc]
    @property
    def est_after_tax_proceeds(self) -> Money:
        gross = self.qty * self.est_price
        return gross - self.est_tax if self.side == "sell" else -gross
