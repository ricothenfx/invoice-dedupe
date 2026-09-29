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

## 6. Document ingestion (Phase 2, vision fallback in Phase 4)

The deterministic, primary extraction path of decision D6: PDFs with a text layer are
parsed locally with **pdfplumber**. Text-less PDFs and images (scans, phone photos) go
to the vision-LLM fallback (Phase 4).

```
upload (PDF | PNG | JPEG)
   │  worker._extract_dispatch()
   ├─ PDF with text layer ──►  pdfplumber                     (primary, D6)
   ├─ PDF without text    ──►  vision.extract_image()         (fallback, D6)
   └─ image (PNG/JPEG)    ──►  vision.extract_image()         (fallback, D6)
   │
raw response ──────────────►  stored verbatim (audit trail, invoices.raw_text)
   │  parse_fields() or vision.validate_extraction()
   ▼
fields (vendor, number, amount, date, tax id)
   │  result_to_invoice()      missing fields stay empty, never guessed
   ▼
Invoice → normalization → detection (unchanged Phase 1 engine)
```

Parser rules (pdfplumber path):
- Field lines must carry an explicit label (`Vendor:`, `Invoice No:`, `Invoice Date:`,
  `Amount Due:`/`Total:`, `Tax ID:`/`VAT:`); unstructured text is ignored.
- `Invoice No` and `Invoice Date` labels are disambiguated, and amounts reuse the
  Phase 1 international/European amount parser.
- Dates accept ISO, `14 Sep 2026`, `September 14, 2026`, and `14/09/2026` forms.
- **Extraction confidence** = fraction of the 4 core fields found (vendor, number,
  amount, date). It is persisted per invoice and reported by the API; missing fields
  are listed explicitly.

### 6.1 Vision-LLM fallback (Phase 4)

`vision.py` implements the D6 fallback with two clients behind one contract:

- **Mock** (default, offline): reads fixture photos with the embedded-font recognizer
  (`ocr.py`, see below) and emits the same JSON a real model would. CI, tests, and
  demos never touch the network. Photos it cannot read produce null fields with
  confidence 0 — the honest behavior of a blind model, never guessed fields.
- **Real** (opt-in): set `DEDUPE_VISION_LLM_URL` (plus optional
  `DEDUPE_VISION_LLM_MODEL`, `DEDUPE_VISION_LLM_API_KEY`) to call an
  OpenAI-compatible `/chat/completions` endpoint with the image as a base64 data
  URL. Built with stdlib `urllib` — **zero new runtime dependencies**. The prompt
  demands JSON-only output with the schema below and `null` for unreadable fields.

