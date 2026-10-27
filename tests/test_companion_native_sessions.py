import json
from pathlib import Path
import tempfile
import unittest

from eutonos.companion_native_sessions import cache_clear, sources


class NativeSessionDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.other = Path(self.temp.name) / "other"
        self.sessions = Path(self.temp.name) / "sessions"
        self.project.mkdir(); self.other.mkdir(); self.sessions.mkdir()
        self.addCleanup(cache_clear)

    def header(self, name, payload):
        (self.sessions / name).write_text(json.dumps({"type": "session_meta", "payload": payload}) + "\n"
                                         + '{"type":"response_item","payload":{"content":"PRIVATE"}}\n', encoding="utf-8")

    def test_project_root_and_external_cwd_child_are_selected(self):
        self.header("root.jsonl", {"id": "root", "cwd": str(self.project)})
        self.header("child.jsonl", {"id": "child", "cwd": str(self.other), "parent_thread_id": "root",
                                     "source": {"subagent": {"thread_spawn": {"parent_thread_id": "root", "agent_path": "worker"}}}})
        self.header("other.jsonl", {"id": "other", "cwd": str(self.other)})
        result = sources(self.sessions, self.project)
        self.assertEqual([(item["experiment_id"], item["role"]) for item in result["sources"]],
                         [("native-session:child", "support"), ("native-session:root", "primary")])
        self.assertIn("Initial session cwd", result["scope"])

    def test_unknown_subagent_and_malformed_headers_are_not_promoted_to_primary(self):
        self.header("root.jsonl", {"id": "root", "cwd": str(self.project)})
        self.header("other.jsonl", {"id": "guardian", "cwd": str(self.project), "parent_thread_id": "root",
                                     "source": {"subagent": {"other": {}}}})
        (self.sessions / "bad.jsonl").write_text("not json\n", encoding="utf-8")
        result = sources(self.sessions, self.project)
        self.assertEqual([(item["experiment_id"], item["role"]) for item in result["sources"]],
                         [("native-session:root", "primary")])
        self.assertEqual(result["unsupported_subagent_count"], 1)

    def test_other_project_child_working_here_is_not_our_project(self):
        self.header("other.jsonl", {"id": "other", "cwd": str(self.other)})
        self.header("child.jsonl", {"id": "child", "cwd": str(self.project), "parent_thread_id": "other",
            "source": {"subagent": {"thread_spawn": {"parent_thread_id": "other", "agent_path": "worker"}}}})
        (self.sessions / "array.jsonl").write_text("[]\n", encoding="utf-8")
        self.assertEqual(sources(self.sessions, self.project)["sources"], [])

    def test_growing_one_log_reuses_other_headers(self):
        from unittest.mock import patch
        from eutonos import companion_native_sessions as module
        self.header("first.jsonl", {"id": "first", "cwd": str(self.project)})
        self.header("second.jsonl", {"id": "second", "cwd": str(self.project)})
        sources(self.sessions, self.project)
        with (self.sessions / "first.jsonl").open("a", encoding="utf-8") as stream:
            stream.write("{}\n")
        with patch.object(module, "_header", wraps=module._header) as read:
            sources(self.sessions, self.project)
            self.assertEqual(read.call_count, 1)

    def test_duplicate_thread_identity_and_its_descendants_are_excluded(self):
        self.header("project.jsonl", {"id": "duplicate", "cwd": str(self.project)})
        self.header("other.jsonl", {"id": "duplicate", "cwd": str(self.other)})
        self.header("child.jsonl", {"id": "child", "cwd": str(self.other), "parent_thread_id": "duplicate",
                                      "source": {"subagent": {"thread_spawn": {
                                          "parent_thread_id": "duplicate", "agent_path": "worker"}}}})

        result = sources(self.sessions, self.project)

        self.assertEqual(result["sources"], [])
        self.assertEqual(result["ambiguous_thread_id_count"], 1)
        self.assertEqual(result["excluded_thread_ids"], ["child", "duplicate"])


if __name__ == "__main__":
    unittest.main()
