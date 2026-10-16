"""E*TRADE exports.

- Portfolio download ("Download" on Portfolios): Symbol, Last Price $, Change $, Change %, Qty #,
  Price Paid $, Day's Gain $, Total Gain $, Total Gain %, Value $  -> approximate lots (avg "Price Paid").
- Gains & Losses / lot download: Symbol, Acquired Date (or "Date Acquired"), Qty, Price Paid $, ...
  -> true lots when an acquired-date column is present.
"""

from __future__ import annotations

from datetime import date

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


class EtradePortfolio:
    name = "etrade_portfolio"
    custodian = "E*TRADE"

    @staticmethod
    def sniff(text: str) -> bool:
        head = text[:1500].lower()
        return "price paid" in head and ("qty" in head or "quantity" in head)

    def parse(self, text: str, account_id: str, as_of: date | None = None) -> ImportResult:
        res = ImportResult(parser=self.name, account_id=account_id)
        found = find_header(text, ("symbol", "price paid"))
        if not found:
            res.warnings.append("no portfolio table found")
            return res
        header, rows = found
        c_sym, c_qty, c_paid = col(header, "symbol"), col(header, "qty", "quantity"), col(header, "price paid")
        c_dt = col(header, "acquired", "date acq", "open date")
        counter: dict[str, int] = {}
        for r in rows:
            if max(c_sym, c_qty, c_paid) >= len(r):
                continue
            sym, qty, paid = clean_symbol(r[c_sym]), money(r[c_qty]), money(r[c_paid])
            if not sym or sym in {"TOTAL", "CASH"} or not qty or paid is None:
                res.skipped += 1
                continue
            dt = parse_date(r[c_dt]) if c_dt is not None and c_dt < len(r) else None
            if dt:
                counter[sym] = counter.get(sym, 0) + 1
                lot_id = f"{account_id}-{sym}-{dt:%Y%m%d}-{counter[sym]}"
            else:
                res.approximate = True
                dt, lot_id = as_of or today(), f"{account_id}-{sym}-avg"
            res.lots.append(Lot(id=lot_id, account_id=account_id, ticker=sym, qty=qty, acquired_date=dt,
                                basis_per_share=paid, basis_source=BasisSource.TRADE))
        if res.approximate:
            res.warnings.append("position-level export: lots carry average Price Paid and the as-of date")
        return res
