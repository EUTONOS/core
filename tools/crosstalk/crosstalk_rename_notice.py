"""Crosstalk rename-now notice (owner request 2026-10-25, stdlib only): the small window that tells the owner to wait
while the helper turns on remote access and fixes chat names right after a Claude Desktop load.

Two halves that talk through three small files in the helper folder (no sockets, no credentials, no titles):
  * the helper side (`Notice`): writes `rename-now-notice.json` (token, state, text, ts_ms), starts this module in its
    own windowless process (so the helper loop never blocks on a window), waits for `rename-now-ack.json` (the window
    really showed), and reads `rename-now-cancel.json` (the owner pressed Cancel or closed the window);
  * the window side (`python -I -B crosstalk_rename_notice.py --folder DIR --token T`): a small always-on-top Tk window
    with the text and one Cancel button. It follows the status file: while state is "running" it keeps showing the
    text; on any other state it shows the final text for a few seconds and closes itself. It also closes when the
    status file is gone, has another token, or has not been refreshed for STALE_S (the helper died).

The window comes to the front once (crosstalk_focus.forward_own_box) and then lets the helper bring Claude Desktop
forward behind it (allow_any_foreground). It never types, never touches the clipboard and never reads a credential.
Without tkinter or without Windows the window cannot show: `Notice.show` then returns False and the helper does not
run the pass (it never types unannounced).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

HERE = Path(__file__).resolve().parent
TAG = "crosstalk.rename-notice.v1"
WINDOW_TITLE = "Crosstalk: getting your chats ready"
NOTICE_FILE = "rename-now-notice.json"
CANCEL_FILE = "rename-now-cancel.json"
ACK_FILE = "rename-now-ack.json"
STALE_S = 180.0              # the helper refreshes the status between chats; older than this and the window closes
ACK_WAIT_S = 10.0
ACK_STEP_S = 0.25
CLOSE_AFTER_S = {"done": 4.0, "cancelled": 3.0, "failed": 8.0}
POLL_MS = 400
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def notice_text(count: int) -> str:
    return ("Getting your chats ready for your phone and other computers: turning on remote access and fixing chat "
            f"names ({count} chat{'s' if count != 1 else ''}). Please don't type or click until this closes.")


def _read(path: Path):
    with contextlib.suppress(OSError, ValueError):
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    return None


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def window_decision(status, token: str, now_s: float, finished_s: float | None):
    """What the window should do on this poll. Returns (action, text, finished_s): action is "run" (keep showing text),
    "final" (show the final text, closing after the finish delay) or "close" (close now). `finished_s` is when the
    final text was first seen (None until then)."""
    if not isinstance(status, dict) or status.get("token") != token:
        return "close", "", finished_s
    text = str(status.get("text") or "")
    stamp = status.get("ts_ms")
    state = status.get("state")
    if state == "running":
        if not isinstance(stamp, (int, float)) or isinstance(stamp, bool) or now_s - stamp / 1000.0 > STALE_S:
            return "close", text, finished_s
        return "run", text, None
    started = finished_s if finished_s is not None else now_s
    if now_s - started >= CLOSE_AFTER_S.get(str(state), 4.0):
        return "close", text, started
    return "final", text, started


# ------------------------------------------------------------------ helper side
def default_spawn(folder: Path, token: str):
    """Start the window in its own windowless process. Returns the Popen (or raises OSError)."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | NO_WINDOW
    python = sys.executable
    if os.name == "nt":
        sibling = Path(sys.executable).with_name("pythonw.exe")
        python = str(sibling) if sibling.is_file() else sys.executable
    return subprocess.Popen([python, "-I", "-B", str(Path(__file__).resolve()), "--folder", str(folder),
                             "--token", token], creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Notice:
    """The helper's handle on the window. `spawn(folder, token)` starts the window process (tests inject a fake that
    writes the ack file or not)."""

    def __init__(self, folder: Path, spawn=default_spawn, clock=time.time, sleep=time.sleep):
        self.folder, self.spawn, self.clock, self.sleep = Path(folder), spawn, clock, sleep
        self.token = uuid.uuid4().hex[:16]
        self.text = ""

    def _status(self, state: str, text: str) -> None:
        self.text = text
        _write(self.folder / NOTICE_FILE, {"token": self.token, "state": state, "text": text,
                                          "ts_ms": int(self.clock() * 1000)})

    def show(self, count: int) -> bool:
        """Write the status, start the window and wait until it says it is up. False (and the window is told to
        close) when it did not start or did not show in ACK_WAIT_S."""
        try:
            self._status("running", notice_text(count))
            self.spawn(self.folder, self.token)
        except OSError:
            self.close("failed", "")
            return False
        for _ in range(max(1, int(ACK_WAIT_S / ACK_STEP_S))):
            ack = _read(self.folder / ACK_FILE)
            if ack and ack.get("token") == self.token and ack.get("shown") is True:
                return True
            self.sleep(ACK_STEP_S)
        self.close("failed", "")
        return False

    def cancelled(self) -> bool:
        row = _read(self.folder / CANCEL_FILE)
        return bool(row and row.get("token") == self.token)

    def update(self, text: str | None = None) -> None:
        """Refresh the status (and the text, when given) so the window knows the helper is alive."""
        self._status("running", text if text is not None else self.text)

    def close(self, state: str, text: str) -> None:
        """Final state ("done", "cancelled" or "failed") and its short text: the window shows it and closes itself."""
        with contextlib.suppress(OSError):
            self._status(state, text)


# ------------------------------------------------------------------ window side
def run_window(folder: Path, token: str) -> int:  # pragma: no cover - needs a desktop
    """The Tk window. Exit code 0 when it showed and closed, 2 when it could not show."""
    try:
        import tkinter
    except Exception:
        return 2
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import crosstalk_focus as focus
    focus.enable_real_focus(True)
    platform = focus.default_platform()
    try:
        root = tkinter.Tk()
    except Exception:
        return 2
    root.title(WINDOW_TITLE)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    label = tkinter.Label(root, text="", wraplength=380, justify="left", padx=14, pady=12, font=("Segoe UI", 10))
    label.pack()
    state = {"finished": None, "cancelled": False}

    def cancel() -> None:
        state["cancelled"] = True
        with contextlib.suppress(OSError):
            _write(folder / CANCEL_FILE, {"token": token, "ts_ms": int(time.time() * 1000)})
        button.configure(state="disabled", text="Cancelling...")

    button = tkinter.Button(root, text="Cancel", width=14, command=cancel)
    button.pack(pady=(0, 12))
    root.protocol("WM_DELETE_WINDOW", cancel)  # closing the window is Cancel

    def poll() -> None:
        action, text, state["finished"] = window_decision(_read(folder / NOTICE_FILE), token, time.time(),
                                                          state["finished"])
        if action == "close":
            root.destroy()
            return
        if text and label.cget("text") != text:
            label.configure(text=text)
        if action == "final":
            button.configure(state="disabled")
        root.after(POLL_MS, poll)

    def come_forward() -> None:
        root.update_idletasks()
        width, height = root.winfo_reqwidth(), root.winfo_reqheight()
        root.geometry(f"+{max(0, root.winfo_screenwidth() - width - 24)}+{max(0, root.winfo_screenheight() - height - 90)}")
        root.lift()

        def forward() -> None:  # a side thread: the window only shows once the main loop runs
            how = focus.forward_own_box(WINDOW_TITLE, platform=platform, classes=("TkTopLevel",))
            if how == "not-shown":
                return  # no ack: the helper does not run the pass and closes this window
            platform.allow_any_foreground()  # the helper brings Claude Desktop forward next, behind this window
            with contextlib.suppress(OSError):
                _write(folder / ACK_FILE, {"token": token, "shown": True, "ts_ms": int(time.time() * 1000)})
        threading.Thread(target=forward, daemon=True, name="crosstalk-rename-notice").start()

    root.after(50, poll)
    root.after(150, come_forward)
    root.mainloop()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="crosstalk_rename_notice", description=__doc__)
    parser.add_argument("--folder", required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args(argv)
    return run_window(Path(args.folder), args.token)


if __name__ == "__main__":
    sys.exit(main())
