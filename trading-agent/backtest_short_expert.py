"""
backtest_short_expert.py — Multi-thesis short expert backtester
================================================================
Architecture target (Phase 1a):
  - Data loader: consolidate binance_us_cache/ parquet chunks for ETH+SOL
  - BacktestBroker: get_bars(symbol, tf, limit, as_of) — slicing layer
  - BTC fetcher closure: feeds crypto_regime offline mode from BTC parquet
  - SharedPool (P3): MAX_SIM_GLOBAL=2, max 1/expert, no same-symbol long+short
  - Router: T1 zone bounce (ported from archive), T2/T3/T4 stubs
  - Per-thesis attribution: every trade tagged thesis_id + expert_id

Phase 1a deliverable: SKELETON + T1 long + T1 short working,
producing comparable results to backtest_2022_2023_v2.py (G6 verification).

T2/T3/T4 are stubs returning None — Phase 1b/1c fills them.

Run:
    python3 backtest_short_expert.py --year 2023 --enable t1
"""
import warnings; warnings.filterwarnings("ignore")
import os, sys, time, random, argparse, bisect, json
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import numpy as np
import pandas as pd

# ─── CONFIG ───────────────────────────────────────────────────────────────────

CAPITAL          = 1000.0
POS_PCT          = 0.50
MAX_SIM_GLOBAL   = 2
MAX_PER_EXPERT   = 1
CAP_FACTOR       = 0.7     # when other expert active
ZONE_PCT         = 0.003
MAX_TOUCHES      = 2
RSI_LOW          = 20
RSI_HIGH         = 65
RSI_LOW_S        = 35
RSI_HIGH_S       = 80
TARGET_PCT       = 0.009
TIMEOUT_BARS     = 48          # 48 × 5m = 4h
MIN_RR           = 1.2

FEE_MAKER        = 0.0002      # Kraken Futures perp maker 0.02%
FEE_TAKER        = 0.0005      # Kraken Futures perp taker 0.05%
SLIPPAGE_BPS     = 5           # 0.05% slippage applied to MARKET fills only
                               # (T2/T3/T4 entries, all stop/timeout exits).
                               # Limit fills (T1 entries, all TARGET exits) = 0 slippage.
SLIPPAGE_PCT     = SLIPPAGE_BPS / 10000.0

CRYPTO_REGIME_MIN_CONFIDENCE = 50

RNG_SEED         = 42
CACHE_DIR_USD    = "binance_us_cache"
CACHE_DIR_BTC    = "binance_cache"

# ─── DATA LOADING ─────────────────────────────────────────────────────────────

