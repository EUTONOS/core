# Add a repository to EUTONOS

Checklist id: `add-repository`
Checklist revision: ipc5-1

This page ships inside the EUTONOS package (`eutonos/checklists/add-repository.md`). You do not need a EUTONOS checkout to follow it. Add a repository only after EUTONOS is installed on this machine (the `install` page). If you have lost context, read "Lost context?" at the end first.

## How to use this page

Every step has a stable ID, a kind and a runner. The step IDs are the completion-check IDs the slice uses, so one tick-off list serves the page and the slice.

- **mechanical**: a command does the step. The owner runs it during the IPC6, IPC8 and IPC10 trials. At any other time an agent runs it, after showing the owner a dry-run plan.
- **person**: only the owner can do it. You ask, then check the readback yourself.
- **agent**: judgment work. It is done only when its "Done when" holds and you can name the evidence.

Progress is kept in two places:

- **The slice.** One standalone slice per run. Its `completion_checks` are pasted from the snippet below. A check records evidence; it runs nothing. Mark a check `verified` only with a real readback reference. An unfinished check keeps an honest `remaining_action`.
- **The Setup status block.** Module progress (Docs, NAV, RAM) lives in the repository's one "Setup status" block in its AGENTS.md (or its native equivalent). The slice points to that block and does not copy it.

The repository may not have a board until the Docs step creates one. Until then, keep the restart point in the Setup status block, and create the slice as soon as a board owner exists. (`add-project` does not write a setup checkpoint today.)

### Paste into the slice

Print the checks (one line, changes nothing):

`eutonos remove-project --print-checks add`

(`python -I -m eutonos.repository_lifecycle --print-checks add` does the same.) The output is `{"completion_checks": [...]}`. Put the array in the slice's `extensions.completion_checks`. Create the slice with the board tools: `eutonos-boards form --type slice --root ROOT`, then `eutonos-boards batch form`, then `eutonos-boards batch submit --root ROOT --request BATCH.json --host HOST --actor ACTOR` (the command's own help lists the rest; the long procedure is `docs/WORK_LIFECYCLE.md`, which only a EUTONOS source checkout holds). The exact JSON the command prints is:

```json
[{"evidence":[],"id":"add.orient","related_work_ids":[],"remaining_action":"Record the orientation in the slice.","required":true,"state":"pending","title":"Exact root, branch, HEAD and dirty state recorded; repository classified; owners listed."},
{"evidence":[],"id":"add.registered","related_work_ids":[],"remaining_action":"Read back the projects.json entry and the host's admission.","required":true,"state":"pending","title":"add-project registered the root and created or adopted its one host."},
{"evidence":[],"id":"add.adoption-applied","related_work_ids":[],"remaining_action":"Read back eutonos.adoption.json and verify-profile.","required":true,"state":"pending","title":"Adoption profile planned read-only, applied, and recorded in eutonos.adoption.json."},
{"evidence":[],"id":"add.project-entries","related_work_ids":[],"remaining_action":"Read back the project's recorded Effects against the files.","required":true,"state":"pending","title":"Project permission entries and exclude lines applied; Effects recorded on the project."},
{"evidence":[],"id":"add.person-desktop-auto","related_work_ids":[],"remaining_action":"Ask the person; confirm from the newest session record.","required":false,"state":"pending","title":"Desktop Auto mode chosen for this folder (person step)."},
{"evidence":[],"id":"add.person-codex-trust","related_work_ids":[],"remaining_action":"Ask the person; confirm with the Codex trust readback.","required":false,"state":"pending","title":"Codex trust given for this root (person step)."},
{"evidence":[],"id":"add.docs","related_work_ids":[],"remaining_action":"Run the Docs discovery pass, file it, verify.","required":true,"state":"pending","title":"Docs module filed and verified."},
{"evidence":[],"id":"add.nav","related_work_ids":[],"remaining_action":"Build the source map and verify freshness.","required":false,"state":"pending","title":"NAV module filed and verified."},
{"evidence":[],"id":"add.ram","related_work_ids":[],"remaining_action":"File one record and read it back.","required":false,"state":"pending","title":"RAM module filed and read back from a fresh process."},
{"evidence":[],"id":"add.module-boundaries","related_work_ids":[],"remaining_action":"Record each offer in the Setup status block.","required":true,"state":"pending","title":"The person was asked whether to continue at each module boundary."},
{"evidence":[],"id":"add.repository-checks","related_work_ids":[],"remaining_action":"Run the repository's verification commands.","required":true,"state":"pending","title":"The repository's own checks pass."},
{"evidence":[],"id":"add.finalize","related_work_ids":[],"remaining_action":"Plan and apply eutonos-setup finalize.","required":true,"state":"pending","title":"eutonos-setup finalize planned, applied and read back."},
{"evidence":[],"id":"add.acceptance","related_work_ids":[],"remaining_action":"Record the acceptance evidence.","required":true,"state":"pending","title":"Evidence recorded in eutonos.acceptance.json."},
{"evidence":[],"id":"add.setup-status","related_work_ids":[],"remaining_action":"Update the Setup status block; the slice points to it and does not copy it.","required":true,"state":"pending","title":"The Setup status block shows the real state of Docs, NAV and RAM."}]
```

