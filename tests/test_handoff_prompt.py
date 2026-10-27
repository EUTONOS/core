import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import handoff_prompt as hp

TABLE_MD = """# Future features

Some intro text.

## Built-in field tricks (running list)

Intro paragraph before the table.

| Trick | What it gives | Status | Built-in version needs |
| --- | --- | --- | --- |
| Bounded big-file search | Search huge logs at ~1 KB, not MBs | Working: `tools/bigsearch.py` | TOOLS policy line |
| Ghost trick | Does something imaginary | Working: `tools/does_not_exist.py` | Nothing yet |
| Memory portability | Catch up without re-briefing | Working: `handoffs/claude-memory/` | Automatic snapshot |

## Next heading

Unrelated content after the table.
"""


def run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = hp.main(argv)
    return rc, buf.getvalue()


class TempRootMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "docs"), exist_ok=True)
        with open(os.path.join(self.root, "docs", "FUTURE_FEATURES.md"), "w", encoding="utf-8") as fh:
            fh.write(TABLE_MD)
        os.makedirs(os.path.join(self.root, "tools"), exist_ok=True)
        with open(os.path.join(self.root, "tools", "bigsearch.py"), "w", encoding="utf-8") as fh:
            fh.write("# stub\n")

    def tearDown(self):
        self.tmp.cleanup()


class TestParseTable(TempRootMixin, unittest.TestCase):
    def test_parses_all_rows(self):
        rows = hp.load_rows(self.root)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["trick"], "Bounded big-file search")
        self.assertEqual(rows[2]["trick"], "Memory portability")

    def test_row_fields(self):
        rows = hp.load_rows(self.root)
        row = rows[0]
        self.assertIn("Search huge logs", row["gives"])
        self.assertIn("bigsearch.py", row["status"])
        self.assertIn("TOOLS policy", row["needs"])


class TestMatching(TempRootMixin, unittest.TestCase):
    def test_substring_match_case_insensitive(self):
        rows = hp.load_rows(self.root)
        # "bigsearch" isn't in the trick name; it's in the Status column's
        # backticked tool path, so matching falls back to full-row text.
        matches = hp.find_matches(rows, "BIGSEARCH")
        self.assertEqual(matches, [0])

    def test_substring_match_on_trick_name(self):
        rows = hp.load_rows(self.root)
        matches = hp.find_matches(rows, "PORTABILITY")
        self.assertEqual(matches, [2])

    def test_number_match(self):
        rows = hp.load_rows(self.root)
        matches = hp.find_matches(rows, "3")
        self.assertEqual(matches, [2])

    def test_ambiguous_match_returns_multiple(self):
        rows = hp.load_rows(self.root)
        # "o" appears in multiple trick names
        matches = hp.find_matches(rows, "o")
        self.assertGreater(len(matches), 1)

    def test_backticked_tool_outranks_prose_mention(self):
        rows = hp.load_rows(self.root) + [{"trick": "Handoff prompts", "gives": "Teach a rule",
                                            "status": "Used once (bigsearch → Codex)", "needs": "Generator"}]
        self.assertEqual(hp.find_matches(rows, "bigsearch"), [0])

    def test_no_match(self):
        rows = hp.load_rows(self.root)
        matches = hp.find_matches(rows, "nonexistent-trick-xyz")
        self.assertEqual(matches, [])


class TestRenderCli(TempRootMixin, unittest.TestCase):
    def test_render_claude_harness(self):
        rc, out = run_cli(["--root", self.root, "render", "bigsearch", "--harness", "claude"])
        self.assertEqual(rc, 0)
        self.assertIn("auto-memory", out.lower())
        self.assertIn("Bounded big-file search", out)
        self.assertIn("bigsearch.py", out)

    def test_render_codex_harness(self):
        rc, out = run_cli(["--root", self.root, "render", "bigsearch", "--harness", "codex"])
        self.assertEqual(rc, 0)
        self.assertIn("AGENTS-level notes", out)

    def test_render_by_number(self):
        rc, out = run_cli(["--root", self.root, "render", "1", "--harness", "claude"])
        self.assertEqual(rc, 0)
        self.assertIn("Bounded big-file search", out)

    def test_ambiguous_exits_2_and_lists(self):
        rc, out = run_cli(["--root", self.root, "render", "o", "--harness", "claude"])
        self.assertEqual(rc, 2)

    def test_missing_tool_note(self):
        rc, out = run_cli(["--root", self.root, "render", "ghost", "--harness", "claude"])
        self.assertEqual(rc, 0)
        self.assertIn("not present in this checkout", out)

    def test_json_output(self):
        rc, out = run_cli(["--root", self.root, "render", "bigsearch", "--harness", "claude", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["harness"], "claude")
        self.assertIn("prompt", data)

    def test_list_command(self):
        rc, out = run_cli(["--root", self.root, "list"])
        self.assertEqual(rc, 0)
        self.assertIn("1. Bounded big-file search", out)
        self.assertIn("3. Memory portability", out)

    def test_prompt_ends_with_confirmation_line(self):
        rc, out = run_cli(["--root", self.root, "render", "bigsearch", "--harness", "claude"])
        self.assertIn("Reply with one line confirming what you saved.", out)


if __name__ == "__main__":
    unittest.main()
