"""Disposable Git/native-host qualification; no real owner import or runtime."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
from eutonos import board_import, boards, closeout_route, work_closeout, work_lifecycle as work, write_batch, write_retry, tokenslang as ts
from eutonos.effects import LocalRoot, MARKER, MARKER_BYTES, digest
from eutonos.host import enroll, renew, connect
from eutonos.source import GitView


class BoardImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"; self.root.mkdir()
        self.host = Path(self.temp.name) / "host"
        (self.root / "docs/campaigns/done").mkdir(parents=True)
        self.source_path = "docs/campaigns/legacy.md"
        self.source_items = [self.item("OLD", "campaign", "completed"),
            self.item("OLD1", "stage", "completed", parent_id="OLD",
                      extensions={"work_history": [{"id": "old-completion", "status": "completed"}]}),
            self.item("HELD", "slice", "active")]
        self.write_board(self.source_path, self.source_items)
        self.write_board("docs/WORK_ITEMS.md", [self.item("LIVE", "slice", "planned")])
        self.put("eutonos.boards.json", json.dumps({"profile": boards.CONFIG, "owners": ["docs/WORK_ITEMS.md"]}))
        self.put(work.PATH, json.dumps({"profile": work.CONFIG, "current": [], "next": ["LIVE"]}))
        self.put("docs/CHANGELOG.md", "# Changes\n")
        self.put("docs/admission.md", "Disposable host admission.\n")
        self.put("README.md", "# Fixture\n")
        self.put("AGENTS.md", "# Fixture instructions\n")
        self.put("docs/REGISTRY.json", json.dumps({"schema": "eutonos.documentation-bootstrap.v0",
            "retained": {"note": "preserve unrelated metadata"}, "documents": [
                {"id": "fixture.doc." + str(index), "role": "fixture-role-" + str(index), "path": path}
                for index, path in enumerate(["README.md", "AGENTS.md", "docs/WORK_ITEMS.md",
                                             "docs/admission.md", "docs/CHANGELOG.md"])]}) + "\n")
        (self.root / MARKER).write_bytes(MARKER_BYTES)
        self.git("init", "-q", "-b", "main"); self.commit()
        self.pin = self.git("rev-parse", "HEAD").strip()
        view = GitView(self.root, self.pin); _, self.metadata = view.read(self.source_path)
        (self.root / self.source_path).unlink(); self.commit()
        enroll(self.root, self.host, "writer", ["docs/admission.md"], shared=True)

    def git(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        return subprocess.run(["git", "-C", str(self.root), *args], env=env,
                              check=True, capture_output=True, text=True).stdout

    def commit(self):
        self.git("add", "--all")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")

    def put(self, path, body):
        target = self.root / path; target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8", newline="\n")

    def write_board(self, path, items):
        self.put(path, "# Preserved owner prose\n\n```tokenslang-board\n" +
                 json.dumps({"profile": boards.BOARD, "items": items}) + "\n```\n")

    def item(self, identity, kind="slice", status="planned", **extra):
        return {"id": identity, "type": kind, "status": status, "title": identity, "summary": "Source scope", **extra}

    def request(self, mode="register", owner="docs/campaigns/imported.md", items=None):
        local = LocalRoot(self.root)
        selected = self.source_items if items is None else items
        request = {"profile": board_import.PROFILE, "mode": mode, "owner_path": owner,
            "expected_config_sha256": digest(local.read("eutonos.boards.json")),
            "expected_owner_sha256": digest(local.read(owner)),
            "source": {"commit": self.pin, "path": self.source_path,
                       "blob_oid": self.metadata["git_blob"], "sha256": self.metadata["sha256"]},
            "items": [{"id": i["id"], "sha256": digest(ts.encoded(i)),
                       "disposition": "historical" if i["status"] in work.TERMINAL else "held"} for i in selected]}
        registry = json.loads(local.read("docs/REGISTRY.json"))
        if not any(row["path"] == owner for row in registry["documents"]):
            request.update(expected_registry_sha256=digest(local.read("docs/REGISTRY.json")),
                           document={"id": "fixture.import." + owner, "role": "fixture-import-" + owner, "path": owner})
        return request

    def batch(self, request=None, identity="import-fixture"):
        return {"profile": write_batch.PROFILE, "operation_id": identity,
                "operations": [{"kind": "import", "request": request or self.request()}]}

    def submit(self, batch):
        result = write_retry.submit_batch(self.host, self.root, "writer", batch)
        self.assertEqual(result["outcome"], "complete", result)
        return result

    def undo_request(self, result):
        coordinator, _ = connect(self.root, self.host, "writer")
        with coordinator._db() as db:
            row = db.execute("SELECT id,fingerprint FROM effects WHERE attempt=?", (result["result"]["attempt"],)).fetchone()
        return {"profile": board_import.PROFILE, "mode": "undo", "host": str(self.host), "actor": "writer",
                "request_id": result["request_id"], "effect_id": row["id"], "fingerprint": row["fingerprint"]}

    def test_managed_register_exact_retry_and_inverse_undo(self):
        before = {p: LocalRoot(self.root).read(p) for p in ("eutonos.boards.json", work.PATH, "docs/campaigns/imported.md", "docs/REGISTRY.json")}
        batch = self.batch(); result = self.submit(batch)
        self.assertEqual(self.submit(batch)["request_id"], result["request_id"])
        rows = {i["id"]: i for i in boards.project(self.root)["items"]}
        self.assertEqual(rows["HELD"]["status"], "deferred")
        self.assertEqual(rows["OLD1"]["extensions"]["source_import"]["source_item"], self.source_items[1])
        self.assertNotIn("work_history", rows["OLD1"]["extensions"])
        self.assertNotIn("OLD", (self.root / work.DONE).read_text())
        self.assertNotIn("HELD", (self.root / work.NEXT).read_text())
        self.assertIn("Imported history", (self.root / work.ORDER).read_text())
        self.assertIn("Held source work", (self.root / work.ORDER).read_text())
        self.assertNotIn("old-completion", (self.root / "docs/CHANGELOG.md").read_text())
        inverse = self.batch(self.undo_request(result), "undo-fixture")
        self.submit(inverse); self.submit(inverse)
        for path, raw in before.items(): self.assertEqual(LocalRoot(self.root).read(path), raw)

    def test_changed_retry_denied_before_recovery(self):
        batch = self.batch(); result = self.submit(batch)
        changed = deepcopy(batch); changed["operations"][0]["request"]["items"].pop()
        with self.assertRaises(ts.ReadError): write_retry.submit_batch(self.host, self.root, "writer", changed)
        with self.assertRaises(ts.ReadError): write_batch.recover_submitted(self.root, self.host, "writer", changed, result["request_id"])

    def test_undo_afterimage_and_forged_journal_refused(self):
        result = self.submit(self.batch()); undo = self.undo_request(result)
        forged = deepcopy(undo); forged["fingerprint"] = "0" * 64
        with self.assertRaises(ts.ReadError): write_batch.plan(self.root, self.batch(forged, "forged"))
        with (self.root / "docs/campaigns/imported.md").open("a") as stream: stream.write("Changed prose\n")
        with self.assertRaises(ts.ReadError): write_batch.plan(self.root, self.batch(undo, "undo-stale"))

    def test_source_identity_digest_and_missing_parent_refused(self):
        for field, value in (("commit", "main"), ("blob_oid", "0" * 40), ("sha256", "0" * 64)):
            request = self.request(); request["source"][field] = value
            with self.subTest(field=field), self.assertRaises(ts.ReadError): write_batch.plan(self.root, self.batch(request))
        request = self.request(items=[self.source_items[1]])
        with self.assertRaises(ts.ReadError): write_batch.plan(self.root, self.batch(request))
        request = self.request(); request["items"][0]["sha256"] = "0" * 64
        with self.assertRaises(ts.ReadError): write_batch.plan(self.root, self.batch(request))

    def test_unconfigured_done_collision_is_not_dropped(self):
        self.write_board("docs/campaigns/done/hidden.md", [self.item("HELD")]); self.commit()
        with self.assertRaises(ts.ReadError): write_batch.plan(self.root, self.batch())

    def test_registration_only_existing_records_denied(self):
        owner = "docs/campaigns/legacy.md"; self.write_board(owner, [self.item("HELD")]); self.commit()
        request = self.request(owner=owner); request["items"] = []; request.pop("source")
        with self.assertRaises(ts.ReadError): write_batch.plan(self.root, self.batch(request))

    def test_crc_unconfigured_whole_group_reconciliation_preserves_both_images(self):
        owner = self.source_path; self.write_board(owner, self.source_items); self.commit()
        request = self.request(owner=owner)
        for selected, local in zip(request["items"], self.source_items): selected["expected_item_revision"] = work.item_revision(local)
        self.submit(self.batch(request))
        row = next(i for i in boards.project(self.root)["items"] if i["id"] == "HELD")
        self.assertEqual(row["extensions"]["source_import"]["local_before"], self.source_items[2])
        self.assertTrue((self.root / owner).read_text().startswith("# Preserved owner prose"))

    def test_ett_fresh_standalone_retains_source_parent_and_existing_accepted_work(self):
        self.source_items[2]["parent_id"] = "OLD"
        self.write_board(self.source_path, self.source_items); self.commit()
        self.pin = self.git("rev-parse", "HEAD").strip()
        _, self.metadata = GitView(self.root, self.pin).read(self.source_path)
        (self.root / self.source_path).unlink()
        self.write_board("docs/WORK_ITEMS.md", [self.item("LIVE"), self.item("HELD", status="completed")]); self.commit()
        request = self.request(items=[self.source_items[2]])
        request["items"][0].update(disposition="fresh-held", fresh_id="FOLLOWUP",
                                     fresh_scope={"title": "Fresh follow-up", "summary": "Owner review still needed"})
        self.submit(self.batch(request))
        row = next(i for i in boards.project(self.root)["items"] if i["id"] == "FOLLOWUP")
        self.assertNotIn("parent_id", row)
        self.assertEqual(row["extensions"]["source_import"]["source_id"], "HELD")
        self.assertEqual(row["extensions"]["source_import"]["source_item"]["parent_id"], "OLD")
        self.assertEqual(next(i for i in boards.project(self.root)["items"] if i["id"] == "HELD")["status"], "completed")
        self.assertEqual(boards.project(self.root)["work_order"]["next"], ["LIVE"])

    def test_provenance_and_hold_cannot_be_spoofed_or_activated(self):
        self.submit(self.batch())
        row = next(i for i in boards.project(self.root)["items"] if i["id"] == "HELD")
        for operation in ("restore", "update", "reorder"):
            with self.subTest(operation=operation), self.assertRaises(ts.ReadError):
                work.plan(self.root, {"operation": operation, "item_id": "HELD", "expected_revision": row["revision"],
                                      "changes": {"status": "active"}, "lane": "current"})
        with self.assertRaises(ts.ReadError):
            boards.plan_update(self.root, {"profile": boards.UPDATE, "owner_path": row["owner_path"],
                "expected_sha256": digest(LocalRoot(self.root).read(row["owner_path"])), "item_id": "HELD",
                "changes": {"extensions": {}}})
        request = self.request(); request["owner_path"] = "docs/WORK_ITEMS.md"
        with self.assertRaises(ts.ReadError):
            boards.plan_add(self.root, {"profile": boards.ADD, "owner_path": "docs/WORK_ITEMS.md",
                "expected_sha256": digest(LocalRoot(self.root).read("docs/WORK_ITEMS.md")),
                "items": [self.item("SPOOF", extensions={"source_import": {}})]})

    def test_held_import_can_only_be_cancelled_and_keeps_provenance(self):
        self.submit(self.batch())
        row = next(i for i in boards.project(self.root)["items"] if i["id"] == "HELD")
        request = {"operation": "cancel", "item_id": "HELD", "expected_revision": row["revision"],
                   "reason": "Replaced by accepted native work.", "date": "2026-10-08"}
        prepared = work.plan(self.root, request)
        renew(self.root, self.host, "writer", prepared["paths"], shared=True)
        result = write_retry.submit_work(self.host, self.root, "writer", request)
        self.assertEqual(result["outcome"], "complete", result)
        cancelled = next(i for i in boards.project(self.root)["items"] if i["id"] == "HELD")
        self.assertEqual((cancelled["status"], cancelled["work_lane"]), ("cancelled", "done"))
        self.assertEqual(cancelled["extensions"]["source_import"], row["extensions"]["source_import"])
        self.assertEqual(cancelled["extensions"]["work_history"][-1]["why"], "Replaced by accepted native work.")
        projected = boards.project(self.root)
        self.assertEqual({item["id"] for item in projected["imported_history"]}, {"OLD", "OLD1"})
        self.assertNotIn("HELD", {item["id"] for item in projected.get("held_work", [])})
        with self.assertRaisesRegex(ts.ReadError, "cannot rewrite"):
            work.plan(self.root, {"operation": "cancel", "item_id": "HELD",
                "expected_revision": cancelled["revision"], "reason": "Still cancelled.",
                "changes": {"title": "Rewritten import"}})
        for operation in ("complete", "restore", "delete-stage"):
            with self.subTest(operation=operation), self.assertRaises(ts.ReadError):
                work.plan(self.root, {"operation": operation, "item_id": "HELD",
                    "expected_revision": cancelled["revision"], "why": "Not allowed."})

    def test_guided_import_cancellation_plans_without_progress_and_preserves_provenance(self):
        self.submit(self.batch())
        before = next(i for i in boards.project(self.root)["items"] if i["id"] == "HELD")
        context = {"root": str(self.root), "host": str(self.host), "actor": "writer", "work_id": "HELD", "session_id": "fixture"}
        row = work_closeout.lifecycle_assessment({}, context)["example"]["work_closeout"][0]
        self.assertNotIn("progress", row)
        row.update(status="cancelled", cancellation_reason="Replaced by accepted native work.")
        values = {"catalog_changes": [], "work_changes": [], "work_closeout": [row]}
        invalid = {**row, "progress": {"percent": 0, "basis": "Imported hold."}}
        self.assertEqual(closeout_route.prepare({**values, "work_closeout": [invalid]}, context)["outcome"], "incomplete")
        prepared = closeout_route.prepare(values, context)
        self.assertEqual(prepared["outcome"], "prepared", prepared)
        request = prepared["lifecycle_batch"]["request"]["operations"][0]["request"]
        self.assertEqual(request["operation"], "cancel")
        self.assertNotIn("progress", request)
        renew(self.root, self.host, "writer", prepared["allowed_paths"], shared=True)
        with mock.patch("eutonos.closeout_route.prepare_memory", return_value={"profile": "eutonos.tokenslang.workflow.v1", "outcome": "skipped", "reason": "fixture-only"}):
            result = closeout_route.run(prepared, context)
        self.assertEqual(result["outcome"], "completed", result)
        after = next(i for i in boards.project(self.root)["items"] if i["id"] == "HELD")
        self.assertEqual(after["status"], "cancelled")
        self.assertNotIn("progress", after["extensions"])
        self.assertEqual(after["extensions"]["source_import"], before["extensions"]["source_import"])

    def test_guided_import_campaign_cancellation_cascades_only_open_stages(self):
        self.source_items[0]["status"] = "active"
        self.source_items.append(self.item("OLD2", "stage", "active", parent_id="OLD"))
        self.write_board(self.source_path, self.source_items); self.commit()
        self.pin = self.git("rev-parse", "HEAD").strip()
        _, self.metadata = GitView(self.root, self.pin).read(self.source_path)
        (self.root / self.source_path).unlink(); self.commit()
        self.submit(self.batch())
        before = {item["id"]: item for item in boards.project(self.root)["items"]}
        context = {"root": str(self.root), "host": str(self.host), "actor": "writer", "work_id": "OLD", "session_id": "fixture"}
        row = work_closeout.lifecycle_assessment({}, context)["example"]["work_closeout"][0]
        row.update(cancellation_reason="Original campaign replaced by native work.")
        values = {"catalog_changes": [], "work_changes": [], "work_closeout": [row]}
        prepared = closeout_route.prepare(values, context)
        self.assertEqual(prepared["outcome"], "prepared", prepared)
        renew(self.root, self.host, "writer", prepared["allowed_paths"], shared=True)
        with mock.patch("eutonos.closeout_route.prepare_memory", return_value={"profile": "eutonos.tokenslang.workflow.v1", "outcome": "skipped", "reason": "fixture-only"}):
            result = closeout_route.run(prepared, context)
        self.assertEqual(result["outcome"], "completed", result)
        after = {item["id"]: item for item in boards.project(self.root)["items"]}
        self.assertEqual([after[key]["status"] for key in ("OLD", "OLD1", "OLD2")],
                         ["cancelled", "completed", "cancelled"])
        for key in ("OLD", "OLD1", "OLD2"):
            self.assertEqual(after[key]["extensions"]["source_import"], before[key]["extensions"]["source_import"])
        self.assertEqual(after["OLD1"]["extensions"], before["OLD1"]["extensions"])
        self.assertEqual(after["OLD2"]["extensions"]["work_history"][-1]["why"], row["cancellation_reason"])

    def test_changed_census_after_plan_blocks_native_apply(self):
        prepared = write_batch.plan(self.root, self.batch())
        self.write_board("docs/campaigns/new.md", [self.item("OTHER")]); self.commit()
        with self.assertRaises(ts.ReadError):
            write_batch.apply_prepared(self.root, self.host, "writer", prepared, "census-change")
        self.assertFalse((self.root / "docs/campaigns/imported.md").exists())

    def test_fixture_marker_and_mixed_batch_denied(self):
        mixed = self.batch(); mixed["operations"].append({"kind": "work", "request": {"operation": "refresh"}})
        with self.assertRaises(ts.ReadError): write_batch.plan(self.root, mixed)
        (self.root / MARKER).unlink()
        with self.assertRaises(ts.ReadError): write_batch.plan(self.root, self.batch())

    def test_exact_recovery_uses_frozen_journal_after_source_changes(self):
        batch = self.batch(); result = self.submit(batch)
        self.put("docs/unrelated.md", "Later census change\n"); self.commit()
        recovered = write_batch.recover_submitted(self.root, self.host, "writer", batch, result["request_id"])
        self.assertTrue(recovered["complete"], recovered)

    def test_emob_configured_type_reconcile_keeps_local_and_source_types(self):
        owner = "docs/campaigns/emob.md"
        local_items = deepcopy(self.source_items); local_items[1]["type"] = "slice"
        self.write_board(owner, local_items)
        self.put("eutonos.boards.json", json.dumps({"profile": boards.CONFIG, "owners": ["docs/WORK_ITEMS.md", owner]}))
        self.commit()
        request = self.request(mode="reconcile", owner=owner)
        for selection, item in zip(request["items"], local_items): selection["expected_item_revision"] = work.item_revision(item)
        self.submit(self.batch(request))
        row = next(i for i in boards.project(self.root)["items"] if i["id"] == "OLD1")
        provenance = row["extensions"]["source_import"]
        self.assertEqual((row["type"], provenance["local_before"]["type"], provenance["source_item"]["type"]),
                         ("stage", "slice", "stage"))

    def test_mixed_live_parent_links_and_move_are_denied(self):
        self.submit(self.batch())
        entries = boards._load(LocalRoot(self.root), strict=True)[3]
        entries["docs/WORK_ITEMS.md"]["items"][0]["parent_id"] = "OLD"
        with self.assertRaises(ts.ReadError): boards._validate_graph(entries)
        entries["docs/WORK_ITEMS.md"]["items"][0].pop("parent_id")
        imported = entries["docs/campaigns/imported.md"]["items"][2]
        imported["parent_id"] = "LIVE"
        with self.assertRaises(ts.ReadError): boards._validate_graph(entries)

    def test_oversized_blob_refused_before_source_read(self):
        self.put("docs/campaigns/large.md", "x" * (1024 * 1024 + 1)); self.commit()
        pin = self.git("rev-parse", "HEAD").strip()
        oid = self.git("rev-parse", pin + ":docs/campaigns/large.md").strip()
        (self.root / "docs/campaigns/large.md").unlink(); self.commit()
        request = self.request(); request["source"].update(commit=pin, path="docs/campaigns/large.md", blob_oid=oid)
        with mock.patch.object(GitView, "read", side_effect=AssertionError("Source read happened before size refusal")):
            with self.assertRaises(ts.ReadError): write_batch.plan(self.root, self.batch(request))

    def test_completed_effect_interrupted_before_closeout_recovers_then_undo(self):
        before_registry = (self.root / "docs/REGISTRY.json").read_bytes()
        batch = self.batch()
        from eutonos.effects import Coordinator
        with mock.patch.object(Coordinator, "closeout", side_effect=OSError("Fixture closeout interruption")):
            result = write_retry.submit_batch(self.host, self.root, "writer", batch)
        self.assertEqual(result["outcome"], "error", result)
        # The managed journal landed; exact recovery verifies after-images and retires reservations.
        recovered = write_batch.recover_submitted(self.root, self.host, "writer", batch, result["request_id"])
        self.assertTrue(recovered["complete"], recovered)
        # Ordinary native retry advances its receipt to complete without replanning the stale census.
        completed = self.submit(batch)
        self.submit(self.batch(self.undo_request(completed), "recovered-undo"))
        self.assertFalse((self.root / "docs/campaigns/imported.md").exists())
        self.assertEqual((self.root / "docs/REGISTRY.json").read_bytes(), before_registry)

    def test_new_owner_docs_checker_closure_and_retry_binding(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("fixture_docs_checker", Path(__file__).parents[1] / "tools/check_docs.py")
        checker = importlib.util.module_from_spec(spec); spec.loader.exec_module(checker)
        batch = self.batch(); self.submit(batch)
        self.assertEqual(checker.check(self.root)["errors"], [])
        registry = json.loads((self.root / "docs/REGISTRY.json").read_bytes())
        self.assertEqual(registry["retained"], {"note": "preserve unrelated metadata"})
        self.assertIn(batch["operations"][0]["request"]["document"], registry["documents"])
        changed = deepcopy(batch); changed["operations"][0]["request"]["document"]["role"] = "changed-role"
        with self.assertRaises(ts.ReadError): self.submit(changed)

    def test_registry_metadata_conflicts_and_preconditions_refused(self):
        for change in ["missing-hash", "wrong-hash", "missing-document", "id", "role", "path", "blank"]:
            request = self.request()
            if change == "missing-hash": request.pop("expected_registry_sha256")
            elif change == "wrong-hash": request["expected_registry_sha256"] = "0" * 64
            elif change == "missing-document": request.pop("document")
            elif change == "blank": request["document"]["id"] = " "
            else: request["document"][change] = json.loads((self.root / "docs/REGISTRY.json").read_bytes())["documents"][0][change]
            with self.subTest(change=change), self.assertRaises(ts.ReadError): write_batch.plan(self.root, self.batch(request))

    def test_existing_registered_owner_legacy_request_preserves_row_and_prose(self):
        owner = "docs/campaigns/existing.md"; self.write_board(owner, [])
        registry = json.loads((self.root / "docs/REGISTRY.json").read_bytes())
        row = {"id": "fixture.existing", "role": "existing-owner", "path": owner, "retained": "row metadata"}
        registry["documents"].append(row); self.put("docs/REGISTRY.json", json.dumps(registry) + "\n"); self.commit()
        request = self.request(owner=owner)
        self.assertNotIn("document", request)
        null_document = {**deepcopy(request), "document": None}
        with self.assertRaises(ts.ReadError): write_batch.plan(self.root, self.batch(null_document))
        conflict = deepcopy(request)
        conflict.update(document={"id": "replacement", "role": row["role"], "path": owner},
                        expected_registry_sha256=digest((self.root / "docs/REGISTRY.json").read_bytes()))
        with self.assertRaises(ts.ReadError): write_batch.plan(self.root, self.batch(conflict))
        self.submit(self.batch(request))
        self.assertIn(row, json.loads((self.root / "docs/REGISTRY.json").read_bytes())["documents"])
        self.assertTrue((self.root / owner).read_text().startswith("# Preserved owner prose\n"))

    def test_invalid_duplicate_and_oversized_registry_refused(self):
        original = (self.root / "docs/REGISTRY.json").read_bytes()
        for key in ["id", "role", "path"]:
            registry = json.loads(original)
            registry["documents"][1][key] = registry["documents"][0][key]
            self.put("docs/REGISTRY.json", json.dumps(registry)); self.commit()
            with self.subTest(key=key), self.assertRaises(ts.ReadError): write_batch.plan(self.root, self.batch())
        self.put("docs/REGISTRY.json", " " * (board_import.MAX_REGISTRY_BYTES + 1)); self.commit()
        with self.assertRaises(ts.ReadError): board_import.form(self.root)
        self.put("docs/REGISTRY.json", json.dumps({"schema": "wrong", "documents": []})); self.commit()
        with self.assertRaises(ts.ReadError): write_batch.plan(self.root, self.batch())

    def test_two_new_owners_overlay_registry_and_write_budget(self):
        first = self.request(items=self.source_items[:2])
        second = self.request(owner="docs/campaigns/second.md", items=self.source_items[2:])
        batch = self.batch(first); batch["operations"].append({"kind": "import", "request": second})
        planned = write_batch.plan(self.root, batch)
        self.assertIn("docs/REGISTRY.json", [c["path"] for c in planned["effect"]["changes"]])
        self.assertEqual(planned["effect"]["board_census"]["destinations"],
                         sorted([first["owner_path"], second["owner_path"], "docs/REGISTRY.json"]))
        self.assertEqual(planned["effect"]["board_census"]["hashes"]["docs/REGISTRY.json"],
                         first["expected_registry_sha256"])
        self.submit(batch)
        rows = json.loads((self.root / "docs/REGISTRY.json").read_bytes())["documents"]
        self.assertIn(first["document"], rows); self.assertIn(second["document"], rows)
        from eutonos import effects
        updates = {"docs/REGISTRY.json": (self.root / "docs/REGISTRY.json").read_bytes()}
        updates.update({f"docs/campaigns/budget-{i}.md": b"# Fixture\n" for i in range(16)})
        with self.assertRaises(ts.ReadError): effects.plan(LocalRoot(self.root), updates)

    def test_registry_changed_between_prepare_and_effect_refuses_apply(self):
        original_effect_plan = write_batch.effect_plan
        def concurrent_registry_change(local, updates):
            registry = json.loads((self.root / "docs/REGISTRY.json").read_bytes())
            registry["retained"]["concurrent"] = "must not be overwritten"
            self.put("docs/REGISTRY.json", json.dumps(registry) + "\n")
            return original_effect_plan(local, updates)
        with mock.patch.object(write_batch, "effect_plan", side_effect=concurrent_registry_change):
            planned = write_batch.plan(self.root, self.batch())
        current_registry = (self.root / "docs/REGISTRY.json").read_bytes()
        with self.assertRaises(ts.ReadError):
            write_batch.apply_prepared(self.root, self.host, "writer", planned, "registry-race")
        self.assertEqual((self.root / "docs/REGISTRY.json").read_bytes(), current_registry)
        self.assertFalse((self.root / "docs/campaigns/imported.md").exists())

    def test_opaque_issued_identity_and_stage_association_boundaries(self):
        self.source_items[2]["id"] = "PH.opaque-17"
        self.write_board(self.source_path, self.source_items); self.commit()
        self.pin = self.git("rev-parse", "HEAD").strip()
        _, self.metadata = GitView(self.root, self.pin).read(self.source_path)
        (self.root / self.source_path).unlink(); self.commit()
        self.submit(self.batch())
        row = next(i for i in boards.project(self.root)["items"] if i["id"] == "PH.opaque-17")
        self.assertEqual(row["extensions"]["source_import"]["source_id"], "PH.opaque-17")
        entries = boards._load(LocalRoot(self.root), strict=True)[3]
        entries["docs/WORK_ITEMS.md"]["items"][0]["extensions"] = {"stage_id": "OLD1"}
        with self.assertRaises(ts.ReadError): boards._validate_graph(entries)

    def test_partial_native_journal_cannot_be_undone_as_complete(self):
        real = LocalRoot.replace
        calls = []
        def interrupted(local, path, expected, body):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("Fixture interrupted during managed writes")
            return real(local, path, expected, body)
        with mock.patch.object(LocalRoot, "replace", interrupted):
            result = write_retry.submit_batch(self.host, self.root, "writer", self.batch())
        self.assertEqual(result["outcome"], "error", result)
        coordinator, _ = connect(self.root, self.host, "writer")
        attempt = "write-batch-" + result["request_id"].removeprefix("write-batch-")
        with coordinator._db() as db:
            row = db.execute("SELECT id,fingerprint,state FROM effects WHERE attempt=?", (attempt,)).fetchone()
        self.assertEqual(row["state"], "prepared")
        undo = {"profile": board_import.PROFILE, "mode": "undo", "host": str(self.host), "actor": "writer",
                "request_id": result["request_id"], "effect_id": row["id"], "fingerprint": row["fingerprint"]}
        with self.assertRaises(ts.ReadError): write_batch.plan(self.root, self.batch(undo, "incomplete-undo"))
        with self.assertRaises(ts.ReadError):
            write_batch.recover_submitted(self.root, self.host, "writer", self.batch(), result["request_id"])
        self.assertTrue(coordinator.inspect(attempt)["reservations"])

    def test_forged_typed_journal_cannot_expand_inverse_to_unrelated_path(self):
        result = self.submit(self.batch()); undo = self.undo_request(result)
        coordinator, _ = connect(self.root, self.host, "writer")
        with coordinator._db() as db:
            row = db.execute("SELECT plan FROM effects WHERE id=?", (undo["effect_id"],)).fetchone()
            effect = ts.loads(row["plan"])
            unrelated = deepcopy(effect["changes"][0]); unrelated["path"] = "docs/unrelated-private.md"
            effect["changes"].append(unrelated)
            undo["fingerprint"] = digest(ts.encoded(effect))
            db.execute("UPDATE effects SET plan=?,fingerprint=? WHERE id=?",
                       (ts.encoded(effect).decode(), undo["fingerprint"], undo["effect_id"]))
        with self.assertRaises(ts.ReadError) as caught:
            write_batch.plan(self.root, self.batch(undo, "forged-scope-undo"))
        self.assertEqual(caught.exception.code, "denied")
        self.assertFalse((self.root / "docs/unrelated-private.md").exists())

    def test_append_preserves_existing_import_and_no_order_cannot_remove_provenance(self):
        self.submit(self.batch(self.request(items=self.source_items[:2])))
        self.submit(self.batch(self.request(mode="append", items=[self.source_items[2]]), "append-fixture"))
        rows = boards.project(self.root)["items"]
        self.assertEqual(sum("source_import" in i.get("extensions", {}) for i in rows), 3)
        row = next(i for i in rows if i["id"] == "HELD")
        (self.root / work.PATH).unlink()
        with self.assertRaises(ts.ReadError):
            boards.plan_update(self.root, {"profile": boards.UPDATE, "owner_path": row["owner_path"],
                "expected_sha256": digest(LocalRoot(self.root).read(row["owner_path"])), "item_id": "HELD",
                "changes": {"type": "feature", "extensions": {}}})

    def test_hash_consistent_journal_from_replaced_root_identity_refused(self):
        result = self.submit(self.batch()); undo = self.undo_request(result)
        coordinator, _ = connect(self.root, self.host, "writer")
        with coordinator._db() as db:
            row = db.execute("SELECT plan FROM effects WHERE id=?", (undo["effect_id"],)).fetchone()
            effect = ts.loads(row["plan"])
            effect["root_identity"][1] += 1
            effect["board_census"]["root_identity"] = list(effect["root_identity"])
            undo["fingerprint"] = digest(ts.encoded(effect))
            db.execute("UPDATE effects SET plan=?,fingerprint=? WHERE id=?",
                       (ts.encoded(effect).decode(), undo["fingerprint"], undo["effect_id"]))
        with self.assertRaises(ts.ReadError) as caught:
            write_batch.plan(self.root, self.batch(undo, "foreign-root-undo"))
        self.assertEqual(caught.exception.code, "wrong-scope")
