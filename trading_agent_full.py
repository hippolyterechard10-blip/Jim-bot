# ════════════════════════════════════════════════════════════
# FILE: agent.py
# ════════════════════════════════════════════════════════════
import json
import logging
import threading
import uuid
from datetime import datetime, timezone, time as dtime
import anthropic
import config
import pytz
from correlations import CorrelationIntelligence
from geometry import GeometryAnalysis
from regime import MarketRegime
from synthesis import SynthesisEngine
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
        self.scanner       = MarketScanner()
        self.regime        = MarketRegime()
        self.correlations  = CorrelationIntelligence()
        self.geometry      = GeometryAnalysis()
        self.synthesis     = SynthesisEngine(
            self.regime, self.correlations, self.geometry, self.scanner
        )
        # Tracks highest price seen per LONG position (for trailing stop)
        self._high_water: dict = {}
        # Tracks lowest price seen per SHORT position (for trailing stop)
        self._low_water: dict = {}
        # Symbols for which partial profit has already been taken this session
        self._partial_taken: set = set()
        if self.memory:
            try:
                saved = self.memory.get_memory("partial_taken_today", default=[])
                today = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%d")
                saved_date = self.memory.get_memory("partial_taken_date", default="")
                self._partial_taken = set(saved) if saved_date == today else set()
            except:
                pass
        # Score-based trailing stop % per symbol (set at entry, read during management)
        self._trail_pcts: dict = {}
        # Daily dedup: date string (YYYY-MM-DD ET) when cash liberation last fired
        self._cash_lib_last_date: str = ""
        # Two-speed loop: dedup + cross-loop coordination
        self._last_analyzed: dict  = {}  # symbol → UTC datetime of last Claude call
        self._fast_triggered: set  = set()  # symbols triggered by fast loop this cycle
        self._lock = threading.Lock()      # guards _last_analyzed, _fast_triggered
        # Pre-close: date (YYYY-MM-DD ET) when stock pre-close already fired today
        self._preclose_done_date: str = ""

    def _sync_orphan_positions(self):
        """Detect Alpaca positions with no matching open SQLite record and create them."""
        if not self.memory:
            return
        try:
            alpaca_positions = self.broker.get_positions()
            if not alpaca_positions:
                return
            open_trades = self.memory.get_open_trades()
            open_symbols = {t["symbol"].replace("/", "").upper(): t for t in open_trades}
            synced = 0
            for pos in alpaca_positions:
                sym_normalized = pos.symbol.replace("/", "").upper()
                if sym_normalized in open_symbols:
                    continue
                # Determine proper display symbol (crypto: "BTCUSD" → "BTC/USD")
                display_symbol = pos.symbol
                for crypto in config.CRYPTO_SYMBOLS:
                    if crypto.replace("/", "").upper() == sym_normalized:
                        display_symbol = crypto
                        break
                side        = "buy" if getattr(pos, "side", "long") == "long" else "sell"
                qty         = abs(float(pos.qty))
                entry_price = float(pos.avg_entry_price)
                trade_id    = str(uuid.uuid4())
                logger.info(
                    f"🔄 SYNC orphan: {display_symbol} {side} qty={qty:.6g} "
                    f"entry=${entry_price:.4f} — writing to SQLite"
                )
                self.memory.log_trade_open(
                    trade_id=trade_id,
                    symbol=display_symbol,
                    side=side,
                    qty=qty,
                    entry_price=entry_price,
                    alpaca_order_id=f"orphan_{sym_normalized}",
                    market_context={"source": "alpaca_sync",
                                    "synced_at": datetime.now(timezone.utc).isoformat()},
                )
                synced += 1
            if synced:
                logger.info(f"✅ Orphan sync complete: {synced} position(s) added to SQLite")
        except Exception as e:
            logger.error(f"_sync_orphan_positions error: {e}")

    def _pre_market_cash_liberation(self):
        """
        9:25 AM ET weekdays — per-position rules to free cash for stock market open:
          • Negative position          → skip (let trailing stop manage it)
          • Profitable but < +2% gain  → sell 100% (weak overnight, free all capital)
          • Profitable and >= +2% gain → sell 50%  (keep half running with trailing stop)
        """
        ET = pytz.timezone("America/New_York")
        now_et = datetime.now(ET)

        # Weekdays only
        if now_et.weekday() >= 5:
            return

        # Window: 9:23 – 9:28 AM ET (covers any 5-min cycle that straddles 9:25)
        t = now_et.time()
        if not (dtime(9, 23) <= t <= dtime(9, 28)):
            return

        # Daily dedup — fire at most once per calendar day
        today_str = now_et.strftime("%Y-%m-%d")
        if self._cash_lib_last_date == today_str:
            return

        # Mark immediately to prevent double-fire if an error occurs mid-loop
        self._cash_lib_last_date = today_str

        # Fetch all open positions from Alpaca
        try:
            positions = self.broker.get_positions()
        except Exception as e:
            logger.error(f"_pre_market_cash_liberation: cannot fetch positions: {e}")
            return

        if not positions:
            logger.info("💤 PRE-MARKET LIBERATION (9:25 ET): no open positions")
            return

        # Filter to long crypto positions only
        crypto_positions = []
        for pos in positions:
            if not self._is_crypto(pos.symbol):
                continue
            try:
                if getattr(pos, "side", "long") != "long" or float(pos.qty) <= 0:
                    continue
                crypto_positions.append(pos)
            except Exception:
                continue

        if not crypto_positions:
            logger.info("💤 PRE-MARKET LIBERATION (9:25 ET): no open crypto positions")
            return

        logger.info(
            f"⏰ PRE-MARKET LIBERATION triggered (9:25 ET) — "
            f"evaluating {len(crypto_positions)} crypto position(s)"
        )

        freed_total = 0.0

        for pos in crypto_positions:
            try:
                current_price = float(pos.current_price)
                avg_entry     = float(pos.avg_entry_price)
                qty_total     = float(pos.qty)

                # Resolve display symbol (e.g. "BTCUSD" → "BTC/USD")
                display_sym = pos.symbol
                for crypto in config.CRYPTO_SYMBOLS:
                    if crypto.replace("/", "").upper() == pos.symbol.replace("/", "").upper():
                        display_sym = crypto
                        break

                # Position gain %
                if avg_entry <= 0:
                    continue
                pos_gain_pct = (current_price - avg_entry) / avg_entry * 100

                # ── Rule 1: Negative → skip ──────────────────────────────────
                if pos_gain_pct < 0:
                    logger.info(
                        f"  ↳ PRE-MARKET LIBERATION: {display_sym} "
                        f"{pos_gain_pct:+.2f}% — HOLDING (negative, trailing stop managing)"
                    )
                    continue

                # ── Rule 2: Profitable < +2% → sell 100% ────────────────────
                # ── Rule 3: Profitable >= +2% → sell 50% ────────────────────
                if pos_gain_pct < 2.0:
                    sell_ratio = 1.00
                    sell_pct_label = "100%"
                else:
                    sell_ratio = 0.50
                    sell_pct_label = "50%"

                sell_qty = round(qty_total * sell_ratio, 8)
                if sell_qty <= 0:
                    continue

                freed = sell_qty * current_price
                freed_total += freed

                logger.info(
                    f"💵 PRE-MARKET LIBERATION: {display_sym} "
                    f"{pos_gain_pct:+.2f}% — selling {sell_pct_label} "
                    f"— freeing ${freed:.2f} for market open"
                )

                order = self.broker.place_order(display_sym, sell_qty, "sell")

                # Update SQLite memory
                if order and self.memory:
                    try:
                        open_trades = self.memory.get_open_trades()
                        match = next(
                            (t for t in open_trades
                             if t.get("symbol", "").replace("/", "").upper()
                                == pos.symbol.replace("/", "").upper()
                             and t.get("status") == "open"),
                            None,
                        )
                        if match:
                            pnl     = (current_price - float(match["entry_price"])) * sell_qty
                            pnl_pct = (current_price - float(match["entry_price"])) / float(match["entry_price"]) * 100
                            self.memory.log_trade_close(
                                match["trade_id"],
                                exit_price=current_price,
                                close_reason="pre_market_cash_liberation",
                                pnl=round(pnl, 4),
                                pnl_pct=round(pnl_pct, 4),
                            )
                    except Exception as me:
                        logger.warning(f"Memory update after liberation ({display_sym}): {me}")

            except Exception as e:
                logger.error(f"Cash liberation error for {pos.symbol}: {e}")

        if freed_total > 0:
            logger.info(
                f"✅ PRE-MARKET LIBERATION complete — "
                f"${freed_total:.2f} total freed ahead of 9:30 open"
            )
        else:
            logger.info(
                "💤 PRE-MARKET LIBERATION complete — "
                "no positions sold (all negative or zero qty)"
            )

    def _is_crypto(self, alpaca_symbol: str) -> bool:
        """Alpaca returns crypto as 'BTCUSD'; our config uses 'BTC/USD'."""
        normalized = alpaca_symbol.replace("/", "")
        return any(normalized == s.replace("/", "") for s in config.CRYPTO_SYMBOLS)

    # ── Two-speed loop helpers ─────────────────────────────────────────────────

    def _was_recently_analyzed(self, symbol: str, window_seconds: int = 180) -> bool:
        """Return True if Claude was called for this symbol within the last N seconds."""
        with self._lock:
            last = self._last_analyzed.get(symbol)
        if last is None:
            return False
        return (datetime.now(timezone.utc) - last).total_seconds() < window_seconds

    def _mark_analyzed(self, symbol: str):
        """Record the UTC time of the most recent Claude call for this symbol."""
        with self._lock:
            self._last_analyzed[symbol] = datetime.now(timezone.utc)

    def consume_fast_triggered(self) -> set:
        """Return (and clear) the set of symbols triggered by the fast loop since last slow cycle."""
        with self._lock:
            triggered = set(self._fast_triggered)
            self._fast_triggered.clear()
        return triggered

    def _check_hard_stops(self, positions):
        """[FAST] Close any LONG position whose loss exceeds TRADE_STOP_LOSS_PCT."""
        for pos in (positions or []):
            try:
                if getattr(pos, "side", "long") == "short":
                    continue
                current_price = float(pos.current_price)
                avg_entry     = float(pos.avg_entry_price)
                if avg_entry <= 0:
                    continue
                loss_pct = (avg_entry - current_price) / avg_entry
                if loss_pct >= config.TRADE_STOP_LOSS_PCT:
                    symbol = pos.symbol
                    logger.info(
                        f"[FAST] 🛑 FAST STOP: {symbol} hard stop hit at {current_price:.6g} "
                        f"— loss capped at {loss_pct*100:.1f}%"
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
                                    None,
                                )
                                if match:
                                    qty_m   = float(match["qty"])
                                    pnl     = (current_price - float(match["entry_price"])) * qty_m
                                    pnl_pct = -round(loss_pct * 100, 4)
                                    self.memory.log_trade_close(
                                        match["trade_id"],
                                        exit_price=current_price,
                                        close_reason="hard_stop_loss",
                                        pnl=round(pnl, 4),
                                        pnl_pct=pnl_pct,
                                    )
                            except Exception as me:
                                logger.warning(f"[FAST] Memory update after hard stop ({symbol}): {me}")
            except Exception as e:
                logger.error(f"[FAST] Hard stop check error: {e}")

    def _preclose_stocks(self, positions: list, today_str: str) -> None:
        """
        Close all equity (non-crypto) positions at 15:55 ET to avoid overnight
        exposure.  Fires at most once per calendar day (guarded by _preclose_done_date).
        """
        if self._preclose_done_date == today_str:
            return
        self._preclose_done_date = today_str
        stock_positions = [p for p in positions if "/" not in p.symbol]
        if not stock_positions:
            logger.info("[Pre-close] No equity positions to close before 16:00 ET")
            return
        for pos in stock_positions:
            symbol = pos.symbol
            try:
                current_price = float(pos.current_price)
                closed = self.broker.close_position(symbol)
                if closed:
                    logger.info(f"[Pre-close] ✅ Closed {symbol} before 16:00 ET")
                    # Clean up in-memory state
                    self._high_water.pop(symbol, None)
                    self._low_water.pop(symbol, None)
                    self._trail_pcts.pop(symbol, None)
                    self._partial_taken.discard(symbol)
                    # Update SQLite record
                    if self.memory:
                        try:
                            open_trades = self.memory.get_recent_trades(limit=50)
                            match = next(
                                (t for t in open_trades
                                 if t.get("symbol", "") == symbol
                                 and t.get("status") == "open"),
                                None,
                            )
                            if match:
                                qty_m   = float(match["qty"])
                                pnl     = (current_price - float(match["entry_price"])) * qty_m
                                pnl_pct = round(pnl / (float(match["entry_price"]) * qty_m) * 100, 4)
                                self.memory.log_trade_close(
                                    match["trade_id"],
                                    exit_price=current_price,
                                    close_reason="pre_market_close",
                                    pnl=round(pnl, 4),
                                    pnl_pct=pnl_pct,
                                )
                        except Exception as me:
                            logger.warning(f"[Pre-close] Memory update for {symbol}: {me}")
            except Exception as e:
                logger.error(f"[Pre-close] ❌ Failed to close {symbol}: {e}")

    def _close_dust_positions(self, positions: list, threshold: float = 5.0) -> None:
        """
        Close any open position whose absolute market value is below `threshold`
        (default $5).  These 'dust' positions tie up a slot without meaningful
        upside.
        """
        for pos in positions:
            symbol = pos.symbol
            try:
                market_value = abs(float(pos.market_value))
                if market_value >= threshold:
                    continue
                current_price = float(pos.current_price)
                closed = self.broker.close_position(symbol)
                if closed:
                    logger.info(
                        f"[Dust] 🧹 Closed dust position: {symbol} "
                        f"(market_value=${market_value:.2f} < ${threshold:.0f})"
                    )
                    self._high_water.pop(symbol, None)
                    self._low_water.pop(symbol, None)
                    self._trail_pcts.pop(symbol, None)
                    self._partial_taken.discard(symbol)
                    if self.memory:
                        try:
                            open_trades = self.memory.get_recent_trades(limit=50)
                            match = next(
                                (t for t in open_trades
                                 if t.get("symbol", "") == symbol
                                 and t.get("status") == "open"),
                                None,
                            )
                            if match:
                                qty_m   = float(match["qty"])
                                pnl     = (current_price - float(match["entry_price"])) * qty_m
                                pnl_pct = round(pnl / (float(match["entry_price"]) * qty_m) * 100, 4)
                                self.memory.log_trade_close(
                                    match["trade_id"],
                                    exit_price=current_price,
                                    close_reason="dust_cleanup",
                                    pnl=round(pnl, 4),
                                    pnl_pct=pnl_pct,
                                )
                        except Exception as me:
                            logger.warning(f"[Dust] Memory update for {symbol}: {me}")
            except Exception as e:
                logger.error(f"[Dust] Failed to close {symbol}: {e}")

    def fast_loop_tick(self):
        """
        FAST LOOP — called every 30 seconds from a background thread.
        0. Pre-close stocks at 15:55 ET (weekdays only, once per day)
        1. Trailing stop check  → FAST EXIT log
        2. Hard stop check      → FAST STOP log
        3. Dust cleanup: close positions with market_value < $5
        4. RSI/MACD/volume scan → compute raw opportunity score
        5. Score > 60 or < 30  → FAST TRIGGER → call Claude immediately
        6. Volume > 5× average → VOLUME SPIKE log
        Never calls Claude directly; triggers analyze_market() when threshold crossed.
        """
        # ── 0: Pre-close equity positions at 15:55 ET ─────────────────────────
        now_et    = datetime.now(pytz.timezone("America/New_York"))
        today_str = now_et.strftime("%Y-%m-%d")
        if now_et.weekday() < 5 and now_et.hour == 15 and now_et.minute >= 55:
            try:
                preclose_positions = self.broker.get_positions() or []
            except Exception:
                preclose_positions = []
            self._preclose_stocks(preclose_positions, today_str)

        # ── 1 & 2: Position management ────────────────────────────────────────
        try:
            positions = self.broker.get_positions() or []
        except Exception as e:
            logger.error(f"[FAST] Cannot fetch positions: {e}")
            positions = []

        self._manage_trailing_stops(loop_tag="FAST", positions=positions)
        self._manage_short_trailing_stops(loop_tag="FAST", positions=positions)
        self._check_hard_stops(positions)
        self._close_dust_positions(positions)

        # ── 4-6: Technical scan (no movers refresh — uses cached data) ────────
        session_ctx = get_session_context()
        session     = session_ctx["session"]

        # Build fast watchlist from session rules + cached movers
        fast_watchlist: list[str] = []
        if is_good_stock_window():
            fast_watchlist.extend(config.BLUECHIP_SYMBOLS)
        if is_crypto_good_hours():
            fast_watchlist.extend(config.CRYPTO_SYMBOLS)
        cached_movers = self.scanner._movers_cache.get("symbols", [])
        for m in cached_movers[:10]:
            sym = m.get("symbol")
            if sym and sym not in fast_watchlist:
                fast_watchlist.append(sym)

        for symbol in fast_watchlist:
            # Skip symbols recently analyzed (3-min window)
            if self._was_recently_analyzed(symbol, window_seconds=180):
                continue

            try:
                bars = self.broker.get_bars(symbol)
                if bars is None or bars.empty:
                    continue
                bars50  = bars.tail(50)
                prices  = bars50["close"].tolist()
                volumes = bars50["volume"].tolist()
                if len(prices) < 20:
                    continue

                indicators = compute_indicators(prices, volumes)
                if "error" in indicators:
                    continue
                _cm = self.scanner._movers_cache.get("symbols", [])
                _mi = next((m for m in _cm if m["symbol"] == symbol), None)
                if _mi and _mi.get("is_gapper"):
                    indicators["change_pct"] = _mi["change_pct"]
                opens50  = bars50["open"].tolist()
                highs50  = bars50["high"].tolist()
                lows50   = bars50["low"].tolist()
                patterns = detect_patterns(indicators, session)
                score    = compute_opportunity_score(indicators, patterns)

                # ── 5: Volume spike ───────────────────────────────────────────
                sample   = volumes[-20:] if len(volumes) >= 20 else volumes
                avg_vol  = sum(sample) / len(sample) if sample else 1
                last_vol = volumes[-1] if volumes else 0
                ratio    = last_vol / avg_vol if avg_vol > 0 else 1.0
                if ratio >= 5.0:
                    logger.info(
                        f"[FAST] ⚡ VOLUME SPIKE: {symbol} {ratio:.1f}x average — monitoring"
                    )

                # ── 4: Score threshold → synthesis gate → Claude ─────────────
                if score > 60 or score < 30:
                    _side = "long" if score > 60 else "short"

                    # Run full synthesis before calling Claude — same gate as slow loop
                    try:
                        open_pos_syms = [p.symbol for p in positions]
                        synth = self.synthesis.run(
                            symbol=symbol,
                            base_score=score,
                            opens=opens50, highs=highs50, lows=lows50,
                            closes=prices, volumes=volumes,
                            open_positions=open_pos_syms,
                            side=_side,
                        )
                    except Exception as synth_err:
                        logger.warning(f"[FAST] Synthesis error on {symbol}: {synth_err}")
                        synth = {
                            "final_score": score, "should_call_claude": True,
                            "full_context": "", "stop_loss": None, "take_profit": None,
                            "stop_pct": None, "target_pct": None, "risk_reward": None,
                            "size_multiplier": 1.0,
                        }

                    final_score = synth["final_score"]

                    # Hard gate: synthesis must approve before Claude is called
                    if not synth.get("should_call_claude", True):
                        reason = synth.get("decision_reason", "synthesis score below threshold")
                        logger.info(
                            f"[FAST] ⛔ SYNTHESIS SKIP {symbol}: {reason} "
                            f"(raw={score:.0f} → synth={final_score:.0f})"
                        )
                        if self.memory:
                            self.memory.log_decision(
                                decision="hold",
                                reasoning=(
                                    f"[FAST] Synthesis blocked Claude call — {reason}. "
                                    f"Breakdown: {synth.get('score_breakdown', '')}"
                                ),
                                symbol=symbol,
                                confidence=0.0,
                            )
                        continue

                    logger.info(
                        f"[FAST] 🎯 FAST TRIGGER: {symbol} score={score:.0f} "
                        f"→ synth={final_score:.0f} → calling Claude now"
                    )
                    self._mark_analyzed(symbol)
                    with self._lock:
                        self._fast_triggered.add(symbol)

                    market_context = self.scanner.build_market_context()
                    synth_ctx      = synth.get("full_context", "")
                    try:
                        decision = self.analyze_market(
                            symbol, bars,
                            market_context=f"{market_context}\n\n{synth_ctx}" if synth_ctx else market_context,
                        )
                    except Exception as e:
                        logger.error(f"[FAST] analyze_market error {symbol}: {e}")
                        continue

                    if not decision:
                        continue

                    action     = decision.get("decision", "hold")
                    confidence = decision.get("confidence", 0)
                    logger.info(
                        f"[FAST] {symbol}: {action.upper()} conf={confidence:.0%} — "
                        f"{decision.get('reasoning', '')[:120]}"
                    )

                    if self.memory:
                        self.memory.log_decision(
                            decision=action,
                            reasoning=f"[FAST TRIGGER] {decision.get('reasoning', '')}",
                            symbol=symbol,
                            confidence=confidence,
                        )

                    regime_params = self.regime.get_params()
                    min_conf      = regime_params["confidence_threshold"] / 100
                    current_price = float(bars["close"].iloc[-1])

                    if action == "buy" and confidence >= min_conf:
                        if self.risk.can_trade():
                            qty, pct, trail_pct = self.risk.get_position_size_by_score(
                                symbol, current_price, final_score
                            )
                            # Prefer ATR-anchored stop/target from synthesis geometry layer
                            geo_sl = synth.get("stop_loss")
                            geo_tp = synth.get("take_profit")
                            sl     = geo_sl if geo_sl else self.risk.calculate_stop_loss(current_price, "buy")
                            if geo_sl and synth.get("stop_pct"):
                                stop_pct = synth["stop_pct"] / 100
                                if stop_pct < trail_pct:
                                    trail_pct = stop_pct
                            amount = qty * current_price
                            rr_str = f" | R:R={synth.get('risk_reward', 0):.1f}x" if synth.get("risk_reward") else ""
                            tp_str = f" | target=${geo_tp:.4f}" if geo_tp else ""
                            logger.info(
                                f"[FAST] TRADE ENTRY: {symbol} buy | score={final_score:.0f} "
                                f"| position={pct*100:.0f}% = ${amount:,.2f} "
                                f"| stop=${sl:.4f}{tp_str}{rr_str}"
                            )
                            order = self.broker.place_order(symbol, qty, "buy", sl, take_profit=geo_tp)
                            self._trail_pcts[symbol] = trail_pct
                            if order and self.memory:
                                try:
                                    self.memory.log_trade_open(
                                        trade_id=str(uuid.uuid4()),
                                        symbol=symbol,
                                        side="buy",
                                        qty=qty,
                                        entry_price=current_price,
                                        stop_loss=sl,
                                        market_context={
                                            "source": "fast_loop_trigger",
                                            "score": final_score,
                                            "regime": self.regime._cache.get("regime", "unknown"),
                                        },
                                    )
                                except Exception as me:
                                    logger.warning(f"[FAST] Memory log entry ({symbol}): {me}")

                    elif action == "sell" and confidence >= min_conf:
                        logger.info(
                            f"[FAST] TRADE EXIT: {symbol} sell signal | conf={confidence:.0%}"
                        )
                        self.broker.close_position(symbol)

            except Exception as e:
                logger.error(f"[FAST] Scan error {symbol}: {e}")

    # ─────────────────────────────────────────────────────────────────────────

    def _manage_trailing_stops(self, loop_tag: str = "", positions=None):
        """Check every open LONG position against its score-based trailing stop."""
        if positions is None:
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
                        secured_val    = sell_qty * current_price
                        logger.info(
                            f"💰 PARTIAL PROFIT: {symbol} sold 50% at "
                            f"+{unrealised_pct*100:.1f}% — "
                            f"${secured_val:,.2f} secured — "
                            f"remaining 50% running with {trail_pct*100:.0f}% trailing stop"
                        )
                        order = self.broker.place_order(symbol, sell_qty, "sell")
                        self._partial_taken.add(symbol)
                        if self.memory:
                            try:
                                self.memory.set_memory("partial_taken_today", list(self._partial_taken), "state")
                                self.memory.set_memory("partial_taken_date", __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%d"), "state")
                            except:
                                pass

                        # ── Update SQLite: close old record, reopen with remaining qty ──
                        if self.memory:
                            try:
                                open_trades = self.memory.get_open_trades()
                                match = next(
                                    (t for t in open_trades
                                     if t.get("symbol", "").replace("/", "").upper()
                                        == symbol.replace("/", "").upper()
                                     and t.get("status") == "open"),
                                    None,
                                )
                                if match:
                                    entry_p = float(match["entry_price"])
                                    pnl     = (current_price - entry_p) * sell_qty
                                    pnl_pct = (current_price - entry_p) / entry_p * 100 if entry_p > 0 else 0
                                    # Close the original record (for the sold portion)
                                    self.memory.log_trade_close(
                                        match["trade_id"],
                                        exit_price=current_price,
                                        close_reason="partial_profit",
                                        pnl=round(pnl, 4),
                                        pnl_pct=round(pnl_pct, 4),
                                    )
                                    # Reopen a new record for the remaining 50%
                                    if remaining_qty > 0:
                                        self.memory.log_trade_open(
                                            trade_id=str(uuid.uuid4()),
                                            symbol=match["symbol"],
                                            side="buy",
                                            qty=remaining_qty,
                                            entry_price=entry_p,
                                            stop_loss=match.get("stop_loss"),
                                            alpaca_order_id=match.get("alpaca_order_id"),
                                            market_context={"source": "partial_profit_remainder"},
                                        )
                            except Exception as me:
                                logger.warning(f"Memory update after partial profit ({symbol}): {me}")

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
                    if loop_tag:
                        logger.info(
                            f"[{loop_tag}] FAST EXIT: {symbol} trailing stop hit at "
                            f"{current_price:.6g} (peak={new_high:.6g}, -{trail_pct*100:.0f}%)"
                        )
                    else:
                        logger.info(
                            f"[SLOW] 🔴 TRAILING STOP HIT: {symbol} "
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

    def _manage_short_trailing_stops(self, loop_tag: str = "", positions=None):
        """Check every SHORT position — trailing stop 3% above the lowest price reached."""
        if positions is None:
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
                        secured_val   = cover_qty * current_price
                        logger.info(
                            f"💰 PARTIAL PROFIT: {symbol} sold 50% at "
                            f"+{unrealised_pct*100:.1f}% — "
                            f"${secured_val:,.2f} secured — "
                            f"remaining 50% running with {trail_pct*100:.0f}% trailing stop"
                        )
                        self.broker.place_order(symbol, cover_qty, "buy")
                        self._partial_taken.add(short_key)

                        # ── Update SQLite: close old record, reopen with remaining qty ──
                        if self.memory:
                            try:
                                open_trades = self.memory.get_open_trades()
                                match = next(
                                    (t for t in open_trades
                                     if t.get("symbol", "").replace("/", "").upper()
                                        == symbol.replace("/", "").upper()
                                     and t.get("status") == "open"
                                     and t.get("side") == "sell"),
                                    None,
                                )
                                if match:
                                    entry_p = float(match["entry_price"])
                                    pnl     = (entry_p - current_price) * cover_qty
                                    pnl_pct = (entry_p - current_price) / entry_p * 100 if entry_p > 0 else 0
                                    self.memory.log_trade_close(
                                        match["trade_id"],
                                        exit_price=current_price,
                                        close_reason="partial_profit",
                                        pnl=round(pnl, 4),
                                        pnl_pct=round(pnl_pct, 4),
                                    )
                                    if remaining_qty > 0:
                                        self.memory.log_trade_open(
                                            trade_id=str(uuid.uuid4()),
                                            symbol=match["symbol"],
                                            side="sell",
                                            qty=remaining_qty,
                                            entry_price=entry_p,
                                            stop_loss=match.get("stop_loss"),
                                            alpaca_order_id=match.get("alpaca_order_id"),
                                            market_context={"source": "partial_profit_remainder"},
                                        )
                            except Exception as me:
                                logger.warning(f"Memory update after short partial profit ({symbol}): {me}")

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
                    if loop_tag:
                        logger.info(
                            f"[{loop_tag}] FAST EXIT: {symbol} short trailing stop hit at "
                            f"{current_price:.6g} (trough={new_low:.6g}, +{trail_pct*100:.0f}%)"
                        )
                    else:
                        logger.info(
                            f"[SLOW] 🟢 SHORT TRAILING STOP HIT: {symbol} "
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

        regime_context = self.regime.build_regime_context()
        combined_context = market_context if market_context else regime_context

        prompt = build_strategy_prompt(
            symbol=symbol,
            indicators=indicators,
            patterns=patterns,
            session_ctx=session_ctx,
            memory_context=memory_context,
            market_context=combined_context,
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

    def run_cycle(self, skip_symbols: set = None):
        """
        SLOW LOOP — called every 5 minutes from the main thread.
        skip_symbols: set of symbols already triggered+analyzed by the fast loop this cycle.
        """
        # 9:25 ET weekdays: free crypto cash ahead of stock market open
        self._pre_market_cash_liberation()

        # Sync any Alpaca positions that have no SQLite record (e.g. manual trades)
        self._sync_orphan_positions()

        # NOTE: Trailing stop management is handled exclusively by the fast loop (every 30s).
        # No trailing stop call here to avoid double-firing on simultaneous startup.

        if not self.risk.can_trade():
            logger.warning("[SLOW] ⚠️ Trading paused by risk manager")
            return

        session_ctx = get_session_context()
        session = session_ctx["session"]
        logger.info(
            f"[SLOW] 📅 Session: {session} ({session_ctx['time_et']}) — "
            f"stocks: {session_ctx['good_for_stocks']} | crypto: {session_ctx['good_for_crypto']}"
        )

        # ── Regime-driven dynamic parameters ─────────────────────────────────
        regime_params   = self.regime.get_params()
        _regime         = regime_params["regime"]
        score_long_min  = regime_params["score_long_threshold"]
        score_short_max = regime_params["score_short_threshold"]
        size_mult       = regime_params["position_size_multiplier"]
        regime_conf_min = regime_params["confidence_threshold"] / 100
        logger.info(
            f"[SLOW] 🎯 Regime: {_regime.upper()} | "
            f"long_min={score_long_min} | short_max={score_short_max} | "
            f"size={size_mult}x | conf_min={regime_conf_min:.0%}"
        )

        _skip = skip_symbols or set()
        dynamic_watchlist = self.scanner.get_dynamic_watchlist()
        symbols_to_scan = []
        for symbol in dynamic_watchlist:
            is_crypto = "/" in symbol
            if is_crypto and not is_crypto_good_hours():
                continue
            if not is_crypto and not is_good_stock_window():
                continue
            # Skip symbols the fast loop already analyzed this cycle
            if symbol in _skip:
                logger.info(f"[SLOW] ⏭ Skipping {symbol} — already triggered by fast loop")
                continue
            symbols_to_scan.append(symbol)

        if not symbols_to_scan:
            logger.info("[SLOW] 😴 No symbols to scan in current session — crypto off-hours and market closed")
            return

        # Build market context once per cycle (news sentiment + top movers + calendar + earnings)
        market_context = self.scanner.build_market_context()

        # ── Correlation intelligence — refresh prices once per cycle ──────────
        corr_changes    = self.correlations.refresh_prices(symbols_to_scan)
        dxy_trend       = self.regime._cache.get("dxy") or "neutral"
        try:
            _open_pos_list  = self.broker.get_positions() or []
            open_pos_symbols = [p.symbol for p in _open_pos_list]
        except Exception:
            open_pos_symbols = []

        # Build earnings alert map: symbol → {type, days_away}
        earnings_alerts_map = {ea["symbol"]: ea for ea in self.scanner.get_earnings_alerts()}

        cached_movers_list = self.scanner._movers_cache.get("symbols", [])
        symbols_data = {}
        for symbol in symbols_to_scan:
            try:
                bars = self.broker.get_bars(symbol)
                if bars is None or bars.empty:
                    continue
                bars50  = bars.tail(50)
                prices  = bars50['close'].tolist()
                volumes = bars50['volume'].tolist()
                opens   = bars50['open'].tolist()
                highs   = bars50['high'].tolist()
                lows    = bars50['low'].tolist()
                if len(prices) < 20:
                    continue
                indicators = compute_indicators(prices, volumes)
                _mi = next((m for m in cached_movers_list if m["symbol"] == symbol), None)
                if _mi and _mi.get("is_gapper"):
                    indicators["change_pct"] = _mi["change_pct"]
                patterns   = detect_patterns(indicators, session)
                symbols_data[symbol] = {
                    "indicators": indicators, "patterns": patterns, "bars": bars,
                    "opens": opens, "highs": highs, "lows": lows,
                    "prices": prices, "volumes": volumes,
                }
            except Exception as e:
                logger.error(f"Pre-scan error on {symbol}: {e}")

        ranked = rank_symbols({s: d for s, d in symbols_data.items()})
        logger.info(f"[SLOW] 🔍 Scanning {len(ranked)} symbols — top: {ranked[:3]}")

        # Pre-filter: two-band score gate
        #   score > 60 → bullish signal   → Claude evaluates for long
        #   score < 30 → bearish signal   → Claude evaluates for short
        #   30 ≤ score ≤ 60 → ambiguous   → skip (no API cost)
        passed_long, passed_short, skipped, no_short_crypto = [], [], [], []
        cached_movers_list = self.scanner._movers_cache.get("symbols", [])

        for symbol in ranked:
            data      = symbols_data[symbol]
            is_crypto = "/" in symbol
            base_score = compute_opportunity_score(data["indicators"], data["patterns"])
            _side      = "short" if base_score < score_short_max else "long"

            # ── Synthesis: all intelligence layers → one final score ─────────
            try:
                synth = self.synthesis.run(
                    symbol=symbol,
                    base_score=base_score,
                    opens=data["opens"], highs=data["highs"], lows=data["lows"],
                    closes=data["prices"], volumes=data["volumes"],
                    open_positions=open_pos_symbols,
                    side=_side,
                )
            except Exception as synth_err:
                logger.warning(f"Synthesis error on {symbol}: {synth_err}")
                synth = {
                    "final_score": base_score, "should_call_claude": True,
                    "full_context": "", "stop_loss": None, "take_profit": None,
                    "stop_pct": None, "target_pct": None, "risk_reward": None,
                    "size_multiplier": 1.0,
                }

            data["synthesis"] = synth
            opp_score = synth["final_score"]
            data["opportunity_score"] = opp_score

            # ── Gapper override: always reach Claude regardless of regime ───
            if any(m.get("is_gapper") and m["symbol"] == symbol for m in cached_movers_list):
                opp_score = max(opp_score, score_long_min + 1)
                data["opportunity_score"] = opp_score
                logger.info(
                    f"🚨 GAPPER OVERRIDE: {symbol} score forced to {opp_score} "
                    f"— bypassing regime threshold"
                )

            # ── Earnings day: hard skip (stocks only) ──────────────────────
            sym_base = symbol  # stocks use bare ticker; crypto keeps "BTC/USD"
            ea = earnings_alerts_map.get(sym_base)
            if ea and ea["type"] == "earnings_day" and not is_crypto:
                logger.info(f"EARNINGS DAY: skipping {symbol} — too risky to enter")
                if self.memory:
                    self.memory.log_decision(
                        decision="hold",
                        reasoning=f"EARNINGS DAY: {symbol} reports today — no entry, volatility risk too high.",
                        symbol=symbol,
                        confidence=0.0,
                    )
                continue

            # ── Post-earnings gapper: boost score if gap >5% ───────────────
            if ea and ea["type"] == "post_earnings" and not is_crypto:
                mover_info = next((m for m in cached_movers_list if m["symbol"] == sym_base), None)
                if mover_info and mover_info.get("change_pct", 0) >= 5:
                    logger.info(
                        f"📈 POST-EARNINGS GAPPER: {symbol} +{mover_info['change_pct']:.1f}% "
                        f"— boosting score to 90 (was {opp_score})"
                    )
                    opp_score = 90
                    data["opportunity_score"] = 90

            if opp_score > score_long_min:
                passed_long.append(symbol)
            elif opp_score < score_short_max:
                if is_crypto:
                    no_short_crypto.append(symbol)
                    logger.info(f"BEARISH CRYPTO {symbol}: no short on Alpaca")
                    # Log bearish hold so dashboard shows current state
                    if self.memory:
                        self.memory.log_decision(
                            decision="hold",
                            reasoning=f"Bearish signal (score={opp_score}/100) but crypto shorts not supported on Alpaca. Monitoring for reversal.",
                            symbol=symbol,
                            confidence=round(max(0.1, (30 - opp_score) / 30), 2),
                        )
                else:
                    passed_short.append(symbol)
            else:
                skipped.append(symbol)
                logger.info(f"SKIPPED {symbol}: neutral signal score {opp_score}")
                # For crypto: always log HOLD so the dashboard shows current status
                if is_crypto and self.memory:
                    neutral_conf = round(abs(opp_score - 45) / 45 * 0.4, 2)  # max 0.4 for neutral
                    self.memory.log_decision(
                        decision="hold",
                        reasoning=f"Neutral signal (score={opp_score}/100) — no clear directional bias. Monitoring.",
                        symbol=symbol,
                        confidence=neutral_conf,
                    )

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
                data  = symbols_data[symbol]
                bars  = data["bars"]
                synth = data.get("synthesis", {})

                # ── Hard gate: only call Claude when synthesis says go ────────
                if not synth.get("should_call_claude", True):
                    reason = synth.get("decision_reason", "synthesis score below threshold")
                    logger.info(f"⛔ SYNTHESIS SKIP {symbol}: {reason}")
                    if self.memory:
                        self.memory.log_decision(
                            decision="hold",
                            reasoning=(
                                f"Synthesis engine blocked Claude call — {reason}. "
                                f"Breakdown: {synth.get('score_breakdown', '')}"
                            ),
                            symbol=symbol,
                            confidence=0.0,
                        )
                    continue

                synth_ctx = synth.get("full_context", "")
                decision = self.analyze_market(
                    symbol, bars,
                    market_context=f"{market_context}\n\n{synth_ctx}"
                )
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

                # Regime sets the base bar; session can only raise it, never lower
                if session in ("mid_day", "after_hours", "closed"):
                    min_confidence = max(regime_conf_min, 0.85)
                elif session == "weekend":
                    min_confidence = max(regime_conf_min, 0.70)
                else:
                    min_confidence = regime_conf_min

                if action == "buy" and confidence >= min_confidence:
                    # Look up daily volume from scanner cache (movers only; None for crypto)
                    cached_movers = self.scanner._movers_cache.get("symbols", [])
                    mover_info    = next((m for m in cached_movers if m["symbol"] == symbol), None)
                    daily_volume  = mover_info["volume"] if mover_info else None

                    opp_score = data["opportunity_score"]
                    qty, pct, trail_pct = self.risk.get_position_size_by_score(
                        symbol, current_price, opp_score, volume=daily_volume
                    )

                    # ── Correlation conflict check — hard block if ≥80% overlap ──
                    corr_check = self.correlations.check_correlation_conflict(
                        symbol, open_pos_symbols
                    )
                    if corr_check["conflict"] and corr_check.get("score_adjustment", 0) <= -20:
                        logger.info(
                            f"🚫 CORRELATION BLOCK: {symbol} entry skipped — "
                            f"{corr_check['reason']}"
                        )
                        if self.memory:
                            self.memory.log_decision(
                                decision="hold",
                                reasoning=f"Correlation conflict blocked entry: {corr_check['reason']}",
                                symbol=symbol,
                                confidence=confidence,
                            )
                        continue

                    # ── Beta-adjusted size (correlations module) ────────────────
                    beta_pct = self.correlations.get_beta_adjusted_size(
                        symbol, pct * 100, _regime
                    )
                    if abs(beta_pct - pct * 100) > 0.5:
                        ratio    = beta_pct / (pct * 100) if pct > 0 else 1.0
                        orig_qty = qty
                        qty      = max(1, round(qty * ratio, 8))
                        pct      = beta_pct / 100
                        logger.info(
                            f"  ↳ Beta adj: {symbol} → {beta_pct:.0f}% of portfolio "
                            f"| qty {orig_qty} → {qty}"
                        )

                    # ── Minimum position floor ──────────────────────────────────
                    MIN_POSITION_PCT = 0.05
                    if pct < MIN_POSITION_PCT:
                        logger.info(
                            f"  ↳ Position floor: {symbol} pct={pct*100:.1f}% below 5% minimum — skipping entry"
                        )
                        continue

                    # ── Pre-earnings cap: limit to 10% of portfolio ─────────────
                    ea_buy = earnings_alerts_map.get(symbol)
                    if ea_buy and ea_buy["type"] == "pre_earnings":
                        try:
                            account = self.broker.api.get_account()
                            portfolio_value = float(account.portfolio_value)
                            max_qty = int((0.10 * portfolio_value) / current_price)
                            if qty > max_qty:
                                logger.info(
                                    f"⚠️ PRE-EARNINGS cap: {symbol} qty {qty}→{max_qty} "
                                    f"(10% limit, reports in {ea_buy['days_away']}d)"
                                )
                                qty = max_qty
                                pct = 0.10
                        except Exception as cap_err:
                            logger.warning(f"Pre-earnings cap error: {cap_err}")

                    amount = qty * current_price
                    # Prefer ATR-anchored stop/target from synthesis (geometry layer)
                    geo_sl = synth.get("stop_loss")
                    geo_tp = synth.get("take_profit")
                    sl     = geo_sl if geo_sl else self.risk.calculate_stop_loss(current_price, "buy")
                    if geo_sl:
                        geo_stop_pct = synth.get("stop_pct", 0)
                        if geo_stop_pct and geo_stop_pct / 100 < trail_pct:
                            trail_pct               = geo_stop_pct / 100
                            self._trail_pcts[symbol] = trail_pct

                    rr_str = f" | R:R={synth.get('risk_reward', 0):.1f}x" if synth.get("risk_reward") else ""
                    tp_str = f" | target=${geo_tp:.4f}" if geo_tp else ""
                    logger.info(
                        f"TRADE ENTRY: {symbol} buy | score={opp_score} "
                        f"| position={pct*100:.0f}% = ${amount:,.2f} "
                        f"| stop={trail_pct*100:.0f}%{tp_str}{rr_str}"
                    )

                    # Store trail_pct so _manage_trailing_stops uses the correct %
                    self._trail_pcts[symbol] = trail_pct

                    self.broker.place_order(symbol, qty, "buy", sl, take_profit=geo_tp)

                elif action == "sell" and confidence >= min_confidence:
                    self.broker.close_position(symbol)
                    self._high_water.pop(symbol, None)
                    self._trail_pcts.pop(symbol, None)
                    self._partial_taken.discard(symbol)

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

            # Technical gate: 2 of 3 conditions required
            short_signals = sum([
                rsi > config.SHORT_ENTRY_RSI_MIN,
                not macd_bullish,
                not above_sma20,
            ])
            if short_signals < 2:
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
            sl = self.risk.calculate_stop_loss(current_price, "sell")
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
                        market_context={
                            "source": "run_cycle_short",
                            "score": opp_score_short,
                            "regime": self.regime._cache.get("regime", "unknown"),
                        },
                    )
                except Exception as me:
                    logger.warning(f"Memory log short entry: {me}")

        if short_candidates == 0:
            logger.debug("SHORT PASS: no candidates met entry conditions")



# ════════════════════════════════════════════════════════════
# FILE: main.py
# ════════════════════════════════════════════════════════════
import logging
import threading
import time
from dotenv import load_dotenv
load_dotenv()

import config
from broker import AlpacaBroker
from risk import RiskManager
from agent import TradingAgent
from memory import TradingMemory
from analyzer import TradeAnalyzer
from dashboard import start_dashboard
from notifier import TradingNotifier

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)
logger = logging.getLogger(__name__)

WATCHDOG_INTERVAL_SECONDS = 60


def _make_fast_thread(agent: TradingAgent) -> threading.Thread:
    """Create (but do not start) a new fast loop daemon thread."""
    def _run():
        logger.info("[FAST] ⚡ Fast loop thread started — 30s tick")
        while True:
            try:
                agent.fast_loop_tick()
            except Exception as e:
                logger.error(f"[FAST] loop error: {e}")
            time.sleep(config.FAST_LOOP_INTERVAL_SECONDS)

    return threading.Thread(target=_run, daemon=True, name="FastLoop")


def _run_watchdog(agent: TradingAgent, thread_ref: list):
    """
    Daemon thread — wakes every 60 seconds.
    Checks whether the fast loop thread is still alive.
    If dead, spawns a fresh replacement and updates thread_ref[0].
    """
    while True:
        time.sleep(WATCHDOG_INTERVAL_SECONDS)
        t = thread_ref[0]
        if t.is_alive():
            logger.info("[WATCHDOG] fast loop alive ✅")
        else:
            logger.warning("[WATCHDOG] fast loop dead — restarting 🔄")
            new_thread = _make_fast_thread(agent)
            new_thread.start()
            thread_ref[0] = new_thread
            logger.info("[WATCHDOG] fast loop restarted successfully ✅")


def main():
    logger.info("🚀 Trading Agent starting...")

    memory   = TradingMemory("trading_memory.db")
    broker   = AlpacaBroker()
    risk     = RiskManager(broker)
    agent    = TradingAgent(broker, risk, memory)
    analyzer = TradeAnalyzer(memory)
    notifier = TradingNotifier(memory, analyzer)

    start_dashboard(memory, analyzer, scanner=agent.scanner, regime=agent.regime, agent=agent, port=5000)
    notifier.start_scheduler(daily_hour_utc=20)

    # ── Start the fast loop ───────────────────────────────────────────────────
    fast_thread = _make_fast_thread(agent)
    fast_thread.start()

    # thread_ref is a mutable list so the watchdog can swap in a replacement
    thread_ref = [fast_thread]

    # ── Start the watchdog ────────────────────────────────────────────────────
    watchdog_thread = threading.Thread(
        target=_run_watchdog,
        args=(agent, thread_ref),
        daemon=True,
        name="Watchdog",
    )
    watchdog_thread.start()

    logger.info(
        f"✅ All systems running — "
        f"fast loop: {config.FAST_LOOP_INTERVAL_SECONDS}s | "
        f"slow loop: {config.LOOP_INTERVAL_SECONDS}s | "
        f"watchdog: {WATCHDOG_INTERVAL_SECONDS}s"
    )

    # ── Slow loop (main thread) ───────────────────────────────────────────────
    cycle = 0
    while True:
        try:
            cycle += 1
            logger.info(f"[SLOW] --- Cycle {cycle} ---")

            if risk.check_global_stop_loss():
                logger.error("🔴 GLOBAL STOP LOSS — shutting down")
                broker.close_all_positions()
                notifier.send_stop_loss_alert(
                    broker.get_portfolio_value()
                )
                break

            # Hand off symbols the fast loop already triggered this cycle
            skip_symbols = agent.consume_fast_triggered()
            if skip_symbols:
                logger.info(
                    f"[SLOW] ⏭ Fast loop triggered {len(skip_symbols)} symbol(s) "
                    f"this cycle — skipping in slow scan: {sorted(skip_symbols)}"
                )

            agent.run_cycle(skip_symbols=skip_symbols)
            analyzer.run_pending_analyses()

            anomalies = analyzer.detect_performance_anomalies()
            if anomalies:
                logger.warning(f"[SLOW] {len(anomalies)} anomalie(s) detected")

            time.sleep(config.LOOP_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            logger.info("⛔ Agent stopped by user")
            break
        except Exception as e:
            logger.error(f"[SLOW] Cycle error: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()



# ════════════════════════════════════════════════════════════
# FILE: synthesis.py
# ════════════════════════════════════════════════════════════
"""
synthesis.py — Master Scoring Engine
Combines ALL intelligence layers into one final conviction score.
This runs BEFORE every Claude API call.
Only calls Claude when final score crosses the threshold.
"""
import logging
from news_intelligence import NewsIntelligence

logger = logging.getLogger(__name__)

# Score thresholds
LONG_THRESHOLD      = 60
SHORT_THRESHOLD     = 30
CONFIDENCE_MINIMUM  = 0.70


class SynthesisEngine:

    def __init__(self, regime, correlations, geometry, scanner):
        self.regime       = regime
        self.correlations = correlations
        self.geometry     = geometry
        self.scanner      = scanner   # kept for non-news uses (movers cache, etc.)
        self.news         = NewsIntelligence()
        logger.info("✅ SynthesisEngine initialized — all layers connected")

    def run(
        self,
        symbol: str,
        base_score: float,
        opens: list,
        highs: list,
        lows: list,
        closes: list,
        volumes: list,
        open_positions: list,
        side: str = "long",
    ) -> dict:
        """
        Master pre-trade analysis.
        Returns final score, full context string, and stop/target levels.
        Only proceed to Claude if final score crosses threshold.
        """

        # ── Layer 1: Market Regime ─────────────────────────────────────────────
        regime_params   = self.regime.get_params()
        regime_name     = regime_params["regime"]
        regime_adj      = self.regime.get_score_adjustments()
        regime_context  = self.regime.build_regime_context()

        long_threshold   = regime_params["score_long_threshold"]
        short_threshold  = regime_params["score_short_threshold"]
        confidence_min   = regime_params["confidence_threshold"]
        size_multiplier  = regime_params["position_size_multiplier"]

        # ── Layer 2: Correlations ──────────────────────────────────────────────
        # refresh_prices uses 5-min cache; parent cycle already warmed it for all symbols
        changes      = self.correlations.refresh_prices([symbol])
        dxy_trend    = self.regime._cache.get("dxy") or "neutral"
        is_crypto    = "/" in symbol

        corr_conflict = self.correlations.check_correlation_conflict(symbol, open_positions)
        rel_strength  = self.correlations.detect_relative_strength(symbol, changes)
        dxy_impact    = (
            self.correlations.get_dxy_crypto_adjustment(dxy_trend)
            if is_crypto else {"adjustment": 0, "reason": "N/A"}
        )
        corr_context  = self.correlations.build_correlation_context(
            symbol, open_positions, changes, dxy_trend
        )

        # ── Layer 3: Geometry ──────────────────────────────────────────────────
        geo         = self.geometry.build_geometry_context(
            symbol, opens, highs, lows, closes, volumes, side
        )
        geo_context = geo["context"]

        # ── Layer 4: News Intelligence (Tier 1-4 classification) ─────────────────
        # Replaces simple scanner keyword detection with full tiered analysis:
        # Tier 1 = market-moving (±25), Tier 2 = directional (±12-15),
        # Tier 3 = contextual (±6), + Trump direction signal + earnings whisper
        news_result  = self.news.analyze(symbol)
        news_adj     = max(-35, min(35, news_result["total_score_adjustment"]))
        news_context = news_result["context"]

        # ── Final Score Calculation ────────────────────────────────────────────
        regime_score_adj = (
            regime_adj["long_bonus"] if side == "long" else regime_adj["short_penalty"]
        )

        final_score = (
            base_score
            + regime_score_adj
            + rel_strength["score_adjustment"]
            + (dxy_impact["adjustment"] if is_crypto else 0)
            + corr_conflict.get("score_adjustment", 0)
            + geo["score_adjustment"]
            + news_adj
        )
        final_score = max(0, min(100, final_score))

        # ── Decision ──────────────────────────────────────────────────────────
        if side == "long":
            should_call_claude = final_score >= long_threshold
            decision_reason    = (
                f"LONG score {final_score:.0f} "
                f"{'≥' if should_call_claude else '<'} threshold {long_threshold}"
            )
        else:
            should_call_claude = final_score <= short_threshold
            decision_reason    = (
                f"SHORT score {final_score:.0f} "
                f"{'≤' if should_call_claude else '>'} threshold {short_threshold}"
            )

        # ── Build Master Context for Claude ───────────────────────────────────
        score_breakdown = (
            f"Base: {base_score:.0f} | "
            f"Regime: {regime_score_adj:+.0f} | "
            f"RelStr: {rel_strength['score_adjustment']:+.0f} | "
            f"DXY: {dxy_impact['adjustment']:+.0f} | "
            f"Corr: {corr_conflict.get('score_adjustment', 0):+.0f} | "
            f"Geo: {geo['score_adjustment']:+.0f} | "
            f"News: {news_adj:+.0f} | "
            f"FINAL: {final_score:.0f}"
        )

        synthesis_lines = [
            "=== SYNTHESIS SCORE ===",
            score_breakdown,
            f"Decision: {decision_reason}",
        ]

        full_context = "\n\n".join([
            regime_context,
            corr_context,
            geo_context,
            news_context,          # full Tier 1-4 classification from NewsIntelligence
            "\n".join(synthesis_lines),
        ])

        logger.info(
            f"🧠 SYNTHESIS {symbol} {side.upper()}: {score_breakdown} | "
            f"{'→ CALLING CLAUDE' if should_call_claude else '→ SKIPPED'}"
        )

        return {
            "final_score":       final_score,
            "should_call_claude": should_call_claude,
            "decision_reason":   decision_reason,
            "score_breakdown":   score_breakdown,
            "full_context":      full_context,
            "stop_loss":         geo.get("stop_loss"),
            "take_profit":       geo.get("take_profit"),
            "stop_pct":          geo.get("stop_pct"),
            "target_pct":        geo.get("target_pct"),
            "risk_reward":       geo.get("risk_reward"),
            "size_multiplier":   size_multiplier,
            "confidence_minimum": confidence_min,
            "regime":            regime_name,
            "patterns":          geo.get("patterns_detected", []),
        }



# ════════════════════════════════════════════════════════════
# FILE: strategy.py
# ════════════════════════════════════════════════════════════
"""
strategy.py — Stratégies de trading avancées
Gapper scanner, momentum, breakout, mean reversion, crypto 24/7
"""
import logging
from datetime import datetime, timezone, time
import pytz
import numpy as np

logger = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")


# ─── HORAIRES DE MARCHÉ ───────────────────────────────────────────────────────

def get_market_session() -> str:
    """
    Retourne la session de marché actuelle.
    pre_market / open / mid_day / power_hour / closed
    """
    now_et = datetime.now(ET)
    t = now_et.time()
    weekday = now_et.weekday()

    if weekday >= 5:
        return "weekend"

    if time(4, 0) <= t < time(9, 30):
        return "pre_market"
    elif time(9, 30) <= t < time(11, 0):
        return "open"        # ⭐ Meilleure fenêtre — gappers + momentum
    elif time(11, 0) <= t < time(15, 0):
        return "mid_day"     # Volume faible — éviter
    elif time(15, 0) <= t < time(16, 0):
        return "power_hour"  # 2ème meilleure fenêtre
    elif time(16, 0) <= t < time(20, 0):
        return "after_hours"
    else:
        return "closed"


def is_good_stock_window() -> bool:
    """True si on est dans une bonne fenêtre pour trader les stocks."""
    session = get_market_session()
    return session in ["pre_market", "open", "power_hour"]


def is_crypto_good_hours() -> bool:
    """
    Crypto trade 24/7 mais évite 2h-6h UTC (volume mort).
    """
    hour_utc = datetime.now(timezone.utc).hour
    return not (2 <= hour_utc < 6)


def get_session_context() -> dict:
    """Retourne le contexte de session pour le prompt Claude."""
    session = get_market_session()
    now_et = datetime.now(ET)

    context = {
        "session": session,
        "time_et": now_et.strftime("%H:%M ET"),
        "day": now_et.strftime("%A"),
        "good_for_stocks": is_good_stock_window(),
        "good_for_crypto": is_crypto_good_hours(),
    }

    context["instructions"] = {
        "pre_market": "Pre-market 4am-9:30am ET: scan for gappers actively. If a confirmed gapper is found (>20% change, >3x volume), ENTER immediately — do not wait for open. This is the highest priority window for gap plays.",
        "open": "PRIME TIME 9:30-11h: Focus on gappers +20%. Momentum and breakout entries. Be aggressive.",
        "mid_day": "Mid-day: low volume, choppy. Reduce position sizes. Prefer crypto.",
        "power_hour": "Power hour 15-16h: good volatility returns. Look for end-of-day momentum.",
        "after_hours": "After-hours: crypto only. Stocks illiquid.",
        "closed": "Market closed: crypto only.",
        "weekend": "Weekend: crypto only, markets closed.",
    }.get(session, "")

    return context


# ─── INDICATEURS TECHNIQUES ───────────────────────────────────────────────────

def compute_indicators(prices: list, volumes: list) -> dict:
    """
    Calcule tous les indicateurs techniques nécessaires.
    Retourne un dict complet pour le prompt Claude.
    """
    if len(prices) < 20:
        return {"error": "Not enough data"}

    p = np.array(prices, dtype=float)
    v = np.array(volumes, dtype=float)

    # ── Prix ──
    current = float(p[-1])
    prev_close = float(p[-2])
    change_pct = ((current - prev_close) / prev_close) * 100

    # ── Moyennes mobiles ──
    sma20 = float(np.mean(p[-20:]))
    sma9  = float(np.mean(p[-9:])) if len(p) >= 9 else sma20
    # ── MACD ──
    macd_series = np.array([
        _ema(p[:i], 12) - _ema(p[:i], 26)
        for i in range(26, len(p))
    ])
    macd = float(macd_series[-1]) if len(macd_series) > 0 else 0
    macd_signal = _ema(macd_series, 9) if len(macd_series) >= 9 else macd
    macd_hist = macd - macd_signal

    # ── RSI ──
    rsi = _rsi(p, 14)

    # ── Bollinger Bands ──
    bb_mid  = sma20
    bb_std  = float(np.std(p[-20:]))
    bb_up   = bb_mid + 2 * bb_std
    bb_low  = bb_mid - 2 * bb_std
    bb_pct  = (current - bb_low) / (bb_up - bb_low) * 100 if (bb_up - bb_low) > 0 else 50

    # ── Volume ──
    avg_vol    = float(np.mean(v[-20:]))
    curr_vol   = float(v[-1])
    vol_ratio  = curr_vol / avg_vol if avg_vol > 0 else 1

    # ── Momentum ──
    momentum_5  = ((current - p[-5])  / p[-5])  * 100 if len(p) >= 5  else 0
    momentum_10 = ((current - p[-10]) / p[-10]) * 100 if len(p) >= 10 else 0

    # ── Support / Résistance ──
    high_20 = float(np.max(p[-20:]))
    low_20  = float(np.min(p[-20:]))
    near_resistance = current >= high_20 * 0.98
    near_support    = current <= low_20  * 1.02

    # ── ATR (Average True Range) — volatilité ──
    atr = _atr(p, 14)
    atr_pct = (atr / current) * 100

    return {
        "current_price":    round(current, 4),
        "change_pct":       round(change_pct, 2),
        "sma9":             round(sma9, 4),
        "sma20":            round(sma20, 4),
        "above_sma20":      current > sma20,
        "macd":             round(float(macd), 4),
        "macd_signal":      round(float(macd_signal), 4),
        "macd_bullish":     macd > macd_signal,
        "rsi":              round(rsi, 1),
        "rsi_oversold":     rsi < 30,
        "rsi_overbought":   rsi > 70,
        "bb_pct":           round(bb_pct, 1),
        "bb_squeeze":       bb_std < (sma20 * 0.01),
        "volume_ratio":     round(vol_ratio, 2),
        "high_volume":      vol_ratio > 2.0,
        "momentum_5":       round(float(momentum_5), 2),
        "momentum_10":      round(float(momentum_10), 2),
        "near_resistance":  near_resistance,
        "near_support":     near_support,
        "high_20":          round(high_20, 4),
        "low_20":           round(low_20, 4),
        "atr_pct":          round(atr_pct, 2),
        "high_volatility":  atr_pct > 2.0,
    }


def _ema(prices: np.ndarray, period: int) -> float:
    """Calcule l'EMA d'une série de prix."""
    if len(prices) < period:
        return float(np.mean(prices))
    k = 2 / (period + 1)
    ema = float(np.mean(prices[:period]))
    for price in prices[period:]:
        ema = float(price) * k + ema * (1 - k)
    return ema


def _rsi(prices: np.ndarray, period: int = 14) -> float:
    """Calcule le RSI."""
    if len(prices) < period + 1:
        return 50.0
    deltas = np.diff(prices)
    gains  = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _atr(prices: np.ndarray, period: int = 14) -> float:
    """Calcule l'ATR (Average True Range)."""
    if len(prices) < 2:
        return 0.0
    trs = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]
    return float(np.mean(trs[-period:]))


# ─── DÉTECTION DE PATTERNS ────────────────────────────────────────────────────

def detect_patterns(indicators: dict, session: str) -> dict:
    """
    Détecte les patterns de trading et retourne les opportunités.
    """
    patterns = []
    score = 0  # Score d'opportunité global

    rsi       = indicators.get("rsi", 50)
    macd_bull = indicators.get("macd_bullish", False)
    above_sma = indicators.get("above_sma20", False)
    vol_ratio = indicators.get("volume_ratio", 1)
    bb_pct    = indicators.get("bb_pct", 50)
    mom5      = indicators.get("momentum_5", 0)
    near_res  = indicators.get("near_resistance", False)
    near_sup  = indicators.get("near_support", False)
    change    = indicators.get("change_pct", 0)
    high_vol  = indicators.get("high_volatility", False)

    # ── GAPPER (stock en forte hausse à l'ouverture) ──
    if change > 20 and vol_ratio > 3:
        patterns.append("GAPPER")
        score += 40

    # ── MOMENTUM ──
    if mom5 > 2 and macd_bull and above_sma and vol_ratio > 1.5:
        patterns.append("MOMENTUM_BULL")
        score += 25
    elif mom5 < -2 and not macd_bull and not above_sma and vol_ratio > 1.5:
        patterns.append("MOMENTUM_BEAR")
        score += 20

    # ── BREAKOUT ──
    if near_res and vol_ratio > 2 and macd_bull:
        patterns.append("BREAKOUT")
        score += 30
    elif near_sup and vol_ratio > 2 and not macd_bull:
        patterns.append("BREAKDOWN")
        score += 20

    # ── MEAN REVERSION ──
    if rsi < 25 and near_sup:
        patterns.append("OVERSOLD_REVERSAL")
        score += 20
    elif rsi > 75 and near_res:
        patterns.append("OVERBOUGHT_REVERSAL")
        score += 15

    # ── SCALP (volume élevé + volatilité) ──
    if vol_ratio > 3 and high_vol:
        patterns.append("SCALP_OPPORTUNITY")
        score += 10

    # ── CONSOLIDATION (éviter) ──
    if vol_ratio < 0.5 and abs(mom5) < 0.5:
        patterns.append("CONSOLIDATING")
        score -= 20

    return {
        "patterns":   patterns,
        "score":      max(0, score),
        "is_opportunity": score >= 25,
        "best_pattern":   patterns[0] if patterns else "NONE",
        "suggested_action": _suggest_action(patterns, score),
    }


def compute_opportunity_score(indicators: dict, patterns: dict) -> int:
    """
    Score DIRECTIONNEL 0-100 :
      > 60  → signal haussier clair    → Claude appelé (candidat long)
      < 30  → signal baissier clair    → Claude appelé (candidat short)
      30-60 → ambigu / neutre          → Claude skippé (pas de coût API)

    Part d'un score neutre de 50, puis ajoute/retire des points selon la
    direction des indicateurs techniques. Le volume amplifie la direction.
    """
    rsi          = indicators.get("rsi", 50)
    macd_bullish = indicators.get("macd_bullish", True)
    above_sma20  = indicators.get("above_sma20", True)
    momentum_5   = indicators.get("momentum_5", 0.0)
    vol_ratio    = indicators.get("volume_ratio", 1.0)
    bb_pct       = indicators.get("bb_pct", 50.0)
    near_sup     = indicators.get("near_support", False)
    near_res     = indicators.get("near_resistance", False)
    pattern_act  = patterns.get("suggested_action", "hold")
    pattern_sc   = patterns.get("score", 0)

    score = 50  # point de départ neutre

    # ── RSI : oversold = haussier, overbought = baissier (±18 pts) ────────
    if rsi < 25:
        score += 18
    elif rsi < 30:
        score += 12
    elif rsi < 40:
        score += 6
    elif rsi > 75:
        score -= 18
    elif rsi > 70:
        score -= 12
    elif rsi > 60:
        score -= 6

    # ── MACD : bullish = haussier, bearish = baissier (±8 pts) ───────────
    score += 8 if macd_bullish else -8

    # ── SMA20 : au-dessus = haussier, en-dessous = baissier (±6 pts) ─────
    score += 6 if above_sma20 else -6

    # ── Momentum 5 bars : positif = haussier (max ±10 pts) ───────────────
    mom_pts = max(-10, min(10, int(momentum_5 * 3)))
    score += mom_pts

    # ── Volume : amplifie la direction courante (max ±8 pts) ─────────────
    if vol_ratio >= 2.0:
        vol_amp = 8
    elif vol_ratio >= 1.5:
        vol_amp = 4
    else:
        vol_amp = 0
    if score > 50:
        score += vol_amp
    elif score < 50:
        score -= vol_amp

    # ── Bollinger %B : extrêmes confirment la direction (±8 pts) ─────────
    if bb_pct < 15:
        score += 8
    elif bb_pct < 25:
        score += 5
    elif bb_pct > 85:
        score -= 8
    elif bb_pct > 75:
        score -= 5

    # ── Support / Résistance : contexte directionnel (±3 pts) ────────────
    if near_sup:
        score += 3
    if near_res:
        score -= 3

    # ── Pattern détecté (±8 pts) ─────────────────────────────────────────
    if pattern_act == "buy" and pattern_sc > 0:
        score += min(8, pattern_sc // 5)
    elif pattern_act == "sell" and pattern_sc > 0:
        score -= min(8, pattern_sc // 5)

    return max(0, min(100, score))


def _suggest_action(patterns: list, score: int) -> str:
    """Suggère une action basée sur les patterns détectés."""
    if not patterns or score < 25:
        return "hold"
    buy_patterns  = {"GAPPER", "MOMENTUM_BULL", "BREAKOUT", "OVERSOLD_REVERSAL", "SCALP_OPPORTUNITY"}
    sell_patterns = {"MOMENTUM_BEAR", "BREAKDOWN", "OVERBOUGHT_REVERSAL"}
    buy_count  = sum(1 for p in patterns if p in buy_patterns)
    sell_count = sum(1 for p in patterns if p in sell_patterns)
    if buy_count > sell_count:
        return "buy"
    elif sell_count > buy_count:
        return "sell"
    return "hold"


# ─── PROMPT ENRICHI POUR CLAUDE ───────────────────────────────────────────────

def build_strategy_prompt(
    symbol: str,
    indicators: dict,
    patterns: dict,
    session_ctx: dict,
    memory_context: str = "",
    market_context: str = "",
) -> str:
    """
    Construit un prompt complet pour Claude avec tout le contexte stratégique.
    C'est ce prompt qui fait la vraie différence vs un agent basique.
    """
    is_crypto = "/" in symbol
    asset_type = "CRYPTO (24/7)" if is_crypto else "STOCK (NYSE/Nasdaq)"

    return f"""You are an expert day trader with 10 years experience. Make a precise trading decision.

## Asset
- Symbol: {symbol} ({asset_type})
- Session: {session_ctx['session']} ({session_ctx['time_et']})
- Session guidance: {session_ctx['instructions']}

## Technical Indicators
- Price: ${indicators.get('current_price')} ({indicators.get('change_pct'):+.2f}% change)
- RSI: {indicators.get('rsi')} {'🔴 OVERBOUGHT' if indicators.get('rsi_overbought') else '🟢 OVERSOLD' if indicators.get('rsi_oversold') else ''}
- MACD: {'🟢 BULLISH' if indicators.get('macd_bullish') else '🔴 BEARISH'}
- Above SMA20: {indicators.get('above_sma20')}
- Volume ratio vs average: {indicators.get('volume_ratio')}x {'⚡ HIGH VOLUME' if indicators.get('high_volume') else ''}
- Momentum 5 bars: {indicators.get('momentum_5'):+.2f}%
- Bollinger %B: {indicators.get('bb_pct'):.0f}% {'(near top)' if indicators.get('bb_pct',50)>80 else '(near bottom)' if indicators.get('bb_pct',50)<20 else ''}
- Volatility (ATR%): {indicators.get('atr_pct'):.2f}% {'⚡ HIGH' if indicators.get('high_volatility') else ''}
- Near resistance: {indicators.get('near_resistance')} | Near support: {indicators.get('near_support')}

## Detected Patterns
- Patterns: {', '.join(patterns.get('patterns', ['NONE']))}
- Opportunity score: {patterns.get('score')}/100
- Suggested action: {patterns.get('suggested_action').upper()}

## Strategy Rules by Session
{'- PRIME TIME: Prioritize GAPPERS (+20% at open with high volume). Enter momentum early, exit before 11h.' if session_ctx['session'] == 'open' else ''}
{'- MID-DAY: Low conviction trades only. Tight stops. Prefer crypto.' if session_ctx['session'] == 'mid_day' else ''}
{'- POWER HOUR: End-of-day momentum. Watch for reversals.' if session_ctx['session'] == 'power_hour' else ''}
{'- CRYPTO: Mean reversion on dips. Momentum on breakouts. No PDT rule.' if is_crypto else ''}

## Multi-Strategy Framework
- MOMENTUM: Enter if price + volume confirm trend. RSI 40-70 range ideal.
- BREAKOUT: Enter on high-volume break of 20-bar high. Volume must be 2x+ average.
- MEAN REVERSION: Enter on RSI<30 near support. Quick 2-5% target.
- GAPPER: Stock up 20%+ at open with 3x+ volume = highest priority opportunity.
- SCALP: Only if volume ratio >3x AND high volatility. Tight 1-2% target.

## Risk Rules (NON-NEGOTIABLE)
- Max position: 40% of capital (score 90-100), 30% (score 80-89), 20% (score 70-79), 15% (score 60-69)
- Stop loss: ATR-based (typically 1.5-3%), minimum 1:2 risk/reward enforced
- Take profit: +10% (or +2% for scalps)
- Max 5 open positions
- If session is mid_day or bad hours: confidence must be >0.85 to trade

{market_context}

{memory_context}

## Decision Required
Based on ALL the above, what is your trading decision?

Respond ONLY with valid JSON, no markdown, no explanation outside JSON:
{{
  "decision": "buy" or "sell" or "hold",
  "confidence": 0.0 to 1.0,
  "strategy_used": "MOMENTUM" or "BREAKOUT" or "MEAN_REVERSION" or "GAPPER" or "SCALP" or "NONE",
  "reasoning": "2-3 sentences explaining your decision with specific reference to the indicators",
  "entry_price": current price or null,
  "target_price": your take profit target or null,
  "stop_price": your stop loss or null,
  "urgency": "high" or "medium" or "low"
}}"""


# ─── SCANNER DE PRIORITÉ ──────────────────────────────────────────────────────

def rank_symbols(symbols_data: dict) -> list:
    """
    Classe les symboles par ordre de priorité d'opportunité.
    symbols_data = {symbol: {"indicators": ..., "patterns": ...}}
    Retourne une liste triée du plus au moins intéressant.
    """
    ranked = []
    for symbol, data in symbols_data.items():
        patterns = data.get("patterns", {})
        indicators = data.get("indicators", {})
        score = patterns.get("score", 0)

        # Bonus crypto en dehors des heures de marché
        if "/" in symbol and not is_good_stock_window():
            score += 10

        # Bonus gapper
        if "GAPPER" in patterns.get("patterns", []):
            score += 50

        # Bonus volume élevé
        if indicators.get("high_volume"):
            score += 15

        ranked.append((symbol, score))

    ranked.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in ranked]



# ════════════════════════════════════════════════════════════
# FILE: broker.py
# ════════════════════════════════════════════════════════════
import logging
import alpaca_trade_api as tradeapi
import config

logger = logging.getLogger(__name__)

class AlpacaBroker:
    def __init__(self):
        self.api = tradeapi.REST(
            config.ALPACA_API_KEY,
            config.ALPACA_SECRET_KEY,
            config.ALPACA_BASE_URL
        )
        logger.info("✅ Alpaca broker connected")

    def get_account(self):
        try:
            return self.api.get_account()
        except Exception as e:
            logger.error(f"get_account error: {e}")
            return None

    def get_portfolio_value(self):
        account = self.get_account()
        if account:
            return float(account.portfolio_value)
        return config.INITIAL_CAPITAL

    def get_positions(self):
        try:
            return self.api.list_positions()
        except Exception as e:
            logger.error(f"get_positions error: {e}")
            return []

    def get_bars(self, symbol, timeframe="1Min", limit=50):
        try:
            is_crypto = "/" in symbol
            if is_crypto:
                bars = self.api.get_crypto_bars(symbol, timeframe, limit=limit).df
            else:
                bars = self.api.get_bars(symbol, timeframe, limit=limit).df
            return bars
        except Exception as e:
            logger.error(f"get_bars error for {symbol}: {e}")
            return None

    def place_order(self, symbol, qty, side, stop_loss=None, take_profit=None):
        try:
            is_crypto = "/" in symbol
            if not is_crypto:
                qty = max(1, int(qty))
            order_params = dict(
                symbol=symbol,
                qty=qty,
                side=side,
                type="market",
                time_in_force="gtc" if is_crypto else "day"
            )
            if stop_loss and take_profit and "/" not in symbol:  # stocks only — crypto stops managed in memory
                order_params["order_class"] = "bracket"
                order_params["stop_loss"] = {"stop_price": round(stop_loss, 4)}
                order_params["take_profit"] = {"limit_price": round(take_profit, 4)}
            order = self.api.submit_order(**order_params)
            logger.info(f"✅ Order: {side} {qty} {symbol} | stop={stop_loss} target={take_profit}")
            return order
        except Exception as e:
            logger.error(f"place_order error: {e}")
            return None

    def close_position(self, symbol):
        try:
            self.api.close_position(symbol)
            logger.info(f"✅ Position closed: {symbol}")
            return True
        except Exception as e:
            logger.error(f"close_position error: {e}")
            return False

    def close_all_positions(self):
        try:
            self.api.close_all_positions()
            logger.info("✅ All positions closed")
            return True
        except Exception as e:
            logger.error(f"close_all_positions error: {e}")
            return False



# ════════════════════════════════════════════════════════════
# FILE: risk.py
# ════════════════════════════════════════════════════════════
import logging
import config
from regime import MarketRegime

logger = logging.getLogger(__name__)

_regime_detector = MarketRegime()

# Score tiers: (min_score_inclusive, position_pct, trailing_stop_pct)
# Evaluated top-down; first match wins.
SCORE_TIERS = [
    (90, 0.40, 0.02),
    (80, 0.30, 0.03),
    (70, 0.20, 0.04),
    (60, 0.15, 0.05),
]

MAX_POSITION_PCT    = 0.40   # absolute hard cap — no single long > 40%
SHORT_POSITION_PCT  = 0.15   # all short entries fixed at 15%
LOW_VOLUME_CAP_PCT  = 0.10   # daily volume < LOW_VOLUME_THRESHOLD → cap at 10%
LOW_VOLUME_THRESHOLD = 100_000


class RiskManager:
    def __init__(self, broker):
        self.broker = broker

    def get_position_size_by_score(self, symbol, price, opp_score, volume=None):
        """
        Returns (qty, pct, trail_pct) based on opportunity score tier.

        Tier mapping:
          score 90-100 → 40% position, 2% trailing stop
          score 80-89  → 30% position, 3% trailing stop
          score 70-79  → 20% position, 4% trailing stop
          score 60-69  → 15% position, 5% trailing stop

        Low-volume override: if daily volume < 100,000, cap at 10% regardless.
        Hard cap: never exceed 40%.
        """
        portfolio = self.broker.get_portfolio_value()

        # Default to lowest tier (60-69)
        pct, trail_pct = 0.15, 0.05
        for min_score, tier_pct, tier_trail in SCORE_TIERS:
            if opp_score >= min_score:
                pct, trail_pct = tier_pct, tier_trail
                break

        # Low-volume override
        is_low_volume = volume is not None and volume < LOW_VOLUME_THRESHOLD
        if is_low_volume:
            original_pct = pct
            pct = min(pct, LOW_VOLUME_CAP_PCT)
            logger.info(
                f"⚠️ {symbol} low volume ({volume:,}) — position capped at "
                f"{pct*100:.0f}% (was {original_pct*100:.0f}% for score {opp_score})"
            )

        # Hard cap
        pct = min(pct, MAX_POSITION_PCT)

        # Regime multiplier — scales down size in bear/volatile markets
        regime_params = _regime_detector.get_params()
        multiplier = regime_params.get("position_size_multiplier", 1.0)
        pct = round(min(pct * multiplier, MAX_POSITION_PCT), 4)
        logger.info(f"[Risk] {regime_params['regime']} x{multiplier} → {pct*100:.1f}%")

        amount = portfolio * pct
        qty = amount / price
        return round(qty, 4), pct, trail_pct

    def get_short_position_size(self, symbol, price):
        """All short positions are fixed at 15% of portfolio."""
        portfolio = self.broker.get_portfolio_value()
        amount = portfolio * SHORT_POSITION_PCT
        qty = amount / price
        return round(qty, 4), SHORT_POSITION_PCT

    def check_global_stop_loss(self):
        portfolio = self.broker.get_portfolio_value()
        loss_pct = (config.INITIAL_CAPITAL - portfolio) / config.INITIAL_CAPITAL
        if loss_pct >= config.GLOBAL_STOP_LOSS_PCT:
            logger.warning(f"🔴 GLOBAL STOP LOSS triggered: -{loss_pct*100:.1f}%")
            return True
        return False

    def check_max_positions(self):
        positions = self.broker.get_positions()
        regime_params = _regime_detector.get_params()
        max_pos = regime_params.get("max_positions", config.MAX_POSITIONS)
        if len(positions) >= max_pos:
            logger.warning(f"⚠️ Max positions: {len(positions)}/{max_pos} ({regime_params['regime']} regime)")
            return False
        return True

    def calculate_stop_loss(self, entry_price, side):
        if side == "buy":
            return round(entry_price * (1 - config.TRADE_STOP_LOSS_PCT), 4)
        else:
            return round(entry_price * (1 + config.TRADE_STOP_LOSS_PCT), 4)

    def can_trade(self):
        if self.check_global_stop_loss():
            return False
        if not self.check_max_positions():
            return False
        return True



# ════════════════════════════════════════════════════════════
# FILE: config.py
# ════════════════════════════════════════════════════════════
import os

# Anthropic
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Alpaca
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"

# Trading rules

MAX_POSITION_PCT = 0.30
GLOBAL_STOP_LOSS_PCT = 0.20
TRADE_STOP_LOSS_PCT = 0.05
MAX_POSITIONS = 5

# Trailing stop distances — LONG positions (fraction below highest price)
TRAILING_STOP_CRYPTO = 0.03   # 3% for crypto longs
TRAILING_STOP_STOCK  = 0.05   # 5% for stocks/ETFs longs

# Trailing stop distances — SHORT positions (fraction above lowest price)
TRAILING_STOP_SHORT_CRYPTO = 0.03  # 3% for crypto shorts (paper only)
TRAILING_STOP_SHORT_STOCK  = 0.06  # 6% for stocks/ETF shorts

# Short selling rules
MAX_SHORT_SIZE_PCT   = 0.15   # Max 15% of portfolio per short position
SHORT_ENTRY_RSI_MIN  = 70     # RSI must be above this to short
SHORT_ENTRY_CONF_MAX = 0.30   # Claude confidence must be below this

# Partial profit taking
PARTIAL_PROFIT_PCT   = 0.03   # Take partial profits at +3% unrealised gain
PARTIAL_PROFIT_RATIO = 0.50   # Sell / cover this fraction of the position (50%)

# Universe
CRYPTO_SYMBOLS   = ["BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "DOGE/USD", "XRP/USD", "LINK/USD", "SHIB/USD", "MATIC/USD"]
STOCK_SYMBOLS    = ["AAPL", "NVDA", "TSLA", "META", "GOOGL", "MSFT", "AMD"]
ETF_SYMBOLS      = ["QQQ", "SPY", "ARKK"]
# Fixed blue-chip list always evaluated every cycle regardless of top movers
BLUECHIP_SYMBOLS = ["AAPL", "NVDA", "TSLA", "META", "GOOGL", "MSFT", "AMD", "QQQ", "SPY"]
ALL_SYMBOLS      = CRYPTO_SYMBOLS + STOCK_SYMBOLS + ETF_SYMBOLS

# Loop speeds
LOOP_INTERVAL_SECONDS      = 300   # Slow loop: full synthesis + movers refresh
FAST_LOOP_INTERVAL_SECONDS = 30    # Fast loop: position stops + score triggers
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "1000.0"))



# ════════════════════════════════════════════════════════════
# FILE: scanner.py
# ════════════════════════════════════════════════════════════
"""
scanner.py — Dynamic Top Movers + News Sentiment
"""
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import requests
import alpaca_trade_api as tradeapi
import config

logger = logging.getLogger(__name__)

ECONOMIC_EVENTS = [
    {"date": "2026-04-02", "event": "Fed Meeting"},
    {"date": "2026-04-03", "event": "Jobs Report (NFP)"},
    {"date": "2026-04-14", "event": "CPI Inflation"},
    {"date": "2026-04-29", "event": "Fed Meeting"},
    {"date": "2026-05-08", "event": "Jobs Report"},
    {"date": "2026-05-13", "event": "CPI Inflation"},
    {"date": "2026-06-17", "event": "Fed Meeting"},
]

# Confirmed Q1/Q2 2026 earnings dates — update as IRs announce
EARNINGS_DATES = {
    "TSLA":  "2026-04-22",
    "GOOGL": "2026-04-29",
    "MSFT":  "2026-04-30",
    "META":  "2026-04-30",
    "AAPL":  "2026-05-01",
    "AMD":   "2026-05-06",
    "NVDA":  "2026-05-28",
}

NEWS_FEEDS = [
    "https://feeds.marketwatch.com/marketwatch/topstories/",
    "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "https://news.google.com/rss/search?q=stock+market+economy+fed&hl=en-US&gl=US&ceid=US:en",
]

RSS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TradingAgent/1.0; +https://alpaca.markets)"
}

BULLISH_KEYWORDS = ["rate cut", "fed pivot", "strong jobs", "beat expectations",
    "record high", "bull market", "stimulus", "better than expected", "upgrade"]

BEARISH_KEYWORDS = ["tariff", "trade war", "recession", "rate hike", "layoffs",
    "miss expectations", "downgrade", "inflation surge", "trump tariff",
    "china trade", "worse than expected", "hawkish"]

HIGH_ALERT_KEYWORDS = ["trump", "federal reserve", "emergency rate",
    "war", "sanctions", "default", "collapse", "crisis"]

# Only keep headlines relevant to financial markets — excludes personal finance,
# tax advice, lifestyle articles, etc.
FINANCE_FILTER_KEYWORDS = [
    "stock", "market", "share", "equity", "index", "dow", "nasdaq", "s&p", "spy",
    "fed", "federal reserve", "interest rate", "rate cut", "rate hike", "powell",
    "earnings", "revenue", "profit", "quarter", "ipo", "merger", "acquisition",
    "crypto", "bitcoin", "ethereum", "blockchain", "coin",
    "tariff", "trade war", "sanction", "import duty", "export",
    "inflation", "cpi", "pce", "gdp", "recession", "jobs report", "unemployment",
    "oil", "gold", "commodity", "bond", "treasury", "yield", "debt",
    "dollar", "currency", "forex", "yuan", "yen", "euro",
    "wall street", "hedge fund", "bank", "jpmorgan", "goldman", "morgan stanley",
    "apple", "nvidia", "tesla", "meta", "google", "amazon", "microsoft", "intel",
    "tech stock", "semiconductor", "chip", "ai stock",
    "economy", "economic", "fiscal", "monetary", "deficit",
    "opec", "china trade", "supply chain", "manufacturing",
    "rally", "selloff", "correction", "bull market", "bear market",
    "war", "sanctions", "geopolit", "trump", "biden", "white house policy",
]

class MarketScanner:
    def __init__(self):
        self.api = tradeapi.REST(
            config.ALPACA_API_KEY,
            config.ALPACA_SECRET_KEY,
            config.ALPACA_BASE_URL
        )
        self._news_cache = {"headlines": [], "sentiment": "neutral", "cached_at": None}
        self._movers_cache = {"symbols": [], "cached_at": None}
        logger.info("✅ MarketScanner initialized")

    def get_top_movers(self, top_n=6):
        now = datetime.now(timezone.utc)
        if self._movers_cache["cached_at"]:
            age = (now - self._movers_cache["cached_at"]).total_seconds()
            if age < 300 and self._movers_cache["symbols"]:
                return self._movers_cache["symbols"]
        try:
            assets = self.api.list_assets(status="active", asset_class="us_equity")
            tradeable = [a for a in assets if a.tradable]
            symbols = [a.symbol for a in tradeable[:2000]]
            snapshots = self.api.get_snapshots(symbols)
            gappers = []
            movers  = []
            for symbol, snap in snapshots.items():
                try:
                    if not snap.daily_bar or not snap.prev_daily_bar:
                        continue
                    prev       = snap.prev_daily_bar.close
                    curr       = snap.daily_bar.close
                    prev_vol   = snap.prev_daily_bar.volume or 1
                    volume     = snap.daily_bar.volume
                    if prev <= 0:
                        continue
                    change_pct   = ((curr - prev) / prev) * 100
                    volume_ratio = round(volume / prev_vol, 1) if prev_vol > 0 else 0
                    is_microcap  = 0.50 <= curr <= 10.0

                    # Qualify as a mover:
                    # - Standard:  |change| ≥ 3% AND volume > 50 000
                    # - Micro/small cap ($0.50–$10): volume must exceed 500 000 (higher bar)
                    if is_microcap:
                        qualifies = abs(change_pct) >= 3 and volume > 500_000
                    else:
                        qualifies = abs(change_pct) >= 3 and volume > 50_000

                    if not qualifies:
                        continue

                    entry = {
                        "symbol":       symbol,
                        "price":        round(curr, 4),
                        "change_pct":   round(change_pct, 2),
                        "volume":       volume,
                        "volume_ratio": volume_ratio,
                        "direction":    "up" if change_pct > 0 else "down",
                        "is_gapper":    False,
                        "is_microcap":  is_microcap,
                    }

                    # GAPPER ALERT: >50% gain on >5× average volume
                    if change_pct >= 50 and volume_ratio >= 5:
                        entry["is_gapper"] = True
                        logger.info(
                            f"🚨 GAPPER ALERT: {symbol} +{change_pct:.1f}% "
                            f"volume={volume_ratio}x — potential 100-200% move"
                        )
                        gappers.append(entry)
                    else:
                        movers.append(entry)

                except Exception:
                    continue

            # Sort each bucket by magnitude; gappers always win
            gappers.sort(key=lambda x: x["change_pct"], reverse=True)
            movers.sort(key=lambda x: abs(x["change_pct"]), reverse=True)

            # Gappers all included; regular movers capped at top_n
            top = gappers + movers[:top_n]
            self._movers_cache = {"symbols": top, "cached_at": now}

            gap_labels   = [f"🚨{m['symbol']} +{m['change_pct']:.1f}% {m['volume_ratio']}x" for m in gappers]
            mover_labels = [f"{m['symbol']} {m['change_pct']:+.1f}%" for m in movers[:top_n]]
            if gap_labels:
                logger.info(f"🚨 GAPPERS: {gap_labels}")
            logger.info(f"🔥 Top movers: {mover_labels}")
            return top
        except Exception as e:
            logger.error(f"get_top_movers error: {e}")
            return []

    def get_dynamic_watchlist(self):
        from strategy import is_good_stock_window
        seen = set()
        watchlist = []

        def _add(symbols):
            for s in symbols:
                if s not in seen:
                    seen.add(s)
                    watchlist.append(s)

        if is_good_stock_window():
            movers = self.get_top_movers(top_n=6)

            # Split movers into gappers (highest priority) and regular movers
            gapper_symbols = [m["symbol"] for m in movers if m.get("is_gapper")]
            regular_symbols = [m["symbol"] for m in movers if not m.get("is_gapper")]

            _add(gapper_symbols)          # 1. GAPPERS — absolute priority
            _add(regular_symbols)         # 2. Regular top movers
            _add(config.BLUECHIP_SYMBOLS) # 3. Blue chips — always present

        _add(config.CRYPTO_SYMBOLS)       # 4. Crypto — always present

        gappers_in = [s for s in watchlist if s in {m["symbol"] for m in self._movers_cache.get("symbols", []) if m.get("is_gapper")}]
        bc_in_list  = [s for s in config.BLUECHIP_SYMBOLS if s in seen]
        mover_only  = [s for s in watchlist if s not in config.BLUECHIP_SYMBOLS and s not in config.CRYPTO_SYMBOLS and s not in gappers_in]
        logger.info(
            f"📋 Watchlist ({len(watchlist)}): "
            f"gappers={gappers_in} | "
            f"movers={mover_only} | "
            f"bluechips={bc_in_list} | "
            f"crypto={[s for s in watchlist if s in config.CRYPTO_SYMBOLS]}"
        )
        return watchlist

    def fetch_news_headlines(self):
        now = datetime.now(timezone.utc)
        if self._news_cache["cached_at"]:
            age = (now - self._news_cache["cached_at"]).total_seconds()
            if age < 180:
                return self._news_cache["headlines"]
        headlines = []
        for feed_url in NEWS_FEEDS:
            try:
                resp = requests.get(feed_url, timeout=8, headers=RSS_HEADERS)
                if resp.status_code != 200:
                    logger.warning(f"RSS {resp.status_code}: {feed_url[:60]}")
                    continue
                root = ET.fromstring(resp.content)
                fetched = 0
                for item in root.findall(".//item"):
                    title = item.find("title")
                    if title is not None and title.text:
                        text = title.text.strip()
                        # Google News wraps titles in CDATA — clean angle-bracket artefacts
                        text_lower = text.lower()
                        is_finance = any(kw in text_lower for kw in FINANCE_FILTER_KEYWORDS)
                        if text and "<" not in text and is_finance:
                            headlines.append(text)
                            fetched += 1
                    if fetched >= 5:
                        break
                logger.debug(f"RSS OK ({fetched} headlines): {feed_url[:60]}")
            except Exception as e:
                logger.warning(f"RSS error ({feed_url[:40]}): {e}")
        self._news_cache["headlines"] = headlines
        self._news_cache["cached_at"] = now
        return headlines

    def analyze_sentiment(self):
        headlines = self.fetch_news_headlines()
        if not headlines:
            return {"sentiment": "neutral", "score": 0, "alerts": []}
        score = 0
        alerts = []
        text = " ".join(headlines).lower()
        for kw in BULLISH_KEYWORDS:
            if kw in text:
                score += 1
        for kw in BEARISH_KEYWORDS:
            if kw in text:
                score -= 1
        for kw in HIGH_ALERT_KEYWORDS:
            if kw in text:
                alerts.append(f"⚡ HIGH ALERT: '{kw.upper()}' detected in headlines")
        if score >= 3: sentiment = "very_bullish"
        elif score >= 1: sentiment = "bullish"
        elif score <= -3: sentiment = "very_bearish"
        elif score <= -1: sentiment = "bearish"
        else: sentiment = "neutral"
        logger.info(f"📰 Sentiment: {sentiment} (score: {score})")
        ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
        return {"sentiment": sentiment, "score": score, "alerts": alerts, "headlines": headlines[:3], "ts": ts}

    def check_economic_calendar(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for event in ECONOMIC_EVENTS:
            if event["date"] == today:
                return {"event": event["event"], "timing": "today",
                    "note": f"⚡ {event['event']} TODAY — high volatility expected"}
        return {"event": None, "note": ""}

    def get_earnings_alerts(self, symbols=None):
        """
        Return earnings alerts for stocks within ±1 day of their earnings date.
          type='earnings_day'  → reports TODAY (skip the stock)
          type='pre_earnings'  → reports in 1–2 days (cut position to 10%)
          type='post_earnings' → reported yesterday (watch for gap play)
        """
        from datetime import date as date_cls
        today = datetime.now(timezone.utc).date()
        alerts = []
        for symbol, date_str in EARNINGS_DATES.items():
            if symbols and symbol not in symbols:
                continue
            edate     = datetime.strptime(date_str, "%Y-%m-%d").date()
            days_away = (edate - today).days
            if days_away == 0:
                alerts.append({"symbol": symbol, "days_away": 0,  "type": "earnings_day"})
            elif 1 <= days_away <= 2:
                alerts.append({"symbol": symbol, "days_away": days_away, "type": "pre_earnings"})
            elif days_away == -1:
                alerts.append({"symbol": symbol, "days_away": -1, "type": "post_earnings"})
        return alerts

    def build_market_context(self, symbol=None):
        sentiment = self.analyze_sentiment()
        calendar  = self.check_economic_calendar()
        movers    = self.get_top_movers(top_n=6)
        earnings  = self.get_earnings_alerts()

        lines = ["=== MARKET INTELLIGENCE ==="]
        emoji = {"very_bullish":"🚀","bullish":"🟢","neutral":"⚪","bearish":"🔴","very_bearish":"💀"}
        lines.append(f"📰 NEWS: {emoji.get(sentiment['sentiment'],'⚪')} {sentiment['sentiment'].upper()} (score: {sentiment['score']})")
        for alert in sentiment.get("alerts", []):
            lines.append(f"  {alert}")
        if calendar["event"]:
            lines.append(f"📅 CALENDAR: {calendar['note']}")

        # Earnings proximity warnings — passed to Claude so it can factor in volatility
        for ea in earnings:
            if ea["type"] == "earnings_day":
                lines.append(
                    f"🚨 EARNINGS DAY: {ea['symbol']} reports TODAY — "
                    f"DO NOT ENTER any position, risk is too high"
                )
            elif ea["type"] == "pre_earnings":
                lines.append(
                    f"⚠️ EARNINGS ALERT: {ea['symbol']} reports in {ea['days_away']} day(s) — "
                    f"expect high volatility, reduce position size to 10% max"
                )
            elif ea["type"] == "post_earnings":
                lines.append(
                    f"📈 POST-EARNINGS: {ea['symbol']} reported yesterday — "
                    f"if gapping up >5% on high volume, treat as priority gapper (score 90+, up to 40% position)"
                )

        if movers:
            lines.append("🔥 TOP MOVERS:")
            for m in movers[:6]:
                vol_str = f" vol={m.get('volume_ratio','')}x" if m.get("volume_ratio") else ""
                tag     = " 🚨 GAPPER — prioritize, potential 100-200% move" if m.get("is_gapper") else ""
                lines.append(f"  {'↑' if m['direction']=='up' else '↓'} {m['symbol']}: {m['change_pct']:+.1f}%{vol_str}{tag}")
        if symbol:
            sm = next((m for m in movers if m["symbol"] == symbol), None)
            if sm:
                if sm.get("is_gapper"):
                    lines.append(
                        f"🚨 {symbol} IS A CONFIRMED GAPPER: {sm['change_pct']:+.1f}% "
                        f"volume={sm.get('volume_ratio','')}x average — "
                        f"score this 90+, use max position size, set tight trailing stop"
                    )
                else:
                    lines.append(f"⭐ {symbol} IS A TOP MOVER: {sm['change_pct']:+.1f}%")
        if sentiment["sentiment"] in ["very_bullish", "bullish"]:
            lines.append("💡 Conditions favorable for LONG positions")
        elif sentiment["sentiment"] in ["very_bearish", "bearish"]:
            lines.append("💡 Conditions favorable for SHORT positions — tighten stops on longs")
        else:
            lines.append("💡 Neutral market — stick to technical signals only")
        return "\n".join(lines)



# ════════════════════════════════════════════════════════════
# FILE: memory.py
# ════════════════════════════════════════════════════════════
import sqlite3
import json
import logging
from datetime import datetime, timezone
from typing import Optional
from contextlib import contextmanager

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT UNIQUE NOT NULL,
    alpaca_order_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL,
    exit_price REAL,
    stop_loss REAL,
    take_profit REAL,
    status TEXT NOT NULL DEFAULT 'open',
    pnl REAL,
    pnl_pct REAL,
    entry_at TEXT NOT NULL,
    exit_at TEXT,
    hold_duration_min REAL,
    close_reason TEXT,
    market_context TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS agent_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT,
    symbol TEXT,
    decision TEXT NOT NULL,
    confidence REAL,
    reasoning TEXT NOT NULL,
    market_data TEXT,
    decided_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS trade_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    outcome TEXT NOT NULL,
    pnl REAL,
    analysis TEXT NOT NULL,
    lessons TEXT,
    mistakes TEXT,
    strategy_adj TEXT,
    analyzed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS agent_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    value TEXT NOT NULL,
    category TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

class TradingMemory:
    def __init__(self, db_path="trading_memory.db"):
        self.db_path = db_path
        self._init_db()
        logger.info(f"✅ TradingMemory ready: {db_path}")

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise
        finally:
            conn.close()

    def log_trade_open(self, trade_id, symbol, side, qty, entry_price,
                       stop_loss=None, take_profit=None,
                       alpaca_order_id=None, market_context=None):
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO trades (trade_id,alpaca_order_id,symbol,side,qty,entry_price,stop_loss,take_profit,status,entry_at,market_context) VALUES (?,?,?,?,?,?,?,?,'open',?,?)",
                    (trade_id, alpaca_order_id, symbol, side, qty, entry_price,
                     stop_loss, take_profit,
                     datetime.now(timezone.utc).isoformat(),
                     json.dumps(market_context) if market_context else None)
                )
            return True
        except Exception as e:
            logger.error(f"log_trade_open error: {e}")
            return False

    def log_trade_close(self, trade_id, exit_price, close_reason, pnl=None, pnl_pct=None):
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT entry_at, entry_price, qty, side FROM trades WHERE trade_id=?",
                    (trade_id,)
                ).fetchone()
                if not row:
                    return False
                exit_at = datetime.now(timezone.utc)
                entry_at = datetime.fromisoformat(row["entry_at"])
                duration = (exit_at - entry_at).total_seconds() / 60
                if pnl is None:
                    m = 1 if row["side"] == "buy" else -1
                    pnl = (exit_price - row["entry_price"]) * row["qty"] * m
                if pnl_pct is None and row["entry_price"] > 0:
                    pnl_pct = (pnl / (row["entry_price"] * row["qty"])) * 100
                conn.execute(
                    "UPDATE trades SET exit_price=?,exit_at=?,hold_duration_min=?,close_reason=?,pnl=?,pnl_pct=?,status='closed' WHERE trade_id=?",
                    (exit_price, exit_at.isoformat(), duration, close_reason,
                     round(pnl,4), round(pnl_pct,4) if pnl_pct else None, trade_id)
                )
            return True
        except Exception as e:
            logger.error(f"log_trade_close error: {e}")
            return False

    def get_open_trades(self):
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM trades WHERE status='open' ORDER BY entry_at DESC").fetchall()
            return [dict(r) for r in rows]

    def get_recent_trades(self, limit=20, symbol=None):
        with self._conn() as conn:
            if symbol:
                rows = conn.execute("SELECT * FROM trades WHERE symbol=? ORDER BY entry_at DESC LIMIT ?", (symbol, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM trades ORDER BY entry_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_closed_trades_unanalyzed(self):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT t.* FROM trades t LEFT JOIN trade_analyses ta ON t.trade_id=ta.trade_id WHERE t.status='closed' AND ta.trade_id IS NULL ORDER BY t.exit_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def log_decision(self, decision, reasoning, symbol=None, trade_id=None, confidence=None, market_data=None):
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO agent_decisions (trade_id,symbol,decision,confidence,reasoning,market_data) VALUES (?,?,?,?,?,?)",
                    (trade_id, symbol, decision, confidence, reasoning,
                     json.dumps(market_data) if market_data else None)
                )
            return True
        except Exception as e:
            logger.error(f"log_decision error: {e}")
            return False

    def get_recent_decisions(self, limit=10, symbol=None):
        with self._conn() as conn:
            if symbol:
                rows = conn.execute("SELECT * FROM agent_decisions WHERE symbol=? ORDER BY decided_at DESC LIMIT ?", (symbol, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM agent_decisions ORDER BY decided_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def save_trade_analysis(self, trade_id, symbol, outcome, pnl, analysis, lessons=None, mistakes=None, strategy_adj=None):
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO trade_analyses (trade_id,symbol,outcome,pnl,analysis,lessons,mistakes,strategy_adj) VALUES (?,?,?,?,?,?,?,?)",
                    (trade_id, symbol, outcome, pnl, analysis,
                     json.dumps(lessons) if lessons else None,
                     json.dumps(mistakes) if mistakes else None,
                     strategy_adj)
                )
            return True
        except Exception as e:
            logger.error(f"save_trade_analysis error: {e}")
            return False

    def get_analyses(self, limit=10):
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM trade_analyses ORDER BY analyzed_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def compute_performance_stats(self, symbol=None):
        with self._conn() as conn:
            q = "SELECT * FROM trades WHERE status='closed'"
            params = []
            if symbol:
                q += " AND symbol=?"
                params.append(symbol)
            rows = conn.execute(q, params).fetchall()
            trades = [dict(r) for r in rows]
        if not trades:
            return {"total_trades": 0}
        pnls = [t["pnl"] for t in trades if t["pnl"] is not None]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_win = sum(wins) if wins else 0
        gross_loss = abs(sum(losses)) if losses else 0
        pf = gross_win / gross_loss if gross_loss > 0 else 999
        cumulative = peak = max_dd = 0
        for p in pnls:
            cumulative += p
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd
        by_asset = {}
        for t in trades:
            s = t["symbol"]
            if t["pnl"] is not None:
                by_asset.setdefault(s, []).append(t["pnl"])
        asset_pnl = {s: sum(v) for s, v in by_asset.items()}
        return {
            "total_trades": len(pnls),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate": round(len(wins)/len(pnls)*100, 2) if pnls else 0,
            "total_pnl": round(sum(pnls), 2),
            "avg_win": round(sum(wins)/len(wins), 2) if wins else 0,
            "avg_loss": round(sum(losses)/len(losses), 2) if losses else 0,
            "profit_factor": round(pf, 2),
            "max_drawdown": round(max_dd, 2),
            "best_asset": max(asset_pnl, key=asset_pnl.get) if asset_pnl else None,
            "worst_asset": min(asset_pnl, key=asset_pnl.get) if asset_pnl else None,
            "asset_pnl": {k: round(v,2) for k,v in asset_pnl.items()},
        }

    def set_memory(self, key, value, category="strategy"):
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO agent_memory (key,value,category,updated_at) VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,category=excluded.category,updated_at=excluded.updated_at",
                    (key, json.dumps(value), category, datetime.now(timezone.utc).isoformat())
                )
            return True
        except Exception as e:
            logger.error(f"set_memory error: {e}")
            return False

    def get_memory(self, key, default=None):
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM agent_memory WHERE key=?", (key,)).fetchone()
            if row:
                try:
                    return json.loads(row["value"])
                except:
                    return row["value"]
            return default

    def get_all_memory(self, category=None):
        with self._conn() as conn:
            if category:
                rows = conn.execute("SELECT key,value,category,updated_at FROM agent_memory WHERE category=?", (category,)).fetchall()
            else:
                rows = conn.execute("SELECT key,value,category,updated_at FROM agent_memory").fetchall()
            result = {}
            for row in rows:
                try:
                    result[row["key"]] = {"value": json.loads(row["value"]), "category": row["category"]}
                except:
                    result[row["key"]] = {"value": row["value"], "category": row["category"]}
            return result

    def get_context_for_agent(self, symbol=None):
        stats = self.compute_performance_stats(symbol)
        recent = self.get_recent_trades(limit=20, symbol=symbol)
        memory = self.get_all_memory()
        lines = ["=== AGENT MEMORY ==="]
        if stats.get("total_trades", 0) > 0:
            lines.append(f"Performance: {stats['total_trades']} trades | Win rate: {stats['win_rate']}% | P&L: ${stats['total_pnl']}")
        if recent:
            lines.append("Recent trades:")
            for t in recent:
                pnl_str = f"${t['pnl']:.2f}" if t.get("pnl") else "open"
                lines.append(f"  {t['symbol']} {t['side']} | {pnl_str}")
        strategy = {k: v["value"] for k,v in memory.items() if v.get("category") == "strategy"}
        if strategy:
            lines.append("Strategy insights:")
            for k,v in list(strategy.items())[:3]:
                lines.append(f"  • {k}: {v}")
        return "\n".join(lines)



# ════════════════════════════════════════════════════════════
# FILE: analyzer.py
# ════════════════════════════════════════════════════════════
import json
import logging
from datetime import datetime, timezone
from typing import Optional
import anthropic
from memory import TradingMemory

logger = logging.getLogger(__name__)
CLAUDE_MODEL = "claude-sonnet-4-20250514"

class TradeAnalyzer:
    def __init__(self, memory: TradingMemory, api_key=None):
        self.memory = memory
        self.client = anthropic.Anthropic(api_key=api_key)
        logger.info("✅ TradeAnalyzer ready")

    def analyze_trade(self, trade: dict):
        if not trade.get("exit_price") or trade.get("status") != "closed":
            return None
        pnl = trade.get("pnl", 0) or 0
        outcome = "win" if pnl > 0 else ("loss" if pnl < 0 else "breakeven")
        original_decisions = self.memory.get_recent_decisions(limit=50, symbol=trade["symbol"])
        entry_decision = next(
            (d for d in original_decisions if d.get("trade_id") == trade.get("trade_id")), None
        )
        prompt = f"""You are a trading expert analyzing your own past trades to improve.

Trade details:
- Symbol: {trade['symbol']}
- Side: {trade['side'].upper()}
- Entry: ${trade.get('entry_price')}
- Exit: ${trade.get('exit_price')}
- P&L: ${pnl:.2f} ({trade.get('pnl_pct', 0):.2f}%)
- Result: {outcome.upper()}
- Duration: {trade.get('hold_duration_min', 0):.1f} minutes
- Close reason: {trade.get('close_reason')}
- Original reasoning: {entry_decision['reasoning'] if entry_decision else 'N/A'}

Respond ONLY with valid JSON, no markdown:
{{
  "analysis": "3-5 sentence narrative analysis",
  "outcome_reason": "1 sentence explaining the result",
  "mistakes": ["mistake 1", "mistake 2"],
  "lessons": ["actionable lesson 1", "actionable lesson 2"],
  "strategy_adjustments": "specific adjustments for future trades",
  "would_take_same_trade": true,
  "key_insight": "most important insight in 1 sentence"
}}"""
        try:
            response = self.client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=1000,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data = json.loads(raw.strip())
            self.memory.save_trade_analysis(
                trade_id=trade["trade_id"],
                symbol=trade["symbol"],
                outcome=outcome,
                pnl=pnl,
                analysis=data.get("analysis", ""),
                lessons=data.get("lessons", []),
                mistakes=data.get("mistakes", []),
                strategy_adj=data.get("strategy_adjustments", "")
            )
            if data.get("key_insight"):
                self.memory.set_memory(
                    f"insight_{trade['symbol']}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}",
                    data["key_insight"], category="insight"
                )
            if data.get("strategy_adjustments"):
                self.memory.set_memory(
                    f"strategy_{trade['symbol']}",
                    data["strategy_adjustments"], category="strategy"
                )
            logger.info(f"✅ Analysis done for {trade['trade_id']} ({outcome})")
            return data
        except Exception as e:
            logger.error(f"analyze_trade error: {e}")
            return None

    def run_pending_analyses(self):
        pending = self.memory.get_closed_trades_unanalyzed()
        if not pending:
            return 0
        count = 0
        for trade in pending:
            if self.analyze_trade(trade):
                count += 1
        logger.info(f"✅ {count}/{len(pending)} analyses done")
        return count

    def generate_performance_report(self, period="weekly"):
        stats = self.memory.compute_performance_stats()
        if stats.get("total_trades", 0) == 0:
            return "No closed trades yet."
        analyses = self.memory.get_analyses(limit=10)
        lessons = []
        for a in analyses:
            if a.get("lessons"):
                try:
                    lessons.extend(json.loads(a["lessons"]) if isinstance(a["lessons"], str) else a["lessons"])
                except:
                    pass
        prompt = f"""Analyze this trading agent's {period} performance and write a clear report in French.

Stats: {json.dumps(stats, indent=2)}
Recent lessons: {chr(10).join(f'• {l}' for l in lessons[:6])}

Write a structured report with:
1. Executive summary (2-3 sentences)
2. Strengths
3. Weaknesses
4. Assets to favor / avoid
5. Concrete recommendations
6. Score out of 10 with justification

Be direct and critical."""
        try:
            response = self.client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=1500,
                messages=[{"role": "user", "content": prompt}]
            )
            return response.content[0].text.strip()
        except Exception as e:
            logger.error(f"generate_performance_report error: {e}")
            return f"Error: {e}"

    def detect_performance_anomalies(self):
        alerts = []
        stats = self.memory.compute_performance_stats()
        recent = self.memory.get_recent_trades(limit=10)
        if not recent or stats.get("total_trades", 0) < 3:
            return alerts
        if stats.get("win_rate", 50) < 30:
            alerts.append(f"⚠️ Win rate critical: {stats['win_rate']}% (threshold: 30%)")
        last_pnls = [t.get("pnl", 0) or 0 for t in recent if t.get("status") == "closed"]
        if len(last_pnls) >= 3 and all(p < 0 for p in last_pnls[:3]):
            alerts.append(f"🔴 3 consecutive losses. Total: ${sum(last_pnls[:3]):.2f}")
        if stats.get("max_drawdown", 0) > 150:
            alerts.append(f"⚠️ High drawdown: ${stats['max_drawdown']:.2f}")
        if 0 < stats.get("profit_factor", 1) < 1:
            alerts.append(f"📉 Profit factor < 1 ({stats['profit_factor']})")
        return alerts



# ════════════════════════════════════════════════════════════
# FILE: notifier.py
# ════════════════════════════════════════════════════════════
import logging
import os
import smtplib
import threading
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional
from memory import TradingMemory
from analyzer import TradeAnalyzer

logger = logging.getLogger(__name__)

def _get_cfg():
    return {
        "host": os.getenv("SMTP_HOST","smtp.gmail.com"),
        "port": int(os.getenv("SMTP_PORT","587")),
        "user": os.getenv("SMTP_USER",""),
        "pass": os.getenv("SMTP_PASS",""),
        "to":   os.getenv("NOTIFY_EMAIL",""),
    }

def _send(subject, html):
    cfg = _get_cfg()
    if not cfg["user"] or not cfg["pass"] or not cfg["to"]:
        logger.warning(f"[EMAIL SKIPPED] {subject}")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"Trading Agent <{cfg['user']}>"
        msg["To"] = cfg["to"]
        msg.attach(MIMEText(html,"html","utf-8"))
        with smtplib.SMTP(cfg["host"], cfg["port"]) as s:
            s.ehlo(); s.starttls()
            s.login(cfg["user"], cfg["pass"])
            s.sendmail(cfg["user"], cfg["to"], msg.as_string())
        logger.info(f"✅ Email sent: {subject}")
        return True
    except Exception as e:
        logger.error(f"Email error: {e}")
        return False

def _html(title, content):
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>
body{{margin:0;padding:0;background:#080c10;font-family:'Courier New',monospace;color:#c8d6e5}}
.w{{max-width:600px;margin:0 auto;padding:24px 16px}}
.h{{border-bottom:2px solid #00ff88;padding-bottom:16px;margin-bottom:24px}}
.logo{{font-size:20px;font-weight:700;color:#00ff88;letter-spacing:0.15em}}
.sub{{font-size:11px;color:#4a5568;margin-top:4px}}
.st{{font-size:10px;letter-spacing:0.2em;text-transform:uppercase;color:#4a5568;border-bottom:1px solid #1a2030;padding-bottom:6px;margin:20px 0 12px}}
.krow{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}}
.k{{background:#0d1117;border:1px solid #1a2030;border-radius:4px;padding:12px 16px;flex:1;min-width:100px;text-align:center}}
.kl{{font-size:9px;letter-spacing:0.12em;text-transform:uppercase;color:#4a5568}}
.kv{{font-size:22px;font-weight:700;margin-top:4px}}
.pos{{color:#00ff88}} .neg{{color:#ff3860}} .neu{{color:#4fc3f7}}
.alert{{background:rgba(255,56,96,0.1);border:1px solid #ff3860;border-radius:4px;padding:16px;color:#ff3860;margin-bottom:16px}}
.lesson{{padding:6px 0 6px 12px;border-left:2px solid #00ff88;font-size:11px;color:#4fc3f7;margin-bottom:6px}}
.foot{{font-size:10px;color:#4a5568;text-align:center;border-top:1px solid #1a2030;padding-top:16px;margin-top:24px}}
</style></head>
<body><div class="w">
<div class="h"><div class="logo">AGENT/TERMINAL</div>
<div class="sub">{title} — {datetime.now(timezone.utc).strftime('%d %b %Y %H:%M UTC')}</div></div>
{content}
<div class="foot">Email automatique — Paper trading uniquement</div>
</div></body></html>"""

def _pcolor(v):
    if v is None: return ""
    return "pos" if v >= 0 else "neg"

def _fpnl(v):
    if v is None: return "—"
    return f"{'+' if v>=0 else ''}${v:.2f}"

class TradingNotifier:
    def __init__(self, memory: TradingMemory, analyzer: TradeAnalyzer):
        self.memory = memory
        self.analyzer = analyzer
        self._stop = threading.Event()
        logger.info("✅ TradingNotifier ready")

    def send_daily_summary(self):
        import json
        stats = self.memory.compute_performance_stats()
        recent = self.memory.get_recent_trades(limit=10)
        today = datetime.now(timezone.utc).date()
        daily = [t for t in recent if t.get("entry_at") and datetime.fromisoformat(t["entry_at"]).date()==today]
        daily_pnl = sum(t.get("pnl") or 0 for t in daily if t.get("pnl") is not None)
        pnl = stats.get("total_pnl",0) or 0
        analyses = self.memory.get_analyses(limit=3)
        lessons = []
        for a in analyses:
            if a.get("lessons"):
                try:
                    ls = json.loads(a["lessons"]) if isinstance(a["lessons"],str) else a["lessons"]
                    lessons.extend(ls[:2])
                except: pass
        anomalies = self.analyzer.detect_performance_anomalies()
        alert_html = ""
        if anomalies:
            alert_html = f'<div class="alert"><strong>⚠ ALERTES</strong><br><br>{"<br>".join(anomalies)}</div>'
        rows = ""
        for t in daily[:8]:
            p = t.get("pnl"); cls = _pcolor(p)
            rows += f'<tr><td><strong>{t["symbol"]}</strong></td><td>{t["side"].upper()}</td><td class="{cls}">{_fpnl(p)}</td><td style="color:#4a5568">{t.get("close_reason","—")}</td></tr>'
        if not rows:
            rows = '<tr><td colspan="4" style="color:#4a5568;text-align:center">Aucun trade aujourd\'hui</td></tr>'
        lessons_html = "".join(f'<div class="lesson">{l}</div>' for l in lessons[:5])
        content = f"""{alert_html}
<div class="st">Aujourd'hui</div>
<div class="krow">
<div class="k"><div class="kl">P&L aujourd'hui</div><div class="kv {_pcolor(daily_pnl)}">{_fpnl(daily_pnl)}</div></div>
<div class="k"><div class="kl">Trades</div><div class="kv neu">{len(daily)}</div></div>
</div>
<div class="st">Global</div>
<div class="krow">
<div class="k"><div class="kl">P&L Total</div><div class="kv {_pcolor(pnl)}">{_fpnl(pnl)}</div></div>
<div class="k"><div class="kl">Win Rate</div><div class="kv neu">{stats.get('win_rate',0):.1f}%</div></div>
<div class="k"><div class="kl">Trades</div><div class="kv">{stats.get('total_trades',0)}</div></div>
<div class="k"><div class="kl">Drawdown</div><div class="kv neg">-${stats.get('max_drawdown',0):.2f}</div></div>
</div>
<div class="st">Trades du jour</div>
<table style="width:100%;border-collapse:collapse;font-size:11px">
<thead><tr><th style="text-align:left;padding:6px;color:#4a5568">Symbol</th><th style="text-align:left;padding:6px;color:#4a5568">Dir.</th><th style="text-align:left;padding:6px;color:#4a5568">P&L</th><th style="text-align:left;padding:6px;color:#4a5568">Raison</th></tr></thead>
<tbody>{rows}</tbody></table>
{f'<div class="st">Leçons récentes</div>{lessons_html}' if lessons_html else ''}"""
        return _send(f"[Trading Agent] Résumé {today.strftime('%d/%m/%Y')} — {_fpnl(daily_pnl)}", _html("Résumé quotidien", content))

    def send_stop_loss_alert(self, current_capital, initial_capital=1000.0):
        loss = initial_capital - current_capital
        loss_pct = (loss / initial_capital) * 100
        content = f"""<div class="alert"><strong>🔴 STOP LOSS GLOBAL DÉCLENCHÉ</strong><br><br>
Capital en baisse de <strong>{loss_pct:.1f}%</strong>. Toutes les positions ont été fermées.</div>
<div class="krow">
<div class="k"><div class="kl">Capital initial</div><div class="kv">${initial_capital:.2f}</div></div>
<div class="k"><div class="kl">Capital actuel</div><div class="kv neg">${current_capital:.2f}</div></div>
<div class="k"><div class="kl">Perte</div><div class="kv neg">-${loss:.2f}</div></div>
<div class="k"><div class="kl">Perte %</div><div class="kv neg">-{loss_pct:.1f}%</div></div>
</div>"""
        return _send(f"🚨 [Trading Agent] STOP LOSS — Perte: -${loss:.2f} (-{loss_pct:.1f}%)", _html("🚨 STOP LOSS GLOBAL", content))

    def send_test_email(self):
        content = """<div class="st">Configuration OK</div>
<div class="lesson">Résumé quotidien chaque soir à 20h UTC</div>
<div class="lesson">Rapport hebdomadaire chaque lundi matin</div>
<div class="lesson">Alerte immédiate si stop loss global déclenché</div>
<div class="lesson">Alerte si 3 pertes consécutives détectées</div>"""
        return _send("[Trading Agent] ✅ Email de test — OK", _html("Test de configuration", content))

    def _already_sent_today(self, key: str, date_str: str) -> bool:
        """Check SQLite so restarts don't re-send emails for the same date."""
        return self.memory.get_memory(key) == date_str

    def _mark_sent(self, key: str, date_str: str):
        self.memory.set_memory(key, date_str, category="email_scheduler")

    def start_scheduler(self, daily_hour_utc=20):
        self._stop.clear()
        def loop():
            while not self._stop.is_set():
                now = datetime.now(timezone.utc)
                today = now.strftime("%Y-%m-%d")
                week  = now.strftime("%Y-W%W")

                if now.hour == daily_hour_utc:
                    if not self._already_sent_today("email.last_daily_sent", today):
                        self.send_daily_summary()
                        self._mark_sent("email.last_daily_sent", today)

                if now.weekday() == 0 and now.hour == 8:
                    if not self._already_sent_today("email.last_weekly_sent", week):
                        report = self.analyzer.generate_performance_report("weekly")
                        _send("[Trading Agent] Rapport hebdomadaire",
                              _html("Rapport hebdo",
                                    f'<div class="st">Analyse Claude</div>'
                                    f'<p style="font-size:12px;line-height:1.7;white-space:pre-wrap">{report}</p>'))
                        self._mark_sent("email.last_weekly_sent", week)

                self._stop.wait(30)
        threading.Thread(target=loop, daemon=True, name="notifier").start()
        logger.info(f"📅 Scheduler started — daily at {daily_hour_utc}h UTC (persisted dedup via SQLite)")



# ════════════════════════════════════════════════════════════
# FILE: news_intelligence.py
# ════════════════════════════════════════════════════════════
"""
news_intelligence.py — Advanced News Intelligence
Tier 1-4 classification, Trump direction detection, earnings whisper,
post-earnings gap protocol. Upgrades scanner.py keyword detection
into real market impact analysis.
"""
import logging
import re
from datetime import datetime, timezone
from typing import Optional
import requests
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

# ── Tier definitions ──────────────────────────────────────────────────────────

TIER_1_KEYWORDS = {
    "bullish": [
        "fed rate cut", "emergency rate cut", "fed pivot", "quantitative easing",
        "stimulus package", "trade deal signed", "ceasefire", "inflation falls",
        "cpi miss lower", "jobs beat", "gdp beat", "rate cut surprise"
    ],
    "bearish": [
        "fed rate hike surprise", "emergency hike", "bank failure", "default",
        "debt ceiling breach", "war declaration", "nuclear", "exchange hack",
        "crypto ban", "market circuit breaker", "flash crash", "cpi beat higher",
        "jobs miss", "gdp miss", "recession confirmed"
    ]
}

TIER_2_KEYWORDS = {
    "bullish": [
        "fed dovish", "rate cut expected", "inflation cooling", "strong earnings",
        "beat expectations", "raised guidance", "buyback", "dividend increase",
        "etf approval", "institutional buying", "trade talks progress",
        "deregulation", "tax cut"
    ],
    "bearish": [
        "fed hawkish", "rate hike expected", "inflation rising", "missed expectations",
        "lowered guidance", "layoffs", "bankruptcy", "sec investigation",
        "tariff", "trade war", "sanctions", "yield curve inversion",
        "credit downgrade", "margin call"
    ]
}

TIER_3_KEYWORDS = {
    "bullish": [
        "analyst upgrade", "price target raised", "strong demand", "market share gain",
        "partnership", "contract win", "product launch", "revenue beat"
    ],
    "bearish": [
        "analyst downgrade", "price target cut", "weak demand", "market share loss",
        "contract loss", "product recall", "revenue miss", "cost overrun"
    ]
}

# Trump-specific framework — direction matters, not just detection
TRUMP_BULLISH = [
    "tariff pause", "tariff rollback", "trade deal", "deregulation",
    "tax cut", "pro-crypto", "bitcoin reserve", "rate cut pressure",
    "market rally", "strong economy", "jobs record"
]

TRUMP_BEARISH = [
    "new tariff", "tariff increase", "trade war", "china tariff",
    "fed independence", "fed chair fire", "sanction", "import tax",
    "reciprocal tariff", "trade deficit anger"
]

# Assets specifically impacted by news
ASSET_KEYWORDS = {
    "BTC/USD": ["bitcoin", "btc", "crypto", "digital asset", "coinbase", "binance"],
    "ETH/USD": ["ethereum", "eth", "defi", "smart contract", "layer 2"],
    "NVDA": ["nvidia", "nvda", "gpu", "ai chip", "data center", "cuda", "blackwell"],
    "TSLA": ["tesla", "tsla", "elon", "musk", "ev", "electric vehicle", "cybertruck"],
    "META": ["meta", "facebook", "instagram", "zuckerberg", "metaverse", "threads"],
    "GOOGL": ["google", "alphabet", "googl", "gemini", "search", "youtube", "antitrust"],
    "AAPL": ["apple", "aapl", "iphone", "tim cook", "app store", "vision pro"],
    "MSFT": ["microsoft", "msft", "azure", "copilot", "openai", "windows", "activision"],
    "AMD": ["amd", "advanced micro", "ryzen", "radeon", "lisa su"],
    "QQQ":  ["nasdaq", "tech sector", "qqq", "tech stocks"],
    "SPY":  ["s&p 500", "spy", "dow jones", "market crash", "stock market"],
}

# Earnings calendar with whisper context
EARNINGS_CALENDAR = {
    "TSLA": {"date": "2026-04-22", "whisper_note": "Delivery miss risk — analyst estimates range wide"},
    "NVDA": {"date": "2026-05-28", "whisper_note": "Bar is extremely high — any China export concern = miss"},
    "AAPL": {"date": "2026-05-01", "whisper_note": "Services revenue key — hardware expected flat"},
    "META": {"date": "2026-04-30", "whisper_note": "Ad revenue + AI spend balance — guidance critical"},
    "GOOGL": {"date": "2026-04-29", "whisper_note": "Search market share vs AI threat — key narrative"},
    "MSFT": {"date": "2026-04-30", "whisper_note": "Azure growth rate — any deceleration = selloff"},
    "AMD": {"date": "2026-05-06", "whisper_note": "MI300 AI chip demand vs NVDA — market share story"},
}


class NewsIntelligence:
    """
    Advanced news classification engine.
    Goes beyond keyword detection to understand impact, direction, and timing.
    """

    def __init__(self):
        self._cache = {"articles": [], "cached_at": None}
        self._rss_feeds = [
            "https://feeds.marketwatch.com/marketwatch/topstories/",
            "https://feeds.content.dowjones.io/public/rss/mw_topstories",
            "https://cnbc.com/id/100003114/device/rss/rss.html",
            "https://feeds.reuters.com/reuters/businessNews",
            "https://news.google.com/rss/search?q=stock+market+finance&hl=en-US&gl=US&ceid=US:en",
        ]
        logger.info("✅ NewsIntelligence initialized")

    # ── RSS Fetching ──────────────────────────────────────────────────────────

    def fetch_articles(self, force_refresh: bool = False) -> list:
        """Fetch and cache news articles from RSS feeds."""
        now = datetime.now(timezone.utc)
        if not force_refresh and self._cache["cached_at"]:
            age = (now - self._cache["cached_at"]).total_seconds()
            if age < 300:  # 5 minute cache
                return self._cache["articles"]

        articles = []
        for feed_url in self._rss_feeds:
            try:
                headers = {"User-Agent": "Mozilla/5.0 (compatible; JimBot/1.0)"}
                resp = requests.get(feed_url, headers=headers, timeout=5)
                root = ET.fromstring(resp.content)
                for item in root.findall(".//item")[:8]:
                    title = item.find("title")
                    desc = item.find("description")
                    pub_date = item.find("pubDate")
                    if title is not None and title.text:
                        articles.append({
                            "title": title.text.strip(),
                            "description": (desc.text or "").strip()[:200],
                            "published": pub_date.text if pub_date is not None else "",
                            "source": feed_url.split("/")[2],
                            "text": (title.text + " " + (desc.text or "")).lower()
                        })
            except Exception as e:
                logger.warning(f"RSS fetch failed {feed_url}: {e}")

        self._cache = {"articles": articles, "cached_at": now}
        logger.info(f"📰 NewsIntelligence: {len(articles)} articles fetched")
        return articles

    # ── Tier Classification ───────────────────────────────────────────────────

    def classify_article(self, article: dict) -> dict:
        """
        Classify a single article into Tier 1-4 with direction and impact.
        """
        text = article["text"]
        result = {
            "tier": 4,
            "direction": "neutral",
            "score_adjustment": 0,
            "impact": "noise",
            "trump_signal": None,
            "affected_assets": [],
            "headline": article["title"]
        }

        # Tier 1 — Act within 60 seconds
        for kw in TIER_1_KEYWORDS["bullish"]:
            if kw in text:
                result.update({"tier": 1, "direction": "bullish",
                    "score_adjustment": +25, "impact": "market_moving"})
                return result
        for kw in TIER_1_KEYWORDS["bearish"]:
            if kw in text:
                result.update({"tier": 1, "direction": "bearish",
                    "score_adjustment": -25, "impact": "market_moving"})
                return result

        # Trump framework — check direction before scoring
        if "trump" in text:
            for kw in TRUMP_BULLISH:
                if kw in text:
                    result.update({"tier": 2, "direction": "bullish",
                        "score_adjustment": +15, "impact": "directional",
                        "trump_signal": f"TRUMP BULLISH: {kw}"})
                    break
            else:
                for kw in TRUMP_BEARISH:
                    if kw in text:
                        result.update({"tier": 2, "direction": "bearish",
                            "score_adjustment": -15, "impact": "directional",
                            "trump_signal": f"TRUMP BEARISH: {kw}"})
                        break
                else:
                    # Trump mentioned but direction unclear
                    result.update({"tier": 3, "direction": "uncertain",
                        "score_adjustment": -5, "impact": "contextual",
                        "trump_signal": "TRUMP: direction unclear — reduce conviction"})

        # Tier 2 — Directional signal
        if result["tier"] == 4:
            for kw in TIER_2_KEYWORDS["bullish"]:
                if kw in text:
                    result.update({"tier": 2, "direction": "bullish",
                        "score_adjustment": +12, "impact": "directional"})
                    break
            for kw in TIER_2_KEYWORDS["bearish"]:
                if kw in text:
                    result.update({"tier": 2, "direction": "bearish",
                        "score_adjustment": -12, "impact": "directional"})
                    break

        # Tier 3 — Contextual
        if result["tier"] == 4:
            for kw in TIER_3_KEYWORDS["bullish"]:
                if kw in text:
                    result.update({"tier": 3, "direction": "bullish",
                        "score_adjustment": +6, "impact": "contextual"})
                    break
            for kw in TIER_3_KEYWORDS["bearish"]:
                if kw in text:
                    result.update({"tier": 3, "direction": "bearish",
                        "score_adjustment": -6, "impact": "contextual"})
                    break

        # Detect affected assets
        for asset, keywords in ASSET_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                result["affected_assets"].append(asset)

        return result

    # ── Earnings Intelligence ─────────────────────────────────────────────────

    def get_earnings_context(self, symbol: str) -> dict:
        """
        Return earnings intelligence for a specific stock.
        Includes whisper note, days until earnings, and recommended action.
        """
        if symbol not in EARNINGS_CALENDAR:
            return {"has_earnings": False}

        info = EARNINGS_CALENDAR[symbol]
        earnings_date = datetime.strptime(info["date"], "%Y-%m-%d")
        today = datetime.now()
        days_until = (earnings_date - today).days

        if days_until < 0:
            # Past earnings — check if it was recent (post-earnings gap opportunity)
            days_since = abs(days_until)
            if days_since <= 3:
                return {
                    "has_earnings": True,
                    "timing": "post_earnings",
                    "days_since": days_since,
                    "whisper": info["whisper_note"],
                    "action": "GAPPER_PROTOCOL — evaluate post-earnings gap",
                    "score_adjustment": 0  # Neutral — let gap speak
                }
            return {"has_earnings": False}

        elif days_until == 0:
            return {
                "has_earnings": True,
                "timing": "earnings_day",
                "days_until": 0,
                "whisper": info["whisper_note"],
                "action": "SKIP — earnings day, too risky",
                "score_adjustment": -100  # Force skip
            }
        elif days_until <= 2:
            return {
                "has_earnings": True,
                "timing": "pre_earnings",
                "days_until": days_until,
                "whisper": info["whisper_note"],
                "action": f"CAUTION — earnings in {days_until} day(s), cap position at 10%",
                "score_adjustment": -20
            }
        elif days_until <= 7:
            return {
                "has_earnings": True,
                "timing": "earnings_week",
                "days_until": days_until,
                "whisper": info["whisper_note"],
                "action": f"AWARE — earnings in {days_until} days",
                "score_adjustment": -5
            }

        return {"has_earnings": False}

    # ── Master Analysis ───────────────────────────────────────────────────────

    def analyze(self, symbol: str = None) -> dict:
        """
        Run full news intelligence analysis.
        Returns classified articles, aggregate sentiment, and symbol-specific impact.
        """
        articles = self.fetch_articles()
        if not articles:
            return {
                "overall_sentiment": "neutral",
                "overall_score_adjustment": 0,
                "tier1_alerts": [],
                "trump_signals": [],
                "symbol_impact": 0,
                "earnings": {"has_earnings": False},
                "context": "No news data available",
                "top_headlines": [],
                "total_score_adjustment": 0,
            }

        classified = [self.classify_article(a) for a in articles]

        # Aggregate sentiment
        total_adj = sum(c["score_adjustment"] for c in classified)
        tier1_alerts = [c for c in classified if c["tier"] == 1]
        trump_signals = [c["trump_signal"] for c in classified if c["trump_signal"]]

        # Symbol-specific impact
        symbol_impact = 0
        symbol_articles = []
        if symbol:
            for c in classified:
                if symbol in c["affected_assets"]:
                    symbol_impact += c["score_adjustment"]
                    symbol_articles.append(c)

        # Overall sentiment label
        if total_adj >= 20:
            sentiment = "very_bullish"
        elif total_adj >= 8:
            sentiment = "bullish"
        elif total_adj <= -20:
            sentiment = "very_bearish"
        elif total_adj <= -8:
            sentiment = "bearish"
        else:
            sentiment = "neutral"

        # Earnings context
        earnings = self.get_earnings_context(symbol) if symbol else {"has_earnings": False}

        # Build context string for Claude
        lines = ["=== NEWS INTELLIGENCE ==="]
        lines.append(f"Overall sentiment: {sentiment.upper()} (aggregate score: {total_adj:+d})")

        if tier1_alerts:
            lines.append(f"⚡ TIER 1 ALERTS ({len(tier1_alerts)}):")
            for alert in tier1_alerts[:2]:
                lines.append(f"  → {alert['headline'][:80]} [{alert['direction'].upper()}]")

        if trump_signals:
            for sig in trump_signals[:2]:
                lines.append(f"🇺🇸 {sig}")

        if symbol and symbol_articles:
            lines.append(f"📌 {symbol}-specific news (impact: {symbol_impact:+d}):")
            for art in symbol_articles[:2]:
                lines.append(f"  → T{art['tier']} {art['direction'].upper()}: {art['headline'][:70]}")
        elif symbol:
            lines.append(f"📌 No {symbol}-specific news found")

        if earnings.get("has_earnings"):
            lines.append(f"📅 EARNINGS: {earnings['action']}")
            lines.append(f"   Whisper: {earnings['whisper']}")

        # Top 3 relevant headlines
        relevant = [c for c in classified if c["tier"] <= 3 and c["direction"] != "neutral"]
        for art in relevant[:3]:
            emoji = "🟢" if art["direction"] == "bullish" else "🔴"
            lines.append(f"{emoji} T{art['tier']}: {art['headline'][:75]}")

        total_score_adj = total_adj + symbol_impact + earnings.get("score_adjustment", 0)

        return {
            "overall_sentiment": sentiment,
            "overall_score_adjustment": total_adj,
            "symbol_score_adjustment": symbol_impact,
            "earnings_score_adjustment": earnings.get("score_adjustment", 0),
            "total_score_adjustment": total_score_adj,
            "tier1_alerts": tier1_alerts,
            "trump_signals": trump_signals,
            "earnings": earnings,
            "context": "\n".join(lines),
            "top_headlines": [c["headline"] for c in relevant[:3]],
        }

    def build_news_context(self, symbol: str = None) -> str:
        """Returns just the context string for injection into Claude prompt."""
        return self.analyze(symbol)["context"]



# ════════════════════════════════════════════════════════════
# FILE: dashboard.py
# ════════════════════════════════════════════════════════════
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional
from flask import Flask, jsonify, render_template_string
from flask_cors import CORS
from memory import TradingMemory
from analyzer import TradeAnalyzer

logger = logging.getLogger(__name__)
app = Flask(__name__)
CORS(app)
_memory: Optional[TradingMemory] = None
_analyzer: Optional[TradeAnalyzer] = None
_scanner = None
_regime  = None
_agent   = None

def init_dashboard(memory, analyzer, scanner=None, regime=None, agent=None):
    global _memory, _analyzer, _scanner, _regime, _agent
    _memory  = memory
    _analyzer = analyzer
    _scanner = scanner
    _regime  = regime
    _agent   = agent

@app.route("/api/stats")
def api_stats():
    if not _memory: return jsonify({})
    return jsonify(_memory.compute_performance_stats())

@app.route("/api/trades/open")
def api_open_trades():
    if not _memory: return jsonify([])
    return jsonify(_memory.get_open_trades())

@app.route("/api/trades/recent")
def api_recent_trades():
    if not _memory: return jsonify([])
    return jsonify(_memory.get_recent_trades(limit=20))

@app.route("/api/decisions/recent")
def api_recent_decisions():
    if not _memory: return jsonify([])
    return jsonify(_memory.get_recent_decisions(limit=15))

@app.route("/api/analyses/recent")
def api_recent_analyses():
    if not _memory: return jsonify([])
    analyses = _memory.get_analyses(limit=5)
    for a in analyses:
        for field in ["lessons","mistakes"]:
            if a.get(field) and isinstance(a[field], str):
                try: a[field] = json.loads(a[field])
                except: pass
    return jsonify(analyses)

@app.route("/api/anomalies")
def api_anomalies():
    if not _analyzer: return jsonify([])
    return jsonify(_analyzer.detect_performance_anomalies())

@app.route("/api/movers")
def api_movers():
    if not _scanner:
        return jsonify({"movers": [], "error": "scanner not initialized"})
    try:
        movers = _scanner.get_top_movers(top_n=6)
        return jsonify({"movers": movers, "ts": datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        return jsonify({"movers": [], "error": str(e)})

@app.route("/api/sentiment")
def api_sentiment():
    if not _scanner:
        return jsonify({"sentiment": "neutral", "score": 0, "headlines": [], "alerts": []})
    try:
        result = _scanner.analyze_sentiment()
        return jsonify(result)
    except Exception as e:
        return jsonify({"sentiment": "neutral", "score": 0, "headlines": [], "alerts": [], "error": str(e)})

@app.route("/api/calendar")
def api_calendar():
    if not _scanner:
        return jsonify({"event": None, "note": ""})
    try:
        result = _scanner.check_economic_calendar()
        return jsonify(result)
    except Exception as e:
        return jsonify({"event": None, "note": "", "error": str(e)})

@app.route("/api/regime")
def api_regime():
    if not _regime:
        return jsonify({"regime": "UNKNOWN", "vix": None, "dxy": None, "score_long_threshold": 60, "score_short_threshold": 30})
    try:
        import re as _re
        params  = _regime.get_params()
        context = _regime.build_regime_context()
        # Inject VIX into params if present in context string
        vix_m = _re.search(r"VIX:\s*([\d.]+)", context)
        if vix_m:
            params = dict(params)
            params["vix"] = float(vix_m.group(1))
        return jsonify({
            "regime":   params.get("regime", "UNKNOWN"),
            "params":   params,
            "context":  context,
        })
    except Exception as e:
        return jsonify({"regime": "UNKNOWN", "error": str(e)})

def _period_start(period: str) -> str | None:
    now = datetime.now(timezone.utc)
    if period == "today":
        return now.date().isoformat()
    if period == "week":
        monday = now.date() - __import__('datetime').timedelta(days=now.weekday())
        return monday.isoformat()
    if period == "month":
        return now.date().replace(day=1).isoformat()
    if period == "ytd":
        return now.date().replace(month=1, day=1).isoformat()
    return None  # all

@app.route("/api/closed-today")
def api_closed_today():
    from flask import request as flask_req
    if not _memory:
        return jsonify({"closed": [], "date": ""})
    try:
        period    = flask_req.args.get("period", "today")
        since     = _period_start(period)
        today     = datetime.now(timezone.utc).date().isoformat()
        conn      = sqlite3.connect(_memory.db_path, timeout=10)
        c         = conn.cursor()
        if since:
            c.execute("""
                SELECT symbol,
                       SUM(pnl)         AS total_pnl,
                       COUNT(*)         AS trade_count,
                       SUM(qty)         AS total_qty_sold,
                       MAX(exit_at)     AS last_exit_at,
                       GROUP_CONCAT(DISTINCT close_reason) AS reasons
                FROM trades
                WHERE status = 'closed' AND exit_at >= ?
                GROUP BY symbol
                ORDER BY total_pnl DESC
            """, (since,))
        else:
            c.execute("""
                SELECT symbol,
                       SUM(pnl)         AS total_pnl,
                       COUNT(*)         AS trade_count,
                       SUM(qty)         AS total_qty_sold,
                       MAX(exit_at)     AS last_exit_at,
                       GROUP_CONCAT(DISTINCT close_reason) AS reasons
                FROM trades
                WHERE status = 'closed'
                GROUP BY symbol
                ORDER BY total_pnl DESC
            """)
        rows = c.fetchall()
        conn.close()
        closed = [
            {
                "symbol":      r[0],
                "pnl":         round(r[1], 6) if r[1] is not None else 0,
                "trade_count": r[2],
                "qty_sold":    round(r[3], 8) if r[3] is not None else 0,
                "last_exit":   r[4] or "",
                "reasons":     r[5] or "",
            }
            for r in rows
        ]
        return jsonify({"closed": closed, "date": today, "period": period})
    except Exception as e:
        logger.error(f"api_closed_today error: {e}")
        return jsonify({"closed": [], "error": str(e)})

@app.route("/api/analysis")
def api_analysis():
    if not _memory:
        return jsonify({})
    try:
        conn = sqlite3.connect(_memory.db_path, timeout=10)
        c    = conn.cursor()

        # ── All closed trades ──────────────────────────────────────────
        c.execute("""
            SELECT symbol, pnl, pnl_pct, hold_duration_min, close_reason, exit_at
            FROM trades WHERE status='closed'
            ORDER BY exit_at
        """)
        trades = c.fetchall()

        # ── Daily P&L (last 30 days) ───────────────────────────────────
        c.execute("""
            SELECT DATE(exit_at) AS day, SUM(pnl) AS day_pnl, COUNT(*) AS cnt
            FROM trades WHERE status='closed'
            GROUP BY day ORDER BY day DESC LIMIT 30
        """)
        daily_rows = c.fetchall()

        # ── P&L by asset ───────────────────────────────────────────────
        c.execute("""
            SELECT symbol, SUM(pnl) AS total, COUNT(*) AS cnt,
                   AVG(pnl) AS avg_pnl, AVG(hold_duration_min) AS avg_hold
            FROM trades WHERE status='closed'
            GROUP BY symbol ORDER BY total DESC
        """)
        asset_rows = c.fetchall()

        # ── Close reason breakdown ─────────────────────────────────────
        c.execute("""
            SELECT close_reason, COUNT(*) AS cnt, SUM(pnl) AS total_pnl
            FROM trades WHERE status='closed'
            GROUP BY close_reason ORDER BY cnt DESC
        """)
        reason_rows = c.fetchall()

        conn.close()

        # ── Compute core metrics ───────────────────────────────────────
        total  = len(trades)
        wins   = [t for t in trades if (t[1] or 0) > 0]
        losses = [t for t in trades if (t[1] or 0) < 0]
        pnls   = [t[1] or 0 for t in trades]
        holds  = [t[3] or 0 for t in trades if t[3]]

        gross_win  = sum(t[1] for t in wins)  if wins   else 0
        gross_loss = sum(t[1] for t in losses) if losses else 0
        win_rate   = (len(wins) / total * 100) if total else 0
        loss_rate  = 100 - win_rate
        avg_win    = (gross_win / len(wins))   if wins   else 0
        avg_loss   = (gross_loss / len(losses)) if losses else 0
        pf         = (gross_win / abs(gross_loss)) if gross_loss else 999
        expectancy = (win_rate/100 * avg_win) + (loss_rate/100 * avg_loss)

        best_trade  = max(trades, key=lambda t: t[1] or 0) if trades else None
        worst_trade = min(trades, key=lambda t: t[1] or 0) if trades else None
        avg_hold    = (sum(holds) / len(holds)) if holds else 0

        # ── Streak calculation ─────────────────────────────────────────
        streak, max_win_streak, max_loss_streak = 0, 0, 0
        cur_streak_type = None
        for t in trades:
            is_win = (t[1] or 0) >= 0
            if cur_streak_type is None or is_win == cur_streak_type:
                streak += 1
                cur_streak_type = is_win
            else:
                if cur_streak_type:
                    max_win_streak  = max(max_win_streak, streak)
                else:
                    max_loss_streak = max(max_loss_streak, streak)
                streak = 1
                cur_streak_type = is_win
        if cur_streak_type is True:
            max_win_streak  = max(max_win_streak, streak)
        elif cur_streak_type is False:
            max_loss_streak = max(max_loss_streak, streak)
        current_streak      = {"type": "win" if cur_streak_type else "loss", "count": streak} if trades else None

        # ── Avg trades per active day ──────────────────────────────────
        active_days = len(set(t[5][:10] for t in trades if t[5])) if trades else 1
        avg_trades_per_day = total / active_days if active_days else 0

        return jsonify({
            "total_trades":      total,
            "winning_trades":    len(wins),
            "losing_trades":     len(losses),
            "win_rate":          round(win_rate, 1),
            "profit_factor":     round(pf, 2) if pf != 999 else 999,
            "expectancy":        round(expectancy, 4),
            "gross_win":         round(gross_win, 4),
            "gross_loss":        round(gross_loss, 4),
            "total_pnl":         round(sum(pnls), 4),
            "avg_win":           round(avg_win, 4),
            "avg_loss":          round(avg_loss, 4),
            "avg_hold_min":      round(avg_hold, 1),
            "avg_trades_per_day": round(avg_trades_per_day, 1),
            "best_trade":  {"symbol": best_trade[0],  "pnl": round(best_trade[1],4),  "reason": best_trade[4]} if best_trade  else None,
            "worst_trade": {"symbol": worst_trade[0], "pnl": round(worst_trade[1],4), "reason": worst_trade[4]} if worst_trade else None,
            "current_streak":    current_streak,
            "max_win_streak":    max_win_streak,
            "max_loss_streak":   max_loss_streak,
            "daily_pnl":  [{"date": r[0], "pnl": round(r[1],4), "trades": r[2]} for r in daily_rows],
            "by_asset":   [{"symbol": r[0], "pnl": round(r[1],4), "trades": r[2], "avg_pnl": round(r[3],4), "avg_hold_min": round(r[4] or 0,1)} for r in asset_rows],
            "by_reason":  [{"reason": r[0], "trades": r[1], "pnl": round(r[2],4)} for r in reason_rows],
        })
    except Exception as e:
        logger.error(f"api_analysis error: {e}")
        return jsonify({"error": str(e)})

@app.route("/api/account")
def api_account():
    if not _agent:
        return jsonify({"equity": 0, "cash": 0, "buying_power": 0, "portfolio_value": 0})
    try:
        account = _agent.broker.get_account()
        return jsonify({
            "equity":          float(account.equity),
            "cash":            float(account.cash),
            "buying_power":    float(account.buying_power),
            "portfolio_value": float(account.portfolio_value),
            "last_equity":     float(account.last_equity),
        })
    except Exception as e:
        logger.error(f"api_account error: {e}")
        return jsonify({"equity": 0, "cash": 0, "buying_power": 0, "portfolio_value": 0, "error": str(e)})

@app.route("/api/stops")
def api_stops():
    """Return trailing stop prices per symbol from agent's in-memory state."""
    if not _agent:
        return jsonify({"stops": {}})
    try:
        stops = {}
        high  = getattr(_agent, "_high_water", {})
        trail = getattr(_agent, "_trail_pcts", {})
        low   = getattr(_agent, "_low_water",  {})
        for sym, h in high.items():
            pct = trail.get(sym, 0.05)
            stops[sym] = round(h * (1 - pct), 4)
        for sym, l in low.items():
            stops[sym] = round(l * (1 + 0.03), 4)
        return jsonify({"stops": stops})
    except Exception as e:
        return jsonify({"stops": {}, "error": str(e)})

@app.route("/api/health")
def api_health():
    return jsonify({"status":"ok","timestamp":datetime.now(timezone.utc).isoformat()})

@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)

@app.route("/source")
def api_source():
    """Return all Python source files concatenated as plain text — shareable in a browser."""
    from flask import Response
    base = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(base, ".."))

    PYTHON_FILES = [
        "trading-agent/main.py",
        "trading-agent/config.py",
        "trading-agent/broker.py",
        "trading-agent/risk.py",
        "trading-agent/memory.py",
        "trading-agent/strategy.py",
        "trading-agent/regime.py",
        "trading-agent/scanner.py",
        "trading-agent/correlations.py",
        "trading-agent/geometry.py",
        "trading-agent/synthesis.py",
        "trading-agent/agent.py",
        "trading-agent/dashboard.py",
    ]

    SEP = "=" * 80
    chunks = ["JIM BOT — Python Source\n" + SEP + "\n"]

    total_lines = 0
    for rel in PYTHON_FILES:
        abs_path = os.path.join(project_root, rel)
        if not os.path.exists(abs_path):
            continue
        try:
            with open(abs_path, encoding="utf-8", errors="replace") as f:
                content = f.read()
            n = len(content.splitlines())
            total_lines += n
            chunks.append(f"\n{SEP}\nFILE: {rel}  ({n} lines)\n{SEP}\n\n{content}\n")
        except Exception as e:
            chunks.append(f"\n{SEP}\nFILE: {rel}  — ERROR: {e}\n{SEP}\n")

    chunks.append(f"\n{SEP}\nTotal: {len(PYTHON_FILES)} files, {total_lines} lines\n{SEP}\n")
    return Response("".join(chunks), mimetype="text/plain; charset=utf-8")

AGENT_FILES = ["agent", "analyzer", "dashboard", "notifier", "news_intelligence", "synthesis"]

@app.route("/api/source/<filename>")
def source_file(filename):
    """Return a single Python source file by name (no extension)."""
    from flask import Response as _R
    if filename not in AGENT_FILES:
        return "Not found", 404
    base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, f"{filename}.py")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return _R(f.read(), status=200, mimetype="text/plain; charset=utf-8")
    except Exception:
        return "File not found", 404

def start_dashboard(memory, analyzer, scanner=None, regime=None, agent=None, port=8080):
    init_dashboard(memory, analyzer, scanner=scanner, regime=regime, agent=agent)
    def run():
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    logger.info(f"🌐 Dashboard running on port {port}")
    return thread

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TRADING AGENT</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@400;800&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#080c10;--surface:#0d1117;--border:#1a2030;--text:#c8d6e5;--muted:#4a5568;--green:#00ff88;--red:#ff3860;--blue:#4fc3f7;--mono:'Space Mono',monospace;--display:'Syne',sans-serif}
body{background:var(--bg);color:var(--text);font-family:var(--mono);font-size:12px;line-height:1.6}
body::before{content:'';position:fixed;inset:0;background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,255,136,0.015) 2px,rgba(0,255,136,0.015) 4px);pointer-events:none;z-index:9999}
header{display:flex;justify-content:space-between;align-items:center;padding:16px 24px;border-bottom:1px solid var(--border);background:var(--surface);position:sticky;top:0;z-index:100}
.logo{font-family:var(--display);font-size:18px;font-weight:800;letter-spacing:0.15em;color:var(--green);text-transform:uppercase}
.logo span{color:var(--muted);font-weight:400}
.status-dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.3}}
#clock{color:var(--muted);font-size:11px}
.stats-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:1px;background:var(--border)}
.stat-card{background:var(--surface);padding:20px 16px;text-align:center}
.stat-label{font-size:9px;letter-spacing:0.15em;text-transform:uppercase;color:var(--muted);margin-bottom:8px}
.stat-value{font-family:var(--display);font-size:28px;font-weight:800;line-height:1}
.pos{color:var(--green)} .neg{color:var(--red)} .neu{color:var(--blue)}
.main{display:grid;grid-template-columns:1fr 1fr 1fr;gap:1px;background:var(--border)}
.panel{background:var(--surface);padding:20px;overflow:hidden}
.panel-full{grid-column:1/-1} .panel-half{grid-column:span 2}
.panel-title{font-family:var(--display);font-size:10px;font-weight:700;letter-spacing:0.2em;text-transform:uppercase;color:var(--muted);margin-bottom:16px;display:flex;align-items:center;gap:8px}
.panel-title::after{content:'';flex:1;height:1px;background:var(--border)}
.data-table{width:100%;border-collapse:collapse}
.data-table th{font-size:9px;letter-spacing:0.12em;text-transform:uppercase;color:var(--muted);text-align:left;padding:6px 8px;border-bottom:1px solid var(--border)}
.data-table td{padding:8px;border-bottom:1px solid rgba(26,32,48,0.5)}
.tag{display:inline-block;padding:2px 7px;border-radius:3px;font-size:10px;font-weight:700}
.tag-buy{background:rgba(0,255,136,0.1);color:var(--green)}
.tag-sell{background:rgba(255,56,96,0.1);color:var(--red)}
.tag-open{background:rgba(79,195,247,0.1);color:var(--blue)}
.asset-bars{display:flex;flex-direction:column;gap:10px}
.asset-row{display:flex;align-items:center;gap:10px}
.asset-name{width:70px;font-size:11px;flex-shrink:0}
.bar-wrap{flex:1;height:14px;background:var(--bg);border-radius:2px;overflow:hidden}
.bar-fill{height:100%;border-radius:2px;transition:width 0.8s ease}
.bar-fill.pos{background:linear-gradient(90deg,#00cc6a,var(--green))}
.bar-fill.neg{background:linear-gradient(90deg,#cc2040,var(--red))}
.asset-pnl{width:60px;text-align:right;font-size:11px;flex-shrink:0}
.decision-item{padding:12px 0;border-bottom:1px solid var(--border)}
.decision-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.decision-time{font-size:10px;color:var(--muted)}
.decision-reasoning{font-size:11px;color:var(--muted);line-height:1.5;max-height:3.5em;overflow:hidden;cursor:pointer}
.decision-reasoning.expanded{max-height:200px}
.analysis-item{background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:14px;margin-bottom:10px}
.analysis-text{font-size:11px;color:var(--muted);line-height:1.6;margin-bottom:8px}
.lesson{font-size:10px;color:var(--blue);padding-left:12px;position:relative;margin-bottom:4px}
.lesson::before{content:'→';position:absolute;left:0;color:var(--muted)}
.empty{text-align:center;color:var(--muted);padding:30px;font-size:11px}
.alert-box{background:rgba(255,56,96,0.1);border:1px solid var(--red);border-radius:4px;padding:10px 16px;color:var(--red);font-size:11px;margin-bottom:12px}
.refresh-bar{height:2px;background:var(--border);width:100%;position:fixed;bottom:0;left:0}
.refresh-progress{height:100%;background:var(--green);width:0%}
@media(max-width:1024px){.main{grid-template-columns:1fr 1fr}.stats-grid{grid-template-columns:repeat(3,1fr)}.panel-half{grid-column:span 1}}
@media(max-width:640px){.main{grid-template-columns:1fr}.stats-grid{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<header>
  <div class="logo">AGENT<span>/</span>TERMINAL</div>
  <div style="display:flex;align-items:center;gap:20px">
    <div class="status-dot"></div>
    <div id="clock"></div>
  </div>
</header>
<div class="stats-grid">
  <div class="stat-card"><div class="stat-label">P&L Total</div><div class="stat-value" id="kpi-pnl">—</div></div>
  <div class="stat-card"><div class="stat-label">Win Rate</div><div class="stat-value neu" id="kpi-wr">—</div></div>
  <div class="stat-card"><div class="stat-label">Trades</div><div class="stat-value" id="kpi-trades">—</div></div>
  <div class="stat-card"><div class="stat-label">Profit Factor</div><div class="stat-value neu" id="kpi-pf">—</div></div>
  <div class="stat-card"><div class="stat-label">Max Drawdown</div><div class="stat-value neg" id="kpi-dd">—</div></div>
  <div class="stat-card"><div class="stat-label">Positions Open</div><div class="stat-value neu" id="kpi-open">—</div></div>
</div>
<div class="main">
  <div class="panel panel-half">
    <div class="panel-title">Positions ouvertes</div>
    <div id="open-trades-container"><div class="empty">Aucune position ouverte</div></div>
  </div>
  <div class="panel">
    <div class="panel-title">P&L par asset</div>
    <div class="asset-bars" id="asset-bars"><div class="empty">Pas de données</div></div>
  </div>
  <div class="panel panel-full">
    <div class="panel-title">Historique des trades</div>
    <div style="overflow-x:auto">
      <table class="data-table">
        <thead><tr><th>Symbol</th><th>Dir.</th><th>Entrée</th><th>Sortie</th><th>Qté</th><th>P&L $</th><th>P&L %</th><th>Durée</th><th>Raison</th><th>Statut</th></tr></thead>
        <tbody id="trades-body"><tr><td colspan="10" class="empty">Chargement...</td></tr></tbody>
      </table>
    </div>
  </div>
  <div class="panel panel-half" style="max-height:500px;overflow-y:auto">
    <div class="panel-title">Décisions de l'agent</div>
    <div id="decisions-container"><div class="empty">Aucune décision</div></div>
  </div>
  <div class="panel" style="max-height:500px;overflow-y:auto">
    <div class="panel-title">Analyses post-trade</div>
    <div id="analyses-container"><div class="empty">Aucune analyse</div></div>
  </div>
</div>
<div class="refresh-bar"><div class="refresh-progress" id="refresh-progress"></div></div>
<script>
const REFRESH=15000;
function fmt(v,p='$',d=2){if(v===null||v===undefined||isNaN(v))return'—';return p+parseFloat(v).toFixed(d)}
function fmtPct(v){if(v===null||v===undefined||isNaN(v))return'—';const n=parseFloat(v);return(n>0?'+':'')+n.toFixed(2)+'%'}
function pnlCls(v){if(!v||isNaN(v))return'';return parseFloat(v)>=0?'pos':'neg'}
function fmtDur(m){if(!m)return'—';const n=Math.round(m);return n<60?n+'min':Math.floor(n/60)+'h'+(n%60>0?n%60+'min':'')}
async function fetchJSON(url){try{const r=await fetch(url);return await r.json()}catch(e){return null}}
function updateClock(){document.getElementById('clock').textContent=new Date().toUTCString().split(' ')[4]+' UTC'}
setInterval(updateClock,1000);updateClock();
async function updateStats(){
  const s=await fetchJSON('/api/stats');
  if(!s||s.total_trades===0)return;
  const pnl=s.total_pnl||0;
  const el=document.getElementById('kpi-pnl');
  el.textContent=(pnl>=0?'+':'')+' $'+Math.abs(pnl).toFixed(2);
  el.className='stat-value '+(pnl>=0?'pos':'neg');
  document.getElementById('kpi-wr').textContent=(s.win_rate||0).toFixed(1)+'%';
  document.getElementById('kpi-trades').textContent=s.total_trades;
  const pf=document.getElementById('kpi-pf');
  pf.textContent=s.profit_factor>=999?'∞':(s.profit_factor||0).toFixed(2);
  pf.className='stat-value '+((s.profit_factor||0)>=1?'pos':'neg');
  document.getElementById('kpi-dd').textContent='-$'+(s.max_drawdown||0).toFixed(2);
  renderAssetBars(s.asset_pnl||{});
}
async function updateOpenTrades(){
  const trades=await fetchJSON('/api/trades/open');
  const el=document.getElementById('open-trades-container');
  document.getElementById('kpi-open').textContent=trades?trades.length:0;
  if(!trades||trades.length===0){el.innerHTML='<div class="empty">Aucune position ouverte</div>';return}
  el.innerHTML='<table class="data-table"><thead><tr><th>Symbol</th><th>Dir.</th><th>Entrée</th><th>SL</th><th>TP</th></tr></thead><tbody>'+
    trades.map(t=>`<tr><td><strong>${t.symbol}</strong></td><td><span class="tag tag-${t.side}">${t.side.toUpperCase()}</span></td><td>${fmt(t.entry_price)}</td><td style="color:var(--red)">${fmt(t.stop_loss)}</td><td style="color:var(--green)">${fmt(t.take_profit)}</td></tr>`).join('')+
    '</tbody></table>';
}
function renderAssetBars(ap){
  const c=document.getElementById('asset-bars');
  const e=Object.entries(ap);
  if(!e.length){c.innerHTML='<div class="empty">Pas de données</div>';return}
  const max=Math.max(...e.map(([,v])=>Math.abs(v)),1);
  c.innerHTML=e.sort(([,a],[,b])=>b-a).map(([s,p])=>{
    const pct=Math.abs(p)/max*100;const cls=p>=0?'pos':'neg';
    return`<div class="asset-row"><div class="asset-name">${s}</div><div class="bar-wrap"><div class="bar-fill ${cls}" style="width:${pct}%"></div></div><div class="asset-pnl ${cls}">${p>=0?'+':''} $${Math.abs(p).toFixed(2)}</div></div>`;
  }).join('');
}
async function updateTradesHistory(){
  const trades=await fetchJSON('/api/trades/recent');
  const body=document.getElementById('trades-body');
  if(!trades||!trades.length){body.innerHTML='<tr><td colspan="10" class="empty">Aucun trade</td></tr>';return}
  body.innerHTML=trades.map(t=>{
    const pnl=t.pnl;const cls=pnl===null?'':(pnl>=0?'pos':'neg');const isOpen=t.status==='open';
    return`<tr><td><strong>${t.symbol}</strong></td><td><span class="tag tag-${t.side}">${t.side.toUpperCase()}</span></td><td>${fmt(t.entry_price)}</td><td>${isOpen?'<span class="tag tag-open">OUVERT</span>':fmt(t.exit_price)}</td><td>${t.qty}</td><td class="${cls}">${pnl!==null?(pnl>=0?'+':'')+'$'+Math.abs(pnl).toFixed(2):'—'}</td><td class="${cls}">${t.pnl_pct!==null?fmtPct(t.pnl_pct):'—'}</td><td>${fmtDur(t.hold_duration_min)}</td><td style="color:var(--muted)">${t.close_reason||'—'}</td><td><span class="tag ${isOpen?'tag-open':(pnl>=0?'tag-buy':'tag-sell')}">${t.status}</span></td></tr>`;
  }).join('');
}
async function updateDecisions(){
  const d=await fetchJSON('/api/decisions/recent');
  const c=document.getElementById('decisions-container');
  if(!d||!d.length){c.innerHTML='<div class="empty">Aucune décision</div>';return}
  c.innerHTML=d.map(x=>`<div class="decision-item"><div class="decision-header"><span class="tag tag-${x.decision==='buy'?'buy':x.decision==='sell'?'sell':'open'}">${x.decision.toUpperCase()}</span> <strong>${x.symbol||''}</strong><span class="decision-time">${new Date(x.decided_at).toLocaleTimeString('fr-FR')}</span></div><div class="decision-reasoning" onclick="this.classList.toggle('expanded')">${x.reasoning||'—'}</div></div>`).join('');
}
async function updateAnalyses(){
  const a=await fetchJSON('/api/analyses/recent');
  const c=document.getElementById('analyses-container');
  if(!a||!a.length){c.innerHTML='<div class="empty">Aucune analyse</div>';return}
  c.innerHTML=a.map(x=>{
    const lessons=Array.isArray(x.lessons)?x.lessons:[];
    const col=x.outcome==='win'?'var(--green)':x.outcome==='loss'?'var(--red)':'var(--blue)';
    return`<div class="analysis-item"><div style="display:flex;justify-content:space-between;margin-bottom:8px"><strong>${x.symbol}</strong><span style="color:${col};font-weight:700">${x.outcome.toUpperCase()} ${x.pnl?(x.pnl>=0?'+':'')+'$'+Math.abs(x.pnl).toFixed(2):''}</span></div><div class="analysis-text">${x.analysis||'—'}</div>${lessons.map(l=>`<div class="lesson">${l}</div>`).join('')}</div>`;
  }).join('');
}
async function refreshAll(){
  await Promise.all([updateStats(),updateOpenTrades(),updateTradesHistory(),updateDecisions(),updateAnalyses()]);
  const bar=document.getElementById('refresh-progress');
  bar.style.transition='none';bar.style.width='0%';
  requestAnimationFrame(()=>{bar.style.transition=`width ${REFRESH}ms linear`;bar.style.width='100%'});
}
refreshAll();
setInterval(refreshAll,REFRESH);
</script>
</body>
</html>"""



