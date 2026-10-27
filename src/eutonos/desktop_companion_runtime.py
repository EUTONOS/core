"""Hidden EUTONOS Companion startup, single coordinator and crash supervision."""
from __future__ import annotations

import argparse
import csv
import ctypes
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

from . import home as eutonos_home

STARTUP_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
STARTUP_NAME = 'EUTONOS Companion'
LEGACY_STARTUP_NAMES = ('EUTONOS Codex Context Companion',)
COORDINATOR_MUTEX = r'Local\EUTONOS-Companion-Coordinator'
SUPERVISOR_MUTEX = r'Local\EUTONOS-Companion-Supervisor'
COORDINATOR_TITLE = 'EUTONOS Companion Coordinator'
HIDDEN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
TASK_NAME = r'\EUTONOS Desktop Companion'
TASK_DESCRIPTION = 'EUTONOS-owned interactive desktop companion recovery'
DUPLICATE_COORDINATOR = 75


def runtime_diagnostic(root, event, **metadata):
    """Best-effort bounded runtime health metadata, never traceback/prompt text."""
    if not root:
        return
    temporary = None
    try:
        target = Path(root) / '.eutonos-local' / 'desktop-companion-runtime.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        item = {'event': str(event)[:64], 'at': time.time()}
        for key in ('error_type', 'exit_code', 'retry_delay'):
            value = metadata.get(key)
            if isinstance(value, (str, int, float)):
                item[key] = value[:80] if isinstance(value, str) else value
        raw = json.dumps(item).encode('utf-8')
        fd, temporary = tempfile.mkstemp(prefix='companion-runtime-', dir=target.parent)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw[:4096])
        os.replace(temporary, target)
    except OSError:
        pass
    finally:
        if temporary:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def _background_interpreter():
    interpreter = Path(sys.executable)
    windowless = interpreter.with_name('pythonw.exe')
    return windowless if sys.platform == 'win32' and windowless.is_file() else interpreter


def _launch_command(options, *, supervise=False, coordinator=False, ensure=False):
    """Preserve root/host/pinned-view selection through every launch boundary."""
    interpreter = _background_interpreter()
    available = False
    try:
        available = subprocess.run([str(interpreter), '-I', '-c', 'import eutonos.desktop_companion_runtime'],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=10, creationflags=HIDDEN).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        pass
    if available:
        command = [str(interpreter), '-I', '-m', 'eutonos.desktop_companion_runtime']
    else:
        wrapper = Path(__file__).resolve().parents[2] / 'tools' / 'desktop_companion.py'
        if not wrapper.is_file():
            raise FileNotFoundError('Installed Companion or source wrapper unavailable')
        command = [str(interpreter), '-I', str(wrapper)]
    if options.root:
        command += ['--root', str(options.root)]
    if options.host:
        command += ['--host', str(options.host)]
    if options.standalone:
        command += ['--standalone']
    if supervise:
        command += ['--supervise']
    if coordinator:
        command += ['--coordinator']
    if ensure:
        command += ['--ensure-current']
    return command


def _task_query():
    try:
        result = subprocess.run(['schtasks.exe', '/Query', '/TN', TASK_NAME, '/XML'],
                                capture_output=True, timeout=10, creationflags=HIDDEN)
    except subprocess.TimeoutExpired as exc:
        raise OSError('Companion scheduled task query timed out') from exc
    if result.returncode:
        message = (result.stdout + result.stderr).decode('utf-8', errors='replace').casefold()
        if result.returncode == 1 and 'the system cannot find the file specified' in message:
            return None
        raise OSError(f'Companion scheduled task query failed ({result.returncode})')
    try:
        raw = result.stdout
        # schtasks can write console-encoded bytes while declaring UTF-16.
        # Decode that single-byte output before the XML declaration is parsed.
        if raw.startswith(b'<?xml') and b'UTF-16' in raw.split(b'?>', 1)[0].upper():
            try:
                decoded = raw.decode('utf-8')
            except UnicodeDecodeError:
                decoded = raw.decode(f'cp{_task_query_code_page()}')
            return ET.fromstring(decoded)
        return ET.fromstring(raw)
    except (ET.ParseError, UnicodeError) as exc:
        raise OSError('Companion scheduled task has invalid XML') from exc


