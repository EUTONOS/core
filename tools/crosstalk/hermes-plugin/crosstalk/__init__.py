"""EUTONOS Crosstalk plugin for Hermes Desktop (slice 3B.12). Standard library only.

Tools (they run the installed Crosstalk CLI as a subprocess; this chat is always the sender):
  crosstalk_status()          registers this chat (title from Hermes' state.db), reads back pending arrivals
  crosstalk_list(query?, app?, limit?)  compact chat directory, newest first, capped, with delivery hints
  crosstalk_send(to, text)    one letter to an exact Claude or Codex chat from the directory
  crosstalk_create(app, prompt, ...)  open a new Claude, Codex or Hermes chat (prompt through a temp file)

Inbox listener (a daemon thread in the process that hosts Desktop chats; short CLI runs never start delivering).
Host (0.3.1): the Ink TUI sidecar (injector on the manager) or, since the one-gateway
runtime, the gateway process itself (published gateway injector; the session id is mapped to the gateway route key and
a closed chat is not resumed there):
  <HERMES_HOME>/crosstalk/inbox/<letter>.json  {letter_id, session_key, text}, written atomically by the core
  claims the file by os.replace into work/, checks the text is a framed Crosstalk letter (the same rule as the
  Claude plugin: starts "Crosstalk message from ", ends with a last line "ref CTP0-<12 hex>", and that ref equals letter_id;
  anything else goes to cancelled/ and is never injected), re-reads the device folder's controls.json (messaging
  off, halt, deny list: the same rules as the core's controls_check), then calls
  ctx.inject_message(text, "user", session_key=...)
  and writes done/<letter>.result.json {returned, refused, error, pid, ...}. returned True only means the host
  accepted the message (a busy chat queues it); False means it was not delivered. When the first inject returns
  False (chat not live in the backend), the listener loads the stored chat with the backend's own session.resume
  (detached, drop-sink transport, nothing sent by the load), re-reads controls.json and injects once more; a failed
  load is a proven refusal (result.refused "hermes-resume-..."), an inject exception stays uncertain.
  Busy chat (0.2.0): when the chat's turn is running or it holds a queued prompt, nothing is injected (the host would
  only queue it into one text-merged slot that Stop and /new wipe). The letter is parked in waiting/, an early
  result {deferred: "session-busy", returned: null} tells the core it is accepted and held, and the listener
  injects it once, with a fresh controls check, when the chat is idle (cancelled after 24 h: "hermes-busy-timeout").
  <HERMES_HOME>/crosstalk/heartbeat.json {pid, utc, hosts_chats, host, plugin_version} every 5 s, written only by the
  hosting process; the core treats a stale or missing heartbeat as "Hermes not running". A failing loop pass is
  logged (logging "crosstalk") and counted in the heartbeat (listener_errors, last_error) and in
  <HERMES_HOME>/crosstalk/listener-error.json; the listener keeps running.
Self-diagnosis (0.4.0): Hermes loads this plugin in several processes and only one hosts chats, so every process writes
  <HERMES_HOME>/crosstalk/status/<pid>.json at load and every 30 s: {pid, plugin_version, hermes_version, host (the
  detected host kind or null), probes (which host checks it tried and what each found), loaded_utc, utc}. The core
  judges across all fresh records (Hermes not running / plugin not loaded / plugin loaded but no host found), so a
  Hermes update that moves the internals this plugin uses is named at once instead of looking like "not running".
  A hosting process also answers a self-test probe (<HERMES_HOME>/crosstalk/probe/<id>.json, answer in
  probe-done/<id>.result.json) that proves the inbox loop runs and the route lookup works WITHOUT injecting anything
  into any chat. HERMES_CONTRACT below lists, in one place, every Hermes internal this plugin relies on; the core
  checks that list against the installed Hermes source (local files only).

crosstalk.json beside this file (written by the installer) names the installed runtime python, the CLI and the
device folder. Undo: `hermes plugins disable crosstalk`, delete this folder (see crosstalk_p0_install hermes-remove).
Requires config plugins.entries.crosstalk.allow_gateway_injection: true.
"""

import atexit
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_VERSION = "0.4.0"
WAIT_MAX_S = 24 * 3600.0  # a letter still waiting for a busy chat after this long is cancelled (never injected)
THREAD_NAME = "crosstalk-hermes-inbox"
POLL_S, HEARTBEAT_S = 1.0, 5.0
CLI_TIMEOUT_S = 90
RESUME_AGENT_WAIT_S = 30.0
PRUNE_AFTER_S = 3 * 24 * 3600.0
SESSION_RE = re.compile(r"\d{8}_\d{6}_[0-9a-f]{6}\Z")
LETTER_RE = re.compile(r"CTP0-[0-9a-f]{12}\Z")
LETTER_HEAD = "Crosstalk message from "
LETTER_REF_RE = re.compile(r"\nref (CTP0-[0-9a-f]{12})\Z")
ERROR_LOG_EVERY_S = 60.0
LOG = logging.getLogger("crosstalk")
_ERRORS = {"count": 0, "last": None, "written_at": 0.0, "written_text": None}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TAG = "crosstalk.hermes.plugin.v1"
STATUS_EVERY_S = 30.0
PROBE_MAX_ROUTES = 8
STATUS_PROFILE = "crosstalk.hermes.status.v1"

