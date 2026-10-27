"""The install consent screen (IPC2 contract e).

Only the person gives this consent: `accept` takes a typed "I agree" on an interactive console and refuses any
other input, and there is no flag that skips it. `show` and `plan` write nothing. `accept` returns the consent
part of the install record (contract b) and writes nothing either; the orchestrator keeps the record.

Run it as `python -m eutonos.consent show|plan|accept [--root PROJECT]`.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
from pathlib import Path
import sys

from . import permissions

TEXT_ID = "eutonos.consent.v1"
PHRASE = "I agree"
APPS = {"claude": "Claude", "claude-desktop": "Claude Desktop", "claude-org": "Your Claude organisation",
        "codex": "Codex", "hermes": "Hermes", "os": "This computer"}
# One line of what each entry the installer or a launch writes does.
WHAT = {
    "claude-launch-auto": "starts the Claude chats EUTONOS opens in auto mode (--permission-mode auto)",
    "set-remote-control": "lets Claude run Crosstalk's own Remote Control reconnect, in ~/.claude/settings.json",
    "send-message": "lets Claude run the restart relink message, in <project>/.claude/settings.local.json",
    "native-send-message": "lets Claude answer another Claude chat with SendMessage without asking, in <project>/.claude/settings.local.json",
    "crosstalk-reply": "lets Claude use Crosstalk's status, list and send tools, in ~/.claude/settings.json",
    "letter-rule": "adds one auto-mode sentence for chats EUTONOS starts, in the EUTONOS launch settings file",
    "record-keeping-rule": "adds one auto-mode sentence for record keeping, in the EUTONOS launch settings file",
    "codex-mcp-block": "adds Crosstalk's Codex tools with approval set to approve, in ~/.codex/config.toml",
    "codex-sandbox": "sets Codex to workspace-write and never-ask, in <project>/.codex/config.toml",
    "codex-record-rule": "adds one Codex rule allowing only `eutonos-settings delivery submit`, in <project>/.codex/rules/eutonos.rules",
    "hermes-gateway": "if Hermes is found, enables Crosstalk letters there through the Hermes CLI",
    "user-path": "adds the EUTONOS bin folder to your PATH",
    "user-env-home": "sets your user environment variable EUTONOS_HOME to the folder you chose, so other tools find it",
    "installed-apps-entry": "adds EUTONOS to Windows Installed apps so you can uninstall it there",
    "runtime-pip": "installs a pinned pip, offline, inside EUTONOS's own Python runtime folder",
    "simon-codex-rule": "adds one Codex rule allowing only `eutonos-simon` outside the sandbox, in ~/.codex/rules/eutonos-simon.rules",
}
# What a part that is off until the person ticks it lets agents do, beyond its permission entries.
PART_CONSENT = {"simon": "Lets agents move the mouse and type on this desktop (Simon, the Windows desktop-control helper)."}

HEAD = ("Let AI agents work together on this computer? If you agree, AI agents (Claude, Codex and, if installed, "
        "Hermes) can make changes on this computer and send each other messages without asking you each time, in "
        "the projects you add to EUTONOS. This is no more dangerous than letting one AI control your computer, but "
        "a mistake can bounce between agents faster than you can read it. You are responsible for watching what "
        "they do and for any bad outcome.")
STOP = ("Stop buttons: the Stop item on the E icon, the command `eutonos-crosstalk halt`, or telling any chat "
        "\"stop\". Messaging stays off until you agree here.")
DEFAULT_ON = ("On by default: agents commit and push their own record-keeping changes (board and RAM) without "
              "asking. You can turn this off below or later in Settings.")
TAIL = ("We never change a value you set. Everything added is recorded. Uninstall removes exactly what was added "
        "and keeps backups and your project data. Type I agree to continue, or press Enter to cancel.")
TAIL_SETUP = ("We never change a value you set. Everything added is recorded. Uninstall removes exactly what was added "
              "and keeps backups and your project data. Tick I agree to continue, or go back to change your choices.")
SETUP_SURFACE = "setup-wizard"
SETUP_EXTRAS = ("python", "installed_apps", "notes")  # descriptive lines the wizard adds; the rest is decided here


class ConsentRefused(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def off_parts() -> dict[str, str]:
    """``{part id: name}`` for the shipped parts that stay off until the person switches them on (manifest default
    ``off``). Their permission entries and consent lines are listed apart, as applying only to that part."""
    found = {}
    for path in sorted((Path(__file__).resolve().parent / "components").glob("*.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(manifest, dict) and manifest.get("default") == "off" and isinstance(manifest.get("id"), str):
            found[manifest["id"]] = str(manifest.get("name") or manifest["id"])
    return found


def _optional(data: dict) -> list[dict]:
    return [entry for entry in permissions.entries(data)
            if entry["writer"] in ("installer", "launch-flag") and entry["default"] == "on" and not entry["required"]]


def setup_lines(setup: dict) -> list[str]:
    """The plain change list the Setup wizard adds to the consent text, from what the person chose. `setup` has
    ``location``, ``parts`` (rows ``{id, name, state}`` with state required, on or off; the text says switched on
    or off, because every part this computer can run is installed either way), ``for_this_user``, ``adopt``,
    ``python`` (the private Python folder or None), ``installed_apps`` and ``notes`` (extra plain lines, such as a
    Remote Control launcher the person already has)."""
    lines = [f"- Install EUTONOS into {setup['location']}."]
    if setup.get("python"):
        lines.append(f"- Put a private copy of Python in {setup['python']}. EUTONOS uses it, and it stays until you uninstall.")
    parts = []
    switched_on = []
    for row in setup.get("parts") or []:
        parts.append(f"{row['name']} ({'required' if row['state'] == 'required' else 'switched ' + row['state']})")
        if row["state"] == "on" and row["id"] in PART_CONSENT:
            switched_on.append(f"- {row['name']} is switched on. {PART_CONSENT[row['id']]}")
    if parts:
        lines.append("- Parts: " + ", ".join(parts) + ". Every part this computer can run is installed. A part switched "
                     "off adds nothing outside the EUTONOS folder until you switch it on later.")
    lines += switched_on
    if setup.get("for_this_user"):
        lines.append("- This is your real install, so it uses your own Claude, Codex and Hermes settings, "
                     "and your PATH, as listed above.")
        if not setup.get("default_home"):
            lines.append("- Set the user environment variable EUTONOS_HOME to that folder, so tools outside EUTONOS "
                         "find it. Uninstall removes it, unless you changed it.")
    if setup.get("installed_apps"):
        lines.append("- Add EUTONOS to Windows Installed apps. Removing it there runs the EUTONOS uninstall.")
    if setup.get("adopt"):
        lines.append("- Take over the earlier hand install found on this computer: its files and settings are "
                     "backed up and repointed, and uninstall puts them back.")
    lines += [f"- {note}" for note in setup.get("notes") or []]
    return lines


def render(root=None, home=None, *, os_name: str | None = None, data: dict | None = None,
           setup: dict | None = None) -> str:
    """The exact consent text for this machine, as plain text. With `setup` (the Setup wizard), the text also lists
    what that setup will do and ends with the wizard's wording of the closing line."""
    data = data or permissions.load()
    optional_parts = off_parts()
    shipped = permissions.entries(data)
    general = [entry for entry in shipped if entry["component"] not in optional_parts]
    lines = [HEAD, "", STOP, "", "What this turns on:"]
    for entry in general:
        if entry["writer"] in ("installer", "launch-flag") and entry["default"] == "on":
            what = WHAT.get(entry["id"], entry["feature"])
            lines.append(f"- {permissions_label(entry)}: {what} [{entry['id']}]. Why: {entry['why']}")
    for entry in general:  # written only by the Setup program's own install, never by the typed console route
        if entry["writer"] == "installer" and entry["default"] == "off":
            what = WHAT.get(entry["id"], entry["feature"])
            lines.append(f"- {permissions_label(entry)}: {what} [{entry['id']}]. Only when Setup.exe installs for you; "
                         f"otherwise off. Why: {entry['why']}")
    held = [entry for entry in shipped if entry["default"] == "held"]
    if held:
        lines.append("Held for later, nothing is written now: " + ", ".join(entry["id"] for entry in held) + ".")
    for part, name in optional_parts.items():  # a part that is off unless the person switches it on
        own = [entry for entry in shipped if entry["component"] == part and entry["default"] == "on"
               and entry["writer"] in ("installer", "launch-flag")]
        if not own and part not in PART_CONSENT:
            continue
        lines += ["", f"Only if you switch on {name} (it is off unless you tick it, now or later):"]
        if part in PART_CONSENT:
            lines.append(f"- {PART_CONSENT[part]}")
        for entry in own:
            what = WHAT.get(entry["id"], entry["feature"])
            lines.append(f"- {permissions_label(entry)}: {what} [{entry['id']}]. Why: {entry['why']}")
    lines += ["", "What you set by hand afterwards:"]
    steps = data["guided_steps"]
    for entry in shipped:
        if entry["writer"] in ("guided", "admin-only") and entry["default"] != "held":
            lines.append(f"- {permissions_label(entry)} [{entry['id']}]: {steps[entry['id']]}")
    lines += ["", DEFAULT_ON]
    optional = _optional(data)
    if optional:
        lines.append("Optional items, each on by default and yours to turn off: " + ", ".join(entry["id"] for entry in optional) + ".")
    lines += ["", "Recommended, not required:"]
    for item in permissions.tuning(home, data=data):
        current = "not set" if item["current"] is None else item["current"]
        lines.append(f"- {item['app'].title()} {item['setting']}: recommended {item['recommended']}, yours {current}.")
    if setup is not None:
        lines += ["", "What this setup will do:", *setup_lines(setup)]
    lines += ["", TAIL if setup is None else TAIL_SETUP]
    return "\n".join(lines) + "\n"


