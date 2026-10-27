"""Model choices retain intent across native spawn, routing and durable evidence."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from eutonos import companion_attribution, companion_control, companion_quality
from eutonos import model_catalog, role_router, settings, settings_cli, tokenslang as ts
from test_companion_attribution import assignment, receipt


class ModelPolicyConsumersTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "codex-home"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home),
                                                "EUTONOS_HOME": str(self.root / "eutonos-home")}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        self.cache()

    def cache(self, latest="gpt-6.1-sol"):
        rows = [{"slug": model, "visibility": "list", "supported_reasoning_levels":
                 [{"effort": effort} for effort in ("low", "medium", "high")]} for model in
                ("gpt-6-sol", latest, "gpt-6-luna", "gpt-6-astra")]
        (self.home / "models_cache.json").write_text(json.dumps({"models": rows}), encoding="utf-8")

    def role(self, model="sol"):
        roles = {"worker": {"options": [{"harness": "codex", "model": model, "effort": "high"}],
                            "preamble": None, "autonomy": "ask", "edits": "yes"}}
        settings.update_roles(self.root, roles, expected_revision=settings.read_roles(self.root)["revision"])

    def routing(self, allowed):
        state = settings.status(self.root)
        settings.update(self.root, 0, state["revision"], "routing", {
            **state["routing"], "enabled": True, "allowed_models": allowed})
        state = settings.status(self.root)
        settings.update(self.root, 0, state["revision"], "task_rules", {
            **state["task_rules"], "coding": "worker"})

    def test_native_new_spawn_resolves_family_and_retains_selection(self):
        self.role()
        old = role_router.start(self.root, None, "worker", from_harness="codex", brief_text="Bounded job")
        self.assertEqual((old["model_requested"], old["model"]), ("sol", "gpt-6.1-sol"))
        self.assertEqual(old["model_selection"]["selection_kind"], "family")
        self.cache("gpt-6.2-sol")
        new = role_router.start(self.root, None, "worker", from_harness="codex", brief_text="Next job")
        self.assertEqual(new["model"], "gpt-6.2-sol")
        self.assertEqual(old["model"], "gpt-6.1-sol")

    def test_native_spawn_keeps_exact_pin_after_family_moves(self):
        self.role("gpt-6-sol")
        self.cache("gpt-6.2-sol")
        result = role_router.start(self.root, None, "worker", from_harness="codex", brief_text="Pinned job")
        self.assertEqual(result["model"], "gpt-6-sol")
        self.assertEqual(result["model_selection"]["selection_kind"], "pin")

    def test_cross_app_creates_the_chat_with_the_exact_resolved_model(self):
        # Crosstalk takes an exact model id, so the family choice "sol" is resolved first
        # and the retained selection still reports the requested family.
        self.role()
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home),
                                          "EUTONOS_HOME": str(self.root / "eutonos-home")}, clear=True), \
                mock.patch.object(role_router, "create_chat", return_value={"ok": True, "key": "codex:t"}) as create:
            result = role_router.open_role(self.root, None, "worker", from_harness="claude", brief_text="Job", confirm=True)
        self.assertEqual(create.call_args.kwargs["model"], "gpt-6.1-sol")
        self.assertEqual(create.call_args.kwargs["harness"], "codex")
        self.assertEqual(result["model"], "gpt-6.1-sol")
        self.assertEqual(result["model_requested"], "sol")

    def test_family_eligibility_does_not_widen_an_exact_allowed_pin(self):
        self.role()
        self.routing(["sol", "gpt-6-luna"])
        self.assertEqual(settings_cli.route(self.root, "coding", "gpt-6-astra")["model"], "gpt-6.1-sol")
        self.routing(["gpt-6-sol", "gpt-6-luna"])
        result = settings_cli.route(self.root, "coding", "gpt-6-astra")
        self.assertEqual(result["basis"], "task-role-model-not-eligible")

    def test_escalation_policy_is_independent_of_catalog_order(self):
        catalog = model_catalog.describe_catalog()
        catalog["models"] = dict(reversed(list(catalog["models"].items())))
        with mock.patch.object(model_catalog, "describe_catalog", return_value=catalog):
            self.assertEqual(companion_control.routing_decision("gpt-6-sol", "gpt-6.1-sol")["outcome"], "selected")
            self.assertEqual(companion_control.routing_decision("gpt-6-luna", "gpt-6.1-sol")["outcome"], "approval-required")
            self.assertEqual(companion_control.routing_decision("gpt-6-astra", "gpt-6.1-sol")["outcome"], "selected")

    def test_observed_new_exact_identity_survives_without_catalog_membership(self):
        value = receipt(assignment(), worker_model="gpt-7.2-sol", effort="high")
        normalized = companion_attribution.normalize_receipt(value)
        self.assertEqual(normalized["observed_worker"]["model"], "gpt-7.2-sol")
        value["observed_worker"]["model"] = "sol"
        with self.assertRaises(ts.ReadError):
            companion_attribution.normalize_receipt(value)

    def test_running_primary_can_route_when_its_old_pin_leaves_catalog(self):
        # The primary's model is evidence, not a request to start it again.
        result = companion_control.routing_decision("gpt-5.6-sol", "gpt-6.1-sol")
        self.assertEqual(result["outcome"], "selected")
        with self.assertRaises(ValueError):
            companion_control.routing_decision("gpt-6-sol", "gpt-5.6-sol")

    def test_reported_claude_pin_can_be_saved_and_resolved_but_unranked_move_needs_approval(self):
        path = self.root / "eutonos-home" / "model-watch" / "claude-models.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"profile": "eutonos.claude-model-report.v1",
                                   "reported_on": "2026-09-29", "source": "Native listing",
                                   "models": [{"id": "claude-fable-5-1",
                                               "supported_efforts": ["medium", "high"]}]}), encoding="utf-8")
        role = {"new-model": {"options": [{"harness": "claude", "model": "claude-fable-5-1", "effort": "high"}],
                              "preamble": None, "autonomy": "ask"}}
        settings.update_roles(self.root, role, expected_revision=settings.read_roles(self.root)["revision"])
        self.assertEqual(role_router.resolve_role(self.root, "new-model")["model"], "claude-fable-5-1")
        result = companion_control.routing_decision("gpt-6-sol", "claude-fable-5-1", root=self.root)
        self.assertEqual(result["outcome"], "approval-required")
        selected = companion_control.routing_decision("gpt-6-sol", "claude-fable-5-1", root=self.root, user_selected=True)
        self.assertEqual(selected["outcome"], "selected")

    def test_historical_experiment_pins_are_facts_not_available_launches(self):
        experiment = {"id": "x", "label": "Historical", "task_id": "task", "task_brief": "Compare",
                      "mode": "on", "primary_model": "gpt-5.6-sol", "support_model": "gpt-5.6-luna",
                      "source_revision": "old", "source_fingerprint": None, "conditions_id": "pinned"}
        self.assertEqual(companion_quality.validate_experiment(experiment)["support_model"], "gpt-5.6-luna")
