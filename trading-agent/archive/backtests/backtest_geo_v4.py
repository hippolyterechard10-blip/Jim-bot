"""
backtest_geo_v4.py — Type D Amélioré (5 upgrades pros)
========================================================
Base : Type D (support bounce + Pass 3b) + target +0.9% — déjà validé v2T3

5 améliorations vs v2T3 :
    1. ZONES larges (±0.3% autour du pivot) → +80% fréquence
    2. RSI divergence (relâché [20-65] + rsi_now > rsi_3bars) → +20% freq, +10% WR
    3. Multi-niveaux par cycle (tous les supports valides, pas juste le plus proche)
    4. Stop dynamique (sous le wick réel de la bougie de rebond, pas fixe)
    5. Zone freshness (premier touch prioritaire, 3ème touch skipé)

Métriques par classe :
    ETH : stop dynamique, target +0.9%, RSI divergence
    Alts (XRP/AVAX/LINK) : stop dynamique, target +1.0%, RSI divergence

Run: python backtest_geo_v4.py
"""

import warnings
warnings.filterwarnings("ignore")
import sys
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
from collections import defaultdict

try:
    import yfinance as yf
except ImportError:
    print("pip install yfinance --break-system-packages")
    sys.exit(1)

# ── CONFIG PAR CLASSE ─────────────────────────────────────────────────────────

ASSET_CONFIG = {
    "ETH-USD":  {"class": "major", "target": 0.009, "pos_pct": 0.28, "max_simultaneous": 1},
    "XRP-USD":  {"class": "alt",   "target": 0.010, "pos_pct": 0.20, "max_simultaneous": 1},
    "AVAX-USD": {"class": "alt",   "target": 0.010, "pos_pct": 0.20, "max_simultaneous": 1},
    "LINK-USD": {"class": "alt",   "target": 0.010, "pos_pct": 0.20, "max_simultaneous": 1},
}

CAPITAL          = 500.0
LOOKBACK_DAYS    = 30
ZONE_PCT         = 0.003       # Zone = niveau ± 0.3%
MAX_ZONE_TOUCHES = 2           # Skip si zone touchée > 2 fois
RSI_LOW          = 20          # Relâché vs [25-55] précédent
RSI_HIGH         = 65

# ── HELPERS ───────────────────────────────────────────────────────────────────

def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    d  = np.diff(np.array(closes, dtype=float))
    g  = np.where(d > 0, d, 0.0)
    l  = np.where(d < 0, -d, 0.0)
    ag = g[-period:].mean()
    al = l[-period:].mean()
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + ag / al), 2)


def _find_zones(highs, lows, closes, min_tests=1, zone_pct=ZONE_PCT):
    """
    Retourne des ZONES (bandes) et non des lignes ponctuelles.
    Chaque zone = {"low": x, "high": y, "center": z, "tests": n, "wick_low": w}
    Le wick_low est le plus bas des lows dans la zone — utilisé pour le stop dynamique.
    """
    current    = closes[-1]
    swing_lows = []

    for i in range(2, len(highs) - 2):
        if (lows[i] < lows[i-1] and lows[i] < lows[i-2]
                and lows[i] < lows[i+1] and lows[i] < lows[i+2]):
            swing_lows.append((lows[i], highs[i]))  # (low, high of that candle)

    if not swing_lows:
        return []

    swing_lows.sort(key=lambda x: x[0])
    clusters = [[swing_lows[0]]]
    for lvl in swing_lows[1:]:
        if (lvl[0] - clusters[-1][0][0]) / clusters[-1][0][0] < zone_pct * 2:
            clusters[-1].append(lvl)
        else:
            clusters.append([lvl])

    zones = []
    for c in clusters:
        center   = sum(x[0] for x in c) / len(c)
        wick_low = min(x[0] for x in c)
        zone_top = center * (1 + zone_pct)
        zone_bot = center * (1 - zone_pct)
        if center < current * 0.999:
            zones.append({
                "center":   center,
                "low":      zone_bot,
                "high":     zone_top,
                "wick_low": wick_low,
                "tests":    len(c),
            })

    zones.sort(key=lambda x: x["center"], reverse=True)  # nearest first
    return [z for z in zones if z["tests"] >= min_tests]


