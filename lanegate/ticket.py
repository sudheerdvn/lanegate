"""
ticket.py — ticket file parse/write, schema, prefix-aware glob.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import uuid
from pathlib import Path, PurePosixPath

import yaml

from lanegate.config import (
    is_auto_fix_lane,
    is_high_reasoning_ticket,
    load_config,
    resolve_trunk_branch,
)

# Ordered frontmatter keys for clean round-trip dumps
_FRONTMATTER_KEY_ORDER = [
    "id",
    "title",
    "status",
    "status_changed_at",
    "lifecycle_events_summary",
    "lifecycle_events",
    "milestone",
    "batch",
    "priority",
    "touches",
    "file_skeletons_ref",
    "file_skeletons_summary",
    "file_skeletons",
    "change_notes_summary",
    "change_notes",
    "acceptance_matrix",
    "overlap_review",
    "acceptance_contract_audit_summary",
    "acceptance_contract_audit",
    "parallel_safe",
    "autonomy",
    "source",
    "trusted",
    "model",
    "executor",
    "reviewer",
    "analyze_session_id",
    "analyzed_at_sha",
    "pre_complete_verified_sha",
    "implement_session_id",
    "implement_mode",
    "fix_session_id",
    "feature_flag",
    "depends_on",
    "worktree",
    "branch",
    "companion_repos",
    "close_criteria",
    "invariants",
    "review_driver",
    "review_model",
    "review_model_pin",
    "review_independence",
    "reviewed_at",
    "review_verdict",
    "review_summary",
    "review_findings",
    "review_retry_after",
    "review_retry_attempt",
    "verification",
    "auto_fix_attempts",
    "requires_human_merge",
    "human_merge_reason",
    "rebase_conflict_files",
]

# Frontmatter needs to remain compact and safely serializable even when an
# external command returns a verbose error (for example, an echoed prompt).
_MAX_SCALAR_LEN = 8000


def _atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace *path* with text written beside it.

    Writing the temporary file in the destination directory keeps ``replace``
    on the same filesystem, so readers observe either the complete old file or
    the complete new file.
    """
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        # newline="": write \n bytes as-is. Without this, Windows' default
        # text-mode translation expands every \n to \r\n, silently breaking
        # anything that measures byte length from the pre-write string (e.g.
        # file_skeletons_summary["bytes"]) and adding line-ending diff noise.
        tmp_path.write_text(text, encoding=encoding, newline="")
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass

# Terminal means no further lifecycle work is scheduled for the ticket.  It
# deliberately includes failed and closed tickets, which are archived on the
# board but do not provide work a dependent ticket can build upon.
TERMINAL_STATUSES = frozenset({"merged", "validated", "done", "failed", "closed"})

# Dependency eligibility is narrower than terminality: only statuses whose
# work is present in main may unblock a dependent ticket.
DEPENDENCY_SATISFIED_STATUSES = frozenset({"merged", "validated", "done"})

# Review findings are deliberately absent here: they carry a per-attempt
# suffix, so attention_summary reads them via latest_review_findings() instead
# of substring-matching a fixed header.
_ATTENTION_SECTION_HEADERS = (
    "## Needs Review Reason",
    "## Failure Reason",
    "## Hibernation Reason",
)

# A hibernation carrying this marker is retried by resume-watch rather than
# requiring an operator decision.  The classifier lives here because both the
# orchestrator and human-attention views must make exactly the same split.
_RATE_LIMIT_MARKER = "rate limit or quota interruption"

# A hibernation carrying this marker means every eligible independent
# reviewer was cooling down (not a subprocess rate-limit response) -- a
# ticket-level state resolvable by waiting out review_retry_after, distinct
# from both _RATE_LIMIT_MARKER above and the permanent no-reviewer-available
# needs_review escalation.
_REVIEWER_COOLDOWN_MARKER = "temporarily unavailable"
_NON_RATE_LIMIT_HARD_ERROR_PATTERNS = (
    r"\binvalid_request_error\b",
    r"\bstatus[\"']?\s*:\s*400\b",
    r"\brequires a newer version of codex\b",
    r"\bmodel metadata\b.{0,120}\bnot found\b",
    r"\bunknown model\b",
    r"\bmodel .* does not exist\b",
)


def _has_non_rate_limit_hard_error(text: str) -> bool:
    """Return whether a rate-limit-looking hibernation is actually fatal."""
    lowered = text.lower()
    return any(re.search(pattern, lowered) for pattern in _NON_RATE_LIMIT_HARD_ERROR_PATTERNS)


def _active_rate_limit_hibernation(ticket: dict) -> bool:
    """Whether resume-watch, rather than a human, should resume this hibernation.

    The rate-limit marker can land in the ticket body (## Hibernation Reason)
    or, for a hibernated review_pending ticket, in the review_pending_reason
    frontmatter field instead — both must be checked or such tickets get
    misclassified as needing a human.
    """
    text = "\n".join(
        part for part in (ticket.get("_body") or "", ticket.get("review_pending_reason") or "") if part
    )
    return _RATE_LIMIT_MARKER in text and not _has_non_rate_limit_hard_error(text)


def _active_reviewer_cooldown_hibernation(ticket: dict) -> bool:
    """Whether a hibernated review_pending ticket is waiting out a reviewer cooldown.

    Distinct from ``_active_rate_limit_hibernation``: this marks every
    eligible independent reviewer cooling down, not a genuine subprocess
    rate-limit response, so next_batch/cmd_next can gate dispatch on the
    recorded ``review_retry_after`` window instead of resume-watch's
    rate-limit handling.
    """
    return _REVIEWER_COOLDOWN_MARKER in (ticket.get("review_pending_reason") or "")


def reviewer_cooldown_retry_pending(ticket: dict, *, now: datetime.datetime | None = None) -> bool:
    """Whether a reviewer-cooldown hibernation's retry window hasn't elapsed.

    next_batch/cmd_next call this to decide whether a hibernated
    review_pending ticket is still eligible for dispatch. Fails closed to
    "retry eligible now" -- returns False, same as before review_retry_after
    existed -- for a pre-migration ticket with no recorded retry time and for
    a malformed/unparsable timestamp, rather than skipping the ticket forever
    or raising.
    """
    if not _active_reviewer_cooldown_hibernation(ticket):
        return False
    raw = ticket.get("review_retry_after")
    if not raw:
        return False
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        retry_at = datetime.datetime.fromisoformat(text)
    except ValueError:
        return False
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=datetime.UTC)
    return retry_at > (now or datetime.datetime.now(datetime.UTC))

# Statuses shown on the board — unknown statuses are bucketed under OTHER
# draft: intent captured, analysis not yet done. Not terminal, not a lock status.
# failed: executor exited non-zero or produced no commits; not eligible for merge.
# closed: investigated, no code fix needed, not going through the merge pipeline.
_STANDARD_STATUSES = [
    "draft",
    "in_progress",
    "hibernated",
    "needs_review",
    "code_complete",
    "in_review",
    "merged",
    "validated",
    "done",
    "failed",
    "open",
    "blocked",
    "backlog",
    "deferred",
    "closed",
]

# "green"/"yellow"/"red" are risk-based autonomy lanes (TICK-467): green and
# yellow stay on the automatic amend/re-analyze -> fix -> re-review path like
# "full", while red always escalates to a human regardless of retry budget.
_VALID_AUTONOMY = frozenset({"full", "supervised", "manual", "green", "yellow", "red"})
_VALID_EXECUTOR_TYPES = frozenset(
    {
        "claude",
        "claude-subagent",
        "claude-process",
        "aider",
        "openhands",
        "codex",
        "ollama",
        "gemini",  # deprecated 2026-06-18, superseded by "agy" (Antigravity CLI)
        "agy",
        "continue",
    }
)
_VALID_EXECUTORS = _VALID_EXECUTOR_TYPES
_VALID_REVIEWERS = _VALID_EXECUTOR_TYPES | {"human"}


def _valid_file_skeletons_ref(ref: str) -> bool:
    path = Path(ref)
    parts = path.parts
    return (
        not path.is_absolute()
        and ".." not in parts
        and len(parts) == 4
        and parts[0] == ".lanegate"
        and parts[1] == "context"
        and parts[3] == "file_skeletons.json"
    )


