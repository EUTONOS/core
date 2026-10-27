"""Closeout creates one Quick work item when no native work owner resolves."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from eutonos import boards, ram, tokenslang as ts
from eutonos.host import enroll
from eutonos.workflow import execute_closeout, prepare_closeout


class CloseoutCreatesWorkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="closeout-new-work-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        environment = mock.patch.dict(os.environ, {"EUTONOS_HOME": str(self.home / "eutonos-home")})
        environment.start()
        self.addCleanup(environment.stop)
        self.root = self.home / "project"; self.root.mkdir()
        (self.root / "RAM").mkdir()
        (self.root / ".eutonos-local").mkdir()
        existing = {"id": "quick-existing", "type": "quick", "title": "Existing work", "status": "active",
                    "summary": "Already on the board."}
        board = {"profile": boards.BOARD, "items": [existing]}
        (self.root / "BOARD.md").write_bytes(b"# Board\n\n```tokenslang-board\n" + ts.encoded(board) + b"\n```\n")
        (self.root / "eutonos.boards.json").write_bytes(ts.encoded(
            {"profile": boards.CONFIG, "owners": ["BOARD.md"]}))
        (self.root / "changed.py").write_text("before = True\n", encoding="utf-8")
        config = {"profile": ram.CONFIG, "repository_id": "fixture.repo", "scope_id": "fixture.scope",
                  "namespace": "fixture.native", "store_root": "RAM", "current_task": "BOARD.md",
                  "work_owners": {"legacy-work": "BOARD.md"}}
        (self.root / "eutonos.ram.json").write_bytes(ts.encoded(config))
        self.git("init", "-q", "-b", "main"); self.git("add", "--all")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "baseline")
        (self.root / "changed.py").write_text("before = False\n", encoding="utf-8")
        self.host = self.home / "host"
        self.context = {"root": str(self.root), "host": str(self.host), "actor": "writer", "session_id": "session"}
        self.values = {"request": "Change the fixture.", "actions": ["Updated changed.py."],
                       "result": "The fixture now uses the new value.",
                       "why": "changed.py implements the requested value.", "checks": ["fixture check passed"],
                       "unresolved": [], "changed_paths": ["changed.py"]}

    def git(self, *args):
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        return subprocess.check_output(["git", "-c", "safe.directory=" + str(self.root), "-C", str(self.root),
                                        *args], env=env, text=True, stderr=subprocess.PIPE)

    def items(self):
        return boards.project(self.root)["items"]

    def run_closeout(self, context, prepared=None):
        prepared = prepared or prepare_closeout(self.values, context)
        self.assertEqual(prepared["outcome"], "prepared", prepared)
        enroll(self.root, self.host, "writer", prepared["allowed_paths"])
        return prepared, execute_closeout(prepared, context)

    def test_resolvable_work_id_is_unchanged(self):
        context = {**self.context, "work_id": "quick-existing"}
        prepared = prepare_closeout(self.values, context)
        self.assertNotIn("work_creation", prepared)
        self.assertEqual(prepared["identity"]["work_id"], "quick-existing")
        _, result = self.run_closeout(context, prepared)
        self.assertEqual(result["outcome"], "completed", result)
        self.assertNotIn("work_creation", result)
        self.assertEqual([item["id"] for item in self.items()], ["quick-existing"])

    def test_missing_work_id_creates_one_item_on_run_only(self):
        prepared = prepare_closeout(self.values, self.context)
        self.assertEqual(prepared["outcome"], "prepared", prepared)
        creation = prepared["work_creation"]
        self.assertTrue(creation["will_create"])
        self.assertTrue(creation["work_id"].startswith("quick-"))
        self.assertIsNone(creation["supplied_reference"])
        self.assertEqual([item["id"] for item in self.items()], ["quick-existing"])
        _, result = self.run_closeout(self.context, prepared)
        self.assertEqual(result["outcome"], "completed", result)
        self.assertEqual(result["work_creation"]["outcome"], "created")
        created = [item for item in self.items() if item["id"] == creation["work_id"]]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["type"], "quick")
        self.assertEqual(result["operation"]["identity"]["work_id"], creation["work_id"])
        self.assertEqual(len(self.items()), 2)

    def test_unresolved_work_id_is_kept_as_a_reference(self):
        context = {**self.context, "work_id": "crosstalk:claude-chat-42"}
        prepared = prepare_closeout(self.values, context)
        self.assertEqual(prepared["outcome"], "prepared", prepared)
        creation = prepared["work_creation"]
        self.assertEqual(creation["supplied_reference"], "crosstalk:claude-chat-42")
        self.assertNotEqual(creation["work_id"], "crosstalk:claude-chat-42")
        _, result = self.run_closeout(context, prepared)
        self.assertEqual(result["outcome"], "completed", result)
        created = [item for item in self.items() if item["id"] == creation["work_id"]]
        self.assertEqual(len(created), 1)
        self.assertIn("crosstalk:claude-chat-42", created[0]["summary"])
        self.assertFalse(any(item["id"] == "crosstalk:claude-chat-42" for item in self.items()))

    def test_prepare_then_run_is_idempotent(self):
        first = prepare_closeout(self.values, self.context)
        second = prepare_closeout(self.values, self.context)
        self.assertEqual(first["work_creation"]["work_id"], second["work_creation"]["work_id"])
        self.assertEqual(first["attempt"], second["attempt"])
        prepared, result = self.run_closeout(self.context, first)
        self.assertEqual(result["outcome"], "completed", result)
        again = execute_closeout(prepared, self.context)
        self.assertIn(again["outcome"], {"completed", "already-completed"}, again)
        self.assertEqual(again["work_creation"]["outcome"], "exists")
        self.assertEqual(len([i for i in self.items() if i["type"] == "quick"]), 2)

    def test_invalid_session_still_fails_visibly(self):
        result = prepare_closeout(self.values, {**self.context, "session_id": "bad session!"})
        self.assertEqual(result["outcome"], "incomplete")


if __name__ == "__main__":
    unittest.main()
