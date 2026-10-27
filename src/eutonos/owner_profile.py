"""The owner profile (IPC5 lane B, contract f): the owner's private settings, never in the package.

The owner keeps ``eutonos.owner-profile.v1`` in private storage and passes it with ``eutonos-install --owner-profile
PATH``. This module validates it, copies it byte for byte to ``H/profiles/owner.json`` as a recorded ``file-create``
Effect (with the file's sha256), and reads the copy back. Lane A's installer calls ``plan`` (writes nothing) and
``install``; uninstall removes the copy through the generic Effect undo while it still holds the recorded bytes.

What is refused, with a plain reason and nothing written:

* a file that is not a bounded ordinary JSON object, or whose fields do not match the contract;
* secret-like content (a key named like a credential, a token, a private key block, a URL carrying a password, or an
  http(s) URL carrying a user name; ``ssh://git@host`` is an ordinary address): the profile holds recommendations and repository addresses, never credentials;
* a path inside the EUTONOS package (the wheel must never carry it).

Standard library only; the shared-work source checks are the ones ``eutonos.shared_work`` already applies.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

from . import component_support as cs

PROFILE = "eutonos.owner-profile.v1"
MAX_BYTES = 262144
FIELDS = ("profile", "name", "owner_id", "shared_work", "repositories", "components", "tuning", "desktop_accounts")
REQUIRED = ("profile", "name", "owner_id")
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SECRET_KEY = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|private[_-]?key|credential|cookie|auth)")
SECRET_VALUE = re.compile(
    r"(-----BEGIN [A-Z ]*PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}|\bsk-[A-Za-z0-9_-]{20,}"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}|\bAKIA[0-9A-Z]{16}\b|://[^/\s:@]+:[^/\s@]+@|\bhttps?://[^/\s:@]+@)")
COMPONENT = ID


class OwnerProfileError(ValueError):
    """The profile was refused; ``reason`` is a short code and the message says what to fix."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def package_root() -> Path:
    return Path(__file__).resolve().parent


def inside_package(path) -> bool:
    try:
        Path(path).resolve().relative_to(package_root())
        return True
    except (ValueError, OSError):
        return False


