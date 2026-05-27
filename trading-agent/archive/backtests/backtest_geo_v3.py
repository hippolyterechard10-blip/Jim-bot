"""
backtest_geo_v3.py — 4 Setup Types + Métriques par Classe
==========================================================
Type A — Breakout : résistance cassée avec volume → entrée sur retest
Type B — Range midpoint : range détecté → achat en bas du range
Type C — Momentum continuation : 3 higher lows + volume croissant → pullback
Type D — Support bounce : niveau touché + remonté (Pass 3b actuel) ← déjà validé

Métriques par classe :
    Majeurs (ETH) : stop -0.3%, target +0.9%, RSI [25-55], min_tests=1
    Alts (XRP, AVAX, LINK) : stop -0.5%, target +1.0%, RSI [30-50], min_tests=2

Run: python backtest_geo_v3.py
"""

import warnings
warnings.filterwarnings("ignore")
import sys
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    print("pip install yfinance --break-system-packages")
    sys.exit(1)

# ── CONFIG PAR CLASSE ─────────────────────────────────────────────────────────

ASSET_CONFIG = {
    "ETH-USD":  {"class": "major", "stop": 0.003, "target": 0.009, "rsi_lo": 25, "rsi_hi": 55, "min_tests": 1, "pos_pct": 0.28},
    "XRP-USD":  {"class": "alt",   "stop": 0.005, "target": 0.010, "rsi_lo": 30, "rsi_hi": 50, "min_tests": 2, "pos_pct": 0.28},
    "AVAX-USD": {"class": "alt",   "stop": 0.005, "target": 0.010, "rsi_lo": 30, "rsi_hi": 50, "min_tests": 2, "pos_pct": 0.28},
    "LINK-USD": {"class": "alt",   "stop": 0.005, "target": 0.010, "rsi_lo": 30, "rsi_hi": 50, "min_tests": 2, "pos_pct": 0.28},
}

CAPITAL       = 500.0
LOOKBACK_DAYS = 30
WARMUP_1H     = 50
WARMUP_15M    = 100
WARMUP_5M     = 30

# ── HELPERS ───────────────────────────────────────────────────────────────────

def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    d = np.diff(closes)
    g = np.where(d > 0, d, 0.0)
    l = np.where(d < 0, -d, 0.0)
    ag = g[-period:].mean()
    al = l[-period:].mean()
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + ag / al), 2)


def _find_swing_levels(highs, lows, closes, min_tests=2, zone=0.003):
    current = closes[-1]
    sh, sl = [], []
    for i in range(2, len(highs) - 2):
        if (highs[i] > highs[i-1] and highs[i] > highs[i-2]
                and highs[i] > highs[i+1] and highs[i] > highs[i+2]):
            sh.append(highs[i])
        if (lows[i] < lows[i-1] and lows[i] < lows[i-2]
                and lows[i] < lows[i+1] and lows[i] < lows[i+2]):
            sl.append(lows[i])

    def _cluster(lvls):
        if not lvls:
            return []
        s = sorted(lvls)
        c = [[s[0]]]
        for v in s[1:]:
            if (v - c[-1][0]) / c[-1][0] < zone:
                c[-1].append(v)
            else:
                c.append([v])
        return [(sum(x) / len(x), len(x)) for x in c]

    supports    = [{"level": p, "tests": n} for p, n in _cluster(sl)
                   if n >= min_tests and p < current]
    resistances = [{"level": p, "tests": n} for p, n in _cluster(sh)
                   if n >= min_tests and p > current]
    supports.sort(key=lambda x: x["level"], reverse=True)
    resistances.sort(key=lambda x: x["level"])
    return {"supports": supports, "resistances": resistances}


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

# ── 4 SIGNAL TYPES ────────────────────────────────────────────────────────────

def _signal_D(bars_5m_past, chosen_level, closes_5m, rsi_5m, cfg):
    """Type D — Support bounce + Pass 3b (déjà validé)."""
    if len(closes_5m) < 4:
        return False
    moving_toward = closes_5m[-1] <= closes_5m[-4]
    if not moving_toward:
        return False
    if not (cfg["rsi_lo"] <= rsi_5m <= cfg["rsi_hi"]):
        return False
    touched     = any(bars_5m_past["low"].values[-8:] <= chosen_level * 1.001)
    closed_above = closes_5m[-1] > chosen_level * 1.001
    return touched and closed_above


