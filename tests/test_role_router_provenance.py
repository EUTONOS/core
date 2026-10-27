"""Caller provenance on role launches remains metadata, never permission."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from eutonos import role_router as rr


CALLER_SESSION = "session:origin.ref"
CALLER_WORK = "work:AWR-7"
PINNED_MODEL = "gpt-6.1-sol"


def _resolved(harness, *, autonomy="ask"):
    return {
        "role": "worker", "choice": "primary", "harness": harness,
        "model": PINNED_MODEL, "model_requested": PINNED_MODEL,
        "model_selection": {"selection_kind": "pin"}, "effort": "high",
        "preamble": None, "brief_template": "", "autonomy": autonomy,
        "chosen_by": "request", "auto_reason": None,
    }


class RoleRouterProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.marker_patch = mock.patch.dict(
            os.environ, {marker: "" for marker in rr.HARNESS_ENV_MARKERS.values()})
        self.marker_patch.start()
        self.addCleanup(self.marker_patch.stop)
        self.addCleanup(self.temp.cleanup)

    def test_cross_app_create_echoes_caller_fields_and_preserves_model_pin(self):
        with mock.patch.object(rr, "resolve_role", return_value=_resolved("claude")), \
                mock.patch.object(rr, "_build_prompt", return_value="prepared worker prompt"), \
                mock.patch.object(rr, "create_chat", return_value={"ok": True, "key": "claude:r1"}) as send:
            result = rr.open_role(
                self.root, None, "worker", from_harness="codex", brief_text="bounded work",
                confirm=True, caller_session=CALLER_SESSION, caller_work_id=CALLER_WORK)

        request = send.call_args.kwargs
        # Crosstalk's create commands have no provenance field: it is echoed, never sent.
        self.assertNotIn("caller_session_id", request)
        self.assertNotIn("caller_work_id", request)
        self.assertEqual(request["model"], PINNED_MODEL)
        self.assertEqual(request["prompt"], "prepared worker prompt")
        self.assertEqual(result["caller_session_id"], CALLER_SESSION)
        self.assertEqual(result["caller_work_id"], CALLER_WORK)
        self.assertEqual(result["visibility"], "visible-chat")

    def test_caller_metadata_does_not_bypass_confirmation_or_change_legacy_shape(self):
        resolved = _resolved("claude")
        with mock.patch.object(rr, "resolve_role", return_value=resolved), \
                mock.patch.object(rr, "_build_prompt", return_value="prepared worker prompt"), \
                mock.patch.object(rr, "create_chat", return_value={"ok": True, "key": "claude:r2"}) as send:
            preview = rr.open_role(
                self.root, None, "worker", from_harness="codex", brief_text="bounded work",
                caller_session=CALLER_SESSION)
            send.assert_not_called()
            self.assertTrue(preview["needs_confirmation"])
            self.assertEqual(preview["caller_session_id"], CALLER_SESSION)
            self.assertEqual(preview["visibility"], "visible-chat")

            legacy = rr.open_role(
                self.root, None, "worker", from_harness="codex", brief_text="bounded work",
                confirm=True)

        self.assertNotIn("caller_session_id", legacy)
        self.assertNotIn("caller_work_id", legacy)
        self.assertNotIn("visibility", legacy)
        self.assertNotIn("caller_session_id", send.call_args.kwargs)
        self.assertNotIn("caller_work_id", send.call_args.kwargs)

    def test_same_app_start_returns_provenance_descriptor_without_crosstalk_or_prompt_changes(self):
        role_entry = {"edits": "ask", "preamble": None}
        with mock.patch.object(rr, "resolve_role", return_value=_resolved("codex")), \
                mock.patch.object(rr.settings, "read_roles", return_value={"roles": {"worker": role_entry}}), \
                mock.patch.object(rr, "_assemble_role_prompt", return_value="native prompt"), \
                mock.patch.object(rr, "create_chat") as send:
            result = rr.start(
                self.root, None, "worker", from_harness="codex", brief_text="bounded work",
                caller_session=CALLER_SESSION, caller_work_id=CALLER_WORK)

        send.assert_not_called()
        self.assertTrue(result["same_app"])
        self.assertEqual(result["prompt"], "native prompt")
        self.assertNotIn(CALLER_SESSION, result["prompt"])
        self.assertNotIn(CALLER_WORK, result["prompt"])
        self.assertEqual(result["caller_session_id"], CALLER_SESSION)
        self.assertEqual(result["caller_work_id"], CALLER_WORK)
        self.assertEqual(result["visibility"], "owner-native-handoff")

    def test_role_router_cli_forwards_caller_fields_for_open_and_start(self):
        for command, target in (("open", "open_role"), ("start", "start")):
            for work_option in ("--caller-work", "--caller-work-id"):
                with self.subTest(command=command, work_option=work_option):
                    args = [command, "--root", str(self.root), "--role", "worker",
                            "--from-harness", "codex", "--brief-text", "bounded work",
                            "--caller-session", CALLER_SESSION, work_option, CALLER_WORK]
                    output = io.StringIO()
                    with mock.patch.object(rr, target, return_value={"ok": True}) as launch, \
                            contextlib.redirect_stdout(output):
                        self.assertEqual(rr.main(args), 0)
                    self.assertEqual(launch.call_args.kwargs["caller_session"], CALLER_SESSION)
                    self.assertEqual(launch.call_args.kwargs["caller_work_id"], CALLER_WORK)
                    self.assertIn('"ok": true', output.getvalue())

    def test_caller_values_use_the_reference_pattern_and_require_strings(self):
        with self.assertRaises(rr.RoleRouterError) as bad_session:
            rr._caller_metadata(True, None)
        self.assertEqual(bad_session.exception.code, "bad-caller-session")

        with self.assertRaises(rr.RoleRouterError) as bad_work:
            rr._caller_metadata(None, "work id with spaces")
        self.assertEqual(bad_work.exception.code, "bad-caller-work-id")

        # Caller references are opaque and need not be UUID-shaped.
        self.assertEqual(rr._caller_metadata(CALLER_SESSION, CALLER_WORK), {
            "caller_session_id": CALLER_SESSION, "caller_work_id": CALLER_WORK})


if __name__ == "__main__":
    unittest.main()
