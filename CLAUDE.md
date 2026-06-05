# CLAUDE.md — Mémoire projet pour Claude Code

Ce fichier est lu automatiquement par Claude Code au démarrage. Il résume le
contexte du projet pour qu'une nouvelle session Claude reprenne sans re-briefer.

---

## Vue d'ensemble

**Jim Bot** est un bot de trading algorithmique exécutant la stratégie **GEO V4**
sur Kraken Futures (perpétuels ETH/USD et SOL/USD).

**Branche active** : `main`. C'est la ligne à jour : tout le travail récent y est,
et `origin/main` est synchro. La branche `claude/jim-bot-kraken-futures-Zo9t6` est
**archivée** au commit `672e8bb` — un vieil ancêtre, 36 commits derrière `main` au
2026-06-05 ; ne pas y travailler.

**Hub Notion** : <https://www.notion.so/348568d64f5c8184b8e6debdffa22546>
(état session du 19/04, blocage actuel, checklist de reprise).

---

## Architecture

- `trading-agent/experts/geometric_expert.py` — **stratégie GEO V4** :
  - Long sur bounces de support, Short sur rejets de résistance (miroir)
  - Zones ±0.3 % autour des pivots 15min
  - RSI [20-65] long / [35-80] short + divergence + Pass 3b (EMA momentum)
  - Stop dynamique sous wick / au-dessus wick, target ±0.9 %
  - Time-stop 4h, max 2 positions, R:R min 1.2
  - Short gated par `GEO_ENABLE_SHORT` (env)
  - `evaluate()` → dispatch vers `_eval_side("long"|"short")`
  - `_pending` / `_touches` keys en tuples `(dir, zone_key)` pour éviter collisions
- `trading-agent/kraken_broker.py` — broker Kraken Futures (LIVE, fix 2026-05-29) :
  - `place_limit_buy` (long) + `place_limit_sell` (short, ajouté)
  - `get_positions` lit `side` API (long ET short), pas le signe de `size`
  - `close_position` dispatche SELL pour long / BUY pour short selon `pos.side`
  - SL + TP natifs paramétrés par side dans `_ensure_sltp` :
    - long → SL stp sell + TP lmt sell
    - short → SL stp buy + TP lmt buy
  - `list_open_orders` retourne buy ET sell (entries + close orders)
  - `get_close_info` cherche le fill close-direction selon `_sltp.side` cached
  - Cache `_sltp` contient maintenant `side: "long"|"short"`
  - Signature HMAC-SHA512 sur `SHA256(postData + nonce + endpoint)`
  - ⚠️ OHLCV encore via `yfinance` (TODO Phase 2 : migrer vers `/api/charts/v1/trade`)
  - Tests anti-régression : `tests/test_kraken_broker_side.py` (14 tests)
- `trading-agent/broker_kraken_paper.py` — broker Kraken Futures Paper :
  - Wrap CLI `kraken futures paper`
  - `get_positions` lit `side` API (fix 2026-05-29)
  - Tests anti-régression : `tests/test_broker_kraken_paper_side.py` (5 tests)
- `trading-agent/config.py` — config env-driven :
  - `ACTIVE_BROKER=kraken` par défaut
  - `GEO_SYMBOLS`, `GEO_MAX_SIM`, `GEO_POS_PCT` env-configurables
- `trading-agent/main_kraken.py` — entry point (fast loop 30s + slow loop 300s + watchdog)
- `trading-agent/kraken_test.py` — diagnostic auth (5 tests isolés)
- `trading-agent/backtest_geo_realistic.py` — A/B test avec fees+slippage+taxes
- `deploy.sh` — install VPS systemd

---

## Déploiement VPS

- **VPS** : Hetzner Cloud, Nuremberg, `178.104.145.1` (Ubuntu 24)
- **Path** : `/opt/Jim-bot` (clone), `/opt/jimbot-venv` (Python venv)
- **Service systemd** : `jimbot.service` → exécute `main_kraken.py`
- **Logs** : `/var/log/jimbot.log`
- **Env file** : `/opt/Jim-bot/trading-agent/.env` (chmod 600)

