"""Adapter discovery: built-ins plus anything registered under the
`portfoliopilot.market_data` entry point."""

from __future__ import annotations

import logging
from importlib.metadata import entry_points

from portfoliopilot.market_data.base import MarketDataAdapter

log = logging.getLogger(__name__)


def available() -> dict[str, type]:
    found: dict[str, type] = {}
    for ep in entry_points(group="portfoliopilot.market_data"):
        try:
            found[ep.name] = ep.load()
        except ImportError as exc:  # optional dependency not installed
            log.debug("adapter %s unavailable: %s", ep.name, exc)
    return found


def load(name: str = "yfinance", **kwargs) -> MarketDataAdapter:
    adapters = available()
    if name not in adapters:
        raise KeyError(f"Unknown market-data adapter '{name}'. Available: {sorted(adapters)}")
    return adapters[name](**kwargs)
