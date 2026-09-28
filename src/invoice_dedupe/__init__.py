"""invoice-dedupe: duplicate invoice detection to prevent double payments."""
from .engine import DetectionResult, detect
from .models import FLAG, PASS, REVIEW, Invoice, NormalizedInvoice, PairResult
from .scoring import FieldWeights, ScoringConfig, score_pair
from .synth import generate_dataset

__version__ = "0.1.0"

__all__ = [
    "DetectionResult",
    "FieldWeights",
    "FLAG",
    "Invoice",
    "NormalizedInvoice",
    "PASS",
    "PairResult",
    "REVIEW",
    "ScoringConfig",
    "__version__",
    "detect",
    "generate_dataset",
    "score_pair",
]
