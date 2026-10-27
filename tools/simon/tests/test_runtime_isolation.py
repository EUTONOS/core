"""Extraction compatibility and enforced runtime isolation, without desktop I/O."""
from __future__ import annotations

import inspect
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.runtime_support import REAL_RUNTIME, RuntimeGuardedTestCase, redirect_runtime

import agent_helper
import helperctl
import simon_artifacts
import simon_paths
import simon_protocol


class RuntimeIsolationTests(RuntimeGuardedTestCase):
    def test_all_discovered_test_cases_use_the_runtime_guard(self):
        # A future test must not escape the swallowed-error check by inheriting
        # directly from unittest.TestCase. Discovery loads modules before runs.
        for name, module in tuple(sys.modules.items()):
            if not (name.startswith("test_") or name.startswith("tests.test_")):
                continue
            for _, cls in inspect.getmembers(module, inspect.isclass):
                if issubclass(cls, unittest.TestCase):
                    self.assertTrue(issubclass(cls, RuntimeGuardedTestCase), f"{name}.{cls.__name__}")

    def test_guard_blocks_real_runtime_access_even_when_errors_are_swallowed(self):
        # Negative probes run in a child suite: its recorded violations must fail
        # that suite, and the audit hook must block I/O before it touches disk.
        probes = {
            "read": 'REAL_RUNTIME.joinpath("probe.txt").read_text()',
            "write": 'REAL_RUNTIME.joinpath("probe.txt").write_text("must not write")',
            "stat": 'REAL_RUNTIME.exists()',
            "mkdir": 'REAL_RUNTIME.mkdir(exist_ok=True)',
            "worker": 'thread = threading.Thread(target=attempt); thread.start(); thread.join()',
        }
        for name, operation in probes.items():
            with self.subTest(operation=name):
                if name == "worker":
                    body = operation
                else:
                    body = f"attempt(lambda: {operation})"
                code = f'''
import os
import sys
import threading
import unittest
from tests.runtime_support import REAL_RUNTIME, RuntimeGuardedTestCase
# A second barrier keeps mutation probes safe if the primary guard regresses.
# It raises without recording a violation, so the child suite would incorrectly
# pass and the parent assertion would detect the broken primary guard.
def safety_net(event, args):
    if event not in {{"open", "os.mkdir"}} or not isinstance(args[0], (str, bytes, os.PathLike)):
        return
    key = os.path.normcase(os.path.abspath(os.fsdecode(args[0])))
    root = os.path.normcase(os.path.abspath(REAL_RUNTIME))
    if key == root or key.startswith(root + os.sep):
        raise AssertionError("Negative-probe safety barrier")
sys.addaudithook(safety_net)
def attempt(action=lambda: REAL_RUNTIME.joinpath("probe.txt").write_text("must not write")):
    try:
        action()
    except Exception:
        pass
class Probe(RuntimeGuardedTestCase):
    def test_probe(self):
        {body}
suite = unittest.defaultTestLoader.loadTestsFromTestCase(Probe)
result = unittest.TextTestRunner().run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
'''
                completed = subprocess.run(
                    [sys.executable, "-c", code], cwd=REAL_RUNTIME.parent,
                    text=True, capture_output=True, timeout=10,
                )
                self.assertEqual(completed.returncode, 1, completed.stderr)
                self.assertIn("Test accessed real .runtime", completed.stderr)
                self.assertIn("FAILED (failures=1)", completed.stderr)

    def test_host_and_router_compatibility_exports_use_extracted_owners(self):
        self.assertIs(agent_helper.helper_command_router, helperctl)
        for name in ("command_result", "default_next_actions", "build_cli_parser", "build_cli_payload", "emit", "strip_internal", "make_request_id", "safe_request_id"):
            self.assertIs(getattr(helperctl, name), getattr(simon_protocol, name))
        for name in ("write_bridge_status", "append_bridge_history", "append_run_journal", "record_capture_route", "write_command_report", "write_command_failure_bundle", "load_planner_request", "write_planner_request"):
            self.assertIs(getattr(helperctl, name), getattr(simon_artifacts, name))


class ExtractedArtifactTests(RuntimeGuardedTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name) / "runtime"
        self.runtime.mkdir()
        self.paths = redirect_runtime(self, self.runtime)

    def test_all_path_aliases_follow_the_fixture(self):
        for module in (simon_paths, simon_artifacts, helperctl, agent_helper):
            for name, expected in self.paths.items():
                if hasattr(module, name):
                    self.assertEqual(getattr(module, name), expected)
        self.assertEqual(simon_paths.RUNTIME_DIR, self.runtime)

    def test_reports_and_failure_artifacts_remain_contained_and_readable(self):
        payload = {"value": 3}
        for writer in (helperctl.write_guided_action_report, helperctl.write_planner_step_report):
            path = writer("../../request?probe", payload)
            self.assertEqual(path.parent, self.paths["COMMAND_REPORT_DIR"])
            self.assertEqual(path.read_text(), json.dumps(payload, indent=2))

        source = self.runtime / "screen.png"
        source.write_bytes(b"fixture image bytes")
        result = helperctl.command_result("UNCERTAIN", "observe.screenshot", "Fixture failure", data={"raw_screenshot_path": str(source)})
        report = helperctl.write_command_report(result)
        self.assertEqual(json.loads(report.read_text()), result)
        bundle = helperctl.write_command_failure_bundle(result)
        self.assertTrue(bundle.is_relative_to(self.paths["FAILURE_DIAGNOSTICS_DIR"]))
        saved = json.loads(bundle.read_text())
        self.assertEqual(saved["result"], result)
        self.assertEqual(Path(saved["copied_artifacts"][0]).read_bytes(), source.read_bytes())

    def test_nested_request_tracking_restores_the_outer_observation_list(self):
        with helperctl.track_new_planner_requests() as outer:
            helperctl.write_planner_request({"request_id": "REQ-OUTER"})
            with helperctl.track_new_planner_requests() as inner:
                helperctl.write_planner_request({"request_id": "REQ-INNER"})
            helperctl.write_planner_request({"request_id": "REQ-OUTER-2"})
        self.assertEqual(outer, ["REQ-OUTER", "REQ-OUTER-2"])
        self.assertEqual(inner, ["REQ-INNER"])
        self.assertEqual(helperctl.load_planner_request("REQ-INNER"), {"request_id": "REQ-INNER"})
