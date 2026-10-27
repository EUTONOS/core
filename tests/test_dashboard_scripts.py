"""Run every standalone dashboard JavaScript check under unittest discovery."""

from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = tuple(sorted(Path(__file__).resolve().parent.glob("test_dashboard_*.cjs")))
NODE = shutil.which("node")


class DashboardScriptsTests(unittest.TestCase):
    @unittest.skipUnless(NODE, "Node is required for standalone dashboard checks")
    def test_every_dashboard_script(self):
        self.assertTrue(SCRIPTS, "No standalone dashboard scripts were discovered")
        for script in SCRIPTS:
            with self.subTest(script=script.name):
                result = subprocess.run(
                    [NODE, str(script)],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    f"{script.name} failed with exit code {result.returncode}\n"
                    f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
                )


if __name__ == "__main__":
    unittest.main()
