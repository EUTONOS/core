"""Scale and evidence integrity checks for NAV inventory/source reads."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from eutonos import GitView, tokenslang as ts
from eutonos.effects import LocalRoot
from eutonos import nav


class NavInventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eutonos-nav-inventory-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "src").mkdir()
        (self.root / "NAV").mkdir()
        (self.root / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
        (self.root / "src/app.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.cfg = {"profile": nav.CONFIG, "repository_id": "fixture.repo",
                    "scope_id": "fixture.scope", "namespace": "fixture.native",
                    "map_root": "NAV", "source_roots": ["src"],
                    "excluded_roots": ["src/vendor"], "context_paths": ["AGENTS.md"]}
        self.git("init", "-q", "-b", "main")
        self.commit()

    def git(self, *args, input=None):
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        return subprocess.run(
            ["git", "-c", "safe.directory=" + str(self.root), "-c", "core.autocrlf=false",
             "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
             "-C", str(self.root), *args], input=input, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def commit(self):
        self.git("add", "--all")
        self.git("commit", "-qm", "Fixture snapshot")

    def snapshot(self):
        return nav.inventory(LocalRoot(self.root), self.cfg, "eutonos.nav.json")

    def reject(self, code, function, *args):
        with self.assertRaises(ts.ReadError) as error:
            function(*args)
        self.assertEqual(error.exception.code, code)

    def test_scope_first_listing_ignores_more_than_4096_unrelated_paths(self):
        empty = self.git("hash-object", "-w", "--stdin", input=b"").decode().strip()
        records = b"".join(
            f"100644 {empty}\tunrelated/file-{index:05}.txt\n".encode()
            for index in range(4100))
        self.git("update-index", "--index-info", input=records)
        self.git("commit", "-qm", "Large unrelated tree")
        result = self.snapshot()
        self.assertEqual(set(result["paths"]), {"AGENTS.md", "src/app.py"})

    def test_more_than_512_sources_and_8_mib_are_streamed_and_lazy(self):
        body = ("x" * 17000 + "\n").encode()
        for index in range(513):
            (self.root / "src" / f"part-{index:04}.txt").write_bytes(body)
        self.commit()
        result = self.snapshot()
        self.assertGreater(len(result["paths"]), 512)
        self.assertGreater(sum(result["sizes"].values()), 8 * 1024 * 1024)
        self.assertEqual(result["contents"]["src/part-0512.txt"], body.decode())

    def test_excluded_changes_do_not_change_inventory_fingerprint(self):
        vendor = self.root / "src/vendor"
        vendor.mkdir()
        (vendor / "library.py").write_text("VERSION = 1\n", encoding="utf-8")
        self.commit()
        before = self.snapshot()
        (vendor / "library.py").write_text("VERSION = 2\n", encoding="utf-8")
        after = self.snapshot()
        self.assertEqual(before["fingerprint"], after["fingerprint"])
        self.assertNotIn("src/vendor/library.py", after["paths"])

    def test_untracked_nonignored_source_is_included_and_tracked_deletion_skipped(self):
        (self.root / "src/new.py").write_text("NEW = True\n", encoding="utf-8")
        (self.root / "src/app.py").unlink()
        result = self.snapshot()
        self.assertIn("src/new.py", result["paths"])
        self.assertNotIn("src/app.py", result["paths"])

    def test_lazy_content_rejects_changed_and_missing_evidence(self):
        result = self.snapshot()
        (self.root / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
        self.reject("stale", result["contents"].__getitem__, "src/app.py")
        result = self.snapshot()
        (self.root / "AGENTS.md").unlink()
        self.reject("stale", result["contents"].__getitem__, "AGENTS.md")
        self.reject("missing", self.snapshot)

    def test_large_committed_blob_uses_streaming_git_read(self):
        body = (b"large = '" + b"x" * 200000 + b"'\n")
        (self.root / "src/large.py").write_bytes(body)
        self.commit()
        data, evidence = GitView(self.root, "HEAD").read("src/large.py")
        self.assertEqual(data, body)
        self.assertEqual(evidence["bytes"], len(body))

    def test_more_than_512_native_records_preserve_ids(self):
        blocks = []
        for index in range(513):
            rid = f"SYS-scale-{index:04}"
            blocks.append(
                f"### `{rid}` Scale record {index}\n"
                "- Kind: system\n- Aliases: none\n- Summary: Scaled native record.\n"
                "- Evidence: src/app.py\n- Related: none\n- Review: reviewed\n- Revision: 1\n")
        bodies = {name: b"# Native map\n" for name in nav.MAPS}
        bodies["NAVIGATION.md"] += "\n".join(blocks).encode()
        records, _ = nav.parse_maps(bodies)
        self.assertEqual(len(records), 513)
        self.assertEqual(records["SYS-scale-0000"]["id"], "SYS-scale-0000")
        self.assertEqual(records["SYS-scale-0512"]["id"], "SYS-scale-0512")


if __name__ == "__main__":
    unittest.main()
