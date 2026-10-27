"""Steps a switch could not finish itself: the store, the producers in ``set_component``, the role-hook line, the
pickup key and ``eutonos-install pending``. Everything uses temporary homes; the real ``~/.eutonos`` is never read."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from eutonos import installer, pending_actions as pa, role_hooks as rh, settings, workflow

import test_component_toggle as tct


class StoreBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="eutonos-pending-")
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "eutonos-home"
        self.home.mkdir()


class StoreTests(StoreBase):
    def test_a_missing_file_means_nothing_is_pending(self):
        self.assertEqual(pa.list_actions(self.home), [])
        self.assertEqual(pa.summary(self.home), {"count": 0, "items": [], "signature": "", "error": None})
        self.assertIsNone(pa.pickup_hint(self.home))

    def test_add_stores_every_field_and_dedupes_by_source_and_action(self):
        first = pa.add(self.home, source="component:crosstalk", harness="claude", action="Restart Claude.", why="loads plugins")
        again = pa.add(self.home, source="component:crosstalk", harness="claude", action="Restart  Claude.")
        self.assertEqual(first["id"], again["id"])
        self.assertTrue(first["id"].startswith("pa-"))
        self.assertEqual((first["state"], first["done_at"], first["done_by"], first["harness"]),
                         ("pending", None, None, "claude"))
        self.assertEqual(first["why"], "loads plugins")
        other = pa.add(self.home, source="setting:memory-block", action="Add the memory.")
        self.assertNotEqual(other["id"], first["id"])
        self.assertEqual(other["harness"], "any")
        self.assertEqual(len(pa.list_actions(self.home)), 2)
        stored = json.loads(pa.path(self.home).read_text(encoding="utf-8"))
        self.assertEqual(stored["profile"], pa.PROFILE)
        self.assertEqual(len(stored["actions"]), 2)

    def test_bad_input_is_refused(self):
        with self.assertRaises(pa.PendingActionsError):
            pa.add(self.home, source="x", action="y", harness="gemini")
        with self.assertRaises(pa.PendingActionsError):
            pa.add(self.home, source="x", action="  ")
        self.assertFalse(pa.path(self.home).exists())

    def test_list_filters_by_harness_and_state(self):
        a = pa.add(self.home, source="s", harness="claude", action="for claude")
        pa.add(self.home, source="s", harness="codex", action="for codex")
        pa.add(self.home, source="s", harness="any", action="for anyone")
        self.assertEqual([e["action"] for e in pa.list_actions(self.home, harness="claude")], ["for claude", "for anyone"])
        self.assertEqual([e["action"] for e in pa.list_actions(self.home, harness="codex")], ["for codex", "for anyone"])
        pa.complete(self.home, a["id"], by="chat-1")
        self.assertEqual(len(pa.list_actions(self.home)), 2)
        self.assertEqual([e["state"] for e in pa.list_actions(self.home, state="done")], ["done"])
        self.assertEqual(len(pa.list_actions(self.home, state="all")), 3)

    def test_complete_and_dismiss_record_who_and_when_and_are_idempotent(self):
        a = pa.add(self.home, source="s", action="one")
        b = pa.add(self.home, source="s", action="two")
        done = pa.complete(self.home, a["id"], by="claude chat")
        self.assertEqual((done["state"], done["done_by"]), ("done", "claude chat"))
        self.assertTrue(done["done_at"])
        again = pa.complete(self.home, a["id"], by="someone else")
        self.assertEqual(again["done_by"], "claude chat")
        gone = pa.dismiss(self.home, b["id"], note="not needed")
        self.assertEqual((gone["state"], gone["note"]), ("dismissed", "not needed"))
        with self.assertRaises(pa.UnknownAction):
            pa.complete(self.home, "pa-nothing")

    def test_the_same_step_can_be_asked_again_once_the_earlier_one_is_done(self):
        a = pa.add(self.home, source="s", action="one")
        pa.complete(self.home, a["id"])
        b = pa.add(self.home, source="s", action="one")
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(b["state"], "pending")

    def test_dismiss_source_only_touches_that_sources_pending_entries(self):
        a = pa.add(self.home, source="component:x", action="one")
        pa.add(self.home, source="component:y", action="two")
        self.assertEqual(pa.dismiss_source(self.home, "component:x"), [a["id"]])
        self.assertEqual([e["action"] for e in pa.list_actions(self.home)], ["two"])
        self.assertEqual(pa.dismiss_source(self.home, "component:x"), [])

    def test_an_unreadable_file_fails_visibly_and_is_never_read_as_empty(self):
        target = pa.path(self.home)
        target.parent.mkdir(parents=True)
        for text in ("{not json", json.dumps({"profile": "other", "actions": []}),
                     json.dumps({"profile": pa.PROFILE, "actions": [{"id": "pa-1"}]})):
            target.write_text(text, encoding="utf-8")
            with self.assertRaises(pa.PendingActionsError):
                pa.list_actions(self.home)
            with self.assertRaises(pa.PendingActionsError):
                pa.add(self.home, source="s", action="x")
            self.assertEqual(target.read_text(encoding="utf-8"), text)  # never replaced
            found = pa.summary(self.home)
            self.assertEqual((found["count"], found["signature"]), (0, "unreadable"))
            self.assertTrue(found["error"])
            self.assertIn("could not be read", pa.pickup_hint(self.home)["line"])

    def test_finished_entries_beyond_the_cap_drop_off_but_pending_ones_never_do(self):
        keep = pa.add(self.home, source="s", action="keep me")
        for number in range(pa.MAX_FINISHED_KEPT + 5):
            entry = pa.add(self.home, source="s", action=f"step {number}")
            pa.complete(self.home, entry["id"])
        everything = pa.list_actions(self.home, state="all")
        self.assertEqual(len([e for e in everything if e["state"] == "done"]), pa.MAX_FINISHED_KEPT)
        self.assertIn(keep["id"], [e["id"] for e in everything])

    def test_memory_step_helper_uses_the_callers_wording_and_a_known_harness(self):
        entry = pa.request_memory_step(self.home, source="setting:memory-block", harness="codex", verb="add",
                                       memory="EUTONOS", instructions="Use the wording in docs/X.md.", why="toggle on")
        self.assertEqual(entry["harness"], "codex")
        self.assertEqual(entry["action"], "Add the EUTONOS memory in Codex's own memory. Use the wording in docs/X.md.")
        for bad in ({"harness": "any"}, {"verb": "change"}, {"instructions": ""}):
            values = dict(source="s", harness="claude", verb="remove", memory="EUTONOS", instructions="x")
            values.update(bad)
            with self.assertRaises(pa.PendingActionsError):
                pa.request_memory_step(self.home, **values)

    def test_summary_is_bounded_and_the_signature_follows_the_pending_set(self):
        long_text = "word " * 100
        ids = [pa.add(self.home, source="s", harness="claude", action=f"{n} {long_text}")["id"] for n in range(4)]
        found = pa.summary(self.home, harness="claude", limit=2, line_chars=50)
        self.assertEqual((found["count"], len(found["items"])), (4, 2))
        self.assertTrue(all(len(item["action"]) <= 50 for item in found["items"]))
        before = found["signature"]
        pa.complete(self.home, ids[3])
        self.assertNotEqual(pa.summary(self.home, harness="claude")["signature"], before)
        self.assertEqual(pa.summary(self.home, harness="codex")["count"], 0)
        pa.path(self.home).write_bytes(b" " * (pa.MAX_FILE_BYTES + 5))
        self.assertEqual(pa.summary(self.home)["signature"], "unreadable")


# ------------------------------------------------------------------------------- producers
class ProducerTests(tct.ToggleBase):
    def test_a_part_that_needs_an_app_restart_leaves_one_step_per_app(self):
        manifests = self.parts("fakecomp")
        patch = mock.patch.dict(installer.COMPONENT_RESTARTS, {"fakecomp": ("claude", "codex")})
        patch.start()
        self.addCleanup(patch.stop)
        self.first(components=manifests)
        off = self.toggle("fakecomp", False, components=manifests)
        self.assertEqual(off["outcome"], "disabled", off)
        found = pa.list_actions(self.h)
        self.assertEqual(sorted(e["harness"] for e in found), ["claude", "codex"])
        self.assertEqual(sorted(off["pending"]), sorted(e["id"] for e in found))
        self.assertTrue(all(e["source"] == "component:fakecomp" and "switched off" in e["action"] for e in found))
        on = self.toggle("fakecomp", True, components=manifests)
        self.assertEqual(on["outcome"], "enabled", on)
        found = pa.list_actions(self.h)  # the "off" steps were superseded by the "on" ones
        self.assertEqual(len(found), 2)
        self.assertTrue(all("switched on" in e["action"] for e in found))
        self.assertEqual(len(pa.list_actions(self.h, state="dismissed")), 2)

    def test_a_part_with_no_restart_and_nothing_kept_leaves_nothing(self):
        manifests = self.parts("fakecomp")
        self.first(components=manifests)
        off = self.toggle("fakecomp", False, components=manifests)
        self.assertNotIn("pending", off)
        self.assertFalse(pa.path(self.h).exists())

    def test_a_kept_change_on_disable_leaves_a_step_naming_it(self):
        manifests = self.parts("fakecomp")
        self.first(components=manifests)
        self.marker("fakecomp").write_bytes(b"edited by the person\n")
        off = self.toggle("fakecomp", False, components=manifests)
        self.assertEqual(off["outcome"], "disabled", off)
        self.assertEqual(off["kept"][0]["effect_id"], "fakecomp.marker")
        found = pa.list_actions(self.h)
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["harness"], found[0]["source"]), ("any", "component:fakecomp"))
        self.assertIn("fakecomp.marker", found[0]["action"])
        self.assertIn("changed by you", found[0]["why"])
        self.assertEqual(off["pending"], [found[0]["id"]])

    def test_an_unreadable_store_does_not_undo_the_switch_but_says_so(self):
        manifests = self.parts("fakecomp")
        patch = mock.patch.dict(installer.COMPONENT_RESTARTS, {"fakecomp": ("claude",)})
        patch.start()
        self.addCleanup(patch.stop)
        self.first(components=manifests)
        pa.path(self.h).write_text("{broken", encoding="utf-8")
        off = self.toggle("fakecomp", False, components=manifests)
        self.assertEqual(off["outcome"], "disabled", off)
        self.assertFalse(self.saved()["fakecomp"]["enabled"])
        self.assertTrue(any("could not be saved" in note for note in off["notes"]), off["notes"])
        self.assertNotIn("pending", off)

    def test_the_real_manifests_that_need_a_restart_say_so(self):
        ids = {item["id"] for item in installer.load_components()}
        self.assertLessEqual(set(installer.COMPONENT_RESTARTS), ids)
        self.assertEqual(installer.COMPONENT_RESTARTS["crosstalk"], ("claude", "codex"))
        self.assertTrue(all(app in pa.HARNESS_NAMES for apps in installer.COMPONENT_RESTARTS.values() for app in apps))


# ------------------------------------------------------------------------------- role hooks
class HookLineTests(StoreBase):
    def setUp(self):
        super().setUp()
        self.root = Path(self._tmp.name) / "project"
        self.root.mkdir()
        env = mock.patch.dict(os.environ, {"EUTONOS_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("EUTONOS_ROLE", None)

    def fire(self, event, app="claude", session="s1", **fields):
        payload = {"hook_event_name": event, "session_id": session, "cwd": str(self.root), **fields}
        return rh._dispatch(self.root, payload, app=app)

    def context(self, out):
        return (out.get("hookSpecificOutput") or {}).get("additionalContext", "")

    def add(self, action="Ask the person to restart Claude so Crosstalk is on.", **fields):
        return pa.add(self.home, source="component:crosstalk", harness=fields.pop("harness", "claude"), action=action, **fields)

    def test_nothing_is_added_when_the_list_is_empty(self):
        out = self.fire("SessionStart")
        self.assertNotIn("pending actions", self.context(out))
        self.assertNotIn("pending actions", self.context(self.fire("UserPromptSubmit", prompt="hello")))

    def test_session_start_and_resume_show_the_count_and_the_first_two_items(self):
        for number in range(3):
            self.add(f"Step {number}: " + "long words " * 20)
        for source in ("startup", "resume", "compact"):
            text = self.context(self.fire("SessionStart", source=source))
            self.assertIn("EUTONOS pending actions (3):", text)
            self.assertIn("Step 0", text)
            self.assertIn("Step 1", text)
            self.assertNotIn("Step 2", text)
            self.assertIn("(+1 more)", text)
            self.assertIn("eutonos-install pending list", text)
        line = text[text.index("EUTONOS pending actions"):]
        self.assertLessEqual(len(line), rh.PENDING_LINE_MAX_CHARS)

    def test_the_line_comes_after_the_reminder_and_stays_within_the_caps(self):
        self.add("x" * 600)
        self.add("y" * 600)
        self.add("z" * 600)
        text = self.context(self.fire("SessionStart"))
        reminder, _, line = text.rpartition("\n")
        self.assertTrue(line.startswith("EUTONOS pending actions (3)"))
        self.assertTrue(reminder)  # the configured reminder is still there
        self.assertLessEqual(len(line), rh.PENDING_LINE_MAX_CHARS)
        self.assertLessEqual(len(text), 2 * (settings.MAX_HOOK_TEXT_CHARS + 1 + rh.MAX_DELIVERY_NOTICE_CHARS)
                             + rh.PENDING_LINE_MAX_CHARS + 2)

    def test_a_message_shows_it_only_when_the_list_changed(self):
        first = self.add("First step.")
        self.assertIn("First step.", self.context(self.fire("SessionStart")))
        self.assertNotIn("pending actions", self.context(self.fire("UserPromptSubmit", prompt="hi")))
        self.assertNotIn("pending actions", self.context(self.fire("UserPromptSubmit", prompt="again")))
        self.add("Second step.")
        changed = self.context(self.fire("UserPromptSubmit", prompt="and now"))
        self.assertIn("(2)", changed)
        self.assertNotIn("pending actions", self.context(self.fire("UserPromptSubmit", prompt="later")))
        pa.complete(self.home, first["id"])
        self.assertIn("(1)", self.context(self.fire("UserPromptSubmit", prompt="after one was done")))

    def test_a_new_session_sees_it_again_and_a_session_without_a_start_event_sees_it_on_its_first_message(self):
        self.add("Only step.")
        self.assertIn("Only step.", self.context(self.fire("UserPromptSubmit", prompt="hi", session="fresh")))
        self.assertNotIn("pending actions", self.context(self.fire("UserPromptSubmit", prompt="hi", session="fresh")))
        self.assertIn("Only step.", self.context(self.fire("SessionStart", session="another")))

    def test_the_line_is_for_this_app_and_never_for_a_subagent(self):
        self.add("Restart Claude.", harness="claude")
        self.assertNotIn("pending actions", self.context(self.fire("SessionStart", app="codex")))
        self.assertIn("Restart Claude.", self.context(self.fire("SessionStart", app="claude")))
        child = self.fire("SubagentStart", agent_id="a1", agent_type="general-purpose")
        self.assertNotIn("pending actions", self.context(child))
        self.add("Anyone.", harness="any")
        self.assertIn("Anyone.", self.context(self.fire("SessionStart", app="codex", session="c1")))

    def test_it_shows_even_when_the_reminder_text_for_the_trigger_is_off(self):
        current = settings.read_hooks(self.root)
        settings.update_hooks(self.root, {**current["hooks"], "trigger_enabled": {**current["hooks"]["trigger_enabled"],
                                                                                    "session_start": False}},
                              expected_revision=current["revision"])
        self.add("Still shown.")
        self.assertEqual(self.context(self.fire("SessionStart")).count("Still shown."), 1)

    def test_global_hooks_off_adds_nothing(self):
        current = settings.read_hooks(self.root)
        settings.update_hooks(self.root, {**current["hooks"], "enabled": False}, expected_revision=current["revision"])
        self.add("Hidden.")
        self.assertEqual(self.fire("SessionStart"), {})

    def test_an_unreadable_list_gets_one_short_warning_and_nothing_else(self):
        pa.path(self.home).parent.mkdir(parents=True, exist_ok=True)
        pa.path(self.home).write_text("{broken", encoding="utf-8")
        text = self.context(self.fire("SessionStart"))
        self.assertIn(rh.PENDING_UNREADABLE_LINE, text)
        self.assertLess(len(rh.PENDING_UNREADABLE_LINE), 120)
        self.assertNotIn("pending actions", self.context(self.fire("UserPromptSubmit", prompt="hi")))

    def test_a_broken_home_or_reader_never_raises_into_the_host(self):
        self.add("Hidden.")
        with mock.patch.dict(os.environ, {"EUTONOS_HOME": "relative/path"}):
            out = self.fire("SessionStart", session="bad-home")
        self.assertNotIn("pending actions", self.context(out))
        with mock.patch.object(pa, "summary", side_effect=RuntimeError("boom")):
            out = self.fire("SessionStart", session="bad-reader")
        self.assertNotIn("pending actions", self.context(out))

    def test_pre_tool_use_adds_nothing(self):
        self.add("Step.")
        self.assertNotIn("pending actions", self.context(self.fire("PreToolUse", tool_name="Read")))


# ------------------------------------------------------------------------------- pickup
class PickupTests(StoreBase):
    def test_hint_gives_the_count_and_the_first_items(self):
        for number in range(5):
            pa.add(self.home, source="s", harness="claude", action=f"Step {number}")
        hint = pa.pickup_hint(self.home, limit=3)
        self.assertEqual((hint["count"], len(hint["items"])), (5, 3))
        self.assertIn("pending actions: 5", hint["line"])
        self.assertIn("Step 2", hint["line"])
        self.assertIn("(+2 more)", hint["line"])
        self.assertIn("eutonos-install pending done ID", hint["line"])

    def test_workflow_pickup_carries_the_key_only_when_something_waits(self):
        import test_workflow as tw
        base = tw.WorkflowTests("test_pickup_omits_crosstalk_field_when_crosstalk_is_not_installed")
        base.setUp()
        self.addCleanup(base.tearDown)
        with mock.patch.dict(os.environ, {"EUTONOS_HOME": str(self.home)}):
            self.assertNotIn("pending_actions", tw.pickup(base.root))
            pa.add(self.home, source="s", action="Restart Claude.")
            result = tw.pickup(base.root)
            self.assertEqual(result["pending_actions"]["count"], 1)
            with mock.patch("eutonos.pending_actions.pickup_hint", side_effect=RuntimeError("boom")):
                survived = tw.pickup(base.root)
        self.assertEqual(survived["outcome"], "pickup")
        self.assertNotIn("pending_actions", survived)


# ------------------------------------------------------------------------------- CLI
class CliTests(StoreBase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = installer.main(["--home", str(self.home), "pending", *argv])
        return code, out.getvalue()

    def test_list_prints_a_short_text_or_json_and_filters_by_app(self):
        code, text = self.run_cli("list")
        self.assertEqual((code, text), (0, "Nothing is waiting for an agent.\n"))
        a = pa.add(self.home, source="component:crosstalk", harness="claude", action="Restart Claude.", why="reason one")
        pa.add(self.home, source="s", harness="codex", action="Restart Codex.")
        code, text = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn(f"{a['id']}  [claude]  Restart Claude.", text)
        self.assertIn("why: reason one", text)
        code, text = self.run_cli("list", "--harness", "codex", "--json")
        found = json.loads(text)
        self.assertEqual((code, found["count"], found["actions"][0]["action"]), (0, 1, "Restart Codex."))

    def test_no_project_pickup_is_usable_and_does_not_claim_automatic_turn_delivery(self):
        pa.add(self.home, source="setting:memory-block", harness="codex", action="Remove a memory.")
        code, text = self.run_cli("list", "--harness", "codex", "--json")
        found = json.loads(text)
        self.assertEqual((code, found["count"], found["delivery"]["state"]), (0, 1, "manual-pickup"))
        self.assertEqual(found["delivery"]["automatic_turn_delivery"], "not-guaranteed")
        self.assertIn("held", found["delivery"]["reason"])

    def test_done_and_dismiss_change_the_state_and_say_who(self):
        a = pa.add(self.home, source="s", action="one")
        b = pa.add(self.home, source="s", action="two")
        code, text = self.run_cli("done", a["id"], "--by", "claude chat")
        self.assertEqual((code, json.loads(text)["action"]["done_by"]), (0, "claude chat"))
        code, text = self.run_cli("dismiss", b["id"], "--note", "not needed")
        self.assertEqual((code, json.loads(text)["outcome"]), (0, "dismissed"))
        code, text = self.run_cli("list")
        self.assertEqual(text, "Nothing is waiting for an agent.\n")
        code, text = self.run_cli("list", "--all")
        self.assertIn("(done)", text)
        self.assertIn("(dismissed)", text)

    def test_unknown_ids_missing_ids_and_a_broken_file_are_reported(self):
        code, text = self.run_cli("done", "pa-nothing")
        self.assertEqual((code, json.loads(text)["reason"]), (1, "unknown-action"))
        code, text = self.run_cli("done")
        self.assertEqual(code, 2)
        pa.path(self.home).parent.mkdir(parents=True, exist_ok=True)
        pa.path(self.home).write_text("{broken", encoding="utf-8")
        code, text = self.run_cli("list")
        self.assertEqual((code, json.loads(text)["reason"]), (1, "state-unreadable"))


if __name__ == "__main__":
    unittest.main()
