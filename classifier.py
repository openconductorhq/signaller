"""Prompt classifier: asks a local LiteLLM model to pick one label; cached per turn, time-limited."""
import json
import re
import threading
from collections import OrderedDict
from http.client import HTTPConnection, HTTPSConnection
from urllib.parse import urlsplit

from routing import ClassResult, ClassifierSettings, Features

BAD = ClassResult(None, None, "error: unusable answer")


class Classifier:
    def __init__(self, litellm_url: str, litellm_key: str, cache_size: int = 512):
        self.url, self.key, self.size = litellm_url.rstrip("/"), litellm_key, cache_size
        self.cache = OrderedDict()  # key -> ClassResult (LRU)
        self.inflight = {}  # key -> Event
        self.lock = threading.Lock()

    def __call__(self, f: Features, s: ClassifierSettings) -> ClassResult:
        key = (f.turn_key, s.model, tuple((l.name, l.description) for l in s.labels))
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            ev = self.inflight.get(key)
            if ev is None:
                ev = self.inflight[key] = threading.Event()
                threading.Thread(target=self._run, args=(key, ev, f.text, s), daemon=True).start()
        if not ev.wait(s.timeout_s):
            return ClassResult(None, None, "timed out after %g s" % s.timeout_s)
        with self.lock:
            return self.cache[key]

    def _run(self, key, ev, text, s):
        try:
            res = self._ask(text, s)
        except Exception as e:  # network/HTTP/JSON; reason only, never the text
            res = ClassResult(None, None, "error: %s" % (str(e) or type(e).__name__)[:80])
        with self.lock:
            self.cache[key] = res
            while len(self.cache) > self.size:
                self.cache.popitem(last=False)
            del self.inflight[key]
        ev.set()

    def _ask(self, text, s):
        names = [l.name for l in s.labels]
        instr = ("Classify the user's message into exactly one label:\n"
                 + "\n".join("- %s: %s" % (l.name, l.description) for l in s.labels)
                 + '\nReply with only JSON: {"label": "<one of: %s>", "confidence": <0..1>}' % ", ".join(names))
        body = json.dumps({"model": s.model, "temperature": 0, "max_tokens": 200, "messages": [
            {"role": "system", "content": instr}, {"role": "user", "content": text[:4000]}]})
        u = urlsplit(self.url)
        conn = (HTTPSConnection if u.scheme == "https" else HTTPConnection)(u.netloc, timeout=max(s.timeout_s, 1) + 5)
        try:
            conn.request("POST", u.path + "/v1/chat/completions", body,
                         {"content-type": "application/json", "authorization": "Bearer " + self.key})
            r = conn.getresponse()
            data = r.read()
            if r.status != 200:
                return ClassResult(None, None, "error: HTTP %d" % r.status)
        finally:
            conn.close()
        try:
            m = re.search(r"\{.*?\}", json.loads(data)["choices"][0]["message"]["content"], re.DOTALL)
            ans = json.loads(m.group())
            label, conf = ans["label"], ans["confidence"]
            if label in names and isinstance(conf, (int, float)) and not isinstance(conf, bool) and 0 <= conf <= 1:
                return ClassResult(label, float(conf), "")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            pass
        return BAD
