"""ASW2 uses only disposable local Git remotes, with asserted starting boundaries."""
from pathlib import Path
import subprocess
import tempfile
import unittest


def git(root, *args, check=True):
    return subprocess.run(['git', '-C', str(root), *args], capture_output=True, check=check)


class LocalBench(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='eutonos-git-delivery-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.remote = self.home / 'remote.git'
        self.root = self.home / 'work'
        self.peer = self.home / 'peer'
        git(self.home, 'init', '--bare', str(self.remote))
        git(self.home, 'init', '-b', 'work', str(self.root))
        git(self.root, 'config', 'user.name', 'Delivery Fixture')
        git(self.root, 'config', 'user.email', 'fixture@example.invalid')
        (self.root / '.gitignore').write_text('.eutonos-local/\n', encoding='utf-8')
        (self.root / 'docs').mkdir()
        (self.root / 'docs/item.md').write_bytes(b'first\ncontext\nlast\n')
        (self.root / 'code.py').write_bytes(b'VALUE = 1\n')
        git(self.root, 'add', '.')
        git(self.root, 'commit', '-m', 'fixture baseline')
        self.base = git(self.root, 'rev-parse', 'HEAD').stdout.decode().strip()
        git(self.root, 'remote', 'add', 'origin', str(self.remote))
        git(self.root, 'push', 'origin', 'HEAD:refs/heads/work')
        git(self.home, 'clone', '-b', 'work', str(self.remote), str(self.peer))
        git(self.peer, 'config', 'user.name', 'Peer Fixture')
        git(self.peer, 'config', 'user.email', 'peer@example.invalid')
        # Establish exact refs, local-only destination and clean/index boundaries first.
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/work').stdout.decode().strip(), self.base)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD').stdout.decode().strip(), self.base)
        self.assertEqual(git(self.peer, 'rev-parse', 'HEAD').stdout.decode().strip(), self.base)
        self.assertEqual(git(self.root, 'status', '--porcelain').stdout, b'')
        self.assertEqual(git(self.root, 'diff', '--cached', '--name-only').stdout, b'')
        self.assertEqual(git(self.root, 'remote', 'get-url', 'origin').stdout.decode().strip(), str(self.remote))
        self.assertTrue(self.root.is_relative_to(self.home))
        self.assertTrue(self.peer.is_relative_to(self.home))
        self.assertTrue(self.remote.is_relative_to(self.home))

    def test_local_bench_initial_boundaries(self):
        self.assertEqual((self.root / 'code.py').read_bytes(), b'VALUE = 1\n')

import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from eutonos import git_delivery as delivery, git_delivery_native as native, tokenslang as ts
from eutonos.effects import Coordinator, FixtureAuthority, LocalRoot, MARKER, MARKER_BYTES, plan, decode


class SimulatedCrash(BaseException):
    pass


