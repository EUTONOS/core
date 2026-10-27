"""Offline tests: the Crosstalk helper (load detection, restart relink list and guard, letter wake, notices).

Fixture directories and mocks only; no real Claude Desktop, no real claude call, no real app data. The helper's own
clock is injected so settle and age rules run without sleeping."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


KIT = Path(__file__).resolve().parents[1] / "tools" / "crosstalk"
spec = importlib.util.spec_from_file_location("crosstalk_helper_core", KIT / "crosstalk_p0_core.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
with mock.patch.dict("sys.modules", {"crosstalk_p0_core": core}):
    helper_spec = importlib.util.spec_from_file_location("crosstalk_helper_under_test", KIT / "crosstalk_helper.py")
    helper = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(helper)
    cli_spec = importlib.util.spec_from_file_location("crosstalk_helper_cli", KIT / "crosstalk_p0_cli.py")
    cli = importlib.util.module_from_spec(cli_spec)
    cli_spec.loader.exec_module(cli)
    install_spec = importlib.util.spec_from_file_location("crosstalk_helper_install", KIT / "crosstalk_p0_install.py")
    install = importlib.util.module_from_spec(install_spec)
    install_spec.loader.exec_module(install)

ACCOUNT = "88888888-2222-4333-8444-555555555555"
OTHER_ACCOUNT = "99999999-2222-4333-8444-555555555555"
ORG = "77777777-2222-4333-8444-555555555555"
CODEX = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def chat_ids(number: int) -> tuple[str, str]:
    """(local Desktop chat id, CLI session id) for a small number."""
    return (f"local_{number:08d}-2222-4333-8444-555555555555",
            f"cccc{number:04d}-2222-4333-8444-555555555555")


CLAIMANT_LOCAL, CLAIMANT = chat_ids(1)
CHAT_A_LOCAL, CHAT_A = chat_ids(2)
CHAT_B_LOCAL, CHAT_B = chat_ids(3)
RECIPIENT_LOCAL, RECIPIENT = chat_ids(9)


class HelperBase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="crosstalk-helper-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = self.root / "main.log"
        self.base = self.root / "AppData" / "claude-code-sessions"
        self.folder = self.base / ACCOUNT / ORG
        self.folder.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"CROSSTALK_P0_HERMES_HOME": str(self.root / "no-hermes"), "CROSSTALK_P0_DEVICE_DIR": str(self.root / "device"),
                                          "CROSSTALK_P0_CLAUDE_HOME": str(self.root / "claude"),
                                          "CROSSTALK_P0_CLAUDE_JSON": str(self.root / "claude.json"),
                                          "CROSSTALK_P0_CLAUDE_DESKTOP_LOG": str(self.log),
                                          "CROSSTALK_P0_CLAUDE_DESKTOP_DIR": str(self.base),
                                          "CROSSTALK_P0_FOCUS": "off",
                                          "CROSSTALK_P0_CODEX_STATE_DB": str(self.root / "no-codex-state.sqlite")})  # no overlay, no window handling in tests
        env.start()
        self.addCleanup(env.stop)
        (self.root / "claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": ACCOUNT}}))
        core.controls_update(messaging="on", halt=False)
        self.login_prompts = []  # never open the real Yes/No box from a test
        prompt = mock.patch.object(core, "spawn_login_prompt", side_effect=lambda: self.login_prompts.append(1))
        prompt.start()
        self.addCleanup(prompt.stop)
        self.live = {CLAIMANT}
        registration = mock.patch.object(core, "claude_registration", side_effect=lambda sid: {
            "live": sid in self.live, "entrypoint": "claude-desktop", "name": None, "status": None, "cwd": None})
        registration.start()
        self.addCleanup(registration.stop)
        nap = mock.patch.object(core, "FOCUS_SLEEP", lambda seconds: None)  # a confirm timeout must not really wait
        nap.start()
        self.addCleanup(nap.stop)
        self.shows = []  # never launch the real `claude --desktop --resume` from a test
        self.show_result = {"launched": True, "method": "test", "verified": False}
        for name, fake in (("launch_desktop_show", lambda exe, sid: self.shows.append(sid) or dict(self.show_result)),
                           ("claude_exe", lambda: "claude.exe")):
            patcher = mock.patch.object(core, name, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.now = time.time()
        self.h = helper.Helper(clock=lambda: self.now)
        self.h.plugin_stale = lambda: None  # not under test here
        self.chat(CLAIMANT_LOCAL, CLAIMANT)
        self.chat(CHAT_A_LOCAL, CHAT_A, remoteControlUserEnabled=True, title="Chat A")
        self.chat(CHAT_B_LOCAL, CHAT_B, remoteControlUserEnabled=True, title="Chat B")

    # ---- fixture helpers
    def chat(self, local, session, folder=None, **extra):
        target = folder or self.folder
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{local}.json").write_text(json.dumps(dict({"cliSessionId": session}, **extra)))

    def load_line(self, age_s=60, count=3, account=ACCOUNT):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.now - age_s))
        return f"{stamp} [info] Loaded {count} persisted sessions from {self.base / account / ORG}\n"

    def add_log(self, *lines):
        with self.log.open("a", encoding="utf-8") as stream:
            for line in lines:
                stream.write(line)

    def advance(self, seconds):
        self.now += seconds

    def loads(self):
        return core.helper_loads_load()["loads"]

    def only_load(self):
        loads = self.loads()
        self.assertEqual(len(loads), 1)
        return next(iter(loads.items()))

    def journal_bytes(self):
        path = core.journal_path()
        return path.read_bytes() if path.exists() else b""

    def new_load_ready(self, **kwargs):
        """One young load line, recorded, and past the settle time."""
        self.add_log(self.load_line(**kwargs))
        self.assertEqual(self.h.follow_log(), 1)
        self.advance(helper.LOAD_READY_MIN_S + 1)


class LoadDetectionTests(HelperBase):
    def test_a_load_line_is_recorded_once_and_survives_a_helper_restart(self):
        self.add_log("[info] something else\n", self.load_line())
        self.assertEqual(self.h.follow_log(), 1)
        load_id, load = self.only_load()
        self.assertEqual(load["account"], ACCOUNT)
        self.assertEqual(load["phase"], "pending")
        self.assertEqual(self.h.follow_log(), 0)
        self.assertEqual(helper.Helper(clock=lambda: self.now).follow_log(), 0)  # a new process: same answer
        self.assertEqual(list(self.loads()), [load_id])
        self.add_log(self.load_line(age_s=5))  # a later line is a later load
        self.assertEqual(self.h.follow_log(), 1)
        self.assertEqual(len(self.loads()), 2)

    def test_empty_folder_loads_are_not_loads(self):
        self.add_log(self.load_line(count=0), self.load_line(count=0, account=OTHER_ACCOUNT))
        self.assertEqual(self.h.follow_log(), 0)
        self.assertEqual(self.loads(), {})

    def test_first_start_only_acts_on_a_young_load(self):
        self.add_log(self.load_line(age_s=3 * 3600), self.load_line(age_s=60))
        self.assertEqual(self.h.follow_log(), 2)
        phases = sorted(load["phase"] for load in self.loads().values())
        self.assertEqual(phases, ["pending", "skipped:too-old-at-first-start"])

    def test_after_first_start_an_older_stamp_is_still_a_new_load(self):
        self.add_log(self.load_line(age_s=60))
        self.h.follow_log()
        self.add_log(self.load_line(age_s=3 * 3600))  # a late-written line: not first start, so not skipped
        self.assertEqual(self.h.follow_log(), 1)
        self.assertEqual(sorted(load["phase"] for load in self.loads().values()), ["pending", "pending"])

    def test_a_replaced_log_is_read_from_its_start(self):
        self.add_log(self.load_line(age_s=600) + "x" * 600 + "\n")
        self.h.follow_log()
        self.log.write_text("", encoding="utf-8")
        self.add_log(self.load_line(age_s=30))
        self.assertEqual(self.h.follow_log(), 1)

    def test_a_rotated_log_is_read_only_from_its_bounded_tail(self):
        self.add_log(self.load_line(age_s=900) + "x" * 600 + "\n")
        self.h.follow_log()
        old_load = self.load_line(age_s=500)
        recent_load = self.load_line(age_s=20)
        filler = ("filler " * 20 + "\n") * 60  # a rotated file far bigger than the per-tick bound
        self.log.write_text(old_load + filler + recent_load, encoding="utf-8")
        with mock.patch.object(helper, "LOG_FOLLOW_MAX_BYTES", 1500):
            self.assertGreater(self.log.stat().st_size, 1500 * 3)
            self.assertEqual(self.h.follow_log(), 1)  # only the load inside the tail
            self.assertEqual(self.h.follow_log(), 0)  # and it is not read again
        self.assertEqual(core.helper_loads_load()["log"]["offset"], self.log.stat().st_size)
        self.assertEqual(len(self.loads()), 2)  # the first file's load and the rotated file's tail load
        self.add_log(self.load_line(age_s=1))
        self.assertEqual(self.h.follow_log(), 1)  # following goes on normally afterwards

    def test_a_tail_that_still_holds_a_known_load_does_not_record_it_twice(self):
        self.add_log(self.load_line(age_s=900) + "x" * 600 + "\n", self.load_line(age_s=60))
        self.assertEqual(self.h.follow_log(), 2)
        content = self.log.read_text(encoding="utf-8")
        self.log.write_text("y" * 300 + "\n" + content, encoding="utf-8")  # replaced: different start, bigger file
        with mock.patch.object(helper, "LOG_FOLLOW_MAX_BYTES", 1500):
            self.assertEqual(self.h.follow_log(), 0)  # same load ids: deduped, nothing new
        self.assertEqual(len(self.loads()), 2)

    def test_only_the_newest_load_is_acted_on(self):
        self.add_log(self.load_line(age_s=120))
        self.h.follow_log()
        self.advance(5)
        self.add_log(self.load_line(age_s=2))
        self.h.follow_log()
        self.advance(helper.LOAD_READY_MIN_S + 1)
        self.h.process_loads()
        phases = sorted(load["phase"] for load in self.loads().values())
        self.assertEqual(phases, ["enqueued", "superseded"])
        self.assertEqual(sorted(max(self.loads().values(), key=lambda load: load["seen_ns"])["reconnects"]),
                         sorted([CHAT_A_LOCAL, CHAT_B_LOCAL]))  # chats A and B, once


class ReconnectTests(HelperBase):
    """The restart relink list (no relay chat, no letter): the helper lists, one running chat's plugin claims."""

    def rc_line(self, local, on=True, age_s=100):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.now - age_s))
        return f"{stamp} [info] {'Enabling' if on else 'Disabling'} remote control for session {local}\n"

    def make_other_chats(self):
        """Every kind of chat that must not be listed, plus the ones that must."""
        self.skip = {}
        for number, name, extra in ((11, "no-remote", {"remoteControlUserEnabled": False}),
                                    (12, "archived", {"remoteControlUserEnabled": True, "isArchived": True}),
                                    (13, "routine", {"remoteControlUserEnabled": True, "scheduledTaskId": "t1"}),
                                    (14, "denied", {"remoteControlUserEnabled": True}),
                                    (15, "re-enabled", {"remoteControlUserEnabled": True}),
                                    (16, "no-flag-at-all", {})):
            local, session = chat_ids(number)
            self.chat(local, session, title=name, **extra)
            self.skip[name] = (local, session)
        core.controls_update(deny_add="claude:" + self.skip["denied"][1])
        # Desktop's own "Enabling remote control" line after the load: its Remote Control came back by itself.
        self.add_log(self.load_line(age_s=60), self.rc_line(self.skip["re-enabled"][0], age_s=30))
        # A chat running since the load without an Enabling line was warmed by Desktop without Remote Control
        # (live 2026-10-19): it still gets the message.
        self.warm_local, self.warm_session = chat_ids(19)
        self.chat(self.warm_local, self.warm_session, remoteControlUserEnabled=True, title="warmed")
        self.live.add(self.warm_session)
        # The other account's folder: never acted on, even with the same flag.
        other = self.base / OTHER_ACCOUNT / ORG
        self.other_local, self.other_session = chat_ids(18)
        self.chat(self.other_local, self.other_session, folder=other, remoteControlUserEnabled=True)

    def entries(self):
        return next(iter(self.loads().values()))["reconnects"]

    def test_one_entry_per_eligible_chat_per_load_and_nothing_else(self):
        self.make_other_chats()
        self.assertEqual(self.h.follow_log(), 1)
        self.advance(helper.LOAD_READY_MIN_S + 1)
        report = self.h.process_loads()
        _load_id, load = self.only_load()
        self.assertEqual(sorted(load["reconnects"]), sorted([CHAT_A_LOCAL, CHAT_B_LOCAL, self.warm_local]))
        self.assertEqual(report["queued"], 3)
        self.assertEqual(load["phase"], "enqueued")
        for _ in range(3):  # a later tick, and a restarted helper, list nothing more for this load
            self.assertEqual(self.h.process_loads()["queued"], 0)
        self.assertEqual(helper.Helper(clock=lambda: self.now).process_loads()["queued"], 0)
        again = core.relink_enqueue(self.only_load()[0], CHAT_A_LOCAL, CHAT_A)
        self.assertEqual(again, {"queued": False, "why": "already-handled-for-this-load"})
        self.assertNotIn(CLAIMANT_LOCAL, load["reconnects"])  # no chat names itself: the claimant is excluded at claim

    def test_a_new_load_gets_its_own_list(self):
        self.new_load_ready()
        self.h.process_loads()
        self.add_log(self.load_line(age_s=1))
        self.assertEqual(self.h.follow_log(), 1)
        self.advance(helper.LOAD_READY_MIN_S + 1)
        self.assertEqual(self.h.process_loads()["queued"], 2)

    def test_the_flag_file_exists_only_while_entries_wait(self):
        self.new_load_ready()
        self.assertFalse(core.relink_flag_path().exists())
        self.h.process_loads()
        self.assertTrue(core.relink_flag_path().exists())
        core.relink_claim(CLAIMANT)
        self.assertFalse(core.relink_flag_path().exists())

    def test_the_claim_hands_the_fixed_text_to_one_claimant_and_never_a_letter(self):
        self.new_load_ready()
        self.h.process_loads()
        journal = self.journal_bytes()
        claim = core.relink_claim(CLAIMANT)
        self.assertEqual(sorted(job["to"] for job in claim["jobs"]), sorted([CHAT_A_LOCAL, CHAT_B_LOCAL]))
        for job in claim["jobs"]:
            self.assertEqual(job["text"], core.RECONNECT_TEXT)
        self.assertEqual(core.relink_claim(CLAIMANT)["jobs"], [])  # a second claimant gets nothing
        self.assertEqual(self.journal_bytes(), journal)
        self.assertEqual(core.letters_load(), {})

    def test_a_claim_is_reported_once_and_only_by_the_claimer(self):
        self.new_load_ready()
        self.h.process_loads()
        job = core.relink_claim(CLAIMANT)["jobs"][0]
        self.assertEqual(core.relink_done(CLAIMANT, job["to"], "0" * 12, True), {"ok": False, "reason": "no-matching-claim"})
        stranger = "dddddddd-2222-4333-8444-555555555555"
        self.assertEqual(core.relink_done(stranger, job["to"], job["attempt_id"], True)["reason"], "no-matching-claim")
        self.assertEqual(core.relink_done(CLAIMANT, job["to"], job["attempt_id"], True)["state"], "done")
        self.assertEqual(core.relink_done(CLAIMANT, job["to"], job["attempt_id"], False)["reason"], "no-matching-claim")
        self.assertEqual(self.entries()[job["to"]]["state"], "done")

    def test_a_failed_send_is_recorded_and_never_retried(self):
        self.new_load_ready()
        self.h.process_loads()
        job = core.relink_claim(CLAIMANT)["jobs"][0]
        self.assertEqual(core.relink_done(CLAIMANT, job["to"], job["attempt_id"], False, "permission-rule-missing")["state"],
                         "failed")
        self.assertEqual(core.relink_claim(CLAIMANT)["jobs"], [])
        self.h.health()
        notice = [row for row in core.helper_notices_active() if row["kind"] == "reconnect-failed"]
        self.assertEqual(len(notice), 1)
        self.assertIn("permission rule", notice[0]["message"])
        self.assertIn("Not retried", notice[0]["message"])

    def test_a_chat_never_relinks_itself(self):
        self.new_load_ready()
        self.h.process_loads()
        self.live |= {CHAT_A}
        self.chat(CHAT_A_LOCAL, CHAT_A, remoteControlUserEnabled=True, title="Chat A")
        jobs = core.relink_claim(CHAT_A)["jobs"]  # chat A itself claims: it gets chat B's, never its own
        self.assertEqual([job["to"] for job in jobs], [CHAT_B_LOCAL])
        self.assertEqual(self.entries()[CHAT_A_LOCAL]["state"], "queued")

    def test_a_chat_that_is_not_a_desktop_chat_claims_nothing(self):
        self.new_load_ready()
        self.h.process_loads()
        self.assertEqual(core.relink_claim("dddddddd-2222-4333-8444-555555555555")["jobs"], [])
        self.assertEqual({e["state"] for e in self.entries().values()}, {"queued"})

    def test_an_owner_off_chat_is_never_listed_or_claimed(self):
        self.new_load_ready()
        local, session = chat_ids(22)
        self.chat(local, session, remoteControlUserEnabled=False, title="off")
        self.assertEqual(core.relink_enqueue(self.only_load()[0], local, session)["why"], "owner-off")
        self.h.process_loads()
        self.chat(CHAT_A_LOCAL, CHAT_A, remoteControlUserEnabled=False, title="Chat A")  # turned off after listing
        jobs = core.relink_claim(CLAIMANT)["jobs"]
        self.assertEqual([job["to"] for job in jobs], [CHAT_B_LOCAL])
        self.assertEqual(self.entries()[CHAT_A_LOCAL]["state"], "skipped")

    def test_halt_stops_queued_entries_and_nothing_new_is_listed(self):
        self.new_load_ready()
        self.h.process_loads()
        core.controls_update(halt=True)
        self.assertEqual(core.relink_claim(CLAIMANT)["jobs"], [])
        self.h.process_loads()
        self.assertEqual({e["state"] for e in self.entries().values()}, {"refused"})
        self.assertFalse(core.relink_flag_path().exists())
        core.controls_update(halt=False)
        self.assertEqual(core.relink_claim(CLAIMANT)["jobs"], [])
        self.assertEqual(self.h.process_loads()["queued"], 0)

    def test_halt_before_the_list_is_made_lists_nothing(self):
        self.new_load_ready()
        core.controls_update(halt=True)
        self.assertEqual(self.h.process_loads()["queued"], 0)
        self.assertEqual(core.relink_enqueue(self.only_load()[0], CHAT_A_LOCAL, CHAT_A)["why"], "halted")
        # slice 3B.19: messaging is for letters only, so it no longer refuses a relink entry (was "messaging-off")
        core.controls_update(halt=False, messaging="off")
        self.assertEqual(core.relink_enqueue(self.only_load()[0], CHAT_A_LOCAL, CHAT_A), {"queued": True})

    def test_a_chat_denied_after_listing_is_refused_at_claim(self):
        self.new_load_ready()
        self.h.process_loads()
        core.controls_update(deny_add="claude:" + CHAT_A)
        jobs = core.relink_claim(CLAIMANT)["jobs"]
        self.assertEqual([job["to"] for job in jobs], [CHAT_B_LOCAL])
        self.assertEqual(self.entries()[CHAT_A_LOCAL]["state"], "refused")

    def age_entries(self, seconds):
        def age(data):
            for load in data["loads"].values():
                for entry in load["reconnects"].values():
                    entry["queued_ns"] -= int(seconds * 1e9)
                    if entry.get("claimed_ns"):
                        entry["claimed_ns"] -= int(seconds * 1e9)
        core.helper_loads_update(age)

    def test_an_old_unclaimed_entry_expires_and_is_never_retried(self):
        self.new_load_ready()
        self.h.process_loads()
        self.age_entries(core.RELINK_TTL_S + 5)
        self.assertEqual(core.relink_claim(CLAIMANT)["jobs"], [])
        self.assertEqual({e["state"] for e in self.entries().values()}, {"expired"})
        self.h.health()
        notice = [row for row in core.helper_notices_active() if row["kind"] == "reconnect-failed"]
        self.assertEqual(len(notice), 1)
        self.assertIn("Not retried", notice[0]["message"])

    def test_a_claim_nobody_reports_on_fails_at_the_sweep(self):
        self.new_load_ready()
        self.h.process_loads()
        core.relink_claim(CLAIMANT)
        self.age_entries(core.RELINK_CLAIM_STALE_S + 5)
        self.h.process_loads()
        self.assertEqual({(e["state"], e["reason"]) for e in self.entries().values()}, {("failed", "claim-never-reported")})

    def test_a_reconnect_failed_notice_clears_when_a_newer_load_supersedes_its_load(self):
        self.new_load_ready()
        self.h.process_loads()
        self.age_entries(core.RELINK_TTL_S + 5)
        core.relink_claim(CLAIMANT)
        self.h.health()
        self.assertEqual(len([k for k in core.helper_notices_load()["notices"] if k.startswith("reconnect-failed:")]), 1)
        self.advance(5)
        self.add_log(self.load_line(age_s=1))
        self.assertEqual(self.h.follow_log(), 1)
        self.h.relink_notice()
        self.assertEqual([k for k in core.helper_notices_load()["notices"] if k.startswith("reconnect-failed:")], [])

    def test_an_orphan_reconnect_failed_notice_whose_load_left_the_ledger_is_cleared(self):
        core.helper_notice_set("reconnect-failed:gone-load", "reconnect-failed", "old")
        core.helper_notice_set("other", "other", "unrelated")
        self.h.relink_notice()
        self.assertEqual(sorted(core.helper_notices_load()["notices"]), ["other"])

    def test_the_retired_relay_claim_command_hands_out_nothing(self):
        for name in ("claude-relay-claim", "claude-relay-done", "claude-relay-ticket"):
            out = io.StringIO()
            with mock.patch.object(cli, "read_stdin_json", return_value={}), contextlib.redirect_stdout(out):
                cli.main([name])
            row = json.loads(out.getvalue().strip().splitlines()[-1])
            self.assertEqual((row["ok"], row["job"], row["ticket"], row["reason"]), (True, None, None, "relay-retired"))


