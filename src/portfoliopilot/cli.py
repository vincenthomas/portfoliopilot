"""Command-line entry point: `portfoliopilot <command>`."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import typer

from portfoliopilot.core.models import Household
from portfoliopilot.ledger.ledger import Ledger
from portfoliopilot.strategies.base import HouseholdState
from portfoliopilot.strategies.glide_path import GlidePath
from portfoliopilot.strategies.tax_loss_harvest import TaxLossHarvest
from portfoliopilot.tax.engine import TaxEngine

app = typer.Typer(help="Tax-aware, lot-level investment decision support.")


def _load(household_file: Path) -> Household:
    return Household.model_validate_json(household_file.read_text())


def _prices(household: Household, prices_file: Path | None) -> dict[str, Decimal]:
    if prices_file:
        return {k.upper(): Decimal(str(v)) for k, v in json.loads(prices_file.read_text()).items()}
    from portfoliopilot.market_data.adapters.yfinance_adapter import YFinanceAdapter

    tickers = sorted({lot.ticker for lot in household.lots} | {household.constraints.diversified_core_ticker})
    return {t: q.price for t, q in YFinanceAdapter().quotes(tickers).items()}


@app.command()
def positions(household_file: Path, prices_file: Path | None = None) -> None:
    """Show netted positions and concentration across all accounts."""
    h = _load(household_file)
    prices = _prices(h, prices_file)
    mv = Ledger.market_values(h, prices)
    total = sum(mv.values())
    typer.echo(f"Household: {h.name}   total ${total:,.0f}")
    for t, w in Ledger.concentration(h, prices).items():
        flag = " RESTRICTED" if t in {x.upper() for x in h.constraints.restricted_tickers} else ""
        typer.echo(f"  {t:<6} {w:6.1%}  ${mv[t]:>12,.0f}{flag}")


@app.command()
def advise(household_file: Path, prices_file: Path | None = None, as_of: str | None = None) -> None:
    """Run the strategy library and print proposed, tax-priced, compliance-gated tickets."""
    h = _load(household_file)
    state = HouseholdState(
        household=h, prices=_prices(h, prices_file),
        as_of=date.fromisoformat(as_of) if as_of else datetime.now(UTC).date(),
        tax_engine=TaxEngine(h.tax_profile.tax_year),
    )
    for strategy in (TaxLossHarvest(), GlidePath()):
        trades = strategy.propose(state)
        typer.echo(f"\n== {strategy.name}: {len(trades)} tickets ==")
        for t in trades:
            gate = ""
            if t.compliance and not t.compliance.ok:
                gate = "  [BLOCKED]"
            elif t.requires_scheduled_plan:
                gate = "  [10b5-1 queue]"
            typer.echo(
                f"{t.side.upper():<4} {t.qty:>8} {t.ticker:<6} @ ${t.est_price:,.2f}  "
                f"gain ${t.est_gain:>10,.0f}  tax ${t.est_tax:>9,.0f}  {t.account_id}{gate}"
            )
            typer.echo(f"     {t.rationale}")


@app.command()
def headroom(household_file: Path) -> None:
    """Dollars of long-term gain before the next federal LT bracket."""
    h = _load(household_file)
    room, rate = TaxEngine(h.tax_profile.tax_year).lt_headroom(h.tax_profile)
    typer.echo(f"${room:,.0f} of LT gain remains at the {rate:.0%} federal LT rate")


if __name__ == "__main__":
    app()
