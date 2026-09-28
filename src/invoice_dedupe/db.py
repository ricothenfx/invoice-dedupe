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

-- Ground-truth columns (nullable): populated by `invoice-dedupe seed-demo` so
-- the dashboard can measure precision/recall at arbitrary thresholds. Regular
-- ingestion (PDF upload) leaves them NULL.
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS duplicate_of TEXT;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS variant TEXT;
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
                                  invoice_date, tax_id, duplicate_of, variant)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                inv.duplicate_of,
                inv.variant,
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
        duplicate_of=row.get("duplicate_of"),
        variant=row.get("variant"),
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
            " vendor_name, invoice_no, amount, currency, invoice_date, tax_id,"
            " duplicate_of, variant, created_at"
            " FROM invoices ORDER BY created_at, id LIMIT %s OFFSET %s",
            (limit, offset),
        )
        return cur.fetchall()


def count_invoices(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM invoices")
        return int(cur.fetchone()["n"])


def truncate_all(conn: psycopg.Connection) -> None:
    """Remove all demo/application data (used by `seed-demo --reset`)."""
    with conn.cursor() as cur:
        cur.execute("TRUNCATE invoices, invoice_pairs, review_decisions, jobs CASCADE")
    conn.commit()


def insert_invoices_bulk(
    conn: psycopg.Connection,
    invoices: list[Invoice],
    *,
    source_type: str,
    source_name: str = "",
    extraction_method: str = "",
    extraction_confidence: float | None = None,
) -> int:
    """Fast bulk insert used by `seed-demo`; GT columns come from the Invoice."""
    rows = [
        (
            inv.id,
            source_type,
            source_name,
            "",  # raw_text: synthetic invoices have no source document
            extraction_method,
            extraction_confidence,
            inv.vendor_name,
            inv.invoice_no,
            inv.amount,
            inv.currency,
            inv.invoice_date,
            inv.tax_id,
            inv.duplicate_of,
            inv.variant,
        )
        for inv in invoices
    ]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO invoices (id, source_type, source_name, raw_text,
                                  extraction_method, extraction_confidence,
                                  vendor_name, invoice_no, amount, currency,
                                  invoice_date, tax_id, duplicate_of, variant)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            rows,
        )
    conn.commit()
    return len(rows)


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


_PAIR_QUERY = """
    SELECT p.id_a, p.id_b, p.score, p.label, p.breakdown, p.created_at,
           ia.id AS a_id, ia.vendor_name AS a_vendor_name, ia.invoice_no AS a_invoice_no,
           ia.amount AS a_amount, ia.currency AS a_currency, ia.invoice_date AS a_invoice_date,
           ia.tax_id AS a_tax_id, ia.source_type AS a_source_type,
           ia.source_name AS a_source_name, ia.extraction_confidence AS a_extraction_confidence,
           ia.duplicate_of AS a_duplicate_of, ia.variant AS a_variant,
           ib.id AS b_id, ib.vendor_name AS b_vendor_name, ib.invoice_no AS b_invoice_no,
           ib.amount AS b_amount, ib.currency AS b_currency, ib.invoice_date AS b_invoice_date,
           ib.tax_id AS b_tax_id, ib.source_type AS b_source_type,
           ib.source_name AS b_source_name, ib.extraction_confidence AS b_extraction_confidence,
           ib.duplicate_of AS b_duplicate_of, ib.variant AS b_variant,
           d.decision
    FROM invoice_pairs p
    JOIN invoices ia ON ia.id = p.id_a
    JOIN invoices ib ON ib.id = p.id_b
    LEFT JOIN review_decisions d
      ON (d.id_a = p.id_a AND d.id_b = p.id_b) OR (d.id_a = p.id_b AND d.id_b = p.id_a)
"""


def _invoice_summary(row: dict, prefix: str) -> dict:
    keys = [
        "id", "vendor_name", "invoice_no", "amount", "currency", "invoice_date",
        "tax_id", "source_type", "source_name", "extraction_confidence",
        "duplicate_of", "variant",
    ]
    return {k: row[f"{prefix}_{k}"] for k in keys}


def list_pairs_detailed(
    conn: psycopg.Connection,
    label: str | None = None,
    undecided: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Pairs joined with both invoices' summary fields plus the review decision.

    This is the payload behind the web app's review queue: the UI needs the
    compared fields and the per-field `breakdown` without N+1 extra requests.
    """
    if label and label not in (FLAG, REVIEW, PASS):
        raise ValueError(f"invalid label: {label!r}")
    query, conditions, params = _PAIR_QUERY, [], []
    if label:
        conditions.append("p.label = %s")
        params.append(label)
    if undecided:
        conditions.append("d.decision IS NULL")
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY p.score DESC, p.id_a, p.id_b LIMIT %s OFFSET %s"
    params += [limit, offset]
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    out = []
    for row in rows:
        out.append(
            {
                "id_a": row["id_a"],
                "id_b": row["id_b"],
                "score": round(float(row["score"]), 4),
                "label": row["label"],
                "breakdown": row["breakdown"],
                "created_at": row["created_at"],
                "decision": row["decision"],
                "a": _invoice_summary(row, "a"),
                "b": _invoice_summary(row, "b"),
            }
        )
    return out


def label_counts(conn: psycopg.Connection) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT label, count(*) AS n FROM invoice_pairs GROUP BY label")
        return {r["label"]: int(r["n"]) for r in cur.fetchall()}


def decision_counts(conn: psycopg.Connection) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT decision, count(*) AS n FROM review_decisions GROUP BY decision")
        return {r["decision"]: int(r["n"]) for r in cur.fetchall()}


def count_gt_pairs(conn: psycopg.Connection) -> int | None:
    """Number of known duplicate pairs, or None when no ground truth is loaded."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM invoices WHERE duplicate_of IS NOT NULL")
        n = int(cur.fetchone()["n"])
    return n or None


def scored_distribution(conn: psycopg.Connection) -> list[dict]:
    """Minimal per-pair rows for the dashboard: score, GT flag, exposure, decision.

    `gt` marks pairs whose duplicate relation is confirmed by the seeded ground
    truth; `value` is the double-payment exposure of the pair (the larger of the
    two amounts — near-duplicates bill nearly the same amount).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT round(p.score::numeric, 6) AS score,
                   (ia.duplicate_of = p.id_b OR ib.duplicate_of = p.id_a) AS gt,
                   GREATEST(COALESCE(ia.amount, 0), COALESCE(ib.amount, 0)) AS value,
                   d.decision
            FROM invoice_pairs p
            JOIN invoices ia ON ia.id = p.id_a
            JOIN invoices ib ON ib.id = p.id_b
            LEFT JOIN review_decisions d
              ON (d.id_a = p.id_a AND d.id_b = p.id_b)
              OR (d.id_a = p.id_b AND d.id_b = p.id_a)
            """
        )
        return [
            {
                "score": float(r["score"]),
                "gt": bool(r["gt"]),
                "value": int(r["value"]),
                "decision": r["decision"],
            }
            for r in cur.fetchall()
        ]


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
