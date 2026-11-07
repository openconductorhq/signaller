import pytest
from autophase import AutoSettings, Model, drop_old_thinking, human_index, phase, turn_index

S = AutoSettings()
def skill(name): return {"role": "assistant", "content": [{"type": "tool_use", "name": "Skill", "input": {"skill": name}}]}
def edit(path): return {"role": "assistant", "content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": path}}]}
def user(t): return {"role": "user", "content": t}

def test_no_history_no_model_is_none():
    assert phase([user("hello")], S, None)[0] == "none"

def test_planning_skill_opens_planning_for_terse_replies():
    p = phase([user("x"), skill("superpowers:brainstorming"), user("2")], S, None)
    assert p == ("planning", "phase: superpowers:brainstorming")

def test_typed_slash_command_opens_planning():
    m = [user("<command-name>/superpowers:brainstorming</command-name>\n<command-args>auto</command-args>"), user("yes")]
    assert phase(m, S, None)[0] == "planning"

def test_spec_edit_keeps_planning_code_edit_ends_it_one_way():
    base = [user("x"), skill("superpowers:brainstorming"), edit("/r/docs/superpowers/specs/a.md"), user("ok")]
    assert phase(base, S, None)[0] == "planning"
    ended = base + [edit("/r/app.py"), user("next"), skill("superpowers:writing-plans"), user("go")]
    assert phase(ended, S, None) == ("execution", "phase ended: edit /r/app.py")

def test_execution_skill_ends_planning():
    m = [user("x"), skill("superpowers:writing-plans"), skill("superpowers:subagent-driven-development"), user("go")]
    assert phase(m, S, None)[0] == "execution"
    assert phase(m, S, None)[1] == "phase ended: superpowers:subagent-driven-development"

def test_model_opens_planning_above_threshold_only():
    m = [user("let us spec the export flow"), user("2")]
    assert phase(m, S, lambda t: 0.9 if "spec" in t else 0.1) == ("planning", "model 0.90")
    assert phase(m, S, lambda t: 0.5)[0] == "none"

def test_malformed_history_is_ignored():
    m = [{"role": "assistant", "content": "text"}, {"role": "assistant", "content": [1, {"type": "tool_use", "name": "Skill", "input": {}}]},
         {"role": "assistant", "content": [{"type": "tool_use", "name": "Edit", "input": {}}]}, user("hi")]
    assert phase(m, S, None)[0] == "none"

def test_human_index_skips_reminders_and_tool_results():
    m = [user("a"), {"role": "user", "content": [{"type": "text", "text": "<system-reminder>x"}]},
         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1", "content": "ok"}]}]
    assert human_index(m) == 0 and human_index([]) == -1

def test_model_load_and_score(tmp_path):
    p = tmp_path / "m.json"
    p.write_text('{"format": "signaller-auto-1", "bias": -2.0, "weights": {"spec": 3.0, "write spec": 1.0}}')
    m = Model.load(str(p))
    assert m.score("write spec now") > 0.85 and m.score("fix the test") < 0.2

def test_model_load_rejects_bad_files(tmp_path):
    p = tmp_path / "m.json"; p.write_text('{"format": "other"}')
    with pytest.raises(ValueError): Model.load(str(p))
    with pytest.raises(FileNotFoundError): Model.load(str(tmp_path / "none.json"))

def test_model_load_rejects_non_finite_weights(tmp_path):
    p = tmp_path / "m.json"; p.write_text('{"format": "signaller-auto-1", "bias": 0, "weights": {"a": NaN}}')
    with pytest.raises(ValueError): Model.load(str(p))
    p.write_text('{"format": "signaller-auto-1", "bias": 0, "weights": {"a": 1%s}}' % ("0" * 400))
    with pytest.raises(ValueError): Model.load(str(p))

def test_drop_old_thinking_keeps_the_current_turn():
    req = {"messages": [user("a"), {"role": "assistant", "content": [{"type": "thinking", "thinking": "t", "signature": "s"}, {"type": "text", "text": "r"}]},
                        user("b"), {"role": "assistant", "content": [{"type": "thinking", "thinking": "t2", "signature": "s2"}, {"type": "tool_use", "id": "1", "name": "Bash", "input": {}}]},
                        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1", "content": "ok"}]}]}
    assert drop_old_thinking(req) is True
    assert [b["type"] for b in req["messages"][1]["content"]] == ["text"]
    assert req["messages"][3]["content"][0]["type"] == "thinking"  # after the latest human message: kept
    assert drop_old_thinking(req) is False


def test_claude_own_files_do_not_end_planning():
    base = [user("x"), skill("superpowers:brainstorming")]
    for p in ("/private/tmp/claude-502/proj/sess/scratchpad/notes.md", "/Users/x/.claude/projects/p/memory/a.md", "/r/.superpowers/sdd/b.md"):
        assert phase(base + [edit(p), user("ok")], S, None)[0] == "planning"
    for p in ("/r/app.py", "/r/.claude/worktrees/w/app.py"):
        assert phase(base + [edit(p), user("ok")], S, None)[0] == "execution"


def test_drop_old_thinking_downstream_only_and_edges():
    signed = {"type": "thinking", "thinking": "t", "signature": "A" * 452}
    red = {"type": "redacted_thinking", "data": "d"}
    ds = {"type": "thinking", "thinking": "t", "signature": ""}
    only = {"role": "assistant", "content": [dict(ds)]}
    msgs = [user("a"), {"role": "assistant", "content": [signed, red, ds, {"type": "text", "text": "r"}]}, user("b"), only, user("c")]
    req = {"messages": msgs}
    assert drop_old_thinking(req) is True
    assert [b["type"] for b in msgs[1]["content"]] == ["thinking", "redacted_thinking", "text"]  # Anthropic's own stay
    assert only["content"] == [ds]  # would be left empty: kept unchanged
    tr = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1", "content": "ok"}, {"type": "text", "text": "skill body"}]}
    assert turn_index([{"role": "assistant", "content": [ds]}, tr]) == -1
    assert drop_old_thinking({"messages": [{"role": "assistant", "content": [ds, {"type": "text", "text": "r"}]}, tr]}) is False
