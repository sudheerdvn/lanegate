"""Small, failure-aware helpers for Git queries shared by command surfaces."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitText:
    """The outcome of a Git command whose stdout is needed as text.

    ``text`` is meaningful only when ``ok`` is true.  This keeps a successful
    command with no output distinct from a command that could not be run or
    exited unsuccessfully.
    """

    text: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Whether Git successfully produced ``text``."""
        return self.error is None


def git_text(args: list[str], cwd: Path) -> GitText:
    """Run a Git command and retain diagnostics separately from its text output.

    Non-zero commands can occasionally produce neither stdout nor stderr, so
    every failure includes the command and exit status as a usable diagnostic.
    When Git does emit a diagnostic, prefer stderr and fall back to stdout.
    """
    command = " ".join(args)
    try:
        # Git's own output is always UTF-8 regardless of platform locale, but
        # text=True without an explicit encoding decodes with
        # locale.getpreferredencoding() -- cp1252 on a default Windows setup,
        # which mangles any non-ASCII byte (e.g. an em dash in a commit
        # message) instead of raising.
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    except OSError as exc:
        return GitText("", f"could not run {command}: {exc}")

    text = result.stdout.strip()
    if result.returncode == 0:
        return GitText(text)

    detail = next(
        (
            line.strip()
            for output in (result.stderr, result.stdout)
            for line in output.splitlines()
            if line.strip()
        ),
        None,
    )
    error = f"{command} failed (exit {result.returncode})"
    if detail:
        error = f"{error}: {detail}"
    return GitText("", error)


@dataclass(frozen=True)
class PendingCommits:
    """The outcome of asking Git for commits in ``base..head``.

    An empty ``commits`` list is meaningful only when ``ok`` is true.  Keeping
    failures in this result prevents callers from treating an invalid or absent
    ref as an up-to-date environment.
    """

    commits: list[str]
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Whether Git successfully evaluated the requested range."""
        return self.error is None


def pending_commits(repo_root: Path, base: str, head: str) -> PendingCommits:
    """Return oneline commits in ``base..head``, preserving Git failures.

    The first stderr line is deliberately included in errors: it usually names
    the missing or invalid ref and is safe to display in board/pipeline output.
    """
    try:
        result = subprocess.run(
            ["git", "log", f"{base}..{head}", "--oneline"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=repo_root,
        )
    except OSError as exc:
        return PendingCommits([], f"could not run git log {base}..{head}: {exc}")

    if result.returncode != 0:
        detail = next((line.strip() for line in result.stderr.splitlines() if line.strip()), None)
        message = f"git log {base}..{head} failed (exit {result.returncode})"
        if detail:
            message = f"{message}: {detail}"
        return PendingCommits([], message)

    return PendingCommits([line for line in result.stdout.splitlines() if line])
