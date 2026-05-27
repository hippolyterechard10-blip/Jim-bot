"""
crypto_regime.py — Crypto-native regime engine (PAPER validation only).

Replaces the macro VIX/SPY regime with multi-timeframe crypto signals:
  • EMA20/EMA50 trend on 4h + 1h
  • ADX(14) trend strength on 4h
  • Dow structure (HH/HL) on 4h
  • ATR% volatility state on 1h
  • BTC 4h stress proxy

Output: direction ∈ [-1,+1], confidence ∈ [0,100], state label, trading policy.

NOT for live deployment until shadow + paper validation. Use under
config.USE_CRYPTO_REGIME_GATE flag, easy rollback by setting it False.
"""
from __future__ import annotations
import logging
import time
from typing import Any

import numpy as np
import requests

logger = logging.getLogger(__name__)

# ─── Module-level state (read by dashboard) ───────────────────────────────────
_LAST_EVAL: dict[str, dict] = {}   # symbol → most recent evaluation
_CACHE: dict[tuple, tuple] = {}    # (symbol, tf) → (timestamp, bars)
_CACHE_TTL = 60                     # seconds
_BTC_CACHE: dict | None = None      # cached BTC fetch (stress, known, info, ts)
_BTC_CACHE_TTL = 300                # 5 min — BTC market state évolue lentement
_KRAKEN_FUTURES_OHLC = "https://futures.kraken.com/api/charts/v1/trade/PF_XBTUSD/{interval}"


# ─── Indicator helpers ────────────────────────────────────────────────────────

def _ema(values: np.ndarray, period: int) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    if len(arr) == 0: return arr
    alpha = 2.0 / (period + 1)
    out = np.empty_like(arr)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i-1]
    return out


