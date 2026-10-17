"""Console config file and SQLite stats store. Independent of claude_router.py."""
import itertools
import json
import os
import queue
import sqlite3
import sys
import tempfile
import threading
import time

from routing import DEFAULT_CONFIG, config_from_json, config_to_json

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY, ts REAL, track TEXT, reason TEXT,
  explanation TEXT, model_in TEXT, model_out TEXT, status INTEGER, ttfb_ms INTEGER, ms INTEGER,
  stream INTEGER, preview TEXT, turn_id TEXT);
CREATE INDEX IF NOT EXISTS requests_ts ON requests(ts);
CREATE TABLE IF NOT EXISTS turns(id TEXT PRIMARY KEY, ts REAL, preview TEXT, verdict TEXT);
CREATE TABLE IF NOT EXISTS pairs(id INTEGER PRIMARY KEY AUTOINCREMENT, turn_id TEXT, request_id INTEGER,
  step INTEGER, served_track TEXT, shadow_track TEXT, served TEXT, shadow TEXT);
"""
COLS = {
    "requests": "id ts track reason explanation model_in model_out status ttfb_ms ms stream preview turn_id",
    "turns": "id ts preview verdict",
    "pairs": "turn_id request_id step served_track shadow_track served shadow",
}
INSERT = {t: "INSERT %sINTO %s(%s) VALUES(%s)" % ("OR IGNORE " if t == "turns" else "", t, ",".join(c.split()),
                                                  ",".join(":" + k for k in c.split())) for t, c in COLS.items()}
RETAIN = 30 * 86400


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
    fd, tmp = tempfile.mkstemp(dir=d)  # mkstemp is already 0600
    try:
        with os.fdopen(fd, "w") as f:
            os.chmod(tmp, 0o600)
            json.dump(config_to_json(cfg), f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class Store:
    def __init__(self, path, queue_max=10_000):
        self.path, self.dropped, self._dlock = path, 0, threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
        os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            c.execute("DELETE FROM requests WHERE ts < ?", (time.time() - RETAIN,))
            top = c.execute("SELECT MAX(id) FROM requests").fetchone()[0] or 0
        self._ids = itertools.count(top + 1)  # next() on itertools.count is atomic under the GIL
        self._q = queue.Queue(queue_max)
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

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
        with c:
            for t, row in items:
                c.execute(INSERT[t], {k: row.get(k) for k in COLS[t].split()})
            if time.time() - last_prune > 3600:
                c.execute("DELETE FROM requests WHERE ts < ?", (time.time() - RETAIN,))

    def _drop(self):
        with self._dlock:
            self.dropped += 1

    def _all(self, sql, args=()):
        c = self._conn()
        try:
            return [dict(r) for r in c.execute(sql, args)]
        finally:
            c.close()

    def departures(self, since_id=None, before_id=None, limit=200):
        w, a = [], []
        if since_id is not None:
            w.append("id > ?"); a.append(since_id)
        if before_id is not None:
            w.append("id < ?"); a.append(before_id)
        return self._all("SELECT * FROM requests%s ORDER BY id DESC LIMIT ?" % (" WHERE " + " AND ".join(w) if w else ""),
                         a + [limit])

    def request(self, id):
        r = self._all("SELECT * FROM requests WHERE id=?", (id,))
        return r[0] if r else None

    def pair_for_request(self, request_id):
        r = self._all("SELECT * FROM pairs WHERE request_id=? ORDER BY id DESC LIMIT 1", (request_id,))
        return r[0] if r else None

    def stats(self, now):
        hour = self._all("SELECT track, status FROM requests WHERE ts >= ?", (now - 3600,))
        n = len(hour)
        pct = lambda t: round(100 * sum(r["track"] == t for r in hour) / n) if n else 0
        lt = time.localtime(now)
        midnight = time.mktime(lt[:3] + (0, 0, 0, 0, 0, -1))
        c = self._conn()
        try:
            q = lambda sql, *a: c.execute(sql, a).fetchone()[0]
            tally = {"local": 0, "anthropic": 0, "same": 0}
            for v, k in c.execute("SELECT verdict, COUNT(*) FROM turns WHERE verdict IS NOT NULL GROUP BY verdict"):
                tally[v] = k
            return {
                "anthropic_pct": pct("anthropic"), "local_pct": pct("local"), "last_hour_total": n,
                "per_minute": q("SELECT COUNT(*) FROM requests WHERE ts >= ?", now - 300) / 5,
                "errors_last_hour": sum(r["status"] is not None and (r["status"] >= 500 or r["status"] == 0) for r in hour),
                "doubled_turns_today": q("SELECT COUNT(*) FROM turns WHERE ts >= ?", midnight),
                "judged_today": q("SELECT COUNT(*) FROM turns WHERE ts >= ? AND verdict IS NOT NULL", midnight),
                "tally": tally,
            }
        finally:
            c.close()

    def turns(self, unjudged_first=True, limit=100):
        order = "(verdict IS NOT NULL), " if unjudged_first else ""
        return self._all("SELECT * FROM turns ORDER BY %sts DESC LIMIT ?" % order, (limit,))

    def turn(self, turn_id):
        r = self._all("SELECT * FROM turns WHERE id=?", (turn_id,))
        if not r:
            return None
        r[0]["steps"] = self._all("SELECT * FROM pairs WHERE turn_id=? ORDER BY step", (turn_id,))
        return r[0]

    def set_verdict(self, turn_id, verdict):
        if verdict not in ("local", "anthropic", "same"):
            return False
        c = self._conn()
        try:
            with c:
                return c.execute("UPDATE turns SET verdict=? WHERE id=?", (verdict, turn_id)).rowcount > 0
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
