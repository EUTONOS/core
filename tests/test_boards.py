import json
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

from eutonos import boards, boards_cli, completion_checks, goal_records, tokenslang as ts, work_attribution
from eutonos.effects import LocalRoot, digest
from eutonos.document_views import boards_view
from eutonos.host import connect, enroll, selected_apply


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="boards-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.root = self.home / "project"; self.root.mkdir()
        (self.root / "eutonos.boards.json").write_text(json.dumps({
            "profile": boards.CONFIG, "owners": ["BOARD.md"]}) + "\n", encoding="utf-8")
        (self.root / "BOARD.md").write_bytes(b"# Native board\r\n\r\nLegacy prose stays exact.\r\n")
        self.git("init", "-q", "-b", "main"); self.git("add", "--all")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qm", "baseline")
        self.local = LocalRoot(self.root)
        self.host_number = 0

    def test_completion_checks_stamp_reset_and_required_gate(self):
        actor = work_attribution.unknown_snapshot("fixture-writer", "2026-10-11T00:00:00Z")
        check = {"id": "proof-1", "title": "Source is integrated", "required": True,
                 "state": "partial", "evidence": [], "related_work_ids": ["slice-1"],
                 "remaining_action": "Verify target readback."}
        pending = completion_checks.reconcile([check], actor=actor)
        with self.assertRaises(ts.ReadError):
            completion_checks.require_complete({"extensions": {"completion_checks": pending}})
        verified = completion_checks.reconcile([{**check, "state": "verified",
            "evidence": [{"path": "docs/proof.md"}]}], pending, actor)
        self.assertEqual(verified[0]["verification"]["actor"]["event_id"], actor["event_id"])
        self.assertEqual(completion_checks.summary(verified)["required_verified"], 1)
        shown = boards_view({"status": "available", "items": [{
            "id": "W", "type": "task", "title": "Work", "summary": "Scope",
            "status": "active", "extensions": {"completion_checks": verified}}]})
        self.assertEqual(shown["items"][0]["completion_check_summary"]["required_verified"], 1)
        completion_checks.require_complete({"extensions": {"completion_checks": verified}})
        revised = completion_checks.reconcile([{**verified[0], "title": "Target is integrated",
            "state": "partial", "remaining_action": "Recheck target readback."}], verified, actor)
        self.assertNotIn("verification", revised[0])
        self.assertEqual(revised[0]["verification_history"][0]["reason"], "definition-changed")
        for malformed in (None, [None], [{"id": []}], [{**check, "id": []}],
                          [{**check, "verification": {"forged": True}}]):
            with self.subTest(malformed=malformed), self.assertRaises(ts.ReadError):
                completion_checks.reconcile(malformed, actor=actor)

    def test_completion_checks_generic_board_update_cannot_erase_or_complete_unverified(self):
        check = {"id": "proof-1", "title": "Acceptance", "required": True,
                 "state": "pending", "evidence": [], "related_work_ids": []}
        item = self.item("F", "feature")
        item["extensions"] = {"completion_checks": [check], "category": "Workflow"}
        self.write_board([item])
        raw = (self.root / "BOARD.md").read_bytes()
        common = {"profile": boards.UPDATE, "owner_path": "BOARD.md",
                  "expected_sha256": digest(raw), "item_id": "F"}
        for changes in ({"status": "completed"},
                        {"extensions": {"category": "Workflow"}},
                        {"extensions": None}):
            with self.subTest(changes=changes), self.assertRaises(ts.ReadError):
                boards.plan_update(self.root, {**common, "changes": changes})
        self.assertEqual(boards.project(self.root)["items"][0]["extensions"]["completion_checks"][0]["id"], "proof-1")

    def test_optional_execution_recommendation_forms_and_validation(self):
        stage = boards.work_form("stage", {"id": "S", "title": "Stage", "summary": "Scope", "status": "planned", "parent_id": "C"})
        self.assertEqual(stage["missing_fields"], [])
        self.assertNotIn("extensions", stage["batch_operation"]["request"]["items"][0])
        recommendation = {"model": " future ", "effort": " careful ", "basis": " bounded "}
        sliced = boards.work_form("slice", {"title": "Slice", "summary": "Scope", "status": "planned", "extensions": {"execution_recommendation": recommendation}})
        self.assertEqual(sliced["batch_operation"]["request"]["items"][0]["extensions"]["execution_recommendation"],
                         {"model": "future", "effort": "careful", "basis": "bounded", "advisory": True})
        for invalid in (None, {**recommendation, "model": " "}, {**recommendation, "advisory": False}):
            with self.subTest(invalid=invalid), self.assertRaises(ts.ReadError):
                boards.work_form("slice", {"title": "Slice", "summary": "Scope", "status": "planned", "extensions": {"execution_recommendation": invalid}})
        for extensions in (None, [], 1):
            with self.subTest(extensions=extensions), self.assertRaises(ts.ReadError):
                boards.work_form("slice", {"extensions": extensions})
        for kind in ("campaign", "quick"):
            with self.subTest(kind=kind), self.assertRaises(ts.ReadError):
                boards._validate_written_work_item({"type": kind, "extensions": {"execution_recommendation": recommendation}})

    def git(self, *args):
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        return subprocess.check_output(["git", "-c", "safe.directory=" + str(self.root), "-C", str(self.root),
                                        *args], env=env, stderr=subprocess.PIPE)

    def item(self, item_id, item_type="task", status="planned", parent=None):
        value = {"id": item_id, "type": item_type, "title": "Title " + item_id,
                 "summary": "Summary " + item_id, "status": status}
        if parent:
            value["parent_id"] = parent
        return value

    def write_board(self, items, prefix=b"Before prose.\n", suffix=b"\nAfter prose.\n", extensions=None):
        value = {"profile": boards.BOARD, "items": items}
        if extensions is not None:
            value["extensions"] = extensions
        block = b"```tokenslang-board\n" + json.dumps(value, sort_keys=True).encode() + b"\n```"
        (self.root / "BOARD.md").write_bytes(prefix + block + suffix)
        return prefix, suffix

    def apply(self, planned):
        self.host_number += 1
        host = self.home / ("host-" + str(self.host_number))
        enroll(self.root, host, "owner", planned["paths"])
        coordinator, authority = connect(self.root, host, "owner")
        attempt = "board-" + str(self.host_number)
        generation = coordinator.start(authority, attempt)
        coordinator.reserve(authority, attempt, generation, planned["paths"])
        return selected_apply(coordinator, authority, attempt, generation, "board-effect",
                              planned["effect"], planned["fingerprint"])

    def test_not_configured_and_legacy_projection_do_not_infer_prose(self):
        other = self.home / "other"; other.mkdir()
        self.assertEqual(boards.project(other)["status"], "not-configured")
        result = boards.project(self.root)
        self.assertEqual((result["status"], result["items"]), ("partial", []))
        self.assertEqual(result["owners"][0]["status"], "legacy")
        self.assertIn("prose was not interpreted", result["diagnostics"][0])

    def test_goal_api_freezes_identity_and_rejects_unrelated_batch_operations(self):
        draft = {'title': 'Shared outcome', 'summary': 'Scope', 'extensions': {'goal': {
            'profile': goal_records.GOAL, 'outcome': 'Outcome', 'criteria': [{'key': 'result',
                'statement': 'Result exists', 'evidence_refs': [], 'assessment': 'unassessed'}],
            'scope': [], 'exclusions': [], 'work_links': []}}}
        guided = goal_records.form_shared(self.root, 'BOARD.md', draft=draft)
        self.assertEqual(guided['missing_fields'], [])
        request = guided['request']
        planned = boards.plan_add(self.root, {'profile': boards.ADD, **request['operations'][0]['request']})
        self.assertEqual(planned['paths'], ['BOARD.md'])
        self.assertTrue(guided['item']['id'].endswith(guided['item']['extensions']['creation_token']))
        with patch('eutonos.write_retry.submit_batch', return_value={'outcome': 'queued'}) as submit:
            result = goal_records.submit_shared(self.root, request, host=self.home / 'host', actor='actor', session='session')
            self.assertEqual(result['outcome'], 'queued')
            self.assertNotIn('goal_preconditions', submit.call_args.args[3])
            request['operations'].append({'kind': 'work', 'request': {'operation': 'complete', 'item_id': 'other'}})
            with self.assertRaises(ts.ReadError):
                goal_records.submit_shared(self.root, request, host=self.home / 'host', actor='actor', session='session')
            self.assertEqual(submit.call_count, 1)

    def test_new_semantic_goal_rejects_path_derived_or_invalid_creation_identity(self):
        item = goal_records.form('goal')['item']
        item.update(title='Goal', summary='Scope')
        semantic = item['extensions']['goal']
        semantic['outcome'] = 'Outcome'; semantic['criteria'][0]['statement'] = 'Result'
        for token in ('invalid', None):
            if token is None:
                item['extensions'].pop('creation_token', None)
            else:
                item['extensions']['creation_token'] = token
            with self.assertRaises(ts.ReadError):
                boards.plan_add(self.root, {'profile': boards.ADD, 'owner_path': 'BOARD.md',
                    'expected_sha256': digest(self.local.read('BOARD.md')), 'index': 0, 'item': item})

    def test_semantic_goal_add_preserves_existing_work_ids_status_and_order(self):
        from eutonos import work_lifecycle as work
        self.write_board([self.item('current-task', 'task', 'active'), self.item('next-task', 'task', 'planned')])
        work.folders(self.root)
        order = {'profile': work.CONFIG, 'current': ['current-task'], 'next': ['next-task']}
        (self.root / work.PATH).write_bytes(boards._json(order) + b'\n')
        item = goal_records.form('goal', {'title': 'Outcome', 'summary': 'Scope'})['item']
        item['extensions']['goal']['outcome'] = 'Outcome'
        item['extensions']['goal']['criteria'][0]['statement'] = 'Result exists'
        plan = boards.plan_add(self.root, {'profile': boards.ADD, 'owner_path': 'BOARD.md',
            'expected_sha256': digest(self.local.read('BOARD.md')), 'index': 2, 'item': item})
        self.assertNotIn(work.PATH, plan['paths'])
        from eutonos.effects import decode
        desired = next(decode(c['after']) for c in plan['effect']['changes'] if c['path'] == 'BOARD.md')
        board, _ = boards._parse(desired, 'BOARD.md')
        self.assertEqual([(i['id'], i['status']) for i in board['items'][:2]], [('current-task', 'active'), ('next-task', 'planned')])

    def test_batch_add_preserves_legacy_prose_and_counts_direct_stages(self):
        before = (self.root / "BOARD.md").read_bytes()
        campaign = self.item("EFX", "campaign", "active")
        slices = [self.item("EFX" + str(index), "stage", "completed" if index <= 2 else "active", "EFX")
                  for index in range(1, 6)]
        request = {"profile": boards.ADD, "owner_path": "BOARD.md", "expected_sha256": digest(before),
                   "index": 0, "items": [campaign, *slices]}
        planned = boards.plan_add(self.root, request)
        self.assertEqual(planned["item_ids"], ["EFX", "EFX1", "EFX2", "EFX3", "EFX4", "EFX5"])
        self.apply(planned)
        self.assertTrue((self.root / "BOARD.md").read_bytes().startswith(before))
        result = boards.project(self.root)
        self.assertEqual(result["status"], "available")
        campaign_row = next(item for item in result["items"] if item["id"] == "EFX")
        self.assertEqual(campaign_row["child_counts"], {"completed": 2, "total": 5})

    def test_update_preserves_prose_extensions_ids_and_stale_plan_is_rejected(self):
        prefix, suffix = self.write_board([{**self.item("TASK-1"), "extensions": {"native": {"keep": True}}}],
                                          extensions={"owner": "native"})
        raw = self.local.read("BOARD.md")
        planned = boards.plan_update(self.root, {"profile": boards.UPDATE, "owner_path": "BOARD.md",
            "expected_sha256": digest(raw), "item_id": "TASK-1", "changes": {
                "status": "completed", "next_action": "Review evidence."}})
        self.apply(planned)
        changed = self.local.read("BOARD.md")
        self.assertTrue(changed.startswith(prefix)); self.assertTrue(changed.endswith(suffix))
        row = boards.project(self.root)["items"][0]
        self.assertEqual((row["id"], row["status"], row["extensions"]["native"]),
                         ("TASK-1", "completed", {"keep": True}))
        self.assertEqual(set(row['extensions']), {'native', 'attribution'})
        self.assertEqual(row['extensions']['attribution']['creator'], None)
        self.assertEqual([e['kind'] for e in row['extensions']['attribution']['events']], ['completed'])
        parsed, _ = boards._parse(changed, "BOARD.md")
        self.assertEqual(parsed["extensions"], {"owner": "native"})
        stale = boards.plan_update(self.root, {"profile": boards.UPDATE, "owner_path": "BOARD.md",
            "expected_sha256": digest(changed), "item_id": "TASK-1", "changes": {"status": "deferred"}})
        (self.root / "BOARD.md").write_bytes(changed + b"Concurrent prose.\n")
        with self.assertRaises(ts.ReadError) as caught:
            self.apply(stale)
        self.assertEqual(caught.exception.code, "stale")

    def test_reorder_requires_exact_ids_and_preserves_stable_ids(self):
        self.write_board([self.item("A"), self.item("B"), self.item("C")])
        raw = self.local.read("BOARD.md")
        with self.assertRaises(ts.ReadError):
            boards.plan_reorder(self.root, {"profile": boards.REORDER, "owner_path": "BOARD.md",
                "expected_sha256": digest(raw), "item_ids": ["A", "B"]})
        planned = boards.plan_reorder(self.root, {"profile": boards.REORDER, "owner_path": "BOARD.md",
            "expected_sha256": digest(raw), "item_ids": ["C", "A", "B"]})
        self.apply(planned)
        self.assertEqual([item["id"] for item in boards.project(self.root)["items"]], ["C", "A", "B"])

    def test_invalid_graph_and_malformed_fence_are_diagnostic_and_not_repaired(self):
        self.write_board([self.item("A", parent="missing")])
        result = boards.project(self.root)
        self.assertEqual((result["status"], result["items"]), ("partial", []))
        malformed = b"# Owner\n```tokenslang-board\n{\"profile\":\"eutonos.tokenslang.board.v1\"}\n"
        (self.root / "BOARD.md").write_bytes(malformed)
        result = boards.project(self.root)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["owners"][0]["status"], "invalid")
        self.assertEqual(result["owners"][0]["sha256"], digest(malformed))
        with self.assertRaises(ts.ReadError):
            boards.plan_add(self.root, {"profile": boards.ADD, "owner_path": "BOARD.md",
                "expected_sha256": digest(malformed), "index": 0, "item": self.item("SAFE")})
        self.assertEqual((self.root / "BOARD.md").read_bytes(), malformed)

    def test_duplicate_ids_and_parent_cycles_are_rejected(self):
        for items in ([self.item("A"), self.item("A")],
                      [self.item("A", parent="B"), self.item("B", parent="A")]):
            with self.subTest(items=items):
                self.write_board(items)
                self.assertEqual(boards.project(self.root)["status"], "partial")
                raw = self.local.read("BOARD.md")
                with self.assertRaises(ts.ReadError):
                    boards.plan_add(self.root, {"profile": boards.ADD, "owner_path": "BOARD.md",
                        "expected_sha256": digest(raw), "index": len(items), "item": self.item("NEW")})

    def test_other_owner_change_makes_cross_owner_plan_stale(self):
        (self.root / "eutonos.boards.json").write_text(json.dumps({
            "profile": boards.CONFIG, "owners": ["BOARD.md", "OTHER.md"]}), encoding="utf-8")
        self.write_board([self.item("ROOT", "campaign")])
        other_board = {"profile": boards.BOARD, "items": [self.item("CHILD", "slice", parent="ROOT")]}
        (self.root / "OTHER.md").write_bytes(b"```tokenslang-board\n" + json.dumps(other_board).encode() + b"\n```\n")
        raw = self.local.read("BOARD.md")
        planned = boards.plan_update(self.root, {"profile": boards.UPDATE, "owner_path": "BOARD.md",
            "expected_sha256": digest(raw), "item_id": "ROOT", "changes": {"summary": "Updated."}})
        (self.root / "OTHER.md").write_bytes((self.root / "OTHER.md").read_bytes() + b"Changed.\n")
        with self.assertRaises(ts.ReadError) as caught:
            self.apply(planned)
        self.assertEqual(caught.exception.code, "stale")

    def test_more_than_512_items_have_no_board_wide_cap(self):
        self.write_board([self.item("TASK-" + str(index)) for index in range(600)])
        result = boards.project(self.root)
        self.assertEqual((result["status"], len(result["items"])), ("available", 600))

    def test_cli_profile_exposes_complete_forms_and_read_is_json(self):
        description = boards.profile_description()
        self.assertEqual(set(description["plans"]), {"add", "update", "reorder", "move"})
        self.assertIn("expected_sha256", description["plans"]["add"])
        self.assertIn("--fingerprint", description["commands"]["apply"])
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(boards_cli.main(["read", "--root", str(self.root)]), 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "partial")


if __name__ == "__main__":
    unittest.main()