def _task_query_code_page():
    kernel = ctypes.windll.kernel32
    return kernel.GetConsoleOutputCP() or kernel.GetOEMCP()


def _task_owned(task):
    if task is None:
        return False
    namespace = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
    description = task.findtext('t:RegistrationInfo/t:Description', namespaces=namespace)
    arguments = task.findtext('t:Actions/t:Exec/t:Arguments', namespaces=namespace) or ''
    return description == TASK_DESCRIPTION and 'eutonos.desktop_companion_runtime' in arguments and '--ensure-current' in arguments


def _current_user_sid():
    result = subprocess.run(['whoami.exe', '/user', '/fo', 'csv', '/nh'],
                            capture_output=True, text=True, timeout=10, creationflags=HIDDEN)
    if result.returncode:
        raise OSError('Cannot determine current user for Companion scheduled task')
    rows = list(csv.reader(io.StringIO(result.stdout)))
    if len(rows) != 1 or len(rows[0]) != 2 or not rows[0][1].startswith('S-1-'):
        raise OSError('Current user SID was not available')
    return rows[0][1]


def _task_xml(command, sid, *, now=None):
    """An interactive, least-privilege logon task with an hourly finite ensure."""
    namespace = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
    ET.register_namespace('', namespace)
    def add(parent, name, value=None, **attributes):
        node = ET.SubElement(parent, f'{{{namespace}}}{name}', attributes)
        if value is not None:
            node.text = value
        return node
    task = ET.Element(f'{{{namespace}}}Task', {'version': '1.4'})
    add(add(task, 'RegistrationInfo'), 'Description', TASK_DESCRIPTION)
    trigger = add(add(task, 'Triggers'), 'LogonTrigger')
    add(trigger, 'Enabled', 'true')
    add(trigger, 'UserId', sid)
    # The time trigger is armed in the present logon session, without a
    # sign-out. A separate logon trigger restores the resident immediately.
    timer = add(task.find(f'{{{namespace}}}Triggers'), 'TimeTrigger')
    add(add(timer, 'Repetition'), 'Interval', 'PT1H')
    add(timer, 'StartBoundary', ((now or datetime.now()) + timedelta(minutes=1)).replace(microsecond=0).isoformat())
    add(timer, 'Enabled', 'true')
    principal = add(add(task, 'Principals'), 'Principal', id='Author')
    add(principal, 'UserId', sid)
    add(principal, 'LogonType', 'InteractiveToken')
    add(principal, 'RunLevel', 'LeastPrivilege')
    settings = add(task, 'Settings')
    for name, value in (('MultipleInstancesPolicy', 'IgnoreNew'), ('DisallowStartIfOnBatteries', 'false'),
                        ('StopIfGoingOnBatteries', 'false'), ('StartWhenAvailable', 'true'),
                        ('WakeToRun', 'false'), ('ExecutionTimeLimit', 'PT1M'), ('Enabled', 'true'),
                        ('Hidden', 'true')):
        add(settings, name, value)
    action = add(add(task, 'Actions', Context='Author'), 'Exec')
    add(action, 'Command', command[0])
    add(action, 'Arguments', subprocess.list2cmdline(command[1:]))
    # schtasks /Create /XML on this Windows host requires the task file's
    # declaration and bytes to agree on UTF-16, including its BOM.
    return ET.tostring(task, encoding='utf-16', xml_declaration=True)


def _remove_legacy_run_values():
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY) as key:
        for name in (*LEGACY_STARTUP_NAMES, STARTUP_NAME):
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass


def startup_enabled():
    if sys.platform != 'win32':
        return False
    try:
        task = _task_query()
    except OSError:
        task = None
    if task is not None:
        if not _task_owned(task):
            return False
        namespace = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        return task.findtext('t:Settings/t:Enabled', default='true', namespaces=namespace) != 'false'
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY) as key:
            winreg.QueryValueEx(key, STARTUP_NAME)
        return True
    except FileNotFoundError:
        return False


