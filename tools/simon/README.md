# agent-helper-simon-poc

Installed with EUTONOS (the optional Simon part in Setup), run `eutonos-simon ...` from any folder instead of `python helperctl.py ...` below. In a EUTONOS source checkout Simon lives in `tools/simon/`, and its project documentation (goal, current state, features, architecture, roadmap, runbooks, checks) is in [docs/Simon/README.md](../../docs/Simon/README.md); that documentation does not ship in the installed kit. This file is the user manual and holds the generated command reference.

Standalone proof of concept for testing whether a local helper app can make AI-driven desktop work faster and safer by acting as the operator while Codex/ChatGPT acts as the planner.

The project goal is to move fragile GUI execution out of the model. The helper should observe the active app, expose grounded action candidates, manage focus/window safety, execute one verified action at a time, return the next state, and fall back to screenshots/grids only when backend or candidate-based targeting is not enough.

Start with the planner quickstart: `QUICKSTART.md` beside this file in the installed kit (in a EUTONOS source checkout: [docs/Simon/QUICKSTART.md](../../docs/Simon/QUICKSTART.md)). Commands default to `detail=minimal`, keep the v1 `status` and add `outcome`, return one planner image when an observation is available, and point to full evidence through `details_path`. Use `--detail full` for diagnostics. The project Claude Code `/simon-planner` skill and idle-only `SessionEnd` hook use the same guide and lifecycle.

## Files

- `simon_target.py` - Simon Says target desktop app titled `Simon Target`.
- `agent_helper.py` - helper/control desktop app titled `Simon. Screen Interface Mapping Operations Navigator`.
- `helperctl.py` - structured command interface for Codex/ChatGPT to ask the helper for observation, input, recovery screenshots, and Simon-specific proof-of-concept actions.
- `simon_common.py` - shared colors, fixed sequence, JSON/log helpers, and compatibility path exports.
- `simon_paths.py` - canonical runtime paths, with no filesystem writes on import.
- `simon_protocol.py` - CLI parsing, result envelopes, JSON emission, and request identity.
- `simon_schema.py` - one command/argument catalog for CLI, validation, discovery and this generated command reference.
- `simon_images.py` - one bounded planner image, with optional extra evidence images.
- `simon_artifacts.py` - runtime reports, planner persistence, bridge journals, and capture tallies.
- `requirements.txt` - local Python dependencies.
- `run_target.bat` - launches the Simon target app.
- `run_helper.bat` - launches the helper app.
- Project documentation lives outside this folder, in `docs/Simon/` at the EUTONOS repository root of a source checkout (goal, current state, features, architecture, roadmap, runbooks, checks); the installed kit carries only the quickstart.
- `.gitignore` - local/runtime, generated, sensitive, backup, and unrelated-payload exclusions for the audited Git boundary.
- `.runtime/` - created at runtime for telemetry and logs.
- `.runtime/failures/` - created when the helper writes freeze/error diagnostics.

## Install

Use Python 3.10 or newer on Windows.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If you do not use a virtual environment, install the requirements into the Python environment that runs the apps.

## Test

