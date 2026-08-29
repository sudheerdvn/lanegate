"""
board.py — board, next, pipeline-status commands.

- board: groups tickets by status (never silently drops unknown statuses).
- next: greedy non-overlapping touches batch.
- pipeline-status: loops over ordered environments; zero-env safe.
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from typing import NoReturn

from lanegate.concurrency import locked_touches, touches_overlap
from lanegate.config import (
    ConfigError,
    resolve_autonomy,
    resolve_executor_route,
    resolve_ticket_pool,
    resolve_trunk_branch,
    validate_model_for_executor,
)
from lanegate.executor import discover_ollama_context, get_executor_config
from lanegate.git import PendingCommits, pending_commits, verify_local_branch
from lanegate.lifecycle import rejected_auto_fix_recovery_reason, resolve_reviewer
from lanegate.ticket import (
    _STANDARD_STATUSES,
    TERMINAL_STATUSES,
    _active_rate_limit_hibernation,
    _active_reviewer_cooldown_hibernation,
    attention_category,
    attention_summary,
    branch_name,
    canonical_id,
    classify_needs_review_cause,
    get_ticket_summary,
    group_by_status,
    load_all_tickets,
    needs_attention,
    needs_review_recovery_advice,
    reviewer_cooldown_retry_pending,
    unresolved_dependencies,
    validate_ticket,
)


def _time_in_status(ticket: dict) -> str:
    """Return a human-readable age string for how long a ticket has been in its current status.

    Returns "—" when status_changed_at is absent or unparseable (graceful handling
    for tickets created before this field existed).
    """
    raw = ticket.get("status_changed_at")
    if not raw:
        return "—"
    try:
        # Handle both "Z" suffix and "+00:00" offset variants
        ts = raw.replace("Z", "+00:00")
        changed = datetime.datetime.fromisoformat(ts)
        now = datetime.datetime.now(datetime.UTC)
        delta = now - changed
        total_seconds = int(delta.total_seconds())
        if total_seconds < 0:
            return "—"
        if total_seconds < 3600:
            return f"{total_seconds // 60}m"
        if total_seconds < 86400:
            return f"{total_seconds // 3600}h"
        return f"{total_seconds // 86400}d"
    except (ValueError, TypeError):
        return "—"


def _missing_branch_message(branch: str, base: str) -> str:
    return (
        f"branch {branch!r} does not exist — create it "
        f"(git branch {branch} {base}) or remove it from environments in .lanegate.yml"
    )


def _environment_pending(
    repo_root: Path, env: dict, base: str
) -> tuple[PendingCommits, str | None]:
    """Return pending commits plus a friendly missing-branch diagnostic, if applicable."""
    branch = env["branch"]
    pending = pending_commits(repo_root, branch, base)
    if pending.ok:
        return pending, None

    # Keep pending_commits' diagnostic for every unexpected range-query failure.
    # Only replace it after Git specifically confirms that the environment branch
    # itself is absent.
    branch_ref = verify_local_branch(repo_root, branch)
    if branch_ref.ok and not branch_ref.text:
        return pending, _missing_branch_message(branch, base)
    return pending, None


def _pending_payload(
    env: dict, pending: PendingCommits, base: str, missing_branch_error: str | None = None
) -> dict:
    """Build a pipeline JSON entry without disguising Git errors as empty ranges.

    Only the count is embedded, matching what text-mode board already shows — the full
    commit list isn't consumed anywhere and a large promotion backlog (thousands of
    commits) would otherwise blow past MCP tool output limits on every board call.
    """
    return {
        "env": env["name"],
        "base": base,
        "head": env["branch"],
        "trigger": env.get("trigger", "manual"),
        "pending_state": "ok" if pending.ok else "unknown",
        "pending_error": missing_branch_error or pending.error,
        "pending_count": len(pending.commits) if pending.ok else None,
    }


def latest_dispatch_executors(repo_root: Path) -> dict[str, str]:
    """Return ticket -> executor from the latest orchestrate dispatch log."""
    logs_dir = repo_root / ".lanegate" / "logs"
    pointer_path = logs_dir / "last-run.json"
    events_path: Path | None = None
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        raw_events_path = pointer.get("events_path")
        if raw_events_path:
            events_path = Path(raw_events_path)
    except (OSError, json.JSONDecodeError):
        pass

    if events_path is None:
        try:
            events = sorted(logs_dir.glob("orchestrate-*.events.jsonl"))
        except OSError:
            events = []
        if not events:
            return {}
        events_path = events[-1]

    dispatches: dict[str, str] = {}
    try:
        raw = events_path.read_text(encoding="utf-8")
    except OSError:
        return dispatches

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") != "ticket_dispatch":
            continue
        tid = event.get("ticket_id")
        executor = event.get("executor")
        if tid and executor:
            dispatches[tid] = executor
    return dispatches


# Full-history fields that the terminal board never displays (directly, or via the
# condensed attention_summary/needs_review_cause helpers below) -- kept out of the
# board/MCP JSON payload since they only grow over a ticket's lifetime and can dwarf
# everything else on tickets with a long review/rework history. Full detail is still
# available per-ticket via get_ticket_detail() / GET /api/tickets/{id}.
_BOARD_EXCLUDED_FIELDS = frozenset({
    "change_notes",
    "lifecycle_events",
    "acceptance_matrix",
    "close_criteria",
    "review_findings",
    "human_review_rationale",
    "human_review_actor",
    "acceptance_contract_audit",
})


def _ticket_data(
    t: dict,
    cfg: dict | None = None,
    *,
    dispatched_executor: str | None = None,
) -> dict:
    data = {
        k: v
        for k, v in t.items()
        if not k.startswith("_") and k not in _BOARD_EXCLUDED_FIELDS
    }
    attention_ticket = _attention_ticket(t, cfg)
    category = attention_category(attention_ticket)
    data["needs_attention"] = bool(category)
    if category:
        data["attention_category"] = category
    summary = attention_summary(attention_ticket)
    if summary:
        data["attention_summary"] = summary
    if t.get("status") == "needs_review":
        data["needs_review_cause"] = classify_needs_review_cause(t)
        data["needs_review_recovery"] = needs_review_recovery_advice(t)
    if cfg is not None:
        route = resolve_executor_route(cfg, t)
        data["reviewer"] = resolve_reviewer(t, cfg)
        data["implement_executor"] = route["implement"]
        data["review_executor"] = route["review"]
        data["execution_mode"] = route["mode"]
        data["executor_route"] = route
        if cfg.get("routing") or cfg.get("default_pool"):
            routed_pool, routed_pool_reason = resolve_ticket_pool(cfg, t)
            data["routed_pool"] = routed_pool or "unrouted"
            data["routed_pool_reason"] = routed_pool_reason
        if t.get("status") == "in_progress":
            if dispatched_executor:
                data["executor_instance"] = dispatched_executor
                return data
            # surface the resolved named executor instance (e.g.
            # "claude-1") rather than the bare type for in-progress tickets.
            # get_executor_config always returns an "instance" key, falling
            # back to the bare type when no named instance is configured.
            executor_cfg = get_executor_config(route["implement"], cfg)
            data["executor_instance"] = executor_cfg.get("instance") or route["implement"]
    return data


def _render_board_tickets(
    cfg: dict,
    tickets_dir: Path,
    json_output: bool,
    show_all: bool,
    milestone: str | None = None,
) -> tuple[list, dict, list]:
    """Load and filter tickets; return (all_tickets, grouped, quarantined) for rendering."""

    all_tickets, quarantined = _load_board_tickets(cfg, tickets_dir)
    hidden_by_default = TERMINAL_STATUSES - {"failed"}
    visible = (
        all_tickets
        if show_all
        else [t for t in all_tickets if t.get("status") not in hidden_by_default]
    )
    if milestone is not None:
        visible = [t for t in visible if t.get("milestone") == milestone]
    grouped = group_by_status(visible)
    return all_tickets, grouped, quarantined


def _load_board_tickets(cfg: dict, tickets_dir: Path):
    """Load tickets after confirming this is a usable LaneGate project."""
    if not tickets_dir.is_dir():
        raise ConfigError(
            "not a lanegate project — run from inside a project or `lanegate init`"
        )
    return load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)


def _ticket_flags(t: dict) -> str:
    parts = []
    if t.get("equivalent_ticket_id"):
        # reconciliation found this ticket's intent already
        # covered by an already-merged ticket.
        parts.append(f"superseded-by:{t['equivalent_ticket_id']}")
    elif t.get("replacement_commit"):
        # this ticket's own branch was already an ancestor of
        # main -- the work landed through some other path.
        parts.append(f"superseded-by:{t['replacement_commit'][:8]}")
    if t.get("feature_flag"):
        parts.append(f"flag:{t['feature_flag']}")
    if t.get("depends_on"):
        parts.append(f"needs:{', '.join(t['depends_on'])}")
    if t.get("worktree"):
        parts.append(f"wt:{Path(t['worktree']).name}")
    if t.get("review_verdict"):
        parts.append(t["review_verdict"])
    route = t.get("executor_route")
    if route and route.get("mode") == "split":
        parts.append(f"exec:{route['implement']}->{route['review']} split")
    elif t.get("executor_instance"):
        # in-progress, combined-mode tickets show the resolved
        # named executor instance (e.g. "claude-1"); falls back to the bare
        # type when no named instance is configured, via _ticket_data.
        parts.append(f"exec:{t['executor_instance']}")
    if "routed_pool" in t:
        # only present when routing:/default_pool is configured
        # for this project (see _ticket_data) — keeps non-routing boards clean.
        parts.append(f"pool:{t['routed_pool']}")
    if not t.get("milestone"):
        parts.append("no milestone")
    return "  ".join(parts)


def _new_board_table(console_width: int):
    """Return a board table and whether it is using the compact narrow layout."""
    from rich import box
    from rich.table import Table

    table = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold dim",
        padding=(0, 1),
        expand=True,
    )

    if console_width < 100:
        table.add_column("ID", style="cyan", no_wrap=True, min_width=8)
        table.add_column("P", justify="center", no_wrap=True, min_width=1)
        table.add_column("MS", no_wrap=True, min_width=2)
        table.add_column("Age", justify="right", no_wrap=True, min_width=3)
        table.add_column("Title / Flags", ratio=1, overflow="fold")
        return table, True

    table.add_column("ID", style="cyan", no_wrap=True, min_width=8)
    table.add_column("P", justify="center", no_wrap=True, min_width=1)
    table.add_column("MS", no_wrap=True, min_width=2)
    table.add_column("Age", justify="right", no_wrap=True, min_width=3)
    table.add_column("Title", ratio=4, overflow="fold")
    table.add_column("Flags", style="dim", ratio=1, overflow="fold")
    return table, False


def _add_board_row(table, ticket: dict, compact: bool) -> None:
    from rich.text import Text

    flags = _ticket_flags(ticket)
    if compact:
        summary = Text(ticket.get("title", ""))
        if flags:
            summary.append("\n")
            summary.append(flags, style="dim")
        table.add_row(
            ticket["id"],
            str(ticket.get("priority", "-")),
            ticket.get("milestone") or "",
            _time_in_status(ticket),
            summary,
        )
        return

    table.add_row(
        ticket["id"],
        str(ticket.get("priority", "-")),
        ticket.get("milestone") or "",
        _time_in_status(ticket),
        ticket.get("title", ""),
        flags,
    )


def _review_detail_lines(tickets: list[dict]) -> list[str]:
    lines: list[str] = []
    for ticket in sorted(tickets, key=lambda x: x.get("priority", 99)):
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


def _next_step_lines(
    tickets: list[dict], milestone: str | None = None, cfg: dict | None = None
) -> list[str]:
    cfg = cfg or {}
    actionable = [
        t
        for t in tickets
        if (
            t.get("status") in {"in_progress", "code_complete", "hibernated"}
            or needs_attention(t)
        )
        and (milestone is None or t.get("milestone") == milestone)
    ]
    lines: list[str] = []
    for ticket in sorted(actionable, key=lambda x: x.get("priority", 99)):
        tid = ticket["id"]
        status = ticket.get("status")
        verdict = ticket.get("review_verdict")
        reason = attention_summary(ticket)
        if not reason:
            reason = ticket.get("review_pending_reason") or ""

        is_rate_limited = _active_rate_limit_hibernation(ticket)

        if status == "in_progress":
            lines.append(
                f"  {tid}: implementation running or claimed - "
                "check: lanegate run --status"
            )
        elif status == "hibernated" and ticket.get("review_pending"):
            if _active_reviewer_cooldown_hibernation(ticket):
                lines.append(
                    f"  {tid}: review_pending (reviewer cooldown) - independent "
                    "reviewer temporarily unavailable; auto-retries after "
                    f"{ticket.get('review_retry_after')} - next: lanegate run"
                )
            elif is_rate_limited:
                lines.append(
                    f"  {tid}: review_pending (rate-limited) - reviewer rate limited; "
                    "next: lanegate run (auto-recovers on quota reset)"
                )
            else:
                lines.append(
                    f"  {tid}: review_pending - reviewer did not return a verdict; "
                    "next: lanegate run"
                )
            if reason:
                lines.append(f"    reason: {reason}")
        elif status == "hibernated" and is_rate_limited:
            lines.append(
                f"  {tid}: hibernated (rate-limited) - rate limit interrupted execution; "
                "next: lanegate run (auto-resumes on quota reset)"
            )
            if reason:
                lines.append(f"    reason: {reason}")
        elif status != "code_complete" and not needs_attention(ticket):
            # code_complete is exempt: the top-level `actionable` filter above
            # always includes it (same as in_progress/hibernated), but
            # needs_attention()/attention_category() never returns true for
            # a plain code_complete ticket -- nothing is "stuck", it's just
            # waiting for review. Without this exemption the `code_complete`
            # branch below is unreachable and a freshly-completed ticket gets
            # silently dropped from Next Steps instead of pointing at
            # `lanegate review <id> --verdict approved` / `lanegate run`.
            continue
        elif status == "hibernated":
            lines.append(
                f"  {tid}: hibernated - inspect log/worktree, then: "
                f"lanegate reopen {tid} && lanegate run"
            )
            if reason:
                lines.append(f"    reason: {reason}")
        elif status == "needs_review":
            # Do not let a stale rate-limit hibernation marker override a
            # later hard-blocked escalation.  The classifier prioritizes the
            # active explicit reason and is the source of truth for all
            # needs_review guidance.
            cause = classify_needs_review_cause(ticket)
            advice = needs_review_recovery_advice(ticket)
            lines.append(f"  {tid}: needs_review ({cause}) - {advice}")
            if reason:
                lines.append(f"    reason: {reason}")
        elif verdict == "changes_requested":
            recovery_reason = rejected_auto_fix_recovery_reason(ticket, cfg)
            if recovery_reason:
                lines.append(
                    f"  {tid}: exhausted rejected review - after manual fixes, request "
                    f"a fresh independent review: lanegate review {tid}"
                )
                lines.append(
                    f"    To release its touch lock without re-reviewing: "
                    f"lanegate recover-rejected {tid}"
                )
            else:
                lines.append(
                    f"  {tid}: changes_requested - address feedback, then request "
                    f"a fresh independent review: lanegate review {tid}"
                )
            if reason:
                lines.append(f"    reason: {reason}")
        elif status == "in_review" and verdict == "approved":
            lines.append(f"  {tid}: approved - awaiting human merge decision: lanegate merge {tid}")
        elif status == "code_complete":
            lines.append(
                f"  {tid}: code_complete - awaiting review; next: lanegate run "
                f"(dispatches an independent reviewer), or lanegate review {tid} "
                "--verdict approved to self-approve without one"
            )
        elif status == "merged" and ticket.get("post_merge_diagnostic"):
            lines.append(
                f"  {tid}: post_merge_diagnostic - fix the diagnostic, then "
                f"lanegate validate {tid}"
            )
            if reason:
                lines.append(f"    reason: {reason}")
        elif status == "failed":
            lines.append(
                f"  {tid}: failed - inspect log/worktree, then: "
                f"lanegate reopen {tid} && lanegate run"
            )
            if reason:
                lines.append(f"    reason: {reason}")
    return lines


def _print_board_text(
    cfg: dict,
    repo_root: Path,
    all_tickets: list,
    grouped: dict,
    show_all: bool,
    milestone: str | None = None,
) -> None:
    from rich.console import Console

    from lanegate import APP_NAME

    # highlight=False: rich's default ReprHighlighter splits digit runs
    # (e.g. "TICK-001") into separate style spans, which breaks plain-text
    # substring checks on ticket IDs downstream (in tests and anywhere else
    # that greps this output) without changing anything visible to a human.
    console = Console(highlight=False)

    closed_count = sum(1 for t in all_tickets if t.get("status") in TERMINAL_STATUSES)
    suffix = (
        f"  ({closed_count} closed hidden — use --all to show)"
        if not show_all and closed_count
        else ""
    )
    ms_suffix = (
        f"  [dim](milestone: {milestone} — use --all-milestones to show all)[/dim]"
        if milestone
        else ""
    )
    console.print(f"\n[bold]Ticket Board[/bold]{suffix}{ms_suffix}\n")

    ordered_statuses = list(_STANDARD_STATUSES) + [
        s for s in grouped if s not in _STANDARD_STATUSES
    ]
    status_counts = [(s, len(grouped[s])) for s in ordered_statuses if grouped.get(s)]
    if status_counts:
        summary = "  ".join(f"{status.replace('_', ' ')}: {count}" for status, count in status_counts)
        scoped_total = sum(count for _, count in status_counts)
        console.print(f"[dim]{summary}  —  total: {scoped_total}[/dim]\n")

    dispatch_executors = latest_dispatch_executors(repo_root)
    printed = set()
    for status in _STANDARD_STATUSES:
        group = grouped.get(status, [])
        if not group:
            continue
        printed.add(status)
        label = status.upper().replace("_", " ")

        table, compact = _new_board_table(console.width)

        for t in sorted(group, key=lambda x: x.get("priority", 99)):
            _add_board_row(
                table,
                _ticket_data(t, cfg, dispatched_executor=dispatch_executors.get(t["id"])),
                compact,
            )

        console.print(f"[bold yellow]{label} ({len(group)})[/bold yellow]")
        console.print(table)
        if status == "draft":
            sorted_group = sorted(group, key=lambda x: x.get("priority", 99))
            # analyze can populate touches without flipping draft -> open (e.g.
            # `lanegate create --no-analyze` followed by a separate analyze
            # call). A draft that already has touches just needs `open`; only
            # one still lacking touches needs `analyze` -- collapsing both
            # into one blanket "analyze (auto-populate) or open (if you
            # already set touches manually)" message told a ticket that had
            # just been auto-analyzed to re-run analyze, and mischaracterized
            # its already-populated touches as something the user set by hand.
            ready = [t["id"] for t in sorted_group if t.get("touches")]
            needs_touches = [t["id"] for t in sorted_group if not t.get("touches")]
            if ready:
                console.print(
                    f"[dim]  Touches already populated — run `lanegate open <id>` "
                    f"to start: {' / '.join(ready)}[/dim]"
                )
            if needs_touches:
                console.print(
                    f"[dim]  No touches yet — run `lanegate analyze <id>` to "
                    f"populate them: {' / '.join(needs_touches)}[/dim]"
                )
            console.print()
        if status == "in_review":
            console.print("[bold]Review[/bold]")
            for line in _review_detail_lines(group):
                # soft_wrap=True: see the missing_branch_error print above --
                # same wrap-induced ANSI-splitting risk for any long detail line.
                console.print(f"[dim]{line}[/dim]", soft_wrap=True)
            console.print()

    next_lines = _next_step_lines(all_tickets, milestone=milestone, cfg=cfg)
    if next_lines:
        console.print("[bold]Next Steps[/bold]")
        for line in next_lines:
            # soft_wrap=True: same wrap-induced ANSI-splitting risk as above --
            # next-step guidance strings are long enough to wrap at typical
            # console widths, which re-emits the [dim] style's SGR codes mid-word.
            console.print(f"[dim]{line}[/dim]", soft_wrap=True)
        console.print()

    other_statuses = set(grouped) - printed
    if other_statuses:
        table, compact = _new_board_table(console.width)
        other_count = sum(len(grouped[s]) for s in other_statuses)
        console.print(f"[bold yellow]OTHER ({other_count})[/bold yellow]")
        for status in sorted(other_statuses):
            for t in grouped[status]:
                _add_board_row(
                    table,
                    _ticket_data(t, cfg, dispatched_executor=dispatch_executors.get(t["id"])),
                    compact,
                )
        console.print(table)

    envs = cfg.get("environments", [])
    if envs:
        console.print("[bold]Pipeline[/bold]")
        for env in envs:
            base = env.get("from", resolve_trunk_branch(cfg, repo_root))
            head = env["branch"]
            trigger = env.get("trigger", "manual")
            pending, missing_branch_error = _environment_pending(repo_root, env, base)
            if missing_branch_error:
                # soft_wrap=True: this is a single diagnostic line. Without it, Rich
                # word-wraps at console.width and re-emits the style's SGR codes
                # around the wrap point, splitting words mid-string with invisible
                # ANSI bytes -- breaks plain-text substring checks (tests, grep)
                # without changing anything a human sees.
                console.print(
                    f"  {base} → {head} : [yellow]{missing_branch_error}[/yellow]",
                    soft_wrap=True,
                )
            elif not pending.ok:
                console.print(
                    f"  {base} → {head} : [yellow]unknown[/yellow] "
                    f"(could not determine pending commits: {pending.error})"
                )
            elif pending.commits:
                action = (
                    "(hook-driven)" if trigger == "auto" else f"{APP_NAME} promote {env['name']}"
                )
                console.print(
                    f"  {base} → {head} : {len(pending.commits)} commit(s) pending  →  {action}"
                )
            else:
                console.print(f"  {base} → {head} : [green]up to date[/green]")

    from lanegate.pending_globals import check_pending_globals, format_pending_globals_notice
    pg_info = check_pending_globals(repo_root)
    if pg_info["has_pending"]:
        console.print(f"\n{format_pending_globals_notice(pg_info, rich_markup=True)}")

        console.print()


def _board_json_payload(cfg: dict, repo_root: Path, grouped: dict) -> dict:
    result_tickets: dict[str, list] = {}
    dispatch_executors = latest_dispatch_executors(repo_root)
    printed: set[str] = set()
    for status in _STANDARD_STATUSES:
        group = grouped.get(status, [])
        if group:
            printed.add(status)
            result_tickets[status] = [
                _ticket_data(t, cfg, dispatched_executor=dispatch_executors.get(t["id"]))
                for t in sorted(group, key=lambda x: x.get("priority", 99))
            ]
    for status in sorted(set(grouped) - printed):
        result_tickets[status] = [
            _ticket_data(t, cfg, dispatched_executor=dispatch_executors.get(t["id"]))
            for t in grouped[status]
        ]

    pipeline = []
    for env in cfg.get("environments", []):
        base = env.get("from", resolve_trunk_branch(cfg, repo_root))
        pending, missing_branch_error = _environment_pending(repo_root, env, base)
        pipeline.append(_pending_payload(env, pending, base, missing_branch_error))
    return {"tickets": result_tickets, "pipeline": pipeline}


def _cmd_board_global(
    cfg: dict, repo_root: Path, json_output: bool = False, show_all: bool = False
) -> None:
    """Aggregate board across all registered projects."""
    from lanegate.config import CONFIG_FILENAME, load_config, registry_load

    entries = registry_load()
    if not entries:
        print(
            "No projects registered. Use `lanegate projects scan <dir>` to register projects.",
            file=sys.stderr,
        )
        return

    if json_output:
        result = []
        for entry in entries:
            proj_path = Path(entry.get("path", ""))
            proj_name = entry.get("name", proj_path.name)
            config_file = proj_path / CONFIG_FILENAME
            if not config_file.exists():
                print(
                    f"WARNING: skipping {proj_name!r} — config not found at {proj_path}",
                    file=sys.stderr,
                )
                continue
            try:
                proj_cfg = load_config(proj_path)
            except Exception as exc:
                print(
                    f"WARNING: skipping {proj_name!r} — failed to load config: {exc}",
                    file=sys.stderr,
                )
                continue
            tickets_dir = proj_path / proj_cfg["tickets_dir"]
            if not tickets_dir.exists():
                print(
                    f"WARNING: skipping {proj_name!r} — tickets dir missing: {tickets_dir}",
                    file=sys.stderr,
                )
                continue
            all_tickets, grouped, _q = _render_board_tickets(
                proj_cfg, tickets_dir, json_output=False, show_all=show_all
            )
            flat: list[dict] = []
            dispatch_executors = latest_dispatch_executors(proj_path)
            for group in grouped.values():
                flat.extend(
                    _ticket_data(t, proj_cfg, dispatched_executor=dispatch_executors.get(t["id"]))
                    for t in group
                )
            result.append({"project": proj_name, "path": str(proj_path), "tickets": flat})
        print(json.dumps(result, indent=2, default=str))
        return

    # Text mode
    any_printed = False
    for entry in entries:
        proj_path = Path(entry.get("path", ""))
        proj_name = entry.get("name", proj_path.name)
        config_file = proj_path / CONFIG_FILENAME
        if not config_file.exists():
            print(
                f"WARNING: skipping {proj_name!r} — config not found at {proj_path}",
                file=sys.stderr,
            )
            continue
        try:
            proj_cfg = load_config(proj_path)
        except Exception as exc:
            print(
                f"WARNING: skipping {proj_name!r} — failed to load config: {exc}", file=sys.stderr
            )
            continue
        tickets_dir = proj_path / proj_cfg["tickets_dir"]
        if not tickets_dir.exists():
            print(
                f"WARNING: skipping {proj_name!r} — tickets dir missing: {tickets_dir}",
                file=sys.stderr,
            )
            continue

        all_tickets, grouped, _q = _render_board_tickets(
            proj_cfg, tickets_dir, json_output=False, show_all=show_all
        )
        open_count = sum(len(g) for g in grouped.values())
        if open_count == 0 and not show_all:
            continue  # nothing to show for this project

        print(f"=== {proj_name} ({proj_path}) ===\n")
        _print_board_text(proj_cfg, proj_path, all_tickets, grouped, show_all)
        any_printed = True

    if not any_printed:
        print("No open tickets across registered projects.")


def get_board_state(cfg: dict, repo_root: Path) -> dict:
    """Return board state as a JSON-serializable dict (API-friendly wrapper)."""
    tickets_dir = repo_root / cfg["tickets_dir"]
    _, grouped, _ = _render_board_tickets(cfg, tickets_dir, json_output=False, show_all=False)
    return _board_json_payload(cfg, repo_root, grouped)


def get_tickets(cfg: dict, repo_root: Path, show_all: bool = False) -> list[dict]:
    """Return all visible tickets as a flat JSON-serializable list."""
    tickets_dir = repo_root / cfg["tickets_dir"]
    _, grouped, _ = _render_board_tickets(cfg, tickets_dir, json_output=False, show_all=show_all)
    result: list[dict] = []
    for group in grouped.values():
        result.extend(_ticket_data(t, cfg) for t in group)
    return result


def _print_quarantine(quarantined: list) -> None:
    """Print quarantined tickets and their validation errors."""
    if not quarantined:
        print("No quarantined tickets.")
        return
    print(f"=== Quarantined Tickets ({len(quarantined)}) ===\n")
    for q in quarantined:
        print(f"  {q.path.name}")
        print(f"    error: {q.error}")
    print()


def cmd_board(
    cfg: dict,
    repo_root: Path,
    json_output: bool = False,
    show_all: bool = False,
    global_view: bool = False,
    show_quarantine: bool = False,
    milestone: str | None = None,
    all_milestones: bool = False,
) -> None:
    if global_view:
        _cmd_board_global(cfg, repo_root, json_output=json_output, show_all=show_all)
        return

    effective_milestone = None if all_milestones else milestone
    if effective_milestone is None and not all_milestones:
        effective_milestone = cfg.get("default_milestone")

    tickets_dir = repo_root / cfg["tickets_dir"]
    all_tickets, grouped, quarantined = _render_board_tickets(
        cfg, tickets_dir, json_output=json_output, show_all=show_all, milestone=effective_milestone
    )

    if show_quarantine:
        if json_output:
            payload = [{"path": str(q.path), "error": q.error} for q in quarantined]
            print(json.dumps({"quarantined": payload}, indent=2, default=str))
        else:
            _print_quarantine(quarantined)
        return

    if quarantined:
        print(
            f"WARNING: {len(quarantined)} ticket(s) failed validation and were quarantined. Run `lanegate board --quarantine` to inspect.\n",
            file=sys.stderr,
        )

    if json_output:
        print(json.dumps(_board_json_payload(cfg, repo_root, grouped), indent=2, default=str))
        return

    _print_board_text(cfg, repo_root, all_tickets, grouped, show_all, milestone=effective_milestone)


def cmd_next(
    cfg: dict,
    repo_root: Path,
    json_output: bool = False,
    milestone: str | None = None,
    in_flight: list[str] | None = None,
) -> None:
    tickets_dir = repo_root / cfg["tickets_dir"]
    lock_statuses = cfg["lock_statuses"]

    tickets, _ = _load_board_tickets(cfg, tickets_dir)
    status_map = {t["id"]: t.get("status") for t in tickets}
    locked = locked_touches(tickets, lock_statuses)

    # Pre-filter: union in touches from orchestrator-held (in-flight) tickets so the
    # orchestrator can call `lanegate next` immediately after starting a ticket without
    # waiting for the worktree lock to propagate.
    if in_flight:
        ticket_by_id = {t["id"]: t for t in tickets}
        for tid in in_flight:
            t = ticket_by_id.get(tid)
            if t is None:
                continue  # unknown ticket — skip silently
            for touch in t.get("touches") or []:
                locked.add(touch)

    in_progress_tickets = [t for t in tickets if t.get("status") == "in_progress"]

    candidates = []
    for t in tickets:
        if t.get("status") not in ("open", "hibernated"):
            continue
        if t.get("status") == "open" and not t.get("touches"):
            continue
        if (
            t.get("status") == "hibernated"
            and t.get("review_pending")
            and reviewer_cooldown_retry_pending(t)
        ):
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

    top = candidates[0] if candidates else None
    peers: list[dict] = []
    if top and top.get("parallel_safe"):
        taken = set(top.get("touches") or [])
        for c in candidates[1:]:
            if not c.get("parallel_safe"):
                continue
            c_touches = set(c.get("touches") or [])
            if not touches_overlap(c_touches, taken):
                peers.append(c)
                taken |= c_touches

    if json_output:
        print(
            json.dumps(
                {
                    "in_flight": [_ticket_data(t, cfg) for t in in_progress_tickets],
                    "next": _ticket_data(top, cfg) if top else None,
                    "peers": [_ticket_data(p, cfg) for p in peers],
                },
                indent=2,
                default=str,
            )
        )
        return

    if in_progress_tickets:
        print("In progress (claimed — do not re-start):")
        for t in in_progress_tickets:
            print(f"  {t['id']:12s}  {t.get('title', '')[:60]}")
        print()

    if not candidates:
        print("No unblocked open tickets (or hibernated tickets).")
        next_lines = _next_step_lines(tickets, milestone=milestone, cfg=cfg)
        if next_lines:
            print("\nNext steps:")
            for line in next_lines:
                print(line)
        return
    assert top is not None  # candidates non-empty => top was set from candidates[0]

    top_branch = branch_name(top["id"])
    top_route = resolve_executor_route(cfg, top)
    print(f"Next: {top['id']} — {top['title']}")
    print(
        f"  branch={top_branch}  executor={top_route['implement']}  "
        f"review={top_route['review']}  mode={top_route['mode']}"
    )
    top_touches = top.get("touches") or []
    if top_touches:
        print(f"  touches: {', '.join(top_touches)}")

    if peers:
        print("\nParallel-safe batch (can start simultaneously):")
        for p in peers:
            p_branch = branch_name(p["id"])
            p_route = resolve_executor_route(cfg, p)
            p_touches = p.get("touches") or []
            touches_str = f"  [{', '.join(p_touches)}]" if p_touches else ""
            print(
                f"  {p['id']:12s}  p{p.get('priority', '-')}  branch={p_branch}  "
                f"executor={p_route['implement']}  review={p_route['review']}  "
                f"mode={p_route['mode']}  {p.get('title', '')[:50]}{touches_str}"
            )


def _fail_json_or_text(json_output: bool, message: str, *, payload: dict | None = None) -> NoReturn:
    """Print an error and exit(1) -- the shared tail of every board command's
    not-found/validation-failure path, as `{"error": ...}` JSON or `ERROR: ...`
    text depending on ``json_output``. ``payload`` overrides the default
    ``{"error": message}`` JSON body for a caller (e.g. cmd_summary) that
    already has a richer error dict to print.
    """
    if json_output:
        print(json.dumps(payload if payload is not None else {"error": message}, indent=2, default=str))
    else:
        print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def cmd_route(
    cfg: dict,
    repo_root: Path,
    ticket_id: str,
    json_output: bool = False,
    reviewer: str | None = None,
    executor: str | None = None,
    model: str | None = None,
) -> None:
    """Show or update which pool `ticket_id` would be routed to, and why."""
    tickets_dir = repo_root / cfg["tickets_dir"]
    all_tickets, _ = _load_board_tickets(cfg, tickets_dir)
    tid = canonical_id(ticket_id)
    ticket = None
    for t in all_tickets:
        try:
            if canonical_id(t.get("id", "")) == tid:
                ticket = t
                break
        except ValueError:
            continue

    if ticket is None:
        _fail_json_or_text(json_output, f"ticket {tid} not found")

    updated = False
    msg_parts = []
    updated_ticket = dict(ticket)
    if reviewer is not None:
        updated_ticket["reviewer"] = reviewer
        msg_parts.append(f"reviewer → {reviewer}")
        updated = True
    if executor is not None:
        recorded_implementer = ticket.get("implement_session_executor")
        if recorded_implementer and executor != recorded_implementer:
            _fail_json_or_text(
                json_output,
                f"cannot change executor for {tid} after implementation; "
                f"recorded implementer is {recorded_implementer!r}",
            )
        updated_ticket["executor"] = executor
        msg_parts.append(f"executor → {executor}")
        updated = True
    if model is not None:
        updated_ticket["review_model_pin"] = model
        msg_parts.append(f"model → {model}")
        updated = True

    if updated:
        validation_errors = validate_ticket(updated_ticket, cfg)
        if executor is not None and not validation_errors:
            # A bare executor pin can make the global implementation model
            # apply to a different executor family.  Resolve and validate it
            # now, before persisting a route that would fail at dispatch.
            from lanegate.config import resolve_model
            from lanegate.orchestrate.pool import (
                _ticket_for_model_resolution,
                expand_driver,
            )

            implementer = resolve_executor_route(cfg, updated_ticket)["implement"]
            route_driver_cfg = expand_driver(implementer, cfg)
            resolved_executor = route_driver_cfg.get("type", implementer)
            executor_cfg = get_executor_config(resolved_executor, cfg)
            executor_type = executor_cfg.get("type", resolved_executor)
            effective_cfg = (
                dict(cfg, executor=resolved_executor)
                if resolved_executor != cfg.get("executor")
                else cfg
            )
            model_ticket = _ticket_for_model_resolution(updated_ticket, executor_type)
            effective_model = route_driver_cfg.get("model") or resolve_model(
                effective_cfg, "implement", ticket=model_ticket
            )
            if effective_model is not None:
                try:
                    validate_model_for_executor(
                        str(effective_model),
                        str(executor_type),
                        "implementation routing",
                        provider=executor_cfg.get("provider")
                        or route_driver_cfg.get("provider"),
                    )
                except ConfigError as exc:
                    validation_errors.append(str(exc))
        effective_review_model = updated_ticket.get("review_model_pin")
        if (
            (model is not None or reviewer is not None or executor is not None)
            and effective_review_model
            and not validation_errors
        ):
            # ``resolve_reviewer`` describes the configured/default route,
            # but it is not necessarily who will execute a review.  In a
            # mixed pool the review dispatcher excludes the implementer and
            # can consequently select a different executor family.  Validate
            # against that same candidate so a Claude pin cannot later be
            # handed to Codex (or vice versa).
            if updated_ticket.get("reviewer"):
                resolved_reviewer = updated_ticket["reviewer"]
            else:
                from lanegate.orchestrate.review import (
                    _implementer_identity,
                    resolve_independent_review_driver,
                )

                resolved_reviewer, _ = resolve_independent_review_driver(
                    updated_ticket,
                    cfg,
                    repo_root,
                    implementer=_implementer_identity(updated_ticket),
                )
            # No currently healthy reviewer means there is no dispatch target
            # to validate.  The review path will keep the ticket pending and
            # validates again immediately before any eventual launch.
            if resolved_reviewer is None:
                resolved_reviewer = resolve_reviewer(updated_ticket, cfg)
            assert resolved_reviewer is not None
            drivers = cfg.get("drivers") or {}
            driver_cfg = (
                drivers.get(resolved_reviewer, {})
                if isinstance(drivers, dict)
                else {}
            )
            if isinstance(driver_cfg, dict) and driver_cfg:
                reviewer_type = driver_cfg.get("type", resolved_reviewer)
                reviewer_executor_cfg = get_executor_config(str(reviewer_type), cfg)
            else:
                reviewer_executor_cfg = get_executor_config(resolved_reviewer, cfg)
                reviewer_type = reviewer_executor_cfg.get("type", resolved_reviewer)
            try:
                validate_model_for_executor(
                    str(effective_review_model),
                    str(reviewer_type),
                    "review routing",
                    provider=reviewer_executor_cfg.get("provider")
                    or driver_cfg.get("provider"),
                )
            except ConfigError as exc:
                validation_errors.append(str(exc))
        if validation_errors:
            message = "; ".join(validation_errors)
            _fail_json_or_text(
                json_output,
                f"invalid routing update for {tid}: {message}",
                payload={"error": message},
            )

        from lanegate.ticket import write_ticket
        from lanegate.lifecycle import _commit_generated_ticket_write

        original_ticket_text = ticket["_path"].read_text(encoding="utf-8")
        ticket = updated_ticket
        write_ticket(ticket)
        note = f"route {tid} " + ", ".join(msg_parts)
        try:
            _commit_generated_ticket_write(repo_root, ticket["_path"], tid, note, cfg)
        except SystemExit:
            ticket["_path"].write_text(original_ticket_text, encoding="utf-8")
            raise
        if not json_output:
            print(f"Updated routing for {tid}: " + ", ".join(msg_parts))

    pool_name, reason = resolve_ticket_pool(cfg, ticket)
    route = resolve_executor_route(cfg, ticket)
    implement_executor = route.get("implement")

    # Try to discover Ollama context if this is an Ollama-backed executor
    discovered_context = None
    discovery_source = None
    configured_budget = None
    ollama_mismatch = False
    if implement_executor:
        # First try to resolve as a driver (from drivers: block)
        drivers = cfg.get("drivers") or {}
        driver_cfg = drivers.get(implement_executor) if isinstance(drivers, dict) else None

        # If not found in drivers, resolve as an executor
        if driver_cfg is None:
            executor_cfg = get_executor_config(implement_executor, cfg)
        else:
            executor_cfg = driver_cfg

        executor_type = executor_cfg.get("type")

        is_ollama_aider = executor_type == "aider" and executor_cfg.get("provider") == "ollama"
        if executor_type == "ollama" or is_ollama_aider:
            base_url = executor_cfg.get("base_url")
            if not base_url:
                env = executor_cfg.get("env")
                base_url = env.get("OLLAMA_API_BASE") if isinstance(env, dict) else None
            model = executor_cfg.get("model")
            if model:
                discovered_context, discovery_source = discover_ollama_context(base_url or "http://localhost:11434", model)
                configured_budget = executor_cfg.get("context_window_tokens")
                if discovery_source == "runtime" and discovered_context is not None and configured_budget is not None:
                    ollama_mismatch = discovered_context != configured_budget

    result = {
        "id": ticket["id"],
        "complexity": ticket.get("complexity"),
        "touches_count": len(ticket.get("touches") or []),
        "priority": ticket.get("priority"),
        "routed_pool": pool_name or "unrouted",
        "reason": reason,
        "implement_executor": implement_executor,
    }

    if discovered_context is not None:
        result["discovered_ollama_context"] = discovered_context
        result["ollama_context_source"] = discovery_source
        result["configured_context_window_tokens"] = configured_budget
        result["ollama_mismatch"] = ollama_mismatch

    if json_output:
        print(json.dumps(result, indent=2, default=str))
        return

    print(f"{ticket['id']} — {ticket.get('title', '')}")
    print(
        f"  complexity={result['complexity']}  touches={result['touches_count']}  "
        f"priority={result['priority']}"
    )
    print(f"  routed_pool: {result['routed_pool']}")
    print(f"  reason: {reason}")
    if implement_executor:
        print(f"  implement_executor: {implement_executor}")
    if discovered_context is not None:
        mismatch_flag = " ⚠ MISMATCH" if ollama_mismatch else ""
        if configured_budget is not None:
            print(f"  ollama_context ({discovery_source}): discovered={discovered_context}, configured={configured_budget}{mismatch_flag}")
        else:
            print(f"  ollama_context ({discovery_source}): discovered={discovered_context} (no configured budget)")


def cmd_pipeline_status(cfg: dict, repo_root: Path, json_output: bool = False) -> None:
    from lanegate import APP_NAME

    envs = cfg.get("environments", [])

    if json_output:
        result = []
        for env in envs:
            base = env.get("from", resolve_trunk_branch(cfg, repo_root))
            pending, missing_branch_error = _environment_pending(repo_root, env, base)
            result.append(_pending_payload(env, pending, base, missing_branch_error))
        print(json.dumps(result, indent=2, default=str))
        return

    print("=== Pipeline Status ===\n")

    if not envs:
        print("No environments configured.")
        return

    for env in envs:
        base = env.get("from", resolve_trunk_branch(cfg, repo_root))
        head = env["branch"]
        trigger = env.get("trigger", "manual")
        pending, missing_branch_error = _environment_pending(repo_root, env, base)

        if missing_branch_error:
            print(f"{base} → {head}   {missing_branch_error}")
        elif not pending.ok:
            print(
                f"{base} → {head}   unknown — could not determine pending commits: {pending.error}"
            )
        elif pending.commits:
            action = "(hook-driven)" if trigger == "auto" else f"{APP_NAME} promote {env['name']}"
            print(f"{base} → {head}   {len(pending.commits)} commit(s) waiting  →  {action}")
            for line in pending.commits[:6]:
                print(f"    {line}")
            if len(pending.commits) > 6:
                print(f"    ... and {len(pending.commits) - 6} more")
        else:
            print(f"{base} → {head}   up to date")
        print()


def _attention_ticket(ticket: dict, cfg: dict | None) -> dict:
    """Add resolved autonomy for the ticket-only attention predicate."""
    if cfg is None:
        return ticket
    return {**ticket, "_effective_autonomy": resolve_autonomy(cfg, ticket)}


def _needs_attention_entry(ticket: dict, trunk_branch: str) -> dict:
    """Build the shared JSON row used by the CLI and API attention queue."""
    bid = ticket["id"]
    branch = ticket.get("branch") or branch_name(bid)
    return {
        "id": bid,
        "title": ticket.get("title", ""),
        "branch": branch,
        "diff_cmd": f"git diff {trunk_branch}...{branch}",
        "findings": ticket.get("review_findings") or [],
        "attention_summary": attention_summary(ticket),
        "attention_category": attention_category(ticket),
        "priority": ticket.get("priority"),
        "milestone": ticket.get("milestone"),
    }


def _needs_attention_tickets(tickets: list[dict], cfg: dict) -> list[dict]:
    return [
        attention_ticket
        for ticket in tickets
        if needs_attention(attention_ticket := _attention_ticket(ticket, cfg))
    ]


def get_blocked_queue(cfg: dict, repo_root: Path) -> dict:
    """Return the needs-human-decision queue as JSON-serializable data."""
    tickets_dir = repo_root / cfg["tickets_dir"]
    all_tickets, _ = _load_board_tickets(cfg, tickets_dir)

    trunk_branch = resolve_trunk_branch(cfg, repo_root)
    return {"blocked": [_needs_attention_entry(t, trunk_branch) for t in _needs_attention_tickets(all_tickets, cfg)]}


def cmd_blocked(cfg: dict, repo_root: Path, json_output: bool = False) -> None:
    """List tickets awaiting a human decision or intervention."""
    tickets_dir = repo_root / cfg["tickets_dir"]
    all_tickets, _ = _load_board_tickets(cfg, tickets_dir)

    blocked = _needs_attention_tickets(all_tickets, cfg)

    trunk_branch = resolve_trunk_branch(cfg, repo_root)
    if json_output:
        print(json.dumps([_needs_attention_entry(t, trunk_branch) for t in blocked], indent=2, default=str))
        return

    if not blocked:
        print("No tickets need human attention.")
        return

    labels = {
        "escalated": "Escalated",
        "rejected": "Changes requested",
        "failed": "Failed",
        "stuck": "Stuck",
        "awaiting_merge": "Awaiting merge",
        "merged_diagnostic": "Post-merge verification diagnostic",
    }
    print("Needs-human-decision tickets:\n")
    for category in labels:
        grouped = [ticket for ticket in blocked if attention_category(ticket) == category]
        if not grouped:
            continue
        print(f"{labels[category]}:")
        for t in grouped:
            bid = t["id"]
            title = t.get("title", "")
            branch = t.get("branch") or branch_name(bid)
            diff_cmd = f"git diff {trunk_branch}...{branch}"
            findings = t.get("review_findings") or []
            reason = attention_summary(t)
            priority = t.get("priority")
            milestone = t.get("milestone")

            # Construct the header line
            header = f"{bid} — {title}"
            if milestone:
                if priority is not None:
                    header += f" ({milestone}, priority {priority})"
                else:
                    header += f" ({milestone})"
            elif priority is not None:
                header += f" (priority {priority})"

            print(header)
            print(f"  Branch: {branch}   {diff_cmd}")
            if reason:
                print(f"  Reason: {reason}")

            if findings:
                print("  Findings:")
                for idx, finding in enumerate(findings, 1):
                    print(f"    [{idx}] {finding}")
            print()
        print()


def cmd_summary(ticket_id: str, cfg: dict, repo_root: Path, json_output: bool = False) -> None:
    """Print the consolidated why-is-this-stuck view for a single ticket."""
    data = get_ticket_summary(ticket_id, cfg, repo_root)

    if data.get("error"):
        _fail_json_or_text(json_output, data["error"], payload=data)

    if json_output:
        print(json.dumps(data, indent=2, default=str))
        return

    header = f"{data['id']} — {data.get('title', '')}"
    meta = [f"status: {data.get('status')}"]
    if data.get("needs_review_cause"):
        meta.append(f"cause: {data['needs_review_cause']}")
    if data.get("milestone"):
        meta.append(f"milestone: {data['milestone']}")
    if data.get("priority") is not None:
        meta.append(f"priority: {data['priority']}")
    print(header)
    print("  " + "   ".join(meta))
    print()

    if data.get("reason"):
        print("Reason:")
        for line in data["reason"].splitlines():
            print(f"  {line}")
        print()

    if data.get("review_verdict"):
        print(f"Review verdict: {data['review_verdict']}")
    if data.get("review_summary"):
        print(f"Review summary: {data['review_summary']}")
    findings = data.get("review_findings") or []
    if findings:
        print("Review findings:")
        for idx, finding in enumerate(findings, 1):
            print(f"  [{idx}] {finding}")
    if data.get("review_verdict") or data.get("review_summary") or findings:
        print()

    if data.get("diff_error"):
        print(f"Diff: {data['diff_error']}")
    elif data.get("diff_stat"):
        print("Diff:")
        print(data["diff_stat"])
    print()

    if data.get("next_step"):
        print(f"Next: {data['next_step']}")
