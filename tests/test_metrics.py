"""Unit tests for the dashboard metrics (no database needed)."""
from __future__ import annotations

from invoice_dedupe.metrics import threshold_metrics

ROWS = [
    {"score": 0.95, "gt": True, "value": 100, "decision": None},
    {"score": 0.92, "gt": True, "value": 250, "decision": "duplicate"},
    {"score": 0.91, "gt": False, "value": 90, "decision": "not_duplicate"},
    {"score": 0.80, "gt": False, "value": 500, "decision": "duplicate"},
    {"score": 0.50, "gt": True, "value": 10, "decision": None},
]


def test_measured_precision_and_decisions_at_threshold():
    m = threshold_metrics(ROWS, 0.90, gt_pairs_total=3)
    # flagged: the three rows >= 0.90; value 100 + 250 + 90
    assert m["flagged"] == 3
    assert m["value_at_risk"] == 440
    # two of the three flagged rows are ground-truth duplicates
    assert m["gt"] == {"tp": 2, "fp": 1, "precision": round(2 / 3, 4), "recall": round(2 / 3, 4)}
    # decisions only count at/above the threshold
    assert m["decided"] == 2
    assert m["decided_duplicate"] == 1
    assert m["estimated_precision"] == 0.5


def test_threshold_excludes_lower_pairs():
    m = threshold_metrics(ROWS, 0.75, gt_pairs_total=3)
    assert m["flagged"] == 4
    assert m["value_at_risk"] == 940
    assert m["decided"] == 3
    assert m["estimated_precision"] == round(2 / 3, 4)


def test_no_ground_truth_means_no_measured_gt():
    m = threshold_metrics(ROWS, 0.90, gt_pairs_total=None)
    assert m["gt"] is None
    assert m["estimated_precision"] == 0.5


def test_zero_gt_pairs_treated_as_absent():
    m = threshold_metrics(ROWS, 0.90, gt_pairs_total=0)
    assert m["gt"] is None


def test_empty_distribution():
    m = threshold_metrics([], 0.90, gt_pairs_total=2)
    assert m["flagged"] == 0
    assert m["value_at_risk"] == 0
    assert m["decided"] == 0
    assert m["estimated_precision"] is None
    assert m["gt"] == {"tp": 0, "fp": 0, "precision": 0.0, "recall": 0.0}


def test_perfect_precision():
    clean = [r for r in ROWS if r["gt"]]
    m = threshold_metrics(clean, 0.90, gt_pairs_total=2)
    assert m["gt"]["precision"] == 1.0
    assert m["gt"]["fp"] == 0
    assert m["gt"]["recall"] == 1.0
