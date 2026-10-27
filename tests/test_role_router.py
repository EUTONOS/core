"""Tests for role_router.  The Crosstalk chat creation is always replaced here
(``create_chat``, or ``_run_crosstalk_cli`` underneath it): no Crosstalk command runs,
no chat is created and no real claude/codex/hermes process is ever launched."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eutonos import crosstalk_status
from eutonos import role_router as rr
from eutonos import settings


def _role(options, preamble=None, autonomy="off"):
    return {"options": options, "preamble": preamble, "autonomy": autonomy}


def _option(harness, model, effort=None):
    return {"harness": harness, "model": model, "effort": effort}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="eutonos-role-router-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "project"
        self.root.mkdir()

    def set_roles(self, roles):
        rev = settings.read_roles(self.root)["revision"]
        return settings.update_roles(self.root, roles, expected_revision=rev)


class ResolveRoleTests(Base):
    def test_single_option_defaults_to_primary(self):
        self.set_roles({"worker": _role([_option("claude", "claude-sonnet-5", "medium")])})
        result = rr.resolve_role(self.root, "worker")
        self.assertEqual((result["harness"], result["model"], result["effort"], result["choice"]),
                         ("claude", "claude-sonnet-5", "medium", "primary"))
        self.assertEqual(rr.resolve_role(self.root, "worker", choice="primary"), result)

    def test_two_options_primary_and_explicit_secondary(self):
        self.set_roles({"director": _role([_option("codex", "gpt-6-luna", "xhigh"),
                                           _option("claude", "claude-opus-5-5", "high")],
                                          preamble="p.md", autonomy="ask")})
        primary = rr.resolve_role(self.root, "director")
        self.assertEqual((primary["harness"], primary["model"], primary["effort"]),
                         ("codex", "gpt-6-luna", "xhigh"))
        secondary = rr.resolve_role(self.root, "director", choice="secondary")
        self.assertEqual((secondary["harness"], secondary["model"], secondary["effort"]),
                         ("claude", "claude-opus-5-5", "high"))
        self.assertEqual(primary["preamble"], "p.md")
        self.assertEqual(primary["autonomy"], "ask")

    def test_secondary_choice_errors_when_only_one_option(self):
        self.set_roles({"worker": _role([_option("claude", "claude-sonnet-5", None)])})
        with self.assertRaises(rr.RoleRouterError) as ctx:
            rr.resolve_role(self.root, "worker", choice="secondary")
        self.assertEqual(ctx.exception.code, "no-secondary")

    def test_unknown_role_errors(self):
        with self.assertRaises(rr.RoleRouterError) as ctx:
            rr.resolve_role(self.root, "does-not-exist")
        self.assertEqual(ctx.exception.code, "unknown-role")

    def test_unknown_choice_errors(self):
        self.set_roles({"worker": _role([_option("claude", "claude-sonnet-5", None)])})
        with self.assertRaises(rr.RoleRouterError) as ctx:
            rr.resolve_role(self.root, "worker", choice="tertiary")
        self.assertEqual(ctx.exception.code, "bad-choice")

    def test_four_options_resolve_by_position_int_and_digit_string(self):
        self.set_roles({"worker": _role([
            _option("claude", "claude-sonnet-5", "low"),
            _option("claude", "claude-sonnet-5", "medium"),
            _option("codex", "gpt-6-luna", "xhigh"),  # Luna's minimum (owner, 2026-09-29)
            _option("codex", "gpt-6-sol", "high"),
        ])})
        third = rr.resolve_role(self.root, "worker", choice=3)
        self.assertEqual((third["harness"], third["model"], third["choice"], third["position"]),
                         ("codex", "gpt-6-luna", "3", 3))
        fourth = rr.resolve_role(self.root, "worker", choice="4")
        self.assertEqual((fourth["harness"], fourth["model"], fourth["choice"], fourth["position"]),
                         ("codex", "gpt-6-sol", "4", 4))
        # aliases still resolve to positions 1/2 on a 4-option role
        self.assertEqual(rr.resolve_role(self.root, "worker", choice="primary")["position"], 1)
        self.assertEqual(rr.resolve_role(self.root, "worker", choice="secondary")["position"], 2)

    def test_position_beyond_the_roles_options_is_no_such_position(self):
        self.set_roles({"worker": _role([_option("claude", "claude-sonnet-5", None),
                                         _option("claude", "claude-sonnet-5", None)])})
        with self.assertRaises(rr.RoleRouterError) as ctx:
            rr.resolve_role(self.root, "worker", choice=3)
        self.assertEqual(ctx.exception.code, "no-such-position")

    def test_position_zero_or_five_is_bad_choice(self):
        self.set_roles({"worker": _role([_option("claude", "claude-sonnet-5", None)])})
        with self.assertRaises(rr.RoleRouterError) as ctx:
            rr.resolve_role(self.root, "worker", choice=0)
        self.assertEqual(ctx.exception.code, "bad-choice")
        with self.assertRaises(rr.RoleRouterError) as ctx:
            rr.resolve_role(self.root, "worker", choice=5)
        self.assertEqual(ctx.exception.code, "bad-choice")


class ProfilePlacementTests(Base):
    """AWR7 (2026-09-24): the companion routing profile picks resolve_role's
    DEFAULT choice on a two-option role when the caller passes none -- an
    explicit choice always wins regardless of profile."""

    def _two_option_role(self):
        self.set_roles({"director": _role([_option("codex", "gpt-6-luna", "xhigh"),
                                           _option("claude", "claude-opus-5-5", "high")])})

    def _set_profile(self, routing_profile):
        current = settings.status(self.root)
        settings.update(self.root, 0, current["revision"], "routing",
                         {**current["routing"], "routing_profile": routing_profile})

    def test_cost_savings_defaults_to_secondary(self):
        self._two_option_role()
        self._set_profile("cost-savings")
        result = rr.resolve_role(self.root, "director")
        self.assertEqual(result["choice"], "secondary")
        self.assertEqual((result["harness"], result["model"]), ("claude", "claude-opus-5-5"))

    def test_high_quality_defaults_to_primary(self):
        self._two_option_role()
        self._set_profile("high-quality")
        result = rr.resolve_role(self.root, "director")
        self.assertEqual(result["choice"], "primary")
        self.assertEqual((result["harness"], result["model"]), ("codex", "gpt-6-luna"))

    def test_optimus_default_profile_still_defaults_to_primary(self):
        self._two_option_role()
        self._set_profile("optimus")
        self.assertEqual(rr.resolve_role(self.root, "director")["choice"], "primary")

    def test_no_companion_settings_at_all_still_defaults_to_primary(self):
        # Fresh project, no .eutonos-local/companion.json yet -- read_companion's own
        # default routing_profile ("optimus") applies, matching the pre-AWR7 default.
        self._two_option_role()
        self.assertEqual(rr.resolve_role(self.root, "director")["choice"], "primary")

    def test_explicit_choice_always_overrides_the_profile(self):
        self._two_option_role()
        self._set_profile("cost-savings")  # would default to secondary...
        result = rr.resolve_role(self.root, "director", choice="primary")  # ...but this wins
        self.assertEqual(result["choice"], "primary")

    def test_cost_savings_on_a_single_option_role_falls_back_to_primary(self):
        self.set_roles({"worker": _role([_option("claude", "claude-sonnet-5", None)])})
        self._set_profile("cost-savings")
        result = rr.resolve_role(self.root, "worker")
        self.assertEqual(result["choice"], "primary")  # only one option -- no secondary to prefer

    def test_a_corrupt_companion_settings_file_still_falls_back_to_primary(self):
        self._two_option_role()
        companion = self.root / ".eutonos-local" / "companion.json"
        companion.parent.mkdir(parents=True, exist_ok=True)
        companion.write_text("not valid json", encoding="utf-8")
        result = rr.resolve_role(self.root, "director")  # never raises out of a read-only resolve
        self.assertEqual(result["choice"], "primary")


class OpenRoleSameAppAndHermesTests(Base):
    def test_same_app_returns_without_creating_a_crosstalk_chat(self):
        self.set_roles({"worker": _role([_option("claude", "claude-sonnet-5", "medium")], autonomy="off")})
        with mock.patch.object(rr, "create_chat") as fake_send:
            result = rr.open_role(self.root, "/some/host", "worker", from_harness="claude", brief_text="do it")
        fake_send.assert_not_called()
        self.assertEqual(result["model_requested"], "claude-sonnet-5")
        self.assertEqual(result["model_selection"]["selection_kind"], "pin")
        legacy = {key: value for key, value in result.items() if key not in ("model_requested", "model_selection")}
        self.assertEqual(legacy, {"role": "worker", "choice": "primary", "harness": "claude",
                                  "model": "claude-sonnet-5", "effort": "medium", "host_directory": "/some/host",
                                  "same_app": True,
                                  "note": "use this app's own native subagent/session spawn; "
                                          "Crosstalk refuses same-app calls"})

    def test_hermes_role_raises_unsupported_and_never_sends(self):
        self.set_roles({"h": _role([_option("hermes", "whatever-model", None)], autonomy="auto-read-only")})
        with mock.patch.object(rr, "create_chat") as fake_send:
            with self.assertRaises(rr.RoleRouterError) as ctx:
                rr.open_role(self.root, None, "h", from_harness="claude", brief_text="x")
        self.assertEqual(ctx.exception.code, "unsupported")
        fake_send.assert_not_called()

    def test_exactly_one_of_brief_path_or_brief_text_required(self):
        self.set_roles({"worker": _role([_option("claude", "claude-sonnet-5", None)])})
        with self.assertRaises(rr.RoleRouterError) as ctx:
            rr.open_role(self.root, None, "worker", from_harness="claude")
        self.assertEqual(ctx.exception.code, "bad-brief")
        with self.assertRaises(rr.RoleRouterError) as ctx2:
            rr.open_role(self.root, None, "worker", from_harness="claude", brief_text="a", brief_path="b")
        self.assertEqual(ctx2.exception.code, "bad-brief")


class OpenRoleCrossAppAutonomyTests(Base):
    def test_autonomy_off_raises_and_never_sends(self):
        self.set_roles({"worker": _role([_option("codex", "gpt-6-sol", "medium")], autonomy="off")})
        with mock.patch.object(rr, "create_chat") as fake_send:
            with self.assertRaises(rr.RoleRouterError) as ctx:
                rr.open_role(self.root, None, "worker", from_harness="claude", brief_text="x")
        self.assertEqual(ctx.exception.code, "autonomy-off")
        fake_send.assert_not_called()

    def test_autonomy_ask_without_confirm_previews_and_never_sends(self):
        self.set_roles({"worker": _role([_option("codex", "gpt-6-sol", "medium")], autonomy="ask")})
        with mock.patch.object(rr, "create_chat") as fake_send:
            result = rr.open_role(self.root, None, "worker", from_harness="claude", brief_text="hello world")
        fake_send.assert_not_called()
        self.assertTrue(result["needs_confirmation"])
        self.assertIn("hello world", result["prompt_preview"])
        self.assertEqual((result["harness"], result["model"], result["effort"]), ("codex", "gpt-6-sol", "medium"))

    def test_autonomy_ask_with_confirm_creates_the_chat_through_crosstalk(self):
        self.set_roles({"worker": _role([_option("codex", "gpt-6-sol", "medium")], autonomy="ask")})
        with mock.patch.object(rr, "create_chat", return_value={"ok": True, "key": "codex:abc123"}) as fake_send:
            result = rr.open_role(self.root, None, "worker", from_harness="claude",
                                  brief_text="hello", confirm=True, timeout_seconds=900)
        fake_send.assert_called_once()
        kwargs = fake_send.call_args.kwargs
        self.assertEqual((kwargs["harness"], kwargs["model"], kwargs["effort"]),
                         ("codex", "gpt-6-sol", "medium"))
        # The folder defaults to the repository root inside create_chat.
        self.assertNotIn("cwd", kwargs)
        self.assertEqual(kwargs["timeout_seconds"], 900)
        self.assertIn("hello", kwargs["prompt"])
        self.assertEqual(result["key"], "codex:abc123")
        self.assertEqual(result["route"], "crosstalk")
        self.assertEqual(result["created"], {"ok": True, "key": "codex:abc123"})

    def test_legacy_auto_read_only_requires_confirmation(self):
        self.set_roles({"worker": _role([_option("codex", "gpt-6-sol", "medium")], autonomy="auto-read-only",
                                        preamble=None)})
        with mock.patch.object(rr, "create_chat", return_value={"ok": True, "key": "codex:xyz"}) as fake_send:
            result = rr.open_role(self.root, None, "worker", from_harness="claude", brief_text="go")
        fake_send.assert_not_called()
        self.assertTrue(result["needs_confirmation"])

    def test_brief_path_pointer_used_when_no_brief_text(self):
        self.set_roles({"worker": _role([_option("codex", "gpt-6-sol", "medium")], autonomy="auto-read-only")})
        with mock.patch.object(rr, "create_chat", return_value={"ok": True, "key": "codex:p1"}) as fake_send:
            rr.open_role(self.root, None, "worker", from_harness="claude", brief_path="handoffs/BRIEF.md", confirm=True)
        prompt = fake_send.call_args.kwargs["prompt"]
        self.assertIn("handoffs/BRIEF.md", prompt)
        self.assertNotIn("cwd", fake_send.call_args.kwargs)

    def test_cross_app_prompt_bakes_in_the_role_hooks_reminder(self):
        # Hooks rework (owner, 2026-09-28): a Crosstalk-created chat's own hooks do
        # not know which role it was made for, so _build_prompt bakes the role's
        # one reminder in (role_hooks.safe_reminder_text).
        # "worker" is not a named role in the built-in hook set, so it falls back
        # to that set's "default" entry -- still proves the wiring end to end.
        self.set_roles({"worker": _role([_option("codex", "gpt-6-sol", "medium")], autonomy="ask")})
        expected = settings.BUILTIN_HOOK_SETS[settings.BUILTIN_HOOK_SET_NAME]["roles"]["default"]["message"]
        with mock.patch.object(rr, "create_chat", return_value={"ok": True, "key": "codex:abc"}) as fake_send:
            rr.open_role(self.root, None, "worker", from_harness="claude", brief_text="hello", confirm=True)
        prompt = fake_send.call_args.kwargs["prompt"]
        self.assertIn(expected, prompt)
        self.assertIn("hello", prompt)

    def test_cross_app_prompt_carries_the_role_brief_template(self):
        # 2026-09-29: only the same-app prompt carried brief_template, so a
        # Codex discovery worker never saw the discovery template.
        role = {**_role([_option("codex", "gpt-6-sol", "medium")], autonomy="ask"),
                "brief_template": "TEMPLATE-LINE"}
        self.set_roles({"worker": role})
        with mock.patch.object(rr, "create_chat", return_value={"ok": True, "key": "codex:t1"}) as fake_send:
            preview = rr.open_role(self.root, None, "worker", from_harness="claude", brief_text="the brief")
            rr.open_role(self.root, None, "worker", from_harness="claude", brief_text="the brief", confirm=True)
        self.assertIn("TEMPLATE-LINE", preview["prompt_preview"])
        prompt = fake_send.call_args.kwargs["prompt"]
        self.assertLess(prompt.index("TEMPLATE-LINE"), prompt.index("the brief"))

    def test_ask_preview_keeps_task_and_delivery_qualification_with_long_inputs(self):
        template = "REQUESTED-TEMPLATE " + "t" * 1000
        brief = "REQUESTED-BRIEF " + "b" * 1000
        role = {**_role([_option("codex", "gpt-6-sol", "medium")], autonomy="ask"), "brief_template": template}
        self.set_roles({"worker": role})
        for mode, qualification in (("manual", "a milestone or remote grants none"),
                                    ("commit-only", "do not stage, commit or push independently"),
                                    ("commit+push", "do not stage, commit or push independently"),
                                    ("unavailable", "authorizes no commit, push or automatic submission")):
            with self.subTest(mode=mode), mock.patch("eutonos.git_delivery_native.guidance", return_value={"mode": mode}), \
                    mock.patch.object(rr, "create_chat", return_value={"ok": True, "key": "codex:t1"}) as send:
                result = rr.open_role(self.root, None, "worker", from_harness="claude", brief_text=brief)
                send.assert_not_called()
                text = result["prompt_preview"]
                self.assertLessEqual(len(text), rr.PREVIEW_CHARS)
                self.assertIn("REQUESTED-TEMPLATE", text)
                self.assertIn("REQUESTED-BRIEF", text)
                self.assertIn(qualification, text)
                rr.open_role(self.root, None, "worker", from_harness="claude", brief_text=brief, confirm=True)
                prompt = send.call_args.kwargs["prompt"]
                self.assertIn(template, prompt)
                self.assertIn(brief, prompt)
                self.assertIn(qualification, prompt)


class OpenRoleEnforcementTests(Base):
    """R3/R4/R5 (AWR-FR): the router-enforceable slice of autonomy/harness checks,
    and launch-root sharing with a linked worktree's settings root."""

    def test_non_bool_confirm_is_refused_even_though_it_is_truthy(self):
        self.set_roles({"worker": _role([_option("codex", "gpt-6-sol", "medium")], autonomy="ask")})
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(rr, "create_chat") as fake_send:
            with self.assertRaises(rr.RoleRouterError) as ctx:
                rr.open_role(self.root, None, "worker", from_harness="claude",
                             brief_text="x", confirm="yes")
        self.assertEqual(ctx.exception.code, "bad-confirm")
        fake_send.assert_not_called()

    def test_declared_harness_mismatching_claudecode_marker_is_refused(self):
        self.set_roles({"worker": _role([_option("codex", "gpt-6-sol", "medium")], autonomy="ask")})
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}, clear=True), \
             mock.patch.object(rr, "create_chat") as fake_send:
            with self.assertRaises(rr.RoleRouterError) as ctx:
                rr.open_role(self.root, None, "worker", from_harness="codex", brief_text="x")
        self.assertEqual(ctx.exception.code, "harness-mismatch")
        fake_send.assert_not_called()

    def test_declared_harness_matching_claudecode_marker_is_allowed(self):
        self.set_roles({"worker": _role([_option("codex", "gpt-6-sol", "medium")], autonomy="ask")})
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}, clear=True), \
             mock.patch.object(rr, "create_chat") as fake_send:
            result = rr.open_role(self.root, None, "worker", from_harness="claude", brief_text="x")
        self.assertTrue(result["needs_confirmation"])
        fake_send.assert_not_called()

    def test_no_marker_set_does_not_refuse_any_declared_harness(self):
        self.set_roles({"worker": _role([_option("codex", "gpt-6-sol", "medium")], autonomy="ask")})
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(rr, "create_chat") as fake_send:
            result = rr.open_role(self.root, None, "worker", from_harness="claude", brief_text="x")
        self.assertTrue(result["needs_confirmation"])
        fake_send.assert_not_called()

    def test_launch_root_falls_back_to_the_given_root_outside_a_git_worktree(self):
        # No real .git here, so settings._resolve_shared_root's own fallback applies:
        # this exercises the real (unmocked) delegation, unlike the Crosstalk test below.
        self.assertEqual(rr._launch_root(self.root), str(Path(self.root).resolve()))


