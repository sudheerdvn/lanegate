# Contributing to LaneGate

Thanks for considering a contribution. This is currently a solo-maintained project, so the process is intentionally lightweight.

## Before you start

For anything beyond a small fix, open an issue or discussion first describing the change — it saves both of us time if the direction needs adjusting before code gets written.

## Developer Certificate of Origin (DCO)

Every commit must include a `Signed-off-by` trailer, which you add automatically with:

```bash
git commit -s -m "your commit message"
```

This certifies you wrote the change (or otherwise have the right to submit it) under the terms below, and that you're contributing it under this project's license (Apache-2.0, see [LICENSE](LICENSE)). It's the standard [Developer Certificate of Origin](https://developercertificate.org/):

```
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.
1 Letterman Drive, Suite D4700, San Francisco, CA, 94129

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.

Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

Pull requests with unsigned commits won't be merged until they're signed off — if you forgot, `git commit --amend -s` (or `git rebase --signoff <base>` for multiple commits) fixes it retroactively.

## Dev setup

```bash
pip install -e ".[dev]"
python3 -m pytest tests/ -q
```

Most tests are fast unit-style checks and mock git-facing subprocess calls where needed to isolate edge cases. `tests/test_e2e_lifecycle.py` is the real lifecycle integration suite: it creates a temporary git repository with `git init`, makes real commits, creates a real linked worktree, runs the ticket from draft through done, and verifies actual branch, merge, and worktree state. That suite still runs under the default `pytest` invocation, no separate setup needed. Only the model response is stubbed, to avoid nondeterminism and external cost.

The release-artifact gate builds a wheel/sdist in a temporary copy, uses a clean
non-editable installation, and exercises the CLI in throwaway repositories:

```bash
python -m pip install build
python ci/smoke_release.py
```

See [the release smoke-gate guide](docs/release-smoke-gate.md) for its seven
checks and the currently expected first-promotion failure in CI.

## Security issues

Don't open a public issue for a vulnerability — see [SECURITY.md](SECURITY.md) for private reporting instructions.

## Pull requests

- Keep the diff scoped to one change; unrelated cleanup makes review harder, not easier.
- Add or update tests for behavior changes.
- Describe *why*, not just *what*, in the PR description — the diff already shows what changed.
