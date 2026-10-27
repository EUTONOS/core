"""Sentinel regression coverage for shared entrypoints and pinned private sources."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from eutonos import boards, boards_cli, native_owner, write_batch, current_work, current_work_knowledge, dashboard, hub, metric_index
from eutonos import measurement, nav, private_scope, ram, ram_relevance, repository_state, repository_state_cli
from eutonos import assignments, delivery_summary, settings, workflow_diagnostics, work_precheck
from eutonos import shared_attempts, shared_work, tokenslang as ts, workflow, workflow_cli, closeout_route
from eutonos.effects import LocalRoot
from eutonos.source import GitView


class PrivateScopeTests(unittest.TestCase):
    SECRET = "SENTINEL personal title and private body"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eutonos-private-boundary-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "public"
        self.root.mkdir()
        self.home = self.base / "device"
        patch = mock.patch.dict(os.environ, {"EUTONOS_HOME": str(self.home)})
        patch.start(); self.addCleanup(patch.stop)
        self.git(self.root, "init", "-q", "-b", "main")
        self.write_public(self.root, "Ordinary public task")
        self.commit(self.root)
        self.public_commit = self.git(self.root, "rev-parse", "HEAD")

    def git(self, root, *args):
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_OPTIONAL_LOCKS="0")
        return subprocess.check_output(["git", "-c", "safe.directory=" + str(root),
            "-c", "core.autocrlf=false", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "-C", str(root), *args], env=env, stderr=subprocess.PIPE).decode().strip()

    def commit(self, root):
        self.git(root, "add", "--all"); self.git(root, "commit", "-qm", "fixture")

    def write_public(self, root, title):
        (root / "eutonos.read.json").write_text(json.dumps({"repository_id": "fixture.repo", "scope_id": "fixture.scope", "namespace": "fixture.native"}))
        (root / "eutonos.boards.json").write_text(json.dumps({"profile": boards.CONFIG, "owners": ["work.md"]}))
        (root / "work.md").write_text("```tokenslang-board\n" + json.dumps({"profile": boards.BOARD, "items": [
            {"id": "task-one", "type": "task", "title": title, "summary": title, "status": "active"}]}) + "\n```\n")

    def manifest(self, root=None):
        return {"profile": private_scope.PROFILE, "identity": {"repository_id": "private.repo", "scope_id": "private.scope", "namespace": "personal"},
                "owner_id": "fixture-owner", "owners": dict(private_scope.OWNERS)}

    def mark(self, root=None, malformed=False):
        (root or self.root).joinpath(private_scope.MANIFEST).write_text(self.SECRET if malformed else json.dumps(self.manifest()))

    def denied(self, operation):
        with self.assertRaises(ts.ReadError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "denied")
        self.assertNotIn(self.SECRET, str(caught.exception))
        self.assertNotIn(str(self.root), str(caught.exception))

    def test_shared_entrypoints_deny_before_owner_reads_and_cache_creation(self):
        self.mark(malformed=True)
        operations = [lambda: boards.project(self.root), lambda: boards.plan_add(self.root, {}),
            lambda: boards.plan_update(self.root, {}), lambda: boards.plan_reorder(self.root, {}),
            lambda: boards.resolve_owner(self.root, "task-one"),
            lambda: native_owner.form(self.root, "docs/private.md", self.SECRET),
            lambda: native_owner.plan(self.root, {}), lambda: write_batch.form(self.root),
            lambda: write_batch.plan(self.root, {}), lambda: settings.status(self.root),
            lambda: settings.delivery_status(self.root), lambda: delivery_summary.project(self.root), lambda: dashboard.project(self.root),
            lambda: dashboard.lazy_module(self.root, "companion"), lambda: workflow.pickup(self.root),
            lambda: repository_state.project(self.root), lambda: current_work.project(self.root),
            lambda: current_work.sources(self.root), lambda: current_work_knowledge._controls(self.root),
            lambda: ram.Store(LocalRoot(self.root)), lambda: measurement.Store(LocalRoot(self.root)),
            lambda: assignments.records(self.root), lambda: workflow_diagnostics.read_events(self.root),
            lambda: workflow_diagnostics.record(self.root, {}), lambda: work_precheck.read_claim(self.root, "task-one"), lambda: ram_relevance.open_store(self.root),
            lambda: metric_index.project(self.root), lambda: metric_index.update(self.root),
            lambda: metric_index.status(self.root), lambda: shared_work.load(self.root),
            lambda: shared_attempts.start(self.root, "task-one", "session", self.home, "actor"),
            lambda: shared_attempts.finish(self.root, "task-one", "session", self.home, "actor")]
        with mock.patch.object(LocalRoot, "read", side_effect=AssertionError("owner read before guard")):
            for operation in operations:
                with self.subTest(operation=operation): self.denied(operation)
        self.assertFalse(self.home.exists())
        self.assertFalse((self.root / ".eutonos-local").exists())

    def test_generic_closeout_rejects_private_before_draft_defer_or_capture(self):
        self.mark(malformed=True)
        context = {"root": str(self.root), "actor": "actor", "work_id": "task-one", "session_id": "session"}
        deferred = {"profile": workflow.PROFILE, "outcome": "deferred", "values": {"what": self.SECRET},
                    "pending_entry": {"changed_paths": [self.SECRET + ".md"]},
                    "pending_scope": {**context, "native_identity": None}}
        prepared = {"profile": workflow.PROFILE, "outcome": "prepared", "changed_paths": [self.SECRET + ".md"]}
        with mock.patch.object(workflow, "_draft", side_effect=AssertionError("private Git draft")), \
                mock.patch.object(workflow.capture_policy, "defer", side_effect=AssertionError("private defer")), \
                mock.patch.object(settings, "status", side_effect=AssertionError("private settings")), \
                mock.patch.object(LocalRoot, "read", side_effect=AssertionError("private owner read")):
            for operation in (workflow.prepare_closeout, workflow.execute_closeout, closeout_route.prepare, closeout_route.run):
                for values in (None, {"what": self.SECRET}, deferred, prepared):
                    with self.subTest(operation=operation.__name__, values=values):
                        result = operation(values, context)
                        self.assertEqual(result["code"], "denied", result)
                        self.assertNotIn(self.SECRET, json.dumps(result))
                        self.assertNotIn(str(self.root), json.dumps(result))
                        self.assertNotIn("operation", result)
        self.assertFalse((self.root / ".eutonos-local").exists())
        self.assertFalse(self.home.exists())
        self.git(self.root, "add", private_scope.MANIFEST); self.git(self.root, "commit", "-qm", "private marker")
        (self.root / private_scope.MANIFEST).unlink()
        (self.root / ".git" / "HEAD").write_text("invalid head\n")
        self.assertEqual(workflow.prepare_closeout(None, context)["code"], "unavailable")
        self.assertEqual(closeout_route.run(deferred, context)["code"], "unavailable")

    def test_workflow_cli_denies_before_input_load_and_all_postprocessing(self):
        self.mark(malformed=True)
        private_input = self.root / (self.SECRET + ".json")
        private_input.write_text("{malformed " + self.SECRET)
        context_file = self.base / "context.json"
        context_file.write_text(json.dumps({"root": str(self.root)}))
        with mock.patch.object(workflow.capture_policy, "defer", side_effect=AssertionError("private defer")), \
                mock.patch.object(workflow_cli, "record", side_effect=AssertionError("private log")), \
                mock.patch.object(workflow_cli, "_delivery_cadence", side_effect=AssertionError("private cadence")):
            for action in ("prepare", "run"):
                for input_path in (private_input, self.root / (self.SECRET + "-missing.json")):
                    output = io.StringIO()
                    with mock.patch.object(workflow_cli, "_load", side_effect=AssertionError("private input opened")), redirect_stdout(output):
                        code = workflow_cli.main([action, "closeout", "--root", str(self.root), "--input", str(input_path)])
                    self.assertEqual(code, 2)
                    result = json.loads(output.getvalue())
                    self.assertEqual(result["code"], "denied")
                    self.assertNotIn(self.SECRET, output.getvalue())
                    self.assertNotIn(str(self.root), output.getvalue())
                loads = []
                real_load = workflow_cli._load
                def tracked_load(path):
                    loads.append(path)
                    return real_load(path)
                output = io.StringIO()
                with mock.patch.object(workflow_cli, "_load", side_effect=tracked_load), redirect_stdout(output):
                    code = workflow_cli.main([action, "closeout", "--root", str(self.base), "--context", str(context_file), "--input", str(private_input)])
                self.assertEqual(code, 2)
                self.assertEqual(loads, [str(context_file)])
                self.assertEqual(json.loads(output.getvalue())["code"], "denied")
                self.assertNotIn(self.SECRET, output.getvalue())
        self.assertFalse((self.root / ".eutonos-local").exists())
        self.assertFalse(self.home.exists())

    def test_expert_shared_route_checks_forwarded_root_before_module_dispatch(self):
        self.mark(malformed=True)
        for root_args in (["--root", str(self.root)], ["--root=" + str(self.root)]):
            output = io.StringIO()
            with mock.patch.object(workflow_cli.importlib, "import_module") as imported, redirect_stdout(output):
                code = workflow_cli.main(["expert", "docs", "read", "--request", self.SECRET + ".json", *root_args], default_root=str(self.base))
            self.assertEqual(code, 2)
            imported.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())["code"], "denied")
            self.assertNotIn(self.SECRET, output.getvalue())
            self.assertNotIn(str(self.root), output.getvalue())
        for route, argv in (("personal", ["read"]), ("device", ["read"]), ("docs", ["--help"]), ("boards", ["profile"]), ("docs", [])):
            with mock.patch.object(workflow_cli.importlib, "import_module") as imported:
                imported.return_value.main.return_value = 0
                self.assertEqual(workflow_cli.main(["expert", route, *argv], default_root=str(self.root)), 0)
                imported.return_value.main.assert_called_once()

    def test_dashboard_all_roster_paths_are_sanitized_including_deferred_roots(self):
        self.mark(malformed=True)
        value = dashboard.snapshot([self.root], detail="summary", work_project=2)
        output = json.dumps(value)
        self.assertNotIn(str(self.root), output)
        self.assertNotIn('"name"', output)
        self.assertNotIn(self.SECRET, output)
        self.assertEqual(value["projects"][0]["status"], "denied")

    def test_shared_cli_reports_sanitized_denial(self):
        self.mark(malformed=True)
        stream = io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(repository_state_cli.main(["--root", str(self.root)]), 1)
        self.assertNotIn(str(self.root), stream.getvalue())
        self.assertNotIn(self.SECRET, stream.getvalue())

    def test_real_missing_git_is_unknown_without_owner_reads_but_marker_still_denied(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.base / "no-executables")}), mock.patch.object(
                LocalRoot, "read", side_effect=AssertionError("unknown scope opened owners")):
            value = work_precheck.precheck(self.root, "task-one")
            self.assertEqual(value["verdict"], "unknown")
            self.assertFalse(value["commits"]["available"])
            self.assertFalse(value["live_edits"]["available"])
            self.assertNotIn(str(self.root), json.dumps(value))
            self.assertNotIn("Ordinary public task", json.dumps(value))
            self.mark(malformed=True)
            self.denied(lambda: work_precheck.precheck(self.root, "task-one"))

    def test_actual_invalid_head_cannot_declassify_deleted_private_marker(self):
        self.write_public(self.root, self.SECRET)
        self.mark()
        self.commit(self.root)
        (self.root / private_scope.MANIFEST).unlink()
        (self.root / ".git" / "HEAD").write_text("invalid-head\n")
        with mock.patch.object(LocalRoot, "read", side_effect=AssertionError("unknown HEAD opened private owners")):
            with self.assertRaises(ts.ReadError) as caught:
                boards.project(self.root)
            self.assertEqual(caught.exception.code, "unavailable")
            self.assertNotIn(self.SECRET, str(caught.exception))
            value = work_precheck.precheck(self.root, "task-one")
            self.assertEqual(value["verdict"], "unknown")
            self.assertNotIn(self.SECRET, json.dumps(value))
            self.assertNotIn(str(self.root), json.dumps(value))
            value = dashboard.snapshot([self.root], detail="summary", work_project=2)
            self.assertNotIn(str(self.root), json.dumps(value))
            self.assertNotIn('"name"', json.dumps(value))

    def test_board_cli_refuses_private_before_request_or_owner_reads(self):
        self.mark()
        for command in (["read"], ["plan", "add", "--request", str(self.root / "missing-request")],
                        ["owner", "form", "--path", "docs/personal.md", "--title", self.SECRET]):
            stream = io.StringIO()
            with redirect_stdout(stream), mock.patch.object(LocalRoot, "read", side_effect=AssertionError("owner read")):
                self.assertEqual(boards_cli.main(["--root", str(self.root), *command]), 2)
            output = stream.getvalue()
            self.assertNotIn(str(self.root), output)
            self.assertNotIn(self.SECRET, output)
            self.assertEqual(json.loads(output)["code"], "denied")

    def test_ordinary_public_projection_is_preserved(self):
        self.assertFalse(private_scope.is_private(self.root))
        ordinary = self.base / "ordinary-without-git"
        ordinary.mkdir()
        self.write_public(ordinary, "Ordinary non-Git task")
        self.assertEqual(boards.project(ordinary)["items"][0]["title"], "Ordinary non-Git task")
        self.assertEqual(boards.project(self.root)["items"][0]["title"], "Ordinary public task")
        value = repository_state.project(self.root, refresh=True)
        self.assertEqual(value["status"], "available")
        self.assertEqual(value["latest"]["items"][0]["title"], "Ordinary public task")

    def private_branch(self):
        target = self.base / "SENTINEL-private-locator"
        self.git(self.root, "worktree", "add", "-qb", "SENTINEL-private-branch", str(target))
        self.write_public(target, self.SECRET)
        self.mark(target)
        self.commit(target)
        return target, self.git(target, "rev-parse", "HEAD")

    def test_private_refs_worktrees_and_history_paths_never_enter_shared_cache(self):
        target, commit = self.private_branch()
        (target / "SENTINEL-private-path.txt").write_text(self.SECRET)
        self.commit(target)
        value = current_work.project(self.root, refresh=True, include_knowledge=False)
        output = json.dumps(value)
        self.assertNotIn("SENTINEL", output)
        self.assertIn("Ordinary public task", output)
        for path in self.home.rglob("*.json"):
            self.assertNotIn("SENTINEL", path.read_text())
        descriptor = {"kind": "local-ref", "commit": commit, "ref": "SENTINEL-private-branch"}
        with repository_state.SourceReader(self.root) as reader:
            self.denied(lambda: reader.read(descriptor, "work.md"))
        view = GitView(target, commit)
        # The explicitly selected private adapter can still use this low-level reader.
        self.assertIn(self.SECRET.encode(), view.read("work.md")[0])
        self.denied(lambda: hub.Hub(view))
        self.denied(lambda: nav.NavIndex(view))

    def test_pinned_marker_denies_after_worktree_marker_removal(self):
        target, commit = self.private_branch()
        (target / private_scope.MANIFEST).unlink()
        self.denied(lambda: boards.project(target))
        for detail, work_project in (("full", None), ("summary", 2)):
            value = dashboard.snapshot([target], detail=detail, work_project=work_project)
            self.assertNotIn("SENTINEL", json.dumps(value))
        self.denied(lambda: hub.Hub(GitView(self.root, commit)))

    def test_new_worktree_marker_revokes_old_shared_cache_without_refresh(self):
        target = self.base / "SENTINEL-private-locator"
        self.git(self.root, "worktree", "add", "-qb", "SENTINEL-private-branch", str(target))
        before = current_work.project(self.root, refresh=True, include_knowledge=False)
        self.assertIn(str(target), [row.get("path") for row in before["sources"]])
        self.mark(target)
        self.write_public(target, self.SECRET)
        after = current_work.project(self.root, include_knowledge=False)
        self.assertNotIn("SENTINEL", json.dumps(after))
        direct = repository_state.project(self.root)
        self.assertNotIn("SENTINEL", json.dumps(direct))
        for path in self.home.rglob("*.json"):
            self.assertNotIn("SENTINEL", path.read_text())

    def test_owner_selection_and_manifest_validation_are_sanitized(self):
        self.mark()
        self.assertEqual(private_scope.require_owner(self.root, "fixture-owner"), self.manifest())
        self.denied(lambda: private_scope.require_owner(self.root, None))
        self.denied(lambda: private_scope.require_owner(self.root, self.SECRET))
        invalid = self.manifest(); invalid["owners"]["goals"] = "../../" + self.SECRET
        (self.root / private_scope.MANIFEST).write_text(json.dumps(invalid))
        self.denied(lambda: private_scope.require_owner(self.root, "fixture-owner"))

    def test_private_host_is_external_dedicated_and_rejects_shared_home(self):
        self.mark()
        host = private_scope.private_host(self.root)
        self.assertEqual(host.parent, self.home / "private-hosts")
        self.assertNotIn(self.SECRET, str(host))
        self.denied(lambda: private_scope.private_host(self.root, home=self.root))
        shared = self.base / "shared"; shared.mkdir(); self.write_public(shared, "Public")
        self.denied(lambda: private_scope.private_host(self.root, home=shared / "device"))

    def test_destination_rejects_shared_publisher_and_source_nested_paths(self):
        self.mark()
        for path in (self.root / "export", self.root / ".git/eutonos-delivery/export", self.root / "work.md"):
            value = private_scope.validate_private_destination(path, self.root)
            self.assertEqual(value["status"], "denied")
            self.assertNotIn(str(self.root), json.dumps(value))
        private = self.base / "independent-private-export"
        value = private_scope.validate_private_destination(private, self.root)
        self.assertEqual(value["status"], "allowed")
        self.assertEqual(value["independence"], "unverified")
        shared = self.base / "shared"; shared.mkdir(); self.write_public(shared, "Public")
        self.assertEqual(private_scope.validate_private_destination(shared / "export")["status"], "denied")

    def test_aliases_are_refused_before_resolution(self):
        alias = self.base / "alias"
        try:
            alias.symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest("Local OS does not permit disposable directory symlinks")
        self.mark()
        self.denied(lambda: private_scope.require_owner(alias, "fixture-owner"))
        self.assertEqual(private_scope.validate_private_destination(alias / "backup")["status"], "denied")
        self.denied(lambda: private_scope.private_host(self.root, home=alias / "device"))