def _named_driver_keys(cfg: dict | None) -> frozenset[str]:
    """Names a ticket's `executor`/`reviewer` field may legitimately reference,
    beyond the built-in driver types: entries under `drivers:` (per-step named
    instances), plus named executor instances under `executors:` that carry a
    `type` field (TICK-088) — the older per-type override block (e.g.
    `executors: {aider: {max_parallel: 3}}`) has no `type` field and its key IS
    the driver type already covered by _VALID_EXECUTOR_TYPES, so it's excluded
    here to avoid treating arbitrary override-only keys as valid references.
    """
    if not isinstance(cfg, dict):
        return frozenset()

    names: set[str] = set()

    drivers = cfg.get("drivers")
    if isinstance(drivers, dict):
        names.update(k for k in drivers if isinstance(k, str))

    executors = cfg.get("executors")
    if isinstance(executors, dict):
        names.update(
            k
            for k, entry in executors.items()
            if isinstance(k, str) and isinstance(entry, dict) and entry.get("type") is not None
        )

    return frozenset(names)


def _validate_safeguards(safeguards: dict | None) -> list[str]:
    """Validate the safeguards field structure.

    Returns error strings if invalid; empty list if valid or None.
    """
    if safeguards is None:
        return []

    if not isinstance(safeguards, dict):
        return [f"safeguards must be a dict or None, got: {type(safeguards).__name__}"]

    errors: list[str] = []
    valid_stages = {"pre_complete", "pre_merge", "post_merge"}

    for stage, guards in safeguards.items():
        if stage not in valid_stages:
            errors.append(f"safeguards: unknown stage {stage!r} (expected one of {sorted(valid_stages)})")

        # Validate the guard value(s) for this stage
        if isinstance(guards, str):
            # Single guard string is OK
            pass
        elif isinstance(guards, list):
            # List of guard strings
            if not all(isinstance(g, str) for g in guards):
                errors.append(f"safeguards[{stage!r}]: all guards must be strings")
        elif guards is None:
            # None is allowed to mean "no guards for this stage"
            pass
        else:
            errors.append(
                f"safeguards[{stage!r}]: must be a string, list of strings, or None, "
                f"got: {type(guards).__name__}"
            )

    return errors


_ACCEPTANCE_MATRIX_FIELDS = (
    "invariants", "adversarial_cases", "compatibility_cases", "regression_tests",
)
def validate_acceptance_matrix(matrix: object, *, required: bool = False) -> list[str]:
    """Validate the analyzer-produced contract retained on high-risk tickets."""
    if matrix is None:
        return ["acceptance_matrix is required for this high-risk ticket"] if required else []
    if not isinstance(matrix, dict):
        return ["acceptance_matrix must be a mapping"]
    errors: list[str] = []
    for field in _ACCEPTANCE_MATRIX_FIELDS:
        value = matrix.get(field)
        if value is None and not required:
            continue
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            errors.append(f"acceptance_matrix.{field} must be a list of non-empty strings")
        elif required and not value:
            errors.append(f"acceptance_matrix.{field} must be a non-empty list of non-empty strings")
    return errors


def validate_overlap_review(review: object) -> list[str]:
    """Validate an ordering or stacked-review declaration for an overlap."""
    if not isinstance(review, dict):
        return ["overlap_review must be a mapping"]
    if review.get("mode") not in {"dependencies", "stacked_review"}:
        return ["overlap_review.mode must be 'dependencies' or 'stacked_review'"]
    ticket_ids = review.get("ticket_ids")
    if not isinstance(ticket_ids, list) or not ticket_ids or not all(
        isinstance(ticket_id, str) and ticket_id.strip() for ticket_id in ticket_ids
    ):
        return ["overlap_review.ticket_ids must be a non-empty list of ticket IDs"]
    return []


def validate_ticket(meta: dict, cfg: dict | None = None) -> list[str]:
    """Return a list of error strings for a ticket meta dict; [] means valid."""
    errors: list[str] = []
    named_drivers = _named_driver_keys(cfg)
    valid_executors = _VALID_EXECUTOR_TYPES | named_drivers
    valid_reviewers = _VALID_REVIEWERS | named_drivers

    for key in ("id", "title", "status"):
        if not meta.get(key):
            errors.append(f"missing required field: {key!r}")

    status = meta.get("status")
    if status and status not in _STANDARD_STATUSES:
        errors.append(f"unknown status: {status!r} (expected one of {_STANDARD_STATUSES})")

    priority = meta.get("priority")
    if priority is not None:
        try:
            int(priority)
        except (TypeError, ValueError):
            errors.append(f"priority must be an integer, got: {priority!r}")

    touches = meta.get("touches")
    if touches is not None and not isinstance(touches, list):
        errors.append(f"touches must be a list, got: {type(touches).__name__}")

    errors.extend(validate_acceptance_matrix(meta.get("acceptance_matrix")))
    if meta.get("overlap_review") is not None:
        errors.extend(validate_overlap_review(meta["overlap_review"]))

    file_skeletons = meta.get("file_skeletons")
    if file_skeletons is not None:
        if not isinstance(file_skeletons, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in file_skeletons.items()
        ):
            errors.append("file_skeletons must be a dict of str -> str if present")

    file_skeletons_ref = meta.get("file_skeletons_ref")
    if file_skeletons_ref is not None:
        if not isinstance(file_skeletons_ref, str):
            errors.append("file_skeletons_ref must be a string if present")
        elif not _valid_file_skeletons_ref(file_skeletons_ref):
            errors.append(
                "file_skeletons_ref must be a repo-local .lanegate/context/<ticket-id>/file_skeletons.json path"
            )

    file_skeletons_summary = meta.get("file_skeletons_summary")
    if file_skeletons_summary is not None:
        if not isinstance(file_skeletons_summary, dict):
            errors.append("file_skeletons_summary must be a dict if present")
        else:
            files = file_skeletons_summary.get("files")
            byte_count = file_skeletons_summary.get("bytes")
            if not isinstance(files, int) or files < 0:
                errors.append("file_skeletons_summary.files must be a non-negative integer")
            if not isinstance(byte_count, int) or byte_count < 0:
                errors.append("file_skeletons_summary.bytes must be a non-negative integer")

    autonomy = meta.get("autonomy")
    if autonomy is not None and autonomy not in _VALID_AUTONOMY:
        errors.append(f"unknown autonomy: {autonomy!r} (expected one of {sorted(_VALID_AUTONOMY)})")

    auto_fix_attempts = meta.get("auto_fix_attempts")
    if auto_fix_attempts is not None:
        try:
            if int(auto_fix_attempts) < 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(
                f"auto_fix_attempts must be a non-negative integer, got: {auto_fix_attempts!r}"
            )

    executor_val = meta.get("executor")
    if executor_val is not None and executor_val not in valid_executors:
        errors.append(
            f"unknown executor: {executor_val!r} "
            f"(expected one of {sorted(_VALID_EXECUTOR_TYPES)} or a named driver)"
        )

    reviewer_val = meta.get("reviewer")
    if reviewer_val is not None and reviewer_val not in valid_reviewers:
        errors.append(
            f"unknown reviewer: {reviewer_val!r} "
            f"(expected one of {sorted(_VALID_REVIEWERS)} or a named driver)"
        )

    milestone = meta.get("milestone")
    if milestone is not None:
        if not isinstance(milestone, str) or not milestone.strip():
            errors.append("milestone must be a non-empty string if present")

    source = meta.get("source")
    if source is not None and not isinstance(source, str):
        errors.append("source must be a string if present")

    trusted = meta.get("trusted")
    if trusted is not None and not isinstance(trusted, bool):
        errors.append("trusted must be a boolean if present")

    analyze_session_id = meta.get("analyze_session_id")
    if analyze_session_id is not None and not isinstance(analyze_session_id, str):
        errors.append("analyze_session_id must be a string if present")

    analyzed_at_sha = meta.get("analyzed_at_sha")
    if analyzed_at_sha is not None and not isinstance(analyzed_at_sha, str):
        errors.append("analyzed_at_sha must be a string if present")

    implement_session_id = meta.get("implement_session_id")
    if implement_session_id is not None and not isinstance(implement_session_id, str):
        errors.append("implement_session_id must be a string if present")

    fix_session_id = meta.get("fix_session_id")
    if fix_session_id is not None and not isinstance(fix_session_id, str):
        errors.append("fix_session_id must be a string if present")

    review_model_pin = meta.get("review_model_pin")
    if review_model_pin is not None and (
        not isinstance(review_model_pin, str) or not review_model_pin.strip()
    ):
        errors.append("review_model_pin must be a non-empty string if present")

    safeguards = meta.get("safeguards")
    if safeguards is not None:
        errors.extend(_validate_safeguards(safeguards))

    return errors


