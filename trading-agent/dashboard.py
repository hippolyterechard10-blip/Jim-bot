from __future__ import annotations
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional
from flask import Flask, jsonify, make_response, redirect, render_template_string, request, url_for
from flask_cors import CORS
import config
from memory import TradingMemory

logger = logging.getLogger(__name__)
app = Flask(__name__)
CORS(app)
app.secret_key = os.getenv("SESSION_SECRET", secrets.token_hex(32))

_memory: Optional[TradingMemory] = None
_regime  = None


# ── Read-only SQLite helper (Phase E concurrency hardening) ───────────────────
def _ro_conn(db_path: str, timeout: float = 5.0) -> sqlite3.Connection:
    """Open a read-only SQLite connection with busy_timeout + query_only.
    Prevents accidental writes from dashboard endpoints and waits gracefully on locks."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=timeout)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA query_only = 1")
    return conn


# ── Auth helpers ───────────────────────────────────────────────────────────────
_AUTH_COOKIE   = "jb_session"
_SESSION_DAYS  = 30

def _dashboard_password() -> str:
    return os.getenv("DASHBOARD_PASSWORD", "")

def _sign_token(payload: str) -> str:
    key = app.secret_key.encode() if isinstance(app.secret_key, str) else app.secret_key
    sig = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"

def _verify_token(token: str) -> bool:
    if not token or "." not in token:
        return False
    payload, _, sig = token.rpartition(".")
    expected = hmac.new(
        app.secret_key.encode() if isinstance(app.secret_key, str) else app.secret_key,
        payload.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig)

def _is_authenticated() -> bool:
    # Requêtes internes depuis localhost (proxy api-server) → toujours autorisées
    if request.remote_addr in ("127.0.0.1", "::1"):
        return True
    token = request.cookies.get(_AUTH_COOKIE, "")
    return bool(token) and _verify_token(token)

_PUBLIC_PATHS = {"/login", "/favicon.ico"}

@app.before_request
def require_auth():
    if request.path in _PUBLIC_PATHS or request.path.startswith("/static"):
        return
    if not _is_authenticated():
        if request.path.startswith("/api/"):
            return jsonify({"error": "unauthorized"}), 401
        return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        pwd = request.form.get("password", "")
        expected = _dashboard_password()
        if expected and hmac.compare_digest(
            hashlib.sha256(pwd.encode()).hexdigest(),
            hashlib.sha256(expected.encode()).hexdigest(),
        ):
            token   = _sign_token(secrets.token_hex(24))
            resp    = make_response(redirect(url_for("dashboard")))
            expires = datetime.now(timezone.utc) + timedelta(days=_SESSION_DAYS)
            resp.set_cookie(
                _AUTH_COOKIE, token,
                httponly=True, samesite="Lax",
                expires=expires, max_age=_SESSION_DAYS * 86400,
            )
            return resp
        error = "Mot de passe incorrect."
    return render_template_string(_LOGIN_HTML, error=error)

@app.route("/logout")
def logout():
    resp = make_response(redirect(url_for("login")))
    resp.delete_cookie(_AUTH_COOKIE)
    return resp

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Jim Bot — Login</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#080c10;color:#c8d6e5;font-family:'Space Mono',monospace,sans-serif;
     min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#0d1117;border:1px solid #1a2030;border-radius:12px;padding:40px 36px;
      width:100%;max-width:360px;text-align:center}
.logo{font-size:22px;font-weight:800;letter-spacing:.15em;color:#00ff88;
      text-transform:uppercase;margin-bottom:8px}
.sub{font-size:11px;color:#4a5568;margin-bottom:28px;letter-spacing:.05em}
input[type=password]{width:100%;padding:12px 14px;background:#080c10;border:1px solid #1a2030;
  border-radius:8px;color:#c8d6e5;font-family:inherit;font-size:13px;margin-bottom:14px;
  outline:none;transition:border .2s}
input[type=password]:focus{border-color:#00ff88}
button{width:100%;padding:12px;background:#00ff88;color:#080c10;border:none;border-radius:8px;
  font-family:inherit;font-size:13px;font-weight:700;letter-spacing:.08em;cursor:pointer;
  text-transform:uppercase;transition:opacity .2s}
button:hover{opacity:.85}
.error{color:#ff3860;font-size:11px;margin-bottom:12px}
</style>
</head>
<body>
<div class="card">
  <div class="logo">📐 Jim Bot</div>
  <div class="sub">Geo V4 — ETH/USD · Accès restreint</div>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="POST">
    <input type="password" name="password" placeholder="Mot de passe" autofocus>
    <button type="submit">Connexion</button>
  </form>
</div>
</body>
</html>"""


def init_dashboard(memory, regime=None, **kwargs):
    global _memory, _regime
    _memory  = memory
    _regime  = regime

@app.route("/api/stats")
def api_stats():
    """Performance stats. Clean slate Phase 4 cutover by default."""
    if not _memory: return jsonify({})
    PHASE4_CUTOVER = "2026-05-28T21:37:00"
    include_legacy = request.args.get("include_legacy", "0") == "1"
    cutover_clause = "" if include_legacy else f"AND entry_at >= '{PHASE4_CUTOVER}'"
    try:
        conn = _ro_conn(_memory.db_path)
        rows = conn.execute(f"""
            SELECT pnl, symbol FROM trades
            WHERE status='closed' AND pnl IS NOT NULL
              {cutover_clause}
        """).fetchall()
        # Also pull real-time Kraken balance for transparency
        try:
            from broker_kraken_paper import KrakenPaperBroker
            kraken_real_equity = round(KrakenPaperBroker().get_equity(), 2)
        except Exception:
            kraken_real_equity = None
        conn.close()
        n = len(rows)
        if n == 0:
            return jsonify({
                "total_trades": 0, "winning_trades": 0, "losing_trades": 0,
                "win_rate": 0, "total_pnl": 0, "profit_factor": 0,
                "avg_win": 0, "avg_loss": 0, "max_drawdown": 0,
                "best_asset": None, "asset_pnl": {},
                "data_includes_legacy": include_legacy,
                "kraken_real_equity": kraken_real_equity,
                "synthetic_baseline": 10000.0 if not include_legacy else None,
            })
        wins = [r[0] for r in rows if r[0] > 0]
        losses = [r[0] for r in rows if r[0] <= 0]
        gw = sum(wins) if wins else 0
        gl = abs(sum(losses)) if losses else 0
        # Asset PnL
        ap = {}
        for pnl, sym in rows:
            ap[sym] = round(ap.get(sym, 0) + (pnl or 0), 2)
        best = max(ap, key=ap.get) if ap else None
        return jsonify({
            "total_trades": n,
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate": round(len(wins) / n * 100, 1),
            "total_pnl": round(sum(r[0] for r in rows), 2),
            "profit_factor": round(gw / gl, 2) if gl > 1e-9 else (999.0 if gw > 0 else 0),
            "avg_win": round(sum(wins) / len(wins), 2) if wins else 0,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0,
            "best_asset": best,
            "asset_pnl": ap,
            "data_includes_legacy": include_legacy,
            "kraken_real_equity": kraken_real_equity,
            "synthetic_baseline": 10000.0 if not include_legacy else None,
        })
    except Exception as e:
        logger.error(f"api_stats error: {e}")
        return jsonify({"error": str(e)})

@app.route("/api/llm/usage")
def api_llm_usage():
    """LLM usage stats (Audit #4 Phase A — observability).
    Query param ?hours=24 (default) | 168 | 720 ..."""
    try:
        from flask import request as fr
        hours = int(fr.args.get("hours", 24))
        import llm_usage as _lu
        # Use _memory.db_path so we read from the same DB as everything else
        db = _memory.db_path if _memory else None
        s = _lu.stats(window_hours=hours, db_path=db)
        s["recent"] = _lu.recent(limit=20, db_path=db)
        return jsonify(s)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/crypto-regime")
