"""Console config file and SQLite stats store. Independent of claude_router.py."""
import itertools
import json
import os
import queue
import re
import sqlite3
import statistics
import sys
import threading
import zlib
from collections import deque
from hashlib import sha256
from keys import write_json
import time

import sessions as sess
from routing import DEFAULT_CONFIG, config_from_json, config_to_json


SCHEMA = """
CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY, ts REAL, track TEXT, reason TEXT,
  explanation TEXT, model_in TEXT, model_out TEXT, status INTEGER, ttfb_ms INTEGER, ms INTEGER,
  stream INTEGER, preview TEXT, turn_id TEXT, session_id TEXT, in_tok INTEGER, out_tok INTEGER,
  cache_read_tok INTEGER, cache_write_tok INTEGER, cache_write_1h_tok INTEGER, window TEXT, step TEXT, cost REAL, api_cost REAL, prompt_hash TEXT);
CREATE INDEX IF NOT EXISTS requests_ts ON requests(ts);
CREATE TABLE IF NOT EXISTS turns(id TEXT PRIMARY KEY, ts REAL, preview TEXT, verdict TEXT, session_id TEXT);
CREATE TABLE IF NOT EXISTS pairs(id INTEGER PRIMARY KEY AUTOINCREMENT, turn_id TEXT, request_id INTEGER,
  step INTEGER, served_track TEXT, shadow_track TEXT, served TEXT, shadow TEXT, shadow_cost REAL);
CREATE TABLE IF NOT EXISTS prompts(hash TEXT PRIMARY KEY, base TEXT, head INTEGER, tail INTEGER, body BLOB, depth INTEGER);
CREATE INDEX IF NOT EXISTS prompts_base ON prompts(base);
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, cwd TEXT, branch TEXT, first_ts REAL, last_ts REAL, name TEXT);
"""
COLS = {
    "requests": "id ts track reason explanation model_in model_out status ttfb_ms ms stream preview turn_id session_id in_tok out_tok cache_read_tok cache_write_tok cache_write_1h_tok window step cost api_cost prompt_hash",
    "turns": "id ts preview verdict session_id",
    "pairs": "turn_id request_id step served_track shadow_track served shadow shadow_cost",
}
INSERT = {t: "INSERT %sINTO %s(%s) VALUES(%s)" % ("OR IGNORE " if t == "turns" else "", t, ",".join(c.split()),
                                                  ",".join(":" + k for k in c.split())) for t, c in COLS.items()}
UPSERT_SESSION = """INSERT INTO sessions(id,cwd,branch,first_ts,last_ts) VALUES(:id,:cwd,:branch,:ts,:ts)
  ON CONFLICT(id) DO UPDATE SET first_ts=MIN(first_ts,excluded.first_ts), last_ts=MAX(last_ts,excluded.last_ts),
  cwd=COALESCE(excluded.cwd,cwd), branch=COALESCE(excluded.branch,branch)"""
RETAIN = 30 * 86400
MAX_DEPTH = 32  # a full copy at least every 32 links, so reading one never walks far


def _prefix(a, b):
    """Length of the common prefix; binary search so the compares run in C."""
    lo, hi = 0, min(len(a), len(b))
    while lo < hi:
        m = (lo + hi + 1) // 2
        lo, hi = (m, hi) if a[:m] == b[:m] else (lo, m - 1)
    return lo


def _suffix(a, b, limit):
    lo, hi = 0, limit
    while lo < hi:
        m = (lo + hi + 1) // 2
        lo, hi = (m, hi) if a[len(a) - m:] == b[len(b) - m:] else (lo, m - 1)
    return lo


