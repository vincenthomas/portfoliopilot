"""Shared types and helpers for broker import parsers.

A parser turns one export file into an `ImportResult`. Three shapes of export exist:

- lot-level      : one row per tax lot (date, qty, cost/share)  -> `lots`
- position-level : one row per holding with average cost         -> `lots` with `approximate=True`
                   (no acquisition dates: holding period is unknown until the user supplies lots)
- transaction    : one row per trade / vest / dividend            -> `transactions`, replayed by the ledger
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from portfoliopilot.core.models import Lot, Transaction


@dataclass
class ImportResult:
    parser: str
    account_id: str
    lots: list[Lot] = field(default_factory=list)
    transactions: list[Transaction] = field(default_factory=list)
    approximate: bool = False  # True when lots were built from position averages (no dates)
    warnings: list[str] = field(default_factory=list)
    skipped: int = 0


class Parser(Protocol):
    name: str
    custodian: str

    @staticmethod
    def sniff(text: str) -> bool: ...

    def parse(self, text: str, account_id: str, as_of: date | None = None) -> ImportResult: ...


# ---- helpers --------------------------------------------------------------

_MONEY = re.compile(r"[^0-9.\-]")


def money(s: str | None) -> Decimal | None:
    """'$1,234.56' / '(12.30)' / '--' -> Decimal or None."""
    if s is None:
        return None
    t = s.strip()
    if not t or t in {"--", "n/a", "N/A", "-"}:
        return None
    neg = t.startswith("(") and t.endswith(")")
    t = _MONEY.sub("", t)
    if t in {"", "-", "."}:
        return None
    try:
        v = Decimal(t)
    except InvalidOperation:
        return None
    return -v if neg else v


DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%d-%b-%Y", "%b %d, %Y", "%Y%m%d", "%m-%d-%Y")


def today() -> date:
    return datetime.now(UTC).date()


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    t = s.strip().split(" as of ")[0].strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(t, fmt).date()  # noqa: DTZ007 — date only, no tz
        except ValueError:
            continue
    return None


def clean_symbol(s: str | None) -> str:
    """'BRK/B' -> 'BRK.B'; strip whitespace and quotes."""
    return (s or "").strip().strip('"').upper().replace("/", ".")


def find_header(text: str, required: tuple[str, ...]) -> tuple[list[str], list[list[str]]] | None:
    """Locate the first CSV row containing every `required` column (case-insensitive,
    substring match), skipping preamble lines brokers put above the table. Returns
    (header, rows) with rows read until the first blank line."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        cells = next(csv.reader([line]))
        low = [c.strip().lower() for c in cells]
        if all(any(r in c for c in low) for r in required):
            header = [c.strip() for c in cells]
            body: list[list[str]] = []
            for row in csv.reader(io.StringIO("\n".join(lines[i + 1:]))):
                if not any(c.strip() for c in row):
                    break
                body.append(row)
            return header, body
    return None


def col(header: list[str], *names: str) -> int | None:
    """Index of the first header cell matching any of `names` (case-insensitive substring)."""
    low = [h.lower() for h in header]
    for n in names:
        for i, h in enumerate(low):
            if n.lower() in h:
                return i
    return None


def read_text(path: str | Path) -> str:
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")
