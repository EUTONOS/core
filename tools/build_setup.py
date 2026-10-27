"""Build ``dist/EUTONOS-Setup-<version>.exe``, the Windows Setup wizard (Inno Setup 6, per-user).

    python tools/build_setup.py [--python "C:\\Program Files\\Python314"] [--iscc PATH] [--output DIR] [--work DIR] [--no-compile]

Steps: refuse to go on when the generated ``src/eutonos/offers/offers.json`` (EUTONOS's own feature and capability
lists, which the wheel carries) is out of date with the catalogue documents (fix: ``python tools/build_offers.py``,
then commit the result); build the wheel with ``tools/build_wheel.py``; check and copy a python.org CPython (default
``C:\\Program Files\\Python314``) into a staging folder without its test suites, ``site-packages`` contents, ``Doc``,
``include``, ``libs``, ``Scripts``, ``Tools``, ``idlelib``, ``turtledemo`` and ``__pycache__`` (``ensurepip`` and ``venv``
stay: the installer builds EUTONOS's runtime from this Python); stage the wheel and ``installer/windows/bootstrap.py``;
run ``ISCC``. The wheel carries the kits' committed wheels (Simon's Pillow and pyautogui), and the build refuses a
Python those wheels were not built for. Nothing is downloaded. It prints the sha256 of the wheel and of Setup.exe. The staging folder is
``build/setup/stage`` under the repository unless ``--work DIR`` names another place (it removes only ``DIR/stage``).

The same source gives the same Setup.exe, byte for byte: the wheel's zip entries carry one fixed time stamp and
permission (``tools/build_wheel.py``; ``SOURCE_DATE_EPOCH`` sets the time, otherwise 1980-01-01), and the Inno script
stores no file time stamps (``notimestamp``), so neither the clock nor the time the Python install or the checkout
was written reaches the output. The same Python install and Inno Setup version are needed for the same bytes.

The Python must be a 64-bit python.org install of 3.12 or newer with tkinter; a venv, the embeddable zip, a Store
install or anything else is refused with the reason.

Setup.exe is not code-signed. Windows SmartScreen will say "Windows protected your PC": choose "More info", then
"Run anyway". A signing certificate costs money every year; buying one is the owner's decision, not made here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

DEFAULT_PYTHON = r"C:\Program Files\Python314"
MIN_PYTHON = (3, 12)
ISCC_CANDIDATES = (r"C:\Users\{user}\AppData\Local\Programs\Inno Setup 6\ISCC.exe",
                   r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe", r"C:\Program Files\Inno Setup 6\ISCC.exe")
# Left out of the copy of Python: top-level folders and files, anywhere-folders, and folders under Lib.
TOP_FOLDERS = {"doc", "include", "libs", "scripts", "tools"}
TOP_FILES = {"news.txt"}
ANYWHERE_FOLDERS = {"__pycache__", "test", "tests", "idle_test"}
LIB_FOLDERS = {"idlelib", "turtledemo"}
PROBE = ("import json,struct,sys,sysconfig\n"
         "try:\n import tkinter, _tkinter\n tk=True\nexcept Exception:\n tk=False\n"
         "print(json.dumps({'version':list(sys.version_info[:3]),'bits':struct.calcsize('P')*8,'tkinter':tk,"
         "'prefix':sys.prefix,'base_prefix':sys.base_prefix,'platform':sysconfig.get_platform()}))")
# Kits whose committed wheels are built for one CPython: the Python Setup carries must be that one, because the
# installer builds EUTONOS's runtime, and from it each kit's environment, from this Python.
KIT_WHEEL_LOCKS = ("tools/simon/wheels.lock.json",)


class BuildError(Exception):
    """A refusal with the reason in plain words."""


def _probe(python: Path) -> dict:
    done = subprocess.run([str(python), "-I", "-c", PROBE], capture_output=True, text=True, timeout=60)
    if done.returncode:
        raise BuildError(f"{python} would not run: {(done.stderr or done.stdout).strip()[-200:]}")
    return json.loads(done.stdout.strip().splitlines()[-1])


def check_python(folder, probe=None) -> dict:
    """Refuse a Python that Setup cannot carry: not a python.org layout, older than 3.12, not 64-bit or no tkinter.
    Returns the probe's answer (version, bits, tkinter)."""
    folder = Path(folder)
    exe = folder / "python.exe"
    if not exe.is_file():
        raise BuildError(f"{folder} has no python.exe: point --python at a python.org install folder.")
    if "windowsapps" in str(folder).lower():
        raise BuildError(f"{folder} is a Microsoft Store Python; install Python from python.org instead.")
    if (folder / "pyvenv.cfg").is_file():
        raise BuildError(f"{folder} is a virtual environment, not a python.org install.")
    if any(folder.glob("python*._pth")):
        raise BuildError(f"{folder} is the embeddable zip, which has no venv or ensurepip; use the python.org installer.")
    missing = [name for name in ("pythonw.exe", "LICENSE.txt", "DLLs", "Lib", "tcl") if not (folder / name).exists()]
    if not list(folder.glob("python3*.dll")):
        missing.append("python3*.dll")
    if missing:
        raise BuildError(f"{folder} is not a python.org layout (missing {', '.join(missing)}).")
    found = (probe or _probe)(exe)
    version = tuple(found["version"])
    if version < MIN_PYTHON:
        raise BuildError(f"Python {'.'.join(map(str, version))} is too old; EUTONOS needs 3.12 or newer.")
    if found["bits"] != 64:
        raise BuildError(f"This Python is {found['bits']}-bit; Setup carries a 64-bit one.")
    if not found["tkinter"]:
        raise BuildError("This Python has no tkinter; the EUTONOS companion needs it. Reinstall with tcl/tk selected.")
    if os.path.normcase(str(found.get("prefix"))) != os.path.normcase(str(found.get("base_prefix"))):
        raise BuildError(f"{folder} is a virtual environment, not a python.org install.")
    if not (folder / "Lib" / "ensurepip").is_dir() or not (folder / "Lib" / "venv").is_dir():
        raise BuildError(f"{folder} lacks ensurepip or venv, which EUTONOS's installer needs.")
    return found


