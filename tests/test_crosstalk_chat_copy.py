"""Offline tests: copying chat lists between the owner's linked Claude accounts (slice 3B.19, helper job j).

Ports the scenarios of the personal claude-chat-sync tool (copy, skip unsettled, deletion to trash, refuse links, never
write the active folder) onto crosstalk_chat_copy, then the helper job around it (off by default, halt pauses it,
linked accounts only, a folder never seen loaded is never written to, no folder is ever created, the personal copier's
Startup shortcut blocks it). Temporary folders only: never a real Claude folder, Startup folder or account."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from tests.test_crosstalk_helper import ACCOUNT, ORG, OTHER_ACCOUNT, HelperBase, core, helper  # noqa: F401


KIT = Path(__file__).resolve().parents[1] / "tools" / "crosstalk"
spec = importlib.util.spec_from_file_location("crosstalk_chat_copy_under_test", KIT / "crosstalk_chat_copy.py")
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)

A_ID = "aaaaaaaa-2222-4333-8444-555555555555"
B_ID = "bbbbbbbb-2222-4333-8444-555555555555"
C_ID = "cccccccc-2222-4333-8444-555555555555"


def uuid_of(number: int) -> str:
    return f"{number:08d}-2222-4333-8444-555555555555"


def put(folder: Path, name: str, value=None, raw: bytes | None = None, age_s: float = 30.0) -> Path:
    path = Path(folder) / name
    path.write_bytes(raw if raw is not None else json.dumps(value if value is not None else {"n": name},
                                                            separators=(",", ":")).encode("utf-8"))
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))
    return path


def listing(folder: Path) -> dict:
    return {p.name: p.read_bytes() for p in sorted(Path(folder).iterdir()) if p.is_file() and cc.wanted(p.name)}


class CopierBase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chat-copy-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.a, self.b = self.root / "sessions" / A_ID / "org", self.root / "sessions" / B_ID / "org"
        self.a.mkdir(parents=True)
        self.b.mkdir(parents=True)
        self.home = self.root / "home" / "chat-copy"
        self.said: list[str] = []
        self.halt = None
        self.copier = cc.ChatCopier(self.home, say=self.said.append, halted=lambda: self.halt)
        self.copier.remember(B_ID, "org", str(self.b))  # the helper has seen Desktop load B earlier

    def active(self, account=A_ID, folder=None):
        return {"id": account, "org": "org", "folder": str(folder or (self.a if account == A_ID else self.b))}

    def run_pass(self, **options):
        return self.copier.run(self.active(), {A_ID, B_ID}, **options)


class CopyTests(CopierBase):
    def test_the_loaded_folder_is_copied_into_the_other_and_a_second_pass_changes_nothing(self):
        for number in (1, 2, 3):
            put(self.a, f"local_{uuid_of(number)}.json", {"title": f"Chat {number}"})
        put(self.a, "archived-sessions.idx", {"archived": []})
        put(self.a, f"deleted_{uuid_of(9)}", raw=b"1791000000000")
        put(self.a, "scheduled-tasks.json", {"stays": "per account"})  # never copied
        (self.a / "backlog").mkdir()
        report = self.run_pass(snapshots=False)
        self.assertEqual((report["state"], report["copied"]), ("ran", 5))
        self.assertEqual(listing(self.a), listing(self.b))
        self.assertFalse((self.b / "scheduled-tasks.json").exists())
        self.assertFalse((self.b / "backlog").exists())
        self.assertEqual(self.run_pass(snapshots=False)["copied"], 0)
        self.assertFalse([p for p in self.b.iterdir() if p.name.endswith(cc.TMP_SUFFIX)])  # no temp leftovers

    def test_a_rename_is_carried_over_and_keeps_the_source_mtime(self):
        path = put(self.a, f"local_{uuid_of(1)}.json", {"title": "Old"})
        self.run_pass(snapshots=False)
        put(self.a, path.name, {"title": "Renamed in A"})
        self.run_pass(snapshots=False)
        self.assertIn(b"Renamed in A", (self.b / path.name).read_bytes())
        self.assertEqual(int((self.b / path.name).stat().st_mtime), int(path.stat().st_mtime))

    def test_a_half_written_file_is_skipped_and_said_once(self):
        good = json.dumps({"title": "Whole chat record"}).encode()
        put(self.a, f"local_{uuid_of(1)}.json", raw=good[: len(good) // 2])
        for _ in range(3):
            report = self.run_pass(snapshots=False)
        self.assertEqual((report["copied"], report["invalid"]), (0, 1))
        self.assertFalse((self.b / f"local_{uuid_of(1)}.json").exists())
        self.assertEqual(len([line for line in self.said if "not valid JSON" in line]), 1)
        put(self.a, f"local_{uuid_of(1)}.json", raw=good)
        self.assertEqual(self.run_pass(snapshots=False)["copied"], 1)

    def test_a_file_written_less_than_two_seconds_ago_waits_for_the_next_pass(self):
        put(self.a, f"local_{uuid_of(1)}.json", {"title": "Fresh"}, age_s=0)
        report = self.run_pass(snapshots=False)
        self.assertEqual((report["copied"], report["waiting"]), (0, 1))
        self.assertEqual(listing(self.b), {})
        self.copier.settle_s = 0.0
        self.assertEqual(self.run_pass(snapshots=False)["copied"], 1)

    def test_a_file_that_changes_while_it_is_read_waits(self):
        path = put(self.a, f"local_{uuid_of(1)}.json", {"title": "Moving"})
        real_open = open

        def changing(name, mode="r", *args, **kwargs):
            handle = real_open(name, mode, *args, **kwargs)
            if str(name) == str(path) and "b" in mode:
                os.utime(path, (time.time() - 1, time.time() - 1))  # Desktop touched it meanwhile
            return handle
        with mock.patch("builtins.open", changing):
            report = self.run_pass(snapshots=False)
        self.assertEqual((report["copied"], report["waiting"]), (0, 1))

    def test_the_active_folder_is_never_written(self):
        put(self.a, f"local_{uuid_of(1)}.json", {"title": "In A"})
        put(self.b, f"local_{uuid_of(1)}.json", {"title": "Edited on inactive B"})
        put(self.b, f"local_{uuid_of(7)}.json", {"title": "Only on B"})
        before = listing(self.a)
        before_stamps = {p.name: p.stat().st_mtime_ns for p in self.a.iterdir()}
        report = self.run_pass()
        self.assertEqual(listing(self.a), before)
        self.assertEqual({p.name: p.stat().st_mtime_ns for p in self.a.iterdir()}, before_stamps)
        self.assertEqual(report["left_alone"], 1)  # exists only on the inactive side: left alone
        self.assertTrue((self.b / f"local_{uuid_of(7)}.json").exists())
        self.assertEqual(json.loads((self.b / f"local_{uuid_of(1)}.json").read_text())["title"], "In A")

    def test_the_direction_flips_when_the_other_account_is_the_loaded_one(self):
        self.copier.remember(A_ID, "org", str(self.a))
        put(self.b, f"local_{uuid_of(5)}.json", {"title": "Made in B"})
        report = self.copier.run(self.active(B_ID), {A_ID, B_ID}, snapshots=False)
        self.assertEqual(report["copied"], 1)
        self.assertEqual(listing(self.a), listing(self.b))

    def test_a_deletion_is_mirrored_only_with_desktops_marker_and_goes_to_trash(self):
        keep, gone, only_b = (f"local_{uuid_of(n)}.json" for n in (1, 2, 3))
        for name in (keep, gone):
            put(self.a, name, {"title": name})
        self.run_pass(snapshots=False)
        put(self.b, only_b, {"title": "only on the inactive side"})
        (self.a / gone).unlink()
        report = self.run_pass(snapshots=False)  # gone without a marker: not a deletion, just a missing file
        self.assertEqual((report["removed"], report["left_alone"]), (0, 2))
        self.assertTrue((self.b / gone).exists())
        put(self.a, f"deleted_{uuid_of(2)}", raw=b"1791000000000")
        report = self.run_pass(snapshots=False)
        self.assertEqual(report["removed"], 1)
        self.assertFalse((self.b / gone).exists())
        self.assertTrue((self.b / keep).exists() and (self.b / only_b).exists())
        trashed = [p.name for p in (self.home / "trash").iterdir()]
        self.assertEqual(len(trashed), 1)
        self.assertTrue(trashed[0].endswith("-" + gone))  # kept, never deleted for good
        self.assertTrue((self.b / f"deleted_{uuid_of(2)}").exists())  # the marker travels like any other file

    def test_deletion_mirroring_can_be_switched_off(self):
        name = f"local_{uuid_of(2)}.json"
        put(self.a, name, {"title": "x"})
        self.run_pass(snapshots=False)
        (self.a / name).unlink()
        put(self.a, f"deleted_{uuid_of(2)}", raw=b"1")
        report = self.run_pass(deletions=False, snapshots=False)
        self.assertEqual(report["removed"], 0)
        self.assertTrue((self.b / name).exists())
        self.assertFalse((self.home / "trash").exists())

    def test_halt_pauses_the_copy_and_the_pass_after_a_halt_copies_nothing(self):
        put(self.a, f"local_{uuid_of(1)}.json", {"title": "x"})
        self.halt = "halted"
        self.assertEqual(self.run_pass(), {"state": "halted"})
        self.assertEqual(listing(self.b), {})
        self.assertFalse(self.home.joinpath("snapshots").exists())
        self.halt = None
        self.assertEqual(self.run_pass()["copied"], 1)

    def test_halt_in_the_middle_of_a_pass_stops_before_the_next_write(self):
        for number in (1, 2, 3):
            put(self.a, f"local_{uuid_of(number)}.json", {"title": str(number)})
        calls = []

        def halted():
            calls.append(1)
            return "halted" if len(calls) > 3 else None  # the pass starts, then the owner halts
        self.copier.halted = halted
        report = self.run_pass(snapshots=False)
        self.assertLess(len(listing(self.b)), 3)
        self.assertIn(report["state"], ("ran", "halted"))


class FolderRuleTests(CopierBase):
    def test_a_folder_never_seen_loaded_is_never_written_to(self):
        other = self.root / "sessions" / C_ID / "org"
        other.mkdir(parents=True)  # it exists, but Desktop was never seen loading it
        put(self.a, f"local_{uuid_of(1)}.json", {"title": "x"})
        report = self.copier.run(self.active(), {A_ID, C_ID})
        self.assertEqual(report, {"state": "skipped", "reason": "no-other-linked-folder-seen"})
        self.assertEqual(list(other.iterdir()), [])

    def test_an_unlinked_active_account_copies_nothing(self):
        put(self.a, f"local_{uuid_of(1)}.json", {"title": "x"})
        report = self.copier.run(self.active(), {B_ID, C_ID})
        self.assertEqual(report, {"state": "skipped", "reason": "active-account-not-linked"})
        self.assertEqual(listing(self.b), {})

    def test_a_missing_folder_is_never_created_and_never_remembered(self):
        missing = self.root / "sessions" / C_ID / "org"
        self.assertFalse(self.copier.remember(C_ID, "org", str(missing)))
        self.assertNotIn(C_ID, self.copier.seen())
        self.copier.remember(C_ID, "org", str(self.root / "sessions" / C_ID / "org"))
        put(self.a, f"local_{uuid_of(1)}.json", {"title": "x"})
        self.copier.run(self.active(), {A_ID, C_ID})
        self.assertFalse(missing.exists())
        self.assertFalse(missing.parent.exists())

    def test_a_target_folder_that_vanished_after_it_was_seen_is_not_recreated(self):
        put(self.a, f"local_{uuid_of(1)}.json", {"title": "x"})
        import shutil
        shutil.rmtree(self.b)
        report = self.run_pass()
        self.assertEqual((report["state"], report["reason"]), ("skipped", "no-usable-target"))
        self.assertFalse(self.b.exists())
        self.assertTrue(any("STOP" in line and "missing" in line for line in self.said))
        self.run_pass()
        self.assertEqual(len([line for line in self.said if "STOP" in line]), 1)  # said once, not every pass

    def make_junction(self, link: Path, target: Path) -> bool:
        if os.name != "nt":
            try:
                os.symlink(target, link, target_is_directory=True)
                return True
            except OSError:
                return False
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        return done.returncode == 0

    def test_a_link_or_junction_in_place_of_a_folder_is_refused_both_ways(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        put(self.a, f"local_{uuid_of(1)}.json", {"title": "x"})
        link = self.root / "sessions" / B_ID / "linked"
        if not self.make_junction(link, elsewhere):
            self.skipTest("cannot make a junction or symlink here")
        self.assertTrue(cc.is_link(link))
        self.assertEqual(cc.usable_folder(link), "link")
        self.assertIsNone(cc.usable_folder(self.a))
        self.copier.remember(C_ID, "org", str(link))  # a link is never remembered as a seen folder
        self.assertNotIn(C_ID, self.copier.seen())
        self.copier.seen_path().write_text(json.dumps({"version": 1, "folders": {
            C_ID: {"org": "org", "folder": str(link)}}}))  # even a stale record naming it is not written to
        report = self.copier.run(self.active(), {A_ID, C_ID})
        self.assertEqual((report["state"], report["reason"]), ("skipped", "no-usable-target"))
        self.assertEqual(list(elsewhere.iterdir()), [])
        loaded_link = self.copier.run({"id": A_ID, "org": "org", "folder": str(link)}, {A_ID, B_ID})
        self.assertEqual(loaded_link, {"state": "skipped", "reason": "source-link"})
        self.assertEqual(list(elsewhere.iterdir()), [])
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True)  # removes the link only
        else:
            link.unlink()

    def test_the_target_may_not_be_the_loaded_folder(self):
        self.copier.remember(B_ID, "org", str(self.a))
        put(self.a, f"local_{uuid_of(1)}.json", {"title": "x"})
        report = self.run_pass()
        self.assertEqual((report["state"], report["reason"]), ("skipped", "no-usable-target"))


class SnapshotTests(CopierBase):
    def test_one_snapshot_per_day_holding_both_folders_and_taken_before_the_copy(self):
        put(self.a, f"local_{uuid_of(1)}.json", {"title": "x"})
        put(self.b, f"local_{uuid_of(2)}.json", {"title": "y"})
        report = self.run_pass()
        self.assertEqual(report["snapshot"], "written")
        zips = list((self.home / "snapshots").glob("chats-*.zip"))
        self.assertEqual(len(zips), 1)
        import zipfile
        names = zipfile.ZipFile(zips[0]).namelist()
        self.assertEqual(sorted(n.replace("\\", "/") for n in names),
                         sorted([f"{A_ID}/local_{uuid_of(1)}.json", f"{B_ID}/local_{uuid_of(2)}.json"]))  # pre-copy
        put(self.a, f"local_{uuid_of(3)}.json", {"title": "z"})
        self.assertEqual(self.run_pass()["snapshot"], "exists")

    def test_the_last_seven_snapshots_are_kept(self):
        snaps = self.home / "snapshots"
        snaps.mkdir(parents=True)
        for day in range(1, 10):
            (snaps / f"chats-2026010{day}.zip").write_bytes(b"old")
        put(self.a, f"local_{uuid_of(1)}.json", {"title": "x"})
        self.run_pass()
        kept = sorted(p.name for p in snaps.glob("chats-*.zip"))
        self.assertEqual(len(kept), cc.SNAPSHOTS_KEPT)
        self.assertNotIn("chats-20260101.zip", kept)
        self.assertTrue(any(name.startswith("chats-2026") and name > "chats-20260109.zip" for name in kept))

    def test_snapshots_can_be_switched_off(self):
        put(self.a, f"local_{uuid_of(1)}.json", {"title": "x"})
        report = self.run_pass(snapshots=False)
        self.assertNotIn("snapshot", report)
        self.assertFalse((self.home / "snapshots").exists())


class PersonalCopierTests(unittest.TestCase):
    """personal_copier_present reads only the folder it is given: never the real Startup folder."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chat-copy-startup-")
        self.addCleanup(temporary.cleanup)
        self.startup = Path(temporary.name)

    def shortcut(self, name, target=None, arguments=None, wide=True):
        """Stand-in bytes for a .lnk: its target and arguments appear as text, ANSI and UTF-16 as in the real format."""
        text = " ".join(item for item in (target, arguments) if item)
        (self.startup / name).write_bytes(b"L\x00\x00\x00" + text.encode("utf-16-le" if wide else "ascii"))

    def test_no_startup_folder_or_an_empty_one_is_absent(self):
        self.assertFalse(cc.personal_copier_present(self.startup / "nowhere"))
        self.assertFalse(cc.personal_copier_present(self.startup))

    def test_a_shortcut_that_names_the_personal_script_is_found_in_any_encoding(self):
        self.shortcut("Claude chat sync.lnk", r"C:\Python\pythonw.exe", r'-I "C:\Users\me\claude-chat-sync\chat_sync.py" --watch')
        self.assertTrue(cc.personal_copier_present(self.startup))
        (self.startup / "Claude chat sync.lnk").unlink()
        self.shortcut("Claude chat sync.lnk", r"C:\x\CHAT_SYNC.PY", wide=False)
        self.assertTrue(cc.personal_copier_present(self.startup))

    def test_other_shortcuts_and_files_do_not_count(self):
        self.shortcut("Crosstalk helper.lnk", r"C:\Python\pythonw.exe", r"crosstalk_helper.py --watch")
        (self.startup / "notes.txt").write_text("chat_sync.py")  # not a startup item
        self.assertFalse(cc.personal_copier_present(self.startup))

    def test_a_startup_script_that_runs_it_counts(self):
        (self.startup / "sync.cmd").write_text("pythonw chat_sync.py --watch")
        self.assertTrue(cc.personal_copier_present(self.startup))

    def test_the_real_folder_is_only_named_never_read_here(self):
        with mock.patch.dict(os.environ, {"APPDATA": str(self.startup / "AppData" / "Roaming")}):
            folder = cc.startup_folder()
            self.assertTrue(folder is None or str(folder).startswith(str(self.startup)))


