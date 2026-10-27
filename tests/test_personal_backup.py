import json
from pathlib import Path
import zipfile
from unittest.mock import patch

from test_personal_history import HistoryFixture
from eutonos import git_delivery as delivery, personal, personal_backup as backup, scoped_items, tokenslang as ts
from eutonos.effects import digest
from eutonos.private_scope import private_host


class BackupTests(HistoryFixture):
    def archive(self):
        destination = self.home / 'backup.zip'
        result = backup.export(self.root, destination, 'owner', self.host, 'actor')
        self.assertEqual(result['backup']['state'], 'verified')
        self.assertEqual(result['backup']['independence'], 'unverified')
        return destination

    def test_verified_uncommitted_snapshot_and_history_restore_preserves_identity(self):
        self.assert_uncommitted_snapshot_restore(self.request())

    def test_verified_uncommitted_project_restore_preserves_identity(self):
        request = personal.form(self.root, 'project', {'title':'Private project', 'summary':'Owner project'}, owner_id='owner')['request']
        request['item']['extensions']['personal_project']['purpose'] = 'Useful personal project'
        self.assert_uncommitted_snapshot_restore(request)

    def assert_uncommitted_snapshot_restore(self, request):
        original = delivery.git
        def fail_commit(root, *args, **kwargs):
            if args[0] == 'commit':
                raise delivery.DeliveryError('Fixture history unavailable')
            self.assertNotIn(args[0], {'fetch','push','ls-remote','worktree'})
            return original(root, *args, **kwargs)
        with patch.object(delivery, 'git', side_effect=fail_commit):
            prepared, written = self.apply(request)
        self.assertTrue(written['native']['complete'])
        expected = personal.read(self.root, owner_id='owner')
        reference = {'profile': scoped_items.PROFILE, **expected['identity'],
                     'item_id': expected['items'][0]['id']}
        selected = self.archive()
        target = self.home / 'restored'
        def no_transport(root, *args, **kwargs):
            self.assertNotIn(args[0], {'fetch','push','ls-remote','worktree'})
            return original(root, *args, **kwargs)
        with patch.object(delivery, 'git', side_effect=no_transport):
            result = backup.restore(selected, target, 'owner')
        restored = personal.read(target, owner_id='owner')
        self.assertEqual(restored['identity'], expected['identity'])
        self.assertEqual(restored['manifest_sha256'], expected['manifest_sha256'])
        self.assertEqual(restored['items'], expected['items'])
        self.assertEqual(scoped_items.resolve(target, reference, owner_id='owner')['outcome'], 'resolved')
        self.assertEqual(delivery.text(target, 'rev-parse', 'HEAD'), self.base)
        self.assertTrue(result['fresh_admission_required'])
        self.assertTrue(result['reenrollment_required'])
        self.assertNotEqual(private_host(target), self.host)
        self.assertFalse(private_host(target).exists())
        self.assertFalse((target / '.git/eutonos-delivery/bootstrap.json').exists())
        self.assertEqual(delivery.Engine(target).status(), [])
        with zipfile.ZipFile(selected) as z:
            self.assertEqual(set(z.namelist()), backup.ENTRIES)
            self.assertFalse(any('host' in name or 'journal' in name for name in z.namelist()))

    def test_checksummed_snapshot_history_identity_mismatch_refused_before_restore(self):
        self.apply()
        original = self.archive()
        with zipfile.ZipFile(original) as z:
            raw = {name:z.read(name) for name in z.namelist()}
        for part in ('repository_id', 'scope_id', 'namespace', 'owner_id'):
            with self.subTest(part=part):
                content = dict(raw)
                manifest = json.loads(content['manifest.json'])
                snapshot = json.loads(content['snapshots/eutonos.personal.json'])
                if part == 'owner_id':
                    snapshot[part] = manifest[part] = 'different-owner'
                else:
                    snapshot['identity'][part] = manifest['identity'][part] = 'different-identity'
                path = 'snapshots/eutonos.personal.json'
                content[path] = ts.encoded(snapshot)
                manifest['files'][path] = {'sha256':digest(content[path]), 'size':len(content[path])}
                content['manifest.json'] = ts.encoded(manifest)
                archive = self.home / (part + '.zip')
                with zipfile.ZipFile(archive, 'w') as z:
                    for name, body in content.items():
                        z.writestr(name, body)
                target = self.home / (part + '-target')
                target.mkdir()
                with patch.object(backup.history, 'configure_local', side_effect=AssertionError('No destination configuration')):
                    with self.assertRaises(ts.ReadError):
                        backup.restore(archive, target, manifest['owner_id'])
                self.assertTrue(target.is_dir())
                self.assertEqual(list(target.iterdir()), [])

    def test_checksummed_ancestor_manifest_mismatch_refused_before_restore(self):
        self.apply()
        original = self.archive()
        with zipfile.ZipFile(original) as z:
            raw = {name:z.read(name) for name in z.namelist()}
        path = self.root / 'eutonos.personal.json'
        current = path.read_bytes()
        valid = delivery.text(self.root, 'rev-parse', 'HEAD')
        for part in ('identity', 'owner_id'):
            with self.subTest(part=part):
                branch = 'refs/heads/ancestor-' + part
                delivery.git(self.root, 'checkout', '-b', branch.removeprefix('refs/heads/'), valid)
                previous = json.loads(current)
                if part == 'identity':
                    previous['identity']['repository_id'] = 'different-identity'
                else:
                    previous['owner_id'] = 'different-owner'
                path.write_bytes(ts.encoded(previous))
                delivery.git(self.root, 'add', '--', path.name)
                delivery.git(self.root, 'commit', '-m', 'Inconsistent historical manifest')
                path.write_bytes(current)
                delivery.git(self.root, 'add', '--', path.name)
                delivery.git(self.root, 'commit', '-m', 'Matching current manifest')
                content = dict(raw)
                bundle = self.home / (part + '.bundle')
                delivery.git(self.root, 'bundle', 'create', str(bundle), branch)
                content['history.bundle'] = bundle.read_bytes()
                manifest = json.loads(content['manifest.json'])
                manifest['branch'] = branch
                manifest['commit_oid'] = delivery.text(self.root, 'rev-parse', 'HEAD')
                manifest['files']['history.bundle'] = {'sha256':digest(content['history.bundle']),
                                                      'size':len(content['history.bundle'])}
                content['manifest.json'] = ts.encoded(manifest)
                archive = self.home / ('ancestor-' + part + '.zip')
                with zipfile.ZipFile(archive, 'w') as z:
                    for name, body in content.items():
                        z.writestr(name, body)
                target = self.home / ('ancestor-' + part + '-target')
                with self.assertRaises(ts.ReadError):
                    backup.restore(archive, target, 'owner')
                self.assertFalse(target.exists())

    def test_export_refuses_unresolved_effect_and_shared_destination(self):
        from eutonos import managed_write
        prepared = personal.prepare(self.root, self.request(), owner_id='owner')
        with self.assertRaises(RuntimeError):
            managed_write.apply(self.root, self.host, 'actor', 'unresolved', 'unresolved-effect',
                prepared['effect'], prepared['fingerprint'], prepared['paths'], {},
                verify=lambda:(_ for _ in ()).throw(RuntimeError('Interrupted verifier')))
        with self.assertRaises(ts.ReadError):
            backup.export(self.root, self.home / 'blocked.zip', 'owner', self.host, 'actor')
        self.assertFalse((self.home / 'blocked.zip').exists())
        with self.assertRaises(ts.ReadError):
            backup.export(self.root, self.root / 'private.zip', 'owner', self.host, 'actor')
        shared = self.home / 'shared'; shared.mkdir()
        (shared / 'eutonos.shared-work.json').write_text('{}')
        with self.assertRaises(ts.ReadError):
            backup.export(self.root, shared / 'private.zip', 'owner', self.host, 'actor')

    def test_traversal_duplicate_checksum_and_identity_tamper_refused_before_restore(self):
        original = self.archive()
        with zipfile.ZipFile(original) as z:
            raw = {name:z.read(name) for name in z.namelist()}
        cases = []
        traversing = dict(raw); traversing['../secret'] = b'outside'
        cases.append(traversing)
        damaged = dict(raw); damaged['snapshots/goals.md'] = b'damaged'
        cases.append(damaged)
        identity = dict(raw)
        manifest = json.loads(identity['manifest.json'])
        manifest['identity']['repository_id'] = 'wrong-identity'
        identity['manifest.json'] = ts.encoded(manifest)
        cases.append(identity)
        malformed = dict(raw)
        malformed['snapshots/goals.md'] = b'No native board\n'
        manifest = json.loads(malformed['manifest.json'])
        manifest['files']['snapshots/goals.md'] = {'sha256':digest(malformed['snapshots/goals.md']),'size':len(malformed['snapshots/goals.md'])}
        malformed['manifest.json'] = ts.encoded(manifest)
        cases.append(malformed)
        for i, content in enumerate(cases):
            archive = self.home / ('malformed-' + str(i) + '.zip')
            with zipfile.ZipFile(archive, 'w') as z:
                for name, body in content.items():
                    z.writestr(name, body)
            target = self.home / ('target-' + str(i))
            with self.subTest(case=i), self.assertRaises((ts.ReadError, delivery.DeliveryError)):
                backup.restore(archive, target, 'owner')
            self.assertFalse(target.exists())
        duplicate = self.home / 'duplicate.zip'
        with zipfile.ZipFile(duplicate, 'w') as z:
            for name, body in raw.items():
                z.writestr(name, body)
            z.writestr('snapshots/goals.md', raw['snapshots/goals.md'])
        with self.assertRaises(ts.ReadError):
            backup.restore(duplicate, self.home / 'duplicate-target', 'owner')

    def test_wrong_owner_and_nonempty_restore_preserve_destination(self):
        archive = self.archive()
        target = self.home / 'occupied'; target.mkdir()
        (target / 'notes').write_bytes(b'Keep owner data')
        with self.assertRaises(ts.ReadError):
            backup.restore(archive, target, 'owner')
        self.assertEqual((target / 'notes').read_bytes(), b'Keep owner data')
        with self.assertRaises(ts.ReadError):
            backup.restore(archive, self.home / 'wrong-owner', 'different')
        self.assertFalse((self.home / 'wrong-owner').exists())
