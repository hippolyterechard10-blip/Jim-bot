"""
backtest_current.py — Code actuel exact (Fixes 1+3+5 + Circuit-breaker 3%)
===========================================================================
Périodes : 2022 COMPLET + 2023 COMPLET — Binance US 5min
Capital $1,000 | pos_pct 50% | max_sim 2 | fill_rate 65% seed=42

Config exacte du bot live :
  Fix 1 — R:R depuis zone_high (pas zone_center)
  Fix 3 — Pass3b 4 bougies (pas 8)
  Fix 5 — RSI Wilder EMA (pas SMA)
  CB    — Circuit-breaker 3% journalier (pause si perte > $30/jour)

Outputs :
  - Stats globales par période
  - Breakdown mensuel
  - Breakdown ETH vs SOL
  - Comparaison sans/avec circuit-breaker
"""
import warnings; warnings.filterwarnings("ignore")
import os
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
CB_PCT      = 0.03       # circuit-breaker : 3% du capital initial

PERIODS = {
    "2022 Full": ("2022-01-01", "2022-12-31"),
    "2023 Full": ("2023-01-01", "2023-12-31"),
}

# ── DATA ──────────────────────────────────────────────────────────────────────

def load_range(symbol, start, end):
    files = sorted([
        os.path.join(CACHE_DIR, f)
        for f in os.listdir(CACHE_DIR)
        if f.startswith(symbol + "_5m") and f.endswith(".parquet")
    ])
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
    return df.loc[s:e, ["open","high","low","close","volume"]].copy()

def resample(df, rule):
    return df.resample(rule).agg({
        "open":"first","high":"max","low":"min","close":"last","volume":"sum"
    }).dropna()

# ── RSI Wilder EMA (exact code actuel) ────────────────────────────────────────

def _rsi_wilder(c, p=14):
    if len(c) < p+1: return 50.0
    c = c.astype(float); d = np.diff(c)
    g = np.where(d>0,d,0.); l = np.where(d<0,-d,0.)
    ag = g[:p].mean(); al = l[:p].mean()
    a = 1./p
    for i in range(p, len(g)):
        ag = ag*(1-a)+g[i]*a; al = al*(1-a)+l[i]*a
    return 100. if al==0 else 100-100/(1+ag/al)

# ── ZONES ─────────────────────────────────────────────────────────────────────

def find_zones(h15, l15, c15):
    curr = c15[-1]; sw = []
    for i in range(2, len(l15)-2):
        if l15[i]<l15[i-1] and l15[i]<l15[i-2] and l15[i]<l15[i+1] and l15[i]<l15[i+2]:
            sw.append((l15[i], h15[i]))
    if not sw: return []
    sw.sort(); cl = [[sw[0]]]
    for v in sw[1:]:
        if (v[0]-cl[-1][0][0])/cl[-1][0][0] < ZONE_PCT*2: cl[-1].append(v)
        else: cl.append([v])
    res = []
    for c in cl:
        ctr = sum(x[0] for x in c)/len(c); wk = min(x[0] for x in c)
        if ctr < curr*0.999:
            res.append({"c":ctr,"h":ctr*(1+ZONE_PCT),"l":ctr*(1-ZONE_PCT),"wk":wk})
    return sorted(res, key=lambda x: x["c"], reverse=True)

def dyn_stop(lo5, entry, wk):
    fl = entry*0.992
    c  = min(lo5[-8:])*0.999 if len(lo5)>=8 else wk*0.999
    if fl <= c < entry: return c
    z = wk*0.999
    if fl <= z < entry: return z
    return entry*0.997

# ── BACKTEST ──────────────────────────────────────────────────────────────────

