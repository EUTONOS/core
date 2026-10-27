"""Focused eligibility, paging, exact-read and local-feedback checks."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from eutonos import ram, ram_relevance as relevance, settings, tokenslang as ts
from eutonos.effects import MARKER, MARKER_BYTES, digest


class RamRelevanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eutonos-relevance-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "fixture"
        (self.root / "RAM/work/session").mkdir(parents=True)
        (self.root / "src").mkdir()
        (self.root / MARKER).write_bytes(MARKER_BYTES)
        (self.root / "WORK.md").write_text("# Selected work\n", encoding="utf-8")
        (self.root / "src/code.py").write_text("def navigation(): return 1\n", encoding="utf-8")
        self.cfg = {"profile": ram.CONFIG, "repository_id": "fixture.repo", "scope_id": "fixture.scope",
                    "namespace": "fixture.native", "store_root": "RAM", "current_task": "WORK.md",
                    "work_owners": {"work": "WORK.md"}}
        (self.root / "eutonos.ram.json").write_bytes(ts.encoded(self.cfg))
        self.catalog = {"profile": ts.CATALOG, "repository_id": "fixture.repo", "scope_id": "fixture.scope",
                        "namespace": "fixture.native", "owners": {"current_task": {"scope_id": "fixture.scope",
                        "namespace": "fixture.native", "resource_id": "work.owner"}},
                        "bindings": [{"resource_id": "work.owner", "path": "WORK.md", "kind": "docs",
                                      "label": "Work", "visibility": "public", "state": "active"}],
                        "excluded_roots": [], "source_paths": ["WORK.md", "src/code.py"]}
        (self.root / "eutonos.read.json").write_bytes(ts.encoded(self.catalog))
        self.git("init", "-q", "-b", "main")
        self.git("add", "--all")
        self.git("commit", "-qm", "fixture")
        self.commit = self.git("rev-parse", "HEAD").decode().strip()
        self.path = self.write_record("memory", "Generic opening. Navigation scaling retained the exact source route.")

    def git(self, *args):
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        return subprocess.check_output(["git", "-c", "safe.directory=" + str(self.root),
                                        "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                                        "-C", str(self.root), *args], env=env, stderr=subprocess.PIPE)

    def record(self, memory_id, summary, *, state="active", visibility="public", source="src/code.py"):
        identity = {"work_id": "work", "session_id": "session", "issuer_id": "issuer", "memory_id": memory_id}
        return {"profile": ram.RECORD, "scope_id": "fixture.scope", "namespace": "fixture.native",
                "identity": identity, "revision": 1, "receipts": {},
                "controls": {"state": state, "visibility": visibility, "expires_at": None,
                             "updated_at": "2026-09-13T10:00:00-05:00", "reason": "fixture"},
                "versions": [{"revision": 1, "kind": "summary", "summary": summary,
                              "recorded_at": "2026-09-13T10:00:00-05:00", "occurred_at": None,
                              "unfinished": False, "creator": {"id": "fixture", "attribution": "host-supplied", "model": None},
                              "actors": [], "work": {"work_id": "work", "path": "WORK.md", "commit": self.commit,
                              "sha256": digest((self.root / "WORK.md").read_bytes())},
                              "selection": {"path": "WORK.md", "work_content_sha256": digest((self.root / "WORK.md").read_bytes())},
                              "sources": [{"path": source, "evidence": "inspected", "commit": self.commit,
                                           "sha256": digest((self.root / source).read_bytes()), "git_blob": "fixture"}],
                              "children": [], "skill": None, "reason": "fixture"}]}

    def write_record(self, memory_id, summary, **kwargs):
        value = self.record(memory_id, summary, **kwargs)
        path = f"RAM/work/session/6-issuer--{memory_id}.json"
        (self.root / path).write_bytes(ts.encoded(value))
        return path

    def reject(self, code, function, *args, **kwargs):
        with self.assertRaises(ts.ReadError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_ranked_query_no_match_and_contextual_preview(self):
        none = relevance.search(self.root, "unrelated-zebra")
        self.assertEqual(none["results"], [])
        self.assertEqual(none["total"], 0)
        long_path = self.write_record("long", "Generic closeout " * 30 + "needle evidence is here")
        result = relevance.search(self.root, "needle")
        self.assertEqual(result["results"][0]["path"], long_path)
        self.assertIn("needle", result["results"][0]["preview"])
        self.assertLessEqual(len(result["results"][0]["preview"]), 280)
        self.assertEqual(result["results"][0]["ranking_basis"][0]["field"], "summary")

    def test_retrieval_off_and_explicit_blank_browse(self):
        current = settings.status(self.root)
        settings.update(self.root, 0, current["revision"], "ram", {"retrieval": "off", "capture": True})
        self.assertEqual(relevance.search(self.root, "navigation")["outcome"], "disabled")
        selected = relevance.search(self.root, "", explicit=True)
        self.assertEqual(selected["total"], 1)
        item = selected["results"][0]
        self.assertEqual(relevance.get(self.root, item["path"], item["revision"], item["sha256"])["outcome"], "disabled")
        self.assertEqual(relevance.get(self.root, item["path"], item["revision"], item["sha256"], explicit=True)["outcome"], "ok")
        self.reject("incomplete", relevance.search, self.root, "")

    def test_exact_expansion_detects_record_drift_and_source_staleness(self):
        item = relevance.search(self.root, "navigation")["results"][0]
        expanded = relevance.get(self.root, item["path"], item["revision"], item["sha256"])
        self.assertEqual(expanded["freshness"][0]["state"], "match")
        (self.root / "src/code.py").write_text("changed\n", encoding="utf-8")
        stale_source = relevance.get(self.root, item["path"], item["revision"], item["sha256"])
        self.assertEqual(stale_source["freshness"][0]["state"], "stale")
        target = self.root / item["path"]
        target.write_bytes(target.read_bytes() + b"\n")
        self.reject("stale", relevance.get, self.root, item["path"], item["revision"], item["sha256"])

    def test_same_source_distinct_pins_keep_distinct_freshness(self):
        stale_path = self.write_record("older", "navigation older evidence")
        stale = json.loads((self.root / stale_path).read_text(encoding="utf-8"))
        stale["versions"][0]["sources"][0]["sha256"] = "0" * 64
        (self.root / stale_path).write_bytes(ts.encoded(stale))
        results = relevance.search(self.root, "navigation", limit=5)["results"]
        states = {item["path"]: item["freshness"][0]["state"] for item in results}
        self.assertEqual(states[self.path], "match")
        self.assertEqual(states[stale_path], "stale")

    def test_paging_has_no_store_history_ceiling(self):
        for index in range(260):
            self.write_record(f"item{index:03}", f"paged common record {index}")
        first = relevance.search(self.root, "common", limit=5)
        self.assertEqual(first["total"], 260)
        self.assertEqual(first["next_offset"], 5)
        last = relevance.search(self.root, "common", limit=10, offset=255)
        self.assertEqual(len(last["results"]), 5)
        self.assertIsNone(last["next_offset"])

    def test_private_archived_excluded_and_catalog_scope_controls(self):
        private = self.write_record("private", "secret navigation", visibility="private")
        archived = self.write_record("archived", "old navigation", state="archived")
        denied = self.write_record("denied", "hidden navigation")
        self.catalog["bindings"].append({"resource_id": "hidden", "path": denied, "kind": "ram",
                                         "label": "Hidden", "visibility": "private", "state": "active"})
        (self.root / "eutonos.read.json").write_bytes(ts.encoded(self.catalog))
        results = relevance.search(self.root, "navigation")["results"]
        self.assertNotIn(private, [item["path"] for item in results])
        self.assertNotIn(archived, [item["path"] for item in results])
        self.assertNotIn(denied, [item["path"] for item in results])
        self.assertIn(archived, [item["path"] for item in relevance.search(
            self.root, "navigation", include_archived=True)["results"]])
        self.catalog["scope_id"] = "other"
        (self.root / "eutonos.read.json").write_bytes(ts.encoded(self.catalog))
        self.reject("wrong-scope", relevance.open_store, self.root)

    def test_excluded_source_makes_record_ineligible_without_leak(self):
        self.catalog["excluded_roots"] = ["src"]
        (self.root / "eutonos.read.json").write_bytes(ts.encoded(self.catalog))
        result = relevance.search(self.root, "navigation")
        self.assertEqual(result["results"], [])

    def test_feedback_is_exact_local_idempotent_and_readable(self):
        item = relevance.search(self.root, "navigation")["results"][0]
        before = (self.root / self.path).read_bytes()
        request = {"profile": relevance.FEEDBACK, "idempotency_key": "feedback-1", "path": item["path"],
                   "revision": item["revision"], "sha256": item["sha256"], "actor": "worker",
                   "assessment": "useful", "reason": "Saved repeated discovery"}
        first = relevance.feedback(self.root, request)
        self.assertEqual(first["outcome"], "recorded")
        self.assertEqual(relevance.feedback(self.root, request)["outcome"], "unchanged")
        feedback = relevance.read_feedback(self.root, item["path"], item["revision"], item["sha256"])
        self.assertEqual(feedback["total"], 1)
        self.assertEqual(feedback["entries"][0]["assessment"], "useful")
        self.assertEqual((self.root / self.path).read_bytes(), before)
        self.reject("conflict", relevance.feedback, self.root, {**request, "assessment": "irrelevant"})

    def test_cli_profile_and_direct_find(self):
        profile = subprocess.run([sys.executable, "-m", "eutonos.ram_relevance_cli", "profile", "--root", str(self.root)],
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(profile.returncode, 0, profile.stdout)
        self.assertIn("feedback_read", json.loads(profile.stdout))
        found = subprocess.run([sys.executable, "-m", "eutonos.ram_relevance_cli", "find", "navigation",
                                "--root", str(self.root)], capture_output=True, text=True, timeout=30)
        self.assertEqual(found.returncode, 0, found.stdout)
        self.assertEqual(json.loads(found.stdout)["total"], 1)


if __name__ == "__main__":
    unittest.main()
