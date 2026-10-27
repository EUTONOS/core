import io
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest import mock

from eutonos import workflow_cli


class WorkflowCliTests(unittest.TestCase):
    def test_orientation_dispatch_and_private_guard_before_request_access(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request = root / "request.json"
            request.write_text('{"mode":"resume"}', encoding="utf-8")
            with mock.patch.object(workflow_cli.orientation, "orient", return_value={"outcome": "orientation"}) as orient:
                code, result = self.invoke(["orient", "--root", str(root), "--request", str(request)])
            self.assertEqual(code, 0)
            orient.assert_called_once_with(str(root), {"mode": "resume"})
            (root / "eutonos.personal.json").write_text("{}")
            with mock.patch.object(workflow_cli, "_load", side_effect=AssertionError("must not load")), \
                    mock.patch.object(workflow_cli.orientation, "orient") as orient:
                code, result = self.invoke(["orient", "--root", str(root), "--request", str(root / "secret-missing")])
            self.assertEqual((code, result["code"]), (2, "denied"))
            orient.assert_not_called()

    def test_orientation_checkout_and_managed_wrappers_select_same_cli(self):
        wrapper = Path(__file__).resolve().parents[1] / "tools/workflow.py"
        with mock.patch.object(workflow_cli.orientation, "guard"), \
                mock.patch.object(workflow_cli.orientation, "orient", return_value={"outcome": "orientation"}) as orient, \
                mock.patch("sys.stdout", io.StringIO()), \
                mock.patch.object(sys, "argv", [str(wrapper), "orient"]), \
                mock.patch.object(sys, "path", sys.path.copy()), self.assertRaises(SystemExit) as stopped:
            runpy.run_path(str(wrapper), run_name="__main__")
        self.assertEqual(stopped.exception.code, 0)
        orient.assert_called_once_with(str(wrapper.parent.parent), {})
        managed = wrapper.parent / "eutonos_project.py"
        with mock.patch.object(sys, "argv", [str(managed), "workflow", "orient", "--root", "consumer"]), \
                mock.patch.object(Path, "is_file", return_value=True), mock.patch("subprocess.call", return_value=0) as call, \
                self.assertRaises(SystemExit):
            runpy.run_path(str(managed), run_name="__main__")
        self.assertEqual(call.call_args.args[0][1:], ["-m", "eutonos.workflow_cli", "orient", "--root", "consumer"])

    def test_orientation_rejects_duplicate_keys_and_nonfinite_json_before_composition(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            request = root / "request.json"
            for text, expected in (('{"mode":"cold","mode":"resume"}', "ambiguous"), ('{"budget_bytes":NaN}', "invalid")):
                with self.subTest(text=text), mock.patch.object(workflow_cli.orientation, "orient") as orient:
                    request.write_text(text, encoding="utf-8")
                    code, result = self.invoke(["orient", "--root", str(root), "--request", str(request)])
                    self.assertEqual((code, result["code"]), (2, expected))
                    orient.assert_not_called()

    def invoke(self, argv):
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            code = workflow_cli.main(argv)
        return code, json.loads(output.getvalue())

    def test_index_is_same_registry_as_catalog(self):
        self.assertEqual(self.invoke(["index", "memory"]), self.invoke(["catalog", "memory"]))

    def test_pickup_json_preserves_unicode_on_cp1252_stdout(self):
        value = {"outcome": "pickup", "title": "NAV\u200b update \U0001f50e"}
        output = io.BytesIO()
        console = io.TextIOWrapper(output, encoding="cp1252", errors="strict")
        with mock.patch("sys.stdout", console), mock.patch.object(workflow_cli, "pickup", return_value=value):
            self.assertEqual(workflow_cli.main(["pickup"]), 0)
        console.flush()
        self.assertEqual(json.loads(output.getvalue().decode("cp1252")), value)
        console.detach()

    def test_source_wrapper_defaults_to_its_checkout_and_preserves_explicit_roots(self):
        wrapper = Path(__file__).resolve().parents[1] / "tools" / "workflow.py"
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory(prefix="workflow-caller-") as caller:
            try:
                os.chdir(caller)
                for arguments, expected in (([], str(wrapper.parent.parent)),
                                            (["--root", "relative"], "relative"),
                                            (["--root=relative"], "relative")):
                    with self.subTest(arguments=arguments), \
                            mock.patch.object(workflow_cli, "pickup", return_value={"outcome": "pickup"}) as pickup, \
                            mock.patch("sys.stdout", io.StringIO()), \
                            mock.patch.object(sys, "argv", [str(wrapper), "pickup", *arguments]), \
                            mock.patch.object(sys, "path", sys.path.copy()), \
                            self.assertRaises(SystemExit) as stopped:
                        runpy.run_path(str(wrapper), run_name="__main__")
                    self.assertEqual(stopped.exception.code, 0)
                    pickup.assert_called_once_with(expected)
            finally:
                os.chdir(original_cwd)

    def test_expert_explicit_root_is_not_overridden_by_wrapper_default(self):
        for root_args in (["--root", "relative"], ["--root=relative"]):
            with mock.patch.object(workflow_cli.importlib, "import_module") as imported:
                imported.return_value.main.return_value = 0
                self.assertEqual(workflow_cli.main(["expert", "nav", "status", *root_args],
                                                   default_root="wrapper-root"), 0)
            imported.return_value.main.assert_called_once_with(["status", *root_args])

    def test_installed_wrapper_preserves_explicit_root_or_defaults_to_its_checkout(self):
        wrapper = Path(__file__).resolve().parents[1] / "tools" / "eutonos_project.py"
        for arguments in ([], ["--root", "relative"], ["--root=relative"]):
            with self.subTest(arguments=arguments), \
                    mock.patch.object(sys, "argv", [str(wrapper), "nav", "status", *arguments]), \
                    mock.patch.object(Path, "is_file", return_value=True), \
                    mock.patch("subprocess.call", return_value=0) as call, \
                    self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(wrapper), run_name="__main__")
            self.assertEqual(stopped.exception.code, 0)
            expected = arguments or ["--root", str(wrapper.parent.parent)]
            self.assertEqual(call.call_args.args[0][1:], ["-m", "eutonos.nav_cli", "status", *expected])

    def test_nav_guide_orders_analysis_before_guarded_update_and_readback(self):
        code, result = self.invoke(["guide", "nav"])
        self.assertEqual(code, 0)
        self.assertEqual(result["execution"], "not-started")
        command = result["command"]
        steps = command["steps"]
        sequence = ("nav status", "nav maintenance", "nav history-plan", "host status", "host start",
                    "host reserve", "nav start", "nav next", "nav submit", "host closeout", "Readback:")
        self.assertEqual(len(steps), len(sequence))
        for step, operation in zip(steps, sequence):
            self.assertIn(operation, step)
        self.assertTrue(all(step.startswith("Read-only:") for step in steps[:4]))
        self.assertIn("host resume", steps[4])
        self.assertIn("generation returned by start or resume", steps[4])
        self.assertIn("every history-plan path", steps[5])
        self.assertIn("attribution gap", steps[9])
        self.assertIn("Other worktrees and branches are not updated", command["task_intent"])

    def test_help_and_guidance_are_read_only_and_never_read_stdin(self):
        with mock.patch("sys.stdin") as stdin, \
                mock.patch.object(workflow_cli, "prepare_closeout") as prepare, \
                mock.patch.object(workflow_cli, "execute_closeout") as execute, \
                mock.patch.object(workflow_cli, "pickup") as pickup, \
                mock.patch.object(workflow_cli, "record") as record:
            stdin.read.side_effect = AssertionError("Help must not read stdin")
            stdin.readline.side_effect = AssertionError("Help must not prompt")
            for argv in (["help"], ["describe", "closeout"], ["guide", "closeout"],
                         ["explain", "RAM"], ["capabilities"]):
                code, result = self.invoke(argv)
                self.assertEqual(code, 0, result)
                if argv[0] == "guide":
                    self.assertEqual(result["execution"], "not-started")
            for operation in (prepare, execute, pickup, record):
                operation.assert_not_called()

    def test_unknown_command_or_term_is_structured_and_nonzero(self):
        for argv in (["describe", "not-a-command"], ["guide", "not-a-command"],
                     ["explain", "not-a-term"]):
            code, result = self.invoke(argv)
            self.assertEqual(code, 2)
            self.assertEqual(result["outcome"], "error")

    def test_direct_prepare_does_not_require_help_or_guidance(self):
        with mock.patch.object(workflow_cli, "prepare_closeout", return_value={"outcome": "prepared"}) as prepare, \
                mock.patch.object(workflow_cli, "help_topics") as help_topics, \
                mock.patch.object(workflow_cli, "describe") as describe:
            code, _ = self.invoke(["prepare", "closeout"])
        self.assertEqual(code, 0)
        prepare.assert_called_once()
        help_topics.assert_not_called()
        describe.assert_not_called()

    def test_no_arguments_returns_compact_catalog(self):
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            self.assertEqual(workflow_cli.main([]), 0)
        self.assertEqual(json.loads(output.getvalue())["outcome"], "catalog")

    def test_menu_refuses_noninteractive_input_without_waiting(self):
        output = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("")), mock.patch("sys.stdout", output):
            self.assertEqual(workflow_cli.main(["menu"]), 2)
        self.assertEqual(json.loads(output.getvalue())["code"], "noninteractive")

    def test_catalog_accepts_wrapper_root_after_subcommand(self):
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            code = workflow_cli.main(["catalog", "memory", "--root", "."])
        self.assertEqual(code, 0)
        value = json.loads(output.getvalue())
        self.assertEqual(value["outcome"], "catalog")
        self.assertTrue(value["commands"])
        self.assertEqual(len(value["capability_fingerprint"]), 64)

    def test_prepare_reports_all_missing_context_as_json(self):
        source = io.StringIO(json.dumps({"what": "x", "why": "y", "checks": [],
                                        "unresolved": [], "changed_paths": []}))
        output = io.StringIO()
        with mock.patch("sys.stdin", source), mock.patch("sys.stdout", output):
            code = workflow_cli.main(["prepare", "closeout", "--root", "."])
        self.assertEqual(code, 2)
        value = json.loads(output.getvalue())
        self.assertEqual({item["name"] for item in value["missing_context"]},
                         {"host", "actor", "session_id"})

    def test_non_object_closeout_input_is_structured_and_cannot_start_a_write(self):
        for supplied in (["malformed"], 17):
            with self.subTest(supplied=supplied), mock.patch("sys.stdin", io.StringIO(json.dumps(supplied))), \
                    mock.patch("eutonos.write_retry.submit_batch") as submit:
                code, value = self.invoke(["prepare", "closeout", "--input", "-"])
            self.assertEqual(code, 2)
            self.assertEqual(value["outcome"], "incomplete")
            self.assertEqual(value["phase"], "input")
            self.assertEqual(value["memory_assessment"]["outcome"], "incomplete")
            submit.assert_not_called()

    def test_delivery_failure_keeps_native_result_and_requires_phase_recovery(self):
        for state in ("unavailable", "blocked", "failed", "commit-uncertain", "push-uncertain", "paused"):
            with self.subTest(state=state):
                result = {"outcome": "completed", "organization": {"outcome": "completed"},
                          "memory": {"outcome": "completed", "current_verified": True},
                          "git_delivery": {"state": state, "error": "Inspect original delivery"}}
                attention = workflow_cli._closeout_attention(result)
                self.assertEqual([issue["phase"] for issue in attention["issues"]], ["git-delivery"])
                result["closeout_attention"] = attention
                receipt = workflow_cli._closeout_receipt(result)
                self.assertEqual(receipt["state"], "attention-required")
                self.assertFalse(receipt["may_finish_turn"])
                self.assertEqual(result["organization"]["outcome"], "completed")
                self.assertTrue(result["memory"]["current_verified"])

    def test_nested_ram_queue_is_submitted_while_native_filing_stays_landed(self):
        result = {"outcome": "completed", "organization": {"outcome": "completed"},
                  "lifecycle": [{"outcome": "complete", "state": "complete", "request_id": "filed-request",
                                 "result": {"readback": "verified", "complete": True}}],
                  "memory": {"outcome": "deferred", "reason": "write-owner-busy",
                             "write_retry": {"outcome": "queued", "request_id": "ram-request"}}}
        result["closeout_attention"] = workflow_cli._closeout_attention(result)
        self.assertFalse(result["closeout_attention"]["required"])
        receipt = workflow_cli._closeout_receipt(result)
        self.assertEqual(receipt["state"], "submitted")
        self.assertIn("Native work filing landed", receipt["safe_claim"])
        self.assertEqual(receipt["request_ids"], ["filed-request", "ram-request"])
        self.assertEqual(result["organization"]["outcome"], "completed")

    def test_diagnose_uses_local_recorded_events_without_input(self):
        from eutonos.workflow_diagnostics import record
        with tempfile.TemporaryDirectory(prefix="workflow-cli-diagnostic-") as root:
            result = {"profile": "eutonos.tokenslang.workflow.v1", "outcome": "error", "command": "closeout",
                      "operation": {"attempt": "attempt"}, "result": {"outcome": "error", "code": "denied"}}
            record(root, result)
            output = io.StringIO()
            with mock.patch("sys.stdout", output):
                code = workflow_cli.main(["diagnose", "--root", root])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["last_failed_phase"], "ram-capture")


if __name__ == "__main__":
    unittest.main()
