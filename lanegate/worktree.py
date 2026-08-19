"""
worktree.py — git worktree create/remove with protected-branch guard.

Protected branches (environment branches from config) are refused for remove/prune.
Worktree dir and branch name both use lowercase ticket ID to avoid case-mismatch on
case-insensitive filesystems (macOS, Windows).
"""

from __future__ import annotations

import subprocess
import warnings
from pathlib import Path

from lanegate.config import resolve_trunk_branch


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", cwd=cwd)


def worktree_path(worktrees_dir: Path, ticket_id: str) -> Path:
    """Canonical worktree path — always lowercase to avoid case-mismatch bugs."""
    return worktrees_dir / ticket_id.lower()


def _local_branch_exists(repo_root: Path, branch: str) -> bool:
    """Return whether *branch* exists as a local branch, never merely a tag.

    ``rev-parse --verify <name>`` accepts every revision namespace.  In
    particular, a same-named tag makes it look as though an unattached ticket
    branch exists, even though attaching it would create a detached worktree.
    Ticket worktrees must only ever reuse or delete ``refs/heads``.
    """
    return _run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        repo_root,
    ).returncode == 0


def _registered_worktree_branch(repo_root: Path, path: Path) -> tuple[bool, str | None]:
    """Return (registered, branch) for *path* per git's own worktree registry.

    ``registered`` is True only when git itself recognizes *path* as a linked
    worktree (i.e. it was actually produced by ``git worktree add``) -- never
    merely because a directory happens to exist there. A directory dropped in
    by some other means (crash artifact, untrusted content) is never
    registered, no matter what it contains. ``branch`` is the branch
    currently checked out in that worktree, or None if it's detached, bare,
    or unregistered.
    """
    result = _run(["git", "worktree", "list", "--porcelain"], repo_root)
    if result.returncode != 0:
        return False, None
    try:
        target = path.resolve()
    except OSError:
        target = path
    for block in result.stdout.split("\n\n"):
        lines = block.splitlines()
        if not lines or not lines[0].startswith("worktree "):
            continue
        raw_path = lines[0][len("worktree "):].strip()
        try:
            block_path = Path(raw_path).resolve()
        except OSError:
            block_path = Path(raw_path)
        if block_path != target:
            continue
        branch = None
        for line in lines[1:]:
            if line.startswith("branch refs/heads/"):
                branch = line[len("branch refs/heads/"):].strip()
                break
        return True, branch
    return False, None


def _worktree_is_detached(path: Path) -> bool:
    """Return whether Git reports detached HEAD for an existing worktree."""
    head = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], path)
    return head.returncode == 0 and head.stdout.strip() == "HEAD"


def _ensure_graphify_symlink(repo_root: Path, path: Path) -> None:
    graphify_out = repo_root / "graphify-out"
    if not graphify_out.is_dir():
        return
    r = _run(["git", "check-ignore", "-q", "graphify-out"], path)
    if r.returncode != 0:
        return
    wt_graphify = path / "graphify-out"
    if not (wt_graphify.exists() or wt_graphify.is_symlink()):
        try:
            wt_graphify.symlink_to(graphify_out, target_is_directory=True)
        except OSError:
            pass


