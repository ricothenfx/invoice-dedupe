"""JSONL I/O for datasets and detection results."""
from __future__ import annotations

import json
from pathlib import Path

from .models import Invoice, PairResult


def _write_jsonl(rows: list[dict], path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl(path: str | Path) -> list[dict]:
    with Path(path).open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def save_invoices(invoices: list[Invoice], path: str | Path) -> None:
    _write_jsonl([inv.to_dict() for inv in invoices], path)


def load_invoices(path: str | Path) -> list[Invoice]:
    return [Invoice.from_dict(d) for d in _read_jsonl(path)]


def save_pairs(pairs: list[PairResult], path: str | Path) -> None:
    _write_jsonl([p.to_dict() for p in pairs], path)


def load_pairs(path: str | Path) -> list[PairResult]:
    return [PairResult.from_dict(d) for d in _read_jsonl(path)]
