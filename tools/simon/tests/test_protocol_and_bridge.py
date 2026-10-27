from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

from tests.runtime_support import RuntimeGuardedTestCase, redirect_runtime

import helperctl
import simon_protocol
import simon_artifacts
import simon_common
import simon_schema
import simon_images
from PIL import Image
from tests.test_candidates_and_guardrails import active_context, candidate


REQUEST_ID_PATTERN = re.compile(r"^REQ-\d{8}-\d{6}-\d{3}$")


class ProtocolContractTests(RuntimeGuardedTestCase):
    def test_command_result_has_exact_public_shape_and_preserves_empty_actions(self) -> None:
        data = {"value": 7}
        with mock.patch.object(simon_protocol, "now_iso", return_value="2026-07-20T12:34:56"):
            result = helperctl.command_result(
                "SUCCESS",
                "contract.test",
                "Contract result.",
                data=data,
                next_allowed_actions=[],
            )

        self.assertEqual(
            result,
            {
                "status": "SUCCESS",
                "command": "contract.test",
                "message": "Contract result.",
                "timestamp": "2026-07-20T12:34:56",
                "data": {"value": 7},
                "next_allowed_actions": [],
                "outcome": "action_done",
            },
        )
        self.assertEqual(
            set(result),
            {"status", "command", "message", "timestamp", "data", "next_allowed_actions", "outcome"},
        )

    def test_command_result_failure_keeps_schema_and_adds_recovery_hint(self) -> None:
        with mock.patch.object(simon_protocol, "now_iso", return_value="2026-07-20T12:34:56"):
            result = helperctl.command_result("UNCERTAIN", "contract.test", "Not verified.")

        self.assertEqual(
            set(result),
            {"status", "command", "message", "timestamp", "data", "next_allowed_actions", "outcome"},
        )
        self.assertEqual(result["status"], "UNCERTAIN")
        self.assertIn("planner_recovery_hint", result["data"])
        self.assertEqual(
            result["next_allowed_actions"],
            ["status.current", "recovery.capture_grid", "observe.screenshot", "helper.commands"],
        )

    def test_emit_exit_codes_and_internal_image_filter(self) -> None:
        expected_codes = {
            "SUCCESS": 0,
            "NEEDS_PLANNER": 0,
            "UNAVAILABLE": 0,
            "ERROR": 1,
            "FAILED": 1,
            "UNCERTAIN": 1,
        }
        for status, expected_code in expected_codes.items():
            with self.subTest(status=status):
                result = {
                    "status": status,
                    "command": "contract.test",
                    "message": "Result.",
                    "timestamp": "2026-07-20T12:34:56",
                    "data": {},
                    "next_allowed_actions": [],
                    "image": "internal-only",
                }
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = helperctl.emit(result)
                emitted = json.loads(output.getvalue())

                self.assertEqual(exit_code, expected_code)
                self.assertNotIn("image", emitted)
                self.assertEqual(emitted["status"], status)

    def test_string_and_key_chord_normalization(self) -> None:
        self.assertEqual(helperctl.normalize_string_list("  Alpha  "), ["Alpha"])
        self.assertEqual(helperctl.normalize_string_list("   "), [])
        self.assertEqual(helperctl.normalize_string_list([" Alpha ", "", 7, " Beta"]), ["Alpha", "7", "Beta"])
        self.assertEqual(helperctl.normalize_string_list(("Alpha", "Beta")), [])

        self.assertEqual(helperctl.parse_key_chord("ctrl+shift+s"), ["ctrl", "shift", "s"])
        self.assertEqual(helperctl.parse_key_chord(" Ctrl, Shift + S "), ["Ctrl", "Shift", "S"])
        self.assertEqual(helperctl.parse_key_chord(" + , + "), [])

    def test_advertised_command_list_is_unique(self) -> None:
        result = helperctl.cmd_helper_commands({})
        commands = result["data"]["commands"]

        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(len(commands), 37)
        self.assertIn("recovery.stop_screensaver", commands)
        self.assertEqual(len(commands), len(set(commands)))
        self.assertEqual(commands[0], "helper.commands")
        self.assertIn("planner.step", commands)
        self.assertIn("recovery.guided_grid_click", commands)
        self.assertIn("simon.press_next", commands)


