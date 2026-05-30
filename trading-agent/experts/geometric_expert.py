"""
geometric_expert.py — Geo V4 ETH+SOL (OKX broker)
Stratégie validée backtest 2022-2025 :
  - Zones ±0.3% autour des pivots 15min
  - Limit order à zone["high"] (support × 1.003)
  - Stop dynamique sous wick réel
  - RSI divergence [20-65] + Pass 3b
  - Zone freshness MAX_TOUCHES=2
  - Target +0.9%

Broker OKX : SL + TP attachés à l'ordre d'entrée (bracket natif).
manage_open_positions() simplifié : détecte fermeture par comparaison
positions OKX ↔ DB. Aucun price-check bot-side.
"""
from __future__ import annotations
import logging, json, threading, uuid, datetime, time, os
from collections import defaultdict
import numpy as np
import pandas as pd
import config
try:
    import openclaw_notify as notify
    _NOTIFY = True
except ImportError:
    _NOTIFY = False

logger = logging.getLogger(__name__)

TIMEOUT_MIN = 120  # 2h time-stop (default — bull/choppy override via GEO_LOWVOL_TIMEOUT_MIN)


def _smart_round(price: float) -> float:
    if price >= 100:    return round(price, 2)
    elif price >= 1:    return round(price, 4)
    elif price >= 0.01: return round(price, 6)
    else:               return round(price, 8)