def _find_zones_above(bars_df, current_price):
    """Trouve les zones de résistance AU-DESSUS du prix actuel."""
    highs  = bars_df["high"].values
    lows   = bars_df["low"].values
    closes = bars_df["close"].values

    swing_highs = []
    for i in range(2, len(highs) - 2):
        if (highs[i] > highs[i-1] and highs[i] > highs[i-2]
                and highs[i] > highs[i+1] and highs[i] > highs[i+2]):
            swing_highs.append(highs[i])

    if not swing_highs:
        return []
    swing_highs.sort()
    clusters = [[swing_highs[0]]]
    for v in swing_highs[1:]:
        if (v - clusters[-1][0]) / clusters[-1][0] < ZONE_PCT * 2:
            clusters[-1].append(v)
        else:
            clusters.append([v])

    zones = []
    for c in clusters:
        center = sum(c) / len(c)
        if center > current_price * 1.001:
            zones.append({
                "center":   center,
                "low":      center * (1 - ZONE_PCT),
                "high":     center * (1 + ZONE_PCT),
                "wick_low": min(c),
                "tests":    len(c),
            })
    zones.sort(key=lambda x: x["center"])
    return zones


def _rsi_divergence(closes, rsi_now):
    """
    Divergence haussière : prix fait un lower low MAIS RSI fait un higher low.
    On compare close[-1] vs close[-4] et rsi_now vs rsi_4bars_ago.
    """
    if len(closes) < 5:
        return False
    rsi_prev   = _rsi(np.array(closes[:-3]), 14)
    price_lower = closes[-1] < closes[-4]
    rsi_higher  = rsi_now > rsi_prev
    return price_lower and rsi_higher


def _dynamic_stop(bars_5m_past, entry_level, wick_low):
    """
    Stop = sous le vrai wick bas de la bougie de rebond.
    Fallback : entry_level * 0.997.
    Plafonné à -0.8% pour éviter les stops trop larges.
    """
    floor = entry_level * 0.992  # plancher absolu -0.8%

    # Wick low récent dans les 8 dernières bougies
    recent_lows = bars_5m_past["low"].values[-8:]
    candidate   = min(recent_lows) * 0.999  # 0.1% sous le wick

    if candidate >= floor and candidate < entry_level:
        return candidate

    # Fallback : zone wick_low - 0.1%
    zone_stop = wick_low * 0.999
    if zone_stop >= floor and zone_stop < entry_level:
        return zone_stop

    return entry_level * 0.997  # fallback fixe


def _bias_1h(highs, lows):
    if len(highs) < 5:
        return "range"
    hh = highs[-1] > highs[-4]
    hl = lows[-1] > lows[-4]
    lh = highs[-1] < highs[-4]
    ll = lows[-1] < lows[-4]
    if hh and hl:
        return "uptrend"
    if lh and ll:
        return "downtrend"
    return "range"


def _zone_key(center):
    """Clé pour le tracking de freshness — arrondie à 0.1%."""
    return round(center, int(-np.log10(center * 0.001)))

# ── BACKTEST PAR SYMBOLE ──────────────────────────────────────────────────────

