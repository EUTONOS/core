"""Crosstalk MVP CLI: owner controls, native readbacks and the Claude plugin helper.

Run with the installed runtime:
  & "$env:USERPROFILE\\.eutonos\\runtime\\Scripts\\python.exe" -I -B crosstalk_p0_cli.py <command>

Every command prints exactly one line of JSON. Nothing here prints an environment
variable value or reads a token or credential file.

Exit status of send-as and hermes-send: 0 accepted / held / arrived (or blocked: nothing was sent, the letter
waits), 2 refused (proven not sent), 3 uncertain (it may have been sent: never resend). claude-send, the plugin
helper, always exits 0 and carries the outcome in its JSON (the plugin reads stdout). Other failures exit 1.

Owner commands : enable | off | halt | resume | deny KEY | allow KEY | status | account [--codex]
                 wake-budget [--per-chat N] [--total N] [--window-s S]   (show or change how often chats may be woken)
                 directory | list | journal [--tail N] | letters | reconcile | mark-stale [--older-than-hours N] | fault ... | readback LETTER
                 codex-wake LETTER [--delay S]   (the detached waiter the core starts; opens no link unless idle)
                 codex-headless LETTER [--cleanup]   (the detached waiter for an asleep Codex chat: resume first)
                 send-as --app A --id ID --to KEY --text-file F | doctor [--versions]
                 create-claude --folder DIR --model M --effort E [--permission-mode auto] [--title T]
                 (--prompt P | --prompt-file F) [--no-show]
                 codex-assign-project --thread ID [--folder DIR]
                 create-codex --folder DIR --model M --effort E [--title T] [--config K=V ...] (--prompt P | --prompt-file F)
                 create-hermes [--model M [--provider P]] [--effort E] [--title T] [--wait-seconds N]
                 (--prompt P | --prompt-file F)   (needs Hermes Desktop running; no model = Hermes default)
Plugin helper  : claude-register | claude-status | claude-list | claude-create | claude-send   (JSON on stdin)
                 claude-mail-slot | claude-notices   (notices of the Crosstalk helper)
                 claude-relink-claim | claude-relink-done | claude-relink-guard   (the restart relink: no model turn)
                 claude-self-relink | claude-self-relink-done   (a letter-woken chat switches its own Remote Control on)
                 claude-relay-claim | claude-relay-done | claude-relay-ticket   (retired stubs: they hand out nothing;
                 kept one version so plugins from before the relay removal still run)
Hermes plugin  : hermes-status --session ID | hermes-list --session ID | hermes-send --session ID --to KEY --text-file F
Hermes link    : hermes-selftest [--wait-seconds N]   (proves the plugin works without sending a letter)
                 hermes-link-status | hermes-repair [--mode ask|auto|off] [--start claude|codex [--model M] [--effort E]]
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

HERE = str(Path(__file__).resolve().parent)
if HERE not in sys.path:  # python -I drops the script directory from sys.path
    sys.path.insert(0, HERE)

import crosstalk_p0_core as core  # noqa: E402

TAG = "crosstalk.p0.cli.v1"
MAX_STDIN = 16384


def out(value: dict, code: int = 0) -> int:
    value = dict(value)
    value.setdefault("tag", TAG)
    sys.stdout.write(json.dumps(value, sort_keys=True) + "\n")
    sys.stdout.flush()
    return code


def read_text_file(path: str) -> str:
    """A letter or prompt file as UTF-8; a BOM (Notepad writes one) is dropped, never sent in the body."""
    return Path(path).read_text(encoding="utf-8-sig")


def send_exit_code(result: dict) -> int:
    """Exit status of an owner/Hermes send: 0 accepted/arrived (and blocked: nothing sent, the letter waits),
    2 refused (proven not sent), 3 uncertain (may have been sent: never resend), 1 anything unrecognized."""
    state = str(result.get("state"))
    if state in ("accepted", "held", "arrived", "replied") or state.startswith("blocked"):
        return 0
    if state == "refused":
        return 2
    if state == "uncertain":
        return 3
    return 1


def read_stdin_json() -> dict:
    raw = sys.stdin.buffer.read(MAX_STDIN + 1)
    if len(raw) > MAX_STDIN:
        raise core.ProbeError("stdin-too-large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise core.ProbeError("stdin-not-json")
    if not isinstance(value, dict):
        raise core.ProbeError("stdin-not-object")
    return value


# ------------------------------------------------------------- owner commands
def cmd_status(args) -> int:
    reconcile = core.reconcile(resend=False)
    claude = core.current_account()
    value = {"ok": True, "device_folder": str(core.device_dir()), "device": core.probe_device_write(),
             "controls": core.controls_load(), "wake_budget": core.wake_budget_status(),
             "claude_account": claude, "account_check": core.account_check(),
             "claude_desktop_account_folders": core.claude_desktop_account_folders(),
             "directory_keys": sorted(core.directory_load()["entries"]), "letters": core.letter_summary(10),
             "fault": core._read_json(core.device_dir() / "fault.json", {}),
             "reconcile_readback": reconcile, "helper": core.helper_summary()}
    try:
        import crosstalk_p0_install as inst
        value["permission_rules"] = inst.permission_rules_status()
    except Exception as error:  # status never fails on the installer module
        value["permission_rules"] = {"error": type(error).__name__}
    return out(value)


def cmd_account_login(args) -> int:
    core.enable_real_focus()  # the box, the sign-in console and the browser page come to the front
    return out(core.account_login_prompt())


def cmd_account(args) -> int:
    value = {"ok": True, "claude": core.claude_account(),
             "claude_desktop_account_folders": core.claude_desktop_account_folders()}
    if args.codex:
        value["codex"] = core.codex_account(use_cache=False)
    return out(value)


def cmd_directory(args) -> int:
    return out({"ok": True, "directory": core.directory_load()})


def cmd_list(args) -> int:
    return out({"ok": True, "entries": core.list_directory(None), "controls": core.controls_load()})


def cmd_journal(args) -> int:
    rows = core.journal_rows()
    return out({"ok": True, "total": len(rows), "rows": rows[-args.tail:] if args.tail else rows})


def cmd_letters(args) -> int:
    letters = core.letters_load()
    return out({"ok": True, "letters": {k: {f: v.get(f) for f in
                ("state", "to", "from", "route", "reason", "arrivals", "readback_note", "via", "history", "attempt_id")}
                for k, v in letters.items()}})


def cmd_controls(args) -> int:
    if args.command == "enable":
        value = core.controls_update(messaging="on", halt=False)
    elif args.command == "off":
        value = core.controls_update(messaging="off")
    elif args.command == "halt":
        value = core.controls_update(halt=True)
    elif args.command == "resume":
        value = core.controls_update(halt=False)
    elif args.command == "deny":
        value = core.controls_update(deny_add=args.key)
    else:
        value = core.controls_update(deny_remove=args.key)
    return out({"ok": True, "controls": value})


def cmd_wake_budget(args) -> int:
    changes = {name: value for name, value in (("per_chat", args.per_chat), ("total", args.total),
                                               ("window_s", args.window_s)) if value is not None}
    if changes:
        core.controls_update(wake_budget=changes)
    return out({"ok": True, "wake_budget": core.wake_budget_status()})


def cmd_reconcile(args) -> int:
    report = core.reconcile(resend=not args.no_resend, grace_s=args.grace, stale_s=args.stale)
    return out({"ok": True, "report": report, "letters": core.letter_summary(10)})


def cmd_mark_stale(args) -> int:
    return out({"ok": True, "result": core.mark_stale(args.older_than_hours), "letters": core.letter_summary(10)})


def cmd_fault(args) -> int:
    if args.action == "set":
        value = core.fault_set(args.point, args.count)
    elif args.action == "clear":
        core.fault_clear()
        value = {}
    else:
        value = core._read_json(core.device_dir() / "fault.json", {})
    return out({"ok": True, "fault": value, "points": list(core.FAULT_POINTS), "exit_code_on_fault": core.FAULT_EXIT})


def cmd_readback(args) -> int:
    letter = core.letters_load().get(args.letter)
    if letter is None:
        return out({"ok": False, "reason": "no-such-letter"}, 1)
    count, note = core.arrivals(letter)
    return out({"ok": True, "letter_id": args.letter, "state": letter.get("state"), "arrivals": count,
                "note": note, "recipient": letter.get("to")})


def cmd_codex_wake(args) -> int:
    """The detached waiter that crosstalk_p0_core starts for an accepted Codex letter (see codex_wake)."""
    core.enable_real_focus()
    return out(core.codex_wake(args.letter, delay_s=args.delay))


def cmd_codex_headless(args) -> int:
    """The detached waiter for a letter to an asleep Codex chat (resume first), or its bookkeeping-only pass."""
    if args.cleanup:
        return out(core.codex_headless_cleanup(args.letter))
    return out(core.codex_headless_wake(args.letter))


def cmd_send_as(args) -> int:
    """Owner-run stand-in for a chat's tool call, so probes 5 and 6 can be repeated at will.

    This takes a caller ID because it is the OWNER's console, not an agent tool."""
    text = read_text_file(args.text_file)
    result = core.send_letter(args.app, args.id, args.to, text)
    return out(result, send_exit_code(result))


