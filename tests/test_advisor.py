import copy
import json
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


def sse(*events) -> bytes:
    """Data-only SSE, as some downstreams send it (no event: lines)."""
    return b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)


def ev_list(out) -> list:
    """The JSON events in a list of SSE byte chunks."""
    data = b"".join(out).decode()
    return [json.loads(ln[5:]) for ln in data.split("\n") if ln.startswith("data:")]


def mstart():
    return {"type": "message_start", "message": {"id": "m1", "role": "assistant", "content": [],
                                                 "usage": {"input_tokens": 10, "output_tokens": 1}}}


def bstart(i, block):
    return {"type": "content_block_start", "index": i, "content_block": block}


def delta(i, d):
    return {"type": "content_block_delta", "index": i, "delta": d}


def bstop(i):
    return {"type": "content_block_stop", "index": i}


def mdelta(stop, out=7):
    return {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"output_tokens": out}}


MSTOP = {"type": "message_stop"}


def text_block(i, t="hello"):
    return [bstart(i, {"type": "text", "text": ""}), delta(i, {"type": "text_delta", "text": t}), bstop(i)]


def tool_block(i, name, tid, js="{}"):
    return [bstart(i, {"type": "tool_use", "id": tid, "name": name, "input": {}}),
            delta(i, {"type": "input_json_delta", "partial_json": js}), bstop(i)]


def feed_all(sp, data: bytes, step=None) -> list:
    out = []
    step = step or len(data) or 1
    for k in range(0, len(data), step):
        out += sp.feed(data[k:k + step])
    return out


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


