"""The EUTONOS home resolver, its layout helpers, the Crosstalk parity copy and the hard-coded path sweep."""
import ast
import importlib.util
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

from eutonos import home

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "tools" / "crosstalk"
spec = importlib.util.spec_from_file_location("crosstalk_core_for_home_test", KIT / "crosstalk_p0_core.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

# Files that still name the per-user folder, and why.
ALLOWED = {
    "src/eutonos/home.py": "the resolver",
    "tools/crosstalk/crosstalk_p0_core.py": "the kit's parity copy of the resolver",
    "tools/eutonos_project.py": "standalone entry: runs before any runtime exists, so it carries a parity copy",
    "tools/companion_closeout.py": "standalone tool: locates a runtime to run, carries a parity copy",
    "src/eutonos/adoption.py": "text of the managed wrapper written into other repositories (a carrier, IPC4)",
    "src/eutonos/user_tests.py": "per-project `.eutonos/tests` folder, not the per-user home",
    "src/eutonos/user_tests_cli.py": "per-project `.eutonos/tests` folder, not the per-user home",
}
HARD_CODED = re.compile(r"(?<!\w)\.eutonos(?!-)")


def hard_coded(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                  if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                  and node.body and isinstance(node.body[0], ast.Expr)
                  and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str)}
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
            and HARD_CODED.search(node.value)]


