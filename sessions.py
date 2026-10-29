"""Label sessions (cwd, git branch) from Claude Code transcripts. Never raises into the proxy path."""
import glob
import json
import os
import queue
import threading
import time


def claude_dir(env):
    return os.path.expanduser(env.get("CLAUDE_CONFIG_DIR") or "~/.claude")


def find_transcript(claude_dir, sid):
    """(cwd, gitBranch) from the first lines that carry them; (None, None) if absent or unreadable."""
    try:
        for p in glob.glob(os.path.join(glob.escape(claude_dir), "projects", "*", glob.escape(sid) + ".jsonl")):
            cwd = branch = None
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    for _, line in zip(range(50), f):
                        try:
                            o = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(o, dict):
                            cwd = cwd or o.get("cwd") or None
                            branch = branch or o.get("gitBranch") or None
                        if cwd and branch:
                            break
            except OSError:
                continue
            if cwd or branch:
                return cwd, branch
    except Exception:
        pass
    return None, None


def label(sid, cwd, branch):
    if not sid:
        return "unknown session"
    if not cwd:
        return sid[:8]
    base = os.path.basename(cwd.rstrip("/")) or cwd
    return "%s · %s" % (base, branch) if branch else base


class SessionDirectory:
    RETRY = 30

    def __init__(self, store, claude_dir, clock=time.time):
        self.store, self.dir, self.clock = store, claude_dir, clock
        self._known, self._tried = set(), {}
        self._q = queue.Queue()
        threading.Thread(target=self._work, daemon=True).start()

    def note(self, sid, ts):
        try:
            self.store.touch_session(sid, ts)
            if sid in self._known:
                return
            now = self.clock()
            if now - self._tried.get(sid, -1e18) >= self.RETRY:
                self._tried[sid] = now
                self._q.put((sid, ts))
        except Exception:
            pass

    def _work(self):
        while True:
            sid, ts = self._q.get()
            try:
                cwd, branch = find_transcript(self.dir, sid)
                if cwd:
                    self._known.add(sid)
                    self.store.touch_session(sid, ts, cwd, branch)
            except Exception:
                pass
            finally:
                self._q.task_done()
