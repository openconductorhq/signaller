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

    def test_key_never_crosses_schemes(self):
        k = Keys(self.d)
        k.set("lemmi", "https://a.example", "KS")
        self.assertEqual(k.get("lemmi", "https://a.example"), "KS")
        self.assertEqual(k.get("lemmi", "http://a.example"), "")   # an http URL on the same host: no https key
        k.set("lemmi", "http://a.example", "KH")
        self.assertEqual((k.get("lemmi", "https://a.example"), k.get("lemmi", "http://a.example")), ("KS", "KH"))
        k = Keys(self.d)  # re-read: both slots survive, each meaning its own scheme
        self.assertEqual((k.get("lemmi", "https://a.example"), k.get("lemmi", "http://a.example")), ("KS", "KH"))
        k.delete("lemmi", "http://a.example")
        self.assertEqual(Keys(self.d).get("lemmi", "https://a.example"), "KS")
        self.assertEqual(Keys(self.d).get("lemmi", "http://a.example"), "")

    def test_old_flat_file_migrates_to_current_hosts(self):
        with open(self.p, "w") as f:
            json.dump({"valar": "VK", "gone": "GK"}, f)
        k = Keys(self.d)
        k.migrate({"valar": "https://api.valarhq.ai"})
        self.assertEqual(Keys(self.d).get("valar", "https://api.valarhq.ai"), "VK")
        # no downstream to tie "gone" to: kept under the unknown slot, not dropped —
        self.assertEqual(Keys(self.d).get("gone", "https://x"), "")
        self.assertEqual(Keys(self.d).keys["gone"], {"": "GK"})
        Keys(self.d).migrate({"gone": "https://x"})  # — and tied when its downstream appears
        self.assertEqual(Keys(self.d).get("gone", "https://x"), "GK")

    def test_migrate_never_over_a_key_set_since(self):
        with open(self.p, "w") as f:
            json.dump({"lemmi": "OLD"}, f)
        k = Keys(self.d)  # pending; a console-set key for the same id+host is newer and stays
        k.set("lemmi", "https://x", "NEW")
        k.migrate({"lemmi": "https://x"})
        self.assertEqual(Keys(self.d).get("lemmi", "https://x"), "NEW")
        self.assertEqual(Keys(self.d).keys["lemmi"], {"x": "NEW"})  # the old one superseded, not beside it

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
