"""Dependency-free photo fixture generator: invoice text rendered into a PNG.

Phase 4 companion to ``pdfgen`` (charter decision D6): demos and tests need
*images* (phone photos of invoices) to exercise the vision-LLM fallback
without adding an imaging dependency. The module embeds a small 5x7 bitmap
font, renders labeled invoice text, and applies deterministic "crumpled
phone photo" degradations (salt-and-pepper specks, per-line jitter, a
brightness gradient). Output is an 8-bit grayscale PNG built with stdlib
``zlib`` only.

The same embedded font powers the mock vision-LLM's template-matching
recognizer (``ocr.py``), so fixture photos are genuinely read back from
their pixels while staying fully offline and reproducible.
"""
from __future__ import annotations

import random
import struct
import zlib

# --- 5x7 bitmap font (uppercase letters, digits, punctuation) ---------------
# Each glyph is 7 rows of 5 bits, MSB = leftmost column.

FONT_5X7: dict[str, tuple[int, ...]] = {
    "A": (0x0E, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
    "B": (0x1E, 0x11, 0x11, 0x1E, 0x11, 0x11, 0x1E),
    "C": (0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E),
    "D": (0x1C, 0x12, 0x11, 0x11, 0x11, 0x12, 0x1C),
    "E": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x1F),
    "F": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x10),
    "G": (0x0E, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0F),
    "H": (0x11, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
    "I": (0x0E, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "J": (0x07, 0x02, 0x02, 0x02, 0x02, 0x12, 0x0C),
    "K": (0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11),
    "L": (0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1F),
    "M": (0x11, 0x1B, 0x15, 0x15, 0x11, 0x11, 0x11),
    "N": (0x11, 0x11, 0x19, 0x15, 0x13, 0x11, 0x11),
    "O": (0x0E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
    "P": (0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10, 0x10),
    "Q": (0x0E, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0D),
    "R": (0x1E, 0x11, 0x11, 0x1E, 0x14, 0x12, 0x11),
    "S": (0x0F, 0x10, 0x10, 0x0E, 0x01, 0x01, 0x1E),
    "T": (0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04),
    "U": (0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
    "V": (0x11, 0x11, 0x11, 0x11, 0x11, 0x0A, 0x04),
    "W": (0x11, 0x11, 0x11, 0x15, 0x15, 0x15, 0x0A),
    "X": (0x11, 0x11, 0x0A, 0x04, 0x0A, 0x11, 0x11),
    "Y": (0x11, 0x11, 0x0A, 0x04, 0x04, 0x04, 0x04),
    "Z": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1F),
    "0": (0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E),
    "1": (0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "2": (0x0E, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1F),
    "3": (0x1F, 0x02, 0x04, 0x02, 0x01, 0x11, 0x0E),
    "4": (0x02, 0x06, 0x0A, 0x12, 0x1F, 0x02, 0x02),
    "5": (0x1F, 0x10, 0x1E, 0x01, 0x01, 0x11, 0x0E),
    "6": (0x06, 0x08, 0x10, 0x1E, 0x11, 0x11, 0x0E),
    "7": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08),
    "8": (0x0E, 0x11, 0x11, 0x0E, 0x11, 0x11, 0x0E),
    "9": (0x0E, 0x11, 0x11, 0x0F, 0x01, 0x02, 0x0C),
    " ": (0, 0, 0, 0, 0, 0, 0),
    ":": (0x00, 0x04, 0x00, 0x00, 0x04, 0x00, 0x00),
    "-": (0x00, 0x00, 0x00, 0x1F, 0x00, 0x00, 0x00),
    ",": (0x00, 0x00, 0x00, 0x00, 0x00, 0x04, 0x08),
    ".": (0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x04),
    "/": (0x01, 0x01, 0x02, 0x04, 0x08, 0x10, 0x10),
}


def make_png(pixels: list[list[int]]) -> bytes:
    """Encode an 8-bit grayscale image (list of rows) as a PNG file."""
    height, width = len(pixels), len(pixels[0])
    raw = b"".join(b"\x00" + bytes(row) for row in pixels)

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + tag + body
            + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            chunk(b"IHDR", ihdr),
            chunk(b"IDAT", zlib.compress(raw, 9)),
            chunk(b"IEND", b""),
        ]
    )


def render_text(
    lines: list[str],
    *,
    scale: int = 4,
    seed: int = 7,
    speck_rate: float = 0.004,
    jitter: int = 1,
) -> bytes:
    """Render invoice text lines as a degraded grayscale PNG (phone-photo look).

    Degradations are seeded and deterministic: salt-and-pepper specks, a
    horizontal brightness gradient, and a small vertical jitter per text line.
    They stay mild enough for the mock recognizer to read the text back.
    """
    rng = random.Random(seed)
    margin = 4 * scale
    line_h = 9 * scale
    width = margin * 2 + max((len(line) for line in lines), default=1) * 6 * scale
    height = margin * 2 + len(lines) * line_h

    # paper background with a mild brightness gradient (crumpled-sheet feel)
    px = [
        [max(150, min(255, 235 - (30 * x) // max(1, width) + rng.randint(-4, 4)))
         for x in range(width)]
        for _ in range(height)
    ]

    def ink(x: int, y: int) -> None:
        if 0 <= y < height and 0 <= x < width:
            px[y][x] = rng.randint(10, 60)

    for li, line in enumerate(lines):
        dy = rng.randint(-jitter, jitter)  # per-line jitter
        y0 = margin + li * line_h + dy
        for ci, ch in enumerate(line):
            glyph = FONT_5X7.get(ch.upper())
            if glyph is None:
                glyph = FONT_5X7[" "]  # unknown characters render as blanks
            x0 = margin + ci * 6 * scale
            # punctuation stays crisp: a ±1 px wobble would blur a comma
            # into a period at fixture resolution
            wobble = sum(1 for b in glyph if b) >= 3
            for ry, bits in enumerate(glyph):
                dy2 = rng.randint(-1, 1) if wobble else 0
                for rx in range(5):
                    if bits & (0x10 >> rx):
                        for sy in range(scale):
                            for sx in range(scale):
                                ink(x0 + rx * scale + sx, y0 + ry * scale + sy + dy2)

    # salt-and-pepper specks (sensor noise / paper creases)
    for _ in range(int(width * height * speck_rate)):
        x, y = rng.randrange(width), rng.randrange(height)
        px[y][x] = rng.choice((0, 255))

    return make_png(px)


def make_photo(
    *,
    vendor: str,
    invoice_no: str,
    invoice_date: str,
    amount: str,
    tax_id: str,
    seed: int = 7,
) -> bytes:
    """Render a labeled invoice as a crumpled-photo PNG fixture."""
    lines = [
        f"Vendor: {vendor}",
        f"Invoice No: {invoice_no}",
        f"Invoice Date: {invoice_date}",
        f"Amount Due: USD {amount}",
        f"Tax ID: {tax_id}",
    ]
    return render_text(lines, seed=seed)
