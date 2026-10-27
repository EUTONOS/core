"""Simon's own test suite (``tools/simon/tests``), run by ``tools/test_shards.py``.

Simon's tests install a process-wide audit hook that guards its real runtime folder, so they run in a child process
from ``tools/simon``, exactly as ``python -B -m unittest discover -s tests -t .`` would there. The child gets no
``SIMON_RUNTIME_DIR`` (the installed launcher's setting) and no ``PYTHONPATH`` (Simon imports nothing from EUTONOS).
"""
import os
from pathlib import Path
import subprocess
import sys
import unittest

SIMON = Path(__file__).resolve().parents[1] / "tools" / "simon"


class SimonSuiteTests(unittest.TestCase):
    def test_simon_suite_passes(self):
        env = {key: value for key, value in os.environ.items() if key not in ("SIMON_RUNTIME_DIR", "PYTHONPATH")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        done = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                              cwd=SIMON, env=env, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=600)
        self.assertEqual(done.returncode, 0, (done.stdout + done.stderr)[-4000:])
        self.assertIn("OK", done.stderr[-400:])


if __name__ == "__main__":
    unittest.main()