def milestone_near_miss_warnings(tickets: list[dict], active_milestone: str | None) -> list[dict]:
    """Detect milestone values in tickets that appear to be typos/near-misses of the active milestone.

    For example, if active_milestone='v1.5', this returns warnings for tickets with
    milestone='1.5' (missing v prefix). Returns a list of warning dicts, each with
    'ticket_id', 'ticket_milestone', and 'active_milestone' keys.

    Used when running orchestrate with a --milestone filter to surface tickets that
    would have been silently skipped due to an exact-string-match but are likely
    intended for the same milestone.
    """
    if not active_milestone:
        return []

    warnings: list[dict] = []

    def normalize_for_comparison(m: str) -> str:
        """Normalize a milestone value for near-miss detection."""
        return m.lower().strip()

    active_normalized = normalize_for_comparison(active_milestone)

    # Detect common near-miss patterns: missing/extra prefixes (v, V).
    def is_near_miss(ticket_milestone: str) -> bool:
        ticket_normalized = normalize_for_comparison(ticket_milestone)

        # Already exact match
        if ticket_normalized == active_normalized:
            return False

        # Check for v-prefix mismatch: v1.5 vs 1.5
        if active_normalized.startswith("v"):
            without_v = active_normalized[1:]
            if ticket_normalized == without_v:
                return True
        elif ticket_normalized.startswith("v"):
            without_v = ticket_normalized[1:]
            if without_v == active_normalized:
                return True

        return False

    for ticket in tickets:
        ticket_milestone = ticket.get("milestone")
        if ticket_milestone and is_near_miss(ticket_milestone):
            warnings.append(
                {
                    "ticket_id": ticket.get("id", "unknown"),
                    "ticket_milestone": ticket_milestone,
                    "active_milestone": active_milestone,
                }
            )

    return warnings


def ticket_glob(tickets_dir: Path, prefix: str) -> list[Path]:
    """Return sorted ticket files matching <prefix>-*.md, case-insensitive on the prefix."""
    pat = re.compile(rf"^{re.escape(prefix)}-\d+(?:-[^/]*)?\.md$", re.IGNORECASE)
    return sorted(p for p in tickets_dir.iterdir() if p.is_file() and pat.match(p.name))


def _isoformat_date(val: datetime.date | datetime.datetime | str) -> str:
    if isinstance(val, (datetime.date, datetime.datetime)):
        s = val.isoformat()
        if s.endswith("+00:00"):
            s = s[:-6] + "Z"
        return s
    return str(val)


def _normalize_dates(meta: dict) -> dict:
    """Coerce any date/datetime scalar YAML implicitly typed (e.g. `created: 2026-07-03`)
    back to an ISO string, so every ticket dict is JSON-serializable by construction."""
    for key, value in meta.items():
        if isinstance(value, (datetime.date, datetime.datetime)):
            meta[key] = _isoformat_date(value)
    return meta


