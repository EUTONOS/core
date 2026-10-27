"""ISU stage 2: each outside effect has one writer. These scans fail when a second writer of the user PATH or of the
Claude plugin registration appears in the package, so the single declared path (with its readback and undo) stays the only one."""
from pathlib import Path
import re
import unittest

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "src" / "eutonos"


def sources():
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(PACKAGE.glob("*.py"))}


class SingleWriterTests(unittest.TestCase):
    def test_only_the_installers_path_effect_and_remover_write_the_user_path(self):
        callers = {name for name, text in sources().items() if re.search(r"\.user_path_(set|delete)\(", text)}
        self.assertEqual(callers, {"installer_effects.py"})
        text = sources()["installer_effects.py"]
        for call in re.finditer(r"ports\.user_path_set\(", text):
            start = text.rfind("\ndef ", 0, call.start())
            self.assertIn(text[start:start + 40].split("(")[0].strip(), ("def path_effect", "def remove_path_entry"))

    def test_only_the_ports_touch_the_environment_registry_key(self):
        offenders = {name for name, text in sources().items()
                     if re.search(r"OpenKey\([^)]*PATH_KEY|\"Environment\"", text) and name != "installer_ports.py"}
        self.assertEqual(offenders, set())

    def test_the_core_part_writes_no_path_entry(self):
        text = sources()["component_core.py"]
        self.assertNotIn("user_path_set", text)
        self.assertNotIn("winreg", text)

    def test_only_the_installer_registers_the_claude_plugin(self):
        callers = {name for name, text in sources().items()
                   if re.search(r"\"marketplace\", \"add\"|\"plugin\", \"install\"", text)}
        self.assertEqual(callers, {"installer.py"})
        self.assertIn("def register_plugin(", sources()["installer.py"])

    def test_the_crosstalk_part_registers_nothing(self):
        text = sources()["component_crosstalk.py"]
        self.assertNotIn("claude_run(ctx, [\"plugin\", \"install\"", text)
        self.assertNotIn("\"marketplace\", \"add\"", text)

    def test_the_kits_standalone_script_is_the_only_other_registration_and_stays_outside_the_package(self):
        # tools/crosstalk/install.ps1 is the kit's own installer, not part of the EUTONOS package or its installer.
        script = (REPO / "tools" / "crosstalk" / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("plugin marketplace add", script)
        self.assertFalse(any("install.ps1" in text and "plugin" in text and "Start-Process" in text for text in sources().values()))


if __name__ == "__main__":
    unittest.main()