Extraction JSON schema (validated by `vision.validate_extraction`, no `jsonschema`
dependency): `vendor_name`/`invoice_no`/`tax_id` (string or null), `invoice_date`
(recognizable date string or null), `amount` (number or numeric string or null),
`extraction_confidence` (number in [0, 1], the model's own confidence). Schema
violations raise `VisionLLMError` and fail the job — bad model output never reaches
the engine. Stored **extraction confidence** = model confidence × core-field
completeness. The raw response is kept verbatim in `invoices.raw_text` (audit trail,
charter §4); `extraction_method` records `pdfplumber` | `vision_llm_mock` |
`vision_llm_api`.

Mock OCR (`ocr.py`): fixture photos (`photogen.py`) render labeled invoice text from
the same embedded 5×7 bitmap font, degraded deterministically (specks, per-line
jitter, glyph wobble, brightness gradient — "crumpled phone photo"). The recognizer
binarizes, removes speck noise, segments lines/glyphs by ink projection, and matches
each glyph against font templates by XOR distance at the matched scale. It is
deliberately not a general OCR: arbitrary photos yield few labels and low confidence,
which flows through the same missing-field reporting as a weak real model.

## 7. Service architecture (Phase 2, web app in Phase 3)

```
┌─────────┐  POST /invoices/pdf   ┌──────────┐  jobs table   ┌────────┐
│ client  │  POST /invoices/photo │ FastAPI  │ ────────────► │ worker │
│ (web UI)│ ◄───── JSON API ──────│  (api)   │               │(worker)│
└─────────┘                       └────┬─────┘               └───┬────┘
                                       │        PostgreSQL       │ pdfplumber
                                       │  ┌──────────────────┐   │ engine.detect
                                       └─►│ invoices         │◄──┘
                                          │ invoice_pairs    │
                                          │ review_decisions │
                                          │ jobs (queue)     │
                                          └──────────────────┘
```

- **API** (`api.py`): `GET /health`, `POST /invoices/pdf` (multipart, validated
  `%PDF-` magic, ≤10 MiB, returns `202` + job id), `GET /jobs/{id}`, `GET /invoices`,
  `GET /invoices/{id}` (includes raw extraction text), `GET /pairs?label=&undecided=`
  (enriched with both invoices' fields and the stored review decision — the review
  queue renders from this single request), and `POST /pairs/{a}/{b}/decision`
  (persists reviewer verdicts for the Phase 4 loop). Uploads are asynchronous by
  design: the API only validates and enqueues.
- **Worker** (`worker.py`): claims jobs from the `jobs` table with
  `SELECT … FOR UPDATE SKIP LOCKED` (safe with multiple workers), runs
  `extract_pdf` (extract → insert invoice with raw text → enqueue `detect`) and
  `detect` (re-run `engine.detect` over all invoices → atomically replace
  `invoice_pairs`). Failures mark the job `failed` with the error text; bad input
  never kills the worker.
- **Persistence** (`db.py`): plain SQL via psycopg 3. Raw extraction text lives in
  `invoices.raw_text`, separate from the parsed columns, so extraction can improve
  without invalidating audit trails (charter §4).

### 7.1 Web app (Phase 3)

The interactive review app is a single-page React application served by the same
FastAPI process from `src/invoice_dedupe/webapp/` (`GET /` returns `index.html`,
assets under `/static/`). Tabs:

- **Dashboard** (`GET /metrics`): totals (invoices, flagged/review/pass counts,
  decisions), a flag-threshold slider with live recomputed numbers, threshold
  curves, and the pair score histogram (true duplicates stacked against other
  candidates).
- **Review queue** (`GET /pairs`): pairs sorted by score, each rendered as a
  side-by-side comparison of both invoices with per-field score breakdown
  (invoice number 0.35 / vendor 0.25 / amount 0.30 / date 0.10, tax-ID chip,
  rule chip). Reviewer verdicts post to `POST /pairs/{a}/{b}/decision`;
  keyboard triage (`j`/`k` navigate, `d` duplicate, `n` not-a-duplicate) with
  auto-advance to the next undecided pair is what makes "100 flagged pairs in
  minutes" feasible.
- **Invoices** (`GET /invoices`): paginated table with extraction confidence and
  duplicate-variant badges.
- **Upload**: drop a text-layer PDF, polls `GET /jobs/{id}` until extraction and
  chained detection finish, reports confidence and missing fields.

`GET /metrics?flag_threshold=t` returns, in one payload:

- `label_counts`, `decisions`, `n_invoices`, `n_gt_pairs` (ground-truth
  duplicates known, or `null`);
- `at_threshold` — the server-computed, unit-tested numbers at `t`
  (`metrics.threshold_metrics`): flagged count, double-payment exposure
  (`value_at_risk`, the larger amount of each flagged pair), measured
  precision/recall when ground truth exists, and a decision-based precision
  estimate otherwise;
- `distribution` — one row per scored pair (`score`, `gt` flag, exposure,
  decision), so the UI recomputes the slider position live without refetching.

Precision signal precedence (honest by design): **measured** against seeded
ground truth when available (`seed-demo` loads the synthetic GT into nullable
`invoices.duplicate_of`/`variant` columns); else **estimated** from review
decisions at/above the threshold (confirmed ÷ decided, only when at least one
decision exists); else explicitly "no signal yet".

`invoice-dedupe seed-demo --n 10000 --seed 42` generates the synthetic dataset,
bulk-inserts it with ground truth, and runs detection synchronously, so the app
has a realistic review queue (203 flagged / 483 review pairs) within seconds —
no hand-crafted PDFs needed, fully offline.

### 7.2 Feedback loop (Phase 4)

Reviewer decisions in `review_decisions` drive threshold and weight tuning
(`tuning.py`, CLI `invoice-dedupe simulate-feedback` + `invoice-dedupe tune`).

**Training data**: only pairs a reviewer could see (base flag/review zones) with
their verdicts. `simulate-feedback` answers every such pair from the seeded
ground truth (duplicate iff GT pair, reviewer `simulated`) — the acceptance
criterion's "simulated feedback".

**Feature vector** (from the pair breakdown): invoice-no, vendor, amount, date
similarities, plus `billing_period_match` (stored in every breakdown since
Phase 4) and two re-invoice-rule interactions (`rule × same-period`,
`rule × diff-period`). The tax-ID check stays a hard rule, not a feature.

**Model**: logistic regression, sum-loss batch gradient descent with three
deliberate constraints, each validated against the seeded dataset before
shipping:

1. *Pinned weights*: vendor (0.25) and amount (0.30) never move — reviewed
   pairs have no variance in them (same vendor ⇒ same identifiers), so
   learning them would fit noise. The tax-ID mismatch cap is re-applied
   adaptively (below the tuned review threshold), preserving the base rule's
   semantics.
2. *Prior-anchored learning*: only the identifiable weights (invoice no, date,
   billing period, rule interactions) move, pulled toward the deployed values
   by a Gaussian prior — decisions adjust a validated model instead of
   rebuilding one from ~700 decisions.
3. *Global re-scoring*: when a tuning row is active, every `detect` run (and
   `tune`) re-scores **all** pairs with the tuned model, so the stored `score`
   column keeps one coherent meaning. The dashboard defaults its threshold to
   the active model's flag threshold; `/metrics` exposes the active model.

**Threshold selection** happens on decision labels only — F1 for the flag
threshold (auto-blocked payments demand precision), F2 for the review
threshold (the queue tolerates false alarms to keep recall high).

**Measured result** (seed 42, 10,200 invoices; `invoice-dedupe tune` output):

| | P | R | F1 | FP | flagged | review queue |
|---|---|---|---|---|---|---|
| before | 0.9852 | 1.0000 | 0.9926 | 3 | 203 | 483 |
| after | 0.9852 | 1.0000 | 0.9926 | 3 | 203 | **0** |

The measurable improvement is operational: the reviewer queue is eliminated at
identical detection quality (precision, recall, exposure all unchanged). The
learned weights separate recurring-billing collisions (same vendor + amount,
~30-day gaps) from true duplicates via date and billing period.

**Honest false-positive statement**: the 3 remaining FPs are not removable by
any model over the available features. All 3 are re-invoice-rule fires whose
dates cross a month boundary (`diff_period_notdup: 3`); the dataset also
contains 11 true re-invoice duplicates with the identical profile
(`diff_period_dup: 11`) — same vendor, same amount, different number, ≤14-day
gap across a month boundary. `tune` prints this analysis on every run.

## 8. Data model

Phase 1 (in-memory + JSONL):

```
Invoice          id, vendor_name, invoice_no, amount, currency, invoice_date,
                 tax_id, duplicate_of (GT), variant (GT)
NormalizedInvoice id, vendor, vendor_key, invoice_no, amount, date, tax_id, billing_period
PairResult       id_a, id_b, score, label, breakdown (per-field similarities + rules)
```

Phase 2 (PostgreSQL): `invoices` (parsed columns + `raw_text`,
`extraction_method`, `extraction_confidence`; Phase 3 adds nullable
`duplicate_of` / `variant` ground-truth columns, populated only by
`seed-demo`), `invoice_pairs` (`score`, `label`, `breakdown` JSONB — Phase 4
adds `billing_period_match` to every breakdown and a `tuned` audit sub-object
when a tuned model produced the score), `review_decisions`
(`duplicate` / `not_duplicate`), `jobs` (`extract_pdf` / `extract_image` /
`detect`, statuses `pending → running → done|failed`), and Phase 4's
`tuning` (one row per fitted model: weights, bias, thresholds, decision
counts, before/after metrics, `active` flag kept unique by a partial index).
Phase 3 reads these tables read-only through `GET /metrics` and the enriched
`GET /pairs`; Phase 4's `tune` command writes the `tuning` table and
`simulate-feedback` writes `review_decisions`.

## 9. Decision log

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
| 2026-09-28 | Phase 2 runtime deps: `fastapi` + `uvicorn` (API), `pdfplumber` (text-layer extraction), `psycopg[binary]` (PostgreSQL driver), `python-multipart` (FastAPI file uploads); dev-only `httpx` (TestClient) | Each is required by charter §5 Phase 2 scope (FastAPI service, pdfplumber ingestion, PostgreSQL). No ORM, no broker, no cloud SDKs; everything else reuses the stdlib. |
| 2026-09-28 | PostgreSQL `jobs` table as the worker queue (`FOR UPDATE SKIP LOCKED`), not Celery/Redis | One fewer service to run; transactional claim gives exactly-once processing with multiple workers; queue depth is visible in SQL. PDF payloads are base64 in the job row (uploads capped at 10 MiB), so no shared filesystem is needed. |
| 2026-09-28 | Raw SQL via psycopg 3 instead of an ORM (SQLAlchemy) | Four small tables; schema is a single auditable DDL string in `db.py`. An ORM would add a heavy dependency with no benefit at this scale. |
| 2026-09-28 | Dependency-free minimal PDF writer (`pdfgen.py`) instead of a PDF-generation library | Demos and tests need text-layer fixture PDFs; a 60-line writer avoids a dev/runtime dependency and is verified round-trip through pdfplumber. |
| 2026-09-28 | Conservative label-based field parser with per-invoice extraction confidence | Guessing fields would corrupt the matching engine's input and the audit trail. Missing fields are reported (`missing` list + confidence), and the vision-LLM fallback for layouts without labels is explicitly Phase 4 (charter D6). |
| 2026-09-28 | `detect` job recomputes all pairs and atomically replaces `invoice_pairs` | Detection is cheap (~1.3 s at 10k invoices) and pure; incremental pair maintenance would be complex and error-prone. Atomic replacement keeps the pair table always consistent with the invoice set. |
| 2026-09-28 | Phase 3 web app: React 18.3.1 vendored as UMD production builds served by FastAPI (`webapp/`), no node/npm build pipeline | Charter D3 names an interactive React web app the primary deliverable, but the acceptance criterion requires the demo to run offline and AGENTS.md requires minimal dependencies. Vendoring the two MIT-licensed UMD files (unmodified, from unpkg `react@18.3.1`/`react-dom@18.3.1`) gives real React with zero new Python packages, no CDN calls, and no build step; the SPA is plain `React.createElement` code in `app.js`. Revisit (Vite build) only if the UI grows beyond a single file. |
| 2026-09-28 | `seed-demo` command + nullable `invoices.duplicate_of`/`variant` columns | The dashboard's threshold slider needs a precision number that is honest at any position, which requires labeled pairs; persisting the synthetic ground truth makes the demo measurable (P/R at any threshold) instead of decorative. Columns are NULL for regular PDF ingestion, so production behavior is unchanged. Detection is re-run synchronously in the seed command (~1.3 s at 10k) — no worker round-trip needed. |
| 2026-09-28 | `GET /metrics` returns the full scored distribution; the server also computes `at_threshold` | The slider must update live for arbitrary thresholds; shipping raw per-pair rows (score, GT flag, exposure, decision — ~4.5 kB/1k pairs) lets the client recompute instantly. The server-side `at_threshold` block (`metrics.threshold_metrics`) is the unit-tested source of truth, so UI and API cannot drift far. Precision is reported as *measured* (vs seeded GT) or *estimated* (review decisions only) and never conflated. |
| 2026-09-28 | `GET /pairs` enriched with both invoices' fields + stored decision instead of client-side N+1 fetches | The review queue renders a comparison from one request; at 100+ queued pairs, per-invoice fetches would triple latency and complicate the UI for no benefit. |
| 2026-09-28 | Vision-LLM client with stdlib `urllib`, zero new runtime dependencies | The real path is one JSON POST to an OpenAI-compatible endpoint with a base64 data URL; an SDK (openai/anthropic) would add a dependency for a single call and pin API shapes we do not control anyway. The request/response contract lives in ~40 testable lines (`build_request`, response unwrapping, error wrapping), and tests inject a fake `urlopen` — no network. |
| 2026-09-28 | Mock vision mode reads pixels: embedded 5×7 bitmap font + template-matching recognizer (`photogen.py`, `ocr.py`) instead of canned responses | Charter D6 demands mock mode so CI and demos never hit the network, and honesty demands the mock not invent fields. Rendering fixture photos from the same font the recognizer matches against makes the extraction *real* (image → pixels → fields), deterministic, and offline, while arbitrary photos honestly degrade to missing fields + confidence 0. `photogen` is the `pdfgen` pattern extended to PNG (stdlib `zlib` only). Punctuation is rendered without wobble because a ±1 px shift genuinely blurs a comma into a period at fixture resolution. |
| 2026-09-28 | `billing_period_match` stored in every pair breakdown (feature, not score component) | Phase 4's tuner needs the one feature that separates recurring-billing false positives from re-invoice duplicates. Base scoring is untouched (demo metrics byte-identical); the breakdown is additive so the Phase 3 UI and stored pairs stay compatible. |
| 2026-09-28 | Tuner: pinned vendor/amount + tax-cap reapplication + prior-anchored learning + global re-scoring (see §7.2) | Validated against the seeded dataset before shipping: unpinned fits collapsed vendor/amount to ≈0 (no variance among reviewed pairs) and leaked cross-vendor pairs into the flag zone (P fell to 0.05 in one offline variant); a pass-slice-only re-rank was rejected because it leaves two score scales in one column, breaking the dashboard and evaluation semantics. Pinned weights + global re-score measured clean: zero label migration out of the pass zone, queue 483 → 0 at identical P/R/F1. |
| 2026-09-28 | `simulate-feedback` command: simulated reviewer answers from seeded GT | The acceptance criterion ("thresholds improve measurably after simulated feedback") needs reproducible reviewer input; a perfect reviewer over the visible queue (flag/review pairs) is the honest upper bound and exercises the exact persistence path the UI uses. |
| 2026-09-28 | `/metrics` defaults its threshold to the active tuned model's flag threshold | After tuning, pair scores live in the tuned model's scale; keeping 0.90 as the implicit default would misreport flagged count/recall. Explicit `?flag_threshold=` still wins; the UI re-inits its slider from the tuning payload. |
