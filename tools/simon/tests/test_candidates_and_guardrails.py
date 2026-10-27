from __future__ import annotations

import tempfile
import ctypes
import hashlib
import os
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.runtime_support import RuntimeGuardedTestCase, redirect_runtime

import helperctl
import simon_verification as effects


def bounds(left: int = 0, top: int = 0, right: int = 400, bottom: int = 300) -> dict[str, int]:
    return {
        "left": left,
        "top": top,
        "right": right,
        "bottom": bottom,
        "width": max(0, right - left),
        "height": max(0, bottom - top),
    }


def candidate(
    candidate_id: int,
    *,
    title: str = "",
    class_name: str = "Button",
    kind: str = "button",
    visible: bool = True,
    enabled: bool = True,
    candidate_bounds: dict[str, int] | None = None,
    hwnd: int | None = None,
) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "hwnd": candidate_id if hwnd is None else hwnd,
        "title": title,
        "class_name": class_name,
        "kind": kind,
        "visible": visible,
        "enabled": enabled,
        "bounds": candidate_bounds or bounds(20, 20 + candidate_id * 30, 180, 45 + candidate_id * 30),
    }


def active_context(*children: dict[str, object]) -> dict[str, object]:
    return {
        "active_window": {
            "hwnd": 500,
            "title": "Example App",
            "bounds": bounds(),
            "vm_boundary": {"detected": False},
        },
        "current_dialog": None,
        "dialog_open": False,
        "focused_control": None,
        "child_candidates": list(children),
        "vm_boundary": {"detected": False},
    }


def dialog_context(*, vm_boundary: dict[str, object] | None = None) -> dict[str, object]:
    boundary = vm_boundary or {"detected": False}
    dialog = {
        "hwnd": 700,
        "title": "Save As",
        "bounds": bounds(100, 100, 500, 400),
        "vm_boundary": boundary,
    }
    return {
        "active_window": dialog,
        "current_dialog": dialog,
        "dialog_open": True,
        "focused_control": {
            "kind": "text_field",
            "bounds": bounds(130, 180, 450, 210),
        },
        "child_candidates": [],
        "vm_boundary": boundary,
    }


class ControlKindTests(RuntimeGuardedTestCase):
    def test_control_kind_classifies_common_win32_controls(self) -> None:
        cases = [
            ("Edit", "", "text_field"),
            ("RichEditD2DPT", "", "text_field"),
            ("ComboBox", "", "dropdown"),
            ("SysListView32", "", "list"),
            ("TreeView", "", "list"),
            ("Button", "Save", "primary_button"),
            ("Button", "Cancel", "button"),
            ("Static", "Helpful label", "label"),
            ("SysLink", "Help", "link"),
            ("CustomPane", "", "control"),
        ]

        for class_name, title, expected in cases:
            with self.subTest(class_name=class_name, title=title):
                self.assertEqual(helperctl.control_kind(class_name, title), expected)


class CandidateRankingTests(RuntimeGuardedTestCase):
    def test_intent_match_prefers_exact_label_over_token_overlap(self) -> None:
        exact_score, exact_reasons = helperctl.candidate_intent_match_score(
            candidate(1, title="Save"),
            "Click Save to continue",
        )
        token_score, token_reasons = helperctl.candidate_intent_match_score(
            candidate(2, title="Save document"),
            "Save the current item",
        )
        no_score, no_reasons = helperctl.candidate_intent_match_score(
            candidate(3, title="Cancel"),
            "Save the current item",
        )

        self.assertEqual(exact_score, 0.22)
        self.assertEqual(exact_reasons, ["label_in_intent"])
        self.assertGreater(token_score, 0.0)
        self.assertIn("intent_token_match:save", token_reasons)
        self.assertEqual((no_score, no_reasons), (0.0, []))

    def test_ranking_excludes_labels_and_orders_actionable_intent_match_first(self) -> None:
        label = candidate(1, title="Save the file", class_name="Static", kind="label")
        cancel = candidate(2, title="Cancel", kind="button")
        save = candidate(3, title="Save", kind="primary_button")

        ranked = helperctl.rank_candidates(
            [label, cancel, save],
            active_context(label, cancel, save),
            "Click Save",
        )

        self.assertEqual([item["candidate_id"] for item in ranked], [3, 2])
        self.assertEqual([item["rank"] for item in ranked], [1, 2])
        self.assertEqual(ranked[0]["confidence"], "high")
        self.assertIn("label_in_intent", ranked[0]["score_reasons"])

    def test_duplicate_labels_are_retained_and_flagged(self) -> None:
        first = candidate(1, title="Save", kind="primary_button")
        second = candidate(2, title="Save", kind="primary_button")

        ranked = helperctl.rank_candidates(
            [first, second],
            active_context(first, second),
            "Save",
        )

        self.assertEqual(len(ranked), 2)
        self.assertTrue(all("duplicate_label" in item["risk_flags"] for item in ranked))
        self.assertEqual(ranked[0]["score"], ranked[1]["score"])

    def test_action_menu_does_not_recommend_or_offer_non_actionable_candidates(self) -> None:
        unavailable = candidate(
            1,
            title="Save",
            kind="primary_button",
            visible=False,
            enabled=False,
        )

        menu = helperctl.build_action_menu(active_context(unavailable), "Click Save")

        self.assertEqual(len(menu["ranked_candidates"]), 1)
        self.assertIsNone(menu["recommended_candidate"])
        self.assertEqual(menu["likely_click_targets"], [])
        self.assertEqual(menu["likely_primary_buttons"], [])

    def test_candidate_available_actions_follow_kind_and_class(self) -> None:
        cases = [
            (
                candidate(1, kind="primary_button", class_name="Button"),
                ["input.click_candidate", "input.invoke_candidate", "input.select_candidate"],
            ),
            (
                candidate(2, kind="text_field", class_name="Edit"),
                ["input.click_candidate", "input.set_candidate_value"],
            ),
            (
                candidate(3, kind="dropdown", class_name="ComboBox"),
                ["input.click_candidate", "input.expand_candidate"],
            ),
            (
                candidate(4, kind="list", class_name="SysListView32"),
                ["input.click_candidate", "input.select_candidate"],
            ),
        ]

        for item, expected in cases:
            with self.subTest(kind=item["kind"]):
                self.assertEqual(helperctl.candidate_available_actions(item), expected)


