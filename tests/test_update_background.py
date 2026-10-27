"""The companion's background update work: the daily check in each mode, single flight, the offer, Later, Install now
and the popup words. A throwaway key signs the releases; there is no network, and the installer process is a fake."""
import datetime
import io
import json
from pathlib import Path
import subprocess
import unittest
import unittest.mock

import test_installer as ti
from test_update_stage import PUB, sign_release, signed_source
from eutonos import installer
from eutonos import pending_actions
from eutonos import release_signing as rsg
from eutonos import release_source as rs
from eutonos import tokenslang as ts
from eutonos import update_background as ub
from eutonos import update_check as uc
from eutonos import update_stage


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class BackgroundBase(ti.UpdateBase):
    def release(self, name="r2", version="1.3.0", body="two"):
        manifest = ti.make_release(self.releases, name, version=version, body=body)
        sign_release(self.releases, name)
        return manifest

    def check(self, **extra):
        return ub.run_check(self.h, source=signed_source(self.releases), **extra)

    def staged_names(self):
        root = update_stage.staged_root(self.h)
        return sorted(path.name for path in root.iterdir()) if root.is_dir() else []


class RunCheckTests(BackgroundBase):
    def test_manual_does_nothing_at_all(self):
        self.first()
        self.release()
        uc.set_mode("manual", home=self.h)
        boom = unittest.mock.Mock(side_effect=AssertionError("checked"))
        found = ub.run_check(self.h, source=signed_source(self.releases), check=boom)
        self.assertEqual((found["outcome"], found["reason"]), ("skipped", "manual"))
        boom.assert_not_called()
        self.assertIsNone(uc.read(self.h)["last_attempt"])

    def test_nothing_runs_when_nothing_is_installed(self):
        self.release()
        self.assertEqual(self.check()["reason"], "not-installed")

    def test_with_no_pinned_key_there_is_no_network_and_a_quiet_status(self):
        self.first()
        with unittest.mock.patch.object(rs, "urlopen", side_effect=AssertionError("network")) as net,                 unittest.mock.patch.object(rsg, "PINNED_KEYS", ()):
            found = ub.run_check(self.h)  # no source given: it would ask GitHub if a key were pinned
        net.assert_not_called()
        self.assertEqual((found["outcome"], found["reason"]), ("refused", "no-trusted-key"))
        state = uc.read(self.h)
        self.assertEqual(state["last_result"], "refused:no-trusted-key")
        self.assertIsNotNone(state["last_attempt"])
        self.assertEqual(pending_actions.list_actions(self.h), [])
        self.assertEqual(self.staged_names(), [])
        self.assertFalse(uc.due(self.h))  # it is not retried at once

    def test_notify_leaves_the_pending_action_and_downloads_nothing(self):
        self.first()
        manifest = self.release()
        uc.set_mode("notify", home=self.h)
        found = self.check()
        self.assertEqual((found["outcome"], found["artifact_id"]), ("update-available", manifest["artifact_id"]))
        (entry,) = pending_actions.list_actions(self.h)
        self.assertEqual(entry["source"], uc.SOURCE)
        self.assertIn("1.3.0", entry["action"])
        self.assertEqual(self.staged_names(), [])
        self.assertIsNone(uc.read(self.h)["staged"])

    def test_auto_stages_the_release_then_asks_and_never_installs(self):
        self.first()
        manifest = self.release()
        found = self.check()
        self.assertEqual(found["outcome"], "staged", found)
        self.assertEqual(self.staged_names(), [manifest["artifact_id"]])
        self.assertEqual(uc.read(self.h)["staged"]["artifact_id"], manifest["artifact_id"])
        (entry,) = pending_actions.list_actions(self.h)
        self.assertIn("update --staged", entry["action"])
        self.assertEqual(uc.read(self.h)["last_result"], "update-available")
        offers = ub._Offers(self.h)
        with unittest.mock.patch.object(update_stage, "staged_ready", return_value=True):
            self.assertEqual(offers.current()["kind"], "ready")

    def test_a_failed_download_is_recorded_quietly_and_nothing_is_staged(self):
        self.first()
        self.release()

        class Offline(rs.FolderSource):
            def fetch_wheel(self, release, destination):
                raise ts.ReadError("unavailable", "The connection dropped.")

        found = ub.run_check(self.h, source=Offline(self.releases, allow_unsigned=False, keys=(PUB,)))
        self.assertEqual((found["outcome"], found["staging"]), ("update-available", "unavailable"))
        self.assertEqual(self.staged_names(), [])
        self.assertEqual(uc.read(self.h)["last_result"], "unavailable:stage-unavailable")
        self.assertIsNone(uc.read(self.h)["staged"])
        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)
        self.assertTrue(uc.due(self.h, now=later))  # retried sooner than a day

    def test_an_unsigned_release_is_never_staged(self):
        self.first()
        ti.make_release(self.releases, "r2", version="1.3.0", body="two")  # no signature file
        found = ub.run_check(self.h, source=rs.FolderSource(self.releases, allow_unsigned=False, keys=(PUB,)))
        self.assertNotEqual(found["outcome"], "staged")
        self.assertEqual(self.staged_names(), [])

    def test_an_exception_never_escapes_and_is_recorded(self):
        self.first()
        found = ub.run_check(self.h, source=signed_source(self.releases), check=unittest.mock.Mock(side_effect=RuntimeError("x")))
        self.assertEqual((found["outcome"], found["reason"]), ("failed", "RuntimeError"))
        self.assertEqual(uc.read(self.h)["last_result"], "failed:RuntimeError")
        self.assertEqual(pending_actions.list_actions(self.h), [])

    def test_at_most_one_check_runs_at_a_time(self):
        self.first()
        self.assertTrue(ub._flight.acquire(blocking=False))
        try:
            self.assertEqual(ub.run_check(self.h, source=signed_source(self.releases))["outcome"], "busy")
        finally:
            ub._flight.release()

    def test_an_up_to_date_check_drops_a_stale_staged_copy(self):
        self.first()
        self.release()
        self.check()
        self.assertEqual(len(self.staged_names()), 1)
        self.update(source=update_stage.staged_source(self.h, keys=(PUB,)), allow_unsigned=False)
        found = self.check()
        self.assertEqual(found["outcome"], "up-to-date")
        self.assertEqual(self.staged_names(), [])


