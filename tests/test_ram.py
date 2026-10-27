"""EHF4R installed persistence, owner controls and source-bound historical reads."""
from copy import deepcopy
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from eutonos import tokenslang as ts, GitView, Hub
from eutonos.effects import Coordinator, FixtureAuthority, LocalRoot, MARKER, MARKER_BYTES
from eutonos import ram


class RamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eutonos-ram-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "fixture"
        self.root.mkdir(); (self.root / "src").mkdir()
        for session in ("session1", "session2"):
            (self.root / "RAM/work1" / session).mkdir(parents=True)
        (self.root / MARKER).write_bytes(MARKER_BYTES)
        (self.root / "src/code.py").write_bytes(b"def example():\n    return 1\n")
        (self.root / "CHECK.md").write_bytes(b"# Fixture verification\nObserved one result in the synthetic example.\n")
        self.work = {"profile": ts.WORK, "work_id": "work1", "goal": "Inspect the synthetic source", "status": "selected", "required": [], "optional": []}
        self.write_work()
        self.cfg = {"profile": ram.CONFIG, "repository_id": "fixture.repo", "scope_id": "fixture.scope", "namespace": "fixture.native",
                    "store_root": "RAM", "current_task": "WORK.md", "work_owners": {"work1": "WORK.md"}}
        (self.root / "eutonos.ram.json").write_bytes(ts.encoded(self.cfg))
        catalog = {"profile": ts.CATALOG, "repository_id": "fixture.repo", "scope_id": "fixture.scope", "namespace": "fixture.native",
                   "owners": {"current_task": {"scope_id": "fixture.scope", "namespace": "fixture.native", "resource_id": "native.work"}},
                   "bindings": [{"resource_id": "native.work", "path": "WORK.md", "kind": "docs", "label": "Native work", "visibility": "public", "state": "active"}],
                   "excluded_roots": [], "source_paths": ["WORK.md", "src/code.py", "CHECK.md"]}
        (self.root / "eutonos.read.json").write_bytes(ts.encoded(catalog))
        self.git("init", "-q", "-b", "main")
        self.pin = self.commit()
        self.local = LocalRoot(self.root)
        self.store = ram.Store(self.local)
        self.coordinator = Coordinator.initialize(self.root.parent / "owner.sqlite")
        self.ident = {"work_id": "work1", "session_id": "session1", "issuer_id": "fixture", "memory_id": "observation1"}
        self.request = {"profile": ram.WRITE, "operation_id": "capture-1", "operation": "capture", "identity": self.ident,
                        "expected_revision": None, "at": "2026-09-08T10:00:00-05:00", "reason": "Useful source-backed observation",
                        "experience": {"kind": "summary", "summary": "The example returns one; inspect source before relying on this later.",
                                       "recorded_at": "2026-09-08T10:00:00-05:00", "occurred_at": "2026-09-07T17:00:00-05:00",
                                       "unfinished": False, "actors": [{"id": "worker-one", "attribution": "supplied"}], "work_path": "WORK.md",
                                       "source_commit": self.pin, "sources": [{"path": "src/code.py", "evidence": "inspected"}],
                                       "children": [], "skill": None}}
        self.auth = None

    def git(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        return subprocess.check_output(["git", "-c", "safe.directory=" + str(self.root), "-c", "core.autocrlf=false",
                                       "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-C", str(self.root), *args],
                                      env=env, stderr=subprocess.PIPE)

    def commit(self):
        self.git("add", "--all"); self.git("commit", "-qm", "Synthetic RAM source")
        return self.git("rev-parse", "HEAD").decode().strip()

    def write_work(self):
        (self.root / "WORK.md").write_bytes(b"# Native work\nPreserve this owner.\n\n```tokenslang-work\n" + ts.encoded(self.work) + b"\n```\n")

    def admit(self, value):
        paths = frozenset(value["effect"]["observed"])
        self.auth = FixtureAuthority(self.local, "recorder", paths | (self.auth.paths if self.auth else frozenset()), time.time() + 600)
        if not hasattr(self, "started"):
            self.coordinator.start(self.auth, "ram-attempt")
            self.started = True
        self.coordinator.reserve(self.auth, "ram-attempt", 1, [c["path"] for c in value["effect"]["changes"]])

    def apply(self, request=None):
        value = self.store.plan(request or self.request, "recorder")
        self.admit(value)
        return ram.apply(self.coordinator, self.auth, "ram-attempt", 1, value, value["fingerprint"])

    def get(self, identity=None, **extras):
        return self.store.read({"profile": ram.READ, "operation": "get", "identity": identity or self.ident, **extras})

    def control(self, state, operation_id="control-1", visibility="public"):
        return {"profile": ram.WRITE, "operation": "control", "operation_id": operation_id, "identity": self.ident,
                "expected_revision": self.store.load(self.store.path(self.ident))["revision"],
                "at": "2026-09-08T11:00:00-05:00", "reason": "Owner control", "controls": {"state": state, "visibility": visibility, "expires_at": None}}

    def correction(self, summary="Corrected: this is a fixture, not a product conclusion."):
        return {**deepcopy(self.request), "operation": "correct", "operation_id": "correct-1",
                "expected_revision": self.store.load(self.store.path(self.ident))["revision"],
                "experience": {**deepcopy(self.request["experience"]), "summary": summary}}

    def reject(self, code, func, *args, **kwargs):
        with self.assertRaises(ts.ReadError) as error:
            func(*args, **kwargs)
        self.assertEqual(error.exception.code, code)

    def cli(self, command, payload, *args):
        run = subprocess.run([sys.executable, "-m", "eutonos.ram_cli", command, "--root", str(self.root), *args],
                             input=ts.encoded(payload), capture_output=True, timeout=30)
        value = ts.loads(run.stdout.decode())
        self.assertEqual(run.returncode, 0, value)
        return value

    def test_round_trip_native_links_real_cli_and_retransmission(self):
        original = self.local.read("WORK.md")
        value = self.cli("plan", self.request, "--actor", "recorder")
        self.assertEqual(original, self.local.read("WORK.md"))
        self.admit(value)
        applied = self.cli("apply", value, "--fixture", "--actor", "recorder", "--attempt", "ram-attempt",
                           "--coordinator", str(self.coordinator.path), "--fingerprint", value["fingerprint"])
        self.assertEqual(applied["outcome"], "completed")
        read = self.cli("read", {"profile": ram.READ, "operation": "get", "identity": self.ident})
        experience = read["items"][0]["experience"]
        self.assertEqual(experience["summary"], self.request["experience"]["summary"])
        self.assertEqual(experience["creator"]["id"], "recorder")
        self.assertEqual(experience["actors"][0]["id"], "worker-one")
        self.assertFalse(read["items"][0]["current_task_changed"])
        self.assertTrue(self.local.read("WORK.md").startswith(original))
        self.assertIn(self.store.path(self.ident).encode(), self.local.read("WORK.md"))
        repeated = self.store.plan(self.request, "recorder")
        self.assertEqual(repeated["effect"]["changes"], [])
        before = self.coordinator.path.read_bytes()
        self.assertEqual(ram.apply(self.coordinator, self.auth, "ram-attempt", 1, repeated, repeated["fingerprint"])["writes"], 0)
        self.assertEqual(before, self.coordinator.path.read_bytes())
        changed = deepcopy(self.request); changed["experience"]["summary"] = "Different logical capture"
        self.reject("conflict", self.store.plan, changed, "recorder")

    def test_new_configured_owner_uses_flat_path_without_creating_nested_directories(self):
        for session in ("session1", "session2"):
            (self.root / "RAM/work1" / session).rmdir()
        (self.root / "RAM/work1").rmdir()
        path = self.store.path(self.ident)
        self.assertTrue(path.startswith("RAM/record-"))
        self.assertFalse((self.root / "RAM/work1").exists())
        planned = self.store.plan(self.request, "recorder")
        self.assertEqual(planned["record_path"], path)
        self.assertFalse((self.root / "RAM/work1").exists())
        self.reject("unsupported", self.local.read, "RAM/work1/session1/legacy.json")
        self.apply()
        self.assertTrue((self.root / path).is_file())
        self.assertFalse((self.root / "RAM/work1").exists())

    def test_existing_flat_and_nested_paths_remain_stable(self):
        self.apply()
        flat = self.store.path(self.ident)
        self.assertTrue(flat.startswith("RAM/record-"))
        self.assertEqual(self.store.plan(self.correction(), "recorder")["record_path"], flat)
        nested = "RAM/work1/session1/7-fixture--observation1.json"
        (self.root / flat).rename(self.root / nested)
        self.assertEqual(self.store.path(self.ident), nested)
        self.assertEqual(self.store.plan(self.correction(), "recorder")["record_path"], nested)
        self.assertEqual(self.get()["items"][0]["path"], nested)

    def test_dual_layout_identity_conflict_preserves_both_files(self):
        self.apply()
        flat = self.store.path(self.ident)
        correction = self.correction()
        nested = "RAM/work1/session1/7-fixture--observation1.json"
        original = (self.root / flat).read_bytes()
        (self.root / nested).write_bytes(original)
        self.reject("conflict", self.store.path, self.ident)
        self.reject("conflict", self.store.plan, correction, "recorder")
        self.reject("conflict", self.get)
        self.assertEqual((self.root / flat).read_bytes(), original)
        self.assertEqual((self.root / nested).read_bytes(), original)

    def test_scoped_browse_reads_flat_and_nested_then_filters_identity(self):
        self.apply()
        nested_request = deepcopy(self.request)
        nested_request["identity"]["memory_id"] = "legacy"
        nested_request["operation_id"] = "legacy"
        self.apply(nested_request)
        legacy_flat = self.store.path(nested_request["identity"])
        legacy_nested = "RAM/work1/session1/7-fixture--legacy.json"
        (self.root / legacy_flat).rename(self.root / legacy_nested)
        other_session = deepcopy(self.request)
        other_session["identity"]["session_id"] = "session2"
        other_session["operation_id"] = "other-session"
        self.apply(other_session)
        selected = self.store.read({"profile": ram.READ, "operation": "browse",
                                    "work_id": "work1", "session_id": "session1"})["items"]
        self.assertEqual({item["path"] for item in selected}, {self.store.path(self.ident), legacy_nested})
        self.assertEqual({item["identity"]["memory_id"] for item in selected}, {"observation1", "legacy"})
        only_second = self.store.read({"profile": ram.READ, "operation": "browse", "session_id": "session2"})["items"]
        self.assertEqual(len(only_second), 1)
        self.assertEqual(only_second[0]["identity"]["session_id"], "session2")

    def test_scoped_browse_skips_only_proven_unrelated_corrupt_flat_record(self):
        self.apply()
        foreign = {**self.ident, "session_id": "other-session", "memory_id": "broken"}
        foreign_path = self.store.path(foreign)
        (self.root / foreign_path).write_bytes(ts.encoded({"identity": foreign, "profile": "invalid"}))
        selected = self.store.read({"profile": ram.READ, "operation": "browse",
                                    "work_id": "work1", "session_id": "session1"})["items"]
        self.assertEqual([item["path"] for item in selected], [self.store.path(self.ident)])

        matching = {**self.ident, "memory_id": "matching-broken"}
        matching_path = self.store.path(matching)
        (self.root / matching_path).write_bytes(ts.encoded({"identity": matching, "profile": "invalid"}))
        self.reject("invalid", self.store.read, {"profile": ram.READ, "operation": "browse",
                                                 "work_id": "work1", "session_id": "session1"})

    def test_independent_sessions_same_text_survive_and_no_automatic_milestone(self):
        self.apply()
        second = deepcopy(self.request)
        second["identity"]["session_id"] = "session2"; second["operation_id"] = "independent"
        self.apply(second)
        records = self.store.read({"profile": ram.READ, "operation": "browse", "work_id": "work1"})["items"]
        self.assertEqual(len(records), 2)
        self.assertEqual({r["identity"]["session_id"] for r in records}, {"session1", "session2"})
        self.assertTrue(all(r["experience"]["kind"] == "summary" for r in records))
        self.assertEqual(self.get()["items"][0]["experience"]["summary"], records[0]["experience"]["summary"])
        unrelated = self.root / "RAM/other-work/session1"
        unrelated.mkdir(parents=True)
        (unrelated / "future-record.json").write_bytes(b'{"profile":"unsupported-other-work"}')
        scoped = self.store.read({"profile": ram.READ, "operation": "browse", "work_id": "work1", "session_id": "session2"})
        self.assertEqual(len(scoped["items"]), 1)

    def test_hub_pickup_old_memory_does_not_select_work_and_source_move(self):
        self.apply()
        self.work["work_id"] = "work2"; self.work["goal"] = "A different admitted task"
        self.write_work()
        (self.root / "src/code.py").rename(self.root / "src/moved.py")
        pin = self.commit()
        hub = Hub(GitView(self.root, pin))
        memory = hub.handle({"profile": ram.READ, "operation": "get", "identity": self.ident})
        self.assertTrue(memory["items"][0]["current_task_changed"])
        self.assertEqual(memory["items"][0]["source_states"][0]["state"], "missing")
        self.assertEqual(memory["items"][0]["experience"]["sources"][0]["commit"], self.pin)
        orient = hub.handle({"profile": ts.PROFILE, "operation": "orient", "request_id": "pickup", "purpose": "Select admitted work", "max_bytes": 16384})
        self.assertEqual(orient["outcome"], "ok")
        self.assertIn("work2", ts.encoded(orient["data"]).decode())

    def test_late_summary_queries_use_event_time_not_recording_time(self):
        self.apply()
        query = {"profile": ram.READ, "operation": "browse", "from": "2026-09-07T00:00:00-05:00", "to": "2026-09-08T00:00:00-05:00"}
        self.assertEqual(len(self.store.read(query)["items"]), 1)
        self.assertEqual(self.store.read({**query, "time_field": "recorded_at"})["items"], [])
        late = deepcopy(self.request); late["identity"]["memory_id"] = "unknown-event"; late["operation_id"] = "unknown-event"
        late["experience"]["occurred_at"] = None
        self.apply(late)
        self.assertEqual(len(self.store.read(query)["items"]), 1)

    def test_correction_history_suppression_and_forgetting_cannot_resurrect(self):
        self.apply(); self.apply(self.correction())
        self.assertEqual(self.get(revision=1)["items"][0]["experience"]["summary"], self.request["experience"]["summary"])
        self.assertTrue(self.get(revision=1)["items"][0]["historical"])
        self.apply(self.control("suppressed"))
        self.assertNotIn(self.store.path(self.ident).encode(), self.local.read("WORK.md"))
        self.reject("denied", self.get, revision=1)
        self.assertEqual(self.store.read({"profile": ram.READ, "operation": "browse", "include_archived": True})["items"], [])
        forget = {k: v for k, v in self.control("suppressed", "forget-1").items() if k != "controls"}
        forget["operation"] = "forget"
        self.apply(forget)
        self.assertEqual(self.store.load(self.store.path(self.ident))["versions"], [])
        self.reject("denied", self.get)
        self.reject("denied", self.store.plan, self.control("active", "restore"), "recorder")
        self.reject("denied", self.get, revision=1)

    def test_archive_is_explicit_and_unfinished_context_is_protected(self):
        request = deepcopy(self.request); request["experience"]["unfinished"] = True
        self.apply(request)
        self.reject("denied", self.store.plan, self.control("archived"), "recorder")
        self.apply(self.correction())
        self.apply(self.control("archived", "archive-completed"))
        self.reject("archived", self.get)
        self.assertEqual(self.get(include_archived=True)["items"][0]["controls"]["state"], "archived")

    def test_milestone_child_correction_and_suppression_block_derived_summary(self):
        self.apply()
        child_path = self.store.path(self.ident)
        milestone = deepcopy(self.request); milestone["identity"]["memory_id"] = "milestone1"; milestone["operation_id"] = "milestone"
        milestone["experience"].update(kind="milestone", children=[child_path], summary="Fixture campaign experience trail")
        self.apply(milestone)
        self.assertEqual(self.get(milestone["identity"])["items"][0]["experience"]["children"][0]["path"], child_path)
        self.assertEqual(self.get(milestone["identity"])["items"][0]["experience"]["children"][0]["identity"], self.ident)
        self.apply(self.correction())
        self.reject("stale", self.get, milestone["identity"])
        self.apply(self.control("suppressed"))
        self.reject("denied", self.get, milestone["identity"])
        self.assertEqual(self.store.read({"profile": ram.READ, "operation": "browse"})["items"], [])

    def test_stale_plan_conflicting_corrections_and_foreign_actor_preserve_bytes(self):
        value = self.store.plan(self.request, "recorder"); self.admit(value)
        self.work["goal"] = "New work while capture was pending"; self.write_work()
        self.reject("stale", ram.apply, self.coordinator, self.auth, "ram-attempt", 1, value, value["fingerprint"])
        self.assertIsNone(self.local.read(self.store.path(self.ident)))
        self.apply()
        correction = self.store.plan(self.correction(), "recorder"); self.admit(correction)
        foreign = FixtureAuthority(self.local, "foreign", self.auth.paths, time.time() + 100)
        self.reject("conflict", ram.apply, self.coordinator, foreign, "ram-attempt", 1, correction, correction["fingerprint"])
        self.apply(self.control("active", visibility="private"))
        self.reject("stale", ram.apply, self.coordinator, self.auth, "ram-attempt", 1, correction, correction["fingerprint"])

    def test_budget_cursor_private_sources_and_read_zero_writes(self):
        self.apply()
        second = deepcopy(self.request); second["identity"]["memory_id"] = "another"; second["operation_id"] = "another"
        self.apply(second)
        before = self.coordinator.path.read_bytes()
        query = {"profile": ram.READ, "operation": "browse", "limit": 1}
        first = self.store.read(query)
        self.assertFalse(first["complete"])
        self.assertEqual(len(self.store.read({**query, "cursor": first["continuation"]})["items"]), 1)
        overflow = self.get(max_bytes=512)
        self.assertEqual(overflow["outcome"], "overflow")
        self.assertFalse(overflow["complete"])
        self.assertEqual(self.coordinator.path.read_bytes(), before)
        self.apply(self.control("suppressed"))
        self.reject("stale", self.store.read, {**query, "cursor": first["continuation"]})
        restricted = ram.Store(self.local, denied_paths=["src/code.py"])
        self.assertEqual(restricted.read({"profile": ram.READ, "operation": "browse"})["items"], [])

    def test_unknown_profiles_times_unpinned_sources_and_nested_paths_refused(self):
        self.reject("unsupported", self.store.plan, {**self.request, "profile": "future"}, "recorder")
        bad = deepcopy(self.request); bad["experience"]["recorded_at"] = "2026-09-08T10:00:00"
        self.reject("invalid", self.store.plan, bad, "recorder")
        bad = deepcopy(self.request); bad["experience"]["source_commit"] = "HEAD"
        self.reject("invalid", self.store.plan, bad, "recorder")
        bad = deepcopy(self.request); bad["identity"]["memory_id"] = "../escape"
        self.reject("invalid", self.store.plan, bad, "recorder")

    def test_skill_binding_is_source_pinned_and_not_execution(self):
        request = deepcopy(self.request)
        request["experience"].update(kind="skill-binding", sources=[{"path": "CHECK.md", "evidence": "supplied"}],
                                      skill={"path": "CHECK.md", "verification_path": "CHECK.md", "required_tools": ["python"],
                                             "constraints": "Requires actual host permission; this record is not a grant."})
        self.apply(request)
        record = self.get()
        self.assertEqual(record["authorization"], "none; consult native Current Task")
        self.assertEqual(record["items"][0]["experience"]["skill"]["path"], "CHECK.md")

    def test_interrupted_record_and_native_link_effect_recovers_together(self):
        value = self.store.plan(self.request, "recorder"); self.admit(value)
        original = self.local.replace
        calls = 0
        def interrupt(name, before, body):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("Fixture interruption between attributed files")
            original(name, before, body)
        work_before = self.local.read("WORK.md")
        with patch.object(self.local, "replace", side_effect=interrupt):
            with self.assertRaises(RuntimeError):
                ram.apply(self.coordinator, self.auth, "ram-attempt", 1, value, value["fingerprint"])
        effect = self.coordinator.inspect("ram-attempt")["unfinished_effects"][0]["id"]
        self.assertEqual(self.coordinator.reconcile(self.auth, "ram-attempt", 1, effect)["outcome"], "partial")
        self.assertEqual(self.coordinator.rollback(self.auth, "ram-attempt", 1, effect)["outcome"], "rolled-back")
        self.assertIsNone(self.local.read(self.store.path(self.ident)))
        self.assertEqual(self.local.read("WORK.md"), work_before)

    def test_suppression_during_read_refuses_the_projection(self):
        self.apply()
        path = self.store.path(self.ident)
        original = self.local.read
        calls = 0
        def changed(name):
            nonlocal calls
            if name == path:
                calls += 1
                if calls == 3:
                    record = ram.unpack(original(path))
                    record["controls"]["state"] = "suppressed"
                    (self.root / path).write_bytes(ts.encoded(record))
            return original(name)
        with patch.object(self.local, "read", side_effect=changed):
            self.reject("stale", self.get)


if __name__ == "__main__":
    unittest.main()
