import logging
import alpaca_trade_api as tradeapi
import config

logger = logging.getLogger(__name__)

class AlpacaBroker:
    def __init__(self):
        self.api = tradeapi.REST(
            config.ALPACA_API_KEY,
            config.ALPACA_SECRET_KEY,
            config.ALPACA_BASE_URL
        )
        logger.info("✅ Alpaca broker connected")

    def get_account(self):
        try:
            return self.api.get_account()
        except Exception as e:
            logger.error(f"get_account error: {e}")
            return None

    def get_portfolio_value(self):
        account = self.get_account()
        if account:
            return float(account.portfolio_value)
        return config.INITIAL_CAPITAL

    def get_positions(self):
        try:
            return self.api.list_positions()
        except Exception as e:
            logger.error(f"get_positions error: {e}")
            return []

    def get_bars(self, symbol, timeframe="1Min", limit=50):
        try:
            is_crypto = "/" in symbol
            if is_crypto:
                bars = self.api.get_crypto_bars(symbol, timeframe, limit=limit).df
            else:
                bars = self.api.get_bars(symbol, timeframe, limit=limit).df
            return bars
        except Exception as e:
            logger.error(f"get_bars error for {symbol}: {e}")
            return None

    def place_order(self, symbol, qty, side, stop_loss=None, take_profit=None):
        try:
            order = self.api.submit_order(
                symbol=symbol,
                qty=qty,
                side=side,
                type="market",
                time_in_force="gtc"
            )
            logger.info(f"✅ Order placed: {side} {qty} {symbol}")
            return order
        except Exception as e:
            logger.error(f"place_order error: {e}")
            return None

    def close_position(self, symbol):
        try:
            self.api.close_position(symbol)
            logger.info(f"✅ Position closed: {symbol}")
            return True
        except Exception as e:
            logger.error(f"close_position error: {e}")
            return False

    def close_all_positions(self):
        try:
            self.api.close_all_positions()
            logger.info("✅ All positions closed")
            return True
        except Exception as e:
            logger.error(f"close_all_positions error: {e}")
            return False
