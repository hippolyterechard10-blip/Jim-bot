import json
import logging
import sqlite3
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
        self.risk.regime   = self.regime
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

    def _reconcile_stale_positions(self):
        """
        Inverse of _sync_orphan_positions: finds DB 'open' records that have
        NO corresponding live Alpaca position, and marks them as 'closed'.
        This handles cases where positions were closed in Alpaca (trailing stop,
        manual close, bracket order fill) between agent restarts.
        """
        if not self.memory:
            return
        try:
            alpaca_positions = self.broker.get_positions()
            alpaca_symbols = {p.symbol.replace("/", "").upper() for p in alpaca_positions}
            open_trades = self.memory.get_open_trades()
            if not open_trades:
                return
            now_ts = datetime.now(timezone.utc).isoformat()
            conn = sqlite3.connect(self.memory.db_path, timeout=10)
            c = conn.cursor()
            reconciled = 0
            for t in open_trades:
                sym_clean = t["symbol"].replace("/", "").upper()
                if sym_clean in alpaca_symbols:
                    continue
                # Try to get a current price for rough PNL estimate
                try:
                    bars = self.broker.api.get_bars(sym_clean, "1Min", limit=1).df
                    exit_price = float(bars["close"].iloc[-1]) if not bars.empty else t.get("entry_price") or 0
                except Exception:
                    exit_price = t.get("entry_price") or 0
                entry = t.get("entry_price") or 0
                qty = t.get("qty") or 0
                pnl = pnl_pct = None
                if entry and exit_price and qty:
                    diff = exit_price - entry
                    if t.get("side") == "sell":
                        diff = -diff
                    pnl = round(diff * qty, 6)
                    pnl_pct = round(diff / entry * 100, 4)
                c.execute("""UPDATE trades SET
                    status='closed', exit_price=?, pnl=?, pnl_pct=?,
                    close_reason='position_reconciled', exit_at=?
                    WHERE trade_id=?
                """, (exit_price or None, pnl, pnl_pct, now_ts, t["trade_id"]))
                reconciled += 1
                logger.info(
                    f"🔄 RECONCILE stale: {t['symbol']} not in Alpaca → marked closed "
                    f"(est. exit={exit_price:.4f} pnl~={pnl})"
                )
            if reconciled:
                conn.commit()
                logger.info(f"✅ Reconciled {reconciled} stale open record(s)")
            conn.close()
        except Exception as e:
            logger.error(f"_reconcile_stale_positions error: {e}")

    def _sync_todays_orders(self):
        """
        Startup reconciliation: fetch today's filled Alpaca orders and create
        any missing DB records. Handles the crash-between-place_order-and-log case.
        - Filled BUY with no DB record → create open record
        - Filled SELL with DB open match → create close record
        - Filled SELL with no DB match → create synthetic open+close pair
        """
        if not self.memory:
            return
        try:
            from datetime import date as _date
            today_str = _date.today().isoformat() + "T00:00:00Z"
            orders = self.broker.api.list_orders(status="filled", after=today_str, limit=100)
            if not orders:
                return

            # Normalise symbol to our display format (e.g. "LINKUSD" → "LINK/USD")
            def _display(sym):
                for c in config.CRYPTO_SYMBOLS:
                    if c.replace("/", "").upper() == sym.replace("/", "").upper():
                        return c
                return sym

            conn = sqlite3.connect(self.memory.db_path, timeout=10)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            # Process in chronological order
            for o in sorted(orders, key=lambda x: x.filled_at or x.submitted_at):
                display_sym  = _display(o.symbol)
                fill_price   = float(getattr(o, "filled_avg_price", 0) or 0)
                fill_qty     = float(getattr(o, "filled_qty", 0) or 0)
                filled_at    = str(o.filled_at)[:19] if o.filled_at else str(o.submitted_at)[:19]
                order_id     = o.id

                if not fill_price or not fill_qty:
                    continue

                if o.side == "buy":
                    # Check if a DB record already covers this order
                    c.execute(
                        "SELECT trade_id FROM trades WHERE alpaca_order_id=?", (order_id,)
                    )
                    if c.fetchone():
                        continue
                    # Also check by symbol+qty+date to avoid near-duplicates
                    day = filled_at[:10]
                    c.execute(
                        "SELECT trade_id FROM trades WHERE symbol=? AND ABS(qty-?)<=0.1 AND entry_at LIKE ?",
                        (display_sym, fill_qty, day + "%")
                    )
                    if c.fetchone():
                        continue
                    # Also skip if an open record for this symbol already exists with
                    # qty close to the Alpaca live position (the orphan sync already covers it)
                    c.execute(
                        "SELECT trade_id, qty FROM trades WHERE symbol=? AND status='open'",
                        (display_sym,)
                    )
                    existing_opens = c.fetchall()
                    total_open_qty = sum(r["qty"] for r in existing_opens)
                    # If existing open records already cover more qty than this order → skip
                    if total_open_qty >= fill_qty * 0.9:
                        continue
                    new_id = str(uuid.uuid4())
                    c.execute(
                        """INSERT INTO trades
                           (trade_id, alpaca_order_id, symbol, side, qty, entry_price, status, entry_at, market_context)
                           VALUES (?,?,?,?,?,?,'open',?,?)""",
                        (new_id, order_id, display_sym, "buy", fill_qty, fill_price,
                         filled_at, json.dumps({"source": "order_sync"}))
                    )
                    logger.info(f"[OrderSync] Created missing open: {display_sym} buy {fill_qty} @ {fill_price:.4f}")

                elif o.side == "sell":
                    # Check if close already logged for this sell order
                    c.execute(
                        "SELECT trade_id FROM trades WHERE alpaca_order_id=? AND status='closed'",
                        (order_id,)
                    )
                    if c.fetchone():
                        continue
                    # Find oldest open DB record for this symbol to close
                    c.execute(
                        "SELECT trade_id, entry_price, qty, entry_at FROM trades "
                        "WHERE symbol=? AND status='open' ORDER BY entry_at ASC LIMIT 1",
                        (display_sym,)
                    )
                    match = c.fetchone()
                    if match:
                        ep     = match["entry_price"] or fill_price
                        pnl    = round((fill_price - ep) * fill_qty, 4)
                        pnl_pct = round((fill_price - ep) / ep * 100, 4) if ep else 0
                        try:
                            entry_dt = datetime.fromisoformat(match["entry_at"])
                            exit_dt  = datetime.fromisoformat(filled_at)
                            hold_min = (exit_dt - entry_dt).total_seconds() / 60
                        except Exception:
                            hold_min = None
                        c.execute(
                            """UPDATE trades SET status='closed', exit_price=?, exit_at=?,
                               pnl=?, pnl_pct=?, close_reason='synced_close',
                               hold_duration_min=? WHERE trade_id=?""",
                            (fill_price, filled_at, pnl, pnl_pct, hold_min, match["trade_id"])
                        )
                        logger.info(
                            f"[OrderSync] Synced close: {display_sym} sell {fill_qty} @ {fill_price:.4f} "
                            f"pnl={pnl:+.4f}"
                        )
                    else:
                        # No DB open record: create synthetic open+close pair
                        # Use fill_price as both entry and exit (unknown entry)
                        new_id = str(uuid.uuid4())
                        c.execute(
                            """INSERT INTO trades
                               (trade_id, alpaca_order_id, symbol, side, qty, entry_price,
                                exit_price, pnl, pnl_pct, status, close_reason,
                                entry_at, exit_at, hold_duration_min, market_context)
                               VALUES (?,?,?,?,?,?,?,0,0,'closed','synced_close',?,?,0,?)""",
                            (new_id, order_id, display_sym, "buy", fill_qty,
                             fill_price, fill_price, filled_at, filled_at,
                             json.dumps({"source": "order_sync_synthetic"}))
                        )
                        logger.info(
                            f"[OrderSync] Synthetic close (no open match): {display_sym} "
                            f"sell {fill_qty} @ {fill_price:.4f}"
                        )

            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"_sync_todays_orders error: {e}")

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

    def _build_entry_snapshot(self, symbol, indicators, patterns, session_ctx,
                               synth, decision, current_price, base_score=None):
        """Capture full entry context as a dict for post-trade analysis."""
        try:
            ind = indicators or {}
            pat = patterns   or {}
            s   = synth      or {}
            d   = decision   or {}
            sess = session_ctx.get("session") if isinstance(session_ctx, dict) else session_ctx
            return {
                "rsi":           ind.get("rsi"),
                "macd_bullish":  ind.get("macd_bullish"),
                "volume_ratio":  ind.get("volume_ratio"),
                "atr_pct":       ind.get("atr_pct"),
                "bb_pct":        ind.get("bb_pct"),
                "momentum_5":    ind.get("momentum_5"),
                "above_sma20":   ind.get("above_sma20"),
                "change_pct":    ind.get("change_pct"),
                "patterns":      pat.get("patterns", []),
                "base_score":    base_score,
                "final_score":   s.get("final_score"),
                "score_breakdown": s.get("score_breakdown"),
                "regime":        s.get("regime") or self.regime._cache.get("regime", "unknown"),
                "support":       s.get("support"),
                "resistance":    s.get("resistance"),
                "risk_reward":   s.get("risk_reward"),
                "stop_pct":      s.get("stop_pct"),
                "target_pct":    s.get("target_pct"),
                "session":       sess,
                "confidence":    d.get("confidence"),
                "strategy_used": d.get("strategy_used"),
                "reasoning":     (d.get("reasoning") or "")[:500] or None,
                "entry_price":   current_price,
            }
        except Exception:
            return None

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
                                open_trades = self.memory.get_open_trades()
                                match = next(
                                    (t for t in open_trades
                                     if t.get("symbol", "").replace("/", "") == symbol.replace("/", "")),
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
        # hold_overnight applies to stocks only — crypto positions are never force-closed by session rules
        if self.regime.get_params().get("hold_overnight", False):
            logger.info("[Pre-close] hold_overnight=True (bull regime) — keeping equity positions open")
            return
        stock_positions = [p for p in positions if not self._is_crypto(p.symbol)]
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
                            open_trades = self.memory.get_open_trades()
                            match = next(
                                (t for t in open_trades
                                 if t.get("symbol", "") == symbol),
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
                            open_trades = self.memory.get_open_trades()
                            match = next(
                                (t for t in open_trades
                                 if t.get("symbol", "") == symbol),
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
            # hold_overnight applies to stocks only — crypto positions are never force-closed by session rules
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
                _cm_all = self.scanner._movers_cache.get("symbols", [])
                _mi = next((m for m in _cm_all if m["symbol"] == symbol), None)
                if _mi and _mi.get("is_gapper") and abs(_mi.get("change_pct", 0)) > 20:
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
                            # ── Buying power guard ───────────────────────────────
                            try:
                                bp = float(self.broker.api.get_account().buying_power)
                                if amount > bp * 0.95:
                                    logger.warning(
                                        f"[FAST] ⚠️ Skip {symbol}: need ${amount:.2f} "
                                        f"but only ${bp:.2f} buying power available"
                                    )
                                    return
                            except Exception:
                                pass
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
                                    entry_snap = self._build_entry_snapshot(
                                        symbol, indicators, patterns, session_ctx,
                                        synth, decision, current_price, base_score=score,
                                    )
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
                                        entry_snapshot=entry_snap,
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
                                open_trades = self.memory.get_open_trades()
                                match = next(
                                    (t for t in open_trades
                                     if t.get("symbol", "").replace("/", "") == symbol.replace("/", "")),
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
                                open_trades = self.memory.get_open_trades()
                                match = next(
                                    (t for t in open_trades
                                     if t.get("symbol", "").replace("/", "") == symbol.replace("/", "")
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
        # Inverse: mark any DB-open records that no longer exist in Alpaca as closed
        self._reconcile_stale_positions()

        # NOTE: Trailing stop management is handled exclusively by the fast loop (every 30s).
        # No trailing stop call here to avoid double-firing on simultaneous startup.

        if getattr(config, 'TRADING_ENGINE', 'V1') == 'V2':
            # V2 mode: position sync and reconciliation only — trading delegated to Mastermind experts
            self._sync_orphan_positions()
            self._reconcile_stale_positions()
            logger.info("[SLOW] V2 mode — trade logic skipped, Mastermind handles entries")
            return

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
                _cm_all = self.scanner._movers_cache.get("symbols", [])
                _mi = next((m for m in _cm_all if m["symbol"] == symbol), None)
                if _mi and _mi.get("is_gapper") and abs(_mi.get("change_pct", 0)) > 20:
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

                    # ── Buying power guard ──────────────────────────────────
                    try:
                        bp = float(self.broker.api.get_account().buying_power)
                        if amount > bp * 0.95:
                            logger.warning(
                                f"⚠️ Skip {symbol}: need ${amount:.2f} "
                                f"but only ${bp:.2f} buying power available"
                            )
                            continue
                    except Exception:
                        pass

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
        if not getattr(config, "ALLOW_SHORTS", False):
            return  # Shorts disabled — Alpaca account not configured for shorting

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
                    logger.info(f"SHORT skip {symbol}: Claude conf={conf:.0%} >= {config.SHORT_ENTRY_CONF_MAX:.0%}")
                    continue

            # No existing position in this symbol
            sym_key = symbol.replace("/", "")
            if sym_key in open_positions:
                pos_side = getattr(open_positions[sym_key], "side", "long")
                logger.info(f"SHORT skip {symbol}: already in {pos_side} position")
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
                    entry_snap = self._build_entry_snapshot(
                        symbol, data["indicators"], data["patterns"], session_ctx,
                        data.get("synthesis"), None, current_price, base_score=opp_score_short,
                    )
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
                        entry_snapshot=entry_snap,
                    )
                except Exception as me:
                    logger.warning(f"Memory log short entry: {me}")

        if short_candidates == 0:
            logger.info("SHORT PASS: no candidates met entry conditions")
