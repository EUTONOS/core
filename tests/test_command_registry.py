import copy
import contextlib
import io
import re
import unittest
from unittest import mock

from eutonos import command_registry as registry
from eutonos import workflow, workflow_cli, catalog_closeout, work_closeout, work_lifecycle


class CommandRegistryTests(unittest.TestCase):
    def test_orientation_exposes_versioned_schema_effects_and_readable_fallback(self):
        from eutonos import orientation
        command = registry.describe("orient")["command"]
        self.assertEqual(command["input_schema"], orientation.REQUEST_SCHEMA)
        self.assertEqual(command["output_schema"], orientation.PACKET_SCHEMA)
        self.assertEqual(command["side_effects"], orientation.EFFECTS)
        self.assertIsNone(command["configured"])
        self.assertIsNone(command["authorized"])
        self.assertTrue(any(row["id"] == "orient" for row in registry.help_topics()["topics"]))

    def test_optional_adaptive_closeout_schema(self):
        row = registry.describe("closeout")["command"]["input_schema"]["properties"]["work_closeout"]["items"]
        self.assertNotIn("stage_assessment", row["required"])
        self.assertNotIn("execution_recommendation", row["required"])
        assessment = row["properties"]["stage_assessment"]
        self.assertEqual(assessment["properties"]["disposition"]["enum"], ["unchanged", "adjusted", "review-required", "blocked"])
        self.assertFalse(assessment["additionalProperties"])
        self.assertEqual(row["properties"]["execution_recommendation"]["properties"]["advisory"], {"const": True})

    def test_closeout_schema_matches_missing_and_blank_field_validation(self):
        description = registry.describe("closeout")["command"]
        schema = description["input_schema"]
        missing = workflow.validate_closeout({})
        self.assertEqual(set(schema["required"]), {"catalog_changes", "work_changes"})
        self.assertEqual(set(schema["anyOf"][1]["required"]), {field["name"] for field in missing["missing_fields"]})
        self.assertEqual(schema["anyOf"][0]["required"], ["work_closeout"])
        for name, property_schema in schema["properties"].items():
            values = copy.deepcopy(description["example"])
            if name in {"work_closeout", "work_closeout_reason"}:
                values.update(work_closeout=[], work_closeout_reason="No tracked work changed.")
                values[name] = [" \t "] if name == "work_closeout" else " \t "
                with mock.patch.object(work_lifecycle, "load_order", return_value={"current": [], "next": []}):
                    checked = work_closeout.lifecycle_assessment(values, {"root": "."})
                self.assertTrue(checked["errors"], checked)
                if name == "work_closeout":
                    self.assertEqual(property_schema["items"]["type"], "object")
                    self.assertIn("progress", property_schema["items"]["required"])
                continue
            value_schema = property_schema.get("items", property_schema)
            values[name] = [" \t "] if property_schema["type"] == "array" else " \t "
            if name == "children":
                self.assertEqual(value_schema["type"], "object")
                self.assertEqual(set(value_schema["required"]), {"identity", "path", "revision"})
            else:
                self.assertIsNone(re.search(value_schema["pattern"], " \t "))
            if name in catalog_closeout.FIELDS:
                checked = catalog_closeout.assess(values, ".")
                self.assertTrue(checked["errors"])
            elif name in work_closeout.FIELDS:
                checked = work_closeout.assess(values, ".")
                self.assertTrue(checked["errors"])
            else:
                checked = workflow.validate_closeout(catalog_closeout.strip(values))
                self.assertIn(name, {field["name"] for field in checked["invalid_fields"]})

    def test_catalog_keeps_compact_shape_and_canonical_closeout_schema(self):
        result = registry.catalog()
        self.assertEqual((result["profile"], result["outcome"], result["query"]), (registry.PROFILE, "catalog", None))
        by_id = {item["id"]: item for item in result["commands"]}
        self.assertEqual(list(by_id), [item["id"] for item in registry.COMMANDS])
        self.assertEqual(set(by_id["pickup"]), {"id", "label", "summary", "example", "supported"})
        self.assertEqual(by_id["closeout"]["fields"], (*registry.CLOSEOUT_FIELDS, *registry.CATALOG_FIELDS, *registry.WORK_FIELDS))
        self.assertEqual(by_id["closeout"]["legacy_fields"], registry.LEGACY_FIELDS)
        self.assertNotIn("input_schema", by_id["closeout"])
        self.assertIs(workflow.CLOSEOUT_FIELDS, registry.CLOSEOUT_FIELDS)

    def test_fingerprint_is_stable_for_queries_and_changes_with_definition(self):
        baseline = registry.fingerprint()
        self.assertEqual(baseline, registry.fingerprint())
        self.assertEqual(baseline, registry.catalog("memory")["capability_fingerprint"])
        changed = copy.deepcopy(registry.COMMANDS)
        changed[0]["summary"] += " Changed."
        with mock.patch.object(registry, "COMMANDS", changed):
            self.assertNotEqual(baseline, registry.fingerprint())
        changed_work = copy.deepcopy(registry.WORK_FIELDS)
        changed_work[0]["description"] += " Changed."
        with mock.patch.object(registry, "WORK_FIELDS", changed_work):
            self.assertNotEqual(baseline, registry.fingerprint())
        with mock.patch.object(registry, "_available", return_value=False):
            self.assertNotEqual(baseline, registry.fingerprint())

    def test_describe_is_detailed_and_returns_independent_copies(self):
        first = registry.describe("closeout")
        second = registry.describe("closeout")
        self.assertEqual(first["outcome"], "description")
        self.assertEqual(first["command"]["input_schema"]["type"], "object")
        guidance = " ".join(first["command"]["steps"] + first["command"]["prerequisites"]).casefold()
        for required in ("prepare", "fill", "run", "existing context", "native log", "stdin"):
            self.assertIn(required, guidance)
        first["command"]["steps"].append("mutated")
        first["command"]["fields"][0]["name"] = "mutated"
        self.assertNotIn("mutated", second["command"]["steps"])
        self.assertEqual(second["command"]["fields"][0]["name"], "request")

    def test_help_glossary_and_unknown_outcomes_are_exact(self):
        self.assertEqual(registry.help_topics("TokenSlang")["outcome"], "help")
        self.assertEqual(registry.explain("source-pin")["outcome"], "explanation")
        self.assertEqual(registry.describe("missing")["code"], "unknown-command")
        self.assertEqual(registry.explain("missing")["code"], "unknown-term")

    def test_capabilities_do_not_conflate_support_configuration_or_authority(self):
        result = registry.capabilities()
        self.assertEqual(result["outcome"], "capabilities")
        self.assertNotIn("continuity", registry.EXPERT_ROUTES)
        self.assertEqual(registry.GUIDED_COMMANDS, ("closeout",))
        for item in result["capabilities"]:
            self.assertIsInstance(item["supported"], bool)
            self.assertIsNone(item["configured"])
            self.assertIsNone(item["authorized"])
        self.assertNotEqual(result["meaning"]["supported"], result["meaning"]["configured"])

    def test_nav_expert_route_dispatches_to_native_help_without_unparking_graph(self):
        self.assertEqual(registry.EXPERT_ROUTES["nav"], "eutonos.nav_cli")
        self.assertNotIn("graph", registry.EXPERT_ROUTES)

        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            workflow_cli.main(["expert", "nav", "--help"])

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("EUTONOS TokenSlang NAV", output.getvalue())

    def test_closeout_debt_expert_route_dispatches_to_native_assess_help(self):
        self.assertEqual(registry.EXPERT_ROUTES["closeout-debt"], "eutonos.closeout_debt")

        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            workflow_cli.main(["expert", "closeout-debt", "assess", "--help"])

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("--scope", output.getvalue())
        self.assertIn("--disposition {noop,blocked}", output.getvalue())
        self.assertIn("--reason-file", output.getvalue())


if __name__ == "__main__":
    unittest.main()
