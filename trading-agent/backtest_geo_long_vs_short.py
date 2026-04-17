"""
backtest_geo_long_vs_short.py — A/B GEO LONG vs LONG+SHORT
==============================================================================
Compare la stratégie GEO actuelle (long-only) vs sa version symétrique qui
trade aussi les résistances en short. Données Binance US 5min déjà cachées
dans binance_us_cache/ (ETH + SOL, 2022-2026).

Toutes les règles GEO V4 sont conservées :
  - Zones ±0.3 % sur pivots 15min
  - RSI filter [20-65] (long) / [35-80] (short)
  - RSI divergence (haussière / baissière)
  - Stop dynamique sous wick (long) / au-dessus wick (short)
  - Time-stop 4h (48 bougies 5min)
  - Target +0.9 % (long) / -0.9 % (short)
  - R:R min 1.2, max 2 positions simultanées, fees 0.05 % par side
"""
import glob
import os
import warnings
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ── CONFIG ────────────────────────────────────────────────────────────────────
CAPITAL      = 1000.0
POS_PCT      = 0.50    # 50 % du capital par trade
MAX_SIM      = 2       # max positions simultanées
ZONE_PCT     = 0.003
TARGET_PCT   = 0.009
MAX_TOUCHES  = 2
RSI_LOW_L    = 20
RSI_HIGH_L   = 65
RSI_LOW_S    = 35
RSI_HIGH_S   = 80
TIMEOUT_BARS = 48      # 4h en bougies 5min
FEE_PCT      = 0.0005  # 0.05 % maker Kraken Futures (side)
RR_MIN       = 1.2

CACHE_DIR = "binance_us_cache"


# ── DATA LOADER ───────────────────────────────────────────────────────────────

def load_symbol(prefix: str) -> pd.DataFrame:
    """Concatène tous les parquets disponibles pour un symbole, dédoublonne, trie."""
    files = sorted(glob.glob(os.path.join(CACHE_DIR, f"{prefix}_5m_*.parquet")))
    if not files:
        raise FileNotFoundError(f"Aucune donnée pour {prefix} dans {CACHE_DIR}")
    dfs = [pd.read_parquet(f) for f in files]
    df = pd.concat(dfs)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    # uniformise colonnes
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    return df


def resample_ohlcv(df_5m: pd.DataFrame, rule: str) -> pd.DataFrame:
    return df_5m.resample(rule).agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    }).dropna()


# ── HELPERS STRATÉGIE ─────────────────────────────────────────────────────────

def _rsi(closes: np.ndarray, period: int = 14) -> float:
    arr = np.array(closes, dtype=float)
    if len(arr) < period + 1:
        return 50.0
    d  = np.diff(arr)
    g  = np.where(d > 0, d, 0.0)
    l  = np.where(d < 0, -d, 0.0)
    ag = g[-period:].mean()
    al = l[-period:].mean()
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + ag / al), 2)


def _find_support_zones(highs, lows, closes, min_tests=1):
    current = closes[-1]
    sw = []
    for i in range(2, len(highs) - 2):
        if (lows[i] < lows[i-1] and lows[i] < lows[i-2]
                and lows[i] < lows[i+1] and lows[i] < lows[i+2]):
            sw.append((lows[i], highs[i]))
    if not sw:
        return []
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
        if center < current * 0.999 and len(c) >= min_tests:
            zones.append({
                "center":   center,
                "high":     center * (1 + ZONE_PCT),
                "low":      center * (1 - ZONE_PCT),
                "wick":     wick_low,
                "tests":    len(c),
            })
    zones.sort(key=lambda x: x["center"], reverse=True)
    return zones


def _find_resistance_zones(highs, lows, closes, min_tests=1):
    """Miroir de _find_support_zones : clusterise les swing HIGHS."""
    current = closes[-1]
    sw = []
    for i in range(2, len(highs) - 2):
        if (highs[i] > highs[i-1] and highs[i] > highs[i-2]
                and highs[i] > highs[i+1] and highs[i] > highs[i+2]):
            sw.append((highs[i], lows[i]))
    if not sw:
        return []
    sw.sort(key=lambda x: x[0])
    clusters = [[sw[0]]]
    for v in sw[1:]:
        if (v[0] - clusters[-1][0][0]) / clusters[-1][0][0] < ZONE_PCT * 2:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    zones = []
    for c in clusters:
        center    = sum(x[0] for x in c) / len(c)
        wick_high = max(x[0] for x in c)
        if center > current * 1.001 and len(c) >= min_tests:
            zones.append({
                "center":   center,
                "high":     center * (1 + ZONE_PCT),
                "low":      center * (1 - ZONE_PCT),
                "wick":     wick_high,
                "tests":    len(c),
            })
    # plus proche en premier (au-dessus du prix)
    zones.sort(key=lambda x: x["center"])
    return zones


