# /integrate — Merge and Promote

Integration ritual for code-complete tickets.

```bash
# 1. Review what's code_complete
lanegate board

# 2. For each code_complete ticket:
lanegate review TICK-NNN
lanegate merge TICK-NNN

# 3. Check pipeline status
lanegate pipeline-status

# 4. Promote to an environment (manual envs only):
lanegate promote <env-name>

# 5. If feature-flagged, enable in the target environment:
lanegate flag enable <flag-name> --env <env-name>
```

For auto-trigger environments (`trigger: auto`), lanegate automatically promotes them during `lanegate merge` when `from` matches the merge target branch — no external hook required.
