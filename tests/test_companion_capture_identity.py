"""Native lifecycle identity prevents stale-turn receipt attribution."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("capture_identity", Path(__file__).resolve().parents[1] / "tools/companion_capture.py")
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log = self.root / "native.jsonl"
        self.events = [
            {"type": "session_meta", "payload": {"id": "thread"}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "old"}},
            {"type": "response_item", "payload": {"role": "assistant", "phase": "final_answer", "content": "PRIVATE FINAL"}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "old"}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "current"}}]
        self.write()

    def write(self):
        self.log.write_text("".join(json.dumps(e) + "\n" for e in self.events), encoding="utf-8")

    def test_template_derives_current_without_transcript(self):
        value = capture.capture_template(self.log, "pre-final")
        self.assertEqual(value["turn_id"], "current")
        self.assertNotIn("PRIVATE", json.dumps(value))
        with self.assertRaises(ValueError):
            capture.make_receipt(value)  # Blank assessment cannot masquerade as done.

    def test_old_turn_cannot_be_prefinal(self):
        with self.assertRaisesRegex(ValueError, "current active"):
            capture.native_identity(self.log, "old", "pre-final")

    def test_completed_requires_explicit_identity_and_observed_final(self):
        with self.assertRaisesRegex(ValueError, "explicit"):
            capture.native_identity(self.log, stage="completed")
        self.assertEqual(capture.native_identity(self.log, "old", "completed")["state"], "completed")
        self.events.pop(2)
        self.write()
        with self.assertRaisesRegex(ValueError, "observed native final"):
            capture.native_identity(self.log, "old", "completed")

    def test_current_cannot_be_completed(self):
        with self.assertRaises(ValueError):
            capture.native_identity(self.log, "current", "completed")

    def test_record_rejects_wrong_thread_without_writes(self):
        value = capture.capture_template(self.log, "pre-final")
        value.update(thread_id="wrong", summary="Checkpoint only")
        request = self.root / "request.json"
        request.write_text(json.dumps(value), encoding="utf-8")
        code = capture.main(["record", "--root", str(self.root), "--input", str(request), "--log", str(self.log)])
        self.assertEqual(code, 2)
        self.assertFalse((self.root / ".eutonos-local").exists())

    def test_partial_append_and_template_exclusive_create(self):
        with self.log.open("ab") as f:
            f.write(b'{"type":')
        output = self.root / "template.json"
        args = ["template", "--log", str(self.log), "--stage", "pre-final", "--output", str(output)]
        self.assertEqual(capture.main(args), 0)
        before = output.read_bytes()
        self.assertEqual(capture.main(args), 2)
        self.assertEqual(output.read_bytes(), before)

    def test_missing_or_changed_session_fails(self):
        self.events.append({"type": "session_meta", "payload": {"id": "other"}})
        self.write()
        with self.assertRaisesRegex(ValueError, "identity changed"):
            capture.native_identity(self.log)

if __name__ == "__main__":
    unittest.main()
