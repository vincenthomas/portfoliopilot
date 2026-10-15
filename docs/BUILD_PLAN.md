# Build Plan — module by module

One module per PR, in dependency order. Every PR ships with its interface, a stub usable by the
backtester, golden tests, and a one-line CLI or API hook so the module is exercisable on its own.
A module is "done" when its exit gate passes in CI, not when the code exists.

Dependency order (each row depends only on rows above it):

```
core → ledger → market_data → ingest → tax → compliance → risk → strategies → backtest → advice → execution → api → ui
```

## Milestone 1 — Foundation  (done in #1)

| # | Module | Status |
| --- | --- | --- |
| 1.1 | `core` models | ✅ |
| 1.2 | `ledger` DuckDB + netting | ✅ (persistence minimal) |
| 1.3 | `market_data` interface + yfinance + static adapter | ✅ |
| 1.4 | `ingest` generic CSV | ✅ |
| 1.5 | `tax` engine v1 | ✅ |
| 1.6 | `compliance` wash-sale + restricted | ✅ |
| 1.7 | `strategies` GlidePath + TLH | ✅ |
| 1.8 | CLI | ✅ |

## Milestone 2 — Ledger and ingest hardening

| # | Module / PR | Scope | Exit gate |
| --- | --- | --- | --- |
| 2.1 | `ledger`: transaction log | Append-only `transactions` table; `apply(tx)` splits/closes lots (FIFO, specific-ID), handles splits and transfers; realized P&L per tax year | Replaying `examples/` transactions reproduces the lot snapshot exactly |
| 2.2 | `ledger`: price cache | `price_bars` upsert from any adapter; Parquet export/import; `latest(ticker)` | 1,000 bars round-trip; cache hit avoids network |
| 2.3 | `ingest`: broker parsers | Parser plugin registry; Schwab, Fidelity, E*TRADE, Vanguard, Robinhood CSV formats (community-extensible); OFX/QFX reader | Each parser has a fixture file and produces identical `Lot[]` to a hand-built expected list |
| 2.4 | `ingest`: equity comp | RSU grant schedules → `VestEvent[]`; ESPP purchase with discount/lookback → correct basis and ordinary-income component; `basis_source` set | ESPP qualifying vs disqualifying disposition basis matches IRS Pub 525 examples |
| 2.5 | `ingest`: retirement plans | Plan-statement import (fund → ticker map, no lots needed); flag as `AccountType.RETIREMENT` | Retirement positions appear in household netting and are ignored by tax engine |

## Milestone 3 — Tax and compliance depth

| # | Module / PR | Scope | Exit gate |
| --- | --- | --- | --- |
| 3.1 | `tax`: full state tables | Bracketed (not flat-top) tables for all 50 states + DC; WA 7% LT-gain excise; per-state collectibles treatment | Every state file validated by a schema test; 5 hand-computed state cases |
| 3.2 | `tax`: AMT and phase-outs | AMT exposure flag for large ISO/gain years; NIIT MAGI from projected income; standard vs itemized deduction toggle | Golden cases for AMT crossover |
| 3.3 | `tax`: loss carryforward and $3k ordinary offset | Net ST/LT netting order, carryforward across years, ordinary-income offset | Netting matches Schedule D worksheet for 4 scenarios |
| 3.4 | `compliance`: §1259 constructive sale | Collar, short-against-box, and deep-ITM covered-call checks; qualified-covered-call table | Flags the three canonical constructive-sale patterns; passes a qualified covered call |
| 3.5 | `compliance`: scheduled-plan queue | `ScheduledPlan` model (10b5-1 style): window dates, cooling-off period, trade schedule; restricted tickets enqueue instead of block | A restricted sell becomes a dated plan entry; direct execution is refused |
| 3.6 | `compliance`: substantially-identical map | User-extensible ETF/mutual-fund equivalence groups in `ConstraintsProfile`; share-class twins | Group lookup unit tests; wash-sale honors user groups |

## Milestone 4 — Risk and strategies

