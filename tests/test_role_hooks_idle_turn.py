"""End-turn pass skips a turn that left nothing to check (owner, 2026-10-23).

src/eutonos/role_hooks.py: ``_handle_stop`` reads one byte-capped tail of the
transcript the Stop payload names and skips its pass only when that transcript
positively shows no file edit and no effect-making tool call since the last
real user prompt (Claude) or the turn's ``task_started`` row (Codex). Every
other outcome keeps the pass. Synthetic transcripts only.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from eutonos import role_hooks as rh
from eutonos import settings


# ---------------------------------------------------- synthetic Claude rows

def c_prompt(text="please look"):
    return {"type": "user", "message": {"role": "user", "content": text}}


def c_text(text="Here is the answer."):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def c_call(name, **fields):
    block = {"type": "tool_use", "id": "toolu_" + name, "name": name, "input": fields}
    return {"type": "assistant", "message": {"role": "assistant", "content": [block]}}


def c_result():
    return {"type": "user", "message": {"role": "user",
                                        "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}}


# ----------------------------------------------------- synthetic Codex rows

def x_started(turn="t1"):
    return {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}}


def x_item(item_type, **fields):
    return {"type": "response_item", "payload": {"type": item_type, **fields}}


def x_message(role="assistant"):
    return x_item("message", role=role, content=[{"type": "output_text", "text": "hi"}])


def x_exec():
    return x_item("custom_tool_call", name="exec", input="ls")


class IdleTurnSkipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eutonos-idle-turn-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.files = Path(self.temp.name) / "transcripts"
        self.files.mkdir()
        current = settings.read_hooks(self.root)["hooks"]
        self.assertTrue(current["trigger_enabled"]["stop_check"])  # default on
        self._n = 0

    # ------------------------------------------------------------- helpers

    def _transcript(self, rows, name=None, raw=None):
        self._n += 1
        path = self.files / (name or f"t{self._n}.jsonl")
        if raw is None:
            raw = "".join(json.dumps(row) + "\n" for row in rows).encode("utf-8")
        path.write_bytes(raw)
        return path

    def _stop(self, app="claude", event="Stop", session="s1", **fields):
        payload = {"hook_event_name": event, "session_id": session, "cwd": str(self.root), **fields}
        return rh._dispatch(self.root, payload, app=app)

    def _stop_rows(self, session="s1"):
        sid = rh._session_key(session)
        return [row for row in rh.read_recent_events(self.root, limit=50)
                if row["trigger"] == "stop_check" and row["session"] == sid]

    def assertSkipped(self, out, session="s1"):
        self.assertEqual(out, {})
        row = self._stop_rows(session)[0]
        self.assertFalse(row["fired"])
        self.assertEqual(row["reason"], rh.END_TURN_SKIP_REASON)
        self.assertEqual(row["reason"], "skipped: nothing outstanding")

    def assertPass(self, out, session="s1"):
        self.assertEqual(out.get("decision"), "block")
        row = self._stop_rows(session)[0]
        self.assertTrue(row["fired"])
        self.assertEqual(row["reason"], "ok")

    def _claude(self, rows, **fields):
        return self._stop(transcript_path=str(self._transcript(rows)), **fields)

    # ------------------------------------------------- Claude: skipped turns

    def test_plain_answer_turn_skips_the_pass(self):
        self.assertSkipped(self._claude([c_prompt(), c_text()]))

    def test_read_grep_glob_only_turn_skips_the_pass(self):
        rows = [c_prompt(), c_call("Read"), c_result(), c_call("Grep"), c_result(),
                c_call("Glob"), c_result(), c_text()]
        self.assertSkipped(self._claude(rows))

    def test_edits_before_the_last_prompt_do_not_count(self):
        rows = [c_prompt("first"), c_call("Edit"), c_result(), c_call("Bash"), c_result(), c_text("done"),
                c_prompt("what does that do?"), c_text("It does X.")]
        self.assertSkipped(self._claude(rows))

    def test_skip_is_not_a_spent_guard(self):
        # No guard is claimed on a skip: a later Stop of the same session whose
        # transcript shows work still gets its pass.
        self.assertSkipped(self._claude([c_prompt(), c_text()]))
        self.assertPass(self._claude([c_prompt("again"), c_call("Edit"), c_result(), c_text()]))

    def test_read_only_mcp_tools_skip_and_unknown_or_write_ones_do_not(self):
        for name, skipped in (("mcp__crosstalk-p0__list", True), ("mcp__crosstalk-p0__status", True),
                              ("mcp__Claude_Browser__read_page", True), ("mcp__ccd_pr__get_status", True),
                              ("ToolSearch", True), ("WebFetch", True), ("WebSearch", True),
                              ("mcp__crosstalk-p0__send", False), ("mcp__crosstalk-p0__create", False),
                              ("mcp__Claude_Browser__navigate", False), ("mcp__scheduled-tasks__delete_scheduled_task", False),
                              ("mcp__x__export", False), ("mcp__broken", False), ("SomeBrandNewTool", False)):
            with self.subTest(tool=name):
                session = "mcp-" + name
                out = self._claude([c_prompt(), c_call(name), c_result(), c_text()], session_id=session)
                if skipped:
                    self.assertSkipped(out, session)
                else:
                    self.assertPass(out, session)

    # ------------------------------------------------ Claude: kept (passes)

    def test_edit_turn_keeps_the_pass(self):
        for tool in ("Edit", "Write", "NotebookEdit"):
            with self.subTest(tool=tool):
                session = "edit-" + tool
                out = self._claude([c_prompt(), c_call("Read"), c_result(), c_call(tool), c_result(), c_text()],
                                   session_id=session)
                self.assertPass(out, session)

    def test_bash_and_powershell_turn_keeps_the_pass(self):
        for tool in ("Bash", "PowerShell"):
            with self.subTest(tool=tool):
                session = "shell-" + tool
                out = self._claude([c_prompt(), c_call(tool, command="git status"), c_result(), c_text()],
                                   session_id=session)
                self.assertPass(out, session)

    def test_agent_launch_turn_keeps_the_pass(self):
        for tool in ("Agent", "Task", "SendMessage"):
            with self.subTest(tool=tool):
                session = "agent-" + tool
                self.assertPass(self._claude([c_prompt(), c_call(tool), c_result(), c_text()], session_id=session),
                                session)

    def test_effect_in_a_server_tool_or_mixed_block_keeps_the_pass(self):
        mixed = {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "running"}, {"type": "tool_use", "name": "Read", "input": {}},
            {"type": "tool_use", "name": "Bash", "input": {}}]}}
        self.assertPass(self._claude([c_prompt(), mixed, c_result(), c_text()], session_id="mixed"), "mixed")
        mcp = {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "mcp_tool_use", "name": "mcp__x__write_thing", "input": {}}]}}
        self.assertPass(self._claude([c_prompt(), mcp, c_result(), c_text()], session_id="mcp-use"), "mcp-use")

    def test_tool_result_rows_are_not_a_prompt_boundary(self):
        # Edit, then a tool_result user row, then a read-only tail: the result
        # row must not be mistaken for the user's prompt.
        rows = [c_prompt(), c_call("Edit"), c_result(), c_call("Read"), c_result(), c_text()]
        self.assertPass(self._claude(rows))

    def test_meta_sidechain_and_bookkeeping_rows_are_ignored(self):
        meta = {"type": "user", "isMeta": True, "message": {"role": "user", "content": "Stop hook feedback: ..."}}
        side_edit = {**c_call("Edit"), "isSidechain": True}
        side_prompt = {**c_prompt("subagent task"), "isSidechain": True}
        noise = [{"type": "attachment", "attachment": {"type": "hook_success"}},
                 {"type": "api-request-blob", "message": {"content": [{"type": "text", "text": "Bash Edit"}]}},
                 {"type": "last-prompt", "lastPrompt": "x"}, {"type": "system", "subtype": "stop_hook_summary"}]
        # Skipped: only noise, meta and subagent rows follow the real prompt.
        self.assertSkipped(self._claude([c_prompt(), side_prompt, side_edit, c_text(), meta, *noise]))
        # Kept: the main thread's own Agent launch is visible even with sidechain rows around it.
        session = "side"
        rows = [c_prompt(), c_call("Agent"), side_prompt, c_call("Read"), c_result(), c_text(), *noise]
        self.assertPass(self._claude(rows, session_id=session), session)

    def test_compaction_inside_the_turn_keeps_the_pass(self):
        summary = {"type": "user", "isCompactSummary": True, "message": {"role": "user", "content": "This session..."}}
        boundary = {"type": "system", "subtype": "compact_boundary"}
        for name, rows in (("summary", [c_prompt(), summary, c_text()]), ("boundary", [c_prompt(), boundary, c_text()])):
            with self.subTest(name=name):
                self.assertPass(self._claude(rows, session_id="compact-" + name), "compact-" + name)

    def test_prompt_with_image_block_is_a_boundary(self):
        rows = [c_call("Bash"), c_result(),
                {"type": "user", "message": {"role": "user", "content": [
                    {"type": "image", "source": {}}, {"type": "text", "text": "what is this?"}]}}, c_text()]
        self.assertSkipped(self._claude(rows))

    # ------------------------------------- unreadable signals keep the pass

    def test_missing_transcript_keeps_the_pass(self):
        cases = {"no key": {}, "null": {"transcript_path": None}, "empty": {"transcript_path": ""},
                 "not a string": {"transcript_path": 7}, "absent file": {"transcript_path": str(self.files / "nope.jsonl")},
                 "directory": {"transcript_path": str(self.files)},
                 "too long": {"transcript_path": "x" * (rh.MAX_TRANSCRIPT_PATH_CHARS + 1) + ".jsonl"},
                 "nul": {"transcript_path": str(self.files / "a\0b.jsonl")}}
        for name, fields in cases.items():
            with self.subTest(case=name):
                self.assertPass(self._stop(session_id="missing-" + name, **fields), "missing-" + name)

    def test_wrong_file_type_keeps_the_pass(self):
        path = self._transcript([c_prompt(), c_text()], name="plain.txt")
        self.assertPass(self._stop(transcript_path=str(path)))

    def test_malformed_transcript_keeps_the_pass(self):
        good = (json.dumps(c_prompt()) + "\n" + json.dumps(c_text()) + "\n").encode("utf-8")
        cases = {"garbage": b"this is not json\n",
                 "bad last row": good + b'{"type": "assistant", "message": {"con',
                 "bad middle row": json.dumps(c_prompt()).encode() + b"\n{nope\n" + json.dumps(c_text()).encode() + b"\n",
                 "not an object": json.dumps(c_prompt()).encode() + b"\n[1, 2]\n",
                 "assistant without content": json.dumps(c_prompt()).encode() + b'\n{"type": "assistant", "message": {}}\n',
                 "empty prompt": b'{"type": "user", "message": {"content": "  "}}\n',
                 "unknown user content": b'{"type": "user", "message": {"content": [{"type": "mystery"}]}}\n',
                 "empty file": b"", "only blanks": b"\n\n\n",
                 "no prompt row": json.dumps(c_text()).encode() + b"\n"}
        for name, raw in cases.items():
            with self.subTest(case=name):
                path = self._transcript(None, raw=raw)
                self.assertPass(self._stop(session_id="bad-" + name, transcript_path=str(path)), "bad-" + name)

    def test_oversized_transcript_is_only_tailed(self):
        filler = {"type": "attachment", "attachment": {"type": "hook_success", "pad": "x" * 200}}
        with mock.patch.object(rh, "END_TURN_SKIP_TAIL_BYTES", 8192):
            # The prompt is far outside the tail: nothing proves the turn was idle.
            rows = [c_prompt(), *[filler] * 200, c_text()]
            path = self._transcript(rows)
            self.assertGreater(path.stat().st_size, 8192 * 2)
            self.assertPass(self._stop(session_id="big-lost", transcript_path=str(path)), "big-lost")
            # The prompt is inside the tail: skipped, though the file is huge.
            rows = [*[filler] * 200, c_prompt(), c_text()]
            path = self._transcript(rows)
            self.assertGreater(path.stat().st_size, 8192 * 2)
            self.assertSkipped(self._stop(session_id="big-seen", transcript_path=str(path)), "big-seen")
            # An effect inside the tail is still found.
            rows = [c_prompt(), *[filler] * 200, c_call("Bash"), c_result(), c_text()]
            path = self._transcript(rows)
            self.assertPass(self._stop(session_id="big-effect", transcript_path=str(path)), "big-effect")
            # One line longer than the whole tail leaves no readable row.
            giant = {"type": "assistant", "message": {"content": "y" * 20000}}
            path = self._transcript([c_prompt(), giant])
            self.assertPass(self._stop(session_id="big-line", transcript_path=str(path)), "big-line")

    def test_transcript_read_is_one_bounded_tail(self):
        path = self._transcript([c_prompt(), c_text()])
        seen = []
        real = rh._tail_read

        def spy(p, max_bytes):
            seen.append((Path(p), max_bytes))
            return real(p, max_bytes)

        with mock.patch.object(rh, "_tail_read", spy):
            out = self._stop(transcript_path=str(path))
        self.assertSkipped(out)
        self.assertEqual(seen, [(path, rh.END_TURN_SKIP_TAIL_BYTES)])
        self.assertLessEqual(rh.END_TURN_SKIP_TAIL_BYTES, 4 * 1024 * 1024)

    def test_unexpected_reader_error_keeps_the_pass(self):
        path = self._transcript([c_prompt(), c_text()])
        with mock.patch.object(rh, "_tail_read", side_effect=OSError("locked")):
            out = self._stop(transcript_path=str(path))
        self.assertPass(out)  # the log reader shares _tail_read, so assert after the patch ends
        with mock.patch.object(rh, "_claude_turn_state", side_effect=RecursionError()):
            out = self._stop(session_id="recursion", transcript_path=str(path))
        self.assertPass(out, "recursion")

    def test_line_separator_inside_json_text_does_not_split_a_row(self):
        text = "line one line two end"
        rows = [c_prompt(), c_call("Bash", note=text), c_result(), c_text(text)]
        raw = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
        self.assertPass(self._stop(transcript_path=str(self._transcript(None, raw=raw))))

    # ------------------------------------------------- unchanged behaviors

    def test_unchanged_loop_guard_switches_and_no_transcript_default(self):
        path = self._transcript([c_prompt(), c_call("Edit"), c_result(), c_text()])
        # Without a transcript the pass fires exactly as before.
        self.assertPass(self._stop(session_id="plain"), "plain")
        # stop_hook_active still never blocks.
        self.assertEqual(self._stop(session_id="loop", transcript_path=str(path), stop_hook_active=True), {})
        self.assertEqual(self._stop_rows("loop")[0]["reason"], "already-continued")
        # The module switch turns the skip off: a plain answer then gets its pass.
        idle = self._transcript([c_prompt(), c_text()])
        with mock.patch.object(rh, "END_TURN_SKIP_WHEN_IDLE", False):
            self.assertPass(self._stop(session_id="off", transcript_path=str(idle)), "off")
        # No new blocking: a skip is the empty object.
        self.assertEqual(json.dumps(self._stop(session_id="empty", transcript_path=str(idle))), "{}")

    def test_skip_row_is_metadata_only(self):
        self._stop(transcript_path=str(self._transcript([c_prompt("secret prompt text"), c_text("secret answer")])))
        log = rh._events_log_path(self.root).read_text(encoding="utf-8")
        self.assertIn(rh.END_TURN_SKIP_REASON, log)
        self.assertNotIn("secret", log)
        self.assertNotIn(".jsonl", log)

    # ------------------------------------------------------------- Codex

    def _codex(self, rows, event="Stop", **fields):
        key = "agent_transcript_path" if event == "SubagentStop" else "transcript_path"
        return self._stop(app="codex", event=event, **{key: str(self._transcript(rows))}, **fields)

    def test_codex_plain_turn_skips_the_pass(self):
        rows = [x_started(), x_message("user"), x_item("reasoning"), x_message("assistant")]
        self.assertSkipped(self._codex(rows, turn_id="t1"))
        # The reminder file of a compact cue is never written on a skip.
        self.assertFalse(rh._end_turn_reminder_path(self.root, "s1").exists())

    def test_codex_exec_and_unknown_items_keep_the_pass(self):
        cases = {"exec": x_exec(), "send": x_item("function_call", name="send_message"),
                 "shell": x_item("local_shell_call"), "patch": x_item("custom_tool_call", name="apply_patch"),
                 "unknown": x_item("brand_new_action"), "agent msg": x_item("agent_message"),
                 "mcp write": x_item("function_call", name="mcp__crosstalk-p0__send"),
                 "no name": x_item("function_call")}
        for name, row in cases.items():
            with self.subTest(case=name):
                session = "cx-" + name
                rows = [x_started(), x_message("user"), row, x_item("function_call_output"), x_message()]
                self.assertPass(self._codex(rows, session_id=session, turn_id="t1"), session)

    def test_codex_read_only_calls_skip(self):
        rows = [x_started(), x_item("function_call", name="view_image"), x_item("function_call_output"),
                x_item("web_search_call"), x_item("function_call", name="mcp__crosstalk-p0__list"),
                x_item("function_call_output"), x_message()]
        self.assertSkipped(self._codex(rows, turn_id="t1"))

    def test_codex_only_this_turn_counts(self):
        rows = [x_started("t0"), x_exec(), x_item("custom_tool_call_output"), x_message(),
                {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t0"}},
                x_started("t1"), x_message("user"), x_message()]
        self.assertSkipped(self._codex(rows, turn_id="t1"))
        self.assertPass(self._codex(rows[:4], session_id="cx-prev", turn_id="t0"), "cx-prev")

    def test_codex_unrecognized_shapes_keep_the_pass(self):
        compacted = {"type": "compacted", "payload": {}}
        cases = {"turn mismatch": ([x_started("t9"), x_message()], "t1"),
                 "no turn start": ([x_message(), x_message()], "t1"),
                 "compacted": ([x_started(), compacted, x_message()], "t1"),
                 "bad payload": ([x_started(), {"type": "response_item", "payload": "oops"}, x_message()], "t1"),
                 "untyped item": ([x_started(), {"type": "response_item", "payload": {}}, x_message()], "t1")}
        for name, (rows, turn) in cases.items():
            with self.subTest(case=name):
                session = "cx-bad-" + name
                self.assertPass(self._codex(rows, session_id=session, turn_id=turn), session)
        # Malformed or missing transcripts, as for Claude.
        bad = self._transcript(None, raw=b"{broken\n")
        self.assertPass(self._stop(app="codex", session_id="cx-garbage", transcript_path=str(bad)), "cx-garbage")
        self.assertPass(self._stop(app="codex", session_id="cx-none"), "cx-none")
        self.assertPass(self._stop(app="codex", session_id="cx-absent",
                                   transcript_path=str(self.files / "gone.jsonl")), "cx-absent")

    def test_codex_without_a_stop_turn_id_still_reads_the_turn(self):
        self.assertSkipped(self._codex([x_started("t5"), x_message()]))

    def test_codex_child_stop_uses_its_own_rollout_and_releases_its_marker(self):
        child = "child-1"
        rh._dispatch(self.root, {"hook_event_name": "SubagentStart", "session_id": "s1", "cwd": str(self.root),
                                 "agent_id": child}, app="codex")
        self.assertTrue(rh._child_marker_path(self.root, "s1", child).exists())
        idle_child = self._transcript([x_started("c1"), x_message()])
        busy_parent = self._transcript([x_started("p1"), x_exec(), x_message()])
        out = self._stop(app="codex", event="SubagentStop", agent_id=child, turn_id="c1",
                         agent_transcript_path=str(idle_child), transcript_path=str(busy_parent))
        self.assertEqual(out, {})
        rows = [r for r in rh.read_recent_events(self.root, limit=20) if r["trigger"] == "stop_check"]
        self.assertEqual(rows[0]["reason"], rh.END_TURN_SKIP_REASON)
        self.assertFalse(rh._child_marker_path(self.root, "s1", child).exists())
        # Only the parent's rollout (no agent_transcript_path) is never read for a child.
        child2 = "child-2"
        out = self._stop(app="codex", event="SubagentStop", agent_id=child2, transcript_path=str(idle_child))
        self.assertEqual(out.get("decision"), "block")

    # -------------------------------------------------- classifier units

    def test_tool_effect_classification(self):
        for name in ("Read", "Grep", "Glob", "ToolSearch", "WebFetch", "WebSearch", "mcp__a__get_thing",
                     "mcp__a-b__list", "mcp__a__Query_rows"):
            with self.subTest(read_only=name):
                self.assertFalse(rh._tool_is_effectful(name))
        for name in ("Edit", "Write", "NotebookEdit", "Bash", "PowerShell", "Agent", "Task", "apply_patch", "exec",
                     "mcp__a__send", "mcp__a__getaway_car", "mcp__list", "mcp____", "", None, 5, "mcp__a__"):
            with self.subTest(effectful=name):
                self.assertTrue(rh._tool_is_effectful(name))

    def test_read_only_tool_lists_hold_no_write_tool(self):
        self.assertFalse(rh.READ_ONLY_TOOLS & {"Edit", "Write", "NotebookEdit", "Bash", "PowerShell", "Agent",
                                               "Task", "apply_patch", "exec", "SendMessage"})
        self.assertFalse(rh.READ_ONLY_MCP_VERBS & {"send", "create", "write", "set", "update", "delete", "run",
                                                   "navigate", "click", "export", "reply"})


if __name__ == "__main__":
    unittest.main()
