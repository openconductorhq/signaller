import os
import tempfile
import threading
import unittest

from keys import Keys


class KeysTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "downstreams.keys.json")

    def test_round_trip_and_mode(self):
        k = Keys(self.d)
        self.assertFalse(k.has("a"))
        k.set("a", "ka")
        k.set("b", "kb")
        self.assertEqual(oct(os.stat(self.p).st_mode & 0o777), "0o600")
        k = Keys(self.d)  # re-read from disk
        self.assertEqual((k.get("a"), k.get("b"), k.get("zz"), k.has("a")), ("ka", "kb", "", True))
        k.delete("a")
        self.assertEqual((Keys(self.d).get("a"), Keys(self.d).get("b")), ("", "kb"))
        self.assertEqual(oct(os.stat(self.p).st_mode & 0o777), "0o600")

    def test_corrupt_file_reads_empty(self):
        for text in ("not json{", '["a"]', '{"a": 1}'):
            with open(self.p, "w") as f:
                f.write(text)
            self.assertEqual(Keys(self.d).get("a"), "")
            self.assertTrue(os.path.exists(self.p + ".bad"))
            self.assertFalse(os.path.exists(self.p))

    def test_concurrent_sets_all_land(self):
        k = Keys(self.d)
        ts = [threading.Thread(target=k.set, args=("id%d" % i, "k%d" % i)) for i in range(20)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(len(Keys(self.d).keys), 20)
