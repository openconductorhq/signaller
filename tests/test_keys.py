import json
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
        self.assertFalse(k.has("a", "https://x"))
        k.set("a", "https://x", "ka")
        k.set("b", "https://y", "kb")
        self.assertEqual(oct(os.stat(self.p).st_mode & 0o777), "0o600")
        k = Keys(self.d)  # re-read from disk
        self.assertEqual((k.get("a", "https://x"), k.get("b", "https://y"), k.get("zz", "https://x"),
                          k.has("a", "https://x")), ("ka", "kb", "", True))
        k.delete("a", "https://x")
        self.assertEqual((Keys(self.d).get("a", "https://x"), Keys(self.d).get("b", "https://y")), ("", "kb"))
        self.assertEqual(oct(os.stat(self.p).st_mode & 0o777), "0o600")

    def test_key_follows_host(self):
        k = Keys(self.d)
        k.set("lemmi", "https://a.example/v1", "KA")
        self.assertEqual(k.get("lemmi", "https://A.example/other"), "KA")   # host, case-insensitive
        self.assertEqual(k.get("lemmi", "https://b.example"), "")            # another host: missing
        k.set("lemmi", "https://b.example", "KB")
        self.assertEqual((k.get("lemmi", "https://a.example"), k.get("lemmi", "https://b.example")), ("KA", "KB"))
        k.delete("lemmi", "https://b.example")
        self.assertEqual(Keys(self.d).get("lemmi", "https://a.example"), "KA")

    def test_old_flat_file_migrates_to_current_hosts(self):
        with open(self.p, "w") as f:
            json.dump({"valar": "VK", "gone": "GK"}, f)
        k = Keys(self.d)
        k.migrate({"valar": "https://api.valarhq.ai"})
        self.assertEqual(Keys(self.d).get("valar", "https://api.valarhq.ai"), "VK")
        self.assertEqual(Keys(self.d).get("gone", "https://x"), "")   # no downstream to tie it to: dropped

    def test_migrate_noop_on_new_shape(self):
        Keys(self.d).set("a", "https://x", "ka")
        Keys(self.d).migrate({"a": "https://y"})
        self.assertEqual((Keys(self.d).get("a", "https://x"), Keys(self.d).get("a", "https://y")), ("ka", ""))

    def test_corrupt_file_reads_empty(self):
        for text in ("not json{", '["a"]', '{"a": 1}'):
            with open(self.p, "w") as f:
                f.write(text)
            self.assertEqual(Keys(self.d).get("a", "https://x"), "")
            self.assertTrue(os.path.exists(self.p + ".bad"))
            self.assertFalse(os.path.exists(self.p))

    def test_concurrent_sets_all_land(self):
        k = Keys(self.d)
        ts = [threading.Thread(target=k.set, args=("id%d" % i, "https://h", "k%d" % i)) for i in range(20)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(len(Keys(self.d).keys), 20)
