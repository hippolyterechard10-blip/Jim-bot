"""
backtest_historical_daily.py — ETH+SOL sur 3 régimes 2022-2024
===============================================================
Note : 5m / 1h non disponibles pour 2022-2024 depuis Replit
       (yfinance limite 1h à 730j, Binance géo-bloqué)
       → Simulation DAILY BARS avec logique Geo V4 adaptée :
          1d  = barre d'entrée          (≡ 5min en live)
          1d  = zones sur tail(40)      (≡ 15min sur tail(100))
          ZONE_PCT = 1.5%               (≡ 0.3% sur 5min; ratio ×5 pour vol daily)
          dist zone : [0.3% – 5%]       (≡ [0.1% – 2%] sur 5min)
          timeout = 5 barres            (≡ 48 barres 5min)
          3 mois warmup avant chaque fenêtre de test

Régimes testés (fenêtre de 2 mois chacune) :
  2022 Bearish : Mai–Juin 2022    (crash Terra/LUNA, ETH -70%)
  2023 Regain  : Jan–Fév 2023    (rebond post-FTX, ETH +60%)
  2024 Bullish : Mar–Avr 2024    (bull market, BTC ATH, ETH +40%)

Configs par régime :
  A — ETH-only  | B — SOL-only  | C — ETH+SOL pool partagé
"""
import warnings
warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from collections import defaultdict

# ── CONFIG ────────────────────────────────────────────────────────────────────
ASSETS       = ["ETH-USD", "SOL-USD"]
CAPITAL      = 1000.0
POS_PCT      = 0.50
MAX_SIM      = 2
ZONE_PCT     = 0.015   # 1.5% pour daily (vs 0.3% en 5min)
MAX_TOUCHES  = 2
RSI_LOW      = 20
RSI_HIGH     = 65
TARGET_PCT   = 0.012   # 1.2% target daily (vs 0.9% en 5min)
TIMEOUT_BARS = 5       # 5 jours
WARMUP_DAYS  = 90      # 3 mois de warmup pour les zones

# Périodes : (label, test_start, test_end)
PERIODS = [
    ("2022 Bearish  (Mai–Juin)",   "2022-05-01", "2022-07-01"),
    ("2023 Regain   (Jan–Fév)",    "2023-01-01", "2023-03-01"),
    ("2024 Bullish  (Mar–Avr)",    "2024-03-01", "2024-05-01"),
]

# ── DOWNLOAD ──────────────────────────────────────────────────────────────────
def download(symbol, test_start, test_end):
    """Télécharge avec 3 mois de warmup avant le test."""
    warmup_start = (datetime.strptime(test_start, "%Y-%m-%d")
                    - timedelta(days=WARMUP_DAYS)).strftime("%Y-%m-%d")
    df = yf.download(symbol, start=warmup_start, end=test_end,
                     interval="1d", auto_adjust=True, progress=False)
    if df.empty: return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index   = pd.to_datetime(df.index, utc=True)
    df.columns = [c.lower() for c in df.columns]
    return df

# ── HELPERS ───────────────────────────────────────────────────────────────────
def _rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    d  = np.diff(np.array(closes, dtype=float))
    g  = np.where(d > 0, d, 0.0)
    l  = np.where(d < 0, -d, 0.0)
    ag = g[-period:].mean(); al = l[-period:].mean()
    if al == 0: return 100.0
    return round(100 - 100 / (1 + ag / al), 2)

def _find_zones(highs, lows, closes, zone_pct):
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
        if (v[0] - clusters[-1][0][0]) / clusters[-1][0][0] < zone_pct * 2:
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
                "high":     center * (1 + zone_pct),
                "low":      center * (1 - zone_pct),
                "wick_low": wick_low,
                "tests":    len(c),
            })
    zones.sort(key=lambda x: x["center"], reverse=True)
    return zones

def _rsi_div(closes, rsi_now):
    if len(closes) < 5: return False
    rsi_prev = _rsi(np.array(closes[:-3]), 14)
    return closes[-1] < closes[-4] and rsi_now > rsi_prev

def _dyn_stop(lows, entry, wick_low, zone_pct):
    floor = entry * (1 - zone_pct * 2)
    c = min(lows[-5:]) * 0.998 if len(lows) >= 5 else wick_low * 0.998
    if floor <= c < entry: return c
    z = wick_low * 0.998
    if floor <= z < entry: return z
    return entry * (1 - zone_pct)

def _zk(sym, center, zone_pct):
    return f"{sym}_{round(center / (center * zone_pct))}"

