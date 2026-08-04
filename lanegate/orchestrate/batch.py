"""Board batch selection and continuation-queue rendering helpers."""

from __future__ import annotations

import sys
from pathlib import Path

from lanegate.concurrency import locked_touches, touches_overlap
from lanegate.config import resolve_ticket_pool
from lanegate.ticket import attention_summary, load_all_tickets, unresolved_dependencies


def next_batch(
    cfg: dict,
    repo_root: Path,
    milestone: str | None = None,
    *,
    exclude_touches: set[str] | None = None,
    ticket_ids: set[str] | None = None,
) -> list[dict]:
    """Return the next parallel-safe batch of unblocked open tickets.

    Mirrors the logic in board.cmd_next: greedy non-overlapping touches,
    priority-sorted, parallel_safe gate.

    Args:
        cfg: loaded config dict
        repo_root: repository root path
        milestone: optional milestone filter; only tickets with this milestone
                   are eligible when set.
        exclude_touches: additional touches to treat as locked for this query,
            used by the worker-pool scheduler for tickets that are in flight
            but may not have committed their status lock yet.
        ticket_ids: optional explicit set of ticket IDs (TICK-262); when set,
            only these tickets are eligible, on top of (not instead of) the
            usual status/milestone/deps/lock filtering below.
    """
    tickets_dir = repo_root / cfg["tickets_dir"]
    lock_statuses = cfg["lock_statuses"]

    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    status_map = {t["id"]: t.get("status") for t in tickets}
    locked = locked_touches(tickets, lock_statuses, repo_root)
    if exclude_touches:
        locked = set(locked) | set(exclude_touches)

    candidates = []
    for t in tickets:
        if t.get("status") not in ("open", "hibernated"):
            continue
        if milestone is not None and t.get("milestone") != milestone:
            continue
        if ticket_ids is not None and t["id"] not in ticket_ids:
            continue
        if unresolved_dependencies(t.get("depends_on"), status_map):
            continue
        if touches_overlap(t.get("touches") or [], locked):
            continue
        candidates.append(t)

    candidates.sort(
        key=lambda x: (x.get("priority", 99), 0 if x.get("status") == "hibernated" else 1)
    )

    if not candidates:
        return []

    top = candidates[0]
    batch = [top]
    if top.get("parallel_safe"):
        taken = set(top.get("touches") or [])
        for c in candidates[1:]:
            if not c.get("parallel_safe"):
                continue
            c_touches = set(c.get("touches") or [])
            if not touches_overlap(c_touches, taken):
                batch.append(c)
                taken |= c_touches

    # TICK-091: attach the routed pool (and why) to each selected ticket so
    # the pool-dispatch scheduler (TICK-089) can pick an executor instance
    # from that pool instead of the run's single global --pool. Underscore
    # prefix keeps this out of write_ticket()'s frontmatter dump (same
    # convention as _path/_body) -- like pool_assignment elsewhere in this
    # module, pool selection is dispatch-time-only and must never round-trip
    # onto the ticket's own file.
    for t in batch:
        pool_name, reason = resolve_ticket_pool(cfg, t)
        t["_routed_pool"] = pool_name
        t["_routed_pool_reason"] = reason

    return batch


def _format_max_parallel_detail(detail: dict) -> str:
    value = detail["value"]
    source = detail["source"]
    if source == "default executor override":
        label = detail.get("config_key") or "executor max_parallel"
        default_executor = detail.get("default_executor")
        text = (
            f"{value} (source: {source}: {label} — global default executor "
            f"'{default_executor}'; unrelated to per-ticket executor selection"
        )
        overridden = detail.get("overrides")
        if overridden:
            text += (
                f"; default executor override takes precedence over "
                f"{overridden.get('config_key', 'max_parallel')}={overridden['value']}"
            )
        return text + ")"
    return f"{value} (source: {source})"