class CandidateValidationTests(RuntimeGuardedTestCase):
    def setUp(self) -> None:
        self.context = active_context()
        self.base_candidate = candidate(1, title="Save", kind="primary_button")

    def validate(
        self,
        item: dict[str, object],
        *,
        is_window: bool = True,
        args: dict[str, object] | None = None,
    ) -> dict[str, object]:
        fake_windll = SimpleNamespace(user32=SimpleNamespace(IsWindow=Mock(return_value=is_window)))
        with patch.object(helperctl.ctypes, "windll", fake_windll):
            return helperctl.validate_candidate_for_click(item, self.context, args or {})

    def test_rejects_invalid_window_handle(self) -> None:
        result = self.validate(self.base_candidate, is_window=False)

        self.assertEqual(result["status"], "UNCERTAIN")
        self.assertIn("no longer valid", result["message"])

    def test_rejects_invisible_disabled_outside_and_wrong_kind_candidates(self) -> None:
        cases = [
            ({**self.base_candidate, "visible": False}, {}, "not visible"),
            ({**self.base_candidate, "enabled": False}, {}, "disabled"),
            (
                {**self.base_candidate, "bounds": bounds(350, 250, 450, 350)},
                {},
                "outside the active window",
            ),
            (self.base_candidate, {"expected_kind": "text_field"}, "did not match expected kind"),
        ]

        for item, args, message_part in cases:
            with self.subTest(message_part=message_part):
                result = self.validate(item, args=args)
                self.assertEqual(result["status"], "UNCERTAIN")
                self.assertIn(message_part, result["message"])

    def test_accepts_current_visible_enabled_contained_candidate(self) -> None:
        result = self.validate(self.base_candidate, args={"expected_kind": "primary_button"})

        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["data"]["candidate"], self.base_candidate)


