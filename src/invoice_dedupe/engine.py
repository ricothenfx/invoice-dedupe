"""Detection pipeline: normalize -> block -> score -> classify."""
from __future__ import annotations

from dataclasses import dataclass, field

from .blocking import candidate_pairs, stats_to_dict
from .models import Invoice, NormalizedInvoice, PairResult
from .normalize import normalize_invoice
from .scoring import ScoringConfig, label_for, score_pair


@dataclass
class DetectionResult:
    pairs: list[PairResult]
    stats: dict = field(default_factory=dict)


def detect(invoices: list[Invoice], config: ScoringConfig | None = None) -> DetectionResult:
    config = config or ScoringConfig()
    normalized: list[NormalizedInvoice] = [normalize_invoice(inv) for inv in invoices]
    candidates, stats = candidate_pairs(normalized)

    pairs: list[PairResult] = []
    for i, j in sorted(candidates):
        score, breakdown = score_pair(normalized[i], normalized[j], config)
        pairs.append(
            PairResult(
                id_a=normalized[i].id,
                id_b=normalized[j].id,
                score=score,
                label=label_for(score, config),
                breakdown=breakdown,
            )
        )

    result_stats = stats_to_dict(stats)
    result_stats["scored_pairs"] = len(pairs)
    result_stats["flagged"] = sum(1 for p in pairs if p.label == "flag")
    result_stats["review"] = sum(1 for p in pairs if p.label == "review")
    return DetectionResult(pairs=pairs, stats=result_stats)
