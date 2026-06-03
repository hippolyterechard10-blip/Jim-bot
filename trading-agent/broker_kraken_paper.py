"""
broker_kraken_paper.py — KrakenPaperBroker

Wraps `kraken futures paper` CLI for paper trading.

Phase 4.2 (2026-06-01) — Brackets natifs reduce-only :
  Entrée = market order, puis SL stop reduce-only + TP take-profit reduce-only
  placés immédiatement sur le broker. Plus de polling 30s pour les sorties.

  Safety (P7 design) :
    - Si SL post échoue après retries → emergency market close (pas de position nue)
    - Si TP post échoue (post-SL OK) → continue sans TP (capital protégé par SL)
    - OCO : quand un bracket fill, cancel l'autre via cancel_twin_brackets()
    - Timeout 2h bot-side cancelle les 2 brackets puis market close

  Reconciliation persistante :
    - `_sltp[kraken_sym]` cache JSON (entry order_id, sl_order_id, tp_order_id, side)
    - get_close_info() poll `fills` CLI, match par client_order_id
"""
from __future__ import annotations
import json
import logging
import os
import subprocess
import time

import pandas as pd
import requests

import config
from kraken_candles import fetch_kraken_bars

logger = logging.getLogger(__name__)

_BASE_URL   = "https://futures.kraken.com/derivatives/api/v3"
_KRAKEN_CLI = os.path.expanduser("~/.cargo/bin/kraken")
_SLTP_CACHE_FILE = "kraken_paper_sltp_cache.json"

_DB_TO_KRAKEN = {"ETH/USD": "PF_ETHUSD", "SOL/USD": "PF_SOLUSD"}
_KRAKEN_TO_DB = {v: k for k, v in _DB_TO_KRAKEN.items()}

_TF_MAP = {
    "1Min": "1m", "5Min": "5m", "15Min": "15m",
    "30Min": "30m", "1Hour": "1h", "4Hour": "4h", "1Day": "1d",
}

_MIN_QTY  = {"PF_ETHUSD": 0.01, "PF_SOLUSD": 1.0}
_QTY_STEP = {"PF_ETHUSD": 0.01, "PF_SOLUSD": 1.0}
_QTY_DP   = {"PF_ETHUSD": 2,    "PF_SOLUSD": 0}

# Price rounding (mirrored from kraken_broker)
_PRICE_DP = {"PF_ETHUSD": 2, "PF_SOLUSD": 3}


def _to_kraken(symbol: str) -> str:
    return _DB_TO_KRAKEN.get(symbol, symbol)


def _round_qty(kraken_sym: str, qty: float) -> float:
    step = _QTY_STEP.get(kraken_sym, 0.01)
    dp   = _QTY_DP.get(kraken_sym, 2)
    return round(max(step, round(qty / step) * step), dp)


def _round_price(kraken_sym: str, price: float) -> float:
    return round(price, _PRICE_DP.get(kraken_sym, 2))


