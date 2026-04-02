"""
backtest_2025_5m.py — ETH+SOL 2025 en VRAIS 5min
==================================================
Source : Binance US API (api.binance.us) — ETHUSDT + SOLUSDT (dispo depuis Jan 2024)

Régimes 2025 testés :
  Q1 2025 : Jan–Fév 2025  (Trump inaug + crash crypto, ETH $3500→$2000)
  Q2 2025 : Avr–Mai 2025  (rebond ETH $2000→$2500)
  H2 2025 : Jul–Sep 2025  (bull ETH $2500→$3500)

Configs : ETH-only | SOL-only | ETH+SOL pool
Params live : zone±0.3%, target+0.9%, pos_pct=50%, max_sim global=2
"""
import warnings
warnings.filterwarnings("ignore")
import sys, time, os
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
from collections import defaultdict

try:
    import requests
except ImportError:
    print("pip install requests --break-system-packages"); sys.exit(1)

# ── CONFIG ────────────────────────────────────────────────────────────────────
SYMBOLS = {"ETH": "ETHUSDT", "SOL": "SOLUSDT"}
CAPITAL     = 1000.0
POS_PCT     = 0.50
MAX_SIM     = 2
ZONE_PCT    = 0.003
MAX_TOUCHES = 2
RSI_LOW     = 20
RSI_HIGH    = 65
TARGET_PCT  = 0.009
TIMEOUT_B   = 48   # 48 barres × 5min = 4h
WARMUP_DAYS = 90   # 3 mois de warmup pour les zones
CACHE_DIR   = "binance_us_cache_2025"

PERIODS = [
    ("2025 Q1 (Jan–Fév)",   "2025-01-01", "2025-03-01"),
    ("2025 Q2 (Avr–Mai)",   "2025-04-01", "2025-06-01"),
    ("2025 H2 (Jul–Sep)",   "2025-07-01", "2025-10-01"),
]

BASE = "https://api.binance.us/api/v3/klines"

# ── DOWNLOAD BINANCE US ───────────────────────────────────────────────────────
def _ms(dt): return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)

def download(label, symbol, test_start, test_end):
    warmup_start = datetime.strptime(test_start, "%Y-%m-%d") - timedelta(days=WARMUP_DAYS)
    end_dt       = datetime.strptime(test_end,   "%Y-%m-%d")

    os.makedirs(CACHE_DIR, exist_ok=True)
    tag   = f"{symbol}_5m_{warmup_start.strftime('%Y%m%d')}_{end_dt.strftime('%Y%m%d')}"
    cache = os.path.join(CACHE_DIR, f"{tag}.parquet")
    if os.path.exists(cache):
        df = pd.read_parquet(cache)
        print(f"  Cache {label}: {len(df):,} bars 5m")
        return df

    print(f"  Download {label} ({symbol}) {warmup_start.strftime('%Y-%m-%d')} → {test_end}...")
    start_ms = _ms(warmup_start)
    end_ms   = _ms(end_dt)
    rows     = []
    batches  = 0

    while start_ms < end_ms:
        try:
            resp = requests.get(BASE, params={
                "symbol": symbol, "interval": "5m",
                "startTime": start_ms, "limit": 1000,
            }, timeout=15)
            data = resp.json()
        except Exception as e:
            print(f"  Retry {label}: {e}"); time.sleep(3); continue

        if not data or isinstance(data, dict): break
        rows.extend(data)
        start_ms = data[-1][0] + 60_000 * 5
        batches += 1
        if batches % 50 == 0:
            d = datetime.fromtimestamp(rows[-1][0]/1000, tz=timezone.utc).strftime("%Y-%m")
            print(f"  ... {len(rows):,} bars → {d}")
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
    print(f"  {label}: {len(df):,} bars ({df.index[0].date()} → {df.index[-1].date()})")
    return df

