"""Offline tests: mark_stale retires old unconfirmed letters without ever resending them."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock


KIT = Path(__file__).resolve().parents[1] / "tools" / "crosstalk"
spec = importlib.util.spec_from_file_location("crosstalk_mark_stale_core", KIT / "crosstalk_p0_core.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

HOUR_NS = 3600 * 10**9


class MarkStaleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="crosstalk-mark-stale-test-")
        self.addCleanup(temporary.cleanup)
        env = mock.patch.dict(os.environ, {"CROSSTALK_P0_DEVICE_DIR": str(Path(temporary.name) / "device")})
        env.start()
        self.addCleanup(env.stop)

    def letter(self, letter_id, state, age_hours):
        created = time.time_ns() - int(age_hours * HOUR_NS)
        core.journal_append(core._letter_row(letter_id, "journaled", to={"app": "codex", "id": "t"},
                                             created_ns=created))
        if state != "journaled":
            core.journal_append(core._letter_row(letter_id, state, sent=True))

    def test_only_old_unconfirmed_letters_are_retired_and_nothing_is_sent(self):
        self.letter("CTP0-old-held", "held", 30)
        self.letter("CTP0-old-uncertain", "uncertain", 30)
        self.letter("CTP0-new-held", "held", 1)
        self.letter("CTP0-old-arrived", "arrived", 30)
        with mock.patch.object(core, "attempt_delivery") as send:
            result = core.mark_stale(24)
            core.reconcile(resend=True)
        send.assert_not_called()
        self.assertEqual(sorted(result["marked"]), ["CTP0-old-held", "CTP0-old-uncertain"])
        states = {k: v["state"] for k, v in core.letters_load().items()}
        self.assertEqual(states["CTP0-old-held"], "stale-unconfirmed")
        self.assertEqual(states["CTP0-new-held"], "held")
        self.assertEqual(states["CTP0-old-arrived"], "arrived")
        self.assertEqual(core.letter_summary()["pending_count"], 1)
        self.assertEqual(core.mark_stale(24)["marked"], [])


if __name__ == "__main__":
    unittest.main()
