import hashlib
import json
import os
import unittest
from dataclasses import replace

from autophase import AutoSettings
from signaller import load_config
from overrides import Setting
from routing import (DEFAULT_CONFIG, MODEL_TIERS, ClassResult, Downstream, Features, Rule, latest_tool_results, strip_lead_tags, is_routed, session_of, config_from_json,
                     config_to_json, downstream, exec_track, references, route, tier_of, track_model, track_window, tracks, valid_id)
from rulelang import parse_cond


def body(messages, model="claude-opus-5-5"):
    return json.dumps({"model": model, "messages": messages}).encode()


def user(content):
    return {"role": "user", "content": content}


def assistant(content):
    return {"role": "assistant", "content": content}


def text(t):
    return [{"type": "text", "text": t}]


def rules_with(cfg, **enabled):
    """Copy of cfg with rules switched by builtin name, e.g. classifier=False."""
    return replace(cfg, rules=tuple(replace(r, enabled=enabled.get(r.builtin, r.enabled)) for r in cfg.rules))


OFF = rules_with(DEFAULT_CONFIG, classifier=False)


def boom(f, s):
    raise AssertionError("classifier must not be called")


def run(messages, model="claude-opus-5-5", cfg=OFF, classify=boom):
    d = route(cfg, Features.from_body(body(messages, model)), classify)
    return d.track, d.reason


def route_(messages, model="claude-opus-5-5"):
    return run(messages, model)


