"""Public native-context qualification in disposable Git roots/external hosts."""
from copy import deepcopy
from dataclasses import replace
import base64
import io
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

from tests import retention_support as retention
from tests import test_board_import as fixture
from tests import test_board_import_interrupted as interrupted
from tests import test_import_recovery_usability as usability
from eutonos import board_census, board_import, boards, boards_cli, effects, host, managed_write, write_batch, write_retry, tokenslang as ts
from eutonos.effects import LocalRoot, MARKER, MARKER_BYTES, decode, digest
from eutonos.host import connect
from eutonos.source import GitView


NATIVE = "eutonos.tokenslang.board-census.native.v1"


class NativeContextTests(unittest.TestCase):
    # Module-qualified helper composition, never inherited/recollected old tests.
    git = fixture.BoardImportTests.git
    commit = fixture.BoardImportTests.commit
    put = fixture.BoardImportTests.put
    write_board = fixture.BoardImportTests.write_board
    item = fixture.BoardImportTests.item
    request = fixture.BoardImportTests.request
    batch = fixture.BoardImportTests.batch
    submit = fixture.BoardImportTests.submit
    undo_request = fixture.BoardImportTests.undo_request
    interrupt = interrupted.InterruptedImportTests.interrupt
    journal = interrupted.InterruptedImportTests.journal
    snapshot = interrupted.InterruptedImportTests.snapshot
    no_replan = interrupted.InterruptedImportTests.no_replan
    cli = usability.ImportRecoveryUsabilityTests.cli
    binding = usability.ImportRecoveryUsabilityTests.binding
    assert_images = usability.ImportRecoveryUsabilityTests.assert_images

    def setUp(self):
        fixture.BoardImportTests.setUp(self)
        (self.root / MARKER).unlink()
        self.commit()
        self.coordinator, self.authority = connect(self.root, self.host, "writer")
        self.assertFalse((self.root / MARKER).exists())

    def store(self):
        with self.coordinator._db() as db:
            return {table: [tuple(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY rowid")]
                    for table in ("attempts", "effects", "reservations")}

    def board_cli(self, args, *, root=None, request=None):
        argv = ["--root", str(self.root if root is None else root), *args]
        if request is not None:
            path = Path(self.temp.name) / "public-board-request.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            argv.extend(["--request", str(path)])
        output = io.BytesIO()
        with mock.patch.object(sys, "stdout", io.TextIOWrapper(output, encoding="utf-8")) as stream:
            code = boards_cli.main(argv)
            stream.flush()
            body = output.getvalue()
            stream.detach()
        return code, json.loads(body)

    def plan(self, batch=None, *, root=None):
        root = self.root if root is None else root
        _, authority = connect(root, self.host, "writer")
        return write_batch.plan(root, self.batch() if batch is None else batch, authority=authority)

    def public_plan(self, batch, root=None):
        code, value = self.board_cli(["batch", "plan", "--host", str(self.host), "--actor", "writer"],
                                    root=root, request=batch)
        self.assertEqual(code, 0, value)
        return value

    def assert_native(self, effect):
        census = effect["board_census"]
        self.assertEqual(census["profile"], NATIVE)
        self.assertEqual(set(census), board_census.KEYS)
        self.assertIsNone(census["hashes"][MARKER])
        self.assertFalse((self.root / MARKER).exists())

    def test_public_nonmarker_form_plan_submit_inverse_and_repeat(self):
        code, form = self.board_cli(["import", "form", "--host", str(self.host), "--actor", "writer"])
        self.assertEqual(code, 0, form)
        self.assertEqual(form["request"]["profile"], board_import.PROFILE)
        batch = self.batch()
        prepared = self.public_plan(batch)
        self.assert_native(prepared["effect"])
        baseline = {p: LocalRoot(self.root).read(p) for p in prepared["paths"]}
        code, completed = self.board_cli(["batch", "submit", "--host", str(self.host), "--actor", "writer"], request=batch)
        self.assertEqual((code, completed["outcome"]), (0, "complete"), completed)
        row = self.journal(self.coordinator, completed["request_id"])
        self.assertEqual(row["state"], "completed")
        self.assert_native(ts.loads(row["plan"]))
        self.assert_images(row, "after")
        self.assertEqual(self.binding(row["attempt"]),
                         (str(self.root), "writer", digest(ts.encoded(write_batch.normalize(batch)))))
        projected = {i["id"]: i for i in boards.project(self.root)["items"]}
        self.assertEqual(projected["HELD"]["status"], "deferred")
        for source in self.source_items:
            self.assertEqual(projected[source["id"]]["extensions"]["source_import"]["source_item"], source)
        inverse = self.batch(self.undo_request(completed), "native-public-inverse")
        self.assert_native(self.public_plan(inverse)["effect"])
        undone = self.submit(inverse)
        for path, raw in baseline.items():
            self.assertEqual(LocalRoot(self.root).read(path), raw)
        parent = self.journal(self.coordinator, row["attempt"])
        self.assertEqual(parent, row)
        with self.no_replan(), mock.patch.object(LocalRoot, "replace", side_effect=AssertionError("Inverse replay wrote")):
            self.assertEqual(self.submit(inverse)["request_id"], undone["request_id"])

    def test_fixture_profile_bytes_remain_exact_with_or_without_context(self):
        (self.root / MARKER).write_bytes(MARKER_BYTES)
        self.commit()
        local = LocalRoot(self.root)
        batch = self.batch()
        legacy = write_batch.plan(self.root, batch)
        contextual = self.plan(batch)
        self.assertEqual(ts.encoded(contextual), ts.encoded(legacy))
        self.assertEqual(legacy["effect"]["board_census"]["profile"], board_census.PROFILE)
        self.assertEqual(ts.encoded(board_census.freeze(local)),
                         ts.encoded(board_census.freeze(local, authority=self.authority)))
        self.assertEqual(board_import.form(self.root), board_import.form(self.root, authority=self.authority))

    def test_retained_v2_golden_bytes_and_request_identity_remain_exact(self):
        golden = json.loads((Path(__file__).parent / "fixtures/board_import_v2_golden.json").read_text(encoding="utf-8"))
        self.assertEqual(write_batch.normalize(golden["raw_batch"]), golden["normalized_batch"])
        self.assertEqual(ts.encoded(golden["normalized_batch"]), golden["canonical_batch_utf8"].encode())
        with self.no_replan():
            self.assertEqual(board_import.validate_metadata(golden["journal_metadata"]), golden["normalized_batch"])
        for actor in (golden["binding"]["actor"], "other"):
            self.assertEqual(write_retry._request_id(golden["binding"]["root"], actor, "batch",
                             {"batch": golden["normalized_batch"]}), golden["binding"]["id"])

    def test_ordinary_batch_plan_uses_original_kwargs_and_ignores_context(self):
        request = {"profile": write_batch.PROFILE, "operation_id": "ordinary-context-compatible",
                   "operations": [{"kind": "update", "request": {"owner_path": "docs/WORK_ITEMS.md",
                       "item_id": "LIVE", "changes": {"summary": "Ordinary plan"},
                       "expected_sha256": digest(LocalRoot(self.root).read("docs/WORK_ITEMS.md"))}}]}
        original = boards.plan_update
        calls = []
        def old_signature(local, value):
            calls.append(value)
            return original(local, value)
        with mock.patch.object(boards, "plan_update", old_signature), \
                mock.patch.object(board_census, "freeze", side_effect=AssertionError("Ordinary plan used census")):
            plain = write_batch.plan(self.root, request)
            contextual = write_batch.plan(self.root, request, authority=self.authority)
        self.assertEqual(contextual, plain)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("board_import", plain["effect"])
        self.assertNotIn("board_census", plain["effect"])
        code, ordinary_cli = self.board_cli(["batch", "plan", "--host", str(self.host)], request=request)
        self.assertEqual(code, 0, ordinary_cli)
        self.assertEqual(ordinary_cli, plain)

    def test_missing_context_and_unpaired_public_flags_refuse_without_effects(self):
        before = self.store()
        for call in (lambda: board_import.form(self.root),
                     lambda: board_census.freeze(LocalRoot(self.root)),
                     lambda: write_batch.plan(self.root, self.batch())):
            with self.subTest(call=call), \
                    mock.patch.object(board_census, "GitView", side_effect=AssertionError("Missing context accessed Git")), \
                    mock.patch.object(GitView, "read", side_effect=AssertionError("Missing context read source")), \
                    self.assertRaises(ts.ReadError) as error:
                call()
            self.assertEqual(error.exception.code, "denied")
        for flags in ([], ["--host", str(self.host)], ["--actor", "writer"]):
            for args in (["import", "form", *flags], ["batch", "plan", *flags]):
                with self.subTest(args=args):
                    code, result = self.board_cli(args, request=self.batch() if args[0] == "batch" else None)
                    self.assertEqual(code, 2, result)
        self.assertEqual(self.store(), before)
        self.assertFalse((self.root / "docs/campaigns/imported.md").exists())
        self.assertFalse((self.root / MARKER).exists())

    def test_native_census_destination_and_unsafe_path_bounds_remain_fail_closed(self):
        before = self.store()
        for destinations, code in ((["docs/absent-" + str(i) + ".md" for i in range(17)], "overflow"),
                                   (["../outside.md"], "unsafe-path")):
            with self.subTest(destinations=destinations), self.assertRaises(ts.ReadError) as error:
                board_census.freeze(LocalRoot(self.root), destinations=destinations, authority=self.authority)
            self.assertEqual(error.exception.code, code)
        guard = board_census.freeze(LocalRoot(self.root), destinations=["docs/absent-" + str(i) + ".md" for i in range(16)],
                                    authority=self.authority)
        self.assertEqual(len(guard["destinations"]), 16)
        self.assertTrue(all(guard["hashes"][path] is None for path in guard["destinations"]))
        self.assertEqual(self.store(), before)

    def test_foreign_malformed_and_stale_admission_context_refuse_before_inventory(self):
        before = self.store()
        foreign = Path(self.temp.name) / "foreign"
        foreign.mkdir()
        contexts = ({"actor": "writer"}, object(),
                    replace(self.authority, root=LocalRoot(foreign)),
                    replace(self.authority, actor="other"))
        for authority in contexts:
            with self.subTest(authority=type(authority).__name__), \
                    mock.patch.object(GitView, "read", side_effect=AssertionError("Invalid context read source")), \
                    self.assertRaises(ts.ReadError) as error:
                write_batch.plan(self.root, self.batch(), authority=authority)
            self.assertEqual(error.exception.code, "wrong-scope")
        admission = self.host / "admission.json"
        old = admission.read_bytes()
        admission.write_bytes(old + b" ")
        try:
            with self.assertRaises(ts.ReadError) as error:
                self.plan_with_authority(self.authority)
            self.assertEqual(error.exception.code, "denied")
        finally:
            admission.write_bytes(old)
        self.assertEqual(self.store(), before)

    def plan_with_authority(self, authority):
        return write_batch.plan(self.root, self.batch(), authority=authority)

    def test_unknown_marker_and_serialized_native_guard_never_authorize(self):
        guard = board_census.freeze(LocalRoot(self.root), authority=self.authority)
        before = self.store()
        with self.assertRaises(ts.ReadError):
            board_census.validate(LocalRoot(self.root), guard)
        for profile in ("unknown-native-profile", []):
            malformed = deepcopy(guard)
            malformed["profile"] = profile
            with self.subTest(profile=profile), self.assertRaises(ts.ReadError):
                board_census.validate(LocalRoot(self.root), malformed, authority=self.authority)
        malformed_effect = deepcopy(self.plan()["effect"])
        malformed_effect["board_census"] = []
        with self.assertRaises(ts.ReadError) as error:
            effects.validate_plan(LocalRoot(self.root), malformed_effect, authority=self.authority)
        self.assertEqual(error.exception.code, "invalid")
        (self.root / MARKER).write_bytes(b"Unrecognized marker bytes\n")
        with self.assertRaises(ts.ReadError) as error:
            self.plan()
        self.assertEqual(error.exception.code, "denied")
        self.assertEqual(self.store(), before)
        self.assertEqual((self.root / MARKER).read_bytes(), b"Unrecognized marker bytes\n")

    def test_valid_binding_altered_effect_and_generic_tags_refuse_without_writes(self):
        batch = self.batch()
        prepared = self.plan(batch)
        queued = write_retry.enqueue(self.host, self.root, "writer", "batch", {"batch": write_batch.normalize(batch)})
        altered = deepcopy(prepared)
        change = next(c for c in altered["effect"]["changes"] if c["path"] == "docs/campaigns/imported.md")
        raw = decode(change["after"])
        board, matches = boards._parse(raw, change["path"])
        board["items"][0]["title"] = "Unrequested source title"
        change["after"] = base64.b64encode(boards._replace(raw, matches[0], board)).decode()
        altered["fingerprint"] = digest(ts.encoded(altered["effect"]))
        effects.validate_plan(LocalRoot(self.root), altered["effect"], authority=self.authority)
        board_import.native_binding(LocalRoot(self.root), self.host, "writer", queued["request_id"], write_batch.normalize(batch))
        targets = {p: LocalRoot(self.root).read(p) for p in prepared["paths"]}
        before = self.store()
        with mock.patch.object(LocalRoot, "replace", side_effect=AssertionError("Rejected source derivation wrote")):
            with self.assertRaises(ts.ReadError) as error:
                write_batch.apply_prepared(self.root, self.host, "writer", altered, queued["request_id"])
        self.assertIn("source-derived", error.exception.message)
        self.assertEqual(self.store(), before)
        attempt = "generic-native-tags"
        generation = self.coordinator.start(self.authority, attempt)
        self.coordinator.reserve(self.authority, attempt, generation, prepared["paths"])
        guarded = self.store()
        for tagged in (True, False):
            value = deepcopy(prepared["effect"])
            if not tagged:
                value.pop("board_import")
            with self.subTest(tagged=tagged), self.assertRaises(ts.ReadError) as refused:
                host.selected_apply(self.coordinator, self.authority, attempt, generation, "generic",
                                    value, digest(ts.encoded(value)))
            self.assertEqual(refused.exception.code, "denied" if tagged else "wrong-scope")
            self.assertEqual(self.store(), guarded)
        for path, body in targets.items():
            self.assertEqual(LocalRoot(self.root).read(path), body)

    def assert_core_census_race(self, phase):
        real_apply = effects.Coordinator.apply
        real_validate = board_census.validate
        calls = []
        foreign = b"Unrelated physical census race\n"
        def validation(local, guard, **kwargs):
            calls.append(1)
            if len(calls) == phase:
                (self.root / "README.md").write_bytes(foreign)
            return real_validate(local, guard, **kwargs)
        def core(owner, *args, **kwargs):
            with mock.patch.object(board_census, "validate", side_effect=validation):
                return real_apply(owner, *args, **kwargs)
        batch = self.batch()
        planned = self.plan(batch)
        targets = {p: LocalRoot(self.root).read(p) for p in planned["paths"]}
        with mock.patch.object(effects.Coordinator, "apply", core), \
                mock.patch.object(LocalRoot, "replace", side_effect=AssertionError("Census race wrote targets")):
            result = write_retry.submit_batch(self.host, self.root, "writer", batch)
        self.assertEqual(result["outcome"], "error", result)
        self.assertEqual(len(calls), phase)
        self.assertEqual((self.root / "README.md").read_bytes(), foreign)
        for path, raw in targets.items():
            self.assertEqual(LocalRoot(self.root).read(path), raw)
        with self.coordinator._db() as db:
            rows = list(db.execute("SELECT state FROM effects WHERE attempt=?", (result["request_id"],)))
        self.assertEqual([r[0] for r in rows], [] if phase == 1 else ["prepared"])
        self.assertTrue(self.coordinator.inspect(result["request_id"])["reservations"])

    def test_native_first_core_census_boundary_refuses_before_journal(self):
        self.assert_core_census_race(1)

    def test_native_second_core_census_boundary_keeps_prepared_fence(self):
        self.assert_core_census_race(2)

    def frozen_abort_retry(self, batch, *, marker_drift=False):
        result, coordinator, _, _, row = self.interrupt(batch)
        self.assert_native(ts.loads(row["plan"]))
        if marker_drift:
            (self.root / MARKER).write_bytes(b"Unrelated marker drift\n")
        real_read = LocalRoot.read
        def frozen_read(local, name):
            if name == MARKER:
                raise AssertionError("Frozen native abort reread live marker")
            return real_read(local, name)
        with self.no_replan(), mock.patch.object(LocalRoot, "read", frozen_read):
            code, aborted = self.cli("rollback", row)
            self.assertEqual((code, aborted["outcome"]), (0, "abort-awaiting-reconcile"))
            # Re-open through real CLI connection; reconcile alone retains fencing.
            code, reconciled = self.cli("reconcile", row)
            self.assertEqual((code, reconciled["outcome"]), (0, "not-applied"))
            self.assertTrue(coordinator.inspect(row["attempt"])["reservations"])
            with mock.patch.object(LocalRoot, "replace", side_effect=AssertionError("Cleanup wrote targets")):
                recovered = managed_write.recover(self.root, self.host, "writer", row["attempt"], {})
        self.assertEqual((recovered["outcome"], recovered["complete"]), ("retry", False))
        current = self.journal(coordinator, row["attempt"])
        self.assertEqual(current, {**row, "state": "not-applied"})
        self.assert_images(current, "backup")
        self.assertEqual(coordinator.inspect(row["attempt"])["reservations"], [])
        if marker_drift:
            self.assertEqual((self.root / MARKER).read_bytes(), b"Unrelated marker drift\n")
            # A new plan evaluates current marker eligibility; frozen cleanup did not.
            with self.assertRaises(ts.ReadError):
                self.plan(batch)
            (self.root / MARKER).unlink()
        completed = self.submit(batch)
        self.assertEqual(completed["request_id"], result["request_id"])
        self.assert_images(row, "after")
        self.assertEqual(self.journal(coordinator, row["attempt"]), {**row, "state": "completed"})
        return completed

    def test_native_partial_original_public_abort_reopen_recover_exact_retry(self):
        self.frozen_abort_retry(self.batch())

    def test_native_interrupted_unprefixed_inverse_preserves_completed_parent(self):
        original = self.submit(self.batch())
        parent = self.journal(self.coordinator, original["request_id"])
        binding = self.binding(original["request_id"])
        undo = self.undo_request(original)
        undo["request_id"] = undo["request_id"].removeprefix("write-batch-")
        self.frozen_abort_retry(self.batch(undo, "native-interrupted-inverse"))
        self.assertEqual(self.journal(self.coordinator, original["request_id"]), parent)
        self.assertEqual(self.binding(original["request_id"]), binding)

    def test_native_marker_drift_cannot_block_frozen_abort_baseline_cleanup(self):
        self.frozen_abort_retry(self.batch(), marker_drift=True)

    def test_native_partial_foreign_bytes_refuse_abort_without_release(self):
        _, coordinator, _, _, row = self.interrupt(self.batch())
        path = ts.loads(row["plan"])["changes"][-1]["path"]
        foreign = b"Native foreign bytes\n"
        (self.root / path).write_bytes(foreign)
        before = self.snapshot(coordinator, row)
        with self.no_replan(), mock.patch.object(LocalRoot, "replace", side_effect=AssertionError("Divergent abort wrote")):
            code, result = self.cli("rollback", row)
        self.assertEqual((code, result["code"]), (2, "stale"), result)
        self.assertEqual(self.snapshot(coordinator, row), before)
        self.assertEqual((self.root / path).read_bytes(), foreign)
        self.assertTrue(coordinator.inspect(row["attempt"])["reservations"])

    def test_native_mid_abort_interruption_reopens_with_rolling_back_fence(self):
        batch = self.batch()
        _, coordinator, _, _, row = self.interrupt(batch)
        with self.no_replan(), mock.patch.object(LocalRoot, "replace", side_effect=OSError("Disposable abort interruption")):
            code, result = self.cli("rollback", row)
        self.assertEqual(code, 2, result)
        self.assertEqual(self.journal(coordinator, row["attempt"])["state"], "rolling-back")
        before = self.snapshot(coordinator, row)
        with self.no_replan(), self.assertRaises(ts.ReadError) as error:
            managed_write.recover(self.root, self.host, "writer", row["attempt"], {})
        self.assertIn("host rollback", error.exception.message)
        self.assertEqual(self.snapshot(coordinator, row), before)
        with self.no_replan():
            self.assertEqual(self.cli("rollback", row)[0], 0)
            self.assertEqual(self.cli("reconcile", row)[1]["outcome"], "not-applied")
            self.assertEqual(managed_write.recover(self.root, self.host, "writer", row["attempt"], {})["outcome"], "retry")
        self.submit(batch)
        self.assert_images(row, "after")

    def test_native_undo_foreign_actor_and_changed_parent_afterimage_refuse(self):
        original = self.submit(self.batch())
        undo = self.undo_request(original)
        wrong = deepcopy(undo)
        wrong["actor"] = "other"
        before = self.store()
        with self.assertRaises(ts.ReadError):
            self.plan(self.batch(wrong, "wrong-native-undo-actor"))
        self.assertEqual(self.store(), before)
        other_host = Path(self.temp.name) / "other-host"
        host.enroll(self.root, other_host, "writer", ["docs/admission.md"], shared=True)
        wrong = deepcopy(undo)
        wrong["host"] = str(other_host)
        with self.assertRaises(ts.ReadError) as error:
            self.plan(self.batch(wrong, "wrong-native-undo-host"))
        self.assertEqual(error.exception.code, "wrong-scope")
        self.assertEqual(self.store(), before)
        self.put("docs/campaigns/imported.md", "Foreign parent after-image\n")
        with self.assertRaises(ts.ReadError):
            self.plan(self.batch(undo, "stale-native-undo"))
        self.assertEqual(self.store(), before)
        self.assertEqual((self.root / "docs/campaigns/imported.md").read_text(), "Foreign parent after-image\n")

    def test_completed_requests_past_retention_prune_native_receipt_then_frozen_retry(self):
        batch = self.batch()
        prepared = self.plan(batch)
        original = self.submit(batch)
        row = self.journal(self.coordinator, original["request_id"])
        with retention.patched_retention():
            retention.complete_past_limit(self, self.host, self.root, "Native context retention")
        db = write_retry._connect(self.host, readonly=True)
        try:
            self.assertIsNone(db.execute("SELECT id FROM requests WHERE id=?", (original["request_id"],)).fetchone())
        finally:
            db.close()
        self.assertEqual(self.journal(self.coordinator, row["attempt"]), row)
        binding = self.binding(row["attempt"])
        self.assert_images(row, "after")
        # Real source is still a Git object, but current derived planning must not read it.
        self.put("README.md", "Changed current source after receipt pruning\n")
        before = self.snapshot(self.coordinator, row)
        with self.no_replan(), mock.patch.object(LocalRoot, "replace", side_effect=AssertionError("Pruned replay wrote")):
            self.assertEqual(self.submit(batch)["request_id"], original["request_id"])
            repeated = write_batch.apply_prepared(self.root, self.host, "writer", prepared, original["request_id"])
        self.assertTrue(repeated["complete"])
        self.assertEqual(self.snapshot(self.coordinator, row), before)
        self.assertEqual(self.binding(row["attempt"]), binding)
        self.assertFalse((self.root / MARKER).exists())

    def test_h4_absolute_relative_dot_public_paths_preserve_same_native_binding(self):
        batch = self.batch()
        variants = [str(self.root), os.path.relpath(self.root), str(self.root / ".." / self.root.name)]
        plans = [self.public_plan(batch, spelling) for spelling in variants]
        self.assertTrue(all(p["fingerprint"] == plans[0]["fingerprint"] for p in plans))
        code, completed = self.board_cli(["batch", "submit", "--host", str(self.host), "--actor", "writer"], root=variants[1], request=batch)
        self.assertEqual((code, completed["outcome"]), (0, "complete"), completed)
        binding = self.binding(completed["request_id"])
        for spelling in variants:
            with self.subTest(root=spelling), self.no_replan():
                code, repeated = self.board_cli(["batch", "submit", "--host", str(self.host), "--actor", "writer"], root=spelling, request=batch)
            self.assertEqual((code, repeated["outcome"], repeated["request_id"]), (0, "complete", completed["request_id"]), repeated)
            self.assertEqual(self.binding(completed["request_id"]), binding)

    @unittest.skipUnless(os.name == "nt", "Case spelling qualification is Windows-specific")
    def test_h4_windows_case_plan_refuses_lexical_alias_submit_uses_canonical_root(self):
        batch = self.batch()
        alternate = str(self.root).swapcase()
        self.assertTrue(Path(alternate).is_dir())
        before = self.store()
        for args in (["import", "form"], ["batch", "plan"]):
            code, refusal = self.board_cli([*args, "--host", str(self.host), "--actor", "writer"], root=alternate,
                                          request=batch if args[0] == "batch" else None)
            self.assertEqual((code, refusal["code"]), (2, "wrong-scope"), refusal)
        self.assertEqual(self.store(), before)
        original = self.submit(batch)
        with self.no_replan():
            code, repeated = self.board_cli(["batch", "submit", "--host", str(self.host), "--actor", "writer"], root=alternate, request=batch)
        self.assertEqual((code, repeated["outcome"], repeated["request_id"]), (0, "complete", original["request_id"]), repeated)
        self.assertEqual(self.binding(original["request_id"])[0], str(self.root))

    def test_h4_lexical_symlink_parent_remains_refused_before_native_context(self):
        alias = Path(self.temp.name) / "alias"
        try:
            alias.symlink_to(self.root, target_is_directory=True)
        except OSError as error:
            self.skipTest("Disposable directory symlink unavailable: " + str(error))
        before = self.store()
        with self.assertRaises(ts.ReadError) as error:
            board_import.form(alias, authority=self.authority)
        self.assertEqual(error.exception.code, "unsafe-path")
        self.assertEqual(self.store(), before)


if __name__ == "__main__":
    unittest.main()
