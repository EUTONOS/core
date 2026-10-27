"""The Setup program's side of the wizard: a read-only look at this computer, the consent text for the person's
choices, and the hand-off of their acceptance to the installer.

``Setup.exe`` (``installer/windows/EUTONOS.iss``) runs these through its bundled Python before it copies anything:

* ``inspect`` reads what is on this computer (git, an earlier hand install, a Remote Control launcher the person
  already has at login, which apps are present) and writes plain ``key=value`` lines for the wizard to read.
* ``consent-prepare`` renders the exact consent text for the chosen folder and parts, with the plain list of what
  Setup will do, and writes the text for display and a template for the acceptance.
* ``consent-accept`` is run only after the person ticked "I agree": it stamps the template with the acceptance time.
  ``eutonos-install install --consent-file`` then checks that file against this computer's own rendering
  (``consent.from_file``); a file that does not match is refused.
* ``suggest-accounts`` and ``apply-settings`` run after the install, only when the person ticked "Only I use this
  Windows login": the first lists the Claude emails Crosstalk has seen (a suggestion; nothing is written), the second
  saves the ones the person confirmed through ``feature_settings`` and reports each setting's outcome. A setting that
  could not be saved never fails the install; it is shown and a pending step is left for the next agent.
  ``apply-settings --update-mode manual|notify|auto`` also runs after every good install and saves the "Keeping EUTONOS
  up to date" choice (the default is ``auto``, which downloads a newer release in the background and then asks before
  installing it).
* ``open-dashboard`` is the finish page's "Open dashboard" button, run only when the person clicks it: it starts the
  installed ``eutonos-dashboard`` launcher when nothing is listening on the dashboard's port, then opens the browser
  once the dashboard answers. It is the same "open the dashboard, starting it if needed" the companion does.

Nothing here changes the computer outside the files it is told to write (and ``open-dashboard`` starts the installed
dashboard when asked). A person's own Remote Control launcher is only read, never changed.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import webbrowser

from . import component_support
from . import consent
from . import feature_settings as fs
from . import home as eutonos_home
from . import install_record as ir
from . import installer
from . import installer_effects as fx
from . import installer_ports as iports
from . import pending_actions
from . import prereq
from . import projects
from . import update_check
from . import setup_checkpoint as sc
from . import tokenslang as ts

REMOTE_CONTROL = "remote-control"
RC_NOTE = ("you already have one at login ({name}), so EUTONOS leaves its own off. Your launcher is never changed")
DASHBOARD_PORT = 8765  # eutonos-dashboard's own default port (a test keeps the two equal)
DASHBOARD_WAIT_SECONDS = 60
DASHBOARD_PROBE_SECONDS = 0.25
# The consent line for "Only I use this Windows login". Setup passes it to ``consent-prepare`` (``--note``) only; it
# reaches the engine inside the accepted consent file. Read from the inspection file so the wizard script never
# carries its own copy of the words.
SOLO_NOTE = ("You said only you use this Windows login. After the install, Setup asks you to confirm your own Claude "
             "login emails, then turns on Crosstalk linked accounts for exactly those, so chat history follows you when "
             "you switch between your accounts. List none and nothing is saved.")
LINKED_SETTING = "linked-accounts"
UPDATE_SETTING = "update-mode"


def _simon_problem():
    from . import component_simon
    return component_simon.bundled_problem()


# Parts whose bundle must be prepared in this build (kit and offline wheels) before the tick means anything:
# {part id: callable returning the problem text, or None when ready}.
PREPARED_CHECKS = {"simon": _simon_problem}
GOOD_OUTCOMES = ("enabled", "disabled", "changed", "unchanged")
MAX_PENDING_EMAILS_CHARS = 240


def _flat(value) -> str:
    return " ".join(str(value).split())


def _choices(items) -> dict:
    found = {}
    for item in items or []:
        name, _, value = str(item).partition("=")
        if name and value in ("on", "off"):
            found[name] = value
    return found


def part_label(row: dict) -> str:
    """The parts-page caption: every part is installed either way, so the tick says "switched on"."""
    if row["state"] == "required":
        return f"{row['name']} (always installed and on)"
    if row.get("in_build") is False:
        return f"{row['name']} (not in this build)"
    if not row["available"]:
        return f"{row['name']} (not available on this PC, so skipped)"
    return f"{row['name']} (installed; tick to switch on now)"


def inspect(home, *, ports=None, os_name=None, user_home=None, manifests=None, probe=None) -> dict:
    """What this computer already has, for the wizard. Read only. `user_home` is the person's home (None: the real
    one); `probe(name)` says whether an app is present (default: on PATH or ``~/.<name>`` exists)."""
    base = installer.resolve_home(home)
    os_name = os_name or iports.default_os_name()
    ports = ports or iports.Ports(os_name)
    person = Path(user_home) if user_home is not None else Path.home()
    layout = ir.Layout(base)
    try:
        state = ir.read_state(layout)
    except Exception:  # an unreadable state is reported, not repaired here
        state = {"active": None, "pending": None, "unreadable": True}
    legacy = fx.inspect_legacy(layout, ports, person, os_name)
    adopt = {key: value for key, value in legacy.items() if value}
    launchers = fx.find_remote_control_launchers(ports, os_name)
    report = prereq.install_prereq_check(probe_optional=False)
    git = prereq.git_row(report)
    needing_git = component_support.parts_needing(manifests if manifests is not None else installer.load_components(), "git")
    present = probe or (lambda name: bool(ports.which(name)) or (person / f".{name}").is_dir())
    rows = []
    for manifest in manifests if manifests is not None else installer.load_components():
        offered = installer.setup_parts([manifest])
        if not offered:
            continue
        row = dict(offered[0], default=manifest.get("default", "on"), available=True, note="")
        apps = list((manifest.get("requires") or {}).get("apps") or [])
        if apps and not any(present(name) for name in apps):
            row.update(available=False, note="none of " + ", ".join(apps) + " was found here, so it is skipped")
        if not git["ok"] and "git" in ((manifest.get("requires") or {}).get("tools") or []):
            if manifest.get("kind") == "core":  # the core cannot be left out: it installs, with less
                row.update(note="Git was not found, so project memory features (boards, notes, delivery) are unavailable until you install it")
            else:
                row.update(available=False, note="Git was not found, so it is unavailable until you install Git")
        problem = PREPARED_CHECKS[row["id"]]() if row["id"] in PREPARED_CHECKS else None
        if problem:  # the build does not carry what the part needs: say so, and do not offer the tick
            row.update(available=False, in_build=False, note="this build of Setup does not include it yet")
        if row["id"] == REMOTE_CONTROL and launchers:
            row.update(default="off", note=RC_NOTE.format(name=launchers[0]["name"]))
        if manifest.get("kind") == "core":
            row["default"] = "on"
        row["label"] = part_label(row)
        rows.append(row)
    rows.sort(key=lambda row: row["id"] != "core")  # core first, the rest in manifest order
    return {"home": str(base), "home_default": eutonos_home.is_default(base),
            "existing_install": bool(state.get("active")), "pending": bool(state.get("pending")),
            "git_ok": bool(git["ok"]), "git_guidance": git.get("guidance") or "",
            "git_unavailable": [item["id"] for item in needing_git] if not git["ok"] else [],
            "git_note": prereq.without_git_line(prereq.unavailable_without_git(report, needing_git)),
            "adopt": bool(adopt), "adopt_found": sorted(adopt), "remote_control_launchers": launchers,
            "parts": rows}


def write_ini(path, found: dict) -> None:
    """The inspection as ``[setup]`` key=value lines (the wizard reads them with ``GetIniString``)."""
    lines = ["[setup]"]
    flags = {"home_default": found["home_default"], "existing_install": found["existing_install"],
             "pending": found["pending"], "git_ok": found["git_ok"], "adopt": found["adopt"],
             "rc_launcher": bool(found["remote_control_launchers"])}
    lines += [f"{key}={int(value)}" for key, value in flags.items()]
    lines.append("git_guidance=" + _flat(found["git_guidance"]))
    lines.append("git_note=" + _flat(found["git_note"]))
    lines.append("git_unavailable=" + ",".join(found["git_unavailable"]))
    lines.append("rc_launcher_name=" + _flat((found["remote_control_launchers"] or [{}])[0].get("name", "")))
    lines.append("adopt_found=" + ",".join(found["adopt_found"]))
    lines.append("solo_note=" + _flat(SOLO_NOTE))
    lines.append("shared_login_note=" + _flat(fs.SHARED_LOGIN_NOTE))
    lines.append("parts=" + ",".join(row["id"] for row in found["parts"]))
    for row in found["parts"]:
        lines.append(f"{row['id']}_name={_flat(row['name'])}")
        lines.append(f"{row['id']}_label={_flat(row['label'])}")
        lines.append(f"{row['id']}_default={row['default']}")
        lines.append(f"{row['id']}_available={int(row['available'])}")
        lines.append(f"{row['id']}_note={_flat(row['note'])}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="ascii", errors="replace")  # GetIniString reads ANSI


def prepare_consent(home, *, choices=None, for_this_user=True, adopt=False, python_dir=None, installed_apps=False,
                    notes=None, user_home=None, os_name=None, manifests=None) -> dict:
    """The consent text and the template for an acceptance, for the chosen folder and parts."""
    base = installer.resolve_home(home)
    os_name = os_name or iports.default_os_name()
    run = installer.Run(base, kind="install", ports=iports.Ports(os_name), os_name=os_name, manifests=manifests,
                        for_this_user=for_this_user, user_home=user_home)
    setup = installer.setup_base(run, choices or {}, adopt)
    setup.update(python=str(python_dir) if python_dir else None, installed_apps=bool(installed_apps),
                 notes=list(notes or []))
    shown_home = user_home if user_home is not None else installer.default_user_home(base, for_this_user)
    text = consent.render(None, shown_home, os_name=os_name, setup=setup)
    return {"text": text, "text_sha256": consent.text_sha256(text), "surface": consent.SETUP_SURFACE, "accepted": False,
            "choices": {}, "setup": {key: setup[key] for key in consent.SETUP_EXTRAS},
            "lines": consent.setup_lines(setup)}


def parse_accounts(text) -> list[str]:
    """The emails in what the person typed: split on commas, semicolons and whitespace, blanks dropped. Nothing is
    judged here; ``feature_settings.set_linked_accounts`` refuses what is not an email or an account ID."""
    return [item for item in re.split(r"[,;\s]+", str(text or "")) if item]


def suggest_accounts(home, *, kit_path=None) -> dict:
    """The Claude emails Crosstalk has seen (a suggestion for the person to confirm). It needs the finished install,
    so Setup runs it after the install step. Never writes; any problem is an outcome the wizard shows as an empty
    field with a hint."""
    found = fs.suggest_linked_accounts(home=installer.resolve_home(home), kit_path=kit_path)
    if found.get("outcome") != "ok":
        return {"outcome": "unavailable", "emails": [], "detail": found.get("detail") or found.get("error") or ""}
    return {"outcome": "ok", "emails": list(found.get("suggested_emails") or []), "detail": ""}


def _pending_for_failure(base, name, wanted, found) -> tuple[str | None, str | None]:
    """Leave one step for the next agent when a setting could not be saved; returns ``(id, note)``."""
    if found.get("reason") == "not-installed":
        return None, "EUTONOS is not installed here, so no follow-up step was left."
    reason = found.get("detail") or found.get("error") or found.get("reason") or "unknown reason"
    if name == LINKED_SETTING:
        joined = " ".join(wanted)
        who = joined if len(joined) <= MAX_PENDING_EMAILS_CHARS else "the Claude login emails the person gave Setup"
        action = (f"Save the person's Claude logins for Crosstalk linked accounts: eutonos-install setting set "
                  f"{LINKED_SETTING} {who}. Setup could not: {reason}")
    else:
        action = f"Finish the {name} setting Setup could not apply: {reason}. See: eutonos-install setting status"
    try:
        entry = pending_actions.add(
            base, source=f"setup:{name}", harness="any", action=action,
            why="Setup saves these settings after the install and does not fail the install when one cannot be saved.")
        return entry["id"], None
    except (pending_actions.PendingActionsError, sc.LifecycleBusy, OSError) as exc:
        return None, f"The follow-up step could not be saved ({exc})."


def _line(name, wanted, found, step, note) -> str:
    label = {LINKED_SETTING: "Linked accounts", "crosstalk-messaging": "Crosstalk messaging", "ram": "RAM",
             UPDATE_SETTING: "Updates"}.get(name, name)
    outcome = found.get("outcome")
    if outcome == "skipped":
        return (f"{label}: none listed, so nothing was saved. You can add your own Claude logins later in the "
                "EUTONOS dashboard.")
    if outcome in GOOD_OUTCOMES:
        shown = ", ".join(wanted) if name == LINKED_SETTING else (found.get("mode") or outcome) if name == UPDATE_SETTING else outcome
        return f"{label}: {'already set' if outcome == 'unchanged' else 'saved'} ({shown})."
    reason = found.get("detail") or found.get("error") or found.get("reason") or outcome
    tail = (" A step was left for your next AI agent (eutonos-install pending list), and you can also set it in the "
            "EUTONOS dashboard." if step else " You can set it later in the EUTONOS dashboard.")
    return f"{label}: not saved ({reason}).{tail}" + (f" {note}" if note else "")


def apply_settings(home, wanted, *, kit_path=None, root=None) -> dict:
    """Save the settings the person chose in Setup, one outcome each. `wanted` may hold ``linked_accounts`` (a list
    of emails; an empty list writes nothing), ``crosstalk_messaging`` (bool) and ``ram`` (a dict with ``capture``
    and/or ``retrieval``, and ``root``) and ``update_mode`` (``manual``, ``notify`` or ``auto``, the "Keeping EUTONOS up
    to date" choice). Only what is present is touched. Each goes through its ``feature_settings``
    function, which needs the finished install, so this runs after the install step. A setting that fails does not
    fail Setup: it is reported and a pending step is added for the next agent."""
    base = installer.resolve_home(home)
    wanted = wanted if isinstance(wanted, dict) else {}
    results = []

    def record(name, asked, found):
        step = note = None
        if found.get("outcome") not in GOOD_OUTCOMES + ("skipped",):
            step, note = _pending_for_failure(base, name, asked, found)
        results.append({"setting": name, "outcome": found.get("outcome"), "result": found, "pending_id": step,
                        "line": _line(name, asked, found, step, note)})

    if "linked_accounts" in wanted:
        asked = wanted["linked_accounts"]
        if isinstance(asked, str) or not isinstance(asked, (list, tuple)) or not all(isinstance(item, str) for item in asked):
            record(LINKED_SETTING, [], {"outcome": "refused", "reason": "invalid",
                                        "detail": "the accounts must be a list of emails"})
        else:
            asked = [item.strip() for item in asked if item.strip()]
            record(LINKED_SETTING, asked, fs.set_linked_accounts(asked, home=base, kit_path=kit_path)
                   if asked else {"outcome": "skipped"})
    if "crosstalk_messaging" in wanted:
        record("crosstalk-messaging", [], fs.set_crosstalk_messaging(bool(wanted["crosstalk_messaging"]), home=base,
                                                                      kit_path=kit_path))
    if "update_mode" in wanted:
        asked = wanted["update_mode"]
        record(UPDATE_SETTING, [], update_check.set_mode(asked, home=base) if isinstance(asked, str) else
               {"outcome": "refused", "reason": "invalid", "detail": "the update mode must be manual, notify or auto"})
    if isinstance(wanted.get("ram"), dict):
        spec = wanted["ram"]
        record("ram", [], fs.set_ram(root=spec.get("root", root), capture=spec.get("capture"),
                                     retrieval=spec.get("retrieval")))
    attention = [item["setting"] for item in results if item["outcome"] not in GOOD_OUTCOMES + ("skipped",)]
    return {"outcome": "attention" if attention else "ok", "settings": results, "attention": attention,
            "lines": [item["line"] for item in results]}


def accept_template(template, *, now=None) -> dict:
    """The template stamped as accepted now. Call it only once the person has ticked "I agree"."""
    stamp = (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return dict(template, accepted=True, accepted_at=stamp)


def dashboard_roots(home) -> list[str]:
    """The project folders the dashboard is started on: every project registered in ``<home>/projects.json`` whose
    folder still exists; a new install has none, so the EUTONOS home itself (the dashboard shows a folder that is not
    a project as unavailable and writes nothing there)."""
    base = installer.resolve_home(home)
    before = os.environ.get("EUTONOS_HOME")
    os.environ["EUTONOS_HOME"] = str(base)  # projects.load() reads the registry of the home it is given
    try:
        registered = projects.load()["projects"]
    except (OSError, ValueError, ts.ReadError):
        registered = []
    finally:
        if before is None:
            os.environ.pop("EUTONOS_HOME", None)
        else:
            os.environ["EUTONOS_HOME"] = before
    found = [item["root"] for item in registered if isinstance(item.get("root"), str) and Path(item["root"]).is_dir()]
    return found or [str(base)]


def _listening(port=DASHBOARD_PORT, timeout=0.25) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _spawn(argv) -> None:
    """Start the dashboard hidden and on its own, so it keeps running after Setup closes."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, creationflags=flags)


def _tell(text) -> None:
    """Say it in a message box (the button runs windowless, so a printed error would never be seen)."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "EUTONOS", 0x10)
    except (AttributeError, ImportError, OSError):
        pass


def open_dashboard(home, *, os_name=None, listening=None, spawn=None, browser=None, wait=DASHBOARD_WAIT_SECONDS,
                   sleep=time.sleep, clock=time.monotonic) -> dict:
    """Open the dashboard in the browser, starting the installed ``eutonos-dashboard`` launcher first when nothing
    answers on its port. Idempotent (a running dashboard is only opened). Starts nothing but that launcher; every
    effect (the listening probe, the launch, the browser) is a seam. Returns ``{"outcome": "opened"|"error", ...}``."""
    base = installer.resolve_home(home)
    os_name = os_name or iports.default_os_name()
    listening, spawn, browser = listening or _listening, spawn or _spawn, browser or webbrowser.open
    url = f"http://127.0.0.1:{DASHBOARD_PORT}"
    started = False
    if not listening():
        launcher = base / "bin" / ("eutonos-dashboard.exe" if os_name == "windows" else "eutonos-dashboard")
        if not launcher.is_file():
            return {"outcome": "error", "error": f"the dashboard launcher is missing: {launcher}"}
        argv = [str(launcher), "serve", "--port", str(DASHBOARD_PORT)]
        for root in dashboard_roots(base):
            argv += ["--root", root]
        try:
            spawn(argv)
        except OSError as exc:
            return {"outcome": "error", "error": f"the dashboard could not be started: {exc}"}
        started = True
        deadline = clock() + wait
        while not listening():
            if clock() >= deadline:
                return {"outcome": "error", "error": f"the dashboard did not answer within {wait} seconds"}
            sleep(DASHBOARD_PROBE_SECONDS)
    browser(url)
    return {"outcome": "opened", "url": url, "started": started}


def repair_support(home, python_dir, bootstrap, wheel) -> dict:
    """Validate explicit support repair before Inno performs and records its one copy.

    The lifecycle lock checks that no operation is busy during preflight; it does
    not span Inno's later copy. Windows protects in-use files. No engine phase or
    consent is replayed and this helper copies nothing.
    """
    base, python_dir = Path(home), Path(python_dir)
    sources = [(source, base / "python" / source.relative_to(python_dir))
               for source in python_dir.rglob("*") if source.is_file()]
    sources += [(Path(bootstrap), base / "setup" / "bootstrap.py"),
                (Path(wheel), base / "setup" / Path(wheel).name)]
    if not (python_dir / "python.exe").is_file() or any(not source.is_file() for source, _ in sources):
        raise ValueError("Setup's bundled support files are incomplete")
    with sc.machine_lock(base, wait=0):
        state = ir.read_state(ir.Layout(base))
        if not state.get("active") and not state.get("pending"):
            raise ValueError("No active or pending install needs support-file repair")
        for source, target in sources:
            if any(parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction())
                   for parent in (target, *target.parents) if parent != base.parent):
                raise ValueError("Setup will not repair through a linked path")
    return {"outcome": "repair-ready", "home": str(base), "engine_replayed": False,
            "next_action": "Inno Setup copies and records its declared support files, then use the bundled bootstrap for recovery."}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="eutonos-setup-wizard", description="Setup wizard helpers (used by Setup.exe).")
    parser.add_argument("--user-home", help="the person's home (tests only; default: the real one)")
    sub = parser.add_subparsers(dest="command", required=True)
    look = sub.add_parser("inspect", help="read what this computer has; write key=value lines")
    look.add_argument("--home", required=True)
    look.add_argument("--ini", required=True, help="where to write the key=value lines")
    prep = sub.add_parser("consent-prepare", help="render the consent text for these choices")
    prep.add_argument("--home", required=True)
    prep.add_argument("--component", action="append", default=[], metavar="ID=on|off")
    prep.add_argument("--adopt", action="store_true")
    prep.add_argument("--python-dir")
    prep.add_argument("--installed-apps", action="store_true")
    prep.add_argument("--note", action="append", default=[])
    prep.add_argument("--text-out", required=True, help="the text to show (UTF-8)")
    prep.add_argument("--lines-out", help="the plain change list alone (UTF-8)")
    prep.add_argument("--template-out", required=True, help="the acceptance template (JSON)")
    dash = sub.add_parser("open-dashboard", help="start the dashboard if needed and open it in the browser")
    dash.add_argument("--home", required=True)
    done = sub.add_parser("consent-accept", help="stamp the template as accepted (after the person ticked I agree)")
    done.add_argument("--template", required=True)
    done.add_argument("--out", required=True)
    suggest = sub.add_parser("suggest-accounts", help="after the install: write the Claude emails Crosstalk has seen")
    suggest.add_argument("--home", required=True)
    suggest.add_argument("--out", required=True, help="one email per line (empty when there are none)")
    apply = sub.add_parser("apply-settings", help="after the install: save the settings the person chose in Setup")
    apply.add_argument("--home", required=True)
    apply.add_argument("--json", help="the settings as JSON: linked_accounts (list), crosstalk_messaging, ram")
    apply.add_argument("--linked-accounts-file", help="the emails the person confirmed (commas, spaces or lines)")
    apply.add_argument("--update-mode", choices=update_check.MODES, help="how EUTONOS updates (manual, notify or auto)")
    apply.add_argument("--result-out", help="one plain line per setting, for the finish page")
    repair = sub.add_parser("repair-support", help="validate support-file repair before Inno copies its declared files")
    repair.add_argument("--home", required=True)
    repair.add_argument("--python-dir", required=True)
    repair.add_argument("--bootstrap", required=True)
    repair.add_argument("--wheel", required=True)
    args = parser.parse_args(argv)
    if args.command == "open-dashboard":
        try:
            result = open_dashboard(args.home)
        except (OSError, ValueError, ts.ReadError) as exc:
            result = {"outcome": "error", "error": f"{type(exc).__name__}: {exc}"}
        if result["outcome"] != "opened":
            _tell("EUTONOS could not open the dashboard: " + result["error"] + ".\n\nYou can start it later by running "
                  "eutonos-dashboard serve in the bin folder of your EUTONOS folder.")
        print(json.dumps(result))
        return 0 if result["outcome"] == "opened" else 1
    try:
        if args.command == "suggest-accounts":
            found = suggest_accounts(args.home)
            Path(args.out).write_text("".join(item + "\n" for item in found["emails"]), encoding="ascii", errors="replace")
            print(json.dumps(found))
            return 0
        if args.command == "apply-settings":
            wanted = json.loads(Path(args.json).read_text(encoding="utf-8-sig")) if args.json else {}
            if not isinstance(wanted, dict):
                raise ValueError("the settings file must hold a JSON object")
            if args.linked_accounts_file:
                wanted["linked_accounts"] = parse_accounts(Path(args.linked_accounts_file).read_text(encoding="utf-8-sig"))
            if args.update_mode:
                wanted["update_mode"] = args.update_mode
            result = apply_settings(args.home, wanted)
            if args.result_out:
                Path(args.result_out).write_text("\n".join(result["lines"]) + "\n", encoding="ascii", errors="replace",
                                                 newline="\r\n")
            print(json.dumps({"outcome": result["outcome"], "attention": result["attention"], "lines": result["lines"],
                              "settings": [{key: item[key] for key in ("setting", "outcome", "pending_id")}
                                           for item in result["settings"]]}))
            return 0  # a setting that could not be saved is shown, never a failed install
        if args.command == "repair-support":
            print(json.dumps(repair_support(args.home, args.python_dir, args.bootstrap, args.wheel)))
            return 0
        if args.command == "inspect":
            found = inspect(args.home, user_home=args.user_home)
            write_ini(args.ini, found)
            print(json.dumps({"outcome": "ok", "adopt": found["adopt"], "git_ok": found["git_ok"]}))
            return 0
        if args.command == "consent-prepare":
            prepared = prepare_consent(args.home, choices=_choices(args.component), adopt=args.adopt,
                                       python_dir=args.python_dir, installed_apps=args.installed_apps,
                                       notes=args.note, user_home=args.user_home)
            Path(args.text_out).write_text(prepared["text"], encoding="utf-8-sig", newline="\r\n")
            if args.lines_out:
                Path(args.lines_out).write_text("\n".join(prepared["lines"]) + "\n", encoding="utf-8-sig", newline="\r\n")
            Path(args.template_out).write_text(json.dumps({key: prepared[key] for key in (
                "text", "text_sha256", "surface", "accepted", "choices", "setup")}), encoding="utf-8")
            print(json.dumps({"outcome": "ok", "text_sha256": prepared["text_sha256"]}))
            return 0
        template = json.loads(Path(args.template).read_text(encoding="utf-8"))
        Path(args.out).write_text(json.dumps(accept_template(template)), encoding="utf-8")
        print(json.dumps({"outcome": "ok"}))
        return 0
    except (OSError, ValueError, StopIteration, ts.ReadError, sc.LifecycleBusy) as exc:
        print(json.dumps({"outcome": "error", "error": f"{type(exc).__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
