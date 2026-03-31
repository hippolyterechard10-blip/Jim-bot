import json
import logging
from datetime import datetime, timezone
from typing import Optional
import anthropic
from memory import TradingMemory

logger = logging.getLogger(__name__)
CLAUDE_MODEL = "claude-sonnet-4-20250514"

class TradeAnalyzer:
    def __init__(self, memory: TradingMemory, api_key=None):
        self.memory = memory
        self.client = anthropic.Anthropic(api_key=api_key)
        logger.info("✅ TradeAnalyzer ready")

    def analyze_trade(self, trade: dict):
        if not trade.get("exit_price") or trade.get("status") != "closed":
            return None
        pnl = trade.get("pnl", 0) or 0
        outcome = "win" if pnl > 0 else ("loss" if pnl < 0 else "breakeven")

        snap = {}
        raw_snap = trade.get("entry_snapshot")
        if raw_snap:
            try:
                snap = json.loads(raw_snap) if isinstance(raw_snap, str) else (raw_snap or {})
            except Exception:
                snap = {}

        exit_vs_target = trade.get("exit_vs_target")

        original_decisions = self.memory.get_recent_decisions(limit=50, symbol=trade["symbol"])
        entry_decision = next(
            (d for d in original_decisions if d.get("trade_id") == trade.get("trade_id")), None
        )
        reasoning_text = (
            (entry_decision["reasoning"] if entry_decision else None)
            or snap.get("reasoning")
            or "N/A"
        )

        pnl_pct_str   = str(round(trade.get("pnl_pct", 0) or 0, 2))
        duration_str  = str(round(trade.get("hold_duration_min", 0) or 0, 1))

        prompt  = "You are a trading expert analyzing your own past trades to improve.\n\n"
        prompt += "Trade details:\n"
        prompt += "- Symbol: "        + str(trade["symbol"])                     + "\n"
        prompt += "- Side: "          + str(trade["side"].upper())                + "\n"
        prompt += "- Entry: $"        + str(trade.get("entry_price"))             + "\n"
        prompt += "- Exit: $"         + str(trade.get("exit_price"))              + "\n"
        prompt += "- P&L: $"          + str(round(pnl, 2)) + " (" + pnl_pct_str  + "%)\n"
        prompt += "- Result: "        + outcome.upper()                           + "\n"
        prompt += "- Duration: "      + duration_str                              + " minutes\n"
        prompt += "- Exit reason: "   + str(trade.get("close_reason") or "N/A")  + "\n"
        if exit_vs_target is not None:
            prompt += "- Exit vs target: " + str(exit_vs_target) + "% of objective reached\n"
        if snap:
            prompt += "\nEntry context (captured at trade open):\n"
            prompt += "- Session: "        + str(snap.get("session")       or "N/A") + "\n"
            prompt += "- Strategy: "       + str(snap.get("strategy_used") or "N/A") + "\n"
            prompt += "- Regime: "         + str(snap.get("regime")        or "N/A") + "\n"
            score_val = snap.get("final_score") or snap.get("base_score")
            prompt += "- Score: "          + str(score_val if score_val is not None else "N/A") + "/100\n"
            prompt += "- Score breakdown: "+ str(snap.get("score_breakdown") or "N/A") + "\n"
            conf = snap.get("confidence")
            prompt += "- Confidence: "     + (str(round(conf * 100, 0)) + "%" if conf else "N/A") + "\n"
            prompt += "- RSI: "            + str(snap.get("rsi")          or "N/A") + "\n"
            prompt += "- MACD bullish: "   + str(snap.get("macd_bullish"))           + "\n"
            vol = snap.get("volume_ratio")
            prompt += "- Volume ratio: "   + (str(round(vol, 2)) + "x" if vol else "N/A") + "\n"
            pats = snap.get("patterns") or []
            prompt += "- Patterns: "       + (", ".join(pats) if pats else "none") + "\n"
            prompt += "- Support: "        + str(snap.get("support")      or "N/A") + "\n"
            prompt += "- Resistance: "     + str(snap.get("resistance")   or "N/A") + "\n"
            rr = snap.get("risk_reward")
            prompt += "- Risk/Reward: "    + (str(round(rr, 2)) + "x" if rr else "N/A") + "\n"
        prompt += "- Original Claude reasoning: " + reasoning_text + "\n"
        prompt += "\nRespond ONLY with valid JSON, no markdown:\n"
        prompt += "{\n"
        prompt += '  "analysis": "3-5 sentence narrative analysis",\n'
        prompt += '  "outcome_reason": "1 sentence explaining the result",\n'
        prompt += '  "entry_quality": "assessment of entry timing and conditions",\n'
        prompt += '  "exit_timing": "assessment of exit: too early, too late, or optimal",\n'
        prompt += '  "one_improvement": "single most actionable improvement for this exact type of trade",\n'
        prompt += '  "mistakes": ["mistake 1", "mistake 2"],\n'
        prompt += '  "lessons": ["actionable lesson 1", "actionable lesson 2"],\n'
        prompt += '  "strategy_adjustments": "specific adjustments for future trades",\n'
        prompt += '  "would_take_same_trade": true,\n'
        prompt += '  "key_insight": "most important insight in 1 sentence"\n'
        prompt += "}"
        try:
            response = self.client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=1200,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data = json.loads(raw.strip())
            analysis_text = data.get("analysis", "")
            entry_quality = data.get("entry_quality", "")
            exit_timing   = data.get("exit_timing", "")
            if entry_quality:
                analysis_text += "\n\nEntry quality: " + entry_quality
            if exit_timing:
                analysis_text += "\nExit timing: " + exit_timing
            one_improvement = data.get("one_improvement", "")
            strategy_adj    = data.get("strategy_adjustments", "")
            if one_improvement:
                strategy_adj = one_improvement + (" | " + strategy_adj if strategy_adj else "")
            self.memory.save_trade_analysis(
                trade_id=trade["trade_id"],
                symbol=trade["symbol"],
                outcome=outcome,
                pnl=pnl,
                analysis=analysis_text,
                lessons=data.get("lessons", []),
                mistakes=data.get("mistakes", []),
                strategy_adj=strategy_adj
            )
            if data.get("key_insight"):
                self.memory.set_memory(
                    f"insight_{trade['symbol']}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}",
                    data["key_insight"], category="insight"
                )
            if data.get("strategy_adjustments"):
                self.memory.set_memory(
                    f"strategy_{trade['symbol']}",
                    data["strategy_adjustments"], category="strategy"
                )
            logger.info(f"✅ Analysis done for {trade['trade_id']} ({outcome})")
            return data
        except Exception as e:
            logger.error(f"analyze_trade error: {e}")
            return None

    def run_pending_analyses(self):
        pending = self.memory.get_closed_trades_unanalyzed()
        if not pending:
            return 0
        count = 0
        for trade in pending:
            if self.analyze_trade(trade):
                count += 1
        logger.info(f"✅ {count}/{len(pending)} analyses done")
        return count

    def generate_performance_report(self, period="weekly"):
        stats = self.memory.compute_performance_stats()
        if stats.get("total_trades", 0) == 0:
            return "No closed trades yet."
        analyses = self.memory.get_analyses(limit=10)
        lessons = []
        for a in analyses:
            if a.get("lessons"):
                try:
                    lessons.extend(json.loads(a["lessons"]) if isinstance(a["lessons"], str) else a["lessons"])
                except:
                    pass
        prompt = f"""Analyze this trading agent's {period} performance and write a clear report in French.

Stats: {json.dumps(stats, indent=2)}
Recent lessons: {chr(10).join(f'• {l}' for l in lessons[:6])}

Write a structured report with:
1. Executive summary (2-3 sentences)
2. Strengths
3. Weaknesses
4. Assets to favor / avoid
5. Concrete recommendations
6. Score out of 10 with justification

Be direct and critical."""
        try:
            response = self.client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=1500,
                messages=[{"role": "user", "content": prompt}]
            )
            return response.content[0].text.strip()
        except Exception as e:
            logger.error(f"generate_performance_report error: {e}")
            return f"Error: {e}"

    def detect_performance_anomalies(self):
        alerts = []
        stats = self.memory.compute_performance_stats()
        recent = self.memory.get_recent_trades(limit=10)
        if not recent or stats.get("total_trades", 0) < 3:
            return alerts
        if stats.get("win_rate", 50) < 30:
            alerts.append(f"⚠️ Win rate critical: {stats['win_rate']}% (threshold: 30%)")
        last_pnls = [t.get("pnl", 0) or 0 for t in recent if t.get("status") == "closed"]
        if len(last_pnls) >= 3 and all(p < 0 for p in last_pnls[:3]):
            alerts.append(f"🔴 3 consecutive losses. Total: ${sum(last_pnls[:3]):.2f}")
        if stats.get("max_drawdown", 0) > 150:
            alerts.append(f"⚠️ High drawdown: ${stats['max_drawdown']:.2f}")
        if 0 < stats.get("profit_factor", 1) < 1:
            alerts.append(f"📉 Profit factor < 1 ({stats['profit_factor']})")
        return alerts
