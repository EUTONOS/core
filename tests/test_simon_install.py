"""Simon installed by EUTONOS: the runtime folder choice, the kit in the wheel and its wheelhouse, the
``eutonos-simon`` launcher, the Simon part, its instruction block and the end-turn reminder line.

Offline. Every home and user folder is temporary, the venv build is a fake (``seams["simon_env"]``) and the launcher's
child process is a fake; nothing touches the real machine or downloads anything.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from eutonos import component_simon, component_state, consent, installer, kits, permissions, role_hooks
from eutonos import simon_instructions, simon_launch
from eutonos import component_support as cs

import test_component_toggle as tct
import test_components as tc
import test_installer as ti

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("build_wheel_for_simon_test", ROOT / "tools" / "build_wheel.py")
builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(builder)
_paths_spec = importlib.util.spec_from_file_location("simon_paths_for_test", ROOT / "tools" / "simon" / "simon_paths.py")
simon_paths = importlib.util.module_from_spec(_paths_spec)
_paths_spec.loader.exec_module(simon_paths)
_setup_spec = importlib.util.spec_from_file_location("build_setup_for_simon_test", ROOT / "tools" / "build_setup.py")
build_setup = importlib.util.module_from_spec(_setup_spec)
_setup_spec.loader.exec_module(build_setup)


def git(root: Path, *args: str):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)


# ------------------------------------------------------------------------------- simon_paths
class RuntimeFolderTests(unittest.TestCase):
    def test_a_checkout_keeps_its_own_runtime_folder(self):
        app = Path("C:/repo/tools/simon")
        self.assertEqual(simon_paths.runtime_dir(app, environ={}), app / ".runtime")
        self.assertIsNone(simon_paths.installed_home(app))

    def test_an_installed_kit_uses_the_home_runtime_folder(self):
        app = Path("C:/home/.eutonos/kits/simon-0123456789ab")
        self.assertEqual(simon_paths.installed_home(app), Path("C:/home/.eutonos"))
        self.assertEqual(simon_paths.runtime_dir(app, environ={}), Path("C:/home/.eutonos/simon/runtime"))

    def test_only_a_simon_kit_folder_counts_as_installed(self):
        for app in (Path("C:/x/kits/crosstalk-0123"), Path("C:/x/other/simon-0123"), Path("C:/x/kits/simon")):
            with self.subTest(app=app):
                self.assertEqual(simon_paths.runtime_dir(app, environ={}), app / ".runtime")

    def test_the_environment_variable_wins_only_when_absolute(self):
        app = Path("C:/home/.eutonos/kits/simon-0123456789ab")
        given = str(Path(tempfile.gettempdir()) / "simon-runtime")
        self.assertEqual(simon_paths.runtime_dir(app, environ={"SIMON_RUNTIME_DIR": given}), Path(given))
        self.assertEqual(simon_paths.runtime_dir(app, environ={"SIMON_RUNTIME_DIR": "relative"}),
                         Path("C:/home/.eutonos/simon/runtime"))
        self.assertEqual(simon_paths.runtime_dir(app, environ={"SIMON_RUNTIME_DIR": "  "}),
                         Path("C:/home/.eutonos/simon/runtime"))

    def test_the_launcher_and_the_kit_agree_on_the_runtime_folder(self):
        home = Path(tempfile.gettempdir()) / "eh"
        self.assertEqual(simon_paths.runtime_dir(home / "kits" / "simon-0123456789ab", environ={}),
                         simon_launch.runtime_dir(home))


# --------------------------------------------------------------------------- the kit in the wheel
def simon_fixture(folder: Path, wheels: dict | None = None, committed: dict | None = None) -> Path:
    """A throwaway git repository with a small Simon kit (and the files the kit must leave out). `wheels` are locked;
    `committed` ({file: bytes}) are committed in the kit's wheels/ folder."""
    simon = folder / "tools" / "simon"
    for path, text in {
        "helperctl.py": "print('helper')\n",
        "simon_paths.py": "APP = 1\n",
        "requirements.txt": "Pillow>=10.0\n",
        "README.md": "# Simon\n",
        ".claude/skills/simon-planner/SKILL.md": "---\nname: simon-planner\n---\n",
        ".claude/settings.json": "{}\n",
        ".gitignore": ".runtime/\n",
        "run_helper.bat": "@echo off\n",
        "tests/test_x.py": "pass\n",
        "tools/simon_session_end.py": "pass\n",
        "docs/KNOWN_BUGS.md": "# bugs\n",
    }.items():
        (simon / path).parent.mkdir(parents=True, exist_ok=True)
        (simon / path).write_text(text, encoding="utf-8")
    lock = {name: hashlib.sha256(data).hexdigest() for name, data in (wheels or {}).items()}
    (simon / "wheels.lock.json").write_text(json.dumps({"profile": "eutonos.kit-wheels.v1", "wheels": lock}),
                                            encoding="utf-8")
    for name, data in (committed or {}).items():
        (simon / "wheels").mkdir(exist_ok=True)
        (simon / "wheels" / name).write_bytes(data)
    (folder / "docs" / "Simon").mkdir(parents=True)
    (folder / "docs" / "Simon" / "QUICKSTART.md").write_text("# Quickstart\n", encoding="utf-8")
    (folder / "pyproject.toml").write_text('[tool.eutonos.kits]\nsimon = "tools/simon"\n', encoding="utf-8")
    git(folder, "init", "-q")
    git(folder, "add", ".")
    git(folder, "commit", "-q", "-m", "fixture")
    return folder


class KitStagingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def test_the_kit_carries_the_runtime_files_and_the_quickstart_only(self):
        files = builder._kits(simon_fixture(self.base / "repo"))["simon"]
        self.assertEqual(sorted(files), [".claude/skills/simon-planner/SKILL.md", "QUICKSTART.md", "README.md",
                                         "helperctl.py", "requirements.txt", "simon_paths.py", "wheels.lock.json"])
        self.assertEqual(files["QUICKSTART.md"], b"# Quickstart\n")

    def test_an_empty_lock_needs_no_wheel_folder(self):
        files = builder._kits(simon_fixture(self.base / "repo"), {"simon": self.base / "absent"})["simon"]
        self.assertFalse([name for name in files if name.startswith("wheels/")])

    def test_locked_wheels_are_staged_from_the_given_folder_after_their_hash_is_checked(self):
        wheels = {"a-1.0-py3-none-any.whl": b"aaa", "b-2.0-cp314-cp314-win_amd64.whl": b"bbb"}
        root = simon_fixture(self.base / "repo", wheels)
        house = self.base / "house"
        house.mkdir()
        for name, data in wheels.items():
            (house / name).write_bytes(data)
        files = builder._kits(root, {"simon": house})["simon"]
        self.assertEqual({name: files["wheels/" + name] for name in wheels}, wheels)
        with mock.patch.dict(os.environ, {"EUTONOS_SIMON_WHEELS": str(house)}):
            self.assertEqual(builder._kits(root)["simon"], files)

    def test_a_lock_without_its_wheels_refuses_the_build_and_downloads_nothing(self):
        wheels = {"a-1.0-py3-none-any.whl": b"aaa"}
        root = simon_fixture(self.base / "repo", wheels)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EUTONOS_SIMON_WHEELS", None)
            with self.assertRaisesRegex(ValueError, "nothing is downloaded"):
                builder._kits(root)
        house = self.base / "house"
        house.mkdir()
        with self.assertRaisesRegex(ValueError, "wheel is missing"):
            builder._kits(root, {"simon": house})
        (house / "a-1.0-py3-none-any.whl").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "does not match its locked sha256"):
            builder._kits(root, {"simon": house})

    def test_committed_wheels_are_carried_without_a_wheel_folder(self):
        wheels = {"a-1.0-py3-none-any.whl": b"aaa", "b-2.0-cp314-cp314-win_amd64.whl": b"bbb"}
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EUTONOS_SIMON_WHEELS", None)
            files = builder._kits(simon_fixture(self.base / "repo", wheels, committed=wheels))["simon"]
        self.assertEqual({name: files["wheels/" + name] for name in wheels}, wheels)

    def test_a_committed_wheel_that_differs_or_is_not_locked_refuses_the_build(self):
        wheels = {"a-1.0-py3-none-any.whl": b"aaa"}
        with self.assertRaisesRegex(ValueError, "does not match its locked sha256"):
            builder._kits(simon_fixture(self.base / "one", wheels, committed={"a-1.0-py3-none-any.whl": b"zzz"}))
        with self.assertRaisesRegex(ValueError, "does not name: x-1.0-py3-none-any.whl"):
            builder._kits(simon_fixture(self.base / "two", wheels, committed=dict(wheels, **{"x-1.0-py3-none-any.whl": b"x"})))

    def test_a_locked_wheel_that_is_not_committed_still_comes_from_the_wheel_folder(self):
        wheels = {"a-1.0-py3-none-any.whl": b"aaa", "b-1.0-py3-none-any.whl": b"bbb"}
        root = simon_fixture(self.base / "repo", wheels, committed={"a-1.0-py3-none-any.whl": b"aaa"})
        house = self.base / "house"
        house.mkdir()
        (house / "b-1.0-py3-none-any.whl").write_bytes(b"bbb")
        files = builder._kits(root, {"simon": house})["simon"]
        self.assertEqual({name: files["wheels/" + name] for name in wheels}, wheels)

    def test_the_repository_bundles_the_locked_wheels_for_the_python_setup_carries(self):
        simon = ROOT / "tools" / "simon"
        lock = json.loads((simon / "wheels.lock.json").read_text(encoding="utf-8"))
        wheels = builder.wheel_lock((simon / "wheels.lock.json").read_bytes())
        self.assertEqual(len(wheels), 9)
        self.assertEqual(lock["python"], "cp314-win_amd64")
        self.assertEqual(sorted(path.name for path in (simon / "wheels").iterdir()), sorted(wheels))
        for name, digest in wheels.items():
            with self.subTest(wheel=name):
                self.assertEqual(hashlib.sha256((simon / "wheels" / name).read_bytes()).hexdigest(), digest)
                tags = name[:-4].split("-")[-3:]
                self.assertTrue(tags == ["cp314", "cp314", "win_amd64"] or tags[1:] == ["none", "any"], name)
                self.assertIn(name, lock["sources"])
        pinned = {item.split("==")[0].lower().replace("-", "_") for item in lock["requirements"]}
        self.assertEqual(pinned, {name.split("-")[0].lower() for name in wheels})
        self.assertIn("Pillow==12.2.0", lock["requirements"])
        default = {"version": [3, 14, 0], "platform": "win-amd64"}
        self.assertEqual(build_setup.python_tag(default), lock["python"])
        self.assertTrue(build_setup.DEFAULT_PYTHON.endswith("Python314"))  # Setup's own Python is the locked one

    def test_setup_refuses_a_python_the_wheels_were_not_built_for(self):
        build_setup.check_kit_python(ROOT, {"version": [3, 14, 9]})  # an older probe without a platform: win-amd64
        build_setup.check_kit_python(ROOT, {"version": [3, 14, 0], "platform": "win-amd64"})
        for found in ({"version": [3, 13, 5], "platform": "win-amd64"}, {"version": [3, 14, 0], "platform": "win-arm64"}):
            with self.subTest(found=found):
                with self.assertRaisesRegex(build_setup.BuildError, "built for cp314-win_amd64"):
                    build_setup.check_kit_python(ROOT, found)
        build_setup.check_kit_python(self.base, {"version": [3, 12, 0]})  # no lock here: nothing to check

    def test_a_malformed_lock_is_refused(self):
        for value in ({"profile": "other", "wheels": {}}, {"profile": "eutonos.kit-wheels.v1", "wheels": []},
                      {"profile": "eutonos.kit-wheels.v1", "wheels": {"../x.whl": "a" * 64}},
                      {"profile": "eutonos.kit-wheels.v1", "wheels": {"x.whl": "short"}}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    builder.wheel_lock(json.dumps(value).encode("utf-8"))

    def test_the_repository_lock_is_valid_and_the_crosstalk_exclusions_still_apply(self):
        lock = builder.wheel_lock((ROOT / "tools" / "simon" / "wheels.lock.json").read_bytes())
        self.assertIsInstance(lock, dict)
        for inside in ("install.ps1", "uninstall.ps1", "claude-plugin/plugin/hooks/register.mjs", "x/__pycache__/a"):
            self.assertTrue(builder.kit_excluded("crosstalk", inside), inside)
        self.assertFalse(builder.kit_excluded("crosstalk", "tests/test_a.py"))
        self.assertTrue(builder.kit_excluded("simon", "tests/test_a.py"))
        self.assertFalse(builder.kit_excluded("simon", "install.ps1"))

    def test_the_project_declares_the_kit_and_the_launcher(self):
        import tomllib
        config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["tool"]["eutonos"]["kits"]["simon"], "tools/simon")
        self.assertEqual(config["project"]["scripts"]["eutonos-simon"], "eutonos.simon_launch:main")


