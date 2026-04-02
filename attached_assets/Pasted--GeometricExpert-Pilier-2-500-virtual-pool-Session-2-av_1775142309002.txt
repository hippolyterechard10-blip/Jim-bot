"""
GeometricExpert — Pilier 2 ($500 virtual pool)
Session 2 avril 2026 — Réécriture 3 timeframes

Philosophie:
  PASS 1 — 1h  : bias directionnel (uptrend/downtrend/range)
  PASS 2 — 15min: niveau S/R qualité (testé ≥ 2×), nearest-first
  PASS 3 — 5min : confirmation (momentum vers niveau + RSI + volume + VWAP)
  PASS 4 — Stop : swing low/high 5min le plus récent, fallback -0.3%
  PASS 5 — Target: prochaine S/R 5min, fallback +0.5%

Ordre: limit au niveau → bracket pour stocks, watchdog pour crypto.
"""
import logging, json, uuid
import numpy as np
import config

logger = logging.getLogger(__name__)


def _smart_round(price: float) -> float:
    if price >= 100:      return round(price, 2)
    elif price >= 1:      return round(price, 4)
    elif price >= 0.01:   return round(price, 6)
    elif price >= 0.0001: return round(price, 8)
    else:                 return round(price, 10)


def _smart_decimals(price: float) -> int:
    if price >= 100:      return 2
    elif price >= 1:      return 4
    elif price >= 0.01:   return 6
    elif price >= 0.0001: return 8
    else:                 return 10


