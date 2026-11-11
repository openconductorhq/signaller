import json
import os
import time
import unittest
from dataclasses import replace

from autophase import AutoSettings
from http.client import HTTPConnection

import contextlib
import io
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import autotrain
import overrides
from signaller import App, Config, make_server
from routing import MODEL_TIERS
from tests import test_proxy
from tests.test_autotrain import write_transcript, user as tu, asst as ta
from tests.test_proxy import body_for, HDRS, CLASSIFIER_OFF, VALAR



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

    def put_cfg(self, **patch):
        """A whole-config PUT from the page's point of view: the config it last rendered, plus that rev."""
        st = self.js("GET", "/ui/api/state")[1]
        return self.js("PUT", "/ui/api/config", {**st["config"], **patch, "rev": st["config_rev"]})

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
        self.assertEqual(j["downstreams"]["valar"], {"url": self.v.url[7:], "key_set": True})
        self.assertNotIn("upstreams", j)

    def test_v1_with_own_origin_still_forbidden(self):
        s, _ = self.post(body_for("claude-opus-5-5"), {**HDRS, "Origin": "http://127.0.0.1:%d" % self.port})
        self.assertEqual(s, 403)

    def test_non_loopback_host_forbidden(self):
        self.assertEqual(self.req("GET", "/ui/api/state", host="evil.example")[0].status, 403)

    def test_static_traversal_404(self):
        for p in ("/ui/../signaller.py", "/ui/%2e%2e/signaller.py", "/ui/..%2fsignaller.py"):
            self.assertEqual(self.req("GET", p, origin=None)[0].status, 404, p)

    def test_put_invalid_config_unchanged(self):
        s, j = self.put_cfg(lever="bogus")
        self.assertEqual((s, j["error"]), (400, "lever: no downstream named bogus"))
        self.assertEqual(self.js("GET", "/ui/api/state")[1]["config"]["lever"], "rules")

    def test_put_nested_rule_is_400(self):
        cfg = self.js("GET", "/ui/api/state")[1]["config"]
        cfg["rules"][0]["text"] = "when " + "(" * 5000 + "tier is opus" + ")" * 5000 + " go local"
        self.assertEqual(self.js("PUT", "/ui/api/config",
                                 {**cfg, "rev": self.js("GET", "/ui/api/state")[1]["config_rev"]})[0], 400)

    def test_put_config_applies(self):
        self.assertEqual(self.put_cfg(lever="valar")[0], 200)
        self.post(body_for("claude-opus-5-5"))
        self.assertEqual((len(self.v.seen), len(self.a.seen)), (1, 0))

    def test_config_put_with_a_stale_rev_is_409(self):
        st = self.js("GET", "/ui/api/state")[1]
        cfg, rev = st["config"], st["config_rev"]
        # a settings line written by /signaller after the page rendered: the page's copy is stale
        self.app.set_settings_for_session("sx", "valar", None, "window", "priority")
        s, j = self.js("PUT", "/ui/api/config", {**cfg, "lever": "anthropic", "rev": rev})
        self.assertEqual((s, j["error"]), (409, "the config changed elsewhere; reloaded"))
        self.assertEqual(self.js("GET", "/ui/api/state")[1]["config"]["lever"], "rules")  # nothing changed
        s, j = self.put_cfg(lever="anthropic")  # a fresh rev goes through
        self.assertEqual((s, self.js("GET", "/ui/api/state")[1]["config"]["lever"]), (200, "anthropic"))
        self.assertEqual(j["rev"], self.js("GET", "/ui/api/state")[1]["config_rev"])
        self.assertEqual(self.req("PUT", "/ui/api/config", cfg, origin=None)[0].status, 403)
        self.assertEqual(self.js("PUT", "/ui/api/config", {**cfg, "rev": rev})[0], 409)  # missing/stale rev, no origin issues

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

    def test_plugin_reports_first_party_flag(self):
        self.assertIsNone(self.js("GET", "/ui/api/state")[1]["first_party"])  # no report yet: the console offers the alias
        self.js("GET", "/ui/api/lever?session=s1&fp=0")
        self.assertIs(self.js("GET", "/ui/api/state")[1]["first_party"], False)
        self.js("GET", "/ui/api/lever?session=s1&fp=1")
        self.assertIs(self.js("GET", "/ui/api/state")[1]["first_party"], True)
        self.js("GET", "/ui/api/lever?session=s1")  # an older plugin says nothing: the last report stands
        self.assertIs(self.js("GET", "/ui/api/state")[1]["first_party"], True)

    def test_session_lever_applies_to_that_session_only(self):
        self.assertEqual(self.js("GET", "/ui/api/lever?session=s1")[1], {"lever": "rules", "session": None, "global": "rules",
                                                                         "modes": ["rules", "auto", "anthropic", "valar"], "name": None,
                                                                         "notice": None, "prompts": {}, "auto": None, "trained": False})
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

    def test_auto_session_lever_survives_without_downstreams(self):
        self.assertEqual(self.js("PUT", "/ui/api/lever", {"session": "s1", "lever": "auto"})[1]["lever"], "auto")
        self.app.routing = replace(self.app.routing, downstreams=())
        self.assertEqual(self.app.lever_for("s1"), "auto")

    def test_lever_answer_carries_the_auto_phase(self):
        self.js("PUT", "/ui/api/lever", {"session": "s1", "lever": "auto"})
        self.post(body_for("claude-opus-5-5", {"tools": [{"name": "Bash", "input_schema": {}}]}),
                  {**HDRS, "X-Claude-Code-Session-Id": "s1"})  # a main-loop request carries its tools
        self.assertEqual(self.js("GET", "/ui/api/lever?session=s1")[1]["auto"], {"phase": "none", "why": "no planning signal"})
        self.js("PUT", "/ui/api/lever", {"session": "s1", "lever": "rules"})
        self.assertIsNone(self.js("GET", "/ui/api/lever?session=s1")[1]["auto"])  # only while auto is its lever

    def test_dangling_session_lever_shows_the_global(self):
        self.app.session_levers["s1"] = "lemmi"  # a track this setup lacks, saved before a restart
        j = self.js("GET", "/ui/api/lever?session=s1")[1]
        self.assertEqual(j["lever"], "rules")
        self.assertEqual(j["session"], None)

    def test_state_says_whether_the_auto_model_is_loaded(self):
        self.assertIs(self.js("GET", "/ui/api/state")[1]["auto_model"], False)
        self.app.auto_model = object()
        self.assertIs(self.js("GET", "/ui/api/state")[1]["auto_model"], True)

    def test_state_serves_only_levers_the_setup_can_follow(self):
        self.app.session_levers["s1"] = "valar"
        self.app.session_levers["s2"] = "lemmi"  # a preset switch or restart dropped that track
        self.assertEqual(self.js("GET", "/ui/api/state")[1]["session_levers"], {"s1": "valar"})
        self.assertEqual(self.app.session_levers, {"s1": "valar", "s2": "lemmi"})  # the raw entry is kept

    def test_prompts_saved_and_served_to_the_plugin(self):
        s, j = self.put_cfg(prompts=[{"name": "execute", "text": "1. /signaller anthropic\n2. /simplify"}])
        self.assertEqual(s, 200)
        self.assertEqual(j["prompts"], [{"name": "execute", "text": "1. /signaller anthropic\n2. /simplify"}])
        self.assertEqual(self.js("GET", "/ui/api/lever?session=s1")[1]["prompts"], {"execute": "1. /signaller anthropic\n2. /simplify"})
        for bad in ([{"name": "Run It", "text": "x"}], [{"name": "a", "text": " "}], [{"name": "a", "text": "x"}, {"name": "a", "text": "y"}]):
            self.assertEqual(self.put_cfg(prompts=bad)[0], 400, bad)

    def test_session_name(self):
        self.assertEqual(self.js("PUT", "/ui/api/name", {"session": "s1", "name": "  fix   login "})[1], {"session": "s1", "name": "fix login"})
        self.assertEqual(self.js("GET", "/ui/api/lever?session=s1")[1]["name"], "fix login")  # the plugin's status line shows it
        self.assertEqual(self.js("PUT", "/ui/api/name", {"session": "s1", "name": ""})[1]["name"], None)
        self.assertEqual(self.js("PUT", "/ui/api/name", {"session": "unknown", "name": "x"})[0], 400)
        self.assertEqual(self.js("PUT", "/ui/api/name", {"session": "s1", "name": 5})[0], 400)
        self.assertEqual(self.req("PUT", "/ui/api/name", {"session": "s1", "name": "x"}, origin=None)[0].status, 403)

    def test_session_settings_endpoint(self):
        put = lambda b: self.js("PUT", "/ui/api/settings", b)
        line = {"session": "S1", "downstream": "valar", "tier": None, "set": "window", "value": "priority"}
        s, j = put(line)
        self.assertEqual(s, 200)
        self.assertEqual([(x["when"], x["value"]) for x in j["lines"]], [("session is s1", "priority")])  # the sid is lowered
        self.assertEqual(j["collisions"], [])
        self.assertEqual(self.js("GET", "/ui/api/state")[1]["config"]["settings"][0]["value"], "priority")  # the line is at the top
        s, j = put({**line, "value": "asap"})  # the same key updates in place, so still one line
        self.assertEqual([x["value"] for x in j["lines"]], ["asap"])
        s, j = put({"session": "s1", "downstream": "valar", "reset": True})
        self.assertEqual((s, j["lines"]), (200, []))
        self.assertEqual(self.js("GET", "/ui/api/state")[1]["config"]["settings"], [])
        self.assertEqual(self.req("PUT", "/ui/api/settings", line, origin=None)[0].status, 403)
        self.assertEqual(put({**line, "value": "bogus"})[0], 400)
        self.assertEqual(put({**line, "set": "junk"})[0], 400)
        self.assertEqual(put({**line, "tier": "junk"})[0], 400)
        self.assertEqual(put({**line, "session": "a.b"})[0], 400)
        self.assertEqual(put({**line, "downstream": "nope"})[0], 400)

    def test_state_has_collisions_and_clock(self):
        self.app.routing = replace(self.app.routing, settings=overrides.settings_from_json(
            [{"when": "", "downstream": "valar", "tier": None, "set": "window", "value": "asap"},
             {"when": "session is s1", "downstream": "valar", "tier": None, "set": "window", "value": "priority"}],
            self.app.routing.downstreams))
        before = time.strftime("%H:%M")
        j = self.js("GET", "/ui/api/state")[1]
        self.assertEqual([(c["line"], c["by"]) for c in j["collisions"]], [(2, 1)])  # s1's line overlaps the always line
        self.assertEqual(set(j["clock"]), {"tz", "hm"})
        self.assertIn(j["clock"]["hm"], {before, time.strftime("%H:%M")})  # the router's own wall clock; a minute may roll between the request and here

    def test_settings_put_returns_only_this_sessions_collisions(self):
        self.app.routing = replace(self.app.routing, settings=overrides.settings_from_json(
            [{"when": "", "downstream": "valar", "tier": None, "set": "window", "value": "standard"},
             {"when": "session is s1", "downstream": "valar", "tier": None, "set": "window", "value": "priority"},
             {"when": "session is other", "downstream": "valar", "tier": None, "set": "window", "value": "asap"}],
            self.app.routing.downstreams))
        s, j = self.js("PUT", "/ui/api/settings", {"session": "s1", "downstream": "valar", "tier": None,
                                                  "set": "window", "value": "asap"})
        self.assertEqual(s, 200)
        # the always line overlaps both session lines; only the one holding s1's own line comes back
        self.assertEqual([(c["line"], c["by"]) for c in j["collisions"]], [(2, 1)])
        self.assertIn("overlaps line 1 (line 1 wins where both match)", j["collisions"][0]["text"])

    def test_prune_settings_drops_stale_sessions(self):
        self.app.store.touch_session("stale", time.time() - 8 * 86400)
        self.app.store.touch_session("fresh", time.time())
        self.app.store.close()
        self.app.routing = replace(self.app.routing, settings=overrides.settings_from_json(
            [{"when": "session is stale", "downstream": "valar", "tier": None, "set": "window", "value": "priority"},
             {"when": "session is ghost", "downstream": "valar", "tier": None, "set": "window", "value": "priority"},
             {"when": "session is fresh", "downstream": "valar", "tier": None, "set": "window", "value": "asap"}],
            self.app.routing.downstreams))
        self.app.prune_settings()
        # ghost has no sessions row (forgotten, or never seen): it counts as gone too
        self.assertEqual([s.when for s in self.app.routing.settings], ["session is fresh"])
        with open(os.path.join(self.dir, "console.json")) as f:  # the pruned config was saved
            self.assertEqual([x["when"] for x in json.load(f)["settings"]], ["session is fresh"])

    def test_forget_session(self):
        self.js("PUT", "/ui/api/name", {"session": "s1", "name": "doomed"})
        self.app.sessions._known.add("s1")
        self.assertIn("s1", [x["id"] for x in self.js("GET", "/ui/api/sessions?from=0")[1]])
        self.assertEqual(self.js("DELETE", "/ui/api/sessions/s1"), (200, {"ok": True}))
        self.assertNotIn("s1", [x["id"] for x in self.js("GET", "/ui/api/sessions?from=0")[1]])
        self.assertNotIn("s1", self.app.sessions._known)  # an active session finds its cwd · branch again
        self.assertEqual(self.js("DELETE", "/ui/api/sessions/unknown")[0], 200)
        self.assertEqual(self.req("DELETE", "/ui/api/sessions/a.b")[0].status, 400)
        self.assertEqual(self.req("DELETE", "/ui/api/sessions/s1", origin=None)[0].status, 403)

    def test_preview_ignores_lever(self):
        self.post(body_for("claude-sonnet-5-5", {"messages": [{"role": "user", "content": "add schema migration"}]}))
        self.logged(1)
        t = time.time()
        while not self.app.recent and time.time() - t < 2:
            time.sleep(0.01)
        cfg = self.js("GET", "/ui/api/state")[1]["config"]
        self.assertEqual(self.put_cfg(lever="valar")[0], 200)
        rule = {"name": "mig", "text": 'when text contains "migration" go anthropic'}
        j = self.js("POST", "/ui/api/preview", {"rule": rule, "position": 0})[1]
        self.assertEqual((j["matched"], j["changed"]), (1, 1))

    def test_page_assets_local_only(self):
        ui = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")
        for root, _, files in os.walk(ui):
            for n in files:
                if n != "OFL.txt":
                    with open(os.path.join(root, n), "rb") as f:
                        data = f.read().replace(b'xmlns="http://www.w3.org/2000/svg"', b"")  # a namespace name, never fetched
                    self.assertNotIn(b"http://", data, n)
                    self.assertNotIn(b"https://", data, n)
        for path, ctype in (("/ui/app.js", "text/javascript"), ("/ui/app.css", "text/css"), ("/ui/favicon.svg", "image/svg+xml"),
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
        self.assertEqual(saved["listed_prices"]["valar"], {"b": {"standard": [1, 0.1, 5], "asap": [2, 0.2, 9]}})
        self.assertEqual(self.app.prices.listed["valar"], saved["listed_prices"]["valar"])
        self.assertEqual(self.v.seen[0][:2], ("GET", "/v1/models"))
        self.assertEqual(self.v.seen[0][2]["Authorization"], "Bearer VK")
        self.assertNotIn(b"VK", d)

    def test_models_saved_list_when_upstream_down(self):
        self.v.models = b'{"data":[{"id":"a"}]}'
        self.js("GET", "/ui/api/models?track=valar")
        self.set_valar_url("http://127.0.0.1:1")
        self.app.keys.set("valar", "http://127.0.0.1:1", "VK")  # a new host means entering the key again
        s, j = self.js("GET", "/ui/api/models?track=valar")
        self.assertEqual((s, j["models"]), (200, ["a"]))
        self.assertIn("Valar unreachable", j["stale"])
        self.assertEqual(self.js("GET", "/ui/api/models?track=local")[0], 400)  # not a downstream any more

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
        self.app.keys.delete("valar", self.v.url)
        s, j = self.js("GET", "/ui/api/models?track=valar")
        self.assertEqual((s, j["error"]), (502, "Valar not configured"))
        self.assertEqual(self.v.seen, [])
        self.app.keys.set("valar", self.v.url, "VK")
        self.set_valar_url("http://127.0.0.1:1")
        s, j = self.js("GET", "/ui/api/models?track=valar")
        self.assertEqual(s, 502)
        self.assertNotIn("VK", json.dumps(j))

    def test_state_downstreams_no_key(self):
        s, j = self.js("GET", "/ui/api/state")
        self.assertTrue(j["downstreams"]["valar"]["key_set"])
        self.assertEqual(j["downstreams"]["valar"]["url"], self.v.url[7:])
        self.assertNotIn("VK", json.dumps(j))
        self.assertNotIn("LK", json.dumps(j))
        self.app.keys.delete("valar", self.v.url)
        self.assertFalse(self.js("GET", "/ui/api/state")[1]["downstreams"]["valar"]["key_set"])

    def test_verdict_api_valar(self):
        self.app.store.record_turn({"id": "t2", "ts": time.time(), "preview": "p", "verdict": None})
        self.assertEqual(self.js("PUT", "/ui/api/turns/t2/verdict", {"verdict": "valar"})[0], 200)

    def test_page_draws_downstreams(self):
        page = self.req("GET", "/ui/", origin=None)[1].decode()
        self.assertNotIn('data-v="valar"', page)  # the lever, yard and Tracks rows come from config.downstreams
        self.assertIn('id="tracks"', page)
        js = self.req("GET", "/ui/app.js", origin=None)[1].decode()
        self.assertIn('api("GET", "models?track="', js)
        self.assertIn("cfg.downstreams", js)

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

    def save_night(self):
        self.assertEqual(self.js("POST", "/ui/api/presets", {"name": "night", "tagline": "late shifts"}),
                         (201, {"ok": True}))

    def export(self, qs=""):
        r, d = self.req("GET", "/ui/api/presets/my%20setup/export" + qs)
        return r, json.loads(d.decode())

    def test_presets_save_switch_delete(self):
        self.assertEqual(self.js("POST", "/ui/api/presets", {"name": "", "tagline": ""})[0], 400)
        self.save_night()
        st = self.js("GET", "/ui/api/state")[1]
        self.assertEqual([p["name"] for p in st["presets"]], ["my setup", "night"])
        self.assertEqual([p["active"] for p in st["presets"]], [True, False])
        self.assertEqual(st["presets"][0]["summary"]["downstreams"], ["valar"])
        self.assertEqual(st["presets"][0]["missing_keys"], [])  # the key banner reads the active row's
        self.assertEqual(self.js("POST", "/ui/api/presets/my%20setup/activate")[0], 200)  # already active: a no-op
        self.assertEqual(self.js("POST", "/ui/api/presets/night/activate")[0], 200)
        self.assertEqual(self.js("GET", "/ui/api/state")[1]["preset"], "night")
        s, j = self.js("DELETE", "/ui/api/presets/night")
        self.assertEqual((s, j["error"]), (400, "night is active; switch to another preset first"))
        self.js("POST", "/ui/api/presets/my%20setup/activate")
        self.assertEqual(self.js("PUT", "/ui/api/presets/my%20setup", {"name": "day", "tagline": "t"})[0], 200)
        self.assertEqual(self.js("GET", "/ui/api/state")[1]["preset"], "day")  # the active one followed the rename
        self.assertEqual(self.js("PUT", "/ui/api/presets/nope", {"name": "x", "tagline": ""})[0], 404)
        self.assertEqual(self.js("DELETE", "/ui/api/presets/nope")[0], 404)
        self.assertEqual(self.js("DELETE", "/ui/api/presets/night"), (200, {"ok": True}))
        self.assertEqual([p["name"] for p in self.js("GET", "/ui/api/state")[1]["presets"]], ["day"])

    def test_switch_bumps_rev(self):
        self.save_night()
        st = self.js("GET", "/ui/api/state")[1]
        self.js("POST", "/ui/api/presets/night/activate")
        s, j = self.js("PUT", "/ui/api/config", {**st["config"], "rev": st["config_rev"]})
        self.assertEqual((s, j["error"]), (409, "the config changed elsewhere; reloaded"))

    def test_export_has_no_keys_or_session_lines(self):
        self.app.keys.set("valar", self.v.url, "VK")
        self.set_settings({"when": "session is s1", "downstream": "valar", "tier": None, "set": "window", "value": "priority"})
        r, j = self.export()
        self.assertEqual(r.status, 200)
        text = json.dumps(j)
        self.assertNotIn("VK", text)
        self.assertNotIn("session is", text)
        self.assertEqual(j["signaller_preset"], 1)
        self.assertEqual(j["name"], "my setup")
        r, j = self.export("?name=Shared&tagline=for%20a%20friend")
        self.assertEqual((j["name"], j["tagline"]), ("Shared", "for a friend"))
        s, j = self.js("GET", "/ui/api/presets/my%20setup/export?name=" + "x" * 61)  # a bad override answers with the reason
        self.assertEqual((s, j["error"]), (400, "a preset name must be 1–60 characters"))
        r, j = self.export("?name=Shared&tagline=")  # a blank value means blank, not "the stored one again"
        self.assertEqual((j["name"], j["tagline"]), ("Shared", ""))
        s, j = self.js("GET", "/ui/api/presets/my%20setup/export?name=")
        self.assertEqual((s, j["error"]), (400, "a preset name must be 1–60 characters"))

    def test_preview_and_import(self):
        self.save_night()
        self.js("POST", "/ui/api/presets/night/activate")
        r, file = self.export()  # a stored preset's config, not the live one
        s, j = self.js("POST", "/ui/api/presets/preview", {**file, "name": "fresh"})
        self.assertFalse(j["taken"])
        s, j = self.js("POST", "/ui/api/presets/preview", file)  # its own name is already used
        self.assertTrue(j["taken"])
        s, j = self.js("POST", "/ui/api/presets", {"name": "imported", "tagline": "", "config": file["config"]})
        self.assertEqual((s, j), (201, {"ok": True}))
        st = self.js("GET", "/ui/api/state")[1]
        self.assertEqual(st["preset"], "night")  # an import never activates
        self.assertEqual([(p["name"], p["active"]) for p in st["presets"]],
                         [("my setup", False), ("night", True), ("imported", False)])

    def test_preview_refuses_bad_files(self):
        s, j = self.js("POST", "/ui/api/presets/preview", {})
        self.assertEqual((s, j["error"]), (400, "this isn't a Signaller preset"))
        r, file = self.export()
        s, j = self.js("POST", "/ui/api/presets/preview", {**file, "signaller_preset": 2})
        self.assertEqual((s, j["error"]), (400, "made by a newer Signaller"))
        cfg = self.js("GET", "/ui/api/state")[1]["config"]
        bad = {**cfg, "rules": cfg["rules"] + [{"name": "bad", "kind": "dsl", "enabled": True,
                                               "text": 'when text contains "x" go nowhere', "builtin": None}]}
        s, j = self.js("POST", "/ui/api/presets/preview",
                       {"signaller_preset": 1, "name": "x", "tagline": "", "exported": "2026-01-01", "config": bad})
        self.assertEqual(s, 400)
        self.assertIn("nowhere", j["error"])

    def test_preview_lets_a_bad_name_be_fixed_before_add(self):
        r, file = self.export()
        s, j = self.js("POST", "/ui/api/presets/preview", {**file, "name": "preview"})
        self.assertEqual((s, j["name"]), (200, "preview"))  # shown, so the panel can rename it
        s, j = self.js("POST", "/ui/api/presets", {"name": "preview", "tagline": "", "config": j["config"]})
        self.assertEqual((s, j["error"]), (400, "'preview' is a reserved preset name"))

    def test_presets_non_object_config_is_400_with_a_reason(self):
        for bad in ("a string", ["a", "list"], 5):
            s, j = self.js("POST", "/ui/api/presets", {"name": "x", "tagline": "", "config": bad})
            self.assertEqual((s, j["error"]), (400, "a preset's config must be an object"), bad)
        s, j = self.js("POST", "/ui/api/presets", {"name": "x", "tagline": ""})  # no config: the current setup
        self.assertEqual(s, 201)

    def test_export_refuses_a_broken_config(self):
        self.save_night()
        self.app.presets["presets"][1]["config"] = {"version": 99, "downstreams": "oops"}
        s, j = self.js("GET", "/ui/api/presets/night/export")
        self.assertEqual(s, 400)
        self.assertIn("broken", j["error"])


def serve(do_GET):
    """A throwaway upstream whose GET does what the test says."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), type("H", (BaseHTTPRequestHandler,), {"do_GET": do_GET, "log_message": lambda *a: None}))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


PRICED = json.dumps({"data": [{"id": "fast-model", "pricing": [
    {"tier": "realtime", "input_per_1m_tokens": 2, "cached_input_per_1m_tokens": 0.2, "output_per_1m_tokens": 9}]}]}).encode()
ONE = b'{"data":[{"id":"deepseek-v4.1-flash"}]}'


class DownstreamApiTest(Fixture):
    req, js = ConsoleTest.req, ConsoleTest.js

    def add(self, id="lemmi", url=None, key="LMK", **kw):
        self.l.models = self.l.models if self.l.models != b"{}" else ONE
        d = {"id": id, "name": id.title(), "url": url or self.l.url + "/v1/", "models": dict.fromkeys(MODEL_TIERS, "deepseek-v4.1-flash"),
             **({"key": key} if key else {}), **kw}
        return self.js("POST", "/ui/api/downstreams", d)

    def cfg(self):
        st = self.js("GET", "/ui/api/state")[1]
        return {**st["config"], "rev": st["config_rev"]}  # what the page sends: its config plus the rev it rendered

    def keys_file(self):
        with open(os.path.join(self.dir, "downstreams.keys.json")) as f:
            return json.load(f)

    def test_add_probe_delete_downstream(self):
        self.l.models = ONE
        s, j = self.js("POST", "/ui/api/downstreams/probe", {"url": self.l.url + "/v1", "key": "LMK"})
        self.assertEqual((s, j), (200, {"ok": True, "reason": None, "models": ["deepseek-v4.1-flash"], "kind": "generic"}))
        self.assertEqual(self.l.seen[-1][:2], ("GET", "/v1/models"))
        self.assertEqual(self.l.seen[-1][2]["Authorization"], "Bearer LMK")
        s, j = self.add()
        self.assertEqual(s, 201, j)
        self.assertEqual((j["id"], j["kind"], j["url"], j["windows"]), ("lemmi", "generic", self.l.url, None))
        self.assertNotIn("key", j)
        st = self.js("GET", "/ui/api/state")[1]
        self.assertEqual(st["downstreams"]["lemmi"], {"key_set": True, "url": self.l.url[7:]})
        self.assertEqual(st["config"]["doubling"]["shadow"]["lemmi"], None)
        self.assertEqual(self.js("GET", "/ui/api/models?track=lemmi")[1], {"models": ["deepseek-v4.1-flash"]})
        self.assertEqual(self.js("PUT", "/ui/api/lever", {"session": "s1", "lever": "lemmi"})[1]["lever"], "lemmi")
        self.js("PUT", "/ui/api/lever", {"session": "s1", "lever": None})
        self.assertEqual(self.js("PUT", "/ui/api/config", {**self.cfg(), "lever": "lemmi"})[0], 200)
        s, j = self.js("DELETE", "/ui/api/downstreams/lemmi")
        self.assertEqual(s, 409)
        self.assertIn("the lever", j["references"])
        self.assertIn("the lever", j["error"])
        self.assertEqual(self.js("PUT", "/ui/api/config", {**self.cfg(), "lever": "rules"})[0], 200)
        self.assertEqual(self.js("DELETE", "/ui/api/downstreams/lemmi"), (200, {"ok": True}))
        st = self.js("GET", "/ui/api/state")[1]
        self.assertNotIn("lemmi", st["downstreams"])
        self.assertNotIn("lemmi", st["config"]["doubling"]["shadow"])
        self.assertNotIn("lemmi", self.keys_file())
        self.assertNotIn("lemmi", json.load(open(os.path.join(self.dir, "models.json"))))  # a re-added lemmi mustn't show this one's list
        self.assertEqual(self.js("DELETE", "/ui/api/downstreams/lemmi")[0], 404)

    def test_probe_detects_valar(self):
        self.l.models = PRICED
        s, j = self.js("POST", "/ui/api/downstreams/probe", {"url": self.l.url, "key": "x"})
        self.assertEqual((j["ok"], j["kind"], j["models"]), (True, "valar", ["fast-model"]))
        with mock.patch("console.fetch_models", side_effect=OSError("down")):  # by host, even unreachable
            s, j = self.js("POST", "/ui/api/downstreams/probe", {"url": "https://api.valarhq.ai/v1", "key": "x"})
        self.assertEqual((s, j["ok"], j["kind"], j["models"]), (200, False, "valar", []))

    def test_probe_uses_stored_key(self):
        self.assertEqual(self.add(key="sk-STORED")[0], 201)
        self.js("POST", "/ui/api/downstreams/probe", {"url": self.l.url, "id": "lemmi"})  # an id never pulls the stored key
        self.assertNotIn("sk-STORED", self.l.seen[-1][2]["Authorization"])
        self.js("POST", "/ui/api/downstreams/probe", {"url": self.l.url, "key": "sk-TYPED"})
        self.assertEqual(self.l.seen[-1][2]["Authorization"], "Bearer sk-TYPED")
        self.assertEqual(self.add(id="bad", url="lemmi.example")[0], 400)  # no scheme
        self.assertEqual(self.js("PUT", "/ui/api/downstreams/lemmi/key", {"key": "sk-NEW"}), (200, {"key_set": True}))
        self.assertEqual(self.app.keys.get("lemmi", self.l.url + "/v1/"), "sk-NEW")
        self.assertEqual(self.js("PUT", "/ui/api/downstreams/lemmi/key", {"key": ""})[0], 400)
        self.assertEqual(self.js("PUT", "/ui/api/downstreams/nope/key", {"key": "k"})[0], 404)
        self.assertEqual(self.req("PUT", "/ui/api/downstreams/lemmi/key", {"key": "k"}, origin=None)[0].status, 403)
        self.assertEqual(self.req("DELETE", "/ui/api/downstreams/lemmi", origin=None)[0].status, 403)
        self.assertEqual(self.req("POST", "/ui/api/downstreams/probe", {"url": self.l.url}, origin="http://evil.com")[0].status, 403)

    def test_no_key_in_any_response(self):
        self.assertEqual(self.add(key="sk-SECRET")[0], 201)
        out = [self.req("GET", "/ui/api/state")[1]]
        out.append(self.req("PUT", "/ui/api/config", self.cfg())[1])
        out.append(self.req("GET", "/ui/api/models?track=lemmi")[1])
        out.append(self.req("POST", "/ui/api/downstreams/probe", {"url": self.l.url, "id": "lemmi"})[1])
        out.append(self.req("PUT", "/ui/api/lever", {"session": "s1", "lever": "lemmi"})[1])
        out.append(self.req("GET", "/ui/api/lever?session=s1")[1])
        r, d = self.req("POST", "/ui/api/downstreams", {"id": "Bad", "name": "x", "url": self.l.url, "models": {}, "key": "sk-SECRET"})
        self.assertEqual(r.status, 400)
        out.append(d)
        r, d = self.req("DELETE", "/ui/api/downstreams/lemmi")
        self.assertEqual(r.status, 409)
        out.append(d)
        self.set_lemmi_url("http://127.0.0.1:1")
        out.append(self.req("GET", "/ui/api/models?track=lemmi")[1])
        out.append(self.req("POST", "/ui/api/downstreams/probe", {"url": "http://127.0.0.1:1", "id": "lemmi"})[1])
        for d in out:
            self.assertNotIn(b"sk-SECRET", d)
        with open(os.path.join(self.dir, "console.json")) as f:
            self.assertNotIn("sk-SECRET", f.read())
        self.assertNotIn("sk-SECRET", self.err.getvalue())

    def set_lemmi_url(self, url):
        cfg = self.cfg()
        next(d for d in cfg["downstreams"] if d["id"] == "lemmi")["url"] = url
        self.assertEqual(self.js("PUT", "/ui/api/config", cfg)[0], 200)

    def test_delete_blocked_by_session_lever(self):
        self.add()
        self.js("PUT", "/ui/api/lever", {"session": "s1", "lever": "lemmi"})
        s, j = self.js("DELETE", "/ui/api/downstreams/lemmi")
        self.assertEqual((s, j["references"]), (409, ["session s1's lever"]))
        self.assertIn("lemmi", self.js("GET", "/ui/api/state")[1]["downstreams"])
        self.js("PUT", "/ui/api/lever", {"session": "s1", "lever": None})
        self.assertEqual(self.js("DELETE", "/ui/api/downstreams/lemmi")[0], 200)

    def test_delete_blocked_by_auto_execution(self):
        self.add()
        self.app.routing = replace(self.app.routing, auto=AutoSettings(execution="lemmi"))
        s, j = self.js("DELETE", "/ui/api/downstreams/lemmi")
        self.assertEqual((s, j["references"]), (409, ["auto execution"]))
        self.assertIn("lemmi", self.js("GET", "/ui/api/state")[1]["downstreams"])

    def test_probe_bad_endpoints(self):
        def slow(h):
            time.sleep(10)
        def moved(h):
            h.send_response(302)
            h.send_header("Location", self.l.url + "/v1/models")
            h.end_headers()
        def html(h):
            h.send_response(200)
            h.send_header("Content-Type", "text/html")
            h.end_headers()
            h.wfile.write(b"<html>hi</html>")
        for f, frag in ((slow, "timed out"), (moved, "302"), (html, "not a model list")):
            srv, url = serve(f)
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
            t = time.time()
            s, j = self.js("POST", "/ui/api/downstreams/probe", {"url": url, "key": "k"}) if f is not slow else \
                self.js_slow("POST", "/ui/api/downstreams/probe", {"url": url, "key": "k"})
            self.assertLess(time.time() - t, 6)
            self.assertEqual((s, j["ok"], j["models"], j["kind"]), (200, False, [], "generic"), f.__name__)
            self.assertIn(frag, j["reason"], f.__name__)
        self.assertEqual(self.l.seen, [])  # the redirect was not followed
        self.assertEqual(self.js("POST", "/ui/api/downstreams/probe", {"url": "file:///etc/passwd"})[1]["ok"], False)
        self.assertEqual(self.js("POST", "/ui/api/downstreams/probe", {"url": 5})[0], 400)

    def js_slow(self, method, path, body):
        c = HTTPConnection("127.0.0.1", self.port, timeout=8)
        c.request(method, path, json.dumps(body), {"Origin": "http://127.0.0.1:%d" % self.port})
        r = c.getresponse()
        return r.status, json.loads(r.read())

    def test_lever_modes(self):
        self.assertEqual(self.js("GET", "/ui/api/lever?session=s1")[1]["modes"], ["rules", "auto", "anthropic", "valar"])
        self.add()
        s, j = self.js("PUT", "/ui/api/lever", {"session": "s1", "lever": "lemmi"})
        self.assertEqual((s, j["lever"], j["modes"]), (200, "lemmi", ["rules", "auto", "anthropic", "valar", "lemmi"]))
        s, j = self.js("PUT", "/ui/api/lever", {"session": "s1", "lever": "local"})
        self.assertEqual(s, 400)
        self.assertIn("lemmi", j["error"])

    def test_post_sets_server_side_fields(self):
        self.l.models = PRICED
        s, j = self.add("fast", prices={"in": 1, "out": 1, "cache_read": 0, "cache_write": 0})
        self.assertEqual(s, 201, j)
        self.assertEqual((j["kind"], j["windows"], j["color"], j["url"], j["prices"]),
                         ("valar", dict.fromkeys(MODEL_TIERS, "asap"), "#1F8A5B", self.l.url, None))
        self.l.models = ONE
        s, j = self.add(prices={"in": 1, "out": 2, "cache_read": 0.1, "cache_write": 0})
        self.assertEqual((s, j["kind"], j["color"], j["windows"], j["prices"]["out"]), (201, "generic", "#6B5BD6", None, 2.0))
        self.assertEqual(self.js("DELETE", "/ui/api/downstreams/fast")[0], 200)
        self.assertEqual(self.add("third", key=None)[1]["color"], "#1F8A5B")  # first free colour
        self.assertFalse(self.js("GET", "/ui/api/state")[1]["downstreams"]["third"]["key_set"])
        for bad, frag in (("Bad", "lowercase"), ("rules", "reserved"), ("lemmi", "already")):
            s, j = self.add(bad)
            self.assertEqual(s, 400, bad)
            self.assertIn(frag, j["error"], bad)
        self.assertEqual(self.add("nomodels", models={"opus": "x"})[0], 400)
        self.assertEqual(self.add("noname", name="")[0], 400)
        for i in range(3):
            self.assertEqual(self.add("d%d" % i)[0], 201)
        s, j = self.add("seventh")
        self.assertEqual((s, j["error"]), (409, "at most 6 downstreams"))

    def test_put_config_downstream_changes(self):
        self.add()
        cfg = self.cfg()
        lem = lambda c: next(d for d in c["downstreams"] if d["id"] == "lemmi")
        for k, v in (("kind", "valar"), ("color", "#000000"), ("id", "lemmy")):
            c = self.cfg()
            lem(c)[k] = v
            if k == "kind":
                lem(c)["windows"] = dict.fromkeys(MODEL_TIERS, "asap")
            if k == "id":
                c["doubling"]["shadow"]["lemmy"] = c["doubling"]["shadow"].pop("lemmi")
            s, j = self.js("PUT", "/ui/api/config", c)
            self.assertEqual(s, 400, k)
            self.assertIn(k if k != "id" else "add", j["error"], k)
        c = self.cfg()
        lem(c)["name"] = "Lemmi 2"
        self.assertEqual(self.js("PUT", "/ui/api/config", c)[0], 200)  # other edits go through
        self.js("PUT", "/ui/api/lever", {"session": "s1", "lever": "lemmi"})
        c = self.cfg()
        c["downstreams"] = [d for d in c["downstreams"] if d["id"] != "lemmi"]
        del c["doubling"]["shadow"]["lemmi"]
        s, j = self.js("PUT", "/ui/api/config", c)  # removal is Delete's job, never a config PUT
        self.assertEqual(s, 400)
        self.assertIn("Delete", j["error"])
        self.assertIn("lemmi", [d["id"] for d in self.cfg()["downstreams"]])
        self.assertIn("lemmi", self.keys_file())

    def test_price_change_reprices(self):
        self.add()
        now = time.time()
        self.app.store.record_request({"id": 901, "ts": now, "track": "lemmi", "status": 200, "model_out": "deepseek-v4.1-flash",
                                       "in_tok": 1_000_000, "out_tok": 0, "cache_read_tok": 0, "cache_write_tok": 0})
        self.app.store.close()
        self.app.store.__init__(self.app.store.path)
        self.assertIsNone(self.app.store.request(901)["cost"])
        c = self.cfg()
        next(d for d in c["downstreams"] if d["id"] == "lemmi")["prices"] = {"in": 3, "out": 1, "cache_read": 0, "cache_write": 0}
        self.assertEqual(self.js("PUT", "/ui/api/config", c)[0], 200)
        self.assertAlmostEqual(self.app.store.request(901)["cost"], 3.0)

    def test_verdict_any_downstream(self):
        self.add()
        self.app.store.record_turn({"id": "t3", "ts": time.time(), "preview": "p", "verdict": None})
        self.app.store.close()
        self.app.store.__init__(self.app.store.path)
        self.assertEqual(self.js("PUT", "/ui/api/turns/t3/verdict", {"verdict": "lemmi"})[0], 200)
        self.assertEqual(self.js("PUT", "/ui/api/turns/t3/verdict", {"verdict": "local"})[0], 400)
        self.assertEqual(self.js("GET", "/ui/api/state")[1]["stats"]["tally"]["lemmi"], 1)


class MigrationBannerTest(unittest.TestCase):
    def test_banner_names_switched_off_rules_and_migration_saved_once(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        with open(os.path.join(os.path.dirname(__file__), "fixtures", "console_v2.json")) as f:
            o = json.load(f)
        o["rules"].insert(1, {"name": "Mine", "kind": "dsl", "enabled": True, "text": "when tag is x go local", "builtin": None})
        with open(os.path.join(d, "console.json"), "w") as f:
            json.dump(o, f)
        with contextlib.redirect_stderr(io.StringIO()):
            app = App(Config("127.0.0.1", 0, "http://a"), d)
            app.store.close()
            self.assertIn("Mine", app.banner)
            with open(os.path.join(d, "console.json")) as f:
                self.assertEqual(json.load(f)["version"], 4)
            again = App(Config("127.0.0.1", 0, "http://a"), d)
            again.store.close()
        self.assertIsNone(again.banner)
        self.assertFalse(next(r for r in again.routing.rules if r.name == "Mine").enabled)


class TrainTest(Fixture):
    """The training job's API, against a fixture app whose Claude dir is a temp folder seeded with
    synthetic transcripts (never the real ~/.claude/projects)."""

    def setUp(self):
        self.claude = tempfile.mkdtemp(prefix="train-claude-")
        self.addCleanup(shutil.rmtree, self.claude, True)
        self._old_ccd = os.environ.pop("CLAUDE_CONFIG_DIR", None)
        os.environ["CLAUDE_CONFIG_DIR"] = self.claude
        def _restore():
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            if self._old_ccd is not None:
                os.environ["CLAUDE_CONFIG_DIR"] = self._old_ccd
        self.addCleanup(_restore)
        super().setUp()

    def restart(self):  # a new app on the same config dir: labels on disk must survive it
        self.srv.shutdown()
        self.srv.server_close()
        self.app.store.close()
        cfg = Config("127.0.0.1", 0, self.a.url, (self.v.url, "VK"))
        self.app = App(cfg, self.dir)
        self.app.routing = replace(CLASSIFIER_OFF, downstreams=(replace(VALAR, url=self.v.url),))
        self.srv = make_server(cfg, self.app)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.port = self.srv.server_address[1]

    def seed(self, pdir="proj", sessions=3, turns=2):
        d = os.path.join(self.claude, "projects", pdir)
        os.makedirs(d, exist_ok=True)
        texts = []
        for s in range(sessions):
            events = []
            for t in range(turns):
                if (s + t) % 2 == 0:
                    txt = "please plan the design for feature %d in session %d" % (t, s)
                else:
                    txt = "now fix the failing parser test in module %d of session %d" % (t, s)
                texts.append(txt)
                events += [tu(txt), ta("m%d" % t)]
            write_transcript(d, "sess%d" % s, events)
        return texts

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

    def train_get(self):
        return self.js("GET", "/ui/api/train")[1]

    def wait(self, state, timeout=10):
        t, j = time.time(), None
        while time.time() - t < timeout:
            j = self.train_get()
            if j["state"] == state:
                return j
            time.sleep(0.02)
        self.assertEqual(j["state"], state)

    def plan(self, **patch):
        return self.js("POST", "/ui/api/train/plan", {"projects": ["proj"], "cut": False, **patch})

    def test_train_flow(self):
        texts = self.seed(sessions=30)  # 60 candidates, half planning: enough for the held-out check to judge
        j = self.wait("ready")  # the first GET starts the scan
        self.assertEqual(j["cap"], 5.0)
        self.assertEqual([(p["dir"], p["name"], p["sessions"], p["prompts"]) for p in j["projects"]],
                         [("proj", "proj", 30, 60)])
        s, job = self.plan()
        self.assertEqual(s, 200)
        self.assertEqual((job["items"], job["labelled"], job["failed"], job["remaining"]), (60, 0, 0, 60))
        self.assertGreater(job["usd_est"], 0)
        self.assertIsNone(job["cut"])
        self.assertFalse(job["cut_applied"])
        self.assertEqual(self.train_get()["state"], "labelling")
        got = []

        def label(items, start):
            return self.js("POST", "/ui/api/train/labels", {
                "labels": [{"id": x["id"], "label": "planning" if (start + i) % 2 == 0 else "execution"}
                           for i, x in enumerate(items)],
                "failed": [], "usage": {"input_tokens": 100, "output_tokens": 5}})
        for _ in range(2):  # n is capped at 50 a batch: two rounds cover all 60
            s, batch = self.js("POST", "/ui/api/train/next", {"n": 50})
            got += batch["items"]
            s, job = label(batch["items"], len(got) - len(batch["items"]))
        self.assertEqual(len(got), 60)
        self.assertEqual({x["text"] for x in got}, set(texts))
        self.assertEqual((s, job["labelled"], job["remaining"]), (200, 60, 0))
        self.assertGreater(job["usd_spent"], 0)
        self.assertEqual(self.js("POST", "/ui/api/train/finish", {})[0], 200)
        j = self.wait("done")
        self.assertIn(j["result"]["verdict"], ("better", "no better"))
        self.assertEqual(self.js("POST", "/ui/api/train/use", {})[0], 200)
        self.assertTrue(self.js("GET", "/ui/api/state")[1]["auto_model"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "training")))
        with open(os.path.join(self.dir, "auto_model.json")) as f:
            self.assertEqual(json.load(f)["format"], "signaller-auto-1")
        self.assertIsNotNone(self.app.auto_model)
        self.assertEqual(self.train_get()["state"], "scanning")  # the job is gone; the rows list again after a use

    def test_train_get_never_serves_text(self):
        texts = self.seed()
        self.plan()
        for path in ("/ui/api/train", "/ui/api/state", "/ui/api/lever?session=s1"):
            _, data = self.req("GET", path)
            for t in texts:
                self.assertNotIn(t.encode(), data, (path, t))

    def test_train_resume_after_restart(self):
        self.seed()
        self.plan()
        s, got = self.js("POST", "/ui/api/train/next", {"n": 3})
        self.assertEqual(len(got["items"]), 3)
        done = {x["id"] for x in got["items"]}
        self.js("POST", "/ui/api/train/labels",
                {"labels": [{"id": i, "label": "planning"} for i in done],
                 "failed": [], "usage": {"input_tokens": 1, "output_tokens": 1}})
        self.restart()
        s, job = self.plan()
        self.assertEqual((s, job["items"], job["labelled"]), (200, 6, 3))
        s, got = self.js("POST", "/ui/api/train/next", {"n": 50})
        self.assertEqual({x["id"] for x in got["items"]} | done,
                         {"sess%d:%d" % (a, b) for a in range(3) for b in range(2)})

    def test_train_next_needs_origin(self):
        self.seed()
        self.plan()
        self.assertEqual(self.req("POST", "/ui/api/train/next", {"n": 5}, origin=None)[0].status, 403)

    def test_train_unreadable_transcript(self):
        self.seed(sessions=2)
        self.plan()
        os.remove(os.path.join(self.claude, "projects", "proj", "sess0.jsonl"))
        s, got = self.js("POST", "/ui/api/train/next", {"n": 50})
        self.assertEqual((s, [x["id"] for x in got["items"]]), (200, ["sess1:0", "sess1:1"]))
        job = self.train_get()["job"]
        self.assertEqual((job["items"], job["failed"]), (4, 2))

    def test_train_bad_label_400(self):
        self.seed()
        self.plan()
        _, got = self.js("POST", "/ui/api/train/next", {"n": 2})
        self.assertEqual(self.js("POST", "/ui/api/train/labels",
                                 {"labels": [{"id": got["items"][0]["id"], "label": "bogus"}], "failed": []})[0], 400)
        self.assertEqual(self.js("POST", "/ui/api/train/labels",
                                 {"labels": [{"id": "nope:9", "label": "planning"}], "failed": []})[0], 400)
        self.assertEqual(self.js("POST", "/ui/api/train/next", {"n": 0})[0], 400)

    def test_train_no_job_400s(self):
        self.assertEqual(self.js("POST", "/ui/api/train/next", {"n": 5})[0], 400)
        self.assertEqual(self.js("POST", "/ui/api/train/labels", {"labels": []})[0], 400)
        self.assertEqual(self.js("POST", "/ui/api/train/finish", {})[0], 400)
        self.assertEqual(self.js("POST", "/ui/api/train/use", {})[0], 409)
        self.assertEqual(self.js("POST", "/ui/api/train/plan", {"projects": ["nope"]})[0], 400)
        self.assertEqual(self.js("POST", "/ui/api/train/discard", {})[0], 200)  # idempotent

    def test_train_bad_origin_on_plan(self):
        self.seed()
        self.assertEqual(self.req("POST", "/ui/api/train/plan", {"projects": ["proj"]}, origin=None)[0].status, 403)

    def test_train_held_out_transcript_deleted_still_done(self):
        self.seed()  # 3 sessions, 2 candidates each
        self.plan()
        _, got = self.js("POST", "/ui/api/train/next", {"n": 50})
        self.js("POST", "/ui/api/train/labels",
                {"labels": [{"id": x["id"], "label": "planning" if i % 2 == 0 else "execution"}
                            for i, x in enumerate(got["items"])], "failed": []})
        _, test = autotrain.split([{"session": "sess%d" % s} for s in range(3)])
        os.remove(os.path.join(self.claude, "projects", "proj", test[0]["session"] + ".jsonl"))
        self.assertEqual(self.js("POST", "/ui/api/train/finish", {})[0], 200)
        j = self.wait("done")  # the vanished held-out transcript is skipped, not a failed job
        self.assertIn(j["result"]["verdict"], ("better", "no better", "too few"))

    def test_train_get_answers_while_scanning(self):
        d = os.path.join(self.claude, "projects", "big")
        os.makedirs(d)
        pad = "x" * 200000
        events = [tu("please plan the design of the cache layer for me"), ta("m1")]
        for i in range(60):  # ~12 MB of tool results: the scan replays it on the worker
            events.append({"type": "user", "uuid": "t%d" % i,
                           "message": {"role": "user", "content": [{"type": "tool_result", "content": pad}]}})
        events += [tu("now fix the failing parser test in module one"), ta("m2")]
        write_transcript(d, "big", events)
        self.js("GET", "/ui/api/train")  # the first GET starts the scan
        t0 = time.time()
        s, j = self.js("GET", "/ui/api/train")  # and never blocks on it
        self.assertEqual((s, time.time() - t0 < 1), (200, True))
        self.assertEqual(j["state"], "scanning")
        self.assertIsNone(j["projects"][0]["prompts"])
        self.wait("ready")
        self.assertEqual(self.train_get()["projects"][0]["prompts"], 2)

    def test_lever_answer_trained_flag(self):
        self.assertFalse(self.js("GET", "/ui/api/lever?session=s1")[1]["trained"])
        with open(os.path.join(self.dir, "auto_model.json"), "w") as f:
            json.dump({"format": "signaller-auto-1", "bias": 0.1, "weights": {"fix": 0.2}}, f)
        self.restart()
        self.assertTrue(self.js("GET", "/ui/api/lever?session=s1")[1]["trained"])

    def test_label_request_goes_to_anthropic_untouched_and_unstored(self):
        self.app.routing = replace(self.app.routing, lever="valar")
        body = json.dumps({"model": "claude-haiku-5-5", "stream": False,
                           "system": autotrain.LABEL_MARKER + "\nAnswer with one word: planning or execution.",
                           "messages": [{"role": "user", "content": "please plan the design of the parser"}]}).encode()
        self.assertEqual(self.post(body, {**HDRS, "X-Claude-Code-Session-Id": "s1"}), (200, b"{}"))
        self.assertEqual(self.v.seen, [])  # whatever the lever says, a marked request never leaves Anthropic
        self.assertEqual(self.a.seen[0][3], body)  # byte for byte: no tag strip, no thinking drop, no rewrite
        self.assertEqual(self.a.seen[0][2]["Authorization"], HDRS["Authorization"])
        self.logged(1)
        self.assertIn('reason="auto training"', self.err.getvalue())
        self.assertIsNone(self.app.auto_for("s1"))  # never the classifier, never auto state
        t0 = time.time()
        while time.time() - t0 < 2 and not self.app.store.request(1):
            time.sleep(0.01)
        row = self.app.store.request(1)
        self.assertEqual((row["track"], row["reason"]), ("anthropic", "auto training"))
        self.assertIsNone(row["prompt_hash"])  # recorded with no prompt text and no preview; the cost is kept
        self.assertIsNone(row["prompt"])
        self.assertEqual(row["preview"], "")

    def test_train_plan_conflict_409(self):
        self.seed(sessions=2)
        self.seed(pdir="proj2", sessions=1)
        self.plan()
        _, got = self.js("POST", "/ui/api/train/next", {"n": 1})
        self.js("POST", "/ui/api/train/labels", {"labels": [{"id": got["items"][0]["id"], "label": "planning"}],
                                                 "failed": [], "usage": {"input_tokens": 1, "output_tokens": 1}})
        s, j = self.js("POST", "/ui/api/train/plan", {"projects": ["proj2"], "cut": False})
        self.assertEqual((s, j["error"]), (409, "a training run is in progress; discard it first"))
        s, job = self.js("POST", "/ui/api/train/plan", {"projects": ["proj"], "cut": False})  # the same: a resume
        self.assertEqual((s, job["labelled"]), (200, 1))

    def test_use_clears_the_unreadable_model_banner(self):
        with open(os.path.join(self.dir, "auto_model.json"), "w") as f:
            f.write("not a model")
        self.restart()
        self.assertIn("couldn't be read", self.app.banner)
        tr = self.app.trainer
        tr.state = "done"
        tr.model = {"format": "signaller-auto-1", "bias": 0.0, "weights": {"fix": 0.5}}
        tr.result = {"verdict": "better"}
        self.app.use_trained_model()
        self.assertNotIn("couldn't be read", self.app.banner)  # the installed model replaces the complaint
        self.assertIsNotNone(self.app.auto_model)


class ListModelsRedirectTest(unittest.TestCase):
    def test_redirect_not_followed_and_key_not_leaked(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from console import fetch_models
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
                fetch_models("http://127.0.0.1:%d" % a.server_address[1], "k")
            self.assertEqual(auth, [])
        finally:
            a.shutdown(); b.shutdown(); a.server_close(); b.server_close()


class HoistTolerantTest(unittest.TestCase):
    def test_malformed_shapes(self):
        from signaller import hoist_inline_tools
        good = {"type": "tool_definition", "definition": {"name": "g", "x": 1}}
        req = {"tools": ["junk", {"name": "t"}], "messages": [{"role": "user", "content": [
            {"type": "tool_addition", "tool": "junk"}, {"type": "tool_addition", "tool": {"type": "tool_definition", "definition": "s"}},
            {"type": "tool_addition", "tool": {"type": "tool_definition", "definition": {"x": 1}}},
            {"type": "tool_addition", "tool": good}]}]}
        self.assertTrue(hoist_inline_tools(req))
        self.assertEqual(req["tools"], ["junk", {"name": "t"}, {"name": "g", "x": 1}])
        self.assertEqual(req["messages"], [])


class StripAdvisorTest(unittest.TestCase):
    def test_drops_definition_calls_and_results(self):
        from signaller import strip_advisor
        req = {"tools": [{"name": "Bash"}, {"type": "advisor_20260301", "name": "advisor"}], "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "text", "text": "x"}, {"type": "server_tool_use", "id": "a", "name": "advisor"},
                                              {"type": "advisor_tool_result", "tool_use_id": "a"}]},
            {"role": "assistant", "content": [{"type": "advisor_tool_result", "tool_use_id": "b"}]},
            {"role": "user", "content": [{"type": "server_tool_use", "id": "w", "name": "web_search"}]}]}
        self.assertTrue(strip_advisor(req))
        self.assertEqual(req["tools"], [{"name": "Bash"}])
        self.assertEqual([m["content"] for m in req["messages"]],
                         ["hi", [{"type": "text", "text": "x"}], [{"type": "server_tool_use", "id": "w", "name": "web_search"}]])
        self.assertFalse(strip_advisor({"tools": [{"name": "Bash"}], "messages": [{"role": "user", "content": "hi"}]}))


if __name__ == "__main__":
    unittest.main()
