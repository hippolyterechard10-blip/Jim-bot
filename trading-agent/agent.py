import json
import logging
import uuid
import anthropic
import config
from scanner import MarketScanner
from strategy import (
    compute_indicators,
    compute_opportunity_score,
    detect_patterns,
    get_session_context,
    build_strategy_prompt,
    rank_symbols,
    is_good_stock_window,
    is_crypto_good_hours,
)

SCORE_LONG_MIN  = 60   # Score above this → bullish signal → call Claude (long candidate)
SCORE_SHORT_MAX = 30   # Score below this → bearish signal → call Claude (short candidate)
# Scores between 30–60 = ambiguous, no clear signal → skip Claude entirely

SHORT_TRAIL_PCT = 0.03   # Short positions: trailing stop 3% above lowest price reached

logger = logging.getLogger(__name__)

class TradingAgent:
    def __init__(self, broker, risk_manager, memory=None):
        self.broker = broker
        self.risk = risk_manager
        self.memory = memory
        self.client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self.scanner = MarketScanner()
        # Tracks highest price seen per LONG position (for trailing stop)
        self._high_water: dict = {}
        # Tracks lowest price seen per SHORT position (for trailing stop)
        self._low_water: dict = {}
        # Symbols for which partial profit has already been taken this session
        self._partial_taken: set = set()
        # Score-based trailing stop % per symbol (set at entry, read during management)
        self._trail_pcts: dict = {}

    def _is_crypto(self, alpaca_symbol: str) -> bool:
        """Alpaca returns crypto as 'BTCUSD'; our config uses 'BTC/USD'."""
        normalized = alpaca_symbol.replace("/", "")
        return any(normalized == s.replace("/", "") for s in config.CRYPTO_SYMBOLS)

    def _manage_trailing_stops(self):
        """Check every open LONG position against its score-based trailing stop."""
        try:
            positions = self.broker.get_positions()
        except Exception as e:
            logger.error(f"_manage_trailing_stops: could not fetch positions: {e}")
            return

        for pos in positions:
            try:
                if getattr(pos, "side", "long") == "short" or float(pos.qty) < 0:
                    continue
            except Exception:
                continue

            symbol = pos.symbol
            try:
                current_price = float(pos.current_price)
                current_qty   = abs(float(pos.qty))
                # Use score-based trailing %; fall back to 5% if position pre-dates session
                trail_pct = self._trail_pcts.get(symbol, 0.05)

                # ── Partial profit taking (+5% → sell 50%) ─────────────────
                unrealised_pct = float(getattr(pos, "unrealized_plpc", 0))
                if (unrealised_pct >= config.PARTIAL_PROFIT_PCT
                        and symbol not in self._partial_taken
                        and current_qty > 0):
                    sell_qty = round(current_qty * config.PARTIAL_PROFIT_RATIO, 4)
                    if sell_qty > 0:
                        remaining_qty  = current_qty - sell_qty
                        remaining_val  = remaining_qty * current_price
                        logger.info(
                            f"💰 PARTIAL PROFIT: {symbol} sold 50% at +{unrealised_pct*100:.1f}% "
                            f"| remaining ${remaining_val:,.2f} with {trail_pct*100:.0f}% trailing stop"
                        )
                        self.broker.place_order(symbol, sell_qty, "sell")
                        self._partial_taken.add(symbol)

                # Initialise high-water mark on first sight (or after restart)
                if symbol not in self._high_water:
                    self._high_water[symbol] = current_price
                    logger.info(
                        f"📈 Trailing stop init: {symbol} "
                        f"high={current_price:.6g} trail={trail_pct*100:.0f}%"
                    )
                    continue

                new_high = max(self._high_water[symbol], current_price)
                self._high_water[symbol] = new_high
                stop_level = new_high * (1 - trail_pct)

                logger.debug(
                    f"  {symbol}: price={current_price:.6g} "
                    f"high={new_high:.6g} stop={stop_level:.6g} trail={trail_pct*100:.0f}%"
                )

                if current_price <= stop_level:
                    logger.info(
                        f"🔴 TRAILING STOP HIT: {symbol} "
                        f"price={current_price:.6g} < stop={stop_level:.6g} "
                        f"(peak={new_high:.6g}, -{trail_pct*100:.0f}%)"
                    )
                    closed = self.broker.close_position(symbol)
                    if closed:
                        self._high_water.pop(symbol, None)
                        self._trail_pcts.pop(symbol, None)
                        self._partial_taken.discard(symbol)
                        if self.memory:
                            try:
                                open_trades = self.memory.get_recent_trades(limit=50)
                                match = next(
                                    (t for t in open_trades
                                     if t.get("symbol", "").replace("/", "") == symbol.replace("/", "")
                                     and t.get("status") == "open"),
                                    None
                                )
                                if match:
                                    entry  = float(match["entry_price"])
                                    qty_m  = float(match["qty"])
                                    pnl    = (current_price - entry) * qty_m
                                    pnl_pct = (current_price - entry) / entry * 100
                                    logger.info(
                                        f"TRADE EXIT: {symbol} | reason=trailing_stop "
                                        f"| pnl=${pnl:+.2f} ({pnl_pct:+.2f}%)"
                                    )
                                    self.memory.log_trade_close(
                                        match["trade_id"],
                                        exit_price=current_price,
                                        close_reason="trailing_stop",
                                        pnl=pnl,
                                    )
                            except Exception as me:
                                logger.warning(f"Memory update after trailing stop: {me}")
            except Exception as e:
                logger.error(f"Trailing stop error for {symbol}: {e}")

    def _manage_short_trailing_stops(self):
        """Check every SHORT position — trailing stop 3% above the lowest price reached."""
        try:
            positions = self.broker.get_positions()
        except Exception as e:
            logger.error(f"_manage_short_trailing_stops: could not fetch positions: {e}")
            return

        for pos in positions:
            try:
                side    = getattr(pos, "side", "long")
                qty_val = float(pos.qty)
                if side != "short" and qty_val >= 0:
                    continue
            except Exception:
                continue

            symbol = pos.symbol
            try:
                current_price = float(pos.current_price)
                current_qty   = abs(qty_val)
                trail_pct     = SHORT_TRAIL_PCT   # always 3% for shorts

                # ── Partial profit taking (+5% → cover 50%) ────────────────
                short_key      = f"short:{symbol}"
                unrealised_pct = float(getattr(pos, "unrealized_plpc", 0))
                if (unrealised_pct >= config.PARTIAL_PROFIT_PCT
                        and short_key not in self._partial_taken
                        and current_qty > 0):
                    cover_qty     = round(current_qty * config.PARTIAL_PROFIT_RATIO, 4)
                    if cover_qty > 0:
                        remaining_qty = current_qty - cover_qty
                        remaining_val = remaining_qty * current_price
                        logger.info(
                            f"💰 PARTIAL PROFIT: {symbol} sold 50% at +{unrealised_pct*100:.1f}% "
                            f"| remaining ${remaining_val:,.2f} with {trail_pct*100:.0f}% trailing stop"
                        )
                        self.broker.place_order(symbol, cover_qty, "buy")
                        self._partial_taken.add(short_key)

                # Initialise low-water mark on first sight (or after restart)
                if symbol not in self._low_water:
                    self._low_water[symbol] = current_price
                    logger.info(
                        f"📉 Short trailing stop init: {symbol} "
                        f"low={current_price:.6g} trail={trail_pct*100:.0f}%"
                    )
                    continue

                new_low    = min(self._low_water[symbol], current_price)
                self._low_water[symbol] = new_low
                stop_level = new_low * (1 + trail_pct)

                logger.debug(
                    f"  SHORT {symbol}: price={current_price:.6g} "
                    f"low={new_low:.6g} stop={stop_level:.6g} trail={trail_pct*100:.0f}%"
                )

                if current_price >= stop_level:
                    logger.info(
                        f"🟢 SHORT TRAILING STOP HIT: {symbol} "
                        f"price={current_price:.6g} >= stop={stop_level:.6g} "
                        f"(trough={new_low:.6g}, +{trail_pct*100:.0f}%) — covering short"
                    )
                    cover_qty = abs(qty_val)
                    covered   = self.broker.place_order(symbol, cover_qty, "buy")
                    if covered:
                        self._low_water.pop(symbol, None)
                        self._partial_taken.discard(f"short:{symbol}")
                        if self.memory:
                            try:
                                open_trades = self.memory.get_recent_trades(limit=50)
                                match = next(
                                    (t for t in open_trades
                                     if t.get("symbol", "").replace("/", "") == symbol.replace("/", "")
                                     and t.get("status") == "open"
                                     and t.get("side") == "sell"),
                                    None
                                )
                                if match:
                                    entry   = float(match["entry_price"])
                                    pnl     = (entry - current_price) * cover_qty
                                    pnl_pct = (entry - current_price) / entry * 100
                                    logger.info(
                                        f"TRADE EXIT: {symbol} | reason=short_trailing_stop "
                                        f"| pnl=${pnl:+.2f} ({pnl_pct:+.2f}%)"
                                    )
                                    self.memory.log_trade_close(
                                        match["trade_id"],
                                        exit_price=current_price,
                                        close_reason="short_trailing_stop",
                                        pnl=pnl,
                                    )
                            except Exception as me:
                                logger.warning(f"Memory update after short trailing stop: {me}")
            except Exception as e:
                logger.error(f"Short trailing stop error for {symbol}: {e}")

    def analyze_market(self, symbol, bars, market_context: str = ""):
        if bars is None or bars.empty:
            return None

        recent = bars.tail(50)
        prices = recent['close'].tolist()
        volumes = recent['volume'].tolist()

        if len(prices) < 20:
            return None

        indicators = compute_indicators(prices, volumes)
        if "error" in indicators:
            return None

        session_ctx = get_session_context()
        patterns = detect_patterns(indicators, session_ctx["session"])

        memory_context = ""
        if self.memory:
            memory_context = self.memory.get_context_for_agent(symbol)

        prompt = build_strategy_prompt(
            symbol=symbol,
            indicators=indicators,
            patterns=patterns,
            session_ctx=session_ctx,
            memory_context=memory_context,
            market_context=market_context,
        )

        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            result = json.loads(raw.strip())
            result["_indicators"] = indicators
            result["_patterns"] = patterns
            result["_session"] = session_ctx["session"]
            return result
        except Exception as e:
            logger.error(f"analyze_market error: {e}")
            return None

    def run_cycle(self):
        # First: manage trailing stops on all open positions (long and short)
        self._manage_trailing_stops()
        self._manage_short_trailing_stops()

        if not self.risk.can_trade():
            logger.warning("⚠️ Trading paused by risk manager")
            return

        session_ctx = get_session_context()
        session = session_ctx["session"]
        logger.info(f"📅 Session: {session} ({session_ctx['time_et']}) — stocks: {session_ctx['good_for_stocks']} | crypto: {session_ctx['good_for_crypto']}")

        dynamic_watchlist = self.scanner.get_dynamic_watchlist()
        symbols_to_scan = []
        for symbol in dynamic_watchlist:
            is_crypto = "/" in symbol
            if is_crypto and not is_crypto_good_hours():
                continue
            if not is_crypto and not is_good_stock_window():
                continue
            symbols_to_scan.append(symbol)

        if not symbols_to_scan:
            logger.info("😴 No symbols to scan in current session — crypto off-hours and market closed")
            return

        # Build market context once per cycle (news sentiment + top movers + calendar)
        market_context = self.scanner.build_market_context()

        symbols_data = {}
        for symbol in symbols_to_scan:
            try:
                bars = self.broker.get_bars(symbol)
                if bars is None or bars.empty:
                    continue
                prices = bars.tail(50)['close'].tolist()
                volumes = bars.tail(50)['volume'].tolist()
                if len(prices) < 20:
                    continue
                indicators = compute_indicators(prices, volumes)
                patterns = detect_patterns(indicators, session)
                symbols_data[symbol] = {"indicators": indicators, "patterns": patterns, "bars": bars}
            except Exception as e:
                logger.error(f"Pre-scan error on {symbol}: {e}")

        ranked = rank_symbols({s: d for s, d in symbols_data.items()})
        logger.info(f"🔍 Scanning {len(ranked)} symbols — top: {ranked[:3]}")

        # Pre-filter: two-band score gate
        #   score > 60 → bullish signal   → Claude evaluates for long
        #   score < 30 → bearish signal   → Claude evaluates for short
        #   30 ≤ score ≤ 60 → ambiguous   → skip (no API cost)
        passed_long, passed_short, skipped, no_short_crypto = [], [], [], []
        for symbol in ranked:
            data = symbols_data[symbol]
            is_crypto = "/" in symbol
            opp_score = compute_opportunity_score(data["indicators"], data["patterns"])
            data["opportunity_score"] = opp_score
            if opp_score > SCORE_LONG_MIN:
                passed_long.append(symbol)
            elif opp_score < SCORE_SHORT_MAX:
                if is_crypto:
                    no_short_crypto.append(symbol)
                    logger.info(f"BEARISH CRYPTO {symbol}: no short on Alpaca")
                else:
                    passed_short.append(symbol)
            else:
                skipped.append(symbol)
                logger.info(f"SKIPPED {symbol}: neutral signal score {opp_score}")

        passed = passed_long + passed_short
        if no_short_crypto:
            logger.info(
                f"📉 Bearish crypto skipped ({len(no_short_crypto)}): {', '.join(no_short_crypto)}"
            )
        if skipped:
            logger.info(
                f"⚡ Pre-filter: {len(skipped)} neutral score (30≤score≤60) skipped"
            )
        logger.info(
            f"🤖 Calling Claude for {len(passed)}/{len(ranked)} symbols — "
            f"long candidates: {passed_long or 'none'} | "
            f"short candidates (stocks/ETF only): {passed_short or 'none'}"
        )

        claude_confidences: dict = {}  # populated during Claude pass, consumed in short pass

        for symbol in passed:
            try:
                data = symbols_data[symbol]
                bars = data["bars"]

                decision = self.analyze_market(symbol, bars, market_context=market_context)
                if not decision:
                    continue

                current_price = float(bars['close'].iloc[-1])
                confidence = decision.get("confidence", 0)
                action = decision.get("decision", "hold")
                strategy = decision.get("strategy_used", "NONE")
                urgency = decision.get("urgency", "low")

                logger.info(
                    f"{symbol}: {action.upper()} | {strategy} | "
                    f"conf={confidence:.0%} | urgency={urgency} — {decision.get('reasoning','')}"
                )

                if self.memory:
                    self.memory.log_decision(
                        decision=action,
                        reasoning=decision.get("reasoning", ""),
                        symbol=symbol,
                        confidence=confidence,
                    )

                # Store for short selling pass
                claude_confidences[symbol] = confidence

                if session == "weekend":
                    min_confidence = 0.70
                elif session in ("mid_day", "after_hours", "closed"):
                    min_confidence = 0.85
                else:
                    min_confidence = 0.70

                if action == "buy" and confidence >= min_confidence:
                    # Look up daily volume from scanner cache (movers only; None for crypto)
                    cached_movers = self.scanner._movers_cache.get("symbols", [])
                    mover_info    = next((m for m in cached_movers if m["symbol"] == symbol), None)
                    daily_volume  = mover_info["volume"] if mover_info else None

                    opp_score = data["opportunity_score"]
                    qty, pct, trail_pct = self.risk.get_position_size_by_score(
                        symbol, current_price, opp_score, volume=daily_volume
                    )
                    amount = qty * current_price
                    sl     = self.risk.calculate_stop_loss(current_price, "buy")

                    logger.info(
                        f"TRADE ENTRY: {symbol} buy | score={opp_score} "
                        f"| position={pct*100:.0f}% = ${amount:,.2f} "
                        f"| stop={trail_pct*100:.0f}%"
                    )

                    # Store trail_pct so _manage_trailing_stops uses the correct %
                    self._trail_pcts[symbol] = trail_pct

                    self.broker.place_order(symbol, qty, "buy", sl)

                elif action == "sell" and confidence >= min_confidence:
                    self.broker.close_position(symbol)

            except Exception as e:
                logger.error(f"Error on {symbol}: {e}")

        # ── SHORT SELLING PASS ─────────────────────────────────────────────────
        # Separate pass over ALL scanned symbols (incl. pre-filtered ones).
        # Short only stocks/ETFs — Alpaca spot does NOT support crypto shorts.
        # Entry conditions: RSI > 70 AND MACD bearish AND price below SMA20
        #   + if Claude was called for this symbol: confidence < SHORT_ENTRY_CONF_MAX
        try:
            open_positions = {
                p.symbol.replace("/", ""): p
                for p in self.broker.get_positions()
            }
        except Exception as e:
            logger.error(f"Short entry: could not fetch positions: {e}")
            open_positions = {}

        short_candidates = 0
        for symbol, data in symbols_data.items():
            is_crypto = "/" in symbol
            if is_crypto:
                continue  # Alpaca spot = no crypto shorts

            ind = data["indicators"]
            rsi          = ind.get("rsi", 50)
            macd_bullish = ind.get("macd_bullish", True)
            above_sma20  = ind.get("above_sma20", True)
            current_price = ind.get("current_price", 0)

            # Technical gate
            if not (rsi > config.SHORT_ENTRY_RSI_MIN
                    and not macd_bullish
                    and not above_sma20):
                continue

            # Confidence gate: if Claude was called, require conf < threshold
            if symbol in passed:
                conf = claude_confidences.get(symbol, 1.0)
                if conf >= config.SHORT_ENTRY_CONF_MAX:
                    logger.debug(f"SHORT skip {symbol}: Claude conf={conf:.0%} >= {config.SHORT_ENTRY_CONF_MAX:.0%}")
                    continue

            # No existing position in this symbol
            sym_key = symbol.replace("/", "")
            if sym_key in open_positions:
                pos_side = getattr(open_positions[sym_key], "side", "long")
                logger.debug(f"SHORT skip {symbol}: already in {pos_side} position")
                continue

            if current_price <= 0:
                continue

            short_candidates += 1
            qty, short_pct = self.risk.get_short_position_size(symbol, current_price)
            amount = qty * current_price
            sl     = self.risk.calculate_stop_loss(current_price, "sell")
            opp_score_short = symbols_data[symbol]["opportunity_score"]

            logger.info(
                f"TRADE ENTRY: {symbol} sell | score={opp_score_short} "
                f"| position={short_pct*100:.0f}% = ${amount:,.2f} "
                f"| stop={SHORT_TRAIL_PCT*100:.0f}%"
            )
            order = self.broker.place_order(symbol, qty, "sell", sl)
            if order and self.memory:
                try:
                    trade_id = str(uuid.uuid4())
                    alpaca_id = getattr(order, "id", None)
                    self.memory.log_trade_open(
                        trade_id=trade_id,
                        symbol=symbol,
                        side="sell",
                        qty=qty,
                        entry_price=current_price,
                        stop_loss=sl,
                        alpaca_order_id=alpaca_id,
                    )
                except Exception as me:
                    logger.warning(f"Memory log short entry: {me}")

        if short_candidates == 0:
            logger.debug("SHORT PASS: no candidates met entry conditions")
