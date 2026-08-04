# Migrating an existing repo from Vyuha to LaneGate

TICK-385 renamed the project from Vyuha to LaneGate: the Python package is now `lanegate`, the CLI binary is `lanegate` (was `vyuha`), and the config filename/state directory are derived from `APP_NAME = "lanegate"` (see `lanegate/__init__.py` and `lanegate/config.py`). If you initialized a repo with an older `vyuha` install, its on-disk state still uses the old names. LaneGate doesn't rewrite another tool's files automatically, so moving them over is a one-time manual step.

## What to move

1. **Config file**: rename `.vyuha.yml` to `.lanegate.yml`.

   ```sh
   git mv .vyuha.yml .lanegate.yml
   ```

2. **State directory**: rename `.vyuha/` to `.lanegate/`. This carries your tickets, worktree metadata, notes, logs, and locks: anything under the config-filename-derived path scheme described in `docs/config-reference.md`.

   ```sh
   git mv .vyuha .lanegate
   ```

   If `tickets_dir` or `worktrees_dir` in your config point somewhere other than the default `.vyuha/tickets` / `.vyuha/worktrees`, move those paths instead and update the corresponding keys in `.lanegate.yml`.

3. **`.gitignore`**: update any `.vyuha/*`, `.vyuha.yml`, or `vyuha-context-log.jsonl` entries you added by hand to their `.lanegate` equivalents. (Entries LaneGate itself manages via `lanegate init` are regenerated automatically. See `_gitignore_entries()` in `lanegate/config.py`.)

4. **Worktrees**: if you have active `git worktree` checkouts under the old `worktrees_dir`, either finish or re-create them after the move. Worktree paths recorded by git won't follow a manual directory rename.

## What you don't need to touch

- **CLI usage**: swap `vyuha <cmd>` for `lanegate <cmd>` in your own scripts/aliases. The command set and flags are unchanged.
- **PyPI package**: `pip install lanegate` replaces `pip install vyuha`. The old `vyuha` distribution is not being updated further. Publishing the new `lanegate` package to PyPI is a separate manual step by the maintainer, not part of this migration.
- **Ticket content**: ticket Markdown files themselves (frontmatter, body) are unchanged by the rename. Only the directory and config-file names around them moved.

## Why this isn't automatic

LaneGate derives its config filename and state directory from a single `APP_NAME` constant (`f".{APP_NAME}.yml"`, `f".{APP_NAME}/..."`), so a fresh `lanegate init` always produces `.lanegate.yml` / `.lanegate/`. Moving an *existing* repo's state is a filesystem operation on files LaneGate doesn't own until you point it at them, so it's left as an explicit, reviewable step rather than something the CLI does silently on first run.