| # | Module / PR | Scope | Exit gate |
| --- | --- | --- | --- |
| 4.1 | `risk`: concentration and factors | Name and sector weights; configurable factor set (market, size, value, momentum, tech/semis, rates, gold); factor exposures via regression on bar history | Exposures reproduce a published factor example within tolerance |
| 4.2 | `risk`: income correlation | Correlation of each holding to `income_correlated_tickers`; household "paycheck beta" | Sample household reports a single number; unit test on synthetic series |
| 4.3 | `risk`: scenarios | Drawdown, sequence-of-returns Monte Carlo, single-name shock (−30/−50%) | Deterministic with seed; CLI `risk` command |
| 4.4 | `strategies`: SellAtVest | Ticket per `VestEvent` at vest price, paired core buy, respects scheduled-plan queue | Sample vest produces a dated ticket in the queue |
| 4.5 | `strategies`: CorrelatedSatelliteTrim | Trims highest-income-correlation satellites before core, within gain budget | Ordering test: satellite before core |
| 4.6 | `strategies`: CharitableGiftSelector | Lowest-basis LT lots to a dollar target; deduction value at ordinary rate; 30%-AGI limit flag | Picks the right lots; flags AGI limit |
| 4.7 | `strategies`: RetirementRebalance | Shift retirement allocation to offset household factor exposure at zero tax | Household beta moves toward target with no taxable trades |
| 4.8 | `strategies`: CoveredCallOverlay | Call tickets against permanent core; delta/tenor params; §1259 and QCC checks via compliance | Proposed overlay passes compliance on sample |
| 4.9 | `strategies`: FactorScreen | Replacement-candidate screen for the core (ETFs first); used by TLH as `replacement_map` source | TLH proposes a non-identical replacement automatically |
| 4.10 | `strategies`: plugin registry | Entry-point discovery; `portfoliopilot strategies list`; per-strategy params from YAML | Third-party strategy in `examples/plugins` loads and runs |

## Milestone 5 — Backtest and paper trading

| # | Module / PR | Scope | Exit gate |
| --- | --- | --- | --- |
| 5.1 | `backtest`: engine | Event-driven daily loop over bar history; starts from a lot snapshot; calls `Strategy.propose` and applies fills through the ledger with real tax/compliance | Replaying `examples/` with recorded fills reproduces the final lot state |
| 5.2 | `backtest`: scoring | After-tax CAGR, max drawdown, tax drag, turnover, concentration path, income-correlated exposure path; run artifacts in DuckDB | Two strategies compared in one table via CLI |
| 5.3 | `backtest`: paper trading | Same loop forward on live quotes; logs proposals; no execution | 24-hour paper run writes a journal |

## Milestone 6 — Advice engine

| # | Module / PR | Scope | Exit gate |
| --- | --- | --- | --- |
| 6.1 | `advice`: ranker | Score = (Δconcentration·w_c + Δincome_corr·w_p) / (est_tax + λ·timing_cost); dedupes across strategies; drops blocked tickets, keeps queued ones | Priority order on sample: vest > TLH > satellite > core |
| 6.2 | `advice`: action plan | "This week" list with explicit assumptions block; multi-year schedule sized to annual gain budget | Sample produces a 4-year schedule that never exceeds the LT budget |
| 6.3 | `advice`: reconciliation | Fill → realized gain on exact lots → wash-sale recheck on fill date → update `TaxYear` tallies → re-rank; delta report vs prior estimate | Reported fill changes the harvest tally and lists the delta |

## Milestone 7 — Execution

| # | Module / PR | Scope | Exit gate |
| --- | --- | --- | --- |
| 7.1 | `execution`: preview and approval | Ticket state machine: proposed → previewed → approved → submitted → filled/cancelled; audit log | State transitions unit-tested; invalid transitions refused |
| 7.2 | `execution`: manual-fill adapter | Enter fills by hand for any custodian; feeds reconciliation | Manual fill round-trips through 6.3 |
| 7.3 | `execution`: Alpaca adapter | Paper-trading sandbox first; submit/cancel/status; keychain creds via `keyring` | Sandbox round-trip in CI with a secret, skipped without |
| 7.4 | `execution`: broker adapter interface | `BrokerAdapter` Protocol + registry so other brokers can plug in | Static adapter passes the same contract tests as Alpaca |

## Milestone 8 — API, MCP, and UI

| # | Module / PR | Scope | Exit gate |
| --- | --- | --- | --- |
| 8.1 | `api`: REST | Households, lots, positions, advise, tickets, backtests, tax summary; OpenAPI | httpx contract tests |
| 8.2 | `api`: MCP server | Same surface as MCP tools so any AI assistant can query and propose | MCP tool list smoke test |
| 8.3 | `api`: scheduler | APScheduler jobs: nightly bars, vest refresh, weekly advice run | Jobs register and run once in test |
| 8.4 | `ui`: dashboard | Household concentration, gain budget used, income-correlated exposure | Renders sample household |
| 8.5 | `ui`: lot explorer | Filter/sort lots across accounts; unrealized gain by term | Shows netted VTI across two accounts |
| 8.6 | `ui`: approval queue | Preview → approve → submit; shows compliance reasons and cures | Blocked ticket displays cure text |
| 8.7 | `ui`: backtest viewer | Equity curve, drawdown, tax drag, side-by-side runs | Two runs compared |
| 8.8 | Packaging | Dockerfile multi-stage with UI build; `pipx` install; release workflow | `docker compose up` serves UI + API from a clean checkout |

## Working agreement

- Branch per item, named `m<milestone>/<item>-<slug>` (e.g. `m2/2.1-transaction-log`); PR title = the table row.
- CI must be green (ruff + pytest on 3.11–3.13) before merge; squash-merge to `main`.
- Rate tables and any rule that cites tax law carry a source comment and a golden test.
- No real household data in the repo, ever. `examples/` stays synthetic.
