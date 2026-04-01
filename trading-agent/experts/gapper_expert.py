"""
GapperExpert — Pilier 1 ($500 virtual pool)
STUB: evaluate() logs candidates only. No trades placed until next session.
Metrics: Float (primary), Gap%, Volume, Catalyst type, Short interest.
Stop: First 5-min candle low -0.5% | Hard: -10% | Breakeven after +10% partial.
"""
import logging, json
import config

logger = logging.getLogger(__name__)

class GapperExpert:
    def __init__(self, broker, memory, scanner, regime):
        self.broker = broker
        self.memory = memory
        self.scanner = scanner
        self.regime = regime
        self._candidates = []
        self._high_water: dict = {}

    def add_candidate(self, mover: dict):
        self._candidates.append(mover)

    def flush_candidates(self) -> list:
        c = list(self._candidates)
        self._candidates.clear()
        return c

    def get_deployed_capital(self) -> float:
        try:
            total = 0.0
            for t in self.memory.get_open_trades():
                ctx = t.get("market_context") or {}
                if isinstance(ctx, str):
                    try: ctx = json.loads(ctx)
                    except: ctx = {}
                if ctx.get("strategy_source") == "gapper":
                    total += float(t.get("entry_price", 0)) * float(t.get("qty", 0))
            return total
        except Exception as e:
            logger.error(f"GapperExpert.get_deployed_capital: {e}")
            return config.STRATEGY_CAPITAL["gapper"]

    def get_available_capital(self) -> float:
        try:
            import sqlite3
            base = config.STRATEGY_CAPITAL["gapper"]
            if not self.memory or not hasattr(self.memory, 'db_path'):
                return max(0.0, base - self.get_deployed_capital())
            conn = sqlite3.connect(self.memory.db_path, timeout=5)
            row = conn.execute("""
                SELECT COALESCE(SUM(pnl), 0.0) FROM trades
                WHERE status = 'closed'
                  AND json_extract(market_context, '$.strategy_source') = 'gapper'
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
            logger.error(f"GapperExpert.get_available_capital: {e}")
            return max(0.0, config.STRATEGY_CAPITAL["gapper"] - self.get_deployed_capital())

    def has_capital(self) -> bool:
        return self.get_available_capital() >= 50.0

    def evaluate(self, candidate: dict, size_modifier: float = 1.0):
        import pytz
        from datetime import datetime, timezone
        import uuid

        ET = pytz.timezone("America/New_York")
        now_et = datetime.now(ET)
        t = now_et.time()

        from datetime import time as dtime
        if not (dtime(9, 35) <= t <= dtime(10, 45)):
            logger.info(f"[GAPPER] {candidate.get('symbol')} — outside entry window (9:35-10:45 ET), skip")
            return

        symbol = candidate.get("symbol", "")

        # Guard: block double-entry — skip if an open trade already exists for this symbol
        if self.memory:
            open_syms = {t.get("symbol") for t in self.memory.get_open_trades()}
            if symbol in open_syms:
                logger.debug(f"[GAPPER] {symbol} — already have open position, skip")
                return

        change_pct = candidate.get("change_pct", 0)
        float_shares = candidate.get("float_shares")

        # Float-based sizing
        if float_shares:
            if float_shares < 1_000_000:
                size_pct = 0.10
            elif float_shares < 10_000_000:
                size_pct = 0.20
            elif float_shares < 50_000_000:
                size_pct = 0.10
            else:
                logger.info(f"[GAPPER] {symbol} — float {float_shares/1e6:.1f}M too large, skip")
                return
        else:
            size_pct = 0.12  # default if float unknown

        # Get 1-min bars
        bars = self.broker.get_bars(symbol, "1Min", limit=50)
        if bars is None or bars.empty:
            logger.warning(f"[GAPPER] {symbol} — no bars available")
            return

        # Identify first 5-min candle (9:30-9:35 ET)
        try:
            bars_et = bars.copy()
            if bars_et.index.tz is None:
                bars_et.index = bars_et.index.tz_localize("UTC")
            bars_et.index = bars_et.index.tz_convert(ET)

            first_candle_bars = bars_et.between_time("09:30", "09:34")
            if first_candle_bars.empty:
                logger.info(f"[GAPPER] {symbol} — first 5-min candle not yet formed")
                return

            first_candle_high = float(first_candle_bars["high"].max())
            first_candle_low = float(first_candle_bars["low"].min())
        except Exception as e:
            logger.error(f"[GAPPER] {symbol} first candle error: {e}")
            return

        current_price = float(bars["close"].iloc[-1])
        current_volume = float(bars["volume"].iloc[-1])
        avg_volume = float(bars["volume"].tail(20).mean()) if len(bars) >= 20 else current_volume

        # Entry condition: price breaking above first candle high with volume
        if current_price <= first_candle_high:
            logger.info(
                f"[GAPPER] {symbol} — price ${current_price:.2f} not above first candle high "
                f"${first_candle_high:.2f}, waiting for breakout"
            )
            return

        if current_volume < avg_volume * 1.2:
            logger.info(
                f"[GAPPER] {symbol} — volume not confirming breakout "
                f"({current_volume:.0f} vs avg {avg_volume:.0f})"
            )
            return

        # Calculate stops and targets
        stop_price = round(first_candle_low * 0.995, 4)   # first candle low -0.5%
        hard_stop = round(current_price * 0.90, 4)         # -10% hard max
        stop_price = max(stop_price, hard_stop)            # use whichever is tighter

        risk_pct = (current_price - stop_price) / current_price
        if risk_pct > 0.10:
            logger.info(f"[GAPPER] {symbol} — stop too wide ({risk_pct*100:.1f}%), skip")
            return

        # Check capital available
        available = self.get_available_capital()
        if available < 50:
            logger.info(f"[GAPPER] {symbol} — insufficient capital (${available:.0f})")
            return

        # If first trade of day was a loss, reduce size by 50%
        from_memory = self._get_todays_first_outcome()
        if from_memory == "loss":
            size_pct *= 0.5

        # Calculate position size (apply calendar event reduction if active)
        capital_to_use = min(available, config.STRATEGY_CAPITAL["gapper"] * min(size_pct, 0.40))
        capital_to_use *= size_modifier
        if size_modifier < 1.0:
            logger.info(f"[GAPPER] 📅 Calendar modifier ×{size_modifier:.2f} → capital=${capital_to_use:.0f}")
        qty = max(1, int(capital_to_use / current_price))

        if qty * current_price < 20:
            logger.info(f"[GAPPER] {symbol} — position too small (${qty*current_price:.0f}), skip")
            return

        if float_shares:
            logger.info(
                f"[GAPPER] 🚀 ENTERING: {symbol} | price=${current_price:.4f} | "
                f"stop=${stop_price:.4f} (-{risk_pct*100:.1f}%) | "
                f"qty={qty} | capital=${qty*current_price:.0f} | "
                f"gap={change_pct:+.1f}% | float={float_shares/1e6:.1f}M"
            )
        else:
            logger.info(
                f"[GAPPER] 🚀 ENTERING: {symbol} | price=${current_price:.4f} | "
                f"stop=${stop_price:.4f} | qty={qty}"
            )

        order = self.broker.place_order(symbol, qty, "buy", stop_loss=stop_price)

        if order and self.memory:
            trade_id = str(uuid.uuid4())
            self.memory.log_trade_open(
                trade_id=trade_id,
                symbol=symbol,
                side="buy",
                qty=qty,
                entry_price=current_price,
                stop_loss=stop_price,
                alpaca_order_id=getattr(order, "id", None),
                market_context={
                    "strategy_source": "gapper",
                    "change_pct": change_pct,
                    "first_candle_high": first_candle_high,
                    "first_candle_low": first_candle_low,
                    "float_shares": float_shares,
                    "target_partial": round(current_price * 1.10, 4),
                    "target_pct": 10.0,
                }
            )

    def _get_todays_first_outcome(self) -> str:
        """Returns 'win', 'loss', or None for today's first gapper trade."""
        try:
            import pytz, json
            from datetime import datetime
            ET = pytz.timezone("America/New_York")
            today = datetime.now(ET).strftime("%Y-%m-%d")
            recent = self.memory.get_recent_trades(limit=20)
            for t in recent:
                ctx = t.get("market_context") or {}
                if isinstance(ctx, str):
                    try: ctx = json.loads(ctx)
                    except: ctx = {}
                if ctx.get("strategy_source") == "gapper" and t.get("exit_at", "")[:10] == today:
                    return "win" if (t.get("pnl") or 0) > 0 else "loss"
            return None
        except Exception:
            return None

    def manage_open_positions(self):
        """Called from fast loop to manage partial profit and trailing stop for gapper positions."""
        try:
            import json
            import pytz
            from datetime import datetime, time as dtime

            positions = self.broker.get_positions()
            open_trades = self.memory.get_open_trades()

            for pos in (positions or []):
                # Find matching gapper trade
                match = None
                ctx_data = {}
                for t in open_trades:
                    ctx = t.get("market_context") or {}
                    if isinstance(ctx, str):
                        try: ctx = json.loads(ctx)
                        except: ctx = {}
                    if (ctx.get("strategy_source") == "gapper"
                            and t.get("symbol") == pos.symbol
                            and t.get("status") == "open"):
                        match = t
                        ctx_data = ctx
                        break

                if not match:
                    continue

                current_price = float(pos.current_price)
                entry_price = float(match.get("entry_price", current_price))
                qty = float(pos.qty)
                gain_pct = (current_price - entry_price) / entry_price * 100

                symbol = pos.symbol
                target_pct = ctx_data.get("target_pct", 10.0)
                partial_taken = ctx_data.get("partial_taken", False)

                # Check time limit (10:45 ET) — force exit
                ET = pytz.timezone("America/New_York")
                now_et = datetime.now(ET)
                if now_et.weekday() < 5 and now_et.time() >= dtime(10, 45):
                    logger.info(f"[GAPPER] ⏰ Time limit 10:45 ET — closing {symbol}")
                    self.broker.close_position(symbol)
                    if self.memory:
                        pnl = (current_price - entry_price) * qty
                        self.memory.log_trade_close(match["trade_id"], current_price, "time_limit", pnl=pnl)
                    continue

                # Partial profit at +10% → sell 50%, move stop to breakeven
                if gain_pct >= target_pct and not partial_taken:
                    sell_qty = max(1, int(qty * 0.50))
                    logger.info(
                        f"[GAPPER] 💰 PARTIAL PROFIT: {symbol} +{gain_pct:.1f}% "
                        f"→ selling 50%, stop→breakeven"
                    )
                    self.broker.place_order(symbol, sell_qty, "sell")
                    if self.memory:
                        ctx_data["partial_taken"] = True
                        ctx_data["breakeven_stop"] = entry_price
                        self.memory.log_trade_close(
                            match["trade_id"], current_price, "partial_profit",
                            pnl=(current_price - entry_price) * sell_qty
                        )
                        remaining_qty = qty - sell_qty
                        if remaining_qty > 0:
                            self.memory.log_trade_open(
                                trade_id=str(__import__("uuid").uuid4()),
                                symbol=symbol,
                                side="buy",
                                qty=remaining_qty,
                                entry_price=entry_price,
                                stop_loss=entry_price,  # breakeven
                                market_context={**ctx_data, "partial_taken": True}
                            )
                    continue

                # Trailing stop -15% from highest price (after partial)
                if partial_taken:
                    # Use in-memory high_water (ctx_data version resets every tick from SQLite)
                    if symbol not in self._high_water:
                        self._high_water[symbol] = current_price
                    self._high_water[symbol] = max(self._high_water[symbol], current_price)
                    trailing_stop = self._high_water[symbol] * 0.85

                    if current_price <= trailing_stop:
                        logger.info(
                            f"[GAPPER] 🔴 TRAILING STOP: {symbol} "
                            f"${current_price:.4f} <= ${trailing_stop:.4f}"
                        )
                        self.broker.close_position(symbol)
                        if self.memory:
                            pnl = (current_price - entry_price) * qty
                            self.memory.log_trade_close(
                                match["trade_id"], current_price, "trailing_stop", pnl=pnl
                            )

        except Exception as e:
            logger.error(f"[GAPPER] manage_open_positions error: {e}")
