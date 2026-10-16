import contextlib
import io
import json
import socket
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from claude_router import Config, local_headers, make_server


class Fake(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), FakeHandler)
        self.seen = []
        self.error = False
        threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.server_address[1]


class FakeHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        self.server.seen.append((self.command, self.path, self.headers, body))
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
        self.end_headers()
        try:
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
        quiet = contextlib.redirect_stderr(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.a, self.l = Fake(), Fake()
        self.start(self.l.url)

    def start(self, litellm_url):
        self.srv = make_server(Config("127.0.0.1", 0, self.a.url, litellm_url, "LK", "S", "H"))
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        for s in (self.srv, self.a, self.l):
            s.shutdown()
            s.server_close()

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

    def test_local_swaps_credentials_and_model(self):
        self.post(body_for("claude-sonnet-5-5", {"max_tokens": 7}), {**HDRS, "x-api-key": "client"})
        _, path, h, got = self.l.seen[0]
        self.assertEqual(path, "/v1/messages?beta=true")
        self.assertEqual(h["Authorization"], "Bearer LK")
        self.assertIsNone(h["x-api-key"])
        self.assertEqual(h["anthropic-beta"], "claude-code-20250219")
        j = json.loads(got)
        self.assertEqual(j["model"], "S")
        self.assertEqual(j["max_tokens"], 7)
        self.assertEqual(self.a.seen, [])

    def test_streams_incrementally(self):
        c = HTTPConnection("127.0.0.1", self.port, timeout=5)
        t = time.time()
        c.request("POST", "/v1/messages", body_for("claude-opus-5-5", stream=True), HDRS)
        r = c.getresponse()
        self.assertEqual(r.fp.readline(), b"data: 0\n")
        self.assertLess(time.time() - t, 0.25)
        c.close()

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
        status, data = self.post(body_for("claude-sonnet-5-5"))
        self.assertEqual(status, 502)
        j = json.loads(data)
        self.assertEqual(j["type"], "error")
        self.assertEqual(j["error"]["type"], "api_error")
        self.assertIn("LiteLLM unreachable", j["error"]["message"])

    def test_client_disconnect_mid_stream(self):
        body = body_for("claude-opus-5-5", stream=True)
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
        self.assertTrue(s.recv(4096))
        s.close()
        self.assertEqual(self.post(body_for("claude-opus-5-5"))[0], 200)

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


class LocalHeadersTest(unittest.TestCase):
    def test_local_headers(self):
        h = dict(local_headers([
            ("Authorization", "x"), ("X-Api-Key", "y"), ("Host", "h"), ("Content-Length", "1"),
            ("Connection", "c"), ("Transfer-Encoding", "t"), ("anthropic-version", "v"),
            ("anthropic-beta", "a, oauth-2025-04-20,b"),
        ], "K"))
        self.assertEqual(h, {"anthropic-version": "v", "anthropic-beta": "a,b", "Authorization": "Bearer K"})
        h = dict(local_headers([("Anthropic-Beta", "oauth-2025-04-20")], "K"))
        self.assertEqual(h, {"Authorization": "Bearer K"})


if __name__ == "__main__":
    unittest.main()
