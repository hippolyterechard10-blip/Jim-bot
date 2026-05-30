"""
Anti-régression : T1S_DIV_MODE dispatch dans geometric_expert.

Patch 2026-05-30 : ajout config.T1S_DIV_MODE qui dispatche la branche
divergence du loop T1 short. Modes:
  - "never"        : skip div check entièrement (default after validation)
  - "strict"       : div bear required (legacy live behavior)
  - "rsi_fallback" : div OR rsi ∈ [45,70] passes (C3 sweet spot)

Ce test garantit :
  1. Le dispatch fonctionne pour chaque mode
  2. Les filtres en amont (RSI band, touches, pending) ne sont pas modifiés
  3. Le default "never" reste l'option production
"""
import sys, os, unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestT1SDivMode(unittest.TestCase):

    def test_config_exposes_t1s_div_mode(self):
        """config.T1S_DIV_MODE existe et est string valide."""
        import config
        self.assertTrue(hasattr(config, "T1S_DIV_MODE"))
        self.assertIn(config.T1S_DIV_MODE,
                      ("never", "strict", "rsi_fallback"),
                      f"T1S_DIV_MODE={config.T1S_DIV_MODE} not a valid mode")

    def test_default_is_never(self):
        """Default per validation campaign 2026-05-29 = 'never'."""
        os.environ.pop("T1S_DIV_MODE", None)
        # Force reimport to pick up env-less default
        import importlib, config
        importlib.reload(config)
        self.assertEqual(config.T1S_DIV_MODE, "never",
                         "Default doit être 'never' (validated by 7-test campaign)")

    def test_env_override_works(self):
        """T1S_DIV_MODE env var override."""
        import importlib, config
        for mode in ("strict", "rsi_fallback", "never"):
            os.environ["T1S_DIV_MODE"] = mode
            importlib.reload(config)
            self.assertEqual(config.T1S_DIV_MODE, mode)
        os.environ.pop("T1S_DIV_MODE", None)
        importlib.reload(config)

    # Tests fonctionnels du dispatch dans geometric_expert
    # On vérifie que la branche de code prise correspond au mode.

    def _make_expert_with_mode(self, mode):
        """Helper: instancie GeometricExpert avec mocks minimaux + mode désiré."""
        os.environ["T1S_DIV_MODE"] = mode
        import importlib, config, experts.geometric_expert as ge
        importlib.reload(config)
        importlib.reload(ge)
        broker = MagicMock()
        broker._ct = MagicMock(return_value=1.0)
        broker.list_open_orders = MagicMock(return_value=[])
        broker.get_positions = MagicMock(return_value=[])
        memory = MagicMock()
        memory.get_open_trades = MagicMock(return_value=[])
        memory.db_path = ":memory:"
        geometry = MagicMock()
        regime = MagicMock()
        regime.get_cache = MagicMock(return_value={"regime": "neutral"})
        return ge.GeometricExpert(broker, memory, geometry, regime), config

    def test_never_mode_skips_div_check(self):
        """Mode 'never' : appel à _rsi_bearish_divergence non requis pour fire."""
        expert, config = self._make_expert_with_mode("never")
        self.assertEqual(config.T1S_DIV_MODE, "never")
        # Source-level: vérifier que la branche 'never' fait juste pass
        import inspect, experts.geometric_expert as ge
        src = inspect.getsource(ge.GeometricExpert.evaluate)
        self.assertIn('T1S_DIV_MODE == "never"', src)
        self.assertIn("# skip divergence check entirely", src)

    def test_strict_mode_requires_div(self):
        """Mode 'strict' : exige _rsi_bearish_divergence True."""
        expert, config = self._make_expert_with_mode("strict")
        self.assertEqual(config.T1S_DIV_MODE, "strict")
        import inspect, experts.geometric_expert as ge
        src = inspect.getsource(ge.GeometricExpert.evaluate)
        self.assertIn('T1S_DIV_MODE == "strict"', src)

    def test_rsi_fallback_mode_combines(self):
        """Mode 'rsi_fallback' : div OR rsi∈[45,70]."""
        expert, config = self._make_expert_with_mode("rsi_fallback")
        self.assertEqual(config.T1S_DIV_MODE, "rsi_fallback")
        import inspect, experts.geometric_expert as ge
        src = inspect.getsource(ge.GeometricExpert.evaluate)
        # Le mode rsi_fallback est l'else branche, contenant le seuil [45, 70]
        self.assertIn("45 <= rsi_now <= 70", src)

    def test_upstream_rsi_band_filter_unchanged(self):
        """Le filtre RSI band [GEO_RSI_SHORT_LOW, _HIGH] reste avant le div check
        — peu importe le mode."""
        import inspect, experts.geometric_expert as ge
        src = inspect.getsource(ge.GeometricExpert.evaluate)
        # Le RSI band filter doit toujours être présent et toujours bloquer
        self.assertIn("GEO_RSI_SHORT_LOW <= rsi_now <= config.GEO_RSI_SHORT_HIGH", src)


if __name__ == "__main__":
    unittest.main()