# ----------------------------------------------------------------------------------- the launcher
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.home = self.base / "eutonos-home"

    def kit(self):
        return Path(kits.materialize(self.home, "simon", source=tc.fake_simon_kit(self.base / "src"))["path"])

    def env(self):
        python = simon_launch.env_python(self.home)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"python")
        (simon_launch.env_dir(self.home) / simon_launch.ENV_MARKER).write_text("{}", encoding="utf-8")

    def run_main(self, argv, **extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = simon_launch.main(argv, home=self.home, platform="win32", **extra)
        return code, out.getvalue(), err.getvalue()

    def test_each_missing_piece_refuses_visibly_with_exit_2(self):
        calls = []
        fake = lambda *a, **k: calls.append(a)  # noqa: E731
        code, _out, err = self.run_main(["helper.bridge_status"], run=fake)
        self.assertEqual(code, 2)
        self.assertIn("not switched on", err)
        component_state.set_enabled(self.home, "simon", True)
        code, _out, err = self.run_main(["helper.bridge_status"], run=fake)
        self.assertEqual(code, 2)
        self.assertIn("no verified Simon kit", err)
        self.kit()
        code, _out, err = self.run_main(["helper.bridge_status"], run=fake)
        self.assertEqual(code, 2)
        self.assertIn("has not been built", err)
        self.assertEqual(calls, [])
        out = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(out):
            self.assertEqual(simon_launch.main(["x"], home=self.home, platform="darwin", run=fake), 2)
        self.assertEqual(calls, [])

    def test_a_ready_part_runs_helperctl_with_its_own_python_and_the_home_runtime_folder(self):
        component_state.set_enabled(self.home, "simon", True)
        kit = self.kit()
        self.env()
        seen = []

        def fake(argv, env=None):
            seen.append((argv, env))
            return types.SimpleNamespace(returncode=7)

        with mock.patch.dict(os.environ, {"PYTHONPATH": "C:/elsewhere", "SIMON_RUNTIME_DIR": "C:/wrong"}):
            code, _out, err = self.run_main(["helper.commands", "--detail", "full"], run=fake)
        self.assertEqual((code, err), (7, ""))
        argv, env = seen[0]
        self.assertEqual(argv, [str(simon_launch.env_python(self.home)), "-s", "-B", str(kit / "helperctl.py"),
                                "helper.commands", "--detail", "full"])
        self.assertEqual(env["SIMON_RUNTIME_DIR"], str(self.home / "simon" / "runtime"))
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertNotIn("PYTHONPATH", env)
        self.assertTrue((self.home / "simon" / "runtime").is_dir())

    def test_where_prints_a_read_only_diagnosis(self):
        code, out, _err = self.run_main(["--where"])
        info = json.loads(out)
        self.assertEqual((code, info["ready"], info["enabled"]), (2, False, False))
        component_state.set_enabled(self.home, "simon", True)
        kit = self.kit()
        self.env()
        code, out, _err = self.run_main(["--where"])
        info = json.loads(out)
        self.assertEqual((code, info["ready"], info["problem"]), (0, True, None))
        self.assertEqual(info["quickstart"], str(kit / "QUICKSTART.md"))
        self.assertEqual(info["runtime"], str(self.home / "simon" / "runtime"))


# ------------------------------------------------------------------------------------- the part
class SimonPartTests(tc.ComponentCase):
    def decide(self, **extra):
        ctx = dict(self.ctx(), **extra)
        return cs.decide(cs.load_manifest("simon"), ctx, status=component_simon.status)

    def test_it_is_off_unless_chosen_and_installed_when_the_wheels_are_bundled(self):
        self.assertEqual(self.decide()["state"], "skipped")
        self.assertEqual(self.decide(component_choices={"simon": "on"})["state"], "installed")
        self.assertEqual(self.decide(component_choices={"simon": "on"}, os="macos")["state"], "skipped")

    def replace_kit(self, wheels, drop=()):
        """Swap the home's Simon kit for one built from `wheels` (the files in `drop` locked but left out)."""
        import shutil
        shutil.rmtree(self.simon_kit)
        source = tc.fake_simon_kit(self.base / ("src-" + str(len(wheels))), wheels)
        for name in drop:
            (source / "wheels" / name).unlink()
        self.simon_kit = Path(kits.materialize(self.eh, "simon", source=source)["path"])

    def test_without_bundled_wheels_it_says_why_and_writes_nothing(self):
        self.seed_user_files()
        self.replace_kit({})
        state = component_simon.status(self.ctx())
        self.assertEqual(state["state"], "skipped")
        self.assertIn("INSTALL.md", state["reason"])
        before = self.snapshot()
        self.assertEqual(component_simon.plan(self.ctx()), [])
        self.assertEqual(component_simon.apply(self.ctx()), [])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(component_simon.verify(self.ctx())["state"], "held")
        self.assertEqual(self.decide(component_choices={"simon": "on"})["state"], "skipped")

    def test_a_missing_bundled_wheel_is_named(self):
        self.replace_kit({"w-1.0-py3-none-any.whl": b"w"}, drop=["w-1.0-py3-none-any.whl"])
        state = component_simon.status(self.ctx())
        self.assertEqual(state["state"], "skipped")
        self.assertIn("w-1.0-py3-none-any.whl", state["reason"])

    def test_apply_builds_the_environment_once_and_puts_the_skill_beside_each_app(self):
        self.seed_user_files()
        effects = component_simon.apply(self.ctx())
        self.assertEqual([call for call in self.machine.calls if call[0] == "simon-env"],
                         [("simon-env", "env", ["fake-1.0-py3-none-any.whl"])])
        skill = (self.simon_kit / ".claude" / "skills" / "simon-planner" / "SKILL.md").read_bytes()
        for folder in (self.user / ".claude" / "skills" / "simon-planner",
                       self.user / ".codex" / "skills" / "simon-planner",
                       self.machine.hermes / "skills" / "eutonos" / "simon-planner"):
            with self.subTest(folder=folder):
                self.assertEqual((folder / "SKILL.md").read_bytes(), skill)
                self.assertTrue((folder / "QUICKSTART.md").is_file())
        self.assertTrue((self.machine.hermes / "skills" / "eutonos" / "DESCRIPTION.md").is_file())
        info = simon_launch.diagnose(self.eh, platform="win32")
        self.assertIn("not switched on", info["problem"])  # the launcher follows the part's switch, not the files
        component_state.set_enabled(self.eh, "simon", True)
        self.assertTrue(simon_launch.diagnose(self.eh, platform="win32")["ready"])
        again = component_simon.apply(self.ctx(record={"effects": effects}))
        self.assertEqual(again, effects)
        self.assertEqual(len([call for call in self.machine.calls if call[0] == "simon-env"]), 1)

    def test_a_skill_the_person_already_has_is_left_alone(self):
        self.seed_user_files()
        mine = self.user / ".claude" / "skills" / "simon-planner" / "SKILL.md"
        mine.parent.mkdir(parents=True)
        mine.write_text("my own simon skill\n", encoding="utf-8")
        effects = component_simon.apply(self.ctx())
        self.assertEqual(mine.read_text(encoding="utf-8"), "my own simon skill\n")
        self.assertNotIn(str(mine), [item["target"] for item in effects])
        component_simon.remove(self.ctx(record={"effects": effects}), effects)
        self.assertEqual(mine.read_text(encoding="utf-8"), "my own simon skill\n")

    def test_remove_takes_out_the_environment_and_the_skills(self):
        self.seed_user_files()
        effects = component_simon.apply(self.ctx())
        result = component_simon.remove(self.ctx(record={"effects": effects}), effects)
        self.assertEqual(result["kept"], [])
        self.assertFalse(simon_launch.env_dir(self.eh).exists())
        self.assertFalse((self.user / ".claude" / "skills" / "simon-planner").exists())
        self.assertFalse((self.machine.hermes / "skills" / "eutonos").exists())

    def test_a_runtime_on_another_python_is_skipped_with_the_reason(self):
        ctx = self.ctx()
        ctx["seams"] = dict(ctx["seams"], python_tag=lambda ctx: "cp313-win_amd64")
        state = component_simon.status(ctx)
        self.assertEqual(state["state"], "skipped")
        self.assertIn("built for cp314-win_amd64", state["reason"])
        self.assertIn("cp313-win_amd64", state["reason"])

    def test_the_runtime_python_tag_comes_from_its_pyvenv_cfg(self):
        runtime = self.base / "rt"
        (runtime / "Scripts").mkdir(parents=True)
        (runtime / "pyvenv.cfg").write_text("home = C:\\P\nversion_info = 3.13.7.final.0\n", encoding="utf-8")
        tag = component_simon.runtime_tag({"runtime_python": str(runtime / "Scripts" / "python.exe")})
        self.assertTrue(tag.startswith("cp313-"), tag)
        self.assertTrue(component_simon.runtime_tag({}).startswith("cp%d%d-" % __import__("sys").version_info[:2]))

    def test_the_codex_rule_is_written_recorded_and_removed_with_the_part(self):
        self.seed_user_files()
        effects = component_simon.apply(self.ctx())
        rules = self.user / ".codex" / "rules" / "eutonos-simon.rules"
        self.assertEqual(rules.read_text(encoding="utf-8"), component_simon.CODEX_RULES_TEXT)
        item = next(effect for effect in effects if effect["target"] == str(rules))
        self.assertEqual((item["permission_entry"], item["kind"]), ("simon-codex-rule", "file-create"))
        self.assertEqual(sorted(effect["target"] for effect in effects if ".codex" in effect["target"]),
                         sorted([str(rules), str(self.user / ".codex" / "skills" / "simon-planner" / "SKILL.md"),
                                 str(self.user / ".codex" / "skills" / "simon-planner" / "QUICKSTART.md")]))
        result = component_simon.remove(self.ctx(record={"effects": effects}), effects)
        self.assertEqual(result["kept"], [])
        self.assertFalse(rules.exists())
        self.assertFalse((self.user / ".codex" / "rules").exists())  # the folder EUTONOS made goes too
        self.assertEqual((self.user / ".codex" / "config.toml").read_text(encoding="utf-8"), 'model = "x"\n')

    def test_no_codex_means_no_rule_and_a_rules_file_of_the_persons_is_kept(self):
        effects = component_simon.apply(self.ctx())
        self.assertFalse([effect for effect in effects if effect["target"].endswith(".rules")])
        self.seed_user_files()
        mine = self.user / ".codex" / "rules" / "eutonos-simon.rules"
        mine.parent.mkdir(parents=True)
        mine.write_text("# mine\n", encoding="utf-8")
        effects = component_simon.apply(self.ctx(record={"effects": effects}))
        self.assertEqual(mine.read_text(encoding="utf-8"), "# mine\n")
        self.assertNotIn(str(mine), [effect["target"] for effect in effects])

    def test_the_rule_is_the_declared_permission_entry(self):
        data = permissions.load()
        self.assertEqual(permissions.validate(data), [])
        entry = permissions.entry_by_id(data, "simon-codex-rule")
        self.assertEqual((entry["component"], entry["app"], entry["scope"], entry["writer"]),
                         ("simon", "codex", "user", "installer"))
        self.assertEqual(entry["file"], "{home}/.codex/rules/" + component_simon.CODEX_RULES)
        self.assertEqual(entry["value"], {"pattern": component_simon.CODEX_RULE_PATTERN, "decision": "allow"})
        self.assertIn('pattern = ["eutonos-simon"]', component_simon.CODEX_RULES_TEXT)
        self.assertIn('decision = "allow"', component_simon.CODEX_RULES_TEXT)
        self.assertIn("simon-codex-rule", permissions.USER_WIDE_WRITERS)
        self.assertNotIn("simon-codex-rule", permissions.WRITTEN)  # the part writes it, only while it is on
        manifest = cs.load_manifest("simon")
        self.assertEqual((manifest["permissions"], manifest["consent_lines"]), (["simon-codex-rule"], ["simon-codex-rule"]))

    def test_a_rebuilt_environment_is_kept_on_remove(self):
        effects = component_simon.apply(self.ctx())
        marker = simon_launch.env_dir(self.eh) / simon_launch.ENV_MARKER
        marker.write_text('{"someone": "else"}', encoding="utf-8")
        result = component_simon.remove(self.ctx(record={"effects": effects}), effects)
        self.assertTrue(result["kept"])
        self.assertTrue(marker.is_file())


# ---------------------------------------------------------------------------- the consent text
class SimonConsentTests(unittest.TestCase):
    def test_the_simon_lines_are_listed_apart_as_applying_only_when_it_is_switched_on(self):
        text = consent.render(None, None, os_name="windows")
        general, _, part = text.partition("Only if you switch on Simon desktop helper")
        self.assertNotIn("simon-codex-rule", general)
        section = part.split("\n\n")[0]
        self.assertIn("Lets agents move the mouse and type on this desktop", section)
        self.assertIn("[simon-codex-rule]", section)
        self.assertEqual(consent.off_parts().get("simon"), "Simon desktop helper")

    def test_setup_lists_the_desktop_control_line_only_when_simon_is_ticked(self):
        rows = [{"id": "core", "name": "Core", "state": "required"}, {"id": "simon", "name": "Simon desktop helper"}]
        on = consent.setup_lines({"location": "H", "parts": [rows[0], dict(rows[1], state="on")]})
        off = consent.setup_lines({"location": "H", "parts": [rows[0], dict(rows[1], state="off")]})
        self.assertIn("- Simon desktop helper is switched on. Lets agents move the mouse and type on this desktop "
                      "(Simon, the Windows desktop-control helper).", on)
        self.assertFalse([line for line in off if "mouse" in line])


# ----------------------------------------------------------------------- the instruction block
class SimonBlockTextTests(unittest.TestCase):
    def test_each_harness_gets_a_short_user_agnostic_block(self):
        for harness in simon_instructions.HARNESSES:
            with self.subTest(harness=harness):
                block = simon_instructions.block(harness)
                text = block.decode("utf-8")
                self.assertTrue(block.startswith(simon_instructions.START))
                self.assertLessEqual(len(block), simon_instructions.SIZE_CAP)
                for word in ("eutonos-simon helper.bridge_status", "helper.exit --via-app", "simon-planner"):
                    self.assertIn(word, text)
                for word in ("splat", "Rick", "C:/Users", "C:" + chr(92) + "Users"):
                    self.assertNotIn(word, text)
        self.assertIn("sandbox", simon_instructions.render("codex"))
        self.assertNotIn("sandbox", simon_instructions.render("claude"))

    def test_the_block_is_registered_for_uninstall(self):
        from eutonos import installer_effects as fx
        self.assertIs(fx.MODULES["simon"], simon_instructions)


class SimonBlockGateTests(unittest.TestCase):
    def rows(self, **simon):
        return [dict({"id": "simon", "state": "installed", "enabled": True}, **simon)]

    def test_the_block_needs_the_part_installed_and_on(self):
        self.assertIn("simon", installer._blocks_off([]))
        self.assertIn("simon", installer._blocks_off(self.rows(state="skipped", enabled=False)))
        self.assertIn("simon", installer._blocks_off(self.rows(enabled=False)))
        self.assertNotIn("simon", installer._blocks_off(self.rows()))

    def test_the_other_blocks_keep_their_rule(self):
        self.assertEqual(installer._blocks_off([]), {"simon"})  # crosstalk and memory stay on when no row says off
        off = installer._blocks_off([{"id": "crosstalk", "state": "installed", "enabled": False}])
        self.assertEqual(off, {"crosstalk", "simon"})


class SimonBlockToggleTests(tct.ToggleBase):
    def blocks(self):
        return {name: (self.user / name).read_text(encoding="utf-8") if (self.user / name).is_file() else ""
                for name in (".claude/CLAUDE.md", ".codex/AGENTS.md")}

    def test_the_block_follows_the_part_and_uninstall_leaves_the_files_as_they_were(self):
        manifests = self.parts("simon", simon={"default": "off"})
        self.seed_user_files()
        before = ti.snapshot(self.user)
        self.first(components=manifests)
        for text in self.blocks().values():
            self.assertNotIn("eutonos:simon", text)  # off by default: installed, switched off, no block
            self.assertIn("eutonos:memory", text)
        self.assertEqual(self.toggle("simon", True, components=manifests)["outcome"], "enabled")
        self.assertTrue(all("eutonos:simon" in text for text in self.blocks().values()))
        self.assertTrue([item for item in self.effect_ids() if item.startswith("instructions.simon.")])
        self.assertEqual(self.toggle("simon", False, components=manifests)["outcome"], "disabled")
        self.assertFalse([item for item in self.effect_ids() if item.startswith("instructions.simon.")])
        for text in self.blocks().values():
            self.assertNotIn("eutonos:simon", text)
            self.assertIn("eutonos:memory", text)
        self.toggle("simon", True, components=manifests)
        self.assertEqual(self.uninstall(components=manifests)["outcome"], "uninstalled")
        self.assertEqual(ti.snapshot(self.user), before)

    def test_a_build_without_the_part_writes_no_block(self):
        self.first(components=self.parts("fakecomp"))
        for text in self.blocks().values():
            self.assertNotIn("eutonos:simon", text)


# --------------------------------------------------------------------------- the end-turn line
class EndTurnLineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "eutonos-home"
        patcher = mock.patch.dict(os.environ, {"EUTONOS_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def reason(self):
        return role_hooks._end_turn_reason("check your work", {}, "n1", is_subagent=False)

    def test_the_line_appears_only_while_the_part_is_on(self):
        self.assertNotIn(role_hooks.END_TURN_SIMON, self.reason())
        component_state.set_enabled(self.home, "simon", False)
        self.assertNotIn(role_hooks.END_TURN_SIMON, self.reason())
        component_state.set_enabled(self.home, "simon", True)
        reason = self.reason()
        self.assertIn(role_hooks.END_TURN_SIMON, reason)
        self.assertTrue(reason.endswith(role_hooks.END_TURN_AUTHORITY))

    def test_an_unreadable_parts_file_never_breaks_the_hook(self):
        target = component_state.path(self.home)
        target.parent.mkdir(parents=True)
        target.write_text("{not json", encoding="utf-8")
        self.assertNotIn(role_hooks.END_TURN_SIMON, self.reason())


if __name__ == "__main__":
    unittest.main()
