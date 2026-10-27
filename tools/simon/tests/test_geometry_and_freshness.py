from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from tests.runtime_support import RuntimeGuardedTestCase, redirect_runtime

import helperctl
import simon_schema
import simon_geometry
import simon_images
import agent_helper
from unittest import mock


def grid(
    *,
    columns: int = 3,
    rows: int = 3,
    cell_width: float = 10.0,
    cell_height: float = 10.0,
) -> dict[str, object]:
    return {
        "columns": columns,
        "rows": rows,
        "cell_width": cell_width,
        "cell_height": cell_height,
    }


def window(
    *,
    hwnd: int = 101,
    title: str = "Example",
    left: int = 10,
    top: int = 20,
    right: int = 410,
    bottom: int = 320,
) -> helperctl.WindowInfo:
    return helperctl.WindowInfo(
        hwnd=hwnd,
        title=title,
        left=left,
        top=top,
        right=right,
        bottom=bottom,
        minimized=False,
    )


def capture_state(
    *,
    foreground_hwnd: int = 101,
    foreground_title: str = "Example",
    active_hwnd: int = 101,
    active_title: str = "Example",
    dialog_open: bool = False,
    candidate_count: int = 1,
    raw_screenshot_path: str = "baseline.png",
) -> dict[str, object]:
    return {
        "data": {
            "raw_screenshot_path": raw_screenshot_path,
            "foreground_window": {
                "hwnd": foreground_hwnd,
                "title": foreground_title,
            },
            "active_context": {
                "active_window": {
                    "hwnd": active_hwnd,
                    "title": active_title,
                },
                "dialog_open": dialog_open,
                "child_candidates": [
                    {"candidate_id": candidate_id}
                    for candidate_id in range(candidate_count)
                ],
            },
        }
    }


