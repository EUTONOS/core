"""Worker admission refuses Haiku early while retaining owner choices and old settings."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from eutonos import settings


class AwrWorkerAdmissionTests(unittest.TestCase):
    HAIKU = "claude-haiku-4-5-20251001"

    def role(self, model=None):
        return {"options": [{"harness": "claude", "model": model or self.HAIKU, "effort": None}],
                "preamble": None, "autonomy": "ask"}

    def test_worker_and_discovery_defaults_continue_independent_work_after_denial(self):
        for role_name in ("worker-routine", "discovery"):
            with self.subTest(role=role_name):
                template = settings.effective_brief_template(role_name, {"brief_template": ""})
                self.assertIn("skip that step, report it, and continue independent authorized work", template)
                self.assertIn("Never route around the denial.", template)
                self.assertNotIn("stop and report it", template)
        self.assertEqual(settings.effective_brief_template("discovery", {"brief_template": "Owner's template"}),
                         "Owner's template")

    def test_fresh_haiku_worker_save_reports_early_reason(self):
        with self.assertRaisesRegex(ValueError, "not supported for workers"):
            settings.validate_roles({"worker": self.role()})

    def test_preserved_pin_cannot_bypass_worker_policy_on_save(self):
        roles = {"worker": self.role()}
        with self.assertRaisesRegex(ValueError, "cannot run auto mode"):
            settings.validate_roles(roles, preserved=roles)

    def test_legacy_future_haiku_pin_is_retained_on_read(self):
        roles = {"worker": self.role("claude-haiku-5-20271001")}
        self.assertEqual(settings.validate_roles(roles, allow_unavailable=True)["worker"]["options"],
                         roles["worker"]["options"])

    def test_haiku_5_5_is_accepted_as_a_worker_and_older_haiku_still_is_not(self):
        accepted = {"worker": self.role("claude-haiku-5-5")}
        self.assertEqual(settings.validate_roles(accepted, allow_unavailable=True)["worker"]["options"],
                         accepted["worker"]["options"])
        self.assertIsNone(settings.worker_model_disabled_reason("claude", "claude-haiku-5-5"))
        for model in ("claude-haiku-4-5-20251001", "claude-haiku-4-5", "claude-haiku-5", "claude-3-haiku-20240307"):
            with self.subTest(model=model):
                self.assertIn("cannot run auto mode", settings.worker_model_disabled_reason("claude", model))

    def test_owner_orchestrator_choice_is_not_worker_admission(self):
        roles = {"orchestrator": self.role()}
        self.assertEqual(settings.validate_roles(roles)["orchestrator"]["options"],
                         roles["orchestrator"]["options"])
        self.assertEqual(settings._validate_role_option(self.role()["options"][0])["model"], self.HAIKU)

    def test_legacy_saved_worker_remains_readable_and_diagnosed_without_write(self):
        with tempfile.TemporaryDirectory(prefix="awr-worker-admission-") as temp:
            root = Path(temp)
            path = root / ".eutonos-local" / "settings.json"
            path.parent.mkdir()
            legacy = settings._local(root)
            legacy["roles"] = {"old-worker": self.role()}
            path.write_text(json.dumps(legacy), encoding="utf-8")
            before = path.read_bytes()
            readback = settings.read_roles(root)
            self.assertEqual(readback["roles"]["old-worker"]["options"], self.role()["options"])
            diagnostic = next(d for d in readback["diagnostics"] if d["role"] == "old-worker")
            self.assertEqual((diagnostic["option"], diagnostic["code"]), (1, "worker-model-disabled"))
            self.assertIn("cannot run auto mode", diagnostic["reason"])
            with self.assertRaisesRegex(ValueError, "not supported for workers"):
                settings.update_roles(root, readback["roles"], expected_revision=readback["revision"])
            self.assertEqual(path.read_bytes(), before)
            replacement = readback["roles"]
            replacement["old-worker"] = self.role("claude-sonnet-5")
            saved = settings.update_roles(root, replacement, expected_revision=readback["revision"])
            self.assertEqual(saved["roles"]["old-worker"]["options"][0]["model"], "claude-sonnet-5")
            self.assertFalse(settings.read_roles(root).get("diagnostics"))

    def test_worker_picker_disables_haiku_and_preserves_owner_pin_choices(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for dashboard behavior checks")
        path = Path(__file__).parents[1] / "src/eutonos/dashboard_static/app.js"
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8'),box={};vm.createContext(box);
const fn=name=>{const i=source.indexOf('function '+name+'(');assert(i>=0,name);let d=0,j=source.indexOf('){',i)+1;for(;j<source.length;j++){if(source[j]==='{')d++;else if(source[j]==='}'&&--d===0)break}return source.slice(i,j+1)};
box.esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));box.shortModel=v=>String(v||'Unknown');
vm.runInContext(['settingOptions','crossTalkModelsByHarness','crossTalkEffortsForModel','crossTalkModelChoiceLabel','autoModeSupported','autoModeRefused','crossTalkWorkerModelDisabledReason','crossTalkRoleModelOptions','crossTalkRoleOptionRow','modelSelectOptions','refreshModelMenus'].map(fn).join('\n'),box);
const haiku='claude-haiku-4-5-20251001',catalog={models:{
 [haiku]:{harness:'claude',available:true,selection_kind:'pin',resolved:haiku,supported_efforts:['low']},
 'claude-sonnet-5':{harness:'claude',available:true,supported_efforts:['low']},
 'gpt-5.6-sol':{harness:'codex',available:true,selection_kind:'pin',resolved:'gpt-5.6-sol',supported_efforts:['medium']}
}},choices=box.crossTalkModelsByHarness(catalog);
assert(choices.claude.includes(haiku)); // global catalog stays intact
const row=box.crossTalkRoleOptionRow('worker',{harness:'claude',model:haiku},0,1,choices,catalog);
assert(new RegExp('<option value="'+haiku+'" selected disabled').test(row));
assert(row.includes('not supported for workers'));assert(row.includes('The saved choice is retained until you replace it.'));
assert(/<option value="claude-sonnet-5"[^>]*>/.test(row));assert(!/<option value="claude-sonnet-5"[^>]*disabled/.test(row));
const owner=box.crossTalkRoleOptionRow('orchestrator',{harness:'claude',model:haiku},0,1,choices,catalog);
assert(!new RegExp('<option value="'+haiku+'"[^>]*disabled').test(owner));
const pin=box.crossTalkRoleOptionRow('worker',{harness:'codex',model:'gpt-5.6-sol'},0,1,choices,catalog);
assert(/<option value="gpt-5.6-sol" selected[^>]*>/.test(pin));assert(!/<option value="gpt-5.6-sol"[^>]*disabled/.test(pin));
assert(box.crossTalkWorkerModelDisabledReason('worker','claude','claude-haiku-5-5')==='');
assert(box.crossTalkWorkerModelDisabledReason('worker','claude',haiku).includes('cannot run auto mode'));
assert(box.crossTalkWorkerModelDisabledReason('worker','claude','claude-sonnet-5-5')==='');
assert(box.crossTalkWorkerModelDisabledReason('worker','claude','claude-3-haiku-20240307').includes('cannot run auto mode'));
assert(box.crossTalkWorkerModelDisabledReason('worker','codex','gpt-6-luna')==='');
// Live catalog updates must retain the same admission rule as initial render.
const modelNode=(role,harness,model)=>({value:model,innerHTML:'',closest:()=>({dataset:{roleName:role},querySelector:selector=>selector.includes('harness')?{value:harness}:null})});
const worker=modelNode('worker','claude',haiku),primary=modelNode('orchestrator','claude',haiku),oldPin=modelNode('worker','codex','gpt-5.6-sol'),unavailable=modelNode('worker','codex','old-exact-pin');
box.content={querySelectorAll:()=>[worker,primary,oldPin,unavailable],querySelector:()=>null};box.refreshModelChecks=()=>{};
box.refreshModelMenus(catalog,Object.keys(catalog.models));
assert(new RegExp('<option value="'+haiku+'" selected disabled').test(worker.innerHTML));
assert(!new RegExp('<option value="'+haiku+'"[^>]*disabled').test(primary.innerHTML));
assert(/<option value="gpt-5.6-sol" selected[^>]*>/.test(oldPin.innerHTML));assert(!/<option value="gpt-5.6-sol"[^>]*disabled/.test(oldPin.innerHTML));
assert(/<option value="old-exact-pin" selected disabled/.test(unavailable.innerHTML));
assert(!new RegExp('<option value="'+haiku+'"[^>]*disabled').test(box.modelSelectOptions(choices.claude,haiku,catalog)));
// Catalog-provided labels and IDs remain escaped in both render paths.
const hostile='claude-haiku-"<bad>',evil={models:{[hostile]:{harness:'claude',available:true}}};
const escaped=box.crossTalkRoleModelOptions('worker','claude',[hostile],hostile,evil);
assert(escaped.includes('&quot;&lt;bad&gt;'));assert(!escaped.includes('"<bad>'));
'''
        result = subprocess.run([node, "-e", script, str(path)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_closeout_hook_control_preserves_custom_sets_and_stop_reminder(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for dashboard behavior checks")
        path = Path(__file__).parents[1] / "src/eutonos/dashboard_static/app.js"
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8'),box={};vm.createContext(box);
const fn=name=>{const i=source.indexOf('function '+name+'(');assert(i>=0,name);let d=0,j=source.indexOf('){',i)+1;for(;j<source.length;j++){if(source[j]==='{')d++;else if(source[j]==='}'&&--d===0)break}return source.slice(i,j+1)};
box.esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));box.settingsFeedback=()=>'';
vm.runInContext('const HOOK_TRIGGERS=["message","session_start","edit_nudge","stop_check","long_turn"],HOOK_TRIGGER_LABELS={},BUILTIN_HOOK_SET_NAME="eutonos-default",HOOK_BUILT_IN_ROLE_NAMES=["orchestrator"];'+['crossTalkRolesDict','crossTalkHooksDraft','crossTalkHooksRoleNames','crossTalkHooksSection','settingValue'].map(fn).join('\n'),box);
const hooks={enabled:true,closeout_check:false,active_set:'custom',role_enabled:{worker:false},trigger_enabled:{message:true,session_start:true,edit_nudge:true,stop_check:false,long_turn:true},edit_nudge_rate_limit_minutes:10,long_turn_threshold:20,sets:{'eutonos-default':{roles:{}},custom:{roles:{default:{message:'Keep <owner> text',stop_check:'Optional reminder'}}}}};
box.state={settings:{hooks},settingsDrafts:{},settingsSection:'roles',hooks:{recent:[]}};
let html=box.crossTalkHooksSection({},box.state.settings);
assert(/data-setting="hooks-closeout-check"\s*>/.test(html));assert(html.includes('Keep &lt;owner&gt; text'));assert(html.includes('Optional reminder'));
assert(/data-hooks-trigger-enabled="stop_check"\s*>/.test(html));assert(html.includes('It never checks closeout proof'));
const nodes={'[data-setting="hooks-enabled"]':{checked:false},'[data-setting="hooks-closeout-check"]':{checked:false},'[data-setting="hooks-active-set"]':{value:'custom'}};
for(const [trigger,checked] of Object.entries(hooks.trigger_enabled))nodes['[data-hooks-trigger-enabled="'+trigger+'"]']={checked};
box.content={querySelector:selector=>nodes[selector]||null};
let payload=box.settingValue('hooks');assert.equal(payload.enabled,false);assert.equal(payload.closeout_check,false);assert.equal(payload.trigger_enabled.stop_check,false);
assert.equal(payload.sets.custom.roles.default.message,'Keep <owner> text');assert.equal(payload.sets.custom.roles.default.stop_check,'Optional reminder');assert.equal(payload.role_enabled.worker,false);assert.equal(payload.active_set,'custom');
nodes['[data-setting="hooks-closeout-check"]'].checked=true;payload=box.settingValue('hooks');assert.equal(payload.closeout_check,true);assert.equal(payload.enabled,false);assert.equal(payload.trigger_enabled.stop_check,false);
delete nodes['[data-setting="hooks-closeout-check"]'];assert.equal(box.settingValue('hooks').closeout_check,false); // absent DOM keeps saved false
box.state.settingsDrafts={};box.state.settings={hooks:{...hooks}};delete box.state.settings.hooks.closeout_check;
html=box.crossTalkHooksSection({},box.state.settings);assert(/data-setting="hooks-closeout-check" checked/.test(html));assert.equal(box.settingValue('hooks').closeout_check,true);
// The existing generic Settings change listener dispatches this new control.
const normalized=source.replace(/\r\n/g,'\n'),marker='content.addEventListener("change",event=>{\n  const input=event.target,section=';
const start=normalized.indexOf(marker);assert(start>=0);const end=normalized.indexOf('\n});',start)+4;let handler,save;
box.content.addEventListener=(event,callback)=>{handler=callback};box.saveSettings=section=>{save=section};
vm.runInContext(normalized.slice(start,end),box);
handler({target:{closest:()=>({dataset:{settingsSection:'hooks'}}),matches:selector=>selector.startsWith('[data-setting],')}});assert.equal(save,'hooks');
'''
        result = subprocess.run([node, "-e", script, str(path)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
