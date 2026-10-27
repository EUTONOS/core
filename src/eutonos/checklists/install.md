# Install EUTONOS on a machine (also update and rollback)

Checklist id: `install`
Checklist revision: ipc5-1

This page ships inside the EUTONOS package (`eutonos/checklists/install.md`). You do not need a EUTONOS checkout to follow it. Follow the steps in order and tick each one off. If you have lost context, read "Lost context?" at the end first.

## How to use this page

Every step has a stable ID, a kind and a runner.

- **mechanical**: a command does the step and records it. The owner runs it during the IPC6, IPC8 and IPC10 trials. At any other time an agent runs it, after showing the owner a dry-run plan (`--dry-run`).
- **person**: only the owner can do it (a typed screen, an app button). You ask, then check the readback yourself.
- **agent**: judgment work. It is done only when its "Done when" holds and you can name the evidence.

The setup checkpoint is the tick-off list. It is one file, `<EUTONOS home>/install/checkpoint.json` (`~/.eutonos` unless `EUTONOS_HOME` says otherwise). `eutonos install`, `update`, `rollback` and `uninstall` write it for you when they finish.

- Show it: `eutonos-install checkpoint show`
- Tick a step off: `eutonos-install checkpoint record --step STEP_ID --state done --evidence "WHAT YOU SAW"`
- States: `pending`, `in_progress`, `done`, `blocked`, `skipped`.
- A `person` or `agent` step is `done` only with at least one `--evidence`.
- A `skipped` step needs `--reason "WHY"`.
- A blocked step should carry `--next-action "WHAT COMES NEXT"`.

Evidence in these four forms is rechecked by `eutonos doctor`, so it shows as stale if the thing changes later:

- `permission:ENTRY_ID=ok`
- `effect:EFFECT_ID=ok`
- `component:COMPONENT_ID=ok`
- `sha256:ABSOLUTE_PATH#SHA256_OF_THE_FILE`

Any other text is kept as plain evidence.

Notes on commands:

- `eutonos install|update|rollback|uninstall|doctor` go through the `eutonos` command. `resume`, `status` and `checkpoint` are only on `eutonos-install` (the same program).
- `--home` is only accepted before the verb: `eutonos-install --home H install ...`. It is not accepted through `eutonos install`.
- Run nothing against the owner's real machine unless the owner asked for it. To rehearse on a temporary folder: `python tools/install_check.py` in a EUTONOS checkout.

## Before you start

- Read `eutonos doctor` (read-only) to see what is already there: `eutonos doctor --json`.
- Only one install, update, rollback or uninstall can run per machine at a time. A second one answers `busy` and changes nothing. Wait; do not delete the lock.
- A machine with no EUTONOS yet has no `eutonos` command. The first install runs from the wheel: Setup.exe on Windows, or `python -I -B installer/windows/bootstrap.py WHEEL engine install --wheel WHEEL` from a source checkout. It imports the engine from the wheel and needs no existing runtime.
- You need the wheel file for the release you are installing.

## Install steps

### Step `prereq`
- Kind: mechanical
- Runs: agent, after a dry-run plan (the owner runs it during the IPC6, IPC8 and IPC10 trials)
- Command: `eutonos-setup prereq` (install also runs this check itself)
- Done when: the result says `ready` and `missing` is empty. Python 3.12+ is present. Git is not required to install: without it the core installs without its project memory features and each part that declares `requires.tools: ["git"]` is skipped, and the result's `unavailable_without_git` lists them.
- Evidence: the text `prereq check ready`. If Python is missing, the install stops with `prereq-missing` and changes nothing; install the tool (the result names it and how) and run again.

### Step `consent`
- Kind: person
- Runs: owner
- Command: none to type. `eutonos install` shows the consent screen in the terminal. The owner must type `I agree` at an interactive console. No flag skips it, and a non-interactive run refuses with `consent-refused`.
- Done when: the install record holds the accepted consent, and `eutonos doctor --json` shows `consent.state` as `ok`.
- Evidence: the consent text ID, the first 12 characters of its sha256 and the accepted time (the installer writes these).

### Step `runtime`
- Kind: mechanical
- Runs: agent, after a dry-run plan (the owner runs it during the IPC6, IPC8 and IPC10 trials)
- Command: first `eutonos install --wheel WHEEL --dry-run`, then `eutonos install --wheel WHEEL`. Options: `--adopt` (a machine that is already set up, see "Adopt mode"), `--owner-profile PATH`, `--channel dev|beta|stable`. One command runs all the mechanical steps from `runtime` to `activate`.
- Done when: the receipt `<home>/install/receipts/<gen_id>/01-runtime.json` exists and the runtime folder `<home>/runtimes/<artifact_id>` holds the wheel's bytes.
- Evidence: the receipt path.

