"""
Anti-régression kraken_broker.py (LIVE) — bug LONG-ONLY pré-2026-05-29.

Avant patch, kraken_broker.py :
  - get_positions filtrait `side != "long"` → shorts invisibles
  - close_position hardcodait `side: "sell"` → SHORT close = SELL = double position
  - place_limit_sell n'existait pas → AttributeError sur T1S/T2 short
  - list_open_orders filtrait `side != "buy"` → ordres short GTC invisibles
  - get_close_info ne cherchait que les fills `side == "sell"` → exits de short jamais détectés

Ces tests garantissent qu'on ne réintroduit pas ces bugs.
Pattern : mocker `_get` et `_post` du broker (pas HTTP réel).
"""
import sys, os, unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_broker():
    # config import side-effect free with empty env
    os.environ.setdefault("KRAKEN_API_KEY", "test_key")
    os.environ.setdefault("KRAKEN_API_SECRET", "dGVzdF9zZWNyZXQ=")   # base64("test_secret")
    os.environ.setdefault("KRAKEN_PAPER", "1")
    from kraken_broker import KrakenBroker
    return KrakenBroker()


def _openpos_response(side: str, size: float = 4.88,
                      entry: float = 2000.0, mark: float = 2010.0,
                      pnl: float = -10.0):
    """Mock réponse /derivatives/api/v3/openpositions."""
    return {
        "result": "success",
        "openPositions": [{
            "symbol": "PF_ETHUSD",
            "side": side,
            "size": size,
            "price": entry,
            "pnl": pnl,
        }],
    }


def _sendorder_ok(order_id: str = "ORD-123"):
    return {"result": "success", "sendStatus": {"order_id": order_id}}


