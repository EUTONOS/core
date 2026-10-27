"""Regressions at dispatch, saved-job and picker model-policy boundaries."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from eutonos import assignments, assignment_metrics, companion_attribution, companion_control
from eutonos import model_catalog, model_watch, role_router, settings, settings_cli, tokenslang as ts
from test_companion_attribution import assignment, run


class ModelPolicyReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "codex-home"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home),
                                                "EUTONOS_HOME": str(self.root / "eutonos-home")}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        self.cache()

    def cache(self, models=None):
        models = models or {"gpt-6-sol": ["high"], "gpt-6.1-sol": ["high", "ultra"],
                            "gpt-6-luna": ["high"], "gpt-7-solstice": ["high"]}
        rows = [{"slug": model, "visibility": "list", "supported_reasoning_levels":
                 [{"effort": effort} for effort in efforts]} for model, efforts in models.items()]
        (self.home / "models_cache.json").write_text(json.dumps({"models": rows}), encoding="utf-8")

    def routing(self, allowed):
        state = settings.status(self.root)
        settings.update(self.root, 0, state["revision"], "routing", {
            **state["routing"], "enabled": True, "allowed_models": allowed})

    def request(self, model="sol"):
        return {"profile": assignments.CREATE, "idempotency_key": "one", "request_id": "one",
                "primary_thread_id": "primary", "primary_model": "gpt-6-sol",
                "worker_model": model, "worker_effort": "high", "category": "other",
                "logical_scope": "bounded review", "brief": {"goal": "Review", "scope": "One file",
                "checks": ["Focused test"], "closeout": "Report"}, "source_refs": [{"path": "README.md"}]}

    def create(self, request):
        origin = {"root": str(self.root.resolve()), "root_id": "a" * 64, "head": "b" * 40,
                  "basis": "fixture"}
        with mock.patch.object(assignments, "_origin", return_value=origin):
            return assignments.create(self.root, request)

    def test_unknown_pin_containing_known_family_needs_approval(self):
        self.assertEqual(companion_control.routing_decision("gpt-6-sol", "gpt-7-solstice")["outcome"],
                         "approval-required")
        self.assertEqual(companion_control.routing_decision("gpt-6-sol", "gpt-7-solstice",
                                                           approval="approved")["outcome"], "selected")
        self.assertEqual(companion_control.routing_decision("gpt-6-sol", "gpt-7-solstice",
                                                           waited_seconds=60)["outcome"], "continue-primary")

    def test_renaming_role_preserves_one_retired_option_but_cannot_copy_it(self):
        option = {"harness": "codex", "model": "gpt-7-sol", "effort": "high"}
        self.cache({"gpt-7-sol": ["high"]})
        before = settings.read_roles(self.root)
        role = {"options": [option], "preamble": None, "autonomy": "ask"}
        saved = settings.update_roles(self.root, {"old-name": role},
                                      expected_revision=before["revision"])
        model_watch.scan(now=0)
        self.cache({"gpt-6.1-sol": ["high"]})
        model_watch.scan(now=model_watch.ABSENCE_INTERVAL)
        model_watch.scan(now=2 * model_watch.ABSENCE_INTERVAL)
        self.assertFalse(model_catalog.describe_catalog(self.root)["models"]["gpt-7-sol"]["available"])
        moved = settings.update_roles(self.root, {"new-name": role}, expected_revision=saved["revision"])
        self.assertEqual(moved["roles"]["new-name"]["options"], [option])
        copied = {"new-name": role, "second-name": role}
        with self.assertRaisesRegex(ValueError, "Unavailable codex model"):
            settings.update_roles(self.root, copied, expected_revision=moved["revision"])

    def test_unknown_primary_containing_known_family_does_not_authorize_auto_move(self):
        self.assertEqual(companion_control.routing_decision("gpt-7-solstice", "gpt-6-sol")["outcome"],
                         "approval-required")

    def test_assignment_family_replay_keeps_original_resolution(self):
        self.routing(["sol"])
        request = self.request()
        first = self.create(request)
        self.assertEqual(first["outcome"], "created")
        self.assertEqual(first["assignment"]["requested_worker"]["model"], "sol")
        packet = assignments.packet(self.root, first["assignment"]["assignment_id"])
        self.assertEqual(packet["model_selection"]["resolved"], "gpt-6.1-sol")
        self.cache({"gpt-6.2-sol": ["high"]})
        replay = self.create(request)
        self.assertEqual(replay["outcome"], "idempotent")
        self.assertEqual(replay["assignment"], json.loads(json.dumps(first["assignment"])))
        fresh = self.create({**request, "idempotency_key": "two"})
        self.assertEqual(fresh["assignment"]["routing"]["model_selection"]["resolved"], "gpt-6.2-sol")

    def test_historical_packet_without_selection_field_retains_original_hash(self):
        self.routing(["sol"])
        record = self.create(self.request())["assignment"]
        del record["model_selection"]
        assignments._set_packet_hash(record)
        original_packet = assignments._packet(record)
        original_hash = record["packet_sha256"]
        assignments._atomic(assignments._path(self.root, record["assignment_id"]), record)
        self.cache({"gpt-6.2-sol": ["high"]})
        reread = assignments.get(self.root, record["assignment_id"])
        self.assertNotIn("model_selection", original_packet)
        self.assertEqual(reread["packet_sha256"], original_hash)
        self.assertEqual(assignments.packet(self.root, record["assignment_id"]), original_packet)

    def test_unavailable_pin_replay_survives_but_new_assignment_refuses(self):
        self.routing(["gpt-6-sol", "sol"])
        request = self.request("gpt-6-sol")
        first = self.create(request)
        self.cache({"gpt-6.2-sol": ["high"]})
        self.assertEqual(self.create(request)["assignment"], json.loads(json.dumps(first["assignment"])))
        with self.assertRaises(ts.ReadError):
            self.create({**request, "idempotency_key": "two"})
        conflict = self.create({**request, "logical_scope": "changed"})
        self.assertEqual(conflict["outcome"], "conflict")
        self.assertEqual(len(assignments.records(self.root)), 1)

    def test_attribution_uses_saved_family_resolution_after_catalog_moves(self):
        record = assignment(worker_model="sol")
        record["routing"] = {"model_selection": model_catalog.resolve_choice("sol", "codex")}
        self.cache({"gpt-6.2-sol": ["high"]})
        native = [run("worker-thread-a", "worker-1", "gpt-6.1-sol", "support")]
        metadata = assignment_metrics.metadata([record])
        result = companion_attribution.project(metadata, native)["records"][0]["companion"]
        self.assertEqual((result["requested_model"], result["resolved_model"]), ("sol", "gpt-6.1-sol"))
        self.assertTrue(result["model_matches_request"])
        native[0]["models"] = ["gpt-6.2-sol"]
        self.assertFalse(companion_attribution.project(metadata, native)["records"][0]
                         ["companion"]["model_matches_request"])

    def test_family_without_saved_resolution_has_unknown_attribution_match(self):
        record = assignment(worker_model="sol")
        native = [run("worker-thread-a", "worker-1", "gpt-6.1-sol", "support")]
        self.assertIsNone(companion_attribution.project([record], native)["records"][0]
                          ["companion"]["model_matches_request"])

    def test_companion_control_saves_reported_unknown_claude_pin_and_preserves_unavailable_allowed_pin(self):
        report = self.root / "eutonos-home" / "model-watch" / "claude-models.json"
        report.parent.mkdir(parents=True)
        report.write_text(json.dumps({"profile": "eutonos.claude-model-report.v1",
                          "reported_on": "2026-09-29", "source": "native fixture",
                          "models": [{"id": "claude-fable-5-1", "supported_efforts": ["high"]}]}),
                          encoding="utf-8")
        self.routing(["claude-fable-5-1", "gpt-6-sol"])
        self.cache({"gpt-6.2-sol": ["high"]})
        selected = companion_control.control(self.root, "on", model="claude-fable-5-1", effort="high")
        self.assertEqual(selected["model"], "claude-fable-5-1")
        report.unlink()
        self.assertFalse(companion_control.control(self.root, "off")["enabled"])
        # Unknown watch evidence must not make the owner's unchanged saved pin
        # unusable. This is recovery evidence, not a new available default.
        self.assertTrue(companion_control.control(
            self.root, "on", model="claude-fable-5-1", effort="high")["enabled"])
        self.assertEqual(model_catalog.describe_catalog(self.root)["models"]["claude-fable-5-1"]["basis"],
                         "saved-pin-unknown")
        with self.assertRaises(ValueError):
            companion_control.control(self.root, "on", model="claude-fable-5-2", effort="high")

    def test_unsupported_role_harness_reports_invalid_route(self):
        self.routing(["sol"])
        settings.update_roles(self.root, {"worker": {"options": [
            {"harness": "hermes", "model": "future", "effort": "high"}],
            "preamble": None, "autonomy": "ask"}},
            expected_revision=settings.read_roles(self.root)["revision"])
        state = settings.status(self.root)
        settings.update(self.root, 0, state["revision"], "task_rules", {
            **state["task_rules"], "coding": "worker"})
        resolved = {"harness": "hermes", "model": "future", "model_requested": "future",
                    "effort": "high", "model_selection": None}
        with mock.patch.object(role_router, "resolve_role", return_value=resolved):
            self.assertEqual(settings_cli.route(self.root, "coding", "gpt-6-sol")["basis"],
                             "task-role-harness-unsupported")

    def test_picker_preserves_pins_escapes_labels_and_updates_efforts_on_model_change(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is unavailable")
        source = Path(__file__).resolve().parents[1] / "src/eutonos/dashboard_static/app.js"
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8'),handlers=[];
const models={sol:{harness:'codex',supported_efforts:['high','ultra'],selection_kind:'family',label:'Sol',resolved:'gpt-6.1-sol'},
 'gpt-6-sol':{harness:'codex',supported_efforts:['high'],selection_kind:'pin',resolved:'gpt-6-sol'},
 'gpt-7-fable':{harness:'codex',supported_efforts:['high'],selection_kind:'pin',resolved:'gpt-7-fable'}};
const catalog={models},state={settings:{model_catalog:catalog,roles:{worker:{options:[{harness:'codex',model:'gpt-6-sol',effort:'high'}]}}},settingsDrafts:{}};
const ctx=vm.createContext({state,Object,Array,Set,String,Number,Boolean,
 esc:x=>String(x).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'),
 shortModel:x=>x,
 saveSettings:()=>{},content:{addEventListener:(event,fn)=>handlers.push([event,fn])},render:()=>{ctx.renders=(ctx.renders||0)+1}});
vm.runInContext(source.split('\n').find(line=>line.startsWith('function settingOptions')),ctx);
vm.runInContext(source.slice(source.indexOf('function crossTalkModelsByHarness'),source.indexOf('function crossTalkRoleUsedByTasks')),ctx);
vm.runInContext(source.slice(source.indexOf('function crossTalkRolesFromDom'),source.indexOf('content.addEventListener("click",event=>{',source.indexOf('function crossTalkRolesFromDom'))),ctx);
const call=expression=>vm.runInContext(expression,ctx);
assert.equal(call('crossTalkRoleDefaultOption(state.settings.model_catalog).model'),'sol');
assert.equal(call('crossTalkRoleDefaultOption({models:{"gpt-7-fable":state.settings.model_catalog.models["gpt-7-fable"]}}).model'),'');
ctx.option={harness:'codex',model:'gpt-5.6-sol',effort:'high'};
let html=call('crossTalkRoleOptionRow("worker",option,0,1,crossTalkModelsByHarness(state.settings.model_catalog),state.settings.model_catalog)');
assert.match(html,/value="gpt-5.6-sol" selected disabled/);
assert.equal(ctx.option.model,'gpt-5.6-sol');
ctx.bad={models:{sol:{...models.sol,label:'<img src=x onerror=1>'}}};
html=call('crossTalkRoleOptionRow("worker",{harness:"codex",model:"sol"},0,1,crossTalkModelsByHarness(bad),bad)');
assert.doesNotMatch(html,/<img/);assert.match(html,/&lt;img/);
const target={dataset:{roleName:'worker',roleOptionIndex:'0',roleOptionField:'model'},value:'sol',matches:selector=>selector==='[data-role-option-field]'};
handlers.filter(([event])=>event==='change').forEach(([,fn])=>fn({target}));
assert.equal(ctx.renders,1);assert.equal(state.settingsDrafts.roles.worker.options[0].model,'sol');
assert.equal(state.settings.roles.worker.options[0].model,'gpt-6-sol');
html=call('crossTalkRoleOptionRow("worker",state.settingsDrafts.roles.worker.options[0],0,1,crossTalkModelsByHarness(state.settings.model_catalog),state.settings.model_catalog)');
assert.match(html,/value="ultra"/);
'''
        result = subprocess.run([node, "-e", script, str(source)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
