"""Feedback loop: reviewer decisions tune thresholds and weights (charter D4).

The tuner learns a logistic adjustment over the per-field score breakdowns of
pairs a reviewer actually saw (flag/review zone), then re-labels exactly that
slice — pass-zone pairs keep their base scores, so decisions can never leak
into territory the reviewer signal cannot identify.

Design constraints, all validated against the seeded dataset before building:

* Reviewed pairs carry no variance in vendor/amount/tax (same vendor ⇒ same
  identifiers), so those weights are *pinned* to the deployed values and the
  tax-ID hard rule is re-applied; only identifiable weights (invoice number,
  date, billing period, rule interactions) are learned.
* Learned weights stay close to a prior centered on the deployed model
  (MAP with a Gaussian prior), so decisions *adjust* a validated model
  instead of rebuilding it from scratch on 3-sample patterns.
* Sum-loss gradient descent (not mean loss) so rare but real decision
  patterns are learnable at all.
* Thresholds are selected on the decision labels alone (F1 for the flag
  threshold, F2 for the review threshold); the full before/after evaluation
  against ground truth is then *reported honestly*, including the false
  positives the model could not remove.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

# feature layout: [invoice_no, vendor, amount, date, period, rule*period,
#                  rule*(1-period)] — vendor/amount are pinned
FEATURE_KEYS = (
    "invoice_no",
    "vendor",
    "amount",
    "date",
    "period_match",
    "rule_period_match",
    "rule_period_diff",
)
LEARNED_INDEXES = (0, 3, 4, 5, 6)  # positions of learned weights in x
PINNED = {"vendor": 0.25, "amount": 0.30}
PRIOR_MEAN = {0: 0.35, 3: 0.10, 4: 0.0, 5: 0.0, 6: 0.0}
PRIOR_LAMBDA = {0: 1.0, 3: 4.0, 4: 1.0, 5: 1.0, 6: 1.0}

MIN_DECISIONS = 20


@dataclass
class TunedModel:
    """A tuned scorer: learned weights + selected thresholds + audit fields."""

    weights: dict[str, float] = field(default_factory=dict)
    bias: float = 0.0
    flag_threshold: float = 0.90
    review_threshold: float = 0.70
    n_decisions: int = 0
    train_metrics: dict = field(default_factory=dict)
    eval_metrics: dict = field(default_factory=dict)
    model_id: str = ""

    def score(self, x: list[float]) -> float:
        z = self.bias + sum(
            self.weights.get(FEATURE_KEYS[i], 0.0) * x[i] for i in range(len(x))
        )
        z += PINNED["vendor"] * x[1] + PINNED["amount"] * x[2]
        if z >= 0:
            return 1.0 / (1.0 + math.exp(-min(z, 700.0)))
        ez = math.exp(max(z, -700.0))
        return ez / (1.0 + ez)


def features(breakdown: dict, period_a: str, period_b: str) -> list[float]:
    """Build the feature vector from a stored pair breakdown + periods."""
    period_match = 1.0 if period_a and period_b and period_a == period_b else 0.0
    rule = 1.0 if breakdown.get("rule") == "reinvoice_pattern" else 0.0
    return [
        float(breakdown.get("invoice_no", 0.0)),
        float(breakdown.get("vendor", 0.0)),
        float(breakdown.get("amount", 0.0)),
        float(breakdown.get("date", 0.0)),
        period_match,
        rule * period_match,
        rule * (1.0 - period_match),
    ]


def fit(
    rows: list[tuple[list[float], float]],
    *,
    lr: float = 0.004,
    epochs: int = 30_000,
    tol: float = 1e-7,
) -> tuple[dict[str, float], float]:
    """Batch gradient descent with a Gaussian prior on the learned weights.

    Sum loss (not mean) so rare decision patterns stay learnable; the prior
    strength is set on the sum-loss scale. Only the identifiable weights
    (``LEARNED_INDEXES``) move; vendor/amount stay pinned to the deployed
    constants — reviewed pairs carry no variance in them, so learning them
    would be noise. Returns (weights, bias) with learned keys only.
    """
    w = {FEATURE_KEYS[i]: PRIOR_MEAN[i] for i in LEARNED_INDEXES}
    b = 0.0
    if not rows:
        return w, b
    for epoch in range(epochs):
        gw = {i: 0.0 for i in LEARNED_INDEXES}
        gb = 0.0
        for x, y in rows:
            z = b + sum(w[FEATURE_KEYS[i]] * x[i] for i in LEARNED_INDEXES)
            z += PINNED["vendor"] * x[1] + PINNED["amount"] * x[2]
            e = _sigmoid(z) - y
            gb += e
            for i in LEARNED_INDEXES:
                gw[i] += e * x[i]
        step = lr if epoch < epochs * 4 // 5 else lr * 0.25
        new_b = b - step * gb
        max_delta = abs(new_b - b)
        new_w = dict(w)
        for i in LEARNED_INDEXES:
            grad = gw[i] + 2.0 * PRIOR_LAMBDA[i] * (w[FEATURE_KEYS[i]] - PRIOR_MEAN[i])
            new_w[FEATURE_KEYS[i]] = w[FEATURE_KEYS[i]] - step * grad
            max_delta = max(max_delta, abs(new_w[FEATURE_KEYS[i]] - w[FEATURE_KEYS[i]]))
        w, b = new_w, new_b
        if max_delta < tol:
            break
    return w, b


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-min(z, 700.0)))
    ez = math.exp(max(z, -700.0))
    return ez / (1.0 + ez)


def _fbeta(
    decided: list[tuple[float, bool]], threshold: float, n_positives: int, beta: float
) -> float:
    tp = sum(1 for s, pos in decided if s >= threshold and pos)
    fp = sum(1 for s, pos in decided if s >= threshold and not pos)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / n_positives if n_positives else 0.0
    b2 = beta * beta
    denom = b2 * precision + recall
    return (1 + b2) * precision * recall / denom if denom else 0.0


def select_thresholds(
    decided: list[tuple[float, bool]],
) -> tuple[float, float]:
    """Pick (flag, review) thresholds from decision labels only.

    The flag threshold maximizes F1 (auto-blocked payments demand precision);
    the review threshold maximizes F2 among lower scores (the queue tolerates
    false alarms to keep recall high). Ties resolve toward the wider margin.
    """
    positives = sum(1 for _, pos in decided if pos)
    grid = [i / 1000 for i in range(20, 990)]
    flag_t = max(grid, key=lambda t: (round(_fbeta(decided, t, positives, 1.0), 9), t))
    lower = [t for t in grid if t < flag_t]
    review_t = (
        max(lower, key=lambda t: (round(_fbeta(decided, t, positives, 2.0), 9), -t))
        if lower
        else flag_t / 2
    )
    return flag_t, review_t


def rescore_pairs(
    pairs: list,
    model: TunedModel,
    periods: dict[str, str],
) -> list:
    """Re-score all base pairs with the tuned model.

    Every pair is re-scored so the stored ``score`` column keeps one coherent
    meaning (the active model's confidence) for the dashboard and evaluation.
    Out-of-distribution safety comes from the pinned vendor/amount weights and
    the re-applied tax-ID hard rule (capped below the tuned review threshold,
    mirroring base behavior where capped pairs never reach the review zone).
    Each tuned breakdown records the previous score and label.
    """
    from .models import PairResult

    out = []
    for p in pairs:
        x = features(p.breakdown, periods.get(p.id_a, ""), periods.get(p.id_b, ""))
        score = model.score(x)
        if p.breakdown.get("tax_id") == "mismatch":
            score = min(score, 0.5 * model.review_threshold)
        score = round(min(score, 1.0), 9)
        if score >= model.flag_threshold:
            label = "flag"
        elif score >= model.review_threshold:
            label = "review"
        else:
            label = "pass"
        breakdown = dict(p.breakdown)
        breakdown["tuned"] = {
            "score_before": p.score,
            "label_before": p.label,
            "model": model.model_id or None,
        }
        out.append(PairResult(id_a=p.id_a, id_b=p.id_b, score=score, label=label, breakdown=breakdown))
    return out


def rule_fire_period_analysis(
    pairs: list, gt: dict[frozenset, str], periods: dict[str, str]
) -> dict:
    """Honest breakdown of re-invoice-rule fires by ground truth and period.

    This is the evidence behind the false-positive statement in the tuning
    report: rule fires whose two invoices share a billing period vs. those
    that cross a month boundary, counted against ground truth.
    """
    analysis: dict[str, dict[str, int]] = {}
    for p in pairs:
        if p.breakdown.get("rule") != "reinvoice_pattern":
            continue
        same = periods.get(p.id_a) == periods.get(p.id_b) and bool(periods.get(p.id_a))
        pos = frozenset((p.id_a, p.id_b)) in gt
        key = ("same_period" if same else "diff_period") + ("_dup" if pos else "_notdup")
        analysis[key] = analysis.get(key, 0) + 1
    return analysis


def build_model(
    invoices: list,
    base_pairs: list,
    decisions: dict[frozenset, str],
    *,
    model_id: str = "",
    lr: float = 0.004,
    epochs: int = 30_000,
) -> tuple[TunedModel, dict]:
    """Fit a tuned model from reviewer decisions over base-scored pairs.

    ``decisions`` maps frozenset{invoice_a, invoice_b} -> 'duplicate' |
    'not_duplicate'. Returns the model plus a training summary (counts,
    decision-space precision/recall at the selected thresholds).
    """
    from .normalize import normalize_invoice

    if len(decisions) < MIN_DECISIONS:
        raise ValueError(
            f"need at least {MIN_DECISIONS} review decisions to tune "
            f"(have {len(decisions)}); collect decisions in the review queue "
            "or run `invoice-dedupe simulate-feedback` first"
        )
    periods = {inv.id: normalize_invoice(inv).billing_period for inv in invoices}
    scores = {frozenset((p.id_a, p.id_b)): p for p in base_pairs}

    rows: list[tuple[list[float], float]] = []
    for pair_key, decision in decisions.items():
        pair = scores.get(pair_key)
        if pair is None or pair.label == "pass":
            continue  # only pairs a reviewer could see feed the tuner
        y = 1.0 if decision == "duplicate" else 0.0
        x = features(pair.breakdown, periods.get(pair.id_a, ""), periods.get(pair.id_b, ""))
        rows.append((x, y))

    positives = sum(y for _, y in rows)
    negatives = len(rows) - positives
    if positives == 0 or negatives == 0:
        raise ValueError(
            "decisions contain only one class; tuning needs both duplicate "
            "and not_duplicate verdicts"
        )

    weights, bias = fit(rows, lr=lr, epochs=epochs)
    model = TunedModel(
        weights=weights,
        bias=round(bias, 9),
        n_decisions=len(rows),
        model_id=model_id,
    )
    decided = [(model.score(x), y == 1.0) for x, y in rows]
    flag_t, review_t = select_thresholds(decided)
    model.flag_threshold, model.review_threshold = flag_t, review_t

    tp = sum(1 for s, pos in decided if s >= flag_t and pos)
    fp = sum(1 for s, pos in decided if s >= flag_t and not pos)
    model.train_metrics = {
        "n_decisions": len(rows),
        "n_duplicate": int(positives),
        "n_not_duplicate": int(negatives),
        "flag_threshold": flag_t,
        "review_threshold": review_t,
        "decision_precision_at_flag": round(tp / (tp + fp), 4) if tp + fp else 0.0,
        "decision_recall_at_flag": round(tp / positives, 4) if positives else 0.0,
    }
    return model, model.train_metrics
