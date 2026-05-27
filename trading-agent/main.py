"""
main.py — Jim Bot Geo V4 (ETH+SOL)
Boucle : fast loop 30s + slow loop 5min + in-process watchdog.
Broker : Kraken Futures (paper par défaut, live via ACTIVE_BROKER=kraken).
"""
import fcntl
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv()

import config
from memory import TradingMemory
from geometry import GeometryAnalysis
from regime import MarketRegime
from experts.geometric_expert import GeometricExpert
from dashboard import start_dashboard
import openclaw_notify as notify

# ─── Logging : RotatingFileHandler (append, 10 MB × 5 backups) ────────────────
LOG_PATH = "/tmp/jimbot.log"
_log_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
_h_file = logging.handlers.RotatingFileHandler(
    LOG_PATH, mode="a", maxBytes=10 * 1024 * 1024, backupCount=5
)
_h_file.setFormatter(_log_fmt)
_h_stderr = logging.StreamHandler(sys.stderr)
_h_stderr.setFormatter(_log_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_h_file, _h_stderr])
logger = logging.getLogger(__name__)
logger.info("=" * 60)
logger.info(f"Jim restart at {datetime.now(timezone.utc).isoformat()}")
logger.info("=" * 60)

# ─── PID flock — empêche les instances multiples ──────────────────────────────
_PID_LOCK_FD = None
def _acquire_pid_lock(pid_file: str = "/tmp/jimbot.pid"):
    """Acquire exclusive flock on PID file. Exits if another instance holds it.
    Opens in 'a+' (no truncate) so a rejected second launch ne vide pas le fichier."""
    global _PID_LOCK_FD
    _PID_LOCK_FD = open(pid_file, "a+")
    try:
        fcntl.flock(_PID_LOCK_FD, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.error(f"⛔ Another Jim instance holds the lock on {pid_file} — exiting")
        sys.exit(1)
    # Lock acquired — overwrite the PID
    _PID_LOCK_FD.seek(0)
    _PID_LOCK_FD.truncate()
    _PID_LOCK_FD.write(str(os.getpid()))
    _PID_LOCK_FD.flush()
    logger.info(f"✓ PID lock acquired: {os.getpid()}")

# ─── Signal handlers — graceful shutdown ──────────────────────────────────────
def _shutdown_handler(signum, frame):
    try:
        sig_name = signal.Signals(signum).name
    except Exception:
        sig_name = str(signum)
    logger.info(f"⛔ Received {sig_name} — graceful shutdown")
    sys.exit(0)

def _install_signal_handlers():
    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT,  _shutdown_handler)
    logger.info("✓ Signal handlers installed (SIGTERM, SIGINT)")


def _make_broker():
    broker_id = config.ACTIVE_BROKER
    if broker_id == "kraken_paper":
        from broker_kraken_paper import KrakenPaperBroker
        return KrakenPaperBroker()
    if broker_id == "kraken":
        from kraken_broker import KrakenBroker
        return KrakenBroker()
    raise ValueError(
        f"Unsupported broker '{broker_id}' — only kraken_paper and kraken are supported. "
        f"Legacy brokers (alpaca/bybit/okx) moved to archive/brokers_legacy/."
    )


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
    _acquire_pid_lock()
    _install_signal_handlers()
    logger.info(f"🚀 Jim Bot démarrage (broker={config.ACTIVE_BROKER})...")
    notify.bot_started(os.getpid())

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
