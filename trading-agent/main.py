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

WATCHDOG_INTERVAL_SECONDS = 60


def _make_fast_thread(agent: TradingAgent, mastermind=None) -> threading.Thread:
    def _run():
        logger.info("[FAST] ⚡ Fast loop thread started — 30s tick")
        while True:
            try:
                agent.fast_loop_tick()
                if mastermind:
                    mastermind.fast_tick()
            except Exception as e:
                logger.error(f"[FAST] loop error: {e}")
            time.sleep(config.FAST_LOOP_INTERVAL_SECONDS)
    return threading.Thread(target=_run, daemon=True, name="FastLoop")


def _run_watchdog(agent: TradingAgent, thread_ref: list, mastermind=None):
    """
    Daemon thread — wakes every 60 seconds.
    Checks whether the fast loop thread is still alive.
    If dead, spawns a fresh replacement and updates thread_ref[0].
    """
    while True:
        time.sleep(WATCHDOG_INTERVAL_SECONDS)
        t = thread_ref[0]
        if t.is_alive():
            logger.info("[WATCHDOG] fast loop alive ✅")
        else:
            logger.warning("[WATCHDOG] fast loop dead — restarting 🔄")
            new_thread = _make_fast_thread(agent, mastermind)
            new_thread.start()
            thread_ref[0] = new_thread
            logger.info("[WATCHDOG] fast loop restarted successfully ✅")


def main():
    logger.info("🚀 Trading Agent starting...")

    memory   = TradingMemory("trading_memory.db")
    broker   = AlpacaBroker()
    risk     = RiskManager(broker)
    agent    = TradingAgent(broker, risk, memory)

    from mastermind import Mastermind
    mastermind = Mastermind(
        broker=broker,
        memory=memory,
        scanner=agent.scanner,
        regime=agent.regime,
        geometry=agent.geometry,
        correlations=agent.correlations,
        agent=agent,
    )

    analyzer = TradeAnalyzer(memory)
    notifier = TradingNotifier(memory, analyzer)

    # On startup: sync today's filled Alpaca orders into DB (handles crash recovery)
    logger.info("🔄 Syncing today's Alpaca orders with DB...")
    agent._sync_todays_orders()
    # Also mark any DB-open records that no longer exist in Alpaca as closed
    agent._reconcile_stale_positions()

    start_dashboard(memory, analyzer, scanner=agent.scanner, regime=agent.regime, agent=agent, port=5000)
    notifier.start_scheduler(daily_hour_utc=20)

    # ── Start the fast loop ───────────────────────────────────────────────────
    fast_thread = _make_fast_thread(agent, mastermind)
    fast_thread.start()

    # thread_ref is a mutable list so the watchdog can swap in a replacement
    thread_ref = [fast_thread]

    # ── Start the watchdog ────────────────────────────────────────────────────
    watchdog_thread = threading.Thread(
        target=_run_watchdog,
        args=(agent, thread_ref, mastermind),
        daemon=True,
        name="Watchdog",
    )
    watchdog_thread.start()

    logger.info(
        f"✅ All systems running — "
        f"fast loop: {config.FAST_LOOP_INTERVAL_SECONDS}s | "
        f"slow loop: {config.LOOP_INTERVAL_SECONDS}s | "
        f"watchdog: {WATCHDOG_INTERVAL_SECONDS}s"
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
