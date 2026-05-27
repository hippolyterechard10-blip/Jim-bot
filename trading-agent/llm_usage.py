"""
llm_usage.py — Observability for LLM API calls (Zeus, Jim, Claude Code, others).

PHILOSOPHY
    Pure logger. Does NOT make LLM calls itself. The caller has just received
    a response from its provider; it passes token counts here, this module
    computes USD cost and persists to SQLite.

    Defensive : every call is wrapped — observability layer must NEVER crash
    the calling agent.

USAGE
    import llm_usage

    llm_usage.log(
        bot_id        = "zeus",
        provider      = "openrouter",
        model         = "openai/gpt-4o-mini",
        input_tokens  = 1234,
        output_tokens = 567,
        task_type     = "routine",   # routine | audit | decision | debug | other
        session_id    = "2026-05-27_zeus_telegram",
        purpose       = "answering user query about Jim status",
    )

    # Cached tokens (Anthropic prompt caching) — counted with discount
    llm_usage.log(
        bot_id=..., provider=..., model=..., input_tokens=2000,
        cached_input_tokens=8000,   # cached portion (~90% discount applied)
        output_tokens=300,
    )

DB
    Writes to trading_memory.db / llm_usage table (created by migration v6).
    A custom path can be passed via the env var LLM_USAGE_DB.

COST MODEL
    Static price table (~/M tokens). Edit MODEL_PRICES below when providers
    publish new rates or you add a model.
"""
from __future__ import annotations
import json
import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ─── DB path resolution ───────────────────────────────────────────────────────
DEFAULT_DB = Path(__file__).parent / "trading_memory.db"
DB_PATH = Path(os.getenv("LLM_USAGE_DB", str(DEFAULT_DB)))


# ─── Cost table : ($/M tokens for input, $/M tokens for output) ───────────────
# Sources : provider pricing pages (May 2026). Update as needed.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    # ── OpenAI via OpenRouter ────────────────────────────────────────────────
    "openai/gpt-4o-mini":          (0.150, 0.600),
    "openai/gpt-4o":               (2.500, 10.000),
    "openai/gpt-4-turbo":          (10.000, 30.000),
    "openai/o1-mini":              (3.000, 12.000),
    "openai/o1":                   (15.000, 60.000),
    # ── Anthropic via OpenRouter ─────────────────────────────────────────────
    "anthropic/claude-3.5-sonnet": (3.000, 15.000),
    "anthropic/claude-3.5-haiku":  (1.000, 5.000),
    "anthropic/claude-3-haiku":    (0.250, 1.250),
    "anthropic/claude-3-opus":     (15.000, 75.000),
    # ── Anthropic direct (Claude Code) — same prices ─────────────────────────
    "claude-opus-4-7":             (15.000, 75.000),
    "claude-opus-4-7[1m]":         (15.000, 75.000),
    "claude-sonnet-4-6":           (3.000, 15.000),
    "claude-haiku-4-5":            (0.250, 1.250),
    "claude-haiku-4-5-20251001":   (0.250, 1.250),
    # ── Google ───────────────────────────────────────────────────────────────
    "google/gemini-2.5-flash":     (0.075, 0.300),
    "google/gemini-2.5-pro":       (1.250, 10.000),
    "google/gemini-2.0-flash":     (0.075, 0.300),
    # ── DeepSeek ─────────────────────────────────────────────────────────────
    "deepseek/deepseek-chat":      (0.270, 1.100),
    "deepseek/deepseek-r1":        (0.550, 2.190),
    # ── Meta ─────────────────────────────────────────────────────────────────
    "meta-llama/llama-3.1-70b":    (0.350, 0.400),
    "meta-llama/llama-3.1-8b":     (0.055, 0.055),
}

# Cached input tokens are billed at ~10% of standard input by Anthropic
# (90% discount). Most other providers don't publicly support prompt caching
# yet ; the discount factor below is conservative (assume 10% if unknown).
CACHED_INPUT_DISCOUNT = 0.10


def _resolve_price(model: str) -> tuple[float, float]:
    """Returns (input_price_per_M, output_price_per_M). Falls back to a
    conservative middle-ground if model isn't known."""
    if model in MODEL_PRICES:
        return MODEL_PRICES[model]
    # Heuristics : pattern-match the family if specific version missing
    m = model.lower()
    if "gpt-4o-mini" in m or "claude-3-haiku" in m: return (0.150, 0.600)
    if "gpt-4o"     in m or "claude-3.5-sonnet" in m: return (3.000, 15.000)
    if "opus"       in m: return (15.000, 75.000)
    if "gemini" in m and "flash" in m: return (0.075, 0.300)
    if "haiku" in m: return (1.000, 5.000)
    if "sonnet" in m: return (3.000, 15.000)
    logger.debug(f"[llm_usage] unknown model '{model}' — using midprice fallback")
    return (1.000, 5.000)


def compute_cost(model: str, input_tokens: int, output_tokens: int,
                 cached_input_tokens: int = 0) -> float:
    """Compute USD cost given token counts + model. Tolerates None as 0."""
    p_in, p_out = _resolve_price(model)
    in_tok       = max(0, (input_tokens or 0) - (cached_input_tokens or 0))
    cached_tok   = max(0, cached_input_tokens or 0)
    out_tok      = max(0, output_tokens or 0)
    cost = (
        in_tok * p_in / 1_000_000
        + cached_tok * p_in * CACHED_INPUT_DISCOUNT / 1_000_000
        + out_tok * p_out / 1_000_000
    )
    return round(cost, 6)


