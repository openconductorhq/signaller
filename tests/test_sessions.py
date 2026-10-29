import json
import os
import sqlite3
import time

import sessions
from sessions import SessionDirectory, claude_dir, find_transcript, label
from store import Store


def _transcript(root, sid, lines, folder="-tmp-x"):
    d = os.path.join(root, "projects", folder)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, sid + ".jsonl"), "w") as f:
        f.write("\n".join(lines) + "\n")


def test_find_transcript(tmp_path):
    root = str(tmp_path)
    _transcript(root, "abc", [json.dumps({"type": "x"}), "not json", json.dumps({"cwd": "/tmp/x", "gitBranch": "main"})])
    assert find_transcript(root, "abc") == ("/tmp/x", "main")
    assert find_transcript(root, "nope") == (None, None)
    os.makedirs(os.path.join(root, "projects", "-d", "dir.jsonl"))
    assert find_transcript(root, "dir") == (None, None)


def test_label():
    assert label("abcdefghij", "/a/b/proj", "main") == "proj · main"
    assert label("abcdefghij", "/a/b/proj", "") == "proj"
    assert label("abcdefghij", None, "main") == "abcdefgh"
    assert label(None, None, None) == "unknown session"


def test_claude_dir(tmp_path):
    assert claude_dir({"CLAUDE_CONFIG_DIR": str(tmp_path)}) == str(tmp_path)
    assert claude_dir({}) == os.path.expanduser("~/.claude")


class Fake:
    def __init__(self): self.calls = []
    def touch_session(self, *a): self.calls.append(a)


def test_directory_retries_every_30s(tmp_path, monkeypatch):
    looked = []
    monkeypatch.setattr(sessions, "find_transcript", lambda d, s: looked.append(s) or (None, None))
    now, st = [100.0], Fake()
    sd = SessionDirectory(st, str(tmp_path), clock=lambda: now[0])
    sd.note("s", now[0]); sd._q.join()
    now[0] = 110; sd.note("s", now[0]); sd._q.join()
    assert len(looked) == 1 and len(st.calls) == 2
    now[0] = 131; sd.note("s", now[0]); sd._q.join()
    assert len(looked) == 2


def test_directory_stores_found_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "find_transcript", lambda d, s: ("/p", "b"))
    st = Fake()
    sd = SessionDirectory(st, str(tmp_path))
    sd.note("s", 5.0); sd._q.join()
    assert ("s", 5.0, "/p", "b") in st.calls
    sd.note("s", 6.0); sd._q.join()  # known now: no more lookups
    assert [c for c in st.calls if len(c) == 4] == [("s", 5.0, "/p", "b")]


def test_touch_session_upserts(tmp_path):
    p = str(tmp_path / "s.db")
    s = Store(p)
    s.touch_session("s", 10.0)
    s.touch_session("s", 5.0, "/c", "br")
    s.touch_session("s", 20.0)
    s.close(); s._t.join(5)
    r = sqlite3.connect(p).execute("SELECT id,cwd,branch,first_ts,last_ts FROM sessions").fetchall()
    assert r == [("s", "/c", "br", 5.0, 20.0)]
