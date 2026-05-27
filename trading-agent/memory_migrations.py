"""
memory_migrations.py — Idempotent SQLite schema migrations for Jim Bot.

Run at startup before TradingMemory uses the DB. Each migration is identified by
an integer version; only un-applied migrations execute. Tracks state in the
schema_migrations table.

Multi-bot ready : bot_id columns default to 'jim' so existing data is preserved
without modification.
"""
from __future__ import annotations
import logging
import sqlite3
from typing import List, Tuple

logger = logging.getLogger(__name__)

# (version, name, [list of SQL statements to execute atomically])
MIGRATIONS: List[Tuple[int, str, List[str]]] = [
    (1, "schema_migrations_table", [
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version    INTEGER PRIMARY KEY,
               name       TEXT NOT NULL,
               applied_at TEXT NOT NULL DEFAULT (datetime('now'))
           )""",
    ]),
    (2, "add_bot_id_columns", [
        "ALTER TABLE trades          ADD COLUMN bot_id TEXT NOT NULL DEFAULT 'jim'",
        "ALTER TABLE agent_decisions ADD COLUMN bot_id TEXT NOT NULL DEFAULT 'jim'",
        "ALTER TABLE trade_analyses  ADD COLUMN bot_id TEXT NOT NULL DEFAULT 'jim'",
        "ALTER TABLE agent_memory    ADD COLUMN bot_id TEXT NOT NULL DEFAULT 'jim'",
    ]),
    (3, "add_strategy_version", [
        "ALTER TABLE trades ADD COLUMN strategy_version TEXT",
    ]),
    (4, "indices_for_perf", [
        # Multi-tenant filtering : on filtre toujours par bot_id en code v2
        "CREATE INDEX IF NOT EXISTS idx_trades_bot_status        ON trades(bot_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_trades_bot_symbol_entry  ON trades(bot_id, symbol, entry_at)",
        "CREATE INDEX IF NOT EXISTS idx_decisions_bot_decided    ON agent_decisions(bot_id, decided_at)",
        "CREATE INDEX IF NOT EXISTS idx_analyses_bot_trade       ON trade_analyses(bot_id, trade_id)",
        # JSON path indices pour analytics (json_extract)
        "CREATE INDEX IF NOT EXISTS idx_trades_strategy_source   ON trades(json_extract(market_context, '$.strategy_source'))",
        "CREATE INDEX IF NOT EXISTS idx_trades_mode              ON trades(json_extract(market_context, '$.mode'))",
        "CREATE INDEX IF NOT EXISTS idx_trades_status_entry_at   ON trades(status, entry_at)",
    ]),
    (5, "agent_memory_per_bot_key", [
        # agent_memory.key était UNIQUE global → devient unique par (bot_id, key)
        # SQLite ne supporte pas ALTER pour drop constraint; on contourne via new index
        # (l'ancien UNIQUE sur key reste actif jusqu'à recréation table — acceptable car
        #  agent_memory est currently empty, donc pas de conflit en pratique)
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_bot_key ON agent_memory(bot_id, key)",
    ]),
    (6, "llm_usage_observability", [
        # Audit #4 Phase A — observability LLM (Zeus, Jim, Claude Code, etc.)
        # Pure observability table — n'affecte pas le runtime trading
        """CREATE TABLE IF NOT EXISTS llm_usage (
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
           )""",
        "CREATE INDEX IF NOT EXISTS idx_llm_bot_ts        ON llm_usage(bot_id, timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_llm_model_ts      ON llm_usage(model, timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_llm_session       ON llm_usage(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_llm_task_type_ts  ON llm_usage(task_type, timestamp)",
    ]),
]


def _applied_versions(conn: sqlite3.Connection) -> set:
    try:
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        return {r[0] for r in rows}
    except sqlite3.OperationalError:
        # schema_migrations table doesn't exist yet
        return set()


def run_migrations(db_path: str) -> int:
    """Apply all pending migrations. Returns count of newly applied migrations."""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA busy_timeout = 5000")
    applied = _applied_versions(conn)

    newly_applied = 0
    for version, name, statements in MIGRATIONS:
        if version in applied:
            continue
        try:
            for stmt in statements:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (version, name),
            )
            conn.commit()
            logger.info(f"[MIGRATION] applied v{version}: {name}")
            newly_applied += 1
        except sqlite3.OperationalError as e:
            # ALTER TABLE ... ADD COLUMN fails if column already exists ; treat as already applied
            msg = str(e).lower()
            if "duplicate column" in msg or "already exists" in msg:
                logger.info(f"[MIGRATION] v{version} already applied (idempotent skip): {name}")
                # Still record it as applied to skip on next run
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, name) VALUES (?, ?)",
                    (version, name),
                )
                conn.commit()
            else:
                logger.error(f"[MIGRATION] v{version} {name} FAILED: {e}")
                raise

    conn.close()
    return newly_applied


def get_migration_state(db_path: str) -> list[dict]:
    """Return list of applied migrations with timestamps."""
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        rows = conn.execute(
            "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        return [{"version": r[0], "name": r[1], "applied_at": r[2]} for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db = sys.argv[1] if len(sys.argv) > 1 else "trading_memory.db"
    n = run_migrations(db)
    print(f"Applied {n} new migration(s)")
    print("Current state:")
    for m in get_migration_state(db):
        print(f"  v{m['version']} {m['name']:<30}  applied_at={m['applied_at']}")