def api_crypto_regime():
    """Live crypto-native regime evaluation per symbol (read-only snapshot)."""
    try:
        from crypto_regime import get_last_eval
        snap = get_last_eval()
        return jsonify({
            "enabled":    bool(getattr(config, "USE_CRYPTO_REGIME_GATE", False)),
            "min_conf":   getattr(config, "CRYPTO_REGIME_MIN_CONFIDENCE", 50),
            "by_symbol":  snap,
            "symbols":    list(getattr(config, "GEO_SYMBOLS", [])),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/modes/stats")
def api_modes_stats():
    """Per-strategy-mode analytics (lowvol/normal/trend) avec coût model.
    Source de vérité : analytics.py — même calcul que la CLI."""
    try:
        from pathlib import Path
        import analytics as _analytics
        # Forcer le DB path local du dashboard
        db = Path(__file__).parent / "trading_memory.db"
        trades = _analytics.load_trades(db)
        overall      = _analytics.compute_metrics(trades)
        overall_live = _analytics.compute_metrics(trades, live_equivalent=True)
        groups       = _analytics.group_by(trades, "mode")
        by_mode      = {k: _analytics.compute_metrics(v) for k, v in groups.items()}
        by_mode_live = {k: _analytics.compute_metrics(v, live_equivalent=True)
                        for k, v in groups.items()}
        by_broker    = {k: _analytics.compute_metrics(v)
                        for k, v in _analytics.group_by(trades, "broker_mode").items()}

        # Current runtime state — drives the top status chips
        try:
            from experts.geometric_expert import GeometricExpert as _GE
            active_target_pct = float(getattr(config, "GEO_LOWVOL_TARGET_PCT",
                                              config.GEO_TARGET_PCT))
            active_mode = _GE._mode_from_target_pct(active_target_pct)
        except Exception:
            active_target_pct = None
            active_mode = "unknown"
        regime_raw = ""
        if _regime:
            try:
                regime_raw = (_regime.get_cache().get("regime") or "").lower()
            except Exception:
                regime_raw = ""
        if regime_raw in ("bear", "panic"):
            bias = "no_signals"
        elif regime_raw == "bull":
            bias = "longs_favored"
        elif regime_raw == "choppy":
            bias = "both"
        else:
            bias = "both"
        current_state = {
            "broker_mode":       "paper" if getattr(config, "KRAKEN_PAPER", True) else "live",
            "active_mode":       active_mode,
            "active_target_pct": active_target_pct,
            "regime":            regime_raw.upper() if regime_raw else "UNKNOWN",
            "bias":              bias,
            "symbols":           list(getattr(config, "GEO_SYMBOLS", [])),
        }

        return jsonify({
            "overall":       overall,
            "overall_live":  overall_live,
            "by_mode":       by_mode,
            "by_mode_live":  by_mode_live,
            "by_broker":     by_broker,
            "cost_model":    _analytics.COST_MODEL,
            "n_trades":      len(trades),
            "current_state": current_state,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/stats/periods")
def api_stats_periods():
    from flask import request as flask_req
    if not _memory:
        return jsonify({})
    expert = flask_req.args.get("expert", "all")
    try:
        conn = _ro_conn(_memory.db_path)

        src_filter = ""
        # Geo V4 is the only expert. Optional per-thesis filtering via thesis= param.
        if expert == "geo" or expert == "all":
            src_filter = "AND json_extract(market_context, '$.strategy_source') = 'geo_v4'"
        # Per-thesis filter (T1L = long, T1S = short zone, T2 = trend follow)
        thesis = flask_req.args.get("thesis", "").upper()
        if thesis in ("T1", "T2"):
            src_filter += f" AND json_extract(market_context, '$.thesis') = '{thesis}'"
        elif thesis == "T1L":
            src_filter += " AND json_extract(market_context, '$.side') = 'long'"
        elif thesis == "T1S":
            src_filter += " AND json_extract(market_context, '$.side') = 'short' AND COALESCE(json_extract(market_context, '$.thesis'), 'T1') = 'T1'"

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

# ── Phase 4 cost model (placeholder estimates, computed at dashboard layer) ───
# DB stores GROSS pnl from manage_open_positions. We compute estimated net here
# without modifying the live runtime. Reversible.
TAX_RATE_PLACEHOLDER = 0.30   # NOT real tax advice — placeholder
FEE_TAKER_PCT        = 0.0005 # Kraken Futures perp taker — most likely fill type
SLIPPAGE_BPS_LIVE    = 5      # 0.05% each leg, paper Kraken may differ

def _compute_estimated_costs(trade: dict) -> dict:
    """Compute fees, slippage, net, post-tax estimates for a single trade.
    Uses the trade's stored gross pnl (from DB) and entry/exit/qty fields.
    Returns dict with new fields to merge into the trade response."""
    out = {
        "fees_est": None,
        "slip_est": None,
        "net_pnl_est": None,
        "post_tax_pnl_est": None,
        "tax_rate_used": TAX_RATE_PLACEHOLDER,
        "estimates_note": "Estimates only. Real Kraken fees+slippage may differ. Tax 30% is placeholder, not advice.",
    }
    try:
        entry = float(trade.get("entry_price") or 0)
        exit_ = float(trade.get("exit_price") or 0)
        qty   = float(trade.get("qty") or 0)
        gross = trade.get("pnl")
        if gross is None or entry <= 0 or qty <= 0:
            return out
        fees = (entry * qty * FEE_TAKER_PCT) + ((exit_ if exit_ > 0 else entry) * qty * FEE_TAKER_PCT)
        slip = (entry + (exit_ if exit_ > 0 else entry)) * qty * (SLIPPAGE_BPS_LIVE / 10000.0)
        net  = float(gross) - fees - slip
        post_tax = net * (1 - TAX_RATE_PLACEHOLDER) if net > 0 else net
        out.update({
            "fees_est":         round(fees, 2),
            "slip_est":         round(slip, 2),
            "net_pnl_est":      round(net, 2),
            "post_tax_pnl_est": round(post_tax, 2),
        })
    except Exception:
        pass
    return out


@app.route("/api/trades/open")
def api_open_trades():
    if not _memory: return jsonify([])
    trades = _memory.get_open_trades()

    # ── Live enrichment via broker (1 HTTP call total for all symbols) ─────
    # Adds: live_price, pnl_live_gross/pct/net_est, dist_to_tp_abs/pct,
    # dist_to_sl_abs/pct, progress_pct (0=SL side, 100=TP side)
    live_prices = {}
    try:
        from broker_kraken_paper import KrakenPaperBroker
        _br = KrakenPaperBroker()
        for pos in (_br.get_positions() or []):
            live_prices[pos.db_symbol] = float(pos.current_price)
    except Exception as e:
        logger.warning(f"[api_open_trades] live price fetch failed: {e}")

    for t in trades:
        raw = t.get("market_context") or {}
        if isinstance(raw, str):
            try: raw = json.loads(raw)
            except: raw = {}
        t["strategy_source"] = raw.get("strategy_source")
        t["thesis"]          = raw.get("thesis", "T1")    # default T1 for legacy trades
        t["timeout_min"]     = raw.get("timeout_min")
        t["crypto_regime"]   = raw.get("crypto_regime")
        t["deployed"] = round(
            float(t.get("entry_price") or 0) * float(t.get("qty") or 0), 2
        )

        # ── Live-trade enrichment ──────────────────────────────────────────
        entry = float(t.get("entry_price") or 0)
        qty   = float(t.get("qty") or 0)
        sl    = float(t.get("stop_loss") or 0)
        tp    = float(t.get("take_profit") or 0)
        ctx_side = (raw.get("side") or "long").lower()
        is_long  = ctx_side == "long"
        live  = live_prices.get(t.get("symbol"))
        t["ctx_side"]   = ctx_side
        t["live_price"] = round(live, 4) if live else None

        if live and entry > 0 and qty > 0:
            mult = 1 if is_long else -1
            pnl_gross = mult * (live - entry) * qty
            pnl_pct   = mult * (live - entry) / entry * 100
            t["pnl_live_gross"] = round(pnl_gross, 2)
            t["pnl_live_pct"]   = round(pnl_pct, 3)
            # Estimated net at current live (round-trip fees + slippage estimate)
            est_fees = (entry * qty * FEE_TAKER_PCT) + (live * qty * FEE_TAKER_PCT)
            est_slip = (entry + live) * qty * (SLIPPAGE_BPS_LIVE / 10000.0)
            t["pnl_live_net_est"] = round(pnl_gross - est_fees - est_slip, 2)

            if tp > 0:
                dist_tp = (tp - live) if is_long else (live - tp)
                t["dist_to_tp_abs"] = round(dist_tp, 4)
                t["dist_to_tp_pct"] = round(dist_tp / live * 100, 3)
            if sl > 0:
                dist_sl = (live - sl) if is_long else (sl - live)
                t["dist_to_sl_abs"] = round(dist_sl, 4)
                t["dist_to_sl_pct"] = round(dist_sl / live * 100, 3)

            # Progress: 0% (live at SL) → 100% (live at TP). Entry typically ~33-50%.
            if sl > 0 and tp > 0:
                if is_long:
                    rng = tp - sl
                    prog  = (live  - sl) / rng if rng > 0 else 0.5
                    eprog = (entry - sl) / rng if rng > 0 else 0.5
                else:
                    rng = sl - tp
                    prog  = (sl - live)  / rng if rng > 0 else 0.5
                    eprog = (sl - entry) / rng if rng > 0 else 0.5
                t["progress_pct"]       = round(max(-20, min(120, prog  * 100)), 1)
                t["entry_progress_pct"] = round(max(-20, min(120, eprog * 100)), 1)

    return jsonify(trades)

@app.route("/api/trades/recent")
def api_recent_trades():
    """Clean slate: only Phase 4 trades by default. ?include_legacy=1 to see all."""
    if not _memory: return jsonify([])
    PHASE4_CUTOVER = "2026-05-28T21:37:00"
    include_legacy = request.args.get("include_legacy", "0") == "1"
    all_trades = _memory.get_recent_trades(limit=100)    # fetch more, filter, then trim
    filtered = []
    for t in all_trades:
        if not include_legacy:
            entry_at = str(t.get("entry_at", ""))
            if entry_at and entry_at < PHASE4_CUTOVER:
                continue
        raw = t.get("market_context") or {}
        if isinstance(raw, str):
            try: raw = json.loads(raw)
            except: raw = {}
        t["strategy_source"] = raw.get("strategy_source")
        t["thesis"]          = raw.get("thesis", "T1")
        t["crypto_regime"]   = raw.get("crypto_regime")
        # Estimated cost breakdown (gross → fees + slip → net → post-tax)
        t.update(_compute_estimated_costs(t))
        filtered.append(t)
        if len(filtered) >= 20: break
    return jsonify(filtered)

@app.route("/api/decisions/recent")
def api_recent_decisions():
    if not _memory: return jsonify([])
    decisions = _memory.get_recent_decisions(limit=15)
    for d in decisions:
        md = d.get("market_data")
        try:
            ctx = json.loads(md) if isinstance(md, str) else (md or {})
            d["strategy_source"] = ctx.get("strategy_source")
            d["thesis"]          = ctx.get("thesis", "T1")
        except Exception:
            d["strategy_source"] = None
            d["thesis"] = "T1"
    return jsonify(decisions)


@app.route("/api/thesis/breakdown")
def api_thesis_breakdown():
    """Per-thesis stats breakdown: T1L (long zone bounce), T1S (short zone), T2 (trend follow).
    Reflects the actual strategies active in Phase 4.

    CLEAN SLATE: only counts trades AFTER Phase 4 multi-thesis activation (2026-05-28 21:37 UTC).
    Pre-Phase 4 trades used the old strict router + adaptive low_vol mode — they're
    not representative of the current strategy. Set include_legacy=1 to see all.
    """
    if not _memory: return jsonify({})
    try:
        # Phase 4 cutover: 2026-05-28 21:37 UTC (multi-thesis activation)
        PHASE4_CUTOVER = "2026-05-28T21:37:00"
        include_legacy = request.args.get("include_legacy", "0") == "1"
        cutover_clause = "" if include_legacy else f"AND entry_at >= '{PHASE4_CUTOVER}'"
        conn = _ro_conn(_memory.db_path)
        # Per-thesis aggregation. Legacy trades have no thesis field — they're T1
        # by definition (only T1 long and T1 short existed before Phase 4).
        rows = conn.execute(f"""
            SELECT
                COALESCE(json_extract(market_context, '$.thesis'), 'T1') AS thesis,
                json_extract(market_context, '$.side') AS side,
                COUNT(*) AS n,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                COALESCE(SUM(pnl), 0) AS total_pnl,
                COALESCE(AVG(pnl), 0) AS avg_pnl,
                COALESCE(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0) AS gross_win,
                COALESCE(SUM(CASE WHEN pnl < 0 THEN -pnl ELSE 0 END), 0) AS gross_loss
            FROM trades
            WHERE status = 'closed' AND pnl IS NOT NULL
              AND json_extract(market_context, '$.strategy_source') = 'geo_v4'
              {cutover_clause}
            GROUP BY thesis, side
            ORDER BY thesis, side
        """).fetchall()
        conn.close()
        out = []
        for r in rows:
            thesis = r[0] or "T1"
            side = r[1] or "?"
            n = int(r[2]) if r[2] else 0
            wins = int(r[3]) if r[3] else 0
            wr = (wins / n) if n else 0
            gw = float(r[6]) if r[6] else 0
            gl = float(r[7]) if r[7] else 0
            pf = (gw / gl) if gl > 1e-9 else (999.0 if gw > 0 else 0.0)
            # Display name combining thesis + side
            label = thesis
            if thesis == "T1":
                label = "T1L (long zone bounce)" if side == "long" else "T1S (short zone bounce)"
            elif thesis == "T2":
                label = "T2 (short trend follow)"
            out.append({
                "thesis": thesis,
                "side": side,
                "label": label,
                "n_trades": n,
                "wins": wins,
                "win_rate": round(wr, 3),
                "total_pnl": round(float(r[4]) if r[4] else 0, 2),
                "avg_pnl": round(float(r[5]) if r[5] else 0, 3),
                "pf": round(pf, 2) if pf < 999 else 999.0,
            })
        return jsonify({
            "phase": "4 — multi-thesis paper",
            "cutover_date": PHASE4_CUTOVER + "Z",
            "data_includes_legacy": include_legacy,
            "available_strategies": [
                {"id": "T1L", "label": "T1 long zone bounce (Geo V4 long)"},
                {"id": "T1S", "label": "T1 short zone bounce (Geo V4 short)"},
                {"id": "T2",  "label": "T2 short trend follow (EMA20 1h pullback)"},
            ],
            "removed_strategies": [
                {"id": "T3", "reason": "DROPPED — breakdown thesis structurally negative in crypto"},
                {"id": "T4", "reason": "DROPPED — rally fail thesis structurally negative"},
                {"id": "low_vol_mode", "reason": "KILLED on 2026-05-29 — forced off for backtest/live parity"},
                {"id": "gapper", "reason": "REMOVED — old equity gapper expert archived"},
                {"id": "alpaca", "reason": "REMOVED — Alpaca broker archived (Kraken Futures only)"},
            ],
            "active_router": getattr(config, "ROUTER_VARIANT", "strict"),
            "by_thesis": out,
        })
    except Exception as e:
        logger.error(f"api_thesis_breakdown error: {e}")
        return jsonify({"error": str(e)})


@app.route("/api/t1l/distance-gate-stats")
def api_t1l_distance_gate_stats():
    """T1L distance gate parity tracking.
    Splits T1L closed trades by distance_gate_trigger_permissive flag to measure
    impact of the live's wider distance gate vs the backtest's narrower range.

    distance_gate_trigger_permissive = True : dist_pct > -0.001 from zone.high
                                              → would NOT have fired in backtest
    distance_gate_trigger_permissive = False : dist_pct <= -0.001
                                              → would have fired in backtest too (parity)
    """
    if not _memory: return jsonify({})
    try:
        conn = _ro_conn(_memory.db_path)
        rows = conn.execute("""
            SELECT
                COALESCE(json_extract(market_context, '$.distance_gate_trigger_permissive'), 0) AS gate_permissive,
                COUNT(*) AS n,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                COALESCE(SUM(pnl), 0) AS total_pnl,
                COALESCE(AVG(pnl), 0) AS avg_pnl,
                COALESCE(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0) AS gross_win,
                COALESCE(SUM(CASE WHEN pnl < 0 THEN -pnl ELSE 0 END), 0) AS gross_loss
            FROM trades
            WHERE status='closed' AND pnl IS NOT NULL
              AND json_extract(market_context, '$.strategy_source') = 'geo_v4'
              AND json_extract(market_context, '$.side') = 'long'
              AND COALESCE(json_extract(market_context, '$.thesis'), 'T1') = 'T1'
            GROUP BY gate_permissive
            ORDER BY gate_permissive
        """).fetchall()

        # Also get raw dist_pct distribution (last 50 T1L trades)
        dist_rows = conn.execute("""
            SELECT
                id, symbol, entry_at, pnl,
                json_extract(market_context, '$.dist_pct_at_entry') AS dist_pct,
                json_extract(market_context, '$.distance_gate_trigger_permissive') AS gate_permissive
            FROM trades
            WHERE status='closed'
              AND json_extract(market_context, '$.strategy_source') = 'geo_v4'
              AND json_extract(market_context, '$.side') = 'long'
              AND json_extract(market_context, '$.dist_pct_at_entry') IS NOT NULL
            ORDER BY id DESC
            LIMIT 50
        """).fetchall()
        conn.close()

        breakdown = []
        for r in rows:
            n = int(r[1]) if r[1] else 0
            wins = int(r[2]) if r[2] else 0
            wr = (wins / n) if n else 0
            gw = float(r[5]) if r[5] else 0
            gl = float(r[6]) if r[6] else 0
            pf = (gw / gl) if gl > 1e-9 else (999.0 if gw > 0 else 0.0)
            permissive = bool(r[0]) if r[0] is not None else False
            label = (
                "PERMISSIVE (dist > -0.1% — extra trades vs backtest)"
                if permissive else
                "PARITY (dist <= -0.1% — would also fire in backtest)"
            )
            breakdown.append({
                "permissive_trigger": permissive,
                "label": label,
                "n_trades": n,
                "wins": wins,
                "win_rate": round(wr, 3),
                "total_pnl": round(float(r[3]) if r[3] else 0, 2),
                "expectancy": round(float(r[4]) if r[4] else 0, 3),
                "pf": round(pf, 2) if pf < 999 else 999.0,
            })

        recent_dists = []
        for r in dist_rows:
            recent_dists.append({
                "id": r[0], "symbol": r[1], "entry_at": r[2],
                "pnl": round(float(r[3]) if r[3] else 0, 2),
                "dist_pct_at_entry": round(float(r[4]), 5) if r[4] else None,
                "gate_permissive": bool(r[5]) if r[5] is not None else None,
            })

        return jsonify({
            "instrumented_since": "2026-05-29 — Phase 4 parity tracking",
            "rule": "permissive trigger = dist_pct > -0.001 (live's wider gate vs backtest narrower)",
            "by_gate_trigger": breakdown,
            "recent_t1l_trades_with_dist": recent_dists,
        })
    except Exception as e:
        logger.error(f"api_t1l_distance_gate_stats error: {e}")
        return jsonify({"error": str(e)})


@app.route("/api/phase4-status")
def api_phase4_status():
    """Phase 4 multi-thesis short expert config — for dashboard status banner."""
    return jsonify({
        "phase": "4 — paper multi-thesis",
        "router_variant": getattr(config, "ROUTER_VARIANT", "strict"),
        "t1s_include_hard_block": getattr(config, "T1S_INCLUDE_HARD_BLOCK", False),
        "enable_t2_short": getattr(config, "ENABLE_T2_SHORT", False),
        "validated_params": {
            "T2_PULLBACK_BUFFER_PCT": getattr(config, "T2_PULLBACK_BUFFER_PCT", None),
            "T2_STOP_BUFFER_PCT":      getattr(config, "T2_STOP_BUFFER_PCT", None),
            "T2_SWING_LOOKBACK_1H":    getattr(config, "T2_SWING_LOOKBACK_1H", None),
            "T2_RSI_5M_MAX":           getattr(config, "T2_RSI_5M_MAX", None),
            "T2_TIMEOUT_MIN":          getattr(config, "T2_TIMEOUT_MIN", None),
            "T2_TARGET_PCT_DEFAULT":   getattr(config, "T2_TARGET_PCT_DEFAULT", None),
            "T2_INITIAL_CAP":          getattr(config, "T2_INITIAL_CAP", None),
            "T2_COOLDOWN_MIN":         getattr(config, "T2_COOLDOWN_MIN", None),
        },
        "validation_source": "shared/trading/short-expert-validation-2026-05-28.md",
        "backtest_sweet_spot": {
            "sharpe": 9.71,
            "max_dd_pct": -2.56,
            "trades_3y": 1566,
            "wr": 0.693,
        },
        "live_expected_after_discount": {
            "sharpe": "~5",
            "max_dd_pct": "-5 to -7%",
            "cagr_net_pfu": "~16%/yr",
        },
    })

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
    return jsonify([])

@app.route("/api/movers")
def api_movers():
    return jsonify({"movers": [], "note": "geo-only mode"})

@app.route("/api/sentiment")
def api_sentiment():
    return jsonify({"sentiment": "neutral", "score": 0, "headlines": [], "alerts": []})

@app.route("/api/calendar")
def api_calendar():
    return jsonify({"event": None, "note": ""})

@app.route("/api/regime")
def api_regime():
    if not _regime:
        return jsonify({"regime": "UNKNOWN", "vix": None})
    try:
        cache = _regime.get_cache()
        return jsonify({
            "regime": cache.get("regime", "UNKNOWN").upper(),
            "vix":    cache.get("vix"),
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
        conn      = _ro_conn(_memory.db_path, timeout=10)
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
        conn   = _ro_conn(_memory.db_path, timeout=10)
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
                "thesis":          mc.get("thesis", "T1"),
                "geo_context":     geo_ctx,
            })
        # Add estimated cost breakdown (fees, slip, net, post-tax) to each trade
        for t in trades:
            t.update(_compute_estimated_costs(t))
        return jsonify({"trades": trades, "period": period})
    except Exception as e:
        logger.error(f"api_trades_individual error: {e}")
        return jsonify({"trades": [], "error": str(e)})

@app.route("/api/trades/<trade_id>")
def api_trade_detail(trade_id):
    if not _memory:
        return jsonify({"error": "not ready"}), 503
    try:
        conn = _ro_conn(_memory.db_path)
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
        conn = _ro_conn(_memory.db_path, timeout=10)
        c    = conn.cursor()

        # Geo V4 is the only expert. Optional per-thesis filtering.
        expert = request.args.get("expert", "all").lower()
        if expert in ("geo", "all"):
            ef = "AND json_extract(market_context, '$.strategy_source') = 'geo_v4'"
        else:
            ef = ""
        thesis = request.args.get("thesis", "").upper()
        if thesis in ("T1", "T2"):
            ef += f" AND json_extract(market_context, '$.thesis') = '{thesis}'"
        elif thesis == "T1L":
            ef += " AND json_extract(market_context, '$.side') = 'long'"
        elif thesis == "T1S":
            ef += " AND json_extract(market_context, '$.side') = 'short' AND COALESCE(json_extract(market_context, '$.thesis'), 'T1') = 'T1'"

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


# ── Helpers communs pour les 4 endpoints Analysis Geo V4 ──────────────────────
_GEO_V4_SRC = "json_extract(market_context, '$.strategy_source') = 'geo_v4'"
_GEO_EXCLUDE = "close_reason NOT IN ('synced_close', 'orphan_close', 'position_reconciled')"

def _geo_filter(extra: str = "") -> str:
    """Clause WHERE complète pour trades geo_v4 depuis GEO_RESET_DATE."""
    return (
        f"status = 'closed' "
        f"AND exit_at >= '{config.GEO_RESET_DATE}' "
        f"AND {_GEO_V4_SRC} "
        f"AND {_GEO_EXCLUDE} "
        f"{extra}"
    )

def _categorise_reason(r: str) -> str:
    r = (r or "").lower()
    if any(k in r for k in ("stop", "sl_", "stop_loss")):
        return "stop"
    if any(k in r for k in ("target", "tp", "profit", "take_profit")):
        return "target"
    if any(k in r for k in ("timeout", "time", "expir", "max_hold")):
        return "timeout"
    return "other"


@app.route("/api/analysis/rolling")
def api_analysis_rolling():
    """Section 1 — Rolling stats des 30 derniers trades geo_v4."""
    if not _memory:
        return jsonify({"n_trades": 0})
    try:
        conn = _ro_conn(_memory.db_path)
        rows = conn.execute(f"""
            SELECT pnl, hold_duration_min FROM trades
            WHERE {_geo_filter()}
            ORDER BY exit_at DESC LIMIT 30
        """).fetchall()
        conn.close()
        pnls  = [r[0] or 0 for r in rows]
        holds = [r[1] for r in rows if r[1] is not None]
        wins  = [p for p in pnls if p > 0]
        losses= [p for p in pnls if p < 0]
        total = len(pnls)
        pf    = round(sum(wins) / abs(sum(losses)), 2) if losses else 999
        return jsonify({
            "n_trades":      total,
            "win_rate":      round(len(wins) / total * 100, 1) if total else 0,
            "profit_factor": pf,
            "avg_hold_min":  round(sum(holds) / len(holds), 1) if holds else 0,
        })
    except Exception as e:
        logger.error(f"api_analysis_rolling error: {e}")
        return jsonify({"error": str(e), "n_trades": 0})


@app.route("/api/analysis/exits")
def api_analysis_exits():
    """Section 2 — Breakdown par type de sortie (stop/target/timeout)."""
    if not _memory:
        return jsonify({"total": 0})
    try:
        conn = _ro_conn(_memory.db_path)
        rows = conn.execute(f"""
            SELECT close_reason, COUNT(*) as n FROM trades
            WHERE {_geo_filter()}
            GROUP BY close_reason
        """).fetchall()
        conn.close()
        cats = {"stop": 0, "target": 0, "timeout": 0, "other": 0}
        for reason, cnt in rows:
            cats[_categorise_reason(reason)] += cnt
        total = sum(cats.values())
        def pct(n): return round(n / total * 100, 1) if total else 0
        return jsonify({
            "total":   total,
            "stop":    {"n": cats["stop"],    "pct": pct(cats["stop"])},
            "target":  {"n": cats["target"],  "pct": pct(cats["target"])},
            "timeout": {"n": cats["timeout"], "pct": pct(cats["timeout"])},
            "other":   {"n": cats["other"],   "pct": pct(cats["other"])},
        })
    except Exception as e:
        logger.error(f"api_analysis_exits error: {e}")
        return jsonify({"error": str(e), "total": 0})


@app.route("/api/analysis/period")
def api_analysis_period():
    """Section 3 — Stats par période : 7d / mtd / all."""
    if not _memory:
        return jsonify({"n_trades": 0})
    period = request.args.get("period", "all")
    try:
        from datetime import timedelta
        now   = datetime.now(timezone.utc).date()
        since = None
        if period == "7d":
            since = (now - timedelta(days=7)).isoformat()
        elif period == "mtd":
            since = now.replace(day=1).isoformat()
        # "all" → already covered by _geo_filter (>= GEO_RESET_DATE)
        extra = f"AND exit_at >= '{since}'" if since else ""
        conn  = _ro_conn(_memory.db_path)
        rows  = conn.execute(f"""
            SELECT pnl FROM trades WHERE {_geo_filter(extra)}
        """).fetchall()
        conn.close()
        pnls  = [r[0] or 0 for r in rows]
        wins  = [p for p in pnls if p > 0]
        losses= [p for p in pnls if p < 0]
        total = len(pnls)
        pf    = round(sum(wins) / abs(sum(losses)), 2) if losses else 999
        return jsonify({
            "period":        period,
            "n_trades":      total,
            "total_pnl":     round(sum(pnls), 2),
            "win_rate":      round(len(wins) / total * 100, 1) if total else 0,
            "profit_factor": pf,
        })
    except Exception as e:
        logger.error(f"api_analysis_period error: {e}")
        return jsonify({"error": str(e), "n_trades": 0})


@app.route("/api/analysis/equity-curve")
def api_analysis_equity_curve():
    """Section 4 — Courbe d'équité cumulée. Clean slate Phase 4 cutover by default.
    Returns BOTH synthetic baseline (calculated from cutover) AND real Kraken equity."""
    if not _memory:
        return jsonify({"capital_start": 0, "points": []})
    try:
        PHASE4_CUTOVER = "2026-05-28T21:37:00"
        include_legacy = request.args.get("include_legacy", "0") == "1"
        cutover_clause = "" if include_legacy else f"AND exit_at >= '{PHASE4_CUTOVER}'"
        # Synthetic baseline (display-only): start from $10k at Phase 4 cutover
        capital_start = config.GEO_CAPITAL if not include_legacy else _get_alpaca_equity()
        # Real Kraken paper equity for honest reporting
        try:
            from broker_kraken_paper import KrakenPaperBroker
            kraken_real_equity = round(KrakenPaperBroker().get_equity(), 2)
        except Exception:
            kraken_real_equity = None
        conn  = _ro_conn(_memory.db_path)
        rows  = conn.execute(f"""
            SELECT exit_at, pnl FROM trades
            WHERE {_geo_filter()}
              {cutover_clause}
            ORDER BY exit_at
        """).fetchall()
        conn.close()
        points     = []
        cumulative = capital_start
        for exit_at, pnl in rows:
            cumulative += (pnl or 0)
            points.append({
                "date":    (exit_at or "")[:10],
                "capital": round(cumulative, 2),
            })
        return jsonify({
            "capital_start": round(capital_start, 2),
            "capital_now":   round(cumulative, 2),
            "kraken_real_equity": kraken_real_equity,
            "synthetic_note": "capital_start/now = synthetic Phase 4 baseline; kraken_real_equity = actual paper account balance",
            "data_includes_legacy": include_legacy,
            "points":        points,
        })
    except Exception as e:
        logger.error(f"api_analysis_equity_curve error: {e}")
        return jsonify({"capital_start": 0, "points": [], "error": str(e)})


@app.route("/api/account")
def api_account():
    try:
        import alpaca_trade_api as tradeapi
        api = tradeapi.REST(
            os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"),
            "https://paper-api.alpaca.markets"
        )
        account = api.get_account()
        return jsonify({
            "equity":          float(account.equity),
            "cash":            float(account.cash),
            "buying_power":    float(account.buying_power),
            "portfolio_value": float(account.portfolio_value),
            "last_equity":     float(account.last_equity),
        })
    except Exception as e:
        logger.error(f"api_account error: {e}")
        return jsonify({"equity": 0, "cash": 0, "buying_power": 0, "portfolio_value": 0, "error": str(e)})

@app.route("/api/orders/pending")
def api_orders_pending():
    try:
        import alpaca_trade_api as tradeapi
        api = tradeapi.REST(
            os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"),
            "https://paper-api.alpaca.markets"
        )
        orders = api.list_orders(status="open")
        result = []
        for o in orders:
            qty        = float(o.qty or 0)
            lim        = float(o.limit_price) if o.limit_price else None
            est_value  = round(qty * lim, 2) if lim else None
            result.append({
                "id":              o.id,
                "symbol":          o.symbol,
                "side":            o.side,
                "qty":             qty,
                "limit_price":     lim,
                "estimated_value": est_value,
                "status":          o.status,
                "created_at":      str(o.created_at),
                "time_in_force":   o.time_in_force,
            })
        return jsonify(result)
    except Exception as e:
        logger.error(f"api_orders_pending error: {e}")
        return jsonify([])

@app.route("/api/stops")
def api_stops():
    try:
        import sqlite3 as _sq
        conn = _sq.connect(_memory.db_path, timeout=3)
        rows = conn.execute(
            "SELECT symbol, stop_loss FROM trades WHERE status='open' AND stop_loss IS NOT NULL"
        ).fetchall()
        conn.close()
        stops = {r[0]: float(r[1]) for r in rows if r[1]}
        return jsonify({"stops": stops})
    except Exception as e:
        logger.error(f"api_stops error: {e}")
        return jsonify({"stops": {}})

@app.route("/api/health")
def api_health():
    return jsonify({"status":"ok","timestamp":datetime.now(timezone.utc).isoformat()})

@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


# ── Cache equity Alpaca (évite d'appeler l'API à chaque request) ──────────────
_equity_cache: dict = {"value": None, "ts": 0.0}
_EQUITY_CACHE_TTL = 60  # secondes

def _get_alpaca_equity() -> float:
    """Equity courante en mode paper = capital de référence + PnL réalisé.
    Nom conservé pour ne pas casser les callers. Cache 60s."""
    import time as _time
    now = _time.time()
    if _equity_cache["value"] is not None and now - _equity_cache["ts"] < _EQUITY_CACHE_TTL:
        return _equity_cache["value"]
    try:
        # Phase 4 clean slate: equity = capital + Phase 4 trades PnL only.
        PHASE4_CUTOVER = "2026-05-28T21:37:00"
        conn = _ro_conn(_memory.db_path)
        row  = conn.execute(
            f"SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE status='closed' AND entry_at >= '{PHASE4_CUTOVER}'"
        ).fetchone()
        conn.close()
        equity = config.GEO_CAPITAL + float((row[0] if row else 0) or 0)
        _equity_cache["value"] = equity
        _equity_cache["ts"]    = now
        return equity
    except Exception as e:
        logger.warning(f"_get_equity error: {e}")
        return _equity_cache["value"] if _equity_cache["value"] is not None else config.GEO_CAPITAL


@app.route("/api/experts/stats")
def api_experts_stats():
    """Geo-Only ETH — capital et stats de la stratégie geo_v4.
    capital_start = equity réelle Alpaca (nouveau départ GEO_RESET_DATE).
    Seuls les trades APRÈS GEO_RESET_DATE entrent dans le calcul P&L.
    """
    if not _memory:
        return jsonify({})
    try:
        import json as _json
        from datetime import datetime as _dt
        EXCLUDE = ("synced_close", "orphan_close", "position_reconciled")

        # ── Capital de référence (fixe au reset) et equity live Alpaca ─────────
        capital_start = config.GEO_CAPITAL          # valeur fixe au moment du reset
        capital_now   = _get_alpaca_equity()         # equity Alpaca live — source de vérité

        # ── Trades geo_v4 — uniquement APRÈS GEO_RESET_DATE ─────────────────
        all_trades = _memory.get_recent_trades(limit=500)
        geo_trades = []
        for t in all_trades:
            ctx = t.get("market_context") or {}
            if isinstance(ctx, str):
                try: ctx = _json.loads(ctx)
                except: ctx = {}
            if ctx.get("strategy_source") != "geo_v4":
                continue
            # Filtre date de reset
            created = t.get("created_at") or t.get("entry_time") or ""
            if created and str(created)[:10] < config.GEO_RESET_DATE:
                continue
            geo_trades.append(t)

        closed = [t for t in geo_trades if t.get("status") == "closed"
                  and t.get("close_reason") not in EXCLUDE]
        pnls   = [t.get("pnl") or 0 for t in closed]
        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        pf     = round(sum(wins) / abs(sum(losses)), 2) if losses else 999

        deployed = sum(
            float(t.get("entry_price", 0)) * float(t.get("qty", 0))
            for t in geo_trades if t.get("status") == "open"
        )
        total_pnl     = round(sum(pnls), 4)
        capital_return = round((capital_now - capital_start) / capital_start * 100, 2) if capital_start else 0

        return jsonify({
            "geo_v4": {
                "total_trades":    len(closed),
                "total_pnl":       total_pnl,
                "win_rate":        round(len(wins) / len(pnls) * 100, 1) if pnls else 0,
                "profit_factor":   pf,
                "avg_win":         round(sum(wins) / len(wins), 4) if wins else 0,
                "avg_loss":        round(sum(losses) / len(losses), 4) if losses else 0,
                "capital_start":   round(capital_start, 2),
                "capital_now":     round(capital_now, 2),
                "capital_return":  capital_return,
                "open_trades":     len([t for t in geo_trades if t.get("status") == "open"]),
                "live_unrealized": 0.0,
                "deployed":        round(deployed, 2),
            }
        })
    except Exception as e:
        logger.error(f"api_experts_stats error: {e}")
        return jsonify({"error": str(e)})

def start_dashboard(memory, regime=None, port=8080, **kwargs):
    import subprocess, time as _time
    init_dashboard(memory, regime=regime)
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
<title>Jim — AI Trading OS</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:ital,wght@0,300;0,400;0,500;0,600;0,700;0,800;1,400&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#07080d;
  --bg2:#0c0e18;
  --surface:rgba(255,255,255,0.038);
  --surface-hi:rgba(255,255,255,0.062);
  --border:rgba(255,255,255,0.07);
  --border-hi:rgba(255,255,255,0.14);
  --green:#10b981;
  --green-soft:rgba(16,185,129,0.12);
  --green-glow:rgba(16,185,129,0.22);
  --red:#f43f5e;
  --red-soft:rgba(244,63,94,0.12);
  --blue:#818cf8;
  --blue-soft:rgba(129,140,248,0.12);
  --gold:#f59e0b;
  --gold-soft:rgba(245,158,11,0.12);
  --text:#f1f5f9;
  --text2:#8b95a9;
  --text3:#424d5e;
  --r:16px;
  --r-sm:10px;
  --font:'Inter',-apple-system,system-ui,sans-serif;
}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--text);font-family:var(--font);font-size:14px;line-height:1.5;min-height:100vh;overflow-x:hidden}
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.1);border-radius:99px}

