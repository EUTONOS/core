"""Offline tests: the Crosstalk rename tool (slice 3B.10) and the helper's use of it.

No real window, no powershell, no Claude Desktop: the platform, the script runner, the chat focus and the Desktop
records are fakes. A failed guard must send nothing at all, so the script runner records every call it gets."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock


KIT = Path(__file__).resolve().parents[1] / "tools" / "crosstalk"
spec = importlib.util.spec_from_file_location("crosstalk_rename_core", KIT / "crosstalk_p0_core.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
with mock.patch.dict("sys.modules", {"crosstalk_p0_core": core}):
    rename_spec = importlib.util.spec_from_file_location("crosstalk_rename_under_test", KIT / "crosstalk_rename.py")
    rename = importlib.util.module_from_spec(rename_spec)
    rename_spec.loader.exec_module(rename)
    helper_spec = importlib.util.spec_from_file_location("crosstalk_rename_helper", KIT / "crosstalk_helper.py")
    helper = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(helper)

ACCOUNT = "88888888-2222-4333-8444-555555555555"
ORG = "77777777-2222-4333-8444-555555555555"
LOCAL = "local_00000002-2222-4333-8444-555555555555"
OTHER = "local_00000003-2222-4333-8444-555555555555"
TITLE = "Plan the garden"
TEMP = TITLE + " ·"


class FakePlatform:
    available = True

    def __init__(self, exe="claude.exe", idle=60.0):
        self.exe, self.idle, self.tick = exe, idle, 1000

    def foreground(self):
        return {"hwnd": 7, "pid": 8, "exe": self.exe}

    def idle_seconds(self):
        return self.idle

    def last_input_tick(self):
        return self.tick


class FakeDesktop:
    """Desktop records plus a script runner that renames them. `plan` lists the next outcomes (default: renamed)."""

    def __init__(self, title=TITLE, source="tool", platform=None):
        self.record = {"title": title, "titleSource": source, "remote": True}
        self.calls, self.plan, self.platform = [], [], platform

    def records(self, local_id):
        return dict(self.record) if local_id == LOCAL else None

    def run_script(self, current, new):
        self.calls.append((current, new))
        outcome = self.plan.pop(0) if self.plan else "renamed"
        if self.platform is not None:
            self.platform.tick += 1  # the tool's own keystrokes are input too
        if outcome == "renamed":
            self.record.update(title=new, titleSource="user")
        elif outcome == "renamed-but-record-unchanged":
            return {"outcome": "renamed", "step": "done", "reason": "", "exit": 0}
        return {"outcome": outcome, "step": "before-enter" if outcome == "guard-failed" else "x",
                "reason": "focus-not-in-session-name-edit", "exit": 1}


class FakeChatFocus:
    def __init__(self, sequence):
        self.sequence, self.selected = list(sequence), []

    def current(self):
        return self.sequence.pop(0) if len(self.sequence) > 1 else self.sequence[0]

    def select(self, local_id):
        self.selected.append(local_id)


class RenamerBase(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"CROSSTALK_P0_RENAME": "", "CROSSTALK_P0_FOCUS": ""})
        env.start()
        self.addCleanup(env.stop)
        self.platform = FakePlatform()
        self.desktop = FakeDesktop(platform=self.platform)
        self.controls = None
        self.chat = None

    def renamer(self, **options):
        options.setdefault("chat_focus", self.chat)
        return rename.Renamer(platform=self.platform, records=self.desktop.records, run_script=self.desktop.run_script,
                              controls_check=lambda: self.controls, sleep=lambda s: None, **options)


class GuardTests(RenamerBase):
    def assertSentNothing(self, result, reason):
        self.assertEqual(self.desktop.calls, [])
        self.assertEqual(result["outcome"], "refused")
        self.assertEqual(result["reason"], reason)
        self.assertFalse(result["attempted"])

    def test_desktop_not_in_front_sends_nothing(self):
        self.platform.exe = "chrome.exe"
        self.assertSentNothing(self.renamer().rename(LOCAL, TITLE), "desktop-not-in-front")

    def test_owner_not_idle_sends_nothing(self):
        self.platform.idle = 3.0
        self.assertSentNothing(self.renamer().rename(LOCAL, TITLE), "owner-active")
        self.platform.idle = 15.0  # the threshold itself is enough
        self.assertTrue(self.renamer().rename(LOCAL, "Other name")["ok"])

    def test_unknown_idle_is_not_idle(self):
        self.platform.idle = None
        self.assertSentNothing(self.renamer().rename(LOCAL, TITLE), "owner-active")

    def test_controls_off_or_halted_sends_nothing(self):
        for reason in ("halted", "messaging-off"):
            self.controls = reason
            self.assertSentNothing(self.renamer().rename(LOCAL, TITLE), "controls:" + reason)

    def test_kill_switch(self):
        for value in ("off", "0", "false", "NO"):
            with mock.patch.dict(os.environ, {"CROSSTALK_P0_RENAME": value}):
                self.assertSentNothing(self.renamer().rename(LOCAL, TITLE), "rename-off")

    def test_no_window_platform(self):
        platform = rename.NullPlatform()
        result = rename.Renamer(platform=platform, records=self.desktop.records, run_script=self.desktop.run_script,
                                sleep=lambda s: None).rename(LOCAL, TITLE)
        self.assertSentNothing(result, "no-window-platform")

    def test_bad_input_sends_nothing(self):
        for local, title, reason in (("local_x", TITLE, "not-a-desktop-chat-id"), (LOCAL, "  ", "bad-title"),
                                     (LOCAL, "two\nlines", "bad-title"), (LOCAL, "x" * 201, "bad-title"),
                                     (OTHER, TITLE, "no-desktop-record")):
            self.assertSentNothing(self.renamer().rename(local, title), reason)

    def test_chat_without_title_sends_nothing(self):
        self.desktop.record["title"] = ""
        self.assertSentNothing(self.renamer().rename(LOCAL, TITLE), "chat-has-no-title")

    def test_dry_run_reports_every_guard_and_touches_nothing(self):
        self.platform.exe = "chrome.exe"
        self.chat = FakeChatFocus([OTHER])
        result = self.renamer().rename(LOCAL, TITLE, dry_run=True)
        self.assertEqual((result["outcome"], result["reason"]), ("refused", "desktop-not-in-front"))
        self.assertEqual(set(result["guards"]), {"kill_switch", "controls", "platform", "desktop_in_front", "owner_idle"})
        self.assertFalse(result["guards"]["desktop_in_front"]["ok"])
        self.assertTrue(result["guards"]["owner_idle"]["ok"])
        self.platform.exe = "claude.exe"
        result = self.renamer().rename(LOCAL, TITLE, dry_run=True)
        self.assertEqual((result["ok"], result["outcome"], result["push"], result["plan_steps"]),
                         (True, "dry-run", True, 2))
        self.assertEqual(self.desktop.calls, [])
        self.assertEqual(self.chat.selected, [])


class RenameTests(RenamerBase):
    def test_plain_rename_one_step_verified(self):
        result = self.renamer().rename(LOCAL, "New name")
        self.assertEqual(self.desktop.calls, [(TITLE, "New name")])
        self.assertEqual((result["ok"], result["outcome"], result["push"], result["verified"]),
                         (True, "renamed", False, True))
        self.assertEqual(self.desktop.record["titleSource"], "user")

    def test_push_renames_twice_through_the_temporary_name(self):
        result = self.renamer().rename(LOCAL, TITLE)
        self.assertEqual(self.desktop.calls, [(TITLE, TEMP), (TEMP, TITLE)])
        self.assertEqual((result["ok"], result["push"], result["temp_state"], result["title_left"]),
                         (True, True, False, "as-requested"))
        self.assertEqual(self.desktop.record, {"title": TITLE, "titleSource": "user", "remote": True})

    def test_push_from_a_title_that_already_ends_in_the_suffix(self):
        self.desktop.record["title"] = TEMP
        self.renamer().rename(LOCAL, TEMP)
        self.assertEqual(self.desktop.calls, [(TEMP, TITLE), (TITLE, TEMP)])

    def test_first_step_failure_leaves_the_title_alone(self):
        self.desktop.plan = ["guard-failed"]
        result = self.renamer().rename(LOCAL, TITLE)
        self.assertEqual(len(self.desktop.calls), 1)
        self.assertEqual((result["ok"], result["attempted"], result["temp_state"], result["title_left"]),
                         (False, True, False, "unchanged"))
        self.assertIn("script:guard-failed:before-enter", result["reason"])

    def test_second_step_failure_reports_the_temporary_title(self):
        self.desktop.plan = ["renamed", "not-found"]
        result = self.renamer().rename(LOCAL, TITLE)
        self.assertEqual(len(self.desktop.calls), 2)
        self.assertEqual((result["ok"], result["temp_state"], result["title_left"]), (False, True, "temporary"))
        self.assertEqual(self.desktop.record["title"], TEMP)

    def test_owner_input_between_the_steps_aborts_the_push(self):
        def nap(seconds):  # the owner touches the keyboard while the tool waits for Desktop's record
            self.platform.tick += 50
        renamer = rename.Renamer(platform=self.platform, records=self.desktop.records,
                                 run_script=self.desktop.run_script, sleep=nap)
        result = renamer.rename(LOCAL, TITLE)
        self.assertEqual(len(self.desktop.calls), 1)
        self.assertEqual(result["reason"], "between-steps:owner-input")
        self.assertTrue(result["temp_state"])

    def test_own_keystrokes_do_not_count_as_owner_input(self):
        result = self.renamer().rename(LOCAL, TITLE)  # FakeDesktop bumps the tick on every script call
        self.assertTrue(result["ok"])

    def test_desktop_leaving_the_front_between_the_steps_aborts_the_push(self):
        original = self.desktop.run_script

        def run(current, new):
            out = original(current, new)
            self.platform.exe = "chrome.exe"
            return out
        result = rename.Renamer(platform=self.platform, records=self.desktop.records, run_script=run,
                                sleep=lambda s: None).rename(LOCAL, TITLE)
        self.assertEqual(len(self.desktop.calls), 1)
        self.assertEqual(result["reason"], "between-steps:desktop-not-in-front")
        self.assertTrue(result["temp_state"])

    def test_record_that_does_not_show_the_name_fails_verification(self):
        self.desktop.plan = ["renamed-but-record-unchanged"]
        result = self.renamer().rename(LOCAL, "New name")
        self.assertFalse(result["ok"])
        self.assertTrue(result["reason"].startswith("verify-failed"))

    def test_title_source_must_be_user(self):
        original = self.desktop.run_script

        def run(current, new):
            out = original(current, new)
            self.desktop.record["titleSource"] = "tool"
            return out
        result = rename.Renamer(platform=self.platform, records=self.desktop.records, run_script=run,
                                sleep=lambda s: None).rename(LOCAL, "New name")
        self.assertFalse(result["ok"])

    def test_log_line_has_no_titles(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "rename" / "log.jsonl"
            self.renamer(log_path=log).rename(LOCAL, TITLE)
            text = log.read_text(encoding="utf-8")
        self.assertIn(LOCAL, text)
        self.assertNotIn("garden", text)
        self.assertEqual(json.loads(text.splitlines()[0])["outcome"], "renamed")


class ChatRestoreTests(RenamerBase):
    def test_restores_the_chat_that_was_showing(self):
        self.chat = FakeChatFocus([OTHER, LOCAL])  # before the rename, after it
        result = self.renamer().rename(LOCAL, "New name")
        self.assertEqual(self.chat.selected, [OTHER])
        self.assertEqual((result["chat_restore_needed"], result["chat_restored"]), (True, True))

    def test_no_restore_when_the_chat_did_not_change(self):
        self.chat = FakeChatFocus([OTHER, OTHER])
        result = self.renamer().rename(LOCAL, "New name")
        self.assertEqual(self.chat.selected, [])
        self.assertEqual((result["chat_restore_needed"], result["chat_restored"]), (False, None))

    def test_unknown_chat_before_means_nothing_to_restore(self):
        self.chat = FakeChatFocus([None])
        self.assertIsNone(self.renamer().rename(LOCAL, "New name")["chat_restore_needed"])
        self.assertEqual(self.chat.selected, [])

    def test_owner_input_after_the_script_blocks_the_restore(self):
        self.chat = FakeChatFocus([OTHER, LOCAL])
        sleeps = []

        def nap(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 2:  # the owner clicks while the tool settles
                self.platform.tick += 99
        renamer = rename.Renamer(platform=self.platform, records=self.desktop.records, chat_focus=self.chat,
                                 run_script=self.desktop.run_script, sleep=nap)
        result = renamer.rename(LOCAL, "New name")
        self.assertEqual(self.chat.selected, [])
        self.assertEqual((result["chat_restore_needed"], result["chat_restored"]), (True, False))

    def test_an_unreadable_input_clock_blocks_the_restore(self):
        self.chat = FakeChatFocus([OTHER, LOCAL])
        reads = []

        def tick():
            reads.append(1)
            return None if len(reads) > 1 else self.platform.tick  # readable for the guards, then unreadable
        self.platform.last_input_tick = tick
        result = self.renamer().rename(LOCAL, "New name")
        self.assertEqual(self.chat.selected, [])
        self.assertEqual((result["chat_restore_needed"], result["chat_restored"]), (True, False))
        self.assertEqual(result["chat_restore_skipped"], "input-unreadable")

    def test_a_clock_that_raises_blocks_the_restore(self):
        self.chat = FakeChatFocus([OTHER, LOCAL])

        def broken():
            raise OSError("no clock")
        self.platform.last_input_tick = broken
        result = self.renamer().rename(LOCAL, "New name")
        self.assertEqual(self.chat.selected, [])
        self.assertEqual(result["chat_restore_skipped"], "input-unreadable")

    def test_restore_also_runs_after_a_failed_rename(self):
        self.chat = FakeChatFocus([OTHER, LOCAL])
        self.desktop.plan = ["guard-failed"]
        self.renamer().rename(LOCAL, "New name")
        self.assertEqual(self.chat.selected, [OTHER])

    def test_refused_rename_never_reads_the_chat_focus(self):
        self.chat = FakeChatFocus([OTHER, LOCAL])
        self.platform.exe = "chrome.exe"
        self.renamer().rename(LOCAL, "New name")
        self.assertEqual(self.chat.selected, [])


class MinIdleFloorTests(RenamerBase):
    def test_the_idle_threshold_is_clamped_to_a_positive_floor(self):
        for value in (0, 0.0, -5, 0.5, 1.99):
            self.assertEqual(self.renamer(min_idle_s=value).min_idle_s, rename.MIN_IDLE_FLOOR_S, value)
        self.assertEqual(self.renamer(min_idle_s=30).min_idle_s, 30.0)
        for value in (float("nan"), float("inf"), None, "x", True):
            self.assertEqual(self.renamer(min_idle_s=value).min_idle_s, rename.DEFAULT_MIN_IDLE_S, value)
        self.assertGreater(rename.MIN_IDLE_FLOOR_S, 0)

    def test_min_idle_zero_no_longer_lets_a_rename_run_while_the_owner_types(self):
        self.platform.idle = 0.3  # the owner pressed a key a moment ago
        result = self.renamer(min_idle_s=0).rename(LOCAL, "New name")
        self.assertEqual((result["outcome"], result["reason"]), ("refused", "owner-active"))
        self.assertEqual(self.desktop.calls, [])
        self.assertEqual(result["guards"]["owner_idle"]["need_s"], rename.MIN_IDLE_FLOOR_S)

    def test_the_command_line_value_is_clamped_too(self):
        with mock.patch.object(rename, "default_renamer") as make, mock.patch.object(
                sys, "path", list(sys.path)), mock.patch.dict(sys.modules, {"crosstalk_p0_core": mock.Mock()}):
            make.return_value.rename.return_value = {"ok": True}
            rename.main(["--session", LOCAL, "--title", "x", "--min-idle", "0"])
        self.assertEqual(make.call_args.kwargs["min_idle_s"], rename.MIN_IDLE_FLOOR_S)


class ScriptTests(unittest.TestCase):
    def test_run_script_passes_base64_and_parses_the_result_line(self):
        done = mock.Mock(stdout='noise\n{"tag":"crosstalk.rename.ps.v1","outcome":"renamed","step":"done","reason":""}\n',
                         returncode=0)
        with mock.patch.object(rename.subprocess, "run", return_value=done) as run:
            out = rename.run_script("Old ·", "Néw")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:6], ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                    "-File"])
        self.assertTrue(argv[6].endswith("crosstalk_rename.ps1"))
        import base64
        self.assertEqual(base64.b64decode(argv[8]).decode("utf-8"), "Old ·")
        self.assertEqual(base64.b64decode(argv[10]).decode("utf-8"), "Néw")
        self.assertEqual((out["outcome"], out["step"], out["exit"]), ("renamed", "done", 0))

    def test_run_script_failures_never_raise(self):
        with mock.patch.object(rename.subprocess, "run", side_effect=rename.subprocess.TimeoutExpired("x", 1)):
            self.assertEqual(rename.run_script("a", "b")["reason"], "timeout")
        with mock.patch.object(rename.subprocess, "run", side_effect=FileNotFoundError()):
            self.assertEqual(rename.run_script("a", "b")["outcome"], "error")
        with mock.patch.object(rename.subprocess, "run", return_value=mock.Mock(stdout="garbage", returncode=1)):
            self.assertEqual(rename.run_script("a", "b")["reason"], "no-result")

    def test_script_guards_come_before_every_keystroke_and_no_clipboard(self):
        text = (KIT / "crosstalk_rename.ps1").read_text(encoding="utf-8")
        self.assertNotIn("Clipboard", text)
        self.assertNotIn("Set-Clipboard", text)
        order = ["Assert-Guard 'before-select-all' $Current", "SendWait('^a')", "Assert-Guard 'before-typing' $null",
                 "Send-Text $New", "Assert-Guard 'before-enter' $New", "SendWait('{ENTER}')"]
        positions = [text.index(item) for item in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("'+^%~(){}[]'", text)        # SendKeys specials are escaped
        self.assertIn("{ESCAPE}", text)
        self.assertEqual(text.count("SendWait('{ESCAPE}')"), 1)  # sent only from the guard-failure path
        self.assertNotIn("clip", text.replace("the clipboard", "").lower())
        self.assertIn("'Session name'", text)

    def test_script_lookups_poll_and_refuse_duplicate_titles(self):
        # Desktop's accessibility tree is built lazily; a single FindFirst missed the sidebar (1 rename in 11, 2026-10-21).
        text = (KIT / "crosstalk_rename.ps1").read_text(encoding="utf-8")
        self.assertNotIn("FindFirst", text)
        self.assertIn("Write-Result 'not-unique' 'more-options-button'", text)
        self.assertIn("Write-Result 'not-unique' 'session-name-edit'", text)
        self.assertLess(text.index("not-unique' 'more-options-button'"), text.index("::Pattern).Expand()"))
        # Desktop's inline rename Edit is named "Rename" (2026-10-21); the older dialog's "Session name" still counts.
        self.assertIn("$NameProperty, 'Rename')", text.replace("$A::", "$"))
        self.assertIn("$f.Current.Name -ne 'Rename'", text)
        self.assertIn("(Edit-Value $_) -ceq $Current", text)

    def test_default_platform_is_null_with_focus_off(self):
        with mock.patch.dict(os.environ, {"CROSSTALK_P0_FOCUS": "off"}):
            self.assertFalse(rename.default_platform().available)

    def test_temp_title(self):
        self.assertEqual(rename.temp_title("A"), "A ·")
        self.assertEqual(rename.temp_title("A ·"), "A")


# ------------------------------------------------------------------ the helper
def chat_id(number: int) -> tuple[str, str]:
    return (f"local_{number:08d}-2222-4333-8444-555555555555", f"cccc{number:04d}-2222-4333-8444-555555555555")


class FakeRenamer:
    """Stands in for Renamer inside the helper: records calls, edits the fixture records like Desktop would."""

    def __init__(self, folder: Path, front=True):
        self.folder, self.front = folder, front
        self.calls, self.plan = [], []

    def records(self, local_id):
        row = core._read_json(self.folder / f"{local_id}.json", None)
        return {"title": row.get("title"), "titleSource": row.get("titleSource")} if row else None

    def guards(self):
        return {"ok": self.front, "reason": None if self.front else "desktop-not-in-front", "checks": {}}

    def rename(self, local_id, title):
        self.calls.append((local_id, title))
        outcome = self.plan.pop(0) if self.plan else "ok"
        result = {"ok": outcome == "ok", "outcome": "renamed" if outcome == "ok" else "failed", "attempted": True,
                  "push": True, "temp_state": False, "title_left": "as-requested", "reason": None,
                  "chat_restore_needed": False, "chat_restored": None}
        path = self.folder / f"{local_id}.json"
        row = core._read_json(path, {})
        if outcome == "ok":
            row.update(titleSource="user")
        elif outcome == "temp":  # step 1 worked, step 2 did not: the chat keeps the temporary name
            row.update(title=rename.temp_title(title), titleSource="user")
            result.update(temp_state=True, title_left="temporary")
        elif outcome == "restored":  # a restore rename (title -> original) worked
            row.update(title=title, titleSource="user")
            result.update(ok=True, outcome="renamed", push=False)
        elif outcome == "refused":
            result.update(ok=False, outcome="refused", attempted=False)
        core._write_json_atomic(path, row)
        return result


class HelperRenameTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="crosstalk-rename-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = self.root / "main.log"
        self.base = self.root / "AppData" / "claude-code-sessions"
        self.folder = self.base / ACCOUNT / ORG
        self.folder.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"CROSSTALK_P0_CODEX_STATE_DB": str(self.root / "no-codex-state.sqlite"), "CROSSTALK_P0_HERMES_HOME": str(self.root / "no-hermes"), "CROSSTALK_P0_DEVICE_DIR": str(self.root / "device"),
                                          "CROSSTALK_P0_CLAUDE_HOME": str(self.root / "claude"),
                                          "CROSSTALK_P0_CLAUDE_JSON": str(self.root / "claude.json"),
                                          "CROSSTALK_P0_CLAUDE_DESKTOP_LOG": str(self.log),
                                          "CROSSTALK_P0_CLAUDE_DESKTOP_DIR": str(self.base),
                                          "CROSSTALK_P0_FOCUS": "off", "CROSSTALK_P0_RENAME": ""})
        env.start()
        self.addCleanup(env.stop)
        (self.root / "claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": ACCOUNT}}))
        core.controls_update(messaging="on", halt=False)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 600))
        self.log.write_text(f"{stamp} [info] Loaded 9 persisted sessions from {self.folder}\n", encoding="utf-8")
        self.now = time.time()
        self.h = helper.Helper(clock=lambda: self.now)
        self.fake = FakeRenamer(self.folder)
        self.h.renamer = self.fake

    def chat(self, number, **extra):
        local, session = chat_id(number)
        row = dict({"cliSessionId": session, "title": f"Chat {number}", "titleSource": "auto",
                    "remoteControlUserEnabled": True}, **extra)
        (self.folder / f"{local}.json").write_text(json.dumps(row), encoding="utf-8")
        return local

    def tick(self, advance=15):
        self.now += advance
        return self.h.rename_chats()

    def test_picks_one_eligible_chat_per_tick(self):
        first, second = self.chat(2), self.chat(3, titleSource="tool")
        self.chat(4, titleSource="user")                       # already a real name
        self.chat(5, remoteControlUserEnabled=False)           # Remote Control off
        self.chat(6, isArchived=True)                          # archived
        self.chat(7, title="")                                 # no title
        self.chat(8, scheduledTaskId="task")                   # a routine
        one = self.tick()
        self.assertEqual(self.fake.calls, [(first, "Chat 2")])
        self.assertEqual((one["attempted"], one["outcome"]), (1, "renamed"))
        self.tick()
        self.assertEqual(self.fake.calls[1], (second, "Chat 3"))
        self.assertEqual(self.tick()["skipped"], "nothing-to-rename")
        self.assertEqual(len(self.fake.calls), 2)

    def test_remote_on_from_desktop_log_when_the_flag_is_blank(self):
        local = self.chat(2, remoteControlUserEnabled=None)
        skipped = self.chat(3, remoteControlUserEnabled=None)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with self.log.open("a", encoding="utf-8") as stream:
            stream.write(f"{stamp} [info] Enabling remote control for session {local}\n")
            stream.write(f"{stamp} [info] Enabling remote control for session {skipped}\n")
            stream.write(f"{stamp} [info] Disabling remote control for session {skipped}\n")
        self.tick()
        self.assertEqual([call[0] for call in self.fake.calls], [local])

    def test_desktop_not_in_front_does_nothing(self):
        self.chat(2)
        self.fake.front = False
        result = self.tick()
        self.assertEqual((result["attempted"], result["skipped"]), (0, "desktop-not-in-front"))
        self.assertEqual(self.fake.calls, [])
        self.fake.front = True
        self.tick()
        self.assertEqual(len(self.fake.calls), 1)

    def test_owner_not_idle_does_nothing_with_the_real_renamer(self):
        self.chat(2)
        platform, desktop = FakePlatform(idle=2.0), FakeDesktop()
        self.h.renamer = rename.Renamer(platform=platform, records=desktop.records, run_script=desktop.run_script,
                                        sleep=lambda s: None)
        self.assertEqual(self.tick()["skipped"], "owner-active")
        self.assertEqual(desktop.calls, [])

    def test_kill_switch_and_config_switch(self):
        self.chat(2)
        platform, desktop = FakePlatform(), FakeDesktop()
        self.h.renamer = rename.Renamer(platform=platform, records=desktop.records, run_script=desktop.run_script,
                                        sleep=lambda s: None)
        with mock.patch.dict(os.environ, {"CROSSTALK_P0_RENAME": "off"}):
            self.assertEqual(self.tick()["skipped"], "rename-off")
        self.assertEqual(desktop.calls, [])
        self.h.renamer = self.fake
        core._write_json_atomic(core.helper_dir() / "config.json", {"rename_titles": False})
        self.assertEqual(self.tick()["skipped"], "config-off")
        self.assertEqual(self.fake.calls, [])

    def test_halted_crosstalk_does_nothing(self):
        self.chat(2)
        core.controls_update(halt=True)
        self.assertEqual(self.tick()["skipped"], "controls")
        self.assertEqual(self.fake.calls, [])
        # slice 3B.19: messaging off no longer stops renaming (it was "controls"); halt still does
        core.controls_update(halt=False, messaging="off")
        self.assertEqual(self.tick()["attempted"], 1)
        self.assertEqual(len(self.fake.calls), 1)

    def test_messaging_off_still_skips_a_denied_chat(self):
        # slice 3B.19: renaming ignores messaging but still honors the deny list
        denied, allowed = self.chat(2), self.chat(3)
        core.controls_update(messaging="off", deny_add="claude:" + chat_id(2)[1])
        self.assertEqual(self.tick()["local"], allowed)
        self.assertEqual(self.fake.calls, [(allowed, "Chat 3")])
        self.assertNotIn(denied, [call[0] for call in self.fake.calls])

    def test_failure_backs_off_ten_minutes_and_gives_up_after_three(self):
        local = self.chat(2)
        self.fake.plan = ["fail", "fail", "fail"]
        self.tick()
        row = lambda: self.h.rename_ledger()["chats"][self.h.rename_key(local, "Chat 2")]  # noqa: E731
        self.assertEqual(row()["fails"], 1)
        self.assertEqual(self.tick(60)["skipped"], "nothing-to-rename")  # inside the back-off
        self.assertEqual(len(self.fake.calls), 1)
        self.now += 600
        self.h.last_rename_scan_s = 0.0
        self.tick()
        self.assertEqual(len(self.fake.calls), 2)
        self.now += 700
        self.tick()
        self.assertEqual((len(self.fake.calls), row()["fails"]), (3, 3))
        self.now += 100_000                                   # given up for this chat and title
        self.h.last_rename_scan_s = 0.0
        self.assertEqual(self.tick()["skipped"], "nothing-to-rename")
        self.assertEqual(len(self.fake.calls), 3)

    def test_success_is_remembered_even_if_the_title_source_is_not_updated(self):
        local = self.chat(2)
        self.tick()
        row = core._read_json(self.folder / f"{local}.json", {})
        row.update(titleSource="tool")                        # something put it back
        (self.folder / f"{local}.json").write_text(json.dumps(row), encoding="utf-8")
        self.assertEqual(self.tick()["skipped"], "nothing-to-rename")
        self.assertEqual(len(self.fake.calls), 1)

    def test_a_new_title_is_a_new_attempt(self):
        local = self.chat(2)
        self.fake.plan = ["fail"] * 3
        for _ in range(3):
            self.now += 700
            self.h.last_rename_scan_s = 0.0
            self.tick()
        self.assertEqual(len(self.fake.calls), 3)
        row = core._read_json(self.folder / f"{local}.json", {})
        row.update(title="Chat 2 renamed by the model", titleSource="tool")
        (self.folder / f"{local}.json").write_text(json.dumps(row), encoding="utf-8")
        self.tick()
        self.assertEqual(self.fake.calls[-1], (local, "Chat 2 renamed by the model"))

    def test_a_refused_attempt_is_not_a_failure(self):
        self.chat(2)
        self.fake.plan = ["refused"]
        self.assertEqual(self.tick()["attempted"], 0)
        self.assertEqual(self.h.rename_ledger()["chats"], {})
        self.tick()
        self.assertEqual(len(self.fake.calls), 2)

    def test_temporary_title_is_put_back_once_and_never_over_an_owner_name(self):
        local = self.chat(2)
        self.fake.plan = ["temp"]
        self.tick()
        self.assertEqual(self.h.rename_ledger()["restore"][local]["title"], "Chat 2")
        self.now += 700
        self.h.last_rename_scan_s = 0.0
        self.fake.plan = ["restored"]
        self.tick()
        self.assertEqual(self.fake.calls[-1], (local, "Chat 2"))
        self.assertEqual(self.h.rename_ledger()["restore"], {})
        # owner names the chat by hand while a restore is pending: nothing is overwritten
        other = self.chat(3)
        self.fake.plan = ["temp"]
        self.tick()
        row = core._read_json(self.folder / f"{other}.json", {})
        row.update(title="The owner's own name", titleSource="user")
        (self.folder / f"{other}.json").write_text(json.dumps(row), encoding="utf-8")
        calls = len(self.fake.calls)
        self.tick()
        self.assertEqual(len(self.fake.calls), calls)
        self.assertEqual(self.h.rename_ledger()["restore"], {})

    def test_a_new_remote_session_gets_the_owner_title_once(self):
        old, new = "session_01OldLinkOldLinkOldLink", "session_01NewLinkNewLinkNewLink"
        local = self.chat(2, titleSource="user", bridgeSessionIds=[old])
        self.chat(3, titleSource="user", bridgeSessionIds=["not-a-session"])  # no usable link: left alone
        self.tick()
        self.assertEqual(self.fake.calls, [(local, "Chat 2")])
        self.assertEqual(self.tick()["skipped"], "nothing-to-rename")
        # the owner renames it: that rename reaches the same remote session by itself, so no push
        row = core._read_json(self.folder / f"{local}.json", {})
        row.update(title="Owner's name")
        (self.folder / f"{local}.json").write_text(json.dumps(row), encoding="utf-8")
        self.h.last_rename_scan_s = 0.0
        self.assertEqual(self.tick()["skipped"], "nothing-to-rename")
        # a relink (account switch) appends a new remote session: pushed once more
        row["bridgeSessionIds"] = [old, new]
        (self.folder / f"{local}.json").write_text(json.dumps(row), encoding="utf-8")
        self.h.last_rename_scan_s = 0.0
        self.tick()
        self.assertEqual(self.fake.calls[-1], (local, "Owner's name"))
        self.assertIn(self.h.link_key(local, new), self.h.rename_ledger()["chats"])
        self.h.last_rename_scan_s = 0.0
        self.assertEqual(self.tick()["skipped"], "nothing-to-rename")
        self.assertEqual(len(self.fake.calls), 2)

    def test_an_owner_rename_after_this_runs_relink_needs_no_push(self):
        link = "session_01LinkLinkLinkLinkLinkLink"
        renamed = self.chat(2, titleSource="user", bridgeSessionIds=[link])
        relinked = self.chat(3, titleSource="user", bridgeSessionIds=[link + "3"])
        early = self.chat(4, titleSource="user", bridgeSessionIds=[link + "4"])
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        update = "[info] Updated session {}: {{ title: '<redacted>', titleSource: 'user' }}\n"
        with self.log.open("a", encoding="utf-8") as stream:
            stream.write(f"{stamp} " + update.format(early))       # renamed before its relink: still pushed
            for local in (renamed, relinked, early):
                stream.write(f"{stamp} [info] Enabling remote control for session {local}\n")
            stream.write(f"{stamp} " + update.format(renamed))     # renamed after it: already on the phone
        for _ in range(3):
            self.h.last_rename_scan_s = 0.0
            self.tick()
        self.assertEqual(sorted(call[0] for call in self.fake.calls), sorted([relinked, early]))

    def test_an_auto_title_push_also_covers_its_remote_session(self):
        link = "session_01LinkLinkLinkLinkLinkLink"
        local = self.chat(2, bridgeSessionIds=[link])
        self.tick()                                           # auto -> pushed; the fake leaves titleSource "user"
        self.assertEqual(self.fake.calls, [(local, "Chat 2")])
        self.h.last_rename_scan_s = 0.0
        self.assertEqual(self.tick()["skipped"], "nothing-to-rename")
        self.assertEqual(len(self.fake.calls), 1)

    def test_a_failed_relink_push_backs_off_like_any_other(self):
        local = self.chat(2, titleSource="user", bridgeSessionIds=["session_01LinkLinkLinkLinkLinkLink"])
        self.fake.plan = ["fail"]
        self.tick()
        row = self.h.rename_ledger()["chats"][self.h.link_key(local, "session_01LinkLinkLinkLinkLinkLink")]
        self.assertEqual((row["fails"], row.get("ok_ns")), (1, None))
        self.assertEqual(self.tick(60)["skipped"], "nothing-to-rename")
        self.now += 600
        self.h.last_rename_scan_s = 0.0
        self.tick()
        self.assertEqual(len(self.fake.calls), 2)

    def test_log_line_carries_no_titles(self):
        self.chat(2, title="Secret plan")
        self.tick()
        text = self.h.log.path.read_text(encoding="utf-8")
        self.assertIn("rename local_00000002", text)
        self.assertNotIn("Secret", text)

    def test_tick_includes_the_rename_step(self):
        self.chat(2)
        self.h.plugin_stale = lambda: None
        summary = self.h.tick()
        self.assertEqual(summary["rename"]["outcome"], "renamed")


if __name__ == "__main__":
    unittest.main()
