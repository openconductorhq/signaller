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

import advisor
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
        self.script = None  # [(status, content_type, body[, delay s])], answered in order
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
        if self.server.script is not None:
            status, ctype, out, *delay = self.server.script.pop(0) if self.server.script else (500, "application/json", b"{}")
            time.sleep(delay[0] if delay else 0)
            self.send_response(status)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            try:
                self.wfile.write(out)
            except OSError:
                pass
            return
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


def sse(*events):
    return b"".join(b"event: %s\ndata: %s\n\n" % (e["type"].encode(), json.dumps(e).encode()) for e in events)


def events(data):
    return [json.loads(ln[5:]) for ln in data.decode().split("\n") if ln.startswith("data:")]


def leg(*blocks, out=5, inp=100, cached=50):
    """A downstream's streamed answer: a str is a text block, a (name, id) pair a tool_use."""
    ev = [{"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "content": [], "model": "VO",
                                                "usage": {"input_tokens": inp, "output_tokens": 1, "cache_read_input_tokens": cached}}}]
    for i, b in enumerate(blocks):
        if isinstance(b, str):
            ev += [{"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}},
                   {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": b}}]
        else:
            ev += [{"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": b[1], "name": b[0], "input": {}}},
                   {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": "{}"}}]
        ev.append({"type": "content_block_stop", "index": i})
    stop = "tool_use" if any(not isinstance(b, str) for b in blocks) else "end_turn"
    ev += [{"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": out}}, {"type": "message_stop"}]
    return 200, "text/event-stream", sse(*ev)


ADVICE = (200, "application/json", json.dumps({"content": [{"type": "text", "text": "do X"}], "usage": {
    "input_tokens": 300, "output_tokens": 20, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}).encode())
ADV_DEF = {"type": "advisor_20260301", "name": "advisor", "model": "claude-opus-5-5", "defer_loading": True}


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

    def test_image_fallback_drops_the_current_turns_downstream_thinking(self):
        # the fallback lands on Anthropic mid-turn, after the turn already ran on the downstream: its own
        # thinking block is unsigned and Anthropic would 400 on it, so it goes from every message
        self.v.no_images = True
        img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}
        think = {"role": "assistant", "content": [{"type": "thinking", "thinking": "t", "signature": "stub"},
                                                  {"type": "tool_use", "id": "x", "name": "Bash", "input": {}}]}
        res = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}, img]}
        msgs = [{"role": "user", "content": [img, {"type": "text", "text": "#execution look at this"}]}, think, res]
        status, _ = self.post(body_for("claude-opus-5-5", {"messages": msgs}), {**HDRS, "X-Claude-Code-Session-Id": "s1"})
        self.assertEqual(status, 200)
        _, _, _, got = self.a.seen[0]
        self.assertEqual([b["type"] for b in json.loads(got)["messages"][1]["content"]], ["tool_use"])

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


    def advised(self, advisor=True):
        ds = replace(self.app.routing.downstreams[0], url=self.v.url, advisor=advisor,
                     models={"fable": "VO", "opus": "VO", "sonnet": "VS", "haiku": "VH"})
        self.app.routing = replace(self.app.routing, lever="valar", downstreams=(ds,))

    def adv_body(self, stream=True):
        return body_for("claude-sonnet-5-5", {"tools": [{"name": "Bash", "input_schema": {"type": "object"}}, ADV_DEF]}, stream)

    def rows(self, n):
        self.logged(n)
        self.app.store.close()
        return sorted(self.app.store.departures(), key=lambda r: r["id"])

    def test_advisor_call_runs_on_the_downstream(self):
        self.advised()
        self.v.script = [leg("let me ask", ("advisor", "tu1")), ADVICE, leg("done")]
        status, data = self.post(self.adv_body(), {**HDRS, "X-Claude-Code-Session-Id": "s1"})
        self.assertEqual(status, 200)
        self.assertEqual(len(self.v.seen), 3)
        b1, b2, b3 = (json.loads(x[3]) for x in self.v.seen)
        self.assertEqual(b1["tools"][1], advisor.TOOL)
        self.assertEqual((b2["model"], b2["tool_choice"], b2["stream"]), ("VO", {"type": "none"}, False))
        self.assertEqual(b2["messages"][-1]["content"][-1]["text"], advisor.PROMPT)
        self.assertEqual(self.v.seen[1][1], "/v1/messages")
        self.assertEqual(b3["messages"][-2]["content"][-1], {"type": "tool_use", "id": "tu1", "name": "advisor", "input": {}})
        self.assertEqual(b3["messages"][-1]["content"], [{"type": "tool_result", "tool_use_id": "tu1", "content": "do X"}])
        ev = events(data)
        starts = [e["content_block"] for e in ev if e["type"] == "content_block_start"]
        self.assertEqual([b["type"] for b in starts], ["text", "server_tool_use", "advisor_tool_result", "text"])
        self.assertEqual(starts[2]["content"]["text"], "do X")
        self.assertEqual([e["type"] for e in ev].count("message_start"), 1)
        self.assertIn({"type": "text_delta", "text": "done"}, [e.get("delta") for e in ev])
        served, adv = self.rows(2)
        self.assertEqual((adv["reason"], adv["parent_id"], adv["model_in"], adv["model_out"], adv["track"]),
                         ("advisor", served["id"], "claude-opus-5-5", "VO", "valar"))
        self.assertEqual((adv["in_tok"], adv["out_tok"], adv["status"], adv["session_id"]), (300, 20, 200, "s1"))
        self.assertEqual((served["in_tok"], served["out_tok"], served["cache_read_tok"]), (200, 10, 100))  # both legs
        self.assertIsNone(served["parent_id"])
        self.assertIn("reason=advisor", self.err.getvalue())

    def test_advice_model_and_window_follow_settings_lines(self):
        self.advised()
        w = {"fable": "standard", "opus": "standard", "sonnet": "standard", "haiku": "standard"}
        self.app.routing = replace(self.app.routing, downstreams=(replace(self.app.routing.downstreams[0], windows=w),))
        self.set_settings({"when": "", "downstream": "valar", "tier": "opus", "set": "model", "value": "glm-adv"})
        self.app.prices.set_listed("valar", {"glm-adv": {"asap": [1.0, 0.5, 2.0]}})
        self.v.script = [leg(("advisor", "tu1")), ADVICE, leg("done")]
        self.post(self.adv_body())
        (_, _, h1, b1), (_, _, h2, b2) = self.v.seen[:2]
        self.assertEqual((json.loads(b1)["model"], h1.get_all("X-Valar-Completion-Window")), ("VS", ["standard"]))
        self.assertEqual((json.loads(b2)["model"], h2.get_all("X-Valar-Completion-Window")), ("glm-adv", ["asap"]))
        self.assertEqual(h2["Authorization"], "Bearer VK")

    def test_settings_failure_never_fails_the_request(self):
        """A settings line that can't be resolved: logged once per resolution, the downstream's defaults apply."""
        self.advised()
        self.set_settings({"when": "", "downstream": "valar", "tier": None, "set": "model", "value": "glm-x"})
        self.v.script = [leg(("advisor", "tu1")), ADVICE, leg("done")]
        with mock.patch.object(overrides, "resolve", side_effect=RuntimeError("boom")):
            status, _ = self.post(self.adv_body())
        self.assertEqual(status, 200)
        self.assertEqual([json.loads(x[3])["model"] for x in self.v.seen], ["VS", "VO", "VS"])
        served, _ = self.rows(2)
        self.assertIsNone(served["override"])
        self.assertEqual(self.err.getvalue().count("settings failed: RuntimeError"), 2)  # the answer's and the advice's

    def test_advice_row_has_no_turn_id(self):
        """A doubled turn: the served row keeps its turn_id; the advice row, never doubled, has none (parent_id links it)."""
        self.advised()
        self.app.routing = replace(self.app.routing, doubling=replace(self.app.routing.doubling, rate=1.0))
        self.v.script = [leg(("advisor", "tu1")), ADVICE, leg("done")]
        self.post(self.adv_body(), {**HDRS, "X-Claude-Code-Session-Id": "s1"})
        served, adv = self.rows(2)
        self.assertIsNotNone(served["turn_id"])
        self.assertIsNone(adv["turn_id"])
        self.assertEqual((adv["parent_id"], adv["session_id"], adv["prompt_hash"], adv["preview"]),
                         (served["id"], "s1", served["prompt_hash"], served["preview"]))

    def test_advice_failure_continues_without_advice(self):
        self.advised()
        self.v.script = [leg(("advisor", "tu1")), (529, "application/json", b'{"type":"error"}'), leg("done")]
        _, data = self.post(self.adv_body())
        self.assertEqual(len(self.v.seen), 3)
        res = [e["content_block"] for e in events(data) if e["type"] == "content_block_start"][1]
        self.assertEqual(res["content"], {"type": "advisor_tool_result_error", "error_code": "unavailable"})
        self.assertTrue(json.loads(self.v.seen[2][3])["messages"][-1]["content"][0]["is_error"])
        _, adv = self.rows(2)
        self.assertEqual((adv["reason"], adv["status"]), ("advisor", 529))

    def test_cap_of_three_calls(self):
        self.advised()
        self.v.script = [leg(("advisor", "a1")), ADVICE, leg(("advisor", "a2")), ADVICE,
                         leg(("advisor", "a3")), ADVICE, leg("done")]
        self.post(self.adv_body())
        self.assertEqual(len(self.v.seen), 7)
        self.assertIn("advisor", [t["name"] for t in json.loads(self.v.seen[4][3])["tools"]])
        self.assertNotIn("advisor", [t["name"] for t in json.loads(self.v.seen[6][3])["tools"]])

    def test_advisor_off_strips(self):
        self.advised(advisor=False)
        self.v.script = [leg("hi")]
        self.post(self.adv_body())
        self.assertEqual(len(self.v.seen), 1)
        self.assertEqual([t["name"] for t in json.loads(self.v.seen[0][3])["tools"]], ["Bash"])

    def test_anthropic_request_untouched(self):
        self.advised()
        self.app.routing = replace(self.app.routing, lever="anthropic")
        self.a.script = [leg("hi")]
        body = self.adv_body()
        self.post(body)
        self.assertEqual(self.a.seen[0][3], body)
        self.assertEqual(self.v.seen, [])

    def issued_body(self, sid):
        msgs = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": [
                    {"type": "text", "text": "ask"},
                    {"type": "server_tool_use", "id": sid, "name": "advisor", "input": {}},
                    {"type": "advisor_tool_result", "tool_use_id": sid,
                     "content": {"type": "advisor_result", "text": "do X", "stop_reason": "end_turn"}},
                    {"type": "text", "text": "ok"}]},
                {"role": "user", "content": "next"}]
        return body_for("claude-sonnet-5-5", {"messages": msgs, "tools": [ADV_DEF]}, True)

    def test_router_issued_advice_dropped_on_the_way_to_anthropic(self):
        self.advised()
        self.app.routing = replace(self.app.routing, lever="anthropic")
        self.a.script = [leg("hi"), leg("hi")]
        theirs = self.issued_body("srvtoolu_01abc")
        self.post(theirs)
        self.assertEqual(self.a.seen[0][3], theirs)  # Anthropic's own pair: byte for byte
        ours = self.issued_body(advisor.ISSUED + "tu1")
        self.post(ours)
        want = json.loads(ours)
        want["messages"][1]["content"] = [{"type": "text", "text": "ask"}, {"type": "text", "text": "ok"}]
        self.assertEqual(json.loads(self.a.seen[1][3]), want)

    def test_advisor_scans_skipped_without_advisor_bytes(self):
        self.advised()
        self.app.routing = replace(self.app.routing, lever="anthropic")
        body = body_for("claude-opus-5-5", stream=True)
        with mock.patch.object(advisor, "drop_issued", wraps=advisor.drop_issued) as drop, \
                mock.patch.object(advisor, "strip_advisor", wraps=advisor.strip_advisor) as strip, \
                mock.patch.object(advisor, "definition", wraps=advisor.definition) as defn:
            self.post(body)
            self.advised()
            self.post(body_for("claude-sonnet-5-5", {"tools": [{"name": "Bash", "input_schema": {"type": "object"}}]}, True))
        self.assertEqual(self.a.seen[0][3], body)  # byte for byte
        self.assertEqual(json.loads(self.v.seen[0][3])["tools"], [{"name": "Bash", "input_schema": {"type": "object"}}])
        self.assertEqual((drop.call_count, strip.call_count, defn.call_count), (0, 0, 0))

    def test_advisor_scans_run_on_a_hit(self):
        self.advised()
        self.app.routing = replace(self.app.routing, lever="anthropic")
        self.a.script = [leg("hi")]
        with mock.patch.object(advisor, "drop_issued", wraps=advisor.drop_issued) as drop, \
                mock.patch.object(advisor, "strip_advisor", wraps=advisor.strip_advisor) as strip:
            self.post(self.issued_body(advisor.ISSUED + "tu1"))
            self.advised()
            self.post(self.adv_body(stream=False))
        self.assertEqual((drop.call_count, strip.call_count), (1, 1))
        self.assertNotIn(advisor.ISSUED.encode(), self.a.seen[0][3])
        self.assertEqual([t["name"] for t in json.loads(self.v.seen[0][3])["tools"]], ["Bash"])

    def test_router_issued_advice_converted_for_the_downstream(self):
        self.advised()
        self.v.script = [leg("hi")]
        self.post(self.issued_body(advisor.ISSUED + "tu1"))
        msgs = json.loads(self.v.seen[0][3])["messages"]
        self.assertEqual(msgs[1]["content"][-1], {"type": "tool_use", "id": advisor.ISSUED + "tu1", "name": "advisor", "input": {}})
        self.assertEqual(msgs[2]["content"], [{"type": "tool_result", "tool_use_id": advisor.ISSUED + "tu1", "content": "do X"}])

    def test_served_row_bills_every_leg(self):
        self.advised()
        self.app.prices.set_listed("valar", {"VS": dict.fromkeys(("asap", "priority", "standard"), [1.0, 0.5, 2.0])})
        self.v.script = [leg(("advisor", "tu1"), out=5), ADVICE, leg("done", out=7, inp=200, cached=150)]
        _, data = self.post(self.adv_body())
        ev = events(data)
        self.assertEqual([e["message"]["usage"]["input_tokens"] for e in ev if e["type"] == "message_start"], [100])
        self.assertEqual([e["usage"]["output_tokens"] for e in ev if e["type"] == "message_delta"], [12])
        served, _ = self.rows(2)
        self.assertEqual((served["in_tok"], served["cache_read_tok"], served["out_tok"]), (300, 200, 12))
        self.assertAlmostEqual(served["cost"], (300 * 1.0 + 200 * 0.5 + 12 * 2.0) / 1e6)

    def test_unreadable_advice_is_an_error_row(self):
        self.advised()
        self.v.script = [leg(("advisor", "tu1")), (200, "application/json", b"not json"), leg("done")]
        _, data = self.post(self.adv_body())
        res = [e["content_block"] for e in events(data) if e["type"] == "content_block_start"][1]
        self.assertEqual(res["content"]["type"], "advisor_tool_result_error")
        _, adv = self.rows(2)
        self.assertEqual(adv["status"], 502)
        self.assertEqual(self.app.store.stats(time.time(), ("anthropic", "valar"))["errors_last_hour"], 1)

    def test_both_rows_built_by_request_row(self):
        self.advised()
        self.v.script = [leg(("advisor", "tu1")), ADVICE, leg("done")]
        with mock.patch.object(signaller, "request_row", wraps=signaller.request_row) as rr:
            self.post(self.adv_body())
            served, adv = self.rows(2)
        self.assertEqual(sorted(c.kwargs["reason"] for c in rr.call_args_list), ["advisor", "lever"])
        self.assertEqual((served["model_in"], adv["model_in"]), ("claude-sonnet-5-5", "claude-opus-5-5"))

    def test_advice_row_has_an_api_cost(self):
        self.advised()
        self.v.script = [leg(("advisor", "tu1")), ADVICE, leg("done")]
        self.post(self.adv_body())
        _, adv = self.rows(2)
        want = self.app.prices.api_cost("claude-opus-5-5", {"in": 300, "out": 20, "cache_read": 0, "cache_write": 0})
        self.assertAlmostEqual(adv["api_cost"], want)

    def assert_leg2_failed(self, data, t0):
        """Never fail closed: the advice reached the client, then a truncation error; both rows; no hang."""
        self.assertLess(time.time() - t0, 3)
        self.assertIn(b"advisor_tool_result", data)
        self.assertTrue(data.endswith(signaller.MIDSTREAM_SSE))
        self.assertLess(data.index(b"advisor_tool_result"), data.index(signaller.MIDSTREAM_SSE))
        served, adv = self.rows(2)
        self.assertEqual((served["reason"], adv["reason"], adv["status"], adv["parent_id"]),
                         ("lever", "advisor", 200, served["id"]))
        self.assertIn(" err=leg2 ", self.err.getvalue())

    def test_leg2_answering_non_200(self):
        self.advised()
        self.v.script = [leg("ask", ("advisor", "tu1")), ADVICE, (500, "application/json", b'{"type":"error"}')]
        t0 = time.time()
        _, data = self.post(self.adv_body())
        self.assertEqual(len(self.v.seen), 3)
        self.assert_leg2_failed(data, t0)

    def test_leg2_failing_to_connect(self):
        self.advised()
        self.v.script = [leg("ask", ("advisor", "tu1")), ADVICE]
        real, n = signaller.open_upstream, []

        def flaky(*a):  # leg 1 and the advice connect; leg 2 doesn't
            n.append(1)
            if len(n) == 3:
                raise ConnectionRefusedError("refused")
            return real(*a)
        t0 = time.time()
        with mock.patch.object(signaller, "open_upstream", flaky):
            _, data = self.post(self.adv_body())
        self.assertEqual(len(self.v.seen), 2)
        self.assert_leg2_failed(data, t0)

    def test_pings_while_the_advice_runs(self):
        self.advised()
        self.v.script = [leg("ask", ("advisor", "tu1")), (*ADVICE, 0.3), leg("done")]
        with mock.patch.object(signaller, "PING_EVERY", 0.05):
            _, data = self.post(self.adv_body())
        self.assertIn(b'event: ping\ndata: {"type": "ping"}\n\n', data[:data.index(b"server_tool_use")])
        self.assertEqual(len(self.v.seen), 3)

    def test_pings_while_leg2_connects(self):
        self.advised()
        self.v.script = [leg("ask", ("advisor", "tu1")), ADVICE, (*leg("done"), 0.3)]
        with mock.patch.object(signaller, "PING_EVERY", 0.05):
            _, data = self.post(self.adv_body())
        between = data[data.index(b"advisor_tool_result"):data.index(b'"text": "done"')]
        self.assertIn(signaller.PING_SSE, between)
        self.assertEqual([e["type"] for e in events(data)].count("message_stop"), 1)

    def test_client_gone_while_leg2_connects(self):
        self.advised()
        self.v.script = [leg("ask", ("advisor", "tu1"), out=9), ADVICE, (*leg("done"), 0.5)]
        body = self.adv_body()
        # the backstop off: the failed ping write alone must find the client gone
        with mock.patch.object(signaller, "PING_EVERY", 0.05), \
                mock.patch.object(self.srv.RequestHandlerClass, "_client_gone", return_value=False):
            s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            s.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            got = b""
            while b"advisor_tool_result" not in got:  # the advice is out: leg 2 is connecting
                got += s.recv(4096)
            s.close()
            served, adv = self.rows(2)  # both log lines: a clean end, no raise
        self.assertEqual(len(self.v.seen), 3)
        self.assertEqual((served["out_tok"], adv["status"]), (9, 200))
        self.assertNotIn(" err=", self.err.getvalue())

    def test_ping_bytes(self):
        self.assertEqual(signaller.PING_SSE, b'event: ping\ndata: {"type": "ping"}\n\n')

    def test_client_gone_mid_advice(self):
        self.advised()
        self.v.script = [leg("ask", ("advisor", "tu1")), (*ADVICE, 0.5), leg("done")]
        body = self.adv_body()
        with mock.patch.object(signaller, "PING_EVERY", 0.05):
            s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            s.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            got = b""
            while b"event: ping" not in got:  # the advice is running
                got += s.recv(4096)
            s.close()
            served, adv = self.rows(2)  # both log lines: a clean end
        self.assertEqual(len(self.v.seen), 2)  # no leg 2
        self.assertEqual((adv["reason"], adv["status"]), ("advisor", 200))
        self.assertGreaterEqual(adv["ms"], 500)
        self.assertLess(served["ms"], 400)  # the advice's time is its own row's, not counted twice

    def test_served_row_bills_leg1_when_the_client_leaves_during_the_advice(self):
        """No leg 2: the row still has leg 1's held message_delta (9 output tokens), not message_start's 1."""
        self.advised()
        self.v.script = [leg("ask", ("advisor", "tu1"), out=9), (*ADVICE, 0.5), leg("done")]
        body = self.adv_body()
        with mock.patch.object(signaller, "PING_EVERY", 0.05):
            s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            s.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            got = b""
            while b"event: ping" not in got:
                got += s.recv(4096)
            s.close()
            served, _ = self.rows(2)
        self.assertEqual(len(self.v.seen), 2)
        self.assertEqual((served["out_tok"], served["in_tok"]), (9, 100))

    def test_advised_answer_without_a_call_bills_as_before(self):
        self.advised()
        self.v.script = [leg("hi", out=7)]
        _, data = self.post(self.adv_body())
        served = self.rows(1)[0]
        want = signaller.AnswerCapture("text/event-stream")
        want.feed(data)
        self.assertEqual({k: served[k + "_tok"] for k in ("in", "out", "cache_read", "cache_write", "cache_write_1h")},
                         {k: want.result()["usage"].get(k) for k in ("in", "out", "cache_read", "cache_write", "cache_write_1h")})
        self.assertEqual((served["in_tok"], served["out_tok"], served["cache_read_tok"]), (100, 7, 50))

    def test_served_row_bills_leg1_when_leg2_fails_to_connect(self):
        self.advised()
        self.v.script = [leg("ask", ("advisor", "tu1"), out=9), ADVICE]
        real, n = signaller.open_upstream, []

        def flaky(*a):  # leg 1 and the advice connect; leg 2 doesn't
            n.append(1)
            if len(n) == 3:
                raise ConnectionRefusedError("refused")
            return real(*a)
        with mock.patch.object(signaller, "open_upstream", flaky):
            self.post(self.adv_body())
        served, _ = self.rows(2)
        self.assertEqual((served["out_tok"], served["in_tok"]), (9, 100))

    def test_non_streaming_strips(self):
        self.advised()
        self.post(self.adv_body(stream=False))
        self.assertEqual(len(self.v.seen), 1)
        self.assertEqual([t["name"] for t in json.loads(self.v.seen[0][3])["tools"]], ["Bash"])

    def test_advisor_next_to_another_tool_call(self):
        self.advised()
        self.v.script = [leg("both", ("advisor", "tu1"), ("Bash", "tb")), ADVICE]
        _, data = self.post(self.adv_body())
        self.assertEqual(len(self.v.seen), 2)
        ev = events(data)
        starts = [(e["index"], e["content_block"]) for e in ev if e["type"] == "content_block_start"]
        self.assertEqual([(i, b["type"]) for i, b in starts],
                         [(0, "text"), (1, "server_tool_use"), (2, "advisor_tool_result"), (3, "tool_use")])
        self.assertEqual((starts[2][1]["content"]["text"], starts[3][1]["name"]), ("do X", "Bash"))
        self.assertEqual([e["delta"]["stop_reason"] for e in ev if e["type"] == "message_delta"], ["tool_use"])
        self.assertEqual(ev[-1]["type"], "message_stop")

    def test_advisor_call_with_a_done_sentinel(self):
        """A downstream that ends its SSE with `data: [DONE]` (OpenAI-heritage gateways): one clean spliced stream."""
        self.advised()
        done = b"data: [DONE]\n\n"
        l1, l2 = leg("ask", ("advisor", "tu1")), leg("done")
        self.v.script = [(*l1[:2], l1[2] + done), ADVICE, (*l2[:2], l2[2] + done)]
        _, data = self.post(self.adv_body())
        self.assertEqual(len(self.v.seen), 3)
        self.assertEqual(data.count(done), 1)
        self.assertTrue(data.endswith(done))
        ev = events(data[:-len(done)])
        self.assertEqual([e["type"] for e in ev].count("message_start"), 1)
        self.assertEqual([e["type"] for e in ev].count("message_stop"), 1)
        starts = [(e["index"], e["content_block"]["type"]) for e in ev if e["type"] == "content_block_start"]
        self.assertEqual(starts, [(0, "text"), (1, "server_tool_use"), (2, "advisor_tool_result"), (3, "text")])

    def test_session_lever_survives_restart(self):
        self.app.set_session_lever("s1", "valar")
        self.assertEqual(App(Config("127.0.0.1", 0, self.a.url, (self.v.url, "VK")), self.dir).session_levers, {"s1": "valar"})

    def test_dangling_session_lever_is_ignored(self):
        self.app.session_levers["s1"] = "lemmi"          # a track the active setup doesn't have
        self.post(body_for("claude-opus-5-5"), {**HDRS, "X-Claude-Code-Session-Id": "s1"})
        self.assertEqual(len(self.a.seen), 1)            # followed the setup's own routing, no 502
        self.assertEqual(self.app.session_levers["s1"], "lemmi")  # kept for when a preset has it again

    def test_prune_levers_drops_gone_sessions(self):
        self.app.store.touch_session("s1", time.time())
        self.app.store.touch_session("s2", time.time() - 8 * 86400)
        t = time.time()
        while "s1" not in self.app.store.session_last_seen(["s1", "s2"]) and time.time() - t < 2:
            time.sleep(0.01)  # the writer thread lands the rows
        self.app.set_session_lever("s1", "valar")
        self.app.set_session_lever("s2", "valar")
        self.app.set_session_lever("s3", "valar")  # never sent traffic: no sessions row
        self.app.prune_levers()
        self.assertEqual(self.app.session_levers, {"s1": "valar"})

    def auto_post(self, msgs, model="claude-opus-5-5"):
        self.app.set_session_lever("s1", "auto")
        # main-loop requests carry their tools; a request without them is a side request (a title generation)
        self.post(body_for(model, {"messages": msgs, "tools": [{"name": "Bash", "input_schema": {}}]}),
                  {**HDRS, "X-Claude-Code-Session-Id": "s1"})
        return len(self.a.seen), len(self.v.seen)

    def test_auto_routes_by_phase_once_per_turn(self):
        plan = [U("an idea"), tool("Skill", {"skill": "superpowers:brainstorming"}), RES, U("go")]
        self.assertEqual(self.auto_post(plan), (1, 0))  # planning → Anthropic
        built = plan + [tool("Edit", {"file_path": "/x/app.py"}), RES, U("go")]
        self.assertEqual(self.auto_post(built), (1, 1))  # same text, a later turn: its own decision → downstream
        step = plan + [tool("Edit", {"file_path": "/x/app.py"}), RES]
        self.assertEqual(self.auto_post(step), (2, 1))  # a tool-loop step of the planning turn keeps its decision
        self.assertEqual(self.app.auto_last["s1"]["phase"], "planning")

    def test_auto_last_main_thread_only_and_tags(self):
        self.auto_post([U("an idea"), tool("Skill", {"skill": "superpowers:brainstorming"}), RES, U("go")])
        self.app.set_session_lever("s1", "auto")
        self.post(body_for("claude-opus-5-5", {"messages": [U("do a thing")]}), {**HDRS, "X-Claude-Code-Session-Id": "s1", "x-claude-code-agent-id": "a1"})
        self.assertEqual(self.app.auto_for("s1")["phase"], "planning")  # a teammate's request leaves it alone
        self.auto_post([U("#execution just build it")])
        self.assertEqual(self.app.auto_for("s1"), {"phase": "execution", "why": "tag"})

    def test_anthropic_drops_downstream_thinking_every_lever_and_turn(self):
        think = {"role": "assistant", "content": [{"type": "thinking", "thinking": "t", "signature": "s"},
                                                  {"type": "tool_use", "id": "x", "name": "Skill", "input": {"skill": "superpowers:brainstorming"}}]}
        self.auto_post([U("an idea"), think, RES, U("next")])
        self.assertNotIn(b"thinking", self.a.seen[0][3])  # auto: gone
        self.app.set_session_lever("s1", "rules")
        body = body_for("claude-opus-5-5", {"messages": [U("an idea"), think, RES, U("next")]})
        self.post(body, {**HDRS, "X-Claude-Code-Session-Id": "s1"})
        self.assertNotIn(b"thinking", self.a.seen[1][3])  # rules after a downstream turn: gone too
        clean = body_for("claude-opus-5-5", {"messages": [U("an idea"), RES, U("next")]})
        self.post(clean, {**HDRS, "X-Claude-Code-Session-Id": "s1"})
        self.assertEqual(self.a.seen[2][3], clean)  # nothing of theirs to drop: byte for byte

    def test_auto_text_on_a_tool_result_continues_the_turn(self):
        brain = [U("an idea"), tool("Skill", {"skill": "superpowers:brainstorming"}), RES]
        think = lambda name: {"role": "assistant", "content": [{"type": "thinking", "thinking": "t", "signature": "s"},
                                                               {"type": "tool_use", "id": "x", "name": "Skill", "input": {"skill": name}}]}
        skill_body = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"},
                                                  {"type": "text", "text": "Base directory for this skill: /x"}]}
        self.assertEqual(self.auto_post([U("go")]), (0, 1))  # no signal: the downstream
        self.assertEqual(self.auto_post([U("go"), think("superpowers:brainstorming"), skill_body]), (0, 2))  # same turn, same track
        self.assertEqual(self.auto_post(brain + [U("next")]), (1, 2))  # planning → Anthropic
        self.auto_post(brain + [U("next"), think("superpowers:writing-plans"), skill_body])
        # the current turn's own downstream thinking is dropped (no turn boundary); the tool_use it made stays
        self.assertEqual(json.loads(self.a.seen[1][3])["messages"][4]["content"][0]["type"], "tool_use")
        interrupted = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "no", "is_error": True},
                                                   {"type": "text", "text": "[Request interrupted by user for tool use]"},
                                                   {"type": "text", "text": "do it differently"}]}
        self.auto_post(brain + [U("next"), think("superpowers:writing-plans"), interrupted])
        self.assertEqual(json.loads(self.a.seen[2][3])["messages"][4]["content"][0]["type"], "tool_use")

    def test_auto_decide_reads_history_up_to_the_turns_opening(self):
        # sessionless, so no cache holds the turn: step 2's own Skill call sits after the opening message and
        # must not flip the turn — it takes effect at the next turn, whose history then holds it
        f = signaller.Features.from_body(body_for("claude-opus-5-5",
                                                  {"messages": [U("x"), tool("Skill", {"skill": "superpowers:brainstorming"}), RES]}))
        self.assertEqual(self.app.auto_decide(f, self.app.routing.auto), ("none", "no planning signal"))

    def test_auto_side_request_without_tools_leaves_auto_last(self):
        self.auto_post([U("an idea"), tool("Skill", {"skill": "superpowers:brainstorming"}), RES, U("go")])
        # Claude Code's own main-thread side requests (a title generation: one message, no tools) don't
        # overwrite what the person's last prompt chose; main-loop requests always carry their tools
        self.post(body_for("claude-haiku-5-5", {"messages": [U("write a title for this chat")]}),
                  {**HDRS, "X-Claude-Code-Session-Id": "s1"})
        self.assertEqual(self.app.auto_for("s1")["phase"], "planning")

    def test_auto_count_tokens_and_sessionless_requests(self):
        self.auto_post([U("an idea"), tool("Skill", {"skill": "superpowers:brainstorming"}), RES, U("go")])
        self.post(body_for("claude-opus-5-5", {"messages": [U("fresh")]}), {**HDRS, "X-Claude-Code-Session-Id": "s1"},
                  path="/v1/messages/count_tokens")
        self.assertEqual(self.app.auto_for("s1")["phase"], "planning")  # count_tokens doesn't move it
        n = len(self.app._auto)
        f = signaller.Features.from_body(body_for("claude-opus-5-5", {"messages": [U("fresh")]}))
        own = replace(self.app.routing.auto, planning_skills=("mine",))
        self.assertEqual(self.app.auto_decide(f, own)[0], "none")
        self.assertEqual(len(self.app._auto), n)  # no session: not cached
        f.messages = [U("x"), tool("Skill", {"skill": "mine"}), RES, U("fresh")]
        self.assertEqual(self.app.auto_decide(f, own)[0], "planning")  # the settings passed in, not app.routing's

    def test_auto_model_unreadable_or_failing_falls_back(self):
        with open(os.path.join(self.dir, "auto_model.json"), "w") as f:
            f.write("{nope")
        app = App(Config("127.0.0.1", 0, self.a.url, (self.v.url, "VK")), self.dir)
        app.store.close()
        self.assertIsNone(app.auto_model)
        self.assertIn("Auto mode's model couldn't be read (ValueError)", app.banner)
        msgs = [U("an idea"), tool("Skill", {"skill": "superpowers:brainstorming"}), RES, U("go")]
        f = signaller.Features.from_body(body_for("claude-opus-5-5", {"messages": msgs}))
        self.assertEqual(app.auto_decide(f, app.routing.auto)[0], "planning")  # phase alone still decides
        app.auto_model = mock.Mock(score=mock.Mock(side_effect=RuntimeError))
        f = signaller.Features.from_body(body_for("claude-opus-5-5", {"messages": [U("fresh")]}))
        self.assertEqual(app.auto_decide(f, app.routing.auto), ("none", "model error"))  # never raised


def U(text):
    return {"role": "user", "content": text}


def tool(name, inp):
    return {"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": name, "input": inp}]}


RES = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}


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
