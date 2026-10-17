import contextlib
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

from claude_router import App, Config, make_server
from doubling import AnswerCapture, Doubler
from routing import Doubling, Features
from tests.test_proxy import CLASSIFIER_OFF, HDRS


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
    """Fake Anthropic / LiteLLM: SSE when stream is true, JSON otherwise; delay applies to stream:false calls."""
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), UpHandler)
        self.seen, self.delay = [], 0
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
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def msg(text, model="claude-opus-5-5", stream=True):
    return json.dumps({"model": model, "stream": stream, "messages": [{"role": "user", "content": text}]}).encode()


def doubling(**kw):
    return replace(CLASSIFIER_OFF, doubling=replace(Doubling(1, "", True, True), **kw))


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
        self.assertEqual(c.result(), {"text": "Hi", "tools": [{"name": "Bash", "input": '{"command": "ls"}'}],
                                      "output_tokens": 5})

    def test_capture_garbage_never_raises(self):
        for ct in ("text/event-stream", "application/json", ""):
            c = AnswerCapture(ct)
            for junk in (b"\xff\xfe", b"data: {not json\n\n", b'data: {"type":"content_block_delta","delta":7}\n\n',
                         b'data: [1,2]\n\n', b'{"content": 5}'):
                c.feed(junk)
            r = c.result()
            self.assertIsInstance(r["text"], str)

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
        cfg = Doubling(0.2, "", True, True)
        n = sum(d.turn_for(Features.from_body(msg("turn %d" % i)), "anthropic", cfg) is not None for i in range(1000))
        self.assertTrue(150 <= n <= 250, n)


class DoublingTest(unittest.TestCase):
    def setUp(self):
        self.err = io.StringIO()
        quiet = contextlib.redirect_stderr(self.err)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.a, self.l = Up(), Up()
        self.start(self.l.url)

    def start(self, litellm_url):
        self.cfg = Config("127.0.0.1", 0, self.a.url, litellm_url, "LK", "S", "H")
        self.app = App(self.cfg, tempfile.mkdtemp())
        self.app.routing = doubling()
        self.srv = make_server(self.cfg, self.app)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        for s in (self.srv, self.a, self.l):
            s.shutdown()
            s.server_close()
        self.app.store.close()
        self.assertNotIn("Traceback", self.err.getvalue())

    def post(self, body, path="/v1/messages?beta=true"):
        c = HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", path, body, HDRS)
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
        self.assertEqual((r["served_track"], r["shadow_track"]), ("anthropic", "local"))
        self.assertEqual(r["served"]["text"], "Hello world")
        self.assertEqual(r["served"]["output_tokens"], 12)
        self.assertEqual(r["served"]["status"], 200)
        self.assertEqual(r["shadow"]["text"], "Hi")
        self.assertEqual(r["shadow"]["status"], 200)
        turns = self.app.store._all("SELECT * FROM turns")
        self.assertEqual(len(turns), 2)
        reqs = {q["id"]: q["turn_id"] for q in self.app.store._all("SELECT id, turn_id FROM requests")}
        self.assertEqual(reqs[r["request_id"]], r["turn_id"])
        self.assertIn("shadow=local", self.err.getvalue())

    def test_rate_zero_and_tag(self):
        self.app.routing = doubling(rate=0)
        self.post(msg("hello"))
        self.assertEqual(self.settle(), [])
        self.assertEqual(len(self.l.seen), 0)
        self.assertIn("shadow=-", self.err.getvalue())
        self.app.routing = doubling(tag="Both")
        self.post(msg("plain"))
        self.post(msg("compare this #both"))
        rows = self.pairs(1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(self.settle()), 1)
        self.assertEqual(self.app.store._all("SELECT preview FROM turns")[0]["preview"], "compare this #both")

    def test_direction_checkbox(self):
        self.app.routing = doubling(when_local=False)
        self.post(msg("do it #local"))
        self.assertEqual(self.settle(), [])
        self.assertEqual(len(self.a.seen), 0)
        self.post(msg("do it"))
        rows = self.pairs(1)
        self.assertEqual((rows[0]["served_track"], rows[0]["shadow_track"]), ("anthropic", "local"))

    def test_served_identical_and_not_delayed(self):
        self.app.routing = doubling(rate=0)
        _, plain = self.post(msg("same"))
        self.app.routing = doubling()
        self.l.delay = 1
        t = time.time()
        status, doubled = self.post(msg("same again"))
        self.assertLess(time.time() - t, 0.3)
        self.assertEqual((status, doubled), (200, plain))
        rows = self.pairs(1)
        self.assertEqual(rows[0]["shadow"]["text"], "Hi")

    def test_shadow_failure_only_on_pair(self):
        self.l.shutdown()
        self.l.server_close()
        status, data = self.post(msg("go"))
        self.assertEqual((status, data), (200, SSE))
        rows = self.pairs(1)
        self.assertTrue(rows[0]["shadow"]["error"])
        self.assertEqual(rows[0]["served"]["status"], 200)

    def test_busy_local_shadow_skips_not_queues(self):
        self.l.delay = 1
        for _ in range(3):
            self.post(msg("busy turn"))
        self.assertEqual(self.app.doubler.active()["local"], 1)
        rows = self.pairs(3)
        self.assertEqual([r["step"] for r in rows], [1, 2, 3])
        self.assertEqual(rows[0]["shadow"]["text"], "Hi")
        self.assertTrue(all(r["shadow"].get("skipped") == "busy" for r in rows[1:]))
        self.assertIn("shadow=busy", self.err.getvalue())
        self.assertEqual(len(self.l.seen), 1)

    def test_shadow_to_anthropic_keeps_client_auth_local_gets_key(self):
        self.post(msg("served local #local"))
        self.post(msg("served anthropic"))
        self.pairs(2)
        shadows_a = [s for s in self.a.seen if s[2]["stream"] is False]
        shadows_l = [s for s in self.l.seen if s[2]["stream"] is False]
        self.assertEqual(len(shadows_a), 1)
        self.assertEqual(len(shadows_l), 1)
        self.assertEqual(shadows_a[0][1]["Authorization"], HDRS["Authorization"])
        self.assertEqual(shadows_a[0][2]["model"], "claude-opus-5-5")
        h = shadows_l[0][1]
        self.assertEqual(h["Authorization"], "Bearer LK")
        self.assertEqual(h.get_all("Authorization"), ["Bearer LK"])
        self.assertNotIn("oauth-2025-04-20", h.get("anthropic-beta", ""))
        self.assertEqual(shadows_l[0][2]["model"], "S")

    def test_count_tokens_never_doubled(self):
        self.post(msg("count me", stream=False), path="/v1/messages/count_tokens")
        self.assertEqual(self.settle(), [])
        self.assertEqual(len(self.l.seen), 0)


if __name__ == "__main__":
    unittest.main()
