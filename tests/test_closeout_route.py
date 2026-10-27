import tempfile
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from eutonos import closeout_route as route
from eutonos.closeout_organization import PROFILE
from eutonos.host import enroll, connect


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/"repo";self.root.mkdir();(self.root/"docs").mkdir()
        (self.root/"docs/CURRENT_TASK.md").write_text("# Current task\n\n## Done\n\nFinished.\n\n## Keep\n\nStill working.\n",encoding="utf-8")
        subprocess.run(["git","init","-q",str(self.root)],check=True)
        subprocess.run(["git","-C",str(self.root),"add","."],check=True)
        subprocess.run(["git","-C",str(self.root),"-c","user.name=Test","-c","user.email=test@example.invalid","commit","-qm","fixture"],check=True)
        self.context={"root":str(self.root),"host":str(Path(self.tmp.name)/"host"),"actor":"test"}
        self.values={"catalog_changes":[],"work_changes":[],"organization":{"profile":PROFILE,"title":"File done work","summary":"Preserve finished checkpoint.","disposition":"completed","headings":["Done"],"archive_path":"docs/COMPLETED_WORK.md"}}

    def test_ram_independent_organization_and_replay(self):
        prepared=route.prepare(self.values,self.context)
        enroll(self.root,self.context["host"],"test",prepared["allowed_paths"])
        result=route.run(prepared,self.context)
        self.assertEqual(result["outcome"],"completed",result)
        from eutonos.workflow_diagnostics import record
        self.assertEqual(record(self.root,result)["outcome"],"recorded")
        self.assertEqual(result["memory"]["outcome"],"not-requested")
        self.assertEqual(result["organization"]["host"]["reservations"],[])
        self.assertEqual(route.run(prepared,self.context)["outcome"],"already-completed")

    def test_stale_plan_does_not_take_reservations(self):
        prepared=route.prepare(self.values,self.context)
        enroll(self.root,self.context["host"],"test",prepared["allowed_paths"])
        p=self.root/"docs/CURRENT_TASK.md";p.write_text(p.read_text()+"New concurrent work.\n")
        result=route.run(prepared,self.context)
        self.assertEqual(result["outcome"],"error")
        self.assertIn("before taking reservations",result["message"])
        self.assertNotIn("## Done",(self.root/"docs/COMPLETED_WORK.md").read_text() if (self.root/"docs/COMPLETED_WORK.md").exists() else "")

    def test_noop_needs_no_host(self):
        values={"catalog_changes":[],"work_changes":[],"organization":{"profile":PROFILE,"title":"No changes","summary":"No durable change.","disposition":"continuing"}}
        result=route.run(values,{"root":str(self.root)})
        self.assertEqual(result["outcome"],"skipped",result)
        self.assertFalse(Path(self.context["host"]).exists())

    def test_memory_preparation_reports_filing_unassessed(self):
        result=route.prepare({"catalog_changes":[],"work_changes":[]},self.context)
        self.assertEqual(result["organization"]["outcome"],"not-assessed")


