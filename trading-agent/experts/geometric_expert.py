"""
geometric_expert.py — Geo V4 ETH-Only
Stratégie validée backtest 2022-2025 :
  - Zones ±0.3% autour des pivots 15min
  - Limit order à zone["high"] (support × 1.003)
  - Stop dynamique sous wick réel
  - RSI divergence [20-65] + Pass 3b
  - Zone freshness MAX_TOUCHES=2
  - Target +0.9% fallback
"""
import logging, json, uuid
from collections import defaultdict
import numpy as np
import config

logger = logging.getLogger(__name__)


def _smart_round(price: float) -> float:
    if price >= 100:    return round(price, 2)
    elif price >= 1:    return round(price, 4)
    elif price >= 0.01: return round(price, 6)
    else:               return round(price, 8)


def _rsi(closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1: return 50.0
    d  = np.diff(closes.astype(float))
    g  = np.where(d > 0, d, 0.0)
    l  = np.where(d < 0, -d, 0.0)
    ag = g[-period:].mean(); al = l[-period:].mean()
    if al == 0: return 100.0
    return round(100 - 100 / (1 + ag / al), 2)


class GeometricExpert:

    def __init__(self, broker, memory, geometry, regime):
        self.broker   = broker
        self.memory   = memory
        self.geometry = geometry
        self.regime   = regime
        # Pending limit orders : zone_key → {order_id, level, stop, target, qty}
        self._pending: dict = {}
        # Zone touch tracking
        self._touches: defaultdict = defaultdict(int)

    # ── Capital ───────────────────────────────────────────────────────────────

    def get_deployed(self) -> float:
        try:
            total = 0.0
            for t in self.memory.get_open_trades():
                ctx = self._ctx(t)
                if ctx.get("strategy_source") == "geo_v4":
                    total += float(t.get("entry_price", 0)) * float(t.get("qty", 0))
            return total
        except Exception as e:
            logger.error(f"[GEO] get_deployed: {e}")
            return config.GEO_CAPITAL

    def get_available(self) -> float:
        try:
            import sqlite3
            conn = sqlite3.connect(self.memory.db_path, timeout=5)
            row  = conn.execute("""
                SELECT COALESCE(SUM(pnl), 0.0) FROM trades
                WHERE status = 'closed'
                  AND json_extract(market_context, '$.strategy_source') = 'geo_v4'
                  AND close_reason != 'synced_close'
            """).fetchone()
            conn.close()
            closed_pnl = float(row[0]) if row and row[0] is not None else 0.0
            return max(0.0, config.GEO_CAPITAL + closed_pnl - self.get_deployed())
        except Exception as e:
            logger.error(f"[GEO] get_available: {e}")
            return max(0.0, config.GEO_CAPITAL - self.get_deployed())

    def has_capital(self) -> bool:
        return self.get_available() >= 30.0

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _ctx(self, trade: dict) -> dict:
        raw = trade.get("market_context") or {}
        if isinstance(raw, str):
            try: return json.loads(raw)
            except: return {}
        return raw

    def _zone_key(self, center: float) -> float:
        mag = max(1, int(round(-np.log10(center * 0.001))))
        return round(center, mag)

    def _find_zones(self, highs, lows, closes, min_tests=1):
        current = closes[-1]
        sw_lows = []
        for i in range(2, len(highs) - 2):
            if (lows[i] < lows[i-1] and lows[i] < lows[i-2]
                    and lows[i] < lows[i+1] and lows[i] < lows[i+2]):
                sw_lows.append((lows[i], highs[i]))
        if not sw_lows: return []
        sw_lows.sort(key=lambda x: x[0])
        clusters = [[sw_lows[0]]]
        for v in sw_lows[1:]:
            if (v[0] - clusters[-1][0][0]) / clusters[-1][0][0] < config.GEO_ZONE_PCT * 2:
                clusters[-1].append(v)
            else:
                clusters.append([v])
        zones = []
        for c in clusters:
            center   = sum(x[0] for x in c) / len(c)
            wick_low = min(x[0] for x in c)
            if center < current * 0.999 and len(c) >= min_tests:
                zones.append({
                    "center":   center,
                    "high":     center * (1 + config.GEO_ZONE_PCT),
                    "low":      center * (1 - config.GEO_ZONE_PCT),
                    "wick_low": wick_low,
                    "tests":    len(c),
                })
        zones.sort(key=lambda x: x["center"], reverse=True)
        return zones

    def _rsi_divergence(self, closes, rsi_now) -> bool:
        if len(closes) < 5: return False
        rsi_prev    = _rsi(np.array(closes[:-3]), 14)
        price_lower = closes[-1] < closes[-4]
        rsi_higher  = rsi_now > rsi_prev
        return price_lower and rsi_higher

    def _dynamic_stop(self, lows_5m, entry_level, wick_low) -> float:
        floor     = entry_level * 0.992
        candidate = min(lows_5m[-8:]) * 0.999 if len(lows_5m) >= 8 else wick_low * 0.999
        if floor <= candidate < entry_level: return candidate
        zone_stop = wick_low * 0.999
        if floor <= zone_stop < entry_level: return zone_stop
        return entry_level * 0.997

    def _cancel_order(self, order_id: str | None, symbol: str = ""):
        if not order_id: return
        try:
            self.broker.api.cancel_order(order_id)
            logger.info(f"[GEO] 🛑 Cancelled {order_id}")
        except Exception as e:
            logger.debug(f"[GEO] cancel {order_id}: {e}")

    # ── EVALUATE ──────────────────────────────────────────────────────────────

    def evaluate(self, symbol: str = None, regime: str = "unknown"):
        symbol = symbol or config.GEO_SYMBOL
        logger.info(f"[GEO] evaluating {symbol} | régime={regime}")

        # Gate régime
        _r = (regime or "unknown").lower()
        if _r in ("bear", "panic"):
            logger.info(f"[GEO] 🔴 Régime {_r} — pas d'entrée")
            return

        # Double-entrée guard
        open_syms = {
            t["symbol"] for t in self.memory.get_open_trades()
            if self._ctx(t).get("strategy_source") == "geo_v4"
        }
        if len([s for s in open_syms if s == symbol]) >= config.GEO_MAX_SIM:
            logger.debug(f"[GEO] {symbol} — max_sim={config.GEO_MAX_SIM} atteint")
            return

        # Capital
        if not self.has_capital():
            logger.info(f"[GEO] Capital insuffisant (${self.get_available():.0f})")
            return

        # ── Pass 1 : Bias 1h ──────────────────────────────────────────────────
        bars_1h = self.broker.get_bars(symbol, "1Hour", limit=50)
        if bars_1h is None or bars_1h.empty or len(bars_1h) < 10:
            return
        h1h = bars_1h["high"].values; l1h = bars_1h["low"].values
        hh  = h1h[-1] > h1h[-4];     hl  = l1h[-1] > l1h[-4]
        lh  = h1h[-1] < h1h[-4];     ll  = l1h[-1] < l1h[-4]
        if lh and ll:
            logger.debug(f"[GEO] {symbol} — downtrend 1h, skip")
            return

        # ── Pass 2 : Zones 15min ──────────────────────────────────────────────
        bars_15m = self.broker.get_bars(symbol, "15Min", limit=100)
        if bars_15m is None or bars_15m.empty or len(bars_15m) < 20:
            return
        current = float(bars_15m["close"].iloc[-1])
        zones   = self._find_zones(
            bars_15m["high"].values, bars_15m["low"].values,
            bars_15m["close"].values, min_tests=1
        )

        # ── Pass 3 : Confirmation 5min ────────────────────────────────────────
        bars_5m = self.broker.get_bars(symbol, "5Min", limit=30)
        if bars_5m is None or bars_5m.empty or len(bars_5m) < 10:
            return
        closes_5m = bars_5m["close"].values
        vols_5m   = bars_5m["volume"].values
        rsi_now   = _rsi(closes_5m, 14)

        avg_vol = vols_5m[-20:].mean() if len(vols_5m) >= 20 else vols_5m.mean()
        if avg_vol > 0 and vols_5m[-1] < avg_vol * 0.3:
            logger.debug(f"[GEO] {symbol} — volume trop bas")
            return

        # Évaluer chaque zone
        open_count = len([t for t in self.memory.get_open_trades()
                          if self._ctx(t).get("strategy_source") == "geo_v4"])

        for zone in zones:
            if open_count >= config.GEO_MAX_SIM: break

            zk   = self._zone_key(zone["center"])
            dist = (current - zone["center"]) / current

            if not (0.001 <= dist <= 0.020): continue
            if self._touches[zk] >= config.GEO_MAX_TOUCHES: continue
            if zk in self._pending: continue

            # RSI divergence
            if not (config.GEO_RSI_LOW <= rsi_now <= config.GEO_RSI_HIGH): continue
            div = self._rsi_divergence(closes_5m, rsi_now)
            if not div and not (30 <= rsi_now <= 55): continue

            # Pass 3b — touché ET remonté
            touched      = any(bars_5m["low"].values[-8:] <= zone["high"])
            closed_above = closes_5m[-1] > zone["low"]
            if not (touched and closed_above): continue

            # Stop + target
            stop   = self._dynamic_stop(bars_5m["low"].values, zone["center"], zone["wick_low"])
            target = _smart_round(zone["high"] * (1 + config.GEO_TARGET_PCT))
            risk   = abs(zone["center"] - stop)
            reward = abs(target - zone["center"])
            if risk <= 0 or reward / risk < 1.2: continue

            # Sizing
            available = self.get_available()
            if available < 30: break
            deploy = min(available, config.GEO_CAPITAL * config.GEO_POS_PCT)
            qty    = round(deploy / zone["center"], 6)
            if qty * zone["center"] < 20: continue

            # Placement limit order
            limit_price = _smart_round(zone["high"])
            order_id    = None
            try:
                order    = self.broker.api.submit_order(
                    symbol=symbol, qty=qty, side="buy",
                    type="limit", limit_price=limit_price,
                    time_in_force="gtc",
                )
                order_id = getattr(order, "id", None)
                logger.info(
                    f"[GEO] 📋 LIMIT PLACED: {symbol} @ ${limit_price:.4f} | "
                    f"stop=${stop:.4f} | target=${target:.4f} | qty={qty} | "
                    f"RSI={rsi_now:.0f} | div={div} | zone_tests={zone['tests']}"
                )
            except Exception as e:
                logger.error(f"[GEO] order error {symbol}: {e}")
                continue

            self._touches[zk] += 1
            self._pending[zk] = {
                "order_id": order_id, "level": zone["center"],
                "high": zone["high"], "stop": stop,
                "target": target, "qty": qty, "symbol": symbol,
            }

            # Log dashboard
            if self.memory:
                try:
                    self.memory.log_decision(
                        "BUY",
                        f"GEO V4: {symbol} LIMIT @ ${limit_price:.4f} | "
                        f"stop=${stop:.4f} target=${target:.4f} R:R={round(reward/risk,1)}x | "
                        f"regime={_r} RSI={rsi_now:.0f}",
                        symbol=symbol,
                        confidence=round(min(zone["tests"] / 5.0, 1.0), 2),
                        market_data=json.dumps({
                            "zone_center": zone["center"],
                            "zone_tests":  zone["tests"],
                            "rsi":         rsi_now,
                            "divergence":  div,
                        })
                    )
                except Exception as _le:
                    logger.debug(f"[GEO] log_decision: {_le}")

            open_count += 1

    # ── MANAGE PENDING ────────────────────────────────────────────────────────

    def manage_pending_orders(self):
        """Appelé toutes les 30s. Détecte les fills et annule les ordres si niveau cassé."""
        for zk in list(self._pending.keys()):
            p = self._pending[zk]
            try:
                order  = self.broker.api.get_order(p["order_id"])
                status = order.status

                if status == "filled":
                    fill = float(order.filled_avg_price or p["level"])
                    qty  = float(order.filled_qty or p["qty"])
                    logger.info(f"[GEO] ✅ FILLED: {p['symbol']} @ ${fill:.4f}")
                    if self.memory:
                        self.memory.log_trade_open(
                            trade_id=str(uuid.uuid4()),
                            symbol=p["symbol"], side="buy",
                            qty=qty, entry_price=fill,
                            stop_loss=p["stop"], take_profit=p["target"],
                            alpaca_order_id=p["order_id"],
                            market_context={
                                "strategy_source": "geo_v4",
                                "side":   "long",
                                "level":  float(p["level"]),
                                "stop":   float(p["stop"]),
                                "target": float(p["target"]),
                            }
                        )
                    del self._pending[zk]

                elif status in ("canceled", "expired", "rejected"):
                    logger.info(f"[GEO] 🗑 Order {status}: {p['symbol']}")
                    del self._pending[zk]

                else:
                    # Vérifier si niveau cassé
                    bars = self.broker.get_bars(p["symbol"], "1Min", limit=3)
                    if bars is not None and not bars.empty:
                        curr = float(bars["close"].iloc[-1])
                        if curr < p["level"] * 0.997:
                            logger.info(f"[GEO] 🚫 Niveau cassé {p['symbol']} — annulation")
                            self._cancel_order(p["order_id"], p["symbol"])
                            del self._pending[zk]

            except Exception as e:
                logger.debug(f"[GEO] manage_pending {p.get('symbol')}: {e}")

    # ── MANAGE POSITIONS ──────────────────────────────────────────────────────

    def manage_open_positions(self):
        """Stop -0.3% et target +0.9% logiciels pour crypto (pas de bracket Alpaca)."""
        try:
            positions   = self.broker.get_positions()
            open_trades = self.memory.get_open_trades()

            for pos in (positions or []):
                match    = None
                ctx_data = {}
                for t in open_trades:
                    ctx = self._ctx(t)
                    if (ctx.get("strategy_source") == "geo_v4"
                            and t.get("symbol") == pos.symbol
                            and t.get("status") == "open"):
                        match    = t
                        ctx_data = ctx
                        break
                if not match: continue

                current      = float(pos.current_price)
                entry        = float(match.get("entry_price", current))
                qty          = float(pos.qty)
                stop_price   = float(match.get("stop_loss")  or 0)
                target_price = float(match.get("take_profit") or 0)
                symbol       = pos.symbol

                if not stop_price or not target_price: continue

                stop_hit   = current <= stop_price
                target_hit = current >= target_price

                if stop_hit:
                    logger.info(f"[GEO] 🔴 STOP: {symbol} @ ${current:.4f}")
                    self.broker.close_position(symbol)
                    if self.memory:
                        pnl = (current - entry) * qty
                        self.memory.log_trade_close(
                            match["trade_id"], current, "stop", pnl=pnl)

                elif target_hit:
                    logger.info(f"[GEO] 💰 TARGET: {symbol} @ ${current:.4f}")
                    self.broker.close_position(symbol)
                    if self.memory:
                        pnl = (current - entry) * qty
                        self.memory.log_trade_close(
                            match["trade_id"], current, "target", pnl=pnl)

        except Exception as e:
            logger.error(f"[GEO] manage_open_positions: {e}")
