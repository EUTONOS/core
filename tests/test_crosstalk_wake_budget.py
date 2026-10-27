"""Offline tests: the wake budget (Crosstalk rework slice 5, thought experiment 6).

A wake is a Claude letter wake, a Codex headless resume or a Codex link open. The budget is per chat and in total over
a rolling window, lives in controls, and is counted under the state lock at the moment a wake is decided. Temp device
folder only; the clock is passed in, so nothing sleeps."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock


KIT = Path(__file__).resolve().parents[1] / "tools" / "crosstalk"
spec = importlib.util.spec_from_file_location("crosstalk_budget_core", KIT / "crosstalk_p0_core.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
with mock.patch.dict("sys.modules", {"crosstalk_p0_core": core}):
    cli_spec = importlib.util.spec_from_file_location("crosstalk_budget_cli", KIT / "crosstalk_p0_cli.py")
    cli = importlib.util.module_from_spec(cli_spec)
    cli_spec.loader.exec_module(cli)

CHAT_A = "claude:cccc0001-2222-4333-8444-555555555555"
CHAT_B = "claude:cccc0002-2222-4333-8444-555555555555"
CODEX = "codex:aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
SECOND = 1_000_000_000
HOUR = 3600 * SECOND
T0 = 1_800_000_000 * SECOND


class BudgetBase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="crosstalk-budget-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = mock.patch.dict(os.environ, {"CROSSTALK_P0_DEVICE_DIR": str(self.root / "device")})
        env.start()
        self.addCleanup(env.stop)
        core.controls_update(messaging="on", halt=False)

    def claim(self, key, at_s, kind="claude-letter"):
        return core.wake_budget_claim(key, kind, T0 + int(at_s * SECOND))

    def notices(self):
        return {key: row for key, row in core.helper_notices_load()["notices"].items()}


class ControlsTests(BudgetBase):
    def test_defaults_are_six_per_chat_and_twenty_in_total_per_hour(self):
        self.assertEqual(core.controls_load()["wake_budget"], {"per_chat": 6, "total": 20, "window_s": 3600})

    def test_the_owner_changes_it_through_controls_and_it_is_stored_with_them(self):
        core.controls_update(wake_budget={"per_chat": 2, "total": 5})
        self.assertEqual(core.controls_load()["wake_budget"], {"per_chat": 2, "total": 5, "window_s": 3600})
        stored = json.loads((core.device_dir() / "controls.json").read_text())
        self.assertEqual(stored["wake_budget"]["per_chat"], 2)
        core.controls_update(halt=True)  # other control changes keep it
        self.assertEqual(core.controls_load()["wake_budget"]["per_chat"], 2)

    def test_values_out_of_range_or_not_whole_numbers_are_refused_and_change_nothing(self):
        for bad in ({"per_chat": -1}, {"total": 10 ** 6}, {"window_s": 5}, {"per_chat": 1.5}, {"per_chat": True}):
            with self.subTest(bad=bad), self.assertRaises(core.ProbeError):
                core.controls_update(wake_budget=bad)
        self.assertEqual(core.controls_load()["wake_budget"], core.WAKE_BUDGET_DEFAULT)

    def test_a_damaged_stored_value_reads_as_the_default(self):
        (core.device_dir()).mkdir(parents=True, exist_ok=True)
        (core.device_dir() / "controls.json").write_text(json.dumps(
            {"messaging": "on", "wake_budget": {"per_chat": "many", "total": -3, "window_s": 120}}))
        self.assertEqual(core.controls_load()["wake_budget"], {"per_chat": 6, "total": 20, "window_s": 120})

    def test_the_cli_shows_and_changes_the_budget(self):
        def run(*argv):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cli.main(list(argv))
            return code, json.loads(buffer.getvalue())
        code, shown = run("wake-budget")
        self.assertEqual((code, shown["wake_budget"]["per_chat"], shown["wake_budget"]["used_total"]), (0, 6, 0))
        code, changed = run("wake-budget", "--per-chat", "3", "--total", "9")
        self.assertEqual((changed["wake_budget"]["per_chat"], changed["wake_budget"]["total"]), (3, 9))
        self.assertEqual(core.controls_load()["wake_budget"]["per_chat"], 3)
        code, refused = run("wake-budget", "--total", "-4")
        self.assertEqual((code, refused["reason"]), (1, "wake-budget-out-of-range"))
        self.assertEqual(core.controls_load()["wake_budget"]["total"], 9)


class ClaimTests(BudgetBase):
    def test_a_chat_is_woken_up_to_its_budget_and_then_waits(self):
        core.controls_update(wake_budget={"per_chat": 3})
        for number in range(3):
            self.assertTrue(self.claim(CHAT_A, number * 120)["allowed"], number)  # past the batch window each time
        refused = self.claim(CHAT_A, 400)
        self.assertEqual((refused["allowed"], refused["reason"]), (False, "wake-budget-chat"))
        self.assertTrue(self.claim(CHAT_B, 400)["allowed"])  # another chat has its own budget

    def test_the_total_budget_stops_every_chat(self):
        core.controls_update(wake_budget={"per_chat": 5, "total": 4})
        keys = [f"claude:cccc{n:04d}-2222-4333-8444-555555555555" for n in range(6)]
        results = [self.claim(key, n * 5)["allowed"] for n, key in enumerate(keys)]
        self.assertEqual(results, [True, True, True, True, False, False])
        self.assertEqual(self.claim(keys[4], 100)["reason"], "wake-budget-total")

    def test_letters_to_one_chat_inside_sixty_seconds_batch_into_one_wake(self):
        first = self.claim(CHAT_A, 0)
        second = self.claim(CHAT_A, 30)
        third = self.claim(CHAT_A, 59)
        self.assertEqual((first["batched"], second["batched"], third["batched"]), (False, True, True))
        self.assertEqual(core.wake_budget_status(T0 + 60 * SECOND)["used_total"], 1)  # one wake used the budget once
        after = self.claim(CHAT_A, 61)  # the window is anchored to the counted wake, so this is a new wake
        self.assertFalse(after["batched"])
        self.assertEqual(core.wake_budget_status(T0 + 62 * SECOND)["used_per_chat"], {CHAT_A: 2})

    def test_a_batched_wake_is_allowed_even_when_the_chat_is_at_its_budget(self):
        core.controls_update(wake_budget={"per_chat": 1})
        self.assertTrue(self.claim(CHAT_A, 0)["allowed"])
        self.assertTrue(self.claim(CHAT_A, 10)["allowed"])  # same wake
        self.assertFalse(self.claim(CHAT_A, 100)["allowed"])

    def test_the_budget_frees_as_the_window_rolls(self):
        core.controls_update(wake_budget={"per_chat": 2, "total": 10})
        self.assertTrue(self.claim(CHAT_A, 0)["allowed"])
        self.assertTrue(self.claim(CHAT_A, 100)["allowed"])
        self.assertFalse(self.claim(CHAT_A, 1000)["allowed"])
        self.assertFalse(self.claim(CHAT_A, 3599)["allowed"])  # the first wake is still inside the hour
        self.assertTrue(self.claim(CHAT_A, 3601)["allowed"])  # the first wake rolled out: one slot is free
        self.assertFalse(self.claim(CHAT_A, 3680)["allowed"])  # the second is still in
        self.assertTrue(self.claim(CHAT_A, 3702)["allowed"])  # and now the second has rolled out too

    def test_a_wake_that_woke_nothing_gives_its_slot_back(self):
        core.controls_update(wake_budget={"per_chat": 1})
        first = self.claim(CHAT_A, 0)
        self.assertTrue(core.wake_budget_refund(first["event"]))
        self.assertFalse(core.wake_budget_refund(first["event"]))  # once
        self.assertTrue(self.claim(CHAT_A, 200)["allowed"])
        self.assertFalse(core.wake_budget_refund(None))

    def test_room_is_a_read_only_question(self):
        core.controls_update(wake_budget={"per_chat": 1})
        self.assertTrue(core.wake_budget_room(CHAT_A, T0))
        self.assertEqual(core.wake_budget_status(T0)["used_total"], 0)
        self.claim(CHAT_A, 0)
        self.assertFalse(core.wake_budget_room(CHAT_A, T0 + 100 * SECOND))
        self.assertTrue(core.wake_budget_room(CHAT_A, T0 + 10 * SECOND))  # inside the batch window
        self.assertTrue(core.wake_budget_room(CODEX, T0 + 100 * SECOND))  # a chat of its own has room

    def test_two_processes_cannot_both_take_the_last_slot(self):
        core.controls_update(wake_budget={"per_chat": 5, "total": 1})
        results = []
        keys = [f"claude:cccc{n:04d}-2222-4333-8444-555555555555" for n in range(5)]
        threads = [threading.Thread(target=lambda key=key: results.append(self.claim(key, 100)["allowed"]))
                   for key in keys]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), [False, False, False, False, True])


class NoticeTests(BudgetBase):
    def test_one_notice_opens_on_the_first_refusal_and_stays_one(self):
        core.controls_update(wake_budget={"per_chat": 1})
        self.claim(CHAT_A, 0)
        self.assertNotIn("wake-budget", self.notices())
        self.claim(CHAT_A, 100)
        [first] = [row for key, row in self.notices().items() if key == "wake-budget"]
        for at in (200, 300, 400):
            self.claim(CHAT_A, at)
        self.claim(CHAT_B, 450)
        self.claim(CHAT_B, 600)
        again = self.notices()["wake-budget"]
        self.assertEqual(again, first)  # the same text, the same first-seen time: never reopened, never doubled
        self.assertEqual(len(core.helper_notices_load()["notices"]), 1)
        self.assertIn("nothing is lost or sent twice", first["message"])

    def test_the_notice_clears_when_the_refusals_age_out_or_the_chat_is_woken(self):
        core.controls_update(wake_budget={"per_chat": 1})
        self.claim(CHAT_A, 0)
        self.claim(CHAT_A, 100)
        self.assertIn("wake-budget", self.notices())
        self.assertTrue(core.wake_budget_notice_sync(T0 + 200 * SECOND))
        self.assertIn("wake-budget", self.notices())
        self.assertFalse(core.wake_budget_notice_sync(T0 + 100 * SECOND + HOUR + SECOND))
        self.assertNotIn("wake-budget", self.notices())
        self.claim(CHAT_B, 4000)
        self.claim(CHAT_B, 4100)  # a new refusal opens it again, and a later wake of that chat closes it
        self.assertIn("wake-budget", self.notices())
        core.controls_update(wake_budget={"per_chat": 5})
        self.assertTrue(self.claim(CHAT_B, 4200)["allowed"])
        self.assertNotIn("wake-budget", self.notices())


if __name__ == "__main__":
    unittest.main()
