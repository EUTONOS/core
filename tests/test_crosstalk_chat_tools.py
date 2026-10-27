"""Offline tests for the chat-facing Crosstalk tools (slice 3B.11): compact bounded list, create, core-rule text.

No live app, plugin or account is used; create runs a fake CLI script standing in for crosstalk_p0_cli.py."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


KIT = Path(__file__).resolve().parents[1] / "tools" / "crosstalk"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


core = load("crosstalk_chat_tools_core", KIT / "crosstalk_p0_core.py")
with mock.patch.dict("sys.modules", {"crosstalk_p0_core": core}):
    server = load("crosstalk_chat_tools_server", KIT / "codex_tool_server.py")
plugin = load("crosstalk_chat_tools_hermes", KIT / "hermes-plugin" / "crosstalk" / "__init__.py")
CLAUDE_PLUGIN = (KIT / "claude-plugin" / "plugin-src" / "register.mjs.in").read_text(encoding="utf-8")

UUID = "11111111-2222-4333-8444-555555555555"
APPS = ("claude", "codex", "hermes")


def make_entries(count=200, title_len=300):
    rows = []
    for index in range(count):
        app = APPS[index % 3]
        rows.append({"key": f"{app}:{index:08d}-2222-4333-8444-555555555555", "app": app,
                     "title": f"Chat {index} " + "x" * title_len,
                     "project_folder": "C:\\Users\\someone\\Projects\\" + "deep" * 30 + f"\\proj{index}",
                     "cwd_kind": "dir", "account_id": "acct-" + "a" * 36, "last_seen_ns": 1000 + index,
                     "self": index == 7, "delivery_hint": "sendable"})
    return rows


class CompactListTests(unittest.TestCase):
    def test_default_cap_order_more_and_compact_fields(self):
        view = core.compact_list(make_entries(), "claude:00000007-2222-4333-8444-555555555555")
        self.assertEqual((view["total"], view["shown"], view["more"]), (200, 40, 160))
        newest = view["entries"][0]
        self.assertTrue(newest["key"].startswith(("claude:00000199", "codex:00000199", "hermes:00000199"))
                        or "00000199" in newest["key"])
        for row in view["entries"]:
            self.assertEqual(set(row) - {"title", "folder", "self"}, {"key", "app", "hint"})
        self.assertNotIn("account_id", json.dumps(view))
        self.assertNotIn("last_seen_ns", json.dumps(view))

    def test_self_marked_and_titles_truncated(self):
        view = core.compact_list(make_entries(), None, query="Chat 7", limit=100)
        mine = [row for row in view["entries"] if row.get("self")]
        self.assertEqual(len(mine), 1)
        self.assertLessEqual(len(mine[0]["title"]), core.LIST_TITLE_MAX)
        self.assertTrue(mine[0]["title"].endswith("\u2026"))
        self.assertLessEqual(len(mine[0]["folder"]), core.LIST_FOLDER_MAX)

    def test_query_is_case_insensitive_on_title_folder_and_key(self):
        entries = make_entries(30, 5)
        entries[3]["title"] = "Release Planning"
        entries[4]["project_folder"] = "C:\\work\\Billing"
        self.assertEqual([r["key"] for r in core.compact_list(entries, query="release PLAN")["entries"]],
                         [entries[3]["key"]])
        self.assertEqual([r["key"] for r in core.compact_list(entries, query="billing")["entries"]],
                         [entries[4]["key"]])
        self.assertEqual(core.compact_list(entries, query=entries[9]["key"].upper())["total"], 1)

    def test_app_filter_and_validation(self):
        view = core.compact_list(make_entries(), app="hermes", limit=100)
        self.assertTrue(view["entries"] and {r["app"] for r in view["entries"]} == {"hermes"})
        for bad in ({"app": "slack"}, {"limit": 0}, {"limit": "5"}, {"query": 5}):
            with self.assertRaises(core.ProbeError):
                core.compact_list([], **bad)

    def test_size_bound_with_200_long_titled_entries(self):
        view = core.compact_list(make_entries(200, 5000), "self")
        self.assertLess(len(json.dumps(view).encode()), 32768 // 2)
        widest = core.compact_list(make_entries(200, 5000), limit=100)
        self.assertLess(len(json.dumps(widest).encode()), 32768)

    def test_chat_list_view_wraps_core_data_and_leaves_full_list_alone(self):
        full = make_entries(5, 5)
        with mock.patch.object(core, "list_directory", return_value=full), \
                mock.patch.object(core, "controls_load", return_value={"messaging": "on"}):
            view = core.chat_list_view("codex:x", "chat", "claude", None)
        self.assertEqual((view["ok"], view["self"], view["controls"]), (True, "codex:x", {"messaging": "on"}))
        self.assertIn("account_id", full[0])  # the full rows are untouched

    def test_codex_tool_server_list_takes_arguments(self):
        names = {t["name"]: t for t in server.TOOLS}
        self.assertEqual(set(names["crosstalk_list"]["inputSchema"]["properties"]), {"query", "app", "limit"})
        meta = {"threadId": UUID}
        with mock.patch.object(server.core, "list_directory", return_value=make_entries(50, 5)), \
                mock.patch.object(server.core, "controls_load", return_value={}):
            value = server.handle_list(meta, {"app": "codex", "query": "chat 1", "limit": 3})
            bad = server.handle_list(meta, {"app": "nope"})
        self.assertTrue(value["ok"] and value["shown"] <= 3 and all(r["app"] == "codex" for r in value["entries"]))
        self.assertEqual((bad["ok"], bad["reason"]), (False, "invalid-app"))


FAKE_CLI = r'''
import json, sys, time
argv = sys.argv[1:]
log = {"argv": argv}
if "--prompt-file" in argv:
    log["prompt"] = open(argv[argv.index("--prompt-file") + 1], encoding="utf-8").read()
open(__file__ + ".log", "w", encoding="utf-8").write(json.dumps(log))
mode = open(__file__ + ".mode", encoding="utf-8").read().strip()
if mode == "slow":
    time.sleep(2)
if mode == "refuse":
    print(json.dumps({"ok": False, "reason": "controls-halted"}))
elif mode == "uncertain":
    print(json.dumps({"ok": False, "reason": "create-startup-unconfirmed", "operation_id": "op"}))
elif mode == "unknown-model":
    print(json.dumps({"ok": False, "reason": "unknown-model", "detail": "m is not offered", "operation_id": "op"}))
elif mode == "garbage":
    print("not json")
else:
    app = argv[0].split("-")[1]
    print(json.dumps({"ok": True, "key": app + ":new", "session_id": "new", "requested": {}}))
'''


class CreateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="crosstalk-create-tools-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "crosstalk_p0_cli.py").write_text(FAKE_CLI, encoding="utf-8")
        self.folder = self.root / "proj"
        self.folder.mkdir()
        patcher = mock.patch.object(core, "KIT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def mode(self, value):
        (self.root / "crosstalk_p0_cli.py.mode").write_text(value, encoding="utf-8")

    def log(self):
        return json.loads((self.root / "crosstalk_p0_cli.py.log").read_text(encoding="utf-8"))

    def test_argv_mapping_per_app(self):
        claude = core.chat_create_argv("claude", {"folder": "C:\\p", "model": "m", "effort": "high", "title": "T"}, "f.txt")
        self.assertEqual(claude, ["create-claude", "--folder", "C:\\p", "--model", "m", "--effort", "high",
                                  "--title", "T", "--prompt-file", "f.txt"])
        codex = core.chat_create_argv("codex", {"folder": "C:\\p", "model": "m", "effort": "xhigh"}, "f.txt")
        self.assertEqual(codex[0], "create-codex")
        hermes = core.chat_create_argv("hermes", {"model": "m", "provider": "pr", "effort": "low"}, "f.txt", 30)
        self.assertEqual(hermes, ["create-hermes", "--model", "m", "--provider", "pr", "--effort", "low",
                                  "--wait-seconds", "30", "--prompt-file", "f.txt"])
        self.assertEqual(core.chat_create_argv("hermes", {}, "f.txt"), ["create-hermes", "--prompt-file", "f.txt"])
        for argv in (claude, codex, hermes):
            self.assertNotIn("--prompt", argv)

    def test_prompt_goes_through_a_utf8_file_never_the_command_line(self):
        self.mode("ok")
        prompt = "Gr\u00fc\u00dfe \u4e2d\u6587 \"quoted\"\nline two"
        result = core.chat_create("codex", {"folder": str(self.folder), "model": "m", "effort": "high",
                                            "prompt": prompt}, None, wait_s=60)
        self.assertEqual((result["ok"], result["state"], result["key"]), (True, "created", "codex:new"))
        log = self.log()
        self.assertEqual(log["prompt"], prompt)
        self.assertNotIn(prompt, " ".join(log["argv"]))
        self.assertIn("--prompt-file", log["argv"])
        self.assertFalse(Path(log["argv"][log["argv"].index("--prompt-file") + 1]).exists())  # temp file removed

    def test_first_prompt_names_the_creator_for_every_app(self):
        self.mode("ok")
        for app, key in (("claude", "claude:11111111-2222-4333-8444-555555555555"),
                         ("codex", "codex:aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"),
                         ("hermes", "hermes:20261013_230248_6a7bf5")):
            params = {"prompt": "Please send me a one-line hello."}
            if app != "hermes":
                params.update(folder=str(self.folder), model="m", effort="low")
            core.chat_create(app, params, key, wait_s=60)
            prompt = self.log()["prompt"]
            self.assertTrue(prompt.startswith("Please send me a one-line hello.\n\n(Crosstalk: this chat was opened by "))
            self.assertIn(f"{app.capitalize()} chat {key}.", prompt)
            self.assertIn("send a Crosstalk letter to that key", prompt)
        # unknown or malformed caller key: the prompt is exactly the caller's text
        for key in (None, "", "slack:x", "claude:bad key"):
            core.chat_create("hermes", {"prompt": "plain"}, key, wait_s=60)
            self.assertEqual(self.log()["prompt"], "plain")

    def test_origin_line_respects_the_prompt_cap(self):
        self.mode("ok")
        key = "claude:11111111-2222-4333-8444-555555555555"
        room = core.CHAT_CREATE_PROMPT_MAX - len(core.chat_origin_line(key))
        ok = core.chat_create("hermes", {"prompt": "x" * room}, key, wait_s=60)
        self.assertEqual(ok["state"], "created")
        refused = core.chat_create("hermes", {"prompt": "x" * (room + 1)}, key, wait_s=60)
        self.assertEqual((refused["state"], refused["reason"]), ("refused", "prompt-too-long-for-origin-line"))

    def test_folder_defaults_to_the_calling_chats_own_folder(self):
        self.mode("ok")
        with mock.patch.object(core, "caller_project_folder", return_value=str(self.folder)):
            result = core.chat_create("claude", {"model": "m", "effort": "low", "prompt": "hi"}, "claude:x", wait_s=60)
        self.assertEqual(result["state"], "created")
        argv = self.log()["argv"]
        self.assertEqual(argv[argv.index("--folder") + 1], str(self.folder))
        with mock.patch.object(core, "caller_project_folder", return_value=None):
            refused = core.chat_create("codex", {"model": "m", "effort": "low", "prompt": "hi"}, "codex:x")
        self.assertEqual((refused["state"], refused["reason"]), ("refused", "folder-required"))

    def test_refusals_never_start_a_process(self):
        for app, params, reason in (
                ("slack", {"prompt": "x"}, "invalid-app"),
                ("claude", {"folder": "C:\\p", "effort": "low", "prompt": "x"}, "model-required"),
                ("claude", {"folder": "C:\\p", "model": "m", "prompt": "x"}, "effort-required"),
                ("codex", {"folder": "C:\\p", "model": "m", "effort": "low", "prompt": "  "}, "prompt-invalid"),
                ("claude", {"folder": "C:\\p", "model": "m", "effort": "low", "prompt": "x", "provider": "p"},
                 "provider-only-for-hermes"),
                ("hermes", {"prompt": "x", "cwd": "C:\\"}, "unexpected-fields:cwd")):
            result = core.chat_create(app, params, None)
            self.assertEqual((result["state"], result["created"], result["reason"]), ("refused", False, reason))
        self.assertFalse((self.root / "crosstalk_p0_cli.py.log").exists())

    def test_cli_refusal_uncertain_and_unreadable_results(self):
        params = {"folder": str(self.folder), "model": "m", "effort": "low", "prompt": "hi"}
        self.mode("refuse")
        refused = core.chat_create("claude", params, None, wait_s=60)
        self.assertEqual((refused["state"], refused["created"], refused["reason"]), ("refused", False, "controls-halted"))
        self.mode("uncertain")
        uncertain = core.chat_create("codex", params, None, wait_s=60)
        self.assertEqual((uncertain["state"], uncertain["created"]), ("uncertain", None))
        self.assertIn("Do NOT create another", uncertain["guidance"])
        self.mode("garbage")
        unreadable = core.chat_create("claude", params, None, wait_s=60)
        self.assertEqual((unreadable["state"], unreadable["created"]), ("uncertain", None))
        self.mode("unknown-model")
        unknown = core.chat_create("codex", params, None, wait_s=60)
        self.assertEqual((unknown["state"], unknown["created"], unknown["reason"]), ("refused", False, "unknown-model"))
        self.assertNotIn("guidance", unknown)

    def test_slow_create_reports_started_not_failure(self):
        self.mode("slow")
        result = core.chat_create("hermes", {"prompt": "hi"}, None, wait_s=0.3)
        self.assertEqual((result["ok"], result["state"], result["pending"], result["created"]),
                         (True, "started", True, None))
        self.assertIn("NOT retried", result["guidance"])

    def test_codex_tool_server_create_maps_arguments(self):
        names = {t["name"]: t for t in server.TOOLS}
        schema = names["crosstalk_create"]["inputSchema"]
        self.assertEqual(set(schema["properties"]), set(core.CHAT_CREATE_FIELDS))
        self.assertEqual(schema["required"], ["app", "prompt"])
        meta = {"threadId": UUID}
        with mock.patch.object(server.core, "chat_create", return_value={"ok": True, "state": "created"}) as made:
            value = server.handle_create(meta, {"app": "claude", "folder": "C:\\p", "model": "m", "effort": "low",
                                                "prompt": "hi", "title": None})
        self.assertEqual(value["tag"], server.TAG)
        args = made.call_args
        self.assertEqual(args[0][0], "claude")
        self.assertEqual(args[0][1], {"folder": "C:\\p", "model": "m", "effort": "low", "prompt": "hi"})
        self.assertEqual(args[0][2], "codex:" + UUID)
        self.assertEqual(server.handle_create(None, {"app": "claude", "prompt": "x"})["reason"], "no-native-caller")

    def test_hermes_plugin_create_uses_a_prompt_file_and_cli_command(self):
        config = {"python": "C:/rt/python.exe", "cli": "C:/kit/crosstalk_p0_cli.py"}
        captured = {}

        def fake_run(argv, **options):
            captured["argv"], captured["timeout"] = list(argv), options["timeout"]
            path = argv[argv.index("--prompt-file") + 1]
            captured["prompt"] = Path(path).read_bytes().decode("utf-8")
            captured["path"] = path
            return SimpleNamespace(returncode=0, stdout='{"ok": true, "state": "created", "key": "claude:n"}\n', stderr="")

        params = {"app": "claude", "folder": "C:\\p", "model": "m", "effort": "high", "title": "T",
                  "prompt": "caf\u00e9 \u4e2d\u6587"}
        with mock.patch.object(plugin, "_config", return_value=config), \
                mock.patch.object(plugin.subprocess, "run", side_effect=fake_run):
            out = plugin._handler("crosstalk_create")(params, session_id="20261013_230248_6a7bf5")
        self.assertEqual(json.loads(out)["key"], "claude:n")
        argv = captured["argv"]
        self.assertEqual(argv[4:], ["create-claude", "--folder", "C:\\p", "--model", "m", "--effort", "high",
                                    "--title", "T", "--prompt-file", captured["path"]])
        self.assertEqual(captured["prompt"], "caf\u00e9 \u4e2d\u6587" + plugin._origin_line("20261013_230248_6a7bf5"))
        self.assertFalse(Path(captured["path"]).exists())
        self.assertGreater(captured["timeout"], 120)
        with mock.patch.object(plugin, "_config", return_value=config), \
                mock.patch.object(plugin.subprocess, "run", side_effect=fake_run):
            plugin._handler("crosstalk_create")({"app": "hermes", "prompt": "hi", "model": "m", "provider": "p"},
                                                session_id="20261013_230248_6a7bf5")
        self.assertEqual(captured["argv"][4:10], ["create-hermes", "--model", "m", "--provider", "p",
                                                 "--wait-seconds"])

    def test_hermes_plugin_create_refuses_bad_arguments_and_timeout_is_uncertain(self):
        config = {"python": "C:/rt/python.exe", "cli": "C:/kit/crosstalk_p0_cli.py"}
        with mock.patch.object(plugin, "_config", return_value=config), \
                mock.patch.object(plugin.subprocess, "run") as run:
            out = json.loads(plugin._handler("crosstalk_create")({"app": "claude", "prompt": "x"},
                                                                 session_id="20261013_230248_6a7bf5"))
            self.assertEqual((out["state"], out["created"]), ("refused", False))
            run.assert_not_called()
            run.side_effect = plugin.subprocess.TimeoutExpired("x", 1)
            out = json.loads(plugin._handler("crosstalk_create")({"app": "hermes", "prompt": "x"},
                                                                 session_id="20261013_230248_6a7bf5"))
        self.assertEqual((out["state"], out["created"]), ("uncertain", None))

    def test_hermes_list_argv_carries_filters(self):
        config = {"python": "C:/rt/python.exe", "cli": "C:/kit/crosstalk_p0_cli.py"}
        argv = plugin.build_argv(config, "crosstalk_list", "S", {"query": "q", "app": "codex", "limit": 5})
        self.assertEqual(argv[4:], ["hermes-list", "--session", "S", "--query", "q", "--app", "codex", "--limit", "5"])


    def test_hermes_argv_runs_the_installed_launcher_directly(self):
        config = {"python": "C:/eh/bin/eutonos-crosstalk.exe", "cli": "-"}
        self.assertEqual(plugin.build_argv(config, "crosstalk_status", "S", {}),
                         ["C:/eh/bin/eutonos-crosstalk.exe", "hermes-status", "--session", "S"])
        argv = plugin.build_argv(config, "crosstalk_list", "S", {"query": "q"})
        self.assertEqual(argv, ["C:/eh/bin/eutonos-crosstalk.exe", "hermes-list", "--session", "S", "--query", "q"])
        self.assertEqual(plugin.build_argv({"python": "py", "cli": "k.py"}, "crosstalk_status", "S", {})[:4],
                         ["py", "-I", "-B", "k.py"])


class DescriptionRuleTests(unittest.TestCase):
    RULES = ("exact directory keys", "never titles", "Never resend", "accepted, held or uncertain",
             "Held means queued", "recipient", "exact sender key", "Same-app letters", "report the reason to the owner")

    def check(self, text, label):
        flat = " ".join(text.split())
        for rule in self.RULES:
            self.assertIn(rule, flat, f"{label} lacks: {rule}")

    def test_send_and_create_descriptions_carry_the_core_rules_in_all_three_apps(self):
        names = {t["name"]: t for t in server.TOOLS}
        for tool in ("crosstalk_send", "crosstalk_create"):
            self.check(names[tool]["description"], "codex " + tool)
            self.assertLess(len(names[tool]["description"]), 1500)
        self.check(plugin.TOOL_SEND, "hermes send")
        self.check(plugin.TOOL_CREATE, "hermes create")
        for tool in ("send", "create"):
            found = re.search(r"name: '%s',\s*description: '((?:[^'\\]|\\.)*)'" % tool, CLAUDE_PLUGIN)
            self.assertIsNotNone(found, tool)
            self.check(found.group(1), "claude " + tool)

    def test_same_app_rule_names_the_own_app(self):
        self.assertIn("Codex to Codex", server.TOOLS[2]["description"])
        self.assertIn("Hermes to Hermes", plugin.TOOL_SEND)
        self.assertIn("Claude to Claude", CLAUDE_PLUGIN)

    def test_claude_plugin_registers_four_tools_and_forwards_list_filters(self):
        for tool in ("status", "list", "send", "create"):
            self.assertIn("name: '%s'," % tool, CLAUDE_PLUGIN)
        self.assertIn("mcp__crosstalk-p0__create", CLAUDE_PLUGIN)
        self.assertIn("['query', 'app', 'limit']", CLAUDE_PLUGIN)
        self.assertIn("'claude-create'", CLAUDE_PLUGIN)


if __name__ == "__main__":
    unittest.main()
