"""The update check's own small settings and its notice: ``<home>/settings/update-check.json``.

``eutonos-install update --check`` asks the public releases repository whether a newer release exists and downloads
nothing. Updates have three modes (the ``update-mode`` setting, chosen in the dashboard's Features page or with
``eutonos-install setting set update-mode manual|notify|auto``):

* ``manual``: nothing runs on its own. ``update --check`` and ``update`` work as they always did.
* ``notify``: a check about once a day (run by the companion; see ``update_background``). When it finds a newer verified
  release it leaves one pending action (``source`` ``update:available``) and the companion popup offers
  "Download and install" / "Later". Nothing is downloaded until the person asks.
* ``auto`` (the default for a new install): the same daily check, and when a newer verified release exists it is also
  downloaded and verified into ``<home>/updates/staged/<artifact_id>/`` in the background. The popup then asks
  "Install now" / "Later". Nothing is ever installed without that click; the install checks the signature and the
  sha256 again.

Without the companion nothing checks on its own in any mode; a check happens when someone (or a scheduled task the
owner sets up) runs ``update --check``, and ``update`` works as before. ``update-notice on|off`` still works as an alias
(``on`` is ``notify``, ``off`` is ``manual``).

    {"profile": "eutonos.update-check.v1", "updated_at": ISO-UTC, "mode": "manual" | "notify" | "auto",
     "notice": "manual" | "notify", "repo": "owner/name" | null, "last_checked": ISO-UTC | null,
     "last_attempt": ISO-UTC | null, "last_result": "up-to-date" | ... | null,
     "last_available": {"version", "artifact_id", "blocked"?} | null, "staged": {"version", "artifact_id"} | null}

``notice`` is the earlier field, still written (``notify`` for ``notify`` and ``auto``) so an older runtime that is rolled
back to reads the file; a file with only ``notice`` (written before modes existed) reads as that mode, so an earlier
choice is never lost. ``last_attempt`` and ``last_result`` record any check, including one that could not finish, so the
companion does not check more than about once a day and never nags about a failure. ``repo`` is the public releases
repository to ask (default ``EUTONOS/releases``; the ``EUTONOS_RELEASE_REPO`` environment variable still wins,
for tests). Rules as ``pending_actions``: a missing file means the defaults; a file that cannot be read or has the wrong
shape raises ``UpdateCheckError`` and is never read as the defaults.
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

from . import home as eutonos_home
from . import pending_actions
from . import release_source as rs
from . import setup_checkpoint as sc

PROFILE = "eutonos.update-check.v1"
MODES = ("manual", "notify", "auto")
DEFAULT_MODE = "auto"
NOTICES = ("manual", "notify")  # the earlier field's values
SOURCE = "update:available"
MODE_SETTING = "update-mode"
NOTICE_SETTING = "update-notice"  # the earlier name; still accepted as an alias
WRITE_WAIT_SECONDS = 30.0
_NOTICE_ALIASES = {"on": "notify", "notify": "notify", "off": "manual", "manual": "manual"}
MODE_LABELS = {"manual": "Manual (nothing runs on its own)", "notify": "Check and notify",
               "auto": "Check, download, then ask before installing"}
MAX_RESULT_CHARS = 120
CHECK_INTERVAL = datetime.timedelta(hours=23)   # "about once a day", so a login at the same hour still checks
RETRY_INTERVAL = datetime.timedelta(hours=2)    # a check that could not reach GitHub is tried again sooner


class UpdateCheckError(ValueError):
    """The update-check settings file is unreadable or has an unsupported shape."""


def path(home=None) -> Path:
    return (Path(home) if home is not None else eutonos_home.home()) / "settings" / "update-check.json"


def _notice_of(mode: str) -> str:
    return "manual" if mode == "manual" else "notify"


def _shaped(value: dict) -> dict:
    return {**value, "notice": _notice_of(value["mode"])}


def _defaults() -> dict:
    return _shaped({"mode": DEFAULT_MODE, "repo": None, "last_checked": None, "last_attempt": None,
                    "last_result": None, "last_available": None, "staged": None})


def _text_or_none(value, name, target):
    if value is not None and not isinstance(value, str):
        raise UpdateCheckError(f"the update-check settings have a bad {name}: {target}")
    return value


def _version_block(value, name, target, extra=()):
    if value is None:
        return None
    if not (isinstance(value, dict) and isinstance(value.get("version"), str) and isinstance(value.get("artifact_id"), str)):
        raise UpdateCheckError(f"the update-check settings have a bad {name}: {target}")
    found = {"version": value["version"], "artifact_id": value["artifact_id"]}
    for key in extra:
        if isinstance(value.get(key), str):
            found[key] = value[key]
    return found


def read(home=None) -> dict:
    target = path(home)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _defaults()
    except OSError as exc:
        raise UpdateCheckError(f"the update-check settings are unreadable: {target}") from exc
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise UpdateCheckError(f"the update-check settings are not valid JSON: {target}") from exc
    if not isinstance(value, dict) or value.get("profile") != PROFILE:
        raise UpdateCheckError(f"the update-check settings have an unsupported shape: {target}")
    chosen = value.get("mode")
    if chosen is None:  # written before modes existed: the earlier notice is the person's choice, so keep it
        chosen = value.get("notice") if value.get("notice") in NOTICES else None
    if chosen not in MODES:
        raise UpdateCheckError(f"the update-check settings have an unsupported shape: {target}")
    repo = value.get("repo")
    if repo is not None:
        try:
            rs.check_repo(repo)
        except ValueError as exc:
            raise UpdateCheckError(f"the update-check settings name a bad repository: {target}") from exc
    result = _text_or_none(value.get("last_result"), "last_result", target)
    return _shaped({"mode": chosen, "repo": repo,
                    "last_checked": _text_or_none(value.get("last_checked"), "last_checked", target),
                    "last_attempt": _text_or_none(value.get("last_attempt"), "last_attempt", target),
                    "last_result": result[:MAX_RESULT_CHARS] if result else result,
                    "last_available": _version_block(value.get("last_available"), "last_available", target, ("blocked",)),
                    "staged": _version_block(value.get("staged"), "staged", target)})


def _write(home, value: dict, now=None) -> None:
    keep = {key: value.get(key) for key in ("mode", "repo", "last_checked", "last_attempt", "last_result",
                                            "last_available", "staged")}
    sc._write_atomic(path(home), {"profile": PROFILE, "updated_at": sc.now_iso(now), **keep,
                                  "notice": _notice_of(keep["mode"])})


def configured_repo(home=None) -> str:
    """The releases repository to ask: the environment override, else the saved one, else the default."""
    return os.environ.get("EUTONOS_RELEASE_REPO") or read(home)["repo"] or rs.DEFAULT_REPO


def mode(home=None) -> str:
    """The saved update mode (the default when nothing was saved). Raises ``UpdateCheckError`` for a damaged file."""
    return read(home)["mode"]


def status(home=None) -> dict:
    """What the ``setting status`` line and the dashboard show. Never raises: a bad file shows as ``unreadable``."""
    try:
        found = read(home)
    except UpdateCheckError as exc:
        return {"outcome": "failed", "setting": MODE_SETTING, "error": str(exc)}
    return {"outcome": "ok", "setting": MODE_SETTING, "mode": found["mode"], "modes": list(MODES),
            "labels": dict(MODE_LABELS), "enabled": found["mode"] != "manual", "notice": found["notice"],
            "repo": found["repo"] or rs.DEFAULT_REPO, "last_checked": found["last_checked"],
            "last_attempt": found["last_attempt"], "last_result": found["last_result"],
            "last_available": found["last_available"], "staged": found["staged"]}


def _notes_for(value: str) -> list:
    if value == "manual":
        return ["Nothing runs on its own. Check with: eutonos-install update --check. Update with: eutonos-install update."]
    if value == "notify":
        return ["The companion checks about once a day and tells you when a newer release is available. "
                "Nothing is downloaded or installed until you ask. Without the companion nothing checks on its own."]
    return ["The companion checks about once a day, downloads and verifies a newer release in the background, then asks "
            "before installing it. Nothing is installed without your click. Without the companion nothing checks on its own."]


def set_mode(value, *, home=None, now=None, setting=MODE_SETTING) -> dict:
    """Choose manual, notify or auto (``on`` is notify and ``off`` is manual, the earlier ``update-notice`` words).
    Idempotent. Leaving the modes that offer an update clears the pending action; leaving ``auto`` removes anything
    staged."""
    legacy = setting == NOTICE_SETTING
    wanted = None if isinstance(value, (list, tuple)) else str(value).strip().lower()
    chosen = _NOTICE_ALIASES.get(wanted) if legacy else (wanted if wanted in MODES else _NOTICE_ALIASES.get(wanted))
    if chosen is None:
        detail = "update-notice needs on or off." if legacy else "update-mode needs manual, notify or auto."
        return {"outcome": "refused", "setting": setting, "reason": "invalid", "detail": detail, "next_action": detail}
    try:
        with sc.machine_lock(home, wait=WRITE_WAIT_SECONDS):
            current = read(home)
            if current["mode"] == chosen:
                return {"outcome": "unchanged", "setting": setting, "mode": chosen, "enabled": chosen != "manual",
                        "notice": _notice_of(chosen)}
            changes = {"mode": chosen}
            if chosen != "auto":
                changes["staged"] = None
            if chosen == "manual":
                changes["last_available"] = None
            _write(home, {**current, **changes}, now)
            if chosen == "manual":
                pending_actions.dismiss_source(home, SOURCE, note="updates were switched to manual", now=now)
            elif chosen == "notify" and current["staged"]:
                pending_actions.dismiss_source(home, SOURCE, note="the staged copy was removed; the next check offers it again",
                                               now=now)
        if chosen != "auto":
            from . import update_stage
            update_stage.prune(home)
    except (UpdateCheckError, pending_actions.PendingActionsError, OSError) as exc:
        return {"outcome": "failed", "setting": setting, "error": str(exc)}
    except sc.LifecycleBusy as exc:
        return {"outcome": "busy", "setting": setting, "reason": str(exc)}
    answer = {"outcome": "changed", "setting": setting, "mode": chosen, "enabled": chosen != "manual",
              "notice": _notice_of(chosen), "notes": _notes_for(chosen)}
    if legacy:
        answer["outcome"] = "enabled" if chosen != "manual" else "disabled"
    return answer


def set_notice(value, *, home=None, now=None) -> dict:
    """The earlier ``update-notice`` setting: ``on``/``notify`` is notify, ``off``/``manual`` is manual. Idempotent."""
    return set_mode(value, home=home, now=now, setting=NOTICE_SETTING)


def set_repo(repo, *, home=None, now=None) -> dict:
    """Point the check at another public releases repository (``owner/name``), or back at the default with None."""
    if repo is not None:
        rs.check_repo(repo)
    with sc.machine_lock(home, wait=WRITE_WAIT_SECONDS):
        _write(home, {**read(home), "repo": repo}, now)
    return {"outcome": "changed", "setting": "update-repo", "repo": repo or rs.DEFAULT_REPO}


def _ready_text(available: dict, installed) -> str:
    return (f"Tell the person that EUTONOS {available['version']} is ready to install: it is already downloaded and verified"
            + (f" (installed: {installed})" if installed else "")
            + ". Their companion offers Install now. To install it yourself, run: eutonos-install update --staged. "
              "Then mark this done.")


def _available_text(available: dict, installed) -> str:
    blocked = available.get("blocked")
    return (f"Tell the person that EUTONOS {available['version']} is available"
            + (f" (installed: {installed})" if installed else "")
            + (". An update cannot apply it (" + (blocked["detail"] if isinstance(blocked, dict) else str(blocked))
               + ") so they need to run Setup again. Then mark this done."
               if blocked else ". To update, run: eutonos-install update. Then mark this done."))


def _leave_pending(home, available: dict, installed, *, ready: bool, now=None) -> dict:
    pending_actions.dismiss_source(home, SOURCE, note="superseded by a newer check", now=now)
    return pending_actions.add(home, source=SOURCE,
                               action=_ready_text(available, installed) if ready else _available_text(available, installed),
                               why="Updates are set to notify or auto and a newer release was found.", harness="any", now=now)


def record_check(result: dict, *, home=None, now=None, code=None) -> dict:
    """Remember a finished check and, unless updates are manual, leave or clear the pending action. Returns what it did
    (``notice``: ``pending``, ``cleared``, ``unchanged`` or ``off``). A store that cannot be written never fails the check
    itself. ``code`` overrides the recorded result word."""
    try:
        with sc.machine_lock(home, wait=WRITE_WAIT_SECONDS):
            current = read(home)
            outcome = result.get("outcome")
            available = result.get("available") if outcome == "update-available" else None
            checked = outcome in ("update-available", "up-to-date", "no-release")
            stamp = sc.now_iso(now)
            word = code or (outcome if checked else f"{outcome}:{result.get('reason') or 'unknown'}")
            updated = {**current, "last_attempt": stamp, "last_result": str(word)[:MAX_RESULT_CHARS]}
            if checked:
                updated["last_checked"] = stamp
                updated["last_available"] = ({"version": available["version"], "artifact_id": available["artifact_id"],
                                              **({"blocked": available["blocked"]["detail"]} if available.get("blocked") else {})}
                                             if available else None)
                if not available or (current["staged"] or {}).get("artifact_id") != available["artifact_id"]:
                    updated["staged"] = None
            _write(home, updated, now)
            if current["mode"] == "manual" or not checked:
                return {"notice": "off" if current["mode"] == "manual" else "unchanged"}
            if available is None:
                pending_actions.dismiss_source(home, SOURCE, note="no newer release is waiting", now=now)
                return {"notice": "cleared"}
            installed = (result.get("installed") or {}).get("version")
            entry = _leave_pending(home, available, installed, ready=bool(updated["staged"]), now=now)
            return {"notice": "pending", "pending": entry["id"]}
    except (UpdateCheckError, pending_actions.PendingActionsError, sc.LifecycleBusy, OSError) as exc:
        return {"notice": "unrecorded", "detail": f"{type(exc).__name__}: {exc}"}


def record_attempt(code: str, *, home=None, now=None) -> dict:
    """Remember a check that could not finish (no network, no trusted key, ...) so it is not retried at once and never
    nags. Writes only the attempt time and a short result word."""
    try:
        with sc.machine_lock(home, wait=WRITE_WAIT_SECONDS):
            _write(home, {**read(home), "last_attempt": sc.now_iso(now), "last_result": str(code)[:MAX_RESULT_CHARS]}, now)
        return {"recorded": True}
    except (UpdateCheckError, sc.LifecycleBusy, OSError) as exc:
        return {"recorded": False, "detail": f"{type(exc).__name__}: {exc}"}


def mark_staged(available: dict, installed, *, home=None, now=None) -> dict:
    """A release was downloaded and verified into the staging folder: remember it and change the pending action to the
    ready wording. Does nothing unless updates are in ``auto`` and ``available`` is still the release last found."""
    try:
        with sc.machine_lock(home, wait=WRITE_WAIT_SECONDS):
            current = read(home)
            found = current["last_available"] or {}
            if current["mode"] != "auto" or found.get("artifact_id") != available["artifact_id"]:
                return {"marked": False, "reason": "no-longer-wanted"}
            _write(home, {**current, "staged": {"version": available["version"], "artifact_id": available["artifact_id"]}}, now)
            entry = _leave_pending(home, available, installed, ready=True, now=now)
            return {"marked": True, "pending": entry["id"]}
    except (UpdateCheckError, pending_actions.PendingActionsError, sc.LifecycleBusy, OSError) as exc:
        return {"marked": False, "detail": f"{type(exc).__name__}: {exc}"}


def after_update(*, home=None, now=None) -> dict:
    """An update finished: nothing is waiting any more. Clears the offer, the staged copy and the pending action."""
    try:
        with sc.machine_lock(home, wait=WRITE_WAIT_SECONDS):
            current = read(home)
            _write(home, {**current, "last_available": None, "staged": None}, now)
            pending_actions.dismiss_source(home, SOURCE, note="the update was installed", now=now)
        from . import update_stage
        update_stage.prune(home)
        return {"cleared": True}
    except (UpdateCheckError, pending_actions.PendingActionsError, sc.LifecycleBusy, OSError) as exc:
        return {"cleared": False, "detail": f"{type(exc).__name__}: {exc}"}


def _parse(stamp):
    try:
        found = datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=datetime.timezone.utc)


def due(home=None, *, now=None) -> bool:
    """Is a background check due? Never in manual mode; otherwise once the last attempt is old enough (a check that
    could not reach the network is retried sooner). A damaged settings file is never due (it is not read as the
    defaults)."""
    try:
        found = read(home)
    except UpdateCheckError:
        return False
    if found["mode"] == "manual":
        return False
    last = _parse(found["last_attempt"] or found["last_checked"])
    if last is None:
        return True
    moment = now if isinstance(now, datetime.datetime) else datetime.datetime.now(datetime.timezone.utc)
    word = found["last_result"] or ""
    return moment - last >= (RETRY_INTERVAL if word.startswith(("unavailable", "failed")) else CHECK_INTERVAL)
