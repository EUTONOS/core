"""Background update work, hosted by the companion: the daily check, the offer, and "Install now".

The companion is already EUTONOS's one resident process (a declared scheduled task), so the daily check lives there and
adds no scheduled task, Startup item or other outside effect. Without the companion nothing here runs; ``eutonos-install
update --check`` and ``update`` still work by hand in every mode.

* ``run_check`` is the whole background pass, off the UI thread: in ``manual`` mode it does nothing; otherwise it asks the
  releases source for a newer verified release (``installer.check_update``, which leaves the ``update:available`` pending
  action) and, in ``auto`` mode, stages it (``update_stage``: download, verify, rename into place). It fails closed and
  quietly: with no pinned release key it does not even touch the network, and any failure is recorded as a short result
  word and the time of the attempt (``update_check``), never shown as a nag.
* ``UpdateAgent`` is what the companion's tick calls. It starts at most one check at a time, once the last attempt is
  old enough (``update_check.due``: about once a day, sooner after a check that could not reach the network, and not
  within 90 seconds of the companion starting), keeps the current offer for the popup, and runs "Install now".
* ``update_view`` turns the agent's state into the popup's words and buttons (pure; no Tk).
* Install now never runs inside the companion: it starts ``python -m eutonos.installer update [--staged]`` as its own
  process (the update replaces the runtime the companion runs from). The installer takes the lifecycle lock, so a busy
  machine says so and keeps the offer. After a good update the companion starts a small helper from the NEW runtime that
  waits for the old companion to leave and starts the companion again (``desktop_companion_runtime
  --restart-after-update``), which is the same supervisor startup the logon task uses.

Nothing is installed without the person's click, whatever the mode.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import threading
import time

from . import home as eutonos_home
from . import pending_actions
from . import release_signing as rsg
from . import update_check as uc
from . import update_stage

FIRST_CHECK_DELAY = 90.0   # seconds after the companion starts before any network work
POLL_SECONDS = 15.0        # how often the tick re-reads the saved state and the due time
INSTALL_TIMEOUT = 20 * 60  # seconds an update may run before the companion stops waiting for it
HIDDEN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_flight = threading.Lock()  # at most one check at a time in this process


def run_check(home=None, *, source=None, now=None, check=None) -> dict:
    """One background pass. Returns a plain dict (``outcome`` ``skipped``, ``busy``, ``refused``, ``unavailable``,
    ``up-to-date``, ``no-release``, ``update-available``, ``staged`` or ``failed``); never raises. ``source`` and
    ``check`` are seams for tests: no real network happens unless a release key is pinned and no source is given."""
    base = Path(home) if home is not None else eutonos_home.home()
    if not _flight.acquire(blocking=False):
        return {"outcome": "busy"}
    try:
        try:
            wanted = uc.mode(base)
        except uc.UpdateCheckError:
            return {"outcome": "skipped", "reason": "settings-unreadable"}
        if wanted == "manual":
            return {"outcome": "skipped", "reason": "manual"}
        if not _installed(base):
            return {"outcome": "skipped", "reason": "not-installed"}
        if source is None and not rsg.PINNED_KEYS:  # nothing could be trusted, so do not even ask the network
            uc.record_attempt("refused:no-trusted-key", home=base, now=now)
            return {"outcome": "refused", "reason": "no-trusted-key"}
        try:
            from . import installer
            capture = {}
            found = (check or installer.check_update)(source=source, home=base, now=now, capture=capture)
            outcome = found.get("outcome")
            if outcome in ("up-to-date", "no-release"):
                update_stage.prune(base)
                return {"outcome": outcome}
            if outcome != "update-available":
                uc.record_attempt(f"{outcome}:{found.get('reason') or 'unknown'}", home=base, now=now)
                return {"outcome": outcome, "reason": found.get("reason")}
            available = found["available"]
            summary = {"outcome": "update-available", "version": available["version"],
                       "artifact_id": available["artifact_id"]}
            if wanted != "auto" or available.get("blocked") or not capture.get("release"):
                return summary
            staged = update_stage.stage(capture["source"], capture["release"], base)
            if staged["outcome"] != "staged":
                word = "unavailable" if staged["outcome"] == "unavailable" else "failed"
                uc.record_attempt(f"{word}:stage-{staged.get('reason') or 'unknown'}", home=base, now=now)
                return {**summary, "staging": staged["outcome"], "reason": staged.get("reason")}
            marked = uc.mark_staged(available, (found.get("installed") or {}).get("version"), home=base, now=now)
            if not marked.get("marked"):
                update_stage.prune(base)
                return {**summary, "staging": "dropped"}
            return {**summary, "outcome": "staged"}
        except Exception as exc:  # a background pass must never crash the companion
            uc.record_attempt(f"failed:{type(exc).__name__}", home=base, now=now)
            return {"outcome": "failed", "reason": type(exc).__name__}
    finally:
        _flight.release()


def _installed(home) -> bool:
    try:
        from . import install_record as ir
        return bool(ir.read_state(ir.Layout(home)).get("active"))
    except Exception:
        return False


class _Offers:
    """The popup's current offer, recomputed from the saved state (cheap file reads; the staged copy's signature is
    only re-checked when the staged folder changes)."""

    def __init__(self, home):
        self.home = home
        self._cache = None

    def current(self):
        try:
            state = uc.read(self.home)
        except uc.UpdateCheckError:
            return None
        found = state["last_available"]
        if state["mode"] == "manual" or not found:
            return None
        try:
            waiting = [item for item in pending_actions.list_actions(self.home, state="pending") if item["source"] == uc.SOURCE]
        except pending_actions.PendingActionsError:
            return None
        if not waiting:  # the person marked it done or dismissed it elsewhere
            return None
        artifact = found["artifact_id"]
        offer = {"version": found["version"], "artifact_id": artifact, "kind": "available", "detail": found.get("blocked")}
        if found.get("blocked"):
            offer["kind"] = "blocked"
        elif (state["staged"] or {}).get("artifact_id") == artifact and self._ready(artifact):
            offer["kind"] = "ready"
        return offer

    def _ready(self, artifact) -> bool:
        folder = update_stage.staged_root(self.home) / artifact
        try:
            stamp = (artifact, folder.stat().st_mtime_ns)
        except OSError:
            self._cache = None
            return False
        if self._cache and self._cache[0] == stamp:
            return self._cache[1]
        ready = update_stage.staged_ready(self.home, artifact)
        self._cache = (stamp, ready)
        return ready


def installer_command(home, staged: bool, python=None) -> list:
    """The argv for the separate update process: the companion's own interpreter (console flavour, so the output can be
    read), isolated, running the installer's ``update`` command."""
    exe = Path(python or sys.executable)
    console = exe.with_name("python.exe")
    if exe.name.lower() == "pythonw.exe" and console.is_file():
        exe = console
    argv = [str(exe), "-I", "-B", "-m", "eutonos.installer", "--home", str(home), "update"]
    return argv + ["--staged"] if staged else argv


