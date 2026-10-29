import contextlib
import io
import json
import statistics
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from claude_router import App, Config, make_server
from routing import DEFAULT_CONFIG, Rule

BUDGET_MS = 5
OUT = json.dumps({"id": "m", "choices": [{"message": {"content": '{"label": "coding", "confidence": 0.9}'}}]}).encode()


class H(BaseHTTPRequestHandler):  # one fake for LiteLLM (chat + messages) and as the direct baseline
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        self.send_response(200)
        self.send_header("content-length", str(len(OUT)))
        self.end_headers()
        self.wfile.write(OUT)


USER_RULES = tuple(
    Rule("u%d" % i, "dsl", True, t, None) for i, t in enumerate(
        ['when text contains "zebra%d" go local' % i if i % 3 == 0 else
         'when text matches /^zz%d\\b/i go anthropic' % i if i % 3 == 1 else
         'when tokens > %dk go anthropic' % (500 + i) for i in range(20)]))


class SpeedTest(unittest.TestCase):
    def test_router_overhead_under_budget(self):
        fake = ThreadingHTTPServer(("127.0.0.1", 0), H)
        fake.daemon_threads = True
        threading.Thread(target=fake.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        url = "http://127.0.0.1:%d" % fake.server_address[1]
        quiet = contextlib.redirect_stderr(io.StringIO())
        quiet.__enter__()
        cfg = Config("127.0.0.1", 0, url, url, "LK", url, "VK")
        app = App(cfg, tempfile.mkdtemp())
        app.routing = replace(DEFAULT_CONFIG, rules=USER_RULES + DEFAULT_CONFIG.rules)  # user rules first: worst case
        srv = make_server(cfg, app)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

        def cleanup():
            srv.shutdown(); srv.server_close(); fake.shutdown(); fake.server_close()
            app.store.close()
            quiet.__exit__(None, None, None)
        self.addCleanup(cleanup)

        body = json.dumps({"model": "claude-sonnet-5-5", "max_tokens": 5, "stream": False,
                           "messages": [{"role": "user", "content": "fix the failing test"}]})

        def timed(port):
            t = time.perf_counter()
            c = HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("POST", "/v1/messages", body, {"content-type": "application/json"})
            r = c.getresponse(); r.read(); c.close()
            self.assertEqual(r.status, 200)
            return (time.perf_counter() - t) * 1000

        router, direct = srv.server_address[1], fake.server_address[1]
        for _ in range(20):  # first one fills the classifier cache; the rest warm everything
            timed(router); timed(direct)
        self.assertEqual(len(app.classifier.cache), 1)
        r = [timed(router) for _ in range(200)]
        d = [timed(direct) for _ in range(200)]
        extra = statistics.median(r) - statistics.median(d)
        print("router %.2f ms, direct %.2f ms, added %.2f ms" % (statistics.median(r), statistics.median(d), extra))
        self.assertLess(extra, BUDGET_MS)


if __name__ == "__main__":
    unittest.main()


def test_summary_10k_rows_under_50ms(tmp_path):
    from store import Store
    s, now = Store(str(tmp_path / "s.db")), time.time()
    for i in range(10_000):
        s.record_request(dict(id=i + 1, ts=now - i, track=("anthropic", "valar", "local")[i % 3], reason="rule:r%d" % (i % 5),
                              session_id="s%d" % (i % 4), cost=0.01, api_cost=0.02, in_tok=5, out_tok=5, status=200))
    s.close()
    s.summary(None, 0, now + 1, now)
    t = time.perf_counter()
    s.summary(["s1", "s2"], 0, now + 1, now)
    assert (time.perf_counter() - t) * 1000 < 50
