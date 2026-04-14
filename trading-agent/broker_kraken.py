"""
broker_kraken.py — KrakenBroker pour Kraken Futures Perpetuals
Interface identique à BybitBroker — interchangeable.
API REST Kraken Futures (futures.kraken.com).
Symboles : PF_ETHUSD (ETH/USD), PF_SOLUSD (SOL/USD).
"""
import base64
import hashlib
import hmac
import logging
import os
import sqlite3
import time
import urllib.parse
from datetime import datetime, timezone

import pandas as pd
import requests

import config

logger = logging.getLogger(__name__)

_BASE_URL = "https://futures.kraken.com/derivatives/api/v3"

# ── Symboles ────────────────────────────────────────────────────────────────────
_DB_TO_KRAKEN = {
    "ETH/USD": "PF_ETHUSD",
    "SOL/USD": "PF_SOLUSD",
}
_KRAKEN_TO_DB = {v: k for k, v in _DB_TO_KRAKEN.items()}

# ── Timeframes ──────────────────────────────────────────────────────────────────
_TF_MAP = {
    "1Min":  "1m",
    "5Min":  "5m",
    "15Min": "15m",
    "30Min": "30m",
    "1Hour": "1h",
    "4Hour": "4h",
    "1Day":  "1d",
}

# ── Taille minimale de lot ──────────────────────────────────────────────────────
_MIN_QTY  = {"PF_ETHUSD": 0.01, "PF_SOLUSD": 1.0}
_QTY_STEP = {"PF_ETHUSD": 0.01, "PF_SOLUSD": 1.0}
_QTY_DP   = {"PF_ETHUSD": 2,    "PF_SOLUSD": 0}

_DB_PATH = os.path.join(os.path.dirname(__file__), "trades.db")


# ── Helpers ─────────────────────────────────────────────────────────────────────

def _to_kraken(symbol: str) -> str:
    return _DB_TO_KRAKEN.get(symbol, symbol)


def _round_qty(kraken_sym: str, qty: float) -> float:
    step = _QTY_STEP.get(kraken_sym, 0.01)
    dp   = _QTY_DP.get(kraken_sym, 2)
    return round(max(step, round(qty / step) * step), dp)


def _smart_round(price: float) -> float:
    if price >= 1000: return round(price, 1)
    if price >= 100:  return round(price, 2)
    if price >= 1:    return round(price, 4)
    return round(price, 6)


def _sign(secret_b64: str, endpoint: str, post_data: str, nonce: str) -> str:
    """HMAC-SHA512 signature pour Kraken Futures.
    message = SHA256(postData + nonce + endpoint_path)
    signature = HMAC-SHA512(message, base64decode(secret))
    """
    msg = (post_data + nonce + endpoint).encode("utf-8")
    sha = hashlib.sha256(msg).digest()
    key = base64.b64decode(secret_b64)
    sig = hmac.new(key, sha, hashlib.sha512).digest()
    return base64.b64encode(sig).decode()


def _nonce() -> str:
    return str(int(time.time() * 1000))


# ── SQLite trades log ───────────────────────────────────────────────────────────

