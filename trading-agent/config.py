"""
config.py — Jim Bot Geo V4 (PAPER, Kraken Futures)

Cleanup Phase E (audit #2): removed Bybit/Alpaca/OKX legacy constants,
USE_BROKER duplicate (consolidated to ACTIVE_BROKER), dead RSI constants.
"""
import os

# ── Broker sélectionné ────────────────────────────────────────────────────────
# kraken_paper (current) | kraken (Phase 2 LIVE)
ACTIVE_BROKER = os.getenv("ACTIVE_BROKER", "kraken_paper")

# ── Kraken Futures credentials (PAPER = demo-futures.kraken.com) ─────────────
KRAKEN_API_KEY    = os.getenv("KRAKEN_API_KEY", "")
KRAKEN_SECRET_KEY = os.getenv("KRAKEN_SECRET_KEY", "")
KRAKEN_PAPER      = os.getenv("KRAKEN_PAPER", "1") == "1"   # 1 = démo, 0 = live

# ── Capital ───────────────────────────────────────────────────────────────────
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "100.0"))
GEO_CAPITAL     = float(os.getenv("INITIAL_CAPITAL", "100.0"))

# ── Reset P&L date ────────────────────────────────────────────────────────────
GEO_RESET_DATE = "2026-04-06"

# ── Geo V4 — Paramètres stratégie ─────────────────────────────────────────────
GEO_SYMBOLS       = ["ETH/USD", "SOL/USD"]
GEO_ZONE_PCT      = 0.003    # Zone ±0.3% autour du pivot
GEO_MAX_SIM       = 2        # Max 2 positions simultanées
GEO_POS_PCT       = 0.50     # 50% du capital par position
GEO_TARGET_PCT    = 0.009    # Target +0.9% (mode "normal")
GEO_LOWVOL_TARGET_PCT = 0.005 # Target ~+0.5% (mode "lowvol" — fallback si crypto regime n'override pas)
GEO_LOWVOL_TIMEOUT_MIN = 60   # Time-stop agressif en low-vol (1h)
GEO_MAX_TOUCHES   = 2        # Skip zone si touchée > 2 fois
GEO_MIN_RR        = 1.2      # Min reward/risk depuis fill (anti-entry-drift)

# ── RSI bands (long vs short, asymétriques) ───────────────────────────────────
GEO_RSI_LOW        = 20      # Long mode : accepte rebound depuis oversold
GEO_RSI_HIGH       = 65      # Long mode : reject overbought
GEO_RSI_SHORT_LOW  = 35      # Short mode : reject oversold (too late to short)
GEO_RSI_SHORT_HIGH = 80      # Short mode : accepte rejection depuis overbought

# ── Crypto-native regime gate (PAPER VALIDATION ONLY) ─────────────────────────
# Replaces VIX/SPY macro filter with multi-TF crypto signals. Reversible.
USE_CRYPTO_REGIME_GATE        = True   # False = rollback to old VIX/SPY regime
CRYPTO_REGIME_MIN_CONFIDENCE  = 50     # below → no_trade regardless of state

# ── Boucles ───────────────────────────────────────────────────────────────────
FAST_LOOP_SECONDS = 30     # manage_pending + manage_positions
SLOW_LOOP_SECONDS = 300    # evaluate() — nouveau signal

# ── Sécurité ──────────────────────────────────────────────────────────────────
MONTHLY_LOSS_CAP_PCT = 0.15   # Pause si -15% dans le mois
DEBUG_MODE = False  # production gate enforcement (divergence + Pass 3b côté long)
