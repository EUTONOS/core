"""The "What EUTONOS offers" dashboard page: its read-only service route works with no project, serves the list shipped
inside the package, refuses a damaged copy in plain words and offers no way to change anything."""
from __future__ import annotations

from copy import deepcopy
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from eutonos import dashboard, tokenslang as ts
from eutonos.dashboard_cli import app

ROOT = Path(__file__).resolve().parent.parent
SHIPPED = json.loads((ROOT / "src" / "eutonos" / "offers" / "offers.json").read_text(encoding="utf-8"))


class FakeFiles:
    """Stands in for ``importlib.resources.files("eutonos")`` so a damaged copy of the list can be served."""

    def __init__(self, data):
        self.data = data

    def joinpath(self, name):
        self.name = name
        return self

    def read_bytes(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data if isinstance(self.data, bytes) else json.dumps(self.data).encode("utf-8")


def damaged(data):
    return mock.patch("eutonos.dashboard.files", side_effect=lambda package: FakeFiles(data))


def reply(request, **kwargs):
    try:
        with urlopen(request, timeout=20, **kwargs) as response:
            return response.status, response.headers, response.read()
    except HTTPError as error:
        try:
            return error.code, error.headers, error.read()
        finally:
            error.close()


class Served(unittest.TestCase):
    def setUp(self):
        # Nothing here may read or write a real EUTONOS home: point it at a temporary folder and skip the model watch.
        tmp = tempfile.TemporaryDirectory(prefix="eutonos-offers-")
        self.addCleanup(tmp.cleanup)
        for patch in (mock.patch.dict(os.environ, {"EUTONOS_HOME": str(Path(tmp.name) / "eutonos-home")}),
                      mock.patch("eutonos.dashboard_cli._model_watch_hint", return_value={})):
            patch.start()
            self.addCleanup(patch.stop)

    def serve(self, *roots):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app(list(roots)))
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"