def _wilder(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder's smoothing (used by ATR/ADX). alpha = 1/period."""
    arr = np.asarray(values, dtype=float)
    n = len(arr)
    if n < period: return np.zeros(n)
    out = np.zeros(n)
    out[period-1] = arr[:period].mean()
    for i in range(period, n):
        out[i] = out[i-1] + (arr[i] - out[i-1]) / period
    return out


def _atr(highs, lows, closes, period: int = 14) -> float:
    h = np.asarray(highs, dtype=float)
    l = np.asarray(lows, dtype=float)
    c = np.asarray(closes, dtype=float)
    if len(h) < period + 1: return 0.0
    tr = np.maximum.reduce([
        h[1:] - l[1:],
        np.abs(h[1:] - c[:-1]),
        np.abs(l[1:] - c[:-1]),
    ])
    smoothed = _wilder(tr, period)
    return float(smoothed[-1]) if len(smoothed) > 0 else 0.0


def _adx(highs, lows, closes, period: int = 14) -> float:
    """Returns last ADX value, 0-100."""
    h = np.asarray(highs, dtype=float)
    l = np.asarray(lows, dtype=float)
    c = np.asarray(closes, dtype=float)
    if len(h) < period * 2 + 1: return 0.0

    up_move   = h[1:] - h[:-1]
    down_move = l[:-1] - l[1:]
    plus_dm  = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = np.maximum.reduce([
        h[1:] - l[1:],
        np.abs(h[1:] - c[:-1]),
        np.abs(l[1:] - c[:-1]),
    ])

    smooth_tr        = _wilder(tr, period)
    smooth_plus_dm   = _wilder(plus_dm, period)
    smooth_minus_dm  = _wilder(minus_dm, period)

    eps = 1e-9
    plus_di  = 100.0 * smooth_plus_dm  / (smooth_tr + eps)
    minus_di = 100.0 * smooth_minus_dm / (smooth_tr + eps)
    dx = 100.0 * np.abs(plus_di - minus_di) / (plus_di + minus_di + eps)
    adx = _wilder(dx, period)
    return float(adx[-1]) if len(adx) > 0 else 0.0


def _trend_score(closes: np.ndarray, period_fast=20, period_slow=50) -> tuple[float, float, float]:
    """Returns (signed_trend ∈ [-1,1], ema_fast_last, ema_slow_last).
    Magnitude reflects 'force' from EMA spread + recent slope.
    """
    if len(closes) < period_slow + 5:
        return 0.0, float(closes[-1] if len(closes) else 0), float(closes[-1] if len(closes) else 0)
    ema_f = _ema(closes, period_fast)
    ema_s = _ema(closes, period_slow)
    direction = np.sign(ema_f[-1] - ema_s[-1])
    spread = abs(ema_f[-1] - ema_s[-1]) / max(ema_s[-1], 1e-9)
    spread_force = min(spread * 100, 1.0)      # 1% spread → full force
    slope_force  = 1.0 if ema_f[-1] > ema_f[-5] and direction > 0 else \
                   1.0 if ema_f[-1] < ema_f[-5] and direction < 0 else 0.4
    return float(direction * spread_force * slope_force), float(ema_f[-1]), float(ema_s[-1])


def _structure_score(highs: np.ndarray, lows: np.ndarray) -> int:
    """Dow theory simplified: compare highs[-1] vs [-3] vs [-5], same for lows."""
    if len(highs) < 7: return 0
    h1, h2, h3 = highs[-1], highs[-3], highs[-5]
    l1, l2, l3 = lows[-1],  lows[-3],  lows[-5]
    if h1 > h2 > h3 and l1 > l2 > l3: return +1
    if h1 < h2 < h3 and l1 < l2 < l3: return -1
    return 0


def _vol_state(atr_pct: float) -> str:
    """Static thresholds (refined later by percentile when we have history)."""
    if atr_pct < 0.005: return "low"      # <0.5%
    if atr_pct < 0.015: return "normal"   # 0.5-1.5%
    if atr_pct < 0.030: return "high"     # 1.5-3%
    return "extreme"


def _vol_quality_factor(vol_state: str) -> float:
    return {
        "low":     0.7,   # very calm → trend signals less reliable
        "normal":  1.0,
        "high":    0.4,
        "extreme": 0.0,
    }.get(vol_state, 0.5)


# ─── Bars cache ───────────────────────────────────────────────────────────────

def _cached_bars(broker, symbol: str, tf: str, limit: int):
    key = (symbol, tf)
    now = time.time()
    if key in _CACHE:
        ts, bars = _CACHE[key]
        if now - ts < _CACHE_TTL:
            return bars
    try:
        bars = broker.get_bars(symbol, tf, limit=limit)
    except Exception as e:
        logger.warning(f"[CRYPTO_REGIME] get_bars({symbol},{tf}) failed: {e}")
        return None
    if bars is None or len(bars) == 0:
        return None
    _CACHE[key] = (now, bars)
    return bars


# ─── BTC classification (direct Kraken Futures public API) ────────────────────

def _fetch_btc_kraken(interval: str = "4h", limit: int = 50) -> tuple | None:
    """Fetch BTC perp OHLC directly from Kraken Futures public chart API.
    Returns (highs, lows, closes) or None on any failure.
    """
    url = _KRAKEN_FUTURES_OHLC.format(interval=interval)
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "JimBot/1.0"})
    except Exception as e:
        logger.warning(f"[CRYPTO_REGIME] BTC fetch network error: {e}")
        return None
    if r.status_code != 200:
        logger.warning(f"[CRYPTO_REGIME] BTC fetch HTTP {r.status_code}")
        return None
    try:
        data = r.json()
    except Exception as e:
        logger.warning(f"[CRYPTO_REGIME] BTC fetch JSON parse: {e}")
        return None
    candles = data.get("candles") or data.get("series") or []
    if not candles:
        logger.warning(f"[CRYPTO_REGIME] BTC fetch empty candles keys={list(data.keys())[:5]}")
        return None
    candles = candles[-limit:]
    try:
        highs  = [float(c.get("high",  c.get("h"))) for c in candles]
        lows   = [float(c.get("low",   c.get("l"))) for c in candles]
        closes = [float(c.get("close", c.get("c"))) for c in candles]
    except Exception as e:
        logger.warning(f"[CRYPTO_REGIME] BTC fetch parse fields: {e}")
        return None
    if len(closes) < 6:
        return None
    return highs, lows, closes


