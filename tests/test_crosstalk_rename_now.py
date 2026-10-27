"""Offline tests: the helper's rename-now pass (owner request 2026-10-25), its notice handle, the skip-reason log line
and the pieces it uses (Renamer since_tick, desktop_main_window, the login-prompt marker).

Fakes only: no real window, no notice process, no powershell, no Claude Desktop. The helper's clock, sleep, renamer,
notice and focus platform are injected."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock


KIT = Path(__file__).resolve().parents[1] / "tools" / "crosstalk"
spec = importlib.util.spec_from_file_location("crosstalk_rename_now_core", KIT / "crosstalk_p0_core.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
with mock.patch.dict("sys.modules", {"crosstalk_p0_core": core}):
    rename_spec = importlib.util.spec_from_file_location("crosstalk_rename_now_rename", KIT / "crosstalk_rename.py")
    rename = importlib.util.module_from_spec(rename_spec)
    rename_spec.loader.exec_module(rename)
    helper_spec = importlib.util.spec_from_file_location("crosstalk_rename_now_helper", KIT / "crosstalk_helper.py")
    helper = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(helper)
    notice_spec = importlib.util.spec_from_file_location("crosstalk_rename_now_notice", KIT / "crosstalk_rename_notice.py")
    notice_mod = importlib.util.module_from_spec(notice_spec)
    notice_spec.loader.exec_module(notice_mod)

ACCOUNT = "88888888-2222-4333-8444-555555555555"
ORG = "77777777-2222-4333-8444-555555555555"
DESKTOP_HWND = 500


def chat_id(number: int) -> tuple[str, str]:
    return (f"local_{number:08d}-2222-4333-8444-555555555555", f"cccc{number:04d}-2222-4333-8444-555555555555")


class FakeWindow:
    """The focus platform the helper uses to find and raise Claude Desktop."""
    available = True

    def __init__(self):
        self.foreground_hwnd = 1
        self.raised, self.allowed = [], 0
        self.windows = {DESKTOP_HWND: (50, "Chrome_WidgetWin_1", "Claude")}
        self.exes = {50: "claude.exe"}

    def foreground(self):
        return {"hwnd": self.foreground_hwnd, "pid": 0}

    def top_windows(self):
        return dict(self.windows)

    def exe(self, pid):
        return self.exes.get(pid, "")

    def allow_any_foreground(self):
        self.allowed += 1

    def bring_forward(self, hwnd):
        self.raised.append(hwnd)
        self.foreground_hwnd = hwnd
        return "plain"


class FakeNotice:
    def __init__(self, shows=True):
        self.shows, self.shown_with, self.updates, self.closed, self.cancel_now = shows, [], [], None, False

    def show(self, count):
        self.shown_with.append(count)
        return self.shows

    def cancelled(self):
        return self.cancel_now

    def update(self, text=None):
        self.updates.append(text)

    def close(self, state, text):
        self.closed = (state, text)


class NowPlatform:
    """The renamer's window facts: Desktop in front, a settable owner-input clock."""
    available = True

    def __init__(self):
        self.tick, self.exe, self.idle = 1000, "claude.exe", 60.0

    def foreground(self):
        return {"hwnd": 7, "pid": 8, "exe": self.exe}

    def idle_seconds(self):
        return self.idle

    def last_input_tick(self):
        return self.tick


class NowRenamer:
    """A Renamer stand-in with the real guard semantics for since_tick: the first guard of a pass is "no owner input
    since the tick the helper took", the idle path's is "owner idle"."""

    def __init__(self, folder: Path, platform: NowPlatform):
        self.folder, self.platform = folder, platform
        self.calls, self.plan, self.after_rename = [], [], None

    def records(self, local_id):
        row = core._read_json(self.folder / f"{local_id}.json", None)
        return {"title": row.get("title"), "titleSource": row.get("titleSource")} if row else None

    def guards(self, since_tick=None):
        if self.platform.exe != "claude.exe":
            return {"ok": False, "reason": "desktop-not-in-front", "checks": {}}
        if since_tick is None:
            ok = self.platform.idle >= 15
            return {"ok": ok, "reason": None if ok else "owner-active", "checks": {}}
        ok = self.platform.tick == since_tick
        return {"ok": ok, "reason": None if ok else "owner-input", "checks": {}}

    def rename(self, local_id, title, since_tick=None):
        gate = self.guards(since_tick)
        self.calls.append((local_id, title, since_tick))
        if not gate["ok"]:
            return {"ok": False, "outcome": "refused", "attempted": False, "reason": gate["reason"],
                    "input_tick": None}
        outcome = self.plan.pop(0) if self.plan else "ok"
        self.platform.tick += 3  # the tool's own keystrokes are input too
        path = self.folder / f"{local_id}.json"
        row = core._read_json(path, {})
        if outcome == "ok":
            row.update(titleSource="user")
            core._write_json_atomic(path, row)
        result = {"ok": outcome == "ok", "outcome": "renamed" if outcome == "ok" else "failed", "attempted": True,
                  "push": True, "temp_state": False, "title_left": "as-requested", "reason": None,
                  "chat_restore_needed": False, "chat_restored": None, "input_tick": self.platform.tick}
        if self.after_rename:
            self.after_rename(len(self.calls))
        return result


