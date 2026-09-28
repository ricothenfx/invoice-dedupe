"""API + worker integration tests against a live PostgreSQL database.

Skipped unless ``DEDUPE_TEST_DATABASE_URL`` points at a disposable database,
e.g.::

    DEDUPE_TEST_DATABASE_URL=postgresql://invoice_dedupe:invoice_dedupe@127.0.0.1:5433/invoice_dedupe_test
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from invoice_dedupe import db, pdfgen
from invoice_dedupe import worker as worker_mod
from invoice_dedupe.api import app

TEST_URL = os.environ.get("DEDUPE_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_URL, reason="DEDUPE_TEST_DATABASE_URL not set; database tests skipped"
)

INVOICE_A = [
    "Vendor: Northwind Logistics, Ltd.",
    "Invoice No: INV-2026-0001",
    "Invoice Date: 2026-09-14",
    "Amount Due: USD 12,500.00",
    "Tax ID: 12-3456789",
]
# Same transaction, resubmitted with re-formatted number and vendor spelling.
INVOICE_B = [
    "Vendor: NORTHWIND LOGISTICS LTD",
    "Invoice No: INV/2026/0001",
    "Invoice Date: 2026-09-14",
    "Amount Due: USD 12,500.00",
    "Tax ID: 12-3456789",
]


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv(db.DATABASE_URL_ENV, TEST_URL)
    with db.connect(TEST_URL) as conn:
        db.init_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE invoices, invoice_pairs, review_decisions, jobs CASCADE")
        conn.commit()
    with TestClient(app) as test_client:
        yield test_client


def _drain_queue() -> None:
    with db.connect(TEST_URL) as conn:
        worker_mod.process_all_pending(conn)


def test_health(client):
    body = client.get("/health").json()
    assert body == {"status": "ok", "database": "ok"}


def test_upload_rejects_non_pdf(client):
    resp = client.post("/invoices/pdf", files={"file": ("x.txt", b"hello", "text/plain")})
    assert resp.status_code == 400


def test_upload_pdf_detects_and_persists_duplicate(client):
    """Acceptance criterion: upload text-layer PDFs -> duplicates detected and persisted."""
    r_a = client.post("/invoices/pdf", files={"file": ("a.pdf", pdfgen.make_pdf(INVOICE_A), "application/pdf")})
    r_b = client.post("/invoices/pdf", files={"file": ("b.pdf", pdfgen.make_pdf(INVOICE_B), "application/pdf")})
    assert r_a.status_code == r_b.status_code == 202
    job_a, job_b = r_a.json()["job_id"], r_b.json()["job_id"]

    _drain_queue()

    for job_id in (job_a, job_b):
        job = client.get(f"/jobs/{job_id}").json()
        assert job["status"] == "done", job
        assert job["result"]["confidence"] == 1.0
        assert "data" not in job["payload"]

    invoices = client.get("/invoices").json()
    assert len(invoices) == 2
    assert {inv["extraction_method"] for inv in invoices} == {"pdfplumber"}

    flagged = client.get("/pairs", params={"label": "flag"}).json()
    pair = {(p["id_a"], p["id_b"]) for p in flagged}
    ids = {inv["id"] for inv in invoices}
    assert any(a in ids and b in ids for a, b in pair)
    assert all(p["score"] >= 0.90 for p in flagged)

    detail = client.get(f"/invoices/{invoices[0]['id']}").json()
    assert "Invoice No:" in detail["raw_text"]


def test_worker_marks_bad_pdf_as_failed(client):
    resp = client.post("/invoices/pdf", files={"file": ("bad.pdf", b"%PDF-1.4 broken", "application/pdf")})
    job_id = resp.json()["job_id"]
    _drain_queue()
    job = client.get(f"/jobs/{job_id}").json()
    assert job["status"] == "failed"
    assert job["error"]


def test_review_decision_requires_existing_pair(client):
    resp = client.post("/pairs/inv_x/inv_y/decision", json={"decision": "duplicate"})
    assert resp.status_code == 404


def test_review_decision_persisted(client):
    client.post("/invoices/pdf", files={"file": ("a.pdf", pdfgen.make_pdf(INVOICE_A), "application/pdf")})
    client.post("/invoices/pdf", files={"file": ("b.pdf", pdfgen.make_pdf(INVOICE_B), "application/pdf")})
    _drain_queue()

    pairs = client.get("/pairs").json()
    assert pairs
    id_a, id_b = pairs[0]["id_a"], pairs[0]["id_b"]

    resp = client.post(
        f"/pairs/{id_a}/{id_b}/decision",
        json={"decision": "not_duplicate", "reviewer": "ap-team"},
    )
    assert resp.status_code == 200
    assert resp.json()["decision"] == "not_duplicate"

    resp = client.post(f"/pairs/{id_a}/{id_b}/decision", json={"decision": "duplicate"})
    assert resp.json()["decision"] == "duplicate"


def test_pair_ordering_and_labels(client):
    client.post("/invoices/pdf", files={"file": ("a.pdf", pdfgen.make_pdf(INVOICE_A), "application/pdf")})
    _drain_queue()
    assert client.get("/pairs").json() == []
    invoices = client.get("/invoices").json()
    assert len(invoices) == 1
    assert invoices[0]["source_type"] == "pdf"
