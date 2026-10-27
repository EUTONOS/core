"""Single command/argument catalog for CLI, validation, discovery and docs."""
from __future__ import annotations

import difflib
import math
from typing import Any
from simon_verification import EFFECT_PROPERTIES, validate_expected_change

CLI_CONTROLS = {
    "via_app": {"type": "bool", "description": "Execute through the resident Helper."},
    "pretty": {"type": "bool", "description": "Indent JSON for humans."},
    "json": {"type": "str", "description": "JSON object merged into explicit CLI arguments."},
    "json_file": {"type": "str", "description": "File containing a JSON argument object."},
}
SHARED_ARGS = ['detail', 'reason', 'expected_result', 'failure_type', 'request_id', 'goal', 'intent', 'target_description', 'timeout', 'bridge_stale_seconds', 'bridge_request_id', 'skip_helper_precheck', 'allow_vm_control', 'allow_path_text', 'strict_args', 'protocol_version', 'include_images', 'image_max_edge', 'max_age_seconds']
ARGUMENTS = {
    'action': {'type': 'str', 'description': 'Planner action name.'},
    'enforce_focus_guard': {'type': 'bool', 'description': 'Apply the sequence focus guard to this step.'},
    'remember_target': {'type': 'bool', 'description': 'Remember the sequence step target for later focus guards.'},
    'based_on_request_id': {'type': 'str', 'description': 'Observation used for action freshness verification.'},
    'bridge_request_id': {'type': 'str', 'description': 'Explicit bridge request identity.'},
    'planner_request_id': {'type': 'str', 'description': 'Observation used for action freshness verification.'},
    'observation_request_id': {'type': 'str', 'description': 'Observation used for action freshness verification.'},
    'max_age_seconds': {'type': 'number', 'description': 'Maximum age of a grid request before cropping or clicking.'},
    'actions': {'type': 'array', 'description': 'Actions.'},
    'allow_elevation': {'type': 'bool', 'description': 'Allow elevation.'},
    'allow_minimized': {'type': 'bool', 'description': 'Allow minimized.'},
    'allow_mouse_activation': {'type': 'bool', 'description': 'Allow mouse activation.'},
    'allow_non_dialog_hwnd': {'type': 'bool', 'description': 'Allow non dialog hwnd.'},
    'allow_non_text_field': {'type': 'bool', 'description': 'Allow non text field.'},
    'allow_path_text': {'type': 'bool', 'description': 'Allow path text.'},
    'allow_sandbox': {'type': 'bool', 'description': 'Allow sandbox.'},
    'allow_vm_control': {'type': 'bool', 'description': 'Allow vm control.'},
    'allow_window_anchor': {'type': 'bool', 'description': 'Allow window anchor.'},
    'auto_follow_up_capture': {'type': 'bool', 'description': 'Auto follow up capture.'},
    'bridge_stale_seconds': {'type': 'number', 'description': 'Bridge stale seconds.'},
    'candidate_hwnd': {'type': 'int', 'description': 'Candidate hwnd.'},
    'candidate_id': {'type': 'int', 'description': 'Candidate id.'},
    'cell': {'type': 'str', 'description': 'Cell.'},
    'change_poll_seconds': {'type': 'number', 'description': 'Change poll seconds.'},
    'change_settle_seconds': {'type': 'number', 'description': 'Change settle seconds.'},
    'change_timeout_seconds': {'type': 'number', 'description': 'Change timeout seconds.'},
    'changed_pixel_threshold': {'type': 'int', 'description': 'Changed pixel threshold.'},
    'checked': {'type': 'bool', 'description': 'Checked.'},
    'columns': {'type': 'int', 'description': 'Columns.'},
    'compare_width': {'type': 'int', 'description': 'Compare width.'},
    'coordinate_mode': {'type': 'str', 'description': 'Coordinate mode.', 'choices': ['window', 'screen']},
    'crop_columns': {'type': 'int', 'description': 'Crop columns.'},
    'crop_rows': {'type': 'int', 'description': 'Crop rows.'},
    'current_dialog': {'type': 'object', 'description': 'Current dialog.'},
    'current_dialog_hwnd': {'type': 'int', 'description': 'Current dialog hwnd.'},
    'desktop_filename': {'type': 'str', 'description': 'Bare filename in the current Windows Known Folder Desktop, including redirection.'},
    'expected_sha256': {'type': 'str', 'description': 'Exact SHA-256 of expected file bytes; bounded regular-file content check, not save-transition proof.'},
    'detail': {'type': 'str', 'description': 'Detail.', 'choices': ['minimal', 'standard', 'full']},
    'dialog_hwnd': {'type': 'int', 'description': 'Dialog hwnd.'},
    'dry_run': {'type': 'bool', 'description': 'Dry run.'},
    'exact_title': {'type': 'str', 'description': 'Exact title.'},
    'expected_change': {'type': 'object', 'description': 'Typed effect, checked before input; unsupported or unmet effects fail closed.', 'properties': EFFECT_PROPERTIES},
    'expected_kind': {'type': 'str', 'description': 'Expected kind.'},
    'expected_result': {'type': 'str', 'description': 'Expected result.'},
    'failed_command': {'type': 'str', 'description': 'Failed command.'},
    'failure_type': {'type': 'str', 'description': 'Failure type.'},
    'file_path': {'type': 'str', 'description': 'File path.'},
    'focus_guard': {'type': 'bool', 'description': 'Focus guard.'},
    'focus_if_needed': {'type': 'bool', 'description': 'Focus if needed.'},
    'follow_up_hwnd': {'type': 'int', 'description': 'Follow up hwnd.'},
    'follow_up_target': {'type': 'str', 'description': 'Follow up target.'},
    'goal': {'type': 'str', 'description': 'Goal.'},
    'hwnd': {'type': 'int', 'description': 'Hwnd.'},
    'image_max_edge': {'type': 'int', 'description': 'Image max edge.'},
    'include_capture': {'type': 'bool', 'description': 'Include capture.'},
    'include_images': {'type': 'array', 'description': 'Include images.'},
    'index': {'type': 'int', 'description': 'Index.'},
    'intent': {'type': 'str', 'description': 'Intent.'},
    'keys': {'type': 'str', 'description': 'Keys.'},
    'keys_after_click': {'type': 'str', 'description': 'Keys after click.'},
    'max_age_days': {'type': 'number', 'description': 'Max age days.'},
    'max_bytes': {'type': 'int', 'description': 'Max bytes.'},
    'max_changed_ratio': {'type': 'number', 'description': 'Max changed ratio.'},
    'max_groups': {'type': 'int', 'description': 'Max groups.'},
    'max_mean_delta': {'type': 'number', 'description': 'Max mean delta.'},
    'min_height': {'type': 'int', 'description': 'Min height.'},
    'min_size_bytes': {'type': 'int', 'description': 'Min size bytes.'},
    'min_width': {'type': 'int', 'description': 'Min width.'},
    'mode': {'type': 'str', 'description': 'Mode.', 'choices': ['normal', 'regular', 'elevated']},
    'observe_after': {'type': 'object', 'description': 'Observe after.'},
    'offset': {'type': 'str', 'description': 'Offset.'},
    'path': {'type': 'str', 'description': 'Path.'},
    'post_action_delay': {'type': 'number', 'description': 'Post action delay.'},
    'post_click_delay': {'type': 'number', 'description': 'Post click delay.'},
    'protocol_version': {'type': 'int', 'description': 'Protocol version.', 'choices': [1, 2]},
    'query': {'type': 'str', 'description': 'Query.'},
    'question': {'type': 'str', 'description': 'Question.'},
    'reason': {'type': 'str', 'description': 'Reason.'},
    'remembered_window': {'type': 'object', 'description': 'Remembered window.'},
    'request_id': {'type': 'str', 'description': 'Request id.'},
    'require_foreground': {'type': 'bool', 'description': 'Require foreground.'},
    'rows': {'type': 'int', 'description': 'Rows.'},
    'screenshot_on_failure': {'type': 'bool', 'description': 'Screenshot on failure.'},
    'search_settle_seconds': {'type': 'number', 'description': 'Search settle seconds.'},
    'seconds': {'type': 'number', 'description': 'Seconds.'},
    'should_exist': {'type': 'bool', 'description': 'Should exist.'},
    'skip_helper_precheck': {'type': 'bool', 'description': 'Skip helper precheck.'},
    'skip_screen_unchanged_check': {'type': 'bool', 'description': 'Skip screen unchanged check.'},
    'start_settle_seconds': {'type': 'number', 'description': 'Start settle seconds.'},
    'step': {'type': 'object', 'description': 'Step.'},
    'strict_args': {'type': 'bool', 'description': 'Strict args.'},
    'target': {'type': 'str', 'description': 'Target.', 'choices': ['target_window', 'desktop', 'foreground_window', 'active_window', 'current_dialog', 'target_monitor', 'foreground_monitor', 'active_monitor', 'current_dialog_monitor']},
    'target_description': {'type': 'str', 'description': 'Target description.'},
    'target_window': {'type': 'object', 'description': 'Target window.'},
    'text': {'type': 'str', 'description': 'Text.'},
    'text_after_click': {'type': 'str', 'description': 'Text after click.'},
    'timeout': {'type': 'number', 'description': 'Timeout.'},
    'title': {'type': 'str', 'description': 'Title.'},
    'title_contains': {'type': 'str_or_list', 'description': 'Title contains.'},
    'title_excludes': {'type': 'str_or_list', 'description': 'Title excludes.'},
    'use_last_observation_request': {'type': 'bool', 'description': 'Use last observation request.'},
    'value': {'type': 'str', 'description': 'Value.'},
    'verification': {'type': 'object', 'description': 'Verification.'},
    'verify_before': {'type': 'object', 'description': 'Verify before.'},
    'wait_for_change': {'type': 'bool', 'description': 'Wait for change.'},
    'wait_seconds': {'type': 'number', 'description': 'Wait seconds.'},
    'window_hwnd': {'type': 'int', 'description': 'Window hwnd.'},
    'working_window': {'type': 'object', 'description': 'Working window.'},
    'x': {'type': 'number', 'description': 'X.'},
    'x_pct': {'type': 'number', 'description': 'Horizontal fraction of the cell (0.0 to 1.0); finite values outside that range are clamped. Supply with y_pct.'},
    'y': {'type': 'number', 'description': 'Y.'},
    'y_pct': {'type': 'number', 'description': 'Vertical fraction of the cell (0.0 to 1.0); finite values outside that range are clamped. Supply with x_pct.'},
}

