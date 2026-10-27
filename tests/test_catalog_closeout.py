import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from eutonos import boards, catalog_closeout, closeout_route, work_closeout


class CatalogCloseoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.context = {"root": str(self.root)}
        (self.root / "eutonos.boards.json").write_text(json.dumps({"profile": boards.CONFIG, "owners": ["BOARD.md"]}))
        self.item = {"id": "feature-a", "type": "feature", "title": "A", "summary": "A useful thing",
                     "status": "completed", "extensions": {"category": "Observability"}}
        self.save()

    def save(self):
        (self.root / "BOARD.md").write_text("```tokenslang-board\n" + json.dumps({"profile": boards.BOARD, "items": [self.item]}) + "\n```\n")

    def test_assessment_required_before_any_closeout_phase(self):
        with patch.object(closeout_route, "_run") as run:
            result = closeout_route.run({}, self.context)
            self.assertEqual(result["phase"], "catalog")
            run.assert_not_called()
        self.assertIn("Observability", result["catalog_assessment"]["categories"])

    def test_blank_prepare_reports_all_semantic_phases_without_starting_filing(self):
        with patch("eutonos.write_retry.submit_batch") as submit:
            result = closeout_route.prepare({}, self.context)
        self.assertEqual(result["phase"], "catalog")
        self.assertEqual(result["catalog_assessment"]["outcome"], "incomplete")
        self.assertEqual(result["work_assessment"]["outcome"], "incomplete")
        self.assertEqual(result["memory_assessment"]["outcome"], "incomplete")
        self.assertIn("request", {field["name"] for field in result["memory_assessment"]["missing_fields"]})
        self.assertIn("catalog_changes with []", result["catalog_assessment"]["next"])
        submit.assert_not_called()

    def test_none_explicit_and_fields_stripped_before_memory(self):
        with patch.object(closeout_route, "_run", return_value={"outcome": "skipped"}) as run:
            result = closeout_route.run({"catalog_changes": [], "work_changes": [],
                                         "request": "Assess a no-change task", "actions": ["Checked the task"],
                                         "result": "No product change", "why": "No file changed"},
                                        {**self.context, "host": "fixture-host", "actor": "fixture",
                                         "work_id": "fixture-work", "session_id": "fixture-session"})
            self.assertEqual(result["catalog_assessment"]["outcome"], "verified")
            self.assertNotIn("catalog_changes", run.call_args.args[0])

    def test_invalid_ram_labels_refuse_prepare_and_direct_run_before_board_submit(self):
        row = {"item_id": "quick-a", "operation": "update", "what": "Record completion",
               "why": "Checked", "result": "Filed", "checks": ["Focused check"]}
        values = {"catalog_changes": [], "work_changes": [], "work_size": "small",
                  "event": "closeout", "work_closeout": [row]}
        lifecycle = {"outcome": "verified", "requests": [row], "next": "Continue"}
        with patch.object(work_closeout, "lifecycle_assessment", return_value=lifecycle), \
                patch("eutonos.write_retry.submit_batch") as submit:
            for operation in (closeout_route.prepare, closeout_route.run):
                with self.subTest(operation=operation.__name__):
                    result = operation(values, self.context)
                    self.assertEqual(result["phase"], "memory")
                    self.assertEqual(result["memory_assessment"]["outcome"], "incomplete")
                    self.assertEqual({field["name"] for field in result["memory_assessment"]["invalid_fields"]},
                                     {"work_size", "event"})
            submit.assert_not_called()

    def test_missing_session_and_invalid_explicit_lifecycle_answers_refuse_filing(self):
        row = {"item_id": "quick-a", "operation": "update", "what": "Record completion",
               "why": "Checked", "result": "Filed", "checks": ["Focused check"]}
        lifecycle = {"outcome": "verified", "requests": [row], "next": "Continue"}
        base = {"catalog_changes": [], "work_changes": [], "work_closeout": [row]}
        context = {**self.context, "host": "fixture-host", "actor": "fixture", "work_id": "quick-a"}
        with patch.object(work_closeout, "lifecycle_assessment", return_value=lifecycle), \
                patch("eutonos.write_retry.submit_batch") as submit:
            for operation in (closeout_route.prepare, closeout_route.run):
                with self.subTest(operation=operation.__name__):
                    result = operation(base, context)
                    self.assertEqual(result["phase"], "context")
                    self.assertEqual({item["name"] for item in result["missing_context"]}, {"session_id"})
            complete_context = {**context, "session_id": "child-session"}
            invalid_context = {**complete_context, "session_id": "../../not-a-session"}
            result = closeout_route.run(base, invalid_context)
            self.assertEqual(result["phase"], "context")
            self.assertIn("session_id", {item["name"] for item in result["missing_context"]})
            for override in ({"request": " "}, {"request": 17}, {"result": ""},
                             {"why": 17}, {"actions": []}, {"checks": "oops"},
                             {"checks": {"unexpected": "mapping"}}):
                with self.subTest(override=override):
                    result = closeout_route.run({**base, **override}, complete_context)
                    self.assertEqual(result["phase"], "memory")
                    self.assertEqual(result["memory_assessment"]["outcome"], "incomplete")
            submit.assert_not_called()

    def test_missing_optional_ram_configuration_does_not_block_native_filing(self):
        row = {"item_id": "quick-a", "operation": "update", "what": "Record completion",
               "why": "Checked", "result": "Filed", "checks": ["Focused check"]}
        lifecycle = {"outcome": "verified", "requests": [row], "next": "Continue"}
        values = {"catalog_changes": [], "work_changes": [], "work_closeout": [row]}
        context = {**self.context, "host": "fixture-host", "actor": "fixture",
                   "work_id": "quick-a", "session_id": "child-session"}
        with patch.object(work_closeout, "lifecycle_assessment", return_value=lifecycle), \
                patch("eutonos.write_retry.submit_batch", return_value={"outcome": "complete", "request_id": "filed"}) as submit:
            result = closeout_route.run(values, context)
        submit.assert_called_once()
        self.assertEqual(result["organization"]["outcome"], "completed")
        self.assertEqual(result["memory_preparation"]["code"], "not-configured")
        self.assertEqual(result["phase"], "ram-capture")
        self.assertEqual(result["memory_recovery"]["mode"], "prepare-memory-only")

    def test_ram_summary_overflow_refuses_filing(self):
        row = {"item_id": "quick-a", "operation": "update", "what": "Record completion",
               "why": "Checked", "result": "Filed", "checks": ["Focused check"]}
        lifecycle = {"outcome": "verified", "requests": [row], "next": "Continue"}
        values = {"catalog_changes": [], "work_changes": [], "work_closeout": [row]}
        context = {**self.context, "host": "fixture-host", "actor": "fixture",
                   "work_id": "quick-a", "session_id": "child-session"}
        with patch.object(work_closeout, "lifecycle_assessment", return_value=lifecycle), \
                patch.object(closeout_route, "prepare_memory", return_value={"outcome": "incomplete", "code": "overflow"}), \
                patch("eutonos.write_retry.submit_batch") as submit:
            result = closeout_route.run(values, context)
        self.assertEqual(result["phase"], "memory-preparation")
        self.assertEqual(result["memory_preparation"]["code"], "overflow")
        submit.assert_not_called()

    def test_unsafe_changed_path_refuses_prepare_and_run_before_filing(self):
        row = {"item_id": "quick-a", "operation": "update", "what": "Record completion",
               "why": "Checked", "result": "Filed", "checks": ["Focused check"]}
        lifecycle = {"outcome": "verified", "requests": [row], "next": "Continue"}
        values = {"catalog_changes": [], "work_changes": [], "work_closeout": [row],
                  "changed_paths": ["../escape"]}
        context = {**self.context, "host": "fixture-host", "actor": "fixture",
                   "work_id": "quick-a", "session_id": "child-session"}
        with patch.object(work_closeout, "lifecycle_assessment", return_value=lifecycle), \
                patch("eutonos.write_retry.submit_batch") as submit:
            for operation in (closeout_route.prepare, closeout_route.run):
                with self.subTest(operation=operation.__name__):
                    result = operation(values, context)
                    self.assertEqual(result["phase"], "memory")
                    self.assertIn("changed_paths", {field["name"] for field in result["memory_assessment"]["invalid_fields"]})
            submit.assert_not_called()

    def test_changes_require_saved_ids_and_categories(self):
        for ids in ([], ["missing"]):
            result = catalog_closeout.assess({"catalog_changes": ["Added A"], "catalog_ids": ids}, self.root)
            self.assertEqual(result["outcome"], "incomplete")
        value = {"catalog_changes": ["Added A"], "catalog_ids": ["feature-a"]}
        self.assertEqual(catalog_closeout.assess(value, self.root)["outcome"], "verified")
        self.item["extensions"] = {}; self.save()
        self.assertEqual(catalog_closeout.assess(value, self.root)["outcome"], "incomplete")

    def test_prepared_assessment_is_rechecked(self):
        value = {"catalog_assessment": {"catalog_changes": ["Added A"], "catalog_ids": ["feature-a"]}}
        self.assertEqual(catalog_closeout.assess(value, self.root)["outcome"], "verified")
        self.item["id"] = "changed"; self.save()
        self.assertEqual(catalog_closeout.assess(value, self.root)["outcome"], "incomplete")

    def test_other_resolves_to_reusable_category(self):
        self.item["extensions"] = {"category": "Other", "category_other": " Release engineering "}
        boards._validate_catalog_fields(self.item)
        self.assertEqual(self.item["extensions"], {"category": "Release engineering"})
        self.save()
        form = boards.catalog_form("capability", root=self.root)
        self.assertIn("Release engineering", form["categories"])
        self.assertIn("Other", form["categories"])
        self.assertIn("batch form", form["next"]["select"])
        self.assertIn("batch submit", form["next"]["submit"])
        self.assertEqual(form["batch_operation"]["kind"], "add")
        self.assertNotIn("id", form["batch_operation"]["request"]["items"][0])
        form = boards.catalog_form("feature", {"extensions": {"category": "Other"}}, self.root)
        self.assertIn("extensions.category_other", form["missing_fields"])

    def test_mid_slice_additions_require_saved_work_ids(self):
        result = closeout_route.run({"catalog_changes": [], "work_changes": ["Added scope"]}, self.context)
        self.assertEqual(result["phase"], "work")
        self.assertIn("work_ids", result["work_assessment"]["missing_fields"])
        work = {"id": "quick-a", "type": "quick", "title": "Added scope",
                "summary": "Record the added scope immediately.", "status": "active"}
        board = json.loads((self.root / "BOARD.md").read_text().split("```tokenslang-board\n", 1)[1].split("\n```", 1)[0])
        board["items"].append(work)
        (self.root / "BOARD.md").write_text("```tokenslang-board\n" + json.dumps(board) + "\n```\n")
        assessed = work_closeout.assess({"work_changes": ["Added scope"], "work_ids": ["quick-a"]}, self.root)
        self.assertEqual(assessed["outcome"], "verified")


if __name__ == "__main__":
    unittest.main()