TASK_NAMESPACE = 'http://schemas.microsoft.com/windows/2004/02/mit/task'


def export_task():
    """The registered task's full definition as XML text (what ``restore_task`` takes), or None when there is none.
    Read only. The component keeps it before it replaces a task of the same name, so uninstall can put it back."""
    if sys.platform != 'win32':
        raise OSError('Windows startup unavailable')
    task = _task_query()
    if task is None:
        return None
    ET.register_namespace('', TASK_NAMESPACE)
    return ET.tostring(task, encoding='utf-16', xml_declaration=True).decode('utf-16')


def _create_task(xml, previous):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='eutonos-companion-task-', suffix='.xml', delete=False) as stream:
            stream.write(xml)
            temporary = Path(stream.name)
        args = ['schtasks.exe', '/Create', '/TN', TASK_NAME, '/XML', str(temporary)]
        if previous is not None:
            args.append('/F')
        result = subprocess.run(args, capture_output=True, timeout=20, creationflags=HIDDEN)
        if result.returncode:
            detail = ' '.join((result.stderr or result.stdout).decode('utf-8', errors='replace').split())[:240]
            raise OSError(f'Companion scheduled task registration failed ({result.returncode}): {detail}')
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def restore_task(xml):
    """Put a task definition from ``export_task`` back under the companion's name, replacing the one EUTONOS
    registered. Another task of that name that is not EUTONOS's is never touched."""
    if sys.platform != 'win32':
        raise OSError('Windows startup unavailable')
    previous = _task_query()
    if previous is not None and not _task_owned(previous):
        raise OSError('Companion task name belongs to another task')
    _create_task(xml.encode('utf-16') if isinstance(xml, str) else xml, previous)


def register_task(command, *, sid=None, now=None):
    """Register (or replace) the one owned interactive task that runs ``command`` at logon and hourly.

    ``command`` is the full argv, ``[program, *arguments]``. Raises ``OSError`` for another task of the same name or a
    failed registration. Returns ``{"existed": bool, "xml": bytes}``. The EUTONOS component calls this with the
    installed runtime's own interpreter (IPC5 B); ``set_startup`` calls it with the current interpreter."""
    if sys.platform != 'win32':
        raise OSError('Windows startup unavailable')
    previous = _task_query()
    if previous is not None and not _task_owned(previous):
        raise OSError('Companion task name belongs to another task')
    xml = _task_xml(list(command), sid or _current_user_sid(), now=now)
    _create_task(xml, previous)
    return {'existed': previous is not None, 'xml': xml}


def delete_task():
    """Delete the one owned task. Another task of the same name is never touched. True when a task was deleted."""
    if sys.platform != 'win32':
        raise OSError('Windows startup unavailable')
    previous = _task_query()
    if previous is None:
        return False
    if not _task_owned(previous):
        raise OSError('Companion task name belongs to another task')
    result = subprocess.run(['schtasks.exe', '/Delete', '/TN', TASK_NAME, '/F'],
                            capture_output=True, timeout=20, creationflags=HIDDEN)
    if result.returncode:
        raise OSError(f'Companion scheduled task removal failed ({result.returncode})')
    return True


def task_spec(task):
    """``(command, arguments)`` of a queried task's one Exec action, or None. What the component's effect hashes."""
    if task is None:
        return None
    namespace = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
    command = task.findtext('t:Actions/t:Exec/t:Command', namespaces=namespace)
    arguments = task.findtext('t:Actions/t:Exec/t:Arguments', namespaces=namespace) or ''
    return None if command is None else (command, arguments)


def set_startup(enabled, options):
    """Register one owned interactive task and retire the old Run identity."""
    if sys.platform != 'win32':
        raise OSError('Windows startup unavailable')
    if enabled:
        # Install the replacement before retiring the working Run registration.
        register_task(_launch_command(options, ensure=True))
    else:
        delete_task()
    _remove_legacy_run_values()


def _pause_marker():
    return eutonos_home.home() / 'desktop-companion.paused'


