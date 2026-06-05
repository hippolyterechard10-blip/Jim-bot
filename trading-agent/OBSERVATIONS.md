# OBSERVATIONS — notes opérationnelles (hors code)

Journal de constats issus du monitoring de Jim. **Aucune action de code ici** —
seulement des observations datées à transformer en tickets si validées.

---

## 2026-06-05 — Pipeline post-trade inactif + priorité short fill logging

Constats relevés via le recap matinal (`jim-morning-recap`, lecture seule de
`trading_memory.db`) :

### 1. `trade_analyses` est VIDE (0 ligne)
- Le pipeline d'analyse post-trade n'a **jamais été activé** : la table
  `trade_analyses` (schéma dans `memory.py`) ne contient aucune ligne, alors que
  `trades` en compte 87 (dont 19 clôturés sur les dernières 24 h).
- Conséquence : aucune capture de `lessons` / `mistakes` / `strategy_adj`, donc
  pas de boucle d'amélioration automatisée ni de mémoire des erreurs.
- Méthodes existantes mais jamais alimentées : `memory.save_trade_analysis(...)`,
  `memory.get_closed_trades_unanalyzed(...)`.
- **Piste (à valider, non appliquée)** : brancher une analyse post-clôture qui
  appelle `save_trade_analysis` pour chaque trade fermé non analysé.

### 2. `forced_close×10` confirme la priorité du *short fill logging*
- Répartition des `close_reason` en base : `target×29`, `stop×28`, `timeout×19`,
  **`forced_close×10`**, `manual_reconcile_tp_missed×1`.
- Les `forced_close` (+ le `manual_reconcile_tp_missed`) sont des sorties
  **non-standards** : sur les 24 h observées, plusieurs surviennent sur des shorts
  avec P&L ≈ 0 et durée de holding quasi nulle (ex. SOL/USD short fermé en < 1 s),
  signature typique d'un état de fill/position mal tracé côté short.
- Cela **confirme la priorité de l'item connu « short fill logging »** (cf. issues
  Phase 2) : tant que les fills short ne sont pas tracés proprement, la
  réconciliation produit des `forced_close` parasites qui polluent les stats.
- **Piste (à valider, non appliquée)** : instrumenter le chemin de fill short
  (broker paper + live) avant d'augmenter la taille ou de passer en live.

### Contexte santé du jour
- Process vivant, dashboard 200, boucle active. WR 24 h = 47 % (cible 42–48 %),
  P&L net +$45.96, MDD intraday −0.29 % (< seuil −0.8 %). Rien d'urgent côté risque.
- 100 % des trades 24 h sont des **shorts** → d'où l'importance du point 2.

_Source : recap READ-ONLY du 2026-06-05. Aucune modif de boucle / config.py /
fichiers stratégie._
