#!/usr/bin/env python3
"""
llm_usage_ingest.py — Import OpenClaw trajectories into llm_usage table.

OpenClaw already logs every `model.completed` event in
~/.openclaw/agents/<agent>/sessions/<session>.trajectory.jsonl
with full token usage in `data.promptCache.lastCallUsage`.

This script walks all such files, computes USD cost via the price table,
and inserts rows into llm_usage. Idempotent : uses external_id =
"<sessionId>:<seq>" with a UNIQUE constraint, so re-runs are safe.

Usage:
    python3 llm_usage_ingest.py                  # ingest everything
    python3 llm_usage_ingest.py --since YYYY-MM-DD
    python3 llm_usage_ingest.py --dry-run         # show what would be inserted
    python3 llm_usage_ingest.py --agent main      # filter one agent
"""
from __future__ import annotations
import argparse
import glob
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import llm_usage

logger = logging.getLogger(__name__)

TRAJECTORY_ROOT = Path.home() / ".openclaw" / "agents"


# ─── Token extraction ─────────────────────────────────────────────────────────

def _extract_usage(entry: dict) -> tuple[int, int, int]:
    """Returns (input_tokens, output_tokens, cached_input_tokens).
    Returns (0,0,0) for failed / aborted entries."""
    data = entry.get("data", {})

    # Path 1: promptCache.lastCallUsage (most reliable for OpenClaw)
    pc = data.get("promptCache", {})
    last = pc.get("lastCallUsage", {}) if isinstance(pc, dict) else {}
    if isinstance(last, dict):
        in_tok     = int(last.get("input")     or 0)
        out_tok    = int(last.get("output")    or 0)
        cache_read = int(last.get("cacheRead") or 0)
        if in_tok or out_tok or cache_read:
            # input already excludes cacheRead in OpenClaw's accounting
            return in_tok, out_tok, cache_read

    # Path 2: messagesSnapshot[*].usage cumulative (fallback)
    msgs = data.get("messagesSnapshot", [])
    if isinstance(msgs, list):
        total_in = total_out = 0
        for m in msgs:
            if isinstance(m, dict):
                u = m.get("usage", {})
                if isinstance(u, dict):
                    total_in  += int(u.get("input")  or 0)
                    total_out += int(u.get("output") or 0)
        if total_in or total_out:
            return total_in, total_out, 0

    return 0, 0, 0


def _was_successful(entry: dict) -> bool:
    """Skip entries that didn't actually produce a billable response."""
    data = entry.get("data", {})
    # Hard fails
    if data.get("aborted")               : return False
    if data.get("externalAbort")         : return False
    if data.get("timedOut")               : return False
    if data.get("terminalError") in (
        "non_deliverable_terminal_turn", None
    ):
        # null terminalError is fine ; the named one signals no output
        if data.get("terminalError") == "non_deliverable_terminal_turn":
            # may still have billed tokens (we still record them)
            pass
    return True


def _infer_bot_id_from_path(fp: str) -> str:
    """~/.openclaw/agents/<agent>/sessions/...  → bot_id = <agent>
    Special case: 'main' → 'zeus' (per AGENTS.md)."""
    parts = Path(fp).parts
    try:
        i = parts.index("agents")
        agent = parts[i + 1]
    except (ValueError, IndexError):
        agent = "unknown"
    return "zeus" if agent == "main" else agent


# ─── Ingester core ────────────────────────────────────────────────────────────

