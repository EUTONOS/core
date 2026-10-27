"""Cross-app talk (AWR-CT package C) dashboard behavior, executed by the shipped JS.

Mirrors test_dashboard_work_settings_ui.py's run_js/skip-if-no-node pattern.
Note: `files("eutonos")` resolves via the installed package location, which
in a worktree may point at the main checkout rather than this worktree's
edited copy until the branch is merged/reinstalled -- run these from an
environment where the installed "eutonos" package is this checkout.
"""
import shutil
import subprocess
import unittest
from importlib.resources import files


class DashboardCrossTalkUITests(unittest.TestCase):
    def run_js(self, script):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for dashboard behavior checks")
        path = files("eutonos").joinpath("dashboard_static", "app.js")
        syntax = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, timeout=15)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        result = subprocess.run([node, "-e", script, str(path)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_nav_and_settings_subnav_wire_cross_talk(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for dashboard behavior checks")
        index_path = files("eutonos").joinpath("dashboard_static", "index.html")
        html = index_path.read_text(encoding="utf-8")
        self.assertIn('data-view="cross-talk"', html)
        # The machine pages replace the old single Cross-app talk settings tab.
        self.assertNotIn('data-settings-nav="cross_talk"', html)
        for section in ("roles", "m_parts", "m_crosstalk", "m_claude"):
            self.assertIn(f'data-settings-nav="{section}"', html)

    def test_roles_nav_shows_both_roles_and_hooks_settings_sections(self):
        self.run_js(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8'),box={};vm.createContext(box);
const start=source.indexOf('function settingsSectionsForNav(');
vm.runInContext(source.slice(start,source.indexOf('function syncSettingsNav(')),box);
assert.deepEqual(box.settingsSectionsForNav('roles'),['roles','hooks']);
assert.deepEqual(box.settingsSectionsForNav('ram'),['ram']);
const sections=[{dataset:{settingsSection:'hooks'},hidden:false},{dataset:{settingsSection:'roles'},hidden:false},{dataset:{settingsSection:'routing'},hidden:false}];
box.state={view:'settings',settingsSection:'roles'};
box.content={querySelectorAll:()=>sections};
box.applySettingsSection();
assert.equal(sections[0].hidden,false);assert.equal(sections[1].hidden,false);assert.equal(sections[2].hidden,true);
''')

    def test_cross_talk_role_used_by_tasks_filters_backwards(self):
        """crossTalkRoleUsedByTasks(name, taskRules) is the "backwards filter"
        crossTalkRoleCard uses to build each role card's "Used by tasks" line:
        given a role name and the current task_rules mapping, it returns every
        task currently assigned to that role (AWR worker profiles requirement
        5, coordinator review 2026-09-28)."""
        self.run_js(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8'),box={};vm.createContext(box);
const fn=name=>{const i=source.indexOf('function '+name+'(');assert(i>=0,name);let d=0,j=source.indexOf('{',i);for(;j<source.length;j++){if(source[j]==='{')d++;else if(source[j]==='}'&&--d===0)break}return source.slice(i,j+1)};
vm.runInContext(fn('crossTalkRoleUsedByTasks'),box);
const rules={discovery:'worker-routine',coding:'worker-routine',docs:'orchestrator',git:'worker-hard',closeout:'orchestrator'};
assert.deepEqual(box.crossTalkRoleUsedByTasks('worker-routine',rules),['discovery','coding']);
assert.deepEqual(box.crossTalkRoleUsedByTasks('worker-hard',rules),['git']);
assert.deepEqual(box.crossTalkRoleUsedByTasks('orchestrator',rules),['docs','closeout']);
assert.deepEqual(box.crossTalkRoleUsedByTasks('unused-role',rules),[]);
// A missing/undefined taskRules never throws -- crossTalkRoleCard may be asked to
// render before any task_rules value has loaded.
assert.deepEqual(box.crossTalkRoleUsedByTasks('worker-routine',undefined),[]);
''')

    def test_task_rules_role_select_lists_known_roles_and_flags_unknown(self):
        """The Who does what table (jobsTable/jobRow/jobModelOptions, 2026-10-14
        redesign) shows closeout as the main chat, offers family choices before
        exact versions, never lists a model that cannot run auto mode, and omits models of a disabled harness
        (keeping a currently-saved one visible but flagged)."""
        self.run_js(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8'),box={};vm.createContext(box);
const fn=name=>{const i=source.indexOf('function '+name+'(');assert(i>=0,name);let d=0,j=source.indexOf('{',i);for(;j<source.length;j++){if(source[j]==='{')d++;else if(source[j]==='}'&&--d===0)break}return source.slice(i,j+1)};
box.esc=v=>String(v??'').replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
box.state={};
const names=['jobModelOptions'];
// Probe only: the real function depends on the live catalog, so assert on its source shape.
for(const n of names)assert(source.includes('function '+n+'('),n);
assert(source.includes('Latest'));assert(source.includes('Exact versions'));
assert(/jobWorkerModel/.test(fn('jobModelOptions')));
assert(source.includes('cannot run auto mode'));assert(!source.includes('Haiku is never offered'));
vm.runInContext(['autoModeSupported','autoModeRefused','jobWorkerModel'].map(fn).join('\n'),box);
assert(box.jobWorkerModel('claude-haiku-5-5'));assert(box.jobWorkerModel('haiku'));assert(box.jobWorkerModel('claude-sonnet-5-5'));assert(box.jobWorkerModel('gpt-6-luna'));
assert(!box.jobWorkerModel('claude-haiku-4-5-20251001'));assert(!box.jobWorkerModel('claude-3-5-sonnet-20241022'));
assert(source.includes('harness off'));
assert(source.includes('Main chat'));
''')

