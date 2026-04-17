"""
config.py — Jim Bot Geo-Only ETH+SOL — Kraken Futures
"""
import os

# ── Broker actif (Kraken Futures par défaut) ───────────────────────────────────
USE_BROKER    = os.getenv("ACTIVE_BROKER", "kraken")
ACTIVE_BROKER = USE_BROKER    # "kraken" | "bybit" | "alpaca"

# ── Kraken Futures ─────────────────────────────────────────────────────────────
KRAKEN_API_KEY    = os.getenv("KRAKEN_API_KEY", "")
# accepte KRAKEN_SECRET_KEY (legacy) ou KRAKEN_API_SECRET (recommandé)
KRAKEN_SECRET_KEY = os.getenv("KRAKEN_API_SECRET", os.getenv("KRAKEN_SECRET_KEY", ""))
KRAKEN_PAPER      = os.getenv("KRAKEN_PAPER", "1") == "1"   # 1 = démo, 0 = live

# ── Bybit (legacy — optionnel) ─────────────────────────────────────────────────
BYBIT_API_KEY    = os.getenv("BYBIT_API_KEY", "")
BYBIT_SECRET_KEY = os.getenv("BYBIT_API_SECRET", "")
BYBIT_TESTNET    = os.getenv("BYBIT_TESTNET", "0") == "1"
BYBIT_DEMO       = os.getenv("BYBIT_DEMO", "true").lower() == "true"
BYBIT_EU         = os.getenv("BYBIT_EU", "1")
BYBIT_LEVERAGE   = int(os.getenv("BYBIT_LEVERAGE", "1"))

# ── Capital ────────────────────────────────────────────────────────────────────
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "100.0"))
GEO_CAPITAL     = float(os.getenv("INITIAL_CAPITAL", "100.0"))

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

# ── Short (miroir des résistances) ─────────────────────────────────────────────
# Backtest 2022-2025 : +122 pts/an sur SOL, +172 pts/an sur ETH quand activé
GEO_ENABLE_SHORT  = os.getenv("GEO_ENABLE_SHORT", "0") == "1"
GEO_RSI_LOW_S     = 35
GEO_RSI_HIGH_S    = 80

# ── Boucles ────────────────────────────────────────────────────────────────────
FAST_LOOP_SECONDS = 30     # manage_pending + manage_positions
SLOW_LOOP_SECONDS = 300    # evaluate() — nouveau signal

# ── Sécurité ───────────────────────────────────────────────────────────────────
MONTHLY_LOSS_CAP_PCT = 0.15   # Pause si -15% dans le mois
