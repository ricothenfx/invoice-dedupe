"""Candidate generation via blocking keys — avoids O(N^2) pairwise comparison."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from itertools import combinations

from .models import NormalizedInvoice

MAX_BLOCK_SIZE = 1000


def blocking_keys(inv: NormalizedInvoice) -> list[str]:
    """Blocking keys: a pair is compared only when it shares at least one key.

    vn  : (vendor_key, invoice_no_norm)  -> format / look-alike-character duplicates
    amt : (amount)                        -> re-invoice, vendor-variant, date-shift dups
    """
    keys = []
    if inv.vendor_key and inv.invoice_no:
        keys.append(f"vn|{inv.vendor_key}|{inv.invoice_no}")
    if inv.amount is not None:
        keys.append(f"amt|{inv.amount}")
    return keys


@dataclass(frozen=True)
class CandidateStats:
    n_invoices: int
    n_blocks: int
    n_skipped_blocks: int
    n_candidates: int
    naive_candidates: int

    @property
    def reduction(self) -> float:
        if self.naive_candidates == 0:
            return 0.0
        return 1.0 - self.n_candidates / self.naive_candidates


def candidate_pairs(normalized: list[NormalizedInvoice]) -> tuple[set[tuple[int, int]], CandidateStats]:
    index: dict[str, list[int]] = defaultdict(list)
    for i, inv in enumerate(normalized):
        for key in blocking_keys(inv):
            index[key].append(i)

    pairs: set[tuple[int, int]] = set()
    skipped = 0
    for members in index.values():
        if len(members) < 2:
            continue
        if len(members) > MAX_BLOCK_SIZE:
            skipped += 1
            continue
        pairs.update(combinations(sorted(members), 2))

    stats = CandidateStats(
        n_invoices=len(normalized),
        n_blocks=len(index),
        n_skipped_blocks=skipped,
        n_candidates=len(pairs),
        naive_candidates=len(normalized) * (len(normalized) - 1) // 2,
    )
    return pairs, stats


def stats_to_dict(stats: CandidateStats) -> dict:
    d = asdict(stats)
    d["reduction"] = round(stats.reduction, 6)
    return d
