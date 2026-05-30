"""phase_c1_diagnostic.py — C1 rejection logging + C2 would-be P&L analysis.

Run:
    python3 phase_c1_diagnostic.py
"""
import warnings; warnings.filterwarnings("ignore")
import sys; sys.path.insert(0, ".")
import bisect
import pandas as pd
import numpy as np
import backtest_short_expert as bse
from backtest_short_expert import load_data_for_period, run_backtest

SYMBOLS = ["ETH/USD", "SOL/USD"]
START = "2022-01-01"
END = "2024-12-31"
ENABLED = {"long_t1", "t1", "t2", "t3", "t4"}

print("Loading data once...")
data, df_btc = load_data_for_period(SYMBOLS, START, END, verbose=True)

bse.ROUTER_VARIANT = "strict"
print("\nRunning instrumented backtest (P5 strict, 2022-2024 ETH+SOL)...")
res = run_backtest(SYMBOLS, START, END, ENABLED, verbose=False,
                   data=data, df_btc=df_btc)
rejection_log = res.get("rejection_log", [])
print(f"  Captured {len(rejection_log)} shadow signals over the run.")
print(f"  Actual closed trades: {len(res['trades'])}")
print(f"  Final equity: ${res['final_equity']:.2f}")

# Build per-symbol 5m arrays for would-be P&L simulation
sym_arrays = {}
for sym in SYMBOLS:
    d5 = data[sym]["5min"]
    sym_arrays[sym] = {
        "idx": d5.index,
        "pos": {ts: j for j, ts in enumerate(d5.index)},
        "h": d5["high"].values,
        "l": d5["low"].values,
        "c": d5["close"].values,
    }


