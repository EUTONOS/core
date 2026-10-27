"""Settings that are not whole parts: Crosstalk messaging, RAM, and linked Claude accounts.

Setup installs every part and its boxes only switch things on or off; these three are the switches that live inside
a part or beside it. Each one has a small read and set API (the dashboard and Setup call these) and a verb under
``eutonos-install setting``. Every call is idempotent and says what it changed in plain words:

    {"outcome": "enabled" | "disabled" | "changed" | "unchanged" | "refused" | "failed" | "busy", "setting": NAME, ...}

``refused`` means nothing was touched and the reason says why (``reason`` and ``next_action`` in plain words).

* ``crosstalk-messaging``: the Crosstalk messaging switch in the device folder's ``controls.json``, changed through
  the kit's own ``controls_update(messaging=...)``. The owner's stop (``halt``) is never read as a choice and never
  written here; it only shows up in the notes.
* ``ram`` / ``ram-capture`` / ``ram-retrieval``: the RAM settings that already exist in ``eutonos.settings`` (per
  project: ``<checkout>/.eutonos-local/settings.json``, section ``ram``). They go through ``settings.update`` with its
  revision check, so there is no second store. They are per project, not per machine.
* ``linked-accounts``: the person's own Claude logins that the Crosstalk helper links to keep chat history across an
  account switch (``<device>/helper/config.json`` key ``switch_history``). A list written only when the person asks;
  ``suggest`` shows the emails Crosstalk has seen but never writes them. An empty list turns the feature off.

* ``crosstalk-switches``: the per-computer helper switches (``HELPER_SWITCHES``) in the same ``config.json``: one declared
  table (key, default, label, help, page, platform) with a generic read and set. A key the person never set keeps the
  helper's own default ("absent means default"); a write is always an explicit value. Windows-only switches stay
  listed but are marked not available elsewhere. ``crosstalk-everything`` turns every switch and messaging off and
  then the Crosstalk part. It is a convenience, never a precondition: switching the part off is always a plain request,
  the switches are kept as preferences while the part is off, and turning any switch on brings the part back first.
* the owner controls beyond messaging and the stop: the deny list and the wake budget (``controls.json``, through the
  kit's ``controls_update``), and the stop itself through ``crosstalk_status.owner_control``.

The device folder is found through the Crosstalk kit's own core code (``device_dir``), never by path here.
"""
from __future__ import annotations

import codecs
import json
import math
import os
from pathlib import Path

from . import component_state
from . import component_support as cs
from . import home as eutonos_home
from . import install_record as ir
from . import pending_actions
from . import settings
from . import setup_checkpoint as sc
from . import tokenslang as ts

NAMES = ("crosstalk-messaging", "ram", "ram-capture", "ram-retrieval", "linked-accounts", "crosstalk-switch",
         "crosstalk-everything")
MESSAGING = "crosstalk-messaging"
LINKED = "linked-accounts"
HELPER = "crosstalk-switches"
EVERYTHING = "crosstalk-everything"
MAX_LINKED = 16
SHARED_LOGIN_NOTE = ("Leave linked accounts empty on a Windows login that other people use. When it lists accounts, "
                     "Crosstalk copies a chat's history across a switch between them, so list only your own.")
RAM_CAPTURE_NOTE = ("Turning capture off only skips the optional per-turn memory notes. Board updates and closeout "
                    "are project record keeping and still run.")
RAM_PROJECT_NOTE = "RAM settings belong to one project (its checkout), not to the whole machine."


def _refuse(setting: str, reason: str, message: str, **extra) -> dict:
    return {"outcome": "refused", "setting": setting, "reason": reason, "detail": message, "next_action": message, **extra}


def _base(home=None) -> Path:
    return Path(home) if home is not None else eutonos_home.home()


