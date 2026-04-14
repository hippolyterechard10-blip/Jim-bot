"""
broker_bybit.py — BybitBroker pour Bybit EU Futures Perpetuals
Interface identique à AlpacaBroker (broker.py) — interchangeable.
Utilise pybit unified_trading HTTP session.
Demo mode : testnet=False, demo=True (compte démo Bybit).
Live mode  : testnet=False, demo=False.
"""
import logging
import os
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

# ── Taille minimale de lot ────────────────────────────────────────────────────
_MIN_QTY  = {"ETHUSDT": 0.01, "SOLUSDT": 1.0}
_QTY_STEP = {"ETHUSDT": 0.01, "SOLUSDT": 1.0}
_QTY_DP   = {"ETHUSDT": 2,    "SOLUSDT": 0}


def _to_bybit(symbol: str) -> str:
    return _DB_TO_BYBIT.get(symbol, symbol)


def _round_qty(bybit_sym: str, qty: float) -> float:
    step = _QTY_STEP.get(bybit_sym, 0.01)
    dp   = _QTY_DP.get(bybit_sym, 2)
    return round(max(step, round(qty / step) * step), dp)


def _smart_round(price: float) -> float:
    if price >= 1000: return round(price, 1)
    if price >= 100:  return round(price, 2)
    if price >= 1:    return round(price, 4)
    return round(price, 6)


# ── Position wrapper ──────────────────────────────────────────────────────────

class Position:
    """Wrapper position compatible avec l'interface AlpacaBroker."""
    def __init__(self, bybit_sym, qty, avg_px, mark_px, upl):
        self.symbol          = bybit_sym
        self.db_symbol       = _BYBIT_TO_DB.get(bybit_sym, bybit_sym)
        self.qty             = qty
        self.avg_entry_price = avg_px
        self.current_price   = mark_px or avg_px
        self.unrealized_pnl  = upl

        # Compat AlpacaBroker (champs legacy)
        self.asset_id    = bybit_sym
        self.side        = "long"
        self.market_value = qty * (mark_px or avg_px)
        self.cost_basis   = qty * avg_px
        self.unrealized_pl = upl

    def __repr__(self):
        return (f"<Position {self.symbol} qty={self.qty} "
                f"entry={self.avg_entry_price} upl={self.unrealized_pnl}>")


# ── Broker ─────────────────────────────────────────────────────────────────────

