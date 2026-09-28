"""PostgreSQL persistence: connections, schema, and query helpers.

Tables (Phase 2, charter §5): ``invoices`` (with raw extraction stored
separately from the parsed fields), ``invoice_pairs``, ``review_decisions``
(feeds the Phase 4 feedback loop) and ``jobs`` (the worker queue).

The schema is intentionally managed with plain SQL instead of an ORM: the
data model is small and auditable, and it keeps runtime dependencies minimal.
"""
from __future__ import annotations

import os
import uuid
from datetime import date

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .models import FLAG, PASS, REVIEW, Invoice, PairResult

DEFAULT_DATABASE_URL = "postgresql://invoice_dedupe:invoice_dedupe@127.0.0.1:5433/invoice_dedupe"
DATABASE_URL_ENV = "DEDUPE_DATABASE_URL"

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id                    TEXT PRIMARY KEY,
    source_type           TEXT NOT NULL DEFAULT 'manual',
    source_name           TEXT NOT NULL DEFAULT '',
    raw_text              TEXT NOT NULL DEFAULT '',
    extraction_method     TEXT NOT NULL DEFAULT '',
    extraction_confidence REAL,
    vendor_name           TEXT NOT NULL DEFAULT '',
    invoice_no            TEXT NOT NULL DEFAULT '',
    amount                BIGINT,
    currency              TEXT NOT NULL DEFAULT 'USD',
    invoice_date          DATE,
    tax_id                TEXT NOT NULL DEFAULT '',
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS invoice_pairs (
    id_a       TEXT NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    id_b       TEXT NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    score      REAL NOT NULL,
    label      TEXT NOT NULL,
    breakdown  JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id_a, id_b)
);

CREATE TABLE IF NOT EXISTS review_decisions (
    id_a       TEXT NOT NULL,
    id_b       TEXT NOT NULL,
    decision   TEXT NOT NULL CHECK (decision IN ('duplicate', 'not_duplicate')),
    reviewer   TEXT NOT NULL DEFAULT '',
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id_a, id_b)
);

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL CHECK (type IN ('extract_pdf', 'detect')),
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'done', 'failed')),
    payload     JSONB NOT NULL DEFAULT '{}',
    result      JSONB,
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_invoice_pairs_label ON invoice_pairs (label);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status);
"""


def database_url() -> str:
    """Resolve the database URL from the environment (``DEDUPE_DATABASE_URL``)."""
    return os.environ.get(DATABASE_URL_ENV, DEFAULT_DATABASE_URL)


def connect(url: str | None = None) -> psycopg.Connection:
    """Open a connection with dict rows by default."""
    return psycopg.connect(url or database_url(), row_factory=dict_row)


def init_schema(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
    conn.commit()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# --- invoices ---------------------------------------------------------------


def insert_invoice(
    conn: psycopg.Connection,
    inv: Invoice,
    *,
    source_type: str,
    source_name: str = "",
    raw_text: str = "",
    extraction_method: str = "",
    extraction_confidence: float | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO invoices (id, source_type, source_name, raw_text,
                                  extraction_method, extraction_confidence,
                                  vendor_name, invoice_no, amount, currency,
                                  invoice_date, tax_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                inv.id,
                source_type,
                source_name,
                raw_text,
                extraction_method,
                extraction_confidence,
                inv.vendor_name,
                inv.invoice_no,
                inv.amount,
                inv.currency,
                inv.invoice_date,
                inv.tax_id,
            ),
        )
    conn.commit()


def row_to_invoice(row: dict) -> Invoice:
    return Invoice(
        id=row["id"],
        vendor_name=row["vendor_name"],
        invoice_no=row["invoice_no"],
        amount=int(row["amount"]) if row["amount"] is not None else 0,
        currency=row["currency"],
        invoice_date=row["invoice_date"] if isinstance(row["invoice_date"], date) else None,
        tax_id=row["tax_id"],
    )


def load_invoices(conn: psycopg.Connection) -> list[Invoice]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM invoices ORDER BY created_at, id")
        return [row_to_invoice(r) for r in cur.fetchall()]


def get_invoice(conn: psycopg.Connection, invoice_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM invoices WHERE id = %s", (invoice_id,))
        return cur.fetchone()


def list_invoices(conn: psycopg.Connection, limit: int = 100, offset: int = 0) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, source_type, source_name, extraction_method, extraction_confidence,"
            " vendor_name, invoice_no, amount, currency, invoice_date, tax_id, created_at"
            " FROM invoices ORDER BY created_at, id LIMIT %s OFFSET %s",
            (limit, offset),
        )
        return cur.fetchall()


# --- pairs ------------------------------------------------------------------


def replace_pairs(conn: psycopg.Connection, pairs: list[PairResult]) -> None:
    """Atomically replace all stored pairs with a fresh detection run."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM invoice_pairs")
        if pairs:
            cur.executemany(
                """
                INSERT INTO invoice_pairs (id_a, id_b, score, label, breakdown)
                VALUES (%s, %s, %s, %s, %s)
                """,
                [
                    (p.id_a, p.id_b, float(p.score), p.label, Jsonb(p.breakdown))
                    for p in pairs
                ],
            )
    conn.commit()