Run the deterministic command/control suite from the repository root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
```

These tests use temporary files, synthetic images, and mocked Windows boundaries. They do not capture, focus, click, or type into the live desktop; use the manual runbooks under `docs/Simon/tasks/` at the repository root when a change affects those behaviors.

## Run

Open two terminals from this folder.

Terminal 1:

```powershell
python simon_target.py
```

Terminal 2:

```powershell
python agent_helper.py
```

You can also double-click `run_target.bat` and `run_helper.bat` if `python` is on your PATH.

## Architecture

The target app is intentionally plain: six large colored buttons, visible sequence/progress/status labels, reset controls, keyboard/paste controls, a dialog challenge, and a fixed 20-step sequence:

```text
red, green, blue, green, yellow, purple, orange, red, blue, yellow, green, purple, red, orange, blue, green, yellow, red, purple, orange
```

The helper app is the control layer. It:

- Finds the real `Simon Target` window by title using the Windows API.
- Screenshots only the target window region.
- Detects the six button regions by deterministic color matching.
- Stores each detected button center in screen coordinates.
- Reads target telemetry from `.runtime/simon_state.json` to know the expected color and verify progress.
- Clicks the detected screen coordinate with `pyautogui`.
- Freezes action controls unless the state is `READY`.
- Reacquires the target window and rescans coordinates before every click action.
- Verifies that `Simon Target` has foreground focus immediately before acting.
- Retries pre-action scan/focus checks before freezing.
- Can paste text into the target text field with real keyboard/clipboard actions.
- Can toggle the target switch with the target app's `Ctrl+Shift+S` shortcut.
- Can open and complete the target dialog challenge by entering `10`, selecting `Yes`, and clicking `OK`.
- Can run a mixed 20-action gauntlet: 18 Simon color clicks, one paste action, and one dialog challenge.
- Can accept structured `helperctl.py --via-app` requests through a local file bridge and return JSON results.
- Can capture a labeled grid screenshot for uncertain states so Codex/ChatGPT can inspect the screen and choose a guided recovery action.
- Starts with its debug window hidden so the helper does not keep stealing foreground during planner-mode work.
- Keeps an always-on-top top-screen control banner visible with `ESC TO STOP`.
- Polls the Escape key globally so a human can hard-stop helper control even when another app has focus.
- Lets a human restore the hidden debug panel by double-clicking the top banner.

The telemetry file is a deliberate POC shortcut in place of OCR. The helper still uses screenshot/window detection for locating and clicking the actual buttons, but verification is kept deterministic so the experiment focuses on the helper-control architecture instead of text recognition.

The keyboard/paste and dialog tests work similarly: the helper brings the target app or dialog forward, sends real keyboard/mouse actions, and verifies the result through `.runtime/simon_state.json`.

When the helper freezes, it writes a diagnostic JSON report and, when possible, a screenshot to `.runtime/failures/`. The hidden debug window shows the latest failure report filename and has a `Copy Failure Report` button for pasting the full report into a debugging chat.

## Command Interface

`helperctl.py` is the first command-driven helper surface. It lets Codex or another planner ask for structured observations and actions without clicking the Helper UI.

List available commands:

```powershell
python helperctl.py helper.commands
```

`helper.commands` is the authoritative list; it also returns the supported `target_modes`. If this README and that output disagree, trust the command. The commands in common use, by group:

- Lifecycle and bridge: `helper.commands`, `helper.bridge_status`, `helper.start`, `helper.start_elevated`, `helper.exit`.
- Environment: `diagnose.environment`, `detect.vm_boundary`, `recovery.stop_screensaver`.
- Observation: `status.current`, `observe.windows`, `observe.window`, `observe.screenshot`, `observe.ocr`, `planner.fast_observe`.
- Planner loop: `planner.step`.
- Verification: `verify.window_state`, `verify.file_state`.
- Focus and launch: `focus.window`, `desktop.start_search_grid`.
- Candidate input (preferred over coordinates): `input.click_candidate`, `input.invoke_candidate`, `input.set_candidate_value`, `input.expand_candidate`, `input.select_candidate`.
- Direct input: `input.click_point`, `input.type_text`, `input.paste_text`, `input.hotkey`, `input.action_sequence`.
- Grid recovery: `recovery.capture_grid`, `recovery.guided_grid_crop`, `recovery.guided_grid_click`, `recovery.transient_grid_after_click`.
- Simon target POC: `simon.detect_state`, `simon.press_next`, `simon.force_grid_request`.

### CLI Arguments And Defaults

CLI flags you don't pass are not forwarded; each command applies its own defaults. You no longer need `--target desktop` or `"title":""` to avoid the old "Simon Target" filter. Commands that need a window and receive no `title`, `exact_title` or `title_contains` still fall back to the `Simon Target` window, so pass a title or a `--target` mode when you mean a different app.

- `--via-app` sends the command to the running Helper over the file bridge; `--timeout` (default 10 seconds) is how long `helperctl.py` waits for that result. Without `--via-app` the command runs in the calling process.
- `--json` and `--json-file` supply a JSON object that is merged over the CLI flags, so a key in the JSON wins over the same flag. If both are given, `--json-file` is used and `--json` is ignored.
- In PowerShell prefer `--json-file` over inline `--json`. Inline JSON needs `\"` escaping that is easy to get wrong and hard to read in logs. Write the payload to a file (for example under the ignored `.runtime\` folder) and pass its path.
- Results print as compact single-line JSON; add `--pretty` for indented output.

### Common Commands

Ask the running Simon app for current state:

```powershell
python helperctl.py status.current --via-app --timeout 5
```

Capture a grid screenshot through the Simon app when the next action is uncertain. The grid defaults to 12 columns by 8 rows, and the target defaults to the `Simon Target` window:

```powershell
python helperctl.py recovery.capture_grid --via-app --columns 12 --rows 8 --reason "planner needs visual recovery" --timeout 10
```

For another app, pass its exact window title (`--title "Untitled - Notepad"`) or capture what is in front (`--target foreground_window`, `--target current_dialog` or `--target desktop`).

Use `diagnose.environment` before live desktop trials. It reports whether the command process is on the real interactive desktop or an isolated sandbox desktop, whether visible windows can be enumerated, and whether screenshot capture works.

```powershell
python helperctl.py diagnose.environment
```

Use `helper.start` only as a guarded launch path. It refuses to start Simon from the Codex sandbox desktop by default, because a sandbox-launched helper would not see the real desktop apps.

```powershell
python helperctl.py helper.start
```

For backend-first verification before the next action, use `verify.window_state`. It checks open windows and foreground state through the Windows backend first, optionally tries to bring the expected app to foreground, and only captures a desktop planner screenshot if that faster path cannot verify or restore the state.

Save the payload as `.runtime\verify_word.json`:

```json
{
  "title_contains": "Word",
  "require_foreground": true,
  "focus_if_needed": true,
  "screenshot_on_failure": true
}
```

```powershell
python helperctl.py verify.window_state --json-file .runtime\verify_word.json
```

The verification is generic, not Word-specific. The model can also describe a more specific "ready" state by excluding known startup titles and requiring a minimum window size:

```json
{
  "type": "window_state",
  "title_contains": "Word",
  "title_excludes": ["Opening - Word"],
  "min_width": 900,
  "min_height": 600,
  "require_foreground": true,
  "focus_if_needed": true
}
```

Test forced Simon grid recovery:

```powershell
python helperctl.py simon.force_grid_request --via-app --columns 12 --rows 8 --reason "forced grid recovery test" --timeout 10
python helperctl.py recovery.guided_grid_click --via-app --request-id "<request id>" --cell C6 --target-description "Red Simon button" --expected-result "Simon progress increments after clicking red." --timeout 10
```