class SplicerTest(unittest.TestCase):
    LEG1 = [mstart(), *text_block(0, "thinking out loud"), *tool_block(1, "advisor", "tu1"),
            mdelta("tool_use", 7), MSTOP]

    def test_passes_plain_stream_through(self):
        data = sse(mstart(), *text_block(0), *tool_block(1, "Bash", "tb", '{"command": "ls"}'),
                   mdelta("tool_use"), MSTOP)
        sp = A.Splicer()
        self.assertEqual(b"".join(feed_all(sp, data, 1)), data)
        self.assertIsNone(sp.call)
        self.assertFalse(sp.broken)

    def test_holds_advisor_call(self):
        sp = A.Splicer()
        out = feed_all(sp, sse(*self.LEG1), 3)
        self.assertEqual(b"".join(out), sse(mstart(), *text_block(0, "thinking out loud")))
        self.assertEqual(sp.call, {"id": "tu1"})
        self.assertFalse(sp.mixed)
        self.assertEqual(sp.partial, [{"type": "text", "text": "thinking out loud"}])
        self.assertEqual(sp.out_tokens, 7)

    def test_advice_events_and_leg2_indices(self):
        sp = A.Splicer()
        feed_all(sp, sse(*self.LEG1))
        adv = ev_list(sp.advice_events("x", "do X", False))
        self.assertEqual(adv, [
            bstart(1, {"type": "server_tool_use", "id": "srvtoolu_x", "name": "advisor", "input": {}}), bstop(1),
            bstart(2, {"type": "advisor_tool_result", "tool_use_id": "srvtoolu_x",
                       "content": {"type": "advisor_result", "text": "do X", "stop_reason": "end_turn"}}), bstop(2)])
        sp.next_leg()
        out = ev_list(feed_all(sp, sse(mstart(), *text_block(0, "ok"), mdelta("end_turn", 5), MSTOP), 2))
        self.assertNotIn("message_start", [e["type"] for e in out])
        self.assertEqual([e.get("index") for e in out if e["type"].startswith("content_block")], [3, 3, 3])
        self.assertEqual(out[-2]["usage"]["output_tokens"], 12)
        self.assertEqual(out[-2]["delta"]["stop_reason"], "end_turn")
        self.assertEqual(out[-1], MSTOP)
        self.assertEqual(sp.out_tokens, 12)
        self.assertIsNone(sp.call)

    def test_advice_events_error_and_sse_form(self):
        sp = A.Splicer()
        feed_all(sp, sse(*self.LEG1))
        out = sp.advice_events("x", None, True)
        self.assertTrue(out[0].startswith(b"event: content_block_start\ndata: "))
        self.assertTrue(all(c.endswith(b"\n\n") for c in out))
        self.assertEqual(ev_list(out)[2]["content_block"]["content"],
                         {"type": "advisor_tool_result_error", "error_code": "unavailable"})

    def test_mixed_tool_calls_keep_the_other_call(self):
        sp = A.Splicer()
        out = feed_all(sp, sse(mstart(), *text_block(0), *tool_block(1, "advisor", "tu1"),
                               *tool_block(2, "Bash", "tb", '{"command": "ls"}'), mdelta("tool_use"), MSTOP), 5)
        self.assertEqual(ev_list(out), [mstart(), *text_block(0)])
        self.assertTrue(sp.mixed)
        self.assertEqual(sp.call, {"id": "tu1"})
        self.assertEqual([e["index"] for e in ev_list(sp.advice_events("x", "do X", False))], [1, 1, 2, 2])
        self.assertEqual(ev_list(sp.rest()), [*tool_block(3, "Bash", "tb", '{"command": "ls"}'),
                                              mdelta("tool_use"), MSTOP])

    def test_mixed_on_second_leg(self):
        """Leg 2 calls the advisor next to Bash: the indices keep counting from leg 1's advice."""
        sp = A.Splicer()
        feed_all(sp, sse(*self.LEG1))
        sp.advice_events("x", "do X", False)
        sp.next_leg()
        out = feed_all(sp, sse(mstart(), *tool_block(0, "advisor", "tu2"), *tool_block(1, "Bash", "tb"),
                               mdelta("tool_use", 4), MSTOP))
        self.assertEqual(out, [])
        self.assertTrue(sp.mixed)
        self.assertEqual([e["index"] for e in ev_list(sp.advice_events("y", "do Y", False))], [3, 3, 4, 4])
        rest = ev_list(sp.rest())
        self.assertEqual([e.get("index") for e in rest], [5, 5, 5, None, None])
        self.assertEqual(rest[-2]["usage"]["output_tokens"], 11)

    def test_bash_before_advisor_is_out_and_mixed(self):
        sp = A.Splicer()
        out = feed_all(sp, sse(mstart(), *tool_block(0, "Bash", "tb"), *tool_block(1, "advisor", "tu1"),
                               mdelta("tool_use"), MSTOP))
        self.assertEqual(ev_list(out), [mstart(), *tool_block(0, "Bash", "tb")])
        self.assertTrue(sp.mixed)
        sp.advice_events("x", "do X", False)
        self.assertEqual(ev_list(sp.rest()), [mdelta("tool_use"), MSTOP])

    def test_max_tokens_cut_call_is_flushed(self):
        sp = A.Splicer()
        held = sse(*tool_block(1, "advisor", "tu1"), mdelta("max_tokens"), MSTOP)
        out = feed_all(sp, sse(mstart(), *text_block(0)) + held, 4)
        self.assertEqual(b"".join(out), sse(mstart(), *text_block(0)))
        self.assertIsNone(sp.call)
        self.assertEqual(b"".join(sp.flush()), held)
        self.assertEqual(sp.flush(), [])

    def test_unparseable_event_breaks_to_passthrough(self):
        sp = A.Splicer()
        good = sse(mstart(), *text_block(0))
        tail = b"data: {bad\n\n" + sse(*tool_block(1, "advisor", "tu1"), mdelta("tool_use"), MSTOP)
        out = feed_all(sp, good + tail, 7)
        self.assertTrue(sp.broken)
        self.assertEqual(b"".join(out), good + tail)
        self.assertIsNone(sp.call)

    def test_break_while_holding_releases_held(self):
        sp = A.Splicer()
        data = sse(mstart(), *tool_block(0, "advisor", "tu1")) + b"data: {bad\n\n" + sse(MSTOP)
        self.assertEqual(b"".join(feed_all(sp, data, 6)), data)
        self.assertTrue(sp.broken)

    def test_partial_thinking_and_tool_input_split(self):
        """thinking keeps its signature; a tool_use input split across deltas (and chunks) is parsed whole."""
        sp = A.Splicer()
        data = sse(mstart(),
                   bstart(0, {"type": "thinking", "thinking": "", "signature": ""}),
                   delta(0, {"type": "thinking_delta", "thinking": "hm"}),
                   delta(0, {"type": "thinking_delta", "thinking": "m"}),
                   delta(0, {"type": "signature_delta", "signature": "SIG"}), bstop(0),
                   bstart(1, {"type": "tool_use", "id": "tr", "name": "Read", "input": {}}),
                   delta(1, {"type": "input_json_delta", "partial_json": '{"file_'}),
                   delta(1, {"type": "input_json_delta", "partial_json": 'path": "/a"}'}), bstop(1),
                   *tool_block(2, "advisor", "tu1"), mdelta("tool_use"), MSTOP)
        feed_all(sp, data, 1)
        self.assertEqual(sp.partial, [{"type": "thinking", "thinking": "hmm", "signature": "SIG"},
                                      {"type": "tool_use", "id": "tr", "name": "Read", "input": {"file_path": "/a"}}])
        self.assertTrue(sp.mixed)

    def test_leg_without_call_after_next_leg(self):
        sp = A.Splicer()
        feed_all(sp, sse(*self.LEG1))
        sp.advice_events("x", "do X", False)
        sp.next_leg()
        self.assertEqual(sp.partial, [])
        out = ev_list(feed_all(sp, sse(mstart(), *tool_block(0, "Bash", "tb"), mdelta("tool_use", 3), MSTOP)))
        self.assertEqual([e.get("index") for e in out], [3, 3, 3, None, None])
        self.assertIsNone(sp.call)
        self.assertEqual(out[-2]["usage"]["output_tokens"], 10)
        self.assertEqual(sp.partial, [{"type": "tool_use", "id": "tb", "name": "Bash", "input": {}}])

    def test_crlf_and_event_lines(self):
        sp = A.Splicer()
        evs = [mstart(), *text_block(0), *tool_block(1, "advisor", "tu1"), mdelta("tool_use"), MSTOP]
        data = b"".join(b"event: " + e["type"].encode() + b"\r\ndata: " + json.dumps(e).encode() + b"\r\n\r\n"
                        for e in evs)
        out = feed_all(sp, data, 3)
        self.assertEqual(sp.call, {"id": "tu1"})
        self.assertEqual(b"".join(out), data[:data.index(b"event: content_block_start\r\ndata: {\"type\": "
                                                         b"\"content_block_start\", \"index\": 1")])


if __name__ == "__main__":
    unittest.main()
