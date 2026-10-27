"""Explicit slice/stage associations and atomic guided filing."""
from copy import deepcopy
import unittest

from tests import test_work_lifecycle as fixture
from eutonos import boards, tokenslang as ts, work_closeout, work_lifecycle as work, write_batch, write_retry
from eutonos.effects import digest


class StageCloseoutAssociationTests(unittest.TestCase):
    setUp = fixture.WorkLifecycleTests.setUp
    item = fixture.WorkLifecycleTests.item
    write_board = fixture.WorkLifecycleTests.write_board
    row = fixture.WorkLifecycleTests.row
    request = fixture.WorkLifecycleTests.request
    apply = fixture.WorkLifecycleTests.apply

    def association_request(self, stage_id, identity="A"):
        row = self.row(identity)
        extensions = {**deepcopy(row.get("extensions", {})), "stage_id": stage_id}
        return {"profile": boards.UPDATE, "owner_path": row["owner_path"],
                "expected_sha256": digest((self.root / row["owner_path"]).read_bytes()),
                "item_id": identity, "changes": {"extensions": extensions}}

    def associate(self, stage_id="TEST1"):
        request = self.association_request(stage_id)
        result = write_retry.submit_batch(self.host, self.root, "writer", {
            "profile": write_batch.PROFILE, "operations": [{"kind": "update", "request": request}]})
        self.assertEqual(result["outcome"], "complete", result)

    def answer(self, identity, status=None):
        item = self.row(identity)
        return {"item_id": identity, "expected_revision": item["revision"],
                "status": status or item["status"], "what": "Filed bounded scope for " + identity,
                "why": "Owner requested stage-aware filing.",
                "result": "Slice complete; stage and campaign retain remaining work.",
                "checks": ["Source and native work inspected."],
                "progress": {"percent": 60, "basis": "Remaining stage work is explicit."}}

    def assess(self, rows, selected="A"):
        return work_closeout.lifecycle_assessment({"work_closeout": rows},
                                                  {"root": self.root, "work_id": selected})

    def test_work_forms_show_explicit_association_without_parenting_slice(self):
        self.associate()
        shown = work.form(self.root, "A")
        self.assertEqual(shown["stage_association"]["stage_id"], "TEST1")
        self.assertNotIn("parent_id", shown["item"])
        self.assertIn("extensions.stage_id", boards.work_form("slice")["relationship"])
        examples = self.assess([])["example"]["work_closeout"]
        self.assertEqual([row["item_id"] for row in examples], ["A", "TEST1", "TEST"])
        self.assertEqual(examples[1]["status"], "active")

    def test_associated_completion_requires_stage_and_campaign_rows(self):
        self.associate()
        checked = self.assess([self.answer("A", "completed")])
        self.assertEqual(checked["outcome"], "incomplete")
        self.assertIn("stage closeout for TEST1", checked["missing_fields"])
        self.assertIn("campaign progress for TEST", checked["missing_fields"])
        checked = self.assess([self.answer("A", "completed"), self.answer("TEST1")])
        self.assertEqual(checked["missing_fields"], ["campaign progress for TEST"])

    def test_atomic_filing_completes_slice_updates_stage_and_keeps_campaign_open(self):
        self.associate()
        checked = self.assess([self.answer("A", "completed"), self.answer("TEST1"), self.answer("TEST")])
        self.assertEqual(checked["outcome"], "verified", checked)
        self.assertEqual(checked["requests"][1]["operation"], "update")
        result = write_retry.submit_batch(self.host, self.root, "writer", {
            "profile": write_batch.PROFILE,
            "operations": [{"kind": "work", "request": row} for row in checked["requests"]]})
        self.assertEqual(result["outcome"], "complete", result)
        self.assertEqual(self.row("A")["status"], "completed")
        self.assertEqual(self.row("TEST1")["status"], "active")
        self.assertEqual(self.row("TEST")["status"], "active")
        self.assertIn("Filed bounded scope", self.row("TEST1")["extensions"]["checkpoint"]["what"])
        self.assertNotIn("A", boards.project(self.root)["work_order"]["current"])
        self.assertEqual(self.row("A")["extensions"]["stage_id"], "TEST1")

    def test_missing_wrong_type_and_malformed_mapping_are_refused_before_write(self):
        original = (self.root / self.owner).read_bytes()
        for stage_id in ("missing", "TEST", "B", " ", None, ["TEST1"]):
            with self.subTest(stage_id=stage_id):
                with self.assertRaises(ts.ReadError):
                    boards.plan_update(self.root, self.association_request(stage_id))
                self.assertEqual((self.root / self.owner).read_bytes(), original)
        with self.assertRaises(ts.ReadError):
            boards.plan_update(self.root, self.association_request("TEST1", "TEST"))

    def test_invalid_existing_mapping_stays_visible_and_closeout_refuses_it(self):
        for stage_id in ("missing", ["TEST1"]):
            self.write_board(self.owner, [self.item("A", extensions={"stage_id": stage_id}), self.item("B", status="planned")])
            shown = boards.project(self.root)
            self.assertTrue(any("stage association" in issue for issue in shown["diagnostics"]))
            self.assertIn("A", [item["id"] for item in shown["items"]])
            checked = self.assess([self.answer("A", "completed")])
            self.assertEqual(checked["outcome"], "incomplete", checked)
            self.assertTrue(checked["errors"])

    def test_changed_association_invalidates_old_slice_completion_answers(self):
        self.associate("TEST1")
        rows = [self.answer("A", "completed"), self.answer("TEST1"), self.answer("TEST")]
        self.associate("TEST2")
        checked = self.assess(rows)
        self.assertEqual(checked["outcome"], "incomplete")
        self.assertTrue(any("association changed" in error for error in checked["errors"]), checked)
        self.assertEqual(self.row("A")["status"], "active")

    def test_stage_revision_change_rejects_whole_prepared_batch(self):
        self.associate()
        checked = self.assess([self.answer("A", "completed"), self.answer("TEST1"), self.answer("TEST")])
        self.apply(self.request("TEST1", changes={"summary": "Concurrent stage scope."}))
        batch = {"profile": write_batch.PROFILE,
                 "operations": [{"kind": "work", "request": row} for row in checked["requests"]]}
        with self.assertRaises(ts.ReadError):
            write_batch.plan(self.root, batch)
        self.assertEqual(self.row("A")["status"], "active")

    def test_terminal_mapped_stage_requires_explicit_restore(self):
        self.associate()
        self.apply(self.request("TEST1", "complete", what="Stage delivered.", why="Checked scope.",
                                result="Stage closed.", checks=["Source inspected."]))
        checked = self.assess([self.answer("A", "completed"), self.answer("TEST1", "completed"), self.answer("TEST")])
        self.assertEqual(checked["outcome"], "incomplete")
        self.assertTrue(any("terminal" in error for error in checked["errors"]))
        self.assertEqual(self.row("TEST1")["status"], "completed")

    def test_unmapped_legacy_slice_keeps_standalone_completion_and_assessment_prompt(self):
        stage_before = (self.root / self.campaign).read_bytes()
        row = self.answer("A", "completed")
        row["result"] = "Completed standalone scope; no stage changed."
        checked = self.assess([row])
        self.assertEqual(checked["outcome"], "verified", checked)
        self.assertEqual(checked["stage_assessment"]["standalone_slices"], ["A"])
        self.assertIn("no stage changed", checked["stage_assessment"]["prompt"])
        self.apply(checked["requests"][0])
        self.assertEqual((self.root / self.campaign).read_bytes(), stage_before)
        self.assertNotIn("stage_id", self.row("A")["extensions"])

    def test_continuing_associated_slice_does_not_infer_stage_completion(self):
        self.associate()
        checked = self.assess([self.answer("A", "active")])
        self.assertEqual(checked["outcome"], "verified", checked)
        self.assertEqual(checked["requests"][0]["operation"], "update")


if __name__ == "__main__":
    unittest.main()