The guided click command loads the saved planner request, rejects stale screenshots, rejects moved target-window bounds, captures a fresh pre-action screenshot, compares it to the planner screenshot, executes exactly one click only if the screen still matches, captures after the click, and then verifies the result when the planner request includes an automatic verification rule. If the screen changed before action, it returns `NEEDS_PLANNER` with `failure_type: STALE_SCREENSHOT` and a fresh planner request instead of clicking. Planner requests older than `--max-age-seconds` (default 60) are rejected as stale.

### Where In The Cell To Click

`recovery.guided_grid_click` and `recovery.transient_grid_after_click` click the centre of the chosen cell by default. To aim elsewhere inside the cell, use one of:

- `--offset <preset>`: `center` (default), `left-center`, `right-center`, `top-center`, `bottom-center`, `top-left`, `top-right`, `bottom-left` or `bottom-right`. The presets sit at 25%, 50% or 75% of the cell width and height; for example, `top-left` is 25% across and 25% down. Spell the preset exactly: an unrecognised name currently falls back to `center` without an error.
- `--x-pct` and `--y-pct` together: fractions from `0.0` to `1.0` of the cell width and height, measured from the cell's top-left corner and clamped to that range. Passing only one of them is an `ERROR`. When both are given they replace `--offset`.

The `recovery.guided_grid_click` result echoes the `offset`, the `offset_pct` actually used and the resolved `screen_coordinate`.

### Fine-Grid Crops

For precision recovery, use `recovery.guided_grid_crop` on one cell of a saved planner request. It cuts that cell out of the parent's saved raw screenshot (no new capture, no input) and returns a new child request ID. Its 1024 px content edge, adaptive cells, margin rulers and thin translucent lines make small controls readable. Repeated crops compose their native image-to-screen transforms and retain the original capture timestamp; `created_at` records crop creation separately. Guided clicks require verified source geometry, reject synthetic/ambiguous images, and check window identity/bounds again before the press. Native pixels remain authoritative; resized images support grid-label targeting.

For a small or nested target, set `preview: true` on the guided/transient click (or its `planner.step`). Inspect the returned crosshair and cell bounds, then repeat the same action with `confirm_token` and preview omitted/false within 30 seconds. Preview emits no input; a token binds the action and coordinates, is consumed before input, and cannot be replayed. Confirmation rechecks freshness and movement. An expired or mismatched token returns `UNCERTAIN`; take a fresh observation rather than reusing it.

```powershell
python helperctl.py recovery.guided_grid_crop --request-id "<parent request id>" --cell D5 --crop-columns 4 --crop-rows 4 --reason "zoom into the Save button area"
python helperctl.py recovery.guided_grid_click --via-app --request-id "<parent request id>_D5_crop" --cell B2 --expected-result "Save dialog closes." --timeout 10
```

- Arguments: `--request-id` and `--cell` are required. `--crop-columns` and `--crop-rows` set the positive fine-grid dimensions; if omitted, the fine grid is 12x8. `--reason` is stored in the child request. `--max-age-seconds` (default 60) rejects a stale parent, and a parent whose target window has moved is rejected with `failure_type: WINDOW_MOVED`.
- Child id: the child planner request id is `<parent request id>_<CELL>_crop`, for example `REQ-123_D5_crop`. It is returned as `data.request_id`; pass it to `recovery.guided_grid_click`.
- Output images: the default `data.planner_image.path` is the labeled crop, with a maximum long edge of 1568 pixels. Request `include_images: ["context"]` for padded context views or `detail: full` for all native image paths and detailed evidence. Native crop/grid images remain available internally for freshness; padded context images are created only when requested. Resized image pixels are not screen coordinates; target grid labels.
- Native pixels: the crop is not upscaled. A 12x8 capture of a 1200x800 window has 100x100-pixel cells, so a 12x8 fine grid on one of them gives cells about 8 by 12 pixels, which is too small to read or aim at reliably. On small crops use a smaller fine grid such as `--crop-columns 4 --crop-rows 4`.
- One crop level only: crop a fresh capture once, then click. Do not crop a crop (the router would default that to a 20x16 grid) until `SIMON-006` is fixed: nested crop offsets are currently mapped incorrectly, so a click from a crop-of-a-crop can land in the wrong place. (Update as of 2026-10-24: C2 fixed SIMON-006 on 2026-10-14; nested crops now compose and keep the source capture time, see the quickstart. This sentence is older text kept as written.) If one crop is not precise enough, capture again with a better target or grid instead.

### Reading `NEEDS_PLANNER`

`NEEDS_PLANNER` is not always a failure. It is also the normal status of a successful capture or crop (`recovery.capture_grid`, `recovery.guided_grid_crop`, `simon.force_grid_request`, `desktop.start_search_grid`): Simon has saved fresh images and a planner request, and is handing the next decision back to the planner. Always read these fields together before acting:

- `command`: the command that produced the result.
- `outcome`: `observation_ready` for a useful observation, `action_done` for a completed command, `blocked` for failure/unavailable/stale state, or `needs_decision` for unresolved verification. Existing success statuses do not independently prove an app effect.
- `message`: for example `Grid screenshot captured.` or `Fine-grid crop created.` on success, or the reason Simon declined to act.
- `data.failure_type`: absent on a plain successful capture or crop; present (for example `STALE_SCREENSHOT` or `WINDOW_MOVED`) when Simon refused or could not complete an action. The saved planner request's own `failure_type` is `NONE` for a plain observation.
- `next_allowed_actions`: the commands Simon expects next.

Every non-success result also carries a `planner_recovery_hint`. On a successful capture it is general advice, not evidence of a problem.

### Planner Loop And Transient UI

