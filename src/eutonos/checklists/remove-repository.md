# Remove EUTONOS from a repository

Checklist id: `remove-repository`
Checklist revision: ipc5-d.1

This page ships inside the EUTONOS package (`eutonos/checklists/remove-repository.md`). You do not need a EUTONOS checkout to follow it. If the repository's own EUTONOS tooling is already gone, nothing here needs it: the removal runs from the installed `eutonos` commands. If you have lost context, read "Lost context?" at the end first.

## Two levels

- **Detach from this machine** (the default). Removes this machine's registration, the project permission entries and the exclude lines, and archives the host. Repository files stay as they are, because a shared repository may still be in use on another machine.
- **Full de-adoption.** Also reverses `eutonos.adoption.json`: it removes the managed repository content the preservation plan marks `remove`. It needs a preservation plan first. By default the project's own Docs, RAM, NAV and history stay; EUTONOS-managed blocks, wrappers and generated views go.

Ask the owner which level they want. If they have not said, use detach.

## How to use this page

Every step has a stable ID, a kind and a runner. The step IDs are the completion-check IDs the slice uses.

- **mechanical**: a command does the step. The owner runs it during the IPC6, IPC8 and IPC10 trials. At any other time an agent runs it, after showing the owner the dry-run plan.
- **person**: only the owner can do it. You list it, ask, and check the readback yourself.
- **agent**: judgment work. It is done only when its "Done when" holds and you can name the evidence.

Progress is kept in two places:

- **The slice.** One standalone slice per run, with the checks pasted from the snippet below. A check records evidence; it runs nothing. Mark it `verified` only with a real readback reference. An unfinished check keeps an honest `remaining_action`.
- **The removal checkpoint.** `remove-project` records its own seven steps (named on each step below as "Checkpoint step"). It uses the machine setup checkpoint (`<EUTONOS home>/install/checkpoint.json`) when that is free or already this removal. When another checklist is unfinished there, it leaves it alone and uses a file beside `projects.json` (`remove-repository-checkpoint-<key>.json`). The result's `checkpoint.path` says which. Module progress stays in the repository's Setup status block; the slice points to it.

### Paste into the slice

Print the checks (one line, changes nothing):

`eutonos remove-project --print-checks remove`

(`python -I -m eutonos.repository_lifecycle --print-checks remove` does the same.) The output is `{"completion_checks": [...]}`. Put the array in the slice's `extensions.completion_checks`. The exact JSON the command prints is:

```json
[{"evidence":[],"id":"remove.work-parked","related_work_ids":[],"remaining_action":"Close out or park the active work.","required":true,"state":"pending","title":"Active work finished or parked and closed out."},
{"evidence":[],"id":"remove.writers-stopped","related_work_ids":[],"remaining_action":"Halt Crosstalk, then confirm each chat has stopped (halt does not stop a running agent).","required":true,"state":"pending","title":"Chats working in this repository have stopped writing."},
{"evidence":[],"id":"remove.preservation-plan","related_work_ids":[],"remaining_action":"Agree the plan; the default keeps native Docs, RAM, NAV and history.","required":false,"state":"pending","title":"Preservation plan agreed with the owner (full de-adoption only)."},
{"evidence":[],"id":"remove.checkpoint-saved","related_work_ids":[],"remaining_action":"Confirm the checkpoint file lists the steps.","required":true,"state":"pending","title":"Checkpoint saved before the wrapper is removed."},
{"evidence":[],"id":"remove.entries-reversed","related_work_ids":[],"remaining_action":"Read back the files named in the Effects.","required":true,"state":"pending","title":"Project permission Effects and exclude lines reversed; changed ones reported."},
{"evidence":[],"id":"remove.person-reversals","related_work_ids":[],"remaining_action":"List them for the person; they remain the person's to change.","required":true,"state":"pending","title":"The person was told the Desktop Auto and Codex trust settings are theirs to undo."},
{"evidence":[],"id":"remove.adoption-reversed","related_work_ids":[],"remaining_action":"Read back the removal report and the backup folder.","required":false,"state":"pending","title":"Adoption manifest reversed for remove paths; changed files reported and kept."},
{"evidence":[],"id":"remove.device-withdrawn","related_work_ids":[],"remaining_action":"Read back device-repositories.json.","required":true,"state":"pending","title":"Root withdrawn from device-repositories.json."},
{"evidence":[],"id":"remove.unregistered","related_work_ids":[],"remaining_action":"Read back projects.json.","required":true,"state":"pending","title":"Root removed from projects.json under the registry lock."},
{"evidence":[],"id":"remove.host-retired","related_work_ids":[],"remaining_action":"Read back the host's retired.json and that read_snapshot still validates.","required":true,"state":"pending","title":"Host archived with its retired marker, not deleted."},
{"evidence":[],"id":"remove.repository-files","related_work_ids":[],"remaining_action":"Compare the repository tree with its recorded state.","required":true,"state":"pending","title":"Repository files are untouched (detach) or only plan-listed paths changed (de-adopt)."}]
```