def get_signal(sym, t_now, df, zone_pct, target_pct):
    bars = df[df.index <= t_now].tail(50)
    if len(bars) < 20: return None

    # Biais : pas en tendance baissière franche
    h = bars["high"].values; l = bars["low"].values
    if h[-1] < h[-5] and l[-1] < l[-5]: return None

    cl = bars["close"].values
    rsi = _rsi(cl, 14)
    if not (RSI_LOW <= rsi <= RSI_HIGH): return None

    # Volume filter relaxé pour daily
    vo  = bars["volume"].values
    avg = vo[-15:].mean() if len(vo) >= 15 else vo.mean()
    if avg > 0 and vo[-1] < avg * 0.15: return None

    zones = _find_zones(bars["high"].values, bars["low"].values, cl, zone_pct)
    curr  = float(bars["close"].iloc[-1])

    for zone in zones:
        dist = (curr - zone["center"]) / curr
        if not (0.003 <= dist <= 0.05): continue
        div = _rsi_div(cl, rsi)
        if not div and not (30 <= rsi <= 58): continue
        # Touch dans les 5 dernières barres
        if not any(bars["low"].values[-5:] <= zone["high"]): continue
        if cl[-1] <= zone["low"]: continue
        stop   = _dyn_stop(bars["low"].values, zone["center"], zone["wick_low"], zone_pct)
        target = round(zone["high"] * (1 + target_pct), 2)
        risk   = abs(zone["center"] - stop)
        reward = abs(target - zone["center"])
        if risk <= 0 or reward / risk < 1.0: continue
        return {"zone": zone, "stop": stop, "target": target}
    return None