class OffersRoute(Served):
    def test_with_no_project_open_the_shipped_list_is_served(self):
        status, headers, body = reply(self.serve() + "/api/v1/offers")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "application/json")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(json.loads(body.decode("utf-8")), SHIPPED)
        self.assertEqual(json.loads(body.decode("utf-8")), dashboard.offers())

    def test_the_answer_does_not_depend_on_which_project_is_open(self):
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            bodies = {reply(self.serve(*roots) + "/api/v1/offers")[2] for roots in ((), (one,), (one, two))}
        self.assertEqual(len(bodies), 1)

    def test_it_takes_no_query_and_only_local_hosts_are_answered(self):
        base = self.serve()
        for query in ("?project=0", "?refresh=1", "?"):
            status, _, body = reply(base + "/api/v1/offers" + query)
            if query == "?":  # an empty query string carries nothing
                self.assertEqual(status, 200)
            else:
                self.assertEqual((status, json.loads(body)["outcome"]), (400, "invalid-request"), query)
        status, _, _ = reply(Request(base + "/api/v1/offers", headers={"Host": "attacker.invalid"}))
        self.assertEqual(status, 403)

    def test_nothing_can_be_changed_through_it(self):
        base = self.serve()
        status, _, _ = reply(Request(base + "/api/v1/offers", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"}))
        self.assertEqual(status, 403)  # no token, no Origin
        token = json.loads(reply(base + "/api/v1/snapshot")[2])["settings_token"]
        host = base.removeprefix("http://")
        status, _, body = reply(Request(base + "/api/v1/offers", data=b"{}", method="POST",
                                        headers={"Content-Type": "application/json", "Origin": base, "Host": host,
                                                 "X-Eutonos-Settings": token}))
        self.assertEqual((status, json.loads(body)["outcome"]), (404, "not-found"))  # the route is not in the write list
        for method in ("PUT", "DELETE"):
            status, _, _ = reply(Request(base + "/api/v1/offers", method=method))
            self.assertGreaterEqual(status, 400, method)

    def test_the_list_is_not_reachable_as_a_static_file_and_the_page_is_still_the_only_html(self):
        base = self.serve()
        for path in ("/offers/offers.json", "/offers.json", "/src/eutonos/offers/offers.json"):
            self.assertEqual(reply(base + path)[0], 404, path)
        with self.assertRaises(ts.ReadError):
            dashboard.static("offers.json")
        status, _, body = reply(base + "/")
        self.assertEqual(status, 200)
        self.assertIn('<a href="#offers" data-view="offers">What EUTONOS offers</a>', body.decode("utf-8"))

    def test_a_damaged_copy_is_refused_with_a_plain_message_and_no_path_or_trace(self):
        base = self.serve()
        for data in (OSError("C:/secret/place/offers.json is locked"), b"not json at all", b"\xff\xfe"):
            with self.subTest(data=repr(data)), damaged(data):
                status, headers, body = reply(base + "/api/v1/offers")
            self.assertEqual(status, 500)
            self.assertEqual(headers.get_content_type(), "application/json")
            value = json.loads(body.decode("utf-8"))
            self.assertEqual(value["outcome"], "unavailable")
            self.assertEqual(value["message"], "The list of what EUTONOS offers is missing or damaged in this installation.")
            self.assertNotIn("secret", body.decode("utf-8"))
            self.assertNotIn("Traceback", body.decode("utf-8"))


class OffersReader(unittest.TestCase):
    def refused(self, data):
        with damaged(data), self.assertRaises(ts.ReadError) as caught:
            dashboard.offers()
        return caught.exception

    def changed(self, edit):
        data = deepcopy(SHIPPED)
        edit(data)
        return data

    def test_the_shipped_file_is_accepted_whole(self):
        self.assertEqual(dashboard.offers(), SHIPPED)
        with damaged(SHIPPED):
            self.assertEqual(dashboard.offers(), SHIPPED)

    def test_a_missing_or_unreadable_file_says_so(self):
        for data in (FileNotFoundError("gone"), PermissionError("no"), b"{", b""):
            self.assertEqual(self.refused(data).code, "unavailable")

    def test_each_wrong_shape_is_refused_not_shown_half_read(self):
        feature = next(row for row in SHIPPED["items"] if row["type"] == "feature")
        capability = next(row for row in SHIPPED["items"] if row["type"] == "capability")
        cases = {
            "an unknown profile": lambda d: d.update(profile="eutonos.offers.v0"),
            "no items list": lambda d: d.update(items={}),
            "a missing field": lambda d: d["items"][0].pop("summary"),
            "an empty title": lambda d: d["items"][0].update(title=""),
            "a number for text": lambda d: d["items"][0].update(summary=5),
            "an unknown status": lambda d: d["items"][0].update(status="shipped"),
            "a status that is not a list status": lambda d: d["items"][0].update(status="active"),
            "a section that does not exist": lambda d: d["items"][0].update(horizon="past"),
            "a type that does not exist": lambda d: d["items"][0].update(type="slice"),
            "a repeated id": lambda d: d["items"].append(deepcopy(d["items"][0])),
            "a feature with a features list": lambda d: next(r for r in d["items"] if r["id"] == feature["id"]).update(features=[]),
            "a capability without one": lambda d: next(r for r in d["items"] if r["id"] == capability["id"]).pop("features"),
            "links that are not text": lambda d: next(r for r in d["items"] if r["id"] == capability["id"]).update(features=[1]),
            "an item that is not an object": lambda d: d["items"].append("text"),
        }
        for name, edit in cases.items():
            with self.subTest(name):
                self.assertEqual(self.refused(self.changed(edit)).code, "invalid")

    def test_the_whole_file_may_be_empty_of_items(self):
        with damaged({"profile": "eutonos.offers.v1", "items": []}):
            self.assertEqual(dashboard.offers()["items"], [])


if __name__ == "__main__":
    unittest.main()
