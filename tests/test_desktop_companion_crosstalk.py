"""The Companion's Crosstalk section: snapshot block, view model and the toggle/Stop wiring.

Offline: a temporary Crosstalk device folder, fake control actions, no helper, no real
windows except one optional offscreen Tk build. Nothing here calls a real halt or resume.
"""
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import time
import tkinter as tk
import unittest
from unittest import mock

from eutonos import crosstalk_status
from eutonos import desktop_companion as ui
from eutonos import desktop_companion_data as data


class SyncThread:
    """Runs the worker at start() so a test does not wait on a real thread."""
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


class CrosstalkFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.device = Path(self.temp.name) / 'device'
        env = mock.patch.dict(os.environ, {'CROSSTALK_P0_DEVICE_DIR': str(self.device)})
        env.start()
        self.addCleanup(env.stop)
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        self.now = time.time()
        self.collector = data.Collector(self.root, Path(self.temp.name) / 'host')

    def install(self, **controls):
        self.device.mkdir(parents=True, exist_ok=True)
        if controls:
            (self.device / 'controls.json').write_text(json.dumps(controls), encoding='utf-8')

    def journal(self, *rows):
        lines = [json.dumps({'event': 'letter', 'letter_id': letter_id, 'state': state,
                             'ts_ns': int((self.now - age) * 1e9),
                             'from': {'app': 'claude', 'id': 'a'}, 'to': {'app': 'codex', 'id': 'b'},
                             'message': 'SECRET BODY'})
                 for letter_id, state, age in rows]
        (self.device / 'journal.jsonl').write_text('\n'.join(lines) + '\n', encoding='utf-8')

    def heartbeat(self, age_seconds=1, pid=None):
        folder = self.device / 'helper'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'state.json').write_text(json.dumps(
            {'pid': os.getpid() if pid is None else pid, 'ts_ms': int((self.now - age_seconds) * 1000)}), encoding='utf-8')

    def block(self):
        return self.collector.collect(self.now)['crosstalk']


class SnapshotBlockTests(CrosstalkFixture):
    def test_not_installed_is_missing_not_off(self):
        block = self.block()
        self.assertEqual(block['status'], 'missing')
        self.assertFalse(block['installed'])
        self.assertEqual(block['count_basis'], 'unknown')

    def test_installed_without_controls_file_is_off_and_helper_never_ran(self):
        self.install()
        block = self.block()
        self.assertEqual((block['status'], block['installed'], block['messaging'], block['halted']),
                         ('observed', True, 'off', False))
        self.assertEqual(block['helper'], {'alive': False, 'ever_ran': False, 'pid': None, 'ts_ms': None})
        self.assertEqual(block['in_transit'], 0)

    def test_on_with_live_helper_and_letters_in_transit(self):
        self.install(messaging='on', deny=['codex:x'])
        self.heartbeat()
        self.journal(('l1', 'journaled', 5), ('l2', 'sending', 5), ('l3', 'blocked:not-running', 600),
                     ('l4', 'replied', 5), ('l5', 'held', 5), ('l6', 'blocked:unknown-identity', 5),
                     ('l7', 'journaled', 2 * 3600))
        block = self.block()
        self.assertEqual((block['messaging'], block['halted'], block['denied']), ('on', False, 1))
        self.assertTrue(block['helper']['alive'])
        self.assertEqual(block['in_transit'], 3)
        self.assertNotIn('SECRET BODY', json.dumps(block))

    def test_stale_heartbeat_or_dead_pid_is_not_running(self):
        self.install(messaging='on')
        self.heartbeat(age_seconds=600)
        helper = self.block()['helper']
        self.assertEqual((helper['alive'], helper['ever_ran']), (False, True))
        self.heartbeat(pid=2 ** 22 + 12345)
        with mock.patch.object(crosstalk_status, '_pid_alive', return_value=False):
            self.assertFalse(self.block()['helper']['alive'])

    def test_halted_and_wake_budget_notice(self):
        self.install(messaging='on', halt=True)
        notices = self.device / 'helper'
        notices.mkdir()
        (notices / 'notices.json').write_text(json.dumps({'version': 1, 'notices': {
            'wake-budget': {'kind': 'wake-budget', 'message': 'Wake budget reached.', 'since_ns': 1}}}), encoding='utf-8')
        block = self.block()
        self.assertTrue(block['halted'])
        self.assertEqual(block['wake_budget_notice'], 'Wake budget reached.')

    def test_failed_source_is_unknown_and_other_sections_survive(self):
        self.install(messaging='on')
        with mock.patch.object(crosstalk_status, 'recent_letters', side_effect=RuntimeError('boom')):
            snap = self.collector.collect(self.now)
        self.assertEqual(snap['crosstalk']['status'], 'unknown')
        self.assertEqual(snap['crosstalk']['count_basis'], 'unknown')
        self.assertEqual(snap['records']['ram']['state'], 'missing')
        self.assertIn('counts', snap['agents'])
        self.assertEqual(snap['profile'], 'eutonos.desktop-companion.v1')


