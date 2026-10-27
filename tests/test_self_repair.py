"""Self-repair: local patch, its record and status line, the official update after it, and the read-only self-check.

Every test uses temporary EUTONOS homes, temporary user folders, fake OS ports and throwaway git repositories. Nothing
here touches the real machine, and no real app is asked for its version except through the one mocked Ports test.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from eutonos import doctor, installer, release_source as rs, self_repair as sr
from eutonos import install_record as ir
from eutonos import setup_checkpoint as cp
from eutonos import tokenslang as ts
from eutonos.installer_ports import FakePorts, Ports

import test_installer as ti

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_TERMINAL_PROMPT": "0"}


class Repo:
    """A throwaway checkout: pyproject.toml, src/eutonos and a commit history the tests extend."""

    def __init__(self, root):
        self.root = Path(root)
        (self.root / "src" / "eutonos").mkdir(parents=True)
        (self.root / "pyproject.toml").write_text("[project]\nname='eutonos'\n", encoding="utf-8")
        (self.root / "src" / "eutonos" / "__init__.py").write_text("", encoding="utf-8")
        self.git("init", "-q")
        self.commit("first")

    def git(self, *args):
        done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(self.root), *args],
                              capture_output=True, text=True, env={**os.environ, **GIT_ENV})
        if done.returncode:
            raise AssertionError(f"git {args}: {done.stderr}")
        return done.stdout.strip()

    def commit(self, note) -> str:
        (self.root / "src" / "eutonos" / "note.txt").write_text(note, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", note)
        return self.head()

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")


def release_with_commit(folder, name, *, version, body, commit):
    manifest = ti.make_release(folder, name, version=version, body=body)
    if commit is not None:
        manifest["source_commit"] = commit
    (Path(folder) / name / "eutonos-release.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


class PatchBase(ti.UpdateBase):
    def setUp(self):
        super().setUp()
        self.repo = Repo(self.base / "checkout")
        self.ports.app_versions = {"claude": "2.1.0", "codex": "0.5.0"}
        self.body = "patched"

    def build(self, root, output):
        """Like the real builder, the same input gives the same bytes (the test helper stamps the time otherwise)."""
        cached = self.base / "built" / self.body.replace(" ", "_")
        if not cached.exists():
            ti.build_wheel(cached, version="1.2.3", body=self.body)
        wheel = next(cached.glob("*.whl"))
        Path(output).mkdir(parents=True, exist_ok=True)
        target = Path(output) / wheel.name
        target.write_bytes(wheel.read_bytes())
        return target

    def patch(self, **extra):
        values = self.kw(handover=False, doctor=self.doctor, build=self.build, checkout=self.repo.root,
                         reason="Codex 0.6 renamed a flag")
        values.update(extra)
        for name in ("consent", "prereq"):
            values.pop(name)
        return installer.patch(**values)

    def record(self, gen_id):
        return ir.read_record(self.layout, gen_id)


class PatchTests(PatchBase):
    def test_a_patch_is_a_new_generation_whose_record_says_why_and_from_what(self):
        old = self.first()
        result = self.patch()
        self.assertEqual(result["outcome"], "patched", result)
        record = self.record(result["gen_id"])
        meta = record["local_patch"]
        self.assertEqual(meta["reason"], "Codex 0.6 renamed a flag")
        self.assertEqual(meta["source_commit"], self.repo.head())
        self.assertEqual(Path(meta["checkout"]), self.repo.root.resolve())
        self.assertFalse(meta["dirty"])
        self.assertRegex(meta["diff_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(meta["base_release"], {"version": "1.2.3", "artifact_id": old["artifact_id"]})
        self.assertIn("eutonos/payload.py", meta["changed_files"])
        self.assertNotIn("eutonos/__init__.py", meta["changed_files"])
        self.assertEqual(record["previous_gen"], old["gen_id"])
        state = ir.read_state(self.layout)
        self.assertEqual((state["active"], state["pending"]), (result["gen_id"], None))
        self.assertEqual(result["status_line"], f"local patch active: Codex 0.6 renamed a flag (commit {self.repo.head()[:12]})")
        self.assertNotIn("local_patch", self.record(old["gen_id"]))  # the earlier record is untouched

    def test_status_reports_the_local_patch_line(self):
        self.first()
        self.assertNotIn("local_patch_line", installer.status(home=self.h))
        result = self.patch()
        shown = installer.status(home=self.h)
        self.assertEqual(shown["local_patch_line"], result["status_line"])
        self.assertEqual(shown["active_record"]["local_patch"]["reason"], "Codex 0.6 renamed a flag")

    def test_a_dirty_checkout_is_refused_unless_allowed(self):
        old = self.first()
        (self.repo.root / "src" / "eutonos" / "wip.py").write_text("x = 1\n", encoding="utf-8")
        refused = self.patch()
        self.assertEqual((refused["outcome"], refused["reason"]), ("refused", "dirty-checkout"), refused)
        self.assertIn("src/eutonos/wip.py", refused["detail"])
        self.assertEqual(ir.read_state(self.layout)["active"], old["gen_id"])
        allowed = self.patch(allow_dirty=True)
        self.assertEqual(allowed["outcome"], "patched", allowed)
        self.assertTrue(self.record(allowed["gen_id"])["local_patch"]["dirty"])
        self.assertIn("uncommitted", allowed["status_line"])

    def test_dry_run_builds_and_shows_the_plan_but_writes_nothing(self):
        self.seed_user_files()
        self.first()
        home_before, user_before = ti.settled(self.h), ti.snapshot(self.user)
        result = self.patch(dry_run=True)
        self.assertEqual((result["outcome"], result["changes_nothing"]), ("dry-run", True), result)
        self.assertEqual(result["local_patch"]["source_commit"], self.repo.head())
        self.assertIn("eutonos/payload.py", result["local_patch"]["changed_files"])
        self.assertEqual(ti.settled(self.h), home_before)
        self.assertEqual(ti.snapshot(self.user), user_before)

    def test_refusals_leave_everything_as_it_was(self):
        self.assertEqual(self.patch()["reason"], "not-installed")
        old = self.first()
        self.assertEqual(self.patch(reason="   ")["reason"], "invalid")
        not_a_checkout = Repo(self.base / "other")
        (not_a_checkout.root / "pyproject.toml").unlink()
        refused = self.patch(checkout=not_a_checkout.root)
        self.assertEqual((refused["outcome"], refused["reason"]), ("refused", "invalid"), refused)
        self.assertEqual(ir.read_state(self.layout)["active"], old["gen_id"])

    def test_a_build_that_fails_changes_nothing(self):
        old = self.first()

        def broken(root, output):
            raise ts.ReadError("build-failed", "no wheel")

        result = self.patch(build=broken)
        self.assertEqual((result["outcome"], result["reason"]), ("refused", "build-failed"), result)
        self.assertEqual(ir.read_state(self.layout)["active"], old["gen_id"])

    def test_the_same_wheel_is_not_installed_twice(self):
        self.first()
        self.assertEqual(self.patch()["outcome"], "patched")
        again = self.patch()
        self.assertEqual(again["outcome"], "up-to-date", again)
        self.assertIn("nothing was changed", again["note"])

    def test_a_patch_that_fails_to_apply_leaves_the_earlier_generation_active(self):
        manifest, _ = ti.fake_component(self, "fakecomp")
        old = self.first(components=[manifest])

        def broken(ctx, effects):
            raise RuntimeError("boom")

        ti.sys.modules["fake_fakecomp"].apply = broken
        result = self.patch(components=[manifest])
        self.assertEqual(result["outcome"], "rolled-back", result)
        self.assertEqual(ir.read_state(self.layout)["active"], old["gen_id"])

    def test_install_and_update_record_the_app_versions(self):
        old = self.first()
        apps = self.record(old["gen_id"])["app_versions"]["apps"]
        self.assertEqual(apps["claude"], {"state": "ok", "version": "2.1.0"})
        self.assertEqual(apps["codex"], {"state": "ok", "version": "0.5.0"})
        self.assertEqual(apps["hermes"], {"state": "not-found", "version": None})
        self.assertEqual(apps["codex-store"], {"state": "not-found", "version": None})  # its own entry, not "codex"
        self.ports.app_versions["claude"] = "2.2.0"
        ti.make_release(self.releases, "r2", version="1.3.0", body="two")
        new = self.update()
        self.assertEqual(self.record(new["gen_id"])["app_versions"]["apps"]["claude"]["version"], "2.2.0")


class RollbackAfterPatchTests(PatchBase):
    def test_rollback_leaves_the_patch_and_says_so(self):
        old = self.first()
        patched = self.patch()
        result = self.rollback()
        self.assertEqual((result["outcome"], result["gen_id"]), ("rolled-back-to", old["gen_id"]), result)
        self.assertEqual(result["left_local_patch"]["reason"], "Codex 0.6 renamed a flag")
        self.assertEqual(self.record(patched["gen_id"])["state"], "rolled-back")
        self.assertIn("local_patch", self.record(patched["gen_id"]))  # kept as history

    def test_rollback_official_goes_to_the_official_generation_before_the_patch(self):
        old = self.first()
        self.patch()
        dry = self.rollback(official=True, dry_run=True)
        self.assertEqual((dry["outcome"], dry["to"]), ("dry-run", old["gen_id"]), dry)
        result = self.rollback(official=True)
        self.assertEqual((result["outcome"], result["gen_id"]), ("rolled-back-to", old["gen_id"]), result)

    def test_rollback_official_refuses_when_nothing_is_patched(self):
        self.first()
        ti.make_release(self.releases, "r2", version="1.3.0", body="two")
        self.update()
        refused = self.rollback(official=True)
        self.assertEqual((refused["outcome"], refused["reason"]), ("refused", "not-a-local-patch"), refused)
        self.assertEqual(self.rollback()["outcome"], "rolled-back-to")  # plain rollback behaves as before

    def test_rollback_official_refuses_when_the_generation_before_is_also_a_patch(self):
        self.first()
        first = self.patch()
        self.body = "patched again"
        second = self.patch(reason="second try")
        self.assertEqual(self.record(second["gen_id"])["local_patch"]["base_release"],
                         self.record(first["gen_id"])["local_patch"]["base_release"])  # still the official build
        refused = self.rollback(official=True)
        self.assertEqual((refused["outcome"], refused["reason"]), ("refused", "previous-is-also-a-local-patch"), refused)
        self.assertEqual(ir.read_state(self.layout)["active"], second["gen_id"])
        plain = self.rollback()  # the ordinary rollback still goes one step back
        self.assertEqual(plain["gen_id"], first["gen_id"])


class OfficialUpdateAfterPatchTests(PatchBase):
    def patched_at(self, commit_note=None):
        self.first()
        if commit_note:
            self.repo.commit(commit_note)
        return self.patch()

    def test_included_only_when_the_patch_commit_is_an_ancestor_of_the_release_commit(self):
        patched = self.patched_at()
        later = self.repo.commit("a later commit")
        release_with_commit(self.releases, "r2", version="1.3.0", body="two", commit=later)
        plan = self.update(dry_run=True)
        self.assertEqual(plan["replaces_local_patch"]["fix"], sr.FIX_INCLUDED, plan)
        result = self.update()
        self.assertEqual(result["outcome"], "updated", result)
        self.assertEqual(result["replaced_local_patch"]["fix"], sr.FIX_INCLUDED)
        self.assertEqual(result["replaced_local_patch"]["source_commit"], self.record(patched["gen_id"])["local_patch"]["source_commit"])
        record = self.record(result["gen_id"])
        self.assertNotIn("local_patch", record)
        self.assertEqual(record["replaced_local_patch"]["fix"], sr.FIX_INCLUDED)
        self.assertNotIn("local_patch_line", installer.status(home=self.h))

    def test_unknown_when_the_release_names_no_commit(self):
        self.patched_at()
        release_with_commit(self.releases, "r2", version="1.3.0", body="two", commit=None)
        result = self.update()
        self.assertEqual(result["replaced_local_patch"]["fix"], sr.FIX_UNKNOWN, result)
        self.assertIn("no source commit", result["replaced_local_patch"]["detail"])

    def test_unknown_when_the_commit_is_not_an_ancestor(self):
        self.patched_at("the patch commit")
        self.repo.git("checkout", "-q", "-b", "elsewhere", "HEAD~1")
        other = self.repo.commit("a divergent commit")
        release_with_commit(self.releases, "r2", version="1.3.0", body="two", commit=other)
        result = self.update()
        self.assertEqual(result["replaced_local_patch"]["fix"], sr.FIX_UNKNOWN, result)
        self.assertEqual(result["outcome"], "updated")  # the update itself still goes ahead

    def test_unknown_when_the_release_commit_is_not_in_the_checkout(self):
        self.patched_at()
        release_with_commit(self.releases, "r2", version="1.3.0", body="two", commit="b" * 40)
        self.assertEqual(self.update()["replaced_local_patch"]["fix"], sr.FIX_UNKNOWN)

    def test_unknown_when_the_checkout_is_gone(self):
        self.patched_at()
        later = self.repo.commit("a later commit")
        release_with_commit(self.releases, "r2", version="1.3.0", body="two", commit=later)
        self.repo.root.rename(self.base / "moved")
        result = self.update()
        self.assertEqual(result["replaced_local_patch"]["fix"], sr.FIX_UNKNOWN, result)
        self.assertIn("not on this machine", result["replaced_local_patch"]["detail"])

    def test_unknown_when_the_patch_was_built_from_uncommitted_changes(self):
        self.first()
        (self.repo.root / "src" / "eutonos" / "wip.py").write_text("x = 1\n", encoding="utf-8")
        self.patch(allow_dirty=True)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "later")
        release_with_commit(self.releases, "r2", version="1.3.0", body="two", commit=self.repo.head())
        result = self.update()
        self.assertEqual(result["replaced_local_patch"]["fix"], sr.FIX_UNKNOWN, result)
        self.assertIn("uncommitted", result["replaced_local_patch"]["detail"])

    def test_an_update_over_an_ordinary_generation_reports_nothing_about_patches(self):
        self.first()
        ti.make_release(self.releases, "r2", version="1.3.0", body="two")
        result = self.update()
        self.assertNotIn("replaced_local_patch", result)
        self.assertNotIn("replaces_local_patch", self.update(dry_run=True))

    def test_the_fix_verdict_helper_never_says_included_on_a_damaged_record(self):
        self.assertEqual(sr.fix_verdict(None, "a" * 40)["fix"], sr.FIX_UNKNOWN)
        self.assertEqual(sr.fix_verdict({"reason": "x", "dirty": False, "checkout": "", "source_commit": "a" * 40},
                                        "b" * 40)["fix"], sr.FIX_UNKNOWN)


class ManifestAndHelperTests(unittest.TestCase):
    def manifest(self, **change):
        value = {"profile": "eutonos.release.v1", "version": "1.0.0", "artifact_id": "eutonos-1.0.0-abcdef012345",
                 "wheel": "eutonos-1.0.0-py3-none-any.whl", "sha256": "a" * 64, "channels": ["stable"],
                 "min_compatible": None}
        value.update(change)
        return value

    def test_source_commit_is_optional_and_checked(self):
        self.assertNotIn("source_commit", rs.validate_manifest(self.manifest()))
        self.assertEqual(rs.validate_manifest(self.manifest(source_commit="a" * 40))["source_commit"], "a" * 40)
        self.assertIsNone(rs.validate_manifest(self.manifest(source_commit=None))["source_commit"])
        for bad in ("abc", "A" * 40, "g" * 40, 7, "a" * 41):
            with self.assertRaises(ts.ReadError):
                rs.validate_manifest(self.manifest(source_commit=bad))

    def test_parse_version(self):
        self.assertEqual(sr.parse_version("2.1.5 (Claude Code)\n"), "2.1.5")
        self.assertEqual(sr.parse_version("codex-cli 0.46.0"), "0.46.0")
        self.assertEqual(sr.parse_version("Hermes Agent v0.9.1-beta.2\nextra 9.9.9"), "0.9.1-beta.2")
        self.assertEqual(sr.parse_version("v1.2"), "1.2")
        for nothing in ("", None, "no digits here", "7"):
            self.assertIsNone(sr.parse_version(nothing))

    def test_the_reason_is_cleaned_and_required(self):
        self.assertEqual(sr.clean_reason("  two\n lines  "), "two lines")
        self.assertEqual(len(sr.clean_reason("x" * 1000)), sr.MAX_REASON)
        with self.assertRaises(ts.ReadError):
            sr.clean_reason("")

    def test_the_patch_line_of_a_damaged_or_missing_patch_is_none(self):
        for value in (None, "x", {}, {"reason": 3}):
            self.assertIsNone(sr.patch_line(value))
        self.assertEqual(sr.patch_line({"reason": "r", "source_commit": "a" * 40, "dirty": False}),
                         "local patch active: r (commit " + "a" * 12 + ")")

    def test_the_real_ports_read_a_version_from_the_apps_own_flag(self):
        ports = Ports("macos")
        with mock.patch.object(Ports, "which", return_value=None):
            self.assertEqual(ports.app_version("claude"), {"state": "not-found", "version": None})
        with mock.patch.object(Ports, "which", return_value="/bin/claude"):
            with mock.patch.object(Ports, "run", return_value=(0, "2.1.5 (Claude Code)\n", "")) as run:
                self.assertEqual(ports.app_version("claude"), {"state": "ok", "version": "2.1.5"})
                self.assertEqual(run.call_args.args[0], ["/bin/claude", "--version"])
                self.assertEqual(run.call_args.kwargs["input_text"], "")
            with mock.patch.object(Ports, "run", return_value=(1, "", "bad")):
                self.assertEqual(ports.app_version("claude")["state"], "unreadable")
            with mock.patch.object(Ports, "run", return_value=(0, "no number", "")):
                self.assertEqual(ports.app_version("claude")["state"], "unreadable")
            with mock.patch.object(Ports, "run", side_effect=OSError("denied")):
                self.assertEqual(ports.app_version("claude")["state"], "unreadable")

    def test_the_codex_store_app_is_read_from_its_package_under_its_own_name(self):
        ports = Ports("windows")
        with mock.patch.object(Ports, "which", return_value=None) as which:
            with mock.patch.object(Ports, "run", return_value=(0, "26.1016.8622.0", "")) as run:
                self.assertEqual(ports.app_version("codex-store"), {"state": "ok", "version": "26.1016.8622.0"})
            self.assertEqual(run.call_count, 1)
            which.assert_not_called()  # a Store entry is never read through a command on PATH
            argv = run.call_args.args[0]
            self.assertEqual(argv[:3], ["powershell.exe", "-NoProfile", "-NonInteractive"])
            self.assertEqual(run.call_args.kwargs["env"]["EUTONOS_APPX_PACKAGE"], "OpenAI.Codex")
            self.assertIn("Get-AppxPackage", argv[-1])
            for changes in ("Add-Appx", "Remove-Appx", "Reset-Appx", "Start-Process", "Invoke-", "Set-"):
                self.assertNotIn(changes, argv[-1])  # only a read of the installed packages
            with mock.patch.object(Ports, "run", return_value=(0, "\r\n", "")):
                self.assertEqual(ports.app_version("codex-store"), {"state": "not-found", "version": None})  # no package
            with mock.patch.object(Ports, "run", return_value=(1, "", "no Appx module")):
                self.assertEqual(ports.app_version("codex-store")["state"], "unreadable")
            with mock.patch.object(Ports, "run", side_effect=OSError("no powershell")):
                self.assertEqual(ports.app_version("codex-store")["state"], "unreadable")
            with mock.patch.object(Ports, "run", return_value=(0, "no number", "")):
                self.assertEqual(ports.app_version("codex-store")["state"], "unreadable")

    def test_the_codex_command_and_the_codex_store_app_never_stand_in_for_each_other(self):
        windows = Ports("windows")
        with mock.patch.object(Ports, "which", return_value="C:/bin/codex.exe"):
            with mock.patch.object(Ports, "run", return_value=(0, "codex-cli 0.5.0\n", "")) as run:
                self.assertEqual(windows.app_version("codex"), {"state": "ok", "version": "0.5.0"})
            self.assertEqual(run.call_count, 1)  # the Store package was not read
            with mock.patch.object(Ports, "run", return_value=(1, "", "stub")) as run:
                self.assertEqual(windows.app_version("codex")["state"], "unreadable")  # a failing command stub
            self.assertEqual(run.call_count, 1)  # ... is not answered with the Store app's version
        with mock.patch.object(Ports, "which", return_value=None):
            with mock.patch.object(Ports, "run") as run:
                self.assertEqual(windows.app_version("codex"), {"state": "not-found", "version": None})
            run.assert_not_called()  # no command and the Store app is installed: the command is still not found
        # both installed, at different versions: each entry reports its own
        with mock.patch.object(Ports, "which", return_value="C:/bin/codex.exe"):
            answers = {"C:/bin/codex.exe": (0, "codex-cli 0.5.0\n", ""), "powershell.exe": (0, "26.1016.8622.0", "")}
            with mock.patch.object(Ports, "run", side_effect=lambda argv, **k: answers[argv[0]]):
                self.assertEqual(windows.app_version("codex")["version"], "0.5.0")
                self.assertEqual(windows.app_version("codex-store")["version"], "26.1016.8622.0")

    def test_other_apps_and_other_systems_never_ask_the_store(self):
        windows = Ports("windows")
        with mock.patch.object(Ports, "which", return_value=None):
            for ports, name in ((windows, "claude"), (windows, "hermes"), (Ports("macos"), "codex"),
                                (Ports("macos"), "codex-store")):
                with mock.patch.object(Ports, "run") as run:
                    self.assertEqual(ports.app_version(name), {"state": "not-found", "version": None})
                run.assert_not_called()

    def test_an_unreadable_app_never_stops_the_record_from_being_written(self):
        class Raising(FakePorts):
            def app_version(self, name):
                raise RuntimeError("boom")

        found = sr.read_app_versions(Raising("macos"))
        self.assertEqual({row["state"] for row in found["apps"].values()}, {"unreadable"})


def good_report():
    return {"declared": {"permissions": {"items": [{"id": "send-message", "app": "claude", "feature": "send", "required": True,
                                                    "selected": True, "state": "ok"}]},
                         "components": [], "effects": []},
            "undeclared": []}


def broken_report(app="claude", item="send-message"):
    report = good_report()
    report["declared"]["permissions"]["items"] = [{"id": item, "app": app, "feature": "send", "required": True,
                                                    "selected": True, "state": "missing",
                                                    "reason": "the declared setting is not present"}]
    return report


QUIET = {"installed": True, "messaging": "on", "halt": False, "denied": False, "reason": None}


class SelfCheckTests(PatchBase):
    def check(self, report=None, crosstalk=QUIET, **extra):
        with mock.patch.object(sr, "_crosstalk_state", return_value=dict(crosstalk)):
            return sr.self_check(home=self.h, user_home=self.user, root=self.base, ports=self.ports,
                                 report=report or good_report(), **extra)

    def test_nothing_changed_and_nothing_broken(self):
        self.first()
        result = self.check()
        self.assertEqual((result["outcome"], result["action"], result["likely_broken"]), ("ok", "none", None), result)
        self.assertEqual({row["state"] for name, row in result["apps"].items() if name != "hermes"}, {"unchanged"})
        self.assertIn("crosstalk_send", result["next_action"])
        self.assertTrue(result["changes_nothing"])
        self.assertEqual(result["checklist"], "repair")

    def test_doctor_subsystem_fallback_cannot_become_healthy_empty_items(self):
        self.first()
        fallback = {"active_record": {"state": "unknown"},
                    "declared": {"permissions": {"state": "unknown", "items": []}, "components": [], "effects": []},
                    "readiness": {"state": "unknown", "ready": False}}
        result = self.check(fallback)
        self.assertEqual((result["outcome"], result["action"]), ("unknown", "wait"))
        self.assertTrue(any(item["id"] == "doctor-unreadable" for item in result["findings"]))

    def test_a_broken_piece_after_an_app_update_says_patch(self):
        self.first()
        self.ports.app_versions["claude"] = "2.2.0"
        result = self.check(broken_report("claude"))
        self.assertEqual((result["outcome"], result["action"]), ("attention", "patch"), result)
        self.assertIn("claude 2.1.0 -> 2.2.0", result["likely_broken"])
        self.assertIn("eutonos-install patch --checkout", result["next_action"])
        self.assertIn("crosstalk_send", result["next_action"])
        self.assertEqual(result["apps"]["claude"]["state"], "changed")

    def test_a_broken_piece_of_another_app_does_not_blame_the_changed_app(self):
        self.first()
        self.ports.app_versions["claude"] = "2.2.0"
        result = self.check(broken_report("codex"))
        self.assertEqual(result["action"], "patch")  # no earlier generation to go back to
        self.assertIn("no earlier EUTONOS generation", result["likely_broken"])
        self.assertNotIn("2.1.0 -> 2.2.0", result["likely_broken"])

    def test_a_broken_piece_with_no_app_change_says_rollback_when_there_is_a_generation_to_go_back_to(self):
        self.first()
        ti.make_release(self.releases, "r2", version="1.3.0", body="two")
        self.update()
        result = self.check(broken_report("claude"))
        self.assertEqual(result["action"], "rollback", result)
        self.assertIn("newest EUTONOS generation", result["likely_broken"])
        self.assertTrue(result["rollback"]["possible"])
        self.assertIn("rollback --dry-run", result["next_action"])

    def test_after_a_patch_the_rollback_advice_names_official(self):
        self.first()
        self.patch()
        result = self.check(broken_report("claude"))
        self.assertEqual(result["action"], "rollback", result)
        self.assertIn("rollback --official", result["next_action"])
        self.assertIn("local patch active", result["active_record"]["local_patch_line"])

    def test_owner_controls_say_wait(self):
        self.first()
        halted = self.check(crosstalk={**QUIET, "halt": True})
        self.assertEqual((halted["outcome"], halted["action"]), ("ok", "wait"), halted)
        self.assertIn("halted", halted["findings"][0]["plain"])
        off = self.check(crosstalk={**QUIET, "messaging": "off"})
        self.assertEqual(off["action"], "wait")
        absent = self.check(crosstalk={**QUIET, "installed": False})
        self.assertEqual(absent["action"], "none")

    def test_an_unreadable_setting_says_wait_and_not_patch(self):
        self.first()
        report = broken_report("claude")
        report["declared"]["permissions"]["items"][0].update(state="unknown", reason="could not read the file")
        result = self.check(report)
        self.assertEqual((result["outcome"], result["action"]), ("unknown", "wait"), result)

    def test_a_missing_component_is_blamed_on_the_apps_it_needs(self):
        self.first()
        self.ports.app_versions["codex"] = "0.6.0"
        report = good_report()
        report["declared"]["components"] = [{"id": "crosstalk", "selected": True, "required": True, "state": "missing",
                                             "reason": "the readback found nothing"}]
        with mock.patch.object(sr, "_component_apps", return_value={"crosstalk": {"claude", "codex"}}):
            result = self.check(report)
        self.assertEqual(result["action"], "patch", result)
        self.assertIn("codex 0.5.0 -> 0.6.0", result["likely_broken"])

    def test_a_missing_recorded_file_is_blamed_through_its_permission_entry(self):
        self.first()
        self.ports.app_versions["codex"] = "0.6.0"
        report = good_report()
        report["declared"]["permissions"]["items"][0].update(id="codex-rule", app="codex")
        report["declared"]["effects"] = [{"effect_id": "perm.codex", "state": "missing", "permission_entry": "codex-rule"}]
        result = self.check(report)
        self.assertEqual(result["action"], "patch", result)
        self.assertIn("perm.codex", result["findings"][0]["piece"])

    def test_pending_and_not_installed_runs_are_named(self):
        result = self.check()  # nothing installed yet
        self.assertEqual((result["outcome"], result["action"]), ("attention", "install"), result)
        manifest, _ = ti.fake_component(self, "fakecomp", fail="interrupt")
        with self.assertRaises(KeyboardInterrupt):
            self.install(components=[manifest])
        pending = self.check()
        self.assertEqual(pending["action"], "resume", pending)

    def test_an_old_record_without_app_versions_still_checks(self):
        old = self.first()
        state = ir.read_state(self.layout)
        record = self.record(old["gen_id"])
        record.pop("app_versions")
        ir.write_record(self.layout, state, record)
        ir.write_state(self.layout, state)
        result = self.check()
        self.assertEqual({row["state"] for row in result["apps"].values()}, {"not-recorded"})
        self.assertEqual(result["outcome"], "ok")
        self.assertIsNone(result["app_versions_recorded_at"])

    def test_an_app_that_vanished_or_cannot_be_read_is_a_note_not_a_fault(self):
        self.first()
        self.ports.app_versions["claude"] = False
        del self.ports.app_versions["codex"]
        result = self.check()
        self.assertEqual((result["outcome"], result["action"]), ("ok", "wait"), result)
        ids = {item["id"] for item in result["findings"]}
        self.assertEqual(ids, {"app-claude-unreadable", "app-codex-disappeared"})

    def test_a_changed_app_with_nothing_broken_is_only_a_note(self):
        self.first()
        self.ports.app_versions["claude"] = "9.9.9"
        result = self.check()
        self.assertEqual((result["outcome"], result["action"]), ("ok", "none"), result)
        self.assertEqual(result["findings"][0]["id"], "app-claude-changed")

    def test_the_codex_command_and_the_codex_store_app_are_checked_separately(self):
        self.ports.app_versions["codex-store"] = "26.1016.0.0"
        record = self.record(self.first()["gen_id"])["app_versions"]["apps"]
        self.assertEqual((record["codex"]["version"], record["codex-store"]["version"]), ("0.5.0", "26.1016.0.0"))
        quiet = self.check()
        self.assertEqual((quiet["apps"]["codex"]["state"], quiet["apps"]["codex-store"]["state"]),
                         ("unchanged", "unchanged"))
        self.ports.app_versions["codex-store"] = "26.1020.0.0"  # only the Store app updated
        store = self.check()
        self.assertEqual((store["apps"]["codex"]["state"], store["apps"]["codex-store"]["state"]),
                         ("unchanged", "changed"))
        self.assertEqual([item["id"] for item in store["findings"]], ["app-codex-store-changed"])
        self.assertIn("codex Store app changed from 26.1016.0.0 to 26.1020.0.0", store["findings"][0]["plain"])
        self.ports.app_versions.update({"codex-store": "26.1016.0.0", "codex": "0.6.0"})  # only the command updated
        command = self.check()
        self.assertEqual((command["apps"]["codex"]["state"], command["apps"]["codex-store"]["state"]),
                         ("changed", "unchanged"))
        self.assertEqual([item["id"] for item in command["findings"]], ["app-codex-changed"])

    def test_a_broken_codex_piece_is_blamed_on_a_changed_codex_store_app_by_its_family(self):
        self.ports.app_versions["codex-store"] = "26.1016.0.0"
        self.first()
        self.ports.app_versions["codex-store"] = "26.1020.0.0"
        result = self.check(broken_report("codex"))
        self.assertEqual((result["outcome"], result["action"]), ("attention", "patch"), result)
        self.assertIn("codex Store 26.1016.0.0 -> 26.1020.0.0", result["likely_broken"])
        other = self.check(broken_report("claude"))  # a Claude piece is not blamed on a Codex app
        self.assertNotIn("26.1016.0.0", other["likely_broken"])

    def test_the_codex_store_app_going_away_is_a_note_and_an_old_record_without_it_is_not_a_change(self):
        self.ports.app_versions["codex-store"] = "26.1016.0.0"
        old = self.first()
        del self.ports.app_versions["codex-store"]
        gone = self.check()
        self.assertEqual({item["id"] for item in gone["findings"]}, {"app-codex-store-disappeared"})
        self.assertEqual(gone["action"], "wait")
        self.assertIn("codex Store app version can no longer be found", gone["findings"][0]["plain"])
        # a record written before codex-store existed (its codex entry may even hold the Store version) must not
        # read the Store app as having changed
        state = ir.read_state(self.layout)
        record = self.record(old["gen_id"])
        record["app_versions"]["apps"].pop("codex-store")
        ir.write_record(self.layout, state, record)
        ir.write_state(self.layout, state)
        self.ports.app_versions["codex-store"] = "26.1016.0.0"
        result = self.check()
        self.assertEqual(result["apps"]["codex-store"]["state"], "not-recorded")
        self.assertEqual((result["outcome"], result["action"]), ("ok", "none"), result)

    def test_the_self_check_changes_nothing(self):
        self.seed_user_files()
        self.first()
        self.ports.app_versions["claude"] = "2.2.0"
        home_before, user_before, calls = ti.settled(self.h), ti.snapshot(self.user), list(self.ports.calls)
        self.check(broken_report("claude"))
        self.assertEqual(ti.settled(self.h), home_before)
        self.assertEqual(ti.snapshot(self.user), user_before)
        self.assertEqual(self.ports.calls, calls)  # no process was started

    def test_the_crosstalk_reader_restores_the_environment_and_reads_the_home(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CROSSTALK_P0_DEVICE_DIR", None)
            state = sr._crosstalk_state(self.h)
            self.assertNotIn("CROSSTALK_P0_DEVICE_DIR", os.environ)
        self.assertFalse(state["installed"])
        self.assertEqual(set(state), {"installed", "messaging", "halt", "denied", "reason"})

    def test_the_text_view_ends_with_the_next_action(self):
        self.first()
        text = sr.format_check(self.check(broken_report("claude")))
        self.assertTrue(text.startswith("EUTONOS self-check: attention"))
        self.assertIn("\nNext: ", text)


class DoctorAndCliTests(PatchBase):
    def env(self):
        local = self.base / "local"
        local.mkdir(exist_ok=True)
        patcher = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(local), "EUTONOS_HOME": str(self.h),
                                                "HOME": str(self.user), "USERPROFILE": str(self.user),
                                                "CODEX_HOME": str(self.user / ".codex"),
                                                "CLAUDE_CONFIG_DIR": str(self.user / ".claude")})
        patcher.start()
        self.addCleanup(patcher.stop)
        guards = mock.patch.multiple(doctor, _read_run_items=mock.DEFAULT, _git_readback=mock.DEFAULT,
                                     _account_readback=mock.DEFAULT)
        mocks = guards.start()
        self.addCleanup(guards.stop)
        mocks["_read_run_items"].return_value = ([], None)
        mocks["_git_readback"].return_value = {"state": "ok", "values": {"safe.directory": [], "core.autocrlf": []}}
        mocks["_account_readback"].return_value = {"state": "unknown", "reason": "fixture"}

    def run_main(self, *argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), mock.patch.object(
                Ports, "app_version", return_value={"state": "not-found", "version": None}):
            code = installer.main(["--home", str(self.h), *argv])
        return code, buffer.getvalue()

    def test_doctor_shows_the_patch_line_and_text(self):
        self.env()
        self.first()
        self.assertIsNone(doctor.report(self.h, self.user, self.base)["active_record"].get("local_patch_line"))
        result = self.patch()
        report = doctor.report(self.h, self.user, self.base)
        self.assertEqual(report["active_record"]["local_patch_line"], result["status_line"])
        self.assertEqual(report["active_record"]["local_patch"]["reason"], "Codex 0.6 renamed a flag")
        self.assertIn(result["status_line"], doctor._short_text(report))

    def test_the_repair_checklist_is_found_by_doctor(self):
        self.assertIn("repair", doctor._CHECKLISTS)
        self.assertIn("repair", cp.CHECKLISTS)
        self.assertTrue(sr.REPAIR_STEPS)

    def test_the_commands_parse(self):
        parser = installer._parser()
        args = parser.parse_args(["patch", "--checkout", "C", "--reason", "R", "--allow-dirty", "--dry-run"])
        self.assertEqual((args.command, args.checkout, args.reason, args.allow_dirty, args.dry_run), ("patch", "C", "R", True, True))
        self.assertTrue(parser.parse_args(["rollback", "--official"]).official)
        self.assertFalse(parser.parse_args(["rollback"]).official)
        args = parser.parse_args(["self-check", "--json", "--user-home", "U"])
        self.assertEqual((args.json, args.user_home), (True, "U"))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(["patch", "--checkout", "C"])  # a patch needs its reason

    def test_self_check_from_the_command_line_with_a_real_doctor_reads_and_writes_nothing(self):
        self.env()
        self.seed_user_files()
        self.first()
        home_before, user_before = ti.settled(self.h), ti.snapshot(self.user)
        code, out = self.run_main("self-check", "--json", "--user-home", str(self.user))
        self.assertEqual(code, 0)
        found = json.loads(out)
        self.assertEqual(found["profile"], sr.SELF_CHECK_PROFILE)
        self.assertIn(found["outcome"], ("ok", "attention"))
        self.assertIn(found["action"], ("none", "wait", "rollback", "patch"))
        code, text = self.run_main("self-check", "--user-home", str(self.user))
        self.assertEqual(code, 0)
        self.assertTrue(text.startswith("EUTONOS self-check: "))
        self.assertEqual(ti.settled(self.h), home_before)
        self.assertEqual(ti.snapshot(self.user), user_before)

    def test_patch_from_the_command_line_refuses_a_bad_checkout_with_exit_1(self):
        self.first()
        code, out = self.run_main("patch", "--checkout", str(self.base / "nowhere"), "--reason", "r")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["reason"], "invalid")

    def test_rollback_official_from_the_command_line(self):
        self.first()
        code, out = self.run_main("rollback", "--official")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["reason"], "no-previous-generation")


if __name__ == "__main__":
    unittest.main()