class StartTests(Base):
    """AWR worker profiles: ``start`` is the one entry point either app uses to
    launch a role, by name or by task (see role_router.py's WORKER PROFILES
    docstring section)."""

    def set_task_rules(self, **rules):
        current = settings.status(self.root)
        merged = dict(current["task_rules"], **rules)
        settings.update(self.root, 0, current["revision"], "task_rules", merged)

    def test_orchestrator_is_always_refused_by_name(self):
        with mock.patch.object(rr, "create_chat") as fake_send:
            with self.assertRaises(rr.RoleRouterError) as ctx:
                rr.start(self.root, None, "orchestrator", from_harness="claude", brief_text="x")
        self.assertEqual(ctx.exception.code, "orchestrator-not-launchable")
        fake_send.assert_not_called()

    def test_unassigned_task_resolving_to_orchestrator_is_refused(self):
        # "closeout" defaults to "orchestrator" until explicitly reassigned
        # (settings.DEFAULT_TASK_RULES, AWR worker profiles coordinator review
        # 2026-09-28: unlike most tasks, closeout's own default IS orchestrator).
        self.assertEqual(settings.DEFAULT_TASK_RULES["closeout"], "orchestrator")
        with mock.patch.object(rr, "create_chat") as fake_send:
            with self.assertRaises(rr.RoleRouterError) as ctx:
                rr.start(self.root, None, task="closeout", from_harness="claude", brief_text="x")
        self.assertEqual(ctx.exception.code, "orchestrator-not-launchable")
        fake_send.assert_not_called()

    def test_unknown_task_is_refused(self):
        with self.assertRaises(rr.RoleRouterError) as ctx:
            rr.start(self.root, None, task="not-a-task", from_harness="claude", brief_text="x")
        self.assertEqual(ctx.exception.code, "unknown-task")

    def test_exactly_one_of_role_or_task_is_required(self):
        with self.assertRaises(rr.RoleRouterError) as ctx:
            rr.start(self.root, None, from_harness="claude", brief_text="x")
        self.assertEqual(ctx.exception.code, "bad-start")
        with self.assertRaises(rr.RoleRouterError) as ctx2:
            rr.start(self.root, None, "worker", task="discovery", from_harness="claude", brief_text="x")
        self.assertEqual(ctx2.exception.code, "bad-start")

    def test_same_app_by_role_name_returns_spawn_json_without_creating_a_chat(self):
        self.set_roles({"worker": _role([_option("claude", "claude-sonnet-5", "medium")], autonomy="off")})
        with mock.patch.object(rr, "create_chat") as fake_send:
            result = rr.start(self.root, None, "worker", from_harness="claude", brief_text="do the thing")
        fake_send.assert_not_called()
        self.assertEqual(result["same_app"], True)
        self.assertEqual((result["harness"], result["model"], result["effort"]),
                         ("claude", "claude-sonnet-5", "medium"))
        self.assertEqual(result["edits"], "ask")  # default when the test's bare _role() omits it
        self.assertIn("do the thing", result["prompt"])
        self.assertIsNone(result["task"])

    def test_same_app_by_task_resolves_the_mapped_role(self):
        self.set_roles({"worker-routine": _role([_option("claude", "claude-sonnet-5", "medium")])})
        self.set_task_rules(coding="worker-routine")
        with mock.patch.object(rr, "create_chat") as fake_send:
            result = rr.start(self.root, None, task="coding", from_harness="claude", brief_text="go")
        fake_send.assert_not_called()
        self.assertEqual(result["role"], "worker-routine")
        self.assertEqual(result["task"], "coding")
        self.assertTrue(result["same_app"])

    def test_same_app_codex_spawn_bakes_in_a_role_hooks_reminder_fallback(self):
        """Finding 1 (Codex verification, 2026-09-28): a Codex same-app spawn's
        prompt bakes in a short role-hooks reminder as a fallback, since a
        live Codex child's own ordinary hook events are not yet proven to
        reliably carry agent_id (see role_hooks's module docstring, "CODEX
        CHILD IDENTITY"). Claude's own same-app spawn is unaffected -- it
        still relies solely on its live per-turn hook injection."""
        self.set_roles({"worker-routine": _role([_option("codex", "gpt-6-luna", "xhigh")], autonomy="off")})
        with mock.patch.object(rr, "create_chat") as fake_send:
            result = rr.start(self.root, None, "worker-routine", from_harness="codex", brief_text="do the thing")
        fake_send.assert_not_called()
        self.assertTrue(result["same_app"])
        expected = rr.role_hooks.safe_reminder_text(self.root, "worker-routine", "message")
        self.assertTrue(expected)
        self.assertIn(expected, result["prompt"])
        self.assertIn("do the thing", result["prompt"])

    def test_same_app_claude_spawn_has_no_baked_in_reminder(self):
        self.set_roles({"worker": _role([_option("claude", "claude-sonnet-5", "medium")], autonomy="off")})
        with mock.patch.object(rr, "create_chat") as fake_send:
            result = rr.start(self.root, None, "worker", from_harness="claude", brief_text="do the thing")
        fake_send.assert_not_called()
        expected = rr.role_hooks.safe_reminder_text(self.root, "worker", "message")
        self.assertTrue(expected)  # the text exists, but this same-app Claude spawn must not bake it in
        self.assertNotIn(expected, result["prompt"])

    def test_cross_app_by_role_name_creates_through_crosstalk_like_open_role(self):
        self.set_roles({"worker": _role([_option("codex", "gpt-6-sol", "medium")], autonomy="ask")})
        with mock.patch.object(rr, "create_chat", return_value={"ok": True, "key": "codex:s1"}) as fake_send:
            result = rr.start(self.root, None, "worker", from_harness="claude",
                              brief_text="hello", confirm=True)
        fake_send.assert_called_once()
        kwargs = fake_send.call_args.kwargs
        self.assertEqual((kwargs["harness"], kwargs["title"]), ("codex", "EUTONOS worker"))
        self.assertEqual(result["key"], "codex:s1")
        self.assertIsNone(result["task"])
        self.assertNotIn("same_app", result)

    def test_explicit_role_name_bypasses_autonomy_off_but_still_needs_confirm(self):
        """Requirement 4 (coordinator review, 2026-09-28): start() called with
        an explicit ROLE NAME is an explicit act by the caller, so a cross-app
        autonomy of "off" no longer blocks it outright -- it is treated like
        "ask": a preview unless confirm=True, never an outright refusal."""
        self.set_roles({"worker": _role([_option("codex", "gpt-6-sol", "medium")], autonomy="off")})
        with mock.patch.object(rr, "create_chat") as fake_send:
            result = rr.start(self.root, None, "worker", from_harness="claude", brief_text="x")
        self.assertTrue(result["needs_confirmation"])
        fake_send.assert_not_called()
        with mock.patch.object(rr, "create_chat", return_value={"ok": True, "key": "codex:s2"}) as fake_send:
            confirmed = rr.start(self.root, None, "worker", from_harness="claude",
                                 brief_text="x", confirm=True)
        fake_send.assert_called_once()
        self.assertEqual(confirmed["key"], "codex:s2")

    def test_task_based_automatic_call_still_honors_autonomy_off(self):
        """Calling start() with --task/task= is AUTOMATIC dispatch (the caller
        picked a task, not a specific role), so it keeps raising autonomy-off
        for a cross-app call exactly as before requirement 4 (coordinator
        review, 2026-09-28) -- only an explicit role-name call gets the
        softened, ask-style treatment (see the test above)."""
        self.set_roles({"worker": _role([_option("codex", "gpt-6-sol", "medium")], autonomy="off")})
        self.set_task_rules(coding="worker")
        with mock.patch.object(rr, "create_chat") as fake_send:
            with self.assertRaises(rr.RoleRouterError) as ctx:
                rr.start(self.root, None, task="coding", from_harness="claude", brief_text="x")
        self.assertEqual(ctx.exception.code, "autonomy-off")
        fake_send.assert_not_called()

    def test_claude_haiku_option_is_refused_for_both_start_paths_and_open(self):
        # New worker saves refuse Haiku before dispatch. This still qualifies
        # the launch fence for a real configuration saved before that policy.
        legacy = settings._local(self.root)
        legacy["roles"] = {"h": _role([_option("claude", rr.HAIKU_MODEL, "medium")], autonomy="ask")}
        path = self.root / ".eutonos-local" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(legacy), encoding="utf-8")
        # Clear the harness markers so a run inside Claude Code does not trip
        # harness-mismatch before the Haiku refusal for the codex caller.
        with mock.patch.dict(os.environ, {m: "" for m in rr.HARNESS_ENV_MARKERS.values()}),                 mock.patch.object(rr, "create_chat") as fake_send:
            for caller, expected in (("codex", "haiku-cross-app-refused"),
                                     ("claude", "haiku-refused")):
                for launch in (rr.start, rr.open_role):
                    with self.subTest(caller=caller, launch=launch.__name__), \
                            self.assertRaises(rr.RoleRouterError) as ctx:
                        launch(self.root, None, "h", from_harness=caller, brief_text="x")
                    self.assertEqual(ctx.exception.code, expected)
        fake_send.assert_not_called()

    def test_new_resolved_older_haiku_id_is_also_refused(self):
        self.set_roles({"h": _role([_option("claude", "claude-sonnet-5", "medium")])})
        original = rr.resolve_role

        def future_haiku(*args, **kwargs):
            return dict(original(*args, **kwargs), model="claude-haiku-4-5-20251001")

        with mock.patch.object(rr, "resolve_role", side_effect=future_haiku), \
                mock.patch.object(rr, "create_chat") as fake_send:
            with self.assertRaises(rr.RoleRouterError) as ctx:
                rr.start(self.root, None, "h", from_harness="claude", brief_text="x")
        self.assertEqual(ctx.exception.code, "haiku-refused")
        fake_send.assert_not_called()

    def test_haiku_5_5_resolved_option_is_accepted_and_older_haiku_still_refused(self):
        self.set_roles({"h": _role([_option("claude", "claude-sonnet-5", "medium")])})
        original = rr.resolve_role
        for model, refused in (("claude-haiku-5-5", False), ("claude-haiku-6", False),
                               ("claude-haiku-4-5", True), ("claude-haiku-4-5-20251001", True),
                               ("claude-3-5-sonnet-20241022", True)):
            def resolved(*args, _model=model, **kwargs):
                return dict(original(*args, **kwargs), model=_model)
            with self.subTest(model=model), mock.patch.object(rr, "resolve_role", side_effect=resolved):
                if refused:
                    with self.assertRaises(rr.RoleRouterError) as ctx:
                        rr.start(self.root, None, "h", from_harness="claude", brief_text="x")
                    self.assertEqual(ctx.exception.code, "haiku-refused")
                    self.assertIn("cannot run auto mode", str(ctx.exception))
                else:
                    result = rr.start(self.root, None, "h", from_harness="claude", brief_text="x")
                    self.assertEqual(result["model"], model)

    def test_brief_required_exactly_once_for_a_same_app_call(self):
        self.set_roles({"worker": _role([_option("claude", "claude-sonnet-5", None)])})
        with self.assertRaises(rr.RoleRouterError) as ctx:
            rr.start(self.root, None, "worker", from_harness="claude")
        self.assertEqual(ctx.exception.code, "bad-brief")


