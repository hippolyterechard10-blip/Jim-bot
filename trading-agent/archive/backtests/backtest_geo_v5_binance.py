"""
backtest_geo_v5_binance.py — Geo V4 sur données Binance, résultats par année
=============================================================================
Source : API publique Binance (gratuit, pas d'auth)
Data   : 5min bars téléchargés depuis 2017, 15min/1h reconstruits par resampling
Stratégie : exactement V4 (zones ±0.3%, RSI divergence, stop dynamique, Pass 3b)
Output : tableau par année + equity curve PNG

Run:
    pip install requests pandas numpy matplotlib --break-system-packages
    python backtest_geo_v5_binance.py
"""

import warnings
warnings.filterwarnings("ignore")
import sys, time, os
from datetime import datetime, timezone
import numpy as np
import pandas as pd

try:
    import requests
except ImportError:
    print("pip install requests --break-system-packages")
    sys.exit(1)

# ── CONFIG ────────────────────────────────────────────────────────────────────

ASSETS = {
    "ETHUSDT":  {"start": "2023-01-01", "end": "2023-12-31", "target": 0.009, "pos_pct": 0.28, "max_sim": 2, "pool": 700},
    "AVAXUSDT": {"start": "2023-01-01", "end": "2023-12-31", "target": 0.010, "pos_pct": 0.20, "max_sim": 1, "pool": 200},
    "LINKUSDT": {"start": "2023-01-01", "end": "2023-12-31", "target": 0.010, "pos_pct": 0.20, "max_sim": 1, "pool": 100},
}

CAPITAL      = 1000.0
ZONE_PCT     = 0.003       # Zone ±0.3%
MAX_TOUCHES  = 2           # Skip zone si touchée > 2 fois
RSI_LOW      = 20
RSI_HIGH     = 65
CACHE_DIR    = "binance_cache"   # Cache local pour éviter re-téléchargements

# ── BINANCE DATA DOWNLOADER ───────────────────────────────────────────────────

BASE_URL = "https://api.binance.us/api/v3/klines"