For transient UI such as Start/search menus, use `input.action_sequence` with a JSON payload or `--json-file` so the helper owns coupled actions like click, wait, type, hotkey, and screenshot without focus drifting between separate planner commands.

Current planner-mode preference: use backend context and actionable candidates first, click through the UI when a candidate is clear, use the keyboard mainly for true shortcuts and text entry, and favor taking another screenshot/grid over stalling on inferred desktop-app state. Grid mode is a fallback precision tool, not the long-term happy path.

For the faster planner loop, use `planner.step`. It executes one planner action, optionally runs a backend-first `verify_before` gate, then immediately captures the next raw-plus-grid observation and returns a fresh planner request so Codex can choose the next move without stitching separate commands together.

```powershell
python helperctl.py planner.step --json-file .runtime\planner_step_example.json
```

Example payload:

```json
{
  "step": {
    "action": "guided_grid_click",
    "request_id": "REQ-...",
    "cell": "B3",
    "expected_result": "Word best match is opened."
  },
  "verify_before": {
    "title_contains": "Search",
    "require_foreground": true,
    "focus_if_needed": true
  },
  "observe_after": {
    "target": "foreground_window",
    "columns": 12,
    "rows": 8,
    "reason": "Planner needs the next visible state after the click."
  }
}
```

When Codex needs to inspect what appears after one grid-guided click, use `recovery.transient_grid_after_click`. It also checks that the visible screen still matches the planner screenshot before clicking, then clicks one saved planner-grid cell, waits briefly, captures a fresh grid screenshot before the Helper restores focus, and returns a new planner request for the transient UI state.

For Windows Start/Search app-launch recovery, use `desktop.start_search_grid`. It dismisses any existing transient menu with `Esc`, opens Start/Search by keyboard, types the query, captures the active Search window by hwnd when available, and returns a scoped planner request. In live testing, `desktop.start_search_grid --via-app --text word` followed by `recovery.guided_grid_click --via-app --cell B3` opened or restored Microsoft Word from the Search best-match result. In the current direction, planner mode should simply pick one valid open action from the screenshot or grid and let the helper execute it, verify it, and freeze if the result is wrong or unclear. The helper still returns `NEEDS_PLANNER` because Word-open verification is manual until an app/window/accessibility detector exists.

To make a guided click verify Word through the Windows backend, pass a `window_state` verification block in the click command JSON. Save it as `.runtime\click_word.json`:

```json
{
  "request_id": "REQ-...",
  "cell": "B3",
  "expected_result": "Microsoft Word opens or is restored",
  "verification": {
    "type": "window_state",
    "title_contains": "Word",
    "require_foreground": true,
    "focus_if_needed": true
  }
}
```

```powershell
python helperctl.py recovery.guided_grid_click --via-app --json-file .runtime\click_word.json --timeout 35
```

### Save And Open Dialogs: Path-Text Guardrail

