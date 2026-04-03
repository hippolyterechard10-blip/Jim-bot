"""
backtest_atr_final.py — Test filtre ATR avec fill rate 65% réaliste
====================================================================
Périodes : 2022 (bear brutal) + 2023 (recovery) — Binance US 5min
Fill rate : 65% appliqué (simulation réaliste)
Configs :
    1. Baseline — sans filtre ATR
    2. ATR ≥ 0.10% — toujours actif
    3. ATR conditionnel — actif si régime choppy/bear, inactif si bull

Objectif : voir si le filtre améliore le P&L TOTAL sur marchés bear/recovery.
"""
import warnings; warnings.filterwarnings("ignore")
import sys, time, os, bisect, random
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
from collections import defaultdict

try:
    import requests
except ImportError:
    print("pip install requests --break-system-packages"); sys.exit(1)

# ── CONFIG ────────────────────────────────────────────────────────────────────
PERIODS = {
    "2022 Bear":     ("2021-10-01", "2022-12-31"),   # +90j warmup
    "2023 Recovery": ("2022-10-01", "2023-12-31"),   # +90j warmup
}
TEST_START = {
    "2022 Bear":     "2022-01-01",
    "2023 Recovery": "2023-01-01",
}

SYMBOLS   = {"ETH": "ETHUSD", "SOL": "SOLUSD"}
CAPITAL   = 1000.0
POS_PCT   = 0.50
MAX_SIM   = 2
ZONE_PCT  = 0.003
MAX_TOUCHES = 2
RSI_LOW   = 20
RSI_HIGH  = 65
TARGET_PCT = 0.009
FILL_RATE = 0.65
CACHE_DIR = "binance_us_cache"
BASE      = "https://api.binance.us/api/v3/klines"

ATR_CONFIGS = {
    "Baseline (sans ATR)": {"atr_min": None,   "conditional": False},
    "ATR ≥ 0.10% fixe":   {"atr_min": 0.0010, "conditional": False},
    "ATR conditionnel":    {"atr_min": 0.0010, "conditional": True},
}

# ── DOWNLOAD ──────────────────────────────────────────────────────────────────
def _ms(s):
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)

def download(symbol, start, end):
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache = os.path.join(CACHE_DIR, f"{symbol}_5m_{start}_{end}.parquet")
    if os.path.exists(cache):
        df = pd.read_parquet(cache)
        print(f"  [cache] {symbol} {start[:4]}: {len(df):,} bars")
        return df
    print(f"  [dl] {symbol} {start} → {end} ...")
    s_ms, e_ms, rows = _ms(start), _ms(end), []
    while s_ms < e_ms:
        try:
            r = requests.get(BASE, params={
                "symbol": symbol, "interval": "5m",
                "startTime": s_ms, "limit": 1000,
            }, timeout=15)
            data = r.json()
        except Exception as e:
            print(f"  retry: {e}"); time.sleep(3); continue
        if not data or isinstance(data, dict): break
        rows.extend(data)
        s_ms = data[-1][0] + 300_000
        time.sleep(0.06)
        if len(rows) % 50000 == 0:
            d = datetime.fromtimestamp(rows[-1][0]/1000, tz=timezone.utc).strftime("%Y-%m")
            print(f"    ... {len(rows):,} bars → {d}")
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