def _ensure_notes_symlink(repo_root: Path, path: Path) -> None:
    """Expose the control checkout's durable notes store in *path*.

    Ticket worktrees are disposable.  A regular ``.lanegate/notes`` directory
    in one would therefore make its contents invisible to the control checkout
    and delete them with the worktree.  When symlinks are unavailable (notably
    on Windows without the required privilege), when git doesn't ignore
    ``.lanegate/notes`` in this repo (so planting a symlink there would shadow
    a tracked path), or when a real directory already occupies the spot, fall
    back to a private store so worktree creation can still proceed.
    """
    notes_root = repo_root / ".lanegate" / "notes"
    notes_root.mkdir(parents=True, exist_ok=True)
    wt_state = path / ".lanegate"
    wt_state.mkdir(parents=True, exist_ok=True)
    wt_notes = wt_state / "notes"

    if wt_notes.is_symlink():
        try:
            if wt_notes.resolve() == notes_root.resolve():
                return
        except OSError:
            pass
        raise RuntimeError(f"worktree notes link points outside the control store: {wt_notes}")
    if wt_notes.exists():
        if not wt_notes.is_dir():
            raise RuntimeError(
                f"worktree notes path is not the required shared link: {wt_notes}. "
                "Move it aside before retrying."
            )
        warnings.warn(
            f"worktree notes path is not the required shared link: {wt_notes}; "
            "using a worktree-private notes directory",
            RuntimeWarning,
            stacklevel=2,
        )
        return

    r = _run(["git", "check-ignore", "-q", ".lanegate/notes"], path)
    if r.returncode != 0:
        warnings.warn(
            f"{wt_notes} is not ignored by git; using a worktree-private notes directory",
            RuntimeWarning,
            stacklevel=2,
        )
        wt_notes.mkdir(parents=True, exist_ok=True)
        return

    try:
        wt_notes.symlink_to(notes_root, target_is_directory=True)
    except OSError as exc:
        warnings.warn(
            f"could not create shared notes link at {wt_notes}: {exc}; "
            "using a worktree-private notes directory",
            RuntimeWarning,
            stacklevel=2,
        )
        wt_notes.mkdir(parents=True, exist_ok=True)
        return
    try:
        linked_target = wt_notes.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"could not validate shared notes link at {wt_notes}: {exc}") from exc
    if not wt_notes.is_symlink() or linked_target != notes_root.resolve():
        raise RuntimeError(f"shared notes link does not target the control store: {wt_notes}")


