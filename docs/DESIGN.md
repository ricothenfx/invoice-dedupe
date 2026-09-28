# DESIGN — invoice-dedupe

Companion to [`PROJECT_CHARTER.md`](PROJECT_CHARTER.md) (the *what/why*) and
[`AGENTS.md`](../AGENTS.md) (working rules). This document is the *how*: architecture,
algorithms, data model, evaluation methodology, and the decision log. **Update the
decision log whenever behavior changes.**

## 1. Detection pipeline

```
invoices (list[Invoice])
   │  normalize_invoice()                     per-field canonicalization
   ▼
NormalizedInvoice[]
   │  candidate_pairs()                       blocking, avoids O(N²)
   ▼
candidate pairs (index pairs)
   │  score_pair()                            weighted fuzzy + rules
   ▼
PairResult[]  (score, breakdown, label: flag / review / pass)
   │  evaluate()                              vs ground truth
   ▼
EvalReport    (precision / recall / F1, sweep, per-variant recall)
```

Everything is deterministic: datasets come from seeded generation, and scoring is a pure
function of normalized fields plus `ScoringConfig`.

## 2. Normalization rules (Layer 1)

The highest-leverage layer. All matching operates on canonical forms, never raw text.

| Field | Rule | Example |
|---|---|---|
| `invoice_no` | strip non-alphanumerics → uppercase → map look-alike chars **only when adjacent to a digit** | `inv 2024-0012` ≡ `INV-2024/0012`; `INV-O78L` → `INV0781` (but `INV` stays `INV`, never `1NV`) |
| `vendor_name` | uppercase → punctuation to space → drop legal-form tokens from prefix (`PT`,`CV`,`UD`) and suffix (`LTD`,`GMBH`,`TBK`,`BV`,`INC`,... multi-jurisdiction) → key = sorted tokens | `Northwind Logistics, Ltd.` ≡ `northwind logistics` |
| `amount` | parse with international (1,234,567.89) and Indonesian/European (1.234.567,89) conventions; currency symbols stripped | `USD 1,250,000.00` ≡ `1.250.000,00` |
| `tax_id` | digits only | `12-3456789-401` → `123456789401` |
| `billing_period` | `YYYY-MM` from invoice date (anti-FP key for recurring billing; used from Phase 2) | `2024-03-14` → `2024-03` |

Look-alike map: `O→0, I→1, L→1, S→5, B→8, Z→2, G→6`. The digit-adjacency gate exists
because a blanket map corrupts alphabetic prefixes (`INV` → `1NV`) and makes distinct
numbers collide.

## 3. Scoring (Layers 2–3 + hard rule)

### Weights (must sum to 1.0)

| Field | Weight | Similarity function |
|---|---|---|
| `invoice_no` | 0.35 | 1.0 if canonical forms equal; else `0.7 × JaroWinkler` |
| `vendor` | 0.25 | 1.0 if equal; else `max(token_set, token_sort) / 100` |
| `amount` | 0.30 | 1.0 if equal; else `max(0, 1 − |a−b|/max(a,b))` |
| `date` | 0.10 | 1.0 if same day; else `max(0, 1 − days/45)` |

### Label thresholds

| Score | Label | Meaning |
|---|---|---|
| ≥ 0.90 | `flag` | treated as duplicate (auto-flag) |
| 0.70 – 0.90 | `review` | human review queue |
| < 0.70 | `pass` | no action |

### Contextual rule (re-invoice pattern)

If vendor similarity is exactly 1.0 **and** amount similarity is exactly 1.0 **and**
canonical invoice numbers differ **and** the date gap is 1–14 days → score lifted to a
floor of 0.92 (`breakdown["rule"] = "reinvoice_pattern"`). This catches resubmissions
with brand-new numbers, the most expensive duplicate class.

### Hard rule (tax ID mismatch)

If both tax IDs are present and differ → score capped at 0.55 (different legal entities
are almost never duplicates). Applied **after** the contextual rule so it wins.

## 4. Blocking

A pair is scored only if it shares at least one key:

| Key | Catches |
|---|---|
| `vn\|<vendor_key>\|<invoice_no_norm>` | format / look-alike duplicates (same vendor) |
| `amt\|<amount>` | re-invoice, vendor-variant, date-shift duplicates (all preserve amount) |