class EntryAndClickGuardrailTests(RuntimeGuardedTestCase):
    def test_save_dialog_path_text_is_blocked(self) -> None:
        context = dialog_context()
        with patch.object(helperctl, "build_active_context_packet_for_args", return_value=context):
            result = helperctl.evaluate_text_entry_guardrails(
                "input.type_text",
                r"C:\Users\Example\Desktop\report.txt",
                {},
            )

        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "UNCERTAIN")
        self.assertTrue(result["data"]["looks_like_path"])
        self.assertEqual(
            result["next_allowed_actions"],
            ["observe.screenshot", "recovery.capture_grid", "verify.window_state"],
        )

    def test_explicit_path_permission_bypasses_only_the_path_guard(self) -> None:
        context = dialog_context()
        with patch.object(helperctl, "build_active_context_packet_for_args", return_value=context):
            result = helperctl.evaluate_text_entry_guardrails(
                "input.type_text",
                r"C:\Users\Example\Desktop\report.txt",
                {"allow_path_text": True},
            )

        self.assertIsNone(result)

    def test_vm_detection_does_not_override_the_path_guard(self) -> None:
        boundary = {"detected": True, "platform": "remote_desktop"}
        context = dialog_context(vm_boundary=boundary)
        with patch.object(helperctl, "build_active_context_packet_for_args", return_value=context):
            blocked_path = helperctl.evaluate_text_entry_guardrails(
                "input.type_text", r"C:\Users\Example\Desktop\report.txt", {},
            )
            allowed_path = helperctl.evaluate_text_entry_guardrails(
                "input.type_text", r"C:\Users\Example\Desktop\report.txt", {"allow_path_text": True},
            )
            ordinary_text = helperctl.evaluate_text_entry_guardrails(
                "input.type_text", "Hello VM", {},
            )

        self.assertEqual(blocked_path["status"], "UNCERTAIN")
        self.assertTrue(blocked_path["data"]["looks_like_path"])
        self.assertIsNone(allowed_path)
        self.assertIsNone(ordinary_text)

    def test_dialog_click_is_confined_to_dialog_bounds(self) -> None:
        context = dialog_context(vm_boundary={"detected": True, "platform": "vmware"})
        target = context["current_dialog"]
        with patch.object(helperctl, "build_active_context_packet_for_args", return_value=context):
            inside = helperctl.evaluate_click_guardrails(
                "input.click_point",
                {"target": "current_dialog"},
                250,
                250,
                target,
            )
            outside = helperctl.evaluate_click_guardrails(
                "input.click_point",
                {"target": "current_dialog"},
                50,
                50,
                target,
            )

        self.assertIsNone(inside)
        self.assertIsNotNone(outside)
        self.assertEqual(outside["status"], "UNCERTAIN")
        self.assertIn("outside the active dialog bounds", outside["message"])


class VmBoundaryTests(RuntimeGuardedTestCase):
    def test_detects_supported_vm_and_remote_desktop_boundaries(self) -> None:
        cases = [
            (("", "VMware.exe", ""), "vmware"),
            (("Oracle VM VirtualBox", "", ""), "virtualbox"),
            (("", "vmconnect.exe", ""), "hyper_v"),
            (("Remote Desktop", "mstsc.exe", ""), "remote_desktop"),
        ]

        for inputs, platform in cases:
            with self.subTest(platform=platform):
                result = helperctl.detect_vm_boundary(*inputs)
                self.assertTrue(result["detected"])
                self.assertEqual(result["platform"], platform)
                self.assertEqual(result["default_policy"], "informational_only")

        self.assertEqual(
            helperctl.detect_vm_boundary("Example App", "example.exe", "ExampleWindow"),
            {"detected": False},
        )

    def test_vm_detection_never_blocks_input_and_legacy_opt_in_is_ignored(self) -> None:
        boundary = {"detected": True, "platform": "vmware", "reasons": ["process:vmware.exe"]}
        context = {"vm_boundary": boundary, "active_window": {}, "child_candidates": []}
        for args in ({}, {"allow_vm_control": False}, {"allow_vm_control": True}):
            for target in (None, {"vm_boundary": boundary}):
                with self.subTest(args=args, target=target):
                    self.assertIsNone(helperctl.evaluate_vm_boundary_guardrail(
                        "input.click_point", args, active_context=context, target_window=target,
                    ))

    def test_vm_foreground_and_vm_target_clicks_reach_cursor_verification(self) -> None:
        boundary = helperctl.detect_vm_boundary("VMware Player", "vmplayer.exe")
        window = helperctl.WindowInfo(800, "VMware Player", 100, 200, 500, 600, False)
        payload = {"hwnd": 800, "title": window.title, "vm_boundary": boundary}
        context = {"active_window": payload, "vm_boundary": boundary, "dialog_open": False}
        for target in ("desktop", "target_window"):
            for verified in (True, False):
                with (
                    self.subTest(target=target, verified=verified),
                    patch.object(helperctl, "nudge_cursor_from_failsafe_corner", return_value={}),
                    patch.object(helperctl, "resolve_working_window", return_value=window),
                    patch.object(helperctl, "cmd_focus_window", return_value=helperctl.command_result("SUCCESS", "focus.window", "focused")),
                    patch.object(helperctl, "build_active_context_packet_for_args", return_value=context),
                    patch.object(helperctl, "get_foreground_window_snapshot", return_value=payload),
                    patch.object(helperctl, "window_payload", return_value=payload),
                    patch.object(helperctl, "perform_screen_click", return_value={"click_verified": verified, "injection_succeeded": True}) as click,
                    patch.object(helperctl, "current_process_elevation", return_value={"available": True, "is_elevated": False}),
                    patch.object(helperctl, "cancellable_sleep"),
                ):
                    result = helperctl.cmd_input_click_point({"target": target, "coordinate_mode": "screen", "x": 250, "y": 260})
                    click.assert_called_once_with(250, 260)
                    self.assertEqual(result["status"], "SUCCESS" if verified else "UNCERTAIN")
                    if not verified:
                        self.assertEqual(result["data"]["failure_type"], "INPUT_CURSOR_UNVERIFIED")


