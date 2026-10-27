"""Hermes link health (slice 3B.17), offline: the plugin's status records and probe, the core's diagnosis across all
fresh records, the version watch, the self-test, the contract check against a fake Hermes source, the single repair
offer and the helper pass. A temp device folder, temp EUTONOS home, temp Hermes home and a fake Hermes source are used;
no live app, plugin, network or real letter is involved."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import test_crosstalk_hermes as base

core, plugin, installer = base.core, base.plugin, base.installer

NEW_PLUGIN = "0.4.0"


def dead_pid() -> int:
    return next(pid for pid in range(4_000_000, 4_000_500) if not core.pid_alive(pid))


class LinkCase(base.HermesCase):
    def setUp(self):
        super().setUp()
        self.eutonos = self.root / "eutonos-home"
        self.source = self.root / "hermes-agent"
        self.source.mkdir()
        env = mock.patch.dict(os.environ, {"EUTONOS_HOME": str(self.eutonos),
                                          "CROSSTALK_P0_HERMES_SOURCE": str(self.source)})
        env.start()
        self.addCleanup(env.stop)
        plugin._STATUS.update(loaded_utc=None, last_kind="unset", written_at=0.0, hermes=None)
        for patcher in (mock.patch.object(core, "claude_exe", return_value=None),
                        mock.patch.object(core, "codex_exe", return_value=None),
                        mock.patch.object(plugin, "_hermes_source_root", return_value=self.source),
                        mock.patch.object(plugin, "_gateway_server", return_value=SimpleNamespace(
                            _sessions={"sid": {"session_key": base.H1}}, _sessions_lock=None))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.stamp("0.22.5", "bf0203a794a0aabbccdd")
        self.status_dir = self.hermes / "crosstalk" / "status"
        self.status_dir.mkdir(parents=True, exist_ok=True)
        for sub in ("probe", "probe-done"):
            (self.hermes / "crosstalk" / sub).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ fixtures
    def stamp(self, version, commit):
        (self.source / "install-stamp.json").write_text(
            json.dumps({"displayVersion": version, "baseVersion": version, "commit": commit}), encoding="utf-8")

    def iso(self, ago_s=0.0):
        return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - ago_s))

    def record(self, name, host=None, ago=1.0, loaded_ago=300.0, pid=None, probes=None):
        probes = probes if probes is not None else [{"name": "plugin-manager", "found": True},
                                                    {"name": "tui-injector", "found": host == "tui"},
                                                    {"name": "gateway-injector", "found": host == "gateway"}]
        (self.status_dir / f"{name}.json").write_text(json.dumps(
            {"profile": core.HERMES_STATUS_PROFILE, "pid": os.getpid() if pid is None else pid,
             "plugin_version": NEW_PLUGIN, "hermes_version": "0.22.5", "hermes_source": str(self.source), "host": host,
             "hosts_chats": host is not None, "probes": probes, "loaded_utc": self.iso(loaded_ago),
             "utc": self.iso(ago)}), encoding="utf-8")

    def heartbeat(self, ago=1.0):
        (self.hermes / "crosstalk" / "heartbeat.json").write_text(json.dumps(
            {"pid": os.getpid(), "utc": self.iso(ago), "hosts_chats": True, "host": "tui",
             "plugin_version": NEW_PLUGIN}), encoding="utf-8")

    def gateway_pid(self, pid=None):
        (self.hermes / "gateway.pid").write_text(json.dumps({"pid": os.getpid() if pid is None else pid}),
                                                 encoding="utf-8")

    def contract_tree(self, drop_need=None, drop_file=None):
        for entry in core.hermes_contract_entries():
            target = self.source / entry["file"]
            if entry["file"] == "install-stamp.json":
                continue  # the real stamp already carries what the contract needs
            if drop_file == entry["id"]:
                target.unlink(missing_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            existing = target.read_text(encoding="utf-8") if target.exists() else ""
            needs = [n for n in entry["needs"] if not (drop_need and drop_need == (entry["id"], n))]
            target.write_text(existing + "\n" + "\n".join(needs) + "\n", encoding="utf-8")

    def host(self, **options):
        return HostThread(self, **options)


class HostThread(threading.Thread):
    """A stand-in for the plugin in the hosting process: the plugin's own status writer and probe answer."""

    def __init__(self, case, answer_probes=True, heartbeat=True):
        super().__init__(daemon=True)
        self.case, self.answer, self.beat = case, answer_probes, heartbeat
        self.ctx = base.FakeCtx()
        self.stop_flag = threading.Event()

    def run(self):
        folder = self.case.hermes / "crosstalk"
        while not self.stop_flag.is_set():
            plugin._write_status(self.ctx, force=True)
            if self.beat:
                plugin._write_json(folder / "heartbeat.json", {"pid": os.getpid(), "utc": plugin._now(),
                                                                "hosts_chats": True, "host": "tui",
                                                                "plugin_version": NEW_PLUGIN})
            if self.answer:
                plugin._probe_pass(self.ctx, folder, "tui")
            time.sleep(0.02)

    def __enter__(self):
        self.start()
        time.sleep(0.15)
        return self

    def __exit__(self, *exc):
        self.stop_flag.set()
        self.join(2)


