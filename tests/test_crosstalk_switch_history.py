"""Offline tests: keeping Remote Control history across a Claude Desktop account switch (slice 3B.16).

Synthetic transcripts in a temporary folder only; no real chat, process scan or app data."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


KIT = Path(__file__).resolve().parents[1] / "tools" / "crosstalk"
spec = importlib.util.spec_from_file_location("crosstalk_switch_history_under_test", KIT / "crosstalk_switch_history.py")
sh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sh)

CLI = "11111111-2222-4333-8444-555555555555"
CLI_ORG = "11111111-0000-4333-8444-555555555555"
DESK = "22222222-2222-4333-8444-555555555555"
DESK_ORG = "22222222-0000-4333-8444-555555555555"
SESSION = "33333333-2222-4333-8444-555555555555"


def row(value: dict) -> bytes:
    return (json.dumps(value, separators=(",", ":")) + "\n").encode()


def pointer(bridge: str, owner: str | None = CLI, org: str = CLI_ORG, flag: bool = False) -> bytes:
    value = {"type": "bridge-session", "sessionId": SESSION, "bridgeSessionId": bridge, "lastSequenceNum": 4}
    if flag:
        value["noHistoryBackfill"] = True
    if owner:
        value.update(ownerAccountUuid=owner, ownerOrganizationUuid=org)
    return row(value)


MESSAGE = b'{"type":"user","message":{"content":"history-suppression is just text here"}}\n'
MARK = row({"type": "history-suppression", "cause": "restored_owner_mismatch"})


class StampTests(unittest.TestCase):
    def test_switch_away_from_the_cli_login_stamps_the_new_desktop_account(self):
        self.assertEqual(sh.choose_stamp(CLI, CLI, (DESK, DESK_ORG), (CLI, CLI_ORG)), (DESK, DESK_ORG))

    def test_switch_to_the_cli_login_stamps_the_previous_account(self):
        self.assertEqual(sh.choose_stamp(CLI, CLI, (CLI, CLI_ORG), (DESK, DESK_ORG)), (DESK, DESK_ORG))

    def test_owner_already_differing_from_the_login_is_left_alone(self):
        # e.g. the owner signed the CLI in to the new account: Claude Code mints fresh by itself
        self.assertIsNone(sh.choose_stamp(CLI, DESK, (DESK, DESK_ORG), (CLI, CLI_ORG)))

    def test_unknown_login_or_owner_or_no_other_account_stamps_nothing(self):
        self.assertIsNone(sh.choose_stamp(CLI, None, (DESK, DESK_ORG), None))
        self.assertIsNone(sh.choose_stamp(None, CLI, (DESK, DESK_ORG), None))
        self.assertIsNone(sh.choose_stamp(CLI, CLI, (CLI, CLI_ORG), None))

    def test_newest_owner_skips_the_empty_remote_off_row(self):
        lines = [pointer("cse_a", DESK, DESK_ORG), row({"type": "bridge-session", "sessionId": SESSION,
                                                         "bridgeSessionId": "", "lastSequenceNum": 0})]
        self.assertEqual(sh.newest_pointer_owner(lines), DESK)


class PlanTests(unittest.TestCase):
    def test_drops_marks_clears_flags_restamps_and_keeps_other_lines_byte_identical(self):
        empty = row({"type": "bridge-session", "sessionId": SESSION, "bridgeSessionId": "", "lastSequenceNum": 0})
        lines = [MESSAGE, MARK, pointer("cse_a", flag=True), empty, b'{"type":"assistant"}\r\n', pointer("cse_b")]
        kept, counts = sh.plan(lines, (DESK, DESK_ORG))
        self.assertEqual(counts, {"dropped": 1, "cleared": 1, "restamped": 2})
        self.assertEqual(kept[0], MESSAGE)
        self.assertEqual(kept[2], empty)
        self.assertEqual(kept[3], b'{"type":"assistant"}\r\n')
        self.assertEqual(json.loads(kept[1]), json.loads(pointer("cse_a", DESK, DESK_ORG)))
        self.assertEqual(kept[4], pointer("cse_b", DESK, DESK_ORG))

    def test_without_a_stamp_owners_stay(self):
        kept, counts = sh.plan([pointer("cse_a", flag=True)], None)
        self.assertEqual(counts, {"dropped": 0, "cleared": 1, "restamped": 0})
        self.assertEqual(kept, [pointer("cse_a")])


class RestoreChatTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="crosstalk-switch-history-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.transcript = self.root / f"{SESSION}.jsonl"
        self.backups = self.root / "backups"

    def restore(self, running=lambda sid: False):
        return sh.restore_chat(self.transcript, SESSION, CLI, (DESK, DESK_ORG), (CLI, CLI_ORG), self.backups, running)

    def test_cleans_with_a_backup_and_a_second_run_changes_nothing(self):
        original = MESSAGE + MARK + pointer("cse_a", flag=True)
        self.transcript.write_bytes(original)
        result = self.restore()
        self.assertEqual(result["outcome"], "cleaned")
        self.assertEqual(result["stamp"], DESK)
        self.assertEqual(Path(result["backup"]).read_bytes(), original)
        self.assertEqual(self.transcript.read_bytes(), MESSAGE + pointer("cse_a", DESK, DESK_ORG))
        self.assertEqual(self.restore()["outcome"], "unchanged")

    def test_a_running_chat_is_never_touched(self):
        original = MARK + pointer("cse_a")
        self.transcript.write_bytes(original)
        self.assertEqual(self.restore(running=lambda sid: True)["outcome"], "skipped:running")
        self.assertEqual(self.transcript.read_bytes(), original)
        self.assertFalse(self.backups.exists())

    def test_a_chat_that_starts_or_writes_meanwhile_is_left_alone(self):
        original = MARK + pointer("cse_a")
        self.transcript.write_bytes(original)
        calls = []

        def running(sid):
            calls.append(sid)
            if len(calls) == 2:  # the chat appended a line between the read and the write
                with self.transcript.open("ab") as stream:
                    stream.write(MESSAGE)
            return False

        self.assertEqual(self.restore(running)["outcome"], "skipped:changed-meanwhile")
        self.assertEqual(self.transcript.read_bytes(), original + MESSAGE)

    def test_keeps_only_the_newest_backups(self):
        for stamp in ("20260101T000000", "20260102T000000", "20260103T000000"):
            self.backups.mkdir(exist_ok=True)
            (self.backups / f"{SESSION}.{stamp}.jsonl").write_bytes(b"old")
        self.transcript.write_bytes(MARK)
        self.restore()
        self.assertEqual(len(list(self.backups.glob(f"{SESSION}.*.jsonl"))), sh.BACKUPS_KEPT)


if __name__ == "__main__":
    unittest.main()
