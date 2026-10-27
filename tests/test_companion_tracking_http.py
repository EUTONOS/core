import json
import os
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from eutonos import companion_history, dashboard
from eutonos.dashboard_cli import app


class CompanionTrackingHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eutonos-tracking-http-")
        self.addCleanup(self.temp.cleanup)
        environment = patch.dict(os.environ, {"EUTONOS_HOME": str(Path(self.temp.name) / "eutonos-home")})
        environment.start()
        self.addCleanup(environment.stop)
        self.root = Path(self.temp.name) / "selected"
        self.root.mkdir()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app([self.root]))
        self.addCleanup(self._close_server)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = f"127.0.0.1:{self.server.server_port}"
        with patch.object(dashboard, "snapshot", return_value={"projects": []}):
            status, body = self.request("GET", "/api/v1/snapshot")
        self.assertEqual(status, 200)
        self.token = body["settings_token"]

    def _close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, headers=None, payload=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            body = None if payload is None else json.dumps(payload).encode("utf-8")
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            raw = response.read()
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                decoded = raw
            return response.status, decoded
        finally:
            connection.close()

    def auth_headers(self, **overrides):
        headers = {
            "Host": self.host,
            "Origin": "http://" + self.host,
            "X-Eutonos-Settings": self.token,
            "Content-Type": "application/json",
        }
        headers.update(overrides)
        if headers.get("X-Eutonos-Settings") is None:
            headers.pop("X-Eutonos-Settings", None)
        return headers

    def assert_no_history(self):
        self.assertFalse((self.root / ".eutonos-local" / "companion-history.json").exists())

    def test_missing_token_is_denied_without_history(self):
        status, body = self.request("POST", "/api/v1/companion/tracking",
                                    self.auth_headers(**{"X-Eutonos-Settings": None}),
                                    {"project": 0, "enabled": False})
        self.assertEqual(status, 403)
        self.assertEqual(body["outcome"], "denied")
        self.assert_no_history()

    def test_foreign_origin_is_denied_without_history(self):
        status, body = self.request("POST", "/api/v1/companion/tracking",
                                    self.auth_headers(Origin="http://localhost:" + str(self.server.server_port)),
                                    {"project": 0, "enabled": False})
        self.assertEqual(status, 403)
        self.assertEqual(body["outcome"], "denied")
        self.assert_no_history()

    def test_foreign_host_is_denied_without_history(self):
        status, body = self.request("POST", "/api/v1/companion/tracking",
                                    self.auth_headers(Host="example.invalid", Origin="http://example.invalid"),
                                    {"project": 0, "enabled": False})
        self.assertEqual(status, 403)
        self.assertEqual(body["outcome"], "denied")
        self.assert_no_history()

    def test_invalid_project_index_is_rejected_without_history(self):
        status, body = self.request("POST", "/api/v1/companion/tracking", self.auth_headers(),
                                    {"project": 1, "enabled": False})
        self.assertEqual(status, 400)
        self.assertEqual(body["outcome"], "invalid-settings")
        self.assert_no_history()

    def test_unknown_fields_are_rejected_without_history(self):
        status, body = self.request("POST", "/api/v1/companion/tracking", self.auth_headers(),
                                    {"project": 0, "enabled": False, "extra": 1})
        self.assertEqual(status, 400)
        self.assertEqual(body["outcome"], "invalid-settings")
        self.assert_no_history()

    def test_valid_request_persists_disabled_tracking_and_reads_back(self):
        status, body = self.request("POST", "/api/v1/companion/tracking", self.auth_headers(),
                                    {"project": 0, "enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(body["enabled"])
        self.assertFalse(companion_history.tracking(self.root)["enabled"])
        self.assertTrue((self.root / ".eutonos-local" / "companion-history.json").exists())


if __name__ == "__main__":
    unittest.main()
