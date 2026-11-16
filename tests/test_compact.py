import json
import time
import unittest
from dataclasses import replace

from compact import (Anchors, Refusals, body_size, error_body, listed_windows, model_info_windows, over, window_for)
from routing import DEFAULT_CONFIG

def IMG(n):
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * n}}


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

    def test_listed_window_ceiling(self):
        # above 10,000,000 is beyond the manual-window range routing validates against: ignored like one
        p = {"data": [{"id": "big", "context_length": 10_000_001}, {"id": "ok", "context_length": 10_000_000}]}
        self.assertEqual(listed_windows(p), {"ok": 10_000_000})
        self.assertIsNone(window_for(replace(DEFAULT_CONFIG.downstreams[0], context=None), "big",
                                     {"valar": {"big": 10_000_001}}))

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
        self.assertEqual(a.estimate("k", "m", 4000), 1000)       # no anchor: bytes/4
        a.note("k", "m", 40000, 9000)
        self.assertEqual(a.estimate("k", "m", 48000), 11000)    # anchor + growth/4
        self.assertEqual(a.estimate("k", "m", 30000), 9000)      # shrink never goes below the anchor's figure

    def test_anchor_is_model_aware(self):
        # a token figure measured on one model's tokenizer never estimates another model's window
        a = Anchors()
        a.note("k", "m", 40000, 9000)
        self.assertEqual(a.estimate("k", "n", 40000), 10000)     # the m anchor is ignored: bytes/4
        a.note("k", "n", 100000, 30000)                          # n's own answer replaces it
        self.assertEqual((a.estimate("k", "m", 40000), a.estimate("k", "n", 40000)), (10000, 30000))
        a.drop("k")                                              # drop is by conversation, whatever the model
        self.assertEqual(a.estimate("k", "n", 40000), 10000)

    def test_anchor_dropped_after_compaction(self):
        a = Anchors(); a.note("k", "m", 4_000_000, 800_000); a.drop("k")
        self.assertEqual(a.estimate("k", "m", 400_000), 100_000)

    def test_zero_counts_are_never_anchored(self):
        # Kimi-style zeroed usage, or a count_tokens answer of 0, would disable the check for the session
        a = Anchors()
        a.note("k", "m", 4000, 0)
        self.assertEqual(a.estimate("k", "m", 4000), 1000)
        a.note("k", "m", 4000, 100)
        a.note("k", "m", 4000, 0, grow_only=True)
        self.assertEqual(a.estimate("k", "m", 4000), 100)

    def test_count_tokens_never_shrinks_an_anchor(self):
        # a partial count_tokens body (the tool definitions, a stub message) is no reason to lose the
        # exact anchor the last served answer noted
        a = Anchors()
        a.note("k", "m", 4000, 100, grow_only=True)              # with no anchor: noted
        self.assertEqual(a.estimate("k", "m", 4000), 100)
        a.note("k", "m", 2000, 500, grow_only=True)              # a shorter body: kept as it was
        self.assertEqual(a.estimate("k", "m", 4000), 100)
        a.note("k", "m", 5000, 700, grow_only=True)              # a fuller one replaces
        self.assertEqual(a.estimate("k", "m", 4000), 700)
        a.note("k", "m", 2000, 500)                              # a served answer is exact: always replaced
        self.assertEqual(a.estimate("k", "m", 2000), 500)

    def test_note_reinserts_a_hot_key(self):
        # subagent churn fills the map past its cap: a key re-noted between the inserts is re-inserted as
        # newest, so it survives while an old untouched one is evicted first-in-first-out
        a = Anchors()
        a.note("old", "m", 4_000_000, 800_000)
        for n in range(4200):
            a.note("k%d" % n, "m", 4, 4)
            if n % 500 == 0:
                a.note("hot", "m", 4_000_000, 900_000)
        self.assertEqual(a.estimate("hot", "m", 4_000_000), 900_000)    # survived: its anchor, not bytes/4
        self.assertEqual(a.estimate("old", "m", 4_000_000), 1_000_000)  # evicted: bytes/4 again

    def test_body_size_counts_images_flat(self):
        body = {"messages": [{"role": "user", "content": ["x" * 400, IMG(24000), {"type": "text", "text": "y" * 100}]}]}
        raw = len(json.dumps(body))
        # the base64 data (24,000 bytes) counts at IMAGE_COST_BYTES flat, not bytes/4 as tokens
        self.assertEqual(body_size(body, raw), raw - 24000 + 6000)
        nested = {"messages": [{"role": "user", "content": ["x" * 100]},
                                {"role": "user", "content": [
                                    {"type": "tool_result", "content": [IMG(12000), {"type": "text", "text": "z"}]}]}]}
        raw = len(json.dumps(nested))
        self.assertEqual(body_size(nested, raw), raw - 12000 + 6000)
        # a malformed block is skipped, never raised; an image without base64 data is not counted either
        self.assertEqual(body_size({"messages": [{"role": "user", "content": [
            {"type": "image"}, {"type": "image", "source": 7}, {"type": "image", "source": {"data": 5}}]}]}, 100), 100)
        for bad in ("body", None, {}, {"messages": [{"role": "user", "content": "text"}]},
                    {"messages": "nope"}, {"messages": None}):
            self.assertEqual(body_size(bad, 12345), 12345)

    def test_over_max_tokens(self):
        # the limit is the smaller of 90% of the window and the window minus the request's max_tokens
        # (Valar's context_length counts prompt plus output), never below half the window
        self.assertIsNone(over(899, 1000))
        self.assertEqual(over(900, 1000), 900)
        self.assertIsNone(over(799, 1000, max_tokens=200))
        self.assertEqual(over(800, 1000, max_tokens=200), 800)
        self.assertEqual(over(500, 1000, max_tokens=900), 500)   # a huge max_tokens floors at half the window
        self.assertIsNone(over(450, 1000, max_tokens=900))
        self.assertIsNone(over(450, 1000, max_tokens=100))       # without max_tokens it is the bare 90% again

    def test_over_and_body(self):
        self.assertEqual((over(764_999, 850_000), over(765_000, 850_000)), (None, 765000))
        self.assertEqual(json.loads(error_body(780000, 765000)), {"type": "error", "error": {"type": "invalid_request_error",
                         "message": "prompt is too long: 780000 tokens > 765000 maximum"}})

    def test_refusals_loop_breaker(self):
        r = Refusals()
        self.assertTrue(r.refuse("k"))       # the first refusal of a streak goes out
        self.assertFalse(r.refuse("k"))      # refused within the minute: the downstream's own error decides
        self.assertTrue(r.refuse("k"))      # the memory was cleared by the let-through
        r.refuse("k")
        r.clear("k")                         # a served request ends the streak
        self.assertTrue(r.refuse("k"))
        r._map["k"] = time.time() - 61       # an hour-old refusal is no streak
        self.assertTrue(r.refuse("k"))

    def test_malformed_payloads_never_raise(self):
        self.assertEqual(listed_windows({"data": ["nope", None, {"id": 3}, {"id": "ok", "context_length": "big"}, 7]}), {})
        self.assertEqual(model_info_windows({"data": [None, {"model_name": 4}, {"model_name": "m", "model_info": "x"}, "nope"]}), {})


if __name__ == "__main__":
    unittest.main()
