"""The five lifecycle checklist pages ship in the package and must not drift from the code.

Machine pages (install, uninstall): the step IDs, kinds and runners are the ones a real install and a real uninstall
(on temporary folders, with fake OS ports) write into the setup checkpoint. Repository pages: the step IDs are the
completion-check IDs `repository_lifecycle.repository_checks` returns, and the page carries that exact JSON.
"""
import json
from pathlib import Path
import re
import unittest

import eutonos
from eutonos import cli, installer, repository_lifecycle as lifecycle, self_repair, setup_checkpoint as cp

import test_installer as installer_tests

ROOT = Path(__file__).resolve().parents[1]
PAGES = Path(eutonos.__file__).parent / "checklists"
NAMES = ("install", "uninstall", "add-repository", "remove-repository", "repair")
STEP = re.compile(r"^### Step `([^`]+)`[ \t]*$", re.M)
HEADING = re.compile(r"^#{1,3} ", re.M)
TRIAL = "IPC6, IPC8 and IPC10"
# Words a backticked command span starts with when it is one of our commands.
COMMANDS = ("eutonos ", "eutonos-install ", "eutonos-setup ", "eutonos-host ", "eutonos-nav ", "eutonos-ram ",
            "eutonos-settings ", "python -I -m eutonos.repository_lifecycle ", "python -m eutonos.boards_cli ")
FLAG_SOURCES = ("installer.py", "setup_cli.py", "repository_lifecycle.py", "doctor.py", "host_cli.py", "nav_cli.py",
                "ram_cli.py", "boards_cli.py", "settings_cli.py")


def read(name):
    return (PAGES / f"{name}.md").read_text(encoding="utf-8")


def parse(name):
    """{"id", "revision", "steps": [{"id", "kind", "runs", "block"}]} read from the page text."""
    text = read(name)
    header = re.search(r"^Checklist id: `([^`]+)`$", text, re.M)
    revision = re.search(r"^Checklist revision: (\S+)$", text, re.M)
    steps = []
    for match in STEP.finditer(text):
        following = HEADING.search(text, match.end())
        block = text[match.end(): following.start() if following else len(text)]
        kind = re.search(r"^- Kind: (\w+)", block, re.M)
        runs = re.search(r"^- Runs: (\w+)", block, re.M)
        steps.append({"id": match.group(1), "kind": kind and kind.group(1), "runs": runs and runs.group(1),
                      "block": block})
    return {"id": header and header.group(1), "revision": revision and revision.group(1), "steps": steps}


def ids(name):
    return [step["id"] for step in parse(name)["steps"]]


def kinds(name):
    return {step["id"]: (step["kind"], step["runs"]) for step in parse(name)["steps"]}


def checkpoint_kinds(value):
    return {step["id"]: (step["kind"], step["runner"]) for step in value["steps"]}


class PageShapeTests(unittest.TestCase):
    def test_the_five_pages_exist_with_their_id_and_a_revision(self):
        self.assertEqual(sorted(path.name for path in PAGES.glob("*.md")), sorted(f"{name}.md" for name in NAMES))
        for name in NAMES:
            page = parse(name)
            self.assertEqual(page["id"], name)
            self.assertTrue(page["revision"], name)
            self.assertTrue(page["steps"], name)
            self.assertNotIn("@@", read(name))
            self.assertEqual(len(ids(name)), len(set(ids(name))), name)

    def test_every_step_has_a_kind_a_runner_a_command_a_done_condition_and_evidence(self):
        for name in NAMES:
            for step in parse(name)["steps"]:
                where = f"{name}:{step['id']}"
                self.assertIn(step["kind"], cp.KINDS, where)
                self.assertIn(step["runs"], cp.RUNNERS, where)
                for part in (r"^- Command\b", r"^- Done when:", r"^- Evidence:"):
                    self.assertRegex(step["block"], re.compile(part, re.M), where)
                runs_line = re.search(r"^- Runs: .*$", step["block"], re.M).group(0)
                if step["kind"] == "mechanical":
                    self.assertIn(TRIAL, runs_line, where)
                else:
                    self.assertNotIn(TRIAL, runs_line, where)
                if step["kind"] == "person":
                    self.assertEqual(step["runs"], "owner", where)

    def test_each_page_has_resume_and_lock_guidance(self):
        for name in NAMES:
            text = read(name)
            self.assertIn("## Lost context? Resume here", text, name)
            self.assertIn("## Concurrency and locks", text, name)
        for name in ("install", "uninstall"):
            self.assertIn("machine lock", read(name))
        for name in ("add-repository", "remove-repository"):
            self.assertIn("projects.lock", read(name))
        self.assertIn("--keep-projects", read("uninstall"))
        self.assertIn("remove-project", read("uninstall"))
        self.assertIn("--adopt", read("install"))
        self.assertIn("eutonos update", read("install"))
        self.assertIn("eutonos rollback", read("install"))
        for text in ("--de-adopt", "--plan", "preservation"):
            self.assertIn(text, read("remove-repository"))

    def test_commands_on_the_pages_use_real_verbs_and_flags(self):
        sources = "\n".join((ROOT / "src" / "eutonos" / name).read_text(encoding="utf-8") for name in FLAG_SOURCES)
        installer_verbs = set(installer._parser()._subparsers._group_actions[0].choices)
        for name in NAMES:
            for span in re.findall(r"`([^`\n]+)`", read(name)):
                if not span.startswith(COMMANDS):
                    continue
                for flag in re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", span):
                    self.assertIn(f'"{flag}"', sources, f"{name}: {span}")
                words = [word for word in span.split() if not word.startswith("--")]
                if span.startswith("eutonos-install "):
                    verb = next(word for word in words[1:] if word not in ("H",))
                    self.assertIn(verb, installer_verbs, f"{name}: {span}")
                elif span.startswith("eutonos "):
                    for verb in words[1].split("|"):
                        self.assertIn(verb, cli.LIFECYCLE, f"{name}: {span}")


