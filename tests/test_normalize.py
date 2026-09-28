from datetime import date

from invoice_dedupe.models import Invoice
from invoice_dedupe.normalize import (
    billing_period_of,
    normalize_invoice,
    normalize_invoice_no,
    normalize_tax_id,
    normalize_vendor_name,
    parse_amount,
)


def test_normalize_invoice_no_variants():
    assert normalize_invoice_no("INV-2024/0012") == "INV20240012"
    assert normalize_invoice_no("inv 2024-0012") == "INV20240012"
    assert normalize_invoice_no("INV/O78/L") == "INV0781"
    assert normalize_invoice_no("") == ""
    assert normalize_invoice_no(None) == ""


def test_normalize_vendor_strips_legal_forms():
    norm, key = normalize_vendor_name("Northwind Logistics, Ltd.")
    assert norm == "NORTHWIND LOGISTICS"
    assert key == "LOGISTICS NORTHWIND"

    norm2, key2 = normalize_vendor_name("northwind logistics")
    assert norm2 == norm
    assert key2 == key


def test_normalize_vendor_multijurisdiction():
    norm, _ = normalize_vendor_name("PT. Maju Jaya Abadi, Tbk")
    assert norm == "MAJU JAYA ABADI"

    norm2, _ = normalize_vendor_name("CV Berkah Sentosa")
    assert norm2 == "BERKAH SENTOSA"

    norm3, _ = normalize_vendor_name("Acme Trading GmbH")
    assert norm3 == "ACME TRADING"


def test_parse_amount_conventions():
    assert parse_amount("USD 1,250,000.00") == 1_250_000
    assert parse_amount("1.250.000,00") == 1_250_000
    assert parse_amount("12,345") == 12_345
    assert parse_amount("125000") == 125_000
    assert parse_amount(9_000) == 9_000
    assert parse_amount("n/a") is None
    assert parse_amount(None) is None


def test_normalize_tax_id():
    assert normalize_tax_id("12-3456789-401") == "123456789401"
    assert normalize_tax_id("01.234.567.8-091.000") == "012345678091000"
    assert normalize_tax_id("") == ""


def test_normalize_invoice_full():
    inv = Invoice(
        id="A",
        vendor_name="Northwind Logistics, Ltd",
        invoice_no="inv/2024/01",
        amount=5_000,
        invoice_date=date(2024, 3, 14),
        tax_id="12-3456789-401",
    )
    n = normalize_invoice(inv)
    assert n.id == "A"
    assert n.vendor == "NORTHWIND LOGISTICS"
    assert n.vendor_key == "LOGISTICS NORTHWIND"
    assert n.invoice_no == "INV202401"
    assert n.amount == 5_000
    assert n.billing_period == "2024-03"
    assert n.tax_id == "123456789401"


def test_billing_period_empty_without_date():
    assert billing_period_of(None) == ""