# ─── SQLite logger ────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS llm_usage (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           TEXT NOT NULL DEFAULT (datetime('now')),
    bot_id              TEXT NOT NULL,
    session_id          TEXT,
    provider            TEXT NOT NULL,
    model               TEXT NOT NULL,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd            REAL NOT NULL DEFAULT 0,
    task_type           TEXT,
    purpose             TEXT,
    latency_ms          INTEGER,
    success             INTEGER NOT NULL DEFAULT 1,
    meta                TEXT
)
"""


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Create table if missing (defensive — migration v6 normally handles it)."""
    conn.execute(_DDL)


def log(
    bot_id: str,
    provider: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_input_tokens: int = 0,
    task_type: str | None = None,
    purpose: str | None = None,
    session_id: str | None = None,
    latency_ms: int | None = None,
    success: bool = True,
    cost_usd: float | None = None,
    meta: dict | None = None,
    db_path: str | None = None,
) -> bool:
    """Log one LLM call. Returns True if persisted. Never raises.

    If cost_usd is None, it's computed from the price table.
    """
    try:
        if cost_usd is None:
            cost_usd = compute_cost(model, input_tokens, output_tokens, cached_input_tokens)

        path = db_path or str(DB_PATH)
        conn = sqlite3.connect(path, timeout=5)
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            _ensure_table(conn)
            conn.execute(
                """INSERT INTO llm_usage
                   (bot_id, session_id, provider, model,
                    input_tokens, output_tokens, cached_input_tokens, cost_usd,
                    task_type, purpose, latency_ms, success, meta)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    bot_id,
                    session_id,
                    provider,
                    model,
                    int(input_tokens or 0),
                    int(output_tokens or 0),
                    int(cached_input_tokens or 0),
                    float(cost_usd or 0),
                    task_type,
                    purpose,
                    int(latency_ms) if latency_ms is not None else None,
                    1 if success else 0,
                    json.dumps(meta) if meta else None,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        # Never propagate — observability must not break the caller
        logger.warning(f"[llm_usage] log failed (non-fatal): {e}")
        return False


def new_session_id(bot_id: str) -> str:
    """Helper to mint a session id when the agent doesn't have one yet."""
    return f"{time.strftime('%Y%m%d-%H%M%S')}_{bot_id}_{uuid.uuid4().hex[:6]}"


# ─── Aggregations (for dashboard + CLI) ───────────────────────────────────────

def stats(window_hours: int = 24, db_path: str | None = None) -> dict[str, Any]:
    """Aggregated usage over a rolling window."""
    path = db_path or str(DB_PATH)
    try:
        conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")

        cutoff_sql = f"datetime('now', '-{int(window_hours)} hours')"

        total = conn.execute(
            f"""SELECT COUNT(*) AS calls,
                       COALESCE(SUM(input_tokens),0) AS in_tok,
                       COALESCE(SUM(output_tokens),0) AS out_tok,
                       COALESCE(SUM(cached_input_tokens),0) AS cached_tok,
                       COALESCE(SUM(cost_usd),0) AS cost_usd
                FROM llm_usage WHERE timestamp >= {cutoff_sql}"""
        ).fetchone()

        by_bot = [dict(r) for r in conn.execute(
            f"""SELECT bot_id,
                       COUNT(*) AS calls,
                       COALESCE(SUM(cost_usd),0) AS cost_usd,
                       COALESCE(SUM(input_tokens),0) AS in_tok,
                       COALESCE(SUM(output_tokens),0) AS out_tok
                FROM llm_usage WHERE timestamp >= {cutoff_sql}
                GROUP BY bot_id ORDER BY cost_usd DESC"""
        ).fetchall()]

        by_model = [dict(r) for r in conn.execute(
            f"""SELECT model,
                       COUNT(*) AS calls,
                       COALESCE(SUM(cost_usd),0) AS cost_usd
                FROM llm_usage WHERE timestamp >= {cutoff_sql}
                GROUP BY model ORDER BY cost_usd DESC LIMIT 10"""
        ).fetchall()]

        by_task = [dict(r) for r in conn.execute(
            f"""SELECT COALESCE(task_type,'unspecified') AS task_type,
                       COUNT(*) AS calls,
                       COALESCE(SUM(cost_usd),0) AS cost_usd
                FROM llm_usage WHERE timestamp >= {cutoff_sql}
                GROUP BY task_type ORDER BY cost_usd DESC"""
        ).fetchall()]

        conn.close()
        return {
            "window_hours":  window_hours,
            "total":         dict(total) if total else {"calls": 0, "cost_usd": 0},
            "by_bot":        by_bot,
            "by_model":      by_model,
            "by_task_type":  by_task,
        }
    except Exception as e:
        logger.warning(f"[llm_usage] stats failed: {e}")
        return {"error": str(e), "window_hours": window_hours}


def recent(limit: int = 50, db_path: str | None = None) -> list[dict]:
    """Most recent N calls."""
    path = db_path or str(DB_PATH)
    try:
        conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT timestamp, bot_id, provider, model,
                      input_tokens, output_tokens, cached_input_tokens,
                      cost_usd, task_type, purpose, session_id
               FROM llm_usage ORDER BY id DESC LIMIT ?""",
            (int(limit),)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"[llm_usage] recent failed: {e}")
        return []
