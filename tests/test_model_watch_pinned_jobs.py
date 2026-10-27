"""Confirmed retirement fences new pinned jobs without interrupting old ones."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from eutonos import model_catalog, worker_models as wm


class PinnedJobBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = mock.patch.dict(os.environ, {"EUTONOS_HOME": str(self.root / "user"),
                                              "CODEX_HOME": str(self.root / "codex")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.req = {"id": "first", "conversation_id": "conversation-A", "caller_work_id": "work-A"}
        wm.record_new(self.root, session_id="worker", harness="codex", req=self.req,
                      plan={"requested": "gpt-6-sol", "resolved": "gpt-6-sol", "family": "sol",
                            "selection_kind": "pin", "supported_efforts": ["medium", "high"],
                            "basis": "original-native"})
        wm.finalize(self.root, session_id="worker", req=self.req)

    def resume(self, *, different_job=False, catalog=None):
        request = {**self.req, "id": "followup", "mode": "resume", "session_id": "worker",
                   "caller_work_id": "work-B" if different_job else "work-A"}
        with mock.patch.object(model_catalog, "describe_catalog", return_value={"models": catalog or {}}):
            return wm.plan_resume(request, harness="codex", root=self.root)

    def test_known_retired_sol_pin_refuses_new_job_before_reserving(self):
        with self.assertRaisesRegex(wm.WorkerModelError, "unavailable for a new job"):
            self.resume(different_job=True, catalog={"gpt-6-sol": {"available": False}})
        stored = wm.load_entry(self.root, "worker")
        self.assertFalse(stored["busy"])
        self.assertFalse(stored["retired"])
        self.assertEqual(stored["caller_work_id"], "work-A")

    def test_known_retired_sol_pin_still_attempts_same_job(self):
        plan = self.resume(catalog={"gpt-6-sol": {"available": False}})
        self.assertEqual(plan["resolved"], "gpt-6-sol")
        self.assertTrue(plan["same_job_model_unlisted"])
        self.assertFalse(wm.load_entry(self.root, "worker")["retired"])

    def test_unknown_catalog_does_not_invent_retirement(self):
        plan = self.resume(different_job=True)
        self.assertEqual(plan["resolved"], "gpt-6-sol")
        self.assertFalse(wm.load_entry(self.root, "worker")["retired"])
