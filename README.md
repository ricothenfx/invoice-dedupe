# invoice-dedupe

Duplicate invoice detection to prevent **double payments** — catching invoices that look
different (different numbers, formatting, vendor name variants) but are really the same
transaction.

Manual review by AP staff routinely misses subtle duplicates. This project detects them
automatically through layered canonical normalization, weighted fuzzy matching, O(N²)-free
blocking, and contextual rules — with a review queue for the gray zone.

> **New here (human or AI agent)?** Read [`AGENTS.md`](AGENTS.md) first, then
> [`docs/PROJECT_CHARTER.md`](docs/PROJECT_CHARTER.md) (goals, agreed decisions, roadmap)
> and [`docs/DESIGN.md`](docs/DESIGN.md) (architecture, algorithms, decision log).
> Development must rely on those documents, not on any chat history.

## Results (synthetic dataset: 10,200 invoices, 200 duplicates, seed 42)

| Metric | Value |
|---|---|
| Precision @ flag (0.90) | **0.9852** (3 FP) |
| Recall @ flag | **1.0000** (200/200) |
| F1 | **0.9926** |
| Blocking reduction | 52,014,900 → 4,525 comparisons (**99.99%**) |
| Detection time | ~1.3 s, single process |
| Duplicate value caught | **$7.36M** |

Recall per duplicate variant (all 100%): `format`, `confusion`, `vendor_variant`,
`reinvoice` (vendor resubmits with a brand-new number — the hardest and most expensive
class), `date_shift`.

**The 3 false positives** share one pattern: identical vendor + identical amount +
≤14 days apart + different numbers, flagged by the re-invoice rule. That is healthy
precision: such a pair genuinely deserves human scrutiny in the real world (monthly
billing that happens to land close together vs. an actual resubmission). Planned
mitigations: a `billing_period` field (Phase 2) and a feedback loop from review
decisions (Phase 4).

## How it works

```mermaid
flowchart LR
    A[Invoices<br/>PDF/CSV/ERP] --> B[Canonical<br/>normalization]
    B --> C[Blocking<br/>2 candidate keys]
    C --> D[Scoring<br/>weighted fuzzy + rules]
    D --> E{Label}
    E -->|>= 0.90| F[Flag: duplicate]
    E -->|0.70-0.90| G[Review queue]
    E -->|< 0.70| H[Pass]
```

**Layer 1 — canonical normalization** (~70% of the win). Invoice numbers are stripped of
separators, uppercased, and look-alike characters are mapped only when adjacent to a digit
(`INV-O78L` ≡ `INV-0781`, while the `INV` prefix stays intact). Vendor names lose their
legal entity form (`Ltd`, `GmbH`, `PT`, `Tbk`, `B.V.`, ...) across jurisdictions. Amounts
support both international (1,250,000.00) and Indonesian/European (1.250.000,00) formats.

**Layer 2 — weighted fuzzy scoring.** Per-field similarity combined by weights: invoice
number 0.35, vendor 0.25, amount 0.30, date 0.10. Unequal invoice numbers are discounted
(factor 0.7): sequential numbers (`...0001` vs `...0002`) are near-identical
character-wise yet usually distinct transactions — strong evidence must come from amount
and vendor instead.

**Layer 3 — contextual rule.** The re-invoice pattern: exact same vendor + exact same
amount + different number + ≤14 days apart lifts the score to a floor of 0.92 (flag).
This catches the most expensive duplicate class that humans miss most often.

**Hard rule.** Differing tax IDs cap the score at 0.55 — different legal entities are
almost never duplicates.

**Blocking.** Only pairs sharing a `(vendor_key, invoice_no_norm)` or `(amount)` key are
compared, keeping complexity far below O(N²). Degenerate blocks (>1000 members) are
skipped as a safety valve.

## Quickstart

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# end-to-end demo: generate 10k invoices -> detect -> evaluate
invoice-dedupe demo --n 10000 --seed 42

# or step by step
invoice-dedupe generate --n 10000 --seed 42 --out datasets
invoice-dedupe run --dataset datasets/invoices.jsonl --out datasets/pairs.jsonl
invoice-dedupe evaluate --dataset datasets/invoices.jsonl --pairs datasets/pairs.jsonl

pytest
```

## Layout

```
src/invoice_dedupe/
  normalize.py   per-field canonicalization (number, vendor, amount, tax ID, period)
  scoring.py     fuzzy similarity, weights, re-invoice & tax-ID rules, thresholds
  blocking.py    candidate generation without O(N^2)
  engine.py      pipeline: normalize -> block -> score -> label
  synth.py       synthetic dataset generator + 5 duplicate variant classes (ground truth)
  evaluate.py    precision/recall/F1, threshold sweep, per-variant recall
  dataset.py     JSONL I/O
  cli.py         generate / run / evaluate / demo
```

## Roadmap

- **Phase 2**: FastAPI service + PDF ingestion (pdfplumber) + PostgreSQL + worker queue
- **Phase 3**: Interactive review-queue web app (React) with per-field score breakdown
- **Phase 4**: Vision-LLM extraction fallback for scans/photos + feedback loop
  (threshold tuning and weight adjustment from reviewer decisions)

Acceptance criteria per phase: see [`docs/PROJECT_CHARTER.md`](docs/PROJECT_CHARTER.md).

## Honest limitations

- Evaluation uses synthetic data; real-world duplicates are messier (OCR noise, currency
  differences, credit notes).
- Legitimate recurring invoices (monthly subscriptions) are deliberately planted in the
  dataset as *hard negatives*. Currently some land in the review queue rather than being
  flagged; handling them fully requires `billing_period` (Phase 2) and the feedback loop
  (Phase 4).
- Thresholds (0.90/0.70) are hand-set for now; labeled review decisions will replace them.