# ------------------------------------------------------------------------------------ the Crosstalk kit
class _Refusal(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason, self.message = reason, message


def _kit_ctx(base: Path, kit_path=None) -> dict:
    """``{"home", "kit_path"}`` for the active generation's Crosstalk kit (or the one given, for tests)."""
    if kit_path is not None:
        return {"home": str(base), "kit_path": str(kit_path)}
    layout = ir.Layout(base)
    try:
        state = ir.read_state(layout)
        if not state["active"]:
            raise _Refusal("not-installed", "EUTONOS is not installed here. Install first: eutonos-install install")
        record = ir.read_record(layout, state["active"])
    except ts.ReadError as exc:
        raise _Refusal("state-unreadable", f"{exc}. Repair it, then try again.") from exc
    kit = next((item for item in record.get("kits", []) if item.get("component") == "crosstalk"), None)
    if kit is None:
        raise _Refusal("crosstalk-not-installed", "Crosstalk is not part of this installation.")
    return {"home": str(base), "kit_path": str(layout.kits / kit["kit_id"])}


def _core(ctx: dict):
    try:
        return cs.kit_module(ctx, "crosstalk_p0_core")
    except cs.ComponentError as exc:
        raise _Refusal("kit-missing", f"{exc}. Run: eutonos-install update") from exc


def _off_note(base: Path) -> list:
    state = _part_enabled(base)
    if state is False:
        return ["The Crosstalk part is switched off, so letters still do not flow. Switch it on with: "
                "eutonos-install component enable crosstalk"]
    if state is None:
        return ["The part state file could not be read, so whether Crosstalk is switched on is unknown."]
    return []


def _part_enabled(base: Path):
    """Is the Crosstalk part switched on: True or False, or None when the part state file cannot be read."""
    try:
        return component_state.component_enabled(base, "crosstalk")
    except component_state.ComponentStateError:
        return None


def _set_part(base: Path, enable: bool) -> dict:
    """Switch the Crosstalk part through the installer (one seam, so tests do not need a full install)."""
    from . import installer
    return installer.set_component("crosstalk", enable, home=base)


def _helper_start_note() -> str:
    if _is_windows():
        return ("The helper itself starts at your next Windows login: switching the part on adds its Startup shortcut "
                "and does not start the helper now.")
    return "The helper on this computer starts when Crosstalk's own start-up step runs; switching the part on does not start it now."


def _ensure_part_on(base: Path) -> dict:
    """Turning any Crosstalk switch on while the Crosstalk part is off switches the part on first (a switch is a request
    for the feature to work). Returns ``{"notes": [...], "pending": [...]}``. A part that is on, or whose state cannot
    be read as off, is left alone; a part that cannot be switched on leaves a note and the switch still gets set."""
    state = _part_enabled(base)
    if state is None:
        return {"notes": ["The part state file could not be read, so whether Crosstalk is switched on is unknown."],
                "pending": []}
    if state:
        return {"notes": [], "pending": []}
    answer = _set_part(base, True)
    if answer.get("outcome") in ("enabled", "unchanged"):
        return {"notes": ["The Crosstalk part was switched off, so it was switched on first. " + _helper_start_note()]
                + [note for note in answer.get("notes") or [] if note], "pending": list(answer.get("pending") or [])}
    why = answer.get("detail") or answer.get("reason") or answer.get("error") or "no reason was given"
    return {"notes": [f"The Crosstalk part is switched off and could not be switched on ({why}), so letters and helper "
                      "jobs still do not run. Switch it on with: eutonos-install component enable crosstalk"],
            "pending": []}


# ------------------------------------------------------------------------------- Crosstalk messaging
def crosstalk_messaging_status(*, home=None, kit_path=None) -> dict:
    """Read-only: is Crosstalk messaging on, and is the owner's stop set. Takes no lock."""
    base = _base(home)
    try:
        ctx = _kit_ctx(base, kit_path)
        core = _core(ctx)
        with cs.home_env(ctx):
            controls = core.controls_load()
    except _Refusal as exc:
        return _refuse(MESSAGING, exc.reason, exc.message)
    return {"outcome": "ok", "setting": MESSAGING, "enabled": controls["messaging"] == "on",
            "halt": bool(controls["halt"]), "notes": _off_note(base) + (
                ["Crosstalk is halted (the owner's stop). Messaging stays blocked until the owner clears it."]
                if controls["halt"] else [])}


def set_crosstalk_messaging(enable: bool, *, home=None, kit_path=None) -> dict:
    """Turn Crosstalk messaging on or off. Never touches ``halt`` (that belongs to the owner's stop). Asking for the
    state it already has writes nothing."""
    base = _base(home)
    follow = {"notes": [], "pending": []}
    try:
        if enable:  # a switch turned on while the part is off switches the part on first (outside the lock below)
            _kit_ctx(base, kit_path)
            follow = _ensure_part_on(base)
        with sc.machine_lock(base, wait=0):
            ctx = _kit_ctx(base, kit_path)
            core = _core(ctx)
            with cs.home_env(ctx):
                controls = core.controls_load()
                before = controls["messaging"]
                wanted = "on" if enable else "off"
                changed = before != wanted
                if changed:
                    controls = core.controls_update(messaging=wanted)
            notes = list(follow["notes"])
            if controls["halt"]:
                notes.append("Crosstalk is halted (the owner's stop), so letters do not flow whatever this is set to. "
                             "This setting does not clear it.")
            answer = {"setting": MESSAGING, "enabled": bool(enable), "before": before, "after": wanted,
                      "halt": bool(controls["halt"]), "notes": notes}
            if follow["pending"]:
                answer["pending"] = follow["pending"]
            if not changed:
                return {"outcome": "unchanged", "detail": f"Crosstalk messaging is already {wanted}.", **answer}
            return {"outcome": "enabled" if enable else "disabled", **answer}
    except _Refusal as exc:
        return _refuse(MESSAGING, exc.reason, exc.message)
    except sc.LifecycleBusy as exc:
        return {"outcome": "busy", "setting": MESSAGING, "reason": str(exc),
                "next_action": "Wait for the other operation to finish."}
    except Exception as exc:  # a kit or disk error: report it, the file is replaced atomically so it is never half-written
        return {"outcome": "failed", "setting": MESSAGING, "error": f"{type(exc).__name__}: {exc}",
                "next_action": "Fix the cause, then ask again."}


# ------------------------------------------------------------------------------------------------ RAM
def _ram_view(root: Path, current: dict) -> dict:
    ram = current["ram"]
    return {"scope": "project", "root": str(root), "capture": bool(ram["capture"]), "retrieval": ram["retrieval"],
            "cadence": ram["cadence"], "detail": ram["detail"], "revision": current["revision"],
            "enabled": bool(ram["capture"]) and ram["retrieval"] != "off"}


def _ram_root(root) -> Path:
    return Path(root if root is not None else Path.cwd()).resolve()


def _ram_read(root: Path) -> dict:
    return settings.status(root)


def ram_status(*, root=None) -> dict:
    """Read-only: the project's RAM capture and retrieval settings (the existing ``eutonos.settings`` section)."""
    path = _ram_root(root)
    try:
        current = _ram_read(path)
    except (ValueError, OSError, ts.ReadError) as exc:
        return _refuse("ram", "settings-unavailable", f"The RAM settings for {path} could not be read: {exc}")
    return {"outcome": "ok", "setting": "ram", **_ram_view(path, current), "notes": [RAM_PROJECT_NOTE, RAM_CAPTURE_NOTE]}


def _retrieval_value(value) -> str | None:
    if value is None:
        return None
    if value is True or value in ("on", "on-demand"):
        return "on-demand"
    if value is False or value == "off":
        return "off"
    raise ValueError(f"retrieval must be on or off, not {value!r}")


def set_ram(*, root=None, capture: bool | None = None, retrieval=None) -> dict:
    """Set the project's RAM ``capture`` (bool) and/or ``retrieval`` (on/off; stored as ``on-demand`` or ``off``).
    Whatever is left as None keeps its value, and ``cadence`` and ``detail`` are never touched. Goes through
    ``settings.update`` with its revision check (one retry if the settings changed in between)."""
    path = _ram_root(root)
    try:
        wanted_retrieval = _retrieval_value(retrieval)
        if capture is not None and not isinstance(capture, bool):
            raise ValueError(f"capture must be on or off, not {capture!r}")
    except ValueError as exc:
        return _refuse("ram", "invalid", str(exc))
    last = None
    for _attempt in range(2):
        try:
            current = _ram_read(path)
            before = _ram_view(path, current)
            wanted = {}
            if capture is not None and capture != before["capture"]:
                wanted["capture"] = capture
            if wanted_retrieval is not None and wanted_retrieval != before["retrieval"]:
                wanted["retrieval"] = wanted_retrieval
            notes = [RAM_PROJECT_NOTE] + ([RAM_CAPTURE_NOTE] if capture is False else [])
            if not wanted:
                return {"outcome": "unchanged", "setting": "ram", "detail": "The RAM settings already match.",
                        **before, "notes": notes}
            saved = settings.update(path, 0, current["revision"], "ram", wanted)
        except settings.StaleSettings as exc:
            last = exc
            continue
        except (ValueError, OSError, ts.ReadError) as exc:
            return _refuse("ram", "settings-unavailable", f"The RAM settings for {path} could not be saved: {exc}")
        after = _ram_view(path, saved)
        went_on = [name for name, value in wanted.items() if value is True or value == "on-demand"]
        outcome = "changed" if 0 < len(went_on) < len(wanted) else "enabled" if went_on else "disabled"
        return {"outcome": outcome, "setting": "ram", **after, "changed": {name: [before[name], after[name]] for name in wanted},
                "notes": notes}
    return {"outcome": "failed", "setting": "ram", "error": f"The settings kept changing: {last}",
            "next_action": "Ask again."}


# ---------------------------------------------------------------------------------- linked accounts
def _config_path(core) -> Path:
    return core.helper_dir() / "config.json"


def _read_config(core) -> dict:
    """The helper's config as a dict. A missing file is ``{}``; a file that cannot be read as a JSON object raises
    ``_Refusal`` (it is never replaced). A leading BOM is accepted here (the helper would ignore such a file whole) and
    is gone after the next write."""
    target = _config_path(core)
    try:
        raw = target.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise _Refusal("config-unreadable", f"The Crosstalk helper config is unreadable: {target} ({exc}).") from exc
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise _Refusal("config-unreadable", f"The Crosstalk helper config is not valid JSON: {target}. Repair or delete "
                                          "it, then ask again.") from exc
    if not isinstance(value, dict):
        raise _Refusal("config-unreadable", f"The Crosstalk helper config is not a JSON object: {target}. Repair or "
                                          "delete it, then ask again.")
    return value


def _listed(config: dict) -> list:
    value = config.get("switch_history")
    return [item.strip() for item in value if isinstance(item, str) and item.strip()] if isinstance(value, list) else []


def _seen(core) -> list:
    """Accounts Crosstalk has seen in Claude Desktop chats: ``[{"email", "account_id"}]``, sorted by email."""
    return sorted(({"email": email, "account_id": account} for account, email in core.desktop_account_emails().items()),
                  key=lambda item: (item["email"].lower(), item["account_id"]))


def linked_accounts_status(*, home=None, kit_path=None) -> dict:
    """Read-only: the accounts the helper links now (an empty list means the feature is off), which of them
    Crosstalk has seen, and the accounts it has seen that are not linked. Never writes."""
    base = _base(home)
    try:
        ctx = _kit_ctx(base, kit_path)
        core = _core(ctx)
        with cs.home_env(ctx):
            accounts = _listed(_read_config(core))
            seen = _seen(core)
            uuid_ok = core.UUID_RE.fullmatch
    except _Refusal as exc:
        return _refuse(LINKED, exc.reason, exc.message)
    known = {item["email"].lower() for item in seen} | {item["account_id"].lower() for item in seen}
    wanted = {item.lower() for item in accounts}
    return {"outcome": "ok", "setting": LINKED, "enabled": bool(accounts), "accounts": accounts,
            "unmatched": [item for item in accounts if item.lower() not in known and not uuid_ok(item.lower())],
            "suggested": [item for item in seen if item["email"].lower() not in wanted
                          and item["account_id"].lower() not in wanted],
            "notes": [SHARED_LOGIN_NOTE]}


def suggest_linked_accounts(*, home=None, kit_path=None) -> dict:
    """The emails Crosstalk has seen in Claude Desktop chats (its ``accounts.json``), as a suggestion only. Nothing
    is written: the person confirms the list and then ``set_linked_accounts`` writes it."""
    found = linked_accounts_status(home=home, kit_path=kit_path)
    if found["outcome"] != "ok":
        return found
    base = _base(home)
    ctx = _kit_ctx(base, kit_path)
    core = _core(ctx)
    with cs.home_env(ctx):
        seen = _seen(core)
    emails = sorted({item["email"] for item in seen}, key=str.lower)
    return {"outcome": "ok", "setting": LINKED, "suggested_emails": emails, "seen": seen, "accounts": found["accounts"],
            "notes": [SHARED_LOGIN_NOTE, "These are only suggestions. Nothing is saved until you set the list."]}


def _clean_accounts(core, accounts) -> list:
    if isinstance(accounts, str) or not hasattr(accounts, "__iter__"):
        raise ValueError("accounts must be a list of emails or account IDs")
    cleaned, keys = [], set()
    for item in accounts:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{item!r} is not an email or account ID")
        text = item.strip()
        if not (core.EMAIL_RE.fullmatch(text) or core.UUID_RE.fullmatch(text.lower())):
            raise ValueError(f"{text!r} is not an email address or a Claude account ID")
        if text.lower() not in keys:
            keys.add(text.lower())
            cleaned.append(text)
    if len(cleaned) > MAX_LINKED:
        raise ValueError(f"at most {MAX_LINKED} accounts can be linked")
    return cleaned


def set_linked_accounts(accounts, *, home=None, kit_path=None) -> dict:
    """Write the linked-accounts list: the person's own Claude emails or account IDs. An empty list turns the
    feature off. Only called on purpose; ``suggest`` never writes. The config is rewritten as UTF-8 without a BOM,
    keeping every other key. The same list again writes nothing."""
    base = _base(home)
    try:
        with sc.machine_lock(base, wait=0):
            ctx = _kit_ctx(base, kit_path)
            core = _core(ctx)
            with cs.home_env(ctx):
                try:
                    cleaned = _clean_accounts(core, accounts)
                except ValueError as exc:
                    return _refuse(LINKED, "invalid", str(exc))
                with core.locked("config"):
                    config = _read_config(core)
                    before = _listed(config)
                    target = _config_path(core)
                    raw = target.read_bytes() if target.is_file() else b""
                    stored = config.get("switch_history")
                    already = stored == cleaned or (not cleaned and "switch_history" not in config)
                    if already and not raw.startswith(codecs.BOM_UTF8):
                        return {"outcome": "unchanged", "setting": LINKED, "enabled": bool(cleaned), "accounts": cleaned,
                                "before": before, "detail": "The linked accounts are already set that way."
                                if cleaned else "No accounts are linked, so the feature is already off.",
                                "notes": [SHARED_LOGIN_NOTE]}
                    config["switch_history"] = cleaned
                    core._write_json_atomic(target, config)
            return {"outcome": "enabled" if cleaned else "disabled", "setting": LINKED, "enabled": bool(cleaned),
                    "accounts": cleaned, "before": before, "notes": [SHARED_LOGIN_NOTE]}
    except _Refusal as exc:
        return _refuse(LINKED, exc.reason, exc.message)
    except sc.LifecycleBusy as exc:
        return {"outcome": "busy", "setting": LINKED, "reason": str(exc),
                "next_action": "Wait for the other operation to finish."}
    except Exception as exc:
        return {"outcome": "failed", "setting": LINKED, "error": f"{type(exc).__name__}: {exc}",
                "next_action": "Fix the cause, then ask again."}


# --------------------------------------------------------------------------- the helper switches
def _is_windows() -> bool:
    """The helper's Desktop jobs (wake, relink, rename, chat copying) only act on Windows."""
    return os.name == "nt"


def _switch(key, kind, default, label, help, page, section, platform="windows", **more) -> dict:
    return {"key": key, "kind": kind, "default": default, "label": label, "help": help, "page": page,
            "section": section, "platform": platform, **more}


# One declared table: the dashboard draws the switches from it and ``set_helper_switch`` validates against it. ``kind`` is
# bool, choice, number, list (chat IDs) or accounts (the linked-accounts list, which ``linked-accounts`` owns). The helper
# reads each key with its own default ("absent means default"), so a key that is not in ``config.json`` is the default.
HELPER_SWITCHES = (
    _switch("letter_wake", "bool", True, "Wake asleep Claude chats for letters",
            "Open a sleeping chat so it can read its letter. Each wake counts against the wake budget.",
            "crosstalk", "letters"),
    _switch("link_repair_offer", "choice", "ask", "Hermes link repair",
            "When the Hermes link breaks: ask you first, repair it automatically, or only report it.",
            "crosstalk", "apps", platform="any", choices=("ask", "auto", "off"),
            choice_labels={"ask": "Ask me", "auto": "Repair automatically", "off": "Only report"}),
    _switch("focus_notice_seconds", "number", 3, "Focus notice seconds",
            "How long the small notice shows before a chat is brought forward (0 means no notice).",
            "crosstalk", "advanced", minimum=0, maximum=30),
    _switch("switch_history", "accounts", [], "Linked Claude accounts",
            "Your own Claude logins. Only your own: these jobs move chat history between them.",
            "claude", "accounts", managed_by=LINKED),
    _switch("history_fix", "bool", True, "Keep phone history across an account switch",
            "After you switch accounts, chats keep their history on the phone. Needs linked accounts.",
            "claude", "accounts"),
    _switch("relink", "bool", True, "Re-enable Remote Control after a Desktop start or switch",
            "Turn Remote Control back on for the chats that had it.", "claude", "remote"),
    _switch("relink_kick", "bool", True, "Open one chat to start the relink when none is running",
            "After a restart no chat is awake to do the relink, so open the most recent one.", "claude", "remote"),
    _switch("relink_alert", "bool", True, "Show the Enabling Remote Control corner window",
            "A small window shows progress. It never takes focus.", "claude", "remote"),
    _switch("always_remote", "list", [], "Always relink these chats",
            "Chats to relink even when Desktop forgot they had Remote Control. One chat ID per line (local_ and a UUID).",
            "claude", "remote", advanced=True),
    _switch("rename_titles", "bool", True, "Fix chat names for the phone",
            "Rename chats that have generated names so the phone shows the real title. Only while Desktop is in "
            "front and you are idle.", "claude", "names"),
    _switch("rename_now", "bool", True, "Fix names right after a Desktop start",
            "Once per start: a notice, Desktop comes forward, and all names are fixed in one pass.", "claude", "names"),
    _switch("rename_min_idle_s", "number", 15, "Idle seconds before a rename",
            "How long you must be idle before a name is typed.", "claude", "names", advanced=True, minimum=2,
            maximum=3600),
    _switch("login_prompt", "bool", True, "Ask to sign the CLI in to the Desktop account",
            "When Desktop and the CLI use different accounts, ask once whether to sign the CLI in.", "claude", "signin"),
    _switch("chat_copy", "bool", False, "Copy chats between linked accounts",
            "Keep both accounts' chat lists identical by copying from the signed-in account to the other. Needs "
            "linked accounts.", "claude", "copy"),
    _switch("chat_copy_deletions", "bool", True, "Mirror deletions (kept in a trash folder)",
            "A chat deleted on one account is removed from the other. The record goes to a trash folder, never "
            "straight to deletion.", "claude", "copy"),
    _switch("chat_copy_snapshots", "bool", True, "Daily snapshot of both chat lists",
            "One zip per day, 7 kept, under EUTONOS's own folder.", "claude", "copy"),
)
_SWITCH_BY_KEY = {item["key"]: item for item in HELPER_SWITCHES}
BOOL_SWITCHES = tuple(item["key"] for item in HELPER_SWITCHES if item["kind"] == "bool")
MAX_ALWAYS_REMOTE = 64
CHAT_COPY = "chat_copy"
CHAT_COPY_SOURCE = "setting:chat_copy"
LEGACY_COPIER_NEEDLE = "chat_sync.py"
LEGACY_COPIER_STEP = ("Remove the personal 'Claude chat sync' shortcut from your Startup folder and stop its watcher, "
                      "then turn on Copy chats between linked accounts. Two copiers on the same folders would fight. "
                      "Then mark this done.")
KILL_SWITCHES = {"CROSSTALK_P0_RENAME": "Chat renaming is forced off by the CROSSTALK_P0_RENAME environment setting.",
                 "CROSSTALK_P0_FOCUS": "Bringing chats forward is forced off by the CROSSTALK_P0_FOCUS environment setting."}


def _available(spec: dict) -> bool:
    return spec["platform"] == "any" or _is_windows()


def _stored(spec: dict, config: dict):
    """The value the helper uses for this key: the stored one when it is the right kind, else the default."""
    default, raw, kind = spec["default"], config.get(spec["key"]), spec["kind"]
    if kind == "bool":
        return raw if isinstance(raw, bool) else default
    if kind == "choice":
        return raw if raw in spec["choices"] else default
    if kind == "number":
        ok = isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw)
        return min(spec["maximum"], max(spec["minimum"], raw)) if ok else default
    if kind in ("list", "accounts"):
        return [item.strip() for item in raw if isinstance(item, str) and item.strip()] if isinstance(raw, list) else []
    return default