class RenameNowBase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="crosstalk-rename-now-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = self.root / "main.log"
        self.base = self.root / "AppData" / "claude-code-sessions"
        self.folder = self.base / ACCOUNT / ORG
        self.folder.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {
            "CROSSTALK_P0_CODEX_STATE_DB": str(self.root / "no-codex-state.sqlite"),
            "CROSSTALK_P0_HERMES_HOME": str(self.root / "no-hermes"),
            "CROSSTALK_P0_DEVICE_DIR": str(self.root / "device"),
            "CROSSTALK_P0_CLAUDE_HOME": str(self.root / "claude"),
            "CROSSTALK_P0_CLAUDE_JSON": str(self.root / "claude.json"),
            "CROSSTALK_P0_CLAUDE_DESKTOP_LOG": str(self.log),
            "CROSSTALK_P0_CLAUDE_DESKTOP_DIR": str(self.base),
            "CROSSTALK_P0_FOCUS": "off", "CROSSTALK_P0_RENAME": ""})
        env.start()
        self.addCleanup(env.stop)
        (self.root / "claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": ACCOUNT}}))
        core.controls_update(messaging="on", halt=False)
        self.now = time.time()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.now - 200))
        self.log.write_text(f"{stamp} [info] Loaded 9 persisted sessions from {self.folder}\n", encoding="utf-8")
        self.h = helper.Helper(clock=lambda: self.now)
        self.h.sleep = lambda seconds: None
        self.platform, self.window, self.notice = NowPlatform(), FakeWindow(), FakeNotice()
        self.renamer = NowRenamer(self.folder, self.platform)
        self.h.renamer, self.h.window_platform_override = self.renamer, self.window
        self.h.notice_factory = lambda: self.notice
        self.load_id = "load-1"
        self.set_load(phase="done", age_s=200)

    # ---- fixtures
    def set_load(self, phase="done", age_s=200, reconnects=None, **extra):
        def mutate(data):
            data["loads"][self.load_id] = dict({"account": ACCOUNT, "org": ORG, "log_s": self.now - age_s,
                                                "seen_ns": int((self.now - age_s) * 1e9), "phase": phase,
                                                "reconnects": reconnects or {}}, **extra)
        core.helper_loads_update(mutate)

    def chat(self, number, **extra):
        local, session = chat_id(number)
        row = dict({"cliSessionId": session, "title": f"Chat {number}", "titleSource": "auto",
                    "remoteControlUserEnabled": True}, **extra)
        (self.folder / f"{local}.json").write_text(json.dumps(row), encoding="utf-8")
        return local

    def ready(self):
        """The first look sees the candidate list; once it has stayed the same for the quiet time the pass may run."""
        first = self.h.rename_now()
        self.now += helper.RENAME_NOW_QUIET_S + 1
        return first

    def run_pass(self):
        self.ready()
        return self.h.rename_now()

    def load(self):
        return core.helper_loads_load()["loads"][self.load_id]

    def log_text(self):
        path = core.helper_dir() / "helper_log.txt"
        return path.read_text(encoding="utf-8") if path.exists() else ""


