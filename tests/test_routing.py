import hashlib
import json
import unittest
from dataclasses import replace

from claude_router import load_config
from routing import (DEFAULT_CONFIG, ClassResult, Features, Rule, is_routed, config_from_json, config_to_json, route, tier_of, track_model)


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
    def test_tag_local_in_latest_human(self):
        self.assertEqual(route_([user(text("do it #local"))]), ("local", "rule:Tags in your message:2"))

    def test_tag_plan_beats_haiku(self):
        self.assertEqual(route_([user(text("#planning"))], "claude-haiku-4-5"), ("anthropic", "rule:Tags in your message:1"))

    def test_tag_local_beats_opus(self):
        self.assertEqual(route_([user(text("#local"))]), ("local", "rule:Tags in your message:2"))

    def test_both_tags_anthropic(self):
        self.assertEqual(route_([user(text("#local and #planning"))]), ("anthropic", "rule:Tags in your message:1"))

    def test_tag_case_insensitive_whole_word(self):
        self.assertEqual(route_([user(text("#LOCAL"))]), ("local", "rule:Tags in your message:2"))
        self.assertEqual(route_([user(text("#localhost"))]), ("anthropic", "rule:Model the request asked for:1"))

    def test_tag_in_older_message_ignored(self):
        msgs = [user(text("#local")), assistant(text("ok")), user(text("next"))]
        self.assertEqual(route_(msgs), ("anthropic", "rule:Model the request asked for:1"))

    def test_tool_result_only_message_skipped(self):
        msgs = [
            user(text("#local go")),
            assistant([{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]),
            user([{"type": "tool_result", "tool_use_id": "t1", "content": "done"}]),
        ]
        self.assertEqual(route_(msgs), ("local", "rule:Tags in your message:2"))

    def test_system_reminder_block_ignored(self):
        blocks = [
            {"type": "text", "text": "<system-reminder>#local</system-reminder>"},
            {"type": "text", "text": "hello"},
        ]
        self.assertEqual(route_([user(blocks)]), ("anthropic", "rule:Model the request asked for:1"))

    def test_string_content(self):
        self.assertEqual(route_([user("fix #local")]), ("local", "rule:Tags in your message:2"))

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
    ([user(text("do it #local"))], "local"),
    ([user(text("#planning"))], "anthropic"),
    ([user(text("#local and #planning"))], "anthropic"),
    ([user(text("#LOCAL"))], "local"),
    ([user(text("#localhost"))], "anthropic"),
    ([user("hi")], "anthropic"),
    ([user(text("#local")), assistant(text("ok")), user(text("next"))], "anthropic"),
    ([user(text("#local go")),
      assistant([{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]),
      user([{"type": "tool_result", "tool_use_id": "t1", "content": "done"}])], "local"),
    ([user([{"type": "text", "text": "<system-reminder>#local</system-reminder>"},
            {"type": "text", "text": "hello"}])], "anthropic"),
    ([user("fix #local")], "local"),
]


def cls(label, conf, note=""):
    return lambda f, s: ClassResult(label, conf, note)


class EngineTests(unittest.TestCase):
    def go(self, cfg, msgs=None, model="claude-opus-5-5", classify=boom):
        return route(cfg, Features.from_body(body(msgs or [user("hi")], model)), classify)

    def test_lever_overrides(self):
        d = self.go(replace(DEFAULT_CONFIG, lever="local"), [user("#plan")])
        self.assertEqual((d.track, d.reason), ("local", "lever"))
        self.assertEqual(d.explanation, [{"rule": "Lever", "outcome": "sending everything to local", "decided": True}])

    def test_first_match_and_explanation(self):
        d = self.go(OFF, [user("#local")])
        self.assertEqual(d.reason, "rule:Tags in your message:2")
        self.assertEqual([e["outcome"] for e in d.explanation], ["line 2 matched \u2192 local", "not reached", "not reached"])

    def test_disabled_rule_skipped(self):
        d = self.go(rules_with(OFF, tags=False), [user("#local")])
        self.assertEqual((d.track, d.reason), ("anthropic", "rule:Model the request asked for:1"))
        self.assertEqual(d.explanation[0]["outcome"], "switched off")

    def test_otherwise_track(self):
        cfg = replace(OFF, otherwise="local", rules=tuple(replace(r, enabled=False) for r in OFF.rules))
        d = self.go(cfg)
        self.assertEqual((d.track, d.reason), ("local", "default"))
        self.assertEqual(d.explanation[-1], {"rule": "Otherwise", "outcome": "\u2192 local", "decided": True})

    def test_classifier_decides_at_threshold(self):
        d = self.go(DEFAULT_CONFIG, model="claude-sonnet-5-5", classify=cls("planning", 0.7))
        self.assertEqual((d.track, d.reason), ("anthropic", "about:planning"))
        self.assertEqual(d.explanation[1]["outcome"], "planning, 70% sure \u2192 anthropic")
        self.assertEqual(d.classified, ClassResult("planning", 0.7, ""))

    def test_classifier_below_threshold_passes(self):
        d = self.go(DEFAULT_CONFIG, model="claude-sonnet-5-5", classify=cls("planning", 0.69))
        self.assertEqual(d.track, "valar")
        self.assertEqual(d.explanation[1]["outcome"], "planning, 69% sure, below 70%")

    def test_classifier_note_passes(self):
        d = self.go(DEFAULT_CONFIG, model="claude-sonnet-5-5", classify=cls(None, None, "timed out after 1.5 s"))
        self.assertEqual(d.explanation[1]["outcome"], "timed out after 1.5 s")
        self.assertEqual(d.reason, "rule:Model the request asked for:2")

    def test_classifier_skipped_on_opus(self):
        d = self.go(DEFAULT_CONFIG)
        self.assertEqual(d.explanation[1]["outcome"], "skipped: main session (opus)")
        self.assertIsNone(d.classified)

    def test_label_on_opus_never_classifies(self):
        user_rule = Rule("U", "dsl", True, "when label is coding go local", None)
        cfg = replace(DEFAULT_CONFIG, rules=(user_rule,) + DEFAULT_CONFIG.rules)
        d = self.go(cfg)
        self.assertEqual(d.track, "anthropic")

    def test_classify_called_once_per_route(self):
        r = Rule("U", "dsl", True, "when label is coding go local", None)
        cfg = replace(DEFAULT_CONFIG, rules=(r,) + DEFAULT_CONFIG.rules[1:])
        calls = []
        def c(f, s):
            calls.append(1)
            return ClassResult("coding", 0.9, "")
        d = self.go(cfg, model="claude-sonnet-5-5", classify=c)
        self.assertEqual((d.track, len(calls)), ("local", 1))
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


INTENT = [
    ("claude-opus-5-5", "hi", "anthropic", "Model the request asked for:1"),
    ("claude-fable-5-1", "hi", "anthropic", "Model the request asked for:1"),
    ("claude-sonnet-5-5", "hi", "valar", "Model the request asked for:2"),
    ("claude-haiku-5-5", "hi", "valar", "Model the request asked for:2"),
    ("zai-org/GLM-5.3", "hi", "valar", "Model the request asked for:2"),
    ("claude-opus-5-5", "#execution go", "valar", "Tags in your message:3"),
    ("claude-haiku-5-5", "#planning x", "anthropic", "Tags in your message:1"),
    ("claude-opus-5-5", "#local x", "local", "Tags in your message:2"),
    ("claude-opus-5-5", "#local #planning", "anthropic", "Tags in your message:1"),
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

    def test_classifier_skips_fable(self):
        d = route(DEFAULT_CONFIG, Features.from_body(body([user("hi")], "claude-fable-5-1")), boom)
        self.assertEqual(d.explanation[1]["outcome"], "skipped: main session (fable)")

    def test_track_model(self):
        m = {"sonnet": "S", "haiku": "H"}
        self.assertEqual([track_model(x, m) for x in ("claude-haiku-5", "claude-sonnet-5", "claude-opus-5", "claude-3-5-foo", "zai-org/GLM-5.3")],
                         ["H", "S", "S", "S", "zai-org/GLM-5.3"])

    def test_v1_migrates(self):
        v1 = {"version": 1, "lever": "rules", "otherwise": "anthropic",
              "rules": [{"name": r.name, "kind": r.kind, "enabled": r.enabled, "text": r.text, "builtin": r.builtin}
                        for r in DEFAULT_CONFIG.rules],
              "classifier": {"model": "gemma-haiku", "threshold": 0.7, "timeout_s": 1.5,
                             "labels": [{"name": "coding", "description": "d", "track": "local"}]},
              "doubling": {"rate": 0.5, "tag": "x", "when_local": True, "when_anthropic": False}}
        c = config_from_json(v1)
        self.assertEqual(c.models, DEFAULT_CONFIG.models)
        self.assertEqual(c.classifier.upstream, "local")
        self.assertEqual(c.doubling.shadow, {"anthropic": None, "local": "anthropic", "valar": None})
        self.assertEqual(c.rules, DEFAULT_CONFIG.rules)
        self.assertEqual(config_to_json(c)["version"], 2)

    def test_v2_roundtrip(self):
        self.assertEqual(config_from_json(config_to_json(DEFAULT_CONFIG)), DEFAULT_CONFIG)

    def test_v2_errors(self):
        def mod(f):
            o = json.loads(json.dumps(config_to_json(DEFAULT_CONFIG)))
            f(o)
            return o
        def pop_haiku(o): del o["models"]["valar"]["haiku"]
        def empty(o): o["models"]["local"]["sonnet"] = ""
        def up(o): o["classifier"]["upstream"] = "anthropic"
        def sh(v): return lambda o: o["doubling"]["shadow"].__setitem__("valar", v)
        for f in (pop_haiku, empty, up, sh("valar"), sh("openai"),
                  lambda o: o.__setitem__("lever", "openai"), lambda o: o.__setitem__("otherwise", "openai")):
            with self.assertRaises(ValueError):
                config_from_json(mod(f))


class ConfigJsonTests(unittest.TestCase):
    def test_config_roundtrip(self):
        j = config_to_json(DEFAULT_CONFIG)
        self.assertEqual(j["version"], 2)
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
            (j(version=3), "unsupported config version"),
            (j(lever="x"), "lever must be anthropic, rules, valar or local"),
            (j(classifier={**cl, "threshold": 1.5}), "classifier threshold must be 0\u20131"),
            (j(doubling={**config_to_json(DEFAULT_CONFIG)["doubling"], "rate": 2}), "doubling rate must be 0\u20131"),
            (with_rules(rules + [{**rules[1], "name": "Second"}]), "there must be exactly one classifier rule"),
            (with_rules(rules[1:]), "built-in rule 'tags' cannot be deleted"),
            (with_rules(rules + [{**rules[0], "name": rules[0]["name"], "builtin": None}]), "rule names must be unique"),
            (j(classifier={**cl, "labels": [{"name": "a", "description": "d", "track": "x"}]}), "label track must be anthropic, valar or local"),
            (edit(0, text="when tag is plan go"), "rule 'Tags in your message': line 1, col 21: expected anthropic, valar or local"),
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
        c = load_config({"LITELLM_API_KEY": "k"})
        self.assertEqual((c.listen_host, c.listen_port), ("127.0.0.1", 8787))
        self.assertEqual(c.anthropic_url, "https://api.anthropic.com")
        self.assertEqual(c.litellm_url, "http://127.0.0.1:4000")
        self.assertEqual(c.litellm_key, "k")
        self.assertEqual(c.valar_url, "https://api.valarhq.ai")
        c = load_config({})
        self.assertEqual((c.litellm_key, c.valar_key), ("", ""))

    def test_config_valar_url_strip(self):
        for u in ("https://api.valarhq.ai/v1/", "https://api.valarhq.ai/", "https://api.valarhq.ai/v1"):
            self.assertEqual(load_config({"VALAR_URL": u}).valar_url, "https://api.valarhq.ai")

    def test_config_keys_optional(self):
        c = load_config({"VALAR_API_KEY": "v"})
        self.assertEqual((c.valar_key, c.litellm_key), ("v", ""))
        self.assertEqual(load_config({}).listen_port, 8787)

    def test_config_rejects_non_loopback(self):
        for listen in ("0.0.0.0:8787", "192.168.1.5:8787", "example.com:8787"):
            with self.assertRaises(ValueError):
                load_config({"LITELLM_API_KEY": "k", "CLAUDE_ROUTER_LISTEN": listen})
        c = load_config({"LITELLM_API_KEY": "k", "CLAUDE_ROUTER_LISTEN": "localhost:9000"})
        self.assertEqual((c.listen_host, c.listen_port), ("localhost", 9000))
        c = load_config({"LITELLM_API_KEY": "k", "CLAUDE_ROUTER_LISTEN": "[::1]:9000"})
        self.assertEqual((c.listen_host, c.listen_port), ("::1", 9000))


if __name__ == "__main__":
    unittest.main()
