"""
Anti-régression P0 (Opus 4.8 audit 2026-05-30) :
- _place_stop_close et _place_limit_close DOIVENT setter reduceOnly=true
  (sinon SL orphelin → phantom position après fill du TP jumeau en LIVE)
- cancel_twin_brackets DOIT exister et nettoyer le jumeau quand un fill
  natif est détecté

Avant fix: en LIVE Kraken Futures, après fill natif du TP, le SL twin restait
actif sur le matching engine. Si le prix le touchait plus tard, le broker
ouvrait une NOUVELLE position (short à nu après TP long). reduceOnly=true
empêche ça côté broker; cancel_twin_brackets purge côté bot.
"""
import sys, os, unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_broker():
    os.environ.setdefault("KRAKEN_API_KEY", "test_key")
    os.environ.setdefault("KRAKEN_API_SECRET", "dGVzdF9zZWNyZXQ=")
    os.environ.setdefault("KRAKEN_PAPER", "1")
    from kraken_broker import KrakenBroker
    return KrakenBroker()


def _sendorder_ok(order_id="ORD-1"):
    return {"result": "success", "sendStatus": {"order_id": order_id}}


class TestReduceOnlyBrackets(unittest.TestCase):

    def test_stop_close_has_reduce_only_true(self):
        """_place_stop_close doit envoyer reduceOnly=true sur sendorder."""
        b = _make_broker()
        captured = {}
        def fake_post(path, data):
            captured["data"] = data
            return _sendorder_ok()
        with patch.object(b, "_post", side_effect=fake_post):
            b._place_stop_close("PF_ETHUSD", "sell", 1.0, 1990.0)
        self.assertEqual(captured["data"].get("reduceOnly"), "true",
                         "Stop close DOIT être reduceOnly=true (P0 fix SL orphelin)")
        self.assertEqual(captured["data"]["orderType"], "stp")
        self.assertEqual(captured["data"]["side"], "sell")

    def test_limit_close_has_reduce_only_true(self):
        """_place_limit_close doit envoyer reduceOnly=true."""
        b = _make_broker()
        captured = {}
        def fake_post(path, data):
            captured["data"] = data
            return _sendorder_ok()
        with patch.object(b, "_post", side_effect=fake_post):
            b._place_limit_close("PF_ETHUSD", "sell", 1.0, 2018.0)
        self.assertEqual(captured["data"].get("reduceOnly"), "true",
                         "Limit close DOIT être reduceOnly=true (P0 fix TP orphelin)")
        self.assertEqual(captured["data"]["orderType"], "lmt")

    def test_short_close_brackets_also_reduce_only(self):
        """Pour un SHORT, les close brackets (buy side) restent reduceOnly=true."""
        b = _make_broker()
        captured = {}
        def fake_post(path, data):
            captured.setdefault("calls", []).append(data)
            return _sendorder_ok()
        with patch.object(b, "_post", side_effect=fake_post):
            b._place_stop_close("PF_ETHUSD", "buy", 1.0, 2020.0)
            b._place_limit_close("PF_ETHUSD", "buy", 1.0, 1980.0)
        for c in captured["calls"]:
            self.assertEqual(c.get("reduceOnly"), "true",
                             "SHORT brackets aussi reduceOnly")
            self.assertEqual(c["side"], "buy")

    def test_cancel_twin_brackets_exists(self):
        """Méthode cancel_twin_brackets doit exister (geometric_expert l'appelle)."""
        b = _make_broker()
        self.assertTrue(hasattr(b, "cancel_twin_brackets"),
                        "cancel_twin_brackets manquante — geometric_expert ne pourra pas nettoyer")

    def test_cancel_twin_brackets_calls_cancel_and_pops(self):
        """cancel_twin_brackets doit appeler cancel_order pour les 2 twins + pop _sltp."""
        b = _make_broker()
        b._sltp["PF_ETHUSD"] = {
            "side": "long", "sl": 1990, "tp": 2018, "qty": 1.0,
            "sl_order_id": "SL-1", "tp_order_id": "TP-1",
        }
        cancelled = []
        def fake_cancel(symbol, oid):
            cancelled.append(oid)
            return True
        with patch.object(b, "cancel_order", side_effect=fake_cancel):
            n = b.cancel_twin_brackets("ETH/USD")
        self.assertEqual(n, 2, "Doit canceller 2 twins")
        self.assertSetEqual(set(cancelled), {"SL-1", "TP-1"})
        self.assertNotIn("PF_ETHUSD", b._sltp,
                         "_sltp doit être popped quand les 2 twins gone")

    def test_cancel_twin_brackets_exclude_param(self):
        """exclude_order_id permet de skip un jumeau (déjà filled par exemple)."""
        b = _make_broker()
        b._sltp["PF_ETHUSD"] = {
            "side": "long", "sl": 1990, "tp": 2018, "qty": 1.0,
            "sl_order_id": "SL-1", "tp_order_id": "TP-1",
        }
        cancelled = []
        def fake_cancel(symbol, oid):
            cancelled.append(oid)
            return True
        with patch.object(b, "cancel_order", side_effect=fake_cancel):
            # Simule: TP-1 vient d'être filled → on garde, on annule SL-1
            n = b.cancel_twin_brackets("ETH/USD", exclude_order_id="TP-1")
        self.assertEqual(cancelled, ["SL-1"],
                         "Doit ne canceller que le SL (TP exclu)")


if __name__ == "__main__":
    unittest.main()