def permissions_label(entry: dict) -> str:
    return APPS.get(entry["app"], entry["app"])


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def from_file(path, root, base, *, os_name: str | None = None, setup: dict, user_home=None) -> dict:
    """The consent block for what the Setup wizard showed. The file is JSON written by the wizard: ``accepted`` (true),
    ``text`` (exactly what the person saw), ``text_sha256``, ``choices``, ``surface`` and ``accepted_at``. This
    machine re-renders the text for `setup` (location, parts, real-install and adopt marks decided by the engine,
    plus the wizard's descriptive extras from the file's ``setup``); the file is refused unless its text is
    that rendering, its hash matches, the person accepted and the choices are known. Nothing is written. The
    block keeps the shown text and its hash, and ``base_sha256`` of the standard text so the doctor can compare."""
    try:
        with open(path, encoding="utf-8") as handle:
            given = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ConsentRefused("unreadable", f"the consent file could not be read: {type(exc).__name__}") from exc
    if not isinstance(given, dict) or given.get("accepted") is not True:
        raise ConsentRefused("declined", "the consent file does not record an acceptance")
    if given.get("surface") != SETUP_SURFACE:
        raise ConsentRefused("wrong-surface", f"expected {SETUP_SURFACE}, found {given.get('surface')!r}")
    shown = given.get("text")
    if not isinstance(shown, str) or text_sha256(shown) != given.get("text_sha256"):
        raise ConsentRefused("hash-mismatch", "the consent file's text does not match its hash")
    stamp = given.get("accepted_at")
    try:
        datetime.datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ConsentRefused("bad-time", "the consent file has no valid acceptance time") from exc
    data = permissions.load()
    extras = given.get("setup") if isinstance(given.get("setup"), dict) else {}
    setup = {**setup, **{key: extras[key] for key in SETUP_EXTRAS if key in extras}}
    if shown != render(root, user_home, os_name=os_name, data=data, setup=setup):
        raise ConsentRefused("text-mismatch", "this computer would show different text for these choices")
    known = {entry["id"] for entry in permissions.entries(data) if entry["default"] == "on"
             and entry["writer"] in ("installer", "launch-flag")}
    choices = given.get("choices") if isinstance(given.get("choices"), dict) else {}
    if any(name not in known or value not in ("on", "off") for name, value in choices.items()):
        raise ConsentRefused("unknown-choice", "the consent file names an item this version does not know")
    full = {name: choices.get(name, "on") for name in sorted(known)}
    return {"text_id": TEXT_ID, "text_sha256": text_sha256(shown), "base_sha256": text_sha256(
            render(root, user_home, os_name=os_name, data=data)), "surface": SETUP_SURFACE, "accepted_at": stamp,
            "choices": full, "text": shown}


