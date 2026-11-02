import contextlib
import gzip
import io
import json
import random
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import claude_router
from claude_router import App, Config, make_server
from doubling import AnswerCapture, Doubler
from routing import DEFAULT_CONFIG, Doubling, Features
from tests.test_proxy import CLASSIFIER_OFF, HDRS, LEMMI

SHADOW = DEFAULT_CONFIG.doubling.shadow


def sse(*events):
    return b"".join(b"event: %s\ndata: %s\n\n" % (e["type"].encode(), json.dumps(e).encode()) for e in events)


SSE = sse(
    {"type": "message_start", "message": {"usage": {"output_tokens": 1}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " world"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "name": "Read", "input": {}}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"file_path"'}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": ': "a.py"}'}},
    {"type": "content_block_stop", "index": 1},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 12}},
    {"type": "message_stop"},
)
JSON = json.dumps({"content": [{"type": "text", "text": "Hi"},
                               {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}],
                   "usage": {"input_tokens": 3, "output_tokens": 5}}).encode()


class Up(ThreadingHTTPServer):
    """Fake Anthropic / Valar / LiteLLM: SSE when stream is true, JSON otherwise; delay applies to stream:false calls."""
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), UpHandler)
        self.seen, self.delay, self.encoding = [], 0, None
        threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.server_address[1]


class UpHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        self.server.seen.append((self.path, self.headers, json.loads(body)))
        stream = json.loads(body).get("stream")
        if not stream:
            time.sleep(self.server.delay)
        out = SSE if stream else JSON
        self.send_response(200)
        self.send_header("content-type", "text/event-stream" if stream else "application/json")
        if self.server.encoding:  # regardless of accept-encoding, like a misbehaving upstream
            out = gzip.compress(out) if self.server.encoding == "gzip" else b"\x00junk"
            self.send_header("content-encoding", self.server.encoding)
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def msg(text, model="claude-opus-5-5", stream=True):
    return json.dumps({"model": model, "stream": stream, "messages": [{"role": "user", "content": text}]}).encode()


def doubling(**kw):
    return replace(CLASSIFIER_OFF, doubling=replace(Doubling(1, "", SHADOW), **kw))