def _startup_folder() -> Path:
    from . import installer_ports
    return installer_ports.startup_folder()


def legacy_chat_copier(startup=None) -> list:
    """Names of files in the Startup folder that start the owner's personal chat copier (they mention
    ``chat_sync.py``). Read-only; a shortcut stores its arguments as text, so the bytes are searched, not a process."""
    folder = Path(startup) if startup is not None else _startup_folder()
    needles = (LEGACY_COPIER_NEEDLE.encode("ascii"), LEGACY_COPIER_NEEDLE.encode("utf-16-le"))
    found = []
    try:
        entries = sorted(folder.iterdir(), key=lambda item: item.name.lower())
    except OSError:
        return found
    for path in entries:
        try:
            if path.suffix.lower() not in (".lnk", ".bat", ".cmd", ".vbs", ".ps1") or not path.is_file() \
                    or path.stat().st_size > 1 << 20:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if any(needle in raw for needle in needles):
            found.append(path.name)
    return found


def _row(spec: dict, config: dict) -> dict:
    row = {key: spec[key] for key in ("key", "kind", "label", "help", "page", "section", "platform", "default")}
    row.update(available=_available(spec), value=_stored(spec, config), stored=spec["key"] in config,
               advanced=bool(spec.get("advanced")))
    for name in ("choices", "choice_labels", "minimum", "maximum", "managed_by"):
        if name in spec:
            row[name] = spec[name]
    return row