def _rsi_bull_div(closes, rsi_now):
    if len(closes) < 5:
        return False
    rsi_prev = _rsi(np.array(closes[:-3]), 14)
    return closes[-1] < closes[-4] and rsi_now > rsi_prev


def _rsi_bear_div(closes, rsi_now):
    if len(closes) < 5:
        return False
    rsi_prev = _rsi(np.array(closes[:-3]), 14)
    return closes[-1] > closes[-4] and rsi_now < rsi_prev


def _dynamic_stop_long(lows_5m, entry, wick_low):
    floor = entry * 0.992
    cand  = min(lows_5m[-8:]) * 0.999 if len(lows_5m) >= 8 else wick_low * 0.999
    if floor <= cand < entry:
        return cand
    zs = wick_low * 0.999
    if floor <= zs < entry:
        return zs
    return entry * 0.997


def _dynamic_stop_short(highs_5m, entry, wick_high):
    ceiling = entry * 1.008
    cand    = max(highs_5m[-8:]) * 1.001 if len(highs_5m) >= 8 else wick_high * 1.001
    if entry < cand <= ceiling:
        return cand
    zs = wick_high * 1.001
    if entry < zs <= ceiling:
        return zs
    return entry * 1.003


def _bias_1h(highs, lows):
    if len(highs) < 5:
        return "range"
    hh = highs[-1] > highs[-4]
    hl = lows[-1]  > lows[-4]
    lh = highs[-1] < highs[-4]
    ll = lows[-1]  < lows[-4]
    if hh and hl: return "uptrend"
    if lh and ll: return "downtrend"
    return "range"


def _zone_key(center):
    mag = max(1, int(round(-np.log10(center * 0.001))))
    return round(center, mag)


# ── BACKTEST ─────────────────────────────────────────────────────────────────

