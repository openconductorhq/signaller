import unittest

import pytest

from rulelang import parse, parse_cond, evaluate, fields_used, RuleError


def ev(src, **f):
    defaults = dict(text="", tag=set(), tier="other", model="", tokens=0, subagent=False, teammate=False, servertool=False, label="none")
    defaults.update(f)
    return [(evaluate(l.cond, defaults.__getitem__), l.track) for l in parse(src)]


ERRORS = [
    ("when tag is plan anthropic", 1, 18, "expected 'go'"),
    ("when colour is red go local", 1, 6, "unknown field 'colour'"),
    ("when tokens contains 5 go local", 1, 13, "'contains' needs a text field"),
    ("when text > 5 go local", 1, 11, "'>' needs a number field"),
    ("when text matches /(/ go local", 1, 19, "bad regex"),
    ('when tag is plan go "lemmi"', 1, 21, "expected a track name"),
    ("when tag is plan go", 1, 21, "expected a track name"),
    ("when (tag is plan go local", 1, 19, "expected ')'"),
    ("", 1, 1, "expected 'when'"),
]


class RuleLangTests(unittest.TestCase):
    def test_go_valar(self):
        self.assertEqual(parse("when tag is execution go valar")[0].track, "valar")

    def test_go_any_word(self):
        self.assertEqual(parse("when tag is x go lemmi")[0].track, "lemmi")
        self.assertEqual(parse("when tag is x go Lemmi")[0].track, "lemmi")

    def test_table(self):
        self.assertEqual(ev("when tag is plan go anthropic", tag={"plan"}), [(True, "anthropic")])
        self.assertEqual(ev("when tag is #PLAN go anthropic", tag={"plan"}), [(True, "anthropic")])
        self.assertEqual(ev('when text contains "Schema Mig" go local', text="add schema migration"), [(True, "local")])
        self.assertEqual(ev("when text matches /^(hi|thanks)\\b/i go local", text="Thanks!"), [(True, "local")])
        self.assertEqual(ev("when tokens > 100k go anthropic", tokens=100_001), [(True, "anthropic")])
        self.assertEqual(ev("when tokens >= 100k go anthropic", tokens=100_000), [(True, "anthropic")])
        self.assertEqual(ev("when not subagent and tier is opus go anthropic", tier="opus"), [(True, "anthropic")])
        self.assertEqual(ev("when subagent is no go local"), [(True, "local")])
        self.assertEqual(ev("when teammate go valar", teammate=True), [(True, "valar")])
        self.assertEqual(ev('when text is "a \\"q\\" \\\\" go local', text='A "Q" \\'), [(True, "local")])

    def test_precedence(self):
        self.assertEqual(ev("when tier is haiku or tier is sonnet and tag is x go local", tier="haiku"), [(True, "local")])
        self.assertEqual(ev("when (tier is haiku or tier is sonnet) and tag is x go local", tier="haiku"), [(False, "local")])

    def test_lines_and_case(self):
        r = ev("WHEN tag is plan GO anthropic\n\nwhen tag is local go local")
        self.assertEqual([t for _, t in r], ["anthropic", "local"])

    def test_short_circuit_and_fields(self):
        seen = []

        def get(f):
            seen.append(f)
            return {"tier": "opus", "label": "coding"}[f]

        evaluate(parse("when tier is sonnet and label is coding go local")[0].cond, get)
        self.assertEqual(seen, ["tier"])
        self.assertEqual(fields_used(parse("when label is coding or tokens > 5 go local")), {"label", "tokens"})

    def test_errors(self):
        for src, line, col, frag in ERRORS:
            with self.subTest(src=src):
                with self.assertRaises(RuleError) as cm:
                    parse(src)
                e = cm.exception
                self.assertEqual((e.line, e.col), (line, col))
                self.assertIn(frag, e.message)
                self.assertEqual(str(e), f"line {line}, col {col}: {e.message}")

    def test_long_rule_rejected_not_recursion(self):
        ok = "when " + " or ".join(["tier is opus"] * 200) + " go local"
        self.assertEqual(ev(ok, tier="opus"), [(True, "local")])
        for src in ("when " + " or ".join(["tier is opus"] * 1200) + " go local",
                    "when " + " and ".join(["tier is opus"] * 201) + " go local",
                    "when " + "not " * 201 + "tier is opus go local"):
            with self.assertRaises(RuleError) as cm:
                parse(src)
            self.assertEqual(cm.exception.message, "rule is too long")

    def test_error_line_number(self):
        with self.assertRaises(RuleError) as cm:
            parse("when tag is a go local\n\nwhen nope is a go local")
        self.assertEqual((cm.exception.line, cm.exception.col), (3, 6))


def test_hour_wraps_midnight():
    c = parse_cond("hour >= 22 or hour < 6")
    at = lambda h: evaluate(c, lambda n: h)
    assert at(23) and at(3) and not at(12)

def test_session_is():
    c = parse_cond("session is AbC-1 or session is x2")
    assert evaluate(c, lambda n: "abc-1") and not evaluate(c, lambda n: "zz")

def test_parse_cond_blank_and_error():
    assert parse_cond("  ") is None
    with pytest.raises(RuleError):
        parse_cond("hour >")


if __name__ == "__main__":
    unittest.main()
