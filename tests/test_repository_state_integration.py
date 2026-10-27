"""Repository orientation reaches agents/UI without changing local authority."""
import contextlib
import io
import json
import unittest
from unittest import mock

import test_dashboard as dashboard_fixture
import test_workflow as workflow_fixture
from eutonos import dashboard, repository_state_cli, workflow


class RepositoryStateIntegrationTests(unittest.TestCase):
    def test_pickup_keeps_local_owner_and_exposes_cross_checkout_evidence(self):
        fixture = workflow_fixture.WorkflowTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        evidence = {"status": "partial", "main": {"commit": "a" * 40},
                    "latest": {"items": [{"id": "AWR", "status": "completed"}]},
                    "coverage": {"worktrees_truncated": True}}
        with mock.patch("eutonos.repository_state.project", return_value=evidence), \
             mock.patch("eutonos.repository_state.compact", return_value=evidence):
            result = workflow.pickup(fixture.root)
        self.assertEqual(result["outcome"], "pickup")
        self.assertEqual(result["task_owner"]["path"], "WORK.md")
        self.assertEqual(result["repository_state"]["latest"]["items"][0]["status"], "completed")
        self.assertTrue(result["repository_state"]["coverage"]["worktrees_truncated"])

    def test_broken_global_reader_does_not_block_local_pickup(self):
        fixture = workflow_fixture.WorkflowTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with mock.patch("eutonos.repository_state.project", side_effect=ValueError("Unavailable source")):
            result = workflow.pickup(fixture.root)
        self.assertEqual(result["outcome"], "pickup")
        self.assertEqual(result["repository_state"]["status"], "unavailable")

    def test_dashboard_serves_same_evidence_without_inventing_source_acceptance(self):
        fixture = dashboard_fixture.DashboardTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        evidence = {"status": "partial", "main": {"freshness": "cached-local-ref"},
                    "latest": {"completed_unintegrated": [{"id": "work", "source_integration": "unknown"}]}}
        with mock.patch("eutonos.repository_state.project", return_value=evidence):
            result = dashboard.project(fixture.root)
        self.assertEqual(result["modules"]["repository_state"], evidence)

    def test_cli_preserves_partial_coverage_and_reports_unavailable(self):
        evidence = {"status": "partial", "coverage": {"refs_truncated": True}}
        output = io.StringIO()
        with mock.patch("eutonos.repository_state.project", return_value=evidence), contextlib.redirect_stdout(output):
            self.assertEqual(repository_state_cli.main([]), 0)
        self.assertTrue(json.loads(output.getvalue())["coverage"]["refs_truncated"])
        output = io.StringIO()
        with mock.patch("eutonos.repository_state.project", side_effect=ValueError("No Main")), contextlib.redirect_stdout(output):
            self.assertEqual(repository_state_cli.main([]), 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "unavailable")
