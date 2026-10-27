"""The generated list of what EUTONOS offers: what the generator keeps and refuses, that its bytes are stable, that the
checked-in file has not drifted from the catalogue documents, and that the file ships in the wheel and the release."""
from __future__ import annotations

import io
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import build_offers  # noqa: E402
from eutonos import dashboard  # noqa: E402

OWNERS = {("current", "feature"): "docs/CURRENT_FEATURES.md", ("future", "feature"): "docs/FUTURE_FEATURES.md",
          ("current", "capability"): "docs/CURRENT_CAPABILITIES.md", ("future", "capability"): "docs/FUTURE_CAPABILITIES.md"}


def item(identifier, kind, status, title=None, summary=None, category="Workflow", parent=None, **extensions):
    value = {"id": identifier, "type": kind, "status": status, "title": title or identifier.title(),
             "summary": summary or f"About {identifier}."}
    if category is not None:
        extensions = dict(extensions, category=category)
    if extensions:
        value["extensions"] = extensions
    if parent:
        value["parent_id"] = parent
    return value


def write_owner(root, path, items):
    target = Path(root) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    board = {"profile": "eutonos.tokenslang.board.v1", "items": items}
    target.write_text(f"# Owner\n\n```tokenslang-board\n{json.dumps(board, indent=2)}\n```\n", encoding="utf-8")


def make_root(root, **sections):
    """A small repository with the four catalogue owners; `sections` maps current_feature, future_feature,
    current_capability and future_capability to their item lists."""
    root = Path(root)
    documents = [{"path": path, "role": f"{horizon}-{kind}-catalogue"} for (horizon, kind), path in OWNERS.items()]
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "REGISTRY.json").write_text(json.dumps({"documents": documents}), encoding="utf-8")
    for (horizon, kind), path in OWNERS.items():
        write_owner(root, path, sections.get(f"{horizon}_{kind}", []))
    return root


class Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def collect(self, **sections):
        return build_offers.collect(make_root(self.base, **sections))


