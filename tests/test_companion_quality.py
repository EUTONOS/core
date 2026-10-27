"""Quality judgments are explicit, output-pinned and comparable only within scope."""
import copy
import unittest
from eutonos.companion_quality import RUBRIC, validate_experiment, validate_review, review_projection, enrich, experiment_fingerprint


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.e = dict(id="e", label="Trial", task_id="task", task_brief="Answer the frozen task", mode="on",
                      primary_model="gpt-6-astra", support_model="gpt-6-luna", source_revision="commit",
                      source_fingerprint="c" * 64, conditions_id="condition-1")
        self.rows = [dict(id="p", experiment_id="e", role="primary", state="completed", final_sha256="a" * 64, models=["gpt-6-astra"]),
                     dict(id="s", experiment_id="e", role="support", state="completed", final_sha256="b" * 64, models=["gpt-6-luna"])]
        self.r = dict(experiment_id="e", experiment_fingerprint=experiment_fingerprint(self.e), output_summary="Answered the task",
                      output_refs=[dict(run_id="p", final_sha256="a" * 64)], reviewer="Reviewer A", review_kind="independent-agent",
                      rubric_id=RUBRIC["id"], evaluated_at="2026-09-12T12:00:00Z", retrospective=False, limitations=[],
                      dimensions={k:dict(score=s, rationale="Inspected the stated criterion", evidence=["source.py:20"])
                                  for k,s in zip((d["id"] for d in RUBRIC["dimensions"]),(93,96,88,94))})

    def test_weighted_score_and_idempotent_review_id(self):
        valid=validate_review(self.r)
        self.assertEqual(valid,validate_review(valid))
        self.assertEqual(review_projection(valid,self.rows,self.e)["score"],93)

    def test_rejects_missing_or_nonfinite_scores(self):
        for value in (None,True,-1,101,float("nan"),float("inf")):
            r=copy.deepcopy(self.r);r["dimensions"]["correctness"]["score"]=value
            with self.assertRaises(ValueError):validate_review(r)
        r=copy.deepcopy(self.r);del r["dimensions"]["evidence"]
        with self.assertRaises(ValueError):validate_review(r)

    def test_requires_rationale_and_evidence(self):
        for field,value in (("rationale",""),("evidence",[])):
            r=copy.deepcopy(self.r);r["dimensions"]["correctness"][field]=value
            with self.assertRaises(ValueError):validate_review(r)

    def test_changed_output_or_task_invalidates_score(self):
        rows=copy.deepcopy(self.rows);rows[0]["final_sha256"]="d"*64
        self.assertIsNone(review_projection(self.r,rows,self.e)["score"])
        self.assertIsNone(review_projection(self.r,self.rows,{**self.e,"task_brief":"A different task"})["score"])

    def test_new_unreviewed_primary_turn_removes_whole_experiment_score(self):
        rows=self.rows+[dict(self.rows[0],id="new",state="running")]
        self.assertIsNone(review_projection(self.r,rows,self.e)["score"])
        r=copy.deepcopy(self.r);r["output_refs"]=[dict(run_id="s",final_sha256="b"*64)]
        self.assertIsNone(review_projection(r,self.rows,self.e)["score"])

    def test_contributor_review_is_visible_but_not_matched_evidence(self):
        r={**self.r,"review_kind":"contributor","retrospective":True}
        e=enrich([self.e],self.rows,[r])[0]
        self.assertEqual(e["assessment"]["score"],93)
        self.assertIsNone(e["comparison_key"])
        self.assertTrue(e["comparison_limits"])

    def test_different_observed_pairings_share_same_task_comparison_key(self):
        e1=enrich([self.e],self.rows,[self.r])[0]
        e2={**self.e,"id":"other","primary_model":"gpt-6-sol","support_model":"gpt-6-astra"}
        rows=[{**row,"experiment_id":"other","models":[e2[row['role']+'_model']]} for row in self.rows]
        review={**self.r,"experiment_id":"other","experiment_fingerprint":experiment_fingerprint(e2)}
        e2=enrich([e2],rows,[review])[0]
        self.assertIsNotNone(e1["comparison_key"])
        self.assertEqual(e1["comparison_key"],e2["comparison_key"])

    def test_model_mismatch_unknown_source_and_off_mode(self):
        rows=copy.deepcopy(self.rows);rows[1]["models"]=["gpt-6-sol"]
        e=enrich([self.e],rows,[self.r])[0]
        self.assertEqual(e["pairing_status"],"mismatch-or-incomplete")
        self.assertIsNone(e["comparison_key"])
        e=enrich([{**self.e,"source_fingerprint":None}],self.rows,[])[0]
        self.assertIsNone(e["comparison_key"])
        with self.assertRaises(ValueError):validate_experiment({**self.e,"mode":"off"})
        validate_experiment({**self.e,"mode":"off","support_model":None})

    def test_ungraded_is_unknown_and_invalid_latest_review_does_not_reuse_old_score(self):
        self.assertIsNone(enrich([self.e],self.rows,[])[0]["assessment"])
        invalid={**self.r,"rubric_id":"other"}
        self.assertIsNone(enrich([self.e],self.rows,[self.r,invalid])[0]["assessment"]["score"])


if __name__ == "__main__":unittest.main()