# Every Hermes internal this plugin (and the create-hermes connector) relies on, in one place. Pure literals: the
# core reads this list with ast.literal_eval (no import) and checks, against the INSTALLED Hermes source, that each
# `file` (relative to the Hermes source root) exists and still contains each `needs` text. A missing entry does not
# prove the link is broken, it says "this Hermes internal changed: look". Keep it to what the code really uses.
HERMES_CONTRACT = (
    {"id": "plugin-api", "uses": "plugin", "file": "hermes_cli/plugins.py",
     "what": "plugin context: register_tool, register_hook and inject_message",
     "needs": ["class PluginContext", "def register_tool", "def register_hook", "def inject_message"]},
    {"id": "gateway-injector", "uses": "plugin", "file": "hermes_cli/plugins.py",
     "what": "the gateway process publishes its message injector to the plugin manager (the one-gateway host)",
     "needs": ["has_gateway_message_injector", "_gateway_message_injector", "def publish_gateway_message_host"]},
    {"id": "tui-injector", "uses": "plugin", "file": "hermes_cli/plugins.py",
     "what": "the Ink TUI sidecar installs its message injector on the plugin manager (the older host)",
     "needs": ["has_tui_message_injector", "_tui_message_injector"]},
    {"id": "tool-result-hook", "uses": "plugin", "file": "model_tools.py",
     "what": "the transform_tool_result hook that delivers a parked letter mid-turn",
     "needs": ["transform_tool_result"]},
    {"id": "gateway-session-route", "uses": "plugin", "file": "gateway/session.py",
     "what": "the gateway session store maps a Hermes session id to the route key inject_message needs",
     "needs": ["def lookup_by_session_id", "session_key"]},
    {"id": "gateway-runner-store", "uses": "plugin", "file": "gateway/run.py",
     "what": "the gateway runner (owner of the published injector) holds session_store",
     "needs": ["session_store"]},
    {"id": "tui-session-table", "uses": "plugin", "file": "tui_gateway/server.py",
     "what": "the TUI backend's live session table, method table and drop-sink transport (busy check, resume)",
     "needs": ["_sessions", "_sessions_lock", "_methods", "_detached_ws_transport"]},
    {"id": "tui-session-resume", "uses": "plugin", "file": "tui_gateway/methods_session.py",
     "what": "session.resume with eager_build (loads a closed chat before injecting)",
     "needs": ["session.resume", "eager_build"]},
    {"id": "tui-transport", "uses": "plugin", "file": "tui_gateway/transport.py",
     "what": "bind_transport and reset_transport (run resume detached)",
     "needs": ["def bind_transport", "def reset_transport"]},
    {"id": "spawn-ledger", "uses": "connector", "file": "hermes_cli/dashboard_procs.py",
     "what": "spawn-ledger.json rows with purpose serve (create-hermes finds the backend port)",
     "needs": ["purpose", "serve"]},
    {"id": "session-token-page", "uses": "connector", "file": "hermes_cli/web_server_dashboard.py",
     "what": "the backend page that carries the session token create-hermes reads",
     "needs": ["__HERMES_SESSION_TOKEN__"]},
    {"id": "gateway-ready-event", "uses": "connector", "file": "tui_gateway/entry.py",
     "what": "the gateway.ready event create-hermes waits for",
     "needs": ["gateway.ready"]},
    {"id": "session-create", "uses": "connector", "file": "tui_gateway/methods_session.py",
     "what": "session.create (a new Desktop chat)",
     "needs": ["session.create"]},
    {"id": "install-stamp", "uses": "both", "file": "install-stamp.json",
     "what": "the install stamp that names the Hermes version",
     "needs": ["baseVersion"]},
)

CORE_RULES = ("Rules: use exact directory keys, never titles. Never resend a letter that is accepted, held or "
              "uncertain; check crosstalk_status instead. Held means queued and may land when the recipient's turn "
              "ends. Reply to the exact sender key shown in the letter. Same-app letters (Hermes to Hermes) are "
              "refused; use the native Hermes tools. On a refusal, report the reason to the owner.")
TOOL_STATUS = ("Crosstalk status: registers this Hermes chat in the Crosstalk chat directory, checks pending native "
               "arrivals and reports held letters. Takes no arguments. Use it, not a resend, to check on a letter. "
               "Do not call it in a loop to wait for a reply: replies to this chat arrive as new messages after "
               "your turn ends.")
TOOL_LIST = ("Crosstalk directory: lists registered chats, newest first, compact (key, app, title, folder, hint; "
             "self marked). Capped (default 40; 'more' counts the rest): narrow with query (case-insensitive text "
             "in title, folder or key) and app. Titles are display-only. Read-only. Pick a chat whose hint is "
             "'sendable' or 'sendable:wakes-chat'.")
TOOL_SEND = ("Send one letter to an exact Claude or Codex chat from the Crosstalk directory. 'to' is the directory key "
             "(claude:<session-id> or codex:<thread-id>) or the bare ID. The sender is always this chat. Check "
             "crosstalk_list first. Arrived means a native input was verified; held means the transport accepted it "
             "but native arrival is unverified. Other results are refused (proven not sent) or blocked. "
             "Replies come back as new messages in this chat. After sending a letter that needs an answer, end your "
             "turn; do not sleep or poll crosstalk_status in the same turn to wait. After every send, show the "
             "owner the full letter text word for word in your reply (the result's letter_text), then the "
             "recipient and state. " + CORE_RULES)
TOOL_CREATE = ("Open a NEW Claude, Codex or Hermes chat with a first prompt and register it. Returns its directory "
               "key, or state 'started' if it is still being created (never call create again for that; check "
               "crosstalk_list/crosstalk_status). claude and codex need folder (absolute, existing), model and effort; "
               "hermes takes optional model, provider and effort. Respects Crosstalk controls (off, halt, deny). "
               + CORE_RULES)
CREATE_WAIT_S = 30  # how long the Hermes create stays connected for the first turn; the turn keeps running after
CREATE_CLI_TIMEOUT_S = 150
CREATE_APPS = ("claude", "codex", "hermes")