## Order

The install page puts orchestration last, before any repository, so workers can do the reading in parallel. On a dev-channel machine the EUTONOS repository is added first. Beta and stable machines install from a release and need no EUTONOS checkout. One discovery pass by workers feeds Docs, then NAV, then RAM, in that order.

## Steps

### Step `add.orient`
- Kind: agent
- Runs: agent
- Command: `eutonos-setup inspect --root ROOT` and `git -C ROOT status --short --branch`, `git -C ROOT rev-parse HEAD`. `eutonos-setup discover --root ROOT` lists the owners adoption can see.
- Done when: you have written down the exact root, branch, HEAD and dirty state; classified the repository as new, established or previously installed (a missing version marker means unknown, not blank); and listed the existing owners (README, agent entry, current task, docs). Nothing was changed.
- Evidence: the orientation note (root, branch, HEAD, dirty or clean, classification, owner list) saved in the slice.

### Step `add.registered`
- Kind: mechanical
- Runs: agent, after a dry-run plan (the owner runs it during the IPC6, IPC8 and IPC10 trials)
- Command: `eutonos add-project --root ROOT`. Options: `--actor ACTOR`, `--host DIR` (adopt that existing host; a path, or `hosts/NAME` under the EUTONOS home), `--no-permissions` (skip the project permission entries). Running it twice is safe (`already-added`).
- Done when: the result says `added` or `already-added`; `<EUTONOS home>/projects.json` holds an entry for the exact root; and the host directory holds `admission.json` for this root. If the outcome is `ambiguous` (exit 2), several hosts match: pick one and run again with `--host DIR`. Nothing was written in that case.
- Evidence: the `project_key` and the host path from the result.

### Step `add.adoption-applied`
- Kind: mechanical
- Runs: agent, after a dry-run plan (the owner runs it during the IPC6, IPC8 and IPC10 trials)
- Command, in order:
  1. `eutonos-setup profile` prints the profile schema and an example. Write `adoption-profile.json` for this repository: the existing native owners and the modules you want (Docs, delegation, dashboard, RAM, NAV).
  2. `eutonos-setup plan-profile --root ROOT --request adoption-profile.json` is read-only. Save its output as `PLAN.json`. Read the changes and the fingerprint with the owner.
  3. `eutonos-host start --root ROOT --host HOST --attempt ATTEMPT`, then `eutonos-host reserve --root ROOT --host HOST --attempt ATTEMPT --path PATH` once for each path in the plan.
  4. `eutonos-setup apply --root ROOT --host HOST --attempt ATTEMPT --generation GENERATION --effect-id EFFECT_ID --request PLAN.json --fingerprint FINGERPRINT`, with the generation that `start` returned and the fingerprint you reviewed.
  5. `eutonos-setup verify-profile --root ROOT --request adoption-profile.json`.
- Done when: `eutonos.adoption.json` exists in the repository (with a hash for each managed file) and `verify-profile` passes. A native owner that already exists is kept, never overwritten.
- Evidence: the plan fingerprint, the apply result and the `verify-profile` result.

### Step `add.project-entries`
- Kind: mechanical
- Runs: agent, after a dry-run plan (the owner runs it during the IPC6, IPC8 and IPC10 trials)
- Command: `eutonos add-project` already applied these when the install consent is recorded. If its result says `permissions not applied`, follow its `next` text and run `eutonos add-project --root ROOT` again. The project entries are `send-message` (5), `native-send-message`, `codex-sandbox` (12) and `codex-record-rule` (13), plus the `.git/info/exclude` lines.
- Done when: the result says `permissions.applied` is true, and the files named in the recorded Effects hold the entries (the `.claude/settings.local.json`, `.codex/config.toml` and `.codex/rules/eutonos.rules` of this repository, and the exclude lines). Read each file back.
- Evidence: the number of Effects and the files you read back.

### Step `add.person-desktop-auto`
- Kind: person
- Runs: owner
- Command: none to type. Ask the owner: in Claude Desktop, open this folder and choose Auto in the mode selector under the message box (once per project and account). This is project entry 2.
- Done when: the newest Claude session record for this folder shows Auto mode.
- Evidence: the session record you read, or the owner's confirmation if the record cannot be read. This check is not required; if the owner declines, record the reason.

### Step `add.person-codex-trust`
- Kind: person
- Runs: owner
- Command: none to type. Ask the owner: in Codex, open this folder and choose Trust when asked, or add `[projects."<project folder>"]` with `trust_level = "trusted"` to the Codex `config.toml`. This is project entry 14. Until then Codex ignores the project's own settings and rules.
- Done when: the trust line is in `config.toml` or Codex shows the folder as trusted.
- Evidence: the line you read back, or the owner's confirmation. This check is not required; if the owner declines, record the reason.

