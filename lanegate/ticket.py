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

from lanegate.config import load_config, resolve_trunk_branch

# Ordered frontmatter keys for clean round-trip dumps
_FRONTMATTER_KEY_ORDER = [
    "id",
    "title",
    "status",
    "status_changed_at",
    "lifecycle_events",
    "milestone",
    "batch",
    "priority",
    "touches",
    "file_skeletons_ref",
    "file_skeletons_summary",
    "file_skeletons",
    "parallel_safe",
    "autonomy",
    "source",
    "trusted",
    "model",
    "executor",
    "reviewer",
    "analyze_session_id",
    "implement_session_id",
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
    "review_independence",
    "reviewed_at",
    "review_verdict",
    "review_summary",
    "review_findings",
    "verification",
    "auto_fix_attempts",
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
)

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

_VALID_AUTONOMY = frozenset({"full", "supervised", "manual"})
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

    implement_session_id = meta.get("implement_session_id")
    if implement_session_id is not None and not isinstance(implement_session_id, str):
        errors.append("implement_session_id must be a string if present")

    fix_session_id = meta.get("fix_session_id")
    if fix_session_id is not None and not isinstance(fix_session_id, str):
        errors.append("fix_session_id must be a string if present")

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


def _normalize_dates(meta: dict) -> dict:
    """Coerce any date/datetime scalar YAML implicitly typed (e.g. `created: 2026-07-03`)
    back to an ISO string, so every ticket dict is JSON-serializable by construction."""
    for key, value in meta.items():
        if isinstance(value, (datetime.date, datetime.datetime)):
            meta[key] = value.isoformat()
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
    return meta


REVIEW_FINDINGS_HEADER = "## Review Findings"

# Matches both the historical single ``## Review Findings`` header and the
# per-attempt ``## Review Findings (attempt N)`` form written since TICK-343.
_REVIEW_FINDINGS_HEADER_RE = re.compile(
    r"^##[ \t]*Review Findings(?:[ \t]*\(attempt[ \t]*(\d+)\))?[ \t]*$",
    re.MULTILINE,
)


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
        next_heading = rest.find("\n##")
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
    next_heading = after.find("\n##")
    return after if next_heading == -1 else after[:next_heading]


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



def attention_summary(ticket: dict) -> str:
    """Return a one-line reason why a ticket needs operator attention."""
    status = ticket.get("status")
    verdict = ticket.get("review_verdict")

    if status in {"needs_review", "failed"} or verdict == "changes_requested":
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

    return ""



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
    next_heading = after.find("\n##")
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

    transition = ""
    if from_status and to_status:
        transition = f"{from_status} → {to_status}"
    elif to_status:
        transition = to_status
    text = " — ".join(part for part in (transition, summary or event) if part)
    line = f"- {stamp}: {text}"
    body = ticket.get("_body", "")
    header = "## Lifecycle Timeline"
    if header not in body:
        ticket["_body"] = body.rstrip() + f"\n\n{header}\n{line}\n"
        return
    before, _, after = body.partition(header)
    next_heading = after.find("\n##")
    section, tail = (after, "") if next_heading == -1 else (after[:next_heading], after[next_heading:])
    ticket["_body"] = before + header + section.rstrip("\n") + f"\n{line}\n" + tail


def write_ticket(ticket: dict) -> None:
    """Serialise ticket back to file preserving frontmatter key order."""
    path: Path = ticket["_path"]
    body: str = ticket.get("_body", "")
    ordered: dict = {}
    for k in _FRONTMATTER_KEY_ORDER:
        if k in ticket:
            ordered[k] = ticket[k]
    for k, v in ticket.items():
        if not k.startswith("_") and k not in ordered:
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


def load_file_skeletons(ticket: dict, project_root: Path | None = None) -> dict[str, str]:
    """Return file skeletons from a sidecar when present, else legacy inline data.

    Invalid or missing sidecars degrade to inline ``file_skeletons`` for
    compatibility with older tickets and partially migrated branches.
    """
    root = project_root if project_root is not None else Path.cwd()
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
    """Normalize a ticket ID's case and numeric suffix for comparisons."""
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
    """
    canonical_status_map = {
        canonical_id(ticket_id): status for ticket_id, status in status_map.items()
    }
    return [
        dependency
        for dependency in dependencies or []
        if canonical_status_map.get(canonical_id(dependency)) not in DEPENDENCY_SATISFIED_STATUSES
    ]


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
) -> dict:
    """Return a structured, JSON-serializable diff for a ticket branch.

    The API contract is browser-oriented: callers get one entry per changed file,
    and large patches are truncated per file instead of returning one unbounded
    raw diff blob.
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

        patch_r = _run_git(repo_root, ["diff", f"{base}..{branch}", "--", path])
        patch = patch_r.stdout if patch_r.returncode == 0 else ""
        patch_truncated = False
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
        if patch_r.returncode != 0:
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
        if canonical_id(t.get("id", "")) == tid:
            ticket = t
            break

    if ticket is None:
        return {"error": f"ticket {tid} not found", "status": 404}

    # Extract public fields (exclude internal _path, _body)
    result = {k: v for k, v in ticket.items() if not k.startswith("_")}
    result["body"] = ticket.get("_body", "")
    result["close_criteria"] = ticket.get("close_criteria", "")
    result["review_verdict"] = ticket.get("review_verdict")
    result["review_summary"] = ticket.get("review_summary")
    result["review_findings"] = ticket.get("review_findings") or []

    return result


def group_by_status(tickets: list[dict]) -> dict[str, list[dict]]:
    """Group tickets by status; preserves insertion order within each group."""
    grouped: dict[str, list[dict]] = {}
    for t in tickets:
        status = t.get("status", "unknown")
        grouped.setdefault(status, []).append(t)
    return grouped