def _cli(*args, retries: int = 3, base_backoff: float = 0.5) -> dict | list | None:
    """Run `kraken --output json <args>` with retry on transient state-lock errors.

    Kraken Futures Paper sérialise via un file lock. Sous charge (dashboard +
    fast loop + slow loop concurrents) on observe `Validation error: Futures
    paper state is locked by another process`. Retry 3× avec backoff 0.5/1/2s.
    """
    cmd = [_KRAKEN_CLI, "--output", "json"] + list(args)
    last_err = None
    for attempt in range(retries):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                return json.loads(r.stdout) if r.stdout.strip() else None
            # Détecter lock error spécifiquement (sur stdout JSON, pas stderr)
            is_lock = False
            payload = {}
            if r.stdout:
                try:
                    payload = json.loads(r.stdout)
                    msg = (payload.get("message") or "").lower()
                    is_lock = "locked by another process" in msg or "try again shortly" in msg
                except Exception:
                    pass
            if is_lock and attempt < retries - 1:
                time.sleep(base_backoff * (2 ** attempt))  # 0.5, 1.0, 2.0
                continue
            last_err = (payload.get("message") if is_lock else r.stderr.strip()) or f"exit={r.returncode}"
            logger.warning(f"[KrakenPaper] CLI error (attempt {attempt+1}/{retries}): {last_err}")
            return None
        except subprocess.TimeoutExpired:
            last_err = "timeout 15s"
            if attempt < retries - 1:
                time.sleep(base_backoff * (2 ** attempt))
                continue
            logger.error(f"[KrakenPaper] CLI timeout after {retries} attempts: {cmd[3:]}")
            return None
        except Exception as e:
            logger.error(f"[KrakenPaper] CLI exception: {e}")
            return None
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

    # P7 — brackets natifs reduce-only désormais posés à l'entry.
    # Source de vérité des sorties SL/TP = broker fills, plus polling 30s.
    NATIVE_BRACKETS = True

    def __init__(self):
        self._orders: dict[str, Order] = {}
        self._sltp: dict[str, dict] = self._load_sltp_cache()
        status = _cli("futures", "paper", "status")
        if status is None:
            logger.info("[KrakenPaper] Initialisation du compte paper...")
            _cli("futures", "paper", "init")
        logger.info("✅ KrakenPaperBroker prêt (Kraken Futures Paper) — brackets natifs ON")

    # ── SLTP cache persistance ──────────────────────────────────────────────

    def _load_sltp_cache(self) -> dict:
        try:
            if os.path.exists(_SLTP_CACHE_FILE):
                with open(_SLTP_CACHE_FILE) as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"[KrakenPaper] load_sltp_cache: {e}")
        return {}

    def _save_sltp_cache(self):
        try:
            with open(_SLTP_CACHE_FILE, "w") as f:
                json.dump(self._sltp, f, indent=2)
        except Exception as e:
            logger.warning(f"[KrakenPaper] save_sltp_cache: {e}")

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
        """Returns open orders from the Kraken paper CLI. Used by strategy to
        observe SL/TP brackets and reconcile state."""
        data = _cli("futures", "paper", "orders")
        if not data:
            return []
        if isinstance(data, dict):
            data = data.get("orders", []) or data.get("openOrders", []) or []
        out = []
        for o in data:
            oid = o.get("order_id") or o.get("orderId") or o.get("id")
            sym = o.get("symbol", "")
            side = (o.get("side") or "").lower()
            otype = o.get("type") or o.get("orderType") or "limit"
            status = (o.get("status") or "live").lower()
            limit_px = o.get("limit_price") or o.get("limitPrice") or o.get("price")
            qty = o.get("size") or o.get("qty") or 0
            cli_id = o.get("client_order_id") or o.get("cliOrdId")
            db_sym = _KRAKEN_TO_DB.get(sym, sym)
            out.append(Order(str(oid), db_sym, side, status,
                             qty_contracts=float(qty) if qty else None,
                             limit_price=float(limit_px) if limit_px else None,
                             type=otype))
            # Attach client_order_id for traceability
            out[-1].client_order_id = cli_id
        return out

    def get_order(self, symbol: str, order_id: str) -> Order | None:
        if order_id in self._orders:
            return self._orders[order_id]
        # Fall back to broker query
        data = _cli("futures", "paper", "order-status", "--order-id", order_id)
        if not data:
            return None
        status = (data.get("status") or "live").lower()
        return Order(order_id, symbol, data.get("side", ""), status,
                     filled_avg_price=data.get("filled_avg_price"),
                     filled_qty=data.get("filled_qty"),
                     limit_price=data.get("limit_price"),
                     type=data.get("type", "limit"))

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        if order_id in self._orders:
            self._orders[order_id].status = "cancelled"
        result = _cli("futures", "paper", "cancel", "--order-id", order_id)
        return result is not None

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
        try:
            return fetch_kraken_bars(symbol, timeframe, limit)
        except Exception as e:
            logger.error(f"[KrakenPaper] get_bars {symbol}: {e}")
            return None

    # ── Helpers ordres brackets (P7) ─────────────────────────────────────────

    def _parse_order_id(self, cli_result) -> str | None:
        """Extract the broker order_id from a CLI buy/sell JSON response."""
        if not cli_result or not isinstance(cli_result, dict):
            return None
        for k in ("order_id", "orderId", "id"):
            oid = cli_result.get(k)
            if oid:
                return str(oid)
        # Some responses nest under sendStatus or similar
        for nest in ("sendStatus", "result", "data"):
            sub = cli_result.get(nest)
            if isinstance(sub, dict):
                for k in ("order_id", "orderId", "id"):
                    oid = sub.get(k)
                    if oid:
                        return str(oid)
        return None

    def _place_stop_close(self, kraken_sym: str, close_side: str, qty: float,
                          stop_price: float, client_order_id: str) -> str | None:
        """Place a stop reduce-only order. Triggered by mark price.

        close_side : 'sell' for long (close = sell), 'buy' for short.
        """
        sp = _round_price(kraken_sym, stop_price)
        result = _cli("futures", "paper", close_side, kraken_sym, str(qty),
                      "--type", "stop", "--stop-price", str(sp),
                      "--reduce-only", "--trigger-signal", "mark",
                      "--client-order-id", client_order_id)
        return self._parse_order_id(result)

    def _place_tp_close(self, kraken_sym: str, close_side: str, qty: float,
                        tp_price: float, client_order_id: str) -> str | None:
        """Place a take-profit reduce-only order. Triggered by mark price."""
        tp = _round_price(kraken_sym, tp_price)
        result = _cli("futures", "paper", close_side, kraken_sym, str(qty),
                      "--type", "take-profit", "--stop-price", str(tp),
                      "--reduce-only", "--trigger-signal", "mark",
                      "--client-order-id", client_order_id)
        return self._parse_order_id(result)

    def _emergency_close(self, kraken_sym: str, close_side: str, qty: float,
                         reason: str = "no_bracket") -> bool:
        """Force-close a naked position via market reduce-only. Called when
        bracket placement failed and we cannot tolerate the position nue."""
        result = _cli("futures", "paper", close_side, kraken_sym, str(qty),
                      "--type", "market", "--reduce-only")
        if result is not None:
            logger.critical(
                f"[KrakenPaper] 🚨 EMERGENCY CLOSE {kraken_sym} {close_side} "
                f"qty={qty} reason={reason}"
            )
            return True
        logger.critical(
            f"[KrakenPaper] 🚨🚨 EMERGENCY CLOSE FAILED {kraken_sym} — position naked"
        )
        return False

    def _place_market_with_brackets(self, symbol: str, entry_side: str,
                                    qty: float, stop_loss: float,
                                    take_profit: float) -> str | None:
        """Common entry path : market entry + SL bracket + TP bracket.

        entry_side : 'buy' (long) or 'sell' (short).
        Returns : entry order_id on success, None on abort.

        On bracket failure :
          - SL fails → emergency close + abort (return None)
          - TP fails (SL OK) → continue without TP (logged as warning)
        """
        kraken_sym = _to_kraken(symbol)
        position_side = "long" if entry_side == "buy" else "short"
        close_side    = "sell" if position_side == "long" else "buy"

        # 1. Market entry
        entry_result = _cli("futures", "paper", entry_side, kraken_sym, str(qty),
                            "--type", "market")
        if entry_result is None:
            logger.error(f"[KrakenPaper] market {entry_side} {symbol} failed")
            return None

        entry_order_id = self._parse_order_id(entry_result) or str(int(time.time() * 1000))
        fill_px = self.get_live_price(symbol) or 0.0

        # 2. SL bracket (CRITICAL — emergency close on failure)
        sl_cli_id = f"{entry_order_id}-sl"
        sl_order_id = self._place_stop_close(kraken_sym, close_side, qty,
                                             stop_loss, sl_cli_id)
        if sl_order_id is None:
            logger.critical(
                f"[KrakenPaper] 🚨 SL POST FAILED for {symbol} {position_side} "
                f"qty={qty} sl={stop_loss} — emergency close"
            )
            self._emergency_close(kraken_sym, close_side, qty, reason="sl_post_failed")
            return None

        # 3. TP bracket (non-critical — continue if fails, capital protected by SL)
        tp_cli_id = f"{entry_order_id}-tp"
        tp_order_id = self._place_tp_close(kraken_sym, close_side, qty,
                                           take_profit, tp_cli_id)
        tp_missing = tp_order_id is None
        if tp_missing:
            logger.warning(
                f"[KrakenPaper] TP post failed for {symbol} {position_side} "
                f"qty={qty} tp={take_profit} — continuing with SL only "
                f"(timeout 2h takes over for unrealized targets)"
            )

        # 4. Cache for reconciliation
        self._sltp[kraken_sym] = {
            "entry_order_id": entry_order_id,
            "side":           position_side,
            "qty":            float(qty),
            "fill_px":        float(fill_px),
            "sl":             float(stop_loss),
            "tp":             float(take_profit),
            "sl_order_id":    sl_order_id,
            "tp_order_id":    tp_order_id,
            "sl_cli_id":      sl_cli_id,
            "tp_cli_id":      tp_cli_id,
            "tp_missing":     tp_missing,
            "entry_ts_ms":    int(time.time() * 1000),
        }
        self._save_sltp_cache()

        # 5. Track entry order in memory
        self._orders[entry_order_id] = Order(
            entry_order_id, symbol, entry_side, "filled",
            fill_px, qty, qty, fill_px, type="market"
        )
        logger.info(
            f"[KrakenPaper] ✅ {position_side.upper()} {symbol} qty={qty} "
            f"~${fill_px:.4f} SL={stop_loss:.4f}(id={sl_order_id[:8] if sl_order_id else '-'}…) "
            f"TP={take_profit:.4f}(id={tp_order_id[:8] if tp_order_id else 'MISSING'}…) "
            f"native_brackets=True"
        )
        return entry_order_id

    # ── Entrées long / short ─────────────────────────────────────────────────

    def place_limit_buy(self, symbol: str, price: float,
                        stop_loss: float, take_profit: float,
                        deploy_usdt: float) -> str | None:
        """Long entry: market buy + SL stop reduce-only + TP take-profit reduce-only.
        On bracket failure, emergency closes the position to avoid naked exposure."""
        kraken_sym = _to_kraken(symbol)
        qty = _round_qty(kraken_sym, deploy_usdt / price)
        if qty < _MIN_QTY.get(kraken_sym, 0.01):
            logger.warning(f"[KrakenPaper] place_limit_buy {symbol}: qty trop petit ({qty})")
            return None
        return self._place_market_with_brackets(symbol, "buy", qty, stop_loss, take_profit)

    def place_limit_sell(self, symbol: str, price: float,
                         stop_loss: float, take_profit: float,
                         deploy_usdt: float) -> str | None:
        """Short entry: market sell + SL stop reduce-only + TP take-profit reduce-only."""
        kraken_sym = _to_kraken(symbol)
        qty = _round_qty(kraken_sym, deploy_usdt / price)
        if qty < _MIN_QTY.get(kraken_sym, 0.01):
            logger.warning(f"[KrakenPaper] place_limit_sell {symbol}: qty trop petit ({qty})")
            return None
        return self._place_market_with_brackets(symbol, "sell", qty, stop_loss, take_profit)

    def place_order(self, symbol: str, qty: float, side: str,
                    stop_loss: float = None, take_profit: float = None) -> str | None:
        """Generic order placement — used by some upstream paths."""
        kraken_sym = _to_kraken(symbol)
        kraken_qty = _round_qty(kraken_sym, qty)
        cli_side   = "buy" if side.lower() == "buy" else "sell"
        if stop_loss is not None and take_profit is not None:
            return self._place_market_with_brackets(symbol, cli_side, kraken_qty,
                                                    stop_loss, take_profit)
        # Bare market order (no brackets)
        result = _cli("futures", "paper", cli_side, kraken_sym, str(kraken_qty),
                      "--type", "market")
        if result is None:
            return None
        order_id = self._parse_order_id(result) or str(int(time.time() * 1000))
        fill_px  = self.get_live_price(symbol) or 0.0
        self._orders[order_id] = Order(order_id, symbol, cli_side, "filled",
                                       fill_px, kraken_qty, kraken_qty)
        logger.info(f"[KrakenPaper] ✅ {cli_side.upper()} {symbol} qty={kraken_qty} ~${fill_px:.4f}")
        return order_id

    # ── Fermetures et réconciliation ─────────────────────────────────────────

    def cancel_twin_brackets(self, symbol: str,
                             exclude_order_id: str | None = None) -> int:
        """Cancel cached SL/TP brackets for `symbol`, except `exclude_order_id`.
        Called after a native fill is detected (OCO), to prevent the surviving
        twin firing on a phantom position. Returns count of cancelled orders.
        Pops `_sltp[sym]` once both are gone."""
        kraken_sym = _to_kraken(symbol)
        cached = self._sltp.get(kraken_sym, {})
        cancelled = 0
        for k in ("sl_order_id", "tp_order_id"):
            oid = cached.get(k)
            if oid and oid != exclude_order_id:
                if self.cancel_order(symbol, oid):
                    cancelled += 1
                    cached[k] = None
        if not cached.get("sl_order_id") and not cached.get("tp_order_id"):
            self._sltp.pop(kraken_sym, None)
        self._save_sltp_cache()
        return cancelled

    def get_close_info(self, symbol: str, since_ts_ms: int = 0) -> dict | None:
        """Source of truth for SL/TP exits : poll broker fills, match against
        cached SL/TP order IDs.

        On match :
          - return {price, qty, reason} where reason = 'stop' or 'target'
          - cancel the sibling bracket (OCO)
          - clear `_sltp[sym]`
        On no match : return None (no exit yet).
        """
        kraken_sym = _to_kraken(symbol)
        cached = self._sltp.get(kraken_sym)
        if not cached:
            return None
        sl_oid = cached.get("sl_order_id")
        tp_oid = cached.get("tp_order_id")
        if not sl_oid and not tp_oid:
            return None

        fills = _cli("futures", "paper", "fills")
        if fills is None:
            return None
        if isinstance(fills, dict):
            fills = fills.get("fills", []) or []

        # Iterate fills from most-recent-first; find any fill whose order_id
        # matches our cached SL or TP order_id.
        for f in fills:
            fsym = (f.get("symbol") or "").upper()
            if fsym != kraken_sym:
                continue
            oid = f.get("order_id") or f.get("orderId") or f.get("id")
            if not oid:
                continue
            oid = str(oid)
            # Also accept client-order-id match (some CLI versions echo it)
            cli_oid = f.get("client_order_id") or f.get("cliOrdId")

            is_sl = (oid == sl_oid) or (cli_oid and cli_oid == cached.get("sl_cli_id"))
            is_tp = (oid == tp_oid) or (cli_oid and cli_oid == cached.get("tp_cli_id"))
            if not (is_sl or is_tp):
                continue

            # Timestamp filter — Kraken paper returns ISO string in `filled_at`,
            # other variants might return ms; handle both.
            ft_ms = 0
            iso = f.get("filled_at") or f.get("fill_time") or f.get("ts")
            if isinstance(iso, str):
                try:
                    from datetime import datetime
                    ft_ms = int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
                except Exception:
                    ft_ms = 0
            elif isinstance(iso, (int, float)):
                ft_ms = int(iso)
            if since_ts_ms and ft_ms and ft_ms < since_ts_ms:
                continue

            price = float(f.get("price", 0) or f.get("fill_price", 0) or 0)
            qty   = float(f.get("size", 0) or f.get("qty", 0) or cached.get("qty", 0))
            reason = "stop" if is_sl else "target"

            # OCO : cancel the sibling bracket
            keep = oid
            cancelled = self.cancel_twin_brackets(symbol, exclude_order_id=keep)
            if cancelled:
                logger.info(
                    f"[KrakenPaper] 🔗 OCO cancelled {cancelled} sibling bracket "
                    f"for {symbol} after {reason} fill"
                )
            # cache popped inside cancel_twin_brackets once both gone
            logger.info(
                f"[KrakenPaper] 🎯 close detected via {reason.upper()} fill "
                f"{symbol} @ ${price:.4f} qty={qty}"
            )
            return {"price": price, "qty": qty, "reason": reason}

        return None

    def close_position(self, symbol: str) -> bool:
        """Bot-initiated close (timeout, manual). Cancel brackets first (OCO),
        then market close. Used by manage_open_positions for the timeout path."""
        kraken_sym = _to_kraken(symbol)
        pos = self.get_position(symbol)
        if not pos or pos.qty <= 0:
            # Position already gone — just clean cache
            self._sltp.pop(kraken_sym, None)
            self._save_sltp_cache()
            return True
        # 1. Cancel both brackets (OCO logic on bot-side close)
        self.cancel_twin_brackets(symbol, exclude_order_id=None)
        # 2. Market close reduce-only
        close_side = "sell" if pos.side == "long" else "buy"
        result = _cli("futures", "paper", close_side, kraken_sym, str(pos.qty),
                      "--type", "market", "--reduce-only")
        # Cache cleanup
        self._sltp.pop(kraken_sym, None)
        self._save_sltp_cache()
        if result is not None:
            logger.info(f"[KrakenPaper] ✅ Closed {pos.side} {symbol} qty={pos.qty} (market reduce-only)")
            return True
        logger.error(f"[KrakenPaper] close_position {symbol} failed")
        return False
