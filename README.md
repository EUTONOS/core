# EUTONOS core, source snapshot 0.5.3

EUTONOS connects people, agents, tools and knowledge with less repeated discovery and coordination. It keeps work
records, navigation maps, a searchable memory of past work and guarded setup in plain files in your own repository,
and offers a local dashboard, a command line and a Windows setup program on top of them.

This repository is a **read-only source snapshot of version 0.5.3** (built from source commit
`2564e1170a25`). It exists so that you, or an AI agent working for you, can read the core source code and
check what EUTONOS does before trusting it. It holds one commit per release, tagged `v0.5.3` for this one.
It is not where development happens, and it takes no pull requests or issues.

- `src/` is the Python package.
- `installer/` is the Windows setup program.
- `tools/` is the build, release and companion tooling.
- `tests/` is the test suite that ships with the source.
- `docs/PRINCIPLES.md` states the principles the design follows.

Some private and paid parts are not in this snapshot; the code here does not depend on them.

## Installing EUTONOS

Do not build from this snapshot. Releases, with the setup program and the update feed, are at
<https://github.com/EUTONOS/releases>.

## Licence

No licence is granted. Copyright (c) 2026 EUTONOS. All rights reserved. You may read this source. You may not copy,
modify, redistribute or use it, in whole or in part, without written permission. See `LICENSE`.