def helper_switches_status(*, home=None, kit_path=None) -> dict:
    """Read-only: every helper switch with its effective value, whether it exists on this computer, and whether the
    owner's personal chat copier is still in the Startup folder. Never writes."""
    base = _base(home)
    try:
        ctx = _kit_ctx(base, kit_path)
        core = _core(ctx)
        with cs.home_env(ctx):
            config = _read_config(core)
    except _Refusal as exc:
        return _refuse(HELPER, exc.reason, exc.message)
    legacy = legacy_chat_copier() if _is_windows() else []
    return {"outcome": "ok", "setting": HELPER, "windows": _is_windows(),
            "switches": [_row(item, config) for item in HELPER_SWITCHES],
            "legacy_copier": {"found": bool(legacy), "files": legacy},
            "environment": [text for name, text in KILL_SWITCHES.items()
                            if os.environ.get(name, "").strip().lower() in ("off", "0", "false", "no")]}


def _clean_switch_value(core, spec: dict, value):
    kind = spec["kind"]
    if kind == "bool":
        if type(value) is not bool:
            raise ValueError(f"{spec['label']} must be on or off")
        return value
    if kind == "choice":
        if value not in spec["choices"]:
            raise ValueError(f"{spec['label']} must be one of {', '.join(spec['choices'])}")
        return value
    if kind == "number":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        if not ok or not spec["minimum"] <= value <= spec["maximum"]:
            raise ValueError(f"{spec['label']} must be a number from {spec['minimum']} to {spec['maximum']}")
        return int(value) if float(value).is_integer() else float(value)
    if kind == "list":
        if isinstance(value, str) or not hasattr(value, "__iter__"):
            raise ValueError(f"{spec['label']} must be a list of chat IDs")
        cleaned, keys = [], set()
        for item in value:
            text = item.strip().lower() if isinstance(item, str) else ""
            if not core.LOCAL_ID_RE.fullmatch(text):
                raise ValueError(f"{item!r} is not a chat ID (local_ and a UUID)")
            if text not in keys:
                keys.add(text)
                cleaned.append(text)
        if len(cleaned) > MAX_ALWAYS_REMOTE:
            raise ValueError(f"at most {MAX_ALWAYS_REMOTE} chats can be listed")
        return cleaned
    raise ValueError(f"{spec['label']} is set somewhere else")


