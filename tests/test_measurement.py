"""Metadata selection, corrections, failure isolation and comparable accounting."""
from copy import deepcopy
import time
import unittest
from unittest.mock import patch

from eutonos import Hub, GitView, tokenslang as ts, measurement as m
from eutonos.effects import FixtureAuthority
import test_ram


class MeasurementTests(unittest.TestCase):
    def setUp(self):
        self.f = test_ram.RamTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.meter = m.Meter()
        self.ident = {"issuer_id": "host1", "batch_id": "session1"}
        self.selection = {"scope_id": "fixture.scope", "work_id": "work1", "session_id": "session1", "attempt_id": "attempt1", "source_commit": self.f.pin}

    def item(self, **kwargs):
        return {"id": "e1", "logical_id": "pickup1", "parent_id": None, "operation": "hub.read", "level": "entry", "outcome": "ok",
                "elapsed_ns": 100, "returned_bytes": 200, "provider_usage": None, "incident": None, **kwargs}

    def batch(self): return self.meter.snapshot(self.ident, self.selection)

    def test_real_hub_observation_and_broken_off_observers(self):
        request = {"profile": ts.PROFILE, "operation": "orient", "request_id": "pickup", "purpose": "Recover native work", "max_bytes": 32768}
        baseline = Hub(GitView(self.f.root, self.f.pin)).handle(request)
        self.assertEqual(baseline["outcome"], "ok")
        self.assertEqual(Hub(GitView(self.f.root, self.f.pin), meter=self.meter).handle(request), baseline)
        self.assertGreater(self.meter.events[0]["elapsed_ns"], 0)
        self.assertEqual(self.meter.events[0]["returned_bytes"], len(ts.encoded(baseline)))
        class Broken:
            events = []; dropped = 0
            def record(self, _): raise OSError("optional observation unavailable")
        broken = Broken()
        self.assertEqual(Hub(GitView(self.f.root, self.f.pin), meter=broken).handle(request), baseline)
        self.assertEqual(broken.dropped, 1)
        off = m.Meter(enabled=False)
        self.assertEqual(Hub(GitView(self.f.root, self.f.pin), meter=off).handle(request), baseline)
        self.assertEqual(off.snapshot(self.ident, self.selection)["coverage"]["extent"], "not-collected")

    def test_call_exception_remains_unchanged(self):
        def fail(): raise ValueError("underlying task failed")
        with self.assertRaisesRegex(ValueError, "underlying task failed"): m.observe(self.meter, fail)
        self.assertEqual(self.meter.events[0]["outcome"], "error")
        self.assertIsNone(self.meter.events[0]["returned_bytes"])

    def test_disabled_native_meter_skips_observation_encoding_and_preserves_exceptions(self):
        off = m.Meter(enabled=False)
        result = {"outcome": "ok", "value": "primary-result"}
        with patch.object(m.ts, "encoded", side_effect=AssertionError("disabled Meter must not encode")), \
             patch.object(off, "record", side_effect=AssertionError("disabled Meter must not construct/record an event")):
            self.assertIs(m.observe(off, lambda: result), result)
        self.assertEqual(off.events, [])
        self.assertEqual(off.dropped, 0)

        def fail():
            raise ValueError("primary failure")
        with patch.object(m.ts, "encoded", side_effect=AssertionError("disabled Meter must not encode")):
            with self.assertRaisesRegex(ValueError, "primary failure"):
                m.observe(off, fail)
        self.assertEqual(off.events, [])
        self.assertEqual(off.dropped, 0)

    def test_generated_logical_ids_are_meter_scoped_but_explicit_correlation_survives(self):
        first, second = m.Meter(), m.Meter()
        m.observe(first, lambda: {"outcome": "ok"})
        m.observe(second, lambda: {"outcome": "ok"})
        first_batch = first.snapshot({"issuer_id": "host1", "batch_id": "generated-a"}, self.selection)
        second_batch = second.snapshot({"issuer_id": "host2", "batch_id": "generated-b"}, self.selection)
        self.assertNotEqual(first_batch["events"][0]["logical_id"], second_batch["events"][0]["logical_id"])
        generated = m.summary([first_batch, second_batch])["workflow_efficiency"]
        self.assertEqual(generated["outer_invocations"], 2)
        self.assertEqual(generated["logical_operations"], 2)
        self.assertEqual(generated["repeated_logical_events"], 0)
        self.assertIn("Legacy v1", generated["logical_identity_qualification"])

        retry_a, retry_b = m.Meter(), m.Meter()
        m.observe(retry_a, lambda: {"outcome": "error"}, logical_id="shared-retry")
        m.observe(retry_b, lambda: {"outcome": "ok"}, logical_id="shared-retry")
        retry_first = retry_a.snapshot({"issuer_id": "host1", "batch_id": "retry-a"}, self.selection)
        retry_second = retry_b.snapshot({"issuer_id": "host2", "batch_id": "retry-b"}, self.selection)
        explicit = m.summary([retry_first, retry_second])["workflow_efficiency"]
        self.assertEqual(explicit["logical_operations"], 1)
        self.assertEqual(explicit["repeated_logical_events"], 1)
        self.assertEqual(explicit["repeated_failure_events"], 1)
        self.assertIn("not a count of failed retry attempts", explicit["repeated_failure_meaning"])

    def test_checked_projection_validates_scope_controls_malformed_records_and_truncation(self):
        root = self.f.root
        (root / "usage/host1").mkdir(parents=True)
        config = {"profile": m.CONFIG, "scope_id": "fixture.scope", "store_root": "usage",
                  "mode": "metadata", "retention": "explicit-owner-controls", "export": "explicit-guarded-publication"}
        (root / "eutonos.measurement.json").write_bytes(ts.encoded(config))
        store = m.Store(self.f.local)

        def retain(batch_id, *, scope="fixture.scope", state="active", visibility="public"):
            meter = m.Meter()
            meter.record(self.item(id="event-" + batch_id, logical_id="logical-" + batch_id))
            selection = deepcopy(self.selection)
            selection["scope_id"] = scope
            batch = meter.snapshot({"issuer_id": "host1", "batch_id": batch_id}, selection)
            batch["state"], batch["visibility"] = state, visibility
            path = root / store.path(batch["identity"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(ts.encoded(batch))
            return batch

        retain("a")
        retain("b")
        retain("c")
        retain("001-foreign", scope="foreign.scope")
        retain("suppressed", state="suppressed")
        retain("private", visibility="private")
        (root / "usage/host1/000-malformed.json").write_bytes(b"{")

        projected = store.project(max_records=2)
        self.assertEqual([item["identity"]["batch_id"] for item in projected["summary"]["inputs"]], ["a", "b"])
        self.assertTrue(projected["coverage"]["truncated"])
        self.assertEqual(projected["coverage"]["selection_limit"], 2)
        self.assertEqual(projected["coverage"]["state"], "partial")
        self.assertGreaterEqual(projected["coverage"]["rejected_records"], 2)
        self.assertGreaterEqual(projected["coverage"]["checked_candidates"], 4)
        self.assertTrue(all("{" not in item for item in projected["diagnostics"]))

        full = store.project(max_records=16)
        self.assertEqual([item["identity"]["batch_id"] for item in full["summary"]["inputs"]], ["a", "b", "c"])
        self.assertEqual(full["coverage"]["filtered_records"], 2)
        self.assertFalse(full["coverage"]["truncated"])

        config["mode"] = "off"
        (root / "eutonos.measurement.json").write_bytes(ts.encoded(config))
        off_store = m.Store(self.f.local)
        before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}
        off = off_store.project(max_records=16)
        after = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}
        self.assertEqual(before, after)
        self.assertEqual(off["coverage"]["collection_mode"], "off")
        self.assertTrue(off["coverage"]["retained_history"])
        self.assertEqual(len(off["summary"]["inputs"]), 3)
        self.assertIn("does not create new observations", off["coverage"]["qualification"])

    def test_checked_projection_rejects_unsafe_store_roots_and_symlink_escape(self):
        root = self.f.root
        for unsafe in ("../outside", "/absolute"):
            (root / "eutonos.measurement.json").write_bytes(ts.encoded({"profile": m.CONFIG, "scope_id": "fixture.scope",
                "store_root": unsafe, "mode": "metadata", "retention": "explicit-owner-controls",
                "export": "explicit-guarded-publication"}))
            with self.assertRaises(ts.ReadError) as error:
                m.Store(self.f.local)
            self.assertEqual(error.exception.code, "unsafe-path")

        (root / "usage/host1").mkdir(parents=True)
        (root / "eutonos.measurement.json").write_bytes(ts.encoded({"profile": m.CONFIG, "scope_id": "fixture.scope",
            "store_root": "usage", "mode": "metadata", "retention": "explicit-owner-controls",
            "export": "explicit-guarded-publication"}))
        store = m.Store(self.f.local)
        secret = root.parent / "measurement-secret.json"
        secret.write_text('{"secret":"PRIVATE-CONTENT-SENTINEL"}', encoding="utf-8")
        link = root / "usage/host1/escape.json"
        try:
            link.symlink_to(secret)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable on this platform")
        projected = store.project()
        self.assertGreaterEqual(projected["coverage"]["rejected_records"], 1)
        self.assertNotIn("PRIVATE-CONTENT-SENTINEL", repr(projected["diagnostics"]))

    def test_checked_projection_revalidates_selected_record_bytes(self):
        root = self.f.root
        (root / "usage/host1").mkdir(parents=True)
        (root / "eutonos.measurement.json").write_bytes(ts.encoded({"profile": m.CONFIG, "scope_id": "fixture.scope",
            "store_root": "usage", "mode": "metadata", "retention": "explicit-owner-controls",
            "export": "explicit-guarded-publication"}))
        store = m.Store(self.f.local)
        meter = m.Meter()
        meter.record(self.item(id="event-current", logical_id="logical-current"))
        batch = meter.snapshot({"issuer_id": "host1", "batch_id": "current"}, self.selection)
        path = store.path(batch["identity"])
        (root / path).write_bytes(ts.encoded(batch))
        original_read = store.root.read
        record_reads = 0

        def drifting_read(name):
            nonlocal record_reads
            raw = original_read(name)
            if name == path:
                record_reads += 1
                if record_reads > 1:
                    return raw + b" "
            return raw

        with patch.object(store.root, "read", side_effect=drifting_read):
            with self.assertRaises(ts.ReadError) as error:
                store.project()
        self.assertEqual(error.exception.code, "stale")

        config_reads = 0
        def drifting_config(name):
            nonlocal config_reads
            raw = original_read(name)
            if name == store.config_path:
                config_reads += 1
                if config_reads > 1:
                    return raw + b" "
            return raw

        with patch.object(store.root, "read", side_effect=drifting_config):
            with self.assertRaises(ts.ReadError) as error:
                store.project()
        self.assertEqual(error.exception.code, "stale")

    def test_batch_retransmission_correction_and_key_order(self):
        self.meter.record(self.item()); first = self.batch(); copy = deepcopy(first)
        copy["identity"] = {"batch_id": "session1", "issuer_id": "host1"}
        self.assertEqual(m.summary([first, copy])["levels"]["entry"]["count"], 1)
        newer = deepcopy(first); newer["revision"] = 2; newer["events"][0]["returned_bytes"] = 300
        result = m.summary([newer, first, newer])
        self.assertEqual(result["levels"]["entry"]["returned_bytes"], 300)
        copy["events"][0]["outcome"] = "error"
        with self.assertRaises(ts.ReadError): m.summary([first, copy])

    def test_nested_levels_and_unknown_usage(self):
        for item in (self.item(), self.item(id="e2", level="module", parent_id="e1"), self.item(id="e3", level="attempt", parent_id="e2", elapsed_ns=None)):
            self.meter.record(item)
        result = m.summary([self.batch()])
        self.assertEqual([result["levels"][k]["count"] for k in ("entry", "module", "attempt")], [1, 1, 1])
        self.assertEqual(result["levels"]["attempt"]["unknown_durations"], 1)
        self.assertEqual(result["workflow_efficiency"]["outer_invocations"], 1)
        self.assertEqual(result["workflow_efficiency"]["logical_operations"], 1)
        self.assertEqual(result["workflow_efficiency"]["repeated_logical_events"], 2)
        self.assertIn("transport", result["workflow_efficiency"]["coverage"])
        self.assertIsNone(result["provider_tokens"]); self.assertIsNone(result["task_savings"])

    def test_workflow_efficiency_keeps_retries_and_unknowns_qualified(self):
        self.meter.record(self.item(outcome="error", returned_bytes=None))
        self.meter.record(self.item(id="e2", outcome="ok"))
        result = m.summary([self.batch()])["workflow_efficiency"]
        self.assertEqual(result["outer_invocations"], 2)
        self.assertEqual(result["logical_operations"], 1)
        self.assertEqual(result["repeated_logical_events"], 1)
        self.assertEqual(result["repeated_failure_events"], 1)
        self.assertIn("not a count of failed retry attempts", result["repeated_failure_meaning"])
        self.assertEqual(result["unknown_entry_sizes"], 1)

    def test_metadata_filter_bounds_and_qualified_incident(self):
        self.meter = m.Meter(max_events=1)
        self.meter.record(self.item(prompt="must never be retained"))
        self.meter.record(self.item(incident={"kind": "handoff-recovery", "cause": "unknown", "basis": "Observed source lookup after replacement",
                                            "evidence": [{"path": "CHECK.md", "sha256": "fixture-evidence"}]}))
        self.meter.record(self.item(id="e2"))
        batch = self.batch(); self.assertEqual(batch["coverage"]["dropped"], 2)
        self.assertNotIn(b"must never", ts.encoded(batch))
        self.assertEqual(batch["events"][0]["incident"]["cause"], "unknown")
        self.assertGreater(batch["instrumentation_ns"], 0)

    def test_supplied_provider_subsets_not_summed(self):
        value = {"provider": "fixture", "model": None, "basis": "Synthetic reported usage; not live billing", "input_tokens": 100,
                 "output_tokens": 10, "cached_input_tokens": 80, "reasoning_output_tokens": 5}
        self.meter.record(self.item(provider_usage=value))
        result = m.summary([self.batch()])
        self.assertEqual(result["exposed_usage"][0]["usage"]["input_tokens"], 100)
        self.assertIsNone(result["provider_tokens"])
        with self.assertRaises(ts.ReadError): m.usage({**value, "cached_input_tokens": 101})

    def test_comparison_negative_unknown_and_mismatched(self):
        value = {"amount": 100, "unit": "bytes", "boundary": "selected response", "view": self.f.pin, "method": "len-utf8"}
        result = m.compare(value, {**value, "amount": 150})
        self.assertEqual(result["difference"], -50); self.assertEqual(result["percentage"], -50)
        self.assertIsNone(m.compare(value, {**value, "amount": None})["difference"])
        self.assertIsNone(m.compare({**value, "amount": 0}, value)["percentage"])
        with self.assertRaises(ts.ReadError): m.compare(value, {**value, "unit": "tokens"})

    def test_guarded_export_retry_correction_and_suppression(self):
        root = self.f.root; (root / "usage/host1").mkdir(parents=True)
        (root / "eutonos.measurement.json").write_bytes(ts.encoded({"profile": m.CONFIG, "scope_id": "fixture.scope", "store_root": "usage",
            "mode": "metadata", "retention": "explicit-owner-controls", "export": "explicit-guarded-publication"}))
        store = m.Store(self.f.local); self.meter.record(self.item()); batch = self.batch()
        value = store.plan(batch, None, "observer")
        auth = FixtureAuthority(self.f.local, "observer", frozenset(value["effect"]["observed"]), time.time() + 600)
        self.f.coordinator.start(auth, "measure-attempt"); self.f.coordinator.reserve(auth, "measure-attempt", 1, [store.path(self.ident)])
        m.apply(self.f.coordinator, auth, "measure-attempt", 1, value, value["fingerprint"])
        self.assertEqual(store.plan(batch, None, "observer")["effect"]["changes"], [])
        newer = deepcopy(batch); newer["revision"] = 2; newer["state"] = "suppressed"
        value = store.plan(newer, 1, "observer"); m.apply(self.f.coordinator, auth, "measure-attempt", 1, value, value["fingerprint"])
        self.assertEqual(store.read([self.ident])["inputs"], [])
        with self.assertRaises(ts.ReadError): store.plan(batch, 1, "observer")

    def store_fixture(self):
        root = self.f.root
        (root / "usage/host1").mkdir(parents=True, exist_ok=True)
        cfg = {"profile": m.CONFIG, "scope_id": "fixture.scope", "store_root": "usage",
               "mode": "metadata", "retention": "explicit-owner-controls", "export": "explicit-guarded-publication"}
        (root / "eutonos.measurement.json").write_bytes(ts.encoded(cfg))
        return m.Store(self.f.local), cfg

    def test_off_bypass_is_exact_and_cannot_switch_other_meter(self):
        off, other = m.Meter(enabled=False), m.Meter()
        def call():
            m.observe(other, lambda: {"outcome": "ok"})
            return "primary-result"
        self.assertEqual(m.observe(off, call), "primary-result")
        self.assertEqual(len(other.events), 1)
        self.assertEqual(off.events, [])
        self.assertFalse(off.enabled)
        class Custom(m.Meter):
            def record(self, value):
                self.observed = value
        custom = Custom(enabled=False)
        self.assertEqual(m.observe(custom, lambda: "result"), "result")
        self.assertEqual(custom.observed["outcome"], "ok")

    def test_policy_off_between_plan_and_apply_prevents_write(self):
        store, cfg = self.store_fixture()
        self.meter.record(self.item())
        value = store.plan(self.batch(), None, "observer")
        auth = FixtureAuthority(self.f.local, "observer", frozenset(value["effect"]["observed"]), time.time() + 600)
        self.f.coordinator.start(auth, "measure-policy-attempt")
        self.f.coordinator.reserve(auth, "measure-policy-attempt", 1, [store.path(self.ident)])
        cfg["mode"] = "off"
        (self.f.root / "eutonos.measurement.json").write_bytes(ts.encoded(cfg))
        with self.assertRaises(ts.ReadError):
            m.apply(self.f.coordinator, auth, "measure-policy-attempt", 1, value, value["fingerprint"])
        self.assertIsNone(self.f.local.read(store.path(self.ident)))
        with self.assertRaises(ts.ReadError) as caught:
            store.plan(self.batch(), None, "observer")
        self.assertEqual(caught.exception.code, "stale")

    def test_projection_caps_discovery_and_honors_excluded_store(self):
        store, _ = self.store_fixture()
        self.meter.record(self.item())
        for number in range(6):
            batch = deepcopy(self.batch())
            batch["identity"]["batch_id"] = "bounded-" + str(number)
            (self.f.root / store.path(batch["identity"])).write_bytes(ts.encoded(batch))
        limited = store.project(max_candidates=2)
        self.assertTrue(limited["coverage"]["discovery_truncated"])
        self.assertEqual(limited["coverage"]["state"], "partial")
        self.assertLessEqual(limited["coverage"]["checked_candidates"], 2)
        from eutonos.effects import LocalRoot
        excluded = m.Store(LocalRoot(self.f.root, excluded=("usage",)))
        original_scan = m.os.scandir
        def selected_scan(path):
            self.assertNotEqual(Path(path), self.f.root / "usage", "Excluded store must not be enumerated")
            return original_scan(path)
        from pathlib import Path
        with patch.object(m.os, "scandir", side_effect=selected_scan):
            with self.assertRaises(ts.ReadError) as caught:
                excluded.project()
        self.assertEqual(caught.exception.code, "denied")

    def test_projection_skips_inadmissible_discovery_paths_without_losing_valid_history(self):
        from contextlib import nullcontext
        from pathlib import Path
        from types import SimpleNamespace
        import stat

        store, _ = self.store_fixture()
        self.meter.record(self.item())
        (self.f.root / store.path(self.ident)).write_bytes(ts.encoded(self.batch()))
        directory = self.f.root / "usage/host1"
        original_scan = m.os.scandir
        with original_scan(directory) as entries:
            valid_entries = list(entries)

        # Synthetic entries permit reserved Windows names and deep paths on every
        # platform, while actual safe_path and Store.project remain unpatched.
        long_path = "/".join(["PRIVATE-LENGTH-SENTINEL" + "x" * 180] * 6 + ["hidden.json"])
        self.assertGreater(len(long_path), 1024)
        self.assertLessEqual(len(("usage/host1/" + long_path).split("/")), 32)
        bad_paths = [("aux.json", "unsafe-path"), ("PRIVATE-PATH-SENTINEL./hidden.json", "unsafe-path"),
                     ("/".join(["deep"] * 32 + ["hidden.json"]), "unsafe-path"), (long_path, "invalid")]
        for bad, rejection_code in bad_paths:
            with self.subTest(path=bad):
                with self.assertRaises(ts.ReadError) as rejected:
                    m.safe_path("usage/host1/" + bad)
                self.assertEqual(rejected.exception.code, rejection_code)
                bad_entry = SimpleNamespace(name="hidden.json", path=str(directory / bad),
                    stat=lambda **kwargs: SimpleNamespace(st_mode=stat.S_IFREG))

                def selected_scan(path):
                    if Path(path) == directory:
                        return nullcontext(iter([bad_entry, *valid_entries]))
                    return original_scan(path)

                with patch.object(m.os, "scandir", side_effect=selected_scan):
                    result = store.project()
                self.assertEqual(result["coverage"]["state"], "partial")
                self.assertEqual(result["coverage"]["rejected_records"], 1)
                self.assertEqual(result["coverage"]["selected_records"], 1)
                self.assertEqual(result["coverage"]["checked_candidates"], 1)
                self.assertFalse(result["coverage"]["discovery_truncated"])
                self.assertEqual([i["identity"] for i in result["summary"]["inputs"]], [self.ident])
                self.assertEqual(len(result["diagnostics"]), 1)
                self.assertIn("unsafe-path", result["diagnostics"][0])
                self.assertNotIn(bad, repr(result))
                self.assertNotIn("PRIVATE-PATH-SENTINEL", repr(result))
                self.assertNotIn("PRIVATE-LENGTH-SENTINEL", repr(result))

    def test_discovery_does_not_swallow_other_read_errors(self):
        store, _ = self.store_fixture()
        (self.f.root / store.path(self.ident)).write_bytes(ts.encoded(self.batch()))
        with patch.object(m, "safe_path", side_effect=ts.ReadError("stale", "Source changed")):
            with self.assertRaises(ts.ReadError) as failed:
                store.project()
        self.assertEqual(failed.exception.code, "stale")

    def test_projection_diagnostics_are_bounded_and_do_not_echo_malformed_contents(self):
        store, _ = self.store_fixture()
        for number in range(4):
            (self.f.root / f"usage/host1/bad-{number}.json").write_text("PRIVATE-MALFORMED-SENTINEL")
        result = store.project(max_diagnostics=1)
        self.assertEqual(len(result["diagnostics"]), 1)
        self.assertTrue(result["coverage"]["diagnostics_truncated"])
        self.assertEqual(result["coverage"]["rejected_records"], 4)
        self.assertNotIn("PRIVATE-MALFORMED-SENTINEL", repr(result))
        with self.assertRaises(ts.ReadError) as caught:
            store._checked_batch("usage/host1/oversized.json", b" " * (ts.MAX_SOURCE_BYTES + 1))
        self.assertEqual(caught.exception.code, "overflow")

    def test_project_does_not_expose_private_or_wrong_path_payloads(self):
        store, _ = self.store_fixture()
        self.meter.record(self.item(incident={"kind": "rework", "cause": "unknown", "basis": "PRIVATE-DETAIL-SENTINEL", "evidence": []}))
        private = self.batch()
        private["visibility"] = "private"
        path = store.path(self.ident)
        (self.f.root / path).write_bytes(ts.encoded(private))
        self.assertEqual(store.read([self.ident])["inputs"], [])
        result = store.project()
        self.assertEqual(result["coverage"]["filtered_records"], 1)
        self.assertNotIn("PRIVATE-DETAIL-SENTINEL", repr(result))
        private["visibility"] = "public"
        (self.f.root / "usage/host1/wrong-identity.json").write_bytes(ts.encoded(private))
        result = store.project()
        self.assertEqual(result["coverage"]["rejected_records"], 1)
        self.assertNotIn("PRIVATE-DETAIL-SENTINEL", repr(result))


if __name__ == "__main__": unittest.main()
