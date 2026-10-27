"""IPC3 lane B: per-project hosts, the projects registry and registry-resolved pickup."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from eutonos import ReadError, boards, home, projects, setup_cli, work_lifecycle, workflow_cli
from eutonos.host import connect, enroll, status


def git(root, *args, env=None):
    return subprocess.run(["git", "-c", "safe.directory=" + str(root), "-C", str(root), *args],
                          env=env, check=True, capture_output=True).stdout


class ProjectsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eutonos-projects-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "eutonos-home"
        patcher = mock.patch.dict(os.environ, {"EUTONOS_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        self.root = self.make_repo("My Project")

    def make_repo(self, name):
        root = self.base / name
        root.mkdir()
        git(root, "init", env=self.env)
        (root / "native.md").write_bytes(b"Existing project\n")
        (root / "docs").mkdir()
        (root / "docs/CURRENT_TASK.md").write_text("# Historical compatibility owner\nLive work is in the native board.\n")
        (root / "eutonos.boards.json").write_text(json.dumps({"profile": boards.CONFIG, "owners": ["BOARD.md"]}))
        (root / "BOARD.md").write_text("```tokenslang-board\n" + json.dumps({
            "profile": boards.BOARD, "items": [{"id": "live", "type": "quick", "title": "Live work",
                                                "summary": "Current native work", "status": "active"}]}) + "\n```\n")
        (root / "eutonos.work.json").write_text(json.dumps({"profile": work_lifecycle.CONFIG, "current": ["live"], "next": []}))
        git(root, "add", ".", env=self.env)
        git(root, "-c", "user.name=EUTONOS test", "-c", "user.email=test@example.invalid", "commit", "-m", "baseline", env=self.env)
        return root

    def cli(self, module, *argv):
        raw = io.BytesIO()
        console = io.TextIOWrapper(raw, encoding="utf-8")
        with mock.patch("sys.stdout", console):
            code = module.main(list(argv))
        console.flush()
        value = json.loads(raw.getvalue().decode("utf-8"))
        console.detach()
        return code, value

    def test_project_key_shape_and_stability(self):
        key = projects.project_key(self.root)
        self.assertRegex(key, r"^my-project-[0-9a-f]{12}$")
        self.assertEqual(key, projects.project_key(str(self.root) + os.sep))
        self.assertNotEqual(key, projects.project_key(self.base / "other"))
        self.assertRegex(projects.project_key(self.base / ("X" * 40)), r"^x{24}-[0-9a-f]{12}$")
        self.assertRegex(projects.project_key(self.base / "!!!"), r"^project-[0-9a-f]{12}$")

    def test_add_enrolls_no_expiry_host_under_home_and_records_it(self):
        code, result = self.cli(setup_cli, "add-project", "--root", str(self.root), "--actor", "tester")
        self.assertEqual(code, 0)
        self.assertEqual((result["outcome"], result["adopted_host"]), ("added", False))
        host = home.hosts() / projects.project_key(self.root)
        admission = json.loads((host / "admission.json").read_text(encoding="utf-8"))
        self.assertIsNone(admission["expires_at"])
        self.assertEqual(admission["actor"], "tester")
        self.assertFalse(host.is_relative_to(self.root))
        registry = json.loads(home.projects_file().read_text(encoding="utf-8"))
        self.assertEqual(registry["profile"], "eutonos.projects.v1")
        entry, = registry["projects"]
        self.assertEqual((entry["root"], entry["host"], entry["actor"]), (str(self.root), str(host), "tester"))
        self.assertEqual(entry["identity"], list(projects.LocalRoot(self.root).identity))
        self.assertTrue(entry["added_at"].endswith("+00:00"))
        self.assertEqual(status(self.root, host)["admission"]["state"], "no-expiry")

    def test_add_is_idempotent(self):
        first = projects.add(self.root, "tester")
        registry = home.projects_file().read_bytes()
        admission = (home.hosts() / projects.project_key(self.root) / "admission.json").read_bytes()
        again = projects.add(self.root, "someone-else")
        self.assertEqual(again["outcome"], "already-added")
        self.assertEqual(again["project"], first["project"])
        self.assertEqual(home.projects_file().read_bytes(), registry)
        self.assertEqual((home.hosts() / projects.project_key(self.root) / "admission.json").read_bytes(), admission)
        self.assertEqual(len(list(home.hosts().iterdir())), 1)

    def test_add_adopts_existing_host_for_the_same_root_without_rewriting_it(self):
        adopted = home.hosts() / "eutonos"
        home.hosts().mkdir(parents=True)
        enroll(self.root, adopted, "owner-actor", [], hours=8)
        before = (adopted / "admission.json").read_bytes()
        finite = json.loads(before)["expires_at"]
        self.assertIsInstance(finite, float)
        # A host for another root must not be adopted.
        other = self.make_repo("other")
        enroll(other, home.hosts() / "aaa-other", "other-actor", [], hours=8)
        result = projects.add(self.root, "tester")
        self.assertEqual((result["outcome"], result["adopted_host"]), ("added", True))
        self.assertEqual(result["project"]["host"], str(adopted))
        self.assertEqual(result["project"]["actor"], "owner-actor")
        self.assertEqual(result["requested_actor"], "tester")
        self.assertEqual((adopted / "admission.json").read_bytes(), before)
        self.assertEqual(sorted(path.name for path in home.hosts().iterdir()), ["aaa-other", "eutonos"])
        # A finite admission still connects and reports its historical state.
        connect(self.root, adopted, "owner-actor")
        self.assertIn(status(self.root, adopted)["admission"]["state"], ("historical-current", "historical-expired"))
        self.assertEqual(projects.resolve_host(self.root), str(adopted))

    def test_two_projects_get_distinct_hosts(self):
        other = self.make_repo("second")
        projects.add(self.root, "a")
        projects.add(other, "a")
        self.assertEqual(len({item["host"] for item in projects.load()["projects"]}), 2)
        self.assertEqual(projects.resolve_host(other), str(home.hosts() / projects.project_key(other)))

    def test_host_is_never_created_inside_the_project_tree(self):
        with mock.patch.dict(os.environ, {"EUTONOS_HOME": str(self.root / ".eutonos-home")}):
            with self.assertRaises(ReadError) as caught:
                projects.add(self.root, "tester")
        self.assertEqual(caught.exception.code, "denied")
        self.assertFalse((self.root / ".eutonos-home").exists())

    def test_existing_unmatched_host_directory_is_never_reset(self):
        stray = home.hosts() / projects.project_key(self.root)
        stray.mkdir(parents=True)
        (stray / "keep.txt").write_bytes(b"keep")
        with self.assertRaises(ReadError) as caught:
            projects.add(self.root, "tester")
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual((stray / "keep.txt").read_bytes(), b"keep")
        self.assertFalse(home.projects_file().exists())

    def test_damaged_registry_is_refused_not_overwritten(self):
        home.projects_file().parent.mkdir(parents=True)
        home.projects_file().write_bytes(b"{not json")
        with self.assertRaises(ReadError):
            projects.add(self.root, "tester")
        self.assertEqual(home.projects_file().read_bytes(), b"{not json")
        self.assertIsNone(projects.resolve_host(self.root))

    def test_pickup_without_host_resolves_registry_host(self):
        code, bare = self.cli(workflow_cli, "pickup", "--root", str(self.root))
        self.assertEqual((code, bare["outcome"]), (0, "pickup"))
        self.assertNotIn("completion_inbox", bare)
        projects.add(self.root, "tester")
        code, result = self.cli(workflow_cli, "pickup", "--root", str(self.root))
        self.assertEqual((code, result["outcome"]), (0, "pickup"))
        self.assertNotEqual(result["completion_inbox"]["outcome"], "attention-required")
        self.assertEqual(result["completion_inbox"]["outcome"], "clear")

    def two_hosts_for_one_root(self):
        home.hosts().mkdir(parents=True)
        for name, actor in (("apl-task-host", "task-agent"), ("eutonos", "owner-actor")):
            enroll(self.root, home.hosts() / name, actor, [], hours=8)
        return home.hosts() / "apl-task-host", home.hosts() / "eutonos"

    def test_ipc4_f1_two_hosts_for_one_root_are_ambiguous_and_host_picks_one(self):
        task_host, main_host = self.two_hosts_for_one_root()
        admissions = {path: path.joinpath("admission.json").read_bytes() for path in (task_host, main_host)}
        code, result = self.cli(setup_cli, "add-project", "--root", str(self.root))
        self.assertEqual((code, result["outcome"]), (2, "ambiguous"))
        self.assertEqual([item["host"] for item in result["candidates"]], [str(task_host), str(main_host)])
        self.assertEqual([item["actor"] for item in result["candidates"]], ["task-agent", "owner-actor"])
        self.assertIn("--host", result["next"])
        self.assertFalse(home.projects_file().exists())  # nothing was registered
        with self.assertRaises(ReadError):  # --host must name a host of this root
            projects.add(self.root, host=self.base / "nope")
        other = self.make_repo("other-root")
        enroll(other, home.hosts() / "other", "x", [], hours=8)
        with self.assertRaises(ReadError) as wrong:
            projects.add(self.root, host=home.hosts() / "other")
        self.assertEqual(wrong.exception.code, "wrong-scope")
        self.assertFalse(home.projects_file().exists())
        for argument in (str(main_host), "hosts/eutonos", "eutonos"):  # an absolute path, or one under the home
            with self.subTest(host=argument):
                if home.projects_file().exists():
                    home.projects_file().unlink()
                code, chosen = self.cli(setup_cli, "add-project", "--root", str(self.root), "--host", argument)
                self.assertEqual((code, chosen["outcome"], chosen["adopted_host"]), (0, "added", True))
                self.assertEqual((chosen["project"]["host"], chosen["project"]["actor"]), (str(main_host), "owner-actor"))
        # Already bound: a different --host is a conflict, the same one is a no-op.
        self.assertEqual(projects.add(self.root, host=main_host)["outcome"], "already-added")
        with self.assertRaises(ReadError) as moved:
            projects.add(self.root, host=task_host)
        self.assertEqual(moved.exception.code, "conflict")
        self.assertEqual(projects.resolve_host(self.root), str(main_host))
        for path, data in admissions.items():  # no host was rewritten
            self.assertEqual(path.joinpath("admission.json").read_bytes(), data)

    def test_ipc4_f1_a_single_match_is_still_adopted_without_host(self):
        home.hosts().mkdir(parents=True)
        enroll(self.root, home.hosts() / "zzz-only", "solo", [], hours=None)
        result = projects.add(self.root)
        self.assertEqual((result["outcome"], result["project"]["actor"]), ("added", "solo"))

    def test_ipc4_f15_adoption_opens_only_hosts_whose_admission_names_the_root(self):
        home.hosts().mkdir(parents=True)
        for index in range(3):
            enroll(self.make_repo(f"other-{index}"), home.hosts() / f"a-other-{index}", "x", [], hours=8)
        (home.hosts() / "b-stray").mkdir()  # no admission at all
        (home.hosts() / "b-junk").mkdir()
        (home.hosts() / "b-junk" / "admission.json").write_text("{not json", encoding="utf-8")
        enroll(self.root, home.hosts() / "c-mine", "me", [], hours=8)
        with mock.patch.object(projects, "read_snapshot", wraps=projects.read_snapshot) as opened:
            result = projects.add(self.root)
        self.assertEqual(result["project"]["host"], str(home.hosts() / "c-mine"))
        self.assertEqual([Path(call.args[1]).name for call in opened.call_args_list], ["c-mine"])

    def test_ipc4_f14_registry_lookup_compares_resolved_paths(self):
        projects.add(self.root, "tester")
        link = self.base / "linked-project"
        if os.name == "nt":
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(self.root)], capture_output=True).returncode == 0
        else:
            try:
                os.symlink(self.root, link)
                made = True
            except OSError:
                made = False
        if not made:
            self.skipTest("cannot create a junction or symlink here")
        self.assertTrue(projects._same(link, self.root))
        self.assertEqual(projects.resolve_host(link), projects.resolve_host(self.root))
        self.assertIsNotNone(projects.resolve_host(link))
        self.assertEqual(projects.lookup(link)["root"], str(self.root))

    def test_explicit_host_still_wins_over_registry(self):
        projects.add(self.root, "tester")
        with mock.patch.object(workflow_cli, "_completion_inbox", return_value={"outcome": "clear"}) as inbox:
            self.cli(workflow_cli, "pickup", "--root", str(self.root), "--host", "explicit-host")
        inbox.assert_called_once_with("explicit-host", str(self.root))

    def test_hosts_report_lists_without_changing_anything(self):
        expired_root = self.make_repo("old")
        home.hosts().mkdir(parents=True)
        enroll(expired_root, home.hosts() / "old-host", "a", [], hours=1)
        projects.add(self.root, "tester")
        (home.home() / "runtime.backup-20260101-000000").mkdir()
        (home.home() / "runtime.upgrade-tmp").mkdir()
        (home.home() / "runtime").mkdir()
        before = sorted(str(path) for path in home.home().rglob("*"))
        report = projects.hosts_report(now=time.time() + 7200)
        self.assertEqual(report["changed"], False)
        self.assertEqual([item["host"] for item in report["expired_hosts"]], [str(home.hosts() / "old-host")])
        self.assertTrue(report["expired_hosts"][0]["root_exists"])
        self.assertEqual(sorted(Path(item["path"]).name for item in report["leftover_folders"]),
                         ["runtime.backup-20260101-000000", "runtime.upgrade-tmp"])
        self.assertEqual(sorted(str(path) for path in home.home().rglob("*")), before)
        code, via_cli = self.cli(setup_cli, "hosts-report")
        self.assertEqual((code, via_cli["outcome"], len(via_cli["leftover_folders"])), (0, "report", 2))

    def test_hosts_report_on_missing_home(self):
        report = projects.hosts_report()
        self.assertEqual((report["expired_hosts"], report["leftover_folders"]), ([], []))

    def test_dashboard_host_discovery_reads_a_no_expiry_host(self):
        from eutonos import dashboard
        projects.add(self.root, "tester")
        fake_home = self.base / "fake-user-home"
        (fake_home / ".eutonos").mkdir(parents=True)
        os.rename(home.hosts(), fake_home / ".eutonos" / "hosts")
        # Works whether the dashboard reads Path.home()/.eutonos (before lane A) or EUTONOS_HOME (after).
        env = {key: value for key, value in os.environ.items() if key != "EUTONOS_HOME"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("pathlib.Path.home", return_value=fake_home):
            snapshot = dashboard._host(self.root)
        self.assertEqual((snapshot["status"], snapshot["actor"], snapshot["expires_at"]), ("available", "tester", None))

    def test_ipc5_f8_concurrent_adds_under_the_lock_lose_no_entry(self):
        import sys
        import threading
        roots = [self.make_repo(f"race-{index}") for index in range(4)]
        errors = []

        def add(root):
            try:
                projects.add(root, "racer")
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=add, args=(root,)) for root in roots]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(item["root"] for item in projects.load()["projects"]), sorted(str(root) for root in roots))
        # And across processes: three more roots added by three interpreters at once.
        more = [self.make_repo(f"proc-{index}") for index in range(3)]
        script = "import sys; from eutonos import projects; projects.add(sys.argv[1], 'proc')"
        running = [subprocess.Popen([sys.executable, "-c", script, str(root)], env=dict(os.environ),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE) for root in more]
        for process in running:
            _, err = process.communicate(timeout=120)
            self.assertEqual(process.returncode, 0, err.decode(errors="replace"))
        registered = {item["root"] for item in projects.load()["projects"]}
        self.assertEqual(registered, {str(root) for root in roots + more})

    def test_ipc5_f8_a_held_lock_refuses_instead_of_hanging(self):
        with projects.locked():
            self.assertEqual(projects.lookup(self.root), None)  # reads never take the lock
        import threading
        release = threading.Event()
        held = threading.Event()

        def holder():
            with projects.locked():
                held.set()
                release.wait(10)

        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(held.wait(5))
        try:
            with mock.patch.object(projects, "LOCK_SECONDS", 0.2), self.assertRaises(ReadError) as caught:
                projects.remove(self.root)
            self.assertEqual(caught.exception.code, "unavailable")
        finally:
            release.set()
            thread.join()

    def test_ipc5_remove_unregisters_without_touching_the_host(self):
        added = projects.add(self.root, "tester")
        host = Path(added["project"]["host"])
        before = sorted(str(path) for path in host.rglob("*"))
        self.assertEqual(projects.remove(self.root)["outcome"], "removed")
        self.assertIsNone(projects.lookup(self.root))
        self.assertEqual(projects.load()["projects"], [])
        self.assertEqual(sorted(str(path) for path in host.rglob("*")), before)
        self.assertEqual(projects.remove(self.root), {"outcome": "absent", "project": None, "changed": False})

    def test_ipc5_retire_host_marks_and_records_but_never_deletes_or_rewrites(self):
        added = projects.add(self.root, "tester")
        host = Path(added["project"]["host"])
        admission = (host / "admission.json").read_bytes()
        database = (host / "coordinator.sqlite").read_bytes()
        self.assertEqual(projects.host_state(host), "active")
        result = projects.retire_host(host, root=self.root, reason="test")
        self.assertEqual(result["outcome"], "retired")
        self.assertEqual(projects.host_state(host), "retired")
        self.assertEqual((host / "admission.json").read_bytes(), admission)
        self.assertEqual((host / "coordinator.sqlite").read_bytes(), database)
        self.assertEqual(json.loads((host / "retired.json").read_text())["history"][0]["event"], "retired")
        self.assertEqual(projects.load()["retired_hosts"][0]["host"], str(host))
        self.assertEqual(projects.retire_host(host)["outcome"], "already-retired")
        from eutonos.host import read_snapshot
        self.assertEqual(read_snapshot(self.root, host)["status"], "available")  # the admission still validates

    def test_ipc5_a_retired_host_is_adopted_again_when_the_project_returns(self):
        host = Path(projects.add(self.root, "tester")["project"]["host"])
        projects.remove(self.root)
        projects.retire_host(host, root=self.root)
        again = projects.add(self.root, "tester")
        self.assertEqual((again["outcome"], again["adopted_host"], again["project"]["host"]), ("added", True, str(host)))
        self.assertEqual(projects.host_state(host), "active")
        self.assertEqual([entry["event"] for entry in json.loads((host / "retired.json").read_text())["history"]],
                         ["retired", "reinstated"])

    def test_ipc5_effects_are_recorded_per_project(self):
        with self.assertRaises(ReadError):
            projects.set_effects(self.root, [{"effect_id": "x"}])
        projects.add(self.root, "tester")
        projects.set_effects(self.root, [{"effect_id": "x"}])
        self.assertEqual(projects.effects_for(self.root), [{"effect_id": "x"}])
        projects.set_effects(self.root, [])
        self.assertEqual(projects.effects_for(self.root), [])


if __name__ == "__main__":
    unittest.main()