# Verification is an executable payload: window_state may restore focus. Its
# booleans must receive the same checks as top-level command arguments.
ARGUMENTS["verification"]["properties"] = {
    **{key: ARGUMENTS[key] for key in ("exact_title", "title", "title_contains", "title_excludes",
                                      "min_width", "min_height", "allow_minimized",
                                      "require_foreground", "focus_if_needed")},
    "type": {"type": "str", "description": "Verification rule name."},
    "before_index": {"type": "int", "description": "Prior Simon progress index."},
    "expected_color": {"type": "str", "description": "Expected Simon target color."},
    "expected_foreground": {"type": "object", "description": "Recorded focus-guard window evidence."},
}
ARGUMENTS.update({
    "preview": {"type": "bool", "description": "Return a crosshair preview and 30-second confirmation token; emit no input."},
    "confirm_token": {"type": "str", "description": "Confirm the exact previewed action once, after fresh-screen and window checks."},
})

COMMANDS = {
    'helper.commands': {'handler': 'cmd_helper_commands', 'args': [], 'authority': 'read_only', 'description': 'Helper commands.', 'example': 'python helperctl.py helper.commands', 'available': True, 'defaults': {'detail': 'minimal'}},
    'helper.bridge_status': {'handler': 'cmd_helper_bridge_status', 'args': [], 'authority': 'read_only', 'description': 'Helper bridge status.', 'example': 'python helperctl.py helper.bridge_status', 'available': True, 'defaults': {'detail': 'minimal'}},
    'diagnose.environment': {'handler': 'cmd_diagnose_environment', 'args': ['include_capture'], 'authority': 'read_only', 'description': 'Diagnose environment.', 'example': 'python helperctl.py diagnose.environment', 'available': True, 'defaults': {'detail': 'minimal'}},
    'recovery.stop_screensaver': {'handler': 'cmd_stop_screensaver', 'args': [], 'authority': 'lifecycle', 'description': 'Recovery stop screensaver.', 'example': 'python helperctl.py recovery.stop_screensaver', 'available': True, 'defaults': {'detail': 'minimal'}},
    'helper.start': {'handler': 'cmd_helper_start', 'args': ['allow_elevation', 'allow_sandbox', 'mode', 'wait_seconds'], 'authority': 'lifecycle', 'description': 'Start a normal Helper; elevation requires allow_elevation=true.', 'example': 'python helperctl.py helper.start', 'available': True, 'defaults': {'detail': 'minimal', 'allow_elevation': False}},
    'helper.start_elevated': {'handler': 'cmd_helper_start_elevated', 'args': ['allow_elevation', 'allow_sandbox', 'mode', 'wait_seconds'], 'authority': 'elevated', 'description': 'Request elevated launch only with explicit allow_elevation=true; UAC still applies.', 'example': 'python helperctl.py helper.start_elevated', 'available': True, 'defaults': {'detail': 'minimal', 'allow_elevation': False}},
    'helper.exit': {'handler': 'cmd_helper_exit', 'args': [], 'authority': 'lifecycle', 'description': 'Exit the resident Helper; verify stopped status and process exit.', 'example': 'python helperctl.py helper.exit', 'available': True, 'defaults': {'detail': 'minimal'}},
    'status.current': {'handler': 'cmd_status_current', 'args': [], 'authority': 'read_only', 'description': 'Status current.', 'example': 'python helperctl.py status.current', 'available': True, 'defaults': {'detail': 'minimal'}},
    'detect.vm_boundary': {'handler': 'cmd_detect_vm_boundary', 'args': ['allow_minimized', 'allow_non_dialog_hwnd', 'current_dialog', 'current_dialog_hwnd', 'dialog_hwnd', 'exact_title', 'hwnd', 'min_height', 'min_width', 'target', 'target_window', 'title', 'title_contains', 'title_excludes', 'window_hwnd', 'working_window'], 'authority': 'read_only', 'description': 'Detect vm boundary.', 'example': 'python helperctl.py detect.vm_boundary', 'available': True, 'defaults': {'detail': 'minimal'}},
    'observe.windows': {'handler': 'cmd_observe_windows', 'args': ['title'], 'authority': 'read_only', 'description': 'Observe windows.', 'example': 'python helperctl.py observe.windows', 'available': True, 'defaults': {'detail': 'minimal'}},
    'observe.window': {'handler': 'cmd_observe_window', 'args': ['allow_minimized', 'exact_title', 'min_height', 'min_width', 'title', 'title_contains', 'title_excludes'], 'authority': 'read_only', 'description': 'Observe window.', 'example': 'python helperctl.py observe.window', 'available': True, 'defaults': {'detail': 'minimal'}},
    'observe.screenshot': {'handler': 'cmd_observe_screenshot', 'args': ['allow_minimized', 'allow_non_dialog_hwnd', 'columns', 'current_dialog', 'current_dialog_hwnd', 'dialog_hwnd', 'exact_title', 'hwnd', 'min_height', 'min_width', 'rows', 'step', 'target', 'target_window', 'title', 'title_contains', 'title_excludes', 'window_hwnd', 'working_window'], 'authority': 'read_only', 'description': 'Observe screenshot.', 'example': 'python helperctl.py observe.screenshot', 'available': True, 'defaults': {'detail': 'minimal'}},
    'observe.ocr': {'handler': 'cmd_observe_ocr', 'args': ['allow_minimized', 'allow_non_dialog_hwnd', 'columns', 'current_dialog', 'current_dialog_hwnd', 'dialog_hwnd', 'exact_title', 'hwnd', 'min_height', 'min_width', 'rows', 'step', 'target', 'target_window', 'title', 'title_contains', 'title_excludes', 'window_hwnd', 'working_window'], 'authority': 'read_only', 'description': 'Unavailable until the deferred OCR decision; use a screenshot.', 'example': 'python helperctl.py observe.ocr', 'available': False, 'defaults': {'detail': 'minimal'}},
    'planner.fast_observe': {'handler': 'cmd_planner_fast_observe', 'args': ['allow_minimized', 'allow_non_dialog_hwnd', 'columns', 'current_dialog', 'current_dialog_hwnd', 'dialog_hwnd', 'exact_title', 'failed_command', 'hwnd', 'min_height', 'min_width', 'rows', 'step', 'target', 'target_window', 'title', 'title_contains', 'title_excludes', 'verification', 'window_hwnd', 'working_window'], 'authority': 'read_only', 'description': 'Capture the active app; prefer candidate actions, then label-based grid targeting.', 'example': 'python helperctl.py planner.fast_observe', 'available': True, 'defaults': {'detail': 'minimal', 'target': 'desktop', 'columns': 12, 'rows': 8}},
    'planner.step': {'handler': 'cmd_planner_step', 'args': ['action', 'based_on_request_id', 'planner_request_id', 'observation_request_id', 'allow_minimized', 'allow_mouse_activation', 'allow_non_dialog_hwnd', 'allow_non_text_field', 'allow_window_anchor', 'auto_follow_up_capture', 'candidate_hwnd', 'candidate_id', 'cell', 'change_poll_seconds', 'change_settle_seconds', 'change_timeout_seconds', 'changed_pixel_threshold', 'checked', 'columns', 'compare_width', 'coordinate_mode', 'current_dialog', 'current_dialog_hwnd', 'desktop_filename', 'dialog_hwnd', 'exact_title', 'expected_change', 'expected_kind', 'file_path', 'focus_if_needed', 'follow_up_hwnd', 'follow_up_target', 'hwnd', 'index', 'keys', 'keys_after_click', 'max_changed_ratio', 'max_mean_delta', 'min_height', 'min_width', 'observe_after', 'offset', 'path', 'post_action_delay', 'post_click_delay', 'require_foreground', 'rows', 'screenshot_on_failure', 'seconds', 'skip_screen_unchanged_check', 'step', 'target', 'target_window', 'text', 'text_after_click', 'title', 'title_contains', 'title_excludes', 'use_last_observation_request', 'value', 'verification', 'verify_before', 'wait_for_change', 'window_hwnd', 'working_window', 'x', 'x_pct', 'y', 'y_pct'], 'authority': 'input', 'description': 'Perform one bounded action, verify when possible, then observe the next state.', 'example': 'python helperctl.py planner.step', 'available': True, 'defaults': {'detail': 'minimal'}},
    'verify.file_state': {'handler': 'cmd_verify_file_state', 'args': ['desktop_filename', 'expected_sha256', 'file_path', 'min_size_bytes', 'path', 'should_exist'], 'authority': 'read_only', 'description': 'Verify file state and optionally exact content; does not establish a save transition.', 'example': 'python helperctl.py verify.file_state', 'available': True, 'defaults': {'detail': 'minimal'}},
    'verify.window_state': {'handler': 'cmd_verify_window_state', 'args': ['allow_minimized', 'allow_non_dialog_hwnd', 'allow_window_anchor', 'columns', 'current_dialog', 'current_dialog_hwnd', 'dialog_hwnd', 'exact_title', 'focus_if_needed', 'hwnd', 'min_height', 'min_width', 'require_foreground', 'rows', 'screenshot_on_failure', 'step', 'target', 'target_window', 'title', 'title_contains', 'title_excludes', 'verification', 'window_hwnd', 'working_window'], 'authority': 'read_only', 'description': 'Verify window state.', 'example': 'python helperctl.py verify.window_state', 'available': True, 'defaults': {'detail': 'minimal'}},
    'desktop.start_search_grid': {'handler': 'cmd_desktop_start_search_grid', 'args': ['allow_minimized', 'allow_non_dialog_hwnd', 'columns', 'current_dialog', 'current_dialog_hwnd', 'dialog_hwnd', 'exact_title', 'failed_command', 'hwnd', 'min_height', 'min_width', 'post_click_delay', 'query', 'question', 'rows', 'search_settle_seconds', 'start_settle_seconds', 'step', 'target', 'target_window', 'text', 'title', 'title_contains', 'title_excludes', 'verification', 'window_hwnd', 'working_window'], 'authority': 'input', 'description': 'Desktop start search grid.', 'example': 'python helperctl.py desktop.start_search_grid', 'available': True, 'defaults': {'detail': 'minimal'}},
    'focus.window': {'handler': 'cmd_focus_window', 'args': ['allow_minimized', 'allow_mouse_activation', 'allow_window_anchor', 'exact_title', 'hwnd', 'min_height', 'min_width', 'target_window', 'title', 'title_contains', 'title_excludes', 'window_hwnd', 'working_window'], 'authority': 'input', 'description': 'Focus window.', 'example': 'python helperctl.py focus.window', 'available': True, 'defaults': {'detail': 'minimal'}},
    'input.action_sequence': {'handler': 'cmd_input_action_sequence', 'args': ['actions', 'allow_minimized', 'allow_non_dialog_hwnd', 'columns', 'current_dialog', 'current_dialog_hwnd', 'dialog_hwnd', 'exact_title', 'focus_guard', 'hwnd', 'min_height', 'min_width', 'remembered_window', 'rows', 'step', 'target', 'target_window', 'title', 'title_contains', 'title_excludes', 'verification', 'window_hwnd', 'working_window'], 'authority': 'input', 'description': "Verify each declared step effect; stop at the first unresolved input. Wait/observation-only steps may continue.", 'example': 'python helperctl.py input.action_sequence', 'available': True, 'defaults': {'detail': 'minimal'}},
    'input.expand_candidate': {'handler': 'cmd_input_expand_candidate', 'args': ['allow_minimized', 'allow_non_dialog_hwnd', 'candidate_hwnd', 'candidate_id', 'columns', 'current_dialog', 'current_dialog_hwnd', 'dialog_hwnd', 'exact_title', 'expected_kind', 'hwnd', 'min_height', 'min_width', 'post_action_delay', 'rows', 'step', 'target', 'target_window', 'title', 'title_contains', 'title_excludes', 'window_hwnd', 'working_window'], 'authority': 'input', 'description': 'Input expand candidate.', 'example': 'python helperctl.py input.expand_candidate', 'available': True, 'defaults': {'detail': 'minimal'}},
    'input.click_candidate': {'handler': 'cmd_input_click_candidate', 'args': ['allow_minimized', 'allow_non_dialog_hwnd', 'candidate_hwnd', 'candidate_id', 'columns', 'current_dialog', 'current_dialog_hwnd', 'dialog_hwnd', 'exact_title', 'expected_kind', 'hwnd', 'min_height', 'min_width', 'post_click_delay', 'rows', 'step', 'target', 'target_window', 'title', 'title_contains', 'title_excludes', 'window_hwnd', 'working_window'], 'authority': 'input', 'description': 'Input click candidate.', 'example': 'python helperctl.py input.click_candidate', 'available': True, 'defaults': {'detail': 'minimal'}},
    'input.click_point': {'handler': 'cmd_input_click_point', 'args': ['allow_minimized', 'allow_mouse_activation', 'allow_non_dialog_hwnd', 'allow_window_anchor', 'coordinate_mode', 'current_dialog', 'current_dialog_hwnd', 'dialog_hwnd', 'exact_title', 'hwnd', 'min_height', 'min_width', 'target', 'target_window', 'title', 'title_contains', 'title_excludes', 'window_hwnd', 'working_window', 'x', 'y'], 'authority': 'input', 'description': 'Input click point.', 'example': 'python helperctl.py input.click_point', 'available': True, 'defaults': {'detail': 'minimal'}},
    'input.hotkey': {'handler': 'cmd_input_hotkey', 'args': ['allow_minimized', 'allow_mouse_activation', 'allow_non_dialog_hwnd', 'allow_window_anchor', 'current_dialog', 'current_dialog_hwnd', 'dialog_hwnd', 'exact_title', 'hwnd', 'keys', 'min_height', 'min_width', 'target', 'target_window', 'title', 'title_contains', 'title_excludes', 'window_hwnd', 'working_window'], 'authority': 'input', 'description': 'Input hotkey.', 'example': 'python helperctl.py input.hotkey', 'available': True, 'defaults': {'detail': 'minimal'}},
    'input.invoke_candidate': {'handler': 'cmd_input_invoke_candidate', 'args': ['allow_minimized', 'allow_non_dialog_hwnd', 'candidate_hwnd', 'candidate_id', 'columns', 'current_dialog', 'current_dialog_hwnd', 'dialog_hwnd', 'exact_title', 'expected_kind', 'hwnd', 'min_height', 'min_width', 'post_action_delay', 'rows', 'step', 'target', 'target_window', 'title', 'title_contains', 'title_excludes', 'window_hwnd', 'working_window'], 'authority': 'input', 'description': 'Input invoke candidate.', 'example': 'python helperctl.py input.invoke_candidate', 'available': True, 'defaults': {'detail': 'minimal'}},
    'input.paste_text': {'handler': 'cmd_input_paste_text', 'args': ['allow_minimized', 'allow_mouse_activation', 'allow_non_dialog_hwnd', 'allow_window_anchor', 'current_dialog', 'current_dialog_hwnd', 'dialog_hwnd', 'exact_title', 'hwnd', 'min_height', 'min_width', 'target', 'target_window', 'text', 'title', 'title_contains', 'title_excludes', 'window_hwnd', 'working_window'], 'authority': 'input', 'description': 'Paste into a grounded focused field; Save location and filename are separate steps.', 'example': 'python helperctl.py input.paste_text', 'available': True, 'defaults': {'detail': 'minimal'}},
    'input.select_candidate': {'handler': 'cmd_input_select_candidate', 'args': ['allow_minimized', 'allow_non_dialog_hwnd', 'candidate_hwnd', 'candidate_id', 'checked', 'columns', 'current_dialog', 'current_dialog_hwnd', 'dialog_hwnd', 'exact_title', 'expected_kind', 'hwnd', 'index', 'min_height', 'min_width', 'post_action_delay', 'rows', 'step', 'target', 'target_window', 'title', 'title_contains', 'title_excludes', 'window_hwnd', 'working_window'], 'authority': 'input', 'description': 'Input select candidate.', 'example': 'python helperctl.py input.select_candidate', 'available': True, 'defaults': {'detail': 'minimal'}},
    'input.set_candidate_value': {'handler': 'cmd_input_set_candidate_value', 'args': ['allow_minimized', 'allow_non_dialog_hwnd', 'allow_non_text_field', 'candidate_hwnd', 'candidate_id', 'columns', 'current_dialog', 'current_dialog_hwnd', 'dialog_hwnd', 'exact_title', 'expected_kind', 'hwnd', 'min_height', 'min_width', 'post_action_delay', 'rows', 'step', 'target', 'target_window', 'text', 'title', 'title_contains', 'title_excludes', 'value', 'window_hwnd', 'working_window'], 'authority': 'input', 'description': 'Input set candidate value.', 'example': 'python helperctl.py input.set_candidate_value', 'available': True, 'defaults': {'detail': 'minimal'}},
    'input.type_text': {'handler': 'cmd_input_type_text', 'args': ['allow_minimized', 'allow_mouse_activation', 'allow_non_dialog_hwnd', 'allow_window_anchor', 'current_dialog', 'current_dialog_hwnd', 'dialog_hwnd', 'exact_title', 'hwnd', 'min_height', 'min_width', 'target', 'target_window', 'text', 'title', 'title_contains', 'title_excludes', 'window_hwnd', 'working_window'], 'authority': 'input', 'description': 'Input type text.', 'example': 'python helperctl.py input.type_text', 'available': True, 'defaults': {'detail': 'minimal'}},
    'recovery.capture_grid': {'handler': 'cmd_recovery_capture_grid', 'args': ['allow_minimized', 'allow_non_dialog_hwnd', 'columns', 'current_dialog', 'current_dialog_hwnd', 'dialog_hwnd', 'exact_title', 'failed_command', 'hwnd', 'min_height', 'min_width', 'rows', 'step', 'target', 'target_window', 'title', 'title_contains', 'title_excludes', 'verification', 'window_hwnd', 'working_window'], 'authority': 'read_only', 'description': 'Recovery capture grid.', 'example': 'python helperctl.py recovery.capture_grid', 'available': True, 'defaults': {'detail': 'minimal', 'columns': 12, 'rows': 8, 'target': 'target_window'}},
    'recovery.guided_grid_crop': {'handler': 'cmd_recovery_guided_grid_crop', 'args': ['cell', 'crop_columns', 'crop_rows', 'title'], 'authority': 'read_only', 'description': 'Crop a fresh cell; use crop_columns/crop_rows, then target labels.', 'example': 'python helperctl.py recovery.guided_grid_crop', 'available': True, 'defaults': {'detail': 'minimal'}},
    'recovery.guided_grid_click': {'handler': 'cmd_recovery_guided_grid_click', 'args': ['allow_minimized', 'allow_mouse_activation', 'allow_non_dialog_hwnd', 'allow_window_anchor', 'auto_follow_up_capture', 'cell', 'changed_pixel_threshold', 'columns', 'compare_width', 'coordinate_mode', 'current_dialog', 'current_dialog_hwnd', 'dialog_hwnd', 'exact_title', 'focus_if_needed', 'hwnd', 'keys', 'keys_after_click', 'max_changed_ratio', 'max_mean_delta', 'min_height', 'min_width', 'observe_after', 'offset', 'post_click_delay', 'require_foreground', 'rows', 'screenshot_on_failure', 'skip_screen_unchanged_check', 'step', 'target', 'target_window', 'text', 'text_after_click', 'title', 'title_contains', 'title_excludes', 'verification', 'window_hwnd', 'working_window', 'x', 'x_pct', 'y', 'y_pct'], 'authority': 'input', 'description': 'Click one fresh grid label with verification; never infer pixel coordinates from a resized image.', 'example': 'python helperctl.py recovery.guided_grid_click', 'available': True, 'defaults': {'detail': 'minimal'}},
    'recovery.transient_grid_after_click': {'handler': 'cmd_recovery_transient_grid_after_click', 'args': ['allow_minimized', 'allow_mouse_activation', 'allow_non_dialog_hwnd', 'allow_window_anchor', 'cell', 'changed_pixel_threshold', 'columns', 'compare_width', 'coordinate_mode', 'current_dialog', 'current_dialog_hwnd', 'dialog_hwnd', 'exact_title', 'failed_command', 'hwnd', 'max_changed_ratio', 'max_mean_delta', 'min_height', 'min_width', 'offset', 'post_click_delay', 'question', 'rows', 'skip_screen_unchanged_check', 'step', 'target', 'target_window', 'title', 'title_contains', 'title_excludes', 'verification', 'window_hwnd', 'working_window', 'x', 'x_pct', 'y', 'y_pct'], 'authority': 'input', 'description': 'Recovery transient grid after click.', 'example': 'python helperctl.py recovery.transient_grid_after_click', 'available': True, 'defaults': {'detail': 'minimal'}},
    'simon.detect_state': {'handler': 'cmd_simon_detect_state', 'args': ['title'], 'authority': 'read_only', 'description': 'Simon detect state.', 'example': 'python helperctl.py simon.detect_state', 'available': True, 'defaults': {'detail': 'minimal'}},
    'simon.press_next': {'handler': 'cmd_simon_press_next', 'args': ['allow_minimized', 'allow_mouse_activation', 'allow_window_anchor', 'exact_title', 'hwnd', 'min_height', 'min_width', 'target_window', 'title', 'title_contains', 'title_excludes', 'window_hwnd', 'working_window'], 'authority': 'input', 'description': 'Simon press next.', 'example': 'python helperctl.py simon.press_next', 'available': True, 'defaults': {'detail': 'minimal'}},
    'simon.force_grid_request': {'handler': 'cmd_simon_force_grid_request', 'args': ['allow_minimized', 'allow_non_dialog_hwnd', 'columns', 'current_dialog', 'current_dialog_hwnd', 'dialog_hwnd', 'exact_title', 'hwnd', 'min_height', 'min_width', 'rows', 'step', 'target', 'target_window', 'title', 'title_contains', 'title_excludes', 'verification', 'window_hwnd', 'working_window'], 'authority': 'read_only', 'description': 'Simon force grid request.', 'example': 'python helperctl.py simon.force_grid_request', 'available': True, 'defaults': {'detail': 'minimal'}},
    'runtime.cleanup': {'handler': 'cmd_runtime_cleanup', 'args': ['dry_run', 'max_age_days', 'max_bytes', 'max_groups'], 'authority': 'lifecycle', 'description': 'Preview or apply retention to registered Simon artifacts only; preserves active/recent requests and unrelated files.', 'example': 'python helperctl.py runtime.cleanup', 'available': True, 'defaults': {'detail': 'minimal', 'dry_run': True, 'max_age_days': 7, 'max_groups': 100, 'max_bytes': 268435456}},
}