class RelinkKickTests(HelperBase):
    """No running chat claimed the relink list (every chat stopped after the restart, live 2026-10-20): the helper
    opens one listed chat, whose plugin claims the rest."""

    def setUp(self):
        super().setUp()
        self.live = set()  # after a restart nothing runs
        self.chat(CHAT_A_LOCAL, CHAT_A, remoteControlUserEnabled=True, title="Chat A", lastActivityAt=1000)
        self.chat(CHAT_B_LOCAL, CHAT_B, remoteControlUserEnabled=True, title="Chat B", lastActivityAt=2000)
        self.new_load_ready()
        self.h.process_loads()
        self.shows.clear()

    def kick(self):
        return core.helper_loads_load()["loads"][self.only_load()[0]].get("kick") or {}

    def test_an_unclaimed_list_opens_the_most_recently_active_chat_once(self):
        self.advance(helper.RELINK_KICK_AFTER_S - 5)
        self.h.process_loads()
        self.assertEqual(self.shows, [])  # a running chat may still claim it
        self.advance(10)
        report = self.h.process_loads()
        self.assertEqual(self.shows, [CHAT_B])
        self.assertEqual((report["kick"]["to"], report["kick"]["attempt"]), (CHAT_B_LOCAL, 1))
        self.assertEqual(self.kick()["attempts"], 1)
        self.h.process_loads()
        self.assertEqual(self.shows, [CHAT_B])  # not again before the retry time
        self.assertEqual(core.letters_load(), {})  # an open is never a letter

    def test_the_opened_chat_claims_the_rest_and_another_claims_its_entry(self):
        self.advance(helper.RELINK_KICK_AFTER_S + 1)
        self.h.process_loads()
        self.live.add(CHAT_B)  # the opened chat's plugin is running now
        jobs = core.relink_claim(CHAT_B)["jobs"]
        self.assertEqual([job["to"] for job in jobs], [CHAT_A_LOCAL])
        self.live.add(CHAT_A)  # the reconnect text started chat A: its plugin takes chat B's entry
        self.assertEqual([job["to"] for job in core.relink_claim(CHAT_A)["jobs"]], [CHAT_B_LOCAL])
        self.advance(helper.RELINK_KICK_RETRY_S + 1)
        self.h.process_loads()
        self.assertEqual(self.shows, [CHAT_B])  # a claimed list is never opened again

    def test_a_claim_before_the_deadline_means_no_open(self):
        self.live.add(CLAIMANT)
        core.relink_claim(CLAIMANT)
        self.advance(helper.RELINK_KICK_AFTER_S + 1)
        self.h.process_loads()
        self.assertEqual(self.shows, [])

    def test_retries_try_the_other_chat_and_stop_at_the_limit(self):
        self.advance(helper.RELINK_KICK_AFTER_S + 1)
        self.h.process_loads()
        for _ in range(helper.RELINK_KICK_MAX + 2):
            self.advance(helper.RELINK_KICK_RETRY_S + 1)
            self.h.process_loads()
        self.assertEqual(self.shows[:2], [CHAT_B, CHAT_A])  # untried first
        self.assertEqual(len(self.shows), helper.RELINK_KICK_MAX)

    def test_config_off_halt_and_budget_open_nothing(self):
        (core.helper_dir() / "config.json").write_text(json.dumps({"relink_kick": False}))
        self.advance(helper.RELINK_KICK_AFTER_S + 1)
        self.h.process_loads()
        self.assertEqual(self.shows, [])
        (core.helper_dir() / "config.json").write_text("{}")
        with mock.patch.object(core, "wake_budget_claim",
                               return_value={"allowed": False, "reason": "wake-budget-total", "event": None}):
            self.assertEqual(self.h.process_loads()["kick"]["error"], "wake-budget-total")
        self.assertEqual((self.shows, self.kick()), ([], {}))  # a refused budget uses no attempt
        core.controls_update(halt=True)
        self.h.process_loads()
        self.assertEqual(self.shows, [])