class Prompts:
    """Prompts stored once by hash, each as an edit of a recent prompt from the same session: keep the base's first
    `head` and last `tail` chars, put `body` between. Conversations grow by appending, so most rows hold only the new
    turns. Bodies are zlib-compressed. Writer thread only."""

    def __init__(self):
        self.recent = {}  # session -> deque of (hash, text, depth)

    def put(self, c, session, text):
        h = sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
        if session not in self.recent and len(self.recent) >= 64:
            del self.recent[next(iter(self.recent))]  # oldest session out
        recent = self.recent.setdefault(session, deque(maxlen=4))
        row = c.execute("SELECT depth FROM prompts WHERE hash=?", (h,)).fetchone()
        if row:
            depth = row[0]
        else:
            best = (0, 0, None, 0)
            for bh, bt, bd in recent:
                p = _prefix(bt, text)
                t = _suffix(bt, text, min(len(bt), len(text)) - p)
                if p + t > best[0] + best[1] and bd < MAX_DEPTH:
                    best = (p, t, (bh, bt), bd + 1)
            head, tail, base, depth = best
            if base and not c.execute("SELECT 1 FROM prompts WHERE hash=?", (base[0],)).fetchone():
                best = (0, 0, None, 0)  # pruned since it was remembered
                head, tail, base, depth = best
            if head + tail < len(text) // 2:  # not worth a link
                head, tail, base, depth = 0, 0, None, 0
            mid = text[head:len(text) - tail]
            c.execute("INSERT INTO prompts VALUES(?,?,?,?,?,?)",
                      (h, base and base[0], head, tail, zlib.compress(mid.encode("utf-8", "surrogatepass")), depth))
        recent.append((h, text, depth))
        return h

    @staticmethod
    def get(c, h):
        if not h:
            return None
        chain = []
        while h and len(chain) <= MAX_DEPTH:
            r = c.execute("SELECT base, head, tail, body FROM prompts WHERE hash=?", (h,)).fetchone()
            if not r:
                return None
            chain.append(r)
            h = r[0]
        text = ""
        for _, head, tail, body in reversed(chain):
            mid = zlib.decompress(body).decode("utf-8", "surrogatepass")
            text = text[:head] + mid + (text[len(text) - tail:] if tail else "")
        return text

    @staticmethod
    def prune(c):
        """Drop prompts no request and no other prompt points at; repeat to peel unused chains."""
        while c.execute("DELETE FROM prompts WHERE hash NOT IN (SELECT prompt_hash FROM requests WHERE prompt_hash IS NOT NULL) "
                        "AND hash NOT IN (SELECT base FROM prompts WHERE base IS NOT NULL)").rowcount:
            pass


def load_console_config(path):
    try:
        with open(path) as f:
            raw = f.read()
    except FileNotFoundError:
        return DEFAULT_CONFIG, None
    except OSError as e:
        return _bad(path, e)
    try:
        return config_from_json(json.loads(raw)), None
    except ValueError as e:  # JSONDecodeError is a ValueError
        return _bad(path, e)


def _bad(path, err):
    print("console config unreadable: %s" % err, file=sys.stderr)
    try:
        os.replace(path, path + ".bad")
    except OSError:
        pass
    return DEFAULT_CONFIG, ("Your saved settings couldn't be read (%s). They were moved to %s.bad and defaults are in use."
                            % (err, os.path.basename(path)))


def save_console_config(path, cfg):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, mode=0o700, exist_ok=True)
    write_json(path, config_to_json(cfg), indent=2)


def latency(first: list, total: list) -> dict:
    """Median (and mean) first-byte and total ms of a track's answered requests; n is the sample size."""
    return {"n": len(total), "ttfb": statistics.median(first), "ms": statistics.median(total),
            "ttfb_mean": statistics.fmean(first), "ms_mean": statistics.fmean(total)}


