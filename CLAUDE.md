# CLAUDE.md — Mémoire projet pour Claude Code

Ce fichier est lu automatiquement par Claude Code au démarrage. Il résume le
contexte du projet pour qu'une nouvelle session Claude reprenne sans re-briefer.

---

## Vue d'ensemble

**Jim Bot** est un bot de trading algorithmique exécutant la stratégie **GEO V4**
sur Kraken Futures (perpétuels ETH/USD et SOL/USD).

**Branche active** : `claude/jim-bot-kraken-futures-Zo9t6` (toutes les modifs
récentes sont ici, `main` est en retard).

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
- `trading-agent/kraken_broker.py` — broker Kraken Futures :
  - `place_limit_buy` (long) + `place_limit_sell` (short)
  - SL + TP natifs sur le matching engine (survivent au crash bot)
  - OHLCV via `/api/charts/v1/trade` (pas de yfinance)
  - Signature HMAC-SHA512 sur `SHA256(postData + nonce + endpoint)`
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

## État actuel (au 19/04/2026)

**Bot arrêté** — la clé API Kraken génère `authenticationError` sur tous les
endpoints Futures. Test diagnostic confirme :

- ✅ Connectivité Kraken OK (endpoint public)
- ✅ Pas de whitespace dans `.env`, lengths correctes (KEY 56 / SECRET 88)
- ✅ Mon code de signature est correct (testé 2 schemes `/accounts` et `/api/v3/accounts`)
- ❌ La clé elle-même n'est pas reconnue par Kraken

**Hypothèses** : clé désactivée, compte futures pas pleinement activé, ou
permissions manquantes. **Action requise** : régénérer la clé sur
pro.kraken.com avec :
- API générale : Accès complet ✅
- API de retrait : **AUCUN ACCÈS** ⚠️ (sécurité, le bot n'en a pas besoin)
- IP whitelist : `178.104.145.1` (VPS)

---

## Phases de déploiement

### Phase 1 — Validation mécanique (en cours)

- Capital $100 (déjà déposé sur Futures wallet)
- ETH-only, `MAX_SIM=1`, `POS_PCT=1.00`, **short activé** (`GEO_ENABLE_SHORT=1`)
- Objectif : ~30 trades pour valider fills, SL/TP, pas de crash
- Pas de jugement de rendement (trop peu de trades)

`.env` Phase 1 :
```
ACTIVE_BROKER=kraken
KRAKEN_API_KEY=<clé>
KRAKEN_API_SECRET=<secret>
KRAKEN_PAPER=0
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

1. **Crash `okx_symbol`** dans `_reconcile_state` (`geometric_expert.py`) →
   référence à un attribut absent sur KrakenBroker. Remplacé par accès
   broker-agnostique.
2. **Signature HMAC-SHA512** dans `kraken_broker.py` → ordre incorrect
   (était `nonce + endpoint + hex(sha256(...))`, maintenant correct
   `SHA256(postData + nonce + endpoint)` puis HMAC sur bytes).
3. **OHLCV via yfinance** remplacé par endpoint natif Kraken `/api/charts/v1/trade`.
4. **broker_kraken.py** (stub incomplet) supprimé, `main_kraken.py` pointe sur
   `kraken_broker.py` (complet).

---

## Conventions

- Commentaires en français OK dans le code
- **Pas de quote dans le `.env`** (juste `KEY=VALUE`)
- Tester chaque modif avec `python -m py_compile <file>.py` avant commit
- `git push` sur la branche `claude/jim-bot-kraken-futures-Zo9t6`, **pas main**
- Quand auth Kraken passe : `systemctl restart jimbot` puis `tail -f /var/log/jimbot.log`
- Vérifier `[GEO] evaluating ETH/USD | régime=...` tourne toutes les 5 min

---

## Pour reprendre

Si tu démarres une session sans contexte, lance dans l'ordre :
1. `git status` et `git log --oneline -10`
2. Lire ce fichier + la page Notion liée plus haut
3. Si question sur la stratégie : `head -50 trading-agent/experts/geometric_expert.py`
4. Si question backtest : `cat trading-agent/backtest_realistic.csv`
