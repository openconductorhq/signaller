import os
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
        s.record_request(req(s.next_id(), now - 10 * i, t, st))
    s.record_request(req(s.next_id(), now - 7200))
    s.close()
    st = s.stats(now)
    assert (st["anthropic_pct"], st["local_pct"], st["last_hour_total"], st["errors_last_hour"]) == (75, 25, 4, 1)
    assert [r["id"] for r in s.departures()] == [5, 4, 3, 2, 1]
    assert [r["id"] for r in s.departures(since_id=3)] == [5, 4]
    assert [r["id"] for r in s.departures(before_id=3)] == [2, 1]
    assert s.request(4)["track"] == "local" and s.request(99) is None
    assert Store(str(tmp_path / "s.db")).next_id() == 6


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
