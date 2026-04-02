"""
regime.py — Market Regime Detection
Detects current market regime and adjusts ALL trading parameters accordingly.
Bull / Bear / Choppy / Panic — everything changes based on regime.
"""
import logging
from datetime import datetime, timezone
from typing import Optional
import requests

logger = logging.getLogger(__name__)

# ── Regime definitions ────────────────────────────────────────────────────────

REGIMES = {
    "bull":   "Bull Market — aggressive longs, full sizes, let winners run",
    "bear":   "Bear Market — short bias, small longs, large shorts on rallies",
    "choppy": "Choppy — mean reversion only, take profits fast, no overnight holds",
    "panic":  "Panic — no new longs, wait for capitulation, one mean reversion only",
}

# ── Parameter adjustments per regime ─────────────────────────────────────────

REGIME_PARAMS = {
    "bull": {
        "confidence_threshold": 65,
        "position_size_multiplier": 1.0,
        "score_long_threshold": 60,
        "score_short_threshold": 25,
        "hold_overnight": True,
        "trailing_stop_multiplier": 1.0,
        "max_positions": 5,
    },
    "bear": {
        "confidence_threshold": 72,
        "position_size_multiplier": 0.6,
        "score_long_threshold": 72,
        "score_short_threshold": 35,
        "hold_overnight": False,
        "trailing_stop_multiplier": 0.7,
        "max_positions": 3,
    },
    "choppy": {
        "confidence_threshold": 75,
        "position_size_multiplier": 0.5,
        "score_long_threshold": 75,
        "score_short_threshold": 30,
        "hold_overnight": False,
        "trailing_stop_multiplier": 0.6,
        "max_positions": 2,
    },
    "panic": {
        "confidence_threshold": 85,
        "position_size_multiplier": 0.3,
        "score_long_threshold": 88,
        "score_short_threshold": 20,
        "hold_overnight": False,
        "trailing_stop_multiplier": 0.5,
        "max_positions": 1,
    },
}

# Score adjustments injected into Claude prompt per regime
REGIME_SCORE_ADJUSTMENTS = {
    "bull":   {"long_bonus": +10, "short_penalty": -10},
    "bear":   {"long_bonus": -10, "short_penalty": +10},
    "choppy": {"long_bonus": -5,  "short_penalty": +5},
    "panic":  {"long_bonus": -20, "short_penalty": +15},
}