def _underfilled_batch_reason(
    cfg: dict,
    repo_root: Path,
    batch: list[dict],
    max_parallel: int,
    milestone: str | None = None,
) -> str | None:
    if len(batch) >= max_parallel or not batch:
        return None

    tickets_dir = repo_root / cfg["tickets_dir"]
    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    status_map = {t["id"]: t.get("status") for t in tickets}
    locked = locked_touches(tickets, cfg["lock_statuses"], repo_root)

    open_tickets = [
        t
        for t in tickets
        if t.get("status") in ("open", "hibernated")
        and (milestone is None or t.get("milestone") == milestone)
    ]

    candidates = []
    for t in tickets:
        if t.get("status") not in ("open", "hibernated"):
            continue
        if milestone is not None and t.get("milestone") != milestone:
            continue
        if unresolved_dependencies(t.get("depends_on"), status_map):
            continue
        if touches_overlap(t.get("touches") or [], locked):
            continue
        candidates.append(t)

    candidates.sort(
        key=lambda x: (x.get("priority", 99), 0 if x.get("status") == "hibernated" else 1)
    )
    if len(candidates) <= len(batch):
        return f"only {len(batch)} eligible ticket(s) available for cap {max_parallel}"

    top = candidates[0]
    selected_ids = {t["id"] for t in batch}
    skipped_reasons: list[str] = []
    for candidate in open_tickets:
        candidate_id = candidate["id"]
        if candidate_id in selected_ids:
            continue

        blocked_deps = unresolved_dependencies(candidate.get("depends_on"), status_map)
        if blocked_deps:
            skipped_reasons.append(
                f"{candidate_id} blocked by dependency {', '.join(blocked_deps)}"
            )
            continue

        candidate_touches = candidate.get("touches") or []
        if touches_overlap(candidate_touches, locked):
            locked_overlap = sorted(set(candidate_touches) & set(locked)) or ["*"]
            skipped_reasons.append(
                f"{candidate_id} blocked by locked touch {', '.join(locked_overlap)}"
            )
            continue

        if not candidate.get("parallel_safe"):
            skipped_reasons.append(f"{candidate_id} has parallel_safe=false")
            continue

        holders = sorted(
            selected["id"]
            for selected in batch
            if touches_overlap(candidate_touches, selected.get("touches") or [])
        )
        if holders:
            overlap = sorted(
                set(candidate_touches)
                & {
                    touch
                    for selected in batch
                    for touch in (selected.get("touches") or [])
                }
            ) or ["*"]
            skipped_reasons.append(
                f"{candidate_id} conflicts on {', '.join(overlap)} "
                f"with selected ticket(s) {', '.join(holders)}"
            )
            continue

        if not top.get("parallel_safe"):
            skipped_reasons.append(f"{candidate_id} held because {top['id']} is serial")
            continue

    if skipped_reasons:
        prefix = (
            f"selected {top['id']} has parallel_safe=false"
            if not top.get("parallel_safe")
            else f"only {len(batch)} compatible ticket(s) available for cap {max_parallel}"
        )
        return f"{prefix}; skipped peers: {'; '.join(skipped_reasons[:3])}"

    if not top.get("parallel_safe"):
        return f"selected {top['id']} has parallel_safe=false"

    return f"only {len(batch)} compatible ticket(s) available for cap {max_parallel}"


def _review_queue_lines(tickets: list[dict], milestone: str | None = None) -> list[str]:
    review_tickets = [
        t
        for t in tickets
        if t.get("status") == "in_review"
        and (milestone is None or t.get("milestone") == milestone)
    ]
    lines: list[str] = []
    for ticket in sorted(review_tickets, key=lambda x: x.get("priority", 99)):
        tid = ticket["id"]
        verdict = ticket.get("review_verdict") or "pending verdict"
        summary = ticket.get("review_summary") or ""
        reason = attention_summary(ticket)
        line = f"  {tid}: {verdict}"
        lines.append(line)
        if summary:
            lines.append(f"    summary: {summary}")
        if reason:
            lines.append(f"    reason: {reason}")
        if verdict == "approved":
            lines.append(f"    next: lanegate merge {tid}")
        elif verdict == "pending verdict":
            lines.append(f"    next: lanegate review {tid} --verdict approved")
        elif verdict == "changes_requested":
            lines.append(f"    next: address feedback, then lanegate review {tid} --verdict approved")
    return lines


def _ticket_next_step_line(ticket: dict) -> str | None:
    tid = ticket.get("id")
    if not tid:
        return None
    status = ticket.get("status")
    verdict = ticket.get("review_verdict")
    if status == "in_progress":
        return f"{tid}: implementation running or claimed - check: lanegate orchestrate --status"
    if status == "hibernated":
        return f"{tid}: hibernated - next: lanegate orchestrate"
    if status == "needs_review":
        return (
            f"{tid}: needs_review - inspect worktree, then: "
            f"lanegate reopen {tid} && lanegate orchestrate"
        )
    if status == "code_complete" and verdict == "changes_requested":
        return (
            f"{tid}: changes_requested - address feedback, then: "
            f"lanegate review {tid} --verdict approved"
        )
    if status == "code_complete":
        return f"{tid}: code_complete - next: lanegate review {tid} --verdict approved"
    if status == "in_review" and verdict == "approved":
        return f"{tid}: approved - next: lanegate merge {tid}"
    if status == "in_review":
        return f"{tid}: in_review - next: lanegate review {tid} --verdict approved"
    if status == "failed":
        return (
            f"{tid}: failed - inspect log/worktree, then: "
            f"lanegate reopen {tid} && lanegate orchestrate"
        )
    return None


def _continuation_step_lines(tickets: list[dict], milestone: str | None = None) -> list[str]:
    actionable = [
        t
        for t in tickets
        if t.get("status")
        in {"in_progress", "hibernated", "needs_review", "code_complete", "failed"}
        and (milestone is None or t.get("milestone") == milestone)
    ]
    lines = []
    for ticket in sorted(actionable, key=lambda x: x.get("priority", 99)):
        line = _ticket_next_step_line(ticket)
        if line:
            lines.append(f"  {line}")
            reason = attention_summary(ticket)
            if reason:
                lines.append(f"    reason: {reason}")
    return lines


def _print_continuation_steps(
    tickets: list[dict],
    *,
    milestone: str | None = None,
    stream=None,
) -> bool:
    lines = _continuation_step_lines(tickets, milestone=milestone)
    if not lines:
        return False
    out = stream or sys.stdout
    print("[orchestrate] next steps:", file=out)
    for line in lines:
        print(line, file=out)
    return True


def _print_review_queue(
    tickets: list[dict],
    *,
    milestone: str | None = None,
    stream=None,
) -> bool:
    lines = _review_queue_lines(tickets, milestone=milestone)
    if not lines:
        return False
    out = stream or sys.stdout
    print("[orchestrate] review queue:", file=out)
    for line in lines:
        print(line, file=out)
    return True
