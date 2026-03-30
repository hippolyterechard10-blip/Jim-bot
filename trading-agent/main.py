import logging
import threading
import time
from dotenv import load_dotenv
load_dotenv()

import config
from broker import AlpacaBroker
from risk import RiskManager
from agent import TradingAgent
from memory import TradingMemory
from analyzer import TradeAnalyzer
from dashboard import start_dashboard
from notifier import TradingNotifier

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)
logger = logging.getLogger(__name__)


def _run_fast_loop(agent: TradingAgent):
    """
    Background thread — fires every 30 seconds.
    Handles:
      • Trailing stop checks    → [FAST] FAST EXIT
      • Hard stop loss checks   → [FAST] FAST STOP
      • Technical score scan    → [FAST] FAST TRIGGER when score > 60 or < 30
      • Volume spike detection  → [FAST] VOLUME SPIKE
    Never calls Claude directly — triggers analyze_market() only on threshold cross.
    """
    logger.info("[FAST] ⚡ Fast loop thread started — 30s tick")
    while True:
        try:
            agent.fast_loop_tick()
        except Exception as e:
            logger.error(f"[FAST] loop error: {e}")
        time.sleep(config.FAST_LOOP_INTERVAL_SECONDS)


def main():
    logger.info("🚀 Trading Agent starting...")

    memory   = TradingMemory("trading_memory.db")
    broker   = AlpacaBroker()
    risk     = RiskManager(broker)
    agent    = TradingAgent(broker, risk, memory)
    analyzer = TradeAnalyzer(memory)
    notifier = TradingNotifier(memory, analyzer)

    start_dashboard(memory, analyzer, scanner=agent.scanner, port=5000)
    notifier.start_scheduler(daily_hour_utc=20)

    # ── Start the fast loop in a daemon thread ────────────────────────────────
    fast_thread = threading.Thread(
        target=_run_fast_loop,
        args=(agent,),
        daemon=True,
        name="FastLoop",
    )
    fast_thread.start()

    logger.info(
        f"✅ All systems running — "
        f"fast loop: {config.FAST_LOOP_INTERVAL_SECONDS}s | "
        f"slow loop: {config.LOOP_INTERVAL_SECONDS}s"
    )

    # ── Slow loop (main thread) ───────────────────────────────────────────────
    cycle = 0
    while True:
        try:
            cycle += 1
            logger.info(f"[SLOW] --- Cycle {cycle} ---")

            if risk.check_global_stop_loss():
                logger.error("🔴 GLOBAL STOP LOSS — shutting down")
                broker.close_all_positions()
                notifier.send_stop_loss_alert(
                    broker.get_portfolio_value()
                )
                break

            # Hand off symbols the fast loop already triggered this cycle
            skip_symbols = agent.consume_fast_triggered()
            if skip_symbols:
                logger.info(
                    f"[SLOW] ⏭ Fast loop triggered {len(skip_symbols)} symbol(s) "
                    f"this cycle — skipping in slow scan: {sorted(skip_symbols)}"
                )

            agent.run_cycle(skip_symbols=skip_symbols)
            analyzer.run_pending_analyses()

            anomalies = analyzer.detect_performance_anomalies()
            if anomalies:
                logger.warning(f"[SLOW] {len(anomalies)} anomalie(s) detected")

            time.sleep(config.LOOP_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            logger.info("⛔ Agent stopped by user")
            break
        except Exception as e:
            logger.error(f"[SLOW] Cycle error: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