def _home():
    return Path(__file__).resolve().parents[2]


def _base():
    return _home() / "crosstalk"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _write_json(path, data):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    for attempt in range(8):  # on Windows a reader holding the file open makes the replace fail for a moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.02 * (attempt + 1))


def _config():
    try:
        value = json.loads((Path(__file__).resolve().parent / "crosstalk.json").read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


# ------------------------------------------------------------------ controls
def controls_refusal(recipient_key, controls_path):
    """Mirror of the core's controls_check: halted, messaging-off (also when the file is missing), denied."""
    value = {}
    for attempt in range(5):  # the core replaces controls.json atomically; a read can collide with that on Windows
        try:
            value = json.loads(Path(controls_path).read_text(encoding="utf-8")) if controls_path else {}
            break
        except FileNotFoundError:
            break
        except (OSError, ValueError, TypeError):
            time.sleep(0.03 * (attempt + 1))
    if not isinstance(value, dict):
        value = {}
    deny = value.get("deny")
    deny = [d for d in deny if isinstance(d, str)] if isinstance(deny, list) else []
    if value.get("halt") is True:
        return "halted"
    if value.get("messaging") != "on":
        return "messaging-off"
    if recipient_key and recipient_key in deny:
        return "denied"
    return None


def letter_framing_refusal(text, letter_id):
    """None for a framed Crosstalk letter whose ref line matches letter_id, else a short reason.

    Mirrors the Claude plugin's rule (register.mjs LETTER_HEAD / LETTER_REF): text that is not a framed letter
    must never become a user turn."""
    if not isinstance(text, str) or not text.startswith(LETTER_HEAD):
        return "not-a-framed-letter"
    found = LETTER_REF_RE.search(text)
    if not found:
        return "not-a-framed-letter"
    if not isinstance(letter_id, str) or letter_id != found.group(1):
        return "letter-id-mismatch"
    return None


# --------------------------------------------------------------- tool handlers
def build_argv(config, tool, session_id, params, text_file=None):
    """The CLI command for one tool call, or None when the installer's crosstalk.json is incomplete."""
    python, cli = config.get("python"), config.get("cli")
    if not isinstance(python, str) or not isinstance(cli, str) or not python or not cli:
        return None
    argv = [python] if cli == "-" else [python, "-I", "-B", cli]  # "-": python is the installed launcher, already the CLI
    if tool == "crosstalk_status":
        return argv + ["hermes-status", "--session", session_id]
    if tool == "crosstalk_list":
        argv += ["hermes-list", "--session", session_id]
        for name in ("query", "app"):
            if params.get(name) is not None:
                argv += ["--" + name, str(params[name])]
        if params.get("limit") is not None:
            argv += ["--limit", str(params["limit"])]
        return argv
    if tool == "crosstalk_create":
        app = params.get("app")
        argv += ["create-" + app]
        if app in ("claude", "codex"):
            argv += ["--folder", str(params.get("folder")), "--model", str(params.get("model")),
                     "--effort", str(params.get("effort"))]
        else:
            for name in ("model", "provider", "effort"):
                if params.get(name):
                    argv += ["--" + name, str(params[name])]
            argv += ["--wait-seconds", str(CREATE_WAIT_S)]
        if params.get("title"):
            argv += ["--title", str(params["title"])[:80]]
        return argv + ["--prompt-file", text_file]
    if tool == "crosstalk_send":
        return argv + ["hermes-send", "--session", session_id, "--to", str(params.get("to", "")),
                       "--text-file", text_file]
    return None


def _failure(reason, detail=""):
    return json.dumps({"ok": False, "tag": TAG, "reason": reason, "detail": str(detail)[:300]})


def _with_letter_text(line, text):
    """Echo the exact letter text into the send result: Hermes folds tool calls, so the chat shows the owner the
    letter only when the reply quotes it (owner 2026-10-14: show the full letter, as Claude and Codex chats do)."""
    try:
        value = json.loads(line)
    except ValueError:
        return line
    if not isinstance(value, dict):
        return line
    value["letter_text"] = text
    value["show_owner"] = "Quote letter_text in full, word for word, in your reply to the owner."
    return json.dumps(value)


def _origin_line(session_id):
    """Who opened the new chat (same wording as the core's chat_origin_line); the caller's text is otherwise untouched."""
    return (f"\n\n(Crosstalk: this chat was opened by Hermes chat hermes:{session_id}. "
            "To reply to it, send a Crosstalk letter to that key.)")


def _create_refusal(params):
    """A short reason when the arguments cannot make a chat (nothing is started); the CLI validates the rest."""
    app = params.get("app")
    if app not in CREATE_APPS:
        return "invalid-app"
    extra = sorted(set(params) - {"app", "folder", "model", "effort", "title", "prompt", "provider"})
    if extra:
        return "unexpected-fields:" + ",".join(extra)[:100]
    for name in ("folder", "model", "effort", "title", "provider"):
        if params.get(name) is not None and not isinstance(params.get(name), str):
            return "invalid-" + name
    prompt = params.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt or len(prompt) > 20000:
        return "prompt-invalid"
    if app in ("claude", "codex") and not (params.get("folder") and params.get("model") and params.get("effort")):
        return "folder-model-effort-required"
    if params.get("provider") and app != "hermes":
        return "provider-only-for-hermes"
    return None


def _run(tool, params, kwargs):
    session_id = kwargs.get("session_id")
    if not isinstance(session_id, str) or not SESSION_RE.fullmatch(session_id):
        return _failure("no-hermes-session-id", "the host gave this tool call no durable chat id")
    config = _config()
    params = params if isinstance(params, dict) else {}
    text_path = None
    try:
        if tool == "crosstalk_send":
            to, text = params.get("to"), params.get("text")
            if not isinstance(to, str) or not isinstance(text, str):
                return _failure("invalid-arguments", "to and text must be strings")
            handle, text_path = tempfile.mkstemp(prefix="crosstalk-", suffix=".txt")
            with os.fdopen(handle, "wb") as stream:
                stream.write(text.encode("utf-8"))
        if tool == "crosstalk_create":
            refusal = _create_refusal(params)
            if refusal:
                return json.dumps({"ok": False, "tag": TAG, "state": "refused", "created": False, "reason": refusal})
            origin = _origin_line(session_id)
            if len(params["prompt"]) + len(origin) > 20000:
                return json.dumps({"ok": False, "tag": TAG, "state": "refused", "created": False,
                                   "reason": "prompt-too-long-for-origin-line"})
            handle, text_path = tempfile.mkstemp(prefix="crosstalk-", suffix=".txt")
            with os.fdopen(handle, "wb") as stream:
                stream.write((params["prompt"] + origin).encode("utf-8"))
        argv = build_argv(config, tool, session_id, params, text_path)
        if argv is None:
            return _failure("plugin-not-configured", "crosstalk.json beside the plugin is missing or incomplete")
        env = dict(os.environ, CROSSTALK_P0_HERMES_HOME=str(_home()))
        if isinstance(config.get("device_dir"), str) and config["device_dir"]:
            env["CROSSTALK_P0_DEVICE_DIR"] = config["device_dir"]
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=CREATE_CLI_TIMEOUT_S if tool == "crosstalk_create" else CLI_TIMEOUT_S,
                              env=env, creationflags=NO_WINDOW)
        lines = [line for line in (done.stdout or "").splitlines() if line.strip().startswith("{")]
        if not lines:
            return _failure("cli-no-output", (done.stderr or "")[-200:])
        if tool == "crosstalk_send":
            return _with_letter_text(lines[-1], params["text"])
        return lines[-1]
    except subprocess.TimeoutExpired:
        if tool == "crosstalk_create":
            return json.dumps({"ok": False, "tag": TAG, "state": "uncertain", "created": None,
                               "reason": "cli-timeout",
                               "guidance": "The create timed out; a chat may exist. Do NOT create another; check "
                                           "crosstalk_list and tell the owner."})
        # The CLI may have sent: never report refused, and never invite a resend.
        return json.dumps({"ok": False, "tag": TAG, "state": "uncertain", "sent": None, "reason": "cli-timeout",
                           "guidance": "The Crosstalk CLI timed out; the outcome is unknown. Do NOT resend; run "
                                       "crosstalk_status and tell the owner."})
    except OSError as error:
        return _failure("cli-not-launched", type(error).__name__)
    finally:
        if text_path:
            try:
                os.unlink(text_path)
            except OSError:
                pass


