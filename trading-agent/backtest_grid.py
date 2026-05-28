"""
backtest_grid.py — Grid search infrastructure for short expert thesis tuning
============================================================================

Workflow:
  1. Define a param grid (Python dict OR YAML config)
  2. Load data once for the target period
  3. For each Cartesian product of params:
       - Monkey-patch module constants in backtest_short_expert
       - Run a backtest with same enabled theses
       - Compute metrics (N, WR, PF, expectancy, max DD, Sharpe, annualized %)
       - Append one row to a results CSV
  4. Resume capability: skip combos already in the CSV

Run examples:
    # Define a grid in a JSON file:
    python3 backtest_grid.py --config grid_t2.json

    # Or use built-in mini grid for smoke test:
    python3 backtest_grid.py --smoke
"""
import warnings; warnings.filterwarnings("ignore")
import argparse, csv, hashlib, itertools, json, os, sys, time
from collections import OrderedDict
from datetime import datetime
import numpy as np
import pandas as pd

import backtest_short_expert as bse


# ─── METRICS COMPUTATION ──────────────────────────────────────────────────────

def compute_metrics(result: dict) -> dict:
    """Computes standard backtest metrics from a backtest result dict.
    Result must have: trades (list of dict), equity_curve (list of (ts, equity)),
    final_equity, start_capital.
    """
    trades = result.get("trades", [])
    final = result["final_equity"]; start = result["start_capital"]
    equity = result.get("equity_curve", [])

    n = len(trades)
    if n == 0:
        return {
            "n_trades": 0, "win_rate": 0.0, "pf": 0.0, "expectancy": 0.0,
            "expectancy_pct": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
            "total_pnl": 0.0, "final_equity": final, "return_pct": 0.0,
            "max_dd_pct": 0.0, "sharpe": 0.0, "calmar": 0.0,
            "longest_loss_streak": 0,
        }

    df = pd.DataFrame(trades)
    df["win"] = df["pnl_net"] > 0
    wins = df[df["win"]]
    losses = df[~df["win"]]

    win_rate = float(wins.shape[0] / n)
    avg_win = float(wins["pnl_net"].mean()) if len(wins) else 0.0
    avg_loss = float(losses["pnl_net"].mean()) if len(losses) else 0.0
    gross_win = float(wins["pnl_net"].sum())
    gross_loss = float(abs(losses["pnl_net"].sum()))
    pf = (gross_win / gross_loss) if gross_loss > 1e-9 else float("inf") if gross_win > 0 else 0.0
    expectancy = float(df["pnl_net"].mean())
    expectancy_pct = expectancy / (start * bse.POS_PCT)    # rough per-position-size %
    total_pnl = float(df["pnl_net"].sum())
    return_pct = (final - start) / start

    # Max drawdown on equity curve
    if equity:
        eq_series = pd.Series([e for _, e in equity])
        peak = eq_series.cummax()
        dd_pct = (eq_series - peak) / peak
        max_dd_pct = float(dd_pct.min())    # negative number
    else:
        max_dd_pct = 0.0

    # Sharpe : daily aggregation
    sharpe = 0.0
    if equity and len(equity) > 30:
        eq_df = pd.DataFrame(equity, columns=["ts", "equity"]).set_index("ts")
        daily_eq = eq_df["equity"].resample("1D").last().dropna()
        if len(daily_eq) > 5:
            daily_ret = daily_eq.pct_change().dropna()
            if daily_ret.std() > 1e-9:
                sharpe = float(daily_ret.mean() / daily_ret.std() * np.sqrt(365))

    # Calmar: annualized return / |max DD|
    if equity and max_dd_pct < -1e-6:
        days = (equity[-1][0] - equity[0][0]).total_seconds() / 86400
        if days > 30:
            ann_return = (final / start) ** (365 / days) - 1
            calmar = float(ann_return / abs(max_dd_pct))
        else:
            calmar = 0.0
    else:
        calmar = 0.0

    # Longest loss streak
    streak = max_streak = 0
    for w in df["win"].values:
        if not w:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    out = {
        "n_trades": n,
        "win_rate": round(win_rate, 4),
        "pf": round(pf, 3) if pf != float("inf") else 99.0,
        "expectancy": round(expectancy, 3),
        "expectancy_pct": round(expectancy_pct, 5),
        "avg_win": round(avg_win, 3),
        "avg_loss": round(avg_loss, 3),
        "total_pnl": round(total_pnl, 2),
        "final_equity": round(final, 2),
        "return_pct": round(return_pct, 4),
        "max_dd_pct": round(max_dd_pct, 4),
        "sharpe": round(sharpe, 3),
        "calmar": round(calmar, 3),
        "longest_loss_streak": max_streak,
    }

    # ── Per-symbol splits ──
    if "symbol" in df.columns:
        for sym in df["symbol"].unique():
            sub = df[df["symbol"] == sym]
            sym_label = sym.replace("/", "")    # ETH/USD → ETHUSD
            wins_s = sub[sub["win"]]
            losses_s = sub[~sub["win"]]
            gw = float(wins_s["pnl_net"].sum())
            gl = float(abs(losses_s["pnl_net"].sum()))
            out[f"{sym_label}_n"]   = int(sub.shape[0])
            out[f"{sym_label}_wr"]  = round(wins_s.shape[0]/sub.shape[0], 3) if sub.shape[0] else 0
            out[f"{sym_label}_pnl"] = round(float(sub["pnl_net"].sum()), 2)
            out[f"{sym_label}_pf"]  = round(gw/gl, 3) if gl > 1e-9 else (99.0 if gw > 0 else 0.0)
            out[f"{sym_label}_exp"] = round(float(sub["pnl_net"].mean()), 3)

    # ── Per-year splits ──
    if "entry_time" in df.columns:
        df["_year"] = pd.to_datetime(df["entry_time"]).dt.year
        for yr in sorted(df["_year"].unique()):
            sub = df[df["_year"] == yr]
            wins_y = sub[sub["win"]]
            losses_y = sub[~sub["win"]]
            gw = float(wins_y["pnl_net"].sum())
            gl = float(abs(losses_y["pnl_net"].sum()))
            out[f"y{yr}_n"]   = int(sub.shape[0])
            out[f"y{yr}_wr"]  = round(wins_y.shape[0]/sub.shape[0], 3) if sub.shape[0] else 0
            out[f"y{yr}_pnl"] = round(float(sub["pnl_net"].sum()), 2)
            out[f"y{yr}_pf"]  = round(gw/gl, 3) if gl > 1e-9 else (99.0 if gw > 0 else 0.0)
            out[f"y{yr}_exp"] = round(float(sub["pnl_net"].mean()), 3)

    return out


