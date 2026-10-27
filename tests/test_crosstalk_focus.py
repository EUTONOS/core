"""Offline tests: the Crosstalk focus gate (crosstalk_focus.focus_wake and the core's wrapper).

The Win32 layer and every launcher are fakes: no window is touched, no overlay is shown, no process is started."""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock


KIT = Path(__file__).resolve().parents[1] / "tools" / "crosstalk"
focus_spec = importlib.util.spec_from_file_location("crosstalk_focus_under_test", KIT / "crosstalk_focus.py")
focus = importlib.util.module_from_spec(focus_spec)
focus_spec.loader.exec_module(focus)
core_spec = importlib.util.spec_from_file_location("crosstalk_focus_core", KIT / "crosstalk_p0_core.py")
core = importlib.util.module_from_spec(core_spec)
core_spec.loader.exec_module(core)

SESSION = "cccc0002-2222-4333-8444-555555555555"
KEY = f"claude:{SESSION}"
PREV, CAME, OTHER = 101, 202, 303
SECRET = "typed secret text 4711"


class FakePlatform(focus.NullPlatform):
    """Scripted foreground: each foreground() call returns the next value; the last one repeats."""
    available = True

    def __init__(self, sequence=(PREV, CAME, CAME), alive=True, restore_result="plain", idle=60.0,
                 prev_owner_pid=PREV + 1):
        self.sequence = list(sequence)
        self.alive = alive
        self.restore_result = restore_result
        self.idle = idle
        self.prev_owner_pid = prev_owner_pid  # the process that owns PREV's handle now (a recycled handle differs)
        self.exists_pids = []
        self.calls = []
        self.closed = False

    def foreground(self):
        value = self.sequence.pop(0) if len(self.sequence) > 1 else self.sequence[0]
        self.calls.append(("foreground", value))
        return {"hwnd": value, "pid": value + 1}

    def exists(self, hwnd, pid=None):
        self.calls.append(("exists", hwnd))
        self.exists_pids.append(pid)
        return self.alive and (pid is None or pid == self.prev_owner_pid)

    def idle_seconds(self):
        self.calls.append(("idle", self.idle))
        return self.idle

    def overlay(self, text, seconds):
        self.calls.append(("overlay", seconds))
        return True

    def close_overlay(self):
        self.closed = True

    def restore(self, prev, current):
        self.calls.append(("restore", prev["hwnd"], current["hwnd"]))
        return self.restore_result

    def names(self):
        return [call[0] for call in self.calls]


