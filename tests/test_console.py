import json
import os
import time
import unittest
from dataclasses import replace
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
        self.assertEqual(j["upstreams"]["local"], {"url": self.l.url[7:], "configured": True})

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
        self.assertEqual((s, j["error"]), (400, "lever must be anthropic, rules, valar or local"))
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
        self.assertEqual((j["rows"][0]["went"], j["rows"][0]["would"]), ("valar", "anthropic"))
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

    def raw(self, request):
        import socket
        c = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        c.sendall(request)
        data = b""
        while True:
            b = c.recv(65536)
            if not b:
                break
            data += b
        c.close()
        return data

    def test_bad_input_is_400_not_a_dropped_connection(self):
        own = "http://127.0.0.1:%d" % self.port
        cases = [
            ("GET", "/ui/%00", None), ("GET", "/ui/api/departures?since=" + "9" * 31, None),
            ("GET", "/ui/api/departures?since=abc", None), ("GET", "/ui/api/requests/" + "9" * 30, None),
            ("POST", "/ui/api/preview", b'{"rule":{"name":"x","text":"when tier is opus go local"},"position":1e999}'),
            ("PUT", "/ui/api/config", b"[" * 100000),
        ]
        for m, path, body in cases:
            req = ("%s %s HTTP/1.0\r\nHost: 127.0.0.1:%d\r\nOrigin: %s\r\nContent-Length: %d\r\n\r\n"
                   % (m, path, self.port, own, len(body or b""))).encode() + (body or b"")
            self.assertIn(b" 400 ", self.raw(req).split(b"\r\n")[0], (m, path))
        for cl in ("abc", "-5", str(2 << 20)):
            req = ("PUT /ui/api/config HTTP/1.0\r\nHost: 127.0.0.1:%d\r\nOrigin: %s\r\nContent-Length: %s\r\n\r\n"
                   % (self.port, own, cl)).encode()
            self.assertIn(b" 400 ", self.raw(req).split(b"\r\n")[0], cl)

    def test_query_string_on_ui_is_console(self):
        r, _ = self.req("GET", "/ui?x=1", origin=None)
        self.assertEqual(r.status, 200)

    def test_preview_ignores_lever(self):
        self.post(body_for("claude-sonnet-5-5", {"messages": [{"role": "user", "content": "add schema migration"}]}))
        self.logged(1)
        t = time.time()
        while not self.app.recent and time.time() - t < 2:
            time.sleep(0.01)
        cfg = self.js("GET", "/ui/api/state")[1]["config"]
        self.js("PUT", "/ui/api/config", {**cfg, "lever": "local"})
        rule = {"name": "mig", "text": 'when text contains "migration" go anthropic'}
        j = self.js("POST", "/ui/api/preview", {"rule": rule, "position": 0})[1]
        self.assertEqual((j["matched"], j["changed"]), (1, 1))

    def test_page_assets_local_only(self):
        ui = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")
        for root, _, files in os.walk(ui):
            for n in files:
                if n != "OFL.txt":
                    with open(os.path.join(root, n), "rb") as f:
                        data = f.read()
                    self.assertNotIn(b"http://", data, n)
                    self.assertNotIn(b"https://", data, n)
        for path, ctype in (("/ui/app.js", "text/javascript"), ("/ui/app.css", "text/css"),
                            ("/ui/fonts/barlow-condensed-latin-600-normal.woff2", "font/woff2")):
            r, _ = self.req("GET", path, origin=None)
            self.assertEqual(r.status, 200, path)
            self.assertTrue(r.getheader("Content-Type").startswith(ctype), path)

    def test_models_endpoint(self):
        self.v.models = b'{"data":[{"id":"b"},{"id":"a"}]}'
        r, d = self.req("GET", "/ui/api/models?track=valar")
        self.assertEqual((r.status, json.loads(d)), (200, {"models": ["a", "b"]}))
        self.assertEqual(self.v.seen[0][:2], ("GET", "/v1/models"))
        self.assertEqual(self.v.seen[0][2]["Authorization"], "Bearer VK")
        self.assertNotIn(b"VK", d)

    def test_models_errors(self):
        self.assertEqual(self.js("GET", "/ui/api/models?track=anthropic")[0], 400)
        self.assertEqual(self.js("GET", "/ui/api/models?track=bogus")[0], 400)
        self.assertEqual(self.js("GET", "/ui/api/models")[0], 400)
        self.assertEqual(self.req("GET", "/ui/api/models?track=valar", host="evil.example")[0].status, 403)
        self.assertEqual(self.req("GET", "/ui/api/models?track=valar", origin="http://evil.com")[0].status, 403)
        self.assertEqual(self.v.seen, [])
        self.app.cfg = replace(self.app.cfg, valar_key="")
        s, j = self.js("GET", "/ui/api/models?track=valar")
        self.assertEqual((s, j["error"]), (502, "Valar not configured"))
        self.assertEqual(self.v.seen, [])
        self.app.cfg = replace(self.app.cfg, valar_key="VK", valar_url="http://127.0.0.1:1")
        s, j = self.js("GET", "/ui/api/models?track=valar")
        self.assertEqual(s, 502)
        self.assertNotIn("VK", json.dumps(j))

    def test_state_upstreams_no_key(self):
        s, j = self.js("GET", "/ui/api/state")
        self.assertTrue(j["upstreams"]["valar"]["configured"])
        self.assertEqual(j["upstreams"]["valar"]["url"], self.v.url[7:])
        self.assertNotIn("VK", json.dumps(j))
        self.assertNotIn("LK", json.dumps(j))
        self.app.cfg = replace(self.app.cfg, valar_key="")
        self.assertFalse(self.js("GET", "/ui/api/state")[1]["upstreams"]["valar"]["configured"])

    def test_verdict_api_valar(self):
        self.app.store.record_turn({"id": "t2", "ts": time.time(), "preview": "p", "verdict": None})
        self.assertEqual(self.js("PUT", "/ui/api/turns/t2/verdict", {"verdict": "valar"})[0], 200)

    def test_page_has_valar(self):
        page = self.req("GET", "/ui/", origin=None)[1].decode()
        self.assertIn('data-v="valar"', page)
        self.assertIn('id="tracks"', page)
        self.assertIn('api("GET", "models?track="', self.req("GET", "/ui/app.js", origin=None)[1].decode())


if __name__ == "__main__":
    unittest.main()
