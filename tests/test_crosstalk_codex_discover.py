"""Offline tests: a Codex chat that never called a Crosstalk tool is found from its rollout's session_meta.

Fixture rollouts only; no real Codex data."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


KIT = Path(__file__).resolve().parents[1] / "tools" / "crosstalk"
spec = importlib.util.spec_from_file_location("crosstalk_codex_discover_core", KIT / "crosstalk_p0_core.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

THREAD = "01a11d20-8978-7802-a757-a8fadbd74ab2"
ACCOUNT = "d58aa15f-c2a4-4f11-a67d-5d5b40a0d006"


class CodexDiscoverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="crosstalk-codex-discover-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sessions = self.root / "sessions"
        env = mock.patch.dict(os.environ, {"CROSSTALK_P0_DEVICE_DIR": str(self.root / "device"),
                                           "CROSSTALK_P0_HERMES_HOME": str(self.root / "no-hermes"),
                                           "CROSSTALK_P0_CODEX_SESSIONS_DIR": str(self.sessions)})
        env.start()
        self.addCleanup(env.stop)

    def rollout(self, thread=THREAD, meta=None, row_type="session_meta"):
        folder = self.sessions / "2026" / "10" / "08"
        folder.mkdir(parents=True, exist_ok=True)
        payload = {"id": thread, "cwd": "C:\\work\\EUTONOS", "creator_account_id": ACCOUNT}
        payload.update(meta or {})
        path = folder / f"rollout-2026-10-08T15-07-14-{thread}.jsonl"
        path.write_text(json.dumps({"type": row_type, "payload": payload}) + "\n{\"type\":\"x\"}\n", encoding="utf-8")
        return path

    def test_exact_key_registers_from_session_meta(self):
        self.rollout()
        entry = core.resolve_recipient("codex:" + THREAD, "claude")
        self.assertEqual(entry["key"], "codex:" + THREAD)
        self.assertEqual(entry["account"]["id"], ACCOUNT)
        self.assertEqual(entry["project_folder"], "C:\\work\\EUTONOS")
        self.assertEqual(entry["source"], "rollout-discovery")

    def test_accountless_tool_call_registration_is_filled_from_rollout(self):
        self.rollout()
        caller = {"thread_id": THREAD}
        entry = core.register_codex(caller, {"cwd": "C:\\server"}, {"app": "codex", "id": None, "error": "Timeout"})
        self.assertEqual(entry["account"]["id"], ACCOUNT)
        self.assertEqual(entry["project_folder"], "C:\\work\\EUTONOS")
        # An entry left accountless (rollout appeared later) is filled at send time.
        core.directory_register("codex", THREAD, {"account": {"app": "codex", "id": None}})
        directory = core.directory_load()
        directory["entries"]["codex:" + THREAD].pop("account")
        core._write_json_atomic(core.device_dir() / "directory.json", directory)
        self.assertEqual(core.resolve_recipient("codex:" + THREAD, "claude")["account"]["id"], ACCOUNT)

    def test_bare_thread_id_registers(self):
        self.rollout()
        self.assertEqual(core.resolve_recipient(THREAD, "claude")["key"], "codex:" + THREAD)

    def test_missing_rollout_is_still_refused(self):
        with self.assertRaises(core.ProbeError) as raised:
            core.resolve_recipient("codex:" + THREAD, "claude")
        self.assertEqual(str(raised.exception), "recipient-not-in-directory")

    def test_mismatched_or_accountless_meta_registers_nothing(self):
        for meta, row_type in (({"id": "01a11d20-0000-7802-a757-a8fadbd74ab2"}, "session_meta"),
                               ({"creator_account_id": None}, "session_meta"),
                               ({}, "turn_context")):
            with self.subTest(meta=meta, row_type=row_type):
                path = self.rollout(meta=meta, row_type=row_type)
                self.assertEqual(core.codex_discover(THREAD)["registered"], 0)
                self.assertNotIn("codex:" + THREAD, core.directory_load()["entries"])
                path.unlink()

    def test_discovered_entry_keeps_codex_account_check(self):
        self.rollout()
        entry = core.resolve_recipient("codex:" + THREAD, "claude")
        other = {"app": "codex", "id": "99999999-2222-4333-8444-555555555555"}
        self.assertEqual(core.block_reason(entry, other), "blocked:other-account")
        self.assertIsNone(core.block_reason(entry, {"app": "codex", "id": ACCOUNT}))

    def test_codex_sender_cannot_reach_codex(self):
        self.rollout()
        with self.assertRaises(core.ProbeError) as raised:
            core.resolve_recipient("codex:" + THREAD, "codex")
        self.assertEqual(str(raised.exception), "same-app-recipient")


if __name__ == "__main__":
    unittest.main()
