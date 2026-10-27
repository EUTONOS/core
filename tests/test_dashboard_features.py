"""The dashboard machine pages (Settings > This computer): the read route and the guarded write routes.

Every write goes through the same guard as the other Settings writes (Host and Origin check, ``X-Eutonos-Settings``
token, the POST allowlist). Nothing here touches a real EUTONOS home: ``EUTONOS_HOME`` points at a temporary folder,
the project is a temporary folder, and the install/Crosstalk owners are mocked wherever a real call would need a full
install.
"""
from __future__ import annotations

from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest import mock

from eutonos import dashboard, dashboard_features, feature_settings, pending_actions
from eutonos.dashboard_cli import FEATURE_POSTS, app

ROOT = Path(__file__).resolve().parent.parent


class Served(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eutonos-features-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "eutonos-home"
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        for patch in (mock.patch.dict(os.environ, {"EUTONOS_HOME": str(self.home)}),
                      mock.patch("eutonos.dashboard_cli._model_watch_hint", return_value={})):
            patch.start()
            self.addCleanup(patch.stop)

    def serve(self, *roots):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app(list(roots)))
        thread = Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), thread.join(timeout=2)))
        self.host = f"127.0.0.1:{self.server.server_port}"
        with mock.patch.object(dashboard, "snapshot", return_value={"projects": []}):
            _, body = self.request("GET", "/api/v1/snapshot")
        self.token = body["settings_token"]

    def request(self, method, path, payload=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            raw = None if payload is None else (payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8"))
            connection.request(method, path, body=raw, headers=headers or {})
            response = connection.getresponse()
            data = response.read()
            try:
                return response.status, json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return response.status, data
        finally:
            connection.close()

    def headers(self, **changes):
        found = {"Host": self.host, "Origin": "http://" + self.host, "X-Eutonos-Settings": self.token,
                 "Content-Type": "application/json"}
        found.update(changes)
        return {key: value for key, value in found.items() if value is not None}

    def post(self, name, payload, **changes):
        return self.request("POST", "/api/v1/features/" + name, payload, self.headers(**changes))


class ReadRoute(Served):
    def test_a_fresh_machine_with_no_project_and_nothing_installed_reads_without_error(self):
        self.serve()
        status, body = self.request("GET", "/api/v1/features")
        self.assertEqual(status, 200)
        self.assertEqual(body["profile"], dashboard_features.PROFILE)
        self.assertEqual(body["components"]["outcome"], "not-installed")
        self.assertEqual(body["settings"]["ram"]["outcome"], "no-project")
        self.assertIn(body["settings"]["crosstalk-messaging"]["outcome"], ("refused", "failed"))
        self.assertIn(body["settings"]["linked-accounts"]["outcome"], ("refused", "failed"))
        for block in ("crosstalk-switches", "crosstalk-controls"):  # the machine pages read with no project and no install
            self.assertIn(body["settings"][block]["outcome"], ("refused", "failed"), block)
            self.assertTrue(body["settings"][block].get("detail") or body["settings"][block].get("reason"), block)
        self.assertEqual(body["pending"], {"outcome": "ok", "actions": [], "count": 0})
        self.assertIn("Leave linked accounts empty", body["notes"]["shared_login"])
        self.assertFalse(self.home.exists())  # reading wrote nothing

    def test_each_block_carries_its_own_answer_so_one_failure_hides_nothing(self):
        self.serve()
        with mock.patch("eutonos.installer.component_status", side_effect=RuntimeError("boom")):
            _, body = self.request("GET", "/api/v1/features")
        self.assertEqual(body["components"]["outcome"], "failed")
        self.assertIn("boom", body["components"]["detail"])
        self.assertEqual(body["pending"]["outcome"], "ok")

    def test_parts_carry_their_kind_so_the_page_can_say_core_is_always_on(self):
        self.serve()
        rows = [{"id": "core", "name": "EUTONOS core", "state": "installed", "enabled": True, "toggleable": False},
                {"id": "crosstalk", "name": "Crosstalk", "state": "installed", "enabled": False, "toggleable": True,
                 "reason": "turned off"},
                {"id": "role-hooks", "name": "Role reminder hooks", "state": "held", "enabled": False,
                 "reason": "held for later", "toggleable": False}]
        with mock.patch("eutonos.installer.component_status", return_value={"outcome": "ok", "components": rows}):
            _, body = self.request("GET", "/api/v1/features")
        kinds = {row["id"]: row["kind"] for row in body["components"]["components"]}
        self.assertEqual(kinds, {"core": "core", "crosstalk": "bundled", "role-hooks": "bundled"})

    def test_a_project_gives_the_ram_settings_and_a_bad_index_is_refused(self):
        self.serve(self.project)
        status, body = self.request("GET", "/api/v1/features?project=0")
        self.assertEqual(status, 200)
        ram = body["settings"]["ram"]
        self.assertEqual((ram["outcome"], ram["scope"]), ("ok", "project"))
        self.assertEqual(Path(ram["root"]), self.project.resolve())
        self.assertEqual(self.request("GET", "/api/v1/features?project=3")[0], 400)
        self.assertEqual(self.request("GET", "/api/v1/features?project=x")[0], 400)
        self.assertEqual(self.request("GET", "/api/v1/features?other=1")[0], 400)
        self.assertEqual(self.request("GET", "/api/v1/features?project=0",
                                      headers={"Host": "attacker.invalid"})[0], 403)

    def test_a_server_with_no_project_ignores_a_project_index_and_shows_no_ram(self):
        self.serve()
        status, body = self.request("GET", "/api/v1/features?project=0")
        self.assertEqual((status, body["settings"]["ram"]["outcome"]), (200, "no-project"))

    def test_pending_steps_are_listed_with_id_harness_action_and_why(self):
        self.serve()
        entry = pending_actions.add(source="component:crosstalk", harness="claude", action="Restart Claude.", why="It reads plugins at start.")
        _, body = self.request("GET", "/api/v1/features")
        self.assertEqual(body["pending"]["count"], 1)
        shown = body["pending"]["actions"][0]
        self.assertEqual({key: shown[key] for key in ("id", "harness", "action", "why", "source")},
                         {"id": entry["id"], "harness": "claude", "action": "Restart Claude.",
                          "why": "It reads plugins at start.", "source": "component:crosstalk"})

    def test_an_unreadable_pending_file_is_shown_not_read_as_empty(self):
        self.serve()
        target = pending_actions.path()
        target.parent.mkdir(parents=True)
        target.write_text("{not json", encoding="utf-8")
        _, body = self.request("GET", "/api/v1/features")
        self.assertEqual(body["pending"]["outcome"], "refused")
        self.assertEqual(body["pending"]["count"], 0)


class WriteGuard(Served):
    BODIES = {"component": {"component": "crosstalk", "enabled": False}, "messaging": {"enabled": True},
              "ram": {"project": 0, "capture": False}, "linked-accounts": {"action": "save", "accounts": []},
              "update-mode": {"mode": "notify"}, "pending": {"id": "pa-00000000", "action": "done"},
              "helper-switch": {"key": "letter_wake", "value": False}, "everything-off": {},
              "halt": {"halt": True}, "deny": {"action": "block", "key": "claude:local_1"},
              "wake-budget": {"per_chat": 3}}
    TARGETS = {"component": "eutonos.installer.set_component", "messaging": "eutonos.feature_settings.set_crosstalk_messaging",
               "ram": "eutonos.feature_settings.set_ram", "linked-accounts": "eutonos.feature_settings.set_linked_accounts",
               "update-mode": "eutonos.update_check.set_mode", "pending": "eutonos.pending_actions.complete",
               "helper-switch": "eutonos.feature_settings.set_helper_switch",
               "everything-off": "eutonos.feature_settings.turn_everything_off",
               "halt": "eutonos.feature_settings.set_halt", "deny": "eutonos.feature_settings.set_deny",
               "wake-budget": "eutonos.feature_settings.set_wake_budget"}

    def test_the_allowlist_is_exactly_the_switch_paths(self):
        self.assertEqual(FEATURE_POSTS, tuple("/api/v1/features/" + name for name in self.BODIES))
        self.serve(self.project)
        status, body = self.post("nothing-here", {})
        self.assertEqual((status, body["outcome"]), (404, "not-found"))

    def test_every_write_is_refused_without_the_token_a_wrong_token_a_wrong_origin_or_a_wrong_host(self):
        self.serve(self.project)
        for name, payload in self.BODIES.items():
            with mock.patch(self.TARGETS[name]) as owner, self.subTest(name=name):
                for changes in ({"X-Eutonos-Settings": None}, {"X-Eutonos-Settings": "wrong"},
                                {"Origin": "http://evil.invalid"}, {"Origin": None}, {"Host": "attacker.invalid"}):
                    status, body = self.post(name, payload, **changes)
                    self.assertEqual((status, body["outcome"]), (403, "denied"), changes)
                owner.assert_not_called()
        self.assertFalse(self.home.exists())
        self.assertFalse((self.project / ".eutonos-local").exists())

    def test_a_write_needs_a_json_body_of_the_expected_size(self):
        self.serve(self.project)
        with mock.patch("eutonos.feature_settings.set_crosstalk_messaging") as owner:
            self.assertEqual(self.post("messaging", b"{}", **{"Content-Type": "text/plain"})[0], 400)
            self.assertEqual(self.post("messaging", b"[true]")[0], 400)
            self.assertEqual(self.post("messaging", b"x" * 9000)[0], 400)
            owner.assert_not_called()

    def test_a_component_switch_passes_through_the_owners_answer(self):
        self.serve()
        answer = {"outcome": "disabled", "component": "crosstalk", "enabled": False, "pending": ["pa-1"], "notes": ["n"]}
        with mock.patch("eutonos.installer.set_component", return_value=answer) as owner:
            status, body = self.post("component", {"component": "crosstalk", "enabled": False})
        self.assertEqual((status, body), (200, answer))
        owner.assert_called_once_with("crosstalk", False)

    def test_a_component_switch_refused_by_the_owner_comes_back_plainly(self):
        self.serve()
        answer = {"outcome": "refused", "reason": "held", "detail": "role-hooks is not installed here (held).", "next_action": "x"}
        with mock.patch("eutonos.installer.set_component", return_value=answer):
            self.assertEqual(self.post("component", {"component": "role-hooks", "enabled": True})[1], answer)
        with mock.patch("eutonos.installer.set_component", return_value={"outcome": "busy", "reason": "other run", "next_action": "Wait."}):
            self.assertEqual(self.post("component", {"component": "crosstalk", "enabled": True})[1]["outcome"], "busy")

    def test_a_real_component_switch_on_a_machine_with_nothing_installed_is_refused_not_an_error(self):
        self.serve()
        status, body = self.post("component", {"component": "crosstalk", "enabled": False})
        self.assertEqual((status, body["outcome"], body["reason"]), (200, "refused", "not-installed"))

    def test_a_component_request_that_is_not_well_formed_is_refused_before_any_switch(self):
        self.serve()
        with mock.patch("eutonos.installer.set_component") as owner:
            for payload in ({"component": "crosstalk"}, {"enabled": True}, {"component": 5, "enabled": True},
                            {"component": "crosstalk", "enabled": "yes"}, {"component": "", "enabled": True},
                            {"component": "crosstalk", "enabled": True, "extra": 1}, {"component": "x" * 80, "enabled": True}):
                self.assertEqual(self.post("component", payload)[0], 400, payload)
            owner.assert_not_called()

    def test_messaging_goes_to_the_messaging_owner_and_a_halt_field_is_never_accepted_there(self):
        self.serve()
        answer = {"outcome": "enabled", "setting": "crosstalk-messaging", "enabled": True, "halt": True,
                  "notes": ["Crosstalk is halted (the owner's stop)."]}
        with mock.patch("eutonos.feature_settings.set_crosstalk_messaging", return_value=answer) as owner:
            self.assertEqual(self.post("messaging", {"enabled": True}), (200, answer))
            owner.assert_called_once_with(True)
            for payload in ({"enabled": "on"}, {"halt": False}, {"enabled": True, "halt": False}, {}):
                self.assertEqual(self.post("messaging", payload)[0], 400, payload)
            self.assertEqual(owner.call_count, 1)

    def test_the_stop_is_its_own_request_and_takes_only_a_true_or_false(self):
        self.serve()
        answer = {"outcome": "enabled", "setting": "halt", "halt": True, "notes": []}
        with mock.patch("eutonos.feature_settings.set_halt", return_value=answer) as owner:
            self.assertEqual(self.post("halt", {"halt": True}), (200, answer))
            owner.assert_called_once_with(True)
            self.assertEqual(self.post("halt", {"halt": False})[0], 200)
            owner.assert_called_with(False)
            for payload in ({"halt": "yes"}, {"halt": 1}, {}, {"halt": True, "messaging": False}):
                self.assertEqual(self.post("halt", payload)[0], 400, payload)
            self.assertEqual(owner.call_count, 2)

    def test_helper_switch_requests_pass_the_value_and_are_checked_for_shape(self):
        self.serve()
        answer = {"outcome": "disabled", "setting": "letter_wake", "key": "letter_wake", "value": False, "notes": []}
        with mock.patch("eutonos.feature_settings.set_helper_switch", return_value=answer) as owner:
            self.assertEqual(self.post("helper-switch", {"key": "letter_wake", "value": False}), (200, answer))
            owner.assert_called_once_with("letter_wake", False)
            self.post("helper-switch", {"key": "focus_notice_seconds", "value": 5})
            owner.assert_called_with("focus_notice_seconds", 5)
            self.post("helper-switch", {"key": "always_remote", "value": ["local_" + "1" * 8 + "-" + "2" * 4 + "-" + "3" * 4 + "-" + "4" * 4 + "-" + "5" * 12]})
            self.assertEqual(owner.call_count, 3)
            for payload in ({"key": "letter_wake"}, {"value": True}, {"key": "", "value": True}, {"key": 3, "value": True},
                            {"key": "letter_wake", "value": None}, {"key": "letter_wake", "value": {"a": 1}},
                            {"key": "always_remote", "value": [1]}, {"key": "always_remote", "value": ["x"] * 65},
                            {"key": "letter_wake", "value": True, "extra": 1}):
                self.assertEqual(self.post("helper-switch", payload)[0], 400, payload)
            self.assertEqual(owner.call_count, 3)

    def test_everything_off_the_deny_list_and_the_wake_budget_pass_through_and_are_checked(self):
        self.serve()
        with mock.patch("eutonos.feature_settings.turn_everything_off", return_value={"outcome": "disabled"}) as off:
            self.assertEqual(self.post("everything-off", {}), (200, {"outcome": "disabled"}))
            off.assert_called_once_with()
            self.assertEqual(self.post("everything-off", {"force": True})[0], 400)
        with mock.patch("eutonos.feature_settings.set_deny", return_value={"outcome": "changed"}) as deny:
            self.assertEqual(self.post("deny", {"action": "block", "key": "claude:local_x"})[0], 200)
            deny.assert_called_once_with("block", "claude:local_x")
            for payload in ({"action": "ban", "key": "claude:local_x"}, {"action": "block"}, {"key": "claude:local_x"},
                            {"action": "allow", "key": ""}):
                self.assertEqual(self.post("deny", payload)[0], 400, payload)
        with mock.patch("eutonos.feature_settings.set_wake_budget", return_value={"outcome": "changed"}) as budget:
            self.assertEqual(self.post("wake-budget", {"per_chat": 4, "window_s": 600})[0], 200)
            budget.assert_called_once_with({"per_chat": 4, "window_s": 600})
            for payload in ({}, {"per_chat": "4"}, {"per_chat": True}, {"per_chat": 4.5}, {"bogus": 1}):
                self.assertEqual(self.post("wake-budget", payload)[0], 400, payload)
            self.assertEqual(budget.call_count, 1)

    def test_ram_is_set_for_the_selected_project_only(self):
        self.serve(self.project)
        status, body = self.post("ram", {"project": 0, "capture": False})
        self.assertEqual((status, body["outcome"], body["capture"]), (200, "disabled", False))
        _, read = self.request("GET", "/api/v1/features?project=0")
        self.assertFalse(read["settings"]["ram"]["capture"])
        self.assertTrue((self.project / ".eutonos-local" / "settings.json").is_file())
        self.assertFalse(self.home.exists())  # nothing machine-wide was written
        status, body = self.post("ram", {"project": 0, "retrieval": True, "capture": True})
        self.assertEqual((status, body["outcome"]), (200, "enabled"))
        self.assertEqual(self.post("ram", {"project": 0, "capture": True})[1]["outcome"], "unchanged")

    def test_ram_with_no_project_or_a_bad_request_changes_nothing(self):
        self.serve()
        status, body = self.post("ram", {"project": 0, "capture": False})
        self.assertEqual((status, body["outcome"], body["reason"]), (200, "refused", "no-project"))
        self.serve(self.project)
        with mock.patch("eutonos.feature_settings.set_ram") as owner:
            for payload in ({"capture": False}, {"project": 4, "capture": False}, {"project": True, "capture": False},
                            {"project": 0}, {"project": 0, "capture": "off"}, {"project": 0, "retrieval": "maybe"},
                            {"project": 0, "cadence": "always"}):
                self.assertEqual(self.post("ram", payload)[0], 400, payload)
            owner.assert_not_called()

    def test_a_suggestion_never_writes_and_a_save_is_a_separate_request(self):
        self.serve()
        suggestion = {"outcome": "ok", "setting": "linked-accounts", "suggested_emails": ["me@example.com"], "seen": [], "accounts": []}
        with mock.patch("eutonos.feature_settings.suggest_linked_accounts", return_value=suggestion) as suggest, \
                mock.patch("eutonos.feature_settings.set_linked_accounts") as save:
            self.assertEqual(self.post("linked-accounts", {"action": "suggest"}), (200, suggestion))
            suggest.assert_called_once_with()
            save.assert_not_called()
            for payload in ({"action": "suggest", "accounts": ["me@example.com"]}, {"action": "write", "accounts": []},
                            {"action": "save"}, {"action": "save", "accounts": "me@example.com"},
                            {"action": "save", "accounts": [5]}, {"action": "save", "accounts": [""] * 17}, {}):
                self.assertEqual(self.post("linked-accounts", payload)[0], 400, payload)
            save.assert_not_called()

    def test_saving_linked_accounts_passes_exactly_the_list_sent(self):
        self.serve()
        answer = {"outcome": "enabled", "setting": "linked-accounts", "enabled": True, "accounts": ["me@example.com"]}
        with mock.patch("eutonos.feature_settings.set_linked_accounts", return_value=answer) as save:
            self.assertEqual(self.post("linked-accounts", {"action": "save", "accounts": ["me@example.com"]}), (200, answer))
            save.assert_called_once_with(["me@example.com"])
            self.post("linked-accounts", {"action": "save", "accounts": []})
            save.assert_called_with([])

    def test_a_real_save_with_no_crosstalk_kit_is_refused_in_plain_words(self):
        self.serve()
        status, body = self.post("linked-accounts", {"action": "save", "accounts": ["me@example.com"]})
        self.assertEqual((status, body["outcome"]), (200, "refused"))
        self.assertTrue(body["detail"])

    def test_pending_steps_can_be_marked_done_or_dismissed(self):
        self.serve()
        first = pending_actions.add(source="s", action="Do the first thing.")
        second = pending_actions.add(source="s", action="Do the second thing.")
        self.assertEqual(self.post("pending", {"id": first["id"], "action": "done"})[1],
                         {"outcome": "done", "id": first["id"], "state": "done"})
        self.assertEqual(self.post("pending", {"id": second["id"], "action": "dismiss"})[1],
                         {"outcome": "dismissed", "id": second["id"], "state": "dismissed"})
        self.assertEqual(self.request("GET", "/api/v1/features")[1]["pending"]["count"], 0)
        finished = {item["id"]: item for item in pending_actions.list_actions(state=None)}
        self.assertEqual(finished[first["id"]]["done_by"], "dashboard")
        self.assertEqual(finished[second["id"]]["note"], "dismissed from the dashboard")

    def test_an_unknown_step_is_refused_and_a_bad_request_is_a_400(self):
        self.serve()
        status, body = self.post("pending", {"id": "pa-nope", "action": "done"})
        self.assertEqual((status, body["outcome"]), (200, "refused"))
        for payload in ({"id": "pa-1"}, {"id": "pa-1", "action": "delete"}, {"id": 4, "action": "done"},
                        {"id": "", "action": "done"}, {"action": "done"}):
            self.assertEqual(self.post("pending", payload)[0], 400, payload)

    def test_the_pages_and_their_nav_links_ship_with_the_dashboard(self):
        self.serve()
        html = dashboard.static("index.html").decode("utf-8")
        for section, label in (("m_parts", "Parts"), ("m_crosstalk", "Crosstalk"), ("m_claude", "Claude")):
            self.assertIn(f'<a href="#settings" data-settings-nav="{section}">{label}</a>', html)
        self.assertNotIn('data-view="features"', html)  # the page moved into Settings; #features only redirects
        self.assertEqual(html.count('data-settings-nav="delivery"'), 1)  # the duplicated Git delivery link is gone
        self.assertIn('data-settings-nav="roles"', html)
        self.assertNotIn('data-settings-nav="cross_talk"', html)
        self.assertNotIn('data-settings-nav="hooks"', html)
        self.assertLess(html.index("This repository"), html.index('data-settings-nav="ram"'))
        self.assertLess(html.index("This computer"), html.index('data-settings-nav="m_parts"'))
        self.assertLess(html.index('data-settings-nav="delivery"'), html.index("This computer"))
        source = dashboard.static("app.js").decode("utf-8")
        self.assertNotIn("features:featuresPage", source)
        self.assertIn('history.replaceState(null,"","#settings")', source)
        for name in FEATURE_POSTS:
            self.assertIn(name.removeprefix("/api/v1/features/"), source)
        self.assertIn("X-Eutonos-Settings", source[source.index("async function featuresPost"):source.index("async function featuresAct")])


class SharedNotes(unittest.TestCase):
    def test_the_page_shows_the_same_wording_the_owners_ship(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"EUTONOS_HOME": home}):
            notes = dashboard_features.read()["notes"]
        self.assertEqual(notes["shared_login"], feature_settings.SHARED_LOGIN_NOTE)
        self.assertEqual(notes["ram_project"], feature_settings.RAM_PROJECT_NOTE)


if __name__ == "__main__":
    unittest.main()
