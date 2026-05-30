import warnings; warnings.filterwarnings("ignore")
import sys; sys.path.insert(0, ".")
import pandas as pd
import backtest_short_expert as bse
from backtest_short_expert import load_data_for_period, run_backtest
from backtest_grid import compute_metrics

SYMBOLS = ["ETH/USD", "SOL/USD"]
START = "2022-01-01"
END = "2024-12-31"
ENABLED = {"long_t1", "t1", "t2"}

print("Loading data once...")
data, df_btc = load_data_for_period(SYMBOLS, START, END, verbose=True)

VARIANTS = ["strict", "t1_neutral", "t1_trend_fallback"]
results = {}

for variant in VARIANTS:
    print(f"\n=== Running variant '{variant}' ===")
    bse.ROUTER_VARIANT = variant
    res = run_backtest(SYMBOLS, START, END, ENABLED, verbose=False,
                       data=data, df_btc=df_btc)
    metrics = compute_metrics(res)
    metrics["variant"] = variant
    metrics["final_equity"] = res["final_equity"]
    metrics["trades_obj"] = res["trades"]
    results[variant] = metrics
    print(f"  Final equity: ${res['final_equity']:.2f}  N trades: {metrics['n_trades']}")

# Build summary
rows = []
for v, m in results.items():
    rows.append({
        "variant": v,
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
print("\n══════════ ROUTER VARIANT COMPARISON ══════════")
print(df_summary.to_string(index=False))

# Deltas vs strict baseline
baseline = results["strict"]
print("\n══════════ DELTAS vs strict baseline ══════════")
for v in ["t1_neutral", "t1_trend_fallback"]:
    r = results[v]
    print(f"\n{v}:")
    print(f"  Equity Δ : ${r['final_equity']-baseline['final_equity']:+.2f}")
    print(f"  Return Δ : {(r['return_pct']-baseline['return_pct'])*100:+.2f}pp")
    print(f"  N trades Δ: {r['n_trades']-baseline['n_trades']:+d}")
    print(f"  Sharpe Δ : {r['sharpe']-baseline['sharpe']:+.2f}")
    print(f"  MaxDD Δ  : {(r['max_dd_pct']-baseline['max_dd_pct'])*100:+.2f}pp")

# By-thesis breakdown
print("\n══════════ BY-THESIS BREAKDOWN ══════════")
for v, m in results.items():
    trades = m["trades_obj"]
    if not trades:
        print(f"\n{v}: no trades"); continue
    tdf = pd.DataFrame(trades)
    tdf["win"] = tdf["pnl_net"] > 0
    grp = tdf.groupby(["expert_id", "thesis_id"]).agg(
        n=("pnl_net", "size"), wins=("win", "sum"),
        pnl=("pnl_net", "sum"), avg=("pnl_net", "mean"),
    )
    grp["WR%"] = (grp["wins"]/grp["n"]*100).round(1)
    grp["pnl"] = grp["pnl"].round(2)
    grp["avg"] = grp["avg"].round(2)
    print(f"\n--- {v} ---")
    print(grp.to_string())

# Per-year + per-symbol
print("\n══════════ PER-YEAR ══════════")
for v, m in results.items():
    print(f"\n--- {v} ---")
    for yr in (2022, 2023, 2024):
        n = m.get(f"y{yr}_n", 0)
        wr = m.get(f"y{yr}_wr", 0)*100
        pnl = m.get(f"y{yr}_pnl", 0)
        print(f"  {yr}: N={n:4d}  WR={wr:.1f}%  PnL=${pnl:+.2f}")

print("\n══════════ PER-SYMBOL ══════════")
for v, m in results.items():
    print(f"\n--- {v} ---")
    for sym in ("ETHUSD", "SOLUSD"):
        n = m.get(f"{sym}_n", 0)
        wr = m.get(f"{sym}_wr", 0)*100
        pnl = m.get(f"{sym}_pnl", 0)
        print(f"  {sym}: N={n:4d}  WR={wr:.1f}%  PnL=${pnl:+.2f}")