def _handler(tool):
    def handler(params, **kwargs):
        return _run(tool, params, kwargs)
    handler.__name__ = "handle_" + tool
    return handler


# ------------------------------------------------------------------ listener
def _gateway_server():
    """The tui_gateway.server module of this backend process, or None (a short CLI run never loaded it).
    Never imported here: importing it would start nothing useful in a process that is not the backend."""
    return sys.modules.get("tui_gateway.server")


def resume_closed_chat(key, letter_id):
    """Load a stored Hermes chat into this backend, detached, so inject_message can reach it.

    Returns (True, None) when the chat is live afterwards, (False, reason) when the load failed, and (None, note)
    when this process has no usable resume API (then the chat stays 'not open'). Nothing is injected here, so
    any failure is a proven not-sent. Uses the backend's own session.resume handler (the call the Desktop and
    the onboarding flow make), run on a drop-sink transport: no frames go to stdout, the chat gets no WebSocket,
    no orphan-reap timer is armed, and Hermes Desktop later reattaches it through its normal resume.
    Source: tui_gateway/methods_session.py session.resume (_resume_eager), tui_gateway/prompt_turn.py turn gate, tui_gateway/server.py _DropTransport,
    tui_gateway/methods_onboarding.py (in-process _methods['session.resume'] precedent)."""
    server = _gateway_server()
    if server is None:
        return None, "no-gateway-module"
    try:
        handler = server._methods["session.resume"]
        sink = server._detached_ws_transport
        from tui_gateway.transport import bind_transport, reset_transport
    except Exception as exc:
        return None, "resume-api-missing:%s" % type(exc).__name__
    token = bind_transport(sink)
    try:
        # eager_build: the agent is attached before the reply. A cold resume builds it later on a timer, and an
        # inject that beats the build is accepted, then refused by the turn gate ("no agent attached").
        reply = handler("crosstalk-resume-" + str(letter_id),
                        {"session_id": key, "source": "desktop", "eager_build": True, "omit_messages": True})
    except Exception as exc:
        return False, "hermes-resume-failed:%s" % type(exc).__name__
    finally:
        reset_transport(token)
    if not isinstance(reply, dict) or isinstance(reply.get("error"), dict):
        error = reply.get("error") if isinstance(reply, dict) and isinstance(reply.get("error"), dict) else {}
        return False, "hermes-resume-failed:code=%s" % str(error.get("code"))[:12]
    result = reply.get("result")
    stored = None
    if isinstance(result, dict):
        stored = result.get("session_key") or result.get("stored_session_id") or result.get("resumed")
    if stored != key:
        # Resume followed a compression tip to a different stored id; injecting by `key` would miss it.
        return False, "hermes-resume-id-mismatch"
    session = getattr(server, "_sessions", {}).get(result.get("session_id"))
    ready = session.get("agent_ready") if isinstance(session, dict) else None
    if isinstance(session, dict) and session.get("agent") is None and hasattr(ready, "wait"):
        ready.wait(RESUME_AGENT_WAIT_S)  # a reused live chat may still be building its agent
    if not isinstance(session, dict) or session.get("agent") is None:
        # Loaded without an assistant: an inject now would be accepted, then refused by the turn gate.
        return False, "hermes-resume-no-agent"
    return True, None


