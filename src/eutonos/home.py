"""The one per-user EUTONOS home and its frozen layout (IPC2 contract a).

Order (F5): `EUTONOS_HOME` when set (a test and staging override; it must be absolute, a relative one is
refused); then the location of the running code (a runtime whose `sys.prefix` is `<H>/runtimes/eutonos-*` derives
`<H>`); otherwise `~/.eutonos`. Read at call time so tests and installers can redirect it. The Crosstalk kit cannot
import this package and carries a parity copy of `home()` (it also derives `<H>` from a `<H>/kits/<id>` location);
`tools/eutonos_project.py` and `tools/companion_closeout.py` carry parity copies too. A home that is not
`~/.eutonos` is found by code outside the runtime through the user environment variable `EUTONOS_HOME`, which a
real install of such a home sets (a recorded, reversible effect); every parity copy reads that variable first.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def from_runtime() -> Path | None:
    """`<H>` when this Python is a runtime under `<H>/runtimes/eutonos-*`, else None."""
    try:
        prefix = Path(sys.prefix).resolve()
    except OSError:
        return None
    if prefix.parent.name == "runtimes" and prefix.name.startswith("eutonos-"):
        return prefix.parent.parent
    return None


def default() -> Path:
    """The per-user default, `~/.eutonos`: where a real install lives and what a staged home must never be."""
    return Path.home() / ".eutonos"


def for_this_user(base) -> bool:
    """True when the install under `base` was made with the explicit "this is a real install for this user" mark
    (``install --for-this-user``, recorded in ``<base>/install/state.json``). Read without a lock; any problem reads
    as False, so an unreadable or absent state keeps the safe staging rule."""
    try:
        value = json.loads((Path(base) / "install" / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(value, dict) and value.get("for_this_user") is True


def is_default(base) -> bool:
    """Is `base` the per-user default `~/.eutonos`?"""
    try:
        return os.path.normcase(os.path.abspath(base)) == os.path.normcase(os.path.abspath(default()))
    except OSError:
        return False


def home() -> Path:
    given = os.environ.get("EUTONOS_HOME")
    if given:
        if not Path(given).is_absolute():
            raise ValueError(f"EUTONOS_HOME must be an absolute path, not {given!r}.")
        return Path(given)
    located = from_runtime()
    return located if located is not None else default()


def runtimes() -> Path:
    return home() / "runtimes"


def kits() -> Path:
    return home() / "kits"


def bin_dir() -> Path:
    return home() / "bin"


def marketplace() -> Path:
    return home() / "marketplace"


def install() -> Path:
    return home() / "install"


def hosts() -> Path:
    return home() / "hosts"


def projects_file() -> Path:
    return home() / "projects.json"


def settings() -> Path:
    return home() / "settings"


def profiles() -> Path:
    return home() / "profiles"
