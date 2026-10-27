"""Instruction preservation, policy distribution and completion semantics (no model calls)."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from eutonos import completion_policy as cp
from eutonos import adoption, role_hooks


class CompletionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        self.codex = self.base / "codex"
        self.claude = self.base / "claude"

    def plan(self, **kwargs):
        return cp.instruction_plan(root=self.root, global_instructions=True,
                                   codex_home=self.codex, claude_home=self.claude, **kwargs)

    def test_install_preserves_user_bytes_and_reinstall_is_noop(self):
        self.codex.mkdir()
        original = b"\xef\xbb\xbf# Personal notes\r\nKeep my workflow.\r\n\r\n"
        path = self.codex / "AGENTS.md"
        path.write_bytes(original)
        first = cp.install(self.plan())
        self.assertEqual(first["outcome"], "installed", first)
        self.assertEqual(len(first["written"]), 4)
        self.assertTrue(path.read_bytes().startswith(original))
        self.assertIn(cp.CONTRACT, path.read_text(encoding="utf-8-sig"))
        snapshots = {row["path"]: Path(row["path"]).read_bytes() for row in first["files"]}
        second = cp.install(self.plan())
        self.assertEqual(second["written"], [])
        self.assertEqual(snapshots, {path: Path(path).read_bytes() for path in snapshots})
        self.assertIn("@AGENTS.md", (self.root / "CLAUDE.md").read_text())

    def test_upgrade_updates_only_intact_managed_block_and_preserves_tail(self):
        original = b"User prefix\n" + cp.block("Earlier shipped contract") + b"\nUser suffix\n"
        changed = cp.merge_block(original)
        self.assertTrue(changed.startswith(b"User prefix\n"))
        self.assertTrue(changed.endswith(b"\nUser suffix\n"))
        self.assertIn(cp.CONTRACT.encode(), changed)
        for raw in (original.replace(b"Earlier", b"Customized"), cp.block() + cp.block(), cp.START):
            with self.subTest(raw=raw[:50]), self.assertRaises(ValueError):
                cp.merge_block(raw)

    def test_customized_block_stops_all_instruction_writes(self):
        path = self.root / "AGENTS.md"
        raw = cp.block().replace(b"Requests", b"My requests")
        path.write_bytes(raw)
        result = cp.install(self.plan())
        self.assertEqual(result["outcome"], "conflict")
        self.assertEqual(result["written"], [])
        self.assertEqual(path.read_bytes(), raw)
        self.assertFalse(self.codex.exists())

    def test_precedence_layers_need_exact_review_and_are_not_overwritten(self):
        paths = [self.codex / "AGENTS.override.md", self.root / "AGENTS.override.md",
                 self.root / "sub" / "AGENTS.md", self.root / ".claude/rules/review.md",
                 self.claude / "rules/global.md", self.base / "CLAUDE.local.md"]
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Custom scoped instructions\n")
        plan = self.plan()
        self.assertEqual({row["path"] for row in plan["conflicts"]}, {str(path) for path in paths})
        reviewed = {row["path"]: row["sha256"] for row in plan["conflicts"]}
        result = cp.install(self.plan(reviewed=reviewed))
        self.assertEqual(result["outcome"], "installed", result)
        for path in paths:
            self.assertEqual(path.read_text(), "Custom scoped instructions\n")
        paths[0].write_text("Changed override\n")
        self.assertEqual(cp.install(self.plan(reviewed=reviewed))["outcome"], "conflict")

    def test_changed_file_or_new_nested_guidance_refuses_stale_plan(self):
        for mutation in ("target", "nested"):
            with self.subTest(mutation=mutation):
                plan = self.plan()
                path = self.root / ("AGENTS.md" if mutation == "target" else "AGENTS.override.md")
                path.write_text("Concurrent change\n")
                self.assertEqual(cp.install(plan)["outcome"], "attention-required")
                self.assertEqual(path.read_text(), "Concurrent change\n")
                path.unlink()

    def test_existing_claude_import_and_explicit_home_configuration(self):
        raw = b"# Existing\n@AGENTS.md\nOther instructions.\n"
        (self.root / "CLAUDE.md").write_bytes(raw)
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex), "CLAUDE_CONFIG_DIR": str(self.claude)}):
            result = cp.install(cp.instruction_plan(root=self.root, global_instructions=True))
        self.assertEqual(result["outcome"], "installed")
        self.assertEqual((self.root / "CLAUDE.md").read_bytes(), raw)
        self.assertTrue((self.codex / "AGENTS.md").is_file())
        self.assertTrue((self.claude / "CLAUDE.md").is_file())

    def test_scan_cap_and_unreadable_text_report_unknown_not_absence(self):
        (self.root / "file").write_text("x")
        with mock.patch.object(cp, "MAX_ENTRIES", 0):
            self.assertTrue(self.plan()["conflicts"])
        (self.root / "AGENTS.md").write_bytes(b"\xff")
        self.assertEqual(cp.install(self.plan())["outcome"], "conflict")

    def test_same_canonical_contract_in_adoption_and_custom_end_turn_text(self):
        self.assertNotIn(cp.CONTRACT, adoption.DELEGATION_BLOCK)
        custom = "My custom reminder must remain unchanged."
        for child in (False, True):
            result = role_hooks._end_turn_reason(custom, {}, "fixture", is_subagent=child)
            self.assertIn(custom, result)
            self.assertEqual(result.count(cp.CONTRACT), 1)
        # Representative interpretation fixtures pin instruction semantics, not
        # an unsupported claim that a model or Git state was evaluated by code.
        for requirement in ("unmerged implementation remains unfinished", "exploration, research, testing, prototypes",
                            "target state is verified", "coordinator owns", "campaign branch, not main",
                            "deployment, installation, restart or release"):
            self.assertIn(requirement, cp.CONTRACT)


class DecisionsAndMergeTests(unittest.TestCase):
    """The Decisions block and the shared managed-block cases (add, refresh, wrap, similar, restore)."""

    MARK = (cp.DECISIONS_START, cp.DECISIONS_END, cp.DECISIONS_PATTERN)
    BODY = cp.block_body(cp.DECISIONS, cp.DECISIONS_HEADING)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.codex, self.claude = self.base / "codex", self.base / "claude"
        self.codex.mkdir()
        self.claude.mkdir()

    def merge(self, raw):
        return cp.merge_managed(raw, self.MARK, self.BODY)

    def test_decisions_text_is_the_approved_wording_with_the_bold_label(self):
        self.assertTrue(cp.DECISIONS.startswith("Decide what an existing rule, test, sibling pattern"))
        self.assertIn("a line starting with a bold **Decided:** label and the reason", cp.DECISIONS)
        self.assertTrue(cp.DECISIONS.endswith("put questions last, each with a recommendation and reason."))
        self.assertTrue(cp.decisions_block().startswith(b"<!-- eutonos:decisions:start sha256="))
        self.assertIn(b"## Decisions\n\n", cp.decisions_block())

    def test_the_repository_agents_file_carries_a_valid_decisions_block_after_the_completion_block(self):
        raw = (Path(__file__).resolve().parents[1] / "AGENTS.md").read_bytes()
        matches = list(cp.DECISIONS_PATTERN.finditer(raw))
        self.assertEqual(len(matches), 1)
        self.assertEqual(cp.sha(matches[0][2]), matches[0][1].decode())
        self.assertEqual(matches[0][0], cp.decisions_block())
        self.assertGreater(matches[0].start(), raw.index(cp.END))
        self.assertEqual(self.merge(raw), (raw, "unchanged"))
        self.assertEqual(cp.merge_block(raw), raw)  # the completion block beside it is intact too

    def test_case_a_and_b_add_then_refresh_in_place_and_customized_is_a_conflict(self):
        added, mode = self.merge(b"mine\n")
        self.assertEqual(mode, "added")
        self.assertEqual(self.merge(added), (added, "unchanged"))
        older = b"top\n" + cp.make_block(self.MARK, cp.block_body("Earlier text", cp.DECISIONS_HEADING)) + b"\nbottom\n"
        refreshed, mode = self.merge(older)
        self.assertEqual(mode, "replaced")
        self.assertTrue(refreshed.startswith(b"top\n") and refreshed.endswith(b"\nbottom\n"))
        with self.assertRaises(ValueError) as caught:
            self.merge(added.replace(b"Decide what", b"Choose what"))
        self.assertEqual(str(caught.exception), "customized-managed-block")

    def test_case_c_exact_unmarked_copy_is_wrapped_after_normalizing_endings_and_trailing_space(self):
        text = "mine\r\n\r\n## Decisions  \r\n\r\n" + cp.DECISIONS + " \r\n\r\n## Next\r\nkeep\r\n"
        raw = text.encode("utf-8")
        after, mode = self.merge(raw)
        self.assertEqual(mode, "wrap")
        self.assertEqual(after.count(cp.DECISIONS.encode()), 1)
        self.assertTrue(cp.DECISIONS_PATTERN.search(after))
        self.assertEqual(after.count(b"\n"), after.count(b"\r\n"))
        self.assertEqual(self.merge(after)[1], "replaced")  # now a marked block: later runs refresh it
        shape = cp.undo_shape(raw, after, mode, cp.DECISIONS_PATTERN)
        self.assertEqual(shape, {"mode": "wrap", "wrap_nl": "\r\n"})
        self.assertEqual(cp.restore_block(after, shape, cp.DECISIONS_PATTERN, cp.DECISIONS_START, cp.DECISIONS_END),
                         (raw, "removed"))

    def test_case_d_a_similar_copy_or_a_copy_with_more_text_writes_nothing(self):
        for raw in (b"## Decisions\n\nMy own rule.\n",
                    ("## Decisions\n\n" + cp.DECISIONS + "\n\nAnd one more paragraph of mine.\n").encode(),
                    ("## Decisions\n\n" + cp.DECISIONS + "\n").encode() * 2):
            with self.subTest(raw=raw[:40]), self.assertRaises(cp.SimilarUnmarked) as caught:
                self.merge(raw)
            self.assertEqual(str(caught.exception), "similar-unmarked-copy")
        # A heading inside a code fence is an example, not a copy.
        fenced = b"```\n## Decisions\n```\n"
        self.assertEqual(self.merge(fenced)[1], "added")

    def test_decisions_plan_wraps_exact_lists_similar_and_installs_the_rest(self):
        self.codex.joinpath("AGENTS.md").write_bytes(b"codex\n")
        self.claude.joinpath("CLAUDE.md").write_bytes(b"## Decisions\n\nMine.\n")
        plan = cp.decisions_plan(global_instructions=True, codex_home=self.codex, claude_home=self.claude)
        self.assertEqual([row["path"] for row in plan["changes"]], [str(self.codex / "AGENTS.md")])
        self.assertEqual([(row["path"], row["code"]) for row in plan["pending"]],
                         [(str(self.claude / "CLAUDE.md"), "similar-unmarked-copy")])
        self.assertEqual(plan["pending"][0]["shipped_sha256"], cp.sha(cp.decisions_block()))
        result = cp.install(plan, cp.decisions_plan)
        self.assertEqual(result["outcome"], "installed", result)
        self.assertEqual(result["pending"], plan["pending"])
        self.assertEqual((self.claude / "CLAUDE.md").read_bytes(), b"## Decisions\n\nMine.\n")
        self.assertIn(b"eutonos:decisions:start", (self.codex / "AGENTS.md").read_bytes())

    def test_restore_block_puts_back_exactly_what_was_there_for_every_shape(self):
        cases = {
            "added": b"first\nsecond\n",
            "created": b"",
            "replaced": b"x\n" + cp.make_block(self.MARK, cp.block_body("Earlier text", cp.DECISIONS_HEADING)) + b"\ny\r\n",
            "wrap": ("pre\n\n## Decisions\n\n" + cp.DECISIONS + "\n\n## Post\nz\n").encode(),
        }
        for name, before in cases.items():
            with self.subTest(case=name):
                after, mode = self.merge(before)
                shape = cp.undo_shape(before, after, mode, cp.DECISIONS_PATTERN)
                edited = b"top edit\n" + after + b"bottom edit\n"
                restored, state = cp.restore_block(edited, shape, cp.DECISIONS_PATTERN, cp.DECISIONS_START, cp.DECISIONS_END)
                self.assertEqual(state, "removed")
                self.assertEqual(restored, b"top edit\n" + before + b"bottom edit\n" if name != "created" else b"top edit\nbottom edit\n")
                if name != "replaced":
                    self.assertEqual(cp.restore_block(before, shape, cp.DECISIONS_PATTERN, cp.DECISIONS_START,
                                                      cp.DECISIONS_END)[1], "absent")
                # An edit inside the block is kept, never removed.
                inside = after.replace(b"Report in plain English", b"Report in rhyme")
                if name != "replaced":
                    self.assertEqual(cp.restore_block(inside, shape, cp.DECISIONS_PATTERN, cp.DECISIONS_START, cp.DECISIONS_END),
                                     (inside, "customized"))

    def test_restore_block_without_a_recorded_shape_takes_the_older_route(self):
        before = b"mine\n"
        after, _ = self.merge(before)
        old = cp.restore_block(after, {"op": "block-remove", "module": "decisions", "created": False},
                               cp.DECISIONS_PATTERN, cp.DECISIONS_START, cp.DECISIONS_END)
        self.assertEqual(old, cp.unmerge_block(after, cp.DECISIONS_PATTERN, cp.DECISIONS_START, cp.DECISIONS_END))
        self.assertEqual(old[0], before)

    def test_the_command_line_installs_the_decisions_block_with_the_contract(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cp.main(["install", "--global", "--codex-home", str(self.codex), "--claude-home", str(self.claude)])
        self.assertEqual(code, 0, out.getvalue())
        result = json.loads(out.getvalue())
        self.assertEqual(result["decisions"]["outcome"], "installed")
        for path in (self.codex / "AGENTS.md", self.claude / "CLAUDE.md"):
            raw = path.read_bytes()
            self.assertIn(b"eutonos:completion:start", raw)
            self.assertIn(b"eutonos:decisions:start", raw)

    def test_completion_contract_unmarked_copy_is_wrapped_and_a_similar_one_is_pending(self):
        exact = ("# mine\n\n## Completion contract\n\n" + cp.CONTRACT + "\n").encode()
        self.codex.joinpath("AGENTS.md").write_bytes(exact)
        self.claude.joinpath("CLAUDE.md").write_bytes(b"## Completion contract\n\nMerge only when I say so.\n")
        plan = cp.instruction_plan(global_instructions=True, codex_home=self.codex, claude_home=self.claude)
        self.assertEqual([row["mode"] for row in plan["changes"]], ["wrap"])
        self.assertEqual([row["code"] for row in plan["pending"]], ["similar-unmarked-copy"])
        self.assertEqual(plan["conflicts"], [])


if __name__ == "__main__":
    unittest.main()
