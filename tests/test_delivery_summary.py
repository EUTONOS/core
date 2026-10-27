import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from eutonos import delivery_summary


class DeliverySummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eutonos-delivery-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "work"
        self.root.mkdir()
        self.git(self.root, "init", "-q", "-b", "main")
        self.write_boards()
        (self.root / "owned.py").write_text("one\n", encoding="utf-8")
        (self.root / "plain.py").write_text("one\n", encoding="utf-8")
        self.commit(self.root)

    def git(self, root, *args):
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0",
                   GIT_AUTHOR_DATE="2000-01-01T00:00:00+00:00",
                   GIT_COMMITTER_DATE="2000-01-01T00:00:00+00:00")
        return subprocess.check_output(
            ["git", "-c", "safe.directory=" + str(root), "-c", "core.autocrlf=false",
             "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
             "-C", str(root), *args], env=env, stderr=subprocess.PIPE,
        ).decode().strip()

    def commit(self, root, message="fixture"):
        self.git(root, "add", "--all")
        self.git(root, "commit", "-qm", message)

    def write_boards(self, ambiguous=False):
        owners = ["docs/alpha.md"] + (["docs/beta.md"] if ambiguous else [])
        (self.root / "docs").mkdir(exist_ok=True)
        config = {"profile": "eutonos.tokenslang.boards.v1", "owners": owners}
        (self.root / "eutonos.boards.json").write_text(json.dumps(config), encoding="utf-8")
        for campaign in ("alpha", "beta") if ambiguous else ("alpha",):
            board = {"profile": "eutonos.tokenslang.board.v1", "items": [{
                "id": campaign.upper(), "type": "campaign", "title": campaign.title(),
                "summary": "Fixture campaign", "status": "completed",
                "evidence_refs": [{"path": "owned.py"}],
            }]}
            (self.root / "docs" / (campaign + ".md")).write_text(
                "# Owner\n\nVerification passed.\n\n```tokenslang-board\n"
                + json.dumps(board) + "\n```\n", encoding="utf-8")

    def add_upstream(self):
        bare = self.base / "remote.git"
        subprocess.check_call(["git", "init", "--bare", "-q", str(bare)])
        self.git(self.root, "remote", "add", "origin", str(bare))
        self.git(self.root, "push", "-qu", "origin", "main")
        return bare

    def row(self, result, path):
        return next(item for item in result["paths"] if item["path"] == path)

    def snapshot(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file() and ".git" not in path.parts}

    def test_clean_missing_upstream_uses_explicit_unknowns_and_writes_nothing(self):
        before = self.snapshot()
        result = delivery_summary.project(self.root)
        self.assertEqual(result["profile"], delivery_summary.PROFILE)
        self.assertEqual(result["disposition"], {
            "working_tree": "clean", "committed_changes": None, "publication": "unknown"})
        self.assertEqual(result["repository"]["divergence"],
                         {"ahead": None, "behind": None, "known": False})
        self.assertIsNone(result["authority"]["publish"]["deliverable"])
        self.assertEqual(result["paths"], [])
        self.assertEqual(before, self.snapshot())

    def test_modified_staged_and_untracked_paths_are_distinct_and_authority_qualified(self):
        self.add_upstream()
        (self.root / "owned.py").write_text("staged\n", encoding="utf-8")
        self.git(self.root, "add", "owned.py")
        (self.root / "plain.py").write_text("modified\n", encoding="utf-8")
        (self.root / "new.py").write_text("untracked\n", encoding="utf-8")
        result = delivery_summary.project(
            self.root, authority={"commit": {"authorized": True, "source": "owner-session"},
                                  "publish": False})
        self.assertEqual(self.row(result, "owned.py")["states"], ["staged"])
        self.assertEqual(self.row(result, "plain.py")["states"], ["modified"])
        self.assertEqual(self.row(result, "new.py")["states"], ["untracked"])
        self.assertEqual(self.row(result, "owned.py")["ownership"]["status"], "identified")
        self.assertEqual(self.row(result, "plain.py")["ownership"]["status"], "unknown")
        self.assertFalse(result["authority"]["commit"]["deliverable"],
                         "Unowned local paths must prevent an unqualified safe-to-commit answer")
        self.assertIn("plain.py", result["authority"]["commit"]["reasons"][0])
        self.assertNotIn("authority for", " ".join(result["authority"]["commit"]["reasons"]).lower())

    def test_ahead_commit_is_unpublished_and_lists_committed_paths(self):
        self.add_upstream()
        (self.root / "owned.py").write_text("ahead\n", encoding="utf-8")
        self.commit(self.root, "ahead")
        result = delivery_summary.project(self.root, authority={"publish": True})
        self.assertEqual(result["repository"]["divergence"],
                         {"ahead": 1, "behind": 0, "known": True})
        self.assertEqual(result["disposition"]["publication"], "unpublished")
        self.assertEqual(self.row(result, "owned.py")["disposition"], "committed")
        self.assertTrue(result["authority"]["publish"]["deliverable"])

    def test_unpublished_commit_trailer_attributes_exact_paths(self):
        self.add_upstream()
        (self.root / "plain.py").write_text("campaign change\n", encoding="utf-8")
        self.commit(self.root, "campaign change\n\nEutonos-Campaign: ALPHA")
        result = delivery_summary.project(self.root, authority={"publish": True})
        ownership = self.row(result, "plain.py")["ownership"]
        self.assertEqual(ownership["status"], "identified")
        self.assertEqual(ownership["campaigns"][0]["id"], "ALPHA")
        self.assertIn("commit-trailer", {item["kind"] for item in
                                         ownership["campaigns"][0]["verification"]["evidence"]})
        trailer_paths = [item["path"] for item in
                         ownership["campaigns"][0]["verification"]["evidence"]
                         if item["kind"] == "commit-trailer"]
        self.assertEqual(trailer_paths, ["plain.py"])
        self.assertTrue(result["authority"]["publish"]["deliverable"])

    def test_native_path_owner_precedes_different_integration_trailer(self):
        self.add_upstream()
        self.write_boards(ambiguous=True)
        alpha = self.root / "docs" / "alpha.md"
        alpha.write_text(alpha.read_text(encoding="utf-8") + "\nIntegration note.\n",
                         encoding="utf-8")
        self.commit(self.root, "integrate campaign owners\n\nEutonos-Campaign: BETA")
        result = delivery_summary.project(self.root, authority={"publish": True})
        ownership = self.row(result, "docs/alpha.md")["ownership"]
        self.assertEqual(ownership["status"], "identified")
        self.assertEqual([item["id"] for item in ownership["campaigns"]], ["ALPHA"])
        self.assertTrue(result["authority"]["publish"]["deliverable"])

    def test_behind_upstream_blocks_publish_even_when_authorized(self):
        bare = self.add_upstream()
        peer = self.base / "peer"
        subprocess.check_call(["git", "clone", "-q", "-b", "main", str(bare), str(peer)])
        (peer / "remote.py").write_text("remote\n", encoding="utf-8")
        self.commit(peer, "remote")
        self.git(peer, "push", "-q", "origin", "main")
        self.git(self.root, "fetch", "-q", "origin")
        result = delivery_summary.project(self.root, authority={"publish": True})
        self.assertEqual(result["repository"]["divergence"],
                         {"ahead": 0, "behind": 1, "known": True})
        self.assertFalse(result["authority"]["publish"]["deliverable"])
        self.assertIn("behind", " ".join(result["authority"]["publish"]["reasons"]))

    def test_ambiguous_campaign_claim_is_preserved(self):
        self.write_boards(ambiguous=True)
        self.commit(self.root, "ambiguous boards")
        (self.root / "owned.py").write_text("changed\n", encoding="utf-8")
        result = delivery_summary.project(self.root)
        ownership = self.row(result, "owned.py")["ownership"]
        self.assertEqual(ownership["status"], "ambiguous")
        self.assertEqual([item["id"] for item in ownership["campaigns"]], ["ALPHA", "BETA"])
        self.assertEqual(ownership["campaigns"][0]["verification"]["evidence"][0],
                         {"path": "docs/alpha.md", "kind": "campaign-owner"})
        self.assertEqual(ownership["campaigns"][0]["verification"]["status"],
                         "recorded-complete")

    def test_exact_root_and_authority_shape_are_guarded(self):
        nested = self.root / "nested"
        nested.mkdir()
        with self.assertRaisesRegex(ValueError, "exact Git worktree root"):
            delivery_summary.project(nested)
        with self.assertRaisesRegex(ValueError, "commit and publish"):
            delivery_summary.project(self.root, authority={"deploy": True})


if __name__ == "__main__":
    unittest.main()