class PassTests(RenameNowBase):
    def test_runs_every_candidate_in_order_then_finishes(self):
        first, second, third = self.chat(2), self.chat(3), self.chat(4)
        self.chat(5, titleSource="user", bridgeSessionIds=None)       # already a real name: not a candidate
        self.assertEqual(self.ready()["skipped"], "candidates-changing")
        self.assertEqual(self.notice.shown_with, [])                    # nothing is shown before the list is stable
        report = self.h.rename_now()
        self.assertTrue(report["ran"])
        self.assertEqual((report["stop"], report["renamed"]), ("finished", 3))
        self.assertEqual([call[0] for call in self.renamer.calls], [first, second, third])
        self.assertEqual(self.notice.shown_with, [3])
        self.assertEqual(self.notice.closed[0], "done")
        self.assertIn("3 of 3", self.notice.closed[1])
        self.assertEqual(self.window.raised, [DESKTOP_HWND])
        self.assertGreaterEqual(self.window.allowed, 1)
        # the first rename starts from the tick taken right after Desktop came forward; each next from the last typing
        ticks = [call[2] for call in self.renamer.calls]
        self.assertEqual(ticks, [1000, 1003, 1006])
        self.assertEqual(self.load()["rename_now"]["state"], "done")
        for number in (2, 3, 4):
            local, _ = chat_id(number)
            self.assertTrue(self.h.rename_ledger()["chats"][self.h.rename_key(local, f"Chat {number}")]["ok_ns"])

    def test_notice_text_says_what_is_happening(self):
        text = notice_mod.notice_text(4)
        self.assertIn("turning on remote access and fixing chat names (4 chats)", text)
        self.assertIn("Please don't type or click until this closes.", text)
        self.assertIn("(1 chat)", notice_mod.notice_text(1))

    def test_owner_input_mid_pass_stops_it_and_the_rest_fall_to_the_idle_path(self):
        first, second, third = self.chat(2), self.chat(3), self.chat(4)
        self.renamer.after_rename = lambda n: setattr(self.platform, "tick", self.platform.tick + 50) if n == 1 else None
        report = self.run_pass()
        self.assertEqual((report["stop"], report["attempted"]), ("owner-input", 1))
        self.assertEqual([call[0] for call in self.renamer.calls], [first])
        self.assertEqual(self.notice.closed[0], "failed")
        self.assertEqual(self.load()["rename_now"]["state"], "stopped:owner-input")
        self.assertEqual(self.platform.tick, 1053)
        # the idle path takes the next one when the owner is idle and Desktop is in front, at most one per tick
        self.h.last_rename_scan_s = 0.0
        idle = self.h.rename_chats()
        self.assertEqual(idle["attempted"], 1)
        self.assertEqual(self.renamer.calls[-1][0], second)
        self.assertIsNone(self.renamer.calls[-1][2])                    # idle mode, not "since"

    def test_owner_input_before_the_first_chat_stops_without_typing(self):
        self.chat(2)
        # the owner touches something after the baseline was read: the first guard fails and nothing is typed
        self.renamer.guards = lambda since_tick=None: {"ok": False, "reason": "owner-input", "checks": {}}
        report = self.run_pass()
        self.assertEqual((report["stop"], report["attempted"]), ("owner-input", 0))
        self.assertEqual(self.renamer.calls, [])

    def test_cancel_stops_the_pass(self):
        first = self.chat(2)
        self.chat(3)
        self.renamer.after_rename = lambda n: setattr(self.notice, "cancel_now", True)
        report = self.run_pass()
        self.assertEqual((report["stop"], report["attempted"]), ("cancelled", 1))
        self.assertEqual([call[0] for call in self.renamer.calls], [first])
        self.assertEqual(self.notice.closed[0], "cancelled")
        self.assertEqual(self.load()["rename_now"]["state"], "stopped:cancelled")

    def test_two_failures_in_a_row_end_the_pass_and_the_ledger_counts_them(self):
        for number in (2, 3, 4):
            self.chat(number)
        self.renamer.plan = ["fail", "fail", "ok"]
        report = self.run_pass()
        self.assertEqual((report["stop"], report["attempted"], report["renamed"]), ("failed-twice", 2, 0))
        local, _ = chat_id(2)
        self.assertEqual(self.h.rename_ledger()["chats"][self.h.rename_key(local, "Chat 2")]["fails"], 1)
        # inside the back-off the same chat is not due again, so the idle path does not retry it blindly
        self.assertFalse(self.h.rename_due(self.h.rename_ledger()["chats"][self.h.rename_key(local, "Chat 2")], self.now))

    def test_a_chat_already_done_in_the_ledger_is_not_renamed_again(self):
        done, other = self.chat(2), self.chat(3)
        ledger = self.h.rename_ledger()
        ledger["chats"][self.h.rename_key(done, "Chat 2")] = {"ok_ns": 1}
        core._write_json_atomic(core.helper_dir() / helper.RENAME_LEDGER_FILE, ledger)
        report = self.run_pass()
        self.assertEqual([call[0] for call in self.renamer.calls], [other])
        self.assertEqual(report["stop"], "finished")

    def test_desktop_pushed_back_is_brought_forward_once_while_the_owner_has_not_typed(self):
        self.chat(2)
        self.chat(3)

        def after(n):  # something took the front after the first rename
            self.platform.exe = "other.exe"
        self.renamer.after_rename = after
        original = self.window.bring_forward

        def raise_desktop(hwnd):
            self.platform.exe = "claude.exe"
            return original(hwnd)
        self.window.bring_forward = raise_desktop
        report = self.run_pass()
        self.assertEqual((report["stop"], report["renamed"]), ("finished", 2))
        self.assertEqual(self.window.raised, [DESKTOP_HWND, DESKTOP_HWND])

    def test_notice_that_does_not_show_means_nothing_is_typed(self):
        self.chat(2)
        self.notice.shows = False
        report = self.run_pass()
        self.assertEqual(report["skipped"], "notice-not-shown")
        self.assertEqual((self.renamer.calls, self.window.raised), ([], []))
        self.assertEqual(self.load()["rename_now"]["state"], "stopped:notice-not-shown")
        # the idle path is still there
        self.h.last_rename_scan_s = 0.0
        self.assertEqual(self.h.rename_chats()["attempted"], 1)

    def test_no_desktop_window_stops_before_typing(self):
        self.chat(2)
        self.window.windows = {}
        report = self.run_pass()
        self.assertEqual((report["stop"], self.renamer.calls), ("no-desktop-window", []))
        self.assertEqual(self.notice.closed[0], "failed")

    def test_the_pass_has_a_time_limit(self):
        self.chat(2)
        self.chat(3)
        self.renamer.after_rename = lambda n: setattr(self, "now", self.now + helper.RENAME_NOW_MAX_PASS_S + 1)
        report = self.run_pass()
        self.assertEqual((report["stop"], report["attempted"]), ("time-limit", 1))


