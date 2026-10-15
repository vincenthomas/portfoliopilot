# Roadmap

| Phase | Scope | Gate |
| --- | --- | --- |
| 1. Foundation | Core models, DuckDB ledger, generic CSV ingest, yfinance adapter, tax engine, wash-sale + restricted guards, GlidePath + TLH, CLI | Sample household reconciles; golden tax/wash-sale tests pass |
| 2. Tax + compliance depth | Full state bracket tables, §1259 constructive-sale check, ESPP/RSU basis rules, scheduled-plan queue | Hand-checked golden cases for every rule |
| 3. Strategies + backtest | SellAtVest, CorrelatedSatelliteTrim, CharitableGiftSelector, RetirementRebalance, event-driven backtester, paper trading | Replay reproduces sample-household fills |
| 4. Advice + execution + UI | Ranker, weekly action list, multi-year schedule, Alpaca adapter, approval queue, React dashboard, MCP server | First live weekly plan approved end-to-end |
