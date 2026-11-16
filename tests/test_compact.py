import json
import unittest
from dataclasses import replace

from compact import Anchors, error_body, listed_windows, model_info_windows, over, window_for
from routing import DEFAULT_CONFIG


class TestCompact(unittest.TestCase):
    def test_listed_windows(self):
        p = {"data": [{"id": "a", "context_length": 850000}, {"id": "b", "max_input_tokens": 200000},
                      {"id": "c", "max_model_len": 131072}, {"id": "d"}, {"id": "e", "context_length": 0}]}
        self.assertEqual(listed_windows(p), {"a": 850000, "b": 200000, "c": 131072})
        self.assertEqual(listed_windows({}), {})

    def test_listed_window_floor(self):
        # a listed window under 1000 is nonsense (a broken list or hand-edit): ignored, never a "0K of 0K" refusal
        p = {"data": [{"id": "t999", "context_length": 999}, {"id": "t512", "context_length": 512},
                      {"id": "ok", "context_length": 1000}]}
        self.assertEqual(listed_windows(p), {"ok": 1000})
        info = {"data": [{"model_name": "t999", "model_info": {"max_input_tokens": 999}},
                         {"model_name": "t512", "model_info": {"max_input_tokens": 512}},
                         {"model_name": "ok", "model_info": {"max_input_tokens": 1000}}]}
        self.assertEqual(model_info_windows(info), {"ok": 1000})
        ds = replace(DEFAULT_CONFIG.downstreams[0], context=None)
        listed = {"valar": {"t999": 999, "t512": 512, "ok": 1000}}
        self.assertEqual([window_for(ds, m, listed) for m in ("t999", "t512", "ok")], [None, None, 1000])

    def test_model_info_windows(self):
        p = {"data": [{"model_name": "ds", "model_info": {"max_input_tokens": 900000}},
                      {"model_name": "ds", "model_info": {"max_input_tokens": 850000}},
                      {"model_name": "x", "model_info": {"max_input_tokens": None}}]}
        self.assertEqual(model_info_windows(p), {"ds": 850000})

    def test_window_for(self):
        ds = replace(DEFAULT_CONFIG.downstreams[0], context={"m": 500000})
        listed = {"valar": {"m": 850000, "n": 128000}}
        self.assertEqual((window_for(ds, "m", listed), window_for(ds, "n", listed), window_for(ds, "z", listed)), (500000, 128000, None))
        # a hand-edited models.json can give a listed value that isn't a window
        self.assertEqual([window_for(replace(ds, context=None), "m", {"valar": {"m": v}}) for v in ("850000", 0, True, None)],
                         [None, None, None, None])

    def test_estimate(self):
        a = Anchors()
        self.assertEqual(a.estimate("k", 4000), 1000)            # no anchor: bytes/4
        a.note("k", 40000, 9000)
        self.assertEqual(a.estimate("k", 48000), 11000)          # anchor + growth/4
        self.assertEqual(a.estimate("k", 30000), 9000)           # shrink never goes below the anchor's figure

    def test_anchor_dropped_after_compaction(self):
        a = Anchors(); a.note("k", 4_000_000, 800_000); a.drop("k")
        self.assertEqual(a.estimate("k", 400_000), 100_000)

    def test_over_and_body(self):
        self.assertEqual((over(764_999, 850_000), over(765_000, 850_000)), (None, 765000))
        self.assertEqual(json.loads(error_body(780000, 765000)), {"type": "error", "error": {"type": "invalid_request_error",
                         "message": "prompt is too long: 780000 tokens > 765000 maximum"}})

    def test_malformed_payloads_never_raise(self):
        self.assertEqual(listed_windows({"data": ["nope", None, {"id": 3}, {"id": "ok", "context_length": "big"}, 7]}), {})
        self.assertEqual(model_info_windows({"data": [None, {"model_name": 4}, {"model_name": "m", "model_info": "x"}, "nope"]}), {})


if __name__ == "__main__":
    unittest.main()
