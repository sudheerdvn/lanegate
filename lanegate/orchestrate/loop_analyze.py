"""Draft analysis helpers extracted from loop."""

from __future__ import annotations

import contextlib
import io
import re
import sys
from pathlib import Path
from .batch import next_batch
from .run_report import _append_run_event
from .loop_dispatch import _is_interrupted_exit
from lanegate.ticket import load_all_tickets

_ANALYZE_SYSTEMIC_FAILURE_THRESHOLD = 2


_ANALYZE_FAILURE_VOLATILE_ID_RE = re.compile(
    r'''(?ix)
    (?P<key>
        ["']?(?:session|message|request)[_-]?id["']?
        | ["']?uuid["']?
    )
    \s*[:=]\s*
    (?P<value>["'][^"']*["']|[^\s,}\]]+)
    '''
)


_ANALYZE_FAILURE_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


_ANALYZE_FAILURE_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ][0-2]\d:[0-5]\d:[0-5]\d(?:\.\d+)?(?:Z|[+-][0-2]\d:?[0-5]\d)?\b"
)


_ANALYZE_FAILURE_USAGE_RE = re.compile(
    r'''(?ix)
    (?P<key>
        ["']?(?:total_)?cost(?:_usd)?["']?
        | ["']?(?:input|output|cache(?:_creation|_read)?|reasoning)[_-]?tokens?["']?
        | ["']?(?:duration(?:_api)?_?ms|duration_seconds|num_turns)["']?
    )
    \s*[:=]\s*
    -?\d+(?:\.\d+)?
    '''
)


def _normalize_analyze_failure_reason(reason: str) -> str:
    """Return a stable comparison key for executor failure diagnostics.

    Executor stderr is retained verbatim for operators, but common per-run
    metadata must not prevent the draft-analysis circuit breaker from
    recognizing one repeated systemic failure.
    """
    normalized = _ANALYZE_FAILURE_VOLATILE_ID_RE.sub(
        lambda match: f"{match.group('key')}=<volatile>", reason
    )
    normalized = _ANALYZE_FAILURE_UUID_RE.sub("<uuid>", normalized)
    normalized = _ANALYZE_FAILURE_TIMESTAMP_RE.sub("<timestamp>", normalized)
    normalized = _ANALYZE_FAILURE_USAGE_RE.sub(
        lambda match: f"{match.group('key')}=<usage>", normalized
    )
    return normalized