def _init_db():
    con = sqlite3.connect(_DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT,
            symbol     TEXT,
            side       TEXT,
            qty        REAL,
            price      REAL,
            stop_loss  REAL,
            take_profit REAL,
            order_id   TEXT,
            status     TEXT
        )
    """)
    con.commit()
    con.close()


def _log_trade(symbol, side, qty, price, sl, tp, order_id, status):
    try:
        con = sqlite3.connect(_DB_PATH)
        con.execute(
            "INSERT INTO trades (ts,symbol,side,qty,price,stop_loss,take_profit,order_id,status) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), symbol, side, qty,
             price, sl, tp, order_id, status)
        )
        con.commit()
        con.close()
    except Exception as e:
        logger.warning(f"[Kraken] _log_trade: {e}")


# ── Position wrapper ─────────────────────────────────────────────────────────────

class Position:
    def __init__(self, kraken_sym, qty, avg_px, mark_px, upl):
        self.symbol          = kraken_sym
        self.db_symbol       = _KRAKEN_TO_DB.get(kraken_sym, kraken_sym)
        self.qty             = qty
        self.avg_entry_price = avg_px
        self.current_price   = mark_px or avg_px
        self.unrealized_pnl  = upl

        self.asset_id     = kraken_sym
        self.side         = "long"
        self.market_value = qty * (mark_px or avg_px)
        self.cost_basis   = qty * avg_px
        self.unrealized_pl = upl

    def __repr__(self):
        return (f"<Position {self.symbol} qty={self.qty} "
                f"entry={self.avg_entry_price} upl={self.unrealized_pnl}>")


# ── Broker ──────────────────────────────────────────────────────────────────────

class KrakenBroker:

    def __init__(self):
        self._api_key = os.getenv("KRAKEN_API_KEY", "")
        self._secret  = os.getenv("KRAKEN_API_SECRET", "")
        _init_db()
        logger.info("✅ KrakenBroker connecté (Kraken Futures)")

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _get(self, endpoint: str, params: dict = None) -> dict:
        nonce     = _nonce()
        params    = params or {}
        # postData pour la signature = query string SANS nonce
        post_data = urllib.parse.urlencode(params) if params else ""
        api_path  = "/derivatives/api/v3" + endpoint
        sig       = _sign(self._secret, api_path, post_data, nonce)
        # nonce ajouté séparément à la requête réelle
        req_params = dict(params)
        req_params["nonce"] = nonce
        url       = _BASE_URL + endpoint
        headers   = {
            "APIKey":  self._api_key,
            "Authent": sig,
        }
        r = requests.get(url, params=req_params, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()

    def _post(self, endpoint: str, data: dict = None) -> dict:
        nonce     = _nonce()
        data      = data or {}
        # postData pour la signature = body SANS nonce
        post_data = urllib.parse.urlencode(data) if data else ""
        api_path  = "/derivatives/api/v3" + endpoint
        sig       = _sign(self._secret, api_path, post_data, nonce)
        # nonce ajouté séparément au body réel
        req_data  = dict(data)
        req_data["nonce"] = nonce
        url       = _BASE_URL + endpoint
        headers   = {
            "APIKey":       self._api_key,
            "Authent":      sig,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        r = requests.post(url, data=req_data, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()

    # ── Compte ───────────────────────────────────────────────────────────────

    def get_portfolio_value(self) -> float:
        try:
            r = self._get("/accounts")
            accts = r.get("accounts", {})
            for acct in accts.values():
                bal = float(acct.get("balances", {}).get("USDT", 0) or 0)
                if bal > 0:
                    return bal
            # Fallback: cash balance
            cash = float(accts.get("cash", {}).get("balance", 0) or 0)
            if cash > 0:
                return cash
        except Exception as e:
            logger.warning(f"[Kraken] get_portfolio_value: {e}")
        return config.GEO_CAPITAL

    def get_available(self) -> float:
        try:
            r = self._get("/accounts")
            accts = r.get("accounts", {})
            for acct in accts.values():
                av = float(acct.get("balances", {}).get("USDT", 0) or 0)
                if av > 0:
                    return av
        except Exception as e:
            logger.warning(f"[Kraken] get_available: {e}")
        return 0.0

    # ── Positions ────────────────────────────────────────────────────────────

    def get_positions(self) -> list:
        try:
            r = self._get("/openpositions")
            out = []
            for p in r.get("openPositions", []):
                sym  = p.get("symbol", "")
                side = p.get("side", "")
                sz   = float(p.get("size", 0) or 0)
                if sz <= 0 or side.lower() != "long":
                    continue
                avg_px = float(p.get("price", 0) or 0)
                mark   = float(p.get("markValue", 0) or 0)
                mark_px = mark / sz if sz else avg_px
                upl    = float(p.get("unrealisedFunding", 0) or 0) + \
                         float(p.get("pnl", 0) or 0)
                out.append(Position(sym, sz, avg_px, mark_px, upl))
            return out
        except Exception as e:
            logger.error(f"[Kraken] get_positions: {e}")
        return []

    def get_position(self, symbol: str):
        kraken_sym = _to_kraken(symbol)
        for pos in self.get_positions():
            if pos.symbol == kraken_sym:
                return pos
        return None

    # ── Prix live ────────────────────────────────────────────────────────────

    def get_live_price(self, symbol: str) -> float | None:
        kraken_sym = _to_kraken(symbol)
        try:
            r = requests.get(
                f"{_BASE_URL}/tickers",
                timeout=10
            )
            r.raise_for_status()
            for ticker in r.json().get("tickers", []):
                if ticker.get("symbol") == kraken_sym:
                    return float(ticker.get("last", 0) or 0) or None
        except Exception as e:
            logger.warning(f"[Kraken] get_live_price {symbol}: {e}")
        return None

    def get_ask_bid(self, symbol: str):
        kraken_sym = _to_kraken(symbol)
        try:
            r = requests.get(
                f"{_BASE_URL}/orderbook",
                params={"symbol": kraken_sym},
                timeout=10
            )
            r.raise_for_status()
            ob  = r.json().get("orderBook", {})
            ask = float(ob.get("asks", [[0]])[0][0]) if ob.get("asks") else 0.0
            bid = float(ob.get("bids", [[0]])[0][0]) if ob.get("bids") else 0.0
            return ask, bid
        except Exception as e:
            logger.warning(f"[Kraken] get_ask_bid {symbol}: {e}")
        return 0.0, 0.0

    # ── Bars OHLCV ───────────────────────────────────────────────────────────

    def get_bars(self, symbol: str, timeframe: str = "1Min",
                 limit: int = 50) -> pd.DataFrame | None:
        kraken_sym = _to_kraken(symbol)
        interval   = _TF_MAP.get(timeframe, "1m")
        try:
            r = requests.get(
                f"{_BASE_URL}/history",
                params={"symbol": kraken_sym, "resolution": interval},
                timeout=15
            )
            r.raise_for_status()
            candles = r.json().get("candles", [])
            if not candles:
                return None
            rows = []
            for c in candles[-limit:]:
                rows.append({
                    "timestamp": pd.to_datetime(int(c["time"]), unit="ms",
                                                utc=True),
                    "open":   float(c["open"]),
                    "high":   float(c["high"]),
                    "low":    float(c["low"]),
                    "close":  float(c["close"]),
                    "volume": float(c.get("volume", 0)),
                    "symbol": symbol,
                })
            df = pd.DataFrame(rows).set_index("timestamp")
            if len(df) > 1:
                df = df.iloc[:-1]
            return df
        except Exception as e:
            logger.error(f"[Kraken] get_bars {symbol}: {e}")
        return None

    # ── Ordres ───────────────────────────────────────────────────────────────

    def place_order(self, symbol: str, qty: float, side: str,
                    stop_loss: float = None,
                    take_profit: float = None) -> str | None:
        """Place un ordre MARKET avec SL+TP natifs (bracket order).
        side : 'buy' ou 'sell'. Retourne l'orderId ou None."""
        kraken_sym = _to_kraken(symbol)
        kraken_qty = _round_qty(kraken_sym, qty)

        if kraken_qty < _MIN_QTY.get(kraken_sym, 0.01):
            logger.warning(f"[Kraken] place_order {symbol}: qty {kraken_qty} < min")
            return None

        kraken_side = "buy" if side.lower() == "buy" else "sell"

        data = {
            "orderType": "mkt",
            "symbol":    kraken_sym,
            "side":      kraken_side,
            "size":      str(kraken_qty),
        }

        try:
            r = self._post("/sendorder", data)
            status = r.get("result", "")
            if status != "success":
                logger.error(f"[Kraken] place_order {symbol}: {r.get('error', r)}")
                _log_trade(symbol, side, kraken_qty, 0, stop_loss, take_profit,
                           "", "error")
                return None

            order_id = (r.get("sendStatus", {}).get("order_id") or
                        r.get("sendStatus", {}).get("receivedTime", ""))

            price = self.get_live_price(symbol) or 0.0
            logger.info(
                f"[Kraken] ✅ ORDER {kraken_side.upper()} {symbol} "
                f"qty={kraken_qty} SL={stop_loss} TP={take_profit} id={order_id}"
            )
            _log_trade(symbol, side, kraken_qty, price, stop_loss, take_profit,
                       str(order_id), "filled")

            # Bracket orders : SL + TP séparés
            if stop_loss is not None:
                self._place_stop(kraken_sym, kraken_qty, kraken_side,
                                 stop_loss, "stop")
            if take_profit is not None:
                self._place_stop(kraken_sym, kraken_qty, kraken_side,
                                 take_profit, "take_profit")

            return str(order_id)

        except Exception as e:
            logger.error(f"[Kraken] place_order {symbol}: {e}")
        return None

    def _place_stop(self, kraken_sym: str, qty: float, entry_side: str,
                    trigger_price: float, order_type: str):
        """Place un ordre stop ou take_profit côté opposé à l'entrée."""
        close_side = "sell" if entry_side == "buy" else "buy"
        order_type_str = "stp" if order_type == "stop" else "take_profit"
        try:
            data = {
                "orderType":    order_type_str,
                "symbol":       kraken_sym,
                "side":         close_side,
                "size":         str(qty),
                "stopPrice":    str(_smart_round(trigger_price)),
                "reduceOnly":   "true",
            }
            r = self._post("/sendorder", data)
            if r.get("result") == "success":
                logger.info(
                    f"[Kraken] ✅ {order_type.upper()} set @ {trigger_price} "
                    f"for {kraken_sym}"
                )
            else:
                logger.warning(f"[Kraken] {order_type} order failed: {r}")
        except Exception as e:
            logger.warning(f"[Kraken] _place_stop {kraken_sym}: {e}")

    def close_position(self, symbol: str) -> bool:
        """Ferme la position entière via ordre market reduceOnly."""
        pos = self.get_position(symbol)
        if not pos or pos.qty <= 0:
            logger.info(f"[Kraken] close_position {symbol}: pas de position")
            return True
        kraken_sym = _to_kraken(symbol)
        close_side = "sell" if pos.side == "long" else "buy"
        try:
            data = {
                "orderType":  "mkt",
                "symbol":     kraken_sym,
                "side":       close_side,
                "size":       str(pos.qty),
                "reduceOnly": "true",
            }
            r = self._post("/sendorder", data)
            if r.get("result") == "success":
                logger.info(f"[Kraken] ✅ Closed position {symbol}")
                _log_trade(symbol, close_side, pos.qty,
                           pos.current_price, None, None, "", "closed")
                return True
            logger.error(f"[Kraken] close_position {symbol}: {r.get('error', r)}")
        except Exception as e:
            logger.error(f"[Kraken] close_position {symbol}: {e}")
        return False
