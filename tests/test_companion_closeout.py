import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "tools" / "companion_closeout.py"


class CompanionCloseoutTests(unittest.TestCase):
    def run_helper(self, *args):
        return subprocess.run([sys.executable, str(HELPER), *map(str, args)], cwd=ROOT, text=True, capture_output=True)

    def test_init_creates_marked_git_fixture_and_isolated_host_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "fixture"
            result = self.run_helper("init-fixture", "--root", root)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["outcome"], "initialized")
            self.assertTrue((root / ".companion-closeout-fixture.json").is_file())
            self.assertNotEqual(Path(payload["host"]).resolve().parent, root.resolve())
            self.assertFalse(Path(payload["host"]).exists())
            self.assertTrue((root / ".git").is_dir())

    def test_init_refuses_nonempty_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "fixture"
            root.mkdir(); (root / "foreign.txt").write_text("x", encoding="utf-8")
            result = self.run_helper("init-fixture", "--root", root)
            self.assertNotEqual(result.returncode, 0)

    def test_closeout_requires_marked_fixture_and_request_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "fixture"; root.mkdir()
            request = Path(td) / "request.json"; request.write_text("{}", encoding="utf-8")
            result = self.run_helper("closeout", "--root", root, "--request", request)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unmarked root", result.stderr)

    def test_closeout_smoke_applies_reads_back_and_releases_fixture_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "fixture"
            initialized = self.run_helper("init-fixture", "--root", root)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            request = root / "request.json"
            request.write_text(json.dumps({"summary": "Fixture smoke", "result": {"status": "ok"}, "evidence": ["docs/CURRENT_TASK.md"], "identity": {"issuer_id": "terra", "memory_id": "terra-closeout"}, "actor": "fixture-terra", "at": "2026-09-12T20:40:00Z"}), encoding="utf-8")
            result = self.run_helper("closeout", "--root", root, "--request", request)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["outcome"], "closed")
            self.assertEqual(payload["closeout"]["remainingReservations"], [])
            # Since the RAM identity-path fix (commit 2e51ae2, "Fix RAM identity
            # paths before AWR2"), every new identity -- including one for a
            # configured work owner, which COMPANION-SMOKE is -- uses the flat,
            # content-addressed "RAM/record-<sha256>.json" path; the issuer_id is
            # no longer embedded in the path. Confirm the custom "terra" identity
            # from the request still flows through by reading the record itself.
            self.assertTrue(payload["record_path"].startswith("RAM/record-"))
            record = json.loads((root / payload["record_path"]).read_text(encoding="utf-8"))
            self.assertEqual(record["identity"]["issuer_id"], "terra")
            self.assertTrue((root / "result.md").is_file())

    def test_invalid_request_and_dirty_baseline_do_not_start_host_or_write_docs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "fixture"
            initialized = self.run_helper("init-fixture", "--root", root)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            task = root / "docs" / "CURRENT_TASK.md"
            before = task.read_bytes()
            request = Path(td) / "request.json"
            request.write_text(json.dumps({"summary": "x", "result": "x", "evidence": []}), encoding="utf-8")
            result = self.run_helper("closeout", "--root", root, "--request", request)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(task.read_bytes(), before)
            self.assertFalse((Path(td) / "fixture-host").exists())
            self.assertFalse((root / "result.md").exists())
            task.write_bytes(before + b"\nConcurrent edit\n")
            dirty = task.read_bytes()
            request.write_text(json.dumps({"summary": "x", "result": "x", "evidence": ["source"]}), encoding="utf-8")
            result = self.run_helper("closeout", "--root", root, "--request", request)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("tracked changes", result.stderr)
            self.assertEqual(task.read_bytes(), dirty)
            self.assertFalse((Path(td) / "fixture-host").exists())


if __name__ == "__main__":
    unittest.main()