class MarketRegime:
    """
    Detects current market regime using VIX, DXY, and S&P 500 trend.
    Caches results for 30 minutes to avoid excessive API calls.
    """

    def __init__(self):
        self._cache = {
            "regime": "bull",
            "vix": None,
            "dxy": None,
            "sp500_trend": None,
            "cached_at": None,
        }
        logger.info("✅ MarketRegime initialized")

    # ── Data fetching ─────────────────────────────────────────────────────────

    def _fetch_vix(self) -> Optional[float]:
        """Fetch current VIX from Yahoo Finance."""
        try:
            url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?interval=1d&range=2d"
            headers = {"User-Agent": "Mozilla/5.0"}
            resp = requests.get(url, headers=headers, timeout=5)
            data = resp.json()
            closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            vix = [c for c in closes if c is not None][-1]
            logger.info(f"📊 VIX: {vix:.2f}")
            return round(vix, 2)
        except Exception as e:
            logger.warning(f"VIX fetch failed: {e}")
            return None

    def _fetch_dxy_trend(self) -> Optional[str]:
        """Fetch DXY (US Dollar Index) trend — rising or falling."""
        try:
            url = "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?interval=1d&range=5d"
            headers = {"User-Agent": "Mozilla/5.0"}
            resp = requests.get(url, headers=headers, timeout=5)
            data = resp.json()
            closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            closes = [c for c in closes if c is not None]
            if len(closes) < 2:
                return "neutral"
            change = (closes[-1] - closes[-3]) / closes[-3] * 100
            if change > 0.3:
                trend = "rising"
            elif change < -0.3:
                trend = "falling"
            else:
                trend = "neutral"
            logger.info(f"📊 DXY trend: {trend} ({change:+.2f}% over 3 days)")
            return trend
        except Exception as e:
            logger.warning(f"DXY fetch failed: {e}")
            return None

    def _fetch_sp500_trend(self) -> Optional[str]:
        """Check if S&P 500 is above or below its 200-day moving average."""
        try:
            url = "https://query1.finance.yahoo.com/v8/finance/chart/SPY?interval=1d&range=220d"
            headers = {"User-Agent": "Mozilla/5.0"}
            resp = requests.get(url, headers=headers, timeout=5)
            data = resp.json()
            closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            closes = [c for c in closes if c is not None]
            if len(closes) < 200:
                return "unknown"
            ma200 = sum(closes[-200:]) / 200
            current = closes[-1]
            pct_from_ma = (current - ma200) / ma200 * 100
            if pct_from_ma > 2:
                trend = "above_ma200"
            elif pct_from_ma < -2:
                trend = "below_ma200"
            else:
                trend = "at_ma200"
            logger.info(f"📊 SPY vs MA200: {trend} ({pct_from_ma:+.1f}%)")
            return trend
        except Exception as e:
            logger.warning(f"SP500 trend fetch failed: {e}")
            return None

    # ── Regime detection ──────────────────────────────────────────────────────

    def detect_regime(self, force_refresh: bool = False) -> str:
        """
        Detect current market regime.
        Caches for 30 minutes. Returns: 'bull' | 'bear' | 'choppy' | 'panic'
        """
        now = datetime.now(timezone.utc)

        # Use cache if fresh (30 min)
        if not force_refresh and self._cache["cached_at"]:
            age = (now - self._cache["cached_at"]).total_seconds()
            if age < 1800:
                return self._cache["regime"]

        vix = self._fetch_vix()
        dxy = self._fetch_dxy_trend()
        sp500 = self._fetch_sp500_trend()

        regime = self._classify_regime(vix, dxy, sp500)

        self._cache.update({
            "regime": regime,
            "vix": vix,
            "dxy": dxy,
            "sp500_trend": sp500,
            "cached_at": now,
        })

        logger.info(f"🎯 Market regime: {regime.upper()} | VIX={vix} | DXY={dxy} | SP500={sp500}")
        return regime

    def _classify_regime(self, vix, dxy, sp500) -> str:
        """Core regime classification logic."""
        # Panic: VIX > 35 — extreme fear
        if vix and vix > 35:
            return "panic"

        # Bear: VIX elevated + S&P below 200MA + DXY rising
        bear_signals = 0
        if vix and vix > 25:
            bear_signals += 2
        if sp500 == "below_ma200":
            bear_signals += 2
        if dxy == "rising":
            bear_signals += 1

        if bear_signals >= 3:
            return "bear"

        # Choppy: Mixed signals
        choppy_signals = 0
        if vix and 18 < vix <= 25:
            choppy_signals += 1
        if sp500 == "at_ma200":
            choppy_signals += 1
        if dxy == "neutral":
            choppy_signals += 1

        if choppy_signals >= 2:
            return "choppy"

        # Bull: Low VIX + S&P above 200MA + DXY neutral or falling
        return "bull"

    # ── Parameter access ──────────────────────────────────────────────────────

    def get_params(self) -> dict:
        """Returns current regime parameters for use throughout the system."""
        regime = self.detect_regime()
        params = REGIME_PARAMS[regime].copy()
        params["regime"] = regime
        params["description"] = REGIMES[regime]
        return params

    def get_score_adjustments(self) -> dict:
        """Returns score bonuses/penalties for current regime."""
        regime = self.detect_regime()
        return REGIME_SCORE_ADJUSTMENTS[regime]

    def build_regime_context(self) -> str:
        """
        Builds a regime summary string for injection into Claude's prompt.
        """
        regime = self.detect_regime()
        params = REGIME_PARAMS[regime]
        vix = self._cache.get("vix")
        dxy = self._cache.get("dxy")
        sp500 = self._cache.get("sp500_trend")
        adj = REGIME_SCORE_ADJUSTMENTS[regime]

        lines = [
            "=== MARKET REGIME ===",
            f"Regime: {regime.upper()} — {REGIMES[regime]}",
            f"VIX: {vix if vix else 'N/A'} {'⚠️ ELEVATED' if vix and vix > 25 else ''}",
            f"DXY: {dxy if dxy else 'N/A'} {'⚠️ BEARISH FOR CRYPTO' if dxy == 'rising' else '✅ BULLISH FOR CRYPTO' if dxy == 'falling' else ''}",
            f"S&P 500 vs 200MA: {sp500 if sp500 else 'N/A'}",
            f"Score adjustments: LONG {adj['long_bonus']:+d} | SHORT {adj['short_penalty']:+d}",
            f"Max positions: {params['max_positions']} | Size multiplier: {params['position_size_multiplier']}x",
            f"Overnight holds: {'YES' if params['hold_overnight'] else 'NO — close before market close'}",
        ]

        if regime == "bear":
            lines.append("⚠️ BEAR MODE: Prefer shorts on rallies. Longs only on exceptional setups.")
        elif regime == "panic":
            lines.append("🔴 PANIC MODE: Extreme caution. Wait for capitulation. One trade max.")
        elif regime == "choppy":
            lines.append("⚡ CHOPPY MODE: Take profits fast. Mean reversion only. No trend trades.")
        else:
            lines.append("✅ BULL MODE: Let winners run. Full position sizes on quality setups.")

        return "\n".join(lines)