class C2TargetingTests(RuntimeGuardedTestCase):
    def setUp(self):
        super().setUp()
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        redirect_runtime(self, self.root)

    def request(self):
        raw = self.root / 'command_screenshots' / 'root_raw.png'
        raw.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (400, 200), (35, 75, 110)).save(raw)
        transform = simon_geometry.image_transform(400, 200, {'left': -800, 'top': -300, 'width': 400, 'height': 200}, route='window_gdi_region', verified=True)
        request = {'request_id': 'ROOT', 'raw_screenshot_path': str(raw), 'target': 'desktop',
                   'grid': grid(columns=4, rows=2, cell_width=100, cell_height=100),
                   'image_to_screen': transform, 'capture_timestamp': helperctl.now_iso()}
        helperctl.write_planner_request(request)
        return request

    def test_two_crops_compose_scale_origin_and_keep_original_age(self):
        parent = self.request()
        parent['image_to_screen'].update(scale_x=1.5, scale_y=2)
        parent['capture_timestamp'] = '2020-01-01T00:00:00'
        child = helperctl.create_grid_crop_request(parent, 'C2', 2, 2, '', False)['data']['planner_request']
        nested = helperctl.create_grid_crop_request(child, 'B2', 2, 2, '', False)['data']['planner_request']
        self.assertEqual(nested['crop']['offset_x'], 250)
        self.assertEqual(nested['crop']['offset_y'], 150)
        self.assertEqual(nested['capture_timestamp'], parent['capture_timestamp'])
        self.assertIn('stale', helperctl.planner_request_staleness(nested, 60))
        self.assertEqual(simon_geometry.image_point_to_screen(nested['image_to_screen'], (10, 10)), (-410, 20))
        self.assertEqual((nested['screenshot_width'], nested['screenshot_height']), (50, 50))
        self.assertEqual(helperctl.comparable_fresh_region(nested, Image.new('RGB', (400, 200))).size, (50, 50))

    def test_zoom_rulers_cell_density_and_delivered_transform(self):
        parent = self.request()
        child = helperctl.create_grid_crop_request(parent, 'A1', 40, 40, '', False)['data']['planner_request']
        with Image.open(child['grid_screenshot_path']) as rendered:
            self.assertEqual(rendered.size, (1064, 1064))
            # No opaque label box in the first content cell.
            self.assertEqual(rendered.getpixel((47, 47)), (35, 75, 110))
        self.assertEqual(child['grid']['columns'], 16)
        self.assertEqual(child['grid']['rows'], 16)
        output = simon_images.planner_image({'data': child}, {'image_max_edge': 532})
        transform = output['image_to_screen']
        self.assertEqual(simon_geometry.image_point_to_screen(transform, (276, 276)), (-750, -250))
        self.assertIsNone(simon_geometry.image_point_to_screen(transform, (10, 10)))

    def test_pixel_center_mapping_roundtrips_wide_negative_origin_desktop(self):
        # Windows converts absolute coordinates into bins of 65536 / extent.
        # Edge mapping fails this invariant at several points on 16800px desktops.
        for left, top, width, height in ((-7680, -540, 16800, 2160), (0, 0, 1920, 1080)):
            metrics = {76: left, 77: top, 78: width, 79: height}
            with patch.object(helperctl.ctypes.windll.user32, 'GetSystemMetrics', side_effect=lambda key: metrics[key]):
                for x in (left, left+1, left+width//3, left+width//2, left+width-2, left+width-1):
                    for y in (top, top+1, top+height//2, top+height-1):
                        absolute = helperctl.normalized_virtual_mouse_coordinate(x, y)
                        self.assertEqual(left + absolute['x'] * width // 65536, x)
                        self.assertEqual(top + absolute['y'] * height // 65536, y)

    def test_window_aliases_reject_movement_and_reused_identity(self):
        captured = window()
        for target in ('foreground_window', 'active_window', 'current_dialog', 'target_monitor'):
            request = {'target': target, 'target_window': {'hwnd': captured.hwnd, 'bounds': captured.bounds}}
            self.assertIn('moved', helperctl.validate_planner_target_window(request, window(left=11)))
            self.assertIn('identity', helperctl.validate_planner_target_window(request, window(hwnd=202)))

    def test_capture_fallback_transform_uses_actual_monitor_and_synthetic_is_blocked(self):
        image = Image.new('RGB', (400, 200))
        monitor = {'bounds': {'left': -1200, 'top': -400, 'width': 400, 'height': 200}}
        captured = {'bounds': window().bounds}
        transform = helperctl.capture_image_transform(image, 'monitor_gdi_region', captured, monitor)
        self.assertEqual(simon_geometry.image_point_to_screen(transform, (20, 30)), (-1180, -370))
        for route in ('synthetic_visible_windows', 'window_pyautogui_region', 'window_pillow_bbox', 'unknown'):
            self.assertFalse(helperctl.capture_image_transform(image, route, captured, monitor)['verified'])
        self.assertFalse(helperctl.capture_image_transform(Image.new('RGB', (200, 100)), 'monitor_gdi_region', captured, monitor)['verified'])
        captured = {'hwnd': 101, 'bounds': {'left': -800, 'top': -300, 'width': 400, 'height': 200}}
        with patch.object(helperctl, 'get_foreground_window_snapshot', return_value={'hwnd': 202}):
            self.assertFalse(helperctl.capture_image_transform(image, 'window_gdi_region', captured, monitor)['verified'])

    def test_host_and_cli_share_capture_and_title_filtering(self):
        captured = window(left=-800, top=-300, right=-400, bottom=-100)
        app = agent_helper.AgentHelperApp.__new__(agent_helper.AgentHelperApp)
        app._record_capture_route_best_effort = mock.Mock()
        args = {'request_id': 'COMMON', 'target': 'target_window', 'title_contains': 'Example'}
        with patch.object(helperctl, 'find_best_window_for_args', return_value=captured) as select, \
             patch.object(helperctl, 'window_info_from_hwnd', return_value=captured) as by_handle, \
             patch.object(helperctl, 'capture_window_image_with_details', return_value=(Image.new('RGB', (400, 200)), {'method': 'window_gdi_region'})), \
             patch.object(helperctl, 'window_payload', return_value={'hwnd': captured.hwnd, 'bounds': captured.bounds}), \
             patch.object(helperctl, 'monitor_for_window', return_value=None), \
             patch.object(helperctl, 'get_foreground_window_snapshot', return_value={'hwnd': captured.hwnd}), \
             patch.object(helperctl, 'bring_window_to_front', return_value=True), \
             patch.object(helperctl, 'cancellable_sleep'), \
             patch.object(helperctl, 'build_active_context_packet_for_capture', return_value={}), \
             patch.object(helperctl, 'build_action_menu', return_value={}), \
             patch.object(helperctl, 'build_planner_coaching', return_value={}), \
             patch.object(helperctl, 'record_capture_route'):
            for identity in ({'title_contains': 'Example'}, {'exact_title': 'Example'}, {'hwnd': captured.hwnd}):
                payload = {**args, **identity}
                cli = helperctl.cmd_recovery_capture_grid(payload)
                host = app._external_capture_grid(payload, ui_prepared=True)
                self.assertEqual(cli['status'], 'NEEDS_PLANNER')
                self.assertEqual(host['data']['image_to_screen'], cli['data']['image_to_screen'])
        self.assertEqual(select.call_count, 6)
        self.assertEqual(select.call_args.args[0]['exact_title'], 'Example')
        self.assertEqual(by_handle.call_count, 3)
        self.assertEqual(app._record_capture_route_best_effort.call_count, 3)

    def preview_data(self):
        request = self.request()
        return {'planner_request': request, 'title': 'Example', 'screen_coordinate': {'x': -750, 'y': -250},
                'grid_coordinate': {'x': 50, 'y': 50}, 'cell_bbox': {'left': 0, 'top': 0, 'right': 100, 'bottom': 100}}

    def test_confirmation_is_action_bound_expiring_and_single_use(self):
        data = self.preview_data()
        args = {'request_id': 'ROOT', 'cell': 'A1', 'preview': True}
        command = 'recovery.guided_grid_click'
        with patch.object(helperctl.time, 'time', return_value=100):
            result = helperctl.guided_click_confirmation(args, data, command)
        self.assertFalse(result['data']['input_emitted'])
        token = result['data']['confirm_token']
        confirm = {'request_id': 'ROOT', 'cell': 'A1', 'confirm_token': token}
        with patch.object(helperctl.time, 'time', return_value=105):
            self.assertEqual(helperctl.guided_click_confirmation({**confirm, 'cell': 'B1'}, data, command)['status'], 'UNCERTAIN')
            self.assertIsNone(helperctl.guided_click_confirmation(confirm, data, command))
            self.assertEqual(helperctl.guided_click_confirmation(confirm, data, command)['status'], 'UNCERTAIN')
        with patch.object(helperctl.time, 'time', return_value=200):
            result = helperctl.guided_click_confirmation(args, data, command)
        with patch.object(helperctl.time, 'time', return_value=231):
            self.assertEqual(helperctl.guided_click_confirmation({**confirm, 'confirm_token': result['data']['confirm_token']}, data, command)['status'], 'UNCERTAIN')

    def test_preview_handler_emits_no_input_and_rechecks_movement(self):
        data = self.preview_data()
        data.update(request_id='ROOT', cell='A1', target='desktop')
        args = {'request_id': 'ROOT', 'cell': 'A1', 'preview': True}
        with patch.object(helperctl, 'prepare_guided_grid_click', return_value={'status': 'SUCCESS', 'data': data}), \
             patch.object(helperctl, 'capture_target', return_value={'status': 'SUCCESS'}), \
             patch.object(helperctl, 'verify_planner_screen_unchanged', return_value={'status': 'SUCCESS'}), \
             patch.object(helperctl, 'evaluate_click_guardrails', return_value=None), \
             patch.object(helperctl, 'current_window_for_planner', return_value=None), \
             patch.object(helperctl, 'cmd_input_click_point') as input_click:
            result = helperctl.cmd_recovery_guided_grid_click(args)
            self.assertEqual(result['status'], 'NEEDS_PLANNER')
            input_click.assert_not_called()
            with patch.object(helperctl, 'validate_planner_target_window', return_value='Window moved'):
                result = helperctl.cmd_recovery_guided_grid_click({**args, 'preview': False, 'confirm_token': result['data']['confirm_token']})
            self.assertEqual(result['data']['failure_type'], 'WINDOW_MOVED')
            input_click.assert_not_called()
            data['planner_request']['capture_timestamp'] = '2020-01-01T00:00:00'
            result = helperctl.cmd_recovery_guided_grid_click({**args, 'max_age_seconds': -1})
            self.assertEqual(result['data']['failure_type'], 'STALE_SCREENSHOT')
            input_click.assert_not_called()

    def test_planner_preview_rejects_unsupported_actions_and_never_restores_focus(self):
        with patch.object(helperctl, 'capture_planner_step_baseline', return_value={'status': 'SKIPPED'}) as baseline, \
             patch.object(helperctl, 'execute_planner_action', return_value={'status': 'NEEDS_PLANNER'}) as execute, \
             patch.object(helperctl, 'cmd_verify_window_state', return_value={'status': 'SUCCESS'}) as verify, \
             patch.object(helperctl, 'capture_planner_follow_up') as follow_up:
            for options in ({'preview': True}, {'confirm_token': 'token'}):
                result = helperctl.cmd_planner_step({'step': {'action': 'click_point'}, **options})
                self.assertEqual(result['status'], 'ERROR')
                self.assertFalse(result['data']['input_emitted'])
            baseline.assert_not_called()
            execute.assert_not_called()
            result = helperctl.cmd_planner_step({'preview': True, 'step': {'action': 'guided_grid_click', 'preview': False},
                                                'verify_before': {'focus_if_needed': True, 'allow_mouse_activation': True}})
            self.assertEqual(result['status'], 'NEEDS_PLANNER')
            self.assertTrue(execute.call_args.args[1]['preview'])
            self.assertFalse(verify.call_args.args[0]['focus_if_needed'])
            self.assertFalse(verify.call_args.args[0]['allow_mouse_activation'])
            follow_up.assert_not_called()


class FixedDateTime(datetime):
    """Clock used to keep naïve and timezone-aware staleness tests deterministic."""

    _instant = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):  # type: ignore[no-untyped-def]
        if tz is None:
            return cls._instant.replace(tzinfo=None)
        return cls._instant.astimezone(tz)


class ColumnAndGridGeometryTests(RuntimeGuardedTestCase):
    def test_column_labels_cross_single_and_multi_letter_boundaries(self) -> None:
        cases = {
            0: "A",
            25: "Z",
            26: "AA",
            27: "AB",
            51: "AZ",
            52: "BA",
            701: "ZZ",
            702: "AAA",
        }

        for index, expected in cases.items():
            with self.subTest(index=index):
                self.assertEqual(helperctl.column_label(index), expected)

    def test_grid_cell_bbox_supports_multi_letter_columns(self) -> None:
        test_grid = grid(columns=28, rows=3, cell_width=10.0, cell_height=20.0)

        self.assertEqual(helperctl.grid_cell_bbox("Z1", test_grid), (250, 0, 260, 20))
        self.assertEqual(helperctl.grid_cell_bbox("AA2", test_grid), (260, 20, 270, 40))
        self.assertEqual(helperctl.grid_cell_bbox("ab3", test_grid), (270, 40, 280, 60))
        self.assertIsNone(helperctl.grid_cell_bbox("AC1", test_grid))

    def test_grid_cell_bbox_requires_a_complete_column_then_row_token(self) -> None:
        test_grid = grid(columns=30, rows=20)
        malformed_cells = [
            "",
            "A",
            "1",
            "A0",
            "A-1",
            "1A",
            "A 1",
            "A1B2",
            "A1!",
        ]

        for cell in malformed_cells:
            with self.subTest(cell=cell):
                self.assertIsNone(helperctl.grid_cell_bbox(cell, test_grid))

    def test_grid_cell_bbox_rejects_invalid_grid_schema_without_raising(self) -> None:
        invalid_grids: list[dict[str, object]] = [
            {},
            {"columns": 2, "rows": 2, "cell_width": 10.0},
            grid(columns=0),
            grid(rows=0),
            grid(cell_width=0.0),
            grid(cell_height=-1.0),
            grid(cell_width=float("nan")),
            grid(cell_height=float("inf")),
            {"columns": "many", "rows": 2, "cell_width": 10.0, "cell_height": 10.0},
        ]

        for invalid_grid in invalid_grids:
            with self.subTest(grid=invalid_grid):
                self.assertIsNone(helperctl.grid_cell_bbox("A1", invalid_grid))

    def test_grid_cell_point_named_presets(self) -> None:
        test_grid = grid(columns=1, rows=1, cell_width=9.0, cell_height=9.0)
        expected = {
            "center": (4, 4),
            "left-center": (2, 4),
            "right-center": (6, 4),
            "top-center": (4, 2),
            "bottom-center": (4, 6),
            "top-left": (2, 2),
            "top-right": (6, 2),
            "bottom-left": (2, 6),
            "bottom-right": (6, 6),
        }

        for offset, point in expected.items():
            with self.subTest(offset=offset):
                self.assertEqual(
                    helperctl.grid_cell_point("A1", test_grid, offset=offset),
                    point,
                )

        self.assertEqual(helperctl.grid_cell_center("A1", test_grid), expected["center"])

    def test_grid_cell_point_clamps_custom_percentages_to_cell_pixels(self) -> None:
        test_grid = grid(columns=1, rows=1, cell_width=9.0, cell_height=9.0)
        for x_pct, y_pct, expected in ((-3.0, 4.0, (0, 8)), (1.0, 0.0, (8, 0)),
                                       (0.25, 0.75, (2, 6)), (150.0, -200.0, (8, 0))):
            with self.subTest(x_pct=x_pct, y_pct=y_pct):
                for command in ("recovery.guided_grid_click", "recovery.transient_grid_after_click", "planner.step"):
                    self.assertEqual(simon_schema.validate_arguments(command, {
                        "x_pct": x_pct, "y_pct": y_pct, "protocol_version": 2}), ([], []))
                self.assertEqual(helperctl.grid_cell_point("A1", test_grid, x_pct=x_pct, y_pct=y_pct), expected)
        for invalid in (float("nan"), float("inf"), float("-inf")):
            for key in ("x_pct", "y_pct"):
                _, errors = simon_schema.validate_arguments("recovery.guided_grid_click", {key: invalid})
                self.assertIn(f"{key!r} must be finite.", errors)


class CoordinateTranslationTests(RuntimeGuardedTestCase):
    def test_missing_or_synthetic_transform_never_authorizes_coordinates(self) -> None:
        for transform in ({}, {"verified": False, "offset_x": -300}):
            self.assertIsNone(helperctl.grid_coordinate_to_screen({"target": "desktop", "image_to_screen": transform}, (5, 7), None))

    def test_desktop_coordinate_includes_negative_virtual_origin_and_crop(self) -> None:
        planner_request = {
            "target": "desktop",
            "desktop_origin": {"x": -1920, "y": -1080},
            "crop": {"offset_x": 100, "offset_y": 50},
            "image_to_screen": {"verified": True, "width": 100, "height": 100, "scale_x": 1, "scale_y": 1, "offset_x": -1820, "offset_y": -1030},
        }

        self.assertEqual(
            helperctl.grid_coordinate_to_screen(planner_request, (10, 20), None),
            (-1810, -1010),
        )

    def test_window_coordinate_includes_negative_window_origin_and_crop(self) -> None:
        target_window = window(left=-800, top=-300, right=-200, bottom=400)
        planner_request = {
            "target": "target_window",
            "crop": {"offset_x": 25, "offset_y": 10},
            "target_window": {"hwnd": target_window.hwnd, "bounds": target_window.bounds},
            "image_to_screen": {"verified": True, "width": 100, "height": 100, "scale_x": 1, "scale_y": 1, "offset_x": -775, "offset_y": -290},
        }

        self.assertEqual(
            helperctl.grid_coordinate_to_screen(planner_request, (5, 7), target_window),
            (-770, -283),
        )
        self.assertIsNone(helperctl.grid_coordinate_to_screen(planner_request, (5, 7), None))


class PlannerWindowValidationTests(RuntimeGuardedTestCase):
    @staticmethod
    def _request(captured_window: helperctl.WindowInfo | None = None) -> dict[str, object]:
        request: dict[str, object] = {"target": "target_window"}
        if captured_window is not None:
            request["target_window"] = {
                "hwnd": captured_window.hwnd,
                "bounds": captured_window.bounds,
            }
        return request

    @patch.object(helperctl, "window_payload", side_effect=lambda current: {"bounds": current.bounds})
    def test_unchanged_target_window_is_accepted(self, _window_payload) -> None:
        captured = window(left=-500, top=40, right=100, bottom=440)

        self.assertEqual(helperctl.validate_planner_target_window(self._request(captured), captured), "")

    @patch.object(helperctl, "window_payload", side_effect=lambda current: {"bounds": current.bounds})
    def test_moved_target_window_is_rejected(self, _window_payload) -> None:
        captured = window(left=-500, top=40, right=100, bottom=440)
        moved = window(left=-499, top=40, right=101, bottom=440)

        reason = helperctl.validate_planner_target_window(self._request(captured), moved)

        self.assertIn("moved", reason.lower())

    def test_missing_current_target_window_is_rejected(self) -> None:
        captured = window()

        reason = helperctl.validate_planner_target_window(self._request(captured), None)

        self.assertIn("missing", reason.lower())

    def test_missing_captured_target_window_is_rejected(self) -> None:
        reason = helperctl.validate_planner_target_window({"target": "target_window"}, window())

        self.assertIn("did not record", reason.lower())


class PlannerStalenessTests(RuntimeGuardedTestCase):
    def test_naive_timestamp_can_be_fresh_or_stale(self) -> None:
        with patch.object(helperctl, "datetime", FixedDateTime):
            self.assertEqual(
                helperctl.planner_request_staleness(
                    {"capture_timestamp": "2026-07-20T11:59:45"},
                    60.0,
                ),
                "",
            )
            self.assertIn(
                "stale",
                helperctl.planner_request_staleness(
                    {"capture_timestamp": "2026-07-20T11:58:00"},
                    60.0,
                ).lower(),
            )

    def test_aware_timestamp_uses_its_offset_without_mixing_naive_datetimes(self) -> None:
        fresh_equivalent_timestamps = [
            "2026-07-20T11:59:45+00:00",
            "2026-07-20T06:59:45-05:00",
        ]

        with patch.object(helperctl, "datetime", FixedDateTime):
            for timestamp in fresh_equivalent_timestamps:
                with self.subTest(timestamp=timestamp):
                    self.assertEqual(
                        helperctl.planner_request_staleness(
                            {"capture_timestamp": timestamp},
                            60.0,
                        ),
                        "",
                    )
            self.assertIn(
                "stale",
                helperctl.planner_request_staleness(
                    {"capture_timestamp": "2026-07-20T11:58:00+00:00"},
                    60.0,
                ).lower(),
            )

    def test_invalid_timestamp_is_rejected_and_disabled_limit_skips_check(self) -> None:
        with patch.object(helperctl, "datetime", FixedDateTime):
            self.assertIn(
                "parseable",
                helperctl.planner_request_staleness(
                    {"capture_timestamp": "not-a-timestamp"},
                    60.0,
                ).lower(),
            )
            self.assertEqual(
                helperctl.planner_request_staleness(
                    {"capture_timestamp": "not-a-timestamp"},
                    0.0,
                ),
                "",
            )


class PlannerImageComparisonTests(RuntimeGuardedTestCase):
    def _save(self, directory: str, name: str, image: Image.Image) -> Path:
        path = Path(directory) / name
        image.save(path)
        return path

    def test_identical_images_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            baseline = Image.new("RGB", (24, 18), (20, 40, 60))
            baseline_path = self._save(directory, "baseline.png", baseline)

            result = helperctl.compare_planner_screen(
                {"raw_screenshot_path": str(baseline_path)},
                {"image": baseline.copy()},
                {},
            )

        self.assertEqual(result["status"], "SUCCESS")
        self.assertTrue(result["data"]["matched"])
        self.assertEqual(result["data"]["mean_delta"], 0.0)
        self.assertEqual(result["data"]["changed_ratio"], 0.0)

    def test_different_images_do_not_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            baseline_path = self._save(
                directory,
                "baseline.png",
                Image.new("RGB", (24, 18), "black"),
            )

            result = helperctl.compare_planner_screen(
                {"raw_screenshot_path": str(baseline_path)},
                {"image": Image.new("RGB", (24, 18), "white")},
                {},
            )

        self.assertEqual(result["status"], "FAILED")
        self.assertFalse(result["data"]["matched"])
        self.assertGreater(result["data"]["mean_delta"], 0.0)
        self.assertGreater(result["data"]["changed_ratio"], 0.0)

    def test_dimension_change_is_reported_before_pixel_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            baseline_path = self._save(
                directory,
                "baseline.png",
                Image.new("RGB", (12, 8), "black"),
            )

            result = helperctl.compare_planner_screen(
                {"raw_screenshot_path": str(baseline_path)},
                {"image": Image.new("RGB", (13, 8), "black")},
                {},
            )

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["data"]["reason"], "Screenshot dimensions changed.")
        self.assertEqual(result["data"]["original_size"], [12, 8])
        self.assertEqual(result["data"]["fresh_size"], [13, 8])

    def test_crop_compares_the_corresponding_fresh_region(self) -> None:
        crop = {"offset_x": 7, "offset_y": 4, "width": 10, "height": 8}
        crop_color = (20, 40, 60)
        fresh = Image.new("RGB", (30, 20), "white")
        fresh.paste(crop_color, (7, 4, 17, 12))

        with tempfile.TemporaryDirectory() as directory:
            baseline_path = self._save(
                directory,
                "crop.png",
                Image.new("RGB", (10, 8), crop_color),
            )
            planner_request = {
                "raw_screenshot_path": str(baseline_path),
                "crop": crop,
            }

            matched = helperctl.compare_planner_screen(
                planner_request,
                {"image": fresh},
                {},
            )
            changed_fresh = fresh.copy()
            changed_fresh.paste("red", (7, 4, 17, 12))
            changed = helperctl.compare_planner_screen(
                planner_request,
                {"image": changed_fresh},
                {},
            )

        self.assertEqual(matched["status"], "SUCCESS")
        self.assertEqual(matched["data"]["fresh_compared_size"], [10, 8])
        self.assertEqual(changed["status"], "FAILED")
        self.assertFalse(changed["data"]["matched"])


class FollowUpStateComparisonTests(RuntimeGuardedTestCase):
    @patch.object(
        helperctl,
        "compare_planner_screen",
        return_value={"status": "SUCCESS", "data": {"matched": True}},
    )
    def test_identical_state_and_image_report_no_change(self, compare_screen) -> None:
        baseline = capture_state()
        fresh = capture_state()

        result = helperctl.compare_follow_up_state_change(baseline, fresh, {})

        self.assertFalse(result["changed"])
        self.assertEqual(result["reasons"], [])
        compare_screen.assert_called_once_with(
            {"raw_screenshot_path": "baseline.png"},
            fresh,
            {},
        )

    @patch.object(
        helperctl,
        "compare_planner_screen",
        return_value={"status": "SUCCESS", "data": {"matched": True}},
    )
    def test_observed_window_change_is_reported_with_identical_image(self, _compare_screen) -> None:
        result = helperctl.compare_follow_up_state_change(
            capture_state(),
            capture_state(foreground_hwnd=202),
            {},
        )

        self.assertTrue(result["changed"])
        self.assertIn("foreground_hwnd_changed", result["reasons"])
        self.assertNotIn("image_changed", result["reasons"])

    @patch.object(
        helperctl,
        "compare_planner_screen",
        return_value={"status": "SUCCESS", "data": {"matched": False}},
    )
    def test_observed_image_difference_is_reported(self, _compare_screen) -> None:
        result = helperctl.compare_follow_up_state_change(
            capture_state(),
            capture_state(),
            {},
        )

        self.assertTrue(result["changed"])
        self.assertEqual(result["reasons"], ["image_changed"])

    @patch.object(
        helperctl,
        "compare_planner_screen",
        return_value={
            "status": "FAILED",
            "data": {"reason": "Original planner raw screenshot is missing."},
        },
    )
    def test_comparison_error_does_not_masquerade_as_observed_change(self, _compare_screen) -> None:
        result = helperctl.compare_follow_up_state_change(
            capture_state(),
            capture_state(),
            {},
        )

        self.assertFalse(result["changed"])
        self.assertEqual(result["reasons"], [])
        self.assertEqual(
            result["image_comparison"]["reason"],
            "Original planner raw screenshot is missing.",
        )


if __name__ == "__main__":
    unittest.main()
