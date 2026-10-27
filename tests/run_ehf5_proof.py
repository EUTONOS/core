"""Predeclared, source-specific current-agent protocol proof; no extra model/provider."""
from copy import deepcopy
import argparse
import json
import platform
from pathlib import Path
import subprocess
import sys
import time

from eutonos import Hub, GitView, tokenslang as ts, nav, ram, communication as msg, measurement as m
from eutonos.docs import PROFILE as DOCS, plan_adoption, apply_adoption
from eutonos.effects import FixtureAuthority, digest, plan
from eutonos.nav_runner import Session
from test_nav import NavTests

SPEC = Path(__file__).parent / "fixtures/ehf5/spec.json"


def require(value, message):
    if not value: raise AssertionError(message)


class Proof:
    def __init__(self):
        self.spec_bytes = SPEC.read_bytes(); self.spec = ts.loads(self.spec_bytes)
        self.f = NavTests(); self.f.setUp()
        self.root, self.local, self.coordinator = self.f.root, self.f.local, self.f.coordinator
        self.meter, self.timings, self.scenarios = m.Meter(), {}, {}
        self.generation, self.auth = {"A": 1, "B": 1}, {}

    def write(self, path, value):
        target = self.root / path; target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value if isinstance(value, bytes) else ts.encoded(value) + b"\n")

    def host(self, actor, paths):
        old = self.auth.get(actor)
        auth = FixtureAuthority(self.local, "fixture-agent" if actor == "A" else "fixture-reviewer", frozenset(paths) | (old.paths if old else frozenset()), time.time() + 600)
        self.auth[actor] = auth
        return auth

    def attempt(self, actor): return "nav-test" if actor == "A" else "review-test"

    def admit(self, effect, actor="A"):
        auth = self.host(actor, effect["observed"])
        self.coordinator.reserve(auth, self.attempt(actor), self.generation[actor], [c["path"] for c in effect["changes"]])
        return auth

    def effect(self, updates, name, actor="A"):
        value = plan(self.local, updates)
        auth = self.admit(value, actor)
        return self.coordinator.apply(auth, self.attempt(actor), self.generation[actor], name, value)

    def capture(self, store, request, actor="A"):
        value = store.plan(request, "fixture-agent" if actor == "A" else "fixture-reviewer")
        auth = self.admit(value["effect"], actor)
        return ram.apply(self.coordinator, auth, self.attempt(actor), self.generation[actor], value, value["fingerprint"])

    def message(self, board, request, actor="A"):
        value = board.plan(request, "fixture-agent" if actor == "A" else "fixture-reviewer", self.attempt(actor))
        auth = self.admit(value["effect"], actor)
        return msg.apply(self.coordinator, auth, self.attempt(actor), self.generation[actor], value, value["fingerprint"])

    def timed(self, name, call):
        start = time.perf_counter_ns(); result = call(); self.timings[name] = time.perf_counter_ns() - start
        return result

    def reject(self, function, *args):
        try: function(*args)
        except ts.ReadError as exc: return exc.code
        raise AssertionError("Required refusal did not occur")

    def cli(self, module, request, *args):
        result = subprocess.run([sys.executable, "-B", "-m", module, *args], input=ts.encoded(request), capture_output=True, timeout=30)
        require(result.returncode == 0, result.stdout.decode() + result.stderr.decode())
        return ts.loads(result.stdout)

    def seed(self):
        for path, value in self.spec["seed"].items(): self.write(path, value.encode())
        ref = lambda name: {"scope_id": "fixture.scope", "namespace": "fixture.native", "resource_id": name}
        work = {"profile": ts.WORK, "work_id": "work1", "goal": self.spec["task"], "status": "selected",
                "required": [ref("native.vision"), ref("native.rules")],
                "optional": [{"target": {"path": "Docs/HINT.md"}, "why": "The caller is a consequential result path for this predicate change."}]}
        for path, value in (("Docs/WORK.md", work), ("Docs/WORK2.md", {**work, "work_id": "work2", "goal": "Independently preserve a source observation"})):
            self.write(path, b"# Native work\nDocument ID: `" + (b"native.work" if path.endswith("WORK.md") else b"native.work2") + b"`.\n```tokenslang-work\n" + ts.encoded(value) + b"\n```\n")
        cfg = {"repository_id": "fixture.repo", "scope_id": "fixture.scope", "namespace": "fixture.native"}
        self.write("eutonos.read.json", {"profile": ts.CATALOG, **cfg, "owners": {"current_task": ref("native.work")}, "bindings": [],
                                       "excluded_roots": [], "source_paths": ["Docs/HINT.md", "src/auth.py", "src/app.py"]})
        # Stable governing context avoids treating RAM's work-link bookkeeping as source maintenance.
        self.f.cfg["context_paths"] = ["AGENTS.md", "Docs/RULES.md"]
        self.write("eutonos.nav.json", self.f.cfg)
        owners = {"work1": "Docs/WORK.md", "work2": "Docs/WORK2.md"}
        for module, profile, directory in (("ram", ram.CONFIG, "RAM"), ("communication", msg.CONFIG, "messages")):
            self.write("eutonos." + module + ".json", {"profile": profile, **cfg, "store_root": directory, "current_task": "Docs/WORK.md", "work_owners": owners})
        for directory in ("RAM/work1/sessionA", "RAM/work2/sessionB", "messages/work1/thread1", "usage/host1"):
            (self.root / directory).mkdir(parents=True)
        self.write("eutonos.measurement.json", {"profile": m.CONFIG, "scope_id": "fixture.scope", "store_root": "usage", "mode": "metadata",
                                               "retention": "explicit-owner-controls", "export": "explicit-guarded-publication"})
        self.f.session = Session(self.local)
        self.host("A", self.f.session.paths)
        self.host("B", ["Docs/WORK2.md"])
        self.coordinator.start(self.auth["B"], self.attempt("B"))
        self.seed_pin = self.f.commit()
        return {"spec_sha256": digest(self.spec_bytes), "runner_sha256": digest(Path(__file__).read_bytes()), "fixture_commit": self.seed_pin,
                "fixture_tree": self.f.git("rev-parse", "HEAD^{tree}").decode().strip(),
                "thresholds": self.spec["thresholds"], "comparison": self.spec["comparison"], "budget": self.spec["budget"]}

    def provider(self, packet):
        report = self.f.report(packet)
        require(all(any(rule in c["content"] for c in packet["requiredContext"]) for rule in self.spec["required_constraints"]), "NAV packet lost a required constraint")
        records = self.f.session.observe()[2]
        definitions = {
            "SRC-auth": ("Login source family", "The login function evaluates a supplied name.", ["src/auth.py"], "none"),
            "SRC-storage": ("Independent storage source", "store returns a shallow dictionary copy; it is outside the login change.", ["src/db.py"], "none"),
            "SYS-auth": ("Name admission decision", "login decides whether the supplied name is admitted.", ["src/auth.py"], "SRC-auth"),
            "SURF-login": ("Python login callable", "The user argument enters the login callable; this is an API surface, not a GUI.", ["src/auth.py"], "SYS-auth"),
            "INT-submit": ("Caller submits a name", "admit passes name to login and branches on its Boolean result.", ["src/app.py", "src/auth.py"], "SURF-login"),
            "JRN-login": ("Name to allowed or denied", "admit returns allowed when login succeeds, otherwise denied.", ["src/app.py", "src/auth.py"], "INT-submit")}
        chosen = {"P1-source-family-discovery": ["SRC-auth", "SRC-storage"], "P2-capability-system-discovery": ["SYS-auth"],
                  "P3-parent-surface-discovery": ["SURF-login"], "P3-interaction-discovery": ["INT-submit"], "P4-user-journey-discovery": ["JRN-login"]}.get(packet["sweepID"], [])
        if packet["sweepID"] == "M1-affected-review":
            chosen = sorted({rid for lead in packet["leads"] for rid in lead["recordIDs"] if rid in definitions})
        for rid in chosen:
            if rid in records and packet["sweepID"] != "M1-affected-review": continue
            title, summary, evidence, related = definitions[rid]
            for path in evidence:
                source = self.local.read(path)
                require(source is not None, "Mapping evidence absent")
                if path == "src/auth.py": require(b"def login(user):" in source, "Unexpected function source")
                if path == "src/app.py": require(b"if login(name) else 'denied'" in source, "Unexpected caller source")
                if path not in report["inspectedPaths"]: report["inspectedPaths"].append(path)
            if packet["sweepID"] == "M1-affected-review": summary += " Current source rejects empty names via bool(user)."
            kind, owner = nav.KINDS[rid.split("-")[0]]
            text = f"### `{rid}` {title}\n- Kind: {kind}\n- Aliases: {'storage' if rid == 'SRC-storage' else 'login'}\n- Summary: {summary}\n- Evidence: {'; '.join(evidence)}\n- Related: {related}\n- Review: candidate\n- Revision: {packet['sourceFingerprint']}\n- Provenance: current working agent source review; no independent model acceptance\n"
            report["proposedRecords"].append({"recordID": rid, "action": "revise" if rid in records else "add", "mapFile": owner, "evidencePaths": evidence, "text": text})
        report["summary"] = "Source-specific current-agent interpretation replay; callable/caller routes inspected."
        report["omissionSearch"] = "Enumerated the three synthetic source files; storage is unrelated, and this fixture has no GUI/child surface."
        return report

    def nav_cycle(self, name, mode, bound):
        args = ["--fixture", "--coordinator", str(self.coordinator.path), "--actor", "fixture-agent", "--attempt", "nav-test", "--generation", str(self.generation["A"])]
        self.f.cli("start", *args, "--run-id", name, "--mode", mode)
        packets = []
        for _ in range(bound + 1):
            issued = self.f.cli("next", *args)
            if issued["outcome"] != "packet": break
            require(len(packets) < bound, "NAV packet budget exhausted")
            packet = issued["packet"]
            result = self.f.cli("submit", *args, payload=self.provider(packet))
            require(result["outcome"] not in ("retry", "closed-unresolved"), result)
            packets.append({"packet_id": packet["packetID"], "sweep": packet["sweepID"], "bytes": len(ts.encoded(packet))})
        require(issued["outcome"] == "queue-complete", issued)
        return {"closed_sweeps": issued["closedSweeps"], "packets": packets}

    def memory(self, identity, pin, summary, work="Docs/WORK.md", kind="summary", children=None):
        return {"profile": ram.WRITE, "operation": "capture", "operation_id": identity["memory_id"], "identity": identity,
                "expected_revision": None, "at": "2026-09-08T16:00:00Z", "reason": "Useful controlled campaign experience",
                "experience": {"kind": kind, "summary": summary, "recorded_at": "2026-09-08T16:00:00Z", "occurred_at": "2026-09-08T15:00:00Z",
                               "unfinished": False, "actors": [{"id": "fixture-agent" if work.endswith("WORK.md") else "fixture-reviewer", "attribution": "supplied"}],
                               "work_path": work, "source_commit": pin, "sources": [{"path": "src/auth.py", "evidence": "inspected"}], "children": children or [], "skill": None}}

    def run(self):
        start = time.perf_counter_ns()
        before = {p: self.local.read(p) for p in self.spec["seed"]}
        spec = {"profile": DOCS, "repository_id": "fixture.repo", "scope_id": "fixture.scope", "namespace": "fixture.native", "setup_owner": "AGENTS.md",
                "catalog_path": "eutonos.read.json", "manifest_path": "eutonos.docs.json", "owners": {"purpose": {"path": "Docs/VISION.md", "resource_id": "native.vision"},
                "architecture": {"path": "Docs/VISION.md", "resource_id": "native.vision"}, "health": {"path": "Docs/RULES.md", "resource_id": "native.rules"},
                "current_task": {"path": "Docs/WORK.md", "resource_id": "native.work"}}}
        adoption = plan_adoption(self.local, spec); auth = self.admit(adoption["effect"])
        self.timed("docs_adoption_ns", lambda: apply_adoption(self.coordinator, auth, "nav-test", 1, "proof-docs", adoption, adoption["fingerprint"]))
        require(plan_adoption(self.local, spec)["effect"]["changes"] == [], "Docs adoption was not idempotent")
        require(all(self.local.read(p) == b for p, b in before.items() if p != "AGENTS.md"), "Native bytes lost during Docs adoption")
        require(self.local.read("AGENTS.md").startswith(before["AGENTS.md"]), "Native agent rules lost")
        self.scenarios["docs_native_preservation"] = True
        build = self.timed("nav_build_ns", lambda: self.nav_cycle("ehf5-build", "build", self.spec["thresholds"]["max_build_packets"]))
        require(len(build["closed_sweeps"]) == 16, "Incomplete construction sweeps")
        records = self.f.session.observe()[2]
        require(set(self.spec["mapping_quality"]["required_ids"]) <= records.keys(), "Missing source-specific mappings")
        unrelated_map = records["SRC-storage"]["text"]
        ready_pin = self.f.commit(); hub = Hub(GitView(self.root, ready_pin), meter=self.meter)
        orient_request = {"profile": ts.PROFILE, "operation": "orient", "request_id": "proof-pickup", "purpose": "Recover admitted work and required constraints", "max_bytes": 16384}
        baseline = self.timed("baseline_retrieval_ns", lambda: {p: GitView(self.root, ready_pin).read(p)[0] for p in self.spec["comparison"]["selection"]})
        orient = self.timed("hub_orientation_ns", lambda: hub.handle(orient_request))
        direct = self.timed("nav_source_open_ns", lambda: hub.handle({"profile": nav.READ, "operation": "open", "path": "src/auth.py"}))
        require(orient["outcome"] == "ok", orient)
        require(all(rule in ts.encoded(orient).decode() for rule in self.spec["required_constraints"]), "Pickup dropped a required constraint")
        require(orient["data"]["optional"][0]["why"], "Optional context has no relevance")
        require(direct["content"].encode() == baseline["src/auth.py"], "Baseline and Hub source differ")
        candidates = hub.handle({"profile": nav.READ, "operation": "search", "query": self.spec["mapping_quality"]["unfamiliar_phrase"]})
        interpreted = hub.handle({"profile": nav.READ, "operation": "search", "query": "login", "limit": 2})
        require(interpreted["items"] and not interpreted["complete"] and interpreted["continuation"], "Exploration must expose bounded candidates")
        require(not any(self.spec["mapping_quality"]["unfamiliar_phrase"] in r["fields"]["Aliases"] for r in records.values()), "Unfamiliar phrase was preloaded as an alias")
        ambiguous = hub.handle({"profile": nav.READ, "operation": "resolve", "target": {"scope_id": "fixture.scope", "namespace": "fixture.native", "resource_id": "login"}})
        require(ambiguous.get("code") == "ambiguous", "Alias ambiguity was hidden")
        self.scenarios["context_and_candidate_quality"] = {"constraints": 4, "required_ids": sorted(set(records) - {"GAP-bootstrap"}), "phrase_alias_matches": 0, "phrase_lexical_candidates": candidates["total"],
            "interpretation": "Current working agent maps nameless caller to login after reading source; search itself has no semantic model.", "ambiguous_alias_refused": True}
        store, board = ram.Store(self.local), msg.Board(self.local)
        identityA = {"work_id": "work1", "session_id": "sessionA", "issuer_id": "host1", "memory_id": "initial"}
        initial = self.memory(identityA, ready_pin, "Initial source rejects None but permits an empty name; source change is still required.")
        self.capture(store, initial)
        identityB = {"work_id": "work2", "session_id": "sessionB", "issuer_id": "host1", "memory_id": "independent"}
        self.capture(store, self.memory(identityB, ready_pin, "Independent source observation: the caller branches on login; storage remains outside scope.", "Docs/WORK2.md"), "B")
        sibling_path = store.path(identityB); sibling = self.local.read(sibling_path)
        self.coordinator.checkpoint(self.auth["B"], self.attempt("B"), 1, {"work": "work2", "source_commit": ready_pin, "next": "Preserve independent observation"})
        other_checkpoint = self.coordinator.inspect(self.attempt("B"))["checkpoint"]
        self.coordinator.checkpoint(self.auth["A"], self.attempt("A"), 1, {"work": "work1", "source_commit": ready_pin, "next": "Apply bounded empty-name correction"})
        self.host("A", ["src/auth.py"]); self.coordinator.reserve(self.auth["A"], "nav-test", 1, ["src/auth.py"])
        self.host("B", ["src/auth.py"])
        collision = self.reject(self.coordinator.reserve, self.auth["B"], "review-test", 1, ["src/auth.py"])
        frozen_effect = plan(self.local, {"src/auth.py": self.spec["proposed_source"].encode()})
        paused = self.coordinator.pause(self.auth["A"], "nav-test", 1)
        self.generation["A"] = self.coordinator.resume(self.auth["A"], "nav-test", paused)
        stale = self.reject(self.coordinator.apply, self.auth["A"], "nav-test", 1, "old-generation", frozen_effect)
        require(self.coordinator.inspect("review-test")["checkpoint"] == other_checkpoint, "Another lane checkpoint was overwritten")
        self.scenarios["concurrency"] = {"admitted_attempts": 2, "overlap_refusal": collision, "old_generation_refusal": stale, "sibling_checkpoint_preserved": True}
        behavior = "import runpy; login=runpy.run_path('src/auth.py')['login']; assert login(None) is False; assert login('') is False; assert login('Rick') is True"
        initial_check = subprocess.run([sys.executable, "-B", "-c", behavior], cwd=self.root, capture_output=True, timeout=10)
        require(initial_check.returncode != 0, "Initial behavior did not reproduce the admitted bug")
        self.timed("task_effect_ns", lambda: self.effect({"src/auth.py": self.spec["proposed_source"].encode()}, "source-correction"))
        check = subprocess.run([sys.executable, "-B", "-c", behavior], cwd=self.root, capture_output=True, timeout=10)
        require(check.returncode == 0, check.stderr.decode())
        stale_write = self.reject(self.coordinator.apply, self.auth["A"], "nav-test", self.generation["A"], "stale-source-plan", frozen_effect)
        unrelated = subprocess.run([sys.executable, "-B", "legacy_check.py"], cwd=self.root, capture_output=True, timeout=10)
        require(unrelated.returncode != 0 and self.local.read("legacy_check.py") == before["legacy_check.py"], "Unrelated failure expanded the assignment")
        old = store.read({"profile": ram.READ, "operation": "get", "identity": identityA})
        require(old["items"][0]["source_states"][0]["state"] == "changed", "Old RAM became current after source correction")
        self.scenarios["task"] = {"initial_bug_reproduced": True, "passed_behavior_cases": 3, "stale_source_refusal": stale_write, "unrelated_failure_preserved": True}
        impact = self.f.cli("maintenance")
        require("SRC-auth" in impact["affectedRecords"] and "SRC-storage" not in impact["affectedRecords"], "Maintenance scope lost source dependency")
        maintenance = self.timed("nav_maintenance_ns", lambda: self.nav_cycle("ehf5-maintenance", "maintenance", self.spec["thresholds"]["max_maintenance_packets"]))
        require(self.f.session.observe()[2]["SRC-storage"]["text"] == unrelated_map, "Unrelated NAV record was rewritten")
        require(self.f.session.maintenance()["outcome"] == "no-op", "Maintenance did not close the affected view")
        corrected_pin = self.f.commit()
        correction = self.memory(identityA, corrected_pin, "The bounded correction rejects None and empty names, preserves nonempty names and the caller route. Three behavior cases passed; independent acceptance remains separate.")
        correction.update(operation="correct", operation_id="correct-initial", expected_revision=1)
        self.capture(store, correction)
        require(store.plan(correction, "fixture-agent")["effect"]["changes"] == [], "RAM correction retry duplicated")
        require(self.local.read(sibling_path) == sibling, "Independent RAM experience changed")
        milestone_id = {**identityA, "memory_id": "milestone"}
        self.capture(store, self.memory(milestone_id, corrected_pin, "Controlled task checks passed; replacement route is prepared and fresh CLI verification is pending. Retain both independent experiences and native constraints.",
                                       kind="milestone", children=[store.path(identityA), sibling_path]))
        note_identity = {"work_id": "work1", "thread_id": "thread1", "issuer_id": "host1", "message_id": "replacement"}
        note = {"profile": msg.WRITE, "operation": "post", "operation_id": "replacement-note", "identity": note_identity, "expected_revision": None,
                "session": "sessionA", "at": "2026-09-08T16:00:00Z", "content": {"body": "The three empty-name checks pass. Read native WORK/RULES, inspect current auth.py and the RAM milestone; no deployment is authorized.",
                "target": {"kind": "workstream", "id": "work1"}, "reply_to": None, "source_commit": corrected_pin,
                "source_paths": ["src/auth.py"], "occurred_at": "2026-09-08T15:00:00Z"}}
        self.message(board, note)
        pickup_pin = self.f.commit()
        fresh = self.timed("fresh_cli_pickup_ns", lambda: self.cli("eutonos", orient_request, "--root", str(self.root), "--revision", pickup_pin, "--json-request"))
        require(all(rule in ts.encoded(fresh).decode() for rule in self.spec["required_constraints"]), "Fresh process dropped a required constraint")
        notes = self.cli("eutonos.communication_cli", {"profile": msg.READ, "operation": "browse", "work_id": "work1"}, "read", "--root", str(self.root))
        require(notes["items"][0]["content"]["work"]["commit"] == corrected_pin and not notes["items"][0]["actionable_authority"], "Replacement note lost provenance/authority boundary")
        memories = self.cli("eutonos.ram_cli", {"profile": ram.READ, "operation": "get", "identity": milestone_id}, "read", "--root", str(self.root))
        require(len(memories["items"][0]["experience"]["children"]) == 2, "Milestone lost independent trail")
        known = Hub(GitView(self.root, pickup_pin)).handle({"profile": nav.READ, "operation": "open", "path": "src/auth.py"})
        require(known["content"] == self.spec["proposed_source"], "Familiar direct pickup read wrong source")
        confirmed = self.memory(milestone_id, pickup_pin, "Controlled task and fresh CLI pickup checks passed. Both independent experiences and all four native constraints survived. Broader host and independent model acceptance remain unproved.",
                                kind="milestone", children=[store.path(identityA), sibling_path])
        confirmed.update(operation="correct", operation_id="confirm-milestone", expected_revision=1)
        self.capture(store, confirmed)
        self.scenarios["pickup"] = {"fresh_cli_constraints": 4, "milestone_children": 2, "familiar_direct_source": True, "affected_delta": impact,
                                     "environment_change": "new Python CLI processes; independent model comprehension not measured"}
        class Broken:
            events = []; dropped = 0
            def record(self, _): raise OSError("optional telemetry unavailable")
        for meter in (m.Meter(enabled=False), Broken()):
            require(Hub(GitView(self.root, pickup_pin), meter=meter).handle(orient_request) == fresh, "Optional telemetry changed useful work")
        selection = {"scope_id": "fixture.scope", "work_id": "work1", "session_id": "sessionA", "attempt_id": "nav-test", "source_commit": pickup_pin}
        batch = self.meter.snapshot({"issuer_id": "host1", "batch_id": "ehf5"}, selection)
        measure_store = m.Store(self.local); value = measure_store.plan(batch, None, "fixture-agent"); auth = self.admit(value["effect"])
        m.apply(self.coordinator, auth, "nav-test", self.generation["A"], value, value["fingerprint"])
        require(measure_store.plan(batch, None, "fixture-agent")["effect"]["changes"] == [], "Telemetry retry duplicated")
        require(m.summary([batch, batch])["levels"]["entry"]["count"] == len(batch["events"]), "Export was counted twice")
        amount = lambda value: {"amount": value, "unit": "bytes", "boundary": "five selected source/context payloads with each route's returned metadata", "view": ready_pin, "method": "len-utf8"}
        comparison = m.compare(amount(sum(map(len, baseline.values()))), amount(len(ts.encoded(orient)) + len(ts.encoded(direct))))
        self.scenarios["measurement"] = {"entry_calls": len(batch["events"]), "dropped": batch["coverage"]["dropped"], "instrumentation_ns": batch["instrumentation_ns"],
                                         "provider_tokens": None, "duplicate_export_counted_once": True, "optional_failure_preserves_result": True}
        final_pin = self.f.commit()
        selected = ["Docs/WORK.md", "Docs/RULES.md", "src/auth.py", store.path(identityA), sibling_path, store.path(milestone_id), board.path(note_identity), measure_store.path(batch["identity"])]
        published = {"profile": "eutonos.tokenslang.ehf5-published-view.v1", "fixture_commit": final_pin,
                     "qualification": "Explicit synthetic exported source view; no local locks, live peers, signer, independent model or hidden token evidence.",
                     "files": [{"path": p, "sha256": digest(self.local.read(p)), "content": self.local.read(p).decode()} for p in selected]}
        elapsed = time.perf_counter_ns() - start
        require(elapsed / 1e9 <= self.spec["thresholds"]["max_scenario_seconds"], "Scenario duration exceeded its predeclared budget")
        require(all(self.local.read(p) == before[p] for p in ("src/db.py", "src/app.py", "legacy_check.py", "Docs/RULES.md", "Docs/VISION.md")), "Unrelated source/constraints changed")
        return {"profile": "eutonos.tokenslang.ehf5-observed-proof.v1", "spec_sha256": digest(self.spec_bytes), "source_views": {"seed": self.seed_pin, "ready": ready_pin,
                 "corrected": corrected_pin, "pickup": pickup_pin, "published": final_pin}, "environment": {"system": platform.system(), "release": platform.release(), "machine": platform.machine(), "python": platform.python_version()},
                "scenarios": self.scenarios, "build": build, "maintenance": maintenance, "timings_ns": self.timings, "scenario_elapsed_ns": elapsed,
                "payload_comparison": comparison, "accounting_limits": "One ordered warm-capable sample; timings include CLI/fixture adapter overhead where named. Source interpretation/authoring and total model/human time unmeasured. No causal task savings or net benefit.",
                "github_proof": "pending separate exact-view connector observation", "new_model_calls": 0}, published


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--output-dir", required=True); args = parser.parse_args()
    output = Path(args.output_dir); output.mkdir(parents=True, exist_ok=True)
    require(not any(output.iterdir()), "Proof output directory must be new and empty; preserve prior attempts")
    proof = Proof()
    try:
        frozen = proof.seed()
        (output / "frozen.json").write_bytes(ts.encoded(frozen) + b"\n")
        print("Frozen exact fixture and bounds before execution: " + frozen["fixture_commit"], flush=True)
        result, published = proof.run()
        export_start = time.perf_counter_ns()
        (output / "published.json").write_text(json.dumps(published, indent=2) + "\n", newline="\n")
        proof.f.git("bundle", "create", str((output / "fixture.bundle").resolve()), "--all")
        result["export_observation"] = {"duration_ns": time.perf_counter_ns() - export_start,
            "bytes": sum((output / name).stat().st_size for name in ("published.json", "fixture.bundle")),
            "coverage": "Snapshot serialization/write and bundle creation only; excludes this result write, GitHub publication and unobserved authoring."}
        (output / "observed.json").write_text(json.dumps(result, indent=2) + "\n", newline="\n")
        print(json.dumps({"passed": True, "scenario_seconds": result["scenario_elapsed_ns"] / 1e9, "cases": list(result["scenarios"]),
                          "payload_difference_bytes": result["payload_comparison"]["difference"]}), flush=True)
    except Exception as exc:
        (output / "failure.json").write_text(json.dumps({"type": type(exc).__name__, "message": str(exc), "spec_sha256": digest(proof.spec_bytes)}, indent=2) + "\n", newline="\n")
        raise
    finally:
        proof.f.doCleanups()


if __name__ == "__main__": main()