# ── BACKTEST POOL ─────────────────────────────────────────────────────────────
def run(symbols, all_dfs, test_start_ts, zone_pct, target_pct):
    # Index commun, en commençant depuis le début du warmup
    ref_idx = None
    for sym in symbols:
        idx = set(all_dfs[sym].index)
        ref_idx = idx if ref_idx is None else ref_idx & idx
    ref_idx = sorted(ref_idx)

    capital  = CAPITAL
    trades   = []
    open_pos = {}
    touches  = defaultdict(int)

    for i in range(20, len(ref_idx) - 1):
        t_now  = ref_idx[i]
        t_next = ref_idx[i + 1]

        # Gérer positions ouvertes
        for zk in list(open_pos.keys()):
            p   = open_pos[zk]
            sym = p["sym"]
            df  = all_dfs[sym]
            if t_next not in df.index: continue
            nb = df.loc[t_next]
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
                    "pnl": round(pnl, 4), "reason": er,
                    "day": t_now.date(),
                    "in_test": t_now >= test_start_ts,
                })
                del open_pos[zk]

        if capital < 20: continue
        if len(open_pos) >= MAX_SIM: continue
        # N'ouvrir que dans la fenêtre de test (pas pendant le warmup)
        if t_now < test_start_ts: continue

        for sym in symbols:
            if len(open_pos) >= MAX_SIM: break
            sig = get_signal(sym, t_now, all_dfs[sym], zone_pct, target_pct)
            if sig is None: continue
            zone = sig["zone"]
            key  = _zk(sym, zone["center"], zone_pct)
            if touches[key] >= MAX_TOUCHES: continue
            if key in open_pos: continue
            df   = all_dfs[sym]
            if t_next not in df.index: continue
            nb   = df.loc[t_next]
            if float(nb["low"]) <= zone["high"]:
                fill = min(float(nb["open"]), zone["center"])
                qty  = (CAPITAL * POS_PCT) / fill
                touches[key] += 1
                open_pos[key] = {
                    "sym": sym, "entry": fill,
                    "stop": sig["stop"], "target": sig["target"],
                    "qty": qty, "bar": i,
                }

    # Fermer restants
    for key, p in open_pos.items():
        sym  = p["sym"]
        last = float(all_dfs[sym]["close"].iloc[-1])
        pnl  = (last - p["entry"]) * p["qty"]
        capital += pnl
        trades.append({
            "sym": sym, "entry": p["entry"], "exit": last,
            "pnl": round(pnl, 4), "reason": "end",
            "day": all_dfs[sym].index[-1].date(), "in_test": True,
        })

    return trades, capital

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
    ann    = (capital - CAPITAL) / CAPITAL / max(days, 1) * 365 * 100
    eq     = np.array([CAPITAL] + [CAPITAL + sum(t["pnl"] for t in trades[:k+1]) for k in range(n)])
    pk     = np.maximum.accumulate(eq)
    mdd    = ((eq - pk) / pk * 100).min()
    stops  = len([t for t in trades if t["reason"] == "stop"])
    tgts   = len([t for t in trades if t["reason"] == "target"])
    touts  = len([t for t in trades if t["reason"] == "timeout"])
    return {
        "n": n, "wr": round(wr, 1), "pf": round(pf, 2),
        "total": round(total, 2), "ann": round(ann, 1), "mdd": round(mdd, 1),
        "stops": stops, "targets": tgts, "timeouts": touts,
    }

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print("\n" + "═"*78)
    print("  Geo V4 — 3 régimes historiques | DAILY BARS + 90j warmup")
    print("  ETH-only / SOL-only / ETH+SOL pool | $1000 | 50% pos | zone±1.5% | target+1.2%")
    print("  [5m/1h non dispo pour 2022-2024 → daily bars, même logique S/R géométrique]")
    print("═"*78)

    configs = {
        "ETH-only":  ["ETH-USD"],
        "SOL-only":  ["SOL-USD"],
        "ETH+SOL":   ["ETH-USD", "SOL-USD"],
    }

    print(f"\n  {'Régime':<28} {'Config':<12} {'N':>4} {'WR%':>6} {'PF':>5}"
          f" {'P&L$':>8} {'Ann%':>8} {'MDD%':>6} {'T|S|TO'}")
    print(f"  {'─'*28} {'─'*12} {'─'*4} {'─'*6} {'─'*5}"
          f" {'─'*8} {'─'*8} {'─'*6} {'─'*12}")

    summary = {}

    for period_label, test_start, test_end in PERIODS:
        dt_start = datetime.strptime(test_start, "%Y-%m-%d")
        dt_end   = datetime.strptime(test_end,   "%Y-%m-%d")
        days     = (dt_end - dt_start).days
        test_start_ts = pd.Timestamp(test_start, tz="UTC")

        # Télécharger (avec warmup)
        all_dfs = {}
        for sym in ASSETS:
            df = download(sym, test_start, test_end)
            if df is not None:
                all_dfs[sym] = df

        period_results = {}
        for cfg_label, symbols in configs.items():
            avail = [s for s in symbols if s in all_dfs]
            if len(avail) < len(symbols):
                print(f"  {period_label:<28} {cfg_label:<12} données manquantes"); continue

            trades, capital = run(avail, all_dfs, test_start_ts, ZONE_PCT, TARGET_PCT)
            s = stats(trades, capital, days)
            period_results[cfg_label] = s

            if s:
                exits = f"{s['targets']}|{s['stops']}|{s['timeouts']}"
                flag = " ✓" if s["pf"] >= 1.5 and s["total"] >= 0 else (" ~" if s["total"] >= 0 else " ✗")
                print(
                    f"  {period_label:<28} {cfg_label:<12} {s['n']:>4}"
                    f" {s['wr']:>6.1f} {s['pf']:>5.2f} {s['total']:>+8.2f}"
                    f" {s['ann']:>+7.1f}% {s['mdd']:>+5.1f}% {exits:<12}{flag}"
                )
            else:
                print(f"  {period_label:<28} {cfg_label:<12}    0  — — — — — —")

        summary[period_label] = period_results
        print()

    # Récap ETH+SOL
    print(f"  {'═'*78}")
    print("  RÉCAP ETH+SOL POOL — 3 régimes")
    print(f"  {'─'*78}")
    total_pnl   = 0
    valid_count = 0
    profitable  = 0
    for period_label, _, __ in PERIODS:
        results = summary.get(period_label, {})
        s = results.get("ETH+SOL")
        if s and s["n"] > 0:
            icon = "✓" if s["total"] >= 0 else "✗"
            print(f"  [{icon}] {period_label:<30} N={s['n']:>2} WR={s['wr']:.0f}%"
                  f" PF={s['pf']:.2f} P&L={s['total']:>+8.2f}$ Ann={s['ann']:>+6.1f}% MDD={s['mdd']:.1f}%")
            total_pnl += s["total"]
            valid_count += 1
            if s["total"] >= 0: profitable += 1
        else:
            print(f"  [?] {period_label:<30} 0 trades")

    print(f"\n  P&L cumulé 3 régimes : {total_pnl:+.2f}$")
    print(f"  Régimes profitables  : {profitable}/{valid_count}")
    print("═"*78 + "\n")

if __name__ == "__main__":
    main()
