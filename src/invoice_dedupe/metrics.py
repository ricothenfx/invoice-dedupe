"""Dashboard metrics: threshold-dependent counts, precision estimates, exposure.

Pure functions over the pair ``distribution`` produced by
``db.scored_distribution``: one row per scored pair with

    score     float  similarity score (0..1)
    gt        bool   pair is a known duplicate (seeded ground truth)
    value     int    double-payment exposure (larger of the two amounts)
    decision  str    reviewer verdict so far, or None

Two precision signals are supported, in order of strength:

1. **Measured** (ground truth available, e.g. after ``invoice-dedupe
   seed-demo``): precision/recall of the flag rule at the chosen threshold.
2. **Decision-based estimate** (no ground truth): fraction of already
   reviewed pairs at/above the threshold that the reviewer confirmed as
   duplicates. Reported only when at least one such decision exists; it is
   an estimate over reviewed pairs, not over all flagged pairs.
"""
from __future__ import annotations


def threshold_metrics(
    distribution: list[dict],
    flag_threshold: float,
    gt_pairs_total: int | None,
) -> dict:
    """Compute dashboard numbers for one flag threshold.

    ``gt_pairs_total`` is the number of known duplicate pairs (each ground-truth
    duplicate invoice contributes exactly one). Pairs absent from
    ``distribution`` (unscored) count as below any threshold.
    """
    flagged_rows = [r for r in distribution if r["score"] >= flag_threshold]
    flagged = len(flagged_rows)
    value_at_risk = sum(int(r["value"]) for r in flagged_rows)

    decided_rows = [r for r in flagged_rows if r["decision"]]
    decided = len(decided_rows)
    decided_duplicate = sum(1 for r in decided_rows if r["decision"] == "duplicate")
    estimated_precision = (
        round(decided_duplicate / decided, 4) if decided > 0 else None
    )

    gt = None
    if gt_pairs_total:
        tp = sum(1 for r in flagged_rows if r["gt"])
        fp = flagged - tp
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / gt_pairs_total if gt_pairs_total else 0.0
        gt = {
            "tp": tp,
            "fp": fp,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
        }

    return {
        "flagged": flagged,
        "value_at_risk": value_at_risk,
        "decided": decided,
        "decided_duplicate": decided_duplicate,
        "estimated_precision": estimated_precision,
        "gt": gt,
    }