def _legacy_step(base: Path, found: list) -> list:
    """Leave one pending step for the owner's personal copier (replacing an earlier copy of it)."""
    try:
        pending_actions.dismiss_source(base, CHAT_COPY_SOURCE, note="superseded by a later try")
        return [pending_actions.add(base, source=CHAT_COPY_SOURCE, harness="any", action=LEGACY_COPIER_STEP,
                                    why="The personal copier is still in the Startup folder: " + ", ".join(found))["id"]]
    except (pending_actions.PendingActionsError, sc.LifecycleBusy, OSError):
        return []


def _clear_legacy_step(base: Path) -> None:
    try:
        pending_actions.dismiss_source(base, CHAT_COPY_SOURCE, note="the copier is gone")
    except (pending_actions.PendingActionsError, sc.LifecycleBusy, OSError):
        pass


def set_helper_switch(key: str, value, *, home=None, kit_path=None) -> dict:
    """Set one helper switch. A switch turned on while the Crosstalk part is off switches the part on first (and the answer
    says the helper starts at the next login). A switch that does not exist on this computer is refused. Copy chats between
    linked accounts is refused, with a pending step, while the owner's personal copier is in the Startup folder. Asking for
    the value it already has writes nothing; a write is always an explicit value and keeps every other key."""
    base = _base(home)
    name = key if isinstance(key, str) else ""
    spec = _SWITCH_BY_KEY.get(name)
    if spec is None:
        return _refuse(HELPER, "unknown-switch", f"There is no Crosstalk switch called {key!r}.")
    if spec.get("managed_by"):
        return _refuse(name, "managed-elsewhere", f"{spec['label']} is changed with its own list ({spec['managed_by']}).")
    if not _available(spec):
        return _refuse(name, "not-available",
                       f"{spec['label']} is not available on this computer: it only acts on Windows.")
    follow = {"notes": [], "pending": []}
    try:
        ctx = _kit_ctx(base, kit_path)
        core = _core(ctx)
        try:
            with cs.home_env(ctx):
                cleaned = _clean_switch_value(core, spec, value)
        except ValueError as exc:
            return _refuse(name, "invalid", str(exc))
        turning_on = spec["kind"] == "bool" and cleaned is True
        if name == CHAT_COPY and cleaned:
            found = legacy_chat_copier() if _is_windows() else []
            if found:
                return {**_refuse(name, "legacy-copier",
                                  "Your personal chat copier is still in the Startup folder (" + ", ".join(found)
                                  + "). Remove that shortcut and stop its watcher, then turn this on. Two copiers on "
                                    "the same folders would fight."), "pending": _legacy_step(base, found)}
        if turning_on:
            follow = _ensure_part_on(base)
        with sc.machine_lock(base, wait=0):
            ctx = _kit_ctx(base, kit_path)
            core = _core(ctx)
            with cs.home_env(ctx):
                with core.locked("config"):
                    config = _read_config(core)
                    before = _stored(spec, config)
                    target = _config_path(core)
                    raw = target.read_bytes() if target.is_file() else b""
                    present = name in config and type(config[name]) is type(cleaned)
                    same = (present and config[name] == cleaned) or (
                        name not in config and spec["kind"] != "bool" and cleaned == spec["default"])
                    if same and not raw.startswith(codecs.BOM_UTF8):
                        return {"outcome": "unchanged", "setting": name, "key": name, "value": before,
                                "detail": f"{spec['label']} is already set that way.", "notes": list(follow["notes"]),
                                **({"pending": follow["pending"]} if follow["pending"] else {})}
                    config[name] = cleaned
                    core._write_json_atomic(target, config)
    except _Refusal as exc:
        return _refuse(name, exc.reason, exc.message)
    except sc.LifecycleBusy as exc:
        return {"outcome": "busy", "setting": name, "reason": str(exc),
                "next_action": "Wait for the other operation to finish."}
    except Exception as exc:
        return {"outcome": "failed", "setting": name, "error": f"{type(exc).__name__}: {exc}",
                "next_action": "Fix the cause, then ask again."}
    notes = list(follow["notes"])
    if name == CHAT_COPY and cleaned:
        _clear_legacy_step(base)
        if len(_stored(_SWITCH_BY_KEY["switch_history"], config)) < 2:
            notes.append("Chat copying needs two linked accounts. Add them under Linked Claude accounts first, or it "
                         "has nothing to do.")
    outcome = "changed" if spec["kind"] != "bool" else "enabled" if cleaned else "disabled"
    answer = {"outcome": outcome, "setting": name, "key": name, "value": cleaned, "before": before, "notes": notes}
    if follow["pending"]:
        answer["pending"] = follow["pending"]
    return answer


