"""GAP stage 4: selected-goal context, bounded edit and goal-aware pickup."""
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from eutonos import command_registry, workflow_cli
from eutonos.host import enroll

import test_gap_cli as gap
WRAPPER = gap.WRAPPER

SENTINEL = "PRIVATE_GOAL_TITLE_SENTINEL"
BODY = "PRIVATE_GOAL_OUTCOME_SENTINEL"


class GoalContextTests(unittest.TestCase):
    setUp = gap.GapCliTests.setUp
    cli = gap.GapCliTests.cli
    git = gap.GapCliTests.git
    shared = gap.GapCliTests.shared
    private = gap.GapCliTests.private

    def workflow(self, *args, expected=0):
        """Run the real pickup CLI route with the unchanged shared pickup body stubbed.

        A full pickup fixture needs a complete workflow repository; the stub is
        fixed so any goal or personal content in the result came from this stage.
        """
        output = io.StringIO()
        base = {"profile": "eutonos.tokenslang.workflow.v1", "outcome": "pickup", "selected_task": None}
        with mock.patch("sys.stdout", output), mock.patch.object(workflow_cli, "pickup", return_value=dict(base)):
            code = workflow_cli.main([str(a) for a in args])
        self.assertEqual(code, expected, output.getvalue())
        return json.loads(output.getvalue())

    def shared_goal(self, root, host):
        form = self.cli("goals", "--root", root, "form", "--owner-path", "GOALS.md")
        item = form["request"]["operations"][0]["request"]["items"][0]
        item.update(title="Ship a bounded outcome", summary="Shared result")
        goal = item["extensions"]["goal"]
        goal["outcome"] = "A supported shared result"
        goal["criteria"][0]["statement"] = "Result exists"
        goal["scope"] = ["Inside scope"]
        goal["exclusions"] = ["Dashboard work"]
        self.cli("goals", "submit", "--root", root, "--host", host, "--actor", "actor",
                 "--session", "native-session", request=form)
        return item["id"]

    def private_goal(self, root, host):
        form = self.cli("personal", "form", "--kind", "goal", "--root", root, "--owner-id", "owner")
        item = form["request"]["item"]
        item.update(title=SENTINEL, summary="Private summary")
        goal = item["extensions"]["goal"]
        goal["outcome"] = BODY
        goal["criteria"][0]["statement"] = "Private criterion"
        self.cli("personal", "submit", "--root", root, "--owner-id", "owner", "--host", host,
                 "--actor", "actor", "--session", "native-session", request=form)
        return item["id"]

    def test_shared_select_context_pins_unknowns_and_advice(self):
        root = self.shared(); host = self.base / "shared-host"
        enroll(root, host, "actor", ["GOALS.md"])
        goal_id = self.shared_goal(root, host)
        listing = self.cli("goal-context", "select", "--root", root)
        self.assertEqual([c["id"] for c in listing["candidates"]], [goal_id])
        brief = self.cli("goal-context", "context", "--root", root, "--item-id", goal_id)
        self.assertEqual(brief["outcome_statement"], "A supported shared result")
        self.assertEqual(brief["criteria"][0]["assessment"], "unassessed")
        self.assertEqual(brief["exclusions"], ["Dashboard work"])
        self.assertEqual(brief["scope_items"], ["Inside scope"])
        self.assertTrue(brief["pins"]["owner_sha256"] and brief["pins"]["item_revision"])
        self.assertTrue(brief["pins"]["source_pin"]["commit"])
        self.assertTrue(any("Criteria not yet supported" in u for u in brief["unknowns"]))
        self.assertTrue(any("not scanned" in u for u in brief["unknowns"]))
        names = [o["advice"] for o in brief["advice"]["options"]]
        self.assertEqual(names, ["do-now", "defer", "minimal-integration-preparation", "deliberate-goal-revision"])
        self.assertIn("grants no permission", brief["advice"]["authority"])
        full = self.cli("goal-context", "context", "--root", root, "--item-id", goal_id, "--detail", "full")
        self.assertEqual(full["detail"], "full")
        missing = self.cli("goal-context", "context", "--root", root, "--item-id", "goal-none", expected=2)
        self.assertEqual(missing["code"], "missing")

    def test_shared_bounded_edit_uses_existing_write_and_refuses_stale_or_unbounded(self):
        root = self.shared(); host = self.base / "shared-host"
        enroll(root, host, "actor", ["GOALS.md"])
        goal_id = self.shared_goal(root, host)
        args = ("--root", root, "--item-id", goal_id)
        bad = self.cli("goal-context", "edit", "form", *args, request={"extensions": {}}, expected=2)
        self.assertEqual(bad["code"], "invalid")
        stale = self.cli("goal-context", "edit", "form", *args, request={"summary": "Stale"})
        form = self.cli("goal-context", "edit", "form", *args,
                        request={"summary": "Edited", "semantic": {"scope": ["Inside scope", "More"]}})
        self.assertEqual(form["bounded_edit"]["changed_fields"], ["scope", "summary"])
        done = self.cli("goal-context", "edit", "submit", "--root", root, "--host", host, "--actor", "actor",
                        "--session", "native-session", request=form)
        self.assertEqual(done["outcome"], "complete")
        after = self.cli("goal-context", "context", *args)
        self.assertEqual((after["selected"]["summary"], after["scope_items"]), ("Edited", ["Inside scope", "More"]))
        refused = self.cli("goal-context", "edit", "submit", "--root", root, "--host", host, "--actor", "actor",
                           "--session", "native-session", request=stale, expected=2)
        self.assertEqual(refused["code"], "stale")

    def test_direct_root_fallback_needs_no_enrollment_or_catalog(self):
        root = self.shared(); host = self.base / "shared-host"
        enroll(root, host, "actor", ["GOALS.md"])
        goal_id = self.shared_goal(root, host)
        (root / "eutonos.read.json").unlink()
        result = self.cli("goal-context", "context", "--root", root, "--item-id", goal_id)
        self.assertIsNone(result["pins"]["source_pin"])
        self.assertTrue(any("catalog is absent" in u for u in result["unknowns"]))
        self.assertFalse(list(Path(self.env["EUTONOS_HOME"]).glob("device-repositories*")))

    def test_pickup_is_unchanged_without_selection_and_adds_shared_goal_context(self):
        root = self.shared(); host = self.base / "shared-host"
        enroll(root, host, "actor", ["GOALS.md"])
        goal_id = self.shared_goal(root, host)
        plain = self.workflow("--root", root, "pickup")
        self.assertNotIn("goal_context", plain)
        self.assertNotIn("personal_goal_context", plain)
        selected = self.workflow("--root", root, "pickup", "--goal", goal_id)
        self.assertEqual(selected["goal_context"]["outcome_statement"], "A supported shared result")
        self.assertEqual(selected["goal_context"]["pins"]["owner_path"], "GOALS.md")
        bad = self.workflow("--root", root, "pickup", "--goal", "goal-none", expected=2)
        self.assertEqual(bad["outcome"], "pickup")
        self.assertEqual(bad["goal_context"]["code"], "missing")

    def test_private_scope_requires_explicit_selection_and_never_reaches_public_paths(self):
        root, phost, _ = self.private()
        pid = self.private_goal(root, phost)
        shared = self.shared(); host = self.base / "shared-host"
        enroll(shared, host, "actor", ["GOALS.md"])
        self.shared_goal(shared, host)
        for args in (("--scope", "personal"), ("--scope", "personal", "--owner-id", "wrong"), ()):
            for command in (("select",), ("context", "--item-id", pid)):
                out = self.cli("goal-context", *command, "--root", root, *args, expected=2)
                self.assertEqual(out["code"], "denied")
                self.assertNotIn(SENTINEL, json.dumps(out)); self.assertNotIn(str(root), json.dumps(out))
        listing = self.cli("goal-context", "select", "--root", root, "--scope", "personal", "--owner-id", "owner")
        self.assertEqual(listing["candidates"][0]["title"], SENTINEL)
        context = self.cli("goal-context", "context", "--root", root, "--scope", "personal",
                           "--owner-id", "owner", "--item-id", pid)
        self.assertEqual(context["outcome_statement"], BODY)
        public = self.workflow("--root", shared, "pickup")
        self.assertNotIn(SENTINEL, json.dumps(public)); self.assertNotIn(BODY, json.dumps(public))
        # A private root can never be read as shared scope, even by explicit goal ID.
        denied = self.workflow("--root", root, "pickup", "--goal", pid, expected=2)
        self.assertEqual(denied["goal_context"]["code"], "denied")
        self.assertNotIn(SENTINEL, json.dumps(denied)); self.assertNotIn(BODY, json.dumps(denied))
        both = self.workflow("--root", shared, "pickup", "--goal", pid, "--personal-root", root, "--owner-id", "owner")
        self.assertEqual(both["personal_goal_context"]["outcome_statement"], BODY)
        self.assertNotIn(SENTINEL, json.dumps({k: v for k, v in both.items() if k != "personal_goal_context"}))
        wrong = self.workflow("--root", shared, "pickup", "--goal", pid, "--personal-root", root,
                              "--owner-id", "wrong", expected=2)
        self.assertEqual(wrong["personal_goal_context"]["code"], "denied")
        self.assertNotIn(SENTINEL, json.dumps(wrong)); self.assertNotIn(str(root), json.dumps(wrong))
        for top in (shared, Path(self.env["EUTONOS_HOME"])):
            for path in Path(top).rglob("*"):
                if path.is_file() and "private-hosts" not in path.parts:
                    data = path.read_bytes()
                    self.assertNotIn(SENTINEL.encode(), data, str(path))
                    self.assertNotIn(BODY.encode(), data, str(path))

    def test_private_bounded_edit_roundtrip(self):
        root, phost, _ = self.private()
        pid = self.private_goal(root, phost)
        args = ("--root", root, "--scope", "personal", "--owner-id", "owner", "--item-id", pid)
        form = self.cli("goal-context", "edit", "form", *args, request={"semantic": {"exclusions": ["No servers"]}})
        done = self.cli("goal-context", "edit", "submit", *args, "--host", phost, "--actor", "actor",
                        "--session", "native-session", request=form)
        self.assertEqual(done["history"]["state"], "committed")
        after = self.cli("goal-context", "context", *args)
        self.assertEqual(after["exclusions"], ["No servers"])

    def test_registration_and_help(self):
        self.assertEqual(command_registry.EXPERT_ROUTES["goal-context"], "eutonos.goal_context")
        self.assertEqual(command_registry.describe("goal-context")["outcome"], "description")
        result = subprocess.run([sys.executable, str(WRAPPER), "expert", "goal-context", "--help"],
                                capture_output=True, text=True, env=self.env, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--scope", result.stdout)


if __name__ == "__main__":
    unittest.main()
