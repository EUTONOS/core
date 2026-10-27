"""Keep the owner's linked Claude accounts' Code-chat lists identical, by copying (slice 3B.19; helper job j).

This is the personal `claude-chat-sync/chat_sync.py` brought into EUTONOS as a Crosstalk helper job. Claude Desktop
keeps one chat-list folder per account (<claude-code-sessions>/<account>/<org>) and reads it only at app start or
account switch; it never writes to the signed-out account's folder. It refuses to save into linked folders, so links
and junctions are never used here (and never created). Rule: copy ONE way, from the folder Desktop has loaded (the
active account) into the other linked account's folder, and never write into the active folder. When the owner
switches accounts the direction flips by itself.

Which folders: only accounts in the helper's linked-accounts list (config `switch_history`, the same list the history
fix uses; emails are mapped to account IDs through accounts.json) and only a folder the helper has SEEN Desktop load
(`Loaded N persisted sessions from <folder>` in main.log; remembered in chat-copy/seen.json). A folder never seen
loaded is never written to and no folder is ever created: a missing folder, or one that is a link, junction or other
reparse point, stops the copy with one log line.

What is copied: local_*.json (one per chat), deleted_* (Desktop's deletion markers) and archived-sessions.idx.
backlog/ and scheduled-tasks.json stay per account and are never touched.

Safety, as in the personal tool:
  - a file Desktop wrote less than SETTLE_S ago waits for the next pass; a .json/.idx file must parse; the copy goes
    through a temporary file in the target folder and an atomic replace; a file that changed while it was being
    read is skipped;
  - a chat record is removed from the other side only when the active side carries Desktop's own `deleted_<id>`
    marker, and the record is MOVED to <helper>/chat-copy/trash/, never deleted (config `chat_copy_deletions:
    false` turns the mirroring off); a record that exists only on the inactive side is left alone;
  - one zip of both folders per day (config `chat_copy_snapshots: false` turns it off), the last SNAPSHOTS_KEPT
    kept, under <helper>/chat-copy/snapshots/, written before the day's first copy;
  - halt pauses it (the caller passes `halted`); it is off by default (config `chat_copy: true` turns it on) and
    needs two linked accounts.

Only one copier may run: the helper job refuses while the personal watcher's Startup shortcut exists
(personal_copier_present), because two copiers copying the same way would only churn and double the trash.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import os
from pathlib import Path
import shutil
import stat
import time
import zipfile

SETTLE_S = 2.0               # a file Desktop wrote less than this long ago waits for the next pass
SNAPSHOTS_KEPT = 7
PERSONAL_SCRIPT = "chat_sync.py"
TMP_SUFFIX = ".chat-copy-tmp"
SEEN_FILE = "seen.json"
SCRIPT_SUFFIXES = (".cmd", ".bat", ".vbs", ".ps1", ".url")
MAX_STARTUP_FILE_BYTES = 262_144


def wanted(name: str) -> bool:
    return (name.startswith("local_") and name.endswith(".json")) or name.startswith("deleted_") \
        or name == "archived-sessions.idx"


def is_link(path) -> bool:
    """A symlink, a junction or any other reparse point. A folder like that is never copied into or from."""
    path = os.fspath(path)
    if os.path.islink(path):
        return True
    junction = getattr(os.path, "isjunction", None)
    if junction is not None and junction(path):
        return True
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def usable_folder(path) -> str | None:
    """None when `path` is a real existing folder, else why not."""
    path = os.fspath(path)
    if not os.path.isdir(path):
        return "missing"
    if is_link(path):
        return "link"
    return None


# ---------------------------------------------------------------- the personal copier
def startup_folder() -> Path | None:
    """Windows' per-user Startup folder, or None where there is none."""
    appdata = os.environ.get("APPDATA")
    if os.name != "nt" or not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def personal_copier_present(startup_dir=None) -> bool:
    """True when a .lnk in the Startup folder targets, or passes as an argument, the personal chat_sync.py watcher
    (a shortcut stores its target and arguments as text, ANSI and UTF-16, so both are searched as bytes). Script and
    URL files that mention it count too. `startup_dir` is injectable so tests never read the real folder; None means
    the real per-user Startup folder. Read-only; an unreadable folder or file counts as absent."""
    folder = Path(startup_dir) if startup_dir is not None else startup_folder()
    if folder is None:
        return False
    needle = PERSONAL_SCRIPT.encode("ascii")
    needles = (needle.lower(), PERSONAL_SCRIPT.encode("utf-16-le").lower())
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return False
    for entry in entries:
        suffix = entry.suffix.lower()
        if suffix != ".lnk" and suffix not in SCRIPT_SUFFIXES:
            continue
        try:
            if not entry.is_file() or entry.stat().st_size > MAX_STARTUP_FILE_BYTES:
                continue
            data = entry.read_bytes().lower()
        except OSError:
            continue
        if any(item in data for item in needles):
            return True
    return False


