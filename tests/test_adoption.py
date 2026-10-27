"""Preservation-first profile adoption and managed upgrade behavior."""

import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from eutonos import adoption, completion_policy as cp, nav, rule_index, tokenslang as ts
from eutonos.effects import LocalRoot
from eutonos.host import connect, enroll, selected_apply


# Exact shipped 09c3199 output, frozen independently of today's renderer and
# contract. Its digest is asserted before it reaches the guarded adoption seam.
LEGACY_ADOPTION_BLOCK = """<!-- eutonos:delegation:start -->
## EUTONOS delegation

This repository enables instruction-based delegation through the agent host's
existing worker tools. The repository's native owners, boot protocol, write
coordination, tests, and publication rules remain authoritative. Delegation
does not install EUTONOS services or grant writes, Git publication, deployment,
external effects, credentials, or model upgrades.

At pickup, when the managed wrapper exposes settings, run
`python tools/eutonos_project.py settings status` once and honor its effective
routing and task overrides. Otherwise use the policy here without requiring a
missing runtime: adaptive delegation is on and model/effort selection is Auto.
The primary balances expected correctness, total cost, handoff, review, and
latency; prefer a cheaper helper only when it is likely to return the same useful
quality. A user task or session choice overrides the default. Persist a durable
choice only when explicitly requested.

The primary may give one bounded, non-recursive helper a complete assignment.
Moving above the current primary model requires user approval unless already
selected. Continue independent work while approval is pending; if no answer is
given, keep that work on the current primary. Read-only helpers need no write
lease. Writing helpers use the repository's existing coordination for their own
exact scope, preserve unrelated work, run relevant checks, and return source
paths, findings, changes, unresolved risks, and actual closeout. If delegation
is disabled or unavailable, the primary continues normally without a setup loop.

Use `python tools/eutonos_project.py workflow help` once when unfamiliar with
the installed operations, then call known commands directly. For substantive
work, keep the native current-task checkpoint recoverable. Before a substantive
final, use `workflow prepare closeout`, answer its semantic prompts, and use
`workflow run closeout` with the existing actor/work context. Respect effective
RAM capture cadence and retrieval settings; do not manufacture receipt fields.
Quick edits need no campaign or repeated discovery. Exact source and native
owners outrank RAM/NAV summaries. A read-only task never authorizes new writes.

`version status` shows optional project/module versions separately from the
installed EUTONOS runtime. Follow the project's own version convention at useful
milestones; do not bump versions on every tiny edit. Portable reference docs
are upstream guidance, not the consumer's selected work board or acceptance.
## Completion contract

Requests to implement features or finish campaigns, stages, slices or implementation work include integration and merging into the established target unless explicitly scoped to exploration, research, testing, prototypes or another narrower deliverable. Implementation is finished when scoped changes pass required checks, are integrated into that target, and the target state is verified. Local edits, a commit, passing tests, worker handback or an open PR are intermediate; unmerged implementation remains unfinished. The coordinator owns final integration. Resolve the target from repository and campaign rules (a stage target may be a campaign branch, not main); ask only about material ambiguity. Preserve required reviews, branch protections, dependency ordering, concurrent work and explicit narrower instructions. Use the appropriate fast-forward, squash or merge strategy. If blocked, state the exact remaining step rather than claim completion. Merge does not authorize deployment, installation, restart or release, bypass permissions, or widen the task. Follow applicable global and repository instructions; surface conflicts rather than silently overriding them.
<!-- eutonos:delegation:end -->
"""


class AdoptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eutonos-adoption-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.root = self.home / "project"
        self.root.mkdir(); (self.root / "tools").mkdir(); (self.root / "docs").mkdir()
        work = {"profile": ts.WORK, "work_id": "project-work", "goal": "Preserve native project work",
                "status": "selected", "required": [], "optional": []}
        (self.root / "docs/WORK.md").write_bytes(
            b"# Native work\n\n```tokenslang-work\n" + ts.encoded(work) + b"\n```\n")
        (self.root / "docs/ARCHITECTURE.md").write_text("# Native architecture\nExisting owner facts remain authoritative.\n", encoding="utf-8")
        (self.root / "AGENTS.md").write_text("# Existing agent rules\nPreserve the native boot protocol.\n", encoding="utf-8")
        self.git("init", "-q", "-b", "main")
        self.git("add", "--all"); self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                                             "commit", "-qm", "baseline")
        self.local = LocalRoot(self.root)
        self.original_agents = self.local.read("AGENTS.md")

    def git(self, *args):
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        return subprocess.check_output(["git", "-c", "safe.directory=" + str(self.root), "-C", str(self.root),
                                        *args], env=env, stderr=subprocess.PIPE)

    def profile(self, modules=("docs", "delegation"), nav_config=None):
        value = {"profile": adoption.PROFILE, "repository_id": "project.repo", "scope_id": "project.local",
                 "namespace": "project.native", "owners": {
                     "current_task": {"path": "docs/WORK.md", "resource_id": "project.doc.work"},
                     "architecture": {"path": "docs/ARCHITECTURE.md", "resource_id": "project.doc.architecture"}},
                 "modules": list(modules)}
        if nav_config is not None:
            value["nav"] = nav_config
        return value

    def apply(self, value):
        host = self.home / ("host-" + str(len(list(self.home.glob("host-*")))))
        enroll(self.root, host, "owner", value["paths"])
        coordinator, authority = connect(self.root, host, "owner")
        attempt = "adopt"
        generation = coordinator.start(authority, attempt)
        coordinator.reserve(authority, attempt, generation, value["paths"])
        return selected_apply(coordinator, authority, attempt, generation, "adoption",
                              value["effect"], value["fingerprint"])

    def test_profile_example_is_valid_and_malformed_discovery_is_structured(self):
        description = adoption.profile_description()
        self.assertEqual(adoption.validate_profile(description["example"])["profile"], adoption.PROFILE)
        (self.root / "eutonos.ram.json").write_text("[]\n", encoding="utf-8")
        result = adoption.discover(self.local)
        self.assertIn({"path": "eutonos.ram.json", "code": "invalid"}, result["diagnostics"])

    def test_custom_owners_without_ram_apply_and_verify_docs_delegation(self):
        profile = self.profile()
        planned = adoption.plan_profile(self.local, profile)
        self.assertEqual(planned["profile"], "eutonos.tokenslang.setup-plan.v1")
        self.assertNotIn("eutonos.ram.json", planned["paths"])
        self.apply(planned)
        agents = self.local.read("AGENTS.md")
        self.assertIn(self.original_agents, agents)
        self.assertIn(adoption.BLOCK_START.encode(), agents)
        self.assertIn(b"python tools/eutonos_project.py workflow orient --root ROOT", agents)
        self.assertIn(b"Omit\n`--request` for cold entry with a 32,768-byte output budget", agents)
        self.assertIn(b"selected onboarding and exact native owners", agents)
        self.assertIn(b"not assumed consumer paths", agents)
        self.assertIn(
            b"Supplied caller\nidentity remains unverified; prior receipt pins are untrusted comparison input.\n"
            b"Returned source pins describe observed inputs and validated exact RAM reads, not\n"
            b"caller ownership or authority.",
            agents,
        )
        self.assertEqual(agents.count(cp.START), 1)
        # The actual managed entry fails fast if its intentionally selected
        # runtime is absent. Its readable instructions remain usable in both
        # harnesses; no packet or hook is required to read them.
        wrapper = self.root / "tools/eutonos_project.py"
        with mock.patch.dict(os.environ, {"EUTONOS_PYTHON": str(self.home / "missing-python")}), \
                mock.patch.object(sys, "argv", [str(wrapper), "workflow", "orient"]), \
                mock.patch("subprocess.call") as launch, self.assertRaises(SystemExit) as stopped:
            runpy.run_path(str(wrapper), run_name="__main__")
        self.assertIn("runtime missing", str(stopped.exception))
        launch.assert_not_called()
        self.assertEqual(self.local.read("AGENTS.md"), agents)
        self.assertEqual(self.local.read("CLAUDE.md"), b"# Repository instructions\n\n@AGENTS.md\n")
        catalog = ts.loads(self.local.read("eutonos.read.json").decode())
        self.assertEqual(catalog["owners"]["current_task"]["resource_id"], "project.doc.work")
        verified = adoption.verify_profile(self.local, profile)
        self.assertEqual(verified["outcome"], "verified")
        self.assertEqual(verified["selected_modules"], ["delegation", "docs"])

    def test_claude_adapter_preserves_existing_instructions_and_requires_import(self):
        path = self.root / "CLAUDE.md"
        original = b"# Native Claude rules\nKeep my workflow.\n"
        path.write_bytes(original)
        plan = adoption.plan_profile(self.local, self.profile())
        self.assertEqual(plan["outcome"], "conflict")
        self.assertEqual(path.read_bytes(), original)
        path.write_bytes(original + b"\n@AGENTS.md\n")
        plan = adoption.plan_profile(self.local, self.profile())
        self.apply(plan)
        self.assertEqual(path.read_bytes(), original + b"\n@AGENTS.md\n")
        self.assertIn(b"For optional orientation", self.local.read("AGENTS.md"))
        self.assertIn(b"do not require a packet or a setup loop", self.local.read("AGENTS.md"))
        self.assertNotIn(b"workflow orient", path.read_bytes())  # Shared owner, no copied block.

    def test_nested_precedence_guidance_blocks_adoption_without_overwrite(self):
        (self.root / "sub").mkdir()
        path = self.root / "sub/AGENTS.override.md"
        path.write_text("Never merge.\n")
        plan = adoption.plan_profile(self.local, self.profile())
        self.assertEqual(plan["outcome"], "conflict")
        self.assertTrue(any(item["code"] == "instruction-precedence-review" for item in plan["conflicts"]))
        self.assertEqual(path.read_text(), "Never merge.\n")

    def test_repeat_is_noop_and_modified_managed_output_is_per_path_conflict(self):
        profile = self.profile(("docs", "dashboard"))
        first = adoption.plan_profile(self.local, profile)
        self.apply(first)
        repeated = adoption.plan_profile(self.local, profile)
        self.assertEqual(repeated["effect"]["changes"], [])
        wrapper = self.root / "tools/eutonos_project.py"
        wrapper.write_text(wrapper.read_text(encoding="utf-8") + "# local edit\n", encoding="utf-8")
        conflict = adoption.plan_profile(self.local, profile)
        self.assertEqual(conflict["outcome"], "conflict")
        self.assertIn({"path": "tools/eutonos_project.py", "code": "locally-modified",
                       "message": "Previously managed output differs from its adoption manifest."}, conflict["conflicts"])

    def test_existing_nav_config_and_map_ids_are_preserved(self):
        nav_config = {"map_root": "ATLAS", "source_roots": ["src"], "excluded_roots": ["vendor"],
                      "context_paths": ["docs/WORK.md"]}
        (self.root / "src").mkdir(); (self.root / "ATLAS").mkdir()
        config = {"profile": nav.CONFIG, "repository_id": "project.repo", "scope_id": "project.local",
                  "namespace": "project.native", **nav_config}
        (self.root / "eutonos.nav.json").write_bytes(ts.encoded(config))
        native = b"### `SYS-existing` Existing stable identity\n"
        (self.root / "ATLAS/NAVIGATION.md").write_bytes(native)
        planned = adoption.plan_profile(self.local, self.profile(("nav",), nav_config))
        self.assertNotIn("eutonos.nav.json", planned["paths"])
        self.assertFalse(any(path.startswith("ATLAS/") for path in planned["paths"]))
        self.apply(planned)
        self.assertEqual((self.root / "ATLAS/NAVIGATION.md").read_bytes(), native)

    def test_unselected_modules_are_not_generated(self):
        planned = adoption.plan_profile(self.local, self.profile(("delegation",)))
        self.assertEqual(set(planned["paths"]), {"AGENTS.md", "CLAUDE.md", adoption.MANIFEST_PATH, "tools/eutonos_project.py"})
        wrapper = next(change for change in planned["effect"]["changes"]
                       if change["path"] == "tools/eutonos_project.py")
        import base64
        wrapper_body = base64.b64decode(wrapper["after"])
        self.assertIn(b"'settings': 'settings_cli'", wrapper_body)
        self.assertNotIn(b"'ram':", wrapper_body)
        self.assertNotIn(b"'nav':", wrapper_body)
        self.assertNotIn(b"'dashboard':", wrapper_body)
        self.assertNotIn(b"'continuity':", wrapper_body)
        self.assertNotIn("eutonos.read.json", planned["paths"])
        self.assertNotIn("eutonos.ram.json", planned["paths"])
        self.assertNotIn("eutonos.nav.json", planned["paths"])

    def test_delegation_only_accepts_legacy_owner_and_modules_can_be_added(self):
        legacy = b"# Legacy native owner\nExisting task syntax remains authoritative.\n"
        (self.root / "docs/WORK.md").write_bytes(legacy)
        delegated = adoption.plan_profile(self.local, self.profile(("delegation",)))
        self.assertEqual(delegated["profile"], "eutonos.tokenslang.setup-plan.v1")
        self.apply(delegated)
        (self.root / "docs/WORK.md").write_bytes(
            b"# Native work\n\n```tokenslang-work\n" + ts.encoded({
                "profile": ts.WORK, "work_id": "project-work", "goal": "Adopt RAM",
                "status": "selected", "required": [], "optional": []}) + b"\n```\n")
        expanded = adoption.plan_profile(self.local, self.profile(("delegation", "docs", "ram")))
        self.assertEqual(expanded["profile"], "eutonos.tokenslang.setup-plan.v1")
        self.assertIn("eutonos.ram.json", expanded["paths"])
        self.apply(expanded)
        self.assertEqual(adoption.verify_profile(self.local, self.profile(("delegation", "docs", "ram")))["outcome"],
                         "verified")

    def test_reviewed_owner_is_an_apply_precondition(self):
        planned = adoption.plan_profile(self.local, self.profile(("docs",)))
        (self.root / "docs/ARCHITECTURE.md").write_text("Changed after review.\n", encoding="utf-8")
        with self.assertRaises(ts.ReadError) as caught:
            self.apply(planned)
        self.assertEqual(caught.exception.code, "stale")
        self.assertIsNone(self.local.read("eutonos.read.json"))

    def test_unknown_existing_config_and_legacy_work_need_explicit_adaptation(self):
        unknown = b'{"profile":"other.ram"}\n'
        (self.root / "eutonos.ram.json").write_bytes(unknown)
        conflict = adoption.plan_profile(self.local, self.profile(("ram",)))
        self.assertEqual(conflict["outcome"], "conflict")
        self.assertEqual((self.root / "eutonos.ram.json").read_bytes(), unknown)
        (self.root / "eutonos.ram.json").unlink()
        initial = adoption.plan_profile(self.local, self.profile(("docs",)))
        self.apply(initial)
        (self.root / "docs/WORK.md").write_text("# Legacy work\nNo TokenSlang owner block.\n", encoding="utf-8")
        discovered = adoption.discover(self.local)
        owner = next(item for item in discovered["owner_candidates"] if item["path"] == "docs/WORK.md")
        self.assertEqual(owner["status"], "needs-explicit-adaptation")
        conflict = adoption.plan_profile(self.local, self.profile(("docs",)))
        self.assertEqual(conflict["outcome"], "conflict")
        self.assertTrue(any(item["path"] == "docs/WORK.md" for item in conflict["conflicts"]))

    def assert_one_completion(self):
        raw = self.local.read("AGENTS.md")
        self.assertEqual(raw.count(cp.START), 1)
        self.assertEqual(raw.count(cp.CONTRACT.encode()), 1)
        self.assertEqual(cp.merge_block(raw), raw)
        self.assertTrue(raw.startswith(self.original_agents))

    def legacy_adoption(self):
        """Apply the exact shipped legacy renderer through the real guarded seam."""
        legacy = LEGACY_ADOPTION_BLOCK
        self.assertEqual(cp.sha(legacy.rstrip("\n").encode()), cp.LEGACY_ADOPTION_SHA256)
        with mock.patch.object(adoption, "_delegation_block", return_value=legacy), mock.patch.object(
                adoption, "merge_block", side_effect=lambda raw: raw), mock.patch.object(
                adoption, "_source_fingerprint", return_value=
                "227889622c89253196fc36fc1531a3f4969aaeaf520c00aa36ec3282c0c4064d"):
            plan = adoption.plan_profile(self.local, self.profile(("delegation",)))
            self.apply(plan)
        return self.profile(("delegation",))

    def test_global_only_install_leaves_legacy_repository_pair_untouched(self):
        self.legacy_adoption()
        paths = ("AGENTS.md", adoption.MANIFEST_PATH)
        before = [self.local.read(path) for path in paths]
        result = cp.install(cp.instruction_plan(global_instructions=True,
                             codex_home=self.home / "codex", claude_home=self.home / "claude"))
        self.assertEqual(result["outcome"], "installed", result)
        self.assertEqual([self.local.read(path) for path in paths], before)
        self.assertFalse(any(Path(path).is_relative_to(self.root) for path in result["written"]))

    def test_completion_policy_before_adoption_has_one_contract_and_preserves_import(self):
        claude = b"# Custom Claude\n@AGENTS.md\nKeep this tail.\n"
        (self.root / "CLAUDE.md").write_bytes(claude)
        self.assertEqual(cp.install(cp.instruction_plan(root=self.root))["outcome"], "installed")
        self.apply(adoption.plan_profile(self.local, self.profile()))
        self.assert_one_completion()
        self.assertEqual(self.local.read("CLAUDE.md"), claude)
        self.assertEqual(adoption.plan_profile(self.local, self.profile())["effect"]["changes"], [])
        self.assertEqual(adoption.verify_profile(self.local, self.profile())["outcome"], "verified")

    def test_adoption_before_completion_policy_has_one_contract_and_repeats_are_noop(self):
        self.apply(adoption.plan_profile(self.local, self.profile()))
        self.assert_one_completion()
        self.assertEqual(cp.install(cp.instruction_plan(root=self.root))["written"], [])
        self.assertEqual(adoption.plan_profile(self.local, self.profile())["effect"]["changes"], [])
        raw = self.local.read("AGENTS.md")
        self.assertIn(rule_index.PROFILE.encode(), raw)
        self.assertIn(b"native `docs/WORK.md` (`project.doc.work`)", raw)
        self.assertIn(b"upstream reference `docs/WORK_MODEL.md`", raw)

    def test_legacy_adoption_upgrades_before_completion_policy(self):
        profile = self.legacy_adoption()
        # Reproduce the shipped duplicate shape without asking the new installer
        # to create it. Only the outer adoption block is adoption-fingerprinted.
        (self.root / "AGENTS.md").write_bytes(self.local.read("AGENTS.md") + b"\n" + cp.block() + b"\n")
        self.apply(adoption.plan_profile(self.local, profile))
        self.assert_one_completion()
        self.assertEqual(cp.install(cp.instruction_plan(root=self.root))["written"], [])
        self.assertEqual(adoption.verify_profile(self.local, profile)["outcome"], "verified")

    def test_completion_policy_migrates_proved_legacy_pair_then_adoption_upgrades(self):
        profile = self.legacy_adoption()
        original = ts.loads(self.local.read(adoption.MANIFEST_PATH).decode())
        result = cp.install(cp.instruction_plan(root=self.root))
        self.assertEqual(result["outcome"], "installed", result)
        self.assert_one_completion()
        migrated = ts.loads(self.local.read(adoption.MANIFEST_PATH).decode())
        expected = dict(original)
        expected["managed"] = dict(original["managed"])
        expected["managed"]["AGENTS.md"] = migrated["managed"]["AGENTS.md"]
        self.assertEqual(migrated, expected)
        self.assertEqual(cp.install(cp.instruction_plan(root=self.root))["written"], [])
        with self.assertRaises(ts.ReadError) as caught:
            adoption.verify_profile(self.local, profile)
        self.assertEqual(caught.exception.code, "stale")
        self.apply(adoption.plan_profile(self.local, profile))
        self.assert_one_completion()
        self.assertEqual(adoption.verify_profile(self.local, profile)["outcome"], "verified")

    def test_legacy_migration_refuses_unknown_customized_or_invalid_manifests(self):
        self.legacy_adoption()
        agents = self.local.read("AGENTS.md")
        manifest = self.local.read(adoption.MANIFEST_PATH)
        for kind in ("unknown", "customized", "invalid", "fingerprint", "malformed"):
            with self.subTest(kind=kind):
                (self.root / "AGENTS.md").write_bytes(agents)
                (self.root / adoption.MANIFEST_PATH).write_bytes(manifest)
                if kind == "unknown":
                    (self.root / adoption.MANIFEST_PATH).unlink()
                elif kind == "customized":
                    (self.root / "AGENTS.md").write_bytes(agents.replace(b"primary balances", b"primary customizes"))
                elif kind == "invalid":
                    (self.root / adoption.MANIFEST_PATH).write_bytes(b'{}')
                elif kind == "fingerprint":
                    value = ts.loads(manifest.decode())
                    value["managed"]["AGENTS.md"]["sha256"] = "0" * 64
                    (self.root / adoption.MANIFEST_PATH).write_bytes(ts.encoded(value))
                else:
                    (self.root / "AGENTS.md").write_bytes(agents.replace(adoption.BLOCK_END.encode(), b""))
                snapshots = [self.local.read(path) for path in ("AGENTS.md", adoption.MANIFEST_PATH)]
                result = cp.install(cp.instruction_plan(root=self.root))
                self.assertEqual(result["outcome"], "conflict", result)
                self.assertEqual(result["written"], [])
                self.assertEqual(snapshots, [self.local.read(path) for path in ("AGENTS.md", adoption.MANIFEST_PATH)])

    def test_legacy_pair_changed_after_inspection_refuses_all_writes(self):
        self.legacy_adoption()
        plan = cp.instruction_plan(root=self.root)
        raw = self.local.read(adoption.MANIFEST_PATH) + b"\n"
        (self.root / adoption.MANIFEST_PATH).write_bytes(raw)
        original = self.local.read("AGENTS.md")
        result = cp.install(plan)
        self.assertEqual(result["outcome"], "attention-required", result)
        self.assertEqual(result["written"], [])
        self.assertEqual(self.local.read("AGENTS.md"), original)
        self.assertEqual(self.local.read(adoption.MANIFEST_PATH), raw)

    def test_partial_legacy_install_reports_written_file_and_refuses_replay(self):
        self.legacy_adoption()
        before = self.local.read(adoption.MANIFEST_PATH)
        replace = cp.os.replace
        def fail_manifest(source, destination):
            if Path(destination).name == adoption.MANIFEST_PATH:
                raise OSError("fixture manifest replacement failed")
            return replace(source, destination)
        with mock.patch.object(cp.os, "replace", side_effect=fail_manifest):
            result = cp.install(cp.instruction_plan(root=self.root))
        self.assertEqual(result["outcome"], "attention-required", result)
        self.assertEqual(result["written"], [str(self.root / "AGENTS.md")])
        self.assertEqual(self.local.read(adoption.MANIFEST_PATH), before)
        self.assert_one_completion()
        replay = cp.install(cp.instruction_plan(root=self.root))
        self.assertEqual(replay["outcome"], "conflict", replay)
        self.assertEqual(replay["written"], [])

    def test_legacy_first_file_readback_failure_reports_applied_unverified_effect(self):
        self.legacy_adoption()
        plan = cp.instruction_plan(root=self.root)
        before = self.local.read(adoption.MANIFEST_PATH)
        replaced = False
        replace, read = cp.os.replace, cp.read
        def observed_replace(source, destination):
            nonlocal replaced
            replace(source, destination)
            if Path(destination).name == "AGENTS.md":
                replaced = True
        def failed_readback(path):
            if replaced and Path(path).name == "AGENTS.md":
                raise OSError("fixture readback unavailable")
            return read(path)
        with mock.patch.object(cp.os, "replace", side_effect=observed_replace), mock.patch.object(
                cp, "read", side_effect=failed_readback):
            result = cp.install(plan)
        self.assertEqual(result["outcome"], "attention-required", result)
        self.assertEqual(result["written"], [])
        self.assertEqual(result["applied_but_unverified"], [str(self.root / "AGENTS.md")])
        self.assertIn("Inspect applied files", result["next_action"])
        self.assertEqual(self.local.read(adoption.MANIFEST_PATH), before)
        self.assert_one_completion()
        self.assertEqual(cp.install(cp.instruction_plan(root=self.root))["outcome"], "conflict")

    def test_completion_customization_and_malformed_delegation_refuse_adoption(self):
        self.apply(adoption.plan_profile(self.local, self.profile()))
        original = self.local.read("AGENTS.md")
        for raw in (original.replace(b"Requests", b"My requests"),
                    original.replace(adoption.BLOCK_END.encode(), b"")):
            (self.root / "AGENTS.md").write_bytes(raw)
            result = adoption.plan_profile(self.local, self.profile())
            self.assertEqual(result["outcome"], "conflict", result)
            self.assertEqual(self.local.read("AGENTS.md"), raw)


    # ---------------------------------------------------------------- IPC5 lane D
    def test_ipc5_f6_wrapper_calls_the_installed_launcher_under_the_eutonos_home(self):
        self.apply(adoption.plan_profile(self.local, self.profile(("docs", "delegation"))))
        wrapper = self.root / "tools/eutonos_project.py"
        text = wrapper.read_text(encoding="utf-8")
        self.assertNotIn('"runtime"', text)
        self.assertIn("EUTONOS_HOME", text)
        eutonos_home = self.home / "an-eutonos-home"
        (eutonos_home / "bin").mkdir(parents=True)
        suffix = ".exe" if os.name == "nt" else ""
        launcher = eutonos_home / "bin" / ("eutonos-setup" + suffix)
        environment = {key: value for key, value in os.environ.items() if key != "EUTONOS_PYTHON"}
        environment["EUTONOS_HOME"] = str(eutonos_home)

        def run(*argv):
            with mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(sys, "argv", [str(wrapper), *argv]), \
                    mock.patch("subprocess.call", return_value=0) as launch:
                try:
                    runpy.run_path(str(wrapper), run_name="__main__")
                except SystemExit as stopped:
                    return launch, stopped.code
            return launch, None

        launch, code = run("setup", "verify")
        self.assertIn("runtime missing", str(code))  # no launcher yet
        launch.assert_not_called()
        launcher.write_bytes(b"")
        launch, code = run("setup", "verify")
        self.assertEqual(code, 0)
        launch.assert_called_once_with([str(launcher), "verify", "--root", str(self.root)])
        environment["EUTONOS_HOME"] = "relative-home"
        launch, code = run("setup", "verify")
        self.assertIn("absolute", str(code))
        launch.assert_not_called()

    def test_ipc5_f6_every_wrapper_launcher_is_a_shipped_entry_point(self):
        import tomllib
        pyproject = tomllib.loads((Path(adoption.__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8"))
        scripts = pyproject["project"]["scripts"]
        for implementation, name in adoption.LAUNCHERS.items():
            self.assertIn(name, scripts, name)
            self.assertEqual(scripts[name].split(":")[0], "eutonos." + implementation)
        namespace = {}
        for line in adoption._wrapper(("docs", "delegation", "dashboard", "ram", "nav")).splitlines():
            if line.startswith(("modules = ", "launchers = ")):
                exec(line, namespace)
        self.assertEqual(set(namespace["modules"].values()), set(namespace["launchers"]))
        self.assertTrue(set(namespace["launchers"]) <= set(adoption.LAUNCHERS))

    def adopted(self, modules=("docs", "delegation", "dashboard")):
        profile = self.profile(modules)
        self.apply(adoption.plan_profile(self.local, profile))
        return profile

    def snapshot_files(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file() and ".git" not in path.parts}

    def test_ipc5_removal_template_keeps_native_owners_and_configs_by_default(self):
        self.adopted()
        template = adoption.preservation_template(self.local)
        self.assertEqual(template["profile"], adoption.PRESERVATION)
        paths = template["paths"]
        self.assertNotIn("docs/WORK.md", paths)  # a native owner is not a managed output
        self.assertEqual(paths["eutonos.read.json"], "keep")
        for managed in ("tools/eutonos_project.py", "AGENTS.md", "CLAUDE.md", adoption.MANIFEST_PATH):
            self.assertEqual(paths[managed], "remove", managed)

    def test_ipc5_removal_plan_is_read_only_and_must_cover_every_manifest_path(self):
        self.adopted()
        before = self.snapshot_files()
        template = adoption.preservation_template(self.local)
        report = adoption.plan_removal(self.local, template)
        self.assertEqual(report["outcome"], "removal-plan")
        self.assertEqual({item["path"]: item["result"] for item in report["paths"]}["tools/eutonos_project.py"], "remove")
        self.assertEqual(self.snapshot_files(), before)
        for mutate, code in ((lambda p: p["paths"].pop("CLAUDE.md"), "conflict"),
                             (lambda p: p["paths"].update({"stray.md": "remove"}), "conflict"),
                             (lambda p: p["paths"].update({"CLAUDE.md": "maybe"}), "invalid"),
                             (lambda p: p["paths"].update({"eutonos.read.json": "remove"}), "denied")):
            broken = {**template, "paths": dict(template["paths"])}
            mutate(broken)
            with self.assertRaises(ts.ReadError) as caught:
                adoption.plan_removal(self.local, broken)
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.snapshot_files(), before)

    def test_ipc5_removal_removes_only_remove_paths_and_backs_them_up(self):
        self.adopted()
        before = self.snapshot_files()
        template = adoption.preservation_template(self.local)
        backup = self.home / "backups"
        result = adoption.apply_removal(self.local, template, backup_dir=backup)
        self.assertEqual(result["outcome"], "de-adopted")
        after = self.snapshot_files()
        self.assertNotIn("tools/eutonos_project.py", after)
        self.assertNotIn("CLAUDE.md", after)
        self.assertNotIn(adoption.MANIFEST_PATH, after)
        self.assertEqual(after["eutonos.read.json"], before["eutonos.read.json"])  # protected: kept by the default plan
        self.assertEqual(after["docs/WORK.md"], before["docs/WORK.md"])
        self.assertNotIn(adoption.BLOCK_START.encode(), after["AGENTS.md"])
        self.assertTrue(after["AGENTS.md"].startswith(self.original_agents.rstrip(b"\n")))
        self.assertEqual((backup / "tools/eutonos_project.py").read_bytes(), before["tools/eutonos_project.py"])
        self.assertEqual((backup / "AGENTS.md").read_bytes(), before["AGENTS.md"])
        with self.assertRaises(ts.ReadError):  # nothing is managed any more
            adoption.plan_removal(self.local, template)

    def test_ipc5_removal_keeps_a_changed_file_and_the_manifest_that_records_it(self):
        self.adopted()
        wrapper = self.root / "tools/eutonos_project.py"
        wrapper.write_text(wrapper.read_text(encoding="utf-8") + "# local edit\n", encoding="utf-8")
        edited = wrapper.read_bytes()
        template = adoption.preservation_template(self.local)
        result = adoption.apply_removal(self.local, template, backup_dir=self.home / "backups")
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(wrapper.read_bytes(), edited)
        self.assertIn({"path": "tools/eutonos_project.py", "reason": "differs from its adoption manifest; reported and kept"},
                      result["kept"])
        self.assertIsNotNone(self.local.read(adoption.MANIFEST_PATH))  # still there, so a re-run can finish
        self.assertFalse((self.root / "CLAUDE.md").exists())  # the unchanged ones were still removed

    def test_ipc5_removal_discards_a_native_config_only_with_explicit_consent(self):
        self.adopted()
        architecture = self.local.read("docs/ARCHITECTURE.md")
        template = adoption.preservation_template(self.local)
        template["paths"]["eutonos.read.json"] = {"action": "remove", "discard_native": True}
        result = adoption.apply_removal(self.local, template, backup_dir=self.home / "backups")
        self.assertEqual(result["outcome"], "de-adopted")
        self.assertFalse((self.root / "eutonos.read.json").exists())
        self.assertTrue((self.home / "backups" / "eutonos.read.json").is_file())
        self.assertEqual(self.local.read("docs/ARCHITECTURE.md"), architecture)

    def test_ipc5_removal_without_a_manifest_refuses(self):
        with self.assertRaises(ts.ReadError) as caught:
            adoption.preservation_template(self.local)
        self.assertEqual(caught.exception.code, "missing")


if __name__ == "__main__":
    unittest.main()
