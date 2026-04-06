"""
bybit_broker.py — Broker Bybit USDT Perpetual
Interface identique à okx_broker.py.
Supporte les bracket orders natifs (SL + TP dans place_order).
Testnet via BYBIT_TESTNET=1, live via BYBIT_TESTNET=0.
"""
import logging
import datetime
import pandas as pd
from pybit.unified_trading import HTTP
import config

logger = logging.getLogger(__name__)

# ── Symboles ───────────────────────────────────────────────────────────────────
_DB_TO_BYBIT = {
    "ETH/USD": "ETHUSDT",
    "SOL/USD": "SOLUSDT",
}
_BYBIT_TO_DB = {v: k for k, v in _DB_TO_BYBIT.items()}

# ── Timeframes ────────────────────────────────────────────────────────────────
_TF_MAP = {
    "1Min":  "1",
    "5Min":  "5",
    "15Min": "15",
    "30Min": "30",
    "1Hour": "60",
    "4Hour": "240",
    "1Day":  "D",
}

# ── Taille minimale de lot par symbole ────────────────────────────────────────
_MIN_QTY = {
    "ETHUSDT": 0.01,
    "SOLUSDT": 1.0,
}
_QTY_STEP = {
    "ETHUSDT": 0.01,
    "SOLUSDT": 1.0,
}


def _round_qty(symbol: str, qty: float) -> float:
    step = _QTY_STEP.get(symbol, 0.01)
    rounded = max(step, round(round(qty / step) * step, 8))
    return rounded


def _smart_round(price: float) -> float:
    if price >= 1000: return round(price, 1)
    if price >= 100:  return round(price, 2)
    if price >= 1:    return round(price, 4)
    return round(price, 6)


class Position:
    def __init__(self, bybit_symbol, qty, avg_px, mark_px, upl):
        self.bybit_symbol    = bybit_symbol
        self.db_symbol       = _BYBIT_TO_DB.get(bybit_symbol, bybit_symbol)
        self.qty             = qty
        self.avg_entry_price = avg_px
        self.current_price   = mark_px or avg_px
        self.unrealized_pnl  = upl

    @property
    def symbol(self):
        return self.bybit_symbol


class OrderInfo:
    def __init__(self, ord_id, bybit_symbol, side, state, fill_price,
                 filled_qty, limit_price=0.0):
        self.id               = ord_id
        self.bybit_symbol     = bybit_symbol
        self.symbol           = bybit_symbol
        self.db_symbol        = _BYBIT_TO_DB.get(bybit_symbol, bybit_symbol)
        self.side             = side.lower()
        self.status           = state
        self.filled_avg_price = fill_price
        self.filled_qty       = filled_qty
        self.limit_price      = limit_price
        self.time_in_force    = "gtc"
        self.type             = "limit"


