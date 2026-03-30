"""
synthesis.py — Master Scoring Engine
Combines ALL intelligence layers into one final conviction score.
This runs BEFORE every Claude API call.
Only calls Claude when final score crosses the threshold.
"""
import logging

logger = logging.getLogger(__name__)

# Score thresholds
LONG_THRESHOLD      = 60
SHORT_THRESHOLD     = 30
CONFIDENCE_MINIMUM  = 70


class SynthesisEngine:

    def __init__(self, regime, correlations, geometry, scanner):
        self.regime       = regime
        self.correlations = correlations
        self.geometry     = geometry
        self.scanner      = scanner
        logger.info("✅ SynthesisEngine initialized — all layers connected")

    def run(
        self,
        symbol: str,
        base_score: float,
        opens: list,
        highs: list,
        lows: list,
        closes: list,
        volumes: list,
        open_positions: list,
        side: str = "long",
    ) -> dict:
        """
        Master pre-trade analysis.
        Returns final score, full context string, and stop/target levels.
        Only proceed to Claude if final score crosses threshold.
        """

        # ── Layer 1: Market Regime ─────────────────────────────────────────────
        regime_params   = self.regime.get_params()
        regime_name     = regime_params["regime"]
        regime_adj      = self.regime.get_score_adjustments()
        regime_context  = self.regime.build_regime_context()

        long_threshold   = regime_params["score_long_threshold"]
        short_threshold  = regime_params["score_short_threshold"]
        confidence_min   = regime_params["confidence_threshold"]
        size_multiplier  = regime_params["position_size_multiplier"]

        # ── Layer 2: Correlations ──────────────────────────────────────────────
        # refresh_prices uses 5-min cache; parent cycle already warmed it for all symbols
        changes      = self.correlations.refresh_prices([symbol])
        dxy_trend    = self.regime._cache.get("dxy") or "neutral"
        is_crypto    = "/" in symbol

        corr_conflict = self.correlations.check_correlation_conflict(symbol, open_positions)
        rel_strength  = self.correlations.detect_relative_strength(symbol, changes)
        dxy_impact    = (
            self.correlations.get_dxy_crypto_adjustment(dxy_trend)
            if is_crypto else {"adjustment": 0, "reason": "N/A"}
        )
        corr_context  = self.correlations.build_correlation_context(
            symbol, open_positions, changes, dxy_trend
        )

        # ── Layer 3: Geometry ──────────────────────────────────────────────────
        geo         = self.geometry.build_geometry_context(
            symbol, opens, highs, lows, closes, volumes, side
        )
        geo_context = geo["context"]

        # ── Layer 4: News Sentiment ────────────────────────────────────────────
        sentiment = self.scanner.analyze_sentiment()
        news_adj  = 0
        sent      = sentiment.get("sentiment", "neutral")

        if sent == "very_bullish":
            news_adj = +15 if side == "long" else -10
        elif sent == "bullish":
            news_adj = +8  if side == "long" else -5
        elif sent == "very_bearish":
            news_adj = -15 if side == "long" else +15
        elif sent == "bearish":
            news_adj = -8  if side == "long" else +8

        # High-urgency alerts push short bias
        alert_adj = len(sentiment.get("alerts", [])) * 5
        if side == "short":
            news_adj += alert_adj
        else:
            news_adj -= alert_adj

        # ── Final Score Calculation ────────────────────────────────────────────
        regime_score_adj = (
            regime_adj["long_bonus"] if side == "long" else regime_adj["short_penalty"]
        )

        final_score = (
            base_score
            + regime_score_adj
            + rel_strength["score_adjustment"]
            + (dxy_impact["adjustment"] if is_crypto else 0)
            + corr_conflict.get("score_adjustment", 0)
            + geo["score_adjustment"]
            + news_adj
        )
        final_score = max(0, min(100, final_score))

        # ── Decision ──────────────────────────────────────────────────────────
        if side == "long":
            should_call_claude = final_score >= long_threshold
            decision_reason    = (
                f"LONG score {final_score:.0f} "
                f"{'≥' if should_call_claude else '<'} threshold {long_threshold}"
            )
        else:
            should_call_claude = final_score <= short_threshold
            decision_reason    = (
                f"SHORT score {final_score:.0f} "
                f"{'≤' if should_call_claude else '>'} threshold {short_threshold}"
            )

        # ── Build Master Context for Claude ───────────────────────────────────
        score_breakdown = (
            f"Base: {base_score:.0f} | "
            f"Regime: {regime_score_adj:+.0f} | "
            f"RelStr: {rel_strength['score_adjustment']:+.0f} | "
            f"DXY: {dxy_impact['adjustment']:+.0f} | "
            f"Corr: {corr_conflict.get('score_adjustment', 0):+.0f} | "
            f"Geo: {geo['score_adjustment']:+.0f} | "
            f"News: {news_adj:+.0f} | "
            f"FINAL: {final_score:.0f}"
        )

        news_lines = [
            f"=== NEWS SENTIMENT ===",
            f"{sent.upper()} (score: {sentiment.get('score', 0)})",
        ] + sentiment.get("alerts", [])

        synthesis_lines = [
            "=== SYNTHESIS SCORE ===",
            score_breakdown,
            f"Decision: {decision_reason}",
        ]

        full_context = "\n\n".join([
            regime_context,
            corr_context,
            geo_context,
            "\n".join(news_lines),
            "\n".join(synthesis_lines),
        ])

        logger.info(
            f"🧠 SYNTHESIS {symbol} {side.upper()}: {score_breakdown} | "
            f"{'→ CALLING CLAUDE' if should_call_claude else '→ SKIPPED'}"
        )

        return {
            "final_score":       final_score,
            "should_call_claude": should_call_claude,
            "decision_reason":   decision_reason,
            "score_breakdown":   score_breakdown,
            "full_context":      full_context,
            "stop_loss":         geo.get("stop_loss"),
            "take_profit":       geo.get("take_profit"),
            "stop_pct":          geo.get("stop_pct"),
            "target_pct":        geo.get("target_pct"),
            "risk_reward":       geo.get("risk_reward"),
            "size_multiplier":   size_multiplier,
            "confidence_minimum": confidence_min,
            "regime":            regime_name,
            "patterns":          geo.get("patterns_detected", []),
        }