/* ─── Layout ─────────────────────────────────── */
.wrap{max-width:1480px;margin:0 auto;padding:0 28px 56px;position:relative;z-index:1}

/* ─── Header ─────────────────────────────────── */
header{
  display:flex;align-items:center;justify-content:space-between;
  padding:18px 0;border-bottom:1px solid var(--border);margin-bottom:32px;
  position:sticky;top:0;background:rgba(7,8,13,0.88);
  backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);z-index:100
}
.logo{display:flex;align-items:center;gap:11px}
.logo-mark{
  width:33px;height:33px;background:linear-gradient(135deg,var(--green) 0%,#059669 100%);
  border-radius:9px;display:flex;align-items:center;justify-content:center;
  font-weight:800;font-size:15px;color:#fff;letter-spacing:-1px;
  box-shadow:0 0 20px var(--green-glow)
}
.logo-name{font-size:15px;font-weight:600;letter-spacing:-.3px}
.logo-name em{color:var(--text3);font-style:normal;font-weight:400}
.hdr-right{display:flex;align-items:center;gap:12px}
.regime-pill{
  display:flex;align-items:center;gap:7px;padding:6px 14px;
  border-radius:99px;font-size:11px;font-weight:600;letter-spacing:.04em;
  text-transform:uppercase;transition:all .3s
}
.rp-bull{background:var(--green-soft);color:var(--green);border:1px solid rgba(16,185,129,0.2)}
.rp-bear{background:var(--red-soft);color:var(--red);border:1px solid rgba(244,63,94,0.2)}
.rp-neutral{background:var(--surface);color:var(--text3);border:1px solid var(--border)}
.regime-pip{width:6px;height:6px;border-radius:50%;background:currentColor}
.rp-bull .regime-pip{animation:blink 2.4s ease-in-out infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.status-chip{
  display:flex;align-items:center;gap:8px;padding:6px 14px;
  background:var(--surface);border:1px solid var(--border);border-radius:99px
}
.live-dot{
  width:7px;height:7px;border-radius:50%;background:var(--green);
  box-shadow:0 0 8px var(--green);animation:livepulse 2.6s ease-in-out infinite
}
@keyframes livepulse{0%,100%{box-shadow:0 0 8px var(--green)}50%{box-shadow:0 0 3px var(--green)}}
#clock{font-size:12px;font-weight:500;color:var(--text2);font-variant-numeric:tabular-nums}

/* ─── Cards ──────────────────────────────────── */
.card{
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--r);padding:24px;
  transition:border-color .2s;
  animation:fadeUp .4s ease both
}
.card:hover{border-color:var(--border-hi)}
@keyframes fadeUp{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}

.sec-label{
  font-size:11px;font-weight:600;text-transform:uppercase;
  letter-spacing:.08em;color:var(--text3);margin-bottom:20px;
  display:flex;align-items:center;gap:8px
}
.sec-label::after{content:'';flex:1;height:1px;background:var(--border)}

/* ─── Hero ───────────────────────────────────── */
.hero-card{padding:32px 36px;margin-bottom:16px}
.hero-grid{display:grid;grid-template-columns:1fr auto;gap:32px;align-items:end}
.hero-label{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.07em;color:var(--text3);margin-bottom:12px}
.hero-value{
  font-size:54px;font-weight:700;letter-spacing:-2.5px;
  line-height:1;font-variant-numeric:tabular-nums;color:var(--text)
}
.hero-change{display:flex;align-items:center;gap:10px;margin-top:10px}
.pill{
  display:inline-flex;align-items:center;gap:4px;
  padding:4px 11px;border-radius:99px;font-size:13px;font-weight:600
}
.pill-pos{background:var(--green-soft);color:var(--green)}
.pill-neg{background:var(--red-soft);color:var(--red)}
.pill-neu{background:var(--blue-soft);color:var(--blue)}
.hero-meta{font-size:13px;color:var(--text2)}
.hero-aside{text-align:right}
.hero-aside-label{font-size:11px;color:var(--text3);margin-bottom:3px}
.hero-aside-val{font-size:17px;font-weight:600}
.hero-aside-badge{display:flex;align-items:center;gap:6px;justify-content:flex-end;margin-top:10px}
.broker-dot{width:6px;height:6px;border-radius:50%;background:var(--green)}
.broker-name{font-size:12px;font-weight:500;color:var(--green)}

/* ─── KPI strip ──────────────────────────────── */
.kpi-row{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:24px}
.kpi{
  background:var(--surface);border:1px solid var(--border);border-radius:var(--r-sm);
  padding:18px 20px;transition:all .2s;
  animation:fadeUp .4s ease both
}
.kpi:hover{background:var(--surface-hi);border-color:var(--border-hi)}
.kpi:nth-child(1){animation-delay:.06s}
.kpi:nth-child(2){animation-delay:.10s}
.kpi:nth-child(3){animation-delay:.14s}
.kpi:nth-child(4){animation-delay:.18s}
.kpi:nth-child(5){animation-delay:.22s}
.kpi-lbl{font-size:11px;font-weight:500;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px}
.kpi-val{font-size:24px;font-weight:700;letter-spacing:-.5px;font-variant-numeric:tabular-nums;line-height:1}
.kpi-val.pos{color:var(--green)}
.kpi-val.neg{color:var(--red)}
.kpi-val.neu{color:var(--blue)}
.kpi-val.gold{color:var(--gold)}
.kpi-sub{font-size:11px;color:var(--text3);margin-top:4px}

/* ─── Equity curve ───────────────────────────── */
.eq-card{padding:28px;margin-bottom:24px}
.eq-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:24px}
.eq-title{font-size:15px;font-weight:600}
.eq-subtitle{font-size:12px;color:var(--text3);margin-top:2px}
.eq-stats{display:flex;gap:28px}
.eq-stat-lbl{font-size:11px;color:var(--text3);text-align:right}
.eq-stat-val{font-size:17px;font-weight:700;font-variant-numeric:tabular-nums;text-align:right}
#eq-svg{width:100%;height:190px;cursor:crosshair;display:block}
.eq-empty{height:190px;display:flex;align-items:center;justify-content:center;color:var(--text3);font-size:13px;flex-direction:column;gap:8px}
.eq-tooltip{
  position:absolute;background:rgba(7,8,13,.96);border:1px solid var(--border-hi);
  border-radius:9px;padding:9px 14px;font-size:12px;pointer-events:none;
  z-index:50;display:none;white-space:nowrap
}