def _config_switches_on(config: dict) -> list:
    """The boolean helper switches that are on and exist on this computer, as ``[{"key", "label"}]``."""
    return [{"key": item["key"], "label": item["label"]} for item in HELPER_SWITCHES
            if item["kind"] == "bool" and _available(item) and _stored(item, config) is True]


def crosstalk_part_blockers(home=None, kit_path=None) -> list:
    """The switches that are on, as ``[{"key", "label"}]``: Crosstalk messaging and each boolean helper switch. Used to
    say what "turn everything off" switched off. It is empty when nothing can be read (EUTONOS not installed, no kit)."""
    base = _base(home)
    try:
        ctx = _kit_ctx(base, kit_path)
        core = _core(ctx)
        with cs.home_env(ctx):
            controls = core.controls_load()
            config = _read_config(core)
    except (_Refusal, OSError, ValueError):
        return []
    found = [{"key": "messaging", "label": "Crosstalk messaging (letters)"}] if controls["messaging"] == "on" else []
    return found + _config_switches_on(config)


def turn_everything_off(*, home=None, kit_path=None) -> dict:
    """Switch messaging and every boolean helper switch off, then the Crosstalk part. The owner's stop, the deny list, the
    wake budget, the linked-accounts list and the other settings are left as they are."""
    base = _base(home)
    try:
        ctx = _kit_ctx(base, kit_path)
        core = _core(ctx)
        found = crosstalk_part_blockers(home=base, kit_path=kit_path)
        messaging = set_crosstalk_messaging(False, home=base, kit_path=kit_path)
        if messaging["outcome"] not in ("disabled", "unchanged"):
            return {**messaging, "setting": EVERYTHING}
        with sc.machine_lock(base, wait=0):
            with cs.home_env(ctx):
                with core.locked("config"):
                    config = _read_config(core)
                    for item in HELPER_SWITCHES:
                        if item["kind"] == "bool" and _available(item) and config.get(item["key"]) is not False:
                            config[item["key"]] = False
                    core._write_json_atomic(_config_path(core), config)
    except _Refusal as exc:
        return _refuse(EVERYTHING, exc.reason, exc.message)
    except sc.LifecycleBusy as exc:
        return {"outcome": "busy", "setting": EVERYTHING, "reason": str(exc),
                "next_action": "Wait for the other operation to finish."}
    except Exception as exc:
        return {"outcome": "failed", "setting": EVERYTHING, "error": f"{type(exc).__name__}: {exc}",
                "next_action": "Fix the cause, then ask again."}
    turned = [item["label"] for item in found]
    part = _set_part(base, False)
    if part.get("outcome") not in ("disabled", "unchanged"):
        return {"outcome": "refused", "setting": EVERYTHING, "reason": part.get("reason") or "part-not-switched-off",
                "detail": "Every switch is now off, but the Crosstalk part could not be switched off: "
                          + str(part.get("detail") or part.get("error") or part.get("reason") or "no reason was given"),
                "next_action": part.get("next_action") or "Switch the part off from the Parts page.",
                "turned_off": turned}
    answer = {"outcome": "disabled", "setting": EVERYTHING, "turned_off": turned, "part": part.get("outcome"),
              "notes": list(part.get("notes") or [])}
    if part.get("pending"):
        answer["pending"] = part["pending"]
    return answer


