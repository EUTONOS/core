import unittest

from tests.runtime_support import RuntimeGuardedTestCase

from screensaver_control import recover

class ScreenSaverTests(RuntimeGuardedTestCase):
    def test_inactive_does_not_stop_tasks(self):
        state = {"screensaver_running": False, "input_ready": True}
        result = recover(lambda: state, lambda: self.fail("must not stop"))
        self.assertTrue(result["recovered"])

    def test_unknown_does_not_stop_tasks(self):
        state = {"screensaver_running": None, "input_ready": False}
        self.assertFalse(recover(lambda: state, lambda: self.fail("must not stop"))["recovered"])

    def test_stop_requires_independent_readback(self):
        states = iter([{"screensaver_running": True, "input_ready": False},
                       {"screensaver_running": False, "input_ready": True}])
        result = recover(lambda: next(states), lambda: [123])
        self.assertTrue(result["recovered"])
        self.assertEqual(result["stopped_pids"], [123])

    def test_locked_after_stop_is_not_success(self):
        states = [{"screensaver_running": True, "input_ready": False}]
        def probe():
            return states.pop() if states else {"screensaver_running": False, "input_ready": False}
        result = recover(probe, lambda: [123], lambda seconds: None)
        self.assertFalse(result["recovered"])
