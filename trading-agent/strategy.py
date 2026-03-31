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
    trs = [max(abs(prices[i] - prices[i-1]), abs(prices[i] - prices[i-2]) if i > 1 else 0)
           for i in range(1, len(prices))]
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
        if "GAPPER" in patterns.get("patterns", []):
            score += 35
        else:
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
