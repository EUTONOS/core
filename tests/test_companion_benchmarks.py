import unittest
import json
import hashlib
import tempfile
from pathlib import Path
from eutonos.companion_benchmarks import stage, project_suites, analyze_suite, read_report


class BenchmarkStageTests(unittest.TestCase):
    def row(self, start, end, seconds):
        return dict(state='completed', start=f'2026-09-12T12:00:{start:02d}Z', end=f'2026-09-12T12:00:{end:02d}Z', elapsed_seconds=seconds,
                    usage=dict(input_tokens=100,cached_input_tokens=80,output_tokens=20,reasoning_output_tokens=5),
                    api_estimate_usd=.1,standard_credit_estimate=.25,tool_calls=2,tool_counts={'exec':2},
                    efforts=['medium'])

    def test_handoff_is_retained_and_cached_reasoning_are_subsets(self):
        first, second = self.row(0,10,10), self.row(20,30,10)
        first['models'], second['models'] = ['gpt-6-astra'], ['gpt-6-luna']
        r=stage(['a','b'],{'a':first,'b':second})
        self.assertEqual((r['elapsed_seconds'],r['worker_seconds'],r['handoff_seconds']),(30,20,10))
        self.assertEqual((r['input_tokens'],r['cached_input_tokens'],r['output_tokens'],r['reasoning_output_tokens']),(200,160,40,10))
        self.assertEqual(r['cost_by_model'], {'gpt-6-astra': .1, 'gpt-6-luna': .1})
        self.assertEqual((r['effort'],r['efforts']),('medium',['medium']))
        self.assertEqual(r['tool_counts'],{'exec':4})
        self.assertEqual((r['credit_estimate'],r['credit_priced_run_count'],r['credit_unpriced_run_count']),(.5,2,0))

    def test_overlap_is_not_negative_handoff(self):
        r=stage(['a','b'],{'a':self.row(0,20,20),'b':self.row(10,30,20)})
        self.assertEqual((r['elapsed_seconds'],r['worker_seconds'],r['handoff_seconds']),(30,40,0))

    def test_incomplete_missing_and_duplicates_are_not_zero_cost(self):
        row=self.row(0,10,10)
        for ids,rows in [([],{}),(['missing'],{}),(['a','a'],{'a':row}),(['a'],{'a':{**row,'state':'running'}})]:
            self.assertIsNone(stage(ids,rows)['cost_usd'])

    def test_missing_counters_do_not_invent_measurement(self):
        row=self.row(0,10,10);row['usage']['input_tokens']=None;row['api_estimate_usd']=None;row['standard_credit_estimate']=None
        r=stage(['a'],{'a':row})
        self.assertIsNone(r['input_tokens']);self.assertIsNone(r['cost_usd']);self.assertIsNone(r['credit_estimate'])
        self.assertIsNone(r['cost_by_model'])

    def test_cost_without_one_observed_model_stays_explicitly_unattributed(self):
        r = stage(['a'], {'a': self.row(0, 10, 10)})
        self.assertEqual(r['cost_by_model'], {'unknown': .1})

    def test_read_only_ram_condition_keeps_unrun_cost_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / '.eutonos-local' / 'benchmark-suites.json'
            config.parent.mkdir()
            item = dict(id='ram-off', primary_model='astra', context_condition='continuing',
                        ram_condition='Disabled', closeout_applicable=False,
                        memory_metrics={'read_calls': 0}, answer_run_ids=[], closeout_run_ids=[])
            config.write_text(json.dumps({'profile':'eutonos.benchmark-suites.v1',
                                          'suites':[{'id':'memory','results':[item]}]}))
            result = project_suites(td, [])[0]['results'][0]
            self.assertEqual(result['memory_metrics']['read_calls'], 0)
            self.assertFalse(result['closeout_applicable'])
            self.assertEqual(result['context_condition'], 'continuing')
            self.assertEqual(result['ram_condition'], 'Disabled')
            self.assertIsNone(result['answer']['cost_usd'])
            self.assertIsNone(result['closeout']['cost_usd'])

    def test_helper_only_is_not_a_finished_pair_and_changed_answer_invalidates_score(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / '.eutonos-local' / 'benchmark-suites.json'
            config.parent.mkdir()
            item = dict(id='pair',support_model='luna',answer_run_ids=['h'],closeout_run_ids=[],answer_text='answer',quality={'score':99,'answer_sha256':'stale'})
            config.write_text(json.dumps({'profile':'eutonos.benchmark-suites.v1','suites':[{'id':'s','results':[item]}]}))
            rows = [{**self.row(0,10,10),'id':'h','role':'support'}]
            result = project_suites(td, rows)[0]['results'][0]
            self.assertIsNone(result['answer']['cost_usd'])
            self.assertIsNone(result['answer']['tool_counts'])
            self.assertEqual(result['support']['cost_usd'], .1)
            self.assertEqual(result['total']['credit_estimate'], .25)
            self.assertIsNone(result['quality'])

    def test_run_history_preserves_type_and_waits_for_matching_review(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / '.eutonos-local' / 'benchmark-suites.json'
            config.parent.mkdir()
            item = dict(id='condition',primary_model='astra',answer_run_ids=['p'],answer_text='answer',quality={'score':99,'answer_sha256':'stale'})
            suite = dict(id='run-1',task_id='shared-question',test_type='future-type',description='A distinct run',results=[item])
            def project():
                config.write_text(json.dumps({'profile':'eutonos.benchmark-suites.v1','suites':[suite]}))
                return project_suites(td,[{**self.row(0,10,10),'id':'p','role':'primary'}])[0]
            pending=project()
            self.assertEqual(pending['test_type'],'future-type')
            self.assertEqual(pending['description'],'A distinct run')
            self.assertEqual(pending['started_at'],'2026-09-12T12:00:00Z')
            self.assertIsNone(pending['completed_at'])
            item['quality']['answer_sha256']=hashlib.sha256(b'answer').hexdigest()
            self.assertEqual(project()['completed_at'],'2026-09-12T12:00:10Z')
            suite['completed_at']='2026-09-12T13:00:00Z'
            self.assertEqual(project()['completed_at'],'2026-09-12T13:00:00Z')

    def test_companion_analysis_uses_matched_primary_alone_baseline(self):
        baseline = {'id':'alone','primary_model':'astra','support_model':None,
                    'answer':{'cost_usd':2.0,'input_tokens':100,'reasoning_output_tokens':20,'output_tokens':30},
                    'quality':{'score':90}}
        candidate = {'id':'pair','primary_model':'astra','support_model':'sol',
                     'answer':{'cost_usd':1.5,'input_tokens':80,'reasoning_output_tokens':10,'output_tokens':20},
                     'quality':{'score':92}}
        suite = analyze_suite({'test_type':'companion','results':[baseline,candidate]})
        analysis = suite['results'][1]['analysis']
        self.assertEqual((analysis['status'],analysis['baseline_id']),('candidate','alone'))
        self.assertEqual((analysis['cost_delta_usd'],analysis['cost_delta_percent']),(-.5,-25))
        self.assertEqual((analysis['quality_delta'],analysis['token_delta']),(2,-30))
        self.assertEqual(suite['analysis']['candidate'],1)

    def test_delegation_cost_analysis_uses_primary_only_baseline(self):
        baseline = {'id':'primary-only','primary_model':'astra','support_model':None,
                    'answer':{'cost_usd':2.0,'input_tokens':100,'reasoning_output_tokens':20,'output_tokens':30},
                    'quality':{'score':90}}
        candidate = {'id':'luna-compact','primary_model':'astra','support_model':'luna',
                     'answer':{'cost_usd':1.0,'input_tokens':60,'reasoning_output_tokens':10,'output_tokens':20},
                     'quality':{'score':90}}
        suite = analyze_suite({'test_type':'delegation-cost','results':[baseline,candidate]})
        self.assertEqual(suite['results'][0]['analysis']['status'],'baseline')
        self.assertEqual(suite['results'][1]['analysis']['status'],'candidate')
        self.assertEqual(suite['results'][1]['analysis']['baseline_id'],'primary-only')

    def test_ram_analysis_matches_model_pair_and_context(self):
        off = {'id':'off','primary_model':'astra','support_model':'luna','context_condition':'fresh','ram_condition':'Unavailable',
               'answer':{'cost_usd':1.0},'quality':{'score':95}}
        on = {'id':'on','primary_model':'astra','support_model':'luna','context_condition':'fresh','ram_condition':'Optional via Luna',
              'answer':{'cost_usd':1.2},'quality':{'score':97}}
        suite = analyze_suite({'test_type':'ram','results':[off,on]})
        self.assertEqual(suite['results'][1]['analysis']['label'],'Higher-cost quality gain')
        self.assertEqual(suite['results'][1]['analysis']['baseline_id'],'off')
        self.assertEqual(suite['analysis']['baseline_favored'],0)

    def test_analysis_withholds_interpretation_when_baseline_or_measurement_is_missing(self):
        row = {'id':'pair','primary_model':'astra','support_model':'sol','answer':{},'quality':{'score':99}}
        suite = analyze_suite({'test_type':'companion','results':[row]})
        self.assertEqual(suite['results'][0]['analysis']['status'],'insufficient-evidence')
        self.assertEqual(suite['analysis']['insufficient_count'],1)

    def test_report_metadata_is_projected_only_when_safe_and_readback_is_hash_pinned(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            report = root / '.eutonos-local' / 'reports' / 'opaque.md'
            report.parent.mkdir(parents=True)
            report.write_bytes(b'# Opaque report\n')
            digest = hashlib.sha256(report.read_bytes()).hexdigest()
            config = root / '.eutonos-local' / 'benchmark-suites.json'
            config.write_text(json.dumps({'profile':'eutonos.benchmark-suites.v1','suites':[{'id':'suite','results':[
                {'id':'safe','report_path':'.eutonos-local/reports/opaque.md','report_sha256':digest},
                {'id':'unsafe','report_path':'../opaque.md','report_sha256':digest}]}]}), encoding='utf-8')
            rows = project_suites(root, [])[0]['results']
            self.assertEqual(rows[0]['report_path'], '.eutonos-local/reports/opaque.md')
            self.assertNotIn('report_path', rows[1])
            self.assertEqual(read_report(root, rows[0]['report_path'], rows[0]['report_sha256']), '# Opaque report\n')
            report.write_bytes(b'# Changed\n')
            with self.assertRaises(ValueError):
                read_report(root, '.eutonos-local/reports/opaque.md', digest)

    def test_opaque_report_quality_is_bound_to_report_hash_without_answer_text(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            report = root / '.eutonos-local' / 'reports' / 'opaque.md'
            report.parent.mkdir(parents=True)
            report.write_bytes(b'# Opaque report\n')
            digest = hashlib.sha256(report.read_bytes()).hexdigest()
            config = root / '.eutonos-local' / 'benchmark-suites.json'
            config.write_text(json.dumps({'profile':'eutonos.benchmark-suites.v1','suites':[{'id':'suite','results':[
                {'id':'opaque','report_path':'.eutonos-local/reports/opaque.md','report_sha256':digest,
                 'quality':{'score':95,'answer_sha256':digest}}]}]}), encoding='utf-8')
            row = project_suites(root, [])[0]['results'][0]
            self.assertEqual(row['quality']['score'], 95)
            self.assertNotIn('answer_text', {key: value for key, value in row.items() if value is not None})

    def test_opaque_report_retains_measured_answer_stage(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            report = root / '.eutonos-local' / 'reports' / 'opaque.md'
            report.parent.mkdir(parents=True)
            report.write_bytes(b'# Opaque report\n')
            digest = hashlib.sha256(report.read_bytes()).hexdigest()
            config = root / '.eutonos-local' / 'benchmark-suites.json'
            config.write_text(json.dumps({'profile':'eutonos.benchmark-suites.v1','suites':[{'id':'suite','results':[
                {'id':'opaque','primary_model':'sol','support_model':'luna',
                 'answer_run_ids':['p','s'],'closeout_run_ids':[],
                 'report_path':'.eutonos-local/reports/opaque.md','report_sha256':digest,
                 'quality':{'score':95,'answer_sha256':digest}}]}]}), encoding='utf-8')
            runs = [
                {**self.row(0, 10, 10), 'id':'p', 'role':'primary'},
                {**self.row(1, 8, 8), 'id':'s', 'role':'support'},
            ]
            row = project_suites(root, runs)[0]['results'][0]
            self.assertEqual(row['answer']['status'], 'completed')
            self.assertEqual(row['answer']['input_tokens'], 200)
            self.assertEqual(row['primary']['status'], 'completed')
            self.assertEqual(row['support']['status'], 'completed')
