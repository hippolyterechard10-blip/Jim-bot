"""
backtest_geo_realistic.py — A vs B, 2022-2023, coûts réels, SANS compound
==============================================================================
Objectif : répondre à « si je mets $X, je gagne combien par an net ? »

Variantes (toutes LONG+SHORT) :
    A  R:R≥1.2  timeout 240m  RSI div lookback 3 bars   (code actuel)
    B  R:R≥1.5  timeout  90m  RSI div lookback 8 bars   (améliorations)

Notional fixe = 50 % du capital de départ (pas de compound per-trade).
Le notional est remis à 50 % du capital au 1er janvier de chaque année
(= rebalance annuel simple).

Coûts :
  - Fees entry maker 0.02 % | exit TP 0.02 % | exit SL 0.05 %
  - Slippage +0.05 % sur SL uniquement (taker traverse le book)
  - Taxes 30 % PFU (crypto) et 45 % BNC pro, calculées par année civile
"""
import glob
import os
import warnings
from collections import defaultdict

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ── CONFIG ────────────────────────────────────────────────────────────────────
POS_PCT      = 0.50
MAX_SIM      = 2
ZONE_PCT     = 0.003
TARGET_PCT   = 0.009
MAX_TOUCHES  = 2
RSI_LOW_L    = 20
RSI_HIGH_L   = 65
RSI_LOW_S    = 35
RSI_HIGH_S   = 80

FEE_ENTRY    = 0.0002
FEE_EXIT_TP  = 0.0002
FEE_EXIT_SL  = 0.0005
SLIPPAGE_SL  = 0.0010    # 0.10 % — market SL sur perp crypto en mouvement normal

# Réalisme des fills limit (GTC dans le carnet Kraken) :
#  - Pénétration 0 : un GTC touché par la book fill (comportement normal)
#  - FILL_RATE < 1 : simule les fills ratés (queue, latence, bars 5m agrégées
#    qui touchent sans que notre ordre soit traité en time-priority)
FILL_PENETRATION = 0.0     # pas de pénétration requise (GTC normal)
FILL_RATE        = 0.85    # 85 % des setups touchés fillent réellement

TAX_PFU      = 0.30
TAX_BNC_PRO  = 0.45

# Seed pour la reproductibilité du FILL_RATE stochastique
_RNG = np.random.default_rng(42)

MIN_NOTIONAL = {"ETH": 40.0, "SOL": 200.0}

CACHE_DIR = "binance_us_cache"

YEAR_START = "2022-01-01"
YEAR_END   = "2024-01-01"


# ── DATA ──────────────────────────────────────────────────────────────────────

def load_symbol(prefix):
    files = sorted(glob.glob(os.path.join(CACHE_DIR, f"{prefix}_5m_*.parquet")))
    dfs = [pd.read_parquet(f) for f in files]
    df = pd.concat(dfs)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def resample_ohlcv(df_5m, rule):
    return df_5m.resample(rule).agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna()


# ── HELPERS STRATÉGIE ─────────────────────────────────────────────────────────

def _rsi(closes, period=14):
    arr = np.asarray(closes, dtype=float)
    if len(arr) < period + 1:
        return 50.0
    d = np.diff(arr)
    g = np.where(d > 0, d, 0.0)
    l = np.where(d < 0, -d, 0.0)
    ag = g[-period:].mean()
    al = l[-period:].mean()
    if al == 0:
        return 100.0
    return round(100.0 - 100.0 / (1.0 + ag / al), 2)


def swing_lows(lows, highs):
    n = len(lows); mask = np.zeros(n, dtype=bool)
    if n < 5: return mask
    cond = ((lows[2:-2] < lows[:-4]) & (lows[2:-2] < lows[1:-3]) &
            (lows[2:-2] < lows[3:-1]) & (lows[2:-2] < lows[4:]))
    mask[2:-2] = cond
    return mask


