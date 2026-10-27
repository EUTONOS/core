import unittest

from eutonos import companion_attribution as attribution
from eutonos import tokenslang as ts


def assignment(identity="a", request="request-1", worker_turn="worker-1", worker_model="gpt-6-luna"):
    return {"assignment_id": "assignment-" + identity, "request_id": request, "state": "completed",
            "primary": {"thread_id": "primary", "turn_id": "primary-request", "model": "gpt-6-astra"},
            "requested_worker": {"model": worker_model, "effort": "medium"},
            "attachment": {"thread_id": "worker-thread-" + identity, "turn_id": worker_turn},
            "result": {"model": worker_model}}


def run(thread, turn, model, role, start="2026-09-13T10:00:00Z", end="2026-09-13T10:01:00Z",
        usage=None, experiment=None):
    result = {"id": turn, "thread_id": thread, "turn_id": turn, "models": [model], "role": role,
              "state": "completed", "start": start, "end": end,
              "usage": usage or {"input_tokens": 100, "cached_input_tokens": 40,
                                  "output_tokens": 30, "reasoning_output_tokens": 10}}
    if experiment:
        result["experiment_id"] = experiment
    return result


def receipt(record, receipt_id="receipt-1", worker_model="gpt-6-astra", effort="low", review_turns=None):
    return {"profile": attribution.RECEIPT, "receipt_id": receipt_id,
            "request_id": record["request_id"], "assignment_id": record["assignment_id"],
            "primary": {"thread_id": "primary", "turn_id": "primary-request"},
            "worker": {"thread_id": record["attachment"]["thread_id"],
                       "turn_id": record["attachment"]["turn_id"]},
            "observed_worker": {"model": worker_model, "effort": effort, "basis": "native dispatch receipt"},
            "result": {"status": "completed", "basis": "native completion receipt"},
            "review": {"status": "accepted", "turns": review_turns or [
                {"thread_id": "primary", "turn_id": "review-1"}], "basis": "native review receipt"},
            "phases": {"request_started_at": "2026-09-13T09:59:00Z",
                       "dispatched_at": "2026-09-13T10:00:00Z",
                       "worker_started_at": "2026-09-13T10:00:10Z",
                       "worker_completed_at": "2026-09-13T10:01:00Z",
                       "review_started_at": "2026-09-13T10:01:10Z",
                       "review_completed_at": "2026-09-13T10:02:00Z"}}