class PreventedTests(RenameNowBase):
    def assert_nothing_happened(self, reason):
        report = self.run_pass()
        self.assertEqual(report, {"ran": False, "skipped": reason})
        self.assertEqual((self.notice.shown_with, self.window.raised, self.renamer.calls), ([], [], []))
        self.assertIsNone(self.load().get("rename_now"))

    def test_halt(self):
        self.chat(2)
        core.controls_update(halt=True)
        self.assert_nothing_happened("controls")

    def test_messaging_off_does_not_stop_the_pass(self):
        # slice 3B.19: messaging is for letters only (the pass used to be refused with "controls")
        self.chat(2)
        core.controls_update(halt=False, messaging="off")
        report = self.run_pass()
        self.assertTrue(report["ran"])
        self.assertEqual(report["stop"], "finished")

    def test_kill_switch(self):
        self.chat(2)
        with mock.patch.dict(os.environ, {"CROSSTALK_P0_RENAME": "off"}):
            self.assert_nothing_happened("rename-off")

    def test_config_switches(self):
        self.chat(2)
        core._write_json_atomic(core.helper_dir() / "config.json", {"rename_now": False})
        self.assert_nothing_happened("config-off")
        core._write_json_atomic(core.helper_dir() / "config.json", {"rename_titles": False})
        self.assert_nothing_happened("config-off")

    def test_halt_during_the_pass_stops_it(self):
        self.chat(2)
        self.chat(3)
        self.renamer.after_rename = lambda n: core.controls_update(halt=True)
        report = self.run_pass()
        self.assertEqual((report["stop"], report["attempted"]), ("controls", 1))
        core.controls_update(halt=False)

    def test_kill_switch_during_the_pass_stops_it(self):
        self.chat(2)
        self.chat(3)
        self.renamer.after_rename = lambda n: os.environ.__setitem__("CROSSTALK_P0_RENAME", "off")
        with mock.patch.dict(os.environ, {}):
            report = self.run_pass()
        self.assertEqual((report["stop"], report["attempted"]), ("rename-off", 1))

    def test_no_window_platform_does_nothing(self):
        self.chat(2)
        self.window.available = False
        self.assert_nothing_happened("no-window-platform")
        self.window.available = True
        self.platform.available = False
        self.assert_nothing_happened("no-window-platform")

    def test_a_load_that_is_not_ready_gets_no_pass(self):
        self.chat(2)
        for phase, reason in (("pending", "load-not-ready"), ("skipped:not-active-account", "load-not-ready"),
                              ("superseded", "load-not-ready")):
            self.set_load(phase=phase)
            self.assert_nothing_happened(reason)

    def test_too_young_and_too_old(self):
        self.chat(2)
        self.set_load(age_s=30)
        self.assertEqual(self.h.rename_now()["skipped"], "settling")
        self.set_load(age_s=helper.RENAME_NOW_MAX_AGE_S + 5)
        self.assertEqual(self.h.rename_now()["skipped"], "too-old")
        self.assertEqual(self.load()["rename_now"]["state"], "skipped:too-old")
        self.assertEqual(self.notice.shown_with, [])

    def test_a_load_for_another_account_gets_no_pass(self):
        self.chat(2)
        self.set_load()
        def mutate(data):
            data["loads"][self.load_id]["account"] = "99999999-2222-4333-8444-555555555555"
        core.helper_loads_update(mutate)
        self.assertEqual(self.h.rename_now()["skipped"], "not-active-account")

    def test_waits_for_the_relink_to_finish_then_gives_up_waiting(self):
        self.chat(2)
        self.set_load(reconnects={"x": {"state": "claimed"}})
        self.assertEqual(self.h.rename_now()["skipped"], "relink-running")
        self.set_load(age_s=helper.RENAME_NOW_RELINK_WAIT_S + 5, reconnects={"x": {"state": "claimed"}})
        self.assertNotEqual(self.h.rename_now().get("skipped"), "relink-running")

    def test_nothing_to_rename_runs_nothing_and_leaves_the_load_open(self):
        self.chat(2, titleSource="user")
        self.assertEqual(self.h.rename_now()["skipped"], "nothing-to-rename")
        self.assertIsNone(self.load().get("rename_now"))
        self.chat(3)                                   # a chat shows up later (Desktop was slow): still handled
        self.ready()
        self.assertTrue(self.h.rename_now()["ran"])