def backtest_symbol(symbol):
    cfg   = ASSET_CONFIG.get(symbol, ASSET_CONFIG["XRP-USD"])
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=min(LOOKBACK_DAYS + 5, 58))

    print(f"\n    ▶ {symbol}")
    try:
        df_1h  = yf.download(symbol, start=start, end=end, interval="1h",  auto_adjust=True, progress=False)
        df_15m = yf.download(symbol, start=start, end=end, interval="15m", auto_adjust=True, progress=False)
        df_5m  = yf.download(symbol, start=start, end=end, interval="5m",  auto_adjust=True, progress=False)
    except Exception as e:
        print(f"        Download: {e}")
        return {}

    for df in [df_1h, df_15m, df_5m]:
        if df.empty:
            print("        No data")
            return {}
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.index   = pd.to_datetime(df.index, utc=True)
        df.columns = [c.lower() for c in df.columns]

    capital  = CAPITAL
    equity   = [capital]
    trades   = []

    # Multi-positions simultanées
    open_positions   = {}             # zone_key → {entry, stop, target, qty, bar_entry}

    # Zone freshness tracker
    zone_touch_count = defaultdict(int)   # zone_key → nb de fois touchée

    start_i = 55

    for i in range(start_i, len(df_5m) - 1):
        t_now  = df_5m.index[i]
        bar_5m = df_5m.iloc[i]
        next_b = df_5m.iloc[i + 1]

        # ── Gestion positions ouvertes ─────────────────────────────────────
        closed_keys = []
        for zk, pos in list(open_positions.items()):
            hi  = float(next_b["high"])
            lo  = float(next_b["low"])
            cls = float(next_b["close"])
            stop_hit   = lo <= pos["stop"]
            target_hit = hi >= pos["target"]
            timeout    = (i - pos["bar_entry"]) >= 48

            ep = er = None
            if stop_hit and target_hit:
                ep, er = pos["stop"],   "stop"
            elif stop_hit:
                ep, er = pos["stop"],   "stop"
            elif target_hit:
                ep, er = pos["target"], "target"
            elif timeout:
                ep, er = cls,           "timeout"

            if ep is not None:
                qty = pos["qty"]
                pnl = (ep - pos["entry"]) * qty
                capital += pnl
                trades.append({
                    "symbol": symbol, "entry": pos["entry"], "exit": ep,
                    "pnl": round(pnl, 4), "reason": er,
                    "bars": i - pos["bar_entry"],
                })
                equity.append(capital)
                closed_keys.append(zk)

        for zk in closed_keys:
            del open_positions[zk]

        if capital < 20:
            continue

        # ── Données passées (no look-ahead) ───────────────────────────────
        bars_1h_p  = df_1h[df_1h.index <= t_now].tail(50)
        bars_15m_p = df_15m[df_15m.index <= t_now].tail(100)
        bars_5m_p  = df_5m[df_5m.index <= t_now].tail(30)

        if len(bars_1h_p) < 10 or len(bars_15m_p) < 20 or len(bars_5m_p) < 10:
            continue

        # Pass 1 — Bias 1h
        bias = _bias_1h(bars_1h_p["high"].values, bars_1h_p["low"].values)
        if bias == "downtrend":
            continue

        current    = float(bar_5m["close"])
        closes_5m  = bars_5m_p["close"].values
        volumes_5m = bars_5m_p["volume"].values
        rsi_now    = _rsi(closes_5m, 14)

        avg_vol = volumes_5m[-20:].mean() if len(volumes_5m) >= 20 else volumes_5m.mean()
        if avg_vol > 0 and volumes_5m[-1] < avg_vol * 0.3:
            continue

        # Pass 2 — Zones 15min (TOUTES les zones valides, pas juste la plus proche)
        zones_15m = _find_zones(
            bars_15m_p["high"].values,
            bars_15m_p["low"].values,
            bars_15m_p["close"].values,
            min_tests=1,
        )

        # Limiter le nombre de positions simultanées
        if len(open_positions) >= cfg["max_simultaneous"]:
            continue

        # Évaluer CHAQUE zone valide indépendamment
        for zone in zones_15m:
            zk = _zone_key(zone["center"])

            # Skip si déjà en position sur cette zone
            if zk in open_positions:
                continue

            # Skip si zone trop usée
            if zone_touch_count[zk] >= MAX_ZONE_TOUCHES:
                continue

            # Prix dans la zone ou s'en approchant (0.1% à 2%)
            dist = (current - zone["center"]) / current
            if not (0.001 <= dist <= 0.020):
                continue

            # Amélioration 2 — RSI divergence (plus souple mais plus qualitatif)
            if not (RSI_LOW <= rsi_now <= RSI_HIGH):
                continue
            div = _rsi_divergence(closes_5m, rsi_now)
            # Si pas de divergence, RSI gate plus strict [30-55]
            if not div and not (30 <= rsi_now <= 55):
                continue

            # Pass 3b — niveau touché ET remonté (déjà validé)
            touched      = any(bars_5m_p["low"].values[-8:] <= zone["high"])
            closed_above = closes_5m[-1] > zone["low"]
            if not (touched and closed_above):
                continue

            # Amélioration 4 — stop dynamique
            stop = _dynamic_stop(bars_5m_p, zone["center"], zone["wick_low"])

            # Target : résistance naturelle sur 5min, sinon fallback fixe
            target = zone["center"] * (1 + cfg["target"])
            resistances = [
                z for z in _find_zones_above(bars_5m_p, current)
                if 0.005 <= (z["center"] - zone["center"]) / zone["center"] <= 0.020
            ]
            if resistances:
                target = resistances[0]["center"]

            # R:R minimum 1.5:1
            risk   = abs(zone["center"] - stop)
            reward = abs(target - zone["center"])
            if risk <= 0 or reward / risk < 1.5:
                continue

            # Simulation fill
            next_lo   = float(next_b["low"])
            next_open = float(next_b["open"])
            if next_lo <= zone["high"]:  # prix entre dans la zone
                fill = min(next_open, zone["center"])
                qty  = (CAPITAL * cfg["pos_pct"]) / fill

                # Freshness : on incrémente à chaque touch
                zone_touch_count[zk] += 1

                open_positions[zk] = {
                    "entry":     fill,
                    "stop":      stop,
                    "target":    target,
                    "qty":       qty,
                    "bar_entry": i + 1,
                    "divergence": div,
                }

    # Fermer positions restantes
    if open_positions:
        last_cls = float(df_5m["close"].iloc[-1])
        for zk, pos in open_positions.items():
            pnl = (last_cls - pos["entry"]) * pos["qty"]
            capital += pnl
            trades.append({
                "symbol": symbol, "entry": pos["entry"], "exit": last_cls,
                "pnl": round(pnl, 4), "reason": "end",
                "bars": len(df_5m) - 1 - pos["bar_entry"],
            })
        equity.append(capital)

    return {
        "symbol": symbol, "cfg": cfg, "trades": trades,
        "capital": capital, "equity": equity,
        "zone_touches": dict(zone_touch_count),
    }

