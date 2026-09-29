"""ocr: fixture photos round-trip through the recognizer; junk degrades honestly."""
import pytest

from invoice_dedupe import ocr, photogen

INVOICE_LINES = [
    "Vendor: Northwind Logistics, Ltd.",
    "Invoice No: INV-2026-0001",
    "Invoice Date: 2026-09-14",
    "Amount Due: USD 12,500.00",
    "Tax ID: 12-3456789",
]
WANT = [line.upper() for line in INVOICE_LINES]


@pytest.mark.parametrize("seed", [3, 4, 5, 30, 42])
def test_round_trip_exact(seed):
    png = photogen.render_text(INVOICE_LINES, seed=seed)
    assert ocr.extract_text(png).splitlines() == WANT


def test_no_text_yields_empty_string():
    png = photogen.render_text(["   "], seed=5)
    assert ocr.extract_text(png) == ""


def test_non_png_rejected():
    with pytest.raises(ocr.UnreadableImage):
        ocr.extract_text(b"definitely not a png")


def test_digits_survive_even_when_punctuation_wobbles():
    # canonical normalization must recover the invoice number for every seed
    from invoice_dedupe.normalize import normalize_invoice_no
    from invoice_dedupe.extraction import parse_fields

    for seed in range(1, 9):
        png = photogen.render_text(["Invoice No: INV-2026-0001"], seed=seed)
        text = ocr.extract_text(png)
        fields = parse_fields(text)
        assert normalize_invoice_no(fields["invoice_no"]) == "INV20260001", (seed, text)
