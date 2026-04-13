"""
config.py — Jim Bot Geo-Only ETH+SOL
"""
import os

# ── Broker actif ───────────────────────────────────────────────────────────────
USE_BROKER = "bybit"

# ── Kraken Futures ─────────────────────────────────────────────────────────────
KRAKEN_API_KEY    = os.getenv("KRAKEN_API_KEY", "")
KRAKEN_SECRET_KEY = os.getenv("KRAKEN_SECRET_KEY", "")
KRAKEN_PAPER      = os.getenv("KRAKEN_PAPER", "1") == "1"   # 1 = démo, 0 = live

# ── Bybit (conservé pour référence) ────────────────────────────────────────────
BYBIT_API_KEY    = os.getenv("BYBIT_API_KEY", "")
BYBIT_SECRET_KEY = os.getenv("BYBIT_SECRET_KEY", "")
BYBIT_TESTNET    = os.getenv("BYBIT_TESTNET", "1") == "1"
BYBIT_LEVERAGE   = int(os.getenv("BYBIT_LEVERAGE", "1"))

# ── Capital ────────────────────────────────────────────────────────────────────
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "1000.00"))
GEO_CAPITAL     = INITIAL_CAPITAL

# ── Reset P&L ──────────────────────────────────────────────────────────────────
GEO_RESET_DATE = "2026-04-06"

# ── Geo V4 — Paramètres stratégie ──────────────────────────────────────────────
GEO_SYMBOLS       = ["ETH/USD", "SOL/USD"]
GEO_ZONE_PCT      = 0.003    # Zone ±0.3% autour du pivot
GEO_MAX_SIM       = 2        # Max 2 positions simultanées
GEO_POS_PCT       = 0.50     # 50% du capital par position
GEO_TARGET_PCT    = 0.009    # Target +0.9%
GEO_MAX_TOUCHES   = 2        # Skip zone si touchée > 2 fois
GEO_RSI_LOW       = 20
GEO_RSI_HIGH      = 65

# ── Boucles ────────────────────────────────────────────────────────────────────
FAST_LOOP_SECONDS = 30     # manage_pending + manage_positions
SLOW_LOOP_SECONDS = 300    # evaluate() — nouveau signal

# ── Sécurité ───────────────────────────────────────────────────────────────────
MONTHLY_LOSS_CAP_PCT = 0.15   # Pause si -15% dans le mois
