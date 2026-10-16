# PortfolioPilot

Self-hosted, open-source **decision-support for individual investors**: import your tax lots from any broker, price them in real time, run strategies and backtests against your actual holdings, and get **tax-aware, lot-level recommendations** with hard guardrails (wash-sale, restricted-stock, gain-budget).

It proposes. You approve. It never trades on its own.

## Why

Most portfolio tools stop at tickers. Real after-tax outcomes are decided at the **lot** level: which shares you sell, when, in which account, and what you bought within 30 days in *any* other account. PortfolioPilot models all of that and refuses to emit a ticket that breaks the rules.

## Status

Phase 1 (foundation) — core models, ledger, tax engine, wash-sale guard, first two strategies, CLI. See [docs/ROADMAP.md](docs/ROADMAP.md) and the module-by-module [build plan](docs/BUILD_PLAN.md).

## Quick start

```bash
pip install -e ".[dev]"
portfoliopilot positions examples/sample_household.json --prices-file examples/sample_prices.json
portfoliopilot advise    examples/sample_household.json --prices-file examples/sample_prices.json
portfoliopilot headroom  examples/sample_household.json
pytest
```

Drop `--prices-file` to pull live quotes through the default `yfinance` adapter (no API key).

### Historical prices

Bars are cached in a local DuckDB file and only the missing date ranges are ever fetched:

```bash
portfoliopilot prices sync examples/sample_household.json --start 2015-01-01   # every holding
portfoliopilot prices sync SPY QQQ --start 2010-01-01                           # or explicit tickers
portfoliopilot prices show SPY
portfoliopilot prices list
portfoliopilot prices export data/bars.parquet   # share or back up
portfoliopilot prices import data/bars.parquet
```

Whole-universe history (S&P 500 constituents; membership from the public-domain
[datasets/s-and-p-500-companies](https://github.com/datasets/s-and-p-500-companies) list, snapshot bundled for offline use):

```bash
portfoliopilot universe refresh sp500                      # store today's 500+ members
portfoliopilot prices sync --universe sp500 --start 1990-01-01   # batched; re-run any time to resume
portfoliopilot prices verify --universe sp500              # empty / short / stale report (exit 2 if any empty)
portfoliopilot prices sync ^GSPC --start 1927-01-01        # the index itself, back to 1927-12-30
```

`universe refresh` also loads **point-in-time membership** (1996 → today, from the MIT-licensed
[fja05680/sp500](https://github.com/fja05680/sp500) history), so backtests can avoid survivorship bias:

```bash
portfoliopilot universe members-on 2010-01-01     # who was in the index that day (499 names)
portfoliopilot universe changes 2008-09-01 2008-12-31
```

Bars carry both `close` (split-adjusted: use for lots, basis, tickets) and `adj_close`
(split **and** dividend adjusted: use for returns), plus `dividends` and `splits` tables.
`PriceStore.closes(..., kind="price" | "total_return")` makes the caller choose.

```bash
portfoliopilot prices sync --upgrade                               # one-time after upgrading from v1 files
portfoliopilot prices sync --universe sp500 --historical --start 1996-01-01   # + every former member
portfoliopilot prices verify --universe sp500                      # lists delisted names the provider can't serve
```

Set `PORTFOLIOPILOT_DB` (or pass `--db`) to choose the database file.

### Importing from your broker

```bash
portfoliopilot import parsers                                           # what's recognized
portfoliopilot import file household.json positions.csv brokerage-1 --dry-run
portfoliopilot import file household.json activity.csv brokerage-1     # auto-detects the format
```

Built-in parsers: **Schwab** (positions, with or without lot details), **Fidelity** (positions,
lot download, account history), **E*TRADE** (portfolio download, lots), **Vanguard** (download-center
transactions), **Robinhood** (activity report), and **OFX/QFX** from any custodian. Lot-level files
replace an account's lots; transaction files are replayed through the ledger; position-level files
create average-cost lots and are flagged `APPROXIMATE` because they carry no acquisition dates.
Parsers are plugins (`portfoliopilot.ingest` entry point); see CONTRIBUTING.md to add one.

### Transaction log

Lots are never edited by hand: every buy, sell, vest, ESPP purchase, DRIP, gift, split and transfer
is a transaction, and the ledger applies it (FIFO / LIFO / HIFO / specific-ID). Partial sells keep
the lot id; every closed slice is a `RealizedGain` by tax year.

```bash
portfoliopilot ledger apply household.json trades.json --method hifo   # append and update lots
portfoliopilot ledger apply household.json all.json --replay           # rebuild lots from the log
portfoliopilot ledger realized household.json --year 2026              # ST / LT by year, per slice
```

## Your data stays yours

Everything runs locally: a single DuckDB file, rate tables in YAML, broker credentials in your OS keychain. The only network calls are to the market-data and broker providers you configure.

## Design

- **Lots are the atomic unit.** Every position is a list of lots with date, basis, term, and account.
- **Household view, always.** Positions are netted across accounts before any concentration or wash-sale logic runs.
- **Tax engine is a dependency of every strategy.** Federal + state + NIIT, LT/ST, collectibles rate, bracket headroom — against *your* `TaxProfile`.
- **Guardrails are hard gates.** Wash-sale conflicts block a ticket until a cure is attached; restricted tickers route to a scheduled-plan queue.
- **Same code path for backtest and live.** A backtest result is a faithful forecast of what the advice engine would have said.

Full system design: [docs/DESIGN.md](docs/DESIGN.md).

## Contributing

Tax rate tables live in `src/portfoliopilot/tax/rate_tables/<year>/` and need a maintainer check every January. Broker parsers, market-data adapters, and strategies are plugins — see [CONTRIBUTING.md](CONTRIBUTING.md).

## Disclaimer

PortfolioPilot is software, not a licensed advisor. Verify figures that depend on current-year tax law and review any plan with a qualified professional before acting.

## License

Apache-2.0