class WhatIsKept(Tmp):
    def test_each_status_lands_in_its_section_and_the_rest_is_left_out(self):
        payload, _ = self.collect(
            current_feature=[item("f-done", "feature", "completed"), item("f-cancelled", "feature", "cancelled"),
                             item("f-old", "feature", "superseded")],
            future_feature=[item("f-plan", "feature", "planned"), item("f-hold", "feature", "deferred"),
                            item("f-go", "feature", "active"), item("f-stuck", "feature", "blocked"),
                            item("f-dropped", "feature", "cancelled")],
            current_capability=[item("c-done", "capability", "completed", features=["f-done"])],
            future_capability=[item("c-plan", "capability", "planned", features=["f-plan", "c-nothing"])])
        by_id = {row["id"]: row for row in payload["items"]}
        self.assertEqual(sorted(by_id), ["c-done", "c-plan", "f-done", "f-go", "f-hold", "f-plan", "f-stuck"])
        self.assertEqual({name: (row["horizon"], row["status"]) for name, row in by_id.items()},
                         {"f-done": ("current", "completed"), "f-plan": ("future", "planned"),
                          "f-hold": ("future", "deferred"), "f-go": ("future", "in progress"),
                          "f-stuck": ("future", "in progress"), "c-done": ("current", "completed"),
                          "c-plan": ("future", "planned")})
        self.assertEqual(build_offers.counts(payload), {"current features": 1, "future features": 4,
                                                        "current capabilities": 1, "future capabilities": 1})

    def test_every_item_has_the_same_plain_fields_and_only_capabilities_list_features(self):
        payload, _ = self.collect(current_feature=[item("f-one", "feature", "completed", title="One", summary="Does one.",
                                                        category="Dashboard")],
                                  current_capability=[item("c-one", "capability", "completed", features=["f-one"])])
        feature, capability = payload["items"][0], payload["items"][1]
        self.assertEqual(feature, {"id": "f-one", "type": "feature", "title": "One", "summary": "Does one.",
                                   "category": "Dashboard", "status": "completed", "horizon": "current"})
        self.assertEqual(capability["features"], ["f-one"])
        self.assertEqual(set(capability) - set(feature), {"features"})
        self.assertEqual(payload["profile"], "eutonos.offers.v1")

    def test_only_top_level_feature_and_capability_records_are_kept(self):
        payload, _ = self.collect(current_feature=[
            item("f-top", "feature", "completed"), item("f-child", "feature", "completed", parent="f-top"),
            item("s-slice", "slice", "completed"), item("q-quick", "quick", "completed"),
            item("camp", "campaign", "active"), item("b-bug", "bug", "active")])
        self.assertEqual([row["id"] for row in payload["items"]], ["f-top"])

    def test_a_record_without_a_category_is_listed_as_uncategorized(self):
        payload, _ = self.collect(current_feature=[item("f-bare", "feature", "completed", category=None),
                                                   item("f-blank", "feature", "completed", category="  ")])
        self.assertEqual({row["category"] for row in payload["items"]}, {"Uncategorized"})

    def test_a_capability_keeps_only_links_to_features_that_ship_and_says_which_it_dropped(self):
        payload, notes = self.collect(
            current_feature=[item("f-a", "feature", "completed"), item("f-private", "feature", "completed", shared=False)],
            current_capability=[item("c-x", "capability", "completed", features=["f-b-missing", "f-a", "f-private"])],
            future_feature=[item("f-b-missing", "feature", "planned")])
        capability = next(row for row in payload["items"] if row["id"] == "c-x")
        self.assertEqual(capability["features"], ["f-b-missing", "f-a"])  # written order, planned features included
        self.assertEqual(len(notes), 1)
        self.assertIn("f-private", notes[0])

    def test_the_sorting_does_not_depend_on_the_order_the_records_are_written_in(self):
        rows = [item("f-1", "feature", "completed", title="beta", category="Zed"),
                item("f-2", "feature", "completed", title="Alpha", category="Zed"),
                item("f-3", "feature", "completed", title="Gamma", category="apple")]
        first = build_offers.render(self.collect(current_feature=rows)[0])
        second = build_offers.render(self.collect(current_feature=list(reversed(rows)))[0])
        self.assertEqual(first, second)
        order = [row["id"] for row in json.loads(first)["items"]]
        self.assertEqual(order, ["f-3", "f-2", "f-1"])  # category, then title, both ignoring case


class SharingRule(Tmp):
    def test_an_item_ships_unless_shared_is_false(self):
        payload, _ = self.collect(current_feature=[
            item("f-unmarked", "feature", "completed"), item("f-yes", "feature", "completed", shared=True),
            item("f-no", "feature", "completed", shared=False)])
        self.assertEqual([row["id"] for row in payload["items"]], ["f-unmarked", "f-yes"])

    def test_any_other_value_of_shared_is_refused_so_a_typo_cannot_publish_a_private_record(self):
        for value in ("false", "no", 0, None, "False"):
            with self.subTest(value=value):
                with self.assertRaises(build_offers.OffersError) as caught:
                    self.collect(current_feature=[item("f-odd", "feature", "completed", shared=value)])
                self.assertIn("f-odd", str(caught.exception))
                self.assertIn("shared", str(caught.exception))

    def test_no_record_in_the_real_catalogue_uses_shared_today(self):
        for path in OWNERS.values():
            self.assertNotIn('"shared"', (ROOT / path).read_text(encoding="utf-8"), path)


