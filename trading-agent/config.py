"""
config.py — Jim Bot Geo-Only ETH
Configuration simplifiée : un seul asset, une seule stratégie.
"""
import os

# ── Alpaca ────────────────────────────────────────────────────────────────────
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL   = "https://paper-api.alpaca.markets"

# ── Capital ───────────────────────────────────────────────────────────────────
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "1000.0"))
GEO_CAPITAL     = INITIAL_CAPITAL   # Tout en geo, pas de split

# ── Nouveau départ — reset P&L à partir de cette date ─────────────────────────
# Seuls les trades après GEO_RESET_DATE entrent dans le calcul P&L du dashboard.
# capital_start sera l'equity réelle Alpaca au moment du reset.
GEO_RESET_DATE  = "2026-04-02"   # YYYY-MM-DD — mettre à jour à chaque reset

# ── Geo V4 — Paramètres validés par backtest 2022-2025 ───────────────────────
GEO_SYMBOL        = "ETH/USD"
GEO_ZONE_PCT      = 0.003    # Zone ±0.3% autour du pivot
GEO_MAX_SIM       = 2        # Max 2 positions simultanées
GEO_POS_PCT       = 0.28     # 28% du capital par position
GEO_TARGET_PCT    = 0.009    # Target +0.9%
GEO_MAX_TOUCHES   = 2        # Skip zone si touchée > 2 fois
GEO_RSI_LOW       = 20
GEO_RSI_HIGH      = 65

# ── Boucles ───────────────────────────────────────────────────────────────────
FAST_LOOP_SECONDS = 30     # manage_pending + manage_positions
SLOW_LOOP_SECONDS = 300    # evaluate() — nouveau signal

# ── Sécurité ──────────────────────────────────────────────────────────────────
MONTHLY_LOSS_CAP_PCT = 0.15   # Pause si -15% dans le mois