class RequestIdContractTests(RuntimeGuardedTestCase):
    def assert_request_id(self, request_id: str) -> None:
        self.assertRegex(request_id, REQUEST_ID_PATTERN)

    def test_request_id_burst_is_unique_and_format_preserving(self) -> None:
        request_ids = [helperctl.make_request_id() for _ in range(1_000)]

        self.assertEqual(len(request_ids), len(set(request_ids)))
        for request_id in request_ids:
            self.assert_request_id(request_id)

    def test_request_ids_are_unique_across_threads(self) -> None:
        with ThreadPoolExecutor(max_workers=16) as executor:
            request_ids = list(executor.map(lambda _index: helperctl.make_request_id(), range(512)))

        self.assertEqual(len(request_ids), len(set(request_ids)))
        for request_id in request_ids:
            self.assert_request_id(request_id)

    def test_request_ids_advance_when_wall_clock_rolls_back(self) -> None:
        base = time.time_ns()
        rolled_back_values = [base, base - 2_000_000_000, base - 4_000_000_000]
        with mock.patch.object(helperctl.time, "time_ns", side_effect=rolled_back_values):
            request_ids = [helperctl.make_request_id() for _ in rolled_back_values]

        self.assertEqual(len(request_ids), len(set(request_ids)))
        self.assertEqual(request_ids, sorted(request_ids))
        for request_id in request_ids:
            self.assert_request_id(request_id)

    def test_safe_request_id_preserves_normal_ids_and_blocks_path_aliases(self) -> None:
        normal = "REQ-20260720-123456-789"
        self.assertEqual(helperctl.safe_request_id(normal), normal)

        slash_token = helperctl.safe_request_id("../folder/a/b")
        question_token = helperctl.safe_request_id("../folder/a?b")
        self.assertNotEqual(slash_token, question_token)
        for token in (slash_token, question_token):
            self.assertTrue(token)
            self.assertNotIn("/", token)
            self.assertNotIn("\\", token)
            self.assertNotIn(token, {".", ".."})
            self.assertRegex(token, r"-[0-9a-f]{10}$")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            for token in (slash_token, question_token):
                artifact = (root / f"{token}_raw.png").resolve()
                self.assertTrue(artifact.is_relative_to(root))