def _rsi(prices: np.ndarray, period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    deltas = np.diff(prices)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g  = gains[-period:].mean()
    avg_l  = losses[-period:].mean()
    if avg_l == 0:
        return 100.0
    return 100 - (100 / (1 + avg_g / avg_l))


class GeometricExpert:
    def __init__(self, broker, memory, geometry, regime, correlations):
        self.broker       = broker
        self.memory       = memory
        self.geometry     = geometry
        self.regime       = regime
        self.correlations = correlations
        self._candidates  = []
        self._high_water: dict = {}
        self._low_water:  dict = {}
        # Pending limit orders: symbol → {order_id, level, side, stop, target, qty, ...}
        self._pending_orders: dict = {}

    def add_candidate(self, symbol: str):
        if symbol not in self._candidates:
            self._candidates.append(symbol)

    def flush_candidates(self) -> list:
        c = list(self._candidates)
        self._candidates.clear()
        return c

    def _cancel_order(self, order_id: str | None, symbol: str = ""):
        if not order_id:
            return
        try:
            self.broker.api.cancel_order(order_id)
            logger.info(f"[GEO] 🛑 Cancelled order {order_id} for {symbol}")
        except Exception as e:
            logger.debug(f"[GEO] cancel_order {order_id}: {e}")

    # ── Capital ───────────────────────────────────────────────────────────────

    def get_deployed_capital(self) -> float:
        try:
            total = 0.0
            for t in self.memory.get_open_trades():
                ctx = t.get("market_context") or {}
                if isinstance(ctx, str):
                    try: ctx = json.loads(ctx)
                    except: ctx = {}
                if ctx.get("strategy_source") == "geometric":
                    total += float(t.get("entry_price", 0)) * float(t.get("qty", 0))
            return total
        except Exception as e:
            logger.error(f"[GEO] get_deployed_capital: {e}")
            return config.STRATEGY_CAPITAL["geometric"]

    def get_available_capital(self) -> float:
        try:
            import sqlite3
            base = config.STRATEGY_CAPITAL["geometric"]
            if not self.memory or not hasattr(self.memory, "db_path"):
                return max(0.0, base - self.get_deployed_capital())
            conn = sqlite3.connect(self.memory.db_path, timeout=5)
            row = conn.execute("""
                SELECT COALESCE(SUM(pnl), 0.0) FROM trades
                WHERE status = 'closed'
                  AND json_extract(market_context, '$.strategy_source') = 'geometric'
                  AND close_reason != 'synced_close'
            """).fetchone()
            conn.close()
            closed_pnl = float(row[0]) if row and row[0] is not None else 0.0
            return max(0.0, base + closed_pnl - self.get_deployed_capital())
        except Exception as e:
            logger.error(f"[GEO] get_available_capital: {e}")
            return max(0.0, config.STRATEGY_CAPITAL["geometric"] - self.get_deployed_capital())

    def has_capital(self) -> bool:
        return self.get_available_capital() >= 30.0

    # ── EVALUATE — cœur de la stratégie ──────────────────────────────────────

    def evaluate(self, symbol: str, size_modifier: float = 1.0, regime: str = "unknown"):
        from strategy import is_good_stock_window, is_crypto_good_hours

        is_crypto = "/" in symbol

        # Garde session
        if not is_crypto and not is_good_stock_window():
            return
        if is_crypto and not is_crypto_good_hours():
            return

        # Garde fermeture stocks (pas de nouvel ordre dans les 15 min avant 16h)
        if not is_crypto:
            import datetime as _dt
            try:
                import pytz as _pytz
                _ET = _pytz.timezone("America/New_York")
            except ImportError:
                import zoneinfo as _zi
                _ET = _zi.ZoneInfo("America/New_York")
            _now_et = _dt.datetime.now(_ET)
            if _now_et.hour == 15 and _now_et.minute >= 45:
                return

        # Garde double-entrée
        def _ctx(t):
            raw = t.get("market_context") or {}
            if isinstance(raw, str):
                try: return json.loads(raw)
                except: return {}
            return raw

        open_syms = {
            t["symbol"]
            for t in self.memory.get_open_trades()
            if _ctx(t).get("strategy_source") == "geometric"
        }
        if symbol in open_syms:
            logger.debug(f"[GEO] {symbol} already open → skip")
            return

        _regime = (regime or "unknown").lower()

        # ── PASS 1: Bias 1h ───────────────────────────────────────────────────
        bars_1h = self.broker.get_bars(symbol, "1Hour", limit=50)
        if bars_1h is None or bars_1h.empty or len(bars_1h) < 10:
            logger.debug(f"[GEO] {symbol} — pas de données 1h")
            return

        highs_1h  = bars_1h["high"].tolist()
        lows_1h   = bars_1h["low"].tolist()

        # Structure sur les 4 dernières bougies 1h
        hh = highs_1h[-1] > highs_1h[-4]
        hl = lows_1h[-1]  > lows_1h[-4]
        lh = highs_1h[-1] < highs_1h[-4]
        ll = lows_1h[-1]  < lows_1h[-4]

        if hh and hl:
            structure_1h  = "uptrend"
            allowed_sides = ["long"]
        elif lh and ll:
            structure_1h  = "downtrend"
            # Crypto: pas de short sur Alpaca spot
            allowed_sides = [] if is_crypto else ["short"]
        else:
            structure_1h  = "range"
            allowed_sides = ["long"] if is_crypto else ["long", "short"]

        # Filtre régime
        if _regime == "panic":
            allowed_sides = [s for s in allowed_sides if s != "long"]
        if _regime == "bear" and structure_1h != "range":
            allowed_sides = [s for s in allowed_sides if s != "long"]

        if not allowed_sides:
            logger.debug(f"[GEO] {symbol} — aucun side valide (structure={structure_1h} regime={_regime})")
            return

        # ── PASS 2: Niveau 15min ──────────────────────────────────────────────
        bars_15m = self.broker.get_bars(symbol, "15Min", limit=100)
        if bars_15m is None or bars_15m.empty or len(bars_15m) < 20:
            logger.debug(f"[GEO] {symbol} — pas de données 15min")
            return

        current_price = float(bars_15m["close"].iloc[-1])
        swing_15m     = self.geometry.find_swing_levels(bars_15m, min_tests=2)

        chosen_side  = None
        chosen_level = None

        for side in allowed_sides:
            candidates = swing_15m["supports"] if side == "long" else swing_15m["resistances"]
            for lvl_info in candidates:
                lvl  = lvl_info["level"]
                dist = (
                    (current_price - lvl) / current_price if side == "long"
                    else (lvl - current_price) / current_price
                )
                # Prix doit approcher (0.1% à 1.5% du niveau), pas déjà passé à travers
                if 0.001 <= dist <= 0.015:
                    chosen_side  = side
                    chosen_level = lvl
                    logger.info(
                        f"[GEO] {symbol} — niveau 15min trouvé: {side} @ {lvl:.6f} "
                        f"(dist={dist*100:.2f}% tests={lvl_info['tests']})"
                    )
                    break
            if chosen_level:
                break

        if not chosen_level:
            logger.debug(f"[GEO] {symbol} — aucun niveau 15min qualifié")
            return

        # ── PASS 3: Confirmation 5min ─────────────────────────────────────────
        bars_5m = self.broker.get_bars(symbol, "5Min", limit=30)
        if bars_5m is None or bars_5m.empty or len(bars_5m) < 10:
            logger.debug(f"[GEO] {symbol} — pas de données 5min")
            return

        closes_5m  = bars_5m["close"].tolist()
        volumes_5m = bars_5m["volume"].tolist()

        # 1. Momentum vers le niveau (prix se dirige vers le support/résistance)
        if len(closes_5m) >= 4:
            moving_toward = (
                (chosen_side == "long"  and closes_5m[-1] <= closes_5m[-4]) or
                (chosen_side == "short" and closes_5m[-1] >= closes_5m[-4])
            )
            if not moving_toward:
                logger.info(f"[GEO] {symbol} — prix ne se dirige pas vers le niveau, skip")
                return

        # 2. RSI 5min — ni rebond déjà amorcé, ni crash en cours
        rsi_5m = _rsi(np.array(closes_5m), 14)
        rsi_valid = (
            (chosen_side == "long"  and 25 <= rsi_5m <= 55) or
            (chosen_side == "short" and 45 <= rsi_5m <= 75)
        )
        if not rsi_valid:
            logger.info(f"[GEO] {symbol} — RSI5m={rsi_5m:.1f} hors plage pour {chosen_side}, skip")
            return

        # 3. Volume minimum — marché actif
        avg_vol = sum(volumes_5m[-20:]) / min(20, len(volumes_5m))
        if avg_vol > 0 and volumes_5m[-1] < avg_vol * 0.3:
            logger.info(f"[GEO] {symbol} — volume trop bas, skip")
            return

        # 4. VWAP confluence (bonus — pas bloquant)
        vwap           = self.geometry.calculate_vwap(bars_5m)
        vwap_conf      = vwap > 0 and abs(chosen_level - vwap) / vwap < 0.002
        if vwap_conf:
            logger.info(f"[GEO] {symbol} ✨ confluence VWAP @ {vwap:.6f}")

        # ── PASS 4: Stop ──────────────────────────────────────────────────────
        stop_price = self.geometry.find_5min_stop(bars_5m, chosen_side, chosen_level)

        # ── PASS 5: Target ────────────────────────────────────────────────────
        swing_5m     = self.geometry.find_swing_levels(bars_5m, min_tests=1)
        target_price = None

        res_candidates = swing_5m["resistances"] if chosen_side == "long" else swing_5m["supports"]
        for lvl_info in res_candidates:
            lvl      = lvl_info["level"]
            dist_pct = (
                (lvl - chosen_level) / chosen_level if chosen_side == "long"
                else (chosen_level - lvl) / chosen_level
            )
            # Target naturel entre +0.3% et +1.5% du niveau d'entrée
            if 0.003 <= dist_pct <= 0.015:
                target_price = lvl
                break

        # Fallback +0.5%
        if not target_price:
            target_price = _smart_round(
                chosen_level * 1.005 if chosen_side == "long" else chosen_level * 0.995
            )

        # Vérification R:R minimum 1.2:1
        risk   = abs(chosen_level - stop_price)
        reward = abs(float(target_price) - chosen_level)
        if risk <= 0 or reward / risk < 1.2:
            rr_val = round(reward / risk, 1) if risk > 0 else 0
            logger.info(f"[GEO] {symbol} — R:R={rr_val}x insuffisant, skip")
            return

        rr_val = round(reward / risk, 1)

        # ── Capital et sizing ─────────────────────────────────────────────────
        available = self.get_available_capital()
        if available < 30:
            logger.info(f"[GEO] {symbol} — capital insuffisant (${available:.0f})")
            return

        deploy = min(available, config.STRATEGY_CAPITAL["geometric"] * 0.28)
        deploy *= size_modifier
        if _regime == "bear":   deploy *= 0.6
        if _regime == "choppy": deploy *= 0.7

        qty = deploy / chosen_level
        if not is_crypto:
            qty = max(1, int(qty))
        else:
            qty = round(qty, 6)

        if qty * chosen_level < 20:
            logger.info(f"[GEO] {symbol} — position trop petite")
            return

        # ── Garde ordre pending doublon ────────────────────────────────────────
        if symbol in self._pending_orders:
            existing_level = self._pending_orders[symbol].get("level", 0)
            if existing_level and abs(existing_level - chosen_level) / chosen_level < 0.005:
                logger.debug(f"[GEO] {symbol} — pending déjà au niveau {existing_level:.6f}")
                return
            # Niveau changé → annuler l'ancien
            self._cancel_order(self._pending_orders[symbol].get("order_id"), symbol)
            del self._pending_orders[symbol]
            logger.info(f"[GEO] {symbol} — niveau déplacé, remplacement du pending")

        # ── Placement de l'ordre limit ────────────────────────────────────────
        _dec  = _smart_decimals(chosen_level)
        _pfmt = f".{_dec}f"

        logger.info(
            f"[GEO] 📋 LIMIT {chosen_side.upper()}: {symbol} @ {chosen_level:{_pfmt}} | "
            f"stop={stop_price:{_pfmt}} | target={float(target_price):{_pfmt}} | "
            f"R:R={rr_val}x | 1h={structure_1h} | RSI5m={rsi_5m:.0f} | "
            f"VWAP={'✨' if vwap_conf else '—'} | regime={_regime}"
        )

        order_id = None
        try:
            if not is_crypto:
                # Stocks: bracket complet (Alpaca gère stop + TP)
                order = self.broker.api.submit_order(
                    symbol=symbol,
                    qty=qty,
                    side="buy" if chosen_side == "long" else "sell",
                    type="limit",
                    limit_price=round(chosen_level, 2),
                    time_in_force="day",
                    order_class="bracket",
                    take_profit={"limit_price": round(float(target_price), 2)},
                    stop_loss={"stop_price": round(float(stop_price), 2)},
                )
            else:
                # Crypto: limit simple, stop/TP gérés par watchdog
                order = self.broker.api.submit_order(
                    symbol=symbol,
                    qty=qty,
                    side="buy",
                    type="limit",
                    limit_price=_smart_round(chosen_level),
                    time_in_force="gtc",
                )
            order_id = getattr(order, "id", None)
            logger.info(f"[GEO] ✅ Ordre placé: {symbol} id={order_id}")
        except Exception as e:
            logger.error(f"[GEO] Erreur ordre {symbol}: {e}")
            return

        # Enregistrer le pending
        self._pending_orders[symbol] = {
            "order_id":    order_id,
            "level":       chosen_level,
            "side":        chosen_side,
            "stop":        stop_price,
            "target":      float(target_price),
            "qty":         qty,
            "structure":   structure_1h,
            "is_crypto":   is_crypto,
            "regime":      _regime,
            "vwap_conf":   vwap_conf,
        }

        # Log dashboard Signals
        if self.memory:
            try:
                _rsn = (
                    f"GEO 3TF: {symbol} {chosen_side} @ {chosen_level:{_pfmt}} | "
                    f"stop={stop_price:{_pfmt}} target={float(target_price):{_pfmt}} R:R={rr_val}x\n"
                    f"1h={structure_1h} | RSI5m={rsi_5m:.0f} | "
                    f"VWAP={'confluence' if vwap_conf else 'absent'} | regime={_regime}\n"
                    f"{'BULL_MARKET' if structure_1h == 'uptrend' else 'CHOPPY'}"
                )
                self.memory.log_decision(
                    "BUY" if chosen_side == "long" else "SELL", _rsn,
                    symbol=symbol,
                    confidence=round(min(rr_val / 3.0, 1.0), 2),
                    market_data=json.dumps({
                        "level":        chosen_level,
                        "structure_1h": structure_1h,
                        "rsi_5m":       round(rsi_5m, 1),
                        "vwap_conf":    vwap_conf,
                        "rr":           rr_val,
                        "pending":      True,
                    })
                )
            except Exception as _le:
                logger.debug(f"[GEO] log_decision: {_le}")

    # ── Gestion des ordres pending ────────────────────────────────────────────

    def manage_pending_orders(self):
        """
        Appelé toutes les 30s depuis le fast loop.
        - Filled → enregistre le trade comme ouvert en DB
        - Annulé/expiré → retire du dict
        - Niveau cassé (prix traversé) → annule l'ordre
        """
        if not self._pending_orders:
            return

        for symbol in list(self._pending_orders.keys()):
            pending  = self._pending_orders[symbol]
            order_id = pending.get("order_id")
            level    = pending.get("level")
            side     = pending.get("side")
            is_crypto = pending.get("is_crypto", "/" in symbol)

            try:
                order  = self.broker.api.get_order(order_id)
                status = order.status

                if status == "filled":
                    fill_price = float(order.filled_avg_price or level)
                    qty        = float(order.filled_qty or pending["qty"])
                    logger.info(f"[GEO] ✅ FILLED: {symbol} {side} @ ${fill_price:.6f}")

                    if self.memory:
                        self.memory.log_trade_open(
                            trade_id=str(uuid.uuid4()),
                            symbol=symbol,
                            side="buy" if side == "long" else "sell",
                            qty=qty,
                            entry_price=fill_price,
                            stop_loss=pending["stop"],
                            take_profit=pending["target"],
                            alpaca_order_id=order_id,
                            market_context={
                                "strategy_source": "geometric",
                                "side":            side,
                                "level":           float(level),
                                "stop_pct":        round(abs(fill_price - pending["stop"]) / fill_price, 4),
                                "target_pct":      round(abs(pending["target"] - fill_price) / fill_price, 4),
                                "structure":       pending.get("structure", "unknown"),
                                "regime":          pending.get("regime", "unknown"),
                                "is_crypto":       is_crypto,
                                "vwap_conf":       pending.get("vwap_conf", False),
                                "order_type":      "bracket" if not is_crypto else "limit_manual",
                            }
                        )
                    del self._pending_orders[symbol]

                elif status in ("canceled", "expired", "rejected"):
                    logger.info(f"[GEO] 🗑 Ordre {status}: {symbol}")
                    del self._pending_orders[symbol]

                else:
                    # Vérifier si le niveau a été cassé
                    bars = self.broker.get_bars(symbol, "1Min", limit=3)
                    if bars is not None and not bars.empty:
                        current = float(bars["close"].iloc[-1])
                        broken  = (
                            (side == "long"  and current < level * 0.997) or
                            (side == "short" and current > level * 1.003)
                        )
                        if broken:
                            logger.info(
                                f"[GEO] 🚫 Niveau cassé {symbol} {side} "
                                f"@ {level:.6f} (current {current:.6f}) — annulation"
                            )
                            self._cancel_order(order_id, symbol)
                            del self._pending_orders[symbol]

            except Exception as e:
                logger.debug(f"[GEO] manage_pending {symbol}: {e}")

    # ── Gestion des positions ouvertes ────────────────────────────────────────

    def manage_open_positions(self):
        """
        Stocks avec bracket: Alpaca gère stop/TP automatiquement.
        Crypto: stop et target logiciels (watchdog).
        """
        try:
            positions   = self.broker.get_positions()
            open_trades = self.memory.get_open_trades()

            for pos in (positions or []):
                match    = None
                ctx_data = {}
                for t in open_trades:
                    ctx = t.get("market_context") or {}
                    if isinstance(ctx, str):
                        try: ctx = json.loads(ctx)
                        except: ctx = {}
                    if (ctx.get("strategy_source") == "geometric"
                            and t.get("symbol") == pos.symbol
                            and t.get("status") == "open"):
                        match    = t
                        ctx_data = ctx
                        break

                if not match:
                    continue

                symbol        = pos.symbol
                current_price = float(pos.current_price)
                entry_price   = float(match.get("entry_price", current_price))
                qty           = float(pos.qty)
                side          = ctx_data.get("side", "long")
                stop_price    = float(match.get("stop_loss")  or 0)
                target_price  = float(match.get("take_profit") or 0)
                is_crypto_pos = ctx_data.get("is_crypto", "/" in symbol)
                order_type    = ctx_data.get("order_type", "bracket")

                # Stocks bracket: Alpaca gère tout, on ne touche pas
                if not is_crypto_pos and order_type == "bracket":
                    continue

                # Crypto: stop/target logiciels
                if not stop_price or not target_price:
                    continue

                stop_hit   = (side == "long"  and current_price <= stop_price) or \
                             (side == "short" and current_price >= stop_price)
                target_hit = (side == "long"  and current_price >= target_price) or \
                             (side == "short" and current_price <= target_price)

                if stop_hit:
                    logger.info(f"[GEO] 🔴 STOP: {symbol} @ {current_price:.6f}")
                    self.broker.close_position(symbol)
                    if self.memory:
                        mult = 1 if side == "long" else -1
                        pnl  = (current_price - entry_price) * mult * qty
                        self.memory.log_trade_close(match["trade_id"], current_price, "stop", pnl=pnl)

                elif target_hit:
                    logger.info(f"[GEO] 💰 TARGET: {symbol} @ {current_price:.6f}")
                    self.broker.close_position(symbol)
                    if self.memory:
                        mult = 1 if side == "long" else -1
                        pnl  = (current_price - entry_price) * mult * qty
                        self.memory.log_trade_close(match["trade_id"], current_price, "target", pnl=pnl)

        except Exception as e:
            logger.error(f"[GEO] manage_open_positions: {e}")