def cmd_create_claude(args) -> int:
    if bool(args.prompt) == bool(args.prompt_file):
        return out({"ok": False, "reason": "give exactly one of --prompt or --prompt-file"}, 2)
    prompt = args.prompt if args.prompt else read_text_file(args.prompt_file)
    if args.show:
        core.enable_real_focus()  # only --show opens a window
    return out(core.create_claude(args.folder, args.model, args.effort, args.permission_mode, prompt,
                                  title=args.title, show=args.show))


def cmd_create_codex(args) -> int:
    if bool(args.prompt) == bool(args.prompt_file):
        return out({"ok": False, "reason": "give exactly one of --prompt or --prompt-file"}, 2)
    prompt = args.prompt if args.prompt else read_text_file(args.prompt_file)
    config = {}
    for item in args.config or []:
        key, sep, raw = item.partition("=")
        if not sep:
            return out({"ok": False, "reason": "--config takes KEY=VALUE"}, 2)
        if raw.strip().startswith("["):
            try:
                config[key.strip()] = json.loads(raw)
            except ValueError:
                return out({"ok": False, "reason": "--config list value must be a JSON array"}, 2)
            continue
        config[key.strip()] = {"true": True, "false": False}.get(raw.strip().lower(),
                                                                int(raw) if re.fullmatch(r"-?\d+", raw.strip()) else raw)
    extra = {"config": config} if config else {}
    return out(core.create_codex(args.folder, args.model, args.effort, prompt, title=args.title, **extra))


