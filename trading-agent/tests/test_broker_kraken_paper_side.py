"""
Anti-régression : get_positions() doit utiliser le champ `side` de l'API Kraken,
pas le signe de `size` (Kraken Futures renvoie size toujours positif).

Bug historique 2026-05-29 : ghost short 4.88 affiché comme long → close_position
a envoyé SELL → la position est passée à short 9.76 au lieu d'être fermée.
"""
import sys, os, unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestBrokerSide(unittest.TestCase):
    def _make_broker(self):
        from broker_kraken_paper import KrakenPaperBroker
        return KrakenPaperBroker()

    def _cli_response(self, side: str, size: float = 4.88):
        return {
            "positions": [{
                "symbol": "PF_ETHUSD",
                "side": side,
                "size": size,
                "entry_price": 1995.62,
                "mark_price": 1997.91,
                "unrealized_pnl": -22.29 if side == "short" else +22.29,
            }]
        }

    def test_short_position_mapped_correctly(self):
        with patch("broker_kraken_paper._cli", return_value=self._cli_response("short")):
            positions = self._make_broker().get_positions()
        self.assertEqual(len(positions), 1)
        p = positions[0]
        self.assertEqual(p.side, "short", "Short Kraken doit rester short")
        self.assertEqual(p.qty, 4.88)

    def test_long_position_mapped_correctly(self):
        with patch("broker_kraken_paper._cli", return_value=self._cli_response("long")):
            positions = self._make_broker().get_positions()
        self.assertEqual(positions[0].side, "long")

    def test_kraken_size_always_positive_does_not_default_to_long(self):
        """Régression : si Kraken renvoie side='short' + size=9.76 (positif),
        on doit obtenir short, PAS long (le bug précédent)."""
        with patch("broker_kraken_paper._cli", return_value=self._cli_response("short", 9.76)):
            p = self._make_broker().get_positions()[0]
        self.assertEqual(p.side, "short")
        self.assertEqual(p.qty, 9.76)

    def test_fallback_to_sign_when_side_missing(self):
        """Compat : si un broker ne renvoie pas `side`, fallback sur le signe."""
        resp = {"positions": [{"symbol": "PF_ETHUSD", "size": -4.88, "entry_price": 2000}]}
        with patch("broker_kraken_paper._cli", return_value=resp):
            p = self._make_broker().get_positions()[0]
        self.assertEqual(p.side, "short")

    def test_close_position_routes_buy_for_short(self):
        """close_position d'un SHORT doit envoyer un BUY (pas un SELL)."""
        calls = []
        def fake_cli(*args):
            calls.append(args)
            if args == ("futures", "paper", "positions"):
                return self._cli_response("short")
            return {"status": "filled"}
        with patch("broker_kraken_paper._cli", side_effect=fake_cli):
            self._make_broker().close_position("ETH/USD")
        # Find the close order call (not the positions read)
        order_calls = [c for c in calls if len(c) >= 4 and c[2] in ("buy", "sell")]
        self.assertTrue(order_calls, "close_position aurait dû passer un ordre")
        self.assertEqual(order_calls[0][2], "buy",
                         "close d'un short doit être BUY, pas SELL")


if __name__ == "__main__":
    unittest.main()
