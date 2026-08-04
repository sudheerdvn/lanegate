# Changelog

All notable changes to LaneGate are logged here. Dates are the day a change merged to `main`.

## v1.0.3 (2026-08-04): fixture redaction, docs

- A test fixture (`tests/fixtures/captured_output/tick-349-nested-brace-review.txt`) was a
  real captured Claude Code session transcript: real session IDs, timestamps, cost data, and
  internal file paths. The test only needs one JSONL line, so replaced the 162-line real
  transcript with a small synthetic one carrying the same structural shape.
- README now documents the `treesitter` optional extra (`pip install "lanegate[treesitter]"`)
  for non-Python projects — without it, analyze's non-Python symbol matching silently falls
  back to plain ripgrep text search, which is less precise and pushes more exploration (and
  token cost) onto the analyzing agent. Python-only projects don't need it. This extra already
  shipped in earlier versions; it just wasn't documented.

## v1.0.2 (2026-08-04): docs cleanup

- Removed a migration doc that referenced this project's working name before its first
  public release. It didn't apply to anyone: there was no prior public release to migrate
  from.

## v1.0.1 (2026-08-03): Windows encoding fix

- Explicit `encoding="utf-8"` on the remaining ~85 `subprocess.run`/`Popen` call sites that
  still defaulted to `locale.getpreferredencoding()`, which mangles non-ASCII output on a
  default Windows setup. `git.py` was already fixed as part of the earlier Windows CI pass,
  and this closes out the rest of the tree.
- Added test coverage for two previously-untested modules: `lanegate/ghsync.py` (GitHub
  Issues mirror) and `lanegate/agent_tools.py` (Claude/Codex/MCP installer).

## v1.0.0 (2026-08-03): first public release

### Defaults changed from earlier development

Two behaviors that used to require opt-in are now the default for new `lanegate init`
projects:

- **Independent review by default.** Review no longer silently self-reviews. It runs a
  genuinely separate reviewer (a different tool instance, a different model, or a
  different account, in that preference order) before falling back to self-review only
  when nothing else is available.
- **Ticket evidence is git-tracked by default.** Ticket files land under version control
  out of the box instead of being gitignored, so ticket history survives the same way
  the rest of the repo's history does.

### Known gaps not yet run (read before relying on this release)

This release has **not** had the following checks run against it. They don't block
day-to-day use, but you should know they're outstanding rather than assume full coverage:

- **No `mypy` pass has been run against the tree** (tracked as TICK-365, deferred to v1.6).
- **No systematic sweep for drifted duplicate logic has been run.** A handful of instances
  were found incidentally during development and fixed as they were found, but a
  deliberate grep-driven sweep across module boundaries has not happened (tracked as
  TICK-366, deferred to v1.6, and should run *before* 365).

A few smaller items are also explicitly deferred to v1.6 and tracked as open tickets
(TICK-378, TICK-379, TICK-383). None are launch blockers. All are pre-existing scope
decisions, not omissions discovered late.

- **`lanegate flag` has only been unit-tested, not run end-to-end against a real deploy.**
  `tests/test_flags.py` covers the read-modify-write logic against temp paths, but no real
  project has a `.lanegate.yml` `environments:`/`flag_file` setup with an actual deploy hook
  reading the result. Tracked as TICK-387, deferred to v1.6.