class OncePerLoadTests(RenameNowBase):
    def test_a_second_look_at_the_same_load_does_nothing(self):
        self.chat(2)
        self.assertTrue(self.run_pass()["ran"])
        self.chat(3)
        self.now += 600
        self.assertEqual(self.h.rename_now()["skipped"], "already-run")
        self.assertEqual(self.notice.shown_with, [1])
        # a restart of the helper does not repeat it either: the load remembers
        again = helper.Helper(clock=lambda: self.now)
        again.renamer, again.window_platform_override = self.renamer, self.window
        again.notice_factory = lambda: self.notice
        self.assertEqual(again.rename_now()["skipped"], "already-run")

    def test_a_cancelled_pass_is_not_retried_for_that_load(self):
        self.chat(2)
        self.chat(3)
        self.renamer.after_rename = lambda n: setattr(self.notice, "cancel_now", True)
        self.assertEqual(self.run_pass()["stop"], "cancelled")
        self.notice.cancel_now = False
        self.assertEqual(self.h.rename_now()["skipped"], "already-run")
        self.assertEqual(len(self.renamer.calls), 1)

    def test_a_crash_mid_pass_is_remembered_as_running(self):
        self.chat(2)
        self.ready()
        self.h._mark_rename_now(self.load_id, {"state": "running"}, only_if_unset=True)
        self.assertEqual(self.h.rename_now()["skipped"], "already-run")
        self.assertEqual(self.notice.shown_with, [])

    def test_a_newer_load_gets_its_own_pass(self):
        self.chat(2)
        self.assertTrue(self.run_pass()["ran"])
        self.chat(3)
        def mutate(data):
            data["loads"]["load-2"] = {"account": ACCOUNT, "org": ORG, "log_s": self.now - 200,
                                       "seen_ns": int((self.now - 200) * 1e9) + 5, "phase": "done", "reconnects": {}}
        core.helper_loads_update(mutate)
        self.ready()
        report = self.h.rename_now()
        self.assertTrue(report["ran"])