def _classify_btc() -> tuple[str, dict]:
    """Classify BTC into one of 5 states:
        unknown    : fetch fail or invalid data
        calm       : small move, low/normal vol
        trend_down : move_4bars < -2%, ATR moderate, ADX > 25
        trend_up   : move_4bars > +2%, ATR moderate, ADX > 25
        chaos      : large move + extreme vol OR no direction
    Returns (state_label, debug_info). State carries directional information
    AND structural quality — caller maps it to a trading policy.
    """
    global _BTC_CACHE
    now = time.time()
    if _BTC_CACHE and (now - _BTC_CACHE["ts"]) < _BTC_CACHE_TTL:
        return _BTC_CACHE["state"], _BTC_CACHE["info"]

    fetched = _fetch_btc_kraken("4h", limit=50)
    if fetched is None:
        info = {"available": False, "source": "kraken_futures", "error": "fetch_failed"}
        _BTC_CACHE = {"ts": now, "state": "unknown", "info": info}
        return "unknown", info

    highs, lows, closes = fetched
    if closes[-4] <= 0 or closes[-1] <= 0 or len(closes) < 30:
        info = {"available": False, "source": "kraken_futures", "error": "insufficient_data"}
        _BTC_CACHE = {"ts": now, "state": "unknown", "info": info}
        return "unknown", info

    move_4bars = (closes[-1] - closes[-4]) / closes[-4]
    atr_pct    = _atr(highs, lows, closes, 14) / closes[-1]
    adx_4h     = _adx(highs, lows, closes, 14)

    # Classification cascade (order matters)
    if atr_pct > 0.030:
        state = "chaos"
    elif abs(move_4bars) >= 0.02 and adx_4h < 20:
        # Large move but no structural trend force → chaotic wick
        state = "chaos"
    elif move_4bars < -0.02 and atr_pct < 0.025 and adx_4h > 25:
        state = "trend_down"
    elif move_4bars > +0.02 and atr_pct < 0.025 and adx_4h > 25:
        state = "trend_up"
    else:
        state = "calm"

    info = {
        "available":  True,
        "source":     "kraken_futures",
        "symbol":     "PF_XBTUSD",
        "last_close": round(closes[-1], 2),
        "move_4bars": round(move_4bars * 100, 2),
        "atr_pct":    round(atr_pct * 100, 3),
        "adx_4h":     round(adx_4h, 1),
    }
    _BTC_CACHE = {"ts": now, "state": state, "info": info}
    return state, info


def _btc_directional_bias(btc_state: str) -> int:
    """Returns -1/0/+1 — only trend states give directional signal."""
    return {"trend_down": -1, "trend_up": +1}.get(btc_state, 0)


# ─── ETH signal classification ────────────────────────────────────────────────

def _classify_eth_signal(direction: float, confidence: float, vol_state: str) -> str:
    """Bucket the ETH/SOL crypto-side signal:
       long_signal | short_signal | range_quiet | weak"""
    if confidence >= 70 and direction >  0.5: return "long_signal"
    if confidence >= 70 and direction < -0.5: return "short_signal"
    if abs(direction) < 0.3 and vol_state == "low" and confidence >= 40:
        return "range_quiet"
    return "weak"


# ─── 2D Decision table : (btc_state × eth_signal) → trading policy ────────────
# Returns dict with keys: state, allow_long, allow_short, size_mult, target_pct, reason

