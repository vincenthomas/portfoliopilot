"""Restricted-ticker gate: employer stock and anything the user flags must go
through a scheduled plan (e.g. a Rule 10b5-1 plan), never direct execution."""

from __future__ import annotations

from portfoliopilot.core.models import ComplianceResult, ConstraintsProfile, Trade


def check_restricted(trade: Trade, constraints: ConstraintsProfile) -> ComplianceResult:
    if trade.ticker.upper() in {t.upper() for t in constraints.restricted_tickers}:
        trade.requires_scheduled_plan = True
        return ComplianceResult(
            ok=True,
            reasons=[f"{trade.ticker} is restricted; routed to scheduled-plan queue"],
            cures=["Enter this ticket into a 10b5-1 plan during an open trading window"],
        )
    return ComplianceResult(ok=True)