def _flag(owner, name):
    """A manager attribute that is a bool or a bool-returning method (property or call), False on any problem."""
    value = getattr(owner, name, False)
    try:
        return bool(value() if callable(value) else value)
    except Exception:
        return False


def _host_probe(ctx):
    """(kind, probes): which kind of Hermes host this process is, and what each check found.

    kind is None when this process hosts no Desktop chats (a short CLI run, or a Hermes process that is not the
    host). probes goes into the status record, so a missing host is diagnosed and never silent."""
    manager = getattr(ctx, "_manager", None)
    tui = _flag(manager, "has_tui_message_injector")
    gateway = _flag(manager, "has_gateway_message_injector")
    probes = [{"name": "plugin-manager", "found": manager is not None},
              {"name": "tui-injector", "found": tui},
              {"name": "gateway-injector", "found": gateway}]
    return ("tui" if tui else "gateway" if gateway else None), probes


def _host_kind(ctx):
    """Which kind of Hermes host this process is, or None when it hosts no Desktop chats (a short CLI run).

    "tui": the Ink TUI / Desktop sidecar (tui_gateway) installed its injector on this plugin's manager.
    "gateway": the one-gateway runtime (`hermes gateway run`) owns Desktop chats on its session authority and
      published its injector (publish_gateway_message_host). After the 2026-10-26 Hermes update this is the only
      host: no tui_gateway sidecar runs, so has_tui_message_injector is never true and the old check went silent
      (no heartbeat, every letter refused "hermes-not-running").
    (hermes_cli.plugins stamps a process-published TUI host onto every manager, so the manager check covers it.)"""
    return _host_probe(ctx)[0]


def _hosts_chats(ctx):
    """True when this process can inject into Desktop chats (not a short CLI run)."""
    return _host_kind(ctx) is not None


def _inject_key(ctx, session_id):
    """The session_key to hand ctx.inject_message for the chat whose Hermes session id is `session_id`.

    The TUI hosts key their sessions by the stored session id, so it is used as is. The one-gateway runtime keys a
    local Desktop session by its route ("agent:<profile>:local:dm:<chat id>"), not by the session id, so the
    id is looked up in the runner's session store (the runner is the owner of the published gateway injector).
    None when that lookup fails: the gateway accepts an unknown key at once (True) and only drops it at dispatch, so
    injecting the bare id would report a letter held that can never arrive. The caller treats None as not open."""
    if _host_kind(ctx) != "gateway":
        return session_id
    try:
        runner = ctx._manager._gateway_message_injector[0]
        entry = runner.session_store.lookup_by_session_id(session_id)
        route = getattr(entry, "session_key", None)
        if isinstance(route, str) and route:
            return route
    except Exception:
        pass
    return None


def _inject(ctx, text, session_id):
    """ctx.inject_message under the host's key for this chat; False (nothing sent) when the gateway has no route."""
    route = _inject_key(ctx, session_id)
    if route is None:
        return False
    return bool(ctx.inject_message(text, "user", session_key=route))


def _session_busy(key):
    """True when the live Desktop session whose session_key is `key`.

    busy = its turn is running or it still holds a queued prompt. The host's inject only appends to that session's
    queue while busy (tui_gateway/plugin_inject.py: a running session just queues, and returns True), the queue is
    text-merged into one slot (session_auto_continue.py _enqueue_prompt) and is wiped by Stop and /new
    (methods_session_interrupt.py, agent_callbacks.py _reset_session_agent), so a letter injected then can be
    merged away or lost. Unknown or unreadable = not busy (the old behavior)."""
    server = _gateway_server()
    sessions = getattr(server, "_sessions", None) if server is not None else None
    if not isinstance(sessions, dict):
        return False
    lock = getattr(server, "_sessions_lock", None)
    try:
        if hasattr(lock, "__enter__"):
            with lock:
                items = list(sessions.items())
        else:
            items = list(sessions.items())
        for _sid, session in items:
            if isinstance(session, dict) and session.get("session_key") == key:
                return bool(session.get("running") or session.get("queued_prompt") or session.get("queued_prompts"))
    except Exception:
        return False
    return False


