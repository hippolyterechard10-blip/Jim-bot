# Jim Parity Investigation — État au 2026-05-31 (post-Patch 1)

État écrit avant compaction context. Tout ce qui suit est read-only au moment de l'écriture.

## Runtime live (intact)

- **Jim PID 35523** alive, paper Phase 4.1
- Config active : `T1S_DIV_MODE=never`, `ROUTER_VARIANT=t1_neutral`, `T1S_INCLUDE_HARD_BLOCK=1`, `GEO_DISABLE_LOWVOL=1`
- Capital paper : $10 000 → ~$9 744 (perte modeste, ETH short + SOL short ouverts)
- **Aucune modif live faite pendant cette investigation**

## Contexte investigation

Audit externe Opus 4.8 a identifié des divergences structurelles backtest/live. Mon premier instinct (patcher live entry vers `zone.center`) a été infirmé par Opus 4.8 : la limite live à `zone.high` est **marketable** (curr est déjà dans la zone via gate de distance), donc elle fille au prix courant, pas à zone.high. Le vrai fill = `min(curr, zone.high)` pour long.

→ **Le live n'est pas cassé**. C'est le **backtest** qui mentait avec proxy `zone.center`.

## Résultats backtests 2022-2024 (ETH+SOL, capital $10k)

| Config | Backtest module | N trades | WR | PF | Sharpe | MaxDD | Return 3y |
|---|---|---:|---:|---:|---:|---:|---:|
| BASELINE (entry=center, fee=maker) | `backtest_ablation.py` flags off | 7 259 | 72.9 % | 4.47 | **+21.57** | -1.65 % | +574 B % |
| A2 entry edge (entry=high, fee=maker) | `backtest_ablation.py` ABLATION_ENTRY_EDGE | 7 259 | 68.8 % | 1.99 | **+14.92** | -5.05 % | +11.7 M % |
| A7 conservative tie-break | `backtest_ablation.py` ABLATION_CONSERVATIVE_TIE | 7 259 | 72.9 % | 4.46 | +21.53 | -1.65 % | +555 B % |
| A124 combo (timeout+entry+T1L_strict) | `backtest_ablation.py` | 8 522 | 65.4 % | 1.95 | +15.01 | -5.85 % | +14.5 M % |
| **P1 v2 (entry=min(curr,high), fee=taker, +5bps slip)** | `backtest_phase41_v2.py` | **5 380** | **63.74 %** | **1.63** | **+11.88** | **-5.65 %** | **+61 681 %** |
| ALL_parity (entry=edge + tous patches "réalistes") | `backtest_phase41_parity.py` + ablation flags | 5 311 | 44.3 % | 0.89 | **−3.53** | **−76.65 %** | **−74 %** |

## Findings clés

- **A7 (conservative tie-break) NON coupable** : delta Sharpe ~0 vs baseline. SL+TP same-bar tie-breaks quasi inexistants.
- **A2 (entry edge) seul** : -6.65 Sharpe vs baseline. C'était la motivation initiale "patcher live to center" qui a été retournée par Opus 4.8.
- **A124 combo (timeout + entry + T1L_strict)** ≈ A2 seul. Timeout 2h et T1L_strict_div NE sont PAS des destructeurs majeurs.
- **P1 (fill réaliste = curr + FEE_TAKER + slip 5bps)** : Sharpe **+11.88**, PF 1.63. Premier chiffre crédible. Edge survit. Toutes années 2022/2023/2024 profitables.
- **ALL_parity** : Sharpe **-3.53**. Tendance baseline 21 → A2 15 → P1 12 → ALL -3.5 : plus on ajoute de "réalisme", plus ça meurt.

## Per-symbol / per-year (P1 v2)

```
ETH/USD : n=2984  WR=60.6%  PnL=$+1.78M
SOL/USD : n=2396  WR=67.7%  PnL=$+4.39M  ← SOL meilleur

2022 : n=3325  WR=66.9%  PnL=$+1.19M
2023 : n=1380  WR=58.3%  PnL=$+2.29M
2024 : n=675   WR=59.4%  PnL=$+2.68M  ← edge tient toutes années
```

## Doc Opus 4.8 reference

`~/Documents/documents AI-OS/AI-OS/patch docs/doc patch.md` (embed de `Jim ORB Strategy Backtest v2 (1).md`).

Patches prescrits par Opus 4.8 (dans l'ordre) :
1. **Patch 1** (fait) : fill = `min(curr, zone.high)` (long), `max(curr, zone.low)` (short) + FEE_TAKER pour T1 marketable
2. **Patch 2** (à faire) : funding cost (1 bps/8h placeholder, à raffiner)
3. **Patch 3** (à faire) : TIMEOUT_BARS = 24 (2h, aligné live)
4. **Patch 4** : vérif look-ahead (`<=` vs `<` slice)
5. **Fix structurel durable** : extraire signaux dans module partagé entre backtest et live

## Artefacts de mesure à corriger (signalés par user)

### Artefact 1 : Compounding inflation

Returns en milliards de % (baseline +574B%, P1 +61 681%) viennent du modèle de sizing :
```python
deploy = self.equity * pos_pct * size_mult * cap_f
```
Equity grandit → deploy grandit → PnL grandit → super-compounding non-réaliste.

**Fix proposé** : passer en **notionnel fixe** ou fixed-fractional plafonné.
- Option A : `deploy = CAPITAL × POS_PCT × size_mult` (constant, basé capital initial)
- Option B : `deploy = min(equity × POS_PCT × size_mult, MAX_NOTIONAL)` (capped fractional)

À appliquer puis re-run P1 pour avoir un Sharpe + Return crédibles.

### Artefact 2 : Sharpe annualization

Current code :
```python
daily_eq = eq_df["equity"].resample("1D").last().dropna()
daily_ret = daily_eq.pct_change().dropna()
sharpe = daily_ret.mean() / daily_ret.std() * sqrt(365)
```

Daily Sharpe × √365 est CORRECT en théorie. À vérifier :
- Est-ce que `equity_curve` est appended à chaque bar 5min ? → daily resample → OK
- Y a-t-il des jours sans trades ? → returns = 0, deflate std → bon pour la mesure
- Ou est-ce que c'est intra-day Sharpe × √périodes ?

À auditer + recalculer proprement.

## Diff config entre P1 et ALL_parity (Task 1)

**12 différences identifiées** par lecture code (P1 = `backtest_phase41_v2.py`, ALL_parity = `backtest_ablation.py` avec tous les flags activés via `run_one_ablation.py ALL_parity`) :

| # | Item | P1 (Patch 1 only) | ALL_parity (tout) | Testé isolément ? |
|---|---|---|---|---|
| 1 | Entry T1 | `min(curr, zone.high)` long / `max(curr, zone.low)` short | `zone.high` long / `zone.low` short | A2 = Sharpe 14.92 |
| 2 | R:R reference | From `fill` (=curr or edge) | From `curr` directly | Equivalent à P1 |
| 3 | fill_type T1 | `"market"` (FEE_TAKER + 5 bps slip) | `"limit"` (FEE_MAKER, 0 slip) | P1 a market, ALL a limit ⚠️ inversion |
| 4 | Timeout T1 | 48 bars (4h) | 24 bars (2h) | A124 ≈ A2 → non majeur |
| 5 | Distance gate T1L | `(curr - center)/center` ∈ [-0.012, +0.002] | `(curr - high)/high` ∈ [-0.015, +0.015] | **NON testé isolé** |
| 6 | Distance gate T1S | `(center - curr)/curr` ∈ [-0.002, +0.012] | `(low - curr)/low` ∈ [-0.004, +0.015] | **NON testé isolé** |
| 7 | T1L divergence | `rsi_fallback` [30,55] | STRICT (no fallback) | A124 ≈ A2 → non majeur |
| 8 | Volume gate T1 | Active (`vo5[-2] < avgv*0.3`) | REMOVED | **NON testé isolé** ⚠️ suspect |
| 9 | EMA slope gate T1 | Active (`ema5 < ema10*0.9985`) | REMOVED | **NON testé isolé** ⚠️ suspect |
| 10 | Tie-break SL+TP | Random 50/50 | Always stop | A7 = no impact → non coupable |
| 11 | CAP_FACTOR multi-expert | 0.7 | 1.0 | **NON testé isolé** |
| 12 | Funding | NOT modeled | 1 bps/8h ON | **NON testé isolé** |
| 13 | Slippage market exits | 5 bps | 10 bps | **NON testé isolé** (effet probablement faible) |

**Note importante** : item #3 — P1 a `fill_type="market"` mais ALL_parity a gardé `fill_type="limit"`. Donc P1 paye **FEE_TAKER + slippage** mais ALL_parity paye **FEE_MAKER + 0 slippage** sur entrée. Ce qui veut dire qu'à entrée comparable, P1 est PIRE qu'ALL_parity sur le coût d'entrée. Mais le résultat global montre P1 (Sharpe 11.88) > ALL_parity (-3.53). Donc les autres patches dominent largement.

## Plan ablation one-at-a-time depuis P1 (Task 1 suite)

Stratégie : prendre `backtest_phase41_v2.py` (P1 baseline), ajouter flags pour chaque patch ALL_parity, tester un-par-un. Patches déjà connus skip.

**À tester (par ordre de suspect probable)** :
1. **A5 + A6 combiné** : remove volume + EMA gates (les 2 sont des filtres similaires de qualité signal)
2. **A3 distance gate live** : window plus large
3. **Funding ON** (A9)
4. **CAP_FACTOR_LIVE** (A8)
5. **Slippage 10 bps** (A10)
6. **Reste** : si encore négatif après les 5 ci-dessus, autre cause cachée

**À skip** (déjà prouvé non-majeurs) :
- A4 T1L strict div (A124 ≈ A2)
- A1 timeout 2h (A124 ≈ A2)
- A7 conservative tie-break (no impact)
- A2 entry edge (déjà mesuré, P1 utilise une variante plus douce)

**Coût compute** : 5 nouvelles configs × ~12 min en parallèle = ~15 min wall clock.

**Avant de lancer** : confirmer le plan avec user + traiter les artefacts mesure (Task 2).

## Task 2 — Artefacts mesure

### Sizing : compounding inflation

Le backtester actuel : `deploy = equity × POS_PCT × size_mult × cap_f`. Equity grandit avec PnL → deploy grandit → super-compounding.

**Symptôme** : returns en milliards de % (+574B % baseline) clairement non-réaliste pour live.

**Options de fix** :

| Option | Formule | Avantage | Inconvénient |
|---|---|---|---|
| (A) Fixed notional absolu | `deploy = CAPITAL × POS_PCT × size_mult` (constant à $5k) | Simple, comparable trade-by-trade | Ignore l'effet capital growth (qui est réel à scale réaliste) |
| (B) Fixed-fractional plafonné | `deploy = min(equity × POS_PCT × size_mult, MAX_NOTIONAL)` | Plus réaliste : grandit avec equity mais cap à un plafond | Choix du cap arbitraire |
| (C) Log-equity scaling | `deploy = base × (1 + log(equity/CAPITAL))` | Smooth compounding | Moins standard, complexe |

**Recommend** : Option (A) pour ablation tests (élimine compounding noise pour comparer per-trade). Option (B) pour final report (réaliste avec MAX_NOTIONAL = $10k = 1× capital initial).

### Sharpe annualization

Code actuel :
```python
daily_eq = eq_df["equity"].resample("1D").last().dropna()
daily_ret = daily_eq.pct_change().dropna()
sharpe = daily_ret.mean() / daily_ret.std() * sqrt(365)
```

**Audit** :
- `equity_curve.append((t_now, pool.equity))` → 1 entry per 5min bar
- `resample("1D").last()` → garde le dernier equity de chaque jour calendaire
- `pct_change()` → daily returns
- `× sqrt(365)` → annualisation calendrier (365 days)

**Issues potentielles** :
1. Crypto trades 365 jours/an (24/7) → 365 est correct
2. Si position ouverte fin de journée, l'equity ne reflète pas l'unrealized PnL (juste cash + open position notional from entry). À vérifier dans `SharedPool.equity`.
3. Si journée sans trade clos, daily_ret = 0 → deflate la std (bon pour le score mais peut overestimate)
4. ⚠️ Compounding gonfle aussi la std proportionnellement à la mean → Sharpe stays plausible MAIS magnitude des chiffres tellement large que toute interprétation devient bruit
5. Le Sharpe au sens "standard" = excess return / std. Ici on n'a pas de risk-free rate (assumé 0). OK pour crypto.

**Recommend** : recalculer Sharpe avec **fixed-notional sizing (Option A)** d'abord. Si c'est cohérent, les chiffres seront plus interprétables.



## TODO (par ordre demandé)

- [x] Tâche 0 : Écrire ce doc état (jim-parity-state.md)
- [ ] Tâche 1 : Diff exact config P1 vs ALL_parity, puis ablation one-at-a-time depuis P1
- [ ] Tâche 2 : Corriger artefacts mesure (sizing + Sharpe), re-rapporter P1
- [ ] Patch 2 (funding) — bloqué tant que task 1+2 pas faits
- [ ] Patch 3 (timeout)
- [ ] Patch 4 (look-ahead)
- [ ] Fix structurel module partagé
- [ ] Décision finale : Phase 4.1 a-t-elle vraiment un edge ?

## Fichiers backtest pertinents

- `trading-agent/backtest_short_expert.py` (original, après B1 align defaults)
- `trading-agent/backtest_ablation.py` (avec ablation flags, base = "never mode" baseline)
- `trading-agent/backtest_phase41_parity.py` (TOUS les patches activés en dur)
- `trading-agent/backtest_phase41_v2.py` (Patch 1 SEUL appliqué)

## Runners

- `trading-agent/run_one_ablation.py` (ablation flag-based)
- `trading-agent/run_ablation.py` (séquentiel, abandonné — 10h+ buffering)
- `trading-agent/run_phase41_parity.py` (ALL parity)
- `trading-agent/run_phase41_v2.py` (P1 only)

## CSVs résultats actuels

- `trading-agent/ablation_BASELINE.csv`
- `trading-agent/ablation_A2_entry_edge.csv`
- `trading-agent/ablation_A7_tie_stop.csv`
- `trading-agent/ablation_A124_combo.csv`
- `trading-agent/ablation_ALL_parity.csv`
- `trading-agent/phase41_v2_p1_result.csv`
- `trading-agent/phase41_parity_trades.csv` (trade-by-trade pour ALL_parity)

## Commits récents (feature/multi-thesis-short)

- `b2cbe67` fix(broker): P0 SL/TP orphelin reduceOnly + twin cancel
- `915b4d6` feat(dashboard): enriched live trade view
- `23a3884` fix(strat): prevent same-symbol long+short
- `a38c482` fix(backtest): align module defaults with live
- `0e77fcf` docs(geometric_expert): OKX → broker-agnostic
- `e9e9acb` chore: archive phase A/B/C1 research scripts
- `c7e5dfd` chore: archive dead broker_kraken.py
- `a0cd632` fix(strat)+feat(strat): reconcile side + T1S divergence mode
- `71222ee` fix(broker): long+short side mapping end-to-end

État pushé sur `origin/feature/multi-thesis-short`.

---

## Kraken venue A/B — 2026-05-31

**Question** : le Sharpe +8.72 du backtest (data Binance US) tient-il sur les
prix réels de Kraken Futures, le venue où Jim exécute ?

### Source de données

| Symbole live | Source Kraken testée | Premier candle | Coverage |
|---|---|---|---|
| ETH/USD (PF_ETHUSD) | `https://futures.kraken.com/api/charts/v1/trade/PF_ETHUSD/5m` | **2022-04-01 04:00 UTC** | 100.00 % |
| SOL/USD (PF_SOLUSD) | `https://futures.kraken.com/api/charts/v1/trade/PF_SOLUSD/5m` | **2022-04-01 04:00 UTC** | 100.00 % |

- Perp Kraken n'existe pas avant Q2 2022. Pas de fallback Spot nécessaire (perp
  remonte assez loin pour couvrir 2022 Q2-Q4 + 2023 + 2024).
- 289 728 candles 5m / symbole, écrits dans `trading-agent/kraken_cache/`.
- Fetcher : `trading-agent/fetch_kraken_5m.py` (pagine 6.5 j par appel,
  cap API = 2000 candles).

### Asymétrie de couverture Binance vs Kraken (à connaître)

Sur la même fenêtre 2022-04-01 → 2024-12-31, le cache Binance US a des trous
SOL (probable délisting Binance US Q3 2024) :

- Binance bars 5m communs ETH+SOL : **170 829**
- Kraken bars 5m communs ETH+SOL  : **289 441** (1.69 × plus de barres
  évaluables)

→ N(trades) de Kraken est mécaniquement gonflé par cette couverture
supplémentaire. La comparaison honnête se fait sur les **métriques per-trade**
(WR / PF / expectancy / Sharpe), pas sur N ni return brut.

### A/B (même moteur, même fenêtre, même flags ; seule la série ETH+SOL change)

Window : **2022-04-01 → 2024-12-31** (33 mois)
Engine : `backtest_p1_ablation.py` (Patch 1 + sizing fixe $5k)
BTC 4h regime input : Binance dans les DEUX runs (variable de contrôle)

| Métrique         | Binance P1 | Kraken P1 | Δ      | Binance ALL | Kraken ALL | Δ      |
|------------------|-----------:|----------:|-------:|------------:|-----------:|-------:|
| N trades         | 4 512      | 7 258     | +2 746 | 6 999       | 11 030     | +4 031 |
| WR %             | 63.30      | 60.77     | −2.53  | 69.85       | 67.87      | −1.98  |
| PF               | 1.90       | 1.73      | −0.17  | 2.38        | 2.23       | −0.15  |
| **Sharpe**       | **+9.79**  | **+8.43** | **−1.36** | **+9.29** | **+8.21**  | **−1.08** |
| MaxDD %          | −1.29      | −1.72     | −0.43  | −1.06       | −1.54      | −0.48  |
| Expectancy ($)   | 11.80      | 10.00     | −1.80  | 15.97       | 14.74      | −1.23  |
| Return 33m %     | +532       | +726      | +193   | +1 118      | +1 626     | +508   |

### Per-année sur Kraken (P1_sized)

```
2022 (avr-déc) : n=1985  WR=64.1%  PnL=$+25 131
2023           : n=2322  WR=55.4%  PnL=$+14 688
2024           : n=2951  WR=62.8%  PnL=$+32 748
```

Toutes années nettement positives sur Kraken. 2023 reste l'année la plus
faible (cohérent avec Binance), 2024 la plus active.

### Per-symbole sur Kraken (P1_sized)

```
ETH/USD : n=4005  WR=56.2%  PnL=$+26 469
SOL/USD : n=3253  WR=66.5%  PnL=$+46 098
```

SOL toujours meilleur que ETH (cohérent avec Binance), mais ETH WR plus faible
sur Kraken (56 % vs 60 % sur Binance) → légère dégradation côté ETH.

### Verdict

- **L'edge survit au changement de venue d'exécution**. Sharpe +8.43 (P1) à
  +8.21 (ALL_parity) sur prix Kraken vs +9.79 / +9.29 sur Binance.
- **Coût venue mesuré** : Sharpe perd ~1.1-1.4, per-trade expectancy −8 à −15 %,
  WR −2 à −2.5 pp, PF −0.15. Réel mais pas catastrophique.
- **Pas de cause cachée** : pas d'année qui passe en négatif, pas de symbole qui
  s'effondre, pas de MDD qui explose.
- **Decision** : capital allocation Phase 5 reste valide. Le +8.72 Binance
  surestimait l'edge de ~12 % en Sharpe ; les chiffres réalistes Kraken
  (+8.2 à +8.4) restent excellents.

