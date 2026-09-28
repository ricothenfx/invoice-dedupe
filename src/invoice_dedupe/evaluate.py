"""Evaluation against ground truth: precision/recall/F1, threshold sweep, per-variant recall."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .models import Invoice, PairResult


@dataclass
class EvalReport:
    threshold: float
    tp: int
    fp: int
    fn: int
    precision: float
    recall: float
    f1: float
    gt_pairs: int
    flagged_pairs: int
    review_pairs: int
    gt_in_review: int
    per_variant_tp: dict = field(default_factory=dict)
    per_variant_total: dict = field(default_factory=dict)


def ground_truth(invoices: list[Invoice]) -> dict[frozenset, str]:
    """Map duplicate pairs (frozenset of ids) -> variant class."""
    gt: dict[frozenset, str] = {}
    for inv in invoices:
        if inv.duplicate_of:
            gt[frozenset((inv.id, inv.duplicate_of))] = inv.variant or "unknown"
    return gt


def evaluate(
    invoices: list[Invoice],
    pairs: list[PairResult],
    flag_threshold: float = 0.90,
    review_threshold: float = 0.70,
) -> EvalReport:
    gt = ground_truth(invoices)
    scores = {frozenset((p.id_a, p.id_b)): p.score for p in pairs}

    tp = sum(1 for pair in gt if scores.get(pair, 0.0) >= flag_threshold)
    flagged = [pair for pair, s in scores.items() if s >= flag_threshold]
    fp = len(flagged) - tp
    fn = len(gt) - tp

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    per_variant_total = Counter(gt.values())
    per_variant_tp = Counter()
    gt_in_review = 0
    for pair, variant in gt.items():
        s = scores.get(pair, 0.0)
        if s >= flag_threshold:
            per_variant_tp[variant] += 1
        elif review_threshold <= s < flag_threshold:
            gt_in_review += 1

    review_pairs = sum(1 for s in scores.values() if review_threshold <= s < flag_threshold)

    return EvalReport(
        threshold=flag_threshold,
        tp=tp,
        fp=fp,
        fn=fn,
        precision=precision,
        recall=recall,
        f1=f1,
        gt_pairs=len(gt),
        flagged_pairs=len(flagged),
        review_pairs=review_pairs,
        gt_in_review=gt_in_review,
        per_variant_tp=dict(per_variant_tp),
        per_variant_total=dict(per_variant_total),
    )


def sweep_thresholds(
    invoices: list[Invoice],
    pairs: list[PairResult],
    thresholds: list[float],
    review_threshold: float = 0.70,
) -> list[dict]:
    rows = []
    for t in thresholds:
        r = evaluate(invoices, pairs, flag_threshold=t, review_threshold=review_threshold)
        rows.append(
            {
                "threshold": t,
                "precision": round(r.precision, 4),
                "recall": round(r.recall, 4),
                "f1": round(r.f1, 4),
                "fp": r.fp,
                "flagged": r.flagged_pairs,
            }
        )
    return rows


def render_report(r: EvalReport, stats: dict | None = None) -> str:
    lines = []
    if stats:
        naive = stats.get("naive_candidates", 0)
        cand = stats.get("n_candidates", 0)
        lines.append(
            f"blocking : {naive:,} naive comparisons -> {cand:,} candidates "
            f"({stats.get('reduction', 0) * 100:.2f}% reduction)"
        )
        lines.append(
            f"scoring  : {stats.get('scored_pairs', 0):,} pairs scored "
            f"({stats.get('flagged', 0):,} flagged, {stats.get('review', 0):,} review)"
        )
        lines.append("")
    lines.append(f"--- Evaluation at threshold {r.threshold:.2f} ---")
    lines.append(f"precision: {r.precision:.4f}  recall: {r.recall:.4f}  f1: {r.f1:.4f}")
    lines.append(f"tp={r.tp}  fp={r.fp}  fn={r.fn}  (ground-truth pairs: {r.gt_pairs})")
    lines.append(
        f"review queue: {r.review_pairs} pairs; "
        f"GT duplicates in review zone: {r.gt_in_review}"
    )
    lines.append("")
    lines.append("recall per duplicate variant:")
    for variant in sorted(r.per_variant_total):
        tp = r.per_variant_tp.get(variant, 0)
        total = r.per_variant_total[variant]
        pct = tp / total * 100 if total else 0.0
        lines.append(f"  {variant:<14} {tp:>4}/{total:<4} ({pct:.1f}%)")
    return "\n".join(lines)


def render_sweep(rows: list[dict]) -> str:
    lines = ["", "--- Threshold sweep ---", f"{'thr':>5} {'precision':>9} {'recall':>8} {'f1':>7} {'fp':>5} {'flagged':>8}"]
    for row in rows:
        lines.append(
            f"{row['threshold']:>5.2f} {row['precision']:>9.4f} {row['recall']:>8.4f} "
            f"{row['f1']:>7.4f} {row['fp']:>5} {row['flagged']:>8}"
        )
    return "\n".join(lines)
