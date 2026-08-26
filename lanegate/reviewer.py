"""
reviewer.py — Build review prompts for the review subagent and parse results.

All ticket fields are placed inside an <untrusted-data> block so that
malicious or compromised ticket content cannot inject instructions into
the trusted system layer.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from lanegate.config import load_config, resolve_trunk_branch
from lanegate import executor
from lanegate.executor import _CLAUDE_SUBPROCESS_TYPES, matching_verification_groups
from lanegate.safeguards import effective_safeguards
from lanegate.ticket import load_acceptance_contract_audit, load_change_notes
from lanegate.prompts import (
    _resolve_control_root,
    build_prompt,
    component_for as _component,
    get_bounded_reference_excerpts,
    get_payload_budget,
    load_project_guidance,
    load_prompt_template,
    render_discovery_guidance,
    render_prompt,
    resolve_reference_doc_paths,
    truncate_diff_to_budget,
    truncate_to_budget,
)
from lanegate.ticket import load_file_skeletons


class ReviewError(Exception):
    """Raised when a review cannot proceed (missing worktree, empty diff, etc.)."""


def _diff_truncation_note(section_label: str, omitted_paths: list[str]) -> str:
    """Trusted-layer note for when a diff was too large and whole files were dropped.

    Placed in the trusted instruction layer (not the diff itself) so it reads
    as an authoritative caveat rather than something an attacker-controlled
    diff could imitate or bury. The omitted paths are lanegate-computed from
    the diff's own file headers, not copied from untrusted prose.
    """
    file_list = ", ".join(omitted_paths)
    return (
        f"## {section_label} was truncated — some changed files are not shown\n\n"
        f"The following files changed on this branch but were cut entirely from "
        f"{section_label} below because the combined diff exceeded this step's size "
        f"budget: {file_list}. Do not treat their absence as evidence they are "
        "unchanged, or as confirmation that an issue in them is still unresolved — "
        "you have no evidence about their current content either way. Say so "
        "explicitly (e.g. \"cannot verify — file omitted from diff\") rather than "
        "repeating a prior finding about one of these files as if you re-checked it."
    )


@dataclass
class ReviewResult:
    """Parsed result from a review agent response."""

    verdict: str  # "approved" or "changes_requested"
    notes: str = field(default="")
    findings: str = field(default="")
    # A process/config/transport failure means no reviewer made a substantive
    # judgment. Keep it distinct from a real changes_requested verdict, which
    # is the only outcome eligible for an auto-fix cycle.
    harness_error: bool = field(default=False)
    # The reviewer made a verdict, but its own transcript shows it could not
    # execute verification commands. Auto-fix must not act on that verdict.
    verification_not_possible: bool = field(default=False)


def _verification_not_possible(raw: str) -> bool:
    """Detect executor failures that leave a review unable to verify claims."""
    output = raw.casefold()
    if "bwrap:" in output or "loopback:" in output:
        return True
    return sum(output.count(marker) for marker in ("command not found", "permission denied")) >= 2


def _trunk_branch(repo_root: Path) -> str:
    return resolve_trunk_branch(load_config(repo_root), repo_root)


def get_worktree_diff(worktree_path: Path, branch: str, base: str | None = None) -> str:
    """Return the git diff between *base* and *branch* from within the worktree.

    Uses the three-dot (``base...branch``) form, which diffs against the
    merge-base of *base* and *branch* rather than *base*'s current tip. If
    *base* has advanced since *branch* diverged (e.g. another ticket merged
    mid-run, the normal parallel case), a two-dot diff would compare tip vs.
    tip and include every base-side change as spurious reversals on *branch*.
    Three-dot isolates *branch*'s own changes regardless of how far *base*
    has moved.

    Args:
        worktree_path: Absolute path to the ticket's git worktree directory.
        branch: The ticket's branch name (e.g. ``"tick-042"``).
        base: The base ref to diff against (default: the resolved trunk branch).

    Returns:
        The diff text (stdout of ``git diff base...branch``).

    Raises:
        ReviewError: If the worktree path does not exist, the git command fails,
            or the diff is empty (branch has no commits ahead of base).
    """
    base = base or _trunk_branch(worktree_path)
    if not worktree_path.exists():
        raise ReviewError(
            f"Worktree does not exist: {worktree_path}. The ticket may not have been started yet."
        )

    result = subprocess.run(
        ["git", "diff", f"{base}...{branch}"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        raise ReviewError(
            f"git diff {base}...{branch} failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    diff_text = result.stdout
    if not diff_text.strip():
        raise ReviewError(
            f"No diff found between {base} and {branch}. "
            "The branch has no commits ahead of the base — nothing to review."
        )

    return diff_text


def worktree_has_commits(ticket: dict, repo_root: Path, base: str | None = None) -> bool:
    """True if the ticket's branch has real commits ahead of *base*.

    Distinguishes "an executor actually did work here" from "this ticket's
    worktree/branch fields point somewhere with nothing committed to it" --
    e.g. a pre-flight gate blocked before any executor ran, or a ticket's
    status field was hand-edited without the branch ever being touched.

    A missing ``worktree`` does not mean no commits: ``cmd_hibernate --reset``
    preserves recovery work by clearing ``ticket["worktree"]`` while
    deliberately keeping ``ticket["branch"]`` and the branch ref itself
    (hibernate.py's "preserving branch ... resume with `lanegate start`"
    path). Checking the branch from *repo_root* -- not requiring a worktree
    directory to run in -- catches that case; a bare working directory has
    every branch ref available regardless of which worktrees currently exist.
    """
    base = base or _trunk_branch(repo_root)
    branch = ticket.get("branch")
    if not branch:
        return False
    wt = ticket.get("worktree")
    cwd = Path(wt) if wt and Path(wt).is_dir() else Path(repo_root)
    result = subprocess.run(
        ["git", "rev-list", "--count", f"{base}..refs/heads/{branch}"],
        cwd=str(cwd),
        capture_output=True,
        text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        return False
    try:
        return int(result.stdout.strip()) > 0
    except ValueError:
        return False


def _current_head_sha(worktree_path: Path) -> str | None:
    """Return the current HEAD commit sha in *worktree_path*, or None on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True, encoding="utf-8",
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def get_commit_messages(worktree_path: Path, branch: str, base: str | None = None) -> str:
    """Return the commit messages for *branch* ahead of *base*, newest first.

    Used to surface any ``Verification:`` note the implementer left in a
    commit message, since a plain ``git diff`` does not include commit
    messages. Best-effort: returns "" on any git failure rather than raising,
    since a missing commit log should not block review the way a missing
    diff does.
    """
    base = base or _trunk_branch(worktree_path)
    try:
        result = subprocess.run(
            ["git", "log", f"{base}..{branch}", "--format=%B%n---"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True, encoding="utf-8",
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def parse_review_result(raw: str) -> ReviewResult:
    """Parse the raw string output from a review agent into a ReviewResult.

    Fail-closed: any missing, malformed, or unparseable response — including
    empty strings, invalid JSON, and responses that lack a recognised verdict
    value — returns a ``changes_requested`` verdict rather than silently
    approving.

    Args:
        raw: The raw stdout string from the review agent subprocess.

    Returns:
        A :class:`ReviewResult`.  If the verdict cannot be determined the
        result has ``verdict="changes_requested"`` and ``notes`` describes the
        parse problem.
    """
    _VALID_VERDICTS = {"approved", "changes_requested", "APPROVE", "REJECT"}

    try:
        # strict=False tolerates a literal control character (e.g. a raw
        # newline) inside a string value -- smaller/local models routinely
        # hard-wrap a long "summary"/"notes" this way.
        data = json.loads(raw, strict=False)
        verdict = data.get("verdict")
        if verdict not in _VALID_VERDICTS:
            raise ValueError(f"Missing or invalid verdict: {verdict!r}")
        # Normalise APPROVE/REJECT aliases to the canonical values used internally
        if verdict == "APPROVE":
            verdict = "approved"
        elif verdict == "REJECT":
            verdict = "changes_requested"
        notes = data.get("notes", data.get("summary", ""))
        findings = data.get("findings", "")
        if not isinstance(findings, str):
            findings = ""
        verification_not_possible = _verification_not_possible(raw)
        if verification_not_possible:
            flag = "Verification was not actually possible"
            notes = f"{flag}: executor output showed command-execution failures. {notes}".strip()
        return ReviewResult(
            verdict=verdict,
            notes=notes,
            findings=findings,
            verification_not_possible=verification_not_possible,
        )
    except Exception as exc:
        # Fail closed: any parse failure is a rejection
        return ReviewResult(
            verdict="changes_requested",
            notes=f"Review parse error: {exc}",
        )


@dataclass
class DriftCheckResult:
    """Parsed result from a drift-check agent response."""

    ok: bool
    reason: str = ""


def parse_drift_check_result(raw: str) -> DriftCheckResult:
    """Parse the raw string output from a drift-check agent into a DriftCheckResult.

    Fail-closed: any missing, malformed, or unparseable response — including
    empty strings, invalid JSON, and a non-bool ``drift_ok`` — returns
    ``ok=False`` rather than silently letting a fix through.

    Args:
        raw: The raw stdout string from the drift-check agent subprocess.

    Returns:
        A :class:`DriftCheckResult`. If the result cannot be determined,
        ``ok=False`` and ``reason`` describes the parse problem.
    """
    try:
        # strict=False tolerates a literal control character (e.g. a raw
        # newline) inside a string value -- see parse_review_result.
        try:
            data = json.loads(raw, strict=False)
        except json.JSONDecodeError:
            # A drift-check agent's own "reason" text is exactly as prone to
            # unescaped interior quotes as a review verdict's summary/findings
            # -- reuse the same character-level repair rather than failing
            # closed on a response that is otherwise well-formed.
            from lanegate.orchestrate.review import _escape_interior_quotes

            data = json.loads(_escape_interior_quotes(raw), strict=False)
        drift_ok = data.get("drift_ok")
        if not isinstance(drift_ok, bool):
            raise ValueError(f"Missing or invalid drift_ok: {drift_ok!r}")
        reason = data.get("reason", "")
        if not isinstance(reason, str):
            reason = ""
        return DriftCheckResult(ok=drift_ok, reason=reason)
    except Exception as exc:
        # Fail closed: any parse failure means the drift check did not pass
        return DriftCheckResult(ok=False, reason=f"Drift check parse error: {exc}")


# Matches the built-in test-runner invocations safeguards.py itself
# special-cases (pytest / npm test / cargo test / go test) so a lint/type-check/
# deploy-script guard (also valid safeguard entries) is never mistaken for a
# test having run.
# Recognizes `make <target>` and `npm run <x>` only when the target
# is explicitly test-named. Those command forms also run lint, type-check, and
# build targets, none of which prove tests have run. Qualified test target names
# such as `test:unit` and `integration-test` count as test-shaped.
_TEST_TARGET_NAME = r"(?:[A-Za-z0-9]+[-_:./])*tests?(?:[-_:./][A-Za-z0-9]+)*"
_TEST_GUARD_PATTERN = re.compile(
    rf"(?:^|[\s;&|])(?:pytest|npm\s+test|cargo\s+test|go\s+test)\b|"
    rf"(?:^|[\s;&|])(?:npm\s+run|make)\s+{_TEST_TARGET_NAME}(?=$|[\s;&|])",
    re.IGNORECASE,
)


def _test_shaped_guards(guards: list[str]) -> list[str]:
    """Return only the entries of *guards* that invoke a built-in test runner."""
    return [g for g in guards if _TEST_GUARD_PATTERN.search(str(g))]


def is_non_tool_reviewer(reviewer_type: str | None) -> bool:
    """Return True if *reviewer_type* runs without an interactive tool-dispatch loop.

    Delegates to the central ``executor.EXECUTOR_CAPABILITIES`` registry so
    that newly registered non-agentic executor types are automatically covered
    without needing a separate entry here.  An unknown or ``None`` reviewer
    type returns ``False`` (safe default: assume tool-capable).

    See TICK-644 for why this matters: non-tool reviewers must receive the diff
    inline rather than being told to run ``git diff`` themselves, because they
    have no tool loop to satisfy a ``<tool_call>`` they emit.
    """
    if reviewer_type is None or reviewer_type not in executor.EXECUTOR_CAPABILITIES:
        return False
    return not executor.has_capability(reviewer_type, "tool_dispatch_loop")


def _diff_access_note(
    non_tool_reviewer: bool,
    has_diff: bool,
    trunk_branch: str,
    read_only: bool = False,
    claude_blocked_by_read_only: bool = False,
) -> str:
    if not non_tool_reviewer:
        if read_only:
            return (
                "with read-only git and file access, the same environment "
                "the implementer used — you can run read commands (`git "
                "diff`, `git log -p`, `git show`, `cat`) but cannot write or "
                "edit any file. Do not search for it or run commands from "
                f"any other directory. Run `git diff {trunk_branch}...HEAD` (or `git "
                "log -p`) yourself and read the full surrounding context of "
                "each changed file before judging anything; do not evaluate "
                "from a pasted hunk."
            )
        return (
            "with full git, file, and test-execution tool access, the same "
            "environment the implementer used. Do not search for it or run "
            "commands from any other directory. "
            f"Run `git diff {trunk_branch}...HEAD` "
            "(or `git log -p`) yourself and read the full surrounding "
            "context of each changed file before judging anything; do not "
            "evaluate from a pasted hunk."
        )
    if claude_blocked_by_read_only:
        if has_diff:
            return (
                "with your Bash tool disabled, so you cannot run `git diff` "
                "or any other shell command yourself — the full diff of this "
                "branch is provided below as GIT DIFF, inspect it directly. "
                "Your Read, Glob, and Grep tools remain available: use them "
                "to read the full surrounding context of any changed file "
                "beyond what the inlined diff and FILE SKELETONS below show, "
                "the same way a tool-capable reviewer would with `git diff` "
                "-- do not evaluate from a pasted hunk alone."
            )
        return (
            "with your Bash tool disabled, so you cannot run `git diff` or "
            "any other shell command yourself, and no branch diff could be "
            "extracted to include below. Your Read, Glob, and Grep tools "
            "remain available -- use them to read the full surrounding "
            "context of files named in FILE SKELETONS, COMMIT MESSAGES, and "
            "the other untrusted-data sections below."
        )
    if has_diff:
        return (
            "but you do not have shell or file-read tool access in this "
            "session. The full diff of this branch is provided below as "
            "GIT DIFF — inspect it directly, together with FILE SKELETONS "
            "below when present, instead of attempting to run git or read "
            "additional files yourself."
        )
    return (
        "but you do not have shell or file-read tool access in this session, "
        "and no branch diff could be extracted to include below. Judge only "
        "from FILE SKELETONS, COMMIT MESSAGES, and the other untrusted-data "
        "sections below."
    )


def _repro_execution_note(
    non_tool_reviewer: bool,
    has_diff: bool = False,
    read_only: bool = False,
    claude_blocked_by_read_only: bool = False,
) -> str:
    if not non_tool_reviewer:
        if read_only:
            return (
                "construct and verify a minimal repro using only read "
                "commands (`git diff`, `git log -p`, `git show "
                "<sha>:<path>`, `cat`) and, if the project's existing test "
                "suite already covers the behavior, running it as-is — you "
                "cannot write or edit any file in this session, so do not "
                "revert, stash, or otherwise modify the working tree to "
                "test a pre-change state. To reason about a pre-change "
                "state, read it directly with `git show <parent-sha>:<path>` "
                "and trace the logic by comparison instead of actually "
                "reverting the file. If verifying the finding genuinely "
                "requires a filesystem write (e.g. a new one-off test you'd "
                "need to add), say so explicitly and record the finding as "
                "unverified by execution rather than silently dropping it."
            )
        return (
            "construct and execute a minimal repro — a single targeted test "
            "or a few git commands, not the full test suite — using your "
            "existing git/file/test-execution tool access in the working "
            "directory before writing it down; do not assert the failure "
            "from reading the diff alone. Do not use a bare `git stash`/`git "
            "stash pop` to temporarily revert code for this: stash is a "
            "single repo-wide ref stack shared across every worktree of the "
            "clone, and popping can silently apply an unrelated concurrent "
            "session's changes. Instead, revert just the touched file's "
            "working-tree content — `git show <parent-of-first-diff-commit>:"
            "<path> > <path>` or `git checkout <parent-sha> -- <path>` — run "
            "the targeted test, then restore with `git checkout HEAD -- "
            "<path>`. If stash use is genuinely unavoidable, give it a "
            "unique per-invocation name (ticket id plus a random 4-5 digit "
            "suffix) and pop or drop it by that exact name via `git stash "
            "list | grep '<name>'`, never a bare `git stash pop`/`git stash "
            "pop stash@{0}`. If a repro genuinely cannot run in this "
            "environment (for example it needs an external service or "
            "credentials you do not have), say so explicitly and record the "
            "finding as unverified by execution rather than silently "
            "dropping it."
        )
    source = "GIT DIFF and FILE SKELETONS" if has_diff else "FILE SKELETONS"
    if claude_blocked_by_read_only:
        return (
            f"trace through a minimal repro, starting from {source} but "
            "also using your Read, Glob, and Grep tools to read the full "
            "surrounding context of any file involved -- name the concrete "
            "input or state and the exact resulting behavior. Your Bash "
            "tool is disabled, so you cannot run git, run tests, or execute "
            "any command; do not assert a failure without spelling out that "
            "trace from what you can read. If what you can read is not "
            "enough to construct a concrete trace, say so explicitly and "
            "record the finding as unverified rather than silently dropping it."
        )
    return (
        f"trace through a minimal repro by reading {source} — name the "
        "concrete input or state and the exact resulting behavior. You do "
        "not have shell, file, or test-execution tool access in this "
        "session, so do not attempt to run git, run tests, or read "
        "additional files: do not assert a failure without spelling out "
        "that trace from what is already provided. If what is provided is "
        "not enough to construct a concrete trace, say so explicitly and "
        "record the finding as unverified rather than silently dropping it."
    )


def build_review_prompt(
    ticket: dict,
    commit_messages: str = "",
    project_root: Path | None = None,
    cfg: dict | None = None,
    *,
    _components: list | None = None,
    worktree_path: Path | None = None,
    reviewer_type: str | None = None,
    diff: str | None = None,
    read_only: bool = False,
) -> str:
    """Return a trust-separated prompt for reviewing *ticket*.

    The instruction text is loaded from a configurable template
    (``<project_root>/prompts/review.md`` if present, otherwise the
    built-in default).  Ticket fields are placed inside the
    ``<untrusted-data>`` wrapper so they cannot inject instructions into the
    agent's trusted instruction layer.

    When the ticket has prior review findings (from a prior changes_requested
    verdict), they are injected into the trusted instruction layer so the
    reviewer works through each item as a confirmation checklist.

    Args:
        ticket: A parsed ticket dict (as returned by ``parse_ticket``).
        commit_messages: Commit log for the ticket's branch (see
            ``get_commit_messages``). Included so the reviewer can see any
            ``Verification:`` note the implementer left, since a plain diff
            does not carry commit messages.
        project_root: Root of the managed project.  When provided, a
            ``prompts/review.md`` override in that directory takes precedence
            over the built-in template.  When ``None``, the built-in default
            is used.
        cfg: Loaded LaneGate config.  When provided, ``project_guidance`` controls
            which repo-local coding/contribution instructions are added to the
            trusted prompt layer.
        worktree_path: Path to the ticket's git worktree (if distinct from project_root).
        reviewer_type: The resolved reviewer executor type (e.g. ``"claude"``,
            ``"aider"``, ``"ollama"``). When ``is_non_tool_reviewer`` returns
            ``True`` for it (a single-turn, non-agentic invocation with no
            tool-dispatch loop), the prompt inlines *diff* under a GIT DIFF
            section and tells the reviewer to inspect it directly instead of
            running git/file tools itself — otherwise those models emit a
            dead-end ``<tool_call>`` and never produce a verdict (TICK-644).
            ``None`` (the default) keeps the original tool-capable instructions.
        diff: The branch's git diff (see ``get_worktree_diff``), forwarded
            into the prompt only when *reviewer_type* is non-tool-capable.
        read_only: Whether the review dispatch enforces read-only tool
            access (``build_executor_cmd(..., read_only=True)``). For a
            Claude-type reviewer this blocks the Bash tool entirely
            (``disallowed_tools``), leaving no way to self-fetch the diff —
            treated the same as a non-tool reviewer for that purpose. For
            other tool-capable reviewers (codex ``--sandbox read-only``, agy
            ``--mode plan``) reads still work, so only the repro-execution
            instructions change to avoid prescribing filesystem writes.

    Returns:
        A fully-rendered prompt string safe to pass to the review agent.
    """
    non_tool_reviewer = is_non_tool_reviewer(reviewer_type)
    claude_blocked_by_read_only = read_only and reviewer_type in _CLAUDE_SUBPROCESS_TYPES
    effective_non_tool_reviewer = non_tool_reviewer or claude_blocked_by_read_only
    raw_root = project_root if project_root is not None else Path.cwd()
    wt = worktree_path if worktree_path is not None else raw_root
    root = _resolve_control_root(raw_root)

    tid = ticket["id"]
    title = ticket.get("title", tid)
    close_criteria = ticket.get("close_criteria", "")
    touches = ", ".join(ticket.get("touches") or []) or "none"

    # Review previously received no code structure whatsoever — only
    # guidance prose, reference excerpts, change notes and ticket fields — so a
    # reviewer had to re-read files just to learn what a changed symbol was.
    # Regenerated from the worktree rather than replayed from analyze, since the
    # whole point here is to describe the code as the implementer left it.
    # Loaded before the template renders: the discovery guidance below needs to
    # know whether skeletons actually loaded, not just whether the ticket
    # declares touches — a ticket can declare touches for files that don't
    # parse (new files, non-code files) and get zero skeletons back.
    review_skeletons = load_file_skeletons(ticket, wt, regenerate=True)
    has_diff = bool(diff and diff.strip())
    trunk_branch = resolve_trunk_branch(cfg if cfg is not None else load_config(root), root)

    # Load the instruction text from the configurable template
    template = load_prompt_template("review", root)
    
    num_touches = len(ticket.get("touches") or [])
    is_refactor = (
        "refactor" in title.lower()
        or "split" in title.lower()
        or "extract" in title.lower()
        or ("move" in title.lower() and num_touches > 1)
    )
    if is_refactor:
        refactor_guidance = (
            "This is a refactor/split ticket. Do not artificially restrict your review just to TOUCHES if "
            "verifying the move requires checking callers or state across boundaries. You MUST:\n"
            "(a) diff every relocated function/class/method against its pre-change location, byte for byte outside of mechanical relocation and rename, and treat any divergence as a finding;\n"
            "(b) enumerate every self.* attribute and module-level global touched by moved code and confirm a reader and a writer both still exist after the move;\n"
            "(c) grep the whole repository (not just the diff) for every import site of every touched module and flag any inconsistency in how the same file gets imported (bare vs qualified, relative vs absolute) as a finding, not just within the diff's own files;\n"
            "(d) trace any CLI flag, config key, or constructor parameter touched by the diff from its entry point to where it's actually consumed, confirming it still reaches there."
        )
    else:
        refactor_guidance = "Scope your review to what actually changed on this branch — cross-check against TOUCHES below — and do not flag pre-existing code you did not touch."

    instruction = render_prompt(
        template,
        ticket_id=tid,
        title=title,
        close_criteria=close_criteria,
        touches=touches,
        working_directory=str(wt),
        discovery_guidance=render_discovery_guidance(
            cfg, has_skeletons=bool(review_skeletons)
        ),
        diff_access_note=_diff_access_note(
            effective_non_tool_reviewer,
            has_diff,
            trunk_branch,
            read_only=read_only,
            claude_blocked_by_read_only=claude_blocked_by_read_only,
        ),
        repro_execution_note=_repro_execution_note(
            effective_non_tool_reviewer,
            has_diff,
            read_only=read_only,
            claude_blocked_by_read_only=claude_blocked_by_read_only,
        ),
        refactor_guidance=refactor_guidance,
    ).strip()

    # Build the trusted instruction layer with base instruction + prior findings.
    # No ticket ID here by design — TICKET TITLE below already identifies the
    # ticket to the model, so this stays a stable, cacheable prefix across tickets.
    trusted_parts = [instruction]

    # Whether the reviewer should re-run tests depends on the
    # *effective* pre_complete/pre_merge safeguards for this specific ticket
    # (project config plus any permitted per-ticket override via
    # effective_safeguards()) -- a static "check .lanegate.yml" instruction in
    # the template can't see per-ticket overrides, so this is computed here
    # instead of left to the model to infer from a config file it may only
    # partially read.
    #
    # A guard list entry is not necessarily a test: safeguards also cover
    # lint/type-check/deploy scripts (docs/config-reference.md), so only
    # entries that actually invoke one of the built-in test runners count --
    # otherwise a lint-only pre_complete guard would wrongly tell the
    # reviewer tests already ran when nothing tested anything.
    pre_complete_test_guards = _test_shaped_guards(
        effective_safeguards("pre_complete", ticket, cfg or {})
    )
    pre_merge_test_guards = _test_shaped_guards(
        effective_safeguards("pre_merge", ticket, cfg or {})
    )
    # A "do not re-run" claim is only true if pre_complete last
    # verified the commit under review right now. The auto-fix cycle
    # (run_fix_agent -> run_drift_check -> run_review_agent) commits new code
    # without re-running pre_complete, so without this check a fix commit
    # that breaks a test would still be told "tests already ran".
    verified_sha = ticket.get("pre_complete_verified_sha")
    pre_complete_still_current = bool(verified_sha) and verified_sha == _current_head_sha(wt)
    if pre_complete_test_guards and pre_complete_still_current:
        already_ran = ", ".join(pre_complete_test_guards)
        if pre_merge_test_guards:
            detail = (
                f"`{already_ran}` already ran deterministically via `pre_complete` in "
                "this worktree before this review, and "
                f"`{', '.join(pre_merge_test_guards)}` will run again via `pre_merge`/"
                "`post_merge_verify` around the merge."
            )
        else:
            detail = (
                f"`{already_ran}` already ran deterministically via `pre_complete` in "
                "this worktree before this review. No `pre_merge` test guard is "
                "configured, so nothing verifies tests again at merge time."
            )
        trusted_parts.append(
            "## Test execution — do not re-run\n\n"
            f"{detail} Judge test coverage (see below) by reading the test and the "
            "diff, not by re-executing it."
        )
    else:
        if pre_merge_test_guards:
            detail = (
                f"`pre_merge`/`post_merge_verify` will run `{', '.join(pre_merge_test_guards)}` "
                "around the merge, but no `pre_complete` test guard is configured, so "
                "nothing has run tests on this exact commit yet."
            )
        else:
            detail = "No pre_complete or pre_merge test command is configured for this ticket."
        trusted_parts.append(
            "## Test execution — not yet verified\n\n"
            f"{detail} If CLOSE CRITERIA implies behavior that needs verifying by "
            "running code, running the relevant tests yourself now is the most "
            "reliable check available before merge."
        )

    declared_touches = ticket.get("touches") or []

    ref_doc_paths = resolve_reference_doc_paths(root, cfg)
    project_guidance = load_project_guidance(
        root, cfg, step="review", relevant_paths=declared_touches,
        exclude_paths=ref_doc_paths or None,
    )
    if project_guidance:
        trusted_parts.append(project_guidance)

    ref_excerpt, ref_components = get_bounded_reference_excerpts(
        root, declared_touches, cfg=cfg, step="review"
    )
    if ref_excerpt:
        trusted_parts.append(ref_excerpt)

    bounded_skeletons = ""
    if review_skeletons:
        skeleton_text = "\n".join(review_skeletons.values())
        bounded_skeletons, _ = truncate_to_budget(
            skeleton_text, get_payload_budget("review", cfg)
        )
    if _components is not None:
        _components.append(
            _component(
                "file-skeletons",
                "ticket.touches (AST skeleton)",
                "review",
                "\n".join(review_skeletons.values()) if review_skeletons else "",
                reason="regenerated-from-worktree" if review_skeletons else "no-skeletons",
            )
        )

    change_notes = load_change_notes(ticket)
    if change_notes:
        planned_changes = "## Planned changes\n" + "\n".join(
            f"**{f}**: {note}" for f, note in change_notes.items()
        )
        planned_changes, _ = truncate_to_budget(planned_changes, get_payload_budget("review", cfg))
        trusted_parts.append(planned_changes)

    # Prior review findings and acceptance-contract findings originate from an
    # LLM (or deterministic auditor) that has just read an untrusted diff and
    # routinely quotes it verbatim. Only the static instruction text goes in
    # the trusted layer; the findings content itself is untrusted data (F38) —
    # quoted attacker-controlled diff content must not gain instruction-level
    # standing just because a prior reviewer echoed it.
    # Note: `ticket.get('review_findings')` DOES explicitly pass prior findings. 
    # This means TICK-004 in the source project was a correlated model hallucination,
    # not a plumbing gap. The reviewer was told what was found last time and 
    # still flipped to approved without the diff resolving them.
    review_findings = ticket.get("review_findings") or []
    if review_findings:
        trusted_parts.append(
            "## Prior review findings — confirm each is resolved\n\n"
            "The following items (see PRIOR REVIEW FINDINGS below) were raised in a "
            "previous review. Confirm each one is addressed before issuing an `approved` "
            "verdict. If any item is still outstanding, verdict must be `changes_requested` "
            "with findings referencing the unresolved items."
        )

    contract_audit = load_acceptance_contract_audit(ticket)
    contract_findings = []
    if isinstance(contract_audit, dict) and contract_audit.get("ok") is False:
        raw_findings = contract_audit.get("findings") or []
        if isinstance(raw_findings, list):
            contract_findings = [str(f) for f in raw_findings if str(f).strip()]
    if contract_findings:
        trusted_parts.append(
            "## Acceptance-contract audit findings — must be resolved before approval\n\n"
            "The deterministic acceptance-contract audit found that the ticket's "
            "close_criteria is narrower than the source intent or linked context (see "
            "ACCEPTANCE-CONTRACT AUDIT FINDINGS below). Do not approve unless the diff "
            "and ticket metadata resolve these findings."
        )

    verification = ticket.get("verification") or []
    verification_lines = []
    if isinstance(verification, list):
        for rec in verification:
            if not isinstance(rec, dict):
                continue
            status = rec.get("status", "unverified")
            criterion = rec.get("criterion", "")
            evidence = rec.get("evidence", "")
            line = f"[{status}] {criterion}"
            if evidence:
                line += f" — {evidence}"
            verification_lines.append(line)
    if verification_lines:
        trusted_parts.append(
            "## Verification checklist — do not approve with unresolved items\n\n"
            "Each acceptance criterion below (see VERIFICATION CHECKLIST below) was "
            "checked by a deterministic verifier: `verified` means matching evidence was "
            "found in the diff; `manual` means a human already signed off via review "
            "`--findings`; `unverified` means neither. `lanegate review` itself blocks an "
            "`approved` verdict while any item is `unverified` unless `--findings` "
            "documents the human judgment used, so treat an `unverified` item here the "
            "same as an unresolved prior review finding."
        )

    if matching_verification_groups(ticket.get("touches") or [], cfg):
        trusted_parts.append(
            "## Visual verification check\n\n"
            "This ticket touches UI-facing files. Look in COMMIT MESSAGES for a "
            "`Verification:` note describing what the implementer actually observed "
            "(or an explicit statement that no browser/screenshot tooling was available). "
            "If CLOSE CRITERIA implies a visual/behavioral change and there is no such note, "
            "verdict must be `changes_requested` citing the missing verification — do not "
            "approve a UI change on code inspection alone."
        )

    full_instruction = "\n\n".join(trusted_parts)

    untrusted_sections: dict[str, str] = {
        "TICKET TITLE": title,
        "CLOSE CRITERIA": close_criteria,
        "TOUCHES": touches,
    }
    if bounded_skeletons:
        untrusted_sections["FILE SKELETONS"] = bounded_skeletons
    bounded_diff = ""
    if effective_non_tool_reviewer and has_diff and diff is not None:
        bounded_diff, _diff_truncated, _diff_omitted = truncate_diff_to_budget(
            diff, get_payload_budget("review", cfg)
        )
        untrusted_sections["GIT DIFF"] = bounded_diff
        if _diff_omitted:
            full_instruction += "\n\n" + _diff_truncation_note("GIT DIFF", _diff_omitted)
    bounded_commit_messages = ""
    bounded_prior_findings = ""
    bounded_contract_findings = ""
    if commit_messages:
        bounded_commit_messages = truncate_to_budget(
            commit_messages, get_payload_budget("review", cfg)
        )[0]
        untrusted_sections["COMMIT MESSAGES"] = bounded_commit_messages
    if review_findings:
        prior_findings = "\n".join(f"[{i + 1}] {finding}" for i, finding in enumerate(review_findings))
        bounded_prior_findings = truncate_to_budget(
            prior_findings, get_payload_budget("review", cfg)
        )[0]
        untrusted_sections["PRIOR REVIEW FINDINGS"] = bounded_prior_findings
    if contract_findings:
        audit_findings = "\n".join(f"[{i + 1}] {finding}" for i, finding in enumerate(contract_findings))
        bounded_contract_findings = truncate_to_budget(
            audit_findings, get_payload_budget("review", cfg)
        )[0]
        untrusted_sections["ACCEPTANCE-CONTRACT AUDIT FINDINGS"] = bounded_contract_findings
    if verification_lines:
        untrusted_sections["VERIFICATION CHECKLIST"] = "\n".join(verification_lines)

    if _components is not None:
        _components.append(_component("instruction-template", "prompts/review.md", "review", instruction))
        _components.append(_component(
            "project-guidance", "project_guidance.files", "review", project_guidance,
            reason="matched-and-bounded" if project_guidance else "no-matching-files",
        ))
        _components.extend(ref_components)
        _components.append(_component("ticket-title", "ticket.title", "review", title))
        _components.append(_component("ticket-close-criteria", "ticket.close_criteria", "review", close_criteria))
        _components.append(_component("ticket-touches", "ticket.touches", "review", touches))
        _components.append(_component(
            "change-notes", "ticket.change_notes", "review",
            planned_changes if change_notes else "",
            reason="selected-by-ticket" if change_notes else "no-change-notes",
        ))
        _components.append(_component(
            "commit-messages", "worktree commit log", "review", bounded_commit_messages,
            reason="selected-by-ticket"
        ))
        _components.append(_component(
            "prior-review-findings", "ticket.review_findings", "review",
            bounded_prior_findings,
            reason="selected-by-ticket" if review_findings else "no-prior-findings",
        ))
        _components.append(_component(
            "acceptance-contract-findings", "ticket.acceptance_contract_audit", "review",
            bounded_contract_findings,
            reason="selected-by-ticket" if contract_findings else "audit-ok",
        ))
        _components.append(_component(
            "verification-checklist", "ticket.verification", "review",
            "\n".join(verification_lines), reason="selected-by-ticket" if verification_lines else "no-checklist",
        ))
        _components.append(_component(
            "branch-diff", "worktree git diff", "review", bounded_diff,
            reason="non-tool-reviewer" if effective_non_tool_reviewer and has_diff
            else ("tool-capable-reviewer" if not effective_non_tool_reviewer else "no-diff-available"),
        ))

    return build_prompt(
        full_instruction,
        untrusted_sections=untrusted_sections,
    )


def describe_review_payload(
    ticket: dict,
    commit_messages: str = "",
    project_root: Path | None = None,
    cfg: dict | None = None,
    worktree_path: Path | None = None,
    reviewer_type: str | None = None,
    diff: str | None = None,
) -> list[dict]:
    """Return a machine-readable breakdown of every component in the review
    prompt payload -- byte/token estimate, source, pipeline step, and whether
    it's always injected or selected because of the ticket. Component
    metadata only; never includes actual ticket/findings text (payload
    audit).

    ``reviewer_type`` and ``diff`` must be resolved and forwarded by the
    caller the same way ``run_review_agent()`` does (see TICK-644) -- without
    them ``is_non_tool_reviewer(None)`` is always False here, so a project
    actually configured with ``reviewer: aider`` / ``reviewer: ollama`` gets
    audited against the tool-capable prompt shape and undercounts the
    (potentially large) inlined GIT DIFF section, defeating the audit's
    purpose for exactly the configs TICK-644 targets.
    """
    components: list = []
    build_review_prompt(
        ticket, commit_messages, project_root, cfg, _components=components,
        worktree_path=worktree_path, reviewer_type=reviewer_type, diff=diff,
    )
    return [c.as_dict() for c in components]


def build_fix_prompt(
    ticket: dict,
    diff: str,
    findings: str,
    project_root: Path | None = None,
    cfg: dict | None = None,
    *,
    _components: list | None = None,
    worktree_path: Path | None = None,
) -> str:
    """Return a trust-separated prompt instructing an executor to address
    review findings on ticket's existing diff.

    Args:
        ticket: A parsed ticket dict (as returned by ``parse_ticket``).
        diff: The current ``git diff base..branch`` output for the ticket's
            worktree branch — the diff the fix will build on top of.
        findings: The review findings text to address (reviewer-produced
            directive text, not raw ticket-author content — placed in the
            trusted layer, mirroring how prior findings are treated in
            ``build_review_prompt``).
        project_root: Root of the managed project.  When provided, a
            ``prompts/fix.md`` override in that directory takes precedence
            over the built-in template.
        cfg: Loaded LaneGate config.  When provided, ``project_guidance``
            controls which repo-local coding/contribution instructions are
            added to the trusted prompt layer.
        worktree_path: Path to the ticket's git worktree (if distinct from project_root).

    Returns:
        A fully-rendered prompt string safe to pass to the fix agent.
    """
    raw_root = project_root if project_root is not None else Path.cwd()
    wt = worktree_path if worktree_path is not None else raw_root
    root = _resolve_control_root(raw_root)
    diff, _diff_truncated, _diff_omitted = truncate_diff_to_budget(diff, get_payload_budget("fix", cfg))
    findings, _ = truncate_to_budget(findings, get_payload_budget("fix", cfg))

    tid = ticket["id"]
    title = ticket.get("title", tid)
    close_criteria = ticket.get("close_criteria", "")
    touches = ", ".join(ticket.get("touches") or []) or "none"

    template = load_prompt_template("fix", root)
    instruction = render_prompt(
        template,
        ticket_id=tid,
        title=title,
        close_criteria=close_criteria,
        touches=touches,
        diff=diff,
        working_directory=str(wt),
    ).strip()

    trusted_parts = [instruction]
    if _diff_omitted:
        trusted_parts.append(_diff_truncation_note("GIT DIFF", _diff_omitted))

    declared_touches = ticket.get("touches") or []

    ref_doc_paths = resolve_reference_doc_paths(root, cfg)
    project_guidance = load_project_guidance(
        root, cfg, step="review", relevant_paths=declared_touches,
        exclude_paths=ref_doc_paths or None,
    )
    if project_guidance:
        trusted_parts.append(project_guidance)

    ref_excerpt, ref_components = get_bounded_reference_excerpts(
        root, declared_touches, cfg=cfg, step="fix"
    )
    if ref_excerpt:
        trusted_parts.append(ref_excerpt)

    if findings:
        # Findings text is reviewer-LLM-generated and routinely quotes the diff
        # verbatim — it goes in the untrusted layer, not here, for the same
        # reason as build_review_prompt's prior/contract findings (F38).
        trusted_parts.append(
            "## Review Findings To Address\n\n"
            "See REVIEW FINDINGS below for the specific items raised by the reviewer."
        )

    full_instruction = "\n\n".join(trusted_parts)

    untrusted_sections: dict[str, str] = {
        "TICKET TITLE": title,
        "CLOSE CRITERIA": close_criteria,
    }
    if diff:
        untrusted_sections["GIT DIFF"] = diff
    if findings:
        untrusted_sections["REVIEW FINDINGS"] = findings.strip()

    if _components is not None:
        _components.append(_component("instruction-template", "prompts/fix.md", "fix", instruction))
        _components.append(_component(
            "project-guidance", "project_guidance.files", "fix", project_guidance,
            reason="matched-and-bounded" if project_guidance else "no-matching-files",
        ))
        _components.extend(ref_components)
        _components.append(_component("ticket-title", "ticket.title", "fix", title))
        _components.append(_component("ticket-close-criteria", "ticket.close_criteria", "fix", close_criteria))
        _components.append(_component("git-diff", "worktree diff", "fix", diff, reason="selected-by-ticket"))
        _components.append(_component(
            "review-findings", "reviewer output", "fix", findings, reason="selected-by-ticket" if findings else "no-findings"
        ))

    return build_prompt(
        full_instruction,
        untrusted_sections=untrusted_sections,
    )


def describe_fix_payload(
    ticket: dict,
    diff: str,
    findings: str,
    project_root: Path | None = None,
    cfg: dict | None = None,
    worktree_path: Path | None = None,
) -> list[dict]:
    """Return a machine-readable breakdown of every component in the fix
    prompt payload -- byte/token estimate, source, pipeline step, and whether
    it's always injected or selected because of the ticket/diff. Component
    metadata only; never includes actual ticket/diff/findings text (payload
    audit).
    """
    components: list = []
    build_fix_prompt(ticket, diff, findings, project_root, cfg, _components=components, worktree_path=worktree_path)
    return [c.as_dict() for c in components]


def build_drift_check_prompt(
    ticket: dict,
    original_diff: str,
    fix_diff: str,
    findings: str,
    project_root: Path | None = None,
    cfg: dict | None = None,
    worktree_path: Path | None = None,
) -> str:
    """Return a trust-separated prompt asking a drift-check agent whether a
    fix still matches the ticket's intent.

    Args:
        ticket: A parsed ticket dict (as returned by ``parse_ticket``).
        original_diff: The diff before the fix pass ran.
        fix_diff: Only the changes made by the fix pass, isolated from the
            original diff (see ``get_worktree_diff``'s ``base`` param).
        findings: The review findings the fix pass was asked to address.
        project_root: Root of the managed project.  When provided, a
            ``prompts/drift_check.md`` override in that directory takes
            precedence over the built-in template.
        cfg: Loaded LaneGate config.  When provided, ``project_guidance``
            controls which repo-local coding/contribution instructions are
            added to the trusted prompt layer.
        worktree_path: Path to the ticket's git worktree (if distinct from project_root).

    Returns:
        A fully-rendered prompt string safe to pass to the drift-check agent.
    """
    raw_root = project_root if project_root is not None else Path.cwd()
    wt = worktree_path if worktree_path is not None else raw_root
    root = _resolve_control_root(raw_root)
    original_diff, _orig_truncated, _orig_omitted = truncate_diff_to_budget(
        original_diff, get_payload_budget("fix", cfg)
    )
    fix_diff, _fix_truncated, _fix_omitted = truncate_diff_to_budget(
        fix_diff, get_payload_budget("fix", cfg)
    )
    findings, _ = truncate_to_budget(findings, get_payload_budget("fix", cfg))

    tid = ticket["id"]
    title = ticket.get("title", tid)
    close_criteria = ticket.get("close_criteria", "")

    template = load_prompt_template("drift_check", root)
    instruction = render_prompt(
        template,
        ticket_id=tid,
        title=title,
        close_criteria=close_criteria,
        working_directory=str(wt),
    ).strip()

    trusted_parts = [instruction]

    project_guidance = load_project_guidance(
        root, cfg, step="review", relevant_paths=ticket.get("touches") or [],
        exclude_paths=set(),
    )
    if project_guidance:
        trusted_parts.append(project_guidance)
    if _orig_omitted:
        trusted_parts.append(_diff_truncation_note("ORIGINAL DIFF", _orig_omitted))
    if _fix_omitted:
        trusted_parts.append(_diff_truncation_note("FIX DIFF", _fix_omitted))

    full_instruction = "\n\n".join(trusted_parts)

    untrusted_sections: dict[str, str] = {
        "CLOSE CRITERIA": close_criteria,
        "REVIEW FINDINGS": findings or "(none)",
        "ORIGINAL DIFF": original_diff,
        "FIX DIFF": fix_diff,
    }

    return build_prompt(
        full_instruction,
        untrusted_sections=untrusted_sections,
    )