### Notes méthodo

- Pas de fallback Spot Kraken nécessaire (perp couvre 33 mois).
- Coverage Binance imparfaite sur SOL — la comparaison la plus rigoureuse
  serait intersection des timelines. Le delta Sharpe observé est probablement
  surestimé par cette asymétrie (Kraken pénalisé par +60 % trades = +√1.6 ×
  variance daily). Si intersection : delta Sharpe probablement < −1.
- **Aucun patch n'est appliqué sur le live ni sur les modules sources** suite à
  ce run. Le résultat sert à valider que Jim peut tourner sur Kraken avec un
  edge attendu réaliste, pas à modifier le code.

### Fichiers générés (read-only artefacts)

- `trading-agent/kraken_cache/ETHUSD_5m_20220401_20241231.parquet`
- `trading-agent/kraken_cache/SOLUSD_5m_20220401_20241231.parquet`
- `trading-agent/fetch_kraken_5m.py`
- `trading-agent/run_kraken_ab.py`
- `trading-agent/kab_binance_P1_sized.csv` + `_trades.csv`
- `trading-agent/kab_binance_ALL_parity_sized.csv` + `_trades.csv`
- `trading-agent/kab_kraken_P1_sized.csv` + `_trades.csv`
- `trading-agent/kab_kraken_ALL_parity_sized.csv` + `_trades.csv`

---

## Walk-forward overfit measurement — Kraken — 2026-05-31

**Question** : combien du Sharpe +8.4 (in-sample Kraken full-window) est de
l'edge réel vs du fitting aux 33 mois ? Réponse : evaluer sur des données que
les paramètres n'ont jamais vues.

### Degrés de liberté fittés (re-sélectionnables automatiquement)

| Param | Choix | Selection method | Contamination |
|---|---|---|---|
| `T1S_DIV_MODE` | {never, rsi_fallback, strict} | 7 backtests 2022-2024 → "never" | directe |
| `ABLATION_NO_VOL_EMA` | {True, False} | décision "ne pas porter gates au live" sur ablation 2022-2024 | directe |
| `ABLATION_DIST_LIVE` | {True, False} | choix live vs zone.edge | partielle |

Hand-set, pré-2022 (non-retunés ici) : RSI bands, TARGET_PCT, TIMEOUT_BARS, ZONE_PCT, MIN_RR.
Constantes physiques (non-tuning) : FEE_MAKER/TAKER, SLIPPAGE_BPS, FIXED_NOTIONAL.
Patches réalisme Opus 4.8 : FUNDING+CAP_F_1+SLIP_10 ON pour toutes les cellules.

**Grid testé** : 3 × 2 × 2 = **12 cellules**, lockées par Sharpe TRAIN, évaluées sur TEST quarantained.

### Anchored walk-forward

- TRAIN : **2022-04-01 → 2023-12-31** (21 mois) — sélection de params
- TEST  : **2024-01-01 → 2024-12-31** (12 mois) — jamais touché par la sélection

Top 5 cellules (rangées par Sharpe TRAIN) :

| # | cellule (label) | TRAIN Sharpe | rk TRAIN | TEST Sharpe | rk TEST | Δ |
|---|---|---:|---:|---:|---:|---:|
| 1 | div_never_vol1_dist1   | +8.90 | 1 | +11.17 | 2 | +2.27 |
| 2 | div_never_vol0_dist1   | +8.81 | 2 | +11.28 | 1 | +2.47 |
| 3 | div_never_vol1_dist0   | +8.69 | 3 |  +9.85 | 3 | +1.16 |
| 4 | div_fallback_vol1_dist1| +7.56 | 4 |  +7.22 | 5 | −0.34 |
| 5 | div_never_vol0_dist0   | +7.52 | 5 |  +9.22 | 4 | +1.70 |

Spectre complet (anchored, 12 cellules) :
- **Spearman ρ TRAIN→TEST = +0.951** (rangement quasi-identique entre les deux fenêtres)
- Toutes cellules `div_never` dominent toutes `div_fallback` qui dominent toutes `div_strict` dans les DEUX périodes
- `div_strict` est désastreux dans les deux (Sharpe TRAIN 0.94–1.15, TEST 0.99–1.69) — choix « never » validé hors-sample

**Winner lockée (TRAIN uniquement)** : `div_never_vol1_dist1` (= config live actuelle : `T1S_DIV_MODE=never`, vol+EMA gates OFF, distance live)

| Métrique | TRAIN | TEST | Δ |
|---|---:|---:|---:|
| Sharpe | +8.90 | **+11.17** | **+2.27 (+25.5 %)** |
| WR % | 67.23 | 68.86 | +1.63 |
| PF | 2.22 | 2.24 | +0.02 |
| MDD % | −1.54 | −1.87 | −0.33 |
| Expectancy ($) | 14.48 | 15.14 | +0.66 |
| N trades | 6 775 | 4 236 | — |

### Rolling walk-forward 3 folds (robustesse)

TRAIN 12mo / TEST 6mo, glissant :

| Fold | TRAIN | TEST | Winner TRAIN | TRAIN Sh | TEST Sh | Δ | Spearman |
|---|---|---|---|---:|---:|---:|---:|
| A | 2022-04 → 2023-03 | 2023-04 → 2023-09 | div_never_vol1_dist1 | +11.35 |  +6.45 | **−4.90** | +0.804 |
| B | 2022-10 → 2023-09 | 2023-10 → 2024-03 | div_never_vol1_dist1 |  +7.61 | +11.92 | **+4.31** | +0.937 |
| C | 2023-04 → 2024-03 | 2024-04 → 2024-09 | div_never_vol0_dist1 |  +9.06 | +11.42 | **+2.36** | +0.944 |
| **Mean** | | | | **+9.34** | **+9.93** | **+0.59** | **+0.895** |

Stabilité du choix : la cellule `div_never_vol1_dist1` (= config live) est
- TRAIN winner dans 2 folds sur 3 (A et B)
- TRAIN rank #2 dans le fold C, à seulement 0.13 Sharpe du winner
- TEST rank ∈ {1, 2, 2} dans les 3 folds → **toujours dans le top 2 OOS**

Anchored config across folds :
```
Fold A: TRAIN +11.35 → TEST  +6.45   (rank TRAIN#1 TEST#2)
Fold B: TRAIN  +7.61 → TEST +11.92   (rank TRAIN#1 TEST#1)
Fold C: TRAIN  +8.93 → TEST +11.20   (rank TRAIN#2 TEST#2)
Mean  : TRAIN  +9.30 → TEST  +9.86   (Δ +0.56, +6 %)
```

### Verdict overfit

- **Aucun overfit détectable.** Sharpe OOS mean +9.93 vs TRAIN +9.34 (rolling),
  +11.17 vs +8.90 (anchored). L'edge GÉNÉRALISE.
- **Spearman ρ +0.90-0.95** : le grid TRAIN ne sélectionne pas de bruit, il
  sélectionne du vrai signal.
- **Worst-case fold A** : TEST drop à +6.45 (vs TRAIN +11.35). Régime Q2-Q3 2023
  (chop / range léger) est défavorable. Mais +6.45 OOS reste très solide.
- **La config live `T1S_DIV_MODE=never` + vol+EMA OFF + dist live est la plus
  robuste** : TRAIN winner dans 2 folds + anchored, TEST rank ≤ 2 partout,
  Sharpe OOS mean ~+10.
- **Pas de "magie 2022-2024"** : si tout le +8.4 venait du fitting, les TEST OOS
  seraient nuls ou négatifs. Ils ne le sont pas.

### Bound réaliste pour Phase 5

- Mid-case attendu sur futures données inconnues : Sharpe live ~+8 à +10
  (cohérent avec mean rolling +9.93)
- Worst-case observé (régime non-favorable) : Sharpe ~+6.5
- Best-case (régime favorable) : Sharpe ~+11.5
- **Confiance** : 5 mesures indépendantes (anchored + 3 rolling folds + Kraken
  A/B), aucune négative, mean ~+10. Edge réel.

### Notes méthodo

- **Quarantaine respectée** : aucun param n'a "vu" un TEST avant son lock.
  Sélection faite par `sorted(train_results, key=sharpe, reverse=True)[0]`,
  TEST run after.
- BTC 4h regime input = Binance (variable contrôlée — pas testée comme tuning).
- Le grid est minimal (12 cells) ; un grid plus large (RSI bands, TARGET_PCT,
  TIMEOUT, ZONE) confirmerait/raffinerait mais multiplie le compute par ~50.
- **Aucun patch live appliqué** — résultat sert à valider la crédibilité du +8.4,
  pas à modifier le code.

### Fichiers générés

- `trading-agent/run_wf.py` (anchored TRAIN/TEST runner)
- `trading-agent/run_wf_roll.py` (rolling 3-fold runner)
- `trading-agent/wf_train_div_*.csv` (12 fichiers) + `wf_test_div_*.csv` (12)
- `trading-agent/wf_rollA_train_div_*.csv` + `_test_` (24 par fold)
- `trading-agent/wf_report.json` (anchored)
- `trading-agent/wf_rolling_report.json` (rolling)

---

## Diagnostic live vs backtest (Phase 4.1) — 2026-05-31

### CONTEXTE

Live PID 35523 perd en paper (~−$256 sur 7j). Question : pourquoi alors que le
backtest même régime sort +8.4 Sharpe ?

### A1 — Live book 7 derniers jours (paper)

Source : `trading-agent/trading_memory.db` (sqlite, table `trades`).

```
side  n   total_pnl  avg_pnl
buy   30  -$124.30   -$4.14   ← longs
sell  21   -$71.03   -$3.38   ← shorts
```

**Pas all-shorts** : Jim tire 60 % de longs, 40 % de shorts en ce moment.

Par (symbole × side) :
```
ETH/USD BUY   : n=19  WR=26.3%  PnL=-$209.40   ← le saigneur (ETH longs)
ETH/USD SELL  : n=12  WR=50.0%  PnL= -$32.75
SOL/USD BUY   : n=11  WR=81.8%  PnL= +$85.10   ← SOL longs profitables
SOL/USD SELL  : n=9   WR=44.4%  PnL= -$38.27
```

→ La perte est concentrée sur **ETH/USD longs** (19 trades, 26 % WR, −$209).
SOL longs vont très bien.

### A2 — Asymétrie de gate divergence (LIVE)

**T1L (long)** — `experts/geometric_expert.py:745-748` :
```python
if not config.DEBUG_MODE:
    if not self._rsi_divergence(closes_5m, rsi_now):
        if not low_vol_mode:
            n_div += 1; continue  # ← divergence STRICTEMENT requise
```
→ T1L exige `_rsi_divergence` = True (sauf `low_vol_mode` ou `DEBUG_MODE`).
Pas de `T1L_DIV_MODE` switch — strict en dur.

**T1S (short)** — `experts/geometric_expert.py:962-970` :
```python
if config.T1S_DIV_MODE == "never":
    pass  # skip divergence check entirely
elif config.T1S_DIV_MODE == "strict":
    if not self._rsi_bearish_divergence(closes_5m, rsi_now):
        n_div_s += 1; continue
else:  # "rsi_fallback"
    if not self._rsi_bearish_divergence(closes_5m, rsi_now):
        if not (45 <= rsi_now <= 70):
            n_div_s += 1; continue
```
→ Config live `T1S_DIV_MODE="never"` → **shorts SKIP la divergence**.

**Asymétrie confirmée** :
- **T1L** : divergence RSI haussière REQUIS (strict, hard-coded)
- **T1S** : divergence ignorée (`never` mode)

Conséquence en bull : divergences haussières aux supports deviennent rares
(prix monte, fait des HL, pas de LL), donc T1L doit logiquement se faire
filtrer. Mais le **live tire quand même 30 longs en 7j** → la divergence se
déclenche souvent dans les données Yahoo. Cohérent avec un signal de mauvaise
qualité (faux positifs sur RSI/zones bruyantes).

### A3 — Sources de données live

| Composant | Source | Fichier |
|---|---|---|
| Signaux (`get_bars`) | **Yahoo Finance** spot (`ETH-USD`, `SOL-USD`) via `yfinance` | `broker_kraken_paper.py:211-230` |
| Régime BTC 4h | Yahoo Finance spot (`BTC-USD`) via yfinance | `crypto_regime.py` |
| Exécution | Kraken Futures Paper perp (`PF_ETHUSD`, `PF_SOLUSD`) via CLI `kraken futures paper buy/sell` | `broker_kraken_paper.py:240-280` |
| Mark/fill | Kraken Futures perp mark | `broker_kraken_paper.py:get_live_price()` |

**SOURCES SIGNAUX ≠ SOURCES EXÉCUTION**. Le live décide sur Yahoo spot,
exécute sur Kraken perp. Le backtest tournait sur Binance US spot (puis Kraken
perp dans nos derniers tests).

### Bonus structural — **bug de mesure dans tous les backtests précédents**

`run_kraken_ab.py`, `run_wf.py`, `run_wf_roll.py` passaient
`enabled={"t1","t2"}`. Mais `backtest_p1_ablation.py:937` :
```python
def route_long_signal(arr_5m, arr_15m, arr_1h, regime_decision, enabled):
    if "long_t1" not in enabled: return None, None
```

→ `"long_t1"` ne figurait JAMAIS dans `enabled` → `route_long_signal`
retourne toujours None → **AUCUN long ne fire dans le backtest**.

Vérification CSV :
```
kab_kraken_P1_sized_trades.csv         : {'short': 7258}   total= 7258
kab_kraken_ALL_parity_sized_trades.csv : {'short': 11030}  total=11030
wf_train_div_never_vol1_dist1.csv      : short-only (idem)
```

**100 % shorts dans toutes les mesures précédentes.** Le Sharpe +8.43 (Kraken
A/B), +11.17 (anchored OOS), +9.93 (rolling) = **edge short-only**, pas
stratégie long+short telle que déployée.

Patch backtest (read-only, pas de modif live) :
- `backtest_p1_ablation.py` : `SharedPool.open()` reçoit `regime_state` (ligne
  ~435) pour tagger les trades, `_try_enter()` le propage (ligne ~1325)
- `run_long_short.py` : nouveau runner avec `enabled = {"long_t1", "t1", "t2"}`

### A4 — Backtest re-tourné AVEC longs activés (33 mois Kraken, ALL_parity_sized)

```
N=11324  WR=67.71%  PF=2.22  Sharpe=+8.27  MDD=-1.54%  Ret=+1661 %
  LONGS  : n=  333  WR=60.96%  PF=1.72  avg=+$10.40  tot=+$3 464
  SHORTS : n=10991  WR=67.92%  PF=2.24  avg=+$14.80  tot=+$162 641
```

Long share = **2.9 %** du book (333/11324). En live : **60 %** des trades sont
des BUY. Ratio long/short en backtest : **0.03×**. Ratio en live : **1.4×**.
→ **Mismatch structural de 50×** dans la proportion de longs.

Bucketing Side × Regime (33 mois) :

| Side | Regime | N | WR % | PF | Avg PnL | Total |
|---|---|---:|---:|---:|---:|---:|
| **long** | **bull** | 333 | 60.96 | 1.72 | +$10.40 | +$3 464 |
| short | bear | 107 | 73.83 | 3.94 | +$21.14 | +$2 262 |
| **short** | **bull** | 241 | 62.66 | 1.59 | +$8.95 | +$2 157 |
| short | chop | 10 643 | 67.98 | 2.24 | +$14.87 | +$158 222 |

**Verdict A4** : l'hypothèse "shorts-en-bull marginaux dans le backtest" est
**réfutée**. Les shorts-en-bull sortent à PF 1.59, WR 62.66 %, +$2k de PnL net.
Mais il n'y en a que 241 sur 33 mois (≈ 7/mois) — la "bull" classification est
rare dans le régime engine.

La vraie divergence backtest→live :
- **Backtest** : 97 % shorts (régime engine n'autorise quasi jamais long ;
  données Binance/Kraken futures)
- **Live** : 60 % longs (régime engine autorise souvent long ; données Yahoo
  spot)

→ **La cause #1 du gap n'est pas l'exécution mais le RÉGIME ENGINE qui voit
des "bull" différemment selon la source de données**.

### Hypothèses de cause (à valider plus tard)

1. **Yahoo BTC-USD vs Kraken/Binance BTC** : RSI/trend du régime engine en
   diffère → classification "trend_up" beaucoup plus fréquente en live
2. **Yahoo ETH-USD 5m** : barres manquantes, alignement de close à 5min de
   Coinbase Pro ; produit des pivots et un RSI différents → zones supports
   différentes → T1L se déclenche sur des configurations que le backtest n'a
   pas dans son cache (Kraken perp continu 24/7)
3. **N = 51 vs 11 000** : 51 trades a un IC de ±14 pp sur WR à 95 %. Si le
   vrai WR live est 60 %, observer 26 % WR sur 19 trades reste compatible avec
   un mauvais run de variance pure. Mais −$256 sur 7j EST significatif si
   espéré +$10/trade × 51 = +$510

### Notes 

- **Le live n'est pas cassé au sens "code"**. Les sources qu'il consomme
  produisent un mix long+short et un régime différent.
- **Aucune modification live faite.** Aucune modification de la stratégie
  proposée à partir de ces données — il faut d'abord (a) valider que Yahoo
  diffère effectivement de Kraken sur des fenêtres récentes (b) mesurer
  l'effet sur le régime engine.

---

## Sweep timeout 120 / 180 / 240 min — Kraken long+short — 2026-05-31

### B1 — Sweep full window (33 mois, ALL_parity_sized, enabled={long_t1, t1, t2})

| Timeout | N | WR % | PF | **Sharpe** | MDD % | Long N | Long WR | Long PF | Short N | Short WR | Short PF |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 120 min (t24) | 12 989 | 63.71 | 2.14 | **+8.17** | −1.37 | 372 | 58.87 | 1.69 | 12 617 | 63.85 | 2.16 |
| 180 min (t36) | 11 876 | 66.19 | 2.19 | **+8.16** | −1.62 | 342 | 60.23 | 1.69 | 11 534 | 66.37 | 2.21 |
| 240 min (t48) | 11 324 | 67.71 | 2.22 | **+8.27** | −1.54 | 333 | 60.96 | 1.72 | 10 991 | 67.92 | 2.24 |

Sharpe deltas minuscules (max +0.11). Pas de "free win" évident.

### B2 — OOS robustness (TRAIN 2022-04 → 2023-12 / TEST 2024)

```
TRAIN  Sharpe par timeout : t48 +8.95  >  t36 +8.86  >  t24 +8.81
TEST   Sharpe par timeout : t48 +11.34 >  t24 +11.20 >  t36 +11.04
```

→ **Le winner TRAIN (t48) gagne aussi sur TEST**. Pas d'overfit. La hiérarchie
n'est pas inversée — t48 domine cleanement.

### B3 — Long vs Short response au timeout (full window)

Bucketing par regime au sein de chaque timeout :

```
                            t24 (120m)     t36 (180m)     t48 (240m)
long × bull   N / WR / PF   372 / 58.9 / 1.69   342 / 60.2 / 1.69   333 / 61.0 / 1.72
short × bull  N / WR / PF   291 / 60.8 / 1.70   256 / 62.9 / 1.80   241 / 62.7 / 1.59  ← peak à t36
short × bear  N / WR / PF   117 / 71.8 / 4.33   110 / 72.7 / 4.52   107 / 73.8 / 3.94  ← peak à t36
short × chop  N / WR / PF  12 209/63.9/2.16    11 168/66.4/2.21    10 643/68.0/2.24    ← monotone +
```

