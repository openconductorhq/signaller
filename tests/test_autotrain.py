"""autotrain: transcripts to labelling candidates. Synthetic transcripts only, never the real ~/.claude."""
import json
import os
import subprocess
import sys

import autophase
import autotrain


def write_transcript(dir, sid, events):
    os.makedirs(dir, exist_ok=True)
    p = os.path.join(dir, sid + ".jsonl")
    with open(p, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return p


def user(text, **kw):
    e = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]},
         "uuid": "u" + text[:8], "promptSource": "typed", "timestamp": "2026-11-08T10:00:00Z"}
    e.update(kw)
    return e


def asst(mid):
    return {"type": "assistant", "uuid": mid,
            "message": {"role": "assistant", "id": mid, "content": [{"type": "text", "text": "working on it"}]}}


def test_candidates_skip_follow_ons_tags_and_commands(tmp_path):
    p = write_transcript(tmp_path, "s1", [
        user("let's brainstorm a router mode"), asst("m1"),
        user("ok"), asst("m2"),
        user("#execution fix it"), asst("m3"),
        user("<command-name>/superpowers:brainstorming</command-name>"), asst("m4")])
    cs = autotrain.candidates("proj", p)
    assert len(cs) == 1 and cs[0].idx == 0 and cs[0].id == "s1:0"
    assert cs[0].project == "proj" and cs[0].session == "s1"
    assert cs[0].ts == "2026-11-08T10:00:00Z"
    assert cs[0].chars == min(len("let's brainstorm a router mode"), autotrain.LABEL_CHARS)


def test_candidates_idx_matches_replay(tmp_path):
    texts = ["first turn, think about the design of the router",
             "second turn, plan the implementation carefully",
             "third turn, write the code and run the tests"]
    events = []
    for i, t in enumerate(texts):
        events += [user(t), asst("m%d" % i)]
    p = write_transcript(tmp_path, "s2", events)
    cs = autotrain.candidates("proj", p)
    assert [c.idx for c in cs] == [0, 1, 2] and cs[-1].id == "s2:2"
    assert autotrain.turn_text(p, 2) == texts[2]
    assert autotrain.turn_text(p, 9) is None


def test_projects_names_from_cwd(tmp_path):
    d = tmp_path / "claude" / "projects" / "-x-city-os"
    d.mkdir(parents=True)
    (d / "s9.jsonl").write_text(json.dumps({"cwd": "/x/city-os"}) + "\n")
    (tmp_path / "claude" / "projects" / "-x-empty").mkdir()  # no *.jsonl: skipped
    ps = autotrain.projects(str(tmp_path / "claude"))
    assert len(ps) == 1 and ps[0]["dir"] == "-x-city-os"
    assert ps[0]["name"] == "city-os" and ps[0]["sessions"] == 1 and ps[0]["newest"] > 0


def test_candidates_streams_large_file(tmp_path):
    pad = "x" * 200000
    events = [user("real request: design the cache layer for me"), asst("m1")]
    for i in range(30):  # ~6 MB of tool results
        events.append({"type": "user", "uuid": "t%d" % i,
                       "message": {"role": "user", "content": [{"type": "tool_result", "content": pad}]}})
    events += [user("final turn: fix the failing parser test now"), asst("m2")]
    p = write_transcript(tmp_path, "s3", events)
    assert os.path.getsize(p) > 5 * 1024 * 1024
    cs = autotrain.candidates("proj", p)
    assert [c.idx for c in cs] == [0, 1]


def test_tool_still_runs():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # ruling B: import the tool (needs no scikit-learn) instead of running its selftest
    r = subprocess.run(
        [sys.executable, "-c",
         "import auto_train as t; "
         "assert t.replay and t.follow_on and t.split and t._events and t._is_human and t.FOLLOW"],
        env={**os.environ, "PYTHONPATH": os.path.join(root, "tools")},
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def _item(sess, idx, ts, chars=4000):
    return autotrain.Item(id="%s:%d" % (sess, idx), project="proj", session=sess, idx=idx, ts=ts, chars=chars)


def test_estimate_matches_haiku_rate():
    items = [_item("a", 0, "2026-11-08T10:00:00Z"), _item("b", 0, "2026-11-08T10:01:00Z")]
    assert round(autotrain.estimate_usd(items), 6) == round(2 * ((1000 + 300) * 0.10 + 5 * 0.50) / 1e6, 6)


def test_cut_keeps_newest_whole_sessions():
    items = [_item("old", 0, "2026-11-08T10:00:00Z"), _item("new1", 0, "2026-11-08T10:02:00Z"),
             _item("new2", 0, "2026-11-08T10:01:00Z")]
    cap = autotrain.estimate_usd(items[:1]) * 2 + 1e-9  # fits the two newest, not the third
    kept = autotrain.cut(items, cap)
    assert {i.session for i in kept} == {"new1", "new2"}
    assert sorted(i.id for i in autotrain.cut(items, autotrain.estimate_usd(items))) == sorted(i.id for i in items)


def test_fit_separates_words():
    rows = [{"text": "spec the plan for feature %d" % i, "label": "planning"} for i in range(40)]
    rows += [{"text": "fix the failing test in module %d" % i, "label": "execution"} for i in range(40)]
    m = autotrain.fit(rows)
    assert m["format"] == autophase.FORMAT
    model = autophase.Model(m["bias"], m["weights"])
    assert model.score("write the spec") >= 0.5
    assert model.score("fix the test") < 0.5


def test_fit_one_class_is_safe(tmp_path):
    rows = [{"text": "fix the failing test in module %d" % i, "label": "execution"} for i in range(40)]
    m = autotrain.fit(rows)  # no exception
    assert m["format"] == autophase.FORMAT
    p = write_transcript(tmp_path, "s1", [user("now edit src/app.py to fix it"), asst("m1")])
    rows = [{"session": "s1", "idx": 0, "label": "execution"}] * 40
    r = autotrain.evaluate(rows, {"s1": p}, m, autophase.AutoSettings())
    assert r["verdict"] == "too few"


def test_evaluate_counts(tmp_path):
    edit = {"type": "assistant", "uuid": "m2",
            "message": {"role": "assistant", "id": "m2",
                        "content": [{"type": "tool_use", "id": "t1", "name": "Edit",
                                     "input": {"file_path": "/x/src/app.py"}}]}}
    # the edit ends planning before the next turn opens, so that turn is decided on an ended-planning history
    p = write_transcript(tmp_path, "s1", [
        user("<command-name>superpowers:brainstorming</command-name>"), asst("m1"), edit,
        user("now write the code for it")])
    rows = [{"session": "s1", "idx": 0, "label": "planning"}, {"session": "s1", "idx": 1, "label": "execution"}]
    r = autotrain.evaluate(rows, {"s1": p}, {"bias": 0.0, "weights": {}}, autophase.AutoSettings())
    assert r["rules"]["planning_kept"] == 1 and r["rules"]["execution_moved"] == 1
    assert r["model"]["planning_kept"] == 1 and r["model"]["execution_moved"] == 1
    assert r["planning"] == 1 and r["execution"] == 1
    assert r["verdict"] == "too few"  # one planning turn: too few to judge