class BybitBroker:

    def __init__(self):
        self.session = HTTP(
            testnet    = False,
            demo       = (os.getenv("BYBIT_DEMO", "true").lower() == "true"),
            api_key    = config.BYBIT_API_KEY,
            api_secret = config.BYBIT_SECRET_KEY,
        )
        logger.info("✅ BybitBroker connecté")
        for sym in ("ETHUSDT", "SOLUSDT"):
            try:
                r = self.session.set_leverage(
                    category     = "linear",
                    symbol       = sym,
                    buyLeverage  = "2",
                    sellLeverage = "2",
                )
                logger.info(f"[Bybit] leverage {sym} = 2x ✓ {r.get('retMsg','')}")
            except Exception as e:
                logger.warning(f"[Bybit] set_leverage {sym}: {e}")

    # ── Compte ───────────────────────────────────────────────────────────────

    def get_portfolio_value(self) -> float:
        """Retourne l'equity totale du wallet UNIFIED en USDT."""
        try:
            r = self.session.get_wallet_balance(accountType="UNIFIED")
            if r.get("retCode") == 0:
                for acct in r["result"].get("list", []):
                    for coin in acct.get("coin", []):
                        if coin.get("coin") in ("USDT", "USDC"):
                            eq = float(coin.get("equity", 0) or 0)
                            if eq > 0:
                                return eq
                # Fallback : totalEquity du compte
                for acct in r["result"].get("list", []):
                    total = float(acct.get("totalEquity", 0) or 0)
                    if total > 0:
                        return total
        except Exception as e:
            logger.warning(f"[Bybit] get_portfolio_value: {e}")
        return config.GEO_CAPITAL

    def get_available(self) -> float:
        """Retourne le capital disponible (non engagé)."""
        try:
            r = self.session.get_wallet_balance(accountType="UNIFIED")
            if r.get("retCode") == 0:
                for acct in r["result"].get("list", []):
                    for coin in acct.get("coin", []):
                        if coin.get("coin") in ("USDT", "USDC"):
                            av = float(
                                coin.get("availableToWithdraw", 0) or 0
                            )
                            if av > 0:
                                return av
        except Exception as e:
            logger.warning(f"[Bybit] get_available: {e}")
        return 0.0

    # ── Positions ────────────────────────────────────────────────────────────

    def get_positions(self) -> list:
        """Retourne les positions long ouvertes (USDT Perpetual)."""
        try:
            r = self.session.get_positions(
                category    = "linear",
                settleCoin  = "USDT",
            )
            if r.get("retCode") != 0:
                logger.warning(f"[Bybit] get_positions: {r.get('retMsg')}")
                return []
            out = []
            for p in r["result"].get("list", []):
                sz   = float(p.get("size", 0) or 0)
                side = p.get("side", "")
                if sz <= 0 or side != "Buy":
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
        """Retourne la position pour un symbole donné ou None."""
        bybit_sym = _to_bybit(symbol)
        for pos in self.get_positions():
            if pos.symbol == bybit_sym:
                return pos
        return None

    # ── Prix live ────────────────────────────────────────────────────────────

    def get_live_price(self, symbol: str) -> float | None:
        """Retourne le lastPrice pour le symbole (ETH/USD → ETHUSDT)."""
        bybit_sym = _to_bybit(symbol)
        try:
            r = self.session.get_tickers(
                category = "linear",
                symbol   = bybit_sym,
            )
            if r.get("retCode") == 0:
                items = r["result"].get("list", [])
                if items:
                    return float(items[0].get("lastPrice", 0) or 0) or None
        except Exception as e:
            logger.warning(f"[Bybit] get_live_price {symbol}: {e}")
        return None

    def get_ask_bid(self, symbol: str):
        bybit_sym = _to_bybit(symbol)
        try:
            r = self.session.get_orderbook(
                category = "linear",
                symbol   = bybit_sym,
                limit    = 1,
            )
            if r.get("retCode") == 0:
                d   = r["result"]
                ask = float(d["a"][0][0]) if d.get("a") else 0.0
                bid = float(d["b"][0][0]) if d.get("b") else 0.0
                return ask, bid
        except Exception as e:
            logger.warning(f"[Bybit] get_ask_bid {symbol}: {e}")
        return 0.0, 0.0

    # ── Bars OHLCV ───────────────────────────────────────────────────────────

    def get_bars(self, symbol: str, timeframe: str = "1Min",
                 limit: int = 50) -> pd.DataFrame | None:
        """Retourne un DataFrame OHLCV trié ascending avec index timestamp."""
        bybit_sym = _to_bybit(symbol)
        interval  = _TF_MAP.get(timeframe, "1")
        try:
            r = self.session.get_kline(
                category = "linear",
                symbol   = bybit_sym,
                interval = interval,
                limit    = limit,
            )
            if r.get("retCode") != 0:
                logger.error(f"[Bybit] get_bars {symbol}: {r.get('retMsg')}")
                return None
            data = r["result"].get("list", [])
            if not data:
                return None
            data = list(reversed(data))
            rows = []
            for c in data:
                rows.append({
                    "timestamp": pd.to_datetime(int(c[0]), unit="ms",
                                                utc=True),
                    "open":   float(c[1]),
                    "high":   float(c[2]),
                    "low":    float(c[3]),
                    "close":  float(c[4]),
                    "volume": float(c[5]),
                    "symbol": symbol,
                })
            df = pd.DataFrame(rows).set_index("timestamp")
            if len(df) > 1:
                df = df.iloc[:-1]
            return df
        except Exception as e:
            logger.error(f"[Bybit] get_bars {symbol}: {e}")
        return None

    # ── Ordres ───────────────────────────────────────────────────────────────

    def place_order(self, symbol: str, qty: float, side: str,
                    stop_loss: float = None,
                    take_profit: float = None) -> str | None:
        """Place un ordre MARKET avec SL+TP natifs en une seule fois.
        SL et TP restent sur le matching engine Bybit — pas de bot-watcher.
        side : 'buy' ou 'sell' (insensible à la casse).
        Retourne l'orderId ou None."""
        bybit_sym = _to_bybit(symbol)
        bybit_qty = _round_qty(bybit_sym, qty)

        if bybit_qty < _MIN_QTY.get(bybit_sym, 0.01):
            logger.warning(
                f"[Bybit] place_order {symbol}: qty {bybit_qty} < min"
            )
            return None

        bybit_side = "Buy" if side.lower() == "buy" else "Sell"

        params = dict(
            category    = "linear",
            symbol      = bybit_sym,
            side        = bybit_side,
            orderType   = "Market",
            qty         = str(bybit_qty),
        )

        if stop_loss is not None:
            params["stopLoss"]   = str(_smart_round(stop_loss))
            params["slTriggerBy"] = "LastPrice"
        if take_profit is not None:
            params["takeProfit"] = str(_smart_round(take_profit))
            params["tpTriggerBy"] = "LastPrice"
        if stop_loss is not None or take_profit is not None:
            params["tpslMode"] = "Full"

        try:
            r = self.session.place_order(**params)
            if r.get("retCode") == 0:
                ord_id = r["result"]["orderId"]
                logger.info(
                    f"[Bybit] ✅ ORDER {bybit_side} {symbol} "
                    f"qty={bybit_qty} SL={stop_loss} TP={take_profit} "
                    f"id={ord_id}"
                )
                return ord_id
            logger.error(
                f"[Bybit] place_order {symbol}: {r.get('retMsg')}"
            )
        except Exception as e:
            logger.error(f"[Bybit] place_order {symbol}: {e}")
        return None

    def close_position(self, symbol: str) -> bool:
        """Ferme la position entière via ordre market reduceOnly."""
        pos = self.get_position(symbol)
        if not pos or pos.qty <= 0:
            logger.info(f"[Bybit] close_position {symbol}: pas de position")
            return True
        bybit_sym = _to_bybit(symbol)
        try:
            r = self.session.place_order(
                category    = "linear",
                symbol      = bybit_sym,
                side        = "Sell",
                orderType   = "Market",
                qty         = str(pos.qty),
                reduceOnly  = True,
                timeInForce = "IOC",
            )
            if r.get("retCode") == 0:
                logger.info(f"[Bybit] ✅ Closed position {symbol}")
                return True
            logger.error(
                f"[Bybit] close_position {symbol}: {r.get('retMsg')}"
            )
        except Exception as e:
            logger.error(f"[Bybit] close_position {symbol}: {e}")
        return False
