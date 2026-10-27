import os
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout

from eutonos import assignments, assignments_cli, settings, tokenslang as ts


class AssignmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="assignments-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "project"
        self.root.mkdir()
        (self.root / "README.md").write_text("# Fixture\n", encoding="utf-8")
        self.git("init", "-q", "-b", "main"); self.git("add", "README.md")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qm", "baseline")
        current = settings.status(self.root)
        routing = {"enabled": True, "routing": "adaptive", "model": None, "effort": None,
                   "allowed_models": list(settings.MODELS)}
        settings.update(self.root, 0, current["revision"], "routing", routing)

    def git(self, *args):
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        return subprocess.check_output(["git", "-c", "safe.directory=" + str(self.root), "-C", str(self.root),
                                        *args], env=env, stderr=subprocess.PIPE)

    def create_request(self, key="key-one", request_id="request-one"):
        return {"profile": assignments.CREATE, "idempotency_key": key, "request_id": request_id,
                "primary_thread_id": "primary-thread", "primary_turn_id": "primary-turn",
                "primary_model": "gpt-6-sol", "worker_model": "gpt-6-luna",
                "worker_effort": "xhigh", "category": "other", "logical_scope": "bounded source review",
                "brief": {"goal": "Review the bounded source.", "scope": "Only src/example.py.",
                          "checks": ["Run the focused check."], "closeout": "Return findings and evidence."},
                "source_refs": [{"path": "README.md"}]}

    def attach_request(self, record, thread="worker-thread", turn="worker-turn"):
        return {"profile": assignments.ATTACH, "assignment_id": record["assignment_id"],
                "expected_revision": record["revision"], "worker_thread_id": thread, "worker_turn_id": turn,
                "usage_reference": {"record_id": "native-record", "turn_id": turn}}

    def collect_request(self, record, outcome="completed"):
        return {"profile": assignments.COLLECT, "assignment_id": record["assignment_id"],
                "expected_revision": record["revision"], "outcome": outcome,
                "result": {"summary": "Focused review complete.",
                           "findings": [{"summary": "One bounded finding.",
                                         "source_refs": [{"path": "README.md", "start_line": 1, "end_line": 1}]}],
                           "changed_paths": [], "checks": ["Focused check passed."],
                           "unresolved": [], "closeout": {"outcome": "reported", "evidence_refs": [
                               {"scope_id": "fixture", "namespace": "native", "resource_id": "result"}]},
                           "model": "gpt-6-astra"}}

    def test_missing_forms_are_useful_and_do_not_write(self):
        description = assignments.profile_description()
        self.assertEqual(assignments._validate_create(description["operations"]["create"]["example"])["profile"],
                         assignments.CREATE)
        self.assertEqual(assignments._validate_collect(description["operations"]["collect"]["example"])["outcome"],
                         "completed")
        created = assignments.create(self.root, {})
        collected = assignments.collect(self.root, {})
        self.assertEqual(created["outcome"], "incomplete")
        self.assertIn("brief", created["missing_fields"])
        self.assertEqual(collected["outcome"], "incomplete")
        self.assertIn("result", collected["missing_fields"])
        self.assertEqual(assignments.records(self.root), [])

    def test_create_is_idempotent_and_same_key_changed_payload_conflicts(self):
        request = self.create_request()
        first = assignments.create(self.root, request)
        self.assertEqual((first["outcome"], first["assignment"]["state"]), ("created", "prepared"))
        self.assertEqual(assignments.create(self.root, request)["outcome"], "idempotent")
        current = settings.status(self.root)
        settings.update(self.root, 0, current["revision"], "routing", {**current["routing"], "enabled": False})
        self.assertEqual(assignments.create(self.root, request)["outcome"], "idempotent")
        conflict = assignments.create(self.root, {**request, "logical_scope": "different"})
        self.assertEqual((conflict["outcome"], conflict["code"]), ("conflict", "idempotency-conflict"))
        self.assertIsNone(first["assignment"]["attachment"])

    def test_attach_revision_idempotency_and_self_delegation_guard(self):
        record = assignments.create(self.root, self.create_request())["assignment"]
        with self.assertRaises(ts.ReadError) as caught:
            assignments.attach(self.root, self.attach_request(record, thread="primary-thread"))
        self.assertEqual(caught.exception.code, "conflict")
        request = self.attach_request(record)
        attached = assignments.attach(self.root, request)
        self.assertEqual((attached["outcome"], attached["assignment"]["revision"]), ("attached", 2))
        self.assertEqual(assignments.attach(self.root, request)["outcome"], "idempotent")
        with self.assertRaises(ts.ReadError) as stale:
            assignments.attach(self.root, self.attach_request(record, thread="other-worker"))
        self.assertEqual(stale.exception.code, "stale")

    def test_collect_is_idempotent_validated_and_packet_excludes_result(self):
        record = assignments.create(self.root, self.create_request())["assignment"]
        record = assignments.attach(self.root, self.attach_request(record))["assignment"]
        request = self.collect_request(record)
        collected = assignments.collect(self.root, request)
        self.assertEqual((collected["outcome"], collected["assignment"]["state"]), ("collected", "completed"))
        self.assertEqual(collected["assignment"]["result"]["model_identity_basis"],
                         "worker self-reported; not independently verified")
        self.assertEqual(assignments.collect(self.root, request)["outcome"], "idempotent")
        conflicting = self.collect_request(record, outcome="needs-attention")
        with self.assertRaises(ts.ReadError) as stale:
            assignments.collect(self.root, conflicting)
        self.assertEqual(stale.exception.code, "stale")
        packet = assignments.packet(self.root, record["assignment_id"])
        self.assertNotIn("result", packet)
        self.assertNotIn("findings", packet)
        self.assertEqual(packet["worker"]["thread_id"], "worker-thread")
        invalid = self.collect_request({**record, "revision": 3})
        invalid["result"]["changed_paths"] = [str(self.root / "outside.py")]
        with self.assertRaises(ts.ReadError):
            assignments.collect(self.root, invalid)

    def test_same_worker_can_hold_distinct_requests_and_list_pages(self):
        created = [assignments.create(self.root, self.create_request("key-" + str(i), "request-" + str(i)))["assignment"]
                   for i in range(3)]
        attached = [assignments.attach(self.root, self.attach_request(record))["assignment"] for record in created]
        self.assertEqual(len({item["assignment_id"] for item in attached}), 3)
        self.assertEqual({item["attachment"]["thread_id"] for item in attached}, {"worker-thread"})
        first = assignments.list_records(self.root, 0, 2)
        second = assignments.list_records(self.root, first["next_offset"], 2)
        self.assertEqual((first["total"], len(first["assignments"]), len(second["assignments"])), (3, 2, 1))
        self.assertFalse(first["complete"]); self.assertTrue(second["complete"])

    def test_disabled_and_unapproved_upgrade_create_no_record(self):
        current = settings.status(self.root)
        settings.update(self.root, 0, current["revision"], "routing", {
            **current["routing"], "enabled": False})
        self.assertEqual(assignments.create(self.root, self.create_request())["outcome"], "continue-primary")
        self.assertEqual(assignments.records(self.root), [])
        current = settings.status(self.root)
        settings.update(self.root, 0, current["revision"], "routing", {
            **current["routing"], "enabled": True})
        request = {**self.create_request(), "primary_model": "gpt-6-luna", "worker_model": "gpt-6-sol",
                   "worker_effort": "medium"}
        self.assertEqual(assignments.create(self.root, request)["outcome"], "approval-required")
        self.assertEqual(assignments.records(self.root), [])

    def test_global_fixed_route_requires_match_but_explicit_user_selection_can_override(self):
        current = settings.status(self.root)
        settings.update(self.root, 0, current["revision"], "routing", {
            "enabled": True, "routing": "fixed", "model": "gpt-6-luna", "effort": "xhigh",
            "allowed_models": list(settings.MODELS)})
        mismatch = {**self.create_request(), "worker_model": "gpt-6-sol"}
        self.assertEqual(assignments.create(self.root, mismatch)["outcome"], "invalid-settings")
        selected = {**mismatch, "user_selected": True}
        created = assignments.create(self.root, selected)
        self.assertEqual((created["outcome"], created["assignment"]["routing"]["basis"]),
                         ("created", "explicit-user-selection"))
        self.assertEqual(created["assignment"]["project_origin"]["root"], str(self.root))

    def test_supported_efforts_are_separate_from_route_preferences(self):
        request = {**self.create_request(), "primary_model": "gpt-6-luna", "worker_model": "gpt-6-sol",
                   "worker_effort": "high", "user_selected": True}
        created = assignments.create(self.root, request)
        self.assertEqual((created["outcome"], created["assignment"]["requested_worker"]["model"],
                          created["assignment"]["requested_worker"]["effort"]),
                         ("created", "gpt-6-sol", "high"))
        invalid = {**request, "idempotency_key": "invalid-effort", "request_id": "invalid-effort",
                   "worker_model": "gpt-6-luna", "worker_effort": "ultra"}
        with self.assertRaises(ts.ReadError):
            assignments.create(self.root, invalid)

    def test_invalid_source_reference_is_rejected(self):
        request = self.create_request()
        request["source_refs"] = [{"path": str(self.root / "README.md")}]
        with self.assertRaises(ts.ReadError):
            assignments.create(self.root, request)

    def test_cli_profile_and_incomplete_create_are_json(self):
        for argv, outcome, code in ((["profile", "--root", str(self.root)], "profile", 0),
                                    (["create", "--root", str(self.root)], "incomplete", 2)):
            with self.subTest(argv=argv), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(assignments_cli.main(argv), code)
                self.assertEqual(json.loads(output.getvalue())["outcome"], outcome)


if __name__ == "__main__":
    unittest.main()
