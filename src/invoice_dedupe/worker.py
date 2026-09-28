"""Worker: processes extraction and detection jobs from the queue.

The queue is a PostgreSQL table claimed with ``FOR UPDATE SKIP LOCKED``: this
keeps exactly-once processing semantics with several workers while avoiding an
extra message-broker dependency (see DESIGN.md decision log). Job payloads are
small (base64-encoded PDFs, capped by the API), so the database doubles as the
message store.
"""
from __future__ import annotations

import base64
import time

import psycopg

from . import db, extraction
from .engine import detect
from .models import PASS


def handle_extract_pdf(conn: psycopg.Connection, payload: dict) -> dict:
    data = base64.b64decode(payload["data"])
    result = extraction.extract_pdf(data)
    invoice_id = db.new_id("inv")
    invoice = extraction.result_to_invoice(result, invoice_id)
    db.insert_invoice(
        conn,
        invoice,
        source_type="pdf",
        source_name=payload.get("name", ""),
        raw_text=result.raw_text,
        extraction_method="pdfplumber",
        extraction_confidence=result.confidence,
    )
    detect_job = db.create_job(conn, "detect", {"trigger": invoice_id})
    return {
        "invoice_id": invoice_id,
        "confidence": result.confidence,
        "missing": result.missing,
        "detect_job_id": detect_job,
    }


def handle_detect(conn: psycopg.Connection, payload: dict) -> dict:
    invoices = db.load_invoices(conn)
    result = detect(invoices)
    db.replace_pairs(conn, result.pairs)
    return {
        "n_invoices": len(invoices),
        "n_pairs": len(result.pairs),
        "flagged": result.stats.get("flagged", 0),
        "review": result.stats.get("review", 0),
        "pass": sum(1 for p in result.pairs if p.label == PASS),
    }


_HANDLERS = {
    "extract_pdf": handle_extract_pdf,
    "detect": handle_detect,
}


def process_one(conn: psycopg.Connection) -> str | None:
    """Claim and process a single job; return the job id, or None if idle."""
    job = db.claim_job(conn)
    if job is None:
        return None
    handler = _HANDLERS.get(job["type"])
    if handler is None:
        db.finish_job(conn, job["id"], error=f"unknown job type: {job['type']}")
        return job["id"]
    try:
        result = handler(conn, job["payload"])
    except Exception as exc:  # noqa: BLE001 - worker must survive bad jobs
        db.finish_job(conn, job["id"], error=f"{type(exc).__name__}: {exc}")
    else:
        db.finish_job(conn, job["id"], result=result)
    return job["id"]


def process_all_pending(conn: psycopg.Connection, limit: int = 10_000) -> int:
    """Drain the queue (used by tests and by ``run_worker``'s inner loop)."""
    processed = 0
    while processed < limit and process_one(conn) is not None:
        processed += 1
    return processed


def run_worker(conn: psycopg.Connection, poll_seconds: float = 1.0) -> None:
    """Run the worker loop until interrupted."""
    print(f"worker: polling queue every {poll_seconds:g}s (Ctrl+C to stop)")
    idle = False
    try:
        while True:
            if process_one(conn) is None:
                if not idle:
                    print("worker: queue empty, waiting for jobs")
                    idle = True
                time.sleep(poll_seconds)
            else:
                idle = False
    except KeyboardInterrupt:
        print("worker: stopped")