def run_backtest(df_5m, df_15m, df_1h, enable_long=True, enable_short=False):
    capital  = CAPITAL
    trades   = []
    open_pos = {}            # zk → {side, entry, stop, target, qty, bar}
    touches  = defaultdict(int)

    idx = df_5m.index
    n   = len(idx)

    for i in range(60, n - 1):
        t_now   = idx[i]
        next_b  = df_5m.iloc[i + 1]
        hi      = float(next_b["high"])
        lo      = float(next_b["low"])
        cls     = float(next_b["close"])

        # ── Gestion sorties ───────────────────────────────────────────────
        for zk in list(open_pos.keys()):
            pos = open_pos[zk]
            ep = er = None
            if pos["side"] == "long":
                if lo <= pos["stop"]:
                    ep, er = pos["stop"], "stop"
                elif hi >= pos["target"]:
                    ep, er = pos["target"], "target"
                elif (i - pos["bar"]) >= TIMEOUT_BARS:
                    ep, er = cls, "timeout"
            else:  # short
                if hi >= pos["stop"]:
                    ep, er = pos["stop"], "stop"
                elif lo <= pos["target"]:
                    ep, er = pos["target"], "target"
                elif (i - pos["bar"]) >= TIMEOUT_BARS:
                    ep, er = cls, "timeout"

            if ep is not None:
                if pos["side"] == "long":
                    gross = (ep - pos["entry"]) * pos["qty"]
                else:
                    gross = (pos["entry"] - ep) * pos["qty"]
                fees = (pos["entry"] + ep) * pos["qty"] * FEE_PCT
                pnl  = gross - fees
                capital += pnl
                trades.append({
                    "t": t_now, "side": pos["side"],
                    "entry": pos["entry"], "exit": ep,
                    "pnl": round(pnl, 4), "reason": er,
                })
                del open_pos[zk]

        if capital < 20 or len(open_pos) >= MAX_SIM:
            continue

        # ── Données fenêtrées ─────────────────────────────────────────────
        b1h  = df_1h[df_1h.index   <= t_now].tail(50)
        b15  = df_15m[df_15m.index <= t_now].tail(100)
        b5   = df_5m[df_5m.index   <= t_now].tail(30)
        if len(b1h) < 10 or len(b15) < 20 or len(b5) < 15:
            continue

        current   = float(df_5m.iloc[i]["close"])
        closes_5m = b5["close"].values
        highs_5m  = b5["high"].values
        lows_5m   = b5["low"].values
        rsi_now   = _rsi(closes_5m, 14)
        bias      = _bias_1h(b1h["high"].values, b1h["low"].values)

        # volume filter
        vols_5m = b5["volume"].values
        avg_v   = vols_5m[-20:].mean() if len(vols_5m) >= 20 else vols_5m.mean()
        if avg_v > 0 and vols_5m[-1] < avg_v * 0.3:
            continue

        # ── LONG (support bounce) ─────────────────────────────────────────
        if enable_long and bias != "downtrend":
            for zone in _find_support_zones(b15["high"].values, b15["low"].values,
                                             b15["close"].values, 1):
                zk_l = ("L", _zone_key(zone["center"]))
                if zk_l in open_pos or touches[zk_l] >= MAX_TOUCHES:
                    continue
                dist = (current - zone["center"]) / current
                if not (0.001 <= dist <= 0.020):
                    continue
                if not (RSI_LOW_L <= rsi_now <= RSI_HIGH_L):
                    continue
                div = _rsi_bull_div(closes_5m, rsi_now)
                if not div and not (30 <= rsi_now <= 55):
                    continue
                touched   = any(lows_5m[-8:] <= zone["high"])
                above_low = closes_5m[-1] > zone["low"]
                if not (touched and above_low):
                    continue
                stop   = _dynamic_stop_long(lows_5m, zone["center"], zone["wick"])
                target = zone["center"] * (1 + TARGET_PCT)
                risk   = abs(zone["center"] - stop)
                reward = abs(target - zone["center"])
                if risk <= 0 or reward / risk < RR_MIN:
                    continue
                if lo <= zone["high"]:
                    fill = min(float(next_b["open"]), zone["center"])
                    qty  = (capital * POS_PCT) / fill
                    touches[zk_l] += 1
                    open_pos[zk_l] = {
                        "side": "long", "entry": fill,
                        "stop": stop, "target": target,
                        "qty": qty, "bar": i + 1,
                    }
                    if len(open_pos) >= MAX_SIM:
                        break

        # ── SHORT (resistance rejection) ──────────────────────────────────
        if enable_short and bias != "uptrend" and len(open_pos) < MAX_SIM:
            for zone in _find_resistance_zones(b15["high"].values, b15["low"].values,
                                                b15["close"].values, 1):
                zk_s = ("S", _zone_key(zone["center"]))
                if zk_s in open_pos or touches[zk_s] >= MAX_TOUCHES:
                    continue
                dist = (zone["center"] - current) / current
                if not (0.001 <= dist <= 0.020):
                    continue
                if not (RSI_LOW_S <= rsi_now <= RSI_HIGH_S):
                    continue
                div = _rsi_bear_div(closes_5m, rsi_now)
                if not div and not (45 <= rsi_now <= 70):
                    continue
                touched   = any(highs_5m[-8:] >= zone["low"])
                below_hi  = closes_5m[-1] < zone["high"]
                if not (touched and below_hi):
                    continue
                stop   = _dynamic_stop_short(highs_5m, zone["center"], zone["wick"])
                target = zone["center"] * (1 - TARGET_PCT)
                risk   = abs(stop - zone["center"])
                reward = abs(zone["center"] - target)
                if risk <= 0 or reward / risk < RR_MIN:
                    continue
                if hi >= zone["low"]:
                    fill = max(float(next_b["open"]), zone["center"])
                    qty  = (capital * POS_PCT) / fill
                    touches[zk_s] += 1
                    open_pos[zk_s] = {
                        "side": "short", "entry": fill,
                        "stop": stop, "target": target,
                        "qty": qty, "bar": i + 1,
                    }
                    if len(open_pos) >= MAX_SIM:
                        break

    # ferme tout en fin de série
    last = float(df_5m.iloc[-1]["close"])
    for zk, pos in open_pos.items():
        if pos["side"] == "long":
            gross = (last - pos["entry"]) * pos["qty"]
        else:
            gross = (pos["entry"] - last) * pos["qty"]
        fees = (pos["entry"] + last) * pos["qty"] * FEE_PCT
        pnl  = gross - fees
        capital += pnl
        trades.append({
            "t": df_5m.index[-1], "side": pos["side"],
            "entry": pos["entry"], "exit": last,
            "pnl": round(pnl, 4), "reason": "end",
        })

    return trades, capital


# ── REPORTING ─────────────────────────────────────────────────────────────────

