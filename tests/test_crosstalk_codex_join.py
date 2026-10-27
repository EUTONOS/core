"""Offline tests: new Codex chats join the Crosstalk directory from Codex's state DB (read-only fixture DB)."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock


KIT = Path(__file__).resolve().parents[1] / "tools" / "crosstalk"
spec = importlib.util.spec_from_file_location("crosstalk_codex_join_core", KIT / "crosstalk_p0_core.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

ACCOUNT = "d58aa15f-c2a4-4f11-a67d-5d5b40a0d006"
NOW = int(time.time() * 1000)


def tid(n: int) -> str:
    return f"01a157bd-2660-7f90-ba60-{n:012d}"


class CodexJoinTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="crosstalk-codex-join-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "state_5.sqlite"
        env = mock.patch.dict(os.environ, {"CROSSTALK_P0_DEVICE_DIR": str(self.root / "device"),
                                           "CROSSTALK_P0_CODEX_STATE_DB": str(self.db)})
        env.start()
        self.addCleanup(env.stop)
        connection = sqlite3.connect(self.db)
        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, title TEXT, name TEXT, "
                           "archived INTEGER, created_at_ms INTEGER, updated_at_ms INTEGER, agent_role TEXT, "
                           "agent_path TEXT, thread_source TEXT, source TEXT, creator_account_id TEXT)")
        connection.commit()
        connection.close()

    def add(self, n, title="t", name=None, archived=0, updated=None, source="vscode", thread_source="user",
            account=ACCOUNT, role=None, cwd="\\\\?\\C:\\work\\Toy"):
        connection = sqlite3.connect(self.db)
        connection.execute("INSERT OR REPLACE INTO threads VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (tid(n), cwd, title, name, archived, NOW - 1000, updated or NOW, role, None,
                            thread_source, source, account))
        connection.commit()
        connection.close()

    def entries(self):
        return core.directory_load()["entries"]

    def test_new_thread_registers_with_fields(self):
        self.add(1, name="Camis Toy Store image chat")
        self.assertEqual(core.codex_join_scan()["registered"], 1)
        entry = self.entries()["codex:" + tid(1)]
        self.assertEqual(entry["title"], "Camis Toy Store image chat")
        self.assertEqual(entry["project_folder"], "C:\\work\\Toy")
        self.assertEqual(entry["account"]["id"], ACCOUNT)
        self.assertEqual(entry["source"], "codex-thread-discovery")

    def test_excluded_threads_are_not_added(self):
        self.add(2, archived=1)
        self.add(3, thread_source="guardian_review")
        self.add(4, thread_source="subagent")
        self.add(5, source="exec")
        self.add(6, account=None)
        self.add(7, role="worker")
        self.add(8, thread_source=None)  # Crosstalk-created chats have no thread_source
        core.codex_join_scan()
        self.assertEqual(sorted(self.entries()), ["codex:" + tid(8)])

    def test_missing_locked_or_bad_schema_db_never_raises(self):
        self.db.unlink()
        self.assertEqual(core.codex_join_scan()["registered"], 0)
        self.db.write_bytes(b"not a sqlite file at all" * 10)
        self.assertIn("join-error", core.codex_join_scan().get("note", ""))
        self.db.unlink()
        connection = sqlite3.connect(self.db)
        connection.execute("CREATE TABLE threads (id TEXT)")
        connection.commit()
        connection.close()
        self.assertIn("join-error", core.codex_join_scan().get("note", ""))

    def test_title_refresh_and_watermark(self):
        self.add(1, title="first", updated=NOW)
        core.codex_join_scan()
        before = self.entries()["codex:" + tid(1)]["registrations"]
        self.assertEqual(core.codex_join_scan(), {"read": True, "registered": 0, "refreshed": 0})
        self.add(1, title="first", name="Renamed", updated=NOW + 5000)
        self.assertEqual(core.codex_join_scan()["refreshed"], 1)
        self.assertEqual(self.entries()["codex:" + tid(1)]["title"], "Renamed")
        # An update with unchanged fields does not rewrite the entry.
        self.add(1, title="first", name="Renamed", updated=NOW + 9000)
        self.assertEqual(core.codex_join_scan()["refreshed"], 0)
        self.assertEqual(self.entries()["codex:" + tid(1)]["registrations"], before + 1)

    def test_first_run_backfills_only_seven_days(self):
        self.add(1, updated=NOW - 8 * 24 * 3600 * 1000)
        self.add(2, updated=NOW - 3600 * 1000)
        core.codex_join_scan()
        self.assertEqual(sorted(self.entries()), ["codex:" + tid(2)])

    def test_existing_unnamed_entry_gets_title(self):
        core.directory_register("codex", tid(1), {"title": None, "account": {"app": "codex", "id": ACCOUNT}})
        self.add(1, title="From DB")
        core.codex_join_scan()
        self.assertEqual(self.entries()["codex:" + tid(1)]["title"], "From DB")


if __name__ == "__main__":
    unittest.main()