def run_period(sym_data, use_cb=True):
    import random; rng = random.Random(42)

    ref_idx = None
    for sym in SYMBOLS:
        idx = sym_data[sym]["idx5"]
        ref_idx = idx if ref_idx is None else np.intersect1d(ref_idx, idx)
    if len(ref_idx) < 100: return [], CAPITAL, 1

    days     = max(1, (ref_idx[-1]-ref_idx[0]).astype("timedelta64[D]").astype(int))
    capital  = CAPITAL
    trades   = []
    open_pos = {}
    touches  = defaultdict(int)
    daily_pnl: dict = {}     # date_str -> float (circuit-breaker tracking)

    for i in range(60, len(ref_idx)-1):
        t    = ref_idx[i]
        t_nx = ref_idx[i+1]
        t_pd = pd.Timestamp(t)
        day  = t_pd.strftime("%Y-%m-%d")

        # ── Manage open positions ─────────────────────────────────────────────
        for zk in list(open_pos.keys()):
            p = open_pos[zk]; sym = p["sym"]; d = sym_data[sym]
            j = np.searchsorted(d["idx5"], t_nx, side="left")
            if j >= len(d["idx5"]) or d["idx5"][j] != t_nx: continue
            hi = d["h5"][j]; lo = d["l5"][j]; cl = d["c5"][j]
            sh = lo<=p["stop"]; th = hi>=p["target"]; to = (i-p["bar"])>=48
            ep = er = None
            if   sh and th: ep, er = p["stop"],   "stop"
            elif sh:         ep, er = p["stop"],   "stop"
            elif th:         ep, er = p["target"], "target"
            elif to:         ep, er = cl,          "timeout"
            if ep is not None:
                pnl = (ep-p["entry"])*p["qty"]; capital += pnl
                close_day = pd.Timestamp(t_nx).strftime("%Y-%m-%d")
                daily_pnl[close_day] = daily_pnl.get(close_day, 0.0) + pnl
                trades.append({
                    "sym":    sym,
                    "entry":  p["entry"],
                    "exit":   ep,
                    "pnl":    round(pnl, 4),
                    "reason": er,
                    "pct":    round((ep-p["entry"])/p["entry"]*100, 3),
                    "day":    close_day,
                    "month":  close_day[:7],
                    "cb_blocked": False,
                })
                del open_pos[zk]

        if capital < 20 or len(open_pos) >= MAX_SIM: continue

        # ── Circuit-breaker check ─────────────────────────────────────────────
        if use_cb:
            dp = daily_pnl.get(day, 0.0)
            if dp < -(CAPITAL * CB_PCT):
                continue

        deploy = capital * POS_PCT

        # ── Evaluate signals ──────────────────────────────────────────────────
        for sym in SYMBOLS:
            if len(open_pos) >= MAX_SIM: break
            d = sym_data[sym]
            i5  = np.searchsorted(d["idx5"],  t, side="right")
            i15 = np.searchsorted(d["idx15"], t, side="right")
            i1h = np.searchsorted(d["idx1h"], t, side="right")
            if i5 < 30 or i15 < 20 or i1h < 12: continue

            h1 = d["h1h"][max(0,i1h-50):i1h]; l1 = d["l1h"][max(0,i1h-50):i1h]
            if len(h1) < 4 or (h1[-1]<h1[-4] and l1[-1]<l1[-4]): continue

            cl5 = d["c5"][max(0,i5-30):i5]; lo5 = d["l5"][max(0,i5-30):i5]
            vo5 = d["v5"][max(0,i5-30):i5]
            if len(cl5) < 10: continue
            curr = cl5[-1]

            ref_vol = vo5[-2] if len(vo5)>=2 and vo5[-2]>0 else (
                float(np.mean([v for v in vo5[-6:-1] if v>0] or [1.0])))
            avgv = vo5[-20:].mean() if len(vo5)>=20 else vo5.mean()
            if avgv > 0 and ref_vol < avgv*0.3: continue

            rsi = _rsi_wilder(cl5)
            if not (RSI_LOW <= rsi <= RSI_HIGH): continue

            h15 = d["h15"][max(0,i15-100):i15]; l15 = d["l15"][max(0,i15-100):i15]
            zones = find_zones(h15, l15, cl5)

            sig_found = None
            for zone in zones:
                dist = (curr - zone["c"]) / curr
                if not (0.001 <= dist <= 0.020): continue

                rp  = _rsi_wilder(cl5[:-3])
                div = cl5[-1] < cl5[-4] and rsi > rp
                if not div and not (30 <= rsi <= 55): continue

                if not any(lo5[-4:] <= zone["h"]): continue    # Fix 3: 4 bougies
                if cl5[-1] <= zone["l"]: continue

                stop   = dyn_stop(lo5, zone["c"], zone["wk"])
                target = round(zone["h"] * (1+TARGET_PCT), 4)

                # Fix 1: R:R depuis zone_high
                risk = abs(zone["h"]-stop); reward = abs(target-zone["h"])
                if risk <= 0 or reward/risk < 1.2: continue

                sig_found = {"zone": zone, "stop": stop, "target": target}
                break

            if sig_found is None: continue
            zone = sig_found["zone"]
            zk   = f"{sym}_{round(zone['c'],2)}"
            if touches[zk] >= MAX_TOUCHES or zk in open_pos: continue

            j = np.searchsorted(d["idx5"], t_nx, side="left")
            if j >= len(d["idx5"]) or d["idx5"][j] != t_nx: continue
            if d["l5"][j] <= zone["h"]:
                if rng.random() > FILL_RATE: continue
                fill = min(float(d["o5"][j]), zone["h"])
                qty  = deploy / fill
                touches[zk] += 1
                open_pos[zk] = {"sym":sym,"entry":fill,"stop":sig_found["stop"],
                                "target":sig_found["target"],"qty":qty,"bar":i}

    # Close remaining open
    for zk, p in open_pos.items():
        cl  = float(sym_data[p["sym"]]["c5"][-1])
        pnl = (cl-p["entry"])*p["qty"]; capital += pnl
        trades.append({"sym":p["sym"],"entry":p["entry"],"exit":cl,
                       "pnl":round(pnl,4),"reason":"end","pct":0.0,
                       "day":"end","month":"end","cb_blocked":False})

    return trades, capital, days

