"""
GeometricExpert — Pilier 2 ($500 virtual pool)
Metrics: Confluence score (1-5), Market structure, Timeframe alignment, RSI divergence.
Stop: 1x ATR below level. Entry: rejection candle breakout.
"""
import logging, json
import config

logger = logging.getLogger(__name__)

class GeometricExpert:
    def __init__(self, broker, memory, geometry, regime, correlations):
        self.broker = broker
        self.memory = memory
        self.geometry = geometry
        self.regime = regime
        self.correlations = correlations
        self._candidates = []
        self._high_water: dict = {}
        self._low_water: dict = {}

    def add_candidate(self, symbol: str):
        if symbol not in self._candidates:
            self._candidates.append(symbol)

    def flush_candidates(self) -> list:
        c = list(self._candidates)
        self._candidates.clear()
        return c

    def _cancel_stop_order(self, stop_order_id: str | None, symbol: str = ""):
        """Cancel an Alpaca stop order before placing a closing order."""
        if not stop_order_id:
            return
        try:
            self.broker.api.cancel_order(stop_order_id)
            logger.info(f"[GEO] 🛑 Cancelled stop order {stop_order_id} for {symbol}")
        except Exception as _ce:
            logger.debug(f"[GEO] cancel_order {stop_order_id} for {symbol}: {_ce}")

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
            logger.error(f"GeometricExpert.get_deployed_capital: {e}")
            return config.STRATEGY_CAPITAL["geometric"]

    def get_available_capital(self) -> float:
        try:
            import sqlite3
            base = config.STRATEGY_CAPITAL["geometric"]
            if not self.memory or not hasattr(self.memory, 'db_path'):
                return max(0.0, base - self.get_deployed_capital())
            conn = sqlite3.connect(self.memory.db_path, timeout=5)
            row = conn.execute("""
                SELECT COALESCE(SUM(pnl), 0.0) FROM trades
                WHERE status = 'closed'
                  AND json_extract(market_context, '$.strategy_source') = 'geometric'
                  AND close_reason != 'synced_close'
                  AND (json_extract(market_context, '$.source') IS NULL
                       OR json_extract(market_context, '$.source') NOT IN
                          ('order_sync', 'order_sync_synthetic'))
            """).fetchone()
            conn.close()
            closed_pnl = float(row[0]) if row and row[0] is not None else 0.0
            pool = base + closed_pnl
            return max(0.0, pool - self.get_deployed_capital())
        except Exception as e:
            logger.error(f"GeometricExpert.get_available_capital: {e}")
            return max(0.0, config.STRATEGY_CAPITAL["geometric"] - self.get_deployed_capital())

    def has_capital(self) -> bool:
        return self.get_available_capital() >= 50.0

    def get_capital_pct_for_setup(self, confluence_score: int) -> float:
        """Capital deployed based on setup quality."""
        if confluence_score >= 5: return 0.95
        if confluence_score >= 4: return 0.90
        return 0.80

    def evaluate(self, symbol: str, size_modifier: float = 1.0):
        import uuid
        from strategy import compute_indicators, is_good_stock_window, is_crypto_good_hours

        is_crypto = "/" in symbol

        # Session check
        if not is_crypto and not is_good_stock_window():
            return
        if is_crypto and not is_crypto_good_hours():
            return

        # Stocks: block entries within 10 minutes of 16:00 ET close
        if not is_crypto:
            import datetime as _dt
            try:
                import pytz as _pytz
                _ET = _pytz.timezone("America/New_York")
            except ImportError:
                import zoneinfo as _zi
                _ET = _zi.ZoneInfo("America/New_York")
            _now_et = _dt.datetime.now(_ET)
            if _now_et.hour == 15 and _now_et.minute >= 50:
                logger.info(f"[GEO] {symbol} — within 10 min of close (15:{_now_et.minute} ET), skip")
                return

        logger.info(f"[GEO] ✅ Session OK: {symbol}, evaluating...")
        logger.info(f"[GEO] 🔍 Evaluating {symbol} — crypto={is_crypto}")

        # Double-entry guard — block re-entry on already-open geometric position
        # Layer 1: DB check
        def _ctx(t):
            raw = t.get("market_context") or {}
            if isinstance(raw, str):
                try:
                    import json as _json
                    return _json.loads(raw)
                except Exception:
                    return {}
            return raw
        open_syms = {
            t["symbol"]
            for t in self.memory.get_open_trades()
            if _ctx(t).get("strategy_source") == "geometric"
        }
        if symbol in open_syms:
            logger.debug(f"[GEO GUARD] {symbol} already open (DB) → skip")
            return

        # Layer 2: Live Alpaca positions check (catches cases where log_trade_open failed)
        if not is_crypto:
            try:
                live_pos_syms = {p.symbol for p in (self.broker.get_positions() or [])}
                if symbol in live_pos_syms:
                    logger.info(f"[GEO GUARD] {symbol} already open (Alpaca live) → skip")
                    return
            except Exception as _pe:
                logger.debug(f"[GEO GUARD] Alpaca position check error: {_pe}")

        # Get bars — 1-min for entry, 1-hour for structure
        bars_1m = self.broker.get_bars(symbol, "1Min", limit=50)
        logger.info(f"[GEO] bars_1m: {'OK' if bars_1m is not None and not bars_1m.empty else 'NONE/EMPTY'} for {symbol}")
        if bars_1m is None or bars_1m.empty or len(bars_1m) < 20:
            return

        try:
            closes_1m  = bars_1m["close"].tolist()
            highs_1m   = bars_1m["high"].tolist()
            lows_1m    = bars_1m["low"].tolist()
            opens_1m   = bars_1m["open"].tolist()
            volumes_1m = bars_1m["volume"].tolist()
            current_price = closes_1m[-1]

            # STEP 1 — Support/Resistance via geometry.py
            sr = self.geometry.find_support_resistance(closes_1m, highs_1m, lows_1m)
            nearest_support    = sr["nearest_support"]
            nearest_resistance = sr["nearest_resistance"]
            support_score      = sr["support_score"]
            resistance_score   = sr["resistance_score"]

            # Determine which level we're near (within 1.5%)
            dist_to_support    = (current_price - nearest_support) / current_price
            dist_to_resistance = (nearest_resistance - current_price) / current_price

            logger.info(f"[GEO] SR levels: support={sr.get('nearest_support', 0):.2f} resistance={sr.get('nearest_resistance', 0):.2f} dist_sup={dist_to_support:.3f} dist_res={dist_to_resistance:.3f}")
            logger.info(f"[GEO] {symbol} dist_sup={dist_to_support:.3f} dist_res={dist_to_resistance:.3f} (threshold 0.015)")

            if dist_to_support <= 0.015:
                side        = "long"
                level       = nearest_support
                level_score = support_score
            elif dist_to_resistance <= 0.015:
                side        = "short"
                level       = nearest_resistance
                level_score = resistance_score
            else:
                logger.info(f"[GEO] {symbol} — not near any key level, skip")
                return

            # Crypto shorts not supported on Alpaca spot
            if side == "short" and is_crypto:
                logger.debug(f"[GEO] {symbol} — crypto short not supported on Alpaca spot, skip")
                return

            # STEP 2 — Confluence score (1-5)
            confluence = 0

            # Factor 1: Level tested 2+ times
            if level_score >= 2:
                confluence += 1

            # Factor 2: Round number
            magnitude = 10 ** max(0, len(str(int(level))) - 2)
            if abs(level % magnitude) < magnitude * 0.02:
                confluence += 1

            # Factor 3: Near SMA20
            from numpy import mean as np_mean
            sma20 = float(np_mean(closes_1m[-20:]))
            if abs(level - sma20) / sma20 < 0.005:
                confluence += 1

            # Factor 4: High volume ratio recently
            indicators = compute_indicators(closes_1m, volumes_1m)
            if indicators.get("volume_ratio", 1) > 1.5:
                confluence += 1

            # Factor 5: Swing high/low (already captured in level_score)
            if level_score >= 3:
                confluence += 1

            if confluence < 3:
                logger.info(f"[GEO] {symbol} — confluence {confluence}/5 too low, skip")
                return

            # Level exhaustion check
            if level_score >= 6:
                logger.info(f"[GEO] {symbol} — level exhausted ({level_score} tests), skip")
                return

            # STEP 3 — RSI divergence check (+2 bonus)
            from strategy import _rsi
            import numpy as np
            prices_arr = np.array(closes_1m)
            rsi_now  = _rsi(prices_arr, 14)
            logger.info(f"[GEO] RSI check OK: rsi_now={rsi_now:.1f}")
            rsi_prev = _rsi(prices_arr[:-10], 14) if len(prices_arr) > 24 else rsi_now

            price_lower    = closes_1m[-1] < closes_1m[-10]
            rsi_higher     = rsi_now > rsi_prev
            rsi_divergence = (side == "long" and price_lower and rsi_higher)

            if rsi_divergence:
                confluence = min(confluence + 2, 7)
                logger.info(f"[GEO] {symbol} — RSI divergence detected! confluence now {confluence}")

            # STEP 4 — Market structure (1h bars)
            bars_1h = self.broker.get_bars(symbol, "1Hour", limit=20)
            if bars_1h is not None and not bars_1h.empty and len(bars_1h) >= 5:
                closes_1h = bars_1h["close"].tolist()
                highs_1h  = bars_1h["high"].tolist()
                lows_1h   = bars_1h["low"].tolist()

                hh = highs_1h[-1] > highs_1h[-3]
                hl = lows_1h[-1]  > lows_1h[-3]
                lh = highs_1h[-1] < highs_1h[-3]
                ll = lows_1h[-1]  < lows_1h[-3]

                if hh and hl:
                    structure = "uptrend"
                elif lh and ll:
                    structure = "downtrend"
                else:
                    structure = "range"

                if structure == "uptrend" and side == "short":
                    logger.info(f"[GEO] {symbol} — uptrend, skipping short")
                    return
                if structure == "downtrend" and side == "long" and not rsi_divergence:
                    logger.info(f"[GEO] {symbol} — downtrend without RSI divergence, skip long")
                    return
                if structure == "downtrend" and side == "long" and rsi_divergence:
                    logger.info(f"[GEO] {symbol} — downtrend BUT RSI divergence confirmed → allowing counter-trend long")
            else:
                structure = "unknown"

            # STEP 5 — Rejection candle at the level
            candles = self.geometry.detect_candlestick_patterns(
                opens_1m, highs_1m, lows_1m, closes_1m, volumes_1m
            )
            bullish_candles = {"HAMMER", "BULLISH_ENGULFING", "THREE_WHITE_SOLDIERS", "PIN_BAR"}
            bearish_candles = {"SHOOTING_STAR", "BEARISH_ENGULFING", "THREE_BLACK_CROWS"}
            detected_names  = {p["name"] for p in candles["patterns"]}

            if side == "long" and not detected_names.intersection(bullish_candles):
                if confluence >= 5:
                    logger.info(f"[GEO] {symbol} — no bullish candle BUT confluence={confluence} (RSI divergence) → proceeding without candle confirmation")
                else:
                    logger.info(f"[GEO] {symbol} — no bullish candle + confluence={confluence}<5, skip")
                    return
            if side == "short" and not detected_names.intersection(bearish_candles):
                logger.info(f"[GEO] {symbol} — no bearish candle, skip")
                return

            # STEP 6 — Level-based stop (the level holds or it doesn't — geometric principle)
            # ATR on 1-min bars produces 5%+ wide stops; instead anchor to the level itself.
            atr = self.geometry.calculate_atr(highs_1m, lows_1m, closes_1m, period=14)
            is_crypto = "/" in symbol
            if side == "long":
                # 0.5% below level for crypto, 0.3% below for stocks
                stop_price   = round(level * 0.995, 4) if is_crypto else round(level * 0.997, 4)
                target_price = round(nearest_resistance - (nearest_resistance - nearest_support) * 0.1, 6)
            else:
                # 0.5% above level for crypto, 0.3% above for stocks
                stop_price   = round(level * 1.005, 4) if is_crypto else round(level * 1.003, 4)
                target_price = round(nearest_support + (nearest_resistance - nearest_support) * 0.1, 6)

            # R:R check — minimum 1:2
            risk   = abs(current_price - stop_price)
            reward = abs(target_price - current_price)
            if risk <= 0 or reward / risk < 2.0:
                rr_val = round(reward / risk, 1) if risk > 0 else 0
                logger.info(f"[GEO] {symbol} — R:R too low ({rr_val}x), skip")
                # Log WATCH signal when confluence is strong despite bad R:R
                if confluence >= 5 and self.memory:
                    try:
                        _base = min(int(confluence * 14), 70)
                        _geo  = int(confluence * 4)
                        _radj = +5 if structure in ("range", "uptrend") else -5
                        _final = min(100, _base + max(0, _radj) + _geo)
                        _regime_word = "CHOPPY" if _radj < 0 else ("BULL_MARKET" if structure == "uptrend" else "CHOPPY")
                        _rsn = (
                            f"GEO V2 WATCH: {symbol} {side} | structure={structure} | R:R={rr_val}x (below 2:1 threshold)\n"
                            f"Breakdown: Base: {_base} | Regime: {_radj:+d} | RelStr: 0 | DXY: 0 | Corr: 0 | Geo: {_geo} | News: 0 | FINAL: {_final}\n"
                            f"{_regime_word}"
                        )
                        _md = json.dumps({"patterns_detected": sorted(detected_names), "structure": structure, "confluence": confluence, "rr": rr_val})
                        self.memory.log_decision("WATCH", _rsn, symbol=symbol, confidence=round(min(confluence / 5.0, 1.0), 2), market_data=_md)
                    except Exception as _le:
                        logger.debug(f"[GEO] log_decision (watch) error: {_le}")
                return

            # STEP 7 — Correlation check (no 2 correlated positions)
            open_trades = self.memory.get_open_trades()
            geo_open = []
            for t in open_trades:
                ctx = t.get("market_context") or {}
                if isinstance(ctx, str):
                    try: ctx = json.loads(ctx)
                    except: ctx = {}
                if ctx.get("strategy_source") == "geometric":
                    geo_open.append(t.get("symbol", ""))

            corr_check = self.correlations.check_correlation_conflict(symbol, geo_open)
            if corr_check.get("conflict") and corr_check.get("score_adjustment", 0) <= -20:
                logger.info(f"[GEO] {symbol} — correlation conflict with open positions, skip")
                return

            # STEP 8 — Position sizing
            available = self.get_available_capital()
            if available < 50:
                logger.info(f"[GEO] {symbol} — no capital available (${available:.0f})")
                return

            deploy_pct     = self.get_capital_pct_for_setup(min(confluence, 5))
            max_capital    = config.STRATEGY_CAPITAL["geometric"] * deploy_pct
            position_pct   = 0.28  # 28% of pool per position
            capital_to_use = min(available, config.STRATEGY_CAPITAL["geometric"] * position_pct)
            capital_to_use = min(capital_to_use, max_capital - self.get_deployed_capital())
            # Apply calendar event size reduction if active
            capital_to_use *= size_modifier
            if size_modifier < 1.0:
                logger.info(f"[GEO] 📅 Calendar modifier ×{size_modifier:.2f} → capital=${capital_to_use:.0f}")

            if capital_to_use < 30:
                logger.info(f"[GEO] {symbol} — capital deployment limit reached")
                return

            qty = capital_to_use / current_price
            if "/" not in symbol:
                qty = max(1, int(qty))   # whole shares for stocks
            else:
                qty = round(qty, 6)      # fractional for crypto

            logger.info(
                f"[GEO] 📐 ENTERING: {symbol} {side.upper()} | "
                f"level=${level:.4f} | confluence={confluence}/5 | "
                f"stop=${stop_price:.4f} | target=${target_price:.4f} | "
                f"R:R={reward/risk:.1f}x | structure={structure} | "
                f"candles={detected_names} | capital=${capital_to_use:.0f}"
            )

            # Log decision to populate Signals page on the dashboard
            if self.memory:
                try:
                    _rr     = round(reward / risk, 1)
                    _base   = min(int(confluence * 14), 70)
                    _geo    = int(confluence * 4)
                    _radj   = +5 if structure in ("range", "uptrend") else -5
                    _final  = min(100, _base + max(0, _radj) + _geo)
                    _regime_word = "BULL_MARKET" if structure == "uptrend" else ("BEAR_MARKET" if structure == "downtrend" else "CHOPPY")
                    _rsn = (
                        f"GEO V2: {symbol} {side} | structure={structure} | R:R={_rr}x | capital=${capital_to_use:.0f}\n"
                        f"Breakdown: Base: {_base} | Regime: {_radj:+d} | RelStr: 0 | DXY: 0 | Corr: 0 | Geo: {_geo} | News: 0 | FINAL: {_final}\n"
                        f"{_regime_word}"
                    )
                    _md = json.dumps({"patterns_detected": sorted(detected_names), "structure": structure, "confluence": confluence, "rr": _rr})
                    _decision = "BUY" if side == "long" else "SELL"
                    self.memory.log_decision(_decision, _rsn, symbol=symbol, confidence=round(min(confluence / 5.0, 1.0), 2), market_data=_md)
                except Exception as _le:
                    logger.debug(f"[GEO] log_decision error: {_le}")

            order = self.broker.place_order(
                symbol, qty,
                "buy" if side == "long" else "sell",
            )

            if order and self.memory:
                # Place a real Alpaca stop order immediately after entry
                stop_order_id = None
                try:
                    _stop_side = "sell" if side == "long" else "buy"
                    _sp = round(stop_price, 4) if is_crypto else round(stop_price, 2)
                    _stop_ord = self.broker.api.submit_order(
                        symbol=symbol, qty=qty, side=_stop_side,
                        type="stop", stop_price=_sp, time_in_force="gtc",
                    )
                    stop_order_id = getattr(_stop_ord, "id", None)
                    logger.info(f"[GEO] 🛑 Stop order placed: {symbol} stop=${_sp} id={stop_order_id}")
                except Exception as _se:
                    logger.error(f"[GEO] stop order error for {symbol}: {_se}")

                self.memory.log_trade_open(
                    trade_id=str(uuid.uuid4()),
                    symbol=symbol,
                    side="buy" if side == "long" else "sell",
                    qty=qty,
                    entry_price=current_price,
                    stop_loss=stop_price,
                    take_profit=target_price,
                    alpaca_order_id=getattr(order, "id", None),
                    market_context={
                        "strategy_source": "geometric",
                        "side": side,
                        "level": float(level),
                        "confluence": int(confluence),
                        "structure": structure,
                        "atr": float(atr),
                        "target_midpoint": float(target_price),
                        "rsi_divergence": bool(rsi_divergence),
                        "patterns": list(detected_names),
                        "alpaca_stop_order_id": stop_order_id,
                    }
                )

        except Exception as e:
            logger.error(f"[GEO] evaluate({symbol}) error: {e}")

    def manage_open_positions(self):
        """Manage trailing stop for geometric positions."""
        try:
            positions = self.broker.get_positions()
            open_trades = self.memory.get_open_trades()

            for pos in (positions or []):
                match = None
                ctx_data = {}
                for t in open_trades:
                    ctx = t.get("market_context") or {}
                    if isinstance(ctx, str):
                        try: ctx = json.loads(ctx)
                        except: ctx = {}
                    if (ctx.get("strategy_source") == "geometric"
                            and t.get("symbol") == pos.symbol
                            and t.get("status") == "open"):
                        match = t
                        ctx_data = ctx
                        break

                if not match:
                    continue

                current_price = float(pos.current_price)
                entry_price   = float(match.get("entry_price", current_price))
                qty           = float(pos.qty)
                side          = ctx_data.get("side", "long")
                target_mid    = ctx_data.get("target_midpoint")
                partial_taken = ctx_data.get("partial_taken", False)
                symbol        = pos.symbol

                if side == "long":
                    gain_pct = (current_price - entry_price) / entry_price * 100
                else:
                    gain_pct = (entry_price - current_price) / entry_price * 100

                stop_order_id = ctx_data.get("alpaca_stop_order_id")
                _is_crypto = "/" in symbol

                # ── Thesis invalidation: level broken ─────────────────────────────────
                # "The level holds or it doesn't." Exit if the 5-min close bar has moved
                # THROUGH the entry level, but only after 15-min minimum hold and only if
                # the trade has not already moved more than 50% toward the target (in which
                # case the trailing stop manages it).
                _level = ctx_data.get("level")
                _entry_at_str = match.get("entry_at") or match.get("created_at")
                if _level and _entry_at_str and not partial_taken:
                    try:
                        from datetime import datetime, timezone as _tz
                        _entry_dt = datetime.fromisoformat(str(_entry_at_str).replace("Z", "+00:00"))
                        _hold_min = (datetime.now(_tz.utc) - _entry_dt).total_seconds() / 60
                    except Exception:
                        _hold_min = 99  # can't parse → assume old enough

                    if _hold_min >= 15:
                        # Skip if already past 50% of target distance (trade is working)
                        _target_dist = abs(target_mid - entry_price) if target_mid else None
                        _unrealized = abs(current_price - entry_price)
                        _toward_target = (
                            (side == "long" and current_price > entry_price) or
                            (side == "short" and current_price < entry_price)
                        )
                        _well_in_profit = (
                            _target_dist and _toward_target and
                            _unrealized > _target_dist * 0.50
                        )

                        if not _well_in_profit:
                            # Fetch the latest 5-min bar close for confirmation
                            _bars5 = self.broker.get_bars(symbol, "5Min", limit=2)
                            if _bars5 is not None and not _bars5.empty:
                                _close5 = float(_bars5["close"].iloc[-1])
                                _broken = (
                                    (side == "long" and _close5 < _level) or
                                    (side == "short" and _close5 > _level)
                                )
                                if _broken:
                                    _dir = "below support" if side == "long" else "above resistance"
                                    logger.info(
                                        f"[GEO] 🔴 LEVEL BROKEN: {symbol} closed ${_close5:.4f} "
                                        f"{_dir} ${_level:.4f} — thesis invalid, exiting"
                                    )
                                    self._cancel_stop_order(stop_order_id, symbol)
                                    self.broker.close_position(symbol)
                                    if self.memory:
                                        _pnl = (current_price - entry_price) * qty * (
                                            1 if side == "long" else -1)
                                        self.memory.log_trade_close(
                                            match["trade_id"], current_price,
                                            "level_broken", pnl=_pnl
                                        )
                                    continue

                # Partial at midpoint target → sell 50%, cancel old stop, re-place at breakeven
                if target_mid and not partial_taken:
                    if (side == "long" and current_price >= target_mid) or \
                       (side == "short" and current_price <= target_mid):
                        sell_qty = max(1, int(abs(qty) * 0.50)) if not _is_crypto else round(abs(qty) * 0.50, 6)
                        logger.info(f"[GEO] 💰 PARTIAL: {symbol} reached midpoint target ${target_mid:.4f}")
                        self._cancel_stop_order(stop_order_id, symbol)
                        self.broker.place_order(symbol, sell_qty, "sell" if side == "long" else "buy")
                        if self.memory:
                            pnl = (current_price - entry_price) * sell_qty * (1 if side == "long" else -1)
                            self.memory.log_trade_close(match["trade_id"], current_price, "partial_geo", pnl=pnl)
                            remaining = abs(qty) - sell_qty
                            if remaining > 0:
                                import uuid
                                # Re-place stop at breakeven for the remaining position
                                new_stop_id = None
                                try:
                                    _be = round(entry_price * (0.995 if _is_crypto else 0.997), 4 if _is_crypto else 2)
                                    _stop_side = "sell" if side == "long" else "buy"
                                    _ord = self.broker.api.submit_order(
                                        symbol=symbol, qty=remaining, side=_stop_side,
                                        type="stop", stop_price=_be, time_in_force="gtc",
                                    )
                                    new_stop_id = getattr(_ord, "id", None)
                                    logger.info(f"[GEO] 🛑 Breakeven stop placed for remaining {remaining} {symbol} @ ${_be} id={new_stop_id}")
                                except Exception as _rse:
                                    logger.error(f"[GEO] replace stop after partial error: {_rse}")
                                self.memory.log_trade_open(
                                    trade_id=str(uuid.uuid4()),
                                    symbol=symbol, side=match.get("side"),
                                    qty=remaining, entry_price=entry_price,
                                    market_context={**ctx_data, "partial_taken": True, "alpaca_stop_order_id": new_stop_id}
                                )

                # Trailing stop -1% from best price (after partial)
                if partial_taken:
                    if side == "long":
                        if symbol not in self._high_water:
                            self._high_water[symbol] = current_price
                        self._high_water[symbol] = max(self._high_water[symbol], current_price)
                        trail_stop = self._high_water[symbol] * 0.99
                        if current_price <= trail_stop:
                            logger.info(f"[GEO] 🔴 TRAIL STOP (long): {symbol} ${current_price:.4f}")
                            self._cancel_stop_order(stop_order_id, symbol)
                            self.broker.close_position(symbol)
                            if self.memory:
                                pnl = (current_price - entry_price) * qty
                                self.memory.log_trade_close(match["trade_id"], current_price, "geo_trailing_stop", pnl=pnl)
                    else:
                        if symbol not in self._low_water:
                            self._low_water[symbol] = current_price
                        self._low_water[symbol] = min(self._low_water[symbol], current_price)
                        trail_stop = self._low_water[symbol] * 1.01
                        if current_price >= trail_stop:
                            logger.info(f"[GEO] 🔴 TRAIL STOP (short): {symbol} ${current_price:.4f}")
                            self._cancel_stop_order(stop_order_id, symbol)
                            self.broker.close_position(symbol)
                            if self.memory:
                                pnl = (entry_price - current_price) * abs(qty)
                                self.memory.log_trade_close(match["trade_id"], current_price, "geo_trailing_stop", pnl=pnl)

        except Exception as e:
            logger.error(f"[GEO] manage_open_positions error: {e}")
