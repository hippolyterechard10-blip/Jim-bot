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
    import requests
except ImportError:
    print("pip install requests --break-system-packages"); sys.exit(1)

CAPITAL     = 1000.0
POS_PCT     = 0.50
MAX_SIM     = 2
LOOKBACK    = 60
ZONE_PCT    = 0.003
MAX_TOUCHES = 2
RSI_LOW     = 20
RSI_HIGH    = 65
TARGET_PCT  = 0.009
SYMBOLS_BN  = {"ETH-USD": "ETHUSD", "SOL-USD": "SOLUSD"}
SYMBOLS     = list(SYMBOLS_BN.keys())
BASE        = "https://api.binance.us/api/v3/klines"
CACHE_DIR   = "binance_us_cache"

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

# ── DOWNLOAD BINANCE US ────────────────────────────────────────────────────────

def _ms(dt):
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)

def _dl_binance(symbol, days):
    import time, os
    os.makedirs(CACHE_DIR, exist_ok=True)
    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)
    tag      = f"{symbol}_5m_atr_{start_dt.strftime('%Y%m%d')}_{end_dt.strftime('%Y%m%d')}"
    cache    = os.path.join(CACHE_DIR, f"{tag}.parquet")

    if os.path.exists(cache):
        df = pd.read_parquet(cache)
        print(f"  [cache] {symbol}: {len(df):,} bars 5m")
        return df

    print(f"  [dl] {symbol}: {start_dt.date()} → {end_dt.date()} ...")
    start_ms = _ms(start_dt); end_ms = _ms(end_dt)
    rows = []
    while start_ms < end_ms:
        try:
            r = requests.get(BASE, params={
                "symbol": symbol, "interval": "5m",
                "startTime": start_ms, "limit": 1000,
            }, timeout=15)
            data = r.json()
        except Exception as e:
            print(f"  retry: {e}"); time.sleep(3); continue
        if not data or isinstance(data, dict): break
        rows.extend(data)
        start_ms = data[-1][0] + 300_000
        time.sleep(0.05)

    df = pd.DataFrame(rows, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","qv","trades","tbv","tqv","ignore"
    ])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df.set_index("open_time", inplace=True)
    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype(float)
    df = df[["open","high","low","close","volume"]]
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.to_parquet(cache)
    print(f"  → {len(df):,} bars ({df.index[0].date()} → {df.index[-1].date()})")
    return df

