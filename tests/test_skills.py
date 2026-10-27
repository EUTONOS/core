import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout

from eutonos import skills, skills_cli
from eutonos.companion_history import set_tracking


class SkillsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="skills-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "docs/skills/build").mkdir(parents=True)
        self.body = b"# Build release\n\nRun the focused checks, then package the result.\n"
        (self.root / "docs/skills/build/SKILL.md").write_bytes(self.body)
        self.write_catalog([self.item()])

    def item(self, **changes):
        value = {
            "id": "project.skill.build-release",
            "title": "Build the release",
            "description": "Package a verified application release.",
            "version": "1.2.0",
            "status": "usable",
            "tools": ["packager"],
            "tags": ["release", "python"],
            "aliases": ["ship application", "package build"],
            "relationships": {
                "uses_tool": ["packager"],
                "applies_to": ["application"],
                "depends_on": ["project.skill.test"],
                "verified_by": ["tests/release"],
                "supersedes": [],
            },
            "source": {"path": "docs/skills/build/SKILL.md",
                       "sha256": hashlib.sha256(self.body).hexdigest()},
            "evidence": [{"type": "test", "reference": "tests/release",
                          "summary": "Focused fixture passed."}],
            "prerequisites": ["Clean selected release inputs"],
            "inputs": ["Release version"],
            "constraints": ["Do not publish without authority"],
            "expected_outcome": "A locally verified package.",
        }
        value.update(changes)
        return value

    def write_catalog(self, items, **changes):
        value = {"profile": skills.PROFILE, "source_roots": ["docs/skills"],
                 "excluded_roots": [], "skills": items}
        value.update(changes)
        (self.root / skills.CATALOG_PATH).write_text(json.dumps(value), encoding="utf-8")

    def request(self, event="caller-event-1", outcome="succeeded"):
        item = self.item()
        return {"profile": skills.USE_PROFILE, "event_id": event,
                "skill_id": item["id"], "skill_version": item["version"],
                "skill_sha256": item["source"]["sha256"], "outcome": outcome,
                "evidence": ["focused-check"]}

    def test_useful_query_alias_ranking_exact_filters_and_pagination(self):
        second_body = b"# Inspect deployment\n"
        (self.root / "docs/skills/deploy").mkdir()
        (self.root / "docs/skills/deploy/SKILL.md").write_bytes(second_body)
        second = self.item(id="project.skill.inspect-deploy", title="Inspect deployment",
                           description="Read deployment state.", version="2", status="candidate",
                           tools=["deployer"], tags=["inspection"], aliases=["check deployed app"],
                           source={"path": "docs/skills/deploy/SKILL.md",
                                   "sha256": hashlib.sha256(second_body).hexdigest()})
        self.write_catalog([self.item(), second])
        result = skills.search(self.root, "package build", tool="packager", status="usable", limit=1)
        self.assertEqual((result["catalog_status"], result["total"]), ("available", 1))
        self.assertEqual(result["items"][0]["id"], "project.skill.build-release")
        self.assertEqual(result["items"][0]["match"]["terms"], ["package", "build"])
        self.assertEqual(skills.search(self.root, "absent task")["total"], 0)
        self.assertEqual(skills.search(self.root, tool="deployer", offset=0, limit=1)["items"][0]["status"], "candidate")
        with self.assertRaises(Exception):
            skills.search(self.root, limit=True)

    def test_unconfigured_empty_and_no_match_are_distinct(self):
        other = self.root / "other"; other.mkdir()
        self.assertEqual(skills.project(other)["status"], "not-configured")
        self.write_catalog([])
        self.assertEqual(skills.project(self.root)["status"], "empty")
        self.write_catalog([self.item()])
        result = skills.search(self.root, "not represented")
        self.assertEqual((result["catalog_status"], result["total"], result["items"]),
                         ("available", 0, []))

    def test_duplicate_ids_and_invalid_catalog_are_errors(self):
        self.write_catalog([self.item(), self.item(version="2")])
        result = skills.project(self.root)
        self.assertEqual(result["status"], "error")
        self.assertIn("Duplicate skill ID", result["diagnostics"][0])
        (self.root / skills.CATALOG_PATH).write_text('{"profile":"wrong"}', encoding="utf-8")
        self.assertEqual(skills.project(self.root)["status"], "error")

    def test_unsafe_excluded_nested_and_symlink_sources_never_expand(self):
        unsafe = self.item(id="project.skill.unsafe", source={"path": "../outside.md", "sha256": "0" * 64})
        self.write_catalog([unsafe])
        row = skills.project(self.root)["items"][0]
        self.assertEqual(row["source"]["state"], "invalid")
        self.assertNotIn("body", skills.get(self.root, unsafe["id"]))

        self.write_catalog([self.item()], excluded_roots=["docs/skills/build"])
        self.assertEqual(skills.project(self.root)["items"][0]["source"]["state"], "excluded")

        self.write_catalog([self.item()])
        (self.root / "docs/skills/.git").mkdir()
        self.assertEqual(skills.project(self.root)["items"][0]["source"]["state"], "excluded")
        (self.root / "docs/skills/.git").rmdir()

        link = self.root / "docs/skills/link"
        try:
            link.symlink_to(self.root / "docs/skills/build", target_is_directory=True)
        except OSError:
            return
        linked = self.item(id="project.skill.link", source={"path": "docs/skills/link/SKILL.md",
                            "sha256": hashlib.sha256(self.body).hexdigest()})
        self.write_catalog([linked])
        self.assertEqual(skills.project(self.root)["items"][0]["source"]["state"], "invalid")

    def test_global_read_catalog_exclusion_cannot_be_overridden(self):
        read_catalog = {"profile": "eutonos.tokenslang.catalog.v1", "repository_id": "repo",
                        "scope_id": "scope", "namespace": "ns", "owners": {}, "bindings": [],
                        "excluded_roots": ["docs/skills/build"], "source_paths": []}
        (self.root / "eutonos.read.json").write_text(json.dumps(read_catalog), encoding="utf-8")
        result = skills.project(self.root)
        self.assertEqual(result["items"][0]["source"]["state"], "excluded")

    def test_source_change_and_pins_never_return_stale_body(self):
        item = self.item()
        available = skills.get(self.root, item["id"], item["version"], item["source"]["sha256"])
        self.assertEqual(available["body"], self.body.decode())
        self.assertEqual(skills.get(self.root, item["id"], "old")["status"], "version-mismatch")
        self.assertEqual(skills.get(self.root, item["id"], sha256="f" * 64)["status"], "hash-mismatch")
        (self.root / "docs/skills/build/SKILL.md").write_text("changed", encoding="utf-8")
        stale = skills.get(self.root, item["id"])
        self.assertEqual(stale["status"], "stale")
        self.assertNotIn("body", stale)

    def test_ram_is_irrelevant_and_reads_do_not_record_use(self):
        (self.root / "eutonos.ram.json").write_text("malformed RAM is not consulted", encoding="utf-8")
        before = skills.project(self.root)
        self.assertEqual(before["usage_coverage"]["status"], "missing")
        for _ in range(3):
            self.assertEqual(skills.search(self.root, "release")["total"], 1)
            self.assertEqual(skills.get(self.root, self.item()["id"])["status"], "available")
        self.assertFalse((self.root / skills.USAGE_PATH).exists())

    def test_explicit_receipt_counts_outcomes_and_exact_replay(self):
        first = skills.record_use(self.root, self.request())
        replay = skills.record_use(self.root, self.request())
        skills.record_use(self.root, self.request("caller-event-2", "failed"))
        skills.record_use(self.root, self.request("caller-event-3", "unknown"))
        self.assertEqual((first["outcome"], replay["outcome"]), ("recorded", "already-recorded"))
        usage = skills.project(self.root)["items"][0]["usage"]
        self.assertEqual((usage["count"], usage["success_count"], usage["failure_count"], usage["coverage"]),
                         (3, 1, 1, "complete"))
        self.assertIsNotNone(usage["last_used"])
        conflict = self.request(outcome="failed")
        with self.assertRaises(Exception):
            skills.record_use(self.root, conflict)

    def test_parallel_replay_creates_one_complete_receipt(self):
        request = self.request("parallel-event")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: skills.record_use(self.root, request), range(16)))
        self.assertEqual(sum(result["outcome"] == "recorded" for result in results), 1)
        self.assertEqual(sum(result["outcome"] == "already-recorded" for result in results), 15)
        files = list((self.root / skills.USAGE_PATH).glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(skills.project(self.root)["items"][0]["usage"]["count"], 1)

    def test_missing_empty_disabled_and_malformed_usage_have_honest_coverage(self):
        missing = skills.project(self.root)["items"][0]["usage"]
        self.assertIsNone(missing["count"])
        usage_dir = self.root / skills.USAGE_PATH
        usage_dir.mkdir(parents=True)
        empty = skills.project(self.root)["items"][0]["usage"]
        self.assertEqual((empty["count"], empty["coverage"]), (0, "complete"))
        (usage_dir / ("0" * 64 + ".json")).write_text("{", encoding="utf-8")
        partial = skills.project(self.root)
        self.assertEqual(partial["usage_coverage"]["status"], "partial")
        self.assertIsNone(partial["items"][0]["usage"]["count"])
        (usage_dir / ("0" * 64 + ".json")).unlink()
        set_tracking(self.root, False)
        disabled = skills.project(self.root)
        self.assertEqual(disabled["usage_coverage"]["status"], "disabled")
        self.assertIsNone(disabled["items"][0]["usage"]["count"])
        self.assertEqual(skills.record_use(self.root, self.request("disabled-event"))["outcome"], "disabled")

    def test_cli_json_read_and_record_use(self):
        with redirect_stdout(io.StringIO()) as output:
            code = skills_cli.main(["search", "release", "--root", str(self.root)])
        self.assertEqual((code, json.loads(output.getvalue())["total"]), (0, 1))
        request_path = self.root / "request.json"
        request_path.write_text(json.dumps(self.request("cli-event")), encoding="utf-8")
        with redirect_stdout(io.StringIO()) as output:
            code = skills_cli.main(["record-use", "--root", str(self.root), "--request", str(request_path)])
        self.assertEqual((code, json.loads(output.getvalue())["outcome"]), (0, "recorded"))


if __name__ == "__main__":
    unittest.main()
