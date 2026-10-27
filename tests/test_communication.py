"""Installed work-linked publication, current controls and replacement-reader checks."""
from copy import deepcopy
import subprocess
import sys
import time
import unittest

from eutonos import Hub, GitView, tokenslang as ts, communication as msg
from eutonos.effects import FixtureAuthority
import test_ram


class CommunicationTests(unittest.TestCase):
    def setUp(self):
        self.f = test_ram.RamTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.root, self.local, self.coordinator = self.f.root, self.f.local, self.f.coordinator
        (self.root / "messages/work1/thread1").mkdir(parents=True)
        cfg = {**self.f.cfg, "profile": msg.CONFIG, "store_root": "messages"}
        (self.root / "eutonos.communication.json").write_bytes(ts.encoded(cfg))
        self.pin = self.f.commit()
        self.board = msg.Board(self.local)
        self.ident = {"work_id": "work1", "thread_id": "thread1", "issuer_id": "host1", "message_id": "note1"}
        self.request = {"profile": msg.WRITE, "operation": "post", "operation_id": "post1", "identity": self.ident,
                        "expected_revision": None, "session": "session1", "at": "2026-09-08T15:00:00Z",
                        "content": {"body": "Inspect current source and WORK before continuing; this note grants nothing.",
                                    "target": {"kind": "workstream", "id": "work1"}, "reply_to": None,
                                    "source_commit": self.pin, "source_paths": ["src/code.py"], "occurred_at": "2026-09-08T14:00:00Z"}}
        self.auth = None

    def apply(self, request=None):
        value = self.board.plan(request or self.request, "worker", "message-attempt")
        paths = frozenset(value["effect"]["observed"])
        self.auth = FixtureAuthority(self.local, "worker", paths | (self.auth.paths if self.auth else frozenset()), time.time() + 600)
        if not hasattr(self, "started"):
            self.coordinator.start(self.auth, "message-attempt"); self.started = True
        self.coordinator.reserve(self.auth, "message-attempt", 1, [c["path"] for c in value["effect"]["changes"]])
        return msg.apply(self.coordinator, self.auth, "message-attempt", 1, value, value["fingerprint"])

    def read(self, **kwargs):
        return msg.Board(self.local).read({"profile": msg.READ, "operation": "browse", "work_id": "work1", **kwargs})

    def change(self, operation, revision=1, **fields):
        return {k: v for k, v in self.request.items() if k != "content"} | {
            "operation": operation, "operation_id": operation + str(revision), "expected_revision": revision, **fields}

    def test_replacement_cli_and_hub_preserve_source_attribution(self):
        self.apply()
        request = {"profile": msg.READ, "operation": "browse", "work_id": "work1"}
        raw = subprocess.check_output([sys.executable, "-m", "eutonos.communication_cli", "read", "--root", str(self.root)], input=ts.encoded(request))
        item = ts.loads(raw)["items"][0]
        self.assertEqual(item["content"]["work"]["commit"], self.pin)
        self.assertIsNone(item["content"]["sender"]["model"])
        self.assertEqual([e["kind"] for e in item["events"]], ["posted"])
        self.assertFalse(item["actionable_authority"])
        self.assertEqual(len(Hub(GitView(self.root, self.pin)).handle(request)["items"]), 1)

    def test_siblings_and_retransmission(self):
        self.apply(); before = self.local.read(self.board.path(self.ident))
        self.apply()
        other = deepcopy(self.request); other["identity"]["issuer_id"] = "other"; other["operation_id"] = "other-post"
        self.apply(other)
        self.assertEqual(self.local.read(self.board.path(self.ident)), before)
        self.assertEqual(len(self.read()["items"]), 2)
        bad = deepcopy(self.request); bad["content"]["body"] = "Different retry"
        with self.assertRaisesRegex(ts.ReadError, "different content"):
            self.apply(bad)

    def test_reply_observation_and_correction_revision(self):
        self.apply()
        reply = deepcopy(self.request); reply["identity"]["message_id"] = "reply1"; reply["content"]["reply_to"] = self.ident
        reply["operation_id"] = "reply"; self.apply(reply)
        self.apply(self.change("observe", observation={"kind": "acknowledged", "message_revision": 1, "basis": "Fixture reader explicitly acknowledged revision one."}))
        correction = self.change("correct", 2, content={**self.request["content"], "body": "Corrected note; recheck current task."})
        self.apply(correction)
        with self.assertRaisesRegex(ts.ReadError, "superseded"):
            self.apply(self.change("observe", 3, observation={"kind": "resolved", "message_revision": 1, "basis": "Old content"}))
        self.assertEqual(len(self.read()["items"]), 2)

    def test_controls_and_archive(self):
        self.apply()
        for revision, visibility, state in [(1, "private", "active"), (2, "public", "suppressed"), (3, "public", "archived")]:
            self.apply(self.change("control", revision, controls={"visibility": visibility, "state": state}))
            self.assertEqual(self.read()["items"], [])
        self.assertEqual(len(self.read(include_archived=True)["items"]), 1)

    def test_changed_source_and_task_do_not_become_instructions(self):
        self.apply()
        (self.root / "src/code.py").write_bytes(b"def example():\n    return 2\n")
        (self.root / "WORK.md").write_bytes(b"# Another assignment\n")
        item = self.read()["items"][0]
        self.assertTrue(item["source_changes"][0]["changed"])
        self.assertTrue(item["current_task_changed"])
        self.assertFalse(item["actionable_authority"])

    def test_stale_plan_refuses_without_touching_sibling(self):
        value = self.board.plan(self.request, "worker", "message-attempt")
        self.apply()
        original = self.local.read(self.board.path(self.ident))
        value["operation_id"] = "different-logical-operation"
        with self.assertRaises(ts.ReadError):
            msg.apply(self.coordinator, self.auth, "message-attempt", 1, value, value["fingerprint"])
        self.assertEqual(self.local.read(self.board.path(self.ident)), original)

    def test_bounds_unknown_profile_and_scope(self):
        self.apply()
        self.assertEqual(self.read(max_bytes=512)["outcome"], "overflow")
        bad = deepcopy(self.request); bad["content"]["target"]["id"] = "other-work"
        bad["identity"]["message_id"] = "wrong"
        with self.assertRaises(ts.ReadError): self.apply(bad)
        with self.assertRaises(ts.ReadError): self.read(profile="TokenLang")
        with self.assertRaises(ts.ReadError): self.read(work_id="../escape")

    def test_pinned_hub_cannot_bypass_new_catalog_controls(self):
        self.apply(); hub = Hub(GitView(self.root, self.pin))
        path = self.root / "eutonos.read.json"; cfg = ts.loads(path.read_bytes())
        cfg["bindings"][0]["visibility"] = "private"; path.write_bytes(ts.encoded(cfg))
        self.assertEqual(hub.handle({"profile": msg.READ, "operation": "browse"})["code"], "stale")
        current = self.f.commit()
        self.assertEqual(Hub(GitView(self.root, current)).handle({"profile": msg.READ, "operation": "browse"})["code"], "denied")


if __name__ == "__main__":
    unittest.main()
