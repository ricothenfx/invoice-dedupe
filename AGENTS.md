# AGENTS.md — Working Agreement for Humans and AI Agents

`invoice-dedupe` detects duplicate invoices to prevent double payments. This file is the
entry point for any person or AI agent continuing development. **Development must rely on
the project documents below, never on prior chat history.**

## Read in this order

1. `docs/PROJECT_CHARTER.md` — mission, agreed decisions, scope, phased roadmap with
   acceptance criteria. This is the source of truth for *what* we are building and *why*.
2. `docs/DESIGN.md` — architecture, algorithms, evaluation methodology, and the decision
   log (ADR-style). Source of truth for *how* it works and *why it is built this way*.
3. `README.md` — quickstart and current measured results.

## Ground rules

- **Language**: all code, docstrings, docs, CLI output, and commit messages are in
  **English**. The product targets a global audience.
- **Docs-first**: any behavior change (weights, thresholds, rules, schema, blocking keys)
  must update `docs/DESIGN.md` (decision log entry) in the same change. Stale docs are
  treated as bugs.
- **Verification**: `pytest` must pass before any change is considered done. If the
  change can affect metrics, re-run `invoice-dedupe demo --n 10000 --seed 42` and update
  the results table in `README.md`. Never report numbers you did not reproduce.
- **Reproducibility**: datasets are generated from explicit seeds. Ground truth exists
  only through `synth.py`; never hand-edit generated JSONL.
- **Phase discipline**: phases and their acceptance criteria live in the charter
  (§Roadmap). Finish a phase's criteria before starting the next. The current phase
  status is recorded at the bottom of the charter.
- **Dependencies**: runtime dependencies stay minimal (`rapidfuzz` for the engine; Phase 2
  added `fastapi`, `uvicorn`, `pdfplumber`, `psycopg`, `python-multipart` for the service
  layer; Phase 3 added **none** — the web app vendors React UMD assets; Phase 4 added
  **none** — the vision-LLM client uses stdlib `urllib` and fixture photos are written
  with stdlib `zlib`, see the decision log). Adding one requires a decision-log entry
  with justification.
- **Honesty over hype**: report precision/recall including false positives and describe
  known limitations. Planted *hard negatives* (recurring invoices) stay in the dataset.

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # setup
pytest                                                       # test suite
invoice-dedupe demo --n 10000 --seed 42                      # e2e: generate+detect+evaluate
invoice-dedupe generate --n 10000 --seed 42 --out datasets   # write dataset + meta.json
invoice-dedupe run --dataset datasets/invoices.jsonl --out datasets/pairs.jsonl
invoice-dedupe evaluate --dataset datasets/invoices.jsonl --pairs datasets/pairs.jsonl
invoice-dedupe init-db                                       # create PostgreSQL schema
invoice-dedupe seed-demo                                     # load synthetic demo data (+ GT) for the web app
invoice-dedupe serve                                         # run the FastAPI service (+ web app at /)
invoice-dedupe worker                                        # run the job worker
invoice-dedupe make-pdf --out sample.pdf                     # sample text-layer invoice PDF
invoice-dedupe make-photo --out photo.png                    # crumpled-invoice photo PNG (vision fixture)
invoice-dedupe simulate-feedback                             # persist GT reviewer decisions (demo input)
invoice-dedupe tune                                          # fit thresholds/weights from decisions (+ --reset)
```

Database connection: `DEDUPE_DATABASE_URL` (default
`postgresql://invoice_dedupe:invoice_dedupe@127.0.0.1:5433/invoice_dedupe`).
Integration tests need `DEDUPE_TEST_DATABASE_URL` pointing at a disposable database and
are skipped when it is unset.

## Layout

```
src/invoice_dedupe/   engine package (normalize, scoring, blocking, engine, synth,
                      evaluate, dataset, cli, models, metrics) + service layer
                      (extraction, pdfgen, db, worker, api, webapp/)
app.py                Vercel serverless entrypoint (exposes the FastAPI app)
vercel.json           serverless function config (maxDuration, excluded files)
tests/                pytest suite (integration tests need DEDUPE_TEST_DATABASE_URL)
docs/                 PROJECT_CHARTER.md, DESIGN.md
datasets/             generated artifacts (git-ignored)
```
