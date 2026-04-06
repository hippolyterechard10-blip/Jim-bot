"""
config.py — Jim Bot Geo-Only ETH+SOL
"""
import os

# ── Broker actif ──────────────────────────────────────────────────────────────
# "okx"    → OKX Demo/Live (recommandé : supporte SL+TP natif)
# "alpaca" → Alpaca paper trading (legacy)
USE_BROKER = os.getenv("USE_BROKER", "okx")

# ── OKX ───────────────────────────────────────────────────────────────────────
OKX_API_KEY    = os.getenv("OKX_API_KEY", "")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")
OKX_DEMO       = os.getenv("OKX_DEMO", "1") == "1"   # Demo Trading = True par défaut
OKX_LEVERAGE   = int(os.getenv("OKX_LEVERAGE", "1"))  # 1x = équivalent spot

# Mapping DB symbol → OKX instId (USDT Perpetual)
OKX_SYMBOL_MAP = {
    "ETH/USD": "ETH-USDT-SWAP",
    "SOL/USD": "SOL-USDT-SWAP",
}

# ── Alpaca (legacy) ────────────────────────────────────────────────────────────
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL   = "https://paper-api.alpaca.markets"

# ── Capital ───────────────────────────────────────────────────────────────────
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "978.86"))
GEO_CAPITAL     = INITIAL_CAPITAL

# ── Reset P&L ─────────────────────────────────────────────────────────────────
GEO_RESET_DATE = "2026-04-06"   # YYYY-MM-DD — mis à jour lors de la migration OKX

# ── Geo V4 — Paramètres stratégie ─────────────────────────────────────────────
GEO_SYMBOLS       = ["ETH/USD", "SOL/USD"]
GEO_ZONE_PCT      = 0.003    # Zone ±0.3% autour du pivot
GEO_MAX_SIM       = 2        # Max 2 positions simultanées (pool global ETH+SOL)
GEO_POS_PCT       = 0.50     # 50% du capital par position
GEO_TARGET_PCT    = 0.009    # Target +0.9%
GEO_MAX_TOUCHES   = 2        # Skip zone si touchée > 2 fois
GEO_RSI_LOW       = 20
GEO_RSI_HIGH      = 65

# ── Boucles ───────────────────────────────────────────────────────────────────
FAST_LOOP_SECONDS = 30     # manage_pending + manage_positions
SLOW_LOOP_SECONDS = 300    # evaluate() — nouveau signal

# ── Sécurité ──────────────────────────────────────────────────────────────────
MONTHLY_LOSS_CAP_PCT = 0.15   # Pause si -15% dans le mois
