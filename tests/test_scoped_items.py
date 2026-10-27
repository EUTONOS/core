import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from eutonos import boards, device_repositories as device, scoped_items as scoped, tokenslang as ts
from eutonos.effects import digest


class ScopedItemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scoped-items-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "device"
        self.root = self.repository("first", "first-repo", "First native item")
        self.other = self.repository("second", "second-repo", "Second native item")

    def git(self, root, *args):
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        return subprocess.check_output(["git", "-c", "safe.directory=" + str(root), "-C", str(root), *args],
                                       env=env, stderr=subprocess.PIPE)

    def repository(self, name, repository_id, title):
        root = self.base / name; root.mkdir()
        (root / "eutonos.read.json").write_text(json.dumps({"profile": ts.CATALOG,
            "repository_id": repository_id, "scope_id": "scope", "namespace": "native"}), encoding="utf-8")
        (root / "eutonos.boards.json").write_text(json.dumps({"profile": boards.CONFIG, "owners": ["BOARD.md"]}), encoding="utf-8")
        self.write_items(root, [{"id": "same-item", "type": "goal", "title": title, "summary": "Legacy generic goal", "status": "planned"}])
        self.git(root, "init", "-q", "-b", "main"); self.git(root, "add", ".")
        self.git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "native baseline")
        return root

    def write_items(self, root, items):
        (root / "BOARD.md").write_bytes(b"```tokenslang-board\n" + ts.encoded({"profile": boards.BOARD, "items": items}) + b"\n```\n")

    def ref(self, root, item_id="same-item", owner_id=None):
        return {"profile": scoped.PROFILE, **device.native_identity(root, owner_id=owner_id), "item_id": item_id}

    def enroll(self, root, **extra):
        current = device.read(home=self.home)
        return device.update({"profile": device.REQUEST, "operation": "enroll", "locator": str(root), **extra},
                             expected_revision=current["revision"], home=self.home)

    def test_immutable_shape_and_distinct_same_ids_direct_roots(self):
        first = scoped.resolve(self.root, self.ref(self.root))
        second = scoped.resolve(self.other, self.ref(self.other))
        self.assertEqual(first["outcome"], "resolved")
        self.assertEqual(second["outcome"], "resolved")
        self.assertNotEqual(first["item"]["title"], second["item"]["title"])
        self.assertEqual(first["owner_sha256"], digest((self.root / "BOARD.md").read_bytes()))
        self.assertEqual(first["source_pin"]["observation"], "committed")
        self.assertEqual(first["coverage"]["status"], "complete")
        self.assertFalse(self.home.exists())
        for field in ("path", "commit", "revision"):
            with self.subTest(field=field), self.assertRaises(ts.ReadError):
                scoped.validate({**self.ref(self.root), field: "forbidden identity field"})

    def test_selected_cross_root_and_no_title_fallback(self):
        enrolled = self.enroll(self.other)
        result = scoped.resolve(self.root, self.ref(self.other), enrollment=enrolled)
        self.assertEqual(result["item"]["title"], "Second native item")
        missing = scoped.resolve(self.root, self.ref(self.other, "nonexistent"), enrollment=enrolled)
        self.assertEqual(missing["outcome"], "missing")
        self.assertNotIn("item", missing)

    def test_collision_ambiguous_but_explicit_root_remains_usable(self):
        self.enroll(self.other)
        collision = self.repository("collision", "second-repo", "Reused identity")
        enrolled = self.enroll(collision)
        self.assertEqual(len(enrolled["shared"]), 2)
        self.assertEqual(scoped.resolve(self.root, self.ref(self.other), enrollment=enrolled)["outcome"], "ambiguous")
        self.assertEqual(scoped.resolve(collision, self.ref(collision))["item"]["title"], "Reused identity")

    def test_moved_clone_keeps_ref_and_selected_native_owner(self):
        self.enroll(self.other)
        clone = self.base / "clone"
        self.git(self.base, "clone", "-q", str(self.other), str(clone))
        enrolled = self.enroll(clone, associate_with=str(self.other))
        reference = self.ref(clone)
        moved = self.base / "moved-clone"
        shutil.move(str(clone), moved)
        device.update({"profile": device.REQUEST, "operation": "relocate", "locator": str(clone), "new_locator": str(moved)},
                      expected_revision=enrolled["revision"], home=self.home)
        result = scoped.resolve(self.root, reference, enrollment=device.read(home=self.home))
        self.assertEqual(result["outcome"], "resolved")
        self.assertEqual(result["reference"], reference)
        self.assertEqual(result["source_pin"]["locator"], str(moved))

    def test_offline_unknown_stale_identity_and_issued_link_preservation(self):
        enrolled = self.enroll(self.other)
        ref = self.ref(self.other)
        moved = self.base / "offline"
        shutil.move(str(self.other), moved)
        result = scoped.resolve(self.root, ref, enrollment=enrolled)
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(scoped.check_shared_links(self.root, [ref], previous=[ref], enrollment=enrolled), [ref])
        with self.assertRaises(ts.ReadError) as caught:
            scoped.check_shared_links(self.root, [ref], enrollment=enrolled)
        self.assertEqual(caught.exception.code, "unknown")
        shutil.move(str(moved), self.other)
        (self.other / "eutonos.read.json").write_text(json.dumps({"profile": ts.CATALOG,
            "repository_id": "changed", "scope_id": "scope", "namespace": "native"}), encoding="utf-8")
        self.assertEqual(scoped.resolve(self.root, ref, enrollment=enrolled)["outcome"], "stale")

    def test_working_tree_pin_duplicate_item_and_partial_coverage(self):
        items = [{"id": "same-item", "type": "goal", "title": "Changed", "summary": "Current meaning", "status": "active"}]
        self.write_items(self.root, items)
        result = scoped.resolve(self.root, self.ref(self.root))
        self.assertEqual(result["source_pin"]["observation"], "working-tree")
        self.assertEqual(result["item"]["status"], "active")
        self.write_items(self.root, items * 2)
        self.assertEqual(scoped.resolve(self.root, self.ref(self.root))["outcome"], "ambiguous")
        (self.root / "BOARD.md").write_text("```tokenslang-board\ninvalid\n```", encoding="utf-8")
        result = scoped.resolve(self.root, self.ref(self.root))
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(result["coverage"]["status"], "partial")

    def test_private_owner_denial_before_content_and_personal_to_shared(self):
        from eutonos import personal
        private = self.base / "private-sentinel"; private.mkdir()
        for path, raw in personal.initial_files("owner").items():
            (private / path).write_bytes(raw)
        private_ref = self.ref(private, "same-item", owner_id="owner")
        (private / "goals.md").write_bytes(b"```tokenslang-board\n" + ts.encoded({"profile": boards.BOARD, "items": [
            {"id": "same-item", "type": "goal", "title": "SECRET SENTINEL", "summary": "Private", "status": "planned"}]}) + b"\n```\n")
        enrolled = self.enroll(private, kind="personal", owner_id="owner")
        denied = scoped.resolve(self.root, private_ref, owner_id="owner", enrollment=enrolled)
        self.assertEqual(denied["outcome"], "denied")
        self.assertNotIn("sentinel", json.dumps(denied).lower())
        with patch.object(personal, "read", side_effect=AssertionError("Private content must not be read")):
            self.assertEqual(scoped.resolve(private, private_ref, owner_id="wrong")["outcome"], "denied")
        result = scoped.resolve(private, private_ref, owner_id="owner")
        self.assertEqual(result["item"]["title"], "SECRET SENTINEL")
        with self.assertRaises(ts.ReadError) as caught:
            scoped.check_shared_links(self.root, [private_ref], enrollment=enrolled)
        self.assertEqual(caught.exception.code, "denied")
        enrolled = self.enroll(self.other)
        shared = scoped.resolve(private, self.ref(self.other), owner_id="owner", enrollment=enrolled)
        self.assertEqual(shared["outcome"], "resolved")

    def test_original_root_alias_denied_before_native_read(self):
        alias = self.base / "alias"
        try:
            alias.symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest("Host does not permit a directory-symlink fixture")
        with patch.object(scoped, "_shared_item", side_effect=AssertionError("Alias must be denied first")):
            result = scoped.resolve(alias, self.ref(self.root))
        self.assertEqual(result["outcome"], "denied")
        self.assertNotIn("locator", json.dumps(result))

    def test_supplied_personal_enrollment_alias_denied_before_content(self):
        from eutonos import personal
        origin = self.base / "personal-origin"; origin.mkdir()
        target = self.base / "private-sentinel"; target.mkdir()
        for root in (origin, target):
            for path, raw in personal.initial_files("owner").items():
                (root / path).write_bytes(raw)
        reference = self.ref(target, owner_id="owner")
        alias = self.base / "private-alias"
        try:
            alias.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("Host does not permit a directory-symlink fixture")
        enrollment = {"profile": device.PROFILE, "shared": [], "personal": {
            "identity": {key: reference[key] for key in device.IDENTITY},
            "locator": str(alias), "owner_id": "owner"}}
        with patch.object(personal, "read", side_effect=AssertionError("Aliased private content must not be read")):
            result = scoped.resolve(origin, reference, owner_id="owner", enrollment=enrollment)
        self.assertEqual(result["outcome"], "denied")
        self.assertNotIn("sentinel", json.dumps(result).lower())
        self.assertNotIn("locator", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
