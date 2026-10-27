"""crosstalk_status is a read-only reader of the Crosstalk device controls, and
local_state holds the neutral atomic-write helper."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from eutonos import crosstalk_status as cs
from eutonos import local_state


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.device = self.root / "device"
        self.eutonos_home = self.root / "eutonos-home"  # never the real ~/.eutonos
        env = mock.patch.dict(os.environ, {cs.DEVICE_DIR_ENV: str(self.device), "EUTONOS_HOME": str(self.eutonos_home)})
        env.start()
        self.addCleanup(env.stop)

    def controls(self, value):
        self.device.mkdir(exist_ok=True)
        text = value if isinstance(value, str) else json.dumps(value)
        (self.device / "controls.json").write_text(text, encoding="utf-8")

    def checkout(self):
        cli = self.root / "checkout" / cs.CLI_RELATIVE
        cli.parent.mkdir(parents=True)
        cli.write_text("#\n", encoding="utf-8")
        return self.root / "checkout"


class DeviceDirTests(Base):
    def test_env_override_then_home_default(self):
        self.assertEqual(cs.device_dir(), self.device)
        with mock.patch.dict(os.environ, {cs.DEVICE_DIR_ENV: "", "USERPROFILE": str(self.root), "HOME": str(self.root)}):
            os.environ.pop("EUTONOS_HOME", None)
            self.assertEqual(cs.device_dir(), self.root / ".eutonos" / "crosstalk-probe")
            os.environ["EUTONOS_HOME"] = str(self.root / "elsewhere")
            self.assertEqual(cs.device_dir(), self.root / "elsewhere" / "crosstalk-probe")


class StatusTests(Base):
    def test_absent_device_folder_is_not_installed_and_never_raises(self):
        state = cs.status()
        self.assertEqual((state["installed"], state["available"], state["reason"]),
                         (False, False, "crosstalk-not-installed"))
        self.assertIsNone(cs.pickup_hint())
        self.assertFalse(self.device.exists())  # read-only: nothing was created

    def test_missing_or_damaged_controls_mean_messaging_off(self):
        self.device.mkdir()
        for value in (None, "not json", "[1]", '{"messaging": "yes"}'):
            with self.subTest(value=value):
                if value is not None:
                    self.controls(value)
                state = cs.status()
                self.assertEqual((state["messaging"], state["halt"], state["reason"]),
                                 ("off", False, "crosstalk-messaging-off"))

    def test_on_halted_and_denied(self):
        self.controls({"messaging": "on", "deny": ["codex:a", "claude:b", 7]})
        state = cs.status()
        self.assertEqual((state["available"], state["reason"], state["denied"]), (True, None, 2))
        self.controls({"messaging": "on", "halt": True})
        halted = cs.status()
        self.assertEqual((halted["available"], halted["reason"]), (False, "crosstalk-halted"))
        # halt beats off
        self.controls({"messaging": "off", "halt": True})
        self.assertEqual(cs.status()["reason"], "crosstalk-halted")

    def test_cli_presence_is_only_checked_when_roots_are_given(self):
        self.controls({"messaging": "on"})
        self.assertTrue(cs.status()["available"])
        empty = self.root / "empty"
        empty.mkdir()
        self.assertEqual(cs.status(empty)["reason"], "crosstalk-cli-missing")
        checkout = self.checkout()
        state = cs.status(empty, checkout)
        self.assertTrue(state["available"])
        self.assertEqual(state["cli"], str(checkout / cs.CLI_RELATIVE))

    def materialize_kit(self, body):
        from eutonos import kits
        source = self.root / "kit-source"
        source.mkdir(exist_ok=True)
        (source / "crosstalk_p0_cli.py").write_text(body, encoding="utf-8")
        return kits.materialize(self.eutonos_home, "crosstalk", source=source)

    def test_ipc4_f4_cli_path_falls_back_to_the_active_kit_when_no_checkout_has_it(self):
        self.assertIsNone(cs.cli_path(self.root / "empty"))
        kit = self.materialize_kit("# cli\n")
        found = cs.cli_path(self.root / "empty")
        self.assertEqual(found, Path(kit["path"]) / "crosstalk_p0_cli.py")
        self.assertTrue(cs.cli_path().is_file())
        checkout = self.checkout()  # a checkout still wins, so checkout mode is unchanged
        self.assertEqual(cs.cli_path(checkout), checkout / cs.CLI_RELATIVE)
        self.controls({"messaging": "on"})
        self.assertTrue(cs.status(self.root / "empty")["available"])
        (Path(kit["path"]) / "crosstalk_p0_cli.py").write_text("tampered", encoding="utf-8")  # fails verify
        self.assertIsNone(cs.cli_path(self.root / "empty"))

    def test_ipc4_f4_owner_control_finds_the_kit_cli_without_a_checkout_root(self):
        self.materialize_kit("\n".join([
            "import json, os, sys",
            "folder = os.environ['CROSSTALK_P0_DEVICE_DIR']",
            "os.makedirs(folder, exist_ok=True)",
            "state = {'messaging': 'on', 'halt': sys.argv[1] == 'halt'}",
            "open(os.path.join(folder, 'controls.json'), 'w').write(json.dumps(state))", ""]))
        self.controls({"messaging": "on"})
        with mock.patch.object(cs, "default_roots", return_value=[self.root / "no-checkout"]):
            result = cs.owner_control("halt")
        self.assertTrue(result["ok"], result)
        self.assertTrue(cs.controls()["halt"])

    def test_every_reason_has_text(self):
        for code in ("crosstalk-not-installed", "crosstalk-cli-missing", "crosstalk-halted",
                     "crosstalk-messaging-off"):
            self.assertNotEqual(cs.reason_text(code), cs.reason_text(None))
        self.assertEqual(cs.reason_text(None), "Crosstalk is unavailable.")

    def test_pickup_hint_lines(self):
        self.controls({"messaging": "on", "deny": ["codex:a"]})
        hint = cs.pickup_hint()
        self.assertIn("crosstalk: messaging on, 1 denied", hint["line"])
        self.assertIn("crosstalk_send", hint["line"])
        self.controls({"messaging": "on", "halt": True})
        self.assertIn("HALTED", cs.pickup_hint()["line"])
        self.controls({})
        self.assertIn("messaging OFF", cs.pickup_hint()["line"])


class LocalStateTests(unittest.TestCase):
    def test_atomic_write(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            target = root / "a" / "b.json"
            target.parent.mkdir()
            local_state.atomic_write(target, b"one")
            local_state.atomic_write(target, b"two")
            self.assertEqual(target.read_bytes(), b"two")
            self.assertEqual([p.name for p in target.parent.iterdir()], ["b.json"])  # no tmp left

    def test_local_state_does_not_import_the_mailbox(self):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(local_state))
        names = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
                {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        self.assertFalse(any(name and "mailbox" in name for name in names))


if __name__ == "__main__":
    unittest.main()
