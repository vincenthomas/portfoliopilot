# Contributing

## Ground rules
- Every tax or compliance rule change ships with a hand-computed golden test in `tests/`.
- Rate tables (`src/portfoliopilot/tax/rate_tables/<year>/`) cite the IRS / state source in a comment and are reviewed each January.
- No personal data in the repo. `examples/` is synthetic.

## Plugins
- **Market-data adapter:** implement `MarketDataAdapter` in `market_data/base.py`; register under the `portfoliopilot.market_data` entry point.
- **Broker parser:** add `ingest/parsers/<custodian>.py` with `name`, `custodian`, `sniff(text)` and `parse(text, account_id, as_of) -> ImportResult`; register it in `ingest/registry.py` (or the `portfoliopilot.ingest` entry point) and add a fixture under `tests/fixtures/brokers/` with a hand-built expected result. Fixtures must be synthetic — never a real statement.
- **Strategy:** implement `propose(state) -> list[Trade]`; register under `portfoliopilot.strategies`.

## Dev
```bash
pip install -e ".[dev]"
ruff check . && pytest
```