class ViewModelTests(unittest.TestCase):
    OBSERVED = {'status': 'observed', 'messaging': 'on', 'halted': False, 'denied': 0, 'in_transit': 2,
                'count_basis': 'observed', 'helper': {'alive': True, 'ever_ran': True}}

    def test_on_running_state(self):
        view = ui.crosstalk_view(self.OBSERVED)
        self.assertEqual((view['state_text'], view['toggle_verb'], view['toggle_enabled'], view['stop_enabled']),
                         ('On', 'off', True, True))
        self.assertEqual((view['helper_text'], view['transit_text']), ('Helper running', '2 letters in transit'))

    def test_off_toggle_enables_and_single_letter_wording(self):
        view = ui.crosstalk_view({**self.OBSERVED, 'messaging': 'off', 'in_transit': 1, 'helper': {'alive': False, 'ever_ran': True}})
        self.assertEqual((view['state_text'], view['toggle_verb']), ('Off', 'enable'))
        self.assertEqual((view['helper_text'], view['transit_text']), ('Helper not running', '1 letter in transit'))

    def test_halted_disables_toggle_and_stop_and_offers_resume(self):
        view = ui.crosstalk_view({**self.OBSERVED, 'halted': True})
        self.assertEqual(view['state_text'], 'HALTED')
        self.assertFalse(view['on'])
        self.assertFalse(view['toggle_enabled'])
        self.assertFalse(view['stop_enabled'])
        self.assertTrue(view['resume_visible'] and view['resume_enabled'])

    def test_not_installed_and_unknown_disable_stop(self):
        for block, text in (({'status': 'missing'}, 'Not installed'), ({'status': 'unknown'}, 'unknown'), (None, 'unknown')):
            view = ui.crosstalk_view(block)
            self.assertEqual(view['state_text'], text)
            self.assertFalse(view['stop_enabled'])
            self.assertFalse(view['toggle_enabled'])
            self.assertFalse(view['resume_visible'])

    def test_busy_disables_everything(self):
        view = ui.crosstalk_view(self.OBSERVED, busy=True)
        self.assertFalse(view['toggle_enabled'] or view['stop_enabled'])


