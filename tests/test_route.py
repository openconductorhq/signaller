import json
import unittest

from claude_router import decide, load_config, local_model


def body(messages, model="claude-opus-5-5"):
    return json.dumps({"model": model, "messages": messages}).encode()


def user(content):
    return {"role": "user", "content": content}


def assistant(content):
    return {"role": "assistant", "content": content}


def text(t):
    return [{"type": "text", "text": t}]


def route(messages, model="claude-opus-5-5", path="/v1/messages"):
    return decide("POST", path, body(messages, model))


class RouteTests(unittest.TestCase):
    def test_tag_local_in_latest_human(self):
        self.assertEqual(route([user(text("do it #local"))], path="/v1/messages?beta=true"), ("local", "tag:local"))

    def test_tag_plan_beats_haiku(self):
        self.assertEqual(route([user(text("#plan"))], "claude-haiku-4-5"), ("anthropic", "tag:plan"))

    def test_tag_local_beats_opus(self):
        self.assertEqual(route([user(text("#local"))]), ("local", "tag:local"))

    def test_both_tags_anthropic(self):
        self.assertEqual(route([user(text("#local and #plan"))]), ("anthropic", "tag:plan"))

    def test_tag_case_insensitive_whole_word(self):
        self.assertEqual(route([user(text("#LOCAL"))]), ("local", "tag:local"))
        self.assertEqual(route([user(text("#localhost"))]), ("anthropic", "tier"))

    def test_tag_in_older_message_ignored(self):
        msgs = [user(text("#local")), assistant(text("ok")), user(text("next"))]
        self.assertEqual(route(msgs), ("anthropic", "tier"))

    def test_tool_result_only_message_skipped(self):
        msgs = [
            user(text("#local go")),
            assistant([{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]),
            user([{"type": "tool_result", "tool_use_id": "t1", "content": "done"}]),
        ]
        self.assertEqual(route(msgs), ("local", "tag:local"))

    def test_system_reminder_block_ignored(self):
        blocks = [
            {"type": "text", "text": "<system-reminder>#local</system-reminder>"},
            {"type": "text", "text": "hello"},
        ]
        self.assertEqual(route([user(blocks)]), ("anthropic", "tier"))

    def test_string_content(self):
        self.assertEqual(route([user("fix #local")]), ("local", "tag:local"))

    def test_tier(self):
        m = [user("hi")]
        self.assertEqual(route(m), ("anthropic", "tier"))
        self.assertEqual(route(m, "claude-sonnet-5-5"), ("local", "tier"))
        self.assertEqual(route(m, "claude-haiku-5-5"), ("local", "tier"))
        self.assertEqual(route(m, "something-else"), ("anthropic", "tier"))

    def test_count_tokens_routed(self):
        self.assertEqual(route([user("hi")], "claude-sonnet-5-5", "/v1/messages/count_tokens"), ("local", "tier"))

    def test_passthrough(self):
        for args in [("GET", "/api/hello", b""), ("POST", "/api/oauth/x", b"{}"), ("POST", "/v1/messages", b"not json")]:
            self.assertEqual(decide(*args), ("anthropic", "passthrough"))

    def test_non_string_model_passthrough(self):
        for m in (None, 5, ["x"]):
            body = json.dumps({"model": m, "messages": [user("hi")]}).encode()
            self.assertEqual(decide("POST", "/v1/messages", body), ("anthropic", "passthrough"))

    def test_local_model(self):
        self.assertEqual(local_model("claude-haiku-5-5", "S", "H"), "H")
        self.assertEqual(local_model("claude-opus-5-5", "S", "H"), "S")
        self.assertEqual(local_model("claude-sonnet-5-5", "S", "H"), "S")


class ConfigTests(unittest.TestCase):
    def test_config_defaults(self):
        c = load_config({"LITELLM_API_KEY": "k"})
        self.assertEqual((c.listen_host, c.listen_port), ("127.0.0.1", 8787))
        self.assertEqual(c.anthropic_url, "https://api.anthropic.com")
        self.assertEqual(c.litellm_url, "http://127.0.0.1:4000")
        self.assertEqual(c.litellm_key, "k")
        self.assertEqual(c.local_sonnet, "claude-sonnet-4-6")
        self.assertEqual(c.local_haiku, "claude-haiku-4-5-20251001")

    def test_config_requires_key(self):
        with self.assertRaises(ValueError):
            load_config({})

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