class Store:
    def __init__(self, path, queue_max=10_000):
        self.path, self.dropped, self._dlock = path, 0, threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
        os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            self._migrate(c)
            c.execute("CREATE INDEX IF NOT EXISTS requests_sess_ts ON requests(session_id, ts)")  # after migrate: old dbs lack the column
            c.execute("CREATE INDEX IF NOT EXISTS requests_prompt ON requests(prompt_hash)")
            c.execute("DELETE FROM requests WHERE ts < ?", (time.time() - RETAIN,))
            c.execute("DELETE FROM sessions WHERE last_ts < ?", (time.time() - RETAIN,))
            Prompts.prune(c)
            top = c.execute("SELECT MAX(id) FROM requests").fetchone()[0] or 0
        self._prompts = Prompts()
        self._ids = itertools.count(top + 1)  # next() on itertools.count is atomic under the GIL
        self._q = queue.Queue(queue_max)
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    @staticmethod
    def _migrate(c):
        """Old databases lack the newer columns; add them (CREATE TABLE IF NOT EXISTS doesn't)."""
        for t, cols in COLS.items():
            have = {r[1] for r in c.execute("PRAGMA table_info(%s)" % t)}
            types = {"in_tok": "INTEGER", "out_tok": "INTEGER", "cache_read_tok": "INTEGER", "cache_write_tok": "INTEGER", "cache_write_1h_tok": "INTEGER",
                     "cost": "REAL", "api_cost": "REAL", "shadow_cost": "REAL", "id": "INTEGER", "ts": "REAL"}
            for col in cols.split():
                if col not in have:
                    c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (t, col, types.get(col, "TEXT")))
        if "name" not in {r[1] for r in c.execute("PRAGMA table_info(sessions)")}:
            c.execute("ALTER TABLE sessions ADD COLUMN name TEXT")

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=5)
        c.row_factory = sqlite3.Row
        return c

    def next_id(self):
        return next(self._ids)

    def _put(self, item):
        try:
            self._q.put_nowait(item)
        except queue.Full:
            self._drop()

    def record_request(self, row): self._put(("requests", row))
    def record_pair(self, row): self._put(("pairs", row))
    def record_turn(self, row): self._put(("turns", row))

    def forget_session(self, sid, timeout=10):
        """Delete a session's requests, turns, pairs and prompts (an X on its chip). Runs on the writer thread so it
        can't race a prompt being chained onto one it deletes. "unknown" is the requests with no session id."""
        done = threading.Event()
        self._q.put(("forget", (None if sid == "unknown" else sid, done)))
        return done.wait(timeout)

    def touch_session(self, id, ts, cwd=None, branch=None):
        self._put(("sessions", {"id": id, "ts": ts, "cwd": cwd, "branch": branch}))

    def close(self):
        self._q.put(None)  # blocking: flush must not be dropped
        self._t.join()

    def _run(self):
        c, last_prune, done = self._conn(), time.time(), False
        while not done:
            try:
                items = [self._q.get(timeout=60)]
            except queue.Empty:
                items = []
            while items and len(items) < 200:
                try:
                    items.append(self._q.get_nowait())
                except queue.Empty:
                    break
            done = None in items
            items = [it for it in items if it]
            try:
                self._write(c, items, last_prune)
            except Exception:  # ponytail: retry one by one so a single bad row costs only itself
                for it in items:
                    try:
                        self._write(c, [it], last_prune)
                    except Exception as e:
                        print("store write failed: %s" % type(e).__name__, file=sys.stderr)
                        self._drop()
            if time.time() - last_prune > 3600:
                last_prune = time.time()
        c.close()

    def _write(self, c, items, last_prune):
        done = []
        with c:
            for t, row in items:
                if t == "forget":
                    sid, ev = row
                    c.execute("DELETE FROM pairs WHERE request_id IN (SELECT id FROM requests WHERE session_id IS ?) "
                              "OR turn_id IN (SELECT id FROM turns WHERE session_id IS ?)", (sid, sid))
                    for tbl, col in (("requests", "session_id"), ("turns", "session_id"), ("sessions", "id")):
                        c.execute("DELETE FROM %s WHERE %s IS ?" % (tbl, col), (sid,))
                    Prompts.prune(c)
                    self._prompts.recent.pop(sid, None)
                    done.append(ev)
                elif t == "sessions":
                    c.execute(UPSERT_SESSION, row)
                else:
                    if t == "requests" and row.get("prompt"):
                        row = {**row, "prompt_hash": self._prompts.put(c, row.get("session_id"), row["prompt"])}
                    c.execute(INSERT[t], {k: row.get(k) for k in COLS[t].split()})
            if time.time() - last_prune > 3600:
                c.execute("DELETE FROM sessions WHERE last_ts < ?", (time.time() - RETAIN,))
                c.execute("DELETE FROM requests WHERE ts < ?", (time.time() - RETAIN,))
                Prompts.prune(c)
        for ev in done:
            ev.set()

    def _drop(self):
        with self._dlock:
            self.dropped += 1

    def _all(self, sql, args=()):
        c = self._conn()
        try:
            return [dict(r) for r in c.execute(sql, args)]
        finally:
            c.close()

    @staticmethod
    def _sess(sessions, col="session_id"):
        """SQL clause + args for a session filter; None = all, "unknown" = NULL session."""
        if sessions is None:
            return "1", []
        ids = [x for x in sessions if x != "unknown"]
        parts = (["%s IN (%s)" % (col, ",".join("?" * len(ids)))] if ids else []) + (["%s IS NULL" % col] if "unknown" in sessions else [])
        return "(%s)" % " OR ".join(parts) if parts else "0", ids

    def departures(self, since_id=None, before_id=None, limit=200, sessions=None, t0=None, t1=None):
        sc, sa = self._sess(sessions)
        w, a = [sc], sa
        if t0 is not None:
            w.append("ts >= ?"); a.append(t0)
        if t1 is not None:
            w.append("ts <= ?"); a.append(t1)
        if since_id is not None:
            w.append("id > ?"); a.append(since_id)
        if before_id is not None:
            w.append("id < ?"); a.append(before_id)
        return self._all("SELECT * FROM requests%s ORDER BY id DESC LIMIT ?" % (" WHERE " + " AND ".join(w)),
                         a + [limit])

    def request(self, id):
        r = self._all("SELECT * FROM requests WHERE id=?", (id,))
        if not r:
            return None
        c = self._conn()
        try:
            r[0]["prompt"] = Prompts.get(c, r[0]["prompt_hash"])
        finally:
            c.close()
        return r[0]

    def pair_for_request(self, request_id):
        r = self._all("SELECT * FROM pairs WHERE request_id=? ORDER BY id DESC LIMIT 1", (request_id,))
        return r[0] if r else None

    def stats(self, now, tracks):
        hour = self._all("SELECT track, status FROM requests WHERE ts >= ?", (now - 3600,))
        n = len(hour)
        pct = lambda t: round(100 * sum(r["track"] == t for r in hour) / n) if n else 0
        lt = time.localtime(now)
        midnight = time.mktime(lt[:3] + (0, 0, 0, 0, 0, -1))
        c = self._conn()
        try:
            q = lambda sql, *a: c.execute(sql, a).fetchone()[0]
            tally = dict.fromkeys([*tracks, "same"], 0)  # plus any other track judged before it was removed
            for v, k in c.execute("SELECT verdict, COUNT(*) FROM turns WHERE verdict IS NOT NULL GROUP BY verdict"):
                tally[v] = k
            return {
                **{"%s_pct" % t: pct(t) for t in dict.fromkeys([*tracks, *(r["track"] for r in hour)])},
                "last_hour_total": n,
                "per_minute": q("SELECT COUNT(*) FROM requests WHERE ts >= ?", now - 300) / 5,
                "errors_last_hour": sum(r["status"] is not None and (r["status"] >= 500 or r["status"] == 0) for r in hour),
                "doubled_turns_today": q("SELECT COUNT(*) FROM turns WHERE ts >= ?", midnight),
                "judged_today": q("SELECT COUNT(*) FROM turns WHERE ts >= ? AND verdict IS NOT NULL", midnight),
                "tally": tally,
            }
        finally:
            c.close()

    def turns(self, unjudged_first=True, limit=100, sessions=None):
        order = "(verdict IS NOT NULL), " if unjudged_first else ""
        sc, sa = self._sess(sessions)
        return self._all("SELECT * FROM turns WHERE %s ORDER BY %sts DESC LIMIT ?" % (sc, order), sa + [limit])

    def first_ts(self, sessions):
        """Whole-session start: oldest request of the picked sessions (all: oldest kept)."""
        sc, sa = self._sess(sessions)
        return self._all("SELECT COALESCE(MIN(ts), 0) t FROM requests WHERE %s" % sc, sa)[0]["t"]

    def summary(self, sessions, t0, t1, now, tracks):
        """tracks: the configured ones; rows of any other track still show, marked removed."""
        sc, sa = self._sess(sessions)
        span = [t0, t1]
        by = {r["track"]: r for r in self._all(
            "SELECT track, COUNT(*) n, COALESCE(SUM(in_tok),0) i, COALESCE(SUM(out_tok),0) o, COALESCE(SUM(cost),0) cost, "
            "COALESCE(SUM(cache_read_tok),0) cr, COALESCE(SUM(cache_write_tok),0) cw, "
            "COALESCE(SUM(api_cost),0) api, "
            "COALESCE(SUM(CASE WHEN cost IS NOT NULL THEN api_cost - cost END),0) sav, "
            "COALESCE(SUM(CASE WHEN cost IS NOT NULL THEN api_cost END),0) papi, SUM(cost IS NULL AND COALESCE(in_tok, out_tok, cache_read_tok, cache_write_tok) IS NOT NULL) nocost FROM requests WHERE %s AND ts BETWEEN ? AND ? GROUP BY track" % sc,
            sa + span)}
        comp = {r["shadow_track"]: r["c"] for r in self._all(
            "SELECT p.shadow_track, COALESCE(SUM(p.shadow_cost),0) c FROM pairs p JOIN requests r ON r.id=p.request_id "
            "WHERE %s AND r.ts BETWEEN ? AND ? GROUP BY p.shadow_track" % self._sess(sessions, "r.session_id")[0], sa + span)}
        # ponytail: medians in Python over every answered row of the span; a SQL percentile if spans get huge
        lat = {}
        for r in self._all("SELECT track, ttfb_ms f, ms t FROM requests WHERE %s AND ts BETWEEN ? AND ? "
                           "AND status BETWEEN 200 AND 299 AND ms IS NOT NULL" % sc, sa + span):
            x = lat.setdefault(r["track"], ([], []))
            x[0].append(r["f"] if r["f"] is not None else r["t"])
            x[1].append(r["t"])
        total = sum(r["n"] for r in by.values())
        z = {"n": 0, "i": 0, "o": 0, "cr": 0, "cw": 0, "cost": 0, "api": 0, "sav": 0, "papi": 0, "nocost": 0}
        out_tracks = {}
        for t in dict.fromkeys([*tracks, *by]):
            r = by.get(t, z)
            out_tracks[t] = {"n": r["n"], "pct": round(100 * r["n"] / total) if total else 0, "in": r["i"], "out": r["o"],
                             "cost": r["cost"], "api_cost": r["api"], "unpriced": r["nocost"] if t != "anthropic" else 0,
                             "comparisons": comp.get(t, 0), "cache_read": r["cr"], "cache_write": r["cw"],
                             "latency": latency(*lat[t]) if t in lat else None}
            if t not in tracks:
                out_tracks[t]["removed"] = True
        rules = {}
        for r in self._all("SELECT reason, COUNT(*) n FROM requests "
                           "WHERE %s AND ts BETWEEN ? AND ? GROUP BY reason" % sc, sa + span):
            k = r["reason"] or ""
            k = re.sub(r":\d+$", "", k[5:]) if k.startswith("rule:") else "classifier" if k.startswith("about:") else \
                {"default": "otherwise", "lever": "lever"}.get(k)
            if k:
                rules[k] = rules.get(k, 0) + r["n"]
        errors = self._all("SELECT COUNT(*) n FROM requests WHERE %s AND ts BETWEEN ? AND ? AND (status >= 500 OR status = 0)" % sc, sa + span)[0]["n"]
        recent = self._all("SELECT COUNT(*) n FROM requests WHERE %s AND ts BETWEEN ? AND ?" % sc, sa + [max(t0, now - 300), t1])[0]["n"]
        tally = dict.fromkeys([*out_tracks, "same"], 0)
        for r in self._all("SELECT verdict, COUNT(*) n FROM turns WHERE %s AND ts BETWEEN ? AND ? AND verdict IS NOT NULL GROUP BY verdict"
                           % self._sess(sessions)[0], sa + span):
            tally[r["verdict"]] = r["n"]
        # only rows with a price: an unpriced row is unknown, not free
        others = [t for t in by if t != "anthropic"]
        savings = sum(by[t]["sav"] for t in others)
        saved_would = sum(by[t]["papi"] for t in others)
        return {"total": total, "per_minute": recent / 5, "tracks": out_tracks, "savings": savings, "saved_would": saved_would, "rules": rules,
                "errors": errors, "tally": tally}

    def list_sessions(self, t0, t1):
        rows = self._all(
            "SELECT r.session_id id, s.cwd, s.branch, s.name, s.first_ts, s.last_ts, COUNT(*) n, MAX(r.ts) last FROM requests r "
            "LEFT JOIN sessions s ON s.id=r.session_id WHERE r.ts BETWEEN ? AND ? GROUP BY r.session_id", (t0, t1))
        # a session just named with /router shows right away, before its next request is saved
        rows += self._all("SELECT id, cwd, branch, name, first_ts, last_ts, 0 n, last_ts last FROM sessions WHERE name IS NOT NULL "
                          "AND last_ts BETWEEN ? AND ? AND id NOT IN (SELECT DISTINCT session_id FROM requests WHERE ts BETWEEN ? AND ? "
                          "AND session_id IS NOT NULL)", (t0, t1, t0, t1))
        rows.sort(key=lambda r: r["last"] or 0, reverse=True)
        out = []
        for r in rows:
            r.pop("last")
            r["label"] = r["name"] or sess.label(r["id"], r["cwd"], r["branch"])
            r["id"] = r["id"] or "unknown"
            out.append(r)
        return out

    def turn(self, turn_id):
        r = self._all("SELECT * FROM turns WHERE id=?", (turn_id,))
        if not r:
            return None
        r[0]["steps"] = self._all("SELECT * FROM pairs WHERE turn_id=? ORDER BY step", (turn_id,))
        return r[0]

    def set_verdict(self, turn_id, verdict, tracks):
        if verdict not in (*tracks, "same"):
            return False
        c = self._conn()
        try:
            with c:
                return c.execute("UPDATE turns SET verdict=? WHERE id=?", (verdict, turn_id)).rowcount > 0
        finally:
            c.close()

    def set_session_name(self, sid, name):
        """Name a session (from /router name); None clears it. Synchronous so the console shows it on the next poll."""
        now = time.time()
        c = self._conn()
        try:
            with c:
                c.execute("INSERT INTO sessions(id,first_ts,last_ts,name) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, last_ts=MAX(last_ts,excluded.last_ts)",
                          (sid, now, now, name))
        finally:
            c.close()

    def forget_pairs(self):
        c = self._conn()
        try:
            with c:
                c.execute("DELETE FROM pairs")
                c.execute("DELETE FROM turns")
        finally:
            c.close()

    def reprice(self, prices, downstreams):
        """Fill `cost` on rows recorded before their downstream had a price; never touches a cost already set. Returns rows updated."""
        by = {d.id: d for d in downstreams if d.kind == "valar" and prices.listed.get(d.id) or d.prices}
        if not by:
            return 0
        rows = self._all("SELECT id, track, model_out, window, in_tok, out_tok, cache_read_tok, cache_write_tok FROM requests "
                         "WHERE track IN (%s) AND cost IS NULL AND COALESCE(in_tok, out_tok, cache_read_tok, cache_write_tok) IS NOT NULL"
                         % ",".join("?" * len(by)), list(by))
        upd = []
        for r in rows:
            u = {"in": r["in_tok"], "out": r["out_tok"], "cache_read": r["cache_read_tok"], "cache_write": r["cache_write_tok"]}
            c = prices.cost(by[r["track"]], r["model_out"], r["window"], u)
            if c is not None:
                upd.append((c, r["id"]))
        c = self._conn()
        try:
            with c:
                c.executemany("UPDATE requests SET cost=? WHERE id=? AND cost IS NULL", upd)
        finally:
            c.close()
        return len(upd)


