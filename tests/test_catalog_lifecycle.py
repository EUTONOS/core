"""Future to current catalogue lifecycle: queued while active, one placement rule when completed."""
import json
import tempfile
import unittest
from pathlib import Path

from eutonos import boards, current_work, repository_state, work_lifecycle as work, write_batch
from eutonos import tokenslang as ts
from eutonos.effects import LocalRoot, decode, digest

FUTURE_F, FUTURE_C = "docs/FUTURE_FEATURES.md", "docs/FUTURE_CAPABILITIES.md"
CURRENT_F, CURRENT_C = "docs/CURRENT_FEATURES.md", "docs/CURRENT_CAPABILITIES.md"
WORK_ITEMS = "docs/WORK_ITEMS.md"
ROLES = {FUTURE_F: "future-feature-catalogue", FUTURE_C: "future-capability-catalogue",
         CURRENT_F: "current-feature-catalogue", CURRENT_C: "current-capability-catalogue"}
REPO = Path(__file__).resolve().parents[1]


def fence(items):
    return b"```tokenslang-board\n" + boards._json({"profile": boards.BOARD, "items": items}) + b"\n```\n"


def entry(identity, kind, status, **extra):
    row = {"id": identity, "type": kind, "title": identity.title(), "summary": "Summary of " + identity, "status": status}
    if kind in boards.CATEGORY_TYPES:
        row["extensions"] = {"category": "Workflow"}
    return {**row, **extra}


class CatalogLifecycleBase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="catalog-lifecycle-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / "docs").mkdir()
        self.write(FUTURE_F, [
            entry("feature-planned", "feature", "planned"), entry("feature-deferred", "feature", "deferred"),
            entry("feature-active", "feature", "active"), entry("feature-blocked", "feature", "blocked"),
            entry("feature-retired", "feature", "superseded"), entry("slice-future", "slice", "planned")])
        self.write(FUTURE_C, [entry("cap-planned", "capability", "planned"), entry("cap-active", "capability", "active")])
        self.write(CURRENT_F, [entry("feature-shipped", "feature", "completed")])
        self.write(CURRENT_C, [entry("cap-shipped", "capability", "completed")])
        self.write(WORK_ITEMS, [entry("slice-live", "slice", "active"), entry("slice-later", "slice", "planned")])
        self.registry(ROLES)
        self.config([FUTURE_F, FUTURE_C, CURRENT_F, CURRENT_C, WORK_ITEMS])
        (self.root / work.PATH).write_text(json.dumps({"profile": work.CONFIG, "current": [], "next": []}) + "\n")
        work.folders(self.root)

    def write(self, path, items):
        (self.root / path).write_bytes(b"# Owner\n\nProse stays exact.\n\n" + fence(items))

    def registry(self, roles):
        documents = [{"id": "eutonos.doc.cat-" + str(n), "path": path, "role": role}
                     for n, (path, role) in enumerate(roles.items())]
        (self.root / "docs/REGISTRY.json").write_text(json.dumps({"schema": "x", "documents": documents}) + "\n")

    def config(self, owners):
        (self.root / "eutonos.boards.json").write_text(json.dumps({"profile": boards.CONFIG, "owners": owners}) + "\n")

    def sha(self, path):
        target = self.root / path
        return digest(target.read_bytes()) if target.exists() else "1" * 64

    def update(self, owner, item_id, changes):
        return {"kind": "update", "request": {"owner_path": owner, "expected_sha256": self.sha(owner),
                                              "item_id": item_id, "changes": changes}}

    def add(self, owner, items, index=0):
        return {"kind": "add", "request": {"owner_path": owner, "expected_sha256": self.sha(owner),
                                           "index": index, "items": items}}

    def move(self, item_id, source, target, index=0, **extra):
        return {"kind": "move", "request": {
            "from_owner_path": source, "from_expected_sha256": self.sha(source),
            "to_owner_path": target, "to_expected_sha256": self.sha(target), "item_id": item_id,
            "index": index, **extra}}

    def plan(self, *operations):
        return write_batch.plan(self.root, {"profile": write_batch.PROFILE, "operations": list(operations)})

    def after(self, planned, path):
        for change in planned["effect"]["changes"]:
            if change["path"] == path:
                return decode(change["after"])
        raise AssertionError("path not written: " + path)

    def items(self, planned, path):
        return boards._parse(self.after(planned, path), path)[0]["items"]

    def projected(self, identity):
        return next(i for i in boards.project(self.root)["items"] if i["id"] == identity)

    def refused(self, *operations):
        with self.assertRaises(ts.ReadError) as raised:
            self.plan(*operations)
        self.assertEqual(raised.exception.code, "invalid")
        return str(raised.exception)

    def generated(self):
        local = LocalRoot(self.root)
        entries = boards._load(local)[3]
        updates = {}
        work.views(local, entries, work.load_order(local), updates)
        return {path: raw.decode("utf-8") for path, raw in updates.items()}


