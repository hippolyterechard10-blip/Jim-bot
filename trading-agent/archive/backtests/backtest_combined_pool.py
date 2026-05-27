"""
backtest_combined_pool.py — ETH+SOL pool partagé $1000
=======================================================
Pool unique $1,000. max_sim GLOBAL = 2. Chaque trade = $500.
ETH peut prendre 2 slots si SOL idle, et vice versa.

Compare :
  A — ETH-only  : max_sim=2, $500/trade
  B — SOL-only  : max_sim=2, $500/trade
  C — ETH+SOL   : pool partagé, max_sim global=2, $500/trade
                  les deux assets se partagent les slots disponibles
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
CAPITAL     = 1000.0
POS_PCT     = 0.50       # $500 par trade
MAX_SIM     = 2          # max positions GLOBALES simultanées
LOOKBACK    = 60
ZONE_PCT    = 0.003
MAX_TOUCHES = 2
RSI_LOW     = 20
RSI_HIGH    = 65
TARGET_PCT  = 0.009

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

# ── DOWNLOAD ──────────────────────────────────────────────────────────────────
def download(symbol):
    end            = datetime.now(timezone.utc)
    start          = end - timedelta(days=LOOKBACK + 5)
    start_intraday = end - timedelta(days=58)  # yfinance limite 5m/15m à 60j
    dfs = {}
    specs = [("1h", start), ("15m", start_intraday), ("5m", start_intraday)]
    for key, s in specs:
        df = yf.download(symbol, start=s, end=end, interval=key,
                         auto_adjust=True, progress=False)
        if df.empty: return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.index   = pd.to_datetime(df.index, utc=True)
        df.columns = [c.lower() for c in df.columns]
        dfs[key]   = df
    return dfs

def get_signal(sym, t_now, dfs):
    b1h  = dfs["1h"][dfs["1h"].index   <= t_now].tail(50)
    b15m = dfs["15m"][dfs["15m"].index <= t_now].tail(100)
    b5m  = dfs["5m"][dfs["5m"].index   <= t_now].tail(30)
    if len(b1h) < 10 or len(b15m) < 20 or len(b5m) < 10: return None

    h1 = b1h["high"].values; l1 = b1h["low"].values
    if h1[-1] < h1[-4] and l1[-1] < l1[-4]: return None

    cl5  = b5m["close"].values
    vo5  = b5m["volume"].values
    rsi  = _rsi(cl5, 14)
    avgv = vo5[-20:].mean() if len(vo5) >= 20 else vo5.mean()
    if avgv > 0 and vo5[-1] < avgv * 0.3: return None

    zones = _find_zones(b15m["high"].values, b15m["low"].values, cl5)
    curr  = float(b5m["close"].iloc[-1])

    for zone in zones:
        dist = (curr - zone["center"]) / curr
        if not (0.001 <= dist <= 0.020): continue
        if not (RSI_LOW <= rsi <= RSI_HIGH): continue
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

# ── BACKTEST GÉNÉRIQUE ────────────────────────────────────────────────────────
def run(symbols):
    print(f"  Téléchargement {symbols}...")
    all_dfs = {}
    for sym in symbols:
        d = download(sym)
        if d is None:
            print(f"  {sym}: impossible de télécharger"); return [], CAPITAL
        all_dfs[sym] = d
        print(f"  {sym} : {len(d['5m'])} bars 5min")

    ref_sym = symbols[0]
    idx     = list(all_dfs[ref_sym]["5m"].index)

    capital  = CAPITAL
    trades   = []
    open_pos = {}          # zk → position
    touches  = defaultdict(int)

    for i in range(55, len(idx) - 1):
        t_now  = idx[i]
        t_next = idx[i + 1]

        # Gérer positions ouvertes
        for zk in list(open_pos.keys()):
            p   = open_pos[zk]
            sym = p["sym"]
            df5 = all_dfs[sym]["5m"]
            if t_next not in df5.index: continue
            nb = df5.loc[t_next]
            hi = float(nb["high"]); lo = float(nb["low"]); cl = float(nb["close"])
            sh = lo <= p["stop"]; th = hi >= p["target"]; to = (i - p["bar"]) >= 48
            ep = er = None
            if sh and th: ep, er = p["stop"],   "stop"
            elif sh:      ep, er = p["stop"],   "stop"
            elif th:      ep, er = p["target"], "target"
            elif to:      ep, er = cl,          "timeout"
            if ep:
                pnl = (ep - p["entry"]) * p["qty"]
                capital += pnl
                trades.append({
                    "sym": sym, "entry": p["entry"], "exit": ep,
                    "pnl": round(pnl, 4), "reason": er, "day": t_now.date(),
                })
                del open_pos[zk]

        if capital < 20: continue
        if len(open_pos) >= MAX_SIM: continue  # pool global plein

        deploy = CAPITAL * POS_PCT  # $500 fixe

        # Évaluer chaque asset dans l'ordre
        for sym in symbols:
            if len(open_pos) >= MAX_SIM: break
            if sym not in all_dfs: continue

            sig = get_signal(sym, t_now, all_dfs[sym])
            if sig is None: continue

            zone = sig["zone"]
            key  = _zk(sym, zone["center"])
            if touches[key] >= MAX_TOUCHES: continue
            if key in open_pos: continue

            df5    = all_dfs[sym]["5m"]
            if t_next not in df5.index: continue
            nb     = df5.loc[t_next]
            nb_lo  = float(nb["low"])
            nb_open= float(nb["open"])

            if nb_lo <= zone["high"]:
                fill = min(nb_open, zone["center"])
                qty  = deploy / fill
                touches[key] += 1
                open_pos[key] = {
                    "sym": sym, "entry": fill,
                    "stop": sig["stop"], "target": sig["target"],
                    "qty": qty, "bar": i,
                }

    # Fermer positions restantes
    for key, p in open_pos.items():
        sym  = p["sym"]
        last = float(all_dfs[sym]["5m"]["close"].iloc[-1])
        pnl  = (last - p["entry"]) * p["qty"]
        capital += pnl
        trades.append({
            "sym": sym, "entry": p["entry"], "exit": last,
            "pnl": round(pnl, 4), "reason": "end",
            "day": all_dfs[sym]["5m"].index[-1].date(),
        })

    return trades, capital

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
    touts  = len([t for t in trades if t["reason"] == "timeout"])
    per_day= n / LOOKBACK
    return {
        "n": n, "wr": round(wr, 1), "pf": round(pf, 2),
        "total": round(total, 2), "ann": round(ann, 1), "mdd": round(mdd, 1),
        "stops": stops, "targets": tgts, "timeouts": touts,
        "per_day": round(per_day, 1),
    }

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print("\n" + "═"*72)
    print(f"  Pool Partagé — ETH-only vs SOL-only vs ETH+SOL")
    print(f"  Capital ${CAPITAL:.0f} | pos_pct {POS_PCT*100:.0f}% (${CAPITAL*POS_PCT:.0f}/trade)"
          f" | max_sim global={MAX_SIM} | {LOOKBACK}j")
    print("═"*72)

    configs = {
        "A — ETH-only":  ["ETH-USD"],
        "B — SOL-only":  ["SOL-USD"],
        "C — ETH + SOL": ["ETH-USD", "SOL-USD"],
    }

    results = {}
    for label, symbols in configs.items():
        print(f"\n{'─'*72}")
        print(f"  Config {label}")
        trades, capital = run(symbols)
        s = stats(trades, capital)
        if s:
            results[label] = (s, trades)
            if len(symbols) > 1:
                for sym in symbols:
                    st = [t for t in trades if t["sym"] == sym]
                    wn = len([t for t in st if t["pnl"] > 0])
                    print(f"  {sym}: {len(st)} trades | WR {wn/len(st)*100:.1f}% | P&L {sum(t['pnl'] for t in st):+.2f}$")
        else:
            print(f"  → 0 trades")

    # Tableau comparatif
    print(f"\n{'═'*72}")
    print(f"  {'Config':<20} {'N':>5} {'WR%':>6} {'PF':>5} {'P&L$':>9} {'Ann%':>8} {'MDD%':>6} {'T/j':>5}")
    print(f"  {'─'*20} {'─'*5} {'─'*6} {'─'*5} {'─'*9} {'─'*8} {'─'*6} {'─'*5}")

    for label, (s, _) in results.items():
        flag = " ✓✓" if s["ann"] > 200 else (" ✓" if s["ann"] > 100 else "")
        print(
            f"  {label:<20} {s['n']:>5} {s['wr']:>6.1f} {s['pf']:>5.2f}"
            f"  {s['total']:>+9.2f} {s['ann']:>+7.1f}% {s['mdd']:>+5.1f}%"
            f"  {s['per_day']:>5.1f}{flag}"
        )

    # Breakdown sorties
    print(f"\n  Breakdown sorties :")
    for label, (s, _) in results.items():
        print(
            f"  {label}: Stop {s['stops']}/{s['n']} ({s['stops']/s['n']*100:.0f}%)"
            f" | Target {s['targets']}/{s['n']} ({s['targets']/s['n']*100:.0f}%)"
            f" | Timeout {s['timeouts']}/{s['n']} ({s['timeouts']/s['n']*100:.0f}%)"
        )

    # Analyse comparative
    print(f"\n  ANALYSE COMPARATIVE")
    ra = results.get("A — ETH-only")
    rb = results.get("B — SOL-only")
    rc = results.get("C — ETH + SOL")

    if ra and rc:
        sa, _ = ra; sc, _ = rc
        delta_pnl = sc["total"] - sa["total"]
        delta_mdd = sc["mdd"]   - sa["mdd"]
        print(f"  C vs A (ETH-only) : P&L {delta_pnl:+.2f}$ | MDD {sa['mdd']:.1f}% → {sc['mdd']:.1f}% | N {sa['n']} → {sc['n']}")
    if rb and rc:
        sb, _ = rb; sc, _ = rc
        delta_pnl = sc["total"] - sb["total"]
        print(f"  C vs B (SOL-only) : P&L {delta_pnl:+.2f}$ | N {sb['n']} → {sc['n']}")

    if rc:
        sc, tc = rc
        print(f"\n  CONCLUSION :")
        if sc["pf"] >= 3.0 and sc["ann"] > 150:
            print(f"  → Pool ETH+SOL viable : PF {sc['pf']} | Ann {sc['ann']}%")
            if ra and sc["total"] > ra[0]["total"]:
                print(f"  → Surperforme ETH-only de {sc['total']-ra[0]['total']:+.2f}$ sur {LOOKBACK}j")
            elif ra and sc["total"] <= ra[0]["total"]:
                print(f"  → Sous-performe ETH-only ({sc['total']-ra[0]['total']:+.2f}$) — SOL dilue l'alpha ETH")
        else:
            print(f"  → Pool combiné moins convaincant — rester ETH-only")

    print("═"*72 + "\n")

if __name__ == "__main__":
    main()
