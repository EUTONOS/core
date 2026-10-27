import json
import tempfile
import unittest
from pathlib import Path

from eutonos import boards, release

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "src" / "eutonos" / "release.json"
CATALOGUE = ("docs/CURRENT_FEATURES.md", "docs/CURRENT_CAPABILITIES.md",
             "docs/FUTURE_FEATURES.md", "docs/FUTURE_CAPABILITIES.md")


def docs_paths(manifest):
    return [path for module in manifest["modules"] if module["id"] == "docs" for path in module["paths"]]


def configured_owners(root):
    config = json.loads((Path(root) / "eutonos.boards.json").read_text(encoding="utf-8"))
    return list(config["owners"])


def unshipped_catalogue_owners(root, manifest):
    """Configured owners that hold a feature or capability but are not shipped by the Docs module."""
    shipped, found = set(docs_paths(manifest)), []
    for path in configured_owners(root):
        target = Path(root) / path
        if not target.is_file():
            continue
        board, _ = boards._parse(target.read_bytes(), path)
        if board and any(item.get("type") in boards.CATEGORY_TYPES for item in board["items"]) and path not in shipped:
            found.append(path)
    return found


class CatalogueReleaseTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_every_owner_holding_features_or_capabilities_ships_in_the_docs_module(self):
        self.assertEqual(unshipped_catalogue_owners(ROOT, self.manifest), [])

    def test_all_four_catalogue_documents_ship(self):
        self.assertEqual([path for path in CATALOGUE if path not in docs_paths(self.manifest)], [])

    def test_a_missing_catalogue_document_is_only_an_unregistered_drafted_owner(self):
        # Registration of docs/CURRENT_FEATURES.md and docs/FUTURE_CAPABILITIES.md is a drafted request until the
        # coordinator applies it. Until then the Docs module may be missing exactly those two paths; once an owner is
        # configured its file must exist, so the module fingerprint is valid again.
        docs = [m for m in release.inventory(ROOT, self.manifest) if m["id"] == "docs"][0]
        configured = set(configured_owners(ROOT))
        for path in docs["missing"]:
            self.assertIn(path, CATALOGUE)
            self.assertNotIn(path, configured, path + " is configured as an owner but its file is missing")

    def test_the_check_fails_for_a_catalogue_owner_that_is_not_shipped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "docs").mkdir()
            fence = b"```tokenslang-board\n" + boards._json({"profile": boards.BOARD, "items": [
                {"id": "feature-a", "type": "feature", "title": "A", "summary": "s", "status": "completed",
                 "extensions": {"category": "Workflow"}}]}) + b"\n```\n"
            (root / "docs/NEW_CATALOGUE.md").write_bytes(b"# New\n\n" + fence)
            (root / "docs/PLAIN.md").write_bytes(b"# Plain\n\n" + b"```tokenslang-board\n" + boards._json(
                {"profile": boards.BOARD, "items": [{"id": "slice-a", "type": "slice", "title": "S", "summary": "s",
                                                     "status": "planned"}]}) + b"\n```\n")
            (root / "eutonos.boards.json").write_text(json.dumps(
                {"profile": boards.CONFIG, "owners": ["docs/NEW_CATALOGUE.md", "docs/PLAIN.md"]}), encoding="utf-8")
            manifest = {"modules": [{"id": "docs", "paths": ["docs/PLAIN.md"]}]}
            self.assertEqual(unshipped_catalogue_owners(root, manifest), ["docs/NEW_CATALOGUE.md"])
            manifest["modules"][0]["paths"].append("docs/NEW_CATALOGUE.md")
            self.assertEqual(unshipped_catalogue_owners(root, manifest), [])


if __name__ == "__main__":
    unittest.main()
