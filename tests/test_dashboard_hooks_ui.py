"""Role hooks (AWR role hooks, board slice slice-d948010a127d) dashboard settings UI,
executed by the shipped JS.

Mirrors test_dashboard_crosstalk_ui.py's run_js/skip-if-no-node pattern and its
fn()-by-name / el()/match() DOM-stub extraction techniques. Note: `files("eutonos")`
resolves via the installed package location, which in a worktree may point at the
main checkout rather than this worktree's edited copy until the branch is
merged/reinstalled -- run these from an environment where the installed "eutonos"
package is this checkout.

The backend half (settings.py's hooks section, dashboard_cli.py's endpoints,
role_hooks.py) is covered elsewhere; this file only covers the frontend: the new
"Role hooks" settings section, its nav wiring, its draft/mutator functions, and
the removal of the retired per-role "reminder" field.
"""
import shutil
import subprocess
import unittest
from importlib.resources import files


class DashboardHooksUITests(unittest.TestCase):
    def run_js(self, script):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for dashboard behavior checks")
        path = files("eutonos").joinpath("dashboard_static", "app.js")
        syntax = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, timeout=15)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        result = subprocess.run([node, "-e", script, str(path)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_nav_and_settings_subnav_wire_hooks(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for dashboard behavior checks")
        index_path = files("eutonos").joinpath("dashboard_static", "index.html")
        html = index_path.read_text(encoding="utf-8")
        # Role hooks shows under the Roles link (one nav entry, two sections), inside the existing Settings subnav.
        self.assertIn('data-settings-nav="roles"', html)
        self.assertNotIn('data-settings-nav="hooks"', html)

    def test_roles_nav_shows_roles_and_hooks_and_hooks_alone_maps_to_itself(self):
        """The "roles" nav fans out to ["roles","hooks"]; "hooks" on its own
        still falls back to ["hooks"]."""
        self.run_js(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8'),box={};vm.createContext(box);
const start=source.indexOf('function settingsSectionsForNav(');
vm.runInContext(source.slice(start,source.indexOf('function syncSettingsNav(')),box);
assert.deepEqual(box.settingsSectionsForNav('hooks'),['hooks']);
assert.deepEqual(box.settingsSectionsForNav('roles'),['roles','hooks']);
const sections=[{dataset:{settingsSection:'hooks'},hidden:false},{dataset:{settingsSection:'roles'},hidden:false},{dataset:{settingsSection:'routing'},hidden:false}];
box.state={view:'settings',settingsSection:'hooks'};
box.content={querySelectorAll:()=>sections};
box.applySettingsSection();
assert.equal(sections[0].hidden,false);assert.equal(sections[1].hidden,true);assert.equal(sections[2].hidden,true);
''')

    def test_setting_value_hooks_reads_dom_and_falls_back_to_draft(self):
        """settingValue('hooks') scans the DOM for the simple scalar-ish fields
        (enabled/trigger_enabled/limits/active_set), the same way the "routing"
        branch already does, but pulls role_enabled/sets straight
        off the draft (crossTalkHooksDraft) rather than re-scanning them -- the
        same "list mutators write straight to the draft, DOM-scanning mutators
        only read simple scalar fields" split the file already uses for its
        other settings sections vs. its own list-shaped fields."""
        self.run_js(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8'),box={};vm.createContext(box);
function el(attrs,extra){const dataset={};for(const k in attrs)if(k.startsWith('data-'))dataset[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=attrs[k];return {attrs,dataset,checked:false,value:'',...extra}}
function match(node,sel){let ck=false;if(sel.endsWith(':checked')){ck=true;sel=sel.slice(0,-8)}const m=sel.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/);const [,attr,val]=m;if(!(attr in node.attrs))return false;if(val!==undefined&&node.attrs[attr]!==val)return false;if(ck&&!node.checked)return false;return true}
box.content={querySelector:sel=>box._els.find(n=>match(n,sel))||null,querySelectorAll:sel=>box._els.filter(n=>match(n,sel))};
const start=source.indexOf('const HOOK_TRIGGERS=');
vm.runInContext(source.slice(start,source.indexOf('function settingsPresentation(')),box);
box.state={settingsDrafts:{},settings:{hooks:{enabled:false,active_set:'eutonos-default',role_enabled:{worker:false},
  trigger_enabled:{message:true,session_start:true,edit_nudge:true,stop_check:false,long_turn:true},
  edit_nudge_rate_limit_minutes:10,long_turn_threshold:20,sets:{'eutonos-default':{roles:{}}}}}};
box._els=[
  el({'data-setting':'hooks-enabled'},{checked:true}),
  el({'data-hooks-trigger-enabled':'message'},{checked:true}),
  el({'data-hooks-trigger-enabled':'session_start'},{checked:false}),
  el({'data-hooks-trigger-enabled':'edit_nudge'},{checked:true}),
  el({'data-hooks-trigger-enabled':'stop_check'},{checked:true}),
  el({'data-hooks-trigger-enabled':'long_turn'},{checked:true}),
  el({'data-hooks-field':'edit_nudge_rate_limit_minutes'},{value:'15'}),
  el({'data-hooks-field':'long_turn_threshold'},{value:''}),
  el({'data-setting':'hooks-active-set'},{value:'eutonos-default'}),
];
const value=box.settingValue('hooks');
assert.equal(value.enabled,true);
assert.deepEqual(value.trigger_enabled,{message:true,session_start:false,edit_nudge:true,stop_check:true,long_turn:true});
assert.equal(value.edit_nudge_rate_limit_minutes,15);
assert.equal(value.long_turn_threshold,20); // blank input falls back to the draft's current value
assert.equal(value.active_set,'eutonos-default');
assert.deepEqual(value.role_enabled,{worker:false}); // pulled straight from the draft, never re-scanned
assert.deepEqual(value.sets,{'eutonos-default':{roles:{}}});
// settingValue is a pure read: it never mutates the draft it started from.
assert.equal(box.state.settingsDrafts.hooks.enabled,false);
''')

    def test_settings_revision_key_and_merge_for_hooks(self):
        self.run_js(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8'),box={};vm.createContext(box);
const start=source.indexOf('const SETTINGS_REVISION_KEY=');
const script=source.slice(start,source.indexOf('const settingsSavers=')).replace('const SETTINGS_REVISION_KEY=','var SETTINGS_REVISION_KEY=');
vm.runInContext(script,box);
assert.equal(box.SETTINGS_REVISION_KEY.hooks,'hooks_revision');
assert.equal(box.SETTINGS_REVISION_KEY.roles,'roles_revision');
const merged=box.mergeSettingsResult({hooks:{enabled:false},hooks_revision:'r1',other:'keep'},'hooks',{hooks:{enabled:true},revision:'r2'});
assert.deepEqual(merged,{hooks:{enabled:true},hooks_revision:'r2',other:'keep'});
''')

    def test_delivery_form_has_explicit_save_and_independent_revision(self):
        self.run_js(r'''const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8'),box={};vm.createContext(box);
const start=source.indexOf('const SETTINGS_REVISION_KEY=');
vm.runInContext(source.slice(start,source.indexOf('const settingsSavers=')).replace('const SETTINGS_REVISION_KEY=','var SETTINGS_REVISION_KEY='),box);
assert.equal(box.SETTINGS_REVISION_KEY.delivery,'delivery_revision');
const merged=box.mergeSettingsResult({revision:'other',delivery_revision:0,ram:{capture:true}},'delivery',{delivery:{mode:'commit-only'},revision:1});
assert.equal(merged.delivery_revision,1);assert.equal(merged.revision,'other');assert.equal(merged.ram.capture,true);
box.state={settingsDrafts:{}};box.esc=v=>String(v);box.settingOptions=()=>'';box.settingsFeedback=()=>'';
const i=source.indexOf('function gitDeliverySettings('),j=source.indexOf('// Cross-app talk: the Crosstalk owner card plus',i);
vm.runInContext(source.slice(i,j),box);
const html=box.gitDeliverySettings({delivery:{mode:'manual'},delivery_revision:0});
assert(html.includes('data-settings-save="delivery"'));assert(html.includes('data-delivery-field="mode"'));assert(!html.includes('data-setting="delivery'));
assert(html.includes('Participant'));assert(html.includes('Accepted baseline commit'));assert(!html.includes('data-delivery-config'));
const valueStart=source.indexOf('function settingValue('),valueEnd=source.indexOf('function settingsPresentation(',valueStart);
vm.runInContext(source.slice(valueStart,valueEnd),box);
const fields={project_id:'p',participant:'rick',mode:'commit-only',remote_url:'/tmp/remote.git',remote_ref:'refs/heads/main',branch:'refs/heads/main',canonical_base:'pin',records_ref:'refs/heads/records/rick',allowed_paths:'docs/\ncode.py',checks:'[["python","check.py"]]'};
box.content={querySelector:sel=>({value:fields[sel.match(/"([^"]+)"/)[1]]})};
const config=box.settingValue('delivery');assert.equal(config.profile,'eutonos.git-delivery.config.v1');assert.deepEqual(config.allowed_paths,['docs/','code.py']);assert.deepEqual(config.checks,[['python','check.py']]);
''')

    def test_reminder_ui_fully_removed(self):
        """The retired per-role "reminder" field (moved to hook sets; the backend
        no longer accepts or reads settings.roles[*].reminder) has no surviving
        UI: no data-role-reminder attribute anywhere in app.js, and the new-role
        object literal in crossTalkAddRole no longer seeds a "reminder" key."""
        app_path = files("eutonos").joinpath("dashboard_static", "app.js")
        source = app_path.read_text(encoding="utf-8")
        self.assertNotIn("data-role-reminder", source)
        fn_start = source.index("function crossTalkAddRole(")
        depth = 0
        end = source.index("{", fn_start)
        for k in range(end, len(source)):
            if source[k] == "{":
                depth += 1
            elif source[k] == "}":
                depth -= 1
                if depth == 0:
                    end = k
                    break
        add_role_source = source[fn_start:end + 1]
        self.assertNotIn("reminder:", add_role_source)
        self.assertNotIn("reminder", source[source.index("function settingsFocus("):source.index("function settingsFocus(") + 400])

    def test_duplicate_hooks_set_produces_new_name_and_copies_texts(self):
        self.run_js(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8'),box={};vm.createContext(box);
const fn=name=>{const i=source.indexOf('function '+name+'(');assert(i>=0,name);let d=0,j=source.indexOf('{',i);for(;j<source.length;j++){if(source[j]==='{')d++;else if(source[j]==='}'&&--d===0)break}return source.slice(i,j+1)};
vm.runInContext([fn('crossTalkHooksDraft'),fn('crossTalkAddHooksSet')].join('\n'),box);
box.saveSettings=()=>{};box.render=()=>{};box.toast=()=>{};
box.state={settingsDrafts:{},hooksEditingSet:null,settings:{hooks:{enabled:true,active_set:'eutonos-default',role_enabled:{},trigger_enabled:{},
  edit_nudge_rate_limit_minutes:10,long_turn_threshold:20,
  sets:{'eutonos-default':{roles:{default:{message:'Stay in scope.',session_start:null,edit_nudge:null,stop_check:null,long_turn:null}}}}}}};
const created=box.crossTalkAddHooksSet('eutonos-default');
assert.notEqual(created,'eutonos-default');
const draft=box.state.settingsDrafts.hooks;
assert(created in draft.sets);
assert.deepEqual(draft.sets[created].roles.default,draft.sets['eutonos-default'].roles.default);
assert.equal(box.state.hooksEditingSet,created); // duplicating switches the editor to the new copy
''')

    def test_builtin_set_editing_controls_render_disabled(self):
        """eutonos-default is read-only: its rename input/button, delete button
        and set-text textareas render disabled, but Duplicate always stays
        enabled (settings.validate_hooks rejects any write that changes it, so
        the only way to edit it is to duplicate it into a custom set first)."""
        self.run_js(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8'),box={};vm.createContext(box);
const fn=name=>{const i=source.indexOf('function '+name+'(');assert(i>=0,name);let d=0,j=source.indexOf('{',i);for(;j<source.length;j++){if(source[j]==='{')d++;else if(source[j]==='}'&&--d===0)break}return source.slice(i,j+1)};
const start=source.indexOf('const HOOK_TRIGGERS=');
const end=source.indexOf('function settingValue(section){');
box.esc=v=>String(v??'').replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
box.when=()=>'';box.queueMicrotask=()=>{};
vm.runInContext([fn('crossTalkRolesDict'),fn('settingsFeedback'),source.slice(start,end)].join('\n'),box);
box.state={settingsDrafts:{},settingsFeedback:{},settingsSection:'hooks',hooksEditingSet:'eutonos-default',hooks:{recent:null,recentLoading:false,recentError:null}};
const value={roles:{},hooks:{enabled:true,active_set:'eutonos-default',role_enabled:{},trigger_enabled:{},edit_nudge_rate_limit_minutes:10,long_turn_threshold:20,
  sets:{'eutonos-default':{roles:{default:{message:'Stay in scope.',session_start:null,edit_nudge:null,stop_check:null,long_turn:null}}}}}};
const html=box.crossTalkHooksSection({},value);
assert(html.includes('data-settings-section="hooks"'));
const editor=html.slice(html.indexOf('Edit a set'));
assert(/data-hooks-set-rename-input[^>]*disabled/.test(editor));
assert(/data-hooks-set-rename="eutonos-default"[^>]*disabled/.test(editor));
assert(/data-hooks-set-delete="eutonos-default"[^>]*disabled/.test(editor));
assert(/data-hooks-set-text[^>]*disabled/.test(editor));
assert(!/data-hooks-set-duplicate="eutonos-default"[^>]*disabled/.test(editor));
''')

    def test_project_switch_clears_hooks_recent_cache(self):
        """Finding 5 (Codex verification, 2026-09-28): the recent-firings cache
        (state.hooks.recent/recentLoading/recentError) is a single flat object,
        not keyed by project -- so switching projects must clear it, otherwise
        the newly-selected project would briefly (or, if its own fetch never
        resolves, indefinitely) show the PREVIOUS project's cached rows. This
        extracts the project <select> "change" handler's own body (it is an
        inline arrow function, not a named one, so it cannot use the fn()
        helper the other tests here use) and confirms it resets state.hooks
        exactly like it already resets state.settings/state.metricProjection/etc."""
        self.run_js(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8'),box={};vm.createContext(box);
const marker='select.addEventListener("change",()=>{';
const firstMarkerStart=source.indexOf(marker);
assert(firstMarkerStart>=0,'project select change handler not found');
const markerStart=source.indexOf(marker,firstMarkerStart+1);
assert(markerStart>=0,'project select change handler not found (only one match)');
const braceStart=markerStart+marker.length-1;
let depth=0,end=-1;
for(let i=braceStart;i<source.length;i++){
  if(source[i]==='{')depth++;
  else if(source[i]==='}'){depth--;if(depth===0){end=i;break}}
}
assert(end>=0,'unbalanced handler body');
const handlerBody=source.slice(braceStart+1,end);
box.select={value:'1'};
box.render=()=>{};
box.refreshMemorySnapshot=()=>{};
box.workHashId=()=> 'work-target-123';
box.workRoutePending=false;
box.state={project:0,view:'overview',overviewCampaignIndex:3,campaignPage:2,settingsFeedback:{stale:true},
  settings:{stale:true},settingsProject:0,settingsDrafts:{stale:true},discardSettingsDraft:'stale',
  metricProjection:{stale:true},timelineScroll:42,
  hooks:{recent:[{t:'project-0-row'}],recentLoading:true,recentError:'project-0-error'}};
vm.runInContext('(function(){'+handlerBody+'})()',box);
assert.equal(box.state.project,1);
assert.deepEqual(box.state.hooks,{recent:null,recentLoading:false,recentError:null});
assert.equal(box.workRoutePending,true,'switching projects must re-arm the active work-target route');
''')

    def test_set_hooks_role_enabled_toggles_without_touching_roles_draft(self):
        """crossTalkSetHooksRoleEnabled writes hooks.role_enabled on the hooks
        draft only -- it must never touch state.settingsDrafts.roles, even
        though the identical data-role-hooks-enabled checkbox is rendered
        physically inside each role card (crossTalkRoleCard)."""
        self.run_js(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8'),box={};vm.createContext(box);
const fn=name=>{const i=source.indexOf('function '+name+'(');assert(i>=0,name);let d=0,j=source.indexOf('{',i);for(;j<source.length;j++){if(source[j]==='{')d++;else if(source[j]==='}'&&--d===0)break}return source.slice(i,j+1)};
vm.runInContext([fn('crossTalkHooksDraft'),fn('crossTalkSetHooksRoleEnabled')].join('\n'),box);
box.saveSettings=()=>{};
box.state={settingsDrafts:{roles:{director:{options:[]}}},settings:{hooks:{enabled:true,active_set:'eutonos-default',role_enabled:{},trigger_enabled:{},edit_nudge_rate_limit_minutes:10,long_turn_threshold:20,sets:{}}}};
box.crossTalkSetHooksRoleEnabled('director',false);
assert.equal(box.state.settingsDrafts.hooks.role_enabled.director,false);
assert.deepEqual(box.state.settingsDrafts.roles,{director:{options:[]}});
''')


if __name__ == "__main__":
    unittest.main()
