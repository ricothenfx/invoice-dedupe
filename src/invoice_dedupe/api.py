"""FastAPI service: PDF upload, job status, invoices, pairs, review decisions.

Endpoints (Phase 2, charter §5):
  GET  /health                          liveness + database check
  POST /invoices/pdf                    upload a text-layer PDF -> extraction job
  GET  /jobs/{job_id}                   job status/result
  GET  /invoices                        list invoices (parsed fields, no raw text)
  GET  /invoices/{invoice_id}           single invoice incl. raw extraction text
  GET  /pairs?label=flag|review|pass    detection results
  POST /pairs/{id_a}/{id_b}/decision    persist a reviewer decision (Phase 4 input)

Uploads are asynchronous: the API only validates and enqueues; workers do the
extraction and detection (see worker.py).
"""
from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from typing import Literal

import psycopg
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import db

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
PDF_MAGIC = b"%PDF-"


@asynccontextmanager
async def lifespan(app: FastAPI):
    with db.connect() as conn:
        db.init_schema(conn)
    yield


app = FastAPI(
    title="invoice-dedupe",
    description="Detect duplicate invoices to prevent double payments",
    version="0.2.0",
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
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    with _conn() as conn:
        return db.list_pairs(conn, label=label, limit=limit, offset=offset)


@app.post("/pairs/{id_a}/{id_b}/decision")
def decide_pair(id_a: str, id_b: str, body: DecisionInput) -> dict:
    with _conn() as conn:
        if not db.pair_exists(conn, id_a, id_b):
            raise HTTPException(status_code=404, detail="pair not found")
        return db.upsert_review_decision(conn, id_a, id_b, body.decision, body.reviewer)
