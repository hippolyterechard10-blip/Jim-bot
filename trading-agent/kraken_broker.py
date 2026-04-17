"""
kraken_broker.py — Broker Kraken Futures (Perpetual)
Interface identique à bybit_broker.py — stratégie GEO plug-and-play.
ETH/USD → PF_ETHUSD, SOL/USD → PF_SOLUSD
SL = ordre stop resting sur Kraken, TP = limit sell resting.
Les deux survivent à un crash du bot (sur le matching engine Kraken).
OHLCV natif via /api/charts/v1/trade (pas de dépendance yfinance).
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse

import pandas as pd
import requests

import config

logger = logging.getLogger(__name__)

BASE_URL_LIVE   = "https://futures.kraken.com"
BASE_URL_PAPER  = "https://demo-futures.kraken.com"
SLTP_CACHE_FILE = "kraken_sltp_cache.json"

API_PREFIX   = "/derivatives/api/v3"
CHARTS_PATH  = "/api/charts/v1/trade"   # OHLCV public endpoint

# ── Symboles ───────────────────────────────────────────────────────────────────
_DB_TO_KRAKEN = {
    "ETH/USD": "PF_ETHUSD",
    "SOL/USD": "PF_SOLUSD",
}
_KRAKEN_TO_DB = {v: k for k, v in _DB_TO_KRAKEN.items()}

# Résolutions natives Kraken Futures
_TF_MAP = {
    "1Min":  "1m",
    "5Min":  "5m",
    "15Min": "15m",
    "30Min": "30m",
    "1Hour": "1h",
    "4Hour": "4h",
    "1Day":  "1d",
}

# Durée d'une bougie en secondes — sert à calculer la fenêtre from/to
_TF_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}

_MIN_QTY = {"PF_ETHUSD": 0.01, "PF_SOLUSD": 1.0}
_QTY_DP  = {"PF_ETHUSD": 2,    "PF_SOLUSD": 0}


def _round_qty(symbol: str, qty: float) -> float:
    step = _MIN_QTY.get(symbol, 0.01)
    dp   = _QTY_DP.get(symbol, 2)
    return round(max(step, round(qty / step) * step), dp)


def _smart_round(price: float) -> float:
    if price >= 1000: return round(price, 1)
    if price >= 100:  return round(price, 2)
    if price >= 1:    return round(price, 4)
    return round(price, 6)


# ── Data classes ───────────────────────────────────────────────────────────────

class Position:
    def __init__(self, kraken_symbol, qty, avg_px, current_px, upl):
        self.kraken_symbol   = kraken_symbol
        self.symbol          = kraken_symbol
        self.db_symbol       = _KRAKEN_TO_DB.get(kraken_symbol, kraken_symbol)
        self.qty             = qty
        self.avg_entry_price = avg_px
        self.current_price   = current_px or avg_px
        self.unrealized_pnl  = upl


class OrderInfo:
    def __init__(self, ord_id, kraken_symbol, side, state,
                 fill_price, filled_qty, limit_price=0.0):
        self.id               = ord_id
        self.kraken_symbol    = kraken_symbol
        self.symbol           = kraken_symbol
        self.db_symbol        = _KRAKEN_TO_DB.get(kraken_symbol, kraken_symbol)
        self.side             = side.lower()
        self.status           = state
        self.filled_avg_price = fill_price
        self.filled_qty       = filled_qty
        self.qty_contracts    = filled_qty
        self.limit_price      = limit_price
        self.time_in_force    = "gtc"
        self.type             = "limit"


# ── Broker ─────────────────────────────────────────────────────────────────────

class KrakenBroker:

    def __init__(self):
        self.api_key    = config.KRAKEN_API_KEY
        self.api_secret = config.KRAKEN_SECRET_KEY
        self.base_url   = BASE_URL_PAPER if config.KRAKEN_PAPER else BASE_URL_LIVE
        self._sltp      = self._load_sltp_cache()
        mode = "PAPER (démo)" if config.KRAKEN_PAPER else "LIVE 🔴"
        logger.info(f"✅ Kraken Futures broker connecté ({mode})")

    # ── Stub OKX compat ──────────────────────────────────────────────────────
    def _ct(self, symbol):
        return 1.0

    # ── Cache SL/TP (survit aux restarts) ────────────────────────────────────

    def _load_sltp_cache(self) -> dict:
        try:
            if os.path.exists(SLTP_CACHE_FILE):
                with open(SLTP_CACHE_FILE) as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_sltp_cache(self):
        try:
            with open(SLTP_CACHE_FILE, "w") as f:
                json.dump(self._sltp, f)
        except Exception as e:
            logger.warning(f"[Kraken] save_sltp_cache: {e}")

    # ── Auth ─────────────────────────────────────────────────────────────────

    def _sign(self, endpoint_path: str, post_data: str, nonce: str) -> str:
        """Signature Kraken Futures.
        Auth = Base64( HMAC-SHA512( Base64Decode(secret),
                                    SHA256(postData + nonce + endpointPath) ) )
        endpoint_path est le path SANS le préfixe /derivatives/api/v3.
        """
        msg = (post_data + nonce + endpoint_path).encode("utf-8")
        sha = hashlib.sha256(msg).digest()
        key = base64.b64decode(self.api_secret)
        sig = hmac.new(key, sha, hashlib.sha512).digest()
        return base64.b64encode(sig).decode()

    def _get(self, path: str, params: dict = None) -> dict:
        nonce     = str(int(time.time() * 1000))
        qs        = urllib.parse.urlencode(params or {})
        endpoint  = path[len(API_PREFIX):] if path.startswith(API_PREFIX) else path
        headers   = {
            "APIKey":  self.api_key,
            "Nonce":   nonce,
            "Authent": self._sign(endpoint, qs, nonce),
        }
        url = self.base_url + path + (("?" + qs) if qs else "")
        r = requests.get(url, headers=headers, timeout=10)
        return r.json()

    def _post(self, path: str, data: dict) -> dict:
        nonce     = str(int(time.time() * 1000))
        post_data = urllib.parse.urlencode(data or {})
        endpoint  = path[len(API_PREFIX):] if path.startswith(API_PREFIX) else path
        headers   = {
            "APIKey":       self.api_key,
            "Nonce":        nonce,
            "Authent":      self._sign(endpoint, post_data, nonce),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        r = requests.post(self.base_url + path, data=post_data,
                          headers=headers, timeout=10)
        return r.json()

    def _to_kraken(self, symbol: str) -> str:
        return _DB_TO_KRAKEN.get(symbol, symbol)

    # ── Account ──────────────────────────────────────────────────────────────

    def get_available(self) -> float:
        try:
            r    = self._get("/derivatives/api/v3/accounts")
            flex = r.get("accounts", {}).get("flex", {})
            return float(flex.get("availableFunds", 0) or 0)
        except Exception as e:
            logger.warning(f"[Kraken] get_available: {e}")
        return 0.0

    def get_equity(self) -> float:
        try:
            r    = self._get("/derivatives/api/v3/accounts")
            flex = r.get("accounts", {}).get("flex", {})
            return float(flex.get("equity", 0) or 0)
        except Exception as e:
            logger.warning(f"[Kraken] get_equity: {e}")
        return config.GEO_CAPITAL

    def get_portfolio_value(self) -> float:
        return self.get_equity()

    def get_account(self):
        class _A: pass
        a            = _A()
        a.equity     = self.get_equity()
        a.buying_power = self.get_available()
        return a

    # ── Positions ────────────────────────────────────────────────────────────

    def get_positions(self) -> list:
        try:
            r = self._get("/derivatives/api/v3/openpositions")
            if r.get("result") != "success":
                logger.warning(f"[Kraken] get_positions: {r.get('error')}")
                return []
            out = []
            for p in r.get("openPositions", []):
                if p.get("side", "").lower() != "long":
                    continue
                sym = p.get("symbol", "").upper()
                qty = float(p.get("size", 0) or 0)
                if qty <= 0:
                    continue
                avg_px  = float(p.get("price", 0) or 0)
                mark_px = self.get_live_price(
                    _KRAKEN_TO_DB.get(sym, sym)
                ) or avg_px
                pnl = float(p.get("pnl", 0) or 0)
                pos = Position(sym, qty, avg_px, mark_px, pnl)
                out.append(pos)
                self._ensure_sltp(pos)
            return out
        except Exception as e:
            logger.error(f"[Kraken] get_positions: {e}")
        return []

    def get_position(self, symbol: str):
        sym = self._to_kraken(symbol)
        for pos in self.get_positions():
            if pos.kraken_symbol == sym:
                return pos
        return None

    # ── Auto SL/TP ───────────────────────────────────────────────────────────

    def _ensure_sltp(self, pos: Position):
        """Place SL et TP orders si absents pour cette position."""
        sym    = pos.kraken_symbol
        cached = self._sltp.get(sym, {})
        sl     = cached.get("sl")
        tp     = cached.get("tp")
        if not sl or not tp:
            return

        changed = False
        if not cached.get("sl_order_id"):
            sl_id = self._place_stop(sym, pos.qty, sl)
            if sl_id:
                self._sltp[sym]["sl_order_id"] = sl_id
                logger.info(f"[Kraken] 🛡️  SL placed {sym} @ ${sl} id={sl_id}")
                changed = True

        if not cached.get("tp_order_id"):
            tp_id = self._place_limit_sell(sym, pos.qty, tp)
            if tp_id:
                self._sltp[sym]["tp_order_id"] = tp_id
                logger.info(f"[Kraken] 🎯 TP placed {sym} @ ${tp} id={tp_id}")
                changed = True

        if changed:
            self._save_sltp_cache()

    def _place_stop(self, kraken_sym: str, qty: float,
                    stop_price: float) -> str | None:
        try:
            r = self._post("/derivatives/api/v3/sendorder", {
                "orderType":     "stp",
                "symbol":        kraken_sym,
                "side":          "sell",
                "size":          str(qty),
                "stopPrice":     str(_smart_round(stop_price)),
                "triggerSignal": "last",
            })
            if r.get("result") == "success":
                return r.get("sendStatus", {}).get("order_id")
            logger.warning(f"[Kraken] _place_stop: {r.get('error')}")
        except Exception as e:
            logger.error(f"[Kraken] _place_stop: {e}")
        return None

    def _place_limit_sell(self, kraken_sym: str, qty: float,
                          limit_price: float) -> str | None:
        try:
            r = self._post("/derivatives/api/v3/sendorder", {
                "orderType":  "lmt",
                "symbol":     kraken_sym,
                "side":       "sell",
                "size":       str(qty),
                "limitPrice": str(_smart_round(limit_price)),
            })
            if r.get("result") == "success":
                return r.get("sendStatus", {}).get("order_id")
            logger.warning(f"[Kraken] _place_limit_sell: {r.get('error')}")
        except Exception as e:
            logger.error(f"[Kraken] _place_limit_sell: {e}")
        return None

    # ── Prix live ────────────────────────────────────────────────────────────

    def get_live_price(self, symbol: str) -> float | None:
        sym = self._to_kraken(symbol)
        try:
            r = requests.get(f"{self.base_url}/derivatives/api/v3/tickers",
                             timeout=8)
            for t in r.json().get("tickers", []):
                if t.get("symbol", "").upper() == sym:
                    bid  = float(t.get("bid", 0) or 0)
                    ask  = float(t.get("ask", 0) or 0)
                    last = float(t.get("last", 0) or 0)
                    if bid and ask:
                        return (bid + ask) / 2
                    return last or None
        except Exception as e:
            logger.warning(f"[Kraken] get_live_price {symbol}: {e}")
        return None

    def get_ask_bid(self, symbol: str):
        sym = self._to_kraken(symbol)
        try:
            r = requests.get(f"{self.base_url}/derivatives/api/v3/tickers",
                             timeout=8)
            for t in r.json().get("tickers", []):
                if t.get("symbol", "").upper() == sym:
                    return (float(t.get("ask", 0) or 0),
                            float(t.get("bid", 0) or 0))
        except Exception as e:
            logger.warning(f"[Kraken] get_ask_bid {symbol}: {e}")
        return 0.0, 0.0

    # ── Bars OHLCV ───────────────────────────────────────────────────────────

    def get_bars(self, symbol: str, timeframe: str = "15Min",
                 limit: int = 100) -> pd.DataFrame | None:
        """OHLCV depuis le endpoint public Kraken Futures — pas de clé API requise.
        Endpoint: /api/charts/v1/trade/{symbol}/{resolution}?from=...&to=...
        """
        kraken_sym = self._to_kraken(symbol)
        tf         = _TF_MAP.get(timeframe, "15m")
        step_s     = _TF_SECONDS.get(tf, 900)
        now_s      = int(time.time())
        # marge de sécurité (+5 bougies) pour tolérer les trous éventuels
        from_s     = now_s - step_s * (limit + 5)
        url = f"{BASE_URL_LIVE}{CHARTS_PATH}/{kraken_sym}/{tf}"
        try:
            r = requests.get(
                url,
                params={"from": from_s, "to": now_s},
                timeout=10,
            )
            r.raise_for_status()
            candles = r.json().get("candles", [])
            if not candles:
                return None
            rows = []
            for c in candles:
                rows.append({
                    "timestamp": pd.to_datetime(int(c["time"]),
                                                unit="ms", utc=True),
                    "open":   float(c["open"]),
                    "high":   float(c["high"]),
                    "low":    float(c["low"]),
                    "close":  float(c["close"]),
                    "volume": float(c.get("volume", 0) or 0),
                    "symbol": symbol,
                })
            df = pd.DataFrame(rows).set_index("timestamp")
            # écarte la bougie en cours (non close)
            if len(df) > 1:
                df = df.iloc[:-1]
            return df.tail(limit)
        except Exception as e:
            logger.error(f"[Kraken] get_bars {symbol}: {e}")
        return None

    # ── Orders ───────────────────────────────────────────────────────────────

    def place_limit_buy(self, symbol: str, price: float,
                        stop_loss: float, take_profit: float,
                        deploy_usdt: float) -> str | None:
        sym = self._to_kraken(symbol)
        qty = _round_qty(sym, deploy_usdt / price)
        if qty < _MIN_QTY.get(sym, 0.01):
            logger.warning(f"[Kraken] place_limit_buy {symbol}: "
                           f"qty {qty} < min")
            return None
        try:
            r = self._post("/derivatives/api/v3/sendorder", {
                "orderType":  "lmt",
                "symbol":     sym,
                "side":       "buy",
                "size":       str(qty),
                "limitPrice": str(_smart_round(price)),
            })
            if r.get("result") == "success":
                ord_id = r.get("sendStatus", {}).get("order_id")
                self._sltp[sym] = {
                    "sl": stop_loss, "tp": take_profit,
                    "qty": qty,
                    "sl_order_id": None, "tp_order_id": None,
                }
                self._save_sltp_cache()
                logger.info(
                    f"[Kraken] 📋 LIMIT BUY: {symbol} @ ${price} "
                    f"qty={qty} SL=${stop_loss} TP=${take_profit} id={ord_id}"
                )
                return ord_id
            logger.error(f"[Kraken] place_limit_buy {symbol}: "
                         f"{r.get('error')}")
        except Exception as e:
            logger.error(f"[Kraken] place_limit_buy {symbol}: {e}")
        return None

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        try:
            r = self._post("/derivatives/api/v3/cancelorder",
                           {"order_id": order_id})
            if r.get("result") == "success":
                logger.info(f"[Kraken] 🛑 Cancelled {order_id}")
                return True
            logger.warning(f"[Kraken] cancel_order {order_id}: "
                           f"{r.get('error')}")
        except Exception as e:
            logger.warning(f"[Kraken] cancel_order {order_id}: {e}")
        return False

    def get_order(self, symbol: str, order_id: str):
        for o in self.list_open_orders(symbol):
            if o.id == order_id:
                return o
        # Chercher dans les fills récents
        try:
            r = self._get("/derivatives/api/v3/fills")
            for fill in r.get("fills", []):
                if fill.get("order_id") == order_id:
                    sym   = fill.get("symbol", "").upper()
                    price = float(fill.get("price", 0) or 0)
                    qty   = float(fill.get("size", 0) or 0)
                    return OrderInfo(
                        ord_id      = order_id,
                        kraken_symbol = sym or self._to_kraken(symbol),
                        side        = fill.get("side", "buy"),
                        state       = "filled",
                        fill_price  = price,
                        filled_qty  = qty,
                        limit_price = price,
                    )
        except Exception as e:
            logger.warning(f"[Kraken] get_order {order_id}: {e}")
        return None

    def list_open_orders(self, symbol: str = None) -> list:
        try:
            r = self._get("/derivatives/api/v3/openorders")
            if r.get("result") != "success":
                return []
            orders = []
            for d in r.get("openOrders", []):
                sym  = d.get("symbol", "").upper()
                side = d.get("side", "buy").lower()
                if symbol and sym != self._to_kraken(symbol):
                    continue
                if side != "buy":
                    continue
                orders.append(OrderInfo(
                    ord_id      = d.get("order_id"),
                    kraken_symbol = sym,
                    side        = side,
                    state       = "open",
                    fill_price  = 0.0,
                    filled_qty  = 0.0,
                    limit_price = float(d.get("limitPrice", 0) or 0),
                ))
            return orders
        except Exception as e:
            logger.warning(f"[Kraken] list_open_orders: {e}")
        return []

    def cancel_all_pending(self, symbol: str = None):
        try:
            if symbol:
                r = self._post("/derivatives/api/v3/cancelallorders",
                               {"symbol": self._to_kraken(symbol)})
            else:
                r = self._post("/derivatives/api/v3/cancelallorders", {})
            if r.get("result") == "success":
                logger.info(f"[Kraken] cancelled all orders {symbol or ''}")
        except Exception as e:
            logger.warning(f"[Kraken] cancel_all_pending: {e}")

    # ── Fermeture ────────────────────────────────────────────────────────────

    def close_position(self, symbol: str) -> bool:
        sym = self._to_kraken(symbol)
        pos = self.get_position(symbol)
        if not pos or pos.qty <= 0:
            logger.info(f"[Kraken] close_position {symbol}: pas de position")
            return True
        try:
            cached = self._sltp.get(sym, {})
            for k in ("sl_order_id", "tp_order_id"):
                oid = cached.get(k)
                if oid:
                    self.cancel_order(symbol, oid)

            r = self._post("/derivatives/api/v3/sendorder", {
                "orderType": "mkt",
                "symbol":    sym,
                "side":      "sell",
                "size":      str(pos.qty),
            })
            if r.get("result") == "success":
                self._sltp.pop(sym, None)
                self._save_sltp_cache()
                logger.info(f"✅ Kraken position closed: {symbol}")
                return True
            logger.error(f"[Kraken] close_position {symbol}: "
                         f"{r.get('error')}")
        except Exception as e:
            logger.error(f"[Kraken] close_position {symbol}: {e}")
        return False

    def close_all_positions(self) -> bool:
        ok = True
        for pos in self.get_positions():
            if not self.close_position(pos.db_symbol):
                ok = False
        return ok

    # ── Détection SL/TP ──────────────────────────────────────────────────────

    def get_close_info(self, symbol: str,
                       since_ts_ms: int = 0) -> dict | None:
        try:
            r = self._get("/derivatives/api/v3/fills")
            for fill in r.get("fills", []):
                sym = fill.get("symbol", "").upper()
                if sym != self._to_kraken(symbol):
                    continue
                if fill.get("side", "").lower() != "sell":
                    continue
                fill_time = fill.get("fillTime", "")
                if since_ts_ms:
                    try:
                        ts = int(pd.to_datetime(fill_time).timestamp()
                                 * 1000)
                        if ts < since_ts_ms:
                            continue
                    except Exception:
                        pass
                price = float(fill.get("price", 0) or 0)
                if price <= 0:
                    continue
                cached = self._sltp.get(self._to_kraken(symbol), {})
                sl = cached.get("sl", 0)
                tp = cached.get("tp", 0)
                if tp and price >= tp * 0.999:
                    reason = "target"
                elif sl and price <= sl * 1.001:
                    reason = "stop"
                else:
                    reason = None
                return {
                    "price":  price,
                    "qty":    float(fill.get("size", 0) or 0),
                    "reason": reason,
                    "source": "kraken_fill",
                }
        except Exception as e:
            logger.warning(f"[Kraken] get_close_info {symbol}: {e}")
        return None

    def get_last_fill(self, symbol: str,
                      since_ts_ms: int = 0) -> dict | None:
        return self.get_close_info(symbol, since_ts_ms)