def standalone_copies() -> dict:
    """The `_eutonos_home` parity copies in the standalone tools, taken out of their source (those files act on import)."""
    found = {}
    for relative in ("tools/eutonos_project.py", "tools/companion_closeout.py"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        function = next(node for node in ast.parse(source).body
                        if isinstance(node, ast.FunctionDef) and node.name == "_eutonos_home")
        namespace = {"os": os, "sys": sys, "Path": Path}
        exec(compile(ast.Module([function], []), relative, "exec"), namespace)
        found[relative] = namespace["_eutonos_home"]
    return found


class HomeTests(unittest.TestCase):
    def test_environment_wins_and_is_read_at_call_time(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"EUTONOS_HOME": folder}):
            self.assertEqual(home.home(), Path(folder))
            self.assertEqual(home.hosts(), Path(folder) / "hosts")
        with tempfile.TemporaryDirectory() as other, mock.patch.dict(os.environ, {"EUTONOS_HOME": other}):
            self.assertEqual(home.home(), Path(other))

    def test_default_is_dot_eutonos_under_the_user_home(self):
        with tempfile.TemporaryDirectory() as folder:
            env = {"USERPROFILE": folder, "HOME": folder}
            with mock.patch.dict(os.environ, env):
                os.environ.pop("EUTONOS_HOME", None)
                self.assertEqual(home.home(), Path.home() / ".eutonos")
                self.assertEqual(home.home(), Path(folder) / ".eutonos")
            with mock.patch.dict(os.environ, {**env, "EUTONOS_HOME": ""}):
                self.assertEqual(home.home(), Path(folder) / ".eutonos")  # empty counts as unset

    def test_frozen_layout(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"EUTONOS_HOME": folder}):
            base = Path(folder)
            self.assertEqual(
                [home.runtimes(), home.kits(), home.bin_dir(), home.marketplace(), home.install(), home.hosts(),
                 home.projects_file(), home.settings(), home.profiles()],
                [base / name for name in ("runtimes", "kits", "bin", "marketplace", "install", "hosts",
                                          "projects.json", "settings", "profiles")])

    def test_crosstalk_parity_copy_matches_the_resolver(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as profile:
            with mock.patch.dict(os.environ, {"EUTONOS_HOME": folder}):
                self.assertEqual(core.eutonos_home(), home.home())
                self.assertEqual(core.device_dir(), Path(folder) / "crosstalk-probe")
            with mock.patch.dict(os.environ, {"USERPROFILE": profile, "HOME": profile}):
                for value in (None, ""):  # unset and empty both mean unset
                    if value is None:
                        os.environ.pop("EUTONOS_HOME", None)
                    else:
                        os.environ["EUTONOS_HOME"] = value
                    self.assertEqual(core.eutonos_home(), home.home())
                    self.assertEqual(core.eutonos_home(), Path(profile) / ".eutonos")
                    self.assertEqual(core.device_dir(), Path(profile) / ".eutonos" / "crosstalk-probe")

    def test_a_relative_eutonos_home_is_refused_everywhere(self):
        copies = standalone_copies()
        with mock.patch.dict(os.environ, {"EUTONOS_HOME": "relative/folder"}):
            with self.assertRaises(ValueError):
                home.home()
            with self.assertRaises(ValueError):
                core.eutonos_home()
            for name, copy in copies.items():
                with self.subTest(copy=name), self.assertRaises(ValueError):
                    copy()

    def test_a_runtime_derives_its_home_from_where_it_lives(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as profile:
            base = Path(folder).resolve()
            runtime = base / "runtimes" / "eutonos-1.2.3-0123456789ab"
            runtime.mkdir(parents=True)
            stranger = base / "venvs" / "eutonos-1.2.3-0123456789ab"
            stranger.mkdir(parents=True)
            with mock.patch.dict(os.environ, {"USERPROFILE": profile, "HOME": profile}):
                os.environ.pop("EUTONOS_HOME", None)
                with mock.patch.object(sys, "prefix", str(runtime)):
                    self.assertEqual(home.home(), base)
                    self.assertEqual(core.eutonos_home(), base)
                    for name, copy in standalone_copies().items():
                        with self.subTest(copy=name):
                            self.assertEqual(copy(), base)
                    os.environ["EUTONOS_HOME"] = str(base / "override")  # the environment still wins
                    self.assertEqual(home.home(), base / "override")
                    self.assertEqual(core.eutonos_home(), base / "override")
                with mock.patch.object(sys, "prefix", str(stranger)):
                    os.environ.pop("EUTONOS_HOME", None)
                    self.assertEqual(home.home(), Path(profile) / ".eutonos")
                    for name, copy in standalone_copies().items():
                        with self.subTest(copy=name):
                            self.assertEqual(copy(), Path(profile) / ".eutonos")

    def test_a_kit_derives_its_home_from_where_it_lives(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as profile:
            base = Path(folder).resolve()
            kit = base / "kits" / "crosstalk-abc123"
            kit.mkdir(parents=True)
            with mock.patch.dict(os.environ, {"USERPROFILE": profile, "HOME": profile}):
                os.environ.pop("EUTONOS_HOME", None)
                with mock.patch.object(core, "KIT", kit):
                    self.assertEqual(core.eutonos_home(), base)
                    self.assertEqual(core.device_dir(), base / "crosstalk-probe")

    def test_standalone_parity_copies_match_the_resolver(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as profile:
            with mock.patch.dict(os.environ, {"USERPROFILE": profile, "HOME": profile}):
                for value in (folder, None, ""):
                    if value is None:
                        os.environ.pop("EUTONOS_HOME", None)
                    else:
                        os.environ["EUTONOS_HOME"] = value
                    for name, copy in standalone_copies().items():
                        with self.subTest(copy=name, value=value):
                            self.assertEqual(copy(), home.home())

    def test_device_dir_override_still_wins(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"CROSSTALK_P0_DEVICE_DIR": folder}):
            self.assertEqual(core.device_dir(), Path(folder))

    def test_no_module_hard_codes_the_per_user_folder(self):
        offenders = {}
        for base in ("src", "tools"):
            for path in sorted((ROOT / base).rglob("*.py")):
                relative = path.relative_to(ROOT).as_posix()
                if relative in ALLOWED:
                    continue
                lines = hard_coded(path)
                if lines:
                    offenders[relative] = lines
        self.assertEqual(offenders, {})

    def test_allowed_files_really_need_their_exception(self):
        for relative in ALLOWED:
            with self.subTest(relative=relative):
                self.assertTrue(hard_coded(ROOT / relative), "remove it from ALLOWED")


if __name__ == "__main__":
    unittest.main()
