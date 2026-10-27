"""Self-repair: the local patch record and the post-update self-check (IPC campaign, "self-repair" slice).

When a Claude, Codex or Hermes update (or anything else) breaks EUTONOS, the user's own agent must be able to patch
EUTONOS locally so the user keeps working before an official release. This module holds the pieces that are not the
install machinery itself; ``installer.patch`` and ``installer.update`` use them:

* the ``local_patch`` install-record field (reason, checkout, source commit, base release, dirty flag, a digest of
  the changed package files and a short changed-file list) and the one status line built from it;
* whether an official release includes a patched fix (``fix_verdict``): "included" only when the patch's source
  commit is provably an ancestor of the release's source commit, otherwise "unknown - check the release notes";
* the app versions recorded at install and update (``Ports.app_version``: an executable's ``--version``, never an
  account or credential file; the Codex Microsoft Store app is its own ``codex-store`` entry, read from the installed
  package, so the command and the Store app never share one version) and ``self_check``, a read-only "an app
  updated, what broke?" report.

Everything here only reads, except ``build_wheel_from_checkout``, which writes a wheel into a scratch folder the
caller owns.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile

from . import home as eutonos_home
from . import install_record as ir
from . import tokenslang as ts
from .hidden_process import no_window

# Each entry is one separately versioned thing. ``codex`` is the ``codex`` command (``--version``); ``codex-store`` is
# the Codex desktop app's Microsoft Store package. They update independently, so they are never folded into one key.
APPS = ("claude", "codex", "codex-store", "hermes")
APP_LABELS = {"codex-store": "codex Store"}
SELF_CHECK_PROFILE = "eutonos.self-check.v1"
FIX_UNKNOWN = "unknown - check the release notes"
FIX_INCLUDED = "included"
# The files a wheel is built from. Only a change here makes a checkout "dirty" for a local patch; unrelated untracked
# files elsewhere in the checkout (RAM records, notes) never matter to the build.
BUILD_PATHS = ("src", "tools", "pyproject.toml", "docs/Crosstalk/QUICKSTART.md")
MAX_REASON = 300
MAX_LISTED_FILES = 20
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_VERSION = re.compile(r"\d+(?:\.\d+)+(?:[-+.][0-9A-Za-z.+-]*)?")
LETTER_TEST = ("Test one letter by hand: with the Crosstalk send tool (crosstalk_send), send one short status letter to "
               "one chat of another app, then read the reply back in that chat. Check `eutonos-crosstalk status` first. "
               "Never resend a letter reported accepted, held or uncertain; check status instead.")
# The repair checklist page's step IDs, in order (tests/test_checklists.py keeps the page and this list equal).
REPAIR_STEPS = ("self-check", "decide", "fix-in-checkout", "dry-run", "patch", "verify", "tell-user", "confirm-official")


# ------------------------------------------------------------------ small helpers
def parse_version(text) -> str | None:
    """The first dotted version number in a ``--version`` answer (``2.1.5 (Claude Code)`` gives ``2.1.5``)."""
    lines = str(text or "").strip().splitlines()
    found = _VERSION.search(lines[0]) if lines else None
    return found.group(0).rstrip(".-+") if found else None


def _family(name) -> str:
    """The app an item belongs to: ``claude-desktop`` and ``claude-org`` are Claude, ``codex-store`` is Codex, the
    rest are themselves."""
    text = str(name or "").lower()
    for family in ("claude", "codex"):
        if text.startswith(family):
            return family
    return text


def _label(name) -> str:
    """How a self-check line names an ``APPS`` entry (``codex-store`` reads ``codex Store``)."""
    return APP_LABELS.get(name, name)


def _clean_text(value, limit=MAX_REASON) -> str:
    return " ".join(str(value).split())[:limit]


def clean_reason(reason) -> str:
    text = _clean_text(reason or "")
    ts.require(bool(text), "invalid", "A local patch needs a reason: --reason \"WHAT IS BROKEN AND WHAT THE PATCH FIXES\".")
    return text


def short_commit(commit) -> str:
    return str(commit or "unknown")[:12]


# ------------------------------------------------------------------ git and the checkout
def _git(root, *args, timeout=60) -> tuple[int, str]:
    try:
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, stdin=subprocess.DEVNULL, **no_window())
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return done.returncode, done.stdout or ""


def inspect_checkout(checkout, *, allow_dirty=False, git=None) -> dict:
    """``{root, commit, dirty, dirty_files}`` for a checkout a wheel will be built from. A dirty build input refuses
    unless `allow_dirty`. `git` is the seam tests may replace: ``git(root, *args) -> (code, stdout)``."""
    git = git or _git
    root = Path(checkout)
    ts.require(root.is_dir() and (root / "pyproject.toml").is_file() and (root / "src" / "eutonos").is_dir(), "invalid",
               f"Not an EUTONOS checkout (no pyproject.toml and src/eutonos): {root}")
    root = root.resolve()
    code, commit = git(root, "rev-parse", "HEAD")
    commit = commit.strip()
    ts.require(code == 0 and _COMMIT.fullmatch(commit) is not None, "invalid",
               f"The checkout is not a git repository with a commit: {root}")
    code, listing = git(root, "status", "--porcelain", "--untracked-files=all", "--", *BUILD_PATHS)
    ts.require(code == 0, "invalid", f"git status failed in {root}")
    dirty_files = [line[3:].strip() for line in listing.splitlines() if line.strip()]
    ts.require(allow_dirty or not dirty_files, "dirty-checkout",
               "The checkout has uncommitted changes in " + ", ".join(dirty_files[:5])
               + (" ..." if len(dirty_files) > 5 else "")
               + ". Commit them (a branch is fine) so the patch names an exact commit, or pass --allow-dirty.")
    return {"root": str(root), "commit": commit, "dirty": bool(dirty_files), "dirty_files": dirty_files[:MAX_LISTED_FILES]}


def build_wheel_from_checkout(root, output, allow_dirty=False) -> Path:
    """Build the wheel with the checkout's own ``tools/build_wheel.py`` in a child interpreter (isolated: ``-I``).
    `allow_dirty` passes ``--allow-dirty`` (the builder refuses uncommitted core files otherwise)."""
    root, output = Path(root), Path(output)
    builder = root / "tools" / "build_wheel.py"
    ts.require(builder.is_file(), "invalid", f"The checkout has no tools/build_wheel.py: {root}")
    try:
        done = subprocess.run([sys.executable, "-I", str(builder), "--root", str(root), "--output", str(output),
                               *(["--allow-dirty"] if allow_dirty else [])],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
                              stdin=subprocess.DEVNULL, **no_window())
    except (OSError, subprocess.SubprocessError) as exc:
        raise ts.ReadError("build-failed", f"The wheel build could not run: {type(exc).__name__}: {exc}") from exc
    lines = [line for line in (done.stdout or "").splitlines() if line.strip()]
    wheel = Path(lines[-1]) if lines else None
    ts.require(done.returncode == 0 and wheel is not None and wheel.is_file(), "build-failed",
               "The wheel build failed: " + ((done.stderr or done.stdout or "").strip()[-400:] or f"exit {done.returncode}"))
    return wheel


def _wheel_files(wheel) -> dict[str, str]:
    from .installer_effects import _package_member
    found = {}
    with zipfile.ZipFile(wheel) as archive:
        for member in archive.namelist():
            if _package_member(member):
                found[member] = hashlib.sha256(archive.read(member)).hexdigest()
    return found


def _tree_files(site_packages) -> dict[str, str]:
    from .installer_effects import _package_member
    base = Path(site_packages) / "eutonos"
    found = {}
    if base.is_dir():
        for item in base.rglob("*"):
            relative = "eutonos/" + item.relative_to(base).as_posix()
            if item.is_file() and _package_member(relative):
                found[relative] = ir.sha256_file(item) or ""
    return found


def changed_files(wheel, active_site_packages) -> tuple[list[str], str]:
    """The package files whose bytes differ between the new wheel and the active runtime (added, changed or removed),
    sorted, and a sha256 over ``path NUL new-sha256`` lines for them (``-`` for a removed file)."""
    new, old = _wheel_files(wheel), _tree_files(active_site_packages)
    names = sorted(name for name in set(new) | set(old) if new.get(name) != old.get(name))
    digest = hashlib.sha256("\n".join(f"{name}\0{new.get(name, '-')}" for name in names).encode("utf-8")).hexdigest()
    return names, digest


# ------------------------------------------------------------------ the local_patch record
def base_release_of(active: dict) -> dict:
    """The official build a patch sits on: the active record's own, or, when the active generation is itself a
    patch, the base that patch recorded."""
    patch = active.get("local_patch")
    if isinstance(patch, dict) and isinstance(patch.get("base_release"), dict):
        return dict(patch["base_release"])
    return {"version": active.get("eutonos_version"), "artifact_id": (active.get("wheel") or {}).get("artifact_id")}


def local_patch_record(reason, checkout_info, active, changed, diff_sha256) -> dict:
    return {"reason": clean_reason(reason), "checkout": checkout_info["root"], "source_commit": checkout_info["commit"],
            "base_release": base_release_of(active), "dirty": bool(checkout_info["dirty"]), "diff_sha256": diff_sha256,
            "changed_files": list(changed[:MAX_LISTED_FILES]), "changed_count": len(changed)}


def patch_summary(value) -> dict | None:
    """A safe, bounded copy of a record's ``local_patch`` (an old record has none; a damaged one gives None)."""
    if not isinstance(value, dict) or not isinstance(value.get("reason"), str):
        return None
    files = value.get("changed_files")
    return {"reason": _clean_text(value["reason"]), "checkout": _clean_text(value.get("checkout") or "", 500),
            "source_commit": _clean_text(value.get("source_commit") or "", 64),
            "base_release": value.get("base_release") if isinstance(value.get("base_release"), dict) else None,
            "dirty": bool(value.get("dirty")), "diff_sha256": _clean_text(value.get("diff_sha256") or "", 64),
            "changed_files": [_clean_text(item, 300) for item in files[:MAX_LISTED_FILES]] if isinstance(files, list) else [],
            "changed_count": value.get("changed_count") if isinstance(value.get("changed_count"), int) else None}


