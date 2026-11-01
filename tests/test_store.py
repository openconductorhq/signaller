import os
import sqlite3
import threading
import time

import store
from routing import DEFAULT_CONFIG
from store import Store, load_console_config, save_console_config


def req(i, ts, track="anthropic", status=200, **kw):
    return dict(id=i, ts=ts, track=track, status=status, preview="p%d" % i, **kw)


def test_config_roundtrip_and_mode(tmp_path):
    p = str(tmp_path / "sub" / "console.json")
    save_console_config(p, DEFAULT_CONFIG)
    assert os.stat(p).st_mode & 0o777 == 0o600
    assert load_console_config(p) == (DEFAULT_CONFIG, None)


def test_missing_config_defaults(tmp_path):
    assert load_console_config(str(tmp_path / "x.json")) == (DEFAULT_CONFIG, None)


def test_corrupt_config_falls_back(tmp_path):
    p = tmp_path / "console.json"
    p.write_text("{nope")
    cfg, banner = load_console_config(str(p))
    assert cfg == DEFAULT_CONFIG and "console.json.bad" in banner
    assert (tmp_path / "console.json.bad").exists() and not p.exists()


def test_departures_and_stats(tmp_path):
    s, now = Store(str(tmp_path / "s.db")), time.time()
    for i, (t, st) in enumerate([("anthropic", 200)] * 3 + [("local", 502)], 1):
        s.record_request(req(s.next_id(), now - 10 * i, t, st, prompt="full %d" % i))
    s.record_request(req(s.next_id(), now - 7200))
    s.close()
    st = s.stats(now)
    assert (st["anthropic_pct"], st["local_pct"], st["last_hour_total"], st["errors_last_hour"]) == (75, 25, 4, 1)
    assert [r["id"] for r in s.departures()] == [5, 4, 3, 2, 1]
    assert [r["id"] for r in s.departures(since_id=3)] == [5, 4]
    assert [r["id"] for r in s.departures(before_id=3)] == [2, 1]
    assert s.request(4)["track"] == "local" and s.request(99) is None
    assert s.request(4)["prompt"] == "full 4" and s.request(5)["prompt"] is None
    assert Store(str(tmp_path / "s.db")).next_id() == 6


def test_prompts_stored_as_edits(tmp_path):
    p = str(tmp_path / "s.db")
    s, now = Store(p), time.time()
    turns = ["<transcript>\n" + "".join("turn %d %s\n" % (k, "x" * 3000) for k in range(n)) for n in range(1, 41)]
    texts = turns + ["unrelated", turns[5], turns[39][:500] + "EDIT" + turns[39][520:]]
    for i, t in enumerate(texts):
        s.record_request(req(s.next_id(), now, session_id="s1", prompt=t))
    s.close()
    assert [s.request(i + 1)["prompt"] for i in range(len(texts))] == texts
    c = sqlite3.connect(p)
    stored = c.execute("SELECT COUNT(*), SUM(LENGTH(body)), MAX(depth) FROM prompts").fetchone()
    assert stored[0] == len(texts) - 1 and stored[1] < 20_000 < sum(map(len, texts)) // 100 and stored[2] <= store.MAX_DEPTH
    c.execute("DELETE FROM requests WHERE id > 1")
    store.Prompts.prune(c)
    assert c.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 1  # turns[0] is a snapshot, nothing else needed
    c.commit()
    assert s.request(1)["prompt"] == texts[0]


def test_turns_pairs_verdict(tmp_path):
    s, now = Store(str(tmp_path / "s.db")), time.time()
    s.record_turn(dict(id="a", ts=now - 5, preview="a"))
    s.record_turn(dict(id="b", ts=now, preview="b"))
    s.record_turn(dict(id="b", ts=now, preview="dup"))
    for step in (2, 1):
        s.record_pair(dict(turn_id="a", request_id=1, step=step, served_track="anthropic", shadow_track="local",
                           served="{}", shadow="{}"))
    s.close()
    assert [x["step"] for x in s.turn("a")["steps"]] == [1, 2]
    assert s.turn("b")["preview"] == "b" and s.turn("zz") is None
    assert s.set_verdict("b", "local") and not s.set_verdict("b", "bogus") and not s.set_verdict("zz", "same")
    assert [t["id"] for t in s.turns()] == ["a", "b"]
    assert s.stats(now)["tally"] == {"local": 1, "anthropic": 0, "valar": 0, "same": 0}
    assert s.stats(now)["judged_today"] == 1
    s.forget_pairs()
    assert s.turns() == [] and s.turn("a") is None


def test_blocked_writer_never_blocks(tmp_path, monkeypatch):
    ev = threading.Event()
    monkeypatch.setattr(Store, "_run", lambda self: ev.wait(5))
    s = Store(str(tmp_path / "s.db"), queue_max=10)
    t = time.perf_counter()
    for i in range(100):
        s.record_request(req(i, 0))
    assert time.perf_counter() - t < 0.05
    assert abs(s.dropped - 90) <= 1
    ev.set()


