"""Build an EUTONOS release from HEAD: the wheel (core and kits) and its ``eutonos-release.json`` manifest.

    python tools/release.py build --root CHECKOUT --output DIST [--channel dev|beta|stable] [--min-compatible X.Y.Z]
                                  [--kit-wheels NAME=DIR] [--signing-key FILE]
    python tools/release.py keygen --out FILE

Only what HEAD commits is built: the core package, ``pyproject.toml`` and every kit file are read from the committed
blobs (``tools/build_wheel.py --from-head``), so edits, untracked files and a dirty working tree never reach a release.
The Claude plugin version (``plugin.json`` and the marketplace listing in the crosstalk kit) is stamped from the
release version in the built bytes only, so each release carries a new plugin version and no repository file changes.

The output folder gets one sub-folder per release, ``DIST/<artifact_id>/``, holding the wheel, ``eutonos-release.json``
and, when ``--signing-key`` is given, ``eutonos-release.json.sig`` (the Ed25519 signature of the manifest's exact bytes,
``src/eutonos/release_signing.py``). A release with no signature is accepted only by a developer's explicit
``eutonos-install update --source DIST --allow-unsigned``; a normal update refuses it. Publishing these files to the
public releases repository is the owner's step, never this script's. Nothing here uses the network.

``keygen`` makes the owner's release signing key: 32 random bytes written as hex to FILE (refused if FILE exists or lies
inside this repository) and the matching public key printed. The private key stays with the owner, out of the repository
and out of every build; only the public key is pasted into ``PINNED_KEYS`` in ``src/eutonos/release_signing.py``.
``--signing-key`` reads that file when you sign a release. The key is never printed.

The manifest (``eutonos.release.v1``)::

    {"profile": "eutonos.release.v1", "name": "EUTONOS", "version": "0.5.3", "artifact_id": "eutonos-0.5.3-<12 hex>",
     "wheel": "eutonos-0.5.3-py3-none-any.whl", "sha256": "<wheel sha256>", "channels": ["dev"],
     "min_compatible": null | "X.Y.Z", "source_commit": "<HEAD>", "plugin_version": "0.5.3",
     "interpreter": null | "cp314-win_amd64", "files": {"<wheel>": {"sha256": "...", "size": N}}}

``interpreter`` names the CPython the bundled kit wheels were built for (from the kits' ``wheels.lock.json``): a
release that needs a different interpreter than the installed one cannot be applied by an update, only by running
Setup again.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

TOOLS = Path(__file__).resolve().parent
SRC = TOOLS.parent / "src"
if str(SRC) not in sys.path and importlib.util.find_spec("eutonos") is None:
    sys.path.insert(0, str(SRC))


def _builder():
    spec = importlib.util.spec_from_file_location("eutonos_build_wheel_for_release", TOOLS / "build_wheel.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def head_commit(root: Path) -> str:
    done = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "HEAD"], capture_output=True, text=True)
    commit = done.stdout.strip()
    if done.returncode != 0 or len(commit) not in (40, 64):
        raise ValueError("A release is built from a git checkout with a commit; HEAD was not found.")
    return commit


def kit_interpreter(builder, root: Path, pyproject: str) -> str | None:
    """The one interpreter tag the kits' locked wheels were built for, or None when no kit locks wheels."""
    import tomllib
    kits = tomllib.loads(pyproject).get("tool", {}).get("eutonos", {}).get("kits", {})
    tags = set()
    for folder in kits.values():
        lock = builder.head_files(root, folder.rstrip("/") + "/" + builder.WHEEL_LOCK).get(folder.rstrip("/") + "/" + builder.WHEEL_LOCK)
        if lock is None:
            continue
        value = json.loads(lock.decode("utf-8"))
        if isinstance(value, dict) and value.get("wheels") and value.get("python"):
            tags.add(str(value["python"]))
    if len(tags) > 1:
        raise ValueError("The kits lock wheels for different interpreters: " + ", ".join(sorted(tags)))
    return next(iter(tags), None)


def manifest_bytes(manifest: dict) -> bytes:
    """The manifest as it is written and signed: sorted keys, two-space indent, one trailing newline."""
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")


def read_seed(path) -> bytes:
    """The private seed in a key file written by ``keygen`` (64 hex digits), or a ``ValueError`` that never shows it."""
    text = Path(path).read_text(encoding="ascii", errors="replace").strip().lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError("The signing key file must hold 64 hex digits (the file keygen writes).")
    return bytes.fromhex(text)


