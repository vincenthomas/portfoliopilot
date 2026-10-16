"""Robinhood account-statement / activity CSV (Reports & statements -> Activity report):
Activity Date, Process Date, Settle Date, Instrument, Description, Trans Code, Quantity, Price, Amount.

Trans codes: Buy, Sell, CDIV (cash dividend), REC (receive/transfer in), SPL (split), ACH, ...
Only share-moving codes become transactions.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from portfoliopilot.core.models import Transaction, TxType
from portfoliopilot.ingest.base import (
    ImportResult,
    clean_symbol,
    col,
    find_header,
    money,
    parse_date,
)

CODES = {"BUY": TxType.BUY, "SELL": TxType.SELL, "DRIP": TxType.DRIP, "REC": TxType.TRANSFER}


class RobinhoodActivity:
    name = "robinhood_activity"
    custodian = "Robinhood"

    @staticmethod
    def sniff(text: str) -> bool:
        head = text[:1000].lower()
        return "activity date" in head and "trans code" in head

    def parse(self, text: str, account_id: str, as_of: date | None = None) -> ImportResult:
        res = ImportResult(parser=self.name, account_id=account_id)
        found = find_header(text, ("activity date", "instrument", "trans code", "quantity"))
        if not found:
            res.warnings.append("no activity table found")
            return res
        header, rows = found
        c_dt, c_sym, c_code = col(header, "activity date"), col(header, "instrument"), col(header, "trans code")
        c_qty, c_px = col(header, "quantity"), col(header, "price")
        for i, r in enumerate(rows, 1):
            if max(c_dt, c_sym, c_code, c_qty) >= len(r):
                continue
            code = r[c_code].strip().upper()
            if code == "REC":
                res.warnings.append(f"row {i}: shares received (REC) need a basis; add the lot manually")
                res.skipped += 1
                continue
            t = CODES.get(code)
            dt, sym, qty = parse_date(r[c_dt]), clean_symbol(r[c_sym]), money(r[c_qty])
            if t is None or not dt or not sym or not qty:
                res.skipped += 1
                continue
            res.transactions.append(Transaction(
                id=f"{account_id}-{dt:%Y%m%d}-{i}", account_id=account_id, ticker=sym, type=t, date=dt,
                qty=abs(qty), price=money(r[c_px]) or Decimal(0) if c_px is not None else Decimal(0),
                note=r[c_code].strip()))
        return res
