"""Source-pinned Current Work knowledge, controls and conflict fixtures."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from eutonos import current_work_knowledge as knowledge, measurement, ram, ram_relevance, tokenslang as ts
from eutonos.effects import digest


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eutonos-knowledge-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        (self.root / "RAM").mkdir()
        (self.root / "docs").mkdir()
        (self.root / "NAV").mkdir()
        (self.root / "docs/guide.md").write_text("# Guide\nmain edition\n", encoding="utf-8")
        (self.root / "WORK.md").write_text("# Work\n", encoding="utf-8")
        self.config = {"profile": ram.CONFIG, "repository_id": "fixture.repo", "scope_id": "fixture.scope",
                       "namespace": "fixture.native", "store_root": "RAM", "current_task": "WORK.md",
                       "work_owners": {"work": "WORK.md"}}
        self.write_json("eutonos.ram.json", self.config)
        self.catalog = {"profile": ts.CATALOG, "repository_id": "fixture.repo", "scope_id": "fixture.scope",
                        "namespace": "fixture.native", "owners": {}, "excluded_roots": [],
                        "source_paths": ["docs/guide.md"],
                        "bindings": [{"resource_id": "guide", "path": "docs/guide.md", "kind": "docs",
                                      "label": "Guide", "visibility": "public", "state": "active"}]}
        self.write_json("eutonos.read.json", self.catalog)
        self.write_json("eutonos.nav.json", {"profile": "eutonos.tokenslang.nav-config.v1",
                      "repository_id": "fixture.repo", "scope_id": "fixture.scope",
                      "namespace": "fixture.native", "map_root": "NAV", "source_roots": ["src"],
                      "excluded_roots": [], "context_paths": ["docs/guide.md"]})
        self.write_maps("main map")
        self.git("init", "-q", "-b", "main")
        self.git("add", "--all")
        self.git("commit", "-qm", "base")
        self.base = self.head()

    def git(self, *args, root=None):
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        return subprocess.check_output(["git", "-c", "safe.directory=" + str(root or self.root),
                                        "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                                        "-C", str(root or self.root), *args], env=env, stderr=subprocess.PIPE)

    def head(self):
        return self.git("rev-parse", "HEAD").decode().strip()

    def write_json(self, path, value, root=None):
        target = (root or self.root) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(ts.encoded(value) + b"\n")

    def write_maps(self, summary, root=None):
        base = root or self.root
        (base / "NAV").mkdir(exist_ok=True)
        (base / "NAV/NAVIGATION.md").write_text(
            "### `AREA-demo` Demo\n- Kind: area\n- Aliases: none\n- Summary: " + summary
            + "\n- Evidence: docs/guide.md\n- Related: none\n- Review: candidate\n- Revision: 1\n",
            encoding="utf-8")
        for name in ("SURFACES.md", "JOURNEYS.md", "SOURCE_FAMILIES.md"):
            (base / "NAV" / name).write_text("# Empty\n", encoding="utf-8")

    def descriptor(self, ref, *, path=None, main=False, checkout=False):
        commit = self.git("rev-parse", ref).decode().strip()
        kind = "working-tree" if path else "local-ref"
        return {"id": (ref + "-" + commit[:8]).replace("/", "-"), "label": ref, "kind": kind,
                "ref": "worktree:" + str(path) if path else ref, "commit": commit,
                "path": str(path) if path else None, "status": "observed",
                "is_main": main, "is_checkout": checkout}

    def record(self, summary, *, root=None, state="active"):
        selected = root or self.root
        identity = {"work_id": "work", "session_id": "session", "issuer_id": "issuer", "memory_id": "memory"}
        value = {"profile": ram.RECORD, "scope_id": "fixture.scope", "namespace": "fixture.native",
                 "identity": identity, "revision": 1, "receipts": {},
                 "controls": {"state": state, "visibility": "public", "expires_at": None,
                              "updated_at": "2026-10-06T12:00:00-05:00", "reason": "fixture"},
                 "versions": [{"revision": 1, "kind": "summary", "summary": summary,
                               "recorded_at": "2026-10-06T12:00:00-05:00", "occurred_at": None,
                               "unfinished": False, "creator": {"id": "fixture", "attribution": "host-supplied", "model": None},
                               "actors": [], "work": {"work_id": "work", "path": "WORK.md", "commit": self.base,
                                                     "sha256": digest((selected / "WORK.md").read_bytes())},
                               "selection": {"path": "WORK.md", "work_content_sha256": digest((selected / "WORK.md").read_bytes())},
                               "sources": [{"path": "docs/guide.md", "evidence": "inspected", "commit": self.base,
                                            "sha256": digest((selected / "docs/guide.md").read_bytes()), "git_blob": "fixture"}],
                               "children": [], "skill": None, "reason": "fixture"}]}
        path = "RAM/work/session/6-issuer--memory.json"
        self.write_json(path, value, selected)
        return path

    def test_branch_only_dedup_divergence_and_current_control(self):
        self.git("checkout", "-qb", "feature")
        path = self.record("branch only memory")
        self.git("add", "--all")
        self.git("commit", "-qm", "branch memory")
        feature = self.descriptor("feature")
        self.git("checkout", "-q", "main")
        main = self.descriptor("main", main=True)
        sources = [main, feature]
        result = knowledge.project(self.root, sources, modules=("ram",))
        self.assertEqual(result["ram"]["total"], 1, result["diagnostics"])
        self.assertEqual(result["ram"]["records"][0]["summary"], "branch only memory")
        pinned = result["ram"]["records"][0]
        self.assertEqual(knowledge.read_ram(self.root, sources, path=path, revision=1,
                         sha256=pinned["sha256"], source_id=feature["id"])["content"]["summary"],
                         "branch only memory")
        self.record("branch only memory")
        self.git("add", "--all")
        self.git("commit", "-qm", "same memory")
        main = self.descriptor("main", main=True)
        same = knowledge.project(self.root, [main, feature], modules=("ram",))
        self.assertFalse(same["ram"]["records"][0]["conflict"])
        self.assertEqual(len(same["ram"]["records"][0]["variants"][0]["sources"]), 2)
        self.record("corrected main memory")
        self.git("add", "--all")
        self.git("commit", "-qm", "correction")
        main = self.descriptor("main", main=True)
        different = knowledge.project(self.root, [main, feature], modules=("ram",))
        self.assertTrue(different["ram"]["records"][0]["conflict"])
        value = ts.loads((self.root / path).read_text(encoding="utf-8"))
        value["controls"]["state"] = "archived"
        self.write_json(path, value)
        archived = knowledge.project(self.root, [main, feature], modules=("ram",))
        self.assertEqual(archived["ram"]["total"], 0)
        explicit = knowledge.project(self.root, [main, feature], modules=("ram",), include_archived=True)
        self.assertEqual(explicit["ram"]["total"], 1)
        value["controls"]["state"] = "suppressed"
        self.write_json(path, value)
        hidden = knowledge.project(self.root, [main, feature], modules=("ram",))
        self.assertEqual(hidden["ram"]["total"], 0)
        value["controls"]["state"] = "active"
        self.write_json(path, value)
        self.catalog["bindings"].append({"resource_id": "hidden-memory", "path": path,
                                          "kind": "ram", "label": "Hidden memory",
                                          "visibility": "private", "state": "active"})
        self.write_json("eutonos.read.json", self.catalog)
        denied = knowledge.project(self.root, [main, feature], modules=("ram",))
        self.assertEqual(denied["ram"]["total"], 0)

    def test_repeated_ram_candidates_share_current_control_check_within_one_read(self):
        self.git("checkout", "-qb", "feature")
        self.record("same memory")
        self.git("add", "--all")
        self.git("commit", "-qm", "branch memory")
        feature = self.descriptor("feature")
        self.git("checkout", "-q", "main")
        self.record("same memory")
        self.git("add", "--all")
        self.git("commit", "-qm", "main memory")
        main = self.descriptor("main", main=True)
        with patch.object(knowledge, "_current_ram_control", wraps=knowledge._current_ram_control) as check:
            result = knowledge.project(self.root, [main, feature], modules=("ram",))
        self.assertEqual(result["ram"]["total"], 1)
        self.assertEqual(check.call_count, 2)  # One shared check and one fresh final check.

    def test_new_current_control_during_knowledge_read_hides_branch_ram(self):
        self.git("checkout", "-qb", "feature")
        self.record("branch memory")
        self.git("add", "--all")
        self.git("commit", "-qm", "branch memory")
        feature = self.descriptor("feature")
        self.git("checkout", "-q", "main")
        main = self.descriptor("main", main=True)

        def hide_during_documents(*_args):
            self.record("new local control", state="suppressed")
            return {"status": "available", "items": []}

        with patch.object(knowledge, "_document_rows", side_effect=hide_during_documents):
            changed = knowledge.project(self.root, [main, feature], modules=("ram", "documents"))
        self.assertEqual(changed["status"], "partial")
        self.assertEqual(changed["ram"]["status"], "partial")
        self.assertEqual(changed["ram"]["records"], [])
        self.assertTrue(any("controls changed" in item for item in changed["diagnostics"]))
        self.assertEqual(knowledge.project(self.root, [main, feature], modules=("ram",))["ram"]["total"], 0)
        self.record("new local control", state="active")
        self.assertEqual(knowledge.project(self.root, [main, feature], modules=("ram",))["ram"]["total"], 1)

    def test_child_control_change_during_knowledge_read_hides_parent(self):
        self.git("checkout", "-qb", "feature")
        parent_path = self.record("parent milestone")
        parent = ts.loads((self.root / parent_path).read_text(encoding="utf-8"))
        child = ts.loads((self.root / parent_path).read_text(encoding="utf-8"))
        child["identity"]["memory_id"] = "child"
        child["versions"][-1]["summary"] = "child source"
        child_path = "RAM/work/session/6-issuer--child.json"
        self.write_json(child_path, child)
        parent["versions"][-1]["kind"] = "milestone"
        parent["versions"][-1]["children"] = [
            {"identity": child["identity"], "path": child_path, "revision": 1}]
        self.write_json(parent_path, parent)
        self.git("add", "--all")
        self.git("commit", "-qm", "branch milestone")
        feature = self.descriptor("feature")
        self.git("checkout", "-q", "main")
        main = self.descriptor("main", main=True)

        def hide_child_during_documents(*_args):
            child["controls"]["visibility"] = "private"
            self.write_json(child_path, child)
            return {"status": "available", "items": []}

        with patch.object(knowledge, "_document_rows", side_effect=hide_child_during_documents):
            changed = knowledge.project(self.root, [main, feature], modules=("ram", "documents"))
        self.assertEqual(changed["status"], "partial")
        self.assertEqual(changed["ram"]["records"], [])
        self.assertTrue(any("controls changed" in item for item in changed["diagnostics"]))

    def _nav_cfg(self, excluded):
        return {"profile": "eutonos.tokenslang.nav-config.v1",
                "repository_id": "fixture.repo", "scope_id": "fixture.scope",
                "namespace": "fixture.native", "map_root": "NAV", "source_roots": ["src"],
                "excluded_roots": excluded, "context_paths": ["docs/guide.md"]}

    def test_nav_config_excluding_map_root_keeps_shared_nav_available(self):
        self.write_json("eutonos.nav.json", self._nav_cfg(["NAV"]))
        self.git("add", "--all")
        self.git("commit", "-qm", "exclude nav")
        result = knowledge.project(self.root, [self.descriptor("main", main=True)], modules=("nav",))
        self.assertEqual(result["nav"]["status"], "available", result["diagnostics"])
        self.assertEqual(result["nav"]["records"], 1)

    def test_catalog_denial_of_nav_maps_still_hides_nav(self):
        self.catalog["excluded_roots"] = ["NAV"]
        self.write_json("eutonos.read.json", self.catalog)
        self.git("add", "--all")
        self.git("commit", "-qm", "catalog hides nav")
        result = knowledge.project(self.root, [self.descriptor("main", main=True)], modules=("nav",))
        self.assertEqual(result["nav"]["records"], 0)
        self.assertNotEqual(result["nav"]["status"], "available")

    def test_nav_evidence_in_nav_excluded_root_stays_hidden(self):
        self.write_json("eutonos.nav.json", self._nav_cfg(["docs/private"]))
        (self.root / "NAV/NAVIGATION.md").write_text(
            "### `AREA-demo` Demo\n- Kind: area\n- Aliases: none\n- Summary: s\n"
            "- Evidence: docs/private/x.md\n- Related: none\n- Review: candidate\n- Revision: 1\n",
            encoding="utf-8")
        self.git("add", "--all")
        self.git("commit", "-qm", "private evidence")
        result = knowledge.project(self.root, [self.descriptor("main", main=True)], modules=("nav",))
        self.assertEqual(result["nav"]["records"], 0)

    def test_other_dirty_worktree_document_variants_and_nav_routes(self):
        other = Path(self.temp.name) / "other"
        self.git("worktree", "add", "-qb", "other", str(other))
        path = self.record("dirty peer memory", root=other)
        self.write_maps("peer map", root=other)
        (other / "docs/guide.md").write_text("# Guide\npeer edition\n", encoding="utf-8")
        main = self.descriptor("main", main=True)
        peer = self.descriptor("other", path=other)
        result = knowledge.project(self.root, [main, peer])
        self.assertEqual(result["ram"]["total"], 1, result["diagnostics"])
        self.assertEqual(result["nav"]["records"], 1)
        self.assertEqual(len(result["nav"]["routes"][0]["variants"]), 2)
        doc = next(item for item in result["documents"]["items"] if item["path"] == "docs/guide.md")
        self.assertTrue(doc["conflict"])
        peer_variant = next(v for v in doc["variants"] if v["source_id"] == peer["id"])
        expanded = knowledge.read_document(self.root, [main, peer], path="docs/guide.md",
                                           source_id=peer["id"], sha256=peer_variant["sha256"])
        self.assertIn("peer edition", expanded["content"])
        (other / "docs/guide.md").write_text("changed after pin", encoding="utf-8")
        with self.assertRaises(ts.ReadError):
            knowledge.read_document(self.root, [main, peer], path="docs/guide.md",
                                    source_id=peer["id"], sha256=peer_variant["sha256"])

    def test_large_commit_and_dirty_worktree_documents_page_without_loss(self):
        committed_text = "x" * 131071 + "🪐" + ("large UTF-8 document\n" * 30000)
        self.assertGreater(len(committed_text.encode("utf-8")), 512 * 1024)
        (self.root / "docs/guide.md").write_bytes(committed_text.encode("utf-8"))
        self.git("add", "docs/guide.md")
        self.git("commit", "-qm", "large document")
        other = Path(self.temp.name) / "other-large"
        self.git("worktree", "add", "-qb", "other-large", str(other))
        dirty_text = "🪐" + committed_text + "dirty worktree\n"
        (other / "docs/guide.md").write_bytes(dirty_text.encode("utf-8"))
        main = self.descriptor("main", main=True)
        peer = self.descriptor("other-large", path=other)
        result = knowledge.project(self.root, [main, peer], modules=("documents",))
        self.assertEqual(result["documents"]["status"], "available", result["diagnostics"])
        row = next(item for item in result["documents"]["items"] if item["path"] == "docs/guide.md")
        self.assertTrue(row["conflict"])
        for source, expected in ((main, committed_text), (peer, dirty_text)):
            variant = next(item for item in row["variants"] if item["source_id"] == source["id"])
            self.assertEqual(variant["bytes"], len(expected.encode("utf-8")))
            chunks = []
            offset = 0
            while True:
                page = knowledge.read_document(self.root, [main, peer], path="docs/guide.md",
                    source_id=source["id"], sha256=variant["sha256"], offset=offset)
                chunks.append(page["content"])
                self.assertEqual(page["total_bytes"], variant["bytes"])
                self.assertEqual(page["offset"], offset)
                if page["eof"]:
                    self.assertIsNone(page["next_offset"])
                    break
                self.assertGreater(page["next_offset"], offset)
                offset = page["next_offset"]
            self.assertEqual("".join(chunks), expected)
        tiny = knowledge.read_document(self.root, [main, peer], path="docs/guide.md",
            source_id=peer["id"], sha256=variant["sha256"], max_bytes=1)
        self.assertEqual(tiny["content"], "🪐")
        self.assertEqual(tiny["next_offset"], 4)
        for bad_offset, bad_size in ((-1, 8), (0, 0), (0, 131073), (1, 8), (10**9, 8)):
            with self.assertRaises(ts.ReadError):
                knowledge.read_document(self.root, [main, peer], path="docs/guide.md",
                    source_id=peer["id"], sha256=variant["sha256"],
                    offset=bad_offset, max_bytes=bad_size)
        (other / "docs/guide.md").write_bytes((dirty_text + "changed\n").encode("utf-8"))
        with self.assertRaises(ts.ReadError):
            knowledge.read_document(self.root, [main, peer], path="docs/guide.md",
                source_id=peer["id"], sha256=variant["sha256"], offset=4)

    def test_missing_measurement_and_fresh_clone(self):
        main = self.descriptor("main", main=True)
        result = knowledge.project(self.root, [main])
        self.assertEqual(result["measurement"]["status"], "not-configured")
        self.assertEqual(result["nav"]["records"], 1)
        clone = Path(self.temp.name) / "clone"
        self.git("clone", "-q", str(self.root), str(clone))
        commit = self.git("rev-parse", "HEAD", root=clone).decode().strip()
        fresh = {**main, "commit": commit, "id": "fresh-clone", "is_checkout": True}
        observed = knowledge.project(clone, [fresh], view="checkout")
        self.assertEqual(observed["nav"]["records"], 1)
        self.assertEqual(observed["documents"]["total"] > 0, True)

    def test_source_inventory_accepts_core_bound_and_reports_future_overflow(self):
        main = self.descriptor("main", main=True)
        aliases = [{**main, "id": f"alias-{index}", "ref": f"refs/heads/alias-{index}",
                    "is_main": index == 0} for index in range(640)]
        bounded = knowledge.project(self.root, aliases, modules=("nav",))
        self.assertEqual(bounded["status"], "available", bounded["diagnostics"])
        self.assertEqual(bounded["coverage"]["sources_input"], 640)
        self.assertEqual(bounded["coverage"]["sources_selected"], 1)
        self.assertEqual(bounded["nav"]["records"], 1)
        extra = {**main, "id": "extra", "ref": "refs/heads/extra", "is_main": False}
        overflow = knowledge.project(self.root, [*aliases, extra], modules=("nav",))
        self.assertEqual(overflow["status"], "partial")
        self.assertTrue(overflow["coverage"]["sources_truncated"])
        self.assertEqual(overflow["coverage"]["sources_omitted"], 1)
        self.assertEqual(overflow["nav"]["status"], "partial")

    def test_measurement_dedups_observation_identity_and_exposes_conflict(self):
        self.write_json("eutonos.measurement.json", {"profile": measurement.CONFIG,
                        "scope_id": "fixture.scope", "store_root": "observations", "mode": "metadata",
                        "retention": "explicit-owner-controls", "export": "explicit-guarded-publication"})
        self.git("add", "--all")
        self.git("commit", "-qm", "measurement config")
        self.git("checkout", "-qb", "feature")
        batch = {"profile": measurement.BATCH, "identity": {"issuer_id": "issuer", "batch_id": "batch"},
                 "revision": 1, "method": measurement.METHOD,
                 "selection": {"scope_id": "fixture.scope", "work_id": "work", "session_id": "session",
                               "attempt_id": "attempt", "source_commit": self.base},
                 "window": {"start": "2026-10-06T12:00:00-05:00", "end": "2026-10-06T12:00:01-05:00"},
                 "recorded_at": "2026-10-06T12:00:01-05:00",
                 "coverage": {"mode": "metadata", "extent": "partial", "dropped": 0,
                              "limitation": "Only selected event."},
                 "events": [{"id": "event-1", "logical_id": "logical-1", "parent_id": None,
                             "operation": "hub.read", "level": "entry", "outcome": "ok",
                             "elapsed_ns": None, "returned_bytes": None,
                             "provider_usage": None, "incident": None}],
                 "visibility": "public", "state": "active", "instrumentation_ns": None}
        self.write_json("observations/issuer/batch.json", batch)
        self.git("add", "--all")
        self.git("commit", "-qm", "branch observation")
        feature = self.descriptor("feature")
        self.git("checkout", "-q", "main")
        self.write_json("observations/issuer/batch.json", batch)
        self.git("add", "--all")
        self.git("commit", "-qm", "same observation")
        main = self.descriptor("main", main=True)
        same = knowledge.project(self.root, [main, feature], modules=("measurement",))
        self.assertEqual(same["measurement"]["event_count"], 1, same["diagnostics"])
        self.assertEqual(same["measurement"]["levels"]["entry"]["unknown_durations"], 1)
        changed = {**batch, "events": [{**batch["events"][0], "outcome": "error"}]}
        self.write_json("observations/issuer/batch.json", changed)
        self.git("add", "--all")
        self.git("commit", "-qm", "conflicting observation")
        main = self.descriptor("main", main=True)
        conflict = knowledge.project(self.root, [main, feature], modules=("measurement",))
        self.assertEqual(conflict["measurement"]["event_count"], 0)
        self.assertEqual(len(conflict["measurement"]["conflicts"]), 1)

    def test_shared_relevance_search_and_get_keep_exact_source_pin(self):
        self.git("checkout", "-qb", "feature")
        path = self.record("branch navigation insight")
        self.git("add", "--all")
        self.git("commit", "-qm", "branch memory")
        feature = self.descriptor("feature")
        self.git("checkout", "-q", "main")
        main = self.descriptor("main", main=True)
        with patch("eutonos.current_work.sources", return_value={"sources": [main, feature]}):
            result = ram_relevance.search(self.root, "navigation", explicit=True, view="current")
            self.assertEqual(result["total"], 1, result.get("diagnostics"))
            item = result["results"][0]
            self.assertEqual(item["source_id"], feature["id"])
            expanded = ram_relevance.get(self.root, path, item["revision"], item["sha256"],
                                         explicit=True, view="current", source=item["source_id"])
            self.assertEqual(expanded["content"]["summary"], "branch navigation insight")
            self.record("new dirty version", state="suppressed")
            with self.assertRaises(ts.ReadError):
                ram_relevance.get(self.root, path, item["revision"], item["sha256"],
                                  explicit=True, view="current", source=item["source_id"])


if __name__ == "__main__":
    unittest.main()
