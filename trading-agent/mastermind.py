"""
mastermind.py — Central Orchestrator V2
Job 1: Detect signals → route to experts (binary checks only)
Job 2: Circuit breaker (flash crash, VIX, calendar, pre-close, liberation)
Reuses all V1 methods — does NOT rebuild what exists.
Agent.py is untouched.
"""
import logging
import datetime as dt
from datetime import timezone
import sqlite3
import json
import pytz
import config
from experts.gapper_expert import GapperExpert
from experts.geometric_expert import GeometricExpert

logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")


def _smart_round(price: float) -> float:
    """Precision-aware rounding — handles micro-priced tokens like SHIB ($0.000009)."""
    if price >= 100:
        return round(price, 2)
    elif price >= 1:
        return round(price, 4)
    elif price >= 0.01:
        return round(price, 6)
    elif price >= 0.0001:
        return round(price, 8)
    else:
        return round(price, 10)


class Mastermind:
    def __init__(self, broker, memory, scanner, regime, geometry, correlations, agent):
        self.broker = broker
        self.memory = memory
        self.scanner = scanner
        self.regime = regime
        self.agent = agent  # to call existing V1 methods

        self.gapper = GapperExpert(broker, memory, scanner, regime)
        self.geometric = GeometricExpert(broker, memory, geometry, regime, correlations)

        # Circuit breaker state
        self._pause_until: dt.datetime = None
        self._pause_reason: str = ""
        self._halted_symbols: dict = {}  # symbol → expiry datetime

        # Gapper day trading state (reset daily)
        self._gapper_trades_today: int = 0
        self._gapper_consecutive_losses: int = 0
        self._last_reset_date: str = ""

        # Calendar-driven size modifier (refreshed every 5min)
        self._size_modifier: float = 1.0

        logger.info("✅ Mastermind V2 initialized")
        logger.info(
            f"💰 CAPITAL SPLIT: Gap=${config.STRATEGY_CAPITAL['gapper']:.2f} | "
            f"Geo=${config.STRATEGY_CAPITAL['geometric']:.2f} | "
            f"Total=${sum(config.STRATEGY_CAPITAL.values()):.2f}"
        )
        self._place_missing_stops()
        self._correct_micro_stops()

    # ── Retroactive stop placement ─────────────────────────────────────────

    def _place_missing_stops(self):
        """
        One-shot at startup: find open geo trades with a stop_loss but no
        alpaca_stop_order_id and place a stop_limit sell order for each.
        Crypto uses stop_limit (Alpaca rejects plain stop orders for crypto).
        """
        if not self.memory or not self.broker:
            return
        try:
            open_trades = self.memory.get_open_trades()
            alpaca_positions = {
                p.symbol.replace("/", "").upper(): p
                for p in (self.broker.get_positions() or [])
            }

            for trade in open_trades:
                ctx = trade.get("market_context") or {}
                if isinstance(ctx, str):
                    try:
                        ctx = json.loads(ctx)
                    except Exception:
                        ctx = {}

                # Only geo trades with a stop_loss and no existing stop order
                if ctx.get("strategy_source") != "geometric":
                    continue
                stop_price = trade.get("stop_loss")
                if not stop_price or stop_price <= 0:
                    continue
                if ctx.get("alpaca_stop_order_id"):
                    continue

                symbol = trade["symbol"]
                is_crypto = "/" in symbol

                # Use live qty from Alpaca position (most accurate)
                sym_key = symbol.replace("/", "").upper()
                alpaca_pos = alpaca_positions.get(sym_key)
                if not alpaca_pos:
                    logger.warning(f"[MASTERMIND] ⚠️ No Alpaca position for {symbol}, skipping retroactive stop")
                    continue
                live_qty = abs(float(alpaca_pos.qty))

                _sp  = _smart_round(stop_price)
                _lmt = _smart_round(stop_price * 0.995)  # 0.5% below for guaranteed fill

                try:
                    if is_crypto:
                        order = self.broker.api.submit_order(
                            symbol=symbol,
                            qty=live_qty,
                            side="sell",
                            type="stop_limit",
                            stop_price=_sp,
                            limit_price=_lmt,
                            time_in_force="gtc",
                        )
                    else:
                        order = self.broker.api.submit_order(
                            symbol=symbol,
                            qty=live_qty,
                            side="sell",
                            type="stop",
                            stop_price=_sp,
                            time_in_force="gtc",
                        )

                    stop_order_id = getattr(order, "id", None)
                    logger.info(
                        f"[MASTERMIND] 🛡️ Retroactive stop placed for {symbol} at {_sp} "
                        f"(limit={_lmt}) qty={live_qty} id={stop_order_id}"
                    )

                    # Persist the stop order ID back into market_context
                    ctx["alpaca_stop_order_id"] = stop_order_id
                    with sqlite3.connect(self.memory.db_path, timeout=15) as _conn:
                        _conn.execute(
                            "UPDATE trades SET market_context=? WHERE trade_id=?",
                            (json.dumps(ctx), trade["trade_id"]),
                        )

                except Exception as _oe:
                    logger.error(f"[MASTERMIND] ❌ Retroactive stop failed for {symbol}: {_oe}")

        except Exception as e:
            logger.error(f"[MASTERMIND] _place_missing_stops error: {e}")

    def _correct_micro_stops(self):
        """
        One-shot at startup: find open geo trades where stop_loss is a micro-price
        (< $0.001, e.g. SHIB/USD at ~$0.0000092) and whose existing Alpaca stop order
        may have been placed with insufficient decimal precision (rounded to $0.0000).
        Cancels the bad order and replaces it with a correctly-rounded one.
        """
        if not self.memory or not self.broker:
            return
        try:
            open_trades = self.memory.get_open_trades()
            alpaca_positions = {
                p.symbol.replace("/", "").upper(): p
                for p in (self.broker.get_positions() or [])
            }

            for trade in open_trades:
                ctx = trade.get("market_context") or {}
                if isinstance(ctx, str):
                    try:
                        ctx = json.loads(ctx)
                    except Exception:
                        ctx = {}

                if ctx.get("strategy_source") != "geometric":
                    continue

                symbol    = trade["symbol"]
                stop_loss = trade.get("stop_loss")

                # Only check micro-priced symbols (SHIB etc.)
                if not stop_loss or stop_loss >= 0.001:
                    continue

                correct_sp  = _smart_round(stop_loss)
                correct_lmt = _smart_round(stop_loss * 0.995)

                # Fetch existing stop order to check its stored price
                stop_order_id = ctx.get("alpaca_stop_order_id")
                old_stop = None
                if stop_order_id:
                    try:
                        existing = self.broker.api.get_order(stop_order_id)
                        old_stop = float(getattr(existing, "stop_price", None) or 0)
                    except Exception as _fe:
                        logger.debug(f"[MASTERMIND] get_order {stop_order_id}: {_fe}")
                        old_stop = None

                # If the stop is already correct (within floating-point tolerance), skip
                if old_stop is not None and abs(old_stop - correct_sp) < 1e-12:
                    continue

                logger.info(
                    f"[GEO] {symbol} stop corrected: {old_stop} → {correct_sp} "
                    f"(limit={correct_lmt})"
                )

                # Cancel the bad stop order
                if stop_order_id:
                    try:
                        self.broker.api.cancel_order(stop_order_id)
                    except Exception as _ce:
                        logger.debug(f"[MASTERMIND] cancel old stop {stop_order_id}: {_ce}")

                # Get live qty from Alpaca
                sym_key    = symbol.replace("/", "").upper()
                alpaca_pos = alpaca_positions.get(sym_key)
                if not alpaca_pos:
                    logger.warning(f"[MASTERMIND] ⚠️ No Alpaca position for {symbol}, cannot replace stop")
                    continue
                live_qty = abs(float(alpaca_pos.qty))

                # Place corrected stop_limit order
                try:
                    order = self.broker.api.submit_order(
                        symbol=symbol,
                        qty=live_qty,
                        side="sell",
                        type="stop_limit",
                        stop_price=correct_sp,
                        limit_price=correct_lmt,
                        time_in_force="gtc",
                    )
                    new_stop_id = getattr(order, "id", None)
                    logger.info(
                        f"[GEO] {symbol} corrected stop placed: {correct_sp} "
                        f"(limit={correct_lmt}) qty={live_qty} id={new_stop_id}"
                    )

                    ctx["alpaca_stop_order_id"] = new_stop_id
                    with sqlite3.connect(self.memory.db_path, timeout=15) as _conn:
                        _conn.execute(
                            "UPDATE trades SET market_context=? WHERE trade_id=?",
                            (json.dumps(ctx), trade["trade_id"]),
                        )

                except Exception as _oe:
                    logger.error(f"[MASTERMIND] ❌ Micro stop correction failed for {symbol}: {_oe}")

        except Exception as e:
            logger.error(f"[MASTERMIND] _correct_micro_stops error: {e}")

    # ── Daily reset ────────────────────────────────────────────────────────

    def _reset_daily_if_needed(self):
        today = dt.datetime.now(ET).strftime("%Y-%m-%d")
        if self._last_reset_date != today:
            self._gapper_trades_today = 0
            self._gapper_consecutive_losses = 0
            self._last_reset_date = today
            logger.info("[MASTERMIND] 📅 Daily state reset")

    def record_gapper_outcome(self, won: bool):
        """Call this when a gapper trade closes to track day trader rules."""
        self._gapper_trades_today += 1
        if won:
            self._gapper_consecutive_losses = 0
        else:
            self._gapper_consecutive_losses += 1
        logger.info(
            f"[MASTERMIND] Gap outcome: {'WIN' if won else 'LOSS'} | "
            f"today={self._gapper_trades_today} | consec_losses={self._gapper_consecutive_losses}"
        )

    def _compound_capital(self, expert: str, pnl: float):
        """Persist realized P&L into capital_lock.json so capital survives restarts."""
        import json
        lock_file = config._CAPITAL_LOCK_FILE
        try:
            data = {"gapper": config.STRATEGY_CAPITAL["gapper"],
                    "geometric": config.STRATEGY_CAPITAL["geometric"]}
            if hasattr(config, '_CAPITAL_LOCK_FILE') and __import__('os').path.exists(lock_file):
                with open(lock_file) as f:
                    data = json.load(f)
            key = "gapper" if expert == "gapper" else "geometric"
            old = float(data.get(key, config.STRATEGY_CAPITAL[key]))
            data[key] = round(old + pnl, 2)
            data["updated_at"] = dt.datetime.utcnow().isoformat()
            config.STRATEGY_CAPITAL[key] = data[key]
            with open(lock_file, "w") as f:
                json.dump(data, f)
            logger.info(f"[CAPITAL] 💰 {expert} compounded: ${old:.2f} → ${data[key]:.2f} (pnl={pnl:+.2f})")
        except Exception as e:
            logger.error(f"[CAPITAL] compound error: {e}")

    # ── Circuit breaker ────────────────────────────────────────────────────

    def _is_paused(self) -> bool:
        if not self._pause_until:
            return False
        if dt.datetime.now(timezone.utc) >= self._pause_until:
            logger.info(f"[MASTERMIND] ✅ Pause lifted: {self._pause_reason}")
            self._pause_until = None
            self._pause_reason = ""
            return False
        mins = int((self._pause_until - dt.datetime.now(timezone.utc)).total_seconds() / 60)
        logger.info(f"[MASTERMIND] ⏸ Paused ({self._pause_reason}) — {mins}min left")
        return True

    def _pause_entries(self, minutes: int, reason: str):
        self._pause_until = dt.datetime.now(timezone.utc) + dt.timedelta(minutes=minutes)
        self._pause_reason = reason
        logger.warning(f"[MASTERMIND] ⏸ PAUSE {minutes}min — {reason}")

    def _detect_flash_crash(self) -> bool:
        """SPY drops 2%+ in 10 minutes."""
        try:
            bars = self.broker.get_bars("SPY", "1Min", limit=12)
            if bars is None or bars.empty or len(bars) < 10:
                return False
            prices = bars["close"].tolist()
            change = (prices[-1] - prices[-10]) / prices[-10]
            if change <= config.FLASH_CRASH_THRESHOLD:
                logger.warning(f"[MASTERMIND] ⚡ FLASH CRASH: SPY {change*100:.1f}% in 10min")
                return True
        except Exception as e:
            logger.error(f"[MASTERMIND] Flash crash check error: {e}")
        return False

    def _check_vix(self) -> bool:
        """VIX > 35 → panic."""
        vix = self.regime._cache.get("vix")
        if vix and vix > config.VIX_PANIC_THRESHOLD:
            logger.error(f"[MASTERMIND] 🔴 VIX PANIC {vix:.1f}")
            return True
        return False

    def _check_calendar_size_modifier(self) -> float:
        """Returns a size multiplier based on scheduled economic events today."""
        try:
            cal = self.scanner.check_economic_calendar()
            if cal.get("event"):
                logger.info(f"[MASTERMIND] 📅 Economic event today: {cal['event']} → size ×0.70")
                return 0.70  # Reduce all sizes 30% on event days
        except Exception:
            pass
        return 1.0  # Normal

    def _circuit_breaker_fast(self):
        """Fast checks — called every 30s."""
        now_et = dt.datetime.now(ET)
        weekday = now_et.weekday()

        # VIX panic → close everything
        if self._check_vix():
            try: self.broker.close_all_positions()
            except: pass
            self._pause_entries(120, f"VIX panic")
            return

        # Flash crash → pause entries
        if self._detect_flash_crash():
            self._pause_entries(30, "Flash crash SPY")
            return

        # Pre-close stocks 15:55 ET — call existing V1 method
        if weekday < 5 and now_et.hour == 15 and now_et.minute >= 55:
            try:
                positions = self.broker.get_positions() or []
                today_str = now_et.strftime("%Y-%m-%d")
                self.agent._preclose_stocks(positions, today_str)
            except Exception as e:
                logger.error(f"[MASTERMIND] Pre-close error: {e}")

        # Pre-market liberation 9:25 ET — call existing V1 method
        if weekday < 5 and now_et.hour == 9 and 23 <= now_et.minute <= 28:
            try:
                self.agent._pre_market_cash_liberation()
            except Exception as e:
                logger.error(f"[MASTERMIND] Liberation error: {e}")

    def _circuit_breaker_slow(self):
        """Slow checks — called every 5min."""
        # Calendar size modifier — refresh every slow cycle
        self._size_modifier = self._check_calendar_size_modifier()

        # Earnings day check — log symbols to skip
        try:
            earnings = self.scanner.get_earnings_alerts()
            for ea in earnings:
                if ea["type"] == "earnings_day":
                    logger.info(f"[MASTERMIND] 🚨 EARNINGS DAY: {ea['symbol']} — geometric will skip")
        except Exception:
            pass

    # ── Minimum filter ─────────────────────────────────────────────────────

    def _passes_minimum_filter(self, symbol: str, price: float, volume: int) -> bool:
        now = dt.datetime.now(timezone.utc)
        expired = [s for s, exp in self._halted_symbols.items() if now >= exp]
        for s in expired:
            del self._halted_symbols[s]
        if symbol in self._halted_symbols:
            return False
        if price < 1.0:
            return False
        if volume < 100_000:
            return False
        return True

    # ── Float enrichment ───────────────────────────────────────────────────

    def _enrich_with_float(self, candidate: dict):
        """Fetch float shares via yfinance. Non-blocking — skips on error."""
        try:
            import yfinance as yf
            info = yf.Ticker(candidate["symbol"]).info
            candidate["float_shares"] = info.get("floatShares")
            candidate["short_interest"] = info.get("shortPercentOfFloat")
            candidate["short_pct"] = info.get("shortPercentOfFloat") or 0.0
        except Exception:
            candidate["float_shares"] = None
            candidate["short_interest"] = None
            candidate["short_pct"] = 0.0

    # ── Day trader rules ───────────────────────────────────────────────────

    def _can_gapper_trade(self) -> bool:
        if self._gapper_trades_today >= config.GAPPER_MAX_TRADES_PER_DAY:
            logger.info(f"[MASTERMIND] Gap: max {config.GAPPER_MAX_TRADES_PER_DAY} trades/day reached")
            return False
        if self._gapper_consecutive_losses >= config.GAPPER_MAX_CONSECUTIVE_LOSSES:
            logger.info(f"[MASTERMIND] Gap: {config.GAPPER_MAX_CONSECUTIVE_LOSSES} consecutive losses — stopped")
            return False
        now_et = dt.datetime.now(ET)
        if now_et.weekday() >= 5:
            return False
        t = now_et.time()
        if not (dt.time(9, 30) <= t <= dt.time(11, 0)):
            return False
        return True

    # ── Job 1: Signal detection ────────────────────────────────────────────

    def _detect_gappers(self):
        """Detect gapper candidates. Called every 30s from fast loop."""
        now_et = dt.datetime.now(ET)
        if now_et.weekday() >= 5:
            return
        t = now_et.time()

        # Gapper window: pre-market 4h-9h30 + open 9h30-11h
        if not (dt.time(4, 0) <= t <= dt.time(11, 0)):
            return

        try:
            if t < dt.time(9, 30):
                # Pre-market: use dedicated scanner
                candidates = self.scanner.get_premarket_gappers()
            else:
                # Market hours: use top movers, filter for real gappers
                movers = self.scanner.get_top_movers(top_n=10)
                for m in movers:
                    chg = m.get("change_pct", 0)
                    vol = m.get("volume_ratio", 0)
                    verdict = "PASS" if chg >= 20 and vol >= 3 else "FAIL"
                    logger.info(
                        f"[MASTERMIND] Mover: {m.get('symbol','')} "
                        f"change={chg:.1f}% vol={vol:.1f}x — {verdict}"
                    )
                candidates = [
                    m for m in movers
                    if m.get("change_pct", 0) >= 20 and m.get("volume_ratio", 0) >= 3
                ]

            if not candidates:
                return

            # Sort by conviction: gap × volume — strongest first
            candidates.sort(key=lambda x: x.get("change_pct", 0) * x.get("volume_ratio", 0), reverse=True)

            for candidate in candidates[:3]:  # Top 3 max per tick
                sym = candidate.get("symbol", "")
                price = candidate.get("price", 0)
                volume = candidate.get("volume", 0)

                if not self._passes_minimum_filter(sym, price, volume):
                    continue

                # Enrich with float (non-blocking)
                self._enrich_with_float(candidate)

                logger.info(
                    f"[MASTERMIND] 🚨 GAPPER DETECTED: {sym} "
                    f"+{candidate.get('change_pct', 0):.1f}% "
                    f"vol={candidate.get('volume_ratio', 0):.1f}x "
                    f"→ routing to GapperExpert"
                )

                if self._can_gapper_trade() and self.gapper.has_capital():
                    self.gapper.evaluate(candidate, size_modifier=self._size_modifier)

        except Exception as e:
            logger.error(f"[MASTERMIND] Gapper detection error: {e}")

    def _detect_geometric(self):
        """Detect geometric candidates. Called every 5min from slow loop."""
        try:
            watchlist = self.scanner.get_dynamic_watchlist()
            routed = 0
            for symbol in watchlist[:20]:  # Cap at 20 to avoid overload
                self.geometric.add_candidate(symbol)
                routed += 1

            if routed > 0:
                logger.info(f"[MASTERMIND] 📐 {routed} geometric candidates queued")

            # Execute all candidates (was [:5] — crypto at positions 16-24 was never reached)
            if self.geometric.has_capital():
                candidates = self.geometric.flush_candidates()
                logger.info(f"[MASTERMIND] 📐 Evaluating {len(candidates)} geo candidates this cycle")
                # Night multiplier: 2h-6h UTC = 40% size (low liquidity window)
                _hour_utc = dt.datetime.now(timezone.utc).hour
                _night_mult = 0.4 if 2 <= _hour_utc < 6 else 1.0
                _eff_modifier = round(self._size_modifier * _night_mult, 4)
                if _night_mult < 1.0:
                    logger.info(f"[MASTERMIND] 🌙 Night window (02-06 UTC) — size modifier {self._size_modifier:.2f} × 0.40 = {_eff_modifier:.2f}")
                _regime_str = self.regime.detect_regime()
                logger.info(f"[MASTERMIND] 📊 Regime for geo evaluation: {_regime_str.upper()}")
                for symbol in candidates:
                    self.geometric.evaluate(symbol, size_modifier=_eff_modifier, regime=_regime_str)
            else:
                self.geometric.flush_candidates()  # Clear queue

        except Exception as e:
            logger.error(f"[MASTERMIND] Geometric detection error: {e}")

    # ── Main entry points ──────────────────────────────────────────────────

    def fast_tick(self):
        """Called every 30s from main.py fast loop."""
        self._reset_daily_if_needed()
        self._circuit_breaker_fast()
        if self._is_paused():
            return
        self._detect_gappers()
        # Manage open expert positions
        try:
            self.gapper.manage_open_positions()
            self.geometric.manage_open_positions()
        except Exception as e:
            logger.error(f"[MASTERMIND] position management error: {e}")

        # Track gapper outcomes for day trader rules (consecutive losses, daily count)
        try:
            if self.memory and hasattr(self.memory, 'db_path'):
                import json, sqlite3
                conn = sqlite3.connect(self.memory.db_path, timeout=5)
                conn.row_factory = sqlite3.Row
                # Query recently closed gapper trades that haven't been recorded yet
                # Uses exit_at DESC so freshly closed trades always appear regardless of limit
                rows = conn.execute("""
                    SELECT trade_id, pnl, market_context
                    FROM trades
                    WHERE status = 'closed'
                      AND close_reason IN ('time_limit', 'trailing_stop', 'hard_stop_loss')
                      AND json_extract(market_context, '$.strategy_source') = 'gapper'
                      AND (json_extract(market_context, '$.outcome_recorded') IS NULL
                           OR json_extract(market_context, '$.outcome_recorded') = 0)
                    ORDER BY exit_at DESC
                    LIMIT 20
                """).fetchall()
                for row in rows:
                    ctx = {}
                    try: ctx = json.loads(row["market_context"] or "{}")
                    except: pass
                    pnl = row["pnl"] or 0
                    won = pnl > 0
                    self.record_gapper_outcome(won)
                    if pnl != 0:
                        self._compound_capital("gapper", pnl)
                    new_ctx = {**ctx, "outcome_recorded": True, "capital_recorded": True}
                    conn.execute("UPDATE trades SET market_context=? WHERE trade_id=?",
                                 (json.dumps(new_ctx), row["trade_id"]))
                conn.commit()
                conn.close()
        except Exception as e:
            logger.error(f"[MASTERMIND] outcome tracking error: {e}")

    def _compound_geo_closed(self):
        """Scan for newly closed geo trades and compound their P&L into the capital lock."""
        try:
            if not (self.memory and hasattr(self.memory, 'db_path')):
                return
            conn = sqlite3.connect(self.memory.db_path, timeout=5)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT trade_id, pnl, market_context
                FROM trades
                WHERE status = 'closed'
                  AND json_extract(market_context, '$.strategy_source') = 'geometric'
                  AND close_reason NOT IN ('synced_close', 'orphan_close')
                  AND (json_extract(market_context, '$.capital_recorded') IS NULL
                       OR json_extract(market_context, '$.capital_recorded') = 0)
                  AND pnl IS NOT NULL AND pnl != 0
                ORDER BY exit_at DESC
                LIMIT 20
            """).fetchall()
            for row in rows:
                ctx = {}
                try: ctx = json.loads(row["market_context"] or "{}")
                except: pass
                pnl = row["pnl"] or 0
                if pnl != 0:
                    self._compound_capital("geometric", pnl)
                new_ctx = {**ctx, "capital_recorded": True}
                conn.execute("UPDATE trades SET market_context=? WHERE trade_id=?",
                             (json.dumps(new_ctx), row["trade_id"]))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"[MASTERMIND] geo compounding error: {e}")

    def run(self):
        """Called every 5min from main.py slow loop."""
        if self._is_paused():
            return
        self._circuit_breaker_slow()
        self._compound_geo_closed()
        self._detect_geometric()
        logger.info(
            f"[MASTERMIND] ✅ Cycle | "
            f"Gap: {self._gapper_trades_today}/{config.GAPPER_MAX_TRADES_PER_DAY} trades "
            f"| consec_losses={self._gapper_consecutive_losses} "
            f"| Gap capital=${self.gapper.get_available_capital():.0f} "
            f"| Geo capital=${self.geometric.get_available_capital():.0f}"
        )
