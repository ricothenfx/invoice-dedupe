"""Template-match text recognition for fixture photos.

The mock vision-LLM (``vision.MockVisionClient``) must read invoice fields
from image *pixels* without a network call and without shipping an OCR
dependency. Because fixture photos (``photogen``) are rendered from the same
embedded 5x7 bitmap font, a small recognizer recovers the text reliably:
binarize, segment lines/glyphs by ink projection, normalize each glyph box
to 5x7, and take the nearest font template by Hamming distance.

This is deliberately NOT a general OCR engine: arbitrary photos return few
or no labels, which the vision layer reports as missing fields with low
confidence — the same honest behavior a weak model would produce.
"""
from __future__ import annotations

import struct
import zlib

from .photogen import FONT_5X7

_BINARY_THRESHOLD = 128
_MIN_GLYPH_INK = 2
_MAX_ERROR_RATE = 0.45  # max XOR pixel error (vs template ink) to accept a match
_MIN_XOR_DENOM = 60.0  # absolute pixel slack so tiny glyphs are not noise-dominated


class UnreadableImage(ValueError):
    """The image cannot be decoded as a supported grayscale PNG."""


def decode_png(data: bytes) -> list[list[int]]:
    """Decode an 8-bit grayscale, non-interlaced PNG into pixel rows."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise UnreadableImage("not a PNG file")
    pos, idat, meta = 8, bytearray(), None
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        tag = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        if tag == b"IHDR":
            meta = struct.unpack(">IIBBBBB", body)
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
        pos += 12 + length
    if meta is None:
        raise UnreadableImage("missing IHDR")
    width, height, depth, color, *_ = meta
    if depth != 8 or color != 0:
        raise UnreadableImage("only 8-bit grayscale PNG is supported")
    raw = zlib.decompress(bytes(idat))
    stride = width + 1  # filter byte per row
    if len(raw) != stride * height:
        raise UnreadableImage("unexpected PNG payload size")
    rows = []
    for y in range(height):
        row = raw[y * stride + 1 : (y + 1) * stride]
        if raw[y * stride] != 0:
            raise UnreadableImage("only filter mode 0 is supported")
        rows.append(list(row))
    return rows


def _binarize(rows: list[list[int]]) -> list[list[bool]]:
    return [[v < _BINARY_THRESHOLD for v in row] for row in rows]


def _segments(flags: list[bool], min_gap: int) -> list[tuple[int, int]]:
    """Contiguous True-runs, merging runs separated by fewer than min_gap gaps."""
    out: list[tuple[int, int]] = []
    start = None
    gap = 0
    for i, on in enumerate(flags + [False] * max(1, min_gap)):
        if on:
            if start is None:
                start = i
            gap = 0
        elif start is not None:
            gap += 1
            if gap >= min_gap:
                out.append((start, i - gap + 1))
                start, gap = None, 0
    return out


def _denoise(rows: list[list[bool]]) -> list[list[bool]]:
    """Remove specks: drop dark pixels with no dark orthogonal neighbor, and
    fill bright pixels that are surrounded by dark ink (holes in strokes)."""
    h, w = len(rows), len(rows[0])
    out = [row[:] for row in rows]

    def dark(y: int, x: int) -> bool:
        return 0 <= y < h and 0 <= x < w and rows[y][x]

    for y in range(h):
        for x in range(w):
            n = dark(y - 1, x) + dark(y + 1, x) + dark(y, x - 1) + dark(y, x + 1)
            if rows[y][x]:
                if n == 0:
                    out[y][x] = False  # isolated pepper speck
            elif n >= 3:
                out[y][x] = True  # salt hole inside a stroke
    return out


def _template_ink_span(template: tuple[int, ...]) -> tuple[range, range, int] | None:
    """(row span, col span, ink pixel count) of a template in font cells."""
    rows = [y for y, bits in enumerate(template) if bits]
    if not rows:
        return None
    cols = [x for x in range(5) if any(bits & (0x10 >> x) for bits in template)]
    ink = sum(bin(bits).count("1") for bits in template)
    return range(min(rows), max(rows) + 1), range(min(cols), max(cols) + 1), ink


_SPANS = {ch: _template_ink_span(t) for ch, t in FONT_5X7.items()}


def _render(template: tuple[int, ...], s: float) -> list[list[bool]]:
    """Render a font template at scale ``s`` (pixels per font cell)."""
    cols, rows = max(1, round(5 * s)), max(1, round(7 * s))
    canvas = [[False] * cols for _ in range(rows)]
    for ry, bits in enumerate(template):
        for rx in range(5):
            if bits & (0x10 >> rx):
                y0, y1 = round(ry * s), round((ry + 1) * s)
                x0, x1 = round(rx * s), round((rx + 1) * s)
                for y in range(y0, min(y1, rows)):
                    for x in range(x0, min(x1, cols)):
                        canvas[y][x] = True
    return canvas


def _xor_error(box: list[list[bool]], rendered: list[list[bool]], dy: int) -> int:
    """XOR pixel count between the glyph box and a vertically shifted rendering."""
    h, w = len(box), len(box[0])
    rh, rw = len(rendered), len(rendered[0])
    canvas_w = max(w, rw)
    xor = 0
    for y in range(max(h, rh)):
        by, ry = y, y - dy
        brow = box[by] if 0 <= by < h else ()
        rrow = rendered[ry] if 0 <= ry < rh else ()
        for x in range(canvas_w):
            if (x < len(brow) and brow[x]) != (x < len(rrow) and rrow[x]):
                xor += 1
    return xor


def _crop_to_ink(box: list[list[bool]]) -> list[list[bool]]:
    first = next((y for y, row in enumerate(box) if any(row)), 0)
    last = next((y for y in range(len(box) - 1, -1, -1) if any(box[y])), len(box) - 1)
    rows = box[first : last + 1]
    cols = [x for x in range(len(rows[0])) if any(r[x] for r in rows)]
    if cols:
        lo, hi = min(cols), max(cols) + 1
        rows = [r[lo:hi] for r in rows]
    return rows


def _crop_render(rendered: list[list[bool]]) -> list[list[bool]]:
    if not any(any(r) for r in rendered):
        return rendered
    return _crop_to_ink(rendered)


def _match_glyph(box: list[list[bool]]) -> str:
    cropped = _crop_to_ink(box)
    h, w = len(cropped), len(cropped[0])
    if h < 2 or w < 1:
        return "?"
    best_ch, best_err = "?", _MAX_ERROR_RATE
    for ch, template in FONT_5X7.items():
        span = _SPANS[ch]
        if span is None:
            continue
        rspan, cspan, t_ink = span
        # per-template scale: the ink spans must map onto the box consistently
        s_r, s_c = h / len(rspan), w / len(cspan)
        # small glyphs tolerate a larger relative wobble (1 px is a big share)
        tol = max(0.35 * max(s_r, s_c), 1.6)
        if abs(s_r - s_c) > tol:
            continue
        s = (s_r + s_c) / 2
        rendered = _crop_render(_render(template, s))
        rh, rw = len(rendered), len(rendered[0])
        if abs(rh - h) > max(3, 0.4 * h) or abs(rw - w) > max(3, 0.6 * w):
            continue
        err = min(
            _xor_error(cropped, rendered, dy) / max(t_ink * s * s, _MIN_XOR_DENOM)
            for dy in (-1, 0, 1)
        )
        if err < best_err:
            best_ch, best_err = ch, err
    # fallback only: when nothing matched, classify dot-like boxes by size
    # (a comma is a 2x2-cell diagonal, a period a single cell)
    if best_ch == "?" and w <= 8 and h >= w:
        return "," if h >= 1.4 * w else "."
    return best_ch


def extract_text(data: bytes) -> str:
    """Read text lines from a fixture photo rendered by ``photogen``.

    Returns "" when no readable text is present; unrecognized glyphs become
    '?' so label parsing fails honestly instead of guessing.
    """
    rows = _denoise(_binarize(decode_png(data)))
    row_ink = [sum(1 for v in r if v) for r in rows]
    lines = []
    for y0, y1 in _segments([n > 0 for n in row_ink], min_gap=2):
        line_rows = rows[y0:y1]
        h = y1 - y0
        col_ink = [sum(1 for r in line_rows if r[x]) for x in range(len(rows[0]))]
        segs = [
            (x0, x1)
            for x0, x1 in _segments([n >= _MIN_GLYPH_INK for n in col_ink], min_gap=2)
            if x1 - x0 >= 2 and h >= 4
        ]
        # a speck column can split one glyph in two; such gaps are narrower
        # than the render's letter pitch gap, so re-join them
        merged: list[list[int]] = []
        for x0, x1 in segs:
            if merged and x0 - merged[-1][1] <= 3:
                merged[-1][1] = x1
            else:
                merged.append([x0, x1])
        segs = [(a, b) for a, b in merged]
        if not segs:
            continue
        # The renderer places every character on a fixed pitch, so the mode of
        # segment-start differences is the pitch; a start difference of ~2x
        # pitch means one blank character (space) between the glyphs.
        starts = [x0 for x0, _ in segs]
        diffs = [b - a for a, b in zip(starts, starts[1:])]
        pitch = _mode(diffs)
        chars = []
        for i, (x0, x1) in enumerate(segs):
            if i:
                blanks = max(0, round(diffs[i - 1] / pitch) - 1) if pitch else 1
                chars.extend(" " * min(blanks, 8))
            box = [[line_rows[y][x] for x in range(x0, x1)] for y in range(h)]
            chars.append(_match_glyph(box))
        text = "".join(chars).strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def _mode(values: list[int]) -> int:
    """Most common value, ties broken toward the smaller (letter pitch)."""
    counts: dict[int, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return min(
        (v for v, c in counts.items() if c == max(counts.values())),
        default=0,
    )
