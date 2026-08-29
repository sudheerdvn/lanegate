# Git hooks

Enable the repository hooks with:

```bash
git config core.hooksPath .githooks
```

The size ratchet checks staged Python files. Use `git commit --no-verify` only for an intentional emergency escape hatch.

## Transitional exceptions (TICK-700 aftermath)

The ratchet is grow-only: it blocks *any* line growth of a file already over
1200 lines. A handful of tickets were implemented and reviewed **before** the
ratchet landed and append a few lines to a still-oversized second-tier module:

- **TICK-071** → `orchestrate/pool.py` (+7)
- **TICK-565** → `orchestrate/review.py` (+29), `orchestrate/run_report.py` (+59)
- **TICK-708** → `orchestrate/pool.py` (+24), `tests/orchestrate/test_pool.py` (+18) —
  isolation-leak fix; too load-bearing to defer behind the split.

These were committed with the hook bypassed. The one CI run that introduces
each growth will go red on `check_file_size.py --against origin/main`; the
baseline catches up on the next push. **TICK-706** splits pool.py / review.py /
run_report.py and clears the underlying debt — no further exceptions after that.