/* ─── Mid grid ───────────────────────────────── */
.mid-grid{display:grid;grid-template-columns:2fr 1fr;gap:16px;margin-bottom:24px}

/* ─── Positions ──────────────────────────────── */
.pos-empty{
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  padding:48px 24px;color:var(--text3);gap:8px;text-align:center
}
.pos-empty-icon{font-size:30px;opacity:.35}
.pos-header{
  display:grid;gap:16px;
  padding-bottom:10px;border-bottom:1px solid var(--border);
  font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.06em
}
.pos-row{
  display:grid;gap:16px;align-items:center;
  padding:14px 0;border-bottom:1px solid rgba(255,255,255,0.04)
}
.pos-row:last-child{border-bottom:none}
.pos-sym{font-size:14px;font-weight:700;letter-spacing:-.3px}
.side-tag{
  display:inline-flex;align-items:center;padding:3px 9px;
  border-radius:6px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.04em
}
.side-long{background:var(--green-soft);color:var(--green)}
.side-short{background:var(--red-soft);color:var(--red)}
.price-block .lbl{font-size:10px;color:var(--text3);margin-bottom:2px}
.price-block .val{font-size:14px;font-weight:600;font-variant-numeric:tabular-nums}
.val-sl{color:var(--red)}
.val-tp{color:var(--green)}
.rr-block .lbl{font-size:10px;color:var(--text3);margin-bottom:2px}
.rr-block .val{font-size:14px;font-weight:600}
.rr-good{color:var(--green)}
.rr-ok{color:var(--gold)}

/* ─── Regime panel ───────────────────────────── */
.regime-panel{
  display:flex;flex-direction:column;align-items:center;
  justify-content:center;min-height:200px;gap:16px;text-align:center;padding:8px
}
.regime-ring{
  width:84px;height:84px;border-radius:50%;display:flex;
  align-items:center;justify-content:center;font-size:30px;position:relative
}
.ring-bull{
  background:radial-gradient(circle,rgba(16,185,129,.14),rgba(16,185,129,.04));
  border:2px solid rgba(16,185,129,.3);
  box-shadow:0 0 32px rgba(16,185,129,.15);
  animation:ringpulse 3s ease-in-out infinite
}
.ring-bear{
  background:radial-gradient(circle,rgba(244,63,94,.14),rgba(244,63,94,.04));
  border:2px solid rgba(244,63,94,.3);
  box-shadow:0 0 32px rgba(244,63,94,.15)
}
.ring-neutral{background:rgba(255,255,255,.04);border:2px solid var(--border)}
@keyframes ringpulse{
  0%,100%{box-shadow:0 0 32px rgba(16,185,129,.15)}
  50%{box-shadow:0 0 48px rgba(16,185,129,.28)}
}
.regime-name{font-size:22px;font-weight:800;letter-spacing:-.5px}
.regime-name-bull{color:var(--green)}
.regime-name-bear{color:var(--red)}
.regime-name-neutral{color:var(--text2)}
.regime-desc{font-size:12px;color:var(--text3);max-width:150px;line-height:1.5}
.regime-vix{
  display:inline-flex;align-items:center;gap:6px;
  background:var(--surface);border:1px solid var(--border);
  border-radius:99px;padding:4px 12px;font-size:11px;color:var(--text2)
}

/* ─── Bottom grid ────────────────────────────── */
.bot-grid{display:grid;grid-template-columns:3fr 2fr;gap:16px;margin-bottom:24px}

/* ─── Trades table ───────────────────────────── */
.tbl{width:100%;border-collapse:collapse}
.tbl th{
  font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;
  letter-spacing:.06em;padding:0 14px 12px 0;text-align:left;
  border-bottom:1px solid var(--border)
}
.tbl td{
  padding:12px 14px 12px 0;font-size:13px;
  border-bottom:1px solid rgba(255,255,255,0.04);
  font-variant-numeric:tabular-nums
}
.tbl tr:last-child td{border-bottom:none}
.tbl tr:hover td{background:rgba(255,255,255,.018)}
.tbl-empty{text-align:center;color:var(--text3);padding:36px!important}
.reason-tag{
  display:inline-block;padding:2px 8px;border-radius:5px;
  font-size:10px;background:rgba(255,255,255,.05);color:var(--text3)
}
.period-tabs{display:flex;gap:4px}
.ptab{
  padding:5px 12px;border-radius:6px;font-size:12px;font-weight:500;
  cursor:pointer;border:1px solid transparent;color:var(--text3);
  transition:all .15s;background:none
}
.ptab.active{background:var(--surface-hi);border-color:var(--border-hi);color:var(--text)}
.ptab:hover:not(.active){color:var(--text2)}

/* ─── Decisions ──────────────────────────────── */
.dec-card{
  padding:16px 0;border-bottom:1px solid var(--border);
  cursor:pointer;transition:opacity .2s
}
.dec-card:last-child{border-bottom:none}
.dec-card:hover{opacity:.82}
.dec-hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.dec-left{display:flex;align-items:center;gap:8px}
.dec-action{
  padding:3px 9px;border-radius:6px;font-size:11px;font-weight:700;
  text-transform:uppercase;letter-spacing:.04em
}
.da-buy{background:var(--green-soft);color:var(--green)}
.da-sell{background:var(--red-soft);color:var(--red)}
.da-hold{background:rgba(255,255,255,.05);color:var(--text3)}
.dec-sym{font-size:13px;font-weight:700}
.dec-time{font-size:11px;color:var(--text3)}
.dec-body{font-size:12px;color:var(--text2);line-height:1.65;max-height:54px;overflow:hidden;transition:max-height .3s ease}
.dec-body.open{max-height:500px}

/* ─── Exit breakdown ─────────────────────────── */
.exit-row{display:flex;align-items:center;gap:12px;margin-bottom:12px}
.exit-row:last-child{margin-bottom:0}
.exit-icon{font-size:16px;width:28px;text-align:center}
.exit-info{flex:1}
.exit-label{font-size:12px;font-weight:500;color:var(--text)}
.exit-bar-wrap{height:3px;background:rgba(255,255,255,.06);border-radius:2px;margin-top:5px;overflow:hidden}
.exit-bar-fill{height:100%;border-radius:2px;transition:width .8s ease}
.exit-nums{text-align:right;min-width:60px}
.exit-pct{font-size:14px;font-weight:700}
.exit-n{font-size:10px;color:var(--text3)}

/* ─── Analysis cards ─────────────────────────── */
.analysis-card{
  padding:16px;background:rgba(255,255,255,.025);border:1px solid var(--border);
  border-radius:var(--r-sm);margin-bottom:12px;transition:border-color .2s
}
.analysis-card:last-child{margin-bottom:0}
.analysis-card:hover{border-color:var(--border-hi)}
.analysis-hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.outcome-tag{padding:3px 9px;border-radius:6px;font-size:11px;font-weight:700;text-transform:uppercase}
.ot-win{background:var(--green-soft);color:var(--green)}
.ot-loss{background:var(--red-soft);color:var(--red)}
.analysis-text{font-size:12px;color:var(--text2);line-height:1.65;margin-bottom:10px}
.lesson{display:flex;gap:7px;font-size:11px;color:var(--blue);margin-bottom:3px;line-height:1.5}
.lesson::before{content:'→';color:var(--text3);flex-shrink:0}
.mistake{color:var(--red)}

/* ─── Refresh bar ────────────────────────────── */
.rfbar{position:fixed;bottom:0;left:0;right:0;height:2px;background:rgba(255,255,255,.03);z-index:200}
.rfbar-fill{height:100%;background:linear-gradient(90deg,var(--green),#059669);width:0%;transition:none}

/* ─── Responsive ─────────────────────────────── */
@media(max-width:1200px){
  .kpi-row{grid-template-columns:repeat(3,1fr)}
  .mid-grid{grid-template-columns:1fr}
  .bot-grid{grid-template-columns:1fr}
}
@media(max-width:768px){
  .kpi-row{grid-template-columns:repeat(2,1fr)}
  .hero-grid{grid-template-columns:1fr}
  .hero-aside{display:none}
  .hero-value{font-size:38px}
  .wrap{padding:0 16px 40px}
}
@media(max-width:480px){
  .kpi-row{grid-template-columns:repeat(2,1fr)}
}

/* ─── Strategy Analytics ─────────────────────────── */
.strat-card{padding:24px}
.strat-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;gap:16px;flex-wrap:wrap}
.strat-title{font-size:11px;font-weight:600;letter-spacing:.18em;text-transform:uppercase;color:var(--text2)}
.strat-title em{color:var(--text3);font-style:normal;font-weight:400;text-transform:none;letter-spacing:.02em;margin-left:8px}
.strat-state-chips{display:flex;gap:6px;flex-wrap:wrap}
.s-chip{display:inline-flex;align-items:center;gap:6px;padding:4px 11px;border-radius:99px;font-size:10px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;border:1px solid var(--border);font-family:'SF Mono','Monaco',monospace}
.s-chip-paper{background:var(--gold-soft);color:var(--gold);border-color:rgba(245,158,11,0.28)}
.s-chip-live{background:var(--green-soft);color:var(--green);border-color:rgba(16,185,129,0.28)}
.s-chip-mode{background:var(--blue-soft);color:var(--blue);border-color:rgba(129,140,248,0.28)}
.s-chip-regime{background:var(--surface);color:var(--text2)}
.s-chip-bias{background:var(--surface);color:var(--text2)}

.modes-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
.mode-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-sm);padding:18px;transition:border-color .2s,background .2s}
.mode-card.edge{border-color:rgba(16,185,129,0.42);background:linear-gradient(180deg,rgba(16,185,129,0.04),transparent 60%)}
.mode-card.marginal{border-color:rgba(245,158,11,0.42);background:linear-gradient(180deg,rgba(245,158,11,0.04),transparent 60%)}
.mode-card.negative{border-color:rgba(244,63,94,0.42);background:linear-gradient(180deg,rgba(244,63,94,0.04),transparent 60%)}
.mode-card.plumbing{border-color:rgba(129,140,248,0.36);background:linear-gradient(180deg,rgba(129,140,248,0.04),transparent 60%)}
.mode-card.nodata{opacity:.55}
.mode-card.active-mode{box-shadow:0 0 0 1px var(--border-hi) inset}

.mode-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
.mode-name{font-size:12px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--text);display:flex;align-items:center;gap:6px}
.mode-name .active-dot{width:6px;height:6px;border-radius:50%;background:var(--green);box-shadow:0 0 6px var(--green)}
.mode-badge{font-size:9px;font-weight:700;letter-spacing:.08em;padding:3px 8px;border-radius:4px;text-transform:uppercase;font-family:'SF Mono','Monaco',monospace}
.mode-badge.edge{background:var(--green-soft);color:var(--green)}
.mode-badge.marginal{background:var(--gold-soft);color:var(--gold)}
.mode-badge.negative{background:var(--red-soft);color:var(--red)}
.mode-badge.plumbing{background:var(--blue-soft);color:var(--blue)}
.mode-badge.nodata{background:var(--surface);color:var(--text3);border:1px solid var(--border)}

.mode-verdict{font-size:11px;color:var(--text2);margin-bottom:14px;min-height:14px;line-height:1.4}

.mode-pnl{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding-bottom:14px;margin-bottom:14px;border-bottom:1px solid var(--border)}
.mode-pnl-block .lbl{font-size:9px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--text3);margin-bottom:4px}
.mode-pnl-block .val{font-family:'SF Mono','Monaco',monospace;font-size:20px;font-weight:600;letter-spacing:-.02em;line-height:1}
.mode-pnl-block .val.pos{color:var(--green)}
.mode-pnl-block .val.neg{color:var(--red)}
.mode-pnl-block .val.neu{color:var(--text)}

.mode-metrics{display:grid;grid-template-columns:repeat(2,1fr);gap:5px 18px}
.mode-metric{display:flex;justify-content:space-between;align-items:baseline;padding:1px 0}
.mode-metric .k{color:var(--text3);font-size:10px;letter-spacing:.04em}
.mode-metric .v{font-family:'SF Mono','Monaco',monospace;font-weight:500;font-size:12px;color:var(--text)}
.mode-metric .v.pos{color:var(--green)}
.mode-metric .v.neg{color:var(--red)}

.mode-foot{margin-top:12px;padding-top:10px;border-top:1px dashed var(--border);font-size:10px;color:var(--text3);font-family:'SF Mono','Monaco',monospace;display:flex;justify-content:space-between}

.strat-confidence{margin-top:16px;padding:13px 17px;background:var(--surface);border:1px solid var(--border);border-radius:var(--r-sm);font-size:12px;color:var(--text2);line-height:1.5}
.strat-confidence strong{color:var(--text);font-weight:600;letter-spacing:.02em}
.strat-confidence.positive strong{color:var(--green)}
.strat-confidence.negative strong{color:var(--red)}
.strat-confidence.neutral strong{color:var(--gold)}

/* ─── LLM Cost Observability ─────────────────────── */
.llm-card{padding:24px}
.llm-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;gap:16px;flex-wrap:wrap}
.llm-window-tabs{display:flex;gap:4px;padding:3px;background:var(--surface);border-radius:6px;border:1px solid var(--border)}
.llm-tab{padding:4px 12px;font-size:10px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;background:transparent;border:none;color:var(--text3);cursor:pointer;border-radius:4px;transition:all .15s;font-family:'SF Mono','Monaco',monospace}
.llm-tab:hover{color:var(--text2)}
.llm-tab.active{background:var(--surface-hi);color:var(--text)}

.llm-kpi-row{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px}
.llm-kpi{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-sm);padding:14px 16px}
.llm-kpi .lbl{font-size:9px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--text3);margin-bottom:4px}
.llm-kpi .val{font-family:'SF Mono','Monaco',monospace;font-size:22px;font-weight:600;letter-spacing:-.02em;color:var(--text);line-height:1}
.llm-kpi .val.warn{color:var(--gold)}
.llm-kpi .val.crit{color:var(--red)}
.llm-kpi .val.good{color:var(--green)}
.llm-kpi .sub{font-family:'SF Mono','Monaco',monospace;font-size:10px;color:var(--text3);margin-top:5px}