def parse_ticket(path: Path) -> dict | None:
    """Return ticket meta dict with _body and _path private keys, or None if unparseable."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    # File format: ---\n{frontmatter}\n---\n{body}
    # Use regex to split on line-anchored --- delimiters (not substring matches)
    # This prevents --- in titles or body from corrupting the parse.
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not match:
        return None
    frontmatter_text = match.group(1)
    body = match.group(2).strip()
    meta = yaml.safe_load(frontmatter_text) or {}
    meta = _normalize_dates(meta)
    meta["_body"] = body
    meta["_path"] = path

    cnotes = load_change_notes(meta)
    if cnotes:
        meta["change_notes"] = cnotes

    audit = load_acceptance_contract_audit(meta)
    if audit:
        meta["acceptance_contract_audit"] = audit

    l_events = load_lifecycle_events(meta)
    if l_events:
        meta["lifecycle_events"] = l_events

    return meta


REVIEW_FINDINGS_HEADER = "## Review Findings"

# Matches both the historical single ``## Review Findings`` header and the
# per-attempt ``## Review Findings (attempt N)`` form written since TICK-343.
_REVIEW_FINDINGS_HEADER_RE = re.compile(
    r"^##[ \t]*Review Findings(?:[ \t]*\(attempt[ \t]*(\d+)\))?[ \t]*$",
    re.MULTILINE,
)


def _find_next_h2_heading(text: str) -> int:
    """Return character index of next H2 section header (``\\n## `` not ``\\n###``) in ``text``, or -1."""
    match = re.search(r"\n##(?![#])", text)
    return match.start() if match else -1


def review_findings_sections(body: str) -> list[tuple[str, str]]:
    """Return ``(header, text)`` for each review-findings section, oldest first.

    One canonical parser for a section header that three unrelated modules read
    (``lifecycle.cmd_review`` writes it, ``orchestrate.autofix`` feeds it to the
    fix agent, ``attention_summary`` below renders it on the board).  A private
    copy in any of them would silently stop seeing per-attempt sections.
    """
    sections: list[tuple[str, str]] = []
    for match in _REVIEW_FINDINGS_HEADER_RE.finditer(body or ""):
        rest = body[match.end():]
        next_heading = _find_next_h2_heading(rest)
        end = len(rest) if next_heading == -1 else next_heading
        sections.append((match.group(0).strip(), rest[:end].strip()))
    return sections


def latest_review_findings(ticket: dict) -> str:
    """Return the most recent review's findings text, or ``""`` if there are none."""
    sections = review_findings_sections(ticket.get("_body", ""))
    return sections[-1][1] if sections else ""


def next_review_findings_header(body: str) -> str:
    """Return the header for the next review attempt on this body.

    Re-review must not overwrite the previous reviewer's findings: a
    changes_requested → auto-fix → re-review cycle is exactly when both sets
    matter.  An older ticket whose only section is the unnumbered
    ``## Review Findings`` counts as attempt 1 and is left in place.
    """
    return f"{REVIEW_FINDINGS_HEADER} (attempt {len(review_findings_sections(body)) + 1})"


def _body_section(ticket: dict, header: str) -> str:
    body = ticket.get("_body", "")
    if header not in body:
        return ""
    _, _, rest = body.partition(header)
    after = rest.lstrip("\n")
    next_heading = _find_next_h2_heading(after)
    return after if next_heading == -1 else after[:next_heading]


def _upsert_body_section(body: str, header: str, new_section_text: str) -> str:
    """Replace or append a section in ticket body."""
    if not new_section_text:
        return body
    if header in body:
        before, _, after = body.partition(header)
        next_heading = _find_next_h2_heading(after)
        tail = "" if next_heading == -1 else after[next_heading:]
        return before.rstrip() + "\n\n" + new_section_text.strip() + "\n" + tail
    else:
        return body.rstrip() + "\n\n" + new_section_text.strip() + "\n"


def render_change_notes_section(change_notes: dict[str, str]) -> str:
    """Render structured change_notes dict into readable '## Change Notes' markdown section."""
    if not isinstance(change_notes, dict) or not change_notes:
        return ""
    lines = ["## Change Notes"]
    for path, note in change_notes.items():
        if isinstance(path, str) and isinstance(note, str):
            lines.append(f"**{path}**: {note}")
    return "\n".join(lines)


def load_change_notes(ticket: dict) -> dict[str, str]:
    """Return change_notes dict from ticket (legacy frontmatter dict or hydrated from body)."""
    raw = ticket.get("change_notes")
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    sec = _body_section(ticket, "## Change Notes")
    if not sec:
        return {}
    res: dict[str, str] = {}
    for line in sec.splitlines():
        line_str = line.strip()
        if not line_str:
            continue
        line_str = re.sub(r"^[-*]\s+", "", line_str)
        m = re.match(r"^(?:\*\*(.*?)\*\*|([^\n:]+))\s*:\s*(.*)$", line_str)
        if m:
            path = (m.group(1) or m.group(2) or "").strip()
            note = (m.group(3) or "").strip()
            if path and note:
                res[path] = note
    return res


_CROSS_TICKET_NOTES_STATUSES = frozenset({"merged", "done"})
_CROSS_TICKET_NOTES_BUDGET_BYTES = 4000


def find_control_plane_touch_overlaps(
    ticket: dict, tickets_dir: Path, cfg: dict | None = None, *, exclude_id: str | None = None,
) -> list[dict[str, object]]:
    """Return active high-risk tickets sharing an exact declared touch.

    High-risk classification comes from portable ticket metadata rather than
    package-specific paths, so projects embedding LaneGate use the same gate.
    """
    ticket_id = exclude_id or ticket.get("id")
    touches = {str(path) for path in (ticket.get("touches") or [])}
    if not is_high_reasoning_ticket(ticket) or not touches or not tickets_dir.exists():
        return []
    prefix = (cfg or {}).get("ticket_prefix", "TICK")
    all_tickets, _ = load_all_tickets(tickets_dir, prefix, cfg)
    overlaps: list[dict[str, object]] = []
    for other in all_tickets:
        other_id = other.get("id")
        if not other_id or other_id == ticket_id or other.get("status") in TERMINAL_STATUSES:
            continue
        if not is_high_reasoning_ticket(other):
            continue
        shared = sorted(touches & {str(path) for path in (other.get("touches") or [])})
        if shared:
            overlaps.append({"ticket_id": other_id, "paths": shared})
    return overlaps


def collect_cross_ticket_change_notes(
    ticket: dict,
    tickets_dir: Path,
    cfg: dict | None = None,
    exclude_id: str | None = None,
) -> str:
    """Return a bounded '## Prior Change Notes' section surfacing what earlier
    merged/done tickets recorded (via change_notes) about files *ticket* also
    touches.

    This is the git-tracked replacement for the old per-file
    ``.lanegate/notes/<flat_path>.md`` mechanism (dead: written only in the
    ticket's own worktree, never at the repo_root the reader used, and
    gitignored besides). change_notes lives in ticket frontmatter, which is
    git-tracked and survives worktree merges, so cross-ticket lookup over it
    works from any repo_root without extra plumbing.

    Returns "" when there is nothing relevant to surface.
    """
    from lanegate.prompts import truncate_to_budget

    exclude = exclude_id or ticket.get("id")
    touches = [str(p) for p in (ticket.get("touches") or []) if p]
    if not touches or not tickets_dir.exists():
        return ""
    touches_set = set(touches)

    prefix = (cfg or {}).get("ticket_prefix", "TICK") if cfg else "TICK"
    all_tickets, _ = load_all_tickets(tickets_dir, prefix, cfg)

    entries: list[str] = []
    for other in all_tickets:
        other_id = other.get("id")
        if not other_id or other_id == exclude:
            continue
        if other.get("status") not in _CROSS_TICKET_NOTES_STATUSES:
            continue
        other_touches = set(str(p) for p in (other.get("touches") or []) if p)
        overlap = sorted(touches_set & other_touches)
        if not overlap:
            continue
        other_notes = load_change_notes(other)
        for path in overlap:
            note = other_notes.get(path)
            if note:
                entries.append(f"**{path}** ({other_id}): {note}")

    if not entries:
        return ""

    section = "## Prior Change Notes\n" + "\n".join(entries)
    section, _ = truncate_to_budget(section, _CROSS_TICKET_NOTES_BUDGET_BYTES)
    return section


def render_acceptance_contract_audit_section(audit: dict) -> str:
    """Render structured acceptance_contract_audit dict into readable '## Acceptance Contract Audit' markdown section."""
    if not isinstance(audit, dict) or not audit:
        return ""
    ok = bool(audit.get("ok"))
    findings = [str(f) for f in audit.get("findings") or []]
    omitted = [str(item) for item in audit.get("omitted_items") or []]
    checked = [str(item) for item in audit.get("checked_items") or []]
    sources = [str(s) for s in audit.get("sources") or []]

    status_str = f"ok ({len(findings)} findings)" if ok else f"failed ({len(findings)} findings)"
    lines = ["## Acceptance Contract Audit", f"**Status**: {status_str}"]

    if findings:
        lines.append("\n**Findings**:")
        for f in findings:
            lines.append(f"- {f}")

    if omitted:
        lines.append("\n**Omitted Items**:")
        for item in omitted:
            lines.append(f"- {item}")

    if checked:
        lines.append("\n**Checked Items**:")
        for item in checked:
            lines.append(f"- {item}")

    if sources:
        lines.append("\n**Sources**:")
        for s in sources:
            lines.append(f"- {s}")

    return "\n".join(lines)


def load_acceptance_contract_audit(ticket: dict) -> dict:
    """Return acceptance_contract_audit dict from ticket (legacy frontmatter dict or hydrated from body)."""
    raw = ticket.get("acceptance_contract_audit")
    if isinstance(raw, dict) and "ok" in raw and isinstance(raw["ok"], bool):
        return raw
    sec = _body_section(ticket, "## Acceptance Contract Audit")
    if not sec:
        return {}

    ok = True
    findings: list[str] = []
    omitted_items: list[str] = []
    checked_items: list[str] = []
    sources: list[str] = []

    current_target: list[str] | None = None
    for line in sec.splitlines():
        lstr = line.strip()
        if not lstr:
            continue
        if lstr.startswith("**Status**:") or lstr.startswith("Status:"):
            status_text = lstr.partition(":")[2].strip().lower()
            if "failed" in status_text:
                ok = False
            elif "ok" in status_text:
                ok = True
            continue

        lowered = lstr.lower()
        if "findings" in lowered and (lstr.startswith("**") or lstr.startswith("###") or lstr.endswith(":")):
            current_target = findings
            continue
        elif "omitted" in lowered and (lstr.startswith("**") or lstr.startswith("###") or lstr.endswith(":")):
            current_target = omitted_items
            continue
        elif "checked" in lowered and (lstr.startswith("**") or lstr.startswith("###") or lstr.endswith(":")):
            current_target = checked_items
            continue
        elif "sources" in lowered and (lstr.startswith("**") or lstr.startswith("###") or lstr.endswith(":")):
            current_target = sources
            continue

        if current_target is not None and (lstr.startswith("-") or lstr.startswith("*")):
            item = lstr.lstrip("-* ").strip()
            if item and item.lower() != "none":
                current_target.append(item)

    if "failed" not in sec.lower() and not findings:
        ok = True
    elif findings and "ok" not in sec.lower().split("\n")[0]:
        ok = False

    return {
        "ok": ok,
        "findings": findings,
        "omitted_items": omitted_items,
        "checked_items": checked_items,
        "sources": sources,
    }


def render_lifecycle_timeline_section(events: list[dict]) -> str:
    """Render structured lifecycle_events list into readable '## Lifecycle Timeline' markdown section."""
    if not isinstance(events, list) or not events:
        return ""
    lines = ["## Lifecycle Timeline"]
    for evt in events:
        if not isinstance(evt, dict):
            continue
        stamp = evt.get("at", "")
        if stamp:
            stamp = _isoformat_date(stamp)
        else:
            stamp = ""
        from_status = evt.get("from_status")
        to_status = evt.get("to_status")
        summary = evt.get("summary", "")
        evt_name = evt.get("event", "")

        transition = ""
        if from_status and to_status:
            transition = f"{from_status} → {to_status}"
        elif to_status:
            transition = to_status

        if evt_name and summary and evt_name != summary:
            detail = f"{evt_name}: {summary}"
        else:
            detail = summary or evt_name

        text = " — ".join(part for part in (transition, detail) if part)
        if stamp:
            lines.append(f"- {stamp}: {text}")
        else:
            lines.append(f"- {text}")
    return "\n".join(lines)


def load_lifecycle_events(ticket: dict) -> list[dict]:
    """Return lifecycle_events list from ticket (legacy frontmatter list or hydrated from body)."""
    raw = ticket.get("lifecycle_events")
    if isinstance(raw, list) and raw and all(isinstance(x, dict) for x in raw):
        res = []
        for x in raw:
            item = dict(x)
            at_val = item.get("at")
            if at_val is not None:
                item["at"] = _isoformat_date(at_val)
            res.append(item)
        return res
    sec = _body_section(ticket, "## Lifecycle Timeline")
    if not sec:
        return []
    events: list[dict] = []
    for line in sec.splitlines():
        lstr = line.strip()
        if not (lstr.startswith("-") or lstr.startswith("*")):
            continue
        entry_text = lstr.lstrip("-* ").strip()
        if not entry_text:
            continue
        stamp = ""
        m = re.match(r"^(\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?)?)\s*:\s*(.*)$", entry_text)
        if m:
            stamp = m.group(1)
            rest = m.group(2)
        else:
            rest = entry_text

        summary = ""
        trans_part = rest
        detail_part = ""
        if " — " in rest:
            trans_part, _, detail_part = rest.partition(" — ")
        elif " - " in rest:
            trans_part, _, detail_part = rest.partition(" - ")

        from_status = None
        to_status = None
        if " → " in trans_part:
            fs, _, ts = trans_part.partition(" → ")
            from_status = fs.strip()
            to_status = ts.strip()
        elif " -> " in trans_part:
            fs, _, ts = trans_part.partition(" -> ")
            from_status = fs.strip()
            to_status = ts.strip()
        elif trans_part.strip() in {"open", "in_progress", "code_complete", "in_review", "hibernated", "done", "merged", "validated", "failed", "closed", "needs_review"}:
            to_status = trans_part.strip()
        else:
            if not detail_part:
                detail_part = trans_part
                trans_part = ""

        evt_name = ""
        summary = ""
        detail_str = detail_part.strip()
        if ": " in detail_str:
            evt_name, _, summary = detail_str.partition(": ")
            evt_name = evt_name.strip()
            summary = summary.strip()
        elif detail_str:
            summary = detail_str
            if summary in {"implementation_started", "status_changed", "needs_review", "review_verdict", "review_started", "review_completed", "merged", "hibernated", "reopened", "superseded", "failed"}:
                evt_name = summary
            elif summary == "worktree claimed for implementation" or (from_status == "open" and to_status == "in_progress" and "claimed" in summary):
                evt_name = "implementation_started"
            elif summary == "merge completed on main" or to_status == "merged":
                evt_name = "merged"
            elif summary == "lifecycle transition":
                evt_name = "status_changed"
            else:
                evt_name = summary
        elif from_status or to_status:
            evt_name = to_status or "transition"
            summary = ""
        else:
            evt_name = "transition"
            summary = ""

        if not evt_name:
            evt_name = summary or (trans_part.strip() if trans_part else "transition")

        events.append({
            "at": stamp,
            "event": evt_name,
            "from_status": from_status,
            "to_status": to_status,
            "summary": summary,
        })
    return events


def _summary_line(text: str, *, limit: int = 180) -> str:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^[-*]\s+", "", line)
        line = re.sub(r"^\[\d+\]\s*", "", line)
        line = re.sub(r"^\d+[.)]\s+", "", line)
        if len(line) > limit:
            return line[: limit - 1].rstrip() + "…"
        return line
    return ""


def _clean_attention_reason(reason: str) -> str:
    """Clean up verbose command representations and translate raw exit codes in attention summaries."""
    if not reason:
        return ""
    if "Command '['" in reason or 'Command "["' in reason:
        match = re.search(r"timed out after (\d+\s*\w*)", reason)
        if match:
            return f"Reviewer process timed out after {match.group(1)}"
        return "Reviewer subprocess execution failed"

    lowered = reason.lower()
    if "rate limit" in lowered or "quota" in lowered:
        # The rate-limit/quota phrase is more actionable than a generic exit-code
        # translation, and callers rely on this text to detect is_rate_limited.
        return reason

    match = re.search(r"executor exited (?:with code|code)?\s*(-?\d+)", reason)
    if match:
        code = int(match.group(1))
        meaning_map = {
            1: "general error",
            2: "CLI / configuration error",
            127: "command not found",
            130: "interrupted",
            -2: "interrupted",
            137: "process killed / out of memory",
            -9: "process killed / out of memory",
            143: "terminated",
            -15: "terminated",
        }
        meaning = meaning_map.get(code)
        if not meaning and code < 0:
            meaning = f"signal {abs(code)}"
        if meaning:
            return f"executor failed (exit code {code}: {meaning})"
        return f"executor failed (exit code {code})"

    return reason


def _reason_section_applies(status: str | None, verdict: str | None) -> bool:
    """Whether a ## Needs/Failure/Hibernation Reason body section still describes
    the ticket's *current* state.

    A ticket resumed via ``lanegate start`` (which bypasses ``cmd_reopen``'s
    body-stripping) can carry an old reason section through to a later, healthy
    status -- these are exactly the statuses/verdict where that section is still
    live. Shared by attention_summary() and get_ticket_summary() so the board's
    one-line reason and `lanegate summary`'s detailed reason never disagree
    about which tickets show one.
    """
    return status in {"needs_review", "failed", "hibernated"} or verdict == "changes_requested"


def attention_summary(ticket: dict) -> str:
    """Return a one-line reason why a ticket needs operator attention."""
    status = ticket.get("status")
    verdict = ticket.get("review_verdict")

    if _reason_section_applies(status, verdict):
        for header in _ATTENTION_SECTION_HEADERS:
            summary = _summary_line(_body_section(ticket, header))
            if summary:
                return _clean_attention_reason(summary)
        summary = _summary_line(latest_review_findings(ticket))
        if summary:
            return _clean_attention_reason(summary)

    findings = ticket.get("review_findings") or []
    if verdict == "changes_requested" and findings:
        first = str(findings[0]).strip()
        return _clean_attention_reason(_summary_line(first, limit=140))

    if verdict == "changes_requested" and ticket.get("review_summary"):
        return _clean_attention_reason(_summary_line(str(ticket["review_summary"]).strip(), limit=140))

    if status == "failed" and ticket.get("review_summary"):
        return _clean_attention_reason(_summary_line(str(ticket["review_summary"]).strip(), limit=140))

    if status == "hibernated" and ticket.get("review_summary"):
        return _clean_attention_reason(_summary_line(str(ticket["review_summary"]).strip(), limit=140))

    if status == "hibernated" and ticket.get("review_pending") and ticket.get("review_pending_reason"):
        return _clean_attention_reason(
            _summary_line(str(ticket["review_pending_reason"]).strip(), limit=140)
        )

    if status == "merged" and ticket.get("post_merge_diagnostic"):
        return _clean_attention_reason(_summary_line(str(ticket["post_merge_diagnostic"]).strip()))

    category = attention_category(ticket)
    if category == "awaiting_merge":
        if ticket.get("requires_human_merge"):
            files = ", ".join(ticket.get("rebase_conflict_files") or [])
            suffix = f"; inspect recovered files: {files}" if files else ""
            return f"Automated rebase conflict recovery; human merge approval required{suffix}"
        return "Approved; awaiting human merge decision"

    if category == "stuck":
        return "Hibernated for a non-rate-limit reason"

    default_reasons = {
        "escalated": "Manual review required",
        "failed": "Ticket failed; inspect log and worktree",
        "rejected": "Review changes requested",
        "merged_diagnostic": "Post-merge verification diagnostic recorded",
    }
    if category and category in default_reasons:
        return default_reasons[category]

    return ""


def attention_category(ticket: dict) -> str:
    """Return the operator-remediation category for a ticket, or ``""``.

    The category deliberately describes the next human decision instead of a
    lifecycle status: the Blocked screen combines several status families.
    """
    status = ticket.get("status")
    verdict = ticket.get("review_verdict")

    # These statuses represent an agent actively doing work.  An old verdict
    # must not make an in-flight ticket look like a queue item.
    if status == "in_progress":
        return ""
    if status == "in_review":
        autonomy = ticket.get("_effective_autonomy", ticket.get("autonomy", "supervised"))
        if verdict == "approved" and (
            ticket.get("requires_human_merge") or not is_auto_fix_lane(autonomy)
        ):
            return "awaiting_merge"
        return ""

    if status == "needs_review":
        return "escalated"
    if status == "failed":
        return "failed"
    # A resolved terminal status (closed/merged/validated/done) means the
    # ticket is done needing action, full stop -- a stale review_verdict left
    # over from before it reached that status (e.g. `lanegate supersede`
    # flips status without clearing review_verdict) must not resurrect it as
    # "rejected" forever. `failed` is excluded: it's terminal but still
    # actionable, handled explicitly above.
    if status in TERMINAL_STATUSES and status != "failed":
        if status == "merged" and ticket.get("post_merge_diagnostic"):
            return "merged_diagnostic"
        return ""
    if verdict == "changes_requested":
        return "rejected"
    if (
        status == "hibernated"
        and not _active_rate_limit_hibernation(ticket)
        and not _active_reviewer_cooldown_hibernation(ticket)
    ):
        return "stuck"
    return ""


def needs_attention(ticket: dict) -> bool:
    """Return whether a ticket is waiting for a human decision or intervention."""
    return bool(attention_category(ticket))


# Ordered most-specific-first: several needs_review paths share the same
# generic review_verdict=changes_requested/review_summary="blocked by
# orchestrate gate" shape (see downgrade_approved_review_to_needs_review in
# orchestrate/loop.py), so only the free-text reason recorded in the
# ## Needs Review Reason section reliably distinguishes *why* a ticket
# landed there. Patterns are matched against that text, lower-cased.
_NEEDS_REVIEW_CAUSE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("protected_path", ("hard-blocked categories", "security_sensitive_paths", "human review required")),
    ("scope_drift", ("outside touches list", "could not auto-claim")),
    ("static_analysis", ("static analysis findings", "safeguards failed", "safeguards unavailable")),
    ("conflict", ("conflict", "rebase")),
    (
        "auto_fix_exhausted",
        (
            "bounded auto-fix/re-review exhausted",
            "drift check failed after bounded auto-fix",
            "auto-fix attempts exhausted",
        ),
    ),
    (
        "no_independent_reviewer",
        ("no healthy independent reviewer is available", "retry budget exhausted"),
    ),
    ("review_rejection", ("reviewer", "review harness error")),
)

