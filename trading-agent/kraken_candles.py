"""
kraken_candles.py — Shared OHLCV fetcher for live brokers.

Replaces yfinance as the signal data source for both `broker_kraken_paper.py`
and `kraken_broker.py`. Public Kraken Futures candles endpoint, no auth.

Used by:
  - KrakenPaperBroker.get_bars() (Phase 4.1 paper)
  - KrakenBroker.get_bars()      (Live)

Cap : 2000 candles/call (Kraken-side). All live callers stay well under.
"""
from __future__ import annotations
import logging
import time

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_URL = "https://futures.kraken.com/api/charts/v1/trade/{symbol}/{interval}"

_DB_TO_KRAKEN = {
    "ETH/USD": "PF_ETHUSD",
    "SOL/USD": "PF_SOLUSD",
}

_TF_MAP = {
    "1Min":  "1m",  "5Min":  "5m",  "15Min": "15m",
    "30Min": "30m", "1Hour": "1h",  "4Hour": "4h",
    "12Hour": "12h", "1Day":  "1d", "1Week": "1w",
}


def fetch_kraken_bars(db_symbol: str, timeframe: str, limit: int) -> pd.DataFrame | None:
    """Fetch latest OHLCV candles from Kraken Futures public chart API.

    db_symbol  : "ETH/USD" or "SOL/USD" (database convention)
    timeframe  : "1Min", "5Min", "15Min", "1Hour", "4Hour", ... (broker convention)
    limit      : number of most recent candles to return

    Returns a tz-aware UTC DataFrame indexed by candle close time, with columns
    [open, high, low, close, volume, symbol], or None on any failure.

    Mirrors prior yfinance get_bars output shape so callers are unchanged.
    """
    kraken_sym = _DB_TO_KRAKEN.get(db_symbol, db_symbol)
    interval   = _TF_MAP.get(timeframe)
    if interval is None:
        logger.warning(f"[kraken_candles] unknown timeframe {timeframe}")
        return None

    url = _URL.format(symbol=kraken_sym, interval=interval)
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "JimBot/1.0"})
        r.raise_for_status()
    except Exception as e:
        logger.warning(f"[kraken_candles] fetch {kraken_sym}/{interval}: {e}")
        return None

    try:
        payload = r.json()
    except Exception as e:
        logger.warning(f"[kraken_candles] JSON parse {kraken_sym}: {e}")
        return None

    candles = payload.get("candles") or payload.get("series") or []
    if not candles:
        return None

    # Kraken returns oldest → newest; the LAST candle may be the currently-forming
    # bar (close time = bar start + interval ; published progressively). To stay
    # consistent with the prior yfinance get_bars() which dropped the trailing
    # forming bar (`df.iloc[:-1]`), drop the last candle if its start time is
    # within the current interval. Otherwise keep it.
    now_ms = int(time.time() * 1000)
    interval_ms_map = {
        "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
        "1h": 3_600_000, "4h": 14_400_000, "12h": 43_200_000,
        "1d": 86_400_000, "1w": 604_800_000,
    }
    iv_ms = interval_ms_map.get(interval, 300_000)
    last_t = int(candles[-1].get("time", 0))
    if last_t and now_ms - last_t < iv_ms:
        # Last bar is still forming → drop, matches prior yfinance behaviour.
        candles = candles[:-1]
    if not candles:
        return None

    candles = candles[-limit:]

    try:
        rows = [{
            "ts":     pd.Timestamp(int(c["time"]), unit="ms", tz="UTC"),
            "open":   float(c.get("open",   c.get("o"))),
            "high":   float(c.get("high",   c.get("h"))),
            "low":    float(c.get("low",    c.get("l"))),
            "close":  float(c.get("close",  c.get("c"))),
            "volume": float(c.get("volume", c.get("v", 0))),
        } for c in candles]
    except Exception as e:
        logger.warning(f"[kraken_candles] parse fields {kraken_sym}: {e}")
        return None

    if not rows:
        return None
    df = pd.DataFrame(rows).set_index("ts")
    df["symbol"] = db_symbol
    return df[["open", "high", "low", "close", "volume", "symbol"]]
