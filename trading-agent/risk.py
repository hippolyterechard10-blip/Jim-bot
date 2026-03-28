import logging
import config

logger = logging.getLogger(__name__)

class RiskManager:
    def __init__(self, broker):
        self.broker = broker

    def get_position_size(self, symbol, price):
        portfolio = self.broker.get_portfolio_value()
        max_amount = portfolio * config.MAX_POSITION_PCT
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

    def calculate_take_profit(self, entry_price, side):
        if side == "buy":
            return round(entry_price * (1 + config.TRADE_TAKE_PROFIT_PCT), 4)
        else:
            return round(entry_price * (1 - config.TRADE_TAKE_PROFIT_PCT), 4)

    def can_trade(self):
        if self.check_global_stop_loss():
            return False
        if not self.check_max_positions():
            return False
        return True
