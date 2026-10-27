import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from eutonos import personal, tokenslang as ts, managed_write
from eutonos.effects import LocalRoot, digest
from eutonos.host import enroll
from eutonos.private_scope import private_host


class PersonalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='personal-domain-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.root = self.home / 'personal'; self.root.mkdir()
        self.env = patch.dict(os.environ, {'EUTONOS_HOME': str(self.home / 'home')})
        self.env.start(); self.addCleanup(self.env.stop)
        for path, raw in personal.initial_files('owner').items():
            (self.root / path).write_bytes(raw)
        self.git('init', '-q', '-b', 'personal')
        self.git('add', '--all')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'baseline')
        self.host = private_host(self.root)
        self.host.parent.mkdir(parents=True)
        enroll(self.root, self.host, 'actor', ['goals.md', 'projects.md', 'priorities.json'])

    def git(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
        return subprocess.check_output(['git', '-c', 'safe.directory=' + str(self.root), '-C', str(self.root), *args], env=env, stderr=subprocess.PIPE)

    def request(self, kind='goal'):
        guided = personal.form(self.root, kind, {'title': 'Private title', 'summary': 'Bounded outcome'}, owner_id='owner')
        item = guided['request']['item']
        if kind == 'goal':
            item['extensions']['goal']['outcome'] = 'Result'
            item['extensions']['goal']['criteria'][0]['statement'] = 'A result exists'
        else:
            item['extensions']['personal_project']['purpose'] = 'Personal project outcome'
        return guided['request']

    def apply(self, prepared, verify=None):
        return managed_write.apply(self.root, self.host, 'actor', 'personal-' + prepared['operation_id'],
            'effect-' + prepared['operation_id'], prepared['effect'], prepared['fingerprint'], prepared['paths'],
            {'completed': 'Private typed readback'}, verify=verify or (lambda: personal.verify(self.root, prepared, owner_id='owner')))

    def test_owner_before_content_and_native_project_without_work_order(self):
        with self.assertRaises(ts.ReadError):
            personal.read(self.root, owner_id='different')
        for kind in ('goal', 'project'):
            prepared = personal.prepare(self.root, self.request(kind), owner_id='owner')
            self.assertEqual(prepared['paths'], [kind + 's.md'])
            result = self.apply(prepared)
            self.assertTrue(result['complete'])
        self.assertFalse((self.root / 'eutonos.work.json').exists())
        current = personal.read(self.root, owner_id='owner')
        self.assertEqual({i['type'] for i in current['items']}, {'goal', 'project'})
        item = current['items'][0]
        selected = personal.read(self.root, owner_id='owner', item_id=item['id'])
        self.assertEqual(selected['item_revision'], item['revision'])

    def test_stale_owner_and_item_revisions_are_rejected(self):
        request = self.request()
        (self.root / 'goals.md').write_bytes((self.root / 'goals.md').read_bytes() + b'\nConcurrent prose\n')
        with self.assertRaises(ts.ReadError) as caught:
            personal.prepare(self.root, request, owner_id='owner')
        self.assertEqual(caught.exception.code, 'stale')
        prepared = personal.prepare(self.root, self.request(), owner_id='owner')
        self.apply(prepared)
        item_id = prepared['request']['item']['id']
        changed = personal.form(self.root, 'goal', {'summary': 'Changed'}, owner_id='owner', item_id=item_id)['request']
        changed['expected_revision'] = '0' * 64
        with self.assertRaises(ts.ReadError) as caught:
            personal.prepare(self.root, changed, owner_id='owner')
        self.assertEqual(caught.exception.code, 'stale')

    def test_interrupted_typed_verification_recovers_original_effect(self):
        prepared = personal.prepare(self.root, self.request(), owner_id='owner')
        with self.assertRaises(RuntimeError):
            self.apply(prepared, verify=lambda: (_ for _ in ()).throw(RuntimeError('interrupted')))
        result = managed_write.recover(self.root, self.host, 'actor', 'personal-' + prepared['operation_id'],
            {'completed': 'Recovered'}, verify=lambda: personal.verify(self.root, prepared, owner_id='owner'),
            effect_id='effect-' + prepared['operation_id'], fingerprint=prepared['fingerprint'])
        self.assertTrue(result['complete'])
        self.assertEqual(len(personal.read(self.root, owner_id='owner')['items']), 1)

    def test_exact_readback_rejects_divergent_bytes_and_update_keeps_id(self):
        prepared = personal.prepare(self.root, self.request(), owner_id='owner')
        self.apply(prepared)
        item_id = prepared['request']['item']['id']
        update = personal.form(self.root, 'goal', {'summary': 'Explicit revision'}, owner_id='owner', item_id=item_id)['request']
        next_plan = personal.prepare(self.root, update, owner_id='owner')
        self.apply(next_plan)
        self.assertEqual(personal.read(self.root, owner_id='owner')['items'][0]['id'], item_id)
        with self.assertRaises(ts.ReadError):
            personal.verify(self.root, prepared, owner_id='owner')

    def history(self):
        from eutonos import personal_history
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        personal_history.configure_local(self.root, 'owner', branch='refs/heads/personal')
        return personal_history

    def test_submit_freezes_intent_and_recovers_artifact_before_native_start(self):
        history = self.history()
        request = self.request()
        with patch.object(history, 'apply', side_effect=RuntimeError('interrupted before intent')):
            with self.assertRaises(RuntimeError):
                personal.submit(self.root, request, owner_id='owner', host=self.host, actor='actor', session='session')
        self.assertEqual(personal.read(self.root, owner_id='owner')['items'], [])
        result = personal.submit(self.root, request, owner_id='owner', host=self.host, actor='actor', session='session')
        self.assertTrue(result['native']['complete'])
        self.assertEqual(result['history']['state'], 'committed')
        with self.assertRaises(ts.ReadError):
            personal.submit(self.root, {**request, 'expected_revision': 'changed'}, owner_id='owner', host=self.host, actor='actor', session='session')
        self.assertEqual(len(personal.read(self.root, owner_id='owner')['items']), 1)

    def test_submit_history_failure_retains_native_and_blocks_new_write(self):
        history = self.history()
        request = self.request()
        with patch('eutonos.git_delivery.Engine.run', side_effect=lambda engine_id: (_ for _ in ()).throw(RuntimeError('history interrupt'))):
            with self.assertRaises(RuntimeError):
                personal.submit(self.root, request, owner_id='owner', host=self.host, actor='actor', session='session')
        self.assertEqual(len(personal.read(self.root, owner_id='owner')['items']), 1)
        with self.assertRaises(ts.ReadError):
            personal.submit(self.root, self.request('project'), owner_id='owner', host=self.host, actor='actor', session='session')
        recovered = personal.recover(self.root, request['operation_id'], owner_id='owner', host=self.host, actor='actor')
        self.assertTrue(recovered['native']['complete'])
        self.assertEqual(recovered['history']['state'], 'committed')


if __name__ == '__main__':
    unittest.main()
