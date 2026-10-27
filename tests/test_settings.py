import importlib.util
import json
import os
import errno
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import subprocess
import tempfile
import time
from threading import Event, Thread
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from eutonos import settings
from eutonos import local_lock
from eutonos.dashboard_cli import app

SPEC = importlib.util.spec_from_file_location("settings_tool", Path(__file__).resolve().parents[1] / "tools/settings.py")
settings_tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(settings_tool)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eutonos-settings-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()

    def update(self, section, value, current=None):
        current = current or settings.status(self.root)
        return settings.update(self.root, 0, current["revision"], section, value)

    def test_defaults_are_read_only_and_complete(self):
        value = settings.status(self.root, 3)
        self.assertEqual(value["project"], 3)
        self.assertEqual(value["task_rules"], dict(settings.DEFAULT_TASK_RULES))
        self.assertEqual(value["ram"], {"retrieval": "on-demand", "capture": True,
                                        "cadence": "every-turn", "detail": "summary"})
        self.assertEqual(value["tracking"], {"enabled": True})
        self.assertFalse(value["routing"]["enabled"])
        self.assertEqual(value["models"], list(settings.MODELS))
        self.assertEqual(value["efforts"], list(settings.EFFORTS))
        self.assertFalse((self.root / ".eutonos-local").exists())

    def test_native_model_efforts_and_unknown_costs(self):
        value = settings.status(self.root)
        catalog = value["model_catalog"]
        self.assertEqual(catalog["recorded_on"], "2026-10-14")
        # Native capability (supported_efforts) is separate from the owner-allowed
        # floor a model's "efforts" list may apply on top of it (Luna's minimum is
        # xhigh); check the native list directly rather than assuming "efforts"
        # always contains every natively supported value.
        self.assertIn("medium", catalog["models"]["gpt-6-luna"]["supported_efforts"])
        self.assertIn("xhigh", catalog["models"]["gpt-6-luna"]["efforts"])
        self.assertIn("ultra", catalog["models"]["gpt-6-sol"]["efforts"])
        self.assertEqual(catalog["costs"]["state"], "unknown")
        self.assertEqual(settings.MODEL_EFFORTS["gpt-6-luna"], catalog["models"]["gpt-6-luna"]["efforts"])
        self.assertEqual(settings.MODEL_EFFORTS["gpt-6-sol"], catalog["models"]["gpt-6-sol"]["efforts"])

        routing = {"enabled": True, "routing": "fixed", "model": "gpt-6-luna", "effort": "xhigh",
                   "allowed_models": ["gpt-6-luna", "gpt-6-sol"]}
        updated = self.update("routing", routing)
        # task_rules now maps a task to a role NAME (AWR worker profiles); it is a
        # plain string, not a {model, effort} pair, and is not cross-checked against
        # the live roles section at write time (see settings._validate_task_rules).
        rules = dict(updated["task_rules"], docs="worker-hard")
        self.assertEqual(self.update("task_rules", rules, updated)["task_rules"]["docs"], "worker-hard")
        rules["docs"] = 123  # not a string
        with self.assertRaises(ValueError):
            self.update("task_rules", rules)

    def test_sections_save_to_their_existing_owners_and_change_revision(self):
        before = settings.status(self.root)
        routing = {"enabled": True, "routing": "fixed", "model": "gpt-6-astra",
                   "effort": "high", "allowed_models": ["gpt-6-luna", "gpt-6-astra"]}
        after_routing = self.update("routing", routing, before)
        self.assertNotEqual(before["revision"], after_routing["revision"])
        self.assertEqual(after_routing["routing"], {
            **routing, "routing_profile": "optimus", "max_concurrent_companions": 1})
        self.assertEqual(json.loads((self.root / ".eutonos-local/companion.json").read_text())["profile"],
                         settings.COMPANION_PROFILE)

        rules = dict(after_routing["task_rules"], docs="orchestrator", git="worker-routine")
        after_rules = self.update("task_rules", rules, after_routing)
        self.assertEqual(after_rules["task_rules"], rules)
        after_ram = self.update("ram", {"retrieval": "off", "capture": False}, after_rules)
        self.assertEqual(after_ram["ram"], {"retrieval": "off", "capture": False,
                                            "cadence": "every-turn", "detail": "summary"})
        after_tracking = self.update("tracking", {"enabled": False}, after_ram)
        self.assertFalse(after_tracking["tracking"]["enabled"])
        history = json.loads((self.root / ".eutonos-local/companion-history.json").read_text())
        self.assertFalse(history["tracking"]["enabled"])

    def test_routing_preferences_are_renamed_and_optimus_is_the_default(self):
        self.assertEqual(settings.ROUTING_PREFERENCES, ("cost-savings", "optimus", "high-quality"))
        self.assertEqual(settings.read_companion(self.root)["routing_profile"], "optimus")

    def _write_companion(self, routing_profile):
        path = self.root / ".eutonos-local" / "companion.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "profile": settings.COMPANION_PROFILE, "enabled": False, "routing": "profile",
            "model": None, "effort": None, "allowed_models": list(settings.MODELS),
            "routing_profile": routing_profile, "max_concurrent_companions": 1}), encoding="utf-8")
        return path

    def test_unrecognized_routing_profile_still_raises(self):
        self._write_companion("nonsense")
        with self.assertRaises(ValueError):
            settings.read_companion(self.root)

    def _write_companion_full(self, **overrides):
        path = self.root / ".eutonos-local" / "companion.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {"profile": settings.COMPANION_PROFILE, "enabled": False, "routing": "profile",
                 "model": None, "effort": None, "allowed_models": list(settings.MODELS),
                 "routing_profile": "optimus", "max_concurrent_companions": 1}
        value.update(overrides)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def _write_local(self, **overrides):
        path = self.root / ".eutonos-local" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {"profile": settings.PROFILE,
                 "task_rules": {task: "orchestrator" for task in settings.TASKS},
                 "ram": {"retrieval": "on-demand", "capture": True, "cadence": "every-turn", "detail": "summary"},
                 "roles": {}, "role_defaults_seeded": [],
                 "hooks": json.loads(json.dumps(settings.DEFAULT_HOOKS))}
        value.update(overrides)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_saved_copy_of_older_builtin_hook_set_still_reads(self):
        # 2026-09-29: every saved settings.json carries the built-in set. Changing
        # built-in text must not make an older file unreadable; a read refreshes it.
        hooks = json.loads(json.dumps(settings.DEFAULT_HOOKS))
        builtin = settings.BUILTIN_HOOK_SET_NAME
        hooks["sets"][builtin]["roles"]["default"]["message"] = "older built-in text"
        hooks["sets"][builtin]["roles"]["orchestrator"]["message"] = "close out and push own paths."
        hooks["sets"]["owner-copy"] = json.loads(json.dumps(hooks["sets"][builtin]))
        path = self._write_local(hooks=hooks)
        original = path.read_bytes()
        current = settings.BUILTIN_HOOK_SETS[builtin]
        self.assertEqual(settings.read_hooks(self.root)["hooks"]["sets"][builtin], current)
        self.assertEqual(settings.read_hooks_bounded(self.root)["sets"][builtin], current)
        self.assertIn("discovery", settings.read_roles(self.root)["roles"])
        self.assertEqual(settings.read_hooks(self.root)["hooks"]["sets"]["owner-copy"], hooks["sets"]["owner-copy"])
        self.assertEqual(path.read_bytes(), original)  # Refresh built-in on read, never migrate custom copies.
        # A write that really edits the built-in set is still refused.
        with self.assertRaises(ValueError):
            settings.validate_hooks(hooks)

    def test_file_missing_a_newer_section_reads_with_its_default(self):
        # Forward compatibility, not a legacy reader: a section added after a
        # file was written starts at its default. Unknown keys still fail.
        path = self._write_local()
        value = json.loads(path.read_text(encoding="utf-8"))
        del value["hooks"]
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(settings.read_hooks_bounded(self.root), settings.DEFAULT_HOOKS)
        self.assertIn("ram", settings.status(self.root))
        self._write_local(unexpected={})
        with self.assertRaises(ValueError):
            settings.status(self.root)

    def test_retired_mailbox_model_mode_key_in_an_old_file_is_ignored_and_dropped(self):
        # The key belonged to the retired file mailbox. A file saved while it existed must
        # still load (even when its stored value would no longer validate), nothing reads
        # the value, and the next save leaves it out.
        path = self._write_local(mailbox_model_mode={"mode": "my-choices", "allowed_models": ["no-such-model"]})
        self.assertIn("mailbox_model_mode", json.loads(path.read_text(encoding="utf-8")))
        self.assertNotIn("mailbox_model_mode", settings._local(self.root))
        self.assertNotIn("mailbox_model_mode", settings.status(self.root))
        self.assertEqual(settings.read_hooks_bounded(self.root), settings.DEFAULT_HOOKS)
        roles = settings.read_roles(self.root)
        settings.update_roles(self.root, roles["roles"], expected_revision=roles["revision"])
        self.assertNotIn("mailbox_model_mode", json.loads(path.read_text(encoding="utf-8")))
        # A key that was never part of the settings is still refused.
        self._write_local(unexpected={})
        with self.assertRaises(ValueError):
            settings.status(self.root)

    # Stored model IDs remain exact pins, including older native choices.
    def test_existing_model_pins_preserved_on_read(self):
        # The owner's actual stored companion allowed_models (2026-09-28).
        self._write_companion_full(
            allowed_models=["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra"])
        result = settings.read_companion(self.root)
        self.assertEqual(result["allowed_models"], ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra"])

    def test_terra_only_allowed_models_remains_exact(self):
        self._write_companion_full(allowed_models=["gpt-5.6-terra"])
        result = settings.read_companion(self.root)
        self.assertEqual(result["allowed_models"], ["gpt-5.6-terra"])

    def test_terra_fixed_model_remains_exact(self):
        self._write_companion_full(routing="fixed", model="gpt-5.6-terra", effort="high",
                                    allowed_models=["gpt-5.6-terra", "gpt-6-astra"])
        result = settings.read_companion(self.root)
        self.assertEqual((result["routing"], result["model"], result["effort"]),
                         ("fixed", "gpt-5.6-terra", "high"))
        self.assertEqual(result["allowed_models"], ["gpt-5.6-terra", "gpt-6-astra"])

    def test_stored_fixed_model_and_effort_stay_exact(self):
        self._write_companion_full(routing="fixed", model="gpt-5.6-luna", effort="medium",
                                    allowed_models=["gpt-5.6-luna"])
        result = settings.read_companion(self.root)
        self.assertEqual((result["model"], result["effort"]), ("gpt-5.6-luna", "medium"))
        self.assertEqual(result["allowed_models"], ["gpt-5.6-luna"])

    def test_migrated_companion_value_writes_back_without_raising(self):
        self._write_companion_full(
            allowed_models=["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra"])
        current = settings.status(self.root)
        self.assertEqual(current["routing"]["allowed_models"],
                         ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra"])
        after = self.update("routing", dict(current["routing"], enabled=True), current)
        self.assertTrue(after["routing"]["enabled"])
        on_disk = json.loads((self.root / ".eutonos-local/companion.json").read_text())
        self.assertEqual(on_disk["allowed_models"],
                         ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra"])

    def test_role_option_preserves_exact_model_and_effort(self):
        self._write_local(roles={"worker": {"options": [
            {"harness": "codex", "model": "gpt-5.6-sol", "effort": "ultra"}],
            "preamble": None, "autonomy": "off"}})
        result = settings.read_roles(self.root)
        self.assertEqual(result["roles"]["worker"]["options"][0],
                         {"harness": "codex", "model": "gpt-5.6-sol", "effort": "ultra"})

    def test_role_option_preserves_terra_pin(self):
        self._write_local(roles={"worker": {"options": [
            {"harness": "codex", "model": "gpt-5.6-terra", "effort": "high"}],
            "preamble": None, "autonomy": "off"}})
        result = settings.read_roles(self.root)
        option = result["roles"]["worker"]["options"][0]
        self.assertEqual(option["harness"], "codex")
        self.assertEqual(option["effort"], "high")
        self.assertEqual(option["model"], "gpt-5.6-terra")
        self.assertEqual(settings.MODEL_HARNESS[option["model"]], "codex")

    # Owner policy floor (2026-09-29): Luna's minimum allowed effort is xhigh,
    # enforced only on a fresh WRITE (see settings.LUNA_MIN_EFFORT and the
    # gating comment above validate_routing); a READ of settings saved before
    # this rule existed must keep loading a stored lower Luna effort exactly,
    # never raise and never rewrite the file (owner correction, 2026-09-29:
    # resolving an omitted effort to xhigh is Codex's own dispatch resolver's
    # job, not this module's, so it is not exercised here).
    def test_old_saved_luna_high_role_option_still_reads(self):
        self._write_local(roles={"worker": {"options": [
            {"harness": "codex", "model": "gpt-6-luna", "effort": "high"}],
            "preamble": None, "autonomy": "off"}})
        before = (self.root / ".eutonos-local" / "settings.json").read_bytes()
        result = settings.read_roles(self.root)
        self.assertEqual(result["roles"]["worker"]["options"][0],
                         {"harness": "codex", "model": "gpt-6-luna", "effort": "high"})
        self.assertEqual((self.root / ".eutonos-local" / "settings.json").read_bytes(), before)

    def test_old_saved_luna_high_fixed_routing_still_reads(self):
        self._write_companion_full(routing="fixed", model="gpt-6-luna", effort="high",
                                    allowed_models=["gpt-6-luna"])
        result = settings.read_companion(self.root)
        self.assertEqual((result["model"], result["effort"]), ("gpt-6-luna", "high"))

    def test_saving_a_luna_role_option_below_xhigh_is_rejected(self):
        rev = settings.read_roles(self.root)["revision"]
        for effort in ("low", "medium", "high"):
            with self.assertRaises(ValueError):
                settings.update_roles(self.root, {"worker": {"options": [
                    {"harness": "codex", "model": "gpt-6-luna", "effort": effort}],
                    "preamble": None, "autonomy": "off"}}, expected_revision=rev)

    def test_saving_a_luna_fixed_routing_effort_below_xhigh_is_rejected(self):
        current = settings.status(self.root)
        for effort in ("low", "medium", "high"):
            routing = {"enabled": True, "routing": "fixed", "model": "gpt-6-luna", "effort": effort,
                      "allowed_models": ["gpt-6-luna"]}
            with self.assertRaises(ValueError):
                self.update("routing", routing, current)

    def test_saving_a_luna_role_option_at_or_above_xhigh_is_accepted(self):
        rev = settings.read_roles(self.root)["revision"]
        for effort in ("xhigh", "max"):
            saved = settings.update_roles(self.root, {"worker": {"options": [
                {"harness": "codex", "model": "gpt-6-luna", "effort": effort}],
                "preamble": None, "autonomy": "off"}}, expected_revision=rev)
            self.assertEqual(saved["roles"]["worker"]["options"][0]["effort"], effort)
            rev = saved["revision"]

    def test_saving_a_non_luna_role_option_is_unaffected_by_the_luna_floor(self):
        rev = settings.read_roles(self.root)["revision"]
        for model, effort in (("gpt-6-sol", "high"), ("gpt-6-astra", "high"), ("claude-sonnet-5", "high")):
            harness = settings.MODEL_HARNESS[model]
            saved = settings.update_roles(self.root, {"worker": {"options": [
                {"harness": harness, "model": model, "effort": effort}],
                "preamble": None, "autonomy": "off"}}, expected_revision=rev)
            self.assertEqual(saved["roles"]["worker"]["options"][0]["effort"], effort)
            rev = saved["revision"]

    def test_stale_revision_never_mutates(self):
        old = settings.status(self.root)
        current = self.update("ram", {"retrieval": "off", "capture": True}, old)
        local_path = self.root / ".eutonos-local/settings.json"
        before = local_path.read_bytes()
        with self.assertRaises(settings.StaleSettings) as caught:
            settings.update(self.root, 0, old["revision"], "ram",
                            {"retrieval": "on-demand", "capture": False})
        self.assertEqual(caught.exception.current["revision"], current["revision"])
        self.assertEqual(local_path.read_bytes(), before)

    def test_same_process_concurrent_posts_serialize_revision_check_and_write(self):
        revision = settings.status(self.root)["revision"]
        values = ({"retrieval": "off", "capture": True},
                  {"retrieval": "on-demand", "capture": False})
        def save(value):
            try:
                return settings.update(self.root, 0, revision, "ram", value)["ram"]
            except settings.StaleSettings:
                return "stale"
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(save, values))
        self.assertEqual(outcomes.count("stale"), 1)
        selected = settings.status(self.root)["ram"]
        self.assertIn({key: selected[key] for key in ("retrieval", "capture")}, values)

    def test_separate_process_same_revision_writers_yield_one_stale_result(self):
        revision = settings.status(self.root)["revision"]
        source = Path(__file__).resolve().parents[1] / "src"
        program = (
            "import json, sys; sys.path.insert(0, sys.argv[1]); from eutonos import settings; "
            "root, revision, value = sys.argv[2:]; "
            "\ntry:\n settings.update(root, 0, revision, 'ram', json.loads(value)); print('saved')"
            "\nexcept settings.StaleSettings:\n print('stale')"
        )
        values = (json.dumps({"retrieval": "off", "capture": True}),
                  json.dumps({"retrieval": "on-demand", "capture": False}))
        commands = [[sys.executable, "-c", program, str(source), str(self.root), revision, value]
                    for value in values]
        processes = [subprocess.Popen(command, stdout=subprocess.PIPE, text=True) for command in commands]
        outputs = [process.communicate(timeout=10) for process in processes]
        self.assertEqual([process.returncode for process in processes], [0, 0])
        self.assertEqual(sorted(output.strip() for output, _ in outputs), ["saved", "stale"])

    def test_crashed_writer_releases_os_lock_without_manual_cleanup(self):
        source = Path(__file__).resolve().parents[1] / "src"
        marker = self.root / "entered"
        program = (
            "import os, sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
            "from eutonos.local_lock import local_lock; "
            "\nwith local_lock(sys.argv[2]):\n Path(sys.argv[3]).write_text('entered'); os._exit(0)"
        )
        child = subprocess.run([sys.executable, "-c", program, str(source), str(self.root), str(marker)],
                               capture_output=True, text=True, timeout=10)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertTrue(marker.exists())
        current = settings.status(self.root)
        saved = settings.update(self.root, 0, current["revision"], "ram", {"retrieval": "off", "capture": True})
        self.assertEqual(saved["ram"]["retrieval"], "off")

    @unittest.skipUnless(os.name == "nt", "Windows locking behavior")
    def test_windows_lock_does_not_retry_a_noncontention_error(self):
        import msvcrt
        path = self.root / "invalid-handle.lock"
        path.write_bytes(b"0")
        with path.open("r+b") as handle, mock.patch.object(
                msvcrt, "locking", side_effect=OSError(errno.EBADF, "bad file handle")):
            with self.assertRaisesRegex(OSError, "bad file handle"):
                local_lock._acquire_file(handle)

    def test_local_lock_timeout_raises_instead_of_blocking(self):
        """Finding 4 (Codex verification, 2026-09-28): a caller that cannot
        afford to block (role_hooks._hooks_config) passes a short timeout;
        once another holder of the OS-level lock is confirmed (simulated
        here directly on the file handle, bypassing this process's own
        in-process thread lock, exactly like a genuinely separate process
        would), local_lock(..., timeout=...) raises TimeoutError promptly
        rather than waiting indefinitely. timeout=None (the default, every
        other existing caller) is unaffected -- it still blocks."""
        lock_path = self.root / ".eutonos-local" / ".locks" / "metadata.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        local_lock._acquire_file(handle)
        try:
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                with local_lock.local_lock(self.root, timeout=0.05):
                    pass
            self.assertLess(time.monotonic() - started, 2.0)
        finally:
            local_lock._release_file(handle)
            handle.close()

    def test_local_lock_timeout_raises_when_another_thread_holds_it(self):
        """Finding 4 bug fix (coordinator recheck, 2026-09-28): the original
        implementation entered the in-process threading.RLock with a plain
        ``with thread_lock:`` -- an unbounded, un-timed-out block -- and only
        passed ``timeout`` on to the OS-level file lock afterwards. A caller
        with a genuinely short budget could still block indefinitely on a
        DIFFERENT THREAD in the SAME PROCESS holding this lock (not just a
        separate process holding the OS-level lock, which the test above
        covers) -- defeating the whole point of the parameter. ONE deadline
        must now cover both the thread lock and the OS-level lock."""
        held = Event()
        release = Event()

        def hold():
            with local_lock.local_lock(self.root):
                held.set()
                release.wait(timeout=5)

        thread = Thread(target=hold)
        thread.start()
        try:
            self.assertTrue(held.wait(timeout=5))
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                with local_lock.local_lock(self.root, timeout=0.05):
                    pass
            self.assertLess(time.monotonic() - started, 2.0)
        finally:
            release.set()
            thread.join(timeout=5)

    def test_read_hooks_timeout_propagates_to_caller(self):
        lock_path = settings._resolve_shared_root(self.root) / ".eutonos-local" / ".locks" / "metadata.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        local_lock._acquire_file(handle)
        try:
            with self.assertRaises(TimeoutError):
                settings.read_hooks(self.root, timeout=0.05)
        finally:
            local_lock._release_file(handle)
            handle.close()

    def test_read_hooks_bounded_returns_none_for_an_oversized_settings_file(self):
        """Finding 5 (coordinator recheck, 2026-09-28): settings.read_hooks_bounded
        is role_hooks.py's own hot-path read -- unlike read_hooks/_local, which
        read the WHOLE settings.json unconditionally, this caps the read
        itself and treats anything past the cap as an ordinary read error
        (None), never falling through to parse the rest of an oversized file."""
        local_path = settings._resolve_shared_root(self.root) / ".eutonos-local" / "settings.json"
        local_path.parent.mkdir(parents=True, exist_ok=True)
        padded = json.dumps({"profile": settings.PROFILE,
                             "padding": "x" * (settings.HOOKS_BOUNDED_READ_MAX_BYTES + 1000)})
        self.assertGreater(len(padded.encode("utf-8")), settings.HOOKS_BOUNDED_READ_MAX_BYTES)
        local_path.write_text(padded, encoding="utf-8")
        self.assertIsNone(settings.read_hooks_bounded(self.root))

    def test_read_hooks_bounded_matches_read_hooks_for_an_ordinary_file(self):
        """A normal-sized settings.json (missing entirely, or a real saved
        one) reads the same effective ``hooks`` value through the bounded
        path as through the ordinary one -- the cap only ever changes
        behavior for a file that is actually oversized."""
        self.assertEqual(settings.read_hooks_bounded(self.root), settings.read_hooks(self.root)["hooks"])
        rev = settings.read_hooks(self.root)["revision"]
        current = settings.read_hooks(self.root)["hooks"]
        settings.update_hooks(self.root, {**current, "long_turn_threshold": 7}, expected_revision=rev)
        self.assertEqual(settings.read_hooks_bounded(self.root)["long_turn_threshold"], 7)
        self.assertEqual(settings.read_hooks_bounded(self.root), settings.read_hooks(self.root)["hooks"])

    def test_invalid_sections_and_effort_are_rejected_without_files(self):
        revision = settings.status(self.root)["revision"]
        invalid = [
            ("ram", {"retrieval": "always", "capture": True}),
            ("tracking", {"enabled": 1}),
            ("task_rules", {task: {"model": "inherit", "effort": "global"}
                            for task in settings.TASKS if task != "git"}),
            ("routing", {"enabled": True, "routing": "fixed", "model": "gpt-6-luna",
                         "effort": "ultra", "allowed_models": ["gpt-6-luna"]}),
        ]
        for section, value in invalid:
            with self.subTest(section=section):
                with self.assertRaises(ValueError):
                    settings.update(self.root, 0, revision, section, value)
        self.assertFalse((self.root / ".eutonos-local").exists())

    def test_route_honors_off_task_rules_eligibility_and_approval(self):
        off = settings_tool.route(self.root, "docs", "gpt-6-astra")
        self.assertEqual((off["outcome"], off["basis"]), ("continue-primary", "companion-off"))
        current = settings.status(self.root)
        routing = {"enabled": True, "routing": "adaptive", "model": None, "effort": "high",
                   "allowed_models": list(settings.MODELS)}
        current = self.update("routing", routing, current)
        # A custom role naming gpt-6-astra directly, so once gpt-6-astra is
        # excluded from allowed_models below the closeout assertion exercises
        # "task-role-model-not-eligible".
        roles_before = settings.read_roles(self.root)
        settings.update_roles(self.root, dict(roles_before["roles"], **{
            "astra-worker": {"options": [{"harness": "codex", "model": "gpt-6-astra", "effort": None}],
                             "preamble": None, "autonomy": "ask"}}),
            expected_revision=roles_before["revision"])
        rules = dict(current["task_rules"], docs="orchestrator", git="worker-hard", review="astra-worker",
                    discovery="discovery")
        current = self.update("task_rules", rules, current)
        # The family choices remain eligible; the explicit Astra pin does not.
        narrowed = dict(current["routing"], allowed_models=["luna", "sol"])
        self.update("routing", narrowed, current)
        self.assertEqual(settings_tool.route(self.root, "docs", "gpt-6-astra")["basis"], "task-rule-orchestrator")
        self.assertEqual(settings_tool.route(self.root, "git", "gpt-6-luna")["outcome"], "approval-required")
        approved = settings_tool.route(self.root, "git", "gpt-6-luna", "approved")
        self.assertEqual((approved["model"], approved["effort"], approved["role"]),
                         ("gpt-6.1-sol", "high", "worker-hard"))
        self.assertEqual(settings_tool.route(self.root, "review", "gpt-6-astra")["basis"],
                         "task-role-model-not-eligible")
        # The Luna family is lower in the explicit policy rank than Astra.
        disc = settings_tool.route(self.root, "discovery", "gpt-6-astra")
        self.assertEqual((disc["outcome"], disc["model"], disc["effort"]),
                         ("selected", "gpt-6-luna", "xhigh"))

    def test_role_task_rule_ignores_global_fixed_model_and_uses_its_own_effort(self):
        """A role always resolves to its own concrete model/effort (AWR worker
        profiles); unlike the retired "inherit" task rule, it never falls back
        to or inherits the global fixed routing model/effort."""
        routing = {"enabled": True, "routing": "fixed", "model": "gpt-6-astra",
                   "effort": "high", "allowed_models": list(settings.MODELS)}
        current = self.update("routing", routing)
        rules = dict(current["task_rules"], discovery="worker-routine")
        self.update("task_rules", rules, current)
        # worker-routine's first option is codex/gpt-6-luna/xhigh (index 1), an
        # upgrade over gpt-6-astra (index 0), so it still needs approval like
        # any other candidate -- the global fixed model/effort above is unused.
        result = settings_tool.route(self.root, "discovery", "gpt-6-astra", "approved")
        self.assertEqual((result["model"], result["effort"]), ("gpt-6-luna", "xhigh"))

    def test_task_role_naming_an_unknown_role_is_diagnostic(self):
        """A fresh task_rules write naming an unknown role is now rejected up
        front (see test_task_rules_write_rejects_unknown_role_name below,
        coordinator review 2026-09-28). But a role can still be deleted out
        from under an EXISTING task_rules mapping afterwards -- update_roles
        has no reason to know about task_rules -- so a stale on-disk mapping
        (simulated here by writing settings.json directly, bypassing
        settings.update's validation) still needs its own diagnostic at
        route-decision time rather than raising out of a read."""
        self._write_local(task_rules=dict(dict.fromkeys(settings.TASKS, "orchestrator"),
                                          discovery="no-such-role"))
        self.update("routing", {"enabled": True, "routing": "profile", "model": None,
                                "effort": None, "allowed_models": list(settings.MODELS)})
        result = settings_tool.route(self.root, "discovery", "gpt-6-astra")
        self.assertEqual((result["outcome"], result["basis"], result["role"]),
                         ("invalid-settings", "task-role-unresolvable", "no-such-role"))
        self.assertIn("no-such-role", result["message"])

    def test_task_rules_write_rejects_unknown_role_name(self):
        """Requirement 3 (coordinator review, 2026-09-28): settings.update's
        task_rules branch cross-checks every non-"orchestrator" value against
        the CURRENT roles section (after the DEFAULT_ROLES merge) and rejects
        a name that does not exist there, with a clear, diagnosable error."""
        current = settings.status(self.root)
        rules = dict(current["task_rules"], discovery="no-such-role")
        with self.assertRaisesRegex(ValueError, "no-such-role"):
            self.update("task_rules", rules, current)
        # A default role name (present via the DEFAULT_ROLES merge, without
        # ever having been explicitly saved) and the "orchestrator" sentinel
        # both still pass.
        rules = dict(current["task_rules"], discovery="worker-routine", docs="orchestrator")
        saved = self.update("task_rules", rules, current)
        self.assertEqual(saved["task_rules"]["discovery"], "worker-routine")
        self.assertEqual(saved["task_rules"]["docs"], "orchestrator")

    def test_partial_ram_update_preserves_existing_capture_policy(self):
        current = self.update("ram", {"cadence": "checkpoint", "detail": "detailed"})
        saved = self.update("ram", {"capture": False}, current)
        self.assertEqual(saved["ram"], {"retrieval": "on-demand", "capture": False,
                                        "cadence": "checkpoint", "detail": "detailed"})

    def test_task_role_wait_timeout_falls_back_to_primary_and_effort_fields_are_explicit(self):
        routing = {"enabled": True, "routing": "profile", "model": None,
                   "effort": None, "allowed_models": list(settings.MODELS)}
        current = self.update("routing", routing)
        rules = dict(current["task_rules"], git="worker-hard")
        self.update("task_rules", rules, current)
        # worker-hard resolves to the latest Sol/high, an upgrade over Luna,
        # so it needs approval; waiting under 60s stays
        # pending, and 60s+ falls back to the primary without ever selecting it.
        pending = settings_tool.route(self.root, "git", "gpt-6-luna", waited_seconds=59)
        self.assertEqual((pending["outcome"], pending["wait_seconds_remaining"], pending["effort"],
                          pending["proposed_effort"]), ("approval-required", 1, None, "high"))
        fallback = settings_tool.route(self.root, "git", "gpt-6-luna", waited_seconds=60)
        self.assertEqual((fallback["outcome"], fallback["model"], fallback["effort"],
                          fallback["proposed_effort"]), ("continue-primary", "gpt-6-luna", None, "high"))

    def test_claude_models_are_present_with_harness_and_existing_gpt_consumers_still_work(self):
        catalog = settings.MODEL_CATALOG["models"]
        for model in ("claude-opus-5-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"):
            self.assertEqual(catalog[model]["harness"], "claude")
            self.assertEqual(catalog[model]["efforts"], ("low", "medium", "high"))
            self.assertEqual(settings.MODEL_EFFORTS[model], ("low", "medium", "high"))
            self.assertIn(model, settings.MODELS)
        for model in ("gpt-6-astra", "gpt-6-luna", "gpt-6-sol"):
            self.assertEqual(catalog[model]["harness"], "codex")
        # existing GPT-only consumers of MODEL_EFFORTS/basis/MODELS keep working unmodified
        # (native capability; see supported_efforts vs. the owner-allowed "efforts" floor)
        self.assertIn("medium", catalog["gpt-6-luna"]["supported_efforts"])
        self.assertIn(catalog["gpt-6-luna"]["basis"],
                      ("codex-native-cache", "dated-fallback-2026-10-14"))
        routing = {"enabled": True, "routing": "fixed", "model": "gpt-6-luna",
                   "effort": "xhigh", "allowed_models": list(settings.MODELS)}
        self.assertIsNone(settings.validate_routing(routing))

    def test_roles_cas_round_trip_and_stale_rejection(self):
        before = settings.read_roles(self.root)
        # Roles is never truly empty: DEFAULT_ROLES backfills any missing name
        # (AWR worker profiles), so an untouched project already reports the
        # default catalog rather than {}.
        self.assertEqual(set(before["roles"]), set(settings.DEFAULT_ROLES))
        roles = {
            "worker": {"options": [{"harness": "claude", "model": "claude-sonnet-5", "effort": "medium"}],
                       "preamble": None, "autonomy": "off"},
            "director": {"options": [{"harness": "codex", "model": "gpt-6-luna", "effort": "xhigh"},
                                     {"harness": "claude", "model": "claude-opus-5-5", "effort": "high"}],
                        "preamble": "docs/PRINCIPLES.md", "autonomy": "ask"},
        }
        after = settings.update_roles(self.root, roles, expected_revision=before["revision"])
        # "director" is a default-catalog name, but the caller's own value for
        # it is kept, never overwritten; the other default names are
        # backfilled around it (new optional keys default in, unset).
        expected_worker = {"options": roles["worker"]["options"], "preamble": None, "autonomy": "off",
                           "description": None, "edits": "ask", "brief_template": None}
        expected_director = {"options": roles["director"]["options"], "preamble": "docs/PRINCIPLES.md",
                             "autonomy": "ask", "description": None, "edits": "ask",
                             "brief_template": None}
        self.assertEqual(after["roles"]["worker"], expected_worker)
        self.assertEqual(after["roles"]["director"], expected_director)
        self.assertEqual(set(after["roles"]), {"worker"} | set(settings.DEFAULT_ROLES))
        self.assertNotEqual(after["revision"], before["revision"])
        reread = settings.read_roles(self.root)
        self.assertEqual(reread, after)
        with self.assertRaises(settings.StaleSettings) as caught:
            settings.update_roles(self.root, {}, expected_revision=before["revision"])
        self.assertEqual(caught.exception.current["revision"], after["revision"])
        self.assertEqual(settings.read_roles(self.root)["roles"], after["roles"])  # never mutated on stale

    def test_discovery_default_role_has_the_discovery_brief_template(self):
        """Owner brief (2026-09-29): unlike the other worker roles, "discovery"
        previously had no default brief_template at all; it now defaults to
        settings._DISCOVERY_BRIEF_TEMPLATE (guidance prose, not the worker
        fill-in skeleton) -- but only for a project that has never saved its
        own discovery brief_template (_merge_default_roles never overwrites a
        user's saved value)."""
        self.assertEqual(settings.DEFAULT_ROLES["discovery"]["brief_template"],
                         settings._DISCOVERY_BRIEF_TEMPLATE)
        default = settings.read_roles(self.root)["roles"]["discovery"]
        self.assertEqual(default["brief_template"], settings._DISCOVERY_BRIEF_TEMPLATE)

    def test_saved_discovery_brief_template_is_kept_as_is(self):
        rev = settings.read_roles(self.root)["revision"]
        custom_template = "Custom discovery brief: just check X and report back."
        saved = settings.update_roles(self.root, {"discovery": {
            "options": [{"harness": "claude", "model": "claude-sonnet-5", "effort": "medium"}],
            "preamble": None, "autonomy": "ask", "brief_template": custom_template}},
            expected_revision=rev)
        self.assertEqual(saved["roles"]["discovery"]["brief_template"], custom_template)
        reread = settings.read_roles(self.root)
        self.assertEqual(reread["roles"]["discovery"]["brief_template"], custom_template)

    def test_default_brief_template_known_and_unknown_names(self):
        """default_brief_template is the single lookup of a DEFAULT_ROLES name's
        built-in template; an unknown name (or a name with no built-in template,
        like "reviewer") returns None rather than raising."""
        self.assertEqual(settings.default_brief_template("worker-routine"),
                         settings._WORKER_BRIEF_TEMPLATE)
        self.assertEqual(settings.default_brief_template("discovery"),
                         settings._DISCOVERY_BRIEF_TEMPLATE)
        self.assertIsNone(settings.default_brief_template("reviewer"))
        self.assertIsNone(settings.default_brief_template("not-a-real-role"))

    def test_effective_brief_template_empty_or_missing_saved_falls_back_to_default(self):
        """Owner decision (2026-09-29): a saved role's empty/missing brief_template
        must resolve to the role's built-in default at read/resolve time -- the
        bug this brief fixes (all seven roles were saved earlier with empty
        templates, so the new defaults never reached a live launch)."""
        self.assertEqual(settings.effective_brief_template("worker-hard", {"brief_template": ""}),
                         settings._WORKER_BRIEF_TEMPLATE)
        self.assertEqual(settings.effective_brief_template("worker-hard", {"brief_template": None}),
                         settings._WORKER_BRIEF_TEMPLATE)
        self.assertEqual(settings.effective_brief_template("worker-hard", {}),
                         settings._WORKER_BRIEF_TEMPLATE)
        # A non-empty saved template always wins unchanged.
        self.assertEqual(settings.effective_brief_template("worker-hard", {"brief_template": "MINE"}),
                         "MINE")
        # A role name outside DEFAULT_ROLES with no saved template stays empty.
        self.assertIsNone(settings.effective_brief_template("reviewer", {"brief_template": ""}))
        self.assertIsNone(settings.effective_brief_template("custom-role", {"brief_template": None}))

    def test_deleting_a_default_role_sticks_across_reread_and_rewrite(self):
        """Requirement 2 (coordinator review, 2026-09-28): the settings-level
        role_defaults_seeded sibling key marks every DEFAULT_ROLES name as
        already offered once the FIRST write happens, so a deliberate
        deletion of a default role afterwards is not silently undone by the
        next read's (or write's) default-merge."""
        before = settings.read_roles(self.root)
        self.assertIn("director", before["roles"])
        # First write: doesn't mention "director" at all, but nothing has been
        # seeded yet (a brand-new project), so it is still backfilled around
        # the caller's own role, same as before this key existed.
        first = settings.update_roles(self.root, {"reviewer": before["roles"]["reviewer"]},
                                      expected_revision=before["revision"])
        self.assertIn("director", first["roles"])
        # Second write: explicitly omits "director" -- a deletion. Every
        # DEFAULT_ROLES name is now seeded (recorded by the first write
        # above), so "director" must NOT be backfilled back in this time.
        without_director = {name: role for name, role in first["roles"].items() if name != "director"}
        after = settings.update_roles(self.root, without_director, expected_revision=first["revision"])
        self.assertNotIn("director", after["roles"])
        # A plain re-read must agree -- the deletion sticks, it is not just an
        # artifact of this one update_roles response -- and its own revision
        # must match what the write just returned (read/write consistency).
        reread = settings.read_roles(self.root)
        self.assertNotIn("director", reread["roles"])
        self.assertEqual(reread["revision"], after["revision"])
        # A further write that still omits "director" keeps it deleted too.
        again = settings.update_roles(self.root, reread["roles"], expected_revision=reread["revision"])
        self.assertNotIn("director", again["roles"])

    def test_role_name_pattern_is_enforced_on_write(self):
        """Requirement 3 (coordinator review, 2026-09-28): a role name must
        match ROLE_NAME_RE -- the same pattern role_router checks for the
        "role" request field -- so a savable role can always be sent
        cross-app. A write with a non-conforming name is rejected outright."""
        rev = settings.read_roles(self.root)["revision"]
        with self.assertRaises(ValueError):
            settings.update_roles(self.root, {"bad name!": {
                "options": [{"harness": "claude", "model": "claude-sonnet-5", "effort": None}],
                "preamble": None, "autonomy": "off"}}, expected_revision=rev)

    def test_roles_schema_rejections(self):
        rev = settings.read_roles(self.root)["revision"]

        def bad(roles):
            with self.assertRaises(ValueError):
                settings.update_roles(self.root, roles, expected_revision=rev)

        bad({"w": {"options": [], "preamble": None, "autonomy": "off"}})  # 0 options
        bad({"w": {"options": [{"harness": "claude", "model": "claude-sonnet-5", "effort": "medium"}] * 5,
                   "preamble": None, "autonomy": "off"}})  # 5 options (MAX_ROLE_OPTIONS is 4)
        bad({"w": {"options": [{"harness": "claude", "model": "gpt-6-luna", "effort": None}],
                   "preamble": None, "autonomy": "off"}})  # wrong catalog for harness
        bad({"w": {"options": [{"harness": "codex", "model": "claude-sonnet-5", "effort": None}],
                   "preamble": None, "autonomy": "off"}})  # wrong catalog for harness
        bad({"w": {"options": [{"harness": "claude", "model": "claude-sonnet-5", "effort": "xhigh"}],
                   "preamble": None, "autonomy": "off"}})  # effort unsupported by that model
        bad({"w": {"options": [{"harness": "mars", "model": "x", "effort": None}],
                   "preamble": None, "autonomy": "off"}})  # invalid harness
        bad({"w": {"options": [{"harness": "claude", "model": "claude-sonnet-5", "effort": None}],
                   "preamble": None, "autonomy": "sometimes"}})  # invalid autonomy
        bad({"w": {"options": [{"harness": "claude", "model": "claude-sonnet-5", "effort": None}],
                   "preamble": None, "autonomy": "off", "extra": 1}})  # unknown key
        bad({"w": {"options": [{"harness": "claude", "model": "claude-sonnet-5", "effort": None,
                                "extra": 1}], "preamble": None, "autonomy": "off"}})  # unknown option key
        # hermes: harness itself must be schema-valid; no catalog exists yet so it is skipped
        ok = {"h": {"options": [{"harness": "hermes", "model": "anything-goes", "effort": None}],
                    "preamble": None, "autonomy": "off"}}
        saved = settings.update_roles(self.root, ok, expected_revision=rev)
        self.assertEqual(saved["roles"]["h"]["options"], ok["h"]["options"])
        self.assertEqual(set(saved["roles"]), {"h"} | set(settings.DEFAULT_ROLES))

    def test_roles_accept_four_options_and_primary_secondary_aliases(self):
        """AWR-CT (2026-09-28): MAX_ROLE_OPTIONS went from 2 to 4; a role may now
        have up to 4 ordered options, and old-shape callers using "primary"/
        "secondary" (see role_router.resolve_role) keep working unmodified."""
        self.assertEqual(settings.MAX_ROLE_OPTIONS, 4)
        rev = settings.read_roles(self.root)["revision"]
        roles = {"worker": {"options": [
            {"harness": "claude", "model": "claude-sonnet-5", "effort": "low"},
            {"harness": "claude", "model": "claude-sonnet-5", "effort": "medium"},
            {"harness": "codex", "model": "gpt-6-luna", "effort": "xhigh"},  # Luna's minimum (owner, 2026-09-29)
            {"harness": "codex", "model": "gpt-6-sol", "effort": "high"},
        ], "preamble": None, "autonomy": "off"}}
        saved = settings.update_roles(self.root, roles, expected_revision=rev)
        self.assertEqual(saved["roles"]["worker"]["options"], roles["worker"]["options"])
        with self.assertRaises(ValueError):
            settings.update_roles(self.root, {"w": {"options": [
                {"harness": "claude", "model": "claude-sonnet-5", "effort": None}] * 5,
                "preamble": None, "autonomy": "off"}}, expected_revision=saved["revision"])

    def test_role_preamble_length_is_bounded(self):
        rev = settings.read_roles(self.root)["revision"]
        too_long = {"w": {"options": [{"harness": "claude", "model": "claude-sonnet-5", "effort": None}],
                          "preamble": "x" * (settings.MAX_ROLE_PREAMBLE_CHARS + 1), "autonomy": "off"}}
        with self.assertRaises(ValueError):
            settings.update_roles(self.root, too_long, expected_revision=rev)
        at_limit = {"w": {"options": [{"harness": "claude", "model": "claude-sonnet-5", "effort": None}],
                          "preamble": "x" * settings.MAX_ROLE_PREAMBLE_CHARS, "autonomy": "off"}}
        saved = settings.update_roles(self.root, at_limit, expected_revision=rev)
        self.assertEqual(len(saved["roles"]["w"]["preamble"]), settings.MAX_ROLE_PREAMBLE_CHARS)


class WorktreeSettingsRootTests(unittest.TestCase):
    """AWR7 (2026-09-24): the AWR2 worktree settings-root exception. A linked
    git worktree must share ONE settings/companion state with its main
    checkout instead of silently defaulting to "routing off" because its own
    directory has no `.eutonos-local` of its own -- the exact AWR2 audit
    finding. Real `git init`/`git worktree add` subprocess calls, matching
    this repository's established pattern for tests that need real git
    plumbing."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eutonos-settings-worktree-")
        self.addCleanup(self.temp.cleanup)
        self.main = Path(self.temp.name) / "main-repo"
        self.main.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "Test")
        (self.main / "a.txt").write_text("hi", encoding="utf-8", newline="\n")
        self._git("add", "a.txt")
        self._git("commit", "-q", "-m", "init")
        self.linked = Path(self.temp.name) / "linked-wt"
        self._git("worktree", "add", "-q", str(self.linked), "-b", "linked-branch")

    def _git(self, *args):
        result = subprocess.run(["git", *args], cwd=str(self.main), capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_linked_worktree_resolves_to_the_same_shared_root_as_main(self):
        self.assertEqual(settings._resolve_shared_root(self.linked),
                          settings._resolve_shared_root(self.main))
        self.assertEqual(settings._resolve_shared_root(self.main), Path(self.main).resolve())

    def test_a_write_from_main_is_visible_from_the_linked_worktree(self):
        current = settings.status(self.main)
        self.assertFalse(current["routing"]["enabled"])
        settings.update(self.main, 0, current["revision"], "routing",
                         {**current["routing"], "enabled": True})
        seen_from_linked = settings.status(self.linked)
        self.assertTrue(seen_from_linked["routing"]["enabled"])

    def test_a_write_from_the_linked_worktree_is_visible_from_main(self):
        current = settings.status(self.linked)
        settings.update(self.linked, 0, current["revision"], "tracking", {"enabled": False})
        seen_from_main = settings.status(self.main)
        self.assertFalse(seen_from_main["tracking"]["enabled"])

    def test_settings_file_lands_under_main_not_under_the_linked_worktree(self):
        current = settings.status(self.linked)
        settings.update(self.linked, 0, current["revision"], "routing",
                         {**current["routing"], "enabled": True})
        self.assertTrue((self.main / ".eutonos-local" / "companion.json").exists())
        self.assertFalse((self.linked / ".eutonos-local").exists())

    def test_roles_section_is_also_shared_across_the_worktree(self):
        rev = settings.read_roles(self.main)["revision"]
        role = {"w": {"options": [{"harness": "claude", "model": "claude-sonnet-5", "effort": None}],
                      "preamble": None, "autonomy": "off"}}
        settings.update_roles(self.main, role, expected_revision=rev)
        self.assertEqual(settings.read_roles(self.linked)["roles"]["w"]["options"], role["w"]["options"])

    def test_a_plain_non_git_directory_is_unaffected(self):
        other = Path(tempfile.mkdtemp(prefix="eutonos-settings-nongit-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(other, ignore_errors=True))
        self.assertEqual(settings._resolve_shared_root(other), other.resolve())
        value = settings.status(other)
        self.assertFalse(value["routing"]["enabled"])
        self.assertFalse((other / ".eutonos-local").exists())


class SettingsHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eutonos-settings-http-")
        self.addCleanup(self.temp.cleanup)
        environment = mock.patch.dict(os.environ, {"EUTONOS_HOME": str(Path(self.temp.name) / "eutonos-home")})
        environment.start()
        self.addCleanup(environment.stop)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app([self.root]))
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._close)
        status, snapshot = self.request("GET", "/api/v1/snapshot", {"Host": self.host})
        self.assertEqual(status, 200)
        self.token = snapshot["settings_token"]

    @property
    def host(self):
        return f"127.0.0.1:{self.server.server_port}"

    def _close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)

    def request(self, method, path, headers=None, payload=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            body = json.dumps(payload).encode() if payload is not None else None
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def headers(self, **changes):
        value = {"Host": self.host, "Origin": "http://" + self.host,
                 "X-Eutonos-Settings": self.token, "Content-Type": "application/json"}
        value.update(changes)
        return value

    def test_delivery_settings_use_engine_revision_and_reads_do_not_activate(self):
        from eutonos.git_delivery import CONFIG
        environment = {**os.environ, 'GIT_CONFIG_GLOBAL':os.devnull,'GIT_CONFIG_NOSYSTEM':'1'}
        def git(*args):
            return subprocess.run(['git','-C',str(self.root),*args],env=environment,check=True,capture_output=True).stdout.decode().strip()
        git('init','-b','main');git('config','user.name','ASW4 Fixture');git('config','user.email','asw4@example.invalid')
        (self.root/'code.py').write_text('value = 1\n');git('add','code.py');git('commit','-m','baseline')
        code, before=self.request('GET','/api/v1/settings?project=0',{'Host':self.host})
        self.assertEqual(before['delivery']['mode'],'manual')
        self.assertFalse((self.root/'.git/eutonos-delivery').exists())
        remote=self.root.parent/'remote.git';git('init','--bare',str(remote))
        config={'profile':CONFIG,'project_id':'fixture','participant':'participant','mode':'commit-only',
            'remote_url':str(remote),'remote_ref':'refs/heads/main','branch':'refs/heads/main',
            'records_ref':'refs/heads/records/asw4','canonical_base':git('rev-parse','HEAD'),
            'allowed_paths':['code.py'],'checks':[[sys.executable,'-c','pass']]}
        payload={'project':0,'revision':0,'section':'delivery','value':config}
        code,saved=self.request('POST','/api/v1/settings',self.headers(),payload)
        self.assertEqual((code,saved['revision'],saved['delivery']['mode']),(200,1,'commit-only'))
        code,read=self.request('GET','/api/v1/settings?project=0',{'Host':self.host})
        self.assertEqual(read['delivery_revision'],1)
        self.assertEqual(read['revision'],before['revision']) # independent CAS owner
        from eutonos.git_delivery_native import inspect
        self.assertEqual(inspect(self.root)['requests'],[])
        code,stale=self.request('POST','/api/v1/settings',self.headers(),payload)
        self.assertEqual((code,stale['outcome']),(409,'stale-settings'))
        self.assertEqual(stale['settings']['revision'],1)

    def test_get_post_and_stale_conflict_return_exact_project(self):
        code, current = self.request("GET", "/api/v1/settings?project=0", {"Host": self.host})
        self.assertEqual((code, current["project"]), (200, 0))
        payload = {"project": 0, "revision": current["revision"], "section": "ram",
                   "value": {"retrieval": "off", "capture": False}}
        code, saved = self.request("POST", "/api/v1/settings", self.headers(), payload)
        self.assertEqual((code, saved["project"], saved["ram"]["retrieval"]), (200, 0, "off"))
        code, conflict = self.request("POST", "/api/v1/settings", self.headers(), payload)
        self.assertEqual((code, conflict["outcome"]), (409, "stale-settings"))
        self.assertEqual(conflict["settings"]["revision"], saved["revision"])

    def test_get_is_bounded_and_post_keeps_existing_local_guard(self):
        for path in ("/api/v1/settings", "/api/v1/settings?project=1", "/api/v1/settings?project=0&x=1"):
            code, body = self.request("GET", path, {"Host": self.host})
            self.assertEqual((code, body["outcome"]), (400, "invalid-settings"))
        current = settings.status(self.root)
        payload = {"project": 0, "revision": current["revision"], "section": "tracking",
                   "value": {"enabled": False}}
        code, body = self.request("POST", "/api/v1/settings", self.headers(Origin="http://example.invalid"), payload)
        self.assertEqual((code, body["outcome"]), (403, "denied"))
        self.assertTrue(settings.status(self.root)["tracking"]["enabled"])

    def test_refused_post_body_is_read_so_the_reply_survives(self):
        # Windows resets a socket closed with unread bytes (WinError 10053): before
        # the server read refused bodies, ~6% of these lost their 403.
        body = {"pad": "x" * 200_000}
        cases = [("/api/v1/settings", self.headers(Origin="http://example.invalid"), 403, "denied"),
                 ("/api/v1/unknown", self.headers(), 404, "not-found"),
                 ("/api/v1/settings", self.headers(), 400, "invalid-settings")]
        for path, headers, status, outcome in cases:
            with self.subTest(path=path, status=status):
                for _ in range(40):
                    self.assertEqual(self.request("POST", path, headers, body), (status, {"outcome": outcome}))

    def test_metrics_get_accepts_compact_ranges_and_rejects_malformed_queries(self):
        code, all_time = self.request("GET", "/api/v1/companion/metrics?project=0", {"Host": self.host})
        self.assertEqual((code, all_time["range"]["label"]), (200, "All time"))
        self.assertIn("credits", all_time["metrics"])
        code, result = self.request("GET", "/api/v1/companion/metrics?project=0&amount=10&unit=minutes",
                                    {"Host": self.host})
        self.assertEqual((code, result["project"], result["range"]["kind"]), (200, 0, "relative"))
        self.assertEqual(result["range"]["basis"], "Completed turns ending in range; whole-turn usage")
        code, result = self.request(
            "GET", "/api/v1/companion/metrics?project=0&from=2026-09-12T10%3A00%3A00Z&to=2026-09-12T11%3A00%3A00Z",
            {"Host": self.host})
        self.assertEqual((code, result["range"]["kind"]), (200, "custom"))
        for query in ("project=0&amount=0&unit=minutes", "project=0&amount=1",
                      "project=0&unit=hours", "project=0&from=2026-09-12T10%3A00%3A00Z",
                      "project=true", "project=1"):
            code, result = self.request("GET", "/api/v1/companion/metrics?" + query, {"Host": self.host})
            self.assertEqual((code, result["outcome"]), (400, "invalid-settings"))


class RoutingRedesignTests(unittest.TestCase):
    """Dashboard routing redesign (2026-10-14): per-job roles, review task, fixed
    closeout, per-harness worker settings and migration of older saved files."""

    JOB_ROLES = ("worker-coding", "worker-docs", "worker-" + "git")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eutonos-routing-redesign-")
        self.addCleanup(self.temp.cleanup)
        environment = mock.patch.dict(os.environ, {"CODEX_HOME": str(Path(self.temp.name) / "codex"),
                                                   "EUTONOS_HOME": str(Path(self.temp.name) / "home")})
        environment.start()
        self.addCleanup(environment.stop)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()

    def update(self, section, value):
        current = settings.status(self.root)
        return settings.update(self.root, 0, current["revision"], section, value)

    def old_seeded(self):
        return [name for name in settings.DEFAULT_ROLES if name not in self.JOB_ROLES]

    def write_local(self, **overrides):
        """A settings file as saved before the redesign: three jobs share worker-routine, no review."""
        path = self.root / ".eutonos-local" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {"profile": settings.PROFILE,
                 "task_rules": {"discovery": "discovery", "coding": "worker-routine", "docs": "worker-routine",
                                "git": "worker-routine", "closeout": "orchestrator"},
                 "ram": {"retrieval": "on-demand", "capture": True, "cadence": "every-turn", "detail": "summary"},
                 "roles": {}, "role_defaults_seeded": self.old_seeded(),
                 "hooks": json.loads(json.dumps(settings.DEFAULT_HOOKS))}
        value.update(overrides)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_defaults_give_each_job_its_own_role_and_review_has_one(self):
        rules = settings.status(self.root)["task_rules"]
        self.assertEqual(set(rules), set(settings.TASKS))
        self.assertEqual(rules["review"], "reviewer")
        roles = settings.read_roles(self.root)["roles"]
        owners = [rules[task] for task in settings.WORKER_TASKS]
        self.assertEqual(len(set(owners)), len(owners))
        self.assertTrue(all(name in roles for name in owners))

    def test_closeout_is_fixed_to_the_main_chat(self):
        with self.assertRaisesRegex(ValueError, "Closeout is fixed"):
            self.update("task_rules", dict(settings.status(self.root)["task_rules"], closeout="worker-hard"))
        # An older file naming a role for closeout is read as the main chat, not rejected.
        self.write_local(task_rules={"discovery": "discovery", "coding": "worker-hard", "docs": "worker-hard",
                                     "git": "worker-hard", "closeout": "worker-hard"})
        self.assertEqual(settings.status(self.root)["task_rules"]["closeout"], "orchestrator")

    def test_older_rules_without_review_load_and_a_write_may_omit_it(self):
        self.write_local()
        self.assertEqual(settings.status(self.root)["task_rules"]["review"], "reviewer")
        old = {task: "orchestrator" for task in settings.TASKS if task != "review"}
        saved = self.update("task_rules", old)
        self.assertEqual(saved["task_rules"]["review"], "reviewer")

    def test_shared_role_is_split_per_job_from_the_saved_role(self):
        custom = json.loads(json.dumps(settings.DEFAULT_ROLES["worker-routine"]))
        custom["preamble"] = "CUSTOM"
        custom["options"][0] = {"harness": "codex", "model": "sol", "effort": "high"}
        self.write_local(roles={"worker-routine": custom})
        state = settings.status(self.root)
        roles = settings.read_roles(self.root)["roles"]
        self.assertEqual([state["task_rules"][task] for task in ("coding", "docs", "git")], list(self.JOB_ROLES))
        for name in self.JOB_ROLES:
            self.assertEqual(roles[name]["preamble"], "CUSTOM")
            self.assertEqual(roles[name]["options"][0], {"harness": "codex", "model": "sol", "effort": "high"})
        # Editing one job's role leaves the others alone.
        edited = json.loads(json.dumps(roles))
        edited["worker-docs"]["options"][0] = {"harness": "claude", "model": "sonnet", "effort": "high"}
        settings.update_roles(self.root, edited, expected_revision=settings.read_roles(self.root)["revision"])
        again = settings.read_roles(self.root)["roles"]
        self.assertEqual(again["worker-docs"]["options"][0]["model"], "sonnet")
        self.assertEqual(again["worker-coding"]["options"][0]["model"], "sol")
        self.assertEqual(again[self.JOB_ROLES[2]]["options"][0]["model"], "sol")

    def test_split_leaves_a_role_alone_when_not_shared(self):
        self.write_local(task_rules={"discovery": "discovery", "coding": "worker-hard", "docs": "worker-routine",
                                     "git": "orchestrator", "review": "reviewer", "closeout": "orchestrator"})
        rules = settings.status(self.root)["task_rules"]
        self.assertEqual((rules["coding"], rules["docs"], rules["git"]),
                         ("worker-hard", "worker-routine", "orchestrator"))

    def test_harness_settings_validate_round_trip_and_derive_the_cap(self):
        state = settings.status(self.root)
        self.assertEqual(state["harnesses"], settings.DEFAULT_HARNESSES)
        before_cap = state["routing"]["max_concurrent_companions"]
        # A settings write that never touched harnesses keeps the stored cap and leaves the key out of the file.
        current = settings.read_roles(self.root)
        settings.update_roles(self.root, current["roles"], expected_revision=current["revision"])
        path = self.root / ".eutonos-local" / "settings.json"
        self.assertNotIn("harnesses", json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(settings.status(self.root)["routing"]["max_concurrent_companions"], before_cap)
        for bad in ({"claude": {"enabled": True, "max_workers": 0}, "codex": {"enabled": True, "max_workers": 4},
                     "hermes": {"enabled": False, "max_workers": 1}},
                    {"claude": {"enabled": True, "max_workers": 8}},
                    {"claude": {"enabled": "yes", "max_workers": 2}},
                    {"unknown": {"enabled": True, "max_workers": 2}}):
            with self.assertRaises(ValueError):
                self.update("harnesses", bad)
        saved = self.update("harnesses", {"claude": {"enabled": True, "max_workers": 2},
                                          "codex": {"enabled": True, "max_workers": 7},
                                          "hermes": {"enabled": False, "max_workers": 1}})
        self.assertEqual(saved["harnesses"]["codex"]["max_workers"], 7)
        self.assertEqual(saved["routing"]["max_concurrent_companions"], 7)  # 2 + 7, capped
        off = self.update("harnesses", {"claude": {"enabled": False, "max_workers": 2},
                                        "codex": {"enabled": True, "max_workers": 3},
                                        "hermes": {"enabled": False, "max_workers": 1}})
        self.assertEqual(off["routing"]["max_concurrent_companions"], 3)
        # Saving explicit defaults is still an explicit choice and persists.
        self.update("harnesses", json.loads(json.dumps(settings.DEFAULT_HARNESSES)))
        self.assertIn("harnesses", json.loads(path.read_text(encoding="utf-8")))

    def enable_routing(self):
        current = settings.status(self.root)
        routing = dict(current["routing"], enabled=True, routing="adaptive", effort="high",
                       allowed_models=list(settings.MODELS))
        settings.update(self.root, 0, current["revision"], "routing", routing)

    def test_route_refuses_a_job_whose_harness_is_off(self):
        self.enable_routing()
        self.update("harnesses", {"claude": {"enabled": True, "max_workers": 2},
                                  "codex": {"enabled": False, "max_workers": 2},
                                  "hermes": {"enabled": False, "max_workers": 1}})
        result = settings_tool.route(self.root, "coding", "claude-opus-5-5", "approved")
        self.assertEqual((result["outcome"], result["basis"]), ("invalid-settings", "task-role-harness-disabled"))

    def test_review_task_routes_to_the_reviewer_role(self):
        self.enable_routing()
        result = settings_tool.route(self.root, "review", "gpt-5.5", "approved")
        self.assertEqual(result["role"], "reviewer")


if __name__ == "__main__":
    unittest.main()