class AgentTests(ti.UpdateBase):
    def agent(self, **extra):
        self.clock = Clock()
        self.calls = []
        values = dict(clock=self.clock, spawn=lambda target: target(), start_delay=90.0, poll_seconds=15.0,
                      check=lambda home: self.calls.append(home) or {"outcome": "up-to-date"},
                      runner=lambda home, staged: {"outcome": "updated"})
        values.update(extra)
        return ub.UpdateAgent(self.h, **values)

    def offer(self, kind="available", version="9.0.0", artifact="eutonos-9.0.0-aaaaaaaaaaaa", **extra):
        found = {"version": version, "artifact_id": artifact, **extra}
        target = uc.path(self.h)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"profile": "eutonos.update-check.v1", "mode": "auto", "last_available": found,
                                      "staged": found if kind == "ready" else None}), encoding="utf-8")
        pending_actions.add(self.h, source=uc.SOURCE, action=f"Tell the person {version} is available.", why="x")
        return found

    def test_no_network_work_before_the_start_delay_then_one_check_when_due(self):
        agent = self.agent()
        self.clock.now += 30
        agent.tick()
        self.assertEqual(self.calls, [])  # the companion only just started
        self.clock.now += 100
        agent.tick()
        self.assertEqual(self.calls, [self.h])
        agent.tick()  # inside the poll window
        self.assertEqual(len(self.calls), 1)

    def test_the_last_attempt_time_stops_a_restart_from_checking_again(self):
        uc.record_attempt("up-to-date", home=self.h)
        agent = self.agent()
        self.clock.now += 200
        agent.tick()
        self.assertEqual(self.calls, [])
        uc.set_mode("manual", home=self.h)
        self.assertFalse(uc.due(self.h))

    def test_a_check_never_overlaps_another(self):
        started = []
        agent = self.agent(spawn=lambda target: started.append(target), check=lambda home: {"outcome": "up-to-date"})
        self.clock.now += 200
        agent.tick()  # the poll thread is queued, not run
        self.assertEqual(len(started), 1)
        agent.tick()
        self.clock.now += 20
        agent.tick()
        self.assertEqual(len(started), 1)  # a second poll is not started while one is pending
        started.pop(0)()                    # the poll runs and queues the check
        self.assertTrue(agent.checking)
        self.assertEqual(len(started), 1)
        self.clock.now += 20
        agent.tick()
        started.pop()()                     # a second poll sees a check in flight and queues nothing more
        self.assertEqual(len(started), 1)   # still only the one check
        self.assertTrue(agent.checking)
        started.pop()()                     # the check finishes
        self.assertFalse(agent.checking)

    def test_the_offer_shows_ready_available_and_blocked_words(self):
        self.offer("available")
        agent = self.agent()
        agent._poll()
        view = ub.update_view(agent)
        self.assertEqual((view["show"], view["kind"], view["headline"]), (True, "available", "EUTONOS 9.0.0 is available"))
        self.assertEqual([label for label, _ in view["buttons"]], ["Download and install", "Later"])
        agent.offer = {**agent.offer, "kind": "ready"}
        view = ub.update_view(agent)
        self.assertEqual((view["headline"], [label for label, _ in view["buttons"]]),
                         ("EUTONOS 9.0.0 is ready", ["Install now", "Later"]))
        self.assertIn("Nothing is installed until you click", view["detail"])
        agent.offer = {**agent.offer, "kind": "blocked", "detail": "It needs a newer Python"}
        view = ub.update_view(agent)
        self.assertEqual([label for label, _ in view["buttons"]], ["Later"])
        self.assertIn("Run Setup again", view["detail"])
        self.assertFalse(ub.update_view(None)["show"])

    def test_later_hides_the_offer_until_a_fresh_check_runs(self):
        self.offer("available")
        agent = self.agent(check=lambda home: {"outcome": "update-available"})
        agent._poll()
        self.assertIsNotNone(agent.visible_offer())
        before = agent.revision
        agent.later()
        self.assertIsNone(agent.visible_offer())
        self.assertFalse(ub.update_view(agent)["show"])
        self.assertGreater(agent.revision, before)
        self.assertIsNotNone(agent.offer)  # the pending action is untouched
        agent._run_check()
        self.assertIsNotNone(agent.visible_offer())  # the next daily check asks again

    def test_a_skipped_check_does_not_end_the_snooze(self):
        self.offer("available")
        agent = self.agent(check=lambda home: {"outcome": "skipped"})
        agent._poll()
        agent.later()
        agent._run_check()
        self.assertIsNone(agent.visible_offer())

    def test_an_offer_someone_dismissed_elsewhere_disappears(self):
        self.offer("available")
        agent = self.agent()
        agent._poll()
        pending_actions.dismiss_source(self.h, uc.SOURCE)
        agent._poll()
        self.assertIsNone(agent.offer)

    def test_install_now_runs_the_update_in_its_own_process_then_asks_for_a_restart(self):
        self.offer("ready")
        ran = []
        agent = self.agent(runner=lambda home, staged: ran.append((home, staged)) or {"outcome": "updated"})
        agent.offer = {**agent._offers.current(), "kind": "ready"}
        self.assertTrue(agent.install_now())
        self.assertEqual(ran, [(self.h, True)])  # a ready offer installs from the staged folder
        self.assertEqual(agent.install["phase"], "done")
        self.assertTrue(agent.restart_requested)
        self.assertIsNone(agent.offer)
        self.assertEqual(ub.update_view(agent)["headline"], "EUTONOS 9.0.0 is installed")

    def test_an_available_offer_installs_by_downloading(self):
        self.offer("available")
        ran = []
        agent = self.agent(runner=lambda home, staged: ran.append(staged) or {"outcome": "updated"})
        agent._poll()
        agent.install_now()
        self.assertEqual(ran, [False])

    def test_a_busy_installer_keeps_the_offer_and_says_so(self):
        self.offer("available")
        agent = self.agent(runner=lambda home, staged: {"outcome": "busy", "reason": "lock held"})
        agent._poll()
        agent.install_now()
        self.assertEqual(agent.install["phase"], "failed")
        self.assertFalse(agent.restart_requested)
        view = ub.update_view(agent)
        self.assertIn("Another EUTONOS install or update is running", view["note"])
        self.assertEqual([label for label, _ in view["buttons"]], ["Download and install", "Later"])

    def test_a_failed_update_says_the_earlier_version_is_still_in_use(self):
        self.offer("available")
        agent = self.agent(runner=lambda home, staged: {"outcome": "refused", "reason": "sha256-mismatch"})
        agent._poll()
        agent.install_now()
        self.assertIn("sha256-mismatch", agent.install["message"])
        self.assertIn("earlier version is still in use", agent.install["message"])
        self.assertFalse(agent.restart_requested)
        crashed = self.agent(runner=lambda home, staged: (_ for _ in ()).throw(RuntimeError("x")))
        crashed._poll()
        crashed.install_now()
        self.assertEqual(crashed.install["phase"], "failed")

    def test_install_now_is_single_flight_and_refuses_a_blocked_or_missing_offer(self):
        self.offer("available")
        queued = []
        agent = self.agent(spawn=lambda target: queued.append(target))
        self.assertFalse(agent.install_now())  # nothing offered yet
        agent.offer = agent._offers.current()
        self.assertTrue(agent.install_now())
        self.assertFalse(agent.install_now())  # one is already running
        self.assertEqual(ub.update_view(agent)["kind"], "installing")
        self.assertEqual(len(queued), 1)
        blocked = self.agent()
        blocked.offer = {"version": "9.0.0", "artifact_id": "a", "kind": "blocked", "detail": "x"}
        self.assertFalse(blocked.install_now())


