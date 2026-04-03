"""
backtest_2022_2023.py — GEO V4 · ETH+SOL · Binance US · 2022 + 2023
=====================================================================
Code identique à backtest_2025.py + FILL RATE 65% (limit orders réalistes)
Périodes : 2022 Bear (Jan–Déc) + 2023 Recovery (Jan–Déc)
Capital  : $1000 · pos 50% · max 2 positions simultanées
Warmup   : 90 jours avant chaque période (données déjà en cache)
"""
import warnings; warnings.filterwarnings("ignore")
import sys, time, os, bisect, random
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
from collections import defaultdict

try:
    import requests
except ImportError:
    print("pip install requests --break-system-packages"); sys.exit(1)

# ── CONFIG — identique au live ─────────────────────────────────────────────────
SYMBOLS     = {"ETH": "ETHUSD", "SOL": "SOLUSD"}
CAPITAL     = 1000.0
POS_PCT     = 0.50
MAX_SIM     = 2
ZONE_PCT    = 0.003
MAX_TOUCHES = 2
RSI_LOW     = 20
RSI_HIGH    = 65
TARGET_PCT  = 0.009
TIMEOUT_B   = 48           # 48 × 5min = 4h
FILL_RATE   = 0.65         # 65% fill rate limit orders
RNG_SEED    = 42
CACHE_DIR   = "binance_us_cache"
BASE        = "https://api.binance.us/api/v3/klines"

# Périodes avec warmup 90j inclus
PERIODS = {
    "2022 Bear": {
        "dl_start": "2021-10-01", "dl_end": "2022-12-31",
        "test_start": "2022-01-01", "test_end": "2023-01-01",
    },
    "2023 Recovery": {
        "dl_start": "2022-10-01", "dl_end": "2023-12-31",
        "test_start": "2023-01-01", "test_end": "2024-01-01",
    },
}

# ── DOWNLOAD ────────────────────────────────────────────────────────────────────
def _ms(dt):
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)

def download(symbol, start_str, end_str):
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache = os.path.join(CACHE_DIR, f"{symbol}_5m_{start_str}_{end_str}.parquet")
    if os.path.exists(cache):
        df = pd.read_parquet(cache)
        print(f"  [cache] {symbol} {start_str[:7]}: {len(df):,} barres 5m")
        return df
    print(f"  [dl] {symbol}: {start_str} → {end_str} ...")
    start_dt = datetime.strptime(start_str, "%Y-%m-%d")
    end_dt   = datetime.strptime(end_str,   "%Y-%m-%d")
    start_ms = _ms(start_dt); end_ms = _ms(end_dt)
    rows, batches = [], 0
    while start_ms < end_ms:
        try:
            r = requests.get(BASE, params={
                "symbol": symbol, "interval": "5m",
                "startTime": start_ms, "endTime": end_ms, "limit": 1000,
            }, timeout=20)
            data = r.json()
        except Exception as e:
            print(f"  retry: {e}"); time.sleep(5); continue
        if not data or isinstance(data, dict): break
        rows.extend(data)
        start_ms = data[-1][0] + 300_000
        batches += 1
        if batches % 100 == 0:
            d = datetime.fromtimestamp(rows[-1][0]/1000, tz=timezone.utc).strftime("%Y-%m")
            print(f"    ... {len(rows):,} barres → {d}")
        time.sleep(0.05)
    df = pd.DataFrame(rows, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","qv","trades","tbv","tqv","ignore"
    ])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df.set_index("open_time", inplace=True)
    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype(float)
    df = df[["open","high","low","close","volume"]]
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.to_parquet(cache)
    print(f"  → {len(df):,} barres ({df.index[0].date()} → {df.index[-1].date()})")
    return df

