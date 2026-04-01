import logging
import requests
import pandas as pd
import alpaca_trade_api as tradeapi
import config

logger = logging.getLogger(__name__)

_DATA_BASE = "https://data.alpaca.markets/v1beta3/crypto/us"

class AlpacaBroker:
    def __init__(self):
        self.api = tradeapi.REST(
            config.ALPACA_API_KEY,
            config.ALPACA_SECRET_KEY,
            config.ALPACA_BASE_URL
        )
        self._headers = {
            "APCA-API-KEY-ID":     config.ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
        }
        logger.info("✅ Alpaca broker connected")

    # ── Account / portfolio ───────────────────────────────────────────────────

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

    # ── Live price (true real-time via v1beta3 latest/quotes) ─────────────────

    def get_live_price(self, symbol: str) -> float | None:
        """
        Returns the current mid-price for a crypto symbol using
        Alpaca v1beta3 /latest/quotes (sub-second latency).
        Falls back to latest trade price, then daily bar close.
        """
        if "/" not in symbol:
            return None
        encoded = symbol.replace("/", "%2F")
        try:
            r = requests.get(
                f"{_DATA_BASE}/latest/quotes?symbols={encoded}",
                headers=self._headers, timeout=5
            )
            if r.ok:
                q = r.json()["quotes"].get(symbol)
                if q:
                    mid = (float(q["ap"]) + float(q["bp"])) / 2
                    logger.debug(f"[live] {symbol} quote mid=${mid:.4f} ask=${q['ap']} bid=${q['bp']}")
                    return mid
        except Exception as e:
            logger.warning(f"get_live_price quote error for {symbol}: {e}")
        try:
            r2 = requests.get(
                f"{_DATA_BASE}/latest/trades?symbols={encoded}",
                headers=self._headers, timeout=5
            )
            if r2.ok:
                t = r2.json()["trades"].get(symbol)
                if t:
                    price = float(t["p"])
                    logger.debug(f"[live] {symbol} latest trade=${price:.4f}")
                    return price
        except Exception as e2:
            logger.warning(f"get_live_price trade error for {symbol}: {e2}")
        return None

    # ── Bars (most recent N bars via v1beta3 with sort=desc) ──────────────────

    def get_bars(self, symbol: str, timeframe: str = "1Min", limit: int = 50) -> pd.DataFrame | None:
        """
        Returns the most recent `limit` bars for symbol.

        Crypto: uses Alpaca v1beta3 /bars?sort=desc so limit=50 always
                returns the LATEST 50 bars, not the first 50 of the day.
        Stocks: uses the legacy SDK call (unchanged behaviour).
        """
        is_crypto = "/" in symbol
        try:
            if is_crypto:
                # Build URL manually to avoid double-encoding of the "/" in LINK/USD
                qs = f"symbols={symbol}&timeframe={timeframe}&limit={limit}&sort=desc"
                r = requests.get(
                    f"{_DATA_BASE}/bars?{qs}",
                    headers=self._headers,
                    timeout=10,
                )
                if not r.ok:
                    logger.error(f"get_bars v1beta3 {symbol} {r.status_code}: {r.text[:200]}")
                    return None
                raw_bars = r.json().get("bars", {}).get(symbol, [])
                if not raw_bars:
                    return None
                df = pd.DataFrame(raw_bars)
                df["timestamp"] = pd.to_datetime(df["t"])
                df = df.sort_values("timestamp")   # ascending so oldest→newest
                df = df.rename(columns={
                    "o": "open", "h": "high", "l": "low",
                    "c": "close", "v": "volume", "vw": "vwap",
                })
                df = df.set_index("timestamp")
                df["symbol"] = symbol
                return df
            else:
                bars = self.api.get_bars(symbol, timeframe, limit=limit).df
                return bars
        except Exception as e:
            logger.error(f"get_bars error for {symbol}: {e}")
            return None

    # ── Orders / positions ────────────────────────────────────────────────────

    def place_order(self, symbol, qty, side, stop_loss=None, take_profit=None):
        try:
            is_crypto = "/" in symbol
            if not is_crypto:
                qty = max(1, int(qty))
            order_params = dict(
                symbol=symbol,
                qty=qty,
                side=side,
                type="market",
                time_in_force="gtc" if is_crypto else "day"
            )
            if stop_loss and take_profit and "/" not in symbol:
                order_params["order_class"] = "bracket"
                order_params["stop_loss"]   = {"stop_price": round(stop_loss, 2)}
                order_params["take_profit"] = {"limit_price": round(take_profit, 2)}
            order = self.api.submit_order(**order_params)
            logger.info(f"✅ Order: {side} {qty} {symbol} | stop={stop_loss} target={take_profit}")
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
