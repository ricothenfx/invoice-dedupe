"""FastAPI service: PDF upload, job status, invoices, pairs, review decisions.

Endpoints (Phase 2, charter §5):
  GET  /health                          liveness + database check
  POST /invoices/pdf                    upload a text-layer PDF -> extraction job
  GET  /jobs/{job_id}                   job status/result
  GET  /invoices                        list invoices (parsed fields, no raw text)
  GET  /invoices/{invoice_id}           single invoice incl. raw extraction text
  GET  /pairs?label=&undecided=         detection results, enriched for the
                                        review queue (both invoices + decision)
  POST /pairs/{id_a}/{id_b}/decision    persist a reviewer decision (Phase 4 input)

Endpoints (Phase 3, charter §5):
  GET  /                                review-queue web app (React SPA)
  GET  /static/...                      web app assets (vendored React, no CDN)
  GET  /metrics?flag_threshold=         dashboard: counts, threshold-dependent
                                        precision/exposure, score distribution

Uploads are asynchronous: the API only validates and enqueues; workers do the
extraction and detection (see worker.py).
"""
from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import psycopg
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, metrics

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
PDF_MAGIC = b"%PDF-"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"
WEBAPP_DIR = Path(__file__).resolve().parent / "webapp"


@asynccontextmanager
async def lifespan(app: FastAPI):
    with db.connect() as conn:
        db.init_schema(conn)
    yield


app = FastAPI(
    title="invoice-dedupe",
    description="Detect duplicate invoices to prevent double payments",
    version="0.3.0",
    lifespan=lifespan,
)


class DecisionInput(BaseModel):
    decision: Literal["duplicate", "not_duplicate"]
    reviewer: str = ""


@app.exception_handler(psycopg.OperationalError)
async def _db_unavailable(request, exc):
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


def _conn() -> psycopg.Connection:
    return db.connect()


@app.get("/health")
def health() -> dict:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return {"status": "ok", "database": "ok"}
    except psycopg.Error:
        return JSONResponse(status_code=503, content={"status": "degraded", "database": "unavailable"})


@app.post("/invoices/pdf", status_code=202)
async def upload_pdf(
    file: UploadFile = File(...),
) -> dict:
    data = await file.read()
    if not data.startswith(PDF_MAGIC):
        raise HTTPException(status_code=400, detail="not a PDF file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="PDF exceeds the 10 MiB upload limit")

    payload = {"data": base64.b64encode(data).decode("ascii"), "name": file.filename or ""}
    with _conn() as conn:
        job_id = db.create_job(conn, "extract_pdf", payload)
    return {"job_id": job_id, "status": "pending"}


@app.post("/invoices/photo", status_code=202)
async def upload_photo(
    file: UploadFile = File(...),
) -> dict:
    """Upload a scan/photo of an invoice (PNG or JPEG) for vision-LLM extraction.

    The vision layer runs in mock mode unless a real endpoint is configured
    via ``DEDUPE_VISION_LLM_URL`` (charter decision D6). Asynchronous like
    the PDF path: the API validates and enqueues, the worker extracts.
    """
    data = await file.read()
    content_type = file.content_type or ""
    if content_type not in ("image/png", "image/jpeg") or (
        not data.startswith(PNG_MAGIC) and not data.startswith(JPEG_MAGIC)
    ):
        raise HTTPException(status_code=400, detail="not a PNG or JPEG image")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="image exceeds the 10 MiB upload limit")

    payload = {"data": base64.b64encode(data).decode("ascii"), "name": file.filename or ""}
    with _conn() as conn:
        job_id = db.create_job(conn, "extract_image", payload)
    return {"job_id": job_id, "status": "pending"}


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    with _conn() as conn:
        job = db.get_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    payload = {k: v for k, v in (job["payload"] or {}).items() if k != "data"}
    return {**job, "payload": payload}


@app.get("/invoices")
def list_invoices(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    with _conn() as conn:
        return db.list_invoices(conn, limit=limit, offset=offset)


@app.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: str) -> dict:
    with _conn() as conn:
        row = db.get_invoice(conn, invoice_id)
    if row is None:
        raise HTTPException(status_code=404, detail="invoice not found")
    return row


@app.get("/pairs")
def list_pairs(
    label: Literal["flag", "review", "pass"] | None = None,
    undecided: bool = False,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    with _conn() as conn:
        return db.list_pairs_detailed(
            conn, label=label, undecided=undecided, limit=limit, offset=offset
        )


@app.get("/metrics")
def get_metrics(
    flag_threshold: float | None = Query(None, ge=0.0, le=1.0),
) -> dict:
    """Dashboard payload: totals plus everything the threshold slider needs.

    ``distribution`` carries one row per scored pair (score, GT flag, exposure,
    decision), so the UI can recompute live numbers for any slider position
    client-side; ``at_threshold`` is the server-computed source of truth at the
    requested threshold (covered by tests). When a tuned model is active, pair
    scores live in the tuned model's scale, so an omitted threshold defaults to
    the tuned flag threshold; without tuning it defaults to 0.90.
    """
    with _conn() as conn:
        n_invoices = db.count_invoices(conn)
        n_gt_pairs = db.count_gt_pairs(conn)
        label_counts = db.label_counts(conn)
        decisions = db.decision_counts(conn)
        distribution = db.scored_distribution(conn)
        active_tuning = db.get_active_tuning(conn)
    tuning_info = None
    if active_tuning is not None:
        tuning_info = {
            "model_id": active_tuning["id"],
            "flag_threshold": active_tuning["flag_threshold"],
            "review_threshold": active_tuning["review_threshold"],
            "n_decisions": active_tuning["n_decisions"],
            "created_at": active_tuning["created_at"].isoformat(),
        }
    if flag_threshold is None:
        flag_threshold = tuning_info["flag_threshold"] if tuning_info else 0.90
    return {
        "n_invoices": n_invoices,
        "n_gt_pairs": n_gt_pairs,
        "label_counts": label_counts,
        "decisions": decisions,
        "tuning": tuning_info,
        "flag_threshold": flag_threshold,
        "at_threshold": metrics.threshold_metrics(
            distribution, flag_threshold, n_gt_pairs
        ),
        "distribution": distribution,
    }


@app.post("/pairs/{id_a}/{id_b}/decision")
def decide_pair(id_a: str, id_b: str, body: DecisionInput) -> dict:
    with _conn() as conn:
        if not db.pair_exists(conn, id_a, id_b):
            raise HTTPException(status_code=404, detail="pair not found")
        return db.upsert_review_decision(conn, id_a, id_b, body.decision, body.reviewer)


# --- web app (Phase 3) --------------------------------------------------------
# Served last so API routes keep precedence; assets are vendored (React UMD
# production builds) so the demo runs fully offline, no CDN.


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEBAPP_DIR / "index.html")


app.mount(
    "/static",
    StaticFiles(directory=WEBAPP_DIR),
    name="static",
)
