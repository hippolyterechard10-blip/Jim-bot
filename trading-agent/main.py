import logging
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

def main():
    logger.info("🚀 Trading Agent starting...")

    memory   = TradingMemory("trading_memory.db")
    broker   = AlpacaBroker()
    risk     = RiskManager(broker)
    agent    = TradingAgent(broker, risk, memory)
    analyzer = TradeAnalyzer(memory)
    notifier = TradingNotifier(memory, analyzer)

    start_dashboard(memory, analyzer, port=5000)
    notifier.start_scheduler(daily_hour_utc=20)
    notifier.send_test_email()

    logger.info("✅ All systems running. Starting trading loop...")

    cycle = 0
    while True:
        try:
            cycle += 1
            logger.info(f"--- Cycle {cycle} ---")

            if risk.check_global_stop_loss():
                logger.error("🔴 GLOBAL STOP LOSS — shutting down")
                broker.close_all_positions()
                notifier.send_stop_loss_alert(
                    broker.get_portfolio_value()
                )
                break

            agent.run_cycle()
            analyzer.run_pending_analyses()

            anomalies = analyzer.detect_performance_anomalies()
            if anomalies:
                logger.warning(f"{len(anomalies)} anomalie(s) détectée(s)")

            time.sleep(config.LOOP_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            logger.info("⛔ Agent stopped by user")
            break
        except Exception as e:
            logger.error(f"Cycle error: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