class LifecycleMemorySignalTests(unittest.TestCase):
    def result(self, receipt_outcome, memory):
        row = {"item_id": "synthetic", "operation": "update", "what": "Synthetic closeout",
               "why": "Fixture", "result": "Landed", "checks": ["Synthetic only"]}
        receipt = {"outcome": receipt_outcome, "request_id": "synthetic-receipt"}
        with patch("eutonos.write_retry.submit_batch", return_value=receipt) as submit, \
                patch.object(route, "_run", return_value={"memory": memory}) as capture:
            result = route._lifecycle_closeout(None, {}, {},
                {"root": "synthetic", "host": "synthetic", "actor": "synthetic"}, {"requests": [row]})
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(capture.call_count, 1 if receipt_outcome == "complete" else 0)
        self.assertEqual(result["lifecycle"], [receipt])
        return result

    def test_landed_filing_keeps_failed_ram_separate_and_blocks_completion_claim(self):
        from eutonos.workflow_cli import _closeout_attention, _closeout_receipt
        for outcome in ("error", "incomplete", "denied", "stale", "failed", "partial", "blocked",
                        "conflict", "unavailable", "attention-required"):
            with self.subTest(outcome=outcome):
                memory = {"outcome": outcome, "message": "Synthetic capture failure",
                          "next_supported_action": "Recover the frozen RAM attempt."}
                result = self.result("complete", memory)
                self.assertEqual(result["outcome"], "attention-required")
                self.assertEqual(result["organization"]["outcome"], "completed")
                self.assertEqual(result["memory"], memory)
                self.assertEqual(result["ram_attention"]["required_filing"], "landed")
                self.assertIn("Do not replay lifecycle filing to retry RAM", result["next"])
                self.assertIn(memory["next_supported_action"], result["next"])
                self.assertEqual(result["memory_recovery"]["mode"], "prepare-memory-only")
                self.assertEqual(result["memory_recovery"]["input"]["work_closeout"], [])
                result["closeout_attention"] = _closeout_attention(result)
                # The newer CLI must still refuse a landed-completion claim.
                receipt = _closeout_receipt(result)
                self.assertEqual(receipt["state"], "attention-required")
                self.assertFalse(receipt["may_finish_turn"])

    def test_exact_memory_recovery_bypasses_completed_filing(self):
        from eutonos.workflow import PROFILE as MEMORY_PROFILE
        prepared = {"profile": MEMORY_PROFILE, "outcome": "prepared", "attempt": "original-ram-attempt"}
        row = {"item_id": "synthetic", "operation": "update", "what": "Synthetic closeout",
               "why": "Fixture", "result": "Filed", "checks": ["Synthetic only"]}
        with patch("eutonos.write_retry.submit_batch", return_value={"outcome": "complete", "request_id": "filed"}) as submit, \
                patch.object(route, "_run", return_value={"memory": {"outcome": "error", "message": "Capture interrupted"},
                                                         "prepared_operation": prepared}):
            result = route._lifecycle_closeout(None, {}, {},
                {"root": "synthetic", "host": "synthetic", "actor": "synthetic"}, {"requests": [row]})
        submit.assert_called_once()
        self.assertEqual(result["lifecycle"][0]["outcome"], "complete")
        self.assertEqual(result["memory_recovery"]["input"], prepared)
        self.assertEqual(result["memory_recovery"]["mode"], "exact-prepared-operation")
        with patch("eutonos.write_retry.submit_batch") as replay, \
                patch.object(route, "run_memory", return_value={"outcome": "already-completed", "current_verified": True}) as capture:
            recovered = route.run(prepared, {"root": "synthetic", "host": "synthetic", "actor": "synthetic"})
        replay.assert_not_called()
        capture.assert_called_once_with(prepared, {"root": "synthetic", "host": "synthetic", "actor": "synthetic"})
        self.assertEqual(recovered["outcome"], "already-completed")
        self.assertEqual(recovered["organization"]["outcome"], "not-assessed")

    def test_landed_success_and_skipped_memory_preserve_existing_outcomes(self):
        for outcome in ("completed", "skipped", "not-requested", "already-completed"):
            with self.subTest(outcome=outcome):
                result = self.result("complete", {"outcome": outcome})
                self.assertEqual(result["outcome"], "completed")
                self.assertNotIn("ram_attention", result)
                self.assertEqual(result["memory"]["outcome"], outcome)

    def test_verified_lifecycle_and_ram_receipt_is_landed_not_queued(self):
        from eutonos.workflow_cli import _closeout_attention, _closeout_receipt
        row = {"item_id": "synthetic", "operation": "update", "what": "Record completion",
               "why": "Checked", "result": "Filed", "checks": ["Focused check"]}
        filing = {"outcome": "complete", "state": "complete", "request_id": "filed-request",
                  "result": {"outcome": "complete", "readback": "verified", "complete": True}}
        ram_capture = {"outcome": "completed", "result": {"outcome": "completed", "current_verified": True,
                                                         "readback": {"path": "RAM/record.json", "revision": 1}}}
        with patch("eutonos.write_retry.submit_batch", return_value=filing) as submit, \
                patch.object(route, "_run", return_value=ram_capture) as capture:
            result = route._lifecycle_closeout(None, {}, {},
                {"root": "synthetic", "host": "synthetic", "actor": "synthetic"}, {"requests": [row]})
        submit.assert_called_once()
        capture.assert_called_once()
        self.assertEqual(result["organization"]["outcome"], "completed")
        self.assertEqual(result["lifecycle"][0]["result"]["readback"], "verified")
        self.assertTrue(result["memory"]["result"]["current_verified"])
        result["closeout_attention"] = _closeout_attention(result)
        receipt = _closeout_receipt(result)
        self.assertEqual(receipt["state"], "landed")
        self.assertEqual(receipt["request_ids"], ["filed-request"])
        self.assertNotIn("queued", receipt["safe_claim"])

    def test_pending_filing_does_not_capture_or_claim_memory_failure(self):
        result = self.result("queued", {"outcome": "error"})
        self.assertEqual(result["outcome"], "deferred")
        self.assertEqual(result["organization"]["outcome"], "queued")
        self.assertEqual(result["memory"]["reason"], "required-filing-queued")
        self.assertNotIn("ram_attention", result)

if __name__=="__main__":unittest.main()
