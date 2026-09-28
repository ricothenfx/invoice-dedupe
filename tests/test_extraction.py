"""Tests for PDF text-layer extraction and label-based field parsing."""
from invoice_dedupe.extraction import extract_pdf, parse_date, parse_fields, result_to_invoice
from invoice_dedupe.pdfgen import make_pdf


def test_parse_date_supported_formats():
    assert str(parse_date("2026-09-14")) == "2026-09-14"
    assert str(parse_date("14 Sep 2026")) == "2026-09-14"
    assert str(parse_date("September 14, 2026")) == "2026-09-14"
    assert str(parse_date("14/09/2026")) == "2026-09-14"
    assert parse_date("not a date") is None
    assert parse_date("") is None


def test_parse_fields_labels():
    text = "\n".join(
        [
            "ACME Paper Supplies",
            "Vendor: Northwind Logistics, Ltd.",
            "Invoice No: INV-2026/0012",
            "Invoice Date: 14 Sep 2026",
            "Amount Due: USD 1.250.000,00",
            "Tax ID: 12-3456789-401",
        ]
    )
    fields = parse_fields(text)
    assert fields["vendor_name"] == "Northwind Logistics, Ltd."
    assert fields["invoice_no"] == "INV-2026/0012"
    assert str(fields["invoice_date"]) == "2026-09-14"
    assert fields["amount"] == 1250000
    assert fields["tax_id"] == "12-3456789-401"


def test_parse_fields_missing_fields_are_none():
    fields = parse_fields("some unstructured text\nwithout any labels")
    assert fields["vendor_name"] == ""
    assert fields["invoice_no"] == ""
    assert fields["amount"] is None
    assert fields["invoice_date"] is None


def test_invoice_no_label_is_not_confused_with_date_label():
    fields = parse_fields("Invoice Date: 2026-09-14\nInvoice No: X-1")
    assert fields["invoice_no"] == "X-1"
    assert str(fields["invoice_date"]) == "2026-09-14"


def test_extract_pdf_end_to_end():
    data = make_pdf(
        [
            "Vendor: Northwind Logistics, Ltd.",
            "Invoice No: INV-2026-0001",
            "Invoice Date: 2026-09-14",
            "Amount Due: USD 12,500.00",
            "Tax ID: 12-3456789",
        ]
    )
    result = extract_pdf(data)
    assert result.missing == []
    assert result.confidence == 1.0
    assert result.fields["vendor_name"] == "Northwind Logistics, Ltd."
    assert result.fields["amount"] == 12500
    assert "Invoice No: INV-2026-0001" in result.raw_text

    inv = result_to_invoice(result, "inv_test")
    assert inv.invoice_no == "INV-2026-0001"
    assert inv.amount == 12500
    assert inv.tax_id == "12-3456789"


def test_extract_pdf_partial_confidence():
    data = make_pdf(["Invoice No: INV-1"])
    result = extract_pdf(data)
    assert result.missing == ["vendor_name", "invoice_date", "amount"]
    assert result.confidence == 0.25