def patch_line(value) -> str | None:
    """``local patch active: REASON (commit X)`` for a record's ``local_patch``, or None."""
    summary = patch_summary(value)
    if summary is None:
        return None
    return f"local patch active: {summary['reason']} (commit {short_commit(summary['source_commit'])})" \
           + (" [built from uncommitted changes]" if summary["dirty"] else "")


# ------------------------------------------------------------------ is the fix in the official release
def fix_verdict(patch, release_commit, *, git=None) -> dict:
    """Whether an official release includes a local patch's fix. ``fix`` is "included" only on proof: the patch was
    built from a clean commit, a git repository at the patch's checkout has both commits, and the patch's commit is an
    ancestor of the release's source commit. Anything else is "unknown - check the release notes"; this never says
    "not included" either, because a fix can arrive by another commit."""
    git = git or _git
    summary = patch_summary(patch)
    if summary is None:
        return {"fix": FIX_UNKNOWN, "detail": "the replaced record names no local patch"}
    if summary["dirty"]:
        return {"fix": FIX_UNKNOWN, "detail": "the patch was built from uncommitted changes, so no commit stands for it"}
    if not isinstance(release_commit, str) or _COMMIT.fullmatch(release_commit) is None:
        return {"fix": FIX_UNKNOWN, "detail": "the release names no source commit"}
    checkout = Path(summary["checkout"]) if summary["checkout"] else None
    if checkout is None or not checkout.is_dir():
        return {"fix": FIX_UNKNOWN, "detail": "the checkout the patch was built from is not on this machine"}
    for commit in (summary["source_commit"], release_commit):
        code, _ = git(checkout, "cat-file", "-e", commit + "^{commit}")
        if code != 0:
            return {"fix": FIX_UNKNOWN, "detail": f"commit {short_commit(commit)} is not in the patch's checkout"}
    code, _ = git(checkout, "merge-base", "--is-ancestor", summary["source_commit"], release_commit)
    if code == 0:
        return {"fix": FIX_INCLUDED, "detail": f"commit {short_commit(summary['source_commit'])} is an ancestor of the "
                                               f"release's source commit {short_commit(release_commit)}"}
    return {"fix": FIX_UNKNOWN, "detail": f"commit {short_commit(summary['source_commit'])} is not an ancestor of "
                                          f"{short_commit(release_commit)}; the fix may still be there under other commits"}