_NEEDS_REVIEW_RECOVERY_ADVICE: dict[str, str] = {
    "protected_path": (
        "hard-blocked path — human review required; inspect the diff, then: "
        "lanegate human-review {tid} --rationale \"...\""
    ),
    "scope_drift": (
        "extra files outside touches — claim them, then: "
        "lanegate reopen {tid} && lanegate run"
    ),
    "static_analysis": (
        "automated safeguard/static-analysis finding — fix it, then: "
        "lanegate reopen {tid} && lanegate run"
    ),
    "conflict": "merge conflict — resolve it: lanegate resolve-conflict {tid}",
    "auto_fix_exhausted": (
        "bounded auto-fix exhausted — make a targeted worktree repair; when ready, "
        "run: lanegate reopen {tid}, then lanegate review {tid}"
    ),
    "review_rejection": (
        "reviewer could not produce a verdict — inspect the log, then: "
        "lanegate reopen {tid} && lanegate run"
    ),
    "no_independent_reviewer": (
        "no independent reviewer configured or all eligible reviewers stayed "
        "unavailable past the retry budget. To clear just this ticket: "
        "lanegate human-review {tid} --rationale \"...\". A single-account "
        "setup will hit this on every ticket until the config changes: set "
        "review_fallback: same_model in .lanegate.yml (accepts same-model "
        "self-review) or configure a second reviewer/pool member."
    ),
    "rate_limit": (
        "reviewer/executor was rate limited — next: "
        "lanegate recover-rate-limited-reviews {tid} or lanegate run (auto-recovers on quota reset)"
    ),
    "rate_limit_auto_fix_attempted": (
        "reviewer/executor was rate limited, but this ticket already used an "
        "auto-fix attempt so unattended recovery is skipped — fix it, then: "
        "lanegate reopen {tid} && lanegate run"
    ),
    "unknown": "inspect worktree, then: lanegate reopen {tid} && lanegate run",
}


