"""
backtest_2022_2023_v2.py — GEO V4 · ETH+SOL · Binance US · 2022 + 2023
=======================================================================
Corrections vs v1 :
  1. Frais réels OKX : maker 0.02% + taker 0.05% par jambe
  2. Position sizing compoundé (capital courant, pas capital initial)
  3. Signaux SHORT (zones de résistance) en plus des longs
  4. Filtre régime VIX (données Yahoo Finance via yfinance)
  5. Résolution stop/target même bougie : 50/50 aléatoire
  6. Fill rate dynamique selon profondeur de pénétration dans la zone
     (30–90% selon combien le prix a traversé le niveau)
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

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False
    print("  [warn] yfinance non installé → filtre VIX désactivé (pip install yfinance)")

# ── CONFIG ─────────────────────────────────────────────────────────────────────
SYMBOLS      = {"ETH": "ETHUSD", "SOL": "SOLUSD"}
CAPITAL      = 1000.0
POS_PCT      = 0.50
MAX_SIM      = 2
ZONE_PCT     = 0.003
MAX_TOUCHES  = 2
RSI_LOW      = 20
RSI_HIGH     = 65
RSI_LOW_S    = 35    # short
RSI_HIGH_S   = 80    # short
TARGET_PCT   = 0.009
TIMEOUT_B    = 48    # 48 × 5min = 4h

FEE_MAKER    = 0.0002   # 0.02% — entry limit order
FEE_TAKER    = 0.0005   # 0.05% — exit market (stop/timeout) ou taker fill

VIX_BEAR_TH  = 25.0
VIX_PANIC_TH = 35.0

RNG_SEED     = 42
CACHE_DIR    = "binance_us_cache"
BASE         = "https://api.binance.us/api/v3/klines"

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

# ── VIX (Yahoo Finance) ────────────────────────────────────────────────────────
_vix_cache = {}

def _load_vix(year_start, year_end):
    if not HAS_YF:
        return {}
    cache_key = f"{year_start}_{year_end}"
    if cache_key in _vix_cache:
        return _vix_cache[cache_key]
    try:
        vix = yf.download("^VIX", start=year_start, end=year_end,
                          auto_adjust=True, progress=False)
        if vix.empty:
            return {}
        # dict date → vix close
        result = {}
        close_col = "Close" if "Close" in vix.columns else vix.columns[0]
        for ts, row in vix.iterrows():
            d = ts.date() if hasattr(ts, "date") else ts
            result[d] = float(row[close_col])
        _vix_cache[cache_key] = result
        print(f"  [vix] {len(result)} jours chargés ({year_start} → {year_end})")
        return result
    except Exception as e:
        print(f"  [vix] erreur: {e} → filtre VIX désactivé")
        return {}

def _vix_regime(vix_map, date):
    if not vix_map:
        return "bull"
    # Cherche la valeur VIX la plus récente ≤ date
    for delta in range(7):
        d = date - timedelta(days=delta)
        if d in vix_map:
            v = vix_map[d]
            if v > VIX_PANIC_TH: return "panic"
            if v > VIX_BEAR_TH:  return "bear"
            return "bull"
    return "bull"

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

# ── INDICATEURS ────────────────────────────────────────────────────────────────
def _rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    d  = np.diff(np.array(closes, dtype=float))
    g  = np.where(d > 0, d, 0.0); l = np.where(d < 0, -d, 0.0)
    ag = g[-period:].mean(); al = l[-period:].mean()
    if al == 0: return 100.0
    return round(100 - 100 / (1 + ag / al), 2)

def _ema(arr, n):
    if len(arr) < n: return arr[-1] if len(arr) else 0.0
    k = 2 / (n + 1)
    e = float(arr[0])
    for v in arr[1:]:
        e = v * k + e * (1 - k)
    return e

def _find_support_zones(highs, lows, closes):
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
                "dir": "long",
                "center": center,
                "high":   center * (1 + ZONE_PCT),
                "low":    center * (1 - ZONE_PCT),
                "wick":   wick_low,
            })
    zones.sort(key=lambda x: x["center"], reverse=True)
    return zones

def _find_resistance_zones(highs, lows, closes):
    current = closes[-1]; sw = []
    for i in range(2, len(highs) - 2):
        if (highs[i] > highs[i-1] and highs[i] > highs[i-2]
                and highs[i] > highs[i+1] and highs[i] > highs[i+2]):
            sw.append((highs[i], lows[i]))
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
        center    = sum(x[0] for x in c) / len(c)
        wick_high = max(x[0] for x in c)
        if center > current * 1.001:
            zones.append({
                "dir": "short",
                "center": center,
                "high":   center * (1 + ZONE_PCT),
                "low":    center * (1 - ZONE_PCT),
                "wick":   wick_high,
            })
    zones.sort(key=lambda x: x["center"])
    return zones

def _rsi_bull_div(closes, rsi_now):
    if len(closes) < 5: return False
    rsi_prev = _rsi(np.array(closes[:-3]), 14)
    return closes[-1] < closes[-4] and rsi_now > rsi_prev

def _rsi_bear_div(closes, rsi_now):
    if len(closes) < 5: return False
    rsi_prev = _rsi(np.array(closes[:-3]), 14)
    return closes[-1] > closes[-4] and rsi_now < rsi_prev

def _dyn_stop_long(lows, entry, wick):
    floor = entry * 0.992
    c = min(lows[-8:]) * 0.999 if len(lows) >= 8 else wick * 0.999
    if floor <= c < entry: return c
    z = wick * 0.999
    if floor <= z < entry: return z
    return entry * 0.997

def _dyn_stop_short(highs, entry, wick):
    ceiling = entry * 1.008
    c = max(highs[-8:]) * 1.001 if len(highs) >= 8 else wick * 1.001
    if entry < c <= ceiling: return c
    z = wick * 1.001
    if entry < z <= ceiling: return z
    return entry * 1.003

def _zk(sym, center, direction):
    mag = max(1, int(round(-np.log10(center * 0.001))))
    return f"{sym}_{direction}_{round(center, mag)}"

# ── FILL RATE DYNAMIQUE ────────────────────────────────────────────────────────
def _fill_prob(entry_level, candle_extreme, zone_width, rng):
    """
    Plus le prix pénètre profondément dans la zone, plus le fill est probable.
    entry_level : prix de l'ordre limit
    candle_extreme : low (long) ou high (short) de la bougie suivante
    zone_width : largeur de la zone (zone.high - zone.low)
    Retourne True si l'ordre est rempli.
    """
    if zone_width <= 0:
        return rng.random() < 0.50
    penetration = abs(entry_level - candle_extreme) / zone_width
    # 0% pénétration = price frôle à peine  → 30% fill
    # 100%+ pénétration = price traverse toute la zone → 90% fill
    prob = 0.30 + min(penetration, 1.0) * 0.60
    return rng.random() < prob

# ── SIGNAUX ────────────────────────────────────────────────────────────────────
def get_long_signal(sym, h5, l5, c5, v5, h15, l15, c15, h1h, l1h):
    if len(c5) < 30 or len(c15) < 20 or len(h1h) < 10: return None
    # Filtre downtrend 1H
    if h1h[-1] < h1h[-4] and l1h[-1] < l1h[-4]: return None
    cl5 = c5[-30:]; vo5 = v5[-30:]
    rsi = _rsi(cl5, 14)
    # Filtre volume
    avgv = vo5[:-1][-20:].mean() if len(vo5) > 1 else 1.0
    if avgv > 0 and vo5[-2] < avgv * 0.3: return None
    # Filtre EMA
    ema5 = _ema(cl5, 5); ema10 = _ema(cl5, 10)
    if ema5 < ema10 * 0.9985: return None
    zones = _find_support_zones(h15[-100:], l15[-100:], c15[-100:])
    curr = float(cl5[-1])
    for zone in zones:
        dist = (curr - zone["center"]) / curr
        if not (0.001 <= dist <= 0.020): continue
        if not (RSI_LOW <= rsi <= RSI_HIGH): continue
        div = _rsi_bull_div(cl5, rsi)
        if not div and not (30 <= rsi <= 55): continue
        if not any(l5[-8:] <= zone["high"]): continue
        if cl5[-1] <= zone["low"]: continue
        stop   = _dyn_stop_long(l5[-8:], zone["center"], zone["wick"])
        target = round(zone["high"] * (1 + TARGET_PCT), 2)
        risk   = abs(zone["center"] - stop)
        reward = abs(target - zone["center"])
        if risk <= 0 or reward / risk < 1.2: continue
        return {"zone": zone, "stop": stop, "target": target, "dir": "long"}
    return None

def get_short_signal(sym, h5, l5, c5, v5, h15, l15, c15, h1h, l1h):
    if len(c5) < 30 or len(c15) < 20 or len(h1h) < 10: return None
    # Filtre uptrend 1H
    if h1h[-1] > h1h[-4] and l1h[-1] > l1h[-4]: return None
    cl5 = c5[-30:]; vo5 = v5[-30:]
    rsi = _rsi(cl5, 14)
    avgv = vo5[:-1][-20:].mean() if len(vo5) > 1 else 1.0
    if avgv > 0 and vo5[-2] < avgv * 0.3: return None
    # Filtre EMA bearish
    ema5 = _ema(cl5, 5); ema10 = _ema(cl5, 10)
    if ema5 > ema10 * 1.0015: return None
    zones = _find_resistance_zones(h15[-100:], l15[-100:], c15[-100:])
    curr = float(cl5[-1])
    for zone in zones:
        dist = (zone["center"] - curr) / curr
        if not (-0.002 <= dist <= 0.012): continue
        if not (RSI_LOW_S <= rsi <= RSI_HIGH_S): continue
        div = _rsi_bear_div(cl5, rsi)
        if not div and not (45 <= rsi <= 70): continue
        if not any(h5[-8:] >= zone["low"]): continue
        if cl5[-1] >= zone["high"]: continue
        stop   = _dyn_stop_short(h5[-8:], zone["center"], zone["wick"])
        target = round(zone["low"] * (1 - TARGET_PCT), 2)
        risk   = abs(stop - zone["center"])
        reward = abs(zone["center"] - target)
        if risk <= 0 or reward / risk < 1.2: continue
        return {"zone": zone, "stop": stop, "target": target, "dir": "short"}
    return None

# ── BACKTEST ────────────────────────────────────────────────────────────────────
def run(symbols, all_data, test_start_ts, test_end_ts, rng, vix_map):
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
    total_fees = 0.0

    for i in range(55, n - 1):
        t_now  = ref_idx[i]
        t_next = ref_idx[i + 1]
        if t_now >= test_end_ts: break

        # ── Fermer positions ────────────────────────────────────────────────────
        for zk in list(open_pos.keys()):
            p   = open_pos[zk]; sym = p["sym"]
            pm  = pos_maps[sym]
            if t_next not in pm: continue
            ni  = pm[t_next]; arr = arrays[sym]
            hi  = arr["h5"][ni]; lo = arr["l5"][ni]; cl = arr["c5"][ni]

            if p["dir"] == "long":
                sh = lo <= p["stop"]
                th = hi >= p["target"]
            else:  # short
                sh = hi >= p["stop"]
                th = lo <= p["target"]

            to = (i - p["bar"]) >= TIMEOUT_B
            ep = er = None

            if sh and th:
                # Résolution 50/50 quand stop et target atteints même bougie
                if rng.random() < 0.5:
                    ep, er = p["stop"],   "stop"
                else:
                    ep, er = p["target"], "target"
            elif sh:  ep, er = p["stop"],   "stop"
            elif th:  ep, er = p["target"], "target"
            elif to:  ep, er = cl,          "timeout"

            if ep:
                # PnL selon direction
                if p["dir"] == "long":
                    pnl_gross = (ep - p["entry"]) * p["qty"]
                else:
                    pnl_gross = (p["entry"] - ep) * p["qty"]

                # Frais de sortie : taker si stop/timeout, maker si target
                fee_rate_exit = FEE_MAKER if er == "target" else FEE_TAKER
                fee_exit = ep * p["qty"] * fee_rate_exit
                pnl_net = pnl_gross - fee_exit
                total_fees += fee_exit

                capital += pnl_net
                trades.append({
                    "sym": sym, "dir": p["dir"],
                    "entry": p["entry"], "exit": ep,
                    "pnl": round(pnl_net, 4),
                    "pnl_gross": round(pnl_gross, 4),
                    "fee": round(p["fee_entry"] + fee_exit, 4),
                    "reason": er,
                    "day": t_now.date(),
                    "month": t_now.strftime("%Y-%m"),
                })
                del open_pos[zk]

        if capital < 20 or len(open_pos) >= MAX_SIM: continue
        if t_now < test_start_ts: continue

        # ── Filtre régime VIX ───────────────────────────────────────────────────
        regime = _vix_regime(vix_map, t_now.date())

        for sym in symbols:
            if len(open_pos) >= MAX_SIM: break
            arr = arrays[sym]
            ci5  = pos_maps[sym].get(t_now)
            if ci5 is None: continue
            ci15 = tf_pos(arr["idx15"], t_now)
            ci1h = tf_pos(arr["idx1h"], t_now)

            h5s  = arr["h5"][:ci5+1];  l5s  = arr["l5"][:ci5+1]
            c5s  = arr["c5"][:ci5+1];  v5s  = arr["v5"][:ci5+1]
            h15s = arr["h15"][:ci15+1]; l15s = arr["l15"][:ci15+1]
            c15s = arr["c15"][:ci15+1]
            h1hs = arr["h1h"][:ci1h+1]; l1hs = arr["l1h"][:ci1h+1]

            sigs = []

            # Longs bloqués en panic/bear
            if regime not in ("panic", "bear"):
                sig = get_long_signal(sym, h5s, l5s, c5s, v5s, h15s, l15s, c15s, h1hs, l1hs)
                if sig: sigs.append(sig)

            # Shorts bloqués en panic (pas en bear — les shorts profitent du bear)
            if regime != "panic":
                sig = get_short_signal(sym, h5s, l5s, c5s, v5s, h15s, l15s, c15s, h1hs, l1hs)
                if sig: sigs.append(sig)

            for sig in sigs:
                if len(open_pos) >= MAX_SIM: break
                zone = sig["zone"]
                direction = sig["dir"]
                key = _zk(sym, zone["center"], direction)
                if touches[key] >= MAX_TOUCHES or key in open_pos: continue

                pm = pos_maps[sym]
                if t_next not in pm: continue
                ni = pm[t_next]
                lo_next = arr["l5"][ni]
                hi_next = arr["h5"][ni]
                op_next = arr["o5"][ni]

                zone_width = zone["high"] - zone["low"]

                if direction == "long" and lo_next <= zone["high"]:
                    if not _fill_prob(zone["high"], lo_next, zone_width, rng):
                        skipped += 1
                        continue
                    filled += 1
                    fill = min(op_next, zone["center"])
                    # Position sizing compoundé sur capital courant
                    pos_size = capital * POS_PCT
                    qty      = pos_size / fill
                    fee_entry = fill * qty * FEE_MAKER
                    capital  -= fee_entry
                    total_fees += fee_entry
                    touches[key] += 1
                    open_pos[key] = {
                        "sym": sym, "dir": "long", "entry": fill,
                        "stop": sig["stop"], "target": sig["target"],
                        "qty": qty, "bar": i, "fee_entry": fee_entry,
                    }

                elif direction == "short" and hi_next >= zone["low"]:
                    if not _fill_prob(zone["low"], hi_next, zone_width, rng):
                        skipped += 1
                        continue
                    filled += 1
                    fill = max(op_next, zone["center"])
                    pos_size = capital * POS_PCT
                    qty      = pos_size / fill
                    fee_entry = fill * qty * FEE_MAKER
                    capital  -= fee_entry
                    total_fees += fee_entry
                    touches[key] += 1
                    open_pos[key] = {
                        "sym": sym, "dir": "short", "entry": fill,
                        "stop": sig["stop"], "target": sig["target"],
                        "qty": qty, "bar": i, "fee_entry": fee_entry,
                    }

    # Fermer positions restantes (fin de période)
    for key, p in open_pos.items():
        sym  = p["sym"]
        last = float(arrays[sym]["c5"][-1])
        if p["dir"] == "long":
            pnl_gross = (last - p["entry"]) * p["qty"]
        else:
            pnl_gross = (p["entry"] - last) * p["qty"]
        fee_exit = last * p["qty"] * FEE_TAKER
        pnl_net  = pnl_gross - fee_exit
        total_fees += fee_exit
        capital += pnl_net
        trades.append({
            "sym": sym, "dir": p["dir"],
            "entry": p["entry"], "exit": last,
            "pnl": round(pnl_net, 4),
            "pnl_gross": round(pnl_gross, 4),
            "fee": round(p["fee_entry"] + fee_exit, 4),
            "reason": "end",
            "day": ref_idx[-1].date(),
            "month": ref_idx[-1].strftime("%Y-%m"),
        })

    return trades, capital, filled, skipped, round(total_fees, 2)

# ── STATS ───────────────────────────────────────────────────────────────────────
def stats(trades, capital, days):
    n = len(trades)
    if n == 0: return None
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total  = sum(t["pnl"] for t in trades)
    total_fees = sum(t["fee"] for t in trades)
    wr     = len(wins) / n * 100
    sum_l  = sum(t["pnl"] for t in losses)
    pf     = abs(sum(t["pnl"] for t in wins) / sum_l) if sum_l else 99.0
    ann    = (capital - CAPITAL) / CAPITAL / max(days, 1) * 365 * 100
    eq     = np.array([CAPITAL] + [CAPITAL + sum(t["pnl"] for t in trades[:k+1]) for k in range(n)])
    pk     = np.maximum.accumulate(eq); mdd = ((eq - pk) / pk * 100).min()
    longs  = [t for t in trades if t["dir"] == "long"]
    shorts = [t for t in trades if t["dir"] == "short"]
    return {
        "n": n, "wr": round(wr,1), "pf": round(pf,2),
        "total": round(total,2), "total_fees": round(total_fees,2),
        "ann": round(ann,1), "mdd": round(mdd,1),
        "capital": round(capital,2),
        "stops":   len([t for t in trades if t["reason"] == "stop"]),
        "targets": len([t for t in trades if t["reason"] == "target"]),
        "timeouts":len([t for t in trades if t["reason"] in ("timeout","end")]),
        "n_long":  len(longs),  "n_short": len(shorts),
        "wr_long": round(len([t for t in longs if t["pnl"]>0])/len(longs)*100, 1) if longs else 0,
        "wr_short":round(len([t for t in shorts if t["pnl"]>0])/len(shorts)*100, 1) if shorts else 0,
    }

# ── MAIN ────────────────────────────────────────────────────────────────────────
def main():
    print("\n" + "═"*80)
    print("  GEO V4 — BACKTEST v2 — 2022 + 2023 — ETH+SOL — Corrections réalisme")
    print("  Frais OKX maker/taker · Position sizing compoundé · Longs + Shorts")
    print("  Filtre VIX · Stop/target 50/50 · Fill rate dynamique (pénétration zone)")
    print("═"*80)

    rng = random.Random(RNG_SEED)

    configs = {
        "ETH-only": ["ETH"],
        "SOL-only": ["SOL"],
        "ETH+SOL":  ["ETH", "SOL"],
    }

    all_period_results = {}
    all_period_trades  = {}

    for period_label, pcfg in PERIODS.items():
        ts_start = pd.Timestamp(pcfg["test_start"], tz="UTC")
        ts_end   = pd.Timestamp(pcfg["test_end"],   tz="UTC")
        days     = (datetime.strptime(pcfg["test_end"],   "%Y-%m-%d") -
                    datetime.strptime(pcfg["test_start"], "%Y-%m-%d")).days

        print(f"\n{'─'*80}")
        print(f"  {period_label} ({pcfg['test_start']} → {pcfg['test_end']})")
        print(f"  Téléchargement...")

        # Données prix
        all_data = {}
        for sym_key, bn_sym in SYMBOLS.items():
            df5 = download(bn_sym, pcfg["dl_start"], pcfg["dl_end"])
            if df5 is None or df5.empty:
                print(f"  ERREUR: {bn_sym} vide"); continue
            df5 = df5[df5.index < ts_end]
            all_data[sym_key] = {
                "5m":  df5,
                "15m": resample(df5, "15min"),
                "1h":  resample(df5, "1h"),
            }

        # VIX
        vix_map = _load_vix(pcfg["dl_start"], pcfg["test_end"])

        print(f"\n  {'Config':<12} {'N':>5} {'L/S':>7} {'WR%':>6} {'PF':>5} {'P&L $':>9} "
              f"{'Fees $':>7} {'Capital':>9} {'Ann%':>8} {'MDD%':>7}  T|S|TO")
        print(f"  {'─'*12} {'─'*5} {'─'*7} {'─'*6} {'─'*5} {'─'*9} "
              f"{'─'*7} {'─'*9} {'─'*8} {'─'*7}  {'─'*8}")

        period_results = {}
        all_trades_by_cfg = {}

        for cfg_label, symbols in configs.items():
            trades, capital, filled, skipped, total_fees = run(
                symbols, all_data, ts_start, ts_end, rng, vix_map
            )
            s = stats(trades, capital, days)
            period_results[cfg_label] = s
            all_trades_by_cfg[cfg_label] = trades

            if s:
                exits    = f"{s['targets']}|{s['stops']}|{s['timeouts']}"
                ls_str   = f"{s['n_long']}L/{s['n_short']}S"
                flag = "✓" if s["pf"] >= 1.5 and s["total"] >= 0 else ("~" if s["total"] >= 0 else "✗")
                print(f"  [{flag}] {cfg_label:<10} {s['n']:>5} {ls_str:>7} {s['wr']:>6.1f}"
                      f" {s['pf']:>5.2f} {s['total']:>+9.2f} {s['total_fees']:>7.2f}"
                      f" {s['capital']:>9.2f} {s['ann']:>+7.1f}% {s['mdd']:>+6.1f}%  {exits}")
            else:
                print(f"  [?] {cfg_label:<10}     0  —")

        # Détail mensuel ETH+SOL
        eth_sol_trades = all_trades_by_cfg.get("ETH+SOL", [])
        if eth_sol_trades:
            print(f"\n  Détail mensuel — ETH+SOL")
            year = pcfg["test_start"][:4]
            months = [(f"{m:02d}/{year}", f"{year}-{m:02d}-01",
                       f"{year}-{m+1:02d}-01" if m < 12 else f"{int(year)+1}-01-01")
                      for m in range(1, 13)]
            running_cap = CAPITAL
            for m_label, m_start, m_end in months:
                m_s = datetime.strptime(m_start, "%Y-%m-%d").date()
                m_e = datetime.strptime(m_end,   "%Y-%m-%d").date()
                mt  = [t for t in eth_sol_trades if m_s <= t["day"] < m_e]
                if not mt:
                    continue
                m_pnl = sum(t["pnl"] for t in mt)
                m_fees = sum(t["fee"] for t in mt)
                running_cap += m_pnl
                wr  = len([t for t in mt if t["pnl"] > 0]) / len(mt) * 100
                n_l = len([t for t in mt if t["dir"] == "long"])
                n_s = len([t for t in mt if t["dir"] == "short"])
                bar = ("+" if m_pnl >= 0 else "-") + "█" * min(int(abs(m_pnl)/3), 20)
                print(f"    {m_label}  N={len(mt):>3}({n_l}L/{n_s}S)  WR={wr:>4.0f}%  "
                      f"P&L={m_pnl:>+7.2f}$  Fees={m_fees:>5.2f}$  Cap={running_cap:>8.2f}$  {bar}")

        all_period_results[period_label] = period_results
        all_period_trades[period_label]  = all_trades_by_cfg

    # ── RÉSUMÉ GLOBAL ────────────────────────────────────────────────────────────
    print(f"\n{'═'*80}")
    print("  RÉSUMÉ GLOBAL — 2022 + 2023 cumulés")
    print(f"{'─'*80}")
    for cfg_label in configs:
        total_n    = sum((all_period_results[p].get(cfg_label) or {}).get("n", 0) for p in PERIODS)
        total_pnl  = sum((all_period_results[p].get(cfg_label) or {}).get("total", 0) for p in PERIODS)
        total_fees = sum((all_period_results[p].get(cfg_label) or {}).get("total_fees", 0) for p in PERIODS)
        avg_wr     = np.mean([
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
        print(f"  [{flag}] {cfg_label:<12}  N={total_n:>5}  WR={avg_wr:.1f}%  PF={avg_pf:.2f}"
              f"  P&L={total_pnl:>+8.2f}$  Fees={total_fees:.2f}$")

    print(f"\n  Corrections appliquées vs v1 :")
    print(f"    · Frais OKX : maker {FEE_MAKER*100:.2f}% + taker {FEE_TAKER*100:.2f}% par jambe")
    print(f"    · Position sizing compoundé sur capital courant")
    print(f"    · Signaux SHORT (résistances) ajoutés")
    print(f"    · Filtre VIX : longs bloqués si VIX>{VIX_BEAR_TH}, tout bloqué si VIX>{VIX_PANIC_TH}")
    print(f"    · Stop/target même bougie : 50/50 aléatoire")
    print(f"    · Fill rate dynamique selon pénétration dans la zone (30–90%)")
    print("═"*80 + "\n")

if __name__ == "__main__":
    main()