def paused():
    """Owner pause is shared by interactive and scheduled task contexts."""
    try:
        _pause_marker().stat()
        return True
    except FileNotFoundError:
        return False


def set_paused(value):
    marker = _pause_marker()
    if value:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch(exist_ok=True)
    else:
        marker.unlink(missing_ok=True)


def _claim(name):
    from .desktop_companion_windows import KERNEL32
    handle = KERNEL32.CreateMutexW(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        KERNEL32.CloseHandle(handle)
        return None
    return handle


def _mutex_exists(name):
    # Presence checks must never create the mutex: that could make the real
    # supervisor's CreateMutexW conclude another supervisor already owns it.
    from .desktop_companion_windows import KERNEL32
    open_mutex = KERNEL32.OpenMutexW
    open_mutex.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p)
    open_mutex.restype = ctypes.c_void_p
    handle = open_mutex(0x00100000, False, name)  # SYNCHRONIZE
    if handle:
        KERNEL32.CloseHandle(handle)
        return True
    error = ctypes.get_last_error()
    if error == 2:  # ERROR_FILE_NOT_FOUND
        return False
    raise ctypes.WinError(error)


def _release(handle):
    from .desktop_companion_windows import KERNEL32
    if handle:
        KERNEL32.CloseHandle(handle)


def _existing_window():
    from .desktop_companion_windows import USER32
    return bool(USER32.FindWindowW(None, COORDINATOR_TITLE))


def _set_dpi_awareness():
    """Restore physical per-monitor geometry before Tk creates any HWND."""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        ctypes.windll.user32.SetProcessDPIAware()


def _supervise(options):
    """Restart unexpected exits forever, with a bounded 1..30 second delay.

    An orphan coordinator retains its mutex after a supervisor death. Wait for
    that exact resident instead of opening a duplicate or abandoning recovery.
    """
    guard = _claim(SUPERVISOR_MUTEX)
    if not guard:
        return 0
    delay = 1
    process = None
    try:
        command = _launch_command(options, coordinator=True)
        while True:
            if paused():
                return 0
            if _mutex_exists(COORDINATOR_MUTEX):
                runtime_diagnostic(options.root, 'orphan-coordinator-observed')
                while _mutex_exists(COORDINATOR_MUTEX):
                    if paused():
                        return 0
                    time.sleep(1)
                delay = 1
                continue
            started = time.monotonic()
            try:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, creationflags=HIDDEN)
                code = process.wait()
            except OSError:
                code = 1
            if paused():
                return 0
            if code == DUPLICATE_COORDINATOR:
                # The coordinator won a race with another resident. Observe
                # its mutex until it leaves, then create our own child.
                continue
            if time.monotonic() - started >= 60:
                delay = 1
            runtime_diagnostic(options.root, 'coordinator-restarting', exit_code=code, retry_delay=delay)
            time.sleep(delay)
            delay = min(30, delay * 2)
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
        _release(guard)


def start_current_session(options):
    set_paused(False)
    status = ensure_current_session(options)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if _existing_window():
            return status
        if not _mutex_exists(SUPERVISOR_MUTEX):
            raise OSError('Companion supervisor exited before its resident was ready')
        time.sleep(.1)
    raise TimeoutError('Companion resident startup was not observed')


def ensure_current_session(options):
    """Finite Task Scheduler action; the spawned supervisor owns the lifetime."""
    if paused():
        return 'paused'
    if _mutex_exists(SUPERVISOR_MUTEX):
        return 'already-supervised'
    process = subprocess.Popen(_launch_command(options, supervise=True), stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=HIDDEN)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _mutex_exists(SUPERVISOR_MUTEX):
            return 'started'
        if process.poll() is not None:
            raise OSError('Companion supervisor exited before claiming its guard')
        time.sleep(.1)
    raise TimeoutError('Companion supervisor startup was not observed')


RESTART_WAIT_SECONDS = 120
DETACHED = 0x00000008  # DETACHED_PROCESS: the helper outlives the companion that starts it


