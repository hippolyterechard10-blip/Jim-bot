"""
backtest_pos_pct.py — Comparaison pos_pct 28% vs 50% vs 100%
=============================================================
Même strat Geo V4, même données yfinance 60j ETH
Seul paramètre qui change : pos_pct
"""
import warnings
warnings.filterwarnings("ignore")
import sys
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone
from collections import defaultdict

try:
    import yfinance as yf
except ImportError:
    print("pip install yfinance --break-system-packages")
    sys.exit(1)

# ── CONFIG ────────────────────────────────────────────────────────────────────
SYMBOL           = "ETH-USD"
CAPITAL          = 1000.0
LOOKBACK         = 60
ZONE_PCT         = 0.003
MAX_TOUCHES      = 2
RSI_LOW          = 20
RSI_HIGH         = 65
MAX_SIM          = 2
TARGET_PCT       = 0.009

POS_PCTS_TO_TEST = [0.28, 0.50, 1.0]

# ── HELPERS ───────────────────────────────────────────────────────────────────
def _rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    d  = np.diff(np.array(closes, dtype=float))
    g  = np.where(d > 0, d, 0.0)
    l  = np.where(d < 0, -d, 0.0)
    ag = g[-period:].mean(); al = l[-period:].mean()
    if al == 0: return 100.0
    return round(100 - 100 / (1 + ag / al), 2)

def _find_zones(highs, lows, closes):
    current = closes[-1]
    sw = []
    for i in range(2, len(highs) - 2):
        if (lows[i] < lows[i-1] and lows[i] < lows[i-2]
                and lows[i] < lows[i+1] and lows[i] < lows[i+2]):
            sw.append((lows[i], highs[i]))
    if not sw: return []
    sw.sort(key=lambda x: x[0])
    clusters = [[sw[0]]]
    for v in sw[1:]:
        if (v[0] - clusters[-1][0][0]) / clusters[-1][0][0] < ZONE_PCT * 2:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    zones = []
    for c in clusters:
        center   = sum(x[0] for x in c) / len(c)
        wick_low = min(x[0] for x in c)
        if center < current * 0.999:
            zones.append({
                "center":   center,
                "high":     center * (1 + ZONE_PCT),
                "low":      center * (1 - ZONE_PCT),
                "wick_low": wick_low,
                "tests":    len(c),
            })
    zones.sort(key=lambda x: x["center"], reverse=True)
    return zones

def _rsi_div(closes, rsi_now):
    if len(closes) < 5: return False
    rsi_prev = _rsi(np.array(closes[:-3]), 14)
    return closes[-1] < closes[-4] and rsi_now > rsi_prev

def _dyn_stop(lows, entry, wick_low):
    floor = entry * 0.992
    c = min(lows[-8:]) * 0.999 if len(lows) >= 8 else wick_low * 0.999
    if floor <= c < entry: return c
    z = wick_low * 0.999
    if floor <= z < entry: return z
    return entry * 0.997

def _zone_key(center):
    mag = max(1, int(round(-np.log10(center * 0.001))))
    return round(center, mag)

