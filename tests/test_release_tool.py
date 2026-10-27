"""tools/release.py: a release is built from HEAD only and described by eutonos-release.json."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
spec = importlib.util.spec_from_file_location("release_tool", ROOT / "tools" / "release.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)

from eutonos import release_source as rs  # noqa: E402

PLUGIN = "tools/crosstalk/claude-plugin/plugin/.claude-plugin/plugin.json"
MARKET = "tools/crosstalk/claude-plugin/.claude-plugin/marketplace.json"
KIT = "tools/crosstalk/"


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout


class ReleaseToolTests(unittest.TestCase):
    def repo(self, base, lock=None):
        root = base / "repo"
        (root / "src/eutonos").mkdir(parents=True)
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8").split("[tool.eutonos.kits]")[0]
        (root / "pyproject.toml").write_text(pyproject + '[tool.eutonos.kits]\ncrosstalk = "tools/crosstalk"\n', encoding="utf-8")
        shipped = json.loads((ROOT / "src/eutonos/release.json").read_text(encoding="utf-8"))
        (root / "src/eutonos/release.json").write_text(json.dumps(shipped), encoding="utf-8")
        (root / "src/eutonos/__init__.py").write_text("# fixture\n")
        files = {PLUGIN: json.dumps({"name": "x", "version": "0.0.1"}, indent=2) + "\n",
                 MARKET: json.dumps({"name": "m", "plugins": [{"name": "x", "version": "0.0.1"}]}, indent=2) + "\n",
                 KIT + "README.md": "kit\n", "docs/Crosstalk/QUICKSTART.md": "quick\n"}
        if lock is not None:
            files[KIT + "wheels.lock.json"] = json.dumps(lock)
        for name, text in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        git(root, "init", "-q")
        git(root, "add", ".")
        git(root, "commit", "-q", "-m", "fixture")
        return root, shipped["version"]

    def test_a_release_is_the_wheel_and_a_manifest_the_installer_accepts(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, version = self.repo(base)
            result = tool.build_release(root, base / "dist")
            folder = Path(result["folder"])
            self.assertEqual(folder.parent, base / "dist")
            self.assertEqual(sorted(p.name for p in folder.iterdir()),
                             sorted([rs.MANIFEST_NAME, f"eutonos-{version}-py3-none-any.whl"]))
            manifest = json.loads((folder / rs.MANIFEST_NAME).read_text(encoding="utf-8"))
            rs.validate_manifest(manifest)
            wheel = folder / manifest["wheel"]
            self.assertEqual(manifest["sha256"], hashlib.sha256(wheel.read_bytes()).hexdigest())
            self.assertEqual(manifest["files"][manifest["wheel"]], {"sha256": manifest["sha256"], "size": wheel.stat().st_size})
            self.assertEqual(manifest["source_commit"], git(root, "rev-parse", "HEAD").strip())
            self.assertEqual((manifest["version"], manifest["plugin_version"]), (version, version))
            self.assertEqual(manifest["channels"], ["dev"])
            self.assertIsNone(manifest["min_compatible"])
            self.assertIsNone(manifest["interpreter"])
            self.assertEqual((folder / rs.MANIFEST_NAME).read_bytes(), tool.manifest_bytes(manifest))

    def test_the_plugin_version_is_stamped_in_the_wheel_and_the_repository_is_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, version = self.repo(base)
            before = git(root, "status", "--porcelain")
            result = tool.build_release(root, base / "dist")
            with zipfile.ZipFile(next(Path(result["folder"]).glob("*.whl"))) as archive:
                prefix = "eutonos/kits/crosstalk/"
                self.assertEqual(json.loads(archive.read(prefix + PLUGIN[len(KIT):]))["version"], version)
                self.assertEqual(json.loads(archive.read(prefix + MARKET[len(KIT):]))["plugins"][0]["version"], version)
            self.assertIn('"0.0.1"', (root / PLUGIN).read_text(encoding="utf-8"))
            self.assertEqual(git(root, "status", "--porcelain"), before)

    def test_only_what_head_commits_reaches_the_release(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, _ = self.repo(base)
            (root / "src/eutonos/__init__.py").write_text("# dirty edit\n")
            (root / "src/eutonos/untracked.py").write_text("x = 1\n")
            (root / KIT / "README.md").write_text("dirty kit edit\n")
            (root / KIT / "new.txt").write_text("untracked kit file\n")
            result = tool.build_release(root, base / "dist")
            with zipfile.ZipFile(next(Path(result["folder"]).glob("*.whl"))) as archive:
                names = archive.namelist()
                self.assertEqual(archive.read("eutonos/__init__.py"), b"# fixture\n")
                self.assertNotIn("eutonos/untracked.py", names)
                self.assertEqual(archive.read("eutonos/kits/crosstalk/README.md"), b"kit\n")
                self.assertNotIn("eutonos/kits/crosstalk/new.txt", names)

    def test_the_same_commit_builds_the_same_artifact_and_a_second_build_refuses(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, _ = self.repo(base)
            first = tool.build_release(root, base / "one")
            second = tool.build_release(root, base / "two")
            self.assertEqual((first["artifact_id"], first["sha256"]), (second["artifact_id"], second["sha256"]))
            with self.assertRaises(FileExistsError):
                tool.build_release(root, base / "one")
            self.assertEqual([p.name for p in (base / "one").iterdir()], [first["artifact_id"]])  # no scratch left behind

    def test_the_interpreter_comes_from_the_kits_locked_wheels(self):
        builder = tool._builder()
        data = b"wheel bytes"
        file = "dep-1.0-cp314-cp314-win_amd64.whl"
        lock = {"profile": builder.WHEEL_LOCK_PROFILE, "python": "cp314-win_amd64",
                "wheels": {file: hashlib.sha256(data).hexdigest()}}
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, _ = self.repo(base, lock=lock)
            (root / KIT / "wheels").mkdir()
            (root / KIT / "wheels" / file).write_bytes(data)
            git(root, "add", ".")
            git(root, "commit", "-q", "-m", "wheel")
            pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
            self.assertEqual(tool.kit_interpreter(builder, root, pyproject), "cp314-win_amd64")
            result = tool.build_release(root, base / "dist")
            self.assertEqual(result["interpreter"], "cp314-win_amd64")
        with tempfile.TemporaryDirectory() as temp:
            root, _ = self.repo(Path(temp))
            self.assertIsNone(tool.kit_interpreter(builder, root, (root / "pyproject.toml").read_text(encoding="utf-8")))

    def test_the_channel_and_min_compatible_are_taken_from_the_arguments(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, _ = self.repo(base)
            result = tool.build_release(root, base / "dist", channel="stable", min_compatible="0.4.0")
            manifest = json.loads((Path(result["folder"]) / rs.MANIFEST_NAME).read_text(encoding="utf-8"))
            self.assertEqual((manifest["channels"], manifest["min_compatible"]), (["stable"], "0.4.0"))
            with self.assertRaisesRegex(ValueError, "channel"):
                tool.build_release(root, base / "other", channel="nightly")

    def test_a_directory_without_a_commit_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / "empty").mkdir()
            subprocess.run(["git", "-C", str(base / "empty"), "init", "-q"], check=True)
            with self.assertRaisesRegex(ValueError, "HEAD"):
                tool.build_release(base / "empty", base / "dist")


if __name__ == "__main__":
    unittest.main()
