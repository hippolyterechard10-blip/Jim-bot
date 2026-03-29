import json
import logging
import anthropic
import config
from strategy import (
    compute_indicators,
    detect_patterns,
    get_session_context,
    build_strategy_prompt,
    rank_symbols,
    is_good_stock_window,
    is_crypto_good_hours,
)

logger = logging.getLogger(__name__)

class TradingAgent:
    def __init__(self, broker, risk_manager, memory=None):
        self.broker = broker
        self.risk = risk_manager
        self.memory = memory
        self.client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    def analyze_market(self, symbol, bars):
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

        prompt = build_strategy_prompt(
            symbol=symbol,
            indicators=indicators,
            patterns=patterns,
            session_ctx=session_ctx,
            memory_context=memory_context,
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
        if not self.risk.can_trade():
            logger.warning("⚠️ Trading paused by risk manager")
            return

        session_ctx = get_session_context()
        session = session_ctx["session"]
        logger.info(f"📅 Session: {session} ({session_ctx['time_et']}) — stocks: {session_ctx['good_for_stocks']} | crypto: {session_ctx['good_for_crypto']}")

        symbols_to_scan = []
        for symbol in config.ALL_SYMBOLS:
            is_crypto = "/" in symbol
            if is_crypto and not is_crypto_good_hours():
                continue
            if not is_crypto and not is_good_stock_window():
                continue
            symbols_to_scan.append(symbol)

        if not symbols_to_scan:
            logger.info("😴 No symbols to scan in current session — crypto off-hours and market closed")
            return

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

        for symbol in ranked:
            try:
                data = symbols_data[symbol]
                bars = data["bars"]

                decision = self.analyze_market(symbol, bars)
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

                if session == "weekend":
                    min_confidence = 0.70
                elif session in ("mid_day", "after_hours", "closed"):
                    min_confidence = 0.85
                else:
                    min_confidence = 0.70

                if action == "buy" and confidence >= min_confidence:
                    qty = self.risk.get_position_size(symbol, current_price)
                    sl = self.risk.calculate_stop_loss(current_price, "buy")
                    tp = self.risk.calculate_take_profit(current_price, "buy")
                    self.broker.place_order(symbol, qty, "buy", sl, tp)

                elif action == "sell" and confidence >= min_confidence:
                    self.broker.close_position(symbol)

            except Exception as e:
                logger.error(f"Error on {symbol}: {e}")
