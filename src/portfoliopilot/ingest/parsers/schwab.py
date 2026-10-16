"""Charles Schwab exports.

Two layouts are recognized:
- Positions export (lot-level when "Lot Details" rows are included): columns like
  Symbol, Description, Quantity, Price, Cost Basis, ... with per-lot rows beneath each
  symbol carrying "Open Date", "Quantity", "Cost/Share".
- Positions export without lots: one row per symbol with "Cost Basis" (total) -> approximate lots.
Schwab puts 1-3 preamble lines ("Positions for account ... as of ...") above the header.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from portfoliopilot.core.models import BasisSource, Lot
from portfoliopilot.ingest.base import (
    ImportResult,
    clean_symbol,
    col,
    find_header,
    money,
    parse_date,
    today,
)

SKIP_SYMBOLS = {"CASH & CASH INVESTMENTS", "ACCOUNT TOTAL", "CASH", ""}


class SchwabPositions:
    name = "schwab_positions"
    custodian = "Schwab"

    @staticmethod
    def sniff(text: str) -> bool:
        head = text[:2000].lower()
        return ("positions for account" in head or "schwab" in head) and "cost basis" in head

    def parse(self, text: str, account_id: str, as_of: date | None = None) -> ImportResult:
        res = ImportResult(parser=self.name, account_id=account_id)
        found = find_header(text, ("symbol", "quantity", "cost basis"))
        if not found:
            res.warnings.append("no positions table found")
            return res
        header, rows = found
        c_sym, c_qty = col(header, "symbol"), col(header, "quantity")
        c_cps = col(header, "cost/share", "cost per share")
        c_cb, c_open = col(header, "cost basis"), col(header, "open date", "acquired")
        c_type = col(header, "security type")
        current_symbol = ""
        lot_rows: dict[str, list[tuple[date, Decimal, Decimal]]] = {}
        position_rows: dict[str, tuple[Decimal, Decimal | None]] = {}
        for r in rows:
            sym = clean_symbol(r[c_sym]) if c_sym is not None and c_sym < len(r) else ""
            qty = money(r[c_qty]) if c_qty is not None and c_qty < len(r) else None
            opened = parse_date(r[c_open]) if c_open is not None and c_open < len(r) else None
            if sym and sym not in SKIP_SYMBOLS and not sym.startswith("ACCOUNT"):
                if c_type is not None and c_type < len(r) and "cash" in r[c_type].lower():
                    continue
                current_symbol = sym
                if qty:
                    position_rows[sym] = (qty, money(r[c_cb]) if c_cb is not None and c_cb < len(r) else None)
                continue
            # lot detail row (symbol blank, open date present)
            if opened and qty and current_symbol:
                cps = money(r[c_cps]) if c_cps is not None and c_cps < len(r) else None
                if cps is None and c_cb is not None and c_cb < len(r):
                    total = money(r[c_cb])
                    cps = (total / qty) if total is not None and qty else None
                if cps is None:
                    res.skipped += 1
                    continue
                lot_rows.setdefault(current_symbol, []).append((opened, qty, cps))
        for sym, (qty, total) in position_rows.items():
            lots = lot_rows.get(sym)
            if lots:
                for i, (opened, q, cps) in enumerate(lots, 1):
                    res.lots.append(Lot(id=f"{account_id}-{sym}-{opened:%Y%m%d}-{i}", account_id=account_id,
                                        ticker=sym, qty=q, acquired_date=opened, basis_per_share=cps,
                                        basis_source=BasisSource.TRADE))
            else:
                if total is None:
                    res.skipped += 1
                    res.warnings.append(f"{sym}: no cost basis; skipped")
                    continue
                res.approximate = True
                res.lots.append(Lot(id=f"{account_id}-{sym}-avg", account_id=account_id, ticker=sym,
                                    qty=qty, acquired_date=as_of or today(),
                                    basis_per_share=(total / qty).quantize(Decimal("0.0001")),
                                    basis_source=BasisSource.TRADE))
        if res.approximate:
            res.warnings.append("position-level export: lots carry average cost and the as-of date; "
                                "export Lot Details for true holding periods")
        return res