# ---------------------------------------------------------------------- the copier
class ChatCopier:
    """One copy pass at a time. `home` is <device>/helper/chat-copy; `say` writes one log line; `halted()` returns a
    truthy reason while Crosstalk is halted."""

    def __init__(self, home, *, clock=time.time, say=None, halted=None, settle_s: float = SETTLE_S):
        self.home = Path(home)
        self.clock = clock
        self.say = say or (lambda message: None)
        self.halted = halted or (lambda: None)
        self.settle_s = settle_s
        self.logged: set = set()      # lines already said, so a still-invalid file or a missing folder is said once

    def say_once(self, key, message: str) -> None:
        if key not in self.logged:
            self.logged.add(key)
            self.say(message)

    # ---- the folders the helper has seen Desktop load
    def seen_path(self) -> Path:
        return self.home / SEEN_FILE

    def seen(self) -> dict:
        try:
            value = json.loads(self.seen_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        folders = value.get("folders") if isinstance(value, dict) else None
        return {account: row for account, row in (folders or {}).items()
                if isinstance(account, str) and isinstance(row, dict) and isinstance(row.get("folder"), str)}

    def remember(self, account: str, org: str | None, folder: str) -> bool:
        """Record that Desktop loaded `folder` for `account` (the helper calls this for lines it read from main.log).
        Only an existing real folder is remembered. True when the record changed."""
        account = str(account).lower()
        if usable_folder(folder) is not None:
            return False
        folders = self.seen()
        row = {"org": str(org).lower() if org else None, "folder": str(folder)}
        if {k: folders.get(account, {}).get(k) for k in row} == row:
            return False
        folders[account] = dict(row, seen_ns=int(self.clock() * 1e9))
        self.home.mkdir(parents=True, exist_ok=True)
        tmp = self.seen_path().with_name(SEEN_FILE + TMP_SUFFIX)
        tmp.write_text(json.dumps({"version": 1, "folders": folders}, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.seen_path())
        return True

    # ---- one pass
    def run(self, active: dict, linked, *, deletions: bool = True, snapshots: bool = True) -> dict:
        """Copy the active account's folder into every other linked account's seen folder. `active` is {"id", "org",
        "folder"} (Desktop's loaded account); `linked` the set of linked account IDs. Returns a report with
        `state`: "halted", "skipped" (with `reason`) or "ran"."""
        if self.halted():
            return {"state": "halted"}
        account = str(active.get("id") or "").lower()
        linked = {str(item).lower() for item in linked}
        if account not in linked:
            return {"state": "skipped", "reason": "active-account-not-linked"}
        source = str(active.get("folder") or "")
        problem = usable_folder(source)
        if problem:
            self.say_once(("source", problem, source), f"chat copy: STOP: the loaded folder is {problem}; not copying")
            return {"state": "skipped", "reason": "source-" + problem}
        self.remember(account, active.get("org"), source)
        seen = self.seen()
        targets = {other: seen[other]["folder"] for other in sorted(linked - {account}) if other in seen}
        if not targets:
            return {"state": "skipped", "reason": "no-other-linked-folder-seen"}
        report = {"state": "ran", "copied": 0, "removed": 0, "waiting": 0, "invalid": 0, "left_alone": 0,
                  "targets": []}
        ready = {}
        for other, folder in targets.items():
            problem = usable_folder(folder)
            if problem or self._same(folder, source):
                self.say_once(("target", other, problem, folder),
                              f"chat copy: STOP: {other}'s folder is {problem or 'the loaded folder'}; not copying to it")
                report.setdefault("stopped", []).append({"account": other, "why": problem or "same-as-loaded"})
                continue
            ready[other] = folder
        if not ready:
            return dict(report, state="skipped", reason="no-usable-target")
        if snapshots:
            report["snapshot"] = self.snapshot({account: source, **ready})
        for other, folder in ready.items():
            if self.halted():
                report["state"] = "halted"
                break
            part = self.copy_once(source, folder, deletions=deletions)
            for key in ("copied", "removed", "waiting", "invalid", "left_alone"):
                report[key] += part[key]
            report["targets"].append(other)
        if report["copied"] or report["removed"]:
            self.say(f"chat copy: {account} -> {', '.join(report['targets'])}: copied {report['copied']}, "
                     f"removed {report['removed']} (kept in trash)")
        return report

    @staticmethod
    def _same(a, b) -> bool:
        try:
            return os.path.samefile(a, b)
        except OSError:
            return False

    def copy_once(self, src: str, dst: str, *, deletions: bool = True) -> dict:
        """Copy src -> dst. Never creates dst, never writes src."""
        counts = {"copied": 0, "removed": 0, "waiting": 0, "invalid": 0, "left_alone": 0}
        now = self.clock()
        src_names: set = set()
        try:
            entries = [entry for entry in os.scandir(src) if entry.is_file(follow_symlinks=False) and wanted(entry.name)]
        except OSError as error:
            self.say(f"chat copy: cannot list the loaded folder: {error!r}")
            return counts
        for entry in entries:
            src_names.add(entry.name)
            try:
                st = entry.stat()
            except OSError:
                continue
            if now - st.st_mtime < self.settle_s:
                counts["waiting"] += 1  # Desktop may still be writing; next pass
                continue
            target = os.path.join(dst, entry.name)
            try:
                tst = os.stat(target)
                if tst.st_size == st.st_size and int(tst.st_mtime) == int(st.st_mtime):
                    continue
            except FileNotFoundError:
                pass
            except OSError:
                continue
            try:
                with open(entry.path, "rb") as handle:
                    data = handle.read()
                after = os.stat(entry.path)
            except OSError:
                continue
            if (after.st_size, after.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
                counts["waiting"] += 1  # it changed while it was read
                continue
            if entry.name.endswith((".json", ".idx")):
                try:
                    json.loads(data.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    counts["invalid"] += 1
                    self.say_once(("invalid", entry.name, st.st_mtime_ns),
                                  f"chat copy: skip {entry.name}: not valid JSON yet")
                    continue
            if os.path.exists(target):
                with contextlib.suppress(OSError):
                    with open(target, "rb") as handle:
                        if handle.read() == data:
                            os.utime(target, (st.st_atime, st.st_mtime))
                            continue
            if self.halted():
                return counts
            tmp = f"{target}.{os.getpid()}{TMP_SUFFIX}"
            try:
                with open(tmp, "wb") as handle:
                    handle.write(data)
                os.utime(tmp, (st.st_atime, st.st_mtime))
                os.replace(tmp, target)
                counts["copied"] += 1
            except OSError as error:
                self.say(f"chat copy: could not copy {entry.name}: {error!r}")
                with contextlib.suppress(OSError):
                    os.remove(tmp)
        # Deletions: only where the loaded side carries Desktop's own deleted_ marker.
        try:
            names = os.listdir(dst)
        except OSError:
            return counts
        for name in names:
            if not (name.startswith("local_") and name.endswith(".json")) or name in src_names:
                continue
            chat = name[len("local_"):-len(".json")]
            if f"deleted_{chat}" not in src_names:
                counts["left_alone"] += 1  # exists only on the inactive account: never removed on a guess
                continue
            if not deletions or self.halted():
                continue
            trash = self.home / "trash"
            try:
                trash.mkdir(parents=True, exist_ok=True)
                stamp = datetime.datetime.fromtimestamp(self.clock()).strftime("%Y%m%d-%H%M%S")
                shutil.move(os.path.join(dst, name), str(trash / f"{stamp}-{name}"))
                counts["removed"] += 1
            except OSError as error:
                self.say(f"chat copy: could not move {name} to trash: {error!r}")
        return counts

    def snapshot(self, folders: dict) -> str:
        """One zip of the folders per day (`chats-YYYYMMDD.zip`, a directory per account ID inside); the last
        SNAPSHOTS_KEPT zips are kept. Returns "written", "exists" or "failed"."""
        snaps = self.home / "snapshots"
        path = snaps / f"chats-{datetime.datetime.fromtimestamp(self.clock()):%Y%m%d}.zip"
        if path.exists():
            return "exists"
        tmp = path.with_name(path.name + ".tmp")
        try:
            snaps.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
                for account, folder in folders.items():
                    for dirpath, dirnames, files in os.walk(folder):
                        dirnames[:] = [d for d in dirnames if not is_link(os.path.join(dirpath, d))]
                        for name in files:
                            full = os.path.join(dirpath, name)
                            if not is_link(full):
                                archive.write(full, os.path.join(account, os.path.relpath(full, folder)))
            os.replace(tmp, path)
            for old in sorted(p for p in snaps.glob("chats-*.zip"))[:-SNAPSHOTS_KEPT]:
                with contextlib.suppress(OSError):
                    old.unlink()
        except (OSError, zipfile.BadZipFile) as error:
            self.say(f"chat copy: snapshot failed: {error!r}")
            with contextlib.suppress(OSError):
                tmp.unlink()
            return "failed"
        self.say(f"chat copy: snapshot {path.name}")
        return "written"