def python_tag(found: dict) -> str:
    """``cpXY-<platform>`` for a probe answer (``win-amd64`` when an older probe did not say)."""
    version = found["version"]
    platform = str(found.get("platform") or "win-amd64")
    return f"cp{version[0]}{version[1]}-" + platform.replace("-", "_").replace(".", "_")


def check_kit_python(root, found) -> None:
    """Refuse a Python that the kits' committed wheels were not built for (``wheels.lock.json`` ``python``)."""
    have = python_tag(found)
    for relative in KIT_WHEEL_LOCKS:
        path = Path(root) / relative
        if not path.is_file():
            continue
        lock = json.loads(path.read_text(encoding="utf-8"))
        wanted = lock.get("python") if isinstance(lock, dict) else None
        if lock.get("wheels") and wanted and wanted != have:
            raise BuildError(f"{relative} bundles wheels built for {wanted}, but this Python is {have}. Build Setup "
                             f"with that Python (--python), or refresh the lock for this one (docs/Simon/INSTALL.md).")


def _skipped(relative: Path, is_dir: bool) -> bool:
    parts = [part.lower() for part in relative.parts]
    if parts[0] in TOP_FOLDERS and (is_dir or len(parts) > 1):
        return True
    if len(parts) == 1 and not is_dir and parts[0] in TOP_FILES:
        return True
    if any(part in ANYWHERE_FOLDERS for part in parts[:-1] + ([parts[-1]] if is_dir else [])):
        return True
    if parts[0] == "lib":
        if len(parts) >= 2 and parts[1] in LIB_FOLDERS:
            return True
        if len(parts) >= 3 and parts[1] == "site-packages":
            return True
    return not is_dir and relative.suffix.lower() in (".pyc", ".pyo")


def stage_python(source, target) -> dict:
    """Copy `source` to `target` without the excluded folders. Returns ``{"files": n, "bytes": n}``. `Lib/site-packages`
    is kept as an empty folder."""
    source, target = Path(source), Path(target)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    count = size = 0
    for current, folders, files in os.walk(source):
        here = Path(current).relative_to(source)
        keep = []
        for name in sorted(folders):
            relative = here / name
            if not _skipped(relative, True):
                keep.append(name)
                (target / relative).mkdir(parents=True, exist_ok=True)
        folders[:] = keep
        for name in sorted(files):
            relative = here / name
            if _skipped(relative, False):
                continue
            shutil.copy2(source / relative, target / relative)
            count += 1
            size += (target / relative).stat().st_size
    (target / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)
    return {"files": count, "bytes": size}


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def find_iscc(explicit=None) -> Path | None:
    """The Inno Setup compiler: `explicit`, ``ISCC`` on PATH, or the usual install folders."""
    candidates = [explicit] if explicit else [shutil.which("ISCC")] + [
        item.format(user=os.environ.get("USERNAME", "")) for item in ISCC_CANDIDATES]
    for item in candidates:
        if item and Path(item).is_file():
            return Path(item)
    return None


