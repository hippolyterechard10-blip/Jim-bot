"""
broker_kraken_paper.py — KrakenPaperBroker
Wraps `kraken futures paper` CLI pour le paper trading.
- Longs ET shorts supportés
- SL/TP gérés bot-side (NATIVE_BRACKETS = False)
- Prix live via API publique Kraken Futures (pas d'auth)
"""
from __future__ import annotations
import json
import logging
import os
import subprocess
import time

import pandas as pd
import requests
import yfinance as yf

import config

logger = logging.getLogger(__name__)

_BASE_URL   = "https://futures.kraken.com/derivatives/api/v3"
_KRAKEN_CLI = os.path.expanduser("~/.cargo/bin/kraken")

_DB_TO_KRAKEN = {"ETH/USD": "PF_ETHUSD", "SOL/USD": "PF_SOLUSD"}
_KRAKEN_TO_DB = {v: k for k, v in _DB_TO_KRAKEN.items()}

_TF_MAP = {
    "1Min": "1m", "5Min": "5m", "15Min": "15m",
    "30Min": "30m", "1Hour": "1h", "4Hour": "4h", "1Day": "1d",
}
_PERIOD_MAP = {
    "1m": "1d", "5m": "5d", "15m": "8d",
    "30m": "14d", "1h": "30d", "4h": "60d", "1d": "365d",
}
_YF_MAP = {"ETH/USD": "ETH-USD", "SOL/USD": "SOL-USD"}

_MIN_QTY  = {"PF_ETHUSD": 0.01, "PF_SOLUSD": 1.0}
_QTY_STEP = {"PF_ETHUSD": 0.01, "PF_SOLUSD": 1.0}
_QTY_DP   = {"PF_ETHUSD": 2,    "PF_SOLUSD": 0}


def _to_kraken(symbol: str) -> str:
    return _DB_TO_KRAKEN.get(symbol, symbol)


def _round_qty(kraken_sym: str, qty: float) -> float:
    step = _QTY_STEP.get(kraken_sym, 0.01)
    dp   = _QTY_DP.get(kraken_sym, 2)
    return round(max(step, round(qty / step) * step), dp)


def _cli(*args) -> dict | list | None:
    cmd = [_KRAKEN_CLI, "--output", "json"] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            logger.warning(f"[KrakenPaper] CLI error: {r.stderr.strip()}")
            return None
        return json.loads(r.stdout)
    except Exception as e:
        logger.error(f"[KrakenPaper] CLI exception: {e}")
        return None


class Order:
    def __init__(self, order_id, db_symbol, side, status,
                 filled_avg_price=None, filled_qty=None,
                 qty_contracts=None, limit_price=None, type="limit"):
        self.id               = order_id
        self.db_symbol        = db_symbol
        self.side             = side           # "buy" | "sell"
        self.status           = status         # "filled" | "live" | "cancelled"
        self.filled_avg_price = filled_avg_price
        self.filled_qty       = filled_qty
        self.qty_contracts    = qty_contracts
        self.limit_price      = limit_price
        self.type             = type


class Position:
    def __init__(self, kraken_sym, qty, avg_px, mark_px, side, upl=0.0):
        self.symbol          = kraken_sym
        self.db_symbol       = _KRAKEN_TO_DB.get(kraken_sym, kraken_sym)
        self.qty             = abs(qty)
        self.avg_entry_price = avg_px
        self.current_price   = mark_px or avg_px
        self.unrealized_pnl  = upl
        self.side            = side
        self.asset_id        = kraken_sym
        self.market_value    = self.qty * self.current_price
        self.cost_basis      = self.qty * avg_px
        self.unrealized_pl   = upl

    def __repr__(self):
        return (f"<Position {self.symbol} {self.side} qty={self.qty} "
                f"entry={self.avg_entry_price} upl={self.unrealized_pnl}>")


