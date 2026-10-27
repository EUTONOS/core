"""Native turn accounting, pricing and local-only dashboard projection."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from eutonos.companion_results import read_turns, estimate, project, RATES, CONFIG
from eutonos import companion_history, companion_results

class CompanionResultsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.log=self.root/'native.jsonl'
    def write(self, records):
        self.log.write_text(''.join(json.dumps(r)+'\n' for r in records),encoding='utf-8')
    def event(self, kind, payload, time='2026-09-12T12:00:00Z'):
        return dict(type=kind,payload=payload,timestamp=time)
    def usage(self, i=100,c=80,o=20,r=5):
        u=dict(input_tokens=i,cached_input_tokens=c,output_tokens=o,reasoning_output_tokens=r)
        return self.event('event_msg',dict(type='token_count',info=dict(total_token_usage=u,last_token_usage=u)))
    def fixture(self):
        return [self.event('session_meta',{'id':'thread'}),
                self.event('event_msg',{'type':'task_started','turn_id':'one'}),
                self.event('turn_context',{'model':'gpt-6-luna','effort':'medium'}),
                self.event('response_item',{'type':'message','role':'assistant','phase':'analysis','content':'PRIVATE THOUGHT'}),
                self.event('response_item',{'type':'message','role':'assistant','phase':'final_answer','content':'PRIVATE ANSWER'}),
                self.usage(),self.usage(),
                self.event('event_msg',{'type':'task_complete','turn_id':'one'},'2026-09-12T12:00:10Z')]
    def test_metadata_dedup_handoff_and_no_content_export(self):
        self.write(self.fixture())
        rows,gaps=read_turns(self.log,'support','e')
        self.assertEqual(gaps,[])
        r=rows[0]
        self.assertEqual(r['usage']['input_tokens'],100)
        self.assertEqual(r['handoff_tokens'],15)
        self.assertEqual(r['counter_updates'],1)
        self.assertEqual(r['elapsed_seconds'],10)
        self.assertEqual(r['efforts'],['medium'])
        reviewer,_=read_turns(self.log,'reviewer','e')
        self.assertEqual(reviewer[0]['role'],'reviewer')
        self.assertNotIn('PRIVATE',json.dumps(rows))
        self.assertNotIn(str(self.log),json.dumps(rows))
    def test_following_turn_delta_and_filters(self):
        self.write(self.fixture()+[self.event('event_msg',{'type':'task_started','turn_id':'two'},'2026-09-12T12:01:00Z'),self.usage(180,100,35,8)])
        rows,_=read_turns(self.log,'support','e',after='2026-09-12T12:00:30Z')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['usage']['input_tokens'],80)
        self.assertEqual(rows[0]['state'],'running')
        self.assertEqual(rows[0]['models'],[])
        self.assertEqual(rows[0]['efforts'],[])
        rows,_=read_turns(self.log,'support','e',before='2026-09-12T12:00:30Z')
        self.assertEqual(len(rows),1)
    def test_tool_inventory_keeps_names_order_and_counts_without_payloads(self):
        calls=[self.event('response_item',{'type':kind,'name':name,'arguments':'PRIVATE COMMAND','input':'PRIVATE SCRIPT'})
               for kind,name in [('function_call','functions.exec'),('custom_tool_call','functions.exec'),('function_call','tools.view_image')]]
        calls.append(self.event('response_item',{'type':'function_call','arguments':'PRIVATE MISSING NAME'}))
        self.write(self.fixture()[:-1]+calls+self.fixture()[-1:]+[
            self.event('event_msg',{'type':'task_started','turn_id':'two'},'2026-09-12T12:01:00Z')])
        rows,_=read_turns(self.log,'support','e')
        self.assertEqual(rows[0]['tool_counts'],{'functions.exec':2,'tools.view_image':1,'unknown':1})
        self.assertEqual(rows[0]['tool_sequence'],['functions.exec','functions.exec','tools.view_image','unknown'])
        self.assertEqual(sum(rows[0]['tool_counts'].values()),rows[0]['tool_calls'])
        self.assertEqual(rows[1]['tool_counts'],{})
        self.assertNotIn('PRIVATE',json.dumps(rows))
    def test_reset_and_missing_counter_unknown(self):
        self.write(self.fixture()[:-1]+[self.usage(50,20,10,3)])
        rows,_=read_turns(self.log,'support','e')
        self.assertIsNone(rows[0]['usage']['input_tokens'])
        self.assertTrue(rows[0]['gaps'])
        self.write(self.fixture()[:-1]+[self.usage(100,None,20,5)])
        self.assertIsNone(read_turns(self.log,'support','e')[0][0]['usage']['output_tokens'])

    def test_resumed_first_update_reset_requires_complete_last_usage_match(self):
        start=self.event('event_msg',{'type':'task_started','turn_id':'two'},'2026-09-12T12:01:00Z')
        reset=self.usage(50,40,10,2)
        later=self.usage(110,90,23,5)
        later['payload']['info']['last_token_usage']=dict(input_tokens=60,cached_input_tokens=50,output_tokens=13,reasoning_output_tokens=3)
        self.write(self.fixture()+[start,reset,later])
        rows,_=read_turns(self.log,'support','e')
        self.assertEqual(rows[0]['usage']['input_tokens'],100)
        self.assertEqual(rows[1]['usage'],dict(input_tokens=110,cached_input_tokens=90,output_tokens=23,reasoning_output_tokens=5))
        self.assertEqual(rows[1]['gaps'],[])
        self.assertIn('reset',rows[1]['counter_basis'])
        reset['payload']['info']['last_token_usage']={**reset['payload']['info']['last_token_usage'],'input_tokens':45}
        self.write(self.fixture()+[start,reset,later])
        self.assertIsNone(read_turns(self.log,'support','e')[0][1]['usage']['input_tokens'])
    def test_fractional_timestamp_boundary_and_timezones(self):
        records=self.fixture()
        records[1]['timestamp']='2026-09-12T12:00:00.500Z'
        self.write(records)
        rows,_=read_turns(self.log,'support','e',after='2026-09-12T12:00:00Z')
        self.assertEqual(len(rows),1)
        rows,_=read_turns(self.log,'support','e',after='2026-09-12T07:00:00-05:00')
        self.assertEqual(len(rows),1)
        with self.assertRaises(ValueError):read_turns(self.log,'support','e',after='2026-09-12T12:00:00')
    def test_partial_event_is_deferred(self):
        self.write(self.fixture())
        with self.log.open('ab') as f:f.write(b'{"type":')
        rows,gaps=read_turns(self.log,'support','e')
        self.assertEqual(rows[0]['usage']['input_tokens'],100)
        self.assertTrue(gaps)

    def test_mismatched_completion_does_not_close_the_active_turn(self):
        events = self.fixture()[:-1]
        events.append(self.event('event_msg', {'type': 'task_complete', 'turn_id': 'other'},
                                 '2026-09-12T12:00:09Z'))
        self.write(events)

        rows, diagnostics = read_turns(self.log, 'support', 'e')

        self.assertEqual(rows[0]['state'], 'running')
        self.assertIsNone(rows[0]['end'])
        self.assertIn('Completion identity mismatch', diagnostics[0])

        events.append(self.event('event_msg', {'type': 'task_complete', 'turn_id': 'one'},
                                 '2026-09-12T12:00:10Z'))
        self.write(events)
        rows, diagnostics = read_turns(self.log, 'support', 'e')

        self.assertEqual(rows[0]['state'], 'completed')
        self.assertEqual(rows[0]['end'], '2026-09-12T12:00:10Z')
        self.assertEqual(rows[0]['gaps'], [])
        self.assertIn('Completion identity mismatch', diagnostics[0])
    def test_completed_output_pins_do_not_drift_when_log_grows(self):
        self.write(self.fixture())
        first=read_turns(self.log,'support','e')[0][0]
        with self.log.open('a',encoding='utf-8') as f:f.write(json.dumps(self.event('event_msg',{'type':'task_started','turn_id':'next'}))+'\n')
        later=read_turns(self.log,'support','e')[0][0]
        self.assertEqual(first['source_prefix_sha256'],later['source_prefix_sha256'])
        self.assertEqual(first['final_sha256'],later['final_sha256'])
        self.assertEqual(first['source_bytes'],later['source_bytes'])
    def test_price_subsets_and_unknowns(self):
        u={'input_tokens':1000000,'cached_input_tokens':800000,'output_tokens':100000,'reasoning_output_tokens':70000}
        self.assertAlmostEqual(estimate(u,RATES[0]),.176)
        self.assertAlmostEqual(estimate(u,RATES[2]),7.8)
        u['cached_input_tokens']=None
        self.assertIsNone(estimate(u,RATES[0]))
    def test_projection_deduplicates_and_missing_log_is_not_zero(self):
        self.write(self.fixture())
        local=self.root/'.eutonos-local';local.mkdir()
        source={'path':str(self.log),'role':'support','experiment_id':'e'}
        cfg={'profile':CONFIG,'sources':[source,source,dict(source,path=str(self.root/'missing'))]}
        (local/'companion-results.json').write_text(json.dumps(cfg))
        p=project(self.root)
        self.assertEqual(len(p['runs']),1)
        self.assertEqual(len(p['diagnostics']),2)
        self.assertEqual(p['lifetime_metrics']['coverage']['duplicate_turns_excluded'],1)
        self.assertEqual(p['lifetime_metrics']['coverage']['registered_source_count'],3)
        self.assertEqual(p['runs'][0]['project_origin']['basis'],'explicit-source-registration')
        self.assertEqual(p['lifetime_metrics']['timeline']['scope']['explicit_registration_turn_count'],1)
        self.assertIsNone(p['actual_charge_usd'])
        self.assertIsNone(p['measured_savings_usd'])
        self.assertNotIn(str(self.log),json.dumps(p))

    def test_tracking_off_does_not_traverse_configured_native_sessions(self):
        local=self.root/'.eutonos-local';local.mkdir()
        (local/'companion-results.json').write_text(json.dumps({'profile':CONFIG,'sources':[],
                                                                 'native_sessions_root':str(self.root/'sessions')}))
        companion_history.set_tracking(self.root,False)
        with mock.patch.object(companion_results, 'native_sources') as discover:
            result=project(self.root)
        discover.assert_not_called()
        self.assertEqual(result['native_session_discovery']['status'],'tracking-off')

    def test_explicit_source_wins_over_discovery_role_and_window(self):
        records=self.fixture()
        records[0]['payload']['cwd']=str(self.root)
        self.write(records)
        local=self.root/'.eutonos-local';local.mkdir()
        explicit={'path':str(self.log),'role':'support','experiment_id':'explicit','after':'2026-09-12T11:00:00Z'}
        (local/'companion-results.json').write_text(json.dumps({'profile':CONFIG,'sources':[explicit],
                                                                 'native_sessions_root':str(self.root)}))
        result=project(self.root)
        self.assertEqual([(row['role'],row['experiment_id']) for row in result['runs']],[('support','explicit')])
        self.assertEqual(result['native_session_discovery']['discovered_source_count'],0)

    def test_native_discovery_reports_and_excludes_ambiguous_thread_identity(self):
        sessions = self.root / 'sessions'; sessions.mkdir()
        other = self.root / 'other'; other.mkdir()
        for name, cwd in [('project.jsonl', self.root), ('other.jsonl', other)]:
            (sessions / name).write_text(json.dumps({
                'type': 'session_meta', 'payload': {'id': 'duplicate', 'cwd': str(cwd)}}) + '\n',
                encoding='utf-8')
        local=self.root/'.eutonos-local';local.mkdir()
        (local/'companion-results.json').write_text(json.dumps({
            'profile': CONFIG, 'sources': [], 'native_sessions_root': str(sessions)}))

        result = project(self.root)

        self.assertEqual(result['runs'], [])
        self.assertEqual(result['native_session_discovery']['ambiguous_thread_id_count'], 1)
        self.assertEqual(result['native_session_discovery']['discovered_source_count'], 0)
        self.assertTrue(any('Ambiguous native thread identities' in item for item in result['diagnostics']))

    def test_ambiguous_native_history_is_withheld_without_deletion_or_explicit_source_loss(self):
        sessions = self.root / 'sessions'; sessions.mkdir()
        other = self.root / 'other'; other.mkdir()
        records = self.fixture()
        records[0]['payload'].update(id='duplicate', cwd=str(self.root))
        records[1]['payload']['turn_id'] = 'explicit'
        records[-1]['payload']['turn_id'] = 'explicit'
        explicit_log = sessions / 'project.jsonl'
        explicit_log.write_text(''.join(json.dumps(item) + '\n' for item in records), encoding='utf-8')
        (sessions / 'other.jsonl').write_text(json.dumps({
            'type': 'session_meta', 'payload': {'id': 'duplicate', 'cwd': str(other)}}) + '\n', encoding='utf-8')
        (sessions / 'child.jsonl').write_text(json.dumps({
            'type': 'session_meta', 'payload': {'id': 'child', 'cwd': str(other),
            'parent_thread_id': 'duplicate', 'source': {'subagent': {'thread_spawn': {
                'parent_thread_id': 'duplicate', 'agent_path': 'worker'}}}}}) + '\n', encoding='utf-8')
        local=self.root/'.eutonos-local';local.mkdir()
        companion_history.sync(self.root, [
            {'id': 'duplicate:historical', 'thread_id': 'duplicate', 'turn_id': 'historical',
             'project_origin': {'scope': 'current-project', 'basis': 'native-project-session-origin'}},
            {'id': 'child:historical', 'thread_id': 'child', 'turn_id': 'historical',
             'project_origin': {'scope': 'current-project', 'basis': 'native-project-session-origin'}},
        ])
        (local/'companion-results.json').write_text(json.dumps({
            'profile': CONFIG,
            'sources': [{'path': str(explicit_log), 'role': 'primary', 'experiment_id': 'explicit'}],
            'native_sessions_root': str(sessions)}))

        result = project(self.root)

        self.assertEqual([row['id'] for row in result['runs']], ['duplicate:explicit'])
        self.assertEqual(result['runs'][0]['project_origin']['basis'], 'explicit-source-registration')
        self.assertEqual(result['native_session_discovery']['excluded_retained_turn_count'], 2)
        retained = companion_history.sync(self.root, [])['rows']
        self.assertEqual({row['id'] for row in retained},
                         {'duplicate:historical', 'child:historical', 'duplicate:explicit'})
        self.assertNotIn('duplicate', json.dumps(result['diagnostics']))
        self.assertNotIn('child', json.dumps(result['diagnostics']))

if __name__=='__main__':unittest.main()
