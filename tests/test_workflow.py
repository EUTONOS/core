import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from eutonos import capture_policy, ram, settings, tokenslang as ts
from eutonos.effects import LocalRoot
from eutonos.host import enroll
from eutonos.workflow import catalog, execute_closeout, pickup, prepare_closeout, validate_closeout


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workflow-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        environment = mock.patch.dict(os.environ, {"EUTONOS_HOME": str(self.home / "eutonos-home")})
        environment.start()
        self.addCleanup(environment.stop)
        self.root = self.home / "project"; self.root.mkdir()
        (self.root / "RAM/work/session").mkdir(parents=True)
        (self.root / ".eutonos-local").mkdir()
        work = {"profile": ts.WORK, "work_id": "work", "goal": "Fixture closeout", "status": "selected",
                "required": [], "optional": []}
        (self.root / "WORK.md").write_bytes(b"# Work\n\n```tokenslang-work\n" + ts.encoded(work) + b"\n```\n")
        (self.root / "changed.py").write_text("before = True\n", encoding="utf-8")
        config = {"profile": ram.CONFIG, "repository_id": "fixture.repo", "scope_id": "fixture.scope",
                  "namespace": "fixture.native", "store_root": "RAM", "current_task": "WORK.md",
                  "work_owners": {"work": "WORK.md"}}
        (self.root / "eutonos.ram.json").write_bytes(ts.encoded(config))
        self.git("init", "-q", "-b", "main"); self.git("add", "--all")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "baseline")
        (self.root / "changed.py").write_text("before = False\n", encoding="utf-8")
        self.host = self.home / "host"
        self.context = {"root": str(self.root), "host": str(self.host), "actor": "writer",
                        "work_id": "work", "session_id": "session"}
        self.values = {"what": "Changed the fixture.", "why": "Exercise closeout.",
                       "checks": ["fixture check passed"], "unresolved": [], "changed_paths": ["changed.py"]}
        self.new_values = {"request": "Change the fixture.", "actions": ["Updated changed.py."],
                           "result": "The fixture now uses the new value.",
                           "why": "changed.py implements the requested value.", "checks": ["fixture check passed"],
                           "unresolved": [], "changed_paths": ["changed.py"]}

    def git(self, *args):
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        return subprocess.check_output(["git", "-c", "safe.directory=" + str(self.root), "-C", str(self.root),
                                        *args], env=env, text=True, stderr=subprocess.PIPE)

    def configure_delivery(self, checks=None, mode='commit-only'):
        from eutonos.git_delivery import Engine, CONFIG
        import sys
        self.git('config', 'user.name', 'ASW4 Fixture'); self.git('config','user.email','asw4@example.invalid')
        remote = self.home / 'remote.git'
        self.git('init','--bare',str(remote)); self.git('push',str(remote),'HEAD:refs/heads/main')
        engine = Engine(self.root)
        engine.configure({'profile':CONFIG,'project_id':'fixture','participant':'participant','mode':mode,
            'remote_url':str(remote),'remote_ref':'refs/heads/main','branch':'refs/heads/main',
            'records_ref':'refs/heads/records/asw4','canonical_base':self.git('rev-parse','HEAD').strip(),
            'allowed_paths':['changed.py','RAM/','WORK.md'],
            'checks': checks or [[sys.executable,'-c',"from pathlib import Path; compile(Path('changed.py').read_text(),'changed.py','exec')"]]})
        return engine

    def test_explicit_checked_code_closeout_freezes_scope_and_runs_engine_checks(self):
        engine = self.configure_delivery()
        context = {**self.context,'delivery':{'logical_id':'checked-closeout','paths':['changed.py']}}
        prepared = prepare_closeout(self.values, context)
        self.assertEqual(prepared['outcome'],'prepared',prepared)
        self.assertEqual(engine.status()[0]['state'],'waiting-native')
        enroll(self.root,self.host,'writer',prepared['allowed_paths'])
        result = execute_closeout(prepared, context)
        self.assertEqual(result['outcome'],'completed',result)
        self.assertEqual(result['git_delivery']['state'],'committed',result)
        jobs = engine.status(); self.assertEqual(len(jobs),2) # separate RAM and code scopes
        self.assertEqual({tuple(sorted(j['payload']['changes'])) for j in jobs},
                         {('changed.py',),tuple(sorted(prepared['allowed_paths']))})
        self.assertTrue(all(j['checks'] for j in jobs))
        from tests.test_work_lifecycle import report_bounds
        for job in engine.status():report_bounds(self.root,'ram-code-closeout-'+job['payload']['kind'],job['payload']['changes'],job['payload']['source_parent'])
        retry = execute_closeout(prepared,context)
        self.assertEqual(retry['git_delivery']['commit_oid'],result['git_delivery']['commit_oid'])
        self.assertEqual(len(engine.status()),2) # receipt/internal files do not loop

    def test_prepared_code_cannot_run_or_recover_before_native_closeout(self):
        import io
        from contextlib import redirect_stdout
        from eutonos.git_delivery_cli import main
        from eutonos.git_delivery import DeliveryError
        engine=self.configure_delivery()
        context={**self.context,'delivery':{'logical_id':'waiting-native','paths':['changed.py']}}
        prepared=prepare_closeout(self.values,context)
        key=engine.status()[0]['id']
        self.assertEqual(engine.run(key)['state'],'waiting-native')
        with self.assertRaises(DeliveryError):engine.resume(key)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--root',str(self.root),'recover',key]),1)
        self.assertEqual(engine.status()[0]['commit_oid'],None)
        self.assertEqual(self.git('rev-parse','HEAD').strip(),prepared['source_commit'])

    def test_native_success_with_later_required_receipt_failure_keeps_code_waiting(self):
        from eutonos.git_delivery import DeliveryError
        engine=self.configure_delivery()
        context={**self.context,'native_log':str(self.native_log()),
                 'delivery':{'logical_id':'receipt-fence','paths':['changed.py']}}
        prepared=prepare_closeout(self.values,context);self.admit(prepared)
        with mock.patch('eutonos.companion_capture.write_and_read_receipt',side_effect=OSError('receipt unavailable')):
            result=execute_closeout(prepared,context)
        self.assertEqual(result['outcome'],'partial')
        self.assertEqual(result['result']['outcome'],'completed')
        code=next(j for j in engine.status() if j['payload']['kind']=='code')
        engine.drain_verified()
        self.assertEqual(engine.run(code['id'])['state'],'waiting-native')
        with self.assertRaises(DeliveryError):engine.resume(code['id'])
        self.assertIsNone(engine.status(code['id'])[0]['commit_oid'])
        retry=execute_closeout(prepared,context)
        self.assertEqual(retry['git_delivery']['state'],'committed',retry)

    def test_failed_candidate_check_keeps_native_closeout_success(self):
        import sys
        engine = self.configure_delivery(checks=[[sys.executable,'-c','raise SystemExit(7)']])
        context = {**self.context,'delivery':{'logical_id':'failed-check','paths':['changed.py']}}
        prepared = prepare_closeout(self.values,context)
        enroll(self.root,self.host,'writer',prepared['allowed_paths'])
        result = execute_closeout(prepared,context)
        self.assertEqual(result['outcome'],'completed',result)
        self.assertEqual(result['git_delivery']['state'],'blocked',result)
        self.assertIn('check',result['git_delivery']['error'].lower())
        self.assertTrue(result['result']['current_verified'])

    def test_checked_closeout_tamper_and_directory_selection_are_refused(self):
        from eutonos.git_delivery_native import deliver_code, prepare_code
        from eutonos.git_delivery import DeliveryError, freeze
        engine = self.configure_delivery()
        context = {**self.context,'delivery':{'logical_id':'tamper','paths':['changed.py']}}
        prepared = prepare_closeout(self.values,context)
        frozen = json.loads(json.dumps(prepared['code_delivery']))
        frozen['changes']['changed.py']['after'] = freeze(b'foreign = True\n')
        self.assertEqual(deliver_code(self.root,frozen,session='session',work_id='work',selection=context['delivery'])['state'],'unavailable')
        frozen = json.loads(json.dumps(prepared['code_delivery'])); frozen['session']='another-session'
        self.assertEqual(deliver_code(self.root,frozen,session='session',work_id='work')['state'],'unavailable')
        with self.assertRaises(DeliveryError):
            prepare_code(self.root,{'logical_id':'directory','paths':['RAM']},session='session',work_id='work')
        self.assertEqual(engine.status()[0]['state'],'waiting-native')

    def write_work(self, path="WORK.md", *, status="selected", profile=ts.WORK, blocks=1):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        value = {"profile": profile, "work_id": "catalog-work", "goal": "Catalog-selected work",
                 "status": status, "required": [], "optional": []}
        block = b"```tokenslang-work\n" + ts.encoded(value) + b"\n```\n"
        target.write_bytes(b"# Catalog work\n\n" + block * blocks)
        return target

    def write_catalog(self, path="WORK.md", **binding_changes):
        binding = {"resource_id": "fixture.current", "path": path, "kind": "docs",
                   "label": "Current work", "visibility": "public", "state": "active"}
        binding.update(binding_changes)
        catalog = {"profile": ts.CATALOG, "repository_id": "fixture.repo", "scope_id": "fixture.scope",
                   "namespace": "fixture.native", "owners": {"current_task": {
                       "scope_id": "fixture.scope", "namespace": "fixture.native",
                       "resource_id": "fixture.current"}}, "bindings": [binding],
                   "excluded_roots": [], "source_paths": []}
        (self.root / "eutonos.read.json").write_bytes(ts.encoded(catalog))
        return catalog

    def admit(self, *prepared):
        paths = sorted({path for item in prepared for path in item["allowed_paths"]})
        enroll(self.root, self.host, "writer", paths)

    def native_log(self, active=True):
        path = self.home / ("native-active.jsonl" if active else "native-invalid.jsonl")
        events = [{"type": "session_meta", "payload": {"id": "thread-fixture"}}]
        if active:
            events.append({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-fixture"}})
        path.write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")
        return path

    def test_catalog_and_incomplete_form_share_definitions_and_preserve_values(self):
        commands = catalog("close")["commands"]
        self.assertEqual([item["id"] for item in commands], ["closeout", "boards", "other"])
        self.assertTrue(next(item for item in commands if item["id"] == "closeout")["supported"])
        result = validate_closeout({"what": "Kept", "checks": "not-a-list"})
        self.assertEqual(result["outcome"], "incomplete")
        self.assertEqual(result["values"]["what"], "Kept")
        self.assertEqual({item["name"] for item in result["missing_fields"]}, {"why", "unresolved"})
        self.assertEqual([item["name"] for item in result["invalid_fields"]], ["checks"])

    def test_prepare_fills_identity_commit_paths_and_classifies_current_bytes(self):
        result = prepare_closeout(self.values, self.context)
        self.assertEqual(result["outcome"], "prepared")
        self.assertEqual(result["changed_paths"][0]["state"], "modified")
        self.assertTrue(result["changed_paths"][0]["current_sha256"])
        self.assertEqual(result["semantic"]["experience"]["source_commit"], self.git("rev-parse", "HEAD").strip())
        self.assertEqual(result["semantic"]["experience"]["actors"][0]["id"], "writer")
        self.assertEqual(result["allowed_paths"], sorted([result["record_path"], "WORK.md"]))

    def test_new_form_records_request_actions_result_and_known_registry_reference(self):
        (self.root / "docs").mkdir()
        (self.root / "docs/REGISTRY.json").write_text(json.dumps({"documents": [
            {"id": "fixture.changed", "path": "changed.py"}]}), encoding="utf-8")
        result = prepare_closeout(self.new_values, self.context)
        self.assertEqual(result["outcome"], "prepared")
        summary = result["semantic"]["experience"]["summary"]
        self.assertIn("User request: Change the fixture.", summary)
        self.assertIn("Agent actions: Updated changed.py.", summary)
        self.assertIn("Outcome: The fixture now uses the new value.", summary)
        self.assertEqual(result["references"][0]["id"], "fixture.changed")

    def test_persisted_source_pins_exclude_modified_deleted_and_added_bytes(self):
        (self.root / "clean.py").write_text("unchanged = True\n", encoding="utf-8")
        (self.root / "deleted.py").write_text("old = True\n", encoding="utf-8")
        self.git("add", "clean.py", "deleted.py")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "source fixtures")
        (self.root / "deleted.py").unlink()
        (self.root / "new.py").write_text("new = True\n", encoding="utf-8")
        (self.root / "staged.py").write_text("staged = True\n", encoding="utf-8")
        self.git("add", "staged.py")
        values = {**self.new_values, "changed_paths": ["changed.py", "deleted.py", "new.py", "staged.py", "clean.py"]}
        prepared = prepare_closeout(values, self.context)
        self.assertEqual(prepared["outcome"], "prepared")
        self.assertEqual({item["path"]: item["state"] for item in prepared["changed_paths"]},
                         {"changed.py": "modified", "deleted.py": "deleted", "new.py": "untracked",
                          "staged.py": "added", "clean.py": "committed-clean"})
        self.admit(prepared)
        result = execute_closeout(prepared, self.context)
        self.assertEqual(result["outcome"], "completed")
        record = ram.Store(LocalRoot(self.root)).load(prepared["record_path"])
        sources = record["versions"][-1]["sources"]
        self.assertEqual([item["path"] for item in sources], ["clean.py"])
        from eutonos.effects import digest
        self.assertEqual(sources[0]["sha256"], digest((self.root / "clean.py").read_bytes()))

    def test_dirty_work_owner_is_only_an_explicit_baseline_fallback(self):
        work = self.root / "WORK.md"
        work.write_text(work.read_text(encoding="utf-8") + "\nNew decision not at HEAD.\n", encoding="utf-8")
        prepared = prepare_closeout({**self.new_values, "changed_paths": ["changed.py", "WORK.md"]}, self.context)
        self.assertEqual(prepared["outcome"], "prepared")
        self.assertTrue(prepared["baseline_work_anchor"])
        self.assertEqual([item["path"] for item in prepared["semantic"]["experience"]["sources"]], ["eutonos.ram.json"])
        self.assertIn("historical baseline anchor only", prepared["semantic"]["experience"]["summary"])
        self.assertIn("not proof of uncommitted changes", prepared["semantic"]["experience"]["summary"])
        self.admit(prepared)
        result = execute_closeout(prepared, self.context)
        self.assertEqual(result["outcome"], "completed")
        record = ram.Store(LocalRoot(self.root)).load(prepared["record_path"])
        from eutonos.effects import digest
        self.assertNotEqual(record["versions"][-1]["sources"][0]["sha256"], digest(work.read_bytes()))

    def test_ram_capture_runs_even_when_saved_settings_disable_it(self):
        state = settings.status(self.root)
        settings.update(self.root, 0, state["revision"], "ram", {"retrieval": "on-demand", "capture": False})
        prepared = prepare_closeout(self.new_values, self.context)
        self.assertEqual(prepared["outcome"], "prepared")
        self.assertTrue(prepared["capture_policy"]["eligible"])
        self.admit(prepared)
        result = execute_closeout(prepared, self.context)
        self.assertEqual(result["outcome"], "completed", result)
        self.assertTrue(result["result"]["current_verified"])
        self.assertIsNotNone(ram.Store(LocalRoot(self.root)).load(prepared["record_path"]))
        self.assertEqual(capture_policy.snapshot(self.root, "writer", "work", "session"), [])

    def test_closeout_runs_for_quick_turn_under_old_deferred_cadence(self):
        current = settings.status(self.root)
        settings.update(self.root, 0, current["revision"], "ram",
                        {"cadence": "work-sized", "capture": False})
        quick = {**self.new_values, "work_size": "quick", "event": "turn"}
        prepared = prepare_closeout(quick, self.context)
        self.assertEqual(prepared["outcome"], "prepared")
        self.assertTrue(prepared["capture_policy"]["eligible"])
        self.admit(prepared)
        captured = execute_closeout(prepared, self.context)
        self.assertEqual(captured["outcome"], "completed")
        self.assertTrue(captured["result"]["current_verified"])
        self.assertEqual(capture_policy.snapshot(self.root, "writer", "work", "session"), [])

    def test_legacy_deferred_closeout_is_flushed_once_and_replay_is_not_captured_again(self):
        from eutonos.workflow import _pending_entry
        raw_values = {**self.new_values, "work_size": "quick", "event": "turn"}
        values = validate_closeout(raw_values)["values"]
        pending_entry = _pending_entry(values)
        pending_scope = {"root": str(self.root.resolve()), "actor": "writer", "work_id": "work",
                         "session_id": "session", "native_identity": None}
        legacy_deferred = {"profile": "eutonos.tokenslang.workflow.v1", "outcome": "deferred",
                           "command": "closeout", "values": values, "pending_entry": pending_entry,
                           "pending_scope": pending_scope}
        prepared_current = prepare_closeout(raw_values, self.context)
        self.admit(prepared_current)
        first = execute_closeout(legacy_deferred, self.context)
        self.assertEqual(first["outcome"], "completed", first)
        self.assertTrue(first["result"]["current_verified"])
        self.assertEqual(capture_policy.snapshot(self.root, "writer", "work", "session"), [])
        replay = execute_closeout(legacy_deferred, self.context)
        self.assertEqual(replay["outcome"], "already-completed")
        self.assertEqual(replay["reason"], "pending-delta-already-consumed")

    def test_deferred_native_turn_identity_distinguishes_repeated_semantics(self):
        current = settings.status(self.root)
        settings.update(self.root, 0, current["revision"], "ram", {"cadence": "checkpoint"})
        first_log = self.native_log()
        second_log = self.home / "native-second.jsonl"
        second_log.write_text(json.dumps({"type": "session_meta", "payload": {"id": "thread-fixture"}}) + "\n" +
                              json.dumps({"type": "event_msg", "payload": {
                                  "type": "task_started", "turn_id": "turn-second"}}) + "\n", encoding="utf-8")
        first_context = {**self.context, "native_log": str(first_log)}
        second_context = {**self.context, "native_log": str(second_log)}
        first = prepare_closeout({**self.new_values, "event": "turn"}, first_context)
        second = prepare_closeout({**self.new_values, "event": "turn"}, second_context)
        self.assertEqual((first["outcome"], second["outcome"]), ("prepared", "prepared"))
        self.assertNotEqual(first["native_identity"], second["native_identity"])
        mismatch = execute_closeout(first, second_context)
        self.assertEqual((mismatch["outcome"], mismatch["code"]), ("error", "stale"))

    def test_failed_flush_retains_pending_for_exact_retry(self):
        from eutonos.workflow import _pending_entry
        current = settings.status(self.root)
        settings.update(self.root, 0, current["revision"], "ram", {"cadence": "checkpoint"})
        turn = {**self.new_values, "event": "turn"}
        old = validate_closeout(turn)["values"]
        capture_policy.defer(self.root, "writer", "work", "session",
                             _pending_entry(old))
        checkpoint = {**self.new_values, "result": "Checkpoint result.", "event": "checkpoint"}
        prepared = prepare_closeout(checkpoint, self.context)
        self.admit(prepared)
        failed = execute_closeout(prepared, self.context, capture_fn=lambda *args: {
            "outcome": "error", "code": "unavailable", "current_verified": False})
        self.assertEqual(failed["outcome"], "error")
        self.assertEqual(len(capture_policy.snapshot(self.root, "writer", "work", "session")), 1)
        self.assertEqual(execute_closeout(prepared, self.context)["pending_clear"]["remaining"], 0)

    def test_consolidated_and_selected_pending_ids_preserve_safety_fields(self):
        from eutonos.workflow import _pending_entry
        current = settings.status(self.root)
        settings.update(self.root, 0, current["revision"], "ram", {"cadence": "checkpoint"})
        old = {**self.new_values, "request": "Verbose old request.", "checks": ["old check"],
               "unresolved": ["old risk"], "event": "turn"}
        old = validate_closeout(old)["values"]
        capture_policy.defer(self.root, "writer", "work", "session",
                             _pending_entry(old))
        pending = capture_policy.snapshot(self.root, "writer", "work", "session")
        current_values = {**self.new_values, "request": "Consolidated request.", "event": "checkpoint",
                          "consolidated_pending_ids": [pending[0]["id"]]}
        prepared = prepare_closeout(current_values, self.context)
        summary = prepared["semantic"]["experience"]["summary"]
        self.assertNotIn("Verbose old request.", summary)
        self.assertIn("old check", summary)
        self.assertIn("old risk", summary)
        empty = prepare_closeout({**current_values, "consolidated_pending_ids": [],
                                  "selected_pending_ids": []}, self.context)
        self.assertEqual(empty["pending"], [])
        self.assertEqual(len(capture_policy.snapshot(self.root, "writer", "work", "session")), 1)

    def test_ram_disabled_after_prepare_does_not_suppress_frozen_write(self):
        prepared = prepare_closeout(self.new_values, self.context)
        state = settings.status(self.root)
        settings.update(self.root, 0, state["revision"], "ram", {"retrieval": "on-demand", "capture": False})
        self.admit(prepared)
        result = execute_closeout(prepared, self.context)
        self.assertEqual(result["outcome"], "completed")
        self.assertTrue(result["result"]["current_verified"])

    def test_ram_off_replay_reads_completed_record_without_duplicate_capture(self):
        prepared = prepare_closeout(self.new_values, self.context); self.admit(prepared)
        first = execute_closeout(prepared, self.context)
        self.assertEqual(first["outcome"], "completed")
        record = ram.Store(LocalRoot(self.root)).load(prepared["record_path"])
        before_versions = len(record["versions"])
        state = settings.status(self.root)
        settings.update(self.root, 0, state["revision"], "ram", {"retrieval": "on-demand", "capture": False})
        result = execute_closeout(prepared, self.context)
        self.assertEqual(result["outcome"], "already-completed")
        self.assertTrue(result["result"]["current_verified"])
        after = ram.Store(LocalRoot(self.root)).load(prepared["record_path"])
        self.assertEqual(len(after["versions"]), before_versions)

    def test_pickup_reads_only_selected_task_owner_and_routes(self):
        result = pickup(self.root)
        self.assertEqual(result["outcome"], "pickup")
        self.assertEqual(result["selected_task"]["work_id"], "work")
        self.assertEqual(result["task_owner"]["path"], "WORK.md")
        self.assertIn("diagnose", {item["id"] for item in result["routes"]})

    def test_pickup_shows_current_work_order_row_and_admission_rule(self):
        items = [{"id": "C", "type": "campaign", "title": "Campaign", "status": "active"},
                 {"id": "S1", "type": "stage", "title": "Stage one", "status": "planned", "parent_id": "C"},
                 {"id": "Q", "type": "quick", "title": "Quick", "status": "active"}]
        order = {"configured": True, "revision": "r", "current": ["Q"], "next": [],
                 "rows": {"current": [{"together": ["S1", "Q"]}], "next": []}}
        board = {"status": "available", "owners": [], "items": items, "diagnostics": [], "work_order": order}
        with mock.patch("eutonos.boards.project", return_value=board), \
                mock.patch("eutonos.current_work.project", side_effect=OSError("offline")):
            result = pickup(self.root)
        row = result["current_row"]
        self.assertTrue(row["together"])
        self.assertEqual([(i["id"], i["status"], i.get("campaign")) for i in row["items"]],
                         [("S1", "planned", "Campaign"), ("Q", "active", None)])
        self.assertIn("agents start work when the owner assigns it", row["admission"])
        self.assertIn("Owner-only actions", row["admission"])
        self.assertIsNone(pickup(self.root)["current_row"])

    def test_pickup_omits_crosstalk_field_when_crosstalk_is_not_installed(self):
        # Slice 3B.6: no Crosstalk device folder -> pickup's output stays exactly as it
        # always has, with no new key, and the retired legacy "mailbox" key never appears.
        with mock.patch.dict(os.environ, {"CROSSTALK_P0_DEVICE_DIR": str(self.root / "no-such-device")}):
            result = pickup(self.root)
        self.assertEqual(result["outcome"], "pickup")
        self.assertNotIn("crosstalk", result)
        self.assertNotIn("mailbox", result)

    def test_pickup_uses_live_catalog_owner_without_ram_installation(self):
        (self.root / "eutonos.ram.json").unlink()
        self.write_work("owners/LIVE.md")
        self.write_catalog("owners/LIVE.md")
        result = pickup(self.root)
        self.assertEqual(result["outcome"], "pickup")
        self.assertEqual(result["task_owner"]["path"], "owners/LIVE.md")
        self.assertEqual(result["selected_task"]["work_id"], "catalog-work")
        self.assertFalse(result["ram"]["configured"])

    def test_pickup_rejects_invalid_missing_ambiguous_and_ineligible_catalog_owners(self):
        (self.root / "eutonos.ram.json").unlink()
        cases = []
        wrong_scope = self.write_catalog()
        wrong_scope["owners"]["current_task"]["scope_id"] = "other.scope"
        cases.append((wrong_scope, "wrong-scope"))
        missing = self.write_catalog()
        missing["owners"]["current_task"]["resource_id"] = "missing"
        cases.append((missing, "missing"))
        ambiguous = self.write_catalog()
        ambiguous["bindings"].append({**ambiguous["bindings"][0], "path": "OTHER.md"})
        cases.append((ambiguous, "ambiguous"))
        private = self.write_catalog(visibility="private")
        cases.append((private, "denied"))
        inactive = self.write_catalog(state="suppressed")
        cases.append((inactive, "denied"))
        wrong_kind = self.write_catalog(kind="source")
        cases.append((wrong_kind, "unsupported"))
        excluded = self.write_catalog()
        excluded["excluded_roots"] = ["WORK.md"]
        cases.append((excluded, "denied"))
        unsafe = self.write_catalog(path="../WORK.md")
        cases.append((unsafe, "unsafe-path"))
        for catalog, code in cases:
            with self.subTest(code=code, catalog=catalog):
                (self.root / "eutonos.read.json").write_bytes(ts.encoded(catalog))
                result = pickup(self.root)
                self.assertEqual((result["outcome"], result["code"]), ("error", code))

    def test_pickup_rejects_catalog_and_ram_owner_conflict(self):
        self.write_work("owners/LIVE.md")
        self.write_catalog("owners/LIVE.md")
        result = pickup(self.root)
        self.assertEqual((result["outcome"], result["code"]), ("error", "conflict"))

    def test_pickup_enforces_every_live_fingerprint_on_the_selected_path(self):
        (self.root / "eutonos.ram.json").unlink()
        catalog = self.write_catalog()
        catalog["bindings"].append({**catalog["bindings"][0], "resource_id": "fixture.same-path",
                                    "sha256": "0" * 64})
        (self.root / "eutonos.read.json").write_bytes(ts.encoded(catalog))
        result = pickup(self.root)
        self.assertEqual((result["outcome"], result["code"]), ("error", "stale"))

    def test_pickup_rejects_malformed_ambiguous_and_unselected_work_blocks(self):
        (self.root / "eutonos.ram.json").unlink()
        self.write_catalog()
        cases = [(b"# No native work\n", "unsupported")]
        self.write_work(blocks=2)
        cases.append(((self.root / "WORK.md").read_bytes(), "ambiguous"))
        self.write_work(status="cancelled")
        cases.append(((self.root / "WORK.md").read_bytes(), "conflict"))
        for body, code in cases:
            with self.subTest(code=code):
                (self.root / "WORK.md").write_bytes(body)
                result = pickup(self.root)
                self.assertEqual((result["outcome"], result["code"]), ("error", code))

    def test_new_form_closeout_retry_keeps_one_native_ram_identity(self):
        prepared = prepare_closeout(self.new_values, self.context); self.admit(prepared)
        self.assertEqual(execute_closeout(prepared, self.context)["outcome"], "completed")
        self.assertEqual(execute_closeout(prepared, self.context)["outcome"], "already-completed")
        refreshed = prepare_closeout(self.new_values, self.context)
        self.assertTrue(refreshed["ram_native"]["existing"])
        self.assertIn(prepared["record_path"], refreshed["ram_native"]["existing_work_links"])

    def test_incomplete_prepare_still_drafts_changed_paths(self):
        with mock.patch("eutonos.workflow._path_evidence") as evidence:
            result = prepare_closeout({"what": "Known"}, {"root": str(self.root)})
        evidence.assert_not_called()
        self.assertEqual(result["outcome"], "incomplete")
        self.assertEqual([item["path"] for item in result["draft"]["detected_changed_paths"]], ["changed.py"])
        self.assertTrue(result["draft"]["detected_changed_paths"][0]["candidate_only"])
        self.assertEqual(result["draft"]["detected_changed_paths"][0]["status"], "changed")
        self.assertEqual({item["name"] for item in result["missing_context"]},
                         {"host", "actor", "session_id"})

    def test_end_to_end_closeout_and_exact_retry_have_one_memory_version(self):
        prepared = prepare_closeout(self.values, self.context); self.admit(prepared)
        first = execute_closeout(self.values, self.context)
        self.assertEqual(first["outcome"], "completed")
        self.assertTrue(first["result"]["current_verified"])
        retry = execute_closeout(self.values, self.context)
        self.assertEqual(retry["outcome"], "already-completed")
        frozen_retry = execute_closeout(prepared, self.context)
        self.assertEqual(frozen_retry["outcome"], "already-completed")
        record = ram.Store(LocalRoot(self.root)).load(prepared["record_path"])
        self.assertEqual(record["revision"], 1)
        self.assertEqual(len(record["versions"]), 1)
        self.assertEqual(len(record["receipts"]), 1)

    def test_frozen_form_detects_changed_source_before_first_write(self):
        prepared = prepare_closeout(self.values, self.context); self.admit(prepared)
        (self.root / "changed.py").write_text("moved = True\n", encoding="utf-8")
        result = execute_closeout(prepared, self.context)
        self.assertEqual(result["outcome"], "error")
        self.assertEqual(result["code"], "stale")
        self.assertFalse(Path(prepared["artifact"]).exists())

    def test_malformed_prepared_form_is_a_structured_error(self):
        result = execute_closeout({"profile": "eutonos.tokenslang.workflow.v1", "outcome": "prepared"}, self.context)
        self.assertEqual(result["outcome"], "error")
        self.assertEqual(result["code"], "invalid")

    def test_same_form_at_new_head_gets_new_frozen_identity(self):
        first = prepare_closeout(self.values, self.context)
        self.git("add", "changed.py")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "new source")
        second = prepare_closeout(self.values, self.context)
        self.assertNotEqual(first["identity"], second["identity"])

    def test_independent_forms_write_independent_paths(self):
        second_values = {**self.values, "what": "A separate result."}
        first = prepare_closeout(self.values, self.context)
        second = prepare_closeout(second_values, self.context)
        self.assertNotEqual(first["record_path"], second["record_path"])
        self.admit(first, second)
        self.assertEqual(execute_closeout(self.values, self.context)["outcome"], "completed")
        self.assertEqual(execute_closeout(second_values, self.context)["outcome"], "completed")

    def test_same_memory_written_without_frozen_operation_returns_conflict(self):
        prepared = prepare_closeout(self.values, self.context); self.admit(prepared)
        from eutonos.companion_ram_capture import capture
        other_artifact = self.root / ".eutonos-local" / "other.json"
        seeded = capture(self.root, self.host, "writer", "seed-attempt", prepared["semantic"],
                         prepared["allowed_paths"], other_artifact, "2026-09-13T02:01:02Z")
        self.assertEqual(seeded["outcome"], "completed")
        result = execute_closeout(self.values, self.context)
        self.assertEqual(result["outcome"], "error")
        self.assertEqual(result["result"]["code"], "conflict")
        self.assertIn("Record exists", result["result"]["message"])

    def test_native_prefinal_receipt_is_verified_pending_and_idempotent(self):
        context = {**self.context, "native_log": str(self.native_log())}
        prepared = prepare_closeout(self.values, context); self.admit(prepared)
        first = execute_closeout(prepared, context)
        self.assertEqual(first["outcome"], "completed")
        receipt = first["capture_receipt"]
        self.assertEqual(receipt["receipt"]["stage"], "pre-final")
        self.assertFalse(receipt["receipt"]["counts_completed"])
        self.assertFalse(receipt["counted_completed"])
        self.assertEqual(receipt["receipt"]["assertion"]["native_proof"], "verified-readback")
        self.assertFalse(receipt["counted_verified"])
        retry = execute_closeout(prepared, context)
        self.assertEqual(retry["outcome"], "already-completed")
        self.assertEqual(retry["capture_receipt"]["outcome"], "idempotent")
        record = ram.Store(LocalRoot(self.root)).load(prepared["record_path"])
        self.assertEqual((record["revision"], len(record["versions"])), (1, 1))

    def test_receipt_failure_after_ram_is_partial_and_retry_does_not_duplicate_ram(self):
        from eutonos.workflow import _pending_entry
        context = {**self.context, "native_log": str(self.native_log())}
        current = settings.status(self.root)
        current = settings.update(self.root, 0, current["revision"], "ram", {"cadence": "checkpoint"})
        prior = validate_closeout({**self.new_values, "event": "turn"})["values"]
        from eutonos.companion_capture import native_identity
        capture_policy.defer(self.root, "writer", "work", "session",
                             _pending_entry(prior, native_identity(context["native_log"], stage="pre-final")))
        settings.update(self.root, 0, current["revision"], "ram", {"cadence": "every-turn"})
        prepared = prepare_closeout(self.values, context); self.admit(prepared)
        self.assertEqual(len(prepared["pending"]), 1)
        with mock.patch("eutonos.companion_capture.write_and_read_receipt", side_effect=OSError("receipt unavailable")):
            first = execute_closeout(prepared, context)
        self.assertEqual(first["outcome"], "partial")
        self.assertEqual(first["result"]["outcome"], "completed")
        self.assertEqual(len(capture_policy.snapshot(self.root, "writer", "work", "session")), 1)
        retry = execute_closeout(prepared, context)
        self.assertEqual(retry["outcome"], "already-completed")
        self.assertEqual(retry["capture_receipt"]["outcome"], "recorded")
        self.assertEqual(retry["pending_clear"]["remaining"], 0)
        record = ram.Store(LocalRoot(self.root)).load(prepared["record_path"])
        self.assertEqual((record["revision"], len(record["versions"])), (1, 1))

    def test_receipt_cannot_move_to_a_new_turn_after_ram_capture(self):
        from eutonos import companion_capture
        context = {**self.context, "native_log": str(self.native_log())}
        prepared = prepare_closeout(self.values, context); self.admit(prepared)
        template = companion_capture.capture_template(context["native_log"], "pre-final")
        with mock.patch("eutonos.companion_capture.capture_template", return_value={**template, "turn_id": "different-turn"}):
            result = execute_closeout(prepared, context)
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["capture_receipt"]["code"], "stale")
        self.assertEqual(companion_capture.read_receipts(self.root), [])

    def test_invalid_native_log_prevents_ram_write(self):
        context = {**self.context, "native_log": str(self.native_log(active=False))}
        result = prepare_closeout(self.values, context)
        self.assertEqual(result["outcome"], "error")
        store = ram.Store(LocalRoot(self.root))
        self.assertEqual(list((self.root / store.cfg["store_root"]).rglob("*.json")), [])

    def test_git_only_repo_still_drafts_candidates_and_reports_setup(self):
        other = self.home / "git-only"; other.mkdir()
        (other / "plain.txt").write_text("one\n", encoding="utf-8")
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        def git(*args):
            return subprocess.check_output(["git", "-c", "safe.directory=" + str(other), "-C", str(other), *args],
                                           env=env, text=True, encoding="utf-8", stderr=subprocess.PIPE)
        git("init", "-q", "-b", "main"); git("add", "--all")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "baseline")
        (other / "plain.txt").write_text("two\n", encoding="utf-8")
        draft = prepare_closeout({"what": "Known"}, {"root": str(other)})
        self.assertEqual([item["path"] for item in draft["draft"]["detected_changed_paths"]], ["plain.txt"])
        complete_context = {**self.context, "root": str(other)}
        result = prepare_closeout(self.values, complete_context)
        self.assertEqual((result["outcome"], result["code"]), ("error", "not-configured"))