class BriefTemplateDefaultTests(Base):
    """Owner decision (2026-09-29): a saved role's empty/missing brief_template
    must resolve to that role's built-in default at read/resolve time, on both
    the same-app (start's same_app branch, _assemble_role_prompt) and cross-app
    (open_role/start, _build_prompt) launch paths -- this project's seven saved
    roles all have an empty saved template today, so without this the new
    default templates (worker/discovery) never reach a live launch."""

    def test_resolve_role_falls_back_to_default_when_saved_template_is_empty(self):
        self.set_roles({"worker-hard": _role([_option("claude", "claude-sonnet-5", "medium")],
                                             autonomy="ask")
                        | {"brief_template": ""}})
        resolved = rr.resolve_role(self.root, "worker-hard")
        self.assertEqual(resolved["brief_template"], settings._WORKER_BRIEF_TEMPLATE)

    def test_resolve_role_keeps_a_non_empty_saved_template_unchanged(self):
        self.set_roles({"worker-hard": _role([_option("claude", "claude-sonnet-5", "medium")],
                                             autonomy="ask")
                        | {"brief_template": "MY OWN TEMPLATE"}})
        resolved = rr.resolve_role(self.root, "worker-hard")
        self.assertEqual(resolved["brief_template"], "MY OWN TEMPLATE")

    def test_same_app_launch_path_carries_the_default_template(self):
        self.set_roles({"worker-hard": _role([_option("claude", "claude-sonnet-5", "medium")],
                                             autonomy="off")
                        | {"brief_template": ""}})
        with mock.patch.object(rr, "create_chat") as fake_send:
            result = rr.start(self.root, None, "worker-hard", from_harness="claude", brief_text="do it")
        fake_send.assert_not_called()
        self.assertTrue(result["same_app"])
        self.assertIn(settings._WORKER_BRIEF_TEMPLATE, result["prompt"])

    def test_cross_app_launch_path_carries_the_default_template(self):
        self.set_roles({"worker-hard": _role([_option("codex", "gpt-6-sol", "medium")], autonomy="ask")
                        | {"brief_template": None}})
        with mock.patch.object(rr, "create_chat", return_value={"ok": True, "key": "codex:d1"}) as fake_send:
            rr.start(self.root, None, "worker-hard", from_harness="claude", brief_text="do it", confirm=True)
        prompt = fake_send.call_args.kwargs["prompt"]
        self.assertIn(settings._WORKER_BRIEF_TEMPLATE, prompt)

    def test_a_non_empty_saved_template_wins_on_both_launch_paths(self):
        custom = "CUSTOM TEMPLATE LINE"
        self.set_roles({"worker-hard": _role([_option("claude", "claude-sonnet-5", "medium")],
                                             autonomy="off")
                        | {"brief_template": custom}})
        with mock.patch.object(rr, "create_chat"):
            same_app = rr.start(self.root, None, "worker-hard", from_harness="claude", brief_text="x")
        self.assertIn(custom, same_app["prompt"])
        self.assertNotIn(settings._WORKER_BRIEF_TEMPLATE, same_app["prompt"])
        self.set_roles({"worker-hard2": _role([_option("codex", "gpt-6-sol", "medium")], autonomy="ask")
                        | {"brief_template": custom}})
        with mock.patch.object(rr, "create_chat", return_value={"ok": True, "key": "codex:d2"}) as fake_send:
            rr.start(self.root, None, "worker-hard2", from_harness="claude", brief_text="x", confirm=True)
        prompt = fake_send.call_args.kwargs["prompt"]
        self.assertIn(custom, prompt)
        self.assertNotIn(settings._WORKER_BRIEF_TEMPLATE, prompt)

    def test_a_role_name_with_no_built_in_default_keeps_an_empty_template(self):
        # "reviewer" is a DEFAULT_ROLES name but has no built-in brief_template.
        self.set_roles({"reviewer": _role([_option("claude", "claude-sonnet-5", "medium")], autonomy="off")
                        | {"brief_template": ""}})
        resolved = rr.resolve_role(self.root, "reviewer")
        self.assertIsNone(resolved["brief_template"])

    def test_an_old_saved_roles_file_without_a_brief_template_key_still_reads(self):
        """A settings file saved before brief_template existed at all (the key
        is simply absent from the dict) must still read and resolve, falling
        back to the default exactly like an explicit empty/None value does."""
        self._write_local(roles={"worker-hard": {
            "options": [{"harness": "claude", "model": "claude-sonnet-5", "effort": "medium"}],
            "preamble": None, "autonomy": "off", "description": None, "edits": "ask"}})
        resolved = rr.resolve_role(self.root, "worker-hard")
        self.assertEqual(resolved["brief_template"], settings._WORKER_BRIEF_TEMPLATE)

    def _write_local(self, **overrides):
        # Mirrors test_settings.py's SettingsTests._write_local helper: write a
        # raw local settings.json file directly, bypassing validate_roles, to
        # simulate a file saved before a schema field (here, brief_template)
        # existed at all.
        path = self.root / ".eutonos-local" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {"profile": settings.PROFILE,
                 "task_rules": {task: "orchestrator" for task in settings.TASKS},
                 "ram": {"retrieval": "on-demand", "capture": True, "cadence": "every-turn", "detail": "summary"},
                 "roles": {}, "role_defaults_seeded": [],
                 "hooks": json.loads(json.dumps(settings.DEFAULT_HOOKS))}
        value.update(overrides)
        path.write_text(json.dumps(value), encoding="utf-8")


