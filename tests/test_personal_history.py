import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eutonos import git_delivery as delivery, git_delivery_native as native, personal, personal_history as history, tokenslang as ts
from eutonos.effects import LocalRoot
from eutonos.host import enroll
from eutonos.private_scope import private_host


class Crash(BaseException):
    pass


class HistoryFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='personal-history-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.root = self.home / 'personal'; self.root.mkdir()
        self.env = patch.dict(os.environ, {'EUTONOS_HOME':str(self.home / 'home')})
        self.env.start(); self.addCleanup(self.env.stop)
        self.initial = personal.initial_files('owner')
        for path, body in self.initial.items():
            (self.root / path).write_bytes(body)
        delivery.git(self.root, 'init', '-b', 'personal')
        delivery.git(self.root, 'config', 'user.name', 'Personal Fixture')
        delivery.git(self.root, 'config', 'user.email', 'fixture@example.invalid')
        delivery.git(self.root, 'add', '--', *history.PATHS)
        delivery.git(self.root, 'commit', '-m', 'Private baseline')
        self.base = delivery.text(self.root, 'rev-parse', 'HEAD')
        history.configure_local(self.root, 'owner')
        self.host = private_host(self.root); self.host.parent.mkdir(parents=True)
        enroll(self.root, self.host, 'actor', list(history.PATHS))

    def request(self):
        request = personal.form(self.root, 'goal', {'title':'Private sentinel','summary':'Owner outcome'}, owner_id='owner')['request']
        semantic = request['item']['extensions']['goal']
        semantic['outcome'] = 'Useful outcome'
        semantic['criteria'][0]['statement'] = 'Verified result exists'
        return request

    def apply(self, request=None):
        request = request or self.request()
        prepared = personal.prepare(self.root, request, owner_id='owner')
        result = history.apply(self.root, prepared, 'owner', self.host, 'actor', 'session',
                               lambda:personal.verify(self.root, prepared, owner_id='owner'))
        return prepared, result

    def recover(self, prepared):
        return history.recover(self.root, prepared['operation_id'], 'owner', self.host, 'actor',
                               lambda:personal.verify(self.root, prepared, owner_id='owner'))