def _handle(ctx, path, waiting=False):
    """Deliver one letter file. `waiting` is True for a file parked in waiting/ (a busy chat's letter)."""
    base = _base()
    claimed = base / "work" / ("%s.%d.json" % (path.stem, os.getpid()))
    try:
        os.replace(path, claimed)  # another process may have claimed it first
    except OSError:
        return
    result = {"letter_id": path.stem, "pid": os.getpid(), "claimed_utc": _now(), "returned": None}
    destination = "done"
    try:
        msg = json.loads(claimed.read_text(encoding="utf-8-sig"))
        key = msg["session_key"]
        if not isinstance(key, str) or not SESSION_RE.fullmatch(key):
            raise ValueError("bad session_key")
        result["session_key"] = key
        framing = letter_framing_refusal(msg.get("text"), msg.get("letter_id"))
        if framing:
            # Not a framed Crosstalk letter: never a user turn. The core reads this result as a proven refusal.
            result["returned"] = False
            result["refused"] = framing
            destination = "cancelled"
        else:
            device = _config().get("device_dir")
            refusal = controls_refusal("hermes:" + key, str(Path(device) / "controls.json")
                                       if isinstance(device, str) and device else None)
            if refusal:
                result["returned"] = False
                result["refused"] = refusal
            elif _session_busy(key):
                # Never inject into a running turn: park the letter (nothing was injected) and retry when the chat
                # is idle. The core reads the early result as accepted, so the letter stays held, never refused.
                try:
                    age = time.time() - claimed.stat().st_mtime
                except OSError:
                    age = 0.0
                if age > WAIT_MAX_S:
                    result["returned"] = False
                    result["refused"] = "hermes-busy-timeout"
                    destination = "cancelled"
                else:
                    if not waiting:
                        result["deferred"] = "session-busy"
                        result["returned"] = None
                        _write_json(base / "done" / (path.stem + ".result.json"), result)
                    (base / "waiting").mkdir(parents=True, exist_ok=True)
                    os.replace(claimed, base / "waiting" / path.name)
                    return
            else:
                result["returned"] = _inject(ctx, str(msg["text"]), key)
                if result["returned"] is False:
                    # Closed chat: the host only injects into a live session. Load it (nothing is sent by the
                    # load), re-check controls, then inject once more. A resume failure is a proven not-sent.
                    # The one-gateway runtime has no tui_gateway session.resume to call: stays "not open".
                    if _host_kind(ctx) == "gateway":
                        resumed, why = None, "gateway-host-no-resume"
                    else:
                        resumed, why = resume_closed_chat(key, path.stem)
                    result["resumed"] = resumed
                    if resumed is None:
                        result["resume_note"] = why
                    elif not resumed:
                        result["refused"] = why
                    else:
                        again = controls_refusal("hermes:" + key, str(Path(device) / "controls.json")
                                                 if isinstance(device, str) and device else None)
                        if again:
                            result["refused"] = again
                        else:
                            result["returned"] = _inject(ctx, str(msg["text"]), key)
    except Exception as exc:
        result["returned"] = None
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
    result["finished_utc"] = _now()
    _write_json(base / "done" / (path.stem + ".result.json"), result)
    try:
        os.replace(claimed, base / destination / path.name)
    except OSError:
        pass


def _in_send_order(paths):
    """Letter files oldest first. Names are random letter ids, so a name sort is not send order; the file's
    mtime is its inbox write time (os.replace into waiting/ keeps it). Ties fall back to the name."""
    def key(path):
        try:
            return (path.stat().st_mtime_ns, path.name)
        except OSError:
            return (0, path.name)
    return sorted(paths, key=key)


def _waiting_pass(ctx, base):
    """Retry parked letters whose chat is idle now (or whose controls changed, so they are refused). Returns the
    number still waiting. A letter is claimed by an atomic rename before its one inject, so it cannot go twice."""
    directory = base / "waiting"
    try:
        files = _in_send_order(directory.glob("*.json"))
    except OSError:
        return 0
    left = 0
    device = _config().get("device_dir")
    controls = str(Path(device) / "controls.json") if isinstance(device, str) and device else None
    for path in files:
        try:
            key = json.loads(path.read_text(encoding="utf-8-sig"))["session_key"]
        except (OSError, ValueError, KeyError, TypeError):
            key = None
        if (isinstance(key, str) and _session_busy(key) and not controls_refusal("hermes:" + key, controls)
                and time.time() - path.stat().st_mtime <= WAIT_MAX_S):
            left += 1
            continue
        _handle(ctx, path, waiting=True)
        if path.exists():
            left += 1
    return left


MIDTURN_HEAD = "[Crosstalk: letter delivered mid-turn, at this tool call]"


def _midturn_block(letters):
    """The separated block appended to a tool result. Each letter keeps its normal framing (header line and the
    trailing ref line), so readback counts it like an injected user turn (core hermes_arrivals)."""
    parts = [MIDTURN_HEAD]
    for text in letters:
        parts.append(text)
    return "\n\n---\n" + "\n\n".join(parts) + "\n---"


def _restore(claimed_list, base):
    for claimed, name in claimed_list:
        try:
            os.replace(claimed, base / "waiting" / name)
        except OSError:
            pass


def _transform_tool_result(**kwargs):
    """Host hook transform_tool_result (hermes model_tools._apply_transform_tool_result_hook): the first string a
    plugin returns replaces the tool result. If a letter is parked for this session, claim it by atomic rename
    (the end-of-turn waiting pass claims the same file the same way, so exactly one of them delivers) and return
    the original result with the letter(s) appended, oldest first. Any problem: return None (result unchanged)
    and leave the letter parked for the end-of-turn path. Never raises into the host."""
    claimed_list = []
    base = None
    try:
        result = kwargs.get("result")
        session = kwargs.get("session_id")
        if not isinstance(result, str) or not isinstance(session, str) or not SESSION_RE.fullmatch(session):
            return None
        base = _base()
        directory = base / "waiting"
        if not directory.is_dir():
            return None
        device = _config().get("device_dir")
        controls = str(Path(device) / "controls.json") if isinstance(device, str) and device else None
        refusal = controls_refusal("hermes:" + session, controls)
        if refusal:
            return None  # halt/off/deny: the waiting pass cancels it
        letters = []
        for path in _in_send_order(directory.glob("*.json")):
            try:
                msg = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                break  # keep send order: never skip past a letter we could not read
            if not isinstance(msg, dict) or msg.get("session_key") != session:
                continue
            if letter_framing_refusal(msg.get("text"), msg.get("letter_id")) or not isinstance(msg.get("text"), str):
                break
            try:
                if time.time() - path.stat().st_mtime > WAIT_MAX_S:
                    break  # the waiting pass cancels it with hermes-busy-timeout
            except OSError:
                break
            claimed = base / "work" / ("%s.hook%d.json" % (path.stem, os.getpid()))
            try:
                os.replace(path, claimed)
            except OSError:
                break  # another claimer has it
            claimed_list.append((claimed, path.name))
            letters.append((path.stem, msg["text"]))
        if not letters:
            return None
        out = result + _midturn_block([t for _i, t in letters])
        for (claimed, name), (letter_id, _t) in zip(claimed_list, letters):
            _write_json(base / "done" / (letter_id + ".result.json"),
                        {"letter_id": letter_id, "pid": os.getpid(), "claimed_utc": _now(), "returned": True,
                         "via": "tool-result", "session_key": session, "finished_utc": _now()})
            try:
                os.replace(claimed, base / "done" / name)
            except OSError:
                pass
        return out
    except Exception:
        if base is not None:
            _restore(claimed_list, base)
        return None


