"""Small cross-process lock for coordinated local metadata updates.

The lock is advisory and held only around a read/validate/write transaction.
Its file is merely a stable locking target: operating-system handle release makes
an abandoned process stop blocking other callers.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import threading
import time


_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_DEPTH = threading.local()


def _key(root) -> str:
    return os.path.normcase(str(Path(root).resolve()))


def _thread_lock(key: str) -> threading.RLock:
    with _GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


def _acquire_file(handle, *, timeout=None) -> None:
    """Acquire the OS-level byte-range lock on ``handle``.

    ``timeout`` (finding 4, Codex verification, 2026-09-28): ``None`` (the
    default) blocks exactly as before -- every existing caller (settings
    reads/writes that must not silently skip a transaction) keeps its current
    guarantee unchanged. A caller that cannot afford to block at all (a hook
    program that must never hang the wrapping app -- see ``role_hooks.py``)
    passes a small positive number of seconds; once elapsed without acquiring
    the lock, this raises ``TimeoutError`` instead of continuing to wait.
    """
    handle.seek(0)
    deadline = None if timeout is None else time.monotonic() + timeout
    if os.name == "nt":
        import msvcrt
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EINTR):
                    raise
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("timed out waiting for local_lock") from None
                time.sleep(0.01)
    else:
        import fcntl
        if deadline is None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            return
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EINTR):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out waiting for local_lock") from None
                time.sleep(0.01)


def _release_file(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def local_lock(root, *, create=True, timeout=None):
    """Coordinate all `.eutonos-local` settings/history/control writers.

    Nested calls in one thread share the same OS lock.  No lock survives an
    orderly close or a process crash because the operating system releases the
    byte-range lock with its file handle.

    ``timeout`` (finding 4, Codex verification, 2026-09-28; corrected on
    coordinator recheck, 2026-09-28): seconds to wait for the lock before
    giving up; ``None`` (the default) blocks indefinitely, unchanged from
    before this parameter existed. A reentrant (same-thread, already-held)
    call is unaffected either way -- there is nothing to wait for, since
    ``threading.RLock.acquire`` on a lock the calling thread already holds
    returns immediately regardless of ``timeout``.

    BUG FIX: the original implementation entered the in-process
    ``threading.RLock`` with a plain ``with thread_lock:`` -- an unbounded,
    un-timed-out block -- and only passed ``timeout`` on to the OS-level
    file lock afterwards. A caller with a genuinely short budget (a hook
    program that must never hang the wrapping app) could still block
    indefinitely on a DIFFERENT THREAD in the SAME PROCESS holding this
    lock, defeating the entire point of the parameter. ONE deadline now
    covers both waits: the thread lock's own ``acquire(timeout=...)`` first
    (raising ``TimeoutError`` promptly if a sibling thread holds it), then
    whatever budget remains for the OS-level file lock. Reentrancy is
    unchanged -- the SAME thread's own nested call still succeeds instantly
    and still releases exactly once per acquisition, matching the prior
    ``with thread_lock:`` behavior."""
    key = _key(root)
    thread_lock = _thread_lock(key)
    deadline = None if timeout is None else time.monotonic() + timeout

    def _remaining_or_block() -> float:
        # threading.Lock/RLock.acquire's own convention: a negative timeout
        # means "block indefinitely" -- exactly what timeout=None (the
        # default, every existing caller) must keep doing.
        if deadline is None:
            return -1
        return max(0.0, deadline - time.monotonic())

    if not thread_lock.acquire(timeout=_remaining_or_block()):
        raise TimeoutError("timed out waiting for local_lock")
    try:
        depths = getattr(_DEPTH, "values", None)
        if depths is None:
            depths = _DEPTH.values = {}
        depth = depths.get(key, 0)
        if depth:
            depths[key] = depth + 1
            try:
                yield
            finally:
                depths[key] -= 1
            return
        path = Path(key) / ".eutonos-local" / ".locks" / "metadata.lock"
        if not create and not path.exists():
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            _acquire_file(handle, timeout=None if deadline is None else _remaining_or_block())
            depths[key] = 1
            try:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                yield
            finally:
                depths.pop(key, None)
                _release_file(handle)
    finally:
        thread_lock.release()


__all__ = ["local_lock"]
