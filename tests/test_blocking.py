from datetime import date

from invoice_dedupe.blocking import MAX_BLOCK_SIZE, candidate_pairs
from invoice_dedupe.models import Invoice
from invoice_dedupe.normalize import normalize_invoice


def _norm(invoices):
    return [normalize_invoice(i) for i in invoices]


def test_candidates_contain_true_duplicate_and_skip_unrelated():
    invoices = [
        Invoice(id="A", vendor_name="Northwind Logistics", invoice_no="INV-2024/0001",
                amount=5_000, invoice_date=date(2024, 3, 10), tax_id="12-3456789-401"),
        Invoice(id="B", vendor_name="northwind logistics", invoice_no="inv 2024 0001",
                amount=5_000, invoice_date=date(2024, 3, 10), tax_id="12-3456789-401"),
        Invoice(id="C", vendor_name="Acme Trading", invoice_no="ZZ-9",
                amount=9_000, invoice_date=date(2024, 3, 11), tax_id="98-7654321-004"),
    ]
    pairs, stats = candidate_pairs(_norm(invoices))
    assert (0, 1) in pairs
    assert all(2 not in pair for pair in pairs)
    assert stats.naive_candidates == 3
    assert stats.n_candidates == 1
    assert stats.reduction > 0


def test_reinvoice_variant_caught_via_amount_block():
    invoices = [
        Invoice(id="A", vendor_name="Northwind Logistics", invoice_no="INV-2024/0001",
                amount=5_000, invoice_date=date(2024, 3, 10), tax_id="12-3456789-401"),
        Invoice(id="B", vendor_name="Northwind Logistics", invoice_no="BIL-2024-9999",
                amount=5_000, invoice_date=date(2024, 3, 20), tax_id="12-3456789-401"),
    ]
    pairs, stats = candidate_pairs(_norm(invoices))
    assert (0, 1) in pairs


def test_degenerate_block_is_skipped():
    invoices = [
        Invoice(id=f"X{i}", vendor_name=f"Vendor {i}", invoice_no=f"N-{i}",
                amount=1_000, invoice_date=date(2024, 1, 1), tax_id=f"T{i}")
        for i in range(MAX_BLOCK_SIZE + 1)
    ]
    pairs, stats = candidate_pairs(_norm(invoices))
    assert stats.n_skipped_blocks == 1
    assert stats.n_candidates == 0
