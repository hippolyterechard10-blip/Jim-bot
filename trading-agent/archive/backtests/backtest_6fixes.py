"""
backtest_6fixes.py — Test des 6 fixes audit expert
====================================================
Périodes : 2022 Bear + 2023 Recovery — Binance US 5m
Fill rate : 65% (seed=42 reproductible)
Capital $1,000 | pos_pct 50% | max_sim 2

Configs testées :
    0. Baseline    (code actuel)
    1. Fix R:R     (depuis zone_high, pas zone_center)
    2. Fix Trend   (10 bougies 1h, pas 4)
    3. Fix Pass3b  (4 bougies, pas 8)
    4. Fix Session (skip 22h-08h UTC)
    5. Fix RSI     (Wilder EMA, pas SMA)
    6. Combo       (tous les fixes)
"""
import warnings; warnings.filterwarnings("ignore")
import os, sys
import numpy as np
import pandas as pd
from collections import defaultdict

CACHE_DIR   = "binance_us_cache"
SYMBOLS     = ["ETHUSD", "SOLUSD"]
CAPITAL     = 1000.0
POS_PCT     = 0.50
MAX_SIM     = 2
ZONE_PCT    = 0.003
MAX_TOUCHES = 2
RSI_LOW     = 20
RSI_HIGH    = 65
TARGET_PCT  = 0.009
FILL_RATE   = 0.65

PERIODS = {
    "2022 Bear":     ("2022-01-01", "2022-12-31"),
    "2023 Recovery": ("2023-01-01", "2023-07-31"),
}

CONFIGS = {
    "0. Baseline":       {"rr_high": False, "trend_n": 4,  "p3b_n": 8, "sess": False, "wilder": False},
    "1. Fix R:R":        {"rr_high": True,  "trend_n": 4,  "p3b_n": 8, "sess": False, "wilder": False},
    "2. Fix Trend 10b":  {"rr_high": False, "trend_n": 10, "p3b_n": 8, "sess": False, "wilder": False},
    "3. Fix Pass3b 4b":  {"rr_high": False, "trend_n": 4,  "p3b_n": 4, "sess": False, "wilder": False},
    "4. Fix Session":    {"rr_high": False, "trend_n": 4,  "p3b_n": 8, "sess": True,  "wilder": False},
    "5. Fix RSI Wilder": {"rr_high": False, "trend_n": 4,  "p3b_n": 8, "sess": False, "wilder": True},
    "6. Combo":          {"rr_high": True,  "trend_n": 10, "p3b_n": 4, "sess": True,  "wilder": True},
}

# ── DATA LOADING ──────────────────────────────────────────────────────────────

def load_range(symbol, start, end):
    files = sorted([
        os.path.join(CACHE_DIR, f)
        for f in os.listdir(CACHE_DIR)
        if f.startswith(symbol + "_5m") and f.endswith(".parquet")
    ])
    if not files:
        raise FileNotFoundError(f"No cache for {symbol}")
    dfs = []
    for f in files:
        df = pd.read_parquet(f)
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        dfs.append(df)
    df = pd.concat(dfs).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    s  = pd.Timestamp(start, tz="UTC")
    e  = pd.Timestamp(end,   tz="UTC") + pd.Timedelta(days=1)
    df = df.loc[s:e, ["open","high","low","close","volume"]].copy()
    return df

def resample(df, rule):
    return df.resample(rule).agg({
        "open":"first","high":"max","low":"min","close":"last","volume":"sum"
    }).dropna()

# ── RSI ───────────────────────────────────────────────────────────────────────

def _rsi_sma(c, p=14):
    if len(c) < p+1: return 50.0
    d = np.diff(c); g = np.where(d>0,d,0.); l = np.where(d<0,-d,0.)
    ag = g[-p:].mean(); al = l[-p:].mean()
    return 100. if al==0 else 100-100/(1+ag/al)

def _rsi_wilder(c, p=14):
    if len(c) < p+1: return 50.0
    d = np.diff(c); g = np.where(d>0,d,0.); l = np.where(d<0,-d,0.)
    ag = g[:p].mean(); al = l[:p].mean()
    a = 1./p
    for i in range(p, len(g)):
        ag = ag*(1-a)+g[i]*a; al = al*(1-a)+l[i]*a
    return 100. if al==0 else 100-100/(1+ag/al)