class ExpectedChangeContractTests(RuntimeGuardedTestCase):
    def test_expected_change_normalization_prefers_observe_after_and_copies_input(self) -> None:
        root_args = {
            "expected_change": {"type": "file_exists", "path": "fallback.txt"},
            "observe_after": {
                "expected_change": {"type": "Dialog_Open", "title_contains": "Save"}
            },
        }

        normalized = helperctl.expected_change_from_args(root_args)

        self.assertEqual(
            normalized,
            {"type": "dialog_open", "title_contains": "Save"},
        )
        self.assertEqual(root_args["observe_after"]["expected_change"]["type"], "Dialog_Open")
        self.assertEqual(helperctl.expected_change_from_args({}), {})

    def test_expected_change_result_has_stable_shape_and_retains_last_six_polls(self) -> None:
        polls = [{"attempt": index} for index in range(1, 9)]

        result = helperctl.expected_change_result(
            {"type": "foreground_change"},
            False,
            8,
            450,
            75,
            "expected_change_timeout",
            polls,
        )

        self.assertEqual(result["enabled"], True)
        self.assertEqual(result["changed"], False)
        self.assertEqual(result["matched"], False)
        self.assertEqual(result["reason"], "expected_change_timeout")
        self.assertEqual([item["attempt"] for item in result["polls"]], [3, 4, 5, 6, 7, 8])

    def test_file_exists_and_file_absent_helpers_match_without_desktop_observation(self) -> None:
        baseline = {"status": "SUCCESS", "data": {}}
        with tempfile.TemporaryDirectory() as directory:
            present = Path(directory) / "present.txt"
            absent = Path(directory) / "absent.txt"
            present.write_text("fixture", encoding="utf-8")

            with patch.object(helperctl, "list_windows", return_value=[]):
                exists_result = helperctl.wait_for_expected_change(
                    {},
                    "input.click_candidate",
                    {},
                    baseline,
                    {"type": "file_exists", "path": str(present), "max_wait_ms": 1},
                )
                absent_result = helperctl.wait_for_expected_change(
                    {},
                    "input.click_candidate",
                    {},
                    baseline,
                    {"type": "file_absent", "path": str(absent), "max_wait_ms": 1},
                )

        self.assertTrue(exists_result["matched"])
        self.assertEqual(exists_result["reason"], "expected_file_exists")
        self.assertTrue(absent_result["matched"])
        self.assertEqual(absent_result["reason"], "expected_file_absent")

    def test_find_expected_window_filters_excluded_handles_title_and_process(self) -> None:
        windows = [
            helperctl.WindowInfo(10, "Old Save Dialog", 0, 0, 100, 100, False),
            helperctl.WindowInfo(20, "New Save Dialog", 0, 0, 100, 100, False),
            helperctl.WindowInfo(30, "New Save Dialog", 0, 0, 100, 100, False),
        ]

        with (
            patch.object(helperctl, "list_windows", return_value=windows),
            patch.object(
                helperctl,
                "get_process_name_for_hwnd",
                side_effect=lambda hwnd: {10: "old.exe", 20: "notepad.exe", 30: "other.exe"}[hwnd],
            ),
        ):
            match = helperctl.find_expected_window(
                {"title_contains": "Save", "process_contains": "notepad"},
                exclude_hwnds={10},
            )

        self.assertIsNotNone(match)
        self.assertEqual(match.hwnd, 20)



