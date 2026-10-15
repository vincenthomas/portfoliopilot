"""Generic CSV lot importer. Works with any broker export once columns are mapped.

Expected columns (case-insensitive; rename via `column_map`):
    account_id, ticker, qty, acquired_date (YYYY-MM-DD), basis_per_share, [basis_source]
"""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

from portfoliopilot.core.models import BasisSource, Lot

DEFAULT_MAP = {
    "account_id": "account_id", "ticker": "ticker", "qty": "qty",
    "acquired_date": "acquired_date", "basis_per_share": "basis_per_share",
    "basis_source": "basis_source",
}


def import_lots(path: str | Path, column_map: dict[str, str] | None = None) -> list[Lot]:
    cmap = {**DEFAULT_MAP, **(column_map or {})}
    lots: list[Lot] = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, start=1):
            r = {k.strip().lower(): v for k, v in row.items()}
            def get(key: str, _r: dict = r) -> str:
                return _r.get(cmap[key].lower(), "")
            lots.append(Lot(
                id=f"{get('account_id')}-{get('ticker')}-{i}",
                account_id=get("account_id"),
                ticker=get("ticker").upper(),
                qty=Decimal(get("qty")),
                acquired_date=date.fromisoformat(get("acquired_date")),
                basis_per_share=Decimal(get("basis_per_share")),
                basis_source=BasisSource(get("basis_source") or "trade"),
            ))
    return lots
