"""Downstream API keys: one 0600 file, {id: {host: key}}, kept out of console.json, every API answer and every log line."""
import functools
import json
import os
import sys
import tempfile
import threading
from urllib.parse import urlsplit


UNKNOWN = ""  # the slot a key sits under while no host is known for it: no URL maps there, a later migrate ties it


@functools.lru_cache(maxsize=64)
def host_of(url: str) -> str:
    """The slot a key lives under, by origin: https under the bare host (a bare host in an old file means
    https), http under scheme://host — so a key never leaves for a URL of another scheme."""
    u = urlsplit(url)
    return u.netloc.lower() if u.scheme == "https" else "http://" + u.netloc.lower()


def write_json(path: str, obj, indent=None) -> None:
    """Atomic, durable JSON write: a 0600 temp file (mkstemp) in the same dir, fsync, rename over."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class Keys:
    def __init__(self, config_dir: str):
        self.path = os.path.join(config_dir, "downstreams.keys.json")
        self.lock = threading.Lock()
        self.pending = None  # an old flat {id: key} file, held until migrate() ties each id to a host
        self.keys = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path) as f:
                keys = json.load(f)
            if isinstance(keys, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in keys.items()):
                self.pending = keys  # the old flat shape
                return {}
            if not (isinstance(keys, dict) and all(
                    isinstance(k, str) and isinstance(v, dict) and
                    all(isinstance(h, str) and isinstance(x, str) for h, x in v.items())
                    for k, v in keys.items())):
                raise ValueError("not an {id: {host: key}} object")
            return keys
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:  # reason only, never the file's text
            print("downstream keys unreadable: %s" % type(e).__name__, file=sys.stderr, flush=True)
            try:
                os.replace(self.path, self.path + ".bad")
            except OSError:
                pass
            return {}

    def migrate(self, hosts: dict[str, str]) -> None:
        """Tie each key still without a host — an old flat file's, or one a start left under the unknown slot —
        to the hosts of the current setup. An id with no downstream here is kept under the unknown slot, tied
        by a later migrate; a key the person set for that host since stays theirs. Never loses a key."""
        with self.lock:
            old = dict(self.pending or {})
            for i, d in self.keys.items():  # keys tied earlier but left waiting (a start with no host for them)
                if UNKNOWN in d and i not in old:
                    old[i] = d[UNKNOWN]
            if not old:
                self.pending = None
                return
            out = {i: dict(d) for i, d in self.keys.items()}
            for i, key in old.items():
                d = out.setdefault(i, {})
                if hosts.get(i):
                    d.setdefault(host_of(hosts[i]), key)  # never over a key set for that host since
                    d.pop(UNKNOWN, None)
                else:
                    d.setdefault(UNKNOWN, key)
            if out != self.keys:
                self._save(out)
            self.pending = None

    def get(self, id: str, url: str) -> str:
        return self.keys.get(id, {}).get(host_of(url), "")

    def has(self, id: str, url: str) -> bool:
        return bool(self.get(id, url))

    def _carry(self, keys: dict) -> dict:  # caller holds self.lock
        """The flat file's still-untied ids ride along under the unknown slot, so a write while they wait
        never drops them off the file."""
        if not self.pending:
            return keys
        out = dict(keys)
        for i, key in self.pending.items():
            out.setdefault(i, {}).setdefault(UNKNOWN, key)
        return out

    def set(self, id: str, url: str, key: str) -> None:
        with self.lock:  # build inside the lock: two concurrent sets must both land
            self._save(self._carry({**self.keys, id: {**self.keys.get(id, {}), host_of(url): key}}))

    def delete(self, id: str, url: str) -> None:
        with self.lock:
            d = {h: k for h, k in self.keys.get(id, {}).items() if h != host_of(url)}
            self._save(self._carry({**self.keys, id: d} if d else {k: v for k, v in self.keys.items() if k != id}))

    def _save(self, keys: dict) -> None:  # caller holds self.lock
        write_json(self.path, keys)
        self.keys = keys
