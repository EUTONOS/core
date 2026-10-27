# EUTONOS Setup for Windows

`EUTONOS.iss` is the Inno Setup 6 script for `EUTONOS-Setup-<version>.exe`, the normal Windows wizard. It installs for one user, needs no administrator rights, and hands all the real work to the EUTONOS engine.

## Build

```
python tools/build_setup.py [--python "C:\Program Files\Python314"] [--iscc PATH]
```

It builds the wheel (`tools/build_wheel.py`), checks and copies a python.org Python, stages `bootstrap.py`, runs `ISCC.exe` (Inno Setup 6.7 or newer) and writes `dist/EUTONOS-Setup-<version>.exe`. It prints the sha256 of the wheel and of Setup.exe. Nothing is downloaded. `dist/` and `build/` are not committed.

The Python must be 64-bit python.org 3.12 or newer with tkinter. A venv, the embeddable zip or a Store Python is refused with the reason. The copy leaves out test suites, `site-packages` contents, Doc, include, libs, Scripts, Tools, idlelib, turtledemo and `__pycache__`. It keeps `ensurepip` and `venv`, because the engine builds the EUTONOS runtime from this Python.

Before anything is staged it checks that `src/eutonos/offers/offers.json` (EUTONOS's own feature and capability lists, which the wheel carries for the dashboard's "What EUTONOS offers" page) still matches the catalogue documents it is generated from. If the documents changed, the build stops and names the fix: run `python tools/build_offers.py`, then commit the regenerated file.

`--work DIR` puts the staging folder in `DIR\stage` instead of `build\setup\stage` (for example a temporary folder outside the repository), and `--output DIR` puts Setup.exe elsewhere than `dist\`.

### Same source, same bytes

Building twice gives the same Setup.exe, byte for byte, whatever the time or the time stamps on the files: the wheel's zip entries all carry one fixed time (1980-01-01, or `SOURCE_DATE_EPOCH` when set), a fixed permission and sorted order, and the script marks every file `notimestamp`, so Setup stores no file times. What still has to be the same is outside this repository: the same Python install, the same Inno Setup version and the same files (the repository checks out with LF line endings). Tests cover the wheel (`tests/test_build_wheel.py`) and, where Inno Setup is installed, compile the same small stage twice with different file times (`tests/test_setup_app.py`).

## What the wizard does

1. A splash, then a welcome page that says in plain words what EUTONOS is.
2. The install location, with the recommended folder `%USERPROFILE%\.eutonos` and a Browse button. For an active or unfinished install, Setup offers an explicit support-file repair: it restores only its bundled Python, bootstrap and wheel after a lifecycle busy check. Close EUTONOS apps first; in-use files or a busy lifecycle stop repair. It skips parts, consent and engine replay, then shows bundled-bootstrap status/resume/uninstall commands, including when no launcher exists yet.
3. The parts: core (always), Crosstalk, Hermes, Remote Control, the companion. Setup installs every part this PC can run; a box reads, for example, "Crosstalk (installed; tick to switch on now)", and the tick only says whether it starts switched on. A part whose app is not on this PC is skipped and says why. If you already have a Remote Control launcher in your Startup folder, Remote Control starts off, with the note that you already have one at login. Setup never changes your launcher.
4. A "Who uses this Windows login?" page with **Only I use this Windows login** (unticked by default, with a note to leave it unticked on a shared login). When ticked, a page after the install lists the Claude emails Crosstalk has seen, for you to keep or fix; only the emails you confirm are saved (`wizard apply-settings`), and a failure is shown and left as a pending step, never a failed install.
5. A "Keeping EUTONOS up to date" page with a dropdown: Manual (nothing runs on its own), Check and notify, or Check, download, then ask before installing (the default). After a good install Setup saves the choice (`wizard apply-settings --update-mode`); a failure is shown on the finish page and never fails the install. EUTONOS never installs an update without a click. The daily check runs inside the companion. The same dropdown is in the dashboard, Features, Updates.
6. The consent page: the plain change list and the exact consent text the engine renders for these choices, with **I agree**. Next stays off until it is ticked.
7. The install. The engine runs from the wheel Setup carries, with its live steps on the progress page.
8. A finish page that tells you to restart Claude Desktop, Codex and Hermes. After a good install it also has an **Open dashboard** button (bottom left). Nothing starts until you click it. The click runs `wizard open-dashboard` from the installed files: if nothing answers on port 8765 it starts `<home>\bin\eutonos-dashboard.exe` hidden on your registered projects (a new install has none, so on the EUTONOS folder), waits up to a minute, then opens `http://127.0.0.1:8765` in your browser. A dashboard that is already running is only opened, and a failure is said in a message box. The dashboard keeps running after Setup closes, like the one the companion starts.

If an earlier hand install is found, Setup passes `--adopt` and says so on the change list.

## How the pieces connect

- `bootstrap.py` imports the engine from the wheel (`python -I -B`, never `PYTHONPATH`) and runs `eutonos.setup_wizard` (look at this PC, render the consent text, stamp the acceptance) or `eutonos.installer` (the engine).
- The engine's consent route is `install --consent-file F`. The file carries the sha256 of the text shown, the choices, the surface `setup-wizard` and the time you agreed. The engine renders the text again for the same folder and choices and refuses on any mismatch.
- `--for-this-user` marks the install as the real one (your own Claude, Codex and Hermes settings and PATH). It is recorded in the install record and every later command honours it. A non-default folder also gets the user variable `EUTONOS_HOME`, which uninstall removes unless you changed it.
- The bundled Python is installed at `<home>\python` and stays until uninstall. Setup's own files are in `<home>\setup`.

## Uninstall

Windows Settings, Installed apps, EUTONOS runs the engine's uninstall first (the installed engine, with the bundled Python), then Inno removes its own files. If the engine reports a problem, uninstall stops with the message, and nothing is deleted: not the bundled Python, not the install record. Backups, hosts, RAM and project data stay, as the engine says.

## Recovery without a launcher

After support-file repair, use PowerShell with the wheel filename shown on Setup's finish page:

```powershell
& "H\python\python.exe" -I -B "H\setup\bootstrap.py" "H\setup\eutonos-VERSION-py3-none-any.whl" engine --home "H" status
```

Inspect the pending generation and journal first. Explicitly replace `status` with `resume` to finish the recorded run, or `uninstall --keep-projects` to undo it. These commands use the bundled engine, require no existing `bin` launcher, and do not accept new consent. Incomplete removals keep retry state and Setup support files; fix the reported problem and retry uninstall without replaying successful removals.

## Not signed

Setup.exe is not code-signed, so Windows SmartScreen shows "Windows protected your PC". Choose **More info**, then **Run anyway**. A signing certificate costs money every year; buying one is the owner's spending decision.

## Limits

- Setup refuses `/SILENT`: the consent has to be read and accepted on screen.
- Setup.exe has been compiled but its wizard pages can only be checked by running it, which is the owner's IPC6 trial.