def simulate_would_be(row, arr_sym, default_timeout_min=240):
    """Simulate a single rejected entry over the next N bars.
    Returns realized P&L per unit (pnl_pct), exit reason, hold bars."""
    pos_i = arr_sym["pos"].get(row["ts"])
    if pos_i is None:
        return None
    timeout_min = row.get("timeout_min") or default_timeout_min
    timeout_bars = int(timeout_min // 5)
    entry = float(row["entry"])
    stop = float(row["stop"])
    target = float(row["target"])
    side = row["side"]
    fill_type = row.get("fill_type", "limit")
    SLIPPAGE = bse.SLIPPAGE_PCT
    FEE_MAKER = bse.FEE_MAKER
    FEE_TAKER = bse.FEE_TAKER

    # Apply entry slippage for market fills
    if fill_type == "market":
        entry_eff = entry * (1 + SLIPPAGE) if side == "long" else entry * (1 - SLIPPAGE)
        fee_entry_rate = FEE_TAKER
    else:
        entry_eff = entry
        fee_entry_rate = FEE_MAKER

    end_i = min(pos_i + 1 + timeout_bars, len(arr_sym["h"]))
    for ni in range(pos_i + 1, end_i):
        hi = arr_sym["h"][ni]
        lo = arr_sym["l"][ni]
        if side == "long":
            sh, th = lo <= stop, hi >= target
        else:
            sh, th = hi >= stop, lo <= target
        ep, er = None, None
        if sh and th:
            ep, er = (stop, "stop") if (ni % 2 == 0) else (target, "target")
        elif sh:
            ep, er = stop, "stop"
        elif th:
            ep, er = target, "target"
        if ep is not None:
            if er == "target":
                ep_adj, fee_exit_rate = ep, FEE_MAKER
            else:
                ep_adj = ep * (1 - SLIPPAGE) if side == "long" else ep * (1 + SLIPPAGE)
                fee_exit_rate = FEE_TAKER
            sign = 1 if side == "long" else -1
            pnl_pct = sign * (ep_adj - entry_eff) / entry_eff - fee_entry_rate - fee_exit_rate
            return {"pnl_pct": pnl_pct, "exit_reason": er, "hold_bars": ni - pos_i}
    # Timeout exit at last close
    if end_i > pos_i + 1:
        cl_last = arr_sym["c"][end_i - 1]
        ep_adj = cl_last * (1 - SLIPPAGE) if side == "long" else cl_last * (1 + SLIPPAGE)
        sign = 1 if side == "long" else -1
        pnl_pct = sign * (ep_adj - entry_eff) / entry_eff - fee_entry_rate - FEE_TAKER
        return {"pnl_pct": pnl_pct, "exit_reason": "timeout", "hold_bars": end_i - 1 - pos_i}
    return None


# Simulate each rejection
print("\nSimulating would-be P&L on each shadow signal...")
results = []
for row in rejection_log:
    arr_sym = sym_arrays[row["symbol"]]
    out = simulate_would_be(row, arr_sym)
    if out is None:
        continue
    results.append({**row, **out})

df = pd.DataFrame(results)
if df.empty:
    print("No results to analyze.")
    sys.exit(1)

df["win"] = df["pnl_pct"] > 0

# ── C2 aggregation ──────────────────────────────────────────────────────────────
print(f"\n{'='*72}")
print(f"C2 ANALYSIS — {len(df)} simulated would-be trades")
print(f"{'='*72}")

print("\n══ A. Top rejection reasons (by N) ══")
reason_counts = df.groupby(["thesis", "reason"]).size().sort_values(ascending=False)
print(reason_counts.to_string())

print("\n══ B. Would-be expectancy by rejection reason (excl. 'taken') ══")
df_rej = df[df["reason"] != "taken"]
agg = df_rej.groupby(["thesis", "reason"]).agg(
    N=("pnl_pct", "size"),
    WR=("win", "mean"),
    avg_pnl_pct=("pnl_pct", "mean"),
    sum_pnl_pct=("pnl_pct", "sum"),
)
agg["WR"] = (agg["WR"] * 100).round(1)
agg["avg_pnl_pct"] = (agg["avg_pnl_pct"] * 100).round(3)
agg["sum_pnl_pct"] = (agg["sum_pnl_pct"] * 100).round(2)
print(agg.to_string())

print("\n══ C. Would-be expectancy by state (rejected signals only) ══")
agg_st = df_rej.groupby(["thesis", "state"]).agg(
    N=("pnl_pct", "size"),
    WR=("win", "mean"),
    avg_pnl_pct=("pnl_pct", "mean"),
    sum_pnl_pct=("pnl_pct", "sum"),
)
agg_st["WR"] = (agg_st["WR"] * 100).round(1)
agg_st["avg_pnl_pct"] = (agg_st["avg_pnl_pct"] * 100).round(3)
agg_st["sum_pnl_pct"] = (agg_st["sum_pnl_pct"] * 100).round(2)
print(agg_st.to_string())

print("\n══ D. Confidence buckets (rejected signals, all theses) ══")
df_rej_copy = df_rej.copy()
df_rej_copy["conf_bucket"] = pd.cut(
    df_rej_copy["conf"],
    bins=[0, 30, 40, 50, 60, 70, 80, 100],
    labels=["0-30", "30-40", "40-50", "50-60", "60-70", "70-80", "80-100"],
)
agg_c = df_rej_copy.groupby(["thesis", "conf_bucket"], observed=True).agg(
    N=("pnl_pct", "size"),
    WR=("win", "mean"),
    avg_pnl_pct=("pnl_pct", "mean"),
)
agg_c["WR"] = (agg_c["WR"] * 100).round(1)
agg_c["avg_pnl_pct"] = (agg_c["avg_pnl_pct"] * 100).round(3)
print(agg_c.to_string())

print("\n══ E. Recoverable trades estimate (positive expectancy buckets only) ══")
df_rej_copy["bucket_key"] = (
    df_rej_copy["thesis"] + "_"
    + df_rej_copy["conf_bucket"].astype(str) + "_"
    + df_rej_copy["state"]
)
bucket_stats = df_rej_copy.groupby("bucket_key").agg(
    N=("pnl_pct", "size"),
    avg_pnl_pct=("pnl_pct", "mean"),
    sum_pnl_pct=("pnl_pct", "sum"),
)
recoverable = bucket_stats[bucket_stats["avg_pnl_pct"] > 0]
print(f"  Recoverable bucket count: {len(recoverable)}")
print(f"  Total recoverable trades (sum N): {int(recoverable['N'].sum())}")
total_pct = recoverable["sum_pnl_pct"].sum()
print(f"  Sum of would-be pnl_pct: {total_pct * 100:.2f}%")
print(
    f"  Conservative impact estimate (50% pos_pct, no compounding): "
    f"+{total_pct * 0.5 * 100:.1f}% over 3 years = "
    f"+{((1 + total_pct * 0.5) ** (1/3) - 1) * 100:.2f}%/year"
)

print("\n══ F. Summary: per-thesis taken vs rejected ══")
tot = df.groupby(["thesis", "reason"]).size().unstack(fill_value=0)
print(tot.to_string())

# ── Final 3-line summary ─────────────────────────────────────────────────────────
total_rejected = len(df[df["reason"] != "taken"])
top3 = df[df["reason"] != "taken"].groupby("reason").size().sort_values(ascending=False).head(3)
print(f"\n{'='*72}")
print(f"SUMMARY: Total N rejected shadow signals (excl taken): {total_rejected}")
print(f"Top 3 rejection reasons: " + " | ".join(f"{r}={n}" for r, n in top3.items()))
print(
    "Most actionable sections for C3: "
    "B (expectancy per rejection reason — look for positive avg_pnl_pct blocks), "
    "C (by state — identifies which states to relax gate for), "
    "E (recoverable estimate — quantifies the prize)."
)
