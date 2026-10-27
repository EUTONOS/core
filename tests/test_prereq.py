"""IPC3 lane B: the prerequisite check reports, guides, installs nothing and exits 2 when a requirement is missing."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from eutonos import prereq, setup_cli

PRESENT = {"git": "/bin/git", "node": "/bin/node", "claude": "/bin/claude", "codex": "/bin/codex", "hermes": "/bin/hermes"}


def fake_run(command, **kwargs):
    return SimpleNamespace(stdout="%s 1.2.3\n" % Path(command[0]).name, stderr="")


def row(report, name):
    return next(item for item in report["requirements"] if item["name"] == name)


class PrereqTests(unittest.TestCase):
    def check(self, present=None, **kwargs):
        found = PRESENT if present is None else present
        return prereq.prereq_check(which=found.get, run=fake_run, **kwargs)

    def test_everything_present_is_ready_and_reports_versions(self):
        report = self.check(version_info=(3, 12, 1))
        self.assertEqual((report["outcome"], report["missing"], report["installs"]), ("ready", [], "none"))
        self.assertEqual(row(report, "node")["version"], "node 1.2.3")
        self.assertEqual(row(report, "python")["version"], "3.12.1")
        self.assertEqual(report["guidance"], [])

    def test_git_hidden_is_missing_with_guidance(self):
        report = self.check({k: v for k, v in PRESENT.items() if k != "git"}, version_info=(3, 13, 0))
        self.assertEqual((report["outcome"], report["missing"]), ("missing", ["git"]))
        self.assertTrue(any("git" in line.lower() for line in report["guidance"]))

    def test_python_311_is_missing_with_guidance_per_platform(self):
        for platform, hint in (("win32", "winget install Python.Python.3.12"), ("darwin", "brew install python@3.12"),
                               ("linux", "package manager")):
            with self.subTest(platform=platform):
                report = self.check(version_info=(3, 11, 9), platform=platform)
                self.assertEqual((report["outcome"], report["missing"]), ("missing", ["python"]))
                self.assertIn(hint, report["guidance"][0])
                self.assertEqual(row(report, "python")["version"], "3.11.9")

    def test_optional_tools_never_fail_the_check(self):
        report = self.check({"git": "/bin/git"}, version_info=(3, 12, 0))
        self.assertEqual(report["outcome"], "ready")
        for name in ("node", "claude", "codex", "hermes"):
            self.assertFalse(row(report, name)["ok"])
            self.assertIn("Optional", row(report, name)["guidance"])
        self.assertEqual(report["guidance"], [])

    def test_unrunnable_tool_reports_no_version(self):
        def broken(command, **kwargs):
            raise OSError("no")
        report = prereq.prereq_check(which=PRESENT.get, run=broken, version_info=(3, 12, 0))
        self.assertEqual(report["outcome"], "ready")
        self.assertIsNone(row(report, "node")["version"])

    def test_setup_prereq_exits_2_when_python_is_311(self):
        fake = SimpleNamespace(version_info=(3, 11, 9, "final", 0), platform="win32", executable=sys.executable)
        raw = io.BytesIO()
        with mock.patch.object(prereq, "sys", fake), mock.patch.object(setup_cli.sys, "stdout", SimpleNamespace(buffer=raw)), \
                redirect_stderr(io.StringIO()) as err:
            code = setup_cli.main(["prereq"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(raw.getvalue())["missing"], ["python"])
        self.assertIn("winget install Python.Python.3.12", err.getvalue())
        self.assertIn("Nothing was installed", err.getvalue())

    def test_setup_prereq_with_git_hidden_still_passes_and_names_what_it_limits(self):
        raw = io.BytesIO()
        real = prereq.shutil.which
        with mock.patch.object(prereq.shutil, "which", lambda name, *a, **k: None if name == "git" else real(name, *a, **k)),                 mock.patch.object(setup_cli.sys, "stdout", SimpleNamespace(buffer=raw)), redirect_stderr(io.StringIO()) as err:
            code = setup_cli.main(["prereq"])
        result = json.loads(raw.getvalue())
        self.assertEqual((code, result["missing"], result["outcome"]), (0, [], "ready"))
        self.assertEqual(result["unavailable_without_git"], ["core"])  # only the core declares git; it installs, with less
        self.assertIn("git", err.getvalue().lower())
        self.assertIn("project memory", err.getvalue())

    def test_setup_prereq_exits_0_when_ready(self):
        raw = io.BytesIO()
        with mock.patch.object(prereq, "prereq_check", return_value=self.check(version_info=(3, 12, 0))), \
                mock.patch.object(setup_cli.sys, "stdout", SimpleNamespace(buffer=raw)):
            code = setup_cli.main(["prereq"])
        self.assertEqual((code, json.loads(raw.getvalue())["outcome"]), (0, "ready"))

    def test_prereq_runs_without_a_project_root(self):
        with tempfile.TemporaryDirectory() as empty:
            run = subprocess.run([sys.executable, "-m", "eutonos.setup_cli", "prereq", "--root", os.path.join(empty, "missing")],
                                 capture_output=True)
        self.assertIn(run.returncode, (0, 2))
        self.assertEqual(json.loads(run.stdout)["profile"], "eutonos.prereq.v1")


class GitIsOnlyNeededByThePartsThatUseItTests(unittest.TestCase):
    def test_the_install_check_does_not_require_git_but_keeps_the_row_and_guidance(self):
        report = prereq.install_prereq_check(which={"node": "/bin/node"}.get, run=fake_run, version_info=(3, 12, 0))
        self.assertEqual((report["outcome"], report["missing"]), ("ready", []))
        git = row(report, "git")
        self.assertFalse(git["ok"])
        self.assertFalse(git["required"])
        self.assertIn("git", git["guidance"].lower())

    def test_python_is_still_required_to_install(self):
        report = prereq.install_prereq_check(which=PRESENT.get, run=fake_run, version_info=(3, 11, 0))
        self.assertEqual((report["outcome"], report["missing"]), ("missing", ["python"]))

    def test_the_sentence_names_core_as_limited_and_other_parts_as_skipped(self):
        parts = [{"id": "core", "name": "EUTONOS core"}, {"id": "later", "name": "Later part"}]
        line = prereq.without_git_line(parts)
        self.assertIn("project memory", line)
        self.assertIn("Later part", line)
        self.assertNotIn("EUTONOS core", line)
        self.assertEqual(prereq.without_git_line([]), "")

    def test_nothing_is_unavailable_when_git_is_present(self):
        report = prereq.install_prereq_check(which=PRESENT.get, run=fake_run, version_info=(3, 12, 0))
        self.assertEqual(prereq.unavailable_without_git(report, [{"id": "core", "name": "x"}]), [])
        self.assertEqual([item["id"] for item in prereq.parts_needing_git()], ["core"])


if __name__ == "__main__":
    unittest.main()