def _signal_A(bars_5m_past, closes_5m, resistances, rsi_5m, cfg):
    """Type A — Breakout + retest de la résistance devenue support."""
    if not resistances:
        return False, None
    volumes_5m = bars_5m_past["volume"].values
    avg_vol    = volumes_5m[-20:].mean() if len(volumes_5m) >= 20 else volumes_5m.mean()
    current    = closes_5m[-1]

    for res in resistances[:3]:
        level = res["level"]
        if len(closes_5m) < 3:
            continue
        broke_above = closes_5m[-3] < level and closes_5m[-1] > level * 1.001
        vol_ok      = volumes_5m[-1] > avg_vol * 1.3
        retesting   = abs(current - level) / level < 0.005 and current >= level
        if broke_above and vol_ok and retesting:
            return True, level
        if broke_above and volumes_5m[-1] > avg_vol * 2.0:
            return True, level
    return False, None


def _signal_B(bars_5m_past, closes_5m, rsi_5m, cfg):
    """Type B — Range midpoint : détecte un range et achète en bas."""
    if len(closes_5m) < 20:
        return False, None
    w        = closes_5m[-20:]
    rng_high = max(w)
    rng_low  = min(w)
    rng_size = (rng_high - rng_low) / rng_low
    if rng_size > 0.02:
        return False, None
    current     = closes_5m[-1]
    lower_third = rng_low + (rng_high - rng_low) * 0.35
    if current > lower_third:
        return False, None
    if rsi_5m < 25:
        return False, None
    level = rng_low
    return True, level


def _signal_C(bars_5m_past, closes_5m, rsi_5m, cfg):
    """Type C — Momentum continuation : 3 higher lows + volume croissant."""
    if len(closes_5m) < 8:
        return False, None
    lows_5m    = bars_5m_past["low"].values[-10:]
    volumes_5m = bars_5m_past["volume"].values[-10:]
    if len(lows_5m) < 6:
        return False, None
    hl1         = lows_5m[-2] > lows_5m[-4]
    hl2         = lows_5m[-4] > lows_5m[-6]
    higher_lows = hl1 and hl2
    if not higher_lows:
        return False, None
    vol_growing = volumes_5m[-1] > volumes_5m[-2] > volumes_5m[-3]
    if not vol_growing:
        return False, None
    if not (35 <= rsi_5m <= 65):
        return False, None
    pullback = closes_5m[-1] < closes_5m[-3]
    if not pullback:
        return False, None
    return True, closes_5m[-1]

# ── BACKTEST PAR SYMBOLE ──────────────────────────────────────────────────────