def _ms(dt_str):
    return int(datetime.strptime(dt_str, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


def download_binance(symbol, start_date, interval="15m", cache=True, end_date=None):
    """
    Télécharge les klines Binance depuis start_date jusqu'à end_date (ou aujourd'hui).
    Cache local en parquet pour éviter de re-télécharger.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    end_suffix = f"_{end_date}" if end_date else ""
    cache_file = os.path.join(CACHE_DIR, f"{symbol}_{interval}_{start_date}{end_suffix}.parquet")

    if cache and os.path.exists(cache_file):
        df = pd.read_parquet(cache_file)
        last_ts = int(df.index[-1].timestamp() * 1000)
        now_ms  = int(datetime.now(timezone.utc).timestamp() * 1000)
        if end_date or now_ms - last_ts < 7 * 24 * 3600 * 1000:
            print(f"        Cache OK: {symbol} {interval} ({len(df)} bars)")
            return df
        else:
            start_ms = last_ts + 60_000
            print(f"        Mise à jour cache: {symbol} depuis {df.index[-1].date()}")
    else:
        start_ms = _ms(start_date)
        label = f" → {end_date}" if end_date else ""
        print(f"        Téléchargement: {symbol} depuis {start_date}{label}")

    end_ms = _ms(end_date) + 86_400_000 if end_date else int(datetime.now(timezone.utc).timestamp() * 1000)
    all_rows = []
    batch    = 0

    while start_ms < end_ms:
        try:
            resp = requests.get(BASE_URL, params={
                "symbol":    symbol,
                "interval":  interval,
                "startTime": start_ms,
                "limit":     1000,
            }, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"        Erreur téléchargement {symbol}: {e} — retry 5s")
            time.sleep(5)
            continue

        if not data:
            break

        all_rows.extend(data)
        start_ms = data[-1][0] + 60_000
        batch   += 1

        if batch % 50 == 0:
            n = len(all_rows)
            d = datetime.fromtimestamp(all_rows[-1][0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            print(f"        ... {n:,} bars jusqu'à {d}")

        time.sleep(0.08)   # rate limit Binance : max 1200 req/min

    if not all_rows:
        return pd.DataFrame()

    df_new = pd.DataFrame(all_rows, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "qvolume", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore",
    ])
    df_new["open_time"] = pd.to_datetime(df_new["open_time"], unit="ms", utc=True)
    df_new.set_index("open_time", inplace=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df_new[col] = df_new[col].astype(float)
    df_new = df_new[["open", "high", "low", "close", "volume"]]
    df_new = df_new[~df_new.index.duplicated(keep="last")]

    if cache and os.path.exists(cache_file):
        df_old = pd.read_parquet(cache_file)
        df_new = pd.concat([df_old, df_new])
        df_new = df_new[~df_new.index.duplicated(keep="last")].sort_index()

    df_new.to_parquet(cache_file)
    print(f"        {symbol}: {len(df_new):,} bars ({df_new.index[0].date()} → {df_new.index[-1].date()})")
    return df_new


def resample_ohlcv(df_5m, rule):
    """Reconstruit 15min ou 1h depuis les bars 5min."""
    return df_5m.resample(rule).agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    }).dropna()

# ── HELPERS STRATÉGIE (identiques V4) ────────────────────────────────────────

def _rsi(closes, period=14):
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


def _find_zones(highs, lows, closes, min_tests=1):
    current    = closes[-1]
    swing_lows = []
    for i in range(2, len(highs) - 2):
        if (lows[i] < lows[i-1] and lows[i] < lows[i-2]
                and lows[i] < lows[i+1] and lows[i] < lows[i+2]):
            swing_lows.append((lows[i], highs[i]))

    if not swing_lows:
        return []
    swing_lows.sort(key=lambda x: x[0])
    clusters = [[swing_lows[0]]]
    for v in swing_lows[1:]:
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
                "wick_low": wick_low,
                "tests":    len(c),
            })
    zones.sort(key=lambda x: x["center"], reverse=True)
    return zones


def _rsi_divergence(closes, rsi_now):
    if len(closes) < 5:
        return False
    rsi_prev    = _rsi(np.array(closes[:-3]), 14)
    price_lower = closes[-1] < closes[-4]
    rsi_higher  = rsi_now > rsi_prev
    return price_lower and rsi_higher


def _dynamic_stop(lows_5m, entry_level, wick_low):
    floor     = entry_level * 0.992
    candidate = min(lows_5m[-8:]) * 0.999 if len(lows_5m) >= 8 else wick_low * 0.999
    if floor <= candidate < entry_level:
        return candidate
    zone_stop = wick_low * 0.999
    if floor <= zone_stop < entry_level:
        return zone_stop
    return entry_level * 0.997


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
    mag = max(1, int(round(-np.log10(center * 0.001))))
    return round(center, mag)

# ── BACKTEST ANNÉE PAR ANNÉE ──────────────────────────────────────────────────

def backtest_one_year(df_5m_year, df_15m, df_1h, cfg):
    """
    Backteste une année de données 5min.
    df_15m et df_1h contiennent l'historique COMPLET (pour warmup correct).
    """
    from collections import defaultdict

    capital  = CAPITAL
    equity   = [capital]
    trades   = []
    open_pos = {}
    touches  = defaultdict(int)

    idx_5m  = df_5m_year.index
    n       = len(idx_5m)
    start_i = 60  # warmup

    for i in range(start_i, n - 1):
        t_now  = idx_5m[i]
        bar    = df_5m_year.iloc[i]
        next_b = df_5m_year.iloc[i + 1]

        # ── Gestion positions ─────────────────────────────────────────────
        for zk in list(open_pos.keys()):
            pos = open_pos[zk]
            hi  = float(next_b["high"])
            lo  = float(next_b["low"])
            cls = float(next_b["close"])
            sh  = lo <= pos["stop"]
            th  = hi >= pos["target"]
            to  = (i - pos["bar"]) >= 48

            ep = er = None
            if sh and th:
                ep, er = pos["stop"],   "stop"
            elif sh:
                ep, er = pos["stop"],   "stop"
            elif th:
                ep, er = pos["target"], "target"
            elif to:
                ep, er = cls,           "timeout"

            if ep is not None:
                pnl = (ep - pos["entry"]) * pos["qty"]
                capital += pnl
                trades.append({
                    "t": t_now, "entry": pos["entry"],
                    "exit": ep, "pnl": round(pnl, 4), "reason": er,
                })
                equity.append(capital)
                del open_pos[zk]

        if capital < 20:
            continue

        # Limite simultané
        if len(open_pos) >= cfg["max_sim"]:
            continue

        # ── Données fenêtrées (no look-ahead) ────────────────────────────
        bars_1h_w  = df_1h[df_1h.index <= t_now].tail(50)
        bars_15m_w = df_15m[df_15m.index <= t_now].tail(100)
        bars_5m_w  = df_5m_year[df_5m_year.index <= t_now].tail(30)

        if len(bars_1h_w) < 10 or len(bars_15m_w) < 20 or len(bars_5m_w) < 10:
            continue

        # Pass 1 — Bias 1h
        if _bias_1h(bars_1h_w["high"].values, bars_1h_w["low"].values) == "downtrend":
            continue

        current   = float(bar["close"])
        closes_5m = bars_5m_w["close"].values
        vols_5m   = bars_5m_w["volume"].values
        rsi_now   = _rsi(closes_5m, 14)

        avg_vol = vols_5m[-20:].mean() if len(vols_5m) >= 20 else vols_5m.mean()
        if avg_vol > 0 and vols_5m[-1] < avg_vol * 0.3:
            continue

        # Pass 2 — Zones 15min
        zones = _find_zones(
            bars_15m_w["high"].values, bars_15m_w["low"].values,
            bars_15m_w["close"].values, min_tests=1,
        )

        for zone in zones:
            zk = _zone_key(zone["center"])
            if zk in open_pos:
                continue
            if touches[zk] >= MAX_TOUCHES:
                continue

            dist = (current - zone["center"]) / current
            if not (0.001 <= dist <= 0.020):
                continue

            if not (RSI_LOW <= rsi_now <= RSI_HIGH):
                continue
            div = _rsi_divergence(closes_5m, rsi_now)
            if not div and not (30 <= rsi_now <= 55):
                continue

            # Pass 3b — touché ET remonté
            touched      = any(bars_5m_w["low"].values[-8:] <= zone["high"])
            closed_above = closes_5m[-1] > zone["low"]
            if not (touched and closed_above):
                continue

            # Stop + target
            stop   = _dynamic_stop(bars_5m_w["low"].values, zone["center"], zone["wick_low"])
            target = zone["center"] * (1 + cfg["target"])

            risk   = abs(zone["center"] - stop)
            reward = abs(target - zone["center"])
            if risk <= 0 or reward / risk < 1.2:
                continue

            # Fill
            if float(next_b["low"]) <= zone["high"]:
                fill = min(float(next_b["open"]), zone["center"])
                qty  = (cfg["pool"] * cfg["pos_pct"]) / fill
                touches[zk] += 1
                open_pos[zk] = {
                    "entry": fill, "stop": stop, "target": target,
                    "qty": qty, "bar": i + 1,
                }

    # Fermer restants
    if open_pos:
        last = float(df_5m_year["close"].iloc[-1])
        for zk, pos in open_pos.items():
            pnl = (last - pos["entry"]) * pos["qty"]
            capital += pnl
            trades.append({
                "t": df_5m_year.index[-1], "entry": pos["entry"],
                "exit": last, "pnl": round(pnl, 4), "reason": "end",
            })
        equity.append(capital)

    return trades, capital, equity

# ── MAIN ──────────────────────────────────────────────────────────────────────

def run():
    print("\n" + "═" * 70)
    print("        GEO V5 — Backtest Multi-Années Binance")
    print(f"        Strat: Zones ±{ZONE_PCT*100:.1f}% | RSI div | Stop dynamique | Pass 3b")
    print("═" * 70)

    all_results = {}   # symbol → {year → {trades, pnl, ret, ann}}

    for symbol, cfg in ASSETS.items():
        print(f"\n{'─'*70}")
        print(f"        {symbol}")

        # Téléchargement 15min (3× moins de bars que 5min)
        df_5m = download_binance(symbol, cfg["start"], interval="15m", end_date=cfg.get("end"))
        if df_5m.empty:
            print(f"        Pas de données pour {symbol}")
            continue

        # Reconstruction 1h depuis les 15min (pas de 5min disponible)
        df_15m = df_5m.copy()             # 15min = timeframe principal
        df_1h  = resample_ohlcv(df_5m, "1h")

        years          = sorted(df_5m.index.year.unique())
        symbol_results = {}

        print(f"\n        {'Année':<8} {'N':>5} {'WR%':>7} {'PF':>5} {'P&L$':>8} {'30j%':>7} {'Ann%':>8} {'MDD%':>7}")
        print(f"        {'─'*8} {'─'*5} {'─'*7} {'─'*5} {'─'*8} {'─'*7} {'─'*8} {'─'*7}")

        for year in years:
            df_y = df_5m[df_5m.index.year == year]
            if len(df_y) < 500:
                continue

            trades, cap_final, equity = backtest_one_year(df_y, df_15m, df_1h, cfg)

            n = len(trades)
            if n == 0:
                print(f"        {year:<8} {'0':>5}")
                continue

            wins  = [t for t in trades if t["pnl"] > 0]
            losses = [t for t in trades if t["pnl"] <= 0]
            total = sum(t["pnl"] for t in trades)
            wr    = len(wins) / n * 100
            sum_l = sum(t["pnl"] for t in losses)
            pf    = abs(sum(t["pnl"] for t in wins) / sum_l) if sum_l else 99.0
            days  = max(1, (df_y.index[-1] - df_y.index[0]).days)
            ret   = (cap_final - CAPITAL) / CAPITAL * 100
            ann   = ret / days * 365

            eq  = np.array([CAPITAL] + [CAPITAL + sum(t["pnl"] for t in trades[:k+1])
                                        for k in range(len(trades))])
            pk  = np.maximum.accumulate(eq)
            mdd = ((eq - pk) / pk * 100).min()

            symbol_results[year] = {
                "n": n, "wins": len(wins), "wr": round(wr, 1),
                "pf": round(pf, 2), "total": round(total, 2),
                "ret": round(ret, 1), "ann": round(ann, 1),
                "mdd": round(mdd, 1), "days": days, "trades": trades,
            }

            flag = "✅" if ann >= 15 else ("➕" if ann >= 5 else "➖")
            print(
                f"        {year:<8} {n:>5} {wr:>7.1f} {pf:>5.2f} "
                f"{total:>+8.2f} {ret/days*30:>+6.1f}% {ann:>+7.1f}%  {mdd:>+6.1f}%  {flag}"
            )

        all_results[symbol] = symbol_results

    # ── Tableau croisé : tous assets par année ────────────────────────────
    print("\n" + "═" * 70)
    print("        VUE CROISÉE — Ann% par asset et par année")
    print("═" * 70)

    all_years = sorted({y for sr in all_results.values() for y in sr})
    header = f"        {'Année':<8}" + "".join(f" {s[:8]:>10}" for s in all_results)
    print(header)
    print("        " + "─" * (8 + 11 * len(all_results)))

    for year in all_years:
        row = f"        {year:<8}"
        for symbol, sr in all_results.items():
            if year in sr:
                a    = sr[year]["ann"]
                flag = "✅" if a >= 15 else ("➕" if a >= 5 else "➖")
                row += f" {a:>+7.1f}% {flag}"
            else:
                row += f"  {'N/A':>9}"
        print(row)

    # ── Stats globales par asset ───────────────────────────────────────────
    print("\n" + "═" * 70)
    print("        MOYENNES GLOBALES PAR ASSET")
    print(f"        {'Asset':<12} {'Années':>6} {'WR%moy':>8} {'PFmoy':>7} {'Ann%moy':>9} {'MDDmin%':>9}")
    print(f"        {'─'*12} {'─'*6} {'─'*8} {'─'*7} {'─'*9} {'─'*9}")

    for symbol, sr in all_results.items():
        if not sr:
            continue
        wrs  = [v["wr"]  for v in sr.values()]
        pfs  = [v["pf"]  for v in sr.values()]
        anns = [v["ann"] for v in sr.values()]
        mdds = [v["mdd"] for v in sr.values()]
        flag = "✅" if np.mean(anns) >= 15 else ("➕" if np.mean(anns) >= 8 else "➖")
        print(
            f"        {symbol:<12} {len(sr):>6} {np.mean(wrs):>8.1f} "
            f"{np.mean(pfs):>7.2f} {np.mean(anns):>+8.1f}%  "
            f"{min(mdds):>+8.1f}%  {flag}"
        )

    # ── CSV global ────────────────────────────────────────────────────────
    rows = []
    for symbol, sr in all_results.items():
        for year, v in sr.items():
            rows.append({"symbol": symbol, "year": year,
                         **{k: v[k] for k in ["n", "wr", "pf", "total", "ret", "ann", "mdd"]}})
    if rows:
        pd.DataFrame(rows).to_csv("backtest_geo_v5_by_year.csv", index=False)
        print(f"\n        Résultats → backtest_geo_v5_by_year.csv")

    # ── Equity curve ETH par année ────────────────────────────────────────
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.cm as cm

        eth_sr = all_results.get("ETHUSDT", {})
        if eth_sr:
            fig, axes = plt.subplots(
                1, len(eth_sr), figsize=(4 * len(eth_sr), 4), sharey=False
            )
            if len(eth_sr) == 1:
                axes = [axes]
            colors = cm.tab10(np.linspace(0, 1, len(eth_sr)))
            for ax, (year, v), color in zip(axes, sorted(eth_sr.items()), colors):
                eq_pts = [CAPITAL]
                for t in v["trades"]:
                    eq_pts.append(eq_pts[-1] + t["pnl"])
                ax.plot(eq_pts, color=color, linewidth=1)
                ax.axhline(CAPITAL, color="gray", linestyle="--", linewidth=0.7)
                ax.set_title(f"{year}\n{v['ann']:+.1f}%/an | WR {v['wr']}% | PF {v['pf']}")
                ax.set_xlabel("Trades")
                ax.set_ylabel("Capital ($)" if ax == axes[0] else "")
                ax.grid(alpha=0.3)

            plt.suptitle("Geo V5 — ETHUSDT par année (Binance 5min)", y=1.02)
            plt.tight_layout()
            plt.savefig("backtest_geo_v5_eth_by_year.png", dpi=130, bbox_inches="tight")
            print("        Equity curves → backtest_geo_v5_eth_by_year.png")
    except Exception as e:
        print(f"        Chart skipped: {e}")

    print("\n" + "═" * 70 + "\n")


if __name__ == "__main__":
    run()