def _resample(df, rule):
    return df.resample(rule).agg(
        {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
    ).dropna()

def download_all():
    all_dfs = {}
    for sym, bn_sym in SYMBOLS_BN.items():
        df5  = _dl_binance(bn_sym, LOOKBACK + 2)
        if df5 is None or df5.empty: return None
        all_dfs[sym] = {
            "5m":  df5,
            "15m": _resample(df5, "15min"),
            "1h":  _resample(df5, "1h"),
        }
        print(f"  {sym}: {len(df5):,} bars 5m | 15m:{len(all_dfs[sym]['15m'])} | 1h:{len(all_dfs[sym]['1h'])}")
    return all_dfs

# ── SIGNAL (numpy vectorisé) ──────────────────────────────────────────────────

def get_signal_fast(h5, l5, c5, v5, h15, l15, c15, h1h, l1h, atr_min):
    n5 = len(c5); n15 = len(c15); n1h = len(h1h)
    if n5 < 30 or n15 < 20 or n1h < 10: return None, False

    if h1h[-1] < h1h[-4] and l1h[-1] < l1h[-4]: return None, False

    cl5 = c5[-30:]; vo5 = v5[-30:]
    rsi = _rsi(cl5, 14)
    curr = float(cl5[-1])

    avgv = vo5[:-1][-20:].mean() if len(vo5) > 1 else 1.0
    if avgv > 0 and vo5[-2] < avgv * 0.3: return None, False

    # Filtre ATR
    is_flat = False
    if atr_min is not None:
        atr_p = np.mean(h5[-14:] - l5[-14:]) / curr * 100
        if atr_p < atr_min * 100:
            return None, True  # flat

    if not (RSI_LOW <= rsi <= RSI_HIGH): return None, False

    zones = _find_zones(h15[-100:], l15[-100:], c15[-100:])
    for zone in zones:
        dist = (curr - zone["center"]) / curr
        if not (0.001 <= dist <= 0.020): continue
        div = _rsi_div(cl5, rsi)
        if not div and not (30 <= rsi <= 55): continue
        if not any(l5[-8:] <= zone["high"]): continue
        if cl5[-1] <= zone["low"]: continue
        stop   = _dyn_stop(l5[-8:], zone["center"], zone["wick_low"])
        target = round(zone["high"] * (1 + TARGET_PCT), 2)
        risk   = abs(zone["center"] - stop)
        reward = abs(target - zone["center"])
        if risk <= 0 or reward / risk < 1.2: continue
        return {"zone": zone, "stop": stop, "target": target}, False
    return None, False

# ── BACKTEST (numpy rapide) ────────────────────────────────────────────────────

def run(all_dfs, atr_min):
    import bisect
    arrays = {}
    for sym in SYMBOLS:
        df5  = all_dfs[sym]["5m"]
        df15 = all_dfs[sym]["15m"]
        df1h = all_dfs[sym]["1h"]
        arrays[sym] = {
            "idx5":  df5.index,  "idx15": df15.index, "idx1h": df1h.index,
            "h5":  df5["high"].values,  "l5":  df5["low"].values,
            "c5":  df5["close"].values, "v5":  df5["volume"].values,
            "o5":  df5["open"].values,
            "h15": df15["high"].values, "l15": df15["low"].values, "c15": df15["close"].values,
            "h1h": df1h["high"].values, "l1h": df1h["low"].values,
        }

    ref_idx = sorted(set(arrays[SYMBOLS[0]]["idx5"]).intersection(
        *[set(arrays[s]["idx5"]) for s in SYMBOLS[1:]]
    ))
    n = len(ref_idx)

    pos_maps = {sym: {ts: i for i, ts in enumerate(arrays[sym]["idx5"])} for sym in SYMBOLS}

    def tf_pos(idx_list, ts):
        return max(0, bisect.bisect_right(idx_list, ts) - 1)

    capital     = CAPITAL
    trades      = []
    open_pos    = {}
    touches     = defaultdict(int)
    skipped_flat = 0

    for i in range(55, n - 1):
        t_now  = ref_idx[i]
        t_next = ref_idx[i + 1]

        for zk in list(open_pos.keys()):
            p   = open_pos[zk]
            sym = p["sym"]
            pm  = pos_maps[sym]
            if t_next not in pm: continue
            ni  = pm[t_next]
            arr = arrays[sym]
            hi = arr["h5"][ni]; lo = arr["l5"][ni]; cl = arr["c5"][ni]
            sh = lo <= p["stop"]; th = hi >= p["target"]; to = (i - p["bar"]) >= 48
            ep = er = None
            if sh and th: ep, er = p["stop"],   "stop"
            elif sh:      ep, er = p["stop"],   "stop"
            elif th:      ep, er = p["target"], "target"
            elif to:      ep, er = cl,          "timeout"
            if ep:
                pnl = (ep - p["entry"]) * p["qty"]
                capital += pnl
                trades.append({"sym": sym, "entry": p["entry"], "exit": ep,
                               "pnl": round(pnl, 4), "reason": er})
                del open_pos[zk]

        if capital < 20 or len(open_pos) >= MAX_SIM: continue

        for sym in SYMBOLS:
            if len(open_pos) >= MAX_SIM: break
            arr = arrays[sym]
            ci5 = pos_maps[sym].get(t_now)
            if ci5 is None: continue
            ci15 = tf_pos(arr["idx15"], t_now)
            ci1h = tf_pos(arr["idx1h"], t_now)

            sig, is_flat = get_signal_fast(
                arr["h5"][:ci5+1],  arr["l5"][:ci5+1],
                arr["c5"][:ci5+1],  arr["v5"][:ci5+1],
                arr["h15"][:ci15+1], arr["l15"][:ci15+1], arr["c15"][:ci15+1],
                arr["h1h"][:ci1h+1], arr["l1h"][:ci1h+1],
                atr_min,
            )
            if is_flat: skipped_flat += 1
            if sig is None: continue

            zone = sig["zone"]
            key  = _zk(sym, zone["center"])
            if touches[key] >= MAX_TOUCHES or key in open_pos: continue

            pm = pos_maps[sym]
            if t_next not in pm: continue
            ni = pm[t_next]
            if arr["l5"][ni] <= zone["high"]:
                fill = min(arr["o5"][ni], zone["center"])
                qty  = (CAPITAL * POS_PCT) / fill
                touches[key] += 1
                open_pos[key] = {"sym": sym, "entry": fill,
                                 "stop": sig["stop"], "target": sig["target"],
                                 "qty": qty, "bar": i}

    for key, p in open_pos.items():
        sym  = p["sym"]
        last = float(arrays[sym]["c5"][-1])
        pnl  = (last - p["entry"]) * p["qty"]
        capital += pnl
        trades.append({"sym": sym, "entry": p["entry"], "exit": last,
                       "pnl": round(pnl, 4), "reason": "end"})

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
