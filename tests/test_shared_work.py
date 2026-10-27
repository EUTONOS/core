"""ASW5 native writer/publisher proof uses two disposable clones and a local bare remote."""
from contextlib import redirect_stdout
from copy import deepcopy
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import urlopen
import uuid

from eutonos import boards, boards_cli, current_work, dashboard_cli, git_delivery, host
from eutonos import ram, closeout_route, work_closeout, work_lifecycle, work_attribution
from tests import test_current_work_knowledge as knowledge_fixture
from eutonos import native_owner, shared_work, shared_attempts, work_precheck, write_batch, write_retry
from eutonos.effects import digest
from eutonos import tokenslang as ts


class SharedWorkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='eutonos-asw5-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        env = patch.dict(os.environ, {'EUTONOS_HOME': str(self.home / 'device')})
        env.start(); self.addCleanup(env.stop)
        self.remote = self.home / 'remote.git'
        seed = self.home / 'seed'; seed.mkdir(); (seed / 'docs').mkdir()
        self.identity = {'repository_id': 'fixture.repo', 'scope_id': 'fixture.scope', 'namespace': 'fixture.native'}
        (seed / 'eutonos.read.json').write_text(json.dumps({'profile': ts.CATALOG, **self.identity, 'owners': {}, 'excluded_roots': [],
            'source_paths': ['docs/guide.md'], 'bindings': [{'resource_id': 'guide', 'path': 'docs/guide.md', 'kind': 'docs', 'label': 'Guide', 'visibility': 'public', 'state': 'active'}]}))
        (seed / 'WORK.md').write_text('# Work\n')
        (seed / 'docs/guide.md').write_text('# Guide\nBaseline guide.\n')
        (seed / 'RAM').mkdir()
        (seed / 'eutonos.ram.json').write_text(json.dumps({'profile': ram.CONFIG, **self.identity, 'store_root': 'RAM', 'current_task': 'WORK.md', 'work_owners': {'work': 'WORK.md'}}))
        (seed / '.gitignore').write_text('.eutonos-local/\neutonos.shared-work.json\n')
        (seed / 'eutonos.boards.json').write_text(json.dumps({'profile': boards.CONFIG, 'owners': ['docs/attempts.md']}))
        (seed / 'docs/attempts.md').write_text('# Attempts\n\n```tokenslang-board\n' + json.dumps({'profile': boards.BOARD, 'items': []}) + '\n```\n')
        self.git(seed, 'init', '-b', 'main'); self.git(seed, 'add', '.'); self.git(seed, 'commit', '-m', 'native baseline')
        self.base = self.git(seed, 'rev-parse', 'HEAD')
        self.git(self.home, 'init', '--bare', str(self.remote)); self.git(seed, 'push', str(self.remote), 'HEAD:refs/heads/main')
        self.a, self.b = self.home / 'alice', self.home / 'bob'
        for root, name in ((self.a, 'alice'), (self.b, 'bob')):
            self.git(self.home, 'clone', '-b', 'main', str(self.remote), str(root))
            self.git(root, 'checkout', '-b', name)
            self.git(root, 'push', str(self.remote), 'HEAD:refs/heads/' + name)
            self.assertEqual(self.git(root, 'status', '--porcelain'), '')
            self.assertEqual(self.git(root, 'diff', '--cached', '--name-only'), '')
            admission = self.home / (name + '-host')
            host.enroll(root, admission, name, [], shared=True)
        self.engines = {}

    def git(self, root, *args):
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0')
        result = subprocess.run(['git', '-c', 'user.name=ASW5 Fixture', '-c', 'user.email=asw5@example.invalid',
                                 '-c', 'core.autocrlf=false', '-C', str(root), *args], capture_output=True, env=env, check=True)
        return result.stdout.decode().strip()

    def configure(self, root, *, enabled=True, peers=None, canonical=None):
        name = root.name
        value = {'profile': shared_work.PROFILE, **self.identity, 'enabled': enabled, 'participant': name,
                 'canonical': canonical or {'url': str(self.remote), 'ref': 'refs/heads/main'},
                 'peers': peers if peers is not None else [{'id': 'peer', 'url': str(self.remote), 'ref': 'refs/heads/records/' + ('bob' if name == 'alice' else 'alice')}],
                 'attempt_owner': 'docs/attempts.md', 'fetch_interval_seconds': 5}
        return shared_work.configure(root, value, shared_work.load(root)['revision'])

    def publisher(self, root, mode='commit+push'):
        name = root.name
        # All destinations and bindings are the disposable fixture, never this checkout.
        engine = git_delivery.Engine(root)
        engine.configure({'profile': git_delivery.CONFIG, 'project_id': self.identity['repository_id'], 'participant': name,
                          'mode': mode, 'remote_url': str(self.remote), 'remote_ref': 'refs/heads/' + name,
                          'records_ref': 'refs/heads/records/' + name, 'branch': 'refs/heads/' + name,
                          'canonical_base': self.base, 'allowed_paths': ['docs/', 'RAM/', 'eutonos.boards.json', 'eutonos.work.json'],
                          'checks': [[sys.executable, '-c', 'pass']]}, expected_revision=engine.config()['revision'])
        self.engines[name] = engine
        return engine

    def cli(self, main, args):
        output = io.StringIO()
        with redirect_stdout(output): status = main(args)
        result = json.loads(output.getvalue())
        self.assertEqual(status, 0, result)
        return result

    def create(self, root, title='Shared campaign', identity=None):
        name = root.name; token = str(uuid.uuid4()); path = 'docs/' + name + '-' + token + '.md'
        form = self.cli(boards_cli.main, ['owner', 'form', '--root', str(root), '--path', path, '--title', title])
        draft = {'title': title, 'summary': 'Peer-only canonical work, saved by native writer.', 'status': 'active'}
        if identity is not None: draft['id'] = identity
        request_path = self.home / 'draft.json'; request_path.write_text(json.dumps(draft))
        record = self.cli(boards_cli.main, ['form', '--type', 'campaign', '--root', str(root), '--request', str(request_path)])
        item = record['batch_operation']['request']['items'][0]
        add = {'kind': 'add', 'request': {'owner_path': path, 'expected_sha256': form['owner_sha256'], 'index': 0, 'items': [item]}}
        batch = {'profile': write_batch.PROFILE, 'operation_id': 'new-campaign-' + token, 'operations': [form['batch_operation'], add]}
        request_path = self.home / ('create-' + token + '.json'); request_path.write_text(json.dumps(batch))
        result = self.cli(boards_cli.main, ['batch', 'submit', '--root', str(root), '--request', str(request_path), '--host', str(self.home / (name + '-host')), '--actor', name])
        self.assertEqual(result['outcome'], 'complete', result)
        self.assertEqual(result['result']['git_delivery']['state'], 'published', result)
        self.assertEqual(self.git(root, 'diff', '--cached', '--name-only'), '')
        registry = json.loads((root / 'docs/REGISTRY.json').read_bytes())['documents']
        for key in ('id', 'path', 'role'): self.assertEqual(len({row[key] for row in registry}), len(registry))
        self.assertEqual(self.git(self.remote, 'rev-parse', 'refs/heads/records/' + name), result['result']['git_delivery']['commit_oid'])
        return item['id'], batch, result

    def observe(self, root, *, knowledge=False):
        # Simulate the configured cadence elapsing without sleeping in tests.
        common = Path(self.git(root, 'rev-parse', '--git-common-dir'))
        if not common.is_absolute(): common = root / common
        for path in (common / 'eutonos-shared-work').glob('*.json'):
            value = json.loads(path.read_bytes()); value['attempted_at'] = 0; path.write_text(json.dumps(value))
        return current_work.project(root, refresh=True, include_knowledge=knowledge)

    def test_two_clone_native_create_publish_observe_claim_reverse_and_finish(self):
        self.configure(self.a); self.configure(self.b); self.publisher(self.a); self.publisher(self.b)
        canonical = self.git(self.remote, 'rev-parse', 'refs/heads/main')
        identity, batch, created = self.create(self.a)
        peer = self.observe(self.b)
        row = next(row for row in peer['boards']['items'] if row['id'] == identity)
        self.assertFalse(row['record_on_main'])
        self.assertEqual(row['publication'], 'cached-local-ref')
        self.assertFalse(any(row['id'] == identity for row in boards.project(self.b)['items']))
        self.assertEqual(self.git(self.b, 'rev-parse', 'HEAD'), self.base)
        claimed = self.cli(work_precheck.main, [identity, '--root', str(self.b), '--claim', '--chat', 'bob-real-session',
            '--host', str(self.home / 'bob-host'), '--actor', 'bob', '--intent', 'Implement peer work'])
        action = claimed['claim_action']; self.assertEqual(action['native_save'], 'complete', action)
        self.assertEqual(action['write_result']['result']['git_delivery']['state'], 'published', action)
        attempt = action['attempt_id']; self.assertEqual(len(attempt), 36)
        retry = self.cli(work_precheck.main, [identity, '--root', str(self.b), '--claim', '--chat', 'bob-real-session',
            '--host', str(self.home / 'bob-host'), '--actor', 'bob', '--intent', 'Implement peer work'])
        self.assertEqual(retry['claim_action']['attempt_id'], attempt)
        self.observe(self.a)
        precheck = self.cli(work_precheck.main, [identity, '--root', str(self.a), '--chat', 'alice-real-session'])
        self.assertEqual(precheck['verdict'], 'overlap', precheck)
        server = ThreadingHTTPServer(('127.0.0.1', 0), dashboard_cli.app([self.a]))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with urlopen('http://127.0.0.1:' + str(server.server_port) + '/api/v1/current-work?project=0') as response:
                dashboard = json.load(response)
            shown = dashboard['modules']['current_work']['attempts']['items']
            self.assertEqual(shown[0]['attempt_id'], attempt)
            self.assertEqual(shown[0]['target']['work_id'], identity)
        finally: server.shutdown(); server.server_close(); thread.join(2)
        parallel = shared_attempts.start(self.a, identity, 'alice-real-session', self.home / 'alice-host', 'alice', intentional_parallel=True)
        self.assertEqual(parallel['native_save'], 'complete', parallel)
        self.assertEqual(self.observe(self.b)['attempts']['overlaps'][0]['intentional_parallel'], True)
        finish = self.cli(work_precheck.main, [identity, '--root', str(self.b), '--finish', '--chat', 'bob-real-session', '--host', str(self.home / 'bob-host'), '--actor', 'bob'])
        self.assertEqual(finish['claim_action']['native_save'], 'complete', finish)
        self.assertEqual(next(row for row in self.observe(self.a)['boards']['items'] if row['id'] == identity)['status'], 'active')
        reverse, _, _ = self.create(self.b)
        self.assertIn(reverse, {row['id'] for row in self.observe(self.a)['boards']['items']})
        reverse_attempt = shared_attempts.start(self.a, reverse, 'alice-reverse-session', self.home / 'alice-host', 'alice')
        self.assertEqual(reverse_attempt['native_save'], 'complete', reverse_attempt)
        self.assertEqual(reverse_attempt['git_delivery']['state'], 'published', reverse_attempt)
        self.assertIn(reverse_attempt['attempt_id'], {row['attempt_id'] for row in self.observe(self.b)['attempts']['items']})
        same_looking, _, _ = self.create(self.a)
        self.assertNotEqual(identity, same_looking)
        for record in boards.project(self.a)['items']:
            if record['type'] == 'campaign': self.assertEqual(len(record['extensions']['creation_token']), 36)
        self.assertEqual(self.git(self.remote, 'rev-parse', 'refs/heads/main'), canonical)
        print('ASW5_NATIVE_PROOF ' + json.dumps({'created': identity, 'reverse': reverse, 'distinct': same_looking, 'attempt': attempt,
            'target_commit': row['provenance'][0]['commit'], 'canonical_commit': canonical, 'dashboard_http': 200, 'native_writer': 'write_retry.submit_batch', 'publisher': 'git_delivery'}))

    def native_work(self, root, identity=None, operation='update', **fields):
        request = {'operation': operation, **fields}
        if identity is not None:
            row = next(r for r in boards.project(root)['items'] if r['id'] == identity)
            request.update(item_id=identity, expected_revision=row['revision'])
        result = write_retry.submit_work(self.home / (root.name + '-host'), root, root.name, request)
        self.assertEqual(result['outcome'], 'complete', result)
        return result

    def test_two_clone_docs_ram_closeout_selected_merge_and_squash_truth(self):
        for root, given in ((self.a, 'Alice'), (self.b, 'Bob')):
            work_attribution.configure_profile(root, {'given_name': given, 'family_name': 'Example'}, None)
            self.configure(root)
            self.publisher(root)
        identity, _, _ = self.create(self.a)
        work_lifecycle.folders(self.a)
        # EHF4R_RAM.md: configured RAM parent directories are explicit host setup.
        (self.a / 'RAM').mkdir(exist_ok=True)
        self.native_work(self.a, operation='initialize')
        self.native_work(self.a, identity, changes={'summary': 'Docs and source round trip.'},
                         progress={'percent': 50, 'basis': 'Qualification in progress.'},
                         what='Updated native checkpoint.', why='Requested qualification.',
                         result='Ready for bounded closeout.', checks=['Fixture checkpoint inspected.'])
        row = next(r for r in boards.project(self.a)['items'] if r['id'] == identity)
        owner = row['owner_path']
        seen = next(r for r in self.observe(self.b)['boards']['items'] if r['id'] == identity)
        self.assertEqual(seen['extensions']['progress']['percent'], 50)
        self.assertFalse(seen['record_on_main'])
        work_attribution.configure_profile(self.a, {'given_name': 'Alicia', 'family_name': 'Example'},
                                           work_attribution.profile_status(self.a)['revision'])
        (self.a / 'docs/guide.md').write_text('# Guide\nSelected adoption guide.\n')
        (self.a / 'docs/product.py').write_text('qualified = True\n')
        (self.a / 'private.txt').write_text('unrelated working bytes')
        (self.a / 'staged.txt').write_text('unrelated staged bytes')
        self.git(self.a, 'add', 'staged.txt')
        context = {'root': str(self.a), 'host': str(self.home / 'alice-host'), 'actor': 'alice',
                   'work_id': identity, 'session_id': 'alice-closeout', 'app': 'Codex', 'model': 'fixture-model',
                   'delivery': {'logical_id': 'selected-roundtrip', 'paths': ['docs/guide.md', 'docs/product.py']}}
        answer = work_closeout.lifecycle_assessment({}, context)['example']['work_closeout'][0]
        answer.update(status='completed', acceptance_confirmed=True, defer_owner_move=True,
                      what='Completed selected fixture scope.', why='Qualify two developer adoption.',
                      result='Guide and source checked; no stage changed.', checks=['Configured candidate check.'],
                      progress={'percent': 100, 'basis': 'Bounded fixture complete.'})
        values = {'catalog_changes': [], 'work_changes': [], 'work_closeout': [answer],
                  'changed_paths': ['docs/guide.md', 'docs/product.py']}
        prepared = closeout_route.prepare(values, context)
        self.assertEqual(prepared['outcome'], 'prepared', prepared)
        closed = closeout_route.run(prepared, context)
        self.assertEqual(closed['outcome'], 'completed', {k: closed.get(k) for k in ('outcome', 'memory', 'git_delivery')})
        self.assertEqual(closed['git_delivery']['state'], 'blocked')
        self.assertIn('Pre-existing index', closed['git_delivery']['error'])
        self.assertEqual(closed['memory']['outcome'], 'completed', closed['memory'].get('message'))
        self.assertEqual(self.git(self.a, 'diff', '--cached', '--name-only'), 'staged.txt')
        self.assertEqual((self.a / 'private.txt').read_text(), 'unrelated working bytes')
        self.assertEqual((self.a / 'staged.txt').read_text(), 'unrelated staged bytes')
        # The configured publisher deliberately refuses an occupied index.
        # Explicit fixture recovery resolves only its own staged entry and reuses
        # the frozen code request; native closeout/RAM are not replayed.
        self.git(self.a, 'restore', '--staged', '--', 'staged.txt')
        engine = self.engines['alice']
        key = closed['git_delivery']['request_id']
        engine.resume(key)
        delivered = engine.run(key)
        self.assertEqual(delivered['state'], 'published', delivered.get('error'))
        self.assertEqual((self.a / 'staged.txt').read_text(), 'unrelated staged bytes')
        completed = next(r for r in boards.project(self.a)['items'] if r['id'] == identity)
        self.assertEqual(completed['attribution']['creator']['human']['full_name'], 'Alice Example')
        self.assertEqual(completed['attribution']['completion']['human']['full_name'], 'Alicia Example')
        self.assertEqual(len(completed['attribution']['contributors']), 1)
        records = self.git(self.remote, 'rev-parse', 'refs/heads/records/alice')
        code = self.git(self.remote, 'rev-parse', 'refs/heads/alice')
        self.assertNotEqual(records, code)
        memory_paths = [p.relative_to(self.a).as_posix() for p in (self.a / 'RAM').rglob('*.json')]
        self.assertEqual(len(memory_paths), 1)
        memory_path = memory_paths[0]
        memory_bytes = (self.a / memory_path).read_bytes()
        links = json.loads(ram.BLOCK.search((self.a / owner).read_bytes())[1])
        self.assertIn(memory_path, next(r['memories'] for r in links['links'] if r['work_id'] == identity))
        before = self.observe(self.b, knowledge=True)
        self.assertIn(memory_path, {r['path'] for r in before['ram']['records']}, before['diagnostics'])
        self.assertEqual(self.git(self.b, 'rev-parse', 'HEAD'), self.base)
        self.git(self.b, 'fetch', str(self.remote), 'refs/heads/records/alice:refs/remotes/fixture/records',
                 'refs/heads/alice:refs/remotes/fixture/code')
        for mode in ('ordinary', 'squash'):
            self.git(self.b, 'checkout', '-b', 'integration-' + mode, self.base)
            self.git(self.b, 'merge', '--no-ff', 'refs/remotes/fixture/records', '-m', 'selected records')
            target_ref = 'refs/heads/integration-' + mode
            target = self.git(self.b, 'rev-parse', 'HEAD')
            proof = {'source_commit': code, 'target_ref': target_ref, 'target_commit': target}
            with self.assertRaises(ts.ReadError) as announcement:
                work_attribution.integration_proof(self.b, proof)
            self.assertEqual(announcement.exception.code, 'unverified')
            self.assertFalse((self.b / 'docs/product.py').exists())
            if mode == 'ordinary':
                self.git(self.b, 'merge', '--no-ff', 'refs/remotes/fixture/code', '-m', 'selected checked source')
            else:
                self.git(self.b, 'merge', '--squash', 'refs/remotes/fixture/code')
                self.git(self.b, 'commit', '-m', 'selected checked source squash')
            proof['target_commit'] = self.git(self.b, 'rev-parse', 'HEAD')
            integrated = next(r for r in boards.project(self.b)['items'] if r['id'] == identity)
            self.assertEqual(integrated['extensions']['attribution'], completed['extensions']['attribution'])
            self.assertEqual(integrated['extensions']['work_history'], completed['extensions']['work_history'])
            self.assertEqual((self.b / memory_path).read_bytes(), memory_bytes)
            self.assertIn(memory_path, (self.b / owner).read_text())
            self.assertEqual((self.b / 'docs/guide.md').read_text(), '# Guide\nSelected adoption guide.\n')
            self.assertEqual((self.b / 'docs/product.py').read_text(), 'qualified = True\n')
            self.assertFalse((self.b / 'staged.txt').exists())
            if mode == 'ordinary':
                self.assertTrue(work_attribution.integration_proof(self.b, proof)['verified'])
            else:
                with self.assertRaises(ts.ReadError) as squash:
                    work_attribution.integration_proof(self.b, proof)
                self.assertEqual(squash.exception.code, 'unverified')
            print('ASW7_MERGE_PROOF ' + json.dumps({'mode': mode, 'work_id': identity, 'memory_path': memory_path,
                'record_commit': records, 'code_commit': code, 'target_commit': proof['target_commit'],
                'integration': 'raw-git-ancestry' if mode == 'ordinary' else 'unverified-squash'}))
        # Publish the selected ordinary integration to the fixture's accepted main.
        self.git(self.b, 'checkout', 'integration-ordinary')
        config = {k: v for k, v in self.engines['bob'].config().items() if k not in {'root', 'revision', 'activation_cursor'}}
        config['mode'] = 'manual'
        self.engines['bob'].configure(config, expected_revision=self.engines['bob'].config()['revision'])
        proof = {'source_commit': code, 'target_ref': 'refs/heads/integration-ordinary',
                 'target_commit': self.git(self.b, 'rev-parse', 'HEAD')}
        self.native_work(self.b, identity, operation='integrate', integration=proof,
                         evidence=['Verified selected code raw ancestry in ordinary target.'],
                         caller_context={'app': 'Codex', 'session': 'bob-integration'})
        signed = next(r for r in boards.project(self.b)['items'] if r['id'] == identity)
        self.assertTrue(signed['attribution']['integrations'][-1]['verified'])
        self.assertEqual(signed['attribution']['integrations'][-1]['human']['full_name'], 'Bob Example')
        self.git(self.b, 'add', 'docs', 'eutonos.work.json')
        self.git(self.b, 'commit', '-m', 'record verified integration attribution')
        accepted = self.git(self.b, 'rev-parse', 'HEAD')
        self.git(self.b, 'push', str(self.remote), 'HEAD:refs/heads/main')
        self.assertEqual(self.git(self.remote, 'rev-parse', 'refs/heads/main'), accepted)
        for root in (self.a, self.b):
            observed = self.observe(root, knowledge=True)
            self.assertEqual(observed['shared_work']['canonical']['commit'], accepted)
            accepted_row = next(r for r in observed['boards']['items'] if r['id'] == identity)
            self.assertTrue(accepted_row['record_on_main'])
            self.assertIn(memory_path, {r['path'] for r in observed['ram']['records']})
            self.assertIn(owner, {r['path'] for r in observed['documents']['items']})
        print('ASW7_MAIN_PROOF ' + json.dumps({'work_id': identity, 'accepted_commit': accepted,
            'memory_path': memory_path, 'integration_actor': 'Bob Example', 'canonical_readback': 'both-clones'}))

    def test_same_owner_concurrent_edits_reconcile_through_native_writer(self):
        work_attribution.configure_profile(self.a, {'given_name': 'Alice', 'family_name': 'Example'}, None)
        self.configure(self.a); self.publisher(self.a)
        identity, _, _ = self.create(self.a)
        work_lifecycle.folders(self.a)
        # EHF4R_RAM.md: configured RAM parent directories are explicit host setup.
        (self.a / 'RAM').mkdir(exist_ok=True)
        self.native_work(self.a, operation='initialize')
        # Establish contributor and review history before the two copies diverge.
        row = next(r for r in boards.project(self.a)['items'] if r['id'] == identity)
        owner = row['owner_path']
        self.native_work(self.a, identity, operation='review', reviewed_revision=work_attribution.content_revision(row),
                         decision='KEEP', evidence=['Reviewed baseline content.'], source_commit=self.base)
        baseline = self.git(self.remote, 'rev-parse', 'refs/heads/records/alice')
        roots = []
        for name, given in (('left', 'Alice'), ('right', 'Bob')):
            root = self.home / name
            self.git(self.home, 'clone', '-b', 'records/alice', str(self.remote), str(root))
            self.git(root, 'checkout', '-b', name)
            host.enroll(root, self.home / (name + '-host'), name, [], shared=True)
            work_attribution.configure_profile(root, {'given_name': given, 'family_name': 'Example'}, None)
            roots.append(root)
        left, right = roots
        original = next(r for r in boards.project(left)['items'] if r['id'] == identity)
        original_extensions = deepcopy(original['extensions'])
        # Concurrent adjacent fields in the same row force a textual conflict;
        # neither semantic update rewrites original contributor history.
        self.native_work(left, identity, changes={'summary': 'Alice adds source qualification.'})
        self.native_work(right, identity, changes={'title': 'Bob adds adoption qualification'})
        for root in roots:
            peer_row = next(r for r in boards.project(root)['items'] if r['id'] == identity)
            self.assertEqual(peer_row['extensions']['attribution'], original_extensions['attribution'])
            self.assertEqual(peer_row['extensions'].get('work_history'), original_extensions.get('work_history'))
            self.git(root, 'add', 'docs', 'eutonos.work.json')
            self.git(root, 'commit', '-m', root.name + ' native checkpoint')
            self.git(root, 'push', str(self.remote), 'HEAD:refs/heads/' + root.name)
        self.git(left, 'fetch', str(self.remote), 'refs/heads/right:refs/remotes/fixture/right')
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
        merge = subprocess.run(['git', '-c', 'user.name=ASW7 Fixture', '-c', 'user.email=asw7@example.invalid', '-C', str(left), 'merge', '--no-commit', 'refs/remotes/fixture/right'],
                               capture_output=True, env=env)
        self.assertEqual(merge.returncode, 1, (merge.stdout, merge.stderr))
        self.assertIn(owner, self.git(left, 'diff', '--name-only', '--diff-filter=U').splitlines())
        conflict_blobs = [self.git(left, 'show', ':' + str(stage) + ':' + owner) for stage in (1, 2, 3)]
        self.assertIn('Alice adds source qualification.', conflict_blobs[1])
        self.assertIn('Bob adds adoption qualification', conflict_blobs[2])
        # Abort only this disposable fixture's merge; retain both pinned branches.
        self.git(left, 'merge', '--abort')
        (left / 'private.txt').write_text('unrelated concurrent data')
        replay = self.native_work(left, identity, changes={'title': 'Bob adds adoption qualification'})
        self.assertEqual(replay['result']['git_delivery']['state'], 'manual')
        reconciled = next(r for r in boards.project(left)['items'] if r['id'] == identity)
        self.assertEqual(reconciled['title'], 'Bob adds adoption qualification')
        self.assertEqual(reconciled['summary'], 'Alice adds source qualification.')
        self.assertEqual(reconciled['extensions']['attribution'], original_extensions['attribution'])
        self.assertEqual(reconciled['attribution']['creator'], original['attribution']['creator'])
        self.assertFalse(reconciled['attribution']['reviews'][0]['current'])
        self.assertEqual((left / 'private.txt').read_text(), 'unrelated concurrent data')
        # Freeze the exact reviewed native reconciliation, then retain both parents.
        self.git(left, 'add', 'docs', 'eutonos.work.json')
        self.git(left, 'commit', '-m', 'native semantic reconciliation')
        merge = subprocess.run(['git', '-c', 'user.name=ASW7 Fixture', '-c', 'user.email=asw7@example.invalid', '-C', str(left), 'merge', '--no-ff', '--no-commit', 'refs/remotes/fixture/right'], capture_output=True, env=env)
        self.assertIn(merge.returncode, (0, 1))
        unmerged = self.git(left, 'diff', '--name-only', '--diff-filter=U').splitlines()
        self.assertTrue(all(path.startswith('docs/') for path in unmerged), unmerged)
        if unmerged:
            self.git(left, 'checkout', '--ours', '--', *unmerged)
            self.git(left, 'add', '--', *unmerged)
        self.git(left, 'commit', '-m', 'retain reconciled peer ancestry')
        merged = next(r for r in boards.project(left)['items'] if r['id'] == identity)
        self.assertEqual(merged['extensions'], reconciled['extensions'])
        self.assertEqual(self.git(left, 'merge-base', baseline, 'HEAD'), baseline)
        print('ASW7_CONFLICT_PROOF ' + json.dumps({'owner': owner, 'work_id': identity,
            'baseline': baseline, 'reconciliation': self.git(left, 'rev-parse', 'HEAD'),
            'route': 'abort fixture merge, native semantic replay, commit, ordinary merge'}))

    def test_disable_after_populated_cache_hides_peer_without_fetch_or_publisher_change(self):
        self.configure(self.a); self.configure(self.b); self.publisher(self.a)
        identity, _, _ = self.create(self.a)
        first = self.observe(self.b, knowledge=True)
        owner = next(r['owner_path'] for r in first['boards']['items'] if r['id'] == identity)
        engine = self.publisher(self.b, 'commit-only')
        config = deepcopy(engine.config())
        self.configure(self.b, enabled=False)
        original_git = shared_work._git
        def no_fetch(root, *args, **kwargs):
            self.assertNotIn('fetch', args)
            return original_git(root, *args, **kwargs)
        with patch('eutonos.shared_work._git', side_effect=no_fetch):
            after = current_work.project(self.b, include_knowledge=True, refresh=True)
        self.assertEqual(after['shared_work']['fetch_state'], 'disabled')
        self.assertNotIn(identity, {r['id'] for r in after['boards']['items']})
        self.assertNotIn(owner, {r['path'] for r in after['documents']['items']})
        self.assertEqual(engine.config(), config)
        self.configure(self.b, enabled=True)
        self.assertIn(identity, {r['id'] for r in self.observe(self.b)['boards']['items']})
        self.assertEqual(engine.config(), config)

    def test_portable_manifest_never_enables_a_clone_and_credentials_are_refused(self):
        self.configure(self.a)
        (self.b / shared_work.PATH).write_bytes((self.a / shared_work.PATH).read_bytes())
        cfg = shared_work.load(self.b); self.assertFalse(cfg['enabled']); self.assertIsNone(cfg['participant'])
        with patch('eutonos.shared_work._git', side_effect=AssertionError('no fetch')):
            self.assertEqual(shared_work.observe(self.b)['fetch_state'], 'disabled')
        for url in ('https://name:secret@example.invalid/repo', 'https://secret@example.invalid/repo', 'ext::run command', 'https://example.invalid/repo?token=secret'):
            with self.subTest(url=url), self.assertRaises(ValueError): shared_work.transport(url)

    def test_source_withdrawal_filters_populated_board_and_knowledge_caches(self):
        self.configure(self.a); self.configure(self.b); self.publisher(self.a)
        identity, _, _ = self.create(self.a)
        peer_document = next(row['owner_path'] for row in boards.project(self.a)['items'] if row['id'] == identity)
        # This additional knowledge fixture publishes a fully eligible RAM candidate;
        # the native campaign oracle above independently proves the production writer.
        memory_clone = self.home / 'memory-records'
        self.git(self.home, 'clone', '-b', 'records/alice', str(self.remote), str(memory_clone))
        helper = knowledge_fixture.KnowledgeTests(); helper.root = memory_clone; helper.base = self.base
        memory_path = helper.record('Peer-only working memory')
        self.git(memory_clone, 'add', memory_path)
        self.git(memory_clone, 'commit', '-m', 'eligible peer RAM fixture')
        self.git(memory_clone, 'push', str(self.remote), 'HEAD:refs/heads/records/alice')
        first = self.observe(self.b, knowledge=True)
        self.assertIn(peer_document, {row['path'] for row in first['documents']['items']})
        self.assertIn(memory_path, {row['path'] for row in first['ram']['records']}, first['diagnostics'])
        self.assertIn(identity, {row['id'] for row in first['boards']['items']})
        old = first['shared_work']['revision']
        self.configure(self.b, peers=[])
        after = current_work.project(self.b, include_knowledge=True)
        self.assertNotEqual(after['shared_work']['revision'], old)
        self.assertNotIn(identity, {row['id'] for row in after['boards']['items']})
        self.assertFalse(any('/peer/' in row['ref'] for row in after['sources']))
        self.assertNotIn(peer_document, {row['path'] for row in after['documents']['items']})
        self.assertNotIn(memory_path, {row['path'] for row in after['ram']['records']})
        with patch('eutonos.repository_state.project', side_effect=ValueError('scan down')):
            fallback = current_work.project(self.b, include_knowledge=True, refresh=True)
        self.assertNotIn(identity, {row['id'] for row in fallback['boards']['items']})
        self.assertNotIn(peer_document, {row['path'] for row in fallback['documents']['items']})
        self.assertNotIn(memory_path, {row['path'] for row in fallback['ram']['records']})

    def test_missing_canonical_has_unknown_coverage_and_no_fork_main_fallback(self):
        self.configure(self.b, canonical={'url': str(self.remote), 'ref': 'refs/heads/missing'})
        observed = self.observe(self.b)
        self.assertEqual(observed['shared_work']['coverage'], 'unknown')
        self.assertFalse(any(row['is_main'] for row in observed['sources']))
        self.assertEqual(observed['boards']['items'], [])

    def test_owner_form_stale_registry_and_uuid_creation_are_guarded(self):
        self.configure(self.a)
        form = native_owner.form(self.a, 'docs/new.md', 'New board')
        (self.a / 'docs/REGISTRY.json').write_text('{"documents":[]}')
        with self.assertRaises(ts.ReadError): native_owner.plan(self.a, form['batch_operation']['request'])
        first = boards.work_form('campaign', {'title': 'Same', 'summary': 'Same', 'status': 'active'}, self.a)
        second = boards.work_form('campaign', {'title': 'Same', 'summary': 'Same', 'status': 'active'}, self.a)
        self.assertNotEqual(first['item']['id'], second['item']['id'])
        self.assertEqual(first['item']['id'], 'campaign-' + first['item']['extensions']['creation_token'])


    def test_source_rebind_offline_does_not_read_old_owned_ref_or_change_delivery_base(self):
        self.configure(self.a); self.configure(self.b); self.publisher(self.a)
        identity, _, _ = self.create(self.a)
        populated = self.observe(self.b)
        self.assertIn(identity, {r['id'] for r in populated['boards']['items']})
        engine = self.publisher(self.b, 'commit-only'); before = deepcopy(engine.config())
        self.configure(self.b, peers=[{'id': 'peer', 'url': str(self.home / 'unavailable.git'), 'ref': 'refs/heads/records/alice'}])
        changed = self.observe(self.b)
        self.assertEqual(changed['shared_work']['coverage'], 'unknown')
        self.assertIsNone(changed['shared_work']['peers'][0]['commit'])
        self.assertNotIn(identity, {r['id'] for r in changed['boards']['items']})
        self.assertEqual(engine.config(), before)
        self.assertNotEqual(populated['shared_work']['peers'][0]['owned_ref'], changed['shared_work']['peers'][0]['owned_ref'])
        self.configure(self.b, canonical={'url': str(self.home / 'unavailable.git'), 'ref': 'refs/heads/main'}, peers=[])
        rebound = self.observe(self.b)
        self.assertIsNone(rebound['shared_work']['canonical']['commit'])
        self.assertFalse(any(r['is_main'] for r in rebound['sources']))

    def test_offline_attempt_local_save_then_explicit_same_binding_publish_recovery(self):
        self.configure(self.a); self.configure(self.b); self.publisher(self.a)
        identity, _, _ = self.create(self.a); self.observe(self.b)
        engine = self.publisher(self.b, 'commit-only')
        original_git = shared_work._git
        def offline(root, *args, **kwargs):
            if 'fetch' in args: return subprocess.CompletedProcess(args, 1, b'', b'offline')
            return original_git(root, *args, **kwargs)
        with patch('eutonos.shared_work._git', side_effect=offline):
            self.assertEqual(self.observe(self.b)['shared_work']['coverage'], 'unknown')
            saved = shared_attempts.start(self.b, identity, 'offline-real-session', self.home / 'bob-host', 'bob')
        self.assertEqual(saved['native_save'], 'complete', saved)
        self.assertEqual(saved['git_delivery']['state'], 'committed', saved)
        self.assertEqual(saved['advisory']['coverage'], 'unknown')
        token, commit = saved['attempt_id'], saved['git_delivery']['commit_oid']
        key = saved['git_delivery']['request_id']
        cfg = {k: v for k, v in engine.config().items() if k not in {'root', 'revision', 'activation_cursor'}}
        cfg['mode'] = 'commit+push'; engine.configure(cfg, expected_revision=engine.config()['revision'])
        engine.resume(key)
        recovered = shared_attempts.start(self.b, identity, 'offline-real-session', self.home / 'bob-host', 'bob')
        self.assertEqual(recovered['attempt_id'], token)
        self.assertEqual(recovered['git_delivery']['state'], 'published', recovered)
        self.assertEqual(recovered['git_delivery']['commit_oid'], commit)
        self.assertEqual(recovered['write_result']['result']['writes'], 0)
        self.assertIn(token, {r['attempt_id'] for r in self.observe(self.a)['attempts']['items']})

    def test_source_conflict_is_visible_and_claim_does_not_choose_one(self):
        self.configure(self.a); self.configure(self.b); self.publisher(self.a); self.publisher(self.b)
        identity, _, _ = self.create(self.a, title='Alice intent')
        self.create(self.b, title='Bob competing intent', identity=identity)
        observed = self.observe(self.b)
        target = next(r for r in observed['boards']['items'] if r['id'] == identity)
        self.assertTrue(target['record_conflict'])
        before = self.git(self.remote, 'rev-parse', 'refs/heads/records/bob')
        with self.assertRaises(ts.ReadError): shared_attempts.start(self.b, identity, 'conflict-session', self.home / 'bob-host', 'bob')
        self.assertEqual(self.git(self.remote, 'rev-parse', 'refs/heads/records/bob'), before)

    def test_shared_config_cas_peers_default_id_and_status_are_passive(self):
        cfg = shared_work.load(self.a)
        value = {'profile': shared_work.PROFILE, **self.identity, 'enabled': False, 'participant': None,
                 'canonical': {'url': str(self.remote), 'ref': 'refs/heads/main'},
                 'peers': [{'url': str(self.remote), 'ref': 'refs/heads/bob'}],
                 'attempt_owner': 'docs/attempts.md', 'fetch_interval_seconds': 5}
        result = shared_work.configure(self.a, value, cfg['revision'])
        self.assertTrue(result['shared_work']['peers'][0]['id'].startswith('peer-'))
        from eutonos.settings import StaleSettings
        with self.assertRaises(StaleSettings) as stale: shared_work.configure(self.a, value, cfg['revision'])
        self.assertEqual(stale.exception.current['revision'], result['revision'])
        from eutonos import settings
        with patch('eutonos.shared_work._git', side_effect=AssertionError('status must not fetch')):
            status = settings.status(self.a)
        self.assertEqual(status['shared_work_revision'], result['revision'])
        self.assertFalse(status['shared_work']['enabled'])
        from eutonos.settings_cli import main as settings_main
        passive = self.cli(settings_main, ['shared-work', 'status', '--root', str(self.a)])
        self.assertEqual(passive['shared_work']['revision'], result['revision'])

    def test_shared_fetch_refuses_git_url_rewrite_without_changing_publisher(self):
        self.configure(self.a, peers=[])
        other = self.home / 'alternate.git'
        self.git(self.home, 'init', '--bare', str(other))
        self.git(self.a, 'config', 'url.' + str(other) + '.insteadOf', str(self.remote))
        with patch('eutonos.shared_work._git', wraps=shared_work._git) as called:
            observed = self.observe(self.a)
        self.assertEqual(observed['shared_work']['coverage'], 'unknown')
        self.assertTrue(any('unavailable' in reason for reason in observed['shared_work']['diagnostics']))
        self.assertFalse(any('fetch' in call.args[1:] for call in called.call_args_list))
        self.git(self.a, 'config', '--unset', 'url.' + str(other) + '.insteadOf')
        recovered = self.observe(self.a)
        self.assertEqual(recovered['shared_work']['coverage'], 'complete')

    def test_auto_fetch_local_migration_defaults_off_preservation_and_revision_safety(self):
        from eutonos import settings
        from eutonos.settings import StaleSettings
        # An unshared root can save just the local preference without a manifest.
        initial = shared_work.load(self.a)
        self.assertTrue(initial['auto_fetch']); self.assertFalse(initial['enabled'])
        selected = shared_work.selection(self.a)
        off = settings.update(self.a, 0, initial['revision'], 'shared_auto_fetch', {'auto_fetch': False})
        self.assertFalse((self.a / shared_work.PATH).exists())
        self.assertEqual(shared_work.selection(self.a), selected)
        with patch('eutonos.shared_work._git', side_effect=AssertionError('unshared never fetches')):
            self.assertEqual(shared_work.observe(self.a)['fetch_state'], 'disabled')
        with self.assertRaises(StaleSettings):
            settings.update(self.a, 0, initial['revision'], 'shared_auto_fetch', {'auto_fetch': True})
        self.assertFalse(shared_work.load(self.a)['auto_fetch'])
        for invalid in (None, 1, 'off'):
            with self.assertRaises(ValueError):
                shared_work.configure(self.a, {'auto_fetch': invalid}, off['revision'])
        self.configure(self.a)
        self.assertFalse(shared_work.load(self.a)['auto_fetch'])
        self.configure(self.a, enabled=False)
        self.configure(self.a, peers=[])
        self.assertFalse(shared_work.load(self.a)['auto_fetch'])
        portable = json.loads((self.a / shared_work.PATH).read_bytes())
        self.assertNotIn('auto_fetch', portable)
        # Old local config reads as on, preserving the former enabled behavior.
        (self.a / shared_work.LOCAL).write_text(json.dumps({'enabled': True, 'participant': 'alice'}))
        migrated = shared_work.load(self.a)
        self.assertTrue(migrated['enabled']); self.assertTrue(migrated['auto_fetch'])
        shared_work.configure(self.a, {'auto_fetch': False}, migrated['revision'])
        self.configure(self.a, canonical={'url': str(self.remote), 'ref': 'refs/heads/alice'})
        self.assertFalse(shared_work.load(self.a)['auto_fetch'])

    def test_auto_fetch_off_retains_cached_work_unknown_coverage_and_resumes_independently(self):
        self.configure(self.a); self.configure(self.b); self.publisher(self.a)
        identity, _, _ = self.create(self.a)
        first = self.observe(self.b, knowledge=True)
        owner = next(r['owner_path'] for r in first['boards']['items'] if r['id'] == identity)
        publisher = self.publisher(self.b, 'commit-only'); delivery = deepcopy(publisher.config())
        cfg = shared_work.load(self.b); selection = shared_work.selection(self.b)
        shared_work.configure(self.b, {'auto_fetch': False}, cfg['revision'])
        self.assertEqual(shared_work.selection(self.b), selection)
        original_git = shared_work._git
        def no_fetch(root, *args, **kwargs):
            self.assertNotIn('fetch', args)
            return original_git(root, *args, **kwargs)
        with patch('eutonos.shared_work._git', side_effect=no_fetch):
            cached = current_work.project(self.b, include_knowledge=True, refresh=True)
        self.assertIn(identity, {r['id'] for r in cached['boards']['items']})
        self.assertIn(owner, {r['path'] for r in cached['documents']['items']})
        observation = cached['shared_work']
        self.assertTrue(observation['enabled']); self.assertFalse(observation['auto_fetch'])
        self.assertEqual(observation['fetch_state'], 'auto-fetch-off')
        self.assertEqual(observation['coverage'], 'unknown')
        self.assertEqual(observation['freshness']['kind'], 'stale')
        self.assertEqual(observation['last_observed_at'], first['shared_work']['last_observed_at'])
        self.assertEqual(observation['peers'][0]['commit'], first['shared_work']['peers'][0]['commit'])
        new_identity, _, _ = self.create(self.a, 'Published while auto-fetch off')
        with patch('eutonos.shared_work._git', side_effect=no_fetch):
            self.assertNotIn(new_identity, {r['id'] for r in current_work.project(self.b, refresh=True)['boards']['items']})
        cfg = shared_work.load(self.b)
        shared_work.configure(self.b, {'auto_fetch': True}, cfg['revision'])
        resumed = self.observe(self.b)
        self.assertIn(new_identity, {r['id'] for r in resumed['boards']['items']})
        self.assertEqual(resumed['shared_work']['coverage'], 'complete')
        self.assertEqual(publisher.config(), delivery)

    def test_dashboard_shared_roster_is_selected_roots_only_passive_and_keeps_project_indexes(self):
        from eutonos import dashboard
        self.configure(self.a, peers=[])
        self.observe(self.a)
        original_git = shared_work._git
        def no_fetch(root, *args, **kwargs):
            self.assertNotIn('fetch', args)
            return original_git(root, *args, **kwargs)
        def project(root, **kwargs):
            if root == self.a: raise ValueError('unreadable selected project')
            return {'name': root.name, 'root': str(root), 'modules': {}}
        with patch('eutonos.shared_work._git', side_effect=no_fetch), patch('eutonos.dashboard.project', side_effect=project):
            snapshot = dashboard.snapshot([self.a, self.b], detail='summary', work_project=0)
        self.assertEqual(len(snapshot['projects']), 2)
        self.assertTrue(snapshot['projects'][0]['unavailable_project'])
        self.assertTrue(snapshot['projects'][1]['deferred_project'])
        roster = snapshot['shared_repositories']
        self.assertEqual([r['project'] for r in roster], [0, 1])
        self.assertTrue(roster[0]['configuration']['enabled'])
        self.assertTrue(roster[0]['configuration']['auto_fetch'])
        self.assertEqual(roster[0]['observation']['coverage_scope'], 'configured-sources-only')
        self.assertEqual(roster[0]['configuration']['peers'], [])
        self.assertFalse(roster[1]['configuration']['enabled'])
        self.assertEqual(roster[1]['observation']['fetch_state'], 'disabled')
        self.assertEqual(snapshot['coverage'], 'selected-roots-only')

    def test_solo_precheck_git_failure_is_unknown_and_not_clear(self):
        self.configure(self.a, enabled=False)
        with patch('eutonos.work_precheck._git', return_value=None):
            result = work_precheck.precheck(self.a, 'not-created')
        self.assertEqual(result['verdict'], 'unknown')

    def test_attempt_scope_and_pair_work_are_bounded(self):
        token = str(uuid.uuid4())
        value = {'profile': shared_attempts.PROFILE, 'attempt_id': token, 'project': self.identity,
                 'participant': 'p', 'session': 's', 'branch': 'b', 'state': 'active', 'intent': 'work', 'checkpoint': 'started',
                 'intentional_parallel': False, 'canonical_commit': self.base,
                 'target': {'work_id': 'campaign-x', 'source_ref': 'refs/heads/main', 'source_commit': self.base, 'revision': '1' * 64, 'ancestors': []}}
        rows = []
        for n in range(201):
            extension = deepcopy(value); extension['attempt_id'] = str(uuid.uuid4())
            rows.append({'id': 'attempt-' + extension['attempt_id'], 'type': 'attempt', 'title': 'Try', 'summary': 'Work', 'status': 'active', 'extensions': {'shared_attempt': extension}})
        result = shared_attempts.project(rows, [], 'complete', self.identity)
        self.assertTrue(result['truncated']); self.assertEqual(result['coverage'], 'unknown')
        self.assertLessEqual(len(result['overlaps']), 200)
        foreign = {**self.identity, 'namespace': 'other'}
        self.assertEqual(shared_attempts.project(rows, [], 'complete', foreign)['items'], [])
        other = deepcopy(value); other['project'] = foreign
        self.assertFalse(shared_attempts.overlaps(value, other))
        tombstone = deepcopy(rows[0]); tombstone['status'] = 'removed'
        unavailable = shared_attempts.project([tombstone], [], 'complete', self.identity)
        self.assertEqual(unavailable['items'][0]['state'], 'unknown')
        self.assertEqual(unavailable['coverage'], 'unknown')
        self.assertEqual(unavailable['overlaps'], [])


    def test_manual_completed_start_survives_later_same_owner_write(self):
        self.configure(self.a); self.configure(self.b); self.publisher(self.a)
        identity, _, _ = self.create(self.a); self.observe(self.b)
        first = shared_attempts.start(self.b, identity, 'manual-first', self.home / 'bob-host', 'bob')
        self.assertEqual(first['git_delivery']['state'], 'manual', first)
        second = shared_attempts.start(self.b, identity, 'manual-second', self.home / 'bob-host', 'bob')
        self.assertNotEqual(first['attempt_id'], second['attempt_id'])
        repeated = shared_attempts.start(self.b, identity, 'manual-first', self.home / 'bob-host', 'bob')
        self.assertEqual(repeated['native_save'], 'complete', repeated)
        self.assertEqual(repeated['attempt_id'], first['attempt_id'])
        self.assertEqual(len([r for r in boards.project(self.b)['items'] if r['type'] == 'attempt']), 2)
        terminal = shared_attempts.finish(self.b, identity, 'manual-first', self.home / 'bob-host', 'bob', state='finished')
        newer = shared_attempts.start(self.b, identity, 'manual-third', self.home / 'bob-host', 'bob')
        recovered = shared_attempts.finish(self.b, identity, 'manual-first', self.home / 'bob-host', 'bob', state='finished')
        self.assertEqual(recovered['native_save'], 'complete', recovered)
        self.assertEqual(recovered['write_result']['request_id'], terminal['write_result']['request_id'])


    def test_deleted_attempt_ref_projects_unknown_instead_of_crashing_current_work(self):
        self.configure(self.a); self.configure(self.b); self.publisher(self.a); self.publisher(self.b)
        identity, _, _ = self.create(self.a); self.observe(self.b)
        attempt = shared_attempts.start(self.b, identity, 'deleted-fixture-session', self.home / 'bob-host', 'bob')
        self.assertEqual(attempt['git_delivery']['state'], 'published', attempt)
        fixture = self.home / 'deleted-records'
        self.git(self.home, 'clone', '-b', 'records/bob', str(self.remote), str(fixture))
        # Establish a real canonical attempt version, then an ordinary descendant
        # deletion on the peer records ref. This is reader-only fixture mutation.
        self.git(fixture, 'push', str(self.remote), 'HEAD:refs/heads/main')
        observed = self.observe(self.a)
        self.assertIn(attempt['attempt_id'], {r['attempt_id'] for r in observed['attempts']['items']})
        path = fixture / 'docs/attempts.md'
        board, matches = boards._parse(path.read_bytes(), 'docs/attempts.md')
        board['items'] = []
        path.write_bytes(boards._replace(path.read_bytes(), matches[0], board))
        self.git(fixture, 'add', 'docs/attempts.md'); self.git(fixture, 'commit', '-m', 'deleted attempt reader fixture')
        self.git(fixture, 'push', str(self.remote), 'HEAD:refs/heads/records/bob')
        removed = self.observe(self.a)
        row = next(r for r in removed['attempts']['items'] if r['attempt_id'] == attempt['attempt_id'])
        self.assertIn(row['state'], {'unknown', 'conflict'})
        self.assertEqual(removed['attempts']['coverage'], 'unknown')
        self.assertFalse(any(r['id'].startswith('attempt-') for r in removed['boards']['items']))


    def test_fetch_coalescing_and_total_round_budget_retain_unknown_coverage(self):
        peers = [{'id': 'p' + str(n), 'url': str(self.remote), 'ref': 'refs/heads/main'} for n in range(8)]
        self.configure(self.a, peers=peers)
        first = self.observe(self.a)
        self.assertEqual(first['shared_work']['coverage'], 'complete')
        import time
        actual_git = shared_work._git
        for setup_seconds in (0, 2):
            with self.subTest(setup_seconds=setup_seconds):
                started, elapsed, fetched, routed = time.monotonic(), [0], [], []
                def slow_observation(root, *args, **kwargs):
                    if args == ('rev-parse', '--git-common-dir'):
                        elapsed[0] += setup_seconds
                    elif args[0] == 'config':
                        routed.append(kwargs['timeout']); elapsed[0] += kwargs['timeout']
                        return subprocess.CompletedProcess(args, 1, b'', b'')
                    elif 'fetch' in args:
                        fetched.append(kwargs['timeout']); elapsed[0] += kwargs['timeout']
                        return subprocess.CompletedProcess(args, 0, b'', b'')
                    elif args[:2] == ('rev-parse', '--verify'):
                        elapsed[0] += kwargs['timeout']
                    return actual_git(root, *args, **kwargs)
                with patch('eutonos.shared_work.time.monotonic', side_effect=lambda: started + elapsed[0]), patch('eutonos.shared_work._git', side_effect=slow_observation):
                    bounded = self.observe(self.a)
                self.assertLessEqual(elapsed[0], 15)
                self.assertEqual(routed, [5, 5] if setup_seconds == 0 else [5, 4])
                self.assertEqual(fetched, [3, 1] if setup_seconds == 0 else [3])
                self.assertEqual(bounded['shared_work']['coverage'], 'unknown')
                self.assertTrue(all(row['commit'] == self.base for row in [bounded['shared_work']['canonical'], *bounded['shared_work']['peers']]))
        def no_fetch(root, *args, **kwargs):
            self.assertNotIn('fetch', args)
            return actual_git(root, *args, **kwargs)
        with patch('eutonos.shared_work.local_lock', side_effect=TimeoutError), patch('eutonos.shared_work._git', side_effect=no_fetch):
            joined = shared_work.observe(self.a)
        self.assertEqual(joined['fetch_state'], 'coalesced')
        self.assertEqual(joined['coverage'], 'unknown')


if __name__ == '__main__': unittest.main()
