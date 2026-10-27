"""Stop checkpoints: actual native readback, scope isolation, and finite debt."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from tests import test_workflow as fixture
from eutonos import closeout_debt as debt, role_hooks, settings, workflow_cli
from eutonos.workflow import prepare_closeout, execute_closeout


class DebtTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="closeout-debt-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.payload = {"hook_event_name": "Stop", "session_id": "thread", "turn_id": "turn",
                        "cwd": str(self.root), "stop_hook_active": False}

    def test_one_guard_shared_by_stop_and_subagent_stop(self):
        payload = {**self.payload, "agent_id": "child"}
        first, reason = debt.check(self.root, payload, "codex")
        self.assertEqual(first["decision"], "block")
        self.assertEqual(reason, "continuation-requested")
        second, reason = debt.check(self.root, {**payload, "hook_event_name": "SubagentStop"}, "codex")
        self.assertEqual(second, {})
        self.assertEqual(reason, "unresolved-continuation-spent")

    def test_concurrent_stop_events_spend_one_create_once_guard(self):
        debt.observe(self.root, self.payload, "codex")
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: debt.check(self.root, self.payload, "codex"), range(2)))
        self.assertEqual(sum(bool(out) for out, _ in results), 1)

    def test_default_on_role_hook_allows_meaningful_dialogue_without_checkpoint(self):
        self.assertTrue(settings.DEFAULT_HOOKS["closeout_check"])
        with mock.patch.object(role_hooks, "_hooks_config", return_value=settings.DEFAULT_HOOKS), \
                mock.patch.object(debt, "check") as check:
            out = role_hooks._dispatch(self.root, {**self.payload, "last_assistant_message": "Meaningful explanation."}, app="codex")
            # One end-turn pass, then the turn ends with no checkpoint filed.
            self.assertEqual(out["decision"], "block")
            # Codex gets a link cue; the full reminder is in the linked file.
            saved = role_hooks._end_turn_reminder_path(self.root, "thread").read_text(encoding="utf-8")
            self.assertIn(role_hooks.END_TURN_RECORDING_POLICY, saved)
            done =role_hooks._dispatch(self.root, {**self.payload, "stop_hook_active": True}, app="codex")
        self.assertEqual(done, {})
        check.assert_not_called()

    def test_subagent_stop_parent_turn_reuses_exact_child_scope(self):
        payload = {**self.payload, "agent_id": "child", "turn_id": "child-turn"}
        record = debt.observe(self.root, payload, "codex")
        debt.assess(self.root, record["scope_id"], "noop", "Explicit child acknowledgement.")
        parent_event = {**payload, "hook_event_name": "SubagentStop", "turn_id": "parent-turn"}
        retained = debt.observe(self.root, parent_event, "codex")
        self.assertEqual(retained["scope_id"], record["scope_id"])
        self.assertEqual(retained["scope"]["turn_id"], "child-turn")
        self.assertEqual(debt.check(self.root, parent_event, "codex")[0], {})
        unknown_child = debt.observe(self.root, {**parent_event, "agent_id": "unseen"}, "codex")
        self.assertIsNone(unknown_child["scope"]["turn_id"])

    def test_new_child_start_cannot_borrow_old_ack_or_parent_turn_identity(self):
        payload = {**self.payload, "agent_id": "child", "hook_event_name": "SubagentStart"}
        original = debt.observe(self.root, payload, "codex")
        self.assertIsNone(original["scope"]["turn_id"])
        debt.assess(self.root, original["scope_id"], "noop", "Previous job was trivial.")
        next_job = debt.observe(self.root, payload, "codex")
        self.assertNotEqual(next_job["scope_id"], original["scope_id"])
        self.assertIsNone(next_job["assessment"])

    def test_synthetic_prompt_new_turn_keeps_original_guard(self):
        first, _ = debt.check(self.root, self.payload, "codex")
        original = debt.observe(self.root, self.payload, "codex")
        prompt = {**self.payload, "hook_event_name": "UserPromptSubmit", "turn_id": "continuation", "prompt": first["reason"]}
        continued = debt.observe(self.root, prompt, "codex")
        self.assertEqual(continued["scope_id"], original["scope_id"])
        out, reason = debt.check(self.root, {**prompt, "hook_event_name": "Stop"}, "codex")
        self.assertEqual(out, {})
        self.assertEqual(reason, "unresolved-continuation-spent")

    def test_stale_token_cannot_reactivate_acknowledged_historical_turn(self):
        first, _ = debt.check(self.root, self.payload, "codex")
        original = debt.observe(self.root, self.payload, "codex")
        debt.assess(self.root, original["scope_id"], "noop", "Earlier turn acknowledgement.")
        new_payload = {**self.payload, "hook_event_name": "UserPromptSubmit", "turn_id": "next", "prompt": "New work"}
        current = debt.observe(self.root, new_payload, "codex")
        replay = debt.observe(self.root, {**new_payload, "prompt": first["reason"]}, "codex")
        self.assertEqual(replay["scope_id"], current["scope_id"])
        self.assertNotEqual(replay["scope_id"], original["scope_id"])
        self.assertIsNone(replay["assessment"])

    def test_stop_old_snapshot_cannot_roll_back_newer_tool_observation(self):
        record = debt.observe(self.root, self.payload, "codex")
        debt.assess(self.root, record["scope_id"], "noop", "Before later work.")
        read, resume = threading.Event(), threading.Event()
        original = debt.observe
        def scheduled(root, payload, app):
            value = original(root, payload, app)
            if payload["hook_event_name"] == "Stop":
                read.set()
                self.assertTrue(resume.wait(5))
            return value
        with mock.patch.object(debt, "observe", side_effect=scheduled), ThreadPoolExecutor(max_workers=1) as worker:
            stopped = worker.submit(debt.check, self.root, self.payload, "codex")
            self.assertTrue(read.wait(5))
            updated = original(self.root, {**self.payload, "hook_event_name": "PreToolUse"}, "codex")
            resume.set()
            out, _ = stopped.result(timeout=5)
        self.assertEqual(updated["generation"], record["generation"] + 1)
        self.assertEqual(debt.load(self.root, record["scope_id"])["generation"], updated["generation"])
        self.assertEqual(out["decision"], "block")

    def test_assessment_overlap_retains_sticky_uncertainty_until_blocked(self):
        record = debt.observe(self.root, self.payload, "codex")
        read, resume = threading.Event(), threading.Event()
        original = debt.load
        def scheduled(root, scope):
            value = original(root, scope)
            if threading.current_thread().name.startswith("ack-test"):
                read.set()
                self.assertTrue(resume.wait(5))
            return value
        with mock.patch.object(debt, "load", side_effect=scheduled), ThreadPoolExecutor(max_workers=1, thread_name_prefix="ack-test") as worker:
            acknowledged = worker.submit(debt.assess, self.root, record["scope_id"], "noop", "Concurrent old assessment.")
            self.assertTrue(read.wait(5))
            with self.assertRaises(ValueError):
                debt.observe(self.root, {**self.payload, "hook_event_name": "PreToolUse"}, "codex")
            resume.set()
            with self.assertRaisesRegex(ValueError, "uncertain"):
                acknowledged.result(timeout=5)
        self.assertTrue(debt._uncertain(self.root, record["scope_id"]))
        with self.assertRaisesRegex(ValueError, "blocker"):
            debt.assess(self.root, record["scope_id"], "noop", "Cannot silently repair.")
        debt.assess(self.root, record["scope_id"], "blocked", "Concurrent observation could not be serialized.")
        self.assertEqual(debt.check(self.root, self.payload, "codex"), ({}, "acknowledged-blocked"))
        self.assertTrue(debt._uncertain(self.root, record["scope_id"]))

    def test_end_turn_pass_ignores_closeout_debt_state(self):
        record = debt.observe(self.root, self.payload, "codex")
        debt.assess(self.root, record["scope_id"], "noop", "Trivial exchange.")
        cfg = deepcopy(settings.DEFAULT_HOOKS)
        cfg.pop("closeout_check")  # Existing saved config takes new default.
        reminder = "Before finishing, check the work and close out meaningful Docs/RAM."
        with mock.patch.object(role_hooks, "_text_for", return_value=(reminder, "ok")), \
                mock.patch.object(debt, "check") as check:
            out = role_hooks._handle_stop(self.root, self.payload, cfg, app="codex")
            self.assertEqual(out["decision"], "block")
            saved = role_hooks._end_turn_reminder_path(self.root, "thread").read_text(encoding="utf-8")
            self.assertIn(reminder, saved)
            self.assertNotIn("no verified closeout checkpoint", saved + out["reason"])
            # A missing checkpoint in the same turn never adds a second pass.
            self.assertEqual(role_hooks._handle_stop(self.root,
                {**self.payload, "turn_id": "missing-checkpoint"}, cfg, app="codex"), {})
            check.assert_not_called()

    def test_scope_isolates_turn_agent_root_and_app(self):
        original = debt.observe(self.root, self.payload, "codex")
        debt.assess(self.root, original["scope_id"], "noop", "Trivial acknowledgement only.")
        for payload, app, root in [({**self.payload, "turn_id": "next"}, "codex", self.root),
                                    ({**self.payload, "agent_id": "child"}, "codex", self.root),
                                    (self.payload, "claude", self.root),
                                    (self.payload, "codex", self.root / "other")]:
            with self.subTest(payload=payload, app=app, root=root):
                out, _ = debt.check(root, payload, app)
                self.assertEqual(out["decision"], "block")

    def test_explicit_noop_and_blocked_ack_never_claim_ram(self):
        record = debt.observe(self.root, self.payload, "codex")
        for disposition in ("noop", "blocked"):
            ack = debt.assess(self.root, record["scope_id"], disposition, "Assessment supplied by agent.")
            self.assertFalse(ack["native_verified"])
            self.assertEqual(debt.check(self.root, self.payload, "codex")[0], {})
        self.assertFalse((self.root / "RAM").exists())

    def test_later_tool_reopens_ack_without_second_continuation(self):
        record = debt.observe(self.root, self.payload, "codex")
        debt.assess(self.root, record["scope_id"], "noop", "Read-only trivial exchange.")
        debt.observe(self.root, {**self.payload, "hook_event_name": "PreToolUse", "tool_name": "Bash"}, "codex")
        first, _ = debt.check(self.root, self.payload, "codex")
        self.assertEqual(first["decision"], "block")
        debt.assess(self.root, record["scope_id"], "blocked", "Required RAM write was denied; report it.")
        debt.observe(self.root, {**self.payload, "hook_event_name": "PreToolUse"}, "codex")
        self.assertEqual(debt.check(self.root, self.payload, "codex")[0], {})

    def test_guard_failure_fails_open_and_reports_unresolved(self):
        debt.observe(self.root, self.payload, "codex")
        original = Path.open
        def denied(path, mode="r", *args, **kwargs):
            if mode == "xb":
                raise PermissionError("denied")
            return original(path, mode, *args, **kwargs)
        with mock.patch.object(Path, "open", denied):
            out, reason = debt.check(self.root, self.payload, "codex")
        self.assertEqual(out, {})
        self.assertIn("unresolved", reason)

    def test_stop_hook_active_never_requests_continuation(self):
        self.assertEqual(debt.check(self.root, {**self.payload, "stop_hook_active": True}, "codex")[0], {})

    def test_missing_identity_and_corrupted_state_are_unverified(self):
        self.assertEqual(debt.check(self.root, {"hook_event_name": "Stop"}, "codex")[0], {})
        record = debt.observe(self.root, self.payload, "codex")
        debt._path(self.root, record["scope_id"]).write_bytes(b"x" * (debt.MAX_STATE + 1))
        out, reason = debt.check(self.root, self.payload, "codex")
        self.assertEqual(out, {})
        self.assertIn("unresolved", reason)

    def test_deeply_nested_and_wrong_typed_state_fail_open(self):
        record = debt.observe(self.root, self.payload, "codex")
        path = debt._path(self.root, record["scope_id"])
        for bad in (b'{"nested":' + b'[' * 1200 + b'0' + b']' * 1200 + b'}',
                    json.dumps({**record, "scope": []}).encode(),
                    json.dumps({**record, "generation": "one"}).encode()):
            with self.subTest(bad=bad[:30]):
                path.write_bytes(bad)
                out, reason = debt.check(self.root, self.payload, "codex")
                self.assertEqual(out, {})
                self.assertIn("unresolved", reason)

    def test_claude_local_boundary_requires_observed_prompt(self):
        payload = {key: value for key, value in self.payload.items() if key != "turn_id"}
        self.assertEqual(debt.check(self.root, payload, "claude")[1], "unresolved-hook-boundary-unavailable")
        record = debt.observe(self.root, {**payload, "hook_event_name": "UserPromptSubmit", "prompt": "Real work"}, "claude")
        self.assertIsNone(record["scope"]["turn_id"])
        self.assertTrue(record["scope"]["local_boundary"])
        self.assertEqual(debt.check(self.root, payload, "claude")[0]["decision"], "block")

    def test_ack_cli_uses_opaque_scope_and_reason_file(self):
        record = debt.observe(self.root, self.payload, "codex")
        reason = self.root / "reason.txt"
        reason.write_text("Owner denied the write; retain debt and report the boundary.", encoding="utf-8")
        with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            code = debt.main(["assess", "--root", str(self.root), "--scope", record["scope_id"],
                              "--disposition", "blocked", "--reason-file", str(reason)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["outcome"], "acknowledged-blocked")
        with self.assertRaises(ValueError):
            debt.assess(self.root, "../escape", "noop", "not allowed")

    def test_hooks_global_off_switch(self):
        cfg = deepcopy(settings.DEFAULT_HOOKS)
        cfg["enabled"] = False
        with mock.patch.object(role_hooks, "_hooks_config", return_value=cfg), mock.patch.object(debt, "observe") as observe:
            self.assertEqual(role_hooks._dispatch(self.root, self.payload, app="codex"), {})
            observe.assert_not_called()

    def test_subagent_marker_released_after_end_turn_pass(self):
        payload = {**self.payload, "hook_event_name": "SubagentStop", "agent_id": "child"}
        with mock.patch.object(role_hooks, "_release_active_child", return_value="released") as release:
            first = role_hooks._handle_subagent_stop(self.root, payload, settings.DEFAULT_HOOKS, app="codex")
            self.assertEqual(first["decision"], "block")
            release.assert_not_called()  # The continued child is still active.
            self.assertEqual(role_hooks._handle_subagent_stop(self.root, {**payload, "stop_hook_active": True},
                                                              settings.DEFAULT_HOOKS, app="codex"), {})
            release.assert_called_once()

    def test_saved_missing_setting_migrates_without_changing_stop_check(self):
        cfg = deepcopy(settings.DEFAULT_HOOKS)
        cfg.pop("closeout_check")
        migrated = settings.validate_hooks(cfg)
        self.assertTrue(migrated["closeout_check"])
        self.assertTrue(migrated["trigger_enabled"]["stop_check"])
        self.assertEqual(migrated["sets"], cfg["sets"])
        with self.assertRaises(ValueError):
            settings.validate_hooks({**cfg, "closeout_check": "true"})


class WorkflowDebtTests(unittest.TestCase):
    setUp = fixture.WorkflowTests.setUp
    git = fixture.WorkflowTests.git
    admit = fixture.WorkflowTests.admit
    native_log = fixture.WorkflowTests.native_log

    def checkpoint(self, native=True, debt_root=None):
        log = self.native_log() if native else None
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": "thread-fixture" if native else self.context["session_id"],
                   "turn_id": "turn-fixture"}
        if log:
            payload["transcript_path"] = str(log)
        record = debt.observe(debt_root or self.root, payload, "codex")
        context = {**self.context, "closeout_debt": record["scope_id"]}
        if debt_root is not None:
            context["closeout_debt_root"] = str(debt_root)
        if log:
            context["native_log"] = str(log)
        prepared = prepare_closeout(self.new_values, context)
        self.assertEqual(prepared["outcome"], "prepared", prepared)
        self.admit(prepared)
        result = execute_closeout(prepared, context)
        self.assertEqual(result["outcome"], "completed", result)
        result["closeout_attention"] = workflow_cli._closeout_attention(result)
        result["closeout_receipt"] = workflow_cli._closeout_receipt(result)
        values = {**self.new_values, "work_closeout": [], "work_closeout_reason": "No tracked item status changed in this checkpoint."}
        return payload, record, context, result, values

    def hook_worktree(self, name="hook-checkout"):
        root = self.home / name
        self.git("worktree", "add", "--detach", str(root), "HEAD")
        return root

    def test_cross_worktree_native_proof_is_quiet_without_copying_ram(self):
        # Actual topology: hooks use the original checkout while normal
        # closeout evidence belongs to the explicitly selected linked worktree.
        hook_root = self.root
        self.root = self.hook_worktree("evidence-checkout")
        (self.root / "RAM/work/session").mkdir(parents=True)
        (self.root / ".eutonos-local").mkdir()
        self.host = self.home / "evidence-host"
        self.context = {**self.context, "root": str(self.root), "host": str(self.host)}
        (self.root / "changed.py").write_text("before = False\n", encoding="utf-8")
        payload, record, context, result, values = self.checkpoint(debt_root=hook_root)
        published = debt.publish(self.root, context, result, values)
        self.assertEqual(published["outcome"], "verified-prefinal")
        self.assertFalse(published["counts_completed"])
        proof = debt._read(debt._path(hook_root, record["scope_id"], ".proof.json"))[0]
        self.assertEqual(proof["evidence"]["work"]["root"], str(self.root))
        self.assertFalse((hook_root / proof["ram"]["path"]).exists())
        self.assertFalse(debt._path(self.root, record["scope_id"], ".proof.json").exists())
        # Stop reads only bounded direct metadata/evidence, even across roots.
        with mock.patch.object(debt.subprocess, "run", side_effect=AssertionError("hook ran Git")), \
                mock.patch.object(Path, "glob", side_effect=AssertionError("hook scanned")), \
                mock.patch.object(Path, "rglob", side_effect=AssertionError("hook scanned")):
            self.assertEqual(debt.check(hook_root, {**payload, "hook_event_name": "Stop"}, "codex"),
                             ({}, "verified-prefinal"))
        article = self.root / proof["ram"]["path"]
        article.write_bytes(article.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "changed"):
            debt._verified(hook_root, debt.load(hook_root, record["scope_id"]))

    def test_cross_worktree_registered_checkpoint_preserves_session_and_child_rules(self):
        hook_root = self.hook_worktree()
        payload, record, context, result, values = self.checkpoint(native=False, debt_root=hook_root)
        with self.assertRaisesRegex(ValueError, "session differs"):
            debt.publish(self.root, {**context, "session_id": "foreign"}, result, values)
        child = debt.observe(hook_root, {**payload, "agent_id": "child"}, "codex")
        with self.assertRaisesRegex(ValueError, "child registered identity unavailable"):
            debt.publish(self.root, {**context, "closeout_debt": child["scope_id"]}, result, values)
        published = debt.publish(self.root, context, result, values)
        self.assertEqual(published["basis"], "registered-checkpoint")
        self.assertFalse(published["counts_completed"])
        self.assertEqual(debt.check(hook_root, {**payload, "hook_event_name": "Stop"}, "codex")[0], {})
        debt.observe(hook_root, {**payload, "hook_event_name": "PreToolUse"}, "codex")
        with self.assertRaisesRegex(ValueError, "boundary differs"):
            debt._verified(hook_root, debt.load(hook_root, record["scope_id"]))
        debt._mark_uncertain(hook_root, record["scope_id"])
        with self.assertRaisesRegex(ValueError, "uncertain"):
            debt.publish(self.root, context, result, values)

    def test_cross_worktree_refuses_foreign_repo_and_work_context_mismatch(self):
        hook_root = self.hook_worktree()
        _, record, context, result, values = self.checkpoint(debt_root=hook_root)
        foreign = self.home / "foreign-repository"
        foreign.mkdir()
        original = self.root
        try:
            self.root = foreign
            self.git("init", "-q")
        finally:
            self.root = original
        with self.assertRaisesRegex(ValueError, "another Git repository"):
            debt.publish(self.root, {**context, "closeout_debt_root": str(foreign)}, result, values)
        with self.assertRaisesRegex(ValueError, "evidence root differs"):
            debt.publish(hook_root, context, result, values)
        with self.assertRaisesRegex(ValueError, "absolute checkout path"):
            debt.publish(self.root, {**context, "closeout_debt_root": "relative"}, result, values)
        # Omitting the explicit hook root cannot find or borrow its scope.
        with self.assertRaises(FileNotFoundError):
            debt.publish(self.root, {k: v for k, v in context.items() if k != "closeout_debt_root"}, result, values)
        self.assertFalse(debt._path(hook_root, record["scope_id"], ".proof.json").exists())

    def test_cross_worktree_proof_root_and_git_links_are_pinned(self):
        hook_root = self.hook_worktree()
        _, record, context, result, values = self.checkpoint(debt_root=hook_root)
        debt.publish(self.root, context, result, values)
        record = debt.load(hook_root, record["scope_id"])
        proof_path = debt._path(hook_root, record["scope_id"], ".proof.json")
        proof = debt._read(proof_path)[0]
        other = self.hook_worktree("other-checkout")
        changed = deepcopy(proof)
        changed["evidence"]["work"] = debt._git_binding(other)
        debt._write(proof_path, changed)
        with self.assertRaisesRegex(ValueError, "root differs"):
            debt._verified(hook_root, record)
        debt._write(proof_path, proof)
        for section in ("ram", "capture"):
            changed = deepcopy(proof)
            changed[section]["path"] = "../escaped.json"
            debt._write(proof_path, changed)
            with self.assertRaisesRegex(ValueError, "escapes checkout"):
                debt._verified(hook_root, record)
        debt._write(proof_path, proof)
        git_dir = Path(proof["evidence"]["debt"]["git_dir"])
        (git_dir / "commondir").write_text("../../foreign", encoding="utf-8")
        with self.assertRaises((OSError, ValueError)):
            debt._verified(hook_root, record)

    def test_real_native_capture_readback_discharge_is_prefinal_only(self):
        payload, record, context, result, values = self.checkpoint()
        published = debt.publish(self.root, context, result, values)
        self.assertEqual(published["basis"], "native-event")
        self.assertFalse(published["counts_completed"])
        self.assertEqual(debt.check(self.root, {**payload, "hook_event_name": "Stop"}, "codex"), ({}, "verified-prefinal"))
        proof = debt._read(debt._path(self.root, record["scope_id"], ".proof.json"))[0]
        self.assertIs(proof["counts_completed"], False)

    def test_registered_checkpoint_uses_real_current_ram_without_native_final_claim(self):
        payload, _, context, result, values = self.checkpoint(native=False)
        published = debt.publish(self.root, context, result, values)
        self.assertEqual(published["basis"], "registered-checkpoint")
        self.assertFalse(published["counts_completed"])
        self.assertEqual(debt.check(self.root, {**payload, "hook_event_name": "Stop"}, "codex")[0], {})

    def test_registered_main_wrong_session_and_unregistered_child_refuse(self):
        payload, _, context, result, values = self.checkpoint(native=False)
        with self.assertRaisesRegex(ValueError, "session differs"):
            debt.publish(self.root, {**context, "session_id": "another-session"}, result, values)
        child = debt.observe(self.root, {**payload, "agent_id": "child"}, "codex")
        with self.assertRaisesRegex(ValueError, "child registered identity unavailable"):
            debt.publish(self.root, {**context, "closeout_debt": child["scope_id"]}, result, values)

    def test_native_context_omission_and_wrong_ram_owner_do_not_downgrade(self):
        _, _, context, result, values = self.checkpoint()
        with self.assertRaisesRegex(ValueError, "recorded native log"):
            debt.publish(self.root, {k: v for k, v in context.items() if k != "native_log"}, result, values)
        with self.assertRaisesRegex(ValueError, "RAM owner differs"):
            debt.publish(self.root, {**context, "actor": "another-writer"}, result, values)

    def test_old_turn_mismatch_stale_ram_and_capture_refuse(self):
        payload, record, context, result, values = self.checkpoint()
        debt.publish(self.root, context, result, values)
        self.assertEqual(debt.check(self.root, {**payload, "turn_id": "next", "hook_event_name": "Stop"}, "codex")[0]["decision"], "block")
        with self.assertRaisesRegex(ValueError, "differs"):
            debt.publish(self.root, context, result, values)
        # A current checkpoint no longer verifies after its exact article changes.
        article = self.root / result["capture_receipt"]["receipt"]["ram"]["path"]
        article.write_bytes(article.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "changed"):
            debt._verified(self.root, record)

    def test_prepare_green_and_missing_filing_assessment_do_not_discharge(self):
        _, _, context, result, values = self.checkpoint()
        for false_result in ({"command": "closeout", "outcome": "prepared"},
                             {"command": "closeout", "outcome": "completed", "current_verified": True}):
            with self.assertRaises(ValueError):
                debt.publish(self.root, context, false_result, values)
        with self.assertRaisesRegex(ValueError, "filing"):
            debt.publish(self.root, context, result, self.new_values)
        with self.assertRaises(KeyError):
            debt.publish(self.root, self.context, result, values)

    def test_pending_and_queued_are_not_landed_ram(self):
        _, record, context, result, values = self.checkpoint()
        result = {"command": "closeout", "outcome": "deferred", "closeout_attention": {"required": False},
                  "write_retry": {"outcome": "queued", "request_id": "write-fixture"},
                  "closeout_receipt": {"state": "submitted", "request_ids": ["write-fixture"]}}
        published = debt.publish(self.root, context, result, values)
        self.assertEqual(published["outcome"], "submitted")
        proof = debt._read(debt._path(self.root, record["scope_id"], ".proof.json"))[0]
        self.assertNotIn("ram", proof)
        with self.assertRaises(ValueError):
            debt.publish(self.root, context, {**result, "closeout_receipt": {"state": "pending-retained"}}, values)

    def test_later_observation_reopens_verified_checkpoint(self):
        payload, _, context, result, values = self.checkpoint()
        debt.publish(self.root, context, result, values)
        debt.observe(self.root, {**payload, "hook_event_name": "PreToolUse"}, "codex")
        self.assertEqual(debt.check(self.root, {**payload, "hook_event_name": "Stop"}, "codex")[0]["decision"], "block")

    def test_verified_child_scope_survives_parent_turn_subagent_stop(self):
        _, _, context, _, values = self.checkpoint()
        child_log = self.home / "native-child.jsonl"
        child_log.write_text(json.dumps({"type": "session_meta", "payload": {"id": "child-thread"}}) + "\n" +
                             json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "child-turn"}}) + "\n", encoding="utf-8")
        child_payload = {"hook_event_name": "PreToolUse", "session_id": "parent-fixture", "turn_id": "child-turn",
                         "agent_id": "child-native", "agent_transcript_path": str(child_log)}
        child_record = debt.observe(self.root, child_payload, "codex")
        child_context = {**context, "session_id": "child-session", "native_log": str(child_log),
                         "closeout_debt": child_record["scope_id"]}
        prepared = prepare_closeout(self.new_values, child_context)
        result = execute_closeout(prepared, child_context)
        self.assertEqual(result["outcome"], "completed", result)
        result["closeout_attention"] = workflow_cli._closeout_attention(result)
        result["closeout_receipt"] = workflow_cli._closeout_receipt(result)
        debt.publish(self.root, child_context, result, values)
        parent_stop = {**child_payload, "hook_event_name": "SubagentStop", "turn_id": "parent-different"}
        self.assertEqual(debt.check(self.root, parent_stop, "codex"), ({}, "verified-prefinal"))

    def test_cli_producer_is_optional_and_runs_after_real_result_classification(self):
        _, _, context, result, values = self.checkpoint()
        input_path = self.home / "values.json"
        context_path = self.home / "context.json"
        input_path.write_text(json.dumps(values), encoding="utf-8")
        context_path.write_text(json.dumps(context), encoding="utf-8")
        with mock.patch.object(workflow_cli, "execute_closeout", return_value=deepcopy(result)), \
                mock.patch.object(workflow_cli.settings, "status", return_value={"tracking": {"enabled": False}}), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            code = workflow_cli.main(["run", "closeout", "--root", str(self.root), "--input", str(input_path), "--context", str(context_path)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["closeout_debt"]["outcome"], "verified-prefinal")


if __name__ == "__main__":
    unittest.main()
