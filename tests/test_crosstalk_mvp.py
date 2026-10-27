"""Offline MVP delivery regressions; no live app, account or transport is used."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


KIT = Path(__file__).resolve().parents[1] / "tools" / "crosstalk"
spec = importlib.util.spec_from_file_location("crosstalk_mvp_core", KIT / "crosstalk_p0_core.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
with mock.patch.dict("sys.modules", {"crosstalk_p0_core": core}):
    cli_spec = importlib.util.spec_from_file_location("crosstalk_mvp_cli", KIT / "crosstalk_p0_cli.py")
    cli = importlib.util.module_from_spec(cli_spec)
    cli_spec.loader.exec_module(cli)

CLAUDE = "11111111-2222-4333-8444-555555555555"
CODEX = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
CODEX_ACCOUNT = "cccccccc-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class CrosstalkMvpTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="crosstalk-mvp-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = mock.patch.dict(os.environ, {"CROSSTALK_P0_HERMES_HOME": str(self.root / "no-hermes"), "CROSSTALK_P0_DEVICE_DIR": str(self.root / "device"),
                                               "CROSSTALK_P0_CLAUDE_HOME": str(self.root / "claude"),
                                               "CROSSTALK_P0_CODEX_WAKE": "off",  # no detached waiter in tests
                                               "CROSSTALK_P0_FOCUS": "off"})  # no overlay or window code in tests
        self.env.start()
        self.addCleanup(self.env.stop)
        core.directory_register("codex", CODEX, {"account": {"id": CODEX_ACCOUNT}})
        codex_now = mock.patch.object(core, "codex_account", return_value={"app": "codex", "id": CODEX_ACCOUNT})
        codex_now.start()  # the identity gate compares the recipient's recorded Codex account with this one
        self.addCleanup(codex_now.stop)
        core.controls_update(messaging="on")

    def send(self):
        return core.send_letter("claude", CLAUDE, "codex:" + CODEX, "Hello")

    def row(self, letter_id="CTP0-0123456789ab", state="accepted", app="codex"):
        native_id = CODEX if app == "codex" else CLAUDE
        core.journal_append(core._letter_row(letter_id, state, to={"app": app, "id": native_id},
                                            message=core.frame_message(letter_id, "claude", "sender", "Hello")))
        return letter_id

    def test_transport_acceptance_with_zero_native_inputs_is_held(self):
        with mock.patch.object(core, "deliver_codex", return_value=("accepted", {"queue_id": "native-q"})) as leg, \
                mock.patch.object(core, "arrivals", return_value=(0, "thread-read")):
            result = self.send()
        self.assertEqual(result["state"], "held")
        self.assertTrue(result["transport_accepted"])
        self.assertTrue(result["sent"])
        self.assertFalse(result["arrived"])
        self.assertEqual(result["reason"], "native-input-not-observed")
        self.assertEqual(result["arrivals"], 0)
        self.assertIn("Do NOT resend", result["guidance"])
        self.assertEqual(leg.call_count, 1)
        self.assertEqual([r["state"] for r in core.letters_load()[result["letter_id"]]["history"]],
                         ["journaled", "sending", "accepted", "held"])

    def test_unknown_readback_is_held_with_evidence_not_arrived(self):
        with mock.patch.object(core, "deliver_codex", return_value=("accepted", {})), \
                mock.patch.object(core, "arrivals", return_value=(None, "thread-turns-unavailable")):
            result = self.send()
        self.assertEqual(result["state"], "held")
        self.assertEqual(result["reason"], "readback-unavailable")
        self.assertIsNone(result["arrivals"])
        self.assertEqual(result["readback_note"], "thread-turns-unavailable")

    def test_readback_exception_keeps_accepted_letter_held(self):
        with mock.patch.object(core, "deliver_codex", return_value=("accepted", {})), \
                mock.patch.object(core, "arrivals", side_effect=OSError("unavailable")):
            result = self.send()
        self.assertEqual(result["state"], "held")
        self.assertTrue(result["transport_accepted"])
        self.assertEqual(result["readback_note"], "readback-error:OSError")

    def test_immediate_native_input_is_arrived(self):
        with mock.patch.object(core, "deliver_codex", return_value=("accepted", {})), \
                mock.patch.object(core, "arrivals", return_value=(1, "thread-read")):
            result = self.send()
        self.assertEqual(result["state"], "arrived")
        self.assertTrue(result["arrived"])
        self.assertEqual(result["arrivals"], 1)

    def test_owner_send_cli_returns_success_for_held_or_arrived_transport(self):
        text_file = self.root / "message.txt"
        text_file.write_text("Hello", encoding="utf-8")
        args = SimpleNamespace(app="claude", id=CLAUDE, to="codex:" + CODEX, text_file=str(text_file))
        for count, expected in ((0, "held"), (1, "arrived")):
            with self.subTest(state=expected), \
                    mock.patch.object(core, "deliver_codex", return_value=("accepted", {})), \
                    mock.patch.object(core, "arrivals", return_value=(count, "thread-read")), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.cmd_send_as(args), 0)
            self.assertEqual(json.loads(output.getvalue())["state"], expected)

    def test_owner_send_cli_exit_status_separates_refused_and_uncertain(self):
        text_file = self.root / "message.txt"
        text_file.write_bytes(b"\xef\xbb\xbfHello")  # a Notepad BOM must not reach the letter body
        args = SimpleNamespace(app="claude", id=CLAUDE, to="codex:" + CODEX, text_file=str(text_file))
        for native, expected_state, expected_code in (
                (("refused", {"reason": "codex-cli-unavailable"}), "refused", 2),
                (("uncertain", {"reason": "codex-queue-unconfirmed"}), "uncertain", 3)):
            with self.subTest(state=expected_state), \
                    mock.patch.object(core, "deliver_codex", return_value=native) as leg, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                code = cli.cmd_send_as(args)
            self.assertEqual(code, expected_code)
            value = json.loads(output.getvalue())
            self.assertEqual(value["state"], expected_state)
            self.assertNotIn("\ufeff", leg.call_args[0][1])
        core.controls_update(messaging="off")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.cmd_send_as(args), 2)  # refused by the control, never sent
        self.assertEqual(json.loads(output.getvalue())["reason"], "messaging-off")

    def test_cli_exit_status_mapping(self):
        for state, code in (("accepted", 0), ("held", 0), ("arrived", 0), ("blocked:unknown-identity", 0),
                            ("refused", 2), ("uncertain", 3), ("unknown", 1), (None, 1)):
            self.assertEqual(cli.send_exit_code({"state": state}), code, state)

    def test_prompt_and_letter_files_drop_a_bom(self):
        path = self.root / "prompt.txt"
        path.write_bytes(b"\xef\xbb\xbfline one\nline \xc3\xa9")
        self.assertEqual(cli.read_text_file(str(path)), "line one\nline \u00e9")

    def test_journal_append_writes_the_whole_line_even_after_short_writes(self):
        real = os.write
        calls = []

        def short(descriptor, data):
            calls.append(len(data))
            return real(descriptor, bytes(data)[:7])  # at most 7 bytes per call

        with mock.patch.object(core.os, "write", side_effect=short):
            core.journal_append({"event": "probe", "marker": "x" * 40})
        self.assertGreater(len(calls), 3)
        rows = [row for row in core.journal_rows() if row.get("event") == "probe"]
        self.assertEqual([row["marker"] for row in rows], ["x" * 40])

    def test_journal_append_raises_when_the_write_makes_no_progress(self):
        with mock.patch.object(core.os, "write", return_value=0):
            with self.assertRaises(OSError):
                core.journal_append({"event": "probe"})
        self.assertEqual([row for row in core.journal_rows() if row.get("event") == "probe"], [])

    def test_a_partial_line_does_not_corrupt_the_next_row(self):
        real = os.write
        state = {"n": 0}

        def failing(descriptor, data):
            state["n"] += 1
            if state["n"] == 1:
                return real(descriptor, bytes(data)[:5])
            if state["n"] == 2:
                raise OSError("disk full")
            return real(descriptor, data)

        with mock.patch.object(core.os, "write", side_effect=failing):
            with self.assertRaises(OSError):
                core.journal_append({"event": "probe", "marker": "lost"})
        core.journal_append({"event": "probe", "marker": "kept"})
        self.assertEqual([row["marker"] for row in core.journal_rows() if row.get("event") == "probe"], ["kept"])

    def test_readback_programming_errors_are_not_masked_as_unavailable(self):
        letter_id = self.row(state="held")
        with mock.patch.object(core, "arrivals", side_effect=KeyError("bug")):
            with self.assertRaises(KeyError):
                core.check_arrival(letter_id)
        for error in (OSError("x"), ValueError("x"), core.sqlite3.OperationalError("x"), core.ProbeError("app-server-exited")):
            with self.subTest(error=type(error).__name__), mock.patch.object(core, "arrivals", side_effect=error):
                count, note = core.check_arrival(letter_id)
            self.assertIsNone(count)
            self.assertTrue(note.startswith("readback-error:"))
            self.assertEqual(core.letters_load()[letter_id]["state"], "held")

    def test_transcript_rows_of_an_unexpected_shape_are_skipped_not_raised(self):
        letter_id = "CTP0-0123456789ab"
        transcript = self.root / "claude" / "projects" / "fixture" / f"{CLAUDE}.jsonl"
        transcript.parent.mkdir(parents=True)
        message = core.frame_message(letter_id, "codex", "sender", "Hello")
        good = {"type": "user", "uuid": "visible", "message": {"content": message}}
        rows = ["[\"" + letter_id + "\"]", json.dumps({"type": "user", "uuid": "s", "message": letter_id}),
                json.dumps(good)]
        transcript.write_text("\n".join(rows) + "\n", encoding="utf-8")
        self.assertEqual(core.claude_arrivals(CLAUDE, letter_id), (1, "complete-scan"))

    def test_status_rechecks_held_and_promotes_without_sending_even_when_off(self):
        letter_id = self.row(state="held")
        core.controls_update(messaging="off", halt=True, deny_add="codex:" + CODEX)
        output = io.StringIO()
        with mock.patch.object(core, "arrivals", return_value=(1, "thread-read")), \
                mock.patch.object(core, "deliver_codex") as leg, \
                mock.patch.object(core, "claude_account", return_value={"id": None}), \
                mock.patch.object(core, "claude_desktop_account_folders", return_value=[]), \
                contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["status"]), 0)
        value = json.loads(output.getvalue())
        self.assertEqual(value["letters"]["counts"], {"arrived": 1})
        self.assertEqual(value["reconcile_readback"]["promoted"][0]["letter_id"], letter_id)
        leg.assert_not_called()
        self.assertEqual(core.controls_load(), {"messaging": "off", "halt": True, "deny": ["codex:" + CODEX],
                                                "wake_budget": core.WAKE_BUDGET_DEFAULT})

    def test_accepted_held_and_uncertain_never_replay_after_zero_readback(self):
        ids = [self.row(f"CTP0-{index:012x}", state) for index, state in
               enumerate(("accepted", "held", "uncertain"))]
        with mock.patch.object(core, "arrivals", return_value=(0, "thread-read")), \
                mock.patch.object(core, "deliver_codex") as leg:
            for _ in range(2):
                core.reconcile(resend=True, grace_s=0, stale_s=0)
                for letter_id in ids:
                    core.attempt_delivery(letter_id)
        leg.assert_not_called()
        self.assertEqual([core.letters_load()[key]["state"] for key in ids], ["held", "held", "uncertain"])

    def test_uncertain_native_input_promotes_without_replay(self):
        letter_id = self.row(state="uncertain")
        with mock.patch.object(core, "arrivals", return_value=(1, "thread-read")), \
                mock.patch.object(core, "deliver_codex") as leg:
            core.reconcile(resend=True)
        self.assertEqual(core.letters_load()[letter_id]["state"], "arrived")
        leg.assert_not_called()

    def test_controls_and_late_halt_prevent_native_effect(self):
        for control, expected in (({"messaging": "off"}, "messaging-off"),
                                  ({"halt": True}, "halted"),
                                  ({"deny_add": "codex:" + CODEX}, "denied")):
            with self.subTest(control=control):
                core.controls_update(messaging="on", halt=False, deny_remove="codex:" + CODEX)
                core.controls_update(**control)
                with mock.patch.object(core, "deliver_codex") as leg:
                    result = self.send()
                self.assertEqual(result["reason"], expected)
                self.assertFalse(result["sent"])
                leg.assert_not_called()
        core.controls_update(messaging="on", halt=False, deny_remove="codex:" + CODEX)
        with mock.patch.object(core, "controls_check", side_effect=[None, "halted"]), \
                mock.patch.object(core, "deliver_codex") as leg:
            result = self.send()
        self.assertEqual(result["reason"], "halted")
        leg.assert_not_called()

    def test_hidden_claude_attachment_stays_held_until_visible_user_input(self):
        letter_id = self.row(app="claude")
        transcript = self.root / "claude" / "projects" / "fixture" / f"{CLAUDE}.jsonl"
        transcript.parent.mkdir(parents=True)
        message = core.frame_message(letter_id, "codex", "sender", "Hello")
        hidden = {"type": "attachment", "uuid": "hidden", "attachment": {"type": "queued_command", "prompt": message}}
        meta = {"type": "user", "uuid": "meta", "isMeta": True, "message": {"content": message}}
        wrapped = {"type": "user", "uuid": "wrapped", "message": {"content": f"<cross-session-message>\n{message}\n</cross-session-message>"}}
        ref_only = {"type": "user", "uuid": "mention", "message": {"content": f"An unrelated note\nref {letter_id}"}}
        hidden_rows = "".join(json.dumps(row) + "\n" for row in (hidden, meta, wrapped, ref_only))
        transcript.write_text(hidden_rows, encoding="utf-8")
        core.reconcile(resend=False)
        self.assertEqual(core.letters_load()[letter_id]["state"], "held")
        visible = {"type": "user", "uuid": "visible", "message": {"content": message}}
        transcript.write_text(hidden_rows + json.dumps(visible) + "\n", encoding="utf-8")
        core.reconcile(resend=False)
        self.assertEqual(core.letters_load()[letter_id]["state"], "arrived")
        self.assertEqual(core.letters_load()[letter_id]["arrivals"], 1)

    def test_codex_native_input_ids_and_exact_ref_not_nonce_mentions(self):
        letter_id = "CTP0-0123456789ab"
        real = {"id": "input-1", "type": "userMessage", "content": [{"type": "text", "text": core.frame_message(letter_id, "claude", "sender", "Hello")}]}
        mention = {"id": "input-2", "type": "userMessage", "content": [{"type": "text", "text": f"I saw {letter_id}"}]}
        assistant = {"id": "assistant", "type": "agentMessage", "content": real["content"]}
        server = mock.Mock()
        server.request.return_value = {"thread": {"turns": [{"id": "turn", "items": [real, real, mention, assistant]}]}}
        with mock.patch.object(core, "codex_exe", return_value="unused"), \
                mock.patch.object(core, "AppServer", return_value=server):
            self.assertEqual(core.codex_arrivals(CODEX, letter_id), (1, "thread-read"))
        self.assertLessEqual(server.start.call_args.args[1], core.READBACK_TIMEOUT_S)
        self.assertLessEqual(server.request.call_args.args[2], core.READBACK_TIMEOUT_S)
        server.close.assert_called_once()

    def test_bounded_reconcile_rotates_past_unavailable_old_letters(self):
        ids = [self.row(f"CTP0-{index:012x}") for index in range(5)]
        clock = [0.0]
        checked = []

        def unavailable(letter, timeout):
            checked.append(letter["letter_id"])
            clock[0] += timeout
            return None, "native-readback-timeout"

        with mock.patch.object(core.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(core, "arrivals", side_effect=unavailable), \
                mock.patch.object(core, "deliver_codex") as leg:
            first = core.reconcile(resend=False)
            self.assertEqual(checked, ids[:3])
            self.assertEqual(first["readback_checked"], 3)
            self.assertEqual(first["readback_deferred"], 2)
            self.assertTrue(all(item["note"] == "readback-budget-exhausted" for item in first["waiting"]))
            checked.clear()
            core.reconcile(resend=False)
            self.assertEqual(checked[:2], ids[3:])
        leg.assert_not_called()

    def test_reconcile_rechecks_an_aged_held_letter_rarely_and_never_resends(self):
        now = core.time.time_ns()
        old, fresh = "CTP0-00000000a9ed", "CTP0-0000000f7e54"
        core.journal_append(core._letter_row(old, "held", to={"app": "codex", "id": CODEX}, sent=True,
                                             created_ns=now - int((core.AGED_LETTER_S + 60) * 1e9),
                                             readback_ns=now - int(60 * 1e9),
                                             message=core.frame_message(old, "claude", "sender", "Hello")))
        self.row(fresh)
        with mock.patch.object(core, "arrivals", return_value=(0, "thread-read")) as read, \
                mock.patch.object(core, "deliver_codex") as leg:
            report = core.reconcile()
        self.assertEqual([call.args[0]["letter_id"] for call in read.call_args_list], [fresh])
        self.assertIn({"letter_id": old, "state": "held", "note": "aged-recheck-later"}, report["waiting"])
        leg.assert_not_called()
        stale = now - int((core.AGED_RECHECK_S + 60) * 1e9)
        core.journal_append(core._letter_row(old, "held", readback_ns=stale))
        with mock.patch.object(core, "arrivals", return_value=(0, "thread-read")) as read:
            core.reconcile()
        self.assertIn(old, [call.args[0]["letter_id"] for call in read.call_args_list])

    def test_status_pending_counts_expose_truncation_beyond_recent_window(self):
        for index in range(51):
            self.row(f"CTP0-{index:012x}", "held")
        summary = core.letter_summary(5)
        self.assertEqual(summary["pending_count"], 51)
        self.assertTrue(summary["pending_truncated"])
        self.assertEqual(len(summary["pending"]), 50)
        self.assertEqual(summary["counts"]["held"], 51)

    def test_previous_sending_age_survives_readback_metadata_updates(self):
        letter_id = self.row(state="sending")
        with mock.patch.object(core, "arrivals", return_value=(0, "thread-read")):
            core.reconcile(resend=False, stale_s=0)
        self.assertEqual(core.letters_load()[letter_id]["state"], "uncertain")
        self.assertEqual(core.letters_load()[letter_id]["reason"], "helper-ended-mid-attempt")


if __name__ == "__main__":
    unittest.main()
