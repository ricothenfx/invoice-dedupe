"""photogen: deterministic PNG fixture photos readable back as pixels."""
import invoice_dedupe.photogen as photogen


def test_png_signature_and_determinism():
    kwargs = dict(
        vendor="Northwind Logistics, Ltd.",
        invoice_no="INV-2026-0001",
        invoice_date="2026-09-14",
        amount="12,500.00",
        tax_id="12-3456789",
    )
    a = photogen.make_photo(seed=7, **kwargs)
    b = photogen.make_photo(seed=7, **kwargs)
    assert a.startswith(b"\x89PNG\r\n\x1a\n")
    assert a.endswith(b"IEND\xaeB`\x82")
    assert a == b  # same seed -> identical fixture
    assert photogen.make_photo(seed=8, **kwargs) != a


def test_rendered_text_has_ink_on_dark_and_bright_pixels():
    png = photogen.render_text(["VENDOR: ABC"], seed=3)
    rows = __import__("invoice_dedupe.ocr", fromlist=["decode_png"]).decode_png(png)
    values = [v for row in rows for v in row]
    assert min(values) < 60  # ink
    assert max(values) > 200  # paper
