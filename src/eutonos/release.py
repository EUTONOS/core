"""Explicit project/module versions and exact content inventories; no version guessing."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
from importlib.resources import files

from .hidden_process import no_window

PROFILE = "eutonos.tokenslang.release.v1"
CHANNELS = ("dev", "release")
CHANNEL_ALIASES = {"development": "dev"}  # the earlier name; still read from an older eutonos.version.json


def canonical_channel(value):
    """``dev`` or ``release`` for a manifest channel; the earlier ``development`` reads as ``dev``; anything else is None."""
    value = CHANNEL_ALIASES.get(value, value)
    return value if value in CHANNELS else None


def validate(value):
    if not isinstance(value, dict) or value.get("profile") != PROFILE:
        raise ValueError("Select an EUTONOS release manifest.")
    for key in ("name", "version"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError("Release name and version are required.")
    if canonical_channel(value.get("channel")) is None:
        raise ValueError("Channel must be dev or release.")
    modules = value.get("modules", [])
    if not isinstance(modules, list):
        raise ValueError("Modules must be a list.")
    seen = set()
    for module in modules:
        if not isinstance(module, dict) or any(not isinstance(module.get(k), str) or not module[k].strip()
                                                for k in ("id", "name", "version")):
            raise ValueError("Each module needs a stable ID, name and version.")
        if module["id"] in seen:
            raise ValueError("Duplicate module ID.")
        seen.add(module["id"])
        patterns = module.get("paths")
        if not isinstance(patterns, list) or not patterns:
            raise ValueError("Each module needs explicit source paths or glob patterns.")
        for pattern in patterns:
            if not isinstance(pattern, str) or not pattern or "\\" in pattern or ":" in pattern or PurePosixPath(pattern).is_absolute() or ".." in PurePosixPath(pattern).parts:
                raise ValueError("Module paths must stay inside the repository.")
    return value


def inventory(root, manifest):
    """Fingerprint paths and raw bytes. Missing selections remain explicit, never empty success."""
    root = Path(root).resolve()
    validate(manifest)
    result = []
    for module in manifest.get("modules", []):
        selected, missing = {}, []
        for pattern in module["paths"]:
            matched = False
            for path in root.glob(pattern):
                if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                    continue
                if path.is_symlink() or not path.resolve().is_relative_to(root):
                    raise ValueError("Module source escapes repository: " + pattern)
                matched = True
                selected[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            if not matched:
                missing.append(pattern)
        fingerprint = hashlib.sha256(json.dumps(selected, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        result.append({"id": module["id"], "name": module["name"], "version": module["version"],
                       "fingerprint": fingerprint if not missing else None, "files": selected,
                       "file_count": len(selected), "missing": missing,
                       "status": "available" if not missing else "incomplete"})
    return result


def load(root):
    root = Path(root).resolve()
    path = root / "eutonos.version.json"
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if set(value) == {"manifest"}:
        relative = value["manifest"]
        if not isinstance(relative, str) or "\\" in relative or ":" in relative or ".." in PurePosixPath(relative).parts:
            raise ValueError("Manifest reference must stay inside the repository.")
        path = root / relative
        if not path.resolve().is_relative_to(root):
            raise ValueError("Manifest reference escapes repository.")
        value = json.loads(path.read_text(encoding="utf-8"))
    return validate(value)


def status(root):
    root = Path(root).resolve()
    runtime = json.loads(files("eutonos").joinpath("release.json").read_text(encoding="utf-8"))
    result = {"status": "not-configured", "runtime_version": runtime["version"],
              "runtime_modules": [{k: m[k] for k in ("id", "name", "version")} for m in runtime["modules"]]}
    try:
        manifest = load(root)
        if manifest is None:
            return result
        result.update(status="available", name=manifest["name"], version=manifest["version"],
                      channel=canonical_channel(manifest["channel"]), modules=inventory(root, manifest))
        def git(*args):
            proc = subprocess.run(["git", "-c", "safe.directory=" + str(root), "-C", str(root), *args],
                                  capture_output=True, text=True, timeout=10, **no_window())
            return proc.stdout.strip() if proc.returncode == 0 else None
        result["commit"] = git("rev-parse", "HEAD")
        dirty = git("status", "--porcelain", "--untracked-files=normal")
        result["dirty"] = None if dirty is None else bool(dirty)
        return result
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
        return {**result, "status": "error", "reason": str(exc)}


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "profile"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    value = status(args.root) if args.command == "status" else {
        "profile": PROFILE, "name": "Your project", "version": "0.1.0", "channel": "dev",
        "modules": [{"id": "app", "name": "Application", "version": "0.1.0", "paths": ["src/**/*.py"]}]}
    print(json.dumps(value, indent=2))
    return 1 if value.get("status") == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