class LoginPromptTests(RenameNowBase):
    def test_waits_while_the_login_prompt_is_open_then_runs(self):
        self.chat(2)
        marker = core.login_prompt_marker_path()
        core._write_json_atomic(marker, {"pid": os.getpid(), "ts_ms": int(self.now * 1000)})
        self.assertEqual(self.h.rename_now()["skipped"], "login-prompt-open")
        self.now += 120
        self.assertEqual(self.h.rename_now()["skipped"], "login-prompt-open")
        self.assertEqual(self.notice.shown_with, [])
        marker.unlink()
        self.assertEqual(self.h.rename_now()["skipped"], "candidates-changing")
        self.now += helper.RENAME_NOW_QUIET_S + 1
        self.assertTrue(self.h.rename_now()["ran"])

    def test_a_prompt_the_helper_just_started_counts_as_open(self):
        self.chat(2)
        self.h.write_state(login_prompt_spawn_ns=int((self.now - 5) * 1e9))
        self.assertEqual(self.h.rename_now()["skipped"], "login-prompt-open")
        self.now += helper.LOGIN_PROMPT_SPAWN_GRACE_S + 1
        self.assertNotEqual(self.h.rename_now()["skipped"], "login-prompt-open")

    def test_a_dead_or_old_marker_does_not_block(self):
        marker = core.login_prompt_marker_path()
        core._write_json_atomic(marker, {"pid": 99999999, "ts_ms": int(time.time() * 1000)})
        self.assertFalse(core.login_prompt_open())
        core._write_json_atomic(marker, {"pid": os.getpid(),
                                         "ts_ms": int((time.time() - core.LOGIN_PROMPT_OPEN_MAX_S - 5) * 1000)})
        self.assertFalse(core.login_prompt_open())
        core._write_json_atomic(marker, {"pid": os.getpid(), "ts_ms": int(time.time() * 1000)})
        self.assertTrue(core.login_prompt_open())

    def test_the_prompt_marks_itself_open_while_the_box_is_up_and_clears_after(self):
        seen = []
        check = {"state": "mismatch", "desktop": ACCOUNT, "cli": "other"}
        with mock.patch.object(core, "account_check", return_value=check), \
                mock.patch.object(core, "claude_exe", return_value="claude.exe"):
            result = core.account_login_prompt(ask=lambda title, text: seen.append(core.login_prompt_open()) or False)
        self.assertEqual((result["answer"], seen), ("no", [True]))
        self.assertFalse(core.login_prompt_marker_path().exists())
        with mock.patch.object(core, "account_check", return_value=check), \
                mock.patch.object(core, "claude_exe", return_value="claude.exe"):
            core.account_login_prompt(ask=lambda title, text: seen.append(core.login_prompt_open()) or True,
                                      launch=lambda argv: seen.append(core.login_prompt_open()))
        self.assertEqual(seen, [True, True, True])
        self.assertFalse(core.login_prompt_marker_path().exists())


class SettleTests(RenameNowBase):
    def test_a_candidate_list_that_keeps_changing_keeps_waiting(self):
        self.chat(2)
        self.assertEqual(self.h.rename_now()["skipped"], "candidates-changing")
        self.now += helper.RENAME_NOW_QUIET_S - 5
        self.chat(3)                                   # Desktop turned Remote Control on for one more chat
        self.assertEqual(self.h.rename_now()["skipped"], "candidates-changing")
        self.now += helper.RENAME_NOW_QUIET_S - 5
        self.assertEqual(self.h.rename_now()["skipped"], "candidates-changing")
        self.now += 10
        self.assertTrue(self.h.rename_now()["ran"])
        self.assertEqual(self.notice.shown_with, [2])


