"""IPC5 lane D: detach, full de-adoption, dry-run, resume and the add/remove completion checks."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from eutonos import (adoption, completion_checks, device_repositories as device, home, permissions, projects,
                     repository_lifecycle as lifecycle, setup_cli, tokenslang as ts)
from eutonos.effects import LocalRoot
from eutonos.host import connect, enroll, read_snapshot, selected_apply


def tree(base, skip=()):
    """Every file under base (relative, posix) with its bytes; `.git` internals are left out unless named in skip."""
    base = Path(base)
    return {path.relative_to(base).as_posix(): path.read_bytes() for path in sorted(base.rglob("*"))
            if path.is_file() and ".git" not in path.relative_to(base).parts[:1] and path.name not in skip}


class FakeCheckpoint:
    """The shape of lane A's setup_checkpoint, held in memory."""

    def __init__(self, existing=None):
        self.value = existing

    def read(self, home=None):
        return self.value

    def start(self, checklist, steps, *, checklist_revision, gen_id=None, root=None, home=None, now=None):
        self.value = {"profile": "eutonos.setup-checkpoint.v1", "checklist": checklist, "root": root, "steps": steps}
        return self.value

    def record_step(self, step_id, state, evidence=None, reason=None, next_action=None, *, home=None, now=None):
        step = next(item for item in self.value["steps"] if item["id"] == step_id)
        step.update(state=state, evidence=step["evidence"] + list(evidence or []))
        return self.value

    def checkpoint_path(self, home=None):
        return Path("in-memory-checkpoint")


class RepositoryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eutonos-lifecycle-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.eutonos_home = self.base / "eutonos-home"
        self.user = self.base / "user"
        self.user.mkdir()
        patcher = mock.patch.dict(os.environ, {"EUTONOS_HOME": str(self.eutonos_home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        # The real application folders are never read: every call names this fake user folder.
        self.root = self.base / "project"
        self.root.mkdir()
        (self.root / "tools").mkdir()
        (self.root / "docs").mkdir()
        work = {"profile": ts.WORK, "work_id": "project-work", "goal": "Preserve native project work",
                "status": "selected", "required": [], "optional": []}
        (self.root / "docs/WORK.md").write_bytes(b"# Native work\n\n```tokenslang-work\n" + ts.encoded(work) + b"\n```\n")
        (self.root / "docs/ARCHITECTURE.md").write_text("# Native architecture\nExisting owner facts.\n", encoding="utf-8")
        (self.root / "AGENTS.md").write_text("# Existing agent rules\nPreserve the native boot protocol.\n", encoding="utf-8")
        self.git("init", "-q", "-b", "main")
        self.git("add", "--all")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "baseline")
        self.local = LocalRoot(self.root)

    def git(self, *args):
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        return subprocess.check_output(["git", "-c", "safe.directory=" + str(self.root), "-C", str(self.root), *args],
                                       env=env, stderr=subprocess.PIPE)

    def adopt(self, register=True):
        """Adopt Docs and delegation with a scratch host, commit the result, then register, apply project entries and
        enroll the device, the way an add-repository run leaves things."""
        profile = {"profile": adoption.PROFILE, "repository_id": "project.repo", "scope_id": "project.local",
                   "namespace": "project.native", "owners": {
                       "current_task": {"path": "docs/WORK.md", "resource_id": "project.doc.work"},
                       "architecture": {"path": "docs/ARCHITECTURE.md", "resource_id": "project.doc.architecture"}},
                   "modules": ["docs", "delegation"]}
        planned = adoption.plan_profile(self.local, profile)
        scratch = self.base / "adoption-host"
        enroll(self.root, scratch, "owner", planned["paths"])
        coordinator, authority = connect(self.root, scratch, "owner")
        generation = coordinator.start(authority, "adopt")
        coordinator.reserve(authority, "adopt", generation, planned["paths"])
        selected_apply(coordinator, authority, "adopt", generation, "adoption", planned["effect"], planned["fingerprint"])
        self.git("add", "--all")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "adopt")
        self.before_entries = tree(self.root)
        self.exclude = self.root / ".git/info/exclude"
        self.exclude_before = self.exclude.read_bytes() if self.exclude.exists() else b""
        if not register:
            return
        added = projects.add(self.root)
        self.host = Path(added["project"]["host"])
        self.effects = lifecycle.apply_project_permissions(self.root, user_home=self.user)
        current = device.read()
        device.update({"profile": device.REQUEST, "operation": "enroll", "locator": str(self.root)},
                      expected_revision=current["revision"])

    def record_consent(self, choices=None, *, accepted=True, state="active"):
        """An install record with consent under a temporary EUTONOS home (what lane A's installer writes)."""
        gen = "0123456789abcdef"
        folder = self.eutonos_home / "install"
        (folder / "generations" / gen).mkdir(parents=True)
        (folder / "state.json").write_text(json.dumps({"profile": "eutonos.install.state.v1", "active": gen, "pending": None,
                                                       "generations": {gen: "x"}, "legacy": None}), encoding="utf-8")
        consent = {"text_id": "eutonos.consent.v1", "text_sha256": "0" * 64, "surface": "terminal",
                   "accepted_at": "2026-10-22T00:00:00Z" if accepted else None, "choices": choices or {}}
        (folder / "generations" / gen / "record.json").write_text(
            json.dumps({"profile": "eutonos.install.record.v1", "gen_id": gen, "state": state, "consent": consent}),
            encoding="utf-8")

    def template_plan(self, **choices):
        plan = adoption.preservation_template(self.local)
        plan["paths"].update(choices)
        path = self.base / "plan.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        return path

    def run_cli(self, *argv):
        raw = io.BytesIO()
        console = io.TextIOWrapper(raw, encoding="utf-8")
        with mock.patch("sys.stdout", console):
            code = setup_cli.main(list(argv))
        console.flush()
        value = json.loads(raw.getvalue().decode("utf-8"))
        console.detach()
        return code, value

    # ------------------------------------------------------------------ detach
    def test_detach_reverses_entries_and_leaves_repository_files_byte_identical(self):
        self.adopt()
        targets = {Path(effect["target"]).as_posix() for effect in self.effects}
        self.assertIn((self.root / ".claude/settings.local.json").as_posix(), targets)
        self.assertIn((self.root / ".codex/config.toml").as_posix(), targets)
        self.assertIn((self.root / ".codex/rules/eutonos.rules").as_posix(), targets)
        self.assertIn(self.exclude.as_posix(), targets)  # the .git/info/exclude lines are Effects too
        self.assertTrue(all(Path(target).is_relative_to(self.root) for target in targets))  # nothing user-wide was touched
        self.assertNotEqual(self.exclude.read_bytes(), self.exclude_before)
        result = lifecycle.remove_project(self.root)
        self.assertEqual(result["outcome"], "removed", result)
        self.assertEqual(result["level"], "detach")
        self.assertEqual(tree(self.root), self.before_entries)  # every repository file, including the wrapper and manifest
        self.assertFalse((self.root / ".claude").exists() or (self.root / ".codex").exists())
        self.assertEqual(self.exclude.read_bytes(), self.exclude_before)
        self.assertTrue((self.root / adoption.MANIFEST_PATH).is_file())
        self.assertTrue((self.root / "tools/eutonos_project.py").is_file())
        self.assertEqual({entry["entry"] for entry in result["person_reversals"]}, {"desktop-folder-auto", "codex-trust"})
        self.assertEqual(result["steps"]["person-reversals"]["state"], "pending")  # the person's to undo, listed not done

    def test_a_target_you_changed_is_kept_and_reported(self):
        self.adopt()
        rules = self.root / ".codex/rules/eutonos.rules"
        rules.write_text(rules.read_text(encoding="utf-8") + "# my own rule\n", encoding="utf-8")
        mine = rules.read_bytes()
        result = lifecycle.remove_project(self.root)
        self.assertEqual(result["outcome"], "removed-with-kept")
        self.assertEqual(rules.read_bytes(), mine)
        self.assertEqual([item["reason"] for item in result["kept_permission_effects"]], ["changed by you, kept"])
        self.assertFalse((self.root / ".claude/settings.local.json").exists())  # the untouched ones still went

    def test_root_is_withdrawn_unregistered_and_the_host_is_retired_not_deleted(self):
        self.adopt()
        admission = (self.host / "admission.json").read_bytes()
        self.assertEqual(len(device.read()["shared"]), 1)
        result = lifecycle.remove_project(self.root)
        self.assertEqual(result["outcome"], "removed")
        self.assertEqual(device.read()["shared"], [])
        self.assertIsNone(projects.lookup(self.root))
        self.assertTrue(self.host.is_dir())
        self.assertEqual((self.host / "admission.json").read_bytes(), admission)
        self.assertEqual(projects.host_state(self.host), "retired")
        self.assertTrue((self.host / "coordinator.sqlite").exists())
        read_snapshot(self.root, self.host)  # an archived host still validates
        self.assertEqual({name: item["state"] for name, item in result["steps"].items()
                          if name != "person-reversals"},
                         {"preservation-plan": "skipped", "reverse-project-entries": "done", "reverse-adoption": "skipped",
                          "withdraw-device-repository": "done", "unregister-project": "done", "retire-host": "done"})
        again = lifecycle.remove_project(self.root)  # a second run changes nothing and does not fail
        self.assertEqual(again["outcome"], "removed")
        self.assertEqual(projects.host_state(self.host), "retired")

    def test_detach_still_works_when_the_wrapper_is_gone(self):
        self.adopt()
        (self.root / "tools/eutonos_project.py").unlink()
        result = lifecycle.remove_project(self.root)
        self.assertEqual(result["outcome"], "removed")
        self.assertIsNone(projects.lookup(self.root))

    # ------------------------------------------------------------------ dry run
    def test_dry_run_prints_the_plan_and_writes_nothing(self):
        self.adopt()
        before = {"base": tree(self.base), "exclude": self.exclude.read_bytes(), "registry": home.projects_file().read_bytes()}
        report = lifecycle.remove_project(self.root, dry_run=True)
        self.assertEqual((report["outcome"], report["changed"], report["registered"]), ("dry-run", False, True))
        results = {item["effect_id"]: item["result"] for item in report["permission_effects"]}
        self.assertTrue(results and set(results.values()) == {"would-remove"})
        self.assertEqual((report["device_repositories"], report["registry"], report["host_action"]),
                         ("would-withdraw", "would-remove", "would-retire"))
        after = {"base": tree(self.base), "exclude": self.exclude.read_bytes(), "registry": home.projects_file().read_bytes()}
        self.assertEqual(after, before)
        self.assertFalse(list(self.eutonos_home.glob("remove-repository-checkpoint-*")))
        self.assertEqual(projects.host_state(self.host), "active")
        code, value = self.run_cli("remove-project", "--root", str(self.root), "--dry-run")
        self.assertEqual((code, value["outcome"]), (0, "dry-run"))
        self.assertEqual(tree(self.base), before["base"])

    def test_dry_run_reports_a_changed_target_as_kept(self):
        self.adopt()
        rules = self.root / ".codex/rules/eutonos.rules"
        rules.write_text(rules.read_text(encoding="utf-8") + "# mine\n", encoding="utf-8")
        report = lifecycle.remove_project(self.root, dry_run=True)
        kept = [item for item in report["permission_effects"] if item["result"] == "would-keep"]
        self.assertEqual([item["reason"] for item in kept], ["changed by you, kept"])
        self.assertTrue(rules.is_file())

    # ------------------------------------------------------------------ full de-adoption
    def test_de_adopt_refuses_without_a_plan_and_changes_nothing(self):
        self.adopt()
        before = tree(self.base)
        for call in (lambda: lifecycle.remove_project(self.root, de_adopt=True),
                     lambda: lifecycle.remove_project(self.root, de_adopt=True, plan=self.base / "no-such-plan.json")):
            with self.assertRaises(ts.ReadError):
                call()
        code, value = self.run_cli("remove-project", "--root", str(self.root), "--de-adopt")
        self.assertEqual((code, value["outcome"], value["code"]), (2, "error", "invalid"))
        self.assertEqual(tree(self.base), before)
        self.assertIsNotNone(projects.lookup(self.root))
        bad = self.template_plan()
        broken = json.loads(bad.read_text(encoding="utf-8"))
        broken["paths"].pop("CLAUDE.md")
        bad.write_text(json.dumps(broken), encoding="utf-8")
        before = tree(self.base)  # now including the plan file itself
        with self.assertRaises(ts.ReadError) as caught:  # a plan that does not match the manifest is refused before any write
            lifecycle.remove_project(self.root, de_adopt=True, plan=bad)
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(tree(self.base), before)

    def test_de_adopt_removes_only_the_remove_paths_and_backs_them_up(self):
        self.adopt()
        plan = self.template_plan(**{"AGENTS.md": "keep"})
        result = lifecycle.remove_project(self.root, de_adopt=True, plan=plan)
        self.assertEqual(result["outcome"], "removed", result)
        self.assertEqual(result["steps"]["reverse-adoption"]["state"], "done")
        after = tree(self.root)
        self.assertNotIn("tools/eutonos_project.py", after)
        self.assertNotIn("CLAUDE.md", after)
        self.assertNotIn(adoption.MANIFEST_PATH, after)
        self.assertEqual(after["AGENTS.md"], self.before_entries["AGENTS.md"])  # the plan said keep
        for kept in ("eutonos.read.json", "docs/WORK.md", "docs/ARCHITECTURE.md"):  # native owners and the catalog stay
            self.assertEqual(after[kept], self.before_entries[kept], kept)
        backup = Path(result["backup_dir"])
        self.assertEqual((backup / "tools/eutonos_project.py").read_bytes(), self.before_entries["tools/eutonos_project.py"])
        self.assertTrue(backup.is_relative_to(self.eutonos_home))
        self.assertIsNone(projects.lookup(self.root))
        self.assertEqual(projects.host_state(self.host), "retired")

    def test_de_adopt_keeps_a_file_that_was_changed_and_the_manifest_that_records_it(self):
        self.adopt()
        wrapper = self.root / "tools/eutonos_project.py"
        wrapper.write_text(wrapper.read_text(encoding="utf-8") + "# local edit\n", encoding="utf-8")
        edited = wrapper.read_bytes()
        result = lifecycle.remove_project(self.root, de_adopt=True, plan=self.template_plan())
        self.assertEqual(result["outcome"], "removed-with-kept")
        self.assertEqual(wrapper.read_bytes(), edited)
        self.assertIn("tools/eutonos_project.py", [item["path"] for item in result["kept_repository_files"]])
        self.assertTrue((self.root / adoption.MANIFEST_PATH).is_file())  # a later run can finish
        self.assertFalse((self.root / "CLAUDE.md").exists())
        self.assertIsNone(projects.lookup(self.root))  # the machine detach itself still completed

    def test_de_adopt_works_when_the_wrapper_is_already_gone(self):
        self.adopt()
        plan = self.template_plan()
        (self.root / "tools/eutonos_project.py").unlink()
        result = lifecycle.remove_project(self.root, de_adopt=True, plan=plan)
        self.assertEqual(result["outcome"], "removed", result)
        self.assertFalse((self.root / adoption.MANIFEST_PATH).exists())

    # ------------------------------------------------------------------ checkpoint and resume
    def test_an_interrupted_run_resumes_and_rechecks_the_manifest_hashes(self):
        self.adopt()
        plan = self.template_plan()
        with mock.patch.object(adoption, "apply_removal", side_effect=OSError("disk went away")):
            first = lifecycle.remove_project(self.root, de_adopt=True, plan=plan, checkpoint=False)
        self.assertEqual((first["outcome"], first["blocked_at"]), ("blocked", "reverse-adoption"))
        self.assertEqual(first["checkpoint"]["used"], "local-json")
        saved = json.loads(Path(first["checkpoint"]["path"]).read_text(encoding="utf-8"))
        states = {step["id"]: step["state"] for step in saved["steps"]}
        self.assertEqual((states["reverse-project-entries"], states["reverse-adoption"], states["unregister-project"]),
                         ("done", "blocked", "pending"))
        self.assertIsNotNone(projects.lookup(self.root))  # nothing past the failed step ran
        self.assertEqual(projects.host_state(self.host), "active")
        wrapper = self.root / "tools/eutonos_project.py"  # drift while the run was stopped
        wrapper.write_text(wrapper.read_text(encoding="utf-8") + "# edited meanwhile\n", encoding="utf-8")
        second = lifecycle.remove_project(self.root, de_adopt=True, plan=plan, checkpoint=False)
        self.assertTrue(second["checkpoint"]["resumed"])
        self.assertEqual(second["outcome"], "removed-with-kept")
        self.assertIn("tools/eutonos_project.py", [item["path"] for item in second["kept_repository_files"]])
        self.assertTrue(wrapper.is_file())
        self.assertFalse((self.root / "CLAUDE.md").exists())
        self.assertIsNone(projects.lookup(self.root))
        saved = json.loads(Path(second["checkpoint"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual({step["id"]: step["state"] for step in saved["steps"]}["retire-host"], "done")

    def test_a_failed_withdraw_stops_before_the_host_is_retired_and_resumes(self):
        self.adopt()
        with mock.patch.object(device, "update", side_effect=ts.ReadError("unavailable", "enrollment is busy")):
            first = lifecycle.remove_project(self.root)
        self.assertEqual((first["outcome"], first["blocked_at"], first["reason"]),
                         ("blocked", "withdraw-device-repository", "enrollment is busy"))
        self.assertEqual(projects.host_state(self.host), "active")
        self.assertIsNotNone(projects.lookup(self.root))
        second = lifecycle.remove_project(self.root)
        self.assertEqual(second["outcome"], "removed")
        self.assertEqual(device.read()["shared"], [])
        self.assertEqual(projects.host_state(self.host), "retired")

    def test_resume_after_the_registry_entry_is_gone_still_retires_the_host(self):
        self.adopt()
        with mock.patch.object(projects, "retire_host", side_effect=OSError("interrupted")):
            first = lifecycle.remove_project(self.root)
        self.assertEqual(first["blocked_at"], "retire-host")
        self.assertIsNone(projects.lookup(self.root))  # unregistered already
        second = lifecycle.remove_project(self.root)  # the host is found again from its key
        self.assertEqual(second["outcome"], "removed")
        self.assertEqual(projects.host_state(self.host), "retired")

    def test_it_uses_lane_a_checkpoint_when_present_and_never_replaces_another_checklist(self):
        self.adopt()
        fake = FakeCheckpoint()
        result = lifecycle.remove_project(self.root, checkpoint=fake)
        self.assertEqual(result["checkpoint"]["used"], "setup_checkpoint")
        self.assertEqual(fake.value["checklist"], "remove-repository")
        self.assertEqual({step["id"]: step["state"] for step in fake.value["steps"]}["retire-host"], "done")
        self.assertFalse(list(self.eutonos_home.glob("remove-repository-checkpoint-*")))

    def test_an_unfinished_install_checkpoint_is_left_alone(self):
        self.adopt()
        install = {"profile": "eutonos.setup-checkpoint.v1", "checklist": "install", "root": None,
                   "steps": [{"id": "restart-claude", "kind": "person", "runner": "owner", "state": "pending",
                              "evidence": [], "reason": None, "next_action": "restart"}]}
        fake = FakeCheckpoint(json.loads(json.dumps(install)))
        result = lifecycle.remove_project(self.root, checkpoint=fake)
        self.assertEqual(result["checkpoint"]["used"], "local-json")
        self.assertIn("install", result["checkpoint"]["note"])
        self.assertEqual(fake.value, install)
        self.assertTrue(Path(result["checkpoint"]["path"]).is_file())

    # ------------------------------------------------------------------ add-project wiring
    def test_add_with_consent_records_the_effects_and_detach_reverses_them_byte_identically(self):
        self.adopt(register=False)
        self.record_consent({"codex-record-rule": "off", "set-remote-control": "off"})
        result = lifecycle.add_project(self.root, user_home=self.user)
        self.assertEqual(result["outcome"], "added")
        self.assertTrue(result["permissions"]["applied"])
        recorded = projects.effects_for(self.root)
        self.assertEqual(len(recorded), result["permissions"]["effects"])
        self.assertTrue((self.root / ".claude/settings.local.json").is_file())
        self.assertTrue((self.root / ".codex/config.toml").is_file())
        self.assertFalse((self.root / ".codex/rules/eutonos.rules").exists())  # the recorded choice was off
        self.assertNotEqual(self.exclude.read_bytes(), self.exclude_before)
        self.assertEqual({step["entry"] for step in result["person_steps"]}, {"desktop-folder-auto", "codex-trust"})
        self.assertEqual(list(self.user.iterdir()), [])  # user-wide entries are not this step's
        detached = lifecycle.remove_project(self.root)
        self.assertEqual(detached["outcome"], "removed", detached)
        self.assertEqual(tree(self.root), self.before_entries)
        self.assertEqual(self.exclude.read_bytes(), self.exclude_before)
        self.assertFalse((self.root / ".claude").exists() or (self.root / ".codex").exists())

    def test_add_without_consent_applies_nothing_and_says_so(self):
        self.adopt(register=False)
        for setup in (lambda: None, lambda: self.record_consent(accepted=False)):
            setup()
            result = lifecycle.add_project(self.root, user_home=self.user)
            self.assertEqual(result["permissions"]["applied"], False)
            self.assertEqual(result["permissions"]["message"], "permissions not applied: no install consent")
            self.assertIn("consent", result["permissions"]["next"])
            self.assertEqual(projects.effects_for(self.root), [])
            self.assertEqual(tree(self.root), self.before_entries)
            self.assertEqual(self.exclude.read_bytes(), self.exclude_before)
        self.assertIsNotNone(projects.lookup(self.root))  # the project itself was still registered
        self.assertEqual({step["entry"] for step in result["person_steps"]}, {"desktop-folder-auto", "codex-trust"})

    def test_a_retired_or_damaged_install_record_counts_as_no_consent(self):
        self.record_consent({"send-message": "on"}, state="retired")
        self.assertIsNone(lifecycle.install_consent())
        (self.eutonos_home / "install" / "state.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(lifecycle.install_consent())

    def test_no_permissions_skips_even_with_consent(self):
        self.adopt(register=False)
        self.record_consent({})
        result = lifecycle.add_project(self.root, user_home=self.user, apply_permissions=False)
        self.assertEqual(result["permissions"]["message"], "permissions not applied: --no-permissions")
        self.assertEqual(projects.effects_for(self.root), [])
        self.assertFalse((self.root / ".claude").exists())
        code, value = self.run_cli("add-project", "--root", str(self.root), "--no-permissions")  # already added: idempotent
        self.assertEqual((code, value["outcome"], value["permissions"]["applied"]), (0, "already-added", False))
        self.assertEqual(tree(self.root), self.before_entries)

    def test_the_add_project_command_skips_without_consent(self):
        self.adopt(register=False)
        code, value = self.run_cli("add-project", "--root", str(self.root))  # no consent: nothing near the real home runs
        self.assertEqual((code, value["outcome"], value["permissions"]["message"]),
                         (0, "added", "permissions not applied: no install consent"))

    # ------------------------------------------------------------------ the project permission helper
    def test_applying_project_entries_needs_a_registered_project_and_is_recorded(self):
        with self.assertRaises(ts.ReadError) as caught:
            lifecycle.apply_project_permissions(self.root, user_home=self.user)
        self.assertEqual(caught.exception.code, "missing")
        self.assertFalse((self.root / ".codex").exists())
        self.adopt()
        recorded = projects.effects_for(self.root)
        self.assertEqual([effect["effect_id"] for effect in recorded], [effect["effect_id"] for effect in self.effects])
        self.assertEqual(list(self.user.iterdir()), [])  # the user-wide entries were not applied

    # ------------------------------------------------------------------ the completion checks
    def test_checks_for_add_and_remove_validate_and_start_pending(self):
        for kind in ("add", "remove"):
            checks = lifecycle.repository_checks(kind)
            self.assertEqual(completion_checks.validate(checks), checks)
            self.assertTrue(0 < len(checks) <= 40)
            self.assertEqual({check["state"] for check in checks}, {"pending"})
            self.assertEqual(len({check["id"] for check in checks}), len(checks))
            self.assertTrue(all(check["id"].startswith(kind + ".") and check["evidence"] == [] and check["remaining_action"]
                                for check in checks))
        self.assertEqual(lifecycle.repository_checks("remove"), lifecycle.repository_checks("remove"))  # stable ids
        self.assertIn("remove.host-retired", {check["id"] for check in lifecycle.repository_checks("remove")})
        with self.assertRaises(ts.ReadError):
            lifecycle.repository_checks("update")

    def run_lifecycle(self, *argv):
        raw = io.BytesIO()
        console = io.TextIOWrapper(raw, encoding="utf-8")
        with mock.patch("sys.stdout", console):
            code = lifecycle.main(list(argv))
        console.flush()
        value = json.loads(raw.getvalue().decode("utf-8"))
        console.detach()
        return code, value

    def test_print_checks_prints_exactly_the_checks_a_slice_starts_with_and_changes_nothing(self):
        before = (tree(self.root), sorted(path.as_posix() for path in self.eutonos_home.rglob("*")))
        for kind in ("add", "remove"):
            code, value = self.run_lifecycle("--print-checks", kind, "--root", str(self.root))
            self.assertEqual((code, value), (0, {"completion_checks": lifecycle.repository_checks(kind)}))
        self.assertEqual(before, (tree(self.root), sorted(path.as_posix() for path in self.eutonos_home.rglob("*"))))

    def test_print_preservation_template_prints_the_default_plan_and_changes_nothing(self):
        self.adopt()
        before = tree(self.root)
        code, value = self.run_lifecycle("--print-preservation-template", "--root", str(self.root))
        self.assertEqual(code, 0)
        self.assertEqual(value, adoption.preservation_template(self.local))
        self.assertEqual(tree(self.root), before)
        code, value = self.run_lifecycle("--print-preservation-template", "--root", str(self.base / "nowhere"))
        self.assertEqual((code, value["outcome"]), (2, "error"))


if __name__ == "__main__":
    unittest.main()
