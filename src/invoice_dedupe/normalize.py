"""Canonical normalization of invoice fields before matching."""
from __future__ import annotations

import calendar
import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .models import Invoice, NormalizedInvoice

_NON_ALNUM = re.compile(r"[^0-9A-Za-z]+")
_NON_DIGIT = re.compile(r"\D+")
_CURRENCY_TOKEN = re.compile(r"(?i)\b(?:rp|idr|usd|eur|gbp)\b\.?")
_AMOUNT_CLEAN = re.compile(r"[^\d.,\-]")
_WS = re.compile(r"\s+")

# Look-alike characters (typos / OCR): mapped only when adjacent to a digit,
# so alphabetic prefixes such as "INV" are never corrupted.
_CONFUSABLE_MAP = {"O": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2", "G": "6"}

_PREFIX_LEGAL = {"PT", "CV", "UD", "FA"}
_SUFFIX_LEGAL = {
    "TBK", "PERSERO", "PKP", "LTD", "LIMITED", "INC", "INCORPORATED",
    "LLC", "PLLC", "PTE", "PTY", "PLC", "GMBH", "AG", "SA", "SAS", "SARL",
    "SRL", "SPA", "BV", "NV", "AB", "AS", "APS", "OY", "SDN", "BHD",
    "KK", "KFT", "CORP", "CORPORATION", "CO",
}


def normalize_invoice_no(raw: str | None) -> str:
    """Canonicalize an invoice number: strip separators, uppercase, and map
    look-alike characters when they are adjacent to a digit.

    >>> normalize_invoice_no("inv 2024-0012")
    'INV20240012'
    >>> normalize_invoice_no("INV-O78L")
    'INV0781'
    >>> normalize_invoice_no("INV-2024/0012")
    'INV20240012'
    """
    if not raw:
        return ""
    s = _NON_ALNUM.sub("", str(raw)).upper()
    out = []
    for i, ch in enumerate(s):
        mapped = _CONFUSABLE_MAP.get(ch)
        if mapped is not None and (
            (i > 0 and s[i - 1].isdigit()) or (i + 1 < len(s) and s[i + 1].isdigit())
        ):
            out.append(mapped)
        else:
            out.append(ch)
    return "".join(out)


def normalize_vendor_name(raw: str | None) -> tuple[str, str]:
    """Normalize a vendor name; return (norm, key) where key is the sorted token form.

    Legal entity forms (Ltd, GmbH, PT, Tbk, ...) are dropped from prefix/suffix
    positions, supporting companies from multiple jurisdictions.
    >>> normalize_vendor_name("Northwind Logistics, Ltd.")
    ('NORTHWIND LOGISTICS', 'LOGISTICS NORTHWIND')
    >>> normalize_vendor_name("PT. Maju Jaya Abadi, Tbk")
    ('MAJU JAYA ABADI', 'ABADI JAYA MAJU')
    """
    if not raw:
        return "", ""
    s = _WS.sub(" ", str(raw).upper())
    s = re.sub(r"[.,/()\-]+", " ", s)
    tokens = [t for t in s.split() if t]
    body = [t for t in tokens if t not in _PREFIX_LEGAL]
    while body and body[-1] in _SUFFIX_LEGAL:
        body.pop()
    while body and body[0] in _PREFIX_LEGAL:
        body.pop(0)
    return " ".join(body), " ".join(sorted(body))


def normalize_tax_id(raw: str | None) -> str:
    """Reduce a tax/VAT registration number to digits only."""
    if not raw:
        return ""
    return _NON_DIGIT.sub("", str(raw))


def parse_amount(raw: object) -> int | None:
    """Parse an amount written in common formats into a whole number.

    Handles both international (1,234,567.89) and Indonesian/European
    (1.234.567,89) conventions, with optional currency symbols.
    >>> parse_amount("USD 1,250,000.00")
    1250000
    >>> parse_amount("1.250.000,00")
    1250000
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, Decimal):
        d = raw
    elif isinstance(raw, float):
        d = Decimal(str(raw))
    else:
        s = _CURRENCY_TOKEN.sub(" ", str(raw))
        s = _AMOUNT_CLEAN.sub("", s).strip("., ")
        if not s or not re.search(r"\d", s):
            return None
        has_comma = "," in s
        has_dot = "." in s
        if has_comma and has_dot:
            dec_sep = "," if s.rfind(",") > s.rfind(".") else "."
            thou_sep = "." if dec_sep == "," else ","
            s = s.replace(thou_sep, "").replace(dec_sep, ".")
        elif has_comma:
            parts = s.split(",")
            if len(parts) > 1 and all(len(p) == 3 for p in parts[1:]):
                s = "".join(parts)
            else:
                s = s.replace(",", ".")
        elif has_dot:
            parts = s.split(".")
            if len(parts) > 1 and all(len(p) == 3 for p in parts[1:]):
                s = "".join(parts)
        try:
            d = Decimal(s)
        except InvalidOperation:
            return None
    return int(d.to_integral_value(rounding=ROUND_HALF_UP))


def billing_period_of(d: date | None) -> str:
    return f"{d.year:04d}-{d.month:02d}" if d else ""


def normalize_invoice(inv: Invoice) -> NormalizedInvoice:
    vendor, vendor_key = normalize_vendor_name(inv.vendor_name)
    return NormalizedInvoice(
        id=inv.id,
        invoice=inv,
        vendor=vendor,
        vendor_key=vendor_key,
        invoice_no=normalize_invoice_no(inv.invoice_no),
        amount=inv.amount,
        date=inv.invoice_date,
        tax_id=normalize_tax_id(inv.tax_id),
        billing_period=billing_period_of(inv.invoice_date),
    )


def safe_date(year: int, month: int, day: int) -> date:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, max(1, min(day, last)))
