"""GlidePath: walk any over-concentrated name toward its target weight.

Sells highest-basis lots first (cheapest tax per dollar of risk removed),
stays within the annual LT-gain budget, never sells below the core floor,
and pairs every sell with a buy into the diversified core.
"""

from __future__ import annotations

from decimal import Decimal

from portfoliopilot.compliance.restricted import check_restricted
from portfoliopilot.core.models import Trade
from portfoliopilot.ledger.ledger import Ledger
from portfoliopilot.strategies.base import HouseholdState, next_trade_id


class GlidePath:
    name = "glide_path"

    def __init__(self, years: int = 4):
        self.years = years

    def propose(self, state: HouseholdState) -> list[Trade]:
        h, prices = state.household, state.prices
        c = h.constraints
        weights = Ledger.concentration(h, prices)
        total_mv = sum(Ledger.market_values(h, prices).values())
        budget = c.annual_lt_gain_budget
        if budget is None:
            headroom, _ = state.tax_engine.lt_headroom(h.tax_profile)
            budget = headroom if headroom.is_finite() else Decimal("Infinity")
        gain_used = Decimal(0)
        trades: list[Trade] = []

        core = c.diversified_core_ticker.upper()
        for ticker, w in weights.items():
            if ticker == core:
                continue  # the diversified core is the destination, never a concentration risk
            floor = c.core_floor_pct if ticker in {t.upper() for t in c.permanent_core_tickers} else 0.0
            target = max(c.max_single_name_pct, floor)
            if w <= target:
                continue
            # Spread the excess over the remaining glide years.
            excess_value = Decimal(str(w - target)) * total_mv / self.years
            price = prices[ticker]
            lots = sorted(
                Ledger.open_lots(h, ticker),
                key=lambda lot: -lot.basis_per_share,  # highest basis first
            )
            remaining_value = excess_value
            for lot in lots:
                if remaining_value <= 0:
                    break
                qty = min(lot.qty, (remaining_value / price).quantize(Decimal(1)))
                if qty <= 0:
                    continue
                est = state.tax_engine.estimate_sale(lot, qty, price, h.tax_profile, state.as_of)
                if est.is_long_term and est.gain > 0 and gain_used + est.gain > budget:
                    # Scale down to fit the remaining budget.
                    room = budget - gain_used
                    per_share_gain = price - lot.basis_per_share
                    qty = (room / per_share_gain).quantize(Decimal(1)) if per_share_gain > 0 else qty
                    if qty <= 0:
                        break
                    est = state.tax_engine.estimate_sale(lot, qty, price, h.tax_profile, state.as_of)
                sell = Trade(
                    id=next_trade_id("GP"), strategy_id=self.name, account_id=lot.account_id,
                    ticker=ticker, lot_ids=[lot.id], side="sell", qty=qty, est_price=price,
                    est_gain=est.gain, est_tax=est.total,
                    rationale=(f"{ticker} is {w:.1%} of household vs {target:.0%} target; "
                               f"lot {lot.id} (basis ${lot.basis_per_share}) is the highest-basis open lot"),
                )
                sell.compliance = check_restricted(sell, c)
                buy = Trade(
                    id=next_trade_id("GP"), strategy_id=self.name, account_id=lot.account_id,
                    ticker=c.diversified_core_ticker, lot_ids=[], side="buy",
                    qty=(sell.est_after_tax_proceeds / prices.get(c.diversified_core_ticker, price)).quantize(Decimal(1)),
                    est_price=prices.get(c.diversified_core_ticker, price),
                    rationale=f"Reinvest after-tax proceeds of {sell.id} into {c.diversified_core_ticker}",
                )
                sell.paired_trade_id, buy.paired_trade_id = buy.id, sell.id
                trades += [sell, buy]
                gain_used += max(est.gain, Decimal(0))
                remaining_value -= qty * price
        return trades
