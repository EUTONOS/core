"""No app/process actions: fixture supervision, coordinator and window geometry."""
import io
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import os
import queue
from types import SimpleNamespace
import unittest
from unittest import mock

from eutonos import desktop_companion as ui
from eutonos import desktop_companion_runtime as runtime
from eutonos import desktop_companion_windows as windows


class CompanionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.options = SimpleNamespace(root=self.root, host=None, standalone=False)

    def stream_probe(self, stream):
        probe = windows.Probe.__new__(windows.Probe)
        probe.process = mock.Mock(stdout=stream)
        probe.messages = queue.Queue(maxsize=3)
        probe.reader_error = None
        return probe

    def test_actual_stream_reader_handles_json_noise_and_reaches_coordinator_views(self):
        observation = dict(windows=[dict(harness='codex', pid=1, handle=10, title='Codex'),
                                    dict(harness='claude', pid=2, handle=20, title='Claude')], foreground=0)
        probe = self.stream_probe(io.StringIO('malformed\n[]\n' + json.dumps(observation) + '\n'))
        probe._read(probe.process)
        coordinator = ui.Coordinator.__new__(ui.Coordinator)
        coordinator.closed, coordinator.views, coordinator.selected = False, {}, {}
        coordinator.probe, coordinator.root = probe, mock.Mock()
        coordinator.snapshot_results, coordinator.collector = queue.Queue(), None
        coordinator.snapshot = dict(health='unknown')
        coordinator.last_observation = 0
        view_factory = lambda *args: mock.Mock(handle=args[1]['handle'], last_motion=0)
        with mock.patch.object(probe, 'ensure_running'), mock.patch.object(ui, 'DesktopCompanion', side_effect=view_factory), mock.patch.object(windows.USER32, 'GetForegroundWindow', return_value=0):
            coordinator._tick()
        self.assertEqual(set(coordinator.views), {('codex', 1), ('claude', 2)})
        for view in coordinator.views.values():
            view.follow_window.assert_called_once_with(0, True)
        coordinator.root.after.assert_called_once()
        self.assertEqual(probe.reader_error, 'probe-stream-ended')

    def test_stream_reader_failure_is_qualified_and_live_producer_is_restarted(self):
        class BrokenStream:
            def __iter__(self):
                raise RuntimeError('private subprocess text')
        probe = self.stream_probe(BrokenStream())
        probe.reader = mock.Mock(**{'is_alive.return_value': False})
        probe.last_start = 0
        probe.process.poll.return_value = None
        probe._read(probe.process)
        failure = probe.messages.get_nowait()
        self.assertEqual(failure['error'], 'probe-reader-failed:RuntimeError')
        self.assertNotIn('private', json.dumps(failure))
        self.assertFalse(probe.ensure_running())
        probe.process.terminate.assert_called_once()
        probe.process.poll.return_value = 1
        replacement = mock.Mock(stdout=io.StringIO())
        with mock.patch.object(windows.time, 'monotonic', return_value=6), mock.patch.object(windows.subprocess, 'Popen', return_value=replacement), mock.patch.object(windows.threading, 'Thread') as thread:
            self.assertFalse(probe.ensure_running())
        self.assertIs(probe.process, replacement)
        self.assertIsNone(probe.reader_error)
        thread.return_value.start.assert_called_once()

    def test_eof_with_live_producer_recovers_and_old_reader_cannot_poison_new_queue(self):
        probe = self.stream_probe(io.StringIO())
        old = probe.process
        probe._read(old)
        self.assertEqual(probe.messages.get_nowait()['error'], 'probe-stream-ended')
        probe.process = mock.Mock(stdout=io.StringIO())
        probe.reader_error = None
        probe._read(old)
        self.assertIsNone(probe.reader_error)
        self.assertTrue(probe.messages.empty())

    @unittest.skipUnless(sys.platform == 'win32', 'Windows streaming observer contract')
    def test_actual_hidden_continuous_probe_delivers_multiple_observations_to_queue(self):
        probe = windows.Probe()
        try:
            self.assertEqual(probe.process.args[-2:], ['-ParentProcessId', str(os.getpid())])
            values = [probe.messages.get(timeout=15) for _ in range(2)]
            for value in values:
                self.assertNotIn('error', value)
                self.assertIsInstance(value['windows'], list)
                self.assertIsInstance(value['foreground'], int)
                accepted = ui.app_windows(value['windows'])
                self.assertLessEqual(len(accepted), 16)
                self.assertTrue(all(type(item['handle']) is int for item in accepted.values()))
            self.assertTrue(probe.reader.is_alive())
            self.assertTrue(probe.ensure_running())
        finally:
            probe.close()
            if probe.process is not None:
                probe.process.wait(timeout=5)
            if probe.reader is not None:
                probe.reader.join(timeout=5)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows parent-bound observer contract')
    def test_continuous_probe_exits_when_its_original_parent_exits(self):
        parent = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                  creationflags=runtime.HIDDEN)
        probe = None
        try:
            probe = subprocess.Popen(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                      '-File', str(windows.PROBE), '-ParentProcessId', str(parent.pid)],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding='utf-8',
                                     creationflags=runtime.HIDDEN)
            self.assertTrue(probe.stdout.readline().strip())
            parent.terminate()
            parent.wait(timeout=5)
            self.assertEqual(probe.wait(timeout=5), 0)
        finally:
            if parent.poll() is None:
                parent.terminate()
                parent.wait(timeout=5)
            if probe is not None and probe.poll() is None:
                probe.terminate()
                probe.wait(timeout=5)

    def test_simultaneous_apps_deduplicate_pid_keep_selection_and_reopen(self):
        observed = [dict(harness='codex', pid=1, handle=10, title='Codex'),
                    dict(harness='codex', pid=1, handle=11, title='Second window'),
                    dict(harness='claude', pid=2, handle=20, title='Claude')]
        selected = ui.app_windows(observed)
        self.assertEqual({key: row['handle'] for key, row in selected.items()}, {('codex', 1): 10, ('claude', 2): 20})
        selected = ui.app_windows(observed, selected, foreground=11)
        self.assertEqual(selected[('codex', 1)]['handle'], 11)
        self.assertEqual(ui.app_windows(observed, selected)[('codex', 1)]['handle'], 11)
        coordinator = ui.Coordinator.__new__(ui.Coordinator)
        coordinator.views, coordinator.selected = {}, {}
        with mock.patch.object(ui, 'DesktopCompanion', side_effect=lambda *args: mock.Mock(handle=args[1]['handle'])) as view:
            coordinator._sync_windows(observed, 11)
            self.assertEqual(view.call_count, 2)
            retained = coordinator.views[('codex', 1)]
            removed = coordinator.views[('claude', 2)]
            coordinator._sync_windows(observed[:2], 11)
            removed.root.destroy.assert_called_once()
            coordinator._sync_windows(observed[:2] + [dict(harness='claude', pid=3, handle=30, title='Reopened')], 30)
            self.assertEqual(view.call_count, 3)
            self.assertIs(coordinator.views[('codex', 1)], retained)
            self.assertIn(('claude', 3), coordinator.views)

    def test_bounded_app_selection_rejects_unknown_and_invalid_identity(self):
        selected = ui.app_windows([dict(harness='browser', pid=1, handle=10), dict(harness='claude', pid='1', handle=20)] +
                                  [dict(harness='codex', pid=n, handle=n, title='Known') for n in range(1, 100)])
        self.assertEqual(len(selected), 16)
        self.assertTrue(all(key[0] == 'codex' for key in selected))

    def test_collapsed_badges_are_raised_thirty_pixels_with_monitor_clamp(self):
        self.assertEqual(windows.collapsed_anchor((1610, 624, 3282, 1951), (36, 36)), (3264, 1876))
        self.assertEqual(windows.collapsed_anchor((115, 625, 1604, 1975), (36, 36)), (1586, 1900))
        view = windows.WindowPrimitives.__new__(windows.WindowPrimitives)
        view.root = mock.Mock()
        view.handle, view.dpi_scale, view.expanded = 10, 1, False
        view.window_size = view.geometry_size = (36, 36)
        with mock.patch.object(windows, 'window_rect', return_value=(1610, 624, 3282, 1951)), mock.patch.object(windows, 'monitor_work_rect', return_value=(0, 0, 4000, 2160)), mock.patch.object(windows.USER32, 'GetDpiForWindow', return_value=96):
            view._position()
        view.root.geometry.assert_called_with('36x36+3264+1876')

    def test_background_badge_stacks_on_first_show_and_after_minimize_restore(self):
        view = ui.DesktopCompanion.__new__(ui.DesktopCompanion)
        view.standalone, view.handle, view.window_owner = False, 10, (20, 10)
        view.root, view._position = mock.Mock(), mock.Mock()
        view.root.state.return_value = 'withdrawn'
        rect = (100, 100, 500, 500)
        view.last_rect = rect
        with mock.patch.object(windows, 'window_rect', side_effect=[rect, None, rect]), mock.patch.object(windows.USER32, 'GetAncestor', return_value=20), mock.patch.object(windows, 'stack_above_owner', return_value=True) as stack:
            view.follow_window(99, True)
            self.assertEqual(view.root.deiconify.call_count, 1)
            stack.assert_called_once_with(20, 10)
            view.follow_window(99, True)
            view.root.withdraw.assert_called_once()
            self.assertEqual(stack.call_count, 1)
            view.follow_window(10, True)
            self.assertEqual(stack.call_count, 2)
            self.assertEqual(view.root.deiconify.call_count, 2)

    def test_owner_stack_keeps_unrelated_predecessor_and_does_not_raise_owner(self):
        with mock.patch.object(windows.USER32, 'GetWindow', return_value=99), mock.patch.object(windows.USER32, 'SetWindowPos', return_value=True) as position:
            self.assertTrue(windows.stack_above_owner(20, 10))
        flags = windows.SWP_NOACTIVATE | windows.SWP_NOMOVE | windows.SWP_NOSIZE | windows.SWP_NOOWNERZORDER
        position.assert_called_once_with(20, 99, 0, 0, 0, 0, flags)
        with mock.patch.object(windows.USER32, 'GetWindow', return_value=20), mock.patch.object(windows.USER32, 'SetWindowPos') as position:
            self.assertTrue(windows.stack_above_owner(20, 10))
            position.assert_not_called()

    @unittest.skipUnless(sys.platform == 'win32' and not os.environ.get('EUTONOS_COMPANION_NO_GUI_TEST'), 'Windows offscreen native owned stacking fixture')
    def test_actual_native_owned_badge_is_above_owner_below_unrelated_without_activation(self):
        native = ctypes.WinDLL('user32', use_last_error=True)
        native.CreateWindowExW.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                         ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                         wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p)
        native.CreateWindowExW.restype = wintypes.HWND
        native.DestroyWindow.argtypes = (wintypes.HWND,)
        native.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        handles = []
        foreground = windows.USER32.GetForegroundWindow()
        try:
            for title in ('Fixture owner', 'Fixture badge', 'Fixture unrelated'):
                # Offscreen, tool-window, no-activate surfaces never control an app.
                handle = native.CreateWindowExW(0x08000080, 'STATIC', title, 0x80000000,
                                              -20000, -20000, 40, 40, None, None, None, None)
                self.assertTrue(handle, ctypes.get_last_error())
                handles.append(handle)
                native.ShowWindow(handle, 4)
            owner, own, unrelated = handles
            flags = windows.SWP_NOACTIVATE | windows.SWP_NOMOVE | windows.SWP_NOSIZE | windows.SWP_NOOWNERZORDER
            windows.USER32.SetWindowPos(owner, windows.HWND_TOP, 0, 0, 0, 0, flags)
            windows.USER32.SetWindowPos(unrelated, windows.HWND_TOP, 0, 0, 0, 0, flags)
            windows.USER32.SetWindowLongPtrW(own, -8, owner)
            self.assertTrue(windows.stack_above_owner(own, owner))
            self.assertEqual(windows.USER32.GetWindow(owner, windows.GW_HWNDPREV), own)
            self.assertEqual(windows.USER32.GetWindow(own, windows.GW_HWNDPREV), unrelated)
            self.assertEqual(windows.USER32.GetWindow(own, 4), owner)
            self.assertEqual(windows.USER32.GetForegroundWindow(), foreground)
        finally:
            for handle in reversed(handles):
                native.DestroyWindow(handle)

    def test_hidden_supervision_retries_forever_with_capped_backoff_and_clean_quit(self):
        processes = [mock.Mock(**{'wait.return_value': code, 'poll.return_value': code}) for code in [1] * 8 + [0]]
        with mock.patch.object(runtime, '_claim', return_value=1), mock.patch.object(runtime, '_release') as release, mock.patch.object(runtime, '_mutex_exists', return_value=False), mock.patch.object(runtime, 'paused', side_effect=[False, False] * 8 + [False, True]), mock.patch.object(runtime, '_launch_command', return_value=['fixture']), mock.patch.object(runtime.subprocess, 'Popen', side_effect=processes) as launch, mock.patch.object(runtime.time, 'sleep') as sleep, mock.patch.object(runtime.time, 'monotonic', return_value=0):
            self.assertEqual(runtime._supervise(self.options), 0)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 4, 8, 16, 30, 30, 30])
        self.assertEqual(launch.call_count, 9)
        self.assertTrue(all(call.kwargs['creationflags'] == runtime.HIDDEN for call in launch.call_args_list))
        release.assert_called_once_with(1)
        self.assertEqual(json.loads((self.root / '.eutonos-local/desktop-companion-runtime.json').read_text())['retry_delay'], 30)

    def test_orphan_coordinator_is_observed_before_new_child(self):
        child = mock.Mock(**{'wait.return_value': 0, 'poll.return_value': 0})
        with mock.patch.object(runtime, '_claim', return_value=1), mock.patch.object(runtime, '_release'), mock.patch.object(runtime, '_mutex_exists', side_effect=[True, True, False, False]) as resident, mock.patch.object(runtime, 'paused', side_effect=[False, False, False, True]), mock.patch.object(runtime, '_launch_command', return_value=['fixture']), mock.patch.object(runtime.subprocess, 'Popen', return_value=child) as launch, mock.patch.object(runtime.time, 'sleep') as sleep:
            self.assertEqual(runtime._supervise(self.options), 0)
        self.assertEqual(resident.call_count, 4)
        sleep.assert_called_once_with(1)
        launch.assert_called_once()

    def test_hourly_ensure_honors_pause_and_explicit_start_resumes(self):
        with mock.patch.object(runtime, 'paused', return_value=True), mock.patch.object(runtime.subprocess, 'Popen') as launch:
            self.assertEqual(runtime.ensure_current_session(self.options), 'paused')
            launch.assert_not_called()
        with mock.patch.object(runtime, 'set_paused') as unpause, mock.patch.object(runtime, 'ensure_current_session', return_value='already-supervised'), mock.patch.object(runtime, '_existing_window', return_value=True):
            self.assertEqual(runtime.start_current_session(self.options), 'already-supervised')
            unpause.assert_called_once_with(False)

    def test_pause_marker_is_durable_and_shared_by_path(self):
        marker = self.root / '.eutonos' / 'desktop-companion.paused'
        with mock.patch.object(runtime, '_pause_marker', return_value=marker):
            self.assertFalse(runtime.paused())
            runtime.set_paused(True)
            self.assertTrue(marker.is_file())
            self.assertTrue(runtime.paused())
            runtime.set_paused(False)
            self.assertFalse(runtime.paused())

    def test_task_is_interactive_hourly_and_action_is_finite(self):
        command = ['C:\\runtime\\Scripts\\pythonw.exe', '-I', '-m', 'eutonos.desktop_companion_runtime', '--ensure-current']
        task = runtime._task_xml(command, 'S-1-5-21-42')
        self.assertTrue(task.startswith(b'\xff\xfe'))
        self.assertIn('encoding=\'utf-16\'', task.decode('utf-16').splitlines()[0])
        root = runtime.ET.fromstring(task)
        ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        self.assertEqual(root.findtext('t:Principals/t:Principal/t:LogonType', namespaces=ns), 'InteractiveToken')
        self.assertEqual(root.findtext('t:Principals/t:Principal/t:RunLevel', namespaces=ns), 'LeastPrivilege')
        self.assertIsNotNone(root.find('t:Triggers/t:LogonTrigger', ns))
        self.assertEqual(root.findtext('t:Triggers/t:TimeTrigger/t:Repetition/t:Interval', namespaces=ns), 'PT1H')
        self.assertEqual(root.findtext('t:Settings/t:ExecutionTimeLimit', namespaces=ns), 'PT1M')
        self.assertEqual(root.findtext('t:Settings/t:MultipleInstancesPolicy', namespaces=ns), 'IgnoreNew')
        self.assertTrue(runtime._task_owned(root))

    def test_task_query_decodes_misdeclared_console_xml_and_real_utf16(self):
        command = [r'C:\Users\Renée\pythonw.exe', '-I', '-m', 'eutonos.desktop_companion_runtime', '--ensure-current']
        xml = runtime._task_xml(command, 'S-1-5-21-42')
        text = xml.decode('utf-16')
        self.assertIn('encoding=\'utf-16\'', text.splitlines()[0])
        for output in (text.encode('utf-8'), text.encode('cp437'), xml):
            with self.subTest(output=output[:50]), mock.patch.object(runtime.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout=output, stderr=b'')), mock.patch.object(runtime, '_task_query_code_page', return_value=437):
                task = runtime._task_query()
                self.assertTrue(runtime._task_owned(task))
                self.assertEqual(task.findtext('t:Actions/t:Exec/t:Command', namespaces={'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}), command[0])

    def test_task_query_rejects_malformed_or_foreign_xml(self):
        command = ['pythonw.exe', '-I', '-m', 'eutonos.desktop_companion_runtime', '--ensure-current']
        text = runtime._task_xml(command, 'S-1-5-21-42').decode('utf-16')
        foreign = text.replace(runtime.TASK_DESCRIPTION, 'Another task')
        with mock.patch.object(runtime.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout=foreign.encode('utf-8'), stderr=b'')):
            self.assertFalse(runtime._task_owned(runtime._task_query()))
        with mock.patch.object(runtime.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout=b'<?xml version="1.0" encoding="UTF-16"?><Task>', stderr=b'')):
            with self.assertRaisesRegex(OSError, 'invalid XML'):
                runtime._task_query()

    def test_tk_callback_failure_exits_nonzero_with_bounded_diagnostic(self):
        coordinator = ui.Coordinator.__new__(ui.Coordinator)
        coordinator.project_root, coordinator.closed, coordinator.exit_code = self.root, False, 0
        coordinator.probe, coordinator.root = mock.Mock(), mock.Mock()
        coordinator._callback_failure(RuntimeError, RuntimeError('private exception text'), None)
        self.assertEqual(coordinator.exit_code, 1)
        coordinator.probe.close.assert_called_once()
        coordinator.root.quit.assert_called_once()
        coordinator.root.destroy.assert_called_once()
        content = (self.root / '.eutonos-local/desktop-companion-runtime.json').read_text()
        self.assertLess(len(content), 4096)
        self.assertIn('RuntimeError', content)
        self.assertNotIn('private exception', content)

    def test_context_and_sampling_cli_are_retired(self):
        for option in ('--context', '--sample'):
            with mock.patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
                runtime.build_parser().parse_args([option])
        self.assertNotEqual(runtime.COORDINATOR_MUTEX, runtime.SUPERVISOR_MUTEX)

    def test_per_monitor_dpi_is_set_before_coordinator_constructs_tk(self):
        order = []
        with mock.patch.object(ui, 'selected_root', return_value=self.root), mock.patch.object(runtime, 'paused', return_value=False), mock.patch.object(runtime, '_claim', return_value=1), mock.patch.object(runtime, '_release'), mock.patch.object(runtime, '_set_dpi_awareness', side_effect=lambda: order.append('dpi')), mock.patch.object(ui, 'Coordinator', side_effect=lambda _options: (order.append('tk') or mock.Mock(**{'run.return_value': 0}))):
            self.assertEqual(runtime.main(['--coordinator', '--root', str(self.root)]), 0)
        self.assertEqual(order, ['dpi', 'tk'])

    @unittest.skipUnless(sys.platform == 'win32' and not os.environ.get('EUTONOS_COMPANION_NO_GUI_TEST'), 'Windows Tk readiness fixture')
    def test_actual_withdrawn_coordinator_has_discoverable_native_window(self):
        probe = mock.Mock(messages=queue.Queue())
        with mock.patch.object(windows, 'Probe', return_value=probe):
            coordinator = ui.Coordinator(self.options)
        try:
            self.assertEqual(coordinator.root.state(), 'withdrawn')
            self.assertTrue(coordinator.native_root)
            self.assertTrue(runtime._existing_window())
        finally:
            coordinator.quit()

    def test_startup_migration_preserves_unrelated_run_entries(self):
        import winreg
        entries = {runtime.LEGACY_STARTUP_NAMES[0]: 'old', 'Other application': 'keep'}
        key = mock.MagicMock()
        key.__enter__.return_value = key
        def remove(_key, name):
            if name not in entries:
                raise FileNotFoundError(name)
            del entries[name]
        with mock.patch.object(winreg, 'CreateKey', return_value=key), mock.patch.object(winreg, 'DeleteValue', side_effect=remove), mock.patch.object(runtime, '_task_query', return_value=None), mock.patch.object(runtime, '_current_user_sid', return_value='S-1-5-21-42'), mock.patch.object(runtime, '_launch_command', return_value=['pythonw.exe', '-m', 'eutonos.desktop_companion_runtime', '--ensure-current']), mock.patch.object(runtime.subprocess, 'run', return_value=mock.Mock(returncode=0)) as task:
            runtime.set_startup(True, self.options)
            self.assertNotIn(runtime.LEGACY_STARTUP_NAMES[0], entries)
            self.assertEqual(entries['Other application'], 'keep')
            create = task.call_args
            self.assertEqual(create.args[0][:4], ['schtasks.exe', '/Create', '/TN', runtime.TASK_NAME])
            runtime.set_startup(False, self.options)
        self.assertEqual(entries, {'Other application': 'keep'})

    def test_task_registration_failure_reports_bounded_scheduler_error(self):
        failure = mock.Mock(returncode=1, stderr=b'ERROR: unable to switch the encoding\r\n' + b'x' * 500, stdout=b'')
        with mock.patch.object(runtime, '_task_query', return_value=None), mock.patch.object(runtime, '_current_user_sid', return_value='S-1-5-21-42'), mock.patch.object(runtime, '_launch_command', return_value=['pythonw.exe', '--ensure-current']), mock.patch.object(runtime.subprocess, 'run', return_value=failure):
            with self.assertRaisesRegex(OSError, 'unable to switch the encoding') as caught:
                runtime.set_startup(True, self.options)
        self.assertLess(len(str(caught.exception)), 320)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows observer contract')
    def test_app_windows_keeps_hermes(self):
        observed = [dict(harness='hermes', pid=7, handle=70, title='Hermes'),
                    dict(harness='other', pid=8, handle=80, title='x')]
        self.assertEqual(list(ui.app_windows(observed)), [('hermes', 7)])

    def test_actual_read_only_probe_compiles_and_returns_typed_native_windows(self):
        observed = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                   '-File', str(windows.PROBE), '-Once'], capture_output=True,
                                  text=True, encoding='utf-8', timeout=30, creationflags=runtime.HIDDEN)
        self.assertEqual(observed.returncode, 0, observed.stderr)
        result = json.loads(observed.stdout)
        self.assertNotIn('error', result)
        self.assertIsInstance(result['foreground'], int)
        self.assertIsInstance(result['windows'], list)
        for item in result['windows']:
            self.assertIn(item['harness'], ('codex', 'claude', 'hermes'))
            self.assertIsInstance(item['pid'], int)
            self.assertGreater(item['handle'], 0)
        accepted = ui.app_windows(result['windows'], foreground=result['foreground'])
        self.assertLessEqual(len(accepted), 16)

    def test_restart_helper_command_runs_the_new_runtime_with_the_same_selection(self):
        options = SimpleNamespace(root=self.root, host='h1', standalone=True)
        command = runtime.restart_command(self.root / 'new' / 'python.exe', options)
        self.assertEqual(command[1:3], ['-I', '-m'])
        self.assertEqual(command[3:5], ['eutonos.desktop_companion_runtime', '--restart-after-update'])
        self.assertEqual(command[5:], ['--root', str(self.root), '--host', 'h1', '--standalone'])
        bare = runtime.restart_command(self.root / 'new' / 'python.exe', SimpleNamespace(root=None, host=None, standalone=False))
        self.assertEqual(bare[3:], ['eutonos.desktop_companion_runtime', '--restart-after-update'])

    def test_spawning_the_helper_is_detached_and_reports_a_failure_to_start(self):
        options = SimpleNamespace(root=self.root, host=None, standalone=False)
        with mock.patch.object(runtime.subprocess, 'Popen') as popen:
            self.assertTrue(runtime.spawn_restart(self.root / 'python.exe', options))
        self.assertTrue(popen.call_args.kwargs['creationflags'] & runtime.DETACHED)
        with mock.patch.object(runtime.subprocess, 'Popen', side_effect=OSError('no')):
            self.assertFalse(runtime.spawn_restart(self.root / 'python.exe', options))

    def test_the_helper_waits_for_the_old_companion_clears_the_pause_and_starts_the_new_one(self):
        steps = []
        resident = iter([True, True, False, False])  # supervisor then coordinator, twice
        with mock.patch.object(runtime, '_mutex_exists', side_effect=lambda name: next(resident, False)), \
                mock.patch.object(runtime, 'set_paused', side_effect=lambda value: steps.append(('paused', value))), \
                mock.patch.object(runtime, 'ensure_current_session', side_effect=lambda options: steps.append('ensure') or 'started'):
            result = runtime.restart_after_update(self.options, wait=60, sleep=lambda seconds: steps.append('wait'), clock=lambda: 0)
        self.assertEqual(result, 'started')
        self.assertEqual(steps, ['wait', 'wait', ('paused', False), 'ensure'])

    def test_the_helper_clears_the_pause_even_when_the_old_companion_never_leaves(self):
        ticks = iter(range(0, 1000, 50))
        with mock.patch.object(runtime, '_mutex_exists', return_value=True), \
                mock.patch.object(runtime, 'set_paused') as paused, \
                mock.patch.object(runtime, 'ensure_current_session', return_value='x'), \
                mock.patch.object(runtime, 'runtime_diagnostic') as diagnostic:
            runtime.restart_after_update(self.options, wait=120, sleep=lambda seconds: None, clock=lambda: next(ticks))
        paused.assert_called_once_with(False)
        self.assertEqual(diagnostic.call_args.args[1], 'restart-wait-timeout')

    def test_the_restart_flag_is_hidden_and_cannot_be_combined_with_other_actions(self):
        with mock.patch.object(runtime, 'restart_after_update', return_value='ok') as helper, \
                mock.patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(runtime.main(['--restart-after-update']), 0)
        helper.assert_called_once()
        with mock.patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit):
            runtime.main(['--restart-after-update', '--ensure-current'])
        self.assertNotIn('restart-after-update', runtime.build_parser().format_help())

    def test_the_coordinator_restarts_onto_the_new_generation_after_a_good_update(self):
        from eutonos import update_background
        coordinator = ui.Coordinator.__new__(ui.Coordinator)
        coordinator.options = self.options
        coordinator.closed = False
        coordinator.views = {}
        coordinator.update_revision = 0
        coordinator.updates = SimpleNamespace(restart_requested=True, revision=1, home=self.root, install={'phase': 'done'},
                                              tick=lambda: None)
        coordinator.quit = mock.Mock(side_effect=lambda: setattr(coordinator, 'closed', True))
        with mock.patch.object(update_background, 'active_runtime_python', return_value=self.root / 'new' / 'python.exe'), \
                mock.patch.object(runtime, 'spawn_restart', return_value=True) as spawn, \
                mock.patch.object(runtime, 'set_paused') as paused:
            coordinator._update_tick()
        spawn.assert_called_once_with(self.root / 'new' / 'python.exe', self.options)
        paused.assert_called_once_with(True)  # the helper clears it once the old companion has left
        coordinator.quit.assert_called_once()
        self.assertFalse(coordinator.updates.restart_requested)

    def test_the_coordinator_stays_up_and_says_so_when_the_helper_cannot_start(self):
        from eutonos import update_background
        coordinator = ui.Coordinator.__new__(ui.Coordinator)
        coordinator.options = self.options
        coordinator.closed = False
        coordinator.views = {}
        coordinator.update_revision = 0
        coordinator.updates = SimpleNamespace(restart_requested=True, revision=1, home=self.root, install={'phase': 'done'},
                                              tick=lambda: None)
        coordinator.quit = mock.Mock()
        with mock.patch.object(update_background, 'active_runtime_python', return_value=None), \
                mock.patch.object(runtime, 'set_paused') as paused:
            coordinator._update_tick()
        coordinator.quit.assert_not_called()
        paused.assert_not_called()
        self.assertIn('Sign out and back in', coordinator.updates.install['message'])