class ActionWiringTests(CrosstalkFixture):
    def view(self, controls):
        self.install(**controls)
        view = ui.DesktopCompanion.__new__(ui.DesktopCompanion)
        view.project_root = self.root
        view.expanded = False
        view.snapshot = {'crosstalk': {'status': 'unknown'}, 'health': 'observed'}
        return view

    def run_action(self, view, verb, fake):
        with mock.patch.object(crosstalk_status, 'owner_control', side_effect=fake) as control, \
                mock.patch.object(ui.threading, 'Thread', SyncThread):
            view._crosstalk_act(verb)
            self.assertTrue(view.crosstalk_busy)
            view._drain_crosstalk_results()
        return control

    def writer(self, **changes):
        """A fake owner_control that applies ``changes`` to controls.json like the CLI would."""
        def fake(command, *roots):
            current = crosstalk_status.controls()
            current.update(changes)
            (self.device / 'controls.json').write_text(json.dumps(current), encoding='utf-8')
            ctl = crosstalk_status.controls()
            ok = {'halt': ctl['halt'], 'resume': not ctl['halt'], 'enable': ctl['messaging'] == 'on',
                  'off': ctl['messaging'] == 'off'}[command]
            return {'ok': ok, 'command': command, 'controls': ctl}
        return fake

    def test_each_verb_reaches_owner_control_and_display_comes_from_readback(self):
        cases = (('halt', {'messaging': 'on'}, {'halt': True}, 'HALTED'),
                 ('resume', {'messaging': 'on', 'halt': True}, {'halt': False}, 'On'),
                 ('enable', {'messaging': 'off'}, {'messaging': 'on'}, 'On'),
                 ('off', {'messaging': 'on'}, {'messaging': 'off'}, 'Off'))
        for verb, start, change, shown in cases:
            with self.subTest(verb=verb):
                view = self.view(start)
                control = self.run_action(view, verb, self.writer(**change))
                control.assert_called_once_with(verb, self.root)
                self.assertFalse(view.crosstalk_busy)
                self.assertIsNone(view.crosstalk_note)
                self.assertEqual(ui.crosstalk_view(view.snapshot['crosstalk'])['state_text'], shown)

    def test_failed_action_is_shown_plainly_with_the_readback(self):
        view = self.view({'messaging': 'on'})
        fake = lambda command, *roots: {'ok': False, 'error': 'crosstalk-control-not-applied',
                                        'controls': crosstalk_status.controls()}
        self.run_action(view, 'halt', fake)
        self.assertIn('Stop did not take effect', view.crosstalk_note)
        self.assertIn('crosstalk-control-not-applied', view.crosstalk_note)
        self.assertIn('messaging on, not halted', view.crosstalk_note)
        # The display still reflects what controls.json says, not the click.
        self.assertEqual(ui.crosstalk_view(view.snapshot['crosstalk'])['state_text'], 'On')

    def test_action_that_raises_is_a_failure_not_a_crash(self):
        view = self.view({'messaging': 'on'})
        self.run_action(view, 'halt', mock.Mock(side_effect=OSError('x')))
        self.assertIn('did not take effect', view.crosstalk_note)

    def test_no_double_submit_while_an_action_is_running(self):
        view = self.view({'messaging': 'on'})
        with mock.patch.object(ui.threading, 'Thread') as thread:
            view._crosstalk_act('halt')
            view._crosstalk_act('off')
        thread.assert_called_once()
        thread.return_value.start.assert_called_once()

    def test_only_the_four_core_verbs_are_accepted(self):
        view = self.view({'messaging': 'on'})
        with mock.patch.object(ui.threading, 'Thread') as thread:
            view._crosstalk_act('replay')
            view._crosstalk_act('send')
        thread.assert_not_called()
        self.assertFalse(getattr(view, 'crosstalk_busy', False))

    def test_feeds_action_is_the_path_used(self):
        view = self.view({'messaging': 'on'})
        with mock.patch('eutonos.crosstalk_dashboard_feeds.action', return_value={'ok': True}) as action, \
                mock.patch.object(ui.threading, 'Thread', SyncThread):
            view._crosstalk_act('halt')
        action.assert_called_once_with(self.root, {'action': 'halt'})


@unittest.skipUnless(sys.platform == 'win32' and not os.environ.get('EUTONOS_COMPANION_NO_GUI_TEST'), 'Windows Tk build disabled or unavailable')
class WidgetTests(CrosstalkFixture):
    def build(self, block):
        view = ui.DesktopCompanion.__new__(ui.DesktopCompanion)
        view.root = tk.Tk()
        self.addCleanup(view.root.destroy)
        view.root.geometry('+-4000+-4000')
        view.expanded = True
        view.theme = 'light'
        view.menu = None
        view.details_open = False
        view.harness = 'claude'
        view.project_root = self.root
        view.snapshot = {'health': 'observed', 'observed_at': time.time(), 'attention': [], 'records': {}, 'agents': {}, 'crosstalk': block}
        view.body = tk.Frame(view.root)
        view.body.pack()
        view._render_crosstalk(block)
        return view

    @staticmethod
    def texts(widget):
        found = [widget.cget('text')] if isinstance(widget, tk.Label) else []
        for child in widget.winfo_children():
            found += WidgetTests.texts(child)
        return found

    def test_halted_section_says_halted_and_offers_resume(self):
        view = self.build({'status': 'observed', 'messaging': 'on', 'halted': True, 'denied': 0, 'in_transit': 0,
                           'count_basis': 'observed', 'helper': {'alive': True, 'ever_ran': True}, 'recent_letters': []})
        texts = self.texts(view.body)
        self.assertIn('HALTED', texts)
        self.assertIn('Resume', texts)
        self.assertNotIn('Stop', texts)

    def test_not_installed_shows_a_disabled_stop(self):
        view = self.build({'status': 'missing'})
        texts = self.texts(view.body)
        self.assertIn('Crosstalk', texts)
        self.assertIn('Crosstalk not installed on this device', texts)
        self.assertIn('Stop', texts)
        self.assertFalse(ui.crosstalk_view({'status': 'missing'})['stop_enabled'])

    def test_running_section_layout(self):
        view = self.build({'status': 'observed', 'messaging': 'on', 'halted': False, 'denied': 0, 'in_transit': 2,
                           'count_basis': 'observed', 'helper': {'alive': True, 'ever_ran': True}, 'recent_letters': []})
        texts = self.texts(view.body)
        self.assertEqual(texts[0], 'Crosstalk')
        self.assertIn('Helper running · 2 letters in transit', texts)
        self.assertIn('Stop', texts)


if __name__ == '__main__':
    unittest.main()
