# /integrate — Merge and Promote

Integration ritual for code-complete tickets.

```bash
# 1. Review what's code_complete
lanegate board

# 2. For each code_complete ticket:
lanegate review TICK-NNN
# Human/none/auto-none only, after inspecting the diff:
lanegate review TICK-NNN --verdict approved
lanegate merge TICK-NNN

# 3. Check pipeline status
lanegate pipeline-status

# 4. Promote to an environment (manual envs only):
lanegate promote <env-name>

# 5. If feature-flagged, enable in the target environment:
lanegate flag enable <flag-name> --env <env-name>
```

`lanegate review` normally launches the configured review agent and waits for its
verdict; it is not a no-cost state transition. If `reviewer` is `human`,
`none`, or `auto-none`, inspect the worktree diff and record the human outcome
with `lanegate review TICK-NNN --verdict approved` before merging. Do not merge a
`changes_requested` ticket; address the findings and re-review. Do not pass an
explicit approved verdict merely to skip a configured reviewer.


For auto-trigger environments (`trigger: auto`), lanegate automatically promotes them during `lanegate merge` when `from` matches the merge target branch — no external hook required.