def parse_answer(text) -> dict:
    """The installer's JSON answer from its output (it prints one pretty-printed object), or ``{}``."""
    text = str(text or "")
    for start in [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n" and text[i + 1:i + 2] == "{"]:
        try:
            value = json.loads(text[start:])
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def run_installer(home, staged: bool, *, python=None, run=subprocess.run, timeout=INSTALL_TIMEOUT) -> dict:
    """Run the update as its own process and say what it answered. Never raises."""
    try:
        done = run(installer_command(home, staged, python), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                   timeout=timeout, creationflags=HIDDEN)
    except subprocess.TimeoutExpired:
        return {"outcome": "failed", "detail": "The update did not finish in time. Check it with: eutonos-install status"}
    except OSError as exc:
        return {"outcome": "failed", "detail": f"The update could not be started ({type(exc).__name__})."}
    answer = parse_answer(done.stdout)
    if not answer:
        return {"outcome": "failed", "detail": ((done.stderr or done.stdout or "").strip()[-200:]
                                                or f"The update stopped (exit {done.returncode}).")}
    return answer


def active_runtime_python(home) -> Path | None:
    """The interpreter of the generation that is active now (the new one, right after an update), or None."""
    try:
        from . import install_record as ir
        from . import installer_ports as iports
        layout = ir.Layout(home)
        record = ir.read_record(layout, ir.read_state(layout)["active"])
        python = iports.Ports(iports.default_os_name()).runtime_python(layout.runtimes / record["wheel"]["artifact_id"])
        return python if python.is_file() else None
    except Exception:
        return None


class UpdateAgent:
    """What the companion's tick drives. Everything slow runs on a thread; ``tick`` and the view methods are cheap and
    safe to call from the Tk thread. ``revision`` changes whenever the popup should redraw."""

    def __init__(self, home=None, *, clock=time.monotonic, check=run_check, runner=run_installer, spawn=None,
                 start_delay=FIRST_CHECK_DELAY, poll_seconds=POLL_SECONDS):
        self.home = Path(home) if home is not None else eutonos_home.home()
        self._clock, self._check, self._runner = clock, check, runner
        self._spawn = spawn or (lambda target: threading.Thread(target=target, daemon=True).start())
        self._start_delay, self._poll_seconds = start_delay, poll_seconds
        self._began = clock()
        self._next_poll = self._began
        self._offers = _Offers(self.home)
        self._lock = threading.Lock()
        self._polling = False
        self.checking = False
        self.offer = None
        self.snoozed = None
        self.install = None  # {"phase": "installing" | "failed" | "done", "version", "message"}
        self.restart_requested = False
        self.revision = 0

    # -- the tick
    def tick(self) -> None:
        now = self._clock()
        if now < self._next_poll or self._polling:
            return
        self._next_poll = now + self._poll_seconds
        self._polling = True
        self._spawn(self._poll)

    def _bump(self) -> None:
        self.revision += 1

    def _poll(self) -> None:
        try:
            offer = self._offers.current()
            if offer != self.offer:
                self.offer = offer
                self._bump()
            if (self._clock() - self._began >= self._start_delay and not self.checking
                    and not (self.install or {}).get("phase") == "installing" and uc.due(self.home)):
                self.checking = True
                self._spawn(self._run_check)
        except Exception:
            pass
        finally:
            self._polling = False

    def _run_check(self) -> None:
        try:
            found = self._check(self.home)
            if found.get("outcome") not in ("skipped", "busy"):
                self.snoozed = None  # a fresh check asks again, as "Later" promised
            self.offer = self._offers.current()
        except Exception:
            pass
        finally:
            self.checking = False
            self._bump()

    # -- what the popup shows and does
    def visible_offer(self):
        offer = self.offer
        if self.install and self.install.get("phase") == "installing":
            return offer
        return offer if offer and offer["artifact_id"] != self.snoozed else None

    def later(self) -> None:
        if self.offer:
            self.snoozed = self.offer["artifact_id"]
            self.install = None
            self._bump()

    def install_now(self) -> bool:
        """Start the update in its own process. False when one is already running or there is nothing to install."""
        with self._lock:
            offer = self.offer
            if not offer or offer["kind"] == "blocked" or (self.install or {}).get("phase") == "installing":
                return False
            self.install = {"phase": "installing", "version": offer["version"], "message": None}
        self._bump()
        self._spawn(lambda: self._install(offer))
        return True

    def _install(self, offer) -> None:
        try:
            answer = self._runner(self.home, offer["kind"] == "ready")
        except Exception as exc:
            answer = {"outcome": "failed", "detail": type(exc).__name__}
        outcome = answer.get("outcome")
        if outcome == "updated":
            self.install = {"phase": "done", "version": offer["version"], "message": None}
            self.offer = None
            self.restart_requested = True
        elif outcome == "busy":
            self.install = {"phase": "failed", "version": offer["version"],
                            "message": "Another EUTONOS install or update is running. The offer stays; try again in a minute."}
        else:
            why = answer.get("detail") or answer.get("reason") or answer.get("error") or outcome or "unknown"
            self.install = {"phase": "failed", "version": offer["version"],
                            "message": f"The update did not finish ({str(why)[:140]}). The earlier version is still in use."}
        self._bump()


def update_view(agent) -> dict:
    """The popup section as plain data: ``show``, ``headline``, ``detail``, ``note`` and ``buttons`` (a list of
    ``(label, verb)``, verbs ``install`` and ``later``). Pure."""
    offer = agent.visible_offer() if agent is not None else None
    install = (agent.install if agent is not None else None) or {}
    if install.get("phase") == "installing":
        return {"show": True, "kind": "installing", "headline": f"Installing EUTONOS {install['version']}...",
                "detail": "This takes a minute. The companion restarts when it is done.", "note": None, "buttons": []}
    if install.get("phase") == "done":
        return {"show": True, "kind": "done", "headline": f"EUTONOS {install['version']} is installed",
                "detail": install.get("message") or "The companion is restarting.", "note": None, "buttons": []}
    if not offer:
        return {"show": False, "kind": None, "headline": "", "detail": "", "note": None, "buttons": []}
    note = install.get("message") if install.get("phase") == "failed" else None
    version = offer["version"]
    if offer["kind"] == "ready":
        return {"show": True, "kind": "ready", "headline": f"EUTONOS {version} is ready",
                "detail": "Downloaded and checked. Nothing is installed until you click Install now.",
                "note": note, "buttons": [("Install now", "install"), ("Later", "later")]}
    if offer["kind"] == "blocked":
        return {"show": True, "kind": "blocked", "headline": f"EUTONOS {version} is available",
                "detail": ((offer.get("detail") or "An update cannot apply it") + ". Run Setup again to get it."),
                "note": note, "buttons": [("Later", "later")]}
    return {"show": True, "kind": "available", "headline": f"EUTONOS {version} is available",
            "detail": "Nothing is downloaded until you click.", "note": note,
            "buttons": [("Download and install", "install"), ("Later", "later")]}
