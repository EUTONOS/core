"""Metadata-only native compaction reconciliation, including future inclusion."""
import json
from pathlib import Path
import tempfile
import unittest

from eutonos.companion_results import FIELDS, read_turns, project, CONFIG


def counts(i=0, c=0, o=0, r=0):
    return dict(zip(FIELDS, (i, c, o, r)))


def add(*values):
    return {key: sum(value[key] for value in values) for key in FIELDS}


class CompactionTests(unittest.TestCase):
    base = counts(100, 60, 20, 5)
    compact = counts(110, 70, 30, 0)
    later = counts(25, 10, 5, 1)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log = self.root / 'native.jsonl'

    def event(self, kind, **payload):
        return {'type': kind, 'payload': payload, 'timestamp': '2026-09-12T12:00:00Z'}

    def counter(self, total, last=None):
        return self.event('event_msg', type='token_count', info={
            'total_token_usage': total, 'last_token_usage': total if last is None else last})

    def start(self, turn='one'):
        return self.event('event_msg', type='task_started', turn_id=turn)

    def complete(self, turn='one'):
        return self.event('event_msg', type='task_complete', turn_id=turn)

    def compaction(self, response='response', turn='one', total=None, **overrides):
        total = add(self.base, self.compact) if total is None else total
        record = dict(thread_id='thread', session_id='thread', turn_id=turn,
                      root_turn_id=turn, response_id=response, usage=self.compact,
                      turn_token_usage=total, thread_token_usage=total)
        record.update(overrides)
        return self.event('compacted', latest_token_usage_record=record,
                          message='PRIVATE COMPACTION', replacement_history='PRIVATE HISTORY')

    def read(self, events):
        prefix = [self.event('session_meta', id='thread'), self.start(),
                  self.event('turn_context', model='gpt-6-sol')]
        self.log.write_text(''.join(json.dumps(e) + '\n' for e in prefix + events), encoding='utf-8')
        rows, diagnostics = read_turns(self.log, 'support', 'experiment')
        self.assertNotIn('PRIVATE', json.dumps(rows))
        return rows, diagnostics

    def test_omitted_compaction_added_once_and_persisted(self):
        rows, diagnostics = self.read([
            self.counter(self.base), self.compaction(), self.compaction(),
            self.counter(self.base, counts()),
            self.counter(add(self.base, self.later), self.later), self.complete()])
        row = rows[0]
        self.assertEqual(diagnostics, [])
        self.assertEqual(row['gaps'], [])
        self.assertEqual(row['usage'], add(self.base, self.compact, self.later))
        self.assertEqual(row['compaction_usage'], self.compact)
        self.assertEqual(row['compaction_count'], 1)
        self.assertEqual(row['max_input'], 110)
        local = self.root / '.eutonos-local'
        local.mkdir()
        (local / 'companion-results.json').write_text(json.dumps({
            'profile': CONFIG, 'sources': [{'path': str(self.log), 'role': 'support', 'experiment_id': 'experiment'}]}))
        self.assertEqual(project(self.root)['runs'][0]['compaction_usage'], self.compact)

    def test_compaction_included_in_next_native_counter_not_doubled(self):
        for last in (counts(), self.compact):
            with self.subTest(last=last):
                rows, _ = self.read([self.counter(self.base), self.compaction(),
                                     self.counter(add(self.base, self.compact), last), self.complete()])
                self.assertEqual(rows[0]['usage'], add(self.base, self.compact))
                self.assertEqual(rows[0]['gaps'], [])

    def test_compaction_included_before_event_not_doubled(self):
        rows, _ = self.read([self.counter(self.base),
                            self.counter(add(self.base, self.compact), self.compact), self.compaction(),
                            self.counter(add(self.base, self.compact), counts()), self.complete()])
        self.assertEqual(rows[0]['usage'], add(self.base, self.compact))
        self.assertEqual(rows[0]['gaps'], [])

    def test_delayed_native_inclusion_not_doubled(self):
        rows, _ = self.read([self.counter(self.base), self.compaction(), self.counter(self.base, counts()),
                            self.counter(add(self.base, self.compact, self.later), self.later), self.complete()])
        self.assertEqual(rows[0]['usage'], add(self.base, self.compact, self.later))
        self.assertEqual(rows[0]['gaps'], [])

    def test_delayed_response_without_identity_cannot_be_counted_as_new_usage(self):
        rows, _ = self.read([self.counter(self.base), self.compaction(), self.counter(self.base, counts()),
                            self.counter(add(self.base, self.compact), self.compact), self.complete()])
        self.assertIsNone(rows[0]['usage']['input_tokens'])
        self.assertTrue(rows[0]['gaps'])

    def test_ambiguous_identity_or_usage_withholds_totals(self):
        for override in ({'turn_id': 'other'}, {'thread_id': 'other'}, {'response_id': None},
                         {'usage': None}, {'thread_token_usage': counts()}):
            with self.subTest(override=override):
                rows, _ = self.read([self.counter(self.base), self.compaction(**override),
                                    self.counter(self.base, counts()), self.complete()])
                self.assertIsNone(rows[0]['usage']['input_tokens'])
                self.assertIsNone(rows[0]['compaction_usage']['input_tokens'])
                self.assertTrue(rows[0]['gaps'])

    def test_unreconciled_turn_total_or_neighbor_withholds_combined_usage(self):
        for events in ([self.compaction(total=self.base), self.counter(self.base, counts())],
                       [self.compaction()],
                       [self.compaction(), self.counter(add(self.base, self.later), self.later)],
                       [self.compaction(), self.counter(self.base, counts()),
                        self.counter(add(self.base, self.later, self.later), self.later)]):
            with self.subTest(events=events):
                rows, _ = self.read([self.counter(self.base), *events, self.complete()])
                self.assertIsNone(rows[0]['usage']['input_tokens'])
                self.assertEqual(rows[0]['compaction_usage'], self.compact)
                self.assertTrue(rows[0]['gaps'])

    def test_conflicting_duplicate_marks_original_and_current_turn_unknown(self):
        rows, _ = self.read([self.counter(self.base), self.compaction(), self.counter(self.base, counts()),
                            self.complete(), self.start('two'), self.compaction(turn='two'), self.complete('two')])
        for row in rows:
            self.assertIsNone(row['usage']['input_tokens'])
            self.assertIsNone(row['compaction_usage']['input_tokens'])

    def test_multiple_compactions_and_turns_preserve_counter_baseline(self):
        second_total = add(self.base, self.compact, self.compact, self.later)
        rows, _ = self.read([self.counter(self.base), self.compaction(), self.counter(self.base, counts()),
                            self.counter(add(self.base, self.later), self.later),
                            self.compaction(response='second', total=second_total),
                            self.counter(add(self.base, self.later), counts()), self.complete(),
                            self.start('two'), self.counter(add(self.base, self.later, self.later), self.later),
                            self.complete('two')])
        self.assertEqual(rows[0]['usage'], second_total)
        self.assertEqual(rows[0]['compaction_count'], 2)
        self.assertEqual(rows[0]['compaction_usage'], add(self.compact, self.compact))
        self.assertEqual(rows[1]['usage'], self.later)
        self.assertEqual(rows[1]['compaction_count'], 0)
        self.assertEqual(rows[1]['gaps'], [])

    def test_delayed_inclusion_in_later_turn_does_not_reassign_usage(self):
        rows, _ = self.read([self.counter(self.base), self.compaction(), self.counter(self.base, counts()),
                            self.complete(), self.start('two'),
                            self.counter(add(self.base, self.compact, self.later), self.later), self.complete('two')])
        self.assertEqual(rows[0]['usage'], add(self.base, self.compact))
        self.assertEqual(rows[1]['usage'], self.later)

    def test_final_phase_yields_output_metadata(self):
        rows, _ = self.read([self.event('response_item', type='message', role='assistant', phase='final',
                                      content='PRIVATE FINAL'), self.counter(self.base), self.complete()])
        self.assertEqual(rows[0]['handoff_tokens'], 15)
        self.assertTrue(rows[0]['final_sha256'])


if __name__ == '__main__':
    unittest.main()