def cmd_codex_assign_project(args) -> int:
    result = core.codex_assign_project(args.thread, args.folder)
    return out(result, 0 if result.get("ok") else 1)


def cmd_codex_create_worker(args) -> int:
    if args.status:
        return out(core.create_codex_status(args.operation))
    return out(core.codex_create_worker(args.operation))


def cmd_create_hermes(args) -> int:
    if bool(args.prompt) == bool(args.prompt_file):
        return out({"ok": False, "reason": "give exactly one of --prompt or --prompt-file"}, 2)
    prompt = args.prompt if args.prompt else Path(args.prompt_file).read_text(encoding="utf-8")
    return out(core.create_hermes(prompt, title=args.title, model=args.model, provider=args.provider,
                                  effort=args.effort, wait_s=args.wait_seconds))


def cmd_doctor(args) -> int:
    claude = core.claude_exe()
    codex = core.codex_exe()
    value = {"ok": True,
             "python": {"isolated": bool(sys.flags.isolated), "version": sys.version.split()[0],
                        "runtime_in_use": core.eutonos_home() in Path(sys.executable).parents},
             "device": core.probe_device_write(), "claude_cli": claude, "codex_cli": codex,
             "sender_plugin_present": (core.SENDER_PLUGIN / "hooks" / "register.mjs").is_file(),
             "claude_account": core.current_account(), "controls": core.controls_load()}
    # Config readbacks are text-only checks of the two files install.ps1 edits; no credential file is opened.
    try:
        import crosstalk_p0_install as inst
        value["installed"] = inst.verify_installed()
    except Exception as error:  # doctor never fails on a missing helper
        value["installed"] = {"error": type(error).__name__}
    if args.versions:
        versions = {}
        for name, exe in (("claude", claude), ("codex", codex)):
            if exe:
                try:
                    done = subprocess.run(core._exe_argv(exe) + ["--version"], stdin=subprocess.DEVNULL,
                                          capture_output=True, text=True,
                                          timeout=30, env=core.child_env()[0], creationflags=core.NO_WINDOW)
                    versions[name] = (done.stdout or done.stderr).strip()[:80]
                except (OSError, subprocess.TimeoutExpired) as error:
                    versions[name] = "error:" + type(error).__name__
        value["versions"] = versions
    return out(value)