class QueuedViewsTests(CatalogLifecycleBase):
    """Item 1: active and blocked catalogue items are Current Tasks; nothing else about the queue changes."""

    def test_queued_covers_only_active_and_blocked_features_and_capabilities(self):
        self.assertTrue(work.queued(entry("a", "feature", "active")))
        self.assertTrue(work.queued(entry("a", "capability", "blocked")))
        for status in ("planned", "deferred", "completed", "cancelled", "superseded"):
            self.assertFalse(work.queued(entry("a", "feature", status)), status)
        self.assertFalse(work.queued(entry("a", "slice", "active")))
        self.assertFalse(work.queued(entry("a", "feature", "active", parent_id="x")))
        self.assertFalse(work.queued(entry("a", "feature", "active", extensions={"source_import": {}})))

    def test_current_tasks_lists_active_and_blocked_items_with_their_kind(self):
        views = self.generated()
        current = views[work.CURRENT]
        self.assertIn("feature-active", current)
        self.assertIn("feature-blocked", current)
        self.assertIn("cap-active", current)
        self.assertRegex(current, r"feature-active.*\) · feature \| active \|")
        self.assertRegex(current, r"cap-active.*\) · capability \| active \|")
        self.assertRegex(current, r"feature-blocked.*\) · feature \| blocked \|")
        self.assertIn("slice-live", current)
        self.assertNotRegex(current, r"slice-live[^|]*\) · (feature|capability)")
        self.assertIn("(FUTURE_FEATURES.md)", current)  # the row links the owner that still holds the ID

    def test_future_completed_and_historical_items_stay_off_the_work_queue(self):
        views = self.generated()
        listed = views[work.CURRENT] + views[work.NEXT] + views[work.ORDER] + views[work.DONE] + views[work.DONE_CAMPAIGNS]
        for identity in ("feature-planned", "feature-deferred", "feature-retired", "feature-shipped",
                         "cap-planned", "cap-shipped"):
            self.assertNotIn(identity, listed, identity)
        # Ordinary planned work in a catalogue owner is still Next Work, exactly as before.
        self.assertIn("slice-later", views[work.NEXT])
        self.assertIn("slice-future", views[work.NEXT])
        self.assertNotIn("feature-active", views[work.NEXT] + views[work.DONE])

    def test_work_order_file_and_table_put_queued_items_in_the_current_lane(self):
        views = self.generated()
        saved = json.loads(views[work.PATH])
        self.assertEqual(sorted(saved["current"]), sorted(["feature-active", "feature-blocked", "cap-active", "slice-live"]))
        self.assertEqual(sorted(saved["next"]), ["slice-future", "slice-later"])
        table = views[work.ORDER].split("## Next Work")[0]
        self.assertRegex(table, r"feature-active.*\) · feature \| active")
        self.assertRegex(table, r"cap-active.*\) · capability \| active")

    def test_ordered_keeps_a_saved_position_and_drops_an_item_that_stops_being_active(self):
        items = [{**i, "owner_path": path} for path, board in boards._load(LocalRoot(self.root))[3].items() for i in board["items"]]
        saved = {"profile": work.CONFIG, "current": ["cap-active", "slice-live", "feature-planned"], "next": ["slice-later"]}
        result = work.ordered(saved, items)
        self.assertEqual(result["current"][:2], ["cap-active", "slice-live"])  # saved order wins
        self.assertNotIn("feature-planned", result["current"] + result["next"])  # planned is not live work
        self.assertEqual(sorted(result["current"]), sorted(["cap-active", "slice-live", "feature-active", "feature-blocked"]))
        shown = work.view_order(result, items)
        self.assertEqual(shown["current"][:2], ["cap-active", "slice-live"])
        self.assertEqual(sorted(shown["next"]), ["slice-future", "slice-later"])

    def test_projection_reports_the_work_lane_and_catalogue_section(self):
        active, planned = self.projected("feature-active"), self.projected("feature-planned")
        self.assertEqual((active["work_lane"], active["catalog_section"]), ("current", "active"))
        self.assertEqual(self.projected("feature-blocked")["work_lane"], "current")
        self.assertEqual(self.projected("cap-active")["catalog_section"], "active")
        self.assertEqual(planned["catalog_section"], "future")
        self.assertIsNone(planned["work_lane"])
        self.assertEqual(self.projected("feature-deferred")["catalog_section"], "future")
        self.assertEqual(self.projected("feature-shipped")["catalog_section"], "current")
        self.assertEqual(self.projected("feature-retired")["catalog_section"], "historical")
        self.assertIn("feature-active", boards.project(self.root)["work_order"]["current"])
        self.assertNotIn("feature-planned", boards.project(self.root)["work_order"]["next"])

    def test_starting_work_in_a_batch_queues_the_item_and_stopping_it_removes_it(self):
        start = self.plan(self.update(FUTURE_F, "feature-planned", {"status": "active"}))
        self.assertIn("feature-planned", self.after(start, work.CURRENT).decode())
        self.assertIn("feature-planned", json.loads(self.after(start, work.PATH))["current"])
        self.assertEqual(self.items(start, FUTURE_F)[0]["id"], "feature-planned")  # the ID stays where it is
        stop = self.plan(self.update(FUTURE_F, "feature-active", {"status": "deferred"}))
        self.assertNotIn("feature-active", self.after(stop, work.CURRENT).decode())
        self.assertNotIn("feature-active", json.loads(self.after(stop, work.PATH))["current"])

    def shared_rows(self):
        return [{**item, "source_id": "main", "provenance": [{"ref": "refs/heads/main", "commit": "0" * 40,
                                                              "source_kind": "ref"}]}
                for item in boards.project(self.root)["items"]]

    def test_consolidated_current_work_orders_queued_catalogue_items_and_compacts_them(self):
        order = work.load_order(LocalRoot(self.root))
        board = current_work._board(self.shared_rows(), [], order, basis="test")
        self.assertEqual(sorted(board["work_order"]["current"]),
                         sorted(["feature-active", "feature-blocked", "cap-active", "slice-live"]))
        self.assertEqual(sorted(board["work_order"]["next"]), ["slice-future", "slice-later"])
        lanes = {item["id"]: item["work_lane"] for item in board["items"]}
        self.assertEqual((lanes["feature-active"], lanes["cap-active"]), ("current", "current"))
        self.assertIsNone(lanes["feature-planned"])
        self.assertEqual(lanes["feature-shipped"], "done")
        compact = current_work.compact({"boards": board, "status": "available"})
        listed = {item["id"] for item in compact["current"]}
        self.assertEqual(listed, {"feature-active", "feature-blocked", "cap-active", "slice-live"})

    def test_repository_state_compact_lists_active_catalogue_items_only(self):
        def row(item):
            return {**item, "source_integration": "main", "record_on_main": False,
                    "provenance": [{"ref": "refs/heads/work", "commit": "1" * 40, "source_kind": "ref"}]}
        rows = [row(item) for item in boards.project(self.root)["items"]]
        result = {"status": "available", "observed_at": "2026-10-23T00:00:00Z",
                  "observation_freshness": {"kind": "fresh"}, "coverage": {}, "diagnostics": [],
                  "main": {"ref": "refs/heads/main", "commit": "0" * 40, "freshness": "fresh", "items": []},
                  "latest": {"items": rows, "completed_unintegrated": [], "conflicts": [],
                             "historical_candidates": [], "unclassified_changes": []}}
        listed = {item["id"] for item in repository_state.compact(result)["latest"]["current"]}
        self.assertEqual(listed, {"feature-active", "feature-blocked", "cap-active", "slice-live"})