class CompanionAttributionTests(unittest.TestCase):
    def test_exact_identity_keeps_requested_and_observed_execution_distinct(self):
        record = assignment()
        native = [run("primary", "primary-request", "gpt-6-astra", "primary"),
                  run("worker-thread-a", "worker-1", "gpt-6-astra", "support", experiment="companion-on"),
                  run("primary", "review-1", "gpt-6-astra", "primary",
                      start="2026-09-13T10:01:10Z", end="2026-09-13T10:02:00Z",
                      usage={"input_tokens": 20, "cached_input_tokens": 5,
                             "output_tokens": 8, "reasoning_output_tokens": 2})]
        comparisons = [{"experiment_id": "companion-on", "baseline_id": "companion-off",
                        "savings_usd": 0.75, "limits": []}]
        result = attribution.project([record], native, [receipt(record)], comparisons)
        item = result["records"][0]
        self.assertEqual(item["key"], {"request_id": "request-1", "assignment_id": "assignment-a"})
        self.assertEqual((item["companion"]["requested_model"], item["companion"]["observed_model"]),
                         ("gpt-6-luna", "gpt-6-astra"))
        self.assertEqual((item["companion"]["requested_effort"], item["companion"]["observed_effort"]),
                         ("medium", "low"))
        self.assertFalse(item["companion"]["model_matches_request"])
        self.assertEqual(item["review"]["overhead"]["tokens"]["input_tokens"], 20)
        self.assertEqual(item["tokens"]["combined_known"]["input_tokens"], 120)
        self.assertEqual(item["latency_seconds"]["queue_seconds"], 10)
        self.assertEqual(item["latency_seconds"]["end_to_end_seconds"], 180)
        self.assertEqual(item["qualified_savings"]["status"], "qualified")

    def test_exact_receipt_replays_do_not_duplicate_attribution(self):
        record = assignment()
        first = receipt(record, "receipt-1")
        retry = receipt(record, "receipt-2")
        native = [run("primary", "primary-request", "gpt-6-astra", "primary"),
                  run("worker-thread-a", "worker-1", "gpt-6-astra", "support"),
                  run("primary", "review-1", "gpt-6-astra", "primary")]
        result = attribution.project([record], native, [first, first, retry])
        self.assertEqual((result["record_count"], result["receipts"]["accepted_count"]), (1, 1))
        self.assertEqual(result["receipts"]["replay_count"], 2)
        self.assertEqual(result["records"][0]["receipt"]["status"], "observed")

    def test_conflicting_receipt_id_fails_closed_but_native_model_remains_observed(self):
        record = assignment()
        first = receipt(record)
        conflicting = receipt(record)
        conflicting["review"]["status"] = "rejected"
        native = [run("primary", "primary-request", "gpt-6-astra", "primary"),
                  run("worker-thread-a", "worker-1", "gpt-6-astra", "support")]
        result = attribution.project([record], native, [first, conflicting])
        item = result["records"][0]
        self.assertEqual(item["receipt"]["status"], "conflict")
        self.assertEqual(item["companion"]["observed_model"], "gpt-6-astra")
        self.assertIsNone(item["companion"]["observed_effort"])
        self.assertIsNone(item["review"]["status"])
        self.assertEqual(result["receipts"]["conflict_count"], 1)

    def test_several_helpers_count_request_once_and_each_model_pair_once(self):
        first = assignment("a", worker_turn="worker-a")
        second = assignment("b", worker_turn="worker-b", worker_model="gpt-6-sol")
        native = [run("primary", "primary-request", "gpt-6-astra", "primary"),
                  run("worker-thread-a", "worker-a", "gpt-6-luna", "support"),
                  run("worker-thread-b", "worker-b", "gpt-6-sol", "support")]
        result = attribution.project([first, second], native)
        self.assertEqual((result["request_count"], result["observed_request_count"]), (1, 1))
        self.assertEqual(result["requests"][0]["observed_helper_count"], 2)
        self.assertEqual({row["companion_model"] for row in result["model_pair_request_counts"]},
                         {"gpt-6-luna", "gpt-6-sol"})
        self.assertTrue(all(row["request_count"] == 1 for row in result["model_pair_request_counts"]))

    def test_missing_identity_usage_and_timing_stay_unknown(self):
        record = assignment()
        unrelated = run("other", "worker-1", "gpt-6-luna", "support",
                        start="2026-09-13T10:00:00Z", end="2026-09-13T10:01:00Z")
        unrelated["cwd"] = "same/project/path"
        item = attribution.project([record], [unrelated])["records"][0]
        self.assertIsNone(item["companion"]["observed_model"])
        self.assertIsNone(item["tokens"]["companion"])
        self.assertIsNone(item["latency_seconds"]["worker_phase_seconds"])
        self.assertIsNone(item["latency_seconds"]["end_to_end_seconds"])
        self.assertEqual(item["qualified_savings"]["status"], "unknown")

    def test_ambiguous_assignment_claims_do_not_double_count_one_turn(self):
        first = assignment("a", worker_turn="same-turn")
        second = assignment("b", worker_turn="same-turn")
        second["attachment"]["thread_id"] = first["attachment"]["thread_id"]
        native = [run("primary", "primary-request", "gpt-6-astra", "primary"),
                  run("worker-thread-a", "same-turn", "gpt-6-luna", "support")]
        result = attribution.project([first, second], native)
        self.assertEqual(result["observed_request_count"], 0)
        self.assertTrue(all(item["companion"]["observed_model"] is None for item in result["records"]))

    def test_invalid_receipt_phases_are_rejected(self):
        record = assignment()
        value = receipt(record)
        value["phases"]["worker_started_at"] = "2026-09-13T11:00:00Z"
        with self.assertRaises(ts.ReadError) as caught:
            attribution.normalize_receipt(value)
        self.assertEqual(caught.exception.code, "conflict")

    def test_unqualified_comparison_withholds_savings(self):
        record = assignment()
        native = [run("primary", "primary-request", "gpt-6-astra", "primary"),
                  run("worker-thread-a", "worker-1", "gpt-6-luna", "support", experiment="trial")]
        item = attribution.project([record], native, comparisons=[{
            "experiment_id": "trial", "savings_usd": 3.0, "limits": ["Quality review missing"]
        }])["records"][0]
        self.assertEqual(item["qualified_savings"]["status"], "withheld")
        self.assertIsNone(item["qualified_savings"]["usd"])


if __name__ == "__main__":
    unittest.main()
