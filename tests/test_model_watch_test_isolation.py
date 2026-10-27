"""Official shards protect the real per-user watch store before imports."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


class UserStateIsolationTests(unittest.TestCase):
    def test_shards_override_inherited_user_store_and_clean_after_children(self):
        path = Path(__file__).parents[1] / "tools/test_shards.py"
        spec = importlib.util.spec_from_file_location("isolated_test_shards", path)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "tests").mkdir()
            for name in ("test_a.py", "test_b.py"):
                (root / "tests" / name).write_text("# fixture\n", encoding="utf-8")
            inherited = root / "owner-home"
            inherited.mkdir()
            sentinel = inherited / "baseline.json"
            sentinel.write_bytes(b"owner data unchanged")
            homes = []

            def child(argv, *, cwd, env, stdout, stderr):
                user_home = Path(env["EUTONOS_HOME"])
                self.assertNotEqual(user_home, inherited)
                self.assertTrue(user_home.is_dir())
                homes.append(user_home)
                (user_home / "watch-test.json").write_text("{}", encoding="utf-8")
                stdout.write("Ran 1 test in 0.001s\n\nOK\n")
                return mock.Mock(wait=mock.Mock(return_value=0))

            with mock.patch.dict(os.environ, {"EUTONOS_HOME": str(inherited)}), \
                    mock.patch.object(runner.subprocess, "Popen", side_effect=child):
                self.assertEqual(runner.main(["2", "--root", str(root)]), 0)
                self.assertEqual(os.environ["EUTONOS_HOME"], str(inherited))
            self.assertEqual(len(homes), 2)
            self.assertEqual(homes[0], homes[1])
            self.assertFalse(homes[0].exists())
            self.assertEqual(sentinel.read_bytes(), b"owner data unchanged")
