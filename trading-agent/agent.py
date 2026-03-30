import json
import logging
import uuid
from datetime import datetime, timezone, time as dtime
import anthropic
import config
import pytz
from correlations import CorrelationIntelligence
from regime import MarketRegime
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
        # Tracks highest price seen per LONG position (for trailing stop)
        self._high_water: dict = {}
        # Tracks lowest price seen per SHORT position (for trailing stop)
        self._low_water: dict = {}
        # Symbols for which partial profit has already been taken this session
        self._partial_taken: set = set()
        # Score-based trailing stop % per symbol (set at entry, read during management)
        self._trail_pcts: dict = {}
        # Daily dedup: date string (YYYY-MM-DD ET) when cash liberation last fired
        self._cash_lib_last_date: str = ""

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
                        secured_val    = sell_qty * current_price
                        logger.info(
                            f"💰 PARTIAL PROFIT: {symbol} sold 50% at "
                            f"+{unrealised_pct*100:.1f}% — "
                            f"${secured_val:,.2f} secured — "
                            f"remaining 50% running with {trail_pct*100:.0f}% trailing stop"
                        )
                        order = self.broker.place_order(symbol, sell_qty, "sell")
                        self._partial_taken.add(symbol)

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

        regime_context = self.regime.build_regime_context()
        combined_context = f"{market_context}\n\n{regime_context}" if market_context else regime_context

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

    def run_cycle(self):
        # 9:25 ET weekdays: free crypto cash ahead of stock market open
        self._pre_market_cash_liberation()

        # Sync any Alpaca positions that have no SQLite record (e.g. manual trades)
        self._sync_orphan_positions()

        # Manage trailing stops on all open positions (long and short)
        self._manage_trailing_stops()
        self._manage_short_trailing_stops()

        if not self.risk.can_trade():
            logger.warning("⚠️ Trading paused by risk manager")
            return

        session_ctx = get_session_context()
        session = session_ctx["session"]
        logger.info(f"📅 Session: {session} ({session_ctx['time_et']}) — stocks: {session_ctx['good_for_stocks']} | crypto: {session_ctx['good_for_crypto']}")

        # ── Regime-driven dynamic parameters ─────────────────────────────────
        regime_params   = self.regime.get_params()
        _regime         = regime_params["regime"]
        score_long_min  = regime_params["score_long_threshold"]
        score_short_max = regime_params["score_short_threshold"]
        size_mult       = regime_params["position_size_multiplier"]
        regime_conf_min = regime_params["confidence_threshold"] / 100
        logger.info(
            f"🎯 Regime: {_regime.upper()} | "
            f"long_min={score_long_min} | short_max={score_short_max} | "
            f"size={size_mult}x | conf_min={regime_conf_min:.0%}"
        )

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
        cached_movers_list = self.scanner._movers_cache.get("symbols", [])

        for symbol in ranked:
            data      = symbols_data[symbol]
            is_crypto = "/" in symbol
            opp_score = compute_opportunity_score(data["indicators"], data["patterns"])
            data["opportunity_score"] = opp_score

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
                data = symbols_data[symbol]
                bars = data["bars"]

                corr_ctx  = self.correlations.build_correlation_context(
                    symbol, open_pos_symbols, corr_changes, dxy_trend
                )
                decision = self.analyze_market(
                    symbol, bars,
                    market_context=f"{market_context}\n\n{corr_ctx}"
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

                    # ── Regime size multiplier ──────────────────────────────────
                    if size_mult != 1.0:
                        orig_qty = qty
                        qty  = max(1, round(qty * size_mult, 8))
                        pct  = pct * size_mult
                        logger.info(
                            f"  ↳ Regime {_regime} size_mult={size_mult}x: "
                            f"qty {orig_qty} → {qty} | position={pct*100:.0f}%"
                        )

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