## The removal command

One command does every mechanical step below, in order, and stops at the first failure (the step is recorded `blocked`; run the same command again to resume; finished steps are not repeated).

1. Dry run, which changes nothing and prints every planned change: `eutonos remove-project --root ROOT --dry-run`.
2. Detach: `eutonos remove-project --root ROOT`.
3. Full de-adoption: `eutonos remove-project --root ROOT --de-adopt --plan PLAN.json` (add `--dry-run` first). `--plan` only works with `--de-adopt`.
4. Add `--host DIR` when the project is no longer in the registry and you must name its host directory.

Results: `removed` (all done), `removed-with-kept` (done, but something you or the owner changed was kept; read `kept_permission_effects` and `kept_repository_files`), `blocked` (the exit code is 2; the result names the step and why) and `error`.

## Steps

### Step `remove.work-parked`
- Kind: agent
- Runs: agent
- Command: none. Finish or park the work that is active in this repository and close it out through its normal closeout.
- Done when: the repository's board shows no work in progress that depends on EUTONOS being here, or the owner knowingly parked it.
- Evidence: the closeout or parking record, and the owner's answer.

### Step `remove.writers-stopped`
- Kind: agent
- Runs: agent
- Command: `eutonos-crosstalk halt`, then `eutonos-crosstalk status`. Then ask the owner which chats are working in this repository and have them stop. A halt blocks new letters. It does not stop an agent that is already running.
- Done when: `controls.halt` is true in `status`, and the owner confirmed each chat in this repository has stopped writing.
- Evidence: the `status` line and the list of chats with the owner's answer. Resume Crosstalk after the removal only if the owner wants it.