# ------------------------------------------------------------------ the plugin's self-report
class PluginSelfReportTests(LinkCase):
    def test_the_plugin_is_version_040(self):
        self.assertEqual(plugin.PLUGIN_VERSION, NEW_PLUGIN)

    def test_host_probe_lists_every_check_and_names_the_kind(self):
        kind, probes = plugin._host_probe(SimpleNamespace())
        self.assertIsNone(kind)
        self.assertEqual([p["name"] for p in probes], ["plugin-manager", "tui-injector", "gateway-injector"])
        self.assertFalse(any(p["found"] for p in probes))
        self.assertEqual(plugin._host_probe(base.FakeCtx())[0], "tui")
        gateway = SimpleNamespace(_manager=SimpleNamespace(has_tui_message_injector=False,
                                                           has_gateway_message_injector=True))
        self.assertEqual(plugin._host_probe(gateway)[0], "gateway")

    def test_every_load_writes_a_status_record_even_without_a_host(self):
        plugin._write_status(SimpleNamespace(), force=True)
        written = json.loads((self.status_dir / f"{os.getpid()}.json").read_text(encoding="utf-8"))
        self.assertEqual(written["profile"], "crosstalk.hermes.status.v1")
        self.assertEqual((written["pid"], written["plugin_version"], written["host"], written["hosts_chats"]),
                         (os.getpid(), NEW_PLUGIN, None, False))
        self.assertEqual((written["hermes_version"], written["hermes_commit"]), ("0.22.5", "bf0203a794a0"))
        self.assertEqual(written["hermes_source"], str(self.source))
        self.assertEqual(len(written["probes"]), 3)
        self.assertTrue(written["loaded_utc"] and written["utc"])

    def test_the_status_record_says_which_host_was_found(self):
        plugin._write_status(base.FakeCtx(), force=True)
        written = json.loads((self.status_dir / f"{os.getpid()}.json").read_text(encoding="utf-8"))
        self.assertEqual((written["host"], written["hosts_chats"]), ("tui", True))
        plugin._remove_status()
        self.assertFalse((self.status_dir / f"{os.getpid()}.json").exists())

    def test_a_probe_is_answered_without_injecting_into_any_chat(self):
        ctx = base.FakeCtx()
        folder = self.hermes / "crosstalk"
        (folder / "probe" / "probe-0123456789ab.json").write_text(
            json.dumps({"probe_id": "probe-0123456789ab", "session_ids": [base.H1, base.H2]}), encoding="utf-8")
        (folder / "probe" / "not-a-probe.json").write_text("{}", encoding="utf-8")
        plugin._probe_pass(ctx, folder, "tui")
        answer = json.loads((folder / "probe-done" / "probe-0123456789ab.result.json").read_text(encoding="utf-8"))
        self.assertEqual(ctx.calls, [])
        self.assertIs(answer["injected"], False)
        self.assertTrue(answer["inbox_loop"] and answer["hosts_chats"])
        self.assertEqual(answer["route"], {"api": "ok", "tried": 2, "open": [base.H1], "errors": 0})
        self.assertTrue((folder / "probe" / "not-a-probe.json").exists())  # a foreign file is left alone
        self.assertEqual(list((folder / "work").glob("*probe*")), [])

    def test_a_missing_route_api_is_reported_not_hidden(self):
        with mock.patch.object(plugin, "_gateway_server", return_value=SimpleNamespace(_sessions=None)):
            route = plugin._probe_route(base.FakeCtx(), "tui", [base.H1])
        self.assertTrue(route["api"].startswith("missing:"))

    def test_the_contract_list_is_literal_and_complete(self):
        entries = core.hermes_contract_entries()
        self.assertEqual(len(entries), len(plugin.HERMES_CONTRACT))
        self.assertEqual(len({e["id"] for e in entries}), len(entries))
        for entry in entries:
            self.assertTrue(entry["needs"] and entry["file"] and entry["what"])
            self.assertFalse(Path(entry["file"]).is_absolute())