def create_worktree(
    repo_root: Path,
    worktrees_dir: Path,
    ticket_id: str,
    branch: str,
    base: str | None = None,
    *,
    reuse_existing_branch: bool = False,
    protected: set[str] | None = None,
) -> Path:
    """
    Create a worktree for ticket_id on branch. Both dir name and branch name are lowercase.

    ``reuse_existing_branch`` is reserved for an explicit recovery path.
    ``protected`` comes from the trusted control-checkout configuration; it
    prevents a stale canonical path from being used to remove an environment
    worktree. Fresh dispatches fail rather than silently reusing an unattached
    branch, which might contain rejected or otherwise recovery-only work.
    Returns the worktree path.
    """
    base = base or resolve_trunk_branch({}, repo_root)
    path = worktree_path(worktrees_dir, ticket_id)
    replacing_canonical_worktree = path.exists()
    # Whether the directory we're about to tear down is actually registered
    # with git as a linked worktree -- not merely a directory that happens to
    # occupy the canonical path (e.g. an unregistered leftover dir dropped in
    # by a crash or untrusted content). Only a confirmed git-registered
    # worktree can "release" a same-named branch when it's removed;
    # otherwise that branch must be treated as unattached recovery evidence,
    # same as if no directory existed at all. Checked via git's own registry
    # (not the currently checked-out HEAD branch) so a worktree that was
    # later detached is still recognized as having legitimately owned this
    # canonical slot.
    worktree_registered = False
    worktree_branch: str | None = None
    if path.exists():
        worktree_registered, worktree_branch = _registered_worktree_branch(repo_root, path)
        branch_exists = _local_branch_exists(repo_root, branch)
        # A plain (non-directional) merge-base: confirms *branch* shares
        # history with *base* at all, rather than requiring base's current
        # tip to be an ancestor of branch. The latter (--is-ancestor) breaks
        # the instant trunk gains a single commit the ticket branch doesn't
        # have -- true for essentially every ticket branch shortly after it's
        # cut -- which made every resume/reattach fail once other tickets
        # merged. This only guards against a branch with genuinely disjoint,
        # unrelated history (e.g. an orphan branch reusing this name).
        ancestry_check = (
            _run(["git", "merge-base", base, f"refs/heads/{branch}"], repo_root)
            if branch_exists
            else None
        )

        is_detached = _worktree_is_detached(path)
        attached_expected = worktree_registered and worktree_branch == branch and not is_detached
        ancestry_ok = ancestry_check is not None and ancestry_check.returncode == 0
        valid = worktree_registered and attached_expected and branch_exists and ancestry_ok
        if valid:
            _ensure_graphify_symlink(repo_root, path)
            _ensure_notes_symlink(repo_root, path)
            return path

        # A registered worktree at the canonical path must be attached to this
        # ticket branch before it is disposable. A different branch and a
        # detached HEAD are both recovery/foreign-work evidence: Git's
        # registry cannot prove a detached checkout belongs to this ticket.
        # Removing it would also destroy someone else's checkout before we
        # discover the target ticket branch is an unattached recovery branch.
        if worktree_registered and not attached_expected:
            if worktree_branch in (protected or set()):
                raise PermissionError(
                    f"Refusing to remove worktree on protected environment branch "
                    f"'{worktree_branch}'"
                )
            actual = f"branch '{worktree_branch}'" if worktree_branch else "detached HEAD"
            raise RuntimeError(
                f"ERROR: Canonical worktree path '{path}' is registered on {actual}, "
                f"not ticket branch '{branch}'; preserving it."
            )

        # The worktree IS correctly attached to this ticket's branch -- the
        # only reason it's invalid is a failed ancestry check. Decide to
        # refuse *before* destroying it: this is live, attached work, not
        # disposable recovery evidence, and force-removing it first (as a
        # prior version of this function did) would delete a worktree that
        # then turns out could not be recreated either.
        if worktree_registered and attached_expected and branch_exists and not ancestry_ok:
            raise RuntimeError(
                f"ERROR: Existing worktree at '{path}' is on branch '{branch}', but that branch "
                f"shares no history with '{base}'; preserving it. Inspect or explicitly recover "
                "it before retrying."
            )

        remove_worktree(repo_root, path, protected or set())
        if path.exists():
            import shutil

            shutil.rmtree(path, ignore_errors=True)
        if path.exists():
            raise RuntimeError(f"ERROR: Stale worktree directory at {path} could not be removed.")

        # A branch that does not descend from the requested base is recovery
        # evidence, not a candidate for hibernation restore.  It is important
        # to check this *after* detaching the invalid worktree as well: the
        # old implementation removed the path and then immediately reattached
        # the same rejected branch when reuse_existing_branch was true.
        if branch_exists and not ancestry_ok:
            raise RuntimeError(
                f"ERROR: Existing branch '{branch}' was preserved because it shares no history "
                f"with '{base}'; inspect or explicitly recover it before retrying."
            )

    # A branch without its canonical worktree can be intentional recovery
    # evidence (for example hibernate --reset after a truncated capture).  Do
    # not silently discard or reuse it.  A branch that occupied a *confirmed
    # git-registered* stale canonical worktree (worktree_registered), on the
    # other hand, belongs to that worktree slot and is safe to recreate for a
    # fresh dispatch -- ticket worktrees are always created with `-b branch`,
    # so a genuine former worktree at this path was always for this branch,
    # regardless of whether it was later left detached. A directory that
    # merely occupied the canonical path -- without git recognizing it as a
    # registered worktree at all -- must not count as "released"; the branch
    # is preserved as recovery evidence instead.
    if _local_branch_exists(repo_root, branch):
        if reuse_existing_branch:
            ancestry_check = _run(
                ["git", "merge-base", base, f"refs/heads/{branch}"], repo_root
            )
            if ancestry_check.returncode != 0:
                raise RuntimeError(
                    f"ERROR: Existing branch '{branch}' was preserved because it shares no history "
                    f"with '{base}'; inspect or explicitly recover it before retrying."
                )
            # Attach via the fully-qualified ref, not the bare branch name: git's
            # revision disambiguation prefers refs/tags/ over refs/heads/ for an
            # ambiguous short name, which would silently check out a same-named
            # tag's commit in detached HEAD instead of the actual ticket branch.
            r = _run(["git", "worktree", "add", str(path), f"refs/heads/{branch}"], repo_root)
            if r.returncode != 0:
                raise RuntimeError(f"ERROR restoring worktree:\n{r.stderr}")
            signoff = _run(["git", "config", "format.signoff", "true"], path)
            if signoff.returncode != 0:
                raise RuntimeError(f"ERROR configuring DCO sign-off in worktree:\n{signoff.stderr}")
            _ensure_graphify_symlink(repo_root, path)
            _ensure_notes_symlink(repo_root, path)
            return path
        # Control only reaches here with worktree_registered == False: every
        # path above with worktree_registered == True either returned early
        # (valid) or raised (wrong branch/detached, or attached-but-failed-
        # ancestry). An unattached branch found this way is preserved as
        # recovery evidence UNLESS it merely occupies the slot of a
        # confirmed git-registered worktree we just released above (a fresh,
        # non-recovery dispatch reclaiming its own canonical path) -- in that
        # one case the stale same-named branch is safe to force-delete so a
        # fresh worktree/branch can be created below.
        if not (replacing_canonical_worktree and worktree_registered):
            raise RuntimeError(
                f"ERROR: Existing unattached branch '{branch}' was preserved; "
                "inspect or explicitly remove it before starting a fresh dispatch."
            )
        _run(["git", "branch", "-D", branch], repo_root)
        if _local_branch_exists(repo_root, branch):
            raise RuntimeError(f"ERROR: Stale branch '{branch}' exists and could not be deleted.")

    # Try to create branch from base
    r = _run(["git", "worktree", "add", "-b", branch, str(path), base], repo_root)
    if r.returncode != 0:
        raise RuntimeError(f"ERROR creating worktree:\n{r.stderr}")
    signoff = _run(["git", "config", "format.signoff", "true"], path)
    if signoff.returncode != 0:
        raise RuntimeError(f"ERROR configuring DCO sign-off in worktree:\n{signoff.stderr}")
    _ensure_graphify_symlink(repo_root, path)
    _ensure_notes_symlink(repo_root, path)
    return path