### Step `remove.preservation-plan`
- Kind: agent
- Runs: agent
- Checkpoint step: `preservation-plan`
- Command: for full de-adoption only. `python -I -m eutonos.repository_lifecycle --root ROOT --print-preservation-template` prints the default plan: every adoption-manifest path is `keep` (the project's native owners and the Docs, RAM and NAV configuration) or `remove` (managed blocks, wrappers, generated views). Save it as `PLAN.json`, agree it with the owner line by line, then run the removal with `--de-adopt --plan PLAN.json`. Removing a native owner needs the explicit form `{"action": "remove", "discard_native": true}` for that path, and the owner's yes. For detach this step is `skipped` with the reason "detach level".
- Done when: the owner agreed the plan, and `eutonos remove-project --root ROOT --de-adopt --plan PLAN.json --dry-run` accepts it (it lists every path exactly once).
- Evidence: the plan file path and its sha256, and the owner's agreement. This check is not required for detach.

### Step `remove.checkpoint-saved`
- Kind: agent
- Runs: agent
- Command: none of its own. The removal command opens the checkpoint before it changes anything, so it is saved before the wrapper or any other managed file is removed.
- Done when: the file at the result's `checkpoint.path` exists and lists the seven checkpoint steps. For machine-checkpoint runs, `eutonos-install checkpoint show` lists them too.
- Evidence: the checkpoint path and the step IDs you saw.

### Step `remove.entries-reversed`
- Kind: mechanical
- Runs: agent, after the dry-run plan (the owner runs it during the IPC6, IPC8 and IPC10 trials)
- Checkpoint step: `reverse-project-entries`
- Command: part of the removal command. It undoes the project permission entries (`send-message`, `native-send-message`, `codex-sandbox`, `codex-record-rule`) and the `.git/info/exclude` lines recorded for this project, newest first. A line or block that no longer matches what was recorded is kept and reported.
- Done when: the files named in the project's recorded Effects no longer hold the entries, except those listed in `kept_permission_effects`. Read each file back.
- Evidence: the result line "reversed N effect(s); kept M you changed" and the files you read back.

### Step `remove.person-reversals`
- Kind: person
- Runs: owner
- Checkpoint step: `person-reversals`
- Command: none to type. The removal leaves this step `pending` and lists two settings that only the owner can undo; EUTONOS never changes them:
  - Claude Desktop: set this folder's chats back from Auto if the owner no longer wants it.
  - Codex: remove this folder's trust line (`[projects."<project folder>"]` with `trust_level`) from `config.toml` if the owner wants Codex to stop trusting it.
- Done when: you told the owner both, and they said they did it or chose to keep them.
- Evidence: the owner's answer for each. If the checkpoint is the machine one, tick it with `eutonos-install checkpoint record --step person-reversals --state done --evidence "..."`. If the checkpoint is the local file (`checkpoint.used` is `local-json`), no command ticks it; record the answer in the slice check instead.

### Step `remove.adoption-reversed`
- Kind: mechanical
- Runs: agent, after the dry-run plan (the owner runs it during the IPC6, IPC8 and IPC10 trials)
- Checkpoint step: `reverse-adoption`
- Command: part of the removal command with `--de-adopt --plan PLAN.json`. For each path the plan marks `remove`, it copies the file or block to the backup folder first (`<EUTONOS home>/install/backups/remove-<project key>`), then removes it, only while its hash still matches the manifest. A changed file is reported and kept. The manifest `eutonos.adoption.json` goes last, and only when nothing was left over. For detach this step is `skipped` with the reason "detach level".
- Done when: `adoption.outcome` and the lists `removed` and `kept` in the result match the plan; every path in `kept_repository_files` was reported to the owner; the backup folder holds a copy of each removed file. A matching hash alone is no reason to discard native maps or history, so check that no `keep` path was removed.
- Evidence: the result line "removed N, kept M", the backup folder path and the kept files with their reasons. This check is not required for detach.

### Step `remove.device-withdrawn`
- Kind: mechanical
- Runs: agent, after the dry-run plan (the owner runs it during the IPC6, IPC8 and IPC10 trials)
- Checkpoint step: `withdraw-device-repository`
- Command: part of the removal command. It withdraws the root from `device-repositories.json`.
- Done when: `device-repositories.json` no longer lists the root's locator, or the step says "not enrolled".
- Evidence: the result line (for example `withdrawn; revision ...`) and the file you read back.

### Step `remove.unregistered`
- Kind: mechanical
- Runs: agent, after the dry-run plan (the owner runs it during the IPC6, IPC8 and IPC10 trials)
- Checkpoint step: `unregister-project`
- Command: part of the removal command. It removes the root from `<EUTONOS home>/projects.json` while holding the registry lock.
- Done when: `projects.json` has no entry for the root.
- Evidence: the result line `registry: removed` (or `absent` when it was already gone) and the file you read back.

### Step `remove.host-retired`
- Kind: mechanical
- Runs: agent, after the dry-run plan (the owner runs it during the IPC6, IPC8 and IPC10 trials)
- Checkpoint step: `retire-host`
- Command: part of the removal command. It runs last. The host is archived with a `retired.json` marker. An agent never deletes a host.
- Done when: `retired.json` is in the host directory, `admission.json` and the journal are still there, and the host's state reads `retired`.
- Evidence: the result line `host retired: HOST_PATH` and the marker file you read back.

### Step `remove.repository-files`
- Kind: agent
- Runs: agent
- Command: `git -C ROOT status --short` and `git -C ROOT diff --stat`.
- Done when: for detach, the repository tree has no change from the removal (only the owner's own edits). For full de-adoption, the only changed paths are the ones the plan marks `remove`.
- Evidence: the `git status` output compared with the plan.

## Done when (whole removal)

- Every required check is `verified` with evidence, or `blocked` with a stated reason and next action.
- Anything that was kept (changed by you or the owner) was read and reported.
- The owner knows the Desktop Auto and Codex trust settings are theirs to undo.
- Crosstalk is resumed only if the owner wants it.

## Lost context? Resume here

1. Run the dry run again: `eutonos remove-project --root ROOT --dry-run`. It reads live state and says what is left (`registered`, `host_state`, `permission_effects`, `device_repositories`).
2. Read the checkpoint at `checkpoint.path` from the last result, or `eutonos-install checkpoint show` for the machine one. Take the first step that is not `done` or `skipped`.
3. Read the slice's `completion_checks`. Re-read the evidence before the first open check.
4. A `blocked` step: fix the cause it names, then run the same removal command again. It rechecks the registry entry and the adoption hashes and does not repeat finished steps.
5. Do not delete any host, backup or checkpoint by hand.

## Concurrency and locks

- The removal takes the project registry lock (`<EUTONOS home>/projects.lock`) for the whole run, scoped to one root. A busy lock answers "Project registry is busy; try again." Try again.
- The machine lock is taken only inside checkpoint writes. Lock order is fixed: the projects lock first, then the machine lock, never the reverse.
- An uninstall, update, rollback or install running at the same time may delay a checkpoint write for up to 30 seconds.
- After a resume, recheck what each step depends on (the registry entry, the manifest hashes) before you write. The command does this itself and lists it under `rechecked`.
