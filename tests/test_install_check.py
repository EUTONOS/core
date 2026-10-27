"""The install check (tools/install_check.py): its pieces, a full pass on temporary folders, and that it fails when
an uninstall leaves something behind. Every OS and app call is a recording fake; no real home is touched."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("install_check_tool", ROOT / "tools" / "install_check.py")
ic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ic)


class PiecesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()

    def test_the_environment_is_pointed_at_the_temporary_tree_and_restored(self):
        user = self.base / "user"
        before = {key: os.environ.get(key) for key in ic.ENV_KEYS}
        with ic.isolated_environment(self.base, user):
            for key in ("HOME", "USERPROFILE", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "EUTONOS_HOME"):
                self.assertTrue(Path(os.environ[key]).resolve().is_relative_to(self.base), key)
            self.assertNotIn("PYTHONPATH", os.environ)
        self.assertEqual({key: os.environ.get(key) for key in ic.ENV_KEYS}, before)

    def test_the_claude_fake_writes_only_below_the_temporary_user_home(self):
        user, hermes = self.base / "user", self.base / "hermes"
        machine = ic.Machine(self.base, user, hermes)
        machine.claude_run(["claude", "plugin", "marketplace", "add", str(self.base / "m" / "crosstalk")])
        machine.claude_run(["claude", "plugin", "install", "crosstalk@crosstalk"])
        self.assertTrue((user / ".claude" / "plugins" / "known_marketplaces.json").is_file())
        settings = json.loads((user / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["enabledPlugins"], {"crosstalk@crosstalk": True})
        machine.claude_run(["claude", "plugin", "uninstall", "crosstalk@crosstalk"])
        machine.claude_run(["claude", "plugin", "marketplace", "remove", "crosstalk"])
        self.assertFalse((user / ".claude" / "plugins" / "known_marketplaces.json").exists())
        self.assertNotIn("enabledPlugins", json.loads((user / ".claude" / "settings.json").read_text(encoding="utf-8")))

    def test_the_task_fake_answers_owned_from_what_it_holds_and_refuses_a_task_that_is_not_ours(self):
        from eutonos import desktop_companion_runtime as runtime
        machine = ic.Machine(self.base, self.base / "user", self.base / "hermes")
        task = machine.seams()["task"]
        self.assertFalse(task["owned"]())  # no task at all
        task["register"]([r"C:\h\pythonw.exe", "-I", "-m", "eutonos.desktop_companion_runtime", "--ensure-current"])
        self.assertTrue(task["owned"]())  # what register wrote carries EUTONOS's description
        self.assertEqual(machine.task_description, runtime.TASK_DESCRIPTION)
        self.assertTrue(task["delete"]())
        self.assertEqual((machine.task, machine.task_description), (None, None))
        for seeded in ((r"C:\Tools\backup.exe", "--nightly"), ic.OLDER_COMPANION_TASK):
            machine.seed_task(seeded, "My own task")  # another command, and our command under another description
            self.assertFalse(task["owned"]())
            with self.assertRaises(OSError):
                task["register"]([r"C:\h\pythonw.exe", "-I", "-m", "eutonos.desktop_companion_runtime", "--ensure-current"])
            with self.assertRaises(OSError):
                task["delete"]()
            self.assertEqual((machine.task, machine.task_description), (seeded, "My own task"))  # never touched
        machine.seed_task(ic.OLDER_COMPANION_TASK, runtime.TASK_DESCRIPTION)  # an earlier EUTONOS registration
        self.assertTrue(task["owned"]())
        machine.seed_task(ic.OLDER_COMPANION_TASK)  # no description: not known to be ours, so not ours
        self.assertFalse(task["owned"]())

    def test_leftovers_names_launchers_a_marketplace_a_carrier_word_and_a_new_user_file(self):
        user, home = self.base / "user", self.base / "home"
        (user / ".claude").mkdir(parents=True)
        (user / ".claude" / "CLAUDE.md").write_text("mine\n", encoding="utf-8")
        seeded = ic.snapshot(user)
        self.assertEqual(ic.leftovers(self.base, user, home, seeded), [])
        (home / "bin").mkdir(parents=True)
        (home / "bin" / "eutonos-x.exe").write_bytes(b"x")
        (home / "marketplace" / "crosstalk").mkdir(parents=True)
        (user / ".claude" / "CLAUDE.md").write_text("mine\nEUTONOS block\n", encoding="utf-8")
        (user / "stray.txt").write_text("x", encoding="utf-8")
        found = ic.leftovers(self.base, user, home, seeded)
        self.assertEqual(len(found), 4, found)

    def test_setup_cli_refuses_a_root_without_the_check_script(self):
        from eutonos import setup_cli
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(setup_cli.main(["install-check", "--root", str(self.base)]), 2)
        self.assertIn("needs a EUTONOS checkout", json.loads(err.getvalue())["error"])


@unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "the check builds its wheel from git archive HEAD")
class FullCheckTests(unittest.TestCase):
    def test_install_verify_uninstall_and_reinstall_all_pass_on_temporary_folders(self):
        result = ic.run(ROOT)
        failed = [item for item in result["steps"] if not item["ok"]]
        self.assertEqual(result["outcome"], "passed", failed)
        names = [item["step"] for item in result["steps"]]
        for wanted in ("dry run plans and changes nothing", "install", "uninstall: user files are byte-identical to the seed",
                       "uninstall: no carriers are left", "reinstall is idempotent: same Effects and the same user files"):
            self.assertIn(wanted, names)
        if os.name == "nt":  # the task steps exist on Windows only, where the companion's scheduled task exists
            for wanted in ("companion task, a task that runs something else: install leaves it alone",
                           "companion task, a task that runs something else: uninstall leaves it as it was",
                           "companion task, a task that runs our command under its own description: install leaves it alone",
                           "companion task, a task that runs our command under its own description: "
                           "uninstall leaves it as it was",
                           "companion task, one EUTONOS registered earlier: install replaces it and keeps it for uninstall",
                           "companion task, one EUTONOS registered earlier: uninstall puts it back"):
                self.assertIn(wanted, names)

    @unittest.skipUnless(os.name == "nt", "the companion's scheduled task exists on Windows only")
    def test_it_fails_when_the_task_seam_cannot_say_whose_a_task_is_and_a_foreign_one_gets_replaced(self):
        # Without ``owned`` the companion step takes any task that runs our command for ours and registers over it; the
        # real runtime refuses that for a task under another description. The check must see it, not pass on a fake
        # that never says no.
        real = ic.Machine.seams

        def without_owned(self):
            seams = real(self)
            seams["task"] = {name: value for name, value in seams["task"].items() if name != "owned"}
            return seams

        with unittest.mock.patch.object(ic.Machine, "seams", without_owned):
            result = ic.run(ROOT)
        self.assertEqual(result["outcome"], "failed")
        failed = [item["step"] for item in result["steps"] if not item["ok"]]
        self.assertEqual(failed, ["companion task, a task that runs our command under its own description: "
                                  "install leaves it alone"], failed)

    def test_it_fails_when_an_uninstall_leaves_a_carrier_behind(self):
        from eutonos import installer
        real = installer.uninstall

        def leaky(**kwargs):
            outcome = real(**kwargs)
            stray = Path(kwargs["home"]) / "bin"
            stray.mkdir(parents=True, exist_ok=True)
            (stray / "eutonos-left-behind.exe").write_bytes(b"x")
            return outcome

        with unittest.mock.patch.object(installer, "uninstall", leaky):
            result = ic.run(ROOT)
        self.assertEqual(result["outcome"], "failed")
        self.assertTrue(any("no carriers are left" in item["step"] and not item["ok"] for item in result["steps"]))

    def test_the_real_home_is_never_the_target(self):
        real = Path.home() / ".eutonos"
        before = sorted(str(p) for p in real.rglob("*")) if real.exists() else None
        ic.run(ROOT)
        after = sorted(str(p) for p in real.rglob("*")) if real.exists() else None
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