`input.type_text`, `input.paste_text` and `input.set_candidate_value`, including `type_text` and `paste_text` steps inside `input.action_sequence`, refuse to enter text that looks like a path while a dialog whose window title contains "Save" or "Open" is active. Text counts as a path if it contains `:\` or `/`, or starts with `\\`. The command returns `UNCERTAIN` with `looks_like_path: true` and does not type. This prevents a full path from landing in the wrong field of a native file dialog.

The safe pattern is:

1. Choose the folder as its own grounded step. Use the navigation pane or a candidate, or type a location name such as `Desktop` into the address bar and then check the breadcrumb. A full path typed into the address bar also trips the guardrail.
2. Type only a bare file name (for example `test.txt`) into the filename field. Prefer `input.set_candidate_value`, and observe again first, because candidate IDs are only valid for the observation that produced them.
3. Press Save through a candidate.
4. Prove the result with `verify.file_state`.

`docs/Simon/tasks/planner-loop-test-suite.md` (Loop 1) at the repository root walks through this.

`"allow_path_text": true` in a command's JSON turns the guardrail off for that one command. In `input.action_sequence` it can be set on a single step, or on the whole sequence, which applies it to every step that does not set it. It is a deliberate override for a run the owner is watching and has approved, not a default and not a way to retry a blocked step. Do not add it to stored payloads or templates.

### Bridge

The `--via-app` bridge is still sequential, but it now records bridge status/history, rejects duplicate Helper instances, and cleans stale pending request files. Send one bridged command at a time; a true queue/socket service is still future work.

Inspect the bridge (without `--via-app`, so it also works when the Helper is busy or stopped):

```powershell
python helperctl.py helper.bridge_status
```

### Helper Lifecycle And Shutdown

The Helper (`python agent_helper.py`) stays resident until something stops it. The ways it stops are:

- `helper.exit`: the normal end of a session. A planner must always send it at the end of its session, even after failures, so no Helper is left resident with input authority. It only takes effect through the bridge:

  ```powershell
  python helperctl.py helper.exit --via-app --timeout 10
  ```

  It returns `SUCCESS` with `data.helper_should_exit: true` and no `next_allowed_actions`. The Helper then shuts down.
- Escape: pressing Escape anywhere is an emergency stop. It cancels the command in flight and shuts the Helper down.
- Watchdog timeout: a bridged command that runs past its `--timeout` is cancelled and the Helper shuts down, so no input can happen after the timeout.
- Window close: closing the Helper's debug window shuts it down.
- Idle auto-exit: the Helper exits by itself after 600 seconds (10 minutes) with no activity. Activity means a bridged (`--via-app`) command starting or someone using the Helper's own controls; commands run without `--via-app` do not count. Set `SIMON_HELPER_IDLE_EXIT_SECONDS` before starting the Helper to change the limit. Values below 30 are raised to 30, `0` or a negative value disables auto-exit, and an unparseable value keeps the 600-second default. The idle check runs every 5 seconds.

After any stop, start the Helper again before sending more `--via-app` commands.

To confirm shutdown, run `python helperctl.py helper.bridge_status` without `--via-app` and check:

- `data.bridge_status.state` is `stopped`. `stopping` means shutdown was requested but has not finished; if it stays `stopping` for more than a few seconds, report it.
- `data.bridge_status.data` names the stop: `stop_event` (`helper_exit`, `emergency_stop`, `external_command_timeout`, `window_close` or `idle_auto_exit`), `stop_reason`, `stopped_at`, and `forced_exit`. `mainloop_returned` or `mainloop_exception` means the Helper's window loop ended without a recorded shutdown request. `forced_exit: true` means the 2-second force-exit fallback ended the process because the normal window teardown did not finish.
- `data.helper_runtime.running` is `false`.
- `data.history_tail` contains a `helper_stopped` event with the same stop details. On a normal exit it is followed by a `helper_startup_exit` event with reason `mainloop_returned`.

## Project Workflow

Simon follows the EUTONOS repository rules (root `AGENTS.md`). Simon-specific engineering notes, checks and the live-trial rules are in `docs/Simon/README.md` at the repository root; the remaining plan is `docs/Simon/ROADMAP.md`.

Keep runtime evidence under `.runtime/` local and transient. Route reproducible defects to the EUTONOS Known Bugs board and reusable findings to EUTONOS `docs/LESSONS.md` (tagged `(Simon)`); history is the Git log.

## Manual Test Report

Use the helper log panel and `.runtime/helper_log.txt` for evidence.

Note: `Simon. Screen Interface Mapping Operations Navigator` now starts with its debug window hidden. The top banner stays visible and always on top. Double-click the banner if you need to reopen the debug window.

The manual button-driven tests below are legacy Simon-target checks. Current planner-mode work should prefer `helperctl.py` commands and `--via-app` runs instead of relying on the Helper's old foreground control panel.

### Test 1: Static Window

1. Start `Simon Target`.
2. Start `Simon`.
3. Click `Scan Target`.
4. Click `Press Full Sequence`.

Expected: helper reaches `COMPLETE` and target shows `Complete` at `20 / 20`.

### Test 2: Moved Window

1. Start both apps.
2. Click `Scan Target`.
3. Move the `Simon Target` window.
4. Click `Scan Target` again.
5. Click `Press Full Sequence`.

Expected: helper detects the new bounds and completes the sequence.

### Test 3: Manual Window Move During Run

1. Start a sequence with `Press Full Sequence`.
2. Move the `Simon Target` window while it is running.

Expected: each click reacquires the target window and rescans before acting, so the helper should either continue from the new location or freeze if it cannot verify the result.

If another window steals focus or covers the target right before a click, the helper should refuse to act and write a failure report.

### Test 4: Wrong / Uncertain State

1. Cover part of the `Simon Target` window, minimize it, or move it partly offscreen.
2. Click `Scan Target`.

Expected: helper enters `NOT_READY`, `UNCERTAIN`, or `ERROR` and disables `Press Next` / `Press Full Sequence`.

### Test 5: Direct Codex vs Helper-Mediated Codex

Run one trial where Codex/computer-use clicks the Simon target buttons directly. Run another where Codex/computer-use only clicks helper controls. Compare success rate, time, wrong clicks, stalls, and recovery events.

### Test 6: Paste And Shortcut

1. Start both apps.
2. Click `Scan Target`.
3. In Simon, change the text in `Keyboard / Paste Test Controls`.
4. Click `Paste Text Into Target`.
5. Click `Toggle Switch Shortcut`.

Expected: Simon Target's text field receives the pasted text, its shortcut switch changes state, and Simon reports verified paste/switch results.

### Test 7: Dialog Challenge

1. Start both apps.
2. Click `Scan Target`.
3. In Simon, click `Run Dialog Test`.

Expected: Simon Target opens `Simon Dialog Test`, the helper enters `10`, clicks `Yes`, clicks `OK`, the dialog closes, and Simon reports a verified dialog pass.

### Test 8: 20-Step Gauntlet

1. Start both apps.
2. In Simon, click `Run 20-Step Gauntlet`.

Expected: helper resets the target, completes 18 verified Simon color clicks, pastes text into the target, completes the dialog challenge, and reports `PASS - 18 clicks + paste + dialog`.

## Known Limitations

- The helper uses a local telemetry JSON file for expected color and verification instead of OCR.
- Text and shortcut verification also use the local telemetry JSON file.
- `observe.ocr` is a command hook only unless an OCR engine is installed and configured.
- The current `--via-app` command bridge is file-based and sequential, not a multi-client service.
- Bridged commands now run behind one active worker thread so Tk can continue heartbeat and Escape polling while router work is in flight. Cancellation is cooperative, and live hard-stop behavior still requires interactive proof.
- The file bridge has no authenticated caller/least-authority boundary and is not accepted as a production or privileged IPC service.
- Deterministic command/control and host-cancellation contracts have automated coverage, but real Tk scheduling under Windows load, live capture/focus/input, and interactive target reactions do not; the implementation also remains concentrated in large modules.
- Transient desktop UI is not reliable across separate bridged commands; use helper-owned action sequences or a stronger bridge/session service.
- Multi-monitor desktop screenshots require virtual-origin coordinate mapping before clicks.
- Grid screenshots use layered capture fallbacks. In this desktop session, `PrintWindow` worked after Pillow, pyautogui, and desktop `BitBlt` failed.
- Color detection assumes the target button colors are not changed by OS high-contrast themes or aggressive display filters.
- If another window partially covers the target but leaves enough matching button color visible, the helper may still detect the button. The verification step should catch wrong/no progress and freeze.
- The helper is Windows-oriented because window title detection uses `ctypes.windll.user32`.
- `pyautogui` may need accessibility/security permission on some systems, though normal Windows desktop use should work.

## Suggested Next Experiments

The ordered reliability and maintainability slice plan lives in `docs/Simon/ROADMAP.md` at the repository root. It prioritizes repository recovery, characterization tests, responsive cancellation, modular decomposition, secure local transport, repeated benchmarking, and UI Automation before OCR or visual-target expansion.

## Generated command reference

<!-- simon:commands:start -->
| Command | Authority | Available | Arguments (plus shared) | Example |
| --- | --- | --- | --- | --- |
| `helper.commands` | read_only | yes | none | `python helperctl.py helper.commands` |
| `helper.bridge_status` | read_only | yes | none | `python helperctl.py helper.bridge_status` |
| `diagnose.environment` | read_only | yes | `include_capture` | `python helperctl.py diagnose.environment` |
| `recovery.stop_screensaver` | lifecycle | yes | none | `python helperctl.py recovery.stop_screensaver` |
| `helper.start` | lifecycle | yes | `allow_elevation`, `allow_sandbox`, `mode`, `wait_seconds` | `python helperctl.py helper.start` |
| `helper.start_elevated` | elevated | yes | `allow_elevation`, `allow_sandbox`, `mode`, `wait_seconds` | `python helperctl.py helper.start_elevated` |
| `helper.exit` | lifecycle | yes | none | `python helperctl.py helper.exit` |
| `status.current` | read_only | yes | none | `python helperctl.py status.current` |
| `detect.vm_boundary` | read_only | yes | `allow_minimized`, `allow_non_dialog_hwnd`, `current_dialog`, `current_dialog_hwnd`, `dialog_hwnd`, `exact_title`, `hwnd`, `min_height`, `min_width`, `target`, `target_window`, `title`, `title_contains`, `title_excludes`, `window_hwnd`, `working_window` | `python helperctl.py detect.vm_boundary` |
| `observe.windows` | read_only | yes | `title` | `python helperctl.py observe.windows` |
| `observe.window` | read_only | yes | `allow_minimized`, `exact_title`, `min_height`, `min_width`, `title`, `title_contains`, `title_excludes` | `python helperctl.py observe.window` |
| `observe.screenshot` | read_only | yes | `allow_minimized`, `allow_non_dialog_hwnd`, `columns`, `current_dialog`, `current_dialog_hwnd`, `dialog_hwnd`, `exact_title`, `hwnd`, `min_height`, `min_width`, `rows`, `step`, `target`, `target_window`, `title`, `title_contains`, `title_excludes`, `window_hwnd`, `working_window` | `python helperctl.py observe.screenshot` |
| `observe.ocr` | read_only | no | `allow_minimized`, `allow_non_dialog_hwnd`, `columns`, `current_dialog`, `current_dialog_hwnd`, `dialog_hwnd`, `exact_title`, `hwnd`, `min_height`, `min_width`, `rows`, `step`, `target`, `target_window`, `title`, `title_contains`, `title_excludes`, `window_hwnd`, `working_window` | `python helperctl.py observe.ocr` |
| `planner.fast_observe` | read_only | yes | `allow_minimized`, `allow_non_dialog_hwnd`, `columns`, `current_dialog`, `current_dialog_hwnd`, `dialog_hwnd`, `exact_title`, `failed_command`, `hwnd`, `min_height`, `min_width`, `rows`, `step`, `target`, `target_window`, `title`, `title_contains`, `title_excludes`, `verification`, `window_hwnd`, `working_window` | `python helperctl.py planner.fast_observe` |
| `planner.step` | input | yes | `action`, `based_on_request_id`, `planner_request_id`, `observation_request_id`, `allow_minimized`, `allow_mouse_activation`, `allow_non_dialog_hwnd`, `allow_non_text_field`, `allow_window_anchor`, `auto_follow_up_capture`, `candidate_hwnd`, `candidate_id`, `cell`, `change_poll_seconds`, `change_settle_seconds`, `change_timeout_seconds`, `changed_pixel_threshold`, `checked`, `columns`, `compare_width`, `coordinate_mode`, `current_dialog`, `current_dialog_hwnd`, `desktop_filename`, `dialog_hwnd`, `exact_title`, `expected_change`, `expected_kind`, `file_path`, `focus_if_needed`, `follow_up_hwnd`, `follow_up_target`, `hwnd`, `index`, `keys`, `keys_after_click`, `max_changed_ratio`, `max_mean_delta`, `min_height`, `min_width`, `observe_after`, `offset`, `path`, `post_action_delay`, `post_click_delay`, `require_foreground`, `rows`, `screenshot_on_failure`, `seconds`, `skip_screen_unchanged_check`, `step`, `target`, `target_window`, `text`, `text_after_click`, `title`, `title_contains`, `title_excludes`, `use_last_observation_request`, `value`, `verification`, `verify_before`, `wait_for_change`, `window_hwnd`, `working_window`, `x`, `x_pct`, `y`, `y_pct`, `preview`, `confirm_token` | `python helperctl.py planner.step` |
| `verify.file_state` | read_only | yes | `desktop_filename`, `expected_sha256`, `file_path`, `min_size_bytes`, `path`, `should_exist` | `python helperctl.py verify.file_state` |
| `verify.window_state` | read_only | yes | `allow_minimized`, `allow_non_dialog_hwnd`, `allow_window_anchor`, `columns`, `current_dialog`, `current_dialog_hwnd`, `dialog_hwnd`, `exact_title`, `focus_if_needed`, `hwnd`, `min_height`, `min_width`, `require_foreground`, `rows`, `screenshot_on_failure`, `step`, `target`, `target_window`, `title`, `title_contains`, `title_excludes`, `verification`, `window_hwnd`, `working_window` | `python helperctl.py verify.window_state` |
| `desktop.start_search_grid` | input | yes | `allow_minimized`, `allow_non_dialog_hwnd`, `columns`, `current_dialog`, `current_dialog_hwnd`, `dialog_hwnd`, `exact_title`, `failed_command`, `hwnd`, `min_height`, `min_width`, `post_click_delay`, `query`, `question`, `rows`, `search_settle_seconds`, `start_settle_seconds`, `step`, `target`, `target_window`, `text`, `title`, `title_contains`, `title_excludes`, `verification`, `window_hwnd`, `working_window` | `python helperctl.py desktop.start_search_grid` |
| `focus.window` | input | yes | `allow_minimized`, `allow_mouse_activation`, `allow_window_anchor`, `exact_title`, `hwnd`, `min_height`, `min_width`, `target_window`, `title`, `title_contains`, `title_excludes`, `window_hwnd`, `working_window` | `python helperctl.py focus.window` |
| `input.action_sequence` | input | yes | `actions`, `allow_minimized`, `allow_non_dialog_hwnd`, `columns`, `current_dialog`, `current_dialog_hwnd`, `dialog_hwnd`, `exact_title`, `focus_guard`, `hwnd`, `min_height`, `min_width`, `remembered_window`, `rows`, `step`, `target`, `target_window`, `title`, `title_contains`, `title_excludes`, `verification`, `window_hwnd`, `working_window` | `python helperctl.py input.action_sequence` |
| `input.expand_candidate` | input | yes | `allow_minimized`, `allow_non_dialog_hwnd`, `candidate_hwnd`, `candidate_id`, `columns`, `current_dialog`, `current_dialog_hwnd`, `dialog_hwnd`, `exact_title`, `expected_kind`, `hwnd`, `min_height`, `min_width`, `post_action_delay`, `rows`, `step`, `target`, `target_window`, `title`, `title_contains`, `title_excludes`, `window_hwnd`, `working_window` | `python helperctl.py input.expand_candidate` |
| `input.click_candidate` | input | yes | `allow_minimized`, `allow_non_dialog_hwnd`, `candidate_hwnd`, `candidate_id`, `columns`, `current_dialog`, `current_dialog_hwnd`, `dialog_hwnd`, `exact_title`, `expected_kind`, `hwnd`, `min_height`, `min_width`, `post_click_delay`, `rows`, `step`, `target`, `target_window`, `title`, `title_contains`, `title_excludes`, `window_hwnd`, `working_window` | `python helperctl.py input.click_candidate` |
| `input.click_point` | input | yes | `allow_minimized`, `allow_mouse_activation`, `allow_non_dialog_hwnd`, `allow_window_anchor`, `coordinate_mode`, `current_dialog`, `current_dialog_hwnd`, `dialog_hwnd`, `exact_title`, `hwnd`, `min_height`, `min_width`, `target`, `target_window`, `title`, `title_contains`, `title_excludes`, `window_hwnd`, `working_window`, `x`, `y` | `python helperctl.py input.click_point` |
| `input.hotkey` | input | yes | `allow_minimized`, `allow_mouse_activation`, `allow_non_dialog_hwnd`, `allow_window_anchor`, `current_dialog`, `current_dialog_hwnd`, `dialog_hwnd`, `exact_title`, `hwnd`, `keys`, `min_height`, `min_width`, `target`, `target_window`, `title`, `title_contains`, `title_excludes`, `window_hwnd`, `working_window` | `python helperctl.py input.hotkey` |
| `input.invoke_candidate` | input | yes | `allow_minimized`, `allow_non_dialog_hwnd`, `candidate_hwnd`, `candidate_id`, `columns`, `current_dialog`, `current_dialog_hwnd`, `dialog_hwnd`, `exact_title`, `expected_kind`, `hwnd`, `min_height`, `min_width`, `post_action_delay`, `rows`, `step`, `target`, `target_window`, `title`, `title_contains`, `title_excludes`, `window_hwnd`, `working_window` | `python helperctl.py input.invoke_candidate` |
| `input.paste_text` | input | yes | `allow_minimized`, `allow_mouse_activation`, `allow_non_dialog_hwnd`, `allow_window_anchor`, `current_dialog`, `current_dialog_hwnd`, `dialog_hwnd`, `exact_title`, `hwnd`, `min_height`, `min_width`, `target`, `target_window`, `text`, `title`, `title_contains`, `title_excludes`, `window_hwnd`, `working_window` | `python helperctl.py input.paste_text` |
| `input.select_candidate` | input | yes | `allow_minimized`, `allow_non_dialog_hwnd`, `candidate_hwnd`, `candidate_id`, `checked`, `columns`, `current_dialog`, `current_dialog_hwnd`, `dialog_hwnd`, `exact_title`, `expected_kind`, `hwnd`, `index`, `min_height`, `min_width`, `post_action_delay`, `rows`, `step`, `target`, `target_window`, `title`, `title_contains`, `title_excludes`, `window_hwnd`, `working_window` | `python helperctl.py input.select_candidate` |
| `input.set_candidate_value` | input | yes | `allow_minimized`, `allow_non_dialog_hwnd`, `allow_non_text_field`, `candidate_hwnd`, `candidate_id`, `columns`, `current_dialog`, `current_dialog_hwnd`, `dialog_hwnd`, `exact_title`, `expected_kind`, `hwnd`, `min_height`, `min_width`, `post_action_delay`, `rows`, `step`, `target`, `target_window`, `text`, `title`, `title_contains`, `title_excludes`, `value`, `window_hwnd`, `working_window` | `python helperctl.py input.set_candidate_value` |
| `input.type_text` | input | yes | `allow_minimized`, `allow_mouse_activation`, `allow_non_dialog_hwnd`, `allow_window_anchor`, `current_dialog`, `current_dialog_hwnd`, `dialog_hwnd`, `exact_title`, `hwnd`, `min_height`, `min_width`, `target`, `target_window`, `text`, `title`, `title_contains`, `title_excludes`, `window_hwnd`, `working_window` | `python helperctl.py input.type_text` |
| `recovery.capture_grid` | read_only | yes | `allow_minimized`, `allow_non_dialog_hwnd`, `columns`, `current_dialog`, `current_dialog_hwnd`, `dialog_hwnd`, `exact_title`, `failed_command`, `hwnd`, `min_height`, `min_width`, `rows`, `step`, `target`, `target_window`, `title`, `title_contains`, `title_excludes`, `verification`, `window_hwnd`, `working_window` | `python helperctl.py recovery.capture_grid` |
| `recovery.guided_grid_crop` | read_only | yes | `cell`, `crop_columns`, `crop_rows`, `title` | `python helperctl.py recovery.guided_grid_crop` |
| `recovery.guided_grid_click` | input | yes | `allow_minimized`, `allow_mouse_activation`, `allow_non_dialog_hwnd`, `allow_window_anchor`, `auto_follow_up_capture`, `cell`, `changed_pixel_threshold`, `columns`, `compare_width`, `coordinate_mode`, `current_dialog`, `current_dialog_hwnd`, `dialog_hwnd`, `exact_title`, `focus_if_needed`, `hwnd`, `keys`, `keys_after_click`, `max_changed_ratio`, `max_mean_delta`, `min_height`, `min_width`, `observe_after`, `offset`, `post_click_delay`, `require_foreground`, `rows`, `screenshot_on_failure`, `skip_screen_unchanged_check`, `step`, `target`, `target_window`, `text`, `text_after_click`, `title`, `title_contains`, `title_excludes`, `verification`, `window_hwnd`, `working_window`, `x`, `x_pct`, `y`, `y_pct`, `preview`, `confirm_token` | `python helperctl.py recovery.guided_grid_click` |
| `recovery.transient_grid_after_click` | input | yes | `allow_minimized`, `allow_mouse_activation`, `allow_non_dialog_hwnd`, `allow_window_anchor`, `cell`, `changed_pixel_threshold`, `columns`, `compare_width`, `coordinate_mode`, `current_dialog`, `current_dialog_hwnd`, `dialog_hwnd`, `exact_title`, `failed_command`, `hwnd`, `max_changed_ratio`, `max_mean_delta`, `min_height`, `min_width`, `offset`, `post_click_delay`, `question`, `rows`, `skip_screen_unchanged_check`, `step`, `target`, `target_window`, `title`, `title_contains`, `title_excludes`, `verification`, `window_hwnd`, `working_window`, `x`, `x_pct`, `y`, `y_pct`, `preview`, `confirm_token` | `python helperctl.py recovery.transient_grid_after_click` |
| `simon.detect_state` | read_only | yes | `title` | `python helperctl.py simon.detect_state` |
| `simon.press_next` | input | yes | `allow_minimized`, `allow_mouse_activation`, `allow_window_anchor`, `exact_title`, `hwnd`, `min_height`, `min_width`, `target_window`, `title`, `title_contains`, `title_excludes`, `window_hwnd`, `working_window` | `python helperctl.py simon.press_next` |
| `simon.force_grid_request` | read_only | yes | `allow_minimized`, `allow_non_dialog_hwnd`, `columns`, `current_dialog`, `current_dialog_hwnd`, `dialog_hwnd`, `exact_title`, `hwnd`, `min_height`, `min_width`, `rows`, `step`, `target`, `target_window`, `title`, `title_contains`, `title_excludes`, `verification`, `window_hwnd`, `working_window` | `python helperctl.py simon.force_grid_request` |
| `runtime.cleanup` | lifecycle | yes | `dry_run`, `max_age_days`, `max_bytes`, `max_groups` | `python helperctl.py runtime.cleanup` |

Shared arguments: `detail`, `reason`, `expected_result`, `failure_type`, `request_id`, `goal`, `intent`, `target_description`, `timeout`, `bridge_stale_seconds`, `bridge_request_id`, `skip_helper_precheck`, `allow_vm_control`, `allow_path_text`, `strict_args`, `protocol_version`, `include_images`, `image_max_edge`, `max_age_seconds`.

Argument types and command descriptions are returned by `helper.commands --detail full`. Unknown JSON arguments warn in v1; `protocol_version: 2` or `strict_args: true` rejects them. Defaults belong to each command and are never injected by argparse.
<!-- simon:commands:end -->
