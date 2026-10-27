import json
from pathlib import Path
import tempfile
import unittest

from eutonos import release


def package_files(root):
    """Every shipped file below src/eutonos, as repository-relative POSIX paths (no bytecode)."""
    package = Path(root) / "src" / "eutonos"
    return {path.relative_to(root).as_posix() for path in package.rglob("*")
            if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts
            and path.suffix not in (".pyc", ".pyo")}


def uncovered(root, manifest):
    """Package files that no release module claims."""
    covered = {path for module in release.inventory(root, manifest) for path in module["files"]}
    return sorted(package_files(root) - covered)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "app.py").write_text("first", encoding="utf-8")
        self.manifest = {"profile": release.PROFILE, "name": "Consumer", "version": "7.2",
                         "channel": "development", "modules": [{"id": "app", "name": "App",
                         "version": "3.1", "paths": ["app.py"]}]}

    def save(self):
        (self.root / "eutonos.version.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def test_the_dev_channel_replaces_development_which_still_reads_as_dev(self):
        self.save()
        self.assertEqual(release.status(self.root)["channel"], "dev")  # an older manifest says development
        self.manifest["channel"] = "dev"
        self.save()
        self.assertEqual(release.status(self.root)["channel"], "dev")
        self.manifest["channel"] = "release"
        self.save()
        self.assertEqual(release.status(self.root)["channel"], "release")
        for bad in ("nightly", "", None):
            self.manifest["channel"] = bad
            with self.assertRaises(ValueError):
                release.validate(self.manifest)
        shipped = json.loads((Path(release.__file__).parent / "release.json").read_text(encoding="utf-8"))
        self.assertEqual(shipped["channel"], "dev")

    def test_same_version_edit_changes_fingerprint(self):
        before = release.inventory(self.root, self.manifest)[0]
        (self.root / "app.py").write_text("second", encoding="utf-8")
        after = release.inventory(self.root, self.manifest)[0]
        self.assertEqual(before["version"], after["version"])
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])

    def test_missing_pattern_is_not_valid_fingerprint(self):
        self.manifest["modules"][0]["paths"].append("absent/*.py")
        item = release.inventory(self.root, self.manifest)[0]
        self.assertIsNone(item["fingerprint"])
        self.assertEqual(item["missing"], ["absent/*.py"])

    def test_unsafe_paths_and_duplicate_identity_rejected(self):
        for path in ("../secret", "/absolute", "C:/secret", "..\\secret"):
            self.manifest["modules"][0]["paths"] = [path]
            with self.assertRaises(ValueError):
                release.inventory(self.root, self.manifest)
        self.manifest["modules"][0]["paths"] = ["app.py"]
        self.manifest["modules"].append(dict(self.manifest["modules"][0]))
        with self.assertRaises(ValueError):
            release.validate(self.manifest)

    def test_optional_project_identity_separate_from_runtime(self):
        self.assertEqual(release.status(self.root)["status"], "not-configured")
        self.save()
        result = release.status(self.root)
        self.assertEqual(result["version"], "7.2")
        self.assertNotEqual(result["runtime_version"], "7.2")
        self.assertIsNone(result["dirty"])

    def test_current_release_metadata_covers_exact_package_payload(self):
        root = Path(__file__).resolve().parents[1]
        modules = release.inventory(root, release.load(root))
        self.assertEqual(uncovered(root, release.load(root)), [])
        self.assertTrue(all(module["status"] == "available" for module in modules))
        source = {path.removeprefix("src/eutonos/") for path in package_files(root)}
        self.assertIn("desktop_companion_probe.ps1", source)
        self.assertNotIn("codex_context_probe.ps1", source)
        self.assertIn("catalog_closeout.py", source)
        self.assertIn("rule_index.json", source)
        workflow = next(module for module in modules if module["id"] == "workflow")
        self.assertIn("src/eutonos/rule_index.json", workflow["files"])
        self.assertIn("src/eutonos/orientation.py", workflow["files"])

    def test_package_file_outside_release_modules_is_reported_uncovered(self):
        package = self.root / "src" / "eutonos"
        package.mkdir(parents=True)
        (package / "setup_cli.py").write_text("# fixture", encoding="utf-8")
        (package / "orphan.py").write_text("# unassigned", encoding="utf-8")
        self.manifest["modules"] = [{"id": "setup", "name": "Setup", "version": "1.0",
                                     "paths": ["src/eutonos/setup_cli.py"]}]
        self.assertEqual(uncovered(self.root, self.manifest), ["src/eutonos/orphan.py"])

    def test_runtime_module_selection_retains_coherent_source_ownership(self):
        root = Path(__file__).resolve().parents[1]
        modules = {item["id"]: item for item in release.inventory(root, release.load(root))}
        expected = {"workflow": ["catalog_closeout.py", "work_lifecycle.py", "user_tests.py"],
                    "coordination": ["write_retry.py", "grants.py"],
                    "delegation": ["role_router.py", "worker_models.py"],
                    "dashboard": ["desktop_companion.py", "desktop_companion_windows.py", "desktop_companion_runtime.py", "desktop_companion_probe.ps1", "desktop_companion_data.py", "desktop_agent_activity.py"],
                    "setup": ["setup_finalize.py"], "metrics": ["metric_index.py"],
                    "crosstalk": ["crosstalk_status.py", "crosstalk_dashboard_feeds.py"],
                    "foundation": ["local_state.py"]}
        for owner, paths in expected.items():
            for name in paths:
                with self.subTest(owner=owner, name=name):
                    self.assertIn("src/eutonos/" + name, modules[owner]["files"])
                    self.assertTrue(all("src/eutonos/" + name not in module["files"]
                                        for key, module in modules.items() if key != owner))

    def test_crosstalk_kit_ships_with_the_release(self):
        root = Path(__file__).resolve().parents[1]
        modules = {item["id"]: item for item in release.inventory(root, release.load(root))}
        files = modules["crosstalk"]["files"]
        self.assertEqual(modules["crosstalk"]["status"], "available")
        for name in ("tools/crosstalk/crosstalk_p0_cli.py", "tools/crosstalk/crosstalk_p0_core.py",
                     "tools/crosstalk/install.ps1", "tools/crosstalk/claude-plugin/plugin/hooks/hooks.json",
                     "tools/crosstalk/hermes-plugin/crosstalk/plugin.yaml", "docs/Crosstalk/README.md"):
            with self.subTest(name=name):
                self.assertIn(name, files)

    def test_manifest_reference_escape_rejected(self):
        (self.root / "eutonos.version.json").write_text('{"manifest":"../other.json"}', encoding="utf-8")
        self.assertEqual(release.status(self.root)["status"], "error")


if __name__ == "__main__":
    unittest.main()
