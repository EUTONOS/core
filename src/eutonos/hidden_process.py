"""Keep captured child processes from opening a visible console window on Windows."""

import os
import subprocess


def no_window() -> dict:
    """Return ``creationflags`` keyword arguments that hide the child's console on Windows."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}
