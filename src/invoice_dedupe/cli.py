"""invoice-dedupe CLI: generate, run, evaluate, demo, init-db, seed-demo,
serve, worker, make-pdf, make-photo (Phase 4), simulate-feedback (Phase 4),
tune (Phase 4)."""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from . import db, pdfgen, photogen
from .dataset import load_invoices, load_pairs, save_invoices, save_pairs
from .engine import detect
from .evaluate import evaluate, render_report, render_sweep, sweep_thresholds
from .scoring import ScoringConfig
from .synth import generate_dataset


def _add_scoring_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--flag-threshold", type=float, default=0.90)
    p.add_argument("--review-threshold", type=float, default=0.70)
    p.add_argument("--reinvoice-window", type=int, default=14)


def cmd_generate(args: argparse.Namespace) -> int:
    invoices, meta = generate_dataset(n_invoices=args.n, dup_rate=args.dup_rate, seed=args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    save_invoices(invoices, out / "invoices.jsonl")
    (out / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"dataset : {out / 'invoices.jsonl'}")
    print(json.dumps(meta, indent=2, ensure_ascii=False))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    invoices = load_invoices(args.dataset)
    config = ScoringConfig(
        flag_threshold=args.flag_threshold,
        review_threshold=args.review_threshold,
        reinvoice_window_days=args.reinvoice_window,
    )
    result = detect(invoices, config)
    save_pairs(result.pairs, args.out)
    print(f"pairs   : {args.out}")
    for k, v in result.stats.items():
        print(f"  {k}: {v:,}" if isinstance(v, int) else f"  {k}: {v}")
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    invoices = load_invoices(args.dataset)
    pairs = load_pairs(args.pairs)
    config = ScoringConfig(
        flag_threshold=args.flag_threshold,
        review_threshold=args.review_threshold,
        reinvoice_window_days=args.reinvoice_window,
    )
    report = evaluate(
        invoices, pairs,
        flag_threshold=config.flag_threshold,
        review_threshold=config.review_threshold,
    )
    print(render_report(report))
    rows = sweep_thresholds(
        invoices, pairs,
        thresholds=[0.80, 0.85, 0.88, 0.90, 0.92, 0.95],
        review_threshold=config.review_threshold,
    )
    print(render_sweep(rows))
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    invoices, meta = generate_dataset(n_invoices=args.n, dup_rate=args.dup_rate, seed=args.seed)
    config = ScoringConfig(
        flag_threshold=args.flag_threshold,
        review_threshold=args.review_threshold,
        reinvoice_window_days=args.reinvoice_window,
    )
    result = detect(invoices, config)
    report = evaluate(
        invoices, result.pairs,
        flag_threshold=config.flag_threshold,
        review_threshold=config.review_threshold,
    )
    print("=== invoice-dedupe demo ===")
    print(
        f"dataset : {meta['n_invoices']:,} invoices, {meta['n_duplicates']} duplicates "
        f"({meta['dup_rate_effective'] * 100:.1f}%), {meta['n_vendors']} vendors "
        f"({meta['n_recurring_vendors']} recurring)"
    )
    print()
    print(render_report(report, result.stats))
    rows = sweep_thresholds(
        invoices, result.pairs,
        thresholds=[0.80, 0.85, 0.88, 0.90, 0.92, 0.95],
        review_threshold=config.review_threshold,
    )
    print(render_sweep(rows))
    return 0


def cmd_init_db(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        db.init_schema(conn)
    print(f"database: schema initialized ({db.database_url()})")
    return 0


def cmd_seed_demo(args: argparse.Namespace) -> int:
    """Load a seeded synthetic dataset (with ground truth) and run detection.

    Gives the Phase 3 web app a realistic review queue (hundreds of flagged
    pairs) without hand-crafting PDFs. Ground truth is persisted in the
    nullable `duplicate_of`/`variant` columns, so the dashboard can measure
    precision/recall live at any threshold. Regular PDF ingestion is unaffected.
    """
    invoices, meta = generate_dataset(n_invoices=args.n, dup_rate=args.dup_rate, seed=args.seed)
    with db.connect() as conn:
        db.init_schema(conn)
        existing = db.count_invoices(conn)
        if existing and not args.reset:
            print(
                f"error   : database already holds {existing:,} invoices; "
                "use --reset to replace all data"
            )
            return 1
        if args.reset:
            db.truncate_all(conn)
            if db.deactivate_tuning(conn):
                print("reset   : deactivated tuning model(s) (decisions were cleared)")
        inserted = db.insert_invoices_bulk(
            conn,
            invoices,
            source_type="synthetic",
            source_name=f"seeded demo (n={args.n}, seed={args.seed})",
        )
        config = ScoringConfig()
        result = detect(invoices, config)
        db.replace_pairs(conn, result.pairs)

    print(f"seeded  : {inserted:,} invoices, {meta['n_duplicates']} ground-truth duplicates")
    print(
        f"pairs   : {len(result.pairs):,} scored "
        f"({result.stats.get('flagged', 0):,} flagged, {result.stats.get('review', 0):,} review)"
    )
    print("next    : invoice-dedupe serve   # then open http://127.0.0.1:8000")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("invoice_dedupe.api:app", host=args.host, port=args.port)
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    from . import worker as worker_mod

    with db.connect() as conn:
        worker_mod.run_worker(conn, poll_seconds=args.poll_seconds)
    return 0


def cmd_make_pdf(args: argparse.Namespace) -> int:
    lines = [
        f"Vendor: {args.vendor}",
        f"Invoice No: {args.invoice_no}",
        f"Invoice Date: {args.invoice_date}",
        f"Amount Due: USD {args.amount}",
        f"Tax ID: {args.tax_id}",
    ]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(pdfgen.make_pdf(lines))
    print(f"pdf     : {out}")
    return 0


def cmd_make_photo(args: argparse.Namespace) -> int:
    """Write a crumpled-invoice photo PNG for the vision-LLM path (Phase 4)."""
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(
        photogen.make_photo(
            vendor=args.vendor,
            invoice_no=args.invoice_no,
            invoice_date=args.invoice_date,
            amount=args.amount,
            tax_id=args.tax_id,
            seed=args.seed,
        )
    )
    print(f"photo   : {out}")
    return 0


def cmd_simulate_feedback(args: argparse.Namespace) -> int:
    """Persist reviewer decisions from the seeded ground truth (demo input).

    Every flagged/reviewed pair gets the verdict a perfect reviewer would
    give (duplicate iff the pair is a seeded ground-truth duplicate). This
    simulates the review-queue work that feeds the Phase 4 tuning loop.
    """
    with db.connect() as conn:
        db.init_schema(conn)
        if not db.count_gt_pairs(conn):
            print("error   : no ground truth in database; run `invoice-dedupe seed-demo` first")
            return 1
        pairs = db.list_pairs(conn, limit=1_000_000)
        gt_dupes = {
            frozenset((row["id"], row["duplicate_of"]))
            for row in conn.execute(
                "SELECT id, duplicate_of FROM invoices WHERE duplicate_of IS NOT NULL"
            ).fetchall()
        }
        decided = 0
        for row in pairs:
            if row["label"] not in ("flag", "review"):
                continue
            key = frozenset((row["id_a"], row["id_b"]))
            decision = "duplicate" if key in gt_dupes else "not_duplicate"
            db.upsert_review_decision(conn, row["id_a"], row["id_b"], decision, "simulated")
            decided += 1
        counts = db.decision_counts(conn)
    print(f"decisions: {decided} persisted (reviewer=simulated: {counts})")
    print("next    : invoice-dedupe tune")
    return 0


def cmd_tune(args: argparse.Namespace) -> int:
    """Fit the tuned model from review decisions and report before/after."""
    from .evaluate import ground_truth
    from .normalize import normalize_invoice
    from .tuning import build_model, rescore_pairs, rule_fire_period_analysis

    with db.connect() as conn:
        db.init_schema(conn)
        if args.reset:
            n = db.deactivate_tuning(conn)
            print(f"reset   : deactivated {n} tuning model(s); base thresholds apply")
            invoices = db.load_invoices(conn)
            if invoices:
                base = detect(invoices)
                db.replace_pairs(conn, base.pairs)
                print(f"rescored: base model ({sum(1 for p in base.pairs if p.label == 'flag')} flagged, "
                      f"{sum(1 for p in base.pairs if p.label == 'review')} review)")
            return 0
        invoices = db.load_invoices(conn)
        if not invoices:
            print("error   : database is empty; run `invoice-dedupe seed-demo` first")
            return 1
        decisions = db.all_review_decisions(conn)
        if not decisions:
            print(
                "error   : no review decisions found; triage pairs in the web app "
                "or run `invoice-dedupe simulate-feedback`"
            )
            return 1
        config = ScoringConfig()
        base = detect(invoices, config)
        periods = {inv.id: normalize_invoice(inv).billing_period for inv in invoices}
        model_id = f"tune_{uuid.uuid4().hex[:8]}"
        try:
            model, train = build_model(
                invoices, base.pairs, decisions, model_id=model_id,
                epochs=args.epochs,
            )
        except ValueError as exc:
            print(f"error   : {exc}")
            return 1

        after_pairs = rescore_pairs(base.pairs, model, periods)
        gt = ground_truth(invoices)
        before = evaluate(
            invoices, base.pairs,
            flag_threshold=config.flag_threshold,
            review_threshold=config.review_threshold,
        )
        after = evaluate(
            invoices, after_pairs,
            flag_threshold=model.flag_threshold,
            review_threshold=model.review_threshold,
        )
        model.eval_metrics = {
            "before": {
                "flag_threshold": config.flag_threshold,
                "review_threshold": config.review_threshold,
                "precision": round(before.precision, 4),
                "recall": round(before.recall, 4),
                "f1": round(before.f1, 4),
                "false_positives": before.fp,
                "flagged": before.flagged_pairs,
                "review_queue": before.review_pairs,
            },
            "after": {
                "flag_threshold": model.flag_threshold,
                "review_threshold": model.review_threshold,
                "precision": round(after.precision, 4),
                "recall": round(after.recall, 4),
                "f1": round(after.f1, 4),
                "false_positives": after.fp,
                "flagged": after.flagged_pairs,
                "review_queue": after.review_pairs,
            },
            "rule_fire_period_analysis": rule_fire_period_analysis(base.pairs, gt, periods),
        }

        print("=== invoice-dedupe tune ===")
        print(f"model   : {model_id} from {train['n_decisions']} decisions "
              f"({train['n_duplicate']} duplicate / {train['n_not_duplicate']} not-duplicate)")
        print(f"weights : {json.dumps({k: round(v, 4) for k, v in model.weights.items()})}")
        print(f"          vendor={0.25} amount={0.30} (pinned); thresholds: "
              f"flag>={model.flag_threshold} review>={model.review_threshold}")
        b, a = model.eval_metrics["before"], model.eval_metrics["after"]
        print(f"before  : P={b['precision']:.4f} R={b['recall']:.4f} F1={b['f1']:.4f} "
              f"fp={b['false_positives']} flagged={b['flagged']} review={b['review_queue']}")
        print(f"after   : P={a['precision']:.4f} R={a['recall']:.4f} F1={a['f1']:.4f} "
              f"fp={a['false_positives']} flagged={a['flagged']} review={a['review_queue']}")
        analysis = model.eval_metrics["rule_fire_period_analysis"]
        print("rule fires (re-invoice pattern) by ground truth and billing period:")
        for key in sorted(analysis):
            print(f"  {key}: {analysis[key]}")
        diff = [
            k for k in ("same_period_dup", "diff_period_dup")
            if analysis.get(k) and analysis.get(k.replace("dup", "notdup"))
        ]
        for k in diff:
            print(
                f"  note  : {analysis[k]} rule-fire duplicates and "
                f"{analysis.get(k.replace('dup', 'notdup'), 0)} rule-fire false positives "
                "share the same period profile; those false positives are not "
                "separable with the available features and are reported as-is"
            )

        if not args.no_apply:
            db.insert_tuning(conn, model)
            db.activate_tuning(conn, model_id)
            pairs = rescore_pairs(base.pairs, model, periods)
            db.replace_pairs(conn, pairs)
            print(f"applied : model {model_id} active; pairs re-scored "
                  f"({sum(1 for p in pairs if p.label == 'flag')} flagged, "
                  f"{sum(1 for p in pairs if p.label == 'review')} review)")
            print("next    : invoice-dedupe serve   # dashboard shows the tuned model")
        else:
            print("dry run : nothing applied (--no-apply)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="invoice-dedupe",
        description="Detect duplicate invoices to prevent double payments",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="generate a labeled synthetic dataset")
    p_gen.add_argument("--n", type=int, default=10_000)
    p_gen.add_argument("--dup-rate", type=float, default=0.02)
    p_gen.add_argument("--seed", type=int, default=42)
    p_gen.add_argument("--out", default="datasets")
    p_gen.set_defaults(func=cmd_generate)

    p_run = sub.add_parser("run", help="run detection over a JSONL dataset")
    p_run.add_argument("--dataset", required=True)
    p_run.add_argument("--out", default="pairs.jsonl")
    _add_scoring_args(p_run)
    p_run.set_defaults(func=cmd_run)

    p_eval = sub.add_parser("evaluate", help="evaluate detection results against ground truth")
    p_eval.add_argument("--dataset", required=True)
    p_eval.add_argument("--pairs", required=True)
    _add_scoring_args(p_eval)
    p_eval.set_defaults(func=cmd_evaluate)

    p_demo = sub.add_parser("demo", help="generate + detect + evaluate in one process")
    p_demo.add_argument("--n", type=int, default=10_000)
    p_demo.add_argument("--dup-rate", type=float, default=0.02)
    p_demo.add_argument("--seed", type=int, default=42)
    _add_scoring_args(p_demo)
    p_demo.set_defaults(func=cmd_demo)

    p_init = sub.add_parser("init-db", help="create the PostgreSQL schema (idempotent)")
    p_init.set_defaults(func=cmd_init_db)

    p_seed = sub.add_parser(
        "seed-demo",
        help="load a seeded synthetic dataset (with ground truth) into PostgreSQL for the web app",
    )
    p_seed.add_argument("--n", type=int, default=10_000)
    p_seed.add_argument("--dup-rate", type=float, default=0.02)
    p_seed.add_argument("--seed", type=int, default=42)
    p_seed.add_argument("--reset", action="store_true", help="replace existing data")
    p_seed.set_defaults(func=cmd_seed_demo)

    p_serve = sub.add_parser("serve", help="run the FastAPI service")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.set_defaults(func=cmd_serve)

    p_worker = sub.add_parser("worker", help="run the job worker (extraction/detection)")
    p_worker.add_argument("--poll-seconds", type=float, default=1.0)
    p_worker.set_defaults(func=cmd_worker)

    p_pdf = sub.add_parser("make-pdf", help="write a sample text-layer invoice PDF")
    p_pdf.add_argument("--out", default="sample_invoice.pdf")
    p_pdf.add_argument("--vendor", default="Northwind Logistics, Ltd.")
    p_pdf.add_argument("--invoice-no", default="INV-2026-0001")
    p_pdf.add_argument("--invoice-date", default="2026-09-14")
    p_pdf.add_argument("--amount", default="12,500.00")
    p_pdf.add_argument("--tax-id", default="12-3456789")
    p_pdf.set_defaults(func=cmd_make_pdf)

    p_photo = sub.add_parser(
        "make-photo", help="write a crumpled-invoice photo PNG (vision-LLM fixture)"
    )
    p_photo.add_argument("--out", default="sample_invoice_photo.png")
    p_photo.add_argument("--vendor", default="Northwind Logistics, Ltd.")
    p_photo.add_argument("--invoice-no", default="INV-2026-0001")
    p_photo.add_argument("--invoice-date", default="2026-09-14")
    p_photo.add_argument("--amount", default="12,500.00")
    p_photo.add_argument("--tax-id", default="12-3456789")
    p_photo.add_argument("--seed", type=int, default=7)
    p_photo.set_defaults(func=cmd_make_photo)

    p_sim = sub.add_parser(
        "simulate-feedback",
        help="persist reviewer decisions from seeded ground truth (demo input for tune)",
    )
    p_sim.set_defaults(func=cmd_simulate_feedback)

    p_tune = sub.add_parser(
        "tune",
        help="fit thresholds/weights from review decisions, evaluate before/after, apply",
    )
    p_tune.add_argument("--epochs", type=int, default=30_000)
    p_tune.add_argument("--no-apply", action="store_true", help="evaluate and report only")
    p_tune.add_argument(
        "--reset", action="store_true", help="deactivate tuning and re-score with the base model"
    )
    p_tune.set_defaults(func=cmd_tune)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
