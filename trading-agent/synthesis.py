"""
synthesis.py — Master Scoring Engine
Combines ALL intelligence layers into one final conviction score.
This runs BEFORE every Claude API call.
Only calls Claude when final score crosses the threshold.
"""
import logging
from news_intelligence import NewsIntelligence

logger = logging.getLogger(__name__)

# Score thresholds
LONG_THRESHOLD      = 60
SHORT_THRESHOLD     = 30
CONFIDENCE_MINIMUM  = 0.70


class SynthesisEngine:

    def __init__(self, regime, correlations, geometry, scanner):
        self.regime       = regime
        self.correlations = correlations
        self.geometry     = geometry
        self.scanner      = scanner   # kept for non-news uses (movers cache, etc.)
        self.news         = NewsIntelligence()
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
        # refresh_prices uses 5-min cache; fetch all correlated peers so relative strength works
        all_symbols  = list(self.correlations.CORRELATION_MATRIX.keys())
        flat_symbols = list(set([s for pair in all_symbols for s in pair] + [symbol]))
        changes      = self.correlations.refresh_prices(flat_symbols)
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

        # ── Layer 4: News Intelligence (Tier 1-4 classification) ─────────────────
        # Replaces simple scanner keyword detection with full tiered analysis:
        # Tier 1 = market-moving (±25), Tier 2 = directional (±12-15),
        # Tier 3 = contextual (±6), + Trump direction signal + earnings whisper
        news_result  = self.news.analyze(symbol)
        news_adj     = max(-35, min(35, news_result["total_score_adjustment"]))
        news_context = news_result["context"]

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

        synthesis_lines = [
            "=== SYNTHESIS SCORE ===",
            score_breakdown,
            f"Decision: {decision_reason}",
        ]

        full_context = "\n\n".join([
            regime_context,
            corr_context,
            geo_context,
            news_context,          # full Tier 1-4 classification from NewsIntelligence
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
