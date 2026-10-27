"""Luna's owner floor reaches native launch plans and refuses before reservation."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from eutonos import role_router, worker_models


class LunaDispatchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.home = self.root / "codex-home"
        self.home.mkdir()
        environment = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home),
                                                   "EUTONOS_HOME": str(self.root / "eutonos-home")})
        environment.start()
        self.addCleanup(environment.stop)
        (self.home / "models_cache.json").write_text(json.dumps({"models": [{
            "slug": "gpt-6-luna", "visibility": "list", "supported_reasoning_levels": [
                {"effort": effort} for effort in ("low", "medium", "high", "xhigh", "max")]}]}),
            encoding="utf-8")

    def request(self, mode="new", **overrides):
        return {"id": "request-one", "mode": mode, "model": "luna", "to": "codex", **overrides}

    def test_omitted_luna_effort_reaches_new_codex_argv(self):
        request = self.request()
        plan = worker_models.plan_launch(request, harness="codex", root=self.root)
        self.assertNotIn("effort", request)
        self.assertEqual(plan["effort"], "xhigh")

    def test_omitted_role_effort_reaches_native_spawn(self):
        option = {"harness": "codex", "model": "luna", "effort": None}
        role = {"options": [option], "preamble": None, "autonomy": "ask"}
        with mock.patch.object(role_router.settings, "read_roles", return_value={"roles": {"worker": role}}):
            resolved = role_router.resolve_role(self.root, "worker")
        self.assertEqual(resolved["effort"], "xhigh")
        self.assertEqual(option["effort"], None)

    def record(self):
        session = "11111111-2222-4333-8444-555555555555"
        request = self.request()
        plan = worker_models.plan_new(request, harness="codex", root=self.root)
        worker_models.record_new(self.root, session_id=session, harness="codex", req=request, plan=plan)
        worker_models.finalize(self.root, session_id=session, req=request)
        return session

    def test_resume_omission_supplies_xhigh_without_switching_model(self):
        session = self.record()
        request = self.request("resume", session_id=session, id="request-two")
        plan = worker_models.plan_launch(request, harness="codex", root=self.root)
        self.assertEqual(plan["effort"], "xhigh")
        self.assertEqual(plan["resolved"], "gpt-6-luna")

    def test_explicit_low_resume_refuses_without_leaking_busy(self):
        session = self.record()
        for effort in ("low", "medium", "high"):
            with self.subTest(effort=effort), self.assertRaises(worker_models.WorkerModelError):
                worker_models.plan_resume(self.request("resume", session_id=session, effort=effort,
                                                       id="request-two"), harness="codex", root=self.root)
            self.assertFalse(worker_models.load_entry(self.root, session)["busy"])


if __name__ == "__main__":
    unittest.main()
