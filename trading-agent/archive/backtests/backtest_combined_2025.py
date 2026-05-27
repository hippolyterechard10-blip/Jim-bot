"""
backtest_combined_2025.py — ETH+SOL pool partagé, H1 2025
==========================================================
Données : yfinance 1h — ETH-USD + SOL-USD
Période : 2025-01-01 → 2025-07-01 (H1 2025, ~6 mois)
Note    : Kraken / Binance géo-bloqués depuis Replit → yfinance 1h
          Timeframes adaptés : 1h entrée / 4h zones / 1d biais
          Timeout 8 bars 1h (≈8h) vs 48 bars 5m (≈4h) en live

Compare :
  A — ETH-only  : max_sim=2, $500/trade
  B — SOL-only  : max_sim=2, $500/trade
  C — ETH+SOL   : pool partagé global max_sim=2, $500/trade
"""
import warnings
warnings.filterwarnings("ignore")
import sys
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from collections import defaultdict

try:
    import yfinance as yf
except ImportError:
    print("pip install yfinance --break-system-packages"); sys.exit(1)

# ── CONFIG ────────────────────────────────────────────────────────────────────
ASSETS      = ["ETH-USD", "SOL-USD"]
START_DATE  = "2025-01-01"
END_DATE    = "2025-07-01"
CAPITAL     = 1000.0
POS_PCT     = 0.50
MAX_SIM     = 2
ZONE_PCT    = 0.003
MAX_TOUCHES = 2
RSI_LOW     = 20
RSI_HIGH    = 65
TARGET_PCT  = 0.009
TIMEOUT_BARS= 8    # 8 barres 1h ≈ 8h (équivalent 48×5m)