# ---------------------------------------------------------- plugin helper
def _facts(payload: dict) -> dict:
    facts = {key: payload.get(key) for key in ("sessionId", "cwd", "root", "repo", "surfaces", "version",
                                              "startEvent", "toolUseId")}
    if not isinstance(facts["sessionId"], str):
        raise core.ProbeError("invalid-session-id")
    return facts


def cmd_claude_register(args) -> int:
    entry = core.register_claude(_facts(read_stdin_json()), "session.start")
    return out({"ok": True, "key": entry["key"], "registrations": entry["registrations"],
                "account_id": (entry.get("account") or {}).get("id")})


def cmd_claude_status(args) -> int:
    payload = read_stdin_json()
    facts = _facts(payload)
    entry = core.register_claude(facts, "tool-call")
    reconcile = core.reconcile(resend=False)
    return out({"ok": True, "caller": {"session_id": facts["sessionId"], "cwd": facts["cwd"],
                                       "tool_use_id": facts["toolUseId"], "version": facts["version"],
                                       "surfaces": facts["surfaces"]},
                "registered": {"key": entry["key"], "registrations": entry["registrations"],
                               "project": entry.get("project"), "native_registration": entry.get("native_registration")},
                "account": entry.get("account"), "account_check": core.account_check(),
                "device": core.probe_device_write(), "device_folder": str(core.device_dir()), "controls": core.controls_load(),
                "letters": core.letter_summary(5), "helper": core.helper_summary(),
                "reconcile_readback": {k: reconcile[k] for k in
                                       ("checked", "readback_checked", "readback_deferred", "promoted")}})


def cmd_claude_list(args) -> int:
    payload = read_stdin_json()
    session_id = payload.get("sessionId")
    key = f"claude:{session_id}" if isinstance(session_id, str) else None
    extra = sorted(set(payload) - {"sessionId", "query", "app", "limit"})
    if extra:
        return out({"ok": False, "reason": "unexpected-fields:" + core.clip_reason(",".join(extra))}, 0)
    return out(core.chat_list_view(key, payload.get("query"), payload.get("app"), payload.get("limit")))


def cmd_claude_create(args) -> int:
    """The Claude plugin's create tool: the calling chat's folder is the default; never retried."""
    payload = read_stdin_json()
    session_id = payload.get("sessionId")
    params = {k: v for k, v in payload.items() if k != "sessionId" and v is not None}
    key = f"claude:{session_id}" if isinstance(session_id, str) and core.UUID_RE.fullmatch(session_id) else None
    app = params.pop("app", None)
    return out(core.chat_create(app, params, key, wait_s=90.0))


