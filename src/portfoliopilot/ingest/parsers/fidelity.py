"""Fidelity exports.

- Positions download ("Portfolio Positions"): Account Number, Account Name, Symbol, Description,
  Quantity, Last Price, ..., Cost Basis Total, Average Cost Basis, Type  -> approximate lots.
- Lot download (from a position's "Purchase History / Lots" page): Symbol, Date Acquired,
  Quantity, Cost Basis per Share (or Cost Basis Total), ...                -> true lots.
- Account history ("Activity & Orders" download): Run Date, Account, Action, Symbol,
  Description, Type, Quantity, Price ($), Commission ($), Fees ($), ..., Amount ($)  -> transactions.
Fidelity appends disclaimer lines after the table; rows end at the first blank line.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from portfoliopilot.core.models import BasisSource, Lot, Transaction, TxType
from portfoliopilot.ingest.base import (
    ImportResult,
    clean_symbol,
    col,
    find_header,
    money,
    parse_date,
    today,
)

CASH_LIKE = {"SPAXX", "FDRXX", "FZFXX", "FCASH", "FDLXX", "PENDING ACTIVITY"}


class FidelityPositions:
    name = "fidelity_positions"
    custodian = "Fidelity"

    @staticmethod
    def sniff(text: str) -> bool:
        head = text[:1500].lower()
        return "account number" in head and "symbol" in head and (
            "cost basis total" in head or "average cost" in head or "date acquired" in head)

    def parse(self, text: str, account_id: str, as_of: date | None = None) -> ImportResult:
        res = ImportResult(parser=self.name, account_id=account_id)
        lot_table = find_header(text, ("symbol", "date acquired", "quantity"))
        if lot_table:
            header, rows = lot_table
            c_sym, c_dt, c_qty = col(header, "symbol"), col(header, "date acquired"), col(header, "quantity")
            c_cps, c_tot = col(header, "cost basis per share", "cost/share"), col(header, "cost basis total")
            counter: dict[str, int] = {}
            for r in rows:
                sym, dt, qty = clean_symbol(r[c_sym]), parse_date(r[c_dt]), money(r[c_qty])
                if not sym or not dt or not qty:
                    res.skipped += 1
                    continue
                cps = money(r[c_cps]) if c_cps is not None else None
                if cps is None and c_tot is not None:
                    tot = money(r[c_tot])
                    cps = tot / qty if tot is not None else None
                if cps is None:
                    res.skipped += 1
                    continue
                counter[sym] = counter.get(sym, 0) + 1
                res.lots.append(Lot(id=f"{account_id}-{sym}-{dt:%Y%m%d}-{counter[sym]}", account_id=account_id,
                                    ticker=sym, qty=qty, acquired_date=dt, basis_per_share=cps,
                                    basis_source=BasisSource.TRADE))
            return res
        found = find_header(text, ("symbol", "quantity", "cost basis"))
        if not found:
            res.warnings.append("no positions table found")
            return res
        header, rows = found
        c_sym, c_qty = col(header, "symbol"), col(header, "quantity")
        c_avg, c_tot = col(header, "average cost"), col(header, "cost basis total")
        for r in rows:
            sym = clean_symbol(r[c_sym]) if c_sym < len(r) else ""
            qty = money(r[c_qty]) if c_qty < len(r) else None
            if not sym or sym in CASH_LIKE or sym.endswith("**") or not qty:
                res.skipped += 1
                continue
            cps = money(r[c_avg]) if c_avg is not None and c_avg < len(r) else None
            if cps is None and c_tot is not None and c_tot < len(r):
                tot = money(r[c_tot])
                cps = (tot / qty) if tot is not None else None
            if cps is None:
                res.skipped += 1
                res.warnings.append(f"{sym}: no cost basis; skipped")
                continue
            res.approximate = True
            res.lots.append(Lot(id=f"{account_id}-{sym}-avg", account_id=account_id, ticker=sym, qty=qty,
                                acquired_date=as_of or today(),
                                basis_per_share=cps.quantize(Decimal("0.0001")), basis_source=BasisSource.TRADE))
        if res.approximate:
            res.warnings.append("position-level export: lots carry average cost and the as-of date")
        return res


class FidelityHistory:
    name = "fidelity_history"
    custodian = "Fidelity"

    @staticmethod
    def sniff(text: str) -> bool:
        head = text[:1500].lower()
        return "run date" in head and "action" in head and "symbol" in head

    def parse(self, text: str, account_id: str, as_of: date | None = None) -> ImportResult:
        res = ImportResult(parser=self.name, account_id=account_id)
        found = find_header(text, ("run date", "action", "symbol", "quantity"))
        if not found:
            res.warnings.append("no history table found")
            return res
        header, rows = found
        c_dt, c_act, c_sym = col(header, "run date"), col(header, "action"), col(header, "symbol")
        c_qty, c_px = col(header, "quantity"), col(header, "price")
        c_comm, c_fee = col(header, "commission"), col(header, "fees")
        for i, r in enumerate(rows, 1):
            if max(c_dt, c_act, c_sym, c_qty) >= len(r):
                continue
            dt, act, sym = parse_date(r[c_dt]), r[c_act].upper(), clean_symbol(r[c_sym])
            qty, px = money(r[c_qty]), money(r[c_px]) if c_px is not None and c_px < len(r) else None
            if not dt or not sym or not qty:
                res.skipped += 1
                continue
            fees = (money(r[c_comm]) or Decimal(0)) + (money(r[c_fee]) or Decimal(0)) if c_comm is not None else Decimal(0)
            if "YOU BOUGHT" in act or act.startswith("BUY"):
                t = TxType.BUY
            elif "YOU SOLD" in act or act.startswith("SELL"):
                t = TxType.SELL
            elif "REINVEST" in act:
                t = TxType.DRIP
            elif "ESPP" in act or "EMPLOYEE STOCK PURCHASE" in act:
                t = TxType.ESPP
            elif "VEST" in act or "RESTRICTED STOCK" in act or "RELEASE" in act:
                t = TxType.VEST
            else:
                res.skipped += 1  # dividends, interest, transfers of cash, etc.
                continue
            res.transactions.append(Transaction(
                id=f"{account_id}-{dt:%Y%m%d}-{i}", account_id=account_id, ticker=sym, type=t, date=dt,
                qty=abs(qty), price=px or Decimal(0), fees=abs(fees), note=r[c_act].strip()))
        return res