def resample(df, rule):
    return df.resample(rule).agg(
        {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
    ).dropna()

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

def get_signal_fast(sym, i5, arr5, arr15, arr1h):
    """Version rapide : indices pré-calculés, accès numpy direct."""
    # Correspondances 1h et 15m : on passe les slices directement
    h5m, l5m, c5m, v5m   = arr5
    h15, l15, c15         = arr15
    h1h, l1h              = arr1h

    n5 = len(c5m); n15 = len(c15); n1h = len(h1h)
    if n5 < 30 or n15 < 20 or n1h < 10: return None

    # Biais 1h
    if h1h[-1] < h1h[-4] and l1h[-1] < l1h[-4]: return None

    cl5  = c5m[-30:]
    vo5  = v5m[-30:]
    rsi  = _rsi(cl5, 14)
    avgv = vo5[-20:].mean() if len(vo5) >= 20 else vo5.mean()
    if avgv > 0 and vo5[-1] < avgv * 0.3: return None

    zones = _find_zones(h15[-100:], l15[-100:], c15[-100:])
    curr  = float(cl5[-1])

    for zone in zones:
        dist = (curr - zone["center"]) / curr
        if not (0.001 <= dist <= 0.020): continue
        if not (RSI_LOW <= rsi <= RSI_HIGH): continue
        div = _rsi_div(cl5, rsi)
        if not div and not (30 <= rsi <= 55): continue
        if not any(l5m[-8:] <= zone["high"]): continue
        if cl5[-1] <= zone["low"]: continue
        stop   = _dyn_stop(l5m[-8:], zone["center"], zone["wick_low"])
        target = round(zone["high"] * (1 + TARGET_PCT), 2)
        risk   = abs(zone["center"] - stop)
        reward = abs(target - zone["center"])
        if risk <= 0 or reward / risk < 1.2: continue
        return {"zone": zone, "stop": stop, "target": target}
    return None

# ── BACKTEST ──────────────────────────────────────────────────────────────────
def run(symbols, all_data, test_start_ts):
    # Pré-calcul des arrays numpy pour accès rapide
    arrays = {}
    for sym in symbols:
        df5  = all_data[sym]["5m"]
        df15 = all_data[sym]["15m"]
        df1h = all_data[sym]["1h"]
        idx5 = df5.index
        arrays[sym] = {
            "idx5":  idx5,
            "h5":    df5["high"].values,
            "l5":    df5["low"].values,
            "c5":    df5["close"].values,
            "v5":    df5["volume"].values,
            "o5":    df5["open"].values,
            "idx15": df15.index,
            "h15":   df15["high"].values,
            "l15":   df15["low"].values,
            "c15":   df15["close"].values,
            "idx1h": df1h.index,
            "h1h":   df1h["high"].values,
            "l1h":   df1h["low"].values,
        }

    # Index de référence = intersection des idx5
    ref_idx = sorted(set(arrays[symbols[0]]["idx5"]).intersection(
        *[set(arrays[s]["idx5"]) for s in symbols[1:]]
    ))
    n = len(ref_idx)

    # Lookup position → idx5 position
    pos_maps = {}
    for sym in symbols:
        idx5 = arrays[sym]["idx5"]
        pm   = {ts: i for i, ts in enumerate(idx5)}
        pos_maps[sym] = pm

    # Lookup position → idx15/idx1h (bisect)
    import bisect
    def get_tf_pos(idx_list, ts):
        p = bisect.bisect_right(idx_list, ts) - 1
        return max(0, p)

    capital  = CAPITAL
    trades   = []
    open_pos = {}
    touches  = defaultdict(int)

    for i in range(55, n - 1):
        t_now  = ref_idx[i]
        t_next = ref_idx[i + 1]

        # Gérer positions ouvertes
        for zk in list(open_pos.keys()):
            p    = open_pos[zk]
            sym  = p["sym"]
            pm   = pos_maps[sym]
            if t_next not in pm: continue
            ni   = pm[t_next]
            arr  = arrays[sym]
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
                    "pnl": round(pnl, 4), "reason": er, "day": t_now.date(),
                })
                del open_pos[zk]

        if capital < 20: continue
        if len(open_pos) >= MAX_SIM: continue
        if t_now < test_start_ts: continue

        for sym in symbols:
            if len(open_pos) >= MAX_SIM: break
            arr  = arrays[sym]
            ci5  = pos_maps[sym].get(t_now)
            if ci5 is None: continue

            # Tranches numpy
            ci15 = get_tf_pos(arr["idx15"], t_now)
            ci1h = get_tf_pos(arr["idx1h"], t_now)

            sig = get_signal_fast(sym, ci5,
                (arr["h5"][:ci5+1], arr["l5"][:ci5+1], arr["c5"][:ci5+1], arr["v5"][:ci5+1]),
                (arr["h15"][:ci15+1], arr["l15"][:ci15+1], arr["c15"][:ci15+1]),
                (arr["h1h"][:ci1h+1], arr["l1h"][:ci1h+1]),
            )
            if sig is None: continue

            zone = sig["zone"]
            key  = _zk(sym, zone["center"])
            if touches[key] >= MAX_TOUCHES: continue
            if key in open_pos: continue

            pm  = pos_maps[sym]
            if t_next not in pm: continue
            ni  = pm[t_next]
            lo_next = arr["l5"][ni]; op_next = arr["o5"][ni]

            if lo_next <= zone["high"]:
                fill = min(op_next, zone["center"])
                qty  = (CAPITAL * POS_PCT) / fill
                touches[key] += 1
                open_pos[key] = {
                    "sym": sym, "entry": fill,
                    "stop": sig["stop"], "target": sig["target"],
                    "qty": qty, "bar": i,
                }

    for key, p in open_pos.items():
        sym  = p["sym"]
        last = float(arrays[sym]["c5"][-1])
        pnl  = (last - p["entry"]) * p["qty"]
        capital += pnl
        trades.append({
            "sym": sym, "entry": p["entry"], "exit": last,
            "pnl": round(pnl, 4), "reason": "end",
            "day": ref_idx[-1].date(),
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
    avg_w  = sum(t["pnl"] for t in wins)  / len(wins)   if wins   else 0
    avg_l  = sum(t["pnl"] for t in losses)/ len(losses) if losses else 0
    return {
        "n": n, "wr": round(wr, 1), "pf": round(pf, 2),
        "total": round(total, 2), "ann": round(ann, 1), "mdd": round(mdd, 1),
        "stops": stops, "targets": tgts, "timeouts": touts,
        "avg_win": round(avg_w, 4), "avg_loss": round(avg_l, 4),
    }

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print("\n" + "═"*78)
    print("  Geo V4 — VRAIS 5min — 2025 (Binance US — ETHUSDT / SOLUSDT)")
    print("  ETH-only / SOL-only / ETH+SOL pool | $1000 | 50% pos | zone±0.3% | target+0.9%")
    print("  Warmup 90j → Q1 bull→crash / Q2 rebond / H2 bull run")
    print("═"*78)

    configs = {
        "ETH-only":  ["ETH"],
        "SOL-only":  ["SOL"],
        "ETH+SOL":   ["ETH", "SOL"],
    }

    print(f"\n  {'Régime':<28} {'Config':<12} {'N':>4} {'WR%':>6} {'PF':>5}"
          f" {'P&L$':>8} {'Ann%':>8} {'MDD%':>6} {'T|S|TO'}")
    print(f"  {'─'*28} {'─'*12} {'─'*4} {'─'*6} {'─'*5}"
          f" {'─'*8} {'─'*8} {'─'*6} {'─'*10}")

    summary = {}

    for period_label, test_start, test_end in PERIODS:
        dt_start = datetime.strptime(test_start, "%Y-%m-%d")
        dt_end   = datetime.strptime(test_end,   "%Y-%m-%d")
        days     = (dt_end - dt_start).days
        test_start_ts = pd.Timestamp(test_start, tz="UTC")

        print(f"\n  ── {period_label} ──")

        # Télécharger 5m + resample
        all_data = {}
        ok = True
        for label, pair in SYMBOLS.items():
            df5 = download(label, pair, test_start, test_end)
            if df5 is None or df5.empty:
                print(f"  {label}: données vides"); ok = False; break
            all_data[label] = {
                "5m":  df5,
                "15m": resample(df5, "15min"),
                "1h":  resample(df5, "1h"),
            }
        if not ok: continue

        period_results = {}
        for cfg_label, symbols in configs.items():
            trades, capital = run(symbols, all_data, test_start_ts)
            s = stats(trades, capital, days)
            period_results[cfg_label] = s

            if s:
                exits = f"{s['targets']}|{s['stops']}|{s['timeouts']}"
                flag = " ✓" if s["pf"] >= 1.5 and s["total"] >= 0 else (" ~" if s["total"] >= 0 else " ✗")
                print(
                    f"  {period_label:<28} {cfg_label:<12} {s['n']:>4}"
                    f" {s['wr']:>6.1f} {s['pf']:>5.2f} {s['total']:>+8.2f}"
                    f" {s['ann']:>+7.1f}% {s['mdd']:>+5.1f}%  {exits}{flag}"
                )
            else:
                print(f"  {period_label:<28} {cfg_label:<12}    0  — — — — — —")

        summary[period_label] = period_results

    # Récap ETH+SOL
    print(f"\n{'═'*78}")
    print("  RÉCAP ETH+SOL POOL — 3 régimes VRAIS 5min")
    print(f"  {'─'*78}")
    total_pnl  = 0
    valid      = 0
    profitable = 0
    for period_label, _, __ in PERIODS:
        results = summary.get(period_label, {})
        s = results.get("ETH+SOL")
        if s and s["n"] > 0:
            icon = "✓" if s["total"] >= 0 else "✗"
            print(f"  [{icon}] {period_label:<30} N={s['n']:>3} WR={s['wr']:.0f}%"
                  f" PF={s['pf']:.2f} P&L={s['total']:>+8.2f}$ Ann={s['ann']:>+6.1f}% MDD={s['mdd']:.1f}%")
            total_pnl += s["total"]
            valid     += 1
            if s["total"] >= 0: profitable += 1
        else:
            print(f"  [?] {period_label:<30} 0 trades")

    print(f"\n  P&L cumulé 3 régimes : {total_pnl:+.2f}$")
    print(f"  Régimes profitables  : {profitable}/{valid}")
    print("═"*78 + "\n")

if __name__ == "__main__":
    main()