def swing_highs(highs):
    n = len(highs); mask = np.zeros(n, dtype=bool)
    if n < 5: return mask
    cond = ((highs[2:-2] > highs[:-4]) & (highs[2:-2] > highs[1:-3]) &
            (highs[2:-2] > highs[3:-1]) & (highs[2:-2] > highs[4:]))
    mask[2:-2] = cond
    return mask


def cluster_zones(levels, current, side):
    if len(levels) == 0:
        return []
    sorted_lvls = np.sort(levels)
    clusters = [[sorted_lvls[0]]]
    for lvl in sorted_lvls[1:]:
        base = clusters[-1][0]
        if (lvl - base) / base < ZONE_PCT * 2:
            clusters[-1].append(lvl)
        else:
            clusters.append([lvl])
    zones = []
    for c in clusters:
        center = sum(c) / len(c)
        wick   = min(c) if side == "long" else max(c)
        if side == "long" and center < current * 0.999:
            zones.append({"center": center, "wick": wick, "tests": len(c)})
        elif side == "short" and center > current * 1.001:
            zones.append({"center": center, "wick": wick, "tests": len(c)})
    zones.sort(key=lambda x: x["center"], reverse=(side == "long"))
    return zones


def dyn_stop_long(lows, entry, wick):
    floor = entry * 0.992
    cand  = lows[-8:].min() * 0.999 if len(lows) >= 8 else wick * 0.999
    if floor <= cand < entry: return cand
    zs = wick * 0.999
    if floor <= zs < entry: return zs
    return entry * 0.997


def dyn_stop_short(highs, entry, wick):
    ceiling = entry * 1.008
    cand    = highs[-8:].max() * 1.001 if len(highs) >= 8 else wick * 1.001
    if entry < cand <= ceiling: return cand
    zs = wick * 1.001
    if entry < zs <= ceiling: return zs
    return entry * 1.003


def bias(highs, lows):
    if len(highs) < 5: return "range"
    if highs[-1] > highs[-4] and lows[-1] > lows[-4]: return "uptrend"
    if highs[-1] < highs[-4] and lows[-1] < lows[-4]: return "downtrend"
    return "range"


def zone_key(center):
    mag = max(1, int(round(-np.log10(center * 0.001))))
    return round(center, mag)


# ── BACKTEST — notional fixe par année ───────────────────────────────────────

