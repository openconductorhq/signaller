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
    def req(self, method, path, body=None, origin="own", host=None, hdrs=None):
        h = dict(hdrs or {})
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
        self.assertEqual((s, j["error"]), (400, "lever: no downstream named bogus"))
        self.assertEqual(self.js("GET", "/ui/api/state")[1]["config"]["lever"], "rules")

    def test_put_nested_rule_is_400(self):
        cfg = self.js("GET", "/ui/api/state")[1]["config"]
        cfg["rules"][0]["text"] = "when " + "(" * 5000 + "tier is opus" + ")" * 5000 + " go local"
        self.assertEqual(self.js("PUT", "/ui/api/config", cfg)[0], 400)

    def test_put_config_applies(self):
        cfg = self.js("GET", "/ui/api/state")[1]["config"]
        self.assertEqual(self.js("PUT", "/ui/api/config", {**cfg, "lever": "valar"})[0], 200)
        self.post(body_for("claude-opus-5-5"))
        self.assertEqual((len(self.v.seen), len(self.a.seen)), (1, 0))

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

    def test_session_lever_applies_to_that_session_only(self):
        self.assertEqual(self.js("GET", "/ui/api/lever?session=s1")[1], {"lever": "rules", "session": None, "global": "rules"})
        self.assertEqual(self.js("PUT", "/ui/api/lever", {"session": "s1", "lever": "valar"})[1]["lever"], "valar")
        self.assertEqual(self.js("PUT", "/ui/api/lever", {"session": "s1", "lever": "bogus"})[0], 400)
        self.assertEqual(self.js("PUT", "/ui/api/lever", {"session": "unknown", "lever": "valar"})[0], 400)
        self.assertEqual(self.req("PUT", "/ui/api/lever", {"session": "s1", "lever": "local"}, origin=None)[0].status, 403)
        for sid in ("s1", "s2"):
            self.post(body_for("claude-opus-5-5"), {**HDRS, "X-Claude-Code-Session-Id": sid})
        self.assertEqual((len(self.v.seen), len(self.a.seen)), (1, 1))  # s1 → valar, s2 keeps the global rules
        self.assertEqual(self.js("GET", "/ui/api/state")[1]["session_levers"], {"s1": "valar"})  # the console's lever follows it
        self.js("PUT", "/ui/api/lever", {"session": "s1", "lever": None})
        self.assertEqual(self.js("GET", "/ui/api/lever?session=s1")[1]["session"], None)
        self.assertEqual(self.js("GET", "/ui/api/state")[1]["session_levers"], {})

    def test_session_name(self):
        self.assertEqual(self.js("PUT", "/ui/api/name", {"session": "s1", "name": "  fix   login "})[1], {"session": "s1", "name": "fix login"})
        self.assertEqual(self.js("PUT", "/ui/api/name", {"session": "s1", "name": ""})[1]["name"], None)
        self.assertEqual(self.js("PUT", "/ui/api/name", {"session": "unknown", "name": "x"})[0], 400)
        self.assertEqual(self.js("PUT", "/ui/api/name", {"session": "s1", "name": 5})[0], 400)
        self.assertEqual(self.req("PUT", "/ui/api/name", {"session": "s1", "name": "x"}, origin=None)[0].status, 403)

    def test_preview_ignores_lever(self):
        self.post(body_for("claude-sonnet-5-5", {"messages": [{"role": "user", "content": "add schema migration"}]}))
        self.logged(1)
        t = time.time()
        while not self.app.recent and time.time() - t < 2:
            time.sleep(0.01)
        cfg = self.js("GET", "/ui/api/state")[1]["config"]
        self.assertEqual(self.js("PUT", "/ui/api/config", {**cfg, "lever": "valar"})[0], 200)
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
        self.v.models = json.dumps({"data": [{"id": "b", "pricing": [
            {"tier": "standard", "input_per_1m_tokens": 1, "cached_input_per_1m_tokens": 0.1, "output_per_1m_tokens": 5},
            {"tier": "realtime", "input_per_1m_tokens": 2, "cached_input_per_1m_tokens": 0.2, "output_per_1m_tokens": 9},
            {"tier": "flex", "input_per_1m_tokens": 1, "cached_input_per_1m_tokens": 0.1, "output_per_1m_tokens": 5}]},
            {"id": "a", "pricing": []}]}).encode()
        r, d = self.req("GET", "/ui/api/models?track=valar")
        self.assertEqual((r.status, json.loads(d)), (200, {"models": ["a", "b"], "windows": {"b": ["asap", "standard"]}}))
        with open(os.path.join(self.dir, "models.json")) as f:
            saved = json.load(f)
        self.assertEqual(saved["valar"], ["a", "b"])
        self.assertEqual(saved["valar_prices"], {"b": {"standard": [1, 0.1, 5], "asap": [2, 0.2, 9]}})
        self.assertEqual(self.app.prices.valar, saved["valar_prices"])
        self.assertEqual(self.v.seen[0][:2], ("GET", "/v1/models"))
        self.assertEqual(self.v.seen[0][2]["Authorization"], "Bearer VK")
        self.assertNotIn(b"VK", d)

    def test_models_saved_list_when_upstream_down(self):
        self.v.models = b'{"data":[{"id":"a"}]}'
        self.js("GET", "/ui/api/models?track=valar")
        self.app.cfg = replace(self.app.cfg, valar_url="http://127.0.0.1:1")
        s, j = self.js("GET", "/ui/api/models?track=valar")
        self.assertEqual((s, j["models"]), (200, ["a"]))
        self.assertIn("Valar unreachable", j["stale"])
        s, j = self.js("GET", "/ui/api/models?track=local")  # nothing saved for local yet
        self.assertEqual(s, 502)

    def test_models_sec_fetch_site(self):
        self.v.models = b'{"data":[{"id":"a"}]}'
        for site, want in (("cross-site", 403), ("same-site", 403), ("same-origin", 200), ("none", 200), (None, 200)):
            n = len(self.v.seen)
            r, _ = self.req("GET", "/ui/api/models?track=valar", hdrs={"Sec-Fetch-Site": site} if site else None)
            self.assertEqual(r.status, want, site)
            self.assertEqual(len(self.v.seen) - n, int(want == 200), site)

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

    def test_filters(self):
        now = time.time()
        self.app.store.record_request({"id": 900, "ts": now - 10, "track": "valar", "status": 200, "session_id": "zz", "reason": "default"})
        self.app.store.close()
        self.app.store.__init__(self.app.store.path)
        self.assertEqual(self.js("GET", "/ui/api/state?sessions=zz")[1]["summary"]["total"], 1)
        self.assertEqual(self.js("GET", "/ui/api/state?sessions=zz&from=session")[1]["summary"]["tracks"]["valar"]["n"], 1)
        self.assertEqual(self.js("GET", "/ui/api/state?sessions=zz&from=%f&to=%f" % (now - 5, now))[1]["summary"]["total"], 0)
        self.assertIn("stats", self.js("GET", "/ui/api/state")[1])
        self.assertEqual([x["id"] for x in self.js("GET", "/ui/api/sessions?from=session")[1]][:1], ["zz"])
        self.assertEqual([r["id"] for r in self.js("GET", "/ui/api/departures?sessions=zz")[1]], [900])
        for p in ("/ui/api/state?from=abc", "/ui/api/state?to=x", "/ui/api/sessions?from=abc"):
            s, j = self.js("GET", p)
            self.assertEqual((s, j["error"]), (400, "from/to must be numbers or 'session'"))