# ------------------------------------------------------- self-diagnosis (0.4.0)
_STATUS = {"loaded_utc": None, "last_kind": "unset", "written_at": 0.0, "hermes": None}
PROBE_ID_RE = re.compile(r"probe-[0-9a-f]{12}\Z")


def _hermes_source_root():
    """The Hermes source folder this process runs from (hermes_cli's parent), else <HERMES_HOME>/hermes-agent."""
    module = sys.modules.get("hermes_cli")
    where = getattr(module, "__file__", None)
    if isinstance(where, str) and where:
        return Path(where).resolve().parents[1]
    candidate = _home() / "hermes-agent"
    return candidate if candidate.is_dir() else None


def _hermes_info():
    """{version, commit, source_root} from Hermes' install stamp (a plain JSON file; nothing is imported)."""
    if _STATUS["hermes"] is None:
        info = {"version": None, "commit": None, "source_root": None}
        try:
            root = _hermes_source_root()
            if root is not None:
                info["source_root"] = str(root)
                stamp = json.loads((root / "install-stamp.json").read_text(encoding="utf-8-sig"))
                version = stamp.get("displayVersion") or stamp.get("baseVersion")
                info["version"] = str(version)[:40] if version else None
                info["commit"] = str(stamp["commit"])[:12] if stamp.get("commit") else None
        except Exception:
            pass
        _STATUS["hermes"] = info
    return _STATUS["hermes"]


def _status_path():
    return _base() / "status" / ("%d.json" % os.getpid())


def _write_status(ctx, force=False):
    """This process's status record: what it is, which host checks it tried and what each found."""
    kind, probes = _host_probe(ctx)
    now = time.monotonic()
    if not force and kind == _STATUS["last_kind"] and now - _STATUS["written_at"] < STATUS_EVERY_S:
        return
    if _STATUS["loaded_utc"] is None:
        _STATUS["loaded_utc"] = _now()
    info = _hermes_info()
    path = _status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, {"profile": STATUS_PROFILE, "pid": os.getpid(), "plugin_version": PLUGIN_VERSION,
                       "hermes_version": info["version"], "hermes_commit": info["commit"],
                       "hermes_source": info["source_root"], "host": kind, "hosts_chats": kind is not None,
                       "probes": probes, "process": Path(sys.executable).name[:40],
                       "loaded_utc": _STATUS["loaded_utc"], "utc": _now()})
    _STATUS["last_kind"], _STATUS["written_at"] = kind, now


def _remove_status():
    try:
        _status_path().unlink()
    except OSError:
        pass


def _session_open(key):
    """True when the TUI backend holds a live session whose session_key is `key` (read-only)."""
    server = _gateway_server()
    sessions = getattr(server, "_sessions", None) if server is not None else None
    if not isinstance(sessions, dict):
        return False
    lock = getattr(server, "_sessions_lock", None)
    if hasattr(lock, "__enter__"):
        with lock:
            items = list(sessions.values())
    else:
        items = list(sessions.values())
    return any(isinstance(session, dict) and session.get("session_key") == key for session in items)


def _probe_route(ctx, kind, session_ids):
    """The route lookup the real inject uses, run for known chat ids and NEVER followed by an inject.

    api: "ok", or "missing:<why>" when the lookup this host kind needs is gone. open: ids that resolve to a live
    route (a closed chat is normal and not an error)."""
    result = {"api": "ok", "tried": 0, "open": [], "errors": 0}
    try:
        if kind == "gateway":
            runner = ctx._manager._gateway_message_injector[0]
            if not callable(getattr(runner.session_store, "lookup_by_session_id", None)):
                raise AttributeError("lookup_by_session_id")
        else:
            server = _gateway_server()
            if server is None:
                result["api"] = "missing:no-gateway-module"
                return result
            if not isinstance(getattr(server, "_sessions", None), dict):
                raise AttributeError("_sessions")
    except Exception as exc:
        result["api"] = "missing:%s" % type(exc).__name__
        return result
    for session_id in session_ids[:PROBE_MAX_ROUTES]:
        if not isinstance(session_id, str) or not SESSION_RE.fullmatch(session_id):
            continue
        result["tried"] += 1
        try:
            found = _inject_key(ctx, session_id) is not None if kind == "gateway" else _session_open(session_id)
        except Exception:
            result["errors"] += 1
            continue
        if found:
            result["open"].append(session_id)
    return result