class SkipReasonLogTests(RenameNowBase):
    def test_idle_path_logs_only_when_the_skip_reason_changes(self):
        self.chat(2)
        self.platform.exe = "other.exe"
        for _ in range(5):
            self.now += 15
            self.h.rename_chats()
        self.assertEqual(self.log_text().count("rename skipped: desktop-not-in-front"), 1)
        self.platform.exe, self.platform.idle = "claude.exe", 1.0
        for _ in range(4):
            self.now += 15
            self.h.rename_chats()
        self.assertEqual(self.log_text().count("rename skipped: owner-active"), 1)
        core.controls_update(halt=True)
        self.now += 15
        self.h.rename_chats()
        self.now += 15
        self.h.rename_chats()
        core.controls_update(halt=False)
        self.assertEqual(self.log_text().count("rename skipped: controls"), 1)
        self.platform.idle = 60.0
        self.h.last_rename_scan_s = 0.0
        self.h.rename_chats()                             # attempts the rename: no skip line, the attempt has its own
        self.assertEqual(self.log_text().count("rename skipped"), 3)
        self.h.last_rename_scan_s = 0.0
        self.now += 100
        self.h.rename_chats()
        self.assertEqual(self.log_text().count("rename skipped: nothing-to-rename"), 1)
        self.h.rename_chats()                             # throttled: not a new reason
        self.assertEqual(self.log_text().count("rename skipped"), 4)

    def test_the_same_reason_after_an_attempt_is_logged_again(self):
        self.chat(2)
        self.platform.exe = "other.exe"
        self.h.rename_chats()
        self.platform.exe = "claude.exe"
        self.now += 15
        self.h.rename_chats()                             # renames chat 2
        self.platform.exe = "other.exe"
        self.chat(3)
        self.h.last_rename_scan_s = 0.0
        self.now += 15
        self.h.rename_chats()
        self.assertEqual(self.log_text().count("rename skipped: desktop-not-in-front"), 2)

    def test_the_rename_now_pass_logs_its_waiting_reason_once(self):
        self.chat(2)
        self.set_load(age_s=10)
        for _ in range(4):
            self.h.rename_now()
        self.assertEqual(self.log_text().count("rename-now skipped: settling"), 1)

    def test_a_tick_runs_the_pass_and_then_not_the_idle_rename(self):
        self.chat(2)
        with mock.patch.object(self.h, "rename_now", return_value={"ran": True}) as now_pass, \
                mock.patch.object(self.h, "rename_chats", return_value={"attempted": 0}) as idle:
            self.h.tick()
        now_pass.assert_called_once()
        idle.assert_not_called()
        with mock.patch.object(self.h, "rename_now", return_value={"ran": False, "skipped": "x"}), \
                mock.patch.object(self.h, "rename_chats", return_value={"attempted": 0}) as idle:
            self.h.tick()
        idle.assert_called_once()


class NoticeHandleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="crosstalk-notice-test-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.napped = []

    def notice(self, ack=True):
        holder = {}

        def spawn(folder, token):
            holder["token"] = token
            if ack:
                (folder / notice_mod.ACK_FILE).write_text(json.dumps({"token": token, "shown": True}))
        handle = notice_mod.Notice(self.folder, spawn=spawn, sleep=self.napped.append)
        return handle, holder

    def status(self):
        return json.loads((self.folder / notice_mod.NOTICE_FILE).read_text())

    def test_show_writes_the_status_and_waits_for_the_window(self):
        handle, holder = self.notice()
        self.assertTrue(handle.show(3))
        self.assertEqual(holder["token"], handle.token)
        self.assertEqual((self.status()["state"], self.status()["token"]), ("running", handle.token))
        self.assertIn("(3 chats)", self.status()["text"])

    def test_a_window_that_never_shows_is_closed_and_reported(self):
        handle, _ = self.notice(ack=False)
        self.assertFalse(handle.show(3))
        self.assertEqual(self.status()["state"], "failed")
        self.assertTrue(self.napped)

    def test_a_stale_ack_from_another_window_does_not_count(self):
        (self.folder / notice_mod.ACK_FILE).write_text(json.dumps({"token": "old", "shown": True}))
        handle, _ = self.notice(ack=False)
        self.assertFalse(handle.show(1))

    def test_a_failed_start_is_reported(self):
        def spawn(folder, token):
            raise OSError("no")
        self.assertFalse(notice_mod.Notice(self.folder, spawn=spawn, sleep=lambda s: None).show(1))

    def test_cancel_is_read_by_token(self):
        handle, _ = self.notice()
        handle.show(1)
        self.assertFalse(handle.cancelled())
        (self.folder / notice_mod.CANCEL_FILE).write_text(json.dumps({"token": "someone-else"}))
        self.assertFalse(handle.cancelled())
        (self.folder / notice_mod.CANCEL_FILE).write_text(json.dumps({"token": handle.token}))
        self.assertTrue(handle.cancelled())

    def test_update_and_close(self):
        handle, _ = self.notice()
        handle.show(2)
        handle.update("Chat 1 of 2")
        self.assertEqual((self.status()["state"], self.status()["text"]), ("running", "Chat 1 of 2"))
        handle.close("done", "All set")
        self.assertEqual((self.status()["state"], self.status()["text"]), ("done", "All set"))

    def test_window_decision(self):
        status = {"token": "t", "state": "running", "text": "x", "ts_ms": 1_000_000}
        decide = notice_mod.window_decision
        self.assertEqual(decide(status, "t", 1010.0, None)[0], "run")
        self.assertEqual(decide(status, "other", 1010.0, None)[0], "close")                 # another pass's file
        self.assertEqual(decide(None, "t", 1010.0, None)[0], "close")                       # file gone
        self.assertEqual(decide(status, "t", 1000.0 + notice_mod.STALE_S + 1, None)[0], "close")   # helper died
        done = dict(status, state="done", text="All set")
        action, text, started = decide(done, "t", 1010.0, None)
        self.assertEqual((action, text, started), ("final", "All set", 1010.0))
        self.assertEqual(decide(done, "t", 1010.0 + notice_mod.CLOSE_AFTER_S["done"] + 0.1, started)[0], "close")