class _Tee:
    """Write-only file-like object that mirrors writes to two streams."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, data: str) -> int:
        for stream in self._streams:
            stream.write(data)
        return len(data)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()


def _analyze_drafts(
    cfg: dict,
    repo_root: Path,
    milestone: str | None = None,
    tickets_dir=None,
    ticket_ids: set[str] | None = None,
    pool_name: str | None = None,
    session_ts: str | None = None,
) -> bool:
    """Analyze eligible draft tickets, one at a time, until one is dispatchable.

    Skips drafts outside the active milestone filter, and outside an explicit
    ticket scope when one is given — a run scoped to specific
    ticket(s) must not go analyze unrelated drafts elsewhere in the milestone
    just because none of the requested ticket(s) were ready to dispatch.
    On analyze failure, logs a warning and continues to the next draft — UNLESS
    the same failure reason repeats on consecutive drafts, which means the
    problem is systemic (bad executor config, a parsing bug, ...) rather than
    that one ticket's content. Repeating the same doomed call across every
    remaining draft just burns model cost for a guaranteed-identical failure,
    so the whole draft-analysis pass stops early in that case instead.

    Returns as soon as a successful analyze produces a dispatchable ticket,
    instead of draining the entire draft backlog first — ready-to-implement
    work must never sit idle behind unrelated drafts still waiting their turn.
    Callers sit inside a loop that re-invokes this (checking for dispatchable
    work first each time), so remaining drafts still get analyzed — just
    interleaved with dispatch instead of front-loaded before it.

    Returns True when the analysis subprocess was interrupted by the operator.
    Callers must halt the run rather than dispatching another draft or worker.
    """
    from lanegate.analyze import cmd_analyze
    from lanegate.ticket import load_all_tickets as _load_all_tickets

    if tickets_dir is None:
        tickets_dir = repo_root / cfg["tickets_dir"]

    tickets, _ = _load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    drafts = [
        t
        for t in tickets
        if (t.get("status") == "draft" or (t.get("status") == "open" and not t.get("touches")))
        and (milestone is None or t.get("milestone") == milestone)
        and (ticket_ids is None or t["id"] in ticket_ids)
    ]
    last_failure_reason: str | None = None
    repeat_count = 0
    for t in drafts:
        if (t.get("review_summary") or "").startswith("already_resolved:") or "analyze: ticket premise appears already resolved" in (t.get("_body") or ""):
            print(f"[orchestrate] skipping draft {t['id']} (already flagged as already_resolved)")
            continue
        print(f"[orchestrate] auto-analyzing draft {t['id']}")
        captured = io.StringIO()
        try:
            with contextlib.redirect_stderr(_Tee(sys.stderr, captured)):
                cmd_analyze(t["id"], cfg, repo_root, pool_name=pool_name)
        except (Exception, SystemExit) as exc:
            code = exc.code if isinstance(exc, SystemExit) else exc
            reason = captured.getvalue().strip() or str(exc)
            if isinstance(code, int) and _is_interrupted_exit(code):
                print(
                    f"[orchestrate] draft analysis for {t['id']} was interrupted — "
                    "stopping further dispatch",
                    file=sys.stderr,
                )
                return True
            comparison_reason = _normalize_analyze_failure_reason(reason)
            print(
                f"WARNING: analyze failed for {t['id']}: {code} — skipping",
                file=sys.stderr,
            )
            _append_run_event(
                repo_root, session_ts, "ticket_outcome",
                ticket_id=t["id"], outcome="failure", reason=reason,
            )
            if comparison_reason and comparison_reason == last_failure_reason:
                repeat_count += 1
            else:
                last_failure_reason = comparison_reason
                repeat_count = 1
            if repeat_count >= _ANALYZE_SYSTEMIC_FAILURE_THRESHOLD:
                print(
                    f"ERROR: analyze failed with the same error on {repeat_count} consecutive "
                    f"drafts — this looks like a systemic executor/config problem, not a "
                    f"per-ticket issue. Stopping draft analysis instead of repeating the same "
                    f"failure across the rest of the queue.",
                    file=sys.stderr,
                )
                return False
            continue
        last_failure_reason = None
        repeat_count = 0
        if next_batch(cfg, repo_root, milestone=milestone, ticket_ids=ticket_ids):
            return False
    return False


def _queue_code_complete_reviews(
    cfg: dict,
    repo_root: Path,
    *,
    milestone: str | None = None,
    ticket_ids: set[str] | None = None,
    exclude_ticket_ids: set[str] | None = None,
    reason: str = "awaiting independent review",
) -> list[str]:
    """Move eligible completed work into the existing review-resume path.

    A ticket with a changes-requested verdict is deliberately excluded: it must
    stay visible to the fix workflow, not be silently sent through a fresh
    review. This helper is used both at startup and before draft analysis.
    """
    from lanegate.lifecycle import mark_review_pending

    tickets_dir = repo_root / cfg["tickets_dir"]
    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    queued: list[str] = []
    for ticket in tickets:
        if ticket.get("status") != "code_complete" or ticket.get("review_verdict"):
            continue
        if exclude_ticket_ids is not None and ticket["id"] in exclude_ticket_ids:
            continue
        if milestone is not None and ticket.get("milestone") != milestone:
            continue
        if ticket_ids is not None and ticket["id"] not in ticket_ids:
            continue
        mark_review_pending(ticket, cfg, repo_root, reason=reason)
        queued.append(ticket["id"])
    return queued


def _print_draft_analysis_plan(
    cfg: dict,
    repo_root: Path,
    milestone: str | None = None,
    tickets_dir=None,
    ticket_ids: set[str] | None = None,
) -> None:
    """Print which drafts would be analyzed (dry-run mode)."""
    from lanegate.ticket import load_all_tickets as _load_all_tickets

    if tickets_dir is None:
        tickets_dir = repo_root / cfg["tickets_dir"]

    tickets, _ = _load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    drafts = [
        t
        for t in tickets
        if (t.get("status") == "draft" or (t.get("status") == "open" and not t.get("touches")))
        and (milestone is None or t.get("milestone") == milestone)
        and (ticket_ids is None or t["id"] in ticket_ids)
    ]
    for t in drafts:
        print(f"[dry-run] would analyze draft {t['id']}")


