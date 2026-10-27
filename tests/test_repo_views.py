"""Cheap repository health and metrics projections: no composite score, bounded scans."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from eutonos import dashboard, repo_views


def _git(root, *args):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    subprocess.check_call(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                           "-C", str(root), *args], env=env)


class HealthIndicatorTests(unittest.TestCase):
    """health() is pure over an already-built project() result; no repo needed for most cases."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eutonos-repo-views-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def base_result(self, **overrides):
        result = {
            "repository": {"status": "available", "changes": {"modified": 0, "staged": 0, "untracked": 0, "conflicts": 0},
                            "uncommitted": 0},
            "release": {"status": "not-configured"},
            "modules": {"boards": {"status": "available", "items": []},
                        "nav": {"status": "available", "routes": []},
                        "write_retry": {"status": "not-started"},
                        "repo_metrics": {"status": "available", "divergence": {"branch": "main", "upstream": None,
                                                                               "ahead": None, "behind": None}}},
        }
        result.update(overrides)
        return result

    def by_id(self, indicators, id_):
        return next(i for i in indicators if i["id"] == id_)

    def test_counts_add_up_to_indicator_total(self):
        result = repo_views.health(self.root, self.base_result())
        self.assertEqual(sum(result["counts"].values()), len(result["indicators"]))
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["profile"], repo_views.HEALTH_PROFILE)

    def test_verification_is_always_unknown(self):
        result = repo_views.health(self.root, self.base_result())
        indicator = self.by_id(result["indicators"], "verification.no_store")
        self.assertEqual(indicator["state"], "unknown")

    def test_dependencies_is_always_unknown(self):
        result = repo_views.health(self.root, self.base_result())
        indicator = self.by_id(result["indicators"], "dependencies.no_audit")
        self.assertEqual(indicator["state"], "unknown")

    def test_boards_available_is_healthy(self):
        result = repo_views.health(self.root, self.base_result())
        indicator = self.by_id(result["indicators"], "work_records.boards")
        self.assertEqual(indicator["state"], "healthy")

    def test_boards_partial_is_warning_with_diagnostics(self):
        project_result = self.base_result()
        project_result["modules"]["boards"] = {"status": "partial", "items": [], "diagnostics": ["bad row"]}
        result = repo_views.health(self.root, project_result)
        indicator = self.by_id(result["indicators"], "work_records.boards")
        self.assertEqual(indicator["state"], "warning")
        self.assertIn("bad row", indicator["evidence"])

    def test_boards_error_is_alert(self):
        project_result = self.base_result()
        project_result["modules"]["boards"] = {"status": "error", "reason": "boom"}
        result = repo_views.health(self.root, project_result)
        indicator = self.by_id(result["indicators"], "work_records.boards")
        self.assertEqual(indicator["state"], "alert")

    def test_boards_missing_module_is_unknown(self):
        project_result = self.base_result()
        del project_result["modules"]["boards"]
        result = repo_views.health(self.root, project_result)
        indicator = self.by_id(result["indicators"], "work_records.boards")
        self.assertEqual(indicator["state"], "unknown")

    def test_nav_evidence_missing_paths_is_warning(self):
        project_result = self.base_result()
        project_result["modules"]["nav"] = {"status": "available",
                                            "routes": [{"evidence": ["does/not/exist.py"]}]}
        result = repo_views.health(self.root, project_result)
        indicator = self.by_id(result["indicators"], "knowledge.nav_evidence")
        self.assertEqual(indicator["state"], "warning")
        self.assertIn("does/not/exist.py", indicator["evidence"])

    def test_nav_evidence_existing_paths_is_healthy(self):
        (self.root / "present.py").write_text("x = 1\n", encoding="utf-8")
        project_result = self.base_result()
        project_result["modules"]["nav"] = {"status": "available", "routes": [{"evidence": ["present.py"]}]}
        result = repo_views.health(self.root, project_result)
        indicator = self.by_id(result["indicators"], "knowledge.nav_evidence")
        self.assertEqual(indicator["state"], "healthy")

    def test_nav_unavailable_module_is_unknown(self):
        project_result = self.base_result()
        project_result["modules"]["nav"] = {"status": "error", "reason": "boom"}
        result = repo_views.health(self.root, project_result)
        self.assertEqual(self.by_id(result["indicators"], "knowledge.nav_freshness")["state"], "unknown")
        self.assertEqual(self.by_id(result["indicators"], "knowledge.nav_evidence")["state"], "unknown")

    def test_nav_maps_without_baseline_run_is_unknown_not_healthy(self):
        from unittest import mock
        project_result = self.base_result()
        project_result["modules"]["nav"] = {"status": "available", "routes": []}
        no_baseline = {"outcome": "no-baseline", "records": 23, "sourceCurrent": "unknown",
                       "mapsCurrent": "unknown", "next": "Run a guarded build cycle."}
        with mock.patch("eutonos.nav_runner.Session") as session:
            session.return_value.status.return_value = no_baseline
            result = repo_views.health(self.root, project_result)
        indicator = self.by_id(result["indicators"], "knowledge.nav_freshness")
        self.assertEqual(indicator["state"], "unknown")

    def test_dirty_worktree_alone_is_not_unhealthy(self):
        project_result = self.base_result()
        project_result["repository"]["changes"] = {"modified": 3, "staged": 1, "untracked": 5, "conflicts": 0}
        project_result["repository"]["uncommitted"] = 9
        project_result["modules"]["repo_metrics"]["divergence"] = {"branch": "main", "upstream": "origin/main",
                                                                    "ahead": 0, "behind": 0}
        result = repo_views.health(self.root, project_result)
        indicator = self.by_id(result["indicators"], "delivery.push_state")
        self.assertEqual(indicator["state"], "healthy")

    def test_conflicts_are_alert(self):
        project_result = self.base_result()
        project_result["repository"]["changes"]["conflicts"] = 2
        result = repo_views.health(self.root, project_result)
        indicator = self.by_id(result["indicators"], "delivery.push_state")
        self.assertEqual(indicator["state"], "alert")

    def test_no_upstream_is_unknown(self):
        result = repo_views.health(self.root, self.base_result())
        indicator = self.by_id(result["indicators"], "delivery.push_state")
        self.assertEqual(indicator["state"], "unknown")

    def test_ahead_only_is_warning(self):
        project_result = self.base_result()
        project_result["modules"]["repo_metrics"]["divergence"] = {"branch": "main", "upstream": "origin/main",
                                                                    "ahead": 2, "behind": 0}
        result = repo_views.health(self.root, project_result)
        self.assertEqual(self.by_id(result["indicators"], "delivery.push_state")["state"], "warning")

    def test_diverged_ahead_and_behind_is_warning(self):
        project_result = self.base_result()
        project_result["modules"]["repo_metrics"]["divergence"] = {"branch": "main", "upstream": "origin/main",
                                                                    "ahead": 2, "behind": 3}
        result = repo_views.health(self.root, project_result)
        self.assertEqual(self.by_id(result["indicators"], "delivery.push_state")["state"], "warning")

    def test_matching_upstream_is_healthy(self):
        project_result = self.base_result()
        project_result["modules"]["repo_metrics"]["divergence"] = {"branch": "main", "upstream": "origin/main",
                                                                    "ahead": 0, "behind": 0}
        result = repo_views.health(self.root, project_result)
        self.assertEqual(self.by_id(result["indicators"], "delivery.push_state")["state"], "healthy")

    def test_write_retry_not_started_is_healthy(self):
        result = repo_views.health(self.root, self.base_result())
        self.assertEqual(self.by_id(result["indicators"], "recovery.write_retry")["state"], "healthy")

    def test_write_retry_pending_is_warning(self):
        project_result = self.base_result()
        project_result["modules"]["write_retry"] = {"status": "available",
                                                     "counts": {"pending": 1, "waiting": 0, "working": 0,
                                                                "complete": 0, "failed": 0}}
        result = repo_views.health(self.root, project_result)
        self.assertEqual(self.by_id(result["indicators"], "recovery.write_retry")["state"], "warning")

    def test_write_retry_failed_is_alert(self):
        project_result = self.base_result()
        project_result["modules"]["write_retry"] = {"status": "available",
                                                     "counts": {"pending": 0, "waiting": 0, "working": 0,
                                                                "complete": 0, "failed": 1}}
        result = repo_views.health(self.root, project_result)
        self.assertEqual(self.by_id(result["indicators"], "recovery.write_retry")["state"], "alert")

    def test_write_retry_error_is_alert(self):
        project_result = self.base_result()
        project_result["modules"]["write_retry"] = {"status": "error", "reason": "boom"}
        result = repo_views.health(self.root, project_result)
        self.assertEqual(self.by_id(result["indicators"], "recovery.write_retry")["state"], "alert")

    def test_runtime_not_configured_is_unknown(self):
        result = repo_views.health(self.root, self.base_result())
        self.assertEqual(self.by_id(result["indicators"], "runtime.alignment")["state"], "unknown")

    def test_runtime_matching_versions_is_healthy(self):
        project_result = self.base_result()
        project_result["release"] = {"status": "available", "version": "1.0.0", "runtime_version": "1.0.0"}
        result = repo_views.health(self.root, project_result)
        self.assertEqual(self.by_id(result["indicators"], "runtime.alignment")["state"], "healthy")

    def test_runtime_mismatched_versions_is_warning(self):
        project_result = self.base_result()
        project_result["release"] = {"status": "available", "version": "1.0.0", "runtime_version": "2.0.0"}
        result = repo_views.health(self.root, project_result)
        self.assertEqual(self.by_id(result["indicators"], "runtime.alignment")["state"], "warning")


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eutonos-repo-metrics-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        _git(self.root, "init", "-q", "-b", "main")
        repo_views._METRICS_CACHE.clear()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self, message, files):
        for name, text in files.items():
            self.write(name, text)
        _git(self.root, "add", "--all")
        _git(self.root, "commit", "-qm", message)

    def test_hotspots_and_co_change(self):
        self.commit("one", {"a.py": "1", "b.py": "1"})
        self.commit("two", {"a.py": "2", "b.py": "2"})
        self.commit("three", {"a.py": "3", "b.py": "3"})
        result = repo_views.metrics(self.root, limit=500)
        self.assertEqual(result["status"], "available")
        paths = {h["path"]: h["commits"] for h in result["hotspots"]}
        self.assertEqual(paths.get("a.py"), 3)
        self.assertEqual(paths.get("b.py"), 3)
        pair = next((p for p in result["co_change"] if {p["a"], p["b"]} == {"a.py", "b.py"}), None)
        self.assertIsNotNone(pair)
        self.assertEqual(pair["count"], 3)
        self.assertEqual(result["window"]["commits_scanned"], 3)
        self.assertFalse(result["window"]["truncated"])

    def test_deleted_file_excluded_from_hotspots(self):
        self.commit("add", {"gone.py": "1"})
        _git(self.root, "rm", "-q", "gone.py")
        _git(self.root, "commit", "-qm", "remove")
        result = repo_views.metrics(self.root, limit=500)
        self.assertNotIn("gone.py", [h["path"] for h in result["hotspots"]])

    def test_pair_below_threshold_excluded(self):
        self.commit("one", {"x.py": "1", "y.py": "1"})
        result = repo_views.metrics(self.root, limit=500)
        self.assertEqual(result["co_change"], [])

    def test_large_commit_skipped_from_co_change(self):
        files = {f"f{i}.py": "1" for i in range(45)}
        self.commit("big", files)
        result = repo_views.metrics(self.root, limit=500)
        self.assertEqual(result["co_change"], [])
        self.assertEqual(result["co_change_skipped_commits"], 1)
        # hotspots still count files from the big commit
        self.assertEqual(len(result["hotspots"]), 25)

    def test_truncated_flag_when_limit_reached(self):
        for i in range(5):
            self.commit(f"c{i}", {"a.py": str(i)})
        result = repo_views.metrics(self.root, limit=3)
        self.assertTrue(result["window"]["truncated"])
        self.assertEqual(result["window"]["commits_scanned"], 3)

    def test_cache_hit_on_unchanged_head(self):
        self.commit("one", {"a.py": "1"})
        first = repo_views.metrics(self.root, limit=500)
        with patch.object(repo_views, "_git_raw", side_effect=AssertionError("should not rescan")):
            second = repo_views.metrics(self.root, limit=500)
        self.assertEqual(first, second)

    def test_cache_miss_after_new_commit(self):
        self.commit("one", {"a.py": "1"})
        first = repo_views.metrics(self.root, limit=500)
        self.commit("two", {"a.py": "2"})
        second = repo_views.metrics(self.root, limit=500)
        self.assertNotEqual(first["window"]["last_commit"], second["window"]["last_commit"])

    def test_no_commits_is_unavailable(self):
        empty = Path(tempfile.mkdtemp(prefix="eutonos-repo-empty-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(empty, ignore_errors=True))
        _git(empty, "init", "-q", "-b", "main")
        result = repo_views.metrics(empty, limit=500)
        self.assertEqual(result["status"], "unavailable")

    def test_not_a_git_repository_is_unavailable(self):
        not_git = Path(tempfile.mkdtemp(prefix="eutonos-repo-notgit-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(not_git, ignore_errors=True))
        with patch.object(repo_views, "_git", return_value="git version 2.0.0"):
            result = repo_views.metrics(not_git, limit=500)
        self.assertEqual(result["status"], "unavailable")

    def test_git_missing_entirely_is_unavailable(self):
        self.commit("one", {"a.py": "1"})
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = repo_views.metrics(self.root, limit=500)
        self.assertEqual(result["status"], "unavailable")

    def test_divergence_no_upstream_is_unknown(self):
        self.commit("one", {"a.py": "1"})
        result = repo_views.metrics(self.root, limit=500)
        self.assertIsNone(result["divergence"]["upstream"])
        self.assertIsNone(result["divergence"]["ahead"])


class DashboardWiringTests(unittest.TestCase):
    def test_real_repo_includes_both_modules(self):
        repo_root = Path(__file__).resolve().parents[1]
        result = dashboard.project(repo_root)
        for name in ("repo_metrics", "repo_health"):
            self.assertIn(name, result["modules"])
            self.assertEqual(result["modules"][name]["status"], "available")
        self.assertIn("indicators", result["modules"]["repo_health"])
        self.assertIn("hotspots", result["modules"]["repo_metrics"])

    def test_static_allowlist_includes_repo_views_assets(self):
        with self.assertRaises(Exception):
            dashboard.static("unknown-asset.txt")
        # These names must be accepted by the allowlist even if the physical
        # asset file does not exist yet (a FileNotFoundError is acceptable,
        # a tokenslang denial is not).
        for name in ("repo_views.js", "repo_views.css"):
            try:
                dashboard.static(name)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    unittest.main()
