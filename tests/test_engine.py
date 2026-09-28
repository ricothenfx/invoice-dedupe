from invoice_dedupe.dataset import load_invoices, load_pairs, save_invoices, save_pairs
from invoice_dedupe.engine import detect
from invoice_dedupe.evaluate import evaluate
from invoice_dedupe.scoring import ScoringConfig
from invoice_dedupe.synth import generate_dataset


def test_end_to_end_small_dataset():
    invoices, meta = generate_dataset(n_invoices=400, dup_rate=0.06, seed=11)
    result = detect(invoices, ScoringConfig())
    report = evaluate(invoices, result.pairs)
    assert report.recall == 1.0
    assert report.precision >= 0.95
    assert result.stats["n_candidates"] < result.stats["naive_candidates"] / 100


def test_jsonl_roundtrip(tmp_path):
    invoices, _ = generate_dataset(n_invoices=100, seed=5)
    inv_path = tmp_path / "invoices.jsonl"
    save_invoices(invoices, inv_path)
    assert load_invoices(inv_path) == invoices

    result = detect(invoices, ScoringConfig())
    pairs_path = tmp_path / "pairs.jsonl"
    save_pairs(result.pairs, pairs_path)
    loaded = load_pairs(pairs_path)
    assert [p.to_dict() for p in loaded] == [p.to_dict() for p in result.pairs]


def test_detect_flags_without_ground_truth_labels():
    invoices, _ = generate_dataset(n_invoices=200, dup_rate=0.10, seed=3)
    result = detect(invoices, ScoringConfig())
    assert result.stats["flagged"] > 0
    assert all(p.label in {"flag", "review", "pass"} for p in result.pairs)
