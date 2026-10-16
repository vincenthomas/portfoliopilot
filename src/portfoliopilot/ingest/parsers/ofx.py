"""OFX / QFX (Open Financial Exchange) investment statements, as exported by most custodians
("Download to Quicken"). Handles both SGML-style (OFX 1.x, unclosed tags) and XML (2.x).

Reads <SECLIST> to map CUSIP -> ticker, <INVPOSLIST> for positions (-> approximate lots, since
OFX positions carry no acquisition dates), and <INVTRANLIST> BUYSTOCK/SELLSTOCK/REINVEST/
BUYMF/SELLMF/TRANSFER (-> transactions).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

from portfoliopilot.core.models import BasisSource, Lot, Transaction, TxType
from portfoliopilot.ingest.base import ImportResult, today


def _tag(block: str, name: str) -> str | None:
    m = re.search(rf"<{name}>([^<\r\n]*)", block, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _blocks(text: str, name: str) -> list[str]:
    """Return the inner text of each <NAME>...</NAME> block (greedy per block)."""
    return [m.group(1) for m in re.finditer(rf"<{name}>(.*?)</{name}>", text, re.IGNORECASE | re.DOTALL)]


def _date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:8], "%Y%m%d").date()  # noqa: DTZ007 — date only
    except ValueError:
        return None


def _dec(s: str | None) -> Decimal | None:
    try:
        return Decimal(s) if s not in (None, "") else None
    except Exception:  # noqa: BLE001 — malformed numeric field
        return None


class OfxInvestment:
    name = "ofx"
    custodian = "OFX/QFX"

    @staticmethod
    def sniff(text: str) -> bool:
        head = text[:2000].upper()
        return "OFXHEADER" in head or "<OFX>" in head or "<INVSTMTRS>" in text.upper()

    def parse(self, text: str, account_id: str, as_of: date | None = None) -> ImportResult:
        res = ImportResult(parser=self.name, account_id=account_id)
        tickers: dict[str, str] = {}
        for sec in _blocks(text, "SECINFO"):
            cusip, tk = _tag(sec, "UNIQUEID"), _tag(sec, "TICKER")
            if cusip and tk:
                tickers[cusip] = tk.upper()

        def symbol(block: str) -> str | None:
            cusip = _tag(block, "UNIQUEID")
            return tickers.get(cusip or "", None)

        stmt_date = _date(_tag(text, "DTASOF")) or as_of or today()
        for pos in _blocks(text, "POSSTOCK") + _blocks(text, "POSMF"):
            sym, units, price = symbol(pos), _dec(_tag(pos, "UNITS")), _dec(_tag(pos, "UNITPRICE"))
            if not sym or not units or units <= 0:
                res.skipped += 1
                continue
            res.approximate = True
            res.lots.append(Lot(id=f"{account_id}-{sym}-ofx", account_id=account_id, ticker=sym, qty=units,
                                acquired_date=stmt_date, basis_per_share=price or Decimal(0),
                                basis_source=BasisSource.TRADE))
        if res.lots:
            res.warnings.append("OFX positions carry no cost basis or dates: lots use the statement "
                                "price as basis and the statement date; replace with broker lot data")

        kinds = {"BUYSTOCK": TxType.BUY, "BUYMF": TxType.BUY, "SELLSTOCK": TxType.SELL,
                 "SELLMF": TxType.SELL, "REINVEST": TxType.DRIP}
        for kind, t in kinds.items():
            for i, tr in enumerate(_blocks(text, kind), 1):
                sym, units, price = symbol(tr), _dec(_tag(tr, "UNITS")), _dec(_tag(tr, "UNITPRICE"))
                dt = _date(_tag(tr, "DTTRADE"))
                if not sym or not units or not dt:
                    res.skipped += 1
                    continue
                fees = (_dec(_tag(tr, "COMMISSION")) or Decimal(0)) + (_dec(_tag(tr, "FEES")) or Decimal(0))
                res.transactions.append(Transaction(
                    id=f"{account_id}-{_tag(tr, 'FITID') or f'{kind}-{i}'}", account_id=account_id,
                    ticker=sym, type=t, date=dt, qty=abs(units), price=price or Decimal(0), fees=abs(fees),
                    note=kind))
        res.transactions.sort(key=lambda x: (x.date, x.id))
        return res
