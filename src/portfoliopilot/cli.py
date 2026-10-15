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
from portfoliopilot.market_data.universe import UniverseStore, sp500, sp500_history, yahoo_symbol
from portfoliopilot.strategies.base import HouseholdState
from portfoliopilot.strategies.glide_path import GlidePath
from portfoliopilot.strategies.tax_loss_harvest import TaxLossHarvest
from portfoliopilot.tax.engine import TaxEngine

app = typer.Typer(help="Tax-aware, lot-level investment decision support.")


def _load(household_file: Path) -> Household:
    return Household.model_validate_json(household_file.read_text())


UNIVERSE_ARG = typer.Argument("sp500", help="Universe name")
TICKERS_ARG = typer.Argument(None, help="Tickers, or a household JSON path to sync its holdings")
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
    tickers: list[str] | None = TICKERS_ARG,
    universe: str | None = typer.Option(None, help="Sync a stored universe, e.g. sp500"),
    historical: bool = typer.Option(False, help="With --universe: also every symbol that was EVER a member "
                                                 "(delisted names, through their last member day)"),
    upgrade: bool = typer.Option(False, help="Re-download tickers stored before adjustments/dividends "
                                              "existed (one-time after a schema change)"),
    start: str = typer.Option("2015-01-01", help="YYYY-MM-DD"),
    end: str | None = typer.Option(None, help="YYYY-MM-DD, default today"),
    adapter: str = typer.Option("yfinance", help="Market-data adapter name"),
    db: Path = DB_OPTION,
) -> None:
    """Fetch only the missing date ranges and persist them. Safe to re-run; resumes where it left off."""
    tickers = list(tickers or [])
    today = datetime.now(UTC).date()
    if upgrade:
        svc = _service(db, adapter)
        todo = svc.store.tickers_without_adjustments()
        typer.echo(f"upgrading {len(todo)} tickers to include adj_close, dividends, splits")
        n = 0
        for r in svc.upgrade(todo, progress=lambda r: typer.echo(
                f"  {r.ticker:<8} {r.stored_count:>6} bars  {r.stored_first} .. {r.stored_last}"
                + ("  [EMPTY]" if r.empty else ""))):
            n += 1
        typer.echo(f"upgraded {n} tickers")
        return
    if len(tickers) == 1 and tickers[0].endswith(".json"):
        h = _load(Path(tickers[0]))
        tickers = sorted({lot.ticker for lot in h.lots} | {h.constraints.diversified_core_ticker})
    if universe:
        store = UniverseStore(PriceStore.open(db).con)
        syms = store.symbols(universe)
        if not syms:
            typer.echo(f"universe '{universe}' is empty; run `portfoliopilot universe refresh {universe}` first")
            raise typer.Exit(1)
        tickers += [yahoo_symbol(s) for s in syms]
        if historical:
            current = set(syms)
            gone = [s for s in store.all_historical_symbols(universe) if s not in current]
            ends = {}
            for sym, _, end_d in store.con.execute(
                    "SELECT symbol, start_date, max(end_date) FROM universe_history WHERE universe = ? "
                    "GROUP BY symbol, start_date", [universe]).fetchall():
                if end_d:
                    ends[sym] = max(ends.get(sym, end_d), end_d)
            svc = _service(db, adapter)
            typer.echo(f"syncing {len(gone)} former members through their last member day")
            _, unserved = svc.sync_delisted(
                [yahoo_symbol(s) for s in gone], {yahoo_symbol(k): v for k, v in ends.items()},
                date.fromisoformat(start), today,
                progress=lambda r: typer.echo(f"  {r.ticker:<8} {r.stored_count:>6} bars"
                                              + ("  [unavailable]" if r.empty else "")))
            typer.echo(f"former members: {len(gone) - len(unserved)} with history, "
                       f"{len(unserved)} unavailable from this provider (see `prices verify`)")
    if not tickers:
        typer.echo("nothing to sync: give tickers, a household JSON, or --universe"); raise typer.Exit(1)
    svc = _service(db, adapter)
    end_d = date.fromisoformat(end) if end else today
    total = {"fetched": 0, "n": 0}

    def show(r):
        total["fetched"] += r.fetched; total["n"] += 1
        span = f"{r.stored_first} .. {r.stored_last}" if r.stored_first else "empty"
        typer.echo(f"[{total['n']:>3}/{len(tickers)}] {r.ticker:<8} fetched {r.fetched:>6}  stored {r.stored_count:>6}  {span}")

    reports = svc.sync_many(tickers, date.fromisoformat(start), end_d, progress=show)
    typer.echo(f"done: {total['fetched']:,} new bars across {total['n']} tickers")
    empty = [r.ticker for r in reports if r.empty or not r.stored_count]
    if empty:
        typer.echo(f"still empty after retries ({len(empty)}): {' '.join(empty)}")
        typer.echo("re-run the same command later; only these will be fetched")
        raise typer.Exit(2)


