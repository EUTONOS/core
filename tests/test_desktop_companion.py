"""Focused metadata/edge/UI fixtures: no installed runtime or live messages."""
from contextlib import closing
import ctypes
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import time
import tkinter as tk
import unittest
from unittest import mock
import zlib

from eutonos import desktop_companion_data as data
from eutonos import desktop_agent_activity as activity
from eutonos import desktop_companion as ui
from eutonos import desktop_companion_runtime as runtime
from eutonos import role_hooks, companion_history


class DesktopCompanionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Never read the live Crosstalk device folder: point it at a temp one, absent by default.
        crosstalk_env = mock.patch.dict(os.environ, {'CROSSTALK_P0_DEVICE_DIR': str(Path(self.temp.name) / 'crosstalk-device')})
        crosstalk_env.start()
        self.addCleanup(crosstalk_env.stop)
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        self.host = Path(self.temp.name) / 'host'
        self.host.mkdir()
        self.now = time.time()
        self.collector = data.Collector(self.root, self.host)

    def put(self, path, value):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')
        return path



    def test_empty_missing_sources_are_unknown_not_saved(self):
        snap = self.collector.collect(self.now)
        self.assertEqual(snap['health'], 'unknown')
        self.assertEqual(snap['records']['ram']['state'], 'missing')
        self.assertEqual(snap['agents']['status'], 'unknown')

    def test_read_only_no_model_no_creation(self):
        before = list(self.root.rglob('*'))
        with mock.patch('subprocess.Popen', side_effect=AssertionError('No launches')), mock.patch('subprocess.run', side_effect=AssertionError('No subprocesses')):
            self.collector.collect(self.now)
        self.assertEqual(before, list(self.root.rglob('*')))








    def test_records_frozen_input_is_not_ram_receipt(self):
        self.put(self.root / '.eutonos-local' / 'workflow' / 'closeout-one.json', {'semantic': {}, 'binding': {}})
        self.assertEqual(self.collector.collect(self.now)['records']['ram']['state'], 'missing')

    def test_failed_ram_effect_alert_and_later_verified_receipt_resolution(self):
        path = self.root / '.eutonos-local' / 'workflow' / 'one.result.json'
        self.put(path, {'profile': 'eutonos.companion-ram-capture.v1', 'outcome': 'error'})
        self.assertTrue(self.collector.collect(time.time())['records']['problems'])
        self.put(path, {'profile': 'eutonos.companion-ram-capture.v1', 'outcome': 'completed', 'current_verified': True})
        result = self.collector.collect(time.time())['records']
        self.assertFalse(result['problems'])
        self.assertEqual(result['ram']['state'], 'verified')

    def test_superseded_and_completed_turn_gap_no_longer_alert(self):
        common = {'profile': 'eutonos.companion-capture-receipt.v1', 'thread_id': 't', 'turn_id': 'u', 'recorded_at': self.now}
        self.put(self.root / '.eutonos-local' / 'capture' / 'gap.json', {**common, 'receipt_id': 'old', 'disposition': 'gap'})
        self.put(self.root / '.eutonos-local' / 'capture' / 'saved.json', {**common, 'receipt_id': 'new', 'disposition': 'captured', 'supersedes': 'old',
                 'counts_completed': True, 'assertion': {'native_proof': 'verified-readback'}})
        records = self.collector.collect(self.now)['records']
        self.assertEqual(records['ram']['state'], 'verified')
        self.assertFalse(records['problems'])

    def test_write_receipt_same_id_retry_resolution_and_locked_db(self):
        dbpath = self.host / 'write-retry.sqlite'
        with closing(sqlite3.connect(dbpath)) as db:
            db.execute('CREATE TABLE requests(id,kind,state,updated_at,result,error,root)')
            db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?)', ('one', 'ram-closeout', 'failed', self.now, '{}', 'failure', str(self.root)))
            db.commit()
        self.assertTrue(self.collector.collect(self.now)['records']['problems'])
        with closing(sqlite3.connect(dbpath)) as db:
            db.execute('UPDATE requests SET state=?,result=?', ('complete', json.dumps({'result': {'current_verified': True}})))
            db.commit()
        record = self.collector.collect(self.now)['records']
        self.assertEqual(record['ram']['state'], 'verified')
        self.assertFalse(record['problems'])
        with closing(sqlite3.connect(dbpath)) as db:
            db.execute('BEGIN EXCLUSIVE')
            record = self.collector.collect(self.now)['records']
            self.assertIn('unavailable-or-locked', [i['code'] for i in record['diagnostics']])

    def test_stale_saved_data_never_healthy(self):
        path = self.root / 'docs' / 'CURRENT_TASKS.md'
        path.parent.mkdir()
        path.write_text('saved')
        os.utime(path, (self.now - 172800, self.now - 172800))
        self.assertEqual(self.collector.collect(self.now)['health'], 'unknown')
        self.assertEqual(self.collector.collect(self.now)['records']['docs']['freshness'], 'stale')

    def test_agents_registered_active_roles_lifecycle_and_stale(self):
        for index, (role, state) in enumerate((('worker-hard', 'working'), ('worker', 'completed'), ('reviewer', 'working'), ('director', 'waiting for owner'), ('mystery', 'working'))):
            self.put(self.root / '.eutonos-local' / 'agent-activity' / f'{index}.json',
                     {'profile': activity.PROFILE, 'id': str(index), 'role': role, 'harness': 'claude', 'state': state, 'updated_at': self.now})
        agents = self.collector.collect(self.now)['agents']
        self.assertEqual(agents['counts']['worker'], {'registered': 2, 'active': 1})
        self.assertEqual(agents['counts']['director'], {'registered': 1, 'active': 1})
        self.assertEqual(agents['counts']['reviewer'], {'registered': 1, 'active': 1})
        self.assertEqual(agents['counts']['unknown'], {'registered': 1, 'active': 1})
        old = self.collector.collect(self.now + 121)['agents']
        self.assertIsNone(old['counts']['worker']['active'])
        self.assertIn('stale / unknown', [row['state'] for row in old['rows']])

    def test_open_session_is_not_active_and_reminder_disabled_still_observes(self):
        with mock.patch.object(role_hooks, '_hooks_config', return_value={'enabled': False}), mock.patch.object(role_hooks, '_resolved_root', return_value=self.root):
            self.assertEqual(role_hooks._dispatch(self.root, {'hook_event_name': 'SessionStart', 'session_id': 's'}, app='claude'), {})
        self.assertIsNone(self.collector.collect(self.now)['agents']['counts']['orchestrator']['active'])
        with mock.patch.object(role_hooks, '_hooks_config', return_value={'enabled': False}), mock.patch.object(role_hooks, '_resolved_root', return_value=self.root):
            role_hooks._dispatch(self.root, {'hook_event_name': 'UserPromptSubmit', 'session_id': 's'}, app='claude')
        self.assertEqual(self.collector.collect(time.time())['agents']['counts']['orchestrator']['active'], 1)

    def test_actual_stop_optional_closeout_finishes_activity_immediately(self):
        payload = {'hook_event_name': 'Stop', 'session_id': 'debt', 'turn_id': 'turn', 'cwd': str(self.root)}
        with mock.patch.object(role_hooks, '_hooks_config', return_value={'enabled': True, 'closeout_check': True}), mock.patch.object(role_hooks, '_text_for', return_value=(None, 'trigger disabled')), mock.patch.object(role_hooks, '_resolved_root', return_value=self.root):
            result = role_hooks._dispatch(self.root, payload, app='claude')
        self.assertEqual(result, {})
        row = json.loads(next((self.root / '.eutonos-local/agent-activity').glob('*.json')).read_text())
        self.assertEqual(row['state'], 'idle / turn finished')

    def test_actual_stop_end_turn_pass_keeps_working_then_idles(self):
        payload = {'hook_event_name': 'Stop', 'session_id': 'spent', 'turn_id': 'turn', 'cwd': str(self.root)}
        with mock.patch.object(role_hooks, '_hooks_config', return_value={'enabled': True, 'closeout_check': True}), mock.patch.object(role_hooks, '_text_for', return_value=('end-turn reminder', 'ok')), mock.patch.object(role_hooks, '_resolved_root', return_value=self.root):
            first = role_hooks._dispatch(self.root, payload, app='claude')
            row_path = next((self.root / '.eutonos-local/agent-activity').glob('*.json'))
            self.assertEqual(first['decision'], 'block')
            self.assertEqual(json.loads(row_path.read_text())['state'], 'working')
            second = role_hooks._dispatch(self.root, {**payload, 'stop_hook_active': True}, app='claude')
        self.assertEqual(second, {})
        row = json.loads(next((self.root / '.eutonos-local/agent-activity').glob('*.json')).read_text())
        self.assertEqual(row['state'], 'idle / turn finished')

    def test_actual_subagent_stop_completes_after_end_turn_pass(self):
        payload = {'hook_event_name': 'SubagentStop', 'session_id': 'parent', 'agent_id': 'child',
                   'turn_id': 'turn', 'cwd': str(self.root)}
        with mock.patch.object(role_hooks, '_hooks_config', return_value={'enabled': True, 'closeout_check': True}), mock.patch.object(role_hooks, '_text_for', return_value=('end-turn reminder', 'ok')), mock.patch.object(role_hooks, '_resolved_root', return_value=self.root):
            first = role_hooks._dispatch(self.root, payload, app='codex')
            row_path = next((self.root / '.eutonos-local/agent-activity').glob('*.json'))
            self.assertEqual(first['decision'], 'block')
            self.assertNotEqual(json.loads(row_path.read_text())['state'], 'completed')
            self.assertEqual(role_hooks._dispatch(self.root, {**payload, 'stop_hook_active': True}, app='codex'), {})
            self.assertEqual(json.loads(row_path.read_text())['state'], 'completed')
            second = role_hooks._dispatch(self.root, payload, app='codex')
        self.assertEqual(second, {})
        self.assertEqual(json.loads(row_path.read_text())['state'], 'completed')

    def test_tracking_off_preserves_reminders_and_existing_activity(self):
        activity.observe(self.root, {'hook_event_name': 'UserPromptSubmit', 'session_id': 'old'}, app='claude', role='worker')
        before = {path: path.read_bytes() for path in (self.root / '.eutonos-local' / 'agent-activity').iterdir()}
        companion_history.set_tracking(self.root, False)
        with mock.patch.object(role_hooks, '_hooks_config', return_value={'enabled': True}), mock.patch.object(role_hooks, '_resolved_root', return_value=self.root), mock.patch.object(role_hooks, '_handle_user_prompt_submit', return_value={'reminder': 'unchanged'}) as reminder:
            result = role_hooks._dispatch(self.root, {'hook_event_name': 'UserPromptSubmit', 'session_id': 'new'}, app='claude')
        self.assertEqual(result, {'reminder': 'unchanged'})
        reminder.assert_called_once()
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertEqual(len(list((self.root / '.eutonos-local' / 'agent-activity').iterdir())), 1)
        agents = self.collector.collect(self.now)['agents']
        self.assertEqual(agents['lifecycle_capture'], 'disabled')
        self.assertIsNone(agents['counts']['worker']['active'])
        self.assertEqual(agents['registration_counts']['observed'], 1)

    def _dashboard_click(self, listening):
        fake = mock.Mock()
        fake._dashboard_listening = lambda: listening
        with mock.patch.object(ui.webbrowser, 'open') as opened, mock.patch.object(ui.subprocess, 'Popen') as popen:
            ui.DesktopCompanion._open_running_or_launch(fake, 'x.bat')
        return opened, popen

    def test_open_dashboard_opens_directly_when_port_listens(self):
        opened, popen = self._dashboard_click(True)
        opened.assert_called_once_with('http://127.0.0.1:8765')
        popen.assert_not_called()

    def test_open_dashboard_falls_back_to_launcher_when_port_closed(self):
        with mock.patch.object(ui.time, 'monotonic', side_effect=[0, 0, ui.DASHBOARD_START_TIMEOUT + 1]), \
                mock.patch.object(ui.time, 'sleep'):
            opened, popen = self._dashboard_click(False)
        opened.assert_not_called()
        popen.assert_called_once()

    def test_open_dashboard_after_cold_start_is_ready(self):
        fake = mock.Mock()
        fake._dashboard_listening = mock.Mock(side_effect=[False, True])
        with mock.patch.object(ui.webbrowser, 'open') as opened, mock.patch.object(ui.subprocess, 'Popen') as popen:
            ui.DesktopCompanion._open_running_or_launch(fake, 'x.bat')
        popen.assert_called_once()
        self.assertIn('EUTONOS_DASHBOARD_OPENED_BY_COMPANION', popen.call_args.args[0][-1])
        opened.assert_called_once_with('http://127.0.0.1:8765')

    def test_open_dashboard_with_launcher_probes_off_ui_thread(self):
        fake = mock.Mock()
        fake.configuration = {'dashboard_launcher': 'x.bat'}
        with mock.patch.object(ui.threading, 'Thread') as thread:
            ui.DesktopCompanion._open_dashboard(fake)
        thread.assert_called_once()
        thread.return_value.start.assert_called_once()

    def test_tracking_unknown_is_bounded_nonwaiting_and_suppresses_capture(self):
        from eutonos.local_lock import local_lock
        with mock.patch('eutonos.companion_history.local_lock', side_effect=TimeoutError):
            self.assertIsNone(companion_history.tracking_enabled_bounded(self.root))
        self.assertTrue(companion_history.tracking_enabled_bounded(self.root))
        path = self.put(self.root / '.eutonos-local' / 'companion-history.json', {'profile': companion_history.PROFILE, 'tracking': {'enabled': False}})
        self.assertIsNone(companion_history.tracking_enabled_bounded(self.root, max_bytes=8))
        path.write_text('{bad json')
        with mock.patch.object(role_hooks, '_hooks_config', return_value={'enabled': False}), mock.patch.object(role_hooks, '_resolved_root', return_value=self.root):
            role_hooks._dispatch(self.root, {'hook_event_name': 'UserPromptSubmit', 'session_id': 'new'}, app='claude')
        self.assertFalse((self.root / '.eutonos-local' / 'agent-activity').exists())
        self.assertEqual(self.collector.collect(self.now)['agents']['lifecycle_capture'], 'unknown')



    def test_sql_query_execution_and_cumulative_payload_are_bounded(self):
        dbpath = self.host / 'write-retry.sqlite'
        with closing(sqlite3.connect(dbpath)) as db:
            db.execute('CREATE TABLE requests(id,kind,state,updated_at,result,error,root)')
            db.executemany('INSERT INTO requests VALUES(?,?,?,?,?,?,?)', [(str(n), 'ram-closeout', 'complete', self.now + n, json.dumps({'data': 'x' * 2048}), None, str(self.root)) for n in range(200)])
            db.commit()
        with mock.patch.object(data, 'TOTAL_BYTES', 8192):
            result = self.collector.collect(self.now)
        self.assertLessEqual(result['io']['bytes'], 8192)
        self.assertIn('byte-limit', [issue['code'] for issue in result['records']['diagnostics']])
        self.assertIn('record-limit', [issue['code'] for issue in result['records']['diagnostics']])
        self.collector.reader.reset()
        with mock.patch.object(data.time, 'monotonic', side_effect=[0] + [1] * 1000):
            self.collector._write_rows(dbpath)
        self.assertIn('query-budget', [issue['code'] for issue in self.collector.reader.issues])
    def test_periodic_refresh_remembers_detail_scroll(self):
        companion = ui.DesktopCompanion.__new__(ui.DesktopCompanion)
        companion.detail_canvas = mock.Mock()
        companion.detail_canvas.winfo_exists.return_value = True
        companion.detail_canvas.yview.return_value = (.42, .68)
        companion._remember_detail_scroll()
        self.assertEqual(companion.detail_scroll, .42)
    def test_standalone_is_explicit_and_default_popup_has_no_compaction(self):
        source = Path(ui.__file__).read_text(encoding='utf-8')
        self.assertIn('if self.standalone:', source)
        self.assertNotIn('self._compact()', source)
        self.assertNotIn('SetForegroundWindow', source)
        with mock.patch.object(role_hooks, '_hooks_config', return_value={'enabled': False}), mock.patch.object(role_hooks, '_resolved_root', return_value=self.root):
            role_hooks._dispatch(self.root, {'hook_event_name': 'Stop', 'session_id': 's', 'stop_hook_active': True}, app='claude')
        self.assertEqual(self.collector.collect(time.time())['agents']['rows'][0]['state'], 'idle / turn finished')

    def test_stop_activity_finishes_under_all_global_role_trigger_gates(self):
        for global_on, role_on, trigger_on in ((False, True, True), (True, False, True), (True, True, False), (True, True, True)):
            with self.subTest(global_on=global_on, role_on=role_on, trigger_on=trigger_on):
                cfg = {'enabled': global_on, 'active_set': 'fixture',
                       'role_enabled': {'orchestrator': role_on}, 'trigger_enabled': {'stop_check': trigger_on},
                       'sets': {'fixture': {'roles': {'orchestrator': {'stop_check': 'Saved stop-check text'}}}}}
                with mock.patch.object(role_hooks, '_hooks_config', return_value=cfg), mock.patch.object(role_hooks, '_resolved_root', return_value=self.root), mock.patch.dict(os.environ, {'EUTONOS_ROLE': 'orchestrator'}):
                    output = role_hooks._dispatch(self.root, {'hook_event_name': 'Stop', 'session_id': 'gated-stop'}, app='claude')
                row = self.collector.collect(time.time())['agents']['rows'][0]
                if global_on and role_on and trigger_on:
                    self.assertEqual(output['decision'], 'block')  # One end-turn pass.
                    self.assertEqual(row['state'], 'working')
                else:
                    self.assertEqual(output, {})
                    self.assertEqual(row['state'], 'idle / turn finished')
    def test_lifecycle_waits_and_stop_block_are_distinct_and_metadata_only(self):
        for event, tool, expected in (('PreToolUse', 'request_user_input', 'waiting for owner'),
                                      ('PreToolUse', 'wait_threads', 'waiting for reply'),
                                      ('Stop', None, 'idle / turn finished'), ('SubagentStop', None, 'completed')):
            activity.observe(self.root, {'hook_event_name': event, 'session_id': 's', 'tool_name': tool, 'prompt': 'secret text'}, app='claude', role='director')
            path = next((self.root / '.eutonos-local' / 'agent-activity').glob('*.json'))
            row = json.loads(path.read_text())
            self.assertEqual(row['state'], expected)
            self.assertNotIn('secret text', path.read_text())
        activity.observe(self.root, {'hook_event_name': 'Stop', 'session_id': 's'}, app='claude', role='director', stop_blocked=True)
        self.assertEqual(json.loads(path.read_text())['state'], 'working')

    def test_explicit_registration_merges_activity_and_unknown_role_stays_unknown(self):
        activity.observe(self.root, {'hook_event_name': 'PreToolUse', 'session_id': 's'}, app='codex', role='worker')
        self.put(self.root / '.eutonos-local' / 'companion-results.json', {'sources': [{'thread_id': 's', 'harness': 'codex', 'role': 'worker', 'work_id': 'slice-one'},
                                                                                               {'thread_id': 'old', 'harness': 'codex', 'role': 'baseline'}]})
        agents = self.collector.collect(time.time())['agents']
        self.assertEqual(agents['counts']['worker'], {'registered': 1, 'active': 1})
        self.assertEqual(agents['counts']['unknown'], {'registered': 1, 'active': None})
        self.assertEqual(agents['registration_counts'], {'explicit': 2, 'observed': 1, 'overlap': 1, 'union': 2})
        observed = next(row for row in agents['rows'] if row['active'])
        self.assertEqual(observed['assignment'], 'slice-one')

    def test_large_results_file_reads_more_than_one_folder_of_native_registrations(self):
        activity.observe(self.root, {'hook_event_name': 'PreToolUse', 'session_id': 's0'}, app='codex', role='worker')
        sources = []
        for number in range(70):
            log = self.put(self.root / 'logs' / f'{number}.jsonl',
                           {'type': 'session_meta', 'payload': {'id': f's{number}'}})
            sources.append({'path': str(log), 'role': 'support', 'experiment_id': 'historical'})
        sources.append(dict(sources[0]))  # Two registrations of one native identity count once.
        cfgpath = self.put(self.root / '.eutonos-local' / 'companion-results.json',
                           {'sources': sources, 'annotations': {'padding': 'x' * data.FILE_BYTES}})
        self.assertGreater(cfgpath.stat().st_size, data.FILE_BYTES)
        agents = self.collector.collect(time.time())['agents']
        self.assertEqual(agents['registration_counts'], {'explicit': 70, 'observed': 1, 'overlap': 1, 'union': 70})
        self.assertEqual(agents['counts']['worker'], {'registered': 1, 'active': 1})
        self.assertEqual(agents['counts']['unknown'], {'registered': 69, 'active': None})
        self.assertNotIn('oversize', [issue['code'] for issue in agents['diagnostics']])
        self.assertNotIn('registration-limit', [issue['code'] for issue in agents['diagnostics']])
        self.assertLessEqual(self.collector.reader.bytes, data.TOTAL_BYTES)

    def test_results_read_cap_and_registration_limit_remain_partial(self):
        cfgpath = self.put(self.root / '.eutonos-local' / 'companion-results.json',
                           {'sources': [], 'padding': 'x' * data.FILE_BYTES})
        reader = data.Reader()
        self.assertIsNotNone(reader.json(cfgpath, max_bytes=data.COMPANION_RESULTS_BYTES))
        self.assertIsNone(reader.json(cfgpath))  # A cached large read cannot bypass the ordinary cap.
        self.assertIn('oversize', [issue['code'] for issue in reader.issues])
        cfgpath.write_bytes(b'x' * (data.COMPANION_RESULTS_BYTES + 1))
        agents = self.collector.collect(self.now)['agents']
        self.assertIn('oversize', [issue['code'] for issue in agents['diagnostics']])
        self.assertEqual(agents['count_basis'], 'unknown')

        self.put(cfgpath, {'sources': [{'thread_id': f's{number}', 'harness': 'codex', 'role': 'support'}
                                       for number in range(data.REGISTRATION_LIMIT + 1)]})
        agents = self.collector.collect(self.now)['agents']
        self.assertEqual(agents['registration_counts']['explicit'], data.REGISTRATION_LIMIT)
        self.assertEqual(agents['count_basis'], 'partial')
        self.assertIn('registration-limit', [issue['code'] for issue in agents['diagnostics']])


    def test_root_selection_does_not_inspect_arbitrary_cwd(self):
        with mock.patch.object(ui.Path, 'cwd', return_value=self.root), mock.patch.dict(os.environ, {'EUTONOS_COMPANION_ROOT': ''}):
            # Source checkout auto-selection is allowed only with its installation manifest.
            with mock.patch.object(ui, '__file__', str(self.root / 'runtime' / 'src' / 'module.py')):
                self.assertIsNone(ui.selected_root())
        self.assertEqual(ui.selected_root(self.root), self.root)

    def test_dashboard_uses_explicit_address_or_existing_launcher(self):
        self.put(self.root / '.eutonos-local' / 'desktop-companion.json', {'dashboard_url': 'http://localhost:9900'})
        self.assertEqual(data.local_configuration(self.root)['dashboard_url'], 'http://localhost:9900')
        self.put(self.root / '.eutonos-local' / 'desktop-companion.json', {'dashboard_url': 'javascript:alert(1)'})
        self.assertIsNone(data.local_configuration(self.root)['dashboard_url'])
        (self.root / 'launch eutonos dashboard.bat').write_text('@echo off')
        self.assertTrue(data.local_configuration(self.root)['dashboard_launcher'])

    def test_actual_launch_builder_preserves_root_host_and_selected_mode(self):
        from types import SimpleNamespace
        options = SimpleNamespace(root=self.root, host=self.host, standalone=True)
        with mock.patch.object(runtime, '_background_interpreter', return_value=Path(sys.executable)), mock.patch.object(runtime.subprocess, 'run', return_value=mock.Mock(returncode=0)):
            command = runtime._launch_command(options, supervise=True)
        self.assertEqual(command[-6:], ['--root', str(self.root), '--host', str(self.host), '--standalone', '--supervise'])
        self.assertIn('eutonos.desktop_companion_runtime', command)
        with mock.patch.object(runtime, '_background_interpreter', return_value=Path(sys.executable)), mock.patch.object(runtime.subprocess, 'run', return_value=mock.Mock(returncode=1)):
            command = runtime._launch_command(options, coordinator=True)
        self.assertEqual(command[-6:], ['--root', str(self.root), '--host', str(self.host), '--standalone', '--coordinator'])
        self.assertEqual(Path(command[2]).name, 'desktop_companion.py')

    def test_packaging_retires_context_and_probe_has_only_read_only_mode(self):
        root = Path(__file__).resolve().parents[1]
        package = (root / 'pyproject.toml').read_text()
        self.assertIn('[project.gui-scripts]', package)
        self.assertIn('eutonos.desktop_companion_runtime:main', package)
        self.assertNotIn('eutonos-codex-context =', package)
        self.assertFalse((root / 'src/eutonos/codex_context_companion.py').exists())
        source = (root / 'src/eutonos/desktop_companion_probe.ps1').read_text()
        self.assertIn('GetWindowThreadProcessId', source)
        for unsafe in ('SendKeys', 'SetForegroundWindow', 'UIAutomationClient', '$Compact'):
            self.assertNotIn(unsafe, source)

    @unittest.skipUnless(sys.platform == 'win32' and not os.environ.get('EUTONOS_COMPANION_NO_GUI_TEST'), 'Windows Tk preview disabled or unavailable')
    def test_actual_widgets_preview(self):
        view = ui.DesktopCompanion.__new__(ui.DesktopCompanion)
        view.root = tk.Tk()
        self.addCleanup(view.root.destroy)
        view.root.overrideredirect(True)
        view.root.attributes('-transparentcolor', ui.windows.TRANSPARENT)
        view.expanded = True
        view.theme = 'light'
        view.theme_mode = 'system'
        view.menu = None
        view.details_open = False
        view.harness = 'claude'
        view.title = 'Fixture'
        view.project_root = self.root
        view.configuration = {}
        view.startup_var = tk.BooleanVar(value=False)
        view.snapshot = {
            'health': 'attention', 'observed_at': time.time(),
            'attention': [{'title': 'RAM write needs inspection', 'detail': 'Queued capture has no recent progress', 'source': str(self.root)}],
            'records': {'docs': {'state': 'saved', 'updated_at': time.time() - 72, 'freshness': 'recent', 'source': str(self.root)},
                        'ram': {'state': 'queued', 'updated_at': time.time() - 1900, 'freshness': 'recent', 'source': str(self.root)}},
            'agents': {'counts': {'orchestrator': {'registered': 2, 'active': 1}, 'worker': {'registered': 8, 'active': 3}, 'director': {'registered': 1, 'active': 0}}, 'rows': []}}
        view._build()
        view.root.geometry('+40+40')
        view.root.update()
        texts = []
        def read(widget):
            if isinstance(widget, tk.Label):
                texts.append(widget.cget('text'))
            for child in widget.winfo_children():
                read(child)
        read(view.root)
        self.assertIn('Workers · 8 registered, ', texts)
        self.assertIn('3 active', texts)
        self.assertFalse(any('Compact now' == text for text in texts))
        output = Path(__file__).resolve().parents[1] / '.eutonos-local' / 'companion-preview.png'
        _capture_window(view.root, output)
        self.assertTrue(output.is_file())
        view.details_open = True
        view.snapshot['agents']['rows'] = [
            {'role': 'worker-hard', 'harness': 'codex', 'state': 'working', 'assignment': 'Companion build', 'updated_at': time.time(), 'active': True},
            {'role': 'director', 'harness': 'claude', 'state': 'completed', 'assignment': 'Review milestone', 'updated_at': time.time()-3600, 'active': False}]
        view._build()
        view.root.update()
        _capture_window(view.root, output.with_name('companion-details-preview.png'))
        view._menu(view.menu_icon)
        view.root.update()
        _capture_window(view.menu, output.with_name('companion-menu-preview.png'))
        view._close_menu()
        for widget in view.root.winfo_children():
            widget.destroy()
        view.root.geometry('390x80')
        view.root.configure(bg='#ffffff')
        holder = tk.Frame(view.root, bg='#ffffff')
        holder.pack()
        for health in ('observed', 'attention', 'unknown'):
            slot = tk.Frame(holder, bg='#ffffff')
            slot.pack(side='left', padx=20, pady=5)
            ring = tk.Canvas(slot, width=36, height=36, bg='#ffffff', highlightthickness=0, bd=0)
            ring.pack()
            view._draw_badge_surface(ring)
            view.ring = ring
            view.expanded = False
            view.snapshot = {'health': health}
            view._render_ring()
            tk.Label(slot, text=health, bg='#ffffff', fg='#717171', font=('Segoe UI', 9)).pack()
        view.root.update()
        _capture_window(view.root, output.with_name('companion-icons-preview.png'))

    @unittest.skipUnless(sys.platform == 'win32' and not os.environ.get('EUTONOS_COMPANION_NO_GUI_TEST'), 'Windows Tk preview disabled or unavailable')
    def test_hamburger_opens_popup_menu_without_touching_the_panel(self):
        view = self._menu_view()
        panel_before = (view.panel, view.root.winfo_width(), view.root.winfo_height(), len(view.panel.winfo_children()))
        with mock.patch.object(type(view), '_rebuild') as rebuild, mock.patch.object(type(view), '_build') as build:
            view.menu_icon.event_generate('<Button-1>')
            view.root.update()
            self.assertIsNotNone(view.menu)
            self.assertTrue(view.menu.winfo_exists())
            self.assertEqual(view.menu.winfo_toplevel(), view.menu)
            view.menu_icon.event_generate('<Button-1>')
            view.root.update()
            self.assertIsNone(view.menu)
            view._menu(view.menu_icon)
            view.menu.event_generate('<Escape>')
            view.root.update()
            self.assertIsNone(view.menu)
            rebuild.assert_not_called()
            build.assert_not_called()
        self.assertEqual(panel_before, (view.panel, view.root.winfo_width(), view.root.winfo_height(), len(view.panel.winfo_children())))

    @unittest.skipUnless(sys.platform == 'win32' and not os.environ.get('EUTONOS_COMPANION_NO_GUI_TEST'), 'Windows Tk preview disabled or unavailable')
    def test_popup_menu_items_run_callbacks_and_close_the_menu(self):
        view = self._menu_view()
        calls = []
        view._set_appearance = lambda mode: calls.append(('appearance', mode))
        view._toggle_startup = lambda: calls.append(('startup',))
        view.quit = lambda: calls.append(('quit',))
        entries = [entry for entry in view._menu_entries() if entry[0] == 'item']
        self.assertEqual([entry[1] for entry in entries], ['System', 'Light', 'Dark', 'Launch at sign-in', 'Quit companion'])
        self.assertEqual([entry[2] for entry in entries], [True, False, False, False, False])
        for index in range(len(entries)):
            view._menu(view.menu_icon)
            view.root.update()
            rows = [child for child in view.menu.winfo_children()[0].winfo_children() if isinstance(child, tk.Frame) and child.winfo_height() > 3]
            self.assertEqual(len(rows), len(entries))
            rows[index].event_generate('<Button-1>')
            view.root.update()
            self.assertIsNone(view.menu)
        self.assertEqual(calls, [('appearance', 'system'), ('appearance', 'light'), ('appearance', 'dark'), ('startup',), ('quit',)])

    @unittest.skipUnless(sys.platform == 'win32' and not os.environ.get('EUTONOS_COMPANION_NO_GUI_TEST'), 'Windows Tk preview disabled or unavailable')
    def test_hamburger_bars_are_equal_and_evenly_spaced(self):
        for scale in (1.0, 1.25, 1.5, 2.0):
            view = self._menu_view(scale)
            icon = view.menu_icon
            boxes = [icon.coords(item) for item in icon.find_withtag('bar')]
            self.assertEqual(len(boxes), 3)
            heights = {round(bottom - top) for _x0, top, _x1, bottom in boxes}
            gaps = {round(boxes[n + 1][1] - boxes[n][3]) for n in range(2)}
            self.assertEqual(len(heights), 1, boxes)
            self.assertEqual(len(gaps), 1, boxes)
            self.assertEqual({box[0] for box in boxes}, {0.0})
            self.assertEqual(len({box[2] for box in boxes}), 1)
            self.assertEqual(icon.itemcget(icon.find_withtag('bar')[0], 'fill'), view.palette['muted'])
            self.assertEqual(str(icon.cget('cursor')), 'hand2')
            _destroy_quietly(view.root)

    def _menu_view(self, scale=1.0):
        view = ui.DesktopCompanion.__new__(ui.DesktopCompanion)
        view.root = tk.Tk()
        self.addCleanup(_destroy_quietly, view.root)
        view.root.geometry('+-4000+-4000')
        view.expanded = True
        view.theme = 'light'
        view.theme_mode = 'system'
        view.menu = None
        view.details_open = False
        view.dpi_scale = scale
        view.harness = 'claude'
        view.title = 'Fixture'
        view.project_root = self.root
        view.configuration = {}
        view.startup_var = tk.BooleanVar(value=False)
        view.snapshot = {'health': 'observed', 'observed_at': time.time(), 'attention': [], 'records': {}, 'agents': {}}
        view._build()
        view.root.update()
        return view