class BybitBroker:
    def __init__(self):
        self.testnet = config.BYBIT_TESTNET
        self.session = HTTP(
            testnet    = self.testnet,
            api_key    = config.BYBIT_API_KEY,
            api_secret = config.BYBIT_SECRET_KEY,
        )
        self._init_leverage()
        mode = "TESTNET" if self.testnet else "LIVE"
        logger.info(f"✅ Bybit broker connecté ({mode}, leverage={config.BYBIT_LEVERAGE}x)")

    # ── Init ─────────────────────────────────────────────────────────────────

    def _init_leverage(self):
        for sym in _DB_TO_BYBIT.values():
            try:
                self.session.set_leverage(
                    category     = "linear",
                    symbol       = sym,
                    buyLeverage  = str(config.BYBIT_LEVERAGE),
                    sellLeverage = str(config.BYBIT_LEVERAGE),
                )
                logger.debug(f"[Bybit] leverage {sym} = {config.BYBIT_LEVERAGE}x ✓")
            except Exception as e:
                logger.warning(f"[Bybit] set_leverage {sym}: {e}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _to_bybit(self, symbol: str) -> str:
        return _DB_TO_BYBIT.get(symbol, symbol)

    # ── Account ───────────────────────────────────────────────────────────────

    def get_equity(self) -> float:
        try:
            r = self.session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
            if r.get("retCode") == 0:
                accts = r["result"].get("list", [])
                if accts:
                    for coin in accts[0].get("coin", []):
                        if coin.get("coin") == "USDT":
                            return float(coin.get("equity", 0) or 0)
        except Exception as e:
            logger.warning(f"[Bybit] get_equity: {e}")
        return config.GEO_CAPITAL

    def get_available(self) -> float:
        try:
            r = self.session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
            if r.get("retCode") == 0:
                accts = r["result"].get("list", [])
                if accts:
                    for coin in accts[0].get("coin", []):
                        if coin.get("coin") == "USDT":
                            return float(coin.get("availableToWithdraw", 0) or 0)
        except Exception as e:
            logger.warning(f"[Bybit] get_available: {e}")
        return 0.0

    def get_portfolio_value(self) -> float:
        return self.get_equity()

    def get_account(self):
        class _A:
            pass
        a = _A()
        a.equity       = self.get_equity()
        a.buying_power = self.get_available()
        return a

    # ── Positions ─────────────────────────────────────────────────────────────

    def get_positions(self) -> list:
        try:
            r = self.session.get_positions(category="linear", settleCoin="USDT")
            if r.get("retCode") != 0:
                logger.warning(f"[Bybit] get_positions: {r.get('retMsg')}")
                return []
            out = []
            for p in r["result"].get("list", []):
                sz = float(p.get("size", 0) or 0)
                if sz <= 0:
                    continue
                side = p.get("side", "")
                if side != "Buy":
                    continue
                sym    = p["symbol"]
                avg_px = float(p.get("avgPrice", 0) or 0)
                mk_px  = float(p.get("markPrice", avg_px) or avg_px)
                upl    = float(p.get("unrealisedPnl", 0) or 0)
                out.append(Position(sym, sz, avg_px, mk_px, upl))
            return out
        except Exception as e:
            logger.error(f"[Bybit] get_positions: {e}")
            return []

    def get_position(self, symbol: str):
        sym = self._to_bybit(symbol)
        for pos in self.get_positions():
            if pos.bybit_symbol == sym:
                return pos
        return None

    # ── Prix live ─────────────────────────────────────────────────────────────

    def get_live_price(self, symbol: str):
        sym = self._to_bybit(symbol)
        try:
            r = self.session.get_orderbook(category="linear", symbol=sym, limit=1)
            if r.get("retCode") == 0:
                d   = r["result"]
                ask = float(d["a"][0][0]) if d.get("a") else 0.0
                bid = float(d["b"][0][0]) if d.get("b") else 0.0
                if ask and bid:
                    return (ask + bid) / 2
        except Exception as e:
            logger.warning(f"[Bybit] get_live_price {symbol}: {e}")
        return None

    def get_ask_bid(self, symbol: str):
        sym = self._to_bybit(symbol)
        try:
            r = self.session.get_orderbook(category="linear", symbol=sym, limit=1)
            if r.get("retCode") == 0:
                d   = r["result"]
                ask = float(d["a"][0][0]) if d.get("a") else 0.0
                bid = float(d["b"][0][0]) if d.get("b") else 0.0
                return ask, bid
        except Exception as e:
            logger.warning(f"[Bybit] get_ask_bid {symbol}: {e}")
        return 0.0, 0.0

    # ── Bars (OHLCV) ─────────────────────────────────────────────────────────

    def get_bars(self, symbol: str, timeframe: str = "15Min", limit: int = 100):
        sym = self._to_bybit(symbol)
        interval = _TF_MAP.get(timeframe, "15")
        try:
            r = self.session.get_kline(
                category = "linear",
                symbol   = sym,
                interval = interval,
                limit    = limit,
            )
            if r.get("retCode") != 0:
                logger.error(f"[Bybit] get_bars {symbol}: {r.get('retMsg')}")
                return None
            data = r["result"].get("list", [])
            if not data:
                return None
            data = list(reversed(data))   # Bybit = newest-first → oldest-first
            rows = []
            for c in data:
                rows.append({
                    "timestamp": pd.to_datetime(int(c[0]), unit="ms", utc=True),
                    "open":      float(c[1]),
                    "high":      float(c[2]),
                    "low":       float(c[3]),
                    "close":     float(c[4]),
                    "volume":    float(c[5]),
                    "symbol":    symbol,
                })
            df = pd.DataFrame(rows).set_index("timestamp")
            if len(df) > 1:
                df = df.iloc[:-1]   # drop candle en cours
            return df
        except Exception as e:
            logger.error(f"[Bybit] get_bars {symbol}: {e}")
            return None

    # ── Orders ────────────────────────────────────────────────────────────────

    def place_limit_buy(self, symbol: str, price: float,
                        stop_loss: float, take_profit: float,
                        deploy_usdt: float):
        """Place un ordre limit buy avec SL + TP natifs Bybit.
        Les ordres SL et TP restent sur le matching engine Bybit — pas de bot-watcher.
        Retourne l'orderId ou None."""
        sym = self._to_bybit(symbol)
        qty = _round_qty(sym, deploy_usdt / price)
        if qty < _MIN_QTY.get(sym, 0.01):
            logger.warning(f"[Bybit] place_limit_buy {symbol}: qty {qty} < min")
            return None

        px = str(_smart_round(price))
        sl = str(_smart_round(stop_loss))
        tp = str(_smart_round(take_profit))

        try:
            r = self.session.place_order(
                category     = "linear",
                symbol       = sym,
                side         = "Buy",
                orderType    = "Limit",
                qty          = str(qty),
                price        = px,
                timeInForce  = "GTC",
                takeProfit   = tp,
                stopLoss     = sl,
                tpTriggerBy  = "LastPrice",
                slTriggerBy  = "LastPrice",
                tpOrderType  = "Market",
                slOrderType  = "Market",
            )
            if r.get("retCode") == 0:
                ord_id = r["result"]["orderId"]
                logger.info(
                    f"[Bybit] 📋 LIMIT PLACED: {symbol} @ ${price} "
                    f"qty={qty} SL=${stop_loss} TP=${take_profit} id={ord_id}"
                )
                return ord_id
            else:
                logger.error(f"[Bybit] place_limit_buy {symbol}: {r.get('retMsg')}")
                return None
        except Exception as e:
            logger.error(f"[Bybit] place_limit_buy {symbol}: {e}")
            return None

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        sym = self._to_bybit(symbol)
        try:
            r = self.session.cancel_order(
                category = "linear",
                symbol   = sym,
                orderId  = order_id,
            )
            if r.get("retCode") == 0:
                logger.info(f"[Bybit] 🛑 Cancelled {order_id}")
                return True
            logger.warning(f"[Bybit] cancel_order {order_id}: {r.get('retMsg')}")
            return False
        except Exception as e:
            logger.warning(f"[Bybit] cancel_order {order_id}: {e}")
            return False

    def get_order(self, symbol: str, order_id: str):
        sym = self._to_bybit(symbol)
        try:
            r = self.session.get_open_orders(
                category = "linear",
                symbol   = sym,
                orderId  = order_id,
            )
            if r.get("retCode") == 0:
                items = r["result"].get("list", [])
                if items:
                    d = items[0]
                    return OrderInfo(
                        ord_id      = order_id,
                        bybit_symbol= sym,
                        side        = d.get("side", "Buy"),
                        state       = d.get("orderStatus", "New"),
                        fill_price  = float(d.get("avgPrice", 0) or 0),
                        filled_qty  = float(d.get("cumExecQty", 0) or 0),
                        limit_price = float(d.get("price", 0) or 0),
                    )
            # Si pas dans open orders → chercher dans l'historique
            r2 = self.session.get_order_history(
                category = "linear",
                symbol   = sym,
                orderId  = order_id,
                limit    = 1,
            )
            if r2.get("retCode") == 0:
                items = r2["result"].get("list", [])
                if items:
                    d = items[0]
                    return OrderInfo(
                        ord_id      = order_id,
                        bybit_symbol= sym,
                        side        = d.get("side", "Buy"),
                        state       = d.get("orderStatus", "Filled"),
                        fill_price  = float(d.get("avgPrice", 0) or 0),
                        filled_qty  = float(d.get("cumExecQty", 0) or 0),
                        limit_price = float(d.get("price", 0) or 0),
                    )
        except Exception as e:
            logger.warning(f"[Bybit] get_order {order_id}: {e}")
        return None

    def list_open_orders(self, symbol: str = None) -> list:
        try:
            params = {"category": "linear", "settleCoin": "USDT"}
            if symbol:
                params["symbol"] = self._to_bybit(symbol)
            r = self.session.get_open_orders(**params)
            if r.get("retCode") != 0:
                return []
            orders = []
            for d in r["result"].get("list", []):
                sym = d["symbol"]
                orders.append(OrderInfo(
                    ord_id      = d["orderId"],
                    bybit_symbol= sym,
                    side        = d.get("side", "Buy"),
                    state       = d.get("orderStatus", "New"),
                    fill_price  = float(d.get("avgPrice", 0) or 0),
                    filled_qty  = float(d.get("cumExecQty", 0) or 0),
                    limit_price = float(d.get("price", 0) or 0),
                ))
            return orders
        except Exception as e:
            logger.warning(f"[Bybit] list_open_orders: {e}")
            return []

    # ── Fermeture position ────────────────────────────────────────────────────

    def close_position(self, symbol: str) -> bool:
        sym = self._to_bybit(symbol)
        pos = self.get_position(symbol)
        if not pos or pos.qty <= 0:
            logger.info(f"[Bybit] close_position {symbol}: pas de position")
            return True
        try:
            r = self.session.place_order(
                category    = "linear",
                symbol      = sym,
                side        = "Sell",
                orderType   = "Market",
                qty         = str(pos.qty),
                timeInForce = "IOC",
                reduceOnly  = True,
            )
            if r.get("retCode") == 0:
                logger.info(f"✅ Bybit position closed: {symbol}")
                return True
            logger.error(f"[Bybit] close_position {symbol}: {r.get('retMsg')}")
            return False
        except Exception as e:
            logger.error(f"[Bybit] close_position {symbol}: {e}")
            return False

    def close_all_positions(self) -> bool:
        ok = True
        for pos in self.get_positions():
            if not self.close_position(pos.db_symbol):
                ok = False
        return ok

    # ── Détection fermeture SL/TP ─────────────────────────────────────────────

    def get_close_info(self, symbol: str, since_ts_ms: int = 0) -> dict | None:
        """Lit l'historique des exécutions Bybit pour détecter si
        la position a été fermée par TP ou SL."""
        sym = self._to_bybit(symbol)

        # ── Méthode 1 : historique des ordres (closed orders) ────────────────
        try:
            r = self.session.get_order_history(
                category  = "linear",
                symbol    = sym,
                orderFilter = "StopOrder",
                limit     = 20,
            )
            if r.get("retCode") == 0:
                for d in r["result"].get("list", []):
                    if d.get("orderStatus") != "Filled":
                        continue
                    ts_ms = int(d.get("updatedTime", 0))
                    if since_ts_ms and ts_ms < since_ts_ms:
                        continue
                    if d.get("side", "").lower() != "sell":
                        continue
                    stop_type = d.get("stopOrderType", "")
                    fill_px   = float(d.get("avgPrice", 0) or 0)
                    if fill_px <= 0:
                        continue
                    reason = "target" if stop_type == "TakeProfit" else "stop"
                    return {
                        "price":  fill_px,
                        "qty":    float(d.get("cumExecQty", 0) or 0),
                        "reason": reason,
                        "source": "stop_order",
                    }
        except Exception as e:
            logger.debug(f"[Bybit] get_close_info stop_orders {symbol}: {e}")

        # ── Méthode 2 : historique des trades exécutés ───────────────────────
        try:
            r = self.session.get_executions(
                category = "linear",
                symbol   = sym,
                limit    = 20,
            )
            if r.get("retCode") == 0:
                for d in r["result"].get("list", []):
                    ts_ms = int(d.get("execTime", 0))
                    if since_ts_ms and ts_ms < since_ts_ms:
                        continue
                    if d.get("side", "").lower() != "sell":
                        continue
                    fill_px = float(d.get("execPrice", 0) or 0)
                    if fill_px <= 0:
                        continue
                    exec_type = d.get("execType", "")
                    reason = None
                    if exec_type == "Trade":
                        reason = None   # expert détermine via SL/TP levels
                    return {
                        "price":  fill_px,
                        "qty":    float(d.get("execQty", 0) or 0),
                        "reason": reason,
                        "source": "execution",
                    }
        except Exception as e:
            logger.warning(f"[Bybit] get_close_info executions {symbol}: {e}")

        return None

    def get_last_fill(self, symbol: str, since_ts_ms: int = 0) -> dict | None:
        return self.get_close_info(symbol, since_ts_ms)