def classify_needs_review_cause(ticket: dict) -> str:
    """Structurally classify why a ``needs_review`` ticket landed there.

    Returns "" for a ticket that isn't currently needs_review. Otherwise one
    of: protected_path, scope_drift, static_analysis, conflict,
    auto_fix_exhausted, review_rejection, rate_limit, unknown. Board/API/orchestrator surfaces
    use this instead of a blanket "reopen && orchestrate" so a hard-blocked
    change (protected path, security-sensitive file) is routed to
    ``lanegate human-review`` rather than silently resubmitted for another
    automatic pass.
    """
    if ticket.get("status") != "needs_review":
        return ""
    text = "\n".join(
        part
        for part in (
            _body_section(ticket, "## Needs Review Reason"),
            str(ticket.get("review_summary") or ""),
        )
        if part
    ).lower()
    for cause, needles in _NEEDS_REVIEW_CAUSE_PATTERNS:
        if any(needle in text for needle in needles):
            return cause
    if _active_rate_limit_hibernation(ticket):
        return "rate_limit"
    return "unknown"


def needs_review_recovery_advice(ticket: dict) -> str:
    """Return a cause-specific recovery instruction for a needs_review ticket, or ""."""
    cause = classify_needs_review_cause(ticket)
    if not cause:
        return ""
    advice_key = cause
    if cause == "rate_limit" and ticket.get("auto_fix_attempts"):
        advice_key = "rate_limit_auto_fix_attempted"
    tid = ticket.get("id", "")
    return _NEEDS_REVIEW_RECOVERY_ADVICE[advice_key].format(tid=tid)


def append_status_history(ticket: dict, from_status: str, to_status: str, reason: str) -> None:
    """Record a status transition in the ticket's ## Status History section.

    Meant for transitions that move a ticket backward (e.g. code_complete
    reset to open) or otherwise override the normal forward flow -- without
    this, a ticket that's been reopened just looks like a fresh `open`
    ticket with no explanation for why it isn't further along, which reads
    as a regression instead of a deliberate, explained correction. Ordinary
    forward transitions (open -> in_progress -> code_complete -> ...) don't
    need this since the status itself is self-explanatory.

    Mutates ticket["_body"] in place; callers still call write_ticket()
    themselves as part of their existing save.
    """
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    line = f"- {stamp}: {from_status} → {to_status} ({reason})"
    body = ticket.get("_body", "")
    header = "## Status History"
    if header not in body:
        ticket["_body"] = body.rstrip() + f"\n\n{header}\n{line}\n"
        return
    before, _, after = body.partition(header)
    next_heading = _find_next_h2_heading(after)
    section, tail = (after, "") if next_heading == -1 else (after[:next_heading], after[next_heading:])
    ticket["_body"] = before + header + section.rstrip("\n") + f"\n{line}\n" + tail


def append_lifecycle_event(
    ticket: dict,
    *,
    event: str,
    summary: str = "",
    from_status: str | None = None,
    to_status: str | None = None,
) -> None:
    """Record a durable, structured lifecycle event and readable ticket timeline.

    ``lifecycle_events`` is intentionally ordinary ticket frontmatter so the
    API, TUI, and reports can consume it without parsing prose.  The matching
    ``## Lifecycle Timeline`` body section keeps the same information visible
    when someone opens the ticket file directly.
    """
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    record = {
        "at": stamp,
        "event": event,
        "from_status": from_status,
        "to_status": to_status,
        "summary": summary,
    }
    events = list(ticket.get("lifecycle_events") or [])
    events.append(record)
    ticket["lifecycle_events"] = events

    sec_text = render_lifecycle_timeline_section(events)
    ticket["_body"] = _upsert_body_section(ticket.get("_body", ""), "## Lifecycle Timeline", sec_text)


def write_ticket(ticket: dict) -> None:
    """Serialise ticket back to file preserving frontmatter key order."""
    path: Path = ticket["_path"]
    body: str = ticket.get("_body", "")

    cnotes = load_change_notes(ticket)
    if cnotes:
        sec_text = render_change_notes_section(cnotes)
        body = _upsert_body_section(body, "## Change Notes", sec_text)
        ticket["_body"] = body
        ticket["change_notes_summary"] = len(cnotes)
        ticket["change_notes"] = cnotes

    audit = load_acceptance_contract_audit(ticket)
    if audit:
        sec_text = render_acceptance_contract_audit_section(audit)
        body = _upsert_body_section(body, "## Acceptance Contract Audit", sec_text)
        ticket["_body"] = body
        ok = bool(audit.get("ok"))
        findings_count = len(audit.get("findings") or [])
        ticket["acceptance_contract_audit_summary"] = (
            f"ok ({findings_count} findings)" if ok else f"failed ({findings_count} findings)"
        )
        ticket["acceptance_contract_audit"] = audit

    l_events = load_lifecycle_events(ticket)
    if l_events:
        sec_text = render_lifecycle_timeline_section(l_events)
        body = _upsert_body_section(body, "## Lifecycle Timeline", sec_text)
        ticket["_body"] = body
        ticket["lifecycle_events_summary"] = len(l_events)
        ticket["lifecycle_events"] = l_events

    ordered: dict = {}
    for k in _FRONTMATTER_KEY_ORDER:
        if k in ticket:
            v = ticket[k]
            if k == "change_notes" and isinstance(v, dict):
                continue
            if k == "acceptance_contract_audit" and isinstance(v, dict):
                continue
            if k == "lifecycle_events" and isinstance(v, list):
                continue
            ordered[k] = v
    for k, v in ticket.items():
        if not k.startswith("_") and k not in ordered:
            if k == "change_notes" and isinstance(v, dict):
                continue
            if k == "acceptance_contract_audit" and isinstance(v, dict):
                continue
            if k == "lifecycle_events" and isinstance(v, list):
                continue
            ordered[k] = v
    for key, value in ordered.items():
        if isinstance(value, str) and len(value) > _MAX_SCALAR_LEN:
            omitted = len(value) - _MAX_SCALAR_LEN
            ordered[key] = value[:_MAX_SCALAR_LEN] + f" ... [truncated, {omitted} chars omitted]"
    front = yaml.dump(ordered, default_flow_style=None, sort_keys=False, allow_unicode=True)
    _atomic_write_text(path, f"---\n{front}---\n{body}\n")


def file_skeletons_sidecar_ref(ticket_id: str) -> str:
    """Return the deterministic repo-relative sidecar path for ticket skeleton context."""
    return f".lanegate/context/{canonical_id(ticket_id)}/file_skeletons.json"


