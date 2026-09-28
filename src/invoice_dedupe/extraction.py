"""PDF text-layer extraction (pdfplumber) and label-based field parsing.

This is the deterministic, primary extraction path (charter decision D6):
PDFs with a text layer are parsed locally with pdfplumber; the vision-LLM
fallback for scans is Phase 4. The parser is label-based and deliberately
conservative: fields it cannot find are reported as missing and reflected in
the extraction confidence, instead of being guessed.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime

import pdfplumber

from .models import Invoice
from .normalize import parse_amount

_CORE_FIELDS = ("vendor_name", "invoice_no", "invoice_date", "amount")

_VENDOR_RE = re.compile(r"(?i)^\s*(?:vendor|supplier|seller|billed\s*by|from)\s*:\s*(.+?)\s*$")
_INVNO_SEP_RE = re.compile(r"(?i)^\s*(?:invoice|inv)\s*(?:number|no\.?|#)?\s*[:#]\s*(.+?)\s*$")
_INVNO_KW_RE = re.compile(r"(?i)^\s*(?:invoice|inv)\s+(?:number|no\.?)\s+(.+?)\s*$")
_DATE_RE = re.compile(r"(?i)^\s*(?:invoice\s*)?date\s*:\s*(.+?)\s*$")
_AMOUNT_RE = re.compile(r"(?i)^\s*(?:amount(?:\s*due)?|total(?:\s*amount)?|grand\s*total)\s*:\s*(.+?)\s*$")
_TAXID_RE = re.compile(r"(?i)^\s*(?:tax\s*(?:id|no\.?|number)?|vat(?:\s*(?:id|no\.?|number))?)\s*:\s*(.+?)\s*$")

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d %b %Y",
    "%d %B %Y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%d/%m/%Y",
)


@dataclass
class ExtractionResult:
    raw_text: str
    fields: dict = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    confidence: float = 0.0


def parse_date(raw: str | None) -> date | None:
    """Parse a date written in any of the supported invoice conventions."""
    if not raw:
        return None
    s = str(raw).strip().rstrip(".")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_fields(text: str) -> dict:
    """Extract invoice fields from plain text using label conventions.

    Returns a dict with keys ``vendor_name``, ``invoice_no``, ``amount``
    (int or None), ``invoice_date`` (date or None) and ``tax_id``. Values that
    cannot be located are empty/None.
    """
    result: dict = {
        "vendor_name": "",
        "invoice_no": "",
        "amount": None,
        "invoice_date": None,
        "tax_id": "",
    }
    for line in text.splitlines():
        if not result["vendor_name"] and (m := _VENDOR_RE.match(line)):
            result["vendor_name"] = m.group(1).strip()
            continue
        if not result["invoice_no"]:
            m = _INVNO_SEP_RE.match(line) or _INVNO_KW_RE.match(line)
            if m:
                result["invoice_no"] = m.group(1).strip()
                continue
        if result["invoice_date"] is None and (m := _DATE_RE.match(line)):
            result["invoice_date"] = parse_date(m.group(1))
            continue
        if result["amount"] is None and (m := _AMOUNT_RE.match(line)):
            result["amount"] = parse_amount(m.group(1))
            continue
        if not result["tax_id"] and (m := _TAXID_RE.match(line)):
            result["tax_id"] = m.group(1).strip()
    return result


def extract_pdf(data: bytes) -> ExtractionResult:
    """Extract text from a PDF's text layer, then parse invoice fields."""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = [(page.extract_text() or "") for page in pdf.pages]
    raw_text = "\n".join(pages).strip()

    fields = parse_fields(raw_text)
    missing = [name for name in _CORE_FIELDS if not fields.get(name)]
    confidence = round((len(_CORE_FIELDS) - len(missing)) / len(_CORE_FIELDS), 2)
    return ExtractionResult(
        raw_text=raw_text,
        fields=fields,
        missing=missing,
        confidence=confidence,
    )


def result_to_invoice(result: ExtractionResult, invoice_id: str, currency: str = "USD") -> Invoice:
    """Build an Invoice model from an extraction result (no ground truth)."""
    f = result.fields
    return Invoice(
        id=invoice_id,
        vendor_name=f.get("vendor_name", ""),
        invoice_no=f.get("invoice_no", ""),
        amount=f.get("amount") or 0,
        currency=currency,
        invoice_date=f.get("invoice_date"),
        tax_id=f.get("tax_id", ""),
    )