class NullStore:
    """Stats off (stats.db unusable): same interface, writes dropped, reads empty."""
    def __init__(self):
        self.dropped, self._ids, self._dlock = 0, itertools.count(1), threading.Lock()
    def next_id(self): return next(self._ids)
    def _drop(self):
        with self._dlock:
            self.dropped += 1
    def record_request(self, row): self._drop()
    def record_pair(self, row): self._drop()
    def record_turn(self, row): self._drop()
    def touch_session(self, *a, **k): pass
    def close(self): pass
    def departures(self, *a, **k): return []
    def request(self, id): return None
    def pair_for_request(self, request_id): return None
    def turns(self, *a, **k): return []
    def list_sessions(self, *a, **k): return []
    def first_ts(self, *a): return 0
    def summary(self, sessions, t0, t1, now, tracks):
        t = {"n": 0, "pct": 0, "in": 0, "out": 0, "cost": 0, "api_cost": 0, "unpriced": 0, "comparisons": 0, "cache_read": 0, "cache_write": 0,
             "latency": None}
        return {"total": 0, "per_minute": 0, "tracks": {k: dict(t) for k in tracks}, "savings": 0, "saved_would": 0, "rules": {}, "errors": 0,
                "tally": dict.fromkeys([*tracks, "same"], 0)}
    def turn(self, turn_id): return None
    def set_verdict(self, turn_id, verdict, tracks): return False
    def forget_pairs(self): pass
    def set_session_name(self, sid, name): pass
    def forget_session(self, sid): return True
    def reprice(self, prices, downstreams): return 0

    def stats(self, now, tracks):
        return {**{"%s_pct" % t: 0 for t in tracks}, "last_hour_total": 0, "per_minute": 0, "errors_last_hour": 0,
                "doubled_turns_today": 0, "judged_today": 0, "tally": dict.fromkeys([*tracks, "same"], 0)}