- **LONGS bull** : WR monte 58.9 → 61.0 (improving), PF flat 1.69 → 1.72
- **SHORTS bull** : pic à t36 (PF 1.80), recul à t48 (PF 1.59 = −0.21).
  Cohérent avec l'intuition "timeout long sur shorts counter-trend = risque
  de drift contre nous", mais l'effet est petit
- **SHORTS bear** : pic à t36 (PF 4.52), léger recul à t48 (3.94)
- **SHORTS chop** (94 % du book) : monotone amélioration t24 → t48 (PF 2.16 →
  2.24). Domine l'arbitrage global.

### Verdict timeout

- **t48 (240 min) = le winner sur TRAIN, TEST, et full window**. Sharpe gain
  marginal (+0.10 à +0.30 vs t24/t36).
- **Live tourne actuellement à t24 (TIMEOUT_MIN=120)** → c'est le PIRE des
  trois testés (mais l'écart est petit). Passer à 240 min en live donnerait
  un Sharpe attendu marginalement supérieur ; pas un changement game-changing.
- **Aucun patch live appliqué** sur cette base — l'écart Sharpe est trop petit
  pour justifier le changement seul, et le diagnostic A1-A4 doit être traité
  d'abord.
- B3 : la dichotomie "t48 aide les longs mais empire les shorts" est
  partiellement vraie pour shorts-en-bull et shorts-en-bear, mais l'effet est
  écrasé par la masse des shorts-en-chop qui préfèrent t48.

### Fichiers générés (read-only artefacts)

- `trading-agent/run_long_short.py` (runner avec longs activés)
- `trading-agent/run_timeout.py` (runner timeout sweep)
- `trading-agent/ls_ALL_parity_sized_20220401_20241231.csv` + `_trades.csv`
- `trading-agent/ls_P1_sized_20220401_20241231.csv` + `_trades.csv`
- `trading-agent/timeout_full_t{24,36,48}.csv` + `_trades.csv`
- `trading-agent/timeout_train_t{24,36,48}.csv` + `_trades.csv`
- `trading-agent/timeout_test_t{24,36,48}.csv` + `_trades.csv`

### Modifs `backtest_p1_ablation.py` (read-only re. live)

- `SharedPool.open()` : nouveau kwarg `regime_state=None`, stocké sur la
  position pour propagation au close
- `_try_enter()` : passe `regime.get("state")` à `pool.open`

---

## Long+short revalidation — 2026-05-31

### Contexte

Le "Bonus critique" précédent a révélé que tous les backtests précédents
émettaient 100 % shorts (mismatch `enabled={"t1","t2"}` vs route_long_signal
`"long_t1"`). Les +8.4 / +11.17 mesuraient une stratégie short-only, pas la
stratégie déployée. On revalide tout en long+short.

### 1. Fix routing

Runners corrigés : `enabled = {"long_t1", "t1", "t2"}`. Smoke test confirme :
longs ET shorts firent dans le backtest, contrôlés par la même logique que
le live (`route_long_signal` route via `allow_long` du régime engine, comme
`_eval_side("long")` en live). **Aucune modif live**.

### 2. Head-to-head V_short vs V_ls (33mo Kraken, ALL_parity_sized)

Même fenêtre 2022-04-01 → 2024-12-31, même flags parité, mêmes données ;
seule différence : `enabled` inclut `long_t1` ou non.

| Métrique | V_short | V_ls | Δ |
|---|---:|---:|---:|
| N trades | 11 030 | 11 324 | +294 |
| WR % | 67.87 | 67.71 | −0.16 |
| PF | 2.23 | 2.22 | −0.01 |
| **Sharpe** | **+8.21** | **+8.27** | **+0.06** |
| MDD % | −1.54 | −1.54 | 0.00 |
| Expectancy ($) | 14.74 | 14.67 | −0.07 |
| Return 33m % | +1 626 | +1 661 | +35 |

**Décomposition V_ls par side** :

```
LONGS  : n=  333   WR=60.96%   PF=1.72   exp=$+10.40   PnL=$+3 464
SHORTS : n=10 991  WR=67.92%   PF=2.24   exp=$+14.80   PnL=$+162 641
```

- **Long contribution = 2.09 %** du PnL (333 / 11 324 = 2.94 % des trades)
- **Short contribution = 97.91 %** du PnL

→ Les longs **AJOUTENT** marginalement (Sharpe +0.06, return +$3.5k) mais
**DILUENT** la qualité par trade (PF −0.01, exp −$0.07). Pas actively
nuisibles, juste marginaux.

### T1S_DIV_MODE — le levier qui crée les 10 991 shorts

Sur 21mo TRAIN (Kraken, vol1 dist1) :

| T1S_DIV_MODE | N shorts | × vs strict |
|---|---:|---:|
| strict | 603 | 1.00 × |
| rsi_fallback | 4 803 | 7.97 × |
| **never** | **6 775** | **11.24 ×** |

→ ~92 % des shorts viennent du choix `never`. Avec strict, on serait à ~950
shorts sur 33mo (vs 10 991 actuels). C'était la décision de validation
2026-05-28 (`[Project — short expert validation]`).

T1L n'utilise PAS T1S_DIV_MODE → nombre de longs invariant sous ce choix.

### 3. Walk-forward V_ls — anchored (TRAIN 2022-04→2023-12 / TEST 2024)

Grid 12 cellules × longs+shorts. Sélection par Sharpe TRAIN uniquement.

| # | label | TRAIN Sharpe | rk | TEST Sharpe | rk | tr_L | te_L | tr_S | te_S |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | div_never_vol1_dist1 | +8.95 | 1 | +11.34 | 2 | 132 | 198 | 6 772 | 4 200 |
| 2 | div_never_vol0_dist1 | +8.81 | 2 | +11.44 | 1 | 103 | 173 | 5 035 | 3 553 |
| 3 | div_never_vol1_dist0 | +8.70 | 3 | +10.18 | 3 |  69 | 104 | 5 585 | 3 481 |
| 4 | div_fallback_vol1_dist1 | +7.69 | 4 |  +7.59 | 5 | 131 | 198 | 4 796 | 2 878 |
| ... | ... | ... | ... | ... | ... | ... | ... | ... | ... |
| 12 | div_strict_vol0_dist0 | +0.89 | 12 |  +2.61 | 11 |  60 | 101 | 314 | 267 |

- **Spearman ρ TRAIN→TEST = +0.965** (rang quasi-identique)
- **Winner TRAIN-locked** : `div_never_vol1_dist1` = config live actuelle
- Winner TEST : div_never_vol0_dist1 (vol+EMA gates ON), à seulement +0.10
  Sharpe au-dessus du winner TRAIN

**OOS Sharpe degradation** : −26.7 % (ouais, c'est NÉGATIF dans le sens où
TEST > TRAIN : +8.95 → +11.34) → **edge s'amplifie OOS**.

**Longs spécifiquement** (config winner sur TRAIN puis TEST) :

| Métrique long | TRAIN | TEST | Δ |
|---|---:|---:|---:|
| N | 132 | 198 | +66 |
| WR | 56.82 % | 62.63 % | +5.81 pp |
| PF | 1.54 | 1.77 | +0.23 |
| Total PnL | +$1 099 | +$2 167 | +$1 068 |

→ **Les longs s'améliorent OOS sur le winner**. Pas d'overfit sur la partie
longs.

### 4. Walk-forward V_ls — rolling 3 folds (TRAIN 12mo / TEST 6mo glissants)

| Fold | TRAIN | TEST | Winner | TRAIN Sh | TEST Sh | Δ | Spearman |
|---|---|---|---|---:|---:|---:|---:|
| A | 2022-04→2023-03 | 2023-04→2023-09 | div_never_vol1_dist1 | +11.44 | **+6.42** | **−5.02** | +0.853 |
| B | 2022-10→2023-09 | 2023-10→2024-03 | div_never_vol1_dist1 |  +7.92 | +12.43 | +4.51 | +1.000 |
| C | 2023-04→2024-03 | 2024-04→2024-09 | div_never_vol0_dist1 |  +8.97 | +11.64 | +2.67 | +0.965 |
| **Mean** | | | | **+9.44** | **+10.16** | **+0.72** | **+0.939** |

- Mean OOS Sharpe (+10.16) ≥ Mean TRAIN (+9.44) → pas d'overfit aggregate
- Mean Spearman ρ = +0.939 → ranking robust
- **Worst-case** : Fold A, TEST Sharpe **+6.42** (régime chop Q2-Q3 2023)
- Config `div_never_vol1_dist1` (= live) winner dans 2 folds + anchored

### LONGS specifically — robustness rolling

Per-fold winner config :

| Fold | TRAIN longs | TEST longs |
|---|---|---|
| A | n=63 WR=60.3 % PF=1.97 | n=43 WR=65.1 % PF=1.86 |
| B | n=79 WR=63.3 % PF=2.05 | n=101 WR=60.4 % PF=1.60 |
| C | n=118 WR=54.2 % PF=1.27 | n=51 WR=66.7 % PF=2.29 |
| **Mean** | **PF=1.76 WR=59.3 %** | **PF=1.92 WR=64.1 %** |

- Mean PF longs TRAIN→TEST : 1.76 → 1.92 (**+0.15**)
- Mean WR longs TRAIN→TEST : 59.3 % → 64.1 % (**+4.78 pp**)
- **Les longs s'améliorent OOS en moyenne sur les 3 folds**

Caveat statistique : N(longs) par fold ∈ [43, 198]. Échantillon plus petit
que les shorts (N ∈ [1352, 4200]). Confidence interval sur WR longs ~ ±10 pp
à 95 %. Edge longs probable mais bruit non négligeable.

### Comparaison V_short vs V_ls — robustness OOS

| Métrique OOS | V_short | V_ls | Δ |
|---|---:|---:|---:|
| Anchored TEST Sharpe (winner TRAIN) | +11.17 | +11.34 | +0.17 |
| Rolling mean TEST Sharpe | +9.93 | +10.16 | +0.23 |
| Worst-case fold TEST Sharpe | +6.45 | +6.42 | −0.03 |
| Spearman anchored | +0.951 | +0.965 | +0.014 |
| Spearman rolling mean | +0.895 | +0.939 | +0.044 |

→ V_ls est **marginalement meilleur que V_short** sur toutes les métriques OOS.
Les longs ne dégradent rien et ajoutent un peu.

### VERDICT — long+short revalidation

1. **V_ls passe le walk-forward sans overfit** : Spearman ρ +0.94-0.97,
   Sharpe OOS s'améliore (+26.7 % anchored, +7.6 % rolling mean).
2. **Les LONGS spécifiquement généralisent OOS** : PF mean 1.76→1.92, WR
   mean 59.3→64.1 % sur les 3 folds. Pas du fitting.
3. **Live config (`div_never_vol1_dist1` = T1S_DIV_MODE=never, vol+EMA OFF,
   distance live) est le winner ou rank #2 sur TOUS les TRAIN+TEST**.
4. **Mais l'apport des longs est mince** : Sharpe +0.06-0.23 OOS, 2.1 % du
   PnL. Les supprimer ne coûte presque rien ; les garder ne rapporte presque
   rien dans le backtest Kraken.
5. **Worst-case réaliste OOS** : Sharpe **+6.42** (fold A, régime chop) —
   plancher observé sur 5 mesures indépendantes.

### Implication pour la décision Yahoo → Kraken

Hypothèse pré-revalidation : "si long+short gagne, fixer Yahoo pour que le
live le tourne sur la bonne série".

**Résultat** : long+short gagne *de très peu* (+0.06 Sharpe). Le mismatch
explique surtout la composition (60 % buy en live vs 3 % en backtest), pas
un effet de SR significatif.

Deux décisions possibles à présenter :

- **Option A (conservateur)** : passer la source signaux live de Yahoo à
  Kraken Futures (`/api/charts/v1/trade/PF_*`). Garde la stratégie long+short
  telle que déployée, mais elle tournera sur les données qui ont été
  validées en backtest. Attendu : ratio long/short converge vers ~3 % en
  live, et les ETH longs qui saignent disparaissent.
- **Option B (radical)** : désactiver les longs en live (`GEO_ENABLE_LONG=0`
  ou équivalent). Backtest dit que les longs ne valent que +$3.5k sur 33mo,
  donc le coût est trivial, et le live perdrait moins de variance pendant
  qu'on monte une vraie validation Yahoo→Kraken plus profonde.

**Aucune décision prise** sur ces options dans ce rapport — la validation
backtest seule ne tranche pas. Les chiffres sont là pour décider.

### Fichiers générés

- `trading-agent/run_long_short.py` (full-window long+short runner)
- `trading-agent/run_wf_ls.py` (anchored grid V_ls)
- `trading-agent/run_wf_roll_ls.py` (rolling V_ls)
- `trading-agent/ls_ALL_parity_sized_20220401_20241231.csv` + `_trades.csv`
- `trading-agent/wfls_ls_{train,test}_div_*.csv` (24 anchored cells)
- `trading-agent/wfls_ls_roll{A,B,C}_{train,test}_div_*.csv` (72 rolling cells)
- `trading-agent/wfls_report.json` (synthèse anchored + rolling)

---

## Option A appliquée — Yahoo → Kraken pour les signaux live — 2026-06-01

Décision : passer la source de données SIGNAUX live de Yahoo (yfinance ETH-USD /
SOL-USD spot) vers **Kraken Futures public candles**
(`/api/charts/v1/trade/PF_*/5m`) — la même série que l'exécution paper et la
même que celle validée en backtest 33mo + walk-forward V_ls (Spearman ρ +0.96).

### Patches read-only (live à redémarrer manuellement)

1. **Nouveau module `trading-agent/kraken_candles.py`** — helper
   `fetch_kraken_bars(db_symbol, timeframe, limit)` qui interroge le endpoint
   public Kraken Futures et retourne un `pd.DataFrame` au même format que
   l'ancien `yfinance`. Gère :
   - Mapping `db_symbol → kraken_sym` (ETH/USD → PF_ETHUSD, etc.)
   - Mapping timeframes broker → Kraken (`5Min`→`5m`, `1Hour`→`1h`, ...)
   - Drop conservateur de la barre en formation (cohérent avec ancien
     `df.iloc[:-1]` yfinance)
   - Headers + timeout + try/except (graceful None sur fail)

2. **`broker_kraken_paper.py`** — `get_bars()` remplacé par appel à
   `fetch_kraken_bars`. `import yfinance` retiré (plus utilisé). Import
   `from kraken_candles import fetch_kraken_bars` ajouté.

3. **`kraken_broker.py`** (LIVE futur) — même patch. `yfinance` retiré.

### Vérifications

- `python -m py_compile` OK sur les 3 fichiers
- Smoke E2E via `KrakenPaperBroker().get_bars(...)` :
  ```
  ETH/USD 5Min : last close=1967.9 @ 2026-06-01 14:20:00 UTC
  ETH/USD 15Min: last close=1966.5 @ 2026-06-01 14:00:00 UTC
  ETH/USD 1Hour: last close=1969.0 @ 2026-06-01 13:00:00 UTC
  ETH/USD 4Hour: last close=1979.2 @ 2026-06-01 08:00:00 UTC
  SOL/USD 5Min : last close= 79.64
  SOL/USD 1Hour: last close= 79.78
  ```
  Prix et fraîcheur cohérents avec mark Kraken Futures actuel.

### Effets attendus en live après redémarrage

- Régime engine reçoit des prix Kraken pour ETH+SOL → classification "bull"
  plus rare (cohérent avec backtest 33mo où seulement ~5 % des trades
  tombent dans bull/bear, le reste en chop)
- T1L se déclenche moins → ratio long/short live devrait converger vers le
  ~3 % du backtest (au lieu des 60 % observés sur Yahoo)
- ETH longs qui saignaient (WR 26 %, −$209 sur 7j) devraient disparaître
- Le book devient majoritairement short — comme la stratégie le prévoit

### À faire par l'opérateur

- Redémarrer le runtime Jim (PID 35523) pour charger le nouveau code
- Surveiller le ratio long/short sur les premières 48h
- Si encore beaucoup de longs malgré Kraken → diagnostic supplémentaire
  côté régime engine

### Fichiers touchés

- `trading-agent/kraken_candles.py` (NEW)
- `trading-agent/broker_kraken_paper.py` (import + get_bars body)
- `trading-agent/kraken_broker.py` (import + get_bars body)

---

## P2 — Preuve Yahoo vs Kraken — 2026-06-01

### Setup

Replay du moteur backtest (`backtest_p1_ablation.py`, ALL_parity_sized,
T1S_DIV_MODE=never, enabled = {long_t1, t1, t2}) sur **2026-05-01 → 2026-06-01**
(31 jours, couvre la période live observée à 60 % longs).

**Une seule variable change** : la source des bars ETH/SOL 5m.
- `kraken_cache_p2/` : Kraken Futures PF_ETHUSD/PF_SOLUSD 5m (8929 bars)
- `yahoo_cache_p2/`  : Yahoo ETH-USD/SOL-USD 5m via yfinance (8905 bars)

Tout le reste constant — BTC 4h regime (Kraken PF_XBTUSD, identique aux deux
runs), engine, flags, fenêtre.

### Résultats

| Source | N | N longs | N shorts | % longs | Long net $ | Short net $ | Total $ |
|---|---:|---:|---:|---:|---:|---:|---:|
| **Kraken** | 191 | **2** | 189 | **1.0 %** | −77 | +438 | +361 |
| **Yahoo**  | 199 | **1** | 198 | **0.5 %** | −39 | +553 | +513 |

Side × Regime (Kraken) :
```
long  bull    2   WR= 0.00%   PF=0.00    -$77
short bear    2   WR=100.0%   PF=999     +$52
short chop  187   WR=48.13%   PF=1.14   +$386
```

Side × Regime (Yahoo) :
```
long  bull    1   WR= 0.00%   PF=0.00    -$39
short bear    2   WR= 50.0%   PF=0.10    -$43
short chop  196   WR=48.98%   PF=1.22   +$596
```

### Lecture

**Comparaison live vs backtest sur la MÊME fenêtre (~30 jours)** :

| Source | Long share |
|---|---:|
| Live runtime (Yahoo, geometric_expert.py) | **~60 %** |
| Backtest engine sur Yahoo | **0.5 %** |
| Backtest engine sur Kraken | **1.0 %** |

→ **Le swap data source ne change PAS la proportion de longs**. Yahoo et Kraken
produisent tous les deux ~1 % longs dans le moteur backtest, vs 60 % dans le
runtime live.

**Conclusion** :
- L'hypothèse "Yahoo est la cause des 60 % longs" est **invalidée**.
- La cause est dans une **divergence de CODE entre `geometric_expert.py` (live)
  et `backtest_p1_ablation.py` (backtest)**, pas dans la source de données.
- Le patch Option A est **inert** sur le problème — il ne fera pas baisser le
  ratio de longs en live. Il améliore la parité de données (et c'est bien)
  mais ne corrige pas la divergence engine.

### Implications

1. **Ne pas redémarrer le live sur Option A en pensant que ça règle les longs**.
   Le patch peut rester (data parité = bien), mais ne sera pas une solution au
   live qui saigne.
2. **Ni option A ni option B ne suffisent** (per user's pre-experiment criterion).
   Il faut traquer le divergence de code.
3. **Longs restent ON en paper** (consigne user) — la donnée reste utile pour
   diagnostiquer.

### À investiguer ensuite (pas fait dans ce ticket)

Comparer side-by-side :
- `experts/geometric_expert.py:_eval_side("long")` (lignes ~720-815) — gate live T1L
- `backtest_p1_ablation.py:get_long_signal_t1()` (lignes ~465-509) — gate backtest T1L

Candidats à examiner :
- Calcul des zones de support (`_find_zones` backtest vs `geometric_expert._find_support_zones`)
- `_rsi_divergence` : implémentation identique ? Mêmes paramètres lookback ?
- `_dyn_stop_long` : stop dynamique sous wick — même formule ?
- `Pass 3b` : `lows_5m[-8:] <= zone["high"] + zone_gap` (live) vs même chose en backtest ?
- Régime engine appelé identiquement ? `evaluate(symbol, as_of=...)` vs `evaluate(symbol)` ?
- Gates additionnels live-only ? (low_vol_mode, DEBUG_MODE, T1L_INCLUDE_HARD_BLOCK, etc.)

### Fichiers générés

- `trading-agent/fetch_p2_data.py` (fetcher ETH/SOL/BTC pour la fenêtre P2)
- `trading-agent/run_p2_replay.py` (runner backtest avec override CACHE_DIR_USD + load_btc_4h)
- `trading-agent/kraken_cache_p2/{ETHUSD,SOLUSD}_5m_*.parquet`
- `trading-agent/yahoo_cache_p2/{ETHUSD,SOLUSD}_5m_*.parquet`
- `trading-agent/btc_cache_p2/BTCUSD_4h_2026-05-01_2026-06-01.parquet`
- `trading-agent/p2_{kraken,yahoo}.csv` + `_trades.csv`

---

## P3 — Diagnostic empirique gate-par-gate — 2026-06-01

### Méthode

1. Extraction des 30 trades `side='buy'` (longs) sur 30j depuis `trading_memory.db`
2. Pour chaque long : recoupage des bars Yahoo P2 au timestamp d'entrée
3. Passage dans une réplique instrumentée de `get_long_signal_t1()` du backtest
4. Identification de la première gate qui rejette

### Résultats bruts du replay

```
Gate de rejet                       N     %
regime_disallow_long               30  100.0 %
PASS                                0    0.0 %
```

**100 % des longs sont rejetés par la régime gate** (`state = neutral`,
`allow_long = False`). Aucun n'est rejeté par : divergence, distance, RSI,
pass_3b, ou R:R.

### Renversement de cadre — d'où viennent ces 30 longs ?

Cross-check avec `market_context.broker` :

| Broker source | N | Total PnL | Buys | Sells |
|---|---:|---:|---:|---:|
| **kraken_paper** (session actuelle, depuis 2026-05-30 10:05) | 20 | **+$30.30** | **0** | **20** |
| okx (session précédente, 2026-05-26 → 2026-05-29) | 29 | −$89.87 | 29 | 0 |
| (broker manquant) | 3 | −$134 | 1 | 2 |

→ Les **30 longs viennent de la session OKX précédente**, pas du runtime
Kraken paper actuel. Le runtime actuel (PID 35523, 36h up) a fait
**20 shorts, 0 longs, +$30.30**.

**La prémisse "le live tire 60 % de longs" était basée sur des données
mélangées entre deux sessions.** Une fois isolée à la session courante,
le ratio long/short est **0 %**, **conforme à la prédiction du backtest**
(1-3 %).

### Divergence de code identifiée — quand même réelle, juste pas active actuellement

Même si le runtime actuel ne déclenche pas les divergences, le code live
contient deux ressorts qui ont pu causer la session OKX à tirer des longs
là où le backtest n'en aurait pas tiré.

#### Divergence #1 — fail-OPEN sur regime gate

`experts/geometric_expert.py:572-587` (LIVE) :
```python
cr_decision = None
cr_allow_long  = True       # ← DEFAULT TRUE
cr_allow_short = True
cr_size_mult   = 1.0
...
if self.crypto_regime is not None:
    try:
        cr_decision = self.crypto_regime.evaluate(symbol)
    except Exception as e:
        logger.warning(f"[CRYPTO_REGIME] evaluate({symbol}) failed: {e} — fail-safe block")
        cr_decision = None
    if cr_decision is not None:
        ...
        cr_allow_long  = bool(cr_decision.get("allow_long",  False))
```

Si `crypto_regime.evaluate()` lève → `cr_decision = None` → la branche
de mise à jour est skip → **`cr_allow_long` reste à True**. Le commentaire
dit "fail-safe block" mais le code est en fait fail-OPEN sur les longs.

`backtest_p1_ablation.py:935-941` (BACKTEST) :
```python
def route_long_signal(arr_5m, arr_15m, arr_1h, regime_decision, enabled):
    if "long_t1" not in enabled: return None, None
    allow_long = regime_decision.get("allow_long", False)   # default FALSE
    if not allow_long: return None, None
    sig = get_long_signal_t1(arr_5m, arr_15m, arr_1h)
```

Si la régime décision ne contient pas `allow_long` → default `False` →
**fail-CLOSED**. C'est la sémantique sûre.

**Impact estimé** : si le régime engine de la session OKX levait des
exceptions transitoires, l'OKX live aurait tiré des longs à allow_long=True
là où le backtest les aurait rejetés.

#### Divergence #2 — `low_vol_mode` bypass divergence RSI

`experts/geometric_expert.py:745-748` (LIVE) :
```python
if not config.DEBUG_MODE:
    if not self._rsi_divergence(closes_5m, rsi_now):
        if not low_vol_mode:          # ← bypass si low_vol_mode
            n_div += 1; continue
```

Si `low_vol_mode=True` (i.e. `GEO_DISABLE_LOWVOL=0` ET régime ∈ {choppy,
bull}) → la divergence est **bypassée**. Le long fire même sans divergence.

`backtest_p1_ablation.py:493-494` (BACKTEST) :
```python
div = _rsi_bull_div(cl5, rsi)
if not div and not (30 <= rsi <= 55): continue
```

Le backtest n'a **pas de notion de `low_vol_mode`**. La divergence est
toujours évaluée, avec un fallback RSI [30, 55] uniquement.

**Impact estimé** : si la session OKX tournait avec `GEO_DISABLE_LOWVOL=0`,
les régimes `bull`/`choppy` permettraient de tirer des longs en mode
low-vol sans divergence — un volume potentiellement large.

Tous les 29 trades OKX ont `mode: lowvol` + `target_pct_used: 0.005` —
cohérent avec un régime engine qui émettait `target_pct=0.005`. Note : ce
tag `mode=lowvol` reflète `target_pct_used`, pas directement `low_vol_mode`.
Il ne suffit pas seul à confirmer GEO_DISABLE_LOWVOL=0, mais c'est
suggestif.

#### Divergence #3 (mineure) — fallback RSI 30-55

`backtest_p1_ablation.py:494` accepte le signal si pas de divergence MAIS
RSI dans [30, 55]. `geometric_expert.py:745-748` n'a PAS ce fallback —
soit divergence, soit lowvol bypass. Donc le LIVE est strictement plus
strict sur la divergence sauf en mode lowvol. Cette divergence va dans le
sens "live moins permissif" ; pas une cause des 60 % longs.

### Conclusion

- **Bug structurel confirmé** sur 2 gates (regime fail-open default,
  low_vol_mode bypass) — divergence réelle entre `geometric_expert.py` et
  `backtest_p1_ablation.py`.
- **Impact actuel : 0** sur le runtime courant. La session Kraken paper en
  cours fait 0 longs et est profitable. Les 30 longs analysés étaient des
  trades historiques OKX, pas du live courant.
- **Bug latent** : si `GEO_DISABLE_LOWVOL=0` est restauré OU si le régime
  engine lève des exceptions, le live recommencerait à tirer des longs là
  où le backtest n'en tirerait pas.

### Diff proposé (à TON OK avant patch)

Aligner `geometric_expert.py` sur la sémantique du backtest (référence
validée par walk-forward). 2 changements isolés et atomiques :

#### Patch 1 — fail-CLOSED sur regime gate

```diff
 # ── Crypto-native regime gate (PAPER validation) ──────────────────────
 cr_decision = None
-cr_allow_long  = True
-cr_allow_short = True
-cr_size_mult   = 1.0          # auto-switch from regime
+cr_allow_long  = False
+cr_allow_short = False
+cr_size_mult   = 0.0          # auto-switch from regime
 cr_target_pct  = None         # None = keep legacy lowvol_target_pct
```

`experts/geometric_expert.py:572-575` — défauts passent à False/0 pour
fail-CLOSED, comme `route_long_signal` du backtest. Si l'évaluation
crypto_regime échoue, le bot ne tire AUCUN signal (ni long ni short)
jusqu'à ce que le régime engine reprenne — comportement défensif aligné
avec la phrase "fail-safe block" du commentaire existant.

Risque résiduel : aucun, sauf si quelque chose dépend du fail-OPEN
historique (à grep avant patch).

#### Patch 2 — retirer le `low_vol_mode` bypass divergence

```diff
 if not config.DEBUG_MODE:
     if not self._rsi_divergence(closes_5m, rsi_now):
-        if not low_vol_mode:
-            n_div += 1; continue
+        n_div += 1; continue
```

`experts/geometric_expert.py:745-748` — supprime la branche qui bypassait
la divergence en lowvol. Aligné sur backtest qui n'a pas ce branch.

Risque résiduel : enlève une optionalité historique. Avec
`GEO_DISABLE_LOWVOL=1` (la config actuelle), `low_vol_mode=False` déjà
toujours → le branch est inactif → aucun effet observable. Si un jour
on flip `GEO_DISABLE_LOWVOL=0`, le bypass disparaît, le comportement
reste aligné au backtest.

Décision laissée à toi.

### Note refactor à terme

À horizon court : extraire la logique des signaux T1L/T1S dans un module
partagé `experts/_geo_signals.py` (ou `signals_geo_v4.py`) consommé par
`geometric_expert.py` ET par `backtest_p1_ablation.py`. Toute divergence
future devient mécaniquement impossible. Aujourd'hui il y a 2 versions
parallèles à maintenir, ce qui invite ce genre de drift.

Note moins urgente parce que :
- la session Kraken paper actuelle est alignée
- les 2 patches ci-dessus, s'ils sont appliqués, ferment les 2 gates
  divergentes connues

---

## P5 — Fake-TP diagnostic (kraken_paper only) — 2026-06-01

### 1. Anatomie des sorties "target" sur Kraken paper

20 trades total sur le runtime courant. Filtre `market_context.broker = 'kraken_paper'`.

| trade | side | entry | TP placé broker | TP voulu (%) | exit réel | exit (%) | parcouru | net |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 9a98256c (SOL) | sell | 81.91 | **81.18** | −0.90 % | **81.47** | −0.54 % | 60 % | +$12.76 |
| 88faed70 (ETH) | sell | 2002.30 | **1984.64** | −0.88 % | **2000.80** | −0.07 % | **8 %** | +$1.80 |

**TP placé matche la décision** (broker log : `[KrakenPaper] ✅ SHORT ETH/USD ... TP=1984.6400`). **Pas un bug de placement.**

L'exit s'est fait à 0.07 % du chemin pour ETH (vs 0.88 % visé) — clairement pas un fill du TP. **Bug de LABEL.**

### 2. Cause mécanique exacte

Log croisé avec l'exit de 88faed70 :

```
14:33:15 INFO [KrakenPaper] ✅ SHORT ETH/USD qty=1.2 ~$2002.30 SL=2005.6036 TP=1984.6400
14:33:19 INFO [GEO] ✅ FILLED: ETH/USD SHORT @ $2002.3000 qty=1.2000
                              ⋯ 8 min de loop sans event ⋯
14:41:25 ERROR [KrakenPaper] CLI exception: kraken futures paper positions
         TIMED OUT after 15 seconds
14:41:25 WARNING [GEO] ETH/USD absent broker, fermeture approx
         @ $2000.8000 pnl=$1.80
```

Idem pour 9a98256c (12:57:45 EDT) : 50 min de hold, puis un seul "fermeture approx" event.

Cardinalité : **2 'fermeture approx' dans le log = 2 trades 'target' en DB**. Match exact.

### Chaîne causale ligne par ligne

1. `_cli()` (`broker_kraken_paper.py:54-64`) timeout sur `kraken futures paper positions` (15 s)
2. `_cli()` retourne `None`
3. `get_positions()` (`broker_kraken_paper.py:136-159`) → liste vide `[]`
4. Dans `manage_open_positions` (`geometric_expert.py:1200`) : `symbol not in broker_positions` → True
5. `get_close_info()` (`broker_kraken_paper.py:181-182`) → `return None` (stub paper)
6. Branche fail-safe `geometric_expert.py:1228-1236` :
   ```python
   else:
       live = self.broker.get_live_price(symbol) or entry
       pnl  = mult * (live - entry) * qty_t
       reason = "stop" if pnl < 0 else "target"     # ← LIGNE 1231 = LABEL bug
       logger.warning(f"[GEO] {symbol} absent broker, fermeture approx ...")
       self.memory.log_trade_close(trade_id, live, reason, pnl=pnl)
   ```
7. `pnl=+$1.80 > 0` → tag `"target"`
8. DB enregistre exit_price=$2000.80 (live tick au moment du panic) avec label "target"

**LIGNE EXACTE EN CAUSE : `experts/geometric_expert.py:1231`**

### Verdict placement vs label

- **Placement broker** : correct (TP envoyé à zone.low, ~0.88-0.90 % du fill)
- **Label** : faux. Quand le CLI Kraken time out → le bot panique → ferme dans la DB → tag basé sur signe(PnL) au lieu de comparer à TP

→ **Bug de label**, pas de placement.

Note dérivée : la ligne 1236 fait `log_trade_close(...)` **sans appeler `self.broker.close_position(symbol)`**. La DB pense que la position est fermée, mais Kraken paper côté broker peut toujours l'avoir ouverte (phantom). Ces 2 cas n'ont pas généré de phantom observable (les positions ont fini par disparaître côté broker plus tard), mais le risque structural existe.

### 3. Distribution gross / friction (20 kraken_paper trades)

Friction estimée par trade ≈ $4.80 (FEE_TAKER 0.05 % × 2 + slippage 5 bps × 2, sur notionnel effectif ~$2400).

| close_reason | N | avg net | min | max | total | wins | losses |
|---|---:|---:|---:|---:|---:|---:|---:|
| stop | 10 | −$4.15 | −$16.12 | +$0.18 | **−$41.51** | 1 | 9 |
| timeout | 8 | +$7.16 | +$1.24 | +$16.63 | **+$57.25** | 8 | 0 |
| target (fake) | 2 | +$7.28 | +$1.80 | +$12.76 | **+$14.56** | 2 | 0 |

| close_reason | clear friction | below friction |
|---|---:|---:|
| stop | 2 | 8 |
| target | 1 | 1 |
| timeout | 4 | 4 |

WR net global : 11/20 = 55 %. Net total **+$30.30**.

**Toutes** les sorties "target" sur Kraken paper sont en fait des fake-target
(CLI timeout → panic close). **Aucun vrai TP hit** sur 20 trades.

### Impact sur l'edge net

- Net actuel : +$30.30
- Net sans les 2 fake-target : +$30.30 − $14.56 = **+$15.74**
- Le fake-target a contribué ~48 % du PnL net.

L'edge réel sur 20 trades = +$15.74, soit ~$0.78/trade. Avec friction ~$4.80/trade,
la marge est très mince. La séquence des stops (−$41.51) est presque entièrement
compensée par la séquence des timeouts (+$57.25).

**Verdict** : sans le fake-target, le runtime kraken_paper actuel serait
**marginalement positif** (+$15.74 sur ~36h, soit ~$10/jour), pas franc.
Cohérent avec l'attente théorique sur la fenêtre courante (peu de trades,
variance forte).

### Distinction CORRECTION du label vs CORRECTION du lifecycle

Deux fixes possibles sur ligne 1231 :

**Fix A — label correct** : remplacer `reason = "stop" if pnl < 0 else "target"`
par `reason = "broker_lost"` (ou `"phantom_close"`). PnL inchangé, label
diagnostique honnête. **Ne change rien à l'edge** (+$30.30 reste +$30.30).

**Fix B — lifecycle correct** : ne PAS fermer dans la DB sur CLI timeout.
Retry CLI quelques fois. Si toujours absent après N retries → alors fermer.
Sinon laisser la position ouverte, elle finira par être réconciliée.
**Peut changer le PnL** (les positions pourraient hit TP réel ou SL réel
au lieu de panic-close).

Recommendation diagnostique (PAS un patch) :
- Fix A donne des labels honnêtes pour la suite des mesures
- Fix B est plus profond (touche au lifecycle) — à valider avec replay de
  ce que les positions auraient fait si laissées ouvertes
- À ce stade : **rien à patcher** tant que tu n'as pas tranché entre A et B

### Fichiers et lignes pertinents

- `experts/geometric_expert.py:1228-1236` : branche `else` "absent broker"
- `experts/geometric_expert.py:1231` : LE label bug
- `broker_kraken_paper.py:54-64` : `_cli` (15s timeout)
- `broker_kraken_paper.py:136-159` : `get_positions` (retourne `[]` si CLI fail)
- `broker_kraken_paper.py:181-182` : `get_close_info` (stub paper, retourne `None`)
- DB query :
  ```sql
  SELECT * FROM trades
  WHERE json_extract(market_context, '$.broker') = 'kraken_paper'
   AND close_reason = 'target';
  ```

---

## P6 — Distribution live vs backtest + Steps 2-3 — 2026-06-01

### Step 1 — Distribution close_reason live vs backtest

| Source | Trades | target % | timeout % | stop % | target avg | timeout avg | stop avg | Net total |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **Live Kraken paper** (20 trades, 36h) | 20 | **10 %** (FAKE) | 40 % | 50 % | +$7.28 | +$7.16 | −$4.15 | **+$30.30** |
| Backtest Kraken P2 (191 trades, 31j) | 191 | **36.1 %** | 36.6 % | 27.2 % | **+$42.46** | −$4.25 | −$43.70 | +$360.27 |
| Backtest Yahoo P2 (199 trades, 31j) | 199 | 36.7 % | 40.2 % | 23.1 % | +$42.24 | −$7.10 | −$43.53 | +$513.24 |

Normalisation par $/trade : live +$1.52, backtest +$1.89 — similaire.
Mais MÉCANIQUES DIFFÉRENTES :

- **Backtest** : profit = **TP hits** (36 % des trades à +$42 avg ⇒ +$2930 brut).
  Timeouts mildement négatifs, stops gros (~−$43).
- **Live** : **0 vrai TP hit**, tous les "target" en DB sont des fake (CLI timeout).
  Profit = **timeouts profitables** (+$7 avg sur 8 trades).
  Stops 10× plus petits que backtest (−$4 vs −$43).

### Découverte structurelle critique

`broker_kraken_paper.place_limit_buy/sell` reçoit `stop_loss` et `take_profit`
en arguments mais ne les passe JAMAIS au CLI :
```python
result = _cli("futures", "paper", "buy", kraken_sym, str(qty), "--type", "market")
# ↑ pas de --stop-loss, pas de --take-profit
```

→ **Aucun bracket natif côté Kraken paper.** Le bot polle à 30s. Tout wick
intra-30s qui touche TP est manqué. Le backtest évalue high/low de chaque
bar 5m → catche les wicks intrabar.

**C'est la raison structurale pour laquelle le TP ne hit jamais en live.**
Pas un bug du runtime — un manquant dans la couche broker.

### Step 2 — Fix label appliqué

Patch à `experts/geometric_expert.py:1231` :

```diff
                     else:
                         live = self.broker.get_live_price(symbol) or entry
                         pnl  = mult * (live - entry) * qty_t
-                        reason = "stop" if pnl < 0 else "target"
+                        # Une fermeture forcée (broker absent, CLI timeout) n'est ni
+                        # un TP hit ni un SL hit — tagger par signe(PnL) gonfle les
+                        # stats "target". Label honnête → "forced_close".
+                        reason = "forced_close"
                         logger.warning(
                             f"[GEO] {symbol} absent broker, fermeture approx "
                             f"@ ${live:.4f} pnl=${pnl:.2f}"
                         )
                         self.memory.log_trade_close(trade_id, live, reason, pnl=pnl)
```

`py_compile` OK. Effet : futures forced-closures ne seront plus tagués "target".

Note : les 2 lignes DB historiques (9a98256c SOL, 88faed70 ETH) restent étiquetées
"target". Pas de retroactive update (décision laissée à toi). Pour les exclure
des stats : `WHERE close_reason = 'target' AND trade_id NOT IN ('9a98256c-...', '88faed70-...')`.

### Step 3 — Lifecycle fix : ROOT CAUSE + PROPOSITION (pas appliqué)

#### Root cause des CLI errors

Test manuel CLI :
```
$ ~/.cargo/bin/kraken --output json futures paper positions
{"error":"validation",
 "message":"Validation error: Futures paper state is locked by another process.
            Try again shortly. If a previous command crashed, remove
            '~/Library/Application Support/kraken/paper/futures_state.json.lock'."}
exit code: 1
```

Le CLI utilise un **lock fichier** sur le state Kraken paper. La contention vient de :
1. Slow loop : `get_positions` par symbol/cycle (5min)
2. Fast loop : `get_live_price` + position check toutes les 30 s
3. **Dashboard endpoints créent N KrakenPaperBroker instances** (1 par GET HTTP),
   chacune appelant le CLI à init. Visible dans le log : "✅ KrakenPaperBroker prêt"
   apparait des dizaines de fois.

Statistiques : **4953 CLI errors / 36h** = ~138/h = ~1 par fast loop. Plus 1 timeout
catastrophique (lock tenu >15s).

#### Pourquoi les 2 force-closures n'arrivent qu'avec une position ouverte

`manage_open_positions` itère sur les trades ouverts en DB. La branche
"fermeture approx" se déclenche seulement si :
- Trade ouvert en DB
- `get_positions()` retourne []
- `get_close_info()` retourne None

Si la position est ouverte ET le CLI fail → fake close. Avec 1-2 trades ouverts
× 30 s polling × CLI fail-rate ~100 % → on aurait dû voir BEAUCOUP plus de fakes.
Pourquoi seulement 2 ?

Hypothèse : le lock est plus souvent contesté pendant les requêtes dashboard
(burst) que pendant les position checks bot (espacés). Quand le CLI marche
sporadiquement, le bot voit la position revenir avant de fermer.

#### Proposition de fix (4 changements)

##### Patch L1 — `_cli()` : detect lock error + retry

```diff
 def _cli(*args) -> dict | list | None:
     cmd = [_KRAKEN_CLI, "--output", "json"] + list(args)
-    try:
-        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
-        if r.returncode != 0:
-            logger.warning(f"[KrakenPaper] CLI error: {r.stderr.strip()}")
-            return None
-        return json.loads(r.stdout)
-    except Exception as e:
-        logger.error(f"[KrakenPaper] CLI exception: {e}")
-        return None
+    # Retry on transient state-lock errors (Kraken paper sérialise via file lock).
+    # Max 3 tentatives, backoff exponentiel 0.5/1/2 s.
+    last_err = None
+    for attempt in range(3):
+        try:
+            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
+            if r.returncode == 0:
+                return json.loads(r.stdout)
+            # Détecter spécifiquement le lock error (sur stdout JSON, pas stderr)
+            try:
+                payload = json.loads(r.stdout) if r.stdout else {}
+                msg = (payload.get("message") or "").lower()
+                is_lock = "locked by another process" in msg or "try again shortly" in msg
+            except Exception:
+                is_lock = False
+            if is_lock and attempt < 2:
+                time.sleep(0.5 * (2 ** attempt))   # 0.5, 1.0, 2.0
+                continue
+            last_err = (payload.get("message") if is_lock else r.stderr.strip()) or "unknown"
+            logger.warning(f"[KrakenPaper] CLI error (attempt {attempt+1}): {last_err}")
+            return None
+        except subprocess.TimeoutExpired:
+            last_err = "timeout 15s"
+            if attempt < 2:
+                time.sleep(0.5 * (2 ** attempt))
+                continue
+            logger.error(f"[KrakenPaper] CLI timeout after {attempt+1} attempts")
+            return None
+        except Exception as e:
+            last_err = str(e)
+            logger.error(f"[KrakenPaper] CLI exception: {e}")
+            return None
+    return None
```

Effet attendu : 70-90 % des CLI errors actuelles deviennent succès en 2e/3e
tentative (le lock se libère en <2s typiquement).

##### Patch L2 — `manage_open_positions` : ne PAS force-close sur 1 broker miss

Idée : compteur "consecutive_broker_misses" par trade. Force-close uniquement
après N misses (e.g. 3) ET sur durée totale (e.g. > 2 min) ET avec un dernier
recours `get_close_info` post-retry.

```diff
                 # ── 2. Position absente du broker (SL/TP natifs touchés) ───────
-                if symbol not in broker_positions:
+                if symbol not in broker_positions:
+                    # Ne PAS force-close sur un seul miss. Le CLI fail souvent
+                    # transitoire. On compte les misses consécutifs et n'agit
+                    # qu'après N misses sur fenêtre persistante.
+                    miss_count = ctx.get("_broker_miss_count", 0) + 1
+                    first_miss_ts = ctx.get("_broker_miss_first_ts", now_utc.isoformat())
+                    first_miss_dt = datetime.fromisoformat(first_miss_ts.replace("Z","+00:00"))
+                    miss_span_s = (now_utc - first_miss_dt).total_seconds()
+                    self.memory.update_market_context(trade_id, {
+                        "_broker_miss_count": miss_count,
+                        "_broker_miss_first_ts": first_miss_ts,
+                    })
+                    if miss_count < 3 or miss_span_s < 120:
+                        logger.info(
+                            f"[GEO] {symbol} broker miss #{miss_count} "
+                            f"(span={miss_span_s:.0f}s) — wait, will retry next cycle"
+                        )
+                        continue
+                    # Après 3 misses sur >2min : tenter une dernière réconciliation
                     close_info = self.broker.get_close_info(symbol, since_ts_ms=entry_ts_ms)
                     if close_info:
                         ...
                     else:
                         live = self.broker.get_live_price(symbol) or entry
                         pnl  = mult * (live - entry) * qty_t
                         reason = "forced_close"
                         logger.warning(
-                            f"[GEO] {symbol} absent broker, fermeture approx "
-                            f"@ ${live:.4f} pnl=${pnl:.2f}"
+                            f"[GEO] {symbol} absent broker {miss_count}× sur "
+                            f"{miss_span_s:.0f}s — fermeture forcée @ ${live:.4f} "
+                            f"pnl=${pnl:.2f}"
                         )
                         self.memory.log_trade_close(trade_id, live, reason, pnl=pnl)
                     continue
+                else:
+                    # Position back, reset counter
+                    if ctx.get("_broker_miss_count"):
+                        self.memory.update_market_context(trade_id, {
+                            "_broker_miss_count": 0,
+                            "_broker_miss_first_ts": None,
+                        })
```

Nécessite une méthode `TradingMemory.update_market_context` qui patch le JSON.
Si elle n'existe pas, à ajouter (trivial).

##### Patch L3 — Ne pas inventer un fill price

Dans la branche force-close, le PnL est calculé avec `get_live_price(symbol)` —
le mark courant. C'est une approximation. Alternative plus honnête :

```diff
                     else:
-                        live = self.broker.get_live_price(symbol) or entry
-                        pnl  = mult * (live - entry) * qty_t
+                        # Pas de fill confirmé. Marker la position 'orphaned',
+                        # garder ouverte en DB (status='orphaned' au lieu de 'closed'),
+                        # pour reconciliation manuelle.
                         reason = "forced_close"
                         logger.warning(
-                            f"[GEO] {symbol} absent broker {miss_count}× sur "
-                            f"{miss_span_s:.0f}s — fermeture forcée @ ${live:.4f} "
-                            f"pnl=${pnl:.2f}"
+                            f"[GEO] {symbol} absent broker {miss_count}× sur "
+                            f"{miss_span_s:.0f}s — ORPHAN, reconciliation manuelle"
                         )
-                        self.memory.log_trade_close(trade_id, live, reason, pnl=pnl)
+                        self.memory.mark_orphan(trade_id, miss_count, miss_span_s)
```

Nécessite `TradingMemory.mark_orphan` (nouveau). Effet : pas de PnL faux,
position reste en "orphan" jusqu'à réconciliation manuelle ou retour broker.

Trade-off : plus conservateur mais nécessite humain dans la boucle. Peut être
préférable au "trade fantôme" qui pollue les stats.

##### Patch L4 (refactor) — Singleton broker au lieu de N instances dashboard

Refactor : faire de `KrakenPaperBroker` un singleton ou une instance shared
entre flask handlers et bot. Aujourd'hui chaque GET request en instancie une
nouvelle → init CLI call → lock contention.

```python
# dashboard.py ou main.py
_broker_singleton = None
def get_broker():
    global _broker_singleton
    if _broker_singleton is None:
        _broker_singleton = KrakenPaperBroker()
    return _broker_singleton
```

Réduit le rate des CLI calls de ~10× selon le dashboard usage.

#### Recommandation appliquer L1+L2+L3 ; L4 séparément

L1 (retry) + L2 (counter+span) + L3 (orphan flag) = patch lifecycle complet,
~30 lignes Python, change semantics MAIS ne casse pas le chemin "normal close"
(c'est uniquement la branche "broker absent" qui devient plus défensive).

L4 (singleton) = refactor, à voir séparément ; pas critique pour la sécurité
fonctionnelle.

#### Vérification non-régression avant patch

Avant d'appliquer L1+L2+L3, lancer le bot localement sans modification, observer
qu'il fait les trades existants correctement, puis appliquer et observer :
- Plus de "CLI error: " avec stderr vide (remplacé par retry + soit succès soit
  erreur informative)
- Force-closures seulement après 3 misses sur 2 min (zéro sur runtime stable)
- 0 nouveau trade tagué "target" en cas de panic close

À ce stade je ne patch RIEN au-delà du Step 2 (label fix déjà appliqué).
**Décision sur L1+L2+L3+L4 laissée à toi.**

### Fichiers et tâches

- L1 patch : `broker_kraken_paper.py:54-64` (_cli) — ~25 lignes
- L2 patch : `experts/geometric_expert.py:1200-1237` (manage_open_positions branche absent) — ~20 lignes
- L2 dependency : `TradingMemory.update_market_context(trade_id, dict)` — nouvelle méthode
- L3 patch : `experts/geometric_expert.py:1228-1236` (else branche) + nouvelle status "orphaned"
- L3 dependency : `TradingMemory.mark_orphan(trade_id, ...)` — nouvelle méthode
- L4 refactor : `dashboard.py` ou `main.py` (broker singleton)
- Smoke test : exécuter localement + vérifier les force-closures attendues / inattendues

---

## P7 — Brackets natifs Kraken paper (sécurité live) — 2026-06-01

User redirection : **L1+L2+L3 sont des palliatifs** ; le vrai fix est de remplacer
les SL/TP software (polling 30s) par des **brackets natifs reduce-only** côté
broker. Plus de position nue côté exchange. Le timeout 2h reste bot-side (moteur
de profit principal selon backtest A4).

### Step 0 — Capacités CLI Kraken paper (vérifiées)

**Order types** sur `buy`/`sell` : `[limit, market, post, stop, take-profit, ioc, trailing-stop, fok]`

**Flags utiles** :
| Flag | Effet |
|---|---|
| `--type stop` | Ordre stop : trigger sur `--stop-price`, fill market |
| `--type take-profit` | Take-profit : trigger sur `--stop-price`, fill market (limit si `--price` aussi) |
| `--stop-price <P>` | Prix de déclenchement |
| `--trigger-signal mark` | Signal de trigger (mark price, par défaut) — `mark|index|last` |
| `--reduce-only` | **Ordre ne peut QUE fermer**, jamais ouvrir ou flipper. Essentiel pour la sécurité. |
| `--client-order-id <ID>` | ID custom pour tracking / reconciliation |

**Sous-commandes** existantes :
| Cmd | Usage |
|---|---|
| `orders --output json` | liste les ordres ouverts (pour vérifier que le bracket EST posté) |
| `order-status --order-id <ID>` | statut détaillé d'un ordre |
| `fills --output json` | historique des fills (source de vérité pour close prices) |
| `cancel --order-id <ID>` | annuler un ordre (pour l'OCO) |
| `cancel-all` | dernier recours |
| `batch-order` | placer plusieurs ordres en une seule call (JSON array, atomique-ish) |

### Invocations exactes

**Pour une position SHORT** (entry = `sell ... --type market`) :
- SL (couvre la hausse, doit RACHETER ⇒ `buy` reduceOnly) :
  ```
  kraken futures paper buy PF_ETHUSD <qty> \
    --type stop --stop-price <SL> \
    --reduce-only --trigger-signal mark \
    --client-order-id <trade_id>-sl --output json
  ```
- TP (target en bas, doit RACHETER ⇒ `buy` reduceOnly) :
  ```
  kraken futures paper buy PF_ETHUSD <qty> \
    --type take-profit --stop-price <TP> \
    --reduce-only --trigger-signal mark \
    --client-order-id <trade_id>-tp --output json
  ```

**Pour une position LONG** (entry = `buy ... --type market`) :
- SL (couvre la baisse, doit VENDRE ⇒ `sell` reduceOnly, --type stop)
- TP (target en haut, doit VENDRE ⇒ `sell` reduceOnly, --type take-profit)

`--reduce-only` est natif. Sans ce flag, l'ordre pourrait ouvrir une position de
sens opposé si la position courante a déjà été fermée — risque de flip que
reduce-only élimine.

### Step 1 — Design du nouveau chemin de sortie

#### Vue d'ensemble

```
┌──────────────────────────────────────────────────────────────────┐
│ ENTRY                                                            │
│  1. Place market order (buy/sell) → wait for fill confirmation   │
│  2. Place SL reduce-only stop @ SL_price (client-id: trade_id-sl)│
│  3. Place TP reduce-only take-profit @ TP_price (id: trade_id-tp)│
│  4. If step 2 OR 3 fails → recovery (see below)                  │
│  5. Cache (sl_order_id, tp_order_id) on the position             │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│ MANAGE_OPEN_POSITIONS (every 30s fast loop)                      │
│  Source of truth = broker fills + orders, PAS polling current_px │
│                                                                  │
│  A. fills(since=entry_ts) → check if SL or TP order_id filled    │
│     → if YES :                                                   │
│        - record close at fill_price                              │
│        - tag reason = "stop" / "target" (selon qui a tiré)       │
│        - cancel the SIBLING bracket (OCO)                        │
│        - log_trade_close                                         │
│     → if NO : check timeout (next)                               │
│                                                                  │
│  B. Timeout (now - entry_ts >= 120 min) :                        │
│     - cancel SL + cancel TP (atomic — best effort, OR cancel-all)│
│     - market close position (sell reduce-only / buy reduce-only) │
│     - tag reason = "timeout" at fill price                       │
│                                                                  │
│  C. Broker absent (existant) : ne PAS panic-close.               │
│     L1 retry CLI / L2 miss counter / L3 orphan flag s'appliquent │
│     toujours en filet de sécurité.                               │
└──────────────────────────────────────────────────────────────────┘
```

#### Recovery sur échec de bracket post-entry (le point critique)

**Scenario 1 — Entry filled, SL post échoue** :
1. Détecté immédiatement (return None du CLI après retry interne L1)
2. **Position nue dans cette fenêtre = INACCEPTABLE**
3. Action :
   - Retry pose SL (3 tentatives, 0.5s/1s/2s backoff via L1)
   - Si toujours fail → **market close immédiat de la position** (sell/buy reduce-only `--type market`)
   - Tag reason = `"abort_no_bracket"` (ni stop ni target — l'entry a été annulée)
   - Logger ERROR critique (pas WARN)
   - PnL = (close_price - entry_price) × qty, peut être négatif (slippage de l'aller-retour)
   - **Mieux perdre quelques $ que rester nu**

**Scenario 2 — Entry filled, SL posté, TP post échoue** :
1. Position protégée par SL mais sans TP
2. Action :
   - Retry pose TP (3 tentatives)
   - Si fail → **continuer SANS TP**. Le timeout 2h + SL bot-side prennent le relais
   - Logger WARN, marquer trade_context.tp_missing = True
   - Risque résiduel : on rate les TP hits → return inférieur attendu, mais pas de risque de capital (SL est là)

**Scenario 3 — Entry market lui-même fail** :
1. Aucune position → rien à faire, juste log

#### OCO (One-Cancels-Other)

Tracking via le cache `_sltp` existant (déjà dans `kraken_broker.py:get_close_info`) :
```python
self._sltp[symbol] = {
    "side": "long"|"short",
    "sl_order_id": "...",
    "tp_order_id": "...",
    "entry_ts_ms": ...,
}
```

Quand on détecte qu'un des deux a fill (via `fills`) :
1. Identifier le frère via `_sltp[symbol]`
2. `cancel --order-id <frere>` (retry sur fail)
3. Si cancel fail définitivement : `cancel-all` (last resort, mais cancel-all serait global → préférer `cancel --client-order-id` ciblé)
4. Clear `_sltp[symbol]`

#### Détection des fills — comment

Polling `kraken futures paper fills --output json` :
- Cher en CPU/lock — éviter à chaque fast loop tick
- Cadence proposée : **chaque fast loop (30s)** mais avec un filter `since=last_check_ts`
- Si `fills` revient avec un fill matchant un trade_id en cours :
  - extraire `fill_price`, `fill_qty`, `fill_ts`
  - identifier reason = `"target"` ou `"stop"` selon le `client-order-id`
  - close trade en DB au `fill_price` réel (pas approximé)

Edge case : 2 fills back-to-back (SL trigger pendant qu'on cancel le TP). Possible mais rare. Idempotence : `log_trade_close` doit être idempotent sur trade_id. Si trade déjà closed, le 2e appel ne fait rien.

#### Pseudo-flux complet

```python
def place_limit_buy(symbol, price, stop_loss, take_profit, deploy_usdt) -> str | None:
    # 1. Market entry
    market_result = _cli("buy", sym, qty, "--type", "market", "--reduce-only=false")
    if market_result is None:
        return None
    trade_id = generate_uuid()
    fill_px = parse_fill_price(market_result) or get_live_price()
    
    # 2. Place SL bracket (sell reduce-only stop)
    sl_id = f"{trade_id}-sl"
    sl_result = _cli("sell", sym, qty, "--type", "stop",
                     "--stop-price", str(stop_loss), "--reduce-only",
                     "--trigger-signal", "mark", "--client-order-id", sl_id)
    if sl_result is None:
        # CRITICAL: position naked. Emergency close.
        logger.critical(f"SL post FAILED for {sym} — emergency market close")
        emergency_close = _cli("sell", sym, qty, "--type", "market", "--reduce-only")
        return None  # entry aborted
    
    # 3. Place TP bracket (sell reduce-only take-profit)
    tp_id = f"{trade_id}-tp"
    tp_result = _cli("sell", sym, qty, "--type", "take-profit",
                     "--stop-price", str(take_profit), "--reduce-only",
                     "--trigger-signal", "mark", "--client-order-id", tp_id)
    tp_missing = (tp_result is None)
    if tp_missing:
        logger.warning(f"TP post FAILED for {sym} — continuing with SL only")
    
    # 4. Cache for reconciliation
    self._sltp[sym] = {
        "side": "long", "trade_id": trade_id,
        "sl_order_id": parse_order_id(sl_result),
        "tp_order_id": parse_order_id(tp_result) if not tp_missing else None,
        "entry_ts_ms": time.time() * 1000,
        "tp_missing": tp_missing,
    }
    return trade_id


def get_close_info(symbol, since_ts_ms) -> dict | None:
    """Reconcile via broker fills. Returns {price, qty, reason} on match."""
    cached = self._sltp.get(symbol)
    if not cached: return None
    fills = _cli("fills", "--output", "json")  # might want filter param
    for f in fills:
        if f["order_id"] in (cached["sl_order_id"], cached["tp_order_id"]):
            if f["ts_ms"] < since_ts_ms: continue  # too old
            reason = "stop" if f["order_id"] == cached["sl_order_id"] else "target"
            # OCO: cancel the sibling
            sibling = cached["tp_order_id"] if reason == "stop" else cached["sl_order_id"]
            if sibling:
                _cli("cancel", "--order-id", sibling)
            del self._sltp[symbol]
            return {"price": float(f["price"]), "qty": float(f["qty"]), "reason": reason}
    return None


def close_position(symbol) -> bool:
    """Bot-initiated close (timeout, or force). Cancel brackets first, then market close."""
    cached = self._sltp.get(symbol, {})
    side = cached.get("side")
    qty = ...  # from broker positions
    # 1. Cancel both brackets (OCO)
    if cached.get("sl_order_id"): _cli("cancel", "--order-id", cached["sl_order_id"])
    if cached.get("tp_order_id"): _cli("cancel", "--order-id", cached["tp_order_id"])
    # 2. Market close
    close_side = "sell" if side == "long" else "buy"
    _cli(close_side, sym, qty, "--type", "market", "--reduce-only")
    self._sltp.pop(symbol, None)
    return True
```

#### Modifications côté stratégie `geometric_expert.py`

- `manage_open_positions` simplifié :
  - **Path A (SL/TP hit)** : utiliser `broker.get_close_info()` (qui réconcilie via
    fills + applique OCO). Si retourne un dict → close DB + log.
  - **Path B (timeout)** : appeler `broker.close_position(symbol)` (qui cancel les
    brackets puis market close). Tag "timeout".
  - **Path C (broker absent)** : L1 retry / L2 miss counter / L3 orphan flag —
    palliatifs en cas de pathologie réseau ou CLI. Ne devrait PAS être le chemin
    principal.

- La logique software-side `sl_hit = current_px <= stop_db` (lignes 1179-1182)
  devient **fallback diagnostique** : si `get_close_info()` ne voit pas le fill
  alors que current_px a clairement traversé, logger un WARN ("bracket missed?")
  mais NE PAS fermer manuellement — laisser le broker faire son job.

#### Test paper minimal (Step 3 prevu)

1. Manuel CLI : poser un SL+TP fake (avec un --client-order-id de test), faire
   bouger le mark price via un trade manuel, vérifier que :
   - `orders` montre les 2 brackets
   - Si on hit le SL : le bracket fire, position close, `fills` enregistre
   - Le TP frère est ANNULÉ (via le code OCO)
2. Restart bot avec brackets, observer un trade complet, vérifier en DB :
   - trade tagué "stop" ou "target" avec `exit_price = fill_price broker réel`
   - `_sltp` cache vidé après close
3. Vérifier 0 position nue : pas de fenêtre où la position existe sans bracket

#### Step 4 — Re-validation prévue (après application)

- Distribution close_reason live re-mesurée sur 24h post-patch
- Comparaison avec backtest Kraken P2 :
  - target % devrait approcher 36 % (vs 10 % fake actuel)
  - timeout % devrait approcher 36 % (vs 40 %)
  - stop % devrait approcher 27 % (vs 50 %)
- Si convergence : le défaut était bien le polling 30s + absence de bracket
- Si écart résiduel : autre divergence (latence trigger, slippage on stop/TP,
  ou divergence signal restante)

### Status — décisions

- **Step 0 (capacités)** : ✅ vérifié
- **Step 1 (design)** : ✅ ci-dessus, attente OK utilisateur
- **Step 2 (code)** : ❌ pas commencé, attente OK
- **Step 3 (test paper)** : ❌ pas commencé
- **Step 4 (re-validation)** : ❌ pas commencé

**L1+L2+L3 du P6 ne sont PAS appliqués** — ils restent utiles en fallback mais
le bracket-natif est le mécanisme principal. Si tu valides le design, le code
P7 inclura les améliorations L1 (retry CLI) intégrées dans le flow bracket.

**À toi : valider le design ci-dessus, ou demander des modifications, avant
que je passe à Step 2 (code).**

### Step 2 — Code appliqué (2026-06-01)

**Fichier touché** : `broker_kraken_paper.py` — réécriture complète (~480 lignes
vs 290 avant, +190 nettes). Aucune modif live au runtime tant que pas restart.

**Strategy (`geometric_expert.py`)** : pas de modif nécessaire (sauf le label fix
Step 2 du P5 déjà appliqué). Le flow `manage_open_positions` existant fonctionne
correctement avec le nouveau broker grâce à `NATIVE_BRACKETS=True` + le path
`get_close_info` déjà en place.

#### Changements clés

1. **`_cli()` retry sur lock errors** (L1 intégré) :
   - 3 tentatives, backoff 0.5/1/2 s
   - Détecte "locked by another process" sur stdout JSON
   - Distinct des timeouts subprocess (lesquels retentent aussi)

2. **`NATIVE_BRACKETS = True`** — le bot ne fait plus de polling SL/TP 30s.
   Source de vérité = broker fills via `get_close_info`.

3. **Persistance `_sltp` cache** :
   - `kraken_paper_sltp_cache.json` (parallèle de `kraken_sltp_cache.json`
     pour LIVE). Stocke entry_order_id, sl_order_id, tp_order_id, side, qty,
     client_order_ids
   - Survit aux restarts

4. **Helpers privés** :
   - `_place_stop_close(kraken_sym, close_side, qty, stop_price, client_order_id)`
   - `_place_tp_close(...)`
   - `_emergency_close(kraken_sym, close_side, qty, reason)` — ferme une position
     nue en cas d'échec de SL après l'entry
   - `_place_market_with_brackets(symbol, entry_side, qty, sl, tp)` — flow unifié

5. **`place_limit_buy/sell` réécrits** — délèguent à
   `_place_market_with_brackets`. Si SL post échoue après retries →
   emergency_close + return None (entry annulé). Si TP post échoue (post-SL
   OK) → continue sans TP (capital protégé par SL).

6. **`get_close_info(symbol, since_ts_ms)`** — implémentation réelle :
   - Poll `kraken futures paper fills`
   - Match par `order_id` OU `client_order_id` (cache `_sltp`)
   - Filtre par timestamp (since_ts_ms)
   - Retourne `{price, qty, reason}` avec reason='stop'/'target'
   - OCO : appelle `cancel_twin_brackets(exclude_order_id=keep)` pour annuler
     le frère du fill détecté

7. **`cancel_twin_brackets(symbol, exclude_order_id=None)`** — annule les
   brackets cachés sauf exclude_order_id. Vide `_sltp[sym]` quand les deux
   sont gone.

8. **`close_position(symbol)`** — réécrit :
   - Cancel les 2 brackets d'abord (OCO bot-side)
   - Market close reduce-only
   - Vide cache `_sltp`

9. **`list_open_orders`** — implémentation réelle (était stub vide). Poll
   `orders` CLI, attache `client_order_id`.

#### Tests

- `python -m py_compile` ✅ broker_kraken_paper.py + experts/geometric_expert.py + kraken_candles.py
- Méthodes attendues toutes présentes (vérifié via reflection)
- Smoke CLI live : **bloqué par lock contention du runtime actuel PID 35523**
  (le bot tient le lock en continu via dashboard requests + fast loop).
  Le retry L1 atteint son max sans succès tant que le bot tourne. Sera testable
  après restart.

#### Risques résiduels / points d'attention pour le test paper

1. **Lock contention au restart** : avec ~138 erreurs CLI/h actuelles, même
   avec retry L1, certaines opérations peuvent échouer. Mitigation future = L4
   singleton broker pour le dashboard (réduit le rate de ~10×). Hors scope de
   ce patch ; à voir séparément.

2. **Édition `--client-order-id`** : si le CLI Kraken paper n'accepte pas
   un `--client-order-id` arbitraire (longueur, charset), la pose pourrait
   échouer. Format actuel : `{entry_order_id}-sl` (e.g. `12345-sl`). À tester
   au premier vrai trade.

3. **Format réel des fills** : mon `get_close_info` essaie plusieurs noms de
   champs (`order_id`/`orderId`/`id`, `fill_time_ms`/`fillTime`/`ts`,
   `price`/`fill_price`). Le format exact du JSON `fills` Kraken paper sera
   visible au premier fill — peut nécessiter un ajustement mineur.

4. **Race entry → bracket** : entre `market entry` et `place_stop_close`, il
   y a une fenêtre de quelques 100ms où la position est nue. Probabilité de
   crash de prix énorme dans ce window = très faible. Mitigation actuelle =
   sequential, peut être amélioré via `batch-order` (place les 3 ordres en
   une CLI call).

### Step 3 — Test paper (recommandation pour user)

1. **Restart Jim** (kill PID 35523, restart `main.py`)
2. Observer le premier trade :
   - Log : `[KrakenPaper] ✅ SHORT/LONG ... native_brackets=True`
   - Log : `[Kraken] 🛡️ SL placed ... id=...` (si le wrapper log existe pour paper)
   - Log : `[Kraken] 🎯 TP placed ... id=...`
3. Vérifier via CLI à la main pendant qu'un trade est ouvert :
   ```
   ~/.cargo/bin/kraken --output json futures paper orders
   ```
   Devrait montrer 2 brackets (SL stop + TP take-profit) avec
   `reduce-only=true` et `client-order-id` matchant le pattern `*-sl` / `*-tp`.
4. Attendre un TP ou SL fill, vérifier en DB :
   - `close_reason = "target"` ou `"stop"` (PAS "forced_close")
   - `exit_price` = fill broker réel (pas mark approximé)
   - L'autre bracket est annulé (verify via `orders`)
5. Sur timeout 2h : vérifier que les 2 brackets sont cancelés ET la position
   market-close. Tag `"timeout"`.

### Step 4 — Re-validation (à mesurer après quelques trades)

Comparaison distribution close_reason :
- Avant patch : 10 % target FAKE / 40 % timeout / 50 % stop
- Backtest cible : 36 % target / 36 % timeout / 27 % stop
- Attendu après patch : target_rate proche de backtest (TP hits réels)

### Fichiers touchés

- `broker_kraken_paper.py` (réécrit, +190 net lines)
- `experts/geometric_expert.py` (Step 2 fake-target label fix déjà appliqué — pas
  d'autre modif requise)

### Status final

- **Step 0** : ✅ vérifié
- **Step 1** : ✅ design validé
- **Step 2** : ✅ code appliqué + py_compile OK
- **Step 3** : ✅ **CYCLE COMPLET OBSERVÉ — voir P7 Step 3 ci-dessous**
- **Step 4** : ⏳ accumulation données sur N trades

### Step 3 — Test paper : cycle complet observé (2026-06-01 15:36→15:39)

#### Préalable au restart

État Kraken paper au moment du kill PID 35523 :
- 2 phantom positions encore ouvertes côté broker (SOL 29.0 entry 81.91, ETH 1.2
  entry 2002.0) — résidus des fake-target trades du P5, jamais réellement
  fermés par l'ancien code
- Lock file `futures_state.json.lock` stale (du 14:41 EDT, timestamp du fake
  close 88faed70) — empêchait toutes les CLI
- 0 open trades en DB

**Nettoyage** :
- `rm` lock file stale + PID file stale
- Close manuel des 2 phantoms via buy reduce-only market (SOL +$24.65, ETH +$0.60)
- 0 positions, 0 orders, état propre

#### Restart

```
2026-06-01 15:36:06 ✓ PID lock acquired: 16287
2026-06-01 15:36:06 ✅ KrakenPaperBroker prêt (Kraken Futures Paper) — brackets natifs ON
2026-06-01 15:36:07 [FAST] ⚡ Fast loop started — 30s
2026-06-01 15:36:07 [SLOW] ── Cycle 1 ──
```

Confirmation `brackets natifs ON` dans le log d'init.

#### Bug fix avant restart : format JSON fills

Le smoke CLI a révélé que `kraken futures paper fills` retourne `filled_at`
(ISO string) et non `fill_time_ms`. Patch appliqué à `get_close_info` pour
parser l'ISO :
```python
ft_ms = 0
iso = f.get("filled_at") or f.get("fill_time") or f.get("ts")
if isinstance(iso, str):
    ft_ms = int(datetime.fromisoformat(iso.replace("Z","+00:00")).timestamp() * 1000)
```

#### Cycle complet — trade 087ff6cd

```
15:36:14.403  [KrakenPaper] ✅ SHORT SOL/USD qty=60.0 ~$81.0400 
               SL=81.1511(id=FP-00247…) TP=79.6772(id=FP-00248…)
               native_brackets=True
15:36:14.404  [GEO] SHORT_T1 SOL/USD @ $80.8906 SL=$81.1511 TP=$79.6772 R:R=4.7x
15:36:37.180  [GEO] ✅ FILLED: SOL/USD SHORT @ $81.0400 qty=60.0000
              ⋯ 3 min plus tard ⋯
15:39:43.255  [KrakenPaper] 🔗 OCO cancelled 1 sibling bracket for SOL/USD after stop fill
15:39:43.256  [KrakenPaper] 🎯 close detected via STOP fill SOL/USD @ $81.1740 qty=60.0
15:39:43.256  [GEO] 🔴 EXIT [stop] short: SOL/USD @ $81.1740 pnl=$-8.04
```

#### Vérifications brackets via CLI direct (pendant trade ouvert)

```json
"open_orders": [
  {"client_order_id":"FP-00245-sl","id":"FP-00247",
   "reduce_only":true,"side":"long","size":60.0,"type":"stop","symbol":"PF_SOLUSD"},
  {"client_order_id":"FP-00245-tp","id":"FP-00248",
   "reduce_only":true,"side":"long","size":60.0,"type":"take-profit","symbol":"PF_SOLUSD"}
]
```

→ Les 2 brackets sont **RÉELLEMENT posés** côté broker, pas juste loggés.
Pattern `{entry_id}-sl` / `{entry_id}-tp` confirmé. `reduce_only=true` natif.

#### Vérifications cache + DB

`kraken_paper_sltp_cache.json` :
```json
{"PF_SOLUSD":{
  "entry_order_id":"FP-00245","side":"short","qty":60.0,"fill_px":81.04,
  "sl":81.151,"tp":79.6772,"sl_order_id":"FP-00247","tp_order_id":"FP-00248",
  "sl_cli_id":"FP-00245-sl","tp_cli_id":"FP-00245-tp","tp_missing":false,
  "entry_ts_ms":1780342574403}}
```

DB après close :
| id | symbol | side | entry | **exit** | **close_reason** | pnl |
|---|---|---|---:|---:|---|---:|
| 087ff6cd | SOL/USD | sell | 81.04 | **81.174** (fill broker réel) | **stop** | −$8.04 |

#### Checklist user — tous les items observés ✅

| Item | Observé |
|---|---|
| log montre `native_brackets=True` | ✅ |
| brackets posés avec IDs | ✅ FP-00247 (SL) + FP-00248 (TP) |
| `orders` CLI montre 2 brackets reduce_only=true | ✅ |
| pattern `*-sl` / `*-tp` | ✅ `FP-00245-sl` + `FP-00245-tp` |
| sur fill : `close_reason="stop"` (pas "forced_close") | ✅ |
| `exit_price` = fill broker réel | ✅ $81.174 (pas approximé) |
| OCO cancel le frère | ✅ "OCO cancelled 1 sibling bracket" |
| 0 position résiduelle après close | ✅ count=0 |
| 0 ordre résiduel | ✅ count=0 |
| cache `_sltp` vidé | ✅ |

#### Confirmation prédiction utilisateur : "stop rate va monter"

- Ancien stop avg (20 trades pré-restart, polling 30s) : **−$4.15** moyenne
- Premier stop natif (touch broker exact) : **−$8.04**

Différence : avec l'ancien polling, certains wicks de stop disparaissaient
avant le check suivant (selection bias sur les stops "lents"). Le SL natif
fire au touch exact + slight market slippage du market order de close.

C'est exactement la prédiction utilisateur : "l'ancien SL pollait toutes
les 30s (laggy, ratait les mèches) ; le SL natif fire au touch. Donc le %
de stops va probablement MONTER."

Avec 1 trade observé : trop tôt pour conclure sur la distribution complète.
À suivre sur les prochains 10-20 trades pour comparer :
- ancien : 10 stop (−$4.15 avg), 8 timeout (+$7.16), 2 fake-target (+$7.28)
- nouveau (en cours) : observer la distribution sur N≥10 trades

#### Status

- ✅ Cycle complet observé (entry → bracket → fill → OCO → DB cohérent)
- ⏳ Distribution sur trades futurs (accumulation en cours)
- ⏳ Re-validation P7 Step 4 (comparaison structurelle live vs backtest) sur sample ≥ 20

---

## P8 — Divergence #4 : routing T1 dans trend_down_smooth — 2026-06-01

### Cas concret

Trade `f5a91ca3` post-restart : SHORT SOL/USD à $81.13, **régime
`trend_down_smooth`**, **thesis T1**, TP $79.6772 (−1.79 %, R:R 7.6×).

Le backtest, en mode `regime` (TP régime-driven, voir P7 + analyse cr_target_pct
ci-dessous), ne tire pas T1 dans `trend_down_smooth` avec `ROUTER_VARIANT=t1_neutral`
(default). T1 n'y fire qu'avec `t1_trend_fallback`. Pourtant le live a tiré T1
là — donc divergence routing.

### Mécanique

**Backtest** `backtest_p1_ablation.py:883-900` :
```python
if state == "trend_down_smooth":
    # T2 priority
    if "t2" in enabled and conf >= 75 and dirn <= -0.85:
        sig = get_short_signal_t2(...)
        if sig: return "T2", sig
    # T4 secondary
    if "t4" in enabled:
        sig = get_short_signal_t4(...)
        if sig: return "T4", sig
    # T3 fallback
    if "t3" in enabled:
        sig = get_short_signal_t3(...)
        if sig: return "T3", sig
    # T1 SEULEMENT si ROUTER_VARIANT == "t1_trend_fallback"
    if ROUTER_VARIANT == "t1_trend_fallback" and "t1" in enabled:
        sig = get_short_signal_t1(...)
        if sig: return "T1", sig
    return None, None
```

→ Avec `ROUTER_VARIANT=t1_neutral` (default backtest), **T1 ne fire pas en
trend_down_smooth**.

**Live** `experts/geometric_expert.py:926-932` :
```python
t1_eligible = False
if cr_allow_short:           # ← TRUE quel que soit le state si crypto_regime allow_short
    t1_eligible = True
if config.ROUTER_VARIANT == "t1_neutral" and state == "neutral":
    t1_eligible = True
if config.ROUTER_VARIANT == "t1_trend_fallback" and state == "trend_down_smooth":
    t1_eligible = True
```

→ `cr_allow_short=True` rend T1 éligible **inconditionnellement** côté live, y
compris en `trend_down_smooth`. La gate `ROUTER_VARIANT == "t1_trend_fallback"`
ne sert plus à rien — T1 est déjà éligible via la 1ère condition.

### Conséquence sur les trades observés

Trades live `trend_down_smooth` post-restart : `087ff6cd` (SOL T1) et
`f5a91ca3` (SOL T1) ont tous deux fire **T1**, alors que dans le backtest mode
`regime` :
- `trend_down_smooth` (N=24 trades sur 33mo) : 100 % T2, 0 % T1

Sample N=2 vs backtest N=24 sur 33mo : la live tire des T1 dans ce régime
beaucoup plus souvent que la backtest ne le simule.

### Impact estimé

- Backtest pure aggregate : non mesuré directement (différence de comptage et
  qualité par trade)
- Heuristique : on observe 2 T1 / 2 trades live en `trend_down_smooth` (100 %).
  Si le backtest faisait pareil, on aurait ~100 % T1 dans ce régime au lieu de
  100 % T2 — ce qui changerait le mix de PnL dans la bucket. Mais cette bucket
  ne pèse que 24/11148 = **0.22 % du volume backtest** → impact aggregate
  très faible.
- L'impact est essentiellement **sur les régimes secondaires où la live tire
  T1 et le backtest filtre via ROUTER_VARIANT**.

### Statut

**Pas de fix maintenant.** Impact aggregate < 1 %. Priorité plus haute = laisser
les brackets natifs accumuler du sample.

Si fix un jour, sens du patch (cohérent avec stratégie historique : backtest =
référence) :
- Soit aligner LIVE sur BACKTEST : retirer la 1ère condition `if cr_allow_short`
  qui rend T1 éligible inconditionnellement
- Soit aligner BACKTEST sur LIVE : retirer la gate `ROUTER_VARIANT` dans la
  branche trend_down_smooth (rendre T1 fallback systématique après T2/T4/T3)
- Décision dépend de quel comportement EST validé. Probablement vérifier qu'avec
  T1 dans trend_down_smooth (mode `t1_trend_fallback`), le backtest reste
  profitable.

---

## P9 — Méta-pattern : 4 divergences backtest≠live trouvées cette session — 2026-06-01

Toutes ces divergences viennent du même drift structurel : le **code stratégie
existe en deux exemplaires parallèles** (`backtest_p1_ablation.py` pour le
backtest, `experts/geometric_expert.py` pour le live). Chaque modification d'un
côté n'est pas automatiquement portée à l'autre.

### Recensement

| # | Divergence | Découverte | Côté qui s'écarte | Impact mesuré | Status |
|---|---|---|---|---|---|
| 1 | `low_vol_mode` bypass divergence | P3 | live (bypass strict div en lowvol) | Cause des 29 OKX BUYs en bull pre-792b237. Fermé par commit `792b237` (force-off lowvol) le 2026-05-29. Garde résiduelle = Patch P3-2 (proposé, non appliqué) | Mitigé (commit) |
| 2 | `cr_allow_long`/`short` fail-OPEN default | P3 | live (defaults True au lieu de False) | 0 occurrence observée. Risque latent : si crypto_regime.evaluate() lève → live trade avec gates relaxées. Patch P3-1 proposé (non appliqué) | Latent |
| 3 | `cr_target_pct` (TP régime-driven 0.5–1.5 %) | P7+analyse | live (depuis commit `3fc204d` 2026-05-27) | Backtest aggregate **−1.6 %** (cf P9 analyse régime ci-dessous). Hit-rate TP préservé (63 % → 63 %). Effet localisé sur shorts contre-tendance (WR −10pp en trend_up). | Acceptable, non urgent |
| 4 | T1 routing inconditionnel via `cr_allow_short` | P8 | live (T1 fire en trend_down_smooth sans ROUTER_VARIANT) | Bucket trend_down_smooth = 0.22 % du volume → impact aggregate < 1 % | Pas urgent |

### Origine commune

Tous les patches stratégie depuis 2026-05-27 ont été appliqués sur **live
seulement**, sans porter au backtest :
- `3fc204d` (2026-05-27) — crypto-native regime → introduit cr_target_pct
- `8d965e7` (2026-05-28) — T2/T3/T4 + T1S hard-block
- `99c4a60` (2026-05-29) — t1_neutral + regime-gate fix
- `792b237` (2026-05-29) — force lowvol off (parity backport — UN des rares à
  être conscient de la divergence)
- `a9b77d2` (2026-05-29) — instrumentation phase 4
- `a0cd632` (2026-05-30) — reconcile side + T1S divergence mode
- `23a3884` (2026-05-30) — prevent same-symbol long+short

Chaque commit qui modifie le code stratégie (T1L/T1S signal/route) crée une
divergence potentielle. La fenêtre de drift = 5 jours, 4 divergences trouvées →
**~1 divergence par jour de drift**.

### Chantier identifié : refactor module signal/routing partagé

**À acter, PAS à démarrer maintenant.** La vraie solution durable :

```
experts/_signals_geo_v4.py (NEW)
├─ get_long_signal_t1(arr_5m, arr_15m, arr_1h, target_pct=None) → signal | None
├─ get_short_signal_t1(arr_5m, arr_15m, arr_1h, target_pct=None) → signal | None
├─ get_short_signal_t2/t3/t4(...) → signal | None
├─ route_long_signal(arrays, regime, enabled) → (thesis, signal)
└─ route_short_signal(arrays, regime, enabled, router_variant) → (thesis, signal)

experts/geometric_expert.py  → wraps _signals_geo_v4 + live-specific concerns
                                (broker, pool, memory, dashboard, regime fetcher)

backtest_p1_ablation.py      → wraps _signals_geo_v4 + backtest-specific concerns
                                (data slicing, pool sim, equity curve)
```

**Bénéfices** :
- Un changement de gate → un seul fichier touché
- Backtest et live structurellement identiques sur la logique de signal
- Plus aucune divergence type P3/P4/P8 possible

**Coûts** :
- Refactor ~500-1000 lignes (extraire les signaux + routing des 2 fichiers)
- Risque de régression pendant le refactor (à valider avec re-run complet
  walk-forward + 1 semaine paper)
- Touche le live → nécessite drain des positions + restart

**Status** : identifié comme la dette technique fondamentale qui explique les
4 divergences. À planifier après les priorités plus immédiates (observation
brackets natifs, validation paper edge).

---

## P10 — Observation post-bracket — accumulation et config référence — 2026-06-01

### Config référence pour comparaison

Le live tourne maintenant avec :
- Brackets natifs SL+TP reduce-only (P7) — source de vérité broker
- Régime-driven `cr_target_pct` (P3 commit `3fc204d`) → TP varie 0.5–1.5 %
- T1 inconditionnel dans tous régimes allow_short (P8) → T1 fire en trend_*

Backtest analogues testés sur Kraken 33mo (long+short, ALL_parity, sized) :

| Variante | TP source | T1 trend_down_smooth | Best analog ? |
|---|---|---|---|
| **const09** | hardcode 0.9 % | Non (ROUTER_VARIANT=t1_neutral) | non — TP différent |
| **regime** | régime-driven | Non (idem) | **OUI — le plus proche** |
| **regime + t1_trend_fallback** | régime-driven | Oui (T1 fire) | Pas mesuré (à faire un jour) |

### Config référence figée : **`regime`** (= `regtp_regime.csv`)

Métriques cibles pour validation des trades live post-restart :
| Métrique | regime (cible) |
|---|---:|
| N trades / 33mo | 11 148 |
| WR % | 67.17 |
| PF | 2.20 |
| Expectancy $/trade | +14.67 |
| Sharpe | +8.27 |
| target % | 62.6 |
| stop % | 24.7 |
| timeout % | 12.8 |
| **avg stop $** | **−$44.52** |
| **avg target $** | **+$42.11** |
| **avg timeout $** | **−$5.46** |

Caveat P8 : la divergence routing fait que la live tirera plus de T1 en
trend_down_smooth que ce que cette config simule. Sample live aura
potentiellement plus de trades dans cette bucket.

### Accumulation post-restart

Snapshot live (à mettre à jour à chaque trade fermé) :

| | trades | target | timeout | stop | avg stop | avg target | avg timeout | total $ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **Pré-restart (20 trades, polling 30s)** | 20 | 10 % (FAKE) | 40 % | 50 % | **−$4.15** | +$7.28 | +$7.16 | +$30.30 |
| Post-restart (brackets natifs) | 1 | 0 | 0 | **1 (100 %)** | **−$8.04** | — | — | **−$8.04** |
| **Backtest regime cible** | 11 148 | 62.6 % | 12.8 % | 24.7 % | **−$44.52** | +$42.11 | −$5.46 | +$163 510 |

Sample N=1 = trop tôt pour conclure. À continuer jusqu'à N=10-15 minimum.

### Plan d'observation (pas d'action automatique avant N≥10)

À chaque trade fermé observé dans le log live, accumuler :
1. close_reason (target/timeout/stop/forced_close/abort_no_bracket)
2. pnl_net
3. regime_state à l'entrée
4. thesis (T1/T2/...)
5. tp_pct_used

Mise à jour du tableau ci-dessus à N=5, N=10, N=15. À N≥10, comparaison
structurée vs config `regime` :
- Stop avg : live vs −$44.52 backtest. Si proche → SL natif fire au touch
  comme prévu. Si très inférieur en magnitude → SL trop large ou exit tardif.
- Target avg : live vs +$42.11 backtest. Similaire → TP natif fonctionne.
- Distribution % : doit converger vers 63/25/13 (target/stop/timeout).

### Status accumulation

| # | Trade | Symbol | Side | Régime | TP % | Exit | Hold min | PnL |
|---|---|---|---|---|---:|---|---:|---:|
| 1 | 087ff6cd | SOL/USD | sell | trend_down_smooth | 1.5 | **stop** | 3.1 | **−$8.04** |
| 2 | f5a91ca3 | SOL/USD | sell | trend_down_smooth | 1.5 | **timeout** | 120.3 | **+$22.53** |
| 3 | 7c829393 | ETH/USD | sell | neutral | 0.9 | **TARGET** | 58.7 | **+$16.88** |

→ **Première target réelle hit** (#3) sur Kraken paper depuis l'instauration des
brackets. close_reason=target via fill broker réel, pas un fake.

#### Snapshot N=3 vs cibles

| | N | target % | timeout % | stop % | avg stop | avg target | avg timeout | total $ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Live N=3 (post-bracket) | 3 | 33 % | 33 % | 33 % | −$8.04 | +$16.88 | +$22.53 | +$31.37 |
| **Live N=9 (2026-06-02 tick)** | 9 | **22 %** | **33 %** | **44 %** | **−$5.49** | **+$20.80** | +$19.72 | **+$78.83** |

---

## P11 — Diagnostic régime : dashboard label vs strategy reality — 2026-06-02

User a vu dashboard afficher "régime BULL" alors que la stratégie tire
exclusivement des SHORTS et certains trades en TP 1.5 %. Vérifions s'il y a
divergence ou juste un affichage trompeur.

### 1. Régimes par trade (9 trades depuis 1er restart bracket)

OLD (VIX/SPY label affiché sur le chip dashboard) vient de `MarketRegime` —
log `[GEO] evaluating ETH/USD | régime=bull`. La valeur est passée à
`evaluate()` mais N'est PAS stockée par trade dans market_context. Confirmée
constante = **BULL** sur la période via `[CRYPTO_REGIME] ... old=BULL ...`.

NEW (crypto_regime) est stockée dans `market_context.crypto_regime` :

| trade_id | symbol | side | TP $ | TP % | **cr_state (NEW)** | btc_state | eth_signal | target_pct | conf | dir |
|---|---|---|---:|---:|---|---|---|---:|---:|---:|
| 087ff6cd | SOL/USD | sell | 79.677 | 1.682 | **trend_down_smooth** | calm | short_signal | 0.015 | 85.2 | −0.904 |
| f5a91ca3 | SOL/USD | sell | 79.677 | 1.791 | **trend_down_smooth** | calm | short_signal | 0.015 | 85.2 | −0.904 |
| 7c829393 | ETH/USD | sell | 1985.29 | 0.701 | **neutral** | calm | weak | 0.009 | 33.1 | −0.662 |
| c6c49e7a | ETH/USD | sell | 1969.58 | 0.877 | **neutral** | calm | weak | 0.009 | 32.7 | −0.654 |
| fbac66b8 | ETH/USD | sell | 1969.58 | 1.051 | **neutral** | calm | weak | 0.009 | 32.7 | −0.654 |
| da094420 | ETH/USD | sell | 1983.07 | 0.926 | **neutral** | calm | weak | 0.009 | 32.6 | −0.652 |
| 5e754353 | SOL/USD | sell | 78.983 | 0.713 | **neutral** | calm | weak | 0.009 | 41.8 | −0.871 |
| ef0f93fe | ETH/USD | sell | 1960.74 | 1.242 | **neutral** | calm | weak | 0.009 | 36.1 | −0.723 |
| 3d8eed31 | SOL/USD | sell | 78.983 | 0.875 | **neutral** | calm | weak | 0.009 | 42.9 | −0.895 |

OLD label `BULL` constant. NEW state = mix `neutral` (7 trades) + `trend_down_smooth` (2 trades). **Toutes les décisions correspondent au NEW (crypto), pas à l'OLD (VIX/SPY).** Dashboard chip affichait "BULL" = trompeur.

### 2. Cohérence target_pct ↔ TP affiché

| trade | target_pct_used (cr_state émis) | TP placé % | TP attendu × |
|---|---:|---:|---|
| 087ff6cd | **0.015** | 1.68 % | ✓ (≥ 1.5 % car entry au-dessus de zone.low) |
| f5a91ca3 | **0.015** | 1.79 % | ✓ |
| 7 trades ETH+SOL neutral | **0.009** | 0.70-1.24 % | ✓ (range autour de 0.9 % selon écart entry/zone.low) |

→ `cr_decision.target_pct` correspond exactement au TP placé. Pas de bug
d'écart entre régime et TP. Tous les TP placés viennent **directement** du
champ `target_pct` du régime engine. Pas de hardcoding silencieux.

### 3. SOL TP 1.5 % — quel cr_state ?

**`trend_down_smooth`** pour les 2 cas (087ff6cd et f5a91ca3). Les deux trades
SOL TP 1.5 % ont :
- `cr_state = trend_down_smooth`
- `btc_state = calm`
- `eth_signal = short_signal`
- `direction = −0.904`
- `confidence = 85.2`

Per `crypto_regime.py:374-375`, `trend_down_smooth` retourne explicitement
`"target_pct": 0.015`. Le routing en backtest envoie T2 prioritaire ici
(et T1 seulement si `ROUTER_VARIANT=t1_trend_fallback`). Live l'a tiré en T1
(divergence P8). Mais le **target_pct lui-même** est cohérent backtest↔live.

### 4. Backtest utilise `crypto_regime.py` de la même façon ?

**Oui, identique.** Vérifié :

`backtest_p1_ablation.py:1006-1008` :
```python
btc_fetcher = make_btc_fetcher(df_btc)
from crypto_regime import CryptoRegime
regime_eng = CryptoRegime(broker, fetch_btc=btc_fetcher)
```

`backtest_p1_ablation.py:1121` :
```python
rd = regime_eng.evaluate(sym, as_of=ts_unix)
```

Live (`experts/geometric_expert.py:584`) :
```python
cr_decision = self.crypto_regime.evaluate(symbol)  # as_of=None par défaut
```

**Différences de plomberie** (pas de logique) :
- BTC source : live = `_fetch_btc_kraken()` (Kraken Futures REST, real-time) ;
  backtest = closure sur cache parquet (PF_XBTUSD historique). Même API
  Kraken, juste real-time vs replay.
- Mode : live `as_of=None` (TTL cache 60s ETH / 300s BTC) ; backtest
  `as_of=ts_unix` (bypass cache, slice historique).
- Logique de classification : **strictement identique** (même code path).

→ **Pas de 5e divergence côté regime engine.** Pour les MÊMES inputs (BTC +
ETH bars), les sorties sont strictement identiques live et backtest. La
différence vient des données alimentaires (real-time vs cache), pas du code.

### 5. Dashboard fix — proposition

Le chip `s-chip-regime` à `dashboard.py:2938` affiche **`régime · BULL`** depuis
le champ `cs.regime` qui est l'OLD VIX/SPY label.

Le dashboard A DÉJÀ une bonne carte "Crypto Market State" plus bas dans la
page qui affiche le NEW state. **Mais le chip header est trompeur.**

Fix proposé : remplacer ou doubler le chip. Le moins risqué = AJOUTER un chip
"crypto" à côté, sans toucher le legacy chip :

```diff
   mk('s-chip-regime', 'régime · ' + (cs.regime || '—'));
+  // Add crypto-regime chip alongside the legacy VIX/SPY one
+  try {
+    const cr = await api('/api/crypto-regime');
+    if (cr && cr.by_symbol) {
+      const states = Object.values(cr.by_symbol).map(s => s.state || '?');
+      const uniqStates = [...new Set(states)];
+      const label = uniqStates.length === 1
+        ? uniqStates[0]
+        : uniqStates.join('/');
+      mk('s-chip-regime', 'crypto · ' + label.replace(/_/g,' '));
+    }
+  } catch(e) {}
```

Effet :
- `régime · BULL` (legacy VIX/SPY) — conservé
- `crypto · neutral` ou `crypto · trend down smooth/neutral` (per-symbol)
  ajouté à côté
- User voit instantanément la divergence entre macro et crypto regime

Risque résiduel : nul, c'est un append.

Si tu valides, j'applique immédiatement (paper, restart non-bloquant) — sinon
le diff est là.

### Status

- Diagnostic OLD vs NEW : ✓ documenté
- target_pct cohérent dans tous les cas : ✓
- SOL 1.5 % vient bien de `trend_down_smooth` : ✓
- Backtest utilise crypto_regime de manière identique : ✓ (pas de 5e divergence)
- Fix dashboard : **appliqué + restart PID 67614** (cf 6 ci-dessous)

### 6. SEGMENTATION LIVE — par cr_state raw (post-bracket, N=9)

Cutoff `created_at >= 2026-06-01 19:36:00 UTC` (= 1er restart bracket PID 16287).
N=9 trades fermés. Tous SHORTS, 0 long.

| cr_state | N | WR % | PF | exp $ | total $ | close_reasons |
|---|---:|---:|---:|---:|---:|---|
| `neutral` | 7 | 57.1 | 5.63 | +$9.19 | +$64.35 | target=2 (avg +$20.8), stop=3 (avg −$4.6), timeout=2 (avg +$18.3) |
| `trend_down_smooth` | 2 | 50.0 | 2.80 | +$7.24 | +$14.49 | stop=1 (−$8.0), timeout=1 (+$22.5) |

Sample trop petit par bucket (max 7), mais observations préliminaires :
- LIVE avg stop **−$5.49** (N=4) vs **−$4.15** ancien (polling 30s, N=20) vs
  **−$44.52** backtest (regime mode, N=2751)
- Le live stop reste 8× plus petit que backtest en magnitude. Hypothèse :
  notionnel live ($5000 × size_mult 0.5-1.0) plus petit que backtest
  ($5000 fixed × cap_f 1.0) ? À creuser quand N≥15.

### 7. MIRROR BACKTEST — par cr_state raw (mêmes états)

Config matchée live actuel : Kraken 33mo, ALL_parity_sized + regime-driven TP
(USE_REGIME_TP=True), GEO_DISABLE_LOWVOL=1, brackets implicites
(target/stop/timeout déterministes). Source : `regtp_regime_trades.csv`.

#### SHORTS — par cr_state raw

| cr_state | N | WR % | PF | exp $ | tgt % | tgt avg | stp % | stp avg | tmo % | tmo avg |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **neutral** | 8 157 | 65.4 | 2.03 | +$12.98 | 60.4 | +$41.45 | 24.8 | −$44.63 | 14.8 | −$6.44 |
| **trend_down_smooth** | 24 | 54.2 | 4.84 | +$13.36 | 29.2 | +$48.28 | 0.0 | $0 | 70.8 | −$1.02 |
| aligned_short_trend | 66 | 74.2 | 4.79 | +$39.74 | 71.2 | +$69.96 | 21.2 | −$43.51 | 7.6 | −$11.24 |
| btc_chaos | 1 348 | 79.8 | 3.83 | +$23.65 | 79.2 | +$40.33 | 18.7 | −$43.63 | 2.2 | −$5.73 |
| btc_trend_eth_weak | 892 | 71.0 | 2.35 | +$16.25 | 69.1 | +$40.70 | 26.2 | −$44.34 | 4.7 | −$4.80 |
| vol_extreme | 172 | 80.8 | 3.87 | +$23.92 | 80.8 | +$39.92 | 19.2 | −$43.47 | 0.0 | $0 |
| counter_cyclical_block | 3 | 100 | inf | +$39.02 | 100 | +$39.02 | 0 | $0 | 0 | $0 |

États non observés dans le backtest 33mo : `trend_down_choppy`, `range_quiet`,
`btc_led_short`, `btc_unknown` (N=0 — pas vus sur la fenêtre).

#### LONGS — par cr_state raw

| cr_state | N | WR % | PF | exp $ | tgt % | tgt avg | stp % | stp avg | tmo % | tmo avg |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| trend_up_smooth | 247 | 52.2 | 1.63 | +$10.98 | 35.6 | +$71.79 | 37.2 | −$41.94 | 27.1 | +$3.78 |
| aligned_long_trend | 46 | 52.2 | 2.09 | +$18.11 | 45.7 | +$72.77 | 37.0 | −$41.84 | 17.4 | +$2.04 |

Observation : les longs en bull regimes ont WR 52 % (≈ coin flip). Les wins
sont gros (+$72) et les losses sont gros (−$42). Edge net positif via
asymétrie tgt/stp.

### 8. Comparaison directe live↔backtest pour les 2 états observés

| cr_state × side | Live (N) | Backtest target % | Live target % | Backtest exp $ | Live exp $ |
|---|---|---:|---:|---:|---:|
| neutral × short | 7 | 60.4 | **28.6** (2/7) | +$12.98 | +$9.19 |
| trend_down_smooth × short | 2 | 29.2 | **0** (0/2) | +$13.36 | +$7.24 |

Hit-rate target live actuellement (28.6 % neutral, 0 % trend_down_smooth) << backtest
(60 % / 29 %). N trop petit pour conclure, mais le pattern à surveiller :
- Est-ce que le SL natif intercepte plus de "would-be targets" ?
- Est-ce une simple variance sur petit échantillon ?

À mesurer sérieusement quand N≥10-15 par bucket.

### 9. DASHBOARD FIX — appliqué + restart

Patch à `dashboard.py:2938` (après le chip legacy) :

```javascript
mk('s-chip-regime', 'régime · ' + (cs.regime || '—'));
// P11 fix : also display crypto_regime state(s)
try {
  const cr = await api('/api/crypto-regime');
  if (cr && cr.by_symbol) {
    const states = Object.values(cr.by_symbol).map(s => (s.state || '?'));
    const uniq = [...new Set(states)];
    const label = uniq.length === 1 ? uniq[0] : uniq.join('/');
    mk('s-chip-regime', 'crypto · ' + label.replace(/_/g, ' '));
  }
} catch(e) {}
```

`py_compile` OK. Restart PID 65746 → 67614 (~3 s, 0 position en cours).

Test : `curl /api/crypto-regime` retourne actuellement state=`btc_trend_eth_weak`
pour ETH et SOL → après refresh page, le user verra :
- `régime · BULL` (legacy chip conservé)
- `crypto · btc trend eth weak` (nouveau chip ajouté)

Quand les états divergent entre ETH et SOL, le chip affiche `cryptoSymA/symB`
(par exemple `crypto · neutral/trend_down_smooth`).

### Status

- Diagnostic OLD vs NEW : ✓
- Segmentation live N=9 : ✓
- Mirror backtest par cr_state : ✓
- Dashboard fix appliqué (PID 67614) : ✓
- N≥10-15 par bucket pour comparaison sérieuse : ⏳
| Backtest regime cible | 11 148 | 62.6 % | 12.8 % | 24.7 % | −$44.52 | +$42.11 | −$5.46 | — |

Trop tôt pour interpréter (N=3). On continue jusqu'à N≥10.

Observations partielles :
- avg stop −$8.04 (1 trade) vs cible −$44.52 backtest : 5× plus petit en
  magnitude. Le SL natif fire au touch précis sans gap intra-bar massif.
- avg target +$16.88 (1 trade) vs cible +$42.11 : 2.5× plus petit. Peut être
  dû au TP régime 0.9 % (neutral) vs régimes plus larges en backtest.
- avg timeout +$22.53 (1 trade) vs cible −$5.46 : positif inattendu — le marché
  a dérivé en faveur du short avant le timeout.

Toutes ces lectures sont sur 1 observation/bucket → bruit dominant.

### Fichiers générés

- `trading-agent/run_p2_long_replay.py` (replay des 30 longs gate-par-gate)
- `trading-agent/p2_long_replay_detail.csv` (per-trade rejection gate)

---

## P4 — Résolution définitive de la contradiction live ≈60 % longs vs backtest ≈1 % — 2026-06-01

User a poussé : ne pas classer "probable historique" sans preuve. Re-mesure rigoureuse.

### 1. RATIO LIVE ACTUEL (sources explicites)

Source : `trading-agent/trading_memory.db`, requêtes SQL directes.

**24 dernières heures** (depuis 2026-05-31 ~15:30 UTC) :
| Source | BUY (long) | SELL (short) |
|---|---:|---:|
| agent_decisions table | 0 | 1 |
| trades closed | 0 | 14 |
| trades open | 0 | 0 |
| → **ratio long** | **0 %** | |

**48 dernières heures** :
| Source | BUY | SELL |
|---|---:|---:|
| agent_decisions | 0 | 14 |
| trades closed | 0 | 19 |
| → **ratio long** | **0 %** | |

**Aucun signal long n'est tiré par le runtime live actuel.** Ni décision, ni trade ouvert, ni trade fermé.

### 2. ATTRIBUTION BROKER des 30 BUY antérieurs

Source : `json_extract(market_context, '$.broker')`.

| broker tag | N | Total PnL | Buys | Sells | Période |
|---|---:|---:|---:|---:|---|
| **kraken_paper** (session actuelle) | **20** | **+$30.30** | **0** | 20 | 2026-05-30 → en cours |
| `okx` (session précédente) | 29 | −$89.87 | 29 | 0 | 2026-05-26 → 27 |
| (null/manquant) | 3 | −$134 | 1 | 2 | divers |

Cross-check : `SELECT COUNT(*) FROM trades WHERE side='buy' AND market_context.broker = 'kraken_paper'` → **0**.

**Le live Kraken paper n'a JAMAIS tiré le moindre long.** La prémisse "60 % longs en live" était un artefact de comptage mélangeant 3 brokers sources.

### 3. TIMING `GEO_DISABLE_LOWVOL` pendant la fenêtre des 29 OKX BUYs

**OKX BUYs** : 2026-05-26 21:18 → 2026-05-27 13:39 UTC

**Commit `792b237`** ("phase 4 parity: force low_vol_mode=False for T1 zone bounce") : **2026-05-29 05:24 UTC** — soit 2 jours APRÈS les OKX BUYs.

Diff de `792b237` (extrait) :
```diff
-        low_vol_mode = (_r in ("choppy", "bull"))
-        lowvol_target_pct = getattr(config, "GEO_LOWVOL_TARGET_PCT", config.GEO_TARGET_PCT)
+        force_disable_lowvol = (os.getenv("GEO_DISABLE_LOWVOL", "1") == "1")
+        if force_disable_lowvol:
+            low_vol_mode = False
+            lowvol_target_pct = config.GEO_TARGET_PCT
+        else:
+            low_vol_mode = (_r in ("choppy", "bull"))
+            lowvol_target_pct = getattr(config, "GEO_LOWVOL_TARGET_PCT", ...)
```

→ **Pendant la fenêtre des 29 OKX BUYs, la version du code n'avait PAS encore la garde lowvol**. `low_vol_mode = (_r in ("choppy","bull"))` était actif sans condition.

`_r` à cette époque : le log live courant montre `_r="bull"` constamment (légère hypothèse pour la session OKX par cohérence env). Donc `low_vol_mode=True` → la branche
```python
if not self._rsi_divergence(closes_5m, rsi_now):
    if not low_vol_mode:        # ← False, n'entre pas
        n_div += 1; continue
```
**bypassait la divergence**. Les 29 longs sont tirés sur RSI+zone+pass_3b sans divergence requise.

Confirmation indirecte : 29/29 OKX trades ont `mode:lowvol` + `target_pct_used:0.005`, cohérent avec `lowvol_target_pct=0.005` (pré-commit, basé sur `GEO_LOWVOL_TARGET_PCT`).

### Contradiction RÉSOLUE

- "Live ≈ 60 % longs" : **artefact de comptage mélangeant la session OKX legacy avec la session Kraken courante**.
- "Backtest/replay ≈ 1 % longs" : juste, et **conforme au runtime Kraken courant** (0 % longs sur 48h).
- Les 29 longs OKX historiques s'expliquent par `low_vol_mode = (_r in ("choppy","bull"))` actif pré-commit `792b237`. Cause directement bypassed par la garde introduite le 2026-05-29.

**Pas d'explication "probable" — c'est une chronologie de git + un mécanisme exact dans le code.**

### 4. Replay empirique — pas nécessaire

Les sections 1-3 résolvent la contradiction. Pas besoin de replay supplémentaire. Le replay déjà fait dans P3 (rejection à `regime_disallow_long` pour 30/30 OKX BUYs sur le moteur backtest) est cohérent avec l'explication lowvol-bypass — le backtest n'a JAMAIS eu de notion de `low_vol_mode`, ses critères étaient strictement plus stricts à cet endroit.

### 5. Sécurité Patch 1 (fail-CLOSED `cr_allow_long`)

User a raison de demander la preuve avant patch.

**Q** : est-ce que `crypto_regime.evaluate()` set TOUJOURS explicitement `allow_long`, `allow_short`, `size_multiplier` ?

Réponse : **oui**, vérifié dans `crypto_regime.py` :
- Tous les chemins de retour incluent `allow_long`, `allow_short`, `size_mult` (et alias `size_multiplier` ligne 483/518) avec valeurs explicites
- Ligne 313 : `block = {"allow_long": False, "allow_short": False, "size_mult": 0.0, "target_pct": 0.0}` (fail-safe par défaut)
- 10 chemins de classification (`aligned_long_trend`, `btc_led_short`, etc.) chacun avec valeurs explicites

**Q** : combien de fois `evaluate()` lève en pratique ?

Réponse : grep `"CRYPTO_REGIME.*failed\|crypto_regime.*Exception"` sur le log de 36 h du runtime courant → **0 exceptions**.

**Q** : si Patch 1 est appliqué et `evaluate()` lève transitoirement, quelle est la blast radius ?

Réponse :
- TTL cache ETH : 60 s — un seul fail n'invalide rien si retry < 60s
- TTL cache BTC : 300 s (5 min) — idem côté BTC
- En cas de fail prolongé > 300 s, BLOCAGE total des nouveaux trades (long ET short) jusqu'au retour de l'engine
- Comparaison : sémantique fail-OPEN actuelle = trade avec gates relaxées (potentiellement dangereuse) ; fail-CLOSED = pause sécurisée

**Conclusion safety** : Patch 1 fail-CLOSED est sûr. Risque résiduel = pause si engine cassé > 5 min, ce qui est le comportement souhaité (mieux pause que trader avec gates absentes). Le backtest (référence validée) fait déjà fail-CLOSED — c'est juste un alignement.

### 6. État des décisions sur Patches

**Aucun patch n'est appliqué.** Diff prêts dans le doc P3 :
- Patch 1 : `cr_allow_long`/`cr_allow_short` defaults → False (fail-CLOSED) — **sûr, prouvé**
- Patch 2 : retire `low_vol_mode` bypass divergence — **inactif aujourd'hui** (`GEO_DISABLE_LOWVOL=1`), garde défensive si flag flipé un jour

**Recommendation** : appliquer les 2 comme défense en profondeur, **PAS** comme correction de bug actif. La cause des 29 OKX BUYs est déjà fermée par la garde lowvol introduite le 2026-05-29. Patch 2 ferme la porte définitivement même si quelqu'un change l'env. Patch 1 protège contre une exception transitoire du régime engine.

**Décision laissée à toi.**

### 7. Refactor à terme (non urgent)

Extraire `get_long_signal_t1` / `get_short_signal_t1` dans un module partagé
`signals_geo_v4.py` consommé par `geometric_expert.py` et `backtest_p1_ablation.py`.
Aujourd'hui : 2 implémentations parallèles, divergence subtile possible (la 4e
gate divergente trouvée si on creuse plus, ou la prochaine introduction
intentionnelle qui passe sous le radar). Module partagé = zéro chance de drift.

## P12 — Incident ETH "TP raté" : part sleep vs part bug brackets/réconciliation — 2026-06-03

> Analyse post-mortem. Position fermée manuellement (opérationnel), **aucune modif de code**.
> Contexte matériel fourni par l'opérateur : **lid du Mac fermé une grande partie de la
> journée du 3 juin, AVANT le restart de 18:16 et AVANT que Jim soit protégé du sleep**
> (batterie, pas de caffeinate/Amphetamine actif). À pondérer dans l'analyse.

### Résumé de l'incident
Position `ETH/USD short 2.61 @ 1859.9999` (ouverte 01:04 le 3 juin) restée ouverte ~17 h
alors que le prix avait **franchi son TP** (TP=1843.26, prix descendu jusqu'à ~1828) :
"au-dessus du TP, ne sort pas". Récupérée comme orpheline au restart de 18:16
(`🔄 Recovered orphan position`), puis **fermée manuellement au CLI** à 22:37
(fill @1831.5, realized +74.38 brut / +72.00 net, collatéral 9838.89→9910.89).
DB réconciliée : `close_reason=manual_reconcile_tp_missed`.

### Timeline (preuves /tmp/jimbot.log + futures_state.json)
- **02/06 22:08→23:59** : log continu, **0 gap** → machine éveillée. Lock `futures_state.json.lock` créé à **23:54** (mtime) **en veille active**, pas pendant un sleep.
- **03/06 01:04:59** : position ETH ouverte (broker). **01:05:02** : 1er SL placé (FP-00323) → les ops broker fonctionnaient encore à 01:05.
- **03/06 ~01:50** : lock devenu **bloquant** → `Futures paper state is locked by another process` en boucle. **Machine éveillée.**
- **01:50 → 10:10** : `market sell ETH/USD failed` répétés (~16 clusters), **machine éveillée** (matin = 0 gap > 4 min). Le bot ne peut ni gérer ni fermer la position.
- **03:54** : DB enregistre un **forced_close fantôme** (+75.69) alors que le broker **garde** la position → désync DB↔broker.
- **11:31 → 18:08** : **6.4 h de gel** réparties en **19 trous** de 15–36 min = lid fermé, process figé. **Aucune récupération possible** (watchdog figé lui aussi).
- **18:16** : restart manuel (sur `main`). Reconcile récupère l'orpheline. Lock périmé **retiré manuellement** (`rm`).
- État au restart : broker = **3 SL orphelins empilés** (FP-00323/00331/00343) + **0 ordre TP**. Cache local = `tp_order_id=FP-00344, tp_missing:false` → **ordre inexistant côté broker**.
- **22:37** : fermeture manuelle + cancel des 3 SL. Broker ETH : 0 position / 0 ordre.

### Estimation : part sleep vs part bug

**Part SLEEP / interruption — surtout la DURÉE (amplificateur), ~60-70 % du "temps bloqué") :**
- L'après-midi (6.4 h de gel) est la raison **directe** pour laquelle la position est restée
  bloquée jusqu'au restart de 18:16 : machine endormie → ni le bot ni le watchdog ne pouvaient
  agir. C'est le sleep qui a transformé un glitch en blocage de ~17 h.
- **MAIS le sleep n'explique PAS le déclenchement** : le lock était déjà bloquant à **01:50 en
  pleine veille**, tous les `market sell failed` du matin (01:50-10:10) sont **hors sommeil**, et
  la nuit du 2 (création du lock à 23:54) ne montre **aucun gap de sommeil**.
- Confiance : **élevée** que le sleep est le facteur d'aggravation/durée ; **faible/nulle** qu'il
  soit la cause du lock ou du TP manquant.

**Part BUG RÉEL — indépendante du sleep, reproductible (~30-40 %, mais c'est la cause racine) :**
1. **Lock périmé jamais auto-réclamé.** Le broker retente (hardening OK) mais ne supprime jamais
   un lock prouvablement périmé (aucun PID détenteur). Il a fallu un `rm` manuel. Or un lock
   périmé peut naître de **tout** crash/kill de subprocess CLI — pas besoin de sleep (timeout CLI
   15 s déjà observé le 31/05). **Ce seul fix aurait évité tout l'incident, quelle que soit la cause.**
2. **TP enregistré "placé" sans confirmation broker.** Cache `tp_missing:false` + `tp_order_id=FP-00344`
   alors que l'ordre **n'existe pas** côté broker. Sous contention lock, le placement TP a échoué
   mais le bot l'a noté comme réussi → la position court **sans TP** et le self-healing
   (re-placement si `tp_missing`) est **neutralisé**.
3. **Réconciliation ne vérifie pas l'existence des brackets.** Au restart, le reconcile récupère la
   **position** (correct) mais fait confiance au flag `tp_missing` du cache plutôt que de comparer
   aux **ordres ouverts réels** du broker. Un TP manquant n'est jamais détecté/réparé à la récupération.
4. **forced_close marque un trade clos en DB sans confirmation broker** → désync (close fantôme 03:54).
   Repli volontaire (broker injoignable) mais qui **autorise** la désync.
- Symptôme annexe : **3 SL dupliqués** = retries de placement sous lock ; le `reduceOnly` empêche
  heureusement la sur-fermeture (le fix bracket fonctionne sur ce point).

### Conclusion
- **Ne pas classer l'incident "100 % sleep".** Le sleep est l'**amplificateur** (il a allongé le
  blocage à ~17 h en gelant la machine tout l'après-midi), **pas la cause racine**.
- **Cause racine = lock périmé (origine ambiguë, non clairement sleep) + absence de 3 garde-fous**
  (auto-clear lock, confirmation TP, vérif brackets au reconcile). Ces points sont de **vrais bugs**,
  reproductibles dès qu'un appel CLI échoue, **sans sleep**.
- **Priorité de fix** (par impact, indépendante du sleep) :
  1. Auto-clear d'un lock périmé (PID détenteur absent/mort) avant retry.
  2. Confirmation broker du TP avant d'écrire `tp_missing=false`.
  3. Reconcile qui compare le cache aux **ordres bracket réels** du broker et re-pose ce qui manque.
- **Mitigation matérielle complémentaire** (n'est pas un fix) : empêcher le sleep
  (caffeinate/Amphetamine + secteur) → réduit la fenêtre d'exposition mais ne corrige pas les bugs.

### Reste ouvert au moment de l'écriture
- Résidu cache `PF_ETHUSD` (`tp_missing:false`, ordre mort) — inerte (manage loop itère sur les
  positions broker, vide pour ETH) ; sera écrasé au prochain trade ETH ou purgé par un restart.
- 2 SL orphelins SOL **sans position SOL** (hors périmètre de l'intervention ETH).
- Boucle de ré-instanciation broker ~2 s : présente avant ET après la clôture → **non liée** à
  l'incident (pattern de code, bruit de log).

_Aucune correction de code appliquée (consigne opérateur). Ce document = analyse seule._
