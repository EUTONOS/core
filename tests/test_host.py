"""Unmarked Git projects exercise the same journal and reservation safeguards."""
import base64
from contextlib import closing
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from eutonos import ReadError, nav, tokenslang as ts
from eutonos.effects import LocalRoot, digest, plan
from eutonos.host import connect, enroll, renew, selected_apply, status
from eutonos import setup_cli
from eutonos.setup_cli import prepare


class HostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eutonos-project-host-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.root = self.home / "project"
        self.root.mkdir()
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        self.git("init")
        (self.root / "native.md").write_bytes(b"Existing project\n")
        self.git("add", "native.md")
        self.git("-c", "user.name=EUTONOS test", "-c", "user.email=test@example.invalid",
                 "commit", "-m", "baseline")
        self.directory = self.home / "host"

    def git(self, *args):
        return subprocess.run(["git", "-c", "safe.directory=" + str(self.root), "-C", str(self.root), *args],
                              env=self.env, check=True, capture_output=True).stdout

    def admitted(self):
        enroll(self.root, self.directory, "actor", ["native.md", "new.md"])
        owner, authority = connect(self.root, self.directory, "actor")
        owner.start(authority, "work")
        owner.reserve(authority, "work", 1, ["native.md", "new.md"])
        return owner, authority

    def reject(self, code, function, *args):
        with self.assertRaises(ReadError) as caught:
            function(*args)
        self.assertEqual(code, caught.exception.code)

    def test_setup_verifies_native_board_and_legacy_work_pickup(self):
        from eutonos import boards, setup_cli, workflow, work_lifecycle
        (self.root / "docs").mkdir()
        task = self.root / "docs/CURRENT_TASK.md"
        manifest = {"profile": "eutonos.tokenslang.installation.v1", "owners": {
            role: "docs/CURRENT_TASK.md" for role in
            ("purpose", "architecture", "health", "current_task", "state", "capabilities", "future", "lessons")}}
        (self.root / "eutonos.installation.json").write_text(json.dumps(manifest))
        for native in (True, False):
            with self.subTest(native=native):
                if native:
                    task.write_text("# Historical compatibility owner\nLive work is in the native board.\n")
                    (self.root / "eutonos.boards.json").write_text(json.dumps({
                        "profile": boards.CONFIG, "owners": ["BOARD.md"]}))
                    (self.root / "BOARD.md").write_text("```tokenslang-board\n" + json.dumps({
                        "profile": boards.BOARD, "items": [{"id": "live", "type": "quick", "title": "Live work",
                            "summary": "Current native work", "status": "active"}]}) + "\n```\n")
                    (self.root / "eutonos.work.json").write_text(json.dumps({
                        "profile": work_lifecycle.CONFIG, "current": ["live"], "next": []}))
                else:
                    (self.root / "eutonos.boards.json").unlink()
                    (self.root / "eutonos.work.json").unlink()
                    task.write_text("# Legacy work\n```tokenslang-work\n" + json.dumps({
                        "profile": ts.WORK, "work_id": "legacy", "goal": "Legacy work remains supported",
                        "status": "selected", "required": [], "optional": []}) + "\n```\n")
                # Exercise the real native/legacy reader; mock only the process boundary
                # and unrelated NAV/config checks so no installed runtime is needed.
                pickup = workflow.pickup(self.root)
                self.assertEqual(pickup["outcome"], "pickup")
                self.assertEqual(pickup["current_tasks"][0]["id"] if native else
                                 pickup["selected_task"]["work_id"], "live" if native else "legacy")
                process = subprocess.CompletedProcess([], 0, ts.encoded(pickup), b"")
                with patch("eutonos.setup_cli.subprocess.run", return_value=process) as run, \
                        patch("eutonos.setup_cli.nav.NavIndex") as index, \
                        patch("eutonos.setup_cli.Session") as session, \
                        patch("eutonos.setup_cli.ram.Store"), \
                        patch("eutonos.setup_cli.communication.Board"), \
                        patch("eutonos.setup_cli.measurement.Store"):
                    index.return_value.handle.return_value = {"outcome": "ok", "items": [{}]}
                    session.return_value.maintenance.return_value = {"outcome": "no-op"}
                    result = setup_cli.verify(LocalRoot(self.root))
                    self.assertEqual(result["outcome"], "verified")
                    self.assertIn("fresh installed workflow CLI", result["checks"])
                    self.assertIn("live native owners", result["limits"][-1])
                    self.assertEqual(run.call_args.args[0], [sys.executable, "-I", "-B", "-m",
                        "eutonos.workflow_cli", "pickup", "--root", str(self.root)])
                    run.return_value = subprocess.CompletedProcess([], 2, ts.encoded({"outcome": "error"}), b"")
                    self.reject("unavailable", setup_cli.verify, LocalRoot(self.root))
                    if native:
                        # Pickup can succeed with an empty work order despite damaged
                        # local owners; a successful process alone cannot qualify them.
                        board_path = self.root / "BOARD.md"
                        original = board_path.read_bytes()
                        (self.root / "eutonos.work.json").write_text(json.dumps({
                            "profile": work_lifecycle.CONFIG, "current": [], "next": []}))
                        for damaged in (None, b"```tokenslang-board\n{bad json\n```\n"):
                            if damaged is None:
                                board_path.unlink()
                            else:
                                board_path.write_bytes(damaged)
                            run.return_value = process
                            self.reject("unavailable", setup_cli.verify, LocalRoot(self.root))
                        board_path.write_bytes(original)

    def test_setup_pickup_delay_does_not_age_pinned_nav_budget(self):
        from types import SimpleNamespace
        from eutonos import setup_cli
        # Only the Git adapter sees this clock. The child wait is simulated,
        # while its NAV read exercises the real command/time budget.
        clock = [100.0]
        baseline = self.git("rev-parse", "HEAD").decode().strip()
        (self.root / "eutonos.installation.json").write_text(json.dumps({
            "profile": "eutonos.tokenslang.installation.v1", "owners": {
                role: "native.md" for role in
                ("purpose", "architecture", "health", "current_task", "state", "capabilities", "future", "lessons")}}))
        (self.root / "native.md").write_text("A populated current owner with more than thirty bytes.\n")
        observed = []

        def delayed_pickup(*args, **kwargs):
            clock[0] += 35
            return subprocess.CompletedProcess([], 0, ts.encoded({"outcome": "pickup"}), b"")

        def pinned_nav(view):
            body, evidence = view.read("native.md")
            self.assertEqual(body, b"Existing project\n")
            self.assertEqual(evidence["source_commit"], baseline)
            self.assertEqual(view._deadline, 130.0)
            observed.append(view)
            return SimpleNamespace(handle=lambda request: {"outcome": "ok", "items": [{}]})

        with patch("eutonos.source.time", SimpleNamespace(monotonic=lambda: clock[0])), \
                patch("eutonos.setup_cli.subprocess.run", side_effect=delayed_pickup), \
                patch("eutonos.setup_cli.nav.NavIndex", side_effect=pinned_nav), \
                patch("eutonos.setup_cli.Session") as session, \
                patch("eutonos.setup_cli.ram.Store"), \
                patch("eutonos.setup_cli.communication.Board"), \
                patch("eutonos.setup_cli.measurement.Store"):
            session.return_value.maintenance.return_value = {"outcome": "no-op"}
            result = setup_cli.verify(LocalRoot(self.root))
            self.assertEqual(result["commit"], baseline)
            self.assertEqual(result["outcome"], "verified")
            self.assertEqual(len(observed), 1)
            # The original limits still refuse a later read on the expired view.
            self.reject("overflow", observed[0].read, "native.md")

    def test_renew_is_retired_and_preserves_files_and_reservations(self):
        owner, auth = self.admitted()
        admission = (self.directory / "admission.json").read_bytes()
        self.assertEqual(renew(self.root, self.directory, "actor", [], 8, ["native.md"])["outcome"], "retired")
        self.assertEqual(len(owner.inspect("work")["reservations"]), 2)
        owner.closeout(auth, "work", 1, {"new.md": None, "native.md": digest(auth.root.read("native.md"))}, {"next": "done"})
        renew(self.root, self.directory, "actor", ["next.md"], 8, ["native.md", "new.md"])
        _, fresh = connect(self.root, self.directory, "actor")
        self.assertEqual(fresh.paths, frozenset())
        self.assertEqual((self.root / "native.md").read_bytes(), b"Existing project\n")
        self.assertEqual((self.directory / "admission.json").read_bytes(), admission)
        auth.check([])

    def test_unmarked_project_apply_replay_rollback_and_release(self):
        owner, auth = self.admitted()
        self.assertFalse((self.root / ".eutonos-synthetic").exists())
        value = plan(auth.root, {"new.md": b"new"})
        fingerprint = digest(ts.encoded(value))
        self.reject("denied", selected_apply, owner, auth, "work", 1, "effect", value, "wrong")
        self.assertEqual(selected_apply(owner, auth, "work", 1, "effect", value, fingerprint)["writes"], 1)
        self.assertEqual(selected_apply(owner, auth, "work", 1, "effect", value, fingerprint)["writes"], 0)
        owner.rollback(auth, "work", 1, "effect")
        self.assertFalse((self.root / "new.md").exists())
        owner.release(auth, "work", 1, {"new.md": None, "native.md": digest(auth.root.read("native.md"))})

    def test_setup_plan_rechecks_nav_source_without_expanding_observed_paths(self):
        (self.root / "src").mkdir()
        source = self.root / "src/demo.py"
        source.write_text("version = 1\n", encoding="utf-8")
        config = {"profile": nav.CONFIG, "repository_id": "test.repo", "scope_id": "test.scope",
                  "namespace": "test.native", "map_root": "NAV", "source_roots": ["src"],
                  "excluded_roots": [], "context_paths": ["native.md"]}
        (self.root / "eutonos.nav.json").write_text(json.dumps(config), encoding="utf-8")
        root = LocalRoot(self.root)
        fingerprint = nav.inventory(root, nav.config(root.read("eutonos.nav.json")),
                                    "eutonos.nav.json")["fingerprint"]
        owner, auth = self.admitted()
        value = plan(auth.root, {"new.md": b"verified"},
                     nav_source_fingerprint=fingerprint)
        self.assertLessEqual(len(value["observed"]), 64)
        self.assertNotIn("src/demo.py", value["observed"])
        selected = digest(ts.encoded(value))
        changed_guard = {**value, "nav_source_fingerprint": "b" * 64}
        self.reject("denied", selected_apply, owner, auth, "work", 1, "changed-guard",
                    changed_guard, selected)
        source.write_text("version = 2\n", encoding="utf-8")
        self.reject("stale", selected_apply, owner, auth, "work", 1, "guarded", value, selected)
        self.assertFalse((self.root / "new.md").exists())

        malformed = {**value, "nav_source_fingerprint": "bad"}
        self.reject("invalid", selected_apply, owner, auth, "work", 1, "malformed",
                    malformed, digest(ts.encoded(malformed)))
        unknown = {**value, "nav_source_guard": {}}
        self.reject("unsupported", selected_apply, owner, auth, "work", 1, "unknown",
                    unknown, digest(ts.encoded(unknown)))
        self.assertFalse((self.root / "new.md").exists())

        legacy = plan(auth.root, {"new.md": b"verified"})
        self.assertNotIn("nav_source_fingerprint", legacy)
        self.assertEqual(selected_apply(owner, auth, "work", 1, "legacy", legacy,
                                        digest(ts.encoded(legacy)))["writes"], 1)

    def test_finalizer_refreshes_host_observation_and_preserves_prior_acceptance(self):
        (self.root / "NAV").mkdir()
        (self.root / "eutonos.installation.json").write_text(
            json.dumps({"owners": {"purpose": "native.md"}}), encoding="utf-8")
        (self.root / "eutonos.acceptance.json").write_text(json.dumps({
            "host_observation": {"machine": "x86_64", "python": "3.14.4", "system": "Linux"},
            "prior_acceptance": {"dashboard": {"tests": 5}}}), encoding="utf-8")
        verified = {"outcome": "verified", "commit": self.git("rev-parse", "HEAD").decode().strip(),
                    "owners": {"purpose": {"path": "native.md", "sha256": digest(b"Existing project\n")}}}
        output = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        with patch("eutonos.setup_cli.verify", return_value=verified), \
                patch("eutonos.nav_runner.Session") as session, \
                patch("platform.machine", return_value="AMD64"), \
                patch("platform.python_version", return_value="3.14.4"), \
                patch("platform.system", return_value="Windows"), \
                redirect_stdout(output):
            session.return_value.observe.return_value = ({"fingerprint": "a" * 64},)
            self.assertEqual(setup_cli.main(["finalize", "--root", str(self.root)]), 0)
        output.flush()
        effect = json.loads(output.buffer.getvalue())["effect"]
        planned = next(c for c in effect["changes"] if c["path"] == "eutonos.acceptance.json")
        acceptance = json.loads(base64.b64decode(planned["after"]))
        self.assertEqual(acceptance["host_observation"],
                         {"machine": "AMD64", "python": "3.14.4", "system": "Windows"})
        self.assertEqual(acceptance["prior_acceptance"]["dashboard"], {"tests": 5})
        self.assertEqual(effect["nav_source_fingerprint"], "a" * 64)

    def test_closeout_is_atomic_and_external_review_keeps_evidence(self):
        owner, auth = self.admitted()
        value = plan(auth.root, {"native.md": b"effect bytes"})
        owner.apply(auth, "work", 1, "first", value)
        (self.root / "native.md").write_bytes(b"external bytes")
        expected = {"new.md": None, "native.md": digest(b"external bytes")}
        self.reject("conflict", owner.closeout, auth, "work", 1, expected, {"next": "done"})
        self.assertEqual(len(owner.inspect("work")["reservations"]), 2)
        self.assertEqual(owner.inspect("work")["state"], "active")
        self.reject("stale", owner.review_external, auth, "work", 1, {"native.md": "wrong"}, "Reviewed diff")
        review = owner.review_external(auth, "work", 1, {"native.md": expected["native.md"]}, "Owner-authorized exact diff review")
        self.assertEqual(review["writes"], 0)
        self.assertEqual(review["receipt"]["evidence"][0]["priorBaseline"], digest(b"Existing project\n"))
        result = owner.closeout(auth, "work", 1, expected, {"next": "done"})
        self.assertEqual(result["outcome"], "closed")
        self.assertEqual(result["generation"], 2)
        self.assertEqual((self.root / "native.md").read_bytes(), b"external bytes")
        self.reject("conflict", owner.reserve, auth, "work", 1, ["native.md"])

    def test_closeout_reports_retained_reservations(self):
        owner, auth = self.admitted()
        result = owner.closeout(auth, "work", 1, {"new.md": None}, {"next": "review native"})
        self.assertEqual(result["outcome"], "paused-with-reservations")
        self.assertEqual(result["remainingReservations"], ["native.md"])

    def test_git_initial_commit_required_without_creating_host(self):
        blank = self.home / "blank"
        blank.mkdir()
        self.reject("unavailable", enroll, blank, self.directory, "actor", ["new.md"])
        self.assertFalse(self.directory.exists())
        subprocess.run(["git", "init", str(blank)], env=self.env, check=True, capture_output=True)
        self.reject("unavailable", enroll, blank, self.directory, "actor", ["new.md"])
        self.assertFalse(self.directory.exists())

    def test_wrong_actor_root_historical_expiry_and_changed_admission(self):
        owner, auth = self.admitted()
        self.reject("wrong-scope", connect, self.root, self.directory, "other")
        other = self.home / "other"
        other.mkdir()
        self.reject("wrong-scope", connect, other, self.directory)
        with patch("eutonos.host.time.time", return_value=auth.expires_at + 1):
            auth.check([])
            owner.checkpoint(auth, "work", 1, {"expiry": "historical"})
        (self.directory / "admission.json").write_bytes(auth.receipt + b" ")
        self.reject("denied", owner.pause, auth, "work", 1)

    def test_no_expiry_admission_is_written_connects_and_reports_no_expiry(self):
        result = enroll(self.root, self.directory, "actor", [], hours=None)
        self.assertIsNone(result["expires_at"])
        raw = (self.directory / "admission.json").read_bytes()
        self.assertIsNone(json.loads(raw)["expires_at"])
        owner, auth = connect(self.root, self.directory, "actor")
        self.assertIsNone(auth.expires_at)
        owner.start(auth, "work")
        owner.checkpoint(auth, "work", 1, {"no": "expiry"})
        # Far-future time changes nothing: there is no expiry to pass.
        with patch("eutonos.host.time.time", return_value=4e12):
            self.assertEqual(status(self.root, self.directory, "actor", "work")["admission"],
                             {"actor": "actor", "expires_at": None, "state": "no-expiry"})
        self.assertEqual((self.directory / "admission.json").read_bytes(), raw)

    def test_invalid_expiry_values_are_still_rejected(self):
        enroll(self.root, self.directory, "actor", [], hours=None)
        for bad in ("never", True):
            with self.subTest(bad=bad):
                value = json.loads((self.directory / "admission.json").read_text(encoding="utf-8"))
                value["expires_at"] = bad
                (self.directory / "admission.json").write_text(json.dumps(value), encoding="utf-8")
                self.reject("invalid", connect, self.root, self.directory)
        with self.assertRaises(ReadError) as caught:
            enroll(self.root, self.home / "bad-hours", "actor", [], hours=0)
        self.assertEqual(caught.exception.code, "invalid")

    def test_status_reports_expired_admission_without_mutating_host(self):
        owner, auth = self.admitted()
        before_admission = (self.directory / "admission.json").read_bytes()
        before_journal = (self.directory / "coordinator.sqlite").read_bytes()
        with patch("eutonos.host.time.time", return_value=auth.expires_at + 1):
            result = status(self.root, self.directory, "actor", "work")
        self.assertEqual(result["admission"]["state"], "historical-expired")
        self.assertEqual(result["attempts"][0]["generation"], 1)
        self.assertEqual(result["attempts"][0]["reservations"], ["native.md", "new.md"])
        self.assertEqual(result["recommendation"]["state"], "ready")
        self.assertEqual((self.directory / "admission.json").read_bytes(), before_admission)
        self.assertEqual((self.directory / "coordinator.sqlite").read_bytes(), before_journal)

    def test_status_reports_paused_generation_and_resume(self):
        owner, auth = self.admitted()
        owner.pause(auth, "work", 1)
        before = (self.directory / "coordinator.sqlite").read_bytes()
        result = status(self.root, self.directory, "actor", "work")
        self.assertEqual(result["attempts"][0]["state"], "paused")
        self.assertEqual(result["attempts"][0]["generation"], 2)
        self.assertEqual(result["recommendation"], {
            "operation": "resume", "arguments": {"attempt": "work", "generation": 2},
            "reason": "The selected attempt is paused; resume advances its generation before a new effect.",
        })
        self.assertEqual((self.directory / "coordinator.sqlite").read_bytes(), before)

    def test_status_does_not_reopen_a_clean_paused_attempt(self):
        enroll(self.root, self.directory, "actor", ["native.md"])
        owner, auth = connect(self.root, self.directory, "actor")
        owner.start(auth, "clean-work")
        owner.pause(auth, "clean-work", 1)
        result = status(self.root, self.directory, "actor", "clean-work")
        self.assertEqual(result["recommendation"], {
            "operation": None, "arguments": {}, "state": "ready", "allowed_operations": ["start"],
            "reason": "The paused attempt retains no reservation or unfinished effect; no recovery is needed and new work may start a separate attempt.",
        })

    def test_status_reads_an_explicit_old_attempt_beyond_inventory_limit(self):
        owner, auth = self.admitted()
        for index in range(256):
            owner.start(auth, f"later-{index}")
        inventory = status(self.root, self.directory, "actor")
        self.assertTrue(inventory["attempts_truncated"])
        self.assertIsInstance(inventory["next_cursor"], int)
        self.assertEqual(inventory["recommendation"]["state"], "selection-required")
        result = status(self.root, self.directory, "actor", "work")
        self.assertEqual(result["attempts"], [{
            "id": "work", "state": "active", "generation": 1,
            "reservations": ["native.md", "new.md"], "unfinished_effects": [],
        }])
        self.assertFalse(result["attempts_truncated"])
        self.assertEqual(result["recommendation"]["state"], "ready")

    def test_status_paginates_inventory_without_hiding_old_attempts(self):
        owner, auth = self.admitted()
        for index in range(256):
            owner.start(auth, f"later-{index}")
        first = status(self.root, self.directory, "actor")
        second = status(self.root, self.directory, "actor", cursor=first["next_cursor"])
        first_ids = {item["id"] for item in first["attempts"]}
        second_ids = {item["id"] for item in second["attempts"]}
        self.assertTrue(first_ids.isdisjoint(second_ids))
        self.assertEqual(second_ids, {"work"})
        self.assertFalse(second["attempts_truncated"])
        self.assertIsNone(second["next_cursor"])
        self.reject("invalid", status, self.root, self.directory, "actor", "work", first["next_cursor"])

    def test_status_refuses_missing_journal_and_wrong_scope_without_creating(self):
        self.reject("unavailable", status, self.root, self.directory, "actor")
        self.assertFalse(self.directory.exists())
        self.admitted()
        self.reject("wrong-scope", status, self.root, self.directory, "other")
        other = self.home / "other"; other.mkdir()
        self.reject("wrong-scope", status, other, self.directory, "actor")

    def test_status_refuses_a_corrupt_journal_without_replacing_it(self):
        self.admitted()
        journal = self.directory / "coordinator.sqlite"
        journal.write_bytes(b"not a sqlite journal")
        before = journal.read_bytes()
        self.reject("unavailable", status, self.root, self.directory, "actor", "work")
        self.assertEqual(journal.read_bytes(), before)

    def test_scope_stale_writes_and_generation_fencing(self):
        owner, auth = self.admitted()
        owner.reserve(auth, "work", 1, ["unadmitted.md"])
        self.reject("unsafe-path", owner.reserve, auth, "work", 1, ["../outside.md"])
        value = plan(auth.root, {"native.md": b"replacement"})
        (self.root / "native.md").write_bytes(b"newer human edit")
        self.reject("stale", owner.apply, auth, "work", 1, "effect", value)
        generation = owner.pause(auth, "work", 1)
        self.reject("conflict", owner.apply, auth, "work", 1, "effect", value)
        self.assertEqual(owner.resume(auth, "work", generation), 3)
        self.assertEqual((self.root / "native.md").read_bytes(), b"newer human edit")

    def test_missing_owner_is_not_recreated_and_duplicate_enrollment_refused(self):
        owner, auth = self.admitted()
        self.reject("conflict", enroll, self.root, self.directory, "actor", ["new.md"])
        owner.path.unlink()
        self.reject("unavailable", connect, self.root, self.directory)
        self.assertFalse(owner.path.exists())

    def test_host_inside_project_is_refused(self):
        self.reject("denied", enroll, self.root, self.root / "host", "actor", ["new.md"])

    def test_retired_renewal_never_issues_grants(self):
        owner, auth = self.admitted()
        self.assertEqual(renew(self.root, self.directory, "actor", ["extra.md"])["outcome"], "retired")
        auth.check([])
        current, authority = connect(self.root, self.directory)
        self.assertEqual(current.inspect("work")["reservations"], owner.inspect("work")["reservations"])
        self.assertEqual(authority.paths, frozenset())
        self.assertEqual(status(self.root, self.directory)["grants"]["active_paths"], 0)
        owner.path.unlink()
        self.reject("unavailable", renew, self.root, self.directory, "actor", [])

    def test_expired_admission_without_grants_allows_write_but_reservation_conflicts(self):
        enroll(self.root, self.directory, "actor", [], hours=0.001)
        owner, authority = connect(self.root, self.directory, "actor")
        with closing(sqlite3.connect(self.directory / "coordinator.sqlite")) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='write_grants'").fetchone())
        with patch("eutonos.host.time.time", return_value=authority.expires_at + 1):
            expired_owner, expired_authority = connect(self.root, self.directory, "actor")
            expired_owner.start(expired_authority, "first")
            expired_owner.start(expired_authority, "second")
            expired_owner.reserve(expired_authority, "first", 1, ["native.md"])
            self.reject("conflict", expired_owner.reserve, expired_authority, "second", 1, ["native.md"])
            effect = plan(expired_authority.root, {"native.md": b"after expiry\n"})
            self.assertEqual(expired_owner.apply(expired_authority, "first", 1, "write", effect)["writes"], 1)
        self.assertEqual((self.root / "native.md").read_bytes(), b"after expiry\n")

    def test_existing_grant_rows_are_history_and_are_preserved(self):
        enroll(self.root, self.directory, "actor", ["native.md"])
        _, authority = connect(self.root, self.directory, "actor")
        root_id = ":".join(str(part) for part in authority.root.identity)
        journal = self.directory / "coordinator.sqlite"
        with closing(sqlite3.connect(journal)) as db:
            db.execute("CREATE TABLE write_grants(root TEXT, actor TEXT, path TEXT, kind TEXT, owner TEXT, expires_at REAL, granted_at REAL)")
            db.execute("INSERT INTO write_grants VALUES(?,?,?,?,?,?,?)",
                       (root_id, "actor", "native.md", "shared", "", 1.0, 1.0))
            db.commit()
        owner, authority = connect(self.root, self.directory, "actor")
        owner.start(authority, "work")
        owner.reserve(authority, "work", 1, ["new.md"])
        self.assertEqual(renew(self.root, self.directory, "actor", ["new.md"])["outcome"], "retired")
        self.assertEqual(status(self.root, self.directory)["grants"]["retained"], 1)
        with closing(sqlite3.connect(journal)) as db:
            self.assertEqual(db.execute("SELECT path,expires_at FROM write_grants").fetchall(), [("native.md", 1.0)])

    def test_setup_plan_preserves_existing_files_and_repeated_apply_is_empty(self):
        owner, auth = self.admitted()
        recipe = {"profile": "eutonos.tokenslang.setup-recipe.v1", "files": [
            {"path": "native.md", "content": "New content", "before": None}]}
        self.reject("conflict", prepare, auth.root, recipe)
        recipe["files"][0]["before"] = digest(auth.root.read("native.md"))
        value = prepare(auth.root, recipe)
        self.assertEqual(auth.root.read("native.md"), b"Existing project\n")
        selected_apply(owner, auth, "work", 1, "setup", value["effect"], value["fingerprint"])
        self.assertEqual(prepare(auth.root, recipe)["effect"]["changes"], [])

    def test_fresh_cli_enroll_start_reserve_and_inspect(self):
        def cli(command, *extra):
            process = subprocess.run([sys.executable, "-m", "eutonos.host_cli", command,
                                      "--root", str(self.root), "--host", str(self.directory), *extra],
                                     capture_output=True, env=self.env)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            return json.loads(process.stdout)
        self.assertEqual(cli("enroll", "--actor", "actor", "--path", "new.md")["outcome"], "enrolled")
        cli("start", "--attempt", "work")
        cli("reserve", "--attempt", "work", "--path", "new.md")
        self.assertEqual(cli("inspect", "--attempt", "work")["attempt"]["reservations"][0]["path"], "new.md")
        result = cli("status", "--attempt", "work")
        self.assertEqual(result["outcome"], "status")
        self.assertEqual(result["recommendation"]["arguments"], {"attempt": "work", "generation": 1})
        self.assertEqual(result["recommendation"]["state"], "ready")


if __name__ == "__main__":
    unittest.main()