.llm-breakdown{display:grid;grid-template-columns:1.4fr 1fr 1fr;gap:12px}
.llm-panel{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-sm);padding:13px 15px;min-height:140px}
.llm-panel-title{font-size:9px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--text3);margin-bottom:10px}
.llm-row{display:flex;align-items:center;padding:4px 0;font-family:'SF Mono','Monaco',monospace;font-size:11px;border-top:1px dashed transparent}
.llm-row+.llm-row{border-top-color:rgba(255,255,255,0.04)}
.llm-row .label{color:var(--text2);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding-right:8px}
.llm-row .calls{color:var(--text3);margin-right:10px;min-width:36px;text-align:right;font-size:10px}
.llm-row .cost{color:var(--text);font-weight:500;min-width:60px;text-align:right}
.llm-row .cost.high{color:var(--red)}
.llm-row .cost.mid{color:var(--gold)}
.llm-empty{color:var(--text3);font-size:11px;font-style:italic;padding:8px 0}

.llm-footer{margin-top:12px;padding-top:10px;border-top:1px dashed var(--border);display:flex;justify-content:space-between;font-family:'SF Mono','Monaco',monospace;font-size:10px;color:var(--text3)}

@media(max-width:980px){
  .llm-kpi-row{grid-template-columns:repeat(2,1fr)}
  .llm-breakdown{grid-template-columns:1fr}
}

/* ─── Tab Navigation ─────────────────────────────── */
.tab-nav{display:flex;gap:4px;padding:4px;background:var(--surface);border:1px solid var(--border);border-radius:10px;margin-bottom:24px;width:fit-content}
.tab-btn{padding:8px 18px;font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;background:transparent;border:none;color:var(--text3);cursor:pointer;border-radius:7px;transition:all .15s;font-family:'SF Mono','Monaco',monospace;display:flex;align-items:center;gap:8px}
.tab-btn:hover{color:var(--text2)}
.tab-btn.active{background:var(--surface-hi);color:var(--text);box-shadow:0 1px 3px rgba(0,0,0,0.15)}
.tab-btn .tab-count{font-size:9px;background:var(--surface);padding:2px 6px;border-radius:99px;color:var(--text3);border:1px solid var(--border)}
.tab-btn.active .tab-count{background:var(--green-soft);color:var(--green);border-color:rgba(16,185,129,0.25)}

/* Tab pane visibility — controlled by .wrap[data-active-tab="X"] */
.wrap[data-active-tab="overview"] [data-tab]:not([data-tab="overview"]){display:none}
.wrap[data-active-tab="trades"]   [data-tab]:not([data-tab="trades"]){display:none}
.wrap[data-active-tab="strategy"] [data-tab]:not([data-tab="strategy"]){display:none}
.wrap[data-active-tab="cost"]     [data-tab]:not([data-tab="cost"]){display:none}

@media(max-width:520px){
  .tab-nav{width:100%;overflow-x:auto}
  .tab-btn{padding:8px 12px;flex-shrink:0}
}

/* ─── Crypto regime strip ────────────────────────── */
.cregime-strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:10px;margin-bottom:14px}
.creg-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-sm);padding:14px 16px}
.creg-card.go-long{border-color:rgba(16,185,129,0.4)}
.creg-card.go-short{border-color:rgba(244,63,94,0.4)}
.creg-card.go-both{border-color:rgba(245,158,11,0.4)}
.creg-card.block{border-color:var(--border);opacity:.7}
.creg-row1{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px}
.creg-sym{font-size:11px;font-weight:700;letter-spacing:.1em;color:var(--text2)}
.creg-state{font-family:'SF Mono','Monaco',monospace;font-size:13px;font-weight:600;letter-spacing:.02em}
.creg-state.go-long{color:var(--green)}
.creg-state.go-short{color:var(--red)}
.creg-state.go-both{color:var(--gold)}
.creg-state.block{color:var(--text3)}
.creg-row2{display:flex;align-items:center;gap:10px;margin-top:8px}
.creg-conf-bar{flex:1;height:5px;background:rgba(255,255,255,0.06);border-radius:3px;overflow:hidden}
.creg-conf-fill{height:100%;border-radius:3px;transition:width .3s,background .3s}
.creg-conf-num{font-family:'SF Mono','Monaco',monospace;font-size:11px;font-weight:600;color:var(--text2);min-width:42px;text-align:right}
.creg-feats{display:flex;justify-content:space-between;gap:8px;margin-top:8px;font-family:'SF Mono','Monaco',monospace;font-size:10px;color:var(--text3);flex-wrap:wrap}
.creg-feats span b{color:var(--text2);font-weight:500}
.creg-disabled{padding:12px 14px;background:var(--surface);border:1px dashed var(--border);border-radius:var(--r-sm);font-size:11px;color:var(--text3);text-align:center;margin-bottom:14px;font-family:'SF Mono','Monaco',monospace;letter-spacing:.04em}
/* ── Live trade cards (Phase 4.1 enriched positions view) ─────────────────── */
.pos-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:12px}
.pos-card.card-long{border-left:3px solid var(--green)}
.pos-card.card-short{border-left:3px solid var(--red)}
.pos-card-header{display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap}
.thesis-chip{font-size:10px;padding:3px 8px;background:rgba(255,255,255,0.05);border-radius:6px;color:var(--text3);font-weight:600;letter-spacing:.04em;text-transform:uppercase}
.pos-card-prices{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:14px}
.pos-card-metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;padding:12px 0;border-top:1px solid var(--border);border-bottom:1px solid var(--border);margin-bottom:14px}
.val-live{color:var(--text);font-weight:700}
.metric-block .lbl{font-size:10px;color:var(--text3);margin-bottom:4px;font-weight:600;letter-spacing:.04em;text-transform:uppercase}
.metric-block .val{font-size:14px;font-weight:600;font-variant-numeric:tabular-nums}
.metric-secondary{font-size:11px;font-weight:400;color:var(--text3)}
.metric-tertiary{font-size:10px;color:var(--text3);margin-top:2px;font-style:italic}
.pnl-pos{color:var(--green)}
.pnl-neg{color:var(--red)}
.pos-progress-wrap{padding-top:16px}
.progress-track{position:relative;height:32px;border-radius:16px;background:linear-gradient(to right,rgba(244,63,94,0.18) 0%,rgba(244,63,94,0.04) 40%,rgba(16,185,129,0.04) 60%,rgba(16,185,129,0.18) 100%);border:1px solid var(--border);overflow:visible}
.progress-label-left,.progress-label-right{position:absolute;top:50%;transform:translateY(-50%);font-size:10px;font-weight:700;letter-spacing:.08em;pointer-events:none}
.progress-label-left{left:10px;color:var(--red)}
.progress-label-right{right:10px;color:var(--green)}
.progress-marker-entry{position:absolute;top:4px;bottom:4px;width:2px;background:rgba(255,255,255,0.4);transform:translateX(-1px);z-index:1}
.progress-marker-entry span{position:absolute;top:-14px;left:50%;transform:translateX(-50%);font-size:8px;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;font-weight:600}
.progress-marker-live{position:absolute;top:-4px;bottom:-4px;width:4px;border-radius:2px;transform:translateX(-2px);z-index:2;transition:left .4s cubic-bezier(.4,0,.2,1)}
.progress-marker-live.live-long{background:var(--green);box-shadow:0 0 12px rgba(16,185,129,.6)}
.progress-marker-live.live-short{background:var(--green);box-shadow:0 0 12px rgba(16,185,129,.6)}
</style>
</head>
<body>
<div class="wrap" data-active-tab="overview">

<!-- ═══ HEADER ══════════════════════════════════════════════════════════════ -->
<header>
  <div class="logo">
    <div class="logo-mark">J</div>
    <div>
      <div class="logo-name">Jim Bot <em>/ AI Trading OS</em></div>
    </div>
  </div>
  <div class="hdr-right">
    <div id="regime-pill" class="regime-pill rp-neutral">
      <div class="regime-pip"></div>
      <span id="regime-pill-text">—</span>
    </div>
    <div class="status-chip">
      <div class="live-dot"></div>
      <div id="clock">—</div>
    </div>
  </div>
</header>

<!-- ═══ TAB NAVIGATION ═══════════════════════════════════════════════════════ -->
<nav class="tab-nav" role="tablist">
  <button class="tab-btn active" data-target-tab="overview" role="tab">Overview</button>
  <button class="tab-btn"        data-target-tab="trades"   role="tab">Trades <span class="tab-count" id="tab-count-trades">—</span></button>
  <button class="tab-btn"        data-target-tab="strategy" role="tab">Strategy</button>
  <button class="tab-btn"        data-target-tab="cost"     role="tab">Cost</button>
</nav>

<!-- ═══ HERO ═════════════════════════════════════════════════════════════════ -->
<div class="card hero-card" data-tab="overview" style="animation-delay:.02s">
  <div class="hero-grid">
    <div>
      <div class="hero-label">Valeur du Portfolio</div>
      <div class="hero-value" id="hero-val">—</div>
      <div class="hero-change">
        <div class="pill pill-neu" id="hero-pill">—</div>
        <div class="hero-meta" id="hero-meta">depuis le démarrage</div>
      </div>
    </div>
    <div class="hero-aside">
      <div class="hero-aside-label">Capital initial</div>
      <div class="hero-aside-val" id="hero-start">—</div>
      <div class="hero-aside-badge">
        <div class="broker-dot"></div>
        <span class="broker-name">Kraken Futures</span>
      </div>
    </div>
  </div>
</div>

<!-- ═══ STRATEGY ANALYTICS ════════════════════════════════════════════════════ -->
<div class="card strat-card" data-tab="strategy" style="animation-delay:.04s">
  <div class="strat-header">
    <div class="strat-title">Strategy Analytics <em>— confidence layer</em></div>
    <div class="strat-state-chips" id="strat-state-chips"></div>
  </div>
  <div class="cregime-strip" id="cregime-strip"></div>
  <div class="modes-grid" id="modes-grid"></div>
  <div class="strat-confidence" id="strat-confidence">Loading…</div>
</div>

<!-- ═══ LLM COST OBSERVABILITY ═════════════════════════════════════════════ -->
<div class="card llm-card" data-tab="cost" style="animation-delay:.06s">
  <div class="llm-header">
    <div class="strat-title">LLM Cost Observability <em>— OpenClaw + Claude Code</em></div>
    <div class="llm-window-tabs" id="llm-window-tabs">
      <button class="llm-tab" data-h="24">24h</button>
      <button class="llm-tab active" data-h="168">7d</button>
      <button class="llm-tab" data-h="720">30d</button>
    </div>
  </div>
  <div class="llm-kpi-row" id="llm-kpi-row"></div>
  <div class="llm-breakdown" id="llm-breakdown"></div>
  <div class="llm-footer" id="llm-footer"></div>
</div>

<!-- ═══ KPI STRIP ════════════════════════════════════════════════════════════ -->
<div class="kpi-row" data-tab="overview">
  <div class="kpi">
    <div class="kpi-lbl">Win Rate</div>
    <div class="kpi-val neu" id="kpi-wr">—</div>
    <div class="kpi-sub" id="kpi-wr-sub">trades fermés : —</div>
  </div>
  <div class="kpi">
    <div class="kpi-lbl">Profit Factor</div>
    <div class="kpi-val neu" id="kpi-pf">—</div>
    <div class="kpi-sub">&gt; 1.5 = excellent</div>
  </div>
  <div class="kpi">
    <div class="kpi-lbl">Espérance / trade</div>
    <div class="kpi-val" id="kpi-exp">—</div>
    <div class="kpi-sub">gain moyen attendu</div>
  </div>
  <div class="kpi">
    <div class="kpi-lbl">Max Drawdown</div>
    <div class="kpi-val neg" id="kpi-dd">—</div>
    <div class="kpi-sub">perte cumulée max</div>
  </div>
  <div class="kpi">
    <div class="kpi-lbl">Capital déployé</div>
    <div class="kpi-val gold" id="kpi-dep">—</div>
    <div class="kpi-sub" id="kpi-open-sub">positions ouvertes : —</div>
  </div>
</div>

<!-- ═══ EQUITY CURVE ══════════════════════════════════════════════════════════ -->
<div class="card eq-card" data-tab="overview" style="animation-delay:.08s">
  <div class="eq-top">
    <div>
      <div class="eq-title">Courbe d'équité</div>
      <div class="eq-subtitle">Performance cumulée — Paper Trading</div>
    </div>
    <div class="eq-stats">
      <div>
        <div class="eq-stat-lbl">Retour total</div>
        <div class="eq-stat-val" id="eq-ret">—</div>
      </div>
      <div>
        <div class="eq-stat-lbl">Capital actuel</div>
        <div class="eq-stat-val" id="eq-now">—</div>
      </div>
    </div>
  </div>
  <div style="position:relative">
    <div class="eq-empty" id="eq-empty">
      <span style="font-size:22px;opacity:.3">◈</span>
      <span>En attente du premier trade...</span>
    </div>
    <svg id="eq-svg" style="display:none"></svg>
    <div class="eq-tooltip" id="eq-tip"></div>
  </div>
</div>

<!-- ═══ MID GRID : Positions + Regime ════════════════════════════════════════ -->
<div class="mid-grid" data-tab="overview">

  <!-- Positions -->
  <div class="card" style="animation-delay:.10s">
    <div class="sec-label">
      Positions ouvertes
      <span id="pos-cnt" style="color:var(--text2);font-weight:700;text-transform:none;letter-spacing:0;font-size:12px;"></span>
    </div>
    <div id="pos-container">
      <div class="pos-empty">
        <div class="pos-empty-icon">◎</div>
        <div style="font-size:13px">Aucune position ouverte</div>
        <div style="font-size:11px">Le bot scanne les marchés en continu</div>
      </div>
    </div>
  </div>

  <!-- Regime -->
  <div class="card" style="animation-delay:.12s">
    <div class="sec-label">Crypto Market State <span style="color:var(--text3);font-weight:400;text-transform:none;letter-spacing:0;font-size:11px">— ETH/SOL crypto-native regime</span></div>
    <div class="regime-panel">
      <div class="regime-ring ring-neutral" id="reg-ring"><span id="reg-icon">◔</span></div>
      <div class="regime-name regime-name-neutral" id="reg-name">—</div>
      <div class="regime-desc" id="reg-desc">Analyse du régime en cours...</div>
      <div class="regime-vix" id="reg-vix" style="display:none">
        VIX <strong id="reg-vix-val">—</strong>
      </div>
    </div>
  </div>

</div>

<!-- ═══ BOT GRID : Trades + Decisions ════════════════════════════════════════ -->
<div class="bot-grid" data-tab="trades">

  <!-- Trade History -->
  <div class="card" style="animation-delay:.14s">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:20px">
      <div class="sec-label" style="margin-bottom:0;flex:1">Historique des trades</div>
      <div class="period-tabs">
        <button class="ptab active" onclick="setP(this,'recent')">Récents</button>
        <button class="ptab" onclick="setP(this,'7d')">7j</button>
        <button class="ptab" onclick="setP(this,'mtd')">MTD</button>
        <button class="ptab" onclick="setP(this,'all')">Tout</button>
      </div>
    </div>
    <div style="overflow-x:auto">
      <table class="tbl">
        <thead>
          <tr>
            <th>Symbole</th><th>Dir.</th><th>Entrée</th><th>Sortie</th>
            <th title="Gross PnL = (entry - exit) × qty, no fees or slippage applied">Gross</th>
            <th title="Estimated fees: taker 0.05% × entry + taker 0.05% × exit">Fees est.</th>
            <th title="Estimated slippage: 5 bps each leg (entry+exit) — paper Kraken may differ">Slip est.</th>
            <th title="Net PnL est. = Gross − Fees est. − Slip est.">Net est.</th>
            <th title="In the pocket = Net est. × (1 − 0.30 tax). Placeholder 30%, not real tax advice.">In pocket</th>
            <th>P&L %</th><th>Durée</th><th>Raison</th>
          </tr>
        </thead>
        <tbody id="trades-body">
          <tr><td class="tbl-empty" colspan="13">Chargement...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- AI Decisions + Exit breakdown -->
  <div style="display:flex;flex-direction:column;gap:16px">

    <div class="card" style="animation-delay:.16s">
      <div class="sec-label">AI Decisions</div>
      <div id="dec-container">
        <div style="text-align:center;color:var(--text3);padding:28px 0;font-size:13px">Aucune décision récente</div>
      </div>
    </div>

    <div class="card" style="animation-delay:.18s">
      <div class="sec-label">Exits breakdown</div>
      <div id="exits-container">
        <div style="text-align:center;color:var(--text3);padding:20px 0;font-size:13px">—</div>
      </div>
    </div>

  </div>
