"""Managed Crosstalk instruction block: placement, migration, preservation (temp homes only)."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from eutonos import completion_policy as cp
from eutonos import crosstalk_instructions as ci

ROOT = "C:/Users/x/EUTONOS"
OLD = "# EUTONOS cross-app communication (user-level pointer)\n\nOld text.\nMore.\n"
COMPLETION = cp.block().decode()
CLAUDE_FILE = (OLD + "\n" + COMPLETION + "\n\n## Claude chat sync\n\nKeep.\n").encode()
CODEX_FILE = ("# Working preferences\n\nPrefs.\n\n# EUTONOS cross-app communication\n\nOld codex text.\n\n"
              "# VM screenshots\n\nShots.\n\n" + COMPLETION + "\n").encode()


class CrosstalkInstructionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.codex, self.claude = base / "codex", base / "claude"
        self.codex.mkdir()
        self.claude.mkdir()
        self.cfile, self.lfile = self.codex / "AGENTS.md", self.claude / "CLAUDE.md"

    def plan(self, **kw):
        return ci.instruction_plan(root=ROOT, codex_home=self.codex, claude_home=self.claude, **kw)

    def test_body_text_and_root(self):
        body = ci.render("C:\\a\\b")
        self.assertTrue(body.startswith("## Crosstalk (cross-app chats)\n\nCrosstalk carries letters"))
        self.assertIn("Quick start: C:/a/b/docs/Crosstalk/QUICKSTART.md\n", body)
        self.assertIn("python -I C:/a/b/tools/crosstalk/crosstalk_p0_cli.py halt", body)
        self.assertNotIn("<EUTONOS>", body)
        self.assertEqual(len(body.splitlines()), 7)

    def test_fresh_files_get_block_at_top_and_rerun_is_noop(self):
        self.lfile.write_bytes(b"# Mine\nKeep.\n")
        result = ci.install(self.plan())
        self.assertEqual(result["outcome"], "installed", result)
        self.assertEqual(result["policy_sha256"], ci.policy_sha256(ROOT))
        self.assertEqual(self.lfile.read_bytes(), ci.block(ROOT) + b"\n\n# Mine\nKeep.\n")
        self.assertEqual(self.cfile.read_bytes(), ci.block(ROOT) + b"\n")
        again = ci.install(self.plan())
        self.assertEqual(again["written"], [])
        self.assertEqual(again["outcome"], "installed")

    def test_text_change_updates_and_customized_refuses(self):
        self.lfile.write_bytes(b"Pre\n" + ci.block("C:/old") + b"\nPost\n")
        result = ci.install(self.plan())
        self.assertEqual(result["outcome"], "installed", result)
        self.assertEqual(self.lfile.read_bytes(), b"Pre\n" + ci.block(ROOT) + b"\nPost\n")
        self.cfile.unlink()
        custom = ci.block(ROOT).replace(b"Crosstalk carries", b"My carries")
        self.lfile.write_bytes(custom)
        result = ci.install(self.plan())
        self.assertEqual(result["outcome"], "conflict")
        self.assertEqual(result["conflicts"][0]["code"], "customized-managed-block")
        self.assertEqual(self.lfile.read_bytes(), custom)
        self.assertFalse(self.cfile.exists())

    def test_unmanaged_detected_with_sha_and_not_written_without_flag(self):
        self.lfile.write_bytes(CLAUDE_FILE)
        self.cfile.write_bytes(CODEX_FILE)
        result = ci.install(self.plan())
        self.assertEqual(result["outcome"], "conflict")
        codes = {Path(r["path"]).name: r for r in result["conflicts"]}
        self.assertEqual(codes["CLAUDE.md"]["code"], "unmanaged-crosstalk-section")
        self.assertEqual(codes["CLAUDE.md"]["sha256"], cp.sha(OLD.rstrip("\n").encode()))
        self.assertEqual(codes["AGENTS.md"]["sha256"],
                         cp.sha(b"# EUTONOS cross-app communication\n\nOld codex text."))
        self.assertEqual(self.lfile.read_bytes(), CLAUDE_FILE)
        self.assertEqual(self.cfile.read_bytes(), CODEX_FILE)

    def test_replace_unmanaged_in_place_for_both_shapes(self):
        self.lfile.write_bytes(CLAUDE_FILE)
        self.cfile.write_bytes(CODEX_FILE)
        sections = {self.lfile: OLD.rstrip("\n").encode(),
                    self.cfile: b"# EUTONOS cross-app communication\n\nOld codex text."}
        result = ci.install(self.plan(replace_unmanaged={str(p): cp.sha(s) for p, s in sections.items()}))
        self.assertEqual(result["outcome"], "installed", result)
        new = ci.block(ROOT)
        self.assertEqual(self.lfile.read_bytes(), CLAUDE_FILE.replace(sections[self.lfile], new))
        self.assertEqual(self.cfile.read_bytes(), CODEX_FILE.replace(sections[self.cfile], new))
        self.assertTrue(self.lfile.read_bytes().startswith(new))
        self.assertIn(COMPLETION.encode(), self.lfile.read_bytes())
        self.assertIn(b"# Working preferences\n\nPrefs.\n\n" + new + b"\n\n# VM screenshots", self.cfile.read_bytes())
        self.assertIn(COMPLETION.encode(), self.cfile.read_bytes())
        self.assertEqual(ci.install(self.plan())["written"], [])

    def test_wrong_sha_refuses_all_writes(self):
        self.lfile.write_bytes(CLAUDE_FILE)
        result = ci.install(self.plan(replace_unmanaged={str(self.lfile): "0" * 64}))
        self.assertEqual(result["outcome"], "conflict")
        self.assertEqual(result["conflicts"][0]["code"], "unmanaged-crosstalk-section-sha-mismatch")
        self.assertEqual(self.lfile.read_bytes(), CLAUDE_FILE)
        self.assertFalse(self.cfile.exists())

    def test_crlf_files(self):
        raw = CODEX_FILE.replace(b"\n", b"\r\n")
        self.cfile.write_bytes(raw)
        section = b"# EUTONOS cross-app communication\r\n\r\nOld codex text."
        result = ci.install(self.plan(replace_unmanaged={str(self.cfile): cp.sha(section)}))
        self.assertEqual(result["outcome"], "installed", result)
        new = ci.block(ROOT).replace(b"\n", b"\r\n")
        self.assertEqual(self.cfile.read_bytes(), raw.replace(section, new))
        # CRLF managed block updates in place, and a fresh CRLF file keeps its endings.
        self.lfile.write_bytes(b"# Mine\r\n")
        ci.install(self.plan())
        self.assertEqual(self.lfile.read_bytes(), new + b"\r\n\r\n# Mine\r\n")
        self.assertEqual(ci.install(self.plan())["written"], [])

    def test_linked_path_refused(self):
        self.lfile.write_bytes(b"x\n")
        with mock.patch.object(Path, "is_symlink", lambda self: self.name == "CLAUDE.md"):
            result = ci.install(self.plan())
        self.assertEqual(result["outcome"], "conflict")
        self.assertEqual(result["conflicts"][0]["code"], "linked-instruction-path")
        self.assertEqual(self.lfile.read_bytes(), b"x\n")

    def test_stale_plan_refused(self):
        self.lfile.write_bytes(b"x\n")
        plan = self.plan()
        self.lfile.write_bytes(b"changed\n")
        result = ci.install(plan)
        self.assertEqual(result["outcome"], "attention-required")
        self.assertIn("stale-instruction-plan", result["error"])
        self.assertEqual(self.lfile.read_bytes(), b"changed\n")
        self.assertFalse(self.cfile.exists())

    def test_cli_inspect_json(self):
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            code = ci.main(["inspect", "--eutonos-root", ROOT, "--codex-home", str(self.codex),
                            "--claude-home", str(self.claude)])
        data = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(data["outcome"], "ready")
        self.assertEqual(data["policy_sha256"], ci.policy_sha256(Path(ROOT).absolute().as_posix()))

    def test_an_exact_unmarked_copy_is_wrapped_in_place_and_a_similar_one_is_pending(self):
        exact = ("# mine\n\n" + ci.render(ROOT) + "\n## Next\nkeep\n").encode()
        self.cfile.write_bytes(exact)
        self.lfile.write_bytes(b"## Crosstalk (cross-app chats)\n\nMy old paths: C:/old/EUTONOS.\n")
        plan = self.plan()
        self.assertEqual(plan["conflicts"], [])
        self.assertEqual([(row["path"], row["code"]) for row in plan["pending"]],
                         [(str(self.lfile), "similar-unmarked-copy")])
        self.assertEqual([(row["path"], row["mode"]) for row in plan["changes"]], [(str(self.cfile), "wrap")])
        result = ci.install(plan)
        self.assertEqual(result["outcome"], "installed", result)
        after = self.cfile.read_bytes()
        self.assertEqual(after.count(b"Crosstalk carries letters"), 1)
        self.assertTrue(after.startswith(b"# mine\n\n<!-- eutonos:crosstalk:start"))
        self.assertTrue(after.endswith(b"## Next\nkeep\n"))
        self.assertEqual(self.lfile.read_bytes(), b"## Crosstalk (cross-app chats)\n\nMy old paths: C:/old/EUTONOS.\n")
        shape = cp.undo_shape(exact, after, "wrap", ci.PATTERN)
        self.assertEqual(cp.restore_block(after, shape, ci.PATTERN, ci.START, ci.END), (exact, "removed"))


if __name__ == "__main__":
    unittest.main()
