# Repair EUTONOS after an app update (local patch)

Checklist id: `repair`
Checklist revision: repair-1

This page ships inside the EUTONOS package (`eutonos/checklists/repair.md`). You do not need a EUTONOS checkout to read it, but the fix itself is made in one. Use it when a Claude, Codex or Hermes update (or anything else) broke part of EUTONOS and the owner must keep working before an official release. If you have lost context, read "Lost context?" at the end first.

## How to use this page

Every step has a stable ID, a kind and a runner, as on the install page.

- **agent**: judgment work, or a command you run and read back. It is done only when its "Done when" holds and you can name the evidence.
- Nothing on this page needs a person to type or click, except the app restarts the install checkpoint lists after a patch (`restart-apps`).

The setup checkpoint is the tick-off list (`eutonos-install checkpoint show`). A patch is an ordinary update, so it writes the `install` checkpoint with `restart-apps` pending; the steps below are yours to keep in your own notes or in a `repair` checkpoint.

Notes on commands:

- `eutonos patch|update|rollback|self-check|doctor` go through the `eutonos` command; `status` and `checkpoint` are only on `eutonos-install` (the same program).
- `--home` is only accepted before the verb: `eutonos-install --home H patch ...`.
- A patch changes the owner's real EUTONOS install. Show the owner the dry-run plan first, unless the owner already told you to go ahead.
- Never hand-edit files outside the repository (allow rules, Codex rules, instruction blocks in `CLAUDE.md`, `AGENTS.md` or Hermes' `SOUL.md`, Startup items, app settings). Declare the change in the repository (`src/eutonos/permissions.json` or a component manifest) so the installer applies, records and can remove it.
- Never open credential or secret files (`.credentials.json`, `auth.json`, `*.key`, `.env`, cookies). App versions come from the apps' own `--version`; the Codex desktop app's version comes from its installed Microsoft Store package (a read of the package list, no account file).

## Repair steps

### Step `self-check`
- Kind: agent
- Runs: agent
- Command: `eutonos-install self-check` (read-only; add `--json` for the full report). It compares the Claude, Codex (the `codex` command and the Codex Store app, each on its own) and Hermes versions recorded at the last install or update with the ones readable now, and adds Crosstalk's halt and messaging state, the declared permission rules, and the component and effect readbacks.
- Done when: you have the report's `outcome`, `likely_broken` (plain words) and `action` (`patch`, `rollback`, `wait`, `resume` or `install`).
- Evidence: the `likely_broken` line and the `action`. Keep the app versions it names (recorded and now).

### Step `decide`
- Kind: agent
- Runs: agent
- Command: none. Choose one:
  - `patch`: an app changed and a piece EUTONOS relies on stopped working. Rolling EUTONOS back will not undo the app's change. Go on to `fix-in-checkout`.
  - `rollback`: nothing app-side changed and the newest EUTONOS generation is the likelier cause. Run `eutonos-install rollback --dry-run`, show the owner, then `eutonos-install rollback` (from a local patch use `eutonos-install rollback --official`). Then stop; restart the apps.
  - `wait`: Crosstalk is halted or messaging is off (owner controls), a setting could not be read back, or only a note was reported. Tell the owner and wait. Do not patch around an owner stop.
  - `resume`: an earlier run did not finish. Run `eutonos-install status`, inspect what it names, then `eutonos-install resume`.
- Done when: the choice is made and you can say why in one sentence.
- Evidence: the decision and the reason.

### Step `fix-in-checkout`
- Kind: agent
- Runs: agent
- Command: none. Edit a checkout of the EUTONOS repository, on its own branch, and commit. Make the smallest change that fixes the named piece. Check it with `python tools/test_shards.py 1 --only TEST_NAMES` (focused tests; the whole suite is not needed for a local patch). Commit everything that goes into the wheel (`src`, `tools`, `pyproject.toml`): a patch from uncommitted changes needs `--allow-dirty` and then records `dirty: true`, which also stops the later "included" proof.
- Done when: the checkout is clean at one commit and the focused tests pass.
- Evidence: the commit ID and the focused test result.

### Step `dry-run`
- Kind: agent
- Runs: agent
- Command: `eutonos-install patch --checkout PATH --reason "TEXT" --dry-run`. The reason says what is broken and what the patch fixes (one line). It builds the wheel in a scratch folder and writes nothing under the EUTONOS home.
- Done when: the outcome is `dry-run`, and `local_patch.changed_files` is the list you expect (only your fix) with `base_release` naming the installed official build.
- Evidence: the changed-file list and the source commit. A refusal (`dirty-checkout`, `build-failed`, `pending`, `busy`) names its cause; fix that and run again.

### Step `patch`
- Kind: agent
- Runs: agent
- Command: `eutonos-install patch --checkout PATH --reason "TEXT"` (the same command without `--dry-run`). It installs the wheel as a new generation beside the current one; a failed patch leaves the earlier generation active.
- Done when: the outcome is `patched` and `eutonos-install status` shows `local_patch_line` (`local patch active: REASON (commit X)`).
- Evidence: the new generation ID and the status line. Then do the `restart-apps` step in the install checkpoint (the owner restarts the apps).

### Step `verify`
- Kind: agent
- Runs: agent
- Command: `eutonos-install self-check` again, `eutonos doctor --json`, and one Crosstalk letter by hand (with the Crosstalk send tool, to a chat of another app, then read the reply back; check `eutonos-crosstalk status` first and never resend a letter reported accepted, held or uncertain).
- Done when: the piece the first self-check named is no longer reported as broken, doctor's `local_patch_line` is present, and the hand-sent letter arrived and was answered.
- Evidence: the new `likely_broken` (empty) and the letter's arrival and reply readback. If it is still broken, either make a further patch (go back to `fix-in-checkout`) or `eutonos-install rollback --official` and tell the owner.

### Step `tell-user`
- Kind: agent
- Runs: agent
- Command: none. Tell the owner in plain words: what broke, which app update it followed, what the patch changed (the changed-file list), how to undo it (`eutonos-install rollback --official`), and that the next official update replaces the patch.
- Done when: the owner has the four points above.
- Evidence: what you told the owner.

### Step `confirm-official`
- Kind: agent
- Runs: agent
- Command: later, when an official release appears: `eutonos update --dry-run` shows `replaces_local_patch` with a `fix` verdict. After `eutonos update`, the result's `replaced_local_patch.fix` is `included` only when git proves the patch's commit is an ancestor of the release's source commit; otherwise it says `unknown - check the release notes`. Read the notes, then run `eutonos-install self-check` and test one letter by hand.
- Done when: either the verdict is `included` and the self-check is clean, or you re-tested the broken piece on the official build and it works (or you made a new patch).
- Evidence: the verdict text and the fresh self-check result.

## Done when (whole repair)

- The piece the self-check named works again (second self-check and a hand-sent letter agree).
- The owner knows what changed and how to undo it.
- Once an official release has replaced the patch, the verdict was read and the piece re-tested.

## What a local patch is

- A new generation, built from your checkout by the same wheel builder as a release, installed through the same update path. Its record carries `local_patch`: `reason`, `checkout`, `source_commit`, `base_release`, `dirty`, `diff_sha256` and a short `changed_files` list.
- `eutonos-install rollback` goes back to the generation before it, as for any update. `eutonos-install rollback --official` does so only when that generation is an official build; otherwise it refuses and says to run `eutonos-install update`.
- `eutonos update` proceeds normally: the official build replaces the patch, and the result says whether the fix is included.
- `eutonos-install uninstall` works unchanged.

## Lost context? Resume here

1. `eutonos-install status` shows the active generation, any pending run, and `local_patch_line` when a patch is active.
2. `eutonos-install self-check` says what changed in the apps and what EUTONOS piece likely broke.
3. `eutonos doctor --json` shows the declared items and the active record's `local_patch`.
4. If a patch run was interrupted, `status` shows `pending`; inspect the targets it names, then `eutonos-install resume`.
5. Do the first step above that you cannot show evidence for.

## Concurrency and locks

- One machine lifecycle operation at a time (install, update, patch, uninstall, rollback), held by the machine lock under `<home>/install/`. A second one answers `busy`. Wait for the first to finish; do not delete the lock.
- `self-check`, `status` and `doctor` only read and never take the lock.
