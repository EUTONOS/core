"""Installed-wheel consumer proof: no checkout imports, network or model calls."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class InstalledWatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        builder = os.environ.get("EUTONOS_WHEEL_BUILD_PYTHON", sys.executable)
        if builder == sys.executable and importlib.util.find_spec("setuptools") is None:
            raise unittest.SkipTest("Offline wheel builder unavailable; set EUTONOS_WHEEL_BUILD_PYTHON")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.base = Path(cls.tmp.name)
        checkout = Path(__file__).resolve().parents[1]
        source = cls.base / "wheel-source"
        source.mkdir()
        shutil.copy2(checkout / "pyproject.toml", source / "pyproject.toml")
        shutil.copytree(checkout / "src" / "eutonos", source / "src" / "eutonos",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        wheels = cls.base / "wheels"
        wheels.mkdir()
        cls.run_command([builder, "-I", "-m", "pip", "--cache-dir", str(cls.base / "pip-cache"), "wheel", "--no-index", "--no-deps",
                         "--no-build-isolation", "--wheel-dir", str(wheels), str(source)])
        cls.run_command([sys.executable, "-I", "-m", "venv", str(cls.base / "consumer-runtime")])
        cls.python = cls.base / "consumer-runtime" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        cls.run_command([str(cls.python), "-I", "-m", "pip", "install", "--no-index", "--no-deps",
                         str(next(wheels.glob("eutonos-*.whl")))])
        # Consumer has no .git, tools/, src/ or repository configuration.
        cls.consumer = cls.base / "unrelated-consumer"
        cls.consumer.mkdir()

    @classmethod
    def run_command(cls, argv, **kwargs):
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=120, **kwargs)
        if proc.returncode:
            raise AssertionError(f"Offline consumer command failed ({proc.returncode}): {proc.stderr[-2000:]}")
        return proc.stdout

    def test_installed_module_tracks_addition_retirement_ack_and_unknown_source(self):
        script = r'''
import json, os
from pathlib import Path
from eutonos import model_watch as w, model_catalog as c
from eutonos import dashboard_cli
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.request import urlopen
home=Path.cwd()/'user-home'; home.mkdir()
os.environ['EUTONOS_HOME']=str(home/'eutonos')
os.environ['CODEX_HOME']=str(home/'codex')
cache=Path(os.environ['CODEX_HOME'])/'models_cache.json'; cache.parent.mkdir()
def write(ids):
    cache.write_text(json.dumps({'models':[{'slug':i,'visibility':'list',
        'supported_reasoning_levels':[{'effort':'high'}]} for i in ids]}))
write(['gpt-6-sol'])
first=w.scan(Path.cwd(),now=100000)
assert not first['changed']
write(['gpt-6-sol','gpt-6.1-sol'])
assert w.scan(Path.cwd(),now=100001)['changed']
assert c.resolve_choice('sol','codex',root=Path.cwd())['resolved']=='gpt-6.1-sol'
assert w.pending(Path.cwd())['count'] > 0
write(['gpt-6.1-sol'])
w.scan(Path.cwd(),now=100002)
assert c.describe_catalog(Path.cwd())['models']['gpt-6-sol']['available']
w.scan(Path.cwd(),now=100002+21600)
assert c.describe_catalog(Path.cwd())['models']['gpt-6-sol']['available'] is False
cache.write_text('{broken')
w.scan(Path.cwd(),now=100003+43200)
assert c.describe_catalog(Path.cwd())['models']['gpt-6.1-sol']['resolved']=='gpt-6.1-sol'
w.ack(Path.cwd(), ids=[item['id'] for item in w.pending(Path.cwd())['items']])
pending=w.pending(Path.cwd())
assert not pending['items']
assert 'site-packages' in str(Path(w.__file__))
assert not (Path.cwd()/'src').exists()
# The packaged dashboard exposes this same retained catalog without repo tools.
server=ThreadingHTTPServer(('127.0.0.1',0),dashboard_cli.app([Path.cwd()]))
Thread(target=server.serve_forever,daemon=True).start()
try:
    with urlopen(f'http://127.0.0.1:{server.server_port}/api/v1/model-watch/catalog?project=0',timeout=5) as response:
        projection=json.load(response)
    assert projection['model_catalog']['models']['gpt-6-sol']['available'] is False
    assert projection['model_catalog']['models']['sol']['resolved']=='gpt-6.1-sol'
    assert projection['model_watch']['pending_count']==0
finally:
    server.shutdown(); server.server_close()
print(json.dumps({'installed':True,'state':str(w.state_path(Path.cwd())),
    'catalog_source':c.describe_catalog(Path.cwd()).get('watch',{})}))
'''
        output = self.run_command([str(self.python), "-I", "-c", script], cwd=self.consumer)
        self.assertTrue(json.loads(output)["installed"])

    def test_installed_documentation_watch_keeps_catalog_and_review_separate(self):
        script = r'''
import json, os
from pathlib import Path
from eutonos import harness_docs_watch as docs, model_catalog as catalog

home = Path.cwd() / 'docs-user-home'
home.mkdir()
os.environ['EUTONOS_HOME'] = str(home / 'eutonos')
os.environ['CODEX_HOME'] = str(home / 'codex')
cache = Path(os.environ['CODEX_HOME']) / 'models_cache.json'
cache.parent.mkdir()
cache.write_text(json.dumps({'models': [{'slug': 'gpt-6.1-sol', 'visibility': 'list',
    'supported_reasoning_levels': [{'effort': 'high'}]}]}))
before = catalog.describe_catalog(Path.cwd())['models']
assert catalog.resolve_choice('sol', 'codex', root=Path.cwd())['resolved'] == 'gpt-6.1-sol'

pages = {url: 'A model option and agent configuration behavior apply to this release.'
         for url in docs.URLS}
def fetch(url, etag, last_modified):
    assert url in pages
    article = ('<html><main><article><h1>Harness docs</h1><p>' + pages[url]
               + '</p></article></main></html>')
    return {'status': 200, 'url': url, 'body': article.encode(), 'etag': '"v1"'}

root = Path.cwd()
assert docs.scan(root, now=1000, fetcher=fetch)['changed'] is False
assert docs.pending(root)['count'] == 0
target = docs.URLS[0]
pages[target] = 'A new model option and changed agent configuration behavior apply to this release.'
assert docs.scan(root, now=2000, fetcher=fetch)['changed'] is True
first = docs.pending(root)['items']
assert len(first) == 1 and first[0]['kind'] == 'changed' and first[0]['url'] == target
first_id = first[0]['id']
assert docs.scan(root, now=3000, fetcher=fetch)['changed'] is False
assert docs.pending(root)['items'][0]['id'] == first_id
assert docs.ack(root, ids=[first_id])['acknowledged'] == [first_id]
assert docs.pending(root)['count'] == 0
pages[target] = 'A third model option and another agent configuration behavior apply to this release.'
assert docs.scan(root, now=4000, fetcher=fetch)['changed'] is True
second_id = docs.pending(root)['items'][0]['id']
assert second_id != first_id
assert docs.ack(root, ids=[first_id])['acknowledged'] == []
assert docs.pending(root)['items'][0]['id'] == second_id
assert docs.pickup_hint(root)['pending_count'] == 1
assert docs.scan_if_due(root, now=4000, fetcher=fetch)['status'] == 'not-due'
assert catalog.describe_catalog(root)['models'] == before
assert catalog.resolve_choice('sol', 'codex', root=root)['resolved'] == 'gpt-6.1-sol'
assert 'site-packages' in str(Path(docs.__file__))
assert not (root / 'src').exists() and not (root / 'tools').exists()
assert docs.state_path(root).is_file()
print(json.dumps({'installed': True, 'pending': docs.pending(root)['count']}))
'''
        output = self.run_command([str(self.python), "-I", "-c", script], cwd=self.consumer)
        self.assertEqual(json.loads(output), {"installed": True, "pending": 1})


if __name__ == "__main__":
    unittest.main()
