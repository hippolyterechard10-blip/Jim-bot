"""
geometry.py — Geometric & Candlestick Analysis
Support/resistance detection, candlestick patterns, chart patterns, ATR-based stops.
Transforms arbitrary % stops into technically anchored levels.
"""
import logging
import numpy as np

logger = logging.getLogger(__name__)


class GeometryAnalysis:

    def __init__(self):
        logger.info("✅ GeometryAnalysis initialized")

    # ── ATR Calculation ───────────────────────────────────────────────────────

    def calculate_atr(self, highs: list, lows: list, closes: list, period: int = 14) -> float:
        """Calculate Average True Range over given period."""
        if len(closes) < period + 1:
            return 0.0
        true_ranges = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1])
            )
            true_ranges.append(tr)
        atr = sum(true_ranges[-period:]) / period
        return round(atr, 6)

    # ── Support & Resistance ──────────────────────────────────────────────────

    def find_support_resistance(
        self,
        closes: list,
        highs: list,
        lows: list,
        lookback: int = 50
    ) -> dict:
        """
        Identify key support and resistance levels using swing highs/lows.
        Score each level by how many times it has been tested.
        """
        if len(closes) < lookback:
            lookback = len(closes)

        recent_closes = closes[-lookback:]
        recent_highs  = highs[-lookback:]
        recent_lows   = lows[-lookback:]
        current_price = closes[-1]

        swing_highs = []
        swing_lows  = []

        for i in range(2, len(recent_highs) - 2):
            if recent_highs[i] > recent_highs[i-1] and recent_highs[i] > recent_highs[i+1]:
                swing_highs.append(recent_highs[i])
            if recent_lows[i] < recent_lows[i-1] and recent_lows[i] < recent_lows[i+1]:
                swing_lows.append(recent_lows[i])

        supports    = [l for l in swing_lows   if l < current_price * 0.999]
        resistances = [h for h in swing_highs  if h > current_price * 1.001]

        nearest_support    = max(supports)    if supports    else current_price * 0.97
        nearest_resistance = min(resistances) if resistances else current_price * 1.03

        support_score = min(5, sum(
            1 for l in swing_lows
            if abs(l - nearest_support) / nearest_support < 0.005
        ))
        resistance_score = min(5, sum(
            1 for h in swing_highs
            if abs(h - nearest_resistance) / nearest_resistance < 0.005
        ))

        return {
            "current_price":           current_price,
            "nearest_support":         round(nearest_support, 6),
            "nearest_resistance":      round(nearest_resistance, 6),
            "support_score":           support_score,
            "resistance_score":        resistance_score,
            "support_distance_pct":    round((current_price - nearest_support)    / current_price * 100, 2),
            "resistance_distance_pct": round((nearest_resistance - current_price) / current_price * 100, 2),
            "swing_highs":             sorted(swing_highs, reverse=True)[:3],
            "swing_lows":              sorted(swing_lows,  reverse=True)[:3],
        }

    def find_htf_levels(
        self,
        highs_4h: list = None,
        lows_4h: list = None,
        highs_1d: list = None,
        lows_1d: list = None,
    ) -> dict:
        """
        Extract swing high/low levels from 4h and daily bars for HTF confluence scoring.
        Uses the same ±1-bar swing detection as find_support_resistance().
        Returns htf_supports and htf_resistances as flat price lists.
        """
        htf_supports = []
        htf_resistances = []
        for highs, lows in [(highs_4h, lows_4h), (highs_1d, lows_1d)]:
            if not highs or not lows or len(highs) < 3:
                continue
            for i in range(1, len(highs) - 1):
                if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
                    htf_resistances.append(highs[i])
                if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
                    htf_supports.append(lows[i])
        return {"htf_supports": htf_supports, "htf_resistances": htf_resistances}

    # ── ATR-Based Stop Calculation ────────────────────────────────────────────

    def calculate_atr_stop(
        self,
        entry_price: float,
        atr: float,
        side: str,
        support: float = None,
        resistance: float = None,
    ) -> dict:
        """
        Calculate stop loss and take profit using ATR + support/resistance.
        Stop is placed 0.3% beyond the nearest technical level.
        Take profit = 2x the stop distance (1:2 minimum risk/reward).
        """
        if side == "long":
            if support and (entry_price - support) / entry_price < 0.04:
                stop_price = support * 0.997
            else:
                stop_price = entry_price - (1.0 * atr)

            stop_distance = entry_price - stop_price
            target_price  = entry_price + (2.0 * stop_distance)
            stop_pct      = round(stop_distance  / entry_price * 100, 2)
            target_pct    = round((target_price  - entry_price) / entry_price * 100, 2)

        else:  # short
            if resistance and (resistance - entry_price) / entry_price < 0.04:
                stop_price = resistance * 1.003
            else:
                stop_price = entry_price + (1.0 * atr)

            stop_distance = stop_price - entry_price
            target_price  = entry_price - (2.0 * stop_distance)
            stop_pct      = round(stop_distance  / entry_price * 100, 2)
            target_pct    = round((entry_price   - target_price) / entry_price * 100, 2)

        return {
            "entry":       round(entry_price, 6),
            "stop":        round(stop_price,  6),
            "target":      round(target_price, 6),
            "stop_pct":    stop_pct,
            "target_pct":  target_pct,
            "risk_reward": round(target_pct / stop_pct, 2) if stop_pct > 0 else 0,
            "atr":         round(atr, 6),
        }

    # ── Candlestick Patterns ──────────────────────────────────────────────────

    def detect_candlestick_patterns(
        self,
        opens: list,
        highs: list,
        lows: list,
        closes: list,
        volumes: list = None
    ) -> dict:
        """
        Detect the 5 most important single-candle patterns + 2 multi-candle patterns.
        Returns detected patterns with score adjustments.
        """
        if len(closes) < 3:
            return {"patterns": [], "score_adjustment": 0}

        patterns  = []
        score_adj = 0

        o, h, l, c = opens[-1], highs[-1], lows[-1], closes[-1]
        body         = abs(c - o)
        upper_wick   = h - max(o, c)
        lower_wick   = min(o, c) - l
        candle_range = h - l

        if candle_range == 0:
            return {"patterns": [], "score_adjustment": 0}

        # ── Hammer ──
        if (lower_wick >= 2 * body and
                upper_wick <= body * 0.3 and
                c > o and body > 0):
            patterns.append({
                "name": "HAMMER", "direction": "bullish", "score_adj": +15,
                "note": "Strong reversal signal — buyers absorbed selling pressure"
            })
            score_adj += 15

        # ── Shooting Star ──
        elif (upper_wick >= 2 * body and
              lower_wick <= body * 0.3 and
              c < o and body > 0):
            patterns.append({
                "name": "SHOOTING_STAR", "direction": "bearish", "score_adj": -15,
                "note": "Strong reversal signal — sellers overwhelmed buyers"
            })
            score_adj -= 15

        # ── Doji ──
        elif body <= candle_range * 0.1:
            patterns.append({
                "name": "DOJI", "direction": "neutral", "score_adj": -5,
                "note": "Indecision — wait for next candle confirmation"
            })
            score_adj -= 5

        # ── Pin Bar ──
        elif (lower_wick >= 3 * body or upper_wick >= 3 * body):
            direction = "bullish" if lower_wick >= 3 * body else "bearish"
            adj = +20 if direction == "bullish" else -20
            patterns.append({
                "name": "PIN_BAR", "direction": direction, "score_adj": adj,
                "note": "Strong price rejection — trade opposite to the long wick"
            })
            score_adj += adj

        # ── Bullish Engulfing ──
        if (len(closes) >= 2 and
                opens[-2] > closes[-2] and
                c > o and
                o < closes[-2] and
                c > opens[-2]):
            patterns.append({
                "name": "BULLISH_ENGULFING", "direction": "bullish", "score_adj": +20,
                "note": "Buyers completely absorbed previous sellers — strong reversal"
            })
            score_adj += 20

        # ── Bearish Engulfing ──
        elif (len(closes) >= 2 and
              closes[-2] > opens[-2] and
              c < o and
              o > closes[-2] and
              c < opens[-2]):
            patterns.append({
                "name": "BEARISH_ENGULFING", "direction": "bearish", "score_adj": -20,
                "note": "Sellers completely absorbed previous buyers — strong reversal"
            })
            score_adj -= 20

        # ── Three White Soldiers ──
        if (len(closes) >= 3 and
                closes[-1] > closes[-2] > closes[-3] and
                opens[-1]  > opens[-2]  > opens[-3]  and
                closes[-1] > opens[-1] and
                closes[-2] > opens[-2] and
                closes[-3] > opens[-3]):
            patterns.append({
                "name": "THREE_WHITE_SOLDIERS", "direction": "bullish", "score_adj": +25,
                "note": "Strong momentum continuation — three consecutive green candles"
            })
            score_adj += 25

        # ── Three Black Crows ──
        elif (len(closes) >= 3 and
              closes[-1] < closes[-2] < closes[-3] and
              opens[-1]  < opens[-2]  < opens[-3]  and
              closes[-1] < opens[-1] and
              closes[-2] < opens[-2] and
              closes[-3] < opens[-3]):
            patterns.append({
                "name": "THREE_BLACK_CROWS", "direction": "bearish", "score_adj": -25,
                "note": "Strong bearish momentum — three consecutive red candles"
            })
            score_adj -= 25

        # Volume confirmation boost
        if volumes and len(volumes) >= 2:
            avg_vol = sum(volumes[-10:]) / len(volumes[-10:]) if len(volumes) >= 10 else volumes[-1]
            if volumes[-1] > avg_vol * 1.5 and patterns:
                patterns[-1]["note"] += " (VOLUME CONFIRMED ✅)"
                score_adj += 10
            elif volumes[-1] < avg_vol * 0.7 and patterns:
                patterns[-1]["note"] += " (low volume — less reliable ⚠️)"
                score_adj -= 5

        return {
            "patterns":      patterns,
            "score_adjustment": score_adj,
            "latest_candle": {
                "open": o, "high": h, "low": l, "close": c,
                "body_pct":  round(body / candle_range * 100, 1),
                "direction": "green" if c >= o else "red"
            }
        }

    # ── Chart Pattern Detection ───────────────────────────────────────────────

    def detect_chart_patterns(
        self,
        closes: list,
        highs: list,
        lows: list,
        volumes: list = None,
        lookback: int = 30
    ) -> dict:
        """
        Detect Bull/Bear Flag and Double Top/Bottom patterns.
        """
        if len(closes) < lookback:
            return {"patterns": [], "score_adjustment": 0}

        patterns  = []
        score_adj = 0

        recent_closes = closes[-lookback:]
        recent_highs  = highs[-lookback:]
        recent_lows   = lows[-lookback:]
        mid           = lookback // 2

        first_half_change  = (recent_closes[mid] - recent_closes[0])  / recent_closes[0]  * 100
        second_half_change = (recent_closes[-1]  - recent_closes[mid]) / recent_closes[mid] * 100

        # ── Bull Flag ──
        if first_half_change > 5 and -3 < second_half_change < 1:
            patterns.append({
                "name": "BULL_FLAG", "direction": "bullish", "score_adj": +20,
                "note":       f"Bull flag: +{first_half_change:.1f}% flagpole then consolidation. Breakout imminent.",
                "entry":      "Break above flag high with volume",
                "target_pct": f"+{first_half_change:.0f}% (flagpole height)"
            })
            score_adj += 20

        # ── Bear Flag ──
        elif first_half_change < -5 and -1 < second_half_change < 3:
            patterns.append({
                "name": "BEAR_FLAG", "direction": "bearish", "score_adj": -20,
                "note":       f"Bear flag: {first_half_change:.1f}% drop then weak bounce. Breakdown imminent.",
                "entry":      "Break below flag low with volume",
                "target_pct": f"{first_half_change:.0f}% (flagpole height)"
            })
            score_adj -= 20

        # ── Double Top ──
        if len(recent_highs) >= 20:
            top1_idx = recent_highs.index(max(recent_highs[:mid]))
            top2_idx = mid + recent_highs[mid:].index(max(recent_highs[mid:]))
            top1, top2 = recent_highs[top1_idx], recent_highs[top2_idx]

            if (abs(top1 - top2) / top1 < 0.02 and
                    top1_idx != top2_idx and
                    recent_closes[-1] < top2 * 0.98):
                valley = min(recent_closes[top1_idx:top2_idx]) if top1_idx < top2_idx else 0
                patterns.append({
                    "name": "DOUBLE_TOP", "direction": "bearish", "score_adj": -25,
                    "note":  f"Double top at ~${top1:.2f}. Price failing to break resistance twice.",
                    "entry": f"Short on break below ${valley:.2f}",
                })
                score_adj -= 25

        # ── Double Bottom ──
        if len(recent_lows) >= 20:
            bot1_idx = recent_lows.index(min(recent_lows[:mid]))
            bot2_idx = mid + recent_lows[mid:].index(min(recent_lows[mid:]))
            bot1, bot2 = recent_lows[bot1_idx], recent_lows[bot2_idx]

            if (abs(bot1 - bot2) / bot1 < 0.02 and
                    bot1_idx != bot2_idx and
                    recent_closes[-1] > bot2 * 1.02):
                peak = max(recent_closes[bot1_idx:bot2_idx]) if bot1_idx < bot2_idx else 0
                patterns.append({
                    "name": "DOUBLE_BOTTOM", "direction": "bullish", "score_adj": +25,
                    "note":  f"Double bottom at ~${bot1:.2f}. Strong support held twice.",
                    "entry": f"Long on break above ${peak:.2f}",
                })
                score_adj += 25

        return {"patterns": patterns, "score_adjustment": score_adj}

    # ── Volume Analysis ───────────────────────────────────────────────────────

    def analyze_volume(self, volumes: list, period: int = 20) -> dict:
        """
        Analyze volume relative to average.
        Returns confirmation signal and score adjustment.
        """
        if not volumes or len(volumes) < 5:
            return {"signal": "unknown", "score_adjustment": 0, "ratio": 1.0}

        avg_vol     = sum(volumes[-period:]) / min(period, len(volumes))
        current_vol = volumes[-1]
        ratio       = round(current_vol / avg_vol, 2) if avg_vol > 0 else 1.0

        if ratio >= 3.0:
            return {
                "signal": "climax_volume", "score_adjustment": 0, "ratio": ratio,
                "note": f"⚡ CLIMAX VOLUME ({ratio:.1f}x avg) — potential exhaustion/reversal, not continuation"
            }
        elif ratio >= 1.5:
            return {
                "signal": "high_volume", "score_adjustment": +15, "ratio": ratio,
                "note": f"✅ High volume confirmation ({ratio:.1f}x avg)"
            }
        elif ratio < 0.7:
            return {
                "signal": "low_volume", "score_adjustment": -10, "ratio": ratio,
                "note": f"⚠️ Low volume ({ratio:.1f}x avg) — signal less reliable"
            }
        else:
            return {
                "signal": "normal_volume", "score_adjustment": 0, "ratio": ratio,
                "note": f"Volume normal ({ratio:.1f}x avg)"
            }

    # ── Master Context Builder ────────────────────────────────────────────────

    def build_geometry_context(
        self,
        symbol: str,
        opens: list,
        highs: list,
        lows: list,
        closes: list,
        volumes: list = None,
        side: str = "long"
    ) -> dict:
        """
        Master function — runs all geometric analysis and returns:
        - Combined score adjustment
        - ATR-based stop/target levels
        - Full context string for Claude's prompt
        """
        if len(closes) < 10:
            return {
                "context":          "Insufficient price history for geometric analysis",
                "score_adjustment": 0,
                "stop_loss":        None,
                "take_profit":      None,
            }

        atr           = self.calculate_atr(highs, lows, closes)
        current_price = closes[-1]
        atr_pct       = round(atr / current_price * 100, 3) if current_price > 0 else 0

        sr      = self.find_support_resistance(closes, highs, lows)
        candles = self.detect_candlestick_patterns(opens, highs, lows, closes, volumes)
        charts  = self.detect_chart_patterns(closes, highs, lows, volumes)
        vol     = (
            self.analyze_volume(volumes)
            if volumes
            else {"signal": "unknown", "score_adjustment": 0, "ratio": 1.0}
        )

        stops = self.calculate_atr_stop(
            entry_price=current_price,
            atr=atr,
            side=side,
            support=sr["nearest_support"]    if side == "long"  else None,
            resistance=sr["nearest_resistance"] if side == "short" else None,
        )

        total_score_adj = (
            candles["score_adjustment"] +
            charts["score_adjustment"]  +
            vol["score_adjustment"]
        )

        if side == "long"  and sr["support_score"]    >= 3:
            total_score_adj += 15
        elif side == "short" and sr["resistance_score"] >= 3:
            total_score_adj += 15

        lines = [f"=== GEOMETRIC ANALYSIS: {symbol} ==="]
        lines.append(f"Price: ${current_price:.4f} | ATR: ${atr:.4f} ({atr_pct:.2f}%)")
        lines.append(
            f"Support: ${sr['nearest_support']:.4f} (-{sr['support_distance_pct']:.2f}%) "
            f"score={sr['support_score']}/5"
        )
        lines.append(
            f"Resistance: ${sr['nearest_resistance']:.4f} (+{sr['resistance_distance_pct']:.2f}%) "
            f"score={sr['resistance_score']}/5"
        )

        if candles["patterns"]:
            for p in candles["patterns"]:
                emoji = "🟢" if p["direction"] == "bullish" else "🔴" if p["direction"] == "bearish" else "⚪"
                lines.append(f"Candle: {emoji} {p['name']} — {p['note']}")
        else:
            lines.append("Candle: No significant pattern detected")

        if charts["patterns"]:
            for p in charts["patterns"]:
                emoji = "🟢" if p["direction"] == "bullish" else "🔴"
                lines.append(f"Chart: {emoji} {p['name']} — {p['note']}")

        lines.append(f"Volume: {vol.get('note', 'N/A')}")
        lines.append(
            f"ATR Stop ({side.upper()}): entry=${stops['entry']:.4f} | "
            f"stop=${stops['stop']:.4f} (-{stops['stop_pct']:.2f}%) | "
            f"target=${stops['target']:.4f} (+{stops['target_pct']:.2f}%) | "
            f"R:R={stops['risk_reward']:.1f}x"
        )
        lines.append(f"Geometry score adjustment: {total_score_adj:+d}")

        return {
            "context":           "\n".join(lines),
            "score_adjustment":  total_score_adj,
            "stop_loss":         stops["stop"],
            "take_profit":       stops["target"],
            "stop_pct":          stops["stop_pct"],
            "target_pct":        stops["target_pct"],
            "risk_reward":       stops["risk_reward"],
            "atr":               atr,
            "support":           sr["nearest_support"],
            "resistance":        sr["nearest_resistance"],
            "patterns_detected": [p["name"] for p in candles["patterns"] + charts["patterns"]],
        }

    # ── Méthodes scalping 3-timeframes ───────────────────────────────────────

    def find_swing_levels(self, bars_df, min_tests: int = 2) -> dict:
        """
        Détecte les niveaux S/R depuis un DataFrame (n'importe quel timeframe).
        Groupe les pivots proches à ±0.3% en clusters.
        Ne retourne que les niveaux testés >= min_tests fois.
        Utilisé par GeometricExpert pour les passes 15min et 5min.
        """
        if bars_df is None or bars_df.empty or len(bars_df) < 10:
            return {"supports": [], "resistances": []}

        highs   = bars_df["high"].tolist()
        lows    = bars_df["low"].tolist()
        closes  = bars_df["close"].tolist()
        current = closes[-1]

        swing_highs = []
        swing_lows  = []

        # Fenêtre de 2 bougies de chaque côté — confirmée (pas look-ahead live)
        for i in range(2, len(highs) - 2):
            if (highs[i] > highs[i-1] and highs[i] > highs[i-2]
                    and highs[i] > highs[i+1] and highs[i] > highs[i+2]):
                swing_highs.append(highs[i])
            if (lows[i] < lows[i-1] and lows[i] < lows[i-2]
                    and lows[i] < lows[i+1] and lows[i] < lows[i+2]):
                swing_lows.append(lows[i])

        def _cluster(levels, zone_pct=0.003):
            """Regroupe les niveaux proches. Retourne [(prix_moyen, nb_tests)]."""
            if not levels:
                return []
            sorted_lvls = sorted(levels)
            clusters    = [[sorted_lvls[0]]]
            for lvl in sorted_lvls[1:]:
                if (lvl - clusters[-1][0]) / clusters[-1][0] < zone_pct:
                    clusters[-1].append(lvl)
                else:
                    clusters.append([lvl])
            return [(sum(c) / len(c), len(c)) for c in clusters]

        supports = [
            {"level": round(price, 8), "tests": count}
            for price, count in _cluster(swing_lows)
            if count >= min_tests and price < current * 0.999
        ]
        resistances = [
            {"level": round(price, 8), "tests": count}
            for price, count in _cluster(swing_highs)
            if count >= min_tests and price > current * 1.001
        ]

        # Nearest-first: supports décroissants, resistances croissantes
        supports.sort(key=lambda x: x["level"], reverse=True)
        resistances.sort(key=lambda x: x["level"])

        return {"supports": supports, "resistances": resistances}

    def find_5min_stop(self, bars_5m, side: str, entry_price: float) -> float:
        """
        Cherche le swing low 5min le plus récent sous l'entrée (long)
        ou swing high au-dessus (short) dans une fenêtre de 15 bougies.
        Place le stop 0.2% au-delà du swing pour lui laisser un buffer.
        Fallback : entry * 0.997 (long) ou 1.003 (short) si aucun swing valide.
        Stop plafonné à -0.5% / +0.5% pour rester dans la philosophie scalping.
        """
        fallback_mult = 0.997 if side == "long" else 1.003
        fallback      = round(entry_price * fallback_mult, 8)

        if bars_5m is None or bars_5m.empty or len(bars_5m) < 6:
            return fallback

        highs = bars_5m["high"].tolist()
        lows  = bars_5m["low"].tolist()

        window_lows  = lows[-15:]
        window_highs = highs[-15:]

        if side == "long":
            max_stop = entry_price * 0.995          # plafond -0.5%
            candidates = [
                window_lows[i]
                for i in range(1, len(window_lows) - 1)
                if (window_lows[i] < window_lows[i-1]
                    and window_lows[i] < window_lows[i+1]
                    and max_stop <= window_lows[i] < entry_price)
            ]
            if candidates:
                stop = max(candidates) * 0.998      # 0.2% sous le swing low
                return round(stop, 8)

        else:  # short
            max_stop = entry_price * 1.005          # plafond +0.5%
            candidates = [
                window_highs[i]
                for i in range(1, len(window_highs) - 1)
                if (window_highs[i] > window_highs[i-1]
                    and window_highs[i] > window_highs[i+1]
                    and entry_price < window_highs[i] <= max_stop)
            ]
            if candidates:
                stop = min(candidates) * 1.002      # 0.2% au-dessus du swing high
                return round(stop, 8)

        return fallback

    def calculate_vwap(self, bars_df) -> float:
        """
        VWAP intraday = somme(prix_typique × volume) / somme(volume)
        Prix typique = (high + low + close) / 3.
        Retourne 0.0 si données insuffisantes ou volume nul.
        """
        if bars_df is None or bars_df.empty:
            return 0.0
        try:
            typical_price = (bars_df["high"] + bars_df["low"] + bars_df["close"]) / 3
            total_volume  = bars_df["volume"].sum()
            if total_volume <= 0:
                return 0.0
            vwap = (typical_price * bars_df["volume"]).sum() / total_volume
            return round(float(vwap), 8)
        except Exception:
            return 0.0
