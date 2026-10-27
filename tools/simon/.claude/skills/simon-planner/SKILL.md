---
name: simon-planner
description: Operate Windows desktop applications through Simon Helper (shipped with EUTONOS) with grounded observations, bounded actions and verified results, when native computer use fails or is blocked. Prefer direct file or API operations when they fit the task.
---

Read the Simon Quickstart before desktop work. It owns the operating procedure, worked transcript, result interpretation, Save pattern and lifecycle checks. In an installed EUTONOS it is `QUICKSTART.md` beside this skill file; in a EUTONOS source checkout it is `docs/Simon/QUICKSTART.md` from the repository root.

Run Simon as `eutonos-simon <command>` (the EUTONOS launcher; it refuses visibly when the Simon part is not installed or is turned off). In a source checkout, `python helperctl.py <command>` from `tools/simon` is the same. Use `eutonos-simon helper.commands --detail full` for the current argument schemas instead of inventing flags.

Use fresh candidate IDs or grid labels. Planner images are resized for reading and permit label targeting only. Inspect `outcome`, failure evidence and verification before continuing; stop on uncertain capture/focus, after two meaningfully different failed paths, or when the user halts. At session end, run `eutonos-simon helper.exit --via-app`, verify Helper exit and clean up only resources created for the task. Respect the user's task scope and existing authorization.
