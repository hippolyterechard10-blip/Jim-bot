"""
okx_broker.py — Broker OKX v5 (Demo Trading / Mainnet)
Interface unifiée qui remplace AlpacaBroker.
Supporte les ordres bracket natifs (SL + TP via attachAlgoOrds).
"""
import logging
import datetime
import pandas as pd
import okx.Trade as Trade
import okx.Account as Account
import okx.MarketData as MarketData
import okx.PublicData as PublicData
import config

logger = logging.getLogger(__name__)

# ── Symboles ──────────────────────────────────────────────────────────────────
_DB_TO_OKX = {
    "ETH/USD": "ETH-USDT-SWAP",
    "SOL/USD": "SOL-USDT-SWAP",
}
_OKX_TO_DB = {v: k for k, v in _DB_TO_OKX.items()}

# ── Timeframes ─────────────────────────────────────────────────────────────────
_TF_MAP = {
    "1Min":  "1m",
    "5Min":  "5m",
    "15Min": "15m",
    "30Min": "30m",
    "1Hour": "1H",
    "4Hour": "4H",
    "1Day":  "1D",
}

# ── Contract sizes par défaut ──────────────────────────────────────────────────
_CT_VAL_DEFAULT = {
    "ETH-USDT-SWAP": 0.01,
    "SOL-USDT-SWAP": 1.0,
}


def _smart_round(price: float) -> float:
    if price >= 100:    return round(price, 2)
    elif price >= 1:    return round(price, 4)
    elif price >= 0.01: return round(price, 6)
    else:               return round(price, 8)


class Position:
    """Position OKX normalisée."""
    def __init__(self, inst_id, qty_contracts, ct_val, avg_px, mark_px, upl):
        self.okx_symbol      = inst_id
        self.db_symbol       = _OKX_TO_DB.get(inst_id, inst_id)
        self.qty_contracts   = qty_contracts
        self.qty             = qty_contracts * ct_val
        self.avg_entry_price = avg_px
        self.current_price   = mark_px or avg_px
        self.unrealized_pnl  = upl

    @property
    def symbol(self):
        return self.okx_symbol


class OrderInfo:
    """Ordre OKX normalisé."""
    def __init__(self, ord_id, inst_id, side, state, fill_price,
                 filled_qty_contracts, ct_val, limit_price=0.0):
        self.id               = ord_id
        self.okx_symbol       = inst_id
        self.symbol           = inst_id
        self.db_symbol        = _OKX_TO_DB.get(inst_id, inst_id)
        self.side             = side
        self.status           = state
        self.filled_avg_price = fill_price
        self.filled_qty       = filled_qty_contracts * ct_val
        self.qty_contracts    = filled_qty_contracts
        self.limit_price      = limit_price
        self.time_in_force    = "gtc"
        self.type             = "limit"