class PlacementRuleTests(CatalogLifecycleBase):
    """Item 3: a completed feature or capability must end up in its Current owner in the same batch."""

    def test_a_lone_completion_in_a_future_owner_is_refused_naming_the_target(self):
        message = self.refused(self.update(FUTURE_F, "feature-active", {"status": "completed"}))
        self.assertIn("feature-active", message)
        self.assertIn(CURRENT_F, message)
        self.assertIn("move", message)
        message = self.refused(self.update(FUTURE_C, "cap-active", {"status": "completed"}))
        self.assertIn(CURRENT_C, message)

    def test_completion_with_its_move_lands_in_the_current_owner_in_either_order(self):
        for first in ("update", "move"):
            with self.subTest(first=first):
                moved = self.move("feature-active", FUTURE_F, CURRENT_F)
                # After the move the record lives in the Current owner, which is where a later update names it.
                done = self.update(FUTURE_F if first == "update" else CURRENT_F, "feature-active", {"status": "completed"})
                planned = self.plan(*((done, moved) if first == "update" else (moved, done)))
                landed = [i for i in self.items(planned, CURRENT_F) if i["id"] == "feature-active"]
                self.assertEqual([i["status"] for i in landed], ["completed"])
                self.assertNotIn("feature-active", [i["id"] for i in self.items(planned, FUTURE_F)])
                self.assertNotIn("feature-active", self.after(planned, work.CURRENT).decode())
                self.assertNotIn("feature-active", json.loads(self.after(planned, work.PATH))["current"])

    def test_a_capability_completes_into_the_capability_owner(self):
        planned = self.plan(self.update(FUTURE_C, "cap-active", {"status": "completed"}),
                            self.move("cap-active", FUTURE_C, CURRENT_C))
        self.assertEqual([i["id"] for i in self.items(planned, CURRENT_C)], ["cap-active", "cap-shipped"])
        # An item that was never started can complete in one step the same way.
        planned = self.plan(self.update(FUTURE_C, "cap-planned", {"status": "completed"}),
                            self.move("cap-planned", FUTURE_C, CURRENT_C))
        self.assertEqual(self.items(planned, CURRENT_C)[0]["status"], "completed")

    def test_adding_or_moving_a_completed_item_into_a_future_owner_is_refused(self):
        self.refused(self.add(FUTURE_F, [entry("feature-new", "feature", "completed")]))
        self.refused(self.move("feature-shipped", CURRENT_F, FUTURE_F))
        self.refused(self.move("cap-shipped", CURRENT_C, FUTURE_C))

    def test_moves_and_adds_that_keep_the_item_unfinished_are_allowed(self):
        self.plan(self.add(FUTURE_F, [entry("feature-new", "feature", "planned")]))
        planned = self.plan(self.move("feature-active", FUTURE_F, FUTURE_C, type="capability"))
        self.assertIn("feature-active", [i["id"] for i in self.items(planned, FUTURE_C)])
        self.plan(self.add(CURRENT_F, [entry("feature-new", "feature", "completed")]))

    def test_a_misplaced_record_that_is_not_touched_never_blocks_other_work(self):
        self.write(FUTURE_F, [entry("feature-old", "feature", "completed"), entry("feature-planned", "feature", "planned")])
        planned = self.plan(self.update(FUTURE_F, "feature-planned", {"title": "Renamed"}))
        self.assertEqual(self.items(planned, FUTURE_F)[1]["title"], "Renamed")
        # Touching the misplaced record itself, even without a status change, is judged.
        self.refused(self.update(FUTURE_F, "feature-old", {"title": "Renamed"}))
        self.plan(self.move("feature-old", FUTURE_F, CURRENT_F))

    def test_without_registry_roles_or_a_current_owner_nothing_is_refused(self):
        self.registry({})
        self.plan(self.update(FUTURE_F, "feature-active", {"status": "completed"}))
        self.registry({FUTURE_F: ROLES[FUTURE_F], CURRENT_C: ROLES[CURRENT_C]})
        self.plan(self.update(FUTURE_F, "feature-active", {"status": "completed"}))  # no Current feature owner declared

    def test_other_status_changes_and_regular_work_are_not_affected(self):
        self.plan(self.update(FUTURE_F, "feature-planned", {"status": "active"}))
        self.plan(self.update(FUTURE_F, "feature-active", {"status": "cancelled"}))
        self.plan(self.update(FUTURE_F, "feature-blocked", {"status": "superseded"}))
        self.plan(self.update(WORK_ITEMS, "slice-live", {"title": "Renamed"}))

    def test_the_standalone_board_update_planner_enforces_the_same_rule(self):
        request = {"profile": boards.UPDATE, "owner_path": FUTURE_F, "expected_sha256": self.sha(FUTURE_F), "item_id": "feature-active",
                   "changes": {"status": "completed"}}
        with self.assertRaises(ts.ReadError) as raised:
            boards.plan_update(self.root, request)
        self.assertEqual(raised.exception.code, "invalid")
        self.assertIn(CURRENT_F, str(raised.exception))
        request["changes"] = {"title": "Renamed"}
        self.assertEqual(boards.plan_update(self.root, request)["outcome"], "planned")

    def test_owner_mapping_is_read_from_registry_roles(self):
        owners = boards.catalog_owners(LocalRoot(self.root))
        self.assertEqual(owners, {FUTURE_F: ("future", "feature"), FUTURE_C: ("future", "capability"),
                                  CURRENT_F: ("current", "feature"), CURRENT_C: ("current", "capability")})
        self.registry({FUTURE_F: "future-feature-catalogue", "docs/X.md": "marketing-feature-catalogue-source",
                       "docs/Y.md": "current-feature-catalogue-extra"})
        self.assertEqual(boards.catalog_owners(LocalRoot(self.root)), {FUTURE_F: ("future", "feature")})
        (self.root / "docs/REGISTRY.json").write_text("not json")
        self.assertEqual(boards.catalog_owners(LocalRoot(self.root)), {})
        (self.root / "docs/REGISTRY.json").unlink()
        self.assertEqual(boards.catalog_owners(LocalRoot(self.root)), {})
        with tempfile.TemporaryDirectory(prefix="catalog-bare-") as bare:  # no docs folder at all
            self.assertEqual(boards.catalog_owners(LocalRoot(bare)), {})

    def test_the_repository_registry_declares_all_four_catalogue_owners(self):
        owners = boards.catalog_owners(LocalRoot(REPO))
        self.assertEqual(sorted(owners.values()), sorted([("future", "feature"), ("future", "capability"),
                                                          ("current", "feature"), ("current", "capability")]))
        configured = set(json.loads((REPO / "eutonos.boards.json").read_text())["owners"])
        self.assertTrue(set(owners) <= configured, set(owners) - configured)

    def test_form_and_profile_describe_the_active_state_and_the_placement_rule(self):
        semantics = boards.catalog_form("feature", root=self.root)["catalog_semantics"]
        self.assertEqual(semantics["future"], "status planned or deferred")
        self.assertIn("active", semantics)
        description = boards.profile_description()["catalog"]
        self.assertEqual(description["sections"]["future"], "planned or deferred")
        self.assertIn("placement", description)


if __name__ == "__main__":
    unittest.main()