class GateBase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="crosstalk-focus-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = self.root / "focus" / "log.jsonl"
        self.refusal = None
        self.order = []
        self.sleeps = []

    @contextlib.contextmanager
    def free_lock(self, timeout):
        self.order.append("lock")
        yield
        self.order.append("unlock")

    def action(self, result=None):
        def run():
            self.order.append("action")
            return {"launched": True} if result is None else result
        return run

    def confirm(self, answer=True):
        def run(start_ns):
            self.order.append("confirm")
            return answer
        return run

    def wake(self, platform, action=None, confirm=None, **options):
        options.setdefault("sleep", self.sleeps.append)
        return focus.focus_wake(KEY, action or self.action(), confirm or self.confirm(), text="waking " + SECRET,
                                kind="test", controls_check=lambda key: self.refusal, lock=self.free_lock,
                                log_path=self.log, platform=platform, **options)

    def logged(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


class GateFlowTests(GateBase):
    def test_the_whole_sequence_runs_in_the_owner_ruled_order_and_is_logged(self):
        platform = FakePlatform()
        result = self.wake(platform)
        self.assertEqual(self.order, ["lock", "action", "confirm", "unlock"])  # focus lock held until confirm ends
        # overlay (3 s) comes after the foreground was noted and before the action; restore follows the come-forward
        self.assertEqual(platform.names(), ["foreground", "overlay", "foreground", "foreground", "exists", "restore"])
        self.assertEqual(platform.calls[1], ("overlay", 3.0))
        self.assertEqual(platform.calls[-1], ("restore", PREV, CAME))
        self.assertTrue(all(result[key] for key in ("overlay", "launched", "came_forward", "restored", "confirmed")))
        self.assertEqual(result["restore_method"], "plain")
        self.assertEqual(result["before"], {"hwnd": PREV, "pid": PREV + 1})
        [row] = self.logged()
        self.assertEqual((row["target"], row["kind"], row["confirmed"], row["refused"]), (KEY, "test", True, None))
        self.assertIn("elapsed_s", row)

    def test_the_log_never_holds_message_text_or_titles(self):
        self.wake(FakePlatform(), action=lambda: {"launched": True, "text": SECRET})
        self.assertNotIn(SECRET, self.log.read_text(encoding="utf-8"))
        self.assertNotIn("text", self.logged()[0])

    def test_controls_refused_before_anything_means_no_overlay_and_no_action(self):
        self.refusal = "halted"
        platform = FakePlatform()
        result = self.wake(platform)
        self.assertEqual(result["refused"], "halted")
        self.assertNotIn("action", self.order)
        self.assertNotIn("confirm", self.order)
        self.assertNotIn("overlay", platform.names())
        self.assertEqual(self.logged()[0]["refused"], "halted")

    def test_controls_changing_during_the_countdown_still_stops_the_action(self):
        answers = [None, "halted"]
        platform = FakePlatform()
        result = focus.focus_wake(KEY, self.action(), self.confirm(), text="x", kind="test",
                                  controls_check=lambda key: answers.pop(0), lock=self.free_lock, log_path=self.log,
                                  platform=platform, sleep=self.sleeps.append)
        self.assertEqual(result["refused"], "halted")
        self.assertIn("overlay", platform.names())
        self.assertNotIn("action", self.order)
        self.assertNotIn("restore", platform.names())

    def test_the_controls_check_names_the_exact_target(self):
        seen = []
        focus.focus_wake(KEY, self.action(), self.confirm(), text="x", kind="test", sleep=self.sleeps.append,
                         controls_check=lambda key: seen.append(key), lock=self.free_lock, log_path=self.log,
                         platform=FakePlatform())
        self.assertEqual(set(seen), {KEY})

    def test_focus_is_not_restored_when_the_owner_chose_another_window(self):
        platform = FakePlatform(sequence=(PREV, CAME, OTHER))
        result = self.wake(platform)
        self.assertTrue(result["came_forward"])
        self.assertFalse(result["restored"])
        self.assertEqual(result["reason"], "owner-switched-windows")
        self.assertNotIn("restore", platform.names())
        self.assertTrue(result["confirmed"])  # the confirm still ran

    def test_focus_is_not_restored_when_config_says_so_or_the_window_is_gone(self):
        platform = FakePlatform()
        result = self.wake(platform, restore=False)
        self.assertEqual((result["restored"], result["reason"]), (False, "restore-off"))
        self.assertNotIn("restore", platform.names())
        platform = FakePlatform(alive=False)
        result = self.wake(platform)
        self.assertEqual((result["restored"], result["reason"]), (False, "previous-window-gone"))
        self.assertNotIn("restore", platform.names())

    def test_nothing_comes_forward_means_nothing_to_restore(self):
        platform = FakePlatform(sequence=(PREV,))
        result = self.wake(platform)
        self.assertFalse(result["came_forward"])
        self.assertNotIn("restore", platform.names())
        self.assertTrue(result["confirmed"])
        self.assertGreaterEqual(len(self.sleeps), 40)  # it waited about 10 s (0.25 s steps), then went on

    def test_a_failed_restore_is_tried_once_and_recorded(self):
        platform = FakePlatform(restore_result="failed")
        result = self.wake(platform)
        self.assertEqual((result["restored"], result["restore_method"]), (False, "failed"))
        self.assertEqual(platform.names().count("restore"), 1)

    def test_overlay_is_off_when_the_notice_is_zero(self):
        platform = FakePlatform()
        result = self.wake(platform, notice_s=0)
        self.assertFalse(result["overlay"])
        self.assertNotIn("overlay", platform.names())
        self.assertIn("action", self.order)

    def test_a_confirm_that_never_comes_is_recorded_once_and_nothing_is_retried(self):
        actions = []
        result = self.wake(FakePlatform(), action=lambda: actions.append(1) or {"launched": True},
                           confirm=self.confirm(False))
        self.assertEqual(actions, [1])
        self.assertFalse(result["confirmed"])
        self.assertEqual(self.order.count("confirm"), 30)  # one poll a second for about 30 s
        self.assertFalse(self.logged()[0]["confirmed"])

    def test_a_failed_launch_skips_the_wait_and_the_confirm(self):
        platform = FakePlatform()
        result = self.wake(platform, action=lambda: {"launched": False, "error": "OSError"})
        self.assertFalse(result["launched"])
        self.assertEqual(result["error"], "not-launched")
        self.assertNotIn("confirm", self.order)
        self.assertNotIn("restore", platform.names())
        self.assertEqual(self.order[-1], "unlock")

    def test_an_action_that_raises_is_recorded_and_the_lock_is_released(self):
        def boom():
            raise OSError("nope")
        result = self.wake(FakePlatform(), action=boom)
        self.assertEqual(result["error"], "action-failed:OSError")
        self.assertEqual(self.order[-1], "unlock")

    def test_a_confirm_that_raises_is_recorded_not_raised(self):
        def bad(_start):
            raise ValueError("x")
        result = self.wake(FakePlatform(), confirm=bad)
        self.assertEqual(result["error"], "confirm-failed:ValueError")
        self.assertFalse(result["confirmed"])

    def test_off_windows_only_the_action_and_the_confirm_run(self):
        platform = focus.NullPlatform()
        result = self.wake(platform)
        self.assertEqual(self.order, ["lock", "action", "confirm", "unlock"])
        self.assertFalse(result["overlay"] or result["came_forward"] or result["restored"])
        self.assertTrue(result["confirmed"])
        self.assertEqual(len(self.sleeps), 1)  # only the first confirm poll

    def test_the_focus_switch_in_the_environment_selects_the_null_platform(self):
        with mock.patch.dict(os.environ, {"CROSSTALK_P0_FOCUS": "off"}):
            self.assertIsInstance(focus.default_platform(), focus.NullPlatform)
            self.assertFalse(focus.default_platform().available)

    def test_real_focus_is_opt_in(self):
        env = {k: v for k, v in os.environ.items() if k != "CROSSTALK_P0_FOCUS"}
        with mock.patch.dict(os.environ, env, clear=True),                 mock.patch.object(focus, "Win32Platform", FakePlatform):
            focus.enable_real_focus(False)
            self.assertIsInstance(focus.default_platform(), focus.NullPlatform)  # default: never Win32
            self.assertFalse(focus.default_platform().available)
            try:
                focus.enable_real_focus()
                if os.name == "nt":
                    self.assertIsInstance(focus.default_platform(), FakePlatform)
                    os.environ["CROSSTALK_P0_FOCUS"] = "off"  # off still wins over the opt-in
                    self.assertNotIsInstance(focus.default_platform(), FakePlatform)
            finally:
                focus.enable_real_focus(False)

    def test_overlay_without_a_move_is_logged(self):
        platform = FakePlatform()
        result = self.wake(platform, action=lambda: {"launched": False})
        self.assertTrue(result["overlay_shown"])
        self.assertFalse(result["focus_moved"])
        self.assertTrue(result["overlay_without_move"])
        self.assertTrue(platform.closed)

    def test_a_move_is_logged_as_moved(self):
        result = self.wake(FakePlatform())
        self.assertTrue(result["overlay_shown"] and result["focus_moved"])
        self.assertFalse(result["overlay_without_move"])

    def test_the_log_rotates_at_one_megabyte(self):
        self.log.parent.mkdir(parents=True)
        self.log.write_text("x" * (focus.LOG_ROTATE_BYTES + 10), encoding="utf-8")
        self.wake(focus.NullPlatform())
        self.assertTrue(Path(str(self.log) + ".1").is_file())
        self.assertEqual(len(self.logged()), 1)

    def test_a_busy_focus_lock_refuses_and_does_nothing(self):
        @contextlib.contextmanager
        def busy(timeout):
            raise TimeoutError("busy")
            yield
        result = focus.focus_wake(KEY, self.action(), self.confirm(), text="x", kind="test", sleep=self.sleeps.append,
                                  controls_check=lambda key: None, lock=busy, log_path=self.log,
                                  platform=FakePlatform())
        self.assertEqual(result["refused"], "focus-lock-busy")
        self.assertEqual(self.order, [])

    def test_the_notice_seconds_are_bounded(self):
        for value, expected in ((None, 3.0), ("2", 3.0), (True, 3.0), (0, 0.0), (-5, 0.0), (2.5, 2.5), (999, 30.0)):
            self.assertEqual(focus.notice_seconds(value), expected, value)


class LateWakeTests(GateBase):
    """The wake can come forward after come_forward_timeout_s (a cold `claude --desktop --resume`)."""

    def late(self, tail=(CAME,), **platform_options):
        # come_forward_timeout_s=1.0 at 0.25 s steps is 4 polls: the foreground is PREV for the first note plus four
        # polls, then the tail shows up during the confirm phase.
        return FakePlatform(sequence=(PREV,) * 5 + tuple(tail), **platform_options)

    def test_a_wake_that_comes_forward_after_the_wait_is_still_given_back(self):
        platform = self.late()
        result = self.wake(platform, come_forward_timeout_s=1.0)
        self.assertTrue(result["came_forward"] and result["late_forward"])
        self.assertTrue(result["restored"])
        self.assertEqual(platform.calls[-1], ("restore", PREV, CAME))
        self.assertEqual(platform.names().count("restore"), 1)
        self.assertTrue(result["confirmed"])
        self.assertEqual(self.logged()[0]["late_forward"], True)

    def test_a_wake_that_comes_forward_only_as_the_confirm_ends_is_still_found(self):
        platform = FakePlatform(sequence=(PREV,) * 6 + (CAME,))  # the in-loop look still sees PREV
        result = self.wake(platform, come_forward_timeout_s=1.0)
        self.assertTrue(result["late_forward"])
        self.assertTrue(result["restored"])

    def test_a_late_wake_is_left_alone_when_the_owner_is_typing_or_idle_is_unreadable(self):
        for idle in (0.4, None):
            platform = self.late(idle=idle)
            result = self.wake(platform, come_forward_timeout_s=1.0)
            self.assertTrue(result["late_forward"])
            self.assertFalse(result["restored"], idle)
            self.assertEqual(result["reason"], "late-restore-owner-active")
            self.assertNotIn("restore", platform.names())

    def test_a_late_wake_is_left_alone_when_the_owner_already_switched_windows(self):
        platform = self.late(tail=(CAME, OTHER))
        result = self.wake(platform, come_forward_timeout_s=1.0)
        self.assertEqual((result["restored"], result["reason"]), (False, "owner-switched-windows"))
        self.assertNotIn("restore", platform.names())

    def test_late_restore_keeps_the_off_and_gone_outcomes(self):
        result = self.wake(self.late(), come_forward_timeout_s=1.0, restore=False)
        self.assertEqual((result["restored"], result["reason"]), (False, "restore-off"))
        platform = self.late(alive=False)
        result = self.wake(platform, come_forward_timeout_s=1.0)
        self.assertEqual((result["restored"], result["reason"]), (False, "previous-window-gone"))
        self.assertNotIn("restore", platform.names())

    def test_a_late_restore_happens_at_most_once(self):
        platform = self.late()
        self.wake(platform, come_forward_timeout_s=1.0, confirm=self.confirm(False), confirm_timeout_s=5.0)
        self.assertEqual(platform.names().count("restore"), 1)

    def test_nothing_ever_coming_forward_restores_nothing(self):
        platform = FakePlatform(sequence=(PREV,))
        result = self.wake(platform, come_forward_timeout_s=1.0)
        self.assertFalse(result["came_forward"] or result["late_forward"])
        self.assertNotIn("restore", platform.names())


class RecycledHandleTests(GateBase):
    def test_the_gate_passes_the_recorded_pid_so_a_recycled_handle_is_not_restored(self):
        platform = FakePlatform(prev_owner_pid=999)  # PREV's handle now belongs to another process
        result = self.wake(platform)
        self.assertIn(PREV + 1, platform.exists_pids)
        self.assertEqual((result["restored"], result["reason"]), (False, "previous-window-gone"))
        self.assertNotIn("restore", platform.names())

    def test_win32_restore_refuses_a_handle_that_no_longer_belongs_to_the_recorded_process(self):
        platform = object.__new__(focus.Win32Platform)
        platform.u32 = mock.Mock()
        with mock.patch.object(focus.Win32Platform, "exists", return_value=False) as exists:
            self.assertEqual(platform.restore({"hwnd": PREV, "pid": 5}, {"hwnd": CAME, "pid": 6}), "gone")
        exists.assert_called_once_with(PREV, 5)
        platform.u32.SetForegroundWindow.assert_not_called()

    def test_win32_exists_compares_the_owning_pid(self):
        platform = object.__new__(focus.Win32Platform)
        platform.u32 = mock.Mock()
        platform.u32.IsWindow.return_value = 1
        with mock.patch.object(focus.Win32Platform, "_pid", return_value=(1, 5)):
            self.assertTrue(platform.exists(PREV, 5))
            self.assertFalse(platform.exists(PREV, 6))
            self.assertTrue(platform.exists(PREV))  # no pid recorded: only the handle is checked
        platform.u32.IsWindow.return_value = 0
        self.assertFalse(platform.exists(PREV, 5))


class LaunchResultTests(GateBase):
    def test_a_result_without_launched_is_not_a_launch(self):
        for result in ({}, {"method": "x"}, {"launched": False}):
            self.order.clear()
            outcome = self.wake(FakePlatform(), action=lambda result=result: result)
            self.assertFalse(outcome["launched"], result)
            self.assertNotIn("confirm", self.order)
        self.order.clear()
        outcome = self.wake(FakePlatform(), action=lambda: None)
        self.assertFalse(outcome["launched"])
        self.assertNotIn("confirm", self.order)

    def test_the_real_launcher_shapes_still_count_as_launched(self):
        self.assertTrue(focus._launched({"launched": True, "method": "CREATE_NEW_CONSOLE", "verified": False}))
        self.assertTrue(focus._launched({"launched": True, "result": "opened"}))
        self.assertFalse(focus._launched({"launched": False, "result": "open-failed:OSError"}))


class CoreWrapperTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="crosstalk-focus-core-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = mock.patch.dict(os.environ, {"CROSSTALK_P0_HERMES_HOME": str(self.root / "no-hermes"), "CROSSTALK_P0_DEVICE_DIR": str(self.root / "device"),
                                          "CROSSTALK_P0_CLAUDE_HOME": str(self.root / "claude"),
                                          "CROSSTALK_P0_FOCUS": "off"})
        env.start()
        self.addCleanup(env.stop)
        nap = mock.patch.object(core, "FOCUS_SLEEP", lambda seconds: None)
        nap.start()
        self.addCleanup(nap.stop)
        core.controls_update(messaging="on", halt=False)
        self.order = []

    def config(self, **values):
        core.helper_dir().mkdir(parents=True, exist_ok=True)
        (core.helper_dir() / "config.json").write_text(json.dumps(values))

    def run_wake(self, platform, key=KEY, **kwargs):
        return core.focus_wake(key, lambda: self.order.append("action") or {"launched": True},
                               lambda start_ns: self.order.append("confirm") or True, text="x", kind="test",
                               platform=platform, **kwargs)

    def test_config_keys_set_the_overlay_time_and_the_restore_switch(self):
        platform = FakePlatform()
        self.run_wake(platform)
        self.assertEqual(platform.calls[1], ("overlay", 3.0))  # default: three seconds
        self.config(focus_notice_seconds=0, focus_restore=False)
        platform = FakePlatform()
        result = self.run_wake(platform)
        self.assertNotIn("overlay", platform.names())
        self.assertNotIn("restore", platform.names())
        self.assertEqual(result["reason"], "restore-off")
        self.config(focus_notice_seconds=5)
        platform = FakePlatform()
        self.run_wake(platform)
        self.assertEqual(platform.calls[1], ("overlay", 5.0))

    def test_the_core_controls_gate_the_target_and_the_log_is_in_the_device_folder(self):
        core.controls_update(deny_add=KEY)
        result = self.run_wake(FakePlatform())
        self.assertEqual(result["refused"], "denied")
        self.assertEqual(self.order, [])
        core.controls_update(deny_remove=KEY, halt=True)
        self.assertEqual(self.run_wake(FakePlatform())["refused"], "halted")
        core.controls_update(halt=False, messaging="off")
        self.assertEqual(self.run_wake(FakePlatform())["refused"], "messaging-off")
        rows = [json.loads(line) for line in (core.device_dir() / "focus" / "log.jsonl").read_text().splitlines()]
        self.assertEqual([row["refused"] for row in rows], ["denied", "halted", "messaging-off"])

    def test_two_wakes_run_one_after_another_the_second_only_after_the_first_confirms(self):
        events, started, release = [], threading.Event(), threading.Event()

        def first_action():
            events.append("A-action")
            started.set()
            return {"launched": True}

        def first_confirm(_start):
            release.wait(5)
            events.append("A-confirm")
            return True

        def second():
            core.focus_wake(KEY, lambda: events.append("B-action") or {"launched": True},
                            lambda start_ns: events.append("B-confirm") or True, text="x", kind="test",
                            platform=focus.NullPlatform())

        first = threading.Thread(target=lambda: core.focus_wake(
            KEY, first_action, first_confirm, text="x", kind="test", platform=focus.NullPlatform()))
        first.start()
        self.assertTrue(started.wait(5))
        other = threading.Thread(target=second)
        other.start()
        time.sleep(0.5)
        self.assertEqual(events, ["A-action"])  # B waits on the device-wide focus lock
        release.set()
        first.join(10)
        other.join(10)
        self.assertEqual(events, ["A-action", "A-confirm", "B-action", "B-confirm"])

    def test_the_second_wake_also_runs_after_the_first_times_out(self):
        events = []
        core.focus_wake(KEY, lambda: events.append("A-action") or {"launched": True}, lambda start_ns: False,
                        text="x", kind="test", platform=focus.NullPlatform())
        core.focus_wake(KEY, lambda: events.append("B-action") or {"launched": True}, lambda start_ns: True,
                        text="x", kind="test", platform=focus.NullPlatform())
        self.assertEqual(events, ["A-action", "B-action"])
        rows = [json.loads(line) for line in (core.device_dir() / "focus" / "log.jsonl").read_text().splitlines()]
        self.assertEqual([row["confirmed"] for row in rows], [False, True])

    def test_claude_checked_in_reads_the_directory_entry(self):
        before = time.time_ns()
        self.assertFalse(core.claude_checked_in(SESSION, before))
        self.assertFalse(core.claude_checked_in("not-a-uuid", before))
        core.directory_register("claude", SESSION, {})
        self.assertTrue(core.claude_checked_in(SESSION, before))
        self.assertFalse(core.claude_checked_in(SESSION, time.time_ns() + 10**10))  # check-in is older than the wake

    @unittest.skipUnless(os.name == "nt", "Windows console flags")
    def test_the_desktop_open_hides_its_console(self):
        with mock.patch.object(core.subprocess, "Popen") as popen:
            result = core.launch_desktop_show("claude.exe", SESSION)
        self.assertEqual((result["launched"], result["method"]), (True, "CREATE_NEW_CONSOLE"))
        kwargs = popen.call_args.kwargs
        self.assertEqual(kwargs["creationflags"], core.subprocess.CREATE_NEW_CONSOLE)
        startup = kwargs["startupinfo"]
        self.assertTrue(startup.dwFlags & core.subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(startup.wShowWindow, 0)  # SW_HIDE
        self.assertEqual(popen.call_args.args[0][-3:], ["--desktop", "--resume", SESSION])


class FakeChatFocus:
    """Scripted selected chat inside the app: each current() returns the next value; the last one repeats."""

    def __init__(self, sequence):
        self.sequence = list(sequence)
        self.selected = []

    def current(self):
        return self.sequence.pop(0) if len(self.sequence) > 1 else self.sequence[0]

    def select(self, chat):
        self.selected.append(chat)


class AwakeAndChatRestoreTests(GateBase):
    def test_an_already_running_chat_gets_no_overlay_no_launch_and_no_focus_change(self):
        platform = FakePlatform()
        result = self.wake(platform, awake=lambda: True)
        self.assertEqual(result["reason"], "already-awake")
        self.assertTrue(result["confirmed"])
        self.assertEqual(platform.calls, [])
        self.assertNotIn("action", self.order)

    def test_an_awake_check_that_fails_counts_as_asleep(self):
        def broken():
            raise OSError("unreadable")
        result = self.wake(FakePlatform(), awake=broken)
        self.assertTrue(result["launched"])

    def test_the_owner_is_put_back_on_their_chat_before_the_window_goes_back(self):
        platform = FakePlatform()
        chats = FakeChatFocus(["local_owner", "local_woken"])
        result = self.wake(platform, chat_focus=chats)
        self.assertEqual(chats.selected, ["local_owner"])
        self.assertTrue(result["chat_restored"])
        self.assertTrue(result["restored"])

    def test_the_chat_is_put_back_even_when_the_app_window_was_already_in_front(self):
        platform = FakePlatform(sequence=(PREV,))  # the owner was reading Claude: no other window comes forward
        chats = FakeChatFocus(["local_owner", "local_owner", "local_woken"])
        result = self.wake(platform, chat_focus=chats)
        self.assertFalse(result["came_forward"])
        self.assertEqual(chats.selected, ["local_owner"])
        self.assertTrue(result["chat_restored"])
        self.assertNotIn("restore", platform.names())

    def test_an_unchanged_chat_is_left_alone(self):
        chats = FakeChatFocus(["local_owner"])
        result = self.wake(FakePlatform(), chat_focus=chats)
        self.assertEqual(chats.selected, [])
        self.assertFalse(result["chat_restored"])

    def test_controls_are_rechecked_right_before_the_chat_is_selected(self):
        answers = [None, None, "halted"]  # entry, just before the action, then just before the chat select
        chats = FakeChatFocus(["local_owner", "local_woken"])
        platform = FakePlatform()
        result = focus.focus_wake(KEY, self.action(), self.confirm(), text="x", kind="test", sleep=self.sleeps.append,
                                  controls_check=lambda key: answers.pop(0) if answers else "halted",
                                  lock=self.free_lock, log_path=self.log, platform=platform, chat_focus=chats)
        self.assertEqual(chats.selected, [])
        self.assertFalse(result["chat_restored"])
        self.assertEqual(result["chat_restore_skipped"], "controls:halted")
        self.assertTrue(result["restored"])  # giving the owner's window back is not a wake effect

    def test_restore_off_also_leaves_the_chat_alone(self):
        chats = FakeChatFocus(["local_owner", "local_woken"])
        result = self.wake(FakePlatform(), chat_focus=chats, restore=False)
        self.assertEqual(chats.selected, [])
        self.assertFalse(result["chat_restored"])

    def test_desktop_chat_focus_reads_the_last_selected_chat_from_main_log(self):
        log = self.root / "main.log"
        first, second = "local_" + "1" * 8 + "-1111-4111-8111-" + "1" * 12, "local_" + "2" * 8 + "-2222-4222-8222-" + "2" * 12
        log.write_text(f"x setFocusedSession: sessionId=null\nx setFocusedSession: sessionId={first}\n"
                       f"x setFocusedSession: sessionId=null\nx setFocusedSession: sessionId={second}\n",
                       encoding="utf-8")
        with mock.patch.object(core, "desktop_log_path", return_value=log):
            self.assertEqual(core.DesktopChatFocus().current(), second)
            log.write_text(log.read_text(encoding="utf-8") + "x setFocusedSession: sessionId=null\n", encoding="utf-8")
            self.assertIsNone(core.DesktopChatFocus().current())  # mid-switch or no chat open: do not guess
        with self.assertRaises(ValueError):
            core.DesktopChatFocus().select("claude://evil")



class WindowsPlatform(focus.NullPlatform):
    """Scripted top-level windows: each top_windows() call returns the next snapshot; the last one repeats."""
    available = True

    def __init__(self, snapshots, exes=None, forward="plain"):
        self.snapshots = list(snapshots)
        self.exes = exes or {}
        self.forward = forward
        self.forwarded = []

    def top_windows(self):
        return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]

    def exe(self, pid):
        return self.exes.get(pid, "")

    def bring_forward(self, hwnd):
        self.forwarded.append(hwnd)
        return self.forward


