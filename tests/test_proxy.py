import contextlib
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from unittest import mock
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import claude_router
from claude_router import App, Config, make_server, upstream_headers
from classifier import Classifier
from routing import DEFAULT_CONFIG

MODELS = {"valar": {"fable": "VS", "opus": "VS", "sonnet": "VS", "haiku": "VH"},
          "local": {"fable": "S", "opus": "S", "sonnet": "S", "haiku": "H"}}
BASE = replace(DEFAULT_CONFIG, models=MODELS)
CLASSIFIER_OFF = replace(BASE, rules=tuple(replace(r, enabled=r.kind != "classifier") for r in BASE.rules))


class Fake(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), FakeHandler)
        self.seen = []
        self.error = False
        self.die = False
        self.hang = False
        self.models = b"{}"
        threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.server_address[1]


class FakeHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.endswith("/v1/models"):
            self.server.seen.append((self.command, self.path, self.headers, b""))
            self.send_response(200)
            self.send_header("content-length", str(len(self.server.models)))
            self.end_headers()
            return self.wfile.write(self.server.models)
        self.do_POST()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        self.server.seen.append((self.command, self.path, self.headers, body))
        if self.server.hang:
            time.sleep(0.6)
        if self.server.error:
            out = b'{"type":"error","error":{"type":"overloaded_error"}}'
            self.send_response(529)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if b'"stream": true' not in body[:200]:
            self.send_response(200)
            self.send_header("content-length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        if self.server.die:  # promise more than we send, then drop the connection
            self.send_header("content-length", "1000")
        self.end_headers()
        try:
            if self.server.die:
                self.wfile.write(b"data: 0\n\n")
                return
            if b"USAGE" in body:
                self.wfile.write(b'data: {"type":"message_start","message":{"usage":{"input_tokens":10,"output_tokens":1,'
                                 b'"cache_read_input_tokens":5,"cache_creation_input_tokens":2}}}\n\n'
                                 b'data: {"type":"message_delta","delta":{},"usage":{"output_tokens":7}}\n\n')
                return
            for i in range(3):
                self.wfile.write(b"data: %d\n\n" % i)
                self.wfile.flush()
                time.sleep(0.3)
        except OSError:
            pass


def body_for(model, extra=None, stream=False):
    d = {"stream": stream, "model": model, "messages": [{"role": "user", "content": "hi"}]}
    d.update(extra or {})
    return json.dumps(d).encode()


HDRS = {"Authorization": "Bearer sk-ant-oat01-X", "anthropic-beta": "claude-code-20250219,oauth-2025-04-20"}


class ProxyTest(unittest.TestCase):
    def setUp(self):
        self.err = io.StringIO()
        quiet = contextlib.redirect_stderr(self.err)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.a, self.v, self.l = Fake(), Fake(), Fake()
        self.start(self.l.url)

    def start(self, litellm_url, classifier_url=None, valar_key="VK"):
        self.dir = tempfile.mkdtemp()
        cfg = Config("127.0.0.1", 0, self.a.url, litellm_url, "LK", self.v.url, valar_key)
        self.app = App(cfg, self.dir)
        self.app.routing = CLASSIFIER_OFF
        if classifier_url:
            self.app.classifier = Classifier({"valar": (classifier_url, "VK"), "local": (classifier_url, "LK")})
        self.srv = make_server(cfg, self.app)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        for s in (self.srv, self.a, self.v, self.l):
            s.shutdown()
            s.server_close()
        self.app.store.close()
        self.assertNotIn("Traceback", self.err.getvalue())

    def logged(self, n=1):  # the handler logs and records just after the client has the response
        t = time.time()
        while self.err.getvalue().count("route=") < n and time.time() - t < 2:
            time.sleep(0.01)

    def post(self, body, headers=HDRS, path="/v1/messages?beta=true"):
        c = HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", path, body, headers)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def test_anthropic_passthrough_untouched(self):
        body = body_for("claude-opus-5-5")
        self.assertEqual(self.post(body)[0], 200)
        _, path, h, got = self.a.seen[0]
        self.assertEqual(path, "/v1/messages?beta=true")
        self.assertEqual(got, body)
        self.assertEqual(h["Authorization"], HDRS["Authorization"])
        self.assertEqual(h["anthropic-beta"], HDRS["anthropic-beta"])
        self.assertEqual(self.l.seen, [])

    def test_valar_swaps_credentials_and_model(self):
        self.post(body_for("claude-sonnet-5-5", {"max_tokens": 7}), {**HDRS, "x-api-key": "client"})
        _, path, h, got = self.v.seen[0]
        self.assertEqual(path, "/v1/messages?beta=true")
        self.assertEqual(h["Authorization"], "Bearer VK")
        self.assertIsNone(h["x-api-key"])
        self.assertEqual(h["anthropic-beta"], "claude-code-20250219")
        j = json.loads(got)
        self.assertEqual(j["model"], "VS")
        self.assertEqual(j["max_tokens"], 7)
        self.assertEqual(self.a.seen + self.l.seen, [])
        self.post(body_for("claude-haiku-4-5"))
        self.assertEqual(json.loads(self.v.seen[1][3])["model"], "VH")

    def test_valar_window_header_and_row(self):
        w = {"valar": {"fable": "asap", "opus": "asap", "sonnet": "standard", "haiku": "asap"}}
        self.app.routing = replace(CLASSIFIER_OFF, windows=w)
        self.post(body_for("claude-sonnet-5-5"), {**HDRS, "X-Valar-Completion-Window": "flex"})
        self.assertEqual(self.v.seen[0][2].get_all("X-Valar-Completion-Window"), ["standard"])
        self.post(body_for("claude-haiku-4-5"))
        self.assertEqual(self.v.seen[1][2]["X-Valar-Completion-Window"], "asap")
        self.app.routing = replace(self.app.routing, lever="anthropic")
        self.post(body_for("claude-sonnet-5-5"))
        self.app.routing = replace(self.app.routing, lever="local")
        self.post(body_for("claude-sonnet-5-5"))
        self.assertIsNone(self.a.seen[0][2]["X-Valar-Completion-Window"])
        self.assertIsNone(self.l.seen[0][2]["X-Valar-Completion-Window"])
        self.logged(4)
        self.app.store.close()
        rows = sorted(self.app.store.departures(), key=lambda r: r["id"])
        self.assertEqual([r["window"] for r in rows], ["standard", "asap", None, None])

    def test_valar_streams(self):
        c = HTTPConnection("127.0.0.1", self.port, timeout=5)
        t = time.time()
        c.request("POST", "/v1/messages", body_for("claude-sonnet-5-5", stream=True), HDRS)
        r = c.getresponse()
        self.assertEqual(r.fp.readline(), b"data: 0\n")
        self.assertLess(time.time() - t, 0.25)
        c.close()

    def test_non_claude_model_untouched(self):
        body = body_for("zai-org/GLM-5.3")
        self.post(body, {**HDRS, "x-api-key": "client"})
        self.assertEqual(json.loads(self.v.seen[0][3])["model"], "zai-org/GLM-5.3")

    def test_valar_gets_inline_tools_hoisted(self):
        # Claude Code's inline-tools beta: Valar 400s on any tool_addition block
        t = {"name": "late", "input_schema": {"type": "object"}}
        add = {"type": "tool_addition", "tool": {"type": "tool_definition", "definition": t}}
        msgs = [{"role": "user", "content": "hi"},
                {"role": "system", "content": [{"type": "text", "text": "ctx"}, add]},
                {"role": "system", "content": [add]}]
        body = body_for("zai-org/GLM-5.3", {"messages": msgs, "tools": [{"name": "Bash"}]})
        self.post(body)
        j = json.loads(self.v.seen[0][3])
        self.assertNotIn(b"tool_addition", self.v.seen[0][3])
        self.assertEqual(j["messages"], [{"role": "user", "content": "hi"},
                                         {"role": "system", "content": [{"type": "text", "text": "ctx"}]}])
        self.assertEqual(j["tools"], [{"name": "Bash"}, t])
        self.app.set_routing(replace(self.app.routing, lever="anthropic"))
        self.post(body)
        self.assertEqual(self.a.seen[0][3], body)  # never on the Anthropic route

    def test_valar_not_configured_502(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.start(self.l.url, valar_key="")
        status, data = self.post(body_for("claude-sonnet-5-5"))
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(data)["error"]["message"], "claude-router: Valar not configured (VALAR_API_KEY unset)")
        self.assertEqual(self.v.seen + self.a.seen + self.l.seen, [])

    def test_valar_down_502(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.v.server_address = ("127.0.0.1", s.getsockname()[1])  # url of a closed port
        self.srv.shutdown()
        self.srv.server_close()
        self.start(self.l.url)
        status, data = self.post(body_for("claude-sonnet-5-5"))
        self.assertEqual(status, 502)
        self.assertIn("Valar unreachable", json.loads(data)["error"]["message"])
        self.assertEqual(self.a.seen, [])

    def test_local_still_works(self):
        self.post(body_for("claude-sonnet-5-5", {"messages": [{"role": "user", "content": "go #local"}]}), {**HDRS, "x-api-key": "client"})
        _, path, h, got = self.l.seen[0]
        self.assertEqual(h["Authorization"], "Bearer LK")
        self.assertIsNone(h["x-api-key"])
        self.assertEqual(json.loads(got)["model"], "S")
        self.assertEqual(self.v.seen + self.a.seen, [])

    def test_config_change_applies_next_request(self):
        self.post(body_for("claude-sonnet-5-5"))
        self.assertEqual((len(self.v.seen), len(self.a.seen)), (1, 0))
        self.app.set_routing(replace(self.app.routing, lever="anthropic"))
        self.post(body_for("claude-sonnet-5-5"))
        self.assertEqual((len(self.v.seen), len(self.a.seen)), (1, 1))
        with open(os.path.join(self.dir, "console.json")) as f:
            self.assertEqual(json.load(f)["lever"], "anthropic")

    def test_request_recorded(self):
        long = "x" * 300
        self.post(body_for("claude-sonnet-5-5", {"messages": [{"role": "user", "content": long}]}))
        self.post(body_for("claude-sonnet-5-5"), path="/v1/messages/count_tokens")
        self.post(b"{}", path="/api/hello")
        self.logged(3)
        self.app.store.close()
        rows = self.app.store.departures()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["track"], r["reason"], r["status"], r["preview"]),
                         ("valar", "rule:Model the request asked for:2", 200, long[:120]))
        self.assertIsInstance(r["ttfb_ms"], int)
        self.assertIsInstance(r["ms"], int)
        self.assertEqual(len(self.app.recent), 1)

    def test_classifier_timeout_routes_on(self):
        slow = Fake()
        slow.hang = True
        self.addCleanup(lambda: (slow.shutdown(), slow.server_close()))
        self.srv.shutdown()
        self.srv.server_close()
        self.start(self.l.url, slow.url)
        self.app.routing = replace(BASE, classifier=replace(BASE.classifier, timeout_s=0.2))
        t = time.time()
        self.assertEqual(self.post(body_for("claude-sonnet-5-5"))[0], 200)
        self.assertLess(time.time() - t, 0.5)
        self.assertEqual(len(self.v.seen), 1)
        self.logged()
        self.assertIn("timed out after 0.2 s", json.dumps(self.app.recent[0][1].explanation))

    def test_log_reason_quoted(self):
        self.post(body_for("claude-sonnet-5-5"))
        self.logged()
        self.assertIn('reason="rule:Model the request asked for:2"', self.err.getvalue())
        self.assertIn(" shadow=-", self.err.getvalue())

    def test_streams_incrementally(self):
        c = HTTPConnection("127.0.0.1", self.port, timeout=5)
        t = time.time()
        c.request("POST", "/v1/messages", body_for("claude-opus-5-5", stream=True), HDRS)
        r = c.getresponse()
        self.assertEqual(r.fp.readline(), b"data: 0\n")
        self.assertLess(time.time() - t, 0.25)
        c.close()

    def test_session_and_usage_recorded(self):
        b = body_for("claude-sonnet-5-5", {"messages": [{"role": "user", "content": "USAGE"}]}, stream=True)
        self.post(b, {**HDRS, "X-Claude-Code-Session-Id": "S1"})
        self.post(b)
        self.a.error = self.v.error = self.l.error = True
        s, data = self.post(b, {**HDRS, "X-Claude-Code-Session-Id": "S1"})
        self.assertEqual(s, 529)
        self.assertEqual(data, b'{"type":"error","error":{"type":"overloaded_error"}}')
        self.logged(3)
        self.app.store.close()
        ok = sorted(self.app.store.departures(), key=lambda r: r["id"])
        self.assertEqual([r["session_id"] for r in ok], ["S1", None, "S1"])
        self.assertEqual([ok[0][k] for k in ("in_tok", "out_tok", "cache_read_tok", "cache_write_tok")], [10, 7, 5, 2])
        self.assertEqual(ok[1]["in_tok"], 10)
        self.assertEqual((ok[2]["status"], ok[2]["in_tok"], ok[2]["out_tok"]), (529, None, None))

    def test_cost_recorded_and_repriced(self):
        b = body_for("claude-sonnet-5-5", {"messages": [{"role": "user", "content": "USAGE"}]}, stream=True)
        self.post(b)  # Valar, no prices loaded yet
        self.app.routing = replace(self.app.routing, lever="anthropic")
        self.post(b)
        self.logged(2)
        self.app.store.close()
        v, a = sorted(self.app.store.departures(), key=lambda r: r["id"])
        api = (10 * 2 + 7 * 10 + 5 * 0.2 + 2 * 2.5) / 1e6  # sonnet row
        self.assertIsNone(v["cost"])
        self.assertAlmostEqual(v["api_cost"], api)
        self.assertEqual(a["cost"], 0.0)
        self.assertAlmostEqual(a["api_cost"], api)
        self.app.prices.set_valar({"VS": {"asap": [1.0, 0.5, 2.0]}})
        self.assertEqual(self.app.store.reprice(self.app.prices), 1)
        self.assertAlmostEqual(self.app.store.request(v["id"])["cost"], (10 + 5 * 0.5 + 7 * 2) / 1e6)
        self.assertEqual(self.app.store.reprice(self.app.prices), 0)

    def test_valar_cost_at_record_time(self):
        self.app.prices.set_valar({"VS": {"asap": [1.0, 0.5, 2.0]}})
        self.post(body_for("claude-sonnet-5-5", {"messages": [{"role": "user", "content": "USAGE"}]}, stream=True))
        self.logged()
        self.app.store.close()
        r = self.app.store.departures()[0]
        self.assertAlmostEqual(r["cost"], (10 + 5 * 0.5 + 7 * 2) / 1e6)
        self.assertGreater(r["api_cost"], 0)

    def test_refresh_valar_prices(self):
        self.v.models = json.dumps({"data": [{"id": "VS", "pricing": [
            {"tier": "realtime", "input_per_1m_tokens": 1, "cached_input_per_1m_tokens": 0.5, "output_per_1m_tokens": 2}]},
            {"id": "valar-auto-balanced", "pricing": []}]}).encode()
        self.post(body_for("claude-sonnet-5-5", {"messages": [{"role": "user", "content": "USAGE"}]}, stream=True))
        self.logged()
        self.app.store.close()
        self.app.refresh_valar_prices()
        self.assertEqual(self.v.seen[-1][:2], ("GET", "/v1/models"))
        self.assertAlmostEqual(self.app.store.departures()[0]["cost"], (10 + 5 * 0.5 + 7 * 2) / 1e6)
        with open(os.path.join(self.dir, "models.json")) as f:
            saved = json.load(f)
        self.assertEqual(saved["valar_prices"], {"VS": {"asap": [1, 0.5, 2]}})
        self.v.models = json.dumps({"data": [{"id": "VS", "pricing": []}]}).encode()
        self.app.refresh_valar_prices()  # nothing priced: keep what we have
        self.assertEqual(self.app.prices.valar, {"VS": {"asap": [1, 0.5, 2]}})
        with open(os.path.join(self.dir, "models.json")) as f:
            self.assertEqual(json.load(f)["valar_prices"], {"VS": {"asap": [1, 0.5, 2]}})
        self.app.cfg = replace(self.app.cfg, valar_url="http://127.0.0.1:1")
        self.app.refresh_valar_prices()  # failure is logged, never raised
        self.assertIn("valar prices", self.err.getvalue())

    def test_upstream_error_passthrough(self):
        self.a.error = True
        s, data = self.post(body_for("claude-opus-5-5"))
        self.assertEqual(s, 529)
        self.assertEqual(data, b'{"type":"error","error":{"type":"overloaded_error"}}')

    def test_litellm_down_502(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead = "http://127.0.0.1:%d" % s.getsockname()[1]
        self.srv.shutdown()
        self.srv.server_close()
        self.start(dead)
        status, data = self.post(body_for("claude-sonnet-5-5", {"messages": [{"role": "user", "content": "#local"}]}))
        self.assertEqual(status, 502)
        j = json.loads(data)
        self.assertEqual(j["type"], "error")
        self.assertEqual(j["error"]["type"], "api_error")
        self.assertIn("LiteLLM unreachable", j["error"]["message"])

    def test_client_disconnect_mid_stream(self):
        body = body_for("claude-opus-5-5", stream=True)
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
        self.assertTrue(s.recv(4096))
        t = time.time()
        s.close()
        # handler must notice the dead client and finish before the 0.9 s stream ends
        while "stream=True" not in self.err.getvalue() and time.time() - t < 2:
            time.sleep(0.01)
        self.assertIn("status=200", self.err.getvalue())
        self.assertLess(time.time() - t, 0.8)
        self.assertEqual(self.post(body_for("claude-opus-5-5"))[0], 200)

    def test_midstream_upstream_death_is_an_error(self):
        self.a.die = True
        c = HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/v1/messages", body_for("claude-opus-5-5", stream=True), HDRS)
        r = c.getresponse()
        data = b""
        while chunk := r.read1(4096):  # read1: the forwarded content-length is deliberately short
            data += chunk
        c.close()
        self.assertEqual(r.status, 200)
        self.assertTrue(data.startswith(b"data: 0\n\n"))
        self.assertIn(b"event: error\ndata: ", data)
        self.assertIn(b"upstream ended mid-stream", data)
        self.assertIn("err=midstream", self.err.getvalue())

    def test_rejects_browser_origin(self):
        s, data = self.post(body_for("claude-sonnet-5-5"), {**HDRS, "Origin": "http://evil.example"})
        self.assertEqual(s, 403)
        self.assertEqual(json.loads(data)["type"], "error")
        self.assertEqual(self.l.seen + self.a.seen + self.v.seen, [])

    def test_rejects_foreign_host(self):
        for host in ("evil.example", "evil.example:80", "127.0.0.1.evil.example"):
            self.assertEqual(self.post(body_for("claude-sonnet-5-5"), {**HDRS, "Host": host})[0], 403, host)
        self.assertEqual(self.l.seen + self.a.seen + self.v.seen, [])
        for host in ("localhost:8787", "[::1]:8787", "127.0.0.1"):
            self.assertEqual(self.post(body_for("claude-opus-5-5"), {**HDRS, "Host": host})[0], 200, host)

    def test_upstream_timeout_says_timed_out(self):
        self.a.hang = True
        with mock.patch.object(claude_router, "UPSTREAM_TIMEOUT", 0.2):
            s, data = self.post(body_for("claude-opus-5-5"))
        self.assertEqual(s, 502)
        self.assertIn("timed out", json.loads(data)["error"]["message"])

    def test_large_body(self):
        body = body_for("claude-opus-5-5", {"pad": "x" * 5_000_000})
        self.post(body)
        self.assertEqual(self.a.seen[0][3], body)

    def test_concurrent_streams(self):
        out = []

        def run():
            c = HTTPConnection("127.0.0.1", self.port, timeout=5)
            c.request("POST", "/v1/messages", body_for("claude-opus-5-5", stream=True), HDRS)
            out.append(c.getresponse().read())
            c.close()

        t0 = time.time()
        ts = [threading.Thread(target=run) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertLess(time.time() - t0, 2)
        self.assertEqual(len(out), 2)
        for o in out:
            self.assertEqual(o.count(b"data:"), 3)


    def test_routing_failure_is_a_500_not_a_dropped_connection(self):
        with mock.patch.object(claude_router, "decide_route", side_effect=RuntimeError("boom")):
            s, data = self.post(body_for("claude-sonnet-5-5"))
        self.assertEqual(s, 500)
        self.assertEqual(json.loads(data)["error"]["message"], "claude-router: routing failed")
        self.assertEqual(self.l.seen + self.a.seen + self.v.seen, [])
        self.logged()
        self.assertRegex(self.err.getvalue(), r"route=- reason=error status=500 .*shadow=-")
        self.assertNotIn("Traceback", self.err.getvalue())

    def test_forbidden_log_line_has_shadow(self):
        self.post(body_for("claude-sonnet-5-5"), {**HDRS, "Origin": "http://evil.example"})
        self.logged()
        self.assertIn("reason=forbidden status=403 shadow=-", self.err.getvalue())

    def test_favicon_never_reaches_upstream(self):
        c = HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/favicon.ico")
        r = c.getresponse()
        r.read()
        c.close()
        self.assertEqual(r.status, 404)
        self.assertEqual(self.l.seen + self.a.seen + self.v.seen, [])

    def test_broken_stats_db_keeps_routing(self):
        for make in (lambda p: os.mkdir(p), lambda p: open(p, "wb").write(b"not a database" * 100)):
            self.srv.shutdown()
            self.srv.server_close()
            self.app.store.close()
            self.dir = tempfile.mkdtemp()
            make(os.path.join(self.dir, "stats.db"))
            cfg = Config("127.0.0.1", 0, self.a.url, self.l.url, "LK", self.v.url, "VK")
            self.app = App(cfg, self.dir)
            self.assertTrue(self.app.banner.startswith("Stats are off: "), self.app.banner)
            self.srv = make_server(cfg, self.app)
            threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
            self.port = self.srv.server_address[1]
            self.assertEqual(self.post(body_for("claude-opus-5-5"))[0], 200)
            self.assertEqual(self.app.store.departures(), [])


class MainTest(unittest.TestCase):
    def test_config_dir_tilde_expanded(self):
        with mock.patch.dict(os.environ, {"CLAUDE_ROUTER_CONFIG_DIR": "~/x-router"}):
            self.assertEqual(claude_router.default_config_dir(), os.path.expanduser("~/x-router"))

    def test_unusable_config_dir_exits_2(self):
        f = tempfile.NamedTemporaryFile()
        err = io.StringIO()
        env = {"LITELLM_API_KEY": "k", "CLAUDE_ROUTER_CONFIG_DIR": os.path.join(f.name, "sub")}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            claude_router.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(err.getvalue().count("\n"), 1)
        self.assertIn("claude-router: ", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


class UpstreamHeadersTest(unittest.TestCase):
    def test_upstream_headers(self):
        h = dict(upstream_headers([
            ("Authorization", "x"), ("X-Api-Key", "y"), ("Host", "h"), ("Content-Length", "1"),
            ("Connection", "c"), ("Transfer-Encoding", "t"), ("anthropic-version", "v"),
            ("anthropic-beta", "a, oauth-2025-04-20,b"),
        ], "K"))
        self.assertEqual(h, {"anthropic-version": "v", "anthropic-beta": "a,b", "Authorization": "Bearer K"})
        h = dict(upstream_headers([("Anthropic-Beta", "oauth-2025-04-20")], "K"))
        self.assertEqual(h, {"Authorization": "Bearer K"})


if __name__ == "__main__":
    unittest.main()
