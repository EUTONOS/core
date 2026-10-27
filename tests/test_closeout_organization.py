import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from eutonos import closeout_organization as organization
from eutonos.host import connect, enroll, selected_apply


class CloseoutOrganizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="closeout-org-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.root = self.home / "project"; self.root.mkdir()
        (self.root / "docs" / "history").mkdir(parents=True)
        self.source = self.root / "docs" / "CURRENT_TASK.md"
        self.source.write_bytes(
            b"# Current task\r\n\r\nLead stays.\r\n\r\n"
            b"## Keep\r\n\r\nKeep bytes.\r\n\r\n"
            b"```md\r\n## Fenced heading\r\n```\r\n\r\n"
            b"```md\r\n```not-a-close\r\n## Still fenced\r\n```\r\n\r\n"
            b"## Archive me\r\n\r\nExact archived bytes.\r\n\r\n"
            b"## Tail\r\n\r\nTail stays.\r\n"
        )
        self.git("init", "-q", "-b", "main"); self.git("add", "--all")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "baseline")
        self.number = 0

    def git(self, *args):
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        return subprocess.check_output(["git", "-c", "safe.directory=" + str(self.root), "-C", str(self.root), *args], env=env)

    def request(self, **changes):
        value = {"profile": organization.PROFILE, "title": "Archive checkpoint",
                 "summary": "Preserve the completed checkpoint.", "disposition": "completed",
                 "headings": ["Archive me"], "archive_path": "docs/COMPLETED_WORK.md",
                 "generated_at": "2026-09-13T12:00:00Z"}
        value.update(changes)
        return value

    def apply(self, plan):
        self.number += 1
        host = self.home / f"host-{self.number}"
        enroll(self.root, host, "owner", plan["paths"])
        coordinator, authority = connect(self.root, host, "owner")
        attempt = f"eco-{self.number}"
        generation = coordinator.start(authority, attempt)
        coordinator.reserve(authority, attempt, generation, plan["paths"])
        return selected_apply(coordinator, authority, attempt, generation, "organization", plan["effect"], plan["fingerprint"])

    def test_archives_exact_section_and_preserves_every_unselected_byte(self):
        before = self.source.read_bytes()
        section = b"## Archive me\r\n\r\nExact archived bytes.\r\n\r\n"
        plan = organization.prepare(self.root, self.request())
        self.assertEqual(organization.readback(self.root, plan)["outcome"], "pending")
        self.apply(plan)
        current = self.source.read_bytes()
        self.assertEqual(current.replace(b'<a id="archive-me"></a>\n\nArchived: [Archive me](COMPLETED_WORK.md#archive-me)\n\n', b""), before.replace(section, b""))
        self.assertIn(section, (self.root / "docs" / "COMPLETED_WORK.md").read_bytes())
        self.assertEqual(organization.readback(self.root, plan)["outcome"], "applied")

    def test_fenced_absent_duplicate_active_and_work_sections_are_rejected(self):
        with self.assertRaisesRegex(Exception, "absent"):
            organization.prepare(self.root, self.request(headings=["Fenced heading"]))
        with self.assertRaisesRegex(Exception, "absent"):
            organization.prepare(self.root, self.request(headings=["Still fenced"]))
        self.source.write_bytes(self.source.read_bytes() + b"\r\n## Archive me\r\nAgain.\r\n")
        with self.assertRaisesRegex(Exception, "duplicated"):
            organization.prepare(self.root, self.request())
        self.source.write_bytes(b"# Task\n\n## Active: selected\nBody\n")
        with self.assertRaisesRegex(Exception, "archive_active"):
            organization.prepare(self.root, self.request(headings=["Active: selected"]))
        self.source.write_bytes(b"# Task\n\n## Done\n```tokenslang-work\n{}\n```\n")
        with self.assertRaisesRegex(Exception, "work or RAM routing"):
            organization.prepare(self.root, self.request(headings=["Done"]))

    def test_stale_plan_is_rejected_and_readback_reports_divergence(self):
        plan = organization.prepare(self.root, self.request())
        self.source.write_bytes(self.source.read_bytes() + b"external\n")
        self.assertEqual(organization.readback(self.root, plan)["outcome"], "diverged")
        with self.assertRaisesRegex(Exception, "precondition changed"):
            self.apply(plan)

    def test_archive_collision_and_repeated_effect_are_safe(self):
        source_before = self.source.read_bytes()
        plan = organization.prepare(self.root, self.request())
        result = self.apply(plan)
        self.assertEqual(result["outcome"], "completed")
        # The same selected effect is recognized through readback and does not need another write.
        self.assertEqual(organization.readback(self.root, plan)["outcome"], "applied")
        archive = self.root / "docs" / "COMPLETED_WORK.md"
        self.source.write_bytes(source_before)
        with self.assertRaisesRegex(Exception, "already contains"):
            organization.prepare(self.root, self.request())

    def test_small_change_needs_no_archive_and_has_no_ram_dependency(self):
        request = self.request(headings=[], archive_path=None, record_change=True, module="workflow", area="closeout")
        plan = organization.prepare(self.root, request)
        self.assertEqual(plan["paths"], ["docs/CHANGELOG.md"])
        self.assertFalse((self.root / "eutonos.ram.json").exists())
        self.apply(plan)
        body = (self.root / "docs" / "CHANGELOG.md").read_bytes()
        self.assertIn(b"```tokenslang-change", body)
        record = organization._change_blocks(body)[0]
        self.assertEqual((record["id"], record["module"], record["area"]), (plan["id"], "workflow", "closeout"))
        self.assertEqual(organization.readback(self.root, plan)["outcome"], "applied")
        repeated = organization.prepare(self.root, request)
        self.assertEqual((repeated["outcome"], repeated["paths"]), ("no-op", []))
        self.assertEqual((self.root / "docs" / "CHANGELOG.md").read_bytes(), body)

    def test_malformed_and_unsafe_requests_are_rejected(self):
        for request in (
            self.request(disposition="finished"),
            self.request(headings=["Archive me", "Archive me"]),
            self.request(archive_path="private/history.md"),
            self.request(archive_path="docs/private-history.md"),
            self.request(archive_path="docs/history/elsewhere.md"),
            self.request(archive_path="docs/history/../CURRENT_TASK.md"),
        ):
            with self.subTest(request=request), self.assertRaises(Exception):
                organization.prepare(self.root, request)

    def test_empty_organization_is_a_healthy_no_op(self):
        plan = organization.prepare(self.root, self.request(headings=[], archive_path=None, record_change=False))
        self.assertEqual((plan["outcome"], plan["paths"], plan["effect"]["changes"]), ("no-op", [], []))
        self.assertEqual(organization.readback(self.root, plan)["outcome"], "applied")


if __name__ == "__main__":
    unittest.main()