# ─── PARAM INJECTION ──────────────────────────────────────────────────────────

def set_params(params: dict):
    """Monkey-patch module-level constants in backtest_short_expert.
    Any key matching a module attribute is overridden.
    Raises if a key doesn't correspond to a known constant (prevents typos).
    """
    for k, v in params.items():
        if not hasattr(bse, k):
            raise AttributeError(f"Unknown param '{k}' (not in backtest_short_expert module)")
        setattr(bse, k, v)


def snapshot_default_params(keys: list) -> dict:
    """Get current values of named module attributes (for resume / re-baseline)."""
    return {k: getattr(bse, k) for k in keys if hasattr(bse, k)}


# ─── GRID ITERATION ───────────────────────────────────────────────────────────

def cartesian_grid(grid: dict):
    """Yield each Cartesian product of a {param: [values]} dict."""
    keys = list(grid.keys())
    vals = [grid[k] for k in keys]
    for combo in itertools.product(*vals):
        yield OrderedDict(zip(keys, combo))


def params_hash(params: dict) -> str:
    """Stable hash of params (for resume dedup)."""
    s = json.dumps(params, sort_keys=True, default=str)
    return hashlib.sha1(s.encode()).hexdigest()[:12]


def load_existing_hashes(csv_path: str) -> set:
    """Read existing CSV and return set of already-computed param hashes."""
    if not os.path.isfile(csv_path): return set()
    try:
        df = pd.read_csv(csv_path)
        return set(df.get("params_hash", pd.Series(dtype=str)).astype(str).tolist())
    except Exception:
        return set()