class FileOutcomeTests(RuntimeGuardedTestCase):
    def test_file_declarations_reject_ambiguous_incomplete_and_invalid_content_before_input(self):
        for spec in (
            {"type": "file_saved", "path": "x"},
            {"type": "file_content", "path": "x", "expected_sha256": "wrong"},
            {"type": "file_renamed", "old_path": "x"},
            {"type": "file_renamed", "old_path": "X", "new_path": ".\\x"},
            {"type": "file_exists", "desktop_filename": "..\\other.txt"},
            {"type": "file_exists", "desktop_filename": "x", "path": "y"},
            {"type": "file_exists", "path": "bad\0path"},
        ):
            with self.subTest(spec=spec), patch.object(helperctl, "cmd_input_hotkey") as send, patch.object(helperctl, "capture_planner_step_baseline") as baseline:
                self.assertTrue(effects.validate_expected_change(spec))
                planner = helperctl.cmd_planner_step({"action": "hotkey", "keys": "ctrl+s", "expected_change": spec})
                sequence = helperctl.cmd_input_action_sequence({"actions": [{"action": "hotkey", "keys": "ctrl+s"}, {"action": "hotkey", "keys": "enter", "expected_change": spec}]})
                self.assertEqual(planner["status"], "ERROR")
                self.assertEqual(sequence["data"]["attempted_count"], 0)
                send.assert_not_called()
                baseline.assert_not_called()

    def test_redirected_desktop_uses_known_folder_and_lookup_failure_is_not_absence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "owned.txt").write_bytes(b"saved")
            with patch.object(helperctl, "desktop_known_folder", return_value=root) as folder:
                result = helperctl.cmd_verify_file_state({"desktop_filename": "owned.txt"})
                snapshot = helperctl.capture_effect_snapshot({"type": "file_exists", "desktop_filename": "owned.txt"})
                self.assertEqual(result["status"], "SUCCESS")
                self.assertTrue(snapshot["exists"])
                self.assertEqual(Path(snapshot["path"]), root / "owned.txt")
                self.assertEqual(folder.call_count, 2)
        with patch.object(helperctl, "desktop_known_folder", side_effect=OSError("lookup failed")):
            self.assertEqual(helperctl.cmd_verify_file_state({"desktop_filename": "x", "should_exist": False})["status"], "UNCERTAIN")
            self.assertIn("error", helperctl.capture_effect_snapshot({"type": "file_absent", "desktop_filename": "x"}))

    def test_known_folder_api_uses_current_path_and_balances_com_and_allocations(self):
        buffer = ctypes.create_unicode_buffer(r"C:\Redirected\Desktop")
        for initialized, result_code in ((0, 0), (1, 0), (-2147417850, 0), (0, -2147467259)):
            def query(folder_id, flags, token, output):
                self.assertEqual(bytes(ctypes.cast(folder_id, ctypes.POINTER(ctypes.c_ubyte * 16)).contents), helperctl.uuid.UUID("B4BFCC3A-DB2C-424C-B029-7FE99A87C641").bytes_le)
                self.assertEqual(flags, 0)
                self.assertIsNone(token)
                ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(buffer)
                return result_code
            shell = Mock(SHGetKnownFolderPath=Mock(side_effect=query))
            ole = Mock(CoInitializeEx=Mock(return_value=initialized))
            with self.subTest(initialized=initialized, result=result_code), patch.object(helperctl.ctypes.windll, "shell32", shell), patch.object(helperctl.ctypes.windll, "ole32", ole):
                if result_code < 0:
                    with self.assertRaises(OSError):
                        helperctl.desktop_known_folder()
                else:
                    self.assertEqual(helperctl.desktop_known_folder(), Path(r"C:\Redirected\Desktop"))
                ole.CoTaskMemFree.assert_called_once()
                self.assertEqual(ole.CoUninitialize.call_count, int(initialized >= 0))

    def test_exact_content_and_save_transition_never_accept_existing_content_or_metadata_only(self):
        desired = b"new content\r\n"
        digest = hashlib.sha256(desired).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "owned.txt"
            spec = {"type": "file_saved", "path": str(path), "expected_sha256": digest, "max_wait_ms": 0}
            absent = helperctl.capture_effect_snapshot(spec)
            path.write_bytes(desired)
            present = helperctl.capture_effect_snapshot(spec)
            self.assertTrue(effects.evaluate_effect(spec, absent, present)[0])
            self.assertFalse(effects.evaluate_effect(spec, present, present)[0])
            self.assertTrue(effects.evaluate_effect({**spec, "type": "file_content"}, {}, present)[0])
            os.utime(path, None)
            self.assertFalse(effects.evaluate_effect(spec, present, helperctl.capture_effect_snapshot(spec))[0])
            path.write_bytes(b"bad content\r\n")  # same length, different bytes
            wrong = helperctl.capture_effect_snapshot(spec)
            self.assertFalse(effects.evaluate_effect(spec, absent, wrong)[0])
            self.assertEqual(helperctl.cmd_verify_file_state({"path": str(path), "expected_sha256": digest})["status"], "UNCERTAIN")
            path.write_bytes(desired)
            self.assertTrue(effects.evaluate_effect(spec, wrong, helperctl.capture_effect_snapshot(spec))[0])
            self.assertEqual(helperctl.cmd_verify_file_state({"path": str(path), "expected_sha256": digest.upper()})["status"], "SUCCESS")
            self.assertNotIn("new content", str(present))

    def test_rename_requires_old_new_transition_same_identity_and_unchanged_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            old, new = Path(temporary) / "old.txt", Path(temporary) / "new.txt"
            old.write_bytes(b"owned")
            spec = {"type": "file_renamed", "old_path": str(old), "new_path": str(new)}
            before = helperctl.capture_effect_snapshot(spec)
            old.rename(new)
            after = helperctl.capture_effect_snapshot(spec)
            self.assertTrue(effects.evaluate_effect(spec, before, after)[0])
            self.assertFalse(effects.evaluate_effect(spec, after, after)[0])
            new.rename(old)
            before = helperctl.capture_effect_snapshot(spec)
            shutil.copyfile(old, new)
            old.unlink()
            self.assertFalse(effects.evaluate_effect(spec, before, helperctl.capture_effect_snapshot(spec))[0])
            unavailable = {**before, "old": {**before["old"], "identity": None}}
            self.assertFalse(effects.evaluate_effect(spec, unavailable, after)[0])
            preexisting = {**before, "new": after["new"]}
            self.assertFalse(effects.evaluate_effect(spec, preexisting, after)[0])
            changed = {**after, "new": {**after["new"], "sha256": "different"}}
            self.assertFalse(effects.evaluate_effect(spec, before, changed)[0])

    def test_content_evidence_fails_closed_for_large_unstable_and_unreadable_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "owned.txt"
            path.write_bytes(b"owned")
            with patch.object(helperctl, "MAX_CONTENT_BYTES", 4):
                self.assertIn("error", helperctl.capture_file_evidence(path, content=True))
            info = path.stat()
            changed = SimpleNamespace(**{key: getattr(info, key) for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")})
            changed.st_size += 1
            with patch.object(helperctl.os, "fstat", side_effect=[info, changed]):
                self.assertIn("error", helperctl.capture_file_evidence(path, content=True))
            with patch.object(Path, "open", side_effect=PermissionError("denied")):
                self.assertIn("error", helperctl.capture_file_evidence(path, content=True))
            with patch.object(Path, "lstat", return_value=info), patch.object(Path, "stat", side_effect=FileNotFoundError("link target unavailable")):
                broken = helperctl.capture_file_evidence(path, content=True)
                self.assertIn("error", broken)
                self.assertNotIn("exists", broken)
            # Cross-source ctime can differ on Windows; each source must remain
            # stable throughout the read, while identity/size/mtime still agree.
            opened = SimpleNamespace(**{key: getattr(info, key) for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_birthtime_ns")})
            opened.st_ctime_ns += 1
            with patch.object(helperctl.os, "fstat", side_effect=[opened, opened]):
                self.assertIn("sha256", helperctl.capture_file_evidence(path, content=True))
            self.assertIn("error", helperctl.capture_file_evidence(Path(temporary), content=True))
            token = helperctl.CommandCancellationToken()
            token.cancel("stop file read")
            with helperctl.command_cancellation(token), self.assertRaises(helperctl.CommandCancelled):
                helperctl.capture_file_evidence(path, content=True)

    def test_sequence_content_transition_controls_later_steps_using_shared_watcher(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            redirect_runtime(self, root / "runtime")
            path = root / "owned.txt"
            digest = hashlib.sha256(b"saved").hexdigest()
            spec = {"type": "file_saved", "path": str(path), "expected_sha256": digest, "max_wait_ms": 0}
            actions = [{"action": "hotkey", "keys": "ctrl+s", "expected_change": spec}, {"action": "wait", "seconds": 0}]
            def save(args):
                path.write_bytes(b"saved")
                return helperctl.command_result("SUCCESS", "input.hotkey", "delivered")
            with patch.object(helperctl, "get_foreground_window_snapshot", return_value={}), patch.object(helperctl, "cmd_input_hotkey", side_effect=save):
                result = helperctl.cmd_input_action_sequence({"actions": actions})
                self.assertEqual(result["status"], "SUCCESS")
                self.assertEqual(result["data"]["attempted_count"], 2)
                repeated = helperctl.cmd_input_action_sequence({"actions": actions})
                self.assertEqual(repeated["status"], "UNCERTAIN")
                self.assertEqual(repeated["data"]["attempted_count"], 1)


class TypedEffectTests(RuntimeGuardedTestCase):
    def test_declarations_reject_unknown_incomplete_conflicting_and_unbounded_effects(self):
        for spec in ({}, {"type": "magic"}, {"type": "window_close"},
                     {"type": "title_change", "hwnd": 1}, {"type": "new_window"},
                     {"type": "file_created"}, {"type": "any_change", "poll_ms": False},
                     {"type": "any_change", "max_wait_ms": 30001},
                     {"type": "file_exists", "path": "x", "title_contans": "x"}):
            with self.subTest(spec=spec):
                self.assertTrue(effects.validate_expected_change(spec))
                with patch.object(helperctl, "capture_planner_step_baseline") as baseline, patch.object(helperctl, "execute_planner_action") as execute:
                    result = helperctl.cmd_planner_step({"action": "hotkey", "keys": "alt+f4", "expected_change": spec})
                    self.assertEqual(result["status"], "ERROR")
                    baseline.assert_not_called()
                    execute.assert_not_called()
        _, errors = effects.declared_effect({"expected_change": {"type": "any_change"}, "step": {"expected_change": {"type": "visual_stable"}}})
        self.assertTrue(errors)

    def test_typed_states_and_transitions_do_not_accept_preexisting_or_unrelated_state(self):
        cases = [
            ({"type": "file_created"}, {"exists": True}, {"exists": True}, False),
            ({"type": "file_created"}, {"exists": False}, {"exists": True}, True),
            ({"type": "file_deleted"}, {"exists": False}, {"exists": False}, False),
            ({"type": "file_deleted"}, {"exists": True}, {"exists": False}, True),
            ({"type": "file_exists"}, {}, {"exists": True}, True),
            ({"type": "window_close", "hwnd": 1}, {"alive": True}, {"alive": True, "error": "rectangle unavailable"}, False),
            ({"type": "window_close", "hwnd": 1}, {"alive": True}, {"alive": False}, True),
            ({"type": "title_change", "hwnd": 1, "title": "New"}, {"alive": True, "title": "Old", "pid": 5}, {"alive": True, "title": "New", "pid": 6}, False),
            ({"type": "title_change", "hwnd": 1, "title": "New"}, {"alive": True, "title": "Old", "pid": 5}, {"alive": True, "title": "New", "pid": 5}, True),
        ]
        for spec, before, after, expected in cases:
            with self.subTest(spec=spec, before=before):
                self.assertEqual(effects.evaluate_effect(spec, before, after)[0], expected)
        for kind in ("new_window", "dialog_open"):
            spec = {"type": kind, "title_contains": "Save"}
            before = {"windows": [{"hwnd": 1, "title": "Save"}]}
            self.assertFalse(effects.evaluate_effect(spec, before, before)[0])
            self.assertTrue(effects.evaluate_effect(spec, before, {"windows": [{"hwnd": 2, "title": "Save"}]})[0])

    def test_window_read_failure_is_not_closure(self):
        with patch.object(helperctl.ctypes.windll.user32, "IsWindow", return_value=True), patch.object(helperctl, "window_info_from_hwnd", return_value=None), patch.object(helperctl, "get_window_process_id", return_value=4):
            snapshot = helperctl.capture_effect_snapshot({"type": "window_close", "hwnd": 1})
        self.assertTrue(snapshot["alive"])
        self.assertIn("error", snapshot)

    def test_watch_uses_pre_action_window_set_and_requires_snapshot_for_transition(self):
        spec = {"type": "new_window", "title_contains": "Save", "max_wait_ms": 0}
        baseline = {"status": "SUCCESS", "data": {"effect_snapshot": {"windows": [{"hwnd": 1, "title": "Old"}]}}}
        with patch.object(helperctl, "capture_effect_snapshot", return_value={"windows": [{"hwnd": 2, "title": "Save"}]}):
            result = helperctl.wait_for_expected_change({}, "hotkey", {}, baseline, spec)
            self.assertTrue(result["matched"])
            result = helperctl.wait_for_expected_change({}, "hotkey", {}, {"data": {}}, spec)
            self.assertFalse(result["matched"])
            self.assertEqual(result["reason"], "pre_action_evidence_missing")

    def test_content_loaded_must_change_then_stabilize_and_stays_visual_only(self):
        spec = {"type": "content_loaded", "max_wait_ms": 100, "poll_ms": 10, "stable_frames": 1}
        baseline = {"status": "SUCCESS", "data": {}}
        with patch.object(helperctl, "capture_target", return_value=baseline), patch.object(helperctl, "build_follow_up_capture_args", return_value={}), patch.object(helperctl, "compare_capture_images", side_effect=[{"matched": False}, {"matched": True}]):
            result = helperctl.wait_for_expected_change({}, "hotkey", {}, baseline, spec)
            self.assertTrue(result["matched"])
            self.assertEqual(result["verification"]["verdict"], "observed")
        for compare in ({"matched": True}, {"comparison_error": True}):
            with patch.object(helperctl, "capture_target", return_value=baseline), patch.object(helperctl, "build_follow_up_capture_args", return_value={}), patch.object(helperctl, "compare_capture_images", return_value=compare):
                result = helperctl.wait_for_expected_change({}, "hotkey", {}, baseline, {**spec, "max_wait_ms": 0})
                self.assertFalse(result["matched"])

    def test_planner_typed_timeout_governs_outcome_even_with_disabled_watch(self):
        spec = {"type": "window_close", "hwnd": 7, "max_wait_ms": 0}
        for matched, status in ((False, "UNCERTAIN"), (True, "NEEDS_PLANNER")):
            baseline = {"status": "SUCCESS", "capture": {"status": "SUCCESS", "data": {}}}
            follow_up = {"status": "SUCCESS", "data": {"request_id": "TEST", "change_watch": {"matched": matched, "reason": "fixture"}}}
            with patch.object(helperctl, "capture_planner_step_baseline", return_value=baseline), patch.object(helperctl, "cmd_verify_window_state", return_value={"status": "SUCCESS"}), patch.object(helperctl, "capture_effect_snapshot", return_value={"alive": True}), patch.object(helperctl, "execute_planner_action", return_value={"status": "SUCCESS"}) as execute, patch.object(helperctl, "capture_planner_follow_up", return_value=follow_up) as follow, patch.object(helperctl, "write_planner_step_report", return_value=Path("fixture.json")):
                result = helperctl.cmd_planner_step({"step": {"action": "hotkey", "keys": "alt+f4", "expected_change": spec}, "wait_for_change": False, "verify_before": {"focus_if_needed": True}})
                self.assertEqual(result["status"], status)
                self.assertEqual(result["outcome"], "observation_ready" if matched else "blocked")
                self.assertTrue(follow.call_args.args[0]["wait_for_change"])
                self.assertIn("effect_snapshot", follow.call_args.kwargs["baseline_capture"]["data"])
                execute.assert_called_once()

    def test_blocked_action_cannot_be_rehabilitated_by_independent_state(self):
        blocked = helperctl.command_result("NEEDS_PLANNER", "recovery.guided_grid_click", "stale", data={"failure_type": "STALE_SCREENSHOT", "verification": {"status": "FAILED"}})
        self.assertEqual(blocked["outcome"], "blocked")
        legacy = helperctl.command_result("SUCCESS", "recovery.guided_grid_click", "verified", data={"verification": {"status": "SUCCESS"}})
        self.assertEqual(legacy["outcome"], "action_done")
        baseline = {"status": "SUCCESS", "capture": {"status": "SUCCESS", "data": {}}}
        with patch.object(helperctl, "capture_planner_step_baseline", return_value=baseline), patch.object(helperctl, "capture_effect_snapshot", return_value={"exists": True}), patch.object(helperctl, "execute_planner_action", return_value=blocked), patch.object(helperctl, "capture_planner_follow_up") as follow:
            result = helperctl.cmd_planner_step({"action": "guided_grid_click", "expected_change": {"type": "file_exists", "path": "fixture"}})
        self.assertEqual(result["outcome"], "blocked")
        follow.assert_not_called()
        fake = helperctl.command_result("NEEDS_PLANNER", "planner.step", "bad", data={"action_result": blocked, "verification": {"verdict": "verified"}})
        self.assertFalse(helperctl.action_result_succeeded("planner.step", fake))

    def test_visual_stability_resets_after_capture_failure_or_window_switch(self):
        baseline = {"status": "SUCCESS", "data": {"target_window": {"hwnd": 1}}}
        other = {"status": "SUCCESS", "data": {"target_window": {"hwnd": 2}}}
        for kind, captures in (("visual_stable", [baseline, {"status": "UNCERTAIN"}, baseline]), ("same_window_stable", [baseline, other, baseline])):
            with self.subTest(kind=kind), patch.object(helperctl, "capture_target", side_effect=captures), patch.object(helperctl, "build_follow_up_capture_args", return_value={}), patch.object(helperctl, "compare_capture_images", return_value={"matched": True}), patch.object(helperctl.time, "monotonic", side_effect=[0, 0, 0, 0, 0, 1]), patch.object(helperctl, "cancellable_sleep"):
                result = helperctl.wait_for_expected_change({}, "hotkey", {}, baseline, {"type": kind, "stable_frames": 2, "max_wait_ms": 100})
            self.assertFalse(result["matched"])

    def test_cancellation_interrupts_effect_watch(self):
        token = helperctl.CommandCancellationToken()
        token.cancel("fixture stop")
        with helperctl.command_cancellation(token), self.assertRaises(helperctl.CommandCancelled):
            helperctl.wait_for_expected_change({}, "hotkey", {}, {"data": {}}, {"type": "file_exists", "path": "fixture"})


if __name__ == "__main__":
    unittest.main()
