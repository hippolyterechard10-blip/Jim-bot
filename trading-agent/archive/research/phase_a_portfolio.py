import warnings; warnings.filterwarnings("ignore")
import sys; sys.path.insert(0, ".")
import pandas as pd
from backtest_short_expert import load_data_for_period, run_backtest
from backtest_grid import compute_metrics

SYMBOLS = ["ETH/USD", "SOL/USD"]
START = "2022-01-01"
END = "2024-12-31"

print("Loading data once...")
data, df_btc = load_data_for_period(SYMBOLS, START, END, verbose=True)

configs = {
    "P1": {"long_t1"},
    "P3": {"long_t1", "t2"},
    "P5": {"long_t1", "t1", "t2"},
}

results = {}
for cfg_id, enabled in configs.items():
    print(f"\n=== Running {cfg_id} (enabled={enabled}) ===")
    res = run_backtest(SYMBOLS, START, END, enabled, verbose=False,
                       data=data, df_btc=df_btc)
    metrics = compute_metrics(res)
    metrics["enabled"] = ",".join(sorted(enabled))
    metrics["final_equity"] = res["final_equity"]
    metrics["trades_obj"] = res["trades"]
    results[cfg_id] = metrics
    print(f"  Final equity: ${res['final_equity']:.2f}  N trades: {metrics['n_trades']}")

# Build comparison DataFrame
rows = []
for cfg_id, m in results.items():
    rows.append({
        "config": cfg_id,
        "enabled": m["enabled"],
        "N_trades": m["n_trades"],
        "WR%": round(m["win_rate"]*100, 1),
        "PF": m["pf"],
        "Return%": round(m["return_pct"]*100, 2),
        "MaxDD%": round(m["max_dd_pct"]*100, 2),
        "Sharpe": m["sharpe"],
        "Calmar": m["calmar"],
        "Expectancy": m["expectancy"],
        "FinalEq": round(m["final_equity"], 2),
    })
df_summary = pd.DataFrame(rows)
print("\n══════════ MAIN COMPARISON ══════════")
print(df_summary.to_string(index=False))

# Deltas
print("\n══════════ DELTAS ══════════")
print(f"T2 isolated contribution (P3 - P1): equity Δ=${results['P3']['final_equity']-results['P1']['final_equity']:+.2f}, return Δ={(results['P3']['return_pct']-results['P1']['return_pct'])*100:+.2f}pp")
print(f"T1 short addition (P5 - P3):        equity Δ=${results['P5']['final_equity']-results['P3']['final_equity']:+.2f}, return Δ={(results['P5']['return_pct']-results['P3']['return_pct'])*100:+.2f}pp")
print(f"Full multi-thesis uplift (P5 - P1): equity Δ=${results['P5']['final_equity']-results['P1']['final_equity']:+.2f}, return Δ={(results['P5']['return_pct']-results['P1']['return_pct'])*100:+.2f}pp")

# By-thesis breakdown for each config
print("\n══════════ BY-THESIS BREAKDOWN ══════════")
for cfg_id, m in results.items():
    trades = m["trades_obj"]
    if not trades:
        print(f"\n{cfg_id}: no trades"); continue
    tdf = pd.DataFrame(trades)
    tdf["win"] = tdf["pnl_net"] > 0
    grp = tdf.groupby(["expert_id", "thesis_id"]).agg(
        n=("pnl_net", "size"), wins=("win", "sum"),
        pnl=("pnl_net", "sum"), avg=("pnl_net", "mean"),
    )
    grp["WR%"] = (grp["wins"]/grp["n"]*100).round(1)
    grp["pnl"] = grp["pnl"].round(2)
    grp["avg"] = grp["avg"].round(2)
    print(f"\n--- {cfg_id} ---")
    print(grp.to_string())

# Per-year + per-symbol for each config
print("\n══════════ PER-YEAR ══════════")
for cfg_id, m in results.items():
    print(f"\n--- {cfg_id} ---")
    for yr in (2022, 2023, 2024):
        n = m.get(f"y{yr}_n", 0)
        wr = m.get(f"y{yr}_wr", 0)*100
        pnl = m.get(f"y{yr}_pnl", 0)
        print(f"  {yr}: N={n:4d}  WR={wr:.1f}%  PnL=${pnl:+.2f}")

print("\n══════════ PER-SYMBOL ══════════")
for cfg_id, m in results.items():
    print(f"\n--- {cfg_id} ---")
    for sym in ("ETHUSD", "SOLUSD"):
        n = m.get(f"{sym}_n", 0)
        wr = m.get(f"{sym}_wr", 0)*100
        pnl = m.get(f"{sym}_pnl", 0)
        print(f"  {sym}: N={n:4d}  WR={wr:.1f}%  PnL=${pnl:+.2f}")
