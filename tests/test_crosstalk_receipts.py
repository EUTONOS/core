"""Offline tests: the receipt-honesty boundary of Crosstalk delivery (slice 3B.13, review finding 6).

deliver_claude, deliver_codex and deliver_hermes decide `accepted` vs `uncertain` vs `refused`. They are called
DIRECTLY here, with only their lowest native boundary replaced (subprocess.run, the file system, the Hermes
inbox/done files). Invariants pinned: a failure is never recorded as accepted/arrived, and anything that may have
reached the native app is uncertain, never refused. Also: the CLI `reconcile` command and `deny` (not only halt).

No real claude, codex or Hermes process, no real window or app data; the focus gate is off."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock


KIT = Path(__file__).resolve().parents[1] / "tools" / "crosstalk"
spec = importlib.util.spec_from_file_location("crosstalk_receipts_core", KIT / "crosstalk_p0_core.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
with mock.patch.dict("sys.modules", {"crosstalk_p0_core": core}):
    cli_spec = importlib.util.spec_from_file_location("crosstalk_receipts_cli", KIT / "crosstalk_p0_cli.py")
    cli = importlib.util.module_from_spec(cli_spec)
    cli_spec.loader.exec_module(cli)

LETTER = "CTP0-0123456789ab"
CLAUDE = "11111111-2222-4333-8444-555555555555"
CODEX = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
CODEX_ACCOUNT = "cccccccc-bbbb-4ccc-8ddd-eeeeeeeeeeee"
HERMES = "20261013_230248_6a7bf5"
MESSAGE = "Crosstalk message from Codex (codex:x)\nHello\nref " + LETTER


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="crosstalk-receipts-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fake_claude = self.root / "claude.exe"
        self.fake_codex = self.root / "codex.exe"
        self.fake_claude.write_text("x")
        self.fake_codex.write_text("x")
        env = mock.patch.dict(os.environ, {
            "CROSSTALK_P0_DEVICE_DIR": str(self.root / "device"),
            "CROSSTALK_P0_CLAUDE_HOME": str(self.root / "claude-home"),
            "CROSSTALK_P0_CLAUDE_JSON": str(self.root / "claude.json"),
            "CROSSTALK_P0_CLAUDE_DESKTOP_LOG": str(self.root / "main.log"),
            "CROSSTALK_P0_HERMES_HOME": str(self.root / "hermes"),
            "CROSSTALK_P0_CLAUDE_EXE": str(self.fake_claude),
            "CROSSTALK_P0_CODEX_EXE": str(self.fake_codex),
            "CROSSTALK_P0_CODEX_WAKE": "off", "CROSSTALK_P0_CODEX_HEADLESS": "off", "CROSSTALK_P0_FOCUS": "off"})
        env.start()
        self.addCleanup(env.stop)
        core.controls_update(messaging="on", halt=False)


class DeliverClaudeTests(Base):
    """The throwaway `claude -p` sender: only subprocess.run is replaced."""

    def run_sender(self, make_stdout=None, returncode=0, raises=None):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            if raises is not None:
                raise raises
            caller = argv[argv.index("--session-id") + 1]
            payload = json.loads(kwargs["env"]["CROSSTALK_P0_PAYLOAD"])
            stdout = make_stdout(caller, payload) if make_stdout else ""
            return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

        with mock.patch.object(core.subprocess, "run", side_effect=fake_run):
            result = core.deliver_claude(LETTER, CLAUDE, MESSAGE)
        return result, calls

    @staticmethod
    def receipt(inner=None, **outer_changes):
        def make(caller, payload):
            body = {"tag": core.SENDER_TAG, "letter": payload["letter"], "status": "queued", "isDelivered": True,
                    "version": "1.0"}
            body.update(inner or {})
            outer = {"is_error": False, "num_turns": 0, "session_id": caller,
                     "result": "receipt: " + json.dumps(body)}
            outer.update(outer_changes)
            return json.dumps(outer)
        return make

    def test_a_verified_queued_receipt_is_accepted(self):
        (state, extra), calls = self.run_sender(self.receipt())
        self.assertEqual(state, "accepted")
        self.assertEqual(extra["sender_version"], "1.0")
        payload = json.loads(calls[0][1]["env"]["CROSSTALK_P0_PAYLOAD"])
        self.assertEqual((payload["target"], payload["letter"], payload["text"]), (CLAUDE, LETTER, MESSAGE))

    def test_unavailable_cli_or_oversized_payload_is_refused_before_any_process(self):
        os.environ["CROSSTALK_P0_CLAUDE_EXE"] = str(self.root / "missing.exe")
        with mock.patch.object(core.subprocess, "run") as run:
            self.assertEqual(core.deliver_claude(LETTER, CLAUDE, MESSAGE),
                             ("refused", {"reason": "claude-cli-or-sender-plugin-unavailable"}))
        run.assert_not_called()
        os.environ["CROSSTALK_P0_CLAUDE_EXE"] = str(self.fake_claude)
        with mock.patch.object(core.subprocess, "run") as run:
            self.assertEqual(core.deliver_claude(LETTER, CLAUDE, "x" * 31000),
                             ("refused", {"reason": "payload-too-large-for-sender"}))
        run.assert_not_called()

    def test_a_process_that_never_started_is_refused(self):
        (state, extra), _ = self.run_sender(raises=FileNotFoundError("claude"))
        self.assertEqual((state, extra["reason"]), ("refused", "claude-cli-not-launched"))

    def test_a_timeout_may_have_sent_so_it_is_uncertain(self):
        (state, extra), _ = self.run_sender(raises=subprocess.TimeoutExpired("claude", 60))
        self.assertEqual((state, extra["reason"]), ("uncertain", "sender-timeout"))
        self.assertIn("sender_session", extra)

    def test_another_os_error_may_have_sent_so_it_is_uncertain(self):
        for error in (PermissionError("denied"), OSError("broken pipe")):
            with self.subTest(error=type(error).__name__):
                (state, extra), _ = self.run_sender(raises=error)
                self.assertEqual((state, extra["reason"]), ("uncertain", "sender-os-error:" + type(error).__name__))

    def test_unparseable_output_is_uncertain(self):
        for stdout in ("", "not json", "[1, 2]", json.dumps({"result": "no braces"}),
                       json.dumps({"result": "x {broken"})):
            with self.subTest(stdout=stdout):
                (state, extra), _ = self.run_sender(lambda caller, payload, text=stdout: text)
                self.assertEqual(state, "uncertain")
                self.assertEqual(extra["reason"], "sender-output-unparseable"
                                 if stdout in ("", "not json", "[1, 2]", json.dumps({"result": "x {broken"}))
                                 else "sender-receipt-unverified")

    def test_a_receipt_that_does_not_prove_the_session_is_uncertain_not_refused(self):
        wrong_session = self.receipt(session_id="other-session")
        for label, make, code in (("exit", self.receipt(), 1), ("error", self.receipt(is_error=True), 0),
                                  ("turns", self.receipt(num_turns=2), 0), ("session", wrong_session, 0),
                                  ("tag", self.receipt({"tag": "other"}), 0)):
            with self.subTest(case=label):
                (state, extra), _ = self.run_sender(make, returncode=code)
                self.assertEqual((state, extra["reason"]), ("uncertain", "sender-receipt-unverified"))

    def test_the_senders_own_preflight_stop_is_refused_as_not_attempted(self):
        (state, extra), _ = self.run_sender(self.receipt({"status": "not-attempted", "reason": "controls-halted",
                                                          "isDelivered": None}))
        self.assertEqual((state, extra["reason"]), ("refused", "sender-guard:controls-halted"))

    def test_a_receipt_for_another_letter_is_uncertain(self):
        (state, extra), _ = self.run_sender(self.receipt({"letter": "CTP0-ffffffffffff"}))
        self.assertEqual((state, extra["reason"]), ("uncertain", "sender-letter-mismatch"))

    def test_native_refusal_is_refused_only_when_explicitly_not_delivered(self):
        (state, extra), _ = self.run_sender(self.receipt({"status": "refused", "isDelivered": False,
                                                          "reason": "No agent named x"}))
        self.assertEqual((state, extra["reason"]), ("refused", "native-not-delivered:No agent named x"))
        for inner in ({"status": "refused", "isDelivered": None}, {"status": "refused"},
                      {"status": "queued", "isDelivered": False}, {"status": "queued", "isDelivered": "yes"},
                      {"status": "mystery"}):
            with self.subTest(inner=inner):
                (state, extra), _ = self.run_sender(self.receipt(inner))
                self.assertEqual((state, extra["reason"]), ("uncertain", "sender-outcome-unverified"))

    def test_the_letter_state_recorded_for_each_outcome_is_never_arrived_on_a_failure(self):
        core.directory_register("claude", CLAUDE, {})
        core.directory_register("codex", CODEX, {"account": {"id": CODEX_ACCOUNT}})
        for raises, expected in ((subprocess.TimeoutExpired("claude", 60), "uncertain"),
                                 (FileNotFoundError("claude"), "refused")):
            with self.subTest(expected=expected), \
                    mock.patch.object(core, "codex_account", return_value={"app": "codex", "id": CODEX_ACCOUNT}), \
                    mock.patch.object(core, "block_reason", return_value=None), \
                    mock.patch.object(core, "refresh_binding_locked"), \
                    mock.patch.object(core.subprocess, "run", side_effect=raises), \
                    mock.patch.object(core, "arrivals", return_value=(0, "read")):
                result = core.send_letter("codex", CODEX, "claude:" + CLAUDE, "Hello")
            self.assertEqual(result["state"], expected)
            self.assertFalse(result["arrived"])
            self.assertFalse(result["transport_accepted"])
            self.assertIs(result["sent"], None if expected == "uncertain" else False)
            self.assertIn("report_to_owner", result)


class DeliverCodexTests(Base):
    """`codex queue`: only subprocess.run is replaced."""

    def queue(self, stdout="", returncode=0, stderr="", raises=None):
        def fake_run(argv, **kwargs):
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)

        with mock.patch.object(core.subprocess, "run", side_effect=fake_run) as run:
            result = core.deliver_codex(CODEX, MESSAGE)
        return result, run

    def test_a_confirmed_queue_line_is_accepted(self):
        queue_id = "dddddddd-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        (state, extra), run = self.queue(f"Queued message {queue_id} for thread {CODEX}\n")
        self.assertEqual((state, extra["queue_id"], extra["codex_exit"]), ("accepted", queue_id, 0))
        argv = run.call_args[0][0]
        self.assertEqual(argv[-5:], ["queue", "--thread", CODEX, "--message", MESSAGE])

    def test_missing_cli_or_unstarted_process_is_refused(self):
        os.environ["CROSSTALK_P0_CODEX_EXE"] = str(self.root / "missing.exe")
        with mock.patch.object(core.subprocess, "run") as run:
            self.assertEqual(core.deliver_codex(CODEX, MESSAGE), ("refused", {"reason": "codex-cli-unavailable"}))
        run.assert_not_called()
        os.environ["CROSSTALK_P0_CODEX_EXE"] = str(self.fake_codex)
        (state, extra), _ = self.queue(raises=FileNotFoundError("codex"))
        self.assertEqual((state, extra["reason"]), ("refused", "codex-cli-not-launched"))

    def test_timeout_and_os_error_may_have_queued_so_they_are_uncertain(self):
        (state, extra), _ = self.queue(raises=subprocess.TimeoutExpired("codex", 30))
        self.assertEqual((state, extra["reason"]), ("uncertain", "codex-queue-timeout"))
        (state, extra), _ = self.queue(raises=PermissionError("denied"))
        self.assertEqual((state, extra["reason"]), ("uncertain", "codex-queue-os-error:PermissionError"))

    def test_anything_short_of_a_confirmed_queue_line_is_uncertain_never_accepted(self):
        queue_id = "dddddddd-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        good = f"Queued message {queue_id} for thread {CODEX}\n"
        cases = (("empty", "", 0), ("nonzero", good, 1), ("other thread", good.replace(CODEX, CLAUDE), 0),
                 ("bad id", f"Queued message not-a-uuid-not-a-uuid-not-a-uuid-123456 for thread {CODEX}", 0),
                 ("prose", "Queued it!", 0))
        for label, stdout, code in cases:
            with self.subTest(case=label):
                (state, extra), _ = self.queue(stdout, returncode=code, stderr="boom\n")
                self.assertEqual((state, extra["reason"]), ("uncertain", "codex-queue-unconfirmed"))
                self.assertEqual(extra["codex_exit"], code)
                self.assertEqual(extra["stderr_tail"], "boom")

    def test_the_letter_state_recorded_for_an_unconfirmed_queue_is_uncertain_and_never_replayed(self):
        core.directory_register("codex", CODEX, {"account": {"id": CODEX_ACCOUNT}})
        core.directory_register("claude", CLAUDE, {})
        with mock.patch.object(core, "codex_account", return_value={"app": "codex", "id": CODEX_ACCOUNT}), \
                mock.patch.object(core.subprocess, "run",
                                  return_value=subprocess.CompletedProcess([], 0, stdout="?", stderr="")) as run, \
                mock.patch.object(core, "arrivals", return_value=(0, "thread-read")):
            result = core.send_letter("claude", CLAUDE, "codex:" + CODEX, "Hello")
            self.assertEqual((result["state"], result["reason"]), ("uncertain", "codex-queue-unconfirmed"))
            self.assertFalse(result["arrived"])
            core.reconcile(resend=True, grace_s=0, stale_s=0)
            core.attempt_delivery(result["letter_id"])
        self.assertEqual(run.call_count, 1)  # never resent
        self.assertEqual(core.letters_load()[result["letter_id"]]["state"], "uncertain")


class DeliverHermesTests(Base):
    """The Hermes inbox/done files: the plugin's side is played by the test."""

    def setUp(self):
        super().setUp()
        self.base = core.hermes_dir()
        self.base.mkdir(parents=True)
        patcher = mock.patch.object(core, "HERMES_RESULT_WAIT_S", 0.3)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(core, "HERMES_RESULT_POLL_S", 0.01)
        patcher.start()
        self.addCleanup(patcher.stop)

    def heartbeat(self, **changes):
        from datetime import datetime, timezone
        beat = {"utc": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(), "hosts_chats": True}
        beat.update(changes)
        (self.base / "heartbeat.json").write_text(json.dumps(beat))

    def listener_result(self, result):
        """The plugin has already answered: done/<letter>.result.json exists."""
        (self.base / "done").mkdir(exist_ok=True)
        (self.base / "done" / f"{LETTER}.result.json").write_text(json.dumps(result))

    def test_no_listener_is_refused_and_writes_nothing(self):
        for beat in (None, {"hosts_chats": False}, {"pid": 0}, {"utc": "2020-01-01T00:00:00+00:00"}):
            with self.subTest(beat=beat):
                (self.base / "heartbeat.json").unlink(missing_ok=True)
                if beat is not None:
                    self.heartbeat(**beat)
                self.assertEqual(core.deliver_hermes(LETTER, HERMES, MESSAGE), ("refused", {"reason": "hermes-not-running"}))
                self.assertFalse((self.base / "inbox").exists())

    def test_the_host_accepting_the_injection_is_accepted(self):
        self.heartbeat()
        self.listener_result({"returned": True, "pid": 4321})
        state, extra = core.deliver_hermes(LETTER, HERMES, MESSAGE)
        self.assertEqual((state, extra), ("accepted", {"hermes_pid": 4321}))
        sent = json.loads((self.base / "inbox" / f"{LETTER}.json").read_text(encoding="utf-8"))
        self.assertEqual(sent, {"letter_id": LETTER, "session_key": HERMES, "text": MESSAGE})
        self.assertFalse((self.base / "inbox" / f"{LETTER}.json.part").exists())

    def test_explicit_non_delivery_is_refused(self):
        self.heartbeat()
        self.listener_result({"returned": False})
        self.assertEqual(core.deliver_hermes(LETTER, HERMES, MESSAGE)[1]["reason"], "hermes-chat-not-open")
        self.listener_result({"refused": "messaging-off", "returned": None})
        self.assertEqual(core.deliver_hermes(LETTER, HERMES, MESSAGE), ("refused", {"hermes_pid": None, "reason": "messaging-off"}))

    def test_an_inject_error_or_ambiguous_result_is_uncertain_never_accepted(self):
        self.heartbeat()
        for result in ({"returned": True, "error": "RuntimeError: x"}, {"returned": False, "error": "x"},
                       {"returned": None}, {}, {"returned": "yes"}, {"error": "host exploded"}):
            with self.subTest(result=result):
                self.listener_result(result)
                state, extra = core.deliver_hermes(LETTER, HERMES, MESSAGE)
                self.assertEqual(state, "uncertain")
                self.assertTrue(extra["reason"].startswith("hermes-inject-outcome-unverified:"))

    def test_an_unclaimed_file_is_withdrawn_and_refused(self):
        self.heartbeat()
        state, extra = core.deliver_hermes(LETTER, HERMES, MESSAGE)  # nobody answers within the wait
        self.assertEqual((state, extra), ("refused", {"reason": "hermes-not-running"}))
        self.assertFalse((self.base / "inbox" / f"{LETTER}.json").exists())
        self.assertTrue((self.base / "cancelled" / f"{LETTER}.json").is_file())

    def test_a_claimed_file_with_no_result_is_uncertain(self):
        self.heartbeat()
        real_sleep = time.sleep

        def listener_claims(seconds):
            (self.base / "inbox" / f"{LETTER}.json").unlink(missing_ok=True)  # the plugin took it, never answered
            real_sleep(0.001)

        with mock.patch.object(core.time, "sleep", side_effect=listener_claims):
            state, extra = core.deliver_hermes(LETTER, HERMES, MESSAGE)
        self.assertEqual((state, extra), ("uncertain", {"reason": "hermes-claimed-no-result"}))

    def test_an_inbox_write_failure_is_refused_with_nothing_left_behind(self):
        self.heartbeat()
        with mock.patch.object(core.os, "replace", side_effect=PermissionError("denied")):
            state, extra = core.deliver_hermes(LETTER, HERMES, MESSAGE)
        self.assertEqual((state, extra), ("refused", {"reason": "hermes-inbox-write-failed:PermissionError"}))
        self.assertFalse((self.base / "inbox" / f"{LETTER}.json").exists())
        self.assertFalse((self.base / "inbox" / f"{LETTER}.json.part").exists())

    def test_a_failure_to_create_the_runtime_folders_is_refused_not_raised(self):
        self.heartbeat()
        with mock.patch.object(core.Path, "mkdir", side_effect=PermissionError("denied")):
            state, extra = core.deliver_hermes(LETTER, HERMES, MESSAGE)
        self.assertEqual((state, extra), ("refused", {"reason": "hermes-inbox-write-failed:PermissionError"}))

    def test_the_letter_state_recorded_for_hermes_outcomes(self):
        self.heartbeat()
        core.directory_register("hermes", HERMES, {})
        core.directory_register("codex", CODEX, {"account": {"id": CODEX_ACCOUNT}})
        sends = []
        real_deliver = core.deliver_hermes

        def with_answer(letter_id, session_id, message):
            sends.append(letter_id)
            self.listener_result_for(letter_id, self.answer)
            return real_deliver(letter_id, session_id, message)

        for answer, expected in (({"returned": None, "error": "boom"}, "uncertain"), ({"returned": False}, "refused")):
            self.answer = answer
            with self.subTest(expected=expected), mock.patch.object(core, "deliver_hermes", side_effect=with_answer), \
                    mock.patch.object(core, "codex_account", return_value={"app": "codex", "id": CODEX_ACCOUNT}), \
                    mock.patch.object(core, "arrivals", return_value=(0, "state-db-read")):
                result = core.send_letter("codex", CODEX, "hermes:" + HERMES, "Hello")
            self.assertEqual(result["state"], expected)
            self.assertFalse(result["arrived"])
            self.assertFalse(result["transport_accepted"])

    def listener_result_for(self, letter_id, result):
        (self.base / "done").mkdir(exist_ok=True)
        (self.base / "done" / f"{letter_id}.result.json").write_text(json.dumps(result))