def write_file_skeletons_sidecar(
    ticket: dict,
    repo_root: Path,
    file_skeletons: dict[str, str],
) -> str:
    """Write skeleton context to a deterministic sidecar and update ticket metadata.

    The ticket keeps only a compact repo-relative reference and byte/file
    summary; the bulky per-file skeleton text stays in JSON.
    """
    ref = file_skeletons_sidecar_ref(ticket["id"])
    path = repo_root / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(file_skeletons, indent=2, sort_keys=True) + "\n"
    _atomic_write_text(path, payload)
    ticket["file_skeletons_ref"] = ref
    ticket["file_skeletons_summary"] = {
        "files": len(file_skeletons),
        "bytes": len(payload.encode()),
    }
    ticket.pop("file_skeletons", None)
    return ref


def regenerate_file_skeletons(
    touches: list[str], project_root: Path
) -> dict[str, str]:
    """Build skeletons from the files as they are on disk right now.

    Analyze-time skeletons are a snapshot: by the time implement or a fix pass
    runs, an earlier step may have changed the very signatures the skeleton
    describes, and a stale skeleton is worse than none because the agent
    believes it (TICK-412). Regenerating costs one AST parse per touched file.

    Files that cannot be parsed are skipped rather than reported empty, letting
    the caller fall back to the stored snapshot.
    """
    from lanegate.analyze import _build_file_skeleton

    skeletons: dict[str, str] = {}
    for rel in touches:
        if not isinstance(rel, str) or not rel.strip():
            continue
        path = project_root / rel
        if not path.is_file():
            continue
        try:
            skeletons[rel] = _build_file_skeleton(Path(rel), project_root)
        except Exception:
            # Skeletons are an optimization; never let one bad file stop a step.
            continue
    return skeletons


def load_file_skeletons(
    ticket: dict, project_root: Path | None = None, *, regenerate: bool = False
) -> dict[str, str]:
    """Return file skeletons from a sidecar when present, else legacy inline data.

    Invalid or missing sidecars degrade to inline ``file_skeletons`` for
    compatibility with older tickets and partially migrated branches.

    With ``regenerate=True`` the touched files are re-parsed from disk and used
    when they yield anything, so a step sees current signatures rather than the
    analyze-time snapshot. Falls back to the stored data when regeneration
    produces nothing (no touches, unparseable files, missing grammars).
    """
    root = project_root if project_root is not None else Path.cwd()
    if regenerate:
        fresh = regenerate_file_skeletons(ticket.get("touches") or [], root)
        if fresh:
            return fresh
    ref = ticket.get("file_skeletons_ref")
    if isinstance(ref, str) and ref.strip():
        ref_path = Path(ref)
        path = ref_path if ref_path.is_absolute() else root / ref_path
        if not _valid_file_skeletons_ref(ref):
            loaded = None
        else:
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = None
        if isinstance(loaded, dict) and all(
            isinstance(k, str) and isinstance(v, str) for k, v in loaded.items()
        ):
            return loaded

    file_skeletons = ticket.get("file_skeletons") or {}
    if isinstance(file_skeletons, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in file_skeletons.items()
    ):
        return file_skeletons
    return {}


class QuarantinedTicket:
    """A ticket file that failed schema validation on load."""

    __slots__ = ("path", "error")

    def __init__(self, path: Path, error: str) -> None:
        self.path = path
        self.error = error

    def __repr__(self) -> str:  # pragma: no cover
        return f"QuarantinedTicket(path={self.path!r}, error={self.error!r})"


def load_all_tickets(
    tickets_dir: Path,
    prefix: str,
    cfg: dict | None = None,
) -> tuple[list[dict], list[QuarantinedTicket]]:
    """Load all parseable ticket files, sorted by filename.

    Returns a 2-tuple ``(valid, quarantined)``:
    - *valid*: tickets that passed schema validation.
    - *quarantined*: :class:`QuarantinedTicket` entries for files that could
      not be parsed or failed validation; they are excluded from *valid* so
      orchestration never receives a malformed ticket.
    """
    valid: list[dict] = []
    quarantined: list[QuarantinedTicket] = []
    for p in ticket_glob(tickets_dir, prefix):
        try:
            t = parse_ticket(p)
        except yaml.YAMLError as exc:
            quarantined.append(
                QuarantinedTicket(p, f"could not parse frontmatter: {exc}")
            )
            continue
        if t is None:
            quarantined.append(QuarantinedTicket(p, "could not parse frontmatter"))
            continue
        pub = {k: v for k, v in t.items() if not k.startswith("_")}
        errors = validate_ticket(pub, cfg)
        if errors:
            quarantined.append(QuarantinedTicket(p, "; ".join(errors)))
        else:
            valid.append(t)
    return valid, quarantined


def load_tickets_by_ids(
    tickets_dir: Path, prefix: str, ids: set[str], cfg: dict | None = None
) -> dict[str, dict]:
    """Load and parse only the ticket files matching `ids` (canonical form), keyed by
    canonical id. Filters by filename before parsing, so callers that only need a
    handful of tickets out of a large board (e.g. enriching a few failed outcomes
    across many historical runs) don't pay the YAML-parse cost of every ticket file.
    """
    if not ids:
        return {}
    id_prefix_re = re.compile(rf"^({re.escape(prefix)}-\d+)", re.IGNORECASE)
    result: dict[str, dict] = {}
    for p in ticket_glob(tickets_dir, prefix):
        m = id_prefix_re.match(p.name)
        if not m:
            continue
        cid = canonical_id(m.group(1))
        if cid not in ids or cid in result:
            continue
        try:
            t = parse_ticket(p)
        except yaml.YAMLError:
            continue
        if t is None:
            continue
        pub = {k: v for k, v in t.items() if not k.startswith("_")}
        if validate_ticket(pub, cfg):
            continue
        result[cid] = t
    return result


def canonical_id(ticket_id: str) -> str:
    """Normalize a safe ticket ID's case and numeric suffix for comparisons.

    Ticket IDs are used to derive branch and worktree paths.  Reject path
    syntax before any lifecycle command can turn an attacker-controlled ticket
    frontmatter value into a recursive-deletion target.
    """
    # Dots are a legitimate part of project ticket prefixes (for example
    # ``ACME.PROJ-001``).  Keep them while excluding every pathname/ref
    # traversal form: no separators, no leading/trailing dot, no ``..``, and
    # no Git's special ``@{`` sequence.  The resulting IDs remain safe to use
    # as one worktree path component and a local branch name.
    if (
        not isinstance(ticket_id, str)
        or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?", ticket_id)
        or ".." in ticket_id
        or "@{" in ticket_id
        or ticket_id.lower().endswith(".lock")
    ):
        raise ValueError(f"invalid ticket ID: {ticket_id!r}")
    normalized = ticket_id.upper()
    match = re.fullmatch(r"(.+-)(\d+)", normalized)
    if match:
        return f"{match.group(1)}{int(match.group(2)):03d}"
    return normalized


def unresolved_dependencies(
    dependencies: list[str] | None, status_map: dict[str, str | None]
) -> list[str]:
    """Return dependency IDs whose canonical ticket is not delivered.

    Keep dependency gating in one place so scheduler selection, its diagnostic,
    and ``lanegate next`` cannot disagree about failed/closed prerequisites.

    ``dependencies`` comes from executor/LLM-writable ``depends_on``
    frontmatter and is not validated at parse time (``validate_ticket`` has no
    check for it). A malformed entry can never resolve to a real, delivered
    ticket, so it's treated the same as any other unmet dependency rather than
    raising -- letting one bad ``depends_on`` string crash board/next/batch
    selection for every ticket is worse than just leaving that one dependency
    blocked.

    ``status_map``'s keys are ``t["id"]`` straight from ticket frontmatter too
    -- ``validate_ticket`` only checks ``id`` is truthy, not its shape -- so a
    ticket with e.g. a trailing space in its id can reach here unquarantined.
    Skip whatever key fails to canonicalize instead of raising: it can never
    match a (necessarily valid) canonical dependency id anyway, so dropping it
    from the map has the same net effect as if that ticket didn't exist yet.
    """
    canonical_status_map = {}
    for ticket_id, status in status_map.items():
        try:
            canonical_status_map[canonical_id(ticket_id)] = status
        except ValueError:
            continue
    unresolved = []
    for dependency in dependencies or []:
        try:
            canonical_dependency = canonical_id(dependency)
        except ValueError:
            unresolved.append(dependency)
            continue
        if canonical_status_map.get(canonical_dependency) not in DEPENDENCY_SATISFIED_STATUSES:
            unresolved.append(dependency)
    return unresolved


def branch_name(ticket_id: str) -> str:
    """Lowercase branch name derived from ticket ID."""
    return ticket_id.lower()


