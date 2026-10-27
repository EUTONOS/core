"""Deferred pickup is readable without RAM or a host reservation."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from eutonos import capture_policy
from eutonos.workflow_cli import main


class PendingCliTests(unittest.TestCase):
    def test_page_then_expand_exact_note_without_consuming_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = root / "context.json"
            context.write_text(json.dumps({"actor": "worker", "work_id": "task", "session_id": "session"}))
            for text in ("Made button green", "Moved button left"):
                capture_policy.defer(root, "worker", "task", "session", {"request": text, "result": "Done"})
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["pending", "--root", str(root), "--context", str(context), "--limit", "1"])
            page = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(page["total"], 2)
            self.assertEqual(page["next_offset"], 1)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["pending", "--root", str(root), "--context", str(context), "--entry", page["entries"][0]["id"]])
            expanded = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(expanded["entries"][0]["content"]["result"], "Done")
            self.assertEqual(len(capture_policy.snapshot(root, "worker", "task", "session")), 2)


if __name__ == "__main__":
    unittest.main()
