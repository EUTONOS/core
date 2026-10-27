"""The installer's OS and app calls, each behind one injectable method (IPC5 lane A).

`Ports` makes the real calls: it runs processes, builds a venv, edits the user PATH and the Run key and reads or
writes Startup shortcuts. `FakePorts` keeps all of that in memory (and builds a fake runtime from the wheel's own
bytes), so tests and the install check never reach the real machine. The real methods are small on purpose and are
exercised only by a live install, never by a test.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PATH_KEY = r"Environment"
REG_SZ = 1  # winreg.REG_SZ / REG_EXPAND_SZ, named here so the fakes and the record need no winreg
REG_EXPAND_SZ = 2
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# The Installed apps (Add/Remove Programs) key Inno Setup writes for the Setup AppId (a test pins it to EUTONOS.iss).
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{26E45B59-2A6F-498A-81AC-32B29F5F758E}_is1"
# The companion's one HKCU value, written when the person picks an Appearance in its menu (the same key as
# desktop_companion_windows.PREFERENCES_KEY; a test pins them together). Declared as permissions entry companion-appearance.
APPEARANCE_KEY = r"Software\EUTONOS\DesktopCompanion"
_SHORTCUT_SCRIPT = (
    "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:EU_LNK);"
    "$s.TargetPath=$env:EU_TARGET;$s.Arguments=$env:EU_ARGS;$s.WorkingDirectory=$env:EU_CWD;"
    "$s.WindowStyle=1;$s.Description=$env:EU_DESC;$s.Save()")
_SHORTCUT_READ = (
    "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:EU_LNK);"
    "ConvertTo-Json -Compress @{target=$s.TargetPath;args=$s.Arguments;cwd=$s.WorkingDirectory;desc=$s.Description}")
# Version entries that are a Microsoft Store (Appx/MSIX) package rather than a command, by the package's name. Codex's
# desktop app is the Store package OpenAI.Codex (its process is named ChatGPT); it is its own ``codex-store`` entry,
# separate from the ``codex`` command, so an update of one is never read as a change of the other.
APPX_PACKAGES = {"codex-store": "OpenAI.Codex"}
_APPX_VERSION = ("$p = Get-AppxPackage -Name $env:EUTONOS_APPX_PACKAGE | Sort-Object {[version]$_.Version} -Descending | "
                 "Select-Object -First 1; if ($p) { [Console]::Out.Write($p.Version) }")


def startup_folder() -> Path:
    """The Windows Startup folder Windows runs: under %APPDATA%, which may be redirected or roaming."""
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def default_os_name() -> str:
    return "windows" if sys.platform == "win32" else "macos"


def wheel_entry_points(wheel) -> list[str]:
    """Names of the console and gui scripts a wheel declares (its ``entry_points.txt``)."""
    names = []
    with zipfile.ZipFile(wheel) as archive:
        for member in archive.namelist():
            if member.endswith(".dist-info/entry_points.txt"):
                section = ""
                for line in archive.read(member).decode("utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("["):
                        section = line.strip("[]")
                    elif "=" in line and section in ("console_scripts", "gui_scripts"):
                        names.append(line.split("=", 1)[0].strip())
    return sorted(set(names))


class Ports:
    """The real machine. Construct with ``Ports()``; nothing runs until a method is called."""

    def __init__(self, os_name: str | None = None):
        self.os_name = os_name or default_os_name()

    # ----------------------------------------------------------- processes
    def run(self, argv, *, timeout=900, cwd=None, env=None, input_text=None) -> tuple[int, str, str]:
        done = subprocess.run([str(item) for item in argv], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, cwd=cwd, env=env, input=input_text,
                              creationflags=NO_WINDOW)
        return done.returncode, done.stdout or "", done.stderr or ""

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def app_version(self, name: str) -> dict:
        """``{"state": "ok"|"not-found"|"unreadable", "version": str|None}`` from the app's own ``--version`` (a local
        print; no network, no account or credential file is opened). A Microsoft Store entry (``codex-store``) is read
        from the installed package instead (``appx_version``) and never from a command: the command and the Store app
        are separate entries, so neither stands in for the other."""
        from .self_repair import parse_version
        if name in APPX_PACKAGES:
            return self.appx_version(name)
        executable = self.which(name)
        row = {"state": "not-found", "version": None}
        if executable:
            try:
                code, out, err = self.run([executable, "--version"], timeout=15, input_text="")
                version = parse_version(out or err) if code == 0 else None
                row = {"state": "ok", "version": version} if version else {"state": "unreadable", "version": None}
            except (OSError, subprocess.SubprocessError):
                row = {"state": "unreadable", "version": None}
        return row

    def appx_version(self, name: str) -> dict:
        """The version of a Microsoft Store (Appx/MSIX) package entry, read-only: ``Get-AppxPackage -Name <package>``
        lists what the person has installed and starts nothing (no network, no account or credential file). Only
        Windows, and only an entry in ``APPX_PACKAGES``; anything else, or no package, is ``not-found``. The newest
        version wins when several are present."""
        from .self_repair import parse_version
        package = APPX_PACKAGES.get(name)
        if self.os_name != "windows" or not package:
            return {"state": "not-found", "version": None}
        try:
            code, out, _err = self.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _APPX_VERSION],
                                       timeout=60, env=dict(os.environ, EUTONOS_APPX_PACKAGE=package), input_text="")
        except (OSError, subprocess.SubprocessError):
            return {"state": "unreadable", "version": None}
        if code != 0:
            return {"state": "unreadable", "version": None}
        if not out.strip():
            return {"state": "not-found", "version": None}
        version = parse_version(out)
        return {"state": "ok", "version": version} if version else {"state": "unreadable", "version": None}

    # ------------------------------------------------------------- runtime
    def runtime_python(self, runtime: Path) -> Path:
        return runtime / "Scripts" / "python.exe" if self.os_name == "windows" else runtime / "bin" / "python"

    def scripts_dir(self, runtime: Path) -> Path:
        return runtime / ("Scripts" if self.os_name == "windows" else "bin")

    def site_packages(self, runtime: Path) -> Path:
        if self.os_name == "windows":
            return runtime / "Lib" / "site-packages"
        found = sorted((runtime / "lib").glob("python3*/site-packages"))
        return found[-1] if found else runtime / "lib" / "site-packages"

    def build_runtime(self, runtime: Path, wheel: Path) -> None:
        """A venv at ``runtime`` with the wheel installed (no index, no dependencies)."""
        from . import permissions
        pin = permissions.entry_by_id(permissions.load(), "runtime-pip")["value"]
        pip_wheel = Path(__file__).with_name("runtime_wheels") / pin["file"]
        if hashlib.sha256(pip_wheel.read_bytes()).hexdigest() != pin["sha256"]:
            raise RuntimeError("Bundled runtime pip wheel SHA-256 does not match")
        code, _, err = self.run([sys.executable, "-I", "-m", "venv", str(runtime)])
        if code:
            raise RuntimeError("venv creation failed: " + err[-300:])
        code, _, err = self.run([self.runtime_python(runtime), "-I", "-m", "pip", "--isolated", "install",
                                 "--no-index", "--no-deps", "--disable-pip-version-check", "--no-input",
                                 "--upgrade", "--force-reinstall", str(pip_wheel)])
        if code:
            raise RuntimeError("Offline runtime pip repair failed: " + err[-300:])
        code, out, err = self.run([self.runtime_python(runtime), "-I", "-c",
                                  "import importlib.metadata; print(importlib.metadata.version('pip'))"])
        if code or out.strip() != pin["version"]:
            raise RuntimeError("Runtime pip version readback failed: " + err[-300:])
        code, _, err = self.run([self.runtime_python(runtime), "-I", "-m", "pip", "install", "--no-index", "--no-deps",
                                 "--disable-pip-version-check", "--no-input", str(wheel)])
        if code:
            raise RuntimeError("pip install failed: " + err[-300:])

    def launcher_name(self, name: str) -> str:
        return name + (".exe" if self.os_name == "windows" else "")

    def seams(self) -> dict:
        """The ``ctx["seams"]`` the components receive. Empty on the real machine: they make their own real calls."""
        return {}

    # ---------------------------------------------------------- handover
    def handover(self, python: Path, gen_id: str, home: Path, token: str | None = None) -> tuple[int, str, str]:
        """Continue a pending generation in the new runtime, so its own components do the effects. `token` proves
        the caller holds the machine lock that the child must not take again."""
        env = dict(os.environ, EUTONOS_HOME=str(home))
        argv = [python, "-I", "-B", "-m", "eutonos.installer", "continue", "--gen-id", gen_id]
        if token:
            argv += ["--token", token]
        return self.run(argv, env=env)

    # ------------------------------------------------------------ user PATH
    def user_path_state(self) -> tuple[str, int] | None:
        """The user PATH as ``(value, registry type)`` (``REG_SZ`` 1 or ``REG_EXPAND_SZ`` 2), or None when the value
        does not exist. The type is read so a later write can keep it."""
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PATH_KEY) as key:
                value, kind = winreg.QueryValueEx(key, "Path")
                return value, kind
        except FileNotFoundError:
            return None

    def user_path_get(self) -> str | None:
        state = self.user_path_state()
        return None if state is None else state[0]

    def user_path_set(self, value: str, kind: int | None = None) -> None:
        """Write the user PATH as registry type `kind` (default ``REG_EXPAND_SZ``, the type Windows itself uses)."""
        import ctypes
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PATH_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "Path", 0, kind or REG_EXPAND_SZ, value)
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 2, 5000, ctypes.byref(ctypes.c_ulong()))

    def user_path_delete(self) -> None:
        """Remove the user PATH value itself (for a value EUTONOS created where there was none)."""
        import ctypes
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PATH_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, "Path")
        except FileNotFoundError:
            return
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 2, 5000, ctypes.byref(ctypes.c_ulong()))

    # ------------------------------------------------------ user environment variable
    def user_env_get(self, name: str) -> str | None:
        """A user environment variable (HKCU Environment), or None when it is not set."""
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PATH_KEY) as key:
                return winreg.QueryValueEx(key, name)[0]
        except FileNotFoundError:
            return None

    def user_env_set(self, name: str, value: str) -> None:
        import ctypes
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PATH_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 2, 5000, ctypes.byref(ctypes.c_ulong()))

    def user_env_delete(self, name: str) -> None:
        import ctypes
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PATH_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            return
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 2, 5000, ctypes.byref(ctypes.c_ulong()))

    # ------------------------------------------------------------- Run key
    def runkey_get(self, name: str) -> str | None:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                return winreg.QueryValueEx(key, name)[0]
        except FileNotFoundError:
            return None

    def runkey_set(self, name: str, value: str) -> None:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def runkey_delete(self, name: str) -> None:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass

    # ------------------------------------------------- Installed apps entry (read only)
    def uninstall_entry_get(self) -> dict | None:
        """The Installed apps entry Setup's installer wrote (``DisplayName``, ``DisplayVersion``, ``UninstallString``),
        or None when there is none. Read only; only Windows has one."""
        if self.os_name != "windows":
            return None
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
                row = {}
                for name in ("DisplayName", "DisplayVersion", "UninstallString"):
                    try:
                        row[name] = str(winreg.QueryValueEx(key, name)[0])
                    except FileNotFoundError:
                        row[name] = None
                return row
        except FileNotFoundError:
            return None

    # ------------------------------------------------------- companion Appearance value
    def appearance_get(self) -> str | None:
        """The stored companion Appearance value (``system``, ``light`` or ``dark``), or None when none is stored."""
        if self.os_name != "windows":
            return None
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPEARANCE_KEY) as key:
                return str(winreg.QueryValueEx(key, "Appearance")[0])
        except FileNotFoundError:
            return None

    def appearance_delete(self) -> bool:
        """Remove the Appearance value, then its key and the parent EUTONOS key when nothing else is in them. True
        when the value was removed. Only this one value is deleted; a key the person put other values in is left."""
        if self.os_name != "windows":
            return False
        import winreg
        removed = False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPEARANCE_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, "Appearance")
                removed = True
        except FileNotFoundError:
            pass
        for path in (APPEARANCE_KEY, APPEARANCE_KEY.rsplit("\\", 1)[0]):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)  # refused while the key still holds a value or subkey
            except OSError:
                break
        return removed

    # ------------------------------------------------------------ shortcuts
    def startup_folder(self) -> Path:
        return startup_folder()

    def shortcut_read(self, path: Path) -> dict | None:
        if not Path(path).is_file():
            return None
        code, out, _ = self.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _SHORTCUT_READ],
                                env=dict(os.environ, EU_LNK=str(path)), timeout=60)
        try:
            return json.loads(out) if code == 0 else None
        except ValueError:
            return None

    def shortcut_write(self, path: Path, target: str, args: str, cwd: str, description: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        code, _, err = self.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _SHORTCUT_SCRIPT],
                                env=dict(os.environ, EU_LNK=str(path), EU_TARGET=target, EU_ARGS=args, EU_CWD=cwd,
                                         EU_DESC=description), timeout=60)
        if code:
            raise RuntimeError("shortcut write failed: " + err[-200:])


class FakePorts(Ports):
    """In-memory ports. `build_runtime` unpacks the wheel into a fake venv; PATH, the Run key and shortcuts live in
    dictionaries; `run` records each call and answers from `answers` (a callable ``argv -> (code, out, err)``)."""

    def __init__(self, os_name: str | None = None, *, startup: Path | None = None):
        super().__init__(os_name)
        self.calls: list[list[str]] = []
        self.answers = None
        self.path_value: str | None = "C:\\Windows" if self.os_name == "windows" else None
        self.path_kind: int | None = REG_EXPAND_SZ if self.path_value is not None else None
        self.runkeys: dict[str, str] = {}
        self.uninstall_entry: dict | None = None
        self.appearance: str | None = None
        self.user_env: dict[str, str] = {}
        self.shortcuts: dict[str, dict] = {}
        self.startup = Path(startup) if startup else None
        self.handovers: list[tuple] = []
        self.handover_call = None
        self.fail_runtime = False
        self.seam_map: dict = {}  # name -> fake callable/object handed to components as ctx["seams"]
        self.has = {"claude": "/fake/claude", "codex": "/fake/codex"}
        self.app_versions: dict = {}  # name -> version text; absent means not found, False means unreadable

    def run(self, argv, *, timeout=900, cwd=None, env=None, input_text=None):
        self.calls.append([str(item) for item in argv])
        if self.answers is not None:
            return self.answers(self.calls[-1])
        claude = self.seam_map.get("claude_run")
        if claude is not None and self.calls[-1][1:2] == ["plugin"]:  # the fake Claude app answers its plugin commands
            code, output = claude(self.calls[-1])
            return code, output, ""
        return 0, "", ""

    def uninstall_entry_get(self):
        return self.uninstall_entry

    def appearance_get(self):
        return self.appearance

    def appearance_delete(self):
        had, self.appearance = self.appearance is not None, None
        return had

    def which(self, name):
        return self.has.get(name)

    def app_version(self, name):
        value = self.app_versions.get(name)
        if value is None:
            return {"state": "not-found", "version": None}
        return {"state": "unreadable", "version": None} if value is False else {"state": "ok", "version": str(value)}

    def seams(self) -> dict:
        return dict(self.seam_map)

    def build_runtime(self, runtime: Path, wheel: Path) -> None:
        if self.fail_runtime:
            raise RuntimeError("fake runtime failure")
        site = self.site_packages(runtime)
        site.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(site)
        scripts = self.scripts_dir(runtime)
        scripts.mkdir(parents=True, exist_ok=True)
        python = self.runtime_python(runtime)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"fake python\n")
        for name in wheel_entry_points(wheel):
            (scripts / self.launcher_name(name)).write_bytes(f"launcher {name} for {runtime.name}\n".encode("utf-8"))

    def handover(self, python, gen_id, home, token=None):
        self.handovers.append((str(python), gen_id, str(home)))
        if self.handover_call is not None:
            return self.handover_call(python, gen_id, home, token)
        return 0, "", ""

    def user_path_state(self):
        return None if self.path_value is None else (self.path_value, self.path_kind or REG_EXPAND_SZ)

    def user_path_get(self):
        return self.path_value

    def user_path_set(self, value, kind=None):
        self.path_value = value
        self.path_kind = kind or REG_EXPAND_SZ

    def user_path_delete(self):
        self.path_value = None
        self.path_kind = None

    def user_env_get(self, name):
        return self.user_env.get(name)

    def user_env_set(self, name, value):
        self.user_env[name] = value

    def user_env_delete(self, name):
        self.user_env.pop(name, None)

    def runkey_get(self, name):
        return self.runkeys.get(name)

    def runkey_set(self, name, value):
        self.runkeys[name] = value

    def runkey_delete(self, name):
        self.runkeys.pop(name, None)

    def startup_folder(self):
        if self.startup is None:
            raise RuntimeError("FakePorts needs a startup folder")
        return self.startup

    def shortcut_read(self, path):
        try:  # read the file like the real port, so a restored backup is seen
            return json.loads(Path(path).read_bytes())
        except (OSError, ValueError):
            return None

    def shortcut_write(self, path, target, args, cwd, description):
        self.shortcuts[str(path)] = {"target": target, "args": args, "cwd": cwd, "desc": description}
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(json.dumps(self.shortcuts[str(path)], sort_keys=True).encode("utf-8"))