# ------------------------------------------------------------------ the core's diagnosis
class DiagnosisTests(LinkCase):
    def test_not_running_keeps_the_old_refusal_exactly(self):
        self.assertEqual(core.hermes_link_diagnosis()["state"], "not-running")
        self.assertEqual(core.deliver_hermes("l1", base.H1, "hi"), ("refused", {"reason": "hermes-not-running"}))
        self.assertFalse((core.device_dir() / "link-hermes.json").exists())  # nothing to offer: Hermes is closed

    def test_hermes_running_with_no_plugin_record_is_plugin_not_loaded(self):
        self.gateway_pid()
        diag = core.hermes_link_diagnosis()
        self.assertEqual((diag["state"], diag["reason"]), ("plugin-not-loaded", "hermes-plugin-not-loaded"))
        self.assertTrue(diag["hermes_running"])
        state, extra = core.deliver_hermes("l2", base.H1, "hi")
        self.assertEqual((state, extra["reason"], extra["link_state"]), ("refused", "hermes-plugin-not-loaded",
                                                                       "plugin-not-loaded"))

    def test_an_old_installed_plugin_is_named_as_unable_to_report(self):
        self.gateway_pid()
        (self.hermes / "plugins" / "crosstalk" / "__init__.py").write_text('PLUGIN_VERSION = "0.3.0"\n', encoding="utf-8")
        message = core.hermes_link_diagnosis()["message"]
        self.assertIn("0.3.0", message)
        self.assertIn("older than 0.4.0", message)

    def test_records_but_no_host_is_plugin_incompatible_with_the_probes_listed(self):
        self.record("a", host=None)
        self.record("b", host=None)
        diag = core.hermes_link_diagnosis()
        self.assertEqual((diag["state"], diag["reason"]), ("plugin-incompatible", "hermes-plugin-incompatible"))
        self.assertEqual(diag["status_records"], 2)
        self.assertIn("tui-injector: not found", diag["message"])
        self.assertIn("0.22.5", diag["message"])
        self.assertIn("none of them found the host", diag["message"])

    def test_one_hosting_process_among_non_hosting_ones_is_never_incompatible(self):
        self.record("cli", host=None)
        self.record("host", host="tui")
        diag = core.hermes_link_diagnosis()
        self.assertEqual(diag["hosting_records"], 1)
        self.assertEqual(diag["state"], "plugin-not-listening")  # the host exists but its heartbeat is missing
        self.heartbeat()
        self.assertEqual(core.hermes_link_diagnosis()["state"], "ok")

    def test_a_plugin_that_just_loaded_is_starting_not_incompatible(self):
        self.record("a", host=None, loaded_ago=10.0)
        self.assertEqual(core.hermes_link_diagnosis()["state"], "starting")
        self.record("b", host=None, loaded_ago=300.0)  # a process up a while does not hide the one still starting
        self.assertEqual(core.hermes_link_diagnosis()["state"], "starting")
        (self.status_dir / "a.json").unlink()
        self.assertEqual(core.hermes_link_diagnosis()["state"], "plugin-incompatible")

    def test_stale_or_dead_process_records_are_not_evidence(self):
        self.record("old", host=None, ago=core.HERMES_STATUS_FRESH_S + 30)
        self.record("dead", host=None, pid=dead_pid())
        diag = core.hermes_link_diagnosis()
        self.assertEqual((diag["state"], diag["status_records"]), ("not-running", 0))

    def test_a_live_serve_row_shows_hermes_runs_without_the_plugin(self):
        (self.hermes / "spawn-ledger.json").write_text(json.dumps(
            [{"pid": os.getpid(), "purpose": "serve", "port": 1}, {"pid": os.getpid(), "purpose": "other"}]),
            encoding="utf-8")
        self.assertEqual(core.hermes_running_pids(), [os.getpid()])
        self.assertEqual(core.hermes_link_diagnosis()["state"], "plugin-not-loaded")

    def test_refused_letter_guidance_points_at_the_right_cause_and_the_repair(self):
        for reason in ("hermes-plugin-incompatible", "hermes-plugin-not-loaded", "hermes-plugin-not-listening",
                       "hermes-starting", "hermes-not-running"):
            self.assertIn(reason, core.REASON_GUIDANCE)
        result = core._result({"state": "refused", "letter_id": "x", "reason": "hermes-plugin-incompatible",
                               "link_state": "plugin-incompatible", "link_detail": "why", "link_repair": "offered"})
        self.assertIn("A repair has been offered", result["guidance"])
        self.assertEqual(result["link_state"], "plugin-incompatible")

    def test_a_refused_letter_for_a_broken_link_records_one_offer(self):
        self.record("a", host=None)
        state, extra = core.deliver_hermes("l3", base.H1, "hi")
        self.assertEqual((state, extra["reason"], extra["link_repair"]), ("refused", "hermes-plugin-incompatible", "offered"))
        core.deliver_hermes("l4", base.H1, "again")
        link = core.hermes_link_load()
        self.assertEqual(link["repair"]["offer_no"], 1)
        self.assertTrue(Path(link["repair"]["prompt_path"]).is_file())


