import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from eutonos import boards, closeout_route, ram, tokenslang as ts, workflow, write_batch, write_retry
from eutonos.closeout_organization import PROFILE as ORGANIZATION
from eutonos.effects import Coordinator, LocalRoot, digest
from eutonos.host import connect, enroll, selected_apply, status as host_status


class WriteRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="write-retry-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.root = self.home / "repo"; self.root.mkdir()
        (self.root / "docs").mkdir()
        self.host = self.home / "host"
        self.actor = "writer"

    def git(self, *args):
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        return subprocess.check_output(["git", "-c", "safe.directory=" + str(self.root), "-C", str(self.root), *args],
                                       env=env, stderr=subprocess.PIPE)

    def initialize_git(self):
        self.git("init", "-q", "-b", "main"); self.git("add", "--all")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "baseline")

    def blocker(self, paths):
        coordinator, authority = connect(self.root, self.host, self.actor)
        generation = coordinator.start(authority, "blocker")
        coordinator.reserve(authority, "blocker", generation, paths)
        return coordinator, authority, generation

    def release_blocker(self, coordinator, authority, generation, paths):
        coordinator.closeout(authority, "blocker", generation,
            {path: digest(authority.root.read(path)) for path in paths},
            {"completed": "Concurrent fixture complete.", "next": "Queued writer may continue.", "pending": None})

    def test_connection_initialization_is_serialized(self):
        self.host.mkdir()
        real_connect = sqlite3.connect
        real_path = write_retry._db_path
        first_waiting = threading.Event()
        second_attempted = threading.Event()
        release_first = threading.Event()
        second_opened = threading.Event()
        counts = {"paths": 0, "connections": 0}
        counter_lock = threading.Lock()

        def db_path(host):
            with counter_lock:
                counts["paths"] += 1
                if counts["paths"] == 2:
                    second_attempted.set()
            return real_path(host)

        class GatedConnection(sqlite3.Connection):
            def execute(connection, sql, *args, **kwargs):
                if sql == "PRAGMA journal_mode=WAL" and connection.first:
                    first_waiting.set()
                    if not release_first.wait(5):
                        raise AssertionError("first initialization was not released")
                return super().execute(sql, *args, **kwargs)

        def connect_db(*args, **kwargs):
            db = real_connect(*args, **kwargs, factory=GatedConnection)
            with counter_lock:
                counts["connections"] += 1
                db.first = counts["connections"] == 1
                if not db.first:
                    second_opened.set()
            return db

        def initialize():
            with closing(write_retry._connect(self.host)) as db:
                return db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()

        with mock.patch.object(write_retry, "_db_path", side_effect=db_path), \
                mock.patch.object(write_retry.sqlite3, "connect", side_effect=connect_db):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(initialize)
                try:
                    self.assertTrue(first_waiting.wait(5))
                    second = pool.submit(initialize)
                    self.assertTrue(second_attempted.wait(5))
                    self.assertFalse(second_opened.wait(0.2), "second connection bypassed initialization lock")
                finally:
                    release_first.set()
                for future in (first, second):
                    self.assertEqual({row[0] for row in future.result(timeout=5)},
                                     {"requests", "worker", "batch_bindings"})
        self.assertTrue(second_opened.is_set())

    def test_initialization_failure_closes_connection_and_preserves_exception(self):
        self.host.mkdir()
        real_connect = sqlite3.connect
        for stage in ("wal", "schema", "interrupt"):
            with self.subTest(stage=stage):
                failure = KeyboardInterrupt() if stage == "interrupt" else sqlite3.OperationalError("injected " + stage)
                opened = []

                class FailingConnection(sqlite3.Connection):
                    def execute(connection, sql, *args, **kwargs):
                        if sql == "PRAGMA journal_mode=WAL" and stage in {"wal", "interrupt"}:
                            raise failure
                        return super().execute(sql, *args, **kwargs)

                    def executescript(connection, sql):
                        if stage == "schema":
                            raise failure
                        return super().executescript(sql)

                def connect_db(*args, **kwargs):
                    db = real_connect(*args, **kwargs, factory=FailingConnection)
                    opened.append(db)
                    return db

                with mock.patch.object(write_retry.sqlite3, "connect", side_effect=connect_db):
                    with self.assertRaises(type(failure)) as caught:
                        write_retry._connect(self.host)
                self.assertIs(caught.exception, failure)
                self.assertEqual(len(opened), 1)
                with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
                    sqlite3.Connection.execute(opened[0], "SELECT 1")
                # Windows rejects this unlink if the failed constructor leaked its handle.
                write_retry._db_path(self.host).unlink()

    def test_initialization_lock_is_released_while_connection_remains_open(self):
        self.host.mkdir()
        with closing(write_retry._connect(self.host)) as db:
            def acquire_again():
                with write_retry.local_lock(self.host, timeout=0):
                    return True
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertTrue(pool.submit(acquire_again).result(timeout=5))
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_closeout_replans_after_owner_is_released(self):
        (self.root / "docs/CURRENT_TASK.md").write_text("# Current task\n\nStill active.\n", encoding="utf-8")
        self.initialize_git()
        values = {"catalog_changes": [], "work_changes": [], "organization": {
            "profile": ORGANIZATION, "title": "Queued closeout", "summary": "Record a verified change.",
            "disposition": "completed", "record_change": True, "module": "workflow", "area": "closeout"}}
        context = {"root": str(self.root), "host": str(self.host), "actor": self.actor}
        prepared = closeout_route.prepare(values, context)
        enroll(self.root, self.host, self.actor, prepared["allowed_paths"], shared=True)
        coordinator, authority, generation = self.blocker(prepared["allowed_paths"])
        with mock.patch("eutonos.write_retry.start_worker", return_value={"outcome": "not-started"}):
            result = closeout_route.run(values, context)
        self.assertEqual((result["outcome"], result["organization"]["outcome"]), ("deferred", "queued"), result)
        self.release_blocker(coordinator, authority, generation, prepared["allowed_paths"])
        applied = write_retry.run_once(self.host, self.root)
        self.assertEqual(applied["outcome"], "complete", applied)
        self.assertIn("Queued closeout", (self.root / "docs/CHANGELOG.md").read_text(encoding="utf-8"))
        self.assertEqual(write_retry.status(self.host, self.root)["signal"], "green")

    def test_closeout_organization_self_leases_on_fresh_host(self):
        (self.root / "docs/CURRENT_TASK.md").write_text("# Current task\n\nStill active.\n", encoding="utf-8")
        (self.root / "docs/bootstrap.md").write_text("Host only.\n", encoding="utf-8")
        self.initialize_git()
        enroll(self.root, self.host, self.actor, ["docs/bootstrap.md"], shared=True)
        values = {"catalog_changes": [], "work_changes": [], "organization": {
            "profile": ORGANIZATION, "title": "Fresh host closeout", "summary": "Record through an exact lease.",
            "disposition": "completed", "record_change": True, "module": "workflow", "area": "closeout"}}
        context = {"root": str(self.root), "host": str(self.host), "actor": self.actor}
        result = closeout_route.run(values, context)
        self.assertEqual((result["outcome"], result["organization"]["outcome"]), ("completed", "completed"), result)
        self.assertEqual(host_status(self.root, self.host, self.actor)["grants"]["temporary"], 0)

    def test_board_update_replans_and_preserves_concurrent_item(self):
        board = {"profile": boards.BOARD, "items": [{"id": "WORK-1", "type": "quick", "title": "Work",
                 "summary": "Original work.", "status": "active"}]}
        owner = "docs/WORK_ITEMS.md"
        (self.root / "eutonos.boards.json").write_text(json.dumps({"profile": boards.CONFIG, "owners": [owner]}), encoding="utf-8")
        (self.root / owner).write_text("# Work\n\n```tokenslang-board\n" + json.dumps(board) + "\n```\n", encoding="utf-8")
        self.initialize_git()
        enroll(self.root, self.host, self.actor, [owner], shared=True)
        coordinator, authority, generation = self.blocker([owner])
        with mock.patch("eutonos.write_retry.start_worker", return_value={"outcome": "not-started"}):
            queued = write_retry.submit_board_update(self.host, self.root, self.actor,
                {"owner_path": owner, "item_id": "WORK-1", "changes": {"status": "completed"}})
        self.assertEqual(queued["outcome"], "queued", queued)
        self.assertEqual(host_status(self.root, self.host, self.actor)["grants"]["temporary"], 0)
        before = (self.root / owner).read_bytes()
        added = boards.plan_add(self.root, {"profile": boards.ADD, "owner_path": owner,
            "expected_sha256": hashlib.sha256(before).hexdigest(), "index": 1,
            "item": {"id": "OTHER", "type": "quick", "title": "Concurrent", "summary": "Preserve me.", "status": "active"}})
        selected_apply(coordinator, authority, "blocker", generation, "concurrent-add", added["effect"], added["fingerprint"])
        self.release_blocker(coordinator, authority, generation, [owner])
        applied = write_retry.run_once(self.host, self.root)
        self.assertEqual(applied["outcome"], "complete", applied)
        items = {item["id"]: item for item in boards.project(self.root)["items"]}
        self.assertEqual(items["WORK-1"]["status"], "completed")
        self.assertEqual(items["OTHER"]["summary"], "Preserve me.")

    def test_board_update_self_leases_without_retained_board_grant(self):
        owner = "docs/WORK_ITEMS.md"
        board = {"profile": boards.BOARD, "items": [{"id": "WORK-1", "type": "quick", "title": "Work",
                 "summary": "Original work.", "status": "active"}]}
        (self.root / "eutonos.boards.json").write_text(json.dumps({"profile": boards.CONFIG, "owners": [owner]}), encoding="utf-8")
        (self.root / owner).write_text("# Work\n\n```tokenslang-board\n" + json.dumps(board) + "\n```\n", encoding="utf-8")
        (self.root / "docs/bootstrap.md").write_text("Host only.\n", encoding="utf-8")
        self.initialize_git()
        enroll(self.root, self.host, self.actor, ["docs/bootstrap.md"], shared=True)
        result = write_retry.submit_board_update(self.host, self.root, self.actor,
            {"owner_path": owner, "item_id": "WORK-1", "changes": {"status": "completed"}})
        self.assertEqual(result["outcome"], "complete", result)
        self.assertEqual(boards.project(self.root)["items"][0]["status"], "completed")
        self.assertEqual(connect(self.root, self.host, self.actor)[0].inspect(
            "write-retry-board-" + result["request_id"].removeprefix("write-"))["reservations"], [])
        self.assertEqual(host_status(self.root, self.host, self.actor)["grants"]["temporary"], 0)

    def test_batch_operation_id_cannot_be_reused_for_different_work(self):
        owner = "docs/WORK_ITEMS.md"
        board = {"profile": boards.BOARD, "items": [
            {"id": "WORK-1", "type": "quick", "title": "One", "summary": "First.", "status": "active"},
            {"id": "WORK-2", "type": "quick", "title": "Two", "summary": "Second.", "status": "active"}]}
        (self.root / "eutonos.boards.json").write_text(json.dumps({"profile": boards.CONFIG, "owners": [owner]}), encoding="utf-8")
        (self.root / owner).write_text("# Work\n\n```tokenslang-board\n" + json.dumps(board) + "\n```\n", encoding="utf-8")
        (self.root / "docs/bootstrap.md").write_text("Host only.\n", encoding="utf-8")
        self.initialize_git()
        enroll(self.root, self.host, self.actor, ["docs/bootstrap.md"], shared=True)
        owner_sha = digest((self.root / owner).read_bytes())
        first = {"profile": write_batch.PROFILE, "operation_id": "same-logical-request",
                 "operations": [{"kind": "update", "request": {"owner_path": owner, "item_id": "WORK-1",
                                  "expected_sha256": owner_sha, "changes": {"status": "completed"}}}]}
        second = {"profile": write_batch.PROFILE, "operation_id": "same-logical-request",
                  "operations": [{"kind": "update", "request": {"owner_path": owner, "item_id": "WORK-2",
                                   "expected_sha256": owner_sha, "changes": {"status": "completed"}}}]}
        write_retry.enqueue(self.host, self.root, self.actor, "batch", {"batch": first})
        with self.assertRaises(ts.ReadError):
            write_retry.enqueue(self.host, self.root, self.actor, "batch", {"batch": second})

    def test_batch_submission_migrates_existing_queue_database(self):
        owner = "docs/WORK_ITEMS.md"
        board = {"profile": boards.BOARD, "items": [{"id": "WORK-1", "type": "quick",
                 "title": "One", "summary": "First.", "status": "active"}]}
        (self.root / "eutonos.boards.json").write_text(
            json.dumps({"profile": boards.CONFIG, "owners": [owner]}), encoding="utf-8")
        (self.root / owner).write_text(
            "# Work\n\n```tokenslang-board\n" + json.dumps(board) + "\n```\n", encoding="utf-8")
        self.initialize_git()
        enroll(self.root, self.host, self.actor, [owner], shared=True)
        legacy = self.host / write_retry.DATABASE
        with closing(sqlite3.connect(legacy)) as db, db:
            db.executescript("""
                CREATE TABLE requests(
                    id TEXT PRIMARY KEY, root TEXT NOT NULL, actor TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL, error TEXT, result TEXT
                );
                CREATE TABLE worker(
                    root TEXT PRIMARY KEY, state TEXT NOT NULL, claim TEXT,
                    request_id TEXT, stage TEXT, heartbeat REAL NOT NULL, error TEXT
                );
            """)
        batch = {"profile": write_batch.PROFILE, "operation_id": "legacy-host-migration",
                 "operations": [{"kind": "update", "request": {"owner_path": owner,
                  "expected_sha256": digest((self.root / owner).read_bytes()), "item_id": "WORK-1",
                  "changes": {"status": "completed"}}}]}
        result = write_retry.submit_batch(self.host, self.root, self.actor, batch)
        self.assertEqual(result["outcome"], "complete", result)
        self.assertEqual(boards.project(self.root)["items"][0]["status"], "completed")
        with closing(sqlite3.connect(legacy)) as db:
            table = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='batch_bindings'").fetchone()
        self.assertEqual(table, ("batch_bindings",))

    def test_batch_operation_id_binds_claimed_provenance(self):
        owner = "docs/WORK_ITEMS.md"
        board = {"profile": boards.BOARD, "items": [{"id": "WORK-1", "type": "quick", "title": "One",
                 "summary": "First.", "status": "active"}]}
        (self.root / "eutonos.boards.json").write_text(json.dumps({"profile": boards.CONFIG, "owners": [owner]}), encoding="utf-8")
        (self.root / owner).write_text("# Work\n\n```tokenslang-board\n" + json.dumps(board) + "\n```\n", encoding="utf-8")
        self.initialize_git()
        enroll(self.root, self.host, self.actor, [owner], shared=True)
        owner_sha = digest((self.root / owner).read_bytes())
        base = {"profile": write_batch.PROFILE, "operation_id": "claimed-once",
                "operations": [{"kind": "update", "request": {"owner_path": owner,
                 "expected_sha256": owner_sha, "item_id": "WORK-1", "changes": {"status": "completed"}}}]}
        write_retry.enqueue(self.host, self.root, self.actor, "batch",
                            {"batch": {**base, "requested_by": {"kind": "model", "value": "terra"}}})
        with self.assertRaises(ts.ReadError):
            write_retry.enqueue(self.host, self.root, self.actor, "batch",
                                {"batch": {**base, "requested_by": {"kind": "model", "value": "sol"}}})

    def test_pruned_batch_request_keeps_durable_operation_binding(self):
        owner = "docs/WORK_ITEMS.md"
        board = {"profile": boards.BOARD, "items": [
            {"id": "WORK-1", "type": "quick", "title": "One", "summary": "First.", "status": "active"},
            {"id": "WORK-2", "type": "quick", "title": "Two", "summary": "Second.", "status": "active"}]}
        (self.root / "eutonos.boards.json").write_text(
            json.dumps({"profile": boards.CONFIG, "owners": [owner]}), encoding="utf-8")
        (self.root / owner).write_text(
            "# Work\n\n```tokenslang-board\n" + json.dumps(board) + "\n```\n", encoding="utf-8")
        (self.root / "docs/bootstrap.md").write_text("Host only.\n", encoding="utf-8")
        self.initialize_git()
        enroll(self.root, self.host, self.actor, ["docs/bootstrap.md"], shared=True)
        operation_id = "survives-terminal-pruning"
        first = {"profile": write_batch.PROFILE, "operation_id": operation_id,
                 "operations": [{"kind": "update", "request": {"owner_path": owner,
                  "expected_sha256": digest((self.root / owner).read_bytes()), "item_id": "WORK-1",
                  "changes": {"status": "completed"}}}]}
        completed = write_retry.submit_batch(self.host, self.root, self.actor, first)
        self.assertEqual(completed["outcome"], "complete", completed)
        with closing(write_retry._connect(self.host)) as db, db:
            db.execute("DELETE FROM requests WHERE id=?", (completed["request_id"],))
            binding = db.execute("SELECT id FROM batch_bindings WHERE id=?",
                                 (completed["request_id"],)).fetchone()
        self.assertEqual(binding, (completed["request_id"],))
        coordinator, _ = connect(self.root, self.host, self.actor)
        with coordinator._db() as db:
            effects_before = db.execute("SELECT COUNT(*) FROM effects WHERE attempt=?",
                                        (completed["request_id"],)).fetchone()[0]
        repeated = write_retry.submit_batch(self.host, self.root, self.actor, first)
        self.assertEqual((repeated["outcome"], repeated["state"]), ("complete", "complete"), repeated)
        self.assertEqual(repeated["result"]["reason"], "recovered")
        self.assertEqual(boards.project(self.root)["items"][0]["status"], "completed")
        self.assertEqual(host_status(self.root, self.host, self.actor)["grants"]["temporary"], 0)
        with coordinator._db() as db:
            effects_after = db.execute("SELECT COUNT(*) FROM effects WHERE attempt=?",
                                       (completed["request_id"],)).fetchone()[0]
        self.assertEqual(effects_after, effects_before)
        different = {"profile": write_batch.PROFILE, "operation_id": operation_id,
                     "operations": [{"kind": "update", "request": {"owner_path": owner,
                      "expected_sha256": digest((self.root / owner).read_bytes()), "item_id": "WORK-2",
                      "changes": {"status": "completed"}}}]}
        with self.assertRaises(ts.ReadError):
            write_retry.enqueue(self.host, self.root, self.actor, "batch", {"batch": different})

    def test_batch_form_returns_selected_owner_hash_and_item_revisions(self):
        owner = "docs/WORK_ITEMS.md"
        board = {"profile": boards.BOARD, "items": [
            {"id": "WORK-1", "type": "quick", "title": "One", "summary": "First.", "status": "active"},
            {"id": "WORK-2", "type": "quick", "title": "Two", "summary": "Second.", "status": "planned"}]}
        (self.root / "eutonos.boards.json").write_text(
            json.dumps({"profile": boards.CONFIG, "owners": [owner]}), encoding="utf-8")
        (self.root / owner).write_text(
            "# Work\n\n```tokenslang-board\n" + json.dumps(board) + "\n```\n", encoding="utf-8")
        result = write_batch.form(self.root, {"owner_paths": [owner], "item_ids": ["WORK-2"]})
        self.assertEqual(result["outcome"], "form")
        self.assertEqual(result["owners"], [{"path": owner, "status": "available",
            "sha256": digest((self.root / owner).read_bytes()),
            "request_defaults": {"owner_path": owner,
                                 "expected_sha256": digest((self.root / owner).read_bytes())}}])
        self.assertEqual([item["id"] for item in result["items"]], ["WORK-2"])
        self.assertIsInstance(result["items"][0]["revision"], str)
        self.assertTrue(result["items"][0]["revision"])

    def test_busy_batch_retries_as_one_unit(self):
        owner = "docs/WORK_ITEMS.md"
        board = {"profile": boards.BOARD, "items": [
            {"id": "WORK-1", "type": "quick", "title": "One", "summary": "First.", "status": "active"},
            {"id": "WORK-2", "type": "quick", "title": "Two", "summary": "Second.", "status": "active"}]}
        (self.root / "eutonos.boards.json").write_text(json.dumps({"profile": boards.CONFIG, "owners": [owner]}), encoding="utf-8")
        (self.root / owner).write_text("# Work\n\n```tokenslang-board\n" + json.dumps(board) + "\n```\n", encoding="utf-8")
        self.initialize_git()
        enroll(self.root, self.host, self.actor, [owner], shared=True)
        coordinator, authority, generation = self.blocker([owner])
        owner_sha = digest((self.root / owner).read_bytes())
        batch = {"profile": write_batch.PROFILE, "operations": [
            {"kind": "update", "request": {"owner_path": owner, "expected_sha256": owner_sha,
                                               "item_id": "WORK-1", "changes": {"status": "completed"}}},
            {"kind": "update", "request": {"owner_path": owner, "expected_sha256": owner_sha,
                                               "item_id": "WORK-2", "changes": {"status": "completed"}}}]}
        with mock.patch("eutonos.write_retry.start_worker", return_value={"outcome": "not-started"}):
            queued = write_retry.submit_batch(self.host, self.root, self.actor, batch)
        self.assertEqual(queued["outcome"], "queued", queued)
        self.assertEqual([item["status"] for item in boards.project(self.root)["items"]], ["active", "active"])
        self.assertEqual(host_status(self.root, self.host, self.actor)["grants"]["temporary"], 0)
        self.release_blocker(coordinator, authority, generation, [owner])
        with mock.patch.object(write_retry, "_now", return_value=write_retry._now() + 120):
            applied = write_retry.run_once(self.host, self.root)
        self.assertEqual(applied["outcome"], "complete", applied)
        self.assertEqual([item["status"] for item in boards.project(self.root)["items"]], ["completed", "completed"])

    def test_applied_batch_recovers_before_replanning_and_retires_lease(self):
        owner = "docs/WORK_ITEMS.md"
        board = {"profile": boards.BOARD, "items": [{"id": "WORK-1", "type": "quick", "title": "One",
                 "summary": "First.", "status": "active"}]}
        (self.root / "eutonos.boards.json").write_text(json.dumps({"profile": boards.CONFIG, "owners": [owner]}), encoding="utf-8")
        (self.root / owner).write_text("# Work\n\n```tokenslang-board\n" + json.dumps(board) + "\n```\n", encoding="utf-8")
        (self.root / "docs/bootstrap.md").write_text("Host only.\n", encoding="utf-8")
        self.initialize_git()
        enroll(self.root, self.host, self.actor, ["docs/bootstrap.md"], shared=True)
        batch = {"profile": write_batch.PROFILE, "operation_id": "recover-after-apply",
                 "operations": [{"kind": "update", "request": {"owner_path": owner,
                  "expected_sha256": digest((self.root / owner).read_bytes()), "item_id": "WORK-1",
                  "changes": {"status": "completed"}}}]}
        original = Coordinator.closeout
        def interrupt(instance, authority, attempt, generation, expected, checkpoint):
            if attempt.startswith("write-batch-"):
                raise RuntimeError("simulated crash before receipt")
            return original(instance, authority, attempt, generation, expected, checkpoint)
        with mock.patch.object(Coordinator, "closeout", new=interrupt):
            first = write_retry.submit_batch(self.host, self.root, self.actor, batch)
        self.assertEqual(first["outcome"], "error", first)
        self.assertEqual(boards.project(self.root)["items"][0]["status"], "completed")
        recovered = write_retry.submit_batch(self.host, self.root, self.actor, batch)
        self.assertEqual(recovered["outcome"], "complete", recovered)
        receipt = recovered["result"]
        state = connect(self.root, self.host, self.actor)[0].inspect(receipt["attempt"])
        self.assertEqual((state["state"], state["reservations"], state["unfinished_effects"]),
                         ("paused", [], []))
        self.assertEqual(host_status(self.root, self.host, self.actor)["grants"]["temporary"], 0)
        self.assertEqual(receipt["reason"], "recovered")

    def test_partial_batch_stays_red_and_protected(self):
        owners = ["docs/ONE.md", "docs/TWO.md"]
        (self.root / "eutonos.boards.json").write_text(json.dumps({"profile": boards.CONFIG, "owners": owners}), encoding="utf-8")
        for index, owner in enumerate(owners, 1):
            board = {"profile": boards.BOARD, "items": [{"id": f"WORK-{index}", "type": "quick",
                    "title": f"Work {index}", "summary": "Queued work.", "status": "active"}]}
            (self.root / owner).write_text("# Work\n\n```tokenslang-board\n" + json.dumps(board) + "\n```\n", encoding="utf-8")
        (self.root / "docs/bootstrap.md").write_text("Host only.\n", encoding="utf-8")
        self.initialize_git()
        enroll(self.root, self.host, self.actor, ["docs/bootstrap.md"], shared=True)
        batch = {"profile": write_batch.PROFILE, "operation_id": "partial-stays-protected", "operations": [
            {"kind": "update", "request": {"owner_path": owners[0],
             "expected_sha256": digest((self.root / owners[0]).read_bytes()), "item_id": "WORK-1",
             "changes": {"status": "completed"}}},
            {"kind": "update", "request": {"owner_path": owners[1],
             "expected_sha256": digest((self.root / owners[1]).read_bytes()), "item_id": "WORK-2",
             "changes": {"status": "completed"}}}]}
        original = LocalRoot.replace
        calls = 0
        def interrupt(instance, path, before, body):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated mid-batch crash")
            return original(instance, path, before, body)
        with mock.patch.object(LocalRoot, "replace", new=interrupt):
            first = write_retry.submit_batch(self.host, self.root, self.actor, batch)
        self.assertEqual(first["outcome"], "error", first)
        repeated = write_retry.submit_batch(self.host, self.root, self.actor, batch)
        self.assertEqual(repeated["outcome"], "error", repeated)
        self.assertEqual(write_retry.status(self.host, self.root)["signal"], "red")
        request_id = write_retry._request_id(self.root, self.actor, "batch",
                                             {"batch": write_batch.normalize(batch)})
        attempt = "write-batch-" + request_id.removeprefix("write-batch-")
        state = connect(self.root, self.host, self.actor)[0].inspect(attempt)
        self.assertTrue(state["reservations"])
        self.assertTrue(state["unfinished_effects"])
        self.assertFalse(host_status(self.root, self.host, self.actor)["grants"]["enforced"])
        with mock.patch.object(write_retry, "TERMINAL_RETENTION", 0):
            with closing(write_retry._connect(self.host)) as db, db:
                write_retry._prune(db, str(self.root))
                retained = db.execute("SELECT state FROM requests WHERE id=?",
                                      (request_id,)).fetchone()
        self.assertEqual(retained, ("failed",))
        self.assertEqual(write_retry.status(self.host, self.root)["signal"], "red")

    def test_stale_queued_batch_never_rebases_over_newer_owner(self):
        owner = "docs/WORK_ITEMS.md"
        board = {"profile": boards.BOARD, "items": [{"id": "WORK-1", "type": "quick", "title": "One",
                 "summary": "First.", "status": "active"}]}
        (self.root / "eutonos.boards.json").write_text(json.dumps({"profile": boards.CONFIG, "owners": [owner]}), encoding="utf-8")
        (self.root / owner).write_text("# Work\n\n```tokenslang-board\n" + json.dumps(board) + "\n```\n", encoding="utf-8")
        self.initialize_git()
        enroll(self.root, self.host, self.actor, [owner], shared=True)
        original_sha = digest((self.root / owner).read_bytes())
        coordinator, authority, generation = self.blocker([owner])
        batch = {"profile": write_batch.PROFILE, "operations": [{"kind": "update", "request": {
                 "owner_path": owner, "expected_sha256": original_sha, "item_id": "WORK-1",
                 "changes": {"status": "completed"}}}]}
        with mock.patch("eutonos.write_retry.start_worker", return_value={"outcome": "not-started"}):
            queued = write_retry.submit_batch(self.host, self.root, self.actor, batch)
        self.assertEqual(queued["outcome"], "queued", queued)
        added = boards.plan_add(self.root, {"profile": boards.ADD, "owner_path": owner,
            "expected_sha256": original_sha, "index": 1,
            "item": {"id": "OTHER", "type": "quick", "title": "Newer", "summary": "Preserve me.", "status": "active"}})
        selected_apply(coordinator, authority, "blocker", generation, "newer-owner", added["effect"], added["fingerprint"])
        self.release_blocker(coordinator, authority, generation, [owner])
        with mock.patch.object(write_retry, "_now", return_value=write_retry._now() + 120):
            retried = write_retry.run_once(self.host, self.root)
        self.assertEqual(retried["outcome"], "failed", retried)
        items = {item["id"]: item for item in boards.project(self.root)["items"]}
        self.assertEqual(items["WORK-1"]["status"], "active")
        self.assertEqual(items["OTHER"]["summary"], "Preserve me.")

    def test_board_retry_recovers_after_apply_before_already_applied_shortcut(self):
        owner = "docs/WORK_ITEMS.md"
        board = {"profile": boards.BOARD, "items": [{"id": "WORK-1", "type": "quick", "title": "Work",
                 "summary": "Original work.", "status": "active"}]}
        (self.root / "eutonos.boards.json").write_text(json.dumps({"profile": boards.CONFIG, "owners": [owner]}), encoding="utf-8")
        (self.root / owner).write_text("# Work\n\n```tokenslang-board\n" + json.dumps(board) + "\n```\n", encoding="utf-8")
        (self.root / "docs/bootstrap.md").write_text("Host only.\n", encoding="utf-8")
        self.initialize_git()
        enroll(self.root, self.host, self.actor, ["docs/bootstrap.md"], shared=True)
        payload = {"owner_path": owner, "item_id": "WORK-1", "changes": {"status": "completed"}}
        original = Coordinator.closeout
        def interrupt(instance, authority, attempt, generation, expected, checkpoint):
            if attempt.startswith("write-retry-board-"):
                raise RuntimeError("simulated crash before board receipt")
            return original(instance, authority, attempt, generation, expected, checkpoint)
        with mock.patch.object(Coordinator, "closeout", new=interrupt):
            with self.assertRaises(RuntimeError):
                write_retry.submit_board_update(self.host, self.root, self.actor, payload)
        recovered = write_retry.submit_board_update(self.host, self.root, self.actor, payload)
        self.assertEqual((recovered["outcome"], recovered["reason"]), ("complete", "recovered"), recovered)
        attempt = "write-retry-board-" + recovered["request_id"].removeprefix("write-")
        state = connect(self.root, self.host, self.actor)[0].inspect(attempt)
        self.assertEqual((state["reservations"], state["unfinished_effects"]), ([], []))
        self.assertEqual(host_status(self.root, self.host, self.actor)["grants"]["temporary"], 0)

    def test_ram_retries_exact_frozen_operation_once(self):
        (self.root / "RAM/work/session").mkdir(parents=True)
        (self.root / ".eutonos-local").mkdir()
        work = {"profile": ts.WORK, "work_id": "work", "goal": "Fixture", "status": "selected",
                "required": [], "optional": []}
        (self.root / "WORK.md").write_bytes(b"# Work\n\n```tokenslang-work\n" + ts.encoded(work) + b"\n```\n")
        (self.root / "changed.py").write_text("before = True\n", encoding="utf-8")
        config = {"profile": ram.CONFIG, "repository_id": "fixture.repo", "scope_id": "fixture.scope",
                  "namespace": "fixture.native", "store_root": "RAM", "current_task": "WORK.md",
                  "work_owners": {"work": "WORK.md"}}
        (self.root / "eutonos.ram.json").write_bytes(ts.encoded(config))
        self.initialize_git()
        (self.root / "changed.py").write_text("before = False\n", encoding="utf-8")
        context = {"root": str(self.root), "host": str(self.host), "actor": self.actor,
                   "work_id": "work", "session_id": "session"}
        values = {"request": "Change the fixture.", "actions": ["Updated changed.py."],
                  "result": "The fixture changed.", "why": "Exercise retry.", "checks": ["checked"],
                  "unresolved": [], "changed_paths": ["changed.py"]}
        prepared = workflow.prepare_closeout(values, context)
        enroll(self.root, self.host, self.actor, prepared["allowed_paths"], shared=True)
        coordinator, authority, generation = self.blocker([prepared["allowed_paths"][0]])
        with mock.patch("eutonos.write_retry.start_worker", return_value={"outcome": "not-started"}):
            queued = workflow.execute_closeout(prepared, context)
        self.assertEqual((queued["outcome"], queued["write_retry"]["outcome"]), ("deferred", "queued"), queued)
        self.release_blocker(coordinator, authority, generation, [prepared["allowed_paths"][0]])
        applied = write_retry.run_once(self.host, self.root)
        self.assertEqual(applied["outcome"], "complete", applied)
        record = ram.Store(LocalRoot(self.root)).load(prepared["record_path"])
        self.assertEqual((record["revision"], len(record["versions"])), (1, 1))

    def test_waiting_owner_does_not_starve_a_ready_owner(self):
        owners = ["docs/ONE.md", "docs/TWO.md"]
        config = {"profile": boards.CONFIG, "owners": owners}
        (self.root / "eutonos.boards.json").write_text(json.dumps(config), encoding="utf-8")
        for index, owner in enumerate(owners, 1):
            board = {"profile": boards.BOARD, "items": [{"id": f"WORK-{index}", "type": "quick",
                "title": f"Work {index}", "summary": "Queued work.", "status": "active"}]}
            (self.root / owner).write_text("# Work\n\n```tokenslang-board\n" + json.dumps(board) + "\n```\n", encoding="utf-8")
        self.initialize_git()
        enroll(self.root, self.host, self.actor, owners, shared=True)
        first = write_retry.enqueue(self.host, self.root, self.actor, "board-update",
            {"owner_path": owners[0], "item_id": "WORK-1", "changes": {"status": "completed"}})
        second = write_retry.enqueue(self.host, self.root, self.actor, "board-update",
            {"owner_path": owners[1], "item_id": "WORK-2", "changes": {"status": "completed"}})
        with closing(write_retry._connect(self.host)) as db, db:
            db.execute("UPDATE requests SET state='waiting',next_attempt=? WHERE id=?",
                       (time.time() + 60, first["request_id"]))
        applied = write_retry.run_once(self.host, self.root)
        self.assertEqual((applied["outcome"], applied["request_id"]), ("complete", second["request_id"]))
        items = {item["id"]: item for item in boards.project(self.root)["items"]}
        self.assertEqual((items["WORK-1"]["status"], items["WORK-2"]["status"]), ("active", "completed"))

    def test_failed_request_can_be_explicitly_requeued(self):
        (self.root / "docs/WORK.md").write_text("# Work\n", encoding="utf-8")
        self.initialize_git()
        enroll(self.root, self.host, self.actor, ["docs/WORK.md"], shared=True)
        payload = {"owner_path": "docs/WORK.md", "item_id": "WORK", "changes": {"status": "completed"}}
        queued = write_retry.enqueue(self.host, self.root, self.actor, "board-update", payload)
        with closing(write_retry._connect(self.host)) as db, db:
            db.execute("UPDATE requests SET state='failed',error='fixture' WHERE id=?", (queued["request_id"],))
        repeated = write_retry.enqueue(self.host, self.root, self.actor, "board-update", payload)
        self.assertEqual((repeated["outcome"], repeated["state"]), ("requeued", "pending"))

    def test_worker_launch_is_singleton_while_heartbeat_is_fresh(self):
        (self.root / "docs/WORK.md").write_text("# Work\n", encoding="utf-8")
        self.initialize_git()
        enroll(self.root, self.host, self.actor, ["docs/WORK.md"], shared=True)
        with mock.patch("eutonos.write_retry.subprocess.Popen") as popen:
            popen.return_value.pid = 42
            started = write_retry.start_worker(self.host, self.root)
            repeated = write_retry.start_worker(self.host, self.root)
        self.assertEqual(started, {"outcome": "started", "pid": 42})
        self.assertEqual(repeated, {"outcome": "already-running", "state": "launching"})
        popen.assert_called_once()

    def test_long_dispatch_refreshes_worker_heartbeat(self):
        (self.root / "docs/WORK.md").write_text("# Work\n", encoding="utf-8")
        self.initialize_git()
        enroll(self.root, self.host, self.actor, ["docs/WORK.md"], shared=True)
        write_retry.enqueue(self.host, self.root, self.actor, "board-update",
            {"owner_path": "docs/WORK.md", "item_id": "WORK", "changes": {"status": "completed"}})
        entered = threading.Event()
        release = threading.Event()

        def slow_dispatch(*_args):
            entered.set()
            self.assertTrue(release.wait(2))
            return {"outcome": "complete", "result": {"applied": True}}

        with mock.patch.object(write_retry, "STALE_AFTER", 0.15), \
             mock.patch("eutonos.write_retry._dispatch", side_effect=slow_dispatch), \
             ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(write_retry.run_once, self.host, self.root)
            self.assertTrue(entered.wait(1))
            time.sleep(0.25)
            with mock.patch("eutonos.write_retry.subprocess.Popen") as popen:
                takeover = write_retry.start_worker(self.host, self.root)
            release.set()
            result = future.result(timeout=2)
        self.assertEqual(takeover, {"outcome": "already-running", "state": "working"})
        self.assertEqual(result["outcome"], "complete")
        popen.assert_not_called()

    def test_displaced_worker_cannot_overwrite_replacement_state(self):
        (self.root / "docs/WORK.md").write_text("# Work\n", encoding="utf-8")
        self.initialize_git()
        enroll(self.root, self.host, self.actor, ["docs/WORK.md"], shared=True)
        queued = write_retry.enqueue(self.host, self.root, self.actor, "board-update",
            {"owner_path": "docs/WORK.md", "item_id": "WORK", "changes": {"status": "completed"}})

        def displaced_dispatch(*_args):
            with closing(write_retry._connect(self.host)) as db, db:
                write_retry._set_worker(db, str(self.root), "working", claim="replacement",
                                        request_id=queued["request_id"], stage="apply")
            return {"outcome": "complete", "result": {"applied": True}}

        with mock.patch("eutonos.write_retry._dispatch", side_effect=displaced_dispatch):
            result = write_retry.run_once(self.host, self.root)
        self.assertEqual(result["outcome"], "superseded")
        with closing(write_retry._connect(self.host, readonly=True)) as db:
            worker = db.execute("SELECT state,claim,request_id,stage FROM worker WHERE root=?",
                                (str(self.root),)).fetchone()
            request = db.execute("SELECT state FROM requests WHERE id=?", (queued["request_id"],)).fetchone()
        self.assertEqual(tuple(worker), ("working", "replacement", queued["request_id"], "apply"))
        self.assertEqual(tuple(request), ("working",))

    def test_status_counts_hidden_failed_requests_outside_display_window(self):
        (self.root / "docs/bootstrap.md").write_text("Host only.\n", encoding="utf-8")
        self.initialize_git()
        enroll(self.root, self.host, self.actor, ["docs/bootstrap.md"], shared=True)
        now = write_retry._now()
        with closing(write_retry._connect(self.host)) as db, db:
            db.execute("INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       ("old-failure", str(self.root), self.actor, "board-update", "{}",
                        now - 200, now - 200, "failed", 1, now - 200, "repair me", None))
            for index in range(101):
                db.execute("INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                           (f"complete-{index}", str(self.root), self.actor, "board-update", "{}",
                            now + index, now + index, "complete", 1, now + index, None, "{}"))
            write_retry._set_worker(db, str(self.root), "ready")
        result = write_retry.status(self.host, self.root)
        self.assertEqual(result["signal"], "red")
        self.assertEqual(result["counts"]["failed"], 1)
        self.assertEqual(result["counts"]["complete"], 101)
        self.assertEqual(len(result["requests"]), 100)

    def test_simultaneous_identical_batch_enqueue_is_idempotent(self):
        owner = "docs/WORK_ITEMS.md"
        board = {"profile": boards.BOARD, "items": [{"id": "WORK-1", "type": "quick",
                 "title": "One", "summary": "First.", "status": "active"}]}
        (self.root / "eutonos.boards.json").write_text(
            json.dumps({"profile": boards.CONFIG, "owners": [owner]}), encoding="utf-8")
        (self.root / owner).write_text(
            "# Work\n\n```tokenslang-board\n" + json.dumps(board) + "\n```\n", encoding="utf-8")
        self.initialize_git()
        enroll(self.root, self.host, self.actor, [owner], shared=True)
        batch = {"profile": write_batch.PROFILE, "operation_id": "simultaneous-same-request",
                 "operations": [{"kind": "update", "request": {"owner_path": owner,
                  "expected_sha256": digest((self.root / owner).read_bytes()), "item_id": "WORK-1",
                  "changes": {"status": "completed"}}}]}
        payload = {"batch": batch}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda _: write_retry.enqueue(self.host, self.root, self.actor, "batch", payload),
                range(2)))
        self.assertEqual({result["outcome"] for result in results}, {"queued", "already-queued"})
        self.assertEqual(len({result["request_id"] for result in results}), 1)


if __name__ == "__main__":
    unittest.main()
