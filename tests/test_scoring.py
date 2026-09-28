from datetime import date

import pytest

from invoice_dedupe.models import FLAG, PASS, REVIEW, Invoice
from invoice_dedupe.normalize import normalize_invoice
from invoice_dedupe.scoring import FieldWeights, ScoringConfig, label_for, score_pair

CFG = ScoringConfig()


def _inv(**kw):
    defaults = dict(
        id="A",
        vendor_name="Northwind Logistics",
        invoice_no="INV-2024/0001",
        amount=5_000,
        invoice_date=date(2024, 3, 10),
        tax_id="12-3456789-401",
    )
    defaults.update(kw)
    return normalize_invoice(Invoice(**defaults))


def test_weights_must_sum_to_one():
    with pytest.raises(ValueError):
        FieldWeights(invoice_no=0.5, vendor=0.5, amount=0.5, date=0.5)


def test_identical_invoices_score_one():
    score, breakdown = score_pair(_inv(), _inv(id="B"), CFG)
    assert score == 1.0
    assert breakdown["tax_id"] == "match"


def test_confusable_numbers_match_exactly():
    score, _ = score_pair(_inv(invoice_no="INV-O78I"), _inv(id="B", invoice_no="INV-0781"), CFG)
    assert score == 1.0


def test_different_numbers_are_weak_signal():
    score, breakdown = score_pair(
        _inv(invoice_no="INV-2024/0001"),
        _inv(id="B", invoice_no="INV-2024/0002", invoice_date=date(2024, 4, 8)),
        CFG,
    )
    assert breakdown["invoice_no"] < 0.7
    assert score < CFG.flag_threshold


def test_tax_mismatch_caps_score():
    score, breakdown = score_pair(
        _inv(),
        _inv(id="B", tax_id="98-7654321-004"),
        CFG,
    )
    assert score <= CFG.tax_mismatch_cap
    assert breakdown["tax_id"] == "mismatch"


def test_reinvoice_rule_fires():
    score, breakdown = score_pair(
        _inv(invoice_no="INV-2024/0001", invoice_date=date(2024, 3, 10)),
        _inv(id="B", invoice_no="SJ-2024-7777", invoice_date=date(2024, 3, 18)),
        CFG,
    )
    assert breakdown.get("rule") == "reinvoice_pattern"
    assert score >= CFG.reinvoice_floor


def test_reinvoice_rule_ignores_far_dates():
    score, breakdown = score_pair(
        _inv(invoice_no="INV-2024/0001", invoice_date=date(2024, 1, 10)),
        _inv(id="B", invoice_no="SJ-2024-7777", invoice_date=date(2024, 6, 10)),
        CFG,
    )
    assert "rule" not in breakdown
    assert score < CFG.flag_threshold


def test_different_vendor_and_amount_score_low():
    score, _ = score_pair(
        _inv(),
        _inv(
            id="B",
            vendor_name="Acme Trading",
            invoice_no="QTN-2024-5555",
            amount=7_300,
            invoice_date=date(2024, 5, 2),
        ),
        CFG,
    )
    assert score < CFG.review_threshold


def test_label_for_thresholds():
    assert label_for(0.95, CFG) == FLAG
    assert label_for(0.75, CFG) == REVIEW
    assert label_for(0.10, CFG) == PASS
