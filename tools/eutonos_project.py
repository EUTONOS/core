"""Portable consumer entry: python tools/eutonos_project.py setup verify.

Uses EUTONOS_PYTHON when supplied, otherwise the explicitly installed user runtime.
"""
import os
from pathlib import Path
import subprocess
import sys

modules = {"version": "release", "hub": "cli", "setup": "setup_cli", "host": "host_cli", "nav": "nav_cli",
           "ram": "ram_cli", "communication": "communication_cli", "measurement": "measurement_cli",
           "dashboard": "dashboard_cli", "workflow": "workflow_cli",
           "capture": "companion_capture", "ram-capture": "companion_ram_capture",
           "settings": "settings_cli", "companion": "companion_control", "assignments": "assignments_cli",
           "delivery": "delivery_summary_cli", "state": "repository_state_cli", "attribution": "companion_attribution_cli",
           "boards": "boards_cli", "ram-relevance": "ram_relevance_cli", "skills": "skills_cli",
           "goals": "goals_cli", "personal": "personal_cli", "device": "device_repositories_cli",
           "goal-context": "goal_context"}
if len(sys.argv) < 2 or sys.argv[1] not in modules:
    raise SystemExit("Choose: " + ", ".join(modules))
def _eutonos_home() -> Path:
    """Parity copy of eutonos.home.home() (IPC5, tests/test_home.py): EUTONOS_HOME (absolute) wins, then the runtime
    this Python lives in (<H>/runtimes/eutonos-*), then ~/.eutonos."""
    given = os.environ.get("EUTONOS_HOME")
    if given:
        if not Path(given).is_absolute():
            raise ValueError(f"EUTONOS_HOME must be an absolute path, not {given!r}.")
        return Path(given)
    try:
        prefix = Path(sys.prefix).resolve()
    except OSError:
        prefix = None
    if prefix is not None and prefix.parent.name == "runtimes" and prefix.name.startswith("eutonos-"):
        return prefix.parent.parent
    return Path.home() / ".eutonos"

# This entry runs before any runtime exists, so it uses the parity copy above.
home = _eutonos_home() / "runtime"
python = os.environ.get("EUTONOS_PYTHON", str(home / ("Scripts/python.exe" if os.name == "nt" else "bin/python")))
if not Path(python).is_file():
    raise SystemExit("EUTONOS runtime missing. Follow docs/SETUP.md; set EUTONOS_PYTHON to its Python executable.")
root = Path(__file__).resolve().parent.parent
arguments = sys.argv[2:]
if not any(arg == "--root" or arg.startswith("--root=") for arg in arguments):
    arguments = [*arguments, "--root", str(root)]
raise SystemExit(subprocess.call([python, "-m", "eutonos." + modules[sys.argv[1]], *arguments]))