def run_backtest(df_5m, df_15m, df_1h, sym_label, params, start_capital):
    """Retourne (trades, pnl_per_year_dict) avec notional fixe = 50 % du capital
    de DÉPART de chaque année calendaire."""
    opens_5m  = df_5m["open"].values
    highs_5m  = df_5m["high"].values
    lows_5m   = df_5m["low"].values
    closes_5m = df_5m["close"].values
    vols_5m   = df_5m["volume"].values
    ts_5m     = df_5m.index.values.astype("datetime64[ns]")

    ts_15     = df_15m.index.values.astype("datetime64[ns]")
    highs_15  = df_15m["high"].values
    lows_15   = df_15m["low"].values
    pos15     = np.searchsorted(ts_15, ts_5m, side="right") - 1

    ts_1h     = df_1h.index.values.astype("datetime64[ns]")
    highs_1h  = df_1h["high"].values
    lows_1h   = df_1h["low"].values
    pos1h     = np.searchsorted(ts_1h, ts_5m, side="right") - 1

    sl_mask   = swing_lows(lows_15, highs_15)
    sh_mask   = swing_highs(highs_15)

    min_notional = MIN_NOTIONAL[sym_label]
    n = len(df_5m)

    # Capital cumulé (rebalancé chaque 1er janvier)
    capital    = start_capital
    year_start_capital = start_capital
    current_year = pd.Timestamp(ts_5m[0]).year
    notional   = year_start_capital * POS_PCT

    trades   = []
    open_pos = {}
    touches  = defaultdict(int)

    rr_min       = params["rr_min"]
    timeout_bars = params["timeout_bars"]
    div_lookback = params["div_lookback"]

    for i in range(60, n - 1):
        # Rebalance annuel
        y = pd.Timestamp(ts_5m[i]).year
        if y != current_year:
            current_year = y
            year_start_capital = capital
            notional = year_start_capital * POS_PCT

        hi = highs_5m[i + 1]; lo = lows_5m[i + 1]; cls = closes_5m[i + 1]

        # ── sorties ─────────────────────────────────────────────────────
        for zk in list(open_pos.keys()):
            pos = open_pos[zk]
            ep = er = None
            if pos["side"] == "long":
                if lo <= pos["stop"]:
                    ep, er = pos["stop"] * (1 - SLIPPAGE_SL), "stop"
                elif hi >= pos["target"]:
                    ep, er = pos["target"], "target"
                elif (i - pos["bar"]) >= timeout_bars:
                    ep, er = cls, "timeout"
            else:
                if hi >= pos["stop"]:
                    ep, er = pos["stop"] * (1 + SLIPPAGE_SL), "stop"
                elif lo <= pos["target"]:
                    ep, er = pos["target"], "target"
                elif (i - pos["bar"]) >= timeout_bars:
                    ep, er = cls, "timeout"

            if ep is not None:
                if pos["side"] == "long":
                    gross = (ep - pos["entry"]) * pos["qty"]
                else:
                    gross = (pos["entry"] - ep) * pos["qty"]
                exit_fee_rate = FEE_EXIT_TP if er == "target" else FEE_EXIT_SL
                fees = (pos["entry"] * FEE_ENTRY + ep * exit_fee_rate) * pos["qty"]
                pnl  = gross - fees
                capital += pnl
                trades.append({
                    "t": pd.Timestamp(ts_5m[i]), "side": pos["side"],
                    "entry": pos["entry"], "exit": ep,
                    "gross": gross, "fees": fees, "pnl": pnl, "reason": er,
                })
                del open_pos[zk]

        if notional < min_notional or len(open_pos) >= MAX_SIM:
            continue

        p15 = pos15[i]; p1h = pos1h[i]
        if p15 < 20 or p1h < 10:
            continue

        s15  = max(0, p15 - 100)
        l15w = lows_15[s15:p15 + 1]
        h15w = highs_15[s15:p15 + 1]
        slm  = sl_mask[s15:p15 + 1]
        shm  = sh_mask[s15:p15 + 1]

        b = bias(highs_1h[p1h - 4:p1h + 1], lows_1h[p1h - 4:p1h + 1]) \
            if p1h >= 4 else "range"

        s5 = max(0, i - 29)
        c5 = closes_5m[s5:i + 1]; h5 = highs_5m[s5:i + 1]
        l5 = lows_5m[s5:i + 1];   v5 = vols_5m[s5:i + 1]
        if len(c5) < max(15, div_lookback + 2):
            continue
        avg_v = v5[-20:].mean() if len(v5) >= 20 else v5.mean()
        if avg_v > 0 and v5[-1] < avg_v * 0.3:
            continue

        current = closes_5m[i]
        rsi_now = _rsi(c5, 14)

        # LONG — limit buy à zone_high, fill réaliste
        if b != "downtrend":
            for zone in cluster_zones(l15w[slm], current, "long"):
                zk = ("L", zone_key(zone["center"]))
                if zk in open_pos or touches[zk] >= MAX_TOUCHES: continue
                dist = (current - zone["center"]) / current
                if not (0.001 <= dist <= 0.020): continue
                if not (RSI_LOW_L <= rsi_now <= RSI_HIGH_L): continue
                rsi_prev = _rsi(c5[:-div_lookback], 14)
                div = c5[-1] < c5[-1 - div_lookback] and rsi_now > rsi_prev
                if not div and not (30 <= rsi_now <= 55): continue
                zone_high = zone["center"] * (1 + ZONE_PCT)
                zone_low  = zone["center"] * (1 - ZONE_PCT)
                if not (l5[-8:] <= zone_high).any() or c5[-1] <= zone_low:
                    continue
                # Limit price = ce que le bot live place : zone_high
                limit_price = zone_high
                stop   = dyn_stop_long(l5, zone["center"], zone["wick"])
                target = limit_price * (1 + TARGET_PCT)
                risk   = abs(limit_price - stop)
                rew    = abs(target - limit_price)
                if risk <= 0 or rew / risk < rr_min: continue
                # Pénétration requise : next_bar.low doit casser le limit d'au moins 5 bps
                if lo > limit_price * (1 - FILL_PENETRATION): continue
                # Probabiliste : 65 % fill rate (queue Kraken + latence)
                if _RNG.random() > FILL_RATE: continue
                # Fill price = limit (ou open si gap favorable en dessous)
                fill = min(opens_5m[i + 1], limit_price)
                qty  = notional / fill
                touches[zk] += 1
                # Check same-bar stop immédiat après fill
                if lo <= stop:
                    exit_p = stop * (1 - SLIPPAGE_SL)
                    gross  = (exit_p - fill) * qty
                    fees   = (fill * FEE_ENTRY + exit_p * FEE_EXIT_SL) * qty
                    pnl    = gross - fees
                    capital += pnl
                    trades.append({"t": pd.Timestamp(ts_5m[i]), "side": "long",
                                   "entry": fill, "exit": exit_p,
                                   "gross": gross, "fees": fees, "pnl": pnl,
                                   "reason": "stop_same_bar"})
                else:
                    open_pos[zk] = {"side": "long", "entry": fill, "stop": stop,
                                    "target": target, "qty": qty, "bar": i + 1}
                if len(open_pos) >= MAX_SIM: break

        # SHORT — limit sell à zone_low, fill réaliste
        if b != "uptrend" and len(open_pos) < MAX_SIM:
            for zone in cluster_zones(h15w[shm], current, "short"):
                zk = ("S", zone_key(zone["center"]))
                if zk in open_pos or touches[zk] >= MAX_TOUCHES: continue
                dist = (zone["center"] - current) / current
                if not (0.001 <= dist <= 0.020): continue
                if not (RSI_LOW_S <= rsi_now <= RSI_HIGH_S): continue
                rsi_prev = _rsi(c5[:-div_lookback], 14)
                div = c5[-1] > c5[-1 - div_lookback] and rsi_now < rsi_prev
                if not div and not (45 <= rsi_now <= 70): continue
                zone_high = zone["center"] * (1 + ZONE_PCT)
                zone_low  = zone["center"] * (1 - ZONE_PCT)
                if not (h5[-8:] >= zone_low).any() or c5[-1] >= zone_high:
                    continue
                limit_price = zone_low
                stop   = dyn_stop_short(h5, zone["center"], zone["wick"])
                target = limit_price * (1 - TARGET_PCT)
                risk   = abs(stop - limit_price)
                rew    = abs(limit_price - target)
                if risk <= 0 or rew / risk < rr_min: continue
                if hi < limit_price * (1 + FILL_PENETRATION): continue
                if _RNG.random() > FILL_RATE: continue
                fill = max(opens_5m[i + 1], limit_price)
                qty  = notional / fill
                touches[zk] += 1
                if hi >= stop:
                    exit_p = stop * (1 + SLIPPAGE_SL)
                    gross  = (fill - exit_p) * qty
                    fees   = (fill * FEE_ENTRY + exit_p * FEE_EXIT_SL) * qty
                    pnl    = gross - fees
                    capital += pnl
                    trades.append({"t": pd.Timestamp(ts_5m[i]), "side": "short",
                                   "entry": fill, "exit": exit_p,
                                   "gross": gross, "fees": fees, "pnl": pnl,
                                   "reason": "stop_same_bar"})
                else:
                    open_pos[zk] = {"side": "short", "entry": fill, "stop": stop,
                                    "target": target, "qty": qty, "bar": i + 1}
                if len(open_pos) >= MAX_SIM: break

    # Ferme à la fin
    last = closes_5m[-1]
    for zk, pos in open_pos.items():
        if pos["side"] == "long":
            gross = (last - pos["entry"]) * pos["qty"]
        else:
            gross = (pos["entry"] - last) * pos["qty"]
        fees = (pos["entry"] * FEE_ENTRY + last * FEE_EXIT_TP) * pos["qty"]
        pnl  = gross - fees
        capital += pnl
        trades.append({"t": pd.Timestamp(ts_5m[-1]), "side": pos["side"],
                       "entry": pos["entry"], "exit": last,
                       "gross": gross, "fees": fees, "pnl": pnl, "reason": "end"})

    return trades, capital