# ── DOWNLOAD ──────────────────────────────────────────────────────────────────
def download(symbol):
    """Download yfinance 1h data and resample to 4h / 1d."""
    df1h = yf.download(symbol, start=START_DATE, end=END_DATE,
                       interval="1h", auto_adjust=True, progress=False)
    if df1h.empty: return None
    if isinstance(df1h.columns, pd.MultiIndex):
        df1h.columns = df1h.columns.get_level_values(0)
    df1h.index   = pd.to_datetime(df1h.index, utc=True)
    df1h.columns = [c.lower() for c in df1h.columns]

    df4h = df1h.resample("4h").agg(
        {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
    ).dropna()
    df1d = df1h.resample("1d").agg(
        {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
    ).dropna()

    print(f"  {symbol}: {len(df1h):,} bars 1h ({df1h.index[0].date()} → {df1h.index[-1].date()})")
    return {"1h": df1h, "4h": df4h, "1d": df1d}

# ── STRATÉGIE HELPERS ─────────────────────────────────────────────────────────
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

def get_signal(sym, t_now, dfs):
    """Signal sur timeframes adaptés : 1h entrée / 4h zones / 1d biais."""
    b1d  = dfs["1d"][dfs["1d"].index <= t_now].tail(30)
    b4h  = dfs["4h"][dfs["4h"].index <= t_now].tail(60)
    b1h  = dfs["1h"][dfs["1h"].index <= t_now].tail(30)
    if len(b1d) < 5 or len(b4h) < 15 or len(b1h) < 10: return None

    # Biais daily : pas en tendance baissière
    h1d = b1d["high"].values; l1d = b1d["low"].values
    if h1d[-1] < h1d[-4] and l1d[-1] < l1d[-4]: return None

    cl1h = b1h["close"].values
    vo1h = b1h["volume"].values
    rsi  = _rsi(cl1h, 14)
    avgv = vo1h[-20:].mean() if len(vo1h) >= 20 else vo1h.mean()
    if avgv > 0 and vo1h[-1] < avgv * 0.3: return None

    # Zones sur 4h (≡ 15m dans la version 5m)
    zones = _find_zones(b4h["high"].values, b4h["low"].values, b4h["close"].values)
    curr  = float(b1h["close"].iloc[-1])

    for zone in zones:
        dist = (curr - zone["center"]) / curr
        if not (0.001 <= dist <= 0.020): continue
        if not (RSI_LOW <= rsi <= RSI_HIGH): continue
        div = _rsi_div(cl1h, rsi)
        if not div and not (30 <= rsi <= 55): continue
        # Touch dans les 8 dernières barres 1h
        if not any(b1h["low"].values[-8:] <= zone["high"]): continue
        if cl1h[-1] <= zone["low"]: continue
        stop   = _dyn_stop(b1h["low"].values, zone["center"], zone["wick_low"])
        target = round(zone["high"] * (1 + TARGET_PCT), 2)
        risk   = abs(zone["center"] - stop)
        reward = abs(target - zone["center"])
        if risk <= 0 or reward / risk < 1.2: continue
        return {"zone": zone, "stop": stop, "target": target}
    return None

# ── BACKTEST POOL PARTAGÉ ─────────────────────────────────────────────────────
def run(symbols, all_data):
    days = (datetime.strptime(END_DATE, "%Y-%m-%d") -
            datetime.strptime(START_DATE, "%Y-%m-%d")).days

    # Index commun 1h
    ref_idx = None
    for sym in symbols:
        idx = set(all_data[sym]["1h"].index)
        ref_idx = idx if ref_idx is None else ref_idx & idx
    ref_idx = sorted(ref_idx)
    print(f"  Bars communs 1h : {len(ref_idx):,}")

    capital  = CAPITAL
    trades   = []
    open_pos = {}
    touches  = defaultdict(int)

    for i in range(30, len(ref_idx) - 1):
        t_now  = ref_idx[i]
        t_next = ref_idx[i + 1]

        # Gérer positions ouvertes
        for zk in list(open_pos.keys()):
            p   = open_pos[zk]
            sym = p["sym"]
            df1h= all_data[sym]["1h"]
            if t_next not in df1h.index: continue
            nb = df1h.loc[t_next]
            hi = float(nb["high"]); lo = float(nb["low"]); cl = float(nb["close"])
            sh = lo <= p["stop"]; th = hi >= p["target"]
            to = (i - p["bar"]) >= TIMEOUT_BARS
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
        if len(open_pos) >= MAX_SIM: continue

        deploy = CAPITAL * POS_PCT

        for sym in symbols:
            if len(open_pos) >= MAX_SIM: break

            sig = get_signal(sym, t_now, all_data[sym])
            if sig is None: continue

            zone = sig["zone"]
            key  = _zk(sym, zone["center"])
            if touches[key] >= MAX_TOUCHES: continue
            if key in open_pos: continue

            df1h = all_data[sym]["1h"]
            if t_next not in df1h.index: continue
            nb     = df1h.loc[t_next]
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
        last = float(all_data[sym]["1h"]["close"].iloc[-1])
        pnl  = (last - p["entry"]) * p["qty"]
        capital += pnl
        trades.append({
            "sym": sym, "entry": p["entry"], "exit": last,
            "pnl": round(pnl, 4), "reason": "end",
            "day": all_data[sym]["1h"].index[-1].date(),
        })

    return trades, capital, days

# ── STATS ─────────────────────────────────────────────────────────────────────
def stats(trades, capital, days):
    n = len(trades)
    if n == 0: return None
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total  = sum(t["pnl"] for t in trades)
    wr     = len(wins) / n * 100
    sum_l  = sum(t["pnl"] for t in losses)
    pf     = abs(sum(t["pnl"] for t in wins) / sum_l) if sum_l else 99.0
    ann    = (capital - CAPITAL) / CAPITAL / days * 365 * 100
    eq     = np.array([CAPITAL] + [CAPITAL + sum(t["pnl"] for t in trades[:k+1]) for k in range(n)])
    pk     = np.maximum.accumulate(eq)
    mdd    = ((eq - pk) / pk * 100).min()
    stops  = len([t for t in trades if t["reason"] == "stop"])
    tgts   = len([t for t in trades if t["reason"] == "target"])
    touts  = len([t for t in trades if t["reason"] == "timeout"])
    avg_w  = sum(t["pnl"] for t in wins)  / len(wins)   if wins   else 0
    avg_l  = sum(t["pnl"] for t in losses)/ len(losses) if losses else 0
    per_day= n / days
    return {
        "n": n, "wr": round(wr, 1), "pf": round(pf, 2),
        "total": round(total, 2), "ann": round(ann, 1), "mdd": round(mdd, 1),
        "stops": stops, "targets": tgts, "timeouts": touts,
        "avg_win": round(avg_w, 4), "avg_loss": round(avg_l, 4),
        "per_day": round(per_day, 1),
    }

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print("\n" + "═"*72)
    print(f"  H1 2025 — ETH-only vs SOL-only vs ETH+SOL (yfinance 1h)")
    print(f"  {START_DATE} → {END_DATE} | Capital ${CAPITAL:.0f}"
          f" | pos_pct {POS_PCT*100:.0f}% | max_sim global={MAX_SIM}")
    print(f"  Timeframes : 1h entrée / 4h zones / 1d biais | timeout {TIMEOUT_BARS}h")
    print("═"*72)

    print("\n  ── Téléchargement yfinance 1h ──")
    all_data = {}
    for sym in ASSETS:
        d = download(sym)
        if d is None:
            print(f"  {sym}: données vides"); return
        all_data[sym] = d

    configs = {
        "A — ETH-only":  ["ETH-USD"],
        "B — SOL-only":  ["SOL-USD"],
        "C — ETH + SOL": ["ETH-USD", "SOL-USD"],
    }

    results = {}
    for label, symbols in configs.items():
        print(f"\n{'─'*72}")
        print(f"  Config {label}")
        trades, capital, days = run(symbols, all_data)
        s = stats(trades, capital, days)
        if s:
            results[label] = (s, trades)
            if len(symbols) > 1:
                for sym in symbols:
                    st = [t for t in trades if t["sym"] == sym]
                    if st:
                        wn = len([t for t in st if t["pnl"] > 0])
                        pl = sum(t["pnl"] for t in st)
                        print(f"  {sym}: {len(st)} trades | WR {wn/len(st)*100:.1f}% | P&L {pl:+.2f}$")
        else:
            print(f"  → 0 trades")

    # Tableau comparatif
    print(f"\n{'═'*72}")
    print(f"  {'Config':<20} {'N':>5} {'WR%':>6} {'PF':>5} {'P&L$':>9} {'Ann%':>8} {'MDD%':>6} {'T/j':>5}")
    print(f"  {'─'*20} {'─'*5} {'─'*6} {'─'*5} {'─'*9} {'─'*8} {'─'*6} {'─'*5}")

    for label, (s, _) in results.items():
        flag = " ✓✓" if s["ann"] > 100 else (" ✓" if s["ann"] > 40 else "")
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

    # Avg win/loss
    print(f"\n  Avg Win / Avg Loss :")
    for label, (s, _) in results.items():
        ratio = abs(s["avg_win"] / s["avg_loss"]) if s["avg_loss"] != 0 else 0
        print(f"  {label}: AvgW ${s['avg_win']:+.4f} | AvgL ${s['avg_loss']:+.4f} | Ratio {ratio:.2f}x")

    # Analyse comparative
    print(f"\n  ANALYSE COMPARATIVE")
    ra = results.get("A — ETH-only")
    rb = results.get("B — SOL-only")
    rc = results.get("C — ETH + SOL")

    if ra and rc:
        sa, _ = ra; sc, _ = rc
        print(f"  C vs A (ETH-only) : P&L {sc['total']-sa['total']:+.2f}$ | MDD {sa['mdd']:.1f}% → {sc['mdd']:.1f}% | N {sa['n']} → {sc['n']}")
    if rb and rc:
        sb, _ = rb; sc, _ = rc
        print(f"  C vs B (SOL-only) : P&L {sc['total']-sb['total']:+.2f}$ | N {sb['n']} → {sc['n']}")

    if rc:
        sc, _ = rc
        print(f"\n  CONCLUSION H1 2025 :")
        if sc["pf"] >= 2.0:
            print(f"  → Pool ETH+SOL valide sur 6 mois réels : PF {sc['pf']} | Ann {sc['ann']}% | MDD {sc['mdd']}%")
            if ra and sc["total"] > ra[0]["total"]:
                print(f"  → Surperforme ETH-only de {sc['total']-ra[0]['total']:+.2f}$ — diversification payante")
            else:
                print(f"  → Sous-performe ETH-only ({sc['total']-ra[0]['total']:+.2f}$) — SOL dilue l'alpha ETH sur cette période")
        else:
            print(f"  → H1 2025 : résultats insuffisants (PF {sc['pf']}) — rester ETH-only")

    print("═"*72 + "\n")

if __name__ == "__main__":
    main()