def resample(df, rule):
    return df.resample(rule).agg(
        {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
    ).dropna()

# ── HELPERS ───────────────────────────────────────────────────────────────────
def _rsi(c, p=14):
    if len(c) < p+1: return 50.0
    d = np.diff(np.array(c, dtype=float))
    g = np.where(d > 0, d, 0.); l = np.where(d < 0, -d, 0.)
    ag = g[-p:].mean(); al = l[-p:].mean()
    return 100.0 if al == 0 else round(100 - 100 / (1 + ag / al), 2)

def _zones(highs, lows, closes):
    curr = closes[-1]; sw = []
    for i in range(2, len(highs) - 2):
        if (lows[i] < lows[i-1] and lows[i] < lows[i-2]
                and lows[i] < lows[i+1] and lows[i] < lows[i+2]):
            sw.append((lows[i], highs[i]))
    if not sw: return []
    sw.sort(key=lambda x: x[0])
    cl = [[sw[0]]]
    for v in sw[1:]:
        if (v[0] - cl[-1][0][0]) / cl[-1][0][0] < ZONE_PCT * 2:
            cl[-1].append(v)
        else:
            cl.append([v])
    res = []
    for c in cl:
        center = sum(x[0] for x in c) / len(c)
        wk     = min(x[0] for x in c)
        if center < curr * 0.999:
            res.append({"center": center, "high": center*(1+ZONE_PCT),
                        "low": center*(1-ZONE_PCT), "wick_low": wk})
    return sorted(res, key=lambda x: x["center"], reverse=True)

def _div(closes, rsi):
    if len(closes) < 5: return False
    return closes[-1] < closes[-4] and rsi > _rsi(np.array(closes[:-3]), 14)

def _stop(lows, entry, wk):
    floor = entry * 0.992
    c = min(lows[-8:]) * 0.999 if len(lows) >= 8 else wk * 0.999
    if floor <= c < entry: return c
    z = wk * 0.999
    if floor <= z < entry: return z
    return entry * 0.997

def _zk(sym, center):
    mag = max(1, int(round(-np.log10(center * 0.001))))
    return f"{sym}_{round(center, mag)}"

def _regime(h1h, l1h):
    if len(h1h) < 10: return "unknown"
    hh = h1h[-1] > h1h[-5]; hl = l1h[-1] > l1h[-5]
    lh = h1h[-1] < h1h[-5]; ll = l1h[-1] < l1h[-5]
    if hh and hl: return "bull"
    if lh and ll: return "bear"
    return "choppy"

# ── SIGNAL numpy rapide ───────────────────────────────────────────────────────
def get_signal(h5, l5, c5, v5, h15, l15, c15, h1h, l1h, atr_cfg):
    if len(c5) < 30 or len(c15) < 20 or len(h1h) < 10: return None
    if h1h[-1] < h1h[-4] and l1h[-1] < l1h[-4]: return None

    cl5 = c5[-30:]; vo5 = v5[-30:]
    rsi = _rsi(cl5, 14)
    curr = float(cl5[-1])

    avgv = vo5[:-1][-20:].mean() if len(vo5) > 1 else 1.0
    if avgv > 0 and vo5[-2] < avgv * 0.3: return None

    atr_min = atr_cfg["atr_min"]
    if atr_min is not None:
        apply = True
        if atr_cfg["conditional"]:
            apply = _regime(h1h, l1h) in ("choppy", "bear", "unknown")
        if apply:
            atr_p = np.mean(h5[-14:] - l5[-14:]) / curr * 100
            if atr_p < atr_min * 100: return None

    if not (RSI_LOW <= rsi <= RSI_HIGH): return None

    zones = _zones(h15[-100:], l15[-100:], c15[-100:])
    for zone in zones:
        dist = (curr - zone["center"]) / curr
        if not (0.001 <= dist <= 0.020): continue
        div = _div(cl5, rsi)
        if not div and not (30 <= rsi <= 55): continue
        if not any(l5[-8:] <= zone["high"]): continue
        if cl5[-1] <= zone["low"]: continue
        stop   = _stop(l5[-8:], zone["center"], zone["wick_low"])
        target = round(zone["high"] * (1 + TARGET_PCT), 2)
        risk   = abs(zone["center"] - stop)
        reward = abs(target - zone["center"])
        if risk <= 0 or reward / risk < 1.2: continue
        return {"zone": zone, "stop": stop, "target": target}
    return None

# ── BACKTEST numpy ────────────────────────────────────────────────────────────
def run(period_data, atr_cfg, test_start_ts, rng):
    syms = list(SYMBOLS.keys())
    arrays = {}
    for sym in syms:
        df5  = period_data[sym]["5m"]
        df15 = period_data[sym]["15m"]
        df1h = period_data[sym]["1h"]
        arrays[sym] = {
            "idx5":  df5.index,  "idx15": df15.index, "idx1h": df1h.index,
            "h5":  df5["high"].values,  "l5":  df5["low"].values,
            "c5":  df5["close"].values, "v5":  df5["volume"].values,
            "o5":  df5["open"].values,
            "h15": df15["high"].values, "l15": df15["low"].values, "c15": df15["close"].values,
            "h1h": df1h["high"].values, "l1h": df1h["low"].values,
        }

    ref_idx = sorted(set(arrays[syms[0]]["idx5"]).intersection(
        *[set(arrays[s]["idx5"]) for s in syms[1:]]
    ))
    n = len(ref_idx)
    pos_maps = {sym: {ts: i for i, ts in enumerate(arrays[sym]["idx5"])} for sym in syms}

    def tf_pos(idx_list, ts):
        return max(0, bisect.bisect_right(idx_list, ts) - 1)

    capital  = CAPITAL
    trades   = []
    open_pos = {}
    touches  = defaultdict(int)

    for i in range(60, n - 1):
        t_now  = ref_idx[i]
        t_next = ref_idx[i + 1]
        if t_now < test_start_ts: continue

        for zk in list(open_pos.keys()):
            p   = open_pos[zk]; sym = p["sym"]
            pm  = pos_maps[sym]
            if t_next not in pm: continue
            ni  = pm[t_next]; arr = arrays[sym]
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
        deploy = capital * POS_PCT

        for sym in syms:
            if len(open_pos) >= MAX_SIM: break
            arr = arrays[sym]
            ci5  = pos_maps[sym].get(t_now)
            if ci5 is None: continue
            ci15 = tf_pos(arr["idx15"], t_now)
            ci1h = tf_pos(arr["idx1h"], t_now)

            sig = get_signal(
                arr["h5"][:ci5+1],  arr["l5"][:ci5+1],
                arr["c5"][:ci5+1],  arr["v5"][:ci5+1],
                arr["h15"][:ci15+1], arr["l15"][:ci15+1], arr["c15"][:ci15+1],
                arr["h1h"][:ci1h+1], arr["l1h"][:ci1h+1],
                atr_cfg,
            )
            if sig is None: continue

            zone = sig["zone"]
            key  = _zk(sym, zone["center"])
            if touches[key] >= MAX_TOUCHES or key in open_pos: continue

            pm = pos_maps[sym]
            if t_next not in pm: continue
            ni = pm[t_next]
            if arr["l5"][ni] <= zone["high"]:
                if rng.random() > FILL_RATE: continue    # fill rate 65%
                fill = min(arr["o5"][ni], zone["center"])
                qty  = deploy / fill
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

    days = max((ref_idx[-1] - pd.Timestamp(test_start_ts)).days, 1)
    return trades, capital, days

def stats(trades, capital, days):
    n = len(trades)
    if n == 0: return None
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total  = sum(t["pnl"] for t in trades)
    wr     = len(wins) / n * 100
    sl     = sum(t["pnl"] for t in losses)
    pf     = abs(sum(t["pnl"] for t in wins) / sl) if sl else 99.0
    ann    = (capital - CAPITAL) / CAPITAL / days * 365 * 100
    eq     = np.array([CAPITAL] + [CAPITAL + sum(t["pnl"] for t in trades[:k+1]) for k in range(n)])
    pk     = np.maximum.accumulate(eq)
    mdd    = ((eq - pk) / pk * 100).min()
    return {
        "n": n, "wr": round(wr,1), "pf": round(pf,2),
        "total": round(total,2), "ann": round(ann,1), "mdd": round(mdd,1),
        "stops": len([t for t in trades if t["reason"]=="stop"]),
        "targets": len([t for t in trades if t["reason"]=="target"]),
        "avg_w": round(sum(t["pnl"] for t in wins)/len(wins), 4) if wins else 0,
        "avg_l": round(sum(t["pnl"] for t in losses)/len(losses), 4) if losses else 0,
    }

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print("\n" + "═"*75)
    print(f"  Backtest ATR filter — Fill rate {FILL_RATE*100:.0f}% — 2022 Bear + 2023 Recovery")
    print(f"  ETH+SOL | Binance US 5min | $1000 | pos_pct 50% | max_sim 2")
    print("═"*75)

    rng = random.Random(42)   # seed fixe pour reproductibilité

    # ── Téléchargement ──────────────────────────────────────────────────────
    print("\n  Téléchargement données (avec warmup 90j)...")
    period_data = {}
    for period_label, (start, end) in PERIODS.items():
        period_data[period_label] = {}
        for sym_key, bn_sym in SYMBOLS.items():
            df5 = download(bn_sym, start, end)
            if df5 is None or df5.empty:
                print(f"  ERREUR: {bn_sym} vide"); return
            period_data[period_label][sym_key] = {
                "5m":  df5,
                "15m": resample(df5, "15min"),
                "1h":  resample(df5, "1h"),
            }

    # ── Résultats par période ────────────────────────────────────────────────
    all_results = {}

    for period_label in PERIODS:
        test_start_ts = pd.Timestamp(TEST_START[period_label], tz="UTC")
        print(f"\n{'─'*75}")
        print(f"  {period_label} — test depuis {TEST_START[period_label]}")
        print(f"  {'Config':<26} {'N':>4} {'WR%':>6} {'PF':>5} {'P&L$':>8} {'Ann%':>8} {'MDD%':>6}  T|S")
        print(f"  {'─'*26} {'─'*4} {'─'*6} {'─'*5} {'─'*8} {'─'*8} {'─'*6}  {'─'*5}")

        period_results = {}
        for cfg_label, atr_cfg in ATR_CONFIGS.items():
            trades, capital, days = run(period_data[period_label], atr_cfg, test_start_ts, rng)
            s = stats(trades, capital, days)
            period_results[cfg_label] = s
            if s:
                flag = "✓" if s["pf"] >= 1.5 and s["total"] >= 0 else ("~" if s["total"] >= 0 else "✗")
                print(f"  [{flag}] {cfg_label:<24} {s['n']:>4} {s['wr']:>6.1f} {s['pf']:>5.2f}"
                      f" {s['total']:>+8.2f} {s['ann']:>+7.1f}% {s['mdd']:>+5.1f}%"
                      f"  {s['targets']}|{s['stops']}")
            else:
                print(f"  [?] {cfg_label:<24}    0  —  — —  —  —")
        all_results[period_label] = period_results

    # ── Analyse comparative ─────────────────────────────────────────────────
    print(f"\n{'═'*75}")
    print("  ANALYSE COMPARATIVE vs Baseline")
    print(f"{'─'*75}")

    for period_label, period_results in all_results.items():
        base = period_results.get("Baseline (sans ATR)")
        print(f"\n  ── {period_label} ──")
        for cfg_label, s in period_results.items():
            if cfg_label == "Baseline (sans ATR)" or s is None or base is None: continue
            dwr  = s["wr"]    - base["wr"]
            dpf  = s["pf"]    - base["pf"]
            dn   = s["n"]     - base["n"]
            dann = s["ann"]   - base["ann"]
            verdict = "✓ MIEUX" if dpf > 0.1 and dwr > 1 else ("~ NEUTRE" if dpf >= 0 else "✗ PIRE")
            print(f"  [{verdict}] {cfg_label}")
            print(f"    N: {base['n']} → {s['n']} ({dn:+d}) | WR: {base['wr']:.1f}% → {s['wr']:.1f}% ({dwr:+.1f}pp)"
                  f" | PF: {base['pf']:.2f} → {s['pf']:.2f} ({dpf:+.2f}) | Ann: {dann:+.1f}pp")

    # ── Récap global ─────────────────────────────────────────────────────────
    print(f"\n{'═'*75}")
    print("  RÉCAP GLOBAL — P&L cumulé 2022+2023 par config")
    print(f"{'─'*75}")
    for cfg_label in ATR_CONFIGS:
        total_pnl = sum(
            (r.get(cfg_label) or {}).get("total", 0)
            for r in all_results.values()
        )
        total_n = sum(
            (r.get(cfg_label) or {}).get("n", 0)
            for r in all_results.values()
        )
        avg_wr = np.mean([
            (r.get(cfg_label) or {}).get("wr", 0)
            for r in all_results.values()
            if r.get(cfg_label) and r[cfg_label]["n"] > 0
        ])
        flag = "✓" if total_pnl >= 0 else "✗"
        print(f"  [{flag}] {cfg_label:<26}  N={total_n:>4}  WR={avg_wr:.1f}%  P&L={total_pnl:>+8.2f}$")
    print("═"*75 + "\n")

if __name__ == "__main__":
    main()
