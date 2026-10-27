"""Test-only isolation shared by every deterministic test module.

The audit hook blocks real-runtime I/O before it happens, including worker-thread
I/O. The base case also fails on swallowed violations. Stat and path predicates
are covered explicitly because CPython does not emit audit events for them.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock


REAL_RUNTIME = Path(__file__).resolve().parent.parent / ".runtime"
_REAL_RUNTIME_KEY = os.path.normcase(os.path.abspath(REAL_RUNTIME))
_violations: list[str] = []


def _check_path(path, event: str) -> None:
    if not isinstance(path, (str, bytes, os.PathLike)):
        return
    # abspath is lexical: checking a path must never probe the runtime itself.
    key = os.path.normcase(os.path.abspath(os.fsdecode(path)))
    if key == _REAL_RUNTIME_KEY or key.startswith(_REAL_RUNTIME_KEY + os.sep):
        message = f"Test accessed real .runtime: {event}: {path}"
        _violations.append(message)
        raise AssertionError(message)


def _audit(event: str, args: tuple) -> None:
    if event in {"open", "os.mkdir", "os.remove", "os.rmdir", "os.listdir", "os.scandir", "os.chmod", "os.utime", "os.truncate", "os.chdir"}:
        _check_path(args[0], event)
    elif event in {"os.rename", "os.link", "os.symlink", "shutil.copyfile", "shutil.copymode", "shutil.copystat"}:
        _check_path(args[0], event)
        _check_path(args[1], event)


sys.addaudithook(_audit)
_original_stat = os.stat
_original_lstat = os.lstat


def _guarded_stat(path, *args, **kwargs):
    _check_path(path, "os.stat")
    return _original_stat(path, *args, **kwargs)


def _guarded_lstat(path, *args, **kwargs):
    _check_path(path, "os.lstat")
    return _original_lstat(path, *args, **kwargs)


os.stat = _guarded_stat
os.lstat = _guarded_lstat


def _guard_predicate(predicate, event):
    def guarded(path, *args, **kwargs):
        _check_path(path, event)
        return predicate(path, *args, **kwargs)
    return guarded


# Python 3.14 on Windows can use nt._path_exists instead of os.stat.
for _name in ("exists", "lexists", "isfile", "isdir"):
    setattr(os.path, _name, _guard_predicate(getattr(os.path, _name), f"os.path.{_name}"))


class RuntimeGuardedTestCase(unittest.TestCase):
    def run(self, result=None):
        result = result if result is not None else self.defaultTestResult()
        before = len(_violations)
        returned = super().run(result)
        if len(_violations) > before:
            try:
                raise AssertionError("\n".join(_violations[before:]))
            except AssertionError:
                result.addFailure(self, sys.exc_info())
        return returned


def redirect_runtime(test: unittest.TestCase, runtime_dir: Path) -> dict[str, Path]:
    """Redirect every imported path alias to one temporary root.

    Compatibility exports are snapshots, so patch the module that owns a
    function as well as legacy host/router aliases. No runtime paths are
    inferred from a partial per-fixture list.
    """
    import simon_paths

    values = {}
    for name, value in vars(simon_paths).items():
        if isinstance(value, Path) and (value == REAL_RUNTIME or REAL_RUNTIME in value.parents):
            values[name] = runtime_dir / value.relative_to(REAL_RUNTIME)
    for module_name in ("simon_paths", "simon_common", "simon_artifacts", "simon_images", "helperctl", "agent_helper", "simon_target"):
        module = sys.modules.get(module_name)
        if module is None:
            continue
        patches = {name: value for name, value in values.items() if hasattr(module, name)}
        if patches:
            patcher = mock.patch.multiple(module, **patches)
            patcher.start()
            test.addCleanup(patcher.stop)
    return values
