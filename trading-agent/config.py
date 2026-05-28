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

# ── Multi-thesis short expert (Phase 4 paper port from backtest validation) ───
# Validated via 2022-2024 backtest grids (cf. vault doc short-expert-validation-2026-05-28).
# T2 trend-follow thesis: complement to T1 zone-bounce, fires only in trend_down_smooth.
# Sweet spot config validated by C3 grid: Sharpe 9.71 backtest, MaxDD -2.56%.

# T2 trend-follow constants (validated via T2 refinement grid Phase 2c)
T2_PULLBACK_BUFFER_PCT   = 0.012   # Best per refinement grid (vs 0.002 initial)
T2_STOP_BUFFER_PCT       = 0.003   # Best per refinement grid
T2_SWING_LOOKBACK_1H     = 12      # Best per refinement grid (vs 10 initial)
T2_RSI_5M_MAX            = 65      # Best per refinement grid
T2_TIMEOUT_MIN           = 120     # Best per refinement grid (vs 90 initial)
T2_TARGET_PCT_DEFAULT    = 0.015   # Fallback if regime decision.target_pct not provided
T2_INITIAL_CAP           = 0.7     # Per-thesis size cap (paper validation safety)
T2_COOLDOWN_MIN          = 60      # Anti-spam: skip T2 on same symbol within N min after last T2 trade
ENABLE_T2_SHORT          = True    # Master flag for T2 thesis (False = pure T1L+T1S, rollback)

# Router variant (validated via C3 exposure grid Phase 2 C3)
# - "strict"            : current behavior (T1 only in eligible states, no fallback). Sharpe 3.87.
# - "t1_trend_fallback" : T1 short fires as fallback in trend_down_smooth if T2 misses. Sharpe 9.71. SWEET SPOT.
# - "t1_neutral"        : T1 short fires also in neutral state. Sharpe 21 but unrealistic in live.
ROUTER_VARIANT = os.getenv("ROUTER_VARIANT", "t1_trend_fallback")

# T1S regime hard-block override (validated via C2 over-protection diagnostic)
# When True, T1 short ALSO fires in states normally hard-blocked by regime gate
# (btc_chaos, btc_trend_eth_weak, aligned_long_trend, btc_led_long).
# Shadow log showed these states have 64.4% WR + 0.479% avg expectancy on T1S.
T1S_INCLUDE_HARD_BLOCK = os.getenv("T1S_INCLUDE_HARD_BLOCK", "1") == "1"

# ── Boucles ───────────────────────────────────────────────────────────────────
FAST_LOOP_SECONDS = 30     # manage_pending + manage_positions
SLOW_LOOP_SECONDS = 300    # evaluate() — nouveau signal

# ── Sécurité ──────────────────────────────────────────────────────────────────
MONTHLY_LOSS_CAP_PCT = 0.15   # Pause si -15% dans le mois
DEBUG_MODE = False  # production gate enforcement (divergence + Pass 3b côté long)