def _walk(value, where="profile"):
    """Yield ``(where, key, text)`` for every key and every string value."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield where, str(key), None
            yield from _walk(item, f"{where}.{key}")
    elif isinstance(value, list):
        for position, item in enumerate(value):
            yield from _walk(item, f"{where}[{position}]")
    elif isinstance(value, str):
        yield where, None, value


def secrets_found(value) -> list[str]:
    """Where the profile holds credential-like content (the key or place only, never the value)."""
    found = []
    for where, key, text in _walk(value):
        if key is not None and SECRET_KEY.search(key):
            found.append(f"{where}.{key}: the key name looks like a credential")
        if text is not None and SECRET_VALUE.search(text):
            found.append(f"{where}: the value looks like a credential or a URL with a password")
    return found


def _shared_work(items) -> list[str]:
    from . import shared_work
    problems = []
    if not isinstance(items, list):
        return ["shared_work must be a list"]
    for position, item in enumerate(items):
        label = f"shared_work[{position}]"
        wanted = {"repository_url", "canonical", "peers", "attempt_owner", "fetch_interval_seconds"}
        if not isinstance(item, dict) or set(item) != wanted:
            problems.append(f"{label} needs exactly {', '.join(sorted(wanted))}")
            continue
        try:
            shared_work.transport(item["repository_url"])
            shared_work._source(item["canonical"])
            peers = [shared_work._source(peer, True) for peer in item["peers"]]
            if len({peer["id"] for peer in peers}) != len(peers) or len(peers) > 8:
                raise ValueError("peers must be at most eight with unique ids")
        except (ValueError, TypeError, KeyError) as exc:
            problems.append(f"{label}: {exc}")
        if item["attempt_owner"] is not None and not isinstance(item["attempt_owner"], str):
            problems.append(f"{label}.attempt_owner must be a path or null")
        interval = item["fetch_interval_seconds"]
        if type(interval) is not int or not 5 <= interval <= 300:
            problems.append(f"{label}.fetch_interval_seconds must be 5 to 300")
    return problems


def validate(value) -> list[str]:
    """Problems with a decoded profile; an empty list means it matches contract (f)."""
    if not isinstance(value, dict):
        return ["the profile must be a JSON object"]
    problems = [f"unknown field {key}" for key in value if key not in FIELDS]
    problems += [f"missing field {key}" for key in REQUIRED if key not in value]
    if value.get("profile") != PROFILE:
        problems.append(f"profile must be {PROFILE}")
    if not isinstance(value.get("name"), str) or not value.get("name", "").strip():
        problems.append("name must be text")
    if not isinstance(value.get("owner_id"), str) or not ID.fullmatch(value.get("owner_id") or ""):
        problems.append("owner_id must be a short label (letters, digits, dot, dash, underscore)")
    problems += _shared_work(value.get("shared_work", []))
    repositories = value.get("repositories", [])
    if not isinstance(repositories, list):
        problems.append("repositories must be a list")
    else:
        for position, item in enumerate(repositories):
            if (not isinstance(item, dict) or set(item) != {"remote_url", "root_hint", "add"}
                    or not isinstance(item["remote_url"], str) or not item["remote_url"]
                    or not (item["root_hint"] is None or isinstance(item["root_hint"], str))
                    or type(item["add"]) is not bool):
                problems.append(f"repositories[{position}] needs remote_url, root_hint (or null) and add (true or false)")
    components = value.get("components", {})
    if not isinstance(components, dict) or any(
            not isinstance(key, str) or not COMPONENT.fullmatch(key) or item not in ("on", "off")
            for key, item in components.items()):
        problems.append('components must map a component id to "on" or "off"')
    if not isinstance(value.get("tuning", {}), dict):
        problems.append("tuning must be an object of recommended values")
    accounts = value.get("desktop_accounts", [])
    if not isinstance(accounts, list) or any(
            not isinstance(item, dict) or set(item) != {"account_prefix", "folder_auto"}
            or not isinstance(item["account_prefix"], str) or not item["account_prefix"]
            or item["folder_auto"] != "guided" for item in accounts):
        problems.append('desktop_accounts needs account_prefix and folder_auto "guided" for each account')
    return problems


def read(path) -> tuple[dict, bytes]:
    """``(decoded profile, the file's bytes)`` for a validated profile; raises ``OwnerProfileError`` otherwise."""
    source = Path(path)
    if inside_package(source):
        raise OwnerProfileError("inside-package", "the owner profile must live outside the EUTONOS package")
    try:
        if source.is_symlink() or not source.is_file():
            raise OwnerProfileError("not-a-file", f"{source} is not an ordinary file")
        if source.stat().st_size > MAX_BYTES:
            raise OwnerProfileError("too-big", f"{source} is larger than {MAX_BYTES} bytes")
        raw = source.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except OwnerProfileError:
        raise
    except (OSError, ValueError) as exc:
        raise OwnerProfileError("unreadable", f"{source} could not be read as JSON ({type(exc).__name__})") from exc
    problems = validate(value)
    if problems:
        raise OwnerProfileError("invalid", "; ".join(problems[:6]))
    found = secrets_found(value)
    if found:
        raise OwnerProfileError("secret-like", "the profile holds credential-like content: " + "; ".join(found[:4]))
    return value, raw


def target(home) -> Path:
    return Path(home) / "profiles" / "owner.json"


def _step(home, raw: bytes):
    return cs.FileStep("core", "owner-profile", target(home), raw, replace=True)


def _ctx(home, gen_id: str, dry_run: bool, record=None) -> dict:
    return {"home": str(home), "gen_id": gen_id, "dry_run": dry_run, "record": record, "projects": []}


def plan(path, home, *, gen_id: str = "standalone", record=None) -> dict:
    """``{owner_id, profile_sha256, effects}`` for what ``install`` would write. Writes nothing."""
    value, raw = read(path)
    ctx = _ctx(home, gen_id, True, record)
    return {"owner_id": value["owner_id"], "profile_sha256": cs.sha256(raw),
            "effects": cs.plan_steps(ctx, [_step(home, raw)])}


def install(path, home, *, gen_id: str = "standalone", record=None) -> dict:
    """Copy the profile to ``H/profiles/owner.json`` and read it back. Idempotent: the same bytes write nothing and
    return the Effect the record holds. Returns ``{owner_id, profile_sha256, target, effects}``."""
    value, raw = read(path)
    ctx = _ctx(home, gen_id, False, record)
    effects = cs.apply_steps(ctx, [_step(home, raw)])
    back = installed(home)
    if back is None or cs.sha256(back[1]) != cs.sha256(raw):
        raise OwnerProfileError("readback", "the copied profile did not read back as written")
    return {"owner_id": value["owner_id"], "profile_sha256": cs.sha256(raw), "target": str(target(home)),
            "effects": effects}


def installed(home) -> tuple[dict, bytes] | None:
    """The copied profile and its bytes, or None when none was installed (or the copy is unreadable)."""
    try:
        raw = target(home).read_bytes()
        return json.loads(raw.decode("utf-8")), raw
    except (OSError, ValueError):
        return None