class RouteTests(unittest.TestCase):
    def test_tag_execution_in_latest_human(self):
        self.assertEqual(route_([user(text("#execution do it"))]), ("valar", "rule:Tags in your message:2"))

    def test_tag_plan_beats_haiku(self):
        self.assertEqual(route_([user(text("#planning"))], "claude-haiku-4-5"), ("anthropic", "rule:Tags in your message:1"))

    def test_tag_execution_beats_opus(self):
        self.assertEqual(route_([user(text("#execution"))]), ("valar", "rule:Tags in your message:2"))

    def test_both_tags_anthropic(self):
        self.assertEqual(route_([user(text("#execution #planning go"))]), ("anthropic", "rule:Tags in your message:1"))

    def test_tag_case_insensitive_whole_word(self):
        self.assertEqual(route_([user(text("#EXECUTION"))]), ("valar", "rule:Tags in your message:2"))
        self.assertEqual(route_([user(text("#executions"))]), ("anthropic", "rule:Model the request asked for:1"))

    def test_tag_in_older_message_ignored(self):
        msgs = [user(text("#execution")), assistant(text("ok")), user(text("next"))]
        self.assertEqual(route_(msgs), ("anthropic", "rule:Model the request asked for:1"))

    def test_tool_result_only_message_skipped(self):
        msgs = [
            user(text("#execution go")),
            assistant([{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]),
            user([{"type": "tool_result", "tool_use_id": "t1", "content": "done"}]),
        ]
        self.assertEqual(route_(msgs), ("valar", "rule:Tags in your message:2"))

    def test_system_reminder_block_ignored(self):
        blocks = [
            {"type": "text", "text": "<system-reminder>#execution</system-reminder>"},
            {"type": "text", "text": "hello"},
        ]
        self.assertEqual(route_([user(blocks)]), ("anthropic", "rule:Model the request asked for:1"))

    def test_string_content(self):
        self.assertEqual(route_([user("#execution fix")]), ("valar", "rule:Tags in your message:2"))

    def test_tag_only_at_start(self):
        for t in ("fix #execution", "do it #execution", "see log:\n#execution", "#123 #execution"):
            self.assertEqual(route_([user(t)]), ("anthropic", "rule:Model the request asked for:1"), t)
        self.assertEqual(route_([user("  #Execution: go")]), ("valar", "rule:Tags in your message:2"))
        self.assertEqual(route_([user("#local #execution go")]), ("valar", "rule:Tags in your message:2"))

    def test_tier(self):
        m = [user("hi")]
        self.assertEqual(route_(m), ("anthropic", "rule:Model the request asked for:1"))
        self.assertEqual(route_(m, "claude-sonnet-5-5"), ("valar", "rule:Model the request asked for:2"))
        self.assertEqual(route_(m, "claude-haiku-5-5"), ("valar", "rule:Model the request asked for:2"))
        self.assertEqual(route_(m, "something-else"), ("valar", "rule:Model the request asked for:2"))
        self.assertEqual(tier_of("Claude-Haiku-5"), "haiku")

    def test_count_tokens_routed(self):
        self.assertTrue(is_routed("POST", "/v1/messages/count_tokens"))
        self.assertTrue(is_routed("POST", "/v1/messages?beta=true"))

    def test_passthrough(self):
        for args in [("GET", "/v1/messages"), ("POST", "/api/oauth/x")]:
            self.assertFalse(is_routed(*args))
        self.assertIsNone(Features.from_body(b"not json"))
        self.assertIsNone(Features.from_body(b"{}"))
        self.assertIsNone(Features.from_body(b"[]"))

    def test_non_string_model_passthrough(self):
        for m in (None, 5, ["x"]):
            b = json.dumps({"model": m, "messages": [user("hi")]}).encode()
            self.assertIsNone(Features.from_body(b))


OPUS_CASES = [
    ([user(text("#execution do it"))], "valar"),
    ([user(text("do it #execution"))], "anthropic"),
    ([user(text("#planning"))], "anthropic"),
    ([user(text("#execution #planning go"))], "anthropic"),
    ([user(text("#execution and #planning"))], "valar"),
    ([user(text("#EXECUTION"))], "valar"),
    ([user(text("#executions"))], "anthropic"),
    ([user("hi")], "anthropic"),
    ([user(text("#execution")), assistant(text("ok")), user(text("next"))], "anthropic"),
    ([user(text("#execution go")),
      assistant([{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]),
      user([{"type": "tool_result", "tool_use_id": "t1", "content": "done"}])], "valar"),
    ([user([{"type": "text", "text": "<system-reminder>#execution</system-reminder>"},
            {"type": "text", "text": "hello"}])], "anthropic"),
    ([user("#execution fix")], "valar"),
]


def cls(label, conf, note=""):
    return lambda f, s: ClassResult(label, conf, note)


class EngineTests(unittest.TestCase):
    def go(self, cfg, msgs=None, model="claude-opus-5-5", classify=boom):
        return route(cfg, Features.from_body(body(msgs or [user("hi")], model)), classify)

    def test_lever_overrides(self):
        d = self.go(replace(DEFAULT_CONFIG, lever="valar"), [user("#plan")])
        self.assertEqual((d.track, d.reason), ("valar", "lever"))
        self.assertEqual(d.explanation, [{"rule": "Lever", "outcome": "sending everything to valar", "decided": True}])

    def test_hour_rule(self):
        cfg = replace(OFF, rules=(Rule("H", "dsl", True, "when hour < 7 go valar"),))
        f = Features.from_body(body([user("hi")]))
        f.hour = 3
        self.assertEqual(route(cfg, f, boom).track, "valar")
        f.hour = 12
        self.assertEqual(route(cfg, f, boom).track, "anthropic")

    def test_first_match_and_explanation(self):
        d = self.go(OFF, [user("#execution")])
        self.assertEqual(d.reason, "rule:Tags in your message:2")
        self.assertEqual([e["outcome"] for e in d.explanation], ["no match", "line 2 matched \u2192 valar (#execution)", "not reached", "not reached"])

    def test_disabled_rule_skipped(self):
        d = self.go(rules_with(OFF, tags=False), [user("#execution")])
        self.assertEqual((d.track, d.reason), ("anthropic", "rule:Model the request asked for:1"))
        self.assertEqual(d.explanation[1]["outcome"], "switched off")

    def test_otherwise_track(self):
        cfg = replace(OFF, otherwise="valar", rules=tuple(replace(r, enabled=False) for r in OFF.rules))
        d = self.go(cfg)
        self.assertEqual((d.track, d.reason), ("valar", "default"))
        self.assertEqual(d.explanation[-1], {"rule": "Otherwise", "outcome": "\u2192 valar", "decided": True})

    def test_classifier_decides_at_threshold(self):
        d = self.go(DEFAULT_CONFIG, model="claude-sonnet-5-5", classify=cls("planning", 0.7))
        self.assertEqual((d.track, d.reason), ("anthropic", "about:planning"))
        self.assertEqual(d.explanation[2]["outcome"], "planning, 70% sure \u2192 anthropic")
        self.assertEqual(d.classified, ClassResult("planning", 0.7, ""))

    def test_classifier_below_threshold_passes(self):
        d = self.go(DEFAULT_CONFIG, model="claude-sonnet-5-5", classify=cls("planning", 0.69))
        self.assertEqual(d.track, "valar")
        self.assertEqual(d.explanation[2]["outcome"], "planning, 69% sure, below 70%")

    def test_classifier_note_passes(self):
        d = self.go(DEFAULT_CONFIG, model="claude-sonnet-5-5", classify=cls(None, None, "timed out after 1.5 s"))
        self.assertEqual(d.explanation[2]["outcome"], "timed out after 1.5 s")
        self.assertEqual(d.reason, "rule:Model the request asked for:2")

    def test_classifier_skipped_on_opus(self):
        d = self.go(DEFAULT_CONFIG)
        self.assertEqual(d.explanation[2]["outcome"], "skipped: main session (opus)")
        self.assertIsNone(d.classified)

    def test_label_on_opus_never_classifies(self):
        user_rule = Rule("U", "dsl", True, "when label is coding go valar", None)
        cfg = replace(DEFAULT_CONFIG, rules=(user_rule,) + DEFAULT_CONFIG.rules)
        d = self.go(cfg)
        self.assertEqual(d.track, "anthropic")

    def test_classify_called_once_per_route(self):
        r = Rule("U", "dsl", True, "when label is coding go valar", None)
        cfg = replace(DEFAULT_CONFIG, rules=(r,) + DEFAULT_CONFIG.rules[2:])
        calls = []
        def c(f, s):
            calls.append(1)
            return ClassResult("coding", 0.9, "")
        d = self.go(cfg, model="claude-sonnet-5-5", classify=c)
        self.assertEqual((d.track, len(calls)), ("valar", 1))
        d = self.go(replace(cfg, rules=(r, cfg.rules[1])), model="claude-sonnet-5-5", classify=c)
        self.assertEqual(len(calls), 2)

    def test_defaults_classifier_on_for_opus(self):
        for msgs, want in OPUS_CASES:
            self.assertEqual(self.go(DEFAULT_CONFIG, msgs).track, want, msgs)
            self.assertEqual(self.go(OFF, msgs).track, want, msgs)

    def test_lone_surrogate_does_not_crash(self):
        raw = b'{"model":"m","messages":[{"role":"user","content":"\\ud800 hi"}]}'
        self.assertIsNotNone(Features.from_body(raw))

    def test_subagent_and_tokens_fields(self):
        sys_ = [{"type": "text", "text": "x cc_is_subagent=true y"}, {"type": "text", "text": "z"}]
        long = "a" * 25000
        raw = json.dumps({"model": "claude-opus-5-5", "system": sys_, "messages": [user(long)]}).encode()
        f = Features.from_body(raw)
        self.assertIs(f.subagent, True)
        self.assertEqual(f.tokens, len(raw) // 4)
        self.assertEqual(len(f.text), 20000)
        self.assertEqual(f.turn_key, hashlib.sha256(long.encode()).hexdigest())
        self.assertIs(Features.from_body(body([user("hi")])).subagent, False)
        raw = json.dumps({"model": "m", "system": "cc_is_subagent=true", "messages": []}).encode()
        self.assertIs(Features.from_body(raw).subagent, True)

    def test_main_thread_vs_subagent_rule(self):
        # first system block as captured from Claude Code 2.1.316 (main thread, then a haiku subagent)
        main = "x-anthropic-billing-header: cc_version=2.1.316.f82; cc_entrypoint=sdk-cli;"
        sub = "x-anthropic-billing-header: cc_version=2.1.316.69c; cc_entrypoint=sdk-cli; cc_is_subagent=true;"
        rule = Rule("Main thread stays home", "dsl", True, "when not subagent go anthropic\nwhen subagent go valar")
        cfg = replace(OFF, rules=(rule,))
        for first, track in ((main, "anthropic"), (sub, "valar")):
            raw = json.dumps({"model": "claude-haiku-5-5", "system": [{"type": "text", "text": first}], "messages": [user("hi")]}).encode()
            self.assertEqual(route(cfg, Features.from_body(raw), boom).track, track)


INTENT = [
    ("claude-opus-5-5", "hi", "anthropic", "Model the request asked for:1"),
    ("claude-fable-5-1", "hi", "anthropic", "Model the request asked for:1"),
    ("claude-sonnet-5-5", "hi", "valar", "Model the request asked for:2"),
    ("claude-haiku-5-5", "hi", "valar", "Model the request asked for:2"),
    ("zai-org/GLM-5.3", "hi", "valar", "Model the request asked for:2"),
    ("claude-opus-5-5", "#execution go", "valar", "Tags in your message:2"),
    ("claude-haiku-5-5", "#planning x", "anthropic", "Tags in your message:1"),
    ("claude-opus-5-5", "#execution #planning", "anthropic", "Tags in your message:1"),
    ("claude-opus-5-5", "#plan x", "anthropic", "Model the request asked for:1"),
]


class ThreeTrackTests(unittest.TestCase):
    def test_tier_fable(self):
        self.assertEqual(tier_of("claude-fable-5-1"), "fable")
        self.assertEqual(tier_of("zai-org/GLM-5.3"), "other")

    def test_default_rulebook_intent(self):
        for model, t, track, why in INTENT:
            d = route(DEFAULT_CONFIG, Features.from_body(body([user(t)], model)), cls(None, None))
            self.assertEqual((d.track, d.reason), (track, "rule:" + why), (model, t))

    def test_servertool_goes_anthropic(self):
        ws = {"model": "claude-sonnet-5-5", "messages": [user("Perform a web search for the query: x")],
              "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}]}
        f = Features.from_body(json.dumps(ws).encode())
        self.assertIs(f.servertool, True)
        d = route(DEFAULT_CONFIG, f, boom)
        self.assertEqual((d.track, d.reason), ("anthropic", "rule:Tools only Anthropic can run"))
        plain = {**ws, "tools": [{"name": "Bash", "input_schema": {}}, {"type": "custom", "name": "X", "input_schema": {}}]}
        self.assertIs(Features.from_body(json.dumps(plain).encode()).servertool, False)
        check = {"model": "claude-sonnet-5", "messages": [user('<transcript>\n{"user":"hi"}\n</transcript>')]}
        self.assertIs(Features.from_body(json.dumps(check).encode()).servertool, True)
        self.assertEqual(route(replace(DEFAULT_CONFIG, lever="valar"), Features.from_body(json.dumps(check).encode()), boom).track, "anthropic")

    def test_servertool_beats_lever_unless_rule_off(self):
        ws = {"model": "claude-opus-5-5", "messages": [user("q")], "tools": [{"type": "web_search_20250305", "name": "web_search"}]}
        f = Features.from_body(json.dumps(ws).encode())
        self.assertEqual(route(replace(DEFAULT_CONFIG, lever="valar"), f, boom).track, "anthropic")
        off = rules_with(replace(DEFAULT_CONFIG, lever="valar"), servertool=False)
        self.assertEqual(route(off, f, boom).track, "valar")
        self.assertEqual(route(replace(DEFAULT_CONFIG, lever="valar"), Features.from_body(body([user("q")])), boom).track, "valar")

    def test_old_config_gets_servertool_rule_on_top(self):
        o = config_to_json(DEFAULT_CONFIG)
        o["rules"] = o["rules"][1:]
        self.assertEqual(config_from_json(o).rules, DEFAULT_CONFIG.rules)

    def test_classifier_skips_fable(self):
        d = route(DEFAULT_CONFIG, Features.from_body(body([user("hi")], "claude-fable-5-1")), boom)
        self.assertEqual(d.explanation[2]["outcome"], "skipped: main session (fable)")

    def test_track_model(self):
        ds = replace(DEFAULT_CONFIG.downstreams[0], models={"fable": "F", "opus": "O", "sonnet": "S", "haiku": "H"})
        self.assertEqual([track_model(x, ds) for x in ("claude-fable-5-1", "claude-opus-5", "claude-sonnet-5", "claude-haiku-5",
                                                       "claude-3-5-foo", "zai-org/GLM-5.3")],
                         ["F", "O", "S", "H", "S", "zai-org/GLM-5.3"])

    def test_v2_models_without_fable_opus_serve_sonnet(self):
        o = v2()
        o["models"]["valar"] = {"sonnet": "S", "haiku": "H"}
        self.assertEqual(config_from_json(o).downstreams[0].models, {"fable": "S", "opus": "S", "sonnet": "S", "haiku": "H"})

    def test_v1_migrates(self):
        v1 = {"version": 1, "lever": "rules", "otherwise": "anthropic",
              "rules": [{"name": r.name, "kind": r.kind, "enabled": r.enabled, "text": r.text, "builtin": r.builtin}
                        for r in DEFAULT_CONFIG.rules],
              "classifier": {"model": "gemma-haiku", "threshold": 0.7, "timeout_s": 1.5,
                             "labels": [{"name": "coding", "description": "d", "track": "local"}]},
              "doubling": {"rate": 0.5, "tag": "x", "when_local": True, "when_anthropic": False}}
        c = config_from_json(v1)
        self.assertEqual(c.downstreams, DEFAULT_CONFIG.downstreams)
        self.assertEqual(c.classifier.upstream, "valar")  # v1's local classifier now asks Valar
        self.assertEqual(c.classifier.labels[0].track, "anthropic")
        self.assertEqual(c.doubling.shadow, {"anthropic": None, "valar": None})
        self.assertEqual(c.rules, DEFAULT_CONFIG.rules)
        self.assertEqual(config_to_json(c)["version"], 4)

    def test_windows(self):
        o = config_to_json(DEFAULT_CONFIG)
        self.assertEqual(o["downstreams"][0]["windows"], {"fable": "asap", "opus": "asap", "sonnet": "asap", "haiku": "asap"})
        o2 = v2()
        del o2["windows"]
        self.assertEqual(config_from_json(o2).downstreams[0].windows, {t: "asap" for t in MODEL_TIERS})
        w = {"fable": "asap", "opus": "priority", "sonnet": "standard", "haiku": "asap"}
        o["downstreams"][0]["windows"] = w
        self.assertEqual(config_from_json(json.loads(json.dumps(o))).downstreams[0].windows, w)
        for bad in ("flex", "batch", None):
            o["downstreams"][0]["windows"] = {**w, "haiku": bad}
            with self.assertRaisesRegex(ValueError, "downstream valar: windows must be asap, priority or standard"):
                config_from_json(o)

    def test_track_window(self):
        ds = replace(DEFAULT_CONFIG.downstreams[0], windows={"fable": "asap", "opus": "priority", "sonnet": "standard", "haiku": "asap"})
        self.assertEqual([track_window(x, ds) for x in ("claude-opus-5", "claude-sonnet-5", "claude-3-5-foo", "zai-org/GLM-5.3")],
                         ["priority", "standard", "standard", "standard"])
        self.assertIsNone(track_window("claude-opus-5", LEMMI))

    def test_bad_values(self):
        def mod(f):
            o = json.loads(json.dumps(config_to_json(DEFAULT_CONFIG)))
            f(o)
            return o
        def pop_haiku(o): del o["downstreams"][0]["models"]["haiku"]
        def empty(o): o["downstreams"][0]["models"]["sonnet"] = ""
        def up(o): o["classifier"]["upstream"] = "anthropic"
        def sh(v): return lambda o: o["doubling"]["shadow"].__setitem__("valar", v)
        for f in (pop_haiku, empty, up, sh("valar"), sh("openai"),
                  lambda o: o.__setitem__("lever", "openai"), lambda o: o.__setitem__("otherwise", "openai")):
            with self.assertRaises(ValueError):
                config_from_json(mod(f))


class ConfigJsonTests(unittest.TestCase):
    def test_config_roundtrip(self):
        j = config_to_json(DEFAULT_CONFIG)
        self.assertEqual(j["version"], 4)
        self.assertEqual(j["settings"], [])
        self.assertEqual(config_from_json(json.loads(json.dumps(j))), DEFAULT_CONFIG)

    def test_config_errors(self):
        def j(**kw):
            o = config_to_json(DEFAULT_CONFIG)
            for k, v in kw.items():
                o = {**o, k: v}
            return o
        rules = config_to_json(DEFAULT_CONFIG)["rules"]
        def with_rules(rs):
            return j(rules=rs)
        def edit(i, **kw):
            return with_rules([{**r, **kw} if n == i else r for n, r in enumerate(rules)])
        cl = config_to_json(DEFAULT_CONFIG)["classifier"]
        cases = [
            (j(version=5), "unsupported config version"),
            (j(settings=[{"when": "", "downstream": "nope", "tier": None, "set": "window", "value": "priority"}]),
             "settings line 1: no downstream named 'nope'"),
            (j(lever="x"), "lever: no downstream named x"),
            (j(classifier={**cl, "threshold": 1.5}), "classifier threshold must be 0\u20131"),
            (j(doubling={**config_to_json(DEFAULT_CONFIG)["doubling"], "rate": 2}), "doubling rate must be 0\u20131"),
            (with_rules(rules + [{**rules[2], "name": "Second"}]), "there must be exactly one classifier rule"),
            (with_rules(rules[:1] + rules[2:]), "built-in rule 'tags' cannot be deleted"),
            (with_rules(rules + [{**rules[0], "name": rules[0]["name"], "builtin": None}]), "rule names must be unique"),
            (j(classifier={**cl, "labels": [{"name": "a", "description": "d", "track": "x"}]}), "label track: no downstream named x"),
            (edit(1, text="when tag is plan go"), "rule 'Tags in your message': line 1, col 21: expected a track name"),
        ]
        for bad in ("inf", 1e300, float("nan"), float("inf"), -1, 0.05, 31, True):
            cases.append((j(classifier={**cl, "timeout_s": bad}), "classifier time limit must be 0.1\u201330 s"))
        cases.append((j(classifier={**cl, "threshold": float("nan")}), "classifier threshold must be 0\u20131"))
        for obj, msg in cases:
            with self.assertRaises(ValueError) as cm:
                config_from_json(obj)
            self.assertEqual(str(cm.exception), msg)



class ConfigTests(unittest.TestCase):
    def test_config_defaults(self):
        c = load_config({})
        self.assertEqual((c.listen_host, c.listen_port), ("127.0.0.1", 8787))
        self.assertEqual(c.anthropic_url, "https://api.anthropic.com")
        self.assertEqual(c.env_valar, ("", ""))

    def test_config_valar_url_strip(self):
        for u in ("https://api.valarhq.ai/v1/", "https://api.valarhq.ai/", "https://api.valarhq.ai/v1"):
            self.assertEqual(load_config({"VALAR_URL": u}).env_valar[0], "https://api.valarhq.ai")

    def test_config_keys_optional(self):
        c = load_config({"VALAR_API_KEY": "v"})
        self.assertEqual(c.env_valar, ("", "v"))
        self.assertEqual(load_config({}).listen_port, 8787)

    def test_config_rejects_non_loopback(self):
        for listen in ("0.0.0.0:8787", "192.168.1.5:8787", "example.com:8787"):
            with self.assertRaises(ValueError):
                load_config({"SIGNALLER_LISTEN": listen})
        self.assertEqual(load_config({"SIGNALLER_LISTEN": "0.0.0.0:8787", "SIGNALLER_IN_CONTAINER": "1"}).listen_host, "0.0.0.0")
        with self.assertRaises(ValueError):  # only the any-address, and only in a container
            load_config({"SIGNALLER_LISTEN": "192.168.1.5:8787", "SIGNALLER_IN_CONTAINER": "1"})
        c = load_config({"SIGNALLER_LISTEN": "localhost:9000"})
        self.assertEqual((c.listen_host, c.listen_port), ("localhost", 9000))
        c = load_config({"SIGNALLER_LISTEN": "[::1]:9000"})
        self.assertEqual((c.listen_host, c.listen_port), ("::1", 9000))


if __name__ == "__main__":
    unittest.main()


class SessionTests(unittest.TestCase):
    def test_session_of(self):
        meta = {"metadata": {"user_id": json.dumps({"session_id": "M"})}}
        self.assertEqual(session_of({"x-claude-code-session-id": "H"}, meta), "H")
        self.assertEqual(session_of({"X-Claude-Code-Session-Id": "H"}, None), "H")
        self.assertEqual(session_of({}, meta), "M")
        for bad in ("/abs/path/x", "../x", "a,b", "unknown", "x" * 129, "a b"):
            self.assertIsNone(session_of({"X-Claude-Code-Session-Id": bad}, None))
            self.assertIsNone(session_of({}, {"metadata": {"user_id": json.dumps({"session_id": bad})}}))
        u = "0b7e1c52-9d3a-4f6e-8a21-5c4d3e2f1a0b"
        self.assertEqual(session_of({"X-Claude-Code-Session-Id": u}, None), u)
        self.assertEqual(session_of({"X-Claude-Code-Session-Id": "../x"}, {"metadata": {"user_id": json.dumps({"session_id": "M"})}}), "M")
        for req in (None, {}, [], {"metadata": 5}, {"metadata": {"user_id": "{bad"}},
                    {"metadata": {"user_id": "[1]"}}, {"metadata": {"user_id": '{"session_id": 3}'}}):
            self.assertIsNone(session_of({}, req))


FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "console_v2.json")
LEMMI = Downstream("lemmi", "lemmi", "https://lemmi.example", "generic", {t: "deepseek-v4.1-flash" for t in MODEL_TIERS},
                   "#1F8A5B", None, None)


def v2():
    with open(FIXTURE) as f:
        return json.load(f)


def with_lemmi(cfg=DEFAULT_CONFIG, **kw):
    return replace(cfg, downstreams=cfg.downstreams + (LEMMI,),
                   doubling=replace(cfg.doubling, shadow={**cfg.doubling.shadow, "lemmi": None}), **kw)


class DownstreamTests(unittest.TestCase):
    def test_v2_real_config_migrates_without_routing_change(self):
        cfg = config_from_json(v2())
        self.assertEqual([d.id for d in cfg.downstreams], ["valar"])
        self.assertEqual(cfg.downstreams[0].models["opus"], "zai-org/GLM-5.3")
        tags = next(r for r in cfg.rules if r.builtin == "tags")
        self.assertEqual(tags.text, "when tag is planning go anthropic\nwhen tag is execution go valar")
        self.assertEqual(cfg.migrated_off, ())
        for model, t, want in (("claude-opus-5-5", "hi", "anthropic"), ("claude-sonnet-5-5", "hi", "valar"),
                               ("claude-haiku-5-5", "hi", "valar"), ("claude-haiku-5-5", "#planning x", "anthropic")):
            self.assertEqual(route(cfg, Features.from_body(body([user(t)], model)), boom).track, want, model)

    def test_v2_stray_window_key_migrates(self):
        o = v2()
        o["windows"]["valar"]["stray"] = "asap"
        cfg = config_from_json(o)
        self.assertEqual(set(cfg.downstreams[0].windows), set(MODEL_TIERS))

    def test_v2_local_references_repointed(self):
        o = v2()
        o.update(lever="local", otherwise="local")
        o["classifier"].update(upstream="local")
        o["classifier"]["labels"][0]["track"] = "local"
        o["doubling"]["shadow"]["anthropic"] = "local"
        o["rules"].insert(1, {"name": "Mine", "kind": "dsl", "enabled": True, "text": "when tag is x go local", "builtin": None})
        cfg = config_from_json(o)
        self.assertEqual((cfg.lever, cfg.otherwise, cfg.classifier.labels[0].track, cfg.classifier.upstream),
                         ("rules", "anthropic", "anthropic", "valar"))
        self.assertIsNone(cfg.doubling.shadow["anthropic"])
        self.assertNotIn("local", cfg.doubling.shadow)
        self.assertIs(next(r for r in cfg.rules if r.name == "Mine").enabled, False)
        self.assertEqual(cfg.migrated_off, ("Mine",))

    def test_v3_round_trip(self):
        for cfg in (DEFAULT_CONFIG, with_lemmi()):
            j = json.loads(json.dumps(config_to_json(cfg)))
            self.assertEqual(j["version"], 4)
            self.assertEqual(config_from_json(j), cfg)

    def test_unknown_track_in_enabled_rule_rejected(self):
        o = config_to_json(DEFAULT_CONFIG)
        o["rules"].insert(1, {"name": "L", "kind": "dsl", "enabled": True, "text": "when tag is x go lemmi", "builtin": None})
        with self.assertRaisesRegex(ValueError, "no downstream named lemmi"):
            config_from_json(o)

    def test_unknown_track_in_disabled_rule_allowed(self):
        o = config_to_json(DEFAULT_CONFIG)
        o["rules"].insert(1, {"name": "L", "kind": "dsl", "enabled": False, "text": "when tag is x go lemmi", "builtin": None})
        self.assertEqual(config_from_json(o).rules[1].lines[0].track, "lemmi")

    def test_valid_id(self):
        self.assertIsNone(valid_id("lemmi", ["valar"]))
        for bad in ("Lemmi", "anthropic", "name", "go", "tier", "a" * 25):
            self.assertIsInstance(valid_id(bad, []), str, bad)
        self.assertIsInstance(valid_id("lemmi", ["lemmi"]), str)

    def test_references_lists_settings_lines(self):
        cfg = replace(DEFAULT_CONFIG, settings=(Setting("", None, "valar", None, "window", "priority"),
                                                Setting("hour < 7", parse_cond("hour < 7"), "lemmi", None, "model", "glm-x")))
        self.assertIn("settings line 1", references(cfg, "valar", {}))
        self.assertEqual(references(cfg, "lemmi", {})[-1], "settings line 2")
        self.assertNotIn("settings line 1", references(replace(cfg, settings=()), "valar", {}))

    def test_v3_fixture_loads_with_no_settings(self):
        with open(os.path.join(os.path.dirname(__file__), "fixtures", "console_v3.json")) as f:
            cfg = config_from_json(json.load(f))
        self.assertEqual(cfg.settings, ())
        self.assertEqual(cfg.downstreams[0].models["sonnet"], "zai-org/GLM-5.3")

    def test_settings_round_trip(self):
        cfg = replace(DEFAULT_CONFIG, settings=(Setting("hour < 7", parse_cond("hour < 7"), "valar", None, "model", "glm-x"),))
        j = json.loads(json.dumps(config_to_json(cfg)))
        self.assertEqual(j["settings"], [{"when": "hour < 7", "downstream": "valar", "tier": None,
                                           "set": "model", "value": "glm-x"}])
        self.assertEqual(config_from_json(j), cfg)

    def test_references(self):
        rule = Rule("Mine", "dsl", True, "when tag is a go anthropic\nwhen tag is b go lemmi")
        off = Rule("Off", "dsl", False, "when tag is c go lemmi")
        cs = DEFAULT_CONFIG.classifier
        cfg = with_lemmi(lever="lemmi", otherwise="lemmi", rules=(off, rule) + DEFAULT_CONFIG.rules,
                         classifier=replace(cs, upstream="lemmi", labels=(replace(cs.labels[2], track="lemmi"),) + cs.labels[3:]))
        cfg = replace(cfg, doubling=replace(cfg.doubling, shadow={**cfg.doubling.shadow, "anthropic": "lemmi"}))
        self.assertEqual(references(cfg, "lemmi", {"819a0a39-aaaa": "lemmi", "other": "valar"}),
                         ["the lever", "session 819a0a39's lever", "rule 'Mine' line 2", "Otherwise",
                          "classifier label 'coding'", "classifier upstream", "side by side: anthropic → lemmi"])
        self.assertEqual(references(cfg, "valar", {}), ["rule 'Tags in your message' line 2", "rule 'Model the request asked for' line 2",
                                                       "classifier label 'chit-chat'"])  # valar → anthropic is its own
        own = replace(cfg, doubling=replace(cfg.doubling, shadow={**cfg.doubling.shadow, "anthropic": None, "lemmi": "anthropic"}))
        self.assertNotIn("side by side: lemmi → anthropic", references(own, "lemmi", {}))  # its own outgoing shadow goes with it

    def test_seven_downstreams_rejected(self):
        o = config_to_json(DEFAULT_CONFIG)
        o["downstreams"] += [{**o["downstreams"][0], "id": "d%d" % i, "kind": "generic", "windows": None} for i in range(6)]
        o["doubling"]["shadow"].update({"d%d" % i: None for i in range(6)})
        with self.assertRaisesRegex(ValueError, "at most 6 downstreams"):
            config_from_json(o)
        o["downstreams"].pop()
        del o["doubling"]["shadow"]["d5"]
        self.assertEqual(len(config_from_json(o).downstreams), 6)

    def test_zero_downstreams(self):
        o = config_to_json(DEFAULT_CONFIG)
        o["downstreams"] = []
        texts = {"tags": "when tag is planning go anthropic", "tier": "when tier is opus go anthropic"}
        for r in o["rules"]:
            r["text"] = texts.get(r["builtin"], r["text"])
        o["classifier"]["upstream"] = "none"
        o["classifier"]["labels"] = [l for l in o["classifier"]["labels"] if l["track"] == "anthropic"]
        o["doubling"]["shadow"] = {"anthropic": None}
        cfg = config_from_json(o)
        self.assertEqual(tracks(cfg), ("anthropic",))
        self.assertIsNone(downstream(cfg, "valar"))

    def test_v3_errors(self):
        def mod(f):
            o = json.loads(json.dumps(config_to_json(with_lemmi())))
            f(o)
            return o
        def ds(i, **kw): return lambda o: o["downstreams"][i].update(kw)
        cases = [
            (ds(1, id="valar"), "there is already a downstream named valar"),
            (ds(1, id="Lemmi"), "id"),
            (ds(0, windows=None), "windows"),
            (ds(0, windows={"opus": "asap"}), "windows"),
            (ds(1, kind="openai"), "kind"),
            (ds(1, prices={"in": 1, "out": 2, "cache_read": 0}), "prices"),
            (ds(1, prices={"in": 1, "out": -2, "cache_read": 0, "cache_write": 0}), "prices"),
            (ds(1, models={"opus": "x"}), "models"),
            (lambda o: o["classifier"].update(upstream="anthropic"), "classifier upstream"),
            (lambda o: o["doubling"]["shadow"].pop("lemmi"), "shadow"),
            (lambda o: o.update(lever="local"), "lever"),
            (ds(1, color="#88888"), "color"),
            (ds(1, color="red"), "color"),
        ]
        for f, frag in cases:
            with self.assertRaisesRegex(ValueError, frag):
                config_from_json(mod(f))
        ok = mod(ds(1, prices={"in": 1, "out": 2.5, "cache_read": 0.1, "cache_write": 0}))
        self.assertEqual(downstream(config_from_json(ok), "lemmi").prices["out"], 2.5)


class StripTagsTest(unittest.TestCase):
    def test_leading_tags_stripped_from_every_prompt(self):
        req = {"messages": [user("#execution #local  fix it"), assistant(text("ok")),
                            user([{"type": "text", "text": "<system-reminder>#x</system-reminder>"},
                                  {"type": "text", "text": "#planning: next"}]),
                            user("mid #execution stays"), user("#execution")]}
        self.assertTrue(strip_lead_tags(req))
        m = req["messages"]
        self.assertEqual((m[0]["content"], m[2]["content"][0]["text"], m[2]["content"][1]["text"], m[3]["content"], m[4]["content"]),
                         ("fix it", "<system-reminder>#x</system-reminder>", "next", "mid #execution stays", "#execution"))
        self.assertFalse(strip_lead_tags({"messages": [user("plain")]}))


class ToolStepTest(unittest.TestCase):
    def test_tool_results_named_from_the_call(self):
        msgs = [user("do it"),
                {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "Bash", "input": {}},
                                                  {"type": "tool_use", "id": "b", "name": "Read", "input": {}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "a", "content": "3 passed", "is_error": False},
                    {"type": "tool_result", "tool_use_id": "b", "is_error": True,
                     "content": [{"type": "text", "text": "no such file"}, {"type": "image"}]},
                    {"type": "text", "text": "<system-reminder>x</system-reminder>"}]}]
        self.assertEqual(latest_tool_results(msgs), "Bash: 3 passed\nRead (error): no such file [image]")
        f = Features.from_body(body(msgs))
        self.assertEqual((f.text, f.step), ("do it", "Bash: 3 passed\nRead (error): no such file [image]"))

    def test_a_typed_prompt_is_not_a_step(self):
        self.assertIsNone(latest_tool_results([user("hi")]))
        self.assertIsNone(latest_tool_results([]))
        self.assertIsNone(Features.from_body(body([user("hi")])).step)

    def test_capped(self):
        msgs = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "z", "content": "x" * 5000}]}]
        self.assertEqual(latest_tool_results(msgs), ("tool: " + "x" * 5000)[:600])


