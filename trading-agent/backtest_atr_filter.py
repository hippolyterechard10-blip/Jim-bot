"""
backtest_atr_filter.py — Test filtre ATR minimum
=================================================
Compare 3 configs sur 60j yfinance ETH+SOL pool partagé :
    - Sans filtre ATR (baseline)
    - ATR minimum 0.10%
    - ATR minimum 0.15%

Objectif : voir si le filtre améliore WR/PF sans trop réduire la fréquence.
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
    print("pip install yfinance --break-system-packages"); sys.exit(1)

CAPITAL     = 1000.0
POS_PCT     = 0.50
MAX_SIM     = 2
LOOKBACK    = 60
ZONE_PCT    = 0.003
MAX_TOUCHES = 2
RSI_LOW     = 20
RSI_HIGH    = 65
TARGET_PCT  = 0.009
SYMBOLS     = ["ETH-USD", "SOL-USD"]

ATR_CONFIGS = {
    "Sans filtre ATR": None,
    "ATR >= 0.10%":    0.0010,
    "ATR >= 0.15%":    0.0015,
}

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

def _zk(sym, center):
    mag = max(1, int(round(-np.log10(center * 0.001))))
    return f"{sym}_{round(center, mag)}"

def _atr_pct(bars_5m, current):
    highs = bars_5m["high"].values[-14:]
    lows  = bars_5m["low"].values[-14:]
    atr   = np.mean(highs - lows)
    return atr / current * 100

# ── DOWNLOAD ──────────────────────────────────────────────────────────────────

def download_all():
    end   = datetime.now(timezone.utc)
    # yfinance limite 15m et 5m aux 60 derniers jours → on utilise 58j max
    LIMITS = {"1h": LOOKBACK + 5, "15m": 58, "5m": 58}
    all_dfs = {}
    for sym in SYMBOLS:
        dfs = {}
        for key, interval in [("1h", "1h"), ("15m", "15m"), ("5m", "5m")]:
            start = end - timedelta(days=LIMITS[key])
            df = yf.download(sym, start=start, end=end, interval=interval,
                             auto_adjust=True, progress=False)
            if df.empty: return None
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df.index   = pd.to_datetime(df.index, utc=True)
            df.columns = [c.lower() for c in df.columns]
            dfs[key]   = df
        all_dfs[sym] = dfs
        print(f"  {sym}: {len(dfs['5m'])} bars 5min | 15m: {len(dfs['15m'])} | 1h: {len(dfs['1h'])}")
    return all_dfs

# ── SIGNAL ────────────────────────────────────────────────────────────────────

def get_signal(sym, t_now, dfs, atr_min):
    b1h  = dfs["1h"][dfs["1h"].index   <= t_now].tail(50)
    b15m = dfs["15m"][dfs["15m"].index <= t_now].tail(100)
    b5m  = dfs["5m"][dfs["5m"].index   <= t_now].tail(30)
    if len(b1h) < 10 or len(b15m) < 20 or len(b5m) < 10: return None

    h1 = b1h["high"].values; l1 = b1h["low"].values
    if h1[-1] < h1[-4] and l1[-1] < l1[-4]: return None

    cl5  = b5m["close"].values
    vo5  = b5m["volume"].values
    rsi  = _rsi(cl5, 14)
    curr = float(b5m["close"].iloc[-1])

    avgv = vo5[-20:].mean() if len(vo5) >= 20 else vo5.mean()
    if avgv > 0 and len(vo5) >= 2 and vo5[-2] < avgv * 0.3: return None

    # ── FILTRE ATR ────────────────────────────────────────────────────────────
    if atr_min is not None:
        atr_p = _atr_pct(b5m, curr)
        if atr_p < atr_min * 100:
            return None  # marché trop flat

    if not (RSI_LOW <= rsi <= RSI_HIGH): return None

    zones = _find_zones(b15m["high"].values, b15m["low"].values, cl5)
    for zone in zones:
        dist = (curr - zone["center"]) / curr
        if not (0.001 <= dist <= 0.020): continue
        div = _rsi_div(cl5, rsi)
        if not div and not (30 <= rsi <= 55): continue
        if not any(b5m["low"].values[-8:] <= zone["high"]): continue
        if cl5[-1] <= zone["low"]: continue
        stop   = _dyn_stop(b5m["low"].values, zone["center"], zone["wick_low"])
        target = round(zone["high"] * (1 + TARGET_PCT), 2)
        risk   = abs(zone["center"] - stop)
        reward = abs(target - zone["center"])
        if risk <= 0 or reward / risk < 1.2: continue
        return {"zone": zone, "stop": stop, "target": target}
    return None

# ── BACKTEST ──────────────────────────────────────────────────────────────────

def run(all_dfs, atr_min):
    ref_idx = None
    for sym in SYMBOLS:
        idx = set(all_dfs[sym]["5m"].index)
        ref_idx = idx if ref_idx is None else ref_idx & idx
    ref_idx = sorted(ref_idx)

    capital     = CAPITAL
    trades      = []
    open_pos    = {}
    touches     = defaultdict(int)
    skipped_flat = 0

    for i in range(55, len(ref_idx) - 1):
        t_now  = ref_idx[i]
        t_next = ref_idx[i + 1]

        for zk in list(open_pos.keys()):
            p   = open_pos[zk]
            sym = p["sym"]
            df5 = all_dfs[sym]["5m"]
            if t_next not in df5.index: continue
            nb  = df5.loc[t_next]
            hi  = float(nb["high"]); lo = float(nb["low"]); cl = float(nb["close"])
            sh  = lo <= p["stop"]; th = hi >= p["target"]; to = (i - p["bar"]) >= 48
            ep  = er = None
            if sh and th: ep, er = p["stop"],   "stop"
            elif sh:      ep, er = p["stop"],   "stop"
            elif th:      ep, er = p["target"], "target"
            elif to:      ep, er = cl,          "timeout"
            if ep:
                pnl = (ep - p["entry"]) * p["qty"]
                capital += pnl
                trades.append({
                    "sym": sym, "entry": p["entry"], "exit": ep,
                    "pnl": round(pnl, 4), "reason": er,
                })
                del open_pos[zk]

        if capital < 20: continue
        if len(open_pos) >= MAX_SIM: continue

        deploy = CAPITAL * POS_PCT

        for sym in SYMBOLS:
            if len(open_pos) >= MAX_SIM: break

            sig = get_signal(sym, t_now, all_dfs[sym], atr_min)
            if sig is None:
                if atr_min:
                    b5m_w = all_dfs[sym]["5m"]
                    b5m_w = b5m_w[b5m_w.index <= t_now].tail(30)
                    if len(b5m_w) >= 14:
                        curr  = float(b5m_w["close"].iloc[-1])
                        atr_p = _atr_pct(b5m_w, curr)
                        if atr_p < atr_min * 100:
                            skipped_flat += 1
                continue

            zone = sig["zone"]
            key  = _zk(sym, zone["center"])
            if touches[key] >= MAX_TOUCHES: continue
            if key in open_pos: continue

            df5 = all_dfs[sym]["5m"]
            if t_next not in df5.index: continue
            nb  = df5.loc[t_next]
            if float(nb["low"]) <= zone["high"]:
                fill = min(float(nb["open"]), zone["center"])
                qty  = deploy / fill
                touches[key] += 1
                open_pos[key] = {
                    "sym": sym, "entry": fill,
                    "stop": sig["stop"], "target": sig["target"],
                    "qty": qty, "bar": i,
                }

    for key, p in open_pos.items():
        sym  = p["sym"]
        last = float(all_dfs[sym]["5m"]["close"].iloc[-1])
        pnl  = (last - p["entry"]) * p["qty"]
        capital += pnl
        trades.append({
            "sym": sym, "entry": p["entry"], "exit": last,
            "pnl": round(pnl, 4), "reason": "end",
        })

    return trades, capital, skipped_flat

# ── STATS ─────────────────────────────────────────────────────────────────────

def stats(trades, capital):
    n = len(trades)
    if n == 0: return None
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total  = sum(t["pnl"] for t in trades)
    wr     = len(wins) / n * 100
    sum_l  = sum(t["pnl"] for t in losses)
    pf     = abs(sum(t["pnl"] for t in wins) / sum_l) if sum_l else 99.0
    ann    = (capital - CAPITAL) / CAPITAL / LOOKBACK * 365 * 100
    eq     = np.array([CAPITAL] + [CAPITAL + sum(t["pnl"] for t in trades[:k+1]) for k in range(n)])
    pk     = np.maximum.accumulate(eq)
    mdd    = ((eq - pk) / pk * 100).min()
    stops  = len([t for t in trades if t["reason"] == "stop"])
    tgts   = len([t for t in trades if t["reason"] == "target"])
    return {
        "n": n, "wr": round(wr, 1), "pf": round(pf, 2),
        "total": round(total, 2), "ann": round(ann, 1),
        "mdd": round(mdd, 1), "stops": stops, "targets": tgts,
    }

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "═"*70)
    print(f"  Backtest filtre ATR — ETH+SOL pool | {LOOKBACK}j | $1000 | pos_pct 50%")
    print("═"*70)

    print("\n  Téléchargement données...")
    all_dfs = download_all()
    if not all_dfs:
        print("  Données indisponibles"); return

    results = {}
    for label, atr_min in ATR_CONFIGS.items():
        trades, capital, skipped = run(all_dfs, atr_min)
        s = stats(trades, capital)
        if s:
            results[label] = (s, skipped)

    print(f"\n{'─'*70}")
    print(f"  {'Config':<22} {'N':>5} {'WR%':>6} {'PF':>5} "
          f"{'P&L$':>8} {'Ann%':>8} {'MDD%':>6} {'Stops':>6} {'Tgts':>5} {'Flat↓':>6}")
    print(f"  {'─'*22} {'─'*5} {'─'*6} {'─'*5} "
          f"{'─'*8} {'─'*8} {'─'*6} {'─'*6} {'─'*5} {'─'*6}")

    for label, (s, skipped) in results.items():
        print(f"  {label:<22} {s['n']:>5} {s['wr']:>6.1f} {s['pf']:>5.2f} "
              f"{s['total']:>+8.2f} {s['ann']:>+7.1f}% {s['mdd']:>+5.1f}% "
              f"{s['stops']:>6} {s['targets']:>5} {skipped:>6}")

    # Analyse comparative
    print(f"\n  ANALYSE vs baseline (Sans filtre ATR)")
    print(f"  {'─'*60}")
    base_s = results.get("Sans filtre ATR", (None, 0))[0]
    for label, (s, skipped) in results.items():
        if label == "Sans filtre ATR": continue
        if not base_s: continue
        diff_wr  = s["wr"]  - base_s["wr"]
        diff_pf  = s["pf"]  - base_s["pf"]
        diff_n   = s["n"]   - base_s["n"]
        diff_ann = s["ann"] - base_s["ann"]
        verdict  = "✓ MIEUX" if diff_wr > 1 and diff_pf > 0.1 else ("~ NEUTRE" if diff_pf > 0 else "✗ PIRE")
        print(f"\n  [{verdict}] {label}")
        print(f"    Trades  : {base_s['n']} → {s['n']} ({diff_n:+d})")
        print(f"    WR%     : {base_s['wr']:.1f}% → {s['wr']:.1f}% ({diff_wr:+.1f}pp)")
        print(f"    PF      : {base_s['pf']:.2f} → {s['pf']:.2f} ({diff_pf:+.2f})")
        print(f"    Ann%    : {base_s['ann']:.1f}% → {s['ann']:.1f}% ({diff_ann:+.1f}pp)")
        print(f"    Trades flat filtrés : {skipped}")

    print("\n" + "═"*70 + "\n")

if __name__ == "__main__":
    main()