class ListModelsRedirectTest(unittest.TestCase):
    def test_redirect_not_followed_and_key_not_leaked(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from console import list_models
        auth = []

        class B(BaseHTTPRequestHandler):
            def do_GET(self):
                auth.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"data":[]}')
            log_message = lambda *a: None
        b = HTTPServer(("127.0.0.1", 0), B)

        class A(B):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:%d/v1/models" % b.server_address[1])
                self.end_headers()
        a = HTTPServer(("127.0.0.1", 0), A)
        for x in (a, b):
            threading.Thread(target=x.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(Exception):
                list_models("http://127.0.0.1:%d" % a.server_address[1], "k")
            self.assertEqual(auth, [])
        finally:
            a.shutdown(); b.shutdown(); a.server_close(); b.server_close()


class HoistTolerantTest(unittest.TestCase):
    def test_malformed_shapes(self):
        from claude_router import hoist_inline_tools
        good = {"type": "tool_definition", "definition": {"name": "g", "x": 1}}
        req = {"tools": ["junk", {"name": "t"}], "messages": [{"role": "user", "content": [
            {"type": "tool_addition", "tool": "junk"}, {"type": "tool_addition", "tool": {"type": "tool_definition", "definition": "s"}},
            {"type": "tool_addition", "tool": {"type": "tool_definition", "definition": {"x": 1}}},
            {"type": "tool_addition", "tool": good}]}]}
        self.assertTrue(hoist_inline_tools(req))
        self.assertEqual(req["tools"], ["junk", {"name": "t"}, {"name": "g", "x": 1}])
        self.assertEqual(req["messages"], [])


if __name__ == "__main__":
    unittest.main()