class MachinePagesFollowARealRun(installer_tests.LifeBase):
    OS = "macos"

    def test_install_page_matches_the_install_checkpoint(self):
        result = self.install()
        self.assertEqual(result["outcome"], "installed", result)
        written = cp.read(self.h)
        self.assertEqual(written["checklist"], "install")
        page = parse("install")
        self.assertEqual(page["revision"], installer.CHECKLIST_REVISION)
        self.assertEqual(written["checklist_revision"], page["revision"])
        # the steps a real install writes, in the same order and with the same kind and runner
        listed = ids("install")
        self.assertEqual(listed[: len(written["steps"])], [step["id"] for step in written["steps"]])
        for step_id, pair in checkpoint_kinds(written).items():
            self.assertEqual(kinds("install")[step_id], pair, step_id)
        self.assertEqual([step[0] for step in installer.INSTALL_STEPS] + [step[0] for step in installer.INSTALL_OPEN],
                         [step["id"] for step in written["steps"]])
        # the rest are written only by an update or rollback: named in the code as literal step IDs
        extra = set(listed) - {step["id"] for step in written["steps"]}
        self.assertEqual(extra, {"three-way"})
        source = Path(installer.__file__).read_text(encoding="utf-8")
        self.assertIn('{"id": "three-way", "kind": "agent", "runner": "agent"', source)
        self.assertEqual(kinds("install")["three-way"], ("agent", "agent"))
        # a rollback writes two more, named on the page under Rollback as bullets
        rollback = read("install").split("\n## Rollback", 1)[1].split("\n## ", 1)[0]
        for step_id in ("resync", "reverse-newer-effects"):
            self.assertIn(f'{{"id": "{step_id}", "kind": "mechanical", "runner": "agent"', source)
            self.assertIn(f"- `{step_id}` (mechanical, agent)", rollback)

    def test_uninstall_page_matches_the_uninstall_checkpoint(self):
        self.install()
        result = self.uninstall()
        self.assertEqual(result["outcome"], "uninstalled", result)
        written = cp.read(self.h)
        self.assertEqual(written["checklist"], "uninstall")
        page = parse("uninstall")
        self.assertEqual(page["revision"], installer.CHECKLIST_REVISION)
        self.assertEqual(written["checklist_revision"], page["revision"])
        self.assertEqual(ids("uninstall"), [step["id"] for step in written["steps"]])
        self.assertEqual(kinds("uninstall"), checkpoint_kinds(written))
        undo = {f"undo-{item['id']}" for item in installer._person_reversals()}
        self.assertEqual({step_id for step_id in ids("uninstall") if step_id.startswith("undo-")}, undo)


class RepositoryPagesFollowTheChecks(unittest.TestCase):
    def snippet(self, name):
        found = re.search(r"```json\n(.*?)\n```", read(name), re.S)
        self.assertIsNotNone(found, name)
        return json.loads(found.group(1))

    def test_add_page_steps_are_the_add_check_ids_and_it_carries_the_exact_snippet(self):
        checks = lifecycle.repository_checks("add")
        self.assertEqual(ids("add-repository"), [check["id"] for check in checks])
        self.assertEqual(self.snippet("add-repository"), checks)
        self.assertTrue(parse("add-repository")["revision"])
        for step_id, (kind, _) in kinds("add-repository").items():
            if ".person-" in step_id:
                self.assertEqual(kind, "person", step_id)
        self.assertIn("--print-checks add", read("add-repository"))

    def test_remove_page_steps_are_the_remove_check_ids_and_it_carries_the_exact_snippet(self):
        checks = lifecycle.repository_checks("remove")
        self.assertEqual(ids("remove-repository"), [check["id"] for check in checks])
        self.assertEqual(self.snippet("remove-repository"), checks)
        self.assertEqual(parse("remove-repository")["revision"], lifecycle.CHECKLIST_REVISION)
        self.assertIn("--print-checks remove", read("remove-repository"))

    def test_remove_page_names_every_checkpoint_step_the_command_records(self):
        named = {}
        for step in parse("remove-repository")["steps"]:
            found = re.search(r"^- Checkpoint step: `([^`]+)`", step["block"], re.M)
            if found:
                named[found.group(1)] = (step["kind"], step["runs"])
        self.assertEqual(list(named), [item[0] for item in lifecycle.STEPS])
        self.assertEqual(named, {name: (kind, runner) for name, kind, runner in lifecycle.STEPS})


class RepairPageTests(unittest.TestCase):
    def test_repair_page_steps_are_the_repair_steps_in_order(self):
        self.assertEqual(ids("repair"), list(self_repair.REPAIR_STEPS))
        self.assertEqual(parse("repair")["revision"], "repair-1")
        self.assertEqual({runner for _, runner in kinds("repair").values()}, {"agent"})

    def test_repair_page_names_the_commands_the_installer_has(self):
        text = read("repair")
        verbs = set(installer._parser()._subparsers._group_actions[0].choices)
        for verb in ("patch", "self-check", "rollback", "status", "checkpoint", "resume"):
            self.assertIn(verb, verbs)
        for needle in ("--checkout", "--reason", "--allow-dirty", "--dry-run", "--official", self_repair.FIX_UNKNOWN):
            self.assertIn(needle, text)
        self.assertEqual(set(self_repair.REPAIR_STEPS) & {"self-check", "patch"}, {"self-check", "patch"})


if __name__ == "__main__":
    unittest.main()