Commandes utiles :
```bash
sudo systemctl {start,stop,restart,status} jimbot
sudo tail -f /var/log/jimbot.log
sudo /opt/jimbot-venv/bin/python /opt/Jim-bot/trading-agent/kraken_test.py
```

---

## État actuel

**Switch sur Kraken paper** (demo-futures.kraken.com) pour Phase 1 — aucun
risque, faux argent, validation mécanique pure.

Le précédent essai en live avait `authenticationError` sur tous les endpoints
Futures (clé live de pro.kraken.com non reconnue). Hypothèses : clé désactivée,
permissions manquantes, ou activation Futures incomplète. **On va plutôt** :

1. Créer un compte **demo séparé** sur https://demo-futures.kraken.com (compte
   différent du compte live — Kraken les isole totalement)
2. Générer une **clé API paper** sur ce compte demo (Settings → API Keys)
   - API générale : Accès complet ✅
   - API de retrait : aucun accès
3. Mettre `KRAKEN_PAPER=1` dans le `.env`
4. Le code switche automatiquement vers `https://demo-futures.kraken.com`
   (cf. `kraken_broker.py:base_url`)

Plus tard, passage en live :
- Régénérer une clé sur pro.kraken.com (compte live), permissions identiques
- IP whitelist : `178.104.145.1` (VPS) — important cette fois
- Retraits : **AUCUN accès** (le bot n'en a pas besoin)
- Passer `KRAKEN_PAPER=0`

---

## Phases de déploiement

### Phase 1 — Validation mécanique sur Kraken paper

- **Paper mode** : `KRAKEN_PAPER=1` → endpoint demo-futures.kraken.com
- Capital "virtuel" $100 (le compte demo Kraken est crédité auto)
- ETH-only, `MAX_SIM=1`, `POS_PCT=1.00`, **short activé** (`GEO_ENABLE_SHORT=1`)
- Objectif : ~30 trades pour valider fills, SL/TP, pas de crash
- Pas de jugement de rendement (paper fills trop bons + trop peu de trades)

`.env` Phase 1 (paper) :
```
ACTIVE_BROKER=kraken
KRAKEN_API_KEY=<clé demo générée sur demo-futures.kraken.com>
KRAKEN_API_SECRET=<secret demo>
KRAKEN_PAPER=1
INITIAL_CAPITAL=100
GEO_SYMBOLS=ETH/USD
GEO_MAX_SIM=1
GEO_POS_PCT=1.00
GEO_ENABLE_SHORT=1
```

### Phase 2 — Capital réel ($1 000, ETH+SOL)

Après ~30 trades OK en Phase 1 :
- INITIAL_CAPITAL=1000
- GEO_SYMBOLS=ETH/USD,SOL/USD
- GEO_MAX_SIM=2
- **GEO_POS_PCT=0.50** (impératif, sinon 200 % margin = liquidation)
- GEO_ENABLE_SHORT=1

---

## Attentes réalistes (backtest 2022-2023, fees Kraken 0.02/0.05 %, slippage 0.10 %, fill rate 85 %, taxe PFU 30 %)

| Capital | Net annuel PFU (backtest) | Attente live (×0.6-0.7) |
|---|---:|---:|
| $100 ETH-only | ~+$1-2 | quasi nul, juste test |
| $1 000 ETH+SOL | +$476 | ~+$300/an |
| $5 000 ETH+SOL | +$2 399 | ~+$1 500/an |
| $25 000 ETH+SOL | +$13 403 | ~+$8 500/an |

Variance forte entre années (2022 ranges = top, 2023 trends = juste positif).
**Sweet spot** : $5-25k.

---

## Bugs corrigés récemment

1. **Crash `okx_symbol`** dans `_reconcile_state` (`geometric_expert.py:130`) →
   référence à un attribut absent sur KrakenBroker. Remplacé par
   `getattr(o, "kraken_symbol", getattr(o, "symbol", None))` (broker-agnostique).
2. **Signature HMAC-SHA512** dans `kraken_broker.py` → ordre incorrect
   (était `nonce + endpoint + hex(sha256(...))`, maintenant correct
   `SHA256(postData + nonce + endpoint)` puis HMAC sur bytes).
3. **broker_kraken.py** (stub incomplet) supprimé, `main_kraken.py` pointe sur
   `kraken_broker.py` (complet).
4. **broker_kraken_paper.py LONG-ONLY** (2026-05-29) : `get_positions` dérivait
   le sens du SIGNE de `size` (toujours positif chez Kraken). Un T1S short paper
   apparaissait comme long → `close_position` envoyait SELL → position DOUBLÉE.
   Fix : lire le champ `side` API en priorité. Tests : 5 cases dans
   `tests/test_broker_kraken_paper_side.py`.
5. **kraken_broker.py LONG-ONLY** (2026-05-29) : même bug pattern, PLUS
   `place_limit_sell` n'existait pas et tous les helpers SL/TP hardcodaient
   `side: "sell"`. T1S/T2 short auraient crashé en Phase 2 LIVE. Fix complet :
   ajout `place_limit_sell`, helpers close paramétrisés par side, dispatch
   buy/sell dans `close_position`, `get_close_info` side-aware via cache `_sltp`,
   `list_open_orders` retourne buy ET sell. Tests : 14 cases dans
   `tests/test_kraken_broker_side.py`.
6. **T1S divergence filter dispatch** (2026-05-30) : `geometric_expert.py:934`
   exigeait `_rsi_bearish_divergence=True` en strict, ce qui filtrait des
   bons signaux. 7 backtests indépendants (portfolio + solo ETH + solo SOL +
   réaliste avec frictions + statistical artifact) ont convergé que le mode
   `never` (skip divergence) domine `strict` sur tous les axes (Sharpe ×2-3,
   n_trades ×10-15, WR +7 pts). Patch : `config.T1S_DIV_MODE` env var dispatch
   sur 3 modes (never|rsi_fallback|strict), default `never`. Tests : 7 cases
   dans `tests/test_t1s_div_mode.py`. Validation doc complète :
   vault `phase4-divergence-validation.md`.

## TODOs Phase 2 LIVE (avant d'activer short)

- [x] `kraken_broker.py` short support (fix 2026-05-29)
- [ ] Migrer OHLCV `yfinance` → Kraken `/api/charts/v1/trade` (yfinance unreliable, throttle silent)
- [ ] Vérifier auth Kraken live (compte pro.kraken.com, IP whitelist `178.104.145.1`)
- [ ] Smoke test paper avec `place_limit_sell` réel (T1S fire en t1_neutral bypass)
- [ ] Activer `KRAKEN_PAPER=0` seulement après ces 4 items

---

## Conventions

- Commentaires en français OK dans le code
- **Pas de quote dans le `.env`** (juste `KEY=VALUE`)
- Tester chaque modif avec `python -m py_compile <file>.py` avant commit
- Le travail vit sur `main` (et `origin/main`). Pas de workflow de branches imposé.
- Quand auth Kraken passe : `systemctl restart jimbot` puis `tail -f /var/log/jimbot.log`
- Vérifier `[GEO] evaluating ETH/USD | régime=...` tourne toutes les 5 min

---

## Pour reprendre

Si tu démarres une session sans contexte, lance dans l'ordre :
1. `git status` et `git log --oneline -10`
2. Lire ce fichier + la page Notion liée plus haut
3. Si question sur la stratégie : `head -50 trading-agent/experts/geometric_expert.py`
4. Si question backtest : `cat trading-agent/backtest_realistic.csv`
