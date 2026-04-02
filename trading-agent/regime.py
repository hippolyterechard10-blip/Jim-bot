"""
regime.py — Détection régime simplifiée
Uniquement utilisé comme gate BEAR/PANIC dans geo.
Bull / Bear / Choppy / Panic — cache 30 min.
"""
import logging
from datetime import datetime, timezone
from typing import Optional
import requests

logger = logging.getLogger(__name__)


class MarketRegime:

    def __init__(self):
        self._cache = {
            "regime":    "bull",
            "vix":       None,
            "cached_at": None,
        }
        logger.info("✅ MarketRegime initialized")

    def _fetch_vix(self) -> Optional[float]:
        try:
            url  = "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?interval=1d&range=2d"
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
            data = resp.json()
            closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            vix = [c for c in closes if c is not None][-1]
            logger.info(f"📊 VIX: {vix:.2f}")
            return round(vix, 2)
        except Exception as e:
            logger.warning(f"VIX fetch failed: {e}")
            return None

    def _fetch_sp500(self) -> Optional[str]:
        try:
            url  = "https://query1.finance.yahoo.com/v8/finance/chart/SPY?interval=1d&range=220d"
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
            data = resp.json()
            closes = [c for c in data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                      if c is not None]
            if len(closes) < 200:
                return "unknown"
            ma200 = sum(closes[-200:]) / 200
            pct   = (closes[-1] - ma200) / ma200 * 100
            if pct > 2:  return "above_ma200"
            if pct < -2: return "below_ma200"
            return "at_ma200"
        except Exception as e:
            logger.warning(f"SP500 fetch failed: {e}")
            return None

    def detect_regime(self, force_refresh: bool = False) -> str:
        now = datetime.now(timezone.utc)
        if not force_refresh and self._cache["cached_at"]:
            age = (now - self._cache["cached_at"]).total_seconds()
            if age < 1800:  # cache 30 min
                return self._cache["regime"]

        vix   = self._fetch_vix()
        sp500 = self._fetch_sp500()

        if vix and vix > 35:
            regime = "panic"
        elif (vix and vix > 25) and sp500 == "below_ma200":
            regime = "bear"
        elif (vix and 18 < vix <= 25) or sp500 == "at_ma200":
            regime = "choppy"
        else:
            regime = "bull"

        self._cache.update({
            "regime":    regime,
            "vix":       vix,
            "cached_at": now,
        })
        logger.info(f"🎯 Régime: {regime.upper()} | VIX={vix}")
        return regime

    def get_cache(self) -> dict:
        return self._cache