def backtest_symbol(symbol):
    cfg = ASSET_CONFIG.get(symbol, ASSET_CONFIG["XRP-USD"])
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=LOOKBACK_DAYS + 5)

    try:
        df_1h  = yf.download(symbol, start=start, end=end, interval="1h",  auto_adjust=True, progress=False)
        df_15m = yf.download(symbol, start=start, end=end, interval="15m", auto_adjust=True, progress=False)
        df_5m  = yf.download(symbol, start=start, end=end, interval="5m",  auto_adjust=True, progress=False)
    except Exception as e:
        print(f"    {symbol}: {e}")
        return {}

    for df in [df_1h, df_15m, df_5m]:
        if df.empty:
            print(f"    No data")
            return {}
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.index   = pd.to_datetime(df.index, utc=True)
        df.columns = [c.lower() for c in df.columns]

    capital     = CAPITAL
    equity      = [capital]
    trades      = []
    in_trade    = False
    entry_price = stop_price = target_price = 0.0
    trade_side  = "long"
    entry_bar   = 0
    signal_type = ""

    start_i = max(WARMUP_5M + 5, 50)

    for i in range(start_i, len(df_5m) - 1):
        t_now  = df_5m.index[i]
        bar_5m = df_5m.iloc[i]
        next_b = df_5m.iloc[i + 1]

        # ── Gestion position ──────────────────────────────────────────────
        if in_trade:
            hi  = float(next_b["high"])
            lo  = float(next_b["low"])
            cls = float(next_b["close"])
            stop_hit   = lo <= stop_price
            target_hit = hi >= target_price

            if stop_hit and target_hit:
                ep, er = stop_price, "stop"
            elif stop_hit:
                ep, er = stop_price, "stop"
            elif target_hit:
                ep, er = target_price, "target"
            elif i - entry_bar >= 48:
                ep, er = cls, "timeout"
            else:
                continue

            qty     = (CAPITAL * cfg["pos_pct"]) / entry_price
            pnl     = (ep - entry_price) * qty
            capital += pnl
            trades.append({
                "symbol": symbol, "type": signal_type,
                "entry": entry_price, "exit": ep,
                "pnl": round(pnl, 4), "reason": er,
                "bars": i - entry_bar,
            })
            equity.append(capital)
            in_trade = False
            continue

        if capital < 20:
            continue

        # ── Données passées ───────────────────────────────────────────────
        bars_1h_p  = df_1h[df_1h.index <= t_now].tail(WARMUP_1H)
        bars_15m_p = df_15m[df_15m.index <= t_now].tail(WARMUP_15M)
        bars_5m_p  = df_5m[df_5m.index <= t_now].tail(WARMUP_5M)

        if len(bars_1h_p) < 10 or len(bars_15m_p) < 20 or len(bars_5m_p) < 10:
            continue

        bias = _bias_1h(bars_1h_p["high"].values, bars_1h_p["low"].values)
        if bias == "downtrend":
            continue  # crypto long only

        current   = float(bar_5m["close"])
        closes_5m = bars_5m_p["close"].values
        rsi_5m    = _rsi(closes_5m, 14)
        vols_5m   = bars_5m_p["volume"].values
        avg_vol   = vols_5m[-20:].mean() if len(vols_5m) >= 20 else vols_5m.mean()

        # Volume minimum actif
        if avg_vol > 0 and vols_5m[-1] < avg_vol * 0.3:
            continue

        swing_15m = _find_swing_levels(
            bars_15m_p["high"].values, bars_15m_p["low"].values,
            bars_15m_p["close"].values, cfg["min_tests"]
        )
        swing_5m = _find_swing_levels(
            bars_5m_p["high"].values, bars_5m_p["low"].values,
            bars_5m_p["close"].values, min_tests=1
        )

        chosen_level = None
        chosen_type  = None

        # ── TYPE D — Support bounce (priorité 1, déjà validé) ────────────
        for lvl_info in swing_15m["supports"]:
            lvl  = lvl_info["level"]
            dist = (current - lvl) / current
            if 0.001 <= dist <= 0.015:
                if _signal_D(bars_5m_p, lvl, closes_5m, rsi_5m, cfg):
                    chosen_level = lvl
                    chosen_type  = "D"
                    break

        # ── TYPE A — Breakout (priorité 2) ───────────────────────────────
        if not chosen_level:
            ok, lvl = _signal_A(bars_5m_p, closes_5m, swing_15m["resistances"], rsi_5m, cfg)
            if ok and lvl:
                chosen_level = lvl
                chosen_type  = "A"

        # ── TYPE C — Momentum continuation (priorité 3) ──────────────────
        if not chosen_level:
            ok, lvl = _signal_C(bars_5m_p, closes_5m, rsi_5m, cfg)
            if ok and lvl:
                chosen_level = lvl
                chosen_type  = "C"

        # ── TYPE B — Range midpoint (priorité 4) ─────────────────────────
        if not chosen_level:
            ok, lvl = _signal_B(bars_5m_p, closes_5m, rsi_5m, cfg)
            if ok and lvl:
                chosen_level = lvl
                chosen_type  = "B"

        if not chosen_level:
            continue

        # ── Stop + Target ─────────────────────────────────────────────────
        stop   = chosen_level * (1 - cfg["stop"])
        target = chosen_level * (1 + cfg["target"])

        # Target naturel sur 5min (zone dynamique autour du target fixe)
        for lvl_info in swing_5m["resistances"]:
            lvl      = lvl_info["level"]
            dist     = (lvl - chosen_level) / chosen_level
            if cfg["target"] * 0.7 <= dist <= cfg["target"] * 2.0:
                target = lvl
                break

        risk   = abs(chosen_level - stop)
        reward = abs(target - chosen_level)
        if risk <= 0 or reward / risk < 1.2:
            continue

        # ── Simulation fill ───────────────────────────────────────────────
        next_lo   = float(next_b["low"])
        next_open = float(next_b["open"])
        if next_lo <= chosen_level:
            entry_price  = min(next_open, chosen_level)
            stop_price   = stop
            target_price = target
            trade_side   = "long"
            entry_bar    = i + 1
            signal_type  = chosen_type
            in_trade     = True

    if in_trade:
        cls = float(df_5m["close"].iloc[-1])
        qty = (CAPITAL * cfg["pos_pct"]) / entry_price
        pnl = (cls - entry_price) * qty
        capital += pnl
        trades.append({
            "symbol": symbol, "type": signal_type,
            "entry": entry_price, "exit": cls,
            "pnl": round(pnl, 4), "reason": "end",
            "bars": len(df_5m) - entry_bar,
        })
        equity.append(capital)

    return {"symbol": symbol, "cfg": cfg, "trades": trades,
            "capital": capital, "equity": equity}

# ── STATS ─────────────────────────────────────────────────────────────────────