class RelinkGuardTests(HelperBase):
    """The plugin's guard decides every call of the two Desktop tools Crosstalk's allow rules cover."""

    SET_RC = core.SET_REMOTE_CONTROL_TOOL
    SEND = core.SEND_MESSAGE_TOOL

    def setUp(self):
        super().setUp()
        self.add_log(self.load_line())  # Desktop's active account comes from main.log

    def claimed_chat(self):
        self.new_load_ready()
        self.h.process_loads()
        core.relink_claim(CLAIMANT)

    def test_set_remote_control_only_for_self_and_only_on(self):
        guard = core.relink_guard
        self.assertTrue(guard(CHAT_A, self.SET_RC, {"session_id": "self", "enabled": True})["allow"])
        self.assertFalse(guard(CHAT_A, self.SET_RC, {"session_id": CHAT_B, "enabled": True})["allow"])
        self.assertFalse(guard(CHAT_A, self.SET_RC, {"session_id": "self", "enabled": False})["allow"])
        self.assertFalse(guard(CHAT_A, self.SET_RC, {"session_id": "self"})["allow"])
        self.assertFalse(guard(CHAT_A, self.SET_RC, {"session_id": "self", "enabled": True, "extra": 1})["allow"])

    def test_an_owner_off_chat_may_not_switch_itself_on(self):
        self.chat(CHAT_A_LOCAL, CHAT_A, remoteControlUserEnabled=False, title="Chat A")
        self.assertFalse(core.relink_guard(CHAT_A, self.SET_RC, {"session_id": "self", "enabled": True})["allow"])

    def test_send_message_only_the_fixed_text_to_a_chat_on_the_claimed_list(self):
        self.claimed_chat()
        send = {"session_id": CHAT_A_LOCAL, "message": core.RECONNECT_TEXT}
        self.assertTrue(core.relink_guard(CLAIMANT, self.SEND, send)["allow"])
        self.assertFalse(core.relink_guard(CLAIMANT, self.SEND, dict(send, message="hello"))["allow"])
        self.assertFalse(core.relink_guard(CLAIMANT, self.SEND, dict(send, session_id=CODEX))["allow"])
        self.assertFalse(core.relink_guard(CLAIMANT, self.SEND, dict(send, extra=1))["allow"])
        self.assertFalse(core.relink_guard(CHAT_B, self.SEND, send)["allow"])  # not the claimant

    def test_send_message_is_denied_before_a_claim_and_after_the_entry_is_done(self):
        self.new_load_ready()
        self.h.process_loads()
        send = {"session_id": CHAT_A_LOCAL, "message": core.RECONNECT_TEXT}
        self.assertFalse(core.relink_guard(CLAIMANT, self.SEND, send)["allow"])  # listed, not claimed
        job = next(j for j in core.relink_claim(CLAIMANT)["jobs"] if j["to"] == CHAT_A_LOCAL)
        core.relink_done(CLAIMANT, job["to"], job["attempt_id"], True)
        self.assertFalse(core.relink_guard(CLAIMANT, self.SEND, send)["allow"])  # one send per entry

    def test_any_other_tool_is_not_the_guards_business(self):
        self.assertTrue(core.relink_guard(CHAT_A, "mcp__other__tool", {})["allow"])

    def test_the_self_relink_ask_needs_a_recent_letter_wake_once(self):
        self.live.add(CHAT_A)
        self.assertEqual(core.self_relink_ask(CHAT_A)["reason"], "not-letter-woken")
        self.h.write_state(letter_wakes={"CTP0-aaaaaaaaaaaa": {"session": CHAT_A, "ns": int(self.now * 1e9), "attempts": 1}})
        first = core.self_relink_ask(CHAT_A)
        self.assertTrue(first["relink"])
        again = core.self_relink_ask(CHAT_A)
        self.assertEqual((again["relink"], again["reason"]), (False, "already-handled-for-this-wake"))
        self.assertTrue(core.self_relink_done(CHAT_A, first["attempt_id"], "denied", "permission-ask")["ok"])
        self.assertFalse(core.self_relink_done(CHAT_A, "0" * 12, "on")["ok"])

    def test_the_self_relink_ask_refuses_an_owner_off_chat_a_stale_wake_and_a_halt(self):
        self.live.add(CHAT_A)
        wake = {"CTP0-aaaaaaaaaaaa": {"session": CHAT_A, "ns": int(self.now * 1e9), "attempts": 1}}
        self.h.write_state(letter_wakes=wake)
        self.chat(CHAT_A_LOCAL, CHAT_A, remoteControlUserEnabled=False, title="Chat A")
        self.assertEqual(core.self_relink_ask(CHAT_A)["reason"], "owner-off")
        self.chat(CHAT_A_LOCAL, CHAT_A, remoteControlUserEnabled=True, title="Chat A")
        self.h.write_state(letter_wakes={"CTP0-aaaaaaaaaaaa": {"session": CHAT_A,
                                                              "ns": int((self.now - core.RELINK_SELF_WINDOW_S - 5) * 1e9)}})
        self.assertEqual(core.self_relink_ask(CHAT_A)["reason"], "not-letter-woken")
        self.h.write_state(letter_wakes=wake)
        core.controls_update(halt=True)
        self.assertFalse(core.self_relink_ask(CHAT_A)["relink"])


class ReconnectListFromLogTests(HelperBase):
    """The reconnect list comes from Desktop's main.log (remote ON at the end of the account's previous run), plus the
    stored flag and the config list, minus a stored false (live 2026-10-13)."""

    def setUp(self):
        super().setUp()
        self.c_local, self.c = chat_ids(51)  # blank flag: only the log says it was on
        self.d_local, self.d = chat_ids(52)  # turned off again before the run ended
        self.e_local, self.e = chat_ids(53)  # stored false, but the log says on
        self.f_local, self.f = chat_ids(54)  # blank, listed in always_remote
        for local, session, extra in ((self.c_local, self.c, {}), (self.d_local, self.d, {}),
                                      (self.e_local, self.e, {"remoteControlUserEnabled": False}),
                                      (self.f_local, self.f, {})):
            self.chat(local, session, title=local[-4:], **extra)
        self.chat(CHAT_B_LOCAL, CHAT_B, remoteControlUserEnabled=False, title="Chat B")  # stored false, not in log

    def rc(self, local, on=True, age_s=100):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.now - age_s))
        return f"{stamp} [info] {'Enabling' if on else 'Disabling'} remote control for session {local}\n"

    def queued(self):
        self.h.follow_log()
        self.advance(helper.LOAD_READY_MIN_S + 1)
        self.h.process_loads()
        newest = max(self.loads().values(), key=lambda load: load["seen_ns"])
        return sorted(newest["reconnects"])

    def test_the_last_enable_or_disable_of_the_previous_run_decides(self):
        self.add_log(self.load_line(age_s=600), self.rc(self.c_local, age_s=500), self.rc(self.d_local, age_s=500),
                     self.rc(self.d_local, on=False, age_s=400), self.rc(self.e_local, age_s=300),
                     self.load_line(age_s=60), self.rc(RECIPIENT_LOCAL, age_s=30))  # this run's lines do not count
        # c: on in the log. d: disabled. e: stored false wins over the log. A: stored true. B: stored false.
        self.assertEqual(self.queued(), sorted([CHAT_A_LOCAL, self.c_local]))

    def test_a_chat_missing_from_the_account_folder_is_never_named(self):
        self.add_log(self.load_line(age_s=600), self.rc("local_99999999-2222-4333-8444-555555555555", age_s=500),
                     self.load_line(age_s=60))
        self.assertEqual(self.queued(), [CHAT_A_LOCAL])

    def test_an_account_switch_uses_that_accounts_own_previous_run(self):
        self.add_log(self.load_line(age_s=900), self.rc(self.c_local, age_s=800),
                     self.load_line(age_s=600, account=OTHER_ACCOUNT), self.rc(self.d_local, age_s=500),
                     self.load_line(age_s=60))  # back on the first account: its run is the first, not the middle one
        self.assertEqual(self.queued(), sorted([CHAT_A_LOCAL, self.c_local]))

    def test_no_earlier_run_of_this_account_uses_the_other_accounts_last_run(self):
        # Chat sync keeps both accounts' chat lists as copies under the same Desktop ids (live 2026-10-13).
        self.add_log(self.load_line(age_s=900, account=OTHER_ACCOUNT), self.rc(self.d_local, age_s=850),
                     self.load_line(age_s=600, account=OTHER_ACCOUNT), self.rc(self.c_local, age_s=500),
                     self.load_line(age_s=60))
        self.assertEqual(self.queued(), sorted([CHAT_A_LOCAL, self.c_local]))

    def test_no_earlier_run_at_all_falls_back_to_the_stored_flag(self):
        self.add_log(self.load_line(age_s=60))
        self.assertEqual(self.queued(), [CHAT_A_LOCAL])

    def test_always_remote_is_added_and_a_stored_false_still_wins(self):
        core.helper_dir().mkdir(parents=True, exist_ok=True)
        (core.helper_dir() / "config.json").write_text(json.dumps(
            {"always_remote": [self.f_local, self.e_local, 7]}))
        self.add_log(self.load_line(age_s=600), self.rc(self.e_local, age_s=500), self.load_line(age_s=60))
        self.assertEqual(self.queued(), sorted([CHAT_A_LOCAL, self.f_local]))

    def test_a_chat_warmed_after_the_load_without_an_enabling_line_is_queued(self):
        self.live.add(CHAT_A)  # running since the load, but Desktop never re-enabled it
        self.add_log(self.load_line(age_s=60))
        self.assertEqual(self.queued(), [CHAT_A_LOCAL])

    def test_an_enabling_line_after_the_load_skips_the_chat(self):
        self.add_log(self.load_line(age_s=60), self.rc(CHAT_A_LOCAL, age_s=30))
        self.assertEqual(self.queued(), [])

    def test_an_enabling_line_only_before_the_load_still_queues_the_chat(self):
        self.add_log(self.load_line(age_s=600), self.rc(CHAT_A_LOCAL, age_s=500), self.load_line(age_s=60))
        self.assertEqual(self.queued(), [CHAT_A_LOCAL])

    def test_an_unreadable_main_log_skips_nothing(self):
        self.add_log(self.load_line(age_s=60))
        self.h.follow_log()
        self.log.unlink()
        self.assertEqual(sorted(c["desktop_id"] for c in self.h.candidates(
            {"log_s": self.now}, {"folder": str(self.folder), "id": ACCOUNT}, next(iter(self.loads())))),
            [CHAT_A_LOCAL])

    def test_archived_denied_and_re_enabled_chats_stay_excluded(self):
        arch_local, arch = chat_ids(61)
        deny_local, deny = chat_ids(62)
        beating_local, beating = chat_ids(63)  # stays excluded only because the log says Desktop re-enabled it
        self.chat(arch_local, arch, isArchived=True)
        self.chat(deny_local, deny)
        self.chat(beating_local, beating)
        core.controls_update(deny_add="claude:" + deny)
        self.add_log(self.load_line(age_s=600), *(self.rc(x, age_s=500) for x in
                     (arch_local, deny_local, beating_local, self.c_local)), self.load_line(age_s=60),
                     self.rc(beating_local, age_s=30))
        self.assertEqual(self.queued(), sorted([CHAT_A_LOCAL, self.c_local]))

    def test_an_earlier_run_in_the_rotated_log_counts(self):
        rotated = self.log.parent / "main1.log"
        rotated.write_text(self.load_line(age_s=900) + self.rc(self.c_local, age_s=800), encoding="utf-8")
        self.add_log(self.load_line(age_s=60))
        self.assertEqual(self.queued(), sorted([CHAT_A_LOCAL, self.c_local]))

    def test_reads_are_bounded_to_the_log_tail(self):
        self.add_log(self.load_line(age_s=900), self.rc(self.c_local, age_s=800), "x" * 3000 + "\n",
                     self.load_line(age_s=60))
        with mock.patch.object(core, "LOG_TAIL_BYTES", 400):
            runs = core.desktop_remote_runs()
            self.assertEqual(len(runs), 1)  # the earlier run is outside the tail: unknown stays unknown
            self.assertEqual(core.remote_on_before_load(runs[0]["load_id"], ACCOUNT), set())


