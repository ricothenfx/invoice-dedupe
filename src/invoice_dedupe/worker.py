"""Worker: processes extraction and detection jobs from the queue.

The queue is a PostgreSQL table claimed with ``FOR UPDATE SKIP LOCKED``: this
keeps exactly-once processing semantics with several workers while avoiding an
extra message-broker dependency (see DESIGN.md decision log). Job payloads are
small (base64-encoded documents, capped by the API), so the database doubles
as the message store.

Extraction routing follows charter decision D6: PDFs with a text layer go to
pdfplumber; text-less PDFs and images go to the vision-LLM layer (mock mode
by default, real API opt-in via environment).
"""
from __future__ import annotations

import base64
import time

import psycopg

from . import db, extraction, vision
from .engine import detect
from .models import PASS


def _vision_method() -> str:
    return "vision_llm_api" if vision.vision_mode() == "api" else "vision_llm_mock"


def _extract_dispatch(data: bytes) -> tuple[extraction.ExtractionResult, str, str]:
    """Route a document to the right extractor; return (result, method, source_type)."""
    if data.startswith(b"%PDF-"):
        result = extraction.extract_pdf(data)
        if result.raw_text.strip():
            return result, "pdfplumber", "pdf"
        return vision.extract_image(data, "application/pdf"), _vision_method(), "pdf"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return vision.extract_image(data, "image/png"), _vision_method(), "image"
    if data[:3] == b"\xff\xd8\xff":
        return vision.extract_image(data, "image/jpeg"), _vision_method(), "image"
    raise ValueError("unsupported document type (not a PDF, PNG, or JPEG)")


def handle_extract_document(conn: psycopg.Connection, payload: dict) -> dict:
    data = base64.b64decode(payload["data"])
    result, method, source_type = _extract_dispatch(data)
    invoice_id = db.new_id("inv")
    invoice = extraction.result_to_invoice(result, invoice_id)
    db.insert_invoice(
        conn,
        invoice,
        source_type=source_type,
        source_name=payload.get("name", ""),
        raw_text=result.raw_text,
        extraction_method=method,
        extraction_confidence=result.confidence,
    )
    detect_job = db.create_job(conn, "detect", {"trigger": invoice_id})
    return {
        "invoice_id": invoice_id,
        "extraction_method": method,
        "confidence": result.confidence,
        "missing": result.missing,
        "detect_job_id": detect_job,
    }


def handle_detect(conn: psycopg.Connection, payload: dict) -> dict:
    invoices = db.load_invoices(conn)
    result = detect(invoices)
    pairs = result.pairs
    active = db.get_active_tuning(conn)
    if active is not None:
        from .normalize import normalize_invoice
        from .tuning import TunedModel, rescore_pairs

        periods = {inv.id: normalize_invoice(inv).billing_period for inv in invoices}
        model = TunedModel(
            weights=dict(active["weights"]),
            bias=active["bias"],
            flag_threshold=active["flag_threshold"],
            review_threshold=active["review_threshold"],
            n_decisions=active["n_decisions"],
            model_id=active["id"],
        )
        pairs = rescore_pairs(pairs, model, periods)
    db.replace_pairs(conn, pairs)
    counts = {label: sum(1 for p in pairs if p.label == label) for label in ("flag", "review", PASS)}
    return {
        "n_invoices": len(invoices),
        "n_pairs": len(pairs),
        "tuned": active["id"] if active else None,
        **counts,
    }


_HANDLERS = {
    "extract_pdf": handle_extract_document,
    "extract_image": handle_extract_document,
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