def _decide_policy(eth_signal: str, btc_state: str, vol_state: str,
                   direction: float, confidence: float) -> dict:
    block = {"allow_long": False, "allow_short": False, "size_mult": 0.0, "target_pct": 0.0}

    # ── Hard overrides ────────────────────────────────────────────────────────
    if btc_state == "unknown":
        return {**block, "state": "btc_unknown",
                "reason": "btc data missing → fail-safe no-trade"}
    if btc_state == "chaos":
        return {**block, "state": "btc_chaos",
                "reason": "btc chaotic move (high atr + no adx) — no clear read"}
    if vol_state == "extreme":
        return {**block, "state": "vol_extreme",
                "reason": f"eth vol extreme ({vol_state}) — dangerous"}

    # ── Counter-cyclical blocks (btc and eth disagree) ────────────────────────
    if btc_state == "trend_down" and eth_signal == "long_signal":
        return {**block, "state": "counter_cyclical_block",
                "reason": f"btc trend_down vs eth long_signal — block long (dir=+{direction:.2f})"}
    if btc_state == "trend_up" and eth_signal == "short_signal":
        return {**block, "state": "counter_cyclical_block",
                "reason": f"btc trend_up vs eth short_signal — block short (dir={direction:+.2f})"}

    # ── Aligned strong trends (BTC + ETH confirm same direction) ──────────────
    if btc_state == "trend_down" and eth_signal == "short_signal":
        return {"state": "aligned_short_trend", "allow_long": False, "allow_short": True,
                "size_mult": 1.0, "target_pct": 0.015,
                "reason": f"btc + eth both down — aligned short trend (dir={direction:+.2f} conf={confidence:.0f})"}
    if btc_state == "trend_up" and eth_signal == "long_signal":
        return {"state": "aligned_long_trend", "allow_long": True, "allow_short": False,
                "size_mult": 1.0, "target_pct": 0.015,
                "reason": f"btc + eth both up — aligned long trend (dir={direction:+.2f} conf={confidence:.0f})"}

    # ── BTC overrides ETH range with directional bias ─────────────────────────
    if btc_state == "trend_down" and eth_signal == "range_quiet":
        return {"state": "btc_led_short", "allow_long": False, "allow_short": True,
                "size_mult": 0.5, "target_pct": 0.010,
                "reason": "btc trend_down overrides eth range → short bias"}
    if btc_state == "trend_up" and eth_signal == "range_quiet":
        return {"state": "btc_led_long", "allow_long": True, "allow_short": False,
                "size_mult": 0.5, "target_pct": 0.010,
                "reason": "btc trend_up overrides eth range → long bias"}

    # ── BTC trend + ETH weak → wait, don't take counter-trend nor blind ───────
    if btc_state in ("trend_down", "trend_up") and eth_signal == "weak":
        return {**block, "state": "btc_trend_eth_weak",
                "reason": f"btc {btc_state} but eth signal weak (dir={direction:+.2f} conf={confidence:.0f}) — wait"}

    # ── BTC calm: ETH-led decisions ───────────────────────────────────────────
    if btc_state == "calm":
        if eth_signal == "long_signal":
            if vol_state == "high":
                return {"state": "trend_up_choppy", "allow_long": True, "allow_short": False,
                        "size_mult": 0.5, "target_pct": 0.010,
                        "reason": f"eth long signal, btc calm, vol high"}
            return {"state": "trend_up_smooth", "allow_long": True, "allow_short": False,
                    "size_mult": 1.0, "target_pct": 0.015,
                    "reason": f"eth long signal, btc calm, vol {vol_state}"}
        if eth_signal == "short_signal":
            if vol_state == "high":
                return {"state": "trend_down_choppy", "allow_long": False, "allow_short": True,
                        "size_mult": 0.5, "target_pct": 0.010,
                        "reason": f"eth short signal, btc calm, vol high"}
            return {"state": "trend_down_smooth", "allow_long": False, "allow_short": True,
                    "size_mult": 1.0, "target_pct": 0.015,
                    "reason": f"eth short signal, btc calm, vol {vol_state}"}
        if eth_signal == "range_quiet":
            return {"state": "range_quiet", "allow_long": True, "allow_short": True,
                    "size_mult": 0.3, "target_pct": 0.009,
                    "reason": "btc calm + eth range quiet — both sides small"}
        # weak signal in calm market
        return {**block, "state": "neutral",
                "reason": f"eth signal weak (dir={direction:+.2f} conf={confidence:.0f}), btc calm — wait"}

    # Catch-all (shouldn't reach)
    return {**block, "state": "neutral", "reason": "uncovered case → fail-safe"}


# ─── Main class ───────────────────────────────────────────────────────────────

