"""Vanguard "Download center" export: a positions block followed by a transactions block
in ONE file.

Positions : Account Number, Investment Name, Symbol, Shares, Share Price, Total Value
            (no cost basis -> approximate lots are NOT built; basis must come from transactions
            or the cost-basis page). Positions are reported as warnings only.
Transactions: Account Number, Trade Date, Settlement Date, Transaction Type, Transaction
            Description, Investment Name, Symbol, Shares, Share Price, Principal Amount,
            Commission Fees, Net Amount, Accrued Interest, Account Type   -> transactions.
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

TYPE_MAP = {
    "buy": TxType.BUY, "sell": TxType.SELL, "reinvestment": TxType.DRIP,
    "dividend reinvestment": TxType.DRIP, "conversion": None, "sweep": None, "dividend": None,
}


class VanguardDownload:
    name = "vanguard"
    custodian = "Vanguard"

    @staticmethod
    def sniff(text: str) -> bool:
        head = text[:3000].lower()
        return "investment name" in head and ("trade date" in head or "share price" in head)

    def parse(self, text: str, account_id: str, as_of: date | None = None) -> ImportResult:
        res = ImportResult(parser=self.name, account_id=account_id)
        tx_table = find_header(text, ("trade date", "transaction type", "shares"))
        if tx_table:
            header, rows = tx_table
            c_dt, c_type, c_sym = col(header, "trade date"), col(header, "transaction type"), col(header, "symbol")
            c_sh, c_px, c_fee = col(header, "shares"), col(header, "share price"), col(header, "commission")
            for i, r in enumerate(rows, 1):
                if max(c_dt, c_type, c_sym, c_sh) >= len(r):
                    continue
                kind = r[c_type].strip().lower()
                t = TYPE_MAP.get(kind, None)
                if t is None:
                    if kind not in TYPE_MAP:
                        res.warnings.append(f"unknown transaction type '{r[c_type].strip()}' skipped")
                    res.skipped += 1
                    continue
                dt, sym, sh = parse_date(r[c_dt]), clean_symbol(r[c_sym]), money(r[c_sh])
                if not dt or not sym or not sh:
                    res.skipped += 1
                    continue
                res.transactions.append(Transaction(
                    id=f"{account_id}-{dt:%Y%m%d}-{i}", account_id=account_id, ticker=sym, type=t, date=dt,
                    qty=abs(sh), price=money(r[c_px]) or Decimal(0),
                    fees=abs(money(r[c_fee]) or Decimal(0)) if c_fee is not None and c_fee < len(r) else Decimal(0),
                    note=r[c_type].strip()))
        pos_table = find_header(text, ("investment name", "symbol", "shares", "total value"))
        if pos_table and not tx_table:
            res.warnings.append("Vanguard positions carry no cost basis; import the transaction "
                                "history (same download with 'Transactions' selected) to build lots")
        return res