# ── STATS ─────────────────────────────────────────────────────────────────────

def compute_stats(trades, capital, days):
    n = len(trades)
    if n == 0:
        return None
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total  = sum(t["pnl"] for t in trades)
    wr     = len(wins)/n*100
    sl     = sum(t["pnl"] for t in losses)
    pf     = abs(sum(t["pnl"] for t in wins)/sl) if sl else 99.0
    ann    = (capital-CAPITAL)/CAPITAL/days*365*100
    eq     = np.array([CAPITAL]+[CAPITAL+sum(t["pnl"] for t in trades[:k+1]) for k in range(n)])
    pk     = np.maximum.accumulate(eq)
    mdd    = ((eq-pk)/pk*100).min()
    avg_w  = sum(t["pnl"] for t in wins)/len(wins)     if wins   else 0.0
    avg_l  = sum(t["pnl"] for t in losses)/len(losses) if losses else 0.0
    stops  = len([t for t in trades if t["reason"]=="stop"])
    tgts   = len([t for t in trades if t["reason"]=="target"])
    tos    = len([t for t in trades if t["reason"] in ("timeout","end")])
    eth    = [t for t in trades if "ETH" in t["sym"]]
    sol    = [t for t in trades if "SOL" in t["sym"]]
    return {
        "n": n, "wr": round(wr,1), "pf": round(pf,2),
        "total": round(total,2), "ann": round(ann,1),
        "mdd": round(mdd,1), "avg_w": round(avg_w,2), "avg_l": round(avg_l,2),
        "stops": stops, "tgts": tgts, "tos": tos, "tpd": round(n/days,2),
        "eth_n": len(eth), "eth_pnl": round(sum(t["pnl"] for t in eth),2),
        "sol_n": len(sol), "sol_pnl": round(sum(t["pnl"] for t in sol),2),
        "days": days, "final": round(capital,2),
    }

def monthly_breakdown(trades):
    months = sorted(set(t["month"] for t in trades if t["month"] != "end"))
    rows = []
    for m in months:
        mt = [t for t in trades if t["month"] == m]
        n  = len(mt)
        pnl = sum(t["pnl"] for t in mt)
        wins = sum(1 for t in mt if t["pnl"] > 0)
        wr   = wins/n*100 if n else 0
        rows.append((m, n, round(pnl,2), round(wr,1)))
    return rows

def print_stats(name, s):
    if s is None:
        print(f"  {name}: no trades"); return
    print(f"\n  {'─'*58}")
    print(f"  {name}")
    print(f"  {'─'*58}")
    print(f"  Trades    : {s['n']:>5}   ({s['tpd']:.2f}/day, {s['days']} days)")
    print(f"  Win rate  : {s['wr']:>5.1f}%")
    print(f"  Profit F. : {s['pf']:>5.2f}")
    print(f"  Total PnL : ${s['total']:>+8.0f}  ({s['ann']:+.1f}% ann.)")
    print(f"  Final cap : ${s['final']:>8.2f}  (from $1,000)")
    print(f"  Max DD    : {s['mdd']:>5.1f}%")
    print(f"  Avg win   : ${s['avg_w']:>+7.2f}  |  Avg loss: ${s['avg_l']:>+7.2f}")
    print(f"  Exits     : stops={s['stops']}  targets={s['tgts']}  timeout={s['tos']}")
    print(f"  ETH       : {s['eth_n']} trades  ${s['eth_pnl']:>+.0f}")
    print(f"  SOL       : {s['sol_n']} trades  ${s['sol_pnl']:>+.0f}")

def load_sym_data(sym, start, end):
    df5 = load_range(sym, start, end)
    d15 = resample(df5, "15min"); d1h = resample(df5, "1h")
    return {
        "idx5":  df5.index.values, "o5": df5["open"].values,
        "h5":    df5["high"].values,  "l5": df5["low"].values,
        "c5":    df5["close"].values, "v5": df5["volume"].values,
        "idx15": d15.index.values,
        "h15":   d15["high"].values, "l15": d15["low"].values,
        "idx1h": d1h.index.values,
        "h1h":   d1h["high"].values, "l1h": d1h["low"].values,
    }

