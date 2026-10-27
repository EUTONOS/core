"""Unknown watch evidence and saved-choice write boundaries share one projection."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from eutonos import model_catalog as catalog, model_watch, settings


PIN = "gpt-7-fable"


class CatalogRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.root = self.home / "project"
        self.root.mkdir()
        environment = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home / "codex"),
                                                   "EUTONOS_HOME": str(self.home / "user")})
        environment.start()
        self.addCleanup(environment.stop)
        self.cache = self.home / "codex" / "models_cache.json"
        self.cache.parent.mkdir()
        self.native(True)
        model_watch.scan(self.root, now=100)

    def native(self, include_pin):
        ids = ["gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna", "gpt-6-astra"] + ([PIN] if include_pin else [])
        self.cache.write_text(json.dumps({"models": [{"slug": model, "visibility": "list",
            "supported_reasoning_levels": [{"effort": effort} for effort in catalog.EFFORTS]}
            for model in ids]}), encoding="utf-8")

    def save_pin_role(self):
        read = settings.read_roles(self.root)
        roles = copy.deepcopy(read["roles"])
        roles["saved-pin"] = {"options": [{"harness": "codex", "model": PIN, "effort": "high"}],
            "preamble": None, "autonomy": "ask", "edits": "ask", "description": None, "brief_template": None}
        return settings.update_roles(self.root, roles, expected_revision=read["revision"])

    def retire(self):
        self.native(False)
        model_watch.scan(self.root, now=200)
        model_watch.scan(self.root, now=200 + model_watch.ABSENCE_INTERVAL)
        self.assertFalse(catalog.describe_catalog(self.root)["models"][PIN]["available"])

    def damage(self, *, missing=False):
        path = model_watch.state_path(self.root)
        if missing:
            path.unlink()
        else:
            path.write_bytes(b"{broken")

    def test_pending_pin_survives_corrupt_and_missing_state_in_process(self):
        self.native(False)
        model_watch.scan(self.root, now=200)
        before = catalog.describe_catalog(self.root)
        self.assertTrue(before["models"][PIN]["available"])
        good = model_watch.state_path(self.root).read_bytes()
        for missing in (False, True):
            with self.subTest(missing=missing):
                model_watch.state_path(self.root).write_bytes(good)
                self.damage(missing=missing)
                after = catalog.describe_catalog(self.root)
                self.assertEqual(after["watch_state"], "unknown")
                self.assertTrue(after["models"][PIN]["available"])
                self.assertEqual(after["models"][PIN]["supported_efforts"], before["models"][PIN]["supported_efforts"])
                self.assertEqual(catalog.resolve_choice(PIN, "codex", root=self.root, effort="high")["resolved"], PIN)

    def test_confirmed_retirement_stays_refused_when_state_breaks(self):
        self.save_pin_role()
        self.retire()
        self.damage()
        pin = catalog.describe_catalog(self.root)["models"][PIN]
        self.assertFalse(pin["available"])
        with self.assertRaisesRegex(ValueError, "unavailable"):
            catalog.resolve_choice(PIN, "codex", root=self.root)
        before = settings.read_roles(self.root)
        updated = settings.update_roles(self.root, before["roles"], expected_revision=before["revision"])
        self.assertEqual(updated["roles"]["saved-pin"]["options"], before["roles"]["saved-pin"]["options"])

    def test_new_retired_role_refused_but_same_stored_option_can_update_description(self):
        self.save_pin_role()
        self.retire()
        before = settings.read_roles(self.root)
        roles = copy.deepcopy(before["roles"])
        roles["saved-pin"]["description"] = "Keep the stored pin while editing its description"
        saved = settings.update_roles(self.root, roles, expected_revision=before["revision"])
        roles["new-retired-pin"] = copy.deepcopy(roles["saved-pin"])
        with self.assertRaisesRegex(ValueError, "Unavailable"):
            settings.update_roles(self.root, roles, expected_revision=saved["revision"])
        with self.assertRaisesRegex(ValueError, "Unavailable"):
            settings._validate_role_option({"harness": "codex", "model": PIN, "effort": "high"}, root=self.root)

    def test_retired_routing_replay_is_allowed_but_new_selection_is_not(self):
        routing = {"enabled": True, "routing": "fixed", "model": PIN, "effort": "high",
                   "allowed_models": [PIN, "sol"], "routing_profile": "optimus", "max_concurrent_companions": 1}
        state = settings.status(self.root)
        settings.update(self.root, 0, state["revision"], "routing", routing)
        self.retire()
        state = settings.status(self.root)
        saved = settings.update(self.root, 0, state["revision"], "routing", {**routing, "enabled": False})
        self.assertEqual(saved["routing"]["model"], PIN)
        with self.assertRaisesRegex(ValueError, "Invalid allowed models"):
            settings.update(self.root, 0, saved["revision"], "routing", {**routing, "routing": "adaptive",
                "model": None, "effort": None, "allowed_models": [PIN]})
        with self.assertRaisesRegex(ValueError, "Invalid allowed models"):
            settings.validate_routing(routing, root=self.root)

    def test_fresh_defaults_exclude_retired_models(self):
        self.retire()
        fresh = self.home / "fresh"
        fresh.mkdir()
        self.assertNotIn(PIN, settings.status(fresh)["routing"]["allowed_models"])

    def test_fresh_process_can_attempt_only_saved_exact_pin_when_watch_unknown(self):
        self.save_pin_role()
        self.native(False)
        model_watch.scan(self.root, now=200)
        self.damage()
        program = ("import json,sys;sys.path.insert(0,sys.argv[1]);from eutonos import model_catalog as c,settings;"
                   "root=sys.argv[2];cat=c.describe_catalog(root);r=settings.read_roles(root);"
                   "saved=settings.update_roles(root,r['roles'],expected_revision=r['revision']);"
                   "print(json.dumps({'pin':cat['models'][sys.argv[3]],'resolved':"
                   "c.resolve_choice(sys.argv[3],'codex',root=root,effort='high')['resolved'],"
                   "'alias':'fable' in cat['models'],'default':sys.argv[3] in settings._default_models(root)}))")
        child = subprocess.run([sys.executable, "-c", program,
            str(Path(__file__).resolve().parents[1] / "src"), str(self.root), PIN],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(child.returncode, 0, child.stderr)
        result = json.loads(child.stdout)
        self.assertEqual(result["resolved"], PIN)
        self.assertEqual(result["pin"]["basis"], "saved-pin-unknown")
        self.assertEqual(result["pin"]["supported_efforts"], ["high"])
        self.assertFalse(result["alias"])
        self.assertFalse(result["default"])

    def test_allowed_alternatives_and_invalid_saved_fields_cannot_invent_pin_evidence(self):
        self.native(False)
        self.damage()
        folder = self.root / ".eutonos-local"
        folder.mkdir(exist_ok=True)
        record = {"profile": settings.COMPANION_PROFILE, "enabled": False, "routing": "adaptive",
            "model": None, "effort": None, "allowed_models": [PIN], "routing_profile": "optimus",
            "max_concurrent_companions": 1}
        (folder / "companion.json").write_text(json.dumps(record), encoding="utf-8")
        self.assertNotIn(PIN, catalog._saved_pins(self.root))
        (folder / "settings.json").write_text(json.dumps({"profile": settings.PROFILE,
            "private_unknown_field": "not a settings record", "roles": {"bad": {
                "options": [{"harness": "codex", "model": PIN, "effort": "high"}],
                "preamble": None, "autonomy": "ask"}}}), encoding="utf-8")
        self.assertNotIn(PIN, catalog._saved_pins(self.root))

    def test_last_good_cache_is_isolated_by_user_watch_home(self):
        self.retire()
        with mock.patch.dict(os.environ, {"EUTONOS_HOME": str(self.home / "another-user")}):
            self.assertNotIn(PIN, catalog.describe_catalog(self.root)["models"])

    def test_replaying_stored_choice_does_not_bypass_luna_write_floor(self):
        self.save_pin_role()
        path = self.root / ".eutonos-local" / "settings.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["roles"]["old-luna"] = {"options": [{"harness": "codex", "model": "gpt-6-luna", "effort": "high"}],
            "preamble": None, "autonomy": "ask", "edits": "ask", "description": None, "brief_template": None}
        path.write_text(json.dumps(record), encoding="utf-8")
        before = settings.read_roles(self.root)
        self.assertEqual(before["roles"]["old-luna"]["options"][0]["effort"], "high")
        with self.assertRaisesRegex(ValueError, "Luna's minimum effort"):
            settings.update_roles(self.root, before["roles"], expected_revision=before["revision"])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["roles"]["old-luna"]["options"][0]["effort"], "high")
        routing = {"profile": settings.COMPANION_PROFILE, "enabled": True, "routing": "fixed",
            "model": "gpt-6-luna", "effort": "high", "allowed_models": ["gpt-6-luna"],
            "routing_profile": "optimus", "max_concurrent_companions": 1}
        (path.parent / "companion.json").write_text(json.dumps(routing), encoding="utf-8")
        state = settings.status(self.root)
        with self.assertRaisesRegex(ValueError, "Luna's minimum effort"):
            settings.update(self.root, 0, state["revision"], "routing", {**state["routing"], "enabled": False})

    def test_invalid_role_choices_create_no_project_files(self):
        fresh = self.home / "invalid-input-project"
        fresh.mkdir()
        roles = settings.read_roles(fresh)
        with self.assertRaises(ValueError):
            settings.update_roles(fresh, {"bad": {"options": [], "preamble": None, "autonomy": "ask"}},
                                  expected_revision=roles["revision"])
        self.assertFalse((fresh / ".eutonos-local").exists())