# ------------------------------------------------------------------- the owner controls beyond messaging
def crosstalk_controls_status(*, home=None, kit_path=None) -> dict:
    """Read-only: the stop, messaging, the deny list and the wake budget from ``controls.json``, and the newest letters."""
    base = _base(home)
    try:
        ctx = _kit_ctx(base, kit_path)
        core = _core(ctx)
        with cs.home_env(ctx):
            controls = core.controls_load()
            limits = {name: list(bounds) for name, bounds in core.WAKE_BUDGET_LIMITS.items()}
            config = _read_config(core)
    except _Refusal as exc:
        return _refuse("crosstalk-controls", exc.reason, exc.message)
    from . import crosstalk_status
    on = controls["messaging"] == "on" or bool(_config_switches_on(config))
    return {"outcome": "ok", "setting": "crosstalk-controls", "halt": bool(controls["halt"]),
            "messaging": controls["messaging"] == "on", "deny": list(controls["deny"]),
            "wake_budget": dict(controls["wake_budget"]), "wake_budget_limits": limits, "idle": not on,
            "recent_letters": crosstalk_status.recent_letters(),
            "summary": "Halted: the owner's stop is set" if controls["halt"]
            else "Idle: everything is switched off" if not on else "On"}


def _controls_call(change, *, home=None, kit_path=None) -> dict:
    base = _base(home)
    try:
        with sc.machine_lock(base, wait=0):
            ctx = _kit_ctx(base, kit_path)
            core = _core(ctx)
            with cs.home_env(ctx):
                try:
                    return {"outcome": "ok", "controls": change(core)}
                except ValueError as exc:
                    return _refuse("crosstalk-controls", "invalid", str(exc))
                except Exception as exc:
                    if type(exc).__name__ == "ProbeError":
                        return _refuse("crosstalk-controls", "invalid", str(exc))
                    raise
    except _Refusal as exc:
        return _refuse("crosstalk-controls", exc.reason, exc.message)
    except sc.LifecycleBusy as exc:
        return {"outcome": "busy", "setting": "crosstalk-controls", "reason": str(exc),
                "next_action": "Wait for the other operation to finish."}
    except Exception as exc:
        return {"outcome": "failed", "setting": "crosstalk-controls", "error": f"{type(exc).__name__}: {exc}",
                "next_action": "Fix the cause, then ask again."}


def set_deny(action: str, key: str, *, home=None, kit_path=None) -> dict:
    """Block or allow one chat: a blocked chat never gets letters or Crosstalk actions."""
    if action not in ("block", "allow"):
        return _refuse("deny", "invalid", "The deny list takes block or allow.")

    def change(core):
        if not isinstance(key, str) or not (core.KEY_RE.fullmatch(key) or core.HERMES_KEY_RE.fullmatch(key)):
            raise ValueError(f"{key!r} is not a chat key (claude:, codex: or hermes: and its ID)")
        before = core.controls_load()["deny"]
        if (key in before) == (action == "block"):
            return {"changed": False, "deny": before}
        field = "deny_add" if action == "block" else "deny_remove"
        return {"changed": True, "deny": core.controls_update(**{field: key})["deny"]}

    answer = _controls_call(change, home=home, kit_path=kit_path)
    if answer["outcome"] != "ok":
        return answer
    result = answer["controls"]
    if not result["changed"]:
        return {"outcome": "unchanged", "setting": "deny", "deny": result["deny"],
                "detail": f"{key} is already {'blocked' if action == 'block' else 'allowed'}."}
    return {"outcome": "enabled" if action == "block" else "disabled", "setting": "deny", "deny": result["deny"]}


