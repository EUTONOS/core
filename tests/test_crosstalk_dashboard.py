"""Slice 3B.7: owner controls, journal metadata, ping-pong snapshot and the dashboard's
Crosstalk feeds/routes. Every test uses a temp device folder (CROSSTALK_P0_DEVICE_DIR
patched in-test, bound at call time), a faked CLI runner and no live Crosstalk."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from eutonos import crosstalk_dashboard_feeds as feeds
from eutonos import crosstalk_status as cs
from eutonos.dashboard_cli import app


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="eutonos-ct-dash-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "project"
        self.root.mkdir()
        self.device = Path(temp.name) / "device"
        env = mock.patch.dict(os.environ, {cs.DEVICE_DIR_ENV: str(self.device),
                                           "EUTONOS_HOME": str(Path(temp.name) / "home")})
        env.start()
        self.addCleanup(env.stop)

    def controls(self, **value):
        self.device.mkdir(exist_ok=True)
        (self.device / "controls.json").write_text(json.dumps(value), encoding="utf-8")

    def journal(self, *rows):
        self.device.mkdir(exist_ok=True)
        with open(self.device / "journal.jsonl", "w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")

    def checkout(self):
        cli = self.root / cs.CLI_RELATIVE
        cli.parent.mkdir(parents=True)
        cli.write_text("#\n", encoding="utf-8")


def letter(letter_id, state, ts_ns, **extra):
    return {"event": "letter", "letter_id": letter_id, "state": state, "ts_ns": ts_ns, **extra}


class RecentLettersTests(Base):
    def test_metadata_only_newest_first_and_no_bodies(self):
        self.journal(
            letter("a1", "journaled", 100, to={"app": "codex", "id": "T1"}, message="SECRET BODY",
                   text_sha256="x", **{"from": {"app": "claude", "id": "C1", "turn_id": None}}),
            letter("a1", "arrived", 300),
            letter("b2", "journaled", 200, to={"app": "hermes", "id": "H"}),
            {"event": "register", "ts_ns": 5})
        rows = cs.recent_letters()
        self.assertEqual([r["letter_id"] for r in rows], ["a1", "b2"])
        self.assertEqual(rows[0], {"letter_id": "a1", "from": "claude:C1", "to": "codex:T1",
                                   "state": "arrived", "ts_ns": 300})
        self.assertNotIn("SECRET", json.dumps(rows))

    def test_missing_or_damaged_journal_is_empty(self):
        self.assertEqual(cs.recent_letters(), [])
        self.device.mkdir()
        (self.device / "journal.jsonl").write_text("not json\n[1]\n", encoding="utf-8")
        self.assertEqual(cs.recent_letters(), [])

    def test_limit_applies(self):
        self.journal(*[letter(f"l{i}", "arrived", i + 1) for i in range(20)])
        self.assertEqual(len(cs.recent_letters(5)), 5)


class OwnerControlTests(Base):
    def run_cli(self, **after):
        calls = []

        def run(argv, timeout_seconds=None):
            calls.append(list(argv))
            self.controls(**after)
            return 0, "{}"
        return run, calls

    def test_halt_runs_the_cli_and_proves_the_control(self):
        self.checkout()
        run, calls = self.run_cli(messaging="on", halt=True, deny=["x"])
        with mock.patch.object(cs, "run_cli", run):
            result = cs.owner_control("halt", self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(result["controls"]["deny"], ["x"])
        self.assertEqual(calls[0][1:3], ["-I", "-B"])
        self.assertEqual(Path(calls[0][3]), self.root / cs.CLI_RELATIVE)
        self.assertEqual(calls[0][4], "halt")

    def test_unknown_command_never_runs_anything(self):
        with mock.patch.object(cs, "run_cli") as run:
            self.assertEqual(cs.owner_control("deny", self.root)["error"], "unknown-command")
        run.assert_not_called()

    def test_control_not_applied_is_a_failure(self):
        # Only halt has a degraded fallback (Stop never depends on the kit); the other
        # commands still report a control the CLI left unchanged as a failure.
        self.checkout()
        self.controls(messaging="on", halt=True)
        with mock.patch.object(cs, "run_cli", return_value=(0, "{}")):
            result = cs.owner_control("resume", self.root)
        self.assertEqual((result["ok"], result["error"]), (False, "crosstalk-control-not-applied"))

    def test_halt_not_applied_falls_back_to_a_degraded_halt(self):
        self.checkout()
        self.controls(messaging="on", halt=False)
        with mock.patch.object(cs, "run_cli", return_value=(0, "{}")):
            result = cs.owner_control("halt", self.root)
        self.assertTrue(result["ok"] and result["degraded"])
        self.assertIs(cs.controls()["halt"], True)

    def test_timeout_and_launch_failure(self):
        self.checkout()
        with mock.patch.object(cs, "run_cli", side_effect=subprocess.TimeoutExpired("x", 1)):
            self.assertEqual(cs.owner_control("resume", self.root)["error"], "crosstalk-timeout")
        with mock.patch.object(cs, "run_cli", side_effect=OSError("nope")):
            self.assertEqual(cs.owner_control("resume", self.root)["error"], "crosstalk-launch-failed")

    def test_enable_and_off_check_the_messaging_value(self):
        self.checkout()
        run, _ = self.run_cli(messaging="on", halt=False)
        with mock.patch.object(cs, "run_cli", run):
            self.assertTrue(cs.owner_control("enable", self.root)["ok"])
        run, _ = self.run_cli(messaging="off", halt=False)
        with mock.patch.object(cs, "run_cli", run):
            self.assertTrue(cs.owner_control("off", self.root)["ok"])


class PingpongTests(Base):
    def test_not_installed_is_inactive(self):
        self.assertEqual(cs.pingpong()["active"], False)
        self.assertFalse(self.device.exists())

    def test_recent_letter_is_active_old_or_refused_is_not(self):
        now = time.time()
        ns = int(now * 1e9)
        self.controls(messaging="on", halt=False)
        self.journal(letter("old", "arrived", ns - int(600e9)), letter("bad", "refused", ns))
        self.assertFalse(cs.pingpong(now=now)["active"])
        self.journal(letter("live", "sending", ns - int(5e9)))
        snap = cs.pingpong(now=now)
        self.assertEqual((snap["active"], snap["waiting"], snap["stopped"]), (True, 1, False))

    def test_halted_is_stopped_so_resume_stays_reachable(self):
        self.controls(messaging="on", halt=True)
        self.assertTrue(cs.pingpong()["stopped"])


class FeedsTests(Base):
    def test_status_reports_controls_and_recent_letters(self):
        self.checkout()
        self.controls(messaging="on", halt=True, deny=["codex:x", "claude:y"])
        self.journal(letter("a1", "held", 9, to={"app": "codex", "id": "T"}, message="BODY"))
        result = feeds.status(self.root)
        self.assertEqual((result["ok"], result["installed"], result["messaging"], result["halted"],
                          result["denied"]), (True, True, "on", True, 2))
        self.assertEqual(result["recent_letters"][0]["letter_id"], "a1")
        self.assertIn("HALTED", result["summary"])
        self.assertNotIn("BODY", json.dumps(result))

    def test_status_when_not_installed(self):
        result = feeds.status(self.root)
        self.assertTrue(result["ok"])
        self.assertFalse(result["installed"])
        self.assertEqual(result["recent_letters"], [])

    def test_action_validates_and_delegates(self):
        self.checkout()
        self.controls(messaging="on", halt=False)
        self.assertEqual(feeds.action(self.root, {"action": "prune-apply"})["error"], "unknown-action")
        self.assertEqual(feeds.action(self.root, {"action": "halt", "x": 1})["error"], "invalid-action")
        self.assertEqual(feeds.action(self.root, [])["error"], "invalid-action")
        with mock.patch.object(cs, "owner_control", return_value={"ok": True, "controls": {}}) as control:
            result = feeds.action(self.root, {"action": "halt"})
        self.assertTrue(result["ok"])
        control.assert_called_once_with("halt", self.root)


class CompanionReaderTests(Base):
    def collector(self):
        from eutonos import desktop_companion_data as data
        (self.root / ".eutonos-local").mkdir(exist_ok=True)
        host = self.root.parent / "host"
        host.mkdir(exist_ok=True)
        return data.Collector(self.root, host)

    def test_crosstalk_section_is_metadata_only_and_has_no_mailbox_section(self):
        self.controls(messaging="on", halt=True, deny=["a"])
        now = time.time()
        self.journal(letter("l1", "sending", int(now * 1e9), to={"app": "codex", "id": "T"}, message="BODY"),
                     letter("l2", "held", int(now * 1e9) - 5))
        snap = self.collector().collect(now)
        section = snap["crosstalk"]
        self.assertEqual((section["status"], section["messaging"], section["halted"], section["denied"],
                          section["in_flight"]), ("observed", "on", True, 1, 1))
        self.assertNotIn("BODY", json.dumps(snap))
        self.assertNotIn("mailbox", snap)
        self.assertEqual(snap["attention"], [])  # a held letter is not an attention item

    def test_missing_crosstalk_is_unknown_not_a_failure(self):
        snap = self.collector().collect(time.time())
        self.assertEqual(snap["crosstalk"]["status"], "missing")
        self.assertEqual(snap["health"], "unknown")

    def test_leftover_legacy_mailbox_folder_is_ignored(self):
        (self.root / ".eutonos-local" / "mailbox").mkdir(parents=True)
        snap = self.collector().collect(time.time())
        self.assertNotIn("mailbox", snap)
        self.assertIn("crosstalk", snap)

    def test_reader_launches_nothing(self):
        self.controls(messaging="on", halt=False)
        with mock.patch("subprocess.Popen", side_effect=AssertionError("no launches")), \
                mock.patch("subprocess.run", side_effect=AssertionError("no subprocesses")):
            self.collector().collect(time.time())


class RouteTests(Base):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app([self.root]))
        thread = Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), thread.join(timeout=2)))
        self.host = f"127.0.0.1:{self.server.server_port}"
        _, snapshot = self.request("GET", "/api/v1/snapshot", {"Host": self.host})
        self.token = snapshot["settings_token"]

    def request(self, method, path, headers=None, payload=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            body = json.dumps(payload).encode() if payload is not None else None
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def post(self, payload):
        headers = {"Host": self.host, "Origin": "http://" + self.host, "X-Eutonos-Settings": self.token,
                   "Content-Type": "application/json"}
        return self.request("POST", "/api/v1/crosstalk/action", headers, payload)

    def test_status_route(self):
        self.controls(messaging="on", halt=False)
        status, body = self.request("GET", "/api/v1/crosstalk/status?project=0", {"Host": self.host})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["messaging"], "on")

    def test_status_route_rejects_bad_queries(self):
        for query in ("", "?project=9", "?project=0&x=1"):
            status, _ = self.request("GET", "/api/v1/crosstalk/status" + query, {"Host": self.host})
            self.assertEqual(status, 400, query)

    def test_action_route_runs_the_control_and_needs_the_token(self):
        self.checkout_cli_root = None
        with mock.patch.object(cs, "owner_control", return_value={"ok": True, "controls": {"halt": True}}) as control:
            status, body = self.post({"project": 0, "action": "halt"})
        self.assertEqual((status, body["ok"]), (200, True))
        self.assertEqual(control.call_args.args[0], "halt")
        status, _ = self.request("POST", "/api/v1/crosstalk/action",
                                 {"Host": self.host, "Origin": "http://" + self.host,
                                  "Content-Type": "application/json"}, {"project": 0, "action": "halt"})
        self.assertEqual(status, 403)

    def test_action_route_bad_request_and_unknown_action(self):
        self.assertEqual(self.post({"action": "halt"})[0], 400)
        status, body = self.post({"project": 0, "action": "deny"})
        self.assertEqual((status, body["error"]), (200, "unknown-action"))


if __name__ == "__main__":
    unittest.main()