### Step `kits`
- Kind: mechanical
- Runs: agent, after a dry-run plan (the owner runs it during the IPC6, IPC8 and IPC10 trials)
- Command: the same `eutonos install` run (phase 2).
- Done when: `02-kits.json` exists and each kit under `<home>/kits/` verifies.
- Evidence: the receipt path.

### Step `surfaces`
- Kind: mechanical
- Runs: agent, after a dry-run plan (the owner runs it during the IPC6, IPC8 and IPC10 trials)
- Command: the same `eutonos install` run (phase 3). It writes the launchers in `<home>/bin` and the Claude marketplace folder.
- Done when: `03-surfaces.json` exists and `<home>/bin` holds the launchers.
- Evidence: the receipt path.

### Step `components`
- Kind: mechanical
- Runs: agent, after a dry-run plan (the owner runs it during the IPC6, IPC8 and IPC10 trials)
- Command: the same `eutonos install` run (phase 4, `04-effects.json`). It installs every component that is available here: core, Crosstalk (Claude plugin hooks, Codex MCP block and helper), companion startup, remote-control, Hermes when it is present, and Simon (off by default; Windows; its bundled wheels need the runtime on the Python Setup carries, otherwise it is skipped with the reason; when on it also adds the `simon-codex-rule` Codex rule). role-hooks is held. The companion startup is held on a Mac until IPC8. A component that was not switched on (its default is off, or the person did not tick it) is still installed, but recorded as switched off with none of its effects outside the EUTONOS folder; switch one on later with `eutonos-install component enable ID`.
- Done when: `eutonos doctor --json` lists each component with a state. Required, selected components are `ok`. Held or skipped ones carry their reason, and a switched-off one says so.
- Evidence: `component:ID=ok` for each installed component. For a held or skipped one, record the reason in the step text.

### Step `permissions`
- Kind: mechanical
- Runs: agent, after a dry-run plan (the owner runs it during the IPC6, IPC8 and IPC10 trials)
- Command: the same `eutonos install` run (phase 4). It writes the machine-wide permission entries that need no repository: `set-remote-control` (4), `crosstalk-reply` (6), `letter-rule` (7), `record-keeping-rule` (8) and `codex-mcp-block` (10), as the consent screen listed them. Entry 20 (`desktop-rc-default`) comes from the remote-control component and entry 22 (`user-path`) from the `path` step below. Project entries (2, 5, 12, 13, 14) are not written here; the add-repository page does them.
- Done when: `eutonos doctor --json` `declared.permissions.items` shows each machine entry as `ok`, or `held`/`skipped` with a reason.
- Evidence: `permission:ENTRY_ID=ok` for each.

### Step `instructions`
- Kind: mechanical
- Runs: agent, after a dry-run plan (the owner runs it during the IPC6, IPC8 and IPC10 trials)
- Command: the same `eutonos install` run (phase 4). It adds the managed instruction blocks (completion contract, Crosstalk, Decisions, memory) to the Claude and Codex instruction files (`~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`). The memory block (`eutonos:memory`) is a short pointer to the installed tooling, rendered for each app from `eutonos/templates/memory_block.md`. When the Hermes component is installed it also goes into Hermes' `SOUL.md`, appended and only if that file already exists: EUTONOS never creates `SOUL.md`, and says in the run notes when it is missing or so long that Hermes may cut the block. Per file and block: no copy adds the block; an intact marked block is refreshed in place; an exact unmarked copy of the text gets the markers put around it where it is (nothing moves, no second copy, and uninstall takes out only the markers); a similar copy (the heading is there, the text differs) is left active and written nowhere, and goes to the `three-way` step as `similar-unmarked-copy`. Each effect records what was there before, so uninstall puts it back in place even if the file was edited elsewhere afterwards. The Simon block (`eutonos:simon`) is written only while the Simon part is installed and switched on, and goes into `SOUL.md` only while the Hermes part is on too. The portable bootstrap (`python -I -m eutonos.completion_policy install --global`) is a separate route: it writes the completion and Decisions blocks only, never Crosstalk or memory.
- Done when: doctor lists the instruction effects as `ok`. A block you or the owner edited is reported as "changed by you" and kept.
- Evidence: `effect:EFFECT_ID=ok` for each instruction block.