def set_wake_budget(values: dict, *, home=None, kit_path=None) -> dict:
    """Set the wake budget (``per_chat``, ``total``, ``window_s``); a field left out keeps its value."""
    if not isinstance(values, dict) or not values:
        return _refuse("wake-budget", "invalid", "Say what to set: per_chat, total, window_s.")

    def change(core):
        before = core.controls_load()["wake_budget"]
        unknown = set(values) - set(core.WAKE_BUDGET_DEFAULT)
        if unknown:
            raise ValueError(f"unknown wake budget field {sorted(unknown)[0]}")
        after = core.controls_update(wake_budget=values)["wake_budget"]
        return {"changed": after != before, "wake_budget": after}

    answer = _controls_call(change, home=home, kit_path=kit_path)
    if answer["outcome"] != "ok":
        return answer
    result = answer["controls"]
    if not result["changed"]:
        return {"outcome": "unchanged", "setting": "wake-budget", "wake_budget": result["wake_budget"],
                "detail": "The wake budget already matches."}
    return {"outcome": "changed", "setting": "wake-budget", "wake_budget": result["wake_budget"]}


def set_halt(halt: bool) -> dict:
    """The owner's stop: Stop or Resume, through the command the Crosstalk page always used (Stop never depends on the
    kit). The answer says whether the re-read controls show the change."""
    from . import crosstalk_status
    if type(halt) is not bool:
        return _refuse("halt", "invalid", "halt must be true or false")
    result = crosstalk_status.owner_control("halt" if halt else "resume")
    if result.get("ok"):
        notes = ["The owner's stop is set: nothing Crosstalk does will run until you resume." if halt
                 else "The owner's stop is cleared."]
        if result.get("degraded"):
            notes.append("The stop was written directly (the Crosstalk command was not available).")
        return {"outcome": "enabled" if halt else "disabled", "setting": "halt", "halt": halt, "notes": notes}
    return {"outcome": "failed", "setting": "halt",
            "error": result.get("message") or result.get("error") or "the change was not applied",
            "next_action": "Try again, or run: eutonos-crosstalk " + ("halt" if halt else "resume")}


# ------------------------------------------------------------------------------------------ together
def status(*, home=None, root=None, kit_path=None) -> dict:
    """All three settings, each with its own outcome (one being unavailable does not hide the others)."""
    return {"outcome": "ok", "settings": {
        MESSAGING: crosstalk_messaging_status(home=home, kit_path=kit_path),
        "ram": ram_status(root=root),
        LINKED: linked_accounts_status(home=home, kit_path=kit_path),
        HELPER: helper_switches_status(home=home, kit_path=kit_path)}}


def _set_switch_from_words(values: list, *, home=None, kit_path=None) -> dict:
    """``setting set crosstalk-switch KEY VALUE...``: on/off for a boolean switch, a number, a choice, or a chat ID list."""
    if len(values) < 2 or values[0] not in _SWITCH_BY_KEY:
        names = ", ".join(item["key"] for item in HELPER_SWITCHES if not item.get("managed_by"))
        return _refuse("crosstalk-switch", "invalid", f"crosstalk-switch needs a switch and a value. The switches are: {names}.")
    spec, rest = _SWITCH_BY_KEY[values[0]], values[1:]
    if spec["kind"] == "bool":
        word = rest[0].lower() if len(rest) == 1 else ""
        value = True if word == "on" else False if word == "off" else None
    elif spec["kind"] == "number":
        try:
            value = float(rest[0]) if len(rest) == 1 else None
        except ValueError:
            value = None
    elif spec["kind"] == "list":
        value = [] if [word.lower() for word in rest] in (["off"], ["none"]) else rest
    else:
        value = rest[0] if len(rest) == 1 else None
    if value is None:
        return _refuse("crosstalk-switch", "invalid", f"{spec['key']} needs a value of the right kind ({spec['kind']}).")
    return set_helper_switch(spec["key"], value, home=home, kit_path=kit_path)


def set_setting(name: str, values, *, home=None, root=None, kit_path=None) -> dict:
    """The one call the CLI makes: ``name`` is one of ``NAMES``; `values` is ``["on"]``/``["off"]`` for the switches
    and a list of accounts (or ``["off"]``/``["none"]`` for an empty list) for ``linked-accounts``."""
    values = [values] if isinstance(values, str) else list(values or [])
    if name not in NAMES:
        return _refuse(name, "unknown-setting", f"There is no setting called {name}. The settings are: {', '.join(NAMES)}.")
    if name == LINKED:
        return set_linked_accounts([] if [v.lower() for v in values] in (["off"], ["none"]) else values,
                                   home=home, kit_path=kit_path)
    if name == "crosstalk-switch":
        return _set_switch_from_words(values, home=home, kit_path=kit_path)
    if name == EVERYTHING:
        if values != ["off"]:
            return _refuse(name, "invalid", f"{name} only takes off: it turns every Crosstalk switch and then the part off.")
        return turn_everything_off(home=home, kit_path=kit_path)
    if len(values) != 1 or values[0] not in ("on", "off"):
        return _refuse(name, "invalid", f"{name} needs on or off.")
    on = values[0] == "on"
    if name == MESSAGING:
        return set_crosstalk_messaging(on, home=home, kit_path=kit_path)
    if name == "ram":
        return set_ram(root=root, capture=on, retrieval=on)
    return set_ram(root=root, **({"capture": on} if name == "ram-capture" else {"retrieval": on}))