def resample(df, rule):
    return df.resample(rule).agg(
        {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
    ).dropna()

# ── INDICATEURS (identiques backtest_2025.py) ──────────────────────────────────
def _rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    d  = np.diff(np.array(closes, dtype=float))
    g  = np.where(d > 0, d, 0.0); l = np.where(d < 0, -d, 0.0)
    ag = g[-period:].mean(); al = l[-period:].mean()
    if al == 0: return 100.0
    return round(100 - 100 / (1 + ag / al), 2)

def _find_zones(highs, lows, closes):
    current = closes[-1]; sw = []
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

# ── SIGNAL (identique backtest_2025.py) ────────────────────────────────────────
def get_signal(sym, h5, l5, c5, v5, h15, l15, c15, h1h, l1h):
    if len(c5) < 30 or len(c15) < 20 or len(h1h) < 10: return None
    if h1h[-1] < h1h[-4] and l1h[-1] < l1h[-4]: return None
    cl5 = c5[-30:]; vo5 = v5[-30:]
    rsi = _rsi(cl5, 14)
    avgv = vo5[:-1][-20:].mean() if len(vo5) > 1 else 1.0
    if avgv > 0 and vo5[-2] < avgv * 0.3: return None
    zones = _find_zones(h15[-100:], l15[-100:], c15[-100:])
    curr  = float(cl5[-1])
    for zone in zones:
        dist = (curr - zone["center"]) / curr
        if not (0.001 <= dist <= 0.020): continue
        if not (RSI_LOW <= rsi <= RSI_HIGH): continue
        div = _rsi_div(cl5, rsi)
        if not div and not (30 <= rsi <= 55): continue
        if not any(l5[-8:] <= zone["high"]): continue
        if cl5[-1] <= zone["low"]: continue
        stop   = _dyn_stop(l5[-8:], zone["center"], zone["wick_low"])
        target = round(zone["high"] * (1 + TARGET_PCT), 2)
        risk   = abs(zone["center"] - stop)
        reward = abs(target - zone["center"])
        if risk <= 0 or reward / risk < 1.2: continue
        return {"zone": zone, "stop": stop, "target": target}
    return None

# ── BACKTEST (numpy) ────────────────────────────────────────────────────────────
def run(symbols, all_data, test_start_ts, test_end_ts, rng):
    arrays = {}
    for sym in symbols:
        df5  = all_data[sym]["5m"]
        df15 = all_data[sym]["15m"]
        df1h = all_data[sym]["1h"]
        arrays[sym] = {
            "idx5":  df5.index,  "idx15": df15.index, "idx1h": df1h.index,
            "h5": df5["high"].values,  "l5": df5["low"].values,
            "c5": df5["close"].values, "v5": df5["volume"].values,
            "o5": df5["open"].values,
            "h15": df15["high"].values, "l15": df15["low"].values,
            "c15": df15["close"].values,
            "h1h": df1h["high"].values, "l1h": df1h["low"].values,
        }

    ref_idx = sorted(set(arrays[symbols[0]]["idx5"]).intersection(
        *[set(arrays[s]["idx5"]) for s in symbols[1:]]
    ))
    n = len(ref_idx)
    pos_maps = {sym: {ts: i for i, ts in enumerate(arrays[sym]["idx5"])} for sym in symbols}

    def tf_pos(idx_list, ts):
        return max(0, bisect.bisect_right(idx_list, ts) - 1)

    capital  = CAPITAL
    trades   = []
    open_pos = {}
    touches  = defaultdict(int)
    filled = skipped = 0

    for i in range(55, n - 1):
        t_now  = ref_idx[i]
        t_next = ref_idx[i + 1]
        if t_now >= test_end_ts: break

        # Fermer positions
        for zk in list(open_pos.keys()):
            p   = open_pos[zk]; sym = p["sym"]
            pm  = pos_maps[sym]
            if t_next not in pm: continue
            ni  = pm[t_next]; arr = arrays[sym]
            hi = arr["h5"][ni]; lo = arr["l5"][ni]; cl = arr["c5"][ni]
            sh = lo <= p["stop"]; th = hi >= p["target"]; to = (i - p["bar"]) >= TIMEOUT_B
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
                    "day": t_now.date(), "month": t_now.strftime("%Y-%m"),
                })
                del open_pos[zk]

        if capital < 20 or len(open_pos) >= MAX_SIM: continue
        if t_now < test_start_ts: continue

        for sym in symbols:
            if len(open_pos) >= MAX_SIM: break
            arr = arrays[sym]
            ci5  = pos_maps[sym].get(t_now)
            if ci5 is None: continue
            ci15 = tf_pos(arr["idx15"], t_now)
            ci1h = tf_pos(arr["idx1h"], t_now)

            sig = get_signal(
                sym,
                arr["h5"][:ci5+1],  arr["l5"][:ci5+1],
                arr["c5"][:ci5+1],  arr["v5"][:ci5+1],
                arr["h15"][:ci15+1], arr["l15"][:ci15+1], arr["c15"][:ci15+1],
                arr["h1h"][:ci1h+1], arr["l1h"][:ci1h+1],
            )
            if sig is None: continue

            zone = sig["zone"]
            key  = _zk(sym, zone["center"])
            if touches[key] >= MAX_TOUCHES or key in open_pos: continue

            pm = pos_maps[sym]
            if t_next not in pm: continue
            ni = pm[t_next]
            lo_next = arr["l5"][ni]; op_next = arr["o5"][ni]

            if lo_next <= zone["high"]:
                # ── FILL RATE 65% ────────────────────────────────────────────
                if rng.random() > FILL_RATE:
                    skipped += 1
                    continue
                filled += 1
                fill = min(op_next, zone["center"])
                qty  = (CAPITAL * POS_PCT) / fill
                touches[key] += 1
                open_pos[key] = {
                    "sym": sym, "entry": fill,
                    "stop": sig["stop"], "target": sig["target"],
                    "qty": qty, "bar": i,
                }

    # Fermer positions restantes
    for key, p in open_pos.items():
        sym  = p["sym"]
        last = float(arrays[sym]["c5"][-1])
        pnl  = (last - p["entry"]) * p["qty"]
        capital += pnl
        trades.append({
            "sym": sym, "entry": p["entry"], "exit": last,
            "pnl": round(pnl, 4), "reason": "end",
            "day": ref_idx[-1].date(), "month": ref_idx[-1].strftime("%Y-%m"),
        })

    return trades, capital, filled, skipped