def test_db_mode_0600(tmp_path):
    Store(str(tmp_path / "s.db")).close()
    assert os.stat(tmp_path / "s.db").st_mode & 0o777 == 0o600


def test_prune_30_days(tmp_path):
    p = str(tmp_path / "s.db")
    s, now = Store(p), time.time()
    s.record_request(req(1, now - 31 * 86400))
    s.record_request(req(2, now))
    s.close()
    assert [r["id"] for r in Store(p).departures()] == [2]


def test_bad_rows_dont_poison_batch(tmp_path):
    s, now = Store(str(tmp_path / "s.db")), time.time()
    s.record_request(req(1, now))
    s.record_request(req(2, now, model_in={"bad": 1}))
    s.record_request("not a dict")
    s.record_request(req(3, now))
    s.close()
    assert [r["id"] for r in s.departures()] == [3, 1]
    assert s.dropped == 2


def test_late_row_with_lower_id_found_by_overlapping_since(tmp_path):
    # ids are taken at request start, rows written at the end: a long request lands after a later one
    s, now = Store(str(tmp_path / "s.db")), time.time()
    a, b = s.next_id(), s.next_id()
    s.record_request(req(b, now))
    s.close()
    assert [r["id"] for r in s.departures(since_id=0)] == [b]
    s = Store(str(tmp_path / "s.db"))
    s.record_request(req(a, now - 30))
    s.close()
    assert [r["id"] for r in s.departures(since_id=max(0, b - 50))] == [b, a]


def test_null_store_is_inert():
    n = store.NullStore()
    assert n.next_id() + 1 == n.next_id()
    n.record_request(req(1, 0)); n.record_pair({}); n.record_turn({})
    assert n.dropped == 3
    assert n.departures() == [] and n.request(1) is None and n.turns() == [] and n.turn("x") is None
    assert n.pair_for_request(1) is None and n.set_verdict("x", "local") is False
    assert n.stats(time.time())["last_hour_total"] == 0
    n.forget_pairs(); n.close()


def test_stats_three_tracks(tmp_path):
    s, now = Store(str(tmp_path / "s.db")), time.time()
    for t in ("anthropic", "valar", "local"):
        s.record_request(req(s.next_id(), now - 5, t))
    s.close()
    st = s.stats(now)
    assert (st["anthropic_pct"], st["valar_pct"], st["local_pct"]) == (33, 33, 33)
    assert st["anthropic_pct"] + st["valar_pct"] + st["local_pct"] <= 100


def test_verdict_valar(tmp_path):
    s, now = Store(str(tmp_path / "s.db")), time.time()
    s.record_turn(dict(id="t", ts=now, preview="p"))
    s.close()
    assert s.set_verdict("t", "valar") and not s.set_verdict("t", "openai")
    assert s.stats(now)["tally"] == {"anthropic": 0, "valar": 1, "local": 0, "same": 0}


def test_old_db_gets_columns(tmp_path):
    import sqlite3
    p = str(tmp_path / "old.db")
    c = sqlite3.connect(p)
    c.executescript("""CREATE TABLE requests(id INTEGER PRIMARY KEY, ts REAL, track TEXT, reason TEXT,
      explanation TEXT, model_in TEXT, model_out TEXT, status INTEGER, ttfb_ms INTEGER, ms INTEGER,
      stream INTEGER, preview TEXT, turn_id TEXT);
    CREATE TABLE turns(id TEXT PRIMARY KEY, ts REAL, preview TEXT, verdict TEXT);
    CREATE TABLE pairs(id INTEGER PRIMARY KEY AUTOINCREMENT, turn_id TEXT, request_id INTEGER,
      step INTEGER, served_track TEXT, shadow_track TEXT, served TEXT, shadow TEXT);""")
    c.execute("INSERT INTO requests(id, ts, track) VALUES(1, ?, 'valar')", (time.time(),))
    c.execute("INSERT INTO turns(id, ts) VALUES('t', 1)")
    c.commit(); c.close()
    s = Store(p)
    s.record_request(req(2, time.time(), session_id="S", in_tok=4))
    s.close()
    old = s.request(1)
    assert old["track"] == "valar" and old["session_id"] is None and old["cost"] is None and old["window"] is None
    assert s.request(2)["session_id"] == "S" and s.request(2)["in_tok"] == 4
    c = sqlite3.connect(p)
    assert "session_id" in [r[1] for r in c.execute("PRAGMA table_info(turns)")]
    assert "shadow_cost" in [r[1] for r in c.execute("PRAGMA table_info(pairs)")]
    c.close()


