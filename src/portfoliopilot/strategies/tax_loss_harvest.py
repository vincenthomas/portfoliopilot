"""TaxLossHarvest: realize losses, short-term first, with the wash-sale gate attached."""

from __future__ import annotations

from decimal import Decimal

from portfoliopilot.compliance.wash_sale import check_loss_sale
from portfoliopilot.core.models import Trade
from portfoliopilot.ledger.ledger import Ledger
from portfoliopilot.strategies.base import HouseholdState, next_trade_id


class TaxLossHarvest:
    name = "tax_loss_harvest"

    def __init__(self, min_loss: Decimal = Decimal(500)):
        self.min_loss = min_loss

    def propose(self, state: HouseholdState) -> list[Trade]:
        h, prices = state.household, state.prices
        candidates = []
        for lot in Ledger.open_lots(h):
            price = prices.get(lot.ticker.upper())
            if price is None:
                continue
            loss = (lot.basis_per_share - price) * lot.qty
            if loss >= self.min_loss:
                candidates.append((lot, price, loss))
        # Short-term losses first: they shelter ordinary income before spilling to the $3k limit.
        candidates.sort(key=lambda c: (c[0].is_long_term(state.as_of), -c[2]))

        trades: list[Trade] = []
        for lot, price, loss in candidates:
            compliance = check_loss_sale(h, lot, lot.qty, price, state.as_of)
            term = "LT" if lot.is_long_term(state.as_of) else "ST"
            trades.append(Trade(
                id=next_trade_id("TLH"), strategy_id=self.name, account_id=lot.account_id,
                ticker=lot.ticker, lot_ids=[lot.id], side="sell", qty=lot.qty, est_price=price,
                est_gain=-loss, est_tax=Decimal(0), compliance=compliance,
                rationale=f"Harvest ${loss:,.0f} {term} loss on lot {lot.id}"
                          + ("" if compliance.ok else " — BLOCKED: " + compliance.reasons[0]),
            ))
        return trades
