"""Summary-first dashboard snapshot and its on-demand section endpoint."""
import json
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from eutonos import dashboard
from eutonos.dashboard_cli import app
from http.server import ThreadingHTTPServer

import test_dashboard as _fixture


class LazySummaryTests(unittest.TestCase):
    setUp = _fixture.DashboardTests.setUp
    write = _fixture.DashboardTests.write
    git = _fixture.DashboardTests.git

    def serve(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app([self.root]))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def get(self, base, path, headers=None):
        with urlopen(Request(base + path, headers=headers or {}), timeout=60) as response:
            return json.loads(response.read().decode())

    def test_summary_project_defers_heavy_sections_but_keeps_full_shape(self):
        full = dashboard.project(self.root)
        summary = dashboard.project(self.root, detail="summary")
        self.assertEqual(set(full), set(summary))
        self.assertEqual(set(full["modules"]), set(summary["modules"]))
        for name in dashboard.LAZY_MODULES:
            self.assertTrue(summary["modules"][name].get("deferred"), name)
        self.assertEqual(summary["modules"]["ram"]["records"], [])
        self.assertEqual(summary["modules"]["documents"]["items"], [])
        self.assertTrue(summary["modules"]["companion"]["deferred"])
        for heavy in ("runs", "capture", "benchmark_suites", "experiments"):
            self.assertNotIn(heavy, summary["modules"]["companion"])
        self.assertNotIn("timeline", summary["modules"]["companion"].get("lifetime_metrics", {}))
        for name in ("boards", "nav", "skills", "changes", "current_work"):
            self.assertEqual(summary["modules"][name].get("status"), full["modules"][name].get("status"), name)
        self.assertNotIn("deferred", full["modules"]["ram"])

    def test_snapshot_detail_summary_reads_only_the_selected_project(self):
        snapshot = dashboard.snapshot([self.root, self.root], detail="summary", work_project=1)
        self.assertTrue(snapshot["projects"][0]["deferred_project"])
        self.assertEqual(snapshot["projects"][0]["modules"], {})
        self.assertNotIn("deferred_project", snapshot["projects"][1])
        self.assertEqual(dashboard.snapshot([self.root], detail="summary")["projects"][0]["name"], self.root.name)

    def test_module_endpoint_returns_each_deferred_section_and_validates_queries(self):
        base = self.serve()
        for name in dashboard.LAZY_MODULES:
            result = self.get(base, f"/api/v1/module?project=0&name={name}")
            self.assertEqual((result["project"], result["name"]), (0, name))
            self.assertIn(name, result["modules"])
            self.assertFalse(result["modules"][name].get("deferred"), name)
        self.assertIn("runs", self.get(base, "/api/v1/module?project=0&name=companion")["modules"]["companion"])
        for query in ("project=0", "name=delivery", "project=0&name=ram", "project=0&name=delivery&x=1",
                      "project=0&project=0&name=delivery", "project=1&name=delivery", "project=-1&name=delivery",
                      "project=0&name=delivery&refresh_state=0"):
            with self.assertRaises(HTTPError, msg=query) as error:
                urlopen(base + "/api/v1/module?" + query, timeout=30)
            self.assertEqual(error.exception.code, 400, query)
            error.exception.close()
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(base + "/api/v1/module?project=0&name=delivery", headers={"Host": "attacker.invalid"}), timeout=30)
        self.assertEqual(error.exception.code, 403)
        error.exception.close()

    def test_snapshot_endpoint_accepts_only_known_detail_values(self):
        base = self.serve()
        summary = self.get(base, "/api/v1/snapshot?project=0&detail=summary")
        self.assertTrue(summary["projects"][0]["modules"]["delivery"]["deferred"])
        full = self.get(base, "/api/v1/snapshot?project=0&detail=full")
        self.assertNotIn("deferred", full["projects"][0]["modules"]["delivery"])
        for detail in ("bogus", ""):
            with self.assertRaises(HTTPError):
                urlopen(base + f"/api/v1/snapshot?project=0&detail={detail}", timeout=30)


if __name__ == "__main__":
    unittest.main()