class CryptoRegime:
    """Crypto-native regime evaluator. One per process is enough."""

    def __init__(self, broker):
        self.broker = broker

    def evaluate(self, symbol: str) -> dict:
        """Evaluate regime for one symbol. Returns the full decision dict.
        Also writes to _LAST_EVAL[symbol] for dashboard consumption."""
        bars_4h = _cached_bars(self.broker, symbol, "4Hour", limit=80)
        bars_1h = _cached_bars(self.broker, symbol, "1Hour", limit=80)

        # Defensive: if data missing, return safe neutral
        if bars_4h is None or len(bars_4h) < 55 or bars_1h is None or len(bars_1h) < 55:
            result = self._safe_neutral(symbol, reason="insufficient_bars")
            _LAST_EVAL[symbol] = result
            return result

        closes_4h = bars_4h["close"].values
        highs_4h  = bars_4h["high"].values
        lows_4h   = bars_4h["low"].values
        closes_1h = bars_1h["close"].values
        highs_1h  = bars_1h["high"].values
        lows_1h   = bars_1h["low"].values

        # Trend scores
        trend_4h, ema20_4h, ema50_4h = _trend_score(closes_4h)
        trend_1h, ema20_1h, ema50_1h = _trend_score(closes_1h)
        direction_raw = 0.6 * trend_4h + 0.4 * trend_1h

        # Structure
        structure = _structure_score(highs_4h, lows_4h)
        if structure != 0 and np.sign(structure) == np.sign(direction_raw):
            struct_agreement = 1.0
        elif structure == 0:
            struct_agreement = 0.5
        else:
            struct_agreement = 0.0

        # Volatility
        atr_abs = _atr(highs_1h, lows_1h, closes_1h, 14)
        atr_pct = atr_abs / max(closes_1h[-1], 1e-9)
        vol_state = _vol_state(atr_pct)
        vol_factor = _vol_quality_factor(vol_state)

        # ADX as confirming trend force
        adx_4h = _adx(highs_4h, lows_4h, closes_4h, 14)
        adx_factor = min(adx_4h / 30.0, 1.0)   # cap at 30

        # Combine into direction (already capped) and confidence
        direction = max(-1.0, min(1.0, direction_raw))
        confidence = 100.0 * abs(direction) * struct_agreement * vol_factor * (0.5 + 0.5 * adx_factor)
        confidence = max(0.0, min(100.0, confidence))

        # BTC classification (direct Kraken Futures API)
        btc_state, btc_dbg = _classify_btc()
        btc_known = btc_state != "unknown"
        btc_bias = _btc_directional_bias(btc_state)

        # ETH/SOL signal bucket
        eth_signal = _classify_eth_signal(direction, confidence, vol_state)

        # Lookup 2D decision table (btc_state × eth_signal)
        policy = _decide_policy(eth_signal, btc_state, vol_state, direction, confidence)

        result = {
            "symbol":           symbol,
            "ts":               time.time(),
            "state":            policy["state"],
            "direction":        round(direction, 3),
            "confidence":       round(confidence, 1),
            "vol_state":        vol_state,
            "eth_signal":       eth_signal,
            "btc_state":        btc_state,
            "btc_directional_bias": btc_bias,
            "btc_stress_known": btc_known,
            "allow_long":       policy["allow_long"],
            "allow_short":      policy["allow_short"],
            "size_multiplier":  policy["size_mult"],
            "target_pct":       policy["target_pct"],
            "policy_reason":    policy["reason"],
            "reasons":          [policy["reason"]],
            "features": {
                "trend_4h":      round(trend_4h, 3),
                "trend_1h":      round(trend_1h, 3),
                "ema20_4h":      round(ema20_4h, 4),
                "ema50_4h":      round(ema50_4h, 4),
                "ema20_1h":      round(ema20_1h, 4),
                "ema50_1h":      round(ema50_1h, 4),
                "structure_4h":  int(structure),
                "atr_pct_1h":    round(atr_pct * 100, 3),
                "adx_4h":        round(adx_4h, 1),
                "btc_info":      btc_dbg,
            },
        }
        _LAST_EVAL[symbol] = result
        return result

    @staticmethod
    def _safe_neutral(symbol: str, reason: str) -> dict:
        return {
            "symbol":           symbol,
            "ts":               time.time(),
            "state":            "neutral",
            "direction":        0.0,
            "confidence":       0.0,
            "vol_state":        "unknown",
            "eth_signal":       "weak",
            "btc_state":        "unknown",
            "btc_directional_bias": 0,
            "btc_stress_known": False,
            "allow_long":       False,
            "allow_short":      False,
            "size_multiplier":  0.0,
            "target_pct":       0.0,
            "policy_reason":    reason,
            "reasons":          [reason],
            "features":         {},
        }


# ─── Dashboard accessors ──────────────────────────────────────────────────────

def get_last_eval(symbol: str | None = None) -> Any:
    if symbol is None:
        return {k: dict(v) for k, v in _LAST_EVAL.items()}
    return dict(_LAST_EVAL.get(symbol, {}))


def reset_cache():
    """For testing / debugging."""
    _CACHE.clear()
    _LAST_EVAL.clear()
