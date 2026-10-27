"""Native child trails in semantic closeout; no transcript-derived summaries."""
from copy import deepcopy
import unittest

from tests import test_workflow as fixture
from eutonos import companion_ram_capture, ram, tokenslang as ts, work_closeout
from eutonos.command_registry import _closeout_schema
from eutonos.effects import LocalRoot
from eutonos.workflow import execute_closeout, prepare_closeout, validate_closeout


class WorkflowChildRamTests(unittest.TestCase):
    setUp = fixture.WorkflowTests.setUp
    git = fixture.WorkflowTests.git
    def admit(self, *prepared):
        # Enrollment creates the journal once; record keeping does not need
        # a new enrollment or a path grant for every native RAM operation.
        if not self.host.exists():
            fixture.WorkflowTests.admit(self, *prepared)

    def child(self, session="child"):
        prepared = prepare_closeout(self.new_values, {**self.context, "session_id": session})
        self.assertEqual(prepared["outcome"], "prepared")
        self.admit(prepared)
        result = execute_closeout(prepared, {**self.context, "session_id": session})
        self.assertEqual(result["outcome"], "completed", result)
        return {"identity": prepared["identity"], "path": prepared["record_path"],
                "revision": result["result"]["readback"]["revision"]}, prepared

    def correct_child(self, child, prepared):
        semantic = deepcopy(prepared["semantic"])
        semantic.update(operation="correct", expected_revision=child["revision"])
        semantic["experience"]["summary"] = "Agent supplied a corrected child result."
        recorded_at = companion_ram_capture.now()
        plan = ram.Store(LocalRoot(self.root)).plan(
            companion_ram_capture.freeze_request(semantic, recorded_at), "writer")
        paths = [change["path"] for change in plan["effect"]["changes"]]
        result = companion_ram_capture.capture(
            self.root, self.host, "writer", "correct-child", semantic, paths,
            self.root / ".eutonos-local/correct-child.json", recorded_at)
        self.assertEqual(result["outcome"], "completed", result)
        return {**child, "revision": result["readback"]["revision"]}

    def test_no_children_retains_summary_and_schema_has_native_reference_shape(self):
        prepared = prepare_closeout(self.new_values, self.context)
        self.assertEqual(prepared["semantic"]["experience"]["kind"], "summary")
        self.assertEqual(prepared["semantic"]["experience"]["children"], [])
        schema = _closeout_schema()["properties"]["children"]["items"]
        self.assertEqual(set(schema["required"]), {"identity", "path", "revision"})
        self.assertEqual(schema["properties"]["revision"]["minimum"], 1)

    def test_native_child_is_pinned_once_and_exact_retry_has_one_parent_version(self):
        child, _ = self.child()
        prepared = prepare_closeout({**self.new_values, "children": [child]}, self.context)
        self.assertEqual(prepared["outcome"], "prepared", prepared)
        self.assertEqual(prepared["values"]["children"], [child])
        self.assertEqual(prepared["answer_reuse"]["values"]["children"], [child])
        self.assertEqual(prepared["semantic"]["experience"]["kind"], "milestone")
        self.admit(prepared)
        result = execute_closeout(prepared, self.context)
        self.assertEqual(result["outcome"], "completed", result)
        self.assertTrue(result["child_links"]["current_verified"])
        self.assertEqual(result["child_links"]["children"], [child])
        retry = execute_closeout(prepared, self.context)
        self.assertEqual(retry["outcome"], "already-completed", retry)
        record = ram.Store(LocalRoot(self.root)).load(prepared["record_path"])
        self.assertEqual(len(record["versions"]), 1)
        self.assertEqual(record["versions"][0]["children"], [child])

    def test_raw_text_malformed_revision_duplicate_and_extra_fields_are_rejected(self):
        child, _ = self.child()
        for children in (["worker summary"], [{**child, "revision": True}], [child, child],
                         [{**child, "summary": "Do not fabricate this."}],
                         [{**child, "identity": {**child["identity"], "memory_id": "../escape"}}]):
            with self.subTest(children=children):
                checked = validate_closeout({**self.new_values, "children": children})
                self.assertEqual(checked["outcome"], "incomplete")
                self.assertIn("children", [field["name"] for field in checked["invalid_fields"]])

    def test_missing_wrong_scope_and_historical_child_fail_before_new_capture(self):
        child, child_prepared = self.child()
        updated = self.correct_child(child, child_prepared)
        absent = {**updated, "identity": {**updated["identity"], "memory_id": "missing"}}
        absent["path"] = ram.Store(LocalRoot(self.root)).path(absent["identity"])
        for reference in (child, {**updated, "path": "elsewhere/child.json"}, absent):
            with self.subTest(reference=reference):
                prepared = prepare_closeout({**self.new_values, "children": [reference]}, self.context)
                self.assertEqual(prepared["outcome"], "error", prepared)
        self.assertEqual(prepare_closeout({**self.new_values, "children": [updated]}, self.context)["outcome"], "prepared")

    def test_child_change_after_prepare_refuses_before_parent_write(self):
        child, child_prepared = self.child()
        prepared = prepare_closeout({**self.new_values, "children": [child]}, self.context)
        self.correct_child(child, child_prepared)
        result = execute_closeout(prepared, self.context)
        self.assertEqual(result["outcome"], "error", result)
        self.assertFalse((self.root / prepared["record_path"]).exists())

    def test_child_change_during_capture_is_partial_without_verified_link_claim(self):
        child, child_prepared = self.child()
        prepared = prepare_closeout({**self.new_values, "children": [child]}, self.context)
        self.admit(prepared)

        def capture_then_change(*args):
            result = companion_ram_capture.capture(*args)
            self.correct_child(child, child_prepared)
            return result

        result = execute_closeout(prepared, self.context, capture_fn=capture_then_change)
        self.assertEqual(result["outcome"], "partial", result)
        self.assertFalse(result["child_links"]["current_verified"])
        self.assertEqual(result["child_links"]["outcome"], "error")

    def test_child_change_between_workflow_check_and_native_plan_writes_no_parent(self):
        child, child_prepared = self.child()
        prepared = prepare_closeout({**self.new_values, "children": [child]}, self.context)
        self.admit(prepared)

        def change_then_capture(*args):
            self.correct_child(child, child_prepared)
            return companion_ram_capture.capture(*args)

        result = execute_closeout(prepared, self.context, capture_fn=change_then_capture)
        self.assertEqual(result["outcome"], "error", result)
        self.assertFalse((self.root / prepared["record_path"]).exists())

    def test_prepared_child_tampering_is_rejected(self):
        child, _ = self.child()
        prepared = prepare_closeout({**self.new_values, "children": [child]}, self.context)
        prepared["semantic"]["experience"]["children"] = []
        result = execute_closeout(prepared, self.context)
        self.assertEqual(result["outcome"], "error", result)
        self.assertFalse((self.root / prepared["record_path"]).exists())

    def test_suppressed_and_foreign_scope_records_are_ineligible(self):
        child, _ = self.child()
        store = ram.Store(LocalRoot(self.root))
        original = store.load(child["path"])
        for change in ("suppressed", "foreign-scope"):
            record = deepcopy(original)
            if change == "suppressed":
                record["controls"]["state"] = "suppressed"
            else:
                record["scope_id"] = "other.scope"
            (self.root / child["path"]).write_bytes(ts.encoded(record))
            prepared = prepare_closeout({**self.new_values, "children": [child]}, self.context)
            self.assertEqual(prepared["outcome"], "error", prepared)
        (self.root / child["path"]).write_bytes(ts.encoded(original))

    def test_nested_spawner_uses_milestone_child_without_copying_worker_prose(self):
        child, _ = self.child()
        parent = prepare_closeout({**self.new_values, "children": [child]}, self.context)
        self.admit(parent)
        captured = execute_closeout(parent, self.context)
        self.assertEqual(captured["outcome"], "completed", captured)
        parent_ref = {"identity": parent["identity"], "path": parent["record_path"],
                      "revision": captured["result"]["readback"]["revision"]}
        context = {**self.context, "session_id": "grandparent"}
        outer = prepare_closeout({**self.new_values, "children": [parent_ref]}, context)
        self.admit(outer)
        result = execute_closeout(outer, context)
        self.assertEqual(result["outcome"], "completed", result)
        self.assertEqual(result["child_links"]["children"], [parent_ref])
        self.assertEqual(outer["semantic"]["experience"]["summary"], parent["semantic"]["experience"]["summary"])

    def test_lifecycle_reuses_agent_supplied_child_references(self):
        child, _ = self.child()
        rows = [{"what": "Completed worker review", "why": "Owner requested review",
                 "result": "Review saved", "checks": ["Source inspected"], "operation": "update"}]
        values = work_closeout.lifecycle_memory_values(rows, {"children": [child]})
        self.assertEqual(values["children"], [child])


if __name__ == "__main__":
    unittest.main()