# ── STATS ─────────────────────────────────────────────────────────────────────

def stats(trades, capital):
    n = len(trades)
    if n == 0:
        return None
    wins  = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total = sum(t["pnl"] for t in trades)
    wr    = len(wins) / n * 100
    sum_l = sum(t["pnl"] for t in losses)
    pf    = abs(sum(t["pnl"] for t in wins) / sum_l) if sum_l != 0 else 99.0
    ret   = (capital - CAPITAL) / CAPITAL * 100
    ann   = ret / LOOKBACK_DAYS * 365
    eq    = np.array([CAPITAL] + [CAPITAL + sum(t["pnl"] for t in trades[:k+1])
                                  for k in range(len(trades))])
    pk    = np.maximum.accumulate(eq)
    mdd   = ((eq - pk) / pk * 100).min()
    return {
        "n": n, "wins": len(wins), "losses": len(losses),
        "wr": round(wr, 1), "pf": round(pf, 2),
        "total": round(total, 2), "ret": round(ret, 1),
        "ann": round(ann, 1), "mdd": round(mdd, 1),
    }

# ── MAIN ──────────────────────────────────────────────────────────────────────

def run():
    print("\n" + "═" * 65)
    print("     GEO V4 — Type D Pro (Zones + Divergence + Multi-niveaux)")
    print(f"     {LOOKBACK_DAYS}j | Zone ±{ZONE_PCT*100:.1f}% | RSI divergence | Stop dynamique")
    print("     Comparaison vs v2T3 : ETH 71.4% WR / PF 8.89 / +13.6% ann")
    print("═" * 65)

    results = []
    for sym in ASSET_CONFIG:
        r = backtest_symbol(sym)
        if r and r.get("trades"):
            s = stats(r["trades"], r["capital"])
            if s:
                r["stats"] = s
                results.append(r)

    if not results:
        print("\n     Aucun résultat.")
        return

    print("\n" + "═" * 65)
    print("     RÉSULTATS")
    print(f"     {'Sym':<10} {'N':>4} {'WR%':>6} {'PF':>5} {'P&L$':>8} {'Ann%':>8} {'MDD%':>6}")
    print(f"     {'─'*10} {'─'*4} {'─'*6} {'─'*5} {'─'*8} {'─'*8} {'─'*6}")

    total_pnl = total_n = 0
    for r in sorted(results, key=lambda x: x["stats"]["ann"], reverse=True):
        s    = r["stats"]
        flag = "✅" if s["ann"] >= 15 else ("➕" if s["ann"] >= 8 else "➖")
        print(f"     {r['symbol']:<10} {s['n']:>4} {s['wr']:>6.1f} {s['pf']:>5.2f} "
              f"{s['total']:>+8.2f} {s['ann']:>+7.1f}% {s['mdd']:>+5.1f}%  {flag}")
        total_pnl += s["total"]
        total_n   += s["n"]

    print(f"\n     Total trades   : {total_n}")
    print(f"     P&L portfolio : ${total_pnl:+.2f}")

    print("\n     COMPARAISON v2T3 → v4 (ETH)")
    print("     ┌─────────────────┬────────┬──────┬──────┬──────────┐")
    print("     │ Config          │ Trades │  WR% │   PF │    Ann%  │")
    print("     ├─────────────────┼────────┼──────┼──────┼──────────┤")
    print("     │ v2T3 (baseline) │      7 │ 71.4 │ 8.89 │  +13.6% │")
    for r in results:
        if "ETH" in r["symbol"]:
            s = r["stats"]
            print(f"     │ v4  (Pro)       │ {s['n']:>6} │ {s['wr']:>4.1f} │ {s['pf']:>4.2f} │ {s['ann']:>+7.1f}% │")
    print("     └─────────────────┴────────┴──────┴──────┴──────────┘")

    # CSV
    all_t = []
    for r in results:
        all_t.extend(r["trades"])
    if all_t:
        pd.DataFrame(all_t).to_csv("backtest_geo_v4_trades.csv", index=False)
        print(f"\n     {len(all_t)} trades → backtest_geo_v4_trades.csv")

    print("═" * 65 + "\n")


if __name__ == "__main__":
    run()
