import copy
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
# The owner-authorized freeze (2026-09-14). Fixture seeds pin exact bytes of this
# revision's src/ and tests, so the suite is checked against an extracted copy of it;
# later main changes to those paths must not read as fixture drift.
FREEZE_REVISION = "22b4193166e4615a15eed090ce959e3ac9a0bf6f"
SPEC = importlib.util.spec_from_file_location("emc_experiment", ROOT / "tools" / "emc_experiment.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class EmcExperimentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._frozen_temp = tempfile.TemporaryDirectory(prefix="emc-freeze-")
        cls.frozen = Path(cls._frozen_temp.name) / "freeze"
        cls.frozen.mkdir()
        archive = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", FREEZE_REVISION],
                                 check=True, stdout=subprocess.PIPE).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(cls.frozen, filter="data")
        for command in (["init", "-q"], ["config", "core.autocrlf", "false"], ["add", "."],
                        ["-c", "user.name=EMC Fixture", "-c", "user.email=fixture.invalid",
                         "commit", "-q", "-m", "freeze"]):
            subprocess.run(["git", "-C", str(cls.frozen)] + command, check=True)
        cls.manifest = json.loads((cls.frozen / "experiments" / "EMC_WORKFLOW_SUITE.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls._frozen_temp.cleanup()

    def test_selected_matrix_has_ninety_arms_and_180_worker_assignments(self):
        result = MODULE.validate(self.manifest)
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual((result["test_count"], result["shape_count"]), (5, 9))
        self.assertEqual((result["arm_count"], result["worker_assignments"]), (90, 180))
        self.assertFalse(result["dispatch_authorized"])

    def test_text_fixture_hashes_are_independent_of_windows_worktree_endings(self):
        for suffix in (".py", ".md", ".json", ".txt", ".css", ".html", ".js", ".cjs"):
            self.assertEqual(MODULE.source_sha256(b"line\r\nnext\r\n", "fixture" + suffix),
                             MODULE.source_sha256(b"line\nnext\n", "fixture" + suffix))

    def test_upward_or_same_rank_support_is_rejected(self):
        changed = copy.deepcopy(self.manifest)
        changed["execution_shapes"][1]["primary"] = "luna"
        changed["execution_shapes"][1]["support"] = "astra"
        result = MODULE.validate(changed)
        self.assertEqual(result["outcome"], "invalid")
        self.assertTrue(any("lower-ranked" in error for error in result["errors"]))

    def test_capture_ram_and_dispatch_conditions_are_fixed(self):
        changed = copy.deepcopy(self.manifest)
        changed["capture_during_answer"] = True
        changed["ram_conditions"] = ["on"]
        changed["dispatch_authorized"] = True
        errors = MODULE.validate(changed)["errors"]
        self.assertTrue(any("capture_during_answer" in error for error in errors))
        self.assertTrue(any("ram_conditions" in error for error in errors))
        self.assertTrue(any("dispatch_authorized" in error for error in errors))

    def test_materialized_fixtures_have_frozen_prompt_seed_grader_and_ram_hashes(self):
        result = MODULE.validate_materialized(self.frozen, self.manifest)
        self.assertEqual(result["outcome"], "ok", result["errors"])
        self.assertFalse(result["revision_bound"])
        self.assertEqual(len(result["fixtures"]), 5)
        for item in self.manifest["tests"]:
            fixture = json.loads((self.frozen / item["fixture"] / "fixture.json").read_text(encoding="utf-8"))
            self.assertEqual(fixture["seed_status"], "materialized")
            self.assertEqual(sum(row["weight"] for row in fixture["rubric"]), 100)
            prompt = (self.frozen / item["fixture"] / fixture["actor_prompt"]["path"]).read_text(encoding="utf-8")
            self.assertNotIn("grader", prompt.casefold())
            self.assertNotIn("expected mutation", prompt.casefold())

    def test_current_head_cannot_be_planned_as_if_it_contains_uncommitted_fixtures(self):
        with tempfile.TemporaryDirectory(prefix="emc-uncommitted-") as temp:
            # HEAD holds a changed prompt; the correct frozen prompt exists only as an
            # uncommitted working-tree restore, so planning HEAD must be refused.
            repo, _ = self.make_frozen_repository(temp)
            fixture_root = repo / "experiments" / "fixtures" / "emc-t1"
            prompt = fixture_root / MODULE.read_manifest(fixture_root / "fixture.json")["actor_prompt"]["path"]
            frozen_bytes = prompt.read_bytes()
            prompt.write_bytes(frozen_bytes + b"\nChanged in the committed revision.\n")
            subprocess.run(["git", "-c", "user.name=EMC Fixture", "-c", "user.email=fixture.invalid",
                            "-C", str(repo), "commit", "-q", "-am", "drift"], check=True)
            head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            prompt.write_bytes(frozen_bytes)
            with self.assertRaisesRegex(ValueError, "revision does not contain|frozen bytes"):
                MODULE.project_plan(self.manifest, head, repo)
            for revision in ("abc", "A" * 40, "g" * 40):
                with self.assertRaisesRegex(ValueError, "full lowercase"):
                    MODULE.project_plan(self.manifest, revision, repo)

    def test_plan_rejects_an_uncommitted_suite_change_even_when_fixtures_are_committed(self):
        with tempfile.TemporaryDirectory(prefix="emc-suite-drift-") as temp:
            repo, revision = self.make_frozen_repository(temp)
            suite_path = repo / "experiments" / "EMC_WORKFLOW_SUITE.json"
            changed = MODULE.read_manifest(suite_path)
            changed["status"] = "changed-after-freeze"
            MODULE._write_json(suite_path, changed)
            with self.assertRaisesRegex(ValueError, "workflow-suite bytes"):
                MODULE.project_plan(changed, revision, repo)

    def test_non_dispatching_smoke_covers_failing_before_and_long_characterization(self):
        residue_before = {path.relative_to(self.frozen).as_posix() for path in
                          (self.frozen / "experiments" / "fixtures").rglob("__pycache__")}
        real_command = MODULE._smoke_command
        stubbed = []

        def node_stub(cwd, command, expected):
            # Node.js is optional on this machine: stub only its checks so every other
            # smoke check, including the EMC-T5 Python one, still runs for real.
            if command[0] == "node" and not shutil.which("node"):
                stubbed.append(command)
                return {"command": command, "expected": expected, "returncode": expected,
                        "passed": True, "output_sha256": "node-not-installed"}
            return real_command(cwd, command, expected)

        with mock.patch.object(MODULE, "_smoke_command", node_stub):
            result = MODULE.smoke(self.frozen, self.manifest)
        self.assertEqual(result["outcome"], "ok", result["errors"])
        self.assertEqual((result["answer_production_calls"], result["reviewer_calls"]), (0, 0))
        rows = {row["id"]: row for row in result["fixtures"]}
        self.assertEqual([check["returncode"] for check in rows["EMC-T2"]["checks"]], [1, 0])
        self.assertEqual([check["returncode"] for check in rows["EMC-T3"]["checks"]], [1, 0])
        self.assertTrue(all(row["outcome"] == "ok" for row in rows.values()))
        real_t5 = [check for check in rows["EMC-T5"]["checks"] if check["command"][0] != "node"]
        self.assertTrue(real_t5, "EMC-T5 must keep at least one real, non-node smoke check")
        self.assertTrue(all(check["output_sha256"] != "node-not-installed" for check in real_t5))
        self.assertTrue(all(command[0] == "node" for command in stubbed))
        residue_after = {path.relative_to(self.frozen).as_posix() for path in
                         (self.frozen / "experiments" / "fixtures").rglob("__pycache__")}
        self.assertEqual(residue_after, residue_before)

    def make_frozen_repository(self, destination):
        repo = Path(destination) / "repo"
        repo.mkdir()
        shutil.copy2(self.frozen / ".gitattributes", repo / ".gitattributes")
        shutil.copy2(self.frozen / ".gitignore", repo / ".gitignore")
        shutil.copytree(self.frozen / "experiments", repo / "experiments")
        shutil.copytree(self.frozen / "src", repo / "src")
        (repo / "tests").mkdir()
        for name in ("test_dashboard.py", "test_ram.py", "test_skills_ui.cjs"):
            shutil.copy2(self.frozen / "tests" / name, repo / "tests" / name)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "core.autocrlf", "false"], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-c", "user.name=EMC Fixture", "-c", "user.email=fixture.invalid",
                        "-C", str(repo), "commit", "-q", "-m", "frozen fixture"], check=True)
        revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        return repo, revision

    def test_bound_plan_arm_is_detached_isolated_and_preserves_host_evidence(self):
        with tempfile.TemporaryDirectory(prefix="emc-frozen-") as temp:
            repo, revision = self.make_frozen_repository(temp)
            manifest = MODULE.read_manifest(repo / "experiments" / "EMC_WORKFLOW_SUITE.json")
            plan = MODULE.project_plan(manifest, revision, repo)
            self.assertEqual(len({row["actor_commit_id"] for row in plan["arms"] if row["task_id"] == "EMC-T1"}), 1)
            self.assertEqual((len(plan["arms"]), plan["worker_assignments"], plan["reviewer_assignments"]), (90, 180, 90))
            self.assertEqual([row["ram_condition"] for row in plan["arms"][:4]], ["off", "on", "on", "off"])
            t5 = [row for row in plan["arms"] if row["task_id"] == "EMC-T5"]
            self.assertEqual(sum(row["answer_worker_assignments"] for row in t5), 60)
            self.assertTrue(all(len(row["answer_phases"]) == 2 for row in t5))
            self.assertTrue(all(row["answer_phases"][1]["fresh_identities_required"] for row in t5))
            ledger = plan["benchmark_suite"]
            self.assertEqual(ledger["status"], "planned-unstarted")
            self.assertEqual(len(ledger["results"]), 90)
            self.assertEqual({row["id"] for row in ledger["results"]}, {row["id"] for row in plan["arms"]})
            self.assertTrue(all(row["answer_run_ids"] == [] and row["review_run_ids"] == []
                                and row["quality"] is None for row in ledger["results"]))
            self.assertEqual(ledger["limits"], {"arm_count": 90, "answer_worker_assignments": 180,
                                                "reviewer_assignments": 90, "effort": "medium",
                                                "arm_concurrency": 1})
            self.assertEqual(plan["suite_file_sha256"], MODULE.sha256_path(repo / "experiments" / "EMC_WORKFLOW_SUITE.json"))
            arm = "emc-t1-astra-solo-ram-off"
            run_root = Path(temp) / "runs"
            with self.assertRaisesRegex(ValueError, "outside the source checkout"):
                MODULE.prepare_arm(repo, plan, arm, repo / "runs")
            state = MODULE.prepare_arm(repo, plan, arm, run_root)
            self.assertEqual(state["status"], "prepared")
            self.assertEqual(state["source_revision"], revision)
            self.assertEqual(state["worktree_head"], plan["arms"][0]["actor_commit_id"])
            self.assertNotEqual(state["worktree_identity"], state["host_namespace_identity"])
            self.assertEqual(subprocess.check_output(
                ["git", "-C", state["worktree"], "rev-parse", "--abbrev-ref", "HEAD"], text=True).strip(), "HEAD")
            with self.assertRaisesRegex(ValueError, "disabled"):
                MODULE.ram_access(run_root, arm, "search", query="NAV")
            self.assertEqual(MODULE.recover_arm(repo, run_root, arm)["worktree_status"], [])
            preserved = MODULE.cleanup_arm(repo, run_root, arm)
            self.assertEqual((preserved["outcome"], preserved["reason"]),
                             ("preserved", "host namespace exists and must be reviewed explicitly"))
            archived = MODULE.archive_arm(repo, run_root, arm)
            self.assertEqual(archived["outcome"], "archived")
            self.assertTrue(Path(archived["archive"], "worktree").exists())
            self.assertTrue(Path(archived["archive"], "host").exists())

    def test_each_actor_repository_excludes_controller_files_and_source_git_objects(self):
        with tempfile.TemporaryDirectory(prefix="emc-actor-isolation-") as temp:
            repo, revision = self.make_frozen_repository(temp)
            manifest = MODULE.read_manifest(repo / "experiments" / "EMC_WORKFLOW_SUITE.json")
            plan = MODULE.project_plan(manifest, revision, repo)
            controller_bytes = {(repo / "experiments" / "EMC_RAM_BANK.json").read_bytes()}
            for controller_path in (repo / "experiments" / "fixtures").rglob("*"):
                if controller_path.is_file() and (controller_path.name in {"fixture.json", "seed.json", "grader.json"}
                                                  or "hidden" in controller_path.parts):
                    controller_bytes.add(controller_path.read_bytes())
            for test_id in ("EMC-T1", "EMC-T2", "EMC-T3", "EMC-T4", "EMC-T5"):
                arm = f"{test_id.lower()}-terra-solo-ram-off"
                run_root = Path(temp) / ("runs-" + test_id.lower())
                state = MODULE.prepare_arm(repo, plan, arm, run_root)
                actor = Path(state["worktree"])
                node = shutil.which("node") or "node"
                smoke_commands = {
                    "EMC-T1": [([sys.executable, "tools/workflow.py", "describe", "nav"], 0)],
                    "EMC-T2": [([sys.executable, "-m", "unittest", "-v", "test_receipt_usage.py"], 1)],
                    "EMC-T3": [([sys.executable, "-m", "unittest", "-v", "test_workflow_diagnostics.py"], 1)],
                    "EMC-T4": [([sys.executable, "experiments/fixtures/emc-t4/characterization.py"], 0)],
                    "EMC-T5": [([sys.executable, "experiments/fixtures/emc-t5/characterization.py"], 0),
                               ([node, "experiments/fixtures/emc-t5/characterization.cjs"], 0),
                               ([node, "--check", "src/eutonos/dashboard_static/app.js"], 0)],
                }
                smoke_environment = dict(os.environ)
                smoke_environment["PYTHONDONTWRITEBYTECODE"] = "1"
                for command, expected_exit in smoke_commands[test_id]:
                    if command[0] == "node":  # not installed: the isolation checks below still run
                        continue
                    completed = subprocess.run(command, cwd=actor, env=smoke_environment,
                                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                    self.assertEqual(completed.returncode, expected_exit, completed.stdout)
                filesystem = {path.relative_to(actor).as_posix() for path in actor.rglob("*") if ".git" not in path.parts}
                tree = set(subprocess.check_output(
                    ["git", "-C", str(actor), "ls-tree", "-r", "--name-only", "HEAD"], text=True).splitlines())
                planned_arm = next(row for row in plan["arms"] if row["id"] == arm)
                expected = set(planned_arm["actor_paths"])
                self.assertEqual(tree, expected)
                fixture_root = repo / planned_arm["fixture"]
                fixture = MODULE.read_manifest(fixture_root / "fixture.json")
                seed = MODULE.read_manifest(fixture_root / fixture["seed"]["path"])
                allowed = set(seed.get("allowed_diff_paths", []))
                admitted = set(state["host_setup"]["paths"])
                self.assertEqual(planned_arm["allowed_diff_paths"], seed.get("allowed_diff_paths", []))
                self.assertEqual(admitted, set(planned_arm["host_admitted_paths"]))
                self.assertLessEqual(len(admitted), 64)
                self.assertLessEqual(allowed, admitted)
                self.assertTrue(all(path in expected for path in allowed))
                expected_visible = set(expected)
                for path in expected:
                    parts = PurePosixPath(path).parts
                    expected_visible.update("/".join(parts[:index]) for index in range(1, len(parts)))
                self.assertEqual(filesystem, expected_visible)
                self.assertFalse(any("grader" in path.casefold() or "/hidden/" in f"/{path.casefold()}/"
                                     or path.endswith("seed.json") or path == "experiments/EMC_RAM_BANK.json"
                                     or path == "tools/emc_experiment.py" for path in tree))
                objects = subprocess.check_output(
                    ["git", "-C", str(actor), "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)"],
                    text=True).splitlines()
                actor_blobs = {subprocess.check_output(["git", "-C", str(actor), "cat-file", "blob", line.split()[0]])
                               for line in objects if line.endswith(" blob")}
                self.assertTrue(actor_blobs.isdisjoint(controller_bytes))
                self.assertNotEqual(subprocess.run(
                    ["git", "-C", str(actor), "cat-file", "-e", revision + "^{commit}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode, 0)
                self.assertFalse((actor / ".git" / "objects" / "info" / "alternates").exists())
                self.assertFalse(any(path.name == "__pycache__" for path in actor.rglob("__pycache__")))
                if seed.get("actor_directories"):
                    _, directory_rows = MODULE.directory_projection(
                        repo, fixture_root, seed["actor_directories"][0])
                    dependency = next(row["target"] for row in directory_rows if row["target"] not in admitted)
                    self.assertTrue((actor / dependency).is_file())
                    host_environment = dict(os.environ)
                    host_environment["PYTHONPATH"] = str(repo / "src")
                    base = [sys.executable, "-m", "eutonos.host_cli"]
                    scope = ["--root", str(actor), "--host", state["host"],
                             "--actor", "emc-" + arm, "--attempt", "admission-check"]
                    started = subprocess.run(base + ["start"] + scope, env=host_environment,
                                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                    self.assertEqual(started.returncode, 0, started.stdout)
                    permitted = subprocess.run(base + ["reserve"] + scope + ["--path", sorted(allowed)[0]],
                                               env=host_environment, stdout=subprocess.PIPE,
                                               stderr=subprocess.STDOUT, text=True)
                    self.assertEqual(permitted.returncode, 0, permitted.stdout)
                    denied = subprocess.run(base + ["reserve"] + scope + ["--path", dependency],
                                            env=host_environment, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, text=True)
                    self.assertEqual(denied.returncode, 2, denied.stdout)
                    self.assertIn("Effect exceeds host-admitted paths", denied.stdout)
                if test_id == "EMC-T3":
                    committed = subprocess.check_output(
                        ["git", "-C", str(actor), "show", "HEAD:notes/owner-draft.txt"])
                    self.assertNotEqual(committed, (actor / "notes" / "owner-draft.txt").read_bytes())
                    self.assertIn("notes/owner-draft.txt", "\n".join(MODULE.recover_arm(repo, run_root, arm)["worktree_status"]))
                MODULE.archive_arm(repo, run_root, arm)

    def test_ram_on_is_one_search_frozen_bank_and_capture_off(self):
        with tempfile.TemporaryDirectory(prefix="emc-ram-") as temp:
            run_root = Path(temp)
            arm = "emc-t2-terra-solo-ram-on"
            state = {"profile": MODULE.STATE_PROFILE, "arm": {"ram_bank_sha256": self.manifest["ram_bank"]["sha256"]},
                     "capture_allowed": False, "ram": {"condition": "on", "search_calls": 0, "get_calls": 0,
                                                         "eligible": [], "expanded": []}}
            MODULE._write_json(run_root / "states" / f"{arm}.json", state)
            (run_root / "controller").mkdir()
            shutil.copy2(self.frozen / self.manifest["ram_bank"]["path"], run_root / "controller" / "ram-bank.json")
            with self.assertRaisesRegex(ValueError, "search preview"):
                MODULE.ram_access(run_root, arm, "get", record_id="emc-ram-skill-identity")
            result = MODULE.ram_access(run_root, arm, "search", query="skill duplicate receipt")
            self.assertTrue(result["results"])
            chosen = result["results"][0]["id"]
            self.assertEqual(MODULE.ram_access(run_root, arm, "get", record_id=chosen)["record"]["id"], chosen)
            with self.assertRaisesRegex(ValueError, "one bounded search"):
                MODULE.ram_access(run_root, arm, "search", query="again")
            after = MODULE.read_manifest(run_root / "states" / f"{arm}.json")
            self.assertFalse(after["capture_allowed"])
            self.assertEqual((after["ram"]["search_calls"], after["ram"]["get_calls"]), (1, 1))

    def test_t5_checkpoint_requires_fresh_continuation_and_no_chat_history(self):
        with tempfile.TemporaryDirectory(prefix="emc-handoff-") as temp:
            repo, revision = self.make_frozen_repository(temp)
            manifest = MODULE.read_manifest(repo / "experiments" / "EMC_WORKFLOW_SUITE.json")
            plan = MODULE.project_plan(manifest, revision, repo)
            arm = "emc-t5-astra-luna-ram-on"
            run_root = Path(temp) / "runs"
            MODULE.prepare_arm(repo, plan, arm, run_root)
            checks = {"checks": [{"id": name, "outcome": "passed"} for name in
                                  ("javascript-syntax", "client-characterization", "dashboard-focused")]}
            check_path = Path(temp) / "checks.json"
            MODULE._write_json(check_path, checks)
            checkpoint = MODULE.checkpoint_t5(repo, run_root, arm, "phase-one", check_path)
            self.assertFalse(checkpoint["conversation_history"])
            with self.assertRaisesRegex(ValueError, "fresh primary"):
                MODULE.resume_t5(repo, run_root, arm, "phase-one")
            worktree = Path(MODULE.read_manifest(run_root / "states" / f"{arm}.json")["worktree"])
            scratch = worktree / "phase-two-untracked.txt"
            scratch.write_text("changed after checkpoint\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "changed after"):
                MODULE.resume_t5(repo, run_root, arm, "phase-two")
            scratch.unlink()
            resumed = MODULE.resume_t5(repo, run_root, arm, "phase-two")
            self.assertEqual(resumed["outcome"], "phase-two-ready")
            self.assertFalse(resumed["capture_allowed"])


if __name__ == "__main__":
    unittest.main()
