"""invoice-dedupe CLI: generate, run, evaluate, demo."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
