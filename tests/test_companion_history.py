import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from eutonos import companion_history as history
from eutonos import settings


class CompanionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        history.cache_clear()
        self.clock = iter(["2026-09-12T10:00:00Z", "2026-09-12T10:05:00Z", "2026-09-12T10:10:00Z"])
        self.original_now = history._now
        history._now = lambda: next(self.clock)
        self.addCleanup(self._restore_clock)

    def _restore_clock(self):
        history._now = self.original_now
        history.cache_clear()

    def row(self, row_id="thread:turn", start="2026-09-12T09:00:00Z"):
        return {"id": row_id, "thread_id": "thread", "turn_id": "turn", "role": "support", "efforts": ["medium"],
                "model": "gpt-6-luna", "models": ["gpt-6-luna"], "experiment_id": "exp-1",
                "stage": "PREPARE", "finding": "bounded", "followup_reads": [{"source": "docs/X.md", "reason": "verification"}],
                "source_prefix_sha256": "a" * 64, "final_sha256": "b" * 64,
                "usage": {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 4, "reasoning_output_tokens": 1},
                "handoff_tokens": 3,
                "start": start, "state": "completed", "untrusted_transcript": "drop me",
                "native_log_path": "C:/private/native.json"}

    def test_off_preserves_rows_and_resume_skips_off_started_tasks(self):
        first = history.sync(self.temp.name, [self.row("first")])
        self.assertEqual(first["changed"], 1)
        history.set_tracking(self.temp.name, False)
        history.sync(self.temp.name, [self.row("off", "2026-09-12T10:02:00Z")])
        tracking = history.set_tracking(self.temp.name, True)
        self.assertFalse(any(window["ended_at"] is None for window in tracking["windows"]))
        result = history.sync(self.temp.name, [self.row("off", "2026-09-12T10:02:00Z"), self.row("after", "2026-09-12T10:06:00Z")])
        self.assertEqual([row["id"] for row in result["rows"]], ["after", "first"])

    def test_fractional_timestamps_use_aware_window_comparison(self):
        history.set_tracking(self.temp.name, False)
        history.set_tracking(self.temp.name, True)
        state = history.tracking(self.temp.name)
        self.assertEqual(state["windows"][0]["started_at"], "2026-09-12T10:00:00Z")
        result = history.sync(self.temp.name, [self.row("fractional", "2026-09-12T10:02:00.500000+00:00")])
        self.assertEqual(result["rows"], [])

    def test_dedup_and_idempotency(self):
        row = self.row()
        first = history.sync(self.temp.name, [row])
        second = history.sync(self.temp.name, [row])
        self.assertEqual(first["changed"], 1)
        self.assertEqual(second["changed"], 0)
        self.assertEqual(len(second["rows"]), 1)

    def test_separate_process_history_and_settings_updates_preserve_each_owner(self):
        root = Path(self.temp.name)
        source = Path(__file__).resolve().parents[1] / "src"
        revision = settings.status(root)["revision"]
        row = self.row("independent")
        program = (
            "import json, sys; sys.path.insert(0, sys.argv[1]); "
            "from eutonos import settings, companion_history; "
            "\nif sys.argv[2] == 'settings':\n settings.update(sys.argv[3], 0, sys.argv[4], 'ram', json.loads(sys.argv[5]))"
            "\nelse:\n companion_history.sync(sys.argv[3], [json.loads(sys.argv[5])])"
        )
        settings_value = json.dumps({"retrieval": "off", "capture": False})
        commands = (
            [sys.executable, "-c", program, str(source), "settings", str(root), revision, settings_value],
            [sys.executable, "-c", program, str(source), "history", str(root), revision, json.dumps(row)],
        )
        processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for command in commands]
        results = [process.communicate(timeout=10) for process in processes]
        self.assertEqual([process.returncode for process in processes], [0, 0], results)
        self.assertEqual(settings.status(root)["ram"], {"retrieval": "off", "capture": False,
                                                        "cadence": "every-turn", "detail": "summary"})
        self.assertEqual([item["id"] for item in history.sync(root, []) ["rows"]], ["independent"])

    def test_privacy_whitelist_and_absolute_paths(self):
        saved = history.sync(self.temp.name, [self.row()])["rows"][0]
        self.assertNotIn("untrusted_transcript", saved)
        self.assertNotIn("native_log_path", saved)
        self.assertEqual(saved["followup_reads"][0]["source"], "docs/X.md")
        self.assertEqual(saved["handoff_tokens"], 3)
        self.assertEqual(set(saved["usage"]), {"input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"})
        self.assertEqual(saved["efforts"], ["medium"])

    def test_cache_reuses_unchanged_stat_and_rereads_changed_stat(self):
        source = Path(self.temp.name) / "selected.jsonl"
        source.write_text("one\n", encoding="utf-8")
        calls = []

        def reader(*args):
            calls.append(args)
            return ([{"id": len(calls)}], ["diagnostic"])

        first = history.cached_read(reader, source, "support", "exp-1", None, None)
        first[0][0]["id"] = "mutated"
        second = history.cached_read(reader, source, "support", "exp-1", None, None)
        self.assertEqual(len(calls), 1)
        self.assertEqual(second[0][0]["id"], 1)
        before_rewrite = source.stat()
        source.write_text("two\n", encoding="utf-8")
        # Same-length writes can retain a coarse filesystem timestamp. The cache
        # intentionally keys from source stat, so make this fixture's changed
        # stat explicit instead of relying on host timestamp granularity.
        changed = source.stat()
        os.utime(source, ns=(changed.st_atime_ns, before_rewrite.st_mtime_ns + 1_000_000_000))
        history.cached_read(reader, source, "support", "exp-1", None, None)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(history._CACHE), 1)


if __name__ == "__main__":
    unittest.main()