class OKXBroker:
    def __init__(self):
        flag = "1" if config.OKX_DEMO else "0"
        k = config.OKX_API_KEY
        s = config.OKX_SECRET_KEY
        p = config.OKX_PASSPHRASE

        self.trade  = Trade.TradeAPI(k, s, p, use_server_time=False, flag=flag)
        self.acct   = Account.AccountAPI(k, s, p, use_server_time=False, flag=flag)
        self.market = MarketData.MarketAPI(flag=flag)
        self.public = PublicData.PublicAPI(flag=flag)

        self._ct_val: dict = dict(_CT_VAL_DEFAULT)
        self._load_ct_val()
        self._init_leverage()

        logger.info(f"✅ OKX broker connected (demo={config.OKX_DEMO}, leverage={config.OKX_LEVERAGE}x)")

    # ── Init ──────────────────────────────────────────────────────────────────

    def _load_ct_val(self):
        for inst_id in _DB_TO_OKX.values():
            try:
                r = self.public.get_instruments(instType="SWAP", instId=inst_id)
                if r.get("code") == "0" and r.get("data"):
                    self._ct_val[inst_id] = float(r["data"][0]["ctVal"])
                    logger.debug(f"[OKX] ctVal {inst_id} = {self._ct_val[inst_id]}")
            except Exception as e:
                logger.warning(f"[OKX] ctVal {inst_id}: {e} — default used")

    def _init_leverage(self):
        for inst_id in _DB_TO_OKX.values():
            try:
                r = self.acct.set_leverage(
                    instId  = inst_id,
                    lever   = str(config.OKX_LEVERAGE),
                    mgnMode = "cross",
                )
                if r.get("code") == "0":
                    logger.debug(f"[OKX] leverage {inst_id} = {config.OKX_LEVERAGE}x ✓")
                else:
                    logger.warning(f"[OKX] set_leverage {inst_id}: {r.get('msg', '?')}")
            except Exception as e:
                logger.warning(f"[OKX] set_leverage {inst_id}: {e}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _to_okx(self, symbol: str) -> str:
        return _DB_TO_OKX.get(symbol, symbol)

    def _ct(self, inst_id: str) -> float:
        return self._ct_val.get(inst_id, 1.0)

    def _qty_to_contracts(self, inst_id: str, qty_underlying: float) -> int:
        ct = self._ct(inst_id)
        return max(1, round(qty_underlying / ct))

    # ── Account ───────────────────────────────────────────────────────────────

    def get_equity(self) -> float:
        try:
            r = self.acct.get_account_balance(ccy="USDT")
            if r.get("code") == "0" and r.get("data"):
                for detail in r["data"]:
                    for coin in detail.get("details", []):
                        if coin.get("ccy") == "USDT":
                            return float(coin.get("eq", 0) or 0)
                # fallback total
                return float(r["data"][0].get("totalEq", config.GEO_CAPITAL))
        except Exception as e:
            logger.warning(f"[OKX] get_equity: {e}")
        return config.GEO_CAPITAL

    def get_available(self) -> float:
        try:
            r = self.acct.get_account_balance(ccy="USDT")
            if r.get("code") == "0" and r.get("data"):
                for detail in r["data"]:
                    for coin in detail.get("details", []):
                        if coin.get("ccy") == "USDT":
                            v = coin.get("availEq") or coin.get("availBal") or 0
                            return float(v or 0)
                return float(r["data"][0].get("adjEq", 0))
        except Exception as e:
            logger.warning(f"[OKX] get_available: {e}")
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
            r = self.acct.get_positions(instType="SWAP")
            if r.get("code") != "0":
                logger.warning(f"[OKX] get_positions error: {r.get('msg')}")
                return []
            out = []
            for p in r.get("data", []):
                sz = float(p.get("pos", 0) or 0)
                if sz <= 0:
                    continue
                inst_id = p["instId"]
                ct      = self._ct(inst_id)
                avg_px  = float(p.get("avgPx", 0) or 0)
                mark_px = float(p.get("markPx", avg_px) or avg_px)
                upl     = float(p.get("upl", 0) or 0)
                out.append(Position(inst_id, sz, ct, avg_px, mark_px, upl))
            return out
        except Exception as e:
            logger.error(f"[OKX] get_positions: {e}")
            return []

    def get_position(self, symbol: str):
        inst_id = self._to_okx(symbol)
        for pos in self.get_positions():
            if pos.okx_symbol == inst_id:
                return pos
        return None

    # ── Prix live ─────────────────────────────────────────────────────────────

    def get_live_price(self, symbol: str):
        inst_id = self._to_okx(symbol)
        try:
            r = self.market.get_orderbook(instId=inst_id, sz="1")
            if r.get("code") == "0" and r.get("data"):
                d = r["data"][0]
                ask = float(d["asks"][0][0]) if d.get("asks") else 0.0
                bid = float(d["bids"][0][0]) if d.get("bids") else 0.0
                if ask and bid:
                    return (ask + bid) / 2
        except Exception as e:
            logger.warning(f"[OKX] get_live_price {symbol}: {e}")
        return None

    def get_ask_bid(self, symbol: str):
        inst_id = self._to_okx(symbol)
        try:
            r = self.market.get_orderbook(instId=inst_id, sz="1")
            if r.get("code") == "0" and r.get("data"):
                d   = r["data"][0]
                ask = float(d["asks"][0][0]) if d.get("asks") else 0.0
                bid = float(d["bids"][0][0]) if d.get("bids") else 0.0
                return ask, bid
        except Exception as e:
            logger.warning(f"[OKX] get_ask_bid {symbol}: {e}")
        return 0.0, 0.0

    # ── Bars (OHLCV) ─────────────────────────────────────────────────────────

    def get_bars(self, symbol: str, timeframe: str = "15Min", limit: int = 100):
        inst_id = self._to_okx(symbol)
        bar     = _TF_MAP.get(timeframe, timeframe)
        try:
            r = self.market.get_candlesticks(instId=inst_id, bar=bar, limit=str(limit))
            if r.get("code") != "0":
                logger.error(f"[OKX] get_bars {symbol}/{timeframe}: {r.get('msg')}")
                return None
            data = r.get("data", [])
            if not data:
                return None
            data = list(reversed(data))   # OKX = newest-first → oldest-first
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
            # Drop last candle (still forming)
            if len(df) > 1:
                df = df.iloc[:-1]
            return df
        except Exception as e:
            logger.error(f"[OKX] get_bars {symbol}/{timeframe}: {e}")
            return None

    # ── Orders ────────────────────────────────────────────────────────────────

    def place_limit_buy(self, symbol: str, price: float,
                        stop_loss: float, take_profit: float,
                        deploy_usdt: float):
        """Place un ordre limit buy avec SL + TP attachés (bracket OKX natif).
        Retourne l'orderId OKX ou None en cas d'échec."""
        inst_id  = self._to_okx(symbol)
        ct       = self._ct(inst_id)
        qty_und  = deploy_usdt / price
        sz       = self._qty_to_contracts(inst_id, qty_und)
        if sz < 1:
            logger.warning(f"[OKX] place_limit_buy {symbol}: sz<1 ({qty_und:.4f} / ct={ct})")
            return None

        px = str(_smart_round(price))
        sl = str(_smart_round(stop_loss))
        tp = str(_smart_round(take_profit))

        try:
            r = self.trade.place_order(
                instId  = inst_id,
                tdMode  = "cross",
                side    = "buy",
                ordType = "limit",
                sz      = str(sz),
                px      = px,
                attachAlgoOrds = [{
                    "tpTriggerPx": tp,
                    "tpOrdPx":     "-1",
                    "slTriggerPx": sl,
                    "slOrdPx":     "-1",
                }],
            )
            if r.get("code") == "0" and r.get("data"):
                item = r["data"][0]
                if item.get("sCode") != "0":
                    logger.error(f"[OKX] place_limit_buy {symbol}: {item.get('sMsg')}")
                    return None
                ord_id = item["ordId"]
                logger.info(
                    f"[OKX] 📋 LIMIT PLACED: {symbol} @ ${price} "
                    f"sz={sz}c ({qty_und:.4f} underlying) "
                    f"SL=${stop_loss} TP=${take_profit} ordId={ord_id}"
                )
                return ord_id
            else:
                msg = r["data"][0]["sMsg"] if r.get("data") else r.get("msg", "?")
                logger.error(f"[OKX] place_limit_buy {symbol}: {msg}")
                return None
        except Exception as e:
            logger.error(f"[OKX] place_limit_buy {symbol}: {e}")
            return None

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        inst_id = self._to_okx(symbol)
        try:
            r = self.trade.cancel_order(instId=inst_id, ordId=order_id)
            if r.get("code") == "0":
                logger.info(f"[OKX] 🛑 Cancelled {order_id}")
                return True
            logger.warning(f"[OKX] cancel_order {order_id}: {r.get('msg')}")
            return False
        except Exception as e:
            logger.warning(f"[OKX] cancel_order {order_id}: {e}")
            return False

    def get_order(self, symbol: str, order_id: str):
        inst_id = self._to_okx(symbol)
        ct      = self._ct(inst_id)
        try:
            r = self.trade.get_order(instId=inst_id, ordId=order_id)
            if r.get("code") != "0" or not r.get("data"):
                return None
            d         = r["data"][0]
            fill_sz   = float(d.get("fillSz", 0) or 0)
            fill_px   = float(d.get("fillPx", 0) or d.get("avgPx", 0) or 0)
            lim_px    = float(d.get("px", 0) or 0)
            return OrderInfo(
                ord_id               = order_id,
                inst_id              = inst_id,
                side                 = d.get("side", "buy"),
                state                = d.get("state", "live"),
                fill_price           = fill_px,
                filled_qty_contracts = fill_sz,
                ct_val               = ct,
                limit_price          = lim_px,
            )
        except Exception as e:
            logger.warning(f"[OKX] get_order {order_id}: {e}")
            return None

    def list_open_orders(self, symbol: str = None) -> list:
        """Ordres ouverts (state=live ou partially_filled) pour le symbole ou tous."""
        try:
            params = {"instType": "SWAP", "state": "live"}
            if symbol:
                params["instId"] = self._to_okx(symbol)
            r = self.trade.get_order_list(**params)
            if r.get("code") != "0":
                return []
            orders = []
            for d in r.get("data", []):
                iid = d["instId"]
                ct  = self._ct(iid)
                orders.append(OrderInfo(
                    ord_id               = d["ordId"],
                    inst_id              = iid,
                    side                 = d.get("side", "buy"),
                    state                = d.get("state", "live"),
                    fill_price           = float(d.get("fillPx", 0) or d.get("avgPx", 0) or 0),
                    filled_qty_contracts = float(d.get("fillSz", 0) or 0),
                    ct_val               = ct,
                    limit_price          = float(d.get("px", 0) or 0),
                ))
            return orders
        except Exception as e:
            logger.warning(f"[OKX] list_open_orders: {e}")
            return []

    # ── Fermeture position ────────────────────────────────────────────────────

    def close_position(self, symbol: str) -> bool:
        inst_id = self._to_okx(symbol)
        try:
            r = self.trade.close_positions(
                instId  = inst_id,
                mgnMode = "cross",
                ccy     = "USDT",
            )
            if r.get("code") == "0":
                logger.info(f"✅ OKX position closed: {symbol}")
                return True
            logger.error(f"[OKX] close_position {symbol}: {r.get('msg')}")
            return False
        except Exception as e:
            logger.error(f"[OKX] close_position {symbol}: {e}")
            return False

    def close_all_positions(self) -> bool:
        ok = True
        for pos in self.get_positions():
            if not self.close_position(pos.db_symbol):
                ok = False
        return ok

    # ── Fill history (détection fermeture SL/TP) ─────────────────────────────

    def get_last_fill(self, symbol: str, since_ts_ms: int = 0):
        """Dernier fill de fermeture (sell) pour ce symbole depuis since_ts_ms."""
        inst_id = self._to_okx(symbol)
        ct      = self._ct(inst_id)
        try:
            r = self.trade.get_fills_history(instType="SWAP", instId=inst_id, limit="20")
            if r.get("code") != "0" or not r.get("data"):
                return None
            for fill in r["data"]:   # newest first
                ts_ms = int(fill.get("ts", 0))
                if since_ts_ms and ts_ms < since_ts_ms:
                    continue
                if fill.get("side") == "sell":
                    return {
                        "price":          float(fill["fillPx"]),
                        "qty_contracts":  float(fill["fillSz"]),
                        "qty":            float(fill["fillSz"]) * ct,
                        "ts_ms":          ts_ms,
                    }
        except Exception as e:
            logger.warning(f"[OKX] get_last_fill {symbol}: {e}")
        return None
