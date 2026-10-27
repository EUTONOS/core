"""AWR role hooks (owner, 2026-09-28): src/eutonos/role_hooks.py.

Exercises role resolution, every trigger's output shape, disabled switches
(global/role/trigger), the edit_nudge rate limit, the stop_check no-loop
guard, long_turn counting/reset/threshold, hook-set CRUD via settings.py,
the retired role.reminder migration, the cross-app safe_reminder_text
helper role_router.py uses, and the firing
log's bounds/no-text guarantee.
"""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from eutonos import role_hooks as rh
from eutonos import role_router
from eutonos import settings


class RoleHooksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eutonos-role-hooks-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        # These tests retain their original reminder/marker purpose. The
        # default-on end-turn pass is enabled explicitly by its own tests.
        current = settings.read_hooks(self.root)
        settings.update_hooks(self.root, {**current["hooks"], "closeout_check": False,
                                          "trigger_enabled": {**current["hooks"]["trigger_enabled"],
                                                              "stop_check": False}},
                              expected_revision=current["revision"])
        # The pending-actions line reads the EUTONOS home; keep these tests off the real one.
        self._env_patch = mock.patch.dict("os.environ", {"EUTONOS_HOME": str(Path(self.temp.name) / "eutonos-home")},
                                          clear=False)
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        for key in ("EUTONOS_ROLE",):
            __import__("os").environ.pop(key, None)

    # ------------------------------------------------------------- helpers

    def _hooks(self, **overrides):
        current = settings.read_hooks(self.root)
        value = current["hooks"]
        value = {**value, **overrides}
        return settings.update_hooks(self.root, value, expected_revision=current["revision"])["hooks"]

    def _payload(self, event, **fields):
        return {"hook_event_name": event, "session_id": "s1", "cwd": str(self.root), **fields}

    # ------------------------------------------------------------ resolution

    def test_role_resolution_default_env_and_subagent(self):
        role, is_sub = rh._resolve_role({})
        self.assertEqual((role, is_sub), ("orchestrator", False))
        import os
        os.environ["EUTONOS_ROLE"] = "director"
        role, is_sub = rh._resolve_role({})
        self.assertEqual((role, is_sub), ("director", False))
        # A subagent's agent_id wins over the parent's inherited env marker.
        role, is_sub = rh._resolve_role({"agent_id": "a1", "agent_type": "general-purpose"})
        self.assertEqual((role, is_sub), (rh.DEFAULT_CHILD_ROLE, True))
        role, is_sub = rh._resolve_role({"agent_id": "a2", "agent_type": "Explore"})
        self.assertEqual((role, is_sub), ("discovery", True))

    # --------------------------------------------------------- output shapes

    def test_session_start_output_shape_and_resets_counter(self):
        state_path = rh._state_path(self.root, "s1")
        rh._save_state(state_path, {"tool_count": 7})
        out = rh._dispatch(self.root, self._payload("SessionStart", source="startup"), app="claude")
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIsInstance(out["hookSpecificOutput"]["additionalContext"], str)
        self.assertTrue(out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(rh._load_state(state_path)["tool_count"], 0)

    def test_subagent_start_is_treated_like_session_start(self):
        out = rh._dispatch(self.root, self._payload("SubagentStart", agent_id="a1", agent_type="Explore"), app="claude")
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SubagentStart")
        expected = settings.BUILTIN_HOOK_SETS[settings.BUILTIN_HOOK_SET_NAME]["roles"]["discovery"]["session_start"]
        self.assertTrue(out["hookSpecificOutput"]["additionalContext"].startswith(expected + "\n"))
        self.assertIn("only under existing authorization", out["hookSpecificOutput"]["additionalContext"])

    def _enable_end_turn(self, **overrides):
        current = settings.read_hooks(self.root)["hooks"]
        return self._hooks(trigger_enabled={**current["trigger_enabled"], "stop_check": True}, **overrides)

    def test_stop_check_text_is_not_injected_upfront(self):
        self._enable_end_turn(closeout_check=True)
        reminder = rh.safe_reminder_text(self.root, "orchestrator", "stop_check")
        for event in ("UserPromptSubmit", "SessionStart"):
            with self.subTest(event=event):
                out = rh._dispatch(self.root, self._payload(event), app="codex")
                context = out["hookSpecificOutput"]["additionalContext"]
                self.assertNotIn(reminder, context)
                self.assertNotIn(rh.END_TURN_RECORDING_POLICY, context)
                self.assertNotIn("Closeout is optional", context)

    def test_compact_cue_title_carries_whole_first_sentence_or_none(self):
        self.assertEqual(rh._end_turn_key('Check the "work".  Then more.'), "Check the 'work'.")
        self.assertEqual(rh._end_turn_key("One line only"), "One line only")
        self.assertEqual(rh._end_turn_key("x" * (rh.END_TURN_KEY_CHARS + 1) + ". Rest."), "")
        cue = rh._compact_end_turn_reason(Path("r.txt"), "n0", "")
        self.assertIn('"Read this checklist and apply it once.', cue)

    def test_end_turn_pass_blocks_exactly_once_per_turn(self):
        self._enable_end_turn(closeout_check=True)
        reminder = rh.safe_reminder_text(self.root, "orchestrator", "stop_check")
        for app in ("codex", "claude"):
            with self.subTest(app=app), mock.patch("eutonos.closeout_debt.check") as check:
                session = "turns-" + app
                rh._dispatch(self.root, self._payload("UserPromptSubmit", session_id=session, prompt="do it"), app=app)
                first = rh._dispatch(self.root, self._payload("Stop", session_id=session, stop_hook_active=False), app=app)
                self.assertEqual(first["decision"], "block")
                full_parts = (reminder, rh.END_TURN_RECORDING_POLICY, rh.END_TURN_AUTHORITY, rh.END_TURN_HEADER)
                if app == "codex":
                    self.assertEqual(len(first["reason"].splitlines()), 1)
                    reminder_path = rh._end_turn_reminder_path(self.root, session)
                    nonce = rh._end_turn_nonce(self.root, session)
                    key = rh._end_turn_key(reminder)
                    self.assertTrue(key)
                    self.assertEqual(first["reason"], (
                        f'[{rh.END_TURN_COMPACT_CUE}](<{reminder_path.as_posix()}> '
                        f'"{key} Read this checklist and apply it once. {rh.END_TURN_TAG.format(nonce=nonce)}")'))
                    saved_reminder = reminder_path.read_text(encoding="utf-8")
                    for part in full_parts:
                        self.assertNotIn(part, first["reason"])
                        self.assertIn(part, saved_reminder)
                else:
                    for part in full_parts:
                        self.assertIn(part, first["reason"])
                    self.assertFalse(rh._end_turn_reminder_path(self.root, session).exists())
                # The continuation's own stop ends normally, flagged or not.
                for active in (True, False):
                    self.assertEqual(rh._dispatch(self.root, self._payload("Stop", session_id=session,
                                                  stop_hook_active=active), app=app), {})
                # A genuine next prompt opens the next turn's single pass.
                rh._dispatch(self.root, self._payload("UserPromptSubmit", session_id=session, prompt="next"), app=app)
                self.assertEqual(rh._dispatch(self.root, self._payload("Stop", session_id=session), app=app)["decision"],
                                 "block")
                check.assert_not_called()

    def test_codex_continuation_prompt_keeps_the_spent_guard(self):
        self._enable_end_turn()
        first = rh._dispatch(self.root, self._payload("Stop", turn_id="t1"), app="codex")
        # Simulate a synthetic prompt for compatibility if Codex delivers one.
        reminder_path = rh._end_turn_reminder_path(self.root, "s1")
        self.assertTrue(reminder_path.is_file())
        cont = rh._dispatch(self.root, self._payload("UserPromptSubmit", turn_id="t1", prompt=first["reason"]), app="codex")
        self.assertEqual(cont, {})
        self.assertTrue(reminder_path.is_file())
        self.assertEqual(rh._dispatch(self.root, self._payload("Stop", turn_id="t1"), app="codex"), {})
        # Copied or stale tag text with another nonce is a genuine new prompt.
        stale = rh.END_TURN_TAG.format(nonce="0" * 12) + " old"
        self.assertTrue(rh._dispatch(self.root, self._payload("UserPromptSubmit", prompt=stale), app="codex"))
        self.assertFalse(reminder_path.exists())
        self.assertEqual(rh._dispatch(self.root, self._payload("Stop"), app="codex")["decision"], "block")

    def test_quoted_current_end_turn_cue_starts_a_new_turn(self):
        self._enable_end_turn()
        for app in ("codex", "claude"):
            with self.subTest(app=app):
                session = "quoted-" + app
                first = rh._dispatch(self.root, self._payload("Stop", session_id=session), app=app)
                question = f"Why did this appear? {first['reason']} Please explain."
                submitted = rh._dispatch(self.root, self._payload(
                    "UserPromptSubmit", session_id=session, prompt=question), app=app)
                self.assertIn("additionalContext", submitted["hookSpecificOutput"])
                self.assertEqual(rh._dispatch(self.root, self._payload(
                    "Stop", session_id=session), app=app)["decision"], "block")

    def test_stop_and_subagent_stop_for_one_child_block_once(self):
        import threading
        self._enable_end_turn(closeout_check=True)
        child = "child-codex"
        rh._dispatch(self.root, self._payload("SubagentStart", agent_id=child), app="codex")
        barrier = threading.Barrier(2)
        results = []

        def stop(event):
            barrier.wait(timeout=5)
            results.append(rh._dispatch(self.root, self._payload(event, agent_id=child), app="codex"))

        threads = [threading.Thread(target=stop, args=(event,)) for event in ("Stop", "SubagentStop")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        blocks = [out for out in results if out.get("decision") == "block"]
        self.assertEqual(len(blocks), 1)
        self.assertNotIn(rh.END_TURN_RECORDING_POLICY, blocks[0]["reason"])
        self.assertNotIn(rh.END_TURN_AUTHORITY, blocks[0]["reason"])
        child_reminder = rh._end_turn_reminder_path(self.root, "s1", child).read_text(encoding="utf-8")
        self.assertIn(rh.END_TURN_AUTHORITY, child_reminder)
        self.assertNotIn(rh.END_TURN_RECORDING_POLICY, child_reminder)
        # The continued child's final stop releases its marker.
        self.assertEqual(rh._dispatch(self.root, self._payload("SubagentStop", agent_id=child,
                                      stop_hook_active=True), app="codex"), {})
        self.assertFalse(rh._child_marker_path(self.root, "s1", child).exists())

    def test_codex_reminder_write_failure_falls_back_to_full_reason(self):
        self._enable_end_turn(closeout_check=True)
        with mock.patch.object(rh, "_write_end_turn_reminder", return_value=None):
            out = rh._dispatch(self.root, self._payload("Stop"), app="codex")
        self.assertEqual(out["decision"], "block")
        self.assertIn(rh.END_TURN_HEADER, out["reason"])
        self.assertIn(rh.END_TURN_RECORDING_POLICY, out["reason"])
        self.assertIn(rh.END_TURN_AUTHORITY, out["reason"])
        self.assertFalse(rh._end_turn_reminder_path(self.root, "s1").exists())
        self.assertEqual(rh._dispatch(self.root, self._payload("Stop"), app="codex"), {})

    def test_claude_subagent_stop_never_blocks_and_releases_marker(self):
        # A Claude subagent has already handed back its report; a block could
        # not continue it and would strand its marker.
        self._enable_end_turn(closeout_check=True)
        rh._dispatch(self.root, self._payload("SubagentStart", agent_id="child-claude"), app="claude")
        self.assertTrue(rh._child_marker_path(self.root, "s1", "child-claude").exists())
        self.assertEqual(rh._dispatch(self.root, self._payload("SubagentStop", agent_id="child-claude"), app="claude"), {})
        self.assertFalse(rh._child_marker_path(self.root, "s1", "child-claude").exists())

    def test_end_turn_pass_respects_switches_and_fails_open(self):
        self._enable_end_turn(closeout_check=False)
        out = rh._dispatch(self.root, self._payload("Stop", session_id="no-policy"), app="claude")
        self.assertNotIn(rh.END_TURN_RECORDING_POLICY, out["reason"])
        self.assertEqual(rh._dispatch(self.root, {"hook_event_name": "Stop"}, app="claude"), {})
        with mock.patch.object(rh, "_claim_end_turn", return_value="guard-unavailable"):
            self.assertEqual(rh._dispatch(self.root, self._payload("Stop", session_id="io"), app="codex"), {})
        self._hooks(role_enabled={"orchestrator": False})
        self.assertEqual(rh._dispatch(self.root, self._payload("Stop", session_id="role"), app="claude"), {})
        self._hooks(role_enabled={}, enabled=False)
        self.assertEqual(rh._dispatch(self.root, self._payload("Stop", session_id="off"), app="claude"), {})

    def test_crosstalk_letter_turn_gets_no_end_turn_pass(self):
        self._enable_end_turn(closeout_check=True)
        for session, prompt, blocks in (
                ("letter-a", "Crosstalk message from Codex (x) hello", False),
                ("letter-b", "<cross-session-message from=y>hi", False),
                ("owner-c", "please do it", True)):
            with self.subTest(session=session):
                rh._dispatch(self.root, self._payload("UserPromptSubmit", session_id=session, prompt=prompt),
                             app="claude")
                out = rh._dispatch(self.root, self._payload("Stop", session_id=session, stop_hook_active=False),
                                   app="claude")
                self.assertEqual(out.get("decision") == "block", blocks)

    def test_user_prompt_submit_output_shape(self):
        out = rh._dispatch(self.root, self._payload("UserPromptSubmit", prompt="hi"), app="claude")
        self.assertEqual(out, {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                       "additionalContext": out["hookSpecificOutput"]["additionalContext"]}})
        self.assertTrue(out["hookSpecificOutput"]["additionalContext"])

    def test_pre_tool_use_edit_nudge_shape_main_thread_only(self):
        out = rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Edit", tool_input={}), app="claude")
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertIn("editing", out["hookSpecificOutput"]["additionalContext"].lower())
        # A subagent's own edit never gets the orchestrator/role edit nudge.
        out_sub = rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Edit", agent_id="a1"), app="claude")
        self.assertEqual(out_sub, {})

    def test_pre_tool_use_non_edit_tool_produces_no_nudge(self):
        out = rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Read"), app="claude")
        self.assertEqual(out, {})

    def test_end_turn_pass_uses_role_text(self):
        import os
        os.environ["EUTONOS_ROLE"] = "discovery"
        self._enable_end_turn()
        out = rh._dispatch(self.root, self._payload("Stop", last_assistant_message="done"), app="claude")
        self.assertIn("Before stopping: did you", out["reason"])

    def test_stop_check_defaults_on(self):
        self.assertTrue(settings.DEFAULT_HOOKS["trigger_enabled"]["stop_check"])
        fresh = Path(self.temp.name) / "fresh"
        fresh.mkdir()
        self.assertTrue(settings.read_hooks(fresh)["hooks"]["trigger_enabled"]["stop_check"])

    def test_custom_stop_text_keeps_completion_contract_and_single_pass(self):
        from eutonos.completion_policy import CONTRACT
        self._enable_end_turn()
        custom = "Retain this owner's reminder verbatim."
        for app in ("claude", "codex"):
            payload = self._payload("Stop", session_id="custom-completion-" + app)
            with mock.patch.object(rh, "_text_for", return_value=(custom, "ok")):
                output = rh._dispatch(self.root, payload, app=app)
                text = (rh._end_turn_reminder_path(self.root, payload["session_id"]).read_text(encoding="utf-8")
                        if app == "codex" else output["reason"])
                self.assertIn(custom, text)
                self.assertEqual(text.count(CONTRACT), 1)
                self.assertEqual(rh._dispatch(self.root, payload, app=app), {})

    # ------------------------------------------------------------ disabling

    def test_global_disable_no_ops_every_trigger(self):
        self._hooks(enabled=False)
        for event in ("SessionStart", "UserPromptSubmit"):
            self.assertEqual(rh._dispatch(self.root, self._payload(event), app="claude"), {})
        self.assertEqual(rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Edit"), app="claude"), {})

    def test_role_disable_suppresses_only_that_role(self):
        self._hooks(role_enabled={"orchestrator": False})
        out = rh._dispatch(self.root, self._payload("UserPromptSubmit"), app="claude")
        self.assertEqual(out, {})
        import os
        os.environ["EUTONOS_ROLE"] = "director"
        out = rh._dispatch(self.root, self._payload("UserPromptSubmit"), app="claude")
        self.assertTrue(out)

    def test_trigger_disable_suppresses_that_trigger_globally(self):
        current = settings.read_hooks(self.root)["hooks"]
        self._hooks(trigger_enabled={**current["trigger_enabled"], "message": False})
        out = rh._dispatch(self.root, self._payload("UserPromptSubmit"), app="claude")
        self.assertEqual(out, {})
        # session_start is unaffected.
        out = rh._dispatch(self.root, self._payload("SessionStart"), app="claude")
        self.assertTrue(out)

    # -------------------------------------------------------- edit_nudge rate

    def test_edit_nudge_rate_limited_then_fires_again_after_window(self):
        self._hooks(edit_nudge_rate_limit_minutes=5)
        first = rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Edit"), app="claude")
        self.assertTrue(first)
        second = rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Edit"), app="claude")
        self.assertEqual(second, {})
        state_path = rh._state_path(self.root, "s1")
        state = rh._load_state(state_path)
        state["edit_nudge_last"] = time.time() - 6 * 60
        rh._save_state(state_path, state)
        third = rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Edit"), app="claude")
        self.assertTrue(third)

    # ------------------------------------------------------- Codex child identity

    def test_codex_ordinary_event_is_ambiguous_while_a_child_is_active(self):
        """Finding 1 (Codex verification, 2026-09-28): a Codex SubagentStart
        marks its agent_id active under the parent session; a later ordinary
        Codex event with no agent_id on that same session is then ambiguous
        -- edit_nudge and long_turn are both suppressed (logged with reason
        "ambiguous-child-active") and the long-turn counter is not advanced."""
        self._hooks(long_turn_threshold=1)
        start = rh._dispatch(self.root, self._payload("SubagentStart", agent_id="c1", agent_type="general-purpose"),
                             app="codex")
        self.assertTrue(start)  # the child's own session_start reminder still fires
        out = rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Edit"), app="codex")
        self.assertEqual(out, {})
        state = rh._load_state(rh._state_path(self.root, "s1"))
        self.assertEqual(state.get("tool_count"), None)  # never touched -- ambiguous call returned early
        events = rh.read_recent_events(self.root, limit=10)
        reasons = {(row["trigger"], row["reason"]) for row in events}
        self.assertIn(("edit_nudge", "ambiguous-child-active"), reasons)
        self.assertIn(("long_turn", "ambiguous-child-active"), reasons)
        for row in events:
            if row["trigger"] in ("edit_nudge", "long_turn"):
                self.assertFalse(row["fired"])

    def test_codex_ordinary_event_resolves_normally_with_no_active_child(self):
        """With no active child, a Codex ordinary event is never made
        ambiguous/neutral -- it resolves exactly as the main orchestrating
        chat, same as before this slice."""
        out = rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Edit"), app="codex")
        self.assertIn("editing", out["hookSpecificOutput"]["additionalContext"].lower())

    def test_claude_ordinary_event_is_never_ambiguous_even_with_active_child(self):
        """Claude keeps plain agent_id detection: its docs list agent_id as
        present on ordinary events too, so the ambiguity check never applies,
        even if a child happens to be tracked active for that session."""
        rh._dispatch(self.root, self._payload("SubagentStart", agent_id="c1", agent_type="general-purpose"),
                     app="claude")
        out = rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Edit"), app="claude")
        self.assertIn("editing", out["hookSpecificOutput"]["additionalContext"].lower())

    def test_subagent_stop_releases_active_child_and_ends_ambiguity(self):
        rh._dispatch(self.root, self._payload("SubagentStart", agent_id="c1", agent_type="general-purpose"),
                     app="codex")
        self.assertEqual(rh._active_children_status(self.root, "s1")[0], True)
        out = rh._dispatch(self.root, self._payload("SubagentStop", agent_id="c1"), app="codex")
        self.assertEqual(out, {})
        self.assertEqual(rh._active_children_status(self.root, "s1"), (False, ""))
        # Ordinary events resolve normally again once the only child has stopped.
        edit_out = rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Edit"), app="codex")
        self.assertIn("editing", edit_out["hookSpecificOutput"]["additionalContext"].lower())

    def test_subagent_stop_logs_marker_result_for_both_apps(self):
        for app in ("codex", "claude"):
            with self.subTest(app=app):
                rh._dispatch(self.root, self._payload("SubagentStart", agent_id=app), app=app)
                self.assertEqual(rh._dispatch(self.root, self._payload("SubagentStop", agent_id=app), app=app), {})
                self.assertEqual(rh._dispatch(self.root, self._payload("SubagentStop", agent_id=app), app=app), {})
                rows = [row for row in rh.read_recent_events(self.root, limit=10)
                        if row["app"] == app and row["trigger"] == "subagent_stop"]
                self.assertEqual([row["reason"] for row in rows[:2]], ["no-marker", "marker-released"])
                self.assertTrue(all(row["has_agent_id"] and not row["fired"] for row in rows[:2]))

    def test_subagent_stop_without_identity_logs_no_marker(self):
        self.assertEqual(rh._dispatch(self.root, self._payload("SubagentStop"), app="codex"), {})
        row = rh.read_recent_events(self.root, limit=1)[0]
        self.assertEqual((row["trigger"], row["reason"], row["has_agent_id"]),
                         ("subagent_stop", "no-marker", False))
        self.assertIsNone(row["role"])

    def test_active_child_marker_never_expires_on_age_alone(self):
        """Finding 2, corrected (coordinator recheck, 2026-09-28): age must
        NOT turn a child marker into "no children" -- only its own
        SubagentStop does. A marker far older than the old 30-minute expiry,
        and even older than CHILD_STALE_SECONDS (4 hours), still counts as
        active; past the 4-hour mark the logged reason downgrades to
        "ambiguous-stale" (observability only), but the suppression itself
        is identical either way."""
        rh._touch_active_child(self.root, "s1", "c1")
        marker = rh._child_marker_path(self.root, "s1", "c1")
        very_old = time.time() - rh.CHILD_STALE_SECONDS - 3600
        import os
        os.utime(marker, (very_old, very_old))
        has_active, reason = rh._active_children_status(self.root, "s1")
        self.assertTrue(has_active)  # still active -- age alone never clears it
        self.assertEqual(reason, "ambiguous-stale")
        out = rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Edit"), app="codex")
        self.assertEqual(out, {})  # still suppressed, same as a fresh marker
        events = rh.read_recent_events(self.root, limit=5)
        reasons = {row["reason"] for row in events if row["trigger"] == "edit_nudge"}
        self.assertIn("ambiguous-stale", reasons)

    # -------------------------------------- Codex evidence gate (2026-10-01)

    def _proof(self):
        return rh._agent_id_proof_path(self.root)

    def test_no_proof_live_marker_still_suppresses_orchestrator(self):
        """Without proof that Codex sends agent_id on ordinary events, the
        conservative suppression is unchanged (a child's own PreToolUse that
        lacked agent_id could be mislabeled as the parent's)."""
        self._hooks(long_turn_threshold=1)
        rh._dispatch(self.root, self._payload("SubagentStart", agent_id="c1"), app="codex")
        self.assertFalse(rh._codex_agent_id_proven(self.root))
        self.assertEqual(rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Edit"), app="codex"), {})
        self.assertFalse(self._proof().exists())

    def test_proof_present_orchestrator_reminders_fire_despite_live_marker(self):
        """A leaked marker (child never sent SubagentStop) must not silence the
        orchestrator once Codex was seen sending agent_id on PreToolUse."""
        self._hooks(long_turn_threshold=2)
        rh._dispatch(self.root, self._payload("SubagentStart", agent_id="lost-child"), app="codex")
        rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Read", agent_id="other-child"), app="codex")
        self.assertTrue(rh._codex_agent_id_proven(self.root))
        self.assertTrue(rh._active_children_status(self.root, "s1")[0])  # marker still there
        edit = rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Edit"), app="codex")
        self.assertIn("editing", edit["hookSpecificOutput"]["additionalContext"].lower())
        rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Read"), app="codex")  # 2nd call hits threshold
        rows = [row for row in rh.read_recent_events(self.root, limit=20)
                if row["app"] == "codex" and not row["has_agent_id"]]
        self.assertTrue(any(row["trigger"] == "long_turn" and row["fired"] for row in rows))
        self.assertTrue(any(row["trigger"] == "edit_nudge" and row["fired"] for row in rows))
        self.assertFalse(any(str(row["reason"]).startswith("ambiguous") for row in rows))

    def test_codex_pre_tool_use_with_agent_id_creates_proof_only_for_codex(self):
        self.assertFalse(self._proof().exists())
        rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Read", agent_id="a1"), app="claude")
        self.assertFalse(self._proof().exists())  # Claude behavior unchanged
        rh._dispatch(self.root, self._payload("SubagentStart", agent_id="a1"), app="codex")
        self.assertFalse(self._proof().exists())  # only an ordinary event counts
        rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Read", agent_id="a1"), app="codex")
        value = json.loads(self._proof().read_text(encoding="utf-8"))
        self.assertTrue(value["first_seen"])
        self.assertNotIn("a1", self._proof().read_text(encoding="utf-8"))
        first = value["first_seen"]
        rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Read", agent_id="a2"), app="codex")
        self.assertEqual(json.loads(self._proof().read_text(encoding="utf-8"))["first_seen"], first)
        self.assertEqual([p.name for p in self._proof().parent.iterdir() if ".tmp-" in p.name], [])

    def test_corrupt_or_oversized_proof_counts_as_no_proof(self):
        self._hooks(long_turn_threshold=1)
        rh._dispatch(self.root, self._payload("SubagentStart", agent_id="c1"), app="codex")
        proof = self._proof()
        proof.parent.mkdir(parents=True, exist_ok=True)
        for bad in (b"not json", b"[]", b'{"first_seen": ""}', b'{"first_seen": "' + b"x" * rh.MAX_PROOF_BYTES + b'"}'):
            with self.subTest(size=len(bad)):
                proof.write_bytes(bad)
                self.assertFalse(rh._codex_agent_id_proven(self.root))
                self.assertEqual(rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Edit"), app="codex"), {})
        # A real child event repairs the invalid file.
        rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Read", agent_id="c1"), app="codex")
        self.assertTrue(rh._codex_agent_id_proven(self.root))

    def test_concurrent_subagent_starts_both_keep_their_marker(self):
        """Finding 1 (redesigned, coordinator recheck, 2026-09-28): two
        SubagentStart calls racing for the same parent session must not lose
        either child -- the original shared-map design could, since both
        read the same starting state before either wrote back. Independent
        marker files need no shared read-modify-write, so both survive
        regardless of scheduling."""
        import threading
        barrier = threading.Barrier(2)

        def start(agent_id):
            barrier.wait(timeout=5)
            rh._dispatch(self.root, self._payload("SubagentStart", agent_id=agent_id,
                                                   agent_type="general-purpose"), app="codex")

        threads = [threading.Thread(target=start, args=(aid,)) for aid in ("childA", "childB")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertTrue(rh._child_marker_path(self.root, "s1", "childA").is_file())
        self.assertTrue(rh._child_marker_path(self.root, "s1", "childB").is_file())
        self.assertTrue(rh._active_children_status(self.root, "s1")[0])

    def test_overlapping_stop_and_start_do_not_interfere(self):
        """A concurrent stop of one child and start of a different one must
        each only ever touch their own marker file -- no cross-talk."""
        import threading
        rh._dispatch(self.root, self._payload("SubagentStart", agent_id="childA", agent_type="general-purpose"),
                     app="codex")
        barrier = threading.Barrier(2)

        def stop_a():
            barrier.wait(timeout=5)
            rh._dispatch(self.root, self._payload("SubagentStop", agent_id="childA"), app="codex")

        def start_c():
            barrier.wait(timeout=5)
            rh._dispatch(self.root, self._payload("SubagentStart", agent_id="childC",
                                                   agent_type="general-purpose"), app="codex")

        threads = [threading.Thread(target=stop_a), threading.Thread(target=start_c)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(rh._child_marker_path(self.root, "s1", "childA").exists())
        self.assertTrue(rh._child_marker_path(self.root, "s1", "childC").is_file())

    def test_aged_marker_stays_ambiguous_and_is_never_cleaned_up(self):
        """Codex final rechecks, 2026-09-29: an 8-day-old marker (its child's
        SubagentStop never arrived) must survive the hook's housekeeping and
        still read as ambiguous-stale, never (False, ""). Age-based cleanup of
        child markers and their directories was removed outright: every
        variant raced child registration (rmtree deleted live markers; an
        empty-directory rmdir could land between a child's mkdir and touch)."""
        self.assertFalse(hasattr(rh, "_maybe_prune_children"))
        rh._touch_active_child(self.root, "stale-parent", "c1")
        marker = rh._child_marker_path(self.root, "stale-parent", "c1")
        very_old = time.time() - 8 * 24 * 3600
        os.utime(marker, (very_old, very_old))
        os.utime(marker.parent, (very_old, very_old))
        with mock.patch.object(rh.random, "random", return_value=0.0):
            rh._maybe_prune_state(self.root)
        self.assertTrue(marker.is_file())
        has_active, reason = rh._active_children_status(self.root, "stale-parent")
        self.assertTrue(has_active)
        self.assertEqual(reason, "ambiguous-stale")

    def test_child_registers_into_an_old_empty_parent_directory(self):
        """The reverse-order race Codex reproduced: an old EMPTY parent
        directory already exists when a child starts. With no cleanup that
        can remove it between mkdir and touch, registration always lands."""
        parent_dir = rh._children_dir(self.root, "old-empty-parent")
        parent_dir.mkdir(parents=True)
        very_old = time.time() - 8 * 24 * 3600
        os.utime(parent_dir, (very_old, very_old))
        with mock.patch.object(rh.random, "random", return_value=0.0):
            rh._maybe_prune_state(self.root)
        rh._touch_active_child(self.root, "old-empty-parent", "late-child")
        self.assertTrue(rh._child_marker_path(self.root, "old-empty-parent", "late-child").is_file())
        self.assertEqual(rh._active_children_status(self.root, "old-empty-parent"), (True, "ambiguous-child-active"))

    # ------------------------------------------------------------ long_turn

    def test_long_turn_counts_fires_at_threshold_and_resets(self):
        self._hooks(long_turn_threshold=3)
        for _ in range(2):
            out = rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Read"), app="claude")
            self.assertEqual(out, {})
        fired = rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Read"), app="claude")
        self.assertIn("hookSpecificOutput", fired)
        self.assertIn("long turn", fired["hookSpecificOutput"]["additionalContext"].lower())
        state = rh._load_state(rh._state_path(self.root, "s1"))
        self.assertEqual(state["tool_count"], 0)

    def test_long_turn_disabled_still_resets_counter_without_firing(self):
        current = settings.read_hooks(self.root)["hooks"]
        self._hooks(long_turn_threshold=2, trigger_enabled={**current["trigger_enabled"], "long_turn": False})
        rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Read"), app="claude")
        out = rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Read"), app="claude")
        self.assertEqual(out, {})
        state = rh._load_state(rh._state_path(self.root, "s1"))
        self.assertEqual(state["tool_count"], 0)

    def test_message_and_session_start_reset_long_turn_counter(self):
        self._hooks(long_turn_threshold=100)
        rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Read"), app="claude")
        rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Read"), app="claude")
        self.assertEqual(rh._load_state(rh._state_path(self.root, "s1"))["tool_count"], 2)
        rh._dispatch(self.root, self._payload("UserPromptSubmit"), app="claude")
        self.assertEqual(rh._load_state(rh._state_path(self.root, "s1"))["tool_count"], 0)

    def test_long_turn_keys_counters_separately_per_agent(self):
        self._hooks(long_turn_threshold=100)
        rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Read"), app="claude")
        rh._dispatch(self.root, self._payload("PreToolUse", tool_name="Read", agent_id="a1"), app="claude")
        self.assertEqual(rh._load_state(rh._state_path(self.root, "s1"))["tool_count"], 1)
        self.assertEqual(rh._load_state(rh._state_path(self.root, "s1", "a1"))["tool_count"], 1)

    # -------------------------------------------------------------- set CRUD

    def test_default_set_is_read_only_but_duplicable(self):
        current = settings.read_hooks(self.root)
        mutated = {**current["hooks"], "sets": {**current["hooks"]["sets"],
                   settings.BUILTIN_HOOK_SET_NAME: {"roles": {"orchestrator": {t: "changed" if t == "message" else None
                                                                              for t in settings.HOOK_TRIGGERS}}}}}
        with self.assertRaises(ValueError):
            settings.update_hooks(self.root, mutated, expected_revision=current["revision"])
        duplicate = {**current["hooks"], "sets": {**current["hooks"]["sets"],
                     "my-custom": settings.BUILTIN_HOOK_SETS[settings.BUILTIN_HOOK_SET_NAME]}}
        result = settings.update_hooks(self.root, duplicate, expected_revision=current["revision"])
        self.assertIn("my-custom", result["hooks"]["sets"])

    def test_active_set_must_exist(self):
        current = settings.read_hooks(self.root)
        bad = {**current["hooks"], "active_set": "no-such-set"}
        with self.assertRaises(ValueError):
            settings.update_hooks(self.root, bad, expected_revision=current["revision"])

    def test_set_role_texts_validated(self):
        current = settings.read_hooks(self.root)
        bad = {**current["hooks"], "sets": {**current["hooks"]["sets"], "custom": {"roles": {"worker-routine": {
            "message": "x" * (settings.MAX_HOOK_TEXT_CHARS + 1), "session_start": None, "edit_nudge": None,
            "stop_check": None, "long_turn": None}}}}}
        with self.assertRaises(ValueError):
            settings.update_hooks(self.root, bad, expected_revision=current["revision"])

    # --------------------------------------------------- cross-app safe helper

    def test_safe_reminder_text_feeds_role_router_build_prompt(self):
        text = rh.safe_reminder_text(self.root, "orchestrator", "message")
        self.assertTrue(text)
        prompt = role_router._build_prompt(self.root, None, None, "the brief itself", role="orchestrator")
        self.assertIn(text, prompt)
        self.assertIn("the brief itself", prompt)

    def test_builtin_native_route_and_delivery_qualifications_reach_both_apps(self):
        for app in ("claude", "codex"):
            with self.subTest(app=app):
                out = rh._dispatch(self.root, self._payload("UserPromptSubmit", session_id=app), app=app)
                text = out["hookSpecificOutput"]["additionalContext"]
                for required in ("owner-selected native models", "saved routes are advisory",
                                 "same-app gives native spawn instructions", "cross-app creates the chat through Crosstalk",
                                 "This reminder grants no authority",
                                 "Discover large or unknown scope first", "small changes need a one-line brief",
                                 "only under existing authorization", "milestone or remote grants none"):
                    self.assertIn(required, text)
                self.assertNotIn("close out and push own paths", text)

    def test_custom_body_and_delivery_notice_survive_every_emitted_bound(self):
        # Include the former default phrase to catch arbitrary custom-body
        # rewriting; use the maximum valid body to catch a dropped suffix.
        body = "Owner text: close out and push own paths. "
        body += "x" * (settings.MAX_HOOK_TEXT_CHARS - len(body))
        hooks = settings.read_hooks(self.root)["hooks"]
        custom = {"roles": {"orchestrator": {trigger: body for trigger in settings.HOOK_TRIGGERS}}}
        self._hooks(active_set="custom", sets={**hooks["sets"], "custom": custom}, long_turn_threshold=1)
        self._enable_end_turn()
        modes = (("manual", "Manual/absent delivery", "milestone or remote grants none"),
                 ("commit-only", "Automatic delivery (commit-only)", "do not stage, commit or push independently"),
                 ("commit+push", "Automatic delivery (commit+push)", "do not stage, commit or push independently"),
                 ("unavailable", "Delivery configuration unavailable", "authorizes no commit, push or automatic submission"))
        for app in ("claude", "codex"):
            for mode, label, qualification in modes:
                session = app + "-" + mode
                with self.subTest(app=app, mode=mode), \
                        mock.patch("eutonos.git_delivery_native.guidance", return_value={"mode": mode}):
                    for event in ("SessionStart", "UserPromptSubmit", "PreToolUse"):
                        out = rh._dispatch(self.root, self._payload(event, session_id=session, tool_name="Edit"), app=app)
                        text = out["hookSpecificOutput"]["additionalContext"]
                        self.assertTrue(text.startswith(body + "\n"))
                        self.assertIn(label, text)
                        self.assertIn(qualification, text)
                        copies = 2 if event == "PreToolUse" else 1
                        self.assertEqual(text.count(body), copies)
                        self.assertLessEqual(len(text), copies * (settings.MAX_HOOK_TEXT_CHARS + 1
                                                                 + rh.MAX_DELIVERY_NOTICE_CHARS) + copies - 1)
                    stop = rh._dispatch(self.root, self._payload("Stop", session_id=session), app=app)
                    text = (rh._end_turn_reminder_path(self.root, session).read_text(encoding="utf-8")
                            if app == "codex" else stop["reason"])
                    self.assertIn(body + "\n" + label, text)
                    self.assertIn(qualification, text)
        self.assertEqual(settings.read_hooks(self.root)["hooks"]["sets"]["custom"], custom)

    def test_unavailable_delivery_reader_does_not_block_optional_reminder(self):
        with mock.patch("eutonos.git_delivery_native.guidance", side_effect=RuntimeError("private details")):
            text = rh.safe_reminder_text(self.root, "orchestrator", "message")
        self.assertIn("Delivery configuration unavailable", text)
        self.assertIn("authorizes no commit, push or automatic submission", text)
        self.assertNotIn("private details", text)

    def test_safe_reminder_text_never_raises_for_blank_or_unknown(self):
        self.assertEqual(rh.safe_reminder_text(self.root, "", "message"), "")
        self.assertEqual(rh.safe_reminder_text(self.root, None, "message"), "")
        # An unknown role name degrades gracefully to the active set's "default"
        # entry (see _text_for) rather than raising or returning blank.
        expected_default = settings.BUILTIN_HOOK_SETS[settings.BUILTIN_HOOK_SET_NAME]["roles"]["default"]["message"]
        self.assertTrue(rh.safe_reminder_text(self.root, "totally-unknown-role", "message").startswith(expected_default + "\n"))
        # A broken settings read (any exception) must never propagate -- it
        # degrades to "" rather than blocking prompt assembly.
        with mock.patch.object(settings, "read_hooks_bounded", side_effect=RuntimeError("boom")):
            self.assertEqual(rh.safe_reminder_text(self.root, "orchestrator", "message"), "")

    def test_main_dispatches_end_to_end(self):
        stdout = io.StringIO()
        stdin = mock.Mock()
        stdin.buffer.read.return_value = json.dumps(self._payload("UserPromptSubmit")).encode("utf-8")
        with mock.patch.object(sys, "stdout", stdout), mock.patch.object(sys, "stdin", stdin):
            code = rh.main(["--app", "claude", "--root", str(self.root)])
        self.assertEqual(code, 0)
        value = json.loads(stdout.getvalue())
        self.assertEqual(value["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")

    def test_main_never_raises_on_garbage_stdin(self):
        stdout = io.StringIO()
        stdin = mock.Mock()
        stdin.buffer.read.return_value = b"{not json"
        with mock.patch.object(sys, "stdout", stdout), mock.patch.object(sys, "stdin", stdin):
            code = rh.main(["--app", "claude", "--root", str(self.root)])
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue().strip(), "{}")

    # --------------------------------------------------------------- log

    def test_log_is_metadata_only_and_bounded(self):
        self._hooks(edit_nudge_rate_limit_minutes=1)
        for i in range(40):
            rh._dispatch(self.root, self._payload("UserPromptSubmit", prompt=f"secret prompt {i}"), app="claude")
        events = rh.read_recent_events(self.root, limit=10)
        self.assertTrue(events)
        self.assertLessEqual(len(events), 10)
        for row in events:
            self.assertEqual(set(row), {"t", "app", "session", "has_agent_id", "role", "set", "trigger", "fired", "reason"})
            self.assertIs(row["has_agent_id"], False)
            self.assertNotIn("prompt", json.dumps(row))
            self.assertNotIn("secret", json.dumps(row))
        log_path = rh._events_log_path(self.root)
        raw = log_path.read_text(encoding="utf-8")
        self.assertNotIn("secret prompt", raw)
        # Bounded rotation: force well past MAX_LOG_BYTES and confirm it shrinks back.
        with mock.patch.object(rh, "MAX_LOG_BYTES", 200):
            for i in range(200):
                rh._dispatch(self.root, self._payload("UserPromptSubmit"), app="claude")
        self.assertLessEqual(log_path.stat().st_size, rh.MAX_LOG_BYTES * 50)  # generous: rotation ran at least once

    def test_read_recent_events_never_raises_for_missing_or_corrupt_log(self):
        self.assertEqual(rh.read_recent_events(self.root), [])
        log_path = rh._events_log_path(self.root)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("not json at all\n{\"partial\":true", encoding="utf-8")
        self.assertEqual(rh.read_recent_events(self.root), [])

    # --------------------------------------------------------- finding 4: bounds

    def test_load_state_ignores_oversized_state_file(self):
        """A state file bigger than MAX_STATE_BYTES (this program never writes
        one that large itself -- an external write or a bug elsewhere might)
        is never read in full; it degrades to {} like any other corrupt file."""
        state_path = rh._state_path(self.root, "s1")
        state_path.parent.mkdir(parents=True, exist_ok=True)
        oversized = json.dumps({"tool_count": 1, "padding": "x" * (rh.MAX_STATE_BYTES + 1000)})
        self.assertGreater(len(oversized.encode("utf-8")), rh.MAX_STATE_BYTES)
        state_path.write_text(oversized, encoding="utf-8")
        self.assertEqual(rh._load_state(state_path), {})

    def test_read_recent_events_bounds_an_oversized_log_via_tail_read(self):
        """A log file far bigger than MAX_LOG_TAIL_BYTES (grown externally,
        past whatever rotated it) is still read via a bounded tail read --
        the newest (most "recent") rows, which live at the end, are returned
        rather than the whole file ever being read into memory."""
        log_path = rh._events_log_path(self.root)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Pad the log with oversized filler lines until it clears the tail cap,
        # then append a handful of real, valid rows at the very end.
        with log_path.open("w", encoding="utf-8", newline="\n") as fh:
            filler = "x" * 4096
            written = 0
            while written < rh.MAX_LOG_TAIL_BYTES + (64 * 1024):
                line = json.dumps({"t": "filler", "app": "codex", "session": "s0", "role": None,
                                  "set": None, "trigger": "message", "fired": False, "reason": filler})
                fh.write(line + "\n")
                written += len(line) + 1
            for i in range(5):
                row = {"t": f"row{i}", "app": "claude", "session": "s1", "role": "orchestrator",
                      "set": "eutonos-default", "trigger": "message", "fired": True, "reason": "ok"}
                fh.write(json.dumps(row) + "\n")
        self.assertGreater(log_path.stat().st_size, rh.MAX_LOG_TAIL_BYTES)
        events = rh.read_recent_events(self.root, limit=5)
        self.assertEqual(len(events), 5)
        self.assertEqual({row["t"] for row in events}, {f"row{i}" for i in range(5)})

    def test_maybe_prune_state_bounds_scan_for_a_huge_state_directory(self):
        """Even a huge state directory (far more entries than
        STATE_PRUNE_SCAN_LIMIT, all old enough to prune) only ever has a
        bounded number of entries examined/removed per prune pass -- never a
        full directory listing/sort/stat."""
        directory = rh._hooks_dir(self.root) / rh.STATE_SUBDIR
        directory.mkdir(parents=True, exist_ok=True)
        total = 20
        old_time = time.time() - rh.STATE_PRUNE_AGE_SECONDS - 3600
        for i in range(total):
            path = directory / f"entry{i}.json"
            path.write_text("{}", encoding="utf-8")
            import os
            os.utime(path, (old_time, old_time))
        guard = rh._end_turn_guard_path(self.root, "old-turn")
        guard.parent.mkdir(parents=True, exist_ok=True)
        reminder = rh._end_turn_reminder_path(self.root, "old-turn")
        guard.write_text('{"nonce":"old"}', encoding="utf-8")
        reminder.write_text("old reminder", encoding="utf-8")
        os.utime(guard, (old_time, old_time))
        with mock.patch.object(rh.random, "random", return_value=0.0), \
             mock.patch.object(rh, "STATE_PRUNE_SCAN_LIMIT", 5):
            rh._maybe_prune_state(self.root)
        self.assertFalse(guard.exists())
        self.assertFalse(reminder.exists())
        remaining = list(directory.glob("*.json"))
        # At most STATE_PRUNE_SCAN_LIMIT entries were ever examined this pass,
        # so at most that many could have been removed.
        self.assertGreaterEqual(len(remaining), total - 5)
        self.assertLess(len(remaining), total)  # the bounded pass still did real work

    def test_hooks_config_never_waits_on_a_held_lock(self):
        """The hook's own settings read must never wait on the shared
        local_lock -- a short timeout means a concurrent settings WRITE
        holding that lock degrades this to "no reminder this time" (None)
        instead of ever blocking."""
        from eutonos import local_lock as ll
        lock_path = settings._resolve_shared_root(self.root) / ".eutonos-local" / ".locks" / "metadata.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        ll._acquire_file(handle)  # simulate another process/writer already holding the OS-level lock
        try:
            started = time.monotonic()
            result = rh._hooks_config(self.root)
            elapsed = time.monotonic() - started
        finally:
            ll._release_file(handle)
            handle.close()
        self.assertIsNone(result)
        self.assertLess(elapsed, 2.0)  # generous: real budget is HOOKS_SETTINGS_LOCK_TIMEOUT_SECONDS

    # --------------------------------------------------------- finding 5: bounds

    def test_hooks_config_returns_none_quickly_for_an_oversized_settings_file(self):
        """Finding 5 (coordinator recheck, 2026-09-28): role_hooks.py's own
        settings read must never read the whole settings.json file --
        settings.read_hooks_bounded caps the read itself, so an oversized
        file (grown externally, or by a bug elsewhere) degrades to "no
        reminder this time" (None) immediately, the same as any other
        settings read error, rather than ever being parsed in full."""
        local_path = settings._resolve_shared_root(self.root) / ".eutonos-local" / "settings.json"
        local_path.parent.mkdir(parents=True, exist_ok=True)
        padded = json.dumps({"profile": settings.PROFILE, "padding": "x" * (settings.HOOKS_BOUNDED_READ_MAX_BYTES + 1000)})
        self.assertGreater(len(padded.encode("utf-8")), settings.HOOKS_BOUNDED_READ_MAX_BYTES)
        local_path.write_text(padded, encoding="utf-8")
        started = time.monotonic()
        result = rh._hooks_config(self.root)
        elapsed = time.monotonic() - started
        self.assertIsNone(result)
        self.assertLess(elapsed, 2.0)
        # And the full dispatch path degrades to a plain no-op, not a raise.
        out = rh._dispatch(self.root, self._payload("UserPromptSubmit"), app="claude")
        self.assertEqual(out, {})

    def test_tail_read_bounds_actual_read_calls(self):
        """Finding 5: _tail_read must issue exactly one bounded read(max_bytes)
        call -- no bare/unbounded read(), no separate readline() hitting the
        filesystem."""
        class _RecordingHandle:
            def __init__(self, data):
                self._buf = io.BytesIO(data)
                self.read_calls = []

            def seek(self, *args):
                return self._buf.seek(*args)

            def tell(self):
                return self._buf.tell()

            def read(self, *args):
                self.read_calls.append(args)
                return self._buf.read(*args)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        data = (b"a" * 2000) + b"\n" + (b"b" * 2000)
        path = Path(self.temp.name) / "recording.jsonl"
        path.write_bytes(data)
        handle = _RecordingHandle(data)
        with mock.patch.object(Path, "open", lambda self, mode="r": handle):
            result = rh._tail_read(path, 500)
        self.assertEqual(handle.read_calls, [(500,)])
        self.assertLessEqual(len(result.encode("utf-8")), 500)

    def test_tail_read_growth_during_read_stays_bounded(self):
        """Finding 5: even if the underlying file grows BETWEEN the size
        check and the bounded read call (a concurrent append, simulated here
        via a fake handle), the single capped read(max_bytes) call can never
        return more than max_bytes bytes -- no reliance on the earlier,
        possibly-stale size."""
        initial = b"x" * 100
        grown = initial + (b"y" * 5000)

        class _GrowingHandle:
            def __init__(self):
                self._buf = io.BytesIO(initial)
                self._grown = False

            def seek(self, *args):
                return self._buf.seek(*args)

            def tell(self):
                return self._buf.tell()

            def read(self, size=-1):
                if not self._grown:
                    # Simulate the file growing on disk between the SEEK_END
                    # size check and this read call.
                    position = self._buf.tell()
                    self._buf = io.BytesIO(grown[position:])
                    self._grown = True
                return self._buf.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        handle = _GrowingHandle()
        path = Path(self.temp.name) / "growing.jsonl"
        path.write_bytes(initial)
        with mock.patch.object(Path, "open", lambda self, mode="r": handle):
            result = rh._tail_read(path, 50)
        self.assertLessEqual(len(result.encode("utf-8")), 50)


if __name__ == "__main__":
    unittest.main()
