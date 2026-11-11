import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import advisor as A

DEF = {"type": "advisor_20260301", "name": "advisor", "model": "claude-opus-5-5", "defer_loading": True}


def call(i="s1"):
    return {"type": "server_tool_use", "id": i, "name": "advisor", "input": {}}


def res(i="s1", c=None):
    return {"type": "advisor_tool_result", "tool_use_id": i,
            "content": c or {"type": "advisor_result", "text": "do X", "stop_reason": "end_turn"}}


def txt(t):
    return {"type": "text", "text": t}


def req(content, tools=None):
    return {"tools": tools if tools is not None else [{"name": "Bash"}, DEF],
            "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": content}]}


class AdvisorTest(unittest.TestCase):
    def test_definition_found_and_missing(self):
        self.assertEqual(A.definition(req([])), DEF)
        self.assertIsNone(A.definition({"tools": [{"name": "Bash"}]}))

    def test_to_client_form_swaps_definition(self):
        r = req([])
        self.assertTrue(A.to_client_form(r))
        self.assertEqual(r["tools"], [{"name": "Bash"}, A.TOOL])

    def test_to_client_form_splits_a_pair(self):
        r = req([txt("a"), call(), res(), txt("b")])
        A.to_client_form(r)
        self.assertEqual(r["messages"][1:], [
            {"role": "assistant", "content": [txt("a"), {"type": "tool_use", "id": "s1", "name": "advisor", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "s1", "content": "do X"}]},
            {"role": "assistant", "content": [txt("b")]}])

    def test_pair_last_and_several(self):
        r = req([call("s1"), res("s1"), txt("m"), call("s2"), res("s2")])
        A.to_client_form(r)
        self.assertEqual([m["role"] for m in r["messages"]], ["user", "assistant", "user", "assistant", "user"])
        self.assertEqual(r["messages"][3]["content"][0], txt("m"))
        self.assertEqual(r["messages"][4]["content"][0]["tool_use_id"], "s2")

    def test_to_client_form_error_and_redacted(self):
        r = req([call(), res(c={"type": "advisor_tool_result_error", "error_code": "unavailable"})])
        A.to_client_form(r)
        self.assertEqual(r["messages"][2]["content"][0],
                         {"type": "tool_result", "tool_use_id": "s1", "content": "advisor unavailable", "is_error": True})
        r = req([call(), res(c={"type": "advisor_redacted_result"})])
        A.to_client_form(r)
        self.assertEqual(r["messages"][2]["content"][0]["content"], "(advice redacted)")

    def test_to_client_form_drops_orphans(self):
        r = req([txt("a"), res("x"), call("y")], tools=[])
        self.assertTrue(A.to_client_form(r))
        self.assertEqual(r["messages"][1]["content"], [txt("a")])

    def test_to_client_form_untouched_without_advisor(self):
        r = {"tools": [{"name": "Bash"}], "messages": [{"role": "user", "content": "hi"}]}
        before = copy.deepcopy(r)
        self.assertFalse(A.to_client_form(r))
        self.assertEqual(r, before)

    def test_advice_request_shape(self):
        r = req([txt("a")])
        r["thinking"] = {"type": "enabled"}
        before = copy.deepcopy(r)
        b = A.advice_request(r, [txt("work")], "claude-opus-5-5")
        self.assertEqual(r, before)
        self.assertEqual((b["model"], b["max_tokens"], b["stream"], b["tool_choice"]),
                         ("claude-opus-5-5", 4096, False, {"type": "none"}))
        self.assertNotIn("thinking", b)
        self.assertEqual(b["messages"][-1]["content"][0]["text"], A.PROMPT)
        self.assertEqual(b["messages"][-2], {"role": "assistant", "content": [txt("work")]})

    def test_continue_request_shape(self):
        r = req([txt("a")], tools=[{"name": "Bash"}, A.TOOL])
        b = A.continue_request(r, [txt("w")], "s9", "go", True, False)
        self.assertEqual(b["messages"][-2]["content"][-1], {"type": "tool_use", "id": "s9", "name": "advisor", "input": {}})
        self.assertEqual(b["messages"][-1]["content"][0],
                         {"type": "tool_result", "tool_use_id": "s9", "content": "go", "is_error": True})
        self.assertFalse(any(t.get("name") == "advisor" for t in b["tools"]))
        b = A.continue_request(r, [], "s9", "go", False, True)
        self.assertTrue(any(t.get("name") == "advisor" for t in b["tools"]))

    def test_advice_text_empty(self):
        self.assertEqual(A.advice_text({"content": [{"type": "tool_use"}]}), "(no advice)")
        self.assertEqual(A.advice_text({"content": [txt("a"), txt("b")]}), "a\nb")


if __name__ == "__main__":
    unittest.main()