# ── BACKTEST ──────────────────────────────────────────────────────────────────
def run_backtest(pos_pct):
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=LOOKBACK + 5)

    # yfinance limite 5m/15m aux 60 derniers jours — on adapte le start
    start_intraday = end - timedelta(days=58)
    df_1h  = yf.download("ETH-USD", start=start,          end=end, interval="1h",  auto_adjust=True, progress=False)
    df_15m = yf.download("ETH-USD", start=start_intraday, end=end, interval="15m", auto_adjust=True, progress=False)
    df_5m  = yf.download("ETH-USD", start=start_intraday, end=end, interval="5m",  auto_adjust=True, progress=False)

    for df in [df_1h, df_15m, df_5m]:
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.index   = pd.to_datetime(df.index, utc=True)
        df.columns = [c.lower() for c in df.columns]

    capital  = CAPITAL
    trades   = []
    open_pos = {}
    touches  = defaultdict(int)

    for i in range(55, len(df_5m) - 1):
        t_now  = df_5m.index[i]
        bar    = df_5m.iloc[i]
        next_b = df_5m.iloc[i + 1]

        # Gestion positions ouvertes
        for zk in list(open_pos.keys()):
            p  = open_pos[zk]
            hi = float(next_b["high"]); lo = float(next_b["low"]); cl = float(next_b["close"])
            sh = lo <= p["stop"]; th = hi >= p["target"]; to = (i - p["bar"]) >= 48
            ep = er = None
            if sh and th: ep, er = p["stop"],    "stop"
            elif sh:      ep, er = p["stop"],    "stop"
            elif th:      ep, er = p["target"],  "target"
            elif to:      ep, er = cl,           "timeout"
            if ep:
                pnl = (ep - p["entry"]) * p["qty"]
                capital += pnl
                trades.append({"entry": p["entry"], "exit": ep, "pnl": round(pnl, 4), "reason": er})
                del open_pos[zk]

        if capital < 20: continue
        if len(open_pos) >= MAX_SIM: continue

        bars_1h_p  = df_1h[df_1h.index <= t_now].tail(50)
        bars_15m_p = df_15m[df_15m.index <= t_now].tail(100)
        bars_5m_p  = df_5m[df_5m.index <= t_now].tail(30)
        if len(bars_1h_p) < 10 or len(bars_15m_p) < 20 or len(bars_5m_p) < 10:
            continue

        # Bias 1h
        h1h = bars_1h_p["high"].values; l1h = bars_1h_p["low"].values
        if h1h[-1] < h1h[-4] and l1h[-1] < l1h[-4]: continue

        current   = float(bar["close"])
        closes_5m = bars_5m_p["close"].values
        vols_5m   = bars_5m_p["volume"].values
        rsi_now   = _rsi(closes_5m, 14)
        avg_vol   = vols_5m[-20:].mean() if len(vols_5m) >= 20 else vols_5m.mean()
        if avg_vol > 0 and vols_5m[-1] < avg_vol * 0.3: continue

        zones = _find_zones(
            bars_15m_p["high"].values,
            bars_15m_p["low"].values,
            bars_15m_p["close"].values,
        )

        for zone in zones:
            zk   = _zone_key(zone["center"])
            dist = (current - zone["center"]) / current
            if not (0.001 <= dist <= 0.020): continue
            if touches[zk] >= MAX_TOUCHES: continue
            if zk in open_pos: continue
            if not (RSI_LOW <= rsi_now <= RSI_HIGH): continue
            div = _rsi_div(closes_5m, rsi_now)
            if not div and not (30 <= rsi_now <= 55): continue
            touched      = any(bars_5m_p["low"].values[-8:] <= zone["high"])
            closed_above = closes_5m[-1] > zone["low"]
            if not (touched and closed_above): continue

            stop   = _dyn_stop(bars_5m_p["low"].values, zone["center"], zone["wick_low"])
            target = round(zone["high"] * (1 + TARGET_PCT), 2)
            risk   = abs(zone["center"] - stop)
            reward = abs(target - zone["center"])
            if risk <= 0 or reward / risk < 1.2: continue

            # Paramètre clé : pos_pct
            max_per_trade = CAPITAL * pos_pct
            available     = capital
            deploy        = min(max_per_trade, available / max(1, MAX_SIM - len(open_pos)))
            qty           = deploy / zone["center"]

            if float(next_b["low"]) <= zone["high"]:
                fill = min(float(next_b["open"]), zone["center"])
                touches[zk] += 1
                open_pos[zk] = {
                    "entry": fill, "stop": stop, "target": target,
                    "qty": qty, "bar": i + 1,
                }

    # Fermer positions restantes au dernier prix
    if open_pos:
        last = float(df_5m["close"].iloc[-1])
        for zk, p in open_pos.items():
            pnl = (last - p["entry"]) * p["qty"]
            capital += pnl
            trades.append({"entry": p["entry"], "exit": last, "pnl": round(pnl, 4), "reason": "open"})

    return trades, capital

