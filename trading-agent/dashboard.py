import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional
from flask import Flask, jsonify, render_template_string, request
from flask_cors import CORS
import config
from memory import TradingMemory
from analyzer import TradeAnalyzer

logger = logging.getLogger(__name__)
app = Flask(__name__)
CORS(app)
_memory: Optional[TradingMemory] = None
_analyzer: Optional[TradeAnalyzer] = None
_scanner = None
_regime  = None
_agent   = None

def init_dashboard(memory, analyzer=None, scanner=None, regime=None, agent=None):
    global _memory, _analyzer, _scanner, _regime, _agent
    _memory  = memory
    _analyzer = analyzer
    _scanner = scanner
    _regime  = regime
    _agent   = agent

@app.route("/api/stats")
def api_stats():
    if not _memory: return jsonify({})
    return jsonify(_memory.compute_performance_stats())

@app.route("/api/stats/periods")
def api_stats_periods():
    from flask import request as flask_req
    if not _memory:
        return jsonify({})
    expert = flask_req.args.get("expert", "all")
    try:
        conn = sqlite3.connect(_memory.db_path, timeout=5)

        src_filter = ""
        if expert == "gap":
            src_filter = "AND json_extract(market_context, '$.strategy_source') = 'gapper'"
        elif expert == "geo":
            src_filter = "AND json_extract(market_context, '$.strategy_source') = 'geometric'"

        def _pstats(since):
            date_clause = f"AND exit_at >= '{since}'" if since else ""
            rows = conn.execute(f"""
                SELECT pnl FROM trades
                WHERE status = 'closed'
                  AND (close_reason IS NULL
                       OR close_reason NOT IN ('position_reconciled', 'synced_close'))
                  AND (json_extract(market_context, '$.source') IS NULL
                       OR json_extract(market_context, '$.source')
                          NOT IN ('order_sync', 'order_sync_synthetic'))
                  {src_filter}
                  {date_clause}
            """).fetchall()
            pnls   = [r[0] for r in rows if r[0] is not None]
            wins   = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            total  = len(pnls)
            return {
                "trades":   total,
                "wins":     len(wins),
                "losses":   len(losses),
                "win_rate": round(len(wins) / total * 100, 1) if total else None,
                "pnl":      round(sum(pnls), 4) if pnls else 0.0,
            }

        result = {
            "week":  _pstats(_period_start("week")),
            "month": _pstats(_period_start("month")),
            "ytd":   _pstats(_period_start("ytd")),
            "all":   _pstats(None),
        }
        conn.close()
        return jsonify(result)
    except Exception as e:
        logger.error(f"api_stats_periods error: {e}")
        return jsonify({"error": str(e)})

@app.route("/api/trades/open")
def api_open_trades():
    if not _memory: return jsonify([])
    trades = _memory.get_open_trades()
    for t in trades:
        raw = t.get("market_context") or {}
        if isinstance(raw, str):
            try: raw = json.loads(raw)
            except: raw = {}
        t["strategy_source"] = raw.get("strategy_source")
        t["deployed"] = round(
            float(t.get("entry_price") or 0) * float(t.get("qty") or 0), 2
        )
    return jsonify(trades)

@app.route("/api/trades/recent")
def api_recent_trades():
    if not _memory: return jsonify([])
    return jsonify(_memory.get_recent_trades(limit=20))

@app.route("/api/decisions/recent")
def api_recent_decisions():
    if not _memory: return jsonify([])
    decisions = _memory.get_recent_decisions(limit=15)
    for d in decisions:
        md = d.get("market_data")
        try:
            ctx = json.loads(md) if isinstance(md, str) else (md or {})
            d["strategy_source"] = ctx.get("strategy_source")
        except Exception:
            d["strategy_source"] = None
    return jsonify(decisions)

@app.route("/api/analyses/recent")
def api_recent_analyses():
    if not _memory: return jsonify([])
    analyses = _memory.get_analyses(limit=5)
    for a in analyses:
        for field in ["lessons","mistakes"]:
            if a.get(field) and isinstance(a[field], str):
                try: a[field] = json.loads(a[field])
                except: pass
    return jsonify(analyses)

@app.route("/api/anomalies")
def api_anomalies():
    if not _analyzer: return jsonify([])
    return jsonify(_analyzer.detect_performance_anomalies())