# ------------------------------------------------------------------ the contract check
class ContractTests(LinkCase):
    def test_present_internals_pass(self):
        self.contract_tree()
        result = core.hermes_contract_check()
        self.assertEqual((result["ok"], result["missing"], result["unchecked"]), (True, [], None))
        self.assertEqual(result["checked"], len(core.hermes_contract_entries()))
        self.assertEqual(result["fingerprint"], "0.22.5+bf0203a794a0")

    def test_a_moved_text_and_a_removed_file_are_both_reported(self):
        first, second = core.hermes_contract_entries()[0], core.hermes_contract_entries()[5]
        self.contract_tree(drop_need=(first["id"], first["needs"][0]), drop_file=second["id"])
        missing = {item["id"]: item for item in core.hermes_contract_check()["missing"]}
        self.assertEqual(missing[first["id"]]["problem"], "text-missing")
        self.assertEqual(missing[first["id"]]["needs"], [first["needs"][0]])
        self.assertEqual(missing[second["id"]]["problem"], "file-missing")

    def test_a_source_folder_that_cannot_be_found_is_unchecked_not_failed(self):
        with mock.patch.dict(os.environ, {"CROSSTALK_P0_HERMES_SOURCE": str(self.root / "nowhere")}):
            result = core.hermes_contract_check()
        self.assertIsNone(result["ok"])
        self.assertIn("not found", result["unchecked"])

    def test_the_source_folder_is_found_from_a_status_record_then_the_hermes_home(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("CROSSTALK_P0_HERMES_SOURCE")
            self.assertIsNone(core.hermes_source_root())
            (self.hermes / "hermes-agent").mkdir()
            self.assertEqual(core.hermes_source_root(), self.hermes / "hermes-agent")
            self.record("a", host="tui")
            self.assertEqual(core.hermes_source_root(), self.source)


# ------------------------------------------------------------------ versions
class VersionWatchTests(LinkCase):
    def test_no_record_means_no_false_alarm(self):
        self.assertIsNone(core.hermes_version_notice())

    def test_install_records_the_version_and_an_update_is_flagged_until_a_selftest_passes(self):
        recorded = core.hermes_link_record_install("0.4.0")
        self.assertEqual(recorded["version"]["installed"]["version"], "0.22.5")
        self.assertIsNone(core.hermes_version_notice())
        self.stamp("0.23.0", "ffff0000ffff0000")
        notice = core.hermes_version_notice()
        self.assertEqual(notice["text"], "Hermes updated from 0.22.5 to 0.23.0 since Crosstalk was last checked")
        self.contract_tree()
        with self.host():
            result = core.hermes_selftest(wait_s=3)
        self.assertTrue(result["ok"], result["steps"])
        self.assertIsNone(result["version_notice"])
        self.assertEqual(core.hermes_link_load()["version"]["checked"]["version"], "0.23.0")
        self.assertIsNone(core.hermes_version_notice())

    def test_a_new_build_of_the_same_release_is_still_a_change(self):
        core.hermes_link_record_install("0.4.0")
        self.stamp("0.22.5", "9999999999999999")
        self.assertIn("(bf0203a794a0)", core.hermes_version_notice()["text"])
        self.assertIn("(999999999999)", core.hermes_version_notice()["text"])

    def test_the_first_healthy_sighting_seeds_the_baseline_without_an_alarm(self):
        self.heartbeat()
        link = core.hermes_link_refresh()
        self.assertEqual(link["version"]["checked"]["source"], "first-seen-healthy")
        self.assertIsNone(core.hermes_version_notice(link))

    def test_the_installer_records_the_hermes_version_and_the_plan_shows_it(self):
        target = self.root / "hermes-install"
        (target / "bin").mkdir(parents=True)
        (target / "bin" / "hermes.cmd").write_text("@echo off\n")
        (target / "config.yaml").write_text("model: x\n")
        (target / "hermes-agent").mkdir()
        (target / "hermes-agent" / "install-stamp.json").write_text(
            json.dumps({"displayVersion": "0.30.1", "commit": "abcdef0123456789"}), encoding="utf-8")
        python = self.root / "python.exe"
        python.write_text("")
        with mock.patch.dict(os.environ):
            os.environ.pop("CROSSTALK_P0_HERMES_SOURCE")
            return self.installer_checks(target, python)

    def installer_checks(self, target, python):
        plan = installer.hermes_plan(target, str(python))
        self.assertEqual((plan["plugin_version_to_install"], plan["hermes_version"]), (NEW_PLUGIN, "0.30.1+abcdef012345"))
        report = installer.hermes_install(target, str(python),
                                          runner=lambda command, **options: SimpleNamespace(returncode=0, stderr=""))
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["hermes_version_recorded"], "0.30.1+abcdef012345")
        link = core.hermes_link_load()
        self.assertEqual(link["installed_plugin_version"], NEW_PLUGIN)
        self.assertEqual(link["version"]["checked"]["source"], "install")