def calc_rsi(c, wilder): return _rsi_wilder(c) if wilder else _rsi_sma(c)

# ── ZONES ─────────────────────────────────────────────────────────────────────

def find_zones(h15, l15, c15):
    curr = c15[-1]
    sw = []
    for i in range(2, len(l15)-2):
        if l15[i]<l15[i-1] and l15[i]<l15[i-2] and l15[i]<l15[i+1] and l15[i]<l15[i+2]:
            sw.append((l15[i], h15[i]))
    if not sw: return []
    sw.sort()
    cl = [[sw[0]]]
    for v in sw[1:]:
        if (v[0]-cl[-1][0][0])/cl[-1][0][0] < ZONE_PCT*2: cl[-1].append(v)
        else: cl.append([v])
    res = []
    for c in cl:
        ctr = sum(x[0] for x in c)/len(c); wk = min(x[0] for x in c)
        if ctr < curr*0.999:
            res.append({"c": ctr, "h": ctr*(1+ZONE_PCT), "l": ctr*(1-ZONE_PCT), "wk": wk})
    return sorted(res, key=lambda x: x["c"], reverse=True)

def dyn_stop(lo5, entry, wk):
    fl = entry*0.992
    c  = min(lo5[-8:])*0.999 if len(lo5)>=8 else wk*0.999
    if fl <= c < entry: return c
    z = wk*0.999
    if fl <= z < entry: return z
    return entry*0.997

# ── BACKTEST (fast numpy-indexed) ─────────────────────────────────────────────

