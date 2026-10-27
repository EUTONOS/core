"""ETT1's project-owned setup, run and evidence boundaries."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eutonos import user_tests as ut


HASH = "a" * 64
REVISION = "b" * 40


def ready_setup(template="behavior-regression"):
    return ut.instantiate_setup(
        template, setup_id="login-check", title="Login regression",
        purpose="Check that valid credentials open the account page.",
        work_id="ETT1", readiness="ready",
        sources=[{"path": "src/login.py", "revision": REVISION, "sha256": HASH}],
        prerequisites=["A local test account exists."], inputs=["Valid test credentials."],
        steps=[{"id": "submit", "action": "Submit valid credentials.",
                "expected": "The account page opens."}],
        criteria=[{"id": "opens", "description": "The account page is visible."}],
        limits=["Only the local test account is covered."],
    )


def run(setup, outcome="passed", observations=None, items=None):
    if observations is None:
        observations = ([{"criterion_id": "opens", "outcome": "passed",
                          "note": "Observed the account page."}] if outcome == "passed" else [])
    if items is None:
        items = ([{"path": "evidence/login.txt", "sha256": HASH,
                   "note": "Captured account page label."}] if outcome == "passed" else [])
    return {"profile": ut.RUN_PROFILE, "id": "run-1", "setup": copy.deepcopy(setup),
            "setup_id": setup["id"], "setup_sha256": ut.record_sha256(setup),
            "recorded_at": "2026-09-28T12:00:00Z", "outcome": outcome,
            "summary": "Observed local test behavior.", "observations": observations,
            "evidence": {"profile": ut.EVIDENCE_PROFILE, "items": items}, "limits": []}


class UserTestContractTests(unittest.TestCase):
    def test_three_defaults_are_portable_and_copied(self):
        templates = ut.default_templates()
        self.assertEqual([item["id"] for item in templates],
                         ["behavior-regression", "user-workflow", "comparison-experiment"])
        templates[0]["title"] = "Changed by caller"
        self.assertEqual(ut.default_templates()[0]["title"], "Behavior or regression")

    def test_default_and_custom_setups_freeze_selected_source_and_template(self):
        default = ready_setup()
        custom = ready_setup(None)
        self.assertEqual(default["template"], {"id": "behavior-regression", "version": 1})
        self.assertIsNone(custom["template"])
        self.assertEqual(default["readiness"], "ready")
        self.assertEqual(default["sources"][0]["revision"], REVISION)
        self.assertIs(ut.validate_setup(default), default)
        self.assertEqual(ut.setup_path(default["id"]), ".eutonos/tests/setups/login-check.json")

    def test_draft_preserves_material_gaps_and_cannot_claim_pass(self):
        draft = ut.instantiate_setup("user-workflow", setup_id="draft", title="Checkout",
                                     purpose="Check customer checkout.")
        self.assertEqual(draft["readiness"], "draft")
        self.assertEqual(draft["sources"], [])
        not_run = run(draft, "not-run")
        self.assertIs(ut.validate_run(not_run), not_run)
        blocked = run(draft, "blocked")
        self.assertIs(ut.validate_run(blocked), blocked)
        invalid = run(draft, "passed", observations=[], items=[{"note": "Looks fine."}])
        with self.assertRaisesRegex(ValueError, "draft"):
            ut.validate_run(invalid)
        draft["readiness"] = "ready"
        with self.assertRaisesRegex(ValueError, "Ready setup"):
            ut.validate_setup(draft)

    def test_pass_failure_and_not_run_have_distinct_evidence_requirements(self):
        setup = ready_setup()
        passed = run(setup)
        self.assertIs(ut.validate_run(passed), passed)
        no_evidence = copy.deepcopy(passed)
        no_evidence["evidence"]["items"] = []
        with self.assertRaisesRegex(ValueError, "explicit evidence"):
            ut.validate_run(no_evidence)
        no_observation = copy.deepcopy(passed)
        no_observation["observations"] = []
        with self.assertRaisesRegex(ValueError, "every criterion"):
            ut.validate_run(no_observation)
        failed = run(setup, "failed", [{"criterion_id": "opens", "outcome": "failed",
                                        "note": "Account page did not open."}],
                     [{"note": "Observed an error page instead."}])
        self.assertIs(ut.validate_run(failed), failed)
        not_run = run(setup, "not-run")
        self.assertIs(ut.validate_run(not_run), not_run)
        not_run["evidence"]["items"] = [{"note": "A result was observed."}]
        with self.assertRaisesRegex(ValueError, "Not-run"):
            ut.validate_run(not_run)

    def test_setup_snapshot_keeps_history_after_current_setup_changes(self):
        original = ready_setup()
        historic = run(original)
        changed = copy.deepcopy(original)
        changed["criteria"][0]["description"] = "The account page and profile name are visible."
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            setup_file = root / ut.setup_path(changed["id"])
            run_file = root / ut.run_path(changed["id"], historic["id"])
            setup_file.parent.mkdir(parents=True)
            run_file.parent.mkdir(parents=True)
            setup_file.write_text(json.dumps(changed), encoding="utf-8")
            run_file.write_text(json.dumps(historic), encoding="utf-8")
            projection = ut.project_tests(root)
        self.assertEqual(projection["diagnostics"], [])
        self.assertEqual(projection["tests"][0]["run_count"], 0)
        self.assertIsNone(projection["tests"][0]["latest_run"])
        self.assertEqual(projection["tests"][0]["stale_runs"][0]["record"]["outcome"], "passed")
        self.assertEqual(projection["tests"][0]["runs"][0]["status"], "stale")
        self.assertIs(ut.validate_run(historic), historic)
        self.assertEqual(historic["setup"]["criteria"][0]["description"],
                         "The account page is visible.")

    def test_invalid_paths_fields_and_source_pins_are_rejected(self):
        setup = ready_setup()
        for bad_path in ("../secret", "/absolute/file", "C:/drive/file", "src\\file.py",
                         "src//file.py"):
            changed = copy.deepcopy(setup)
            changed["sources"][0]["path"] = bad_path
            with self.subTest(path=bad_path), self.assertRaisesRegex(ValueError, "path|project"):
                ut.validate_setup(changed)
        changed = copy.deepcopy(setup)
        changed["sources"][0]["sha256"] = "unknown"
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            ut.validate_setup(changed)
        changed = copy.deepcopy(setup)
        changed["command"] = "run anything"
        with self.assertRaisesRegex(ValueError, "unsupported"):
            ut.validate_setup(changed)
        result = run(setup)
        result["setup_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "digest"):
            ut.validate_run(result)
        result = run(setup)
        result["observations"][0]["criterion_id"] = ["opens"]
        with self.assertRaisesRegex(ValueError, "Criterion ID"):
            ut.validate_run(result)
        with self.assertRaisesRegex(ValueError, "stable ID"):
            ut.setup_path("../escape")

    def test_projection_reports_malformed_run_without_claiming_pass(self):
        setup = ready_setup()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            setup_file = root / ut.setup_path(setup["id"])
            run_file = root / ut.run_path(setup["id"], "run-1")
            setup_file.parent.mkdir(parents=True)
            run_file.parent.mkdir(parents=True)
            setup_file.write_text(json.dumps(setup), encoding="utf-8")
            run_file.write_text('{"profile":"bad"}', encoding="utf-8")
            projection = ut.project_tests(root)
        self.assertEqual(projection["tests"][0]["run_count"], 0)
        self.assertIsNone(projection["tests"][0]["latest_run"])
        self.assertFalse(projection["tests"][0]["history_complete"])
        self.assertEqual(len(projection["diagnostics"]), 1)

    def test_truncated_history_never_claims_a_latest_run(self):
        setup = ready_setup()
        first = run(setup)
        second = run(setup)
        second["id"] = "run-2"
        second["recorded_at"] = "2026-09-28T13:00:00Z"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            setup_file = root / ut.setup_path(setup["id"])
            setup_file.parent.mkdir(parents=True)
            setup_file.write_text(json.dumps(setup), encoding="utf-8")
            for result in (first, second):
                run_file = root / ut.run_path(setup["id"], result["id"])
                run_file.parent.mkdir(parents=True, exist_ok=True)
                run_file.write_text(json.dumps(result), encoding="utf-8")
            with mock.patch.object(ut, "MAX_RUNS_PER_SETUP", 1):
                projection = ut.project_tests(root)
        row = projection["tests"][0]
        self.assertFalse(row["history_complete"])
        self.assertTrue(row["run_count_partial"])
        self.assertEqual(row["run_count"], 1)
        self.assertIsNone(row["latest_run"])
        self.assertEqual(len(row["runs"]), 1)
        self.assertTrue(any("Run scan limit" in item["error"] for item in projection["diagnostics"]))

    def test_missing_test_directory_is_an_empty_projection(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / ".eutonos").mkdir()
            projection = ut.project_tests(temp)
        self.assertEqual(projection, {"tests": [], "diagnostics": []})


if __name__ == "__main__":
    unittest.main()
