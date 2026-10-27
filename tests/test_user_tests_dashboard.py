"""ETT3 native Tests projection and escaped read-only renderer."""

import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from eutonos import dashboard, user_tests


HASH = "a" * 64
REVISION = "b" * 40
SOURCE = Path(__file__).resolve().parents[1] / "src" / "eutonos" / "dashboard_static"


def render(module):
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("Node renderer unavailable")
    script = r"""
const fs=require('node:fs'),vm=require('node:vm');
const ctx=vm.createContext({esc:value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]))});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
process.stdout.write(ctx.renderProjectUserTests(JSON.parse(fs.readFileSync(0,'utf8'))));
"""
    process = subprocess.run([node, "-e", script, str(SOURCE / "user_tests.js")],
                             input=json.dumps(module), text=True, capture_output=True, timeout=15)
    if process.returncode:
        raise AssertionError(process.stderr)
    return process.stdout


def setup(setup_id, template, title, readiness="ready"):
    return user_tests.instantiate_setup(
        template, setup_id=setup_id, title=title, purpose="Check selected work & outcomes.",
        work_id="ETT3", readiness=readiness,
        sources=[{"path": "src/feature.py", "revision": REVISION, "sha256": HASH}]
        if readiness == "ready" else [],
        prerequisites=["A local fixture exists."], inputs=["Representative input."],
        steps=[{"id": "check", "action": "Use the feature.", "expected": "Expected state appears."}]
        if readiness == "ready" else [],
        criteria=[{"id": "visible", "description": "The expected state is visible."}]
        if readiness == "ready" else [], limits=["Local fixture only."])


def run(snapshot, run_id, outcome, time):
    observations = [] if outcome == "not-run" else [
        {"criterion_id": "visible", "outcome": outcome,
         "note": "Observed <script>unexpected</script> state."}]
    evidence = [] if outcome == "not-run" else [
        {"path": "evidence/result.txt", "sha256": HASH,
         "note": "Review <img src=x onerror=alert(1)> text."}]
    result = {"profile": user_tests.RUN_PROFILE, "id": run_id,
              "setup": copy.deepcopy(snapshot), "setup_id": snapshot["id"],
              "setup_sha256": user_tests.record_sha256(snapshot),
              "recorded_at": time, "outcome": outcome,
              "summary": "Observed the selected fixture.", "observations": observations,
              "evidence": {"profile": user_tests.EVIDENCE_PROFILE, "items": evidence},
              "limits": ["One local run."]}
    return user_tests.validate_run(result)


class UserTestsDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "eutonos.read.json").write_text(json.dumps({
            "repository_id": "fixture.repo", "scope_id": "fixture.scope",
            "namespace": "fixture.native"}), encoding="utf-8")

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_default_custom_failed_not_run_and_missing_result(self):
        default = setup("default", "behavior-regression", "Login <script>alert(1)</script>")
        custom = setup("custom", None, "Custom question")
        draft = setup("draft", None, "Unrun draft", readiness="draft")
        for item in (default, custom, draft):
            self.write(user_tests.setup_path(item["id"]), item)
        self.write(user_tests.run_path("default", "failed-1"),
                   run(default, "failed-1", "failed", "2026-09-28T12:00:00Z"))
        self.write(user_tests.run_path("custom", "not-run-1"),
                   run(custom, "not-run-1", "not-run", "2026-09-28T13:00:00Z"))
        module = dashboard.project(self.root)["modules"]["user_tests"]
        self.assertEqual(module["status"], "available")
        self.assertEqual(module["diagnostics"], [])
        self.assertEqual({item["setup"]["id"] for item in module["tests"]},
                         {"default", "custom", "draft"})
        html = render(module)
        for text in ("Your project tests", "Behavior or regression v1",
                     "Custom question", "Custom", "Not run", "No result recorded", "failed",
                     "Criterion observations", "Declared SHA-256", "Source pins", "ETT3",
                     "Ask your agent"):
            self.assertIn(text, html)
        self.assertIn("Login &lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("<img src=x onerror=alert(1)>", html)
        self.assertNotIn("href=", html)  # Evidence is text, never an arbitrary-file link.

    def test_earlier_pass_and_malformed_history_cannot_be_current_latest(self):
        old = setup("check", "user-workflow", "Workflow check")
        passed = run(old, "passed-1", "passed", "2026-09-28T10:00:00Z")
        current = copy.deepcopy(old)
        current["criteria"][0]["description"] = "A different expected state is visible."
        self.write(user_tests.setup_path("check"), current)
        self.write(user_tests.run_path("check", "passed-1"), passed)
        malformed = self.root / user_tests.run_path("check", "broken")
        malformed.write_text('{"profile":"bad"}', encoding="utf-8")
        module = dashboard.project(self.root)["modules"]["user_tests"]
        entry = module["tests"][0]
        self.assertFalse(entry["history_complete"])
        self.assertIsNone(entry["latest_run"])
        self.assertEqual(entry["stale_runs"][0]["record"]["outcome"], "passed")
        self.assertEqual(len(module["diagnostics"]), 1)
        html = render(module)
        self.assertIn("history incomplete", html)
        self.assertIn("Earlier setup", html)
        self.assertIn("does not assess the current criteria", html)
        self.assertIn("1 test record issue", html)
        self.assertIn("A different expected state", html)

    def test_empty_and_reader_error_states_and_packaged_asset(self):
        module = dashboard.project(self.root)["modules"]["user_tests"]
        self.assertEqual(module, {"status": "available", "tests": [], "diagnostics": []})
        self.assertIn("No project tests yet", render(module))
        error_html = render({"status": "error", "reason": "<unsafe>"})
        self.assertIn("&lt;unsafe&gt;", error_html)
        self.assertNotIn("<unsafe>", error_html)
        asset = dashboard.static("user_tests.js").decode("utf-8")
        self.assertIn("function renderProjectUserTests(data)", asset)
        self.assertIn('<script src="/user_tests.js" defer></script>',
                      dashboard.static("index.html").decode("utf-8"))
        self.assertIn("renderProjectUserTests(userTests)",
                      dashboard.static("companion.js").decode("utf-8"))
        self.assertIn('"user_tests.js": "text/javascript; charset=utf-8"',
                      (SOURCE.parent / "dashboard_cli.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