def seeded(tmp_path):
    s, now = Store(str(tmp_path / "s.db")), time.time()
    s.touch_session("sa", now - 5000, "/x/alpha", "main")
    s.touch_session("sb", now - 100)
    rows = [  # id, ts, track, reason, session, cost, api_cost, in, out, status
        (1, now - 5000, "anthropic", "rule:big:2", "sa", 0.0, 1.0, 10, 5, 200),
        (2, now - 60, "valar", "rule:a:b", "sa", 0.25, 1.0, 100, 50, 200),
        (3, now - 50, "local", "about:coding", "sb", 0.0, 0.5, 7, 3, 502),
        (4, now - 40, "valar", "default", "sb", None, None, None, None, 200),
        (5, now - 30, "anthropic", "passthrough", None, None, None, None, None, 0),
        (6, now - 20, "anthropic", "lever", "sa", 0.0, 2.0, 1, 1, 200),
        (7, now - 15, "valar", "passthrough", "sb", None, None, 4, 2, 200),  # has tokens, no price
    ]
    for i, ts, t, rs, sid, c, ac, ti, to, st in rows:
        s.record_request(req(i, ts, t, st, reason=rs, session_id=sid, cost=c, api_cost=ac, in_tok=ti, out_tok=to))
    s.record_pair({"turn_id": "t1", "request_id": 2, "step": 0, "shadow_track": "anthropic", "shadow_cost": 0.75})
    s.record_pair({"turn_id": "t1", "request_id": 1, "step": 0, "shadow_track": "local", "shadow_cost": 0.5})
    s.record_turn({"id": "t1", "ts": now - 60, "preview": "p", "verdict": "valar", "session_id": "sa"})
    s.record_turn({"id": "t2", "ts": now - 50, "preview": "q", "verdict": None, "session_id": "sb"})
    s.close()
    return s, now


def test_summary(tmp_path):
    s, now = seeded(tmp_path)
    m = s.summary(None, 0, now + 1, now)
    assert m["total"] == 7 and m["errors"] == 2 and m["per_minute"] == 6 / 5
    a, v, l = (m["tracks"][t] for t in ("anthropic", "valar", "local"))
    assert (a["n"], a["pct"], v["n"], v["pct"], l["pct"]) == (3, 43, 3, 43, 14)
    assert (v["cost"], v["api_cost"], v["unpriced"], v["in"], v["out"]) == (0.25, 1.0, 1, 104, 52)  # unpriced: row 7 only; tokenless row 4 is not counted
    assert (a["comparisons"], l["comparisons"], v["comparisons"]) == (0.75, 0.5, 0)
    assert m["savings"] == 0.75 + 0.5  # valar 1.0-.25 + local .5; anthropic and comparisons excluded
    assert m["rules"] == {"big": 1, "a:b": 1, "classifier": 1, "otherwise": 1, "lever": 1}
    assert m["tally"] == {"anthropic": 0, "valar": 1, "local": 0, "same": 0}
    sa = s.summary(["sa"], 0, now + 1, now)
    assert sa["total"] == 3 and sa["savings"] == 0.75 and sa["tracks"]["local"]["n"] == 0 and sa["tally"]["valar"] == 1
    assert s.summary(["unknown"], 0, now + 1, now)["total"] == 1
    assert s.summary(["sb", "unknown"], 0, now + 1, now)["total"] == 4
    span = s.summary(None, now - 3600, now + 1, now)  # drops request 1 and its comparison
    assert span["total"] == 6 and span["tracks"]["local"]["comparisons"] == 0 and "big" not in span["rules"]
    assert s.summary([], 0, now + 1, now)["total"] == 0
    assert s.first_ts(["sb"]) == now - 50
    s2 = Store(str(tmp_path / "s.db"))  # an unpriced valar row with tokens and an api_cost is not a saving
    s2.record_request(req(8, now - 10, "valar", 200, reason="default", session_id="sb", cost=None, api_cost=9.0, in_tok=5, out_tok=5))
    m2 = s2.summary(None, 0, now + 1, now)
    assert m2["savings"] == m["savings"] and m2["saved_would"] == m["saved_would"] == 1.5
    s2.close()


def test_list_sessions_and_filtered_reads(tmp_path):
    s, now = seeded(tmp_path)
    got = s.list_sessions(0, now + 1)
    assert [(x["id"], x["label"], x["n"]) for x in got] == [("sb", "sb", 3), ("sa", "alpha · main", 3), ("unknown", "unknown session", 1)]
    assert [x["id"] for x in s.list_sessions(now - 3600, now + 1)][0] == "sb"
    assert [r["id"] for r in s.departures(sessions=["sb"])] == [7, 4, 3]
    assert [r["id"] for r in s.departures(sessions=["unknown"])] == [5]
    assert [t["id"] for t in s.turns(sessions=["sb"])] == ["t2"]
    assert len(s.turns()) == 2
    n = store.NullStore()
    assert n.list_sessions(0, 1) == [] and n.summary(None, 0, 1, 1)["total"] == 0