class ChatCopyJobTests(HelperBase):
    """The helper job: default off, linked accounts, halt, folders seen loaded, and the personal copier guard."""

    def setUp(self):
        super().setUp()
        self.startup = self.root / "Startup"
        self.startup.mkdir()
        self.h.startup_dir = self.startup
        self.other = self.base / OTHER_ACCOUNT / ORG
        self.other.mkdir(parents=True)
        core.helper_dir().mkdir(parents=True, exist_ok=True)
        core._write_json_atomic(core.accounts_path(), {"version": 1, "emails": {ACCOUNT: "me@example.com"}})
        self.set_config({"chat_copy": True, "switch_history": ["me@example.com", OTHER_ACCOUNT]})
        self.copy_dir = core.helper_dir() / "chat-copy"
        for path in self.folder.glob("local_*.json"):  # make the fixture records old enough to be settled
            stamp = self.now - 60
            os.utime(path, (stamp, stamp))

    def set_config(self, value):
        (core.helper_dir() / "config.json").write_text(json.dumps(value))

    def both_loaded(self, active=ACCOUNT):
        first, second = (OTHER_ACCOUNT, ACCOUNT) if active == ACCOUNT else (ACCOUNT, OTHER_ACCOUNT)
        self.add_log(self.load_line(age_s=600, account=first), self.load_line(age_s=60, account=second))
        self.h.follow_log()

    def two_passes(self):
        self.advance(15)
        first = self.h.chat_copy()
        self.advance(15)
        return first, self.h.chat_copy()

    def other_names(self):
        return sorted(p.name for p in self.other.iterdir())

    def test_it_is_off_by_default_and_then_reads_and_writes_nothing(self):
        self.both_loaded()
        for config in ({}, {"chat_copy": False, "switch_history": [ACCOUNT, OTHER_ACCOUNT]},
                       {"chat_copy": "yes", "switch_history": [ACCOUNT, OTHER_ACCOUNT]}):
            self.set_config(config)
            with mock.patch.object(helper.chat_copying, "personal_copier_present", side_effect=AssertionError):
                self.assertEqual(self.h.chat_copy(), {"ran": False, "skipped": "config-off"})
        self.assertEqual(self.other_names(), [])
        self.assertFalse(self.copy_dir.exists())

    def test_it_copies_the_loaded_accounts_chats_after_two_agreeing_passes(self):
        self.both_loaded()
        first, second = self.two_passes()
        self.assertEqual(first, {"ran": False, "skipped": "account-settling"})
        self.assertTrue(second["ran"])
        self.assertEqual(second["copied"], 3)
        self.assertEqual(self.other_names(), sorted(p.name for p in self.folder.glob("local_*.json")))
        self.assertEqual(second["snapshot"], "written")
        self.assertTrue(list((self.copy_dir / "snapshots").glob("chats-*.zip")))

    def test_it_never_writes_the_loaded_folder_even_when_the_other_one_is_newer(self):
        self.both_loaded()
        put(self.other, "local_only-other-0000.json", {"title": "only in the other account"})
        before = {p.name: p.read_bytes() for p in self.folder.iterdir()}
        self.two_passes()
        self.assertEqual({p.name: p.read_bytes() for p in self.folder.iterdir()}, before)
        self.assertIn("local_only-other-0000.json", self.other_names())

    def test_the_tick_runs_it_and_reports_it(self):
        self.both_loaded()
        self.advance(15)
        self.assertEqual(self.h.tick()["chat_copy"]["skipped"], "account-settling")
        self.advance(15)
        self.assertEqual(self.h.tick()["chat_copy"]["copied"], 3)

    def test_halt_pauses_it_and_messaging_off_does_not(self):
        self.both_loaded()
        core.controls_update(halt=True)
        first, second = self.two_passes()
        self.assertEqual(second, {"ran": False, "skipped": "halted"})
        self.assertEqual(self.other_names(), [])
        core.controls_update(halt=False, messaging="off")  # messaging is for letters only
        self.assertTrue(self.two_passes()[1]["ran"])  # the first pass after a pause only re-reads the account
        self.assertEqual(len(self.other_names()), 3)

    def test_the_personal_copiers_startup_shortcut_blocks_it_with_one_log_line(self):
        self.both_loaded()
        (self.startup / "Claude chat sync.lnk").write_bytes(
            b"L\x00" + r'pythonw.exe C:\x\claude-chat-sync\chat_sync.py --watch'.encode("utf-16-le"))
        for _ in range(3):
            first, second = self.two_passes()
        self.assertEqual(second, {"ran": False, "skipped": "personal-copier-present"})
        self.assertEqual(self.other_names(), [])
        lines = [line for line in self.h.log.path.read_text().splitlines() if "personal-copier-present" in line]
        self.assertEqual(len(lines), 1)
        (self.startup / "Claude chat sync.lnk").unlink()  # the owner removes it: the job runs
        self.assertTrue(self.two_passes()[1]["ran"])

    def test_it_needs_two_linked_accounts_and_the_loaded_one_must_be_linked(self):
        self.both_loaded()
        self.set_config({"chat_copy": True, "switch_history": ["me@example.com"]})
        self.assertEqual(self.two_passes()[1], {"ran": False, "skipped": "needs-two-linked-accounts"})
        self.set_config({"chat_copy": True, "switch_history": [OTHER_ACCOUNT, "99999999-9999-4999-8999-999999999999"]})
        self.assertEqual(self.two_passes()[1], {"ran": False, "skipped": "active-account-not-linked"})
        self.assertEqual(self.other_names(), [])

    def test_a_folder_the_helper_never_saw_loaded_is_not_written_to(self):
        self.add_log(self.load_line(age_s=60, account=ACCOUNT))  # only this account was ever seen loading
        self.h.follow_log()
        report = self.two_passes()[1]
        self.assertEqual(report, {"ran": False, "skipped": "no-other-linked-folder-seen"})
        self.assertEqual(self.other_names(), [])

    def test_no_folder_is_created_for_an_account_whose_folder_is_missing(self):
        self.both_loaded()
        import shutil
        shutil.rmtree(self.other)
        report = self.two_passes()[1]
        self.assertFalse(report["ran"])
        self.assertFalse(self.other.exists())

    def test_the_direction_follows_an_account_switch(self):
        self.both_loaded(active=OTHER_ACCOUNT)
        put(self.other, "local_made-in-other-0000.json", {"title": "made in the other account"})
        put(self.other, "local_made-in-other-0000.json", {"title": "made in the other account"}, age_s=60)
        self.two_passes()
        self.assertTrue((self.folder / "local_made-in-other-0000.json").exists())

    def test_switches_for_deletions_and_snapshots_are_read_from_config(self):
        self.both_loaded()
        self.set_config({"chat_copy": True, "switch_history": [ACCOUNT, OTHER_ACCOUNT],
                         "chat_copy_snapshots": False, "chat_copy_deletions": False})
        report = self.two_passes()[1]
        self.assertNotIn("snapshot", report)
        gone = next(self.folder.glob("local_*.json"))
        chat = gone.name[len("local_"):-len(".json")]
        gone.unlink()
        put(self.folder, f"deleted_{chat}", raw=b"1")
        self.advance(15)
        self.assertEqual(self.h.chat_copy()["removed"], 0)
        self.assertTrue((self.other / gone.name).exists())
        self.set_config({"chat_copy": True, "switch_history": [ACCOUNT, OTHER_ACCOUNT]})  # absent = on
        self.advance(15)
        self.assertEqual(self.h.chat_copy()["removed"], 1)
        self.assertFalse((self.other / gone.name).exists())
        self.assertEqual(len(list((self.copy_dir / "trash").iterdir())), 1)

    def test_the_helper_source_never_makes_links_or_folders_for_desktop(self):
        for name in ("crosstalk_chat_copy.py",):
            source = (KIT / name).read_text(encoding="utf-8")
            for text in ("os.symlink", "mklink", "CreateSymbolicLink", "os.link(", "makedirs(dst", "mkdir(dst",
                         "makedirs(target", "makedirs(folder"):
                self.assertNotIn(text, source, text)
        source = (KIT / "crosstalk_chat_copy.py").read_text(encoding="utf-8")
        self.assertNotIn("os.remove(target", source)  # a record leaves the other side only into trash


if __name__ == "__main__":
    unittest.main()
