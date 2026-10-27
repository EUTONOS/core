"""Offline tests: the Codex Crosstalk tool server reloads a changed core between calls."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest import mock


KIT = Path(__file__).resolve().parents[1] / "tools" / "crosstalk"


def load_server():
    spec = importlib.util.spec_from_file_location("crosstalk_codex_server_test", KIT / "codex_tool_server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    return server


class ReloadTests(unittest.TestCase):
    def setUp(self):
        self.server = load_server()
        self.addCleanup(sys.modules.pop, "crosstalk_p0_core", None)

    def test_changed_core_reloads_only_when_no_call_is_running(self):
        server = self.server
        with mock.patch.object(server.importlib, "reload") as reload:
            server.enter_call()  # unchanged: no reload
            server.leave_call()
            self.assertEqual(reload.call_count, 0)
            server.CORE_STATE["mtime_ns"] = -1  # pretend the file changed
            server.enter_call()  # first call in: reloads
            self.assertEqual(reload.call_count, 1)
            server.CORE_STATE["mtime_ns"] = -2
            server.enter_call()  # another call is running: no reload yet
            self.assertEqual(reload.call_count, 1)
            server.leave_call()
            server.leave_call()
            self.assertEqual(server.CORE_STATE["in_flight"], 0)
            server.enter_call()
            server.leave_call()
            self.assertEqual(reload.call_count, 2)
            self.assertEqual(server.CORE_STATE["mtime_ns"], os.stat(server.core.__file__).st_mtime_ns)

    def test_failed_reload_keeps_serving(self):
        server = self.server
        server.CORE_STATE["mtime_ns"] = -1
        with mock.patch.object(server.importlib, "reload", side_effect=SyntaxError("x")), \
                mock.patch.object(server.sys, "stderr"):
            server.enter_call()
            server.leave_call()
        self.assertEqual(server.CORE_STATE["mtime_ns"], -1)
        self.assertEqual(server.CORE_STATE["in_flight"], 0)


if __name__ == "__main__":
    unittest.main()