Blocks larger than `MAX_BLOCK_SIZE = 1000` are skipped (counted in stats) to bound the
worst case. Measured effect at 10,200 invoices: 52,014,900 naive pairs → 4,525 candidates
(99.99% reduction).

*Design note*: every duplicate variant class preserves the amount by construction, so the
amount block guarantees recall for all classes; the `vn` block adds cheap precision
support. Cross-vendor same-number pairs are only compared when amounts collide, where the
tax-ID cap neutralizes them.

## 5. Synthetic dataset and evaluation

`synth.py` generates a reproducible corpus (seeded): 120 vendors across amount
categories, per-vendor invoice-number templates, dates across one year, plus:

- **Duplicate variants** (ground truth, `duplicate_of` + `variant` fields):
  `format` (re-formatted number), `confusion` (look-alike char), `vendor_variant`
  (legal-form change + typo), `reinvoice` (new number, date ±2–14d), `date_shift`
  (date ±3–25d). Only digits that have a confusable pair are substituted, so every
  confusion duplicate is recoverable by normalization.
- **Hard negatives**: ~2.5% of vendors are *recurring* — 12 invoices/year, identical
  amount, similar day-of-month. These mimic legitimate subscriptions and must not be
  flagged. They land in the review queue today; full handling needs `billing_period`
  (Phase 2) and the feedback loop (Phase 4).
- **Anti-collision constraints**: legitimate amounts are unique per (vendor, month);
  re-invoice numbers are checked against used canonical numbers per vendor.

Metrics: pair-level precision/recall/F1 at the flag threshold, a threshold sweep, recall
per variant class, and review-queue load. Current reference run (seed 42, 10,200
invoices): **P 0.9852 / R 1.0000 / F1 0.9926**, 3 FP (all same-vendor+amount ≤14d
re-invoice-pattern pairs — genuinely ambiguous without `billing_period`).

## 6. Data model (Phase 1, in-memory + JSONL)

```
Invoice          id, vendor_name, invoice_no, amount, currency, invoice_date,
                 tax_id, duplicate_of (GT), variant (GT)
NormalizedInvoice id, vendor, vendor_key, invoice_no, amount, date, tax_id, billing_period
PairResult       id_a, id_b, score, label, breakdown (per-field similarities + rules)
```

Phase 2 will persist these in PostgreSQL (`invoices`, `invoice_pairs`, `review_decisions`
— the last one feeding the Phase 4 feedback loop) and store raw extraction separately
from normalized fields.

## 7. Decision log

| Date | Decision | Rationale |
|---|---|---|
| 2026-09-28 | Discount unequal invoice numbers by 0.7 before JaroWinkler | Sequential numbers (`...0001` vs `...0002`) score ~0.98 char-wise yet are usually distinct transactions; strong evidence must come from amount+vendor. Prevented recurring-invoice false flags. |
| 2026-09-28 | Gate look-alike char mapping on digit adjacency | Blanket mapping corrupted alphabetic prefixes (`INV`→`1NV`) and broke exact-key matching. |
| 2026-09-28 | Tax-ID mismatch caps score at 0.55 | Different legal entities are almost never duplicates; also neutralizes cross-vendor same-number/same-amount collisions. |
| 2026-09-28 | Re-invoice contextual rule (floor 0.92) | The hardest class (new number from same vendor) scored ~0.80 with weights alone; the rule lifts unambiguous resubmissions to flag while date-window bounds false positives. |
| 2026-09-28 | Recurring vendors planted as hard negatives | The dominant real-world false-positive source must be in the evaluation set from day one; review queue is the honest interim home for them. |
| 2026-09-28 | Generator: substitute only digits that have confusable pairs; clamp-date fallback flips shift direction | Both bugs produced unrecoverable/zero-gap "duplicates"; fixed to keep ground truth well-defined. |
| 2026-09-28 | Synthetic amounts keep fine granularity (≥4,000 distinct values per category) | Coarse USD steps (180 distinct values) caused massive amount-block collisions: precision fell to 0.83. Restored to 0.985. |
| 2026-09-28 | Internationalized everything (English docs/CLI/data, USD, multi-jurisdiction legal forms) | Product targets a global audience (charter D5). |
| 2026-09-28 | Pair similarity rounding to 9 decimals | Floating-point noise made identical pairs score 0.9999…9 breaking exact-match tests. |