class AutoTests(unittest.TestCase):
    cfg = replace(OFF, lever="auto")

    def test_phase_decides(self):
        f = Features.from_body(body([user(text("anything"))]))
        d = route(self.cfg, f, boom, lambda _: ("planning", "phase: demo:plan"))
        self.assertEqual((d.track, d.reason, d.explanation[0]["outcome"]), ("anthropic", "auto:planning", "planning (phase: demo:plan) → anthropic"))
        d = route(self.cfg, f, boom, lambda _: ("none", "no planning signal"))
        self.assertEqual((d.track, d.reason, d.explanation[0]["outcome"]), ("valar", "auto:execution", "execution (no planning signal) → valar"))
        self.assertEqual(route(self.cfg, f, boom).track, "valar")

    def test_tags_and_servertool_win(self):
        def f(t):
            return Features.from_body(body([user(text(t))]))
        self.assertEqual(route(self.cfg, f("#execution go"), boom, lambda _: ("planning", "x")).track, "valar")
        d = route(self.cfg, f("#planning go"), boom, lambda _: ("execution", "x"))
        self.assertEqual((d.track, d.reason), ("anthropic", "tag"))
        self.assertEqual(route(self.cfg, f("#local go"), boom, lambda _: ("planning", "x")).reason, "auto:planning")
        s = f("q")
        s.servertool = True
        self.assertEqual(route(self.cfg, s, boom, lambda _: ("execution", "x")).track, "anthropic")

    def test_auto_tags_read_the_turns_opening_message(self):
        # text Claude Code appends to a tool result mid-turn is not the turn's prompt: its tags, or lack of
        # them, must not flip the track the turn was pinned to
        msgs = [user(text("#execution build it")),
                assistant([{"type": "tool_use", "id": "x", "name": "Bash", "input": {}}]),
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"},
                                             {"type": "text", "text": "Base directory for this skill: /r"}]}]
        f = Features.from_body(body(msgs))
        d = route(self.cfg, f, boom, lambda _: ("planning", "x"))
        self.assertEqual((d.track, d.reason, d.auto), ("valar", "tag", ("execution", "tag")))

    def test_settings_round_trip_defaults_and_rejects(self):
        j = config_to_json(OFF)
        j.pop("auto")
        self.assertEqual(config_from_json(j).auto, AutoSettings())
        j = config_to_json(OFF)
        j["auto"]["threshold"] = 0.9
        self.assertEqual(config_from_json(j).auto.threshold, 0.9)
        fx = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "console_v4_auto.json")))
        self.assertEqual(config_to_json(config_from_json(fx)), fx)
        for bad in ({"threshold": 2}, {"execution": "nope"}, {"plan_dirs": [""]}, {"plan_dirs": ["x"] * 21}):
            with self.assertRaises(ValueError):
                config_from_json({**j, "auto": bad})

    def test_auto_train_cap_roundtrip_and_bounds(self):
        j = config_to_json(OFF)
        self.assertEqual(j["auto"]["train_cap"], 5.0)  # the default, written out
        self.assertEqual(config_from_json({**j, "auto": {k: v for k, v in j["auto"].items() if k != "train_cap"}}).auto.train_cap, 5.0)
        j["auto"]["train_cap"] = 7.5
        self.assertEqual(config_from_json(j).auto.train_cap, 7.5)
        for bad in (0.4, 1001, "x", True, None):
            with self.assertRaises(ValueError):
                config_from_json({**j, "auto": {**j["auto"], "train_cap": bad}})

    def test_exec_track_falls_back(self):
        self.assertEqual(exec_track(replace(OFF, auto=AutoSettings(execution="valar"))), "valar")
        self.assertEqual(exec_track(replace(OFF, downstreams=())), "anthropic")