def cmd_claude_send(args) -> int:
    payload = read_stdin_json()
    extra = sorted(set(payload) - {"caller", "to", "text", "turn"})
    if extra:
        return out({"ok": False, "state": "refused", "sent": False, "reason": "unexpected-fields:" + core.clip_reason(",".join(extra))}, 0)
    caller = payload.get("caller")
    if not isinstance(caller, str) or not core.UUID_RE.fullmatch(caller):
        return out({"ok": False, "state": "refused", "sent": False, "reason": "invalid-caller"}, 0)
    core.directory_register("claude", caller, {"source": "tool-call"}, None)
    result = core.send_letter("claude", caller, payload.get("to"), payload.get("text"),
                              from_turn=payload.get("turn") if isinstance(payload.get("turn"), str) else None)
    return out(result)


# ------------------------------------------------------- Hermes plugin tools
def _hermes_session(args) -> str:
    session = args.session
    if not isinstance(session, str) or not core.HERMES_ID_RE.fullmatch(session):
        raise core.ProbeError("invalid-session-id")
    return session


def cmd_hermes_pre_register(args) -> int:
    """Internal: the create-hermes connector calls this before a new chat's first prompt (creator title)."""
    entry = core.register_hermes_pending(_hermes_session(args), args.title)
    return out({"ok": True, "key": entry["key"]})


def cmd_hermes_status(args) -> int:
    session = _hermes_session(args)
    entry = core.register_hermes(session, "tool-call")
    reconcile = core.reconcile(resend=False)
    beat = core.hermes_heartbeat() or {}
    return out({"ok": True, "caller": {"session_id": session},
                "registered": {"key": entry["key"], "title": entry.get("title"),
                               "registrations": entry["registrations"]},
                "hermes": {"home": str(core.hermes_home()), "listener_alive": core.hermes_listener_alive(),
                           "heartbeat_utc": beat.get("utc"), "plugin_version": beat.get("plugin_version"),
                           "account": "none: Hermes is one local install",
                           "link": core.hermes_link_diagnosis()},
                "device": core.probe_device_write(), "device_folder": str(core.device_dir()),
                "controls": core.controls_load(), "letters": core.letter_summary(5),
                "reconcile_readback": {k: reconcile[k] for k in
                                       ("checked", "readback_checked", "readback_deferred", "promoted")}})


def cmd_hermes_selftest(args) -> int:
    """Prove the Hermes link without sending a letter. Exit 0 pass, 1 failed, 2 inconclusive (Hermes not up yet)."""
    result = core.hermes_selftest(args.wait_seconds)
    return out(result, 2 if result["inconclusive"] else 0 if result["ok"] else 1)


def cmd_hermes_link_status(args) -> int:
    return out(dict(core.hermes_link_summary(), ok=True))


def cmd_hermes_repair(args) -> int:
    """Show the repair offer, set the offer mode, or start the repair chat (only when the owner asks)."""
    if args.mode:
        return out(core.hermes_repair_set_mode(args.mode))
    if args.start:
        return out(core.hermes_repair_start(args.start, args.model, args.effort, show=not args.no_show))
    link = core.hermes_link_load()
    return out({"ok": True, "repair": link.get("repair") or {}, "installed_harnesses": core.installed_harnesses(),
                "mode": core.link_repair_mode(), "link_file": str(core.hermes_link_path())})


def cmd_hermes_list(args) -> int:
    session = _hermes_session(args)
    key = f"hermes:{session}"
    return out(core.chat_list_view(key, args.query, args.app, args.limit))


def cmd_hermes_send(args) -> int:
    """A Hermes chat's crosstalk_send: the text comes from a UTF-8 file, never shell-quoted."""
    session = _hermes_session(args)
    text = read_text_file(args.text_file)
    core.register_hermes(session, "tool-call")
    result = core.send_letter("hermes", session, args.to, text)
    return out(result, send_exit_code(result))


def cmd_claude_relay_retired(args) -> int:
    """Retired relay commands (plugins older than 0.0.12 still call them): hand out nothing, change nothing."""
    read_stdin_json()
    return out({"ok": True, "job": None, "ticket": None, "reason": "relay-retired"})


def cmd_claude_notices(args) -> int:
    """Open helper notices for a chat to show at session start. Read-only."""
    read_stdin_json()
    return out({"ok": True, "notices": [{"key": n["key"], "kind": n["kind"], "message": n["message"]}
                                        for n in core.helper_notices_active()[:5]]})