def is_paired_test_file(committed_file: str, allowed: set[str]) -> bool:
    """True if committed_file is the test file naturally paired with a module
    already declared in a ticket's touches (e.g. tests/test_orchestrate.py for
    lanegate/orchestrate.py), even though the test file itself isn't declared.

    TICK-245: touching code without touching its own test is worse than the
    reverse, so touches-compliance checks should not treat this as scope drift.
    Shared by orchestrate.py's board-clearing-loop guard and lifecycle.py's
    check_touches_compliance (the --allow-drift CLI path) so both enforce the
    same exemption.
    """
    path = PurePosixPath(committed_file)
    if path.parts[:1] != ("tests",):
        return False
    name = path.name
    if not (name.startswith("test_") and name.endswith(".py")):
        return False
    module_name = name[len("test_") : -len(".py")]
    return any(PurePosixPath(a).stem == module_name for a in allowed)


def display_order(tickets: list[dict]) -> list[dict]:
    """Return tickets sorted by the standard status display order, then priority."""
    order_map = {s: i for i, s in enumerate(_STANDARD_STATUSES)}

    def sort_key(t: dict) -> tuple:
        status = t.get("status", "unknown")
        return (order_map.get(status, len(_STANDARD_STATUSES)), t.get("priority", 99))

    return sorted(tickets, key=sort_key)


def _run_git(repo_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True, encoding="utf-8",
        cwd=repo_root,
    )


def get_ticket_diff(
    ticket_id: str,
    repo_root: Path,
    *,
    base: str | None = None,
    max_patch_chars: int = 20_000,
    include_patches: bool = True,
) -> dict:
    """Return a structured, JSON-serializable diff for a ticket branch.

    The API contract is browser-oriented: callers get one entry per changed file,
    and large patches are truncated per file instead of returning one unbounded
    raw diff blob.

    include_patches=False skips the per-file `git diff -- <path>` subprocess
    entirely (files still get path/status from --name-status) for callers that
    only need the stat/file list, e.g. a stat-only summary over many files.
    """
    base = base or resolve_trunk_branch(load_config(repo_root), repo_root)
    tid = canonical_id(ticket_id)
    branch = branch_name(ticket_id)

    branch_check = _run_git(repo_root, ["rev-parse", "--verify", "--quiet", branch])
    if branch_check.returncode != 0:
        return {
            "id": tid,
            "ticket_id": tid,
            "branch": branch,
            "base": base,
            "stat": "",
            "files": [],
            "diff": "",
            "truncated": False,
            "error": f"no branch '{branch}' yet — {tid} has no worktree/commits to diff "
            "(ticket not started, or its worktree/branch was already cleaned up after merge)",
        }

    stat_r = _run_git(repo_root, ["diff", f"{base}..{branch}", "--stat"])
    names_r = _run_git(repo_root, ["diff", f"{base}..{branch}", "--name-status"])

    if names_r.returncode != 0:
        return {
            "id": tid,
            "ticket_id": tid,
            "branch": branch,
            "base": base,
            "stat": "",
            "files": [],
            "diff": "",
            "truncated": False,
            "error": names_r.stderr.strip() or stat_r.stderr.strip(),
        }

    files: list[dict] = []
    aggregate_parts: list[str] = []
    for line in names_r.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        old_path = None
        path = parts[1] if len(parts) > 1 else ""
        if status.startswith("R") and len(parts) > 2:
            old_path = parts[1]
            path = parts[2]

        patch = ""
        patch_r = None
        patch_truncated = False
        if include_patches:
            patch_r = _run_git(repo_root, ["diff", f"{base}..{branch}", "--", path])
            patch = patch_r.stdout if patch_r.returncode == 0 else ""
            if max_patch_chars >= 0 and len(patch) > max_patch_chars:
                patch = patch[:max_patch_chars] + "\n... [truncated]\n"
                patch_truncated = True

        if patch:
            aggregate_parts.append(patch)

        entry = {
            "path": path,
            "status": status,
            "patch": patch,
            "truncated": patch_truncated,
        }
        if old_path is not None:
            entry["old_path"] = old_path
        if patch_r is not None and patch_r.returncode != 0:
            entry["error"] = patch_r.stderr.strip()
        files.append(entry)

    truncated = any(f.get("truncated") for f in files)
    return {
        "id": tid,
        "ticket_id": tid,
        "branch": branch,
        "base": base,
        "stat": stat_r.stdout,
        "files": files,
        "diff": "\n".join(aggregate_parts),
        "truncated": truncated,
        "max_patch_chars": max_patch_chars,
        "error": None,
    }


def get_ticket_detail(ticket_id: str, cfg: dict, repo_root: Path) -> dict:
    """Return full ticket detail as JSON: frontmatter, body, close_criteria, review, files."""
    tid = canonical_id(ticket_id)
    tickets_dir = repo_root / cfg["tickets_dir"]

    # Load all tickets and find the one matching the id
    all_tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    ticket = None
    for t in all_tickets:
        try:
            if canonical_id(t.get("id", "")) == tid:
                ticket = t
                break
        except ValueError:
            continue

    if ticket is None:
        return {"error": f"ticket {tid} not found", "status": 404}

    # Extract public fields (exclude internal _path, _body)
    result = {k: v for k, v in ticket.items() if not k.startswith("_")}
    result["body"] = ticket.get("_body", "")
    result["close_criteria"] = ticket.get("close_criteria", "")
    result["review_verdict"] = ticket.get("review_verdict")
    result["review_summary"] = ticket.get("review_summary")
    result["review_findings"] = ticket.get("review_findings") or []

    if ticket.get("status") == "needs_review":
        result["needs_review_cause"] = classify_needs_review_cause(ticket)
        result["needs_review_recovery"] = needs_review_recovery_advice(ticket)

    return result


def get_ticket_summary(ticket_id: str, cfg: dict, repo_root: Path) -> dict:
    """Aggregate why-is-this-stuck context into one payload for a single ticket.

    Pulls together what's otherwise scattered across the ticket body, the review
    section, and the worktree diff: the full (untruncated) escalation/failure
    reason, review verdict/summary/findings, the needs_review cause + recovery
    command, and a file-level diff stat -- so a stuck ticket can be understood
    without opening the ticket file and the worktree separately.
    """
    tid = canonical_id(ticket_id)
    tickets_dir = repo_root / cfg["tickets_dir"]
    all_tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    ticket = next((t for t in all_tickets if t["id"] == tid), None)
    if ticket is None:
        return {"error": f"ticket {tid} not found", "status": 404}

    result: dict = {
        "id": tid,
        "title": ticket.get("title", ""),
        "status": ticket.get("status"),
        "priority": ticket.get("priority"),
        "milestone": ticket.get("milestone"),
    }

    status = ticket.get("status")
    verdict = ticket.get("review_verdict")

    if _reason_section_applies(status, verdict):
        for header in _ATTENTION_SECTION_HEADERS:
            section = _body_section(ticket, header).strip()
            if section:
                result["reason"] = section
                break

    if verdict:
        result["review_verdict"] = verdict
    if ticket.get("review_summary"):
        result["review_summary"] = ticket["review_summary"]
    findings = ticket.get("review_findings") or []
    if findings:
        result["review_findings"] = findings

    if status == "needs_review":
        result["needs_review_cause"] = classify_needs_review_cause(ticket)
        result["needs_review_recovery"] = needs_review_recovery_advice(ticket)
        result["next_step"] = result["needs_review_recovery"]
    elif status == "code_complete":
        if verdict == "changes_requested":
            result["next_step"] = f"address feedback, then: lanegate review {tid} --verdict approved"
        elif verdict == "approved":
            result["next_step"] = f"lanegate merge {tid}"
        elif findings:
            # Findings exist (e.g. from an ad-hoc/audit review) but no verdict was
            # ever recorded on the normal review path -- a silently stalled ticket
            # otherwise looks identical to one still awaiting its first review.
            result["next_step"] = (
                f"unresolved review findings, no verdict recorded — decide: "
                f"lanegate review {tid} --verdict approved|changes_requested"
            )

    diff = get_ticket_diff(tid, repo_root, include_patches=False)
    if diff.get("error"):
        result["diff_error"] = diff["error"]
    else:
        result["diff_stat"] = diff.get("stat", "").strip()
        result["files_changed"] = [
            {"path": f["path"], "status": f["status"]} for f in diff.get("files", [])
        ]

    return result


def group_by_status(tickets: list[dict]) -> dict[str, list[dict]]:
    """Group tickets by status; preserves insertion order within each group."""
    grouped: dict[str, list[dict]] = {}
    for t in tickets:
        status = t.get("status", "unknown")
        grouped.setdefault(status, []).append(t)
    return grouped
