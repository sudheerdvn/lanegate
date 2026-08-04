"""
reviewer.py — Build review prompts for the review subagent and parse results.

All ticket fields are placed inside an <untrusted-data> block so that
malicious or compromised ticket content cannot inject instructions into
the trusted system layer.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from lanegate.config import load_config, resolve_trunk_branch
from lanegate.executor import matching_verification_groups
from lanegate.prompts import (
    build_prompt,
    component_for as _component,
    get_bounded_architecture_excerpt,
    get_payload_budget,
    load_project_guidance,
    load_prompt_template,
    render_prompt,
    truncate_to_budget,
)


class ReviewError(Exception):
    """Raised when a review cannot proceed (missing worktree, empty diff, etc.)."""


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


def _trunk_branch(repo_root: Path) -> str:
    return resolve_trunk_branch(load_config(repo_root), repo_root)


def get_worktree_diff(worktree_path: Path, branch: str, base: str | None = None) -> str:
    """Return the git diff between *base* and *branch* from within the worktree.

    Args:
        worktree_path: Absolute path to the ticket's git worktree directory.
        branch: The ticket's branch name (e.g. ``"tick-042"``).
        base: The base ref to diff against (default: the resolved trunk branch).

    Returns:
        The diff text (stdout of ``git diff base..branch``).

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
        ["git", "diff", f"{base}..{branch}"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        raise ReviewError(
            f"git diff {base}..{branch} failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    diff_text = result.stdout
    if not diff_text.strip():
        raise ReviewError(
            f"No diff found between {base} and {branch}. "
            "The branch has no commits ahead of the base — nothing to review."
        )

    return diff_text


def worktree_has_commits(ticket: dict, repo_root: Path, base: str | None = None) -> bool:
    """True if the ticket's worktree branch has real commits ahead of *base*.

    Distinguishes "an executor actually did work here" from "this ticket's
    worktree/branch fields point somewhere with nothing committed to it" --
    e.g. a pre-flight gate blocked before any executor ran, or a ticket's
    status field was hand-edited without the branch ever being touched.
    repo_root is unused directly (worktree is always stored as an absolute
    path) but kept in the signature since callers reason about a ticket in
    the context of a specific repo.
    """
    base = base or _trunk_branch(repo_root)
    wt = ticket.get("worktree")
    branch = ticket.get("branch")
    if not wt or not branch:
        return False
    wt_path = Path(wt)
    if not wt_path.is_dir():
        return False
    result = subprocess.run(
        ["git", "rev-list", "--count", f"{base}..{branch}"],
        cwd=str(wt_path),
        capture_output=True,
        text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        return False
    try:
        return int(result.stdout.strip()) > 0
    except ValueError:
        return False


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
        return ReviewResult(verdict=verdict, notes=notes, findings=findings)
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
        data = json.loads(raw, strict=False)
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


def build_review_prompt(
    ticket: dict,
    commit_messages: str = "",
    project_root: Path | None = None,
    cfg: dict | None = None,
    *,
    _components: list | None = None,
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

    Returns:
        A fully-rendered prompt string safe to pass to the review agent.
    """
    root = project_root if project_root is not None else Path.cwd()

    tid = ticket["id"]
    title = ticket.get("title", tid)
    close_criteria = ticket.get("close_criteria", "")
    touches = ", ".join(ticket.get("touches") or []) or "none"

    # Load the instruction text from the configurable template
    template = load_prompt_template("review", root)
    instruction = render_prompt(
        template,
        ticket_id=tid,
        title=title,
        close_criteria=close_criteria,
        touches=touches,
    ).strip()

    # Build the trusted instruction layer with base instruction + prior findings.
    # No ticket ID here by design — TICKET TITLE below already identifies the
    # ticket to the model, so this stays a stable, cacheable prefix across tickets.
    trusted_parts = [instruction]

    declared_touches = ticket.get("touches") or []

    project_guidance = load_project_guidance(
        root, cfg, step="review", relevant_paths=declared_touches
    )
    if project_guidance:
        trusted_parts.append(project_guidance)

    arch_excerpt, arch_component = get_bounded_architecture_excerpt(
        root, declared_touches, cfg=cfg, step="review"
    )
    if arch_excerpt:
        trusted_parts.append(arch_excerpt)

    change_notes = ticket.get("change_notes") or {}
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
    review_findings = ticket.get("review_findings") or []
    if review_findings:
        trusted_parts.append(
            "## Prior review findings — confirm each is resolved\n\n"
            "The following items (see PRIOR REVIEW FINDINGS below) were raised in a "
            "previous review. Confirm each one is addressed before issuing an `approved` "
            "verdict. If any item is still outstanding, verdict must be `changes_requested` "
            "with findings referencing the unresolved items."
        )

    contract_audit = ticket.get("acceptance_contract_audit") or {}
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
        _components.append(arch_component)
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

    return build_prompt(
        full_instruction,
        untrusted_sections=untrusted_sections,
    )


def describe_review_payload(
    ticket: dict,
    commit_messages: str = "",
    project_root: Path | None = None,
    cfg: dict | None = None,
) -> list[dict]:
    """Return a machine-readable breakdown of every component in the review
    prompt payload -- byte/token estimate, source, pipeline step, and whether
    it's always injected or selected because of the ticket. Component
    metadata only; never includes actual ticket/findings text (TICK-306
    payload audit).
    """
    components: list = []
    build_review_prompt(ticket, commit_messages, project_root, cfg, _components=components)
    return [c.as_dict() for c in components]


def build_fix_prompt(
    ticket: dict,
    diff: str,
    findings: str,
    project_root: Path | None = None,
    cfg: dict | None = None,
    *,
    _components: list | None = None,
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

    Returns:
        A fully-rendered prompt string safe to pass to the fix agent.
    """
    root = project_root if project_root is not None else Path.cwd()
    diff, _ = truncate_to_budget(diff, get_payload_budget("fix", cfg))
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
    ).strip()

    trusted_parts = [instruction]

    declared_touches = ticket.get("touches") or []

    project_guidance = load_project_guidance(
        root, cfg, step="review", relevant_paths=declared_touches
    )
    if project_guidance:
        trusted_parts.append(project_guidance)

    arch_excerpt, arch_component = get_bounded_architecture_excerpt(
        root, declared_touches, cfg=cfg, step="fix"
    )
    if arch_excerpt:
        trusted_parts.append(arch_excerpt)

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
        _components.append(arch_component)
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
) -> list[dict]:
    """Return a machine-readable breakdown of every component in the fix
    prompt payload -- byte/token estimate, source, pipeline step, and whether
    it's always injected or selected because of the ticket/diff. Component
    metadata only; never includes actual ticket/diff/findings text (TICK-306
    payload audit).
    """
    components: list = []
    build_fix_prompt(ticket, diff, findings, project_root, cfg, _components=components)
    return [c.as_dict() for c in components]


def run_review_agent(
    prompt: str,
    ticket: dict,
    cfg: dict | None = None,
) -> ReviewResult:
    """
    Run a review agent subprocess with driver resolution.

    When cfg is None, defaults to hardcoded ["claude", "-p", prompt] for
    backward compatibility. When cfg is provided, uses resolve_driver and
    expand_driver to determine the review executor and build the command.

    Args:
        prompt: The review prompt to pass to the agent.
        ticket: The ticket dict (used for driver resolution).
        cfg: Loaded LaneGate config dict. When None, defaults to hardcoded claude.

    Returns:
        A ReviewResult with the parsed verdict, notes, and findings.
    """
    import subprocess

    if cfg is None:
        cmd = ["claude", "-p", prompt]
        env = None
    else:
        from lanegate.executor import build_executor_cmd, get_executor_config, resolve_executor_env
        from lanegate.orchestrate import _build_env, expand_driver, resolve_driver

        driver_name = resolve_driver("review", ticket, cfg)
        driver_cfg = expand_driver(driver_name, cfg)
        executor_type = driver_cfg.get("type", driver_name)
        effective_cfg = dict(cfg, executor=executor_type) if executor_type != cfg.get("executor") else cfg
        cmd = build_executor_cmd(executor_type, prompt, effective_cfg)
        executor_cfg = get_executor_config(executor_type, cfg)
        env = resolve_executor_env(executor_cfg)
        env = _build_env(driver_cfg, base_env=env)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True, encoding="utf-8",
            timeout=300,
        )
        if result.returncode != 0:
            return ReviewResult(
                verdict="changes_requested",
                notes=f"Review subprocess exited with code {result.returncode}",
            )

        # Extract the last JSON object containing "verdict" from output
        import re

        output = result.stdout.strip()
        matches = re.findall(r'\{[^{}]*"verdict"[^{}]*\}', output, re.DOTALL)
        raw_for_parse = matches[-1] if matches else output
        return parse_review_result(raw_for_parse)
    except Exception as exc:
        return ReviewResult(
            verdict="changes_requested",
            notes=f"Review agent failed: {exc}",
        )


def build_drift_check_prompt(
    ticket: dict,
    original_diff: str,
    fix_diff: str,
    findings: str,
    project_root: Path | None = None,
    cfg: dict | None = None,
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

    Returns:
        A fully-rendered prompt string safe to pass to the drift-check agent.
    """
    root = project_root if project_root is not None else Path.cwd()
    original_diff, _ = truncate_to_budget(original_diff, get_payload_budget("fix", cfg))
    fix_diff, _ = truncate_to_budget(fix_diff, get_payload_budget("fix", cfg))
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
    ).strip()

    trusted_parts = [instruction]

    project_guidance = load_project_guidance(
        root, cfg, step="review", relevant_paths=ticket.get("touches") or []
    )
    if project_guidance:
        trusted_parts.append(project_guidance)

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
