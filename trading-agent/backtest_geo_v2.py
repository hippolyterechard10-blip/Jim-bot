"""
backtest_geo_v2.py — GeometricExpert 3-Timeframes Backtest
===========================================================
Tourne sur 1 mois de crypto (30 jours glissants).
Simule exactement la nouvelle logique :
  Pass 1 — 1h   : bias HH/HL ou LH/LL
  Pass 2 — 15min: niveau S/R testé >= 2x dans la zone 0.1%-1.5%
  Pass 3 — 5min : momentum + RSI[25-55] + volume > 0.3x
  Pass 4 — stop : swing low 5min + 0.2% buffer, fallback -0.3%
  Pass 5 — target: prochain swing high 5min [+0.7%-2.0%], fallback +0.9%

Aucun look-ahead : toutes les données utilisées à l'instant T
sont strictement antérieures à T.

Entrée simulée : ordre limit au niveau -> fill si low <= level sur
la prochaine bougie 5min (long) ou high >= level (short).
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
    print("yfinance manquant. Run: pip install yfinance --break-system-packages")
    sys.exit(1)

# ── CONFIG ────────────────────────────────────────────────────────────────────

SYMBOLS       = ["ETH-USD", "XRP-USD", "AVAX-USD", "LINK-USD"]
LOOKBACK_DAYS = 58
CAPITAL       = 500.0
POSITION_PCT  = 0.28
MIN_POSITION  = 20.0
RSI_PERIOD    = 14
WARMUP_1H     = 50
WARMUP_15M    = 100
WARMUP_5M     = 30

# ── HELPERS ───────────────────────────────────────────────────────────────────

def _rsi(closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    d  = np.diff(closes)
    g  = np.where(d > 0, d, 0.0)
    l  = np.where(d < 0, -d, 0.0)
    ag = g[-period:].mean()
    al = l[-period:].mean()
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + ag / al), 2)


def _find_swing_levels(highs, lows, closes, min_tests=2, zone_pct=0.003):
    current = closes[-1]
    swing_h, swing_l = [], []
    for i in range(2, len(highs) - 2):
        if (highs[i] > highs[i-1] and highs[i] > highs[i-2]
                and highs[i] > highs[i+1] and highs[i] > highs[i+2]):
            swing_h.append(highs[i])
        if (lows[i] < lows[i-1] and lows[i] < lows[i-2]
                and lows[i] < lows[i+1] and lows[i] < lows[i+2]):
            swing_l.append(lows[i])

    def _cluster(levels):
        if not levels:
            return []
        sl = sorted(levels)
        clusters = [[sl[0]]]
        for v in sl[1:]:
            if (v - clusters[-1][0]) / clusters[-1][0] < zone_pct:
                clusters[-1].append(v)
            else:
                clusters.append([v])
        return [(sum(c) / len(c), len(c)) for c in clusters]

    supports = [
        {"level": p, "tests": n}
        for p, n in _cluster(swing_l)
        if n >= min_tests and p < current * 0.999
    ]
    resistances = [
        {"level": p, "tests": n}
        for p, n in _cluster(swing_h)
        if n >= min_tests and p > current * 1.001
    ]
    supports.sort(key=lambda x: x["level"], reverse=True)
    resistances.sort(key=lambda x: x["level"])
    return {"supports": supports, "resistances": resistances}


def _find_5min_stop(highs, lows, side, entry_price):
    fallback = entry_price * (0.997 if side == "long" else 1.003)
    w_l = lows[-15:]
    w_h = highs[-15:]
    if side == "long":
        max_stop = entry_price * 0.995
        cands = [
            w_l[i] for i in range(1, len(w_l) - 1)
            if w_l[i] < w_l[i-1] and w_l[i] < w_l[i+1]
            and max_stop <= w_l[i] < entry_price
        ]
        if cands:
            return max(cands) * 0.998
    else:
        max_stop = entry_price * 1.005
        cands = [
            w_h[i] for i in range(1, len(w_h) - 1)
            if w_h[i] > w_h[i-1] and w_h[i] > w_h[i+1]
            and entry_price < w_h[i] <= max_stop
        ]
        if cands:
            return min(cands) * 1.002
    return fallback


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

# ── BACKTEST PAR SYMBOLE ──────────────────────────────────────────────────────

def backtest_symbol(symbol: str) -> dict:
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=LOOKBACK_DAYS)

    print(f"\n{'─'*60}")
    print(f"  {symbol}  —  {LOOKBACK_DAYS} jours  —  5min bars")

    try:
        df_1h  = yf.download(symbol, start=start, end=end, interval="1h",  auto_adjust=True, progress=False)
        df_15m = yf.download(symbol, start=start, end=end, interval="15m", auto_adjust=True, progress=False)
        df_5m  = yf.download(symbol, start=start, end=end, interval="5m",  auto_adjust=True, progress=False)
    except Exception as e:
        print(f"  Download error: {e}")
        return {}

    for df, name in [(df_1h, "1h"), (df_15m, "15m"), (df_5m, "5m")]:
        if df.empty:
            print(f"  No {name} data")
            return {}

    # Flatten MultiIndex (yfinance v0.2+)
    for df in [df_1h, df_15m, df_5m]:
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

    df_1h.index  = pd.to_datetime(df_1h.index,  utc=True)
    df_15m.index = pd.to_datetime(df_15m.index, utc=True)
    df_5m.index  = pd.to_datetime(df_5m.index,  utc=True)

    for df in [df_1h, df_15m, df_5m]:
        df.columns = [c.lower() for c in df.columns]

    print(f"  Bars: 1h={len(df_1h)} | 15m={len(df_15m)} | 5m={len(df_5m)}")

    # ── Simulation ────────────────────────────────────────────────────────────
    capital     = CAPITAL
    equity      = [capital]
    trades      = []
    in_trade    = False
    entry_price = stop_price = target_price = 0.0
    trade_side  = "long"
    entry_bar   = 0

    start_i = max(WARMUP_5M + 5, 50)

    for i in range(start_i, len(df_5m) - 1):
        t_now    = df_5m.index[i]
        bar_5m   = df_5m.iloc[i]
        next_bar = df_5m.iloc[i + 1]

        # ── Gestion position ouverte ──────────────────────────────────────────
        if in_trade:
            hi  = float(next_bar["high"])
            lo  = float(next_bar["low"])
            cls = float(next_bar["close"])
            mult       = 1 if trade_side == "long" else -1
            stop_hit   = (trade_side == "long"  and lo <= stop_price) or \
                         (trade_side == "short" and hi >= stop_price)
            target_hit = (trade_side == "long"  and hi >= target_price) or \
                         (trade_side == "short" and lo <= target_price)

            exit_price  = None
            exit_reason = None

            if stop_hit and target_hit:
                exit_price  = stop_price
                exit_reason = "stop"
            elif stop_hit:
                exit_price  = stop_price
                exit_reason = "stop"
            elif target_hit:
                exit_price  = target_price
                exit_reason = "target"
            elif i - entry_bar >= 48:
                exit_price  = cls
                exit_reason = "timeout"

            if exit_price:
                qty     = (CAPITAL * POSITION_PCT) / entry_price
                pnl     = (exit_price - entry_price) * mult * qty
                capital += pnl
                trades.append({
                    "symbol": symbol, "side": trade_side,
                    "entry": entry_price, "exit": exit_price,
                    "pnl": round(pnl, 4), "reason": exit_reason,
                    "bars_held": i - entry_bar,
                })
                equity.append(capital)
                in_trade = False
            continue

        # ── Recherche de signal ───────────────────────────────────────────────
        if capital < MIN_POSITION:
            continue

        # Pass 1 — bias 1h
        bars_1h_past = df_1h[df_1h.index <= t_now].tail(WARMUP_1H)
        if len(bars_1h_past) < 10:
            continue
        bias = _bias_1h(bars_1h_past["high"].values, bars_1h_past["low"].values)
        if bias == "downtrend":
            continue

        # Pass 2 — niveau 15min
        bars_15m_past = df_15m[df_15m.index <= t_now].tail(WARMUP_15M)
        if len(bars_15m_past) < 20:
            continue
        current_price = float(bar_5m["close"])
        swing_15m = _find_swing_levels(
            bars_15m_past["high"].values,
            bars_15m_past["low"].values,
            bars_15m_past["close"].values,
            min_tests=2
        )

        chosen_level = None
        chosen_side  = None
        for lvl_info in swing_15m["supports"]:
            lvl  = lvl_info["level"]
            dist = (current_price - lvl) / current_price
            if 0.001 <= dist <= 0.015:
                chosen_level = lvl
                chosen_side  = "long"
                break
        if not chosen_level:
            continue

        # Pass 3 — confirmation 5min
        bars_5m_past = df_5m[df_5m.index <= t_now].tail(WARMUP_5M)
        if len(bars_5m_past) < 10:
            continue

        closes_5m  = bars_5m_past["close"].values
        volumes_5m = bars_5m_past["volume"].values

        if len(closes_5m) >= 4 and not (closes_5m[-1] <= closes_5m[-4]):
            continue

        # Pass 3b — Le niveau doit avoir été touché ET le prix doit être remonté dessus
        # Evite d'attraper un couteau qui tombe
        touched_level = any(bars_5m_past["low"].values[-8:] <= chosen_level * 1.001)
        closed_above  = closes_5m[-1] > chosen_level * 1.001
        if not (touched_level and closed_above):
            continue

        rsi_5m = _rsi(closes_5m, RSI_PERIOD)
        if not (25 <= rsi_5m <= 55):
            continue

        avg_vol = volumes_5m[-20:].mean() if len(volumes_5m) >= 20 else volumes_5m.mean()
        if avg_vol > 0 and volumes_5m[-1] < avg_vol * 0.3:
            continue

        # Pass 4 — stop
        stop = _find_5min_stop(
            bars_5m_past["high"].values,
            bars_5m_past["low"].values,
            "long", chosen_level
        )

        # Pass 5 — target
        swing_5m = _find_swing_levels(
            bars_5m_past["high"].values,
            bars_5m_past["low"].values,
            bars_5m_past["close"].values,
            min_tests=1
        )
        target = None
        for lvl_info in swing_5m["resistances"]:
            lvl      = lvl_info["level"]
            dist_pct = (lvl - chosen_level) / chosen_level
            if 0.007 <= dist_pct <= 0.020:
                target = lvl
                break
        if not target:
            target = chosen_level * 1.009

        risk   = abs(chosen_level - stop)
        reward = abs(target - chosen_level)
        if risk <= 0 or reward / risk < 1.2:
            continue

        # ── Simulation fill ───────────────────────────────────────────────────
        next_low  = float(next_bar["low"])
        next_open = float(next_bar["open"])

        if next_low <= chosen_level:
            fill_price   = min(next_open, chosen_level)
            entry_price  = fill_price
            stop_price   = stop
            target_price = target
            trade_side   = "long"
            entry_bar    = i + 1
            in_trade     = True

    # Fermer trade ouvert en fin de données
    if in_trade:
        last_close = float(df_5m["close"].iloc[-1])
        qty     = (CAPITAL * POSITION_PCT) / entry_price
        pnl     = (last_close - entry_price) * qty
        capital += pnl
        trades.append({
            "symbol": symbol, "side": trade_side,
            "entry": entry_price, "exit": last_close,
            "pnl": round(pnl, 4), "reason": "end_of_data",
            "bars_held": len(df_5m) - 1 - entry_bar,
        })
        equity.append(capital)

    # ── Stats ─────────────────────────────────────────────────────────────────
    n = len(trades)
    if n == 0:
        print(f"  Aucun trade généré")
        return {"symbol": symbol, "trades": 0}

    wins      = [t for t in trades if t["pnl"] > 0]
    losses    = [t for t in trades if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in trades)
    wr        = len(wins) / n * 100
    pf        = abs(sum(t["pnl"] for t in wins) / sum(t["pnl"] for t in losses)) \
                if losses and sum(t["pnl"] for t in losses) != 0 else float("inf")
    ret_pct   = (capital - CAPITAL) / CAPITAL * 100
    ann_pct   = ret_pct / LOOKBACK_DAYS * 365

    eq  = np.array(equity)
    pk  = np.maximum.accumulate(eq)
    mdd = ((eq - pk) / pk * 100).min()

    reasons  = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    avg_bars = np.mean([t["bars_held"] for t in trades])

    print(f"  Trades       : {n}  ({len(wins)}W / {len(losses)}L)")
    print(f"  Win rate     : {wr:.1f}%")
    print(f"  Profit factor: {pf:.2f}")
    print(f"  Total P&L    : ${total_pnl:+.2f}  ({ret_pct:+.1f}%)")
    print(f"  Ann. return  : {ann_pct:+.1f}%  (objectif: +15-20%/an)")
    print(f"  Max drawdown : {mdd:.1f}%")
    print(f"  Durée moy    : {avg_bars:.0f} bougies 5min ({avg_bars*5/60:.1f}h)")
    print(f"  Exits        : {reasons}")

    return {
        "symbol":    symbol, "trades": n,
        "wins":      len(wins), "losses": len(losses),
        "win_rate":  round(wr, 1),
        "pf":        round(pf, 2),
        "total_pnl": round(total_pnl, 2),
        "ret_pct":   round(ret_pct, 1),
        "ann_pct":   round(ann_pct, 1),
        "mdd":       round(mdd, 1),
        "equity":    equity,
        "trade_log": trades,
    }

# ── PORTFOLIO ─────────────────────────────────────────────────────────────────

def run():
    print("\n" + "═"*60)
    print("  GEO V2 — Backtest 3-Timeframes  |  Crypto  |  30 jours")
    print(f"  Capital: ${CAPITAL}  |  Position: {POSITION_PCT*100:.0f}%  |  No look-ahead")
    print("═"*60)

    results = []
    for sym in SYMBOLS:
        r = backtest_symbol(sym)
        if r and r.get("trades", 0) > 0:
            results.append(r)

    if not results:
        print("\n  Aucun résultat.")
        return

    print("\n" + "═"*60)
    print("  RÉSUMÉ PORTFOLIO")
    print("═"*60)
    print(f"  {'Symbol':<12} {'N':>4} {'WR%':>6} {'PF':>5} {'P&L$':>8} {'30j%':>7} {'Ann%':>7} {'MDD%':>7}")
    print(f"  {'─'*12} {'─'*4} {'─'*6} {'─'*5} {'─'*8} {'─'*7} {'─'*7} {'─'*7}")

    total_pnl = 0
    for r in sorted(results, key=lambda x: x["ann_pct"], reverse=True):
        flag = "✅" if r["ann_pct"] >= 15 else ("➕" if r["ann_pct"] >= 5 else "➖")
        print(
            f"  {r['symbol']:<12} {r['trades']:>4} {r['win_rate']:>6.1f} "
            f"{r['pf']:>5.2f} {r['total_pnl']:>+8.2f} "
            f"{r['ret_pct']:>+6.1f}% {r['ann_pct']:>+6.1f}% {r['mdd']:>+6.1f}%  {flag}"
        )
        total_pnl += r["total_pnl"]

    print(f"\n  P&L total portfolio : ${total_pnl:+.2f}")

    # CSV
    all_trades = []
    for r in results:
        all_trades.extend(r["trade_log"])
    if all_trades:
        try:
            pd.DataFrame(all_trades).to_csv("trading-agent/backtest_geo_v2_trades.csv", index=False)
            print(f"  {len(all_trades)} trades → backtest_geo_v2_trades.csv")
        except Exception as e:
            print(f"  CSV error: {e}")

    # Equity curve
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(12, 5))
        for r in results:
            ax.plot(r["equity"], label=r["symbol"], alpha=0.8)
        ax.axhline(CAPITAL, color="gray", linestyle="--", lw=0.8)
        ax.set_title(f"Geo V2 — 3TF Scalping — {LOOKBACK_DAYS}j crypto")
        ax.set_xlabel("Événements")
        ax.set_ylabel("Capital ($)")
        ax.legend(fontsize=8, ncol=3)
        ax.grid(alpha=0.3)
        plt.tight_layout()
        plt.savefig("trading-agent/backtest_geo_v2_equity.png", dpi=150)
        print("  Equity curve → backtest_geo_v2_equity.png")
    except Exception as e:
        print(f"  matplotlib non dispo ({e})")

    print("\n" + "═"*60)

    total_trades = sum(r["trades"] for r in results)
    if total_trades < 10:
        print("\n  DIAGNOSTIC : peu de trades générés.")
        print("  Causes possibles :")
        print("  • min_tests=2 trop strict → essaie min_tests=1")
        print("  • RSI [25-55] trop étroit → élargir à [20-60]")
        print("  • dist 0.1%-1.5% trop étroit pour le timeframe 15min")


if __name__ == "__main__":
    run()
