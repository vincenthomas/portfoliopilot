# PortfolioPilot — System Design (summary)

PortfolioPilot is an open-source, self-hosted decision-support and execution-prep system for individual investors. It ingests tax lots from any broker, prices them in real time, runs strategies and backtests against the user's actual holdings, and produces tax-aware, lot-level recommendations with guardrails. It does not auto-trade; a human approves every order ticket.

## Principles (enforced in code)

1. **Lots are the atomic unit.** Every position is a list of lots with acquisition date, quantity, basis, term, account.
2. **Household view, always.** Positions are netted across accounts before concentration, wash-sale, or rebalance logic runs.
3. **Tax engine is a dependency of every strategy.** Federal + state + NIIT, LT/ST, collectibles rate, bracket headroom, against the user's `TaxProfile`.
4. **Wash-sale check is a hard gate.** ±30 days, all accounts, including RSU vests, ESPP buys, DRIP. Conflicts block the ticket until a cure is attached.
5. **Restricted-security gating.** User-flagged tickers route to a scheduled-plan queue (e.g. 10b5-1), never direct execution.
6. **Human-in-the-loop execution.** Propose → preview → approve → reconcile fill → update realized-gain tally.
7. **Privacy by default.** Local DuckDB file; credentials in the OS keychain; network only to providers the user configures.

Softer rules for the advice layer: don't let the tax tail wag the risk dog (reduce *excess* concentration, keep the user's chosen core), and glide rather than dump (every sell carries a paired buy into the diversified core).

## Architecture (bottom to top)

```
UI / reports / approval queue
        ▲
Strategy library ──▶ Advice engine ◀── Backtest & paper trade
        ▲                  ▲                   ▲
Tax engine         Compliance guard      Risk analytics
        ▲                  ▲
Market data        Household ledger ◀── Execution ──▶ Broker order adapters
        ▲                  ▲
Market-data adapters   Broker imports (CSV, API)
```

Sources fill the ledger and market-data cache; the tax, compliance, and risk layer prices and gates every candidate; strategies and backtests feed the advice engine; approved tickets go to execution, whose fills reconcile back into the ledger.

## Modules

| Module | Responsibility |
| --- | --- |
| `market_data` | Quotes, historicals, fundamentals via pluggable adapters (`yfinance` default; `polygon`, `alpaca`) |
| `ingest` | Broker CSV/API parsers → normalized `Lot[]`; generic CSV mapper |
| `ledger` | DuckDB source of truth: positions, basis, transactions, realized P&L, netting |
| `tax` | Prices any trade for a `TaxProfile`; YAML rate tables per year/jurisdiction |
| `compliance` | Wash-sale, restricted tickers, §1259 constructive-sale checks — hard gates |
| `risk` | Concentration by name/factor, income correlation, beta, drawdown, scenarios |
| `strategies` | `propose(state) -> Trade[]` plugins: GlidePath, TaxLossHarvest, SellAtVest, CorrelatedSatelliteTrim, CharitableGiftSelector, RetirementRebalance, CoveredCallOverlay, FactorScreen |
| `backtest` | Event-driven replay on real lots; paper trading; same tax/compliance code as live |
| `advice` | Ranks trades by after-tax risk reduction per dollar of tax; weekly list + multi-year schedule |
| `execution` | Preview, approve, submit via broker adapters, reconcile fills |
| `api` / `ui` | FastAPI + MCP surface; React dashboard, lot explorer, approval queue |

## Data model

`Household` → `TaxProfile`, `ConstraintsProfile`, `Account[]`, `Lot[]`, `Transaction[]`, `VestEvent[]`. `Trade` carries lot ids, estimated gain/tax, a `ComplianceResult`, and a `paired_trade_id`. See `src/portfoliopilot/core/models.py`.

## Ranking score

```
score = (Δconcentration · w_c + Δincome_corr · w_p) / (est_tax + λ · market_timing_cost)
```

## Roadmap

See [ROADMAP.md](ROADMAP.md).