# ── STATS ─────────────────────────────────────────────────────────────────────
def stats(trades, capital):
    n = len(trades)
    if n == 0: return None
    wins  = [t for t in trades if t["pnl"] > 0]
    losses= [t for t in trades if t["pnl"] <= 0]
    total = sum(t["pnl"] for t in trades)
    wr    = len(wins) / n * 100
    sum_l = sum(t["pnl"] for t in losses)
    pf    = abs(sum(t["pnl"] for t in wins) / sum_l) if sum_l else 99.0
    ann   = (capital - CAPITAL) / CAPITAL / LOOKBACK * 365 * 100
    eq    = np.array([CAPITAL] + [CAPITAL + sum(t["pnl"] for t in trades[:k+1]) for k in range(n)])
    pk    = np.maximum.accumulate(eq)
    mdd   = ((eq - pk) / pk * 100).min()

    # Breakdown par raison
    by_reason = {}
    for t in trades:
        r = t.get("reason", "?")
        by_reason.setdefault(r, {"n": 0, "pnl": 0})
        by_reason[r]["n"]   += 1
        by_reason[r]["pnl"] += t["pnl"]

    return {
        "n": n, "wr": round(wr, 1), "pf": round(pf, 2),
        "total": round(total, 2), "ann": round(ann, 1), "mdd": round(mdd, 1),
        "by_reason": by_reason,
    }

# ── MAIN ──────────────────────────────────────────────────────────────────────
def run():
    print("\n" + "═"*72)
    print(f"  Test pos_pct — ETH/USD | {LOOKBACK}j | Capital ${CAPITAL:.0f} | TARGET +{TARGET_PCT*100:.1f}%")
    print("═"*72)
    print(f"  {'pos_pct':>8}  {'N':>5}  {'WR%':>6}  {'PF':>5}  {'P&L$':>8}  {'Ann%':>8}  {'MDD%':>6}  {'$/trade':>9}")
    print(f"  {'─'*8}  {'─'*5}  {'─'*6}  {'─'*5}  {'─'*8}  {'─'*8}  {'─'*6}  {'─'*9}")

    results = {}
    for pct in POS_PCTS_TO_TEST:
        trades, cap = run_backtest(pct)
        s = stats(trades, cap)
        if s:
            avg = s["total"] / s["n"] if s["n"] > 0 else 0
            flag = " ✓" if s["ann"] > 0 else "  "
            print(
                f"  {pct*100:>7.0f}%  {s['n']:>5}  {s['wr']:>6.1f}  {s['pf']:>5.2f}"
                f"  {s['total']:>+8.2f}  {s['ann']:>+7.1f}%  {s['mdd']:>+5.1f}%"
                f"  {avg:>+9.4f}{flag}"
            )
            results[pct] = s
        else:
            print(f"  {pct*100:>7.0f}%  — aucun trade")

    # Détail exit breakdown
    print(f"\n  {'─'*72}")
    print("  EXIT BREAKDOWN")
    for pct in POS_PCTS_TO_TEST:
        s = results.get(pct)
        if not s: continue
        br = s.get("by_reason", {})
        total_n = s["n"]
        parts = []
        for reason in ["target", "stop", "timeout", "open"]:
            d = br.get(reason, {"n": 0, "pnl": 0})
            if d["n"] > 0:
                parts.append(f"{reason}: {d['n']} ({d['n']/total_n*100:.0f}%)")
        print(f"  {pct*100:>3.0f}%  →  {' | '.join(parts)}")

    # Analyse comparative
    print(f"\n  {'─'*72}")
    print("  ANALYSE COMPARATIVE")
    if 0.28 in results and 0.50 in results:
        r28 = results[0.28]; r50 = results[0.50]
        print(f"  50% vs 28% : P&L {r50['total']-r28['total']:+.2f}$"
              f" | WR {r28['wr']:.1f}% → {r50['wr']:.1f}%"
              f" | MDD {r28['mdd']:.1f}% → {r50['mdd']:.1f}%")
        if r50["wr"] >= r28["wr"] - 1.0:
            print("  → pos_pct 50% recommandé — même WR, gains ~x1.8")
        else:
            print(f"  → WR chute de {r28['wr']-r50['wr']:.1f}% à 50% — vérifier si acceptable")

    if 0.50 in results and 1.0 in results:
        r50 = results[0.50]; r100 = results[1.0]
        print(f"  100% vs 50% : P&L {r100['total']-r50['total']:+.2f}$"
              f" | WR {r50['wr']:.1f}% → {r100['wr']:.1f}%"
              f" | MDD {r50['mdd']:.1f}% → {r100['mdd']:.1f}%")

    print("═"*72 + "\n")

if __name__ == "__main__":
    run()
