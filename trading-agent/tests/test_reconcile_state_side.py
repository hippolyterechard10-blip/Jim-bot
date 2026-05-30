"""
Anti-régression : _reconcile_state side dispatch (fix 2026-05-30).

Bug légacy : `_reconcile_state` hardcodait `side="buy"` et `ctx_side="long"`
quand il récupérait un orphan position broker, ignorant `pos.side`. Conséquence
sur une orphan SHORT (T1S fired during downtime) :
  - DB enregistre side=buy → manage_open_positions calcule PnL avec mult=+1
    au lieu de -1
  - SL calculé sous entry (correct pour long, WRONG pour short)
  - TP calculé au-dessus entry (correct pour long, WRONG pour short)
  - close_position broker dispatch OK (broker fix yesterday saves us),
    mais le PnL reporté à log_trade_close sera inversé.

Ce test garantit que reconcile recovers les shorts comme shorts avec SL/TP
corrects.
"""
import sys, os, unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_pos(db_symbol, side, qty, entry_price):
    """Mock Position object with the attributes _reconcile_state reads."""
    pos = MagicMock()
    pos.db_symbol = db_symbol
    pos.side = side
    pos.qty = qty
    pos.avg_entry_price = entry_price
    pos.current_price = entry_price
    return pos


class TestReconcileSide(unittest.TestCase):

    def setUp(self):
        os.environ.setdefault("T1S_DIV_MODE", "never")
        os.environ.setdefault("ROUTER_VARIANT", "t1_neutral")
        os.environ.setdefault("T1S_INCLUDE_HARD_BLOCK", "1")
        # Reset modules to ensure fresh state per test
        import importlib, config, experts.geometric_expert as ge
        importlib.reload(config)
        importlib.reload(ge)
        self.ge = ge

    def _build_expert_with_orphan(self, orphan_pos):
        """Build a GeometricExpert with mocks and a single orphan position."""
        broker = MagicMock()
        broker.list_open_orders = MagicMock(return_value=[])
        broker.get_positions = MagicMock(return_value=[orphan_pos])
        memory = MagicMock()
        memory.get_open_trades = MagicMock(return_value=[])
        memory.db_path = ":memory:"
        # log_trade_open : capture call args for inspection
        memory.log_trade_open = MagicMock()
        geometry = MagicMock()
        regime = MagicMock()
        regime.get_cache = MagicMock(return_value={"regime": "neutral"})
        return self.ge.GeometricExpert(broker, memory, geometry, regime), memory

    def test_orphan_long_recovered_as_long(self):
        """Orphan LONG broker position → DB side=buy, ctx_side=long, SL<entry<TP."""
        pos = _make_pos("ETH/USD", "long", qty=1.0, entry_price=2000.0)
        expert, memory = self._build_expert_with_orphan(pos)
        self.assertEqual(memory.log_trade_open.call_count, 1,
                         "Orphan doit être logué comme nouveau trade open")
        kwargs = memory.log_trade_open.call_args.kwargs
        self.assertEqual(kwargs["side"], "buy", "LONG orphan → side=buy")
        ctx = kwargs["market_context"]
        self.assertEqual(ctx["side"], "long")
        self.assertTrue(kwargs["stop_loss"] < 2000.0,
                        f"LONG SL ({kwargs['stop_loss']}) doit être SOUS entry (2000)")
        self.assertTrue(kwargs["take_profit"] > 2000.0,
                        f"LONG TP ({kwargs['take_profit']}) doit être AU-DESSUS entry (2000)")
        self.assertTrue(ctx["reconciled"])

    def test_orphan_short_recovered_as_short(self):
        """Orphan SHORT broker position → DB side=sell, ctx_side=short, SL>entry>TP.

        Régression critique : avant fix, SHORT était tracké comme LONG → PnL faux."""
        pos = _make_pos("SOL/USD", "short", qty=90.0, entry_price=82.0)
        expert, memory = self._build_expert_with_orphan(pos)
        self.assertEqual(memory.log_trade_open.call_count, 1)
        kwargs = memory.log_trade_open.call_args.kwargs
        self.assertEqual(kwargs["side"], "sell",
                         "SHORT orphan doit être logué side=sell, pas buy (legacy bug)")
        ctx = kwargs["market_context"]
        self.assertEqual(ctx["side"], "short",
                         "SHORT orphan doit avoir ctx_side=short, pas long (legacy bug)")
        self.assertTrue(kwargs["stop_loss"] > 82.0,
                        f"SHORT SL ({kwargs['stop_loss']}) doit être AU-DESSUS entry (82)")
        self.assertTrue(kwargs["take_profit"] < 82.0,
                        f"SHORT TP ({kwargs['take_profit']}) doit être SOUS entry (82)")
        self.assertTrue(ctx["reconciled"])

    def test_target_pct_used_positive_for_both_sides(self):
        """target_pct_used doit être un positif raisonnable (~GEO_TARGET_PCT)
        peu importe le sens — utilisé par _mode_from_target_pct pour classifier."""
        for side_name, pos in [
            ("long",  _make_pos("ETH/USD", "long",  1.0,  2000.0)),
            ("short", _make_pos("ETH/USD", "short", 1.0,  2000.0)),
        ]:
            expert, memory = self._build_expert_with_orphan(pos)
            ctx = memory.log_trade_open.call_args.kwargs["market_context"]
            self.assertIsNotNone(ctx.get("target_pct_used"))
            self.assertGreater(ctx["target_pct_used"], 0,
                               f"{side_name}: target_pct_used doit être positif")
            self.assertLess(ctx["target_pct_used"], 0.05,
                            f"{side_name}: target_pct_used doit être < 5% (raisonnable)")


if __name__ == "__main__":
    unittest.main()