def run_period(sym_data, cfg, start, end):
    """Run one config on pre-loaded sym_data dict."""
    import random; rng = random.Random(42)

    # Build shared 5m timeline from intersection
    ref_idx = None
    for sym in SYMBOLS:
        idx = sym_data[sym]["idx5"]
        ref_idx = idx if ref_idx is None else np.intersect1d(ref_idx, idx)
    if len(ref_idx) < 100: return None
    days = max(1, (ref_idx[-1]-ref_idx[0]).astype("timedelta64[D]").astype(int))

    capital  = CAPITAL
    trades   = []
    open_pos = {}
    touches  = defaultdict(int)
    peak     = capital
    max_dd   = 0.0

    for i in range(60, len(ref_idx)-1):
        t     = ref_idx[i]
        t_nx  = ref_idx[i+1]
        t_hr  = int(pd.Timestamp(t).hour)  # UTC hour

        # ── Session filter ────────────────────────────────────────────────────
        if cfg["sess"] and not (8 <= t_hr < 22):
            # Still need to manage open positions even during off-session
            pass
        else:
            pass  # handled below after position management

        # ── Manage open positions ─────────────────────────────────────────────
        for zk in list(open_pos.keys()):
            p = open_pos[zk]; sym = p["sym"]
            d = sym_data[sym]
            j = np.searchsorted(d["idx5"], t_nx, side="left")
            if j >= len(d["idx5"]) or d["idx5"][j] != t_nx: continue
            hi = d["h5"][j]; lo = d["l5"][j]; cl = d["c5"][j]
            sh = lo <= p["stop"]; th = hi >= p["target"]; to = (i-p["bar"]) >= 48
            ep = er = None
            if   sh and th: ep, er = p["stop"],   "stop"
            elif sh:         ep, er = p["stop"],   "stop"
            elif th:         ep, er = p["target"], "target"
            elif to:         ep, er = cl,          "timeout"
            if ep is not None:
                pnl = (ep-p["entry"])*p["qty"]; capital += pnl
                trades.append({"sym":sym,"entry":p["entry"],"exit":ep,"pnl":round(pnl,4),"reason":er})
                del open_pos[zk]
                peak   = max(peak, capital)
                max_dd = max(max_dd, (peak-capital)/peak)

        if capital < 20 or len(open_pos) >= MAX_SIM: continue
        if cfg["sess"] and not (8 <= t_hr < 22): continue

        deploy = capital * POS_PCT

        # ── Evaluate signals ──────────────────────────────────────────────────
        for sym in SYMBOLS:
            if len(open_pos) >= MAX_SIM: break
            d = sym_data[sym]

            # Slice arrays up to t
            i5  = np.searchsorted(d["idx5"],  t, side="right")
            i15 = np.searchsorted(d["idx15"], t, side="right")
            i1h = np.searchsorted(d["idx1h"], t, side="right")
            if i5 < 30 or i15 < 20 or i1h < 12: continue

            # 1h bias
            nb = cfg["trend_n"]
            h1 = d["h1h"][max(0,i1h-50):i1h]; l1 = d["l1h"][max(0,i1h-50):i1h]
            if len(h1) < nb or (h1[-1] < h1[-nb] and l1[-1] < l1[-nb]): continue

            # 5m slices
            cl5 = d["c5"][max(0,i5-30):i5]
            lo5 = d["l5"][max(0,i5-30):i5]
            hi5 = d["h5"][max(0,i5-30):i5]
            vo5 = d["v5"][max(0,i5-30):i5]
            if len(cl5) < 10: continue
            curr = cl5[-1]

            # Volume check
            ref_vol = vo5[-2] if len(vo5)>=2 and vo5[-2]>0 else (
                float(np.mean([v for v in vo5[-6:-1] if v>0] or [1.0])))
            avgv = vo5[-20:].mean() if len(vo5)>=20 else vo5.mean()
            if avgv > 0 and ref_vol < avgv*0.3: continue

            rsi = calc_rsi(cl5, cfg["wilder"])
            if not (RSI_LOW <= rsi <= RSI_HIGH): continue

            # 15m zones
            h15 = d["h15"][max(0,i15-100):i15]
            l15 = d["l15"][max(0,i15-100):i15]
            zones = find_zones(h15, l15, cl5)

            sig_found = None
            for zone in zones:
                dist = (curr - zone["c"]) / curr
                if not (0.001 <= dist <= 0.020): continue

                # RSI divergence
                div = False
                if len(cl5) >= 5:
                    rp = _rsi_wilder(cl5[:-3]) if cfg["wilder"] else _rsi_sma(cl5[:-3])
                    div = cl5[-1] < cl5[-4] and rsi > rp
                if not div and not (30 <= rsi <= 55): continue

                # Pass 3b
                pb = cfg["p3b_n"]
                if not any(lo5[-pb:] <= zone["h"]): continue
                if cl5[-1] <= zone["l"]: continue

                stop   = dyn_stop(lo5, zone["c"], zone["wk"])
                target = round(zone["h"] * (1+TARGET_PCT), 4)

                if cfg["rr_high"]:
                    risk = abs(zone["h"]-stop); reward = abs(target-zone["h"])
                else:
                    risk = abs(zone["c"]-stop); reward = abs(target-zone["c"])
                if risk <= 0 or reward/risk < 1.2: continue

                sig_found = {"zone": zone, "stop": stop, "target": target}
                break

            if sig_found is None: continue

            zone = sig_found["zone"]
            zk   = f"{sym}_{round(zone['c'],2)}"
            if touches[zk] >= MAX_TOUCHES or zk in open_pos: continue

            # Check fill at next bar
            j = np.searchsorted(d["idx5"], t_nx, side="left")
            if j >= len(d["idx5"]) or d["idx5"][j] != t_nx: continue
            if d["l5"][j] <= zone["h"]:
                if rng.random() > FILL_RATE: continue
                fill = min(float(d["o5"][j]), zone["h"])
                qty  = deploy / fill
                touches[zk] += 1
                open_pos[zk] = {"sym":sym,"entry":fill,"stop":sig_found["stop"],
                                "target":sig_found["target"],"qty":qty,"bar":i}

    # Close remaining
    for zk, p in open_pos.items():
        d   = sym_data[p["sym"]]
        cl  = float(d["c5"][-1])
        pnl = (cl-p["entry"])*p["qty"]; capital += pnl
        trades.append({"sym":p["sym"],"entry":p["entry"],"exit":cl,
                       "pnl":round(pnl,4),"reason":"eod"})

    if not trades:
        return {"n":0,"wr":0.0,"pf":0.0,"pnl":0.0,"mdd":0.0,"tpd":0.0}

    pnls   = [t["pnl"] for t in trades]
    wins   = [p for p in pnls if p>0]; losses = [p for p in pnls if p<0]
    gw     = sum(wins);  gl = abs(sum(losses))
    pf     = round(gw/gl,2) if gl>0 else 999.0
    wr     = round(len(wins)/len(pnls)*100,1)
    return {"n":len(trades),"wr":wr,"pf":pf,"pnl":round(sum(pnls),2),
            "mdd":round(max_dd*100,1),"tpd":round(len(trades)/days,2)}