### Step `path`
- Kind: mechanical
- Runs: agent, after a dry-run plan (the owner runs it during the IPC6, IPC8 and IPC10 trials)
- Command: the same `eutonos install` run (phase 4). It puts `<home>/bin` on the user PATH.
- Done when: doctor shows the PATH effect (`env-path.bin`) as `ok`. A new terminal finds `eutonos`.
- Evidence: `effect:env-path.bin=ok`.

### Step `owner-profile`
- Kind: mechanical
- Runs: agent, after a dry-run plan (the owner runs it during the IPC6, IPC8 and IPC10 trials)
- Command: pass `--owner-profile PATH` to `eutonos install`. The file is copied to `<home>/profiles/owner.json`.
- Done when: the file is copied, or the step is `skipped` with the reason "no owner profile was supplied".
- Evidence: the receipt text, or the skip reason.

### Step `plugin`
- Kind: mechanical
- Runs: agent, after a dry-run plan (the owner runs it during the IPC6, IPC8 and IPC10 trials)
- Command: the same `eutonos install` run (phase 5). It registers the Claude plugin. With `--adopt`, when the marketplace `crosstalk-p0-local` is already registered at an earlier hand install, it removes that registration (recorded as `migrate.marketplace`) and registers the installed copy; any failure puts the earlier marketplace and plugin back at once. Without `--adopt` a marketplace registered elsewhere is left alone and the step is skipped.
- Done when: `05-plugin.json` exists. It is `skipped` with a reason when Claude is not installed, or when the repoint could not finish.
- Evidence: the receipt path, or the skip reason.

### Step `activate`
- Kind: mechanical
- Runs: agent, after a dry-run plan (the owner runs it during the IPC6, IPC8 and IPC10 trials)
- Command: the same `eutonos install` run (phase 6). It writes the record and the state last.
- Done when: `eutonos-install status` shows `active` set to the new generation and `active_record.intact` true.
- Evidence: the generation ID.

If the very first install fails, there is no older generation to go back to. The install undoes this run's own effects, records the state `rolled-back` and says "Nothing is installed". Fix the cause and run the install again.

### Step `restart-apps`
- Kind: person
- Runs: owner
- Command: none. Ask the owner to restart Claude Desktop, Claude Code and Codex so they read the new settings.
- Done when: the apps were restarted after the install finished.
- Evidence: the owner's confirmation and the time.

### Step `remote-control-prompt`
- Kind: person
- Runs: owner
- Command: none. The first time Claude starts Remote Control it asks y/n. The owner answers it.
- Done when: the owner answered and Remote Control is on or off as the owner chose.
- Evidence: the owner's answer.

### Step `mac-grants`
- Kind: person
- Runs: owner
- Command: none. On a Mac the owner allows the Terminal and Claude automation prompts when macOS asks. Also check the launchd item and app paths. On any other machine this step is `skipped` with the reason "this is not a Mac".
- Done when: the owner allowed the prompts and you checked that the launchd item exists and the app paths are right.
- Evidence: what you read back (for example the launchd item name and the app path).

### Step `orchestration`
- Kind: agent
- Runs: agent
- Command: `eutonos-settings status` shows how each task resolves. Choose the routes and roles from the models this machine can really start. Edit roles and task rules in the dashboard (Settings > Roles and Task Rules). Check one route with `eutonos-settings route --task TASK --primary MODEL`. Send one test brief on each route. Role hooks stay held.
- Done when: `eutonos-settings status` shows every task resolving to a worker that can start here, and each route answered its test brief.
- Evidence: the `status` output summary and one line per route that answered.

### Step `cross-app-check`
- Kind: agent
- Runs: agent
- Command: `eutonos-crosstalk status`, then send one letter each way on every enabled app pair (Claude to Codex and back, and so on) with the Crosstalk send tool. Keep the exact native arrival and reply evidence.
- Done when: each enabled pair has a letter that arrived and a reply that came back. "Held" or "accepted" is not proof. Never resend a letter that was reported accepted, held or uncertain; check status instead.
- Evidence: for each pair, the letter ID and the native arrival and reply readback.

### Step `three-way`
- Kind: agent
- Runs: agent
- Command: none. This step only exists when the record has a `three_way` list (an update or install met a managed file that was changed by hand, or an instruction block with a similar unmarked copy: `eutonos doctor` shows the same list). For each row, merge the old shipped text, the new shipped text and the current text. Never overwrite the person's changes.
- Instruction-block rows (reason `similar-unmarked-copy` or `customized-managed-block`): you may keep the person's version, take the shipped one or merge the two. Save the original file first (copy it beside the file or into the install backups), and report the choice as a **Decided:** line with the reason. If the person's version is stricter on approvals, or holds rules the shipped block would drop, keep theirs and ask the owner before changing anything. Taking the shipped block means replacing the person's text with the marked block (`eutonos:completion`, `eutonos:crosstalk`, `eutonos:decisions` or `eutonos:memory`, from the row's shipped hash) so uninstall can manage it.
- Done when: every row was merged or the owner chose to keep the current text.
- Evidence: the row's file path and what you decided.

