# Uninstall EUTONOS from a machine

Checklist id: `uninstall`
Checklist revision: ipc5-1

This page ships inside the EUTONOS package (`eutonos/checklists/uninstall.md`). You do not need a EUTONOS checkout to follow it. If you have lost context, read "Lost context?" at the end first.

## How to use this page

Every step has a stable ID, a kind and a runner.

- **mechanical**: a command does the step and records it. The owner runs it during the IPC6, IPC8 and IPC10 trials. At any other time an agent runs it, after showing the owner a dry-run plan (`--dry-run`).
- **person**: only the owner can do it. You list it, ask, and check the readback yourself.
- **agent**: judgment work. It is done only when its "Done when" holds and you can name the evidence.

Progress lives in the setup checkpoint, `<EUTONOS home>/install/checkpoint.json` (`~/.eutonos` unless `EUTONOS_HOME` says otherwise). `eutonos uninstall` writes it when it finishes, with the person and agent steps still pending.

- Show it: `eutonos-install checkpoint show`
- Tick a step off: `eutonos-install checkpoint record --step STEP_ID --state done --evidence "WHAT YOU SAW"`
- States: `pending`, `in_progress`, `done`, `blocked`, `skipped`. A `person` or `agent` step is `done` only with evidence. A `skipped` step needs `--reason`.

`eutonos doctor` rechecks evidence written as `permission:ID=state`, `effect:ID=state`, `component:ID=state` or `sha256:PATH#HASH`.

Notes on commands:

- `eutonos uninstall` goes through the `eutonos` command. `status` and `checkpoint` are only on `eutonos-install`. `--home` is only accepted before the verb (`eutonos-install --home H uninstall ...`).
- Do not run this against the owner's real machine unless the owner asked for it.

## What uninstall keeps

Uninstall reverses the install record newest first. It keeps:

- every file the person changed (reported as "kept");
- the person's own paragraphs in the global instruction files (Claude's `CLAUDE.md`, Codex's `AGENTS.md` and, when the memory block went into it, Hermes' `SOUL.md`): uninstall finds each managed block (completion, Crosstalk, Decisions, memory) by its markers and puts back exactly what was there before it (the earlier block text, or nothing if EUTONOS created it; for a copy EUTONOS wrapped in markers, only the markers come out and the text stays). Edits elsewhere in the file are untouched. A block edited inside is kept and reported "changed by you, kept";
- backups, hosts, RAM, project data and the install record itself.

It never deletes the EUTONOS home. Deleting `~/.eutonos` for good is a separate step that only the owner runs.

## Order

Do the agent steps first (they need the `eutonos-crosstalk` command, which uninstall removes from the PATH). Then run `eutonos uninstall`. Then record the result and do the person steps.

## Steps

### Step `registered-projects`
- Kind: mechanical
- Runs: agent, after a dry-run plan (the owner runs it during the IPC6, IPC8 and IPC10 trials)
- Command: first each registered repository is detached, or the owner chooses to keep it:
  - detach: `eutonos remove-project --root REPOSITORY_ROOT` (follow the `remove-repository` page for each one);
  - keep: add `--keep-projects` to the uninstall below. The projects stay registered in `projects.json`.
  The list of registered repositories is in `<home>/projects.json`. `eutonos uninstall --dry-run` also names them: it refuses while any is registered and `--keep-projects` is not given.
- Done when: `projects.json` lists no repository, or the owner chose `--keep-projects` and the record's `kept_projects` names them.
- Evidence: the count and the choice, for example `0 registered; keep_projects=False` (the uninstall writes this line).

### Step `halt-crosstalk`
- Kind: agent
- Runs: agent
- Command: `eutonos-crosstalk halt`, then `eutonos-crosstalk status`. Do this before the uninstall.
- Done when: `status` shows `controls.halt` is true. If the command is already gone, say so and record that the halt could not be verified.
- Evidence: the `status` line showing the halt.

### Step `running-chats`
- Kind: agent
- Runs: agent
- Command: none. A Crosstalk halt blocks new letters but does not stop an agent that is already running. List the EUTONOS chats that are still open and ask the owner to stop or finish them.
- Done when: the owner confirmed the running chats have stopped writing to EUTONOS files, or knowingly left them.
- Evidence: the list of chats and the owner's answer.