# ------------------------------------------------------------------ app versions
def read_app_versions(ports, now=None) -> dict:
    """``{"recorded_at", "apps": {name: {"state", "version"}}}``: what the install record keeps for the self-check."""
    apps = {}
    for name in APPS:
        try:
            row = ports.app_version(name)
        except Exception as exc:  # an unreadable app must never stop an install or update
            row = {"state": "unreadable", "version": None, "reason": f"{type(exc).__name__}"}
        apps[name] = {"state": row.get("state", "unreadable"), "version": row.get("version")}
    return {"recorded_at": ir.now_iso(now), "apps": apps}


def _compare(recorded, current) -> dict:
    before = (recorded or {}).get("version")
    after = (current or {}).get("version")
    row = {"recorded": before, "now": after, "recorded_state": (recorded or {}).get("state"),
           "now_state": (current or {}).get("state")}
    if recorded is None:
        row["state"] = "not-recorded"
    elif before and after:
        row["state"] = "unchanged" if before == after else "changed"
    elif not before and not after:
        row["state"] = "unchanged" if row["recorded_state"] == row["now_state"] else "unreadable"
    elif after and not before:
        row["state"] = "appeared" if row["recorded_state"] == "not-found" else "unreadable"
    else:
        row["state"] = "disappeared" if row["now_state"] == "not-found" else "unreadable"
    return row


