"""
backtest_eth_vs_sol.py — ETH vs SOL, même strat Geo V4, même période
=====================================================================
Données yfinance 60j (limite max pour 5min)
Objectif : voir si SOL a les mêmes propriétés S/R qu'ETH
Métriques : N trades, WR%, PF, Ann%, MDD%, corrélation P&L
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
ASSETS = {
    "ETH-USD": {"target": 0.009, "pos_pct": 0.28, "max_sim": 2},
    "SOL-USD": {"target": 0.009, "pos_pct": 0.28, "max_sim": 2},
}

CAPITAL     = 1000.0
LOOKBACK    = 60
ZONE_PCT    = 0.003
MAX_TOUCHES = 2
RSI_LOW     = 20
RSI_HIGH    = 65

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
def run_backtest(symbol, cfg):
    end            = datetime.now(timezone.utc)
    start          = end - timedelta(days=LOOKBACK + 5)
    start_intraday = end - timedelta(days=58)  # yfinance limite 5m/15m à 60j

    try:
        df_1h  = yf.download(symbol, start=start,          end=end, interval="1h",  auto_adjust=True, progress=False)
        df_15m = yf.download(symbol, start=start_intraday, end=end, interval="15m", auto_adjust=True, progress=False)
        df_5m  = yf.download(symbol, start=start_intraday, end=end, interval="5m",  auto_adjust=True, progress=False)
    except Exception as e:
        print(f"  {symbol} download error: {e}")
        return [], CAPITAL

    for df in [df_1h, df_15m, df_5m]:
        if df.empty:
            print(f"  {symbol}: no data"); return [], CAPITAL
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

        for zk in list(open_pos.keys()):
            p  = open_pos[zk]
            hi = float(next_b["high"]); lo = float(next_b["low"]); cl = float(next_b["close"])
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
                    "t": t_now, "day": t_now.date(),
                    "entry": p["entry"], "exit": ep,
                    "pnl": round(pnl, 4), "reason": er,
                })
                del open_pos[zk]

        if capital < 20: continue
        if len(open_pos) >= cfg["max_sim"]: continue

        bars_1h_p  = df_1h[df_1h.index <= t_now].tail(50)
        bars_15m_p = df_15m[df_15m.index <= t_now].tail(100)
        bars_5m_p  = df_5m[df_5m.index <= t_now].tail(30)
        if len(bars_1h_p) < 10 or len(bars_15m_p) < 20 or len(bars_5m_p) < 10:
            continue

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
            target = round(zone["high"] * (1 + cfg["target"]), 2)
            risk   = abs(zone["center"] - stop)
            reward = abs(target - zone["center"])
            if risk <= 0 or reward / risk < 1.2: continue

            deploy = CAPITAL * cfg["pos_pct"]
            qty    = deploy / zone["center"]

            if float(next_b["low"]) <= zone["high"]:
                fill = min(float(next_b["open"]), zone["center"])
                touches[zk] += 1
                open_pos[zk] = {
                    "entry": fill, "stop": stop, "target": target,
                    "qty": qty, "bar": i + 1,
                }

    # Fermer positions restantes
    if open_pos:
        last = float(df_5m["close"].iloc[-1])
        for zk, p in open_pos.items():
            pnl = (last - p["entry"]) * p["qty"]
            capital += pnl
            trades.append({
                "t": df_5m.index[-1], "day": df_5m.index[-1].date(),
                "entry": p["entry"], "exit": last,
                "pnl": round(pnl, 4), "reason": "end",
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
    avg_w  = sum(t["pnl"] for t in wins)  / len(wins)   if wins   else 0
    avg_l  = sum(t["pnl"] for t in losses)/ len(losses) if losses else 0
    stops  = len([t for t in trades if t["reason"] == "stop"])
    tgts   = len([t for t in trades if t["reason"] == "target"])
    touts  = len([t for t in trades if t["reason"] == "timeout"])
    return {
        "n": n, "wr": round(wr, 1), "pf": round(pf, 2),
        "total": round(total, 2), "ann": round(ann, 1), "mdd": round(mdd, 1),
        "avg_win": round(avg_w, 4), "avg_loss": round(avg_l, 4),
        "stops": stops, "targets": tgts, "timeouts": touts,
    }

# ── CORRÉLATION JOURNALIÈRE ───────────────────────────────────────────────────
def daily_correlation(trades_eth, trades_sol):
    def daily_pnl(trades):
        d = defaultdict(float)
        for t in trades:
            d[t["day"]] += t["pnl"]
        return d

    d_eth = daily_pnl(trades_eth)
    d_sol = daily_pnl(trades_sol)
    common = set(d_eth.keys()) & set(d_sol.keys())
    if len(common) < 5:
        return None

    v_eth = [d_eth[d] for d in sorted(common)]
    v_sol = [d_sol[d] for d in sorted(common)]
    corr  = np.corrcoef(v_eth, v_sol)[0, 1]
    return round(corr, 3)

# ── MAIN ──────────────────────────────────────────────────────────────────────
def run():
    print("\n" + "═"*70)
    print(f"  ETH vs SOL — Geo V4 | {LOOKBACK}j | Capital ${CAPITAL:.0f} | pos_pct 28% | target +0.9%")
    print("═"*70)

    results    = {}
    all_trades = {}

    for symbol, cfg in ASSETS.items():
        print(f"\n  ▶ {symbol}...")
        trades, capital = run_backtest(symbol, cfg)
        s = stats(trades, capital)
        if s:
            results[symbol]    = s
            all_trades[symbol] = trades
        else:
            print(f"    {symbol}: 0 trades")

    # Tableau comparatif
    print(f"\n{'─'*70}")
    print(f"  {'':12} {'N':>5} {'WR%':>6} {'PF':>5} {'P&L$':>8} {'Ann%':>8} {'MDD%':>6} {'AvgW$':>8} {'AvgL$':>8}")
    print(f"  {'─'*12} {'─'*5} {'─'*6} {'─'*5} {'─'*8} {'─'*8} {'─'*6} {'─'*8} {'─'*8}")

    for sym, s in results.items():
        flag = " ✓" if s["pf"] >= 3.0 else (" ~" if s["pf"] >= 1.5 else " ✗")
        print(
            f"  {sym:<12} {s['n']:>5} {s['wr']:>6.1f} {s['pf']:>5.2f}"
            f"  {s['total']:>+8.2f} {s['ann']:>+7.1f}% {s['mdd']:>+5.1f}%"
            f"  {s['avg_win']:>+8.4f} {s['avg_loss']:>+8.4f}{flag}"
        )

    # Breakdown sorties
    print(f"\n  Breakdown sorties :")
    for sym, s in results.items():
        if s["n"] > 0:
            print(
                f"  {sym}: Stop {s['stops']}/{s['n']} ({s['stops']/s['n']*100:.0f}%)"
                f" | Target {s['targets']}/{s['n']} ({s['targets']/s['n']*100:.0f}%)"
                f" | Timeout {s['timeouts']}/{s['n']} ({s['timeouts']/s['n']*100:.0f}%)"
            )

    # Corrélation journalière
    if "ETH-USD" in all_trades and "SOL-USD" in all_trades:
        corr = daily_correlation(all_trades["ETH-USD"], all_trades["SOL-USD"])
        print(f"\n{'─'*70}")
        if corr is not None:
            print(f"  Corrélation P&L journalier ETH/SOL : {corr}")
            if abs(corr) < 0.4:
                print("  → Faible corrélation — diversification réelle, intérêt de trader les deux")
            elif abs(corr) < 0.7:
                print("  → Corrélation modérée — partiellement décorrélés, diversification partielle")
            else:
                print("  → Forte corrélation — ETH et SOL bougent ensemble, peu d'intérêt à diversifier")
        else:
            print("  Corrélation : pas assez de jours communs")

    # Conclusion
    print(f"\n  CONCLUSION")
    eth_s = results.get("ETH-USD")
    sol_s = results.get("SOL-USD")
    if eth_s and sol_s:
        if sol_s["pf"] >= 2.0 and sol_s["ann"] > 50:
            print("  → SOL viable : PF et Ann% suffisants pour envisager l'ajout au bot")
            if sol_s["pf"] >= eth_s["pf"] * 0.8:
                print("  → SOL proche ETH en qualité — candidat sérieux")
        else:
            print("  → SOL moins convaincant qu'ETH — rester ETH-only")
        print(f"  → ETH PF={eth_s['pf']} Ann={eth_s['ann']}% | SOL PF={sol_s['pf']} Ann={sol_s['ann']}%")
    print("═"*70 + "\n")

if __name__ == "__main__":
    run()
