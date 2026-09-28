"""Synthetic invoice dataset generator with labeled duplicate variants as ground truth.

Every duplicate carries `duplicate_of` (id of the original invoice) and `variant`
describing how it was created:
  format        : same number, re-formatted (separators / lowercasing)
  confusion     : look-alike characters (0<->O, 1<->I, 5<->S, ...)
  vendor_variant: different legal-form rendering + typo in the vendor name
  reinvoice     : brand-new invoice number (vendor resubmits), date shifted 2-14 days
  date_shift    : date shifted 3-25 days, same number
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, replace
from datetime import date, timedelta

from .models import Invoice
from .normalize import normalize_invoice_no, safe_date

BIZ_WORDS = [
    "Northwind", "Acme", "Apex", "Summit", "Vertex", "Pinnacle", "Harbor",
    "Cascade", "Sterling", "Beacon", "Pioneer", "Frontier", "Meridian",
    "Zenith", "Orion", "Atlas", "Nova", "Quantum", "United", "Consolidated",
]
INDUSTRIES = [
    "Logistics", "Technology", "Construction", "Trading", "Manufacturing",
    "Media", "Agriculture", "Property", "Energy", "Pharma",
]

AMOUNT_CATEGORIES = {
    "small": (500, 5_000, 1),
    "mid": (5_000, 50_000, 5),
    "large": (50_000, 500_000, 50),
}

INV_TEMPLATES = ["yr_slash", "yr_plain", "plain5", "prefix", "mm_slash"]
PREFIXES = ["INV", "BIL", "TRX", "QTN", "FIN"]

VARIANT_FORMAT = "format"
VARIANT_CONFUSION = "confusion"
VARIANT_VENDOR = "vendor_variant"
VARIANT_REINVOICE = "reinvoice"
VARIANT_DATE_SHIFT = "date_shift"

VARIANT_MIX = [
    (VARIANT_REINVOICE, 0.25),
    (VARIANT_VENDOR, 0.20),
    (VARIANT_DATE_SHIFT, 0.20),
    (VARIANT_FORMAT, 0.18),
    (VARIANT_CONFUSION, 0.17),
]

CONFUSABLE_PAIRS = [
    ("0", "O"), ("1", "I"), ("1", "L"), ("5", "S"),
    ("8", "B"), ("2", "Z"), ("6", "G"),
]
_CONFUSABLE_DIGITS = {digit for digit, _ in CONFUSABLE_PAIRS}


@dataclass
class _VendorState:
    name: str
    tax_id: str
    template: str
    prefix: str
    seq: int
    category: str
    style: int
    recurring: bool = False
    recurring_amount: int | None = None


def render_vendor_name(name: str, style: int) -> str:
    if style == 0:
        return f"{name} Ltd"
    if style == 1:
        return f"{name} GmbH"
    if style == 2:
        return f"{name}, Inc."
    return f"{name} B.V."


def gen_tax_id(rng: random.Random) -> str:
    digits = "".join(str(rng.randint(0, 9)) for _ in range(13))
    return f"{digits[:2]}-{digits[2:9]}-{digits[9:]}"


def make_invoice_no(v: _VendorState, year: int, month: int, seq: int, template: str | None = None) -> str:
    t = template or v.template
    if t == "yr_slash":
        return f"INV-{year}/{seq:04d}"
    if t == "yr_plain":
        return f"INV{year}{seq:04d}"
    if t == "plain5":
        return f"INV-{seq:05d}"
    if t == "prefix":
        return f"{v.prefix}-{year}-{seq:04d}"
    return f"INV/{month:02d}/{seq:04d}"


def _clamp_date(d: date, start: date, end: date) -> date:
    return max(start, min(end, d))


def _reformat_invoice_no(no: str, rng: random.Random) -> str:
    base = re.sub(r"[^0-9A-Za-z]", "", no)
    sep = rng.choice(["", "-", " ", "/", "."])
    cuts = sorted(rng.sample(range(1, len(base)), k=min(2, len(base) - 1)))
    parts, prev = [], 0
    for c in cuts:
        parts.append(base[prev:c])
        prev = c
    parts.append(base[prev:])
    body = sep.join(parts)
    return body.lower() if rng.random() < 0.5 else body


def _confuse_invoice_no(no: str, rng: random.Random) -> str:
    chars = list(no)
    eligible = [i for i, ch in enumerate(chars) if ch in _CONFUSABLE_DIGITS]
    if not eligible:
        return _reformat_invoice_no(no, rng)
    pos = rng.choice(eligible)
    options = [letter for digit, letter in CONFUSABLE_PAIRS if digit == chars[pos]]
    chars[pos] = rng.choice(options)
    return "".join(chars)


def _typo_vendor(name: str, rng: random.Random) -> str:
    chars = list(name)
    idxs = [i for i, ch in enumerate(chars) if ch.isalpha()]
    if not idxs:
        return name
    op = rng.random()
    i = rng.choice(idxs)
    if op < 0.4:
        del chars[i]
    elif op < 0.7 and i + 1 < len(chars) and chars[i + 1].isalpha():
        chars[i], chars[i + 1] = chars[i + 1], chars[i]
    else:
        chars.insert(i, chars[i])
    return "".join(chars)


def _make_variant(
    src: Invoice,
    vendor: _VendorState,
    variant: str,
    rng: random.Random,
    used_norm: set[str],
    start: date,
    end: date,
) -> Invoice:
    assert src.invoice_date is not None
    if variant == VARIANT_FORMAT:
        return replace(src, invoice_no=_reformat_invoice_no(src.invoice_no, rng))
    if variant == VARIANT_CONFUSION:
        return replace(src, invoice_no=_confuse_invoice_no(src.invoice_no, rng))
    if variant == VARIANT_VENDOR:
        return replace(src, vendor_name=_typo_vendor(render_vendor_name(vendor.name, rng.randint(0, 3)), rng))
    if variant == VARIANT_REINVOICE:
        others = [t for t in INV_TEMPLATES if t != vendor.template]
        template = rng.choice(others)
        shift = rng.randint(2, 14)
        d = _clamp_date(src.invoice_date + timedelta(days=shift * (1 if rng.random() < 0.5 else -1)), start, end)
        if d == src.invoice_date:
            d = _clamp_date(src.invoice_date + timedelta(days=shift), start, end)
        seq = rng.randint(10001, 99999)
        no = make_invoice_no(vendor, d.year, d.month, seq, template=template)
        while normalize_invoice_no(no) in used_norm:
            seq += 1
            no = make_invoice_no(vendor, d.year, d.month, seq, template=template)
        return replace(src, invoice_no=no, invoice_date=d)
    if variant == VARIANT_DATE_SHIFT:
        shift = rng.randint(3, 25)
        d = _clamp_date(src.invoice_date + timedelta(days=shift * (1 if rng.random() < 0.5 else -1)), start, end)
        if d == src.invoice_date:
            d = _clamp_date(src.invoice_date + timedelta(days=shift), start, end)
        return replace(src, invoice_date=d)
    raise ValueError(f"unknown variant: {variant}")


def generate_dataset(
    n_invoices: int = 10_000,
    dup_rate: float = 0.02,
    seed: int = 42,
    start: date = date(2024, 1, 1),
    end: date = date(2024, 12, 31),
) -> tuple[list[Invoice], dict]:
    """Build a corpus of legitimate invoices plus labeled duplicates. Reproducible via seed."""
    rng = random.Random(seed)
    year = start.year

    combos = [f"{b} {ind}" for b in BIZ_WORDS for ind in INDUSTRIES]
    rng.shuffle(combos)
    n_vendors = min(120, len(combos))

    vendors: list[_VendorState] = []
    for name in combos[:n_vendors]:
        category = rng.choices(list(AMOUNT_CATEGORIES), weights=[0.5, 0.35, 0.15])[0]
        vendors.append(
            _VendorState(
                name=name,
                tax_id=gen_tax_id(rng),
                template=rng.choice(INV_TEMPLATES),
                prefix=rng.choice(PREFIXES),
                seq=rng.randint(1, 9000),
                category=category,
                style=rng.randint(0, 3),
            )
        )

    invoices: list[Invoice] = []
    id_to_vendor: dict[str, int] = {}
    used_amount: dict[tuple[int, str], set[int]] = {}
    used_norm_no: dict[int, set[str]] = {i: set() for i in range(n_vendors)}
    recurring_ids: set[str] = set()

    def record(inv: Invoice, v_idx: int) -> None:
        invoices.append(inv)
        id_to_vendor[inv.id] = v_idx
        used_norm_no[v_idx].add(normalize_invoice_no(inv.invoice_no))
        key = (v_idx, f"{inv.invoice_date.year}-{inv.invoice_date.month:02d}")
        used_amount.setdefault(key, set()).add(inv.amount)

    recurring_pool = [i for i, v in enumerate(vendors) if rng.random() < 0.025]
    for idx in (0, 1):
        if idx not in recurring_pool:
            recurring_pool.append(idx)
    recurring_pool.sort()
    for v_idx in recurring_pool:
        v = vendors[v_idx]
        v.recurring = True
        lo, hi, step = AMOUNT_CATEGORIES[v.category]
        v.recurring_amount = rng.randrange(lo, hi + 1, step)
        base_day = rng.randint(5, 25)
        for month in range(1, 13):
            d = safe_date(year, month, base_day + rng.randint(-2, 2))
            no = make_invoice_no(v, year, month, v.seq)
            v.seq += 1
            inv = Invoice(
                id=f"INV-{len(invoices):06d}",
                vendor_name=render_vendor_name(v.name, v.style),
                invoice_no=no,
                amount=v.recurring_amount,
                invoice_date=d,
                tax_id=v.tax_id,
            )
            recurring_ids.add(inv.id)
            record(inv, v_idx)

    general_pool = [i for i in range(n_vendors) if i not in set(recurring_pool)]
    n_general = n_invoices - len(invoices)
    assignment = [general_pool[i % len(general_pool)] for i in range(n_general)]
    rng.shuffle(assignment)

    for v_idx in assignment:
        v = vendors[v_idx]
        lo, hi, step = AMOUNT_CATEGORIES[v.category]
        d = start + timedelta(days=rng.randint(0, (end - start).days))
        period = f"{d.year}-{d.month:02d}"
        amount = rng.randrange(lo, hi + 1, step)
        for _ in range(50):
            if amount not in used_amount.get((v_idx, period), set()):
                break
            amount = rng.randrange(lo, hi + 1, step)
        no = make_invoice_no(v, d.year, d.month, v.seq)
        v.seq += 1
        record(
            Invoice(
                id=f"INV-{len(invoices):06d}",
                vendor_name=render_vendor_name(v.name, v.style),
                invoice_no=no,
                amount=amount,
                invoice_date=d,
                tax_id=v.tax_id,
            ),
            v_idx,
        )

    eligible = [inv for inv in invoices if inv.id not in recurring_ids]
    n_dup = min(round(n_invoices * dup_rate), len(eligible))
    dup_sources = rng.sample(eligible, n_dup)

    variant_counts: dict[str, int] = {}
    dup_invoices: list[Invoice] = []
    variants = [vt for vt, _ in VARIANT_MIX]
    weights = [w for _, w in VARIANT_MIX]
    for k, src in enumerate(dup_sources):
        v_idx = id_to_vendor[src.id]
        variant = rng.choices(variants, weights=weights)[0]
        dup = _make_variant(src, vendors[v_idx], variant, rng, used_norm_no[v_idx], start, end)
        dup = replace(dup, id=f"INV-D{k:05d}", duplicate_of=src.id, variant=variant)
        used_norm_no[v_idx].add(normalize_invoice_no(dup.invoice_no))
        dup_invoices.append(dup)
        variant_counts[variant] = variant_counts.get(variant, 0) + 1

    dataset = invoices + dup_invoices
    meta = {
        "seed": seed,
        "n_invoices": len(dataset),
        "n_legit": len(invoices),
        "n_duplicates": len(dup_invoices),
        "n_vendors": n_vendors,
        "n_recurring_vendors": len(recurring_pool),
        "dup_rate_effective": round(len(dup_invoices) / len(invoices), 5),
        "variant_counts": dict(sorted(variant_counts.items())),
    }
    return dataset, meta
