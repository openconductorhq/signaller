import contextlib
import io
import json
import os
import tempfile
import unittest

import prices
from prices import Prices, parse_valar
from routing import Downstream

MODELS = dict.fromkeys(("fable", "opus", "sonnet", "haiku"), "m")
VALAR = Downstream("valar", "Valar", "http://v", "valar", MODELS, "#000", dict.fromkeys(MODELS, "asap"))


def generic(prices=None):
    return Downstream("lemmi", "Lemmi", "http://l", "generic", MODELS, "#000", None, prices)


PAYLOAD = {"data": [
    {"id": "moonshotai/Kimi-K3", "pricing": [
        {"tier": "realtime", "input_per_1m_tokens": 2.25, "cached_input_per_1m_tokens": 0.225, "output_per_1m_tokens": 11.25},
        {"tier": "priority", "input_per_1m_tokens": 1.6875, "cached_input_per_1m_tokens": 0.16875, "output_per_1m_tokens": 8.4375},
        {"tier": "standard", "input_per_1m_tokens": 1.125, "cached_input_per_1m_tokens": 0.1125, "output_per_1m_tokens": 5.625},
        {"tier": "flex", "input_per_1m_tokens": 0.5, "cached_input_per_1m_tokens": 0.05, "output_per_1m_tokens": 2.5},
        {"tier": "batch", "input_per_1m_tokens": 0.4, "cached_input_per_1m_tokens": 0.04, "output_per_1m_tokens": 2}]},
    {"id": "valar-auto-balanced", "pricing": []}]}
M = 1_000_000


class PricesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.p = Prices(self.dir)
        self.p.set_listed("valar", parse_valar(PAYLOAD))

    def test_parse_valar(self):
        got = parse_valar(PAYLOAD)
        self.assertEqual(got, {"moonshotai/Kimi-K3": {"asap": [2.25, 0.225, 11.25], "priority": [1.6875, 0.16875, 8.4375],
                                                      "standard": [1.125, 0.1125, 5.625]}})

    def test_valar_cost(self):
        u = {"in": M, "out": 0, "cache_read": 0, "cache_write": 500}
        self.assertAlmostEqual(self.p.cost(VALAR, "moonshotai/Kimi-K3", "standard", u), 1.125)  # cache write free
        u = {"in": M, "out": M, "cache_read": M, "cache_write": None}
        self.assertAlmostEqual(self.p.cost(VALAR, "moonshotai/Kimi-K3", "asap", u), 2.25 + 11.25 + 0.225)
        self.assertIsNone(self.p.cost(VALAR, "nope", "asap", u))
        self.assertIsNone(self.p.cost(VALAR, "moonshotai/Kimi-K3", None, u))
        self.assertIsNone(self.p.cost(VALAR, "moonshotai/Kimi-K3", "asap", dict.fromkeys(u)))
        self.assertEqual(self.p.cost(None, "claude-opus-5-5", None, u), 0.0)

    def test_api_cost(self):
        u = {"in": 1e6, "out": 1e6, "cache_read": 1e6, "cache_write": 1e6}
        self.assertAlmostEqual(self.p.api_cost("claude-opus-5-5", u), 29.2)
        self.assertAlmostEqual(self.p.api_cost("claude-3-5-foo", u), 2 + 10 + 0.2 + 2.5)
        self.assertAlmostEqual(self.p.api_cost("claude-haiku-4-5", {"in": M, "out": None, "cache_read": M, "cache_write": None}),
                               0.11)
        self.assertIsNone(self.p.api_cost("claude-opus-5-5", {}))

    def test_prices_json_written_and_honoured(self):
        with open(os.path.join(self.dir, "prices.json")) as f:
            self.assertEqual(json.load(f), prices.DEFAULT_ANTHROPIC)
        with open(os.path.join(self.dir, "prices.json"), "w") as f:
            json.dump({**prices.DEFAULT_ANTHROPIC, "opus": {"in": 1, "out": 0, "cache_read": 0, "cache_write": 0}}, f)
        self.assertAlmostEqual(Prices(self.dir).api_cost("claude-opus-5-5", {"in": M, "out": M}), 1.0)

    def test_unreadable_prices_json_defaults(self):
        with open(os.path.join(self.dir, "prices.json"), "w") as f:
            f.write("{nope")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            p = Prices(self.dir)
        self.assertAlmostEqual(p.api_cost("claude-opus-5-5", {"in": M}), 4.0)
        self.assertIn("prices.json", err.getvalue())

    def test_generic_manual_price(self):
        ds = generic({"in": 1, "out": 2, "cache_read": 0.5, "cache_write": 1})
        self.assertAlmostEqual(self.p.cost(ds, "m", None, {"in": M, "out": M, "cache_read": 0, "cache_write": 0}), 3.0)
        self.assertAlmostEqual(self.p.cost(ds, "m", None, {"in": None, "out": None, "cache_read": M, "cache_write": M}), 1.5)

    def test_generic_unpriced_is_none(self):
        self.assertIsNone(self.p.cost(generic(), "m", None, {"in": M}))
        self.assertIsNone(self.p.cost(generic({"in": 1, "out": 2, "cache_read": 0.5, "cache_write": 1}), "m", None, {}))

    def test_listed_prices_are_per_downstream(self):
        self.p.set_listed("v2", {"x": {"asap": [3, 0, 0]}})
        self.assertAlmostEqual(self.p.cost(VALAR._replace_id("v2") if False else Downstream("v2", "V2", "u", "valar", MODELS, "#0", dict.fromkeys(MODELS, "asap")), "x", "asap", {"in": M}), 3.0)
        self.assertIsNone(self.p.cost(VALAR, "x", "asap", {"in": M}))

    def test_old_valar_prices_key_loads(self):
        with open(os.path.join(self.dir, "models.json"), "w") as f:
            json.dump({"valar_prices": {"x": {"asap": [1, 0, 0]}}}, f)
        self.assertEqual(Prices(self.dir).listed, {"valar": {"x": {"asap": [1, 0, 0]}}})

    def test_valar_prices_loaded_from_models_json(self):
        with open(os.path.join(self.dir, "models.json"), "w") as f:
            json.dump({"valar": ["x"], "valar_prices": {"x": {"asap": [1, 0, 0]}}}, f)
        self.assertAlmostEqual(Prices(self.dir).cost(VALAR, "x", "asap", {"in": M}), 1.0)


if __name__ == "__main__":
    unittest.main()