def _destroy_quietly(root):
    try:
        root.destroy()
    except tk.TclError:
        pass


def _capture_window(root, path):
    """Capture only the fixture HWND; never read another app or the desktop."""
    user = ctypes.windll.user32
    gdi = ctypes.windll.gdi32
    from ctypes import wintypes
    user.GetAncestor.argtypes = (wintypes.HWND, ctypes.c_uint)
    user.GetDC.argtypes = (wintypes.HWND,)
    user.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
    user.PrintWindow.argtypes = (wintypes.HWND, wintypes.HDC, ctypes.c_uint)
    gdi.CreateCompatibleDC.argtypes = (wintypes.HDC,)
    gdi.CreateCompatibleBitmap.argtypes = (wintypes.HDC, ctypes.c_int, ctypes.c_int)
    gdi.SelectObject.argtypes = (wintypes.HDC, wintypes.HANDLE)
    gdi.GetDIBits.argtypes = (wintypes.HDC, wintypes.HBITMAP, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint)
    gdi.DeleteObject.argtypes = (wintypes.HANDLE,)
    gdi.DeleteDC.argtypes = (wintypes.HDC,)
    user.GetAncestor.restype = wintypes.HWND
    user.GetDC.restype = wintypes.HDC
    gdi.CreateCompatibleDC.restype = wintypes.HDC
    gdi.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi.SelectObject.restype = wintypes.HANDLE
    hwnd = user.GetAncestor(root.winfo_id(), 2)
    width, height = root.winfo_width(), root.winfo_height()
    source = user.GetDC(hwnd)
    dc = gdi.CreateCompatibleDC(source)
    bitmap = gdi.CreateCompatibleBitmap(source, width, height)
    previous = gdi.SelectObject(dc, bitmap)
    try:
        user.PrintWindow(hwnd, dc, 1)
        class Header(ctypes.Structure):
            _fields_ = [('size', ctypes.c_uint32), ('width', ctypes.c_int32), ('height', ctypes.c_int32),
                        ('planes', ctypes.c_uint16), ('bits', ctypes.c_uint16), ('compression', ctypes.c_uint32),
                        ('image_size', ctypes.c_uint32), ('x', ctypes.c_int32), ('y', ctypes.c_int32), ('used', ctypes.c_uint32), ('important', ctypes.c_uint32)]
        header = Header(40, width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        pixels = ctypes.create_string_buffer(width * height * 4)
        gdi.GetDIBits(dc, bitmap, 0, height, pixels, ctypes.byref(header), 0)
        raw = pixels.raw
        rgb = b''.join(b'\0' + b''.join(raw[i + 2:i + 3] + raw[i + 1:i + 2] + raw[i:i + 1] for i in range(y * width * 4, (y + 1) * width * 4, 4)) for y in range(height))
        def chunk(name, value):
            return struct.pack('>I', len(value)) + name + value + struct.pack('>I', zlib.crc32(name + value) & 0xffffffff)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(rgb)) + chunk(b'IEND', b''))
    finally:
        gdi.SelectObject(dc, previous)
        gdi.DeleteObject(bitmap)
        gdi.DeleteDC(dc)
        user.ReleaseDC(hwnd, source)


if __name__ == '__main__':
    unittest.main()
