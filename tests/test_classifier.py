import json
import threading
import time
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from classifier import Classifier
from routing import ClassifierSettings, ClassResult, Features, Label

LABELS = (Label("coding", "writing code", "a"), Label("chat", "small talk", "b"))
S = ClassifierSettings("gemma-haiku", 0.5, 2.0, LABELS)


def feat(text="hello", key="t1"):
    return Features(text, [], "claude-sonnet-4-5", 10, False, key)


class Fake(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, content='{"label": "coding", "confidence": 0.88}', delay=0):
        super().__init__(("127.0.0.1", 0), H)
        self.content, self.delay, self.seen = content, delay, []
        threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.server_address[1]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["content-length"]))
        self.server.seen.append((self.headers.get("authorization"), json.loads(body)))
        time.sleep(self.server.delay)
        out = json.dumps({"choices": [{"message": {"content": self.server.content}}]}).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class T(unittest.TestCase):
    def fake(self, **kw):
        f = Fake(**kw)
        self.addCleanup(f.shutdown)
        self.addCleanup(f.server_close)
        return f, Classifier(f.url, "k")

    def test_labels_and_confidence(self):
        f, c = self.fake(content='Sure: {"label": "coding", "confidence": 0.88}')
        self.assertEqual(c(feat("write a parser"), S), ClassResult("coding", 0.88, ""))
        auth, req = f.seen[0]
        self.assertEqual((auth, req["model"], req["messages"][1]["content"]), ("Bearer k", "gemma-haiku", "write a parser"))

    def test_cached_per_turn(self):
        f, c = self.fake()
        c(feat(), S); c(feat(), S)
        self.assertEqual(len(f.seen), 1)
        c(feat("x", "t2"), S)
        self.assertEqual(len(f.seen), 2)
        c(feat(), replace(S, labels=LABELS[:1]))
        self.assertEqual(len(f.seen), 3)

    def test_timeout_then_late_answer_cached(self):
        f, c = self.fake(delay=0.3)
        s = replace(S, timeout_s=0.1)
        t = time.time()
        r = c(feat(), s)
        self.assertLess(time.time() - t, 0.2)
        self.assertEqual((r.label, r.note), (None, "timed out after 0.1 s"))
        time.sleep(0.4)
        self.assertEqual(c(feat(), s).label, "coding")
        self.assertEqual(len(f.seen), 1)

    def test_concurrent_same_text_one_call(self):
        f, c = self.fake(delay=0.2)
        out = []
        ts = [threading.Thread(target=lambda: out.append(c(feat(), S).label)) for _ in range(5)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual((len(f.seen), out), (1, ["coding"] * 5))

    def test_bad_answers(self):
        for n, content in enumerate(["no idea", '{"label":"poetry","confidence":0.9}',
                                     '{"label":"coding","confidence":"high"}', '{"label":"coding","confidence":2}']):
            f, c = self.fake(content=content)
            r = c(feat(key=str(n)), S)
            self.assertIsNone(r.label)
            self.assertTrue(r.note.startswith("error:"), r.note)

    def test_litellm_down(self):
        f, c = self.fake()
        c.url = "http://127.0.0.1:1"
        t = time.time()
        r = c(feat(), S)
        self.assertTrue(r.note.startswith("error:"), r.note)
        self.assertLess(time.time() - t, S.timeout_s + 0.1)

    def test_text_capped_4000(self):
        f, c = self.fake()
        c(feat("a" * 10000), S)
        self.assertEqual(len(f.seen[0][1]["messages"][1]["content"]), 4000)