### Step `add.docs`
- Kind: agent
- Runs: agent (delegate the reading to workers; one brief each, in the background)
- Command: `eutonos-setup inspect --root ROOT` and `eutonos-setup discover --root ROOT` show what exists and what adoption can see (the Docs, then NAV, then RAM order is the order of this page). `docs/DEPLOYMENT_STARTUP.md` (Onboard) and `docs/SETUP.md` hold more detail but ship only in a EUTONOS source checkout. Fill: purpose, architecture, current state, the real verification commands, the sole current task, lessons, and the Experiments backlog. Preserve existing owners and stable IDs.
- Done when: each Docs owner holds real, source-checked text (not a placeholder), the current task is the one active-work board, and the owner files are readable. (`eutonos-setup verify --root ROOT` checks the Docs owners together with NAV and RAM configuration, so it is the final check after NAV, not a Docs-only check.)
- Evidence: the owner files and their sha256 values.

### Step `add.nav`
- Kind: agent
- Runs: agent (delegate; NAV construction uses the NAV runner)
- Command: `eutonos-nav status --root ROOT` shows what exists; build and maintain it with `eutonos-nav start|next|submit --root ROOT --host HOST ...` (the command's own help lists the steps; the long procedure is `docs/DEPLOYMENT_STARTUP.md`, Build and maintain NAV, in a EUTONOS source checkout only). Check freshness with `eutonos-nav maintenance --root ROOT`.
- Done when: NAV has a baseline run and `maintenance` says `no-op` (fresh). This check is not required: the owner may stop after Docs.
- Evidence: the `status` and `maintenance` results. If skipped, record the owner's choice.

### Step `add.ram`
- Kind: agent
- Runs: agent
- Command: file one attributable RAM record, then read it from a fresh process: `eutonos-ram recent --root ROOT`.
- Done when: the record you filed is returned by that fresh process.
- Evidence: the record's stable ID and the readback output. This check is not required. If skipped, record the owner's choice.

### Step `add.module-boundaries`
- Kind: agent
- Runs: agent
- Command: none. At the end of each module (Docs, then NAV, then RAM), tell the owner what is ready, what is not verified, and the benefit of the next module. Ask once whether to continue. Do not ask again at once if an earlier agent already did.
- Done when: each offer and the owner's answer is written in the Setup status block.
- Evidence: the Setup status block lines (module, offer, answer, time).

### Step `add.repository-checks`
- Kind: agent
- Runs: agent
- Command: run the repository's own verification commands (the ones Docs recorded).
- Done when: they pass, or each failure is reported with its cause and the step is `blocked`.
- Evidence: the commands you ran and their results.

### Step `add.finalize`
- Kind: mechanical
- Runs: agent, after a dry-run plan (the owner runs it during the IPC6, IPC8 and IPC10 trials)
- Command: `eutonos-setup finalize --root ROOT` prints a zero-write plan. Review the plan, then apply it with `eutonos-host start`, `eutonos-host reserve` and `eutonos-setup apply` exactly as in `add.adoption-applied`, with the reviewed fingerprint.
- Done when: the apply result says it was applied and `eutonos.installation.json` says `locally-verified`.
- Evidence: the plan fingerprint and the apply result.

### Step `add.acceptance`
- Kind: mechanical
- Runs: agent, after a dry-run plan (the owner runs it during the IPC6, IPC8 and IPC10 trials)
- Command: the finalize apply writes `eutonos.acceptance.json`. Read it back; do not edit it by hand.
- Done when: `eutonos.acceptance.json` names the finalization time, the source fingerprint and the checks that passed. It does not claim more than local verification.
- Evidence: the file path and its sha256, written as `sha256:ABSOLUTE_PATH#HASH`.

### Step `add.setup-status`
- Kind: agent
- Runs: agent
- Command: edit the repository's Setup status block (in its AGENTS.md or native equivalent) through its native owner.
- Done when: the block shows the real state of Docs, NAV and RAM (ready, paused, declined, not applicable, with a reason for each) and the next incomplete module. The slice points to the block and does not copy it.
- Evidence: the block's location and the lines you wrote.

## Done when (whole add)

- Every required check is `verified` with evidence, or `blocked` with a stated reason and next action.
- Optional checks that were skipped say why.
- The Setup status block matches the slice.
- Close the slice out through the repository's normal closeout.

## Lost context? Resume here

1. Run `precheck` on the slice: `eutonos-workflow expert precheck SLICE_ID --root ROOT` (in a EUTONOS source checkout: `python tools/workflow.py expert precheck SLICE_ID --root ROOT`). It shows the checks and their states.
2. Read the slice's `completion_checks`. Take the first check that is not `verified`.
3. Read the repository's Setup status block for the module state.
4. Re-read the evidence of the checks before it; do not trust a `verified` you did not see.
5. `eutonos-setup hosts-report` and `eutonos doctor --json` are read-only and show the host and machine state.

## Concurrency and locks

- Registering a repository takes the project registry lock (`<EUTONOS home>/projects.lock`), scoped to one root. Two adds for the same root cannot both write an entry. A busy lock answers "Project registry is busy; try again." Try again.
- A repository run does not take the machine lock, so it can run while another repository is being added.
- After a resume, recheck the source preconditions (the fingerprint of the plan, the file hashes) before you write. If the source changed, plan again.
