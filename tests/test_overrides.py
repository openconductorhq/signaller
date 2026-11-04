"""The pure settings-override engine: validation, resolution, collisions, pruning, upserts."""
import json
import os
import shutil
import tempfile
import unittest

from overrides import (Setting, collisions, describe, is_session_line, prune, reset_session, resolve,
                        settings_from_json, settings_to_json, slot, upsert_session)
from routing import DEFAULT_CONFIG, MODEL_TIERS, Downstream, config_from_json, config_to_json
from rulelang import parse_cond
from store import load_console_config

LEMMI = Downstream("lemmi", "lemmi", "https://lemmi.example", "generic",
                   {t: "deepseek-v4.1-flash" for t in MODEL_TIERS}, "#1F8A5B", None, None)
DS = (DEFAULT_CONFIG.downstreams[0], LEMMI)


def line(when, ds="valar", tier=None, key="window", value="priority"):
    return Setting(when, parse_cond(when), ds, tier, key, value)


def hour(n):
    return lambda name: n


class ResolveTests(unittest.TestCase):
    LINES = (line("hour < 7"), line("", value="standard"), line("", key="model", value="glm-x"))

    def test_first_match_wins_per_key(self):
        r = resolve(self.LINES, "valar", "claude-opus-5-5", hour(3))
        self.assertEqual((r["window"][0], r["model"][0]), ("priority", "glm-x"))
        r = resolve(self.LINES, "valar", "claude-opus-5-5", hour(12))
        self.assertEqual((r["window"][0], r["model"][0]), ("standard", "glm-x"))
        self.assertIs(r["model"][1], self.LINES[2])

    def test_tier_line_and_slot(self):
        lines = (line("", tier="opus"), line("", key="model", value="glm-x"))
        self.assertEqual(resolve(lines, "valar", "claude-opus-5-5", hour(0))["window"][0], "priority")
        r = resolve(lines, "valar", "claude-sonnet-5-5", hour(0))
        self.assertNotIn("window", r)  # the opus line doesn't match sonnet's slot
        self.assertEqual(r["model"][0], "glm-x")
        self.assertNotIn("window", resolve(lines, "valar", "claude-3-5-foo", hour(0)))  # slot sonnet, tier opus
        self.assertEqual(resolve((line(""),), "valar", "claude-3-5-foo", hour(0))["window"][0], "priority")
        self.assertEqual([slot(m) for m in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-3-5-foo", "gpt-4")],
                         ["opus", "sonnet", "sonnet", "sonnet"])
        self.assertEqual(resolve(lines, "valar", "gpt-4", hour(0)), {})  # a model line never applies to gpt-4

    def test_no_lines_resolves_empty(self):
        self.assertEqual(resolve((), "valar", "claude-opus-5-5", hour(0)), {})


class CollisionTests(unittest.TestCase):
    def test_collisions(self):
        flagged = collisions((line("hour < 7"), line("hour >= 5", value="standard")))
        self.assertEqual(flagged, [{"line": 2, "by": 1, "text": "line 2 is shadowed by line 1 at hours 5–6"}])
        self.assertEqual(collisions((line("hour < 7"), line("hour >= 7", value="standard"))), [])
        self.assertEqual(collisions((line("hour >= 22 or hour < 6", value="standard"), line("hour >= 22 or hour < 6"))),
                         [{"line": 2, "by": 1, "text": "line 2 is shadowed by line 1 at hours 0–5, 22–23"}])
        self.assertEqual(collisions((line("session is a"), line("session is b", value="standard"))), [])
        self.assertEqual(collisions((line(""), line("session is b", value="standard"))),
                         [{"line": 2, "by": 1, "text": "line 2 is shadowed by line 1"}])
        self.assertEqual(collisions((line("hour < 7"), line("hour < 9", value="standard"),
                                     line("hour < 8", value="priority"))),
                         [{"line": 2, "by": 1, "text": "line 2 is shadowed by line 1 at hours 0–6"},
                          {"line": 3, "by": 2, "text": "line 3 is shadowed by line 2 at hours 0–7"}])
        # a different downstream or key never collides
        self.assertEqual(collisions((line("hour < 7"), line("hour < 9", ds="lemmi", value="standard"))), [])
        self.assertEqual(collisions((line("hour < 7"), line("hour < 9", key="model", value="glm-x"))), [])
        # equal values are not flagged
        self.assertEqual(collisions((line("hour < 7"), line("hour < 9"))), [])


class ValidationTests(unittest.TestCase):
    def j(self, **kw):
        return {"when": "", "downstream": "valar", "tier": None, "set": "window", "value": "priority", **kw}

    def test_validation(self):
        for item, frag in ((self.j(downstream="nope"), "settings line 1: .*nope"),
                           (self.j(downstream="lemmi"), "settings line 1: .*valar"),
                           (self.j(value="flex"), "settings line 1: .*window"),
                           (self.j(when="label is coding"), "settings line 1: .*label"),
                           (self.j(tier="opus2"), "settings line 1: .*tier"),
                           (self.j(set="speed"), "settings line 1: .*set"),
                           (self.j(set="model", value=""), "settings line 1: .*value"),
                           (self.j(when="hour >"), "settings line 1: .*col"),
                           ("no", "settings line 1")):
            with self.assertRaisesRegex(ValueError, frag):
                settings_from_json([item], DS)
        # window on valar with a model key is fine, and line numbers count from the top
        ok = settings_from_json([self.j(set="model", value="glm-x"), self.j()], DS)
        self.assertEqual(ok, (line("", key="model", value="glm-x"), line("")))

    def test_round_trip(self):
        want = (line("hour < 7", key="model", value="glm-x"), line("session is a", value="standard"))
        self.assertEqual(settings_to_json(want),
                         [{"when": "hour < 7", "downstream": "valar", "tier": None, "set": "model", "value": "glm-x"},
                          {"when": "session is a", "downstream": "valar", "tier": None, "set": "window",
                           "value": "standard"}])
        self.assertEqual(settings_from_json(settings_to_json(want), DS), want)


class SessionLineTests(unittest.TestCase):
    def test_is_session_line(self):
        self.assertEqual(is_session_line(line("session is a or session is b")), frozenset(("a", "b")))
        self.assertEqual(is_session_line(line("session is a")), frozenset(("a",)))
        self.assertIsNone(is_session_line(line("")))
        self.assertIsNone(is_session_line(line("hour < 7")))
        self.assertIsNone(is_session_line(line("session is a and hour < 7")))


class PruneTests(unittest.TestCase):
    NOW = 1000000000.0

    def lines(self):
        return (line("session is old or session is new"),
                line("session is old", key="model", value="glm-x"),
                line("hour < 7"))

    def test_prune(self):
        seen = {"old": self.NOW - 8 * 86400, "new": self.NOW - 1 * 86400}
        out = prune(self.lines(), lambda sid: seen.get(sid), self.NOW)
        self.assertEqual(out, (line("session is new"), line("hour < 7")))
        # an unknown id is kept, a non-session line untouched
        out = prune((line("session is ghost"),) + self.lines()[2:], lambda sid: None, self.NOW)
        self.assertEqual(out, (line("session is ghost"), line("hour < 7")))


class UpsertTests(unittest.TestCase):
    def test_upsert_and_reset(self):
        s = upsert_session((), "s1", "valar", None, "window", "priority")
        self.assertEqual(s, (line("session is s1"),))
        s = upsert_session(s, "s1", "valar", None, "window", "standard")
        self.assertEqual((len(s), s[0].value), (1, "standard"))
        s = upsert_session(s, "s1", "valar", "opus", "window", "priority")
        self.assertEqual(len(s), 2)
        s = upsert_session(s, "s1", "valar", "opus", "window", None)
        self.assertEqual((len(s), s[0].tier), (1, None))
        # reset takes the sid out of a two-session line and drops lines left empty
        s = (line("session is s1 or session is s2"),) + s
        s = reset_session(s, "s1", "valar")
        self.assertEqual(s, (line("session is s2"),))
        # a session line for another downstream keeps the sid
        s = (line("session is s1", ds="lemmi"), line("session is s1 or session is s2"))
        self.assertEqual(reset_session(s, "s1", "valar"), (line("session is s1", ds="lemmi"), line("session is s2")))


class DescribeTests(unittest.TestCase):
    def test_describe(self):
        self.assertIsNone(describe({}))
        s = line("hour < 7")
        d = json.loads(describe({"model": ("glm-x", line("", key="model", value="glm-x")), "window": ("priority", s)}))
        self.assertEqual(d, [{"set": "window", "value": "priority", "when": "hour < 7"},
                              {"set": "model", "value": "glm-x", "when": ""}])


class LoadFallbackTests(unittest.TestCase):
    def test_bad_settings_falls_back(self):
        o = config_to_json(DEFAULT_CONFIG)
        o["settings"] = [dict(when="", downstream="nope", tier=None, set="window", value="priority")]
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        with open(os.path.join(d, "console.json"), "w") as f:
            json.dump(o, f)
        cfg, banner = load_console_config(os.path.join(d, "console.json"))
        self.assertEqual(cfg, DEFAULT_CONFIG)
        self.assertIn("couldn't be read", banner)


if __name__ == "__main__":
    unittest.main()