class SendExplicitTests(Base):
    def test_explicit_call_creates_a_chat_and_ignores_roles_autonomy(self):
        # a role named "worker" exists with autonomy off, but send_explicit takes no role
        # at all and must not be affected by it.
        self.set_roles({"worker": _role([_option("codex", "gpt-6-sol", "medium")], autonomy="off")})
        with mock.patch.object(rr, "create_chat", return_value={"ok": True, "key": "codex:e1"}) as fake_create:
            result = rr.send_explicit(self.root, to="codex", sender="claude", prompt="direct call",
                                      model="gpt-6-sol", effort="high", title="T")
        fake_create.assert_called_once_with(self.root, harness="codex", model="gpt-6-sol", effort="high",
                                            prompt="direct call", title="T", cwd=None, timeout_seconds=None)
        self.assertEqual(result["key"], "codex:e1")
        self.assertEqual(result["route"], "crosstalk")

    def test_only_mode_new_exists_and_never_reaches_the_legacy_mailbox(self):
        with mock.patch.object(rr, "create_chat") as fake_create:
            for mode in ("resume", "queue", "deliver"):
                with self.subTest(mode=mode), self.assertRaises(rr.RoleRouterError) as ctx:
                    rr.send_explicit(self.root, to="codex", sender="claude", prompt="x", mode=mode,
                                     model="gpt-6-sol", effort="high")
                self.assertEqual(ctx.exception.code, "mode-unsupported")
        fake_create.assert_not_called()
        self.assertNotIn("mailbox", vars(rr))

    def test_same_app_and_hermes_targets_are_refused(self):
        with mock.patch.object(rr, "create_chat") as fake_create:
            with self.assertRaises(rr.RoleRouterError) as same:
                rr.send_explicit(self.root, to="claude", sender="claude", prompt="x",
                                 model="claude-sonnet-5", effort="high")
            with self.assertRaises(rr.RoleRouterError) as hermes:
                rr.send_explicit(self.root, to="hermes", sender="claude", prompt="x",
                                 model="m", effort="high")
        self.assertEqual((same.exception.code, hermes.exception.code), ("same-app", "unsupported"))
        fake_create.assert_not_called()