for _command in ("recovery.guided_grid_click", "recovery.transient_grid_after_click", "planner.step"):
    COMMANDS[_command]["args"].extend(["preview", "confirm_token"])


def command_catalog() -> dict[str, dict[str, Any]]:
    return {
        name: {**{k: v for k, v in spec.items() if k != "handler"},
               "args": {key: ARGUMENTS[key] for key in sorted(set(SHARED_ARGS) | set(spec["args"]))}}
        for name, spec in COMMANDS.items()
    }


def validate_arguments(command: str, args: dict[str, Any], *, _nested: bool = False,
                       _properties: dict[str, Any] | None = None) -> tuple[list[str], list[str]]:
    spec = COMMANDS.get(command)
    if spec is None:
        return [], [f"Unknown command {command!r}."]
    if not isinstance(args, dict):
        return [], ["Command arguments must be a JSON object."]
    arguments = ARGUMENTS if _properties is None else {
        **_properties, **{key: ARGUMENTS[key] for key in ("strict_args", "protocol_version")}}
    known = set(arguments) if _nested else set(SHARED_ARGS) | set(spec["args"])
    warnings, errors = [], []
    for key, value in args.items():
        if key not in known:
            suggestion = {"columns": "crop_columns", "rows": "crop_rows"}.get(key) if command == "recovery.guided_grid_crop" else None
            matches = difflib.get_close_matches(key, known, n=1, cutoff=0.5)
            suggestion = suggestion or (matches[0] if matches else None)
            message = f"Unknown argument {key!r} for {command}"
            if suggestion:
                message += f"; did you mean {suggestion!r}?"
            (errors if args.get("strict_args") is True or args.get("protocol_version") == 2 else warnings).append(message)
            continue
        arg = arguments[key]
        kind = arg["type"]
        valid = {
            "str": isinstance(value, str), "bool": isinstance(value, bool),
            "int": type(value) is int, "number": type(value) in {int, float},
            "object": isinstance(value, dict), "array": isinstance(value, list),
            "str_or_list": isinstance(value, (str, list)),
        }[kind]
        if not valid:
            errors.append(f"{key!r} must be {kind}.")
            continue
        if key == "expected_change":
            errors.extend(validate_expected_change(value))
            continue
        if kind == "number" and isinstance(value, float) and not math.isfinite(value):
            errors.append(f"{key!r} must be finite.")
        if "choices" in arg and value not in arg["choices"]:
            errors.append(f"{key!r} must be one of {arg['choices']}.")
        if key in {"columns", "rows", "crop_columns", "crop_rows", "image_max_edge"} and value <= 0:
            errors.append(f"{key!r} must be positive.")
        if key in {"max_bytes", "max_groups", "max_age_days", "min_size_bytes", "timeout", "seconds"} and value < 0:
            errors.append(f"{key!r} must be nonnegative.")
        if key == "include_images" and any(not isinstance(item, str) or item not in {"raw", "grid", "candidates", "context"} for item in value):
            errors.append("'include_images' accepts raw, grid, candidates and context.")
        # Composite commands call handlers directly. Validate their known typed
        # arguments before the first step can emit input, too.
        nested = value if key == "actions" else [value] if key in {"step", "observe_after", "verify_before", "verification"} else []
        for index, item in enumerate(nested):
            if not isinstance(item, dict):
                errors.append(f"{key}[{index}] must be an object.")
                continue
            payload = {**item, "strict_args": args.get("strict_args") is True,
                       "protocol_version": args.get("protocol_version", 1)}
            child_warnings, child_errors = validate_arguments(command, payload, _nested=True,
                                                               _properties=arg.get("properties"))
            warnings.extend(f"{key}[{index}]: {message}" for message in child_warnings)
            errors.extend(f"{key}[{index}]: {message}" for message in child_errors)
    return warnings, errors


def command_reference() -> str:
    lines = ["| Command | Authority | Available | Arguments (plus shared) | Example |", "| --- | --- | --- | --- | --- |"]
    for name, spec in COMMANDS.items():
        args = ", ".join(f"`{key}`" for key in spec["args"]) or "none"
        lines.append(f"| `{name}` | {spec['authority']} | {'yes' if spec['available'] else 'no'} | {args} | `{spec['example']}` |")
    lines += ["", "Shared arguments: " + ", ".join(f"`{key}`" for key in SHARED_ARGS) + ".", "", "Argument types and command descriptions are returned by `helper.commands --detail full`. Unknown JSON arguments warn in v1; `protocol_version: 2` or `strict_args: true` rejects them. Defaults belong to each command and are never injected by argparse."]
    return "\n".join(lines) + "\n"
