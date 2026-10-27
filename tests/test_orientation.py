"""Orientation boundaries: context budgets, native pins, privacy and provenance."""
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from eutonos import orientation as api, tokenslang as ts


class OrientationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="eutonos-orientation-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "docs").mkdir()
        (self.root / "docs/WORK_MODEL.md").write_text("Canonical upstream reference\n", encoding="utf-8")
        (self.root / "docs/REGISTRY.json").write_text(json.dumps({"documents": [{"id": "eutonos.doc.work-model", "path": "docs/WORK_MODEL.md"}]}))
        (self.root / "AGENTS.md").write_text("Read native scope before work.\n", encoding="utf-8")
        (self.root / "WORK.md").write_text("Native selected work\n", encoding="utf-8")
        self.catalog = {"profile": ts.CATALOG, "repository_id": "fixture", "scope_id": "fixture",
                        "namespace": "fixture", "owners": {"current_task": {"scope_id": "fixture", "namespace": "fixture", "resource_id": "work"}},
                        "bindings": [{"resource_id": "work", "path": "WORK.md", "kind": "docs", "label": "Native work", "visibility": "public", "state": "active"}],
                        "excluded_roots": [], "source_paths": []}
        self.write_catalog()
        self.source = {"id": "a" * 24, "commit": "b" * 40, "ref": "refs/heads/main", "kind": "local-ref", "status": "observed"}
        self.item = {"id": "HAO2", "title": "Work title", "status": "active", "next_action": "Review implementation",
                     "owner_path": "WORK.md", "record_sha256": "c" * 64, "provenance": [self.source],
                     "extensions": {"checkpoint": {"result": "Native checkpoint"}}}
        self.shared = {"status": "partial", "sources": [self.source], "coverage": {"sources": "partial", "ram_truncated": True},
                       "freshness": {"kind": "stale", "age_seconds": 20}, "diagnostics": ["Limited scan"],
                       "boards": {"items": [self.item], "conflicts": [], "work_order": {"configured": True, "current": ["HAO2"]}}}
        patcher = mock.patch.object(api.current_work, "project", side_effect=lambda *a, **kw: deepcopy(self.shared))
        self.project = patcher.start()
        self.addCleanup(patcher.stop)

    def write_catalog(self):
        (self.root / "eutonos.read.json").write_bytes(ts.encoded(self.catalog))

    def test_cold_unknown_and_supplied_caller_preserve_native_scope_and_no_native_effects(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        with mock.patch.dict("os.environ", {"SESSION_ID": "parent-forged"}):
            result = api.orient(self.root)
        self.assertEqual(result["outcome"], "orientation", result)
        self.assertEqual(result["caller"]["confidence"], "unknown")
        self.assertEqual(result["selected_work"][0]["next_action"], "Review implementation")
        native = next(e["value"] for e in result["entries"] if e["kind"] == "selected-native-work")
        self.assertEqual(native["extensions"]["checkpoint"]["result"], "Native checkpoint")
        self.assertEqual(result["observation"]["coverage"], self.shared["coverage"])
        self.assertEqual(result["observation"]["freshness"]["kind"], "stale")
        self.assertFalse(result["required_context_complete"])
        rows = [e["value"] for e in result["entries"] if e["kind"] == "rule-reference"]
        self.assertEqual(next(r for r in rows if r["id"].endswith("onboarding"))["tier"], "Required-if")
        self.assertEqual(next(r for r in rows if r["id"].endswith("onboarding"))["applicability"], "unassessed")
        forged = api.orient(self.root, {"caller": {"actor": "owner", "session_id": "other-agent"}})
        self.assertEqual(forged["caller"]["confidence"], "unverified")
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})
        self.assertTrue(result["effects"])

    def test_budget_pages_preserve_required_entries_and_revalidate_drift(self):
        request = {"budget_bytes": 7500}
        first = api.orient(self.root, request)
        self.assertEqual(first["outcome"], "orientation", first)
        self.assertLessEqual(api.wire_bytes(first), 7500)
        self.assertGreater(first["omissions"]["required"], 0)
        pages = [first]
        cursor = first["continuation"]
        for _ in range(20):
            if cursor is None:
                break
            page = api.orient(self.root, {**request, "cursor": cursor})
            self.assertEqual(page["outcome"], "orientation", page)
            self.assertLessEqual(api.wire_bytes(page), 7500)
            pages.append(page)
            self.assertNotEqual(cursor, page["continuation"])
            cursor = page["continuation"]
        self.assertIsNone(cursor)
        self.assertEqual(len([e for p in pages for e in p["entries"] if e["kind"] == "rule-reference"]), 12)
        (self.root / "AGENTS.md").write_text("Changed required instructions", encoding="utf-8")
        self.assertEqual(api.orient(self.root, {**request, "cursor": first["continuation"]})["code"], "stale-cursor")
        small = api.orient(self.root, {"budget_bytes": 1024})
        self.assertEqual(small["code"], "budget-too-small")
        self.assertLessEqual(api.wire_bytes(small), 1024)

    def test_resume_reports_changed_and_missing_pins_and_retains_required_rules(self):
        first = api.orient(self.root)
        (self.root / "AGENTS.md").unlink()
        self.shared["sources"] = []
        resumed = api.orient(self.root, {"mode": "resume", "receipt": first["receipt"]})
        self.assertIn("instruction:AGENTS.md", resumed["delta"]["changed_pins"])
        self.assertIn("source:" + self.source["id"], resumed["delta"]["missing_previous_pins"])
        self.assertEqual(len([e for e in resumed["entries"] if e["kind"] == "rule-reference"]), 12)
        self.assertTrue(self.project.call_args.kwargs["refresh"])

    def test_private_denial_precedes_fallback_or_cached_titles(self):
        (self.root / "eutonos.personal.json").write_text("{}")
        result = api.orient(self.root, {"caller": {"actor": "SECRET"}, "profile": "bad"})
        self.assertEqual(result["code"], "denied")
        self.project.assert_not_called()
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertNotIn("Work title", json.dumps(result))

    def test_invalid_denied_conflicting_and_stale_catalog_never_fall_back(self):
        for change, code in (({"visibility": "private"}, "denied"), ({"sha256": "0" * 64}, "stale-owner")):
            with self.subTest(code=code):
                original = deepcopy(self.catalog)
                self.catalog["bindings"][0].update(change)
                self.write_catalog()
                self.assertEqual(api.orient(self.root)["code"], code)
                self.catalog = original
        self.catalog["bindings"].append(deepcopy(self.catalog["bindings"][0]))
        self.write_catalog()
        self.assertEqual(api.orient(self.root)["code"], "ambiguous")
        self.project.assert_not_called()

    def test_unadopted_missing_owner_and_unsupported_schema_are_distinct(self):
        (self.root / "eutonos.read.json").unlink()
        result = api.orient(self.root)
        self.assertEqual(result["setup"], "unadopted-compatibility")
        self.assertTrue(any(g["kind"] == "task-owner" for g in result["gaps"]))
        self.assertFalse(result["required_context_complete"])
        self.assertEqual(result["code"], "partial-context")
        self.assertTrue(result["fallback"])
        self.assertEqual(api.orient(self.root, {"packet_profile": "v999"})["code"], "incompatible-schema")
        self.assertEqual(api.orient(self.root, {"command": "send"})["code"], "unsupported-request")

    def test_malformed_registry_stays_local_and_swapped_catalog_refuses_before_projection(self):
        registry = self.root / "docs/REGISTRY.json"
        valid = {"id": "eutonos.doc.work-model", "path": "docs/WORK_MODEL.md"}
        for body, code in ((b"[]", "invalid"), (b'"x"', "invalid"), (b'{"documents": 1}', "invalid"), (b"{", "invalid"),
                           (b"\xff", "invalid"), (('{"documents": [], "documents": [' + json.dumps(valid) + "]}").encode(), "ambiguous")):
            with self.subTest(body=body):
                registry.write_bytes(body)
                result = api.orient(self.root)
                self.assertEqual((result["outcome"], result["code"]), ("orientation", "partial-context"), result)
                self.assertIn({"kind": "registry", "state": code}, [{k: g.get(k) for k in ("kind", "state")} for g in result["gaps"]])
                row = next(e["value"] for e in result["entries"] if e["id"].endswith("delivery"))
                self.assertEqual((row["source"]["path"], row["source"]["state"]), (None, "missing"))
                self.assertFalse(result["required_context_complete"])
        self.project.reset_mock()
        registry.write_text(json.dumps({"documents": [valid]}))
        from eutonos import workflow
        original = workflow._catalog_task_owner
        self.catalog["excluded_roots"] = ["AGENTS.md"]
        self.write_catalog()

        def swapped(root):
            # Owner selection used the stricter catalog; a looser one is never applied to reads.
            owner = original(root)
            self.catalog["excluded_roots"] = []
            self.write_catalog()
            return owner
        with mock.patch.object(workflow, "_catalog_task_owner", swapped):
            result = api.orient(self.root)
        self.assertEqual(result["code"], "changed-input")
        self.assertNotIn("Read native scope", json.dumps(result))
        self.project.assert_not_called()

    def test_conflicts_and_explicit_missing_work_are_not_selected_as_valid_work(self):
        self.shared["boards"]["items"] = [{**self.item, "status": "conflict"}]
        self.shared["boards"]["conflicts"] = [{"id": "HAO2", "candidates": [self.item]}]
        result = api.orient(self.root, {"work_id": "HAO2"})
        self.assertFalse(result["selected_work_ids"])
        self.assertTrue(any(e["kind"] == "native-conflict" for e in result["entries"]))
        self.assertTrue(any(g["kind"] == "selected-work" for g in result["gaps"]))

    def test_upstream_reference_drift_missing_and_denied_revalidate_without_native_rebinding(self):
        first = api.orient(self.root, {"budget_bytes": 10000})
        path = self.root / "docs/WORK_MODEL.md"
        path.write_text("Changed upstream guidance", encoding="utf-8")
        resumed = api.orient(self.root, {"mode": "resume", "receipt": first["receipt"]})
        self.assertIn("reference:docs/WORK_MODEL.md", resumed["delta"]["changed_pins"])
        self.assertEqual(api.orient(self.root, {"budget_bytes": 10000, "cursor": first["continuation"]})["code"], "stale-cursor")
        path.unlink()
        missing = api.orient(self.root)
        self.assertTrue(any(g["kind"] == "required-reference" and g["id"].endswith("delivery") for g in missing["gaps"]))
        self.catalog["excluded_roots"] = ["docs/WORK_MODEL.md"]
        self.write_catalog()
        self.assertEqual(api.orient(self.root)["code"], "denied")

    def test_portable_reference_is_guidance_and_does_not_rebind_native_owner(self):
        directory = self.root / "reference/docs"
        directory.mkdir(parents=True)
        (directory / "WORK_MODEL.md").write_text("Portable upstream guidance", encoding="utf-8")
        result = api.orient(self.root)
        self.assertEqual(result["task_owner"]["path"], "WORK.md")
        row = next(e["value"] for e in result["entries"] if e["id"].endswith("delivery"))
        self.assertEqual(row["source"]["path"], "reference/docs/WORK_MODEL.md")
        self.assertIn("upstream-guidance", row["source"]["scope"])
        (self.root / "reference/eutonos.personal.json").write_text("{}")
        private = api.orient(self.root)
        self.assertEqual(private["code"], "denied")
        self.assertNotIn("Portable upstream guidance", json.dumps(private))

    def test_control_changed_during_projection_refuses_the_mixed_packet(self):
        def changed(*args, **kwargs):
            self.catalog["bindings"][0]["visibility"] = "private"
            self.write_catalog()
            return deepcopy(self.shared)
        self.project.side_effect = changed
        result = api.orient(self.root)
        self.assertEqual(result["code"], "changed-input")
        self.assertNotIn("Work title", json.dumps(result))

    def test_reference_scope_revoked_during_projection_withholds_all_metadata(self):
        directory = self.root / "reference/docs"
        directory.mkdir(parents=True)
        (directory / "WORK_MODEL.md").write_text("Private after projection", encoding="utf-8")
        def revoked(*args, **kwargs):
            (self.root / "reference/eutonos.personal.json").write_text("{}")
            return deepcopy(self.shared)
        self.project.side_effect = revoked
        result = api.orient(self.root)
        self.assertEqual(result["code"], "denied")
        self.assertNotIn("reference/docs/WORK_MODEL.md", json.dumps(result))
        self.assertNotIn("Work title", json.dumps(result))

    def test_nonfinite_direct_request_is_invalid_without_context_reads(self):
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value):
                result = api.orient(self.root, {"caller": {"actor": "SECRET"}, "ram": [{"search_coverage": {"total": value}}]})
                self.assertEqual(result["code"], "invalid")
                self.assertNotIn("SECRET", json.dumps(result))
        self.project.assert_not_called()

    def test_exact_budget_includes_windows_cli_crlf_bytes(self):
        from eutonos import workflow_cli
        with mock.patch.object(api.os, "linesep", "\r\n"):
            packet = api.orient(self.root)
            budget = api.wire_bytes(packet)
            request = self.root / "request.json"
            request.write_text(json.dumps({"budget_bytes": budget}), encoding="utf-8")
            output = io.BytesIO()
            console = io.TextIOWrapper(output, encoding="utf-8", newline="\r\n")
            with mock.patch("sys.stdout", console):
                self.assertEqual(workflow_cli.main(["orient", "--root", str(self.root), "--request", str(request)]), 0)
            console.flush()
            self.assertEqual(len(output.getvalue()), budget)
            self.assertTrue(output.getvalue().endswith(b"\r\n"))
            console.detach()

    def test_exact_ram_keeps_native_source_and_unassessed_partial_search(self):
        pin = {"source_id": self.source["id"], "path": "RAM/r.json", "revision": 1, "sha256": "d" * 64,
               "search_coverage": {"status": "partial", "sources_scanned": 29, "sources_total": 31, "ram_truncated": True}}
        native = {"outcome": "ok", **{k: v for k, v in pin.items() if k != "search_coverage"},
                  "content": {"result": "Historical experience"}, "semantic_applicability": {"state": "unassessed"}}
        with mock.patch.object(api.ram_relevance, "get", return_value=native) as get:
            result = api.orient(self.root, {"ram": [pin]})
        self.assertEqual(get.call_args.kwargs["view"], "source")
        value = next(e["value"] for e in result["entries"] if e["kind"] == "ram-experience")
        self.assertEqual(value["source"]["commit"], self.source["commit"])
        self.assertEqual(value["semantic_applicability"]["state"], "unassessed")
        self.assertEqual(value["search_coverage"]["value"], pin["search_coverage"])
        disabled = {"outcome": "disabled", "path": pin["path"], "qualification": "optional RAM retrieval is off"}
        with mock.patch.object(api.ram_relevance, "get", return_value=disabled):
            off = api.orient(self.root, {"ram": [pin]})
        gap = next(g for g in off["gaps"] if g["kind"] == "ram-experience")
        self.assertEqual(gap["state"], "disabled")
        self.assertNotIn("substituted local content was withheld", gap["next_action"])
        for returned in ({**native, "source_id": "other"}, {**native, "effective_view": "local", "content": {"result": "SECRET LOCAL"}}, native):
            with self.subTest(returned=returned), mock.patch.object(api.ram_relevance, "get", return_value=returned):
                self.shared["sources"] = []
                withheld = api.orient(self.root, {"ram": [pin]})
                self.assertFalse(any(e["kind"] == "ram-experience" for e in withheld["entries"]))
                self.assertNotIn("SECRET LOCAL", json.dumps(withheld))
                self.assertTrue(any(g["kind"] == "ram-experience" for g in withheld["gaps"]))


if __name__ == "__main__":
    unittest.main()
