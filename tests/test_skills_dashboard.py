"""Skills HTTP integration remains exact, read-only and project-scoped."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from urllib.parse import urlencode
from eutonos.dashboard_cli import app
from eutonos import skills

class SkillDashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); (self.root/'docs').mkdir()
        raw=b'# Test procedure\nRead the selected source.\n'
        (self.root/'docs/procedure.md').write_bytes(raw)
        self.sha=hashlib.sha256(raw).hexdigest()
        self.item={'id':'sample','title':'<script>unsafe</script>','description':'Release check','version':'1',
          'status':'usable','tools':['check'],'tags':['release'],'aliases':['ship'],
          'relationships':{'uses_tool':['check'],'applies_to':[],'depends_on':[],'verified_by':[],'supersedes':[]},'source':{'path':'docs/procedure.md','sha256':self.sha},
          'evidence':[]}
        (self.root/'eutonos.skills.json').write_text(json.dumps({'profile':'eutonos.tokenslang.skills.v1',
          'source_roots':['docs'],'excluded_roots':[],'skills':[self.item]}),encoding='utf-8')
        self.server=ThreadingHTTPServer(('127.0.0.1',0),app([self.root]))
        self.worker=threading.Thread(target=self.server.serve_forever,daemon=True);self.worker.start()
        self.addCleanup(self.stop)
    def stop(self):
        self.server.shutdown();self.server.server_close();self.worker.join()
    def fetch(self,path,values):
        with urlopen('http://127.0.0.1:'+str(self.server.server_port)+path+'?'+urlencode(values)) as r:
            return json.load(r)
    def search(self,**overrides):
        return self.fetch('/api/v1/skills/search',dict(project=0,query='ship',tool='',status='',offset=0,**overrides))
    def test_search_and_read_preserve_body_without_usage_write(self):
        before=list(self.root.rglob('*'))
        result=self.search();self.assertEqual(result['items'][0]['id'],'sample')
        value=self.fetch('/api/v1/skills/read',dict(project=0,id='sample',version='1',sha256=self.sha))
        self.assertIn('Read the selected source.',value['body'])
        self.assertEqual(before,list(self.root.rglob('*')))
    def test_natural_task_phrase_ignores_conversational_words(self):
        value=self.fetch('/api/v1/skills/search',dict(project=0,query='How do I ship this?',tool='',status='',offset=0))
        self.assertEqual([i['id'] for i in value['items']],['sample'])

    def test_drift_does_not_expand_new_body(self):
        (self.root/'docs/procedure.md').write_text('changed secret body',encoding='utf-8')
        value=self.fetch('/api/v1/skills/read',dict(project=0,id='sample',version='1',sha256=self.sha))
        self.assertNotIn('body',value)
    def test_arbitrary_path_and_wrong_project_rejected(self):
        for query in [dict(project=0,path='docs/procedure.md'),dict(project=5,id='sample',version='1',sha256=self.sha)]:
            with self.assertRaises(HTTPError) as cm:self.fetch('/api/v1/skills/read',query)
            self.assertEqual(cm.exception.code,400);cm.exception.close()
    def test_endpoint_has_no_usage_write(self):
        req=Request('http://127.0.0.1:'+str(self.server.server_port)+'/api/v1/skills/record-use',data=b'{}',method='POST')
        with self.assertRaises(HTTPError) as cm:urlopen(req)
        cm.exception.close()
        self.assertFalse((self.root/'.eutonos-local/skill-usage').exists())

if __name__=='__main__':unittest.main()
