import unittest

from eutonos.assignment_metrics import metadata, project
from eutonos.companion_metrics import window


def record(identity, turn, request=None):
    return {"assignment_id": identity, "request_id": request or identity, "state": "completed",
            "primary": {"thread_id": "primary", "turn_id": "p" + turn, "model": "gpt-6-astra"},
            "requested_worker": {"model": "gpt-6-luna", "effort": "medium"},
            "attachment": {"thread_id": "reused-worker", "turn_id": turn},
            "brief": {"goal": "Private task text"}, "result": {"summary": "Private findings"}}


def rows(turn):
    common = {"state": "completed", "start": "2026-09-13T09:00:00Z", "end": "2026-09-13T09:01:00Z",
              "usage": {"input_tokens": 100, "cached_input_tokens": 70, "output_tokens": 20, "reasoning_output_tokens": 10}}
    return [{**common, "id": "p" + turn, "thread_id": "primary", "turn_id": "p" + turn,
             "models": ["gpt-6-astra"], "role": "primary"},
            {**common, "id": turn, "thread_id": "reused-worker", "turn_id": turn,
             "models": ["gpt-6-luna"], "role": "support"}]


class AssignmentMetricsTests(unittest.TestCase):
    def test_reused_worker_turns_attribute_separate_requests(self):
        result = project(metadata([record("a", "1"), record("b", "2")]), rows("1") + rows("2"))
        self.assertEqual(result["observed_request_count"], 2)
        self.assertEqual(result["pairs"][0]["helper_tokens"]["input_tokens"], 200)
        self.assertEqual(result["observed_worker_turn_count"], 2)

    def test_conflicting_assignments_never_double_count_one_turn(self):
        result = project([record("a", "1"), record("b", "1")], rows("1"))
        self.assertEqual(result["observed_worker_turn_count"], 0)
        self.assertEqual(result["ambiguous_assignment_count"], 2)
        self.assertEqual(result["pairs"], [])

    def test_requested_models_and_missing_turns_are_not_observations(self):
        records = [record("a", "1"), record("b", "2")]
        records[0]["attachment"]["turn_id"] = None
        actual = rows("2")
        actual[1]["models"] = ["gpt-6-sol"]
        result = project(records, actual)
        self.assertEqual(result["observed_request_count"], 0)
        self.assertTrue(all(item["usage"] is None for item in result["assignments"]))
        self.assertNotIn("brief", metadata(records)[0])
        self.assertNotIn("result", metadata(records)[0])

    def test_range_counts_only_matching_native_turns_but_retains_registry_count(self):
        result = window({"runs": rows("1"), "assignment_records": metadata([record("a", "1")])}, 0,
                        from_value="2026-09-13T10:00:00Z", to_value="2026-09-13T11:00:00Z")
        self.assertEqual(result["metrics"]["assignments"]["assignment_count"], 1)
        self.assertEqual(result["metrics"]["assignments"]["observed_request_count"], 0)

    def test_saved_family_resolution_survives_metadata_and_joins_exact_native_model(self):
        from eutonos import model_catalog
        from unittest.mock import patch
        item = record("a", "1")
        item["requested_worker"]["model"] = "sol"
        item["model_selection"] = {"requested": "sol", "resolved": "gpt-6.1-sol",
            "selection_kind": "family", "family": "sol", "supported_efforts": ["medium"],
            "basis": "native fixture", "private_prompt": "never copied"}
        actual = rows("1")
        actual[1]["models"] = ["gpt-6.1-sol"]
        selected = metadata([item])
        self.assertNotIn("private_prompt", selected[0]["model_selection"])
        self.assertNotIn("brief", selected[0])
        with patch.object(model_catalog, "describe_catalog", side_effect=AssertionError("No live resolution")):
            result = project(selected, actual)
        self.assertEqual(result["observed_request_count"], 1)
        self.assertEqual(result["pairs"][0]["helper_model"], "gpt-6.1-sol")
        self.assertEqual(result["assignments"][0]["requested_worker"], "sol")
        self.assertEqual(result["assignments"][0]["resolved_worker"], "gpt-6.1-sol")
        actual[1]["models"] = ["gpt-6.2-sol"]
        self.assertEqual(project(selected, actual)["observed_worker_turn_count"], 0)

    def test_family_without_bounded_matching_resolution_has_no_attributed_usage(self):
        item = record("a", "1")
        item["requested_worker"]["model"] = "luna"
        self.assertEqual(project(metadata([item]), rows("1"))["observed_worker_turn_count"], 0)
        item["model_selection"] = {"requested": "sol", "resolved": "gpt-6-luna",
            "selection_kind": "family", "family": "sol", "supported_efforts": ["medium"],
            "basis": "native fixture"}
        self.assertEqual(project(metadata([item]), rows("1"))["observed_worker_turn_count"], 0)
        item["model_selection"]["basis"] = "oversized" * 1024
        self.assertNotIn("model_selection", metadata([item])[0])


if __name__ == "__main__":
    unittest.main()