# ── STATS ───────────────────────────────────────────────────────────────────────
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
    pk     = np.maximum.accumulate(eq); mdd = ((eq - pk) / pk * 100).min()
    return {
        "n": n, "wr": round(wr,1), "pf": round(pf,2),
        "total": round(total,2), "ann": round(ann,1), "mdd": round(mdd,1),
        "capital": round(capital,2),
        "stops":   len([t for t in trades if t["reason"] == "stop"]),
        "targets": len([t for t in trades if t["reason"] == "target"]),
        "timeouts":len([t for t in trades if t["reason"] in ("timeout","end")]),
        "avg_win": round(sum(t["pnl"] for t in wins)  / len(wins),  4) if wins   else 0,
        "avg_loss":round(sum(t["pnl"] for t in losses)/ len(losses), 4) if losses else 0,
    }

# ── MAIN ────────────────────────────────────────────────────────────────────────
def main():
    print("\n" + "═"*80)
    print("  GEO V4 — BACKTEST 2022 + 2023 — Binance US 5min — Fill rate 65%")
    print("  ETH/USD + SOL/USD | $1000 capital | zone±0.3% | target+0.9% | max 2 pos")
    print("  Limit orders : 65% fill rate (seed=42, reproductible)")
    print("═"*80)

    rng = random.Random(RNG_SEED)

    configs = {
        "ETH-only": ["ETH"],
        "SOL-only": ["SOL"],
        "ETH+SOL":  ["ETH", "SOL"],
    }

    all_period_results = {}

    for period_label, pcfg in PERIODS.items():
        ts_start = pd.Timestamp(pcfg["test_start"], tz="UTC")
        ts_end   = pd.Timestamp(pcfg["test_end"],   tz="UTC")
        days     = (datetime.strptime(pcfg["test_end"],   "%Y-%m-%d") -
                    datetime.strptime(pcfg["test_start"], "%Y-%m-%d")).days

        print(f"\n{'─'*80}")
        print(f"  {period_label} ({pcfg['test_start']} → {pcfg['test_end']})")
        print(f"  Téléchargement (warmup depuis {pcfg['dl_start']})...")

        # Charger données
        all_data = {}
        for sym_key, bn_sym in SYMBOLS.items():
            df5 = download(bn_sym, pcfg["dl_start"], pcfg["dl_end"])
            if df5 is None or df5.empty:
                print(f"  ERREUR: {bn_sym} vide"); continue
            # Filtrer jusqu'à la fin du test
            df5 = df5[df5.index < ts_end]
            all_data[sym_key] = {
                "5m":  df5,
                "15m": resample(df5, "15min"),
                "1h":  resample(df5, "1h"),
            }

        print(f"\n  {'Config':<12} {'N':>5} {'WR%':>6} {'PF':>5} {'P&L $':>9} "
              f"{'Capital':>9} {'Ann%':>8} {'MDD%':>7}  T|S|TO  Fill%")
        print(f"  {'─'*12} {'─'*5} {'─'*6} {'─'*5} {'─'*9} "
              f"{'─'*9} {'─'*8} {'─'*7}  {'─'*8}  {'─'*5}")

        period_results = {}
        all_trades_by_cfg = {}

        for cfg_label, symbols in configs.items():
            trades, capital, filled, skipped = run(
                symbols, all_data, ts_start, ts_end, rng
            )
            s = stats(trades, capital, days)
            period_results[cfg_label] = s
            all_trades_by_cfg[cfg_label] = trades

            if s:
                exits    = f"{s['targets']}|{s['stops']}|{s['timeouts']}"
                fill_pct = filled / (filled + skipped) * 100 if (filled + skipped) > 0 else 0
                flag = "✓" if s["pf"] >= 1.5 and s["total"] >= 0 else ("~" if s["total"] >= 0 else "✗")
                print(f"  [{flag}] {cfg_label:<10} {s['n']:>5} {s['wr']:>6.1f} {s['pf']:>5.2f}"
                      f" {s['total']:>+9.2f} {s['capital']:>9.2f}"
                      f" {s['ann']:>+7.1f}% {s['mdd']:>+6.1f}%  {exits:<8}  {fill_pct:.0f}%")
            else:
                print(f"  [?] {cfg_label:<10}     0  —  — —  —  —  —  —")

        # Détail mensuel ETH+SOL
        eth_sol_trades = all_trades_by_cfg.get("ETH+SOL", [])
        if eth_sol_trades:
            print(f"\n  Détail mensuel — ETH+SOL (fill 65%)")
            year = pcfg["test_start"][:4]
            months = [(f"{m:02d}/{year}", f"{year}-{m:02d}-01",
                       f"{year}-{m+1:02d}-01" if m < 12 else f"{int(year)+1}-01-01")
                      for m in range(1, 13)]
            running_cap = CAPITAL
            for m_label, m_start, m_end in months:
                m_s = datetime.strptime(m_start, "%Y-%m-%d").date()
                m_e = datetime.strptime(m_end,   "%Y-%m-%d").date()
                mt  = [t for t in eth_sol_trades if m_s <= t["day"] < m_e]
                m_pnl = sum(t["pnl"] for t in mt)
                running_cap += m_pnl
                wr = len([t for t in mt if t["pnl"] > 0]) / len(mt) * 100 if mt else 0
                n_t = len([t for t in mt if t["reason"] == "target"])
                n_s = len([t for t in mt if t["reason"] == "stop"])
                bar = ("+" if m_pnl >= 0 else "-") + "█" * min(int(abs(m_pnl)/3), 20)
                print(f"    {m_label}  N={len(mt):>3}  WR={wr:>4.0f}%  "
                      f"P&L={m_pnl:>+7.2f}$  Cap={running_cap:>8.2f}$  {bar}")

        all_period_results[period_label] = period_results

    # ── RÉSUMÉ GLOBAL ───────────────────────────────────────────────────────────
    print(f"\n{'═'*80}")
    print("  RÉSUMÉ GLOBAL — ETH+SOL — 2022 + 2023 cumulés (fill 65%)")
    print(f"{'─'*80}")
    for cfg_label, symbols in configs.items():
        total_n   = sum((all_period_results[p].get(cfg_label) or {}).get("n", 0) for p in PERIODS)
        total_pnl = sum((all_period_results[p].get(cfg_label) or {}).get("total", 0) for p in PERIODS)
        avg_wr    = np.mean([
            (all_period_results[p].get(cfg_label) or {}).get("wr", 0)
            for p in PERIODS
            if (all_period_results[p].get(cfg_label) or {}).get("n", 0) > 0
        ]) if total_n > 0 else 0
        avg_pf = np.mean([
            (all_period_results[p].get(cfg_label) or {}).get("pf", 0)
            for p in PERIODS
            if (all_period_results[p].get(cfg_label) or {}).get("n", 0) > 0
        ]) if total_n > 0 else 0
        flag = "✓" if total_pnl >= 0 else "✗"
        print(f"  [{flag}] {cfg_label:<12}  N={total_n:>5}  WR={avg_wr:.1f}%  PF={avg_pf:.2f}  P&L={total_pnl:>+8.2f}$")

    # Comparaison avec 2025 sans fill rate
    print(f"\n{'─'*80}")
    print("  COMPARAISON vs backtest_2025 (sans fill rate)")
    print(f"  2025 : N=684  WR=43.0%  PF=3.65  P&L=+$1035  Ann=+103.5%  MDD=-0.7%")
    print(f"  → Ci-dessus : ETH+SOL avec fill 65% sur marchés bear/recovery")
    print("═"*80 + "\n")

if __name__ == "__main__":
    main()
