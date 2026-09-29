"""tuning: learned weights, threshold selection, rescore semantics, guards."""
import pytest

from invoice_dedupe.models import PairResult
from invoice_dedupe.tuning import (
    MIN_DECISIONS,
    TunedModel,
    build_model,
    features,
    fit,
    rescore_pairs,
    rule_fire_period_analysis,
    select_thresholds,
)


def test_fit_separates_on_the_identifying_feature():
    rows = []
    for _ in range(60):
        rows.append(([0.6, 0.25, 0.3, 0.2, 1.0, 0.0, 0.0], 1.0))
        rows.append(([0.6, 0.25, 0.3, 0.2, 0.0, 0.0, 1.0], 0.0))
    weights, bias = fit(rows, epochs=4000)
    model = TunedModel(weights=weights, bias=bias, flag_threshold=0.5, review_threshold=0.25)
    dup = features({"invoice_no": 0.6, "vendor": 1.0, "amount": 1.0, "date": 0.2,
                    "rule": "reinvoice_pattern"}, "2024-03", "2024-03")
    fp = features({"invoice_no": 0.6, "vendor": 1.0, "amount": 1.0, "date": 0.2,
                   "rule": "reinvoice_pattern"}, "2024-03", "2024-04")
    assert model.score(dup) > 0.8
    assert model.score(fp) < 0.2
    assert weights["period_match"] > 0.0
    assert "vendor" not in weights  # pinned, never learned


def test_select_thresholds_lands_in_the_decision_gap():
    decided = [(0.9, True)] * 5 + [(0.05, False)] * 10
    flag_t, review_t = select_thresholds(decided)
    assert 0.05 < flag_t <= 0.9
    assert review_t <= flag_t


def _pair(a, b, score, label, breakdown):
    return PairResult(id_a=a, id_b=b, score=score, label=label, breakdown=breakdown)


def test_rescore_pairs_relabels_and_audits():
    model = TunedModel(
        weights={"invoice_no": 1.0, "date": 1.0, "period_match": 1.0,
                 "rule_period_match": 1.0, "rule_period_diff": 1.0},
        bias=-3.0,
        flag_threshold=0.6,
        review_threshold=0.3,
        model_id="m1",
    )
    periods = {"a": "2024-03", "b": "2024-03", "c": "2024-03", "d": "2024-04"}
    strong = _pair("a", "b", 0.95, "flag", {
        "invoice_no": 1.0, "vendor": 1.0, "amount": 1.0, "date": 1.0,
        "billing_period_match": 1.0, "tax_id": "match"})
    weak = _pair("c", "d", 0.72, "review", {
        "invoice_no": 0.5, "vendor": 1.0, "amount": 1.0, "date": 0.2,
        "billing_period_match": 0.0, "tax_id": "match"})
    tax = _pair("a", "d", 0.5, "pass", {
        "invoice_no": 1.0, "vendor": 0.6, "amount": 1.0, "date": 1.0,
        "billing_period_match": 0.0, "tax_id": "mismatch"})
    out = {p.id_a + p.id_b: p for p in rescore_pairs([strong, weak, tax], model, periods)}
    # strong duplicate: high across the board -> stays flagged
    assert out["ab"].label == "flag" and out["ab"].score >= 0.6
    assert out["ab"].breakdown["tuned"]["score_before"] == 0.95
    assert out["ab"].breakdown["tuned"]["model"] == "m1"
    # weak evidence -> below review threshold
    assert out["cd"].label == "pass"
    # tax mismatch is capped below the review threshold even with strong fields
    assert out["ad"].score < 0.3
    assert out["ad"].label == "pass"


def test_build_model_requires_enough_decisions():
    invoices, _ = __import__("invoice_dedupe.synth", fromlist=["generate_dataset"]).generate_dataset(n_invoices=200, seed=42)
    base = __import__("invoice_dedupe.engine", fromlist=["detect"]).detect(invoices)
    with pytest.raises(ValueError, match="review decisions"):
        build_model(invoices, base.pairs, {}, model_id="x")


def test_build_model_requires_both_classes():
    from invoice_dedupe.engine import detect
    from invoice_dedupe.synth import generate_dataset

    invoices, _ = generate_dataset(n_invoices=200, seed=42)
    base = detect(invoices)
    decisions = {
        frozenset((p.id_a, p.id_b)): "duplicate"
        for p in base.pairs if p.label in ("flag", "review")
    }
    decisions = dict(list(decisions.items())[: MIN_DECISIONS + 1])
    with pytest.raises(ValueError, match="one class"):
        build_model(invoices, base.pairs, decisions, model_id="x")


def test_rule_fire_period_analysis_counts_honestly():
    from invoice_dedupe.models import PairResult

    pairs = [
        _pair("a", "b", 0.92, "flag", {"rule": "reinvoice_pattern"}),
        _pair("c", "d", 0.92, "flag", {"rule": "reinvoice_pattern"}),
        _pair("e", "f", 0.5, "pass", {"rule": None}),
    ]
    gt = {frozenset(("a", "b")): "reinvoice"}
    periods = {"a": "2024-03", "b": "2024-03", "c": "2024-03", "d": "2024-04", "e": "2024-05", "f": "2024-05"}
    analysis = rule_fire_period_analysis(pairs, gt, periods)
    assert analysis == {"same_period_dup": 1, "diff_period_notdup": 1}


def test_full_feedback_loop_on_seeded_dataset():
    """Acceptance property: simulated feedback measurably improves the queue
    without degrading detection quality (seed 42, 10k invoices)."""
    from invoice_dedupe.engine import detect
    from invoice_dedupe.evaluate import evaluate, ground_truth
    from invoice_dedupe.normalize import normalize_invoice
    from invoice_dedupe.scoring import ScoringConfig
    from invoice_dedupe.synth import generate_dataset

    invoices, _ = generate_dataset(n_invoices=10_000, seed=42)
    base = detect(invoices, ScoringConfig())
    gt = ground_truth(invoices)
    periods = {inv.id: normalize_invoice(inv).billing_period for inv in invoices}
    decisions = {}
    for p in base.pairs:
        if p.label in ("flag", "review"):
            key = frozenset((p.id_a, p.id_b))
            decisions[key] = "duplicate" if key in gt else "not_duplicate"

    model, train = build_model(invoices, base.pairs, decisions, model_id="test")
    assert train["n_decisions"] == len(decisions)

    after_pairs = rescore_pairs(base.pairs, model, periods)
    before = evaluate(invoices, base.pairs, 0.90, 0.70)
    after = evaluate(invoices, after_pairs, model.flag_threshold, model.review_threshold)

    assert after.review_pairs < before.review_pairs  # measurably smaller queue
    assert after.precision >= before.precision - 0.001
    assert after.recall >= before.recall
    assert after.fp <= before.fp
    # pass-zone pairs stay put: no decision leak into unscored territory
    before_pass = {frozenset((p.id_a, p.id_b)) for p in base.pairs if p.label == "pass"}
    after_pass = {frozenset((p.id_a, p.id_b)) for p in after_pairs if p.label == "pass"}
    assert before_pass <= after_pass
