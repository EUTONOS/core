"""Install state, generation records, receipts and journal under ``<EUTONOS home>/install`` (IPC2 contracts a and b).

Layout (contract a)::

    install/state.json                                {profile, active, pending, generations{gen_id: record_sha256}, channel, legacy}
    install/generations/<gen_id>/record.json          the install record (contract b)
    install/generations/<gen_id>/marketplace/crosstalk the Claude marketplace this generation rendered
    install/receipts/<gen_id>/<nn>-<phase>.json       one receipt per finished phase
    install/backups/<gen_id>/<effect_id>.<ext>        bytes an effect replaced
    install/journal/<txn_id>.jsonl                    one line per intent, effect and phase (crash recovery)

Only the installer writes ``state.json`` and the record. Every write here is atomic (temporary file, then replace).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets

from . import home as eutonos_home
from . import tokenslang as ts

STATE_PROFILE = "eutonos.install.state.v1"
RECORD_PROFILE = "eutonos.install.record.v1"
PHASES = ("runtime", "kits", "surfaces", "effects", "plugin", "activate")
RECORD_STATES = ("prepared", "active", "retired", "rolled-back")
CHANNELS = ("dev", "beta", "stable")
EFFECT_KINDS = ("file-create", "file-block", "json-entry", "toml-key", "toml-table", "text-line", "shortcut",
                "scheduled-task", "launchd", "env-path", "env-var", "app-command", "host-enroll")
EFFECT_FIELDS = ("effect_id", "component", "permission_entry", "kind", "target", "selector", "before_sha256",
                 "after_sha256", "shipped_sha256", "backup", "undo")


def now_iso(now=None) -> str:
    return (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str | None:
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None


def write_atomic(path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}-{secrets.token_hex(3)}")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def dump(value) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


class Layout:
    """Every path under one EUTONOS home. ``Layout()`` resolves the current home through ``eutonos.home``."""

    def __init__(self, base=None):
        self.base = Path(base) if base is not None else eutonos_home.home()

    @property
    def install(self) -> Path:
        return self.base / "install"

    @property
    def state(self) -> Path:
        return self.install / "state.json"

    @property
    def runtimes(self) -> Path:
        return self.base / "runtimes"

    @property
    def kits(self) -> Path:
        return self.base / "kits"

    @property
    def bin(self) -> Path:
        return self.base / "bin"

    @property
    def marketplace(self) -> Path:
        return self.base / "marketplace"

    @property
    def settings(self) -> Path:
        return self.base / "settings"

    @property
    def profiles(self) -> Path:
        return self.base / "profiles"

    @property
    def projects_file(self) -> Path:
        return self.base / "projects.json"

    def generation(self, gen_id: str) -> Path:
        return self.install / "generations" / gen_id

    def generation_marketplace(self, gen_id: str) -> Path:
        """The Claude marketplace folder this generation rendered; it is copied to ``marketplace/crosstalk`` (the folder
        Claude has registered) when the generation is activated, so a rollback puts back the plugin that matches it."""
        return self.generation(gen_id) / "marketplace" / "crosstalk"

    def record(self, gen_id: str) -> Path:
        return self.generation(gen_id) / "record.json"

    def receipts(self, gen_id: str) -> Path:
        return self.install / "receipts" / gen_id

    def backups(self, gen_id: str) -> Path:
        return self.install / "backups" / gen_id

    def journal(self, txn_id: str) -> Path:
        return self.install / "journal" / f"{txn_id}.jsonl"


# ------------------------------------------------------------------ ids
def new_gen_id() -> str:
    return secrets.token_hex(8)


def new_txn_id() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


def artifact_id(version: str, wheel_sha256: str) -> str:
    """``eutonos-<version>-<wheel sha256[:12]>`` (contract a)."""
    return f"eutonos-{version}-{wheel_sha256[:12]}"


# ---------------------------------------------------------------- state
def empty_state() -> dict:
    return {"profile": STATE_PROFILE, "active": None, "pending": None, "generations": {}, "channel": "dev",
            "legacy": None, "for_this_user": False}


def read_state(layout: Layout) -> dict:
    """The state, or a fresh empty one when the file is absent. A damaged file is an error, never replaced."""
    try:
        value = json.loads(layout.state.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty_state()
    except (OSError, ValueError) as exc:
        raise ts.ReadError("invalid", f"The install state is unreadable: {layout.state}") from exc
    ts.require(isinstance(value, dict) and value.get("profile") == STATE_PROFILE
               and isinstance(value.get("generations"), dict), "invalid", "The install state has an unsupported shape.")
    for key, default in empty_state().items():
        value.setdefault(key, default)
    return value


def write_state(layout: Layout, state: dict) -> None:
    write_atomic(layout.state, dump(state))


# --------------------------------------------------------------- record
def check_effect(effect: dict) -> None:
    ts.require(isinstance(effect, dict) and all(name in effect for name in EFFECT_FIELDS), "invalid",
               "An effect is missing fields: " + ", ".join(name for name in EFFECT_FIELDS if name not in (effect or {})))
    ts.require(effect["kind"] in EFFECT_KINDS, "invalid", f"Unknown effect kind: {effect['kind']}")


def new_record(*, gen_id: str, previous_gen: str | None, os_name: str, version: str, wheel: dict, kits: list,
               consent: dict | None, now=None, min_compatible: str | None = None, for_this_user: bool = False) -> dict:
    return {"profile": RECORD_PROFILE, "gen_id": gen_id, "previous_gen": previous_gen, "created_at": now_iso(now),
            "os": os_name, "state": "prepared", "eutonos_version": version, "wheel": wheel, "kits": kits,
            "consent": consent, "components": [], "projects": [], "effects": [], "min_compatible": min_compatible,
            "three_way": [], "for_this_user": bool(for_this_user)}


def read_record(layout: Layout, gen_id: str) -> dict:
    try:
        value = json.loads(layout.record(gen_id).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ts.ReadError("missing", f"No install record for generation {gen_id}.") from exc
    except (OSError, ValueError) as exc:
        raise ts.ReadError("invalid", f"The install record for {gen_id} is unreadable.") from exc
    ts.require(isinstance(value, dict) and value.get("profile") == RECORD_PROFILE and value.get("gen_id") == gen_id,
               "invalid", f"The install record for {gen_id} has an unsupported shape.")
    return value


def write_record(layout: Layout, state: dict, record: dict) -> str:
    """Write the record and note its hash in ``state['generations']`` (the caller saves the state)."""
    ts.require(record.get("state") in RECORD_STATES, "invalid", "Unknown record state.")
    data = dump(record)
    write_atomic(layout.record(record["gen_id"]), data)
    digest = sha256_bytes(data)
    state["generations"][record["gen_id"]] = digest
    return digest


def record_intact(layout: Layout, state: dict, gen_id: str) -> bool:
    """True when the record on disk still has the hash the state noted."""
    return state["generations"].get(gen_id) == sha256_file(layout.record(gen_id))


def merge_effects(record: dict, effects: list[dict]) -> None:
    """Add effects to the record, replacing any earlier one with the same ``effect_id`` in place."""
    index = {effect["effect_id"]: position for position, effect in enumerate(record["effects"])}
    for effect in effects:
        check_effect(effect)
        if effect["effect_id"] in index:
            record["effects"][index[effect["effect_id"]]] = effect
        else:
            index[effect["effect_id"]] = len(record["effects"])
            record["effects"].append(effect)


# -------------------------------------------------------------- receipts
def write_receipt(layout: Layout, gen_id: str, phase: str, payload: dict, now=None) -> Path:
    ts.require(phase in PHASES, "invalid", f"Unknown phase: {phase}")
    path = layout.receipts(gen_id) / f"{PHASES.index(phase) + 1:02d}-{phase}.json"
    write_atomic(path, dump({"phase": phase, "gen_id": gen_id, "completed_at": now_iso(now), **payload}))
    return path


def done_phases(layout: Layout, gen_id: str) -> list[str]:
    folder = layout.receipts(gen_id)
    found = []
    if folder.is_dir():
        for phase in PHASES:
            if (folder / f"{PHASES.index(phase) + 1:02d}-{phase}.json").is_file():
                found.append(phase)
    return found


# --------------------------------------------------------------- journal
def journal_append(layout: Layout, txn_id: str, event: dict, now=None) -> None:
    """Append one JSON line. The file is opened for append and flushed, so an interrupted run keeps every line."""
    path = layout.journal(txn_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"t": now_iso(now), **event}, sort_keys=True) + "\n"
    with path.open("ab") as stream:
        stream.write(line.encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())


def journal_read(layout: Layout, txn_id: str) -> list[dict]:
    """The journal's lines. A torn last line (a crash mid-write) is dropped."""
    try:
        text = layout.journal(txn_id).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    events = []
    for line in text.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            break
    return events
