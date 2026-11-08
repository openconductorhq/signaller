"""autotrain: transcripts to labelling candidates. Synthetic transcripts only, never the real ~/.claude."""
import json
import os
import subprocess
import sys
import threading
import time

import pytest

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
    assert autophase.human_text(autotrain.turns_at(p, {2})[2][-1]) == texts[2]
    assert autotrain.turns_at(p, {9}) == {}


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
         "assert t.replay and t.follow_on and t.split and t.events"],
        env={**os.environ, "PYTHONPATH": os.path.join(root, "tools")},
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def _item(sess, idx, ts, chars=4000):
    return autotrain.Item(id="%s:%d" % (sess, idx), project="proj", session=sess, idx=idx, ts=ts, chars=chars)


def _trainer(tmp_path, cap=5.0):
    cfg = tmp_path / "cfg"
    cfg.mkdir(exist_ok=True)
    return autotrain.Trainer(str(cfg), str(tmp_path / "claude"), lambda: autophase.AutoSettings(train_cap=cap))


def _seed(tmp_path, pdir="p", sessions=1, turns=2, pad=""):
    """sessions transcripts of turns human turns each under <claude>/projects/<pdir>."""
    d = tmp_path / "claude" / "projects" / pdir
    d.mkdir(parents=True, exist_ok=True)
    for s in range(sessions):
        events = []
        for t in range(turns):
            events += [user("please plan the design of module %d %s" % (t, pad)), asst("m%d" % t)]
        write_transcript(d, "s%d" % s, events)
    return d


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


def test_trainer_cut_block(tmp_path):
    # ruling A: job["cut"] is {"items", "usd"} for the newest-sessions-under-cap option when the
    # full job would cost more than the cap, else null
    claude = tmp_path / "claude"
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    d = claude / "projects" / "p"
    d.mkdir(parents=True)
    for sid, ts in (("old", "2026-11-08T10:00:00Z"), ("new", "2026-11-08T11:00:00Z")):
        events = []
        for t in range(3):
            events += [user("please plan the design of module %d " % t + "x" * 6000, timestamp=ts), asst("m%d" % t)]
        write_transcript(d, sid, events)
    tr = autotrain.Trainer(str(cfg), str(claude), lambda: autophase.AutoSettings(train_cap=0.001))
    newest = [i for i in autotrain.candidates("p", str(d / "new.jsonl"))]
    job = tr.plan(["p"], False)
    assert job["items"] == 6 and job["usd_est"] > 0.001  # over the cap: the option is shown, not applied
    assert job["cut"] == {"items": 3, "usd": round(autotrain.estimate_usd(newest), 4)}
    job = tr.plan(["p"], True)
    assert job["cut"] == {"items": job["items"], "usd": job["usd_est"]}
    assert job["items"] == 3 and job["usd_est"] <= 0.001
    got = tr.next(50)
    assert {x["id"].split(":")[0] for x in got["items"]} == {"new"}


def test_next_keeps_going_past_a_vanished_transcript(tmp_path):
    d = tmp_path / "claude" / "projects" / "p"
    d.mkdir(parents=True)
    for sid, turns in (("a", 25), ("b", 5)):
        events = []
        for t in range(turns):
            events += [user("please plan the design of module %d" % t), asst("m%d" % t)]
        write_transcript(d, sid, events)
    tr = _trainer(tmp_path)
    tr.plan(["p"], False)
    os.remove(d / "a.jsonl")
    got = tr.next(20)  # a's items fail, but the batch must still fill from what is left
    assert sorted(x["id"] for x in got["items"]) == sorted("b:%d" % i for i in range(5))
    job = tr.status()["job"]
    assert job["failed"] == 25 and job["remaining"] == 5  # served, not labelled yet


def test_plan_sees_a_project_created_after_the_first_scan(tmp_path):
    _seed(tmp_path, sessions=1)
    tr = _trainer(tmp_path)
    tr._scan()
    assert [p["dir"] for p in tr.status()["projects"]] == ["p"]
    _seed(tmp_path, pdir="q", sessions=1)
    job = tr.plan(["q"], False)  # the plan re-lists the projects, so q is plannable without a restart
    assert job["items"] == 2


def test_finish_answers_before_the_fit_ends(tmp_path, monkeypatch):
    _seed(tmp_path, sessions=1, turns=2)
    tr = _trainer(tmp_path)
    tr.plan(["p"], False)
    ids = [x["id"] for x in tr.next(2)["items"]]
    tr.labels([{"id": i, "label": "planning" if n % 2 == 0 else "execution"} for n, i in enumerate(ids)],
              [], {"input_tokens": 1, "output_tokens": 1})
    gate = threading.Event()

    def slow_fit(rows):
        gate.wait(5)
        return {"format": autophase.FORMAT, "bias": 0.0, "weights": {"plan": 0.4}}
    monkeypatch.setattr(autotrain, "fit", slow_fit)
    t0 = time.time()
    tr.finish()
    assert time.time() - t0 < 1 and tr.status()["state"] == "training"  # answered at once, the fit still runs
    gate.set()
    t0 = time.time()
    while tr.status()["state"] != "done" and time.time() - t0 < 5:
        time.sleep(0.01)
    assert tr.status()["state"] == "done"


