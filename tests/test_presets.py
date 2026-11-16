import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from unittest import mock

import console
import presets
import signaller
from keys import UNKNOWN, Keys
from routing import DEFAULT_CONFIG, MODEL_TIERS, config_from_json, config_to_json
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

    def test_switch_drops_listed_windows_when_the_url_changes(self):
        app = self.app()
        app.set_listed_context("valar", {"m": 850000})
        j = presets.portable(config_to_json(app.routing))
        j["downstreams"][0]["url"] = "http://other.example"  # same id, a new host: the old host's windows don't apply
        app.add_preset("b", "", j)
        app.activate_preset("b")
        self.assertEqual(app.listed_context, {})
        with open(os.path.join(self.dir, "models.json")) as f:
            self.assertNotIn("listed_context", json.load(f))
        app.activate_preset("my setup")  # back on the old host, nothing is listed again
        self.assertEqual(app.listed_context, {})

    def test_switch_keeps_listed_windows_when_the_url_is_unchanged(self):
        app = self.app()
        app.set_listed_context("valar", {"m": 850000})
        app.save_preset("b", "")
        app.activate_preset("b")
        self.assertEqual(app.listed_context, {"valar": {"m": 850000}})
        with open(os.path.join(self.dir, "models.json")) as f:
            self.assertEqual(json.load(f)["listed_context"], {"valar": {"m": 850000}})

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

    def test_portable_keeps_a_manual_context(self):
        j = config_to_json(DEFAULT_CONFIG)
        j["downstreams"][0]["context"] = {"m": 850000}
        self.assertEqual(presets.portable(j)["downstreams"][0]["context"], {"m": 850000})

    def test_portable_keeps_an_unparseable_line_for_the_reason(self):
        j = config_to_json(DEFAULT_CONFIG)
        j["settings"] = [{"when": "hour is not a thing", "downstream": "valar", "tier": None,
                          "set": "window", "value": "priority"}]
        self.assertEqual(presets.portable(j)["settings"], j["settings"])  # kept, not dropped and not raised here
        with self.assertRaises(ValueError) as e:
            presets.validated(j)
        self.assertIn("settings line 1", str(e.exception))  # the importer's reason, not a generic one

    def test_portable_rejects_a_non_object_config(self):
        for bad in ("a string", ["a", "list"], 5):
            with self.assertRaises(ValueError):
                presets.portable(bad)

    def test_unreadable_presets_file_falls_back(self):
        with open(os.path.join(self.dir, "presets.json"), "w") as f:
            f.write("{")
        app = self.app()
        self.assertTrue(os.path.exists(os.path.join(self.dir, "presets.json.bad")))
        self.assertIn("presets.json.bad", app.banner)
        self.assertEqual(app.preset_names(), ["my setup"])

    def test_active_name_must_name_a_preset(self):
        with open(os.path.join(self.dir, "presets.json"), "w") as f:
            json.dump({"active": "nope", "presets": [{"name": "my setup", "tagline": "",
                                                      "config": config_to_json(DEFAULT_CONFIG)}]}, f)
        app = self.app()  # valid in every other way: an active naming no preset is a broken store
        self.assertTrue(os.path.exists(os.path.join(self.dir, "presets.json.bad")))
        self.assertIn("presets.json.bad", app.banner)
        self.assertEqual(app.preset_names(), ["my setup"])

    def test_switch_refreshes_prices_off_the_request_path(self):
        app = self.app()
        j = presets.portable(config_to_json(app.routing))
        j["downstreams"][0]["url"] = "http://other.example"  # same id, a new host: prices/models are stale
        app.add_preset("b", "", j)
        app.keys.set("valar", "http://other.example", "KB")
        done = threading.Event()
        set_listed = app.prices.set_listed
        app.prices.set_listed = lambda d, vp: (set_listed(d, vp), done.set())
        saved = console.fetch_models
        console.fetch_models = lambda url, key, timeout=5: {"data": [
            {"id": "m1", "pricing": [{"tier": "realtime", "input_per_1m_tokens": 1,
                                      "cached_input_per_1m_tokens": 1, "output_per_1m_tokens": 1}]}]}
        try:
            app.activate_preset("b")
            self.assertTrue(done.wait(5))  # a background thread, not the request: the switch already answered
            self.assertEqual(app.prices.listed["valar"]["m1"], {"asap": [1.0, 1.0, 1.0]})
        finally:
            console.fetch_models = saved

    def test_migration_skipped_against_a_default_fallback_config(self):
        with open(os.path.join(self.dir, "downstreams.keys.json"), "w") as f:
            json.dump({"valar": "OLDVK"}, f)  # an old flat file, tied to nothing yet
        with open(os.path.join(self.dir, "console.json"), "w") as f:
            f.write("{")  # unreadable: the default config stands in, and its hosts aren't the person's
        app = self.app()
        self.assertIn("couldn't be read", app.banner)
        k = Keys(self.dir)
        self.assertEqual(k.get("valar", "https://api.valarhq.ai"), "")  # never tied to the default's valar host
        # it survives, under the unknown slot (seed's own env key went to the env url, not over it)
        self.assertEqual(k.get("valar", VALAR_URL), "VK")
        self.assertEqual(k.keys["valar"][UNKNOWN], "OLDVK")

    def test_key_migration_failure_is_a_banner_not_a_dead_router(self):
        app = self.app()  # a console.json exists: migration runs on the next start
        with mock.patch.object(Keys, "migrate", side_effect=OSError("disk full")):
            app2 = App(cfg(), self.dir)
        self.addCleanup(app2.store.close)
        self.assertIn("downstream keys", app2.banner.lower())
        self.assertEqual(app2.preset_names(), ["my setup"])  # the router started anyway

    def test_duplicate_and_bad_names_refused(self):
        app = self.app()
        with self.assertRaises(ValueError):
            app.save_preset("my setup", "")
        with self.assertRaises(ValueError):
            app.save_preset("", "")
        with self.assertRaises(ValueError):
            app.delete_preset("my setup")

    def test_reserved_names_refused(self):
        # the names are reserved to keep /ui/api/presets/<name> URLs unambiguous, not because they'd shadow anything
        for name in ("preview", "activate", "export", "Preview"):
            with self.assertRaises(ValueError):
                presets.check_name(name)

    def test_tagline_and_name_reject_control_characters(self):
        with self.assertRaises(ValueError):
            presets.check_tagline("two\nlines")
        with self.assertRaises(ValueError):
            presets.check_name("two\nlines")
        self.assertEqual(presets.check_tagline("still fine"), "still fine")

    def test_unwritable_config_dir_falls_back(self):
        saved = presets.save

        def boom(d, s):
            raise OSError("read-only")

        presets.save = boom
        try:
            store, banner = presets.load(self.dir)
        finally:
            presets.save = saved
        self.assertEqual([p["name"] for p in store["presets"]], ["my setup"])
        self.assertIn("couldn't be written", banner)

    def test_failed_put_restores_the_store(self):
        app = self.app()
        app.save_preset("b", "")
        before = config_to_json(app.routing)

        def boom(_new):
            raise OSError("disk full")

        app._put = boom
        with self.assertRaises(OSError):
            app.activate_preset("b")
        with open(os.path.join(self.dir, "presets.json")) as f:
            store = json.load(f)
        # the switch didn't happen: active back to "my setup", whose live config is its own (no snapshot), b keeps its config
        self.assertEqual(store["active"], "my setup")
        self.assertIsNone(store["presets"][0]["config"])
        self.assertEqual(store["presets"][1]["name"], "b")
        self.assertIsNotNone(store["presets"][1]["config"])
        self.assertEqual(app.presets["active"], "my setup")  # memory matches presets.json again
        self.assertIsNone(app.presets["presets"][0]["config"])
        app2 = App(cfg(), self.dir)  # a restart after such a crash finds the old setup intact
        self.addCleanup(app2.store.close)
        self.assertEqual(app2.preset_names(), ["my setup", "b"])
        self.assertEqual(config_to_json(app2.routing), before)
        self.assertFalse(app2.banner)  # no fallback banner: the store on disk is valid

    def test_failed_save_before_any_write_leaves_both_untouched(self):
        app = self.app()
        app.save_preset("b", "")
        calls = []

        def boom():
            calls.append(app.presets["active"])
            if len(calls) == 1:  # the (a) save fails: nothing about the switch reached the disk
                raise OSError("disk full")

        app._save_presets = boom
        with self.assertRaises(OSError):
            app.activate_preset("b")
        self.assertEqual(calls[0], "b")  # the failed save was the switch's first
        with open(os.path.join(self.dir, "presets.json")) as f:
            store = json.load(f)
        self.assertEqual(store["active"], "my setup")  # the undo-save wrote the store as it was
        self.assertIsNone(store["presets"][0]["config"])
        self.assertEqual(app.presets["active"], "my setup")
        self.assertEqual(self.console(), config_to_json(app.routing))  # console.json untouched
        app2 = App(cfg(), self.dir)
        self.addCleanup(app2.store.close)
        self.assertEqual(app2.preset_names(), ["my setup", "b"])
        self.assertFalse(app2.banner)

    def test_loader_finishes_an_interrupted_switch(self):
        app = self.app()
        app.save_preset("b", "")
        a_cfg, b_cfg = config_to_json(app.routing), app.preset_config("b")
        # the state a crash between activate_preset's two saves leaves: b active but still carrying its config,
        # "my setup" holding a snapshot — console.json still "my setup"'s
        app.presets["active"] = "b"
        app.presets["presets"][0]["config"] = a_cfg
        app._save_presets()
        app2 = App(cfg(), self.dir)
        self.addCleanup(app2.store.close)
        self.assertEqual(app2.presets["active"], "b")  # the switch is finished, not undone
        self.assertEqual(app2.presets["presets"][0]["config"], a_cfg)  # "my setup" keeps the snapshot as its own
        self.assertIsNone(app2.presets["presets"][1]["config"])  # b's was put, then cleared
        self.assertEqual(app2.routing, replace(config_from_json(b_cfg), seeded=True, migrated_off=()))
        self.assertEqual(self.console(), config_to_json(app2.routing))
        with open(os.path.join(self.dir, "presets.json")) as f:
            store = json.load(f)
        self.assertIsNone(store["presets"][1]["config"])  # and saved cleared
        app3 = App(cfg(), self.dir)  # a crash after console.json was put, before the clearing save: idempotent
        self.addCleanup(app3.store.close)
        self.assertEqual(config_to_json(app3.routing), config_to_json(app2.routing))
        self.assertFalse(app3.banner)


if __name__ == "__main__":
    unittest.main()