# ── MAIN ──────────────────────────────────────────────────────────────────────

def load_sym_data(sym, start, end):
    df5 = load_range(sym, start, end)
    d15 = resample(df5, "15min")
    d1h = resample(df5, "1h")
    return {
        "idx5":  df5.index.values, "o5": df5["open"].values,
        "h5":    df5["high"].values,  "l5": df5["low"].values,
        "c5":    df5["close"].values, "v5": df5["volume"].values,
        "idx15": d15.index.values,
        "h15":   d15["high"].values,  "l15": d15["low"].values,
        "idx1h": d1h.index.values,
        "h1h":   d1h["high"].values,  "l1h": d1h["low"].values,
    }

def pfmt(pf): return "∞" if pf>=999 else f"{pf:.2f}"

if __name__ == "__main__":
    print("\n" + "="*74)
    print("  BACKTEST 6 FIXES — ETH+SOL — Binance US 5m — fill=65% seed=42")
    print("="*74)

    results = {}

    for period_name, (start, end) in PERIODS.items():
        print(f"\n── {period_name}  ({start} → {end}) ──")
        sym_data = {}
        for sym in SYMBOLS:
            df5 = load_range(sym, start, end)
            print(f"   {sym}: {len(df5):,} bars")
            d15 = resample(df5, "15min"); d1h = resample(df5, "1h")
            sym_data[sym] = {
                "idx5":  df5.index.values, "o5": df5["open"].values,
                "h5":    df5["high"].values,  "l5": df5["low"].values,
                "c5":    df5["close"].values, "v5": df5["volume"].values,
                "idx15": d15.index.values,
                "h15":   d15["high"].values, "l15": d15["low"].values,
                "idx1h": d1h.index.values,
                "h1h":   d1h["high"].values, "l1h": d1h["low"].values,
            }

        results[period_name] = {}
        print(f"   {'Config':<22}  {'N':>4}  {'WR':>6}  {'PF':>6}  {'PnL':>8}  {'MDD':>5}  tpd")
        print(f"   {'─'*22}  {'─'*4}  {'─'*6}  {'─'*6}  {'─'*8}  {'─'*5}  ───")
        for cname, cfg in CONFIGS.items():
            r = run_period(sym_data, cfg, start, end)
            results[period_name][cname] = r
            sgn = "+" if r["pnl"] >= 0 else ""
            print(f"   {cname:<22}  {r['n']:>4}  {r['wr']:>5.1f}%  {pfmt(r['pf']):>6}  "
                  f"{sgn}${r['pnl']:>7.0f}  {r['mdd']:>4.1f}%  {r['tpd']:.2f}")

    # Summary
    print("\n" + "="*74)
    print("  RÉSUMÉ — PF  /  PnL$  par config")
    print(f"  {'Config':<22}  " + "  ".join(f"{'PF':>5} {'PnL':>7}" for _ in PERIODS))
    print(f"  {'':22}  " + "  ".join(f"{p[:13]:>13}" for p in PERIODS))
    for cname in CONFIGS:
        row = f"  {cname:<22}  "
        row += "  ".join(
            f"{pfmt(results[p][cname]['pf']):>5} ${results[p][cname]['pnl']:>6.0f}"
            for p in PERIODS
        )
        print(row)
    print(f"\n  Baseline=config 0 | $1,000 | 50%/pos | fill=65% | seed=42\n")
