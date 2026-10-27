"""ETT2 guided form and native record transactions."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from eutonos import user_tests as records
from eutonos import user_tests_cli as cli
from eutonos import workflow_cli


HASH = "a" * 64
REVISION = "b" * 40


def invoke(*argv, workflow=False):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        status = (workflow_cli.main(list(argv)) if workflow else cli.main(list(argv)))
    body = stdout.getvalue() if status == 0 else stderr.getvalue()
    return status, json.loads(body)


class UserTestCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return name

    def _ready_request(self):
        return {"setup_id": "login-check", "title": "Login regression",
                "purpose": "Check successful login.", "work_id": "ETT2", "readiness": "ready",
                "sources": [{"path": "src/login.py", "revision": REVISION, "sha256": HASH}],
                "prerequisites": ["A local test account exists."],
                "inputs": ["Valid local credentials."],
                "steps": [{"id": "submit", "action": "Submit valid credentials.",
                           "expected": "The account page opens."}],
                "criteria": [{"id": "opens", "description": "The account page is visible."}],
                "limits": ["Only the local account is covered."]}

    def _save_ready(self):
        request = self._write("forms/request.json", self._ready_request())
        status, formed = invoke("--root", str(self.root), "setup-form", "--template",
                                "behavior-regression", "--request", request,
                                "--output", "forms/setup.json")
        self.assertEqual(status, 0, formed)
        self.assertEqual(formed["outcome"], "formed")
        status, saved = invoke("--root", str(self.root), "save-setup", "--input", "forms/setup.json")
        self.assertEqual(status, 0, saved)
        return saved

    def test_templates_are_discoverable_through_workflow_expert_route(self):
        status, result = invoke("--root", str(self.root), "templates")
        self.assertEqual(status, 0, result)
        self.assertEqual(len(result["templates"]), 3)
        status, via_workflow = invoke("--root", str(self.root), "expert", "tests", "templates",
                                      workflow=True)
        self.assertEqual(status, 0, via_workflow)
        self.assertEqual(via_workflow, result)

    def test_default_custom_not_run_and_failed_roundtrip(self):
        saved = self._save_ready()
        self.assertEqual(saved["path"], records.setup_path("login-check"))
        status, validated = invoke("--root", str(self.root), "validate", "--input", "forms/setup.json")
        self.assertEqual(status, 0, validated)
        self.assertEqual(validated["sha256"], saved["sha256"])
        status, not_run = invoke("--root", str(self.root), "run-form", "--setup-id", "login-check",
                                 "--run-id", "run-1", "--output", "forms/run-1.json")
        self.assertEqual(status, 0, not_run)
        status, recorded = invoke("--root", str(self.root), "append-run", "--input", "forms/run-1.json")
        self.assertEqual(status, 0, recorded)
        self.assertEqual(recorded["path"], records.run_path("login-check", "run-1"))
        failure = {"outcome": "failed", "summary": "Account page did not open.",
                   "observations": [{"criterion_id": "opens", "outcome": "failed",
                                     "note": "The page showed an error."}],
                   "evidence": {"profile": records.EVIDENCE_PROFILE,
                                "items": [{"note": "Observed the local error page."}]}}
        self._write("forms/failure-request.json", failure)
        status, failed = invoke("--root", str(self.root), "run-form", "--setup-id", "login-check",
                                "--run-id", "run-2", "--request", "forms/failure-request.json",
                                "--output", "forms/run-2.json")
        self.assertEqual(status, 0, failed)
        status, recorded = invoke("--root", str(self.root), "append-run", "--input", "forms/run-2.json")
        self.assertEqual(status, 0, recorded)
        projection = records.project_tests(self.root)
        row = projection["tests"][0]
        self.assertEqual([item["record"]["outcome"] for item in row["runs"]],
                         ["not-run", "failed"])
        self.assertTrue(row["history_complete"])
        self.assertEqual(row["run_count"], 2)
        status, duplicate = invoke("--root", str(self.root), "append-run", "--input", "forms/run-2.json")
        self.assertEqual((status, duplicate["code"]), (2, "conflict"))
        status, custom = invoke("--root", str(self.root), "setup-form", "--template", "custom",
                                "--setup-id", "custom-check", "--title", "Custom question",
                                "--purpose", "Explore a novel behavior.",
                                "--output", "forms/custom.json")
        self.assertEqual(status, 0, custom)
        self.assertIsNone(json.loads((self.root / "forms/custom.json").read_text(encoding="utf-8"))["template"])
        status, custom_saved = invoke("--root", str(self.root), "save-setup",
                                      "--input", "forms/custom.json")
        self.assertEqual(status, 0, custom_saved)
        self.assertEqual(custom_saved["path"], records.setup_path("custom-check"))
        self.assertEqual({item["setup"]["id"] for item in records.project_tests(self.root)["tests"]},
                         {"login-check", "custom-check"})

    def test_guarded_update_keeps_history_and_rejects_stale_run(self):
        saved = self._save_ready()
        status, formed = invoke("--root", str(self.root), "run-form", "--setup-id", "login-check",
                                "--run-id", "before-update", "--output", "forms/before-update.json")
        self.assertEqual(status, 0, formed)
        status, recorded = invoke("--root", str(self.root), "append-run", "--input", "forms/before-update.json")
        self.assertEqual(status, 0, recorded)
        status, formed = invoke("--root", str(self.root), "run-form", "--setup-id", "login-check",
                                "--run-id", "stale-after-update", "--output", "forms/stale.json")
        self.assertEqual(status, 0, formed)
        changed = json.loads((self.root / "forms/setup.json").read_text(encoding="utf-8"))
        changed["criteria"][0]["description"] = "The account page and profile name are visible."
        self._write("forms/update.json", changed)
        status, conflict = invoke("--root", str(self.root), "save-setup", "--input", "forms/update.json")
        self.assertEqual((status, conflict["code"]), (2, "conflict"))
        status, conflict = invoke("--root", str(self.root), "save-setup", "--input", "forms/update.json",
                                  "--expected-sha256", "0" * 64)
        self.assertEqual((status, conflict["code"]), (2, "conflict"))
        status, updated = invoke("--root", str(self.root), "save-setup", "--input", "forms/update.json",
                                 "--expected-sha256", saved["sha256"])
        self.assertEqual(status, 0, updated)
        status, conflict = invoke("--root", str(self.root), "append-run", "--input", "forms/stale.json")
        self.assertEqual((status, conflict["code"]), (2, "conflict"))
        projection = records.project_tests(self.root)["tests"][0]
        self.assertEqual(projection["run_count"], 0)
        self.assertEqual(projection["stale_runs"][0]["record"]["id"], "before-update")
        self.assertFalse((self.root / records.run_path("login-check", "stale-after-update")).exists())

    def test_bounded_input_and_safe_output_ids(self):
        (self.root / "large.json").write_bytes(b"x" * (records.MAX_RECORD_BYTES + 1))
        status, error = invoke("--root", str(self.root), "validate", "--input", "large.json")
        self.assertEqual((status, error["code"]), (2, "invalid"))
        self._write("bad-profile.json", {"profile": []})
        status, error = invoke("--root", str(self.root), "validate", "--input", "bad-profile.json")
        self.assertEqual((status, error["code"]), (2, "invalid"))
        for setup_id in ("CON", "con.txt", "LPT1", "NUL."):
            status, error = invoke("--root", str(self.root), "setup-form", "--template", "custom",
                                   "--setup-id", setup_id, "--title", "Question",
                                   "--purpose", "Inspect behavior.")
            self.assertEqual((status, error["code"]), (2, "invalid"))
        status, error = invoke("--root", str(self.root), "setup-form", "--template", "custom",
                               "--setup-id", "safe", "--title", "Question",
                               "--purpose", "Inspect behavior.",
                               "--output", ".eutonos/tests/setups/safe.json")
        self.assertEqual((status, error["code"]), (2, "invalid"))
        self.assertFalse((self.root / ".eutonos/tests/setups/safe.json").exists())
        status, formed = invoke("--root", str(self.root), "setup-form", "--template", "custom",
                                "--setup-id", "cafe", "--title", "Caf\u00e9 workflow",
                                "--purpose", "Inspect behavior.", "--output", "forms/cafe.json")
        self.assertEqual(status, 0, formed)
        self.assertIn(b"Caf\xc3\xa9", (self.root / "forms/cafe.json").read_bytes())
        status, error = invoke("--root", str(self.root), "setup-form", "--template", "custom",
                               "--setup-id", "cafe", "--title", "Caf\u00e9 workflow",
                               "--purpose", "Inspect behavior.", "--output", "forms/cafe.json")
        self.assertEqual((status, error["code"]), (2, "conflict"))


if __name__ == "__main__":
    unittest.main()