def stats(trades, capital, days):
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0, "pf": 0, "ret": 0, "ann": 0, "mdd": 0, "avg": 0}
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total  = sum(t["pnl"] for t in trades)
    wr     = len(wins) / n * 100
    sl     = sum(t["pnl"] for t in losses)
    pf     = abs(sum(t["pnl"] for t in wins) / sl) if sl else 99.0
    ret    = (capital - CAPITAL) / CAPITAL * 100
    ann    = ret / max(1, days) * 365
    eq     = np.array([CAPITAL] + [CAPITAL + sum(t["pnl"] for t in trades[:k+1])
                                    for k in range(n)])
    pk     = np.maximum.accumulate(eq)
    mdd    = ((eq - pk) / pk * 100).min()
    return {
        "n": n, "wr": round(wr, 1), "pf": round(pf, 2),
        "ret": round(ret, 1), "ann": round(ann, 1),
        "mdd": round(mdd, 1), "avg": round(total / n, 2),
    }


def print_table(title, results):
    print(f"\n{'─'*78}")
    print(f"  {title}")
    print(f"{'─'*78}")
    print(f"  {'Symbole':<10} {'Mode':<14} {'N':>5} {'WR%':>6} {'PF':>5} "
          f"{'Ret%':>7} {'Ann%':>7} {'MDD%':>7} {'avg$':>7}")
    print("  " + "─" * 76)
    for (sym, mode), s in results.items():
        print(
            f"  {sym:<10} {mode:<14} {s['n']:>5} {s['wr']:>6.1f} "
            f"{s['pf']:>5.2f} {s['ret']:>+7.1f} {s['ann']:>+7.1f} "
            f"{s['mdd']:>+7.1f} {s['avg']:>+7.2f}"
        )


def main():
    print("═" * 78)
    print("  GEO — Backtest A/B : LONG vs LONG+SHORT vs SHORT")
    print(f"  Fees {FEE_PCT*100:.2f}%/side | R:R≥{RR_MIN} | max {MAX_SIM} pos | "
          f"target {TARGET_PCT*100:.1f}%")
    print("═" * 78)

    symbols = [("ETHUSD", "ETH"), ("SOLUSD", "SOL")]
    results = {}

    for prefix, label in symbols:
        print(f"\n  {label} — chargement des données…")
        df_5m = load_symbol(prefix)
        df_5m = df_5m[df_5m.index >= "2022-01-01"]  # horizon analyse
        df_5m = df_5m[df_5m.index <  "2026-01-01"]
        df_15 = resample_ohlcv(df_5m, "15min")
        df_1h = resample_ohlcv(df_5m, "1h")
        days  = max(1, (df_5m.index[-1] - df_5m.index[0]).days)
        print(f"    {len(df_5m):,} bars 5m | {df_5m.index[0].date()} → "
              f"{df_5m.index[-1].date()} ({days}j)")

        for mode, el, es in [
            ("LONG-only",    True,  False),
            ("SHORT-only",   False, True),
            ("LONG+SHORT",   True,  True),
        ]:
            print(f"    → {mode}…", end=" ", flush=True)
            trades, capital = run_backtest(df_5m, df_15, df_1h,
                                           enable_long=el, enable_short=es)
            s = stats(trades, capital, days)
            results[(label, mode)] = s
            print(f"{s['n']} trades, ret {s['ret']:+.1f}%, ann {s['ann']:+.1f}%")

    print_table("Résumé tous symboles / modes", results)

    # verdict
    print(f"\n{'═'*78}")
    print("  VERDICT")
    print(f"{'═'*78}")
    for (sym, mode), s in results.items():
        if mode == "LONG-only":
            long_ann = s["ann"]
            combo    = results.get((sym, "LONG+SHORT"))
            if combo:
                delta = combo["ann"] - long_ann
                tag   = "✅ SHORT améliore" if delta > 2 else (
                        "➖ SHORT neutre" if delta > -2 else "🔴 SHORT dégrade")
                print(f"  {sym}: LONG={long_ann:+.1f}%/an | "
                      f"LONG+SHORT={combo['ann']:+.1f}%/an | "
                      f"Δ={delta:+.1f}pts → {tag}")

    # CSV
    out = "backtest_long_vs_short.csv"
    rows = [{"symbol": sym, "mode": mode, **s} for (sym, mode), s in results.items()]
    pd.DataFrame(rows).to_csv(out, index=False)
    print(f"\n  CSV → {out}")


if __name__ == "__main__":
    main()
