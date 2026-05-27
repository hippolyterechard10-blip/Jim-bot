#!/usr/bin/env python3
"""
analytics.py — Per-mode strategy analytics for Jim Bot.

Sépare les résultats par mode stratégie (lowvol / normal / trend) et par
broker_mode (paper / live). Calcule PnL net après modèle de coûts, plus
métriques de stabilité (sharpe-like, drawdown, profit factor).

Usage:
    python3 analytics.py                 # console report
    python3 analytics.py --json          # JSON pour dashboard
    python3 analytics.py --mode lowvol   # filtrer un mode
    python3 analytics.py --since DATE    # ISO date filter on entry_at
"""
from __future__ import annotations
import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "trading_memory.db"

# ─── Cost model ────────────────────────────────────────────────────────────────
# Per round-trip. Appliqué au notional (qty × entry_price).
# Référence : Kraken Futures taker 0.05 %, spread ETH/SOL perps ~1bp,
# slippage market exit ~0.05 %, funding ETH/SOL ~0.01 % par 8h en moyenne.
COST_MODEL = {
    "paper": {
        "fees_pct":          0.0,
        "slippage_exit_pct": 0.0,
        "funding_8h_pct":    0.0,
    },
    "live": {
        "fees_pct":          0.0010,   # 2 × 0.05 % taker
        "slippage_exit_pct": 0.0005,   # ~5 bps stop-out slippage
        "funding_8h_pct":    0.0001,   # ~1 bp par 8h capture
    },
}