def check_offers(root) -> None:
    """Refuse to build while ``src/eutonos/offers/offers.json`` (the four lists "What EUTONOS offers" carries) differs
    from what ``tools/build_offers.py`` makes from the catalogue documents; the refusal names the command that fixes it."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import build_offers
    try:
        build_offers.check(root)
    except build_offers.OffersError as exc:
        raise BuildError(f"Setup would carry an out-of-date list of what EUTONOS offers. {exc}") from exc


def build(root, *, python=None, output=None, iscc=None, compile_setup=True, probe=None, run=None, work=None,
          offers_check=None) -> dict:
    """Build the wheel, stage everything and (unless `compile_setup` is False) compile Setup.exe. The staging folder
    is ``<work>/stage`` when `work` is given (for example a temporary folder outside the repository), otherwise
    ``build/setup/stage`` under `root`. `offers_check` replaces the drift check on the offers file (tests only)."""
    root = Path(root).resolve()
    output = Path(output) if output else root / "dist"
    run = run or subprocess.run
    found = check_python(python or DEFAULT_PYTHON, probe)
    check_kit_python(root, found)
    (offers_check or check_offers)(root)
    stage = Path(work).resolve() / "stage" if work else root / "build" / "setup" / "stage"
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "wheel").mkdir(parents=True)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import build_wheel
    wheel = build_wheel.build(root, stage / "wheel")
    copied = stage_python(python or DEFAULT_PYTHON, stage / "python")
    shutil.copy2(root / "installer" / "windows" / "bootstrap.py", stage / "bootstrap.py")
    version = wheel.name.split("-")[1]
    result = {"version": version, "wheel": str(wheel), "wheel_sha256": sha256_file(wheel),
              "python": ".".join(map(str, found["version"])), "python_files": copied["files"],
              "python_bytes": copied["bytes"], "stage": str(stage), "setup": None}
    if not compile_setup:
        return result
    compiler = find_iscc(iscc)
    if compiler is None:
        raise BuildError("Inno Setup's ISCC.exe was not found; install Inno Setup 6 or pass --iscc.")
    output.mkdir(parents=True, exist_ok=True)
    name = f"EUTONOS-Setup-{version}"
    command = [str(compiler), f"/DAppVersion={version}", f"/DStageDir={stage}", f"/DOutputDir={output}",
               f"/DOutputBaseFilename={name}", str(root / "installer" / "windows" / "EUTONOS.iss")]
    done = run(command, capture_output=True, text=True)
    if done.returncode:
        raise BuildError("ISCC failed:\n" + ((done.stdout or "") + (done.stderr or ""))[-2000:])
    setup = output / f"{name}.exe"
    if not setup.is_file():
        raise BuildError(f"ISCC finished but {setup} was not written.")
    result["setup"] = str(setup)
    result["setup_bytes"] = setup.stat().st_size
    result["setup_sha256"] = sha256_file(setup)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--python", default=DEFAULT_PYTHON, help="the python.org install folder to carry")
    parser.add_argument("--iscc", help="path to ISCC.exe")
    parser.add_argument("--output", type=Path, help="where Setup.exe goes (default: dist/)")
    parser.add_argument("--work", type=Path, help="where the staging folder goes (default: build/setup under the repository)")
    parser.add_argument("--no-compile", action="store_true", help="stage only; do not run ISCC")
    args = parser.parse_args(argv)
    try:
        result = build(args.root, python=args.python, output=args.output, iscc=args.iscc,
                       compile_setup=not args.no_compile, work=args.work)
    except (BuildError, ValueError, OSError) as exc:
        print(f"build_setup: {exc}", file=sys.stderr)
        return 1
    print(f"wheel   {result['wheel']}\n        sha256 {result['wheel_sha256']}")
    print(f"python  {result['python']} ({result['python_files']} files, {result['python_bytes'] // (1 << 20)} MB)")
    if result["setup"]:
        print(f"setup   {result['setup']} ({result['setup_bytes']} bytes)\n        sha256 {result['setup_sha256']}")
        print("Setup.exe is not code-signed: Windows SmartScreen will say \"Windows protected your PC\"; "
              "choose \"More info\", then \"Run anyway\". A signing certificate is the owner's spending decision.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
