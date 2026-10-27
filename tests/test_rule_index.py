"""Reference ownership and reading semantics."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from eutonos import rule_index

ROOT = Path(__file__).resolve().parents[1]


class RuleIndexTests(unittest.TestCase):
    def test_all_references_bind_existing_canonical_owners(self):
        index = rule_index.load()
        registry = {row["id"]: row["path"] for row in
                    json.loads((ROOT / "docs/REGISTRY.json").read_text())["documents"]}
        for row in index["rules"]:
            owner = row["owner"]
            if owner["scope"] == "upstream-reference":
                self.assertEqual(registry[owner["resource_id"]], owner["path"])
                self.assertTrue((ROOT / owner["path"]).is_file())

    def test_entry_onboarding_and_optional_experience_are_distinct(self):
        rules = {row["id"]: row for row in rule_index.load()["rules"]}
        self.assertEqual(rules["eutonos.rule.entry"]["tier"], "Required")
        onboarding = rules["eutonos.rule.onboarding"]
        self.assertEqual(onboarding["tier"], "Required-if")
        self.assertIn("first-run exercise", onboarding["applies_when"])
        self.assertIn("source-pinned RAM", onboarding["reason"])
        experience = rules["eutonos.rule.experience-navigation"]
        self.assertEqual(experience["tier"], "Optional")
        self.assertIn("entry/task requirements remain required", experience["applies_when"])

    def test_completion_precedence_references_entry_and_separate_delivery(self):
        relation = next(row for row in rule_index.load()["precedence"]
                        if row["rule"] == "eutonos.rule.completion-integration")
        self.assertIn("eutonos.rule.entry", relation["related"])
        self.assertIn("eutonos.rule.delivery", relation["related"])
        self.assertIn("eutonos.rule.campaign-gates", relation["related"])

    def test_renderer_retains_all_reading_rows_without_claiming_bindings(self):
        result = rule_index.render({"current_task": {"path": "work/TASK.md", "resource_id": "native.task"}})
        for row in rule_index.load()["rules"]:
            self.assertIn(row["id"], result)
            self.assertIn(row["applies_when"], result)
        self.assertIn("native `work/TASK.md` (`native.task`)", result)
        self.assertIn("upstream reference", result)
        self.assertIn("not consumer owners or installed capabilities", result)
        self.assertIn("AGENTS/selected onboarding route if optional orientation is unavailable", result)
        self.assertIn("unbound; resolve before dependent work", rule_index.render({}))
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("python tools/workflow.py orient --root .", agents)
        self.assertIn("**Mandatory first entry:**", agents)
        self.assertIn("[Agent onboarding](docs/AGENT_ONBOARDING.md)", agents)
        self.assertIn("Hooks are optional reminders, not an entry gate", agents)
        self.assertIn("@AGENTS.md", (ROOT / "CLAUDE.md").read_text(encoding="utf-8"))

    def test_schema_refuses_policy_text_duplicates_and_unknown_tiers(self):
        for kind in ("profile", "duplicate", "text", "tier", "path", "precedence"):
            with self.subTest(kind=kind):
                value = deepcopy(rule_index.load())
                if kind == "profile":
                    value["profile"] = "future.v2"
                elif kind == "duplicate":
                    value["rules"].append(deepcopy(value["rules"][0]))
                elif kind == "text":
                    value["rules"][0]["policy_text"] = "Copied policy"
                elif kind == "tier":
                    value["rules"][0]["tier"] = "Skip"
                elif kind == "path":
                    value["rules"][1]["owner"]["path"] = "../private.md"
                else:
                    value["precedence"][0]["related"] = ["unknown"]
                with self.assertRaises(ValueError):
                    rule_index.validate(value)