class LetterWakeTests(HelperBase):
    """A letter waits for an asleep Claude chat: the helper wakes that chat through the focus gate, with backoff
    (owner 2026-10-13). It never sends or resends the letter; the woken chat takes it from its mail slot."""

    def setUp(self):
        super().setUp()
        self.sleeper_local, self.sleeper = chat_ids(31)
        self.other_local, self.other = chat_ids(32)
        self.chat(self.sleeper_local, self.sleeper, title="Sleeper")
        self.chat(self.other_local, self.other, title="Other")
        self.add_log(self.load_line())  # Desktop's active account comes from main.log
        self.letters = 0

    def letter(self, session=None, state="blocked:not-running", age_s=60):
        self.letters += 1
        letter_id = f"CTP0-{self.letters:012x}"
        core.journal_append(core._letter_row(
            letter_id, state, to={"app": "claude", "id": session or self.sleeper}, sent=False,
            created_ns=int((self.now - age_s) * 1e9), message=core.frame_message(letter_id, "codex", "sender", "Hi")))
        return letter_id

    def wake_rows(self):
        path = core.focus_log_path()
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_letters_to_one_chat_share_a_wake_and_nothing_is_sent(self):
        first, second = self.letter(), self.letter()
        before = self.journal_bytes()
        report = self.h.wake_for_waiting_letters()
        self.assertEqual(self.shows, [self.sleeper])
        self.assertEqual(report["woken"], [{"session": self.sleeper, "letters": 2, "launched": True, "attempts": 1}])
        self.assertEqual(self.journal_bytes(), before)  # no letter was sent, resent or re-stated
        self.assertEqual({row["state"] for row in core.letters_load().values()}, {"blocked:not-running"})
        for _ in range(3):  # inside the first backoff nothing more happens
            self.advance(15)
            self.assertEqual(self.h.wake_for_waiting_letters()["woken"], [])
        self.assertEqual(self.shows, [self.sleeper])
        self.assertEqual(set(self.h.letter_wakes()), {first, second})
        third = self.letter()  # a later letter is a new letter: it gets its own first attempt
        self.h.wake_for_waiting_letters()
        self.assertEqual(self.shows, [self.sleeper, self.sleeper])
        self.assertIn(third, self.h.letter_wakes())

    def test_the_attempt_is_remembered_durably_across_a_helper_restart(self):
        self.letter()
        self.h.wake_for_waiting_letters()
        again = helper.Helper(clock=lambda: self.now)
        self.assertEqual(again.wake_for_waiting_letters()["woken"], [])
        self.assertEqual(self.shows, [self.sleeper])

    def test_a_wake_is_retried_with_backoff_and_gives_up_after_the_last_attempt(self):
        letter = self.letter()
        waits = list(core.WAKE_BACKOFF_S)
        self.assertEqual(core.WAKE_MAX_ATTEMPTS, len(waits) + 1)
        for number in range(1, core.WAKE_MAX_ATTEMPTS + 1):
            report = self.h.wake_for_waiting_letters()
            self.assertEqual(report["woken"][0]["attempts"], number)
            self.assertEqual(len(self.shows), number)
            if number <= len(waits):
                self.advance(waits[number - 1] - 1)  # one second short: not due
                self.assertEqual(self.h.wake_for_waiting_letters()["woken"], [])
                self.advance(2)
        for _ in range(3):  # attempts used up: the letter just waits in the mail slot
            self.advance(core.WAKE_BACKOFF_S[-1] + 5)
            self.assertEqual(self.h.wake_for_waiting_letters()["woken"], [])
        self.assertEqual(len(self.shows), core.WAKE_MAX_ATTEMPTS)
        self.assertEqual(self.h.letter_wakes()[letter]["attempts"], core.WAKE_MAX_ATTEMPTS)
        self.assertEqual(core.letters_load()[letter]["state"], "blocked:not-running")  # never sent
        self.h.health()
        self.assertIn("wake-gave-up", {row["kind"] for row in core.helper_notices_active()})

    def test_a_failed_launch_counts_as_an_attempt_and_is_retried_later(self):
        self.show_result = {"launched": False, "method": None, "error": "OSError"}
        self.letter()
        self.assertFalse(self.h.wake_for_waiting_letters()["woken"][0]["launched"])
        self.assertEqual(self.h.wake_for_waiting_letters()["woken"], [])  # backoff
        self.advance(core.WAKE_BACKOFF_S[0] + 1)
        self.show_result = {"launched": True, "method": "test", "verified": False}
        self.assertEqual(self.h.wake_for_waiting_letters()["woken"][0]["attempts"], 2)

    def lock_busy(self, busy):
        real = core.locked

        def locked(name, *args, **kwargs):
            if name == core.FOCUS_LOCK_NAME and busy[0]:
                raise TimeoutError(name)
            return real(name, *args, **kwargs)
        patcher = mock.patch.object(core, "locked", side_effect=locked)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_busy_focus_lock_is_recorded_before_the_attempt_and_retried_after_the_backoff(self):
        busy = [True]
        self.lock_busy(busy)
        letter = self.letter()
        first = self.h.wake_for_waiting_letters()
        self.assertEqual(first["woken"], [{"session": self.sleeper, "letters": 1, "launched": False, "attempts": 1}])
        self.assertEqual(self.shows, [])
        self.assertEqual(self.h.letter_wakes()[letter]["session"], self.sleeper)  # recorded before the attempt
        busy[0] = False
        self.advance(core.WAKE_BACKOFF_S[0] + 1)
        second = self.h.wake_for_waiting_letters()
        self.assertEqual(self.shows, [self.sleeper])
        self.assertTrue(second["woken"][0]["launched"])

    def test_an_uncertain_mail_slot_letter_is_woken_too_but_not_one_on_another_route(self):
        mail_slot = self.letter(state="uncertain")
        other = self.letter(self.other, state="uncertain")
        core.journal_append(core._letter_row(mail_slot, "uncertain", route="mail-slot"))
        core.journal_append(core._letter_row(other, "uncertain", route="desktop-send"))
        self.h.wake_for_waiting_letters()
        self.assertEqual(self.shows, [self.sleeper])
        self.h.wake_for_waiting_letters()
        self.assertEqual(self.shows, [self.sleeper])  # the other letter is not a mail-slot letter: no wake

    def test_a_letter_still_blocked_past_24_hours_ends_expired_unsent(self):
        old = self.letter(age_s=core.MAIL_SLOT_TTL_S + 60)
        young = self.letter()
        core.reconcile(resend=False)
        rows = core.letters_load()
        self.assertEqual(rows[old]["state"], "expired-unsent")
        self.assertFalse(rows[old].get("sent"))
        self.assertEqual(rows[young]["state"], "blocked:not-running")
        self.assertIn("expired-unsent", core.TERMINAL_STATES)

    def test_the_send_result_says_whether_the_helper_runs(self):
        core.directory_register("claude", self.sleeper, {"account": {"id": ACCOUNT}})
        core.directory_register("codex", CODEX, {})
        with mock.patch.object(core, "helper_alive", return_value=False):
            result = core.send_letter("codex", CODEX, "claude:" + self.sleeper, "Hello")
        self.assertEqual(result["state"], "blocked:not-running")
        self.assertIs(result["helper_running"], False)
        self.assertTrue(result["wakeable"])
        self.assertIn("NOT running", result["guidance"])
        with mock.patch.object(core, "helper_alive", return_value=True):
            self.assertIs(core.not_running_detail({"app": "claude", "id": self.sleeper})["helper_running"], True)

    def test_a_young_send_in_flight_is_not_handed_out_by_the_mail_slot_whatever_its_route(self):
        for number, route in enumerate(("desktop-send", "native", "mail-slot", "headless")):
            session = chat_ids(70 + number)[1]
            letter_id = self.letter(session, state="sending")
            core.journal_append(core._letter_row(letter_id, "sending", route=route, sending_ns=time.time_ns(),
                                                 attempt_id="a" * 12))
            with mock.patch.object(core, "claude_arrivals", return_value=(0, "read")):
                answer = core.mail_slot_pull(session)
            self.assertEqual(answer["letters"], [], route)
            self.assertEqual(answer["skipped"][0]["why"], "send-in-flight", route)

    def test_an_old_send_is_handed_out_once_zero_arrivals_are_proven(self):
        session = chat_ids(80)[1]
        letter_id = self.letter(session, state="sending")
        core.journal_append(core._letter_row(letter_id, "sending", route="desktop-send", attempt_id="a" * 12,
                                             sending_ns=time.time_ns() - int((core.MAIL_SLOT_IN_FLIGHT_S + 30) * 1e9)))
        with mock.patch.object(core, "claude_arrivals", return_value=(0, "read")):
            answer = core.mail_slot_pull(session)
        self.assertEqual([row["letter_id"] for row in answer["letters"]], [letter_id])

    def test_in_flight_relay_letters_move_back_to_blocked_once(self):
        waking = self.letter(state="waking")
        relay_send = self.letter(self.other, state="sending")
        core.journal_append(core._letter_row(relay_send, "sending", route="desktop-relay", sending_ns=time.time_ns()))
        plain = self.letter(chat_ids(81)[1], state="sending")
        core.journal_append(core._letter_row(plain, "sending", route="native", sending_ns=time.time_ns()))
        moved = core.relay_retire_inflight()
        self.assertEqual(sorted(moved), sorted([waking, relay_send]))
        rows = core.letters_load()
        self.assertEqual(rows[waking]["state"], "blocked:not-running")
        self.assertEqual(rows[relay_send]["state"], "blocked:not-running")
        self.assertFalse(rows[waking].get("sent"))
        self.assertEqual(rows[plain]["state"], "sending")
        self.assertEqual(core.relay_retire_inflight(), [])  # once

    def test_the_helper_is_the_only_waker_of_an_asleep_chat(self):
        self.letter()
        self.assertEqual(self.shows, [])
        self.h.wake_for_waiting_letters()
        self.assertEqual(self.shows, [self.sleeper])  # no relay chat or heartbeat is needed

    def test_each_chat_is_woken_one_per_tick(self):
        self.letter()
        self.letter(self.other)
        self.h.wake_for_waiting_letters()
        self.assertEqual(self.shows, [self.sleeper])
        self.h.wake_for_waiting_letters()
        self.assertEqual(self.shows, [self.sleeper, self.other])

    def test_halt_off_and_deny_wake_nothing(self):
        self.letter()
        core.controls_update(halt=True)
        self.h.wake_for_waiting_letters()
        core.controls_update(halt=False, messaging="off")
        self.h.wake_for_waiting_letters()
        core.controls_update(messaging="on", deny_add=f"claude:{self.sleeper}")
        self.h.wake_for_waiting_letters()
        self.assertEqual(self.shows, [])
        self.assertEqual(self.h.letter_wakes(), {})

    def test_only_a_valid_asleep_chat_on_the_active_account_with_a_waiting_letter_is_woken(self):
        archived_local, archived = chat_ids(33)
        self.chat(archived_local, archived, isArchived=True)
        routine_local, routine = chat_ids(34)
        self.chat(routine_local, routine, scheduledTaskId="task")
        elsewhere_local, elsewhere = chat_ids(35)
        self.chat(elsewhere_local, elsewhere, folder=self.base / OTHER_ACCOUNT / ORG)
        running_local, running = chat_ids(36)
        self.chat(running_local, running)
        self.live.add(running)
        missing = chat_ids(37)[1]
        for session in (archived, routine, elsewhere, running, missing):
            self.letter(session)
        self.letter(state="arrived")
        self.letter(state="accepted")
        self.letter(self.other, age_s=core.MAIL_SLOT_TTL_S + 60)  # too old for the mail slot
        self.assertEqual(self.h.wake_for_waiting_letters()["woken"], [])
        self.assertEqual(self.shows, [])

    def test_the_switch_in_config_turns_it_off(self):
        core.helper_dir().mkdir(parents=True, exist_ok=True)
        (core.helper_dir() / "config.json").write_text(json.dumps({"letter_wake": False}))
        self.letter()
        self.h.wake_for_waiting_letters()
        self.assertEqual(self.shows, [])

    def test_the_wake_goes_through_the_focus_gate_and_a_checkin_confirms_it(self):
        self.letter()

        def launch(exe, session):
            self.shows.append(session)
            core.directory_register("claude", session, {})  # the woken chat's plugin checks in
            return dict(self.show_result)
        with mock.patch.object(core, "launch_desktop_show", side_effect=launch):
            self.h.wake_for_waiting_letters()
        [row] = self.wake_rows()
        self.assertEqual((row["kind"], row["target"], row["confirmed"], row["launched"]),
                         ("letter-wake", f"claude:{self.sleeper}", True, True))

    def test_a_controls_refusal_at_the_gate_launches_nothing(self):
        self.letter()
        real = core.controls_check
        calls = []

        def refuse_in_the_gate(key):
            calls.append(key)
            return "halted" if len(calls) > 2 else real(key)  # helper's own check, per-chat check, then the gate
        with mock.patch.object(core, "controls_check", side_effect=refuse_in_the_gate):
            report = self.h.wake_for_waiting_letters()
        self.assertEqual(self.shows, [])
        self.assertFalse(report["woken"][0]["launched"])

    def test_the_tick_runs_it_and_reports(self):
        self.letter()
        summary = self.h.tick()
        self.assertEqual(summary["letter_wake"]["woken"][0]["session"], self.sleeper)
        self.assertEqual(self.shows, [self.sleeper])


class WakeBudgetTests(HelperBase):
    """The wake budget in controls (rework thought experiment 6): the helper counts each Claude letter wake when it
    decides to wake, over budget the letters wait unwoken (never sent, never lost, no attempt burned) and one notice
    says so; the budget frees as the window rolls."""

    def setUp(self):
        super().setUp()
        self.sleeper_local, self.sleeper = chat_ids(31)
        self.other_local, self.other = chat_ids(32)
        self.chat(self.sleeper_local, self.sleeper, title="Sleeper")
        self.chat(self.other_local, self.other, title="Other")
        self.add_log(self.load_line())
        self.letters = 0

    letter = LetterWakeTests.letter

    def arrive_for(self, session):
        """The woken chat takes its waiting letters from its mail slot."""
        for letter_id, row in core.letters_load().items():
            if (row.get("to") or {}).get("id") == session and row["state"] == "blocked:not-running":
                core.journal_append(core._letter_row(letter_id, "arrived", sent=True))

    def budget_notices(self):
        return [row for row in core.helper_notices_active() if row["kind"] == "wake-budget"]

    def test_a_chat_over_its_budget_is_not_woken_and_its_letter_uses_no_attempt(self):
        core.controls_update(wake_budget={"per_chat": 1})
        self.letter()
        self.assertEqual(len(self.h.wake_for_waiting_letters()["woken"]), 1)
        self.advance(120)  # past the batch window, inside the hour
        waiting = self.letter()
        before = self.journal_bytes()
        report = self.h.wake_for_waiting_letters()
        self.assertEqual((report["woken"], report["budget_waiting"]), ([], [self.sleeper]))
        self.assertEqual(self.shows, [self.sleeper])
        self.assertNotIn(waiting, self.h.letter_wakes())  # not tried, so nothing to back off from
        self.assertEqual(self.journal_bytes(), before)  # nothing sent, resent or re-stated
        self.assertEqual(core.letters_load()[waiting]["state"], "blocked:not-running")
        self.assertEqual(len(self.budget_notices()), 1)

    def test_the_notice_is_one_notice_however_often_it_is_refused(self):
        core.controls_update(wake_budget={"per_chat": 1})
        self.letter()
        self.h.wake_for_waiting_letters()
        self.advance(120)
        self.letter()
        for _ in range(8):
            self.h.wake_for_waiting_letters()
            self.advance(15)
        [notice] = self.budget_notices()
        self.assertIn("waiting unwoken", notice["message"])
        self.assertEqual(self.shows, [self.sleeper])

    def test_the_budget_frees_as_the_window_rolls_and_the_waiting_letter_is_then_woken(self):
        core.controls_update(wake_budget={"per_chat": 1})
        self.letter()
        self.h.wake_for_waiting_letters()
        self.advance(120)
        waiting = self.letter()
        self.h.wake_for_waiting_letters()
        self.assertEqual(len(self.shows), 1)
        self.advance(3600)
        self.assertEqual(self.h.wake_for_waiting_letters()["woken"][0]["letters"], 2)  # the first one is due again too
        self.assertEqual(self.shows, [self.sleeper, self.sleeper])
        self.assertIn(waiting, self.h.letter_wakes())
        self.h.wake_notice()
        self.assertEqual(self.budget_notices(), [])

    def test_a_chat_over_budget_does_not_hold_back_another_chat_in_the_same_tick(self):
        core.controls_update(wake_budget={"per_chat": 1})
        self.letter()
        self.h.wake_for_waiting_letters()
        self.advance(120)
        self.letter()
        self.letter(self.other)
        report = self.h.wake_for_waiting_letters()
        self.assertEqual([row["session"] for row in report["woken"]], [self.other])
        self.assertEqual(report["budget_waiting"], [self.sleeper])
        self.assertEqual(self.shows, [self.sleeper, self.other])

    def test_the_total_budget_stops_every_chat(self):
        core.controls_update(wake_budget={"per_chat": 5, "total": 1})
        self.letter()
        self.letter(self.other)
        self.assertEqual(len(self.h.wake_for_waiting_letters()["woken"]), 1)
        self.advance(120)
        report = self.h.wake_for_waiting_letters()
        self.assertEqual(report["woken"], [])
        self.assertEqual(self.shows, [self.sleeper])
        self.assertEqual(len(self.budget_notices()), 1)

    def test_letters_to_one_chat_inside_a_minute_share_one_wake_in_the_budget(self):
        core.controls_update(wake_budget={"per_chat": 1})
        self.letter()
        self.h.wake_for_waiting_letters()
        self.advance(30)
        second = self.letter()
        self.assertEqual(len(self.h.wake_for_waiting_letters()["woken"]), 1)  # batched with the wake 30 s ago
        self.assertIn(second, self.h.letter_wakes())
        self.assertEqual(self.budget_notices(), [])
        self.assertEqual(core.wake_budget_status(int(self.now * 1e9))["used_per_chat"], {f"claude:{self.sleeper}": 1})

    def test_a_chat_that_was_already_awake_gives_its_slot_back(self):
        core.controls_update(wake_budget={"per_chat": 1})
        self.letter()
        with mock.patch.object(self.h, "focus_open", return_value={"launched": True, "method": "already-awake"}):
            self.h.wake_for_waiting_letters()
        self.assertEqual(core.wake_budget_status(int(self.now * 1e9))["used_total"], 0)

    def test_halt_wakes_nothing_and_counts_nothing(self):
        self.letter()
        core.controls_update(halt=True)
        self.h.wake_for_waiting_letters()
        self.assertEqual(core.wake_budget_status(int(self.now * 1e9))["used_total"], 0)

    def test_two_asleep_chats_waking_each_other_stop_at_the_budget(self):
        """Thought experiment 6: A's letter wakes A, which answers B; B's wake answers A; and so on. Without a budget
        this never stops; with it each chat is woken six times, then the letters wait and a notice says so."""
        target = self.sleeper
        self.letter(target)
        for _ in range(35):  # 35 x 90 s stays inside the hour
            report = self.h.wake_for_waiting_letters()
            if report["woken"]:
                woken = report["woken"][0]["session"]
                self.arrive_for(woken)
                target = self.other if woken == self.sleeper else self.sleeper
                self.letter(target)  # the woken chat answers the other one, which is asleep too
            self.advance(90)
        self.assertEqual(len(self.shows), 12)
        self.assertEqual((self.shows.count(self.sleeper), self.shows.count(self.other)), (6, 6))
        self.assertEqual(len(self.budget_notices()), 1)
        waiting = [row for row in core.letters_load().values() if row["state"] == "blocked:not-running"]
        self.assertEqual(len(waiting), 1)  # the answer nobody could be woken for: it waits, unsent and not lost
        self.assertFalse(waiting[0].get("sent"))
        shows_at_stop = len(self.shows)
        for _ in range(5):
            self.assertEqual(self.h.wake_for_waiting_letters()["woken"], [])
            self.advance(90)
        self.assertEqual(len(self.shows), shows_at_stop)  # the loop is stopped, not slowed
        self.advance(3600)  # the window rolls: the budget frees and the waiting letter is woken once more
        self.assertEqual(len(self.h.wake_for_waiting_letters()["woken"]), 1)

    def test_the_total_budget_alone_also_ends_the_loop(self):
        core.controls_update(wake_budget={"per_chat": 50, "total": 5})
        target = self.sleeper
        self.letter(target)
        for _ in range(35):
            report = self.h.wake_for_waiting_letters()
            if report["woken"]:
                woken = report["woken"][0]["session"]
                self.arrive_for(woken)
                target = self.other if woken == self.sleeper else self.sleeper
                self.letter(target)
            self.advance(90)
        self.assertEqual(len(self.shows), 5)

    def test_a_codex_letter_held_back_by_the_budget_is_retried_when_the_chat_has_room(self):
        key = f"codex:{CODEX}"
        letter_id = "CTP0-00000000c0de"
        core.journal_append(core._letter_row(
            letter_id, "held", to={"app": "codex", "id": CODEX}, sent=True, created_ns=int((self.now - 60) * 1e9),
            message=core.frame_message(letter_id, "claude", "sender", "Hi"), codex_wake="skipped:wake-budget"))
        core.controls_update(wake_budget={"per_chat": 1})
        core.wake_budget_claim(key, "codex-link", int(self.now * 1e9))  # the chat's slot is used
        self.advance(120)
        spawned = []
        with mock.patch.object(core, "spawn_codex_wake", side_effect=lambda lid: spawned.append(lid) or 4242):
            self.assertEqual(self.h.retry_budget_codex_wakes()["retried"], [])  # still over budget: nothing started
            self.advance(3600)
            self.assertEqual(self.h.retry_budget_codex_wakes()["retried"], [letter_id])
            self.assertEqual(self.h.retry_budget_codex_wakes()["retried"], [])  # one waiter per letter
        self.assertEqual(spawned, [letter_id])
        self.assertEqual(core.letters_load()[letter_id]["state"], "held")  # a waiter only opens a link: never a send
        core.controls_update(halt=True)
        core.journal_append(core._letter_row(letter_id, "held", codex_wake="skipped:wake-budget"))
        with mock.patch.object(core, "spawn_codex_wake") as again:
            self.assertEqual(self.h.retry_budget_codex_wakes()["retried"], [])
        again.assert_not_called()


