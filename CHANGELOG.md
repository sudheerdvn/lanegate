# Changelog

All notable changes to LaneGate are logged here. Dates are the day a change merged to `main`.

## v1.0.0 (2026-08-03) — first public release

### Renamed from Vyuha to LaneGate

The project was renamed to LaneGate before this release (TICK-384/385) after a direct
domain/naming conflict with an unrelated company. The Python package is now `lanegate`,
the CLI is `lanegate` (short alias `lgt`), and the config filename/state directory follow
from that (`.lanegate.yml`, `.lanegate/`). If you have an existing repo initialized with
an older `vyuha` install, see `docs/migration-vyuha-to-lanegate.md` for the one-time
manual move.

### Defaults changed from earlier development

Two behaviors that used to require opt-in are now the default for new `lanegate init`
projects:

- **Independent review by default.** Review no longer silently self-reviews; it runs a
  genuinely separate reviewer (a different tool instance, a different model, or a
  different account, in that preference order) before falling back to self-review only
  when nothing else is available.
- **Ticket evidence is git-tracked by default.** Ticket files land under version control
  out of the box instead of being gitignored, so ticket history survives the same way
  the rest of the repo's history does.

### Known gaps not yet run — read before relying on this release

This release has **not** had the following checks run against it. They don't block
day-to-day use, but you should know they're outstanding rather than assume full coverage:

- **No `mypy` pass has been run against the tree** (tracked as TICK-365, deferred to v1.6).
- **No systematic sweep for drifted duplicate logic has been run.** A handful of instances
  were found incidentally during development and fixed as they were found, but a
  deliberate grep-driven sweep across module boundaries has not happened (tracked as
  TICK-366, deferred to v1.6, and should run *before* 365).

A few smaller items are also explicitly deferred to v1.6 and tracked as open tickets
(TICK-378, TICK-379, TICK-383) — none are launch blockers, all are pre-existing scope
decisions, not omissions discovered late.
