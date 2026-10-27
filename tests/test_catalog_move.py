import json
import tempfile
import unittest
from pathlib import Path

from eutonos import boards, boards_cli, command_registry, native_owner, tokenslang as ts, write_batch
from eutonos.effects import LocalRoot, decode, digest

FUTURE, CURRENT_CAPS, CURRENT_FEATURES = "docs/FUTURE_FEATURES.md", "docs/CURRENT_CAPABILITIES.md", "docs/CURRENT_FEATURES.md"


def fence(items):
    return b"```tokenslang-board\n" + boards._json({"profile": boards.BOARD, "items": items}) + b"\n```\n"


def entry(identity, kind, status="completed", **extra):
    return {"id": identity, "type": kind, "title": identity.title(), "summary": "Summary of " + identity,
            "status": status, **extra}


class CatalogMoveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="catalog-move-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / "docs").mkdir()
        self.future_items = [
            entry("slice-keep", "slice"),
            entry("feature-done", "feature", extensions={"category": "Delivery", "note": "kept exactly"}),
            entry("feature-later", "feature", "planned", extensions={"category": "Workflow"}),
            entry("feature-bare", "feature")]
        self.cap_items = [
            entry("cap-part", "capability", extensions={"category": "Knowledge", "module": "ram"}),
            entry("cap-real", "capability", extensions={"category": "Workflow"})]
        (self.root / FUTURE).write_bytes(b"# Future\r\n\r\nProse stays exact.\r\n\r\n" + fence(self.future_items))
        (self.root / CURRENT_CAPS).write_bytes(b"# Capabilities\n\n" + fence(self.cap_items))
        (self.root / CURRENT_FEATURES).write_bytes(b"# Current features\n\n" + fence([]))
        (self.root / "docs/REGISTRY.json").write_text(json.dumps({"schema": "x", "documents": []}) + "\n")
        self.write_config([FUTURE, CURRENT_CAPS, CURRENT_FEATURES])

    def write_config(self, owners):
        (self.root / "eutonos.boards.json").write_text(json.dumps({"profile": boards.CONFIG, "owners": owners}) + "\n")

    def sha(self, path):
        target = self.root / path
        return digest(target.read_bytes()) if target.exists() else "1" * 64

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
        board, _ = boards._parse(self.after(planned, path), path)
        return board["items"]

    def test_move_keeps_id_and_fields_and_surrounding_bytes(self):
        planned = self.plan(self.move("feature-done", FUTURE, CURRENT_FEATURES))
        self.assertEqual(sorted(planned["paths"]), sorted([FUTURE, CURRENT_FEATURES]))
        landed = self.items(planned, CURRENT_FEATURES)
        self.assertEqual(landed, [self.future_items[1]])
        self.assertEqual([item["id"] for item in self.items(planned, FUTURE)],
                         ["slice-keep", "feature-later", "feature-bare"])
        self.assertTrue(self.after(planned, FUTURE).startswith(b"# Future\r\n\r\nProse stays exact.\r\n\r\n"))
        self.assertIn("feature-done", [record["id"] for record in planned["records"]])

    def test_move_can_retype_set_category_and_choose_index(self):
        planned = self.plan(
            self.move("feature-bare", FUTURE, CURRENT_FEATURES, category="Collaboration"),
            self.move("cap-part", CURRENT_CAPS, CURRENT_FEATURES, index=0, type="feature"))
        moved = self.items(planned, CURRENT_FEATURES)
        self.assertEqual([item["id"] for item in moved], ["cap-part", "feature-bare"])
        self.assertEqual(moved[0]["type"], "feature")
        self.assertEqual(moved[0]["extensions"], {"category": "Knowledge", "module": "ram"})
        self.assertEqual(moved[1]["extensions"]["category"], "Collaboration")
        self.assertEqual([item["id"] for item in self.items(planned, CURRENT_CAPS)], ["cap-real"])

    def test_move_into_a_freshly_registered_owner_in_one_batch(self):
        form = native_owner.form(self.root, "docs/FUTURE_CAPABILITIES.md", "Future capabilities",
                                 "eutonos.doc.future-capabilities", "future-capability-catalogue",
                                 "These are desired outcomes.")
        target = "docs/FUTURE_CAPABILITIES.md"
        request = {"kind": "move", "request": {
            "from_owner_path": CURRENT_CAPS, "from_expected_sha256": self.sha(CURRENT_CAPS),
            "to_owner_path": target, "to_expected_sha256": form["owner_sha256"], "item_id": "cap-real",
            "index": 0}}
        planned = self.plan(form["batch_operation"], request)
        self.assertIn(target, planned["paths"])
        document = self.after(planned, target)
        self.assertIn(b"Document ID: `eutonos.doc.future-capabilities`.\n\nThese are desired outcomes.\n\n```tokenslang-board", document)
        registry = json.loads(self.after(planned, "docs/REGISTRY.json"))["documents"]
        self.assertEqual(registry, [{"id": "eutonos.doc.future-capabilities", "path": target,
                                     "role": "future-capability-catalogue"}])
        self.assertIn(target, json.loads(self.after(planned, "eutonos.boards.json"))["owners"])
        self.assertEqual([item["id"] for item in self.items(planned, target)], ["cap-real"])

    def test_owner_register_default_identity_is_unchanged(self):
        form = native_owner.form(self.root, "docs/plain.md", "Plain")
        planned = self.plan(form["batch_operation"])
        self.assertIn(b"Document ID: native.board.", self.after(planned, "docs/plain.md"))
        with self.assertRaises(ts.ReadError):
            native_owner.form(self.root, "docs/other.md", "Other", document_id="eutonos.doc.other")
        with self.assertRaises(ts.ReadError):
            native_owner.form(self.root, "docs/other.md", "Other", "not-an-id", "role")
        with self.assertRaises(ts.ReadError):
            native_owner.form(self.root, "docs/other.md", "Other", "eutonos.doc.other", "role", "```bad```")

    def test_refusals(self):
        def refused(*operations, code=None):
            with self.assertRaises(ts.ReadError) as raised:
                self.plan(*operations)
            if code is not None:
                self.assertEqual(raised.exception.code, code)
        (self.root / "docs/NOT_CONFIGURED.md").write_bytes(b"# Loose\n\n" + fence([]))
        refused(self.move("feature-done", FUTURE, FUTURE), code="invalid")
        refused(self.move("feature-done", FUTURE, "docs/NOT_CONFIGURED.md"), code="denied")
        refused(self.move("feature-done", "docs/NOT_CONFIGURED.md", CURRENT_FEATURES), code="denied")
        refused(self.move("slice-keep", FUTURE, CURRENT_FEATURES), code="invalid")
        refused(self.move("feature-done", FUTURE, CURRENT_FEATURES, type="slice"), code="invalid")
        refused(self.move("missing-id", FUTURE, CURRENT_FEATURES), code="missing")
        refused(self.move("feature-bare", FUTURE, CURRENT_FEATURES), code="invalid")  # no category yet
        refused(self.move("feature-done", FUTURE, CURRENT_FEATURES, index=5), code="invalid")
        stale = self.move("feature-done", FUTURE, CURRENT_FEATURES)
        stale["request"]["to_expected_sha256"] = "0" * 64
        refused(stale, code="stale")
        stale = self.move("feature-done", FUTURE, CURRENT_FEATURES)
        stale["request"]["from_expected_sha256"] = "0" * 64
        refused(stale, code="stale")
        refused(self.move("feature-done", FUTURE, CURRENT_FEATURES), self.move("feature-done", FUTURE, CURRENT_FEATURES),
                code="conflict")
        # An ID already present in the target is a conflict, never an overwrite.
        (self.root / CURRENT_FEATURES).write_bytes(b"# Current features\n\n" + fence([self.future_items[1]]))
        refused(self.move("feature-done", FUTURE, CURRENT_FEATURES), code="ambiguous")
        # Source files are untouched by a refused plan.
        self.assertEqual(boards._parse((self.root / FUTURE).read_bytes(), FUTURE)[0]["items"], self.future_items)

    def test_unknown_batch_kind_remains_refused_and_move_is_listed(self):
        self.assertIn("move", write_batch.KINDS)
        self.assertEqual(write_batch.form(self.root)["operations"][-1]["kind"], "move")
        with self.assertRaises(ts.ReadError):
            write_batch.normalize({"profile": write_batch.PROFILE, "operations": [{"kind": "shuffle", "request": {}}]})

    def update(self, owner, item_id, changes):
        return {"kind": "update", "request": {"owner_path": owner, "expected_sha256": self.sha(owner),
                                              "item_id": item_id, "changes": changes}}

    def test_capability_feature_links_are_validated(self):
        (self.root / CURRENT_FEATURES).write_bytes(b"# Current features\n\n" + fence([
            entry("feature-linked", "feature", extensions={"category": "Delivery"})]))
        good = {"extensions": {"category": "Workflow", "features": ["feature-linked"]}}
        planned = self.plan(self.update(CURRENT_CAPS, "cap-real", good))
        linked = [item for item in self.items(planned, CURRENT_CAPS) if item["id"] == "cap-real"][0]
        self.assertEqual(linked["extensions"]["features"], ["feature-linked"])
        for features in ("feature-linked", ["feature-linked", "feature-linked"], [""], [3], ["no-such-feature"],
                         ["cap-part"]):
            with self.subTest(features=features), self.assertRaises(ts.ReadError):
                self.plan(self.update(CURRENT_CAPS, "cap-real",
                                      {"extensions": {"category": "Workflow", "features": features}}))
        with self.assertRaises(ts.ReadError):  # a feature does not carry capability links
            self.plan(self.update(CURRENT_FEATURES, "feature-linked",
                                  {"extensions": {"category": "Delivery", "features": ["feature-linked"]}}))

    def test_link_can_name_a_feature_added_earlier_in_the_same_batch(self):
        planned = self.plan(
            self.move("cap-part", CURRENT_CAPS, CURRENT_FEATURES, type="feature"),
            self.update(CURRENT_CAPS, "cap-real", {"extensions": {"category": "Workflow", "features": ["cap-part"]}}))
        capability = self.items(planned, CURRENT_CAPS)[0]
        self.assertEqual(capability["extensions"]["features"], ["cap-part"])

    def test_glossary_defines_feature_and_capability(self):
        feature = command_registry.explain("feature")
        capability = command_registry.explain("Capability")
        self.assertEqual(feature["outcome"], "explanation")
        self.assertIn("named part", feature["term"]["meaning"])
        self.assertIn("extensions.features", capability["term"]["meaning"])

    def test_profile_description_documents_move(self):
        self.assertEqual(boards.profile_description()["plans"]["move"]["profile"], boards.MOVE)


if __name__ == "__main__":
    unittest.main()
