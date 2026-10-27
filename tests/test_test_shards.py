import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location("test_shards_tool",
                                              Path(__file__).resolve().parents[1] / "tools/test_shards.py")
shards = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shards)


class TestShardsTool(unittest.TestCase):
    def test_plan_balances_by_size_and_drops_empty_shards(self):
        with tempfile.TemporaryDirectory() as temp:
            files = []
            for name, size in (("test_big", 900), ("test_mid", 500), ("test_small", 400)):
                path = Path(temp) / f"{name}.py"
                path.write_bytes(b"x" * size)
                files.append(path)
            self.assertEqual(shards.plan(files, 2), [["tests.test_big"], ["tests.test_mid", "tests.test_small"]])
            self.assertEqual(len(shards.plan(files, 8)), 3)
            self.assertEqual(shards.plan(files, 0), [["tests.test_big", "tests.test_mid", "tests.test_small"]])

    def test_plan_prefers_recorded_cost_over_size(self):
        with tempfile.TemporaryDirectory() as temp:
            files = []
            for name, size in (("test_big", 900), ("test_mid", 500), ("test_small", 400)):
                path = Path(temp) / f"{name}.py"
                path.write_bytes(b"x" * size)
                files.append(path)
            costs = {"classes": {"tests.test_small.A": 100.0, "tests.test_big.A": 5.0, "tests.test_mid.A": 5.0}}
            self.assertEqual(shards.plan(files, 2, costs), [["tests.test_small"], ["tests.test_big", "tests.test_mid"]])
            # An unrecorded file is still planned (by size at the recorded seconds-per-byte rate).
            plan = shards.plan(files, 2, {"classes": {"tests.test_mid.A": 500.0}})
            self.assertEqual(sorted(name for bucket in plan for name in bucket),
                             ["tests.test_big", "tests.test_mid", "tests.test_small"])

    def test_plan_splits_costly_module_by_class_without_losing_classes(self):
        with tempfile.TemporaryDirectory() as temp:
            heavy = Path(temp) / "test_heavy.py"
            heavy.write_text("class A: pass\nclass B: pass\nclass C: pass\n", encoding="utf-8")
            light = Path(temp) / "test_light.py"
            light.write_text("class L: pass\n", encoding="utf-8")
            costs = {"classes": {"tests.test_heavy.A": 100.0, "tests.test_heavy.B": 80.0, "tests.test_heavy.C": 1.0,
                                "tests.test_light.L": 20.0}}
            plan = shards.plan([heavy, light], 2, costs)
            units = sorted(name for bucket in plan for name in bucket)
            self.assertEqual(units, ["tests.test_heavy.A", "tests.test_heavy.B", "tests.test_heavy^A^B", "tests.test_light"])
            self.assertEqual(len(plan), 2)
            # One shard gains nothing from splitting; no recorded classes means no split.
            self.assertEqual(sorted(n for b in shards.plan([heavy, light], 1, costs) for n in b),
                             ["tests.test_heavy", "tests.test_light"])
            self.assertEqual(sorted(n for b in shards.plan([heavy, light], 2) for n in b),
                             ["tests.test_heavy", "tests.test_light"])

    def test_plan_peels_slow_tests_from_a_single_huge_class(self):
        with tempfile.TemporaryDirectory() as temp:
            big = Path(temp) / "test_big.py"
            big.write_text("class Only:\n    def test_a(self): pass\n    def test_b(self): pass\n    def test_c(self): pass\n",
                           encoding="utf-8")
            other = Path(temp) / "test_other.py"
            other.write_text("class O: pass\n", encoding="utf-8")
            costs = {"classes": {"tests.test_big.Only": 300.0, "tests.test_other.O": 100.0},
                     "tests": {"tests.test_big.Only.test_a": 120.0, "tests.test_big.Only.test_b": 110.0,
                               "tests.test_big.Only.test_gone": 50.0}}
            plan = shards.plan([big, other], 2, costs)
            self.assertEqual(sorted(n for b in plan for n in b),
                             ["tests.test_big.Only.test_a", "tests.test_big.Only.test_b",
                              "tests.test_big^Only.test_a^Only.test_b", "tests.test_other"])

    def test_default_shards_scale_with_cpu_count(self):
        for cpus, expected in ((None, 1), (1, 1), (6, 2), (24, 8), (64, 8)):
            with mock.patch.object(shards.os, "cpu_count", return_value=cpus):
                self.assertEqual(shards.default_shards(), expected)

    def test_costs_record_and_reload_merging_modules(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "stress" / "test_costs.json"
            self.assertEqual(shards.load_costs(path), {"classes": {}, "tests": {}})
            (root / "one.json").write_text('{"classes": {"tests.test_a.X": 1.5, "tests.test_a.Y": 2.0}, "tests": {"tests.test_a.X.test_slow": 1.0}}', encoding="utf-8")
            (root / "two.json").write_text('{"classes": {"tests.test_b.Z": 4.0, "tests.test_a.X": 0.5}, "tests": {}}', encoding="utf-8")
            shards.record_costs(path, [root / "one.json", root / "two.json", root / "missing.json"])
            # A module split across shards sums its parts.
            self.assertEqual(shards.load_costs(path), {"classes": {"tests.test_a.X": 2.0, "tests.test_a.Y": 2.0, "tests.test_b.Z": 4.0},
                                                       "tests": {"tests.test_a.X.test_slow": 1.0}})
            (root / "three.json").write_text('{"classes": {"tests.test_a.X": 9.0}}', encoding="utf-8")
            shards.record_costs(path, [root / "three.json"])
            self.assertEqual(shards.load_costs(path), {"classes": {"tests.test_a.X": 9.0, "tests.test_b.Z": 4.0}, "tests": {}})
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(shards.load_costs(path), {"classes": {}, "tests": {}})

    def test_select_takes_globs_and_module_names(self):
        with tempfile.TemporaryDirectory() as temp:
            for name in ("test_a.py", "test_ab.py", "test_b.py", "helper.py"):
                (Path(temp) / name).write_text("")
            names = lambda only: [p.name for p in shards.select(Path(temp), only)]
            self.assertEqual(names("test_a"), ["test_a.py"])
            self.assertEqual(names("test_a*, tests.test_b"), ["test_a.py", "test_ab.py", "test_b.py"])
            self.assertEqual(names("test_b.py,test_b"), ["test_b.py"])
            self.assertEqual(names("missing"), [])

    def test_node_dir_prefers_path_then_known_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            bin_dir = Path(temp) / "OpenAI" / "Codex" / "bin"
            bin_dir.mkdir(parents=True)
            self.assertIsNone(shards.node_dir({"PATH": "", "LOCALAPPDATA": temp}))
            (bin_dir / "node.exe").write_text("")
            self.assertEqual(shards.node_dir({"PATH": "", "LOCALAPPDATA": temp}), str(bin_dir))

    def test_summarize_reads_counts_status_and_problems(self):
        text = ("..F\nFAIL: test_a (tests.test_x.T.test_a)\n---\nERROR: test_b (tests.test_x.T.test_b)\n"
                "Ran 3 tests in 1.250s\n\nFAILED (failures=1, errors=1)\n")
        self.assertEqual(shards.summarize(text), {
            "tests": 3, "seconds": 1.25, "status": "FAILED (failures=1, errors=1)",
            "problems": ["FAIL: test_a (tests.test_x.T.test_a)", "ERROR: test_b (tests.test_x.T.test_b)"]})
        self.assertEqual(shards.summarize("crashed")["tests"], 0)


class TestShardsChildCleanup(unittest.TestCase):
    def run_fixture(self, popen, count=2):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "tests").mkdir()
            for index in range(count):
                (root / "tests" / f"test_fixture{index}.py").write_text("# fixture\n", encoding="utf-8")
            logs = root / "logs"
            with mock.patch.object(shards.subprocess, "Popen", side_effect=popen), \
                    mock.patch.object(shards, "node_dir", return_value=None), \
                    contextlib.redirect_stdout(io.StringIO()):
                return shards.main([str(count), "--root", str(root), "--logs", str(logs)])

    def fake_child(self, failure=None, stubborn=False):
        child = mock.Mock()
        child.poll.return_value = None

        def wait(timeout=None):
            if timeout is None and failure is not None:
                raise failure
            if stubborn and not child.kill.called:
                raise subprocess.TimeoutExpired("fixture", timeout)
            child.poll.return_value = 0
            return 0

        child.wait.side_effect = wait
        return child

    def test_interrupted_wait_cleans_all_children_and_preserves_interrupt(self):
        interrupt = KeyboardInterrupt("fixture interruption")
        children = [self.fake_child(interrupt), self.fake_child(stubborn=True)]
        handles, homes = [], []

        def launch(*args, **kwargs):
            handles.append(kwargs["stdout"])
            homes.append(Path(kwargs["env"]["EUTONOS_HOME"]))
            return children[len(handles) - 1]

        with self.assertRaises(KeyboardInterrupt) as raised:
            self.run_fixture(launch)
        self.assertIs(raised.exception, interrupt)
        for child in children:
            child.terminate.assert_called_once()
            self.assertIsNotNone(child.poll())
            self.assertTrue(all(call.kwargs["timeout"] <= 2.0
                                for call in child.wait.call_args_list if call.kwargs))
        children[0].kill.assert_not_called()
        children[1].kill.assert_called_once()
        self.assertTrue(all(handle.closed for handle in handles))
        self.assertTrue(all(not home.exists() for home in homes))

    def test_partial_launch_failure_closes_unowned_log_and_reaps_started_child(self):
        failure = OSError("fixture launch failure")
        child = self.fake_child()
        handles, homes = [], []

        def launch(*args, **kwargs):
            handles.append(kwargs["stdout"])
            homes.append(Path(kwargs["env"]["EUTONOS_HOME"]))
            if len(handles) == 2:
                raise failure
            return child

        with self.assertRaises(OSError) as raised:
            self.run_fixture(launch)
        self.assertIs(raised.exception, failure)
        child.terminate.assert_called_once()
        child.wait.assert_called_once()
        self.assertTrue(all(handle.closed for handle in handles))
        self.assertTrue(all(not home.exists() for home in homes))

    def test_unexpected_summary_failure_reaps_remaining_child(self):
        failure = ValueError("fixture summary failure")
        children = [self.fake_child(), self.fake_child()]
        remaining = iter(children)
        with mock.patch.object(shards, "summarize", side_effect=failure):
            with self.assertRaises(ValueError) as raised:
                self.run_fixture(lambda *args, **kwargs: next(remaining))
        self.assertIs(raised.exception, failure)
        children[0].terminate.assert_not_called()
        children[1].terminate.assert_called_once()
        self.assertTrue(all(child.poll() is not None for child in children))

    def test_normal_exit_does_not_terminate_or_kill(self):
        children = [self.fake_child(), self.fake_child()]
        remaining = iter(children)
        self.assertEqual(self.run_fixture(lambda *args, **kwargs: next(remaining)), 0)
        for child in children:
            child.wait.assert_called_once_with()
            child.terminate.assert_not_called()
            child.kill.assert_not_called()

    def test_normal_failed_shard_preserves_exit_status_without_termination(self):
        child = self.fake_child()
        child.wait.side_effect = None
        child.wait.return_value = 1
        self.assertEqual(self.run_fixture(lambda *args, **kwargs: child, count=1), 1)
        child.terminate.assert_not_called()
        child.kill.assert_not_called()

    def test_cleanup_error_does_not_replace_original_failure(self):
        failure = RuntimeError("original wait failure")
        child = self.fake_child(failure)
        child.terminate.side_effect = OSError("fixture terminate failure")
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            with self.assertRaises(RuntimeError) as raised:
                self.run_fixture(lambda *args, **kwargs: child, count=1)
        self.assertIs(raised.exception, failure)
        self.assertIn("terminate failed", errors.getvalue())
        self.assertIsNotNone(child.poll())

    def test_real_owned_child_is_reaped_before_home_and_log_cleanup(self):
        real_popen = subprocess.Popen
        test_case = self
        children, handles, homes = [], [], []

        class InterruptOnce:
            def __init__(self, process):
                self.process = process
                self.interrupted = False

            def wait(self, timeout=None):
                if timeout is None and not self.interrupted:
                    self.interrupted = True
                    raise KeyboardInterrupt("disposable fixture")
                return self.process.wait(timeout=timeout)

            def poll(self):
                return self.process.poll()

            def terminate(self):
                test_case.assertTrue(all(not handle.closed for handle in handles))
                test_case.assertTrue(all(home.exists() for home in homes))
                self.process.terminate()

            def kill(self):
                self.process.kill()

        def launch(*args, **kwargs):
            handles.append(kwargs["stdout"])
            homes.append(Path(kwargs["env"]["EUTONOS_HOME"]))
            process = real_popen([sys.executable, "-B", "-c", "import time; time.sleep(30)"],
                                 **kwargs)
            children.append(process)
            return InterruptOnce(process)

        try:
            with self.assertRaises(KeyboardInterrupt):
                self.run_fixture(launch, count=1)
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].returncode)
            self.assertTrue(all(handle.closed for handle in handles))
            self.assertTrue(all(not home.exists() for home in homes))
        finally:
            # Own only this fixture's process, including when the regression fails.
            for process in children:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