class WhatIsRefused(Tmp):
    def refused(self, **sections):
        with self.assertRaises(build_offers.OffersError) as caught:
            self.collect(**sections)
        return str(caught.exception)

    def test_a_completed_record_in_a_future_owner_is_refused_not_dropped(self):
        message = self.refused(future_feature=[item("f-late", "feature", "completed")])
        self.assertIn("f-late", message)
        self.assertIn("future", message)

    def test_an_unfinished_record_in_a_current_owner_is_refused(self):
        self.assertIn("f-early", self.refused(current_feature=[item("f-early", "feature", "planned")]))

    def test_a_capability_in_a_feature_owner_and_a_feature_in_a_capability_owner_are_refused(self):
        self.assertIn("c-lost", self.refused(current_feature=[item("c-lost", "capability", "completed")]))
        self.assertIn("f-lost", self.refused(future_capability=[item("f-lost", "feature", "planned")]))

    def test_a_duplicate_id_across_owners_is_refused(self):
        message = self.refused(current_feature=[item("f-twin", "feature", "completed")],
                               future_feature=[item("f-twin", "feature", "planned")])
        self.assertIn("f-twin", message)

    def test_a_registry_without_one_of_the_four_roles_is_refused_naming_it(self):
        root = make_root(self.base)
        registry = json.loads((root / "docs" / "REGISTRY.json").read_text(encoding="utf-8"))
        registry["documents"] = [row for row in registry["documents"] if row["role"] != "future-capability-catalogue"]
        (root / "docs" / "REGISTRY.json").write_text(json.dumps(registry), encoding="utf-8")
        with self.assertRaises(build_offers.OffersError) as caught:
            build_offers.collect(root)
        self.assertIn("future capability", str(caught.exception))

    def test_a_missing_owner_file_or_a_missing_board_block_is_refused(self):
        root = make_root(self.base)
        (root / OWNERS[("future", "feature")]).unlink()
        with self.assertRaisesRegex(build_offers.OffersError, "missing"):
            build_offers.collect(root)
        (root / OWNERS[("future", "feature")]).write_text("# No block here\n", encoding="utf-8")
        with self.assertRaisesRegex(build_offers.OffersError, "no tokenslang-board block"):
            build_offers.collect(root)


