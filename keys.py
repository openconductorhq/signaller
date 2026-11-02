"""Downstream API keys: one 0600 file, {id: key}, kept out of console.json, every API answer and every log line."""
import json
import os
import sys
import tempfile
import threading


class Keys:
    def __init__(self, config_dir: str):
        self.path = os.path.join(config_dir, "downstreams.keys.json")
        self.lock = threading.Lock()
        self.keys = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path) as f:
                keys = json.load(f)
            if not (isinstance(keys, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in keys.items())):
                raise ValueError("not an {id: key} object")
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

    def get(self, id: str) -> str:
        return self.keys.get(id, "")

    def has(self, id: str) -> bool:
        return bool(self.keys.get(id))

    def set(self, id: str, key: str) -> None:
        self._save({**self.keys, id: key})

    def delete(self, id: str) -> None:
        self._save({k: v for k, v in self.keys.items() if k != id})

    def _save(self, keys: dict) -> None:
        with self.lock:
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path))  # 0600 from the start
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(keys, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
            self.keys = keys