## Done when (whole install)

- Every required, selected capability is verified.
- Held or skipped items are listed with their reason.
- `eutonos doctor` and the checkpoint agree (`eutonos doctor --json`: `readiness.ready` true, no blocked or stale checkpoint step).

This page does not ask for "all OK". Some parts (Hermes, the Mac companion) may be held or not selected on purpose.

## Install with Setup.exe (Windows)

`EUTONOS-Setup-<version>.exe` is the same install as the steps above, with a normal Windows wizard in front of it. It is built from a source checkout with `python tools/build_setup.py` (see `installer/windows/README.md`; it prints the sha256 of the wheel and of Setup.exe, so you can compare them with the owner's copy).

- Windows SmartScreen will say "Windows protected your PC", because Setup.exe is not code-signed. Choose "More info", then "Run anyway". A signing certificate is the owner's spending decision.
- Setup asks where to install (recommended: `%USERPROFILE%\.eutonos`, with a Browse button), which parts to switch on, whether only one person uses this Windows login, then shows the exact consent text with an "I agree" box. Next stays off until it is ticked. Setup refuses `/SILENT`, because the consent has to be read on screen.
- Every part this PC can run is installed whatever is ticked. Each box reads, for example, "Crosstalk (installed; tick to switch on now)": the tick only says whether the part starts switched on. An unticked part is installed but switched off and adds nothing outside the EUTONOS folder; the consent text says "switched on" or "switched off" for each. A part whose app is not on this PC is skipped and says so. Later, `eutonos-install component enable|disable ID` or the dashboard switches a part.
- "Only I use this Windows login" is unticked by default, with a note to leave it unticked on a login other people use. When ticked, the consent text says so, and after a good install one more page lists the Claude login emails Crosstalk has seen (a suggestion only) for the person to keep, fix or clear. Only the emails confirmed there are saved, through `wizard apply-settings` (the same code as `eutonos-install setting set linked-accounts`), which turns on Crosstalk linked accounts so chat history follows a switch between those accounts. A blank list, or an unticked box, saves nothing. A setting that cannot be saved does not fail the install: the finish page says so and a step is left for the next agent (`eutonos-install pending list`). Uninstall keeps this setting with the rest of Crosstalk's device data in the EUTONOS folder.
- A Remote Control launcher you already have in your Startup folder is detected. Remote Control then starts off, with the note "you already have one at login", and Setup never changes your launcher.
- If an earlier hand install is found, Setup adopts it (the same as the Adopt mode below) and says so on the change list.
- Setup runs the steps `prereq` to `activate` through the engine and shows them live. `restart-apps` is the finish page's last sentence.
- After a good install the finish page also has an "Open dashboard" button. Nothing starts until it is clicked: it starts the installed dashboard (`<home>\bin\eutonos-dashboard.exe`, on port 8765) if none is running, then opens it in the browser. The dashboard keeps running after Setup closes.
- What Setup writes beyond the standard install: the bundled Python at `<home>\python` (it stays until uninstall), its own files in `<home>\setup`, one Windows Installed apps entry, and, for a folder other than the default, the user variable `EUTONOS_HOME`. All are recorded and undone by uninstall (permission entries `user-env-home` and `installed-apps-entry`, both off in the typed console route).
- The chosen folder is a real install (the install record is marked for this user), so the Claude, Codex and Hermes settings are the person's own. A folder chosen any other way still gets the staging-user safety rule.
- To remove it: Windows Settings, Installed apps, EUTONOS. That runs the engine's uninstall first and stops, deleting nothing, if the engine reports a problem. Backups, hosts, RAM and project data stay.

Done when: Setup's finish page says "EUTONOS is installed", and the "Done when (whole install)" list above holds.

## Adopt mode (a machine that is already set up)

For a machine that was set up by hand, such as the owner's main PC.

1. Run `eutonos doctor --json` first. It changes nothing.
2. Inspect what exists before you claim anything. A matching setting that was there before is not installer-owned until it has been adopted, and the record says so.
3. Keep adopted hosts and the legacy runtime. When more than one host matches, pick the exact one.
4. Switch the companion and the closeout runtime to the new install before you add any repository.
5. Run `eutonos install --wheel WHEEL --adopt --dry-run`, then without `--dry-run`. Install only what is missing.

Uninstall puts the adopted items (the Codex block, the helper shortcut, the companion Run value, the Claude marketplace registration and its plugin) back as they were. A later `--adopt` install repoints them again.

## Update

- Check: `eutonos update --check` says whether a newer release is available. It downloads no wheel and changes nothing. `eutonos-install setting set update-mode manual|notify|auto` chooses what the companion's daily check does (default auto: it downloads and verifies a newer release, then asks the person before installing; notify: it leaves a pending action and a popup; manual: nothing runs on its own). A hand-run check only reports. `eutonos-install update --staged` installs a release the background check already staged, after checking its signature and sha256 again. The older `update-notice on|off` still works (on is notify, off is manual).
- Dry run: `eutonos update --dry-run` shows the release found and changes nothing.
- Update: `eutonos update`. Options: `--channel dev|beta|stable` (switch channel first), `--source FOLDER` (a release folder instead of the public GitHub releases repository, which is read over plain HTTPS with no `gh` and no login).
- It installs the channel's newest release beside the current one and checks its sha256.
- A managed file the person changed goes to the `three-way` step above. It is never overwritten.
- It runs `eutonos doctor` at the end. Read the doctor result in the output.
- The checkpoint is rewritten for the new generation. An update skips `prereq` and `consent` with a reason.
- Turning a component on or off (an add-on, or a part that was held) is an update step: change the owner profile choice, then run `eutonos update`.

## Rollback

- Dry run: `eutonos rollback --dry-run` shows what would be reversed and which newer blocks stay.
- Run: `eutonos rollback`, then `eutonos doctor --json`.
- It refuses when there is no previous generation, when that generation is older than the data allows (`below-min-compatible`), or when the old runtime or kit is damaged.
- Two steps exist only in the rollback checkpoint:
  - `resync` (mechanical, agent): launchers and the plugin are pointed back. Done when the rollback result lists the launchers and the plugin state. Evidence: that line.
  - `reverse-newer-effects` (mechanical, agent): effects only the newer generation made are removed. Done when the result lists removed and kept items. Evidence: the counts.
- Then do `restart-apps` (above) and, if the checkpoint has it, `three-way`.

## Lost context? Resume here

1. `eutonos-install status` shows the active generation, any pending run, and the checkpoint counts and next open step.
2. `eutonos-install checkpoint show` lists every step and its state.
3. `eutonos doctor --json` says which declared items are OK, missing or changed by you, and which checkpoint steps are done, stale or missing evidence.
4. If a run was interrupted, `status` shows `pending` with the completed phases, the next phase and anything unrecorded. Inspect the targets it names before you retry anything. Then run `eutonos-install resume`. It continues at the next phase that has no receipt. Targets the person changed are kept. Report anything left over.
5. Do the first step that is not done or skipped. Check the evidence of the steps before it; do not trust "done" you did not see.

## Concurrency and locks

- One machine lifecycle operation at a time (install, update, uninstall, rollback), held by the machine lock under `<home>/install/`. A second one answers `busy`. Wait for the first to finish.
- Checkpoint writes wait for the lock for up to 30 seconds.
- Install never touches the project registry. Lock order, when both are needed, is the projects lock first, then the machine lock.
- After a resume, recheck what each step depends on (files, hashes, hosts) before you write again.

### Setup recovery and pending agent actions

For an active or interrupted install whose bundled Python, bootstrap or wheel is missing, rerun Setup and explicitly choose support-file repair. Close EUTONOS apps first. Repair checks the lifecycle lock during preflight, then Inno copies and records only Setup-owned files; the lock does not span that copy and Windows protects in-use files. It does not replay install or accept consent. Its finish page shows executable bundled-bootstrap `status`, `resume` and `uninstall --keep-projects` commands. These also work before the normal launcher exists. Inspect status and choose the recovery action explicitly.

Pending agent actions are durable, but a fresh install keeps role hooks held, so automatic delivery at every turn start is not guaranteed. From any chat, including one without a project, run `eutonos-install pending list --harness codex` (or `claude` / `hermes`), carry out the applicable actions, then `eutonos-install pending done ID`. Workflow pickup also displays them. Do not enable held hooks just to deliver this queue.

Rollback to a generation made by an older engine without the reversible-recovery contract is refused. Keep using the current engine for exact-byte removal; updating from an older installation does not retroactively recover missing before-images.
