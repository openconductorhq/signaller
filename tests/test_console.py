import json
import os
import time
import unittest
from http.client import HTTPConnection

from tests import test_proxy
from tests.test_proxy import body_for, HDRS



# reuse ProxyTest's fixtures without inheriting (and re-running) its tests
Fixture = type("Fixture", (unittest.TestCase,), {k: v for k, v in vars(test_proxy.ProxyTest).items()
                                                  if not k.startswith(("test_", "__"))})


class ConsoleTest(Fixture):
    def req(self, method, path, body=None, origin="own", host=None):
        h = {}
        if origin == "own":
            origin = "http://127.0.0.1:%d" % self.port
        if origin:
            h["Origin"] = origin
        if host:
            h["Host"] = host
        c = HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def js(self, *a, **k):
        r, d = self.req(*a, **k)
        return r.status, json.loads(d)

    def test_page_served_with_csp(self):
        r, d = self.req("GET", "/ui", origin=None)
        self.assertEqual(r.status, 200)
        self.assertIn("text/html", r.getheader("Content-Type"))
        self.assertEqual(r.getheader("Content-Security-Policy"), "default-src 'self'")
        self.assertEqual(r.getheader("X-Frame-Options"), "DENY")

    def test_put_without_origin_forbidden(self):
        self.assertEqual(self.req("PUT", "/ui/api/config", {}, origin=None)[0].status, 403)

    def test_foreign_origin_forbidden(self):
        self.assertEqual(self.req("GET", "/ui/api/state", origin="https://evil.example")[0].status, 403)

    def test_other_localhost_port_origin_forbidden(self):
        self.assertEqual(self.req("GET", "/ui/api/state", origin="http://127.0.0.1:3000")[0].status, 403)

    def test_own_origin_ok(self):
        s, j = self.js("GET", "/ui/api/state")
        self.assertEqual(s, 200)
        self.assertEqual(j["config"]["lever"], "rules")
        self.assertEqual(j["upstreams"]["local"], "S via LiteLLM")

    def test_v1_with_own_origin_still_forbidden(self):
        s, _ = self.post(body_for("claude-opus-5-5"), {**HDRS, "Origin": "http://127.0.0.1:%d" % self.port})
        self.assertEqual(s, 403)

    def test_non_loopback_host_forbidden(self):
        self.assertEqual(self.req("GET", "/ui/api/state", host="evil.example")[0].status, 403)

    def test_static_traversal_404(self):
        for p in ("/ui/../claude_router.py", "/ui/%2e%2e/claude_router.py", "/ui/..%2fclaude_router.py"):
            self.assertEqual(self.req("GET", p, origin=None)[0].status, 404, p)

    def test_put_invalid_config_unchanged(self):
        cfg = self.js("GET", "/ui/api/state")[1]["config"]
        s, j = self.js("PUT", "/ui/api/config", {**cfg, "lever": "bogus"})
        self.assertEqual((s, j["error"]), (400, "lever must be anthropic, rules or local"))
        self.assertEqual(self.js("GET", "/ui/api/state")[1]["config"]["lever"], "rules")

    def test_put_nested_rule_is_400(self):
        cfg = self.js("GET", "/ui/api/state")[1]["config"]
        cfg["rules"][0]["text"] = "when " + "(" * 5000 + "tier is opus" + ")" * 5000 + " go local"
        self.assertEqual(self.js("PUT", "/ui/api/config", cfg)[0], 400)

    def test_put_config_applies(self):
        cfg = self.js("GET", "/ui/api/state")[1]["config"]
        self.assertEqual(self.js("PUT", "/ui/api/config", {**cfg, "lever": "local"})[0], 200)
        self.post(body_for("claude-opus-5-5"))
        self.assertEqual((len(self.l.seen), len(self.a.seen)), (1, 0))

    def test_preview(self):
        self.post(body_for("claude-opus-5-5"))
        self.post(body_for("claude-sonnet-5-5", {"messages": [{"role": "user", "content": "add schema migration"}]}))
        self.post(body_for("claude-sonnet-5-5"))
        self.logged(3)
        t = time.time()
        while len(self.app.recent) < 3 and time.time() - t < 2:
            time.sleep(0.01)
        rule = {"name": "mig", "text": 'when text contains "migration" go anthropic'}
        s, j = self.js("POST", "/ui/api/preview", {"rule": rule, "position": 0})
        self.assertEqual((s, j["matched"], j["changed"], j["count"]), (200, 1, 1, 3))
        self.assertEqual((j["rows"][0]["went"], j["rows"][0]["would"]), ("local", "anthropic"))
        s, j = self.js("POST", "/ui/api/preview", {"rule": {"name": "x", "text": "when bogus"}, "position": 0})
        self.assertEqual(s, 400)
        self.assertIn("line", j)
        self.assertIn("col", j)

    def test_verdict_and_forget(self):
        self.app.store.record_turn({"id": "t1", "ts": time.time(), "preview": "p", "verdict": None})
        self.app.store.close()
        self.app.store.__init__(self.app.store.path)  # reopen: close() stopped the writer
        self.assertEqual(self.js("GET", "/ui/api/turns")[1][0]["id"], "t1")
        self.assertEqual(self.js("PUT", "/ui/api/turns/t1/verdict", {"verdict": "same"})[0], 200)
        self.assertEqual(self.js("PUT", "/ui/api/turns/t1/verdict", {"verdict": "zzz"})[0], 400)
        self.assertEqual(self.js("GET", "/ui/api/turns/t1")[1]["verdict"], "same")
        self.assertEqual(self.js("DELETE", "/ui/api/pairs")[0], 200)
        self.assertEqual(self.js("GET", "/ui/api/turns")[1], [])


if __name__ == "__main__":
    unittest.main()
