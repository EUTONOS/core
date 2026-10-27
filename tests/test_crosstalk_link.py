"""The EUTONOS side of the Crosstalk Hermes link (slice 3B.17): the read-only link view, the pickup and doctor lines, the
single pending-action repair offer, and the Harness policy watch item for changed Hermes internals. Temporary
homes and device folders only; the real ~/.eutonos and Hermes are never read."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from eutonos import crosstalk_link as link
from eutonos import crosstalk_status, doctor, model_watch, pending_actions as pa


class LinkCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="eutonos-link-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.device = self.root / "device"
        self.device.mkdir()
        self.source = self.root / "hermes-agent"
        self.source.mkdir()
        env = mock.patch.dict(os.environ, {"EUTONOS_HOME": str(self.root / "home"),
                                          "CROSSTALK_P0_DEVICE_DIR": str(self.device),
                                          "CROSSTALK_P0_HERMES_HOME": str(self.root / "hermes"),
                                          "CROSSTALK_P0_HERMES_SOURCE": str(self.source)})
        env.start()
        self.addCleanup(env.stop)
        (self.root / "home").mkdir()
        self.stamp("0.22.5", "bf0203a794a0aabb")

    def stamp(self, version, commit):
        (self.source / "install-stamp.json").write_text(
            json.dumps({"displayVersion": version, "commit": commit}), encoding="utf-8")

    def write(self, **fields):
        record = {"profile": "crosstalk.link.v1", "endpoint": "hermes", "updated_utc": "2026-10-26T12:00:00+00:00"}
        record.update(fields)
        (self.device / "link-hermes.json").write_text(json.dumps(record), encoding="utf-8")

    def version(self, checked="0.22.5", commit="bf0203a794a0"):
        row = {"version": checked, "commit": commit, "fingerprint": f"{checked}+{commit}"}
        return {"checked": row, "current": row}

    def broken(self, state="offered", key="k1", offer_no=1, mode="ask"):
        return {"diagnosis": {"state": "plugin-incompatible", "message": "loaded but no host"},
                "version": self.version(),
                "repair": {"state": state, "mode": mode, "key": key, "offer_no": offer_no,
                           "prompt_path": str(self.root / "repair.md"),
                           "harnesses": [{"app": "claude", "model": "sonnet", "effort": "high"}],
                           "reasons": [{"id": "diagnosis:plugin-incompatible",
                                        "text": "The plugin is loaded but none of its processes found the chat host."}]}}


class ViewTests(LinkCase):
    def test_no_record_adds_nothing_anywhere(self):
        self.assertIsNone(link.status())
        self.assertIsNone(link.pickup_line())
        self.assertFalse(link.doctor_row()["attention"])
        self.assertEqual(link.watch_items(), [])
        self.assertEqual(link.sync_pending_offer()["did"], "none")

    def test_a_foreign_file_is_not_our_record(self):
        (self.device / "link-hermes.json").write_text(json.dumps({"profile": "other"}), encoding="utf-8")
        self.assertEqual(link.link_record(), {})

    def test_a_healthy_checked_link_is_quiet(self):
        self.write(diagnosis={"state": "ok"}, version=self.version())
        self.assertIsNone(link.pickup_line())
        self.assertEqual(link.doctor_row()["detail"], "the Hermes link is fine")

    def test_a_hermes_update_since_the_last_check_is_flagged(self):
        self.write(diagnosis={"state": "ok"}, version=self.version("0.22.5"))
        self.stamp("0.23.0", "1111222233334444")
        self.assertEqual(link.version_notice()["text"],
                         "Hermes updated from 0.22.5 to 0.23.0 since Crosstalk was last checked")
        self.assertIn("Hermes updated from 0.22.5 to 0.23.0", link.pickup_line())
        self.assertTrue(link.doctor_row()["attention"])

    def test_a_broken_link_is_named_with_the_offer(self):
        self.write(**self.broken())
        line = link.pickup_line()
        self.assertIn("BROKEN", line)
        self.assertIn("none of its processes found the chat host", line)
        self.assertIn("a repair is offered", line)
        row = link.doctor_row()
        self.assertEqual((row["attention"], row["state"], row["repair"]), (True, "plugin-incompatible", "offered"))

    def test_the_crosstalk_pickup_hint_carries_the_link_line(self):
        (self.device / "controls.json").write_text(json.dumps({"messaging": "on"}), encoding="utf-8")
        self.write(**self.broken())
        hint = crosstalk_status.pickup_hint()
        self.assertIn("hermes link:", hint["hermes_link"])
        self.assertTrue(hint["line"].endswith(hint["hermes_link"]))
        self.write(diagnosis={"state": "ok"}, version=self.version())
        self.assertNotIn("hermes_link", crosstalk_status.pickup_hint())

    def test_the_doctor_report_has_the_readback(self):
        self.write(**self.broken())
        self.assertTrue(doctor._hermes_link_readback()["attention"])
        text = doctor._short_text({"readbacks": {"crosstalk_hermes_link": link.doctor_row()}, "readiness": {},
                                   "declared": {}, "undeclared": [], "checkpoint": {}})
        self.assertIn("Hermes link:", text)

    def test_the_repair_mode_is_read_from_the_helper_config(self):
        self.assertEqual(link.repair_mode(), "ask")
        (self.device / "helper").mkdir()
        (self.device / "helper" / "config.json").write_text(json.dumps({"link_repair_offer": "auto"}), encoding="utf-8")
        self.assertEqual(link.repair_mode(), "auto")


class OfferTests(LinkCase):
    def test_an_offered_repair_becomes_exactly_one_pending_action(self):
        self.write(**self.broken())
        first = link.sync_pending_offer()
        second = link.sync_pending_offer()
        self.assertEqual((first["did"], second["did"]), ("offered", "none"))
        entries = pa.list_actions(state="all")
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual((entry["source"], entry["harness"], entry["state"]), (link.OFFER_SOURCE, "any", "pending"))
        self.assertIn("hermes-repair --start claude", entry["action"])
        self.assertIn(str(self.root / "repair.md"), entry["action"])
        self.assertIn("[ref k1-1]", entry["action"])
        self.assertLessEqual(len(entry["action"]), pa.MAX_ACTION_CHARS)
        self.assertIn("none of its processes found the chat host", entry["why"])
        self.assertIn("Fix the Crosstalk link to Hermes", pa.pickup_hint()["line"])

    def test_a_dismissed_offer_is_not_raised_again_but_a_new_offer_is(self):
        self.write(**self.broken())
        link.sync_pending_offer()
        pa.dismiss(None, pa.list_actions()[0]["id"], note="owner declined")
        self.assertEqual(link.sync_pending_offer()["did"], "none")
        self.assertEqual(pa.list_actions(), [])
        self.write(**self.broken(key="k2", offer_no=2))
        self.assertEqual(link.sync_pending_offer()["did"], "offered")
        self.assertEqual(len(pa.list_actions()), 1)

    def test_a_repaired_link_or_a_started_repair_chat_dismisses_the_pending_action(self):
        for number, state in enumerate(("resolved", "started", "starting", "off"), start=1):
            self.write(**self.broken(key="k%d" % number, offer_no=number))
            self.assertEqual(link.sync_pending_offer()["did"], "offered")
            self.assertEqual(len(pa.list_actions()), 1)
            self.write(**self.broken(state=state, key="k%d" % number, offer_no=number))
            self.assertEqual(link.sync_pending_offer()["did"], "dismissed", state)
            self.assertEqual(pa.list_actions(), [])

    def test_off_mode_never_adds_one(self):
        self.write(**self.broken(mode="off"))
        self.assertEqual(link.sync_pending_offer()["did"], "none")
        self.assertEqual(pa.list_actions(state="all"), [])

    def test_a_failed_automatic_start_is_offered_for_the_owner(self):
        self.write(**self.broken(state="start-failed", mode="auto"))
        self.assertEqual(link.sync_pending_offer()["did"], "offered")

    def test_a_broken_pending_actions_file_never_raises(self):
        self.write(**self.broken())
        (self.root / "home" / "settings").mkdir()
        (self.root / "home" / "settings" / "pending-actions.json").write_text("not json", encoding="utf-8")
        self.assertEqual(link.sync_pending_offer()["did"], "error")

    def test_pickup_and_the_dashboard_page_raise_the_offer(self):
        from eutonos import dashboard_features
        self.write(**self.broken())
        page = dashboard_features._pending()
        self.assertEqual(page["count"], 1)
        self.assertEqual(page["actions"][0]["source"], link.OFFER_SOURCE)


class WatchTests(LinkCase):
    def contract(self, missing):
        return {"utc": "2026-10-26T10:00:00+00:00", "first_seen_utc": "2026-10-26T09:00:00+00:00",
                "fingerprint": "0.23.0+aaaa", "ok": not missing, "missing": missing}

    def missing(self, name="gateway-injector"):
        return [{"id": name, "what": "the gateway injector", "uses": "plugin", "file": "hermes_cli/plugins.py",
                 "problem": "text-missing", "needs": ["x"]}]

    def test_no_missing_internals_means_no_item(self):
        self.write(contract=self.contract([]))
        self.assertEqual(link.watch_items(), [])
        self.assertEqual(model_watch.pending()["count"], 0)

    def test_changed_internals_show_in_the_harness_watch_and_pickup(self):
        self.write(contract=self.contract(self.missing()), version=self.version("0.23.0"))
        items = link.watch_items()
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertTrue(item["id"].startswith("hermes-internals-") and len(item["id"]) <= 64)
        self.assertEqual((item["source"], item["kind"]), ("hermes_internals", "changed"))
        self.assertIn("Hermes internals Crosstalk uses changed", item["key"])
        self.assertIn("gateway-injector", item["key"])
        self.assertIsInstance(item["first_seen"], float)
        pending = model_watch.pending()
        self.assertEqual((pending["status"], pending["count"]), ("ok", 1))
        hint = model_watch.pickup_hint()
        self.assertEqual(hint["pending_count"], 1)
        self.assertEqual(hint["items"][0]["source"], "hermes_internals")

    def test_acknowledging_hides_it_until_the_internals_change_again(self):
        self.write(contract=self.contract(self.missing()))
        item_id = link.watch_items()[0]["id"]
        done = model_watch.ack(ids=[item_id])
        self.assertEqual(done["acknowledged"], [item_id])
        self.assertEqual(link.watch_items(), [])
        self.assertEqual(model_watch.pending()["count"], 0)
        self.write(contract=self.contract(self.missing("session-create")))
        again = link.watch_items()
        self.assertEqual(len(again), 1)
        self.assertNotEqual(again[0]["id"], item_id)

    def test_an_unknown_internals_id_acknowledges_nothing(self):
        self.write(contract=self.contract(self.missing()))
        self.assertEqual(link.watch_ack(["hermes-internals-000000000000"]), [])
        self.assertEqual(len(link.watch_items()), 1)

    def test_a_watch_failure_in_the_link_module_never_breaks_the_watch(self):
        with mock.patch.object(link, "watch_items", side_effect=RuntimeError("boom")):
            self.assertEqual(model_watch.pending()["count"], 0)


if __name__ == "__main__":
    unittest.main()