class ReconcileSafetyTests(HelperBase):
    def test_the_helper_reconcile_path_sends_wakes_and_opens_nothing(self):
        core.directory_register("claude", RECIPIENT, {"account": {"id": ACCOUNT}})
        core.directory_register("codex", CODEX, {})
        self.chat(RECIPIENT_LOCAL, RECIPIENT)
        result = core.send_letter("codex", CODEX, "claude:" + RECIPIENT, "Hello")  # asleep: blocked, waits in the mail slot
        self.assertEqual(result["state"], "blocked:not-running")
        before = self.journal_bytes()
        forbidden = {name: mock.patch.object(core, name, side_effect=AssertionError(name + " was called"))
                     for name in ("attempt_delivery", "start_codex_wake", "spawn_codex_wake", "open_codex_link",
                                  "deliver_claude", "send_letter")}
        for patcher in forbidden.values():
            patcher.start()
            self.addCleanup(patcher.stop)
        summary = self.h.tick()
        self.assertNotIn("reconcile_error", summary)
        self.assertIn("reconcile", summary)
        self.assertEqual(self.journal_bytes(), before)
        self.assertEqual(core.letters_load()[result["letter_id"]]["state"], "blocked:not-running")

    def test_the_helper_source_never_resends_or_sends(self):
        source = (KIT / "crosstalk_helper.py").read_text(encoding="utf-8")
        self.assertIn("reconcile(resend=False)", source)
        for text in ("resend=True", "core.reconcile()", "attempt_delivery(", "send_letter(", "deliver_claude(",
                     "open_codex_link(", "spawn_codex_wake(", "start_codex_wake(", "relay_done(", "SendMessage"):
            self.assertNotIn(text, source, text)


class NoticeTests(HelperBase):
    def kinds(self):
        return {row["kind"]: row for row in core.helper_notices_active() if row["kind"] != "helper-not-running"}

    def test_account_mismatch_and_unknown_set_and_clear(self):
        check = {"state": "mismatch", "desktop": ACCOUNT, "cli": OTHER_ACCOUNT, "message": "different accounts"}
        with mock.patch.object(core, "account_check", return_value=check):
            self.h.health()
        self.assertIn("account-mismatch", self.kinds())
        self.assertEqual(self.kinds()["account-mismatch"]["message"], "different accounts")
        with mock.patch.object(core, "account_check", return_value=dict(check, state="unknown", message="no id")):
            self.h.health()
            self.assertNotIn("account-mismatch", self.kinds())
            self.assertNotIn("account-unknown", self.kinds())  # a switch passes through unknown: no notice yet
            self.advance(helper.ACCOUNT_UNKNOWN_NOTICE_AFTER_S + 1)
            self.h.health()
        self.assertIn("account-unknown", self.kinds())
        with mock.patch.object(core, "account_check", return_value=dict(check, state="match")):
            self.h.health()
        self.assertNotIn("account-unknown", self.kinds())
        self.assertNotIn("account-mismatch", self.kinds())

    def test_a_mismatch_asks_once_per_pair_and_again_only_after_a_match(self):
        mismatch = {"state": "mismatch", "desktop": ACCOUNT, "cli": OTHER_ACCOUNT, "message": "different accounts"}
        for check, prompts in ((mismatch, 1), (mismatch, 1), (dict(mismatch, state="unknown"), 1), (mismatch, 1),
                               (dict(mismatch, cli=ACCOUNT, state="match"), 1), (mismatch, 2)):
            with mock.patch.object(core, "account_check", return_value=check):
                self.h.health()
            self.assertEqual(len(self.login_prompts), prompts, check)

    def test_no_login_question_when_halted_or_config_turns_it_off(self):
        mismatch = {"state": "mismatch", "desktop": ACCOUNT, "cli": OTHER_ACCOUNT, "message": "different accounts"}
        core.controls_update(halt=True)
        with mock.patch.object(core, "account_check", return_value=mismatch):
            self.h.health()
        core.controls_update(halt=False)
        core._write_json_atomic(core.helper_dir() / "config.json", {"login_prompt": False})
        with mock.patch.object(core, "account_check", return_value=mismatch):
            self.h.health()
        self.assertEqual(self.login_prompts, [])
        self.assertIn("account-mismatch", self.kinds())  # the notice still shows

    def test_messaging_off_does_not_stop_the_login_question(self):
        # slice 3B.19: messaging is for letters only (was: messaging off skipped the question)
        mismatch = {"state": "mismatch", "desktop": ACCOUNT, "cli": OTHER_ACCOUNT, "message": "different accounts"}
        core.controls_update(messaging="off")
        with mock.patch.object(core, "account_check", return_value=mismatch):
            self.h.health()
        self.assertEqual(len(self.login_prompts), 1)

    def test_a_brief_unknown_during_an_account_switch_raises_no_notice(self):
        unknown = {"state": "unknown", "message": "Account match unknown: could not read the Desktop account ID."}
        with mock.patch.object(core, "account_check", return_value=unknown):
            self.h.health()
        self.advance(30)
        with mock.patch.object(core, "account_check", return_value={"state": "match"}):
            self.h.health()
        self.advance(helper.ACCOUNT_UNKNOWN_NOTICE_AFTER_S)
        with mock.patch.object(core, "account_check", return_value=unknown):
            self.h.health()  # a new unknown starts its own wait
        self.assertNotIn("account-unknown", self.kinds())

    def test_a_missing_permission_rule_is_a_notice_that_clears_when_it_is_added(self):
        status = {"missing": [core.SEND_MESSAGE_TOOL], "present": [core.SET_REMOTE_CONTROL_TOOL]}
        with mock.patch.object(core, "account_check", return_value={"state": "match"}),                 mock.patch.object(install, "permission_rules_status", return_value=status),                 mock.patch.dict("sys.modules", {"crosstalk_p0_install": install}):
            self.h.health()
            self.assertIn(core.SEND_MESSAGE_TOOL, self.kinds()["permission-rules-missing"]["message"])
            status["missing"] = []
            self.h.health()
            self.assertNotIn("permission-rules-missing", self.kinds())

    def test_plugin_version_notice(self):
        with mock.patch.object(core, "account_check", return_value={"state": "match"}):
            self.h.plugin_stale = lambda: "Installed Crosstalk plugin is 0.0.8 but this checkout builds 0.0.9."
            self.h.health()
            self.assertIn("plugin-stale", self.kinds())
            self.h.plugin_stale = lambda: None
            self.h.health()
            self.assertNotIn("plugin-stale", self.kinds())

    def test_plugin_stale_compares_the_checkout_version_with_the_installed_one(self):
        plain = helper.Helper(clock=lambda: self.now)
        installed = self.root / "claude" / "plugins" / "installed_plugins.json"
        installed.parent.mkdir(parents=True, exist_ok=True)
        with mock.patch.dict("sys.modules", {"crosstalk_p0_install": install}):
            installed.write_text(json.dumps({"plugins": {install.PLUGIN_ID: [{"scope": "user", "version": "0.0.1"}]}}))
            self.assertIn("0.0.1", plain.plugin_stale())
            installed.write_text(json.dumps({"plugins": {}}))
            self.assertIn("not installed", plain.plugin_stale())

    def test_remote_control_flag_difference_between_account_folders(self):
        other = self.base / OTHER_ACCOUNT / ORG
        self.chat(CHAT_A_LOCAL, CHAT_A, folder=other, remoteControlUserEnabled=False, title="Chat A")
        self.chat(CHAT_B_LOCAL, CHAT_B, folder=other, remoteControlUserEnabled=True, title="Chat B")
        with mock.patch.object(core, "account_check", return_value={"state": "match"}):
            self.h.health()
            notice = self.kinds()["remote-flag-differs"]
            self.assertIn("1 chat(s)", notice["message"])
            self.assertIn("Chat A", notice["message"])
            self.chat(CHAT_A_LOCAL, CHAT_A, folder=other, remoteControlUserEnabled=True, title="Chat A")
            self.h.health()
            self.assertNotIn("remote-flag-differs", self.kinds())

    def test_notice_keeps_its_first_time_and_does_not_rewrite_when_unchanged(self):
        core.helper_notice_set("k", "kind", "message")
        first = core.helper_notices_load()["notices"]["k"]["since_ns"]
        path = core.helper_dir() / "notices.json"
        stamp = path.stat().st_mtime_ns
        core.helper_notice_set("k", "kind", "message")
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        core.helper_notice_set("k", "kind", "changed")
        self.assertEqual(core.helper_notices_load()["notices"]["k"]["since_ns"], first)
        core.helper_notice_clear("k")
        core.helper_notice_clear("k")
        self.assertEqual(core.helper_notices_load()["notices"], {})

    def test_a_helper_that_stopped_shows_a_virtual_notice(self):
        self.assertEqual(core.helper_notices_active(), [])  # never ran: nothing to report
        self.h.write_state()
        data = json.loads((core.helper_dir() / "state.json").read_text())
        data["ts_ms"] -= int((core.HELPER_STALE_S + 10) * 1000)
        (core.helper_dir() / "state.json").write_text(json.dumps(data))
        self.assertFalse(core.helper_alive())
        self.assertEqual([row["kind"] for row in core.helper_notices_active()], ["helper-not-running"])