# ─── ONE RUN ──────────────────────────────────────────────────────────────────

def run_one(params, symbols, start, end, enabled, data, df_btc, defaults,
            regime_cache_shared=None, expected_keys=None) -> dict:
    """Run one backtest with overridden params. Returns a flat result dict
    (params + metrics + meta) for CSV row append.

    If `expected_keys` is provided, missing keys in the row are filled with 0
    (ensures CSV schema consistency across runs where some symbols/years are absent).
    """
    t0 = time.time()
    try:
        set_params(defaults)
        set_params(params)
        result = bse.run_backtest(symbols, start, end, enabled,
                                  verbose=False, data=data, df_btc=df_btc,
                                  regime_cache_shared=regime_cache_shared)
        metrics = compute_metrics(result)
        ok, err = True, ""
    except Exception as e:
        metrics = compute_metrics({"trades": [], "equity_curve": [],
                                    "final_equity": 1000.0, "start_capital": 1000.0})
        ok, err = False, f"{type(e).__name__}: {e}"

    runtime = time.time() - t0

    row = {
        "params_hash": params_hash(params),
        "ok": ok,
        "error": err,
        "runtime_s": round(runtime, 1),
        "symbols": ",".join(symbols),
        "period": f"{start}_{end}",
        "enabled": ",".join(sorted(enabled)),
        **params,
        **metrics,
    }

    if expected_keys:
        for k in expected_keys:
            if k not in row:
                row[k] = 0
    return row


def make_expected_keys(symbols, start, end):
    """Build the union of all expected per-symbol + per-year metric keys."""
    keys = []
    for sym in symbols:
        sl = sym.replace("/", "")
        keys += [f"{sl}_n", f"{sl}_wr", f"{sl}_pnl", f"{sl}_pf", f"{sl}_exp"]
    y0 = int(start[:4]); y1 = int(end[:4])
    for yr in range(y0, y1 + 1):
        keys += [f"y{yr}_n", f"y{yr}_wr", f"y{yr}_pnl", f"y{yr}_pf", f"y{yr}_exp"]
    return keys


# ─── GRID SEARCH DRIVER ───────────────────────────────────────────────────────

def grid_search(grid, symbols, start, end, enabled, output_csv,
                resume=True, verbose=True):
    """Run grid search, appending one row per combo to output_csv.
    Resume skips combos whose hash already exists in the CSV."""
    combos = list(cartesian_grid(grid))
    n_total = len(combos)
    print(f"\nGrid search: {n_total} combos × ~3-5s each (est. ~{n_total*5/60:.0f}-{n_total*10/60:.0f} min)")
    print(f"Output: {output_csv}")

    # Pre-load data once
    print("\nPre-loading data...")
    data, df_btc = bse.load_data_for_period(symbols, start, end, verbose=verbose)

    # Get default param values (for reset between runs)
    defaults = snapshot_default_params(list(grid.keys()))
    print(f"\nDefaults snapshot: {defaults}")

    # Expected per-symbol + per-year metric keys (for stable CSV schema)
    expected_keys = make_expected_keys(symbols, start, end)
    print(f"Expected schema includes {len(expected_keys)} per-symbol/per-year keys")

    # Shared regime cache — persisted across runs since params don't affect regime
    regime_cache_shared = {}
    print("Regime cache shared across runs (params don't affect crypto_regime)")

    # Resume support
    existing = load_existing_hashes(output_csv) if resume else set()
    if existing:
        print(f"\nResuming: {len(existing)} combos already in CSV, will skip")

    # CSV header
    field_keys = None
    file_exists = os.path.isfile(output_csv)
    f = open(output_csv, "a", newline="")
    writer = None
    try:
        for i, combo in enumerate(combos, 1):
            h = params_hash(combo)
            if h in existing:
                continue

            if verbose:
                print(f"  [{i}/{n_total}] {dict(combo)} ...", end=" ", flush=True)
            row = run_one(combo, symbols, start, end, enabled, data, df_btc, defaults,
                          regime_cache_shared=regime_cache_shared,
                          expected_keys=expected_keys)

            if writer is None:
                field_keys = list(row.keys())
                writer = csv.DictWriter(f, fieldnames=field_keys)
                if not file_exists or os.path.getsize(output_csv) == 0:
                    writer.writeheader()
            writer.writerow(row)
            f.flush()

            if verbose:
                print(f"N={row['n_trades']:4d} WR={row['win_rate']*100:5.1f}% "
                      f"PF={row['pf']:5.2f} ret={row['return_pct']*100:+6.2f}% "
                      f"DD={row['max_dd_pct']*100:+5.2f}% in {row['runtime_s']:.1f}s")
    finally:
        f.close()

    print(f"\n✅ Grid search complete. Results in {output_csv}")