class RuntimePersistenceTests(RuntimeGuardedTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.runtime_dir = self.root / "runtime"
        self.runtime_dir.mkdir(parents=True)

        redirect_runtime(self, self.runtime_dir)

    def test_atomic_json_round_trip_replaces_existing_payload(self) -> None:
        path = self.runtime_dir / "nested" / "state.json"
        first = {"schema": "test-state.v1", "value": 1}
        second = {"schema": "test-state.v1", "value": 2, "items": ["a", "b"]}

        simon_common.write_json_atomic(path, first)
        self.assertEqual(simon_common.read_json(path), first)
        simon_common.write_json_atomic(path, second)
        self.assertEqual(simon_common.read_json(path), second)
        self.assertEqual(list(path.parent.glob(f"{path.name}.*.tmp")), [])

    def test_read_json_accepts_bom_and_rejects_malformed_content(self) -> None:
        bom_path = self.runtime_dir / "bom.json"
        malformed_path = self.runtime_dir / "malformed.json"
        bom_path.write_bytes(b"\xef\xbb\xbf{\"value\": 3}")
        malformed_path.write_text("{not-json", encoding="utf-8")

        self.assertEqual(simon_common.read_json(bom_path), {"value": 3})
        self.assertIsNone(simon_common.read_json(malformed_path))
        self.assertIsNone(simon_common.read_json(self.runtime_dir / "missing.json"))

    def test_bridge_lock_is_exclusive_and_releasable(self) -> None:
        lock_fd = helperctl.acquire_bridge_lock(timeout=0.0)
        self.assertIsNotNone(lock_fd)
        self.assertTrue(helperctl.COMMAND_BRIDGE_LOCK_FILE.exists())
        try:
            self.assertIsNone(helperctl.acquire_bridge_lock(timeout=0.0))
        finally:
            helperctl.release_bridge_lock(lock_fd)
        self.assertFalse(helperctl.COMMAND_BRIDGE_LOCK_FILE.exists())

    def test_bridge_status_and_history_records_keep_runtime_schemas(self) -> None:
        fixed_iso = "2026-07-20T12:34:56"
        with (
            mock.patch.object(simon_artifacts, "now_iso", return_value=fixed_iso),
            mock.patch.object(helperctl.time, "time", return_value=1_234.5),
        ):
            helperctl.write_bridge_status("idle", {"helper_pid": 321})
            helperctl.append_bridge_history("queued", "REQ-1", "status.current", {"client_pid": 111})
            helperctl.append_run_journal("command_issued", "REQ-1", "status.current", "codex", {"attempt": 1})

        status = simon_common.read_json(helperctl.HELPER_BRIDGE_STATUS_FILE)
        self.assertEqual(
            status,
            {
                "schema": "helper-bridge-status.v1",
                "state": "idle",
                "updated_at": fixed_iso,
                "updated_epoch": 1_234.5,
                "data": {"helper_pid": 321},
            },
        )

        bridge_entry = json.loads(helperctl.HELPER_BRIDGE_HISTORY_FILE.read_text(encoding="utf-8").strip())
        self.assertEqual(
            bridge_entry,
            {
                "event": "queued",
                "request_id": "REQ-1",
                "command": "status.current",
                "timestamp": fixed_iso,
                "epoch": 1_234.5,
                "data": {"client_pid": 111},
            },
        )
        self.assertEqual(helperctl.read_bridge_history_tail(1), [bridge_entry])

        journal_entry = json.loads(helperctl.HELPER_RUN_JOURNAL_FILE.read_text(encoding="utf-8").strip())
        self.assertEqual(journal_entry["schema"], "helper-run-journal.v1")
        self.assertEqual(
            set(journal_entry),
            {"schema", "event", "request_id", "command", "role", "timestamp", "epoch", "data"},
        )
        self.assertEqual(journal_entry["role"], "codex")

    def test_helper_runtime_status_has_stable_shape_for_fresh_idle_helper(self) -> None:
        lock_info = {"exists": True, "path": "lock", "content": {"pid": 321}}
        status_record = {
            "schema": "helper-bridge-status.v1",
            "state": "idle",
            "updated_epoch": 995.0,
            "data": {"helper_pid": 321},
        }
        with (
            mock.patch.object(helperctl, "read_lock_file_snapshot", return_value=lock_info),
            mock.patch.object(helperctl, "read_json", return_value=status_record),
            mock.patch.object(helperctl, "is_process_alive", return_value=True),
            mock.patch.object(helperctl.time, "time", return_value=1_000.0),
        ):
            runtime_status = helperctl.helper_runtime_status()

        self.assertEqual(
            runtime_status,
            {
                "running": True,
                "responsive": True,
                "pid": 321,
                "instance_lock": lock_info,
                "bridge_state": "idle",
                "bridge_status_pid": 321,
                "heartbeat_age_seconds": 5.0,
            },
        )

    def test_capture_route_history_and_tally_keep_runtime_schema(self) -> None:
        with (
            mock.patch.object(simon_artifacts, "now_iso", return_value="2026-07-20T12:34:56"),
            mock.patch.object(helperctl.time, "time", return_value=1_234.5),
            mock.patch.object(helperctl.time, "monotonic", return_value=10.25),
        ):
            helperctl.record_capture_route(
                "window_gdi",
                "target_window",
                "SUCCESS",
                started=10.0,
                attempts=[{"method": "window_gdi", "status": "SUCCESS"}],
            )

        event = json.loads(helperctl.HELPER_CAPTURE_ROUTE_HISTORY_FILE.read_text(encoding="utf-8").strip())
        self.assertEqual(event["method"], "window_gdi")
        self.assertEqual(event["elapsed_ms"], 250.0)
        self.assertEqual(
            set(event),
            {"timestamp", "epoch", "method", "target", "status", "elapsed_ms", "attempts"},
        )

        tally = simon_common.read_json(helperctl.HELPER_CAPTURE_ROUTE_TALLY_FILE)
        self.assertEqual(tally["schema"], "capture-route-tally.v1")
        self.assertEqual(tally["totals"], {"captures": 1, "success": 1})
        self.assertEqual(tally["by_method"]["window_gdi"]["count"], 1)
        self.assertEqual(tally["by_target"]["target_window"]["last_method"], "window_gdi")

    def test_planner_request_schema_persists_and_round_trips(self) -> None:
        request_id = "REQ-20260720-123456-789"
        capture_data = {
            "request_id": request_id,
            "target": "desktop",
            "raw_screenshot_path": str(self.runtime_dir / "raw.png"),
            "grid_screenshot_path": str(self.runtime_dir / "grid.png"),
            "grid": {"columns": 12, "rows": 8},
            "capture_timestamp": "2026-07-20T12:30:00",
        }
        args = {
            "title": "Simon Target",
            "failure_type": "CONTROL_NOT_FOUND",
            "reason": "Deterministic contract test.",
            "expected_result": "A safe next action is selected.",
            "verification": {"type": "manual_expected_result"},
        }
        with (
            mock.patch.object(helperctl, "now_iso", return_value="2026-07-20T12:34:56"),
            mock.patch.object(helperctl, "load_planner_working_memory", return_value={}),
        ):
            request = helperctl.create_planner_request(
                capture_data,
                args,
                failed_command="planner.step",
                question="What is the next safe action?",
            )

        self.assertEqual(request["schema"], "planner-request.v1")
        self.assertEqual(request["request_id"], request_id)
        self.assertEqual(request["status"], "NEEDS_PLANNER")
        self.assertEqual(request["failure_type"], "CONTROL_NOT_FOUND")
        self.assertEqual(request["question_for_planner"], "What is the next safe action?")

        request_path = Path(request["planner_request_path"])
        self.assertEqual(request_path.name, f"{request_id}_planner_request.json")
        self.assertTrue(request_path.exists())
        self.assertEqual(helperctl.load_planner_request(request_id), request)

    def test_planner_request_paths_cannot_escape_or_alias(self) -> None:
        first = helperctl.planner_request_path("../planner/a/b").resolve()
        second = helperctl.planner_request_path("../planner/a?b").resolve()
        root = helperctl.PLANNER_REQUEST_DIR.resolve()

        self.assertTrue(first.is_relative_to(root))
        self.assertTrue(second.is_relative_to(root))
        self.assertNotEqual(first, second)

    def test_lean_fixture_byte_budgets_and_full_evidence_fallback(self) -> None:
        context = active_context(*(candidate(i, title=f"Fixture button {i}") for i in range(1, 19)))
        menu = helperctl.build_action_menu(context)
        common = {"active_context": context, "action_menu": menu, "working_memory": {"last_action": {"command": "input.hotkey", "status": "SUCCESS"}}}
        capture = {**common, "request_id": "REQ-FIXTURE", "grid": {"columns": 12, "rows": 8},
                   "target_window": context["active_window"], "foreground_window": context["active_window"],
                   "raw_screenshot_path": "native-private.png", "grid_screenshot_path": "grid-private.png"}
        capture["planner_request"] = {**capture, "planner_request_path": "request.json"}
        step = helperctl.command_result("SUCCESS", "input.hotkey", "Fixture action", data=common)
        fixtures = {
            "recovery.capture_grid": ("NEEDS_PLANNER", capture),
            "input.action_sequence": ("SUCCESS", {**common, "action_count": 3, "results": [{"index": i, "action": "hotkey", "result": step} for i in range(3)]}),
            "verify.file_state": ("SUCCESS", {**common, "path": "fixture.txt", "exists": True, "should_exist": True, "size_bytes": 12}),
        }
        sizes = {}
        for command, (status, data) in fixtures.items():
            result = helperctl.command_result(status, command, "Fixture result", data=data)
            full = simon_protocol.result_view(result, "full")
            minimal = simon_protocol.result_view(result, "minimal")
            full_bytes = len(json.dumps(full, separators=(",", ":")).encode())
            minimal_bytes = len(json.dumps(minimal, separators=(",", ":")).encode())
            sizes[command] = {"full_bytes": full_bytes, "minimal_bytes": minimal_bytes, "image_bytes": 0}
            self.assertLessEqual(minimal_bytes, full_bytes * 0.15, sizes)
            self.assertLessEqual(minimal_bytes, 5000 if command == "recovery.capture_grid" else 3000)
            self.assertEqual(full, result)
            self.assertEqual(minimal["status"], status)
            if command == "recovery.capture_grid":
                self.assertEqual(minimal["data"]["planner_request"]["context_ref"], "#/data/context")
                self.assertNotIn("raw_screenshot_path", minimal["data"])
                for item in minimal["data"]["context"]["candidates"]:
                    self.assertIsInstance(item["center"], dict)
            ids = {item["id"] for item in minimal.get("data", {}).get("context", {}).get("candidates", [])}
            for key in ("likely_click_targets", "likely_text_fields", "likely_primary_buttons"):
                self.assertTrue(set(minimal.get("data", {}).get("context", {}).get(key, [])).issubset(ids))
        print("C1 fixture bytes:", json.dumps(sizes, sort_keys=True))

    def test_unknown_arguments_warn_or_reject_before_handler(self) -> None:
        handler = mock.Mock(return_value=helperctl.command_result("SUCCESS", "recovery.guided_grid_crop", "Fixture crop"))
        with mock.patch.object(helperctl, "build_active_context_packet", return_value={}):
            result = helperctl.dispatch("recovery.guided_grid_crop", {"columns": 4}, handler_override=handler)
            self.assertIn("crop_columns", result["warnings"][0])
            self.assertEqual(handler.call_count, 1)
            handler.reset_mock()
            result = helperctl.dispatch("recovery.guided_grid_crop", {"columns": 4, "strict_args": True}, handler_override=handler)
            self.assertEqual(result["outcome"], "blocked")
            handler.assert_not_called()
            result = helperctl.dispatch("input.click_point", {"x": float("nan"), "y": 3}, handler_override=handler)
            self.assertEqual(result["status"], "ERROR")
            handler.assert_not_called()
            result = helperctl.dispatch("helper.start", {"allow_elevation": "true"}, handler_override=handler)
            self.assertEqual(result["status"], "ERROR")
            handler.assert_not_called()

    def test_elevation_requires_explicit_opt_in_without_any_launch(self) -> None:
        with mock.patch.object(helperctl, "collect_environment_diagnostics", side_effect=AssertionError("must not launch/probe")):
            for command, args in ((helperctl.cmd_helper_start, {"mode": "elevated"}), (helperctl.cmd_helper_start_elevated, {})):
                result = command(args)
                self.assertEqual(result["outcome"], "blocked")
                self.assertEqual(result["data"]["failure_type"], "ELEVATION_OPT_IN_REQUIRED")

    def test_validation_preserves_legacy_arguments_and_rejects_malformed_nested_input(self) -> None:
        parsed = helperctl.build_cli_parser().parse_args(["recovery.guided_grid_crop", "--max-age-seconds", "30"])
        self.assertEqual(parsed.max_age_seconds, 30)
        warnings, errors = simon_schema.validate_arguments("planner.step", {
            "action": "wait", "seconds": 0.1, "based_on_request_id": "REQ-OLD", "protocol_version": 2})
        self.assertEqual((warnings, errors), ([], []))
        handler = mock.Mock()
        for args in ([], {"include_images": [{}]}, {"step": {"action": "click_point", "x": float("nan")}},
                     {"actions": [{"action": "wait"}, {"action": "type_text", "allow_path_text": "false"}]}):
            command = "input.action_sequence" if isinstance(args, dict) and "actions" in args else "planner.step"
            result = helperctl.dispatch(command, args, handler_override=handler)
            self.assertEqual(result["status"], "ERROR")
            self.assertEqual(result["failure_report"], result["details_path"])
        handler.assert_not_called()

    def test_outcomes_distinguish_observation_block_and_operator_decision(self) -> None:
        cases = [("NEEDS_PLANNER", "recovery.capture_grid", {}, "observation_ready"),
                 ("NEEDS_PLANNER", "recovery.capture_grid", {"failure_type": "STALE_SCREENSHOT"}, "blocked"),
                 ("NEEDS_PLANNER", "recovery.guided_grid_click", {}, "needs_decision"),
                 ("UNCERTAIN", "input.click_point", {}, "blocked"),
                 ("SUCCESS", "input.hotkey", {}, "action_done"),
                 ("NEEDS_PLANNER", "planner.step", {"action_result": {"status": "NEEDS_PLANNER", "command": "recovery.guided_grid_click", "data": {"failure_type": "STALE_SCREENSHOT"}}}, "blocked"),
                 ("SUCCESS", "input.action_sequence", {"results": [{"result": {"status": "NEEDS_PLANNER", "command": "input.hotkey"}}]}, "needs_decision")]
        for status, command, data, outcome in cases:
            self.assertEqual(helperctl.command_result(status, command, "Fixture", data=data)["outcome"], outcome)

    def test_nested_verification_flags_are_validated_before_any_handler(self) -> None:
        handler = mock.Mock()
        for payload in (
            {"verification": {"type": "window_state", "focus_if_needed": "false"}},
            {"step": {"action": "guided_grid_click", "verification": {"type": "window_state", "require_foreground": "false"}}},
            {"verification": {"type": "simon_progress_increment", "before_index": "0"}},
        ):
            result = helperctl.dispatch("planner.step", payload, handler_override=handler)
            self.assertEqual(result["status"], "ERROR", payload)
        handler.assert_not_called()
        warnings, errors = simon_schema.validate_arguments("recovery.guided_grid_click", {
            "protocol_version": 2, "verification": {"type": "window_state", "focus_if_needed": False,
                                                       "require_foreground": False, "title_contains": ["Notepad"]}})
        self.assertEqual((warnings, errors), ([], []))
        warnings, errors = simon_schema.validate_arguments("recovery.guided_grid_click", {
            "protocol_version": 2, "verification": {"type": "window_state", "focus_if_neededd": True}})
        self.assertFalse(warnings)
        self.assertTrue(any("focus_if_needed" in error for error in errors))

    def test_compact_candidates_preserve_disabled_and_hidden_state(self) -> None:
        context = active_context(candidate(1, enabled=False), candidate(2, visible=False), candidate(3))
        result = helperctl.command_result("NEEDS_PLANNER", "recovery.capture_grid", "Fixture", data={
            "active_context": context, "action_menu": helperctl.build_action_menu(context)})
        for detail in ("minimal", "standard"):
            rows = {row["id"]: row for row in simon_protocol.result_view(result, detail)["data"]["context"]["candidates"]}
            self.assertFalse(rows[1]["enabled"])
            self.assertFalse(rows[2]["visible"])
            self.assertEqual(rows[1]["actions"], [])
            self.assertEqual(rows[2]["actions"], [])
            self.assertIn("input.click_candidate", rows[3]["actions"])

    def test_planner_image_is_single_resized_label_target_and_latest_observation(self) -> None:
        helperctl.COMMAND_SCREENSHOT_DIR.mkdir()
        before = helperctl.COMMAND_SCREENSHOT_DIR / "before_grid.png"
        after = helperctl.COMMAND_SCREENSHOT_DIR / "after_grid.png"
        Image.new("RGB", (2000, 1000), "red").save(before)
        Image.new("RGB", (2400, 1200), "blue").save(after)
        result = {"data": {"before": {"grid_screenshot_path": str(before), "request_id": "REQ-BEFORE"},
                           "next_observation": {"data": {"grid_screenshot_path": str(after), "raw_screenshot_path": str(after), "request_id": "REQ-AFTER"}}}}
        image = simon_images.planner_image(result, {})
        self.assertEqual((image["width"], image["height"]), (1568, 784))
        self.assertEqual(image["request_id"], "REQ-AFTER")
        self.assertEqual(image["targeting"], "labels_only")
        self.assertNotIn("extra_images", image)
        with Image.open(after) as original:
            self.assertEqual(original.size, (2400, 1200))
        image = simon_images.planner_image(result, {"include_images": ["raw"]})
        self.assertEqual(len(image["extra_images"]), 1)
        followup = helperctl.build_follow_up_capture_args({"target": "desktop", "detail": "full", "include_images": ["candidates"]}, "wait", {}, "REQ-NEXT")
        self.assertEqual(followup["include_images"], ["candidates"])
        self.assertEqual(followup["detail"], "full")
        with mock.patch.object(helperctl, "capture_target", return_value={"status": "UNCERTAIN"}) as capture:
            helperctl.capture_guided_follow_up_observation({"include_images": ["candidates"], "detail": "full"}, {}, "Fixture", 0, source_status="SUCCESS")
        self.assertEqual(capture.call_args.args[0]["include_images"], ["candidates"])
        self.assertEqual(capture.call_args.args[0]["detail"], "full")

    def test_retention_preserves_recent_active_shared_and_unmanaged_files(self) -> None:
        from simon_paths import ARTIFACT_MANIFEST_FILE
        reports = helperctl.COMMAND_REPORT_DIR
        reports.mkdir()
        old, shared, recent = [reports / name for name in ("old.json", "shared.json", "recent.json")]
        unmanaged = self.runtime_dir / "Storyline-output.txt"
        for path in (old, shared, recent, unmanaged):
            path.write_text("fixture")
        simon_common.write_json_atomic(ARTIFACT_MANIFEST_FILE, {"schema": "simon-artifacts.v1", "groups": [
            {"created_epoch": time.time() - 9 * 86400, "request_id": "REQ-OLD", "paths": [str(old), str(shared), str(unmanaged)]},
            {"created_epoch": time.time() - 9 * 86400, "request_id": "REQ-ACTIVE", "paths": [str(shared)]},
            {"created_epoch": time.time(), "paths": [str(recent)]}]})
        helperctl.save_planner_working_memory({"last_observation_request_id": "REQ-ACTIVE"})
        preview = simon_artifacts.cleanup_runtime(dry_run=True, max_groups=0, max_bytes=0)
        self.assertEqual(preview["eligible_files"], 1)
        self.assertTrue(old.exists())
        result = simon_artifacts.cleanup_runtime(dry_run=False, max_groups=0, max_bytes=0)
        self.assertEqual(result["deleted_files"], 1)
        self.assertFalse(old.exists())
        self.assertTrue(shared.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(unmanaged.exists())

    def test_helper_capture_handler_uses_one_postprocessor_and_new_request_memory(self) -> None:
        context = {"active_window": {"hwnd": 7, "title": "Fixture"}, "child_candidates": []}
        def capture(args):
            request = helperctl.create_planner_request({"request_id": "REQ-HOST", "active_context": context}, args, "recovery.capture_grid")
            return helperctl.command_result("NEEDS_PLANNER", "recovery.capture_grid", "Fixture host capture", data={"active_context": context, "request_id": "REQ-HOST", "planner_request": request})
        with mock.patch.object(helperctl, "build_active_context_packet", side_effect=AssertionError("context must be reused")):
            result = helperctl.dispatch("recovery.capture_grid", {}, handler_override=capture)
        self.assertEqual(result["outcome"], "observation_ready")
        self.assertEqual(helperctl.load_planner_working_memory()["last_observation_request_id"], "REQ-HOST")
        self.assertTrue(Path(result["details_path"]).is_file())
        self.assertIn("recovery.capture_grid", helperctl.COMMAND_LOG_FILE.read_text())

    def test_retention_protects_nested_current_observation_and_native_evidence(self) -> None:
        reports = helperctl.COMMAND_REPORT_DIR
        reports.mkdir()
        details = reports / "step.json"
        details.write_text("fixture")
        request = helperctl.create_planner_request({"request_id": "REQ-NESTED"}, {}, "planner.step")
        result = {"data": {"next_observation": {"request_id": "REQ-NESTED", "planner_request": request}}}
        with mock.patch.object(simon_artifacts.time, "time", return_value=time.time() - 9 * 86400):
            simon_artifacts.register_result_artifacts(result, details, [Path(request["planner_request_path"])])
        helperctl.save_planner_working_memory({"last_observation_request_id": "REQ-NESTED"})
        cleanup = simon_artifacts.cleanup_runtime(dry_run=False, max_groups=0, max_bytes=0)
        self.assertEqual(cleanup["deleted_files"], 0)
        self.assertTrue(details.exists())
        self.assertTrue(Path(request["planner_request_path"]).exists())

    def test_retention_registers_generated_intermediate_files_but_not_arbitrary_references(self) -> None:
        helperctl.COMMAND_SCREENSHOT_DIR.mkdir()
        generated = helperctl.COMMAND_SCREENSHOT_DIR / "intermediate.png"
        unregistered = helperctl.COMMAND_SCREENSHOT_DIR / "owner-evidence.png"
        unregistered.write_bytes(b"owner evidence")
        def handler(args):
            generated.write_bytes(b"discarded intermediate capture")
            simon_artifacts.record_generated_artifact(generated)
            return helperctl.command_result("SUCCESS", "observe.screenshot", "Fixture", data={"file_path": str(unregistered)})
        with mock.patch.object(helperctl, "build_active_context_packet", return_value={}):
            with mock.patch.object(simon_artifacts.time, "time", return_value=time.time() - 9 * 86400):
                helperctl.dispatch("observe.screenshot", {}, handler_override=handler)
        cleanup = simon_artifacts.cleanup_runtime(dry_run=False, max_groups=0, max_bytes=0)
        self.assertGreaterEqual(cleanup["deleted_files"], 2)
        self.assertFalse(generated.exists())
        self.assertTrue(unregistered.exists())

    def test_artifact_manifest_updates_are_serialized_between_processes(self) -> None:
        # Child processes install the same guard and redirect before any writes.
        script = """
import sys
from pathlib import Path
from tests.runtime_support import RuntimeGuardedTestCase, redirect_runtime
import simon_artifacts
case = RuntimeGuardedTestCase()
redirect_runtime(case, Path(sys.argv[1]))
try:
    for i in range(12):
        path = simon_artifacts.COMMAND_REPORT_DIR / (sys.argv[2] + str(i) + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture')
        simon_artifacts.register_result_artifacts({'data': {}}, path)
finally:
    case.doCleanups()
"""
        processes = [subprocess.Popen([sys.executable, "-c", script, str(self.runtime_dir), prefix],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for prefix in ("one", "two")]
        try:
            for process in processes:
                stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, stdout + stderr)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        manifest = simon_common.read_json(simon_artifacts.ARTIFACT_MANIFEST_FILE)
        self.assertEqual(len(manifest["groups"]), 24)
        self.assertEqual(len({path for row in manifest["groups"] for path in row["paths"]}), 24)

    def test_retention_cannot_delete_dependency_during_registration(self) -> None:
        reports = helperctl.COMMAND_REPORT_DIR
        reports.mkdir()
        old, shared, new = [reports / name for name in ("old.json", "shared.json", "new.json")]
        for path in (old, shared, new):
            path.write_text("fixture")
        with mock.patch.object(simon_artifacts.time, "time", return_value=time.time() - 9 * 86400):
            simon_artifacts.register_result_artifacts({"data": {}}, old, [shared])
        original_lock = simon_artifacts.artifact_manifest_lock
        cleanup_ran = False

        @contextmanager
        def cleanup_at_first_unlock():
            # Deterministically schedule a second client's cleanup at the first
            # unlocked point, before registration can reacquire the manifest.
            nonlocal cleanup_ran
            with original_lock():
                yield
            if not cleanup_ran:
                cleanup_ran = True
                simon_artifacts.cleanup_runtime(dry_run=False, max_groups=0, max_bytes=0)

        with mock.patch.object(simon_artifacts, "artifact_manifest_lock", cleanup_at_first_unlock):
            simon_artifacts.register_result_artifacts({"data": {"dependency_path": str(shared)}}, new)
        self.assertTrue(cleanup_ran)
        self.assertTrue(shared.exists())
        self.assertFalse(old.exists())
        manifest = simon_common.read_json(simon_artifacts.ARTIFACT_MANIFEST_FILE)
        self.assertTrue(any(str(shared.resolve()) in row["paths"] and str(new.resolve()) in row["paths"]
                            for row in manifest["groups"]))

    def test_command_schema_and_generated_reference_cover_the_same_commands(self) -> None:
        for name, spec in simon_schema.COMMANDS.items():
            self.assertTrue(callable(getattr(helperctl, spec["handler"])), name)
            self.assertIn(f"`{name}`", simon_schema.command_reference())
            for key in spec["args"]:
                self.assertIn(key, simon_schema.ARGUMENTS)
        self.assertEqual(set(helperctl.cmd_helper_commands({})["data"]["commands"]), set(simon_schema.COMMANDS))
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
        generated = readme.split("<!-- simon:commands:start -->\n", 1)[1].split("<!-- simon:commands:end -->", 1)[0]
        self.assertEqual(generated, simon_schema.command_reference())

    def test_session_end_hook_exits_only_idle_responsive_helper(self) -> None:
        from tools import simon_session_end
        event = {"hook_event_name": "SessionEnd", "cwd": str(simon_session_end.PROJECT_DIR)}
        with mock.patch.object(helperctl, "helper_runtime_status") as status, mock.patch.object(helperctl, "send_to_helper_app", return_value={"status": "SUCCESS"}) as send:
            status.return_value = {"responsive": False, "bridge_state": "executing"}
            self.assertEqual(simon_session_end.main(event), 0)
            send.assert_not_called()
            status.return_value = {"responsive": True, "bridge_state": "idle"}
            self.assertEqual(simon_session_end.main(event), 0)
            send.assert_called_once_with("helper.exit", {}, timeout=2.0)


if __name__ == "__main__":
    unittest.main()