def restart_command(python, options):
    """The argv for the post-update helper, run by the NEW runtime's interpreter with the same root/host selection."""
    python = Path(python)
    windowless = python.with_name('pythonw.exe')
    command = [str(windowless if windowless.is_file() else python), '-I', '-m', 'eutonos.desktop_companion_runtime',
               '--restart-after-update']
    if options.root:
        command += ['--root', str(options.root)]
    if options.host:
        command += ['--host', str(options.host)]
    if options.standalone:
        command += ['--standalone']
    return command


def spawn_restart(python, options):
    """Start the post-update helper detached. True when it started; the caller leaves only then."""
    try:
        subprocess.Popen(restart_command(python, options), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=HIDDEN | DETACHED | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
        return True
    except OSError:
        return False


def restart_after_update(options, *, wait=RESTART_WAIT_SECONDS, sleep=time.sleep, clock=time.monotonic):
    """Run by the new runtime after an update: wait for the old supervisor and coordinator to leave, clear the pause the
    old companion set when it left, and start the companion again with the ordinary startup (the same one the logon
    task uses), so it now runs from the new generation."""
    deadline = clock() + wait
    try:
        while _mutex_exists(SUPERVISOR_MUTEX) or _mutex_exists(COORDINATOR_MUTEX):
            if clock() >= deadline:
                runtime_diagnostic(options.root, 'restart-wait-timeout')
                break
            sleep(.5)
    finally:
        set_paused(False)
    return ensure_current_session(options)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--host', type=Path)
    parser.add_argument('--standalone', action='store_true', help='Also show a pinned independent view for Claude Code terminals')
    parser.add_argument('--snapshot', action='store_true', help='Print bounded read-only metadata without opening a window')
    parser.add_argument('--install-startup', action='store_true')
    parser.add_argument('--remove-startup', action='store_true')
    parser.add_argument('--start-current', action='store_true')
    parser.add_argument('--ensure-current', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--supervise', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--coordinator', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--restart-after-update', action='store_true', help=argparse.SUPPRESS)
    return parser


def main(argv=None):
    parser = build_parser()
    options = parser.parse_args(argv)
    if options.install_startup and options.remove_startup:
        parser.error('Choose one startup action')
    if options.start_current and options.ensure_current:
        parser.error('Choose one current-session action')
    if options.restart_after_update and any((options.snapshot, options.install_startup, options.remove_startup,
                                             options.start_current, options.ensure_current, options.supervise,
                                             options.coordinator)):
        parser.error('The post-update restart cannot be combined with other actions')
    if options.snapshot and any((options.install_startup, options.remove_startup, options.start_current, options.ensure_current, options.supervise, options.coordinator)):
        parser.error('Snapshot cannot be combined with runtime actions')
    from .desktop_companion import Coordinator, selected_root
    options.root = selected_root(options.root)
    if options.snapshot:
        from .desktop_companion_data import Collector, local_configuration
        if not options.root:
            print(json.dumps({'health': 'unknown', 'diagnostics': [{'code': 'project-unknown'}]}))
        else:
            cfg = local_configuration(options.root)
            print(json.dumps(Collector(options.root, options.host or cfg.get('host') or eutonos_home.hosts() / 'eutonos').collect()))
        return 0
    if sys.platform != 'win32':
        parser.error('Companion windows require Windows')
    if options.restart_after_update:
        print(restart_after_update(options))
        return 0
    if options.install_startup or options.remove_startup:
        set_startup(options.install_startup, options)
    if options.start_current:
        print(start_current_session(options))
        return 0
    if options.ensure_current:
        print(ensure_current_session(options))
        return 0
    if options.install_startup or options.remove_startup:
        return 0
    if not options.coordinator:
        if not options.supervise:
            set_paused(False)
        return _supervise(options)
    if paused():
        return 0
    guard = _claim(COORDINATOR_MUTEX)
    if not guard:
        return DUPLICATE_COORDINATOR
    try:
        _set_dpi_awareness()
        return Coordinator(options).run()
    finally:
        _release(guard)


if __name__ == '__main__':
    raise SystemExit(main())
