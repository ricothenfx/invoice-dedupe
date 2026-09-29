"""Pairwise invoice similarity: per-field fuzzy scoring, weights, contextual rules."""
from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

from .models import FLAG, PASS, REVIEW, NormalizedInvoice


@dataclass(frozen=True)
class FieldWeights:
    invoice_no: float = 0.35
    vendor: float = 0.25
    amount: float = 0.30
    date: float = 0.10

    def __post_init__(self) -> None:
        total = self.invoice_no + self.vendor + self.amount + self.date
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"field weights must sum to 1.0, got {total}")


@dataclass(frozen=True)
class ScoringConfig:
    weights: FieldWeights = field(default_factory=FieldWeights)
    flag_threshold: float = 0.90
    review_threshold: float = 0.70
    date_decay_days: float = 45.0
    reinvoice_window_days: int = 14
    reinvoice_floor: float = 0.92
    tax_mismatch_cap: float = 0.55
    invno_mismatch_factor: float = 0.70


def similarity_invoice_no(a: str, b: str, mismatch_factor: float = 0.70) -> float:
    """1.0 when canonical forms are identical; different strings are a weak signal.

    Sequential numbers (...0001 vs ...0002) are near-identical character-wise yet
    usually distinct transactions, so unequal strings are discounted before they
    may act as duplicate evidence.
    """
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return mismatch_factor * float(JaroWinkler.normalized_similarity(a, b))


def similarity_vendor(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return max(fuzz.token_set_ratio(a, b), fuzz.token_sort_ratio(a, b)) / 100.0


def similarity_amount(a: int | None, b: int | None) -> float:
    if a is None or b is None:
        return 0.0
    if a == b:
        return 1.0
    hi, lo = max(abs(a), abs(b)), min(abs(a), abs(b))
    if hi == 0:
        return 0.0
    return max(0.0, 1.0 - (hi - lo) / hi)


def similarity_date(a, b, decay_days: float = 45.0) -> float:
    if a is None or b is None:
        return 0.0
    days = abs((a - b).days)
    if days == 0:
        return 1.0
    return max(0.0, 1.0 - days / decay_days)


def score_pair(a: NormalizedInvoice, b: NormalizedInvoice, cfg: ScoringConfig) -> tuple[float, dict]:
    """Score a pair of invoices and return (score, per-field breakdown).

    Scoring layers:
      L1 exact-key    -> similarity 1.0 when canonical forms are identical
      L2 fuzzy        -> JaroWinkler / token-set per field, combined by weights
      L3 contextual   -> re-invoice pattern (same vendor+amount, different number,
                         close dates) lifts the score to the floor
      hard rule       -> differing tax IDs cap the score (different legal entity)
    """
    breakdown = {
        "invoice_no": similarity_invoice_no(a.invoice_no, b.invoice_no, cfg.invno_mismatch_factor),
        "vendor": similarity_vendor(a.vendor, b.vendor),
        "amount": similarity_amount(a.amount, b.amount),
        "date": similarity_date(a.date, b.date, cfg.date_decay_days),
        # stored for the Phase 4 feedback loop (feature, not score component):
        # recurring false positives and re-invoice duplicates separate on it
        "billing_period_match": 1.0 if (a.billing_period and a.billing_period == b.billing_period) else 0.0,
    }
    score = (
        cfg.weights.invoice_no * breakdown["invoice_no"]
        + cfg.weights.vendor * breakdown["vendor"]
        + cfg.weights.amount * breakdown["amount"]
        + cfg.weights.date * breakdown["date"]
    )

    if (
        breakdown["vendor"] == 1.0
        and breakdown["amount"] == 1.0
        and a.invoice_no != b.invoice_no
        and a.date is not None
        and b.date is not None
    ):
        gap = abs((a.date - b.date).days)
        if 1 <= gap <= cfg.reinvoice_window_days:
            score = max(score, cfg.reinvoice_floor)
            breakdown["rule"] = "reinvoice_pattern"

    if a.tax_id and b.tax_id:
        if a.tax_id != b.tax_id:
            score = min(score, cfg.tax_mismatch_cap)
            breakdown["tax_id"] = "mismatch"
        else:
            breakdown["tax_id"] = "match"
    else:
        breakdown["tax_id"] = "n/a"

    return round(min(score, 1.0), 9), breakdown


def label_for(score: float, cfg: ScoringConfig) -> str:
    if score >= cfg.flag_threshold:
        return FLAG
    if score >= cfg.review_threshold:
        return REVIEW
    return PASS