# ── ANALYSE ───────────────────────────────────────────────────────────────────

def year_breakdown(trades):
    """Retourne DataFrame avec PnL / année."""
    if not trades:
        return pd.DataFrame()
    df = pd.DataFrame(trades)
    df["year"] = pd.to_datetime(df["t"]).dt.year
    return df.groupby("year").agg(
        n=("pnl", "count"),
        wr=("pnl", lambda x: (x > 0).mean() * 100),
        pnl=("pnl", "sum"),
        fees=("fees", "sum"),
    )


def apply_tax_yearly(yearly_pnl, tax_rate):
    return sum(pnl * (1 - tax_rate) if pnl > 0 else pnl for pnl in yearly_pnl)


def main():
    print("═" * 90)
    print("  GEO A/B — 2022+2023, réaliste, SANS compound (notional fixe par année)")
    print("═" * 90)
    print("  A (actuel)   : R:R≥1.2 | timeout 240m | RSI div lookback 3 bars")
    print("  B (amélioré) : R:R≥1.5 | timeout  90m | RSI div lookback 8 bars")
    print(f"  Fees         : entry {FEE_ENTRY*100:.2f}% | exit TP {FEE_EXIT_TP*100:.2f}% / "
          f"SL {FEE_EXIT_SL*100:.2f}% + slippage {SLIPPAGE_SL*100:.2f}% sur SL")
    print(f"  Taxes        : {TAX_PFU*100:.0f}% PFU (crypto) | {TAX_BNC_PRO*100:.0f}% BNC pro")
    print(f"  Min lot      : ETH ${MIN_NOTIONAL['ETH']} / SOL ${MIN_NOTIONAL['SOL']}")
    print("═" * 90)

    variants = {
        "A": {"rr_min": 1.2, "timeout_bars": 48, "div_lookback": 3},
        "B": {"rr_min": 1.5, "timeout_bars": 18, "div_lookback": 8},
    }
    capitals = [500, 1000, 2500, 5000, 10000, 25000]

    loaded = {}
    for prefix, label in [("ETHUSD", "ETH"), ("SOLUSD", "SOL")]:
        print(f"\n  Chargement {label}…")
        df_5m = load_symbol(prefix)
        df_5m = df_5m[(df_5m.index >= YEAR_START) & (df_5m.index < YEAR_END)]
        df_15 = resample_ohlcv(df_5m, "15min")
        df_1h = resample_ohlcv(df_5m, "1h")
        loaded[label] = (df_5m, df_15, df_1h)

    all_rows = []

    # Split cap 50/50 ETH/SOL (cas réel Jim-Bot)
    print(f"\n{'─'*90}")
    print("  Résultats en $ (capital total split 50/50 ETH/SOL) — 2 ans cumulés")
    print(f"{'─'*90}")
    print(f"  {'Var':<4} {'Cap tot':>9} {'Trades':>7} {'WR%':>5} "
          f"{'Gross $':>10} {'Fees $':>9} {'Net $ brut':>12} "
          f"{'Net PFU':>10} {'Net BNC':>10} {'/an PFU':>10}")
    print("  " + "─" * 88)

    for v_name, v_params in variants.items():
        for cap in capitals:
            half = cap / 2
            tr_eth, _ = run_backtest(*loaded["ETH"], "ETH", v_params, half)
            tr_sol, _ = run_backtest(*loaded["SOL"], "SOL", v_params, half)
            all_trades = tr_eth + tr_sol
            if not all_trades:
                continue

            df = pd.DataFrame(all_trades)
            df["year"] = pd.to_datetime(df["t"]).dt.year
            yr = df.groupby("year")["pnl"].sum().to_dict()
            yearly_pnl = list(yr.values())
            n       = len(all_trades)
            wr      = (df["pnl"] > 0).mean() * 100
            gross   = df["gross"].sum()
            fees    = df["fees"].sum()
            net_brut= df["pnl"].sum()
            net_pfu = apply_tax_yearly(yearly_pnl, TAX_PFU)
            net_bnc = apply_tax_yearly(yearly_pnl, TAX_BNC_PRO)
            per_year_pfu = net_pfu / 2  # 2 années
            print(f"  {v_name:<4} ${cap:>7,} {n:>7,} {wr:>5.1f} "
                  f"{gross:>+10,.0f} {fees:>9,.0f} {net_brut:>+12,.0f} "
                  f"{net_pfu:>+10,.0f} {net_bnc:>+10,.0f} {per_year_pfu:>+10,.0f}")

            for y, pnl in yr.items():
                all_rows.append({
                    "variant": v_name, "cap_total": cap, "year": y,
                    "n": len(df[df["year"] == y]),
                    "pnl_gross": float(df[df["year"] == y]["gross"].sum()),
                    "fees": float(df[df["year"] == y]["fees"].sum()),
                    "pnl_net": pnl,
                    "pnl_net_pfu": pnl * (1 - TAX_PFU) if pnl > 0 else pnl,
                    "pnl_net_bnc": pnl * (1 - TAX_BNC_PRO) if pnl > 0 else pnl,
                })

    # ── Par année pour voir 2022 vs 2023 ──────────────────────────────
    print(f"\n{'─'*90}")
    print("  Détail année par année (variante B, capital total split 50/50)")
    print(f"{'─'*90}")
    print(f"  {'Cap':>7} {'Année':>6} {'Trades':>7} {'Gross $':>10} "
          f"{'Fees $':>9} {'Net brut':>10} {'Net PFU':>10}")
    print("  " + "─" * 78)
    df_all = pd.DataFrame(all_rows)
    for cap in capitals:
        rows = df_all[(df_all["variant"] == "B") & (df_all["cap_total"] == cap)].sort_values("year")
        for _, row in rows.iterrows():
            print(f"  ${row['cap_total']:>5,} {int(row['year']):>6} {int(row['n']):>7,} "
                  f"{row['pnl_gross']:>+10,.0f} {row['fees']:>9,.0f} "
                  f"{row['pnl_net']:>+10,.0f} {row['pnl_net_pfu']:>+10,.0f}")

    # ── Verdict net break-even ─────────────────────────────────────────
    print(f"\n{'═'*90}")
    print("  VERDICT — capital minimum pour que ça vaille le coup")
    print(f"{'═'*90}")
    for v_name in ["A", "B"]:
        print(f"\n  Variante {v_name}")
        for cap in capitals:
            rows = df_all[(df_all["variant"] == v_name) & (df_all["cap_total"] == cap)]
            if rows.empty: continue
            total_pfu  = rows["pnl_net_pfu"].sum()
            per_year   = total_pfu / 2
            pct_year   = (per_year / cap) * 100
            flag = "✅" if total_pfu > 0 else "🔴"
            print(f"    ${cap:>5,} capital  →  {per_year:>+7,.0f}$/an net PFU  "
                  f"({pct_year:>+6.1f}%/an)  {flag}")

    df_all.to_csv("backtest_realistic.csv", index=False)
    print(f"\n  CSV complet → backtest_realistic.csv")


if __name__ == "__main__":
    main()