def load_symbol_5m(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Charge et concatène tous les fichiers parquet 5m du cache USD pour la période demandée."""
    files = []
    if os.path.isdir(CACHE_DIR_USD):
        for f in sorted(os.listdir(CACHE_DIR_USD)):
            if not f.endswith(".parquet"): continue
            if not f.startswith(f"{symbol}_5m_"): continue
            if "_atr_" in f: continue          # skip atr-augmented variants
            files.append(os.path.join(CACHE_DIR_USD, f))
    if not files:
        raise FileNotFoundError(f"No 5m parquet for {symbol} in {CACHE_DIR_USD}")

    dfs = []
    for f in files:
        df = pd.read_parquet(f)
        dfs.append(df)
    df = pd.concat(dfs, axis=0)
    # Dedupliquer par timestamp (overlap entre fichiers)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    # Filter range
    df = df[(df.index >= pd.Timestamp(start, tz="UTC")) & (df.index <= pd.Timestamp(end, tz="UTC"))]
    # Standardize columns (lowercase)
    df.columns = [c.lower() for c in df.columns]
    if "volume" not in df.columns:
        df["volume"] = 1.0
    return df[["open", "high", "low", "close", "volume"]]


def resample_ohlcv(df_5m: pd.DataFrame, rule: str) -> pd.DataFrame:
    return df_5m.resample(rule, origin="epoch").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna()


def load_btc_4h() -> pd.DataFrame:
    f = os.path.join(CACHE_DIR_BTC, "BTCUSD_4h_2022-01-01_2024-12-31.parquet")
    if not os.path.isfile(f):
        raise FileNotFoundError(f"BTC 4h cache missing: {f}")
    df = pd.read_parquet(f)
    df.columns = [c.lower() for c in df.columns]
    return df[["open", "high", "low", "close", "volume"]]


# ─── BACKTEST BROKER (slicing layer) ──────────────────────────────────────────

class BacktestBroker:
    """Implements get_bars(symbol, tf, limit, as_of) by slicing pre-loaded DataFrames."""

    TF_MAP = {
        "5Min":  "5min",   "15Min": "15min",  "1Hour": "1h",   "4Hour": "4h",
    }

    def __init__(self, data: dict):
        """
        data structure: {symbol: {"5min": df5, "15min": df15, "1h": df1h, "4h": df4h}}
        """
        self.data = data

    def get_bars(self, symbol: str, tf: str, limit: int = 100, as_of=None):
        key = self.TF_MAP.get(tf, tf.lower())
        if symbol not in self.data or key not in self.data[symbol]:
            return None
        df = self.data[symbol][key]
        if as_of is None:
            return df.tail(limit)
        # as_of can be float ts or pd.Timestamp
        if isinstance(as_of, (int, float)):
            as_of_ts = pd.Timestamp(as_of, unit="s", tz="UTC")
        else:
            as_of_ts = pd.Timestamp(as_of)
            if as_of_ts.tz is None:
                as_of_ts = as_of_ts.tz_localize("UTC")
        sub = df[df.index <= as_of_ts]
        return sub.tail(limit) if len(sub) > 0 else None


def make_btc_fetcher(df_btc_4h: pd.DataFrame):
    """Closure: (interval, limit, as_of) → (highs, lows, closes) sliced up to as_of."""
    def fetcher(interval: str, limit: int = 50, as_of=None):
        if as_of is None:
            sub = df_btc_4h
        else:
            if isinstance(as_of, (int, float)):
                ts = pd.Timestamp(as_of, unit="s", tz="UTC")
            else:
                ts = pd.Timestamp(as_of)
                if ts.tz is None:
                    ts = ts.tz_localize("UTC")
            sub = df_btc_4h[df_btc_4h.index <= ts]
        sub = sub.tail(limit)
        if len(sub) < 6:
            return None
        return (
            sub["high"].astype(float).tolist(),
            sub["low"].astype(float).tolist(),
            sub["close"].astype(float).tolist(),
        )
    return fetcher


# ─── INDICATEURS (port from archive) ───────────────────────────────────────────

def _rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    deltas = np.diff(closes); ups = deltas.clip(min=0); dns = -deltas.clip(max=0)
    avg_u = ups[:period].mean(); avg_d = dns[:period].mean()
    for u, d in zip(ups[period:], dns[period:]):
        avg_u = (avg_u * (period - 1) + u) / period
        avg_d = (avg_d * (period - 1) + d) / period
    if avg_d == 0: return 100.0
    rs = avg_u / avg_d
    return 100.0 - (100.0 / (1.0 + rs))


def _ema(arr, period):
    if len(arr) < period: return arr[-1] if len(arr) else 0.0
    a = 2.0 / (period + 1)
    e = arr[0]
    for v in arr[1:]:
        e = v * a + e * (1 - a)
    return e


def _find_zones(highs, lows, closes, min_tests=1, side="support"):
    current = closes[-1]
    if side == "support":
        sw = [(lows[i], highs[i]) for i in range(2, len(lows) - 2)
              if lows[i] < lows[i-1] and lows[i] < lows[i-2]
              and lows[i] < lows[i+1] and lows[i] < lows[i+2]]
    else:
        sw = [(highs[i], lows[i]) for i in range(2, len(highs) - 2)
              if highs[i] > highs[i-1] and highs[i] > highs[i-2]
              and highs[i] > highs[i+1] and highs[i] > highs[i+2]]
    if not sw: return []
    sw.sort(key=lambda x: x[0])
    clusters = [[sw[0]]]
    for v in sw[1:]:
        if abs(v[0] - clusters[-1][0][0]) / clusters[-1][0][0] < ZONE_PCT * 2:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    zones = []
    for c in clusters:
        center = sum(x[0] for x in c) / len(c)
        wick = (min(x[0] for x in c) if side == "support" else max(x[0] for x in c))
        if side == "support" and center < current * 0.999 and len(c) >= min_tests:
            zones.append({"center": center, "low": center * (1 - ZONE_PCT),
                          "high": center * (1 + ZONE_PCT), "wick": wick, "tests": len(c)})
        elif side == "resistance" and center > current * 1.001 and len(c) >= min_tests:
            zones.append({"center": center, "low": center * (1 - ZONE_PCT),
                          "high": center * (1 + ZONE_PCT), "wick": wick, "tests": len(c)})
    # closest to price first
    zones.sort(key=lambda z: abs(z["center"] - current))
    return zones


def _rsi_bull_div(closes, rsi_now):
    if len(closes) < 5: return False
    rsi_prev = _rsi(closes[:-3], 14)
    return (closes[-1] < closes[-4]) and (rsi_now > rsi_prev)


def _rsi_bear_div(closes, rsi_now):
    if len(closes) < 5: return False
    rsi_prev = _rsi(closes[:-3], 14)
    return (closes[-1] > closes[-4]) and (rsi_now < rsi_prev)


def _dyn_stop_long(lows_5m, entry_level, wick_low):
    floor = wick_low * (1 - 0.001)
    recent_low = float(np.min(lows_5m))
    return min(floor, recent_low) * 0.998


def _dyn_stop_short(highs_5m, entry_level, wick_high):
    ceil_ = wick_high * (1 + 0.001)
    recent_high = float(np.max(highs_5m))
    return max(ceil_, recent_high) * 1.002


def _ema_array(closes, period):
    """Returns the full EMA series (same length as input)."""
    n = len(closes)
    if n == 0: return np.array([])
    a = 2.0 / (period + 1)
    out = np.empty(n)
    out[0] = closes[0]
    for i in range(1, n):
        out[i] = closes[i] * a + out[i-1] * (1 - a)
    return out


def _atr_array(highs, lows, closes, period=14):
    """Returns ATR series (Wilder smoothing). Returns last value only for simplicity."""
    n = len(closes)
    if n < period + 1: return 0.0
    tr = np.zeros(n)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
    atr = np.mean(tr[1:period+1])
    for i in range(period+1, n):
        atr = (atr * (period - 1) + tr[i]) / period
    return float(atr)


def _swing_high_5m(highs_5m, lookback=8):
    """Index relatif du swing high local sur les `lookback` derniers bars 5m + valeur."""
    if len(highs_5m) < lookback: return None, None
    sub = highs_5m[-lookback:]
    idx_rel = int(np.argmax(sub))
    return idx_rel, float(sub[idx_rel])


def _swing_low_1h(lows_1h, lookback=10):
    """Plus bas swing low sur lookback 1h bars."""
    if len(lows_1h) < lookback: return None
    sub = lows_1h[-lookback:]
    return float(np.min(sub))


def _nearest_support_1h_below(lows_1h, current_price, lookback=20, min_dist_pct=0.001):
    """Find the NEAREST pivot low 1h that is just below current_price.

    Returns the local-min pivot closest to current_price from below.
    This is the "support to break" — what T3 cares about, NOT the absolute plancher.
    """
    if len(lows_1h) < 5: return None
    sub = lows_1h[-lookback:]
    pivots = []
    for i in range(2, len(sub) - 2):
        if sub[i] < sub[i-1] and sub[i] < sub[i-2] and sub[i] < sub[i+1] and sub[i] < sub[i+2]:
            if sub[i] < current_price * (1 - min_dist_pct):
                pivots.append(sub[i])
    if not pivots: return None
    return max(pivots)    # closest below (highest among those < current)


def _find_next_lower_support(lows_1h, current_price, lookback=50, max_dist_pct=0.05):
    """Find next 1h support below current_price, within max_dist_pct."""
    if len(lows_1h) < 5: return None
    sub = lows_1h[-lookback:]
    # find local minima (pivot lows)
    pivots = []
    for i in range(2, len(sub) - 2):
        if sub[i] < sub[i-1] and sub[i] < sub[i-2] and sub[i] < sub[i+1] and sub[i] < sub[i+2]:
            if sub[i] < current_price * 0.998:    # at least 0.2% below
                if (current_price - sub[i]) / current_price <= max_dist_pct:
                    pivots.append(sub[i])
    if not pivots: return None
    # closest below
    return max(pivots)


def _rsi_at(closes, idx_rel_from_end, period=14):
    """RSI calculé sur closes[: -idx_rel_from_end] (idx_rel_from_end ≥ 0)."""
    if idx_rel_from_end <= 0:
        return _rsi(closes, period)
    sub = closes[:-idx_rel_from_end] if idx_rel_from_end > 0 else closes
    return _rsi(sub, period)


# ─── SHARED POOL (P3) ─────────────────────────────────────────────────────────

class SharedPool:
    def __init__(self, initial_capital, max_sim_global=MAX_SIM_GLOBAL,
                 max_per_expert=MAX_PER_EXPERT, cap_factor=CAP_FACTOR):
        self.equity = initial_capital
        self.max_sim_global = max_sim_global
        self.max_per_expert = max_per_expert
        self.cap_factor = cap_factor
        self.open_positions = []
        self.next_id = 0

    def acquire(self, expert_id, symbol, side, pos_pct, size_mult):
        if len(self.open_positions) >= self.max_sim_global:
            return None
        n_for_expert = sum(1 for p in self.open_positions if p["expert_id"] == expert_id)
        if n_for_expert >= self.max_per_expert:
            return None
        for p in self.open_positions:
            if p["symbol"] == symbol and p["side"] != side:
                return None
        other_active = any(p["expert_id"] != expert_id for p in self.open_positions)
        cap_f = self.cap_factor if other_active else 1.0
        deploy = self.equity * pos_pct * size_mult * cap_f
        return {"deploy": deploy, "cap_factor": cap_f}

    def open(self, expert_id, thesis_id, symbol, side, deploy, entry_price,
             stop, target, bar_idx, fee_entry, timeout_bars=None):
        pid = self.next_id; self.next_id += 1
        qty = deploy / entry_price
        self.open_positions.append({
            "id": pid, "expert_id": expert_id, "thesis_id": thesis_id,
            "symbol": symbol, "side": side,
            "deploy": deploy, "entry": entry_price, "stop": stop, "target": target,
            "bar_idx": bar_idx, "qty": qty, "fee_entry": fee_entry,
            "timeout_bars": timeout_bars,    # None = default global TIMEOUT_BARS
        })
        return pid

    def close(self, pid, exit_price, exit_reason, fee_exit):
        for i, p in enumerate(self.open_positions):
            if p["id"] != pid: continue
            sign = 1 if p["side"] == "long" else -1
            pnl_gross = sign * (exit_price - p["entry"]) * p["qty"]
            pnl_net = pnl_gross - fee_exit
            self.equity += pnl_net
            closed = {**p, "exit": exit_price, "exit_reason": exit_reason,
                      "pnl_gross": pnl_gross, "pnl_net": pnl_net,
                      "fee_exit": fee_exit}
            self.open_positions.pop(i)
            return closed
        return None


# ─── SIGNAL GENERATORS (ported from archive + STUBS for T2/T3/T4) ─────────────

def _bias_1h_up(h1h, l1h):
    if len(h1h) < 5: return False
    return h1h[-1] > h1h[-4] and l1h[-1] > l1h[-4]


def _bias_1h_down(h1h, l1h):
    if len(h1h) < 5: return False
    return h1h[-1] < h1h[-4] and l1h[-1] < l1h[-4]


def get_long_signal_t1(arr_5m, arr_15m, arr_1h):
    """T1 long zone bounce — ported from archive."""
    h5, l5, c5, v5 = arr_5m
    h15, l15, c15 = arr_15m
    h1h, l1h = arr_1h
    if len(c5) < 30 or len(c15) < 20 or len(h1h) < 10: return None
    if _bias_1h_down(h1h, l1h): return None
    cl5 = c5[-30:]; vo5 = v5[-30:]
    rsi = _rsi(cl5, 14)
    avgv = vo5[:-1][-20:].mean() if len(vo5) > 1 else 1.0
    if avgv > 0 and vo5[-2] < avgv * 0.3: return None
    ema5 = _ema(cl5, 5); ema10 = _ema(cl5, 10)
    if ema5 < ema10 * 0.9985: return None
    zones = _find_zones(h15[-100:], l15[-100:], c15[-100:], side="support")
    curr = float(cl5[-1])
    for zone in zones:
        dist = (curr - zone["center"]) / zone["center"]
        if not (-0.012 <= dist <= 0.002): continue
        if not (RSI_LOW <= rsi <= RSI_HIGH): continue
        div = _rsi_bull_div(cl5, rsi)
        if not div and not (30 <= rsi <= 55): continue
        if not any(l5[-8:] <= zone["high"]): continue
        if cl5[-1] <= zone["low"]: continue
        stop = _dyn_stop_long(l5[-8:], zone["center"], zone["wick"])
        target = round(zone["high"] * (1 + TARGET_PCT), 2)
        risk = abs(zone["center"] - stop)
        reward = abs(target - zone["center"])
        if risk <= 0 or reward / risk < MIN_RR: continue
        return {"thesis": "T1", "side": "long", "zone": zone,
                "entry": zone["center"], "stop": stop, "target": target,
                "fill_type": "limit"}
    return None


def get_short_signal_t1(arr_5m, arr_15m, arr_1h):
    """T1 short zone bounce — ported from archive."""
    h5, l5, c5, v5 = arr_5m
    h15, l15, c15 = arr_15m
    h1h, l1h = arr_1h
    if len(c5) < 30 or len(c15) < 20 or len(h1h) < 10: return None
    if _bias_1h_up(h1h, l1h): return None
    cl5 = c5[-30:]; vo5 = v5[-30:]
    rsi = _rsi(cl5, 14)
    avgv = vo5[:-1][-20:].mean() if len(vo5) > 1 else 1.0
    if avgv > 0 and vo5[-2] < avgv * 0.3: return None
    ema5 = _ema(cl5, 5); ema10 = _ema(cl5, 10)
    if ema5 > ema10 * 1.0015: return None
    zones = _find_zones(h15[-100:], l15[-100:], c15[-100:], side="resistance")
    curr = float(cl5[-1])
    for zone in zones:
        dist = (zone["center"] - curr) / curr
        if not (-0.002 <= dist <= 0.012): continue
        if not (RSI_LOW_S <= rsi <= RSI_HIGH_S): continue
        div = _rsi_bear_div(cl5, rsi)
        if not div and not (45 <= rsi <= 70): continue
        if not any(h5[-8:] >= zone["low"]): continue
        if cl5[-1] >= zone["high"]: continue
        stop = _dyn_stop_short(h5[-8:], zone["center"], zone["wick"])
        target = round(zone["low"] * (1 - TARGET_PCT), 2)
        risk = abs(stop - zone["center"])
        reward = abs(zone["center"] - target)
        if risk <= 0 or reward / risk < MIN_RR: continue
        return {"thesis": "T1", "side": "short", "zone": zone,
                "entry": zone["center"], "stop": stop, "target": target,
                "fill_type": "limit"}
    return None


# ─── T2: TREND FOLLOW (pullback EMA20 1h in trend_down_smooth) ────────────────

# T2 params
T2_PULLBACK_BUFFER_PCT = 0.002    # 0.2%
T2_SWING_LOOKBACK_1H   = 10
T2_STOP_BUFFER_PCT     = 0.003    # 0.3%
T2_TIMEOUT_MIN         = 90
T2_RSI_5M_MAX          = 70
T2_TARGET_PCT_DEFAULT  = 0.015    # fallback if regime doesn't carry target_pct
T2_INITIAL_CAP         = 0.7


def get_short_signal_t2(arr_5m, arr_15m, arr_1h, regime_decision):
    """T2 trend-follow short: pullback to EMA20 1h in trend_down_smooth.

    Spec: shared/trading/short-thesis-t2-trend-follow.md
    """
    h5, l5, c5, v5 = arr_5m
    h15, l15, c15 = arr_15m
    h1h, l1h = arr_1h
    if len(c5) < 30 or len(h1h) < 25: return None

    # Need 1h closes for EMA — we infer from highs+lows midpoint or use a separate
    # series. For simplicity use a synthetic close = (high+low)/2 1h.
    # Better: backtester should pass 1h closes. For now reconstruct typical price.
    closes_1h = (h1h + l1h) / 2.0    # approximation, refined when c1h is plumbed in

    # 1. EMA20 1h
    ema20_1h_series = _ema_array(closes_1h, 20)
    ema20_1h = float(ema20_1h_series[-1])

    # 2. Slope EMA20 1h DESCENDANT on last 6 bars
    if len(ema20_1h_series) < 6: return None
    if ema20_1h_series[-1] >= ema20_1h_series[-6]: return None

    # 3. Pullback to EMA20 1h: high_5m_last_3 >= ema20_1h * (1 - buffer)
    #    AND current_close < ema20_1h
    recent_high_5m = float(np.max(h5[-3:]))
    current_close  = float(c5[-1])
    threshold      = ema20_1h * (1 - T2_PULLBACK_BUFFER_PCT)
    if recent_high_5m < threshold: return None
    if current_close >= ema20_1h: return None

    # 4. RSI 5m ∈ [40, 70]
    cl5 = c5[-30:]
    rsi = _rsi(cl5, 14)
    if not (40 <= rsi <= T2_RSI_5M_MAX): return None

    # 5. RSI 5m not surachat persistent — last 3 bars not all > 70
    rsi_prev1 = _rsi(c5[:-1][-30:], 14) if len(c5) > 30 else rsi
    if rsi_prev1 > T2_RSI_5M_MAX + 5: return None

    # 6. Volume present: last bar 5m >= 50% avg(20)
    if len(v5) >= 21:
        avgv = float(np.mean(v5[-21:-1]))
        if avgv > 0 and v5[-1] < avgv * 0.5: return None

    # 7. Swing high 1h on lookback for stop placement
    swing_high_1h = float(np.max(h1h[-T2_SWING_LOOKBACK_1H:]))

    # 8. Stop placement
    stop_raw = max(swing_high_1h, current_close * 1.008) * (1 + T2_STOP_BUFFER_PCT)

    # 9. Target = current * (1 - target_pct from regime, fallback default)
    tp = float(regime_decision.get("target_pct") or T2_TARGET_PCT_DEFAULT)
    if tp <= 0: tp = T2_TARGET_PCT_DEFAULT
    target = current_close * (1 - tp)

    # 10. R:R check
    risk   = stop_raw - current_close
    reward = current_close - target
    if risk <= 0 or reward <= 0 or reward / risk < MIN_RR: return None

    return {
        "thesis": "T2", "side": "short",
        "entry": current_close, "stop": stop_raw, "target": target,
        "ema20_1h": ema20_1h, "swing_high_1h": swing_high_1h,
        "timeout_min": T2_TIMEOUT_MIN, "size_cap": T2_INITIAL_CAP,
        "fill_type": "market",
    }


# ─── T3: BREAKDOWN (support break with volume) ────────────────────────────────

T3_BREAKDOWN_BUFFER_PCT = 0.002    # 0.2%
T3_VOLUME_MULTIPLIER    = 1.5
T3_SUPPORT_LOOKBACK_1H  = 10
T3_RETEST_BUFFER_PCT    = 0.005    # 0.5%
T3_TARGET_PCT_FIXED     = 0.020    # TG3-B
T3_MIN_RR               = 1.5
T3_TIMEOUT_MIN          = 60
T3_RSI_5M_MAX           = 50
T3_INITIAL_CAP          = 0.5


def get_short_signal_t3(arr_5m, arr_15m, arr_1h, regime_decision):
    """T3 breakdown short: 1h support break with volume confirm.

    Spec: shared/trading/short-thesis-t3-breakdown.md
    Uses TG3-B target variant (fixed pct from entry).
    """
    h5, l5, c5, v5 = arr_5m
    h15, l15, c15 = arr_15m
    h1h, l1h = arr_1h
    if len(c5) < 30 or len(h1h) < T3_SUPPORT_LOOKBACK_1H + 5: return None

    current_close = float(c5[-1])
    prev_close    = float(c5[-2]) if len(c5) >= 2 else current_close

    # 1. Identify NEAREST support 1h below current price (not absolute plancher).
    #    The "support to break" is the closest pivot low just under us.
    support_level = _nearest_support_1h_below(l1h, current_close,
                                              lookback=20, min_dist_pct=0.001)
    if support_level is None: return None

    # 2. Confirmation breakdown:
    threshold_break = support_level * (1 - T3_BREAKDOWN_BUFFER_PCT)
    if current_close >= threshold_break: return None
    if prev_close <= support_level * (1 - T3_BREAKDOWN_BUFFER_PCT): return None

    # 3. Volume confirm: bar 5m de breakdown >= 1.5 * avg(20)
    if len(v5) < 21: return None
    avgv = float(np.mean(v5[-21:-1]))
    if avgv > 0 and v5[-1] < avgv * T3_VOLUME_MULTIPLIER: return None

    # 4. Not a fresh retest:
    #    Quick check — was support tested in the last 4 hours (48 bars 5m) by bouncing up?
    #    Test: lows in [-50:-3] reached support and recovered above it
    if len(l5) >= 50:
        sub_lows  = l5[-50:-3]
        sub_highs = h5[-50:-3]
        recent_touch = any(sub_lows <= support_level * 1.002) and any(sub_highs >= support_level * 1.008)
        if recent_touch: return None    # already retested+bounced, T1 territory

    # 5. RSI 5m < 50
    cl5 = c5[-30:]
    rsi = _rsi(cl5, 14)
    if rsi >= T3_RSI_5M_MAX: return None

    # 6. Anti flash-crash: ATR 5m < 3 × avg ATR (proxy: range last 5 bars vs avg)
    if len(h5) >= 30:
        atr_now = _atr_array(h5[-15:], l5[-15:], c5[-15:], period=10)
        atr_avg = _atr_array(h5[-30:], l5[-30:], c5[-30:], period=14)
        if atr_avg > 0 and atr_now > atr_avg * 3.0: return None

    # 7. Stop placement: retest of broken support
    stop = support_level * (1 + T3_RETEST_BUFFER_PCT)

    # 8. Target: TG3-B fixed pct from entry
    target = current_close * (1 - T3_TARGET_PCT_FIXED)

    # 9. R:R check (T3 has stricter MIN_RR)
    risk   = stop - current_close
    reward = current_close - target
    if risk <= 0 or reward <= 0 or reward / risk < T3_MIN_RR: return None

    return {
        "thesis": "T3", "side": "short",
        "entry": current_close, "stop": stop, "target": target,
        "support_level": support_level, "vol_ratio": v5[-1] / avgv if avgv > 0 else 0,
        "timeout_min": T3_TIMEOUT_MIN, "size_cap": T3_INITIAL_CAP,
        "fill_type": "market",
    }


# ─── T4: RALLY FAIL (lower-high after pump in downtrend) ──────────────────────

T4_PREV_HIGH_LOOKBACK   = (-30, -10)    # bars [-30:-10]
T4_CURRENT_HIGH_LOOKBACK = 8
T4_REJECTION_BUFFER_PCT = 0.005    # 0.5%
T4_STOP_BUFFER_PCT      = 0.003    # 0.3%
T4_TARGET_PCT           = 0.012    # 1.2%
T4_MIN_RR_T4            = 1.3
T4_TIMEOUT_MIN          = 45
T4_RSI_AT_SWING_MIN     = 65
T4_INITIAL_CAP          = 0.6


def get_short_signal_t4(arr_5m, arr_15m, arr_1h, regime_decision):
    """T4 rally fail short: lower-high after pump in downtrend.

    Spec: shared/trading/short-thesis-t4-rally-fail.md
    """
    h5, l5, c5, v5 = arr_5m
    h15, l15, c15 = arr_15m
    h1h, l1h = arr_1h
    if len(c5) < 35 or len(h1h) < 10: return None

    # 1. Identify previous swing high (bars [-30:-10])
    s, e = T4_PREV_HIGH_LOOKBACK
    if abs(s) > len(h5): return None
    prev_high = float(np.max(h5[s:e]))

    # 2. Identify current swing high (last 8 bars)
    idx_rel, current_swing_high = _swing_high_5m(h5, T4_CURRENT_HIGH_LOOKBACK)
    if current_swing_high is None: return None
    # Swing should be in last 5 bars (recent rejection)
    if T4_CURRENT_HIGH_LOOKBACK - idx_rel > 5: return None

    # 3. Lower-high check
    if current_swing_high >= prev_high: return None    # not a lower-high

    current_close = float(c5[-1])

    # 4. Rally amplitude check: must be at least 0.5% rally
    if current_swing_high < current_close * 1.005: return None

    # 5. Rally not excessive (anti-squeeze): no more than 4% above current
    if current_swing_high > current_close * 1.04: return None

    # 6. RSI at swing high > 65 (surachat local)
    # RSI calculé sur la fenêtre se terminant au swing high
    bars_back_from_end = T4_CURRENT_HIGH_LOOKBACK - idx_rel - 1
    if bars_back_from_end < 0: bars_back_from_end = 0
    sub_for_rsi_at_swing = c5[:len(c5) - bars_back_from_end] if bars_back_from_end > 0 else c5
    rsi_at_swing = _rsi(sub_for_rsi_at_swing[-30:], 14)
    if rsi_at_swing < T4_RSI_AT_SWING_MIN: return None

    # 7. Current RSI < RSI at swing (bear divergence intra-rally)
    rsi_now = _rsi(c5[-30:], 14)
    if rsi_now >= rsi_at_swing: return None

    # 8. Rejection confirmed: current_close < current_swing_high * (1 - buffer)
    if current_close >= current_swing_high * (1 - T4_REJECTION_BUFFER_PCT): return None

    # 9. Volume present
    if len(v5) >= 21:
        avgv = float(np.mean(v5[-21:-1]))
        if avgv > 0 and v5[-1] < avgv * 0.8: return None

    # 10. EMA20 1h slope still descending
    closes_1h = (h1h + l1h) / 2.0
    ema20_1h_series = _ema_array(closes_1h, 20)
    if len(ema20_1h_series) >= 4:
        if ema20_1h_series[-1] >= ema20_1h_series[-4]: return None

    # 11. Stop placement
    stop = current_swing_high * (1 + T4_STOP_BUFFER_PCT)

    # 12. Target
    target = current_close * (1 - T4_TARGET_PCT)

    # 13. R:R check
    risk   = stop - current_close
    reward = current_close - target
    if risk <= 0 or reward <= 0 or reward / risk < T4_MIN_RR_T4: return None

    return {
        "thesis": "T4", "side": "short",
        "entry": current_close, "stop": stop, "target": target,
        "prev_high": prev_high, "current_swing_high": current_swing_high,
        "rsi_at_swing": rsi_at_swing, "rsi_now": rsi_now,
        "timeout_min": T4_TIMEOUT_MIN, "size_cap": T4_INITIAL_CAP,
        "fill_type": "market",
    }


# ─── ROUTER (multi-thesis with mutual exclusion per matrix) ───────────────────

# States where ALL shorts are hard-blocked (regime gate strict)
_HARD_BLOCK_STATES = {
    "btc_unknown", "btc_chaos", "vol_extreme",
    "counter_cyclical_block", "btc_trend_eth_weak",
    "trend_up_smooth", "trend_up_choppy",
    "aligned_long_trend", "btc_led_long",
}

# T1 elligible states (zone bounce works)
_T1_ELIGIBLE_STATES = {
    "range_quiet", "aligned_short_trend", "btc_led_short", "trend_down_choppy",
}


def route_short_signal(arr_5m, arr_15m, arr_1h, regime_decision, enabled):
    """Returns (thesis_id, signal_dict) or (None, None).
    Enforces AT MOST 1 signal per evaluation via state guards + priority order.

    Matrix per shared/trading/short-thesis-matrix.md:
        - hard block states: nothing fires
        - trend_down_smooth: T2 → T4 → T3
        - trend_down_choppy: T4 → T3 → T1
        - neutral: T3 only
        - {range_quiet, aligned_short_trend, btc_led_short}: T1 only
    """
    state = regime_decision.get("state", "")
    conf  = regime_decision.get("confidence", 0)
    dirn  = regime_decision.get("direction", 0)

    if state in _HARD_BLOCK_STATES:
        return None, None

    # ── trend_down_smooth branch ──────────────────────────────────────────────
    if state == "trend_down_smooth":
        # T2 priority — strict gate on conf + dir
        if "t2" in enabled and conf >= 75 and dirn <= -0.85:
            sig = get_short_signal_t2(arr_5m, arr_15m, arr_1h, regime_decision)
            if sig: return "T2", sig
        # T4 secondary
        if "t4" in enabled:
            sig = get_short_signal_t4(arr_5m, arr_15m, arr_1h, regime_decision)
            if sig: return "T4", sig
        # T3 fallback
        if "t3" in enabled:
            sig = get_short_signal_t3(arr_5m, arr_15m, arr_1h, regime_decision)
            if sig: return "T3", sig
        return None, None

    # ── trend_down_choppy branch ──────────────────────────────────────────────
    if state == "trend_down_choppy":
        if "t4" in enabled:
            sig = get_short_signal_t4(arr_5m, arr_15m, arr_1h, regime_decision)
            if sig: return "T4", sig
        if "t3" in enabled:
            sig = get_short_signal_t3(arr_5m, arr_15m, arr_1h, regime_decision)
            if sig: return "T3", sig
        if "t1" in enabled:
            sig = get_short_signal_t1(arr_5m, arr_15m, arr_1h)
            if sig: return "T1", sig
        return None, None

    # ── neutral branch ────────────────────────────────────────────────────────
    if state == "neutral":
        if "t3" in enabled:
            sig = get_short_signal_t3(arr_5m, arr_15m, arr_1h, regime_decision)
            if sig: return "T3", sig
        return None, None

    # ── T1-only states ────────────────────────────────────────────────────────
    if state in _T1_ELIGIBLE_STATES - {"trend_down_choppy"}:
        if "t1" in enabled:
            sig = get_short_signal_t1(arr_5m, arr_15m, arr_1h)
            if sig: return "T1", sig
        # Optional T3 in range_quiet (off by default; enable with "t3_range")
        if state == "range_quiet" and "t3_range" in enabled:
            sig = get_short_signal_t3(arr_5m, arr_15m, arr_1h, regime_decision)
            if sig: return "T3", sig
        return None, None

    # Unknown / uncovered state → safe fallback (try T1 only if explicitly allowed)
    if "t1" in enabled and regime_decision.get("allow_short", False):
        sig = get_short_signal_t1(arr_5m, arr_15m, arr_1h)
        if sig: return "T1", sig

    return None, None


def route_long_signal(arr_5m, arr_15m, arr_1h, regime_decision, enabled):
    """Geo V4 long — only T1 zone bounce for now."""
    if "long_t1" not in enabled: return None, None
    allow_long = regime_decision.get("allow_long", False)
    if not allow_long: return None, None
    sig = get_long_signal_t1(arr_5m, arr_15m, arr_1h)
    if sig: return "T1", sig
    return None, None


# ─── DATA LOADING (callable separately for grid search reuse) ──────────────────

def load_data_for_period(symbols, start, end, verbose=True):
    """Load all symbols' 5m+resamples + BTC 4h. Returns (data_dict, df_btc).
    Reusable across multiple backtest runs (grid search)."""
    if verbose: print(f"Loading data for {symbols} {start} → {end}...")
    data = {}
    for sym in symbols:
        sym_usd = sym.replace("/", "")
        df5 = load_symbol_5m(sym_usd, start, end)
        data[sym] = {
            "5min":  df5,
            "15min": resample_ohlcv(df5, "15min"),
            "1h":    resample_ohlcv(df5, "1h"),
            "4h":    resample_ohlcv(df5, "4h"),
        }
        if verbose:
            print(f"  {sym}: 5m={len(df5)} | 15m={len(data[sym]['15min'])} | 1h={len(data[sym]['1h'])} | 4h={len(data[sym]['4h'])}")
    df_btc = load_btc_4h()
    if verbose: print(f"  BTC 4h: {len(df_btc)} bars")
    return data, df_btc


# ─── MAIN BACKTEST LOOP ───────────────────────────────────────────────────────

def run_backtest(symbols, start, end, enabled, verbose=True, data=None, df_btc=None,
                 regime_cache_shared=None):
    """Returns dict with trades, equity_curve, final equity, stats per thesis.

    For grid search: pass pre-loaded `data` and `df_btc` to avoid re-loading.
    Pass `regime_cache_shared` (mutable dict) to persist regime evaluations
    across multiple runs (params don't affect regime). Cache is populated
    on first run, reused on subsequent. Cuts per-combo runtime ~10×.
    """
    if verbose:
        print(f"\n{'='*72}\nBacktest range: {start} → {end}\nSymbols: {symbols}\nEnabled: {enabled}\n{'='*72}")
    rng = random.Random(RNG_SEED)

    # Load data (or reuse pre-loaded)
    if data is None or df_btc is None:
        data, df_btc = load_data_for_period(symbols, start, end, verbose=verbose)

    # Build broker + regime engine
    broker = BacktestBroker(data)
    btc_fetcher = make_btc_fetcher(df_btc)
    from crypto_regime import CryptoRegime
    regime_eng = CryptoRegime(broker, fetch_btc=btc_fetcher)

    # Build reference timeline (intersection of 5m timestamps across symbols)
    common_5m = sorted(set.intersection(*[set(data[s]["5min"].index) for s in symbols]))
    n = len(common_5m)
    print(f"\nCommon 5m timeline: {n} bars")

    # Per-symbol arrays for fast access
    arrays = {}
    for s in symbols:
        d5 = data[s]["5min"]; d15 = data[s]["15min"]; d1h = data[s]["1h"]
        arrays[s] = {
            "idx5":  d5.index, "pos5":  {ts: i for i, ts in enumerate(d5.index)},
            "h5": d5["high"].values, "l5": d5["low"].values,
            "c5": d5["close"].values, "v5": d5["volume"].values, "o5": d5["open"].values,
            "idx15": d15.index, "h15": d15["high"].values,
            "l15": d15["low"].values, "c15": d15["close"].values,
            "idx1h": d1h.index, "h1h": d1h["high"].values, "l1h": d1h["low"].values,
        }

    def tf_pos(idx_list, ts):
        return max(0, bisect.bisect_right(idx_list, ts) - 1)

    # Pool
    pool = SharedPool(initial_capital=CAPITAL)
    closed_trades = []
    equity_curve = []

    # Regime evaluation cache (1 per 5m bar per symbol).
    # If regime_cache_shared is provided, reuse across runs (params don't
    # affect regime decisions — they only affect signal triggers downstream).
    if regime_cache_shared is None:
        regime_cache_per_symbol = {s: {} for s in symbols}
    else:
        for s in symbols:
            regime_cache_shared.setdefault(s, {})
        regime_cache_per_symbol = regime_cache_shared

    # Main loop
    REGIME_REFRESH_EVERY = 12   # 1 regime eval per hour (every 12 × 5min)
    start_idx = 55              # need 55 bars of history minimum
    last_progress = time.time()

    for i in range(start_idx, n - 1):
        t_now  = common_5m[i]
        t_next = common_5m[i + 1]

        # ── Close positions at t_next ─────────────────────────────────────────
        for p in list(pool.open_positions):
            sym = p["symbol"]
            ni = arrays[sym]["pos5"].get(t_next)
            if ni is None: continue
            hi = arrays[sym]["h5"][ni]
            lo = arrays[sym]["l5"][ni]
            cl = arrays[sym]["c5"][ni]
            if p["side"] == "long":
                sh, th = lo <= p["stop"], hi >= p["target"]
            else:
                sh, th = hi >= p["stop"], lo <= p["target"]
            # Per-thesis timeout: stored as `timeout_bars` on position (set at open).
            # None → use default global TIMEOUT_BARS.
            timeout_bars_pos = p.get("timeout_bars")
            if timeout_bars_pos is None:
                timeout_bars_pos = TIMEOUT_BARS
            timeout = (i - p["bar_idx"]) >= timeout_bars_pos
            ep, er = None, None
            if sh and th:
                if rng.random() < 0.5:
                    ep, er = p["stop"], "stop"
                else:
                    ep, er = p["target"], "target"
            elif sh:    ep, er = p["stop"], "stop"
            elif th:    ep, er = p["target"], "target"
            elif timeout: ep, er = cl, "timeout"
            if ep is not None:
                # Slippage on stop and timeout (market exits), none on target (limit fill)
                if er == "target":
                    ep_adj = ep
                    fee_rate = FEE_MAKER
                else:
                    # Market exit — worse fill
                    if p["side"] == "long":
                        ep_adj = ep * (1 - SLIPPAGE_PCT)     # sell lower
                    else:
                        ep_adj = ep * (1 + SLIPPAGE_PCT)     # buy back higher
                    fee_rate = FEE_TAKER
                fee_exit = ep_adj * p["qty"] * fee_rate
                closed = pool.close(p["id"], ep_adj, er, fee_exit)
                if closed:
                    closed_trades.append({
                        **closed,
                        "entry_time": common_5m[p["bar_idx"]],
                        "exit_time":  t_next,
                    })

        equity_curve.append((t_now, pool.equity))

        if pool.equity < 20: continue

        # ── Regime evaluation (sparse, ~hourly) ───────────────────────────────
        regime_per_sym = {}
        for sym in symbols:
            bucket = i // REGIME_REFRESH_EVERY
            cache = regime_cache_per_symbol[sym]
            if bucket not in cache:
                ts_unix = t_now.timestamp()
                try:
                    rd = regime_eng.evaluate(sym, as_of=ts_unix)
                except Exception as e:
                    rd = {"state": "unknown", "allow_long": False, "allow_short": False,
                          "confidence": 0, "direction": 0, "size_multiplier": 0, "target_pct": 0}
                cache[bucket] = rd
                # Prune stale
                if len(cache) > 4:
                    for k in list(cache.keys())[:-2]:
                        del cache[k]
            regime_per_sym[sym] = cache[bucket]

        # ── Signal generation for each symbol ──────────────────────────────────
        for sym in symbols:
            if len(pool.open_positions) >= MAX_SIM_GLOBAL: break
            ci5  = arrays[sym]["pos5"].get(t_now)
            if ci5 is None: continue
            ci15 = tf_pos(arrays[sym]["idx15"], t_now)
            ci1h = tf_pos(arrays[sym]["idx1h"], t_now)
            arr5  = (arrays[sym]["h5"][:ci5+1], arrays[sym]["l5"][:ci5+1],
                     arrays[sym]["c5"][:ci5+1], arrays[sym]["v5"][:ci5+1])
            arr15 = (arrays[sym]["h15"][:ci15+1], arrays[sym]["l15"][:ci15+1],
                     arrays[sym]["c15"][:ci15+1])
            arr1h = (arrays[sym]["h1h"][:ci1h+1], arrays[sym]["l1h"][:ci1h+1])
            rd = regime_per_sym[sym]

            # Long expert
            t_id, long_sig = route_long_signal(arr5, arr15, arr1h, rd, enabled)
            if long_sig:
                _try_enter(pool, "long_geo_v4", t_id, sym, long_sig, rd, i,
                           common_5m, arrays[sym], closed_trades, rng)
                continue   # one entry per symbol per cycle

            # Short expert
            t_id, short_sig = route_short_signal(arr5, arr15, arr1h, rd, enabled)
            if short_sig:
                _try_enter(pool, "short_expert", t_id, sym, short_sig, rd, i,
                           common_5m, arrays[sym], closed_trades, rng)

        if verbose and time.time() - last_progress > 5:
            print(f"  [{t_now}] equity=${pool.equity:.2f}  open={len(pool.open_positions)}  closed={len(closed_trades)}")
            last_progress = time.time()

    return {
        "trades": closed_trades,
        "equity_curve": equity_curve,
        "final_equity": pool.equity,
        "start_capital": CAPITAL,
    }


def _try_enter(pool, expert_id, thesis_id, symbol, signal, regime, i,
               common_5m, arr_sym, closed_trades, rng):
    """Common entry path: pool.acquire → simulate fill → pool.open.
    Slippage applied for market fills (T2/T3/T4), not for limit fills (T1)."""
    size_mult = max(0.3, regime.get("size_multiplier", 1.0)) if regime.get("allow_long") or regime.get("allow_short") else 1.0
    size_cap = float(signal.get("size_cap", 1.0))
    effective_size_mult = size_mult * size_cap
    acq = pool.acquire(expert_id, symbol, signal["side"], POS_PCT, effective_size_mult)
    if acq is None: return False

    entry_raw = signal["entry"]
    fill_type = signal.get("fill_type", "limit")
    # Apply slippage on market fills:
    #   - long market entry: worse fill = pay higher
    #   - short market entry: worse fill = sell at lower price
    if fill_type == "market":
        if signal["side"] == "long":
            entry = entry_raw * (1 + SLIPPAGE_PCT)
        else:
            entry = entry_raw * (1 - SLIPPAGE_PCT)
        fee_rate = FEE_TAKER    # market = taker
    else:
        entry = entry_raw
        fee_rate = FEE_MAKER    # limit = maker

    deploy = acq["deploy"]
    qty = deploy / entry
    if qty <= 0 or qty * entry < 5: return False
    fee_entry = entry * qty * fee_rate

    timeout_min = signal.get("timeout_min")
    timeout_bars = (timeout_min // 5) if timeout_min else None
    pool.open(
        expert_id=expert_id, thesis_id=thesis_id,
        symbol=symbol, side=signal["side"],
        deploy=deploy, entry_price=entry,
        stop=signal["stop"], target=signal["target"],
        bar_idx=i, fee_entry=fee_entry, timeout_bars=timeout_bars,
    )
    return True


# ─── STATS + REPORT ───────────────────────────────────────────────────────────

def report(result):
    trades = result["trades"]
    final = result["final_equity"]; start = result["start_capital"]
    print("\n" + "=" * 72)
    print(f"FINAL EQUITY: ${final:.2f}  (start ${start:.2f}, delta ${final-start:+.2f}, {(final/start-1)*100:+.2f}%)")
    print(f"N closed trades: {len(trades)}")
    if not trades: return

    df = pd.DataFrame(trades)
    df["win"] = df["pnl_net"] > 0
    by_expert = df.groupby("expert_id").agg(
        n=("pnl_net", "size"),
        wins=("win", "sum"),
        pnl=("pnl_net", "sum"),
        avg=("pnl_net", "mean"),
    )
    by_expert["win_rate"] = (by_expert["wins"] / by_expert["n"] * 100).round(1)
    print("\n── BY EXPERT ──")
    print(by_expert.round(2))

    by_thesis = df.groupby(["expert_id", "thesis_id"]).agg(
        n=("pnl_net", "size"),
        wins=("win", "sum"),
        pnl=("pnl_net", "sum"),
        avg=("pnl_net", "mean"),
    )
    by_thesis["win_rate"] = (by_thesis["wins"] / by_thesis["n"] * 100).round(1)
    print("\n── BY THESIS ──")
    print(by_thesis.round(2))

    by_side = df.groupby("side").agg(
        n=("pnl_net", "size"),
        wins=("win", "sum"),
        pnl=("pnl_net", "sum"),
    )
    by_side["win_rate"] = (by_side["wins"] / by_side["n"] * 100).round(1)
    print("\n── BY SIDE ──")
    print(by_side.round(2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--end",   default="2023-12-31")
    parser.add_argument("--symbols", default="ETH/USD,SOL/USD")
    parser.add_argument("--enable", default="long_t1,t1",
                        help="comma list: long_t1 (Geo V4 long), t1, t2, t3, t4 (short theses)")
    args = parser.parse_args()

    symbols = args.symbols.split(",")
    enabled = set(args.enable.split(","))

    res = run_backtest(symbols, args.start, args.end, enabled, verbose=True)
    report(res)


if __name__ == "__main__":
    main()