class CaptureTest(unittest.TestCase):
    def test_capture_sse_split_chunks(self):
        c = AnswerCapture("text/event-stream; charset=utf-8")
        for i in range(0, len(SSE), 7):
            c.feed(SSE[i:i + 7])
        r = c.result()
        self.assertEqual(r["text"], "Hello world")
        self.assertEqual(r["tools"], [{"name": "Read", "input": '{"file_path": "a.py"}'}])
        self.assertEqual(r["output_tokens"], 12)

    def test_capture_json(self):
        c = AnswerCapture("application/json")
        c.feed(JSON[:10])
        c.feed(JSON[10:])
        self.assertEqual(c.result(), {"error": None, "text": "Hi", "tools": [{"name": "Bash", "input": '{"command": "ls"}'}],
                                      "output_tokens": 5,
                                      "usage": {"in": 3, "out": 5, "cache_read": None, "cache_write": None}})

    def test_usage_sse_and_json(self):
        want = {"in": 10, "out": 7, "cache_read": 5, "cache_write": 2}
        u = {"input_tokens": 10, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 2}
        c = AnswerCapture("text/event-stream")
        c.feed(sse({"type": "message_start", "message": {"usage": {**u, "output_tokens": 1}}},
                   {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 7}}))
        self.assertEqual(c.result()["usage"], want)
        # real Valar streams (2026-10-29): the final split arrives in message_delta
        kimi = ({"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                {"input_tokens": 1, "output_tokens": 16, "cache_read_input_tokens": 3039, "cache_creation_input_tokens": 0})
        deepseek = ({"input_tokens": 3039, "output_tokens": 0},  # cache-inclusive total
                    {"input_tokens": 95, "output_tokens": 16, "cache_read_input_tokens": 2944, "cache_creation_input_tokens": 0})
        glm = ({"input_tokens": 13, "output_tokens": 0, "cache_read_input_tokens": 3008}, {"output_tokens": 3})
        for start, delta, w in ((*kimi, {"in": 1, "out": 16, "cache_read": 3039, "cache_write": 0}),
                                (*deepseek, {"in": 95, "out": 16, "cache_read": 2944, "cache_write": 0}),
                                (*glm, {"in": 13, "out": 3, "cache_read": 3008, "cache_write": None})):
            c = AnswerCapture("text/event-stream")
            c.feed(sse({"type": "message_start", "message": {"usage": start}},
                       {"type": "message_delta", "delta": {}, "usage": delta}))
            self.assertEqual(c.result()["usage"], w, start)
        c = AnswerCapture("application/json")
        c.feed(json.dumps({"usage": {**u, "output_tokens": 7}}).encode())
        self.assertEqual(c.result()["usage"], want)
        for ct, junk in (("text/event-stream", b"data: {nope\n\n"), ("application/json", b"garbage"), ("", b"")):
            c = AnswerCapture(ct)
            c.feed(junk)
            self.assertEqual(c.result()["usage"], dict.fromkeys(want))

    def test_capture_garbage_never_raises(self):
        for ct in ("text/event-stream", "application/json", ""):
            c = AnswerCapture(ct)
            for junk in (b"\xff\xfe", b"data: {not json\n\n", b'data: {"type":"content_block_delta","delta":7}\n\n',
                         b'data: [1,2]\n\n', b'{"content": 5}'):
                c.feed(junk)
            r = c.result()
            self.assertIsInstance(r["text"], str)

    def test_capture_gzip_split_and_unknown_encoding(self):
        for body, ct in ((SSE, "text/event-stream"), (JSON, "application/json")):
            z, c = gzip.compress(body), AnswerCapture(ct, "gzip")
            for i in range(0, len(z), 7):
                c.feed(z[i:i + 7])
            r = c.result()
            self.assertIsNone(r["error"])
            self.assertEqual(r["text"], "Hello world" if ct.startswith("text") else "Hi")
        c = AnswerCapture("application/json", "br")
        c.feed(b"\x0b\x01")
        self.assertEqual(c.result()["error"], "encoded br")
        c = AnswerCapture("application/json", "gzip")
        c.feed(b"not gzip at all")
        self.assertTrue(c.result()["error"])

    def test_capture_caps(self):
        c = AnswerCapture("application/json")
        c.feed(json.dumps({"content": [{"type": "text", "text": "x" * 60_000},
                                       {"type": "tool_use", "name": "W", "input": {"s": "y" * 5000}}]}).encode())
        r = c.result()
        self.assertEqual(len(r["text"]), 50_000)
        self.assertEqual(len(r["tools"][0]["input"]), 2000)


class RngTest(unittest.TestCase):
    def test_rng_rate(self):
        class S:
            def record_turn(self, row): pass
        d = Doubler(S(), None, random.Random(1))
        cfg = Doubling(0.2, "", SHADOW)
        n = sum(d.turn_for(Features.from_body(msg("turn %d" % i)), "anthropic", cfg) is not None for i in range(1000))
        self.assertTrue(150 <= n <= 250, n)


class DoublingTest(unittest.TestCase):
    def setUp(self):
        nowait = mock.patch.object(claude_router, "RETRY_DELAYS", (0, 0, 0))  # shadow to a dead upstream stays fast
        nowait.start()
        self.addCleanup(nowait.stop)
        self.err = io.StringIO()
        quiet = contextlib.redirect_stderr(self.err)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.a, self.v, self.l = Up(), Up(), Up()
        self.start()

    def doubling(self, **kw):
        r = doubling(**kw)
        return replace(r, downstreams=(replace(r.downstreams[0], url=self.v.url),))

    def start(self, valar_key="VK"):
        self.cfg = Config("127.0.0.1", 0, self.a.url, (self.v.url, valar_key))
        self.app = App(self.cfg, tempfile.mkdtemp())
        self.app.routing = self.doubling()
        self.srv = make_server(self.cfg, self.app)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        for s in (self.srv, self.a, self.v, self.l):
            s.shutdown()
            s.server_close()
        self.app.store.close()
        self.assertNotIn("Traceback", self.err.getvalue())

    def post(self, body, path="/v1/messages?beta=true", headers=HDRS):
        c = HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", path, body, headers)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def pairs(self, n, wait=3):
        t = time.time()
        while True:
            rows = self.app.store._all("SELECT * FROM pairs ORDER BY request_id")
            if len(rows) >= n or time.time() - t > wait:
                break
            time.sleep(0.02)
        for r in rows:
            r["served"], r["shadow"] = json.loads(r["served"]), json.loads(r["shadow"])
        return rows

    def settle(self):  # nothing more should arrive
        time.sleep(0.3)
        return self.pairs(0, 0)

    def test_whole_turn_doubled(self):
        for _ in range(3):
            self.assertEqual(self.post(msg("fix the bug"))[0], 200)
        self.post(msg("another thing"))
        rows = self.pairs(4)
        self.assertEqual(len(rows), 4)
        self.assertEqual(len({r["turn_id"] for r in rows[:3]}), 1)
        self.assertEqual([r["step"] for r in rows[:3]], [1, 2, 3])
        self.assertNotEqual(rows[3]["turn_id"], rows[0]["turn_id"])
        self.assertEqual(rows[3]["step"], 1)
        r = rows[0]
        self.assertEqual((r["served_track"], r["shadow_track"]), ("anthropic", "valar"))
        self.assertEqual(r["served"]["text"], "Hello world")
        self.assertEqual(r["served"]["output_tokens"], 12)
        self.assertEqual(r["served"]["status"], 200)
        self.assertEqual(r["shadow"]["text"], "Hi")
        self.assertEqual(r["shadow"]["status"], 200)
        turns = self.app.store._all("SELECT * FROM turns")
        self.assertEqual(len(turns), 2)
        reqs = {q["id"]: q["turn_id"] for q in self.app.store._all("SELECT id, turn_id FROM requests")}
        self.assertEqual(reqs[r["request_id"]], r["turn_id"])
        self.assertIn("shadow=valar", self.err.getvalue())

    def test_rate_zero_and_tag(self):
        self.app.routing = self.doubling(rate=0)
        self.post(msg("hello"))
        self.assertEqual(self.settle(), [])
        self.assertEqual(len(self.v.seen), 0)
        self.assertIn("shadow=-", self.err.getvalue())
        self.app.routing = self.doubling(tag="Both")
        self.post(msg("plain"))
        self.post(msg("compare this #both"))
        rows = self.pairs(1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(self.settle()), 1)
        self.assertEqual(self.app.store._all("SELECT preview FROM turns")[0]["preview"], "compare this #both")

    def test_direction_checkbox(self):
        self.app.routing = self.doubling(shadow={**SHADOW, "valar": None})
        self.post(msg("do it", "claude-sonnet-5-5"))
        self.assertEqual(self.settle(), [])
        self.assertEqual(len(self.a.seen), 0)
        self.post(msg("do it"))
        rows = self.pairs(1)
        self.assertEqual((rows[0]["served_track"], rows[0]["shadow_track"]), ("anthropic", "valar"))

    def test_served_identical_and_not_delayed(self):
        self.app.routing = self.doubling(rate=0)
        _, plain = self.post(msg("same"))
        self.app.routing = self.doubling()
        self.v.delay = 1
        t = time.time()
        status, doubled = self.post(msg("same again"))
        self.assertLess(time.time() - t, 0.3)
        self.assertEqual((status, doubled), (200, plain))
        rows = self.pairs(1)
        self.assertEqual(rows[0]["shadow"]["text"], "Hi")

    def test_shadow_failure_only_on_pair(self):
        self.v.shutdown()
        self.v.server_close()
        status, data = self.post(msg("go"))
        self.assertEqual((status, data), (200, SSE))
        rows = self.pairs(1)
        self.assertTrue(rows[0]["shadow"]["error"])
        self.assertEqual(rows[0]["served"]["status"], 200)

    def test_busy_valar_shadow_skips_not_queues(self):
        self.v.delay = 1
        for _ in range(3):
            self.post(msg("busy turn"))
        self.assertEqual(self.app.doubler.active()["valar"], 2)
        rows = self.pairs(3)
        self.assertEqual([r["step"] for r in rows], [1, 2, 3])
        self.assertEqual([r["shadow"]["text"] for r in rows[:2]], ["Hi", "Hi"])
        self.assertEqual(rows[2]["shadow"].get("skipped"), "busy")
        self.assertIn("shadow=busy", self.err.getvalue())
        self.assertEqual(len(self.v.seen), 2)

    def test_shadow_per_served_track(self):
        self.post(msg("served anthropic"))
        r = self.pairs(1)[0]
        self.assertEqual((r["served_track"], r["shadow_track"]), ("anthropic", "valar"))
        _, h, req = self.v.seen[0]
        self.assertEqual(h.get_all("Authorization"), ["Bearer VK"])
        self.assertNotIn("oauth-2025-04-20", h.get("anthropic-beta", ""))
        self.assertIs(req["stream"], False)
        self.assertEqual(req["model"], self.app.routing.downstreams[0].models["sonnet"])
        self.assertEqual(self.a.seen[0][1]["Authorization"], HDRS["Authorization"])
        self.assertEqual(self.l.seen, [])

    def test_shadow_to_generic_downstream(self):
        self.app.keys.set("lemmi", "LK")
        r = self.app.routing
        self.app.routing = replace(r, downstreams=r.downstreams + (replace(LEMMI, url=self.l.url),),
                                   doubling=replace(r.doubling, shadow={"anthropic": "lemmi", "valar": None, "lemmi": None}))
        self.post(msg("served anthropic"))
        r = self.pairs(1)[0]
        self.assertEqual((r["served_track"], r["shadow_track"], r["shadow"]["text"]), ("anthropic", "lemmi", "Hi"))
        _, h, req = self.l.seen[0]
        self.assertEqual(h.get_all("Authorization"), ["Bearer LK"])
        self.assertIsNone(h.get("X-Valar-Completion-Window"))
        self.assertEqual((req["model"], req["stream"]), ("deepseek-v4.1-flash", False))
        self.assertEqual(self.v.seen, [])
        self.assertEqual(self.app.doubler.active()["lemmi"], 0)

    def test_shadow_none_means_off(self):
        self.app.routing = self.doubling(shadow={**SHADOW, "valar": None})
        self.post(msg("sonnet turn", "claude-sonnet-5-5"))
        self.assertEqual(self.settle(), [])
        self.assertEqual(self.a.seen, [])

    def test_shadow_to_anthropic_keeps_client_auth(self):
        self.post(msg("served valar", "claude-sonnet-5-5"))
        r = self.pairs(1)[0]
        self.assertEqual((r["served_track"], r["shadow_track"]), ("valar", "anthropic"))
        (_, h, req), = self.a.seen
        self.assertEqual(h["Authorization"], HDRS["Authorization"])
        self.assertIs(req["stream"], False)
        self.assertEqual(req["model"], "claude-sonnet-5-5")
        self.assertEqual(self.v.seen[0][1].get_all("Authorization"), ["Bearer VK"])  # served; the client token never reaches Valar
        self.assertNotIn(HDRS["Authorization"], [x[1]["Authorization"] for x in self.v.seen])

    def test_shadow_to_valar_carries_its_slot_window(self):
        w = {"fable": "asap", "opus": "priority", "sonnet": "standard", "haiku": "asap"}
        self.app.routing = replace(self.app.routing, downstreams=(replace(self.app.routing.downstreams[0], windows=w),))
        self.post(msg("opus turn", "claude-opus-5-5"), headers={**HDRS, "X-Valar-Completion-Window": "flex"})  # served by Anthropic, shadowed on Valar
        self.pairs(1)
        self.assertEqual(self.v.seen[0][1].get_all("X-Valar-Completion-Window"), ["priority"])

    def test_shadow_usage_and_cost_on_pair(self):
        w = {"fable": "asap", "opus": "priority", "sonnet": "standard", "haiku": "asap"}
        self.app.routing = replace(self.app.routing, downstreams=(replace(self.app.routing.downstreams[0], windows=w),))
        self.app.prices.set_valar({"VS": {"priority": [1.0, 0.5, 2.0]}})
        self.post(msg("priced turn", "claude-opus-5-5"))
        r = self.pairs(1)[0]
        self.assertEqual(r["shadow"]["window"], "priority")
        self.assertEqual(r["shadow"]["usage"], {"in": 3, "out": 5, "cache_read": None, "cache_write": None})
        self.assertAlmostEqual(r["shadow"]["cost"], (3 + 5 * 2) / 1e6)
        self.assertAlmostEqual(r["shadow_cost"], r["shadow"]["cost"])

    def test_shadow_valar_not_configured_on_pair(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.start(valar_key="")
        self.post(msg("no key"))
        self.assertEqual(self.pairs(1)[0]["shadow"]["error"], "Valar has no key")
        self.assertEqual(self.v.seen, [])

    def test_gzipped_shadow_and_served_captured(self):
        self.a.encoding = self.v.encoding = "gzip"
        status, data = self.post(msg("zipped"), headers={**HDRS, "Accept-Encoding": "gzip"})
        self.assertEqual((status, data), (200, gzip.compress(SSE)))  # served bytes untouched
        r = self.pairs(1)[0]
        self.assertEqual((r["served"]["text"], r["served"]["output_tokens"], r["served"]["error"]), ("Hello world", 12, None))
        self.assertEqual((r["shadow"]["text"], r["shadow"]["error"]), ("Hi", None))
        served_h, shadow_h = self.a.seen[0][1], self.v.seen[0][1]
        self.assertIsNone(served_h.get("Accept-Encoding"))  # upstreams answer uncompressed now
        self.assertIsNone(shadow_h.get("Accept-Encoding"))

    def test_shadow_to_anthropic_has_no_accept_encoding(self):
        self.post(msg("served valar", "claude-sonnet-5-5"), headers={**HDRS, "Accept-Encoding": "gzip, br"})
        self.pairs(1)
        shadow = [s for s in self.a.seen if s[2]["stream"] is False][0]
        self.assertIsNone(shadow[1].get("Accept-Encoding"))

    def test_unknown_encoding_is_an_error_not_blank(self):
        self.v.encoding = "br"
        self.post(msg("brotli"))
        self.assertEqual(self.pairs(1)[0]["shadow"]["error"], "encoded br")

    def test_doubler_fault_still_serves(self):
        with mock.patch.object(self.app.doubler, "turn_for", side_effect=RuntimeError("boom")):
            self.assertEqual(self.post(msg("x")), (200, SSE))
        self.assertIn("doubling failed: RuntimeError", self.err.getvalue())
        t = time.time()  # the log line comes just after the client has the response
        while "shadow=-" not in self.err.getvalue() and time.time() - t < 2:
            time.sleep(0.01)
        self.assertIn("shadow=-", self.err.getvalue())

    def test_thread_start_failure_releases_cap(self):
        class NoThread(threading.Thread):
            def start(self):
                raise RuntimeError("can't start new thread")
        with mock.patch("doubling.threading", mock.Mock(wraps=threading, Thread=NoThread, Lock=threading.Lock)):
            self.assertEqual(self.post(msg("no threads")), (200, SSE))
        r = self.pairs(1)[0]
        self.assertEqual(r["shadow"]["skipped"], "error")
        self.assertEqual(self.app.doubler.active(), {"valar": 0})
        self.post(msg("no threads"))  # the slot came back: next step really goes out
        self.assertEqual(self.pairs(2)[1]["shadow"]["text"], "Hi")

    def test_count_tokens_never_doubled(self):
        self.post(msg("count me", stream=False), path="/v1/messages/count_tokens")
        self.assertEqual(self.settle(), [])
        self.assertEqual(len(self.v.seen), 0)


if __name__ == "__main__":
    unittest.main()