class ReconcileAndDenyCliTests(Base):
    def setUp(self):
        super().setUp()
        core.directory_register("codex", CODEX, {"account": {"id": CODEX_ACCOUNT}})
        codex_now = mock.patch.object(core, "codex_account", return_value={"app": "codex", "id": CODEX_ACCOUNT})
        codex_now.start()
        self.addCleanup(codex_now.stop)

    def journaled(self, letter_id="CTP0-00000000a001", age_s=60.0):
        core.journal_append(core._letter_row(
            letter_id, "journaled", to={"app": "codex", "id": CODEX},
            message=MESSAGE.rsplit("ref ", 1)[0] + "ref " + letter_id, created_ns=time.time_ns(),
            ts_ns=time.time_ns() - int(age_s * 1e9)))
        return letter_id

    def run_cli(self, *argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(list(argv))
        return code, json.loads(buffer.getvalue())

    def test_reconcile_sends_a_stranded_journaled_letter_exactly_once(self):
        letter_id = self.journaled()
        with mock.patch.object(core, "deliver_codex", return_value=("accepted", {})) as leg, \
                mock.patch.object(core, "arrivals", return_value=(1, "thread-read")):
            code, value = self.run_cli("reconcile")
            self.assertEqual(code, 0)
            self.assertEqual([item["letter_id"] for item in value["report"]["resent"]], [letter_id])
            self.run_cli("reconcile")
        self.assertEqual(leg.call_count, 1)
        self.assertEqual(core.letters_load()[letter_id]["state"], "arrived")

    def test_reconcile_no_resend_and_the_grace_window_send_nothing(self):
        letter_id = self.journaled()
        with mock.patch.object(core, "deliver_codex") as leg:
            code, value = self.run_cli("reconcile", "--no-resend")
            self.assertEqual(value["report"]["waiting"], [{"letter_id": letter_id, "state": "journaled"}])
            self.journaled("CTP0-00000000a002", age_s=0.0)
            code, value = self.run_cli("reconcile", "--grace", "3600")
            self.assertEqual(value["report"]["resent"], [])
        leg.assert_not_called()
        self.assertEqual(core.letters_load()[letter_id]["state"], "journaled")

    def test_reconcile_never_resends_accepted_held_or_uncertain_letters(self):
        ids = []
        for index, state in enumerate(("accepted", "held", "uncertain", "sending")):
            letter_id = f"CTP0-00000000b{index:03d}"
            ids.append(letter_id)
            core.journal_append(core._letter_row(letter_id, state, to={"app": "codex", "id": CODEX}, message=MESSAGE,
                                                 created_ns=time.time_ns()))
        with mock.patch.object(core, "deliver_codex") as leg, mock.patch.object(core, "arrivals", return_value=(0, "read")):
            code, value = self.run_cli("reconcile", "--grace", "0", "--stale", "0")
        self.assertEqual(code, 0)
        self.assertEqual(value["report"]["resent"], [])
        leg.assert_not_called()

    def test_deny_and_halt_make_reconcile_refuse_without_a_native_effect(self):
        for index, (label, change) in enumerate((("deny", lambda: self.run_cli("deny", "codex:" + CODEX)),
                                                 ("halt", lambda: self.run_cli("halt")))):
            letter_id = self.journaled(f"CTP0-00000000c{index:03d}")
            with self.subTest(control=label):
                code, value = change()
                self.assertEqual((code, value["ok"]), (0, True))
                with mock.patch.object(core, "deliver_codex") as leg:
                    self.run_cli("reconcile")
                leg.assert_not_called()
                letter = core.letters_load()[letter_id]
                self.assertEqual((letter["state"], letter["sent"]), ("refused", False))
                self.assertEqual(letter["reason"], "denied" if label == "deny" else "halted")
                self.run_cli("allow", "codex:" + CODEX)
                self.run_cli("resume")


if __name__ == "__main__":
    unittest.main()