class HistoryTests(HistoryFixture):
    def test_private_profile_has_null_transport_and_no_records_worktree(self):
        config = native.configuration(self.root, owner_id="owner")
        self.assertEqual(config['profile'], history.CONFIG)
        self.assertTrue(all(config[k] is None for k in ('remote_url','remote_ref','records_ref')))
        original = delivery.git
        def no_transport(root, *args, **kwargs):
            self.assertNotIn(args[0], {'fetch','push','ls-remote','worktree'})
            return original(root, *args, **kwargs)
        with patch.object(delivery, 'git', side_effect=no_transport):
            _, result = self.apply()
        self.assertTrue(result['native']['complete'])
        self.assertEqual(result['history']['state'], 'committed')
        self.assertEqual(delivery.text(self.root, 'for-each-ref', '--format=%(refname)'), 'refs/heads/personal')
        self.assertFalse((self.root / '.git/eutonos-delivery/records').exists())

    def test_native_success_history_failure_and_original_recovery(self):
        original = delivery.git
        def failed_commit(root, *args, **kwargs):
            if args[0] == 'commit':
                raise delivery.DeliveryError('Fixture author unavailable')
            return original(root, *args, **kwargs)
        with patch.object(delivery, 'git', side_effect=failed_commit):
            prepared, result = self.apply()
        self.assertTrue(result['native']['complete'])
        self.assertEqual(result['history']['state'], 'uncertain')
        self.assertEqual(len(personal.read(self.root, owner_id='owner')['items']), 1)
        with self.assertRaises(ts.ReadError):
            history.assert_ready(self.root)
        with self.assertRaises(delivery.DeliveryError):
            history.configure_local(self.root, 'owner', expected_revision=1)
        self.assertEqual(history.read_config(self.root, 'owner')['revision'], 1)
        after = (self.root / 'goals.md').read_bytes() + b'\nLater owner prose\n'
        (self.root / 'goals.md').write_bytes(after)
        with patch.object(LocalRoot, 'replace', side_effect=AssertionError('Native replay forbidden')):
            recovered = self.recover(prepared)
        self.assertEqual(recovered['history']['state'], 'committed')
        self.assertEqual((self.root / 'goals.md').read_bytes(), after)
        committed = delivery.blob(self.root, 'HEAD', 'goals.md')
        self.assertNotIn(b'Later owner prose', committed)
        history.assert_ready(self.root)

    def test_uncertain_commit_recovery_preserves_unrelated_index_and_working_files(self):
        (self.root / 'notes.txt').write_bytes(b'Owner scratch\n')
        delivery.git(self.root, 'add', 'notes.txt')
        index = delivery.git(self.root, 'ls-files', '--stage', '--', 'notes.txt').stdout
        (self.root / 'notes.txt').write_bytes(b'Later scratch\n')
        prepared = personal.prepare(self.root, self.request(), owner_id='owner')
        original = delivery.Engine._fault
        def crash(fault, stage):
            if stage == 'after_commit':
                raise Crash('After commit before journal')
            return original(fault, stage)
        with patch.object(delivery.Engine, '_fault', side_effect=crash):
            with self.assertRaises(Crash):
                history.apply(self.root, prepared, 'owner', self.host, 'actor', 'session', lambda:personal.verify(self.root,prepared,owner_id='owner'))
        committed = delivery.text(self.root, 'rev-parse', 'HEAD')
        with patch.object(LocalRoot, 'replace', side_effect=AssertionError('Native replay forbidden')):
            result = self.recover(prepared)
        self.assertEqual(result['history']['commit_oid'], committed)
        self.assertEqual(delivery.text(self.root, 'rev-list', '--count', self.base + '..HEAD'), '1')
        self.assertEqual(delivery.git(self.root, 'ls-files', '--stage', '--', 'notes.txt').stdout, index)
        self.assertEqual((self.root / 'notes.txt').read_bytes(), b'Later scratch\n')
        self.assertEqual(delivery.git(self.root, 'diff', '--cached', '--name-only').stdout, b'notes.txt\n')

    def test_bootstrap_missing_author_resumes_frozen_original_without_duplicate(self):
        root = self.home / 'bootstrap'
        files = personal.initial_files('owner')
        original = delivery.git
        def missing_author(selected, *args, **kwargs):
            if args[:2] == ('var','GIT_AUTHOR_IDENT'):
                raise delivery.DeliveryError('Git author/committer identity is unavailable.')
            return original(selected, *args, **kwargs)
        with patch.object(delivery, 'git', side_effect=missing_author):
            result = history.initialize(root, files, 'owner', 'bootstrap-session')
        self.assertTrue(result['native']['complete'])
        self.assertEqual(result['history']['state'], 'pending')
        self.assertIsNone(history._head(root))
        delivery.git(root, 'config', 'user.name', 'Bootstrap Fixture')
        delivery.git(root, 'config', 'user.email', 'bootstrap@example.invalid')
        first = history.initialize(root, files, 'owner', 'bootstrap-session')
        second = history.initialize(root, files, 'owner', 'bootstrap-session')
        self.assertEqual(first['history']['state'], 'committed')
        self.assertEqual(first['history']['commit_oid'], second['history']['commit_oid'])
        self.assertEqual(delivery.text(root, 'rev-list', '--count', 'HEAD'), '1')
        self.assertFalse(private_host(root).exists())

    def test_wrong_private_host_and_shared_delivery_configuration_refused(self):
        prepared = personal.prepare(self.root, self.request(), owner_id='owner')
        with self.assertRaises(ts.ReadError):
            history.apply(self.root, prepared, 'owner', self.home / 'shared-host', 'actor', 'session', lambda:None)
        with self.assertRaises(ts.ReadError):
            delivery.Engine(self.root).configure({})
        with self.assertRaises(delivery.DeliveryError):
            history.configure_local(self.root, 'owner', branch='refs/heads/unrelated', expected_revision=1)


    def test_generic_delivery_cli_cannot_reveal_or_recover_private_payloads(self):
        import io
        from contextlib import redirect_stdout
        from eutonos.git_delivery_cli import main
        _, result = self.apply()
        key = result['history']['request_id']
        for arguments in (['status'], ['inspect', key], ['recover', key], ['run', key]):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(['--root', str(self.root), *arguments]), 1)
            self.assertNotIn('Private sentinel', out.getvalue())
            self.assertNotIn(str(self.root), out.getvalue())
            self.assertNotIn('payload', out.getvalue())
        with self.assertRaises(ts.ReadError):
            native.inspect(self.root)
        with self.assertRaises(ts.ReadError):
            native.configuration(self.root)

    def test_linked_private_marker_refused_before_shared_journal_creation(self):
        shared = self.home / 'shared'; shared.mkdir()
        delivery.git(shared, 'init', '-b', 'shared')
        delivery.git(shared, 'config', 'user.name', 'Shared Fixture')
        delivery.git(shared, 'config', 'user.email', 'shared@example.invalid')
        (shared / 'public.txt').write_bytes(b'Public only')
        delivery.git(shared, 'add', 'public.txt')
        delivery.git(shared, 'commit', '-m', 'Shared baseline')
        linked = self.home / 'linked'
        delivery.git(shared, 'worktree', 'add', '-b', 'linked', str(linked))
        for path, body in personal.initial_files('owner').items():
            (linked / path).write_bytes(body)
        with self.assertRaises(ts.ReadError):
            history.configure_local(linked, 'owner', branch='refs/heads/linked')
        self.assertFalse((shared / '.git/eutonos-delivery').exists())
        with self.assertRaises(ts.ReadError):
            history.assert_ready(linked)
        self.assertFalse((shared / '.git/eutonos-delivery').exists())

    def test_alias_root_refused_before_private_history_effects(self):
        alias = self.home / 'alias'
        try:
            alias.symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest('Local symlink creation unavailable')
        before = delivery.text(self.root, 'rev-parse', 'HEAD')
        for call in (lambda:history.configure_local(alias,'owner',expected_revision=1),
                     lambda:history.read_config(alias,'owner')):
            with self.assertRaises(ts.ReadError):
                call()
        self.assertEqual(delivery.text(self.root, 'rev-parse', 'HEAD'), before)

    def test_configuration_form_read_is_passive(self):
        root = self.home / 'no-journal'; root.mkdir()
        for path, raw in personal.initial_files('owner').items():
            (root / path).write_bytes(raw)
        delivery.git(root, 'init', '-b', 'personal')
        self.assertEqual(history.read_config(root, 'owner')['revision'], 0)
        self.assertFalse((root / '.git/eutonos-delivery').exists())


    def test_reparse_git_storage_refused_before_journal_access(self):
        from types import SimpleNamespace
        original = Path.lstat
        nested = self.root / '.git/objects/pack/nested'
        nested.mkdir(parents=True)
        original_iterdir = Path.iterdir
        for selected in (self.root / '.git', nested):
            def junction(path, *args, **kwargs):
                info = original(path, *args, **kwargs)
                if path == selected:
                    return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
                return info
            def refuse_alias_scan(path):
                if path == selected:
                    raise AssertionError('Never enter aliased storage')
                return original_iterdir(path)
            with patch.object(Path, 'lstat', junction), patch.object(Path, 'iterdir', refuse_alias_scan):
                with self.assertRaises(ts.ReadError):
                    history.configure_local(self.root, 'owner', expected_revision=1)
                with self.assertRaises(ts.ReadError):
                    delivery.Engine(self.root)
