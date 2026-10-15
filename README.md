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
