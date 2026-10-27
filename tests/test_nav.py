"""Installed EHF4 NAV behavior with current-agent CLI and deterministic providers."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from eutonos import GitView, Hub, tokenslang as ts
from eutonos.effects import Coordinator, FixtureAuthority, LocalRoot, MARKER, MARKER_BYTES, digest
from eutonos import nav, nav_cli, nav_history
from eutonos.nav_runner import Session, REPORT, leads


class NavCliErrorTests(unittest.TestCase):
    def test_io_error_retains_unavailable_and_os_detail(self):
        error = PermissionError(13, "Access is denied", "selected-checkout")
        error.winerror = 5
        output = io.BytesIO()
        console = io.TextIOWrapper(output, encoding="utf-8")
        with patch.object(nav_cli, "LocalRoot", side_effect=error), patch("sys.stdout", console):
            self.assertEqual(nav_cli.main(["status", "--root", "selected-checkout"]), 2)
        result = json.loads(output.getvalue())
        self.assertEqual(result["code"], "unavailable")
        self.assertEqual(result["outcome"], "error")
        self.assertIn("Access is denied", result["detail"])
        self.assertIn("selected-checkout", result["detail"])
        self.assertEqual(result["os_error"], {"errno": 13, "winerror": 5})
        console.detach()

    def test_domain_error_keeps_its_existing_classification(self):
        output = io.BytesIO()
        console = io.TextIOWrapper(output, encoding="utf-8")
        with patch.object(nav_cli, "LocalRoot", side_effect=ts.ReadError("stale", "Source changed.")), \
                patch("sys.stdout", console):
            self.assertEqual(nav_cli.main(["status", "--root", "selected-checkout"]), 2)
        result = json.loads(output.getvalue())
        self.assertEqual(result["code"], "stale")
        self.assertEqual(result["message"], "Source changed.")
        self.assertNotIn("os_error", result)
        console.detach()


class NavTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eutonos-nav-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.root = self.home / "native fixture"
        self.root.mkdir()
        (self.root / "src").mkdir(); (self.root / "NAV").mkdir()
        (self.root / MARKER).write_bytes(MARKER_BYTES)
        (self.root / "AGENTS.md").write_bytes(b"# Fixture rules\nPreserve native owners. No deployment.\n")
        (self.root / "src/auth.py").write_bytes(b"def login(user):\n    return user is not None\n")
        (self.root / "src/db.py").write_bytes(b"def store(data):\n    return dict(data)\n")
        self.cfg = {"profile": nav.CONFIG, "repository_id": "fixture.repo", "scope_id": "fixture.scope",
                    "namespace": "fixture.native", "map_root": "NAV", "source_roots": ["src"],
                    "excluded_roots": ["src/vendor"], "context_paths": ["AGENTS.md"]}
        (self.root / "eutonos.nav.json").write_bytes(ts.encoded(self.cfg))
        self.git("init", "-q", "-b", "main")
        self.commit()
        self.local = LocalRoot(self.root)
        self.session = Session(self.local)
        self.coordinator = Coordinator.initialize(self.home / "owner.sqlite")
        self.auth = FixtureAuthority(self.local, "fixture-agent", self.session.paths, time.time() + 600)
        self.coordinator.start(self.auth, "nav-test")
        self.coordinator.reserve(self.auth, "nav-test", 1, self.session.paths)
        self.host = (self.coordinator, self.auth, "nav-test", 1)

    def git(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        return subprocess.check_output(["git", "-c", "safe.directory=" + str(self.root), "-c", "core.autocrlf=false",
                                       "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                                       "-C", str(self.root), *args], env=env, stderr=subprocess.PIPE)

    def commit(self):
        self.git("add", "--all"); self.git("commit", "-qm", "Fixture snapshot")
        return self.git("rev-parse", "HEAD").decode().strip()

    def admit_history(self, run_id):
        paths = self.session.history_plan(run_id)["paths"]
        self.auth = FixtureAuthority(self.local, "fixture-agent", frozenset(paths), time.time() + 600)
        self.host = (self.coordinator, self.auth, "nav-test", 1)
        self.coordinator.reserve(self.auth, "nav-test", 1, paths)

    def start(self, run="build-1", mode="build", restart=False):
        raw = self.local.read(self.session.state_path)
        if (not raw or ts.loads(raw.decode())["runID"] != run) and not (mode == "maintenance" and self.session.maintenance()["outcome"] == "no-op"):
            self.admit_history(run)
        return self.session.start(self.host, run, mode, restart)

    def report(self, packet):
        # Actually read every extant issued source, including required rules.
        inspected = sorted({p for l in packet["leads"] for p in l["paths"] if (self.root / p).is_file()})
        for p in inspected:
            (self.root / p).read_bytes()
        return {"profile": REPORT, "packetID": packet["packetID"], "sweepID": packet["sweepID"],
                "sourceFingerprint": packet["sourceFingerprint"], "inspectedPaths": inspected,
                "leadDispositions": [{"leadID": l["id"], "disposition": "covered" if set(l["paths"]) & set(inspected) else "rejected",
                                      "reason": "Deterministic fixture inspection; not product semantic acceptance."} for l in packet["leads"]],
                "proposedRecords": [], "omissionSearch": "Inspected issued fixture paths; no broader quality claim.", "summary": "Fixture report."}

    def record(self, rid, packet, evidence="src/auth.py", related="none", extra=""):
        kind, owner = nav.KINDS[rid.split("-")[0]]
        return {"recordID": rid, "action": "add", "mapFile": owner, "evidencePaths": [evidence],
                "text": f"### `{rid}` Fixture {rid}\n- Kind: {kind}\n- Aliases: login; fixture\n- Summary: Fixture source route.\n- Evidence: {evidence}\n- Related: {related}\n- Review: candidate\n- Revision: {packet['sourceFingerprint']}\n" + extra}

    def provider(self, packet):
        report = self.report(packet)
        records = self.session.observe()[2]
        candidates = {"P1-source-family-discovery": "SRC-auth", "P2-capability-system-discovery": "SYS-auth",
                      "P3-parent-surface-discovery": "SURF-login", "P3-interaction-discovery": "INT-submit",
                      "P4-user-journey-discovery": "JRN-login", "P5-child-surface-discovery": "SURF-child"}
        rid = candidates.get(packet["sweepID"])
        if rid and rid not in records and "src/auth.py" in report["inspectedPaths"]:
            report["proposedRecords"] = [self.record(rid, packet, extra="- Parent: SURF-login\n" if rid == "SURF-child" else "- Native note: preserve me\n")]
        return report

    def reject(self, code, func, *args):
        with self.assertRaises(ts.ReadError) as error:
            func(*args)
        self.assertEqual(error.exception.code, code)

    def cli(self, command, *args, payload=None):
        result = subprocess.run([sys.executable, "-m", "eutonos.nav_cli", command, "--root", str(self.root), *args],
                                input=ts.encoded(payload) if payload else None, capture_output=True, timeout=30)
        body = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0, body)
        return body

    def test_history_paging_integrity_and_continued_growth(self):
        plan = self.session.history_plan("build-1")
        self.assertIn("Read-only plan", plan["qualification"])
        self.assertIn("Reserve these exact paths through the selected host", plan["qualification"])
        self.assertIn("No admission or grant renewal is needed", plan["qualification"])
        self.start()
        self.session.run(self.host, self.provider, 64)
        old = self.session.observe()[4]
        cycles = [{**deepcopy(old), "runID": "legacy-" + str(i), "padding": "x" * 30000} for i in range(3)]
        legacy = {"cycles": cycles}
        self.assertGreater(len(ts.encoded({**legacy, "cycles": cycles + [old]})), ts.MAX_SOURCE_BYTES)
        archive = ts.encoded(legacy) + b"\n"
        archive_ref = {"path": self.session.history_path.removesuffix(".json") + "_" + digest(archive) + ".json",
                       "sha256": digest(archive)}
        self.local.path(archive_ref["path"]).write_bytes(archive)
        self.local.path(self.session.history_path).write_bytes(ts.encoded(
            {"profile": nav_history.INDEX, "head": None, "legacy": archive_ref, "count": 3}) + b"\n")
        self.admit_history("after-legacy")
        self.session.start(self.host, "after-legacy", "correction", False, ["SYS-auth"])
        self.assertLess(len(self.local.read(self.session.history_path)), 1024)
        first = self.session.history(1)
        self.assertEqual(first["items"][0]["runID"], "build-1")
        second = self.session.history(2, first["continuation"])
        self.assertEqual([i["runID"] for i in second["items"]], ["legacy-2", "legacy-1"])
        last = self.session.history(2, second["continuation"])
        self.assertEqual(last["items"][0]["runID"], "legacy-0")
        self.assertIsNone(last["continuation"])
        self.reject("conflict", self.session.history_plan, "legacy-0")
        ref = first["items"][0]["archive"]
        archived = self.local.read(ref["path"])
        self.assertEqual(ts.loads(archived.decode()), old)
        self.local.path(ref["path"]).write_bytes(b"{}")
        self.reject("stale", self.session.history)
        self.local.path(ref["path"]).write_bytes(archived)
        self.session.run(self.host, self.provider, 64)
        for i in range(3):
            run = "continued-" + str(i)
            self.admit_history(run)
            self.session.start(self.host, run, "correction", False, ["SYS-auth"])
            self.session.run(self.host, self.provider, 64)
            self.assertLess(len(self.local.read(self.session.history_path)), 1024)
        self.reject("stale", self.session.history, 1, first["continuation"])
        self.reject("conflict", self.session.history_plan, "after-legacy")

    def test_bounded_correction_and_generated_report(self):
        self.start()
        self.session.run(self.host, self.provider, 64)
        before = self.session.observe()[2]
        self.admit_history("fix-one")
        self.session.start(self.host, "fix-one", "correction", False, ["SYS-auth"])
        packet = self.session.next(self.host)["packet"]
        draft = self.cli("report-template")
        self.assertEqual(draft["inspectedPaths"], [])
        self.assertEqual(draft["leadDispositions"][0]["leadID"], packet["leads"][0]["id"])
        self.assertEqual(draft["leadDispositions"][0]["disposition"], "unresolved")
        report = self.report(packet)
        proposal = self.record("SYS-auth", packet, extra="- Native note: preserve me\n")
        proposal["action"] = "revise"
        proposal["text"] = proposal["text"].replace("Fixture source route.", "Corrected source-backed behavior.")
        report["proposedRecords"] = [proposal]
        source, bodies, records, preambles, _, _ = self.session.observe()
        bad = deepcopy(report)
        bad["proposedRecords"][0]["action"] = "add"
        self.reject("conflict", self.session.validate_report, bad, packet, source, bodies, records, preambles, "correction")
        bad = deepcopy(report)
        bad["leadDispositions"][0]["leadID"] = "wrong"
        self.reject("invalid", self.session.validate_report, bad, packet, source, bodies, records, preambles, "correction")
        self.assertEqual(self.session.submit(self.host, report)["outcome"], "submitted")
        self.assertEqual(self.session.next(self.host)["outcome"], "queue-complete")
        after = self.session.observe()[2]
        self.assertEqual(set(before), set(after))
        for rid in before:
            if rid != "SYS-auth":
                self.assertEqual(before[rid]["text"], after[rid]["text"])
        self.assertEqual(self.session.status()["closedSweeps"], ["C1-record-correction"])

    def test_full_dynamic_six_pass_construction_preserves_ids_and_source(self):
        before = {p: (self.root / p).read_bytes() for p in ["AGENTS.md", "src/auth.py", "src/db.py", "eutonos.nav.json"]}
        self.start()
        outcome = self.session.run(self.host, self.provider, 64)
        self.assertEqual(outcome["outcome"], "queue-complete")
        self.assertEqual(outcome["closedSweeps"], [s["id"] for s in nav.SWEEPS])
        self.assertEqual({s["pass"] for s in nav.SWEEPS}, set(range(1, 7)))
        records = self.session.observe()[2]
        self.assertTrue({"SRC-auth", "SYS-auth", "SURF-login", "SURF-child", "INT-submit", "JRN-login", "GAP-bootstrap"} <= records.keys())
        self.assertIn("Native note", records["SRC-auth"]["fields"])
        for name in nav.MAPS:
            self.assertFalse(self.local.read(self.session.names[name]).endswith(b"\n\n"))
        self.assertEqual(before, {p: (self.root / p).read_bytes() for p in before})
        parent_sweep = next(s for s in nav.SWEEPS if s["selector"] == "parent-surface-records")
        parents = leads(parent_sweep, records, self.session.observe()[0])
        self.assertEqual([l["recordIDs"] for l in parents], [["SURF-login"]])
        snapshot = {p: self.local.read(p) for p in self.session.paths}
        db = self.coordinator.path.read_bytes()
        self.assertEqual(self.session.maintenance()["outcome"], "no-op")
        self.assertEqual(self.start("maint-noop", "maintenance")["outcome"], "no-op")
        self.assertEqual(snapshot, {p: self.local.read(p) for p in self.session.paths})
        self.assertEqual(db, self.coordinator.path.read_bytes())

    def test_active_packet_replay_cli_and_duplicate_submission(self):
        args = ["--fixture", "--coordinator", str(self.coordinator.path), "--actor", "fixture-agent", "--attempt", "nav-test"]
        self.cli("start", *args, "--run-id", "cli-run")
        first = self.cli("next", *args)["packet"]
        self.assertEqual(first, self.cli("next", *args)["packet"])
        self.assertIn("No deployment", first["requiredContext"][0]["content"])
        report = self.report(first)
        self.cli("submit", *args, payload=report)
        db = self.coordinator.path.read_bytes()
        self.assertEqual(self.cli("submit", *args, payload=report)["outcome"], "already-submitted")
        self.assertEqual(db, self.coordinator.path.read_bytes())
        report["summary"] = "Changed retransmission"
        self.reject("conflict", self.session.submit, self.host, report)

    def test_malformed_retry_exhaustion_advances_and_provider_failure_preserves(self):
        self.start()
        first = self.session.next(self.host)["packet"]
        report = self.report(first); report["leadDispositions"] = []
        self.assertEqual(self.session.submit(self.host, report)["outcome"], "retry")
        self.assertEqual(self.session.submit(self.host, report)["outcome"], "closed-unresolved")
        self.assertTrue(self.session.status()["unresolvedPaths"])
        second = self.session.next(self.host)["packet"]
        self.assertNotEqual(first["packetID"], second["packetID"])
        def failed(_):
            raise RuntimeError("Provider unavailable")
        self.assertEqual(self.session.run(self.host, failed)["outcome"], "provider-failed")
        self.assertEqual(self.session.next(self.host)["packet"], second)
        self.assertEqual(self.session.run(self.host, lambda _: None)["outcome"], "provider-cancelled")
        self.assertEqual(self.session.next(self.host)["packet"], second)

    def test_source_drift_restart_archives_active_packet_without_overwrite(self):
        self.start()
        packet = self.session.next(self.host)["packet"]
        (self.root / "src/auth.py").write_bytes(b"def login(user):\n    return False\n")
        before = self.local.read(self.session.state_path)
        self.reject("stale", self.session.submit, self.host, self.report(packet))
        self.assertEqual(before, self.local.read(self.session.state_path))
        self.start("build-2", restart=True)
        listing = self.session.history()
        archived = ts.loads(nav.decode(self.local.read(listing["items"][0]["archive"]["path"])))
        self.assertEqual(archived["activePacket"], packet)
        self.assertEqual(self.session.status()["runID"], "build-2")

    def test_stale_refusal_names_changed_paths_and_restart_reports_prior_work(self):
        self.start()
        packet = self.session.next(self.host)["packet"]
        (self.root / "src/auth.py").write_bytes(b"def login(user):\n    return False\n")
        (self.root / "src/extra.py").write_bytes(b"X = 1\n")
        with self.assertRaises(ts.ReadError) as raised:
            self.session.submit(self.host, self.report(packet))
        self.assertEqual(raised.exception.code, "stale")
        self.assertIn("modified src/auth.py", raised.exception.message)
        self.assertIn("added src/extra.py", raised.exception.message)
        with self.assertRaises(ts.ReadError) as raised:
            self.session.next(self.host)
        self.assertEqual(raised.exception.code, "stale")
        self.assertIn("src/auth.py", raised.exception.message)
        result = self.start("build-2", restart=True)
        info = result["restarted"]
        self.assertEqual((info["archivedRunID"], info["acceptedReceipts"], info["closedSweeps"]), ("build-1", 0, []))
        self.assertEqual(info["changedSinceOldBaseline"]["added"], ["src/extra.py"])
        self.assertEqual(info["changedSinceOldBaseline"]["modified"], ["src/auth.py"])
        self.assertEqual(info["changedSinceOldBaseline"]["omitted"], 0)

    def test_validate_report_is_read_only_and_matches_submit_codes(self):
        self.start()
        packet = self.session.next(self.host)["packet"]
        before = self.local.read(self.session.state_path)
        good = self.report(packet)
        valid = self.cli("validate-report", payload=good)
        self.assertEqual((valid["outcome"], valid["valid"], valid["mechanicalOnly"]), ("valid", True, True))
        self.assertIn("Mechanical validation only", valid["qualification"])
        bad = {**good, "inspectedPaths": ["src/does_not_exist.py"]}
        invalid = self.cli("validate-report", payload=bad)
        self.assertEqual((invalid["outcome"], invalid["valid"], invalid["code"]), ("invalid", False, "missing"))
        self.assertEqual(before, self.local.read(self.session.state_path))
        self.assertEqual(self.session.observe()[4]["activePacket"]["retry"], 0)
        # submit reaches the same diagnostic, recorded as a retry.
        self.assertEqual(self.session.submit(self.host, bad)["outcome"], "retry")
        self.assertEqual(self.session.observe()[4]["activePacket"]["repair"]["reason"], invalid["message"])
        # Pre-submit refusals are identical errors.
        self.reject("stale", self.session.check_report, {**good, "packetID": "other"})

    def test_path_changes_caps_and_counts_remainder(self):
        from eutonos.nav_runner import path_changes
        changes = path_changes({}, {f"p{i:02d}": "h" for i in range(25)}, cap=20)
        self.assertEqual((changes["total"], len(changes["added"]), changes["omitted"]), (25, 20, 5))

    def test_map_only_commit_not_source_drift_but_unmanaged_map_edit_is(self):
        self.start()
        before = self.session.observe()[0]["fingerprint"]
        self.commit()
        self.assertEqual(before, self.session.observe()[0]["fingerprint"])
        self.session.next(self.host)
        p = self.root / "NAV/NAVIGATION.md"
        p.write_bytes(p.read_bytes() + b"\nUnmanaged change.\n")
        self.reject("stale", self.session.next, self.host)

    def seed_maps_without_baseline(self):
        from importlib.resources import files
        for name in nav.MAPS:
            body = files("eutonos").joinpath("nav_data/" + name.removesuffix(".md") + ".template.txt").read_bytes()                 .replace(b"{{GOVERNING_SOURCE}}", b"AGENTS.md")
            (self.root / "NAV" / name).write_bytes(body)

    def test_seeded_maps_without_baseline_are_not_uninitialized(self):
        self.seed_maps_without_baseline()
        for result in (self.session.status(), self.session.maintenance()):
            self.assertEqual(result["outcome"], "no-baseline")
            self.assertEqual(result["initialized"], "maps-present-no-baseline-run")
            self.assertEqual(len(result["maps"]), len(nav.MAPS))
            self.assertIsInstance(result["records"], int)
            self.assertEqual(result["sourceCurrent"], "unknown")
            self.assertEqual(result["mapsCurrent"], "unknown")
            self.assertEqual(result["semanticAcceptance"], "unproven")
            self.assertNotIn("Construct or import", result["next"])
            self.assertIn("nav start --mode build", result["next"])
            self.assertIn("no import command", result["next"])
        self.assertFalse((self.root / "NAV" / "NAV_RUN.json").exists())

    def test_uninitialized_nav_still_refuses_with_honest_guidance(self):
        with self.assertRaises(ts.ReadError) as raised:
            self.session.status()
        self.assertEqual(raised.exception.code, "missing")
        self.assertIn("not initialized", raised.exception.message)
        self.assertNotIn("Construct or import", raised.exception.message)

    def test_cli_help_states_read_is_committed_only(self):
        output = io.StringIO()
        with patch("sys.stdout", output), self.assertRaises(SystemExit):
            nav_cli.main(["--help"])
        text = " ".join(output.getvalue().split())
        self.assertIn("never saved uncommitted work", text)
        self.assertIn("repository_state_cli", text)

    def test_maintenance_added_deleted_affected_only_and_stable_move(self):
        self.start()
        self.session.run(self.host, self.provider, 64)
        old_records = self.session.observe()[2]
        (self.root / "src/auth.py").rename(self.root / "src/login.py")
        impact = self.session.maintenance()
        self.assertEqual(set(impact["changedPaths"]), {"src/auth.py", "src/login.py"})
        self.assertIn("SRC-auth", impact["affectedRecords"])
        self.assertNotIn("GAP-bootstrap", impact["affectedRecords"])
        self.start("move-maintenance", "maintenance")
        def move_provider(packet):
            report = self.report(packet)
            report["inspectedPaths"].append("src/login.py")
            for l in packet["leads"]:
                for rid in l["recordIDs"]:
                    r = old_records[rid]
                    text = r["text"].replace("src/auth.py", "src/login.py").replace(r["fields"]["Revision"], packet["sourceFingerprint"])
                    report["proposedRecords"].append({"recordID": rid, "action": "revise", "mapFile": r["map"],
                                                      "evidencePaths": ["src/login.py"], "text": text})
            return report
        result = self.session.run(self.host, move_provider, 64)
        self.assertEqual(result["outcome"], "queue-complete")
        self.assertEqual(result["closedSweeps"], ["M1-affected-review"])
        self.assertEqual(self.session.observe()[2]["SRC-auth"]["evidence"], ["src/login.py"])
        self.assertEqual(self.session.observe()[2]["GAP-bootstrap"]["text"], old_records["GAP-bootstrap"]["text"])
        self.assertEqual(self.session.maintenance()["outcome"], "no-op")

    def test_read_routes_alias_ambiguity_overflow_and_hub_same_scope(self):
        self.start(); self.session.run(self.host, self.provider, 64)
        catalog = {"profile": ts.CATALOG, "repository_id": "fixture.repo", "scope_id": "fixture.scope", "namespace": "fixture.native",
                   "owners": {"current_task": {"path": "AGENTS.md"}}, "bindings": [], "excluded_roots": [], "source_paths": ["AGENTS.md"]}
        (self.root / "eutonos.read.json").write_bytes(ts.encoded(catalog))
        revision = self.commit()
        hub = Hub(GitView(self.root, revision))
        request = {"profile": nav.READ, "operation": "browse", "limit": 2}
        first = hub.handle(request)
        self.assertEqual(len(first["items"]), 2)
        second = hub.handle({**request, "cursor": first["continuation"]})
        self.assertNotEqual(first["items"], second["items"])
        self.assertEqual(hub.handle({"profile": nav.READ, "operation": "search", "query": "nonexistent"})["total"], 0)
        ref = {"scope_id": "fixture.scope", "namespace": "fixture.native", "resource_id": "login"}
        self.assertEqual(hub.handle({"profile": nav.READ, "operation": "resolve", "target": ref})["code"], "ambiguous")
        exact = hub.handle({"profile": nav.READ, "operation": "resolve", "target": {**ref, "resource_id": "SRC-auth"}})
        self.assertEqual(exact["items"][0]["direct_sources"], ["src/auth.py"])
        before = self.coordinator.path.read_bytes()
        opened = self.cli("read", "--revision", revision, payload={"profile": nav.READ, "operation": "open", "path": "src/auth.py"})
        self.assertIn("def login", opened["content"])
        self.assertEqual(before, self.coordinator.path.read_bytes())
        small = hub.handle({"profile": nav.READ, "operation": "browse", "max_bytes": 512})
        self.assertEqual(small["outcome"], "overflow")
        self.assertLessEqual(len(ts.encoded(small)), 512)
        self.assertTrue(small.get("direct_map_sources") or small.get("continuation"))
        (self.root / "src/auth.py").write_bytes(b"changed committed source\n")
        changed = self.commit()
        index = nav.NavIndex(GitView(self.root, changed))
        stale = index.handle({"profile": nav.READ, "operation": "resolve", "target": {**ref, "resource_id": "SRC-auth"}})
        self.assertEqual(stale["items"][0]["source_bindings"][0]["binding"], "changed-since-run-baseline")
        self.reject("stale", index.handle, {**request, "cursor": first["continuation"]})
        catalog["bindings"] = [{"resource_id": "private.auth", "path": "src/auth.py", "kind": "source", "label": "Private",
                                "visibility": "private", "state": "active"}]
        (self.root / "eutonos.read.json").write_bytes(ts.encoded(catalog))
        private_view = self.commit()
        private_hub = Hub(GitView(self.root, private_view))
        denied = private_hub.handle({"profile": nav.READ, "operation": "open", "path": "src/auth.py"})
        self.assertEqual(denied["code"], "denied")
        hidden = private_hub.handle({"profile": nav.READ, "operation": "resolve", "target": {**ref, "resource_id": "SRC-auth"}})
        self.assertEqual(hidden["code"], "missing")

    def test_guarded_precondition_checks_source_again_after_journaling(self):
        self.start()
        original_inventory = nav.inventory
        calls = 0
        def change(*args):
            nonlocal calls
            calls += 1
            if calls == 3:  # Observe, preparation check, then execution check.
                (self.root / "src/auth.py").write_bytes(b"changed after journal\n")
            return original_inventory(*args)
        before = self.local.read(self.session.state_path)
        with patch("eutonos.nav.inventory", side_effect=change):
            self.reject("stale", self.session.next, self.host)
        self.assertEqual(before, self.local.read(self.session.state_path))
        args = ["--fixture", "--coordinator", str(self.coordinator.path), "--actor", "fixture-agent", "--attempt", "nav-test"]
        inspection = self.cli("effects", *args)
        effect_id = inspection["attempt"]["unfinished_effects"][0]["id"]
        self.assertEqual(self.cli("reconcile", *args, "--effect-id", effect_id)["outcome"], "not-applied")

    def test_unknown_config_unsafe_scope_and_large_required_context_reference(self):
        cfg = {**self.cfg, "implicitModel": "paid"}
        self.reject("unsupported", nav.config, ts.encoded(cfg))
        cfg = {**self.cfg, "source_roots": ["../escape"]}
        self.reject("unsafe-path", nav.config, ts.encoded(cfg))
        (self.root / "AGENTS.md").write_bytes(b"Required rules.\n" * 5000)
        self.start()
        packet = self.session.next(self.host)["packet"]
        context = packet["requiredContext"][0]
        self.assertEqual(context["contentStatus"], "source-reference")
        self.assertIsNone(context["content"])
        self.assertEqual(context["requiredRead"]["sha256"], digest((self.root / "AGENTS.md").read_bytes()))
        report = self.report(packet)
        report["inspectedPaths"] = [p for p in report["inspectedPaths"] if p != "AGENTS.md"]
        rejected = self.session.submit(self.host, report)
        self.assertEqual(rejected["outcome"], "retry")
        self.assertIn("required context", rejected["packet"]["repair"]["reason"])
        report["inspectedPaths"].append("AGENTS.md")
        self.assertEqual(self.session.submit(self.host, report)["outcome"], "submitted")

    def test_invalid_proposal_never_changes_maps_and_unmanaged_content_preserved(self):
        self.start()
        self.session.submit(self.host, self.report(self.session.next(self.host)["packet"]))
        packet = self.session.next(self.host)["packet"]
        report = self.report(packet)
        proposal = self.record("SRC-bad", packet)
        proposal["text"] = proposal["text"].replace("candidate", "accepted")
        report["proposedRecords"] = [proposal]
        before = {name: self.local.read(path) for name, path in self.session.names.items()}
        self.assertEqual(self.session.submit(self.host, report)["outcome"], "retry")
        self.assertEqual(self.session.submit(self.host, report)["outcome"], "closed-unresolved")
        self.assertEqual(before, {name: self.local.read(path) for name, path in self.session.names.items()})

    def test_native_import_preserves_preambles_accepted_fields_and_unresolved_debt(self):
        bodies = self.session.observe(bootstrap=True)[1]
        bodies["NAVIGATION.md"] = bodies["NAVIGATION.md"].replace(b"- Review: gap", b"- Review: accepted") + b"- Native promise: retain exact bytes\n"
        for name, body in bodies.items():
            (self.root / self.session.names[name]).write_bytes(body)
        self.start()
        self.assertEqual(bodies, self.session.observe()[1])
        packet = self.session.next(self.host)["packet"]
        report = self.report(packet)
        for disposition in report["leadDispositions"]:
            disposition["disposition"] = "unresolved"
        self.session.submit(self.host, report); self.session.submit(self.host, report)
        unresolved = self.session.status()["unresolvedPaths"]
        self.session.run(self.host, self.report, 64)
        self.assertTrue(set(unresolved) <= set(self.session.status()["unresolvedPaths"]))
        self.assertEqual(self.session.observe()[2]["GAP-bootstrap"]["fields"]["Review"], "accepted")
        self.assertEqual(self.session.maintenance()["outcome"], "review-needed")
        self.start("debt-review", "maintenance")
        self.session.run(self.host, self.report, 64)
        self.assertEqual(self.session.maintenance()["outcome"], "no-op")

    def test_configuration_drift_and_maintenance_scope_refusal(self):
        self.start(); self.session.run(self.host, self.report, 64)
        (self.root / "src/db.py").write_bytes(b"def store(data):\n    return {}\n")
        self.start("db-maintenance", "maintenance")
        packet = self.session.next(self.host)["packet"]
        report = self.report(packet)
        report["inspectedPaths"].append("AGENTS.md")
        unrelated = self.record("GAP-bootstrap", packet, "AGENTS.md")
        unrelated["action"] = "revise"
        report["proposedRecords"] = [unrelated]
        before = self.local.read("NAV/NAVIGATION.md")
        self.assertEqual(self.session.submit(self.host, report)["outcome"], "retry")
        self.assertEqual(self.session.submit(self.host, report)["outcome"], "closed-unresolved")
        self.assertEqual(before, self.local.read("NAV/NAVIGATION.md"))
        (self.root / "eutonos.nav.json").write_bytes(ts.encoded({**self.cfg, "source_roots": ["src/db.py"]}))
        self.reject("stale", self.session.next, self.host)

    def test_packaged_native_sweeps_and_templates_are_available(self):
        import hashlib
        from importlib.resources import files
        self.assertEqual(len(nav.SWEEPS), 16)
        git_blob = hashlib.sha1(b"blob " + str(len(nav.SWEEP_BYTES)).encode() + b"\0" + nav.SWEEP_BYTES).hexdigest()
        self.assertEqual(git_blob, "aed3272d4202f687119694f0f37dfabdaed366c2")
        provenance = files("eutonos").joinpath("nav_data/PROVENANCE.txt").read_text()
        self.assertIn("c035ca2d931af99430f975ed75479b754f878d34", provenance)


if __name__ == "__main__":
    unittest.main()