class CommandLineTests(HelperBase):
    def run_main(self, module, argv, stdin=None):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            if stdin is None:
                code = module.main(argv)
            else:
                with mock.patch.object(cli, "read_stdin_json", return_value=stdin):
                    code = module.main(argv)
        return code, json.loads(buffer.getvalue().strip().splitlines()[-1])

    def test_helper_status_and_single_instance(self):
        code, value = self.run_main(helper, ["--status"])
        self.assertEqual((code, value["ok"], value["ever_ran"], value["lock_free"]), (0, True, False, True))
        with core.locked("helper-watch"):
            code, value = self.run_main(helper, ["--once"])
            self.assertEqual((code, value["ran"], value["reason"]), (0, False, "another-helper-is-running"))
            self.assertFalse(helper.run_locked(lambda: True))
        code, value = self.run_main(helper, ["--once"])
        self.assertEqual((code, value["ran"]), (0, True))
        code, value = self.run_main(helper, ["--status"])
        self.assertTrue(value["ever_ran"])
        self.assertTrue(value["alive"])

    def test_cli_notices_command(self):
        core.helper_notice_set("k", "plugin-stale", "The plugin is old.")
        code, value = self.run_main(cli, ["claude-notices"], stdin={"sessionId": CLAIMANT})
        self.assertEqual(value["notices"], [{"key": "k", "kind": "plugin-stale", "message": "The plugin is old."}])

    def test_status_shows_the_helper(self):
        core.helper_notice_set("k", "plugin-stale", "The plugin is old.")
        self.h.write_state()
        code, value = self.run_main(cli, ["status"])
        self.assertEqual(code, 0)
        self.assertEqual(value["helper"]["notices"][0]["kind"], "plugin-stale")
        self.assertIn("alive", value["helper"])


class SwitchHistoryTests(HelperBase):
    """Slice 3B.16: right after an account switch, opted in, the relink candidates' transcripts are cleaned so the
    restarted chats send their history to the phone again."""

    def setUp(self):
        super().setUp()
        core.helper_dir().mkdir(parents=True, exist_ok=True)
        core._write_json_atomic(core.accounts_path(), {"version": 1, "emails": {ACCOUNT: "me@example.com"}})
        self.set_config({"switch_history": ["Me@Example.com", OTHER_ACCOUNT]})  # an email and an account ID
        self.commands = []  # never scan the real process list
        scan = mock.patch.object(helper.switch_history, "claude_command_lines", side_effect=lambda: list(self.commands))
        scan.start()
        self.addCleanup(scan.stop)
        self.project = self.root / "claude" / "projects" / "C--work"
        self.project.mkdir(parents=True)

    def set_config(self, value):
        (core.helper_dir() / "config.json").write_text(json.dumps(value))

    def transcript(self, session, owner=ACCOUNT):
        path = self.project / f"{session}.jsonl"
        rows = [{"type": "history-suppression"},
                {"type": "bridge-session", "sessionId": session, "bridgeSessionId": "cse_1", "lastSequenceNum": 2,
                 "noHistoryBackfill": True, "ownerAccountUuid": owner, "ownerOrganizationUuid": ORG}]
        path.write_text("".join(json.dumps(row, separators=(",", ":")) + chr(10) for row in rows))  # as Claude Code writes
        return path

    def pointer_owner(self, path):
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertFalse([row for row in rows if row["type"] == "history-suppression"])
        pointer = rows[-1]
        self.assertNotIn("noHistoryBackfill", pointer)
        return pointer["ownerAccountUuid"]

    def switch(self, to=ACCOUNT, came_from=OTHER_ACCOUNT):
        self.add_log(self.load_line(age_s=600, account=came_from), self.load_line(age_s=60, account=to))
        self.h.follow_log()

    def test_off_by_default_and_while_halted(self):
        a = self.transcript(CHAT_A)
        before = a.read_bytes()
        self.switch()
        for config in ({}, {"switch_history": True}, {"switch_history": []}, {"switch_history": ["nobody@example.com"]}):
            self.set_config(config)
            self.assertIsNone(self.h.restore_switch_history())
        self.set_config({"switch_history": ["me@example.com", OTHER_ACCOUNT]})
        core.controls_update(halt=True)
        self.assertIsNone(self.h.restore_switch_history())
        self.assertEqual(a.read_bytes(), before)

    def test_switching_to_the_cli_login_restamps_to_the_previous_account_and_skips_a_running_chat(self):
        a, b = self.transcript(CHAT_A), self.transcript(CHAT_B)
        before_b = b.read_bytes()
        self.commands = [f"claude.exe --resume {CHAT_B} --print"]
        self.switch()
        report = self.h.restore_switch_history()
        self.assertEqual(report["outcome"], "done")
        self.assertEqual(report["chats"][CHAT_A_LOCAL]["outcome"], "cleaned")
        self.assertEqual(report["chats"][CHAT_B_LOCAL]["outcome"], "skipped:running")
        self.assertEqual(self.pointer_owner(a), OTHER_ACCOUNT)
        self.assertEqual(b.read_bytes(), before_b)
        self.assertEqual(len(list((core.helper_dir() / "history-backups").glob(f"{CHAT_A}.*.jsonl"))), 1)
        _load_id, load = max(self.loads().items(), key=lambda item: item[1]["seen_ns"])
        self.assertEqual(load["history"]["outcome"], "done")
        self.assertIsNone(self.h.restore_switch_history())  # once per load

    def test_switching_away_from_the_cli_login_restamps_to_the_new_account(self):
        (self.root / "claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": OTHER_ACCOUNT}}))
        a = self.transcript(CHAT_A, owner=OTHER_ACCOUNT)
        self.switch()
        self.assertEqual(self.h.restore_switch_history()["chats"][CHAT_A_LOCAL]["outcome"], "cleaned")
        self.assertEqual(self.pointer_owner(a), ACCOUNT)

    def test_a_restart_on_the_same_account_is_not_a_switch(self):
        a = self.transcript(CHAT_A)
        before = a.read_bytes()
        self.switch(to=ACCOUNT, came_from=ACCOUNT)
        self.assertEqual(self.h.restore_switch_history()["outcome"], "skipped:not-a-switch")
        self.assertEqual(a.read_bytes(), before)

    def test_a_switch_from_an_account_that_is_not_linked_is_left_alone(self):
        a = self.transcript(CHAT_A)
        before = a.read_bytes()
        self.switch(came_from="44444444-2222-4333-8444-555555555555")
        self.assertEqual(self.h.restore_switch_history()["outcome"], "skipped:not-linked")
        self.assertEqual(a.read_bytes(), before)

    def test_the_first_load_ever_is_left_alone(self):
        self.transcript(CHAT_A)
        self.add_log(self.load_line(age_s=60))
        self.h.follow_log()
        self.assertEqual(self.h.restore_switch_history()["outcome"], "skipped:no-previous-load")

    def test_tick_runs_the_step(self):
        a = self.transcript(CHAT_A)
        self.switch()
        summary = self.h.tick()
        self.assertEqual(summary["switch_history"]["outcome"], "done")
        self.assertEqual(self.pointer_owner(a), OTHER_ACCOUNT)


class DesktopJobsAndMessagingTests(HelperBase):
    """Slice 3B.19: `controls.messaging` is for letters only. The Desktop jobs (relink, kick, alert, rename, login
    prompt, switch history) stop on halt and skip a denied chat, never on messaging off; the relink and the history
    fix have their own config switches."""

    def set_config(self, value):
        core.helper_dir().mkdir(parents=True, exist_ok=True)
        (core.helper_dir() / "config.json").write_text(json.dumps(value))

    def test_desktop_check_is_halt_and_deny_only(self):
        core.controls_update(messaging="off")
        self.assertIsNone(core.desktop_check(None))
        self.assertIsNone(core.desktop_check("claude:" + CHAT_A))
        self.assertEqual(core.controls_check(None), "messaging-off")  # letters still stop
        core.controls_update(deny_add="claude:" + CHAT_A)
        self.assertEqual(core.desktop_check("claude:" + CHAT_A), "denied")
        self.assertIsNone(core.desktop_check("claude:" + CHAT_B))
        core.controls_update(halt=True)
        self.assertEqual(core.desktop_check(None), "halted")

    def test_messaging_off_still_relinks_and_halt_stops_it(self):
        core.controls_update(messaging="off")
        self.new_load_ready()
        report = self.h.process_loads()
        self.assertEqual(report["queued"], 2)
        self.assertTrue(core.relink_flag_path().exists())
        jobs = core.relink_claim(CLAIMANT)["jobs"]
        self.assertEqual(sorted(job["to"] for job in jobs), sorted([CHAT_A_LOCAL, CHAT_B_LOCAL]))
        core.controls_update(halt=True)
        self.h.process_loads()
        self.assertFalse(core.relink_flag_path().exists())
        self.assertEqual(core.relink_claim(CLAIMANT)["jobs"], [])

    def test_messaging_off_keeps_a_denied_chat_off_the_list(self):
        core.controls_update(messaging="off", deny_add="claude:" + CHAT_A)
        self.new_load_ready()
        self.h.process_loads()
        self.assertEqual(sorted(self.only_load()[1]["reconnects"]), [CHAT_B_LOCAL])

    def test_messaging_off_still_kicks_an_unclaimed_list_but_a_denied_chat_is_not_opened(self):
        self.live = set()
        core.controls_update(messaging="off", deny_add="claude:" + CHAT_B)
        self.new_load_ready()
        self.h.process_loads()
        self.advance(helper.RELINK_KICK_AFTER_S + 1)
        report = self.h.process_loads()
        self.assertEqual(self.shows, [CHAT_A])
        self.assertEqual(report["kick"]["to"], CHAT_A_LOCAL)

    def test_halt_stops_the_kick(self):
        self.live = set()
        self.new_load_ready()
        self.h.process_loads()
        core.controls_update(halt=True)
        self.advance(helper.RELINK_KICK_AFTER_S + 1)
        self.h.process_loads()
        self.assertEqual(self.shows, [])

    def test_a_letter_wake_still_needs_messaging_on(self):
        # the other side of the split: letters keep controls_check
        core.controls_update(messaging="off")
        self.assertEqual(self.h.wake_for_waiting_letters(), {"woken": []})
        self.assertEqual(core.controls_check(None), "messaging-off")

    def test_relink_false_lists_nothing_refuses_claims_and_kicks_nothing_but_marks_the_load_handled(self):
        self.live = set()
        self.set_config({"relink": False})
        self.new_load_ready()
        report = self.h.process_loads()
        self.assertEqual(report["queued"], 0)
        self.assertEqual(self.only_load()[1]["phase"], "done")  # so the rename-now pass can still run
        self.assertEqual(core.relink_enqueue(self.only_load()[0], CHAT_A_LOCAL, CHAT_A)["why"], "relink-off")
        self.live = {CLAIMANT}  # a running Desktop chat's plugin asks
        self.assertEqual(core.relink_claim(CLAIMANT), {"ok": True, "jobs": [], "reason": "relink-off"})
        self.assertEqual(core.self_relink_ask(CLAIMANT)["reason"], "relink-off")
        self.live = set()
        self.advance(helper.RELINK_KICK_AFTER_S + 1)
        self.h.process_loads()
        self.assertEqual(self.shows, [])
        self.assertFalse(core.relink_flag_path().exists())

    def test_relink_false_refuses_entries_already_queued(self):
        self.new_load_ready()
        self.h.process_loads()
        self.set_config({"relink": False})
        self.h.process_loads()
        self.assertEqual({e["state"] for e in self.only_load()[1]["reconnects"].values()}, {"refused"})
        self.assertEqual({e["reason"] for e in self.only_load()[1]["reconnects"].values()}, {"relink-off"})

    def test_absent_relink_means_on(self):
        self.set_config({})
        self.new_load_ready()
        self.assertEqual(self.h.process_loads()["queued"], 2)

    def test_messaging_off_does_not_stop_the_alert_but_relink_false_does(self):
        spawned = []
        self.h.alert_spawn = lambda folder, load_id: spawned.append(load_id)
        core.controls_update(messaging="off")
        self.set_config({"relink": False})
        self.add_log(self.load_line(age_s=0))
        self.h.follow_log()
        self.h.process_loads()
        self.assertEqual(spawned, [])
        self.set_config({})
        self.h.process_loads()
        self.assertEqual(len(spawned), 1)

    def test_history_fix_false_skips_the_switch_history_job(self):
        core.helper_dir().mkdir(parents=True, exist_ok=True)
        core._write_json_atomic(core.accounts_path(), {"version": 1, "emails": {ACCOUNT: "me@example.com"}})
        self.set_config({"switch_history": ["me@example.com", OTHER_ACCOUNT], "history_fix": False})
        self.add_log(self.load_line(age_s=600, account=OTHER_ACCOUNT), self.load_line(age_s=60, account=ACCOUNT))
        self.h.follow_log()
        self.assertIsNone(self.h.restore_switch_history())
        self.assertNotIn("history", max(self.loads().values(), key=lambda load: load["seen_ns"]))
        self.set_config({"switch_history": ["me@example.com", OTHER_ACCOUNT]})  # absent = on
        self.assertEqual(self.h.restore_switch_history()["outcome"], "done")

    def test_messaging_off_does_not_stop_the_switch_history_job(self):
        core.controls_update(messaging="off")
        core._write_json_atomic(core.accounts_path(), {"version": 1, "emails": {ACCOUNT: "me@example.com"}})
        self.set_config({"switch_history": ["me@example.com", OTHER_ACCOUNT]})
        self.add_log(self.load_line(age_s=600, account=OTHER_ACCOUNT), self.load_line(age_s=60, account=ACCOUNT))
        self.h.follow_log()
        self.assertEqual(self.h.restore_switch_history()["outcome"], "done")  # it already ignored messaging: pinned

    def test_the_tick_summary_reports_both_checks(self):
        core.controls_update(messaging="off")
        summary = self.h.tick()
        self.assertEqual((summary["controls"], summary["desktop_controls"]), ("messaging-off", None))