class ProcessTests(unittest.TestCase):
    def test_the_installer_command_is_isolated_and_only_a_ready_offer_is_staged(self):
        with unittest.mock.patch.object(Path, "is_file", return_value=False):
            plain = ub.installer_command(Path("H"), False, python="C:/rt/python.exe")
            staged = ub.installer_command(Path("H"), True, python="C:/rt/python.exe")
        self.assertEqual(plain[1:], ["-I", "-B", "-m", "eutonos.installer", "--home", "H", "update"])
        self.assertEqual(staged[-2:], ["update", "--staged"])
        self.assertTrue(plain[0].endswith("python.exe"))

    def test_the_answer_is_read_from_pretty_json_after_any_noise(self):
        text = json.dumps({"outcome": "updated", "version": "1.3.0"}, indent=2)
        self.assertEqual(ub.parse_answer(text)["outcome"], "updated")
        self.assertEqual(ub.parse_answer("warning: x\n" + text)["version"], "1.3.0")
        self.assertEqual(ub.parse_answer("not json"), {})
        self.assertEqual(ub.parse_answer(None), {})

    def test_run_installer_reports_each_way_it_can_end(self):
        def done(stdout="", stderr="", code=0):
            return lambda argv, **kw: subprocess.CompletedProcess(argv, code, stdout, stderr)

        self.assertEqual(ub.run_installer(Path("H"), True, run=done(json.dumps({"outcome": "updated"})))["outcome"], "updated")
        self.assertEqual(ub.run_installer(Path("H"), True, run=done("", "boom", 1))["detail"], "boom")
        self.assertEqual(ub.run_installer(Path("H"), True, run=done("", "", 3))["outcome"], "failed")

        def slow(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 1)

        def missing(argv, **kw):
            raise FileNotFoundError("python")

        self.assertIn("did not finish in time", ub.run_installer(Path("H"), False, run=slow)["detail"])
        self.assertIn("could not be started", ub.run_installer(Path("H"), False, run=missing)["detail"])