class KrakenPaperBroker:

    NATIVE_BRACKETS = False  # SL/TP checked by bot in manage_open_positions()

    def __init__(self):
        self._orders: dict[str, Order] = {}
        status = _cli("futures", "paper", "status")
        if status is None:
            logger.info("[KrakenPaper] Initialisation du compte paper...")
            _cli("futures", "paper", "init")
        logger.info("✅ KrakenPaperBroker prêt (Kraken Futures Paper)")

    # ── Compte ───────────────────────────────────────────────────────────────

    def get_portfolio_value(self) -> float:
        data = _cli("futures", "paper", "balance")
        if data:
            row = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
            for key in ("balance", "collateral", "available", "equity"):
                val = row.get(key)
                if val is not None:
                    try:
                        return float(val)
                    except (TypeError, ValueError):
                        pass
        return config.GEO_CAPITAL

    def get_equity(self) -> float:
        return self.get_portfolio_value()

    def get_available(self) -> float:
        return self.get_portfolio_value()

    # ── Positions ────────────────────────────────────────────────────────────

    def get_positions(self) -> list:
        data = _cli("futures", "paper", "positions")
        if not data:
            return []
        if isinstance(data, dict):
            data = data.get("positions", []) or []
        out = []
        for p in data:
            sym = p.get("symbol", "")
            sz  = float(p.get("size", 0) or p.get("qty", 0) or 0)
            if sz == 0:
                continue
            # Kraken Futures retourne `size` toujours positif et le sens dans `side`.
            # Fallback signe pour brokers qui encodent le sens dans le signe.
            api_side = (p.get("side") or "").lower()
            if api_side in ("long", "short"):
                side = api_side
            else:
                side = "long" if sz > 0 else "short"
            avg_px  = float(p.get("price", 0) or p.get("entry_price", 0) or 0)
            mark_px = float(p.get("mark_price", 0) or avg_px)
            upl     = float(p.get("pnl", 0) or p.get("unrealized_pnl", 0) or 0)
            out.append(Position(sym, sz, avg_px, mark_px, side, upl))
        return out

    def get_position(self, symbol: str):
        kraken_sym = _to_kraken(symbol)
        for pos in self.get_positions():
            if pos.symbol == kraken_sym:
                return pos
        return None

    # ── Orders ───────────────────────────────────────────────────────────────

    def list_open_orders(self) -> list:
        return []  # Réconciliation désactivée pour paper — ordres market immédiats

    def get_order(self, symbol: str, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    def cancel_order(self, symbol: str, order_id: str):
        if order_id in self._orders:
            self._orders[order_id].status = "cancelled"
        _cli("futures", "paper", "cancel", "--order-id", order_id)

    def get_close_info(self, symbol: str, since_ts_ms: int = 0) -> dict | None:
        return None  # Bot-managed: clôture explicite via close_position()

    # ── Prix live (API publique) ──────────────────────────────────────────────

    def get_live_price(self, symbol: str) -> float | None:
        kraken_sym = _to_kraken(symbol)
        try:
            r = requests.get(f"{_BASE_URL}/tickers", timeout=10)
            r.raise_for_status()
            for t in r.json().get("tickers", []):
                if t.get("symbol") == kraken_sym:
                    return float(t.get("last", 0) or 0) or None
        except Exception as e:
            logger.warning(f"[KrakenPaper] get_live_price {symbol}: {e}")
        return None

    def get_ask_bid(self, symbol: str):
        kraken_sym = _to_kraken(symbol)
        try:
            r = requests.get(f"{_BASE_URL}/orderbook", params={"symbol": kraken_sym}, timeout=10)
            r.raise_for_status()
            ob  = r.json().get("orderBook", {})
            ask = float(ob.get("asks", [[0]])[0][0]) if ob.get("asks") else 0.0
            bid = float(ob.get("bids", [[0]])[0][0]) if ob.get("bids") else 0.0
            return ask, bid
        except Exception as e:
            logger.warning(f"[KrakenPaper] get_ask_bid {symbol}: {e}")
        return 0.0, 0.0

    def get_bars(self, symbol: str, timeframe: str = "1Min",
                 limit: int = 50) -> pd.DataFrame | None:
        yf_sym = _YF_MAP.get(symbol, symbol)
        yf_tf  = _TF_MAP.get(timeframe, "15m")
        period = _PERIOD_MAP.get(yf_tf, "8d")
        try:
            ticker = yf.Ticker(yf_sym)
            df = ticker.history(period=period, interval=yf_tf, auto_adjust=True)
            if df is None or df.empty:
                return None
            df = df.rename(columns={
                "Open": "open", "High": "high", "Low": "low",
                "Close": "close", "Volume": "volume",
            })
            df["symbol"] = symbol
            df.index     = pd.to_datetime(df.index, utc=True)
            df = df[["open", "high", "low", "close", "volume", "symbol"]]
            if len(df) > 1:
                df = df.iloc[:-1]
            return df.tail(limit)
        except Exception as e:
            logger.error(f"[KrakenPaper] get_bars {symbol}: {e}")
        return None

    # ── Entrées long / short ─────────────────────────────────────────────────

    def place_limit_buy(self, symbol: str, price: float,
                        stop_loss: float, take_profit: float,
                        deploy_usdt: float) -> str | None:
        """Entrée long — exécutée comme market order en paper."""
        kraken_sym = _to_kraken(symbol)
        qty = _round_qty(kraken_sym, deploy_usdt / price)
        if qty < _MIN_QTY.get(kraken_sym, 0.01):
            logger.warning(f"[KrakenPaper] place_limit_buy {symbol}: qty trop petit")
            return None
        result = _cli("futures", "paper", "buy", kraken_sym, str(qty), "--type", "market")
        if result is None:
            return None
        order_id = str(int(time.time() * 1000))
        fill_px  = self.get_live_price(symbol) or price
        self._orders[order_id] = Order(order_id, symbol, "buy", "filled", fill_px, qty, qty, price)
        logger.info(
            f"[KrakenPaper] ✅ LONG {symbol} qty={qty} ~${fill_px:.2f} "
            f"SL={stop_loss:.4f} TP={take_profit:.4f}"
        )
        return order_id

    def place_limit_sell(self, symbol: str, price: float,
                         stop_loss: float, take_profit: float,
                         deploy_usdt: float) -> str | None:
        """Entrée short — exécutée comme market order en paper."""
        kraken_sym = _to_kraken(symbol)
        qty = _round_qty(kraken_sym, deploy_usdt / price)
        if qty < _MIN_QTY.get(kraken_sym, 0.01):
            logger.warning(f"[KrakenPaper] place_limit_sell {symbol}: qty trop petit")
            return None
        result = _cli("futures", "paper", "sell", kraken_sym, str(qty), "--type", "market")
        if result is None:
            return None
        order_id = str(int(time.time() * 1000))
        fill_px  = self.get_live_price(symbol) or price
        self._orders[order_id] = Order(order_id, symbol, "sell", "filled", fill_px, qty, qty, price)
        logger.info(
            f"[KrakenPaper] ✅ SHORT {symbol} qty={qty} ~${fill_px:.2f} "
            f"SL={stop_loss:.4f} TP={take_profit:.4f}"
        )
        return order_id

    def place_order(self, symbol: str, qty: float, side: str,
                    stop_loss: float = None, take_profit: float = None) -> str | None:
        kraken_sym = _to_kraken(symbol)
        kraken_qty = _round_qty(kraken_sym, qty)
        cli_side   = "buy" if side.lower() == "buy" else "sell"
        result = _cli("futures", "paper", cli_side, kraken_sym, str(kraken_qty), "--type", "market")
        if result is None:
            return None
        order_id = str(int(time.time() * 1000))
        fill_px  = self.get_live_price(symbol) or 0.0
        self._orders[order_id] = Order(order_id, symbol, cli_side, "filled", fill_px, kraken_qty, kraken_qty)
        logger.info(f"[KrakenPaper] ✅ {cli_side.upper()} {symbol} qty={kraken_qty} ~${fill_px:.2f}")
        return order_id

    def close_position(self, symbol: str) -> bool:
        pos = self.get_position(symbol)
        if not pos or pos.qty <= 0:
            return True
        kraken_sym = _to_kraken(symbol)
        close_side = "sell" if pos.side == "long" else "buy"
        result = _cli("futures", "paper", close_side, kraken_sym, str(pos.qty), "--type", "market")
        if result is not None:
            logger.info(f"[KrakenPaper] ✅ Closed {pos.side} {symbol} qty={pos.qty}")
            return True
        logger.error(f"[KrakenPaper] close_position {symbol} failed")
        return False