def _probe_pass(ctx, base, kind):
    """Answer self-test probes (only in the process that hosts chats). Nothing is ever injected into a chat."""
    try:
        files = list((base / "probe").glob("*.json"))
    except OSError:
        return
    for path in files:
        if not PROBE_ID_RE.fullmatch(path.stem):
            continue
        claimed = base / "work" / ("%s.probe%d.json" % (path.stem, os.getpid()))
        try:
            os.replace(path, claimed)  # another hosting process may have claimed it first
        except OSError:
            continue
        info = _hermes_info()
        answer = {"probe_id": path.stem, "pid": os.getpid(), "plugin_version": PLUGIN_VERSION,
                  "hermes_version": info["version"], "host": kind, "hosts_chats": True, "inbox_loop": True,
                  "injected": False, "answered_utc": _now()}
        try:
            request = json.loads(claimed.read_text(encoding="utf-8-sig"))
            ids = request.get("session_ids") if isinstance(request, dict) else None
            answer["route"] = _probe_route(ctx, kind, ids if isinstance(ids, list) else [])
        except Exception as exc:
            answer["route"] = {"api": "missing:%s" % type(exc).__name__, "tried": 0, "open": [], "errors": 1}
        (base / "probe-done").mkdir(parents=True, exist_ok=True)
        _write_json(base / "probe-done" / (path.stem + ".result.json"), answer)
        try:
            claimed.unlink()
        except OSError:
            pass


def _prune(base):
    cutoff = time.time() - PRUNE_AFTER_S
    for name in ("done", "cancelled", "work", "probe-done"):
        try:
            for item in (base / name).iterdir():
                if item.is_file() and item.stat().st_mtime < cutoff:
                    item.unlink()
        except OSError:
            pass
    try:  # a status file nobody refreshed for an hour belongs to a process that is gone
        for item in (base / "status").iterdir():
            if item.is_file() and item.stat().st_mtime < time.time() - 3600.0:
                item.unlink()
    except OSError:
        pass


def _record_error(base, error):
    """Make a failing listener visible: log it, count it, leave listener-error.json (rate limited by content)."""
    text = "%s: %s" % (type(error).__name__, str(error)[:200])
    _ERRORS["count"] += 1
    _ERRORS["last"] = text
    try:
        LOG.error("crosstalk listener pass failed: %s", text)
        now = time.monotonic()
        if text != _ERRORS["written_text"] or now - _ERRORS["written_at"] >= ERROR_LOG_EVERY_S:
            _write_json(base / "listener-error.json", {"pid": os.getpid(), "utc": _now(), "count": _ERRORS["count"],
                                                        "last_error": text, "plugin_version": PLUGIN_VERSION})
            _ERRORS["written_at"], _ERRORS["written_text"] = now, text
    except Exception:
        pass  # reporting must never stop the listener


def _loop(ctx):
    base = _base()
    last_beat = last_prune = 0.0
    while True:
        try:
            kind = _host_kind(ctx)
            hosts = _hosts_chats(ctx)
            waiting = len(list((base / "waiting").glob("*.json"))) if (base / "waiting").is_dir() else 0
            try:
                _write_status(ctx)
            except Exception as error:
                _record_error(base, error)
            if hosts and time.monotonic() - last_beat >= HEARTBEAT_S:
                _write_json(base / "heartbeat.json", {"pid": os.getpid(), "utc": _now(), "hosts_chats": True,
                                                       "host": kind, "plugin_version": PLUGIN_VERSION,
                                                       "hermes_version": _hermes_info()["version"],
                                                       "waiting": waiting,
                                                       "listener_errors": _ERRORS["count"],
                                                       "last_error": _ERRORS["last"]})
                last_beat = time.monotonic()
            if hosts:
                _probe_pass(ctx, base, kind)
                for path in _in_send_order((base / "inbox").glob("*.json")):
                    _handle(ctx, path)
                _waiting_pass(ctx, base)
                if time.monotonic() - last_prune >= 3600.0:
                    _prune(base)
                    last_prune = time.monotonic()
        except Exception as error:
            _record_error(base, error)
        time.sleep(POLL_S)


def register(ctx):
    for name, description, properties, required in (
            ("crosstalk_status", TOOL_STATUS, {}, []),
            ("crosstalk_list", TOOL_LIST,
             {"query": {"type": "string", "description": "Case-insensitive text to find in title, folder or key"},
              "app": {"type": "string", "enum": ["claude", "codex", "hermes"], "description": "Only chats of this app"},
              "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Rows to show (default 40)"}},
             []),
            ("crosstalk_send", TOOL_SEND,
             {"to": {"type": "string", "description": "Exact directory key claude:<id> or codex:<id>, or the bare ID"},
              "text": {"type": "string", "description": "Letter text (max 3000 characters)"}},
             ["to", "text"]),
            ("crosstalk_create", TOOL_CREATE,
             {"app": {"type": "string", "enum": ["claude", "codex", "hermes"]},
              "folder": {"type": "string", "description": "Absolute existing project folder (claude, codex)"},
              "model": {"type": "string", "description": "Model name (required for claude and codex)"},
              "effort": {"type": "string", "description": "Effort: low, medium, high, xhigh, max (hermes also none, minimal, ultra)"},
              "title": {"type": "string", "description": "Chat title (max 80 characters)"},
              "prompt": {"type": "string", "description": "First prompt for the new chat"},
              "provider": {"type": "string", "description": "Hermes only, together with model"}},
             ["app", "prompt"])):
        ctx.register_tool(
            name=name,
            toolset="crosstalk",
            schema={"name": name, "description": description,
                    "parameters": {"type": "object", "properties": properties, "required": required}},
            handler=_handler(name),
        )
    try:
        ctx.register_hook("transform_tool_result", _transform_tool_result)
    except Exception:
        pass  # older host: parked letters still go at the end of the turn
    base = _base()
    for sub in ("inbox", "work", "done", "cancelled", "probe", "probe-done", "status"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    try:  # the load itself is on record before the host has had time to publish its injector
        _write_status(ctx, force=True)
        atexit.register(_remove_status)
    except Exception as error:
        _record_error(base, error)
    if any(t.name == THREAD_NAME and t.is_alive() for t in threading.enumerate()):
        return
    threading.Thread(target=_loop, args=(ctx,), name=THREAD_NAME, daemon=True).start()