@app.route("/api/movers")
def api_movers():
    if not _scanner:
        return jsonify({"movers": [], "error": "scanner not initialized"})
    try:
        movers = _scanner.get_top_movers(top_n=6)
        return jsonify({"movers": movers, "ts": datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        return jsonify({"movers": [], "error": str(e)})

@app.route("/api/sentiment")
def api_sentiment():
    if not _scanner:
        return jsonify({"sentiment": "neutral", "score": 0, "headlines": [], "alerts": []})
    try:
        result = _scanner.analyze_sentiment()
        return jsonify(result)
    except Exception as e:
        return jsonify({"sentiment": "neutral", "score": 0, "headlines": [], "alerts": [], "error": str(e)})

@app.route("/api/calendar")
def api_calendar():
    if not _scanner:
        return jsonify({"event": None, "note": ""})
    try:
        result = _scanner.check_economic_calendar()
        return jsonify(result)
    except Exception as e:
        return jsonify({"event": None, "note": "", "error": str(e)})

@app.route("/api/regime")
def api_regime():
    if not _regime:
        return jsonify({"regime": "UNKNOWN", "vix": None, "dxy": None, "score_long_threshold": 60, "score_short_threshold": 30})
    try:
        import re as _re
        params  = _regime.get_params()
        context = _regime.build_regime_context()
        # Inject VIX into params if present in context string
        vix_m = _re.search(r"VIX:\s*([\d.]+)", context)
        if vix_m:
            params = dict(params)
            params["vix"] = float(vix_m.group(1))
        return jsonify({
            "regime":   params.get("regime", "UNKNOWN"),
            "params":   params,
            "context":  context,
        })
    except Exception as e:
        return jsonify({"regime": "UNKNOWN", "error": str(e)})

def _period_start(period: str) -> str | None:
    now = datetime.now(timezone.utc)
    if period == "today":
        return now.date().isoformat()
    if period == "week":
        monday = now.date() - __import__('datetime').timedelta(days=now.weekday())
        return monday.isoformat()
    if period == "month":
        return now.date().replace(day=1).isoformat()
    if period == "ytd":
        return now.date().replace(month=1, day=1).isoformat()
    return None  # all

@app.route("/api/closed-today")
def api_closed_today():
    from flask import request as flask_req
    if not _memory:
        return jsonify({"closed": [], "date": ""})
    try:
        period    = flask_req.args.get("period", "today")
        since     = _period_start(period)
        today     = datetime.now(timezone.utc).date().isoformat()
        conn      = sqlite3.connect(_memory.db_path, timeout=10)
        c         = conn.cursor()
        if since:
            c.execute("""
                SELECT symbol,
                       SUM(pnl)         AS total_pnl,
                       COUNT(*)         AS trade_count,
                       SUM(qty)         AS total_qty_sold,
                       MAX(exit_at)     AS last_exit_at,
                       GROUP_CONCAT(DISTINCT close_reason) AS reasons
                FROM trades
                WHERE status = 'closed'
                  AND (close_reason IS NULL OR close_reason != 'position_reconciled')
                  AND exit_at >= ?
                GROUP BY symbol
                ORDER BY MAX(exit_at) DESC
            """, (since,))
        else:
            c.execute("""
                SELECT symbol,
                       SUM(pnl)         AS total_pnl,
                       COUNT(*)         AS trade_count,
                       SUM(qty)         AS total_qty_sold,
                       MAX(exit_at)     AS last_exit_at,
                       GROUP_CONCAT(DISTINCT close_reason) AS reasons
                FROM trades
                WHERE status = 'closed'
                  AND (close_reason IS NULL OR close_reason != 'position_reconciled')
                GROUP BY symbol
                ORDER BY MAX(exit_at) DESC
            """)
        rows = c.fetchall()
        conn.close()
        closed = [
            {
                "symbol":      r[0],
                "pnl":         round(r[1], 6) if r[1] is not None else 0,
                "trade_count": r[2],
                "qty_sold":    round(r[3], 8) if r[3] is not None else 0,
                "last_exit":   r[4] or "",
                "reasons":     r[5] or "",
            }
            for r in rows
        ]
        return jsonify({"closed": closed, "date": today, "period": period})
    except Exception as e:
        logger.error(f"api_closed_today error: {e}")
        return jsonify({"closed": [], "error": str(e)})

@app.route("/api/trades/individual")
def api_trades_individual():
    from flask import request as flask_req
    if not _memory:
        return jsonify({"trades": []})
    try:
        period = flask_req.args.get("period", "today")
        since  = _period_start(period)
        limit  = min(int(flask_req.args.get("limit", 300)), 500)
        conn   = sqlite3.connect(_memory.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        if since:
            c.execute("""
                SELECT trade_id, symbol, side, qty, entry_price, exit_price,
                       pnl, pnl_pct, hold_duration_min,
                       close_reason, entry_at, exit_at,
                       entry_snapshot, exit_vs_target, market_context
                FROM trades
                WHERE status = 'closed'
                  AND exit_at >= ?
                ORDER BY exit_at DESC LIMIT ?
            """, (since, limit))
        else:
            c.execute("""
                SELECT trade_id, symbol, side, qty, entry_price, exit_price,
                       pnl, pnl_pct, hold_duration_min,
                       close_reason, entry_at, exit_at,
                       entry_snapshot, exit_vs_target, market_context
                FROM trades
                WHERE status = 'closed'
                ORDER BY exit_at DESC LIMIT ?
            """, (limit,))
        rows = c.fetchall()
        conn.close()
        trades = []
        for r in rows:
            snap = {}
            if r["entry_snapshot"]:
                try:
                    snap = json.loads(r["entry_snapshot"])
                except Exception:
                    snap = {}
            mc = {}
            if r["market_context"]:
                try:
                    mc = json.loads(r["market_context"])
                except Exception:
                    mc = {}
            geo_ctx = None
            if mc.get("strategy_source") == "geometric":
                raw_atr = mc.get("atr")
                geo_ctx = {
                    "confluence":     mc.get("confluence"),
                    "structure":      mc.get("structure"),
                    "rsi_divergence": mc.get("rsi_divergence"),
                    "atr":            round(float(raw_atr), 6) if raw_atr is not None else None,
                    "target_midpoint": mc.get("target_midpoint"),
                    "patterns":       mc.get("patterns") or [],
                    "level":          mc.get("level"),
                }
            trades.append({
                "trade_id":       r["trade_id"],
                "symbol":         r["symbol"],
                "side":           r["side"],
                "qty":            round(r["qty"], 8) if r["qty"] else 0,
                "entry_price":    r["entry_price"],
                "exit_price":     r["exit_price"],
                "pnl":            round(r["pnl"], 6)    if r["pnl"]     is not None else None,
                "pnl_pct":        round(r["pnl_pct"], 4) if r["pnl_pct"] is not None else None,
                "hold_min":       round(r["hold_duration_min"], 1) if r["hold_duration_min"] else None,
                "close_reason":   r["close_reason"],
                "entry_at":       r["entry_at"],
                "exit_at":        r["exit_at"],
                "exit_vs_target": r["exit_vs_target"],
                "strategy_source": mc.get("strategy_source"),
                "geo_context":     geo_ctx,
            })
        return jsonify({"trades": trades, "period": period})
    except Exception as e:
        logger.error(f"api_trades_individual error: {e}")
        return jsonify({"trades": [], "error": str(e)})

@app.route("/api/trades/<trade_id>")
def api_trade_detail(trade_id):
    if not _memory:
        return jsonify({"error": "not ready"}), 503
    try:
        conn = sqlite3.connect(_memory.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        trade = conn.execute("""
            SELECT trade_id, symbol, side, qty, entry_price, exit_price,
                   pnl, pnl_pct, hold_duration_min, close_reason,
                   entry_at, exit_at, stop_loss, take_profit,
                   exit_vs_target, market_context
            FROM trades WHERE trade_id = ?
        """, (trade_id,)).fetchone()
        if not trade:
            conn.close()
            return jsonify({"error": "not found"}), 404
        analysis = conn.execute("""
            SELECT outcome, pnl, analysis, lessons, mistakes
            FROM trade_analyses WHERE trade_id = ?
            ORDER BY rowid DESC LIMIT 1
        """, (trade_id,)).fetchone()
        conn.close()
        mc = {}
        try: mc = json.loads(trade["market_context"] or "{}")
        except: pass
        geo = None
        if mc.get("strategy_source") == "geometric":
            geo = {
                "confluence":      mc.get("confluence"),
                "structure":       mc.get("structure"),
                "rsi_divergence":  mc.get("rsi_divergence"),
                "atr":             mc.get("atr"),
                "target_midpoint": mc.get("target_midpoint"),
                "patterns":        mc.get("patterns") or [],
                "level":           mc.get("level"),
                "side":            mc.get("side"),
            }
        return jsonify({
            "trade_id":        trade["trade_id"],
            "symbol":          trade["symbol"],
            "side":            trade["side"],
            "qty":             trade["qty"],
            "entry_price":     trade["entry_price"],
            "exit_price":      trade["exit_price"],
            "pnl":             trade["pnl"],
            "pnl_pct":         trade["pnl_pct"],
            "hold_min":        trade["hold_duration_min"],
            "close_reason":    trade["close_reason"],
            "entry_at":        trade["entry_at"],
            "exit_at":         trade["exit_at"],
            "stop_loss":       trade["stop_loss"],
            "take_profit":     trade["take_profit"],
            "exit_vs_target":  trade["exit_vs_target"],
            "strategy_source": mc.get("strategy_source"),
            "geo_context":     geo,
            "analysis": {
                "outcome":  analysis["outcome"],
                "text":     analysis["analysis"],
                "lessons":  analysis["lessons"],
                "mistakes": analysis["mistakes"],
            } if analysis else None,
        })
    except Exception as e:
        logger.error(f"api_trade_detail error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/analysis")
def api_analysis():
    if not _memory:
        return jsonify({})
    try:
        conn = sqlite3.connect(_memory.db_path, timeout=10)
        c    = conn.cursor()

        # ── Expert filter — based on strategy_source in market_context ──
        expert = request.args.get("expert", "all").lower()
        if expert == "gap":
            ef = "AND json_extract(market_context, '$.strategy_source') = 'gapper'"
        elif expert == "geo":
            ef = "AND json_extract(market_context, '$.strategy_source') = 'geometric'"
        else:
            ef = ""

        # ── All closed trades ──────────────────────────────────────────
        c.execute(f"""
            SELECT symbol, pnl, pnl_pct, hold_duration_min, close_reason, exit_at
            FROM trades WHERE status='closed' {ef}
            ORDER BY exit_at
        """)
        trades = c.fetchall()

        # ── Daily P&L (last 30 days) ───────────────────────────────────
        c.execute(f"""
            SELECT DATE(exit_at) AS day, SUM(pnl) AS day_pnl, COUNT(*) AS cnt
            FROM trades WHERE status='closed' {ef}
            GROUP BY day ORDER BY day DESC LIMIT 30
        """)
        daily_rows = c.fetchall()

        # ── P&L by asset ───────────────────────────────────────────────
        c.execute(f"""
            SELECT symbol, SUM(pnl) AS total, COUNT(*) AS cnt,
                   AVG(pnl) AS avg_pnl, AVG(hold_duration_min) AS avg_hold
            FROM trades WHERE status='closed' {ef}
            GROUP BY symbol ORDER BY total DESC
        """)
        asset_rows = c.fetchall()

        # ── Close reason breakdown ─────────────────────────────────────
        c.execute(f"""
            SELECT close_reason, COUNT(*) AS cnt, SUM(pnl) AS total_pnl
            FROM trades WHERE status='closed' {ef}
            GROUP BY close_reason ORDER BY cnt DESC
        """)
        reason_rows = c.fetchall()

        conn.close()

        # ── Compute core metrics ───────────────────────────────────────
        total  = len(trades)
        wins   = [t for t in trades if (t[1] or 0) > 0]
        losses = [t for t in trades if (t[1] or 0) < 0]
        pnls   = [t[1] or 0 for t in trades]
        holds  = [t[3] or 0 for t in trades if t[3]]

        gross_win  = sum(t[1] for t in wins)  if wins   else 0
        gross_loss = sum(t[1] for t in losses) if losses else 0
        win_rate   = (len(wins) / total * 100) if total else 0
        loss_rate  = 100 - win_rate
        avg_win    = (gross_win / len(wins))   if wins   else 0
        avg_loss   = (gross_loss / len(losses)) if losses else 0
        pf         = (gross_win / abs(gross_loss)) if gross_loss else 999
        expectancy = (win_rate/100 * avg_win) + (loss_rate/100 * avg_loss)

        best_trade  = max(trades, key=lambda t: t[1] or 0) if trades else None
        worst_trade = min(trades, key=lambda t: t[1] or 0) if trades else None
        avg_hold    = (sum(holds) / len(holds)) if holds else 0

        # ── Streak calculation ─────────────────────────────────────────
        streak, max_win_streak, max_loss_streak = 0, 0, 0
        cur_streak_type = None
        for t in trades:
            is_win = (t[1] or 0) >= 0
            if cur_streak_type is None or is_win == cur_streak_type:
                streak += 1
                cur_streak_type = is_win
            else:
                if cur_streak_type:
                    max_win_streak  = max(max_win_streak, streak)
                else:
                    max_loss_streak = max(max_loss_streak, streak)
                streak = 1
                cur_streak_type = is_win
        if cur_streak_type is True:
            max_win_streak  = max(max_win_streak, streak)
        elif cur_streak_type is False:
            max_loss_streak = max(max_loss_streak, streak)
        current_streak      = {"type": "win" if cur_streak_type else "loss", "count": streak} if trades else None

        # ── Avg trades per active day ──────────────────────────────────
        active_days = len(set(t[5][:10] for t in trades if t[5])) if trades else 1
        avg_trades_per_day = total / active_days if active_days else 0

        return jsonify({
            "total_trades":      total,
            "winning_trades":    len(wins),
            "losing_trades":     len(losses),
            "win_rate":          round(win_rate, 1),
            "profit_factor":     round(pf, 2) if pf != 999 else 999,
            "expectancy":        round(expectancy, 4),
            "gross_win":         round(gross_win, 4),
            "gross_loss":        round(gross_loss, 4),
            "total_pnl":         round(sum(pnls), 4),
            "avg_win":           round(avg_win, 4),
            "avg_loss":          round(avg_loss, 4),
            "avg_hold_min":      round(avg_hold, 1),
            "avg_trades_per_day": round(avg_trades_per_day, 1),
            "best_trade":  {"symbol": best_trade[0],  "pnl": round(best_trade[1],4),  "reason": best_trade[4]} if best_trade  else None,
            "worst_trade": {"symbol": worst_trade[0], "pnl": round(worst_trade[1],4), "reason": worst_trade[4]} if worst_trade else None,
            "current_streak":    current_streak,
            "max_win_streak":    max_win_streak,
            "max_loss_streak":   max_loss_streak,
            "daily_pnl":  [{"date": r[0], "pnl": round(r[1],4), "trades": r[2]} for r in daily_rows],
            "by_asset":   [{"symbol": r[0], "pnl": round(r[1],4), "trades": r[2], "avg_pnl": round(r[3],4), "avg_hold_min": round(r[4] or 0,1)} for r in asset_rows],
            "by_reason":  [{"reason": r[0], "trades": r[1], "pnl": round(r[2],4)} for r in reason_rows],
        })
    except Exception as e:
        logger.error(f"api_analysis error: {e}")
        return jsonify({"error": str(e)})

@app.route("/api/account")
def api_account():
    if not _agent:
        return jsonify({"equity": 0, "cash": 0, "buying_power": 0, "portfolio_value": 0})
    try:
        account = _agent.broker.get_account()
        cash = float(account.cash)

        # Compute live equity using our own price feed (bypasses Alpaca's ~15-min delayed marks)
        live_equity = cash
        live_unrealized = 0.0
        positions_live = []
        try:
            positions = _agent.broker.get_positions()
            for p in positions:
                qty   = float(p.qty)
                entry = float(p.avg_entry_price)
                sym_raw = p.symbol  # e.g. LINKUSD
                # Convert LINKUSD → LINK/USD for bar lookup
                if sym_raw.endswith("USD") and "/" not in sym_raw:
                    sym = sym_raw[:-3] + "/USD"
                elif sym_raw.endswith("USDT") and "/" not in sym_raw:
                    sym = sym_raw[:-4] + "/USDT"
                else:
                    sym = sym_raw
                live_price = _agent.broker.get_live_price(sym) if "/" in sym else None
                if live_price is None:
                    bars = _agent.broker.get_bars(sym, "1Min", limit=2)
                    if bars is not None and not bars.empty:
                        live_price = float(bars["close"].iloc[-1])
                    else:
                        live_price = float(p.current_price)  # fallback to Alpaca mark
                side_m = 1.0 if p.side == "long" else -1.0
                unrealized = (live_price - entry) * qty * side_m
                live_unrealized += unrealized
                live_equity += qty * live_price
                positions_live.append({
                    "symbol":      sym,
                    "qty":         qty,
                    "entry_price": round(entry, 6),
                    "live_price":  round(live_price, 6),
                    "alpaca_mark": round(float(p.current_price), 6),
                    "unrealized":  round(unrealized, 4),
                })
        except Exception as pe:
            logger.warning(f"live equity calc error: {pe}")
            live_equity = float(account.portfolio_value)

        return jsonify({
            "equity":           float(account.equity),
            "cash":             cash,
            "buying_power":     float(account.buying_power),
            "portfolio_value":  float(account.portfolio_value),
            "last_equity":      float(account.last_equity),
            "live_equity":      round(live_equity, 4),
            "live_unrealized":  round(live_unrealized, 4),
            "positions_live":   positions_live,
        })
    except Exception as e:
        logger.error(f"api_account error: {e}")
        return jsonify({"equity": 0, "cash": 0, "buying_power": 0, "portfolio_value": 0, "error": str(e)})

@app.route("/api/stops")
def api_stops():
    """Return trailing stop prices per symbol from agent's in-memory state."""
    if not _agent:
        return jsonify({"stops": {}})
    try:
        stops = {}
        high  = getattr(_agent, "_high_water", {})
        trail = getattr(_agent, "_trail_pcts", {})
        low   = getattr(_agent, "_low_water",  {})
        for sym, h in high.items():
            pct = trail.get(sym, 0.05)
            stops[sym] = round(h * (1 - pct), 4)
        for sym, l in low.items():
            stops[sym] = round(l * (1 + 0.03), 4)
        return jsonify({"stops": stops})
    except Exception as e:
        return jsonify({"stops": {}, "error": str(e)})

@app.route("/api/health")
def api_health():
    return jsonify({"status":"ok","timestamp":datetime.now(timezone.utc).isoformat()})

@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)

@app.route("/source")
def api_source():
    """Return all Python source files concatenated as plain text — shareable in a browser."""
    from flask import Response
    base = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(base, ".."))

    PYTHON_FILES = [
        "trading-agent/main.py",
        "trading-agent/config.py",
        "trading-agent/broker.py",
        "trading-agent/risk.py",
        "trading-agent/memory.py",
        "trading-agent/strategy.py",
        "trading-agent/regime.py",
        "trading-agent/scanner.py",
        "trading-agent/correlations.py",
        "trading-agent/geometry.py",
        "trading-agent/synthesis.py",
        "trading-agent/agent.py",
        "trading-agent/dashboard.py",
    ]

    SEP = "=" * 80
    chunks = ["JIM BOT — Python Source\n" + SEP + "\n"]

    total_lines = 0
    for rel in PYTHON_FILES:
        abs_path = os.path.join(project_root, rel)
        if not os.path.exists(abs_path):
            continue
        try:
            with open(abs_path, encoding="utf-8", errors="replace") as f:
                content = f.read()
            n = len(content.splitlines())
            total_lines += n
            chunks.append(f"\n{SEP}\nFILE: {rel}  ({n} lines)\n{SEP}\n\n{content}\n")
        except Exception as e:
            chunks.append(f"\n{SEP}\nFILE: {rel}  — ERROR: {e}\n{SEP}\n")

    chunks.append(f"\n{SEP}\nTotal: {len(PYTHON_FILES)} files, {total_lines} lines\n{SEP}\n")
    return Response("".join(chunks), mimetype="text/plain; charset=utf-8")

AGENT_FILES = ["agent", "analyzer", "dashboard", "notifier", "news_intelligence", "synthesis"]

@app.route("/api/source/<filename>")
def source_file(filename):
    """Return a single Python source file by name (no extension)."""
    from flask import Response as _R
    if filename not in AGENT_FILES:
        return "Not found", 404
    base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, f"{filename}.py")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return _R(f.read(), status=200, mimetype="text/plain; charset=utf-8")
    except Exception:
        return "File not found", 404

@app.route("/api/experts/stats")
def api_experts_stats():
    """Returns independent P&L and capital for each expert.

    Capital source of truth = Alpaca account equity (not DB PnL, which can contain
    mis-priced synced_close entries).

    Logic:
      - capital_start = initial per expert (from capital_lock.json initial_ keys, default 513)
      - gap_capital_now  = capital_start + sum(V2 gap pnl from DB)   [gap trades are reliable]
      - geo_capital_now  = alpaca_equity - gap_capital_now            [derived from real account]
      - total_pnl        = capital_now - capital_start                [always consistent]
    """
    if not _memory:
        return jsonify({})
    try:
        import json as _json

        # ── Read initial capital from capital_lock.json ────────────────────────
        lock = {}
        try:
            with open(config._CAPITAL_LOCK_FILE) as f:
                lock = _json.load(f)
        except Exception:
            pass

        # ── Fetch real account equity from Alpaca ──────────────────────────────
        alpaca_equity = None
        if _agent:
            try:
                alpaca_equity = float(_agent.broker.api.get_account().equity)
            except Exception as e:
                logger.warning(f"api_experts_stats: could not fetch Alpaca equity: {e}")

        all_trades = _memory.get_recent_trades(limit=500)

        # ── Per-source trade filtering ─────────────────────────────────────────
        by_source = {"gapper": [], "geometric": []}
        for t in all_trades:
            ctx = t.get("market_context") or {}
            if isinstance(ctx, str):
                try: ctx = _json.loads(ctx)
                except: ctx = {}
            src = ctx.get("strategy_source")
            if src in by_source:
                by_source[src].append(t)

        # ── Stats filter (V2-only, for win-rate / avg-win / avg-loss) ─────────
        V2_EXCLUDE = ("partial_profit_remainder", "synced_close", "orphan_close", "position_reconciled")

        # ── Gap capital: DB PnL is reliable (V2 closes only, few trades) ──────
        gap_initial = lock.get("initial_gapper", 513.0)
        gap_v2_closed = [t for t in by_source["gapper"]
                         if t.get("status") == "closed"
                         and t.get("close_reason") not in V2_EXCLUDE]
        gap_v2_pnl   = sum(t.get("pnl") or 0 for t in gap_v2_closed)
        gap_capital_now = round(gap_initial + gap_v2_pnl, 2)

        result = {}
        for source in ["gapper", "geometric"]:
            trades     = by_source[source]
            v2_closed  = [t for t in trades if t.get("status") == "closed"
                          and t.get("close_reason") not in V2_EXCLUDE]
            pnls   = [t.get("pnl") or 0 for t in v2_closed]
            wins   = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p < 0]

            capital_start = lock.get(f"initial_{source}", 513.0)

            if source == "gapper":
                capital_now = gap_capital_now
            else:
                # Geo = real account equity minus what gap holds
                # Falls back to gap_initial + total_v2_pnl if Alpaca unavailable
                if alpaca_equity is not None:
                    capital_now = round(alpaca_equity - gap_capital_now, 2)
                else:
                    geo_v2_pnl  = sum(t.get("pnl") or 0 for t in v2_closed)
                    capital_now = round(capital_start + geo_v2_pnl, 2)

            total_pnl = round(capital_now - capital_start, 4)

            open_trades = [t for t in trades if t.get("status") == "open"]
            live_unrealized = 0.0
            if _agent:
                for ot in open_trades:
                    sym  = ot.get("symbol", "")
                    qty  = float(ot.get("qty") or 0)
                    entry = float(ot.get("entry_price") or 0)
                    if not sym or not qty or not entry:
                        continue
                    try:
                        lp = _agent.broker.get_live_price(sym) if "/" in sym else None
                        if lp:
                            side_m = 1.0 if ot.get("side") == "buy" else -1.0
                            live_unrealized += (lp - entry) * qty * side_m
                    except Exception:
                        pass

            result[source] = {
                "total_trades":    len(v2_closed),
                "total_pnl":       round(total_pnl, 4),
                "win_rate":        round(len(wins) / len(pnls) * 100, 1) if pnls else 0,
                "avg_win":         round(sum(wins) / len(wins), 4) if wins else 0,
                "avg_loss":        round(sum(losses) / len(losses), 4) if losses else 0,
                "capital_start":   capital_start,
                "capital_now":     capital_now,
                "capital_return":  round(total_pnl / capital_start * 100, 2)
                                   if capital_start > 0 else 0,
                "open_trades":     len(open_trades),
                "live_unrealized": round(live_unrealized, 4),
            }
        return jsonify(result)
    except Exception as e:
        logger.error(f"api_experts_stats error: {e}")
        return jsonify({"error": str(e)})