# ------------------------------------------------------------------ the self-test
class SelfTestTests(LinkCase):
    def test_a_healthy_link_passes_without_sending_or_injecting_anything(self):
        self.contract_tree()
        core.register_hermes(base.H1, "tool-call")
        with self.host() as host:
            result = core.hermes_selftest(wait_s=3)
        self.assertTrue(result["ok"], result["steps"])
        self.assertEqual([s["name"] for s in result["steps"]],
                         ["hermes-running", "plugin-hosting", "probe-answered", "route-lookup", "contract"])
        self.assertTrue(all(s["ok"] for s in result["steps"]))
        self.assertIn("1 of 1 known chat(s) open", {s["name"]: s["detail"] for s in result["steps"]}["route-lookup"])
        self.assertEqual(host.ctx.calls, [])
        self.assertEqual(core.letters_load(), {})
        self.assertIsNone((core.hermes_link_load().get("repair") or {}).get("state"))

    def test_a_listener_that_does_not_answer_fails_and_opens_one_repair_offer(self):
        self.contract_tree()
        with self.host(answer_probes=False):
            result = core.hermes_selftest(wait_s=0.4)
        self.assertFalse(result["ok"])
        steps = {s["name"]: s for s in result["steps"]}
        self.assertIs(steps["probe-answered"]["ok"], False)
        self.assertEqual(result["repair"]["state"], "offered")
        self.assertEqual(list((self.hermes / "crosstalk" / "probe").glob("*.json")), [])  # the unclaimed probe is withdrawn

    def test_not_running_is_inconclusive_and_offers_nothing(self):
        self.contract_tree()
        result = core.hermes_selftest(wait_s=0.2)
        self.assertEqual((result["ok"], result["inconclusive"]), (False, True))
        self.assertIsNone(result["repair"]["state"])

    def test_an_incompatible_plugin_fails_the_hosting_step(self):
        self.record("a", host=None)
        result = core.hermes_selftest(wait_s=0.2)
        steps = {s["name"]: s for s in result["steps"]}
        self.assertFalse(result["ok"])
        self.assertIs(steps["plugin-hosting"]["ok"], False)
        self.assertNotIn("probe-answered", steps)  # nothing to ask when no process hosts chats
        self.assertEqual(result["repair"]["state"], "offered")

    def test_a_changed_internal_fails_the_contract_step_even_when_the_plugin_answers(self):
        entry = core.hermes_contract_entries()[0]
        self.contract_tree(drop_need=(entry["id"], entry["needs"][0]))
        with self.host():
            result = core.hermes_selftest(wait_s=3)
        steps = {s["name"]: s for s in result["steps"]}
        self.assertIs(steps["contract"]["ok"], False)
        self.assertIn(entry["id"], steps["contract"]["detail"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["repair"]["state"], "offered")

    def test_an_unchecked_contract_is_a_note_not_a_failure(self):
        with mock.patch.dict(os.environ, {"CROSSTALK_P0_HERMES_SOURCE": str(self.root / "nowhere")}):
            with self.host():
                result = core.hermes_selftest(wait_s=3)
        steps = {s["name"]: s for s in result["steps"]}
        self.assertIsNone(steps["contract"]["ok"])
        self.assertTrue(result["ok"])

    def test_the_cli_selftest_exit_codes(self):
        self.contract_tree()
        args = SimpleNamespace(wait_seconds=0.2)
        with mock.patch.object(base.cli, "out", side_effect=lambda value, code=0: code):
            self.assertEqual(base.cli.cmd_hermes_selftest(args), 2)  # Hermes is not running: inconclusive
            with self.host():
                self.assertEqual(base.cli.cmd_hermes_selftest(SimpleNamespace(wait_seconds=3)), 0)


# ------------------------------------------------------------------ the repair offer
class RepairOfferTests(LinkCase):
    def broken(self):
        self.record("a", host=None)
        return core.hermes_selftest(wait_s=0.2)

    def test_the_mode_defaults_to_ask_and_can_be_set(self):
        self.assertEqual(core.link_repair_mode(), "ask")
        for mode in ("auto", "off", "ask"):
            self.assertEqual(core.hermes_repair_set_mode(mode)["link_repair_offer"], mode)
            self.assertEqual(core.link_repair_mode(), mode)
        with self.assertRaises(core.ProbeError):
            core.hermes_repair_set_mode("sometimes")

    def test_one_offer_per_cause_and_the_prompt_holds_everything_the_repairer_needs(self):
        self.broken()
        first = core.hermes_link_load()["repair"]
        self.broken()
        again = core.hermes_link_load()["repair"]
        self.assertEqual((first["key"], first["offer_no"], again["offer_no"]), (again["key"], 1, 1))
        text = Path(first["prompt_path"]).read_text(encoding="utf-8")
        self.assertEqual(Path(first["prompt_path"]), self.eutonos / "repair" / "hermes-link-repair.md")
        for needle in ("python tools/test_shards.py 1 --only test_crosstalk_hermes*", "0.22.5", "plugin-incompatible",
                       "none of them found the host", "tui-injector: NOT found", "HERMES_CONTRACT", "Do not restart Hermes",
                       "Do not open credential or secret files", "hermes-selftest"):
            self.assertIn(needle, text)
        self.assertIn(str(core.HERMES_PLUGIN_SOURCE), text)
        self.assertIn(str(self.source), text)
        self.assertEqual([h["app"] for h in first["harnesses"]], [])  # no Claude or Codex CLI in this sandbox

    def test_off_mode_diagnoses_but_writes_no_prompt_or_offer(self):
        core.hermes_repair_set_mode("off")
        self.broken()
        repair = core.hermes_link_load()["repair"]
        self.assertEqual((repair["state"], repair["prompt_path"]), ("off", None))
        self.assertFalse((self.eutonos / "repair").exists())

    def test_a_repaired_link_resolves_the_offer_and_a_new_break_is_a_new_offer(self):
        self.broken()
        (self.status_dir / "a.json").unlink()
        self.heartbeat()
        self.contract_tree()
        with self.host():
            core.hermes_selftest(wait_s=3)
        self.assertEqual(core.hermes_link_load()["repair"]["state"], "resolved")
        (self.status_dir / f"{os.getpid()}.json").unlink(missing_ok=True)
        (self.hermes / "crosstalk" / "heartbeat.json").unlink()
        self.broken()
        self.assertEqual(core.hermes_link_load()["repair"]["offer_no"], 2)

    def test_starting_a_repair_chat_uses_the_prompt_file_and_only_an_installed_harness(self):
        self.broken()
        with mock.patch.object(core, "claude_exe", return_value="claude.exe"), \
                mock.patch.object(core, "codex_exe", return_value=None), \
                mock.patch.object(core, "create_claude", return_value={"ok": True, "state": "created",
                                                                       "session_id": "s-1"}) as create:
            with self.assertRaises(core.ProbeError) as refused:
                core.hermes_repair_start("codex")
            self.assertEqual(refused.exception.reason, "codex-not-installed")
            self.broken()  # refresh: offered harnesses now include claude
            started = core.hermes_repair_start("claude", show=False)
            with self.assertRaises(core.ProbeError) as twice:
                core.hermes_repair_start("claude")
        self.assertEqual(twice.exception.reason, "repair-already-started")
        folder, model, effort, mode, prompt = create.call_args.args[:5]
        self.assertEqual((model, effort, mode), ("sonnet", "high", "auto"))
        self.assertIn("Repair the Crosstalk link to Hermes", prompt)
        self.assertIs(create.call_args.kwargs["show"], False)
        self.assertEqual(core.hermes_link_load()["repair"]["state"], "started")
        self.assertEqual(started["started"]["result"]["session_id"], "s-1")

    def test_auto_mode_starts_the_repair_chat_once(self):
        core.hermes_repair_set_mode("auto")
        with mock.patch.object(core, "claude_exe", return_value="claude.exe"), \
                mock.patch.object(core, "codex_exe", return_value="codex.exe"), \
                mock.patch.object(core, "create_claude", return_value={"ok": True, "session_id": "s-2"}) as create:
            self.broken()
            self.broken()
        self.assertEqual(create.call_count, 1)
        self.assertEqual(core.hermes_link_load()["repair"]["state"], "started")

    def test_a_failed_auto_start_is_recorded_and_not_retried(self):
        core.hermes_repair_set_mode("auto")
        with mock.patch.object(core, "claude_exe", return_value="claude.exe"), \
                mock.patch.object(core, "codex_exe", return_value=None), \
                mock.patch.object(core, "create_claude", side_effect=core.ProbeError("claude-create-failed")) as create:
            self.broken()
            self.broken()
        self.assertEqual(create.call_count, 1)
        repair = core.hermes_link_load()["repair"]
        self.assertEqual(repair["state"], "start-failed")
        self.assertEqual(repair["started"]["error"], "claude-create-failed")

    def test_the_cli_shows_and_sets_the_offer(self):
        self.broken()
        shown = {}
        with mock.patch.object(base.cli, "out", side_effect=lambda value, code=0: shown.update(value) or code):
            base.cli.cmd_hermes_repair(SimpleNamespace(mode=None, start=None, model=None, effort=None, no_show=False))
            self.assertEqual(shown["repair"]["state"], "offered")
            base.cli.cmd_hermes_repair(SimpleNamespace(mode="off", start=None, model=None, effort=None, no_show=False))
        self.assertEqual(core.link_repair_mode(), "off")


# ------------------------------------------------------------------ the helper pass
class HelperPassTests(LinkCase):
    def test_a_hermes_update_runs_the_selftest_once(self):
        self.contract_tree()
        core.hermes_link_record_install("0.4.0")
        self.stamp("0.23.0", "ffff0000ffff0000")
        self.contract_tree()
        with self.host(), mock.patch.object(core, "HERMES_PROBE_WAIT_S", 3.0):
            first = core.hermes_link_pass()
            second = core.hermes_link_pass()
        self.assertEqual((first["trigger"], first["selftest_ran"], first["selftest_ok"]),
                         ("version:0.23.0+ffff0000ffff", True, True))
        self.assertFalse(second["selftest_ran"])
        self.assertIsNone(second["version_notice"])

    def test_a_newly_incompatible_status_runs_the_selftest_once_and_offers_one_repair(self):
        self.contract_tree()
        self.heartbeat()
        core.hermes_link_pass()  # healthy: baseline seeded, nothing runs
        (self.hermes / "crosstalk" / "heartbeat.json").unlink()
        self.record("a", host=None)
        with mock.patch.object(core, "hermes_selftest", wraps=core.hermes_selftest) as selftest:
            first = core.hermes_link_pass()
            second = core.hermes_link_pass()
        self.assertEqual(selftest.call_count, 1)
        self.assertTrue(first["selftest_ran"] and not second["selftest_ran"])
        self.assertEqual(first["trigger"], "diagnosis:plugin-incompatible:0.22.5+bf0203a794a0")
        self.assertEqual(second["repair"], "offered")
        self.assertEqual(core.hermes_link_load()["repair"]["offer_no"], 1)

    def test_a_changed_contract_is_noticed_without_a_version_change(self):
        self.contract_tree()
        self.heartbeat()
        core.hermes_link_pass()
        self.assertEqual(core.hermes_link_load()["contract"]["ok"], True)
        entry = core.hermes_contract_entries()[2]
        (self.source / entry["file"]).write_text("nothing here\n", encoding="utf-8")
        with mock.patch.object(core, "hermes_contract_check", wraps=core.hermes_contract_check) as check:
            core.hermes_link_pass()
            core.hermes_link_pass()
        self.assertEqual(check.call_count, 0)  # same fingerprint and list: not re-read every minute
        self.stamp("0.22.6", "bf0203a794a0aabbccdd")
        core.hermes_link_pass()
        self.assertTrue(core.hermes_link_load()["contract"]["missing"])

    def test_a_machine_without_the_plugin_is_skipped(self):
        import shutil
        shutil.rmtree(self.hermes / "plugins" / "crosstalk")
        shutil.rmtree(self.hermes / "crosstalk")
        self.assertEqual(core.hermes_link_pass(), {"skipped": "hermes-not-installed"})


class HelperNoticeTests(LinkCase):
    """The helper's notice for the link: broken, Hermes updated, or nothing."""

    def setUp(self):
        super().setUp()
        with mock.patch.dict("sys.modules", {"crosstalk_p0_core": core}):
            spec = importlib.util.spec_from_file_location("crosstalk_hermes_helper", base.KIT / "crosstalk_helper.py")
            self.helper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.helper)
        self.h = self.helper.Helper(clock=time.time)

    def notices(self):
        return core.helper_notices_load().get("notices", {})

    def test_a_broken_link_gets_one_notice_naming_the_cause_and_the_offer(self):
        self.contract_tree()
        self.record("a", host=None)
        self.h.hermes_link_notice()
        notice = self.notices()["hermes-link"]
        self.assertIn("The Crosstalk link to Hermes is broken", notice["message"])
        self.assertIn("none of them found the host", notice["message"])
        self.assertIn("A repair has been offered", notice["message"])

    def test_an_update_is_a_notice_until_the_selftest_passes_and_then_it_clears(self):
        self.contract_tree()
        core.hermes_link_record_install("0.4.0")
        with mock.patch.object(core, "hermes_link_pass", return_value={
                "selftest_ran": False, "version_notice": {"text": "Hermes updated from 0.22.5 to 0.23.0 since Crosstalk was last checked"}}):
            self.h.hermes_link_notice()
        self.assertIn("Hermes updated from 0.22.5 to 0.23.0", self.notices()["hermes-link"]["message"])
        self.heartbeat()
        self.h.hermes_link_notice()
        self.assertNotIn("hermes-link", self.notices())

    def test_a_machine_without_hermes_clears_the_notice(self):
        core.helper_notice_set("hermes-link", "hermes-link", "stale")
        import shutil
        shutil.rmtree(self.hermes / "plugins" / "crosstalk")
        shutil.rmtree(self.hermes / "crosstalk")
        self.h.hermes_link_notice()
        self.assertNotIn("hermes-link", self.notices())


if __name__ == "__main__":
    unittest.main()