def _rsi(closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1: return 50.0
    c = np.array(closes, dtype=float)
    d = np.diff(c)
    g = np.where(d > 0, d, 0.0)
    l = np.where(d < 0, -d, 0.0)
    ag = g[:period].mean()
    al = l[:period].mean()
    alpha = 1.0 / period
    for i in range(period, len(g)):
        ag = ag * (1 - alpha) + g[i] * alpha
        al = al * (1 - alpha) + l[i] * alpha
    if al == 0: return 100.0
    return round(100 - 100 / (1 + ag / al), 2)


def _ema_array(closes, period):
    """Returns the full EMA series (same length as input). Used by T2 for slope check."""
    n = len(closes)
    if n == 0: return np.array([])
    a = 2.0 / (period + 1)
    out = np.empty(n)
    out[0] = closes[0]
    for i in range(1, n):
        out[i] = closes[i] * a + out[i-1] * (1 - a)
    return out


def _swing_high_5m(highs_5m, lookback=8):
    """Return (idx_rel, value) of swing high in last `lookback` bars."""
    if len(highs_5m) < lookback: return None, None
    sub = highs_5m[-lookback:]
    idx_rel = int(np.argmax(sub))
    return idx_rel, float(sub[idx_rel])


class GeometricExpert:

    def __init__(self, broker, memory, geometry, regime):
        self.broker   = broker
        self.memory   = memory
        self.geometry = geometry
        self.regime   = regime
        # Pending limit orders : zone_key → {order_id, symbol, level, stop, target, qty, deploy}
        # Shared between slow loop (writes via evaluate/_reconcile) and fast loop
        # (reads/deletes via manage_pending). RLock pour mutations cross-thread.
        self._pending: dict = {}
        self._pending_lock = threading.RLock()
        # Zone touch tracking
        self._touches: defaultdict = defaultdict(int)
        # Crypto-native regime engine (PAPER only — gated by config.USE_CRYPTO_REGIME_GATE)
        self.crypto_regime = None
        if getattr(config, "USE_CRYPTO_REGIME_GATE", False):
            try:
                from crypto_regime import CryptoRegime
                self.crypto_regime = CryptoRegime(broker)
                logger.info("[CRYPTO_REGIME] gate ENABLED — paper validation mode")
            except Exception as e:
                logger.warning(f"[CRYPTO_REGIME] init failed, falling back to old regime: {e}")
        # T2 trend-follow anti-spam: track last T2 entry timestamp per symbol
        self._last_t2_entry_ts = {}    # symbol -> unix timestamp
        # Réconciliation au démarrage
        self._reconcile_state()

    # ── Réconciliation démarrage ───────────────────────────────────────────────

    def _reconcile_state(self):
        """Au démarrage, recharge les ordres GTC ouverts et les positions orphelines."""
        try:
            # 1. Recharger les ordres limit ouverts dans self._pending
            open_orders = self.broker.list_open_orders()
            for o in open_orders:
                if o.type == "limit" and o.limit_price:
                    lim    = float(o.limit_price)
                    symbol = o.db_symbol
                    side   = getattr(o, "side", "buy")
                    if side == "buy":
                        stop   = round(lim * 0.997, 4)
                        target = round(lim * 1.009, 4)
                    else:
                        stop   = round(lim * 1.003, 4)
                        target = round(lim * 0.991, 4)
                    zk     = self._zone_key(lim) if side == "buy" else -self._zone_key(lim)
                    self._pending_add(zk, {
                        "order_id": o.id,
                        "symbol":   symbol,
                        "level":    lim,
                        "high":     lim,
                        "stop":     stop,
                        "target":   target,
                        "qty":      o.filled_qty or 0,
                        "deploy":   lim * (o.qty_contracts or 0) * self.broker._ct(getattr(o, "kraken_symbol", getattr(o, "symbol", None))),
                        "side":     "long" if side == "buy" else "short",
                    })
                    logger.info(f"[GEO] 🔄 Recovered pending order: {symbol} {side.upper()} GTC@{lim}")

            # 2. Réconcilier les positions OKX sans trade DB correspondant
            positions   = self.broker.get_positions()
            open_db     = {t["symbol"] for t in self.memory.get_open_trades()}
            _now        = datetime.datetime.now(datetime.timezone.utc)
            # Skip si fermé il y a < 10 min (settlement)
            recently_closed = set()
            try:
                import sqlite3 as _sq
                _conn = _sq.connect(self.memory.db_path)
                for _sym, _exit in _conn.execute(
                    "SELECT symbol, exit_at FROM trades WHERE status='closed' AND exit_at IS NOT NULL"
                ).fetchall():
                    try:
                        _et = datetime.datetime.fromisoformat(str(_exit).replace("Z", "+00:00"))
                        if (_now - _et).total_seconds() < 600:
                            recently_closed.add(_sym)
                    except Exception:
                        pass
                _conn.close()
            except Exception:
                pass

            for pos in positions:
                sym = pos.db_symbol
                if sym in recently_closed:
                    continue
                if sym not in open_db and any(s in sym for s in config.GEO_SYMBOLS):
                    entry  = float(pos.avg_entry_price)
                    qty    = float(pos.qty)
                    # Dispatch SL/TP + side per broker pos.side (fix 2026-05-30)
                    # Legacy bug : hardcoded "buy"/"long" → tracked shorts as longs,
                    # SL/TP inversed, PnL sign wrong. cf. broker side mapping family.
                    pos_side = (getattr(pos, "side", None) or "long").lower()
                    if pos_side == "short":
                        center = entry * (1 + config.GEO_ZONE_PCT)
                        stop   = round(center * (1 + config.GEO_ZONE_PCT) * 1.001, 4)
                        target = round(entry * (1 - config.GEO_TARGET_PCT), 4)
                        mem_side = "sell"
                        ctx_side = "short"
                        _reco_target_pct = (entry - target) / entry if entry else None
                    else:
                        center = entry / (1 + config.GEO_ZONE_PCT)
                        stop   = round(center * (1 - config.GEO_ZONE_PCT) * 0.999, 4)
                        target = round(entry * (1 + config.GEO_TARGET_PCT), 4)
                        mem_side = "buy"
                        ctx_side = "long"
                        _reco_target_pct = (target - entry) / entry if entry else None
                    self.memory.log_trade_open(
                        trade_id=str(uuid.uuid4()),
                        symbol=sym, side=mem_side,
                        qty=qty, entry_price=entry,
                        stop_loss=stop, take_profit=target,
                        market_context={
                            "strategy_source": "geo_v4",
                            "side": ctx_side, "level": entry,
                            "stop": stop, "target": target,
                            "reconciled": True,
                            "mode": self._mode_from_target_pct(_reco_target_pct),
                            "broker_mode": self._broker_mode(),
                            "target_pct_used": _reco_target_pct,
                        }
                    )
                    logger.info(f"[GEO] 🔄 Recovered orphan position: {sym} {ctx_side} qty={qty:.4f} entry={entry} SL={stop} TP={target}")
        except Exception as e:
            logger.warning(f"[GEO] _reconcile_state: {e}")

    # ── Capital ───────────────────────────────────────────────────────────────

    def _live_capital(self) -> float:
        try:
            eq = self.broker.get_equity()
            logger.debug(f"[GEO] live capital OKX: ${eq:.2f}")
            return eq
        except Exception as e:
            logger.warning(f"[GEO] _live_capital fallback: {e}")
            return config.GEO_CAPITAL + self._closed_pnl()

    def get_deployed(self) -> float:
        try:
            total = 0.0
            for t in self.memory.get_open_trades():
                ctx = self._ctx(t)
                if ctx.get("strategy_source") == "geo_v4":
                    total += float(t.get("entry_price", 0)) * float(t.get("qty", 0))
            return total
        except Exception:
            return config.GEO_CAPITAL

    def get_available(self) -> float:
        try:
            return max(0.0, self.broker.get_available())
        except Exception as e:
            logger.error(f"[GEO] get_available: {e}")
            return max(0.0, config.GEO_CAPITAL - self.get_deployed())

    def has_capital(self) -> bool:
        return self.get_available() >= 30.0

    def _closed_pnl(self) -> float:
        try:
            import sqlite3
            conn = sqlite3.connect(self.memory.db_path, timeout=5)
            row = conn.execute("""
                SELECT COALESCE(SUM(pnl), 0.0) FROM trades
                WHERE status = 'closed'
                  AND json_extract(market_context, '$.strategy_source') = 'geo_v4'
                  AND close_reason != 'synced_close'
            """).fetchone()
            conn.close()
            return float(row[0]) if row and row[0] else 0.0
        except Exception:
            return 0.0

    def _daily_pnl(self) -> float:
        try:
            import sqlite3
            midnight = datetime.datetime.now(datetime.timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).isoformat()
            conn = sqlite3.connect(self.memory.db_path, timeout=5)
            row = conn.execute("""
                SELECT COALESCE(SUM(pnl), 0.0) FROM trades
                WHERE status = 'closed'
                  AND json_extract(market_context, '$.strategy_source') = 'geo_v4'
                  AND close_reason != 'synced_close'
                  AND exit_at >= ?
            """, (midnight,)).fetchone()
            conn.close()
            return float(row[0]) if row and row[0] else 0.0
        except Exception:
            return 0.0

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _ctx(self, trade: dict) -> dict:
        raw = trade.get("market_context") or {}
        if isinstance(raw, str):
            try: return json.loads(raw)
            except: return {}
        return raw

    # ── Thread-safe accessors for _pending ────────────────────────────────────

    def _pending_add(self, zk, data: dict):
        with self._pending_lock:
            self._pending[zk] = data

    def _pending_del(self, zk):
        with self._pending_lock:
            self._pending.pop(zk, None)

    def _pending_snapshot(self) -> dict:
        with self._pending_lock:
            return dict(self._pending)

    def _pending_has(self, zk) -> bool:
        with self._pending_lock:
            return zk in self._pending

    def _pending_len(self) -> int:
        with self._pending_lock:
            return len(self._pending)

    def _pending_for_symbol(self, symbol: str) -> int:
        with self._pending_lock:
            return sum(1 for p in self._pending.values() if p.get("symbol") == symbol)

    @staticmethod
    def _mode_from_target_pct(target_pct: float) -> str:
        """Mappe target_pct utilisé à l'entrée → label du mode stratégie."""
        if target_pct is None: return "unknown"
        if 0.004 <= target_pct <= 0.006: return "lowvol"
        if 0.008 <= target_pct <= 0.010: return "normal"
        if target_pct >= 0.014:          return "trend"
        return "unknown"

    @staticmethod
    def _broker_mode() -> str:
        return "paper" if getattr(config, "KRAKEN_PAPER", True) else "live"

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

    def _dynamic_stop_short(self, highs_5m, entry_level, wick_high) -> float:
        ceiling   = entry_level * 1.008
        candidate = max(highs_5m[-8:]) * 1.001 if len(highs_5m) >= 8 else wick_high * 1.001
        if entry_level < candidate <= ceiling: return candidate
        zone_stop = wick_high * 1.001
        if entry_level < zone_stop <= ceiling: return zone_stop
        return entry_level * 1.003

    def _get_short_signal_t2(self, symbol, bars_5m, bars_1h, cr_decision):
        """T2 trend-follow short — port from backtest_short_expert.py.

        Activation domain: state == "trend_down_smooth" AND conf >= 75 AND dir <= -0.85.
        Trigger: pullback to EMA20 1h from below, RSI 5m in [40, 65], slope desc,
        BTC trend stable, volume present.

        Returns dict with entry/stop/target/timeout_min/size_cap or None.
        """
        # Anti-spam cooldown
        last = self._last_t2_entry_ts.get(symbol, 0)
        if (time.time() - last) < config.T2_COOLDOWN_MIN * 60:
            return None

        h5 = bars_5m["high"].values
        l5 = bars_5m["low"].values
        c5 = bars_5m["close"].values
        v5 = bars_5m["volume"].values
        c1h = bars_1h["close"].values
        h1h = bars_1h["high"].values

        if len(c5) < 30 or len(c1h) < 25:
            return None

        # 1. EMA20 1h using REAL closes
        ema20_1h_series = _ema_array(c1h, 20)
        ema20_1h = float(ema20_1h_series[-1])

        # 2. Slope EMA20 1h DESCENDING over last 6 bars
        if len(ema20_1h_series) < 6: return None
        if ema20_1h_series[-1] >= ema20_1h_series[-6]: return None

        # 3. Pullback to EMA20 1h: high_5m_last_3 >= ema20_1h * (1 - buffer)
        #    AND current_close < ema20_1h
        recent_high_5m = float(np.max(h5[-3:]))
        current_close = float(c5[-1])
        threshold = ema20_1h * (1 - config.T2_PULLBACK_BUFFER_PCT)
        if recent_high_5m < threshold: return None
        if current_close >= ema20_1h: return None

        # 4. RSI 5m in [40, RSI_MAX]
        cl5 = c5[-30:]
        rsi = _rsi(cl5, 14)
        if not (40 <= rsi <= config.T2_RSI_5M_MAX): return None

        # 5. RSI 5m not persistently overbought
        rsi_prev1 = _rsi(c5[:-1][-30:], 14) if len(c5) > 30 else rsi
        if rsi_prev1 > config.T2_RSI_5M_MAX + 5: return None

        # 6. Volume present
        if len(v5) >= 21:
            avgv = float(np.mean(v5[-21:-1]))
            if avgv > 0 and v5[-1] < avgv * 0.5: return None

        # 7. Swing high 1h for stop placement
        swing_high_1h = float(np.max(h1h[-config.T2_SWING_LOOKBACK_1H:]))

        # 8. Stop placement
        stop_raw = max(swing_high_1h, current_close * 1.008) * (1 + config.T2_STOP_BUFFER_PCT)

        # 9. Target = current * (1 - target_pct from regime, fallback default)
        tp = float((cr_decision or {}).get("target_pct") or 0)
        if tp <= 0: tp = config.T2_TARGET_PCT_DEFAULT
        target = current_close * (1 - tp)

        # 10. R:R check
        risk = stop_raw - current_close
        reward = current_close - target
        if risk <= 0 or reward <= 0 or reward / risk < config.GEO_MIN_RR:
            return None

        return {
            "thesis": "T2",
            "entry": current_close,
            "stop": stop_raw,
            "target": target,
            "ema20_1h": ema20_1h,
            "swing_high_1h": swing_high_1h,
            "timeout_min": config.T2_TIMEOUT_MIN,
            "size_cap": config.T2_INITIAL_CAP,
        }

    def _find_resistance_zones(self, highs, lows, closes, min_tests=1):
        """Swing highs au-dessus du prix actuel — zones de résistance."""
        current = closes[-1]
        sw_highs = []
        for i in range(2, len(highs) - 2):
            if (highs[i] > highs[i-1] and highs[i] > highs[i-2]
                    and highs[i] > highs[i+1] and highs[i] > highs[i+2]):
                sw_highs.append((highs[i], lows[i]))
        if not sw_highs: return []
        sw_highs.sort(key=lambda x: x[0])
        clusters = [[sw_highs[0]]]
        for v in sw_highs[1:]:
            if (v[0] - clusters[-1][0][0]) / clusters[-1][0][0] < config.GEO_ZONE_PCT * 2:
                clusters[-1].append(v)
            else:
                clusters.append([v])
        zones = []
        for c in clusters:
            center    = sum(x[0] for x in c) / len(c)
            wick_high = max(x[0] for x in c)
            if center > current * 1.001 and len(c) >= min_tests:
                zones.append({
                    "center":    center,
                    "low":       center * (1 - config.GEO_ZONE_PCT),
                    "high":      center * (1 + config.GEO_ZONE_PCT),
                    "wick_high": wick_high,
                    "tests":     len(c),
                })
        zones.sort(key=lambda x: x["center"])  # Plus proche en premier
        return zones

    def _rsi_bearish_divergence(self, closes, rsi_now) -> bool:
        if len(closes) < 5: return False
        rsi_prev     = _rsi(np.array(closes[:-3]), 14)
        price_higher = closes[-1] > closes[-4]
        rsi_lower    = rsi_now < rsi_prev
        return price_higher and rsi_lower

    def _log_decision(self, action, symbol, price, stop, target, rr, regime, rsi, zone):
        if not self.memory: return
        try:
            self.memory.log_decision(
                action,
                f"GEO V4: {symbol} {action} @ ${price:.4f} | "
                f"SL=${stop:.4f} TP=${target:.4f} R:R={round(rr,1)}x | "
                f"regime={regime} RSI={rsi:.0f}",
                symbol=symbol,
                confidence=round(min(zone.get("tests", 1) / 5.0, 1.0), 2),
                market_data=json.dumps({
                    "zone_center": zone.get("center"),
                    "zone_tests":  zone.get("tests"),
                    "rsi":         rsi,
                    "rr":          round(rr, 2),
                })
            )
        except Exception as e:
            logger.debug(f"[GEO] log_decision: {e}")

    def _cancel_order(self, symbol: str, order_id: str | None):
        if not order_id: return
        self.broker.cancel_order(symbol, order_id)

    # ── EVALUATE ──────────────────────────────────────────────────────────────

    def evaluate(self, symbol: str = None, regime: str = "unknown"):
        symbol = symbol or config.GEO_SYMBOLS[0]
        logger.info(f"[GEO] evaluating {symbol} | régime={regime}")

        _r = (regime or "unknown").lower()

        # Circuit-breaker journalier
        daily_loss = self._daily_pnl()
        daily_cap  = -abs(config.MONTHLY_LOSS_CAP_PCT * config.GEO_CAPITAL)
        if daily_loss < daily_cap:
            logger.warning(f"[GEO] 🚨 Circuit-breaker: ${daily_loss:.2f} < ${daily_cap:.2f}")
            return

        # Pool global
        open_count_global = len([t for t in self.memory.get_open_trades()
                                  if self._ctx(t).get("strategy_source") == "geo_v4"])
        open_count_global += self._pending_len()
        if open_count_global >= config.GEO_MAX_SIM:
            logger.info(f"[GEO] Pool global plein ({open_count_global}/{config.GEO_MAX_SIM}) — skip {symbol}")
            return

        # Per-symbol cap: max 1 position+pending par symbole (anti-concentration)
        symbol_active = sum(1 for t in self.memory.get_open_trades()
                            if t.get("symbol") == symbol
                            and self._ctx(t).get("strategy_source") == "geo_v4")
        symbol_active += self._pending_for_symbol(symbol)
        if symbol_active >= 1:
            logger.info(f"[GEO] {symbol} déjà actif ({symbol_active}) — skip")
            return

        # Circuit-breaker 3% jour
        try:
            import sqlite3
            conn = sqlite3.connect(self.memory.db_path, timeout=5)
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0.0) FROM trades WHERE status='closed'"
                " AND json_extract(market_context, '$.strategy_source')='geo_v4'"
                " AND close_reason!='synced_close'"
                " AND DATE(exit_at)=DATE('now')"
            ).fetchone()
            conn.close()
            daily_pnl = float(row[0]) if row and row[0] else 0.0
            if daily_pnl < -(config.GEO_CAPITAL * 0.03):
                logger.info(f"[GEO] 🔴 CIRCUIT BREAKER: perte jour ${abs(daily_pnl):.2f} > 3% — pause")
                return
        except Exception as e:
            logger.debug(f"[GEO] circuit-breaker: {e}")

        if not self.has_capital():
            logger.info(f"[GEO] Capital insuffisant (${self.get_available():.0f})")
            return

        # ── Crypto-native regime gate (PAPER validation) ──────────────────────
        cr_decision = None
        cr_allow_long  = True
        cr_allow_short = True
        cr_size_mult   = 1.0          # auto-switch from regime
        cr_target_pct  = None         # None = keep legacy lowvol_target_pct
        # Regime state variables — set to safe defaults; overwritten if crypto_regime active
        state = "neutral"
        conf  = 0.0
        dir_  = 0.0
        t1s_hard_block_override = False
        if self.crypto_regime is not None:
            try:
                cr_decision = self.crypto_regime.evaluate(symbol)
            except Exception as e:
                logger.warning(f"[CRYPTO_REGIME] evaluate({symbol}) failed: {e} — fail-safe block")
                cr_decision = None
            if cr_decision is not None:
                min_conf = getattr(config, "CRYPTO_REGIME_MIN_CONFIDENCE", 50)
                state      = cr_decision.get("state", "neutral")
                conf       = cr_decision.get("confidence", 0)
                dir_       = cr_decision.get("direction", 0)
                eth_signal = cr_decision.get("eth_signal", "weak")
                btc_state  = cr_decision.get("btc_state",  "unknown")
                cr_allow_long  = bool(cr_decision.get("allow_long",  False))
                cr_allow_short = bool(cr_decision.get("allow_short", False))
                cr_size_mult   = float(cr_decision.get("size_multiplier", 0.0))
                cr_target_pct_in = cr_decision.get("target_pct", 0.0)
                if cr_target_pct_in and cr_target_pct_in > 0:
                    cr_target_pct = float(cr_target_pct_in)
                old_regime_label = (regime or "unknown").upper()
                policy_reason = cr_decision.get("policy_reason", "—")
                # T1S hard-block override (cf. config.T1S_INCLUDE_HARD_BLOCK + C2 over-protection finding)
                # Certain states are normally hard-blocked but show 64.4% WR on T1S zone bounces.
                # btc_unknown and vol_extreme remain blocked (safety-critical).
                t1s_hard_block_override = False
                if not cr_allow_short and config.T1S_INCLUDE_HARD_BLOCK and conf >= min_conf:
                    HARD_BLOCK_OVERRIDE_STATES = {
                        "btc_chaos", "vol_extreme",
                        "counter_cyclical_block", "btc_trend_eth_weak",
                        "aligned_long_trend", "btc_led_long",
                    }
                    # btc_unknown and vol_extreme stay blocked even with override
                    ALWAYS_BLOCK_STATES = {"btc_unknown", "vol_extreme"}
                    if state in (HARD_BLOCK_OVERRIDE_STATES - ALWAYS_BLOCK_STATES):
                        t1s_hard_block_override = True
                        cr_allow_short = True   # local override for T1S only
                        logger.info(
                            f"[CRYPTO_REGIME] {symbol} T1S_HARD_BLOCK_OVERRIDE applied "
                            f"state={state} conf={conf:.0f} — T1S short enabled despite hard block"
                        )

                # Hard block: confidence too low OR policy disallows everything.
                # BYPASS: if ROUTER_VARIANT="t1_neutral" and state=="neutral", T1S short is
                # explicitly allowed to fire (C2 diagnostic showed +0.4% avg expectancy 64%
                # WR on T1S in neutral). The router downstream will then decide.
                block_all = not (cr_allow_long or cr_allow_short)
                router_neutral_bypass = (
                    config.ROUTER_VARIANT == "t1_neutral" and state == "neutral"
                )
                if (block_all or conf < min_conf) and not router_neutral_bypass and not t1s_hard_block_override:
                    logger.info(
                        f"[CRYPTO_REGIME] {symbol} BLOCKED "
                        f"old={old_regime_label} new={state} eth={eth_signal} btc={btc_state} "
                        f"conf={conf:.0f} dir={dir_:+.2f} vol={cr_decision.get('vol_state')} "
                        f"long={cr_allow_long} short={cr_allow_short} "
                        f"reason=\"{policy_reason}\""
                    )
                    return
                # If we get here via router_neutral_bypass, force-enable cr_allow_short
                # AND ensure cr_size_mult is non-zero (default size). Otherwise deploy=0
                # and no trade can size. Use 0.5 — moderate size for unconfirmed regime.
                if router_neutral_bypass and not cr_allow_short:
                    cr_allow_short = True
                    if cr_size_mult <= 0:
                        cr_size_mult = 0.5    # default conservative size for neutral bypass
                    logger.info(
                        f"[CRYPTO_REGIME] {symbol} ROUTER_NEUTRAL_BYPASS applied "
                        f"state=neutral conf={conf:.0f} dir={dir_:+.2f} "
                        f"size_mult={cr_size_mult:.2f} — T1S short enabled"
                    )
                # Same fix for T1S hard-block override (size_mult might be 0 in some
                # hard-block states like btc_chaos)
                if t1s_hard_block_override and cr_size_mult <= 0:
                    cr_size_mult = 0.5
                    logger.info(
                        f"[CRYPTO_REGIME] {symbol} T1S hard-block size_mult defaulted to 0.5"
                    )
                logger.info(
                    f"[CRYPTO_REGIME] {symbol} ALLOWED "
                    f"old={old_regime_label} new={state} eth={eth_signal} btc={btc_state} "
                    f"conf={conf:.0f} dir={dir_:+.2f} long={cr_allow_long} short={cr_allow_short} "
                    f"size×{cr_size_mult:.2f} tp={cr_target_pct or 'inherit'} "
                    f"reason=\"{policy_reason}\""
                )
            else:
                # Evaluator failed → fail-safe: block trading
                logger.info(f"[CRYPTO_REGIME] {symbol} BLOCKED — evaluator returned None (fail-safe)")
                return

        # ── Données communes (fetch une seule fois) ───────────────────────────
        bars_1h = self.broker.get_bars(symbol, "1Hour", limit=50)
        if bars_1h is None or bars_1h.empty or len(bars_1h) < 10:
            return
        bars_15m = self.broker.get_bars(symbol, "15Min", limit=100)
        if bars_15m is None or bars_15m.empty or len(bars_15m) < 20:
            return
        bars_5m = self.broker.get_bars(symbol, "5Min", limit=30)
        if bars_5m is None or bars_5m.empty or len(bars_5m) < 15:
            return

        h1h        = bars_1h["high"].values
        l1h        = bars_1h["low"].values
        c1h        = bars_1h["close"].values   # real 1h closes (used by T2 EMA20)
        highs_15m  = bars_15m["high"].values
        lows_15m   = bars_15m["low"].values
        closes_15m = bars_15m["close"].values
        closes_5m  = bars_5m["close"].values
        lows_5m    = bars_5m["low"].values
        highs_5m   = bars_5m["high"].values
        current    = float(closes_15m[-1])
        rsi_now    = _rsi(closes_5m, 14)
        ema5       = float(pd.Series(closes_5m).ewm(span=5,  adjust=False).mean().iloc[-1])
        ema10      = float(pd.Series(closes_5m).ewm(span=10, adjust=False).mean().iloc[-1])

        support_zones    = self._find_zones(highs_15m, lows_15m, closes_15m, min_tests=1)
        resistance_zones = self._find_resistance_zones(highs_15m, lows_15m, closes_15m, min_tests=1)

        # ── Backtest/live parity for Phase 4 paper validation ────────────────────
        # The C3 grid was validated WITHOUT low_vol mode (used GEO_TARGET_PCT=0.009
        # fixed, no dynamic adaptation). To match the validated strategy exactly,
        # force low_vol_mode=False for T1 zone bounce. T2 has its own params and
        # is unaffected. Set GEO_DISABLE_LOWVOL=0 in env to restore legacy behavior.
        force_disable_lowvol = (os.getenv("GEO_DISABLE_LOWVOL", "1") == "1")
        if force_disable_lowvol:
            low_vol_mode = False
            lowvol_target_pct = config.GEO_TARGET_PCT     # 0.009 — the backtested value
        else:
            low_vol_mode = (_r in ("choppy", "bull"))
            lowvol_target_pct = getattr(config, "GEO_LOWVOL_TARGET_PCT", config.GEO_TARGET_PCT)
        # Auto-switch : the crypto regime engine emits a target_pct per state
        # (lowvol 0.005 → range_quiet, normal 0.009 → calm signals, trend 0.015 → aligned trends).
        # When provided, override default so the strategy adapts to regime engine.
        # The crypto_regime target_pct override applies even with lowvol forced off
        # (because it's an explicit regime signal, not the legacy lowvol mode).
        if cr_target_pct is not None:
            lowvol_target_pct = cr_target_pct
        zone_gap = 0.0

        open_count = open_count_global

        # ── LONGS : bounce sur support ────────────────────────────────────────
        # Pas d'entrée long si downtrend 1h confirmé (LH + LL)
        lh = h1h[-1] < h1h[-4]; ll = l1h[-1] < l1h[-4]
        if not (lh and ll) and _r not in ("bear", "panic") and cr_allow_long:
            n_zones = len(support_zones)
            n_dist = n_touches = n_pending = n_rsi = n_div = n_pass3b = n_risk = n_rr = 0

            for zone in support_zones:
                if open_count >= config.GEO_MAX_SIM:
                    break
                zk = self._zone_key(zone["center"])

                dist_pct = (current - zone["high"]) / zone["high"]
                dist_low  = -0.015 if low_vol_mode else -0.015
                dist_high =  0.015 if low_vol_mode else  0.015
                if not (dist_low <= dist_pct <= dist_high):
                    n_dist += 1; continue
                if self._touches[zk] >= config.GEO_MAX_TOUCHES:
                    n_touches += 1; continue
                if self._pending_has(zk):
                    n_pending += 1; continue
                if not (config.GEO_RSI_LOW <= rsi_now <= config.GEO_RSI_HIGH):
                    n_rsi += 1; continue
                if not config.DEBUG_MODE:
                    if not self._rsi_divergence(closes_5m, rsi_now):
                        if not low_vol_mode:
                            n_div += 1; continue

                # Pass 3b exact: last 8 candles touched the entry zone,
                # and the current 5m close is still above the zone low.
                pass_3b = bool(np.any(lows_5m[-8:] <= zone["high"] + zone_gap) and closes_5m[-1] > zone["low"])
                if not config.DEBUG_MODE:
                    if not pass_3b:
                        n_pass3b += 1; continue

                stop = self._dynamic_stop(lows_5m, zone["center"], zone["wick_low"])

                # TP lower in low-vol mode.
                target = _smart_round(zone["high"] * (1 + lowvol_target_pct))

                # Min R:R depuis fill probable (current). Le broker paper market-fill
                # au current → si reward/risk depuis fill < seuil, c'est un trade
                # asymétrique défavorable, skip.
                actual_risk   = current - stop
                actual_reward = target - current
                min_rr        = getattr(config, "GEO_MIN_RR", 1.2)
                if actual_risk <= 0 or actual_reward <= 0 or actual_reward / actual_risk < min_rr:
                    n_rr += 1; continue

                risk   = abs(zone["high"] - stop)
                reward = abs(target - zone["high"])
                if risk <= 0:
                    n_risk += 1; continue

                available = self.get_available()
                if available < 30: break
                deploy      = min(available * 0.995, self._live_capital() * config.GEO_POS_PCT * cr_size_mult)
                limit_price = _smart_round(zone["high"])
                if deploy / limit_price < 0.001: continue

                order_id = self.broker.place_limit_buy(
                    symbol=symbol, price=limit_price,
                    stop_loss=stop, take_profit=target, deploy_usdt=deploy,
                )
                if not order_id:
                    continue

                self._touches[zk] += 1
                # Distance gate parity instrumentation (T1L only):
                # Backtest accepted dist_pct ∈ [-0.012, +0.002] from zone.center
                # Live accepts dist_pct ∈ [-0.015, +0.015] from zone.high
                # Translation: backtest's +0.002 from center ≈ -0.001 from high (with ZONE_PCT=0.003).
                # If live's dist_pct > -0.001, this trade would NOT have fired in backtest
                # → mark as "permissive" trigger to track divergence stats.
                distance_gate_trigger_permissive = bool(dist_pct > -0.001)
                self._pending_add(zk, {
                    "order_id": order_id, "symbol": symbol,
                    "level": zone["center"], "high": zone["high"],
                    "stop": stop, "target": target, "deploy": deploy,
                    "side": "long",
                    "thesis": "T1",
                    "mode": self._mode_from_target_pct(lowvol_target_pct),
                    "broker_mode": self._broker_mode(),
                    "target_pct_used": float(lowvol_target_pct),
                    "size_mult_used":  float(cr_size_mult),
                    "dist_pct_at_entry":           round(float(dist_pct), 5),
                    "distance_gate_trigger_permissive": distance_gate_trigger_permissive,
                    "crypto_regime":     (cr_decision or {}).get("state"),
                    "crypto_confidence": (cr_decision or {}).get("confidence"),
                    "crypto_direction":  (cr_decision or {}).get("direction"),
                    "crypto_vol_state":  (cr_decision or {}).get("vol_state"),
                    "crypto_eth_signal": (cr_decision or {}).get("eth_signal"),
                    "crypto_btc_state":  (cr_decision or {}).get("btc_state"),
                })
                logger.info(
                    f"[GEO] 📋 LONG {symbol} @ ${limit_price:.4f} "
                    f"SL=${_smart_round(stop):.4f} TP=${_smart_round(target):.4f} "
                    f"R:R={round(reward/risk,1)}x RSI={rsi_now:.0f} tests={zone['tests']} "
                    f"lowvol={low_vol_mode} dist={dist_pct*100:+.2f}% "
                    f"gate_permissive={distance_gate_trigger_permissive}"
                )
                self._log_decision("BUY", symbol, limit_price, stop, target,
                                   reward/risk, _r, rsi_now, zone)
                if _NOTIFY:
                    notify.trade_opened(symbol, "long", deploy/limit_price,
                                        limit_price, stop, target, reward/risk)
                open_count += 1
                break  # per-symbol cap : max 1 entrée par evaluate()

            if open_count == open_count_global:
                parts = []
                for label, val in [("dist",n_dist),("touches",n_touches),
                                   ("pending",n_pending),("rsi",n_rsi),
                                   ("div",n_div),("pass3b",n_pass3b),
                                   ("risk",n_risk),("rr",n_rr)]:
                    if val: parts.append(f"{label}:{val}")
                logger.info(
                    f"[GEO] {symbol} — no long | zones={n_zones} "
                    f"price={current:.4f} | {', '.join(parts) or 'no_zones_in_range'} lowvol={low_vol_mode}"
                )

        # ── SHORTS : multi-thesis router ──────────────────────────────────────
        # Validated config: ROUTER_VARIANT="t1_trend_fallback", T1S_INCLUDE_HARD_BLOCK=True
        # Pas d'entrée short si uptrend 1h fort (HH + HL) — SAUF T2 (trend-follow) et T1S hard-block override
        lh_up = h1h[-1] > h1h[-4]; ll_up = l1h[-1] > l1h[-4]

        # ── SHORT ROUTER (multi-thesis per ROUTER_VARIANT) ─────────────────────
        # Branches:
        #   trend_down_smooth + conf>=75 + dir<=-0.85: T2 priority, T1 fallback if T2 misses
        #   T1-eligible states (incl hard-block override): T1 zone bounce
        #   neutral with ROUTER_VARIANT="t1_neutral": T1 zone bounce

        t2_signal = None
        t2_fired = False
        tp_used = None  # track which target_pct was used for logging

        if (config.ENABLE_T2_SHORT and cr_decision is not None
                and state == "trend_down_smooth"
                and conf >= 75 and dir_ <= -0.85
                and cr_allow_short
                and open_count < config.GEO_MAX_SIM):
            t2_signal = self._get_short_signal_t2(symbol, bars_5m, bars_1h, cr_decision)
            if t2_signal:
                tp_used = float((cr_decision or {}).get("target_pct") or 0)
                if tp_used <= 0: tp_used = config.T2_TARGET_PCT_DEFAULT
                deploy_pct = config.GEO_POS_PCT * float(cr_size_mult or 1.0) * t2_signal["size_cap"]
                deploy = min(self.get_available() * 0.995, self._live_capital() * deploy_pct)
                if deploy / t2_signal["entry"] >= 0.001:
                    qty = deploy / t2_signal["entry"]
                    # Use place_limit_sell with current price as market-fill (paper accepts)
                    order_id = self.broker.place_limit_sell(
                        symbol=symbol, price=t2_signal["entry"],
                        stop_loss=t2_signal["stop"], take_profit=t2_signal["target"],
                        deploy_usdt=deploy,
                    )
                    if order_id:
                        # Track T2 entry for anti-spam cooldown
                        self._last_t2_entry_ts[symbol] = time.time()
                        # Use T2-specific pending key (different from T1 zone keys)
                        zk_t2 = ("T2_short", symbol, int(time.time()))
                        self._pending_add(zk_t2, {
                            "order_id": order_id, "symbol": symbol,
                            "level": t2_signal["entry"], "high": t2_signal["entry"],
                            "stop": t2_signal["stop"], "target": t2_signal["target"],
                            "deploy": deploy,
                            "side": "short", "thesis": "T2",
                            "timeout_min": t2_signal["timeout_min"],
                            "mode": "trend_follow",
                            "broker_mode": self._broker_mode(),
                            "target_pct_used": float(tp_used),
                            "size_mult_used": float(cr_size_mult or 1.0),
                            "crypto_regime":     (cr_decision or {}).get("state"),
                            "crypto_confidence": (cr_decision or {}).get("confidence"),
                            "crypto_direction":  (cr_decision or {}).get("direction"),
                            "crypto_vol_state":  (cr_decision or {}).get("vol_state"),
                            "crypto_eth_signal": (cr_decision or {}).get("eth_signal"),
                            "crypto_btc_state":  (cr_decision or {}).get("btc_state"),
                        })
                        t2_fired = True
                        logger.info(
                            f"[GEO] SHORT_T2 {symbol} @ ${t2_signal['entry']:.4f} "
                            f"SL=${t2_signal['stop']:.4f} TP=${t2_signal['target']:.4f} "
                            f"timeout={t2_signal['timeout_min']}min size_cap={t2_signal['size_cap']}"
                        )
                        open_count += 1

        # T1 zone bounce short: fires as fallback in trend_down_smooth (per ROUTER_VARIANT),
        # OR in T1-eligible states, OR if T1S_INCLUDE_HARD_BLOCK overrides.
        if not t2_fired and open_count < config.GEO_MAX_SIM:
            # Eligibility check for T1 short:
            #   (1) Regime allows short (covers normal states + T1S hard-block override)
            #   (2) ROUTER_VARIANT == "t1_neutral" AND state == "neutral"
            #   (3) ROUTER_VARIANT == "t1_trend_fallback" AND state == "trend_down_smooth" (T2 missed)
            t1_eligible = False
            if cr_allow_short:    # covers cases 1 + T1S hard-block override (cr_allow_short was set True above)
                t1_eligible = True
            if config.ROUTER_VARIANT == "t1_neutral" and state == "neutral":
                t1_eligible = True
            if config.ROUTER_VARIANT == "t1_trend_fallback" and state == "trend_down_smooth":
                t1_eligible = True

            # T1 zone bounce short also respects the standard uptrend filter
            # (skip T1 if strong 1h uptrend AND no hard-block override active)
            if t1_eligible and (lh_up and ll_up) and not t1s_hard_block_override and state != "trend_down_smooth":
                t1_eligible = False

            if t1_eligible:
                # ── T1 ZONE BOUNCE SHORT (existing logic) ──────────────────────
                n_res = len(resistance_zones)
                n_dist_s = n_rsi_s = n_div_s = n_pass3b_s = n_risk_s = n_rr_s = 0

                for zone in resistance_zones:
                    if open_count >= config.GEO_MAX_SIM:
                        break
                    zk_s = -self._zone_key(zone["center"])  # clé négative pour shorts

                    dist_pct = (zone["low"] - current) / zone["low"]
                    dist_low_s  = -0.004 if low_vol_mode else -0.004
                    dist_high_s = 0.015 if low_vol_mode else 0.015
                    if not (dist_low_s <= dist_pct <= dist_high_s):
                        n_dist_s += 1; continue
                    if self._touches[zk_s] >= config.GEO_MAX_TOUCHES:
                        continue
                    if self._pending_has(zk_s):
                        continue
                    if not (config.GEO_RSI_SHORT_LOW <= rsi_now <= config.GEO_RSI_SHORT_HIGH):
                        n_rsi_s += 1; continue
                    # T1S divergence gate — modulé par T1S_DIV_MODE pour
                    # backtest/live parity (cf. phase4-divergence-validation 2026-05-29).
                    if config.T1S_DIV_MODE == "never":
                        pass  # skip divergence check entirely
                    elif config.T1S_DIV_MODE == "strict":
                        if not self._rsi_bearish_divergence(closes_5m, rsi_now):
                            n_div_s += 1; continue
                    else:  # "rsi_fallback" (intermediate palier — C3 sweet spot)
                        if not self._rsi_bearish_divergence(closes_5m, rsi_now):
                            if not (45 <= rsi_now <= 70):
                                n_div_s += 1; continue

                    # Pass 3b symmetry: recent highs test resistance, and price
                    # stays below the zone high on the current close.
                    pass_3b = bool(np.any(highs_5m[-8:] >= zone["low"] - zone_gap) and closes_5m[-1] < zone["high"])
                    if not pass_3b:
                        n_pass3b_s += 1; continue

                    stop_s = self._dynamic_stop_short(
                        bars_5m["high"].values, zone["low"], zone["wick_high"]
                    )

                    # TP lower in low-vol mode.
                    target_s = _smart_round(zone["low"] * (1 - lowvol_target_pct))

                    # Min R:R depuis fill probable (symétrie longs).
                    actual_risk_s   = stop_s - current
                    actual_reward_s = current - target_s
                    min_rr          = getattr(config, "GEO_MIN_RR", 1.2)
                    if actual_risk_s <= 0 or actual_reward_s <= 0 or actual_reward_s / actual_risk_s < min_rr:
                        n_rr_s += 1; continue

                    risk_s   = abs(stop_s - zone["low"])
                    reward_s = abs(zone["low"] - target_s)
                    if risk_s <= 0:
                        n_risk_s += 1; continue

                    available = self.get_available()
                    if available < 30: break
                    deploy      = min(available * 0.995, self._live_capital() * config.GEO_POS_PCT * cr_size_mult)
                    limit_price = _smart_round(zone["low"])
                    if deploy / limit_price < 0.001: continue

                    order_id = self.broker.place_limit_sell(
                        symbol=symbol, price=limit_price,
                        stop_loss=stop_s, take_profit=target_s, deploy_usdt=deploy,
                    )
                    if not order_id:
                        continue

                    self._touches[zk_s] += 1
                    self._pending_add(zk_s, {
                        "order_id": order_id, "symbol": symbol,
                        "level": zone["center"], "high": zone["high"],
                        "stop": stop_s, "target": target_s, "deploy": deploy,
                        "side": "short", "thesis": "T1",
                        "mode": self._mode_from_target_pct(lowvol_target_pct),
                        "broker_mode": self._broker_mode(),
                        "target_pct_used": float(lowvol_target_pct),
                        "size_mult_used":  float(cr_size_mult),
                        "crypto_regime":     (cr_decision or {}).get("state"),
                        "crypto_confidence": (cr_decision or {}).get("confidence"),
                        "crypto_direction":  (cr_decision or {}).get("direction"),
                        "crypto_vol_state":  (cr_decision or {}).get("vol_state"),
                        "crypto_eth_signal": (cr_decision or {}).get("eth_signal"),
                        "crypto_btc_state":  (cr_decision or {}).get("btc_state"),
                    })
                    logger.info(
                        f"[GEO] SHORT_T1 {symbol} @ ${limit_price:.4f} "
                        f"SL=${stop_s:.4f} TP=${target_s:.4f} "
                        f"R:R={round(reward_s/risk_s,1)}x RSI={rsi_now:.0f} tests={zone['tests']} lowvol={low_vol_mode}"
                    )
                    self._log_decision("SELL", symbol, limit_price, stop_s, target_s,
                                       reward_s/risk_s, _r, rsi_now, zone)
                    if _NOTIFY:
                        notify.trade_opened(symbol, "short", deploy/limit_price,
                                            limit_price, stop_s, target_s, reward_s/risk_s)
                    open_count += 1
                    break  # per-symbol cap : max 1 entrée par evaluate()

                if open_count == open_count_global:
                    parts = []
                    for label, val in [("dist",n_dist_s),("rsi",n_rsi_s),
                                       ("div",n_div_s),("pass3b",n_pass3b_s),
                                       ("risk",n_risk_s),("rr",n_rr_s)]:
                        if val: parts.append(f"{label}:{val}")
                    logger.info(
                        f"[GEO] {symbol} — no short | res_zones={n_res} "
                        f"price={current:.4f} | {', '.join(parts) or 'no_zones_in_range'} lowvol={low_vol_mode}"
                    )

    # ── MANAGE PENDING ────────────────────────────────────────────────────────

    def manage_pending_orders(self):
        """Vérifie les ordres GTC en attente : fill → log trade.
        SL + TP sont déjà attachés à l'ordre OKX → aucun ordre supplémentaire à placer.
        Itère sur un snapshot pour éviter mutation concurrente avec evaluate()."""
        for zk, p in self._pending_snapshot().items():
            try:
                order = self.broker.get_order(p["symbol"], p["order_id"])
                if order is None:
                    continue

                state = order.status   # "live", "filled", "cancelled", "partially_filled"

                if state == "filled":
                    fill  = float(order.filled_avg_price or p.get("high", p["level"]))
                    qty   = float(order.filled_qty or 0)
                    symbol= p["symbol"]
                    entry_side = p.get("side", "long")
                    mem_side   = "buy" if entry_side == "long" else "sell"
                    ctx_side   = "long" if entry_side == "long" else "short"
                    logger.info(f"[GEO] ✅ FILLED: {symbol} {entry_side.upper()} @ ${fill:.4f} qty={qty:.4f}")
                    if self.memory:
                        self.memory.log_trade_open(
                            trade_id=str(uuid.uuid4()),
                            symbol=symbol, side=mem_side,
                            qty=qty, entry_price=fill,
                            stop_loss=p["stop"], take_profit=p["target"],
                            alpaca_order_id=p["order_id"],
                            market_context={
                                "strategy_source": "geo_v4",
                                "side":   ctx_side,
                                "level":  float(p["level"]),
                                "stop":   float(p["stop"]),
                                "target": float(p["target"]),
                                "broker": config.ACTIVE_BROKER,
                                "mode":   p.get("mode", "unknown"),
                                "broker_mode": p.get("broker_mode", self._broker_mode()),
                                "target_pct_used":   p.get("target_pct_used"),
                                "size_mult_used":    p.get("size_mult_used"),
                                "thesis":            p.get("thesis"),
                                "timeout_min":       p.get("timeout_min"),
                                # Distance gate instrumentation (T1L only — see evaluate()):
                                "dist_pct_at_entry": p.get("dist_pct_at_entry"),
                                "distance_gate_trigger_permissive": p.get("distance_gate_trigger_permissive"),
                                "crypto_regime":     p.get("crypto_regime"),
                                "crypto_confidence": p.get("crypto_confidence"),
                                "crypto_direction":  p.get("crypto_direction"),
                                "crypto_vol_state":  p.get("crypto_vol_state"),
                                "crypto_eth_signal": p.get("crypto_eth_signal"),
                                "crypto_btc_state":  p.get("crypto_btc_state"),
                            }
                        )
                    self._pending_del(zk)

                elif state in ("cancelled", "expired", "rejected"):
                    logger.info(f"[GEO] 🗑 Order {state}: {p['symbol']}")
                    self._pending_del(zk)

                else:  # "live" ou "partially_filled"
                    # Vérifier si le niveau est cassé → annuler
                    bars = self.broker.get_bars(p["symbol"], "1Min", limit=3)
                    if bars is not None and not bars.empty:
                        curr = float(bars["close"].iloc[-1])
                        p_side = p.get("side", "long")
                        broken = (p_side == "long"  and curr < p["level"] * 0.997) or \
                                 (p_side == "short" and curr > p["level"] * 1.003)
                        if broken:
                            logger.info(f"[GEO] 🚫 Niveau cassé {p['symbol']} ({p_side}) — annulation")
                            self._cancel_order(p["symbol"], p["order_id"])
                            self._pending_del(zk)

            except Exception as e:
                logger.debug(f"[GEO] manage_pending {p.get('symbol')}: {e}")

    # ── MANAGE POSITIONS ──────────────────────────────────────────────────────

    def manage_open_positions(self):
        """
        Gère les positions ouvertes :
        1. Bot-managed SL/TP si NATIVE_BRACKETS=False (paper trading)
        2. Détecte les fermetures broker-side (SL/TP natifs)
        3. Time-stop 4h
        Supporte long et short.
        """
        try:
            broker_positions = {pos.db_symbol: pos for pos in (self.broker.get_positions() or [])}
            open_trades      = self.memory.get_open_trades()
            now_utc          = datetime.datetime.now(datetime.timezone.utc)
            native_brackets  = getattr(self.broker, "NATIVE_BRACKETS", True)
            current_regime   = self.regime.get_cache().get("regime") if hasattr(self.regime, "get_cache") else None
            # Backtest/live parity: force low_vol_mode off (same as evaluate())
            # so timeout matches the validated GEO_TARGET_PCT=0.009 + TIMEOUT_MIN=120 config.
            if os.getenv("GEO_DISABLE_LOWVOL", "1") == "1":
                low_vol_mode = False
            else:
                low_vol_mode = current_regime in ("choppy", "bull")

            for t in open_trades:
                ctx = self._ctx(t)
                if ctx.get("strategy_source") != "geo_v4":
                    continue

                symbol      = t.get("symbol")
                trade_id    = t.get("trade_id")
                entry       = float(t.get("entry_price", 0))
                qty_t       = float(t.get("qty", 0))
                stop_db     = float(t.get("stop_loss") or 0)
                tp_db       = float(t.get("take_profit") or 0)
                trade_side  = ctx.get("side", "long")   # "long" | "short"
                mult        = 1.0 if trade_side == "long" else -1.0

                entry_at_str = t.get("entry_at", "")
                try:
                    entry_dt    = datetime.datetime.fromisoformat(
                        str(entry_at_str).replace("Z", "+00:00"))
                    entry_ts_ms = int(entry_dt.timestamp() * 1000)
                except Exception:
                    entry_dt    = now_utc
                    entry_ts_ms = 0

                # ── 1. Bot-managed SL/TP (paper trading) ──────────────────────
                if not native_brackets and symbol in broker_positions:
                    pos        = broker_positions[symbol]
                    current_px = float(pos.current_price)

                    sl_hit = ((trade_side == "long"  and stop_db > 0 and current_px <= stop_db) or
                              (trade_side == "short" and stop_db > 0 and current_px >= stop_db))
                    tp_hit = ((trade_side == "long"  and tp_db   > 0 and current_px >= tp_db)   or
                              (trade_side == "short" and tp_db   > 0 and current_px <= tp_db))

                    if sl_hit or tp_hit:
                        reason = "stop" if sl_hit else "target"
                        self.broker.close_position(symbol)
                        pnl   = mult * (current_px - entry) * qty_t
                        emoji = "🔴" if sl_hit else "💰"
                        logger.info(
                            f"[GEO] {emoji} BOT-EXIT [{reason}] {trade_side}: "
                            f"{symbol} @ ${current_px:.4f} pnl=${pnl:.2f}"
                        )
                        self.memory.log_trade_close(trade_id, current_px, reason, pnl=pnl)
                        if _NOTIFY:
                            notify.trade_closed(symbol, trade_side, entry,
                                                current_px, pnl, reason)
                        continue

                # ── 2. Position absente du broker (SL/TP natifs touchés) ───────
                if symbol not in broker_positions:
                    close_info = self.broker.get_close_info(symbol, since_ts_ms=entry_ts_ms)
                    if close_info:
                        fill_price = close_info["price"]
                        fill_qty   = close_info.get("qty", qty_t)
                        pnl        = mult * (fill_price - entry) * fill_qty
                        reason     = close_info.get("reason")
                        if reason is None:
                            if trade_side == "long":
                                reason = "stop" if fill_price <= stop_db * 1.01 else "target"
                            else:
                                reason = "stop" if fill_price >= stop_db * 0.99 else "target"
                        emoji = "🔴" if reason == "stop" else "💰"
                        logger.info(
                            f"[GEO] {emoji} EXIT [{reason}] {trade_side}: "
                            f"{symbol} @ ${fill_price:.4f} pnl=${pnl:.2f}"
                        )
                        self.memory.log_trade_close(trade_id, fill_price, reason, pnl=pnl)
                    else:
                        live = self.broker.get_live_price(symbol) or entry
                        pnl  = mult * (live - entry) * qty_t
                        reason = "stop" if pnl < 0 else "target"
                        logger.warning(
                            f"[GEO] {symbol} absent broker, fermeture approx "
                            f"@ ${live:.4f} pnl=${pnl:.2f}"
                        )
                        self.memory.log_trade_close(trade_id, live, reason, pnl=pnl)
                    continue

                # ── 3. Time-stop : per-thesis if set, else default 2h / low-vol 1h ──
                try:
                    # T2 trades store timeout_min in market_context; use it if present
                    ctx_timeout = ctx.get("timeout_min")
                    if ctx_timeout and int(ctx_timeout) > 0:
                        timeout_min = int(ctx_timeout)
                    else:
                        timeout_min = getattr(config, "GEO_LOWVOL_TIMEOUT_MIN", TIMEOUT_MIN) if low_vol_mode else TIMEOUT_MIN
                    elapsed_min = (now_utc - entry_dt).total_seconds() / 60
                    if elapsed_min >= timeout_min:
                        pos     = broker_positions[symbol]
                        curr_px = float(pos.current_price)
                        logger.info(f"[GEO] ⏰ TIME-STOP {trade_side} {symbol} après {elapsed_min:.0f}min (lowvol={low_vol_mode})")
                        if self.broker.close_position(symbol):
                            pnl = mult * (curr_px - entry) * qty_t
                            self.memory.log_trade_close(trade_id, curr_px, "timeout", pnl=pnl)
                except Exception as e:
                    logger.error(f"[GEO] time-stop {symbol}: {e}")

        except Exception as e:
            logger.error(f"[GEO] manage_open_positions: {e}")


