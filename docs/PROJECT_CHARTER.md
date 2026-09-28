# PROJECT_CHARTER — invoice-dedupe

Status: **Phase 2 complete** (last updated 2026-09-28).
This charter is the binding agreement for the project's direction. Future work — by the
owner or by AI agents — must conform to it and must not depend on prior chat history.

## 1. Mission

Prevent companies from paying the same invoice twice. Duplicates usually do not look
identical: the vendor resubmits with a new invoice number, formats differ, vendor names
vary, or OCR garbles characters. Manual AP review misses these at scale. We build a
system that catches them automatically and routes the ambiguous cases to a human review
queue.

## 2. Product decisions (agreed 2026-09-28)

| # | Decision | Choice | Rationale |
|---|---|---|---|
| D1 | Target input for MVP+ | **PDF and scanned photos** | Highest demo impact; achieved pragmatically (see D6), not via a custom OCR pipeline |
| D2 | Primary language/stack | **Python** | `rapidfuzz` is best-in-class for fuzzy matching; strong data ecosystem |
| D3 | Primary deliverable | **Interactive web app** | Review-queue UX is the most presentable artifact |
| D4 | Advanced feature in scope | **Feedback loop & threshold tuning** | Reviewer decisions tune thresholds/weights; large differentiator |
| D5 | Audience | **Global** | English everywhere; multi-jurisdiction vendor names and amount formats |
| D6 | Extraction strategy | **Layered: pdfplumber first, vision-LLM API as fallback** | Avoids weeks of custom OCR; LLM only for text-less PDFs/scans; deterministic path stays primary |

## 3. Scope

**In scope (eventually)**
- Layered duplicate detection: exact-key, fuzzy scoring, contextual rules, hard rules
- Synthetic dataset generator with labeled duplicate variants (evaluation ground truth)
- REST API + worker pipeline (Phase 2), review-queue web app (Phase 3),
  vision-LLM extraction fallback + feedback loop (Phase 4)

**Out of scope (for now)**
- Payment execution, ERP integrations, multi-currency conversion, credit-note matching
- Custom OCR/layout-analysis engine (we use pdfplumber + vision LLM instead)
- Real customer data

## 4. Architecture target

```
Phase 1 (DONE): pure-Python engine + synthetic data + CLI evaluation
Phase 2 (DONE): FastAPI service + PDF ingestion (pdfplumber) + PostgreSQL + worker queue
Phase 3:        React web app: review queue with per-field score breakdown + dashboard
Phase 4:        vision-LLM extraction fallback + feedback loop (threshold/weight tuning)
```

The layered-extraction pipeline (Phase 2+): if a PDF has a text layer → pdfplumber;
otherwise → vision-LLM with a JSON extraction schema, validated, with an extraction-
confidence field and a mock mode for offline demos/CI. Raw extraction and normalized
fields are stored separately so prompts can improve without invalidating audit trails.

## 5. Roadmap with acceptance criteria

### Phase 1 — Engine + evaluation (DONE, 2026-09-28)
- [x] Canonical normalization per field (number, vendor, amount, tax ID, billing period)
- [x] Weighted fuzzy scoring with contextual rules and hard rules
- [x] Blocking-based candidate generation (no O(N²))
- [x] Synthetic dataset generator with 5 duplicate variant classes + recurring hard negatives
- [x] Evaluation: precision/recall/F1, threshold sweep, per-variant recall
- [x] CLI (`generate` / `run` / `evaluate` / `demo`) + test suite green
- [x] Results: precision 0.9852, recall 1.0000, F1 0.9926 @ 10,200 invoices, 99.99%
      blocking reduction, ~1.3 s

### Phase 2 — API + PDF ingestion (DONE, 2026-09-28)
- [x] FastAPI service exposing upload + detection + pair results
- [x] PDF ingestion with pdfplumber (text layer); store raw extraction separately
- [x] PostgreSQL persistence (invoices, pairs, review decisions)
- [x] Worker queue for extraction/detection jobs
- [x] Acceptance: upload a text-layer PDF via API → duplicates detected and persisted;
      evaluation pipeline still reproducible via seed

### Phase 3 — Interactive web app  *(this phase makes the project presentable)*
- [ ] Upload UI + invoice list
- [ ] Review queue showing pair comparisons with per-field score breakdown
- [ ] Dashboard with threshold slider + live estimated precision + business metrics
- [ ] Acceptance: a reviewer can triage 100 flagged pairs in minutes; demo runs offline

### Phase 4 — Wow factor + learning loop
- [ ] Vision-LLM extraction fallback for scans/photos (validated JSON schema, mock mode)
- [ ] Feedback loop: review decisions drive threshold tuning; weight adjustment via
      simple logistic regression over per-field breakdowns
- [ ] Acceptance: uploading a phone photo of a crumpled invoice yields structured fields
      and a duplicate verdict; thresholds improve measurably after simulated feedback

## 6. Known risks and standing mitigations

| Risk | Mitigation |
|---|---|
| False positives block legitimate payments (monthly recurring invoices) | Planted as hard negatives in the dataset; `billing_period` field (Phase 2); feedback loop (Phase 4) |
| Threshold mis-tuning | Review queue + estimated-precision curve; sweep tooling already in place |
| LLM API cost/availability (Phase 4) | pdfplumber remains the primary path; mock mode for CI/offline demos |
| No public ground-truth data | Synthetic generator with seeds is itself a documented feature |
| Evaluation overfit to generator quirks | Decision log records generator changes; FP tail is reported honestly |

## 7. Current status log

- **2026-09-28** — Phase 2 complete. FastAPI service (`api.py`), pdfplumber text-layer
  extraction with per-invoice confidence (`extraction.py`), PostgreSQL persistence
  (`db.py`: invoices / invoice_pairs / review_decisions / jobs), worker queue via
  `FOR UPDATE SKIP LOCKED` (`worker.py`); new CLI commands `init-db`, `serve`, `worker`,
  `make-pdf`. Acceptance verified live: two text-layer PDFs uploaded over HTTP →
  extraction + chained detection jobs done → duplicate pair persisted (score 1.0, label
  `flag`); 42 tests green (15 new); demo re-run at seed 42 reproduces P 0.9852 /
  R 1.0000 / F1 0.9926 unchanged (engine untouched). New runtime deps justified in the
  DESIGN.md decision log. Environment note: `python3-venv` was unavailable, so the venv
  was bootstrapped with `--without-pip` + get-pip.py (no project code impact).
- **2026-09-28** — Phase 1 complete. Internationalized (English docs/CLI/data), decision
  log established, 27 tests green. Metrics reproduced at seed 42: P 0.9852 / R 1.0000 /
  F1 0.9926; $7.36M duplicate value caught; 3 FP all of the "same vendor+amount ≤14d"
  re-invoice pattern (ambiguous by design, routed to review in real deployments).
