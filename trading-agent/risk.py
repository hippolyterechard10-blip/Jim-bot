import logging
import config

logger = logging.getLogger(__name__)

class RiskManager:
    def __init__(self, broker):
        self.broker = broker

    LOW_VOLUME_THRESHOLD = 100_000   # daily volume below this → reduced cap
    LOW_VOLUME_MAX_PCT   = 0.10      # 10% max position for low-volume stocks

    def get_position_size(self, symbol, price, volume=None):
        portfolio = self.broker.get_portfolio_value()
        is_low_volume = volume is not None and volume < self.LOW_VOLUME_THRESHOLD
        pct = self.LOW_VOLUME_MAX_PCT if is_low_volume else config.MAX_POSITION_PCT
        if is_low_volume:
            logger.info(f"⚠️ {symbol} low volume ({volume:,}) — position capped at {pct*100:.0f}%")
        max_amount = portfolio * pct
        qty = max_amount / price
        return round(qty, 4)

    def get_short_position_size(self, symbol, price):
        portfolio = self.broker.get_portfolio_value()
        max_amount = portfolio * config.MAX_SHORT_SIZE_PCT
        qty = max_amount / price
        return round(qty, 4)

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
