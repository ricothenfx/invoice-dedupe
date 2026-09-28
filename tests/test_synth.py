from invoice_dedupe.synth import generate_dataset


def test_generation_is_deterministic():
    a, meta_a = generate_dataset(n_invoices=500, dup_rate=0.05, seed=7)
    b, meta_b = generate_dataset(n_invoices=500, dup_rate=0.05, seed=7)
    assert a == b
    assert meta_a == meta_b


def test_duplicates_are_labeled():
    invoices, meta = generate_dataset(n_invoices=800, dup_rate=0.05, seed=1)
    dups = [i for i in invoices if i.duplicate_of]
    assert len(dups) == meta["n_duplicates"] > 0
    ids = {i.id for i in invoices}
    assert all(d.duplicate_of in ids for d in dups)
    assert all(d.variant for d in dups)


def test_variant_mix_covers_multiple_classes():
    _, meta = generate_dataset(n_invoices=2000, dup_rate=0.10, seed=5)
    assert len(meta["variant_counts"]) >= 4


def test_recurring_vendors_exist():
    _, meta = generate_dataset(n_invoices=2000, seed=5)
    assert meta["n_recurring_vendors"] > 0


def test_amounts_unique_per_vendor_month_for_legit():
    invoices, _ = generate_dataset(n_invoices=1000, seed=9)
    seen = set()
    for inv in invoices:
        if inv.duplicate_of:
            continue
        key = (inv.vendor_name, inv.invoice_date.strftime("%Y-%m"), inv.amount)
        assert key not in seen
        seen.add(key)