def stats(trades, capital, cfg):
    n = len(trades)
    if n == 0:
        return None
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total  = sum(t["pnl"] for t in trades)
    wr     = len(wins) / n * 100
    pf     = (abs(sum(t["pnl"] for t in wins) / sum(t["pnl"] for t in losses))
              if losses and sum(t["pnl"] for t in losses) != 0 else 99.0)
    ret    = (capital - CAPITAL) / CAPITAL * 100
    ann    = ret / LOOKBACK_DAYS * 365
    by_type = {}
    for t in trades:
        tp = t["type"]
        if tp not in by_type:
            by_type[tp] = {"n": 0, "wins": 0, "pnl": 0}
        by_type[tp]["n"]    += 1
        by_type[tp]["wins"] += 1 if t["pnl"] > 0 else 0
        by_type[tp]["pnl"]  += t["pnl"]
    return {
        "n": n, "wins": len(wins), "losses": len(losses),
        "wr": round(wr, 1), "pf": round(pf, 2),
        "total": round(total, 2), "ret": round(ret, 1),
        "ann": round(ann, 1), "by_type": by_type,
    }

# ── MAIN ──────────────────────────────────────────────────────────────────────

def run():
    print("\n" + "═" * 70)
    print("     GEO V3 — 4 Setup Types (A/B/C/D) + Métriques par Classe")
    print(f"     Capital: ${CAPITAL} | {LOOKBACK_DAYS} jours | Crypto long only")
    print("═" * 70)

    results = []
    for sym in ASSET_CONFIG:
        print(f"\n▶  {sym}")
        r = backtest_symbol(sym)
        if r and r.get("trades"):
            s = stats(r["trades"], r["capital"], r["cfg"])
            if s:
                r["stats"] = s
                results.append(r)
                print(f"   {s['n']} trades | WR={s['wr']}% | PF={s['pf']} | ${s['total']:+.2f} | Ann={s['ann']:+.1f}%")
                for tp, v in sorted(s["by_type"].items()):
                    twr = round(v["wins"] / v["n"] * 100, 1) if v["n"] > 0 else 0
                    print(f"   Type {tp}: {v['n']} trades | WR={twr}% | P&L=${v['pnl']:+.2f}")
        else:
            print(f"   Aucun trade généré")

    # ── Résumé ────────────────────────────────────────────────────────────
    print("\n" + "═" * 70)
    print("     RÉSUMÉ GLOBAL")
    print(f"     {'Symbol':<12} {'N':>4} {'WR%':>6} {'PF':>5} {'P&L$':>8} {'Ann%':>8}   Types actifs")
    print(f"     {'─'*12} {'─'*4} {'─'*6} {'─'*5} {'─'*8} {'─'*8}")

    total_pnl = 0
    for r in sorted(results, key=lambda x: x["stats"]["ann"], reverse=True):
        s     = r["stats"]
        types = "/".join(sorted(s["by_type"].keys()))
        flag  = "✅" if s["ann"] >= 15 else ("➕" if s["ann"] >= 8 else "➖")
        print(f"     {r['symbol']:<12} {s['n']:>4} {s['wr']:>6.1f} {s['pf']:>5.2f} "
              f"{s['total']:>+8.2f} {s['ann']:>+7.1f}%   {types}  {flag}")
        total_pnl += s["total"]

    print(f"\n     Portfolio total : ${total_pnl:+.2f}")

    # ── Analyse par type ──────────────────────────────────────────────────
    print("\n     PERFORMANCE PAR TYPE DE SETUP (tous assets)")
    global_types = {}
    for r in results:
        for tp, v in r["stats"]["by_type"].items():
            if tp not in global_types:
                global_types[tp] = {"n": 0, "wins": 0, "pnl": 0.0}
            global_types[tp]["n"]    += v["n"]
            global_types[tp]["wins"] += v["wins"]
            global_types[tp]["pnl"]  += v["pnl"]

    labels = {"A": "Breakout", "B": "Range mid", "C": "Momentum cont.", "D": "Support bounce"}
    for tp in sorted(global_types):
        v    = global_types[tp]
        twr  = round(v["wins"] / v["n"] * 100, 1) if v["n"] > 0 else 0
        flag = "✅" if twr >= 55 and v["pnl"] > 0 else ("➕" if v["pnl"] > 0 else "❌")
        print(f"     Type {tp} ({labels.get(tp,'?')}): {v['n']} trades | WR={twr}% | P&L=${v['pnl']:+.2f}  {flag}")

    # ── CSV ───────────────────────────────────────────────────────────────
    all_trades = []
    for r in results:
        all_trades.extend(r["trades"])
    if all_trades:
        pd.DataFrame(all_trades).to_csv("backtest_geo_v3_trades.csv", index=False)
        print(f"\n     {len(all_trades)} trades → backtest_geo_v3_trades.csv")

    print("═" * 70 + "\n")


if __name__ == "__main__":
    run()