def remove_worktree(
    repo_root: Path,
    wt_path: str | Path,
    protected: set[str],
    *,
    expected_branch: str | None = None,
) -> None:
    """
    Remove a worktree. Refuses if its branch is in the protected set.
    protected is the set of branch names from environments[*].branch.

    When ``expected_branch`` is supplied, this is lifecycle cleanup rather
    than a generic maintenance operation: the path must be a Git-registered
    worktree currently attached to that exact ticket branch.  A canonical
    pathname is not ownership proof -- a detached or different-branch
    checkout may contain unrelated or recovery work and must be preserved.
    """
    path = Path(wt_path)
    if not path.exists():
        return

    if expected_branch is not None:
        registered, registered_branch = _registered_worktree_branch(repo_root, path)
        is_detached = _worktree_is_detached(path)
        attached_expected = registered and registered_branch == expected_branch and not is_detached
        if registered and not attached_expected:
            actual = registered_branch or "detached HEAD"
            raise RuntimeError(
                f"ERROR: Refusing lifecycle cleanup of canonical worktree '{path}': "
                f"expected branch '{expected_branch}', found {actual}; preserving it."
            )

    # Determine the branch checked out in this worktree
    branch_check = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True, encoding="utf-8",
        cwd=path,
    )
    branch = branch_check.stdout.strip() if branch_check.returncode == 0 else ""

    if branch in protected:
        raise PermissionError(
            f"Refusing to remove worktree '{path}': branch '{branch}' is a protected environment branch. "
            f"Protected branches: {sorted(protected)}"
        )

    subprocess.run(
        ["git", "worktree", "remove", "--force", str(path)],
        cwd=repo_root,
        capture_output=True,
    )


def prune_worktrees(repo_root: Path, protected: set[str], worktrees_dir: Path) -> None:
    """
    Prune stale worktrees. Never prunes worktrees whose branch is in the protected set.
    """
    # Collect protected worktree paths before pruning
    protected_paths = set()
    if worktrees_dir.exists():
        for wt in worktrees_dir.iterdir():
            if not wt.is_dir():
                continue
            branch_check = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True, encoding="utf-8",
                cwd=wt,
            )
            branch = branch_check.stdout.strip() if branch_check.returncode == 0 else ""
            if branch in protected:
                protected_paths.add(wt)

    # Only prune if there are no protected worktrees (git worktree prune has no exclude option)
    if not protected_paths:
        subprocess.run(["git", "worktree", "prune"], cwd=repo_root, capture_output=True)