class DeliveryTests(LocalBench):
    def configure(self,mode='commit+push',checks=None,**changes):
        if not hasattr(self,'engine'): self.engine=delivery.Engine(self.root)
        value={'profile':delivery.CONFIG,'project_id':'fixture.repo','participant':'rick',
               'mode':mode,'remote_url':str(self.remote),'remote_ref':'refs/heads/work',
               'records_ref':'refs/heads/records/rick','branch':'refs/heads/work',
               'canonical_base':self.base,'allowed_paths':['code.py','docs/','RAM/','experience/','eutonos.ram.json','WORK.md'],
               'checks':checks or [[sys.executable,'-c',"from pathlib import Path; assert Path('code.py').is_file()"]]}
        value.update(changes)
        return self.engine.configure(value,expected_revision=self.engine.config()['revision'])

    def code(self,id='code-1'):
        self.configure() if not hasattr(self,'engine') else None
        after=b'VALUE = 2\n'
        (self.root/'code.py').write_bytes(after)
        return self.engine.submit_code(id,{'code.py':{'before':b'VALUE = 1\n','after':after}},session='session',work_id='work')

    def crash(self,stage):
        def fault(observed):
            if observed==stage: raise SimulatedCrash(stage)
        return fault

    def unchanged_remote(self):
        self.assertEqual(git(self.remote,'rev-parse','refs/heads/work').stdout.decode().strip(),self.base)

    def test_settings_delivery_cli_is_single_engine_and_status_is_passive(self):
        import io
        from contextlib import redirect_stdout
        from eutonos.settings_cli import main
        output=io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['delivery','status','--root',str(self.root)]),0)
        self.assertEqual(json.loads(output.getvalue())['configuration']['mode'],'manual')
        self.assertFalse((self.root/'.git/eutonos-delivery').exists())
        self.configure('commit-only')
        after=b'VALUE = 2\n';(self.root/'code.py').write_bytes(after)
        request=self.home/'request.json'
        request.write_text(json.dumps({'id':'cli-code','session':'session','work_id':'work',
            'changes':{'code.py':{'before':delivery.freeze(b'VALUE = 1\n'),'after':delivery.freeze(after)}}}))
        output=io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--root',str(self.root),'delivery','submit','--input',str(request)]),0)
        result=json.loads(output.getvalue());self.assertEqual(result['state'],'committed')
        output=io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['delivery','inspect',result['request_id'],'--root',str(self.root)]),0)
        self.assertEqual(json.loads(output.getvalue())['requests'][0]['commit_oid'],result['commit_oid'])
        self.assertEqual(len(self.engine.status()),1)

    def test_default_manual_and_pause_preserve_local_work(self):
        engine=delivery.Engine(self.root)
        self.assertEqual(engine.config()['mode'],'manual')
        self.configure('manual');key=self.code()
        result=self.engine.run(key)
        self.assertEqual(result['state'],'paused')
        self.unchanged_remote();self.assertEqual(git(self.root,'diff','--cached','--name-only').stdout,b'')

    def test_code_commit_push_and_duplicate_callback(self):
        key=self.code()
        (self.root/'docs/item.md').write_bytes(b'unrelated dirty docs\n')
        result=self.engine.run(key)
        self.assertEqual(result['state'],'published',result['error'])
        self.assertEqual(git(self.remote,'rev-parse','refs/heads/work').stdout.decode().strip(),result['commit_oid'])
        self.assertEqual((self.root/'docs/item.md').read_bytes(),b'unrelated dirty docs\n')
        self.assertEqual(self.code(),key)
        self.assertEqual(self.engine.run(key)['commit_oid'],result['commit_oid'])
        self.assertEqual(git(self.root,'rev-list','--count',self.base+'..HEAD').stdout.strip(),b'1')

    def normalized_code(self, *, allowed=None, changes=None):
        raw = b'VALUE = 2\r\n'
        changes = changes or {'code.py': {'before': b'VALUE = 1\n', 'after': raw}}
        for path, value in changes.items():
            if value['after'] is not None: (self.root / path).write_bytes(value['after'])
        check = "from pathlib import Path; assert Path('code.py').read_bytes() == b'VALUE = 2\\n'"
        self.configure('commit-only', checks=[[sys.executable, '-c', check]],
                       **({'allowed_paths': allowed} if allowed else {}))
        return self.engine.submit_code('normalized', changes, session='session', work_id='work')

    def test_normalized_autocrlf_checks_commit_bytes_and_retry_preserves_raw_work(self):
        git(self.root, 'config', 'core.autocrlf', 'true')
        key = self.normalized_code()
        (self.root / 'docs/item.md').write_bytes(b'unrelated raw dirty\r\n')
        with self.assertRaises(SimulatedCrash): self.engine.run(key, fault=self.crash('after_commit'))
        result = self.engine.run(key)
        self.assertEqual(result['state'], 'committed', result['error'])
        self.assertEqual(delivery.blob(self.root, result['commit_oid'], 'code.py'), b'VALUE = 2\n')
        self.assertEqual((self.root / 'code.py').read_bytes(), b'VALUE = 2\r\n')
        self.assertEqual((self.root / 'docs/item.md').read_bytes(), b'unrelated raw dirty\r\n')
        self.assertEqual(self.engine.run(key)['commit_oid'], result['commit_oid'])
        self.assertEqual(git(self.root, 'rev-list', '--count', self.base + '..HEAD').stdout.strip(), b'1')
        self.unchanged_remote()

    def test_normalized_selected_attributes_and_frozen_policy_survive_staging_retry(self):
        git(self.root, 'config', 'core.autocrlf', 'false')
        changes = {'code.py': {'before': b'VALUE = 1\n', 'after': b'VALUE = 2\r\n'},
                   '.gitattributes': {'before': None, 'after': b'code.py text eol=lf\r\n'}}
        key = self.normalized_code(allowed=['code.py', '.gitattributes'], changes=changes)
        with self.assertRaises(SimulatedCrash): self.engine.run(key, fault=self.crash('before_staging'))
        # Once frozen, recovery must not rerun any changed local conversion policy.
        git(self.root, 'config', 'core.autocrlf', 'true')
        with patch.object(self.engine, '_normalized_outputs', side_effect=AssertionError('conversion replay')):
            result = self.engine.run(key)
        self.assertEqual(result['state'], 'committed', result['error'])
        self.assertEqual(delivery.blob(self.root, result['commit_oid'], 'code.py'), b'VALUE = 2\n')
        self.assertEqual((self.root / '.gitattributes').read_bytes(), changes['.gitattributes']['after'])

    def test_normalized_parent_attributes_ignore_unselected_dirty_attributes(self):
        (self.root / '.gitattributes').write_bytes(b'code.py text eol=lf\n')
        git(self.root, 'add', '.gitattributes'); git(self.root, 'commit', '-m', 'attribute baseline')
        floor = git(self.root, 'rev-parse', 'HEAD').stdout.decode().strip()
        (self.root / '.gitattributes').write_bytes(b'code.py -text\n')
        git(self.root, 'config', 'core.autocrlf', 'false')
        key = self.normalized_code()
        result = self.engine.run(key)
        self.assertEqual(result['state'], 'committed', result['error'])
        self.assertEqual(delivery.blob(self.root, result['commit_oid'], '.gitattributes'), b'code.py text eol=lf\n')
        self.assertEqual((self.root / '.gitattributes').read_bytes(), b'code.py -text\n')
        self.assertEqual(git(self.root, 'rev-list', '--count', floor + '..HEAD').stdout.strip(), b'1')

    def test_relative_required_filter_uses_pinned_helper_and_preserves_dirty_helper(self):
        import shlex
        git(self.root, 'config', 'core.autocrlf', 'false')
        (self.root / 'tools').mkdir()
        helper = self.root / 'tools/filter.py'
        pinned = b'import sys\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n'
        helper.write_bytes(pinned)
        (self.root / '.gitattributes').write_bytes(b'code.py filter=identity\n')
        command = shlex.quote(sys.executable.replace('\\', '/')) + ' tools/filter.py'
        git(self.root, 'config', 'filter.identity.clean', command)
        git(self.root, 'config', 'filter.identity.required', 'true')
        git(self.root, 'add', 'tools/filter.py', '.gitattributes')
        git(self.root, 'commit', '-m', 'pinned relative identity filter')
        floor = delivery.text(self.root, 'rev-parse', 'HEAD')
        # Conversion must ignore this local helper's wrong output. Native
        # git commit may still refresh the caller index through its clean filter;
        # it must retain the frozen staged/checked bytes rather than that output.
        dirty = b"import sys\nsys.stdout.buffer.write(b'DIRTY FILTER OUTPUT\\n')\n"
        helper.write_bytes(dirty)
        key = self.normalized_code(changes={'code.py':{'before':b'VALUE = 1\n', 'after':b'VALUE = 2\n'}})
        with self.assertRaises(SimulatedCrash): self.engine.run(key, fault=self.crash('before_staging'))
        with patch.object(self.engine, '_normalized_outputs', side_effect=AssertionError('conversion replay')):
            result = self.engine.run(key)
        self.assertEqual(result['state'], 'committed', result['error'])
        self.assertEqual(delivery.blob(self.root, result['commit_oid'], 'code.py'), b'VALUE = 2\n')
        self.assertEqual(delivery.blob(self.root, result['commit_oid'], 'tools/filter.py'), pinned)
        self.assertEqual(helper.read_bytes(), dirty)
        self.assertEqual((self.root / 'code.py').read_bytes(), b'VALUE = 2\n')
        self.assertEqual(delivery.text(self.root, 'rev-list', '--count', floor + '..HEAD'), '1')
        self.assertEqual(self.engine.run(key)['commit_oid'], result['commit_oid'])
        self.assertEqual(git(self.root, 'diff', '--cached', '--name-only').stdout, b'')
        self.unchanged_remote()

    def test_normalized_existing_manual_commit_is_adopted(self):
        git(self.root, 'config', 'core.autocrlf', 'true')
        key = self.normalized_code()
        git(self.root, 'add', 'code.py'); git(self.root, 'commit', '-m', 'exact manually saved candidate')
        oid = git(self.root, 'rev-parse', 'HEAD').stdout.decode().strip()
        result = self.engine.run(key)
        self.assertEqual(result['state'], 'committed', result['error'])
        self.assertEqual(result['commit_oid'], oid)
        self.assertEqual(self.engine.run(key)['commit_oid'], oid)
        self.assertEqual(git(self.root, 'rev-list', '--count', self.base + '..HEAD').stdout.strip(), b'1')

    def test_normalized_equal_blob_raw_change_still_blocks_before_staging(self):
        git(self.root, 'config', 'core.autocrlf', 'true')
        key = self.normalized_code()
        def changed(stage):
            if stage == 'after_checks': (self.root / 'code.py').write_bytes(b'VALUE = 2\n')
        result = self.engine.run(key, fault=changed)
        self.assertEqual(result['state'], 'blocked', result['error'])
        self.assertIn('stale', result['error'])
        self.assertEqual(git(self.root, 'diff', '--cached', '--name-only').stdout, b'')
        self.unchanged_remote()

    def test_normalized_request_preserves_preoccupied_index(self):
        git(self.root, 'config', 'core.autocrlf', 'true')
        key = self.normalized_code()
        (self.root / 'docs/item.md').write_bytes(b'unrelated staged\r\n'); git(self.root, 'add', 'docs/item.md')
        index = self.root / delivery.text(self.root, 'rev-parse', '--git-path', 'index')
        before = index.read_bytes()
        result = self.engine.run(key)
        self.assertEqual(result['state'], 'blocked', result['error'])
        self.assertEqual(index.read_bytes(), before)
        self.assertEqual(delivery.text(self.root, 'rev-parse', 'HEAD'), self.base)

    def test_normalized_failed_check_retry_reuses_frozen_candidate(self):
        git(self.root, 'config', 'core.autocrlf', 'true')
        key = self.normalized_code()
        with patch.object(self.engine, '_checks', side_effect=delivery.DeliveryError('temporary check failure')):
            self.assertEqual(self.engine.run(key)['state'], 'blocked')
        git(self.root, 'config', 'core.autocrlf', 'false')
        with patch.object(self.engine, '_normalized_outputs', side_effect=AssertionError('conversion replay')):
            result = self.engine.run(key)
        self.assertEqual(result['state'], 'committed', result['error'])
        self.assertEqual(delivery.blob(self.root, result['commit_oid'], 'code.py'), b'VALUE = 2\n')

    def test_existing_persisted_staging_plan_without_new_fields_is_reused(self):
        key = self.code()
        with self.assertRaises(SimulatedCrash): self.engine.run(key, fault=self.crash('before_staging'))
        plan = json.loads(self.engine.status(key)[0]['plan'])
        del plan['working_outputs']; del plan['existing_commit']
        self.engine._save(key, plan=delivery.encoded(plan))
        with patch.object(self.engine, '_normalized_outputs', side_effect=AssertionError('legacy replan')):
            self.assertEqual(self.engine.run(key)['state'], 'published')

    def test_normalized_native_records_update_retry_preserves_caller_raw_bytes(self):
        git(self.root, 'config', 'core.autocrlf', 'true')
        self.configure('commit-only', checks=[[sys.executable, '-c',
            "from pathlib import Path; assert Path('docs/item.md').read_bytes() == b'first\\nchanged\\nlast\\n'"]])
        self.setup_native()
        before = b'first\r\ncontext\r\nlast\r\n'
        after = b'first\r\nchanged\r\nlast\r\n'
        (self.root / 'docs/item.md').write_bytes(before)
        effect = plan(self.local, {'docs/item.md': after})
        with patch.object(native.managed_write, 'connect', return_value=(self.coordinator, self.authority)), \
             patch.object(native.managed_write, 'abandon_lease', return_value={'retired_grants': []}):
            result = native.managed_apply(self.engine, 'normalized-native', effect=effect,
                verify=lambda:self.assertEqual((self.root / 'docs/item.md').read_bytes(), after), **self.context)
        key = result['request_id']
        with self.assertRaises(SimulatedCrash): self.engine.run(key, fault=self.crash('after_commit'))
        saved = self.engine.run(key)
        self.assertEqual(saved['state'], 'committed', saved['error'])
        self.assertEqual(delivery.blob(self.root, saved['commit_oid'], 'docs/item.md'), b'first\nchanged\nlast\n')
        self.assertEqual((self.root / 'docs/item.md').read_bytes(), after)
        self.assertEqual(delivery.text(self.root, 'rev-parse', 'HEAD'), self.base)
        self.assertEqual(delivery.text(self.root, 'rev-list', '--count', self.base + '..' + saved['commit_oid']), '1')
        self.assertEqual(self.engine.run(key)['commit_oid'], saved['commit_oid'])
        self.unchanged_remote()

    def test_preoccupied_index_preserved(self):
        key=self.code();(self.root/'docs/item.md').write_bytes(b'pre-staged\n')
        git(self.root,'add','docs/item.md')
        index=Path(git(self.root,'rev-parse','--git-path','index').stdout.decode().strip())
        if not index.is_absolute():index=self.root/index
        before=index.read_bytes()
        result=self.engine.run(key)
        self.assertEqual(result['state'],'blocked');self.assertIn('index',result['error'])
        self.assertEqual(index.read_bytes(),before);self.unchanged_remote()

    def test_stale_selected_source_blocks_before_staging(self):
        key=self.code();(self.root/'code.py').write_bytes(b'VALUE = 3\n')
        self.assertEqual(self.engine.run(key)['state'],'blocked');self.unchanged_remote()
        self.assertEqual(git(self.root,'diff','--cached','--name-only').stdout,b'')

    def test_after_check_source_change_blocks(self):
        key=self.code()
        def fault(stage):
            if stage=='after_checks':(self.root/'code.py').write_bytes(b'VALUE = 3\n')
        self.assertEqual(self.engine.run(key,fault=fault)['state'],'blocked');self.unchanged_remote()

    def test_successful_check_mutating_dependency_blocks(self):
        self.configure(checks=[[sys.executable,'-c',"from pathlib import Path;Path('docs/item.md').write_text('changed dependency')"]])
        result=self.engine.run(self.code())
        self.assertEqual(result['state'],'blocked');self.assertIn('candidate',result['error']);self.unchanged_remote()
        self.assertEqual(git(self.root,'rev-parse','HEAD').stdout.decode().strip(),self.base)

    def test_check_mutate_then_restore_is_not_valid_evidence(self):
        self.configure(checks=[[sys.executable,'-c',"from pathlib import Path;Path('docs/item.md').write_text('changed')"],
                               [sys.executable,'-c',"from pathlib import Path;Path('docs/item.md').write_text('first\\ncontext\\nlast\\n')"]])
        self.assertEqual(self.engine.run(self.code())['state'],'blocked');self.unchanged_remote()

    def test_check_cannot_discover_enclosing_git_or_inherit_git_binding(self):
        self.configure(checks=[[sys.executable,'-c',"import os,subprocess;assert 'GIT_DIR' not in os.environ;assert subprocess.run(['git','rev-parse','--git-dir'],capture_output=True).returncode!=0"]])
        key=self.code()
        with patch.dict(os.environ,{'GIT_DIR':str(self.root/'.git'),'GIT_INDEX_FILE':str(self.root/'.git/index')}):
            result=self.engine.run(key)
        self.assertEqual(result['state'],'published',result['error'])

    def test_index_race_does_not_commit_unselected_allowed_path(self):
        key=self.code();(self.root/'docs/other.md').write_bytes(b'unrelated staged racing writer\n')
        original=delivery.git
        staged=False
        def racing(root,*args,**kwargs):
            nonlocal staged
            if args[0]=='update-index' and '--index-info' in args and not staged:
                staged=True;original(root,'add','docs/other.md')
            return original(root,*args,**kwargs)
        with patch.object(delivery,'git',side_effect=racing):result=self.engine.run(key)
        self.assertEqual(result['state'],'blocked');self.assertIn('Staged tree',result['error']);self.unchanged_remote()
        self.assertIn(b'docs/other.md',git(self.root,'diff','--cached','--name-only').stdout)
        self.assertEqual(git(self.root,'rev-parse','HEAD').stdout.decode().strip(),self.base)

    def test_commit_crash_recovers_without_second_commit(self):
        key=self.code()
        with self.assertRaises(SimulatedCrash):self.engine.run(key,fault=self.crash('after_commit'))
        oid=git(self.root,'rev-parse','HEAD').stdout.decode().strip()
        result=self.engine.run(key)
        self.assertEqual(result['state'],'published',result['error']);self.assertEqual(result['commit_oid'],oid)
        self.assertEqual(git(self.root,'rev-list','--count',self.base+'..HEAD').stdout.strip(),b'1')

    def test_push_crash_remote_descendant_readback(self):
        key=self.code()
        with self.assertRaises(SimulatedCrash):self.engine.run(key,fault=self.crash('after_push'))
        oid=git(self.root,'rev-parse','HEAD').stdout.decode().strip()
        git(self.peer,'pull','--ff-only');(self.peer/'docs/peer.md').write_bytes(b'peer next\n')
        git(self.peer,'add','.');git(self.peer,'commit','-m','peer');git(self.peer,'push','origin','HEAD:refs/heads/work')
        self.assertEqual(self.engine.run(key)['state'],'published')
        self.assertEqual(self.engine.status(key)[0]['commit_oid'],oid)

    def test_uncertain_push_survives_second_readback_failure_and_recovers_descendant(self):
        key = self.code()
        with self.assertRaises(SimulatedCrash):
            self.engine.run(key, fault=self.crash('after_push'))
        oid = self.engine.status(key)[0]['commit_oid']
        git(self.peer, 'pull', '--ff-only')
        (self.peer / 'docs/peer.md').write_bytes(b'peer next\n')
        git(self.peer, 'add', '.')
        git(self.peer, 'commit', '-m', 'peer descendant')
        git(self.peer, 'push', 'origin', 'HEAD:refs/heads/work')
        with patch.object(self.engine, '_published', side_effect=delivery.DeliveryError('temporary readback failure')):
            result = self.engine.run(key)
        self.assertEqual(result['state'], 'push-uncertain')
        self.assertEqual(self.engine.run(key)['state'], 'published')
        self.assertEqual(self.engine.status(key)[0]['commit_oid'], oid)

    def test_uncertain_commit_survives_repeated_recovery_failure(self):
        key = self.code()
        with self.assertRaises(SimulatedCrash):
            self.engine.run(key, fault=self.crash('after_commit'))
        with patch.object(self.engine, '_worktree', side_effect=delivery.DeliveryError('temporary source unavailable')):
            result = self.engine.run(key)
        self.assertEqual(result['state'], 'commit-uncertain')
        original = delivery.text
        def unavailable_head(root, *args):
            if args == ('rev-parse', 'HEAD'):
                raise delivery.DeliveryError('temporary commit readback failure')
            return original(root, *args)
        with patch.object(delivery, 'text', side_effect=unavailable_head):
            result = self.engine.run(key)
        self.assertEqual(result['state'], 'commit-uncertain')
        self.assertEqual(self.engine.run(key)['state'], 'published')

    def test_materialization_bounds_duplicate_blob_paths(self):
        # Two identical files share one Git blob but occupy two files on disk.
        body = b'x' * 33_000_000
        (self.root / 'docs/a.bin').write_bytes(body)
        (self.root / 'docs/b.bin').write_bytes(body)
        git(self.root, 'add', 'docs/a.bin', 'docs/b.bin')
        git(self.root, 'commit', '-m', 'duplicate blobs')
        parent = git(self.root, 'rev-parse', 'HEAD').stdout.decode().strip()
        with self.assertRaisesRegex(delivery.DeliveryError, 'materialized bytes'):
            delivery.Engine(self.root)._materialize(self.root, parent, {}, self.home / 'candidate')

    def test_materialization_bounds_selected_outputs_before_any_candidate_write(self):
        (self.root / 'docs/base.bin').write_bytes(b'x' * 63_000_000)
        git(self.root, 'add', 'docs/base.bin')
        git(self.root, 'commit', '-m', 'large baseline')
        parent = git(self.root, 'rev-parse', 'HEAD').stdout.decode().strip()
        # This selected addition fits the 2 MB scope limit but exceeds the
        # complete candidate limit when combined with the parent tree.
        outputs = {'docs/selected.bin': b'y' * 1_100_000}
        candidate = self.home / 'candidate'
        engine = delivery.Engine(self.root)
        with patch.object(Path, 'write_bytes') as write:
            with self.assertRaisesRegex(delivery.DeliveryError, 'after selected outputs'):
                engine._materialize(self.root, parent, outputs, candidate)
            write.assert_not_called()
        self.assertFalse(candidate.exists())

    def test_adopt_exact_existing_manual_commit(self):
        key=self.code();git(self.root,'add','code.py');git(self.root,'commit','-m','owner manual commit')
        oid=git(self.root,'rev-parse','HEAD').stdout.decode().strip()
        result=self.engine.run(key)
        self.assertEqual(result['state'],'published',result['error']);self.assertEqual(result['commit_oid'],oid)
        self.assertIsNotNone(result['checks'])

    def test_adoption_rejects_manual_executable_mode_contamination(self):
        key=self.code();git(self.root,'add','code.py')
        git(self.root,'update-index','--chmod=+x','code.py')
        git(self.root,'commit','-m','manual mode contamination')
        oid=git(self.root,'rev-parse','HEAD').stdout
        result=self.engine.run(key)
        self.assertEqual(result['state'],'blocked');self.assertIn('checked snapshot',result['error'])
        self.unchanged_remote();self.assertEqual(git(self.root,'rev-parse','HEAD').stdout,oid)
        self.assertIsNone(result['commit_oid'])

    def test_outgoing_reverted_ancestors_block(self):
        self.configure();(self.root/'docs/other.md').write_bytes(b'one\n')
        git(self.root,'add','.');git(self.root,'commit','-m','unpublished unrelated')
        (self.root/'docs/other.md').unlink();git(self.root,'add','.');git(self.root,'commit','-m','revert unrelated')
        result=self.engine.run(self.code())
        self.assertEqual(result['state'],'blocked');self.assertIn('ancestor',result['error']);self.unchanged_remote()

    def test_disable_queued_resume_does_not_redirect(self):
        key=self.code();self.configure('manual')
        self.assertEqual(self.engine.run(key)['state'],'paused')
        self.configure();self.assertEqual(self.engine.run(key)['state'],'paused')
        self.engine.resume(key);self.assertEqual(self.engine.run(key)['state'],'published')
        key2=self.code('code-2');self.configure(remote_ref='refs/heads/other')
        self.assertEqual(self.engine.run(key2)['state'],'blocked')
        with self.assertRaises(delivery.DeliveryError):self.engine.resume(key2)

    def test_disable_after_dispatched_push_reconciles_success(self):
        key=self.code()
        with self.assertRaises(SimulatedCrash):self.engine.run(key,fault=self.crash('after_push'))
        self.configure('manual')
        self.assertEqual(self.engine.run(key)['state'],'published')

    def test_nonfastforward_and_auth_failures_do_not_rewrite(self):
        key=self.code();(self.peer/'docs/peer.md').write_bytes(b'peer\n')
        git(self.peer,'add','.');git(self.peer,'commit','-m','peer');git(self.peer,'push','origin','HEAD:refs/heads/work')
        head=git(self.root,'rev-parse','HEAD').stdout
        self.assertEqual(self.engine.run(key)['state'],'blocked')
        self.assertEqual(git(self.root,'rev-parse','HEAD').stdout,head)
        self.assertEqual(git(self.root,'diff','--cached','--name-only').stdout,b'')

    def test_unknown_schema_and_config_visible(self):
        self.configure()
        with sqlite3.connect(self.engine.dbpath) as db:db.execute('PRAGMA user_version=99')
        db.close()
        with self.assertRaises(delivery.DeliveryError):delivery.Engine(self.root)
        with self.assertRaises(delivery.DeliveryError):self.engine.configure({'profile':'future.v2'})

    def test_hook_changes_staged_tree_withholds_push(self):
        key=self.code()
        hook=self.root/'.git/hooks/pre-commit'
        hook.write_text("#!/bin/sh\nprintf 'VALUE = 99\\n' > code.py\ngit add code.py\n",encoding='utf-8');hook.chmod(0o755)
        result=self.engine.run(key)
        self.assertEqual(result['state'],'commit-uncertain');self.assertIn('hook',result['error']);self.unchanged_remote()
        self.assertEqual(self.engine.run(key)['state'],'blocked')

    def setup_native(self):
        if hasattr(self,'coordinator'):return
        self.configure() if not hasattr(self,'engine') else None
        (self.root/MARKER).write_bytes(MARKER_BYTES)
        self.local=LocalRoot(self.root)
        self.coordinator=Coordinator.initialize(self.home/'coordinator.sqlite')
        self.authority=FixtureAuthority(self.local,'native-actor',frozenset({'docs/native.json','docs/item.md'}),time.time()+600)
        self.context={'host':self.home,'actor':'native-actor','attempt':'native-attempt','effect_id':'native-effect',
                      'session':'native-session','work_id':'native-work','checkpoint':{'completed':'verified native test','next':'none','pending':None}}

    def verifier(self):
        value=json.loads((self.root/'docs/native.json').read_bytes())
        ts.shape(value,{'profile','status'})
        ts.require(value['profile']=='fixture-native.v1' and value['status']=='active','invalid','Typed fixture contract failed.')

    def native_operation(self,body=None,fault=None,id='native-1'):
        self.setup_native()
        self.effect=plan(self.local,{'docs/native.json':body or b'{"profile":"fixture-native.v1","status":"active"}\n'})
        with patch.object(native.managed_write,'connect',return_value=(self.coordinator,self.authority)),patch.object(native.managed_write,'abandon_lease',return_value={'retired_grants':[]}):
            return native.managed_apply(self.engine,id,effect=self.effect,verify=self.verifier,fault=fault,**self.context)

    def confirm_native(self,key):
        with patch.object(native.managed_write,'connect',return_value=(self.coordinator,self.authority)),patch.object(native.managed_write,'abandon_lease',return_value={'retired_grants':[]}):
            return native.confirm(self.engine,key,host=self.home,verify=self.verifier,checkpoint=self.context['checkpoint'])

    def test_native_apply_crash_before_typed_verification_does_not_publish(self):
        with self.assertRaises(SimulatedCrash):self.native_operation(fault=self.crash('before_native_verification'))
        self.assertEqual(self.engine.drain_verified(),[])
        with self.engine.db() as db:key=db.execute('SELECT id FROM intents').fetchone()[0]
        with self.coordinator._db() as db:self.assertEqual(db.execute('SELECT state FROM effects').fetchone()[0],'completed')
        with patch.object(LocalRoot,'replace',side_effect=AssertionError('native replay forbidden')):
            self.confirm_native(key)
        self.assertEqual(self.engine.drain_verified(),[key])
        self.assertEqual(self.engine.run(key)['state'],'published')

    def test_native_marker_enqueue_gap_and_duplicate_recovery(self):
        with self.assertRaises(SimulatedCrash):self.native_operation(fault=self.crash('after_native_verified'))
        self.assertEqual(self.engine.status(),[])
        keys=self.engine.drain_verified();self.assertEqual(len(keys),1)
        self.confirm_native(keys[0]);self.assertEqual(self.engine.drain_verified(),[])
        self.assertEqual(self.engine.run(keys[0])['state'],'published')
        self.unchanged_remote()

    def test_invalid_native_typed_payload_never_gets_verified_marker(self):
        with self.assertRaises(ts.ReadError):self.native_operation(body=b'{"profile":"wrong","status":"active"}\n')
        self.assertEqual(self.engine.drain_verified(),[])
        self.assertEqual(self.engine.status(),[])

    def test_records_delta_preserves_caller_index_and_omits_unrelated_ancestor(self):
        self.setup_native()
        (self.root/'private-code.txt').write_bytes(b'unpublished\n');git(self.root,'add','private-code.txt');git(self.root,'commit','-m','unrelated code')
        original=(self.root/'docs/item.md').read_bytes()
        (self.root/'docs/item.md').write_bytes(b'unrelated first\ncontext\nlast\n')
        before=(self.root/'docs/item.md').read_bytes();after=before.replace(b'last',b'published last')
        self.effect=plan(self.local,{'docs/item.md':after})
        def typed():self.assertEqual((self.root/'docs/item.md').read_bytes(),after)
        with patch.object(native.managed_write,'connect',return_value=(self.coordinator,self.authority)),patch.object(native.managed_write,'abandon_lease',return_value={'retired_grants':[]}):
            result=native.managed_apply(self.engine,'delta',effect=self.effect,verify=typed,**self.context)
        git(self.root,'add','docs/item.md');index=git(self.root,'write-tree').stdout;head=git(self.root,'rev-parse','HEAD').stdout
        delivered=self.engine.run(result['request_id'])
        self.assertEqual(delivered['state'],'published',delivered['error'])
        self.assertEqual(git(self.remote,'show','refs/heads/records/rick:docs/item.md').stdout,original.replace(b'last',b'published last'))
        self.assertNotEqual(git(self.remote,'show','refs/heads/records/rick:private-code.txt',check=False).returncode,0)
        self.assertEqual(git(self.root,'write-tree').stdout,index);self.assertEqual(git(self.root,'rev-parse','HEAD').stdout,head)
        self.assertEqual((self.root/'docs/item.md').read_bytes(),after)

    def test_samefile_overlap_refused(self):
        self.setup_native();before=b'first\ncontext\nunrelated last\n';after=b'first\ncontext\nnew last\n'
        (self.root/'docs/item.md').write_bytes(before)
        self.effect=plan(self.local,{'docs/item.md':after})
        with patch.object(native.managed_write,'connect',return_value=(self.coordinator,self.authority)),patch.object(native.managed_write,'abandon_lease',return_value={'retired_grants':[]}):
            result=native.managed_apply(self.engine,'overlap',effect=self.effect,verify=lambda:self.assertEqual((self.root/'docs/item.md').read_bytes(),after),**self.context)
        self.assertEqual(self.engine.run(result['request_id'])['state'],'blocked')
        self.assertNotEqual(git(self.remote,'rev-parse','refs/heads/records/rick',check=False).returncode,0)

    def test_linked_worktree_dispatch_serializes_duplicate_request(self):
        key=self.native_operation()['request_id']
        linked=self.home/'linked';git(self.root,'worktree','add','-b','linked',str(linked),self.base)
        second=delivery.Engine(linked)
        # Common-dir journal is visible to both; request execution remains bound
        # to the original local source, never implicitly redirected to linked.
        self.assertEqual(second.dbpath,self.engine.dbpath)
        def dispatch(publisher):
            try:return publisher.run(key)
            except TimeoutError:return None  # Bounded lock contention has no effect.
        publishers=(self.engine,second)
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(dispatch,publishers))
        self.assertTrue(any(results))
        # A slow local Git dispatch may exceed the fixed lock wait. Reconcile
        # after the first dispatcher finishes; never create a second request.
        results=[result or publisher.run(key) for publisher,result in zip(publishers,results)]
        self.assertEqual([r['state'] for r in results],['published','published'])
        self.assertEqual(len({r['commit_oid'] for r in results}),1)
        self.assertEqual(git(self.remote,'rev-list','--count',self.base+'..refs/heads/records/rick').stdout.strip(),b'1')

    def test_staging_intent_crash_recovers_own_index_only(self):
        key=self.code()
        with self.assertRaises(SimulatedCrash):self.engine.run(key,fault=self.crash('before_staging'))
        self.assertEqual(self.engine.status(key)[0]['state'],'staging')
        self.assertEqual(self.engine.run(key)['state'],'published')
        self.assertEqual(git(self.root,'rev-list','--count',self.base+'..HEAD').stdout.strip(),b'1')

    def test_explicit_auth_rejection_is_reconciled_without_duplicate_commit(self):
        key=self.code();original=delivery.git
        def denied(root,*args,**kwargs):
            if args[0]=='push':raise delivery.DeliveryError('Simulated authentication denied')
            return original(root,*args,**kwargs)
        with patch.object(delivery,'git',side_effect=denied):result=self.engine.run(key)
        self.assertEqual(result['state'],'push-uncertain');self.assertIn('authentication',result['error']);self.unchanged_remote()
        oid=result['commit_oid'];self.assertEqual(self.engine.run(key)['state'],'published')
        self.assertEqual(self.engine.status(key)[0]['commit_oid'],oid)

    def test_check_added_source_file_is_not_accepted(self):
        self.configure(checks=[[sys.executable,'-c',"from pathlib import Path;Path('docs/new-source.md').write_text('extra')"]])
        self.assertEqual(self.engine.run(self.code())['state'],'blocked');self.unchanged_remote()

    def test_check_mode_change_is_not_accepted(self):
        self.configure(checks=[[sys.executable,'-c',"from pathlib import Path;Path('docs/item.md').chmod(0o444)"]])
        self.assertEqual(self.engine.run(self.code())['state'],'blocked');self.unchanged_remote()

    def test_git_export_attributes_do_not_transform_checked_tree(self):
        (self.root/'.gitattributes').write_text('docs/item.md export-ignore\ncode.py export-subst\n',encoding='utf-8')
        (self.root/'code.py').write_bytes(b"VALUE = 1\n# $Format:%H$\n")
        git(self.root,'add','.');git(self.root,'commit','-m','attribute fixture');git(self.root,'push','origin','HEAD:refs/heads/work')
        self.base=git(self.root,'rev-parse','HEAD').stdout.decode().strip()
        self.configure(checks=[[sys.executable,'-c',"from pathlib import Path;assert Path('docs/item.md').read_bytes()==b'first\\ncontext\\nlast\\n';assert b'$Format:%H$' in Path('code.py').read_bytes()"]])
        before=(self.root/'code.py').read_bytes();after=before.replace(b'1',b'2',1);(self.root/'code.py').write_bytes(after)
        key=self.engine.submit_code('attributes',{'code.py':{'before':before,'after':after}},session='session',work_id='work')
        self.assertEqual(self.engine.run(key)['state'],'published')

    def test_relative_destination_is_refused(self):
        with self.assertRaises(delivery.DeliveryError):self.configure(remote_url='remote.git')
        self.assertEqual(self.engine.config()['revision'],0)

    def test_canonical_base_is_required_even_when_destination_has_other_history(self):
        git(self.root,'checkout','--orphan','alien');git(self.root,'add','.');git(self.root,'commit','-m','independent baseline')
        git(self.root,'push','origin','HEAD:refs/heads/alien')
        self.configure(branch='refs/heads/alien',remote_ref='refs/heads/alien')
        result=self.engine.run(self.code())
        self.assertEqual(result['state'],'blocked');self.assertIn('canonical baseline',result['error'])
        self.assertEqual(git(self.root,'diff','--cached','--name-only').stdout,b'')

    def test_records_commit_only_then_push_retains_prior_bound_records(self):
        self.configure('commit-only');first=self.native_operation()['request_id']
        self.assertEqual(self.engine.run(first)['state'],'committed')
        self.context.update(attempt='native-attempt-2',effect_id='native-effect-2')
        second=self.native_operation(body=b'{"profile": "fixture-native.v1", "status": "active"}\n',id='native-2')['request_id']
        result=self.engine.run(second)
        self.assertEqual(result['state'],'committed',result['error'])
        self.configure();self.engine.resume(second)
        result=self.engine.run(second);self.assertEqual(result['state'],'published',result['error'])
        self.assertEqual(git(self.remote,'rev-list','--count',self.base+'..refs/heads/records/rick').stdout.strip(),b'2')

    def test_native_durable_marker_is_historical_not_invalidated_by_later_writer(self):
        result=self.native_operation();key=result['request_id']
        (self.root/'docs/native.json').write_bytes(b'later writer\n')
        receipt=self.confirm_native(key)
        self.assertTrue(receipt['complete'])
        self.assertEqual(self.engine.run(key)['state'],'published')
        self.assertEqual((self.root/'docs/native.json').read_bytes(),b'later writer\n')

    def test_record_delete_uses_isolated_path(self):
        self.setup_native();self.effect=plan(self.local,{'docs/item.md':None})
        with patch.object(native.managed_write,'connect',return_value=(self.coordinator,self.authority)),patch.object(native.managed_write,'abandon_lease',return_value={'retired_grants':[]}):
            result=native.managed_apply(self.engine,'delete',effect=self.effect,verify=lambda:self.assertFalse((self.root/'docs/item.md').exists()),**self.context)
        self.assertEqual(self.engine.run(result['request_id'])['state'],'published')
        self.assertNotEqual(git(self.remote,'show','refs/heads/records/rick:docs/item.md',check=False).returncode,0)
        self.assertEqual(git(self.root,'rev-parse','HEAD').stdout.decode().strip(),self.base)

    def ram_record(self,store_root='RAM',memory='parent'):
        from eutonos import ram
        if not (self.root/'eutonos.ram.json').exists():
            work={'profile':ts.WORK,'work_id':'work','goal':'Fixture','status':'selected','required':[],'optional':[]}
            (self.root/'WORK.md').write_bytes(b'# Fixture\n\n```tokenslang-work\n'+ts.encoded(work)+b'\n```\n')
            cfg={'profile':ram.CONFIG,'repository_id':'fixture.repo','scope_id':'fixture.scope','namespace':'fixture.native','store_root':store_root,'current_task':'WORK.md','work_owners':{'work':'WORK.md'}}
            (self.root/'eutonos.ram.json').write_bytes(ts.encoded(cfg));(self.root/store_root).mkdir()
            git(self.root,'add','.');git(self.root,'commit','-m','native RAM fixture');git(self.root,'push','origin','HEAD:refs/heads/work')
            self.base=git(self.root,'rev-parse','HEAD').stdout.decode().strip()
        store=ram.Store(LocalRoot(self.root))
        identity={'work_id':'work','session_id':'session','issuer_id':'rick','memory_id':memory}
        request={'profile':ram.WRITE,'operation_id':'capture-'+memory,'operation':'capture','identity':identity,'expected_revision':None,'at':'2026-10-08T12:00:00+00:00','reason':'Fixture proof',
                 'experience':{'kind':'summary','summary':'Native fixture','recorded_at':'2026-10-08T12:00:00+00:00','occurred_at':None,'unfinished':False,'actors':[{'id':'rick','attribution':'supplied'}],
                               'work_path':'WORK.md','source_commit':self.base,'sources':[{'path':'code.py','evidence':'inspected'}],'children':[],'skill':None}}
        planned=store.plan(request,'rick')
        path=store.path(identity)
        from eutonos.effects import decode
        body=next(decode(c['after']) for c in planned['effect']['changes'] if c['path']==path)
        return path,json.loads(body)

    def test_custom_ram_root_reuses_native_private_eligibility(self):
        path,record=self.ram_record('experience');self.configure()
        record['controls']['visibility']='private';body=ts.encoded(record);(self.root/path).write_bytes(body)
        with self.assertRaises(delivery.DeliveryError):self.engine.submit_code('private',{path:{'before':None,'after':body}},session='session',work_id='work')
        self.assertEqual(self.engine.status(),[]);self.unchanged_remote()

    def test_expired_ram_is_not_publishable(self):
        path,record=self.ram_record();self.configure()
        record['controls']['expires_at']='2000-01-01T00:00:00+00:00';body=ts.encoded(record);(self.root/path).write_bytes(body)
        with self.assertRaises(delivery.DeliveryError):self.engine.submit_code('expired',{path:{'before':None,'after':body}},session='session',work_id='work')
        self.assertEqual(self.engine.status(),[]);self.unchanged_remote()

    def test_ram_children_use_frozen_native_eligibility(self):
        path,parent=self.ram_record();child_path,child=self.ram_record(memory='child');self.configure()
        child['controls']['visibility']='private';(self.root/child_path).write_bytes(ts.encoded(child))
        parent['versions'][-1]['children']=[{'identity':child['identity'],'path':child_path,'revision':1}];body=ts.encoded(parent);(self.root/path).write_bytes(body)
        with self.assertRaisesRegex(delivery.DeliveryError,'private or forgotten'):self.engine.submit_code('parent',{path:{'before':None,'after':body}},session='session',work_id='work')
        self.assertEqual(self.engine.status(),[])

    def test_normalized_filter_output_rechecks_native_private_eligibility(self):
        import shlex
        path, record = self.ram_record()
        self.configure('commit-only')
        helper = self.root / '.git' / 'private-filter.py'
        helper.write_text("import json,sys; value=json.load(sys.stdin); value['controls']['visibility']='private'; json.dump(value,sys.stdout)", encoding='utf-8')
        command = shlex.quote(sys.executable.replace('\\', '/')) + ' ' + shlex.quote(str(helper).replace('\\', '/'))
        git(self.root, 'config', 'filter.private.clean', command)
        (self.root / '.git/info/attributes').write_text(path + ' filter=private\n')
        body = ts.encoded(record); (self.root / path).write_bytes(body)
        key = self.engine.submit_code('filtered-private', {path:{'before':None, 'after':body}}, session='session', work_id='work')
        result = self.engine.run(key)
        self.assertEqual(result['state'], 'blocked', result['error'])
        self.assertIn('private', result['error'])
        self.assertEqual(git(self.root, 'diff', '--cached', '--name-only').stdout, b'')
        self.assertEqual((self.root / path).read_bytes(), body)
        self.unchanged_remote()

    def test_public_native_ram_is_publishable(self):
        path,record=self.ram_record();self.configure();body=ts.encoded(record);(self.root/path).write_bytes(body)
        key=self.engine.submit_code('public',{path:{'before':None,'after':body}},session='session',work_id='work')
        result=self.engine.run(key);self.assertEqual(result['state'],'published',result['error'])

    def test_unknown_request_schema_refuses_git_effects(self):
        key=self.code()
        with self.engine.db() as db:
            row=db.execute('SELECT payload FROM jobs WHERE id=?',(key,)).fetchone();payload=json.loads(row[0]);payload['profile']='future.v2'
            db.execute('UPDATE jobs SET payload=? WHERE id=?',(delivery.encoded(payload),key))
        with self.assertRaises(delivery.DeliveryError):self.engine.run(key)
        self.unchanged_remote();self.assertEqual(git(self.root,'rev-parse','HEAD').stdout.decode().strip(),self.base)

    def test_write_tree_race_cannot_rebind_checked_candidate(self):
        key=self.code();(self.root/'docs/other.md').write_bytes(b'race before immutable tree\n')
        original=delivery.git;injected=False
        def racing(root,*args,**kwargs):
            nonlocal injected
            if args[0]=='write-tree' and not injected:
                injected=True;original(root,'add','docs/other.md')
            return original(root,*args,**kwargs)
        with patch.object(delivery,'git',side_effect=racing):result=self.engine.run(key)
        self.assertEqual(result['state'],'blocked');self.assertIn('Immutable staged tree',result['error']);self.unchanged_remote()
        self.assertEqual(git(self.root,'rev-parse','HEAD').stdout.decode().strip(),self.base)
        self.assertIn(b'docs/other.md',git(self.root,'diff','--cached','--name-only').stdout)

    def test_active_operation_and_executable_selected_path_refuse_before_effects(self):
        key=self.code();(self.root/'.git/CHERRY_PICK_HEAD').write_text(self.base,encoding='ascii')
        self.assertEqual(self.engine.run(key)['state'],'blocked');self.unchanged_remote()
        (self.root/'.git/CHERRY_PICK_HEAD').unlink()
        # Modes outside the initial ordinary-file contract are explicit errors.
        git(self.root,'update-index','--chmod=+x','code.py');git(self.root,'commit','-m','mode fixture')
        git(self.root,'push','origin','HEAD:refs/heads/work');self.base=git(self.root,'rev-parse','HEAD').stdout.decode().strip()
        self.configure()
        before=git(self.root,'show','HEAD:code.py').stdout;after=b'VALUE = 3\n';(self.root/'code.py').write_bytes(after)
        second=self.engine.submit_code('mode',{'code.py':{'before':before,'after':after}},session='session',work_id='work')
        result=self.engine.run(second);self.assertEqual(result['state'],'blocked');self.assertIn('ordinary',result['error'])

    def test_native_effect_cannot_be_announced_twice_under_new_logical_id(self):
        self.native_operation()
        with patch.object(native.managed_write,'connect',return_value=(self.coordinator,self.authority)):
            with self.assertRaises(delivery.DeliveryError):native.prepare(self.engine,'different-alias',effect=self.effect,**{k:v for k,v in self.context.items() if k!='checkpoint'})

    def test_private_native_capture_stays_saved_when_transport_blocks(self):
        from eutonos import ram
        path,record=self.ram_record();self.configure();self.setup_native()
        record['controls']['visibility']='private';body=ts.encoded(record)
        self.authority=FixtureAuthority(self.local,'native-actor',frozenset({path}),time.time()+600)
        effect=plan(self.local,{path:body})
        def typed():ram.Store(LocalRoot(self.root)).load(path)
        with patch.object(native.managed_write,'connect',return_value=(self.coordinator,self.authority)),patch.object(native.managed_write,'abandon_lease',return_value={'retired_grants':[]}):
            result=native.managed_apply(self.engine,'private-native',effect=effect,verify=typed,**self.context)
        self.assertTrue(result['native']['complete'])
        delivered=self.engine.run(result['request_id'])
        self.assertEqual(delivered['state'],'blocked');self.assertIn('ineligible',delivered['error'])
        self.assertEqual((self.root/path).read_bytes(),body)
        self.assertNotEqual(git(self.remote,'rev-parse','refs/heads/records/rick',check=False).returncode,0)


    def test_matching_git_url_rewrites_refuse_configuration_and_late_dispatch(self):
        other=self.home/'unconfigured.git';git(self.home,'init','--bare',str(other))
        for variable in ('insteadOf','pushInsteadOf'):
            with self.subTest(variable=variable):
                name='url.'+str(other)+'.'+variable
                git(self.root,'config',name,str(self.remote))
                with self.assertRaisesRegex(delivery.DeliveryError,'Git URL rewrite'):
                    self.configure()
                git(self.root,'config','--unset-all',name)
        self.configure();key=self.code()
        name='url.'+str(other)+'.pushInsteadOf'
        def change_routing(stage):
            if stage=='after_checks':git(self.root,'config',name,str(self.remote))
        result=self.engine.run(key,fault=change_routing)
        self.assertEqual(result['state'],'blocked');self.assertIn('Git URL rewrite',result['error'])
        self.assertIsNone(result['commit_oid']);self.unchanged_remote()
        self.assertEqual(git(self.root,'rev-parse','HEAD').stdout.decode().strip(),self.base)
        self.assertNotEqual(git(other,'rev-parse','refs/heads/work',check=False).returncode,0)
        git(self.root,'config','--unset-all',name)
        self.engine.resume(key);self.assertEqual(self.engine.run(key)['state'],'published')
        self.assertNotEqual(git(other,'rev-parse','refs/heads/work',check=False).returncode,0)

    def test_git_url_rewrite_blocks_lost_ack_readback_without_redirecting(self):
        key=self.code()
        with self.assertRaises(SimulatedCrash):self.engine.run(key,fault=self.crash('after_push'))
        other=self.home/'unconfigured.git';git(self.home,'init','--bare',str(other))
        name='url.'+str(other)+'.insteadOf';git(self.root,'config',name,str(self.remote))
        result=self.engine.run(key)
        self.assertEqual(result['state'],'push-uncertain');self.assertIn('Git URL rewrite',result['error'])
        git(self.root,'config','--unset-all',name)
        result=self.engine.run(key);self.assertEqual(result['state'],'published')
        self.assertEqual(git(self.remote,'rev-parse','refs/heads/work').stdout.decode().strip(),result['commit_oid'])
        self.assertNotEqual(git(other,'rev-parse','refs/heads/work',check=False).returncode,0)

    def test_commit_only_change_during_checks_refuses_executing_resume_and_recovers(self):
        key=self.code()
        def switch(stage):
            if stage=='after_checks':
                self.configure('commit-only')
                with self.assertRaisesRegex(delivery.DeliveryError,'executing'):
                    self.engine.resume(key)
        result=self.engine.run(key,fault=switch)
        self.assertEqual(result['state'],'paused');self.assertIsNone(result['commit_oid'])
        self.unchanged_remote()
        self.engine.resume(key);result=self.engine.run(key)
        self.assertEqual(result['state'],'committed',result['error']);self.unchanged_remote()
        self.assertEqual(git(self.root,'rev-list','--count',self.base+'..HEAD').stdout.strip(),b'1')

    def test_final_push_decision_uses_current_resumed_configuration(self):
        key=self.code();history=self.engine._history
        def switch(*args,**kwargs):
            result=history(*args,**kwargs)
            if self.engine.status(key)[0]['state']=='committed':
                self.configure('commit-only');self.engine.resume(key)
            return result
        with patch.object(self.engine,'_history',side_effect=switch):result=self.engine.run(key)
        self.assertEqual(result['state'],'committed',result['error']);self.unchanged_remote()
        self.assertEqual(self.engine.config()['mode'],'commit-only')

    def test_malformed_custom_ram_remains_ineligible_at_submit_and_candidate(self):
        path,record=self.ram_record('experience');self.configure()
        record['controls']['visibility']='private';record['versions'][-1]['summary']='PRIVATE FIXTURE'
        malformed=ts.encoded(record)+b' trailing malformed JSON'
        (self.root/path).write_bytes(malformed)
        with self.assertRaisesRegex(delivery.DeliveryError,'Native RAM is ineligible'):
            self.engine.submit_code('malformed',{path:{'before':None,'after':malformed}},session='session',work_id='work')
        self.assertEqual(self.engine.status(),[]);self.unchanged_remote()
        # A native save still succeeds locally; transport candidate validation
        # must reject the same malformed path without replaying that native save.
        self.setup_native();self.authority=FixtureAuthority(self.local,'native-actor',frozenset({path}),time.time()+600)
        (self.root/path).unlink();effect=plan(self.local,{path:malformed})
        with patch.object(native.managed_write,'connect',return_value=(self.coordinator,self.authority)),patch.object(native.managed_write,'abandon_lease',return_value={'retired_grants':[]}):
            result=native.managed_apply(self.engine,'malformed-native',effect=effect,verify=lambda:self.assertEqual((self.root/path).read_bytes(),malformed),**self.context)
        self.assertTrue(result['native']['complete'])
        result=self.engine.run(result['request_id'])
        self.assertEqual(result['state'],'blocked');self.assertIn('Native RAM is ineligible',result['error'])
        self.assertEqual((self.root/path).read_bytes(),malformed)
        self.assertNotEqual(git(self.remote,'rev-parse','refs/heads/records/rick',check=False).returncode,0)

    def test_malformed_custom_ram_outgoing_history_is_not_publishable(self):
        path,record=self.ram_record('experience');self.configure('commit-only')
        body=ts.encoded(record);(self.root/path).write_bytes(body)
        key=self.engine.submit_code('ram',{path:{'before':None,'after':body}},session='session',work_id='work')
        self.assertEqual(self.engine.run(key)['state'],'committed')
        record['controls']['visibility']='private';malformed=ts.encoded(record)+b' trailing JSON'
        (self.root/path).write_bytes(malformed);git(self.root,'add',path);git(self.root,'commit','-m','malformed ancestor')
        self.configure();second=self.code('after-malformed')
        result=self.engine.run(second)
        self.assertEqual(result['state'],'blocked');self.assertIn('Native RAM is ineligible',result['error']);self.unchanged_remote()

    def test_invalid_ram_configuration_cannot_reclassify_custom_store(self):
        path,record=self.ram_record('experience');self.configure()
        (self.root/'eutonos.ram.json').write_bytes(b'{malformed config')
        with self.assertRaisesRegex(delivery.DeliveryError,'Invalid native RAM configuration'):
            self.engine.submit_code('ram',{path:{'before':None,'after':ts.encoded(record)}},session='session',work_id='work')
        self.assertEqual(self.engine.status(),[]);self.unchanged_remote()

    def test_native_proof_survives_writer_between_cleanup_and_confirmation(self):
        original=native.managed_write.apply;receipts=[]
        later=b'{"profile":"fixture-native.v1","status":"later"}\n'
        def writer(*args,**kwargs):
            receipt=original(*args,**kwargs);receipts.append(receipt)
            state=self.coordinator.inspect(self.context['attempt'])
            self.assertEqual(state['state'],'paused');self.assertEqual(state['reservations'],[])
            (self.root/'docs/native.json').write_bytes(later)
            return receipt
        with patch.object(native.managed_write,'apply',side_effect=writer):result=self.native_operation()
        self.assertEqual(result['native'],receipts[0]);self.assertTrue(result['native']['complete'])
        key=result['request_id'];self.assertEqual(result['delivery']['state'],'queued')
        with patch.object(native.managed_write,'recover',side_effect=AssertionError('Historical proof must not read live owner')):
            self.assertTrue(self.confirm_native(key)['complete'])
        self.assertEqual(self.engine.run(key)['state'],'published')
        self.assertEqual(git(self.remote,'show','refs/heads/records/rick:docs/native.json').stdout,decode(self.effect['changes'][0]['after']))
        self.assertEqual((self.root/'docs/native.json').read_bytes(),later)

    def test_typed_native_proof_does_not_queue_until_cleanup_recovers(self):
        self.setup_native()
        def fail(*args,**kwargs):raise OSError('cleanup interrupted')
        with patch.object(self.coordinator,'closeout',side_effect=fail):
            with self.assertRaisesRegex(OSError,'cleanup interrupted'):self.native_operation()
        with self.engine.db() as db:row=db.execute('SELECT * FROM intents').fetchone()
        self.assertEqual(row['state'],'native-typed-verified');self.assertFalse(json.loads(row['receipt'])['complete'])
        self.assertEqual(self.engine.drain_verified(),[]);self.assertEqual(self.engine.status(),[])
        self.assertTrue(self.coordinator.inspect(self.context['attempt'])['reservations'])
        with patch.object(LocalRoot,'replace',side_effect=AssertionError('native replay forbidden')):
            receipt=self.confirm_native(row['id'])
        self.assertTrue(receipt['complete']);self.assertEqual(self.engine.drain_verified(),[row['id']])
        self.assertEqual(self.engine.drain_verified(),[])
        self.assertEqual(self.coordinator.inspect(self.context['attempt'])['reservations'],[])

    def test_crash_after_native_proof_recovers_without_replay(self):
        with self.assertRaises(SimulatedCrash):self.native_operation(fault=self.crash('after_native_typed_verified'))
        with self.engine.db() as db:row=db.execute('SELECT * FROM intents').fetchone()
        self.assertEqual(row['state'],'native-typed-verified');self.assertEqual(self.engine.drain_verified(),[])
        with patch.object(LocalRoot,'replace',side_effect=AssertionError('native replay forbidden')):
            self.assertTrue(self.confirm_native(row['id'])['complete'])
        self.assertEqual(self.engine.drain_verified(),[row['id']])

    def test_crash_after_cleanup_reconciles_frozen_proof_after_later_edit(self):
        with self.assertRaises(SimulatedCrash):self.native_operation(fault=self.crash('after_native_cleanup'))
        with self.engine.db() as db:row=db.execute('SELECT * FROM intents').fetchone()
        later=b'later owner bytes\n';(self.root/'docs/native.json').write_bytes(later)
        with patch.object(native.managed_write,'recover',side_effect=AssertionError('no live recovery after successful cleanup')):
            self.assertTrue(self.confirm_native(row['id'])['complete'])
        self.assertEqual(self.engine.drain_verified(),[row['id']]);self.assertEqual((self.root/'docs/native.json').read_bytes(),later)

    def test_failed_native_marker_preserves_reservations_and_recovers(self):
        with patch.object(native,'_typed_marker',return_value=lambda proof: (_ for _ in ()).throw(OSError('marker unavailable'))):
            with self.assertRaisesRegex(OSError,'marker unavailable'):self.native_operation()
        with self.engine.db() as db:row=db.execute('SELECT * FROM intents').fetchone()
        self.assertEqual(row['state'],'awaiting-native-verification');self.assertIsNone(row['receipt'])
        self.assertEqual(self.engine.drain_verified(),[])
        self.assertTrue(self.coordinator.inspect(self.context['attempt'])['reservations'])
        with patch.object(LocalRoot,'replace',side_effect=AssertionError('native replay forbidden')):
            self.assertTrue(self.confirm_native(row['id'])['complete'])
        self.assertEqual(self.engine.drain_verified(),[row['id']])

    def test_recovery_typed_failure_does_not_promote_saved_proof(self):
        self.setup_native()
        with patch.object(self.coordinator,'closeout',side_effect=OSError('cleanup interrupted')):
            with self.assertRaises(OSError):self.native_operation()
        with self.engine.db() as db:row=db.execute('SELECT * FROM intents').fetchone()
        with patch.object(self,'verifier',side_effect=ts.ReadError('invalid','typed recovery refused')):
            with self.assertRaises(ts.ReadError):self.confirm_native(row['id'])
        with self.engine.db() as db:self.assertEqual(db.execute('SELECT state FROM intents').fetchone()[0],'native-typed-verified')
        self.assertEqual(self.engine.drain_verified(),[]);self.assertEqual(self.engine.status(),[])

    def test_changed_native_effect_cannot_promote_historical_proof(self):
        with self.assertRaises(SimulatedCrash):self.native_operation(fault=self.crash('after_native_cleanup'))
        with self.engine.db() as db:row=db.execute('SELECT * FROM intents').fetchone()
        with self.coordinator._db() as db:db.execute("UPDATE effects SET fingerprint='different' WHERE id=?",(self.context['effect_id'],))
        with self.assertRaisesRegex(delivery.DeliveryError,'Native effect proof'):
            self.confirm_native(row['id'])
        self.assertEqual(self.engine.drain_verified(),[]);self.assertEqual(self.engine.status(),[])


    def test_delivery_journal_failure_preserves_successful_native_receipt(self):
        self.setup_native()
        self.effect=plan(self.local,{'docs/native.json':b'{"profile":"fixture-native.v1","status":"active"}\n'})
        original=native.managed_write.apply;receipts=[]
        def applied(*args,**kwargs):
            receipt=original(*args,**kwargs);receipts.append(receipt);return receipt
        errors=(OSError('handoff journal unavailable'),ts.ReadError('unavailable','handoff native host unavailable'))
        with patch.object(native.managed_write,'apply',side_effect=applied),patch.object(native.managed_write,'connect',return_value=(self.coordinator,self.authority)),patch.object(native.managed_write,'abandon_lease',return_value={'retired_grants':[]}):
            for error in errors:
                with self.subTest(error=type(error).__name__),patch.object(native,'confirm',side_effect=error):
                    result=native.managed_apply(self.engine,'native-1',effect=self.effect,verify=self.verifier,**self.context)
                    self.assertEqual(result['native'],receipts[-1]);self.assertTrue(result['native']['complete'])
                    self.assertIsNone(result['delivery']);self.assertTrue(result['delivery_pending'])
                    self.assertEqual(result['delivery_error'],str(error))
                    self.assertEqual(self.engine.status(),[])
                    self.assertEqual(self.coordinator.inspect(self.context['attempt'])['reservations'],[])
        key=result['request_id']
        with patch.object(LocalRoot,'replace',side_effect=AssertionError('native replay forbidden')):
            self.assertTrue(self.confirm_native(key)['complete'])
        self.assertEqual(self.engine.drain_verified(),[key])
        self.assertEqual(self.engine.run(key)['state'],'published')

    def test_concurrent_confirmation_and_drain_keep_original_receipt(self):
        with self.assertRaises(SimulatedCrash):self.native_operation(fault=self.crash('before_native_verification'))
        with self.engine.db() as db:key=db.execute('SELECT id FROM intents').fetchone()[0]
        recover=native.managed_write.recover
        def concurrent(*args,**kwargs):
            receipt=recover(*args,**kwargs)
            self.assertTrue(self.confirm_native(key)['complete'])
            self.assertEqual(self.engine.drain_verified(),[key])
            return receipt
        with patch.object(native.managed_write,'recover',side_effect=concurrent),patch.object(LocalRoot,'replace',side_effect=AssertionError('native replay forbidden')):
            self.assertTrue(self.confirm_native(key)['complete'])
        self.assertEqual(self.engine.drain_verified(),[])
        self.assertEqual(len(self.engine.status()),1)

    def replacement_hook(self):
        import shlex
        helper = self.root / '.git' / 'replacement-post-commit.py'
        helper.write_text('''from pathlib import Path
import subprocess
def git(*args, body=None):
    return subprocess.run(['git', '--no-replace-objects', *args], input=body, capture_output=True, check=True).stdout
checked = git('rev-parse', 'HEAD').decode().strip()
tree = git('rev-parse', checked + '^{tree}').decode().strip()
parent = git('rev-parse', checked + '^').decode().strip()
message = git('show', '-s', '--format=%B', checked)
Path('secret.txt').write_bytes(b'UNCHECKED REAL HOOK OUTPUT\\n')
git('add', 'secret.txt')
actual_tree = git('write-tree').decode().strip()
actual = git('commit-tree', actual_tree, '-p', parent, body=message).decode().strip()
git('update-ref', 'HEAD', actual, checked)
replacement = git('commit-tree', tree, '-p', parent, body=message).decode().strip()
git('replace', actual, replacement)
''', encoding='utf-8')
        hook = self.root / '.git' / 'hooks' / 'post-commit'
        hook.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable.replace('\\', '/')) + ' ' +
                        shlex.quote(str(helper).replace('\\', '/')) + '\n', encoding='utf-8')
        hook.chmod(0o755)

    def test_replacement_hook_cannot_mask_unchecked_commit_or_recovery(self):
        key = self.code()
        self.replacement_hook()
        result = self.engine.run(key)
        self.assertEqual(result['state'], 'commit-uncertain', result['error'])
        self.assertIn('hook', result['error'])
        head = git(self.root, 'rev-parse', 'HEAD').stdout.decode().strip()
        self.assertNotEqual(git(self.root, 'show', head + ':secret.txt', check=False).returncode, 0)
        self.assertEqual(delivery.blob(self.root, head, 'secret.txt'), b'UNCHECKED REAL HOOK OUTPUT\n')
        index = delivery.git(self.root, 'rev-parse', '--git-path', 'index').stdout.decode().strip()
        index = self.root / index
        before = index.read_bytes()
        self.unchanged_remote()
        result = self.engine.run(key)
        self.assertEqual(result['state'], 'blocked', result['error'])
        self.assertIn('incompatible result', result['error'])
        self.assertEqual(index.read_bytes(), before)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD').stdout.decode().strip(), head)
        self.unchanged_remote()

    def test_replacement_hook_crash_recovers_raw_commit(self):
        key = self.code()
        self.replacement_hook()
        with self.assertRaises(SimulatedCrash):
            self.engine.run(key, fault=self.crash('after_commit'))
        result = self.engine.run(key)
        self.assertEqual(result['state'], 'commit-uncertain', result['error'])
        self.assertIn('incompatible result', result['error'])
        self.unchanged_remote()

    def test_replacement_cannot_hide_outgoing_unselected_ancestor(self):
        self.configure()
        (self.root / 'secret.txt').write_bytes(b'private historical bytes\n')
        git(self.root, 'add', 'secret.txt'); git(self.root, 'commit', '-m', 'unbound ancestor')
        hidden = git(self.root, 'rev-parse', 'HEAD').stdout.decode().strip()
        (self.root / 'secret.txt').unlink()
        git(self.root, 'add', 'secret.txt'); git(self.root, 'commit', '-m', 'reverted ancestor')
        # Ordinary Git sees an innocuous baseline in place of the secret commit.
        git(self.root, 'replace', hidden, self.base)
        git(self.root, 'config', 'core.useReplaceRefs', 'true')
        result = self.engine.run(self.code())
        self.assertEqual(result['state'], 'blocked', result['error'])
        self.assertIn('Unauthorized outgoing ancestor path: secret.txt', result['error'])
        self.assertEqual(git(self.root, 'diff', '--cached', '--name-only').stdout, b'')
        self.unchanged_remote()

    def test_graft_topology_refused_with_replace_config_disabled(self):
        key = self.code()
        graft = self.root / '.git' / 'info' / 'grafts'
        graft.write_text(self.base + '\n', encoding='utf-8')
        git(self.root, 'config', 'core.useReplaceRefs', 'false')
        for mode in ('commit+push', 'commit-only'):
            with self.subTest(mode=mode):
                if mode == 'commit-only': self.configure(mode); self.engine.resume(key)
                result = self.engine.run(key)
                self.assertEqual(result['state'], 'blocked', result['error'])
                self.assertIn('graft topology', result['error'])
                self.assertEqual(graft.read_text(encoding='utf-8'), self.base + '\n')
                self.assertEqual(git(self.root, 'rev-parse', 'HEAD').stdout.decode().strip(), self.base)
                self.assertEqual(git(self.root, 'diff', '--cached', '--name-only').stdout, b'')
                self.unchanged_remote()

    def test_linked_worktree_common_and_private_grafts_refused(self):
        original_root = self.root
        linked = self.home / 'linked'
        git(self.root, 'worktree', 'add', '-b', 'linked', str(linked), self.base)
        git(self.root, 'config', 'extensions.worktreeConfig', 'true')
        git(linked, 'config', '--worktree', 'core.useReplaceRefs', 'false')
        self.root = linked
        self.engine = delivery.Engine(linked)
        self.configure(branch='refs/heads/linked')
        key = self.code()
        private = Path(git(linked, 'rev-parse', '--absolute-git-dir').stdout.decode().strip()) / 'info/grafts'
        for graft in (original_root / '.git/info/grafts', private):
            with self.subTest(graft=str(graft)):
                graft.parent.mkdir(exist_ok=True)
                graft.write_text(self.base + '\n', encoding='utf-8')
                result = self.engine.run(key)
                self.assertEqual(result['state'], 'blocked', result['error'])
                self.assertIn('graft topology', result['error'])
                self.assertEqual(git(linked, 'rev-parse', 'HEAD').stdout.decode().strip(), self.base)
                self.assertEqual(git(linked, 'diff', '--cached', '--name-only').stdout, b'')
                self.unchanged_remote()
                graft.unlink()
        self.engine.resume(key)
        self.assertEqual(self.engine.run(key)['state'], 'published')

    def test_graft_appearing_after_checks_withholds_commit(self):
        key = self.code()
        def graft_after_checks(stage):
            if stage == 'after_checks':
                (self.root / '.git/info/grafts').write_text(self.base + '\n', encoding='utf-8')
        result = self.engine.run(key, fault=graft_after_checks)
        self.assertEqual(result['state'], 'blocked', result['error'])
        self.assertIn('graft topology', result['error'])
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD').stdout.decode().strip(), self.base)
        self.assertEqual(git(self.root, 'diff', '--cached', '--name-only').stdout, b'')
        self.unchanged_remote()

    def test_graft_appearing_at_final_push_withholds_publication(self):
        key = self.code()
        original = self.engine._save
        def late_graft(request, **values):
            original(request, **values)
            if values.get('state') == 'pushing':
                (self.root / '.git/info/grafts').write_text(self.base + '\n', encoding='utf-8')
        with patch.object(self.engine, '_save', side_effect=late_graft):
            result = self.engine.run(key)
        self.assertEqual(result['state'], 'push-uncertain', result['error'])
        self.assertIn('graft topology', result['error'])
        self.unchanged_remote()
        with patch.object(delivery, 'git', wraps=delivery.git) as observed:
            result = self.engine.run(key)
        self.assertEqual(result['state'], 'blocked', result['error'])
        self.assertFalse(any(c.args[1] in {'ls-remote', 'fetch', 'push'} for c in observed.call_args_list))
        self.unchanged_remote()

    def no_network_git(self, root, *args, **kwargs):
        self.assertNotIn(args[0], {'ls-remote', 'fetch', 'push'})
        return self.original_git(root, *args, **kwargs)

    def test_offline_commit_only_code_crash_retry_and_publish(self):
        offline = self.home / 'offline.git'
        # Old canonical pins must not require publication authority for already
        # integrated manual history during a local save.
        (self.root / 'docs/manual.md').write_bytes(b'old manually integrated history\n')
        git(self.root, 'add', 'docs/manual.md'); git(self.root, 'commit', '-m', 'manual baseline descendant')
        floor = git(self.root, 'rev-parse', 'HEAD').stdout.decode().strip()
        self.configure('commit-only', remote_url=str(offline))
        key = self.code()
        (self.root / 'docs/item.md').write_bytes(b'unrelated dirty bytes\n')
        self.original_git = delivery.git
        with patch.object(delivery, 'git', side_effect=self.no_network_git):
            with self.assertRaises(SimulatedCrash): self.engine.run(key, fault=self.crash('after_commit'))
            result = self.engine.run(key)
            self.assertEqual(result['state'], 'committed', result['error'])
            self.assertEqual(self.engine.run(key)['commit_oid'], result['commit_oid'])
        self.assertEqual((self.root / 'docs/item.md').read_bytes(), b'unrelated dirty bytes\n')
        self.assertEqual(git(self.root, 'rev-list', '--count', floor + '..HEAD').stdout.strip(), b'1')
        git(self.home, 'init', '--bare', str(offline))
        git(self.root, 'push', str(offline), floor + ':refs/heads/work')
        self.configure(remote_url=str(offline)); self.engine.resume(key)
        published = self.engine.run(key)
        self.assertEqual(published['state'], 'published', published['error'])
        self.assertEqual(published['commit_oid'], result['commit_oid'])
        self.assertEqual(git(offline, 'rev-parse', 'refs/heads/work').stdout.decode().strip(), result['commit_oid'])
        self.unchanged_remote()

    def test_offline_commit_only_records_retry_and_publish(self):
        offline = self.home / 'offline.git'
        self.configure('commit-only', remote_url=str(offline))
        self.original_git = delivery.git
        with patch.object(delivery, 'git', side_effect=self.no_network_git):
            first = self.native_operation()['request_id']
            with self.assertRaises(SimulatedCrash): self.engine.run(first, fault=self.crash('after_commit'))
            result = self.engine.run(first)
            self.assertEqual(result['state'], 'committed', result['error'])
            self.assertEqual(self.engine.run(first)['commit_oid'], result['commit_oid'])
            self.context.update(attempt='offline-native-2', effect_id='offline-effect-2')
            second = self.native_operation(body=b'{"profile": "fixture-native.v1", "status": "active"}\n', id='offline-record-2')['request_id']
            result = self.engine.run(second)
            self.assertEqual(result['state'], 'committed', result['error'])
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD').stdout.decode().strip(), self.base)
        git(self.home, 'init', '--bare', str(offline))
        self.configure(remote_url=str(offline)); self.engine.resume(second)
        result = self.engine.run(second)
        self.assertEqual(result['state'], 'published', result['error'])
        self.assertEqual(git(offline, 'rev-list', '--count', self.base + '..refs/heads/records/rick').stdout.strip(), b'2')
        self.unchanged_remote()

    def test_offline_saved_code_publication_refuses_unbound_ancestry(self):
        self.configure('commit-only')
        (self.root / 'docs/manual.md').write_bytes(b'unreviewed ancestor\n')
        git(self.root, 'add', 'docs/manual.md'); git(self.root, 'commit', '-m', 'unbound ancestor')
        self.original_git = delivery.git
        with patch.object(delivery, 'git', side_effect=self.no_network_git):
            key = self.code(); saved = self.engine.run(key)
        self.assertEqual(saved['state'], 'committed', saved['error'])
        self.configure(); self.engine.resume(key)
        result = self.engine.run(key)
        self.assertEqual(result['state'], 'blocked', result['error'])
        self.assertIn('Unbound outgoing ancestor', result['error'])
        self.assertEqual(result['commit_oid'], saved['commit_oid'])
        self.unchanged_remote()

    def test_offline_saved_code_publication_refuses_private_ancestry(self):
        path, record = self.ram_record()
        record['controls']['visibility'] = 'private'
        (self.root / path).write_bytes(ts.encoded(record))
        git(self.root, 'add', path); git(self.root, 'commit', '-m', 'private ancestor')
        self.configure('commit-only')
        self.original_git = delivery.git
        with patch.object(delivery, 'git', side_effect=self.no_network_git):
            key = self.code(); saved = self.engine.run(key)
        self.assertEqual(saved['state'], 'committed', saved['error'])
        self.configure(); self.engine.resume(key)
        result = self.engine.run(key)
        self.assertEqual(result['state'], 'blocked', result['error'])
        self.assertIn('Native RAM is ineligible', result['error'])
        self.unchanged_remote()

    def test_offline_saved_code_publication_rechecks_remote_divergence(self):
        self.configure('commit-only'); key = self.code()
        self.original_git = delivery.git
        with patch.object(delivery, 'git', side_effect=self.no_network_git): saved = self.engine.run(key)
        self.assertEqual(saved['state'], 'committed', saved['error'])
        (self.peer / 'docs/peer.md').write_bytes(b'divergent destination\n')
        git(self.peer, 'add', 'docs/peer.md'); git(self.peer, 'commit', '-m', 'peer divergence')
        git(self.peer, 'push', 'origin', 'HEAD:refs/heads/work')
        remote = git(self.remote, 'rev-parse', 'refs/heads/work').stdout
        self.configure(); self.engine.resume(key)
        result = self.engine.run(key)
        self.assertEqual(result['state'], 'blocked', result['error'])
        self.assertIn('non-fast-forward', result['error'])
        self.assertEqual(result['commit_oid'], saved['commit_oid'])
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/work').stdout, remote)