# ── MAIN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "="*62)
    print("  BACKTEST — CODE ACTUEL EXACT")
    print("  Fixes 1+3+5 + Circuit-breaker 3% journalier")
    print("  ETH/USD + SOL/USD | $1,000 capital | 65% fill rate")
    print("="*62)

    results_no_cb = {}
    results_cb    = {}

    for period_name, (start, end) in PERIODS.items():
        print(f"\n{'═'*62}")
        print(f"  {period_name}  ({start} → {end})")
        print(f"{'═'*62}")

        sym_data = {}
        for sym in SYMBOLS:
            df5 = load_range(sym, start, end)
            print(f"  {sym}: {len(df5):,} bars 5m")
            sym_data[sym] = load_sym_data(sym, start, end)

        # Run without CB
        t_nocb, cap_nocb, days = run_period(sym_data, use_cb=False)
        s_nocb = compute_stats(t_nocb, cap_nocb, days)
        results_no_cb[period_name] = s_nocb
        print_stats("▷ Sans circuit-breaker", s_nocb)

        # Run with CB
        t_cb, cap_cb, days = run_period(sym_data, use_cb=True)
        s_cb = compute_stats(t_cb, cap_cb, days)
        results_cb[period_name] = s_cb
        print_stats("▷ Avec circuit-breaker 3%", s_cb)

        # Monthly breakdown (with CB)
        if t_cb:
            mb = monthly_breakdown(t_cb)
            print(f"\n  {'─'*58}")
            print(f"  BREAKDOWN MENSUEL — {period_name} (avec CB)")
            print(f"  {'─'*58}")
            print(f"  {'Mois':<8}  {'Trades':>6}  {'PnL':>9}  {'WR':>6}  {'Bar'}")
            print(f"  {'─'*8}  {'─'*6}  {'─'*9}  {'─'*6}  {'─'*20}")
            cumul = 0.0
            for m, n, pnl, wr in mb:
                cumul += pnl
                bar_len = int(abs(pnl) / 5)
                bar = ("█" * min(bar_len,20)) if pnl >= 0 else ("▒" * min(bar_len,20))
                sign = "+" if pnl >= 0 else ""
                print(f"  {m}  {n:>6}  {sign}${pnl:>7.0f}  {wr:>5.0f}%  {bar}")
            print(f"  {'':8}  {'':6}  {'TOTAL':>9}  {'':6}  cumul: ${cumul:+.0f}")

    # ── Comparison summary ────────────────────────────────────────────────────
    print(f"\n{'='*62}")
    print("  COMPARAISON GLOBALE : Sans CB vs Avec CB 3%")
    print(f"{'='*62}")
    print(f"\n  {'Métrique':<18}  {'NoCB 2022':>10}  {'CB 2022':>10}  "
          f"{'NoCB 2023':>10}  {'CB 2023':>10}")
    print(f"  {'─'*18}  {'─'*10}  {'─'*10}  {'─'*10}  {'─'*10}")

    metrics = [
        ("N trades",  "n",     "d"),
        ("Win rate",  "wr",    "p"),
        ("Profit F.", "pf",    "f"),
        ("Total PnL", "total", "$"),
        ("Ann. ret.", "ann",   "a"),
        ("Max DD",    "mdd",   "p"),
        ("Final cap", "final", "c"),
    ]
    for label, key, fmt in metrics:
        vals = []
        for pname in PERIODS:
            for src in [results_no_cb, results_cb]:
                s = src[pname]
                v = s[key] if s else 0
                if   fmt=="$": vals.append(f"${v:>+8.0f}")
                elif fmt=="c": vals.append(f"${v:>8.0f}")
                elif fmt=="p": vals.append(f"{v:>8.1f}%")
                elif fmt=="a": vals.append(f"{v:>8.1f}%")
                elif fmt=="f": vals.append(f"{v:>10.2f}")
                else:          vals.append(f"{v:>10d}")
        print(f"  {label:<18}  {'  '.join(vals)}")

    print(f"\n  {'─'*62}")
    print("  IMPACT CIRCUIT-BREAKER (CB − NoCB)")
    print(f"  {'─'*62}")
    for pname in PERIODS:
        nb = results_no_cb[pname]
        cb = results_cb[pname]
        if nb and cb:
            dn = cb["n"]     - nb["n"]
            dp = cb["total"] - nb["total"]
            dm = cb["mdd"]   - nb["mdd"]
            df_ = cb["pf"]   - nb["pf"]
            print(f"  {pname:<14}: trades {dn:>+d}  PnL ${dp:>+.0f}"
                  f"  MDD {dm:>+.1f}%  PF {df_:>+.2f}")
    print()