</div>

<!-- ═══ POST-TRADE ANALYSES ═══════════════════════════════════════════════════ -->
<div class="card" data-tab="trades" style="animation-delay:.20s">
  <div class="sec-label">Analyses post-trade — Leçons apprises</div>
  <div id="analyses-container" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px">
    <div style="text-align:center;color:var(--text3);padding:32px;grid-column:1/-1;font-size:13px">Aucune analyse disponible</div>
  </div>
</div>

</div><!-- /wrap -->
<div class="rfbar"><div class="rfbar-fill" id="rfbar-fill"></div></div>

<!-- ═══════════════════════════════════════════════════════════════════════════
     JAVASCRIPT
════════════════════════════════════════════════════════════════════════════ -->
<script>
const INTERVAL = 15000;
let _eqPts = [];
let _period = 'recent';

const $  = id => document.getElementById(id);
const f$ = (v,d=2) => {
  if (v===null||v===undefined||isNaN(v)) return '—';
  return '$' + Math.abs(parseFloat(v)).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
};
const fPct = (v,d=1) => {
  if (v===null||v===undefined||isNaN(v)) return '—';
  const n = parseFloat(v);
  return (n>=0?'+':'') + n.toFixed(d) + '%';
};
const fDur = m => {
  if (!m) return '—';
  const n = Math.round(m);
  return n < 60 ? n+'min' : Math.floor(n/60)+'h'+(n%60?n%60+'m':'');
};
const pc  = v => parseFloat(v) >= 0 ? 'pos' : 'neg';
const api = async url => { try { const r = await fetch(url); return await r.json(); } catch { return null; }};

/* ── Clock ──────────────────────────────────────────────────────────────── */
const tick = () => $('clock').textContent = new Date().toLocaleTimeString('fr-FR') + ' UTC';
setInterval(tick, 1000); tick();

/* ── Regime ─────────────────────────────────────────────────────────────── */
async function loadRegime() {
  // Fetch BOTH the macro (legacy VIX/SPY) and crypto regime in parallel.
  // The header pill + mid-grid Regime card now reflect CRYPTO state — macro
  // VIX is shown as small secondary context only.
  const [macro, crypto] = await Promise.all([
    api('/api/regime'),
    api('/api/crypto-regime'),
  ]);

  const pill  = $('regime-pill');
  const ring  = $('reg-ring');
  const name  = $('reg-name');
  const icon  = $('reg-icon');
  const desc  = $('reg-desc');
  const vixEl = $('reg-vix');
  const vixV  = $('reg-vix-val');

  // Default safe state
  let headerLabel = '—';
  let headerCls   = 'rp-neutral';
  let ringCls     = 'ring-neutral';
  let nameCls     = 'regime-name-neutral';
  let nameText    = '—';
  let iconText    = '◔';
  let descText    = 'En attente du régime crypto…';

  if (crypto && crypto.enabled && crypto.by_symbol) {
    const symbols = Object.values(crypto.by_symbol);
    if (symbols.length > 0) {
      const anyLong   = symbols.some(s => s.allow_long);
      const anyShort  = symbols.some(s => s.allow_short);
      const allBlock  = symbols.every(s => !s.allow_long && !s.allow_short);

      // Color the pill by directional bias
      if (anyLong && !anyShort)        { headerCls='rp-bull';    ringCls='ring-bull';    nameCls='regime-name-bull';    iconText='↗'; }
      else if (anyShort && !anyLong)   { headerCls='rp-bear';    ringCls='ring-bear';    nameCls='regime-name-bear';    iconText='↘'; }
      else if (anyLong && anyShort)    { headerCls='rp-neutral'; ringCls='ring-neutral'; nameCls='regime-name-neutral'; iconText='⇅'; }
      else                              { headerCls='rp-neutral'; ringCls='ring-neutral'; nameCls='regime-name-neutral'; iconText='◔'; }

      // Header pill : compact symbol summary "ETH↓80 SOL↓75"
      headerLabel = symbols.map(s => {
        const sym = (s.symbol || '').split('/')[0];
        const d   = s.direction || 0;
        const arr = d > 0.3 ? '↑' : d < -0.3 ? '↓' : '·';
        return `${sym}${arr}${Math.abs((s.confidence || 0)).toFixed(0)}`;
      }).join(' ');

      // Mid-grid card : worst-state symbol as primary, full text
      // Pick the symbol with lowest confidence (most uncertain)
      const primary = [...symbols].sort((a,b) => (a.confidence||0) - (b.confidence||0))[0];
      const state    = primary.state || 'unknown';
      const conf     = primary.confidence || 0;
      const dir      = primary.direction || 0;
      const ethSig   = primary.eth_signal || '—';
      const btc      = primary.btc_state || '—';
      const reason   = primary.policy_reason || '';

      nameText = state.replace(/_/g,' ');
      descText = `${(primary.symbol||'').split('/')[0]} dir ${dir>0?'+':''}${dir.toFixed(2)} · conf ${conf.toFixed(0)} · eth=${ethSig} · btc=${btc}`;
      if (allBlock) descText += ' · no trade';
    }
  } else if (crypto && !crypto.enabled) {
    headerLabel = 'crypto gate OFF';
    descText    = 'Crypto regime gate disabled — old VIX/SPY regime active';
  }

  // Apply
  $('regime-pill-text').textContent = headerLabel;
  pill.className = 'regime-pill ' + headerCls;
  ring.className = 'regime-ring ' + ringCls;
  name.className = 'regime-name ' + nameCls;
  name.textContent = nameText;
  icon.textContent = iconText;
  desc.textContent = descText;

  // VIX as small footnote (macro context)
  if (macro && macro.vix !== null && macro.vix !== undefined) {
    vixEl.style.display = 'inline-flex';
    vixV.textContent = `${parseFloat(macro.vix).toFixed(1)} · macro ${(macro.regime||'?').toLowerCase()}`;
  }
}

/* ── Hero ───────────────────────────────────────────────────────────────── */
async function loadHero() {
  const d = await api('/api/experts/stats');
  if (!d || !d.geo_v4) return;
  const g = d.geo_v4;
  const now   = g.capital_now   ?? 0;
  const start = g.capital_start ?? 0;
  const ret   = g.capital_return ?? 0;

  $('hero-val').textContent  = '$' + now.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
  $('hero-start').textContent = '$' + start.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});

  const pill = $('hero-pill');
  pill.textContent = (ret>=0?'+':'') + ret.toFixed(2) + '% all-time';
  pill.className = 'pill ' + (ret>0?'pill-pos':ret<0?'pill-neg':'pill-neu');
}

/* ── KPIs ───────────────────────────────────────────────────────────────── */
async function loadKPIs() {
  const [st, ex, op] = await Promise.all([
    api('/api/stats'), api('/api/experts/stats'), api('/api/trades/open')
  ]);
  const g = ex?.geo_v4 || {};
  const wr  = g.win_rate ?? st?.win_rate ?? 0;
  const pf  = g.profit_factor ?? st?.profit_factor ?? 0;
  const exp = st?.expectancy ?? null;
  const dd  = st?.max_drawdown ?? 0;
  const dep = g.deployed ?? 0;
  const cnt = op ? op.length : 0;
  const tot = g.total_trades ?? st?.total_trades ?? 0;

  const wrEl = $('kpi-wr');
  wrEl.textContent = (wr||0).toFixed(1)+'%';
  wrEl.className = 'kpi-val ' + (wr>=55?'pos':wr>=45?'neu':'neg');
  $('kpi-wr-sub').textContent = 'trades fermés : ' + tot;

  const pfEl = $('kpi-pf');
  pfEl.textContent = pf>=999?'∞':parseFloat(pf).toFixed(2);
  pfEl.className = 'kpi-val ' + (pf>=1.5?'pos':pf>=1?'neu':'neg');

  const expEl = $('kpi-exp');
  if (exp !== null && exp !== undefined && !isNaN(exp)) {
    expEl.textContent = (parseFloat(exp)>=0?'+':'') + '$' + Math.abs(parseFloat(exp)).toFixed(4);
    expEl.className = 'kpi-val ' + (parseFloat(exp)>=0?'pos':'neg');
  } else {
    expEl.textContent = '—';
    expEl.className = 'kpi-val';
  }

  $('kpi-dd').textContent = dd ? '-$'+parseFloat(dd).toFixed(2) : '—';
  $('kpi-dep').textContent = dep ? '$'+dep.toLocaleString('en-US',{minimumFractionDigits:0,maximumFractionDigits:0}) : '—';
  $('kpi-open-sub').textContent = 'positions ouvertes : ' + cnt;
}

/* ── Equity Curve ───────────────────────────────────────────────────────── */
async function loadEquity() {
  const d = await api('/api/analysis/equity-curve');
  if (!d || !d.points || !d.points.length) {
    $('eq-empty').style.display = 'flex';
    $('eq-svg').style.display = 'none';
    return;
  }
  _eqPts = d.points;

  $('eq-now').textContent = '$' + (d.capital_now||0).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
  const ret = d.capital_start ? (d.capital_now - d.capital_start) / d.capital_start * 100 : 0;
  const retEl = $('eq-ret');
  retEl.textContent = (ret>=0?'+':'') + ret.toFixed(2) + '%';
  retEl.className = 'eq-stat-val ' + (ret>=0?'pos':'neg');

  drawEquity(d.points, d.capital_start);
}

function drawEquity(pts, baseline) {
  const svg = $('eq-svg');
  $('eq-empty').style.display = 'none';
  svg.style.display = 'block';

  const W = svg.parentElement.clientWidth || 900;
  const H = 190;
  const P = {t:16, r:16, b:30, l:60};
  const pw = W - P.l - P.r;
  const ph = H - P.t - P.b;

  const vals = pts.map(p => p.capital);
  const minV = Math.min(...vals, baseline??vals[0]) * 0.9985;
  const maxV = Math.max(...vals, baseline??vals[0]) * 1.0015;
  const span = maxV - minV || 1;

  const X = i => P.l + (i/(pts.length-1||1)) * pw;
  const Y = v => P.t + (1 - (v - minV)/span) * ph;

  let path = `M${X(0)} ${Y(vals[0])}`;
  for (let i=1; i<pts.length; i++) {
    const cx = (X(i-1)+X(i))/2;
    path += ` C${cx} ${Y(vals[i-1])},${cx} ${Y(vals[i])},${X(i)} ${Y(vals[i])}`;
  }
  const area = path + ` L${X(pts.length-1)} ${H-P.b} L${X(0)} ${H-P.b} Z`;
  const pos  = vals[vals.length-1] >= (baseline??vals[0]);
  const lc   = pos ? '#10b981' : '#f43f5e';
  const gc   = pos ? 'rgba(16,185,129,.22)' : 'rgba(244,63,94,.22)';

  let ticks = '';
  for (let i=0; i<=4; i++) {
    const v  = minV + span*i/4;
    const ty = Y(v);
    ticks += `<line x1="${P.l}" y1="${ty}" x2="${W-P.r}" y2="${ty}" stroke="rgba(255,255,255,.04)" stroke-width="1"/>
              <text x="${P.l-6}" y="${ty+4}" text-anchor="end" fill="rgba(255,255,255,.28)" font-size="10" font-family="Inter,sans-serif">$${Math.round(v)}</text>`;
  }

  svg.setAttribute('width', W);
  svg.setAttribute('height', H);
  svg.innerHTML = `
    <defs>
      <linearGradient id="eg" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="${gc}"/>
        <stop offset="100%" stop-color="rgba(0,0,0,0)"/>
      </linearGradient>
      <clipPath id="ec"><rect x="${P.l}" y="${P.t}" width="${pw}" height="${ph+2}"/></clipPath>
    </defs>
    ${ticks}
    <g clip-path="url(#ec)">
      <path d="${area}" fill="url(#eg)"/>
      <path d="${path}" fill="none" stroke="${lc}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
    </g>`;

  svg.onmousemove = e => {
    const r  = svg.getBoundingClientRect();
    const mx = e.clientX - r.left - P.l;
    const i  = Math.max(0, Math.min(pts.length-1, Math.round(mx/pw*(pts.length-1))));
    const pt = pts[i];
    const tip = $('eq-tip');
    const d   = parseFloat(pt.capital) - (baseline??vals[0]);
    tip.style.display = 'block';
    tip.innerHTML = `<div style="color:var(--text3);margin-bottom:3px">${pt.date}</div>
      <div style="font-weight:700;color:var(--text)">$${pt.capital.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}</div>
      <div style="font-size:11px;color:${d>=0?'var(--green)':'var(--red)'};margin-top:1px">${d>=0?'+':''}$${Math.abs(d).toFixed(2)}</div>`;
    tip.style.left = (e.clientX - r.left + 14)+'px';
    tip.style.top  = (e.clientY - r.top  - 52)+'px';
  };
  svg.onmouseleave = () => $('eq-tip').style.display = 'none';
}

/* ── Positions ──────────────────────────────────────────────────────────── */
async function loadPositions() {
  const trades = await api('/api/trades/open');
  const cnt = trades ? trades.length : 0;
  $('pos-cnt').textContent = cnt > 0 ? cnt : '';
  const el = $('pos-container');

  if (!cnt) {
    el.innerHTML = `<div class="pos-empty">
      <div class="pos-empty-icon">◎</div>
      <div style="font-size:13px">Aucune position ouverte</div>
      <div style="font-size:11px">Le bot scanne les marchés en continu</div>
    </div>`;
    return;
  }

  const fmt = (n, dec=2) => Number(n).toLocaleString('en-US',{minimumFractionDigits:dec,maximumFractionDigits:dec});
  const cards = trades.map(t => {
    const entry = parseFloat(t.entry_price || 0);
    const sl    = parseFloat(t.stop_loss   || 0);
    const tp    = parseFloat(t.take_profit || 0);
    const side  = (t.ctx_side || t.side || 'long').toLowerCase();
    const isLong = side === 'long' || side === 'buy';
    const live = (t.live_price !== null && t.live_price !== undefined) ? parseFloat(t.live_price) : null;
    const pnlG = (t.pnl_live_gross !== null && t.pnl_live_gross !== undefined) ? parseFloat(t.pnl_live_gross) : null;
    const pnlP = (t.pnl_live_pct !== null && t.pnl_live_pct !== undefined) ? parseFloat(t.pnl_live_pct) : null;
    const pnlN = (t.pnl_live_net_est !== null && t.pnl_live_net_est !== undefined) ? parseFloat(t.pnl_live_net_est) : null;
    const dSL  = (t.dist_to_sl_abs !== null && t.dist_to_sl_abs !== undefined) ? parseFloat(t.dist_to_sl_abs) : null;
    const dSLp = t.dist_to_sl_pct;
    const dTP  = (t.dist_to_tp_abs !== null && t.dist_to_tp_abs !== undefined) ? parseFloat(t.dist_to_tp_abs) : null;
    const dTPp = t.dist_to_tp_pct;
    const prog = t.progress_pct;
    const ePr  = t.entry_progress_pct;
    const rr   = sl && tp && entry ? Math.abs((tp-entry)/(entry-sl)).toFixed(2) : null;
    const rrNum = rr ? parseFloat(rr) : 0;
    const pnlCls = pnlG !== null ? (pnlG >= 0 ? 'pnl-pos' : 'pnl-neg') : '';
    const pnlSgn = pnlG !== null && pnlG >= 0 ? '+' : '';
    const dec = entry > 100 ? 2 : 4;
    // progress bar (only if all data available)
    let progBar = '';
    if (prog !== null && prog !== undefined && ePr !== null && ePr !== undefined) {
      const pC = Math.max(0, Math.min(100, prog));
      const eC = Math.max(0, Math.min(100, ePr));
      progBar = `
        <div class="pos-progress-wrap">
          <div class="progress-track">
            <span class="progress-label-left">SL</span>
            <span class="progress-label-right">TP</span>
            <div class="progress-marker-entry" style="left:${eC}%"><span>entry</span></div>
            <div class="progress-marker-live ${isLong?'live-long':'live-short'}" style="left:${pC}%"></div>
          </div>
        </div>`;
    }
    return `<div class="pos-card ${isLong?'card-long':'card-short'}">
      <div class="pos-card-header">
        <span class="pos-sym">${t.symbol||'—'}</span>
        <span class="side-tag ${isLong?'side-long':'side-short'}">${isLong?'LONG':'SHORT'}</span>
        <span class="thesis-chip">${t.thesis||'T1'}</span>
        ${t.crypto_regime ? `<span class="thesis-chip">${t.crypto_regime}</span>` : ''}
      </div>
      <div class="pos-card-prices">
        <div class="price-block"><div class="lbl">Entry</div><div class="val">$${fmt(entry,dec)}</div></div>
        <div class="price-block"><div class="lbl">Live</div><div class="val ${live?'val-live':''}">${live!==null?'$'+fmt(live,dec):'—'}</div></div>
        <div class="price-block"><div class="lbl">SL${dSLp!==undefined?` <span class="metric-secondary">(${dSLp>=0?'−':'+'}${Math.abs(dSLp).toFixed(2)}%)</span>`:''}</div><div class="val val-sl">$${fmt(sl,dec)}</div></div>
        <div class="price-block"><div class="lbl">TP${dTPp!==undefined?` <span class="metric-secondary">(+${dTPp.toFixed(2)}%)</span>`:''}</div><div class="val val-tp">$${fmt(tp,dec)}</div></div>
      </div>
      <div class="pos-card-metrics">
        <div class="metric-block">
          <div class="lbl">PnL Live</div>
          <div class="val ${pnlCls}">${pnlG!==null?pnlSgn+'$'+fmt(pnlG):'—'} <span class="metric-secondary">${pnlP!==null?'('+pnlSgn+pnlP.toFixed(2)+'%)':''}</span></div>
          ${pnlN!==null?`<div class="metric-tertiary">Net est. ${pnlN>=0?'+':''}$${fmt(pnlN)}</div>`:''}
        </div>
        <div class="metric-block">
          <div class="lbl">Distance TP</div>
          <div class="val">${dTP!==null?'$'+fmt(dTP):'—'}</div>
          ${dTPp!==undefined?`<div class="metric-tertiary">${dTPp>=0?'+':''}${dTPp.toFixed(2)}%</div>`:''}
        </div>
        <div class="metric-block">
          <div class="lbl">Distance SL</div>
          <div class="val">${dSL!==null?'$'+fmt(dSL):'—'}</div>
          ${dSLp!==undefined?`<div class="metric-tertiary">${dSLp>=0?'−':'+'}${Math.abs(dSLp).toFixed(2)}%</div>`:''}
        </div>
        <div class="metric-block">
          <div class="lbl">R:R</div>
          <div class="val ${rrNum>=2?'rr-good':rrNum>=1.5?'rr-ok':''}">${rr?'1:'+rr:'—'}</div>
        </div>
      </div>
      ${progBar}
    </div>`;
  }).join('');

  el.innerHTML = cards;
}

