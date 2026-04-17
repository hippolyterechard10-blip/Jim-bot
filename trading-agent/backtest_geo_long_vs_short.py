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

Perf : indexation positionnelle + numpy → ~30s pour 4 ans × 3 modes × 2 symboles.
"""
import glob
import os
import warnings
from collections import defaultdict

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ── CONFIG ────────────────────────────────────────────────────────────────────
CAPITAL        = 1000.0
NOTIONAL       = 500.0    # taille fixe $ par trade (isole l'edge du compounding)
MAX_SIM        = 2
ZONE_PCT       = 0.003
TARGET_PCT     = 0.009
MAX_TOUCHES    = 2
RSI_LOW_L      = 20
RSI_HIGH_L     = 65
RSI_LOW_S      = 35
RSI_HIGH_S     = 80
TIMEOUT_BARS   = 48
FEE_PCT        = 0.0005
RR_MIN         = 1.2

CACHE_DIR = "binance_us_cache"


# ── DATA LOADER ───────────────────────────────────────────────────────────────

def load_symbol(prefix: str) -> pd.DataFrame:
    files = sorted(glob.glob(os.path.join(CACHE_DIR, f"{prefix}_5m_*.parquet")))
    if not files:
        raise FileNotFoundError(f"Aucune donnée pour {prefix} dans {CACHE_DIR}")
    dfs = [pd.read_parquet(f) for f in files]
    df = pd.concat(dfs)
    df = df[~df.index.duplicated(keep="last")].sort_index()
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


# ── RSI vectorisé (Wilder exponentiel sur fenêtre glissante) ─────────────────

def _rsi_from_window(closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    d = np.diff(closes)
    g = np.where(d > 0, d, 0.0)
    l = np.where(d < 0, -d, 0.0)
    ag = g[-period:].mean()
    al = l[-period:].mean()
    if al == 0:
        return 100.0
    return round(100.0 - 100.0 / (1.0 + ag / al), 2)


# ── PIVOTS détection vectorisée (swing high/low 5 bougies) ───────────────────

def compute_swing_lows(lows: np.ndarray, highs: np.ndarray):
    """Retourne tableau bool : True si lows[i] est un swing-low (i pivot sur 5 bars)."""
    n = len(lows)
    mask = np.zeros(n, dtype=bool)
    if n < 5:
        return mask
    cond = ((lows[2:-2] < lows[:-4]) & (lows[2:-2] < lows[1:-3]) &
            (lows[2:-2] < lows[3:-1]) & (lows[2:-2] < lows[4:]))
    mask[2:-2] = cond
    return mask


def compute_swing_highs(highs: np.ndarray):
    n = len(highs)
    mask = np.zeros(n, dtype=bool)
    if n < 5:
        return mask
    cond = ((highs[2:-2] > highs[:-4]) & (highs[2:-2] > highs[1:-3]) &
            (highs[2:-2] > highs[3:-1]) & (highs[2:-2] > highs[4:]))
    mask[2:-2] = cond
    return mask


# ── Clustering de pivots en zones ────────────────────────────────────────────

def cluster_zones(levels: np.ndarray, wicks: np.ndarray, current: float,
                  side: str, lookback: int = 100):
    """Cluster des swing-lows (support) ou swing-highs (résistance) dans les
    `lookback` dernières bougies. Retourne liste de zones."""
    if len(levels) == 0:
        return []
    order = np.argsort(levels)
    sorted_lvls = levels[order]
    sorted_wks  = wicks[order]
    clusters = [[(sorted_lvls[0], sorted_wks[0])]]
    for lvl, wk in zip(sorted_lvls[1:], sorted_wks[1:]):
        base = clusters[-1][0][0]
        if (lvl - base) / base < ZONE_PCT * 2:
            clusters[-1].append((lvl, wk))
        else:
            clusters.append([(lvl, wk)])
    zones = []
    for c in clusters:
        center = sum(x[0] for x in c) / len(c)
        if side == "long":
            wick = min(x[1] for x in c)
            if center < current * 0.999:
                zones.append({
                    "center": center,
                    "high":   center * (1 + ZONE_PCT),
                    "low":    center * (1 - ZONE_PCT),
                    "wick":   wick,
                    "tests":  len(c),
                })
        else:
            wick = max(x[1] for x in c)
            if center > current * 1.001:
                zones.append({
                    "center": center,
                    "high":   center * (1 + ZONE_PCT),
                    "low":    center * (1 - ZONE_PCT),
                    "wick":   wick,
                    "tests":  len(c),
                })
    if side == "long":
        zones.sort(key=lambda x: x["center"], reverse=True)  # proche en premier
    else:
        zones.sort(key=lambda x: x["center"])                # proche en premier
    return zones


def _dynamic_stop_long(lows_window, entry, wick_low):
    floor = entry * 0.992
    cand  = lows_window[-8:].min() * 0.999 if len(lows_window) >= 8 else wick_low * 0.999
    if floor <= cand < entry:
        return cand
    zs = wick_low * 0.999
    if floor <= zs < entry:
        return zs
    return entry * 0.997


def _dynamic_stop_short(highs_window, entry, wick_high):
    ceiling = entry * 1.008
    cand    = highs_window[-8:].max() * 1.001 if len(highs_window) >= 8 else wick_high * 1.001
    if entry < cand <= ceiling:
        return cand
    zs = wick_high * 1.001
    if entry < zs <= ceiling:
        return zs
    return entry * 1.003


def _bias(highs, lows):
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


# ── BACKTEST OPTIMISÉ ────────────────────────────────────────────────────────

def run_backtest(df_5m, df_15m, df_1h, enable_long=True, enable_short=False):
    # Arrays 5m
    opens_5m  = df_5m["open"].values
    highs_5m  = df_5m["high"].values
    lows_5m   = df_5m["low"].values
    closes_5m = df_5m["close"].values
    vols_5m   = df_5m["volume"].values
    ts_5m     = df_5m.index.values.astype("datetime64[ns]")

    # Arrays 15m + mapping : pour chaque i dans 5m, pos15[i] = index du dernier
    # 15m-bar fermé avant ou égal à ts_5m[i].
    ts_15  = df_15m.index.values.astype("datetime64[ns]")
    highs_15 = df_15m["high"].values
    lows_15  = df_15m["low"].values
    closes_15 = df_15m["close"].values
    pos15 = np.searchsorted(ts_15, ts_5m, side="right") - 1

    # Arrays 1h
    ts_1h  = df_1h.index.values.astype("datetime64[ns]")
    highs_1h = df_1h["high"].values
    lows_1h  = df_1h["low"].values
    pos1h = np.searchsorted(ts_1h, ts_5m, side="right") - 1

    # Précalcul swing masks sur 15m
    swing_lo_15 = compute_swing_lows(lows_15, highs_15)
    swing_hi_15 = compute_swing_highs(highs_15)

    n = len(df_5m)
    capital  = CAPITAL
    trades   = []
    open_pos = {}
    touches  = defaultdict(int)

    LOOKBACK_15 = 100

    for i in range(60, n - 1):
        hi  = highs_5m[i + 1]
        lo  = lows_5m[i + 1]
        cls = closes_5m[i + 1]

        # ── gestion sorties ──────────────────────────────────────────────
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
            else:
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
                    "t": ts_5m[i], "side": pos["side"],
                    "entry": pos["entry"], "exit": ep,
                    "pnl": round(pnl, 4), "reason": er,
                })
                del open_pos[zk]

        if capital < 20 or len(open_pos) >= MAX_SIM:
            continue

        # ── fenêtres 15m / 1h / 5m via indices positionnels ─────────────
        p15 = pos15[i]
        p1h = pos1h[i]
        if p15 < 20 or p1h < 10:
            continue

        s15 = max(0, p15 - LOOKBACK_15)
        h15_w = highs_15[s15:p15 + 1]
        l15_w = lows_15[s15:p15 + 1]
        sl_mask = swing_lo_15[s15:p15 + 1]
        sh_mask = swing_hi_15[s15:p15 + 1]

        # bias 1h sur derniers 4 bars
        if p1h >= 4:
            bias = _bias(highs_1h[p1h - 4:p1h + 1], lows_1h[p1h - 4:p1h + 1])
        else:
            bias = "range"

        # 5m window (30 derniers)
        s5 = max(0, i - 29)
        c5 = closes_5m[s5:i + 1]
        h5 = highs_5m[s5:i + 1]
        l5 = lows_5m[s5:i + 1]
        v5 = vols_5m[s5:i + 1]
        if len(c5) < 15:
            continue

        # filtre volume
        avg_v = v5[-20:].mean() if len(v5) >= 20 else v5.mean()
        if avg_v > 0 and v5[-1] < avg_v * 0.3:
            continue

        current = closes_5m[i]
        rsi_now = _rsi_from_window(c5, 14)

        # ── LONG ────────────────────────────────────────────────────────
        if enable_long and bias != "downtrend":
            lo_levels = l15_w[sl_mask]
            hi_levels = h15_w[sl_mask]  # wicks "high" lors du swing-low
            zones = cluster_zones(lo_levels, lo_levels, current, "long")
            for zone in zones:
                zk = ("L", _zone_key(zone["center"]))
                if zk in open_pos or touches[zk] >= MAX_TOUCHES:
                    continue
                dist = (current - zone["center"]) / current
                if not (0.001 <= dist <= 0.020):
                    continue
                if not (RSI_LOW_L <= rsi_now <= RSI_HIGH_L):
                    continue
                rsi_prev = _rsi_from_window(c5[:-3], 14)
                div = c5[-1] < c5[-4] and rsi_now > rsi_prev
                if not div and not (30 <= rsi_now <= 55):
                    continue
                touched = (l5[-8:] <= zone["high"]).any()
                if not (touched and c5[-1] > zone["low"]):
                    continue
                stop   = _dynamic_stop_long(l5, zone["center"], zone["wick"])
                target = zone["center"] * (1 + TARGET_PCT)
                risk   = abs(zone["center"] - stop)
                reward = abs(target - zone["center"])
                if risk <= 0 or reward / risk < RR_MIN:
                    continue
                if lo <= zone["high"]:
                    fill = min(opens_5m[i + 1], zone["center"])
                    qty  = NOTIONAL / fill
                    touches[zk] += 1
                    open_pos[zk] = {
                        "side": "long", "entry": fill,
                        "stop": stop, "target": target,
                        "qty": qty, "bar": i + 1,
                    }
                    if len(open_pos) >= MAX_SIM:
                        break

        # ── SHORT ───────────────────────────────────────────────────────
        if enable_short and bias != "uptrend" and len(open_pos) < MAX_SIM:
            hi_levels = h15_w[sh_mask]
            zones = cluster_zones(hi_levels, hi_levels, current, "short")
            for zone in zones:
                zk = ("S", _zone_key(zone["center"]))
                if zk in open_pos or touches[zk] >= MAX_TOUCHES:
                    continue
                dist = (zone["center"] - current) / current
                if not (0.001 <= dist <= 0.020):
                    continue
                if not (RSI_LOW_S <= rsi_now <= RSI_HIGH_S):
                    continue
                rsi_prev = _rsi_from_window(c5[:-3], 14)
                div = c5[-1] > c5[-4] and rsi_now < rsi_prev
                if not div and not (45 <= rsi_now <= 70):
                    continue
                touched = (h5[-8:] >= zone["low"]).any()
                if not (touched and c5[-1] < zone["high"]):
                    continue
                stop   = _dynamic_stop_short(h5, zone["center"], zone["wick"])
                target = zone["center"] * (1 - TARGET_PCT)
                risk   = abs(stop - zone["center"])
                reward = abs(zone["center"] - target)
                if risk <= 0 or reward / risk < RR_MIN:
                    continue
                if hi >= zone["low"]:
                    fill = max(opens_5m[i + 1], zone["center"])
                    qty  = NOTIONAL / fill
                    touches[zk] += 1
                    open_pos[zk] = {
                        "side": "short", "entry": fill,
                        "stop": stop, "target": target,
                        "qty": qty, "bar": i + 1,
                    }
                    if len(open_pos) >= MAX_SIM:
                        break

    # ferme le reste
    last = closes_5m[-1]
    for zk, pos in open_pos.items():
        if pos["side"] == "long":
            gross = (last - pos["entry"]) * pos["qty"]
        else:
            gross = (pos["entry"] - last) * pos["qty"]
        fees = (pos["entry"] + last) * pos["qty"] * FEE_PCT
        pnl  = gross - fees
        capital += pnl
        trades.append({
            "t": ts_5m[-1], "side": pos["side"],
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
        print(f"\n  {label} — chargement…")
        df_5m = load_symbol(prefix)
        df_5m = df_5m[(df_5m.index >= "2022-01-01") & (df_5m.index < "2026-01-01")]
        df_15 = resample_ohlcv(df_5m, "15min")
        df_1h = resample_ohlcv(df_5m, "1h")
        days  = max(1, (df_5m.index[-1] - df_5m.index[0]).days)
        print(f"    {len(df_5m):,} bars 5m | {df_5m.index[0].date()} → "
              f"{df_5m.index[-1].date()} ({days}j)")

        for mode, el, es in [
            ("LONG-only",  True,  False),
            ("SHORT-only", False, True),
            ("LONG+SHORT", True,  True),
        ]:
            print(f"    → {mode}…", end=" ", flush=True)
            trades, capital = run_backtest(df_5m, df_15, df_1h,
                                           enable_long=el, enable_short=es)
            s = stats(trades, capital, days)
            results[(label, mode)] = s
            print(f"{s['n']} trades, ret {s['ret']:+.1f}%, ann {s['ann']:+.1f}%")

    print_table("Résumé tous symboles / modes", results)

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

    out = "backtest_long_vs_short.csv"
    rows = [{"symbol": sym, "mode": mode, **s} for (sym, mode), s in results.items()]
    pd.DataFrame(rows).to_csv(out, index=False)
    print(f"\n  CSV → {out}")


if __name__ == "__main__":
    main()