class StableBytes(Tmp):
    def test_the_bytes_are_ascii_lf_sorted_json_with_no_time_stamp_and_are_the_same_every_time(self):
        rows = [item("f-1", "feature", "completed", title="Café – menu", summary="Quote “x”")]
        root = make_root(self.base, current_feature=rows)
        first = build_offers.build(root)
        self.assertEqual(first, build_offers.build(root))
        first.decode("ascii")
        self.assertNotIn(b"\r", first)
        self.assertTrue(first.endswith(b"}\n") and not first.endswith(b"\n\n"))
        loaded = json.loads(first)
        self.assertEqual(loaded["items"][0]["title"], "Café – menu")  # escaped, and read back whole
        self.assertEqual(first, (json.dumps(loaded, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("ascii"))
        self.assertNotRegex(first.decode("ascii"), r"20\d\d-\d\d-\d\d|generated|sha256|\\\\|[A-Za-z]:/")

    def test_writing_twice_changes_nothing_the_second_time(self):
        root = make_root(self.base, current_feature=[item("f-1", "feature", "completed")])
        changed, _, _ = build_offers.write(root)
        self.assertTrue(changed)
        stamp = build_offers.target(root).stat().st_mtime_ns
        changed, _, _ = build_offers.write(root)
        self.assertFalse(changed)
        self.assertEqual(build_offers.target(root).stat().st_mtime_ns, stamp)


class Drift(Tmp):
    def fresh(self):
        root = make_root(self.base, current_feature=[item("f-1", "feature", "completed")],
                         future_feature=[item("f-2", "feature", "planned")])
        build_offers.write(root)
        return root

    def test_a_file_that_matches_the_documents_passes(self):
        build_offers.check(self.fresh())

    def test_a_changed_document_fails_the_check_and_names_the_command_and_the_record(self):
        root = self.fresh()
        write_owner(root, OWNERS[("current", "feature")],
                    [item("f-1", "feature", "completed"), item("f-new", "feature", "completed")])
        with self.assertRaises(build_offers.OffersError) as caught:
            build_offers.check(root)
        message = str(caught.exception)
        self.assertIn("python tools/build_offers.py", message)
        self.assertIn("f-new", message)

    def test_an_edited_summary_a_hand_edit_and_a_missing_file_all_fail(self):
        root = self.fresh()
        path = build_offers.target(root)
        path.write_bytes(path.read_bytes().replace(b"About f-1.", b"About something else."))
        with self.assertRaisesRegex(build_offers.OffersError, "changed: f-1"):
            build_offers.check(root)
        build_offers.write(root)
        build_offers.check(root)  # regenerating puts it right
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaisesRegex(build_offers.OffersError, "wording or layout only"):
            build_offers.check(root)
        build_offers.write(root)
        path.write_bytes(b"not json")
        with self.assertRaisesRegex(build_offers.OffersError, "cannot be read"):
            build_offers.check(root)
        path.unlink()
        with self.assertRaisesRegex(build_offers.OffersError, "python tools/build_offers.py"):
            build_offers.check(root)

    def test_the_command_line_writes_checks_and_reports(self):
        root = make_root(self.base, current_feature=[item("f-1", "feature", "completed")])
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(build_offers.main(["--root", str(root)]), 0)
        self.assertIn("1 current features", out.getvalue())
        self.assertIn("0 future capabilities", out.getvalue())
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(build_offers.main(["--root", str(root), "--check"]), 0)
        self.assertIn("is current", out.getvalue())
        write_owner(root, OWNERS[("current", "feature")], [])
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(build_offers.main(["--root", str(root), "--check"]), 1)
        self.assertIn("python tools/build_offers.py", err.getvalue())
        self.assertTrue(build_offers.target(root).is_file())  # --check never rewrites

    def test_the_checked_in_file_matches_the_catalogue_documents(self):
        """Fails when someone changes a catalogue document and forgets to regenerate. Fix: python tools/build_offers.py"""
        try:
            build_offers.check(ROOT)
        except build_offers.OffersError as exc:
            self.fail(str(exc))


class ShippedFile(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / build_offers.OFFERS_PATH).read_text(encoding="utf-8"))

    def test_the_file_has_the_four_sections_with_unique_ids_and_resolvable_links(self):
        items = self.data["items"]
        self.assertEqual(self.data["profile"], "eutonos.offers.v1")
        self.assertEqual(len({row["id"] for row in items}), len(items))
        counts = build_offers.counts(self.data)
        self.assertEqual(list(counts), ["current features", "future features", "current capabilities", "future capabilities"])
        self.assertTrue(all(number > 0 for number in counts.values()), counts)
        features = {row["id"] for row in items if row["type"] == "feature"}
        for row in items:
            if row["type"] == "capability":
                self.assertTrue(row["features"], row["id"])
                self.assertLessEqual(set(row["features"]), features, row["id"])
            else:
                self.assertNotIn("features", row)
        for row in items:
            self.assertEqual(row["status"] == "completed", row["horizon"] == "current", row["id"])

    def test_the_file_is_ascii_and_lists_no_source_path_time_or_hash_of_a_record(self):
        raw = (ROOT / build_offers.OFFERS_PATH).read_bytes()
        raw.decode("ascii")
        self.assertNotIn(b"\r", raw)
        self.assertEqual({row["path"] for row in self.data["sources"]}, set(OWNERS.values()))

    def test_the_installed_reader_returns_the_same_file(self):
        self.assertEqual(dashboard.offers(), self.data)

    def test_it_ships_in_the_wheel_and_in_the_release(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(pyproject, r'\[tool\.setuptools\.package-data\]\r?\neutonos = \[[^\]]*"offers/\*\.json"')
        release = json.loads((ROOT / "src" / "eutonos" / "release.json").read_text(encoding="utf-8"))
        owners = {path: module["id"] for module in release["modules"] for path in module["paths"]}
        self.assertEqual(owners.get("src/eutonos/offers/offers.json"), "dashboard")
        self.assertEqual(owners.get("tools/build_offers.py"), "setup")
        for path in OWNERS.values():  # the documents stay registered: the generator reads them and the catalogue tests require them
            self.assertIn(path, owners)

    def test_the_generator_names_its_own_regenerate_command_and_reads_no_secret_place(self):
        text = (ROOT / "tools" / "build_offers.py").read_text(encoding="utf-8")
        self.assertIn("python tools/build_offers.py", text)
        self.assertIsNone(re.search(r"\.eutonos|expanduser|environ|urlopen|subprocess", text))


if __name__ == "__main__":
    unittest.main()