class FastRelinkTests(HelperBase):
    """Slice 3B.18: a Desktop restart is relinked in seconds. Readiness replaced the fixed 30 s settle wait, main.log is
    followed fast while a load is fresh, the kick counts from the moment the list was made, and a chat Desktop
    enabled itself since then is skipped at claim."""

    def rc_line(self, local, age_s=0):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.now - age_s))
        return f"{stamp} [info] Enabling remote control for session {local}\n"

    def test_the_speed_constants_hold_the_owner_targets(self):
        self.assertLessEqual(helper.FAST_FOLLOW_S, 1.0)
        self.assertLessEqual(helper.IDLE_LOG_POLL_S, 3.0)
        self.assertLessEqual(helper.LOAD_READY_MIN_S, 5.0)
        self.assertTrue(10.0 <= helper.RELINK_KICK_AFTER_S <= 15.0)
        self.assertFalse(hasattr(helper, "SETTLE_S"))  # the fixed settle wait is gone

    # ---- readiness instead of a fixed settle
    def test_readiness_is_a_pure_decision(self):
        not_ready = helper.Helper.load_not_ready
        self.assertEqual(not_ready(0.5, True), "settling")
        self.assertEqual(not_ready(helper.LOAD_READY_MIN_S, False), "no-records")
        self.assertIsNone(not_ready(helper.LOAD_READY_MIN_S, True))
        self.assertIsNone(not_ready(helper.LOAD_READY_MAX_WAIT_S, False))  # never waits for records forever

    def test_the_list_is_made_seconds_after_the_load_line_not_after_a_fixed_wait(self):
        self.add_log(self.load_line(age_s=1))
        self.h.follow_log()
        self.advance(1.0)
        self.assertEqual(self.h.process_loads()["queued"], 0)  # the load line is too fresh: another may follow
        self.assertEqual(self.only_load()[1]["phase"], "pending")
        self.advance(helper.LOAD_READY_MIN_S)
        self.assertEqual(self.h.process_loads()["queued"], 2)
        load = self.only_load()[1]
        self.assertEqual(load["phase"], "enqueued")
        self.assertLess((load["listed_ns"] - load["seen_ns"]) / 1e9, 10.0)

    def test_a_newer_load_line_inside_the_ready_time_replaces_the_load_before_any_list(self):
        self.add_log(self.load_line(age_s=1))  # an account switch passes through an in-between load
        self.h.follow_log()
        self.advance(1.0)
        self.add_log(self.load_line(age_s=0))
        self.h.follow_log()
        self.advance(helper.LOAD_READY_MIN_S + 0.5)
        self.assertEqual(self.h.process_loads()["queued"], 2)
        phases = sorted(load["phase"] for load in self.loads().values())
        self.assertEqual(phases, ["enqueued", "superseded"])

    def test_a_folder_without_chat_records_waits_for_them_but_only_up_to_the_cap(self):
        for path in self.folder.glob("local_*.json"):
            path.unlink()
        self.add_log(self.load_line(age_s=1))
        self.h.follow_log()
        self.advance(5)
        self.assertEqual(self.h.process_loads()["queued"], 0)
        self.assertEqual(self.only_load()[1]["phase"], "pending")  # nothing on disk yet: keep waiting
        self.chat(CHAT_A_LOCAL, CHAT_A, remoteControlUserEnabled=True, title="Chat A")
        self.advance(1)
        self.assertEqual(self.h.process_loads()["queued"], 1)

    def test_an_account_folder_that_stays_empty_ends_at_the_cap(self):
        for path in self.folder.glob("local_*.json"):
            path.unlink()
        self.add_log(self.load_line(age_s=1))
        self.h.follow_log()
        self.advance(helper.LOAD_READY_MAX_WAIT_S - 1)
        self.h.process_loads()
        self.assertEqual(self.only_load()[1]["phase"], "pending")
        self.advance(2)
        self.h.process_loads()
        self.assertEqual(self.only_load()[1]["phase"], "done")  # listed nothing, so nothing is left to wait for
        self.assertEqual(self.only_load()[1]["reconnects"], {})

    # ---- never a second relink, never against the owner's off
    def test_a_chat_desktop_enabled_after_the_list_is_skipped_at_claim(self):
        self.new_load_ready()
        self.h.process_loads()
        self.add_log(self.rc_line(CHAT_A_LOCAL, age_s=0))  # the owner opened chat A: Desktop turned its link on
        jobs = core.relink_claim(CLAIMANT)["jobs"]
        self.assertEqual([job["to"] for job in jobs], [CHAT_B_LOCAL])
        entry = self.only_load()[1]["reconnects"][CHAT_A_LOCAL]
        self.assertEqual((entry["state"], entry["reason"]), ("skipped", core.RELINK_SKIP_DESKTOP_ENABLED))
        self.assertEqual(core.relink_claim(CLAIMANT)["jobs"], [])  # and nobody takes it later

    def test_an_owner_off_chat_is_still_never_claimed_on_the_fast_path(self):
        self.new_load_ready()
        self.h.process_loads()
        self.chat(CHAT_B_LOCAL, CHAT_B, remoteControlUserEnabled=False, title="Chat B")
        self.assertEqual([job["to"] for job in core.relink_claim(CLAIMANT)["jobs"]], [CHAT_A_LOCAL])
        self.assertEqual(self.only_load()[1]["reconnects"][CHAT_B_LOCAL]["state"], "skipped")

    # ---- the fast follow
    def test_load_in_flight_is_a_pure_decision(self):
        in_flight = helper.Helper.load_in_flight
        now = 10_000.0

        def ledger(age_s, phase, states=()):
            return {"L": {"seen_ns": int((now - age_s) * 1e9), "phase": phase,
                          "reconnects": {f"c{i}": {"state": state} for i, state in enumerate(states)}}}
        self.assertFalse(in_flight({}, now))
        self.assertTrue(in_flight(ledger(5, "pending"), now))
        self.assertFalse(in_flight(ledger(helper.FAST_FOLLOW_MAX_S + 1, "pending"), now))  # a stuck load stops it
        self.assertTrue(in_flight(ledger(30, "enqueued", ("done", "queued")), now))
        self.assertTrue(in_flight(ledger(30, "enqueued", ("claimed",)), now))
        self.assertFalse(in_flight(ledger(30, "enqueued", ("done", "failed")), now))
        self.assertFalse(in_flight(ledger(30, "done"), now))
        self.assertFalse(in_flight(ledger(30, "skipped:not-active-account"), now))
        newer = dict(ledger(300, "enqueued", ("queued",)), N={"seen_ns": int((now - 5) * 1e9), "phase": "done"})
        self.assertFalse(in_flight(newer, now))  # only the newest load counts

    def test_the_follow_interval_switches_with_the_load_state(self):
        self.h.in_flight = False
        self.assertEqual(self.h.follow_delay(), helper.IDLE_LOG_POLL_S)
        self.h.in_flight = True
        self.assertEqual(self.h.follow_delay(), helper.FAST_FOLLOW_S)
        self.assertLess(helper.FAST_FOLLOW_S, helper.IDLE_LOG_POLL_S)
        self.assertLess(helper.IDLE_LOG_POLL_S, helper.TICK_S)

    def test_a_light_step_does_nothing_while_idle_and_the_log_is_unchanged(self):
        self.add_log("[info] hello\n")
        self.assertTrue(self.h.log_changed())  # remembers the size
        with mock.patch.object(self.h, "follow_log") as follow, mock.patch.object(self.h, "process_loads") as process:
            self.assertEqual(self.h.fast_step(), {})
        follow.assert_not_called()
        process.assert_not_called()
        self.add_log("[info] hello again\n")
        with mock.patch.object(self.h, "follow_log", return_value=0) as follow, \
                mock.patch.object(self.h, "process_loads", return_value={}) as process:
            self.h.fast_step()
        follow.assert_called_once()
        process.assert_called_once()

    def test_a_desktop_start_is_followed_to_a_list_and_a_finish_by_light_steps_alone(self):
        self.add_log(self.load_line(age_s=0))
        first = self.h.fast_step()
        self.assertEqual(first["loads_recorded"], 1)
        self.assertTrue(self.h.in_flight)
        self.advance(helper.LOAD_READY_MIN_S + 0.1)
        self.assertEqual(self.h.fast_step()["loads"]["queued"], 2)  # the log did not change; the load is in flight
        self.assertTrue(core.relink_flag_path().exists())
        for job in core.relink_claim(CLAIMANT)["jobs"]:
            self.assertEqual(core.relink_done(CLAIMANT, job["to"], job["attempt_id"], True)["state"], "done")
        self.h.fast_step()
        self.assertEqual(self.only_load()[1]["phase"], "done")
        self.assertFalse(self.h.in_flight)
        self.assertEqual(self.h.follow_delay(), helper.IDLE_LOG_POLL_S)
        self.assertEqual(self.h.fast_step(), {})

    def test_the_watch_loop_ticks_every_tick_s_and_looks_lightly_in_between(self):
        clock = [0.0]
        stubs = []

        class Stub:
            def __init__(self):
                self.ticks, self.steps, self.delay = 0, 0, helper.FAST_FOLLOW_S
                self.log = type("Log", (), {"say": staticmethod(lambda message: None)})()
                stubs.append(self)

            def tick(self):
                self.ticks += 1

            def fast_step(self):
                self.steps += 1

            def follow_delay(self):
                return self.delay

        def nap(seconds):
            clock[0] += seconds
        with mock.patch.object(helper, "Helper", Stub), mock.patch.object(helper.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(helper.time, "sleep", nap):
            helper.watch(max_seconds=40)
        fast = stubs[0]
        self.assertEqual(fast.ticks, 3)  # at 0, 15 and 30 s
        self.assertGreater(fast.steps, 60)  # about every half second in between
        stubs.clear()
        clock[0] = 0.0
        original = Stub.follow_delay
        Stub.follow_delay = lambda self: helper.IDLE_LOG_POLL_S
        try:
            with mock.patch.object(helper, "Helper", Stub), mock.patch.object(helper.time, "monotonic", lambda: clock[0]), \
                    mock.patch.object(helper.time, "sleep", nap):
                helper.watch(max_seconds=40)
        finally:
            Stub.follow_delay = original
        self.assertEqual(stubs[0].ticks, 3)
        self.assertLess(stubs[0].steps, 25)  # about every 2 s while idle

    def test_the_plugin_flag_is_written_when_missing_and_refreshed_rarely(self):
        self.new_load_ready()
        self.h.process_loads()
        flag = core.relink_flag_path()
        first = flag.read_text()
        self.advance(1)
        self.h.sync_relink_flag()
        self.assertEqual(flag.read_text(), first)  # not rewritten on every half-second look
        self.advance(helper.RELINK_FLAG_REFRESH_S)
        self.h.sync_relink_flag()
        self.assertNotEqual(flag.read_text(), first)  # refreshed well inside the plugin's 120 s window
        flag.unlink()
        self.h.sync_relink_flag()
        self.assertTrue(flag.exists())  # a missing flag is written at once

    # ---- the kick counts from the list
    def test_the_kick_counts_from_the_moment_the_list_was_made(self):
        self.live = set()
        for path in self.folder.glob("local_*.json"):
            path.unlink()  # no records yet, so the list waits for them
        self.add_log(self.load_line(age_s=1))
        self.h.follow_log()
        self.advance(20)
        self.h.process_loads()
        self.chat(CHAT_A_LOCAL, CHAT_A, remoteControlUserEnabled=True, title="Chat A", lastActivityAt=1000)
        self.advance(1)
        self.h.process_loads()  # listed about 21 s after the load line
        load = self.only_load()[1]
        self.assertEqual(load["phase"], "enqueued")
        self.assertGreater((load["listed_ns"] - load["seen_ns"]) / 1e9, 20)
        self.advance(helper.RELINK_KICK_AFTER_S - 1)  # long past the load line, but not yet after the list
        self.h.process_loads()
        self.assertEqual(self.shows, [])
        self.advance(2)
        self.h.process_loads()
        self.assertEqual(self.shows, [CHAT_A])

    def test_a_ready_load_is_kicked_about_ten_seconds_after_its_list(self):
        self.live = set()
        self.new_load_ready()
        self.h.process_loads()
        listed = self.only_load()[1]["listed_ns"] / 1e9
        self.advance(helper.RELINK_KICK_AFTER_S - 0.5)
        self.h.process_loads()
        self.assertEqual(self.shows, [])
        self.advance(1)
        self.h.process_loads()
        self.assertEqual(len(self.shows), 1)
        self.assertLess(self.now - listed, helper.RELINK_KICK_AFTER_S + 2)


class RelinkAlertDecisionTests(HelperBase):
    """Whether, and when, the helper starts the relink alert window (the window itself is never started here)."""

    def setUp(self):
        super().setUp()
        self.spawned = []
        self.markers = []
        self.h.alert_spawn = self.fake_spawn

    def fake_spawn(self, folder, load_id):
        self.markers.append((self.loads()[load_id].get("alert") or {}).get("state"))
        self.spawned.append((Path(folder), load_id))

    def load_now(self, age_s=0, account=ACCOUNT):
        self.add_log(self.load_line(age_s=age_s, account=account))
        self.h.follow_log()
        return self.only_load()[0] if len(self.loads()) == 1 else None

    def alert(self):
        return next(iter(self.loads().values())).get("alert")

    def test_the_alert_starts_at_once_for_a_load_with_chats_to_relink_and_only_once(self):
        load_id = self.load_now()
        self.h.process_loads()  # the load line is brand new: the list is not made yet
        self.assertEqual(self.spawned, [(core.helper_dir(), load_id)])
        self.assertEqual(self.markers, ["starting"])  # remembered BEFORE the window is started
        self.assertEqual(self.alert()["state"], "started")
        self.advance(helper.LOAD_READY_MIN_S + 0.5)
        self.h.process_loads()  # the list is made now
        for _ in range(3):
            self.advance(1)
            self.h.process_loads()
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(helper.Helper(clock=lambda: self.now).process_loads()["queued"], 0)
        self.assertEqual(len(self.spawned), 1)

    def test_no_alert_when_there_is_nothing_to_relink(self):
        for local, session in ((CHAT_A_LOCAL, CHAT_A), (CHAT_B_LOCAL, CHAT_B)):
            self.chat(local, session, remoteControlUserEnabled=False)
        self.load_now()
        self.h.process_loads()
        self.advance(helper.LOAD_READY_MIN_S + 0.5)
        self.h.process_loads()
        self.assertEqual(self.spawned, [])
        self.assertIsNone(self.alert())

    def test_the_config_switch_turns_the_alert_off_and_absent_means_on(self):
        core.helper_dir().mkdir(parents=True, exist_ok=True)
        (core.helper_dir() / "config.json").write_text(json.dumps({"relink_alert": False}))
        self.load_now()
        self.h.process_loads()
        self.advance(helper.LOAD_READY_MIN_S + 0.5)
        self.h.process_loads()
        self.assertEqual(self.spawned, [])
        self.assertIsNone(self.alert())
        (core.helper_dir() / "config.json").write_text("{}")  # absent key: on
        self.h.process_loads()
        self.assertEqual(len(self.spawned), 0)  # the list was already made without an alert: not started late either
        self.advance(1)
        self.h.alert_empty.clear()
        self.assertEqual(self.h.ensure_alert(next(iter(self.loads())), next(iter(self.loads().values())),
                                             core.desktop_active_account(), None, 3.0), "started")

    def test_halt_starts_no_alert_and_messaging_off_does_not_matter(self):
        # slice 3B.19: messaging off used to start no alert; the relink no longer depends on messaging
        self.load_now()
        core.controls_update(halt=True)
        self.h.process_loads()
        self.assertEqual(self.spawned, [])
        core.controls_update(halt=False, messaging="off")  # lifted inside the same minute: the relink goes on
        self.advance(helper.LOAD_READY_MIN_S + 0.5)
        self.h.process_loads()
        self.assertEqual(len(self.spawned), 1)

    def test_a_load_older_than_the_alert_age_limit_gets_no_alert(self):
        self.load_now()
        self.advance(helper.ALERT_MAX_AGE_S + 1)
        self.h.process_loads()  # the list is made late (the helper was busy or blocked)
        self.assertEqual(self.only_load()[1]["phase"], "enqueued")
        self.assertEqual(self.spawned, [])

    def test_another_accounts_load_gets_no_alert(self):
        self.load_now(account=OTHER_ACCOUNT)
        self.h.process_loads()
        self.assertEqual(self.spawned, [])

    def test_a_failed_start_is_remembered_and_not_repeated(self):
        def broken(folder, load_id):
            raise OSError("no process")
        self.h.alert_spawn = broken
        self.load_now()
        self.h.process_loads()
        self.assertEqual(self.alert()["state"], "spawn-failed:OSError")
        self.h.alert_spawn = self.fake_spawn
        self.advance(helper.LOAD_READY_MIN_S + 0.5)
        self.h.process_loads()
        self.assertEqual(self.spawned, [])

    def test_the_real_window_is_only_started_with_a_window_platform(self):
        self.h.alert_spawn = None
        with mock.patch.object(helper.relink_alert, "default_spawn", side_effect=AssertionError("started a window")):
            self.load_now()
            self.h.process_loads()  # tests, --once and CROSSTALK_P0_FOCUS=off have no window platform
        self.assertEqual(self.alert()["state"], "no-window-platform")

    def test_with_a_window_platform_the_window_process_gets_the_helper_folder_and_the_load(self):
        self.h.alert_spawn = None
        self.h.window_platform_override = type("Platform", (), {"available": True})()
        with mock.patch.object(helper.relink_alert, "default_spawn") as spawn:
            load_id = self.load_now()
            self.h.process_loads()
            self.advance(helper.LOAD_READY_MIN_S + 0.5)
            self.h.process_loads()
        spawn.assert_called_once_with(core.helper_dir(), load_id)
        self.assertEqual(self.alert()["state"], "started")


class RelinkAlertViewTests(unittest.TestCase):
    """The window's content and lifecycle are pure functions over the loads.json ledger (no window in these tests)."""
    alert = helper.relink_alert
    SEEN = 1000.0

    def ledger(self, entries, phase="enqueued", extra=None):
        loads = {"L1": {"seen_ns": int(self.SEEN * 1e9), "phase": phase, "reconnects": entries}}
        loads.update(extra or {})
        return {"loads": loads}

    @staticmethod
    def entry(state, title="Chat", **more):
        return dict({"state": state, "title": title, "session": "s"}, **more)

    def view(self, entries, age_s=10, **kwargs):
        return self.alert.view_for_load(self.ledger(entries, **kwargs), "L1", self.SEEN + age_s)

    def test_working_shows_the_title_before_the_list_and_n_of_m_after(self):
        before = self.view({}, phase="pending", age_s=1)
        self.assertEqual((before["state"], before["title"], before["lines"]), ("working", self.alert.WORKING_TEXT, []))
        self.assertIn("Enabling Remote Control for your sessions", before["title"])
        mid = self.view({"a": self.entry("done"), "b": self.entry("claimed"), "c": self.entry("queued")})
        self.assertEqual((mid["state"], mid["lines"], mid["done"], mid["total"]), ("working", ["1 of 3"], 1, 3))

    def test_it_closes_when_nothing_was_listed(self):
        self.assertEqual(self.view({}, phase="pending", age_s=self.alert.EMPTY_LIST_WAIT_S + 1)["state"], "closed")
        self.assertEqual(self.view({}, phase="enqueued")["state"], "closed")

    def test_all_done_is_a_short_done_view(self):
        view = self.view({"a": self.entry("done"), "b": self.entry("done")})
        self.assertEqual((view["state"], view["title"]), ("done", "2 of 2 sessions connected"))

    def test_a_chat_desktop_enabled_itself_counts_as_connected_and_an_owner_off_chat_does_not_count(self):
        view = self.view({"a": self.entry("done"), "b": self.entry("skipped", reason="desktop-enabled"),
                          "c": self.entry("skipped", reason="owner-off-or-unreadable")})
        self.assertEqual((view["state"], view["total"]), ("done", 2))

    def test_a_failure_names_the_chats_and_says_so(self):
        entries = {f"d{i}": self.entry("done") for i in range(4)}
        entries["x"] = self.entry("failed", title="PC Cami's Toystore")
        view = self.view(entries)
        self.assertEqual(view["state"], "problem")
        self.assertEqual(view["title"], "4 of 5 connected · 1 could not reconnect")
        self.assertIn("PC Cami's Toystore", view["lines"])

    def test_expired_entries_are_problems_and_a_long_list_is_cut(self):
        entries = {f"b{i}": self.entry("expired", title=f"Chat {i}") for i in range(6)}
        view = self.view(entries)
        self.assertEqual(view["state"], "problem")
        self.assertEqual(view["lines"][self.alert.TITLES_SHOWN], "+2 more")

    def test_an_owner_halt_is_not_reported_as_a_failure(self):
        refused = {"a": self.entry("refused", reason="halted"), "b": self.entry("refused", reason="halted")}
        self.assertEqual(self.view(refused)["state"], "closed")  # the owner's stop: the window just goes away
        mixed = dict(refused, c=self.entry("done"))
        self.assertEqual((self.view(mixed)["state"], self.view(mixed)["total"]), ("done", 1))

    def test_entries_nobody_took_for_too_long_become_a_problem_instead_of_spinning(self):
        view = self.view({"a": self.entry("done"), "b": self.entry("queued", title="Stuck")},
                         age_s=self.alert.SHOW_UP_TO_S + 5)
        self.assertEqual((view["state"], view["bad"]), ("problem", ["Stuck"]))

    def test_a_newer_load_a_missing_load_and_a_bad_ledger_close_the_window(self):
        newer = {"N": {"seen_ns": int((self.SEEN + 5) * 1e9), "phase": "pending", "reconnects": {}}}
        self.assertEqual(self.alert.view_for_load(self.ledger({"a": self.entry("queued")}, extra=newer), "L1",
                                                  self.SEEN + 6)["state"], "closed")
        self.assertEqual(self.alert.view_for_load(self.ledger({}), "other", self.SEEN)["state"], "closed")
        self.assertEqual(self.alert.view_for_load(None, "L1", self.SEEN)["state"], "closed")

    def test_the_lifecycle_decisions(self):
        decide = self.alert.window_action
        self.assertEqual(decide({"state": "closed"}, 5.0, None)[0], "close")
        self.assertEqual(decide({"state": "working"}, 5.0, 1.0), ("show", None))
        action, started = decide({"state": "done"}, 100.0, None)
        self.assertEqual((action, started), ("show", 100.0))
        self.assertEqual(decide({"state": "done"}, 100.0 + self.alert.DONE_SHOW_S, started)[0], "close")
        action, started = decide({"state": "problem"}, 100.0, None)
        self.assertEqual(action, "show")
        self.assertEqual(decide({"state": "problem"}, 100.0 + self.alert.PROBLEM_SHOW_S - 1, started)[0], "show")
        self.assertEqual(decide({"state": "problem"}, 100.0 + self.alert.PROBLEM_SHOW_S, started)[0], "close")

    def test_the_ledger_read_tolerates_a_missing_or_half_written_file(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(self.alert.read_ledger(Path(folder)))
            (Path(folder) / "loads.json").write_text("{\"loads\": ", encoding="utf-8")
            self.assertIsNone(self.alert.read_ledger(Path(folder)))
            (Path(folder) / "loads.json").write_text(json.dumps(self.ledger({})), encoding="utf-8")
            self.assertIn("loads", self.alert.read_ledger(Path(folder)))

    def test_the_window_module_is_stdlib_only_and_never_writes_the_ledger(self):
        import ast
        source = (KIT / "crosstalk_relink_alert.py").read_text(encoding="utf-8")
        modules = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                modules |= {item.name.split(".")[0] for item in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                modules.add((node.module or "").split(".")[0])
        self.assertLessEqual(modules, set(sys.stdlib_module_names))
        self.assertNotIn("crosstalk_p0_core", modules)
        for needle in ("write_text(", "os.replace(", ".unlink(", "send_message", "set_remote_control"):
            self.assertNotIn(needle, source)


class StartupShortcutTests(unittest.TestCase):
    def test_plan_names_the_hidden_launch_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            plan = install.helper_startup_plan(folder, pythonw="C:\\rt\\pythonw.exe")
            self.assertEqual(plan["shortcut"], str(Path(folder) / "Crosstalk helper.lnk"))
            self.assertEqual(plan["target"], "C:\\rt\\pythonw.exe")
            self.assertTrue(plan["arguments"].startswith("-I -B "))
            self.assertTrue(plan["arguments"].endswith(" --watch"))
            self.assertIn("crosstalk_helper.py", plan["arguments"])
            self.assertEqual(plan["window_style"], 1)
            self.assertTrue(plan["script_exists"])
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_install_uses_the_runtime_pythonw_and_only_touches_its_own_file(self):
        with tempfile.TemporaryDirectory() as folder:
            created = []
            neighbour = Path(folder) / "Claude chat sync.lnk"
            neighbour.write_text("keep")
            result = install.helper_startup_install(folder, pythonw="C:\\rt\\pythonw.exe",
                                                    create=lambda plan: created.append(plan))
            self.assertEqual(len(created), 1)
            self.assertEqual(created[0]["shortcut"], str(Path(folder) / "Crosstalk helper.lnk"))
            self.assertTrue(result["created"])
            self.assertEqual(neighbour.read_text(), "keep")
            self.assertEqual(install.helper_startup_remove(folder)["removed"], False)
            self.assertEqual(neighbour.read_text(), "keep")

    def test_install_refuses_a_missing_runtime_pythonw(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(install.InstallError) as caught:
                install.helper_startup_install(folder, pythonw=str(Path(folder) / "nope" / "pythonw.exe"))
            self.assertEqual(caught.exception.reason, "helper-runtime-pythonw-missing")
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_the_default_target_is_the_installed_runtime_not_system_python(self):
        plan = install.helper_startup_plan(tempfile.gettempdir())
        self.assertTrue(plan["target"].endswith(os.path.join("runtime", "Scripts", "pythonw.exe")))
        self.assertTrue(plan["target"].startswith(str(install.core.eutonos_home())))  # ~/.eutonos unless EUTONOS_HOME


class PluginTemplateTests(unittest.TestCase):
    def template(self):
        return (KIT / "claude-plugin" / "plugin-src" / "register.mjs.in").read_text(encoding="utf-8")

    def test_template_has_the_relink_hooks_the_guard_and_no_relay(self):
        text = self.template()
        for needle in ("claude-relink-claim", "claude-relink-done", "claude-relink-guard", "claude-self-relink",
                       "claude-self-relink-done", "relink-open.json", "$.tool.check", "on('tool.check', { tool: SET_REMOTE_CONTROL }", "on('tool.check', { tool: SEND_MESSAGE }", "next.origin", "$.plugin.name", "decision: 'ask'", "$.tool.call", "on('turn.complete'",
                       "claude-notices", "$.ui.toast", "__CROSSTALK_P0_HELPER__", "{ deny:"):
            self.assertIn(needle, text)
        for needle in ("__CROSSTALK_P0_RELAY__", "claude-relay-claim", "claude-relay-ticket", "heartbeat", "ticket-open",
                       "on('prompt.submit'", "on('prompt.compose'", "JOB_FILE"):
            self.assertNotIn(needle, text)
        # both Desktop tools are guarded, and the plugin never switches Remote Control off
        self.assertIn("{ tool: SET_REMOTE_CONTROL }", text)
        self.assertIn("{ tool: SEND_MESSAGE }", text)
        self.assertNotIn("enabled: false", text.replace("args.enabled === false", ""))

    def test_the_relink_poll_is_fast_starts_before_the_registration_and_sends_a_few_chats_at_a_time(self):
        text = self.template()
        self.assertIn("const RELINK_POLL_MS = 1000", text)
        self.assertLess(text.index("$.clock.every(RELINK_POLL_MS"), text.index("'claude-register'"))
        self.assertEqual(text.count("$.clock.every(RELINK_POLL_MS"), 1)
        self.assertIn("const RELINK_PARALLEL = 3", text)
        self.assertIn("Promise.all", text)
        tick = text[text.index("async function relinkTick"):text.index("async function selfRelink")]
        self.assertEqual(tick.count("await $.tool.check("), 1)  # the permission is asked once for the whole list

    def test_the_versions_agree_and_were_bumped(self):
        version = json.loads((KIT / "claude-plugin" / "plugin" / ".claude-plugin" / "plugin.json").read_text())
        market = json.loads((KIT / "claude-plugin" / ".claude-plugin" / "marketplace.json").read_text())
        self.assertEqual(version["version"], market["plugins"][0]["version"])
        self.assertEqual(version["version"], "0.0.14")


if __name__ == "__main__":
    unittest.main()
