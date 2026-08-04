"""
promote.py — lanegate promote <env>

Promotion sequence: guard_script → pre_promote → sync (ff-only or merge-no-ff) → post_promote.
- auto-trigger environments are refused by cmd_promote (manual path) but are driven
  automatically by cmd_merge via _auto_promote_environments.
- post_promote only runs when the promote moved commits.
- guard_script exit 1 blocks the promote.
- pre_promote failures block the promote.
- All hooks are executed via run_hook (shell=False, allowlist-enforced).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lanegate.config import ConfigError, resolve_trunk_branch
from lanegate.deploy import run_hook
from lanegate.git import pending_commits


def _exec_hook(argv: list[str], repo_root: Path, label: str, *, fatal: bool = True) -> bool:
    """Run *argv* via run_hook. Returns True on success.

    When *fatal* is True (default) prints an error and returns False on any failure.
    When *fatal* is False, prints a warning and returns False (used for post_promote).
    """
    try:
        run_hook(argv, repo_root, label)
        return True
    except (ConfigError, subprocess.CalledProcessError) as exc:
        prefix = "ERROR" if fatal else "WARNING"
        print(f"{prefix}: {label} failed — {exc}", file=sys.stderr)
        return False


def _run_promotion(env: dict, cfg: dict, repo_root: Path) -> bool:
    """Run guard → pre_promote → sync → post_promote for one environment.

    Returns True on success, False on any blocking failure.
    post_promote failure is non-fatal (warns and still returns True).
    Caller decides whether failure is fatal (cmd_promote exits; cmd_merge warns).
    """
    branch = env["branch"]
    from_branch = env.get("from", resolve_trunk_branch(cfg, repo_root))
    sync_strategy = env.get("sync", "ff-only")
    env_name = env["name"]

    print(f"Promoting {from_branch} → {branch} ({env_name})")

    guard = env.get("guard_script")
    if guard:
        print(f"  Running guard: {guard[0]}")
        if not _exec_hook(guard, repo_root, "guard_script"):
            print("Promote blocked by guard.", file=sys.stderr)
            return False

    pre = env.get("pre_promote")
    if pre:
        print(f"  Running pre_promote: {pre[0]}")
        if not _exec_hook(pre, repo_root, "pre_promote"):
            print("Promote blocked by pre_promote failure.", file=sys.stderr)
            return False

    from lanegate.worktree import worktree_path

    worktrees_dir = repo_root / cfg["worktrees_dir"]
    env_wt = worktree_path(worktrees_dir, branch)

    if not env_wt.exists():
        r = subprocess.run(
            ["git", "worktree", "add", str(env_wt), branch],
            capture_output=True,
            text=True, encoding="utf-8",
            cwd=repo_root,
        )
        if r.returncode != 0:
            r2 = subprocess.run(
                ["git", "worktree", "add", "-b", branch, str(env_wt), from_branch],
                capture_output=True,
                text=True, encoding="utf-8",
                cwd=repo_root,
            )
            if r2.returncode != 0:
                print(f"ERROR creating env worktree:\n{r.stderr}\n{r2.stderr}", file=sys.stderr)
                return False

    pending_result = pending_commits(repo_root, branch, from_branch)
    if not pending_result.ok:
        print(f"ERROR: unable to determine pending commits: {pending_result.error}", file=sys.stderr)
        return False

    pending = pending_result.commits
    if not pending:
        print(f"  {branch} is already up to date with {from_branch} — nothing to promote.")
        return True

    print(f"  {len(pending)} commit(s) to promote:")
    for line in pending[:6]:
        print(f"    {line}")
    if len(pending) > 6:
        print(f"    ... and {len(pending) - 6} more")

    if sync_strategy == "ff-only":
        r = subprocess.run(
            ["git", "merge", "--ff-only", from_branch],
            capture_output=True,
            text=True, encoding="utf-8",
            cwd=env_wt,
        )
    else:  # merge-no-ff
        r = subprocess.run(
            ["git", "merge", "--no-ff", from_branch, "-m", f"Promote {from_branch} → {branch}"],
            capture_output=True,
            text=True, encoding="utf-8",
            cwd=env_wt,
        )

    if r.returncode != 0:
        print(f"ERROR: sync failed:\n{r.stderr}", file=sys.stderr)
        return False

    moved = r.stdout.strip()
    if moved:
        print(f"  {moved}")

    post = env.get("post_promote")
    if post:
        print(f"  Running post_promote: {post[0]}")
        if not _exec_hook(post, repo_root, "post_promote", fatal=False):
            print(
                "WARNING: post_promote failed — promote completed but post-hook failed.",
                file=sys.stderr,
            )

    print(f"Promoted: {from_branch} → {branch} ({env_name})")
    return True


def _auto_promote_environments(cfg: dict, repo_root: Path, from_branch: str) -> None:
    """Called by cmd_merge after a successful merge.

    Iterates environments with trigger==auto whose from matches from_branch and
    runs the full promotion sequence. Failures warn but never propagate — the
    merge already succeeded and must not be unwound.
    """
    for env in cfg.get("environments", []):
        if env.get("trigger") == "auto" and env.get(
            "from", resolve_trunk_branch(cfg, repo_root)
        ) == from_branch:
            env_name = env["name"]
            print(f"  Auto-promoting '{env_name}' (trigger: auto, from: {from_branch})")
            try:
                ok = _run_promotion(env, cfg, repo_root)
                if not ok:
                    print(
                        f"WARNING: auto-promote of '{env_name}' failed — "
                        f"merge succeeded; resolve the environment promotion manually.",
                        file=sys.stderr,
                    )
            except Exception as exc:
                print(
                    f"WARNING: auto-promote of '{env_name}' raised an exception — {exc}",
                    file=sys.stderr,
                )


def cmd_promote(env_name: str, cfg: dict, repo_root: Path) -> None:
    from lanegate import APP_NAME

    envs = cfg.get("environments", [])
    env = next((e for e in envs if e["name"] == env_name), None)

    if env is None:
        available = [e["name"] for e in envs]
        print(f"ERROR: environment '{env_name}' not found. Available: {available}", file=sys.stderr)
        sys.exit(1)

    if env.get("trigger", "manual") == "auto":
        print(
            f"ERROR: environment '{env_name}' is hook-driven (trigger: auto) — "
            f"{APP_NAME} does not promote it. The project's hook handles promotion.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not _run_promotion(env, cfg, repo_root):
        sys.exit(1)
