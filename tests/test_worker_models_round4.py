"""Same-job exact-model continuity after catalog removal; no native launches."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eutonos import model_catalog, worker_models as wm


class WorkerModelsRound4Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        environment = mock.patch.dict(os.environ, {"EUTONOS_HOME": str(self.root / "eutonos-home"), "CODEX_HOME": str(self.root / "codex-home")})
        environment.start()
        self.addCleanup(environment.stop)
        # The previously used exact models have disappeared from the listing.
        native = {"gpt-7-luna": ("high", "xhigh", "max"),
                  "gpt-7-sol": ("medium", "high", "xhigh")}
        capabilities = {"status": "ok", "basis": "round4-native", "invalid_items": 0,
                        "models": {model: {"supported_efforts": list(efforts),
                                           "family": model_catalog._family(model),
                                           "effort_basis": "native-supported-efforts"}
                                   for model, efforts in native.items()}}
        patcher = mock.patch.object(model_catalog, "read_codex_capabilities", return_value=capabilities)
        patcher.start()
        self.addCleanup(patcher.stop)

    def record(self, *, model="gpt-6-luna", family="luna", kind="family",
               supported=("high", "xhigh", "max"), observed=None, busy=False):
        req = {"id": "first", "conversation_id": "conversation-A", "caller_work_id": "work-A"}
        plan = {"requested": family if kind == "family" else model,
                "resolved": model, "family": family, "selection_kind": kind,
                "supported_efforts": list(supported) if supported is not None else None,
                "basis": "original-native"}
        wm.record_new(self.root, session_id="worker", harness="codex", req=req, plan=plan)
        if not busy:
            wm.finalize(self.root, session_id="worker", req=req,
                        observed=observed, observed_source="native" if observed else None)
        return wm.load_entry(self.root, "worker")

    def request(self, **overrides):
        return {"id": "followup", "mode": "resume", "session_id": "worker",
                "conversation_id": "conversation-A", "caller_work_id": "work-A", **overrides}

    def resume(self, **overrides):
        return wm.plan_resume(self.request(**overrides), harness="codex", root=self.root)

    def test_unlisted_same_job_family_tries_original_pin_and_keeps_identity(self):
        original = self.record()
        plan = self.resume(model="luna")
        self.assertEqual((plan["resolved"], plan["effort"]), ("gpt-6-luna", "xhigh"))
        self.assertTrue(plan["same_job_model_unlisted"])
        entry = wm.load_entry(self.root, "worker")
        for field in ("requested", "resolved", "observed", "family", "selection_kind",
                      "conversation_id", "caller_work_id", "job_key", "supported_efforts", "basis"):
            self.assertEqual(entry[field], original[field], field)
        self.assertTrue(entry["busy"])
        self.assertFalse(entry["retired"])
        self.assertEqual(entry["in_flight_request_id"], "followup")

    def test_unlisted_same_job_exact_pin_keeps_pin(self):
        self.record(kind="pin")
        plan = self.resume(model="gpt-6-luna", effort="max")
        self.assertEqual(plan["resolved"], "gpt-6-luna")
        self.assertEqual(plan["selection_kind"], "pin")
        self.assertFalse(wm.load_entry(self.root, "worker")["retired"])

    def test_unavailable_catalog_entry_still_attempts_same_job_stored_pin(self):
        self.record()
        catalog = model_catalog.describe_catalog(self.root)
        catalog["models"]["gpt-6-luna"] = {"resolved": "gpt-6-luna", "harness": "codex",
                                              "available": False, "supported_efforts": ("high",)}
        with mock.patch.object(model_catalog, "describe_catalog", return_value=catalog):
            plan = self.resume()
        self.assertEqual(plan["resolved"], "gpt-6-luna")
        self.assertEqual(plan["effort"], "xhigh")
        self.assertTrue(plan["same_job_model_unlisted"])

    def test_observed_unlisted_exact_model_remains_launch_truth(self):
        self.record(observed="gpt-6-luna-20260901")
        plan = self.resume()
        self.assertEqual(plan["resolved"], "gpt-6-luna-20260901")
        self.assertEqual(wm.load_entry(self.root, "worker")["resolved"], "gpt-6-luna")

    def test_changed_conversation_under_same_work_keeps_unlisted_pin(self):
        self.record()
        req = self.request(conversation_id="conversation-B")
        plan = wm.plan_resume(req, harness="codex", root=self.root)
        self.assertEqual(plan["resolved"], "gpt-6-luna")
        entry = wm.finalize(self.root, session_id="worker", req=req)
        self.assertEqual(entry["caller_work_id"], "work-A")
        self.assertIsNone(entry["job_completed_at"])
        self.assertFalse(entry["retired"])

    def test_new_job_resolves_latest_and_retires_unlisted_family_worker(self):
        self.record()
        new = wm.plan_new({"model": "luna"}, harness="codex", root=self.root)
        self.assertEqual(new["resolved"], "gpt-7-luna")
        with self.assertRaises(wm.WorkerModelError) as caught:
            self.resume(caller_work_id="work-B", conversation_id="conversation-B")
        self.assertEqual(caught.exception.code, "worker-retired")
        self.assertEqual(caught.exception.extra["handoff"]["newer_model"], "gpt-7-luna")
        entry = wm.load_entry(self.root, "worker")
        self.assertTrue(entry["retired"])
        self.assertFalse(entry["busy"])
        self.assertEqual(entry["caller_work_id"], "work-A")
        # A refused new job does not invalidate continuations of the original job.
        self.assertEqual(self.resume()["resolved"], "gpt-6-luna")

    def test_new_job_unlisted_exact_luna_pin_refuses_without_retiring(self):
        self.record(kind="pin")
        with self.assertRaises(wm.WorkerModelError) as caught:
            self.resume(caller_work_id="work-B", conversation_id="conversation-B")
        self.assertEqual(caught.exception.code, "model-unresolved")
        entry = wm.load_entry(self.root, "worker")
        self.assertFalse(entry["retired"])
        self.assertFalse(entry["busy"])
        with self.assertRaises(wm.WorkerModelError):
            wm.plan_new({"model": "gpt-6-luna"}, harness="codex", root=self.root)

    def test_same_job_unlisted_sol_also_keeps_exact_model(self):
        self.record(model="gpt-6-sol", family="sol", supported=("medium", "high"))
        self.assertEqual(self.resume(model="sol")["resolved"], "gpt-6-sol")

    def test_unlisted_luna_still_refuses_lower_invalid_and_known_unsupported_effort(self):
        for effort in ("high", "medium", "bogus", [], {}, 1, "ultra"):
            with self.subTest(effort=effort):
                self.record()
                with self.assertRaises(wm.WorkerModelError) as caught:
                    self.resume(effort=effort)
                self.assertEqual(caught.exception.code, "model-unresolved")
                self.assertFalse(wm.load_entry(self.root, "worker")["busy"])

    def test_unlisted_luna_defaults_using_recorded_capabilities(self):
        self.record(supported=("high", "max"))
        self.assertEqual(self.resume()["effort"], "max")
        self.record(supported=("high",))
        with self.assertRaises(wm.WorkerModelError):
            self.resume()
        self.assertFalse(wm.load_entry(self.root, "worker")["busy"])

    def test_missing_recorded_capabilities_try_policy_valid_effort(self):
        self.record(supported=None)
        self.assertEqual(self.resume()["effort"], "xhigh")

    def test_present_exact_model_uses_current_supported_efforts(self):
        self.record(model="gpt-7-luna", supported=("high", "xhigh", "max", "ultra"))
        with self.assertRaises(wm.WorkerModelError) as caught:
            self.resume(effort="ultra")
        self.assertEqual(caught.exception.code, "model-unresolved")
        self.assertFalse(wm.load_entry(self.root, "worker")["busy"])

    def test_unlisted_busy_session_remains_fenced(self):
        self.record(busy=True)
        with self.assertRaises(wm.WorkerModelError) as caught:
            self.resume()
        self.assertEqual(caught.exception.code, "session-busy")
        self.assertEqual(wm.load_entry(self.root, "worker")["in_flight_request_id"], "first")

    def test_unlisted_wrong_harness_remains_refused(self):
        self.record()
        with self.assertRaises(wm.WorkerModelError) as caught:
            wm.plan_resume(self.request(), harness="claude", root=self.root)
        self.assertEqual(caught.exception.code, "resume-harness-mismatch")
        self.assertFalse(wm.load_entry(self.root, "worker")["busy"])

    def test_unlisted_observed_claude_id_cannot_bypass_luna_harness_validation(self):
        self.record(observed="claude-opus-9")
        with self.assertRaises(wm.WorkerModelError) as caught:
            self.resume()
        self.assertEqual(caught.exception.code, "model-unresolved")
        self.assertFalse(wm.load_entry(self.root, "worker")["busy"])

    def test_malformed_observed_resolved_or_capability_types_are_not_trusted(self):
        for field, value in (("observed", {}), ("resolved", []), ("observed", 1),
                             ("supported_efforts", "xhigh"), ("supported_efforts", [1])):
            with self.subTest(field=field, value=value):
                entry = self.record()
                entry[field] = value
                path = wm.registry_dir(self.root) / "worker.json"
                path.write_text(json.dumps(entry), encoding="utf-8")
                with self.assertRaises(wm.WorkerModelError) as caught:
                    self.resume()
                self.assertEqual(caught.exception.code, "resume-model-unknown")
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))[field], value)

    def test_catalog_removal_does_not_allow_model_switch(self):
        self.record()
        with self.assertRaises(wm.WorkerModelError) as caught:
            self.resume(model="gpt-7-luna")
        self.assertEqual(caught.exception.code, "resume-model-mismatch")
        self.assertFalse(wm.load_entry(self.root, "worker")["busy"])


if __name__ == "__main__":
    unittest.main()