@prices_app.command("verify")
def prices_verify(
    tickers: list[str] | None = TICKERS_ARG,
    universe: str | None = typer.Option(None, help="Check every member of a stored universe"),
    min_bars: int = typer.Option(250, help="Flag tickers with fewer bars than this"),
    db: Path = DB_OPTION,
) -> None:
    """Report empty, short, and stale tickers in the cache. Exit code 2 if anything is empty."""
    ps = PriceStore.open(db)
    syms = list(tickers or [])
    if universe:
        syms += [yahoo_symbol(s) for s in UniverseStore(ps.con).symbols(universe)]
    if not syms:
        syms = ps.tickers()
    empty, short, stale = [], [], []
    today = datetime.now(UTC).date()
    for s in syms:
        c = ps.coverage(s)
        if not c.count:
            empty.append(s)
        elif c.count < min_bars:
            short.append((s, c.count, c.first))
        elif c.last and (today - c.last).days > 7:
            stale.append((s, c.last))
    total = sum(ps.coverage(s).count for s in syms)
    unavailable = ps.unavailable()
    known_dead = [s for s in empty if s in unavailable]
    empty = [s for s in empty if s not in unavailable]
    no_adj = [s for s in ps.tickers_without_adjustments() if s in set(syms)]
    typer.echo(f"{len(syms)} tickers, {total:,} bars; {len(empty)} empty, {len(short)} short, "
               f"{len(stale)} stale, {len(known_dead)} delisted-unavailable, "
               f"{len(no_adj)} without adjustments")
    if empty:
        typer.echo(f"empty: {' '.join(empty)}")
    if known_dead:
        typer.echo(f"delisted, no history from provider (needs a paid feed): {' '.join(known_dead)}")
    if no_adj:
        typer.echo(f"without adj_close/dividends — run `prices sync --upgrade`: {len(no_adj)} tickers")
    for s, n, first in short:
        typer.echo(f"short: {s:<8} {n:>5} bars from {first} (likely a recent listing)")
    for s, last in stale:
        typer.echo(f"stale: {s:<8} last bar {last}")
    if empty:
        raise typer.Exit(2)


universe_app = typer.Typer(help="Named ticker universes (S&P 500 constituents, ...).")
app.add_typer(universe_app, name="universe")


@universe_app.command("refresh")
def universe_refresh(name: str = UNIVERSE_ARG, offline: bool = False, db: Path = DB_OPTION) -> None:
    """Fetch the current membership list and persist it with today's as-of date."""
    if name != "sp500":
        typer.echo("only 'sp500' is built in for now"); raise typer.Exit(1)
    members, source = sp500(prefer_live=not offline)
    us = UniverseStore(PriceStore.open(db).con)
    n = us.replace(name, members, source)
    typer.echo(f"{name}: {n} members stored (source: {source})")
    intervals, hsource = sp500_history(prefer_live=not offline)
    k = us.replace_history(name, intervals, hsource)
    lo, hi = us.history_range(name)
    added, removed = us.extend_history(name, [m.symbol for m in members], datetime.now(UTC).date())
    typer.echo(f"{name}: {k} membership intervals {lo} .. {hi} (source: {hsource})")
    if added or removed:
        typer.echo(f"  reconciled with today's list: +{len(added)} {' '.join(added)}  "
                   f"-{len(removed)} {' '.join(removed)}  (dated today, tagged 'observed')")


@universe_app.command("members-on")
def universe_members_on(as_of: str, name: str = "sp500", db: Path = DB_OPTION) -> None:
    """Point-in-time membership: who was in the universe on a given date."""
    us = UniverseStore(PriceStore.open(db).con)
    syms = us.members_on(name, date.fromisoformat(as_of))
    if not syms:
        lo, hi = us.history_range(name)
        typer.echo(f"no history for '{name}' on {as_of} (have {lo} .. {hi}); run `universe refresh`")
        raise typer.Exit(1)
    typer.echo(" ".join(syms))
    typer.echo(f"{len(syms)} members on {as_of}")


@universe_app.command("changes")
def universe_changes(start: str, end: str | None = None, name: str = "sp500",
                     db: Path = DB_OPTION) -> None:
    """Additions and removals between two dates."""
    us = UniverseStore(PriceStore.open(db).con)
    end_d = date.fromisoformat(end) if end else datetime.now(UTC).date()
    for d, kind, sym in us.changes(name, date.fromisoformat(start), end_d):
        typer.echo(f"{d}  {'+' if kind == 'add' else '-'} {sym}")


@universe_app.command("show")
def universe_show(name: str = UNIVERSE_ARG, db: Path = DB_OPTION) -> None:
    """List a universe's symbols and their price-cache coverage."""
    ps = PriceStore.open(db)
    us = UniverseStore(ps.con)
    syms = us.symbols(name)
    if not syms:
        typer.echo(f"universe '{name}' is empty"); raise typer.Exit(1)
    covered = 0
    for s in syms:
        c = ps.coverage(yahoo_symbol(s))
        covered += bool(c.count)
        typer.echo(f"{s:<8} {c.count:>6} bars  {c.first or '-'} .. {c.last or '-'}")
    typer.echo(f"{name}: {len(syms)} members as of {us.as_of(name)}, {covered} with cached prices")


@prices_app.command("show")
def prices_show(ticker: str, last: int = 10, db: Path = DB_OPTION) -> None:
    """Print coverage and the most recent bars for one ticker."""
    store = PriceStore.open(db)
    cov = store.coverage(ticker)
    if not cov.count:
        typer.echo(f"{ticker.upper()}: no bars stored"); raise typer.Exit(1)
    typer.echo(f"{cov.ticker}: {cov.count} bars, {cov.first} .. {cov.last}")
    for b in store.bars(ticker, cov.last - timedelta(days=last * 2), cov.last)[-last:]:
        adj = f"  Adj {b.adj_close:>10.2f}" if b.adj_close is not None else "  Adj        n/a"
        typer.echo(f"  {b.ts}  O {b.open:>10.2f}  H {b.high:>10.2f}  L {b.low:>10.2f}  "
                   f"C {b.close:>10.2f}{adj}  V {b.volume:>12,}  {b.source}")
    divs = store.dividends(ticker, cov.last - timedelta(days=365), cov.last)
    if divs:
        typer.echo("  dividends (last 12m): " + ", ".join(f"{d.ex_date} ${d.amount}" for d in divs))
    sp = store.splits(ticker)
    if sp:
        typer.echo("  splits: " + ", ".join(f"{x.date} {x.ratio}:1" for x in sp))


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