def keygen(out, root=None) -> dict:
    """Write a new signing seed to `out` and return its public key. Refuses an existing file and any path inside the
    repository, so the private key cannot be committed by accident."""
    from eutonos import release_signing as rsg
    target = Path(out).resolve()
    checkout = Path(root).resolve() if root else TOOLS.parent
    if target.exists():
        raise FileExistsError(f"{target} already exists; keygen never overwrites a key.")
    if target == checkout or checkout in target.parents:
        raise ValueError("Keep the private key outside the repository: choose a path that is not inside " + str(checkout))
    seed = os.urandom(32)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "x", encoding="ascii") as handle:
        handle.write(seed.hex() + "\n")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    public = rsg.public_key(seed).hex()
    return {"key_file": str(target), "public_key": public, "key_id": rsg.key_id(public),
            "next_action": "Paste the public key into PINNED_KEYS in src/eutonos/release_signing.py and keep the key file "
                           "private and backed up outside the repository."}


def build_release(root, output, *, channel: str | None = None, min_compatible: str | None = None, wheelhouses=None,
                  signing_seed: bytes | None = None) -> dict:
    """Build the wheel and manifest for HEAD into ``output/<artifact_id>/``, signed when `signing_seed` is given.
    Returns what was written."""
    from eutonos import install_record as ir
    from eutonos import release_signing as rsg
    from eutonos import release_source as rs
    builder = _builder()
    root = Path(root).resolve(strict=True)
    commit = head_commit(root)
    core = builder.head_files(root, "src/eutonos/release.json", "pyproject.toml")
    shipped = json.loads(core["src/eutonos/release.json"].decode("utf-8"))
    version = shipped["version"]
    if channel is None:
        channel = {"dev": "dev", "development": "dev", "release": "stable"}.get(shipped.get("channel"), "dev")
    if channel not in rs.CHANNELS:
        raise ValueError("The channel must be one of " + ", ".join(rs.CHANNELS) + ".")
    interpreter = kit_interpreter(builder, root, core["pyproject.toml"].decode("utf-8"))
    public = rsg.public_key(signing_seed).hex() if signing_seed is not None else None
    if public is not None and rsg.PINNED_KEYS and public not in rsg.PINNED_KEYS:
        raise ValueError("The signing key is not one of the keys pinned in this build (PINNED_KEYS), so installs would "
                         "refuse the release it signs.")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="eutonos-release-", dir=output) as scratch:
        wheel = builder.build(root, Path(scratch) / "wheel", wheelhouses, head_only=True, plugin_version=version)
        digest = ir.sha256_file(wheel)
        artifact = ir.artifact_id(version, digest)
        manifest = {"profile": rs.PROFILE, "name": "EUTONOS", "version": version, "artifact_id": artifact,
                    "wheel": wheel.name, "sha256": digest, "channels": [channel], "min_compatible": min_compatible,
                    "source_commit": commit, "plugin_version": version, "interpreter": interpreter,
                    "files": {wheel.name: {"sha256": digest, "size": wheel.stat().st_size}}}
        rs.validate_manifest(manifest)  # what we write is what the installer will accept
        target = output / artifact
        if target.exists():
            raise FileExistsError(target)
        staged = Path(scratch) / "release"
        staged.mkdir()
        shutil.move(str(wheel), staged / wheel.name)
        data = manifest_bytes(manifest)
        (staged / rs.MANIFEST_NAME).write_bytes(data)
        if signing_seed is not None:
            signature = rsg.sign(signing_seed, data)
            assert rsg.verify(bytes.fromhex(public), signature, data)  # never publish a signature that does not verify
            (staged / rsg.SIGNATURE_NAME).write_text(rsg.signature_text(signature), encoding="ascii")
        shutil.move(str(staged), target)
    return {"folder": str(target), "artifact_id": artifact, "version": version, "sha256": digest, "channels": [channel],
            "source_commit": commit, "plugin_version": version, "interpreter": interpreter,
            "signed_by": rsg.key_id(public) if public else None}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="build the wheel and manifest for HEAD")
    build.add_argument("--root", type=Path, default=Path.cwd())
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--channel", help="dev, beta or stable (default: from src/eutonos/release.json)")
    build.add_argument("--min-compatible", help="the oldest installed version this release can be applied over")
    build.add_argument("--kit-wheels", action="append", default=[], metavar="NAME=DIR")
    build.add_argument("--signing-key", type=Path, help="the key file keygen wrote; signs eutonos-release.json")
    gen = sub.add_parser("keygen", help="make the release signing key (the private key stays outside the repository)")
    gen.add_argument("--out", type=Path, required=True, help="where to write the private key (not inside the repository)")
    args = parser.parse_args(argv)
    try:
        if args.command == "keygen":
            print(json.dumps(keygen(args.out), indent=2))
            return 0
        wheelhouses = {}
        for value in args.kit_wheels:
            name, sep, folder = value.partition("=")
            if not sep or not name or not folder:
                parser.error("--kit-wheels takes NAME=DIR")
            wheelhouses[name] = Path(folder)
        seed = read_seed(args.signing_key) if args.signing_key else None
        print(json.dumps(build_release(args.root, args.output, channel=args.channel, min_compatible=args.min_compatible,
                                       wheelhouses=wheelhouses, signing_seed=seed), indent=2))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"outcome": "error", "error": f"{type(exc).__name__}: {exc}"}, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