class FocusPieceTests(unittest.TestCase):
    focus = None

    @classmethod
    def setUpClass(cls):
        cls.focus = core._focus_module()

    def test_desktop_main_window_finds_the_claude_desktop_window_only(self):
        window = FakeWindow()
        window.windows = {10: (1, "ConsoleWindowClass", "claude"), 11: (2, "Chrome_WidgetWin_1", "Some page"),
                          12: (50, "Chrome_WidgetWin_1", "Settings"), DESKTOP_HWND: (50, "Chrome_WidgetWin_1", "Claude"),
                          13: (50, "TkTopLevel", "Crosstalk: getting your chats ready")}
        window.exes = {1: "claude.exe", 2: "chrome.exe", 50: "claude.exe"}
        self.assertEqual(self.focus.desktop_main_window(window), DESKTOP_HWND)
        del window.windows[DESKTOP_HWND]
        self.assertEqual(self.focus.desktop_main_window(window), 12)      # no window titled Claude: another one of it
        window.windows = {10: (1, "ConsoleWindowClass", "claude")}
        self.assertIsNone(self.focus.desktop_main_window(window))
        window.available = False
        self.assertIsNone(self.focus.desktop_main_window(window))

    def test_forward_own_box_accepts_the_notice_window_class(self):
        window = FakeWindow()
        window.windows = {77: (os.getpid(), "TkTopLevel", "T")}
        self.assertEqual(self.focus.forward_own_box("T", platform=window, sleep=lambda s: None), "not-shown")
        self.assertEqual(self.focus.forward_own_box("T", platform=window, sleep=lambda s: None,
                                                    classes=("TkTopLevel",)), "plain")
        self.assertEqual(window.raised, [77])


class RenamerSinceTickTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"CROSSTALK_P0_RENAME": "", "CROSSTALK_P0_FOCUS": ""})
        env.start()
        self.addCleanup(env.stop)
        self.platform = NowPlatform()
        self.record = {"title": "Plan", "titleSource": "auto", "remote": True}
        self.calls = []

        def run_script(current, new):
            self.calls.append((current, new))
            self.platform.tick += 1
            self.record.update(title=new, titleSource="user")
            return {"outcome": "renamed", "step": "done", "reason": "", "exit": 0}
        self.local = "local_00000002-2222-4333-8444-555555555555"
        self.renamer = rename.Renamer(platform=self.platform, records=lambda lid: dict(self.record),
                                      run_script=run_script, controls_check=lambda: None, sleep=lambda s: None)

    def test_since_tick_replaces_the_idle_wait(self):
        self.platform.idle = 0.5                          # the owner would look active in the idle mode
        self.assertEqual(self.renamer.rename(self.local, "Plan B")["reason"], "owner-active")
        result = self.renamer.rename(self.local, "Plan B", since_tick=1000)
        self.assertTrue(result["ok"])
        self.assertEqual(result["input_tick"], 1001)      # the next chat starts from here

    def test_owner_input_since_the_tick_is_refused_before_anything_is_typed(self):
        result = self.renamer.rename(self.local, "Plan B", since_tick=999)
        self.assertEqual((result["outcome"], result["reason"], result["attempted"]), ("refused", "owner-input", False))
        self.assertEqual(self.calls, [])

    def test_the_other_guards_still_apply_with_since_tick(self):
        self.platform.exe = "notepad.exe"
        self.assertEqual(self.renamer.rename(self.local, "Plan B", since_tick=1000)["reason"], "desktop-not-in-front")
        self.platform.exe = "claude.exe"
        with mock.patch.dict(os.environ, {"CROSSTALK_P0_RENAME": "off"}):
            self.assertEqual(self.renamer.rename(self.local, "Plan B", since_tick=1000)["reason"], "rename-off")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
