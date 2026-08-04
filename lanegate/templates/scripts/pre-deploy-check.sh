#!/usr/bin/env sh
# pre-deploy-check.sh — pre-merge / pre-deploy checks (scaffolded by lanegate init)
#
# This is a no-op starter.  Replace the body below with any checks that
# must pass before the ticket branch is merged into main (e.g. lint,
# integration smoke test, environment variable validation).
#
# The script is executed from the ticket worktree directory.
#
# Exit 0  → checks passed (safeguard allows the merge)
# Exit !0 → checks failed (safeguard blocks the merge)

set -e

echo "pre-deploy-check.sh: no checks configured — exiting 0 (no-op starter)"
exit 0