# ------------------------------------------------------------------ the self-check
def _component_apps(package_dir) -> dict[str, set]:
    found = {}
    try:
        for path in sorted((Path(package_dir) / "components").glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            apps = (value.get("requires") or {}).get("apps") if isinstance(value, dict) else None
            if isinstance(value, dict) and isinstance(value.get("id"), str):
                found[value["id"]] = {_family(item) for item in apps or [] if isinstance(item, str)}
    except OSError:
        pass
    return found


def _crosstalk_state(base) -> dict:
    from . import crosstalk_status
    saved = os.environ.get(crosstalk_status.DEVICE_DIR_ENV)
    try:
        if saved is None:
            os.environ[crosstalk_status.DEVICE_DIR_ENV] = str(Path(base) / "crosstalk-probe")
        state = crosstalk_status.status()
    finally:
        if saved is None:
            os.environ.pop(crosstalk_status.DEVICE_DIR_ENV, None)
    return {key: state.get(key) for key in ("installed", "messaging", "halt", "denied", "reason")}


def _rollback_possible(layout, record) -> dict:
    previous_id = (record or {}).get("previous_gen")
    if not previous_id:
        return {"possible": False, "reason": "there is no previous generation"}
    try:
        previous = ir.read_record(layout, previous_id)
    except ts.ReadError:
        return {"possible": False, "reason": "the previous generation's record is gone"}
    runtime = layout.runtimes / str((previous.get("wheel") or {}).get("artifact_id"))
    if not runtime.is_dir():
        return {"possible": False, "reason": "the previous generation's runtime was removed"}
    from .installer import version_at_least
    floor = record.get("min_compatible")
    if not version_at_least(previous.get("eutonos_version"), floor):
        return {"possible": False, "reason": f"the previous version is older than the {floor} the data needs"}
    return {"possible": True, "previous_gen": previous_id, "version": previous.get("eutonos_version"),
            "official": not isinstance(previous.get("local_patch"), dict)}


def self_check(*, home=None, user_home=None, root=None, ports=None, report=None) -> dict:
    """Read-only: "an app updated, what broke?". Compares the app versions recorded at install or update with the
    ones readable now, adds Crosstalk's controls, the declared permission rules and the component and effect readbacks
    (doctor's), and names the likely broken piece in plain words with one next action: patch, rollback, wait, resume
    or install. It writes nothing and sends no letter; ``letter_test`` says how to test one by hand."""
    base = Path(home) if home is not None else eutonos_home.home()
    layout = ir.Layout(base)
    findings: list[dict] = []
    result: dict = {"profile": SELF_CHECK_PROFILE, "home": str(base), "findings": findings}

    def add(finding_id, severity, piece, plain, action):
        findings.append({"id": finding_id, "severity": severity, "piece": piece, "plain": plain, "action": action})

    try:
        state = ir.read_state(layout)
    except ts.ReadError as exc:
        add("install-state", "broken", "the install state", f"The install state cannot be read ({exc}).", "wait")
        return _finish(result, None, None, None)
    result["active"] = state.get("active")
    if state.get("pending"):
        add("pending-run", "broken", "an unfinished install or update",
            "An earlier install, update or rollback did not finish, so EUTONOS may be half changed.", "resume")
    if not state.get("active"):
        add("not-installed", "broken", "the EUTONOS install", "No EUTONOS generation is active on this machine.",
            "install")
        return _finish(result, None, None, state)
    try:
        record = ir.read_record(layout, state["active"])
    except ts.ReadError as exc:
        add("active-record", "broken", "the active install record", f"The active record cannot be read ({exc}).", "wait")
        return _finish(result, None, None, state)
    result["active_record"] = {"gen_id": record["gen_id"], "version": record["eutonos_version"],
                               "local_patch_line": patch_line(record.get("local_patch"))}
    if not ir.record_intact(layout, state, state["active"]):
        add("record-hash", "broken", "the active install record", "The active record changed on disk after the install "
            "wrote it.", "wait")
    # app versions: recorded at install/update against readable now
    from .installer_ports import Ports
    ports = ports or Ports()
    recorded = record.get("app_versions") if isinstance(record.get("app_versions"), dict) else None
    recorded_apps = (recorded or {}).get("apps") if isinstance((recorded or {}).get("apps"), dict) else {}
    apps = {}
    for name in APPS:
        try:
            now_row = ports.app_version(name)
        except Exception as exc:
            now_row = {"state": "unreadable", "version": None, "reason": type(exc).__name__}
        apps[name] = _compare(recorded_apps.get(name) if recorded is not None else None, now_row)
    result["apps"] = apps
    result["app_versions_recorded_at"] = (recorded or {}).get("recorded_at")
    changed_apps = {name: row for name, row in apps.items() if row["state"] == "changed"}
    # Crosstalk
    crosstalk = _crosstalk_state(base)
    result["crosstalk"] = crosstalk
    if crosstalk.get("installed"):
        if crosstalk.get("halt"):
            add("crosstalk-halted", "note", "Crosstalk", "Crosstalk is halted. That is the owner's control, not a "
                "fault.", "wait")
        elif crosstalk.get("messaging") != "on":
            add("crosstalk-messaging-off", "note", "Crosstalk", "Crosstalk messaging is off. That is the owner's "
                "control, not a fault.", "wait")
    # doctor's readbacks
    if report is None:
        from . import doctor
        report = doctor.report(base, user_home, root)
    declared = report.get("declared") or {}
    if ((report.get("readiness") or {}).get("state") == "unknown"
            or (report.get("active_record") or {}).get("state") == "unknown"
            or (declared.get("permissions") or {}).get("state") == "unknown"):
        add("doctor-unreadable", "unknown", "the doctor readback",
            "Doctor could not inspect the declared install state. Health is unknown; resolve its readback error and rerun.", "wait")
    perms = [item for item in (declared.get("permissions") or {}).get("items") or [] if isinstance(item, dict)]
    comps = [item for item in declared.get("components") or [] if isinstance(item, dict)]
    effects = [item for item in declared.get("effects") or [] if isinstance(item, dict)]
    comp_apps = _component_apps(Path(__file__).parent)
    perm_family = {item.get("id"): _family(item.get("app")) for item in perms}
    broken: list[tuple] = []  # (piece id, plain label, families, why)
    for item in perms:
        if item.get("selected") and item.get("state") == "missing":
            broken.append((f"permission {item['id']}", f"the {item.get('app')} setting {item.get('feature') or item['id']}",
                           {_family(item.get("app"))}, item.get("reason") or "it is declared but not present"))
        elif item.get("selected") and item.get("required") and item.get("state") == "unknown":
            add(f"permission-unread-{item['id']}", "unknown", f"permission {item['id']}",
                f"The {item.get('app')} setting {item['id']} could not be read back ({item.get('reason')}).", "wait")
    for item in comps:
        if item.get("selected") and item.get("state") == "missing":
            broken.append((f"component {item['id']}", f"the {item['id']} component", comp_apps.get(item["id"], set()),
                           item.get("reason") or "its readback says it is missing"))
        elif item.get("required") and item.get("selected") and item.get("state") == "unknown":
            add(f"component-unread-{item['id']}", "unknown", f"component {item['id']}",
                f"The {item['id']} component could not be read back ({item.get('reason')}).", "wait")
    for item in effects:
        if item.get("state") == "missing":
            entry = item.get("permission_entry")
            families = {perm_family[entry]} if entry in perm_family else set(comp_apps.get(item.get("component"), set()))
            broken.append((f"effect {item.get('effect_id')}", f"the file EUTONOS wrote for {item.get('effect_id')}",
                           families, "the recorded file is gone"))
    hand_edited = [item for item in effects if item.get("state") == "changed by you"]
    if hand_edited:
        add("changed-by-you", "note", "managed files", f"{len(hand_edited)} file(s) EUTONOS wrote were changed by "
            "someone else; EUTONOS keeps them as they are.", "wait")
    undeclared = report.get("undeclared") or []
    if undeclared:
        add("undeclared", "note", "settings outside the declared list", f"{len(undeclared)} setting(s) look like "
            "EUTONOS's but are not in the declared list (report only).", "wait")
    rollback = _rollback_possible(layout, record)
    result["rollback"] = rollback
    for piece, label, families, why in broken:
        cause = [name for name in sorted(changed_apps) if _family(name) in families]
        if cause:
            names = ", ".join(f"{_label(name)} {changed_apps[name]['recorded']} -> {changed_apps[name]['now']}"
                              for name in cause)
            add(piece.replace(" ", "-"), "broken", piece, f"{label} is not working ({why}). Likely cause: the {names} "
                "app update changed what EUTONOS relies on.", "patch")
        elif rollback["possible"]:
            add(piece.replace(" ", "-"), "broken", piece, f"{label} is not working ({why}), and no app version changed "
                "since EUTONOS recorded them, so the newest EUTONOS generation is the likelier cause.", "rollback")
        else:
            add(piece.replace(" ", "-"), "broken", piece, f"{label} is not working ({why}), and no earlier EUTONOS "
                "generation is available to go back to.", "patch")
    for name, row in changed_apps.items():
        if not any(item["severity"] == "broken" and item["action"] == "patch" for item in findings):
            add(f"app-{name}-changed", "note", f"the {_label(name)} app", f"The {_label(name)} app changed from "
                f"{row['recorded']} to {row['now']} since EUTONOS recorded it, but nothing EUTONOS checks is broken.",
                "none")
    for name, row in apps.items():
        if row["state"] in ("disappeared", "unreadable"):
            add(f"app-{name}-{row['state']}", "note", f"the {_label(name)} app", f"The {_label(name)} app version "
                f"{'can no longer be found' if row['state'] == 'disappeared' else 'could not be read'} "
                f"(it was {row['recorded'] or 'not readable'} when recorded).", "wait")
    return _finish(result, record, rollback, state)


_ACTION_ORDER = ("resume", "install", "patch", "rollback", "wait", "none")


def _finish(result, record, rollback, state) -> dict:
    findings = result["findings"]
    broken = [item for item in findings if item["severity"] == "broken"]
    pool = broken or findings
    action = next((name for name in _ACTION_ORDER if any(item["action"] == name for item in pool)), "none")
    result["outcome"] = "attention" if broken else "unknown" if any(item["severity"] == "unknown" for item in findings) else "ok"
    result["action"] = action
    result["likely_broken"] = broken[0]["plain"] if broken else None
    patched = bool(record) and isinstance(record.get("local_patch"), dict)
    result["next_action"] = {
        "install": "EUTONOS is not installed: install it first (eutonos-install install --wheel WHEEL --dry-run, then without --dry-run).",
        "resume": "Run `eutonos-install status`, inspect what it names, then `eutonos-install resume`.",
        "patch": "Fix EUTONOS in a checkout, then `eutonos-install patch --checkout PATH --reason \"TEXT\" --dry-run` and "
                 "without --dry-run. Follow the repair checklist page (eutonos/checklists/repair.md in the package). "
                 + LETTER_TEST,
        "rollback": ("Run `eutonos-install rollback" + (" --official" if patched else "") + " --dry-run`; if it looks right, "
                     "run it without --dry-run, then restart the apps. If that refuses, follow the repair checklist to patch."),
        "wait": "Resolve any unreadable state, rerun the readbacks, and tell the owner what remains unknown or needs their action. " + LETTER_TEST,
        "none": "Nothing looks broken. " + LETTER_TEST,
    }[action]
    result["letter_test"] = LETTER_TEST
    result["checklist"] = "repair"
    result["changes_nothing"] = True
    return result


def format_check(value) -> str:
    """A short plain-text view of ``self_check`` for a terminal."""
    lines = [f"EUTONOS self-check: {value.get('outcome')}"]
    if (value.get("active_record") or {}).get("local_patch_line"):
        lines.append(value["active_record"]["local_patch_line"])
    for name, row in (value.get("apps") or {}).items():
        lines.append(f"  {name}: {row['state']} (recorded {row['recorded']}, now {row['now']})")
    for item in value.get("findings") or []:
        lines.append(f"  [{item['severity']}] {item['plain']} -> {item['action']}")
    lines.append("Next: " + str(value.get("next_action")))
    return "\n".join(lines) + "\n"