def estimate_costs(qty: float, entry_price: float, duration_min: float | None,
                   broker_mode: str) -> float:
    """Estime le coût total round-trip pour un trade ($)."""
    if not qty or not entry_price:
        return 0.0
    notional = abs(qty) * float(entry_price)
    model = COST_MODEL.get(broker_mode, COST_MODEL["live"])
    cost = notional * (model["fees_pct"] + model["slippage_exit_pct"])
    if duration_min and duration_min > 0:
        funding_windows = int(duration_min // 480)
        cost += notional * model["funding_8h_pct"] * funding_windows
    return cost


# ─── DB loading ────────────────────────────────────────────────────────────────

def load_trades(db_path: Path, where_extra: str = "") -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    sql = f"""
        SELECT
            id, trade_id, symbol, side, qty, entry_price, exit_price,
            stop_loss, take_profit, status, pnl, close_reason,
            entry_at, exit_at,
            CASE WHEN exit_at IS NOT NULL
                 THEN (julianday(exit_at) - julianday(entry_at)) * 1440
                 ELSE NULL END AS duration_min,
            COALESCE(json_extract(market_context, '$.mode'), 'unknown') AS mode,
            COALESCE(json_extract(market_context, '$.broker_mode'), 'unknown') AS broker_mode,
            json_extract(market_context, '$.side') AS strat_side,
            json_extract(market_context, '$.target_pct_used') AS target_pct_used,
            json_extract(market_context, '$.strategy_source') AS source
        FROM trades
        WHERE json_extract(market_context, '$.strategy_source') = 'geo_v4'
        {where_extra}
        ORDER BY entry_at ASC
    """
    cur = conn.execute(sql)
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    return rows


# ─── Metrics ───────────────────────────────────────────────────────────────────

def _max_drawdown(net_pnls: list[float]) -> float:
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in net_pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return max_dd


def _stdev(values: list[float]) -> float:
    n = len(values)
    if n < 2: return 0.0
    m = sum(values) / n
    return math.sqrt(sum((v - m) ** 2 for v in values) / (n - 1))


def compute_metrics(trades: list[dict], live_equivalent: bool = False) -> dict:
    """Calcule toutes les métriques pour un groupe de trades.
    Si live_equivalent=True, applique les coûts live même aux trades paper
    (pour répondre à "what if it were live")."""
    closed = [t for t in trades if t["status"] == "closed" and t["pnl"] is not None]
    n = len(closed)
    open_n = sum(1 for t in trades if t["status"] == "open")

    if n == 0:
        return {"n": 0, "open": open_n}

    gross_pnls = [float(t["pnl"]) for t in closed]
    costs = []
    for t in closed:
        cost_broker_mode = "live" if live_equivalent else (t.get("broker_mode") or "live")
        costs.append(estimate_costs(t["qty"], t["entry_price"],
                                     t.get("duration_min") or 0, cost_broker_mode))
    net_pnls = [g - c for g, c in zip(gross_pnls, costs)]

    wins = [p for p in net_pnls if p > 0]
    losses = [p for p in net_pnls if p < 0]
    breakevens = [p for p in net_pnls if p == 0]

    win_rate = len(wins) / n
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    expectancy = sum(net_pnls) / n

    sum_wins = sum(wins)
    sum_losses_abs = abs(sum(losses))
    profit_factor = (sum_wins / sum_losses_abs) if sum_losses_abs > 0 else float("inf")

    std = _stdev(net_pnls)
    sharpe_per_trade = (expectancy / std) if std > 0 else 0.0

    max_dd = _max_drawdown(net_pnls)

    # Breakdown by close reason
    by_reason: dict[str, dict] = {}
    for t, np_val in zip(closed, net_pnls):
        r = t.get("close_reason") or "unknown"
        slot = by_reason.setdefault(r, {"n": 0, "pnl": 0.0})
        slot["n"] += 1
        slot["pnl"] += np_val
    for r, slot in by_reason.items():
        slot["pnl"] = round(slot["pnl"], 2)

    # Duration stats
    durations = [t["duration_min"] for t in closed if t.get("duration_min")]

    return {
        "n":               n,
        "open":            open_n,
        "wins":            len(wins),
        "losses":          len(losses),
        "breakevens":      len(breakevens),
        "gross_pnl":       round(sum(gross_pnls), 2),
        "est_costs":       round(sum(costs), 2),
        "net_pnl":         round(sum(net_pnls), 2),
        "win_rate":        round(win_rate, 3),
        "expectancy":      round(expectancy, 2),
        "avg_win":         round(avg_win, 2),
        "avg_loss":        round(avg_loss, 2),
        "profit_factor":   round(profit_factor, 2) if profit_factor != float("inf") else None,
        "sharpe_per_trade": round(sharpe_per_trade, 3),
        "max_drawdown":    round(max_dd, 2),
        "avg_dur_min":     round(sum(durations) / len(durations), 1) if durations else None,
        "by_reason":       by_reason,
    }


def group_by(trades: list[dict], key: str) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for t in trades:
        k = t.get(key) or "unknown"
        groups.setdefault(k, []).append(t)
    return groups


# ─── Reporting ─────────────────────────────────────────────────────────────────

def _fmt(v, w=10, dec=2, suffix=""):
    if v is None:    return "—".rjust(w)
    if v == float("inf"): return "inf".rjust(w)
    if isinstance(v, (int,)) and not isinstance(v, bool):
        return f"{v:>{w}d}{suffix}"
    try:
        return f"{v:>{w}.{dec}f}{suffix}"
    except Exception:
        return str(v).rjust(w)


def print_report(trades: list[dict]):
    overall = compute_metrics(trades)
    overall_live = compute_metrics(trades, live_equivalent=True)
    by_mode = {k: compute_metrics(v) for k, v in group_by(trades, "mode").items()}
    by_mode_live = {k: compute_metrics(v, live_equivalent=True)
                    for k, v in group_by(trades, "mode").items()}
    by_broker = {k: compute_metrics(v) for k, v in group_by(trades, "broker_mode").items()}

    print()
    print("═" * 90)
    print(f"  JIM BOT — Per-mode analytics  ({len(trades)} trades total)")
    print("═" * 90)

    # OVERALL
    print(f"\n  OVERALL")
    print(f"  {'─' * 86}")
    print(f"    n_closed:        {overall.get('n', 0)}  (open now: {overall.get('open', 0)})")
    print(f"    gross PnL:      {_fmt(overall.get('gross_pnl'), 9)}  $")
    print(f"    est. costs:     {_fmt(overall.get('est_costs'), 9)}  $   (broker-aware: paper=0)")
    print(f"    net PnL:        {_fmt(overall.get('net_pnl'), 9)}  $")
    if overall.get('n'):
        print(f"    if-live PnL:    {_fmt(overall_live.get('net_pnl'), 9)}  $   (applique coûts live à tout)")

    # BY MODE
    print(f"\n  BY MODE")
    print(f"  {'─' * 86}")
    header = f"    {'mode':<10} {'n':>4} {'gross':>9} {'costs':>9} {'net':>9} {'if-live':>9} {'WR':>6} {'expect':>8} {'PF':>6} {'sharpe':>7} {'maxDD':>8}"
    print(header)
    for mode, m in sorted(by_mode.items()):
        ml = by_mode_live.get(mode, {})
        if m.get("n", 0) == 0:
            print(f"    {mode:<10} {m.get('open', 0):>4} (open only)")
            continue
        pf = m["profit_factor"]
        pf_str = "inf" if pf is None else f"{pf:.2f}"
        print(f"    {mode:<10} {m['n']:>4d} {m['gross_pnl']:>9.2f} {m['est_costs']:>9.2f} "
              f"{m['net_pnl']:>9.2f} {ml.get('net_pnl', 0):>9.2f} "
              f"{m['win_rate']*100:>5.1f}% {m['expectancy']:>8.2f} "
              f"{pf_str:>6} "
              f"{m['sharpe_per_trade']:>7.3f} {m['max_drawdown']:>8.2f}")

    # BY BROKER
    print(f"\n  BY BROKER MODE")
    print(f"  {'─' * 86}")
    for bm, m in sorted(by_broker.items()):
        if m.get("n", 0) == 0:
            print(f"    {bm:<10} (no closed)")
            continue
        pf = m["profit_factor"]
        pf_str = "inf" if pf is None else f"{pf:.2f}"
        print(f"    {bm:<10} n={m['n']:>3}  net=${m['net_pnl']:>8.2f}  WR={m['win_rate']*100:>5.1f}%  expect=${m['expectancy']:>6.2f}  PF={pf_str}")

    # BY REASON breakdown (overall)
    print(f"\n  CLOSE REASONS (overall)")
    print(f"  {'─' * 86}")
    for r, slot in sorted((overall.get("by_reason") or {}).items()):
        print(f"    {r:<10} n={slot['n']:>3}  net=${slot['pnl']:>8.2f}")

    # Verdict
    print(f"\n  VERDICT (heuristique)")
    print(f"  {'─' * 86}")
    for mode, m in sorted(by_mode.items()):
        ml = by_mode_live.get(mode, {})
        if m.get("n", 0) == 0:
            print(f"    {mode:<10}  — no closed trades yet, can't judge")
            continue
        verdict = _verdict(m, ml)
        print(f"    {mode:<10}  {verdict}")
    print()


def _verdict(m: dict, ml: dict) -> str:
    n = m.get("n", 0)
    if n < 10:
        return f"too few trades (n={n}) — wait for more data"
    live_net = ml.get("net_pnl", 0) if ml else 0
    pf = m.get("profit_factor")
    wr = m.get("win_rate", 0)
    expect = m.get("expectancy", 0)

    if live_net > 0 and (pf is None or pf > 1.3) and wr > 0.5:
        return f"✅ real edge candidate (live-net=${live_net:+.0f}, PF={pf}, WR={wr*100:.0f}%)"
    if live_net > 0:
        return f"⚠️  marginal (live-net=${live_net:+.0f}) — need more data"
    if live_net < 0 and m.get("net_pnl", 0) > 0:
        return f"⚠️  paper-noise harvesting (paper +${m['net_pnl']:.0f} but live-net=${live_net:.0f})"
    if live_net < 0:
        return f"❌ negative expectancy (live-net=${live_net:.0f}) — useful for plumbing only"
    return "neutral"


# ─── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--mode", help="filter to mode (lowvol|normal|trend)")
    ap.add_argument("--broker", help="filter to broker_mode (paper|live)")
    ap.add_argument("--since", help="entry_at >= ISO date (e.g. 2026-05-27)")
    args = ap.parse_args()

    where_parts = []
    if args.mode:    where_parts.append(f"AND json_extract(market_context, '$.mode') = '{args.mode}'")
    if args.broker:  where_parts.append(f"AND json_extract(market_context, '$.broker_mode') = '{args.broker}'")
    if args.since:   where_parts.append(f"AND entry_at >= '{args.since}'")
    where_extra = " ".join(where_parts)

    trades = load_trades(DB_PATH, where_extra)

    if args.json:
        overall = compute_metrics(trades)
        overall_live = compute_metrics(trades, live_equivalent=True)
        by_mode = {k: compute_metrics(v) for k, v in group_by(trades, "mode").items()}
        by_mode_live = {k: compute_metrics(v, live_equivalent=True)
                        for k, v in group_by(trades, "mode").items()}
        by_broker = {k: compute_metrics(v) for k, v in group_by(trades, "broker_mode").items()}
        out = {
            "overall":      overall,
            "overall_live": overall_live,
            "by_mode":      by_mode,
            "by_mode_live": by_mode_live,
            "by_broker":    by_broker,
            "cost_model":   COST_MODEL,
            "n_trades":     len(trades),
        }
        print(json.dumps(out, indent=2, default=str))
    else:
        print_report(trades)


if __name__ == "__main__":
    main()