def list_pairs(
    conn: psycopg.Connection,
    label: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    if label and label not in (FLAG, REVIEW, PASS):
        raise ValueError(f"invalid label: {label!r}")
    query = (
        "SELECT id_a, id_b, score, label, breakdown, created_at"
        " FROM invoice_pairs"
    )
    params: list = []
    if label:
        query += " WHERE label = %s"
        params.append(label)
    query += " ORDER BY score DESC, id_a, id_b LIMIT %s OFFSET %s"
    params += [limit, offset]
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    for row in rows:
        row["score"] = round(float(row["score"]), 4)
    return rows


def pair_exists(conn: psycopg.Connection, id_a: str, id_b: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM invoice_pairs WHERE (id_a, id_b) IN ((%s, %s), (%s, %s))",
            (id_a, id_b, id_b, id_a),
        )
        return cur.fetchone() is not None


# --- review decisions ---------------------------------------------------------


def upsert_review_decision(
    conn: psycopg.Connection,
    id_a: str,
    id_b: str,
    decision: str,
    reviewer: str = "",
) -> dict:
    if decision not in ("duplicate", "not_duplicate"):
        raise ValueError(f"invalid decision: {decision!r}")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO review_decisions (id_a, id_b, decision, reviewer)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (id_a, id_b)
            DO UPDATE SET decision = EXCLUDED.decision,
                          reviewer = EXCLUDED.reviewer,
                          decided_at = now()
            RETURNING id_a, id_b, decision, reviewer, decided_at
            """,
            (id_a, id_b, decision, reviewer),
        )
        row = cur.fetchone()
    conn.commit()
    return row


# --- jobs ---------------------------------------------------------------------


def create_job(conn: psycopg.Connection, job_type: str, payload: dict) -> str:
    job_id = new_id("job")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO jobs (id, type, payload) VALUES (%s, %s, %s)",
            (job_id, job_type, Jsonb(payload)),
        )
    conn.commit()
    return job_id


def get_job(conn: psycopg.Connection, job_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
        return cur.fetchone()


def claim_job(conn: psycopg.Connection) -> dict | None:
    """Claim the oldest pending job using SKIP LOCKED (safe for several workers)."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'pending'
                ORDER BY created_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """
            )
            job = cur.fetchone()
            if job is None:
                return None
            cur.execute(
                "UPDATE jobs SET status = 'running', started_at = now() WHERE id = %s",
                (job["id"],),
            )
    return job


def finish_job(
    conn: psycopg.Connection,
    job_id: str,
    result: dict | None = None,
    error: str | None = None,
) -> None:
    status = "failed" if error else "done"
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE jobs
            SET status = %s, result = %s, error = %s, finished_at = now()
            WHERE id = %s
            """,
            (status, Jsonb(result) if result is not None else None, error, job_id),
        )
    conn.commit()
