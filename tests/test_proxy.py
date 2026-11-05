import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from unittest import mock
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import signaller
import overrides
from signaller import App, Config, make_server, upstream_headers
from routing import DEFAULT_CONFIG, Downstream, Rule

VALAR = replace(DEFAULT_CONFIG.downstreams[0], models={"fable": "VS", "opus": "VS", "sonnet": "VS", "haiku": "VH"})
LEMMI = Downstream("lemmi", "Lemmi", "", "generic", dict.fromkeys(("fable", "opus", "sonnet", "haiku"), "deepseek-v4.1-flash"), "#888888")
BASE = replace(DEFAULT_CONFIG, downstreams=(VALAR,))
CLASSIFIER_OFF = replace(BASE, rules=tuple(replace(r, enabled=r.kind != "classifier") for r in BASE.rules))


class Fake(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), FakeHandler)
        self.seen = []
        self.error = False
        self.die = False
        self.hang = False
        self.no_images = False  # answer a request carrying an image the way a text-only model does
        self.models = b"{}"
        threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def handle_error(self, request, client_address):
        # the router may hang up before the fake finishes writing; that traceback isn't the router's and must not
        # reach the stderr the tests check
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            super().handle_error(request, client_address)

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
        if self.server.error or (self.server.no_images and b'"image"' in body):
            img = not self.server.error
            out = b'{"type":"error","error":{"message":"this model does not support image input"}}' if img \
                else b'{"type":"error","error":{"type":"overloaded_error"}}'
            self.send_response(400 if img else 529)
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
                                 b'"cache_read_input_tokens":5,"cache_creation_input_tokens":2,"cache_creation":{"ephemeral_5m_input_tokens":0,"ephemeral_1h_input_tokens":2}}}}\n\n'
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
        nowait = mock.patch.object(signaller, "RETRY_DELAYS", (0, 0, 0))  # down-upstream tests stay fast
        nowait.start()
        self.addCleanup(nowait.stop)
        self.err = io.StringIO()
        quiet = contextlib.redirect_stderr(self.err)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.a, self.v, self.l = Fake(), Fake(), Fake()
        self.start(self.l.url)

    def start(self, litellm_url, valar_key="VK"):
        self.dir = tempfile.mkdtemp()
        cfg = Config("127.0.0.1", 0, self.a.url, (self.v.url, valar_key))
        self.app = App(cfg, self.dir)
        self.app.routing = replace(CLASSIFIER_OFF, downstreams=(replace(VALAR, url=self.v.url),))
        self.srv = make_server(cfg, self.app)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.port = self.srv.server_address[1]

    def set_valar_url(self, url):
        self.app.routing = replace(self.app.routing, downstreams=(replace(self.app.routing.downstreams[0], url=url),))

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
        self.assertEqual(self.post(body, {**HDRS, "Accept-Encoding": "gzip, br, zstd"})[0], 200)
        _, path, h, got = self.a.seen[0]
        self.assertNotIn("accept-encoding", {k.lower() for k in h})  # identity: the capture reads it, errors can be appended
        self.assertEqual(path, "/v1/messages?beta=true")
        self.assertEqual(got, body)
        self.assertEqual(h["Authorization"], HDRS["Authorization"])
        self.assertEqual(h["anthropic-beta"], HDRS["anthropic-beta"])
        self.assertEqual(self.l.seen, [])

    def test_image_fallback_to_anthropic(self):
        img = {"messages": [{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}},
                                                         {"type": "text", "text": "#execution what is this"}]}]}
        self.v.no_images = True
        h = {**HDRS, "X-Claude-Code-Session-Id": "s1"}
        status, data = self.post(body_for("claude-opus-5-5", img), h)
        self.assertEqual((status, data), (200, b"{}"))  # Claude Code never sees the 400
        self.assertEqual((len(self.v.seen), len(self.a.seen)), (1, 1))
        _, _, ah, got = self.a.seen[0]
        self.assertEqual(ah["Authorization"], HDRS["Authorization"])  # the person's own login, as on any Anthropic request
        self.assertEqual(json.loads(got)["model"], "claude-opus-5-5")  # not the downstream's name
        self.assertEqual(json.loads(got)["messages"][0]["content"][1]["text"], "what is this")  # tags still stripped
        self.assertIn("this turn went to anthropic, not valar", self.app.notices["s1"]["text"].lower())
        self.post(body_for("claude-opus-5-5", img), h)  # nothing remembered: each request tries valar first
        self.assertEqual((len(self.v.seen), len(self.a.seen)), (2, 2))
        self.logged(2)
        self.assertIn("fallback=valar", self.err.getvalue())
        self.v.error = True  # any other downstream error passes through as it came
        self.assertEqual(self.post(body_for("claude-sonnet-5-5", {"messages": [{"role": "user", "content": "#execution hi"}]}), h)[0], 529)

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
        w = {"fable": "asap", "opus": "asap", "sonnet": "standard", "haiku": "asap"}
        self.app.routing = replace(self.app.routing, downstreams=(replace(self.app.routing.downstreams[0], windows=w),))
        self.post(body_for("claude-sonnet-5-5"), {**HDRS, "X-Valar-Completion-Window": "flex"})
        self.assertEqual(self.v.seen[0][2].get_all("X-Valar-Completion-Window"), ["standard"])
        self.post(body_for("claude-haiku-4-5"))
        self.assertEqual(self.v.seen[1][2]["X-Valar-Completion-Window"], "asap")
        self.app.routing = replace(self.app.routing, lever="anthropic")
        self.post(body_for("claude-sonnet-5-5"))
        self.assertIsNone(self.a.seen[0][2]["X-Valar-Completion-Window"])
        self.logged(3)
        self.app.store.close()
        rows = sorted(self.app.store.departures(), key=lambda r: r["id"])
        self.assertEqual([r["window"] for r in rows], ["standard", "asap", None])

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

    def test_generic_downstream_passthrough(self):
        self.app.keys.set("lemmi", self.l.url, "LK")  # after start: keys are read per request
        self.app.routing = replace(self.app.routing, lever="lemmi",
                                   downstreams=self.app.routing.downstreams + (replace(LEMMI, url=self.l.url),))
        t = {"name": "late", "input_schema": {"type": "object"}}
        add = {"type": "tool_addition", "tool": {"type": "tool_definition", "definition": t}}
        msgs = [{"role": "user", "content": "hi"}, {"role": "system", "content": [add]}]
        self.assertEqual(self.post(body_for("claude-opus-5-5", {"messages": msgs}))[0], 200)
        _, _, h, got = self.l.seen[0]
        self.assertEqual(h["Authorization"], "Bearer LK")
        self.assertIsNone(h["X-Valar-Completion-Window"])
        j = json.loads(got)
        self.assertEqual(j["model"], "deepseek-v4.1-flash")
        self.assertEqual(j["messages"], msgs)  # no Valar hoisting on a generic downstream
        self.assertEqual(self.a.seen + self.v.seen, [])
        self.logged()
        self.assertIn("route=lemmi", self.err.getvalue())

    def test_valar_kind_keeps_window_and_hoist(self):  # Valar extras follow kind, not the id "valar"
        w = {"fable": "asap", "opus": "asap", "sonnet": "standard", "haiku": "asap"}
        fast = replace(VALAR, id="fast", name="Fast", url=self.v.url, windows=w)
        self.app.keys.set("fast", self.v.url, "FK")
        self.app.routing = replace(self.app.routing, lever="fast", downstreams=(fast,))
        t = {"name": "late", "input_schema": {"type": "object"}}
        add = {"type": "tool_addition", "tool": {"type": "tool_definition", "definition": t}}
        msgs = [{"role": "user", "content": "hi"}, {"role": "system", "content": [add]}]
        self.post(body_for("claude-sonnet-5-5", {"messages": msgs}))
        _, _, h, got = self.v.seen[0]
        self.assertEqual((h["Authorization"], h.get_all("X-Valar-Completion-Window")), ("Bearer FK", ["standard"]))
        self.assertEqual(json.loads(got)["messages"], [{"role": "user", "content": "hi"}])
        self.assertEqual(json.loads(got)["tools"], [t])

    def test_no_key_502_names_downstream(self):
        self.app.routing = replace(self.app.routing, lever="lemmi",
                                   downstreams=self.app.routing.downstreams + (replace(LEMMI, url=self.l.url),))
        status, data = self.post(body_for("claude-sonnet-5-5"))
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(data)["error"]["message"], "signaller: Lemmi has no key; set it in the console")
        self.assertEqual(self.v.seen + self.a.seen + self.l.seen, [])

    def test_valar_not_configured_502(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.start(self.l.url, valar_key="")
        status, data = self.post(body_for("claude-sonnet-5-5"))
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(data)["error"]["message"], "signaller: Valar has no key; set it in the console")
        self.assertEqual(self.v.seen + self.a.seen + self.l.seen, [])

    def test_bad_content_length_answers_400(self):
        for cl in ("abc", "-5", "1e3"):
            s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            s.sendall(("POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: %s\r\n\r\n" % cl).encode())
            self.assertIn(b" 400 ", s.recv(4096).split(b"\r\n")[0] + b" ", cl)
            s.close()
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

    def test_config_change_applies_next_request(self):
        self.post(body_for("claude-sonnet-5-5"))
        self.assertEqual((len(self.v.seen), len(self.a.seen)), (1, 0))
        self.app.set_routing(replace(self.app.routing, lever="anthropic"))
        self.post(body_for("claude-sonnet-5-5"))
        self.assertEqual((len(self.v.seen), len(self.a.seen)), (1, 1))
        with open(os.path.join(self.dir, "console.json")) as f:
            self.assertEqual(json.load(f)["lever"], "anthropic")

    def test_teammate_is_an_agent_id_without_the_subagent_marker(self):
        rule = Rule("Who asked", "dsl", True, "when teammate go valar\nwhen subagent go anthropic")
        self.app.routing = replace(self.app.routing, rules=(rule,), otherwise="anthropic")
        sub = {"system": [{"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.318; cc_is_subagent=true;"}]}
        agent = {**HDRS, "x-claude-code-agent-id": "a1b2c3"}
        self.post(body_for("claude-opus-5-5"))                # the main thread (the team lead in a team): no agent id
        self.post(body_for("claude-opus-5-5"), agent)         # a teammate: an agent id, no marker
        self.post(body_for("claude-opus-5-5", sub), agent)    # a subagent: both; it is not a teammate
        self.assertEqual((len(self.a.seen), len(self.v.seen)), (2, 1))
        self.logged(3)
        self.assertEqual(self.err.getvalue().count("route=valar"), 1)

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
        self.start(self.l.url)
        self.app.keys.set("cls", slow.url, "CK")
        self.app.routing = replace(BASE, downstreams=(replace(VALAR, url=self.v.url), replace(LEMMI, id="cls", url=slow.url)),
                                   classifier=replace(BASE.classifier, timeout_s=0.2, upstream="cls"))
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
        self.assertEqual(ok[0]["cache_write_1h_tok"], 2)
        self.assertEqual(ok[1]["in_tok"], 10)
        self.assertEqual((ok[2]["status"], ok[2]["in_tok"], ok[2]["out_tok"]), (529, None, None))

    def test_leading_tag_routes_and_is_stripped(self):
        self.post(body_for("claude-opus-5-5", {"messages": [{"role": "user", "content": "#execution fix it"}]}), HDRS)
        self.assertEqual(json.loads(self.v.seen[0][3])["messages"][0]["content"], "fix it")
        self.logged(1)
        self.app.store.close()
        r = self.app.store.departures()[0]
        self.assertEqual((r["preview"], self.app.store.request(r["id"])["prompt"]), ("fix it", "fix it"))
        self.assertEqual(self.a.seen, [])

    def test_tool_step_recorded(self):
        msgs = [{"role": "user", "content": "fix it"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a.py\nb.py"}]}]
        self.post(body_for("claude-sonnet-5-5", {"messages": msgs}), HDRS)
        self.logged(1)
        self.app.store.close()
        r = self.app.store.departures()[0]
        self.assertEqual((r["preview"], r["step"]), ("↳ Bash: a.py · b.py", "Bash: a.py\nb.py"))
        self.assertEqual(self.app.store.request(r["id"])["prompt"], "fix it")

    def test_cost_recorded_and_repriced(self):
        b = body_for("claude-sonnet-5-5", {"messages": [{"role": "user", "content": "USAGE"}]}, stream=True)
        self.post(b)  # Valar, no prices loaded yet
        self.app.routing = replace(self.app.routing, lever="anthropic")
        self.post(b)
        self.logged(2)
        self.app.store.close()
        v, a = sorted(self.app.store.departures(), key=lambda r: r["id"])
        api = (10 * 2 + 7 * 10 + 5 * 0.2 + 2 * 4.0) / 1e6  # sonnet row, the 2 cache writes are 1-hour
        self.assertIsNone(v["cost"])
        self.assertAlmostEqual(v["api_cost"], api)
        self.assertEqual(a["cost"], 0.0)
        self.assertAlmostEqual(a["api_cost"], api)
        self.app.prices.set_listed("valar", {"VS": {"asap": [1.0, 0.5, 2.0]}})
        self.assertEqual(self.app.store.reprice(self.app.prices, self.app.routing.downstreams), 1)
        self.assertAlmostEqual(self.app.store.request(v["id"])["cost"], (10 + 5 * 0.5 + 7 * 2) / 1e6)
        self.assertEqual(self.app.store.reprice(self.app.prices, self.app.routing.downstreams), 0)

    def test_valar_cost_at_record_time(self):
        self.app.prices.set_listed("valar", {"VS": {"asap": [1.0, 0.5, 2.0]}})
        self.post(body_for("claude-sonnet-5-5", {"messages": [{"role": "user", "content": "USAGE"}]}, stream=True))
        self.logged()
        self.app.store.close()
        r = self.app.store.departures()[0]
        self.assertAlmostEqual(r["cost"], (10 + 5 * 0.5 + 7 * 2) / 1e6)
        self.assertGreater(r["api_cost"], 0)

    def test_refresh_prices(self):
        self.v.models = json.dumps({"data": [{"id": "VS", "pricing": [
            {"tier": "realtime", "input_per_1m_tokens": 1, "cached_input_per_1m_tokens": 0.5, "output_per_1m_tokens": 2}]},
            {"id": "valar-auto-balanced", "pricing": []}]}).encode()
        self.post(body_for("claude-sonnet-5-5", {"messages": [{"role": "user", "content": "USAGE"}]}, stream=True))
        self.logged()
        self.app.store.close()
        self.app.refresh_prices()
        self.assertEqual(self.v.seen[-1][:2], ("GET", "/v1/models"))
        self.assertAlmostEqual(self.app.store.departures()[0]["cost"], (10 + 5 * 0.5 + 7 * 2) / 1e6)
        with open(os.path.join(self.dir, "models.json")) as f:
            saved = json.load(f)
        self.assertEqual(saved["listed_prices"]["valar"], {"VS": {"asap": [1, 0.5, 2]}})
        self.v.models = json.dumps({"data": [{"id": "VS", "pricing": []}]}).encode()
        self.app.refresh_prices()  # nothing priced: keep what we have
        self.assertEqual(self.app.prices.listed["valar"], {"VS": {"asap": [1, 0.5, 2]}})
        with open(os.path.join(self.dir, "models.json")) as f:
            self.assertEqual(json.load(f)["listed_prices"]["valar"], {"VS": {"asap": [1, 0.5, 2]}})
        self.set_valar_url("http://127.0.0.1:1")
        self.app.keys.set("valar", "http://127.0.0.1:1", "VK")  # a new host means entering the key again
        self.app.refresh_prices()  # failure is logged, never raised
        self.assertIn("valar prices", self.err.getvalue())

    def test_upstream_error_passthrough(self):
        self.a.error = True
        s, data = self.post(body_for("claude-opus-5-5"))
        self.assertEqual(s, 529)
        self.assertEqual(data, b'{"type":"error","error":{"type":"overloaded_error"}}')

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
        with mock.patch.object(signaller, "UPSTREAM_TIMEOUT", 0.2):
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
        with mock.patch.object(signaller, "decide_route", side_effect=RuntimeError("boom")):
            s, data = self.post(body_for("claude-sonnet-5-5"))
        self.assertEqual(s, 500)
        self.assertEqual(json.loads(data)["error"]["message"], "signaller: routing failed")
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
            cfg = Config("127.0.0.1", 0, self.a.url, (self.v.url, "VK"))
            self.app = App(cfg, self.dir)
            self.assertTrue(self.app.banner.startswith("Stats are off: "), self.app.banner)
            self.srv = make_server(cfg, self.app)
            threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
            self.port = self.srv.server_address[1]
            self.assertEqual(self.post(body_for("claude-opus-5-5"))[0], 200)
            self.assertEqual(self.app.store.departures(), [])

    def set_settings(self, *lines):
        self.app.routing = replace(self.app.routing, settings=overrides.settings_from_json(list(lines), self.app.routing.downstreams))

    def test_window_without_a_price_falls_back_to_asap(self):
        # a model override to a model without the set window: the same rule the Tracks row applies
        self.app.prices.set_listed("valar", {"glm-x": {"asap": [1.0, 0.5, 2.0]}})
        self.set_settings({"when": "", "downstream": "valar", "tier": None, "set": "window", "value": "priority"},
                          {"when": "", "downstream": "valar", "tier": None, "set": "model", "value": "glm-x"})
        self.post(body_for("claude-sonnet-5-5"))
        _, _, h, got = self.v.seen[0]
        self.assertEqual((json.loads(got)["model"], h.get_all("X-Valar-Completion-Window")), ("glm-x", ["asap"]))
        # the model's window list unknown (no prices for it): the window goes out as set
        self.set_settings({"when": "", "downstream": "valar", "tier": None, "set": "window", "value": "priority"},
                          {"when": "", "downstream": "valar", "tier": None, "set": "model", "value": "glm-y"})
        self.post(body_for("claude-sonnet-5-5"))
        _, _, h, got = self.v.seen[1]
        self.assertEqual((json.loads(got)["model"], h.get_all("X-Valar-Completion-Window")), ("glm-y", ["priority"]))

    def test_hour_pinned_per_turn(self):
        # a tool loop crossing an hour boundary keeps the hour its first request ran at
        self.set_settings({"when": "hour < 12", "downstream": "valar", "tier": None, "set": "window", "value": "standard"})
        for hour in (11, 12):
            with mock.patch("time.time", return_value=time.mktime((2026, 1, 1, hour, 30, 0, 0, 0, -1))):
                self.post(body_for("claude-sonnet-5-5"))
        self.assertEqual([self.v.seen[i][2]["X-Valar-Completion-Window"] for i in (0, 1)], ["standard", "standard"])

    def test_settings_line_sets_window_and_model(self):
        self.set_settings({"when": "session is s-one", "downstream": "valar", "tier": None, "set": "window", "value": "priority"},
                          {"when": "", "downstream": "valar", "tier": None, "set": "model", "value": "glm-x"})
        self.post(body_for("claude-sonnet-5-5"), {**HDRS, "X-Claude-Code-Session-Id": "s-one"})
        _, path, h, got = self.v.seen[0]
        self.assertEqual(h.get_all("X-Valar-Completion-Window"), ["priority"])
        self.assertEqual(json.loads(got)["model"], "glm-x")
        self.logged(1)
        self.app.store.close()
        r = self.app.store.departures()[0]
        self.assertEqual(r["window"], "priority")
        self.assertEqual(json.loads(r["override"]), [{"set": "window", "value": "priority", "when": "session is s-one"},
                                                     {"set": "model", "value": "glm-x", "when": ""}])
        self.post(body_for("claude-sonnet-5-5"), {**HDRS, "X-Claude-Code-Session-Id": "s-two"})
        self.assertEqual(self.v.seen[1][2].get_all("X-Valar-Completion-Window"), ["asap"])  # the downstream default

    def test_anthropic_untouched_by_settings(self):
        self.set_settings({"when": "", "downstream": "valar", "tier": None, "set": "model", "value": "glm-x"})
        self.app.routing = replace(self.app.routing, lever="anthropic")
        body = body_for("claude-opus-5-5")
        self.post(body)
        self.assertEqual(self.a.seen[0][3], body)


class SeedTest(unittest.TestCase):
    def test_env_seeds_valar_once(self):
        d = tempfile.mkdtemp()
        cfg = Config("127.0.0.1", 0, "http://a", ("http://v.example", "k1"))
        with contextlib.redirect_stderr(io.StringIO()):
            app = App(cfg, d)
            self.assertEqual(app.keys.get("valar", "http://v.example"), "k1")
            self.assertTrue(app.routing.seeded)
            self.assertEqual(app.routing.downstreams[0].url, "http://v.example")
            self.assertEqual(oct(os.stat(os.path.join(d, "downstreams.keys.json")).st_mode & 0o777), "0o600")
            self.assertNotIn("k1", open(os.path.join(d, "console.json")).read())
            app.store.close()
            app.keys.delete("valar", "http://v.example")
            lemmi = replace(app.routing.downstreams[0], id="lemmi", kind="generic", windows=None)
            # the person replaced Valar with another downstream
            app.set_routing(replace(app.routing, downstreams=(lemmi,), rules=tuple(replace(r, enabled=False) for r in app.routing.rules), lever="anthropic",
                                    classifier=replace(app.routing.classifier, upstream="none", labels=tuple(replace(l, track="anthropic") for l in app.routing.classifier.labels)),
                                    doubling=replace(app.routing.doubling, shadow={"anthropic": None, "lemmi": None})))
            app.store.close()
            app = App(cfg, d)  # same env, restarted
            self.assertEqual([x.id for x in app.routing.downstreams], ["lemmi"])
            self.assertFalse(app.keys.has("valar", "http://v.example"))
            app.store.close()


class MainTest(unittest.TestCase):
    def test_config_dir_tilde_expanded(self):
        with mock.patch.dict(os.environ, {"SIGNALLER_CONFIG_DIR": "~/x-router"}):
            self.assertEqual(signaller.default_config_dir(), os.path.expanduser("~/x-router"))

    def test_unusable_config_dir_exits_2(self):
        f = tempfile.NamedTemporaryFile()
        err = io.StringIO()
        env = {"SIGNALLER_CONFIG_DIR": os.path.join(f.name, "sub")}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            signaller.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(err.getvalue().count("\n"), 1)
        self.assertIn("signaller: ", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


class RetryTest(unittest.TestCase):
    def run_with(self, *outcomes):
        calls = []
        def fake(*a):
            calls.append(a)
            o = outcomes[len(calls) - 1]
            if isinstance(o, BaseException):
                raise o
            return o
        with mock.patch.object(signaller, "_open_upstream", fake), \
                mock.patch.object(signaller, "RETRY_DELAYS", (0, 0, 0)):
            try:
                return signaller.open_upstream("http://v", "POST", "/v1/messages", iter([("h", "v")]), b"{}"), calls
            except Exception as e:
                return e, calls

    def test_connection_failure_retried_then_succeeds(self):
        out, calls = self.run_with(ConnectionRefusedError(), ConnectionResetError(), ("conn", "resp"))
        self.assertEqual(out, ("conn", "resp"))
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[2][3], [("h", "v")])  # headers survive retries even when given as an iterator

    def test_gives_up_after_three_retries(self):
        out, calls = self.run_with(*[ConnectionRefusedError()] * 4)
        self.assertIsInstance(out, ConnectionRefusedError)
        self.assertEqual(len(calls), 4)

    def test_timeout_not_retried(self):
        out, calls = self.run_with(TimeoutError())
        self.assertIsInstance(out, TimeoutError)
        self.assertEqual(len(calls), 1)


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