def start_dashboard(memory, analyzer=None, scanner=None, regime=None, agent=None, port=8080):
    import subprocess, time as _time
    init_dashboard(memory, analyzer, scanner=scanner, regime=regime, agent=agent)
    # Release port from any lingering previous process (daemon thread didn't exit fast enough)
    try:
        subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True, timeout=3)
        _time.sleep(0.5)
    except Exception:
        pass
    def run():
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    logger.info(f"🌐 Dashboard running on port {port}")
    return thread

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TRADING AGENT</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@400;800&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#080c10;--surface:#0d1117;--border:#1a2030;--text:#c8d6e5;--muted:#4a5568;--green:#00ff88;--red:#ff3860;--blue:#4fc3f7;--mono:'Space Mono',monospace;--display:'Syne',sans-serif}
body{background:var(--bg);color:var(--text);font-family:var(--mono);font-size:12px;line-height:1.6}
body::before{content:'';position:fixed;inset:0;background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,255,136,0.015) 2px,rgba(0,255,136,0.015) 4px);pointer-events:none;z-index:9999}
header{display:flex;justify-content:space-between;align-items:center;padding:16px 24px;border-bottom:1px solid var(--border);background:var(--surface);position:sticky;top:0;z-index:100}
.logo{font-family:var(--display);font-size:18px;font-weight:800;letter-spacing:0.15em;color:var(--green);text-transform:uppercase}
.logo span{color:var(--muted);font-weight:400}
.status-dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.3}}
#clock{color:var(--muted);font-size:11px}
.stats-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:1px;background:var(--border)}
.stat-card{background:var(--surface);padding:20px 16px;text-align:center}
.stat-label{font-size:9px;letter-spacing:0.15em;text-transform:uppercase;color:var(--muted);margin-bottom:8px}
.stat-value{font-family:var(--display);font-size:28px;font-weight:800;line-height:1}
.pos{color:var(--green)} .neg{color:var(--red)} .neu{color:var(--blue)}
.main{display:grid;grid-template-columns:1fr 1fr 1fr;gap:1px;background:var(--border)}
.panel{background:var(--surface);padding:20px;overflow:hidden}
.panel-full{grid-column:1/-1} .panel-half{grid-column:span 2}
.panel-title{font-family:var(--display);font-size:10px;font-weight:700;letter-spacing:0.2em;text-transform:uppercase;color:var(--muted);margin-bottom:16px;display:flex;align-items:center;gap:8px}
.panel-title::after{content:'';flex:1;height:1px;background:var(--border)}
.data-table{width:100%;border-collapse:collapse}
.data-table th{font-size:9px;letter-spacing:0.12em;text-transform:uppercase;color:var(--muted);text-align:left;padding:6px 8px;border-bottom:1px solid var(--border)}
.data-table td{padding:8px;border-bottom:1px solid rgba(26,32,48,0.5)}
.tag{display:inline-block;padding:2px 7px;border-radius:3px;font-size:10px;font-weight:700}
.tag-buy{background:rgba(0,255,136,0.1);color:var(--green)}
.tag-sell{background:rgba(255,56,96,0.1);color:var(--red)}
.tag-open{background:rgba(79,195,247,0.1);color:var(--blue)}
.asset-bars{display:flex;flex-direction:column;gap:10px}
.asset-row{display:flex;align-items:center;gap:10px}
.asset-name{width:70px;font-size:11px;flex-shrink:0}
.bar-wrap{flex:1;height:14px;background:var(--bg);border-radius:2px;overflow:hidden}
.bar-fill{height:100%;border-radius:2px;transition:width 0.8s ease}
.bar-fill.pos{background:linear-gradient(90deg,#00cc6a,var(--green))}
.bar-fill.neg{background:linear-gradient(90deg,#cc2040,var(--red))}
.asset-pnl{width:60px;text-align:right;font-size:11px;flex-shrink:0}
.decision-item{padding:12px 0;border-bottom:1px solid var(--border)}
.decision-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.decision-time{font-size:10px;color:var(--muted)}
.decision-reasoning{font-size:11px;color:var(--muted);line-height:1.5;max-height:3.5em;overflow:hidden;cursor:pointer}
.decision-reasoning.expanded{max-height:200px}
.analysis-item{background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:14px;margin-bottom:10px}
.analysis-text{font-size:11px;color:var(--muted);line-height:1.6;margin-bottom:8px}
.lesson{font-size:10px;color:var(--blue);padding-left:12px;position:relative;margin-bottom:4px}
.lesson::before{content:'→';position:absolute;left:0;color:var(--muted)}
.empty{text-align:center;color:var(--muted);padding:30px;font-size:11px}
.alert-box{background:rgba(255,56,96,0.1);border:1px solid var(--red);border-radius:4px;padding:10px 16px;color:var(--red);font-size:11px;margin-bottom:12px}
.refresh-bar{height:2px;background:var(--border);width:100%;position:fixed;bottom:0;left:0}
.refresh-progress{height:100%;background:var(--green);width:0%}
@media(max-width:1024px){.main{grid-template-columns:1fr 1fr}.stats-grid{grid-template-columns:repeat(3,1fr)}.panel-half{grid-column:span 1}}
@media(max-width:640px){.main{grid-template-columns:1fr}.stats-grid{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<header>
  <div class="logo">AGENT<span>/</span>TERMINAL</div>
  <div style="display:flex;align-items:center;gap:20px">
    <div class="status-dot"></div>
    <div id="clock"></div>
  </div>
</header>
<div class="stats-grid">
  <div class="stat-card"><div class="stat-label">P&L Total</div><div class="stat-value" id="kpi-pnl">—</div></div>
  <div class="stat-card"><div class="stat-label">Win Rate</div><div class="stat-value neu" id="kpi-wr">—</div></div>
  <div class="stat-card"><div class="stat-label">Trades</div><div class="stat-value" id="kpi-trades">—</div></div>
  <div class="stat-card"><div class="stat-label">Profit Factor</div><div class="stat-value neu" id="kpi-pf">—</div></div>
  <div class="stat-card"><div class="stat-label">Max Drawdown</div><div class="stat-value neg" id="kpi-dd">—</div></div>
  <div class="stat-card"><div class="stat-label">Positions Open</div><div class="stat-value neu" id="kpi-open">—</div></div>
</div>
<div class="experts-grid" style="display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--border);margin-bottom:1px">
  <div style="background:var(--surface);padding:20px">
    <div class="panel-title">🚀 Gapper Expert</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
      <div><div class="stat-label">Capital</div><div class="stat-value pos" id="gap-capital">—</div></div>
      <div><div class="stat-label">Return</div><div class="stat-value pos" id="gap-return">—</div></div>
      <div><div class="stat-label">Trades</div><div class="stat-value neu" id="gap-trades">—</div></div>
      <div><div class="stat-label">Win Rate</div><div class="stat-value neu" id="gap-wr">—</div></div>
    </div>
  </div>
  <div style="background:var(--surface);padding:20px">
    <div class="panel-title">📐 Geometric Expert</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
      <div><div class="stat-label">Capital</div><div class="stat-value pos" id="geo-capital">—</div></div>
      <div><div class="stat-label">Return</div><div class="stat-value pos" id="geo-return">—</div></div>
      <div><div class="stat-label">Trades</div><div class="stat-value neu" id="geo-trades">—</div></div>
      <div><div class="stat-label">Win Rate</div><div class="stat-value neu" id="geo-wr">—</div></div>
    </div>
  </div>
</div>
<div class="main">
  <div class="panel panel-half">
    <div class="panel-title">Positions ouvertes</div>
    <div id="open-trades-container"><div class="empty">Aucune position ouverte</div></div>
  </div>
  <div class="panel">
    <div class="panel-title">P&L par asset</div>
    <div class="asset-bars" id="asset-bars"><div class="empty">Pas de données</div></div>
  </div>
  <div class="panel panel-full">
    <div class="panel-title">Historique des trades</div>
    <div style="overflow-x:auto">
      <table class="data-table">
        <thead><tr><th>Symbol</th><th>Dir.</th><th>Entrée</th><th>Sortie</th><th>Qté</th><th>P&L $</th><th>P&L %</th><th>Durée</th><th>Raison</th><th>Statut</th></tr></thead>
        <tbody id="trades-body"><tr><td colspan="10" class="empty">Chargement...</td></tr></tbody>
      </table>
    </div>
  </div>
  <div class="panel panel-half" style="max-height:500px;overflow-y:auto">
    <div class="panel-title">Décisions de l'agent</div>
    <div id="decisions-container"><div class="empty">Aucune décision</div></div>
  </div>
  <div class="panel" style="max-height:500px;overflow-y:auto">
    <div class="panel-title">Analyses post-trade</div>
    <div id="analyses-container"><div class="empty">Aucune analyse</div></div>
  </div>
</div>
<div class="refresh-bar"><div class="refresh-progress" id="refresh-progress"></div></div>
<script>
const REFRESH=15000;
function fmt(v,p='$',d=2){if(v===null||v===undefined||isNaN(v))return'—';return p+parseFloat(v).toFixed(d)}
function fmtPct(v){if(v===null||v===undefined||isNaN(v))return'—';const n=parseFloat(v);return(n>0?'+':'')+n.toFixed(2)+'%'}
function pnlCls(v){if(!v||isNaN(v))return'';return parseFloat(v)>=0?'pos':'neg'}
function fmtDur(m){if(!m)return'—';const n=Math.round(m);return n<60?n+'min':Math.floor(n/60)+'h'+(n%60>0?n%60+'min':'')}
async function fetchJSON(url){try{const r=await fetch(url);return await r.json()}catch(e){return null}}
function updateClock(){document.getElementById('clock').textContent=new Date().toUTCString().split(' ')[4]+' UTC'}
setInterval(updateClock,1000);updateClock();
async function updateExpertStats() {
  const d = await fetchJSON('/api/experts/stats');
  if (!d) return;
  const g = d.gapper || {};
  const geo = d.geometric || {};
  const fmt$ = v => '$' + Math.abs(v).toFixed(2);
  const fmtPct2 = v => (v >= 0 ? '+' : '') + v.toFixed(2) + '%';

  document.getElementById('gap-capital').textContent = fmt$(g.capital_now || 500);
  document.getElementById('gap-capital').className = 'stat-value ' + ((g.capital_now || 500) >= 500 ? 'pos' : 'neg');
  document.getElementById('gap-return').textContent = fmtPct2(g.capital_return || 0);
  document.getElementById('gap-return').className = 'stat-value ' + ((g.capital_return || 0) >= 0 ? 'pos' : 'neg');
  document.getElementById('gap-trades').textContent = g.total_trades || 0;
  document.getElementById('gap-wr').textContent = (g.win_rate || 0).toFixed(1) + '%';

  document.getElementById('geo-capital').textContent = fmt$(geo.capital_now || 500);
  document.getElementById('geo-capital').className = 'stat-value ' + ((geo.capital_now || 500) >= 500 ? 'pos' : 'neg');
  document.getElementById('geo-return').textContent = fmtPct2(geo.capital_return || 0);
  document.getElementById('geo-return').className = 'stat-value ' + ((geo.capital_return || 0) >= 0 ? 'pos' : 'neg');
  document.getElementById('geo-trades').textContent = geo.total_trades || 0;
  document.getElementById('geo-wr').textContent = (geo.win_rate || 0).toFixed(1) + '%';
}
async function updateStats(){
  const s=await fetchJSON('/api/stats');
  if(!s||s.total_trades===0)return;
  const pnl=s.total_pnl||0;
  const el=document.getElementById('kpi-pnl');
  el.textContent=(pnl>=0?'+':'')+' $'+Math.abs(pnl).toFixed(2);
  el.className='stat-value '+(pnl>=0?'pos':'neg');
  document.getElementById('kpi-wr').textContent=(s.win_rate||0).toFixed(1)+'%';
  document.getElementById('kpi-trades').textContent=s.total_trades;
  const pf=document.getElementById('kpi-pf');
  pf.textContent=s.profit_factor>=999?'∞':(s.profit_factor||0).toFixed(2);
  pf.className='stat-value '+((s.profit_factor||0)>=1?'pos':'neg');
  document.getElementById('kpi-dd').textContent='-$'+(s.max_drawdown||0).toFixed(2);
  renderAssetBars(s.asset_pnl||{});
}
async function updateOpenTrades(){
  const trades=await fetchJSON('/api/trades/open');
  const el=document.getElementById('open-trades-container');
  document.getElementById('kpi-open').textContent=trades?trades.length:0;
  if(!trades||trades.length===0){el.innerHTML='<div class="empty">Aucune position ouverte</div>';return}
  el.innerHTML='<table class="data-table"><thead><tr><th>Symbol</th><th>Dir.</th><th>Entrée</th><th>SL</th><th>TP</th></tr></thead><tbody>'+
    trades.map(t=>`<tr><td><strong>${t.symbol}</strong></td><td><span class="tag tag-${t.side}">${t.side.toUpperCase()}</span></td><td>${fmt(t.entry_price)}</td><td style="color:var(--red)">${fmt(t.stop_loss)}</td><td style="color:var(--green)">${fmt(t.take_profit)}</td></tr>`).join('')+
    '</tbody></table>';
}
function renderAssetBars(ap){
  const c=document.getElementById('asset-bars');
  const e=Object.entries(ap);
  if(!e.length){c.innerHTML='<div class="empty">Pas de données</div>';return}
  const max=Math.max(...e.map(([,v])=>Math.abs(v)),1);
  c.innerHTML=e.sort(([,a],[,b])=>b-a).map(([s,p])=>{
    const pct=Math.abs(p)/max*100;const cls=p>=0?'pos':'neg';
    return`<div class="asset-row"><div class="asset-name">${s}</div><div class="bar-wrap"><div class="bar-fill ${cls}" style="width:${pct}%"></div></div><div class="asset-pnl ${cls}">${p>=0?'+':''} $${Math.abs(p).toFixed(2)}</div></div>`;
  }).join('');
}
async function updateTradesHistory(){
  const trades=await fetchJSON('/api/trades/recent');
  const body=document.getElementById('trades-body');
  if(!trades||!trades.length){body.innerHTML='<tr><td colspan="10" class="empty">Aucun trade</td></tr>';return}
  body.innerHTML=trades.map(t=>{
    const pnl=t.pnl;const cls=pnl===null?'':(pnl>=0?'pos':'neg');const isOpen=t.status==='open';
    return`<tr><td><strong>${t.symbol}</strong></td><td><span class="tag tag-${t.side}">${t.side.toUpperCase()}</span></td><td>${fmt(t.entry_price)}</td><td>${isOpen?'<span class="tag tag-open">OUVERT</span>':fmt(t.exit_price)}</td><td>${t.qty}</td><td class="${cls}">${pnl!==null?(pnl>=0?'+':'')+'$'+Math.abs(pnl).toFixed(2):'—'}</td><td class="${cls}">${t.pnl_pct!==null?fmtPct(t.pnl_pct):'—'}</td><td>${fmtDur(t.hold_duration_min)}</td><td style="color:var(--muted)">${t.close_reason||'—'}</td><td><span class="tag ${isOpen?'tag-open':(pnl>=0?'tag-buy':'tag-sell')}">${t.status}</span></td></tr>`;
  }).join('');
}
async function updateDecisions(){
  const d=await fetchJSON('/api/decisions/recent');
  const c=document.getElementById('decisions-container');
  if(!d||!d.length){c.innerHTML='<div class="empty">Aucune décision</div>';return}
  c.innerHTML=d.map(x=>`<div class="decision-item"><div class="decision-header"><span class="tag tag-${x.decision==='buy'?'buy':x.decision==='sell'?'sell':'open'}">${x.decision.toUpperCase()}</span> <strong>${x.symbol||''}</strong><span class="decision-time">${new Date(x.decided_at).toLocaleTimeString('fr-FR')}</span></div><div class="decision-reasoning" onclick="this.classList.toggle('expanded')">${x.reasoning||'—'}</div></div>`).join('');
}
async function updateAnalyses(){
  const a=await fetchJSON('/api/analyses/recent');
  const c=document.getElementById('analyses-container');
  if(!a||!a.length){c.innerHTML='<div class="empty">Aucune analyse</div>';return}
  c.innerHTML=a.map(x=>{
    const lessons=Array.isArray(x.lessons)?x.lessons:[];
    const col=x.outcome==='win'?'var(--green)':x.outcome==='loss'?'var(--red)':'var(--blue)';
    return`<div class="analysis-item"><div style="display:flex;justify-content:space-between;margin-bottom:8px"><strong>${x.symbol}</strong><span style="color:${col};font-weight:700">${x.outcome.toUpperCase()} ${x.pnl?(x.pnl>=0?'+':'')+'$'+Math.abs(x.pnl).toFixed(2):''}</span></div><div class="analysis-text">${x.analysis||'—'}</div>${lessons.map(l=>`<div class="lesson">${l}</div>`).join('')}</div>`;
  }).join('');
}
async function refreshAll(){
  await Promise.all([updateStats(),updateExpertStats(),updateOpenTrades(),updateTradesHistory(),updateDecisions(),updateAnalyses()]);
  const bar=document.getElementById('refresh-progress');
  bar.style.transition='none';bar.style.width='0%';
  requestAnimationFrame(()=>{bar.style.transition=`width ${REFRESH}ms linear`;bar.style.width='100%'});
}
refreshAll();
setInterval(refreshAll,REFRESH);
</script>
</body>
</html>"""
