import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from eutonos import ram, tokenslang as ts
from eutonos.effects import LocalRoot
from eutonos.host import connect, enroll

from eutonos import companion_ram_capture as capture


class CompanionRamCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="companion-ram-capture-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.root = self.home / "project"; self.root.mkdir()
        (self.root / "RAM/work/session").mkdir(parents=True)
        (self.root / ".eutonos-local").mkdir()
        self.work = {"profile": ts.WORK, "work_id": "work", "goal": "Fixture capture", "status": "selected",
                     "required": [], "optional": []}
        (self.root / "WORK.md").write_bytes(b"# Work\n\n```tokenslang-work\n" + ts.encoded(self.work) + b"\n```\n")
        (self.root / "source.md").write_text("Observed source\n", encoding="utf-8")
        config = {"profile": ram.CONFIG, "repository_id": "fixture.repo", "scope_id": "fixture.scope",
                  "namespace": "fixture.native", "store_root": "RAM", "current_task": "WORK.md",
                  "work_owners": {"work": "WORK.md"}}
        (self.root / "eutonos.ram.json").write_bytes(ts.encoded(config))
        catalog = {"profile": ts.CATALOG, "repository_id": "fixture.repo", "scope_id": "fixture.scope",
                   "namespace": "fixture.native", "owners": {"current_task": {"scope_id": "fixture.scope",
                   "namespace": "fixture.native", "resource_id": "fixture.work"}}, "bindings": [],
                   "excluded_roots": [], "source_paths": ["WORK.md", "source.md"]}
        (self.root / "eutonos.read.json").write_bytes(ts.encoded(catalog))
        self.git("init", "-q", "-b", "main"); self.git("add", "--all")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "baseline")
        self.pin = self.git("rev-parse", "HEAD").strip()
        self.identity = {"work_id": "work", "session_id": "session", "issuer_id": "helper", "memory_id": "finding"}
        self.semantic = {"profile": ram.WRITE, "operation": "capture", "identity": self.identity,
                         "expected_revision": None, "reason": "Bounded fixture finding",
                         "experience": {"kind": "summary", "summary": "The fixture source was inspected.",
                         "occurred_at": "2026-09-13T02:00:00Z", "unfinished": False,
                         "actors": [{"id": "helper", "attribution": "self-reported"}], "work_path": "WORK.md",
                         "source_commit": self.pin, "sources": [{"path": "source.md", "evidence": "inspected"}],
                         "children": [], "skill": None}}
        store = ram.Store(LocalRoot(self.root))
        self.record_path = store.path(self.identity)
        self.allowed = [self.record_path, "WORK.md"]
        self.host = self.home / "host"
        enroll(self.root, self.host, "actor", self.allowed)
        self.artifact = self.root / ".eutonos-local" / "capture.json"

    def git(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        return subprocess.check_output(["git", "-c", "safe.directory=" + str(self.root), "-C", str(self.root),
                                        *args], env=env, text=True, stderr=subprocess.PIPE)

    def run_capture(self, attempt="new-attempt", allowed=None):
        return capture.capture(self.root, self.host, "actor", attempt, self.semantic,
                               allowed or self.allowed, self.artifact, "2026-09-13T02:01:02Z")

    def configure_delivery(self):
        from eutonos.git_delivery import Engine, CONFIG
        import sys
        self.git('config','user.name','ASW4 Fixture');self.git('config','user.email','asw4@example.invalid')
        remote = self.home / 'remote.git'
        self.git('init','--bare',str(remote));self.git('push',str(remote),'HEAD:refs/heads/main')
        engine=Engine(self.root)
        engine.configure({'profile':CONFIG,'project_id':'fixture','participant':'participant','mode':'commit-only',
            'remote_url':str(remote),'remote_ref':'refs/heads/main','branch':'refs/heads/main',
            'records_ref':'refs/heads/records/asw4','canonical_base':self.pin,
            'allowed_paths':['RAM/','WORK.md'],'checks':[[sys.executable,'-c',"from pathlib import Path; assert Path('WORK.md').is_file()"]]})
        return engine

    def test_automatic_ram_and_terminal_receipt_gap_share_original_identity(self):
        engine=self.configure_delivery()
        first=self.run_capture()
        self.assertEqual(first['git_delivery']['state'],'committed',first)
        from tests.test_work_lifecycle import report_bounds
        for job in engine.status():report_bounds(self.root,'native-ram-'+job['payload']['kind'],job['payload']['changes'],job['payload']['source_parent'])
        _, result_path=capture.artifact_paths(self.root,self.artifact)
        result_path.unlink()
        with mock.patch.object(LocalRoot,'replace',side_effect=AssertionError('native replay')):
            recovered=self.run_capture()
        self.assertEqual(recovered['git_delivery']['commit_oid'],first['git_delivery']['commit_oid'])
        self.assertEqual(len(engine.status()),1)

    def test_automatic_interrupted_ram_recovers_without_reapply(self):
        engine=self.configure_delivery()
        class Crash(BaseException): pass
        with mock.patch.object(capture,'verify_plan_readback',side_effect=Crash):
            with self.assertRaises(Crash):self.run_capture()
        with mock.patch.object(LocalRoot,'replace',side_effect=AssertionError('native replay')):
            recovered=self.run_capture()
        self.assertEqual(recovered['outcome'],'completed',recovered)
        self.assertEqual(recovered['git_delivery']['state'],'committed',recovered)
        self.assertEqual(len(engine.status()),1)

    def test_frozen_request_tampering_is_refused_before_reuse(self):
        self.assertEqual(self.run_capture()["outcome"], "completed")
        artifact = json.loads(self.artifact.read_text(encoding="utf-8"))
        artifact["request"]["experience"]["summary"] = "Unselected changed content"
        self.artifact.write_text(json.dumps(artifact), encoding="utf-8")
        with self.assertRaises(ts.ReadError):
            self.run_capture()

    def test_existing_attempt_outside_inventory_page_is_detected_before_artifact(self):
        owner, authority = connect(self.root, self.host, "actor")
        owner.start(authority, "old")
        owner.pause(authority, "old", 1)
        original = capture.status
        def paged(root, host, actor, attempt=None):
            result = original(root, host, actor, attempt)
            if attempt is None:
                result["attempts"] = []
                result["attempts_truncated"] = True
            return result
        with mock.patch.object(capture, "status", side_effect=paged):
            result = self.run_capture(attempt="old")
        self.assertEqual(result["code"], "conflict")
        self.assertFalse(self.artifact.exists())
        self.assertEqual(owner.inspect("old")["state"], "paused")

    def test_success_and_terminal_retry_reverify_current_state(self):
        result = self.run_capture()
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(result["capture"]["revision"], 1)
        self.assertEqual(result["host"]["reservations"], [])
        frozen = json.loads(self.artifact.read_text(encoding="utf-8"))
        self.assertEqual(frozen["request"]["at"], frozen["request"]["experience"]["recorded_at"])
        self.assertTrue(frozen["request"]["operation_id"].startswith("companion-"))
        retry = self.run_capture()
        self.assertEqual(retry["outcome"], "already-completed")
        self.assertTrue(retry["current_verified"])

    def test_denied_scope_has_no_project_host_or_artifact_mutation(self):
        before_host = (self.host / "coordinator.sqlite").read_bytes()
        before_work = (self.root / "WORK.md").read_bytes()
        result = self.run_capture(allowed=[self.record_path])
        self.assertEqual(result["code"], "denied")
        self.assertFalse(self.artifact.exists())
        self.assertEqual((self.host / "coordinator.sqlite").read_bytes(), before_host)
        self.assertEqual((self.root / "WORK.md").read_bytes(), before_work)

    def test_existing_clean_paused_attempt_is_not_resumed(self):
        owner, authority = connect(self.root, self.host, "actor")
        owner.start(authority, "old"); paused_generation = owner.pause(authority, "old", 1)
        result = self.run_capture(attempt="old")
        self.assertEqual(result["code"], "conflict")
        state = owner.inspect("old")
        self.assertEqual((state["state"], state["generation"]), ("paused", paused_generation))
        self.assertFalse(self.artifact.exists())

    def test_stale_source_preserves_external_content_and_reports_protection(self):
        original_apply = capture.ram.apply
        def change_then_apply(*args, **kwargs):
            (self.root / "WORK.md").write_text("Concurrent owner change\n", encoding="utf-8")
            return original_apply(*args, **kwargs)
        with mock.patch.object(capture.ram, "apply", side_effect=change_then_apply):
            result = self.run_capture()
        self.assertEqual(result["code"], "stale")
        self.assertEqual((self.root / "WORK.md").read_text(encoding="utf-8"), "Concurrent owner change\n")
        self.assertEqual(result["attempt"]["state"], "active")
        self.assertTrue(result["attempt"]["reservations"])
        self.assertFalse(Path(str(self.artifact) + ".result.json").exists())

    def test_identity_conflict_and_existing_artifact_mismatch_are_nonmutating(self):
        wrong = capture.capture(self.root, self.host, "other", "new-attempt", self.semantic,
                                self.allowed, self.artifact, "2026-09-13T02:01:02Z")
        self.assertEqual(wrong["code"], "wrong-scope")
        self.assertFalse(self.artifact.exists())
        self.artifact.write_text(json.dumps({"profile": capture.PROFILE, "binding_id": "other"}), encoding="utf-8")
        with self.assertRaises(ts.ReadError) as caught:
            self.run_capture()
        self.assertEqual(caught.exception.code, "conflict")

    def test_readback_mismatch_never_closes_or_writes_terminal_result(self):
        original = capture.ram.Store.read
        def wrong_read(store, request):
            value = original(store, request)
            value["items"][0]["record_sha256"] = "0" * 64
            return value
        with mock.patch.object(capture.ram.Store, "read", new=wrong_read):
            result = self.run_capture()
        self.assertEqual(result["code"], "stale")
        self.assertFalse(Path(str(self.artifact) + ".result.json").exists())
        self.assertEqual(result["attempt"]["state"], "active")
        self.assertTrue(result["attempt"]["reservations"])

    def test_frozen_artifact_without_started_attempt_resumes_once(self):
        request = capture.freeze_request(self.semantic, "2026-09-13T02:01:02Z")
        plan = ram.Store(LocalRoot(self.root)).plan(request, "actor")
        binding = {"root": str(self.root), "host": str(self.host), "actor": "actor",
                   "attempt": "new-attempt", "allowed_paths": sorted(self.allowed), "semantic": self.semantic}
        artifact = {"profile": capture.PROFILE,
                    "binding_id": capture.hashlib.sha256(capture.canonical(binding).encode("utf-8")).hexdigest(),
                    "root": str(self.root), "host": str(self.host), "actor": "actor", "attempt": "new-attempt",
                    "allowed_paths": sorted(self.allowed), "request": request, "plan": plan}
        capture.write_new(self.artifact, artifact)
        result = self.run_capture()
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(result["capture"]["revision"], 1)

    def test_interrupted_effect_remains_protected_with_exact_recovery_state(self):
        original = capture.LocalRoot.replace
        calls = 0
        def interrupt(instance, path, before, body):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated interruption")
            return original(instance, path, before, body)
        with mock.patch.object(capture.LocalRoot, "replace", new=interrupt):
            result = self.run_capture()
        self.assertEqual(result["outcome"], "error")
        self.assertEqual(result["attempt"]["unfinished_effects"][0]["state"], "prepared")
        self.assertIn("reconcile", result["recovery"].lower())
        retry = self.run_capture()
        self.assertEqual(retry["attempt"]["unfinished_effects"][0]["state"], "prepared")
        self.assertFalse(Path(str(self.artifact) + ".result.json").exists())

    def test_historical_host_expiry_does_not_block_record_capture(self):
        admission = self.host / "admission.json"
        value = json.loads(admission.read_text(encoding="utf-8")); value["expires_at"] = 0
        admission.write_bytes(ts.encoded(value) + b"\n")
        result = self.run_capture()
        self.assertEqual(result["outcome"], "completed")
        self.assertTrue(result["current_verified"])
        self.assertTrue(self.artifact.exists())


if __name__ == "__main__":
    unittest.main()
