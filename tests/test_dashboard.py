"""EDB read-only projections, service boundary and packaged UI."""
from importlib.resources import files
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from threading import Thread
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import urlencode

from eutonos import dashboard, nav, tokenslang as ts
from eutonos.dashboard_cli import app
from eutonos.host import enroll, read_snapshot
from http.server import ThreadingHTTPServer


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eutonos-dashboard-")
        self.addCleanup(self.tmp.cleanup)
        environment = mock.patch.dict(os.environ, {"EUTONOS_HOME": str(Path(self.tmp.name) / "eutonos-home")})
        environment.start()
        self.addCleanup(environment.stop)
        self.root = Path(self.tmp.name) / "fixture"
        self.root.mkdir(); (self.root / "NAV").mkdir(); (self.root / "RAM").mkdir(); (self.root / "observations").mkdir()
        self.write("docs/CURRENT_TASK.md", "# Current task\n\n## Top priority: fixture\n\nInspect the fixture.\n\n```tokenslang-work\n" +
                   json.dumps({"profile": ts.WORK, "work_id": "EDB", "goal": "Build dashboard", "status": "selected", "required": [], "optional": []}) + "\n```\n")
        self.write("eutonos.read.json", json.dumps({"profile": ts.CATALOG, "repository_id": "fixture.repo", "scope_id": "fixture.scope",
                   "namespace": "fixture.native", "owners": {"current_task": {"scope_id": "fixture.scope", "namespace": "fixture.native", "resource_id": "fixture.task"}},
                   "bindings": [{"resource_id": "fixture.task", "path": "docs/CURRENT_TASK.md", "kind": "docs", "label": "Task", "visibility": "public", "state": "active"}],
                   "excluded_roots": [], "source_paths": ["docs/CURRENT_TASK.md"]}))
        self.write("eutonos.ram.json", json.dumps({"profile": "eutonos.tokenslang.ram-config.v1", "repository_id": "fixture.repo",
                   "scope_id": "fixture.scope", "namespace": "fixture.native", "store_root": "RAM", "current_task": "docs/CURRENT_TASK.md",
                   "work_owners": {"EDB": "docs/CURRENT_TASK.md"}}))
        self.write("eutonos.nav.json", json.dumps({"profile": nav.CONFIG, "repository_id": "fixture.repo", "scope_id": "fixture.scope",
                   "namespace": "fixture.native", "map_root": "NAV", "source_roots": ["src"], "excluded_roots": [],
                   "context_paths": ["docs/CURRENT_TASK.md"]}))
        (self.root / "src").mkdir(); self.write("src/main.py", "print('fixture')\n")
        for name in nav.MAPS:
            template = files("eutonos").joinpath("nav_data/" + name.removesuffix(".md") + ".template.txt").read_text()
            self.write("NAV/" + name, template.replace("{{GOVERNING_SOURCE}}", "docs/CURRENT_TASK.md"))
        cycle = {"profile": "eutonos.tokenslang.nav-run.v1", "runID": "fixture-run", "mode": "refinement", "status": "queue-complete",
                 "sourceCommit": "fixture", "sourceFingerprint": "fingerprint", "closedSweeps": ["P1-source"], "debtPaths": [], "debtRecords": [],
                 "receipts": [{"packetID": "packet-1", "sweepID": "P1-source", "summary": "Found the entry route.",
                               "inspectedPaths": ["src/main.py"], "proposedRecordIDs": ["SRC-entry"],
                               "dispositions": {"lead-1": "covered"}, "omissionSearch": "Checked the fixture."}]}
        self.write("NAV/NAV_RUN.json", json.dumps(cycle))
        self.write("eutonos.measurement.json", json.dumps({"profile": "eutonos.tokenslang.measurement-config.v1", "scope_id": "fixture.scope",
                   "store_root": "observations", "mode": "metadata", "retention": "explicit-owner-controls", "export": "explicit-guarded-publication"}))
        self.git("init", "-q", "-b", "main"); self.git("add", "--all"); self.git("commit", "-qm", "Fixture")

    def write(self, name, text):
        path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text, encoding="utf-8")

    def git(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        subprocess.check_call(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-C", str(self.root), *args], env=env)

    def test_measurement_projection_uses_checked_owner_and_rejects_unsafe_config(self):
        from eutonos.effects import LocalRoot
        projected = dashboard._measurements(LocalRoot(self.root), self.root)
        self.assertEqual(projected["coverage"]["state"], "complete")
        self.assertEqual(projected["coverage"]["selected_records"], 0)
        cfg = json.loads((self.root / "eutonos.measurement.json").read_text())
        cfg["store_root"] = "../outside"
        self.write("eutonos.measurement.json", json.dumps(cfg))
        with self.assertRaises(ts.ReadError) as caught:
            dashboard._measurements(LocalRoot(self.root), self.root)
        self.assertEqual(caught.exception.code, "unsafe-path")

    def test_relevance_http_expands_exact_memory_and_rejects_drift(self):
        from test_ram import RamTests
        from urllib.parse import urlencode
        fixture = RamTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.apply()
        server = ThreadingHTTPServer(("127.0.0.1", 0), app([fixture.root]))
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        with urlopen(base + "/api/v1/ram/search?" + urlencode({"project": 0, "query": "example", "offset": 0})) as response:
            result = json.load(response)
        self.assertEqual(result["total"], 1)
        self.assertTrue(result["snapshot"])
        chosen = result["results"][0]
        added = json.loads((fixture.root / chosen["path"]).read_text(encoding="utf-8"))
        added["identity"]["memory_id"] = "added-after-search"
        added["versions"][-1]["summary"] = "example added after the browser snapshot"
        added_path = fixture.root / fixture.store.path(added["identity"])
        added_path.write_bytes(ts.encoded(added))
        frozen_query = {"project": 0, "query": "example", "offset": 0, "snapshot": result["snapshot"]}
        with urlopen(base + "/api/v1/ram/search?" + urlencode(frozen_query)) as response:
            frozen = json.load(response)
        self.assertEqual(frozen["total"], 1)
        with urlopen(base + "/api/v1/ram/search?" + urlencode({"project": 0, "query": "example", "offset": 0, "snapshot": ""})) as response:
            refreshed = json.load(response)
        self.assertEqual(refreshed["total"], 2)
        added_path.unlink()
        endpoint = base + "/api/v1/ram/get?" + urlencode({"project": 0, **{k: chosen[k] for k in ("path", "revision", "sha256")}})
        with urlopen(endpoint) as response:
            expanded = json.load(response)
        self.assertIn("example returns one", expanded["content"]["summary"])
        source = fixture.root / chosen["path"]
        source.write_bytes(source.read_bytes() + b"\n")
        with self.assertRaises(HTTPError) as drift:
            urlopen(endpoint)
        self.assertEqual(drift.exception.code, 404)
        drift.exception.close()
        hidden = json.loads(source.read_bytes())
        hidden["controls"]["visibility"] = "private"
        source.write_text(json.dumps(hidden), encoding="utf-8")
        projected = dashboard.project(fixture.root)
        self.assertEqual(projected["modules"]["ram"]["records"], [])
        self.assertEqual(projected["work"]["path"], "WORK.md")
        from copy import deepcopy
        from unittest.mock import patch
        for memory_id, state in (("aaa-archived", "archived"), ("zzz-visible", "active")):
            record = deepcopy(hidden)
            record["identity"]["memory_id"] = memory_id
            record["controls"].update(visibility="public", state=state)
            target = fixture.root / fixture.store.path(record["identity"])
            target.write_text(json.dumps(record), encoding="utf-8")
        with patch.object(dashboard, "MAX_RECORDS", 1):
            visible = dashboard.project(fixture.root)["modules"]["ram"]
        self.assertEqual([r["identity"]["memory_id"] for r in visible["records"]], ["zzz-visible"])
        with self.assertRaises(HTTPError) as malformed:
            urlopen(base + "/api/v1/ram/search?project=0&query=example&offset=-1")
        self.assertEqual(malformed.exception.code, 400)
        malformed.exception.close()
        with self.assertRaises(HTTPError) as scope:
            urlopen(base + "/api/v1/ram/search?project=1&query=example&offset=0")
        self.assertEqual(scope.exception.code, 400)
        scope.exception.close()

    def test_work_projection_honors_scope_and_private_alias(self):
        catalog = json.loads((self.root / "eutonos.read.json").read_text())
        self.assertEqual(dashboard._work_path(catalog), "docs/CURRENT_TASK.md")
        catalog["owners"]["current_task"]["scope_id"] = "foreign.scope"
        self.assertIsNone(dashboard._work_path(catalog))
        catalog["owners"]["current_task"]["scope_id"] = catalog["scope_id"]
        catalog["bindings"].append({**catalog["bindings"][0], "resource_id": "private.alias", "visibility": "private"})
        self.assertIsNone(dashboard._work_path(catalog))

    def test_repository_projection_counts_structure_activity_and_changes(self):
        self.git("branch", "feature")
        self.write("src/main.py", "print('changed')\n")
        self.write("src/new.py", "print('new')\n")
        value = dashboard.project(self.root)["repository"]
        self.assertEqual(value["status"], "available")
        self.assertEqual(value["worktrees"], 1)
        self.assertEqual(value["branches"], 2)
        self.assertEqual(value["current_branch"], "main")
        self.assertEqual(value["recent_days"], 7)
        self.assertGreaterEqual(value["recent_commits"], 1)
        self.assertEqual(value["uncommitted"], 2)
        self.assertEqual(value["changes"]["modified"], 1)
        self.assertEqual(value["changes"]["untracked"], 1)

    def test_optional_boards_project_only_structured_owner_items(self):
        self.write("eutonos.boards.json", json.dumps({"profile": "eutonos.tokenslang.boards.v1", "owners": ["docs/BOARD.md"]}))
        board = {"profile": "eutonos.tokenslang.board.v1", "items": [
            {"id": "TEST", "type": "campaign", "title": "Campaign", "summary": "Fixture", "status": "active"},
            {"id": "TEST1", "type": "slice", "title": "First", "summary": "Verified fixture", "status": "completed", "parent_id": "TEST"},
            {"id": "TEST2", "type": "slice", "title": "Second", "summary": "Pending", "status": "planned", "parent_id": "TEST"}]}
        self.write("docs/BOARD.md", "# Legacy completed claim\n\n```tokenslang-board\n" + json.dumps(board) + "\n```\n")
        before = (self.root / "docs/BOARD.md").read_bytes()
        result = dashboard.project(self.root)["modules"]["boards"]
        self.assertEqual(result["status"], "available")
        self.assertEqual([item["id"] for item in result["items"]], ["TEST", "TEST1", "TEST2"])
        self.assertEqual(result["items"][0]["child_counts"], {"completed": 1, "total": 2})
        self.assertEqual(result["items"][0]["owner_path"], "docs/BOARD.md")
        self.assertEqual(before, (self.root / "docs/BOARD.md").read_bytes())

    def test_projection_preserves_evidence_boundaries(self):
        result = dashboard.snapshot([self.root])
        self.assertEqual(result["profile"], dashboard.PROFILE)
        project = result["projects"][0]
        self.assertEqual(project["work"]["work_id"], "EDB")
        self.assertEqual(project["modules"]["nav"]["run_count"], 1)
        self.assertEqual(project["modules"]["nav"]["pass_count"], 1)
        self.assertEqual(project["modules"]["nav"]["runs"][0]["sweep_count"], 1)
        found = project["modules"]["nav"]["runs"][0]["passes"][0]
        self.assertEqual(found["changed_records"], ["SRC-entry"])
        self.assertEqual(found["dispositions"], {"covered": 1})
        self.assertEqual(project["modules"]["atlas"]["status"], "not-configured")
        self.assertIn("engine, index, or run adapter", project["modules"]["atlas"]["message"])
        self.assertTrue(project["modules"]["measurement"]["token_evidence"]["unknown"])
        self.assertIn(project["modules"]["delivery"]["status"], {"available", "partial"})
        self.assertIn(project["modules"]["write_retry"]["signal"], {"green", "yellow", "red"})
        self.assertEqual(project["modules"]["attribution"]["profile"],
                         "eutonos.companion-attribution.v1")

        self.write("eutonos.atlas.json", "{}")
        self.write("eutonos.visual-atlas.json", "{}")
        optional = dashboard.snapshot([self.root])["projects"][0]["modules"]
        self.assertEqual(optional["atlas"]["status"], "available")
        self.assertEqual(optional["visual_atlas"]["status"], "available")
        self.assertEqual(optional["nav"]["records"], project["modules"]["nav"]["records"])

    def test_surface_gallery_projects_and_serves_only_retained_images(self):
        gallery = self.root / ".eutonos-local" / "dashboard-polish"
        gallery.mkdir(parents=True)
        image = b"\x89PNG\r\n\x1a\nfixture"
        (gallery / "overview.png").write_bytes(image)
        value = dashboard.project(self.root)["modules"]["visual_atlas"]
        self.assertEqual(value["status"], "available")
        self.assertEqual(value["records"], 1)
        self.assertEqual(value["items"][0]["title"], "Overview")
        self.assertEqual(value["items"][0]["variant"], "desktop")
        server = ThreadingHTTPServer(("127.0.0.1", 0), app([self.root]))
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        with urlopen(base + "/api/v1/surface-image?" + urlencode({"project": 0, "name": "overview.png"})) as response:
            self.assertEqual(response.headers.get_content_type(), "image/png")
            self.assertEqual(response.read(), image)
        with self.assertRaises(HTTPError) as traversal:
            urlopen(base + "/api/v1/surface-image?" + urlencode({"project": 0, "name": "../overview.png"}))
        self.assertEqual(traversal.exception.code, 404)
        traversal.exception.close()

    def test_architecture_projection_reads_headings_without_board_status(self):
        self.write("docs/ARCHITECTURE.md", "# Architecture\n\nOrientation text.\n\n## Hub and modules\n\nDetails.\n\n## Authority and storage\n\nDetails.\n")
        value = dashboard.project(self.root)["modules"]["architecture"]
        self.assertEqual(value["status"], "available")
        self.assertEqual(value["path"], "docs/ARCHITECTURE.md")
        self.assertEqual([section["title"] for section in value["sections"]],
                         ["Hub and modules", "Authority and storage"])
        self.assertEqual(value["sections"][0]["detail"], "Details.")
        self.assertEqual(len(value["sha256"]), 64)
        self.assertIn("without inferring component status", value["qualification"])

    def test_cycle_distinguishes_unique_sweeps_from_receipts(self):
        value = {"runID": "r", "receipts": [{"sweepID": "P1"}, {"sweepID": "P1"}, {"sweepID": "P2"}], "closedSweeps": ["P1", "P2"]}
        result = dashboard._cycle(value)
        self.assertEqual(result["pass_count"], 3)
        self.assertEqual(result["sweep_count"], 2)

    def test_host_snapshot_uses_read_only_project_scope(self):
        host_dir = Path(self.tmp.name) / "host"
        enroll(self.root, host_dir, "fixture-agent", ["docs/CURRENT_TASK.md"])
        before = (host_dir / "coordinator.sqlite").read_bytes()
        result = read_snapshot(self.root, host_dir)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["actor"], "fixture-agent")
        self.assertEqual(result["attempts"], [])
        self.assertEqual(before, (host_dir / "coordinator.sqlite").read_bytes())

    def test_board_and_catalog_completion_checks_render_with_legacy_fallback(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        root = Path(__file__).resolve().parents[1]
        for script in ("tests/test_dashboard_boards.cjs", "tests/test_dashboard_catalog.cjs"):
            result = subprocess.run([node, script], cwd=root,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, script + ": " + result.stdout + result.stderr)

    def test_settings_api_exposes_the_read_only_install_report(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app([self.root]))
        thread = Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        report = {"readiness": {"state": "not-ready", "ready": False},
                  "declared": {"permissions": {"items": []}, "components": [], "effects": []},
                  "consent": {"text_id": "eutonos.consent.v1", "text_sha256": "a" * 64, "text": "fixture", "receipt": None}}
        with (mock.patch("eutonos.settings.status", return_value={"routing": {}, "ram": {}, "tracking": {}}),
              mock.patch("eutonos.settings.read_roles", return_value={"roles": {}, "revision": "roles"}),
              mock.patch("eutonos.settings.read_hooks", return_value={"hooks": {}, "revision": "hooks"}),
              mock.patch("eutonos.dashboard_cli._model_watch_hint", return_value={}),
              mock.patch("eutonos.doctor.report", return_value=report)):
            base = f"http://127.0.0.1:{server.server_port}"
            with urlopen(base + "/api/v1/settings?project=0", timeout=10) as response:
                value = json.loads(response.read().decode("utf-8"))
        self.assertEqual(value["install_doctor"]["readiness"]["state"], "not-ready")
        self.assertEqual(value["install_doctor"]["consent"]["text_sha256"], "a" * 64)

    def test_service_is_local_read_only_and_assets_are_packaged(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app([self.root]))
        thread = Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file() and ".git" not in p.parts}
        with urlopen(base + "/api/v1/snapshot", timeout=60) as response:
            self.assertEqual(response.status, 200); self.assertIn("selected-roots-only", response.read().decode())
        with urlopen(base + "/", timeout=10) as response:
            self.assertIn("EUTONOS Dashboard", response.read().decode())
        with urlopen(base + "/eutonos-epsilon-32.png", timeout=10) as response:
            self.assertEqual(response.headers.get_content_type(), "image/png")
            self.assertTrue(response.read().startswith(b"\x89PNG\r\n\x1a\n"))
        with self.assertRaises(HTTPError) as missing: urlopen(base + "/docs/CURRENT_TASK.md", timeout=10)
        self.assertEqual(missing.exception.code, 404); missing.exception.close()
        request = Request(base + "/api/v1/snapshot", headers={"Host": "attacker.invalid"})
        with self.assertRaises(HTTPError) as error: urlopen(request, timeout=10)
        self.assertEqual(error.exception.code, 403); error.exception.close()
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file() and ".git" not in p.parts}
        self.assertEqual(before, after)
        with urlopen(base + "/appearance.js") as appearance:
            self.assertIn("text/javascript", appearance.headers["Content-Type"])
            self.assertEqual(appearance.read(), dashboard.static("appearance.js"))
        js = dashboard.static("app.js").decode()
        self.assertIn('function delivery(p)', js)
        self.assertIn('Closeout writes', js)
        self.assertIn('data-view="delivery"', dashboard.static("index.html").decode())
        self.assertIn('data-view="gallery"', dashboard.static("index.html").decode())
        self.assertIn('data-settings-nav="routing"', dashboard.static("index.html").decode())
        self.assertIn('>Smart routing</a>', dashboard.static("index.html").decode())
        self.assertIn('data-settings-nav="install_consent"', dashboard.static("index.html").decode())
        self.assertIn('data-settings-nav="permissions"', dashboard.static("index.html").decode())
        self.assertIn("function installConsentSettings(value)", js)
        self.assertIn("function permissionsSettings(value)", js)
        self.assertIn("install_doctor", js)
        self.assertIn('Enable Smart routing', js)
        self.assertIn('When off, the turn-start routing hook sends no routing prompt.', js)
        # Main's settings accept three profiles; the fourth (maximum-cost-savings)
        # is settings-v4 work deferred to AWR7, so the UI must offer exactly these.
        # Renamed 2026-09-24 (AWR7 owner amendment): "balanced" -> "optimus" and
        # "maximum-worthwhile-intelligence" -> "high-quality", matching
        # settings.ROUTING_PREFERENCES.
        self.assertIn('preferences=["cost-savings","optimus","high-quality"]', js)
        # Settings-v4 UI (Luna forced to XHigh, report access) is deferred to AWR7
        # settings alignment; main's settings reader does not accept those fields.
        self.assertIn('data-settings-nav="ram"', dashboard.static("index.html").decode())
        self.assertIn('data-boards-nav="campaigns"', dashboard.static("index.html").decode())
        self.assertIn('data-boards-nav="features"', dashboard.static("index.html").decode())
        self.assertIn('data-boards-nav="bugs"', dashboard.static("index.html").decode())
        self.assertNotIn('data-settings-nav-toggle aria-expanded="false" aria-controls="settings-subnav" aria-label=', dashboard.static("index.html").decode())
        self.assertNotIn('settings-page-nav', js)
        self.assertIn('data-setting="routing-preference"', js)
        self.assertIn('data-harness-enabled', js)
        self.assertIn('data-job-field="model"', js)
        self.assertNotIn('data-setting="routing-max-companions"', js)
        self.assertIn("function applySettingsSection()", js)
        self.assertIn("function syncBoardsNav()", js)
        self.assertIn('workBrowser(p,"overview")', js)
        self.assertIn('function repositoryOverview(p)', js)
        self.assertIn('function controlCenter(p)', js)
        self.assertIn('function deliveryReadinessItems(p)', js)
        self.assertIn('function checkpointStats(records,turns)', js)
        self.assertIn('controlCenter(p)+deliveryReadiness(p,true)+workBrowser(p,"overview")', js)
        self.assertIn('repository:p.repository', js)
        self.assertIn('if(section==="campaigns")return campaignListV2(p)', js)
        self.assertIn("function visibleProjection(snapshot)", js)
        self.assertIn("Refresh memories", js)
        self.assertIn("function refreshMemorySnapshot()", js)
        self.assertIn("Results stay fixed while you page through them", js)
        self.assertIn('if(state.view==="boards")return {boards:p.modules?.boards,delivery:p.modules?.delivery,architecture:p.modules?.architecture}', js)
        self.assertIn("ATLAS read CLI available", js)
        self.assertIn("Version date", js)
        self.assertIn("does not prove publication, installed byte parity", js)
        for mark in ("\u00b7", "\u2026", "\u2192", "\u2019"):
            self.assertNotIn(mark.encode("utf-8").decode("cp1252"), js)
        self.assertNotIn("https://", js); self.assertIn("textContent", js)
        self.assertIn("Recorded lifetime coverage", js)
        self.assertIn("Same-token all-primary scenario", js)
        self.assertIn("Matched observed savings", js)
        self.assertIn("metricMoney(observed.observed_standard_api_estimate_usd)", js)
        self.assertNotIn("metricMoney(observed.standard_api_estimate_usd)", js)
        self.assertIn("Companion pairing not recorded", js)
        self.assertIn('class="timeline-scrollbar"', js)
        self.assertIn('notation:"compact"', js)
        self.assertIn("timelineFollowNewest", js)
        companion_js = dashboard.static("companion.js").decode()
        companion_css = dashboard.static("companion.css").decode()
        self.assertIn('event.preventDefault();setLabTab("tests")', companion_js)
        self.assertIn('function benchmarkReportLink(r)', companion_js)
        self.assertIn('/api/v1/benchmark-report?', companion_js)
        self.assertIn('label:"Delegation cost"', companion_js)
        self.assertIn('url.searchParams.delete("benchmark");url.searchParams.delete("testType")', companion_js)
        self.assertIn('class="benchmark-row-baseline"', companion_js)
        self.assertIn('row("Primary thinking"', companion_js)
        self.assertIn('row("Support thinking"', companion_js)
        self.assertIn('function benchmarkTokenSplit(result)', companion_js)
        self.assertIn('section("Model token split")', companion_js)
        self.assertIn('Combined answer usage', companion_js)
        self.assertIn('benchmarkModelName(model)', companion_js)
        self.assertIn('section("Semantic review usage")', companion_js)
        self.assertIn('row("Review thinking"', companion_js)
        self.assertIn('Analytical interpretation', companion_js)
        self.assertIn('Input / reasoning / output / total token change', companion_js)
        self.assertIn('Review basis and dimensions', companion_js)
        self.assertIn('Review source evidence', companion_js)
        self.assertIn('.benchmark-analysis-candidate{color:var(--green)}', companion_css)
        self.assertIn('row.usage_trust==="investigate"', companion_js)
        self.assertIn('An asterisk marks a preserved native counter', companion_js)
        self.assertIn("Recorded totals by thinking level", js)
        self.assertIn("Observed native turn configuration", js)
        self.assertIn("y1=\"56\" y2=\"56\"", companion_js)
        self.assertIn(".benchmark-row-graphed td{vertical-align:bottom}", companion_css)
        self.assertIn(".benchmark-row-bar{display:block;width:100%;height:58px", companion_css)
        self.assertIn('.benchmark-token-split-card', companion_css)
        self.assertIn('.benchmark-token-split-card:has(.model-sol)', companion_css)
        self.assertIn('data-test-print', companion_js)
        self.assertIn('data-test-pdf', companion_js)
        self.assertIn('Choose “Save as PDF” as the print destination.', companion_js)
        self.assertIn('window.addEventListener("beforeprint"', companion_js)
        self.assertIn("@media print", companion_css)
        self.assertIn(".sidebar,.topbar,#toast,.test-report-actions", companion_css)
        self.assertTrue(dashboard.static("eutonos-epsilon-64.png").startswith(b"\x89PNG\r\n\x1a\n"))

    def test_benchmark_report_endpoint_reads_only_hash_pinned_local_reports(self):
        report = self.root / '.eutonos-local' / 'reports' / 'opaque.md'
        report.parent.mkdir(parents=True)
        body = '# Fixture report\n'; report.write_bytes(body.encode('utf-8'))
        digest = __import__('hashlib').sha256(report.read_bytes()).hexdigest()
        server = ThreadingHTTPServer(("127.0.0.1", 0), app([self.root]))
        thread = Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}/api/v1/benchmark-report?"
        query = urlencode({'project': 0, 'report_path': '.eutonos-local/reports/opaque.md', 'report_sha256': digest})
        with urlopen(base + query) as response:
            self.assertEqual(response.headers.get_content_type(), 'text/plain')
            self.assertEqual(response.read().decode('utf-8'), body)
        for path in ('../opaque.md', 'C:/opaque.md', '.eutonos-local/metric-index.json'):
            with self.assertRaises(HTTPError) as invalid:
                urlopen(base + urlencode({'project': 0, 'report_path': path, 'report_sha256': digest}))
            self.assertEqual(invalid.exception.code, 400); invalid.exception.close()
        report.write_bytes(b'# Changed\n')
        with self.assertRaises(HTTPError) as changed:
            urlopen(base + query)
        self.assertEqual(changed.exception.code, 400); changed.exception.close()

    def test_windows_launcher_uses_the_explicit_installed_runtime(self):
        launcher = (Path(__file__).resolve().parents[1] / "launch eutonos dashboard.bat").read_text()
        self.assertIn('"%EUTONOS_PYTHON%" -I -m eutonos.dashboard_cli serve', launcher)
        self.assertIn('set "EUTONOS_ROOT=%%~fI"', launcher)
        self.assertIn('Closing this window stops the dashboard.', launcher)
        self.assertNotIn('python tools\\eutonos_project.py dashboard serve', launcher)
        self.assertNotIn('metric_index_cli update', launcher)
        self.assertIn('EUTONOS_DASHBOARD_OPENED_BY_COMPANION', launcher)

    def test_refresh_observes_native_change_without_a_frontend_build(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app([self.root]))
        thread = Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        endpoint = f"http://127.0.0.1:{server.server_port}/api/v1/snapshot"
        with urlopen(endpoint, timeout=60) as response:
            before = json.load(response)["projects"][0]["work"]["summary"]
        task = self.root / "docs/CURRENT_TASK.md"
        original = task.read_text(encoding="utf-8")
        self.addCleanup(task.write_text, original, encoding="utf-8")
        task.write_text(original.replace("Inspect the fixture.", "Inspect the changed fixture."), encoding="utf-8")
        with urlopen(endpoint, timeout=60) as response:
            after = json.load(response)["projects"][0]["work"]["summary"]
        self.assertNotEqual(before, after)
        self.assertIn("changed fixture", after)

    def test_companion_sidebar_tests_click_leaves_metrics_and_opens_library(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the packaged frontend behavior check")
        companion_js = files("eutonos").joinpath("dashboard_static", "companion.js")
        script = r'''
const fs = require("fs");
const vm = require("vm");
const clicks = [];
let prevented = false;
let pushed = "";
let rendered = "";
const location = {href: "http://127.0.0.1:8771/?benchmark=old-run&testType=ram#metrics", search: "?benchmark=old-run&testType=ram"};
const context = {
  URL, URLSearchParams, Intl, JSON, console, location,
  state: {view: "metrics"},
  document: {
    addEventListener(name, listener) { if (name === "click") clicks.push(listener); },
    querySelectorAll() { return []; }
  },
  window: {addEventListener() {}},
  history: {
    pushState(_state, _title, url) { pushed = String(url); location.href = pushed; location.search = new URL(pushed).search; },
    replaceState(_state, _title, url) { pushed = String(url); location.href = pushed; location.search = new URL(pushed).search; }
  },
  render() { rendered = context.state.view; }
};
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), context);
const event = {
  target: {closest(selector) { return selector === 'a[data-view="companion"]' ? {dataset: {view: "companion"}} : null; }},
  preventDefault() { prevented = true; }
};
for (const listener of clicks) listener(event);
const route = new URL(pushed);
if (!prevented) throw new Error("sidebar navigation was not intercepted");
if (context.state.view !== "companion" || rendered !== "companion") throw new Error(`view stayed ${context.state.view}; rendered ${rendered}`);
if (route.hash !== "#companion") throw new Error(`wrong hash ${route.hash}`);
if (route.searchParams.has("benchmark") || route.searchParams.has("testType")) throw new Error(`stale library route ${route.search}`);
'''
        result = subprocess.run(
            [node, "-e", script, str(companion_js)], capture_output=True, text=True, timeout=15
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_companion_token_split_labels_models_and_keeps_total_distinct(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the packaged frontend behavior check")
        companion_js = files("eutonos").joinpath("dashboard_static", "companion.js")
        script = r'''
const fs = require("fs");
const vm = require("vm");
const context = {
  URL, URLSearchParams, Intl, JSON, console,
  location: {href: "http://127.0.0.1:8771/#companion", search: ""},
  document: {addEventListener() {}, querySelectorAll() { return []; }},
  window: {addEventListener() {}}, history: {pushState() {}, replaceState() {}},
  esc(value) { return String(value); }, modelMark() { return ""; },
  num(value) { return new Intl.NumberFormat("en-US").format(value); }, dollars(value) { return `$${value}`; },
  toast() {}, localStorage: {getItem() { return null; }, setItem() {}}
};
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), context);
const html = vm.runInNewContext(`benchmarkTokenSplit({
  primary_model: "gpt-6-sol", support_model: "gpt-6-luna",
  primary: {input_tokens: 1000, cached_input_tokens: 700, output_tokens: 90, reasoning_output_tokens: 30},
  support: {input_tokens: 400, cached_input_tokens: 300, output_tokens: 40, reasoning_output_tokens: 10},
  answer: {input_tokens: 1400, cached_input_tokens: 1000, output_tokens: 130, reasoning_output_tokens: 40, cost_usd: 0.0123}
})`, context);
for (const required of ["Sol", "Luna", "Primary", "Support", "Combined answer usage", "Input", "Cached input", "Visible output", "Reasoning", "Full output", "Total tokens", "1,090", "440", "1,530", "model-sol", "model-luna"]) {
  if (!html.includes(required)) throw new Error(`missing ${required}`);
}
'''
        result = subprocess.run(
            [node, "-e", script, str(companion_js)], capture_output=True, text=True, timeout=15
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
