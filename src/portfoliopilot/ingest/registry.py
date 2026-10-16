"""Parser registry: built-in broker parsers plus any registered under the
`portfoliopilot.ingest` entry point. `detect()` sniffs a file; `parse()` runs it."""

from __future__ import annotations

import logging
from datetime import date
from importlib.metadata import entry_points
from pathlib import Path

from portfoliopilot.ingest.base import ImportResult, Parser, read_text
from portfoliopilot.ingest.parsers.etrade import EtradePortfolio
from portfoliopilot.ingest.parsers.fidelity import FidelityHistory, FidelityPositions
from portfoliopilot.ingest.parsers.ofx import OfxInvestment
from portfoliopilot.ingest.parsers.robinhood import RobinhoodActivity
from portfoliopilot.ingest.parsers.schwab import SchwabPositions
from portfoliopilot.ingest.parsers.vanguard import VanguardDownload

log = logging.getLogger(__name__)

BUILTIN: list[type] = [OfxInvestment, RobinhoodActivity, FidelityHistory, FidelityPositions,
                       SchwabPositions, VanguardDownload, EtradePortfolio]


def available() -> dict[str, type]:
    found = {p.name: p for p in BUILTIN}
    for ep in entry_points(group="portfoliopilot.ingest"):
        try:
            found[ep.name] = ep.load()
        except ImportError as exc:
            log.debug("ingest parser %s unavailable: %s", ep.name, exc)
    return found


def detect(text: str) -> list[str]:
    """Names of parsers whose sniff() accepts the text, most specific first."""
    return [name for name, cls in available().items() if cls.sniff(text)]


def parse(path: str | Path, account_id: str, parser: str | None = None,
          as_of: date | None = None) -> ImportResult:
    text = read_text(path)
    name = parser or (detect(text) or [None])[0]
    if name is None:
        raise ValueError(f"{path}: no parser recognizes this file; try --parser generic_csv or map columns")
    cls = available().get(name)
    if cls is None:
        raise KeyError(f"unknown parser '{name}'; available: {sorted(available())}")
    p: Parser = cls()
    return p.parse(text, account_id, as_of)
