import json
from pathlib import Path
import tempfile
import subprocess
import os
import unittest
from unittest import mock

from eutonos import boards, completion_checks, work_lifecycle as work, write_batch, write_retry, work_closeout, closeout_route
from eutonos.effects import LocalRoot, decode, digest
from eutonos.host import enroll, renew, connect, selected_apply
from eutonos import tokenslang as ts
from eutonos.document_views import changes


def report_bounds(root, label, changes, parent='HEAD'):
    """Emit actual fixture writer scope and exact engine baseline accounting."""
    rows=[r for r in subprocess.check_output(['git','-C',str(root),'ls-tree','-rz',parent]).split(b'\0') if r]
    unique=list(dict.fromkeys(r.split(b'\t',1)[0].split()[2] for r in rows))
    materialized=subprocess.check_output(['git','-C',str(root),'cat-file','--batch'],input=b'\n'.join(unique)+b'\n')
    print('ASW4_BOUNDS '+json.dumps({'kind':label,'scope_paths':len(changes),
        'scope_raw_bytes':sum(len(decode(v) or b'') for c in changes.values() for v in c.values()),
        'baseline_files':len(rows),'baseline_materialization_bytes':len(materialized)}))


class WorkLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        (self.root / "docs/campaigns").mkdir(parents=True)
        self.host = Path(self.temp.name) / "host"
        self.owner = "docs/WORK_ITEMS.md"
        self.campaign = "docs/campaigns/TEST.md"
        self.write_board(self.owner, [self.item("A"), self.item("B", status="planned")])
        self.write_board(self.campaign, [self.item("TEST", "campaign"),
            self.item("TEST1", "stage", parent_id="TEST"), self.item("TEST2", "stage", status="planned", parent_id="TEST")])
        (self.root / "eutonos.boards.json").write_text(json.dumps({"profile": boards.CONFIG, "owners": [self.owner, self.campaign]}))
        (self.root / "docs/CHANGELOG.md").write_text("# Changes\n\nKeep legacy prose.\n")
        work.folders(self.root)
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        for args in (("init", "-q", "-b", "main"), ("add", "--all"),
                     ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "baseline")):
            subprocess.run(["git", "-C", str(self.root), *args], env=env, check=True, capture_output=True)
        self.apply({"operation": "initialize"})

    def item(self, identity, kind="slice", status="active", **extra):
        return {"id": identity, "type": kind, "status": status, "title": identity + " title", "summary": "Useful scope.", **extra}

    def write_board(self, path, items):
        (self.root / path).write_text("# Work\n\nKeep prose. [Reference](../reference.md)\n\n```tokenslang-board\n" + json.dumps({"profile": boards.BOARD, "items": items}) + "\n```\n")

    def row(self, identity):
        return next(i for i in boards.project(self.root)["items"] if i["id"] == identity)

    def request(self, identity="A", operation="update", **extra):
        return {"operation": operation, "item_id": identity, "expected_revision": self.row(identity)["revision"], **extra}

    def complete(self, identity="A", **extra):
        return self.request(identity, "complete", what="Implemented scope.", why="User requested it.", result="Checked outcome.", checks=["Focused tests passed."], **extra)

    def test_required_completion_checks_gate_explicit_lifecycle_completion(self):
        check = {"id": "acceptance", "title": "Target readback", "required": True,
                 "state": "pending", "evidence": [], "related_work_ids": []}
        self.apply(self.request(completion_checks=[check]))
        with self.assertRaisesRegex(ts.ReadError, "Required completion checks"):
            work.plan(self.root, self.complete())
        with self.assertRaises(ts.ReadError):
            work.plan(self.root, self.request(changes={"status": "completed"}))
        verified = {**check, "state": "verified", "evidence": [{"path": self.owner}]}
        self.apply(self.request(completion_checks=[verified]))
        saved = self.row("A")["extensions"]["completion_checks"][0]
        self.assertEqual(saved["verification"]["definition_sha256"],
                         completion_checks._definition(saved))
        self.apply(self.complete())
        self.assertEqual(self.row("A")["status"], "completed")
        with self.assertRaisesRegex(ts.ReadError, "Required completion checks"):
            work.plan(self.root, self.request(operation="archive", completion_checks=[
                saved, {**check, "id": "new-required"}]))

    def test_guided_closeout_keeps_completion_check_update_on_parent(self):
        check = {"id": "acceptance", "title": "Target readback", "required": True,
                 "state": "partial", "evidence": [], "related_work_ids": ["A"],
                 "remaining_action": "Inspect target."}
        context = {"root": self.root, "work_id": "TEST1"}
        rows = work_closeout.lifecycle_assessment({}, context)["example"]["work_closeout"]
        rows[0]["completion_checks"] = [check]
        assessment = work_closeout.lifecycle_assessment({"work_closeout": rows}, context)
        self.assertEqual(assessment["outcome"], "verified", assessment)
        self.assertEqual(assessment["requests"][0]["completion_checks"], [check])
        self.assertNotIn("completion_checks", assessment["requests"][1])

    def apply(self, request):
        prepared = work.plan(self.root, request)
        (renew if self.host.exists() else enroll)(self.root, self.host, "writer", prepared["paths"], shared=True)
        result = write_retry.submit_work(self.host, self.root, "writer", request)
        self.assertEqual(result["outcome"], "complete", result)
        return result

    def delivery_config(self, mode='commit-only'):
        from eutonos.git_delivery import Engine, CONFIG
        import sys
        def git(*args):
            return subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True).stdout.decode().strip()
        git('config', 'user.name', 'ASW4 Fixture'); git('config', 'user.email', 'asw4@example.invalid')
        git('add', 'docs', 'eutonos.boards.json', 'eutonos.work.json')
        git('commit', '-m', 'initialized work baseline')
        remote = self.root.parent / 'remote.git'
        git('init', '--bare', str(remote))
        git('push', str(remote), 'HEAD:refs/heads/main')
        engine = Engine(self.root)
        engine.configure({'profile': CONFIG, 'project_id': 'fixture', 'participant': 'participant',
            'mode': mode, 'remote_url': str(remote), 'remote_ref': 'refs/heads/main',
            'records_ref': 'refs/heads/records/asw4', 'branch': 'refs/heads/main',
            'canonical_base': git('rev-parse', 'HEAD'),
            'allowed_paths': ['docs/', 'eutonos.boards.json', 'eutonos.work.json'],
            'checks': [[sys.executable, '-c', "from pathlib import Path; assert Path('eutonos.work.json').is_file()"]]})
        return engine

    def test_native_automatic_work_publishes_only_frozen_records_once(self):
        engine = self.delivery_config('commit+push')
        unrelated = self.root / 'unrelated.py'; unrelated.write_text('private dirty code')
        request = self.request(changes={'summary': 'Native automatic checkpoint.'})
        result = self.apply(request)['result']
        self.assertEqual(result['git_delivery']['state'], 'published', result)
        job=engine.status()[0]
        report_bounds(self.root,'native-work-update',job['payload']['changes'],job['payload']['source_parent'])
        repeated = write_retry.submit_work(self.host, self.root, 'writer', request)['result']
        self.assertEqual(repeated['git_delivery']['commit_oid'], result['git_delivery']['commit_oid'])
        self.assertEqual(len(engine.status()), 1)
        self.assertNotIn('unrelated.py', engine.status()[0]['payload']['changes'])
        self.assertEqual(unrelated.read_text(), 'private dirty code')
        self.assertEqual(subprocess.run(['git','-C',str(self.root),'diff','--cached','--name-only'],capture_output=True).stdout, b'')

    def test_native_transport_failure_and_manual_mode_preserve_save(self):
        engine = self.delivery_config()
        with mock.patch('eutonos.git_delivery.Engine.run', side_effect=OSError('transport unavailable')):
            result = self.apply(self.request(changes={'summary': 'Saved despite offline publisher.'}))['result']
        self.assertEqual(result['outcome'], 'complete')
        self.assertEqual(result['git_delivery']['state'], 'unavailable')
        self.assertEqual(self.row('A')['summary'], 'Saved despite offline publisher.')
        key = engine.status()[0]['id']
        self.assertEqual(engine.run(key)['state'], 'committed')
        cfg = engine.config(); cfg = {k:v for k,v in cfg.items() if k not in {'root','revision','activation_cursor'}}
        cfg['mode'] = 'manual'; engine.configure(cfg, expected_revision=1)
        with mock.patch('eutonos.git_delivery.Engine.run', side_effect=AssertionError('manual Git work')):
            manual = self.apply(self.request(changes={'summary': 'Manual save.'}))['result']
        self.assertEqual(manual['git_delivery']['state'], 'manual')
        self.assertEqual(len(engine.status()), 1)

    def test_native_recovery_proof_survives_later_owner_edit(self):
        from eutonos import git_delivery_native as native
        engine = self.delivery_config()
        prepared = work.plan(self.root, self.request(changes={'summary': 'Frozen native save.'}))
        renew(self.root, self.host, 'writer', prepared['paths'], shared=True)
        with mock.patch.object(native, '_publish', side_effect=OSError('handoff gap')):
            result = work.apply_prepared(self.root, self.host, 'writer', prepared, 'proof-gap')
        self.assertEqual(result['outcome'], 'complete')
        self.assertEqual(len(native.inspect(self.root)['native_intents']), 1)
        original = (self.root / self.owner).read_text()
        (self.root / self.owner).write_text(original.replace('Keep prose.', 'Concurrent later prose.'))
        with mock.patch.object(LocalRoot, 'replace', side_effect=AssertionError('native replay')):
            recovered = native.recover(self.root, self.host, 'writer', 'work-lifecycle-proof-gap',
                {'completed':'recover','next':'none','pending':None},
                verify=lambda: (_ for _ in ()).throw(AssertionError('superseded current bytes')))
        self.assertTrue(recovered['complete'])
        self.assertEqual(recovered['git_delivery']['state'], 'committed', recovered)
        self.assertIn('Concurrent later prose.', (self.root / self.owner).read_text())

    def test_native_interruption_recovers_typed_original_without_reapply(self):
        from eutonos import git_delivery_native as native
        engine = self.delivery_config()
        prepared = work.plan(self.root, self.request(changes={'summary': 'Interrupted native save.'}))
        renew(self.root, self.host, 'writer', prepared['paths'], shared=True)
        class Crash(BaseException): pass
        with mock.patch('eutonos.work_lifecycle.verify_effect', side_effect=Crash):
            with self.assertRaises(Crash):
                work.apply_prepared(self.root, self.host, 'writer', prepared, 'interrupted')
        with mock.patch.object(LocalRoot, 'replace', side_effect=AssertionError('native replay')):
            recovered = native.recover(self.root, self.host, 'writer', 'work-lifecycle-interrupted',
                {'completed':'recover','next':'none','pending':None},
                verify=lambda: work.verify_effect(self.root, prepared['effect']))
        self.assertEqual(recovered['git_delivery']['state'], 'committed', recovered)
        again = native.recover(self.root, self.host, 'writer', 'work-lifecycle-interrupted',
            {'completed':'recover','next':'none','pending':None}, verify=lambda: None)
        self.assertEqual(recovered['git_delivery']['commit_oid'], again['git_delivery']['commit_oid'])

    def test_historical_manual_save_is_not_published_when_enabled(self):
        prepared = work.plan(self.root, self.request(changes={'summary': 'Manual historical save.'}))
        renew(self.root, self.host, 'writer', prepared['paths'], shared=True)
        work.apply_prepared(self.root, self.host, 'writer', prepared, 'historical')
        engine = self.delivery_config()
        from eutonos.git_delivery_native import recover
        result = recover(self.root, self.host, 'writer', 'work-lifecycle-historical',
            {'completed':'recover','next':'none','pending':None},
            verify=lambda: work.verify_effect(self.root, prepared['effect']))
        self.assertEqual(result['git_delivery']['state'], 'not-submitted')
        self.assertEqual(engine.status(), [])

    def test_native_queue_submitted_before_enable_does_not_gain_publication(self):
        from eutonos.git_delivery_native import inspect
        request=self.request(changes={'summary':'Old manual queue finally saved.'})
        prepared=work.plan(self.root,request);renew(self.root,self.host,'writer',prepared['paths'],shared=True)
        queued=write_retry.enqueue(self.host,self.root,'writer','work-lifecycle',{'request':request})
        engine=self.delivery_config('commit+push')
        result=write_retry.run_once(self.host,self.root)
        self.assertEqual(result['outcome'],'complete',result)
        self.assertEqual(result['git_delivery']['state'],'unavailable')
        self.assertIn('predates',result['git_delivery']['error'])
        self.assertEqual(self.row('A')['summary'],'Old manual queue finally saved.')
        self.assertEqual(engine.status(),[]);self.assertEqual(inspect(self.root)['native_intents'],[])
        # Dispatch context was reset: a later newly submitted native event qualifies.
        from eutonos.git_delivery_native import _engine
        self.assertIsNotNone(_engine(self.root))

    def test_native_queue_cannot_adopt_rebound_destination(self):
        from eutonos.git_delivery_native import inspect
        engine=self.delivery_config('commit+push')
        request=self.request(changes={'summary':'Old binding queue finally saved.'})
        prepared=work.plan(self.root,request);renew(self.root,self.host,'writer',prepared['paths'],shared=True)
        write_retry.enqueue(self.host,self.root,'writer','work-lifecycle',{'request':request})
        cfg={k:v for k,v in engine.config().items() if k not in {'root','revision','activation_cursor'}}
        remote=self.root.parent/'rebound.git'
        subprocess.run(['git','init','--bare',str(remote)],check=True,capture_output=True)
        subprocess.run(['git','-C',str(self.root),'push',str(remote),'HEAD:refs/heads/main'],check=True,capture_output=True)
        cfg['remote_url']=str(remote);engine.configure(cfg,expected_revision=1)
        result=write_retry.run_once(self.host,self.root)
        self.assertEqual(result['outcome'],'complete',result)
        self.assertEqual(result['git_delivery']['state'],'unavailable')
        self.assertEqual(engine.status(),[]);self.assertEqual(inspect(self.root)['native_intents'],[])
        self.assertNotEqual(subprocess.run(['git','--git-dir',str(remote),'rev-parse','--verify','refs/heads/records/asw4'],capture_output=True).returncode,0)

    def test_corrupt_delivery_configuration_does_not_change_native_success(self):
        engine=self.delivery_config()
        with engine.db() as db:
            db.execute('UPDATE config SET payload=?', (json.dumps({'profile':'wrong','mode':'commit+push','revision':1}),))
        result=self.apply(self.request(changes={'summary':'Native save survives invalid config.'}))['result']
        self.assertEqual(result['outcome'],'complete')
        self.assertEqual(result['git_delivery']['state'],'unavailable')
        self.assertEqual(self.row('A')['summary'],'Native save survives invalid config.')

    def test_real_multi_owner_native_batch_limit_and_supported_scope(self):
        owners=[]
        for index in range(16):
            path='docs/OWNER'+str(index)+'.md';owners.append(path)
            self.write_board(path,[self.item('N'+str(index),status='planned')])
            owner=self.root/path;owner.write_bytes(owner.read_bytes().replace(b'\r\n',b'\n'))
        cfg=json.loads((self.root/'eutonos.boards.json').read_text());cfg['owners']+=owners
        (self.root/'eutonos.boards.json').write_text(json.dumps(cfg))
        self.apply({'operation':'refresh'})
        engine=self.delivery_config()
        batch={'profile':write_batch.PROFILE,'operations':[{'kind':'work','request':self.request('N'+str(i),changes={'status':'active'})} for i in range(16)]}
        original_plan=write_batch.effect_plan
        def measure(local,updates,**kwargs):
            print('ASW4_BOUNDS '+json.dumps({'kind':'sixteen-owner-native-refusal','native_planned_outputs':len(updates),
                'scope_raw_bytes':sum(len(local.read(p) or b'')+len(body or b'') for p,body in updates.items())}))
            return original_plan(local,updates,**kwargs)
        with mock.patch.object(write_batch,'effect_plan',side_effect=measure):
            with self.assertRaises(ts.ReadError) as caught:write_batch.plan(self.root,batch)
        self.assertEqual(caught.exception.code,'overflow')
        self.assertEqual(engine.status(),[])
        self.assertTrue(all(self.row('N'+str(i))['status']=='planned' for i in range(16)))
        batch['operations']=batch['operations'][:4]
        prepared=write_batch.plan(self.root,batch)
        report_bounds(self.root,'four-owner-native-batch',{c['path']:{'before':c['backup'],'after':c['after']} for c in prepared['effect']['changes']})
        renew(self.root,self.host,'writer',prepared['paths'],shared=True)
        result=write_batch.apply_prepared(self.root,self.host,'writer',prepared,'bounded-batch')
        self.assertEqual(result['outcome'],'complete')
        self.assertEqual(result['git_delivery']['state'],'committed',result['git_delivery'].get('error'))

    def test_native_large_preserved_owner_saves_without_oversize_publication(self):
        owner=self.root/self.owner
        owner.write_bytes(owner.read_bytes()+b'\n'+b'A'*1_000_001+b'\n')
        engine=self.delivery_config()
        prepared=work.plan(self.root,self.request(changes={'summary':'Valid large native owner.'}))
        report_bounds(self.root,'large-preserved-owner',{c['path']:{'before':c['backup'],'after':c['after']} for c in prepared['effect']['changes']})
        renew(self.root,self.host,'writer',prepared['paths'],shared=True)
        result=work.apply_prepared(self.root,self.host,'writer',prepared,'large-owner')
        self.assertEqual(result['outcome'],'complete')
        self.assertEqual(result['git_delivery']['state'],'unavailable')
        self.assertIn('2,000,000',result['git_delivery']['error'])
        self.assertEqual(self.row('A')['summary'],'Valid large native owner.')
        self.assertEqual(engine.status(),[])

    def adaptive(self, disposition="adjusted", **extra):
        return {"disposition": disposition, "basis": "Evidence narrows the next parser.",
                "affected_stage_ids": ["TEST2"], "changes": ["Use the verified smaller boundary."],
                "next_stage_id": "TEST2", **extra}

    def test_adaptive_guided_prepare_managed_batch_and_retry(self):
        recommendation = {"model": "future-model", "effort": "careful", "basis": "Bounded parser"}
        context = {"root": str(self.root), "host": str(self.host), "actor": "writer", "work_id": "TEST1", "session_id": "fixture"}
        rows = work_closeout.lifecycle_assessment({}, context)["example"]["work_closeout"]
        rows[0].update(status="completed", stage_assessment=self.adaptive())
        next_row = {**rows[0], "item_id": "TEST2", "expected_revision": self.row("TEST2")["revision"],
                    "status": "planned", "execution_recommendation": recommendation, "next_action": "Implement the smaller parser."}
        next_row.pop("stage_assessment")
        rows.append(next_row)
        values = {"catalog_changes": [], "work_changes": [], "work_closeout": rows}
        prepared = closeout_route.prepare(values, context)
        self.assertEqual(prepared["outcome"], "prepared", prepared)
        self.assertEqual(prepared["lifecycle_assessment"]["requests"][0]["stage_assessment"], self.adaptive())
        renew(self.root, self.host, "writer", prepared["allowed_paths"], shared=True)
        with mock.patch("eutonos.closeout_route.prepare_memory", return_value={"profile": "eutonos.tokenslang.workflow.v1", "outcome": "skipped", "reason": "fixture-only"}):
            result = closeout_route.run(prepared, context)
        self.assertEqual(result["outcome"], "completed", result)
        self.assertEqual(self.row("TEST1")["extensions"]["stage_assessment"], self.adaptive())
        self.assertEqual(self.row("TEST2")["extensions"]["execution_recommendation"], {**recommendation, "advisory": True})
        self.assertEqual(self.row("TEST2")["status"], "planned")
        history = self.row("TEST1")["extensions"]["work_history"]
        batch = prepared["lifecycle_batch"]["request"]
        repeated = write_retry.submit_batch(self.host, self.root, "writer", batch)
        self.assertEqual(repeated["outcome"], "complete", repeated)
        self.assertEqual(self.row("TEST1")["extensions"]["work_history"], history)

    def test_adaptive_scope_and_terminal_sibling_rejection(self):
        for operation in ("refresh", "reorder", "restore", "cancel"):
            with self.subTest(operation=operation), self.assertRaises(ts.ReadError):
                work.plan(self.root, {"operation": operation, "stage_assessment": self.adaptive()})
        items = [self.row("TEST1"), self.row("TEST2"), self.item("OTHER", "campaign"),
                 self.item("OTHER1", "stage", parent_id="OTHER")]
        for assessment in (self.adaptive(affected_stage_ids=["OTHER1"]), self.adaptive(next_stage_id="OTHER1")):
            with self.subTest(assessment=assessment), self.assertRaises(ts.ReadError):
                work.stage_assessment(assessment, self.row("TEST1"), items)
        self.apply(self.complete("TEST2"))
        with self.assertRaises(ts.ReadError):
            work.plan(self.root, self.complete("TEST1", stage_assessment=self.adaptive()))
        self.assertIsNone(work.form(self.root, "TEST")["stage_assessment"])
        self.assertIsNone(work.form(self.root, "TEST")["execution_recommendation"])

    def test_optional_recommendation_does_not_require_assessment(self):
        self.apply(self.request("TEST1", changes={"execution_recommendation": {"model": "future", "effort": "careful", "basis": "Fits"}}))
        form = work.form(self.root, "TEST1")
        self.assertNotIn("stage_assessment", form["closeout"])
        self.assertEqual(form["request"]["changes"], {})
        self.apply(self.complete("TEST1"))
        self.assertNotIn("stage_assessment", self.row("TEST1")["extensions"])

    def test_assessment_does_not_change_sibling_and_survives_recompletion(self):
        before = self.row("TEST2")
        self.apply(self.complete("TEST1", stage_assessment=self.adaptive()))
        self.assertEqual(self.row("TEST2"), before)
        self.apply(self.request("TEST1", "restore"))
        second = self.adaptive("unchanged", affected_stage_ids=[], changes=[])
        second.pop("next_stage_id")
        self.apply(self.complete("TEST1", stage_assessment=second))
        row = self.row("TEST1")
        self.assertEqual(row["extensions"]["stage_assessment"], second)
        completed = [e for e in row["extensions"]["work_history"] if e["status"] == "completed"]
        self.assertEqual([e["stage_assessment"] for e in completed], [self.adaptive(), second])

    def test_assessment_rejects_invalid_boundaries_before_writes(self):
        before = (self.root / self.campaign).read_bytes()
        variants = [self.adaptive(affected_stage_ids=["TEST1"]), self.adaptive(affected_stage_ids=["A"]),
                    self.adaptive(affected_stage_ids=["TEST2", "TEST2"]), self.adaptive(next_stage_id="missing"),
                    self.adaptive("unchanged"), self.adaptive(changes=[]), self.adaptive(basis=" "),
                    self.adaptive("unknown"), self.adaptive(disposition={}), self.adaptive(disposition=[])]
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ts.ReadError):
                work.plan(self.root, self.complete("TEST1", stage_assessment=value))
        for identity, operation in (("A", "complete"), ("TEST", "complete"), ("TEST1", "update")):
            request = self.complete(identity, stage_assessment=self.adaptive()) if operation == "complete" else self.request(identity, stage_assessment=self.adaptive())
            with self.subTest(identity=identity), self.assertRaises(ts.ReadError):
                work.plan(self.root, request)
        self.assertEqual((self.root / self.campaign).read_bytes(), before)

    def test_guided_rejects_assessment_on_noncompletion(self):
        rows = work_closeout.lifecycle_assessment({}, {"root": self.root, "work_id": "TEST1"})["example"]["work_closeout"]
        rows[0]["stage_assessment"] = self.adaptive()
        assessed = work_closeout.lifecycle_assessment({"work_closeout": rows}, {"root": self.root, "work_id": "TEST1"})
        self.assertEqual(assessed["outcome"], "incomplete")
        self.assertTrue(assessed["errors"])

    def test_adaptive_batch_stale_sibling_rejects_every_change(self):
        request = self.request("TEST2", changes={"execution_recommendation": {"model": "future", "effort": "careful", "basis": "Fits"}})
        self.apply(self.request("TEST2", changes={"summary": "Concurrent update"}))
        before = (self.root / self.campaign).read_bytes()
        with self.assertRaises(ts.ReadError):
            write_batch.plan(self.root, {"profile": write_batch.PROFILE, "operations": [
                {"kind": "work", "request": self.complete("TEST1", stage_assessment=self.adaptive())},
                {"kind": "work", "request": request}]})
        self.assertEqual((self.root / self.campaign).read_bytes(), before)

    def test_priority_is_independent_and_every_live_item_is_listed_once(self):
        order = boards.project(self.root)["work_order"]
        self.assertEqual(order["current"], ["A", "TEST"])
        self.apply({"operation": "reorder", "expected_order_revision": order["revision"], "current": ["B"], "next": ["TEST", "A"]})
        self.assertEqual(self.row("B")["status"], "planned")
        self.assertEqual(boards.project(self.root)["work_order"]["next"], ["TEST", "A"])
        with self.assertRaises(ts.ReadError):
            work.plan(self.root, {"operation": "reorder", "expected_order_revision": order["revision"], "current": ["A"], "next": ["B", "TEST"]})

    def rows_fixture(self):
        """TEST holds three stages (the last a qualification stage); OTHER holds two."""
        self.write_board(self.campaign, [self.item("TEST", "campaign"),
            self.item("TEST1", "stage", parent_id="TEST"),
            self.item("TEST2", "stage", status="planned", parent_id="TEST"),
            self.item("TEST3", "stage", status="planned", parent_id="TEST", extensions={"stage_kind": "qualification"}),
            self.item("OTHER", "campaign", status="planned"),
            self.item("OTHER1", "stage", status="planned", parent_id="OTHER"),
            self.item("OTHER2", "stage", status="planned", parent_id="OTHER")])

    def reorder(self, current, nxt):
        order = boards.project(self.root)["work_order"]
        return {"operation": "reorder", "expected_order_revision": order["revision"], "current": current, "next": nxt}

    def refuses(self, request, pattern):
        with self.assertRaisesRegex(ts.ReadError, pattern):
            work.plan(self.root, request)

    def test_string_only_order_file_loads_unchanged(self):
        self.assertEqual(json.loads((self.root / work.PATH).read_bytes())["current"], ["A", "TEST"])
        self.assertEqual(work.load_order(LocalRoot(self.root))["current"], ["A", "TEST"])
        shown = boards.project(self.root)["work_order"]
        self.assertEqual((shown["current"], shown["rows"]["current"]), (["A", "TEST"], ["A", "TEST"]))
        self.apply(self.reorder(["B", "A"], ["TEST"]))
        self.assertEqual(json.loads((self.root / work.PATH).read_bytes())["current"], ["B", "A"])

    def test_together_group_and_stage_rows_round_trip(self):
        self.rows_fixture()
        self.apply(self.reorder(["TEST1", {"together": ["A", "OTHER1", "OTHER2"]}, "B"], ["TEST", "OTHER", "TEST2"]))
        saved = json.loads((self.root / work.PATH).read_bytes())
        self.assertEqual(saved["current"], ["TEST1", {"together": ["A", "OTHER1", "OTHER2"]}, "B"])
        self.assertEqual(saved["next"], ["TEST", "OTHER", "TEST2"])
        shown = boards.project(self.root)["work_order"]
        self.assertEqual(shown["rows"]["current"], saved["current"])
        # Flat consumers still see only top-level work, in order; stages never show there.
        self.assertEqual((shown["current"], shown["next"]), (["A", "B"], ["TEST", "OTHER"]))
        self.assertEqual((self.row("A")["work_lane"], self.row("A")["priority"]), ("current", 0))
        self.assertIsNone(self.row("TEST1")["work_lane"])
        items = boards.project(self.root)["items"]
        self.assertEqual(work.ordered(saved, items), saved)

    def test_reorder_counts_group_members_and_makes_stage_rows_optional(self):
        self.rows_fixture()
        self.apply(self.reorder([{"together": ["A", "B"]}], ["TEST", "OTHER"]))
        self.refuses(self.reorder([{"together": ["A", "OTHER1"]}], ["TEST", "OTHER"]), "every live top-level ID")
        self.refuses(self.reorder([{"together": ["A", "B"]}, "A"], ["TEST", "OTHER"]), "exactly once")
        self.refuses(self.reorder([{"together": ["A", "B"]}], ["TEST", "NOPE", "OTHER"]), "absent, finished or not a stage")
        self.refuses(self.reorder([{"together": ["A", "B"]}], ["TEST", "OTHER", "TEST1", "TEST1"]), "exactly once")
        self.refuses(self.reorder([{"together": ["A"]}, "B"], ["TEST", "OTHER"]), "together groups of two or more")
        self.refuses(self.reorder([{"together": ["A", "B"], "extra": 1}], ["TEST", "OTHER"]), "together groups of two or more")
        self.refuses(self.reorder(["A", 7, "B"], ["TEST", "OTHER"]), "together groups of two or more")

    def test_stage_rows_keep_each_campaigns_own_stage_order(self):
        self.rows_fixture()
        self.apply(self.reorder(["TEST1", "A", "TEST2"], ["B", "TEST", "OTHER", "OTHER1"]))
        self.refuses(self.reorder(["TEST2", "TEST1"], ["A", "B", "TEST", "OTHER"]), "campaign's own order")
        self.refuses(self.reorder(["TEST2"], ["A", "B", "TEST", "OTHER", "TEST1"]), "campaign's own order")
        self.refuses(self.reorder([{"together": ["TEST2", "TEST1"]}], ["A", "B", "TEST", "OTHER"]), "campaign's own order")
        self.refuses(self.reorder(["OTHER2", {"together": ["A", "OTHER1"]}], ["B", "TEST", "OTHER"]), "campaign's own order")
        # Different campaigns interleave freely, and a group may hold several stages of one campaign in order.
        self.apply(self.reorder([{"together": ["TEST1", "OTHER1", "TEST2", "OTHER2"]}], ["A", "B", "TEST", "OTHER"]))

    def test_qualification_stage_stays_out_of_together_groups(self):
        self.rows_fixture()
        self.refuses(self.reorder([{"together": ["TEST3", "OTHER1"]}], ["A", "B", "TEST", "OTHER"]), "Qualification stage TEST3")
        self.apply(self.reorder(["TEST3"], ["A", "B", "TEST", "OTHER"]))

    def test_stage_row_must_be_a_live_stage(self):
        self.rows_fixture()
        self.apply(self.complete("TEST1"))
        self.refuses(self.reorder(["TEST1"], ["A", "B", "TEST", "OTHER"]), "absent, finished or not a stage")

    def test_finished_items_leave_rows_and_groups_and_stage_rows_are_never_added(self):
        live = [self.item("A"), self.item("B", status="planned"), self.item("C", status="completed"),
                self.item("CAMP", "campaign"), self.item("S1", "stage", parent_id="CAMP"),
                self.item("S2", "stage", status="completed", parent_id="CAMP"),
                self.item("S3", "stage", status="planned", parent_id="CAMP")]
        saved = {"profile": work.CONFIG, "current": [{"together": ["A", "C", "S2"]}, {"together": ["B", "S1", "GONE"]}], "next": ["S3"]}
        # Finished and absent members drop out; a group left with one item is a plain row; the new live campaign is appended.
        self.assertEqual(work.ordered(saved, live), {"profile": work.CONFIG,
            "current": ["A", {"together": ["B", "S1"]}, "CAMP"], "next": ["S3"]})
        self.assertEqual(work.ordered(None, live), {"profile": work.CONFIG, "current": ["A", "CAMP"], "next": ["B"]})

    def test_together_rows_render_in_generated_work_order(self):
        self.rows_fixture()
        self.apply(self.reorder(["TEST1", {"together": ["A", "OTHER1", "OTHER2"]}], ["B", "TEST", "OTHER"]))
        text = (self.root / work.ORDER).read_text(encoding="utf-8")
        self.assertIn("Owner-only actions", text)
        self.assertIn("Saved priority only: agents start work when the owner assigns it", text)
        self.assertIn("needs no further per-stage approval", text)
        self.assertNotIn("does not authorize dispatch", text)
        self.assertIn("| 1 | [TEST1 · TEST1 title](campaigns/TEST.md) · stage of TEST title | active |", text)
        self.assertIn("| 2 together | [A · A title](WORK_ITEMS.md) (active) · [OTHER1 · OTHER1 title](campaigns/TEST.md) (OTHER title, planned)", text)
        self.assertIn("stages of one campaign still run one after another (OTHER title: OTHER1, OTHER2)", text)
        self.assertIn("| active; planned; planned |", text)

    def test_lane_move_removes_the_item_from_a_group(self):
        self.rows_fixture()
        self.apply(self.reorder([{"together": ["A", "B"]}, "TEST1"], ["TEST", "OTHER"]))
        self.apply(self.request("A", "update", lane="next"))
        saved = json.loads((self.root / work.PATH).read_bytes())
        self.assertEqual((saved["current"], saved["next"]), (["B", "TEST1"], ["TEST", "OTHER", "A"]))

    def test_current_row_summary_names_items_campaigns_and_admission(self):
        self.rows_fixture()
        self.apply(self.reorder([{"together": ["A", "OTHER1", "OTHER2"]}, "TEST1"], ["B", "TEST", "OTHER"]))
        projected = boards.project(self.root)
        summary = work.row_summary(projected["work_order"], projected["items"])
        self.assertTrue(summary["together"])
        self.assertEqual([i["id"] for i in summary["items"]], ["A", "OTHER1", "OTHER2"])
        self.assertEqual(summary["items"][1]["campaign"], "OTHER title")
        self.assertNotIn("campaign", summary["items"][0])
        self.assertIn("agents start work when the owner assigns it", summary["admission"])
        self.assertIn("restarts, releases", summary["admission"])
        self.assertIsNone(work.row_summary({"rows": {"current": []}}, projected["items"]))

    def test_progress_59_and_unknown_and_100_do_not_complete(self):
        for value in (None, 0, 59, 100):
            self.apply(self.request(progress={"percent": value, "basis": "Unequal effort remaining."}))
            self.assertEqual(self.row("A")["extensions"]["progress"]["percent"], value)
            self.assertEqual(self.row("A")["status"], "active")
        for value in (-1, 101, True, "59", 1.5):
            with self.assertRaises(ts.ReadError): work.plan(self.root, self.request(progress={"percent": value, "basis": "Estimate."}))

    def test_complete_files_done_and_derives_changelog_once(self):
        request = self.complete()
        self.apply(request)
        result = write_retry.submit_work(self.host, self.root, "writer", request)
        self.assertEqual(result["outcome"], "complete", result)
        self.assertEqual(self.row("A")["work_lane"], "done")
        self.assertNotIn("A · A title", (self.root / work.CURRENT).read_text(encoding="utf-8"))
        self.assertIn("A · A title", (self.root / work.DONE).read_text(encoding="utf-8"))
        self.assertIn("Keep legacy prose.", (self.root / "docs/CHANGELOG.md").read_text(encoding="utf-8"))
        history = changes(self.root)
        self.assertEqual(history["status"], "available", history)
        self.assertEqual(len(history["items"]), 1)
        self.assertEqual(history["items"][0]["summary"], "Checked outcome.")

    def test_submit_claims_and_retires_only_its_lifecycle_paths(self):
        """A semantic submission must not need a retained board-wide grant."""
        (self.root / "docs/host-admission.md").write_text("Host admission fixture.\n", encoding="utf-8")
        isolated_host = Path(self.temp.name) / "isolated-host"
        enroll(self.root, isolated_host, "writer", ["docs/host-admission.md"], shared=True)
        result = write_retry.submit_work(isolated_host, self.root, "writer", self.complete())
        self.assertEqual((result["outcome"], result["result"]["readback"]), ("complete", "verified"), result)
        coordinator, authority = connect(self.root, isolated_host, "writer")
        self.assertEqual(coordinator.inspect("work-lifecycle-" + result["request_id"])["reservations"], [])
        self.assertEqual(self.row("A")["status"], "completed")

    def test_atomic_batch_completes_two_items_with_one_effect(self):
        (self.root / "docs/batch-bootstrap.md").write_text("Host only.\n", encoding="utf-8")
        isolated = Path(self.temp.name) / "batch-host"
        enroll(self.root, isolated, "writer", ["docs/batch-bootstrap.md"], shared=True)
        batch = {"profile": write_batch.PROFILE, "operations": [
            {"kind": "work", "request": self.complete("A")},
            {"kind": "work", "request": self.complete("B")} ]}
        result = write_retry.submit_batch(isolated, self.root, "writer", batch)
        self.assertEqual(result["outcome"], "complete", result)
        self.assertEqual((self.row("A")["status"], self.row("B")["status"]), ("completed", "completed"))
        receipt = result["result"]
        coordinator, _ = connect(self.root, isolated, "writer")
        with coordinator._db() as db:
            effects = db.execute("SELECT COUNT(*) FROM effects WHERE attempt=?", (receipt["attempt"],)).fetchone()[0]
        self.assertEqual(effects, 1)
        self.assertEqual(coordinator.inspect(receipt["attempt"])["reservations"], [])

    def test_batch_mixes_add_and_update_and_separates_claimed_from_observed_identity(self):
        (self.root / "docs/batch-bootstrap.md").write_text("Host only.\n", encoding="utf-8")
        isolated = Path(self.temp.name) / "mixed-batch-host"
        enroll(self.root, isolated, "cli-writer", ["docs/batch-bootstrap.md"], shared=True)
        batch = {"profile": write_batch.PROFILE,
                 "requested_by": {"kind": "model", "value": "terra-reviewer"},
                 "operations": [
                     {"kind": "add", "request": {"owner_path": self.owner,
                      "expected_sha256": digest((self.root / self.owner).read_bytes()), "index": 2,
                      "item": self.item("C", status="planned")}},
                     {"kind": "update", "request": {"owner_path": self.owner,
                      "expected_sha256": digest((self.root / self.owner).read_bytes()), "item_id": "B",
                      "changes": {"summary": "Updated in the same request."}}}]}
        result = write_retry.submit_batch(isolated, self.root, "cli-writer", batch)
        self.assertEqual(result["outcome"], "complete", result)
        receipt = result["result"]
        self.assertEqual(receipt["requested_by"], {"kind": "model", "value": "terra-reviewer"})
        self.assertEqual(receipt["observed_by"]["actor"], "cli-writer")
        self.assertEqual(self.row("B")["summary"], "Updated in the same request.")
        self.assertEqual(self.row("C")["status"], "planned")

    def test_batch_rejects_one_stale_operation_before_any_write(self):
        batch = {"profile": write_batch.PROFILE, "operations": [
            {"kind": "work", "request": self.complete("A")},
            {"kind": "work", "request": self.complete("B")} ]}
        self.apply(self.request("A", changes={"summary": "Newer decision."}))
        before_b = self.row("B")["revision"]
        with self.assertRaises(ts.ReadError):
            write_batch.plan(self.root, batch)
        self.assertEqual(self.row("B")["revision"], before_b)

    def test_stage_completion_does_not_complete_campaign(self):
        self.apply(self.complete("TEST1"))
        self.assertEqual(self.row("TEST")["status"], "active")
        with self.assertRaises(ts.ReadError): work.plan(self.root, self.complete("TEST", acceptance_confirmed=True))
        self.apply(self.complete("TEST2"))
        with self.assertRaises(ts.ReadError): work.plan(self.root, self.complete("TEST"))
        self.apply(self.complete("TEST", acceptance_confirmed=True))
        self.assertEqual(self.row("TEST")["owner_path"], "docs/campaigns/done/TEST/README.md")
        self.assertIn("Campaign moved", (self.root / self.campaign).read_text(encoding="utf-8"))
        self.assertIn("](../../../reference.md)", (self.root / self.row("TEST")["owner_path"]).read_text(encoding="utf-8"))

    def test_cancel_supersede_archive_restore_preserve_evidence(self):
        self.apply(self.request(operation="supersede", reason="Replaced by B.", superseded_by="B"))
        self.assertEqual(self.row("A")["status"], "superseded")
        self.apply(self.request(operation="archive"))
        self.apply(self.request(operation="restore"))
        self.assertEqual(self.row("A")["status"], "planned")
        self.assertEqual(len(self.row("A")["extensions"]["work_history"]), 1)
        self.apply(self.request(operation="cancel", reason="No longer needed."))
        self.assertEqual(self.row("A")["work_lane"], "done")

    def test_campaign_cancel_and_supersede_cascade_open_stages(self):
        self.apply(self.complete("TEST1"))
        self.apply(self.request("TEST", operation="cancel", reason="Replaced by the runtime work.", date="2026-10-08"))
        self.assertEqual(self.row("TEST1")["status"], "completed")
        self.assertEqual(self.row("TEST2")["status"], "cancelled")
        child_event = self.row("TEST2")["extensions"]["work_history"][-1]
        self.assertEqual((child_event["date"], child_event["why"]), ("2026-10-08", "Replaced by the runtime work."))

        self.apply(self.request("TEST", operation="restore"))
        self.apply(self.request("TEST2", operation="restore"))
        self.apply(self.request("TEST", operation="supersede", reason="Use B instead.", superseded_by="B", date="2026-10-08"))
        self.assertEqual(self.row("TEST2")["status"], "superseded")
        self.assertEqual(self.row("TEST2")["extensions"]["superseded_by"], "B")

    def test_campaign_cannot_supersede_itself_with_a_cascaded_stage(self):
        before = (self.root / self.campaign).read_bytes()
        for successor in ("TEST1", "TEST2"):
            with self.subTest(successor=successor), self.assertRaisesRegex(ts.ReadError, "child retired"):
                work.plan(self.root, self.request("TEST", operation="supersede",
                    reason="Use the next stage.", superseded_by=successor))
        self.assertEqual((self.root / self.campaign).read_bytes(), before)
        self.assertEqual(self.row("TEST")["status"], "active")

    def test_delete_stage_records_parent_history_and_preserves_order(self):
        stage = self.row("TEST2")
        parent = self.row("TEST")
        request = {"operation": "delete-stage", "item_id": stage["id"],
                   "expected_revision": stage["revision"], "expected_parent_revision": parent["revision"],
                   "why": "The stage was folded into TEST1.", "date": "2026-10-08"}
        self.apply(request)
        ids = [row["id"] for row in boards.project(self.root)["items"]]
        self.assertNotIn("TEST2", ids)
        self.assertLess(ids.index("TEST"), ids.index("TEST1"))
        event = self.row("TEST")["extensions"]["work_history"][-1]
        self.assertEqual(event["deleted_stage"], {"id": "TEST2", "title": "TEST2 title"})
        self.assertEqual((event["why"], event["date"]), ("The stage was folded into TEST1.", "2026-10-08"))

    def test_delete_stage_refuses_completed_referenced_and_stale_parent(self):
        self.apply(self.complete("TEST1"))
        completed = self.row("TEST1")
        with self.assertRaises(ts.ReadError):
            work.plan(self.root, {"operation": "delete-stage", "item_id": "TEST1",
                "expected_revision": completed["revision"], "expected_parent_revision": self.row("TEST")["revision"], "why": "Remove."})

        board, _ = boards._parse((self.root / self.owner).read_bytes(), self.owner)
        next(row for row in board["items"] if row["id"] == "A").setdefault("extensions", {})["stage_id"] = "TEST2"
        self.write_board(self.owner, board["items"])
        stage, parent = self.row("TEST2"), self.row("TEST")
        with self.assertRaisesRegex(ts.ReadError, "references this stage"):
            work.plan(self.root, {"operation": "delete-stage", "item_id": "TEST2",
                "expected_revision": stage["revision"], "expected_parent_revision": parent["revision"], "why": "Remove."})

        board, _ = boards._parse((self.root / self.owner).read_bytes(), self.owner)
        next(row for row in board["items"] if row["id"] == "A")["extensions"].pop("stage_id")
        self.write_board(self.owner, board["items"])
        stale = {"operation": "delete-stage", "item_id": "TEST2", "expected_revision": self.row("TEST2")["revision"],
                 "expected_parent_revision": self.row("TEST")["revision"], "why": "Remove."}
        self.apply(self.request("TEST", changes={"summary": "Concurrent campaign decision."}))
        with self.assertRaisesRegex(ts.ReadError, "Parent campaign changed"):
            work.plan(self.root, stale)

        stage, parent = self.row("TEST2"), self.row("TEST")
        with self.assertRaisesRegex(ts.ReadError, "nonblank reason"):
            work.plan(self.root, {"operation": "delete-stage", "item_id": "TEST2",
                "expected_revision": stage["revision"], "expected_parent_revision": parent["revision"], "why": "   "})

    def test_closeout_cancel_requires_specific_reason(self):
        context = {"root": self.root, "work_id": "A"}
        row = work_closeout.lifecycle_assessment({}, context)["example"]["work_closeout"][0]
        row["status"] = "cancelled"
        missing = work_closeout.lifecycle_assessment({"work_closeout": [row]}, context)
        self.assertEqual(missing["outcome"], "incomplete")
        self.assertTrue(any("cancellation_reason" in error for error in missing["errors"]))
        row["cancellation_reason"] = "Owner ended this work."
        assessed = work_closeout.lifecycle_assessment({"work_closeout": [row]}, context)
        self.assertEqual(assessed["outcome"], "verified", assessed)
        self.assertEqual((assessed["requests"][0]["operation"], assessed["requests"][0]["why"]),
                         ("cancel", "Owner ended this work."))

    def mixed_campaign(self):
        self.apply(self.complete("TEST1")); self.apply(self.complete("TEST2"))
        board, _ = boards._parse((self.root / self.campaign).read_bytes(), self.campaign)
        self.write_board(self.campaign, [*board["items"], self.item("UNRELATED", status="active")])
        return {row["id"]: row for row in board["items"] if row["id"] != "TEST"} | {"UNRELATED": self.item("UNRELATED", status="active")}

    def test_mixed_owner_completion_defers_move_and_preserves_unrelated_records(self):
        unrelated = self.mixed_campaign()
        config_before = (self.root / "eutonos.boards.json").read_bytes()
        other_owner_before = (self.root / self.owner).read_bytes()
        request = self.complete("TEST", acceptance_confirmed=True, defer_owner_move=True)
        plan = work.plan(self.root, request)
        self.assertEqual(plan["owner_filing"]["state"], "deferred")
        self.assertIn("only by a verified write receipt", plan["owner_filing"]["message"])
        self.assertEqual(self.row("TEST")["status"], "active")  # Planning is not filing.
        result = self.apply(request)
        self.assertEqual(result["result"]["readback"], "verified")
        self.assertEqual(result["result"]["owner_filing"]["owner_path"], self.campaign)
        campaign = self.row("TEST")
        self.assertEqual((campaign["status"], campaign["owner_path"], campaign["work_lane"]), ("completed", self.campaign, "done"))
        self.assertIs(campaign["extensions"]["owner_move_deferred"], True)
        self.assertEqual(campaign["extensions"]["progress"]["percent"], 100)
        self.assertEqual(len(campaign["extensions"]["work_history"]), 1)
        after, _ = boards._parse((self.root / self.campaign).read_bytes(), self.campaign)
        self.assertEqual({row["id"]: row for row in after["items"] if row["id"] != "TEST"}, unrelated)
        self.assertEqual((self.root / "eutonos.boards.json").read_bytes(), config_before)
        self.assertEqual((self.root / self.owner).read_bytes(), other_owner_before)
        self.assertIn("Keep prose.", (self.root / self.campaign).read_text())
        self.assertFalse((self.root / "docs/campaigns/done/TEST/README.md").exists())
        self.assertIn("UNRELATED", boards.project(self.root)["work_order"]["current"])
        self.assertNotIn("TEST", boards.project(self.root)["work_order"]["current"])
        before = (self.root / self.campaign).read_bytes()
        for operation in ("organize-campaign", "archive"):
            with self.assertRaisesRegex(ts.ReadError, "dedicated owner"):
                work.plan(self.root, self.request("TEST", operation=operation))
        self.assertEqual((self.root / self.campaign).read_bytes(), before)

    def test_mixed_owner_cancellation_defers_move_and_preserves_unrelated_records(self):
        unrelated = self.mixed_campaign()
        request = self.request("TEST", operation="cancel", defer_owner_move=True,
                               reason="Replaced by the current campaign.")
        plan = work.plan(self.root, request)
        self.assertEqual(plan["owner_filing"]["state"], "deferred")
        result = self.apply(request)
        self.assertEqual(result["result"]["readback"], "verified")
        campaign = self.row("TEST")
        self.assertEqual((campaign["status"], campaign["owner_path"], campaign["work_lane"]),
                         ("cancelled", self.campaign, "done"))
        self.assertIs(campaign["extensions"]["owner_move_deferred"], True)
        after, _ = boards._parse((self.root / self.campaign).read_bytes(), self.campaign)
        self.assertEqual({row["id"]: row for row in after["items"] if row["id"] != "TEST"}, unrelated)
        self.assertFalse((self.root / "docs/campaigns/done/TEST/README.md").exists())

    def test_restore_deferred_mixed_owner_preserves_records_and_history(self):
        unrelated = self.mixed_campaign()
        self.apply(self.complete("TEST", acceptance_confirmed=True, defer_owner_move=True))
        history = self.row("TEST")["extensions"]["work_history"]
        config = (self.root / "eutonos.boards.json").read_bytes()
        self.apply(self.request("TEST", operation="restore"))
        restored = self.row("TEST")
        self.assertEqual((restored["status"], restored["owner_path"]), ("planned", self.campaign))
        self.assertEqual(restored["extensions"]["work_history"], history)
        self.assertIsNone(restored["extensions"]["progress"]["percent"])
        after, _ = boards._parse((self.root / self.campaign).read_bytes(), self.campaign)
        self.assertEqual({row["id"]: row for row in after["items"] if row["id"] != "TEST"}, unrelated)
        self.assertEqual((self.root / "eutonos.boards.json").read_bytes(), config)
        self.assertIn("TEST", boards.project(self.root)["work_order"]["next"])

    def test_mixed_owner_default_and_explicit_false_still_refuse_move(self):
        self.mixed_campaign()
        before = (self.root / self.campaign).read_bytes()
        for extra in ({}, {"defer_owner_move": False}):
            with self.assertRaisesRegex(ts.ReadError, "dedicated owner"):
                work.plan(self.root, self.complete("TEST", acceptance_confirmed=True, **extra))
        self.assertEqual((self.root / self.campaign).read_bytes(), before)
        self.assertEqual(self.row("TEST")["status"], "active")

    def test_owner_move_deferral_does_not_bypass_completion_or_usage_guards(self):
        with self.assertRaisesRegex(ts.ReadError, "unfinished stages"):
            work.plan(self.root, self.complete("TEST", acceptance_confirmed=True, defer_owner_move=True))
        self.apply(self.complete("TEST1")); self.apply(self.complete("TEST2"))
        with self.assertRaisesRegex(ts.ReadError, "acceptance_confirmed"):
            work.plan(self.root, self.complete("TEST", defer_owner_move=True))
        for value in (None, "true", 1, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ts.ReadError, "boolean"):
                work.plan(self.root, self.complete("TEST", acceptance_confirmed=True, defer_owner_move=value))
        for identity, operation in (("A", "complete"), ("TEST1", "complete"), ("TEST", "update"), ("TEST", "organize-campaign")):
            with self.subTest(identity=identity, operation=operation), self.assertRaisesRegex(ts.ReadError, "terminal campaign decisions"):
                work.plan(self.root, self.request(identity, operation=operation, defer_owner_move=True))

    def test_successful_later_owner_move_removes_only_deferred_marker(self):
        self.apply(self.complete("TEST1")); self.apply(self.complete("TEST2"))
        self.apply(self.complete("TEST", acceptance_confirmed=True, defer_owner_move=True))
        history = self.row("TEST")["extensions"]["work_history"]
        self.apply(self.request("TEST", operation="organize-campaign"))
        filed = self.row("TEST")
        self.assertEqual(filed["owner_path"], "docs/campaigns/done/TEST/README.md")
        self.assertNotIn("owner_move_deferred", filed["extensions"])
        self.assertEqual(filed["extensions"]["work_history"], history)
        self.assertEqual(filed["status"], "completed")

    def test_guided_native_prepare_and_batch_apply_preserve_mixed_owner(self):
        self.mixed_campaign()
        context = {"root": str(self.root), "host": str(self.host), "actor": "writer", "work_id": "TEST", "session_id": "fixture"}
        row = work_closeout.lifecycle_assessment({}, context)["example"]["work_closeout"][0]
        row.update(status="completed", acceptance_confirmed=True, defer_owner_move=True,
                   progress={"percent": 100, "basis": "All stages terminal and accepted."})
        values = {"catalog_changes": [], "work_changes": [], "work_closeout": [row]}
        prepared = closeout_route.prepare(values, context)
        self.assertEqual(prepared["outcome"], "prepared", prepared)
        self.assertIs(prepared["lifecycle_assessment"]["requests"][0]["defer_owner_move"], True)
        self.assertEqual(self.row("TEST")["status"], "active")
        renew(self.root, self.host, "writer", prepared["allowed_paths"], shared=True)
        with mock.patch("eutonos.closeout_route.prepare_memory", return_value={"profile": "eutonos.tokenslang.workflow.v1", "outcome": "skipped", "reason": "ram-capture-off"}):
            result = closeout_route.run(prepared, context)
        self.assertEqual(result["outcome"], "completed", result)
        self.assertEqual(result["lifecycle"][0]["result"]["readback"], "verified")
        self.assertEqual((self.row("TEST")["status"], self.row("TEST")["owner_path"]), ("completed", self.campaign))
        self.assertIs(self.row("TEST")["extensions"]["owner_move_deferred"], True)
        self.assertEqual(self.row("UNRELATED")["status"], "active")
        for identity, status, flag in (("TEST", "active", True), ("A", "completed", True), ("TEST", "completed", "true")):
            invalid = {**row, "item_id": identity, "status": status, "defer_owner_move": flag}
            self.assertEqual(work_closeout.lifecycle_assessment({"work_closeout": [invalid]}, context)["outcome"], "incomplete")

    def test_guided_terminal_campaign_deferral_plans_and_files_cancellation(self):
        self.mixed_campaign()
        context = {"root": str(self.root), "host": str(self.host), "actor": "writer", "work_id": "TEST", "session_id": "fixture"}
        row = work_closeout.lifecycle_assessment({}, context)["example"]["work_closeout"][0]
        row.update(status="cancelled", cancellation_reason="Owner stopped this campaign.", defer_owner_move=True)
        values = {"catalog_changes": [], "work_changes": [], "work_closeout": [row]}
        prepared = closeout_route.prepare(values, context)
        self.assertEqual(prepared["outcome"], "prepared", prepared)
        self.assertEqual(prepared["lifecycle_assessment"]["requests"][0]["operation"], "cancel")
        self.assertEqual(prepared["lifecycle_batch"]["request"]["operations"][0]["request"]["defer_owner_move"], True)
        superseded = {**row, "status": "superseded", "superseded_by": "B"}
        other = closeout_route.prepare({**values, "work_closeout": [superseded]}, context)
        self.assertEqual(other["outcome"], "prepared", other)
        renew(self.root, self.host, "writer", prepared["allowed_paths"], shared=True)
        with mock.patch("eutonos.closeout_route.prepare_memory", return_value={"profile": "eutonos.tokenslang.workflow.v1", "outcome": "skipped", "reason": "fixture-only"}):
            result = closeout_route.run(prepared, context)
        self.assertEqual(result["outcome"], "completed", result)
        self.assertEqual((self.row("TEST")["status"], self.row("TEST")["owner_path"]), ("cancelled", self.campaign))
        self.assertIs(self.row("TEST")["extensions"]["owner_move_deferred"], True)
        self.assertEqual(self.row("UNRELATED")["status"], "active")

    def test_same_item_stale_request_is_rejected(self):
        stale = self.complete()
        self.apply(self.request(changes={"summary": "New scope."}))
        with self.assertRaises(ts.ReadError): work.plan(self.root, stale)

    def test_unrelated_edit_is_rebased(self):
        request = self.complete()
        self.apply(self.request("B", changes={"summary": "Concurrent change."}))
        self.apply(request)
        self.assertEqual(self.row("B")["summary"], "Concurrent change.")

    def test_busy_write_is_durable_and_retries(self):
        request = self.complete()
        prepared = work.plan(self.root, request)
        (renew if self.host.exists() else enroll)(self.root, self.host, "writer", prepared["paths"], shared=True)
        coordinator, authority = connect(self.root, self.host, "writer")
        generation = coordinator.start(authority, "blocker")
        coordinator.reserve(authority, "blocker", generation, [self.owner])
        with mock.patch.object(write_retry, "start_worker", return_value={"outcome": "not-started"}):
            queued = write_retry.submit_work(self.host, self.root, "writer", request)
        self.assertEqual(queued["outcome"], "queued", queued)
        coordinator.closeout(authority, "blocker", generation, {self.owner: digest(authority.root.read(self.owner))}, {"completed": "No changes.", "next": "Retry.", "pending": None})
        with mock.patch.object(write_retry, "_now", return_value=write_retry._now() + 120):
            result = write_retry.run_once(self.host, self.root)
        self.assertEqual(result["outcome"], "complete", result)
        self.assertEqual(self.row("A")["status"], "completed")

    def test_closeout_asks_for_percent_and_records_checkpoint(self):
        result = work_closeout.lifecycle_assessment({}, {"root": self.root, "work_id": "A"})
        self.assertEqual(result["missing_fields"], ["work_closeout"])
        row = result["example"]["work_closeout"][0]
        row.update(what="Built the reader.", why="Requested.", result="Parser ready.", checks=["3 cases passed."], progress={"percent": 59, "basis": "UI remains."})
        assessment = work_closeout.lifecycle_assessment({"work_closeout": [row]}, {"root": self.root, "work_id": "A"})
        self.assertEqual(assessment["outcome"], "verified", assessment)
        self.apply(assessment["requests"][0])
        self.assertEqual(self.row("A")["extensions"]["checkpoint"]["result"], "Parser ready.")

    def test_routine_board_add_updates_live_views(self):
        plan = boards.plan_add(self.root, {"profile": boards.ADD, "owner_path": self.owner,
            "expected_sha256": digest((self.root / self.owner).read_bytes()), "index": 2,
            "item": self.item("NEW")})
        self.assertIn(work.CURRENT, plan["paths"])
        expected = next(decode(c["after"]) for c in plan["effect"]["changes"] if c["path"] == work.CURRENT)
        self.assertIn(b"NEW", expected)

    def test_stage_closeout_requires_a_separate_campaign_estimate(self):
        context = {"root": self.root, "work_id": "TEST1"}
        rows = work_closeout.lifecycle_assessment({}, context)["example"]["work_closeout"]
        self.assertEqual([r["item_id"] for r in rows], ["TEST1", "TEST"])
        rows[0].update(status="completed", progress={"percent": 100, "basis": "Stage checked."})
        rows[1]["progress"] = {"percent": 59, "basis": "The remaining stage has more work."}
        incomplete = work_closeout.lifecycle_assessment({"work_closeout": rows[:1]}, context)
        self.assertIn("campaign progress for TEST", incomplete["missing_fields"])
        assessment = work_closeout.lifecycle_assessment({"work_closeout": rows}, context)
        self.assertEqual(assessment["outcome"], "verified", assessment)
        for request in assessment["requests"]: self.apply(request)
        self.assertEqual(self.row("TEST1")["status"], "completed")
        self.assertEqual(self.row("TEST")["status"], "active")
        self.assertEqual(self.row("TEST")["extensions"]["progress"]["percent"], 59)

    def test_campaign_filing_updates_native_routes(self):
        for name in ("eutonos.ram.json", "eutonos.read.json", "docs/REGISTRY.json"):
            (self.root / name).write_text(json.dumps({"path": self.campaign}))
        self.apply(self.request("TEST", operation="organize-campaign"))
        new = self.row("TEST")["owner_path"]
        for name in ("eutonos.ram.json", "eutonos.read.json", "docs/REGISTRY.json"):
            self.assertEqual(json.loads((self.root / name).read_text(encoding="utf-8"))["path"], new)
        self.assertEqual(self.row("TEST1")["owner_path"], new)

    def test_crash_after_apply_recovers_and_releases(self):
        request = self.complete()
        prepared = work.plan(self.root, request)
        (renew if self.host.exists() else enroll)(self.root, self.host, "writer", prepared["paths"], shared=True)
        real = work.apply_prepared
        def interrupted(root, host, actor, plan, identity):
            coordinator, authority = connect(root, host, actor)
            attempt = "work-lifecycle-" + identity
            generation = coordinator.start(authority, attempt)
            coordinator.reserve(authority, attempt, generation, plan["paths"])
            selected_apply(coordinator, authority, attempt, generation, attempt, plan["effect"], plan["fingerprint"])
            raise OSError("simulated exit after apply")
        with mock.patch.object(work, "apply_prepared", side_effect=interrupted):
            failed = write_retry.submit_work(self.host, self.root, "writer", request)
        self.assertEqual(failed["outcome"], "error", failed)
        recovered = write_retry.submit_work(self.host, self.root, "writer", request)
        self.assertEqual(recovered["outcome"], "complete", recovered)
        self.assertEqual(len(self.row("A")["extensions"]["work_history"]), 1)
        coordinator, _ = connect(self.root, self.host, "writer")
        state = coordinator.inspect("work-lifecycle-" + recovered["request_id"])
        self.assertEqual(state["reservations"], [])

    def test_queued_same_item_change_requires_attention(self):
        request = self.complete()
        prepared = work.plan(self.root, request)
        renew(self.root, self.host, "writer", prepared["paths"], shared=True)
        # Retain the request as if its owner were busy, then make a newer decision.
        queued = write_retry.enqueue(self.host, self.root, "writer", "work-lifecycle", {"request": request})
        newer = work.plan(self.root, self.request(changes={"summary": "New scope must survive."}))
        work.apply_prepared(self.root, self.host, "writer", newer, "newer-decision")
        result = write_retry.run_once(self.host, self.root)
        self.assertEqual(result["outcome"], "failed", result)
        self.assertEqual(self.row("A")["summary"], "New scope must survive.")
        self.assertEqual(self.row("A")["status"], "active")

    def test_campaign_can_return_from_done_without_overwriting_other_files(self):
        (self.root / "docs/REGISTRY.json").write_text(json.dumps({"documents": [
            {"id": "campaign-test", "path": self.campaign, "role": "campaign"}]}))
        self.apply(self.request("TEST", operation="organize-campaign"))
        self.apply(self.complete("TEST1")); self.apply(self.complete("TEST2"))
        self.apply(self.complete("TEST", acceptance_confirmed=True))
        self.apply(self.request("TEST", operation="restore"))
        self.assertEqual(self.row("TEST")["owner_path"], "docs/campaigns/TEST/README.md")
        self.assertEqual(self.row("TEST")["status"], "planned")
        records = json.loads((self.root / "docs/REGISTRY.json").read_text(encoding="utf-8"))["documents"]
        self.assertEqual(len({r["path"] for r in records}), len(records))
        self.assertEqual(next(r["path"] for r in records if r["id"] == "campaign-test"), "docs/campaigns/TEST/README.md")

    def test_campaign_restore_preserves_notes_added_to_redirect(self):
        self.apply(self.request("TEST", operation="organize-campaign"))
        self.apply(self.complete("TEST1")); self.apply(self.complete("TEST2"))
        self.apply(self.complete("TEST", acceptance_confirmed=True))
        old = self.root / "docs/campaigns/TEST/README.md"
        original = old.read_bytes() + b"\nA human left a note here.\n"
        old.write_bytes(original)
        with self.assertRaises(ts.ReadError):
            work.plan(self.root, self.request("TEST", operation="restore"))
        self.assertEqual(old.read_bytes(), original)
        self.assertEqual(self.row("TEST")["status"], "completed")

    def test_cli_closeout_prepare_replay_files_and_reuses_answers(self):
        context = {"root": str(self.root), "host": str(self.host), "actor": "writer", "work_id": "A", "session_id": "fixture"}
        row = work_closeout.lifecycle_assessment({}, context)["example"]["work_closeout"][0]
        row.update(status="completed", what="Implemented.", why="Requested.", result="Works.", checks=["Verified."], progress={"percent": 100, "basis": "Done."})
        values = {"catalog_changes": [], "work_changes": [], "work_closeout": [row]}
        prepared = closeout_route.prepare(values, context)
        self.assertEqual(prepared["outcome"], "prepared", prepared)
        renew(self.root, self.host, "writer", prepared["allowed_paths"], shared=True)
        # Optional memory is separate; no native capture in this fixture.
        with mock.patch("eutonos.closeout_route.prepare_memory", return_value={"profile": "eutonos.tokenslang.workflow.v1", "outcome": "skipped", "reason": "ram-capture-off"}):
            result = closeout_route.run(prepared, context)
        self.assertEqual(result["outcome"], "completed", result)
        self.assertEqual(self.row("A")["status"], "completed")

    def test_checked_lifecycle_closeout_waits_for_required_batch_then_reuses_code_identity(self):
        from eutonos.git_delivery import DeliveryError
        engine=self.delivery_config()
        (self.root/'docs/product.py').write_bytes(b'checked = True\n')
        context={'root':str(self.root),'host':str(self.host),'actor':'writer','work_id':'A',
                 'session_id':'actual-session','delivery':{'logical_id':'lifecycle-code','paths':['docs/product.py']}}
        row=work_closeout.lifecycle_assessment({},context)['example']['work_closeout'][0]
        row.update(status='completed',what='Implemented.',why='Requested.',result='Works.',checks=['Verified.'],
                   progress={'percent':100,'basis':'Done.'})
        values={'catalog_changes':[],'work_changes':[],'work_closeout':[row]}
        prepared=closeout_route.prepare(values,context)
        self.assertEqual(prepared['outcome'],'prepared',prepared)
        key=engine.status()[0]['id']
        with mock.patch('eutonos.write_retry.submit_batch',return_value={'outcome':'queued'}):
            queued=closeout_route.run(prepared,context)
        self.assertEqual(queued['outcome'],'deferred')
        engine.drain_verified()
        self.assertEqual(engine.run(key)['state'],'waiting-native')
        with self.assertRaises(DeliveryError):engine.resume(key)
        renew(self.root,self.host,'writer',prepared['allowed_paths'],shared=True)
        with mock.patch('eutonos.closeout_route.prepare_memory',return_value={'profile':'eutonos.tokenslang.workflow.v1','outcome':'error','message':'fixture RAM failure'}):
            attention=closeout_route.run(prepared,context)
        self.assertEqual(attention['outcome'],'attention-required')
        engine.drain_verified()
        self.assertEqual(engine.run(key)['state'],'waiting-native')
        with self.assertRaises(DeliveryError):engine.resume(key)
        with mock.patch('eutonos.closeout_route.prepare_memory',return_value={'profile':'eutonos.tokenslang.workflow.v1','outcome':'skipped','reason':'fixture-only'}):
            mismatch=closeout_route.run(prepared,{**context,'session_id':'another-session'})
            self.assertEqual(mismatch['git_delivery']['state'],'unavailable')
            self.assertEqual(engine.run(key)['state'],'waiting-native')
            landed=closeout_route.run(prepared,context)
        self.assertEqual(landed['outcome'],'completed',landed)
        self.assertEqual(landed['git_delivery']['state'],'committed',landed)
        self.assertEqual(self.row('A')['status'],'completed')
        self.assertEqual(len(engine.status()),2)
        for job in engine.status():report_bounds(self.root,'lifecycle-closeout-'+job['payload']['kind'],job['payload']['changes'],job['payload']['source_parent'])
        self.assertEqual(engine.status(key)[0]['payload']['changes'].keys(),{'docs/product.py'})

    def test_lifecycle_memory_prefers_supplied_request_and_actions(self):
        rows = [{"item_id": "A", "operation": "complete", "what": "Row what.", "why": "Row why.",
                 "result": "Row result.", "checks": ["Row check.", "Shared check."]}]
        supplied = {"request": "User asked for X.", "actions": ["Did Y.", "Did Z."], "why": "Because W.",
                    "result": "Observed V.", "checks": ["Shared check.", "Agent check."], "unresolved": ["U."]}
        values = work_closeout.lifecycle_memory_values(rows, supplied)
        self.assertEqual(values["request"], "User asked for X.")
        self.assertEqual(values["actions"], ["Did Y.", "Did Z."])
        self.assertEqual((values["why"], values["result"]), ("Because W.", "Observed V."))
        self.assertEqual(values["checks"], ["Shared check.", "Agent check.", "Row check."])
        self.assertEqual(values["unresolved"], ["U."])
        fallback = work_closeout.lifecycle_memory_values(rows, {"request": "  ", "actions": []})
        self.assertEqual((fallback["request"], fallback["actions"]), ("Row what.", ["Row what."]))
        self.assertEqual((fallback["why"], fallback["result"]), ("Row why.", "Row result."))
        self.assertEqual(fallback["checks"], ["Row check.", "Shared check."])


if __name__ == "__main__":
    unittest.main()