class CreateChatTests(Base):
    """``create_chat`` runs the Crosstalk CLI through ``_run_crosstalk_cli`` only; the
    tests replace that one function, so no process starts."""

    def setUp(self):
        super().setUp()
        self.cli = self.root / "tools" / "crosstalk" / "crosstalk_p0_cli.py"
        self.cli.parent.mkdir(parents=True)
        self.cli.write_text("# stand-in\n", encoding="utf-8")
        self.device = self.root / "device"
        self.device.mkdir()
        env = mock.patch.dict(os.environ, {crosstalk_status.DEVICE_DIR_ENV: str(self.device)})
        env.start()
        self.addCleanup(env.stop)

    def controls(self, **value):
        (self.device / "controls.json").write_text(json.dumps(value), encoding="utf-8")

    def create(self, *, harness="codex", effort="xhigh", model="gpt-6-luna", prompt="Do the job.", **extra):
        return rr.create_chat(self.root, harness=harness, model=model, effort=effort, prompt=prompt, **extra)

    def test_codex_target_runs_create_codex_with_exact_arguments_and_a_prompt_file(self):
        self.controls(messaging="on")
        seen = {}

        def fake_run(argv, timeout):
            seen["argv"], seen["timeout"] = list(argv), timeout
            seen["prompt"] = Path(argv[argv.index("--prompt-file") + 1]).read_text(encoding="utf-8")
            return 0, 'noise\n{"ok": true, "key": "codex:t1", "thread_id": "t1", "tag": "x"}\n'

        with mock.patch.object(rr, "_run_crosstalk_cli", side_effect=fake_run):
            result = self.create(title="EUTONOS worker", timeout_seconds=77, prompt="Zebra \u00e9 prompt")
        argv = seen["argv"]
        self.assertEqual(argv[1:4], ["-I", "-B", str(self.cli)])
        self.assertEqual(argv[4], "create-codex")
        for flag, value in (("--folder", str(Path(self.root).resolve())), ("--model", "gpt-6-luna"),
                            ("--effort", "xhigh"), ("--title", "EUTONOS worker")):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertEqual((seen["timeout"], seen["prompt"]), (77, "Zebra \u00e9 prompt"))
        self.assertFalse(Path(argv[argv.index("--prompt-file") + 1]).exists())  # temp file removed
        self.assertEqual(result["key"], "codex:t1")

    def test_claude_target_runs_create_claude(self):
        self.controls(messaging="on")
        with mock.patch.object(rr, "_run_crosstalk_cli", return_value=(0, '{"ok": true, "key": "claude:s"}\n')) as run:
            self.create(harness="claude", model="claude-sonnet-5", effort="high")
        self.assertEqual(run.call_args.args[0][4], "create-claude")

    def test_folder_is_the_shared_launch_root(self):
        self.controls(messaging="on")
        shared = self.root.parent / "shared-main-checkout"
        shared.mkdir()
        (shared / "tools" / "crosstalk").mkdir(parents=True)
        (shared / "tools" / "crosstalk" / "crosstalk_p0_cli.py").write_text("#\n", encoding="utf-8")
        with mock.patch.object(rr, "_launch_root", return_value=str(shared)), \
                mock.patch.object(rr, "_run_crosstalk_cli", return_value=(0, '{"ok": true}\n')) as run:
            self.create()
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--folder") + 1], str(shared))
        self.assertEqual(argv[3], str(shared / "tools" / "crosstalk" / "crosstalk_p0_cli.py"))

    def test_unavailable_crosstalk_refuses_visibly_and_runs_nothing(self):
        cases = (("crosstalk-messaging-off", None), ("crosstalk-messaging-off", {"messaging": "off"}),
                 ("crosstalk-halted", {"messaging": "on", "halt": True}))
        for code, value in cases:
            with self.subTest(code=code, value=value), mock.patch.object(rr, "_run_crosstalk_cli") as run:
                if value is not None:
                    self.controls(**value)
                with self.assertRaises(rr.RoleRouterError) as ctx:
                    self.create()
                self.assertEqual(ctx.exception.code, code)
                run.assert_not_called()
        with mock.patch.dict(os.environ, {crosstalk_status.DEVICE_DIR_ENV: str(self.root / "absent")}), \
                mock.patch.object(rr, "_run_crosstalk_cli") as run:
            with self.assertRaises(rr.RoleRouterError) as ctx:
                self.create()
        self.assertEqual(ctx.exception.code, "crosstalk-not-installed")
        run.assert_not_called()

    def test_missing_cli_in_the_checkout_refuses(self):
        self.controls(messaging="on")
        self.cli.unlink()
        with mock.patch.object(rr, "_run_crosstalk_cli") as run, self.assertRaises(rr.RoleRouterError) as ctx:
            self.create()
        self.assertEqual(ctx.exception.code, "crosstalk-cli-missing")
        run.assert_not_called()

    def test_effort_model_target_and_prompt_are_checked_before_anything_runs(self):
        self.controls(messaging="on")
        bad = (({"effort": None}, "effort-required"), ({"effort": "ultra"}, "effort-unsupported"),
               ({"model": ""}, "model-required"), ({"prompt": "  "}, "bad-brief"),
               ({"harness": "hermes"}, "unsupported"))
        with mock.patch.object(rr, "_run_crosstalk_cli") as run:
            for kwargs, code in bad:
                with self.subTest(code=code), self.assertRaises(rr.RoleRouterError) as ctx:
                    self.create(**kwargs)
                self.assertEqual(ctx.exception.code, code)
        run.assert_not_called()

    def test_failure_after_thread_creation_names_the_thread_and_is_not_retried(self):
        self.controls(messaging="on")
        failure = {"ok": False, "reason": "create-turn-timeout", "detail": "thread_id=T9 no turn", "thread_id": "T9"}
        with mock.patch.object(rr, "_run_crosstalk_cli", return_value=(1, json.dumps(failure) + "\n")) as run, \
                self.assertRaises(rr.RoleRouterError) as ctx:
            self.create()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(ctx.exception.code, "crosstalk-create-failed")
        self.assertIn("T9", ctx.exception.message)
        self.assertEqual(ctx.exception.details["crosstalk"]["thread_id"], "T9")

    def test_timeout_no_output_and_missing_python_are_distinct_visible_errors(self):
        self.controls(messaging="on")
        import subprocess
        for effect, code in ((subprocess.TimeoutExpired("x", 1), "crosstalk-create-timeout"),
                             (OSError("nope"), "crosstalk-cli-unavailable")):
            with self.subTest(code=code), mock.patch.object(rr, "_run_crosstalk_cli", side_effect=effect), \
                    self.assertRaises(rr.RoleRouterError) as ctx:
                self.create()
            self.assertEqual(ctx.exception.code, code)
        with mock.patch.object(rr, "_run_crosstalk_cli", return_value=(0, "not json\n")), \
                self.assertRaises(rr.RoleRouterError) as ctx:
            self.create()
        self.assertEqual(ctx.exception.code, "crosstalk-no-result")

    def test_open_role_end_to_end_uses_the_resolved_exact_model(self):
        self.controls(messaging="on")
        self.set_roles({"worker": _role([_option("codex", "gpt-6-sol", "medium")], autonomy="ask")})
        with mock.patch.object(rr, "_run_crosstalk_cli", return_value=(0, '{"ok": true, "key": "codex:z"}\n')) as run:
            result = rr.open_role(self.root, None, "worker", from_harness="claude", brief_text="Job", confirm=True)
        argv = run.call_args.args[0]
        self.assertEqual((argv[4], argv[argv.index("--model") + 1], argv[argv.index("--effort") + 1]),
                         ("create-codex", "gpt-6-sol", "medium"))
        self.assertEqual(result["key"], "codex:z")


class NoLegacyMailboxTests(unittest.TestCase):
    def test_module_does_not_import_the_legacy_mailbox(self):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(rr))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[-1] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[-1])
                imported.update(alias.name for alias in node.names)
        self.assertNotIn("mailbox", imported)


if __name__ == "__main__":
    unittest.main()
