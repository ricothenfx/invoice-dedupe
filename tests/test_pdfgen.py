"""Tests for the dependency-free PDF writer."""
import io

import pdfplumber

from invoice_dedupe.pdfgen import make_pdf

LINES = [
    "Vendor: Northwind Logistics, Ltd.",
    "Invoice No: INV-2026-0001",
    "Invoice Date: 2026-09-14",
    "Amount Due: USD 12,500.00",
]


def test_make_pdf_roundtrip_text_layer():
    data = make_pdf(LINES)
    assert data.startswith(b"%PDF-")
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        assert len(pdf.pages) == 1
        text = pdf.pages[0].extract_text()
    assert text.splitlines() == LINES


def test_make_pdf_escapes_special_characters():
    lines = ["Note: total (net) 50% \\ adjusted"]
    with pdfplumber.open(io.BytesIO(make_pdf(lines))) as pdf:
        text = pdf.pages[0].extract_text()
    assert "Note: total (net) 50% \\ adjusted" in text