def cmd_claude_mail_slot(args) -> int:
    return out(core.mail_slot_pull(read_stdin_json().get("sessionId")))


def cmd_claude_relink_claim(args) -> int:
    """A running Desktop chat's plugin claims the restart relink list (see core.relink_claim)."""
    return out(core.relink_claim(read_stdin_json().get("sessionId")))


def cmd_claude_relink_done(args) -> int:
    payload = read_stdin_json()
    return out(core.relink_done(payload.get("sessionId"), payload.get("to"), payload.get("attempt_id"),
                                payload.get("delivered"), payload.get("reason")))


def cmd_claude_relink_guard(args) -> int:
    """The plugin's guard hook: may this call of set_remote_control / send_message go ahead? Read-only."""
    payload = read_stdin_json()
    return out(core.relink_guard(payload.get("sessionId"), payload.get("tool"), payload.get("args")))


def cmd_claude_self_relink(args) -> int:
    return out(core.self_relink_ask(read_stdin_json().get("sessionId")))


def cmd_claude_self_relink_done(args) -> int:
    payload = read_stdin_json()
    return out(core.self_relink_done(payload.get("sessionId"), payload.get("attempt_id"), payload.get("outcome"),
                                     payload.get("reason")))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crosstalk_p0_cli", description="Crosstalk MVP controls and readback CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("enable", "off", "halt", "resume"):
        sub.add_parser(name).set_defaults(func=cmd_controls)
    for name in ("deny", "allow"):
        item = sub.add_parser(name)
        item.add_argument("key", help="exact directory key, e.g. claude:<session-id>")
        item.set_defaults(func=cmd_controls)
    budget = sub.add_parser("wake-budget", help="show or change the wake budget (wakes per chat and in total per window)")
    budget.add_argument("--per-chat", type=int, help="wakes per chat per window (default 6)")
    budget.add_argument("--total", type=int, help="wakes in total per window (default 20)")
    budget.add_argument("--window-s", type=int, help="window in seconds (default 3600)")
    budget.set_defaults(func=cmd_wake_budget)
    sub.add_parser("status").set_defaults(func=cmd_status)
    account = sub.add_parser("account")
    account.add_argument("--codex", action="store_true", help="also ask the Codex app-server account/read")
    account.set_defaults(func=cmd_account)
    sub.add_parser("account-login", help="on an account mismatch, ask whether to log the Claude CLI in to the Desktop "
                   "account; Yes opens `claude auth login`").set_defaults(func=cmd_account_login)
    sub.add_parser("directory").set_defaults(func=cmd_directory)
    sub.add_parser("list").set_defaults(func=cmd_list)
    journal = sub.add_parser("journal")
    journal.add_argument("--tail", type=int, default=20, help="0 for all rows")
    journal.set_defaults(func=cmd_journal)
    sub.add_parser("letters").set_defaults(func=cmd_letters)
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("--no-resend", action="store_true", help="readback only (what a tool call does)")
    reconcile.add_argument("--grace", type=float, default=core.JOURNALED_GRACE_S)
    reconcile.add_argument("--stale", type=float, default=core.STALE_SENDING_S)
    reconcile.set_defaults(func=cmd_reconcile)
    stale = sub.add_parser("mark-stale", help="retire old unconfirmed letters without resending them")
    stale.add_argument("--older-than-hours", type=float, default=24.0)
    stale.set_defaults(func=cmd_mark_stale)
    fault = sub.add_parser("fault")
    fault.add_argument("action", choices=("set", "clear", "show"))
    fault.add_argument("point", nargs="?", choices=core.FAULT_POINTS)
    fault.add_argument("--count", type=int, default=1)
    fault.set_defaults(func=cmd_fault)
    readback = sub.add_parser("readback")
    readback.add_argument("letter")
    readback.set_defaults(func=cmd_readback)
    wake = sub.add_parser("codex-wake", help="detached waiter for one accepted Codex letter (started by the core)")
    wake.add_argument("letter")
    wake.add_argument("--delay", type=float, default=core.CODEX_WAKE_DELAY_S)
    wake.set_defaults(func=cmd_codex_wake)
    headless = sub.add_parser("codex-headless", help="detached waiter for a letter to an asleep Codex chat "
                                                     "(started by the core); --cleanup only does bookkeeping")
    headless.add_argument("letter")
    headless.add_argument("--cleanup", action="store_true")
    headless.set_defaults(func=cmd_codex_headless)
    send_as = sub.add_parser("send-as")
    send_as.add_argument("--app", required=True, choices=("claude", "codex", "hermes"))
    send_as.add_argument("--id", required=True)
    send_as.add_argument("--to", required=True)
    send_as.add_argument("--text-file", required=True)
    send_as.set_defaults(func=cmd_send_as)
    create = sub.add_parser("create-claude", help="create a Claude chat through the claude CLI and register it")
    create.add_argument("--folder", required=True, help="absolute existing project folder (the chat's cwd)")
    create.add_argument("--model", required=True)
    create.add_argument("--effort", required=True, choices=core.EFFORTS)
    create.add_argument("--permission-mode", default="auto")
    create.add_argument("--title")
    create.add_argument("--prompt")
    create.add_argument("--prompt-file")
    create.add_argument("--show", action=argparse.BooleanOptionalAction, default=True,
                        help="open it in Claude Desktop with claude --desktop --resume (default on)")
    create.set_defaults(func=cmd_create_claude)
    create_codex = sub.add_parser("create-codex", help="create a Codex chat through a fresh codex app-server "
                                                       "and register it (one thread, never retried)")
    create_codex.add_argument("--folder", required=True, help="absolute existing project folder (the chat's cwd)")
    create_codex.add_argument("--model", required=True)
    create_codex.add_argument("--effort", required=True, choices=core.EFFORTS)
    create_codex.add_argument("--title")
    create_codex.add_argument("--prompt")
    create_codex.add_argument("--prompt-file")
    create_codex.add_argument("--config", action="append", metavar="KEY=VALUE",
                              help="extra thread config override, repeatable (e.g. memories.use_memories=false)")
    create_codex.set_defaults(func=cmd_create_codex)
    assign = sub.add_parser("codex-assign-project", help="assign one existing Codex thread to the app-server "
                                                         "project whose root is the folder (never creates a project)")
    assign.add_argument("--thread", required=True)
    assign.add_argument("--folder", help="default: the thread's directory project_folder")
    assign.set_defaults(func=cmd_codex_assign_project)
    codex_creator = sub.add_parser("codex-create-worker", help="internal detached creator; --status reads its receipt")
    codex_creator.add_argument("operation")
    codex_creator.add_argument("--status", action="store_true")
    codex_creator.set_defaults(func=cmd_codex_create_worker)
    codex_status = sub.add_parser("create-codex-status", help="read a Codex creation operation without restarting it")
    codex_status.add_argument("operation")
    codex_status.set_defaults(func=cmd_codex_create_worker, status=True)
    create_hermes = sub.add_parser("create-hermes", help="create a Hermes Desktop chat through the running Hermes "
                                                         "backend and register it (one session, never retried)")
    create_hermes.add_argument("--model", help="omit to use Hermes' configured default model")
    create_hermes.add_argument("--provider", help="only together with --model")
    create_hermes.add_argument("--effort", choices=core.HERMES_EFFORTS, help="thinking level; omit for Hermes' default")
    create_hermes.add_argument("--title")
    create_hermes.add_argument("--prompt")
    create_hermes.add_argument("--prompt-file")
    create_hermes.add_argument("--wait-seconds", type=float, default=core.CREATE_HERMES_DEFAULT_WAIT_S,
                               help="how long to stay connected for the first turn (10-3600); the turn keeps "
                                    "running afterwards")
    create_hermes.set_defaults(func=cmd_create_hermes)
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--versions", action="store_true")
    doctor.set_defaults(func=cmd_doctor)
    pre = sub.add_parser("hermes-pre-register", help="internal: create-hermes registers the creator title early")
    pre.add_argument("--session", required=True)
    pre.add_argument("--title", required=True)
    pre.set_defaults(func=cmd_hermes_pre_register)
    for name, func in (("hermes-status", cmd_hermes_status), ("hermes-list", cmd_hermes_list)):
        item = sub.add_parser(name, help="Hermes plugin tool (slice 3B.12)")
        item.add_argument("--session", required=True, help="the calling Hermes chat's id (YYYYMMDD_HHMMSS_xxxxxx)")
        if name == "hermes-list":
            item.add_argument("--query", help="case-insensitive substring of title, folder or key")
            item.add_argument("--app", choices=("claude", "codex", "hermes"))
            item.add_argument("--limit", type=int, help=f"rows to show (default {core.LIST_DEFAULT_LIMIT})")
        item.set_defaults(func=func)
    selftest = sub.add_parser("hermes-selftest", help="prove the Hermes link works without sending a letter")
    selftest.add_argument("--wait-seconds", type=float, default=core.HERMES_PROBE_WAIT_S)
    selftest.set_defaults(func=cmd_hermes_selftest)
    sub.add_parser("hermes-link-status", help="the Hermes link diagnosis and version check").set_defaults(
        func=cmd_hermes_link_status)
    repair = sub.add_parser("hermes-repair", help="show or act on the Hermes link repair offer")
    repair.add_argument("--mode", choices=core.LINK_REPAIR_MODES, help="ask (default), auto or off")
    repair.add_argument("--start", choices=("claude", "codex"), help="start a repair chat in that installed app")
    repair.add_argument("--model")
    repair.add_argument("--effort")
    repair.add_argument("--no-show", action="store_true")
    repair.set_defaults(func=cmd_hermes_repair)
    hermes_send = sub.add_parser("hermes-send", help="Hermes plugin tool: send one letter as that Hermes chat")
    hermes_send.add_argument("--session", required=True)
    hermes_send.add_argument("--to", required=True)
    hermes_send.add_argument("--text-file", required=True)
    hermes_send.set_defaults(func=cmd_hermes_send)
    for name, func in (("claude-register", cmd_claude_register), ("claude-status", cmd_claude_status),
                       ("claude-list", cmd_claude_list), ("claude-send", cmd_claude_send),
                       ("claude-create", cmd_claude_create),
                       ("claude-mail-slot", cmd_claude_mail_slot), ("claude-notices", cmd_claude_notices),
                       ("claude-relink-claim", cmd_claude_relink_claim), ("claude-relink-done", cmd_claude_relink_done),
                       ("claude-relink-guard", cmd_claude_relink_guard),
                       ("claude-self-relink", cmd_claude_self_relink),
                       ("claude-self-relink-done", cmd_claude_self_relink_done),
                       ("claude-relay-claim", cmd_claude_relay_retired), ("claude-relay-done", cmd_claude_relay_retired),
                       ("claude-relay-ticket", cmd_claude_relay_retired)):
        sub.add_parser(name).set_defaults(func=func)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    with contextlib.suppress(Exception):
        core.remember_desktop_account()  # a command run from a Desktop chat names that account's email
    if args.command == "fault" and args.action == "set" and not args.point:
        return out({"ok": False, "reason": "fault set needs a point", "points": list(core.FAULT_POINTS)}, 2)
    try:
        return args.func(args)
    except core.ProbeError as error:
        failure = {"ok": False, "reason": error.reason, "detail": error.detail}
        if getattr(error, "thread_id", None):
            failure["thread_id"] = error.thread_id  # a create-codex failure after the thread exists
        if getattr(error, "operation_id", None):
            failure["operation_id"] = error.operation_id
        if getattr(error, "stored_session_id", None):
            failure["stored_session_id"] = error.stored_session_id  # a create-hermes failure after the chat exists
        return out(failure, 1)
    except Exception as error:
        return out({"ok": False, "reason": "cli-exception", "error_type": type(error).__name__}, 1)


if __name__ == "__main__":
    sys.exit(main())