def ingest_file(fp: str, conn: sqlite3.Connection, dry_run: bool = False) -> dict:
    """Process one trajectory file. Returns counts dict."""
    bot_id = _infer_bot_id_from_path(fp)
    inserted = skipped_existing = skipped_zero = skipped_failed = 0
    rows_to_insert = []

    with open(fp, "rb") as fh:
        for raw_line in fh:
            try:
                entry = json.loads(raw_line)
            except Exception:
                continue
            if entry.get("type") != "model.completed":
                continue

            session_id = entry.get("sessionId") or entry.get("traceId", "")
            seq        = entry.get("seq", 0)
            if not session_id:
                continue
            external_id = f"{session_id}:{seq}"

            if not _was_successful(entry):
                skipped_failed += 1
                continue

            in_tok, out_tok, cached_tok = _extract_usage(entry)
            if in_tok == 0 and out_tok == 0 and cached_tok == 0:
                skipped_zero += 1
                continue

            model    = entry.get("modelId", "")
            provider = entry.get("provider", "openrouter")
            ts       = entry.get("ts", "")
            try:
                # Normalize ISO ts → SQLite TEXT
                if ts.endswith("Z"):
                    ts_norm = datetime.fromisoformat(ts.replace("Z","+00:00")).isoformat()
                else:
                    ts_norm = ts
            except Exception:
                ts_norm = ts

            cost = llm_usage.compute_cost(model, in_tok, out_tok, cached_tok)

            rows_to_insert.append((
                ts_norm, bot_id, session_id, provider, model,
                in_tok, out_tok, cached_tok, cost,
                "ingested",                  # task_type marker
                None,                          # purpose
                None, 1,                       # latency_ms, success
                json.dumps({"seq": seq, "source": "trajectory"}),
                external_id,
            ))

    if rows_to_insert and not dry_run:
        for row in rows_to_insert:
            try:
                conn.execute(
                    """INSERT INTO llm_usage
                       (timestamp, bot_id, session_id, provider, model,
                        input_tokens, output_tokens, cached_input_tokens, cost_usd,
                        task_type, purpose, latency_ms, success, meta, external_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    row,
                )
                inserted += 1
            except sqlite3.IntegrityError:
                skipped_existing += 1
        conn.commit()

    return {
        "file":             os.path.basename(fp),
        "agent_bot":        bot_id,
        "candidates":       len(rows_to_insert),
        "inserted":         inserted,
        "skipped_existing": skipped_existing,
        "skipped_zero":     skipped_zero,
        "skipped_failed":   skipped_failed,
    }


def ingest_all(since: str | None = None, agent_filter: str | None = None,
               dry_run: bool = False, db_path: str | None = None) -> dict:
    db = db_path or str(llm_usage.DB_PATH)
    pattern = str(TRAJECTORY_ROOT / "*" / "sessions" / "*.trajectory.jsonl")
    files = sorted(glob.glob(pattern))

    if agent_filter:
        files = [f for f in files if f"/agents/{agent_filter}/" in f]

    if since:
        try:
            cutoff = datetime.fromisoformat(since).timestamp()
            files = [f for f in files if os.path.getmtime(f) >= cutoff]
        except Exception:
            pass

    conn = sqlite3.connect(db, timeout=10)
    conn.execute("PRAGMA busy_timeout = 5000")
    # Ensure the table exists (in case ingester runs before migrations)
    try:
        from memory_migrations import run_migrations
        run_migrations(db)
    except Exception as e:
        logger.warning(f"[ingest] migrations runner: {e}")

    totals = {"files": len(files), "candidates": 0, "inserted": 0,
              "skipped_existing": 0, "skipped_zero": 0, "skipped_failed": 0}
    per_agent = {}

    for fp in files:
        r = ingest_file(fp, conn, dry_run=dry_run)
        for k in ("candidates", "inserted", "skipped_existing", "skipped_zero", "skipped_failed"):
            totals[k] += r[k]
        a = r["agent_bot"]
        per_agent.setdefault(a, {"files": 0, "inserted": 0, "candidates": 0})
        per_agent[a]["files"] += 1
        per_agent[a]["inserted"] += r["inserted"]
        per_agent[a]["candidates"] += r["candidates"]

    conn.close()
    totals["per_agent"] = per_agent
    return totals


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since",  help="filter by file mtime >= YYYY-MM-DD")
    ap.add_argument("--agent",  help="filter trajectories to a single agent (e.g. main, jim-bot)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    print(f"Scanning trajectories in {TRAJECTORY_ROOT}")
    if args.since:  print(f"  filter since   : {args.since}")
    if args.agent:  print(f"  filter agent   : {args.agent}")
    if args.dry_run: print("  DRY RUN (no DB writes)")
    print()

    r = ingest_all(since=args.since, agent_filter=args.agent, dry_run=args.dry_run)

    print(f"Files scanned         : {r['files']}")
    print(f"Candidates found      : {r['candidates']}")
    print(f"Inserted              : {r['inserted']}")
    print(f"Skipped (already in DB): {r['skipped_existing']}")
    print(f"Skipped (zero tokens) : {r['skipped_zero']}")
    print(f"Skipped (failed call) : {r['skipped_failed']}")
    print()
    print(f"Per-agent breakdown:")
    for a, s in sorted(r["per_agent"].items()):
        print(f"  {a:<14} files={s['files']:>4}  candidates={s['candidates']:>5}  inserted={s['inserted']:>5}")


if __name__ == "__main__":
    main()
