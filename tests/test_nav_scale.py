"""Large native NAV state and packets retain exact evidence and write checks."""
import unittest
from unittest.mock import patch

import test_nav
from eutonos import nav, tokenslang as ts
from eutonos.effects import digest
from eutonos.nav_runner import Session, MAX_PACKET


class NavScaleTests(unittest.TestCase):
    setUp = test_nav.NavTests.setUp
    git = test_nav.NavTests.git
    commit = test_nav.NavTests.commit
    admit_history = test_nav.NavTests.admit_history
    start = test_nav.NavTests.start
    reject = test_nav.NavTests.reject
    report = test_nav.NavTests.report

    def test_large_native_baseline_is_written_reopened_and_advanced(self):
        for group in range(10):
            folder = self.root / 'src' / ('group-%02d' % group)
            folder.mkdir()
            for item in range(80):
                name = ('source-%03d-' % item) + 'a' * 88 + '.py'
                (folder / name).write_text('def observable(): return True\n', encoding='utf-8')
        self.start('large-baseline')
        raw = self.local.read(self.session.state_path)
        self.assertGreater(len(raw), 131072)
        before = ts.loads(raw.decode())
        self.assertEqual(len(before['baseline']), 803)
        reopened = Session(self.local)
        first = reopened.next(self.host)['packet']
        self.assertLessEqual(len(ts.encoded(first)), MAX_PACKET)
        self.assertEqual(reopened.next(self.host)['packet'], first)
        after = ts.loads(self.local.read(self.session.state_path).decode())
        self.assertEqual(after['baseline'], before['baseline'])
        self.assertEqual(after['sourceFingerprint'], before['sourceFingerprint'])
        report = self.report(first)
        self.assertEqual(reopened.submit(self.host, report)['outcome'], 'submitted')
        second = reopened.next(self.host)['packet']
        self.assertNotEqual(second['packetID'], first['packetID'])
        self.assertGreater(len(self.local.read(self.session.state_path)), 131072)
        prior = ts.loads(self.local.read(self.session.state_path).decode())
        self.start('after-large', restart=True)
        archive = self.session.history(1)['items'][0]['archive']
        self.assertEqual(ts.loads(self.local.read(archive['path']).decode()), prior)

    def test_large_record_uses_exact_reference_in_a_bounded_packet(self):
        source = nav.inventory(self.local, self.cfg, 'eutonos.nav.json')
        text = ('### `SYS-large` Large record\n- Kind: system\n- Aliases: large\n'
                '- Summary: ' + 'meaning ' * 14000 + '\n- Evidence: src/auth.py\n'
                '- Related: none\n- Review: candidate\n- Revision: current\n')
        bodies = {name: b'# Native map\n' for name in nav.MAPS}
        bodies['NAVIGATION.md'] = text.encode()
        records, preambles = nav.parse_maps(bodies)
        sweep = {'id': 'review-large'}
        issued = [{'id': 'lead-large', 'paths': ['src/auth.py'], 'recordIDs': ['SYS-large']}]
        packet = self.session.packet({'runID': 'large-record'}, sweep, issued, source, bodies, records)
        self.assertLessEqual(len(ts.encoded(packet)), MAX_PACKET)
        ref = packet['recordContext'][0]
        self.assertEqual(ref['id'], 'SYS-large')
        self.assertEqual(ref['path'], 'NAV/NAVIGATION.md')
        self.assertEqual(ref['sha256'], digest(bodies['NAVIGATION.md']))
        self.assertEqual(ref['contentStatus'], 'source-reference')
        self.assertIsNone(ref['text'])
        report = {'profile': 'eutonos.tokenslang.nav-report.v1',
                  'packetID': packet['packetID'], 'sweepID': packet['sweepID'],
                  'sourceFingerprint': packet['sourceFingerprint'],
                  'inspectedPaths': list(source['paths']),
                  'leadDispositions': [{'leadID': 'lead-large', 'disposition': 'covered', 'reason': 'Inspected source'}],
                  'proposedRecords': [], 'omissionSearch': 'Checked', 'summary': 'Checked'}
        with self.assertRaises(ts.ReadError) as caught:
            self.session.validate_report(report, packet, source, bodies, records, preambles, 'build')
        self.assertIn('inspectedRecordIDs', caught.exception.message)
        report['inspectedRecordIDs'] = ['SYS-large']
        output, dispositions = self.session.validate_report(report, packet, source, bodies, records, preambles, 'build')
        self.assertEqual(dispositions['lead-large'], 'covered')
        self.assertEqual(nav.parse_maps(output)[0]['SYS-large']['text'], records['SYS-large']['text'])

    def test_streamed_large_file_detects_changes_and_disappearance(self):
        path = self.root / 'src/auth.py'
        path.write_bytes(b'x' * 200000)
        self.assertEqual(len(self.local.read('src/auth.py')), 200000)
        stream = self.local.iter_read('src/auth.py', 4096)
        next(stream)
        with path.open('ab') as target:
            target.write(b'changed')
        with self.assertRaises(ts.ReadError) as caught:
            list(stream)
        self.assertEqual(caught.exception.code, 'conflict')
        stream = self.local.iter_read('src/auth.py', 4096)
        next(stream)
        # Windows cannot unlink an open stream; a same-path mutation above is
        # the portable race proof. Initial absence must remain a tracked deletion.
        stream.close()
        path.unlink()
        self.assertIsNone(self.local.read('src/auth.py'))
        with self.assertRaises(FileNotFoundError):
            list(self.local.iter_read('src/auth.py'))

    def test_large_source_clues_do_not_materialize_the_file(self):
        (self.root / 'src/auth.py').write_bytes(b'x' * 200000)
        source = nav.inventory(self.local, self.cfg, 'eutonos.nav.json')
        bodies = {name: b'# Native map\n' for name in nav.MAPS}
        issued = [{'id': 'lead-large', 'paths': ['src/auth.py'], 'recordIDs': []}]
        original = type(source['contents']).__getitem__
        def guarded(contents, path):
            self.assertNotEqual(path, 'src/auth.py', 'Large source must remain a reference')
            return original(contents, path)
        with patch.object(type(source['contents']), '__getitem__', guarded):
            packet = self.session.packet({'runID': 'large-source'}, {'id': 'source'}, issued, source, bodies, {})
        self.assertEqual(packet['sourceRoutes'][0]['cluesStatus'], 'source-reference')
        self.assertEqual(packet['sourceRoutes'][0]['bytes'], 200000)
        self.assertLessEqual(len(ts.encoded(packet)), MAX_PACKET)

    def test_many_source_clues_share_one_packet_budget(self):
        source = nav.inventory(self.local, self.cfg, 'eutonos.nav.json')
        source['contents'] = dict(source['contents'])
        for index in range(64):
            path = f'src/file-{index}.py'
            body = ('def ' + 'long_name_' * 12 + '(): pass\n') * 8
            source['contents'][path] = body
            source['paths'][path] = digest(body.encode())
            source['sizes'][path] = len(body.encode())
        issued = [{'id': 'many-routes', 'paths': list(source['paths']), 'recordIDs': []}]
        bodies = {name: b'# Native map\n' for name in nav.MAPS}
        packet = self.session.packet({'runID': 'many-routes'}, {'id': 'source'}, issued, source, bodies, {})
        self.assertLessEqual(len(ts.encoded(packet)), MAX_PACKET)
        self.assertEqual(len(packet['sourceRoutes']), len(source['paths']))
        self.assertIn('source-reference', {item['cluesStatus'] for item in packet['sourceRoutes']})
