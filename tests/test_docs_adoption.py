"""EHF3 native/blank/legacy preservation through the real guarded boundary."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from eutonos import GitView, Hub, ReadError, tokenslang as ts
from eutonos.docs import PROFILE, apply_adoption, plan_adoption
from eutonos.effects import Coordinator, FixtureAuthority, LocalRoot, MARKER, MARKER_BYTES, digest


class DocsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eutonos-docs-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        environment = patch.dict(os.environ, {"EUTONOS_HOME": str(self.home / "eutonos-home")})
        environment.start()
        self.addCleanup(environment.stop)
        self.root = self.home / "native fixture"
        self.root.mkdir(); (self.root / "Docs").mkdir()
        (self.root / MARKER).write_bytes(MARKER_BYTES)
        (self.root / "AGENTS.md").write_bytes(b"# Native agent rules\r\n\r\nNever deploy without owner permission.\r\n")
        (self.root / "Docs/VISION.md").write_bytes(b"# Native project\n\nDocument ID: `native.vision`.\n\nPreserve my architecture and roadmap.\n")
        (self.root / "Docs/RULES.md").write_bytes(b"# Native verification\n\nDocument ID: `native.rules`.\n\nInspect the changed behavior and preserve unique rules.\n")
        work = {"profile": ts.WORK, "work_id": "NATIVE-1", "goal": "Build the next native feature",
                "status": "selected", "required": [self.ref("native.vision"), self.ref("native.rules")], "optional": []}
        (self.root / "Docs/WORK.md").write_bytes(("# Native work\n\nDocument ID: `native.work`.\n\n```tokenslang-work\n" + json.dumps(work) + "\n```\n").encode())
        self.local = LocalRoot(self.root)
        self.spec = {"profile": PROFILE, "repository_id": "fixture.repo", "scope_id": "fixture.scope",
                     "namespace": "fixture.native", "setup_owner": "AGENTS.md", "catalog_path": "eutonos.read.json",
                     "manifest_path": "eutonos.docs.json", "owners": {
                         "purpose": {"path": "Docs/VISION.md", "resource_id": "native.vision"},
                         "architecture": {"path": "Docs/VISION.md", "resource_id": "native.vision"},
                         "health": {"path": "Docs/RULES.md", "resource_id": "native.rules"},
                         "current_task": {"path": "Docs/WORK.md", "resource_id": "native.work"}}}

    def ref(self, name):
        return {"scope_id": "fixture.scope", "namespace": "fixture.native", "resource_id": name}

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def reject(self, code, function, *args):
        with self.assertRaises(ReadError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)

    def prepare_host(self, adoption):
        self.coordinator = Coordinator.initialize(self.home / "coordinator.sqlite")
        self.auth = FixtureAuthority(self.local, "fixture-builder", frozenset(c["path"] for c in adoption["effect"]["changes"]), time.time()+300)
        self.coordinator.start(self.auth, "docs-install")
        self.coordinator.reserve(self.auth, "docs-install", 1, self.auth.paths)

    def apply(self, adoption, effect_id="docs-apply"):
        return apply_adoption(self.coordinator, self.auth, "docs-install", 1, effect_id, adoption, adoption["fingerprint"])

    def commit(self):
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        def git(*args):
            return subprocess.check_output(["git", "-c", "safe.directory=" + str(self.root), "-c", "core.autocrlf=false",
                "-c", "user.name=EUTONOS Fixture", "-c", "user.email=fixture@example.invalid", "-C", str(self.root), *args], env=env, stderr=subprocess.PIPE)
        git("init", "-q", "-b", "main"); git("add", "--all"); git("commit", "-qm", "Synthetic adoption")
        return git("rev-parse", "HEAD").decode().strip()

    def test_native_plan_zero_write_apply_repeat_noop_and_hub_pickup(self):
        before = self.snapshot()
        adoption = plan_adoption(self.local, self.spec)
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.home / "coordinator.sqlite").exists())
        self.assertEqual({c["path"] for c in adoption["effect"]["changes"]}, {"AGENTS.md", "eutonos.read.json", "eutonos.docs.json"})
        self.prepare_host(adoption)
        self.assertEqual(self.apply(adoption)["outcome"], "completed")
        for name, body in before.items():
            if name != "AGENTS.md":
                self.assertEqual((self.root / name).read_bytes(), body)
        self.assertTrue((self.root / "AGENTS.md").read_bytes().startswith(before["AGENTS.md"]))
        repeated = plan_adoption(self.local, self.spec)
        self.assertEqual(repeated["effect"]["changes"], [])
        coordinator_bytes = self.coordinator.path.read_bytes()
        self.assertEqual(self.apply(repeated, "repeat-noop")["outcome"], "no-op")
        self.assertEqual(self.coordinator.path.read_bytes(), coordinator_bytes)
        revision = self.commit()
        result = Hub(GitView(self.root, revision)).handle({"profile": ts.PROFILE, "request_id": "cold-pickup",
            "operation": "orient", "max_bytes": 16384, "purpose": "Locate native work after adoption"})
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["data"]["work"]["work_id"], "NATIVE-1")
        self.assertEqual(result["data"]["required"][0]["ref"]["resource_id"], "native.vision")

    def test_stale_owner_or_plan_fingerprint_cannot_write_anything(self):
        adoption = plan_adoption(self.local, self.spec)
        self.prepare_host(adoption)
        self.reject("conflict", apply_adoption, self.coordinator, self.auth, "docs-install", 1, "docs-apply", adoption, "wrong")
        (self.root / "Docs/RULES.md").write_bytes(b"Stronger new constraint")
        before, db = self.snapshot(), self.coordinator.path.read_bytes()
        self.reject("stale", self.apply, adoption)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(db, self.coordinator.path.read_bytes())

    def test_rollback_removes_only_installation_bytes_and_preserves_later_owner_edit(self):
        adoption = plan_adoption(self.local, self.spec)
        self.prepare_host(adoption); self.apply(adoption)
        (self.root / "AGENTS.md").write_bytes((self.root / "AGENTS.md").read_bytes()+b"\nLater owner rule.\n")
        result = self.coordinator.rollback(self.auth, "docs-install", 1, "docs-apply")
        self.assertEqual(result["preserved"], ["AGENTS.md"])
        self.assertIn(b"Later owner rule.", (self.root / "AGENTS.md").read_bytes())
        self.assertFalse((self.root / "eutonos.docs.json").exists())
        self.assertFalse((self.root / "eutonos.read.json").exists())
        self.assertIn(b"native.vision", (self.root / "Docs/VISION.md").read_bytes())

    def test_existing_ids_and_one_owner_per_identity_are_preserved(self):
        bad = copy.deepcopy(self.spec)
        bad["owners"]["purpose"]["resource_id"] = "replacement-id"
        self.reject("conflict", plan_adoption, self.local, bad)
        bad = copy.deepcopy(self.spec)
        bad["owners"]["architecture"] = {"path": "Docs/RULES.md", "resource_id": "native.vision"}
        self.reject("conflict", plan_adoption, self.local, bad)
        bad = copy.deepcopy(self.spec); bad["owners"]["health"]["path"] = "missing.md"
        self.reject("missing", plan_adoption, self.local, bad)

    def test_existing_setup_owner_is_not_duplicated(self):
        (self.root / "AGENTS.md").write_bytes(b"# Rules\n\n## Setup status\n\nExisting custom checklist.\n")
        self.reject("unsupported", plan_adoption, self.local, self.spec)

    def test_legacy_installation_and_nested_native_roots_remain_intact(self):
        (self.root / ".tova").mkdir()
        legacy = b'{"schema_version":1,"native_id":"keep-me","custom":"unique"}\n'
        (self.root / ".tova/TOVA_INSTALLATION.json").write_bytes(legacy)
        child = self.root / "child"; child.mkdir(); (child / ".project").mkdir()
        (child / ".project/TOVA_INSTALLATION.json").write_bytes(b"{}")
        (child / "WORK.md").write_bytes(b"child work")
        adoption = plan_adoption(self.local, self.spec)
        self.assertEqual(adoption["classification"], "legacy-evidence-preserved")
        self.prepare_host(adoption); self.apply(adoption)
        self.assertEqual((self.root / ".tova/TOVA_INSTALLATION.json").read_bytes(), legacy)
        self.assertEqual((child / "WORK.md").read_bytes(), b"child work")
        self.reject("denied", self.local.read, "child/WORK.md")

    def test_blank_seed_groups_known_roles_without_empty_template_tree(self):
        blank = self.home / "blank"; blank.mkdir(); (blank / MARKER).write_bytes(MARKER_BYTES)
        self.root, self.local = blank, LocalRoot(blank)
        spec = copy.deepcopy(self.spec)
        spec["owners"] = {role: {"path": "PROJECT.md", "resource_id": "native.overview"} for role in spec["owners"]}
        spec["seed"] = {"path": "PROJECT.md", "resource_id": "native.overview", "purpose": "Make a useful small tool",
                        "rules": "Inspect changed behavior; no deployment is authorized.", "work": {
                            "profile": ts.WORK, "work_id": "BLANK-1", "goal": "Create the first design sketch", "status": "selected",
                            "required": [self.ref("native.overview")], "optional": []}}
        adoption = plan_adoption(self.local, spec)
        self.prepare_host(adoption); self.apply(adoption)
        self.assertEqual(set(self.snapshot()), {MARKER, "PROJECT.md", "AGENTS.md", "eutonos.read.json", "eutonos.docs.json"})
        self.assertEqual(plan_adoption(self.local, spec)["effect"]["changes"], [])
        (blank / "PROJECT.md").write_bytes((blank / "PROJECT.md").read_bytes()+b"\nA real later lesson.\n")
        self.assertEqual(plan_adoption(self.local, spec)["effect"]["changes"], [])

    def test_native_freeform_work_stays_readable_with_explicit_parser_limit(self):
        (self.root / "Docs/WORK.md").write_bytes(b"# Work\n\nDocument ID: `native.work`.\n\nUse our native work format.\n")
        before = (self.root / "Docs/WORK.md").read_bytes()
        adoption = plan_adoption(self.local, self.spec)
        self.assertEqual(adoption["orientation"], "native-source-only")
        self.prepare_host(adoption); self.apply(adoption)
        self.assertEqual((self.root / "Docs/WORK.md").read_bytes(), before)

    def test_cli_plan_is_the_same_zero_write_library_path(self):
        (self.root / "adoption.json").write_bytes(ts.encoded(self.spec))
        before = self.snapshot()
        process = subprocess.run([sys.executable, "-B", "-m", "eutonos.docs_cli", "--root", str(self.root), "--spec", "adoption.json"], capture_output=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result["writes"], 0)
        library = plan_adoption(self.local, self.spec)["effect"]
        library["observed"]["adoption.json"] = digest(ts.encoded(self.spec))
        self.assertEqual(result["adoption"]["effect"], library)
        self.assertEqual(before, self.snapshot())

    def test_excluded_native_owner_and_unknown_setup_semantics_are_refused(self):
        self.reject("denied", plan_adoption, LocalRoot(self.root, ("Docs",)), self.spec)
        adoption = plan_adoption(self.local, self.spec)
        self.prepare_host(adoption); self.apply(adoption)
        path = self.root / "AGENTS.md"
        body = path.read_bytes().replace(b'"continuation":"paused"', b'"continuation":[]')
        path.write_bytes(body)
        self.reject("invalid", plan_adoption, self.local, self.spec)

    def test_planning_detects_drift_in_previously_read_native_owner(self):
        original = LocalRoot.read
        changed = False
        def drift(root, name):
            nonlocal changed
            body = original(root, name)
            if name == "Docs/VISION.md" and not changed:
                changed = True
                (root.root / name).write_bytes(body+b"\nNew architecture constraint\n")
            return body
        with patch.object(LocalRoot, "read", drift):
            self.reject("stale", plan_adoption, self.local, self.spec)
        self.assertFalse((self.root / "eutonos.read.json").exists())

    def test_existing_catalog_extensions_survive_but_owner_rebinding_is_refused(self):
        adoption = plan_adoption(self.local, self.spec)
        self.prepare_host(adoption); self.apply(adoption)
        path = self.root / "eutonos.read.json"
        catalog = json.loads(path.read_bytes()); catalog["extensions"] = {"native": {"keep": True}}
        path.write_bytes(ts.encoded(catalog))
        self.assertEqual(plan_adoption(self.local, self.spec)["effect"]["changes"], [])
        catalog["owners"]["current_task"] = self.ref("different-work")
        path.write_bytes(ts.encoded(catalog))
        self.reject("conflict", plan_adoption, self.local, self.spec)


    # ---------------------------------------------------------------- IPC5 lane D: Setup status beyond Docs
    LEGACY_STATUS = (
        "# Rules\n\n<!-- EUTONOS setup: begin -->\n## EUTONOS setup status\n\n"
        "Docs configuration is readable through `eutonos.read.json`. Native owners remain authoritative. "
        "Verify the installation before claiming readiness; interrupted apply may be incomplete. "
        "NAV and RAM are not installed. Setup preferences do not dispatch work or schedule reminders.\n\n"
        "- [purpose, architecture](Docs/VISION.md)\n- [current task](Docs/WORK.md)\n\n"
        "```tokenslang-setup\n" + json.dumps({
            "profile": "eutonos.tokenslang.setup.v1", "selected_modules": ["docs"], "docs": "configured-unverified",
            "manifest": "eutonos.docs.json", "next_module": "nav", "preferences": {
                "continuation": "paused", "reminders": "off", "last_offer_at": None, "next_eligible_at": None,
                "reoffer_used": False}}, sort_keys=True) + "\n```\n<!-- EUTONOS setup: end -->\nTail text.\n")

    def test_ipc5_a_docs_only_block_from_before_still_parses_and_can_grow(self):
        from eutonos import docs
        value, _, _ = docs.setup_record(self.LEGACY_STATUS.encode())
        self.assertEqual((value["selected_modules"], value["docs"], docs.next_module(value)), (["docs"], "configured-unverified", "nav"))
        grown = docs.update_setup(self.LEGACY_STATUS.encode(), selected=["docs", "nav", "ram"],
                                  states={"docs": "ready", "nav": "ready", "ram": "paused"})
        text = grown.decode()
        self.assertNotIn("NAV and RAM are not installed", text)
        for line in ("- Docs: ready", "- NAV: ready", "- RAM: paused"):
            self.assertIn(line, text)
        self.assertIn("2 of 3 selected modules ready; RAM paused", text)
        self.assertIn("Next module: RAM.", text)
        self.assertIn("- [purpose, architecture](Docs/VISION.md)\n- [current task](Docs/WORK.md)", text)  # routes kept
        self.assertTrue(text.startswith("# Rules\n\n") and text.endswith("Tail text.\n"))
        record, _, _ = docs.setup_record(grown)
        self.assertEqual((record["selected_modules"], record["nav"], record["ram"], record["next_module"]),
                         (["docs", "nav", "ram"], "ready", "paused", "ram"))
        # Updating again with nothing new leaves the same bytes.
        self.assertEqual(docs.update_setup(grown), grown)

    def test_ipc5_a_new_block_renders_the_real_state_in_setup_order(self):
        adoption = plan_adoption(self.local, self.spec)
        agents = next(change for change in adoption["effect"]["changes"] if change["path"] == "AGENTS.md")
        import base64
        text = base64.b64decode(agents["after"]).decode("utf-8")
        self.assertNotIn("NAV and RAM are not installed", text)
        self.assertLess(text.index("- Docs: configured-unverified"), text.index("- NAV: not selected"))
        self.assertLess(text.index("- NAV: not selected"), text.index("- RAM: not selected"))
        self.assertIn("Next module: NAV.", text)
        self.assertEqual(adoption["next_module"], "nav")

    def test_ipc5_a_pause_and_a_decline_are_recorded_and_move_the_next_module(self):
        from eutonos import docs
        body = docs.update_setup(self.LEGACY_STATUS.encode(), selected=["docs", "nav", "ram"],
                                 states={"docs": "ready", "nav": "declined"})
        value, _, _ = docs.setup_record(body)
        self.assertEqual((value["nav"], value["ram"], value["next_module"]), ("declined", "not-started", "ram"))
        body = docs.update_setup(body, states={"ram": "ready"})
        value, _, _ = docs.setup_record(body)
        self.assertIsNone(value["next_module"])  # all three are finished: nothing left to offer
        self.assertIn("2 of 3 selected modules ready; NAV declined", docs.progress(value)["text"])
        body = docs.update_setup(body, preferences={"continuation": "continue-requested", "reminders": "sleep"})
        self.assertEqual(docs.setup_record(body)[0]["preferences"]["reminders"], "sleep")

    def test_ipc5_a_setup_update_keeps_crlf_and_refuses_bad_state(self):
        from eutonos import docs
        crlf = self.LEGACY_STATUS.replace("\n", "\r\n").encode()
        grown = docs.update_setup(crlf, selected=["docs", "nav"], states={"nav": "paused"})
        self.assertNotIn(b"\n", grown.replace(b"\r\n", b""))
        self.assertEqual(docs.setup_record(grown)[0]["nav"], "paused")
        for call, code in ((lambda: docs.update_setup(crlf, states={"nav": "ready"}), "invalid"),  # nav not selected
                           (lambda: docs.update_setup(crlf, states={"docs": "done"}), "unsupported"),
                           (lambda: docs.update_setup(crlf, preferences={"nope": 1}), "invalid"),
                           (lambda: docs.update_setup(crlf, selected=["docs"], states={"docs": "ready"}, following="docs"), "invalid")):
            self.reject(code, call)
        record = json.loads(self.LEGACY_STATUS.split("```tokenslang-setup\n")[1].split("\n```")[0])
        for mutate in (lambda v: v.update(selected_modules=["nav"]), lambda v: v.update(selected_modules=["nav", "docs"], nav="ready"),
                       lambda v: v.update(ram="ready"), lambda v: v.update(next_module="bogus")):
            broken = json.loads(json.dumps(record))
            mutate(broken)
            with self.assertRaises(ReadError):
                docs.validate_setup(broken)

    def test_ipc5_a_competing_checklist_is_still_refused(self):
        (self.root / "AGENTS.md").write_bytes(b"# Rules\n\n## Setup status\n\nExisting custom checklist.\n")
        self.reject("unsupported", plan_adoption, self.local, self.spec)


if __name__ == "__main__":
    unittest.main()
