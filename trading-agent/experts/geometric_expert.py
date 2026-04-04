"""
geometric_expert.py — Geo V4 ETH-Only
Stratégie validée backtest 2022-2025 :
  - Zones ±0.3% autour des pivots 15min
  - Limit order à zone["high"] (support × 1.003)
  - Stop dynamique sous wick réel
  - RSI divergence [20-65] + Pass 3b
  - Zone freshness MAX_TOUCHES=2
  - Target +0.9% fallback
"""
import logging, json, uuid
from collections import defaultdict
import numpy as np
import config

logger = logging.getLogger(__name__)


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
        # Pending limit orders : zone_key → {order_id, level, stop, target, qty}
        self._pending: dict = {}
        # Zone touch tracking
        self._touches: defaultdict = defaultdict(int)
        # Réconciliation au démarrage — récupère l'état Alpaca
        self._reconcile_alpaca_state()

    def _reconcile_alpaca_state(self):
        """Au démarrage, recharge les ordres GTC et les positions orphelines depuis Alpaca.
        Évite de perdre le suivi après un redémarrage du bot."""
        try:
            # 1. Recharger les ordres GTC ouverts dans self._pending
            open_orders = self.broker.api.list_orders(status="open")
            for o in open_orders:
                sym = o.symbol  # ex: 'ETH/USD'
                if o.time_in_force == "gtc" and o.side == "buy" and o.limit_price:
                    lim    = float(o.limit_price)
                    qty    = float(o.qty or 0)
                    stop   = round(lim * 0.997, 4)
                    target = round(lim * 1.009, 4)
                    zk     = f"{sym}_{lim:.4f}"
                    self._pending[zk] = {
                        "order_id": o.id,
                        "symbol":   sym,
                        "level":    lim,
                        "stop":     stop,
                        "target":   target,
                        "qty":      qty,
                    }
                    logger.info(f"[GEO] 🔄 Recovered pending order: {sym} GTC@{lim}")

            # 2. Réconcilier les positions Alpaca sans trade DB correspondant
            positions = self.broker.api.list_positions()
            open_db   = {t["symbol"] for t in self.memory.get_open_trades()}
            for pos in positions:
                sym = pos.symbol.replace("USD", "/USD") if "/" not in pos.symbol else pos.symbol
                if sym not in open_db and any(s in sym for s in config.GEO_SYMBOLS):
                    entry  = float(pos.avg_entry_price)
                    qty    = float(pos.qty)
                    # Reconstituer zone_center (entry est zone_high = center × 1.003)
                    # et stop = zone_low × 0.999 = (center × 0.997) × 0.999
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
                    logger.info(f"[GEO] 🔄 Recovered orphan position: {sym} qty={qty} entry={entry}")
        except Exception as e:
            logger.warning(f"[GEO] _reconcile_alpaca_state: {e}")

    # ── Capital ───────────────────────────────────────────────────────────────

    def _live_capital(self) -> float:
        """Equity Alpaca réelle — source de vérité pour le sizing.
        Fallback: GEO_CAPITAL + closed_pnl si l'API est indisponible."""
        try:
            equity = float(self.broker.api.get_account().equity)
            logger.debug(f"[GEO] live capital Alpaca: ${equity:.2f}")
            return equity
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
        except Exception as e:
            logger.error(f"[GEO] get_deployed: {e}")
            return config.GEO_CAPITAL

    def get_available(self) -> float:
        """Utilise buying_power Alpaca — déjà net des ordres GTC en attente."""
        try:
            bp = float(self.broker.api.get_account().buying_power)
            return max(0.0, bp)
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
        except Exception as e:
            logger.debug(f"[GEO] _closed_pnl: {e}")
            return 0.0

    def _daily_pnl(self) -> float:
        """PnL des trades geo_v4 fermés depuis minuit UTC aujourd'hui."""
        try:
            import sqlite3
            from datetime import datetime, timezone
            midnight = datetime.now(timezone.utc).replace(
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
        except Exception as e:
            logger.debug(f"[GEO] _daily_pnl: {e}")
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

    def _cancel_order(self, order_id: str | None, symbol: str = ""):
        if not order_id: return
        try:
            self.broker.api.cancel_order(order_id)
            logger.info(f"[GEO] 🛑 Cancelled {order_id}")
        except Exception as e:
            logger.debug(f"[GEO] cancel {order_id}: {e}")

    # ── EVALUATE ──────────────────────────────────────────────────────────────

    def evaluate(self, symbol: str = None, regime: str = "unknown"):
        symbol = symbol or config.GEO_SYMBOLS[0]
        logger.info(f"[GEO] evaluating {symbol} | régime={regime}")

        # Gate régime
        _r = (regime or "unknown").lower()
        if _r in ("bear", "panic"):
            logger.info(f"[GEO] 🔴 Régime {_r} — pas d'entrée")
            return

        # Circuit-breaker journalier — stop si perte > MONTHLY_LOSS_CAP_PCT × GEO_CAPITAL
        daily_loss = self._daily_pnl()
        daily_cap  = -abs(config.MONTHLY_LOSS_CAP_PCT * config.GEO_CAPITAL)
        if daily_loss < daily_cap:
            logger.warning(
                f"[GEO] 🚨 Circuit-breaker: perte jour ${daily_loss:.2f} "
                f"< seuil ${daily_cap:.2f} — pas d'entrée aujourd'hui"
            )
            return

        # Double-entrée guard — pool global ETH+SOL
        open_count_global = len([t for t in self.memory.get_open_trades()
                                 if self._ctx(t).get("strategy_source") == "geo_v4"])
        open_count_global += len(self._pending)
        if open_count_global >= config.GEO_MAX_SIM:
            logger.info(f"[GEO] Pool global plein ({open_count_global}/{config.GEO_MAX_SIM}, dont {len(self._pending)} pending) — skip {symbol}")
            return

        # Capital
        if not self.has_capital():
            logger.info(f"[GEO] Capital insuffisant (${self.get_available():.0f})")
            return

        # ── Pass 1 : Bias 1h ──────────────────────────────────────────────────
        bars_1h = self.broker.get_bars(symbol, "1Hour", limit=50)
        if bars_1h is None or bars_1h.empty or len(bars_1h) < 10:
            return
        h1h = bars_1h["high"].values; l1h = bars_1h["low"].values
        hh  = h1h[-1] > h1h[-4];     hl  = l1h[-1] > l1h[-4]
        lh  = h1h[-1] < h1h[-4];     ll  = l1h[-1] < l1h[-4]
        if lh and ll:
            logger.info(f"[GEO] {symbol} — downtrend 1h (lh={lh} ll={ll}), skip")
            return

        # ── Pass 2 : Zones 15min ──────────────────────────────────────────────
        bars_15m = self.broker.get_bars(symbol, "15Min", limit=100)
        if bars_15m is None or bars_15m.empty or len(bars_15m) < 20:
            return
        current = float(bars_15m["close"].iloc[-1])
        zones   = self._find_zones(
            bars_15m["high"].values, bars_15m["low"].values,
            bars_15m["close"].values, min_tests=1
        )

        # ── Pass 3 : Confirmation 5min ────────────────────────────────────────
        bars_5m = self.broker.get_bars(symbol, "5Min", limit=30)
        if bars_5m is None or bars_5m.empty or len(bars_5m) < 10:
            return
        closes_5m = bars_5m["close"].values
        vols_5m   = bars_5m["volume"].values
        rsi_now   = _rsi(closes_5m, 14)

        # Volume check — utiliser le dernier candle COMPLÉTÉ ([-2]), pas le candle
        # en cours de formation ([-1] qui commence toujours à 0)
        completed_vols = vols_5m[:-1] if len(vols_5m) > 1 else vols_5m
        avg_vol = completed_vols[-20:].mean() if len(completed_vols) >= 20 else completed_vols.mean()
        last_completed_vol = completed_vols[-1]
        # Fallback heure creuse : si la dernière bougie complétée a un volume nul
        # (Alpaca renvoie parfois 0 sur les périodes peu liquides), utiliser la
        # moyenne des 5 dernières bougies non-nulles pour éviter les faux positifs.
        if last_completed_vol == 0:
            nonzero = [v for v in completed_vols[-6:-1] if v > 0]
            last_completed_vol = float(np.mean(nonzero)) if nonzero else avg_vol
            logger.debug(f"[GEO] {symbol} — vol=0 fallback → {last_completed_vol:.4f}")
        if avg_vol > 0 and last_completed_vol < avg_vol * 0.3:
            logger.info(f"[GEO] {symbol} — volume trop bas (vol={last_completed_vol:.4f} < seuil={avg_vol*0.3:.4f} | avg20={avg_vol:.4f})")
            return

        # Évaluer chaque zone
        open_count = len([t for t in self.memory.get_open_trades()
                          if self._ctx(t).get("strategy_source") == "geo_v4"])

        n_zones = len(zones)
        n_dist = n_touches = n_pending = n_rsi = n_div = n_pass3b = n_rr = 0

        for zone in zones:
            if open_count >= config.GEO_MAX_SIM: break

            zk   = self._zone_key(zone["center"])
            dist = (current - zone["center"]) / current

            if not (0.001 <= dist <= 0.020): n_dist += 1; continue
            if self._touches[zk] >= config.GEO_MAX_TOUCHES: n_touches += 1; continue
            if zk in self._pending: n_pending += 1; continue

            # RSI divergence
            if not (config.GEO_RSI_LOW <= rsi_now <= config.GEO_RSI_HIGH): n_rsi += 1; continue
            div = self._rsi_divergence(closes_5m, rsi_now)
            if not div and not (30 <= rsi_now <= 55): n_div += 1; continue

            # Pass 3b — touché ET remonté
            touched      = any(bars_5m["low"].values[-4:] <= zone["high"])
            closed_above = closes_5m[-1] > zone["low"]
            if not (touched and closed_above): n_pass3b += 1; continue

            # Stop + target
            stop   = self._dynamic_stop(bars_5m["low"].values, zone["center"], zone["wick_low"])
            target = _smart_round(zone["high"] * (1 + config.GEO_TARGET_PCT))
            risk   = abs(zone["high"] - stop)
            reward = abs(target - zone["high"])
            if risk <= 0 or reward / risk < 1.2: n_rr += 1; continue

            # Sizing — basé sur l'equity Alpaca réelle
            available = self.get_available()
            if available < 30: break
            current_capital = self._live_capital()
            deploy = min(available * 0.995, current_capital * config.GEO_POS_PCT)

            # Placement limit order (Alpaca crypto ne supporte pas bracket/OCO)
            # Stop + TP sont gérés après fill dans manage_open_positions()
            limit_price = _smart_round(zone["high"])
            qty = round(deploy / limit_price, 6)
            if qty * limit_price < 20: continue
            order_id = None
            try:
                order = self.broker.api.submit_order(
                    symbol=symbol, qty=qty, side="buy",
                    type="limit", limit_price=limit_price,
                    time_in_force="gtc",
                )
                order_id = getattr(order, "id", None)
                logger.info(
                    f"[GEO] 📋 LIMIT PLACED: {symbol} @ ${limit_price:.4f} | "
                    f"stop=${_smart_round(stop):.4f} | target=${_smart_round(target):.4f} | qty={qty} | "
                    f"RSI={rsi_now:.0f} | div={div} | zone_tests={zone['tests']}"
                )
            except Exception as e:
                logger.error(f"[GEO] order error {symbol}: {e}")
                continue

            self._touches[zk] += 1
            self._pending[zk] = {
                "order_id": order_id, "level": zone["center"],
                "high": zone["high"], "stop": stop,
                "target": target, "qty": qty, "symbol": symbol,
            }

            # Log dashboard
            if self.memory:
                try:
                    self.memory.log_decision(
                        "BUY",
                        f"GEO V4: {symbol} LIMIT @ ${limit_price:.4f} | "
                        f"stop=${stop:.4f} target=${target:.4f} R:R={round(reward/risk,1)}x | "
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

        # Résumé si aucun ordre placé
        if open_count == len([t for t in self.memory.get_open_trades()
                               if self._ctx(t).get("strategy_source") == "geo_v4"]):
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
                f"[GEO] {symbol} — no signal | zones={n_zones} RSI={rsi_now:.0f} "
                f"price={closes_5m[-1]:.4f} | skip: {reason_str}"
            )

    # ── MANAGE PENDING ────────────────────────────────────────────────────────

    def manage_pending_orders(self):
        """Appelé toutes les 30s. Détecte les fills et annule les ordres si niveau cassé."""
        for zk in list(self._pending.keys()):
            p = self._pending[zk]
            try:
                order  = self.broker.api.get_order(p["order_id"])
                status = order.status

                if status == "filled":
                    fill   = float(order.filled_avg_price or p["level"])
                    qty    = float(order.filled_qty or p["qty"])
                    symbol = p["symbol"]
                    logger.info(f"[GEO] ✅ FILLED: {symbol} @ ${fill:.4f}")
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
                            }
                        )
                    # Placer le stop_limit protecteur dans Alpaca
                    # (Alpaca crypto ne supporte pas bracket/OCO — TP géré par price check)
                    try:
                        stop_p   = _smart_round(p["stop"])
                        stop_lp  = _smart_round(p["stop"] * 0.996)
                        self.broker.api.submit_order(
                            symbol=symbol, qty=qty, side="sell",
                            type="stop_limit",
                            stop_price=stop_p, limit_price=stop_lp,
                            time_in_force="gtc",
                        )
                        logger.info(f"[GEO] 🛑 Stop-limit placé: {symbol} trigger=${stop_p} limit=${stop_lp}")
                    except Exception as e:
                        logger.error(f"[GEO] stop-limit post-fill {symbol}: {e}")
                    del self._pending[zk]

                elif status in ("canceled", "expired", "rejected"):
                    logger.info(f"[GEO] 🗑 Order {status}: {p['symbol']}")
                    del self._pending[zk]

                else:
                    # Vérifier si niveau cassé
                    bars = self.broker.get_bars(p["symbol"], "1Min", limit=3)
                    if bars is not None and not bars.empty:
                        curr = float(bars["close"].iloc[-1])
                        if curr < p["level"] * 0.997:
                            logger.info(f"[GEO] 🚫 Niveau cassé {p['symbol']} — annulation")
                            self._cancel_order(p["order_id"], p["symbol"])
                            del self._pending[zk]

            except Exception as e:
                logger.debug(f"[GEO] manage_pending {p.get('symbol')}: {e}")

    # ── MANAGE POSITIONS ──────────────────────────────────────────────────────

    def manage_open_positions(self):
        """Exits gérés par ordres Alpaca réels (stop-market + limit).
        Cette méthode surveille uniquement :
          1. Positions fermées par Alpaca (stop ou target rempli) → mise à jour DB
          2. Time-stop : clôture forcée si position ouverte > 4h
        """
        import datetime
        TIMEOUT_MIN = 240  # 4h

        def _sym_key(s):
            """Normalise 'ETH/USD' → 'ETHUSD' pour matcher les positions Alpaca."""
            return s.replace("/", "").replace("-", "").upper()

        try:
            # Positions Alpaca indexées par clé normalisée (ETHUSD, SOLUSD…)
            alpaca_positions = {_sym_key(p.symbol): p for p in (self.broker.get_positions() or [])}
            open_trades      = self.memory.get_open_trades()

            # Tracker stop_limit et limit (TP bracket) séparément
            try:
                raw_open_sells = self.broker.api.list_orders(status="open", limit=50)
                stop_sell_syms = {
                    _sym_key(o.symbol) for o in raw_open_sells
                    if o.side == "sell" and o.type == "stop_limit"
                }
                tp_sell_syms = {
                    _sym_key(o.symbol) for o in raw_open_sells
                    if o.side == "sell" and o.type == "limit"
                }
            except Exception:
                stop_sell_syms = set()
                tp_sell_syms   = set()

            for t in open_trades:
                ctx = self._ctx(t)
                if ctx.get("strategy_source") != "geo_v4": continue

                symbol   = t.get("symbol")      # "ETH/USD" (format DB)
                sym_k    = _sym_key(symbol)     # "ETHUSD"  (format Alpaca positions)
                trade_id = t.get("trade_id")
                entry    = float(t.get("entry_price", 0))
                qty_t    = float(t.get("qty", 0))

                # ── 0. Réconciliation : place le stop_limit si absent ────────────
                if sym_k in alpaca_positions and sym_k not in stop_sell_syms:
                    stop_db = float(t.get("stop_loss") or 0)
                    pos_qty = float(alpaca_positions[sym_k].qty)
                    if stop_db and pos_qty > 0:
                        try:
                            stop_p  = _smart_round(stop_db)
                            stop_lp = _smart_round(stop_db * 0.996)
                            self.broker.api.submit_order(
                                symbol=symbol, qty=pos_qty, side="sell",
                                type="stop_limit",
                                stop_price=stop_p, limit_price=stop_lp,
                                time_in_force="gtc",
                            )
                            stop_sell_syms.add(sym_k)
                            logger.info(
                                f"[GEO] 🔄 Réconciliation stop: {symbol} "
                                f"trigger=${stop_p} limit=${stop_lp}"
                            )
                        except Exception as e:
                            logger.error(f"[GEO] réconciliation stop {symbol}: {e}")

                # ── 0b. Take-profit : price check si pas de bracket TP Alpaca ──────
                # Bracket orders: le TP est géré par Alpaca (OCO). Sans bracket
                # (legacy ou fallback), le bot ferme lui-même quand prix ≥ target.
                if sym_k in alpaca_positions and sym_k not in tp_sell_syms:
                    target_db = float(t.get("take_profit") or 0)
                    if target_db > 0:
                        pos = alpaca_positions[sym_k]
                        current_px = float(pos.current_price or 0)
                        if current_px >= target_db:
                            try:
                                pos_qty = float(pos.qty)
                                # Annuler le stop_limit protecteur
                                for o in self.broker.api.list_orders(status="open", limit=50):
                                    if _sym_key(o.symbol) == sym_k and o.side == "sell":
                                        self.broker.api.cancel_order(o.id)
                                # Clôture au marché
                                self.broker.close_position(symbol)
                                pnl = (current_px - entry) * pos_qty
                                logger.info(
                                    f"[GEO] 💰 TAKE-PROFIT bot: {symbol} "
                                    f"@ ${current_px:.4f} target=${target_db:.4f} pnl=${pnl:.2f}"
                                )
                                self.memory.log_trade_close(trade_id, current_px, "target", pnl=pnl)
                                continue
                            except Exception as e:
                                logger.error(f"[GEO] take-profit close {symbol}: {e}")

                # ── 1. Position fermée par Alpaca (stop ou target rempli) ────────
                if sym_k not in alpaca_positions:
                    try:
                        filled_sells = [
                            o for o in self.broker.api.list_orders(
                                status="closed", limit=20)
                            if o.symbol == symbol
                            and o.side   == "sell"
                            and o.status == "filled"
                        ]
                        if filled_sells:
                            o          = sorted(filled_sells, key=lambda x: x.filled_at, reverse=True)[0]
                            fill_price = float(o.filled_avg_price or entry)
                            qty_f      = float(o.filled_qty or qty_t)
                            pnl        = (fill_price - entry) * qty_f
                            reason     = "stop" if fill_price <= float(t.get("stop_loss") or 0) * 1.005 else "target"
                            logger.info(
                                f"[GEO] {'🔴' if reason=='stop' else '💰'} "
                                f"Alpaca exit détecté: {symbol} @ ${fill_price:.4f} ({reason}) pnl=${pnl:.2f}"
                            )
                            self.memory.log_trade_close(trade_id, fill_price, reason, pnl=pnl)
                        else:
                            logger.warning(f"[GEO] {symbol} absent des positions Alpaca — pas de sell trouvé")
                    except Exception as e:
                        logger.error(f"[GEO] detect_alpaca_exit {symbol}: {e}")
                    continue

                # ── 2. Time-stop : > 4h sans conviction ─────────────────────────
                entry_at_str = t.get("entry_at")
                if entry_at_str:
                    try:
                        entry_dt    = datetime.datetime.fromisoformat(
                            str(entry_at_str).replace("Z", "+00:00")
                        )
                        now_utc     = datetime.datetime.now(datetime.timezone.utc)
                        elapsed_min = (now_utc - entry_dt).total_seconds() / 60
                        if elapsed_min >= TIMEOUT_MIN:
                            pos     = alpaca_positions[sym_k]
                            current = float(pos.current_price)
                            qty_p   = float(pos.qty)
                            logger.info(
                                f"[GEO] ⏰ TIME-STOP {symbol} après {elapsed_min:.0f}min — clôture forcée"
                            )
                            # Annuler les ordres stop/target Alpaca ouverts
                            try:
                                for o in self.broker.api.list_orders(status="open", limit=50):
                                    if _sym_key(o.symbol) == sym_k and o.side == "sell":
                                        self.broker.api.cancel_order(o.id)
                                        logger.info(f"[GEO] ⏰ Ordre {o.type} annulé ({o.id[:8]})")
                            except Exception as ce:
                                logger.debug(f"[GEO] cancel time-stop {symbol}: {ce}")
                            self.broker.close_position(symbol)
                            pnl = (current - entry) * qty_p
                            self.memory.log_trade_close(trade_id, current, "timeout", pnl=pnl)
                    except Exception as e:
                        logger.error(f"[GEO] time-stop {symbol}: {e}")

        except Exception as e:
            logger.error(f"[GEO] manage_open_positions: {e}")
