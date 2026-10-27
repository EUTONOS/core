"""Neutral helpers for the per-checkout ``.eutonos-local`` state.

A generic primitive the kept components need (the worker-model registry
writes through it): an atomic file write. Nothing here sends or reads a message.
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path


def atomic_write(path: Path, data: bytes) -> None:
    """Write bytes to ``path`` via a same-directory tmp file and os.replace.

    C11: the tmp file is unlinked on any failure (write or replace), never left
    behind. ``os.replace`` onto a file another process has open without
    ``FILE_SHARE_DELETE`` (Python's own default) can raise ``PermissionError``
    on Windows for a transient sharing violation; that alone is retried a
    bounded number of times with a short backoff before giving up.
    """
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