def show(root=None, home=None, *, stream=None, os_name: str | None = None) -> str:
    text = render(root, home, os_name=os_name)
    (stream or sys.stdout).write(text)
    return text


def plan(root, home=None, *, choices: dict | None = None, os_name: str | None = None) -> dict:
    """What agreeing would do for this project: the text hash and the exact additions per file. Writes nothing."""
    text = render(root, home, os_name=os_name)
    return {"text_id": TEXT_ID, "text_sha256": text_sha256(text),
            "permissions": permissions.plan(root, home, choices=choices, os_name=os_name),
            "readbacks": permissions.readbacks(root, home)}


def accept(root=None, home=None, *, stdin=None, stdout=None, surface: str = "terminal", now=None,
           os_name: str | None = None) -> dict:
    """Show the text and take a typed "I agree". Returns the record's consent block; raises ConsentRefused for
    a non-interactive console, anything other than the typed phrase, or an unknown item to turn off."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for stream in (stdin, stdout):
        if not getattr(stream, "isatty", lambda: False)():
            raise ConsentRefused("non-interactive", "consent is only taken from a person at an interactive console")
    data = permissions.load()
    text = render(root, home, os_name=os_name, data=data)
    stdout.write(text)
    optional = [entry["id"] for entry in _optional(data)]
    stdout.write("\nOptional items stay on. Type the ids to turn off (comma separated), or press Enter to keep them all: ")
    stdout.flush()
    answer = stdin.readline().strip()
    off = [item.strip() for item in answer.split(",") if item.strip()]
    for name in off:
        if name not in optional:
            raise ConsentRefused("unknown-choice", name)
    stdout.write(f"Type {PHRASE} to continue, or press Enter to cancel: ")
    stdout.flush()
    if stdin.readline().strip() != PHRASE:
        raise ConsentRefused("declined", "consent was not given")
    stamp = (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    choices = {entry["id"]: ("off" if entry["id"] in off else "on")
               for entry in permissions.entries(data) if entry["default"] == "on" and entry["writer"] in ("installer", "launch-flag")}
    return {"text_id": TEXT_ID, "text_sha256": text_sha256(text), "surface": surface, "accepted_at": stamp,
            "choices": choices}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="EUTONOS install consent. `accept` needs a person at a console.")
    parser.add_argument("command", choices=("show", "plan", "accept"))
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    if args.command == "show":
        show(args.root)
        return 0
    if args.command == "plan":
        print(json.dumps(plan(args.root), indent=2, default=str))
        return 0
    try:
        receipt = accept(args.root)
    except ConsentRefused as refusal:
        print(json.dumps({"accepted": False, "reason": refusal.reason, "detail": refusal.detail}))
        return 2 if refusal.reason == "non-interactive" else 1
    print(json.dumps({"accepted": True, "consent": receipt}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