class OwnerAnswerFocusTests(unittest.TestCase):
    """Owner request 2026-10-25: the CLI-login box, its console and the browser sign-in page come to the front."""
    TITLE = "Crosstalk: Claude CLI account"

    def test_the_box_comes_forward_once_this_process_shows_it(self):
        me = os.getpid()
        platform = WindowsPlatform([{}, {5: (me + 1, "#32770", self.TITLE)},
                                    {5: (me + 1, "#32770", self.TITLE), 6: (me, "#32770", self.TITLE)}])
        self.assertEqual(focus.forward_own_box(self.TITLE, platform=platform, sleep=lambda s: None), "plain")
        self.assertEqual(platform.forwarded, [6])  # another process's box with the same title is left alone

    def test_a_box_that_never_shows_is_reported_and_nothing_moves(self):
        platform = WindowsPlatform([{}])
        result = focus.forward_own_box(self.TITLE, platform=platform, timeout_s=1.0, step_s=0.5, sleep=lambda s: None)
        self.assertEqual((result, platform.forwarded), ("not-shown", []))
        self.assertEqual(focus.forward_own_box(self.TITLE, platform=focus.NullPlatform()), "unavailable")

    def test_the_console_then_the_browser_page_come_forward_once_each(self):
        before = {1: (10, "Chrome_WidgetWin_1", "Inbox - Google Chrome"), 2: (20, "Notepad", "notes.txt")}
        console = {**before, 3: (30, "ConsoleWindowClass", "claude")}
        loading = {**console, 1: (10, "Chrome_WidgetWin_1", "claude.ai/oauth - Google Chrome")}
        loaded = {**console, 1: (10, "Chrome_WidgetWin_1", "Claude - Google Chrome")}
        platform = WindowsPlatform([console, loading, loaded], exes={10: "chrome.exe"})
        result = focus.follow_sign_in(before, platform=platform, sleep=lambda s: None)
        self.assertEqual(result, {"console": "plain", "browser": "plain"})
        self.assertEqual(platform.forwarded, [3, 1])

    def test_unchanged_windows_and_non_browser_claude_windows_stay_put(self):
        desktop = (40, "Chrome_WidgetWin_1", "Claude")  # Claude Desktop: a Chrome-class window, not a browser
        before = {1: (10, "Chrome_WidgetWin_1", "Claude - Google Chrome"), 4: desktop}
        renamed = {**before, 4: (40, "Chrome_WidgetWin_1", "Claude - chat")}
        platform = WindowsPlatform([before, renamed], exes={10: "chrome.exe", 40: "claude.exe"})
        result = focus.follow_sign_in(before, platform=platform, timeout_s=1.0, step_s=0.25, sleep=lambda s: None)
        self.assertEqual((result, platform.forwarded), ({"console": None, "browser": None}, []))

    def test_a_late_console_is_ignored_and_closing_the_console_stops_the_watch(self):
        late = {7: (70, "CASCADIA_HOSTING_WINDOW_CLASS", "PowerShell")}
        platform = WindowsPlatform([{}, {}, late])
        result = focus.follow_sign_in({}, platform=platform, timeout_s=2.0, step_s=0.5, console_wait_s=1.0,
                                      sleep=lambda s: None)
        self.assertEqual((result["console"], platform.forwarded), (None, []))
        console = {3: (30, "ConsoleWindowClass", "claude")}
        calls = []
        platform = WindowsPlatform([console, {}, {9: (90, "Chrome_WidgetWin_1", "Claude")}], exes={90: "chrome.exe"})
        result = focus.follow_sign_in({}, platform=platform, sleep=lambda s: calls.append(s))
        self.assertEqual((result, len(calls)), ({"console": "plain", "browser": None}, 2))

    def test_without_window_handling_nothing_is_watched(self):
        self.assertEqual(focus.follow_sign_in({}, platform=focus.NullPlatform(), sleep=self.fail),
                         {"console": None, "browser": None})


if __name__ == "__main__":
    unittest.main()
