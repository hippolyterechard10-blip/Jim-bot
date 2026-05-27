#!/usr/bin/env python3
"""
llm_usage_cli.py — CLI for LLM cost observability.

Usage:
    python3 llm_usage_cli.py stats [--hours 24]
    python3 llm_usage_cli.py recent [--limit 20]
    python3 llm_usage_cli.py log --bot zeus --provider openrouter --model openai/gpt-4o-mini \\
                                  --in 1234 --out 567 [--task routine] [--purpose "..."]
    python3 llm_usage_cli.py export-csv > out.csv
    python3 llm_usage_cli.py prices

For automated agent integration : import the `llm_usage` module directly
and call `llm_usage.log(...)`.
"""
from __future__ import annotations
import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import llm_usage


def cmd_stats(args):
    s = llm_usage.stats(window_hours=args.hours)
    if "error" in s:
        print(f"  error: {s['error']}", file=sys.stderr); return 1

    tot = s["total"]
    print(f"\n  LLM usage — last {args.hours}h")
    print(f"  {'─' * 60}")
    print(f"  Calls           : {tot['calls']:>8d}")
    print(f"  Input tokens    : {tot['in_tok']:>8,d}")
    print(f"  Cached input    : {tot['cached_tok']:>8,d}")
    print(f"  Output tokens   : {tot['out_tok']:>8,d}")
    print(f"  Cost (USD)      : ${tot['cost_usd']:>9.4f}")

    if s["by_bot"]:
        print(f"\n  By bot")
        print(f"  {'bot':<14} {'calls':>8} {'cost $':>10}")
        for r in s["by_bot"]:
            print(f"  {r['bot_id']:<14} {r['calls']:>8d} {r['cost_usd']:>10.4f}")

    if s["by_model"]:
        print(f"\n  By model")
        print(f"  {'model':<40} {'calls':>8} {'cost $':>10}")
        for r in s["by_model"]:
            print(f"  {r['model']:<40} {r['calls']:>8d} {r['cost_usd']:>10.4f}")

    if s["by_task_type"]:
        print(f"\n  By task_type")
        print(f"  {'task':<14} {'calls':>8} {'cost $':>10}")
        for r in s["by_task_type"]:
            print(f"  {r['task_type']:<14} {r['calls']:>8d} {r['cost_usd']:>10.4f}")
    print()
    return 0


def cmd_recent(args):
    rows = llm_usage.recent(limit=args.limit)
    if not rows:
        print("  (no rows)")
        return 0
    print(f"\n  Last {len(rows)} LLM calls")
    print(f"  {'─' * 100}")
    print(f"  {'timestamp':<20} {'bot':<8} {'model':<35} {'in':>6} {'out':>6} {'cost $':>8} {'task':<10}")
    for r in rows:
        ts = (r['timestamp'] or '')[:19]
        print(f"  {ts:<20} {r['bot_id']:<8} {r['model']:<35} "
              f"{r['input_tokens']:>6d} {r['output_tokens']:>6d} {r['cost_usd']:>8.4f} "
              f"{(r['task_type'] or '-'):<10}")
    print()
    return 0


def cmd_log(args):
    ok = llm_usage.log(
        bot_id=args.bot,
        provider=args.provider,
        model=args.model,
        input_tokens=args.input,
        output_tokens=args.output,
        cached_input_tokens=args.cached_input,
        task_type=args.task,
        purpose=args.purpose,
        session_id=args.session,
        latency_ms=args.latency,
        success=not args.failed,
    )
    if ok:
        cost = llm_usage.compute_cost(args.model, args.input, args.output, args.cached_input)
        print(f"  logged: {args.bot} {args.model} in={args.input} out={args.output} cost=${cost:.6f}")
        return 0
    print("  log failed", file=sys.stderr)
    return 1


def cmd_export_csv(args):
    conn = sqlite3.connect(str(llm_usage.DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM llm_usage ORDER BY id ASC").fetchall()
    conn.close()
    if not rows:
        print("# no rows", file=sys.stderr); return 1
    w = csv.writer(sys.stdout)
    w.writerow(rows[0].keys())
    for r in rows:
        w.writerow(list(r))
    return 0


def cmd_prices(args):
    print(f"\n  Cost table ($/M tokens)")
    print(f"  {'─' * 60}")
    print(f"  {'model':<42} {'input':>10} {'output':>10}")
    for model, (in_p, out_p) in sorted(llm_usage.MODEL_PRICES.items()):
        print(f"  {model:<42} {in_p:>10.3f} {out_p:>10.3f}")
    print(f"\n  Cached input discount factor: {llm_usage.CACHED_INPUT_DISCOUNT}")
    print(f"  DB path                     : {llm_usage.DB_PATH}")
    print()
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("stats", help="aggregated usage stats")
    s.add_argument("--hours", type=int, default=24)
    s.set_defaults(func=cmd_stats)

    r = sub.add_parser("recent", help="last N calls")
    r.add_argument("--limit", type=int, default=20)
    r.set_defaults(func=cmd_recent)

    l = sub.add_parser("log", help="manually log a call")
    l.add_argument("--bot",      required=True)
    l.add_argument("--provider", required=True)
    l.add_argument("--model",    required=True)
    l.add_argument("--input",    type=int, default=0,  dest="input")
    l.add_argument("--output",   type=int, default=0,  dest="output")
    l.add_argument("--cached-input", type=int, default=0, dest="cached_input")
    l.add_argument("--task",     default=None,                  dest="task")
    l.add_argument("--purpose",  default=None,                  dest="purpose")
    l.add_argument("--session",  default=None,                  dest="session")
    l.add_argument("--latency",  type=int, default=None,         dest="latency")
    l.add_argument("--failed",   action="store_true",            dest="failed")
    l.set_defaults(func=cmd_log)

    e = sub.add_parser("export-csv", help="dump all rows as CSV to stdout")
    e.set_defaults(func=cmd_export_csv)

    p = sub.add_parser("prices", help="print model price table")
    p.set_defaults(func=cmd_prices)

    rc = sub.add_parser("recompute-costs",
                        help="recompute cost_usd on every row using current MODEL_PRICES")
    rc.set_defaults(func=lambda args: (
        (lambda r: (print(f"  rows: {r['rows']}\n  updated: {r['updated']}\n"
                           f"  before total cost: ${r['before']}\n"
                           f"  after  total cost: ${r['after']}\n"
                           f"  delta: ${r['delta']:+}") or 0))(llm_usage.recompute_all_costs())
    ))

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