/* ── Trades ─────────────────────────────────────────────────────────────── */
async function loadTrades(period) {
  let trades;
  if (period === 'recent') {
    trades = await api('/api/trades/recent');
  } else {
    const r = await api('/api/trades/individual?period='+period+'&limit=25');
    trades = r ? r.trades : null;
  }
  const body = $('trades-body');
  if (!trades || !trades.length) {
    body.innerHTML = '<tr><td class="tbl-empty" colspan="13">Aucun trade sur cette période</td></tr>';
    return;
  }
  // Compute daily "in the pocket" (post-tax) sum for each day present.
  // Fallback : si pas de pocket calculé, on retombe sur le PnL broker brut.
  const dayNet = {}, dayCount = {};
  trades.forEach(t => {
    const at = t.entry_at;
    if (!at) return;
    const k = String(at).slice(0, 10);  // YYYY-MM-DD
    const pocket = t.post_tax_pnl_est;
    const fallback = t.pnl;
    const v = (pocket != null) ? parseFloat(pocket) : parseFloat(fallback);
    dayNet[k] = (dayNet[k] || 0) + (isNaN(v) ? 0 : v);
    dayCount[k] = (dayCount[k] || 0) + 1;
  });
  let lastDay = null;
  body.innerHTML = trades.map(t => {
    // Day separator row inserted on day change.
    const at = t.entry_at;
    const dayKey = at ? String(at).slice(0, 10) : 'unknown';
    let sep = '';
    if (dayKey !== lastDay) {
      lastDay = dayKey;
      let dayLabel = '—';
      if (at) {
        try {
          dayLabel = new Date(at).toLocaleDateString('fr-FR', {weekday:'long', day:'2-digit', month:'short', year:'numeric'});
        } catch(e) { dayLabel = dayKey; }
      }
      const net = dayNet[dayKey] || 0;
      const n   = dayCount[dayKey] || 0;
      const netCss = net >= 0 ? 'color:var(--green)' : 'color:var(--red)';
      const netStr = (net >= 0 ? '+' : '−') + '$' + Math.abs(net).toFixed(2);
      sep = `<tr class="day-sep"><td colspan="12" style="background:var(--bg2,#1a1d23);padding:6px 12px;font-size:12px;font-weight:600;border-top:1px solid var(--text3,#666)">
        📅 <span style="color:var(--text1,#fff);text-transform:capitalize">${dayLabel}</span>
        <span style="color:var(--text3,#999);margin-left:14px;font-weight:400">${n} trade${n>1?'s':''}</span>
        <span style="margin-left:14px;${netCss}" title="Net après frais, slippage et impôts (in the pocket)">in pocket ${netStr}</span>
      </td></tr>`;
    }
    const pnl    = t.pnl;
    const isOpen = t.status === 'open';
    const side   = (t.side||'').toLowerCase();
    const isLong = side === 'buy' || side === 'long';
    const pnlCss = pnl!==null ? (parseFloat(pnl)>=0?'color:var(--green)':'color:var(--red)') : '';
    const reason = (t.close_reason||'').replace(/_/g,' ');
    // Use API-computed cost fields if present (Phase 4 instrumentation); fall back to '—' for legacy
    const feesEst = t.fees_est;
    const slipEst = t.slip_est;
    const netEst  = t.net_pnl_est;
    const pocketEst = t.post_tax_pnl_est;
    const netCss  = netEst != null ? (parseFloat(netEst)>=0?'color:var(--green)':'color:var(--red)') : '';
    const pocketCss = pocketEst != null ? (parseFloat(pocketEst)>=0?'color:var(--green)':'color:var(--red)') : '';
    const fmt = (v, signed=false) => v == null ? '—' : (signed && parseFloat(v)>=0 ? '+' : '') + '$' + Math.abs(parseFloat(v)).toFixed(2);
    return sep + `<tr>
      <td style="font-weight:700">${t.symbol||'—'}</td>
      <td><span class="side-tag ${isLong?'side-long':'side-short'}" style="font-size:10px;padding:2px 7px">${isLong?'L':'S'}</span></td>
      <td>$${parseFloat(t.entry_price||0).toFixed(2)}</td>
      <td style="color:var(--blue)">${isOpen?'ouvert':(t.exit_price?'$'+parseFloat(t.exit_price).toFixed(2):'—')}</td>
      <td style="${pnlCss}">${pnl!==null?(parseFloat(pnl)>=0?'+':'')+'$'+Math.abs(parseFloat(pnl)).toFixed(2):'—'}</td>
      <td style="color:var(--text3)">−${fmt(feesEst)}</td>
      <td style="color:var(--text3)">−${fmt(slipEst)}</td>
      <td style="${netCss};font-weight:600">${fmt(netEst, true)}</td>
      <td style="${pocketCss};font-weight:700">${fmt(pocketEst, true)}</td>
      <td style="${pnlCss}">${(t.pnl_pct!=null&&t.pnl_pct!==undefined)?(parseFloat(t.pnl_pct)>=0?'+':'')+parseFloat(t.pnl_pct).toFixed(2)+'%':'—'}</td>
      <td style="color:var(--text3)">${fDur(t.hold_duration_min||t.hold_min)}</td>
      <td><span class="reason-tag">${reason||'—'}</span></td>
    </tr>`;
  }).join('');
}

/* ── Decisions ──────────────────────────────────────────────────────────── */
async function loadDecisions() {
  const d = await api('/api/decisions/recent');
  const el = $('dec-container');
  if (!d || !d.length) {
    el.innerHTML = '<div style="text-align:center;color:var(--text3);padding:24px 0;font-size:13px">Aucune décision récente</div>';
    return;
  }
  el.innerHTML = d.slice(0,6).map(x => {
    const dec    = (x.decision||'hold').toLowerCase();
    const isBuy  = dec === 'buy';
    const isSell = dec === 'sell';
    const cls    = isBuy ? 'da-buy' : isSell ? 'da-sell' : 'da-hold';
    const lbl    = isBuy ? 'LONG'   : isSell ? 'SHORT'   : dec.toUpperCase();
    const time   = x.decided_at ? new Date(x.decided_at).toLocaleTimeString('fr-FR') : '—';
    const text   = (x.reasoning||'').substring(0, 320);
    return `<div class="dec-card" onclick="this.querySelector('.dec-body').classList.toggle('open')">
      <div class="dec-hdr">
        <div class="dec-left">
          <span class="dec-action ${cls}">${lbl}</span>
          <span class="dec-sym">${x.symbol||''}</span>
        </div>
        <span class="dec-time">${time}</span>
      </div>
      <div class="dec-body">${text||'—'}</div>
    </div>`;
  }).join('');
}

/* ── Exits breakdown ────────────────────────────────────────────────────── */
async function loadExits() {
  const d = await api('/api/analysis/exits');
  const el = $('exits-container');
  if (!d || !d.total) {
    el.innerHTML = '<div style="text-align:center;color:var(--text3);padding:20px 0;font-size:13px">Aucune donnée</div>';
    return;
  }
  const types = [
    { key:'target',  icon:'🎯', label:'Take Profit',  color:'var(--green)' },
    { key:'stop',    icon:'🛑', label:'Stop Loss',    color:'var(--red)' },
    { key:'timeout', icon:'⏱',  label:'Timeout',      color:'var(--blue)' },
    { key:'other',   icon:'◈',  label:'Autre',        color:'var(--text3)' },
  ];
  el.innerHTML = types.map(t => {
    const item = d[t.key] || {n:0, pct:0};
    return `<div class="exit-row">
      <div class="exit-icon">${t.icon}</div>
      <div class="exit-info">
        <div class="exit-label">${t.label}</div>
        <div class="exit-bar-wrap">
          <div class="exit-bar-fill" style="width:${item.pct}%;background:${t.color}"></div>
        </div>
      </div>
      <div class="exit-nums">
        <div class="exit-pct" style="color:${t.color}">${item.pct}%</div>
        <div class="exit-n">${item.n} trades</div>
      </div>
    </div>`;
  }).join('');
}

/* ── Analyses ───────────────────────────────────────────────────────────── */
async function loadAnalyses() {
  const a = await api('/api/analyses/recent');
  const el = $('analyses-container');
  if (!a || !a.length) {
    el.innerHTML = '<div style="text-align:center;color:var(--text3);padding:32px;grid-column:1/-1;font-size:13px">Aucune analyse disponible</div>';
    return;
  }
  el.innerHTML = a.map(x => {
    const outcome  = (x.outcome||'').toLowerCase();
    const pnl      = x.pnl;
    const lessons  = Array.isArray(x.lessons)  ? x.lessons  : [];
    const mistakes = Array.isArray(x.mistakes) ? x.mistakes : [];
    const otCls    = outcome==='win'?'ot-win':outcome==='loss'?'ot-loss':'';
    return `<div class="analysis-card">
      <div class="analysis-hdr">
        <strong style="font-size:14px">${x.symbol||'—'}</strong>
        <div style="display:flex;align-items:center;gap:8px">
          ${outcome?`<span class="outcome-tag ${otCls}">${outcome}</span>`:''}
          ${pnl!=null?`<span style="font-weight:700;font-size:13px;color:${parseFloat(pnl)>=0?'var(--green)':'var(--red)'}">${parseFloat(pnl)>=0?'+':''}$${Math.abs(parseFloat(pnl)).toFixed(2)}</span>`:''}
        </div>
      </div>
      <div class="analysis-text">${(x.analysis||'').substring(0,220)}</div>
      ${lessons.map(l=>`<div class="lesson">${l}</div>`).join('')}
      ${mistakes.map(m=>`<div class="lesson mistake">${m}</div>`).join('')}
    </div>`;
  }).join('');
}

/* ── Period toggle ──────────────────────────────────────────────────────── */
function setP(btn, p) {
  document.querySelectorAll('.ptab').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  _period = p;
  loadTrades(p);
}

/* ── Refresh ────────────────────────────────────────────────────────────── */
// ─── Strategy Analytics ───────────────────────────────────────
function _classifyMode(m, ml){
  if(!m || (m.n || 0) === 0)        return 'nodata';
  if((m.n || 0) < 10)               return 'nodata';
  const liveNet  = (ml && ml.net_pnl) || 0;
  const paperNet = m.net_pnl || 0;
  const pf       = m.profit_factor;
  const wr       = m.win_rate || 0;
  if(liveNet > 0 && (pf === null || pf > 1.3) && wr > 0.5) return 'edge';
  if(liveNet > 0)                            return 'marginal';
  if(liveNet < 0 && paperNet > 0)            return 'plumbing';
  return 'negative';
}
function _badgeText(cls){
  return ({edge:'Real edge', marginal:'Unstable', plumbing:'Plumbing',
           negative:'Negative', nodata:'No data'})[cls] || '—';
}
function _verdictText(cls, m){
  if(cls === 'nodata' && (!m || m.n === 0)) return 'Pas encore de trades dans ce mode';
  if(cls === 'nodata')   return `Trop peu de trades (${m.n || 0}) pour conclure`;
  if(cls === 'edge')     return 'Edge net positif après coûts';
  if(cls === 'marginal') return 'Marge positive mais faible — instable';
  if(cls === 'plumbing') return 'Paper positif, live négatif — noise harvesting';
  if(cls === 'negative') return 'Expectancy négatif — plumbing/validation seulement';
  return '—';
}
function _fmt$(v, d=2){
  if(v === null || v === undefined) return '—';
  const sign = v > 0 ? '+' : (v < 0 ? '-' : '');
  return sign + '$' + Math.abs(v).toFixed(d);
}
function _signClass(v){ return v > 0 ? 'pos' : (v < 0 ? 'neg' : 'neu'); }

function _renderModeCard(name, m, ml, isActive){
  const cls = _classifyMode(m, ml);
  const div = document.createElement('div');
  div.className = 'mode-card ' + cls + (isActive ? ' active-mode' : '');
  if(!m || (m.n || 0) === 0){
    div.innerHTML = `
      <div class="mode-head">
        <div class="mode-name">${name}${isActive ? '<span class="active-dot"></span>' : ''}</div>
        <div class="mode-badge nodata">No data</div>
      </div>
      <div class="mode-verdict">Pas encore de trades dans ce mode</div>
      <div class="mode-pnl">
        <div class="mode-pnl-block"><div class="lbl">Net</div><div class="val neu">—</div></div>
        <div class="mode-pnl-block"><div class="lbl">If-live</div><div class="val neu">—</div></div>
      </div>
      <div class="mode-metrics">
        <div class="mode-metric"><span class="k">Trades</span><span class="v">0</span></div>
      </div>`;
    return div;
  }
  const pfDisp = (m.profit_factor === null || m.profit_factor === undefined) ? '∞' : Number(m.profit_factor).toFixed(2);
  const pfClass = (m.profit_factor && m.profit_factor > 1) ? 'pos' : 'neg';
  const liveImpact = (ml && ml.net_pnl !== undefined ? ml.net_pnl : 0) - (m.net_pnl || 0);
  div.innerHTML = `
    <div class="mode-head">
      <div class="mode-name">${name}${isActive ? '<span class="active-dot"></span>' : ''}</div>
      <div class="mode-badge ${cls}">${_badgeText(cls)}</div>
    </div>
    <div class="mode-verdict">${_verdictText(cls, m)}</div>
    <div class="mode-pnl">
      <div class="mode-pnl-block">
        <div class="lbl">Net (paper)</div>
        <div class="val ${_signClass(m.net_pnl)}">${_fmt$(m.net_pnl)}</div>
      </div>
      <div class="mode-pnl-block">
        <div class="lbl">If-live</div>
        <div class="val ${_signClass(ml && ml.net_pnl)}">${_fmt$(ml && ml.net_pnl)}</div>
      </div>
    </div>
    <div class="mode-metrics">
      <div class="mode-metric"><span class="k">Win rate</span><span class="v">${(m.win_rate*100).toFixed(1)}%</span></div>
      <div class="mode-metric"><span class="k">Expectancy</span><span class="v ${_signClass(m.expectancy)}">${_fmt$(m.expectancy)}</span></div>
      <div class="mode-metric"><span class="k">Profit factor</span><span class="v ${pfClass}">${pfDisp}</span></div>
      <div class="mode-metric"><span class="k">Sharpe/trade</span><span class="v ${_signClass(m.sharpe_per_trade)}">${Number(m.sharpe_per_trade).toFixed(3)}</span></div>
      <div class="mode-metric"><span class="k">Max DD</span><span class="v neg">-$${Number(m.max_drawdown).toFixed(0)}</span></div>
      <div class="mode-metric"><span class="k">Trades</span><span class="v">${m.n}</span></div>
      <div class="mode-metric"><span class="k">Avg win</span><span class="v pos">${_fmt$(m.avg_win)}</span></div>
      <div class="mode-metric"><span class="k">Avg loss</span><span class="v neg">${_fmt$(m.avg_loss)}</span></div>
    </div>
    <div class="mode-foot">
      <span>Costs impact</span><span class="${_signClass(liveImpact)}">${_fmt$(liveImpact)}</span>
    </div>`;
  return div;
}

