"""Crosstalk MVP MCP tool server for Codex (stdio, stdlib only).

Run as:  %USERPROFILE%\\.eutonos\\runtime\\Scripts\\python.exe -I -B codex_tool_server.py

Tools (no argument names a caller, path, root or command; extra arguments are
refused with JSON-RPC -32602):
  crosstalk_status()          records the native caller metadata Codex sent, the server cwd and
                              whether the device folder is writable; journals one row and
                              registers the caller in directory.json
  crosstalk_list(query?, app?, limit?)  compact directory, newest first, capped, self marked
  crosstalk_send(to, text)    letter to an exact Claude or Hermes chat key from the directory
  crosstalk_create(app, prompt, folder?, model?, effort?, title?, provider?)  open a new chat

P0 tool names remain compatible. Messaging is OFF until the owner runs `crosstalk_p0_cli.py enable`.
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys
import threading
import traceback

HERE = str(Path(__file__).resolve().parent)
if HERE not in sys.path:  # python -I drops the script directory from sys.path
    sys.path.insert(0, HERE)

import crosstalk_p0_core as core  # noqa: E402

TAG = "crosstalk.p0.codex-server.v1"
SERVER_NAME = "crosstalk_p0"
DEFAULT_PROTOCOL = "2025-06-18"
OUT_LOCK = threading.Lock()

TOOLS = [
    {"name": "crosstalk_status",
     "title": "Crosstalk status",
     "description": "Records this chat's native caller metadata, the tool server's working "
                    "directory and whether the device folder is writable, registers this chat in the chat "
                    "directory, checks pending native arrivals and reports held letters. Takes no arguments. "
                    "Do not call it in a sleep loop to wait for a reply: replies to this chat queue until your "
                    "turn ends.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                     "openWorldHint": False}},
    {"name": "crosstalk_list",
     "title": "Crosstalk directory",
     "description": "Lists registered chats, newest first, compact (key, app, title, folder, hint; self marked). "
                    "Capped (default 40; 'more' counts the rest): narrow with query (case-insensitive text in title, "
                    "folder or key) and app. Titles are display-only. Read-only.",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string", "description": "Case-insensitive text to find in title, folder or key"},
                                    "app": {"type": "string", "enum": ["claude", "codex", "hermes"],
                                            "description": "Only chats of this app"},
                                    "limit": {"type": "integer", "minimum": 1, "maximum": 100,
                                              "description": "Rows to show (default 40)"}},
                     "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "crosstalk_send",
     "title": "Crosstalk send",
     "description": "Send one letter to an exact Claude or Hermes chat from the directory ('to' is its key, "
                    "claude:<id> or hermes:<id>). The sender is always this chat. Pick a chat whose hint is "
                    "'sendable' or 'sendable:wakes-chat' (a stopped chat that gets started; that runs a turn there). "
                    "Arrived means a native input was verified; held means the transport accepted it but native "
                    "arrival is unverified. blocked:not-running waits in the mail slot of the recipient and arrives "
                    "when that chat next opens; blocked:other-account is final. Replies come back as new messages "
                    "here and Codex holds them as 'Queued' until your turn ends: after sending a letter that needs "
                    "an answer, finish your turn; never sleep or poll status to wait. Rules: use exact directory keys, never titles. Never resend a letter that is accepted, held or uncertain; check crosstalk_status instead. Held means queued and may land when the recipient's turn ends. Reply to the exact sender key shown in the letter. Same-app letters (Codex to Codex) are refused; use the native Codex tools. On a refusal, report the reason to the owner.",
     "inputSchema": {"type": "object",
                     "properties": {"to": {"type": "string", "description": "Exact directory key claude:<id> or hermes:<id>"},
                                    "text": {"type": "string", "description": "Letter text (max 3000 characters)"}},
                     "required": ["to", "text"], "additionalProperties": False},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                     "openWorldHint": True}},
    {"name": "crosstalk_create",
     "title": "Crosstalk create chat",
     "description": "Open a NEW Claude, Codex or Hermes chat with a first prompt and register it. Returns its "
                    "directory key, or state 'started' if it is still being created (never call create again for "
                    "that; check crosstalk_list/crosstalk_status). claude and codex need folder (absolute, existing; "
                    "defaults to the own folder of this chat), model and effort; hermes takes optional model, "
                    "provider and effort. Respects Crosstalk controls (off, halt, deny). Rules: use exact directory keys, never titles. Never resend a letter that is accepted, held or uncertain; check crosstalk_status instead. Held means queued and may land when the recipient's turn ends. Reply to the exact sender key shown in the letter. Same-app letters (Codex to Codex) are refused; use the native Codex tools. On a refusal, report the reason to the owner.",
     "inputSchema": {"type": "object",
                     "properties": {"app": {"type": "string", "enum": ["claude", "codex", "hermes"]},
                                    "folder": {"type": "string", "description": "Absolute existing project folder (claude, codex); default: own folder of this chat"},
                                    "model": {"type": "string", "description": "Model name (required for claude and codex)"},
                                    "effort": {"type": "string", "description": "Effort: low, medium, high, xhigh, max (hermes also none, minimal, ultra)"},
                                    "title": {"type": "string", "description": "Chat title (max 80 characters)"},
                                    "prompt": {"type": "string", "description": "First prompt for the new chat"},
                                    "provider": {"type": "string", "description": "Hermes only, together with model"}},
                     "required": ["app", "prompt"], "additionalProperties": False},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                     "openWorldHint": True}},
]
ALLOWED_ARGS = {t["name"]: set(t["inputSchema"]["properties"]) for t in TOOLS}


def write_message(message: dict) -> None:
    data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
    with OUT_LOCK:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def rpc_error(request_id, code: int, message: str) -> None:
    write_message({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def tool_result(value: dict, *, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(value, sort_keys=True)}], "isError": error}


def server_facts() -> dict:
    names = ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "CODEX_HOME", "CODEX_THREAD_ID", "CODEX_SANDBOX",
             "HOME", "PATH", "SystemRoot", "TEMP")
    return {"pid": os.getpid(), "cwd": os.getcwd(), "python_isolated": bool(sys.flags.isolated),
            "python_version": sys.version.split()[0],
            "env_names_present": sorted(n for n in names if os.environ.get(n)),
            "env_names_codex": sorted(n for n in os.environ if n.upper().startswith("CODEX")),
            "env_values_logged": False}


def handle_status(meta) -> dict:
    caller = core.codex_caller(meta)
    server = server_facts()
    device = core.probe_device_write()
    if caller is None:
        core.journal_append({"event": "status", "app": "codex", "caller": None, "server": server,
                             "device": device, "meta_keys": sorted(map(str, meta)) if isinstance(meta, dict) else None})
        return {"ok": False, "tag": TAG, "reason": "no-native-caller",
                "detail": "tools/call carried no usable _meta.threadId; nothing registered",
                "meta_keys": sorted(map(str, meta)) if isinstance(meta, dict) else None,
                "server": server, "device": device}
    try:
        account = core.codex_account(timeout=15.0)
    except Exception as error:  # an account failure must not hide the registration
        account = {"app": "codex", "id": None, "error": type(error).__name__}
    entry = core.register_codex(caller, dict(server, device=device), account)
    reconcile = core.reconcile(resend=False)
    return {"ok": True, "tag": TAG, "caller": caller, "server": server, "device": device,
            "registered": {"key": entry["key"], "registrations": entry["registrations"]},
            "account": account, "controls": core.controls_load(), "letters": core.letter_summary(5),
            "reconcile_readback": {k: reconcile[k] for k in
                                   ("checked", "readback_checked", "readback_deferred", "promoted")},
            "device_folder": str(core.device_dir())}


def handle_list(meta, arguments=None) -> dict:
    arguments = arguments or {}
    caller = core.codex_caller(meta)
    self_key = f"codex:{caller['thread_id']}" if caller else None
    try:
        value = core.chat_list_view(self_key, arguments.get("query"), arguments.get("app"), arguments.get("limit"))
    except core.ProbeError as error:
        return {"ok": False, "tag": TAG, "reason": error.reason, "detail": error.detail}
    value["tag"] = TAG
    return value


CREATE_WAIT_S = 50.0  # Codex tool calls time out around a minute; a longer create answers state "started"


def handle_create(meta, arguments: dict) -> dict:
    caller = core.codex_caller(meta)
    if caller is None:
        return {"ok": False, "tag": TAG, "state": "refused", "created": False, "reason": "no-native-caller"}
    params = {k: v for k, v in arguments.items() if v is not None}
    app = params.pop("app", None)
    result = core.chat_create(app, params, f"codex:{caller['thread_id']}", wait_s=CREATE_WAIT_S)
    result["tag"] = TAG
    return result


def handle_send(meta, arguments: dict) -> dict:
    caller = core.codex_caller(meta)
    if caller is None:
        return {"ok": False, "tag": TAG, "state": "refused", "sent": False, "reason": "no-native-caller"}
    core.directory_register("codex", caller["thread_id"], {"source": "tool-call"}, None)
    result = core.send_letter("codex", caller["thread_id"], arguments.get("to"), arguments.get("text"),
                              from_turn=caller.get("turn_id"))
    result["tag"] = TAG
    return result


CORE_LOCK = threading.Lock()
CORE_STATE = {"in_flight": 0, "mtime_ns": None}


def _core_mtime_ns():
    try:
        return os.stat(core.__file__).st_mtime_ns
    except OSError:
        return None


CORE_STATE["mtime_ns"] = _core_mtime_ns()


def enter_call() -> None:
    """Codex starts this server once per app launch. Reload a changed core when no other call is running,
    so a merged fix reaches Codex sends without an app restart (2026-10-09)."""
    with CORE_LOCK:
        mtime = _core_mtime_ns()
        if CORE_STATE["in_flight"] == 0 and mtime is not None and mtime != CORE_STATE["mtime_ns"]:
            try:
                importlib.reload(core)
                CORE_STATE["mtime_ns"] = mtime
            except Exception:  # keep serving with the loaded core; report names only
                sys.stderr.write("crosstalk_p0 core reload failed: " + traceback.format_exc()[-400:] + "\n")
                sys.stderr.flush()
        CORE_STATE["in_flight"] += 1


def leave_call() -> None:
    with CORE_LOCK:
        CORE_STATE["in_flight"] -= 1


def handle_call(request_id, params: dict) -> None:
    enter_call()
    try:
        dispatch_call(request_id, params)
    finally:
        leave_call()


def dispatch_call(request_id, params: dict) -> None:
    try:
        name = params.get("name")
        if name not in ALLOWED_ARGS:
            return rpc_error(request_id, -32602, "unknown tool")
        arguments = params.get("arguments")
        arguments = {} if arguments is None else arguments
        if not isinstance(arguments, dict):
            return rpc_error(request_id, -32602, "arguments must be an object")
        extra = sorted(set(map(str, arguments)) - ALLOWED_ARGS[name])
        if extra:
            # Names only, never values. Caller IDs, paths and roots are never accepted.
            return rpc_error(request_id, -32602,
                             "refused: caller, path and root arguments are not accepted; unexpected argument(s): "
                             + ", ".join(extra)[:200])
        meta = params.get("_meta")
        if name == "crosstalk_status":
            value = handle_status(meta)
        elif name == "crosstalk_list":
            value = handle_list(meta, arguments)
        elif name == "crosstalk_create":
            value = handle_create(meta, arguments)
        else:
            value = handle_send(meta, arguments)
        write_message({"jsonrpc": "2.0", "id": request_id, "result": tool_result(value)})
    except Exception as error:  # report a reason code, never a traceback with paths
        sys.stderr.write("crosstalk_p0 tool error: " + traceback.format_exc()[-600:] + "\n")
        sys.stderr.flush()
        write_message({"jsonrpc": "2.0", "id": request_id,
                       "result": tool_result({"ok": False, "tag": TAG, "reason": "tool-exception",
                                              "error_type": type(error).__name__}, error=True)})


def main() -> int:
    workers: list[threading.Thread] = []
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            rpc_error(None, -32700, "parse error")
            continue
        if not isinstance(message, dict):
            rpc_error(None, -32600, "invalid request")
            continue
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if request_id is None:
            continue  # notifications (initialized, cancelled, ...): no response
        if method == "initialize":
            requested = params.get("protocolVersion")
            write_message({"jsonrpc": "2.0", "id": request_id, "result": {
                "protocolVersion": requested if isinstance(requested, str) else DEFAULT_PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "title": "Crosstalk", "version": "0.0.5"},
                "instructions": "Tools take no caller or root arguments. Held letters are accepted by transport; never resend."}})
        elif method == "ping":
            write_message({"jsonrpc": "2.0", "id": request_id, "result": {}})
        elif method == "tools/list":
            write_message({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            # A send can take ~45 s (one-shot Claude sender), so never block the read loop.
            worker = threading.Thread(target=handle_call, args=(request_id, params), daemon=True)
            worker.start()
            workers.append(worker)
            workers[:] = [w for w in workers if w.is_alive()]
        else:
            rpc_error(request_id, -32601, "method not found")
    for worker in workers:  # stdin closed: let in-flight calls answer before exit
        worker.join(timeout=120)
    return 0


if __name__ == "__main__":
    sys.exit(main())
