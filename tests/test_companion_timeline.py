import unittest

from eutonos.companion_timeline import project


def row(**changes):
    value = {"id": "thread:turn", "thread_id": "thread", "turn_id": "turn",
             "start": "2026-09-12T10:00:00Z", "end": "2026-09-12T10:01:00Z",
             "state": "completed", "role": "primary", "models": ["gpt-6-astra"],
             "experiment_id": "experiment", "usage": {"input_tokens": 10, "cached_input_tokens": 5,
                                                        "output_tokens": 4, "reasoning_output_tokens": 2},
             "tool_counts": {"functions.exec": 1}}
    value.update(changes)
    return value


class TimelineTests(unittest.TestCase):
    def test_exact_active_receipt_only_and_no_request_guess(self):
        receipt = {"profile": "eutonos.companion-capture-receipt.v1", "disposition": "captured"}
        matching = {**receipt, "receipt_id": "capture-current", "thread_id": "thread", "turn_id": "turn", "stage": "completed",
                    "summary": "Current semantic capture", "ram": {"path": "RAM/current.json", "revision": 4}}
        old = {**receipt, "receipt_id": "capture-old", "thread_id": "thread", "turn_id": "turn", "stage": "completed",
               "summary": "Old capture", "ram": {"path": "RAM/old.json", "revision": 2}}
        replacement = {**receipt, "receipt_id": "capture-replacement", "thread_id": "other", "turn_id": "other", "stage": "completed",
                       "summary": "Other capture", "ram": {"path": "RAM/other.json", "revision": 3}, "supersedes": "capture-old"}
        result = project([row()], {"receipts": [matching, old, replacement]})
        turn = result["turns"][0]
        self.assertEqual(turn["ram"], [{"path": "RAM/current.json", "revision": 4,
                                         "summary": "Current semantic capture", "receipt_id": "capture-current",
                                         "summary_basis": "Receipt-linked capture summary; not a RAM record summary.",
                                         "stage": "completed"}])
        self.assertNotIn("request_id", turn)
        self.assertEqual(turn["project_origin"]["basis"], "legacy-retained-registration")

    def test_explicit_request_and_registration_origin_are_retained(self):
        origin = {"scope": "current-project", "basis": "explicit-source-registration", "note": "registered"}
        turn = project([row(project_origin=origin, request_id="request-1")])["turns"][0]
        self.assertEqual((turn["request_id"], turn["project_origin"]), ("request-1", origin))
        self.assertEqual(project([row(project_origin=origin)])["scope"]["explicit_registration_turn_count"], 1)

    def test_malformed_receipt_usage_and_tool_counts_remain_unavailable(self):
        malformed = {"profile": "eutonos.companion-capture-receipt.v1", "receipt_id": "capture-bad",
                     "thread_id": "thread", "turn_id": "turn", "stage": "completed", "disposition": "captured",
                     "summary": "Not admitted", "ram": {"path": "../private.json", "revision": True}}
        result_artifact = {"profile": "eutonos.companion-ram-capture-result.v1", "receipt_id": "capture-artifact",
                           "thread_id": "thread", "turn_id": "turn", "stage": "completed", "disposition": "captured",
                           "summary": "Not a receipt", "ram": {"path": "RAM/result.json", "revision": 3}}
        turn = project([row(usage={"input_tokens": 4, "cached_input_tokens": 5,
                                  "output_tokens": 2, "reasoning_output_tokens": 1},
                            tool_counts={"functions.exec": 1, "bad": -1})], {"receipts": [malformed, result_artifact]})["turns"][0]
        self.assertEqual(turn["usage"], dict.fromkeys(("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")))
        self.assertIsNone(turn["tool_counts"])
        self.assertEqual(turn["ram"], [])


if __name__ == "__main__":
    unittest.main()