class StagedCommandTests(ti.UpdateBase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            code = installer.main(["--home", str(self.h), *argv])
        return code, json.loads(out.getvalue())

    def test_update_staged_refuses_when_nothing_is_staged_or_with_a_source(self):
        self.first()
        code, answer = self.run_cli("update", "--staged")
        self.assertEqual((code, answer["reason"]), (1, "nothing-staged"))
        code, answer = self.run_cli("update", "--staged", "--source", str(self.releases))
        self.assertEqual((code, answer["reason"]), (1, "invalid"))
        code, answer = self.run_cli("update", "--staged", "--allow-unsigned")
        self.assertEqual((code, answer["reason"]), (1, "invalid"))

    def test_update_staged_uses_the_staging_folder_with_no_unsigned_override_and_clears_the_offer(self):
        self.first()
        manifest = ti.make_release(self.releases, "r2", version="1.3.0", body="two")
        sign_release(self.releases, "r2")
        source = signed_source(self.releases)
        update_stage.stage(source, rs.newest(source.releases(), "dev"), self.h)
        found = {"version": "1.3.0", "artifact_id": manifest["artifact_id"]}
        target = uc.path(self.h)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"profile": "eutonos.update-check.v1", "mode": "auto", "last_available": found,
                                      "staged": found}), encoding="utf-8")
        pending_actions.add(self.h, source=uc.SOURCE, action="Tell the person.", why="x")
        with unittest.mock.patch.object(rsg, "PINNED_KEYS", (PUB,)), \
                unittest.mock.patch.object(installer, "update", return_value={"outcome": "updated"}) as fake:
            code, answer = self.run_cli("update", "--staged")
        self.assertEqual(code, 0)
        kwargs = fake.call_args.kwargs
        self.assertEqual(kwargs["source"], str(update_stage.staged_root(self.h)))
        self.assertFalse(kwargs["allow_unsigned"])
        self.assertTrue(answer["offer"]["cleared"])
        self.assertEqual(pending_actions.list_actions(self.h), [])
        self.assertEqual(self.staged(), [])

    def staged(self):
        root = update_stage.staged_root(self.h)
        return sorted(path.name for path in root.iterdir()) if root.is_dir() else []


if __name__ == "__main__":
    unittest.main()