def test_plan_conflicts_with_a_running_job(tmp_path):
    _seed(tmp_path, sessions=2, turns=1)
    _seed(tmp_path, pdir="q", sessions=1)
    tr = _trainer(tmp_path)
    tr.plan(["p"], False)
    ids = [x["id"] for x in tr.next(1)["items"]]
    tr.labels([{"id": ids[0], "label": "planning"}], [], {"input_tokens": 1, "output_tokens": 1})
    with pytest.raises(autotrain.Conflict):  # progress for different projects, and for a different cut choice
        tr.plan(["q"], False)
    with pytest.raises(autotrain.Conflict):
        tr.plan(["p"], True)
    job = tr.plan(["p"], False)  # the same projects and cut choice: a resume
    assert (job["items"], job["labelled"], job["remaining"]) == (2, 1, 1)
    tr.state = "training"
    with pytest.raises(autotrain.Conflict):  # while the fit runs, or a result waits: use or discard first
        tr.plan(["p"], False)
    tr.state = "done"
    with pytest.raises(autotrain.Conflict):
        tr.plan(["p"], False)


def test_cut_is_null_when_no_whole_session_fits(tmp_path):
    _seed(tmp_path, sessions=2, turns=2)
    tr = _trainer(tmp_path, cap=1e-7)  # below even one session's cost: the option is empty, not a 0-item cut
    job = tr.plan(["p"], False)
    assert job["cut"] is None and job["items"] == 4 and job["usd_est"] > 1e-7
    job = tr.plan(["p"], True)
    assert job["cut"] is None and job["cut_applied"] is False and job["items"] == 4


def test_plan_counts_only_unlabelled_prompts(tmp_path):
    _seed(tmp_path, sessions=2, turns=2, pad="x" * 6000)
    tr = _trainer(tmp_path, cap=0.0005)  # over the 4 prompts' cost, under the 2 that still need paying for
    tr.plan(["p"], False)
    done = sorted(x["id"] for x in tr.next(2)["items"])
    tr.labels([{"id": i, "label": "planning"} for i in done], [], {"input_tokens": 1, "output_tokens": 1})
    job = tr.plan(["p"], False)
    assert (job["items"], job["labelled"], job["failed"], job["remaining"]) == (4, 2, 0, 2)
    unlabelled = [i for i in tr.job["items"].values() if i.id not in tr.job["labelled"]]
    assert job["usd_est"] == round(autotrain.estimate_usd(unlabelled), 4) == job["usd_remaining"]
    assert job["cut"] is None  # what's left fits: the cap never counts prompts already paid for


def test_use_refuses_too_few_and_weightless(tmp_path):
    tr = _trainer(tmp_path)
    tr.state, tr.model, tr.result = "done", {"bias": 0.0, "weights": {}}, {"verdict": "too few"}
    with pytest.raises(ValueError):
        tr.use()
    tr.result = {"verdict": "better"}  # no weights: nothing to install
    with pytest.raises(ValueError):
        tr.use()
    tr.model = {"bias": 0.0, "weights": {"plan": 0.4}}
    assert tr.use() == tr.model


def test_scan_skips_a_broken_transcript_and_names_a_broken_scan(tmp_path, monkeypatch):
    def boom(*a):
        raise TypeError("bad")
    _seed(tmp_path, sessions=2)
    tr = _trainer(tmp_path)
    monkeypatch.setattr(autotrain, "candidates", boom)
    tr.state = "scanning"  # as start_scan leaves it
    tr._scan()
    st = tr.status()  # the broken transcripts are skipped and counted as nothing, the scan still finishes
    assert st["state"] == "ready" and st["projects"][0]["prompts"] == 0
    monkeypatch.undo()
    tr.reset()
    monkeypatch.setattr(autotrain, "projects", boom)
    tr.state = "scanning"
    tr._scan()
    # a scan that dies outright is a message, never a stuck "scanning" (status itself can't list now either)
    assert tr.state == "failed" and "TypeError" in tr.error


def test_labels_rejects_bad_usage(tmp_path):
    _seed(tmp_path, sessions=1, turns=2)
    tr = _trainer(tmp_path)
    tr.plan(["p"], False)
    ids = [x["id"] for x in tr.next(2)["items"]]
    for usage in ({"input_tokens": -1, "output_tokens": 0},
                  {"input_tokens": float("nan")},
                  {"input_tokens": float("inf"), "output_tokens": 2}):
        with pytest.raises(ValueError):
            tr.labels([{"id": ids[0], "label": "planning"}], [], usage)


def test_evaluate_counts_only_scored_rows(tmp_path):
    d = _seed(tmp_path, sessions=1, turns=3)
    rows = [{"session": "train", "idx": i, "label": "planning"} for i in range(30)]
    rows += [{"session": "gone", "idx": i, "label": "planning"} for i in range(6)]
    rows += [{"session": "s0", "idx": i, "label": "planning"} for i in range(3)]
    paths = {"gone": str(tmp_path / "nope.jsonl"), "s0": str(d / "s0.jsonl")}
    r = autotrain.evaluate(rows, paths, {"bias": 0.0, "weights": {}}, autophase.AutoSettings())
    # the vanished session's 6 labels were never decided: only the 3 that scored count, so too few to judge
    assert (r["planning"], r["execution"], r["verdict"]) == (3, 0, "too few")
