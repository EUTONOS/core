"""Canonical local runtime paths. No filesystem access occurs on import.

Where Simon keeps its runtime data (state, command bridge, logs, reports, screenshots):

1. ``SIMON_RUNTIME_DIR`` when it names an absolute folder. The installed launcher ``eutonos-simon`` sets it to
   ``<EUTONOS home>/simon/runtime``.
2. A copy installed by EUTONOS (this folder is ``<EUTONOS home>/kits/simon-<hash>``): ``<EUTONOS home>/simon/runtime``.
   A kit is an immutable, hash-checked copy, so Simon never writes inside it.
3. Otherwise (a source checkout): ``.runtime`` beside this file, as before.

The choice is lexical (environment and path names only), so importing this module still touches no file.
"""
import os
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
RUNTIME_ENV = "SIMON_RUNTIME_DIR"


def installed_home(app_dir: Path = APP_DIR) -> Path | None:
    """The EUTONOS home when ``app_dir`` is an installed kit folder (``<home>/kits/simon-<hash>``), else None."""
    if app_dir.parent.name == "kits" and app_dir.name.startswith("simon-"):
        return app_dir.parent.parent
    return None


def runtime_dir(app_dir: Path = APP_DIR, environ=None) -> Path:
    """The runtime data folder for ``app_dir`` (see the module docstring for the order)."""
    given = (os.environ if environ is None else environ).get(RUNTIME_ENV, "").strip()
    if given and os.path.isabs(given):
        return Path(given)
    home = installed_home(app_dir)
    if home is not None:
        return home / "simon" / "runtime"
    return app_dir / ".runtime"


RUNTIME_DIR = runtime_dir()
STATE_FILE = RUNTIME_DIR / "simon_state.json"
COMMAND_FILE = RUNTIME_DIR / "helper_command.json"
HELPER_COMMAND_FILE = RUNTIME_DIR / "helper_app_command.json"
HELPER_RESULT_FILE = RUNTIME_DIR / "helper_app_result.json"
HELPER_BRIDGE_STATUS_FILE = RUNTIME_DIR / "helper_bridge_status.json"
HELPER_BRIDGE_HISTORY_FILE = RUNTIME_DIR / "helper_bridge_history.jsonl"
HELPER_RUN_JOURNAL_FILE = RUNTIME_DIR / "helper_run_journal.jsonl"
HELPER_CAPTURE_ROUTE_HISTORY_FILE = RUNTIME_DIR / "capture_route_history.jsonl"
HELPER_CAPTURE_ROUTE_TALLY_FILE = RUNTIME_DIR / "capture_route_tally.json"
HELPER_INSTANCE_LOCK_FILE = RUNTIME_DIR / "agent_helper_instance.lock"
TARGET_LOG_FILE = RUNTIME_DIR / "target_log.txt"
HELPER_LOG_FILE = RUNTIME_DIR / "helper_log.txt"
COMMAND_LOG_FILE = RUNTIME_DIR / "helperctl_log.txt"
COMMAND_REPORT_DIR = RUNTIME_DIR / "command_reports"
COMMAND_SCREENSHOT_DIR = RUNTIME_DIR / "command_screenshots"
PLANNER_REQUEST_DIR = RUNTIME_DIR / "planner_requests"
FAILURE_DIAGNOSTICS_DIR = RUNTIME_DIR / "failures"
PLANNER_MEMORY_FILE = RUNTIME_DIR / "planner_working_memory.json"
ENVIRONMENT_DIAGNOSTICS_FILE = RUNTIME_DIR / "environment_diagnostics.json"
COMMAND_BRIDGE_LOCK_FILE = RUNTIME_DIR / "helper_app_command.lock"
ARTIFACT_MANIFEST_FILE = RUNTIME_DIR / "artifact_manifest.json"
