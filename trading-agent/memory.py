import sqlite3
import json
import logging
from datetime import datetime, timezone
from typing import Optional
from contextlib import contextmanager

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT UNIQUE NOT NULL,
    alpaca_order_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL,
    exit_price REAL,
    stop_loss REAL,
    take_profit REAL,
    status TEXT NOT NULL DEFAULT 'open',
    pnl REAL,
    pnl_pct REAL,
    entry_at TEXT NOT NULL,
    exit_at TEXT,
    hold_duration_min REAL,
    close_reason TEXT,
    market_context TEXT,
    entry_snapshot TEXT,
    exit_vs_target REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS agent_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT,
    symbol TEXT,
    decision TEXT NOT NULL,
    confidence REAL,
    reasoning TEXT NOT NULL,
    market_data TEXT,
    decided_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS trade_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    outcome TEXT NOT NULL,
    pnl REAL,
    analysis TEXT NOT NULL,
    lessons TEXT,
    mistakes TEXT,
    strategy_adj TEXT,
    analyzed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS agent_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    value TEXT NOT NULL,
    category TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

class TradingMemory:
    def __init__(self, db_path="trading_memory.db"):
        self.db_path = db_path
        self._init_db()
        logger.info(f"✅ TradingMemory ready: {db_path}")

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            for col, ctype in [("entry_snapshot", "TEXT"), ("exit_vs_target", "REAL")]:
                try:
                    conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {ctype}")
                except Exception:
                    pass

    def backfill_strategy_source(self) -> int:
        """One-time migration: set strategy_source on trades that don't have it yet.
        Uses symbol type as proxy ONLY for historical alpaca_sync'd data where
        crypto=geometric and stock=gapper is accurate. Future native trades will
        have strategy_source set at log_trade_open time."""
        updated = 0
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT trade_id, symbol, market_context FROM trades "
                    "WHERE json_extract(market_context,'$.strategy_source') IS NULL"
                ).fetchall()
                for row in rows:
                    trade_id = row["trade_id"]
                    symbol   = row["symbol"] or ""
                    mc_raw   = row["market_context"]
                    try:
                        ctx = json.loads(mc_raw) if mc_raw else {}
                    except Exception:
                        ctx = {}
                    inferred = "geometric" if "/" in symbol else "gapper"
                    ctx["strategy_source"] = inferred
                    conn.execute(
                        "UPDATE trades SET market_context=? WHERE trade_id=?",
                        (json.dumps(ctx), trade_id)
                    )
                    updated += 1
            if updated:
                logger.info(f"[MEMORY] 🔧 Backfilled strategy_source on {updated} trades")
        except Exception as e:
            logger.error(f"backfill_strategy_source error: {e}")
        return updated

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise
        finally:
            conn.close()

    def log_trade_open(self, trade_id, symbol, side, qty, entry_price,
                       stop_loss=None, take_profit=None,
                       alpaca_order_id=None, market_context=None, entry_snapshot=None):
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO trades (trade_id,alpaca_order_id,symbol,side,qty,entry_price,stop_loss,take_profit,status,entry_at,market_context,entry_snapshot) VALUES (?,?,?,?,?,?,?,?,'open',?,?,?)",
                    (trade_id, alpaca_order_id, symbol, side, qty, entry_price,
                     stop_loss, take_profit,
                     datetime.now(timezone.utc).isoformat(),
                     json.dumps(market_context) if market_context else None,
                     json.dumps(entry_snapshot) if entry_snapshot else None)
                )
            return True
        except Exception as e:
            logger.error(f"log_trade_open error: {e}")
            return False

    def log_trade_close(self, trade_id, exit_price, close_reason, pnl=None, pnl_pct=None):
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT entry_at, entry_price, qty, side, take_profit FROM trades WHERE trade_id=?",
                    (trade_id,)
                ).fetchone()
                if not row:
                    return False
                exit_at = datetime.now(timezone.utc)
                entry_at = datetime.fromisoformat(row["entry_at"])
                duration = (exit_at - entry_at).total_seconds() / 60
                if pnl is None:
                    m = 1 if row["side"] == "buy" else -1
                    pnl = (exit_price - row["entry_price"]) * row["qty"] * m
                if pnl_pct is None and row["entry_price"] > 0:
                    pnl_pct = (pnl / (row["entry_price"] * row["qty"])) * 100
                take_profit_val = row["take_profit"]
                exit_vs_target = None
                if take_profit_val and take_profit_val > 0 and row["entry_price"] > 0:
                    target_gain = (take_profit_val - row["entry_price"]) / row["entry_price"]
                    actual_gain = (exit_price - row["entry_price"]) / row["entry_price"]
                    exit_vs_target = round((actual_gain / target_gain) * 100, 1) if target_gain != 0 else None
                conn.execute(
                    "UPDATE trades SET exit_price=?,exit_at=?,hold_duration_min=?,close_reason=?,pnl=?,pnl_pct=?,exit_vs_target=?,status='closed' WHERE trade_id=?",
                    (exit_price, exit_at.isoformat(), duration, close_reason,
                     round(pnl,4), round(pnl_pct,4) if pnl_pct else None,
                     exit_vs_target, trade_id)
                )
            return True
        except Exception as e:
            logger.error(f"log_trade_close error: {e}")
            return False

    def get_open_trades(self):
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM trades WHERE status='open' ORDER BY entry_at DESC").fetchall()
            return [dict(r) for r in rows]

    def get_recent_trades(self, limit=20, symbol=None):
        with self._conn() as conn:
            if symbol:
                rows = conn.execute("SELECT * FROM trades WHERE symbol=? ORDER BY entry_at DESC LIMIT ?", (symbol, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM trades ORDER BY entry_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_closed_trades_unanalyzed(self):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT t.* FROM trades t LEFT JOIN trade_analyses ta ON t.trade_id=ta.trade_id WHERE t.status='closed' AND ta.trade_id IS NULL ORDER BY t.exit_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def log_decision(self, decision, reasoning, symbol=None, trade_id=None, confidence=None, market_data=None):
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO agent_decisions (trade_id,symbol,decision,confidence,reasoning,market_data) VALUES (?,?,?,?,?,?)",
                    (trade_id, symbol, decision, confidence, reasoning,
                     json.dumps(market_data) if market_data else None)
                )
            return True
        except Exception as e:
            logger.error(f"log_decision error: {e}")
            return False

    def get_recent_decisions(self, limit=10, symbol=None):
        with self._conn() as conn:
            if symbol:
                rows = conn.execute("SELECT * FROM agent_decisions WHERE symbol=? ORDER BY decided_at DESC LIMIT ?", (symbol, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM agent_decisions ORDER BY decided_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def save_trade_analysis(self, trade_id, symbol, outcome, pnl, analysis, lessons=None, mistakes=None, strategy_adj=None):
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO trade_analyses (trade_id,symbol,outcome,pnl,analysis,lessons,mistakes,strategy_adj) VALUES (?,?,?,?,?,?,?,?)",
                    (trade_id, symbol, outcome, pnl, analysis,
                     json.dumps(lessons) if lessons else None,
                     json.dumps(mistakes) if mistakes else None,
                     strategy_adj)
                )
            return True
        except Exception as e:
            logger.error(f"save_trade_analysis error: {e}")
            return False

    def get_analyses(self, limit=10):
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM trade_analyses ORDER BY analyzed_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def compute_performance_stats(self, symbol=None):
        with self._conn() as conn:
            q = "SELECT * FROM trades WHERE status='closed'"
            params = []
            if symbol:
                q += " AND symbol=?"
                params.append(symbol)
            rows = conn.execute(q, params).fetchall()
            trades = [dict(r) for r in rows]
        if not trades:
            return {"total_trades": 0}
        real_trades = [t for t in trades if t.get("close_reason") != "partial_profit_remainder"]
        pnls = [t["pnl"] for t in real_trades if t["pnl"] is not None]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_win = sum(wins) if wins else 0
        gross_loss = abs(sum(losses)) if losses else 0
        pf = gross_win / gross_loss if gross_loss > 0 else 999
        cumulative = peak = max_dd = 0
        for p in pnls:
            cumulative += p
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd
        by_asset = {}
        for t in trades:
            s = t["symbol"]
            if t["pnl"] is not None:
                by_asset.setdefault(s, []).append(t["pnl"])
        asset_pnl = {s: sum(v) for s, v in by_asset.items()}
        return {
            "total_trades": len(pnls),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate": round(len(wins)/len(pnls)*100, 2) if pnls else 0,
            "total_pnl": round(sum(pnls), 2),
            "avg_win": round(sum(wins)/len(wins), 2) if wins else 0,
            "avg_loss": round(sum(losses)/len(losses), 2) if losses else 0,
            "profit_factor": round(pf, 2),
            "max_drawdown": round(max_dd, 2),
            "best_asset": max(asset_pnl, key=asset_pnl.get) if asset_pnl else None,
            "worst_asset": min(asset_pnl, key=asset_pnl.get) if asset_pnl else None,
            "asset_pnl": {k: round(v,2) for k,v in asset_pnl.items()},
        }

    def set_memory(self, key, value, category="strategy"):
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO agent_memory (key,value,category,updated_at) VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,category=excluded.category,updated_at=excluded.updated_at",
                    (key, json.dumps(value), category, datetime.now(timezone.utc).isoformat())
                )
            return True
        except Exception as e:
            logger.error(f"set_memory error: {e}")
            return False

    def get_memory(self, key, default=None):
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM agent_memory WHERE key=?", (key,)).fetchone()
            if row:
                try:
                    return json.loads(row["value"])
                except:
                    return row["value"]
            return default

    def get_all_memory(self, category=None):
        with self._conn() as conn:
            if category:
                rows = conn.execute("SELECT key,value,category,updated_at FROM agent_memory WHERE category=?", (category,)).fetchall()
            else:
                rows = conn.execute("SELECT key,value,category,updated_at FROM agent_memory").fetchall()
            result = {}
            for row in rows:
                try:
                    result[row["key"]] = {"value": json.loads(row["value"]), "category": row["category"]}
                except:
                    result[row["key"]] = {"value": row["value"], "category": row["category"]}
            return result

    def get_context_for_agent(self, symbol=None):
        stats = self.compute_performance_stats(symbol)
        recent = self.get_recent_trades(limit=20, symbol=symbol)
        memory = self.get_all_memory()
        lines = ["=== AGENT MEMORY ==="]
        if stats.get("total_trades", 0) > 0:
            lines.append(f"Performance: {stats['total_trades']} trades | Win rate: {stats['win_rate']}% | P&L: ${stats['total_pnl']}")
        if recent:
            lines.append("Recent trades:")
            for t in recent:
                pnl_str = f"${t['pnl']:.2f}" if t.get("pnl") else "open"
                lines.append(f"  {t['symbol']} {t['side']} | {pnl_str}")
        strategy = {k: v["value"] for k,v in memory.items() if v.get("category") == "strategy"}
        if strategy:
            lines.append("Strategy insights:")
            for k,v in list(strategy.items())[:3]:
                lines.append(f"  • {k}: {v}")
        return "\n".join(lines)
