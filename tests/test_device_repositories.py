import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from eutonos import device_repositories as device, tokenslang as ts


class DeviceRepositoriesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="device-repositories-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "device"
        self.root = self.repository("first")

    def git(self, root, *args):
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        return subprocess.check_output(["git", "-c", "safe.directory=" + str(root), "-C", str(root), *args],
                                       env=env, stderr=subprocess.PIPE)

    def repository(self, name, *, ident=None):
        root = self.base / name
        root.mkdir()
        ident = ident or {"repository_id": "repo", "scope_id": "scope", "namespace": "native"}
        (root / "eutonos.read.json").write_text(json.dumps({"profile": ts.CATALOG, **ident}), encoding="utf-8")
        (root / "marker").write_text(name, encoding="utf-8")
        self.git(root, "init", "-q", "-b", "main")
        self.git(root, "add", ".")
        self.git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "native baseline")
        return root

    def update(self, operation, locator=None, **kw):
        current = device.read(home=self.home)
        request = {"profile": device.REQUEST, "operation": operation, "locator": str(locator or self.root), **kw}
        return device.update(request, expected_revision=current["revision"], home=self.home)

    def test_enroll_form_hash_conflict_and_no_scan(self):
        self.assertEqual(device.read(home=self.home)["shared"], [])
        self.assertFalse(self.home.exists())
        form = device.form("enroll", {"locator": str(self.root)}, home=self.home)
        self.assertEqual(form["missing_fields"], [])
        self.assertFalse(self.home.exists())
        result = device.update(form["request"], expected_revision=form["expected_revision"], home=self.home)
        self.assertTrue(result["current_verified"])
        self.assertEqual(result["shared"][0]["identity"]["repository_id"], "repo")
        with self.assertRaises(ts.ReadError) as caught:
            device.update(form["request"], expected_revision=form["expected_revision"], home=self.home)
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(device.read(home=self.home)["revision"], result["revision"])

    def test_reused_identity_keeps_separate_sources_and_refuses_association(self):
        self.update("enroll")
        other = self.repository("unrelated")
        with self.assertRaises(ts.ReadError) as caught:
            self.update("enroll", other, associate_with=str(self.root))
        self.assertEqual(caught.exception.code, "ambiguous")
        result = self.update("enroll", other)
        self.assertEqual(len(result["shared"]), 2)
        self.assertEqual(result["shared"][0]["identity"], result["shared"][1]["identity"])

    def test_clone_association_selected_source_and_divergent_native_pins(self):
        self.update("enroll")
        clone = self.base / "clone"
        self.git(self.base, "clone", "-q", str(self.root), str(clone))
        (clone / "marker").write_text("new native observation", encoding="utf-8")
        self.git(clone, "add", ".")
        self.git(clone, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "diverge")
        result = self.update("enroll", clone, associate_with=str(self.root))
        self.assertEqual(len(result["shared"]), 1)
        entry = result["shared"][0]
        self.assertEqual(entry["selected_locator"], str(clone))
        self.assertEqual(entry["association"][str(clone)]["kind"], "commit-lineage")
        self.assertEqual(device.verify(entry), "verified")

    def test_common_worktree_association(self):
        self.update("enroll")
        other = self.base / "worktree"
        self.git(self.root, "worktree", "add", "-q", "-b", "parallel", str(other))
        result = self.update("enroll", other, associate_with=str(self.root))
        self.assertEqual(result["shared"][0]["association"][str(other)]["kind"], "git-common")

    def test_move_and_withdraw_invalidate_generation_without_deleting_source(self):
        original = self.update("enroll")
        moved = self.base / "moved"
        shutil.move(str(self.root), moved)
        self.assertEqual(device.verify(original["shared"][0]), "unknown")
        relocated = self.update("relocate", self.root, new_locator=str(moved))
        self.assertEqual(relocated["shared"][0]["selected_locator"], str(moved))
        self.assertEqual(relocated["shared"][0]["identity"], original["shared"][0]["identity"])
        withdrawn = self.update("withdraw", moved)
        self.assertGreater(withdrawn["cache_generation"], relocated["cache_generation"])
        self.assertEqual(withdrawn["shared"], [])
        self.assertTrue((moved / "eutonos.read.json").exists())

    def test_selected_clone_withdrawal_requires_explicit_reselection(self):
        self.update("enroll")
        clone = self.base / "clone"
        self.git(self.base, "clone", "-q", str(self.root), str(clone))
        self.update("enroll", clone, associate_with=str(self.root))
        withdrawn = self.update("withdraw", clone)
        self.assertFalse(withdrawn["shared"][0]["enabled"])
        self.assertEqual(device.verify(withdrawn["shared"][0]), "denied")
        selected = self.update("enroll", self.root)
        self.assertTrue(selected["shared"][0]["enabled"])

    def test_identity_mutation_stale_and_replace_failure_preserves_bytes(self):
        enrolled = self.update("enroll")
        (self.root / "eutonos.read.json").write_text(json.dumps({"profile": ts.CATALOG,
            "repository_id": "different", "scope_id": "scope", "namespace": "native"}), encoding="utf-8")
        self.assertEqual(device.verify(enrolled["shared"][0]), "stale")
        before = (self.home / device.PATH).read_bytes()
        clone = self.base / "changed-identity-clone"
        self.git(self.base, "clone", "-q", str(self.root), str(clone))
        (clone / "eutonos.read.json").write_bytes((self.root / "eutonos.read.json").read_bytes())
        with self.assertRaises(ts.ReadError) as caught:
            self.update("enroll", clone, associate_with=str(self.root))
        self.assertEqual(caught.exception.code, "stale")
        self.assertEqual((self.home / device.PATH).read_bytes(), before)
        with patch.object(device.os, "replace", side_effect=OSError("sentinel private path")):
            with self.assertRaises(ts.ReadError) as caught:
                self.update("withdraw")
        self.assertNotIn("sentinel", str(caught.exception))
        self.assertEqual((self.home / device.PATH).read_bytes(), before)
        self.assertEqual(list(self.home.glob("*.tmp")), [])

    def test_replaced_lineage_cannot_reselect_matching_identity(self):
        enrolled = self.update("enroll")
        self.git(self.root, "checkout", "-q", "--orphan", "replacement")
        self.git(self.root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "new unrelated lineage")
        self.assertEqual(device.verify(enrolled["shared"][0]), "stale")
        with self.assertRaises(ts.ReadError) as caught:
            self.update("enroll")
        self.assertEqual(caught.exception.code, "stale")
        self.assertEqual(device.read(home=self.home)["revision"], enrolled["revision"])

    def test_private_slot_redacted_and_wrong_owner_sanitized(self):
        from eutonos import personal
        private = self.base / "personal-sentinel"; private.mkdir()
        for path, raw in personal.initial_files("owner-sentinel").items():
            (private / path).write_bytes(raw)
        selected = self.update("enroll", private, kind="personal", owner_id="owner-sentinel")
        self.assertEqual(selected["personal"]["locator"], str(private))
        public = device.read(home=self.home)
        self.assertEqual(public["personal"], {"outcome": "denied"})
        self.assertNotIn("sentinel", json.dumps(public))
        with self.assertRaises(ts.ReadError) as caught:
            self.update("withdraw", private, kind="personal", owner_id="wrong")
        self.assertEqual(caught.exception.code, "denied")
        self.assertNotIn("sentinel", str(caught.exception))
        with self.assertRaises(ts.ReadError):
            device.form("enroll", {"kind": "personal", "locator": str(private)}, home=self.home)
        with self.assertRaises(ts.ReadError):
            device.form("enroll", {"kind": "shared", "locator": str(private)}, home=self.home)
        with self.assertRaises(ts.ReadError):
            device.form("enroll", {"kind": "personal", "locator": str(private), "owner_id": "owner-sentinel"},
                        owner_id="wrong", home=self.home)

    def test_registry_refuses_shared_repository_destination_before_write(self):
        private = self.base / "personal-sentinel"; private.mkdir()
        from eutonos import personal
        for path, raw in personal.initial_files("owner").items():
            (private / path).write_bytes(raw)
        request = {"profile": device.REQUEST, "operation": "enroll", "kind": "personal",
                   "locator": str(private), "owner_id": "owner"}
        with self.assertRaises(ts.ReadError) as caught:
            device.update(request, expected_revision=None, home=self.root / "public-device")
        self.assertEqual(caught.exception.code, "denied")
        self.assertNotIn("sentinel", str(caught.exception))
        self.assertFalse((self.root / "public-device").exists())

    def test_registry_home_alias_and_source_alias_refused(self):
        alias = self.base / "alias"
        try:
            alias.symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest("Host does not permit a directory-symlink fixture")
        with self.assertRaises(ts.ReadError) as caught:
            device.read(home=alias / "device")
        self.assertEqual(caught.exception.code, "denied")
        with self.assertRaises(ts.ReadError) as caught:
            self.update("enroll", alias)
        self.assertEqual(caught.exception.code, "denied")


if __name__ == "__main__":
    unittest.main()