class CrosstalkPickupHintTests(WorkflowTests):
    """Slice 3B.6: `workflow.pickup()` folds in one compact, read-only Crosstalk status
    line (via `crosstalk_status.pickup_hint`) when the Crosstalk device folder exists,
    and leaves pickup's output unchanged otherwise (see
    `WorkflowTests.test_pickup_omits_crosstalk_field_when_crosstalk_is_not_installed`).
    It replaced the legacy mailbox hint; pickup never reads the legacy mailbox."""

    def device(self, **controls):
        folder = self.root / "crosstalk-device"
        folder.mkdir(exist_ok=True)
        if controls:
            (folder / "controls.json").write_text(json.dumps(controls), encoding="utf-8")
        env = mock.patch.dict(os.environ, {"CROSSTALK_P0_DEVICE_DIR": str(folder)})
        env.start()
        self.addCleanup(env.stop)
        return folder

    def test_pickup_reports_messaging_on(self):
        self.device(messaging="on", deny=["codex:x", "claude:y"])
        result = pickup(self.root)
        self.assertEqual(result["outcome"], "pickup")
        hint = result["crosstalk"]
        self.assertEqual((hint["messaging"], hint["halt"], hint["denied"], hint["available"]),
                         ("on", False, 2, True))
        self.assertIn("crosstalk: messaging on", hint["line"])
        self.assertNotIn("mailbox", result)

    def test_pickup_reports_off_and_halted(self):
        folder = self.device()
        result = pickup(self.root)
        self.assertEqual(result["crosstalk"]["messaging"], "off")
        self.assertIn("messaging OFF", result["crosstalk"]["line"])
        (folder / "controls.json").write_text(json.dumps({"messaging": "on", "halt": True}), encoding="utf-8")
        halted = pickup(self.root)["crosstalk"]
        self.assertTrue(halted["halt"])
        self.assertFalse(halted["available"])
        self.assertIn("HALTED", halted["line"])

    def test_pickup_survives_a_failing_crosstalk_reader(self):
        with mock.patch("eutonos.crosstalk_status.pickup_hint", side_effect=RuntimeError("boom")):
            result = pickup(self.root)
        self.assertEqual(result["outcome"], "pickup")
        self.assertNotIn("crosstalk", result)

    def test_pickup_does_not_touch_the_legacy_mailbox_directory(self):
        self.device(messaging="on")
        pickup(self.root)
        self.assertFalse((self.root / ".eutonos-local" / "mailbox").exists())


if __name__ == "__main__":
    unittest.main()
