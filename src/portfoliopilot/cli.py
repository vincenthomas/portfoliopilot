"""Command-line entry point: `portfoliopilot <command>`."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import typer

from portfoliopilot.core.models import Household
from portfoliopilot.ledger.ledger import Ledger
from portfoliopilot.ledger.prices import PriceStore
from portfoliopilot.market_data.service import MarketDataService
from portfoliopilot.strategies.base import HouseholdState
from portfoliopilot.strategies.glide_path import GlidePath
from portfoliopilot.strategies.tax_loss_harvest import TaxLossHarvest
from portfoliopilot.tax.engine import TaxEngine

app = typer.Typer(help="Tax-aware, lot-level investment decision support.")


def _load(household_file: Path) -> Household:
    return Household.model_validate_json(household_file.read_text())


TICKERS_ARG = typer.Argument(..., help="Tickers, or a household JSON path to sync its holdings")
DB_OPTION = typer.Option("portfoliopilot.duckdb", "--db", envvar="PORTFOLIOPILOT_DB",
                         help="DuckDB file holding the price cache and ledger")


def _service(db: Path, adapter: str = "yfinance") -> MarketDataService:
    from portfoliopilot.market_data.registry import load

    return MarketDataService(load(adapter), PriceStore.open(db))


def _prices(household: Household, prices_file: Path | None, db: Path) -> dict[str, Decimal]:
    if prices_file:
        return {k.upper(): Decimal(str(v)) for k, v in json.loads(prices_file.read_text()).items()}
    tickers = sorted({lot.ticker for lot in household.lots} | {household.constraints.diversified_core_ticker})
    return _service(db).prices(tickers)


@app.command()
def positions(household_file: Path, prices_file: Path | None = None, db: Path = DB_OPTION) -> None:
    """Show netted positions and concentration across all accounts."""
    h = _load(household_file)
    prices = _prices(h, prices_file, db)
    mv = Ledger.market_values(h, prices)
    total = sum(mv.values())
    typer.echo(f"Household: {h.name}   total ${total:,.0f}")
    for t, w in Ledger.concentration(h, prices).items():
        flag = " RESTRICTED" if t in {x.upper() for x in h.constraints.restricted_tickers} else ""
        typer.echo(f"  {t:<6} {w:6.1%}  ${mv[t]:>12,.0f}{flag}")


@app.command()
def advise(household_file: Path, prices_file: Path | None = None, as_of: str | None = None,
           db: Path = DB_OPTION) -> None:
    """Run the strategy library and print proposed, tax-priced, compliance-gated tickets."""
    h = _load(household_file)
    state = HouseholdState(
        household=h, prices=_prices(h, prices_file, db),
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


prices_app = typer.Typer(help="Historical price cache: sync from a provider, inspect, export.")
app.add_typer(prices_app, name="prices")


@prices_app.command("sync")
def prices_sync(
    tickers: list[str] = TICKERS_ARG,
    start: str = typer.Option("2015-01-01", help="YYYY-MM-DD"),
    end: str | None = typer.Option(None, help="YYYY-MM-DD, default today"),
    adapter: str = typer.Option("yfinance", help="Market-data adapter name"),
    db: Path = DB_OPTION,
) -> None:
    """Fetch only the missing date ranges and persist them."""
    if len(tickers) == 1 and tickers[0].endswith(".json"):
        h = _load(Path(tickers[0]))
        tickers = sorted({lot.ticker for lot in h.lots} | {h.constraints.diversified_core_ticker})
    svc = _service(db, adapter)
    end_d = date.fromisoformat(end) if end else datetime.now(UTC).date()
    for r in svc.sync_many(tickers, date.fromisoformat(start), end_d):
        span = f"{r.stored_first} .. {r.stored_last}" if r.stored_first else "empty"
        typer.echo(f"{r.ticker:<8} fetched {r.fetched:>6}  stored {r.stored_count:>6}  {span}")


@prices_app.command("show")
def prices_show(ticker: str, last: int = 10, db: Path = DB_OPTION) -> None:
    """Print coverage and the most recent bars for one ticker."""
    store = PriceStore.open(db)
    cov = store.coverage(ticker)
    if not cov.count:
        typer.echo(f"{ticker.upper()}: no bars stored"); raise typer.Exit(1)
    typer.echo(f"{cov.ticker}: {cov.count} bars, {cov.first} .. {cov.last}")
    for b in store.bars(ticker, cov.last - timedelta(days=last * 2), cov.last)[-last:]:
        typer.echo(f"  {b.ts}  O {b.open:>10.2f}  H {b.high:>10.2f}  L {b.low:>10.2f}  "
                   f"C {b.close:>10.2f}  V {b.volume:>12,}  {b.source}")


@prices_app.command("list")
def prices_list(db: Path = DB_OPTION) -> None:
    """List every ticker in the cache with its coverage."""
    store = PriceStore.open(db)
    for t in store.tickers():
        c = store.coverage(t)
        typer.echo(f"{t:<8} {c.count:>6} bars  {c.first} .. {c.last}")


@prices_app.command("export")
def prices_export(path: Path, db: Path = DB_OPTION) -> None:
    """Write the whole cache to a Parquet file."""
    n = PriceStore.open(db).export_parquet(path)
    typer.echo(f"exported {n} bars to {path}")


@prices_app.command("import")
def prices_import(path: Path, db: Path = DB_OPTION) -> None:
    """Merge a Parquet file into the cache."""
    n = PriceStore.open(db).import_parquet(path)
    typer.echo(f"imported {n} new bars from {path}")


if __name__ == "__main__":
    app()
