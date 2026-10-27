"""Offline wheel closure, metadata, integrity and isolated consumer import."""
import base64
import csv
import configparser
from email.parser import BytesParser
import hashlib
import importlib.util
import io
import itertools
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_wheel", ROOT / "tools" / "build_wheel.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class BuildWheelTests(unittest.TestCase):
    def test_current_wheel_structure_records_assets_and_consumer_import(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            wheel = builder.build(ROOT, base / "dist", allow_dirty=True)  # a working checkout is often mid-edit
            project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
            version = json.loads((ROOT / "src/eutonos/release.json").read_text(encoding="utf-8"))["version"]
            dist = f"eutonos-{version}.dist-info"
            with zipfile.ZipFile(wheel) as archive:
                metadata = BytesParser().parsebytes(archive.read(dist + "/METADATA"))
                self.assertEqual(metadata["Version"], version)
                self.assertEqual(metadata["Summary"], project["description"])
                self.assertEqual(metadata["Requires-Python"], project["requires-python"])
                self.assertEqual(metadata.get_all("Requires-Dist", []), project["dependencies"])
                self.assertIn(b"Tag: py3-none-any", archive.read(dist + "/WHEEL"))
                entries = archive.read(dist + "/entry_points.txt").decode()
                entry_config = configparser.ConfigParser(interpolation=None)
                entry_config.optionxform = str
                entry_config.read_string(entries)
                self.assertEqual(dict(entry_config["console_scripts"]), project["scripts"])
                self.assertEqual(dict(entry_config["gui_scripts"]), project["gui-scripts"])
                self.assertNotIn("eutonos-codex-context", entry_config["console_scripts"])
                self.assertNotIn("eutonos-desktop-companion", entry_config["console_scripts"])
                names = set(archive.namelist())
                self.assertEqual(len(names), len(archive.namelist()))
                for page in ("install", "uninstall", "add-repository", "remove-repository", "repair"):
                    self.assertIn(f"eutonos/checklists/{page}.md", names)
                self.assertIn("eutonos/templates/memory_block.md", names)  # the memory block's text ships in the wheel
                self.assertIn("eutonos/offers/offers.json", names)  # so do EUTONOS's own feature and capability lists
                self.assertTrue(all(not n.startswith("/") and ".." not in Path(n).parts for n in names))
                rows = list(csv.reader(io.StringIO(archive.read(dist + "/RECORD").decode())))
                self.assertEqual({row[0] for row in rows}, names)
                for name, digest, size in rows:
                    if name.endswith("/RECORD"):
                        self.assertEqual((digest, size), ("", ""))
                    else:
                        data = archive.read(name)
                        sha = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
                        self.assertEqual(digest, "sha256=" + sha)
                        self.assertEqual(int(size), len(data))
                for source in (ROOT / "src/eutonos").rglob("*"):
                    if source.is_file() and "__pycache__" not in source.parts and source.suffix not in {".pyc", ".pyo"}:
                        self.assertEqual(archive.read("eutonos/" + source.relative_to(ROOT / "src/eutonos").as_posix()), source.read_bytes())
                consumer = base / "consumer"
                archive.extractall(consumer)
            code = ("import sys,json,importlib; from pathlib import Path; "
                    "sys.path.insert(0,sys.argv[1]); import eutonos; "
                    "assert Path(eutonos.__file__).is_relative_to(Path(sys.argv[1])); "
                    "from eutonos import rule_index; "
                    "assert rule_index.load()['profile'] == 'eutonos.rule-reference-index.v1'; "
                    "assert 'eutonos.rule.completion-integration' in rule_index.render({}); "
                    "from eutonos import memory_instructions as mem; "
                    "assert all(mem.block(h, 'C:/x').startswith(mem.START) for h in mem.HARNESSES); "
                    "from eutonos import orientation,command_registry; "
                    "assert command_registry.describe('orient')['command']['output_schema']['properties']['profile']['const'] == orientation.PACKET; "
                    "from eutonos.workflow_cli import main; import contextlib,io; output=io.StringIO(); "
                    "exec(\"with contextlib.redirect_stdout(output):\\n assert main(['orient','--root',str(Path(sys.argv[1]).parent)]) in (0,2)\"); "
                    "assert json.loads(output.getvalue())['profile'] == orientation.PACKET; "
                    "entries=json.loads(sys.argv[2]); "
                    "assert all(callable(getattr(importlib.import_module(t.split(':')[0]),t.split(':')[1])) for t in entries.values()); "
                    "from eutonos import dashboard; assert len(dashboard.offers()['items']) > 0; "  # the offers list reads from the installed wheel
                    "print(eutonos.__version__,len(entries))")
            result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(consumer), json.dumps(project["scripts"] | project["gui-scripts"])],
                                    cwd=base, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), f"{version} {len(project['scripts']) + len(project['gui-scripts'])}")
            with self.assertRaises(FileExistsError):
                builder.build(ROOT, base / "dist", allow_dirty=True)

    def fixture(self, base):
        root = base / "root"
        package = root / "src/eutonos"
        package.mkdir(parents=True)
        shutil.copy(ROOT / "pyproject.toml", root)
        project = root / "pyproject.toml"  # a fixture has no kit folder or git history to stage
        project.write_text(project.read_text(encoding="utf-8").split("[tool.eutonos.kits]")[0], encoding="utf-8")
        shutil.copy(ROOT / "src/eutonos/release.json", package)
        (package / "__init__.py").write_text("# fixture\n")
        return root, package

    def build_at(self, root, output, clock, environ=None):
        """Build with the wall clock forced to `clock`, so a build that read the time would show it."""
        with mock.patch.dict(os.environ):
            os.environ.pop("SOURCE_DATE_EPOCH", None)
            os.environ.update(environ or {})
            with mock.patch("time.time", side_effect=itertools.count(clock)):
                return builder.build(root, output)

    def test_two_builds_at_different_times_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, _ = self.fixture(base)
            first = self.build_at(root, base / "one", 1_700_000_000)
            second = self.build_at(root, base / "two", 1_900_000_000)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                infos = archive.infolist()
                self.assertEqual([info.filename for info in infos], sorted(info.filename for info in infos))
                for info in infos:
                    self.assertEqual((info.date_time, info.create_system, info.external_attr, info.compress_type),
                                     ((1980, 1, 1, 0, 0, 0), 3, 0o644 << 16, zipfile.ZIP_DEFLATED), info.filename)

    def test_source_date_epoch_sets_every_entry_time_and_keeps_builds_identical(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, _ = self.fixture(base)
            environ = {"SOURCE_DATE_EPOCH": "1700000000"}
            first = self.build_at(root, base / "one", 1_700_000_000, environ)
            second = self.build_at(root, base / "two", 1_950_000_000, environ)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                self.assertEqual({info.date_time for info in archive.infolist()}, {time.gmtime(1_700_000_000)[:6]})
            self.assertEqual(builder.source_epoch({"SOURCE_DATE_EPOCH": "0"}), builder.ZIP_EPOCH_MIN)
            self.assertEqual(builder.source_epoch({}), builder.ZIP_EPOCH_MIN)

    def test_a_source_date_epoch_a_zip_cannot_hold_is_refused_without_a_wheel(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, _ = self.fixture(base)
            for bad in ("yesterday", "-5", "1.5", "99999999999"):
                with self.assertRaisesRegex(ValueError, "SOURCE_DATE_EPOCH"):
                    self.build_at(root, base / "dist", 1_700_000_000, {"SOURCE_DATE_EPOCH": bad})
            self.assertEqual(list((base / "dist").glob("*.whl")), [])

    def test_python_requirement_and_dependencies_are_read_from_project(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, package = self.fixture(base)
            path = root / "pyproject.toml"
            path.write_text(path.read_text().replace('>=3.12', '>=3.13').replace('dependencies = []', 'dependencies = ["example>=2"]'))
            with zipfile.ZipFile(builder.build(root, base / "dist")) as archive:
                name = next(n for n in archive.namelist() if n.endswith("/METADATA"))
                metadata = BytesParser().parsebytes(archive.read(name))
                self.assertEqual(metadata["Requires-Python"], ">=3.13")
                self.assertEqual(metadata["Requires-Dist"], "example>=2")

    def test_unsupported_metadata_and_undeclared_data_refuse_without_wheel(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, package = self.fixture(base)
            path = root / "pyproject.toml"
            original = path.read_text()
            path.write_text(original.replace('[project.scripts]', 'readme = "README.md"\n\n[project.scripts]'))
            with self.assertRaisesRegex(ValueError, "Unsupported project metadata: readme"):
                builder.build(root, base / "dist")
            path.write_text(original)
            (package / "secret.bin").write_bytes(b"outside declared package data")
            with self.assertRaisesRegex(ValueError, "Undeclared package data"):
                builder.build(root, base / "dist")
            self.assertFalse((base / "dist").exists())

    def test_linked_payload_is_rejected_including_directory_links(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, package = self.fixture(base)
            outside = base / "outside"
            outside.mkdir()
            (outside / "secret.py").write_text("secret")
            link = package / "linked"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"Directory symlink creation unavailable: {exc}")
            with self.assertRaisesRegex(ValueError, "Symlink or outside-repository"):
                builder.build(root, base / "dist")
            self.assertFalse((base / "dist").exists())

    def test_source_root_link_is_rejected_before_metadata_read(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, package = self.fixture(base)
            original = Path.is_symlink
            with mock.patch.object(Path, "is_symlink", lambda path: path == root / "src" or original(path)):
                with self.assertRaisesRegex(ValueError, "regular repository directory"):
                    builder.build(root, base / "dist")

    def test_gui_entries_refuse_malformed_group_or_target(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, _ = self.fixture(base)
            path = root / 'pyproject.toml'
            original = path.read_text()
            path.write_text(original.replace('eutonos.desktop_companion_runtime:main', 'bad target'))
            with self.assertRaisesRegex(ValueError, 'Unsupported gui_scripts entry'):
                builder.build(root, base / 'dist')
            path.write_text(original)
            # Isolate the parsed GUI table type without accepting unsupported fields.
            version, project = builder._metadata(ROOT, ROOT / 'src/eutonos')
            project['gui-scripts'] = []
            with mock.patch.object(builder, '_metadata', return_value=(version, project)):
                with self.assertRaisesRegex(ValueError, 'Invalid gui_scripts metadata'):
                    builder.build(root, base / 'dist')
            self.assertFalse((base / 'dist').exists())

    def git_fixture(self, base):
        root, package = self.fixture(base)
        for args in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "fixture"]):
            subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
        return root, package

    def test_a_dirty_core_file_refuses_the_build_unless_allowed_and_is_then_recorded(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, package = self.git_fixture(base)
            self.assertTrue(zipfile.is_zipfile(builder.build(root, base / "clean")))
            (package / "__init__.py").write_bytes(b"# edited\n")
            with self.assertRaisesRegex(ValueError, r"Dirty core files.*src/eutonos/__init__.py"):
                builder.build(root, base / "dist")
            self.assertFalse((base / "dist").exists())
            with zipfile.ZipFile(builder.build(root, base / "allowed", allow_dirty=True)) as archive:
                name = next(n for n in archive.namelist() if n.endswith("/METADATA"))
                self.assertEqual(BytesParser().parsebytes(archive.read(name))["Eutonos-Dirty-Source"], "src/eutonos/__init__.py")
                self.assertEqual(archive.read("eutonos/__init__.py"), b"# edited\n")
            subprocess.run(["git", "-C", str(root), "checkout", "--", "src"], check=True, capture_output=True)
            (root / "pyproject.toml").write_text((root / "pyproject.toml").read_text() + "\n# edit\n")
            with self.assertRaisesRegex(ValueError, "Dirty core files.*pyproject.toml"):
                builder.build(root, base / "dist")

    def test_a_clean_build_records_no_dirty_source(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, _ = self.git_fixture(base)
            with zipfile.ZipFile(builder.build(root, base / "dist", allow_dirty=True)) as archive:
                name = next(n for n in archive.namelist() if n.endswith("/METADATA"))
                self.assertNotIn(b"Eutonos-Dirty-Source", archive.read(name))

    def test_head_only_builds_ignore_the_working_tree_and_never_need_it_clean(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, package = self.git_fixture(base)
            (package / "__init__.py").write_bytes(b"# edited\n")
            (package / "extra.py").write_bytes(b"x = 1\n")
            with zipfile.ZipFile(builder.build(root, base / "head", head_only=True)) as archive:
                self.assertEqual(archive.read("eutonos/__init__.py"), b"# fixture\n")
                self.assertNotIn("eutonos/extra.py", archive.namelist())
                name = next(n for n in archive.namelist() if n.endswith("/METADATA"))
                self.assertNotIn(b"Eutonos-Dirty-Source", archive.read(name))

    def test_the_plugin_version_is_stamped_in_staged_bytes_only(self):
        kits = {"crosstalk": {"claude-plugin/plugin/.claude-plugin/plugin.json": b'{"name": "x", "version": "0.0.1"}',
                              "claude-plugin/.claude-plugin/marketplace.json": b'{"plugins": [{"version": "0.0.1"}]}'}}
        builder.stamp_plugin_version(kits, "1.2.3")
        self.assertEqual(json.loads(kits["crosstalk"]["claude-plugin/plugin/.claude-plugin/plugin.json"])["version"], "1.2.3")
        self.assertEqual(json.loads(kits["crosstalk"]["claude-plugin/.claude-plugin/marketplace.json"])["plugins"][0]["version"], "1.2.3")
        with self.assertRaisesRegex(ValueError, "three numeric parts"):
            builder.stamp_plugin_version(kits, "1.2")
        with self.assertRaisesRegex(ValueError, "no the plugin manifest|has no"):
            builder.stamp_plugin_version({"crosstalk": {}}, "1.2.3")
        with self.assertRaisesRegex(ValueError, "exactly one version"):
            builder.stamp_plugin_version({"crosstalk": {"claude-plugin/plugin/.claude-plugin/plugin.json": b"{}",
                                                        "claude-plugin/.claude-plugin/marketplace.json": b'{"version": "1"}'}}, "1.2.3")