# ─── CLI ──────────────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    """Load grid config from JSON file. Schema:
    {
      "name": "T2 grid search",
      "symbols": ["ETH/USD", "SOL/USD"],
      "start": "2022-01-01",
      "end": "2024-12-31",
      "enabled": ["t2"],
      "output_csv": "grid_t2_results.csv",
      "grid": {
        "T2_PULLBACK_BUFFER_PCT": [0.001, 0.002, 0.005],
        "T2_TIMEOUT_MIN": [60, 90, 120],
        ...
      }
    }
    """
    with open(path) as f:
        return json.load(f)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", help="Path to grid config JSON")
    p.add_argument("--smoke", action="store_true",
                   help="Run a mini 2×2 grid on T2 over 2023 Q1 for smoke test")
    p.add_argument("--no-resume", action="store_true",
                   help="Disable resume (overwrite existing CSV)")
    p.add_argument("--analyze", help="CSV path to analyze (no grid run)")
    p.add_argument("--min-trades", type=int, default=30,
                   help="Minimum N trades for top-K filter (default 30)")
    args = p.parse_args()

    if args.analyze:
        analyze_grid_csv(args.analyze, min_trades=args.min_trades)
        return

    if args.smoke:
        cfg = {
            "name": "smoke",
            "symbols": ["ETH/USD", "SOL/USD"],
            "start": "2023-01-01",
            "end": "2023-03-31",
            "enabled": ["t2"],
            "output_csv": "grid_smoke_t2.csv",
            "grid": {
                "T2_PULLBACK_BUFFER_PCT": [0.001, 0.005],
                "T2_TIMEOUT_MIN":         [60, 90],
            }
        }
    elif args.config:
        cfg = load_config(args.config)
    else:
        print("Either --config or --smoke required.")
        sys.exit(1)

    print(f"\n{'='*72}\nGrid search: {cfg.get('name', 'unnamed')}\n{'='*72}")
    grid_search(
        cfg["grid"], cfg["symbols"], cfg["start"], cfg["end"],
        set(cfg["enabled"]), cfg["output_csv"],
        resume=not args.no_resume,
    )

    # Full analysis
    analyze_grid_csv(cfg["output_csv"], min_trades=30)


def analyze_grid_csv(csv_path: str, min_trades: int = 30, top_k: int = 10):
    """Standalone analyzer: top-K by 3 criteria, per-symbol + per-year splits."""
    df = pd.read_csv(csv_path)
    total = len(df)
    df_ok = df[df["ok"] == True] if "ok" in df.columns else df
    df_min = df_ok[df_ok["n_trades"] >= min_trades]
    print(f"\n{'='*80}")
    print(f"Grid analysis: {csv_path}")
    print(f"Total combos: {total} | Valid (ok): {len(df_ok)} | N≥{min_trades}: {len(df_min)}")
    print(f"{'='*80}")

    if len(df_min) == 0:
        print(f"\n⚠ No combo with N≥{min_trades} trades. Showing N≥1 instead.")
        df_min = df_ok[df_ok["n_trades"] >= 1]
        if len(df_min) == 0:
            print("⚠ No trades produced by any combo."); return

    # Find param columns: everything between 'enabled' and 'n_trades'
    cols = list(df.columns)
    try:
        i_enabled = cols.index("enabled")
        i_metric  = cols.index("n_trades")
        param_cols = cols[i_enabled+1:i_metric]
    except ValueError:
        param_cols = []

    base_cols = param_cols + ["n_trades", "win_rate", "pf", "expectancy",
                              "return_pct", "max_dd_pct", "sharpe", "calmar"]

    print(f"\n── TOP {top_k} BY EXPECTANCY (N ≥ {min_trades}) ──")
    print(df_min.nlargest(top_k, "expectancy")[base_cols].to_string(index=False))

    print(f"\n── TOP {top_k} BY PROFIT FACTOR (N ≥ {min_trades}) ──")
    print(df_min.nlargest(top_k, "pf")[base_cols].to_string(index=False))

    print(f"\n── TOP {top_k} BY |MAX DD| (lowest absolute DD, N ≥ {min_trades}) ──")
    # Note: max_dd_pct is negative; we want closest to 0 = smallest |dd|
    print(df_min.nlargest(top_k, "max_dd_pct")[base_cols].to_string(index=False))

    print(f"\n── TOP {top_k} BY SHARPE (N ≥ {min_trades}) ──")
    print(df_min.nlargest(top_k, "sharpe")[base_cols].to_string(index=False))

    # Per-symbol split for the best combo by expectancy
    best = df_min.nlargest(1, "expectancy").iloc[0]
    print(f"\n── PER-SYMBOL SPLIT (best by expectancy, params_hash={best['params_hash']}) ──")
    for sym in ("ETHUSD", "SOLUSD", "ETH/USD".replace("/",""), "SOL/USD".replace("/","")):
        n_key = f"{sym}_n"
        if n_key in best.index:
            print(f"  {sym}: N={int(best[n_key]):4d}  WR={best.get(f'{sym}_wr', 0)*100:5.1f}%  "
                  f"PF={best.get(f'{sym}_pf', 0):5.2f}  PnL=${best.get(f'{sym}_pnl', 0):+8.2f}  "
                  f"exp={best.get(f'{sym}_exp', 0):+5.2f}")

    # Per-year split
    print(f"\n── PER-YEAR SPLIT (best by expectancy) ──")
    for yr in (2022, 2023, 2024):
        n_key = f"y{yr}_n"
        if n_key in best.index:
            print(f"  {yr}: N={int(best[n_key]):4d}  WR={best.get(f'y{yr}_wr', 0)*100:5.1f}%  "
                  f"PF={best.get(f'y{yr}_pf', 0):5.2f}  PnL=${best.get(f'y{yr}_pnl', 0):+8.2f}  "
                  f"exp={best.get(f'y{yr}_exp', 0):+5.2f}")

    # Stability heuristic: combos profitable in ALL 3 years
    if all(f"y{yr}_pnl" in df_min.columns for yr in (2022, 2023, 2024)):
        all_yrs_pos = df_min[
            (df_min["y2022_pnl"] > 0) & (df_min["y2023_pnl"] > 0) & (df_min["y2024_pnl"] > 0)
        ]
        print(f"\n── COMBOS PROFITABLE IN ALL 3 YEARS (N ≥ {min_trades}): {len(all_yrs_pos)} ──")
        if len(all_yrs_pos) > 0:
            print(all_yrs_pos.nlargest(min(top_k, len(all_yrs_pos)), "expectancy")[base_cols].to_string(index=False))


if __name__ == "__main__":
    main()
