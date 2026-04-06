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
import logging, json, uuid, datetime
from collections import defaultdict
import numpy as np
import pandas as pd
import config

logger = logging.getLogger(__name__)

TIMEOUT_MIN = 240  # 4h time-stop


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


class GeometricExpert:

    def __init__(self, broker, memory, geometry, regime):
        self.broker   = broker
        self.memory   = memory
        self.geometry = geometry
        self.regime   = regime
        # Pending limit orders : zone_key → {order_id, symbol, level, stop, target, qty, deploy}
        self._pending: dict = {}
        # Zone touch tracking
        self._touches: defaultdict = defaultdict(int)
        # Réconciliation au démarrage
        self._reconcile_state()

    # ── Réconciliation démarrage ───────────────────────────────────────────────

    def _reconcile_state(self):
        """Au démarrage, recharge les ordres GTC ouverts et les positions orphelines."""
        try:
            # 1. Recharger les ordres limit buy ouverts dans self._pending
            open_orders = self.broker.list_open_orders()
            for o in open_orders:
                if o.side == "buy" and o.type == "limit" and o.limit_price:
                    lim    = float(o.limit_price)
                    symbol = o.db_symbol
                    stop   = round(lim * 0.997, 4)
                    target = round(lim * 1.009, 4)
                    zk     = self._zone_key(lim)
                    self._pending[zk] = {
                        "order_id": o.id,
                        "symbol":   symbol,
                        "level":    lim,
                        "high":     lim,
                        "stop":     stop,
                        "target":   target,
                        "qty":      o.filled_qty or 0,
                        "deploy":   lim * (o.qty_contracts or 0) * self.broker._ct(o.okx_symbol),
                    }
                    logger.info(f"[GEO] 🔄 Recovered pending order: {symbol} GTC@{lim}")

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
                    center = entry / (1 + config.GEO_ZONE_PCT)
                    stop   = round(center * (1 - config.GEO_ZONE_PCT) * 0.999, 4)
                    target = round(entry * (1 + config.GEO_TARGET_PCT), 4)
                    self.memory.log_trade_open(
                        trade_id=str(uuid.uuid4()),
                        symbol=sym, side="buy",
                        qty=qty, entry_price=entry,
                        stop_loss=stop, take_profit=target,
                        market_context={
                            "strategy_source": "geo_v4",
                            "side": "long", "level": entry,
                            "stop": stop, "target": target,
                            "reconciled": True,
                        }
                    )
                    logger.info(f"[GEO] 🔄 Recovered orphan position: {sym} qty={qty:.4f} entry={entry}")
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

    def _cancel_order(self, symbol: str, order_id: str | None):
        if not order_id: return
        self.broker.cancel_order(symbol, order_id)

    # ── EVALUATE ──────────────────────────────────────────────────────────────

    def evaluate(self, symbol: str = None, regime: str = "unknown"):
        symbol = symbol or config.GEO_SYMBOLS[0]
        logger.info(f"[GEO] evaluating {symbol} | régime={regime}")

        _r = (regime or "unknown").lower()
        if _r in ("bear", "panic"):
            logger.info(f"[GEO] 🔴 Régime {_r} — pas d'entrée")
            return

        # Circuit-breaker journalier
        daily_loss = self._daily_pnl()
        daily_cap  = -abs(config.MONTHLY_LOSS_CAP_PCT * config.GEO_CAPITAL)
        if daily_loss < daily_cap:
            logger.warning(f"[GEO] 🚨 Circuit-breaker: ${daily_loss:.2f} < ${daily_cap:.2f}")
            return

        # Pool global
        open_count_global = len([t for t in self.memory.get_open_trades()
                                  if self._ctx(t).get("strategy_source") == "geo_v4"])
        open_count_global += len(self._pending)
        if open_count_global >= config.GEO_MAX_SIM:
            logger.info(f"[GEO] Pool global plein ({open_count_global}/{config.GEO_MAX_SIM}) — skip {symbol}")
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

        # ── Pass 1 : Bias 1h ──────────────────────────────────────────────────
        bars_1h = self.broker.get_bars(symbol, "1Hour", limit=50)
        if bars_1h is None or bars_1h.empty or len(bars_1h) < 10:
            return
        h1h = bars_1h["high"].values; l1h = bars_1h["low"].values
        lh  = h1h[-1] < h1h[-4];     ll  = l1h[-1] < l1h[-4]
        if lh and ll:
            logger.info(f"[GEO] {symbol} — downtrend 1h, skip")
            return

        # ── Pass 2 : Zones 15min ──────────────────────────────────────────────
        bars_15m = self.broker.get_bars(symbol, "15Min", limit=100)
        if bars_15m is None or bars_15m.empty or len(bars_15m) < 20:
            return
        current = float(bars_15m["close"].iloc[-1])
        zones   = self._find_zones(
            bars_15m["high"].values,
            bars_15m["low"].values,
            bars_15m["close"].values,
            min_tests=1,
        )
        n_zones   = len(zones)
        n_dist    = n_touches = n_pending = n_rsi = n_div = n_pass3b = n_rr = 0
        open_count = open_count_global

        for zone in zones:
            zk = self._zone_key(zone["center"])

            # Distance
            dist_pct = (current - zone["high"]) / zone["high"]
            if not (-0.012 <= dist_pct <= 0.002):
                n_dist += 1; continue
            # Touches
            if self._touches[zk] >= config.GEO_MAX_TOUCHES:
                n_touches += 1; continue
            # Pending
            if zk in self._pending:
                n_pending += 1; continue

            # ── Pass 3 : RSI 5min ─────────────────────────────────────────────
            bars_5m = self.broker.get_bars(symbol, "5Min", limit=30)
            if bars_5m is None or bars_5m.empty or len(bars_5m) < 15:
                continue
            closes_5m = bars_5m["close"].values
            rsi_now   = _rsi(closes_5m, 14)
            div       = self._rsi_divergence(closes_5m, rsi_now)

            if not (config.GEO_RSI_LOW <= rsi_now <= config.GEO_RSI_HIGH):
                n_rsi += 1; continue
            if not div:
                n_div += 1; continue

            # ── Pass 3b : EMA momentum 5min ───────────────────────────────────
            if len(closes_5m) >= 10:
                ema5  = float(pd.Series(closes_5m).ewm(span=5,  adjust=False).mean().iloc[-1])
                ema10 = float(pd.Series(closes_5m).ewm(span=10, adjust=False).mean().iloc[-1])
                if ema5 < ema10 * 0.9985:
                    n_pass3b += 1; continue

            # Stop + target
            stop   = self._dynamic_stop(bars_5m["low"].values, zone["center"], zone["wick_low"])
            target = _smart_round(zone["high"] * (1 + config.GEO_TARGET_PCT))
            risk   = abs(zone["high"] - stop)
            reward = abs(target - zone["high"])
            if risk <= 0 or reward / risk < 1.2:
                n_rr += 1; continue

            # Sizing
            available = self.get_available()
            if available < 30: break
            current_capital = self._live_capital()
            deploy = min(available * 0.995, current_capital * config.GEO_POS_PCT)
            limit_price = _smart_round(zone["high"])
            if deploy / limit_price < 0.001: continue  # trop petit

            # ── Place l'ordre OKX avec SL + TP attachés ───────────────────────
            order_id = self.broker.place_limit_buy(
                symbol     = symbol,
                price      = limit_price,
                stop_loss  = stop,
                take_profit= target,
                deploy_usdt= deploy,
            )
            if not order_id:
                continue

            self._touches[zk] += 1
            self._pending[zk] = {
                "order_id": order_id,
                "symbol":   symbol,
                "level":    zone["center"],
                "high":     zone["high"],
                "stop":     stop,
                "target":   target,
                "deploy":   deploy,
            }
            logger.info(
                f"[GEO] 📋 ORDER: {symbol} @ ${limit_price:.4f} "
                f"SL=${_smart_round(stop):.4f} TP=${_smart_round(target):.4f} "
                f"RSI={rsi_now:.0f} div={div} zone_tests={zone['tests']}"
            )

            # Log dashboard
            if self.memory:
                try:
                    self.memory.log_decision(
                        "BUY",
                        f"GEO V4: {symbol} LIMIT @ ${limit_price:.4f} | "
                        f"SL=${stop:.4f} TP=${target:.4f} R:R={round(reward/risk,1)}x | "
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
            if open_count >= config.GEO_MAX_SIM:
                break

        # Résumé si aucun ordre
        if open_count == open_count_global:
            reasons = []
            if n_dist:    reasons.append(f"dist:{n_dist}")
            if n_touches: reasons.append(f"touches:{n_touches}")
            if n_pending: reasons.append(f"pending:{n_pending}")
            if n_rsi:     reasons.append(f"rsi:{n_rsi}")
            if n_div:     reasons.append(f"div:{n_div}")
            if n_pass3b:  reasons.append(f"pass3b:{n_pass3b}")
            if n_rr:      reasons.append(f"rr:{n_rr}")
            reason_str = ", ".join(reasons) if reasons else "no_zones_in_range"
            logger.info(
                f"[GEO] {symbol} — no signal | zones={n_zones} "
                f"price={current:.4f} | skip: {reason_str}"
            )

    # ── MANAGE PENDING ────────────────────────────────────────────────────────

    def manage_pending_orders(self):
        """Vérifie les ordres GTC en attente : fill → log trade.
        SL + TP sont déjà attachés à l'ordre OKX → aucun ordre supplémentaire à placer."""
        for zk in list(self._pending.keys()):
            p = self._pending[zk]
            try:
                order = self.broker.get_order(p["symbol"], p["order_id"])
                if order is None:
                    continue

                state = order.status   # "live", "filled", "cancelled", "partially_filled"

                if state == "filled":
                    fill  = float(order.filled_avg_price or p.get("high", p["level"]))
                    qty   = float(order.filled_qty or 0)
                    symbol= p["symbol"]
                    logger.info(f"[GEO] ✅ FILLED: {symbol} @ ${fill:.4f} qty={qty:.4f}")
                    if self.memory:
                        self.memory.log_trade_open(
                            trade_id=str(uuid.uuid4()),
                            symbol=symbol, side="buy",
                            qty=qty, entry_price=fill,
                            stop_loss=p["stop"], take_profit=p["target"],
                            alpaca_order_id=p["order_id"],
                            market_context={
                                "strategy_source": "geo_v4",
                                "side":   "long",
                                "level":  float(p["level"]),
                                "stop":   float(p["stop"]),
                                "target": float(p["target"]),
                                "broker": "okx",
                            }
                        )
                    # ⚠️ SL + TP déjà actifs côté OKX — rien à faire ici
                    del self._pending[zk]

                elif state in ("cancelled", "expired", "rejected"):
                    logger.info(f"[GEO] 🗑 Order {state}: {p['symbol']}")
                    del self._pending[zk]

                else:  # "live" ou "partially_filled"
                    # Vérifier si le niveau est cassé → annuler
                    bars = self.broker.get_bars(p["symbol"], "1Min", limit=3)
                    if bars is not None and not bars.empty:
                        curr = float(bars["close"].iloc[-1])
                        if curr < p["level"] * 0.997:
                            logger.info(f"[GEO] 🚫 Niveau cassé {p['symbol']} — annulation")
                            self._cancel_order(p["symbol"], p["order_id"])
                            del self._pending[zk]

            except Exception as e:
                logger.debug(f"[GEO] manage_pending {p.get('symbol')}: {e}")

    # ── MANAGE POSITIONS ──────────────────────────────────────────────────────

    def manage_open_positions(self):
        """
        SIMPLIFIÉ vs version Alpaca :
        - SL et TP sont des ordres réels sur OKX → pas de price-check bot-side
        - Cette méthode :
            1. Détecte les positions fermées par OKX (SL ou TP touché)
            2. Log la fermeture en DB avec le bon close_reason
            3. Time-stop : clôture forcée si position ouverte > 4h
        """
        try:
            # Positions OKX actives indexées par db_symbol
            broker_positions = {pos.db_symbol: pos for pos in (self.broker.get_positions() or [])}
            open_trades      = self.memory.get_open_trades()
            now_utc          = datetime.datetime.now(datetime.timezone.utc)

            for t in open_trades:
                ctx = self._ctx(t)
                if ctx.get("strategy_source") != "geo_v4":
                    continue

                symbol   = t.get("symbol")      # "ETH/USD"
                trade_id = t.get("trade_id")
                entry    = float(t.get("entry_price", 0))
                qty_t    = float(t.get("qty", 0))
                stop_db  = float(t.get("stop_loss") or 0)
                tp_db    = float(t.get("take_profit") or 0)

                # Timestamp d'ouverture pour filtrer les fills
                entry_at_str = t.get("entry_at", "")
                try:
                    entry_dt   = datetime.datetime.fromisoformat(
                        str(entry_at_str).replace("Z", "+00:00"))
                    entry_ts_ms= int(entry_dt.timestamp() * 1000)
                except Exception:
                    entry_ts_ms = 0

                # ── 1. Position fermée par OKX (SL ou TP touché) ──────────────
                if symbol not in broker_positions:
                    # Récupérer le fill de clôture
                    fill_info = self.broker.get_last_fill(symbol, since_ts_ms=entry_ts_ms)
                    if fill_info:
                        fill_price = fill_info["price"]
                        fill_qty   = fill_info.get("qty", qty_t)
                        pnl        = (fill_price - entry) * fill_qty

                        # Détecter la raison : stop ou target
                        if stop_db and fill_price <= stop_db * 1.01:
                            reason = "stop"
                        elif tp_db and fill_price >= tp_db * 0.99:
                            reason = "target"
                        else:
                            reason = "stop" if pnl < 0 else "target"

                        emoji = "🔴" if reason == "stop" else "💰"
                        logger.info(
                            f"[GEO] {emoji} OKX exit: {symbol} @ ${fill_price:.4f} "
                            f"({reason}) pnl=${pnl:.2f}"
                        )
                        self.memory.log_trade_close(trade_id, fill_price, reason, pnl=pnl)
                    else:
                        # Pas de fill trouvé → position disparue sans fill (ex: expirée)
                        # Utiliser le prix live comme approximation
                        live = self.broker.get_live_price(symbol) or entry
                        pnl  = (live - entry) * qty_t
                        logger.warning(
                            f"[GEO] {symbol} absent des positions OKX, pas de fill trouvé "
                            f"— fermeture approx @ ${live:.4f} pnl=${pnl:.2f}"
                        )
                        reason = "stop" if pnl < 0 else "target"
                        self.memory.log_trade_close(trade_id, live, reason, pnl=pnl)
                    continue

                # ── 2. Time-stop : > 4h sans conviction ───────────────────────
                if entry_at_str:
                    try:
                        elapsed_min = (now_utc - entry_dt).total_seconds() / 60
                        if elapsed_min >= TIMEOUT_MIN:
                            pos     = broker_positions[symbol]
                            current = float(pos.current_price)
                            qty_p   = float(pos.qty)
                            logger.info(
                                f"[GEO] ⏰ TIME-STOP {symbol} après {elapsed_min:.0f}min"
                            )
                            closed = self.broker.close_position(symbol)
                            if closed:
                                pnl = (current - entry) * qty_p
                                self.memory.log_trade_close(trade_id, current, "timeout", pnl=pnl)
                    except Exception as e:
                        logger.error(f"[GEO] time-stop {symbol}: {e}")

        except Exception as e:
            logger.error(f"[GEO] manage_open_positions: {e}")


