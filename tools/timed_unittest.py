"""Run unittest names like ``python -m unittest`` and record wall seconds per TestCase class.

Used by tools/test_shards.py so it can balance shards by measured cost.
Names are the usual unittest ones (module, module.Class, module.Class.test),
plus ``module^ClassA^ClassB.test`` meaning "all of module except those classes or tests"
(the remainder of a module whose big parts run in another shard).

Time between one test finishing and the next finishing is attributed to the
later test's class, so setUpClass/setUpModule cost is counted. Costs are keyed
``<loading module>.<class>``. The stream and the "Ran N tests in Xs" / "OK"
lines are the stock unittest ones.

  python tools/timed_unittest.py --costs-out costs.json tests.test_a tests.test_b^Slow
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
import unittest

SLOW_TEST_SECONDS = 3.0  # keep in step with tools/test_shards.py


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def load(name: str, loader: unittest.TestLoader):
    """Return [(cost_key, test)] for one unit name."""
    module_name, *skipped = name.split("^")
    try:
        module = importlib.import_module(module_name)
        owner = module_name
    except Exception:
        owner = module_name.rpartition(".")[0]
        # module.Class or module.Class.test: the owning module is the longest importable prefix.
        while owner:
            try:
                importlib.import_module(owner)
                break
            except Exception:
                owner = owner.rpartition(".")[0]
        module = None
    skip = {f"{owner}.{cls}" for cls in skipped}
    suite = loader.loadTestsFromModule(module) if module is not None else loader.loadTestsFromName(module_name)
    tests = []
    for test in flatten(suite):
        key = f"{owner}.{type(test).__qualname__}"
        method = f"{key}.{getattr(test, '_testMethodName', '?')}"
        if key not in skip and method not in skip:
            tests.append((key, test))
    return tests


class TimedResult(unittest.TextTestResult):
    keys: dict[int, str] = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.class_seconds: dict[str, float] = {}
        self.test_seconds: dict[str, float] = {}
        self._mark = time.monotonic()
        self._test_start = self._mark

    def startTest(self, test):
        self._test_start = time.monotonic()
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        now = time.monotonic()
        key = self.keys.get(id(test)) or f"{type(test).__module__}.{type(test).__qualname__}"
        self.class_seconds[key] = self.class_seconds.get(key, 0.0) + (now - self._mark)
        self.test_seconds[f"{key}.{getattr(test, '_testMethodName', '?')}"] = now - self._test_start
        self._mark = now


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--costs-out", help="write class seconds and slow-test seconds JSON here")
    parser.add_argument("--tests-out", help="write per-test seconds JSON here (profiling)")
    parser.add_argument("names", nargs="+")
    args = parser.parse_args(argv)
    if "" not in sys.path:
        sys.path.insert(0, "")
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    keys = {}
    for name in args.names:
        for key, test in load(name, loader):
            keys[id(test)] = key
            suite.addTest(test)
    TimedResult.keys = keys
    result = unittest.TextTestRunner(stream=sys.stderr, resultclass=TimedResult).run(suite)
    slow = {k: v for k, v in result.test_seconds.items() if v >= SLOW_TEST_SECONDS}
    outputs = ((args.costs_out, {"classes": result.class_seconds, "tests": slow}),
               (args.tests_out, result.test_seconds))
    for path, data in outputs:
        if path:
            try:
                with open(path, "w", encoding="utf-8") as handle:
                    if "classes" in data:
                        data = {part: {k: round(v, 3) for k, v in values.items()} for part, values in data.items()}
                    else:
                        data = {k: round(v, 3) for k, v in data.items()}
                    json.dump(data, handle, indent=1, sort_keys=True)
            except OSError as error:
                print(f"timing output not written: {error!r}", file=sys.stderr)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
