import contextlib
import io
import json
import os
import tempfile
import unittest
from dataclasses import replace

import presets
import signaller
from routing import DEFAULT_CONFIG, MODEL_TIERS, config_to_json
from signaller import App, Config

VALAR_URL = "http://valar.example"


def cfg():
    return Config("127.0.0.1", 0, "http://a.example", (VALAR_URL, "VK"))


def lemmi_json(url="http://lemmi.example"):
    return {"id": "lemmi", "name": "Lemmi", "url": url, "kind": "generic",
            "models": dict.fromkeys(MODEL_TIERS, "m"), "color": "#888888", "windows": None, "prices": None}


class PresetTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        quiet = contextlib.redirect_stderr(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def app(self) -> App:
        app = App(cfg(), self.dir)
        self.addCleanup(app.store.close)
        return app

    def console(self) -> dict:
        with open(os.path.join(self.dir, "console.json")) as f:
            return json.load(f)

    def test_first_run_makes_my_setup(self):
        app = self.app()
        self.assertTrue(os.path.exists(os.path.join(self.dir, "presets.json")))
        with open(os.path.join(self.dir, "presets.json")) as f:
            store = json.load(f)
        self.assertEqual(store["active"], "my setup")
        self.assertEqual([p["name"] for p in store["presets"]], ["my setup"])
        self.assertIsNone(store["presets"][0]["config"])

    def test_switch_round_trip_keeps_edits(self):
        app = self.app()
        app.save_preset("b", "")
        app.activate_preset("b")
        app.set_routing(replace(app.routing, lever="valar"))
        app.activate_preset("my setup")
        self.assertEqual(app.routing.lever, "rules")  # the setup each preset had when last left
        app.activate_preset("b")
        self.assertEqual(app.routing.lever, "valar")
        self.assertEqual(self.console(), config_to_json(app.routing))
        self.assertEqual(self.console(), app.preset_config("b"))

    def test_switch_bumps_rev(self):
        app = self.app()
        app.save_preset("b", "")
        before = app.rev
        app.activate_preset("b")
        self.assertGreater(app.rev, before)

    def test_switch_keeps_keys_per_host(self):
        app = self.app()
        self.assertEqual(signaller.upstream(app, "valar")[2], "VK")  # seeded at host A
        j = presets.portable(config_to_json(app.routing))
        j["downstreams"][0]["url"] = "http://other.example"
        app.add_preset("b", "", j)
        app.activate_preset("b")
        self.assertEqual(signaller.upstream(app, "valar")[2], "")  # host B has no key yet
        app.keys.set("valar", "http://other.example", "KB")
        self.assertEqual(signaller.upstream(app, "valar")[2], "KB")
        app.activate_preset("my setup")
        self.assertEqual(signaller.upstream(app, "valar")[2], "VK")  # host A's key is untouched

    def test_delete_downstream_keeps_shared_key(self):
        app = self.app()
        app.add_downstream(lemmi_json(), "LK")
        app.save_preset("b", "")
        self.assertEqual(app.delete_downstream("lemmi"), [])
        self.assertEqual([d.id for d in app.routing.downstreams], ["valar"])
        self.assertEqual(app.keys.get("lemmi", "http://lemmi.example"), "LK")  # preset "b" still has it

    def test_portable_drops_machine_and_session_state(self):
        j = config_to_json(DEFAULT_CONFIG)
        j["seeded"] = True
        j["migrated_off"] = ["local"]
        j["settings"] = [
            {"when": "session is abc", "downstream": "valar", "tier": None, "set": "window", "value": "priority"},
            {"when": "hour >= 22", "downstream": "valar", "tier": "sonnet", "set": "window", "value": "priority"},
        ]
        out = presets.portable(j)
        self.assertNotIn("seeded", out)
        self.assertNotIn("migrated_off", out)
        self.assertEqual([s["when"] for s in out["settings"]], ["hour >= 22"])

    def test_unreadable_presets_file_falls_back(self):
        with open(os.path.join(self.dir, "presets.json"), "w") as f:
            f.write("{")
        app = self.app()
        self.assertTrue(os.path.exists(os.path.join(self.dir, "presets.json.bad")))
        self.assertIn("presets.json.bad", app.banner)
        self.assertEqual(app.preset_names(), ["my setup"])

    def test_duplicate_and_bad_names_refused(self):
        app = self.app()
        with self.assertRaises(ValueError):
            app.save_preset("my setup", "")
        with self.assertRaises(ValueError):
            app.save_preset("", "")
        with self.assertRaises(ValueError):
            app.delete_preset("my setup")


if __name__ == "__main__":
    unittest.main()
