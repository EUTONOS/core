"""ISU stage 6: Git is needed only by the parts whose manifest declares it, so a missing Git limits those parts and
never refuses the install. The resolver, the install prereq and the doctor readback all say which parts are affected."""
import unittest
from unittest import mock

from eutonos import component_support as cs
from eutonos import doctor, installer, prereq


def manifest(name, kind="bundled", tools=(), **extra):
    value = {"id": name, "name": name.title(), "version": "1.0.0", "kind": kind, "default": "on",
             "requires": {"eutonos": ">=0.1", "components": [], "os": ["windows", "macos"], "apps": [],
                          "tools": list(tools)}}
    value.update(extra)
    return value


def resolve(manifests, git):
    return {row["id"]: row for row in installer.resolve_components(
        manifests, os_name="windows", version="9.0", has_app=lambda name: True, has_tool=lambda name: git)}


class ResolverTests(unittest.TestCase):
    def test_a_part_that_needs_git_is_skipped_without_it_and_installed_with_it(self):
        parts = [manifest("core", "core", ["git"]), manifest("needy", tools=["git"]), manifest("free")]
        without = resolve(parts, git=False)
        self.assertEqual((without["needy"]["state"], without["needy"]["reason"]), ("skipped", "needs Git"))
        self.assertEqual(without["free"]["state"], "installed")
        self.assertEqual(resolve(parts, git=True)["needy"]["state"], "installed")

    def test_the_core_is_never_skipped_but_says_what_is_missing(self):
        core = resolve([manifest("core", "core", ["git"])], git=False)["core"]
        self.assertEqual((core["state"], core["missing_tools"]), ("installed", ["git"]))
        self.assertEqual(resolve([manifest("core", "core", ["git"])], git=True)["core"]["missing_tools"], [])

    def test_a_part_that_needs_a_skipped_part_is_skipped_too(self):
        rows = resolve([manifest("needy", tools=["git"]), manifest("child", components=None)], git=False)
        self.assertEqual(rows["needy"]["state"], "skipped")

    def test_without_a_tool_check_every_tool_is_assumed_present(self):
        rows = {row["id"]: row for row in installer.resolve_components(
            [manifest("needy", tools=["git"])], os_name="windows", version="9.0", has_app=lambda name: True)}
        self.assertEqual(rows["needy"]["state"], "installed")


class InstallPrereqTests(unittest.TestCase):
    def test_the_installer_default_prereq_does_not_refuse_a_pc_without_git(self):
        with mock.patch.object(prereq.shutil, "which", lambda name, *a, **k: None if name == "git" else "/bin/" + name), \
                mock.patch.object(prereq, "_probe", lambda path, run: "x 1"):
            ready = installer._default_prereq()
        self.assertEqual((ready["outcome"], ready["missing"]), ("ready", []))


class DoctorReadbackTests(unittest.TestCase):
    def test_the_doctor_names_what_is_limited_without_git_and_how_to_fix_it(self):
        components = [{"id": "core", "state": "ok"}, {"id": "simon", "state": "skipped"}]
        with mock.patch.object(doctor, "_git_present", return_value=False):
            found = doctor._git_parts_readback(components)
        self.assertEqual((found["state"], found["git"]), ("limited", "not found"))
        self.assertEqual([item["id"] for item in found["parts"]], ["core"])
        self.assertTrue(found["parts"][0]["limited"])
        self.assertIn("project memory", found["reason"])
        self.assertIn("Install Git", found["next_action"])

    def test_the_doctor_is_quiet_when_git_is_present(self):
        with mock.patch.object(doctor, "_git_present", return_value=True):
            found = doctor._git_parts_readback([])
        self.assertEqual((found["state"], found["reason"], found["next_action"]), ("ok", None, None))


class ManifestFieldTests(unittest.TestCase):
    def test_tools_must_be_a_list_of_known_tools(self):
        good = cs.load_manifest("hermes")
        problems = cs.validate_manifest(dict(good, requires=dict(good["requires"], tools=["svn"])))
        self.assertEqual(problems, ["requires.tools must list git"])


if __name__ == "__main__":
    unittest.main()
