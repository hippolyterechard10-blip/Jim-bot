import logging
import config
from regime import MarketRegime

logger = logging.getLogger(__name__)

_regime_detector = MarketRegime()

# Score tiers: (min_score_inclusive, position_pct, trailing_stop_pct)
# Evaluated top-down; first match wins.
SCORE_TIERS = [
    (90, 0.40, 0.02),
    (80, 0.30, 0.03),
    (70, 0.20, 0.04),
    (60, 0.15, 0.05),
]

MAX_POSITION_PCT    = 0.40   # absolute hard cap — no single long > 40%
SHORT_POSITION_PCT  = 0.15   # all short entries fixed at 15%
LOW_VOLUME_CAP_PCT  = 0.10   # daily volume < LOW_VOLUME_THRESHOLD → cap at 10%
LOW_VOLUME_THRESHOLD = 100_000


class RiskManager:
    def __init__(self, broker):
        self.broker = broker

    def get_position_size_by_score(self, symbol, price, opp_score, volume=None):
        """
        Returns (qty, pct, trail_pct) based on opportunity score tier.

        Tier mapping:
          score 90-100 → 40% position, 2% trailing stop
          score 80-89  → 30% position, 3% trailing stop
          score 70-79  → 20% position, 4% trailing stop
          score 60-69  → 15% position, 5% trailing stop

        Low-volume override: if daily volume < 100,000, cap at 10% regardless.
        Hard cap: never exceed 40%.
        """
        portfolio = self.broker.get_portfolio_value()

        # Default to lowest tier (60-69)
        pct, trail_pct = 0.15, 0.05
        for min_score, tier_pct, tier_trail in SCORE_TIERS:
            if opp_score >= min_score:
                pct, trail_pct = tier_pct, tier_trail
                break

        # Low-volume override
        is_low_volume = volume is not None and volume < LOW_VOLUME_THRESHOLD
        if is_low_volume:
            original_pct = pct
            pct = min(pct, LOW_VOLUME_CAP_PCT)
            logger.info(
                f"⚠️ {symbol} low volume ({volume:,}) — position capped at "
                f"{pct*100:.0f}% (was {original_pct*100:.0f}% for score {opp_score})"
            )

        # Hard cap
        pct = min(pct, MAX_POSITION_PCT)

        # Regime multiplier — scales down size in bear/volatile markets
        regime_params = _regime_detector.get_params()
        multiplier = regime_params.get("position_size_multiplier", 1.0)
        pct = round(min(pct * multiplier, MAX_POSITION_PCT), 4)
        logger.info(f"[Risk] {regime_params['regime']} x{multiplier} → {pct*100:.1f}%")

        amount = portfolio * pct
        qty = amount / price
        return round(qty, 4), pct, trail_pct

    def get_short_position_size(self, symbol, price):
        """All short positions are fixed at 15% of portfolio."""
        portfolio = self.broker.get_portfolio_value()
        amount = portfolio * SHORT_POSITION_PCT
        qty = amount / price
        return round(qty, 4), SHORT_POSITION_PCT

    def check_global_stop_loss(self):
        portfolio = self.broker.get_portfolio_value()
        loss_pct = (config.INITIAL_CAPITAL - portfolio) / config.INITIAL_CAPITAL
        if loss_pct >= config.GLOBAL_STOP_LOSS_PCT:
            logger.warning(f"🔴 GLOBAL STOP LOSS triggered: -{loss_pct*100:.1f}%")
            return True
        return False

    def check_max_positions(self):
        positions = self.broker.get_positions()
        if len(positions) >= config.MAX_POSITIONS:
            logger.warning(f"⚠️ Max positions reached: {len(positions)}")
            return False
        return True

    def calculate_stop_loss(self, entry_price, side):
        if side == "buy":
            return round(entry_price * (1 - config.TRADE_STOP_LOSS_PCT), 4)
        else:
            return round(entry_price * (1 + config.TRADE_STOP_LOSS_PCT), 4)

    def can_trade(self):
        if self.check_global_stop_loss():
            return False
        if not self.check_max_positions():
            return False
        return True