### Step `stop-companion`
- Kind: agent
- Runs: agent
- Command: none to type. Ask the owner to use Quit on the EUTONOS companion badge (the hourly check honors an intentional Quit), or end its process. A dashboard can also still be running on its own (the Setup finish page's "Open dashboard" button starts one that keeps running after Setup closes): if something still answers on http://127.0.0.1:8765, ask the owner to end the `eutonos-dashboard` process. The startup entry (scheduled task or Run value) is reversed by the uninstall itself; stopping the running processes is this step.
- Done when: no EUTONOS companion or dashboard process is running.
- Evidence: what you checked (for example the process list was empty).

### Step `reverse-effects`
- Kind: mechanical
- Runs: agent, after a dry-run plan (the owner runs it during the IPC6, IPC8 and IPC10 trials)
- Command: first `eutonos uninstall --dry-run` (it lists every effect it would reverse and the person steps left), then `eutonos uninstall`. Options: `--keep-projects`, `--dry-run`.
- Done when: the result says `uninstalled`. `removed` lists what was undone. `kept` lists what was kept because it changed (read each reason). `eutonos-install status` shows no active generation.
- Evidence: `removed N, kept M` (the uninstall writes this line), and the receipt `<home>/install/receipts/<gen_id>/90-uninstall.json`.

If a run says `busy`, another install, update, rollback or uninstall is running. Wait. If it says `refused` with `projects-registered`, finish `registered-projects` first.

### Person reversals

The uninstall writes one `person` step for each setting that only the owner can undo. Ask the owner, then check what they did. The steps are:

### Step `undo-desktop-folder-auto`
- Kind: person
- Runs: owner
- Command: none. In Claude Desktop, each project folder you added may still be set to Auto. The owner changes it back if they want. EUTONOS never changes that setting for you.
- Done when: the owner said it is done or chose to keep it.
- Evidence: the owner's answer.

### Step `undo-codex-trust`
- Kind: person
- Runs: owner
- Command: none. In Codex the owner removes the folder trust lines (`[projects."<project folder>"]` with `trust_level`) from `config.toml` if they want Codex to stop trusting those folders.
- Done when: the owner said it is done or chose to keep it.
- Evidence: the owner's answer.

### Step `undo-codex-hook-trust`
- Kind: person
- Runs: owner
- Command: none. Held for now. Role hooks ship later, and Codex asks for trust then.
- Done when: nothing to undo; record why.
- Evidence: the reason, or `skipped` with a reason.

### Step `undo-desktop-rc-default`
- Kind: person
- Runs: owner
- Command: none. Optional. In Claude Desktop Settings the owner turns Remote Control for new chats back off if they turned it on.
- Done when: the owner said it is done or never turned it on.
- Evidence: the owner's answer.

### Step `undo-org-channels`
- Kind: person
- Runs: owner
- Command: none. Only a Team organisation admin can change channels in the organisation settings. Ask the admin if it was allowed for EUTONOS.
- Done when: the owner or admin said it is done or not needed.
- Evidence: the answer.

### Step `undo-claude-chat-copy`
- Kind: person
- Runs: owner
- Command: none. Optional. In the EUTONOS dashboard the owner opens Settings > Claude and switches Chat copying off if they turned it on. Chat files already copied into an account stay there; the helper's trash and snapshot folders under the device helper folder can be deleted by the owner.
- Done when: the owner said it is done or never turned it on.
- Evidence: the owner's answer.

### Step `undo-restart-apps`
- Kind: person
- Runs: owner
- Command: none. Ask the owner to restart Claude Desktop, Claude Code and Codex so they stop using EUTONOS.
- Done when: the apps were restarted after the uninstall.
- Evidence: the owner's confirmation and the time.

## Done when (whole uninstall)

- `eutonos-install status` shows no active generation.
- Every `kept` item was read and reported to the owner.
- Every person step is `done` or `skipped` with a reason.
- Backups, hosts, RAM, project data and the install record are still there, and you told the owner they are.

## Lost context? Resume here

1. `eutonos-install status` shows whether a generation is still active and any pending run.
2. `eutonos-install checkpoint show` lists the steps and states. If the checkpoint says `uninstall`, the uninstall already ran: continue with the first step that is not `done` or `skipped`.
3. If the checkpoint still says `install` and a generation is active, the uninstall has not run. Start at `registered-projects`.
4. If an uninstall was interrupted, look at `kept` and at the files named in the record before you run it again. Running `eutonos uninstall` again is safe: removed items are already gone and kept items stay kept.
5. `eutonos doctor --json` (read-only) lists what is still declared and still present.

## Concurrency and locks

- One machine lifecycle operation at a time (install, update, uninstall, rollback), held by the machine lock under `<home>/install/`. A second one answers `busy` and changes nothing. Wait for the first to finish.
- Uninstall only reads `projects.json`. It does not take the projects lock. Detaching a repository takes the projects lock for that repository. Lock order, when both are needed, is the projects lock first, then the machine lock.
- Checkpoint writes wait for the machine lock for up to 30 seconds.
