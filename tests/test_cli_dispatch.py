"""The `eutonos` lifecycle dispatcher: each verb reaches its main through a lazy import, and a missing module is
reported as "not built yet". Nothing here imports or runs a real installer: every target is a stand-in module."""
import contextlib
import io
import json
import sys
import types
import unittest
import unittest.mock

from eutonos import cli


def fake_module(name, seen, code=0):
    module = types.ModuleType(name)
    module.main = lambda argv=None: (seen.append((name, list(argv))), code)[1]
    return module


class DispatchTests(unittest.TestCase):
    def test_every_verb_reaches_its_main_with_the_leading_word_it_expects(self):
        expected = {"install": ("eutonos.installer", ["install", "W"]), "update": ("eutonos.installer", ["update", "W"]),
                    "rollback": ("eutonos.installer", ["rollback", "W"]),
                    "uninstall": ("eutonos.installer", ["uninstall", "W"]),
                    "patch": ("eutonos.installer", ["patch", "W"]), "self-check": ("eutonos.installer", ["self-check", "W"]),
                    "doctor": ("eutonos.doctor", ["W"]), "add-project": ("eutonos.setup_cli", ["add-project", "W"]),
                    "remove-project": ("eutonos.repository_lifecycle", ["W"])}
        self.assertEqual(set(expected), set(cli.LIFECYCLE))
        for verb, (module, argv) in expected.items():
            seen = []
            with unittest.mock.patch.dict(sys.modules, {module: fake_module(module, seen, 7)}):
                self.assertEqual(cli.main([verb, "W"]), 7, verb)
            self.assertEqual(seen, [(module, argv)], verb)

    def test_arguments_default_to_the_process_argv(self):
        seen = []
        with unittest.mock.patch.dict(sys.modules, {"eutonos.installer": fake_module("eutonos.installer", seen)}), \
                unittest.mock.patch.object(sys, "argv", ["eutonos", "install", "--dry-run"]):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(seen, [("eutonos.installer", ["install", "--dry-run"])])

    def test_a_missing_module_is_reported_not_built_yet_and_never_a_traceback(self):
        real = cli.importlib.import_module

        def missing(name, *args):
            if name == "eutonos.doctor":
                raise ModuleNotFoundError("No module named 'eutonos.doctor'", name="eutonos.doctor")
            return real(name, *args)

        err = io.StringIO()
        with unittest.mock.patch.object(cli.importlib, "import_module", missing), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["doctor", "--json"]), 2)
        said = json.loads(err.getvalue())
        self.assertEqual((said["outcome"], said["command"], said["module"]), ("not-built-yet", "doctor", "eutonos.doctor"))
        self.assertIn("not built yet", said["message"])

    def test_a_broken_import_inside_a_module_is_not_hidden_as_not_built_yet(self):
        def broken(name, *args):
            raise ModuleNotFoundError("No module named 'something_else'", name="something_else")

        with unittest.mock.patch.object(cli.importlib, "import_module", broken):
            with self.assertRaises(ModuleNotFoundError):
                cli.main(["install"])

    def test_other_arguments_still_reach_the_read_core_parser(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            cli.main(["no-such-operation"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--root", err.getvalue())


if __name__ == "__main__":
    unittest.main()
