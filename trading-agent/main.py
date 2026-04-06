"""
main.py — Jim Bot Geo-Only ETH+SOL
Boucle : fast loop 30s + slow loop 5min + watchdog.
Broker : Bybit USDT Perpetual (testnet ou live selon BYBIT_TESTNET).
"""
import logging
import os
import threading
import time
from dotenv import load_dotenv
load_dotenv()

import config
from memory import TradingMemory
from geometry import GeometryAnalysis
from regime import MarketRegime
from experts.geometric_expert import GeometricExpert
from dashboard import start_dashboard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)


def _make_broker():
    if config.USE_BROKER == "kraken":
        from kraken_broker import KrakenBroker
        return KrakenBroker()
    from bybit_broker import BybitBroker
    return BybitBroker()


def make_fast_thread(geo: GeometricExpert, broker) -> threading.Thread:
    def _run():
        logger.info("[FAST] ⚡ Fast loop started — 30s")
        while True:
            try:
                geo.manage_pending_orders()
                geo.manage_open_positions()
            except Exception as e:
                logger.error(f"[FAST] error: {e}")
            time.sleep(config.FAST_LOOP_SECONDS)
    return threading.Thread(target=_run, daemon=True, name="FastLoop")


def _run_watchdog(geo: GeometricExpert, broker, thread_ref: list):
    while True:
        time.sleep(60)
        t = thread_ref[0]
        if not t.is_alive():
            logger.warning("[WATCHDOG] Fast loop mort — redémarrage")
            new = make_fast_thread(geo, broker)
            new.start()
            thread_ref[0] = new
            logger.info("[WATCHDOG] ✅ Redémarré")
        else:
            logger.info("[WATCHDOG] ✅ Fast loop alive")


def main():
    logger.info(f"🚀 Jim Bot démarrage (broker={config.USE_BROKER})...")

    memory   = TradingMemory("trading_memory.db")
    broker   = _make_broker()
    geometry = GeometryAnalysis()
    regime   = MarketRegime()

    geo = GeometricExpert(
        broker=broker,
        memory=memory,
        geometry=geometry,
        regime=regime,
    )

    logger.info(f"💰 Capital : ${config.GEO_CAPITAL} | Assets : {config.GEO_SYMBOLS}")

    port = int(os.getenv("PORT", 5000))
    start_dashboard(memory, regime=regime, port=port)

    fast_thread = make_fast_thread(geo, broker)
    fast_thread.start()
    thread_ref  = [fast_thread]

    threading.Thread(
        target=_run_watchdog,
        args=(geo, broker, thread_ref),
        daemon=True,
        name="Watchdog"
    ).start()

    logger.info(f"✅ Prêt — fast:{config.FAST_LOOP_SECONDS}s slow:{config.SLOW_LOOP_SECONDS}s")

    cycle = 0
    while True:
        try:
            cycle += 1
            logger.info(f"[SLOW] ── Cycle {cycle} ──")

            current_regime = regime.detect_regime()

            if current_regime in ("bear", "panic"):
                logger.info(f"[SLOW] 🔴 Régime {current_regime.upper()} — pas de nouveaux signaux")
            else:
                for symbol in config.GEO_SYMBOLS:
                    geo.evaluate(symbol=symbol, regime=current_regime)

            time.sleep(config.SLOW_LOOP_SECONDS)

        except KeyboardInterrupt:
            logger.info("⛔ Arrêt manuel")
            break
        except Exception as e:
            logger.error(f"[SLOW] Cycle error: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
