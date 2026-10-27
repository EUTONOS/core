"""Public GAP2 CLI journeys through actual source-wrapper subprocesses."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from copy import deepcopy
from unittest import mock

from eutonos import boards, command_registry, goal_records, tokenslang as ts, write_batch, write_retry
from eutonos.host import connect, enroll


WRAPPER = Path(__file__).resolve().parents[1] / "tools" / "workflow.py"


class GapCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gap-cli-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        self.env.update(HOME=str(self.base), EUTONOS_HOME=str(self.base / "local"), PYTHONUTF8="1")
        (self.base / ".gitconfig").write_text("[user]\n name = Fixture\n email = fixture@example.invalid\n", encoding="utf-8")

    def cli(self, route, *args, request=None, expected=0):
        command = [sys.executable, "-X", "utf8", str(WRAPPER), "expert", route, *map(str, args)]
        if request is not None:
            command.extend(["--request", "-"])
        result = subprocess.run(command, input=json.dumps(request) if request is not None else None,
                                capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertFalse(result.stderr, result.stderr)
        return json.loads(result.stdout)

    def git(self, root, *args):
        return subprocess.check_output(["git", "-C", str(root), *args], env=self.env, stderr=subprocess.PIPE).decode().strip()

    def shared(self):
        root = self.base / "shared"; root.mkdir()
        (root / "eutonos.boards.json").write_text(json.dumps({"profile": boards.CONFIG, "owners": ["GOALS.md"]}), encoding="utf-8")
        (root / "GOALS.md").write_text("# Outcome goals\n", encoding="utf-8")
        identity = {"repository_id": "fixture-repository", "scope_id": "fixture-scope", "namespace": "fixture"}
        (root / "eutonos.read.json").write_text(json.dumps({"profile": ts.CATALOG, **identity}), encoding="utf-8")
        self.git(root, "init", "-q", "-b", "main")
        self.git(root, "add", "--all")
        self.git(root, "commit", "-qm", "Fixture baseline")
        return root

    def private(self):
        root = self.base / "private"
        form = self.cli("personal", "bootstrap", "form", "--root", root, "--owner-id", "owner")
        self.assertFalse(root.exists(), "Bootstrap form must not write its root")
        result = self.cli("personal", "--root", root, "bootstrap", "submit", "--owner-id", "owner",
                          "--actor", "actor", "--session", "native-session", request=form)
        self.assertTrue(result["native"]["complete"])
        self.assertEqual(result["history"]["state"], "committed")
        self.assertEqual(result["host_admission"]["outcome"], "enrolled")
        return root, result["host"], form

    def test_registration_help_and_expert_routes_are_bounded(self):
        for route, module in (("goals", "goals_cli"), ("personal", "personal_cli"), ("device", "device_repositories_cli")):
            self.assertEqual(command_registry.EXPERT_ROUTES[route], "eutonos." + module)
            description = command_registry.describe(route)
            self.assertEqual(description["outcome"], "description")
            result = subprocess.run([sys.executable, str(WRAPPER), "expert", route, "--help"],
                                    capture_output=True, text=True, env=self.env, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--root", result.stdout)
        self.assertEqual(command_registry.GUIDED_COMMANDS, ("closeout",))
        project = Path(__file__).resolve().parents[1]
        entrypoints = (project / "pyproject.toml").read_text(encoding="utf-8")
        for script in ("eutonos-goals", "eutonos-personal", "eutonos-device-repositories"):
            self.assertIn(script + " = ", entrypoints)

    def test_shared_goal_form_submit_read_and_stale_update(self):
        root = self.shared()
        host = self.base / "shared-host"
        enroll(root, host, "actor", ["GOALS.md"])
        form = self.cli("goals", "--root", root, "form", "--owner-path", "GOALS.md")
        self.assertTrue(form["missing_fields"])
        item = form["request"]["operations"][0]["request"]["items"][0]
        item.update(title="Ship an outcome", summary="A bounded shared result")
        operation_id = form["request"]["operation_id"]
        invalid = self.cli("goals", "submit", "--root", root, "--host", host,
                           "--actor", "actor", "--session", "native-session", request=form, expected=2)
        self.assertEqual(invalid["code"], "invalid")
        with closing(write_retry._connect(host)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM batch_bindings").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0)
        item["extensions"]["goal"]["outcome"] = "A supported result"
        invalid = self.cli("goals", "submit", "--root", root, "--host", host,
                           "--actor", "actor", "--session", "native-session", request=form, expected=2)
        self.assertEqual(invalid["code"], "invalid")
        with closing(write_retry._connect(host)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM batch_bindings").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0)
        item["extensions"]["goal"]["criteria"][0]["statement"] = "Result exists"
        self.assertEqual(form["request"]["operation_id"], operation_id)
        result = self.cli("goals", "submit", "--root", root, "--host", host,
                          "--actor", "actor", "--session", "native-session", request=form)
        self.assertNotIn(result.get("outcome"), {"error", "conflict"})
        repeated_create = self.cli("goals", "submit", "--root", root, "--host", host,
                                   "--actor", "actor", "--session", "native-session", request=form)
        self.assertEqual(repeated_create["request_id"], result["request_id"])
        selected = self.cli("goals", "read", "--root", root, "--item-id", item["id"])
        self.assertEqual(selected["item"]["title"], item["title"])
        stale = self.cli("goals", "form", "--root", root, "--owner-path", "GOALS.md", "--item-id", item["id"], request={"summary": "First update"})
        fresh = self.cli("goals", "form", "--root", root, "--owner-path", "GOALS.md", "--item-id", item["id"], request={"summary": "Second update"})
        original = self.cli("goals", "submit", "--root", root, "--host", host, "--actor", "actor", "--session", "native-session", request=fresh)
        self.assertEqual(original["outcome"], "complete")
        first_bytes = (root / "GOALS.md").read_bytes()
        repeated = self.cli("goals", "submit", "--root", root, "--host", host,
                            "--actor", "actor", "--session", "native-session", request=fresh)
        self.assertEqual((repeated["outcome"], repeated["request_id"]), ("complete", original["request_id"]))
        self.assertEqual((root / "GOALS.md").read_bytes(), first_bytes)
        refused = self.cli("goals", "submit", "--root", root, "--host", host, "--actor", "actor", "--session", "native-session", request=stale, expected=2)
        self.assertEqual(refused["code"], "stale", "A new operation must still validate its observed goal revision")
        changed = deepcopy(fresh)
        changed["request"]["operations"][0]["request"]["changes"]["summary"] = "Changed original intent"
        for request, session in ((changed, "native-session"), (fresh, "different-session")):
            denied = self.cli("goals", "submit", "--root", root, "--host", host,
                              "--actor", "actor", "--session", session, request=request, expected=2)
            self.assertEqual(denied["code"], "conflict")
        later = self.cli("goals", "form", "--root", root, "--owner-path", "GOALS.md", "--item-id", item["id"], request={"summary": "Later legitimate update"})
        latest = self.cli("goals", "submit", "--root", root, "--host", host,
                          "--actor", "actor", "--session", "native-session", request=later)
        latest_bytes = (root / "GOALS.md").read_bytes()
        repeated = self.cli("goals", "submit", "--root", root, "--host", host,
                            "--actor", "actor", "--session", "native-session", request=fresh)
        self.assertEqual(repeated["request_id"], original["request_id"])
        self.assertEqual((root / "GOALS.md").read_bytes(), latest_bytes, "Historical receipt replay must not undo a later edit")
        coordinator, _ = connect(root, host, "actor")
        with coordinator._db() as db:
            effects_before = db.execute("SELECT COUNT(*) FROM effects").fetchone()[0]
        with closing(write_retry._connect(host)) as db, db:
            db.execute("DELETE FROM requests WHERE id=?", (latest["request_id"],))
        recovered = self.cli("goals", "submit", "--root", root, "--host", host,
                             "--actor", "actor", "--session", "native-session", request=later)
        self.assertEqual((recovered["outcome"], recovered["request_id"]), ("complete", latest["request_id"]))
        self.assertEqual(recovered["result"]["reason"], "recovered")
        self.assertEqual((root / "GOALS.md").read_bytes(), latest_bytes)
        with coordinator._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM effects").fetchone()[0], effects_before)

    def test_shared_goal_metadata_only_binding_still_requires_semantic_validation(self):
        root = self.shared()
        host = self.base / "shared-host"
        enroll(root, host, "actor", ["GOALS.md"])
        form = self.cli("goals", "form", "--root", root, "--owner-path", "GOALS.md")
        item = form["request"]["operations"][0]["request"]["items"][0]
        item.update(title="Incomplete semantic draft", summary="Must not bypass goal validation")
        batch = deepcopy(form["request"])
        batch.pop("goal_preconditions")
        batch["caller_context"] = {"session": "native-session"}
        original = (root / "GOALS.md").read_bytes()
        (root / "GOALS.md").write_bytes(original + b'\nConcurrent owner prose\n')
        # The generic API retains a binding before its stale owner-plan failure,
        # but creates no queue row or native effect for this invalid goal draft.
        self.cli("boards", "batch", "submit", "--root", root, "--host", host,
                 "--actor", "actor", request=batch, expected=2)
        with closing(write_retry._connect(host)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM batch_bindings").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0)
        (root / "GOALS.md").write_bytes(original)
        invalid = self.cli("goals", "submit", "--root", root, "--host", host,
                           "--actor", "actor", "--session", "native-session", request=form, expected=2)
        self.assertEqual(invalid["code"], "invalid")
        self.assertEqual((root / "GOALS.md").read_bytes(), original)
        with closing(write_retry._connect(host)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0)

    def test_shared_goal_original_pending_update_recovers_after_native_save(self):
        root = self.shared()
        host = self.base / "shared-host"
        enroll(root, host, "actor", ["GOALS.md"])
        form = self.cli("goals", "form", "--root", root, "--owner-path", "GOALS.md")
        item = form["request"]["operations"][0]["request"]["items"][0]
        item.update(title="Recover original goal", summary="Initial outcome")
        item["extensions"]["goal"]["outcome"] = "A supported result"
        item["extensions"]["goal"]["criteria"][0]["statement"] = "Result exists"
        self.cli("goals", "submit", "--root", root, "--host", host,
                 "--actor", "actor", "--session", "native-session", request=form)
        update = self.cli("goals", "form", "--root", root, "--owner-path", "GOALS.md",
                          "--item-id", item["id"], request={"summary": "Saved before terminal receipt"})
        batch = deepcopy(update["request"])
        batch.pop("goal_preconditions")
        batch["caller_context"] = {"session": "native-session"}

        def save_before_queue_receipt(selected_host, selected_root):
            request_id = write_retry._request_id(selected_root, "actor", "batch", {"batch": batch})
            result = write_retry._dispatch(selected_host, selected_root, "actor", request_id,
                                           "batch", {"batch": write_batch.normalize(batch)})
            self.assertTrue(result["complete"])
            # Simulate process loss before run_once persists the terminal receipt.
            return {"outcome": "interrupted"}

        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(write_retry, "run_once", side_effect=save_before_queue_receipt), \
             mock.patch.object(write_retry, "start_worker", return_value={"outcome": "not-started"}):
            pending = goal_records.submit_shared(root, update["request"], host=host, actor="actor", session="native-session")
        self.assertEqual(pending["outcome"], "queued")
        saved_bytes = (root / "GOALS.md").read_bytes()
        coordinator, _ = connect(root, host, "actor")
        with coordinator._db() as db:
            effects_before = db.execute("SELECT COUNT(*) FROM effects").fetchone()[0]
        recovered = self.cli("goals", "submit", "--root", root, "--host", host,
                             "--actor", "actor", "--session", "native-session", request=update)
        self.assertEqual((recovered["outcome"], recovered["request_id"]), ("complete", pending["request_id"]))
        self.assertEqual((root / "GOALS.md").read_bytes(), saved_bytes)
        with coordinator._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM effects").fetchone()[0], effects_before)

    def test_device_enrollment_forms_revisions_and_no_implicit_root(self):
        root = self.shared()
        home = self.base / "device"
        empty = self.cli("device", "--home", home, "read", "--root", root)
        self.assertEqual(empty["shared"], [])
        self.assertFalse((home / "device-repositories.json").exists())
        form = self.cli("device", "form", "enroll", "--home", home, "--root", root, request={"locator": str(root)})
        self.assertEqual(form["missing_fields"], [])
        enrolled = self.cli("device", "enroll", "--home", home, "--root", root, request=form)
        self.assertEqual(len(enrolled["shared"]), 1)
        self.cli("device", "enroll", "--home", home, "--root", root, request=form, expected=2)
        form = self.cli("device", "form", "withdraw", "--home", home, "--root", root, request={"locator": str(root)})
        self.cli("device", "withdraw", "--home", home, "--root", root, request=form)
        self.assertEqual(self.cli("device", "read", "--home", home, "--root", root)["shared"], [])

    def test_private_project_submit_verify_and_original_recovery(self):
        root, host, bootstrap = self.private()
        first = self.git(root, "rev-parse", "HEAD")
        form = self.cli("personal", "form", "--kind", "project", "--root", root, "--owner-id", "owner")
        item = form["request"]["item"]
        item.update(title="PRIVATE_SENTINEL", summary="Private project without a code repository")
        item["extensions"]["personal_project"]["purpose"] = "A private outcome"
        prepared = self.cli("personal", "prepare", "--root", root, "--owner-id", "owner", request=form)
        result = self.cli("personal", "submit", "--root", root, "--owner-id", "owner", "--host", host,
                          "--actor", "actor", "--session", "native-session", request=form)
        self.assertEqual(result["history"]["state"], "committed")
        self.assertNotEqual(first, self.git(root, "rev-parse", "HEAD"))
        self.cli("personal", "verify", "--root", root, "--owner-id", "owner", request=prepared)
        committed = self.git(root, "rev-parse", "HEAD")
        self.cli("personal", "recover", "--root", root, "--owner-id", "owner", "--host", host,
                 "--actor", "actor", "--session", "native-session", "--operation-id", form["request"]["operation_id"])
        self.assertEqual(committed, self.git(root, "rev-parse", "HEAD"))
        selected = self.cli("personal", "read", "--root", root, "--owner-id", "owner", "--item-id", item["id"])
        self.assertEqual(selected["item"]["title"], "PRIVATE_SENTINEL")
        self.assertFalse((root / "eutonos.work.json").exists())
        # No generic closeout or shared delivery owner is introduced.
        self.assertFalse((root / "RAM").exists())
        self.assertFalse((root / "eutonos.delivery.json").exists())

    def test_owner_errors_precede_request_loading_and_hide_content(self):
        root, _, _ = self.private()
        sentinel = "PRIVATE_REQUEST_AND_PATH_SENTINEL"
        for owner in (None, "wrong-owner"):
            args = ["submit", "--root", root, "--host", self.base / sentinel, "--actor", "actor", "--session", "native-session",
                    "--request", self.base / sentinel]
            if owner:
                args.extend(["--owner-id", owner])
            result = self.cli("personal", *args, expected=2)
            self.assertEqual(result["code"], "denied")
            self.assertNotIn(sentinel, json.dumps(result))
            self.assertNotIn(str(root), json.dumps(result))
        denied = self.cli("goals", "form", "--root", root, "--owner-path", sentinel,
                          "--request", self.base / sentinel, expected=2)
        self.assertEqual(denied["code"], "denied")
        self.assertNotIn(sentinel, json.dumps(denied))
        broken = self.cli("personal", "prepare", "--root", root, "--owner-id", "owner",
                          "--request", self.base / sentinel, expected=2)
        self.assertNotIn(sentinel, json.dumps(broken))

    def test_bootstrap_pending_author_recovers_same_form_without_new_identity(self):
        (self.base / ".gitconfig").write_text("", encoding="utf-8")
        root = self.base / "pending-personal"
        form = self.cli("personal", "bootstrap", "form", "--root", root, "--owner-id", "owner")
        pending = self.cli("personal", "bootstrap", "submit", "--root", root, "--owner-id", "owner",
                           "--actor", "actor", "--session", "native-session", request=form, expected=2)
        self.assertTrue(pending["native"]["complete"])
        self.assertEqual(pending["history"]["state"], "pending")
        self.assertNotIn("host_admission", pending)
        identity = self.cli("personal", "read", "--root", root, "--owner-id", "owner")["identity"]
        # Author identity is fixed only in the disposable repo, through ordinary Git configuration.
        self.git(root, "config", "user.name", "Fixture")
        self.git(root, "config", "user.email", "fixture@example.invalid")
        recovered = self.cli("personal", "bootstrap", "submit", "--root", root, "--owner-id", "owner",
                             "--actor", "actor", "--session", "native-session", request=form)
        self.assertEqual(recovered["history"]["state"], "committed")
        self.assertEqual(self.cli("personal", "read", "--root", root, "--owner-id", "owner")["identity"], identity)
        self.assertEqual(self.git(root, "rev-list", "--count", "HEAD"), "1")

    def test_private_export_restore_keeps_identity_and_requires_fresh_host(self):
        root, host, _ = self.private()
        form = self.cli("personal", "form", "--root", root, "--owner-id", "owner", "--kind", "project")
        item = form["request"]["item"]
        item.update(title="Restored private project", summary="Independent of any code repository")
        item["extensions"]["personal_project"]["purpose"] = "A retained private outcome"
        self.cli("personal", "submit", "--root", root, "--owner-id", "owner", "--host", host,
                 "--actor", "actor", "--session", "native-session", request=form)
        archive = self.base / "selected-private.zip"
        export = self.cli("personal", "export", "form", "--root", root, "--owner-id", "owner", "--destination", archive)
        self.assertFalse(archive.exists())
        backup = self.cli("personal", "export", "submit", "--root", root, "--owner-id", "owner", "--host", host,
                          "--actor", "actor", "--session", "native-session", request=export)
        self.assertEqual(backup["backup"]["state"], "verified")
        self.assertEqual(backup["backup"]["independence"], "unverified")
        destination = self.base / "relocated-private"
        restore = self.cli("personal", "restore", "form", "--root", destination, "--owner-id", "owner",
                           "--destination", destination, "--archive", archive)
        self.assertFalse(destination.exists())
        restored = self.cli("personal", "restore", "submit", "--root", destination, "--owner-id", "owner",
                            "--actor", "actor", "--session", "native-session", request=restore)
        self.assertTrue(restored["fresh_admission_required"])
        self.assertTrue(restored["reenrollment_required"])
        before = self.cli("personal", "read", "--root", root, "--owner-id", "owner")
        after = self.cli("personal", "read", "--root", destination, "--owner-id", "owner")
        self.assertEqual((after["identity"], after["items"]), (before["identity"], before["items"]))
        host_form = self.cli("personal", "host", "form", "--root", destination, "--owner-id", "owner")
        self.assertNotEqual(host_form["host"], host)
        self.cli("personal", "host", "enroll", "--root", destination, "--owner-id", "owner",
                 "--actor", "actor", "--session", "native-session", request=host_form)
        self.assertEqual(self.cli("personal", "host", "status", "--root", destination, "--owner-id", "owner")["outcome"], "status")
        update = self.cli("personal", "form", "--root", destination, "--owner-id", "owner", "--kind", "project",
                          "--item-id", item["id"], request={"summary": "Continued after private restore"})
        continued = self.cli("personal", "submit", "--root", destination, "--owner-id", "owner", "--host", host_form["host"],
                             "--actor", "actor", "--session", "restored-native-session", request=update)
        self.assertEqual(continued["history"]["state"], "committed")
        self.assertEqual(self.cli("personal", "read", "--root", destination, "--owner-id", "owner", "--item-id", item["id"])["item"]["summary"],
                         "Continued after private restore")

    def test_device_personal_selection_is_redacted_without_owner(self):
        root, _, _ = self.private()
        home = self.base / "personal-device"
        for owner in (None, "wrong-owner"):
            owner_args = ["--owner-id", owner] if owner else []
            refused = self.cli("device", "form", "enroll", "--root", root, "--home", home, *owner_args,
                               request={"kind": "personal", "locator": str(root)}, expected=2)
            self.assertEqual(refused["code"], "denied")
            self.assertNotIn(str(root), json.dumps(refused))
        form = self.cli("device", "form", "enroll", "--root", root, "--home", home, "--owner-id", "owner",
                        request={"kind": "personal", "locator": str(root)})
        self.cli("device", "enroll", "--root", root, "--home", home, "--owner-id", "owner", request=form)
        shown = self.cli("device", "read", "--root", root, "--home", home)
        self.assertEqual(shown["personal"], {"outcome": "denied"})
        self.assertNotIn(str(root), json.dumps(shown))
        self.assertNotIn("owner", json.dumps(shown))

    def test_history_form_observes_revision_and_file_request_requires_caller(self):
        root, host, _ = self.private()
        history = self.cli("personal", "history", "form", "--root", root, "--owner-id", "owner")
        self.assertEqual(history["request"]["expected_revision"], 1)
        request_file = self.base / "history-request.json"
        request_file.write_text(json.dumps(history), encoding="utf-8-sig")
        refused = self.cli("personal", "history", "configure", "--root", root, "--owner-id", "owner",
                           "--request", request_file, expected=2)
        self.assertEqual(refused["code"], "invalid")
        unchanged = self.cli("personal", "history", "form", "--root", root, "--owner-id", "owner")
        self.assertEqual(unchanged["request"]["expected_revision"], 1)
        configured = self.cli("personal", "history", "configure", "--root", root, "--owner-id", "owner",
                              "--actor", "actor", "--session", "native-session", "--request", request_file)
        self.assertEqual(configured["revision"], 2)
        self.assertIsNone(configured["remote_url"])
        self.assertIsNone(configured["records_ref"])
        self.cli("personal", "history", "configure", "--root", root, "--owner-id", "owner",
                 "--actor", "actor", "--session", "native-session", "--request", request_file, expected=2)


if __name__ == "__main__":
    unittest.main()