function _renderConfidence(data){
  const ovl = data.overall_live || {};
  const ov  = data.overall || {};
  const n   = ovl.n || 0;
  const el  = $('strat-confidence');
  if(n < 10){
    el.className = 'strat-confidence neutral';
    el.innerHTML = `<strong>Confidence : insufficient</strong> — seulement ${n} trades fermés. Besoin de ≥30 par mode pour conclure statistiquement.`;
    return;
  }
  const liveNet = ovl.net_pnl;
  const paperNet = ov.net_pnl;
  if(liveNet > 0){
    el.className = 'strat-confidence positive';
    el.innerHTML = `<strong>Confidence : cautious positive</strong> — net live-équivalent ${_fmt$(liveNet)} sur ${n} trades. Valider sur plus de cycles avant d'envisager du capital réel.`;
    return;
  }
  el.className = 'strat-confidence negative';
  el.innerHTML = `<strong>Confidence : inadequate for real capital</strong> — net live-équivalent ${_fmt$(liveNet)} sur ${n} trades (paper ${_fmt$(paperNet)}). La stratégie a besoin d'une révision structurelle avant déploiement.`;
}

async function loadCryptoRegime(){
  const data = await api('/api/crypto-regime');
  const strip = $('cregime-strip');
  if(!strip) return;
  strip.innerHTML = '';
  if(!data || data.error){
    strip.innerHTML = '<div class="creg-disabled">crypto regime · endpoint unavailable</div>';
    return;
  }
  if(!data.enabled){
    strip.innerHTML = '<div class="creg-disabled">crypto regime gate · DISABLED (USE_CRYPTO_REGIME_GATE=False) — old VIX/SPY regime active</div>';
    return;
  }
  const bySym = data.by_symbol || {};
  const symbols = data.symbols || Object.keys(bySym);
  const minConf = data.min_conf || 50;
  if(symbols.length === 0){
    strip.innerHTML = '<div class="creg-disabled">crypto regime · waiting for first evaluation…</div>';
    return;
  }
  symbols.forEach(sym => {
    const ev = bySym[sym];
    const card = document.createElement('div');
    card.className = 'creg-card';
    if(!ev || !ev.state){
      card.classList.add('block');
      card.innerHTML = `<div class="creg-row1"><div class="creg-sym">${sym}</div><div class="creg-state block">awaiting…</div></div>`;
      strip.appendChild(card);
      return;
    }
    const st = ev.state;
    const allowL = ev.allow_long, allowS = ev.allow_short;
    let go = 'block';
    if(allowL && allowS) go = 'go-both';
    else if(allowL)      go = 'go-long';
    else if(allowS)      go = 'go-short';
    card.classList.add(go);
    const conf = ev.confidence || 0;
    const confColor = conf >= 75 ? 'var(--green)' : conf >= 50 ? 'var(--gold)' : 'var(--red)';
    const dirSign = ev.direction > 0 ? '+' : '';
    const stateLabel = st.replace(/_/g,' ');
    card.innerHTML = `
      <div class="creg-row1">
        <div class="creg-sym">${sym}</div>
        <div class="creg-state ${go}">${stateLabel}</div>
      </div>
      <div class="creg-row2">
        <div class="creg-conf-bar"><div class="creg-conf-fill" style="width:${Math.min(conf,100)}%;background:${confColor}"></div></div>
        <div class="creg-conf-num">conf ${conf.toFixed(0)}</div>
      </div>
      <div class="creg-feats">
        <span>eth <b>${ev.eth_signal || '—'}</b></span>
        <span>btc <b>${ev.btc_state || (ev.btc_stress_known === false ? 'unknown' : '—')}</b></span>
        <span>dir <b>${dirSign}${(ev.direction||0).toFixed(2)}</b></span>
        <span>vol <b>${ev.vol_state || '—'}</b></span>
        <span>L <b>${allowL ? '✓' : '✗'}</b></span>
        <span>S <b>${allowS ? '✓' : '✗'}</b></span>
      </div>
      <div class="creg-feats" style="margin-top:5px;color:var(--text3);font-style:italic">
        <span style="flex:1">${ev.policy_reason || ''}</span>
      </div>`;
    strip.appendChild(card);
  });
}

async function loadStrategy(){
  await loadCryptoRegime();
  // Phase 4 cleanup: use thesis breakdown (T1L/T1S/T2) instead of legacy modes (lowvol/normal/trend).
  // Lowvol mode was killed on 2026-05-29 for backtest/live parity. See /api/phase4-status.
  const data = await api('/api/modes/stats');
  if(!data || data.error){ return; }
  const cs    = data.current_state || {};
  const chips = $('strat-state-chips');
  chips.innerHTML = '';
  const mk = (cls, text) => { const d = document.createElement('div'); d.className = 's-chip ' + cls; d.textContent = text; chips.appendChild(d); };
  mk(cs.broker_mode === 'paper' ? 's-chip-paper' : 's-chip-live', (cs.broker_mode || '—').toUpperCase());

  // Phase 4 indicator: show ROUTER_VARIANT + T1S hardblock + T2 status (not legacy "mode")
  try {
    const p4 = await api('/api/phase4-status');
    if (p4 && !p4.error) {
      mk('s-chip-mode', 'router · ' + (p4.router_variant || '—'));
      if (p4.t1s_include_hard_block) mk('s-chip-mode', 'T1S hardblock · ON');
      if (p4.enable_t2_short) mk('s-chip-mode', 'T2 · ON');
    }
  } catch(e) {}

  mk('s-chip-regime', 'régime · ' + (cs.regime || '—'));
  // P11 fix : also display crypto_regime state(s) — the actual driver of strategy decisions.
  // Legacy chip above shows OLD VIX/SPY label, this one shows NEW crypto state.
  try {
    const cr = await api('/api/crypto-regime');
    if (cr && cr.by_symbol) {
      const states = Object.values(cr.by_symbol).map(s => (s.state || '?'));
      const uniq = [...new Set(states)];
      const label = uniq.length === 1 ? uniq[0] : uniq.join('/');
      mk('s-chip-regime', 'crypto · ' + label.replace(/_/g, ' '));
    }
  } catch(e) {}
  const biasLabel = ({
    longs_favored:  'biais · longs',
    shorts_favored: 'biais · shorts',
    both:           'biais · both',
    no_signals:     'biais · pause',
  })[cs.bias] || 'biais · —';
  mk('s-chip-bias', biasLabel);

  // Render the active strategies (T1L, T1S, T2) instead of dead lowvol/normal/trend modes
  const grid = $('modes-grid');
  grid.innerHTML = '';
  try {
    const tb = await api('/api/thesis/breakdown');
    if (tb && !tb.error) {
      const byThesis = tb.by_thesis || [];
      // Map: build summary card for each available strategy
      const available = (tb.available_strategies || []).map(s => s.id);
      available.forEach(stratId => {
        // Find the matching row in by_thesis
        let row = null;
        if (stratId === 'T1L') row = byThesis.find(r => r.thesis === 'T1' && r.side === 'long');
        if (stratId === 'T1S') row = byThesis.find(r => r.thesis === 'T1' && r.side === 'short');
        if (stratId === 'T2')  row = byThesis.find(r => r.thesis === 'T2');
        const m = row || {n_trades: 0, win_rate: 0, total_pnl: 0, expectancy: 0, pf: 0};
        grid.appendChild(_renderStrategyCard(stratId, m));
      });
    }
  } catch(e) { console.warn('thesis breakdown failed:', e); }
  _renderConfidence(data);
}

function _renderStrategyCard(stratId, m) {
  const card = document.createElement('div');
  card.className = 's-card';
  const n = m.n_trades || 0;
  const wr = ((m.win_rate || 0) * 100).toFixed(1);
  const pnl = (m.total_pnl || 0).toFixed(2);
  const pnlSign = (m.total_pnl || 0) >= 0 ? '+' : '';
  const exp = (m.expectancy || 0).toFixed(3);
  const pf = (m.pf || 0).toFixed(2);
  const title = {
    'T1L': 'T1 long zone bounce',
    'T1S': 'T1 short zone bounce',
    'T2':  'T2 short trend follow',
  }[stratId] || stratId;
  card.innerHTML = `
    <div class="s-card-head">
      <div class="s-card-title">${stratId}</div>
      <div class="s-card-sub">${title}</div>
    </div>
    <div class="s-card-stats">
      <div><span>N</span><strong>${n}</strong></div>
      <div><span>WR</span><strong>${wr}%</strong></div>
      <div><span>PF</span><strong>${pf}</strong></div>
      <div><span>P&L</span><strong class="${(m.total_pnl||0) >= 0 ? 'pos' : 'neg'}">${pnlSign}$${pnl}</strong></div>
      <div><span>exp/trade</span><strong>${exp}</strong></div>
    </div>
  `;
  return card;
}

// ─── LLM Cost Observability ───────────────────────────────────
let _llmWindow = 168;     // 7d default

function _llmFmt$(v) {
  if (v == null) return '—';
  if (v < 1) return '$' + v.toFixed(3);
  if (v < 100) return '$' + v.toFixed(2);
  return '$' + Math.round(v).toLocaleString();
}

function _llmCostClass(cost, low, high) {
  if (cost >= high) return 'crit';
  if (cost >= low)  return 'warn';
  return '';
}

async function loadLLMUsage() {
  const data = await api(`/api/llm/usage?hours=${_llmWindow}`);
  if (!data || data.error) {
    const k = $('llm-kpi-row');
    if (k) k.innerHTML = `<div class="llm-empty">endpoint error: ${data?.error || 'no data'}</div>`;
    return;
  }
  const total      = data.total      || {};
  const byBot      = data.by_bot     || [];
  const byModel    = data.by_model   || [];
  const byTask     = data.by_task_type || [];

  const totCost    = total.cost_usd || 0;
  const totCalls   = total.calls    || 0;
  const inTok      = total.in_tok   || 0;
  const cachedTok  = total.cached_tok || 0;
  const outTok     = total.out_tok || 0;
  const cacheRatio = (inTok + cachedTok) > 0
                     ? cachedTok / (inTok + cachedTok) * 100 : 0;
  const days       = _llmWindow / 24;
  const monthly    = days > 0 ? (totCost / days) * 30 : 0;

  // KPI tiles
  $('llm-kpi-row').innerHTML = `
    <div class="llm-kpi">
      <div class="lbl">Total cost</div>
      <div class="val ${_llmCostClass(totCost, 5, 25)}">${_llmFmt$(totCost)}</div>
      <div class="sub">over ${days.toFixed(1)}d</div>
    </div>
    <div class="llm-kpi">
      <div class="lbl">Calls</div>
      <div class="val">${totCalls.toLocaleString()}</div>
      <div class="sub">${days > 0 ? Math.round(totCalls/days) : 0}/day avg</div>
    </div>
    <div class="llm-kpi">
      <div class="lbl">Cache hit</div>
      <div class="val ${cacheRatio < 50 ? 'warn' : cacheRatio > 80 ? 'good' : ''}">${cacheRatio.toFixed(0)}%</div>
      <div class="sub">${(cachedTok/1000).toFixed(0)}k cached / ${(inTok/1000).toFixed(0)}k fresh</div>
    </div>
    <div class="llm-kpi">
      <div class="lbl">Est. monthly</div>
      <div class="val ${_llmCostClass(monthly, 20, 80)}">${_llmFmt$(monthly)}</div>
      <div class="sub">extrapolated</div>
    </div>`;

  // Breakdown panels
  const modelRowsHtml = byModel.slice(0, 6).map(r => {
    const klass = r.cost_usd >= 1 ? 'high' : r.cost_usd >= 0.1 ? 'mid' : '';
    return `<div class="llm-row">
      <span class="label">${r.model || '—'}</span>
      <span class="calls">${r.calls}</span>
      <span class="cost ${klass}">${_llmFmt$(r.cost_usd)}</span>
    </div>`;
  }).join('') || '<div class="llm-empty">no calls in window</div>';

  const botRowsHtml = byBot.map(r => `
    <div class="llm-row">
      <span class="label">${r.bot_id}</span>
      <span class="calls">${r.calls}</span>
      <span class="cost">${_llmFmt$(r.cost_usd)}</span>
    </div>`).join('') || '<div class="llm-empty">no calls</div>';

  const taskRowsHtml = byTask.map(r => `
    <div class="llm-row">
      <span class="label">${r.task_type || 'unspec.'}</span>
      <span class="calls">${r.calls}</span>
      <span class="cost">${_llmFmt$(r.cost_usd)}</span>
    </div>`).join('') || '<div class="llm-empty">no calls</div>';

  $('llm-breakdown').innerHTML = `
    <div class="llm-panel">
      <div class="llm-panel-title">By Model (top 6)</div>
      ${modelRowsHtml}
    </div>
    <div class="llm-panel">
      <div class="llm-panel-title">By Bot</div>
      ${botRowsHtml}
    </div>
    <div class="llm-panel">
      <div class="llm-panel-title">By Task Type</div>
      ${taskRowsHtml}
    </div>`;

  $('llm-footer').innerHTML = `
    <span>Tokens : ${(inTok/1000).toFixed(0)}k fresh + ${(cachedTok/1000).toFixed(0)}k cached → ${(outTok/1000).toFixed(0)}k out</span>
    <span>Cost model conservative — calibrate via OpenRouter dashboard</span>`;
}

let _llmTabsInited = false;
function _initLLMTabs() {
  if (_llmTabsInited) return;
  const tabs = document.querySelectorAll('.llm-tab');
  tabs.forEach(t => t.addEventListener('click', () => {
    tabs.forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    _llmWindow = parseInt(t.dataset.h);
    loadLLMUsage();
  }));
  _llmTabsInited = true;
}

// ─── Tab Navigation ──────────────────────────────────────────
const TAB_STORAGE_KEY = 'jimbot.activeTab';

function _activateTab(name) {
  const wrap = document.querySelector('.wrap');
  if (!wrap) return;
  wrap.setAttribute('data-active-tab', name);
  document.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.targetTab === name);
  });
  try { localStorage.setItem(TAB_STORAGE_KEY, name); } catch {}
  // Scroll to top of content area for smooth UX
  window.scrollTo({top: 0, behavior: 'smooth'});
}

let _tabsInited = false;
function _initTabs() {
  if (_tabsInited) return;
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => _activateTab(btn.dataset.targetTab));
  });
  // Restore saved tab if any
  let saved = null;
  try { saved = localStorage.getItem(TAB_STORAGE_KEY); } catch {}
  if (saved && ['overview','trades','strategy','cost'].includes(saved)) {
    _activateTab(saved);
  }
  _tabsInited = true;
}

function _updateTabBadges() {
  // Open positions badge on Trades tab — derived from current data
  const cntEl = document.getElementById('tab-count-trades');
  if (!cntEl) return;
  // Best-effort: count rows in trade history table
  const rows = document.querySelectorAll('#trades-tbody tr');
  if (rows && rows.length) cntEl.textContent = rows.length;
}

async function refresh() {
  _initTabs();
  _initLLMTabs();
  await Promise.all([
    loadHero(), loadKPIs(), loadEquity(), loadPositions(),
    loadRegime(), loadTrades(_period), loadDecisions(),
    loadExits(), loadAnalyses(), loadStrategy(), loadLLMUsage()
  ]);
  const fill = $('rfbar-fill');
  fill.style.transition = 'none';
  fill.style.width = '0%';
  requestAnimationFrame(() => {
    fill.style.transition = `width ${INTERVAL}ms linear`;
    fill.style.width = '100%';
  });
  _updateTabBadges();
}

refresh();
setInterval(refresh, INTERVAL);
window.addEventListener('resize', () => { if (_eqPts.length) drawEquity(_eqPts); });
</script>
</body>
</html>"""
