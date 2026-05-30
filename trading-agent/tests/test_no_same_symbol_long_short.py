"""
Anti-régression P1 (Opus 4.8 audit 2026-05-30) :
evaluate() ne doit JAMAIS ouvrir un LONG puis un SHORT sur le même symbole
dans le même appel.

Bug pré-patch :
  - Check `symbol_active >= 1` à l'entrée de evaluate() ne couvre que les
    positions/pendings AVANT l'appel.
  - Si LONG router ouvre une position (via place_limit_buy + _pending_add),
    le code continue vers SHORT router sans re-checker.
  - SHORT router vérifie `open_count < GEO_MAX_SIM=2` qui ne reflète pas
    l'ajout du pending LONG (open_count_global est figé au début).
  - → LONG + SHORT pouvaient s'ouvrir simultanément sur le même symbole.
  - Sur perp Kraken Futures = netting → position nette zéro + double fees.

Test garantit que la défense `_pending_for_symbol(symbol) >= 1` rentre en jeu
entre LONG et SHORT.
"""
import sys, os, unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestNoSameSymbolLongShort(unittest.TestCase):

    def setUp(self):
        # Force the live config to match Phase 4.1 runtime
        os.environ.setdefault("T1S_DIV_MODE", "never")
        os.environ.setdefault("ROUTER_VARIANT", "t1_neutral")
        os.environ.setdefault("T1S_INCLUDE_HARD_BLOCK", "1")
        os.environ.setdefault("GEO_DISABLE_LOWVOL", "1")
        import importlib, config, experts.geometric_expert as ge
        importlib.reload(config)
        importlib.reload(ge)
        self.ge = ge
        self.config = config

    def _build_expert(self, pending_long_present: bool):
        """Build expert with mocked broker + memory.
        If pending_long_present=True, _pending has a long entry for ETH/USD
        (simulating that a LONG was just opened earlier in the same evaluate())."""
        broker = MagicMock()
        broker.list_open_orders = MagicMock(return_value=[])
        broker.get_positions = MagicMock(return_value=[])
        broker.place_limit_buy = MagicMock(return_value="ORD-BUY")
        broker.place_limit_sell = MagicMock(return_value="ORD-SELL")
        memory = MagicMock()
        memory.get_open_trades = MagicMock(return_value=[])
        memory.db_path = ":memory:"
        geometry = MagicMock()
        regime = MagicMock()
        regime.get_cache = MagicMock(return_value={"regime": "neutral"})
        expert = self.ge.GeometricExpert(broker, memory, geometry, regime)

        if pending_long_present:
            # Simulate that a LONG just opened earlier in same evaluate()
            zk = expert._zone_key(2000.0)
            expert._pending_add(zk, {
                "order_id": "ORD-BUY",
                "symbol":   "ETH/USD",
                "level":    2000.0,
                "high":     2006.0,
                "stop":     1990.0,
                "target":   2018.0,
                "deploy":   5000.0,
                "side":     "long",
            })

        return expert, broker, memory

    def test_pending_for_symbol_correctly_counts_long(self):
        """Le helper _pending_for_symbol doit voir le pending LONG ajouté."""
        expert, _, _ = self._build_expert(pending_long_present=True)
        self.assertEqual(expert._pending_for_symbol("ETH/USD"), 1,
                         "Helper doit voir le pending long")
        self.assertEqual(expert._pending_for_symbol("SOL/USD"), 0,
                         "Helper ne doit pas confondre les symbols")

    def test_short_router_skipped_when_long_pending_same_symbol(self):
        """Régression P1 : si _pending_for_symbol(symbol) >= 1 (long déjà ouvert
        plus tôt dans le même evaluate), le SHORT router doit return immédiatement
        AVANT d'évaluer aucun signal short."""
        import inspect
        src = inspect.getsource(self.ge.GeometricExpert.evaluate)
        # Verify the guard exists between long section and short section
        self.assertIn("_pending_for_symbol(symbol) >= 1", src,
                      "Le guard P1 bugfix doit être présent dans evaluate()")
        # Verify it's before the short section
        idx_guard = src.find("_pending_for_symbol(symbol) >= 1")
        idx_short_section = src.find("# ── SHORTS : multi-thesis router")
        idx_short_router_signal = src.find("ENABLE_T2_SHORT")
        self.assertGreater(idx_guard, idx_short_section,
                           "Guard doit être APRÈS le header SHORTS")
        self.assertLess(idx_guard, idx_short_router_signal,
                        "Guard doit être AVANT l'évaluation T2/T1S signal")

    def test_helper_pending_for_symbol_handles_empty(self):
        """_pending_for_symbol retourne 0 quand _pending est vide."""
        expert, _, _ = self._build_expert(pending_long_present=False)
        self.assertEqual(expert._pending_for_symbol("ETH/USD"), 0)


if __name__ == "__main__":
    unittest.main()