class TestKrakenBrokerSide(unittest.TestCase):

    # ── get_positions ────────────────────────────────────────────────────────

    def test_short_position_visible(self):
        """Régression : un SHORT doit être visible (avant: filtré silencieusement)."""
        b = _make_broker()
        with patch.object(b, "_get", return_value=_openpos_response("short")), \
             patch.object(b, "get_live_price", return_value=2010.0):
            positions = b.get_positions()
        self.assertEqual(len(positions), 1, "SHORT doit apparaître dans get_positions")
        self.assertEqual(positions[0].side, "short")
        self.assertEqual(positions[0].qty, 4.88)

    def test_long_position_visible(self):
        b = _make_broker()
        with patch.object(b, "_get", return_value=_openpos_response("long")), \
             patch.object(b, "get_live_price", return_value=2010.0):
            positions = b.get_positions()
        self.assertEqual(positions[0].side, "long")
        self.assertEqual(positions[0].qty, 4.88)

    def test_size_kraken_always_positive(self):
        """Kraken renvoie size positif même pour SHORT. Position.qty doit être positif."""
        b = _make_broker()
        with patch.object(b, "_get", return_value=_openpos_response("short", size=9.76)), \
             patch.object(b, "get_live_price", return_value=2010.0):
            p = b.get_positions()[0]
        self.assertEqual(p.qty, 9.76)
        self.assertEqual(p.side, "short")

    # ── close_position dispatch (le bug du jour) ─────────────────────────────

    def test_close_short_sends_buy(self):
        """Régression critique : close d'un SHORT doit envoyer BUY, sinon DOUBLE.
        C'est exactement le bug du 2026-05-29 sur paper (4.88 short → 9.76 short)."""
        b = _make_broker()
        captured = {}
        def fake_post(path, data):
            captured["path"] = path
            captured["data"] = data
            return _sendorder_ok()
        with patch.object(b, "_get", return_value=_openpos_response("short")), \
             patch.object(b, "get_live_price", return_value=2010.0), \
             patch.object(b, "_post", side_effect=fake_post):
            ok = b.close_position("ETH/USD")
        self.assertTrue(ok)
        self.assertEqual(captured["data"]["side"], "buy",
                         "close SHORT doit être BUY, pas SELL (sinon double position)")
        self.assertEqual(captured["data"]["orderType"], "mkt")
        self.assertEqual(captured["data"]["symbol"], "PF_ETHUSD")

    def test_close_long_sends_sell(self):
        b = _make_broker()
        captured = {}
        def fake_post(path, data):
            captured["data"] = data
            return _sendorder_ok()
        with patch.object(b, "_get", return_value=_openpos_response("long")), \
             patch.object(b, "get_live_price", return_value=2010.0), \
             patch.object(b, "_post", side_effect=fake_post):
            b.close_position("ETH/USD")
        self.assertEqual(captured["data"]["side"], "sell")

    # ── place_limit_sell exists & valid ──────────────────────────────────────

    def test_place_limit_sell_exists_and_caches_side_short(self):
        """Bug pré-patch : pas de place_limit_sell → AttributeError sur T1S/T2."""
        b = _make_broker()
        self.assertTrue(hasattr(b, "place_limit_sell"),
                        "place_limit_sell doit exister (utilisé par T1S/T2)")
        with patch.object(b, "_post", return_value=_sendorder_ok("ORD-SHORT")):
            order_id = b.place_limit_sell(
                symbol="ETH/USD", price=2000.0,
                stop_loss=2020.0, take_profit=1970.0,
                deploy_usdt=200.0,
            )
        self.assertEqual(order_id, "ORD-SHORT")
        # Cache _sltp doit contenir side="short" pour router le close
        self.assertEqual(b._sltp["PF_ETHUSD"]["side"], "short")
        self.assertEqual(b._sltp["PF_ETHUSD"]["sl"], 2020.0)
        self.assertEqual(b._sltp["PF_ETHUSD"]["tp"], 1970.0)

    def test_place_limit_sell_rejects_invalid_sltp(self):
        """SHORT: SL > entry > TP. Inverse = invalid."""
        b = _make_broker()
        # Cas invalides : SL en-dessous entry (style LONG)
        oid = b.place_limit_sell("ETH/USD", price=2000, stop_loss=1980,
                                  take_profit=2050, deploy_usdt=200)
        self.assertIsNone(oid)

    def test_place_limit_buy_caches_side_long(self):
        b = _make_broker()
        with patch.object(b, "_post", return_value=_sendorder_ok("ORD-LONG")):
            b.place_limit_buy("ETH/USD", price=2000, stop_loss=1980,
                              take_profit=2018, deploy_usdt=200)
        self.assertEqual(b._sltp["PF_ETHUSD"]["side"], "long")

    # ── list_open_orders ─────────────────────────────────────────────────────

    def test_list_open_orders_includes_sells(self):
        """Régression : avant filtrait `side != buy` → ordres SELL invisibles."""
        b = _make_broker()
        with patch.object(b, "_get", return_value={
            "result": "success",
            "openOrders": [
                {"order_id": "BUY-1", "symbol": "PF_ETHUSD", "side": "buy",  "limitPrice": 2000},
                {"order_id": "SELL-1","symbol": "PF_ETHUSD", "side": "sell", "limitPrice": 2050},
            ],
        }):
            orders = b.list_open_orders()
        sides = {o.id: o.side for o in orders}
        self.assertEqual(len(orders), 2)
        self.assertEqual(sides, {"BUY-1": "buy", "SELL-1": "sell"})

    # ── _ensure_sltp dispatch ────────────────────────────────────────────────

    def test_ensure_sltp_routes_long_to_sell_orders(self):
        b = _make_broker()
        b._sltp["PF_ETHUSD"] = {
            "side": "long", "sl": 1980, "tp": 2018, "qty": 1.0,
            "sl_order_id": None, "tp_order_id": None,
        }
        from kraken_broker import Position
        pos = Position("PF_ETHUSD", 1.0, 2000, 2000, 0.0, side="long")
        calls = []
        def fake_post(path, data):
            calls.append(data)
            return _sendorder_ok(f"ORD-{len(calls)}")
        with patch.object(b, "_post", side_effect=fake_post):
            b._ensure_sltp(pos)
        sides = [c["side"] for c in calls]
        self.assertEqual(sides, ["sell", "sell"],
                         "LONG: SL et TP doivent être des SELL (close-long)")

    def test_ensure_sltp_routes_short_to_buy_orders(self):
        b = _make_broker()
        b._sltp["PF_ETHUSD"] = {
            "side": "short", "sl": 2020, "tp": 1970, "qty": 1.0,
            "sl_order_id": None, "tp_order_id": None,
        }
        from kraken_broker import Position
        pos = Position("PF_ETHUSD", 1.0, 2000, 2000, 0.0, side="short")
        calls = []
        def fake_post(path, data):
            calls.append(data)
            return _sendorder_ok(f"ORD-{len(calls)}")
        with patch.object(b, "_post", side_effect=fake_post):
            b._ensure_sltp(pos)
        sides = [c["side"] for c in calls]
        self.assertEqual(sides, ["buy", "buy"],
                         "SHORT: SL et TP doivent être des BUY (close-short)")

    # ── get_close_info side-aware ────────────────────────────────────────────

    def test_get_close_info_short_finds_buy_fill(self):
        """Pour un SHORT, le close fill est un BUY. Avant ne cherchait que SELL."""
        b = _make_broker()
        b._sltp["PF_ETHUSD"] = {
            "side": "short", "sl": 2020, "tp": 1970, "qty": 1.0,
            "sl_order_id": "SL-1", "tp_order_id": "TP-1",
        }
        with patch.object(b, "_get", return_value={
            "fills": [{
                "symbol": "PF_ETHUSD",
                "side": "buy",            # close-short fill
                "price": 1970.0,
                "size": 1.0,
                "fillTime": "2026-05-29T14:00:00Z",
            }],
        }):
            info = b.get_close_info("ETH/USD")
        self.assertIsNotNone(info, "fill BUY doit être détecté comme close-short")
        self.assertEqual(info["reason"], "target",
                         "fill @ 1970 = TP target pour short (entry 2000)")

    def test_get_close_info_long_finds_sell_fill(self):
        b = _make_broker()
        b._sltp["PF_ETHUSD"] = {
            "side": "long", "sl": 1980, "tp": 2018, "qty": 1.0,
            "sl_order_id": "SL-1", "tp_order_id": "TP-1",
        }
        with patch.object(b, "_get", return_value={
            "fills": [{
                "symbol": "PF_ETHUSD",
                "side": "sell",
                "price": 2018.0,
                "size": 1.0,
                "fillTime": "2026-05-29T14:00:00Z",
            }],
        }):
            info = b.get_close_info("ETH/USD")
        self.assertEqual(info["reason"], "target")

    def test_get_close_info_short_stop_detection(self):
        """Short SL hit : fill BUY @ price >= SL."""
        b = _make_broker()
        b._sltp["PF_ETHUSD"] = {
            "side": "short", "sl": 2020, "tp": 1970, "qty": 1.0,
        }
        with patch.object(b, "_get", return_value={
            "fills": [{
                "symbol": "PF_ETHUSD", "side": "buy",
                "price": 2025.0, "size": 1.0,
                "fillTime": "2026-05-29T14:00:00Z",
            }],
        }):
            info = b.get_close_info("ETH/USD")
        self.assertEqual(info["reason"], "stop")


if __name__ == "__main__":
    unittest.main()
