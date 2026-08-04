"""
cli.py — argparse wiring and main() entry point.

All user-facing strings use APP_NAME so the brand is a single knob.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lanegate import APP_NAME, __version__
from lanegate.config import find_repo_root, load_config


def _get_cfg_and_root() -> tuple[dict, Path]:
    repo_root = find_repo_root()
    cfg = load_config(repo_root)
    return cfg, repo_root


def _force_utf8_output() -> None:
    """Make stdout/stderr UTF-8 so status glyphs never crash the CLI.

    The board, doctor, and most commands print Unicode glyphs (✓ ✗ → –). On
    Windows the console and redirected pipes default to a legacy code page
    (e.g. cp1252) that cannot encode them, so a bare ``print`` raises
    UnicodeEncodeError and takes the whole command down. Reconfiguring to UTF-8
    with errors="replace" guarantees output always succeeds; it is a harmless
    no-op where stdout is already UTF-8 (typical on Linux/macOS).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def main() -> None:
    _force_utf8_output()
    p = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Git-native agentic delivery: queue → parallel agents → quality gates → staged deploy",
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"{APP_NAME} {__version__}",
    )
    p.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=False,
        help="Emit structured JSON instead of human-readable text (board, next, pipeline-status, flag list)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    board_p = sub.add_parser("board", help="Ticket board grouped by status + environment pipeline")
    board_p.add_argument(
        "--all",
        dest="show_all",
        action="store_true",
        help="Include closed tickets (merged/done/validated)",
    )
    board_p.add_argument(
        "--global",
        dest="global_",
        action="store_true",
        default=False,
        help="Aggregate open tickets from all registered projects",
    )
    board_p.add_argument(
        "--quarantine",
        dest="show_quarantine",
        action="store_true",
        default=False,
        help="Show tickets that failed schema validation on load",
    )
    board_p.add_argument(
        "--milestone",
        dest="milestone",
        default=None,
        metavar="M",
        help="Filter board to tickets with this milestone tag",
    )
    board_p.add_argument(
        "--all-milestones",
        dest="all_milestones",
        action="store_true",
        default=False,
        help="Show tickets across all milestones (overrides default_milestone in .lanegate.yml)",
    )
    next_p = sub.add_parser(
        "next", help="Recommend next unblocked ticket(s); shows parallel-safe batch"
    )
    next_p.add_argument(
        "--milestone",
        dest="milestone",
        default=None,
        metavar="M",
        help="Restrict candidates to tickets with this milestone tag",
    )
    next_p.add_argument(
        "--in-flight",
        dest="in_flight",
        default=None,
        metavar="IDs",
        help="Comma-separated ticket IDs the orchestrator has already started; their touches are treated as locked when computing the next candidate",
    )
    sub.add_parser("pipeline-status", help="Commits pending at each pipeline stage")
    sub.add_parser("blocked", help="List code_complete tickets with changes_requested review verdict")

    route_p = sub.add_parser(
        "route", help="Dry-run: show which executor pool a ticket would route to, and why"
    )
    route_p.add_argument("ticket_id")

    for name in ("start", "merge", "validate", "done"):
        sp = sub.add_parser(name)
        sp.add_argument("ticket_id")

    hibernate_p = sub.add_parser("hibernate", help="Hibernate interrupted in-progress work")
    hibernate_p.add_argument("ticket_id", nargs="?")
    hibernate_p.add_argument(
        "--reset",
        dest="reset",
        action="store_true",
        default=False,
        help="Also delete the ticket worktree and branch after writing recovery notes",
    )
    hibernate_p.add_argument(
        "--orphaned",
        dest="orphaned",
        action="store_true",
        default=False,
        help="Hibernate all orphaned in-progress tickets",
    )
    hibernate_p.add_argument("--reason", default="", help="Reason recorded in ticket notes")

    stop_p = sub.add_parser(
        "stop", help="Stop a ticket's live executor: SIGTERM the durable PID, wait, hibernate"
    )
    stop_p.add_argument("ticket_id")
    stop_p.add_argument("--reason", default="", help="Reason recorded in ticket notes")
    stop_p.add_argument(
        "--grace-seconds",
        type=float,
        default=5.0,
        dest="grace_seconds",
        help="Seconds to wait for the executor to exit after SIGTERM before hibernating anyway",
    )

    needs_review_p = sub.add_parser(
        "needs-review", help="Preserve worktree and mark ticket for human conflict review"
    )
    needs_review_p.add_argument("ticket_id")
    needs_review_p.add_argument("--reason", default="", help="Reason recorded in the ticket body")

    reopen_p = sub.add_parser("reopen", help="Reset a failed ticket back to open for re-dispatch")
    reopen_p.add_argument("ticket_id")

    supersede_p = sub.add_parser(
        "supersede",
        help="Close a ticket whose work already exists elsewhere (TICK-284 reconciliation)",
    )
    supersede_p.add_argument("ticket_id")
    supersede_p.add_argument(
        "--reason",
        default="",
        help="Human justification for retiring an obsolete non-terminal ticket",
    )

    open_p = sub.add_parser(
        "open",
        help="Flip a draft ticket to open without re-running analysis (requires touches to be set)",
    )
    open_p.add_argument("ticket_id")

    # complete — separate so we can add --allow-drift and --auto-update-touches
    complete_p = sub.add_parser(
        "complete", help="Mark a ticket code_complete (blocks on undeclared file drift)"
    )
    complete_p.add_argument("ticket_id")
    complete_p.add_argument(
        "--allow-drift",
        dest="allow_drift",
        action="store_true",
        default=False,
        help="Warn instead of blocking when the diff contains files not declared in touches",
    )
    complete_p.add_argument(
        "--auto-update-touches",
        dest="auto_update_touches",
        action="store_true",
        default=False,
        help="Auto-add undeclared committed files to ticket touches and proceed",
    )

    # review — split out to support verdict/summary/findings flags
    review_p = sub.add_parser("review", help="Submit ticket for review (→ in_review)")
    review_p.add_argument("ticket_id")
    review_p.add_argument(
        "--verdict",
        choices=["approved", "changes_requested"],
        default=None,
        help="Review verdict from an agent or human reviewer",
    )
    review_p.add_argument(
        "--summary",
        default=None,
        metavar="TEXT",
        help="One-line review summary stored in review_summary frontmatter field",
    )
    review_p.add_argument(
        "--findings",
        default=None,
        metavar="TEXT",
        help="Multi-line findings appended to ticket body under ## Review Findings",
    )

    # fix
    fix_p = sub.add_parser(
        "fix", help="Run fix -> drift-check -> re-review for a changes_requested ticket"
    )
    fix_p.add_argument("ticket_id")

    # promote
    promote_p = sub.add_parser(
        "promote", help="Promote a manual environment (guard → pre → sync → post)"
    )
    promote_p.add_argument("env_name", metavar="ENV")

    # flag
    flag_p = sub.add_parser("flag", help="Manage per-environment feature flags")
    flag_p.add_argument("--env", metavar="ENV", default=None, help="Target environment")
    flag_sub = flag_p.add_subparsers(dest="flag_cmd", required=True)
    flag_sub.add_parser("list", help="Show all feature flags and their state")
    for _action in ("enable", "disable"):
        _sp = flag_sub.add_parser(_action)
        _sp.add_argument("name", help="Flag name (e.g. new_checkout_flow)")

    # create
    create_p = sub.add_parser(
        "create", help="Capture intent as a draft ticket (and analyze by default)"
    )
    create_p.add_argument("intent", help="Natural-language description of what needs to be built")
    create_p.add_argument(
        "--no-analyze",
        dest="no_analyze",
        action="store_true",
        default=False,
        help="Stop at draft — skip the analyze step",
    )
    create_p.add_argument(
        "--milestone",
        dest="milestone",
        default=None,
        metavar="M",
        help="Milestone tag for this ticket (overrides interactive prompt and config default)",
    )

    # analyze
    analyze_p = sub.add_parser(
        "analyze", help="Analyze a draft ticket: populate touches + close_criteria, flip to open"
    )
    analyze_p.add_argument("ticket_id")

    # claim-file
    claim_file_p = sub.add_parser("claim-file", help="Add a file to a ticket's touches list")
    claim_file_p.add_argument("file", metavar="<file>", help="File path to claim")
    claim_file_p.add_argument("ticket", metavar="<ticket>", help="Ticket ID to extend")

    # gh-sync
    gh_sync_p = sub.add_parser("gh-sync", help="Mirror tickets to GitHub Issues (exact-match dedup)")
    gh_sync_p.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=False,
        help="Print planned actions without creating or updating issues",
    )

    # projects
    projects_p = sub.add_parser("projects", help="Manage the global project registry")
    projects_sub = projects_p.add_subparsers(dest="projects_cmd", required=True)
    scan_p = projects_sub.add_parser(
        "scan", help="Scan directories one level deep and register projects"
    )
    scan_p.add_argument("dirs", nargs="+", metavar="<dir>", help="Directories to scan")
    projects_sub.add_parser("list", help="Show all registered projects")
    remove_p = projects_sub.add_parser("remove", help="Deregister a project by path")
    remove_p.add_argument("path", help="Absolute path to the project to remove")

    # prompts
    prompts_p = sub.add_parser("prompts", help="Manage prompt templates")
    prompts_sub = prompts_p.add_subparsers(dest="prompts_cmd", required=True)
    eject_p = prompts_sub.add_parser(
        "eject",
        help="Copy built-in prompt templates to <project>/prompts/ for customization",
    )
    eject_p.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Overwrite existing files (default: skip files that already exist)",
    )

    # init
    init_p = sub.add_parser("init", help=f"Scaffold tickets/ + {APP_NAME}.yml in the current repo")
    init_p.add_argument(
        "--defaults",
        dest="use_defaults",
        action="store_true",
        default=False,
        help="Skip all prompts and write a minimal config non-interactively",
    )
    init_p.add_argument(
        "-i",
        "--interactive",
        dest="force_interactive",
        action="store_true",
        default=False,
        help="Force interactive prompts even when stdin is not a TTY",
    )

    # doctor
    sub.add_parser("doctor", help="check optional dependencies and report install instructions")

    # agent-native tool installers
    install_agent_p = sub.add_parser(
        "install-agent-tools",
        help="Install Claude slash commands and MCP config snippets for Codex/other agents",
    )
    install_agent_p.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=False,
        help="Emit structured install result",
    )
    sub.add_parser(
        "install-commands",
        help="Copy Claude slash commands into .claude/commands/ (compatibility alias)",
    )

    # update-docs — refresh README/ARCHITECTURE.md from completed tickets
    update_docs_p = sub.add_parser(
        "update-docs",
        help="Refresh README/ARCHITECTURE.md based on tickets completed since last doc update",
    )
    update_docs_p.add_argument(
        "--status",
        dest="status",
        default=None,
        metavar="STATUS",
        help="Ticket status filter (default: 'done' or as configured in .lanegate.yml)",
    )

    # stats — time-in-status summary
    stats_p = sub.add_parser("stats", help="Report median time spent in each ticket status")
    stats_p.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=False,
        help="Emit structured JSON",
    )

    # analytics (primary) + context-stats (hidden back-compat alias)
    for _cmd_name, _help in [
        ("analytics", "Show per-ticket agent delegation analytics from the context log"),
        ("context-stats", argparse.SUPPRESS),
    ]:
        _ap = sub.add_parser(_cmd_name, help=_help)
        _ap.add_argument(
            "--log",
            dest="log_paths",
            metavar="PATH",
            action="append",
            default=None,
            help="Path to context log JSONL (repeatable; overrides SQLite default)",
        )
        _ap.add_argument(
            "--all-projects",
            dest="all_projects",
            action="store_true",
            default=False,
            help="Show analytics across all projects (no project filter)",
        )
        _ap.add_argument(
            "--full",
            dest="full",
            action="store_true",
            default=False,
            help="Show extended panels: compression detail, parallelism gain, cost trend, quality",
        )
        _ap.add_argument(
            "--compare",
            dest="compare",
            action="store_true",
            default=False,
            help="Group entries by executor+model and show side-by-side comparison",
        )
        _ap.add_argument(
            "--json",
            dest="json_output",
            action="store_true",
            default=False,
            help="Emit structured JSON",
        )

    # log — backfill analytics for a ticket
    log_p = sub.add_parser(
        "log", help="Backfill or update analytics for a ticket in the central DB"
    )
    log_p.add_argument("ticket_id")
    log_p.add_argument(
        "--tokens", type=int, default=None, dest="subagent_tokens", help="Subagent work tokens"
    )
    log_p.add_argument(
        "--summary-tokens",
        type=int,
        default=None,
        dest="summary_tokens",
        help="Summary tokens that entered main context",
    )
    log_p.add_argument("--executor", default=None, help="Executor type (e.g. claude-subagent)")
    log_p.add_argument("--model", default=None, help="Model ID (e.g. claude-sonnet-4-6)")
    log_p.add_argument(
        "--tests-passed",
        dest="tests_passed",
        action="store_true",
        default=None,
        help="Mark this ticket's tests as passing",
    )

    # logs — colorized tail for orchestrator/executor logs
    logs_p = sub.add_parser("logs", help="Show the latest LaneGate log with optional colorized follow")
    logs_p.add_argument(
        "--path",
        dest="path",
        default=None,
        help="Specific log file to render (defaults to latest .lanegate/logs/*.log)",
    )
    logs_p.add_argument(
        "-n",
        "--lines",
        dest="lines",
        type=int,
        default=80,
        help="Number of trailing lines to show before following",
    )
    logs_p.add_argument(
        "-f",
        "--follow",
        dest="follow",
        action="store_true",
        default=False,
        help="Continue streaming appended log lines",
    )
    logs_p.add_argument(
        "--color",
        dest="color",
        choices=["auto", "always", "never"],
        default="auto",
        help="Color output policy (default: auto)",
    )
    logs_p.add_argument(
        "--open-with",
        dest="open_with",
        choices=["lnav", "multitail", "colortail"],
        default=None,
        help="Open the selected log with an installed external viewer",
    )

    # session-summary — record main-session orchestrator cost
    sess_p = sub.add_parser(
        "session-summary",
        help="Record the main-session orchestrator token cost for a completed run",
    )
    sess_p.add_argument(
        "--session-tokens",
        type=int,
        required=True,
        dest="session_tokens",
        help="Token count consumed by the orchestrator's main session turns",
    )
    sess_p.add_argument(
        "--tickets",
        default="",
        dest="tickets",
        help="Comma-separated ticket IDs merged this run (e.g. TICK-016,TICK-017)",
    )

    # watch
    watch_p = sub.add_parser(
        "watch",
        help="Background daemon: poll PR review decisions and auto-merge on approval",
    )
    watch_p.add_argument(
        "--status",
        dest="watch_status",
        action="store_true",
        default=False,
        help="Print whether a watcher is running and its PID",
    )
    watch_p.add_argument(
        "--stop",
        dest="watch_stop",
        action="store_true",
        default=False,
        help="Kill a running watcher and remove the PID file",
    )

    # resume-watch
    resume_watch_p = sub.add_parser(
        "resume-watch",
        help="Background daemon: wait out a rate limit and auto-resume `lanegate orchestrate`",
    )
    resume_watch_p.add_argument(
        "--status",
        dest="resume_watch_status",
        action="store_true",
        default=False,
        help="Print whether a resume watcher is running and its PID",
    )
    resume_watch_p.add_argument(
        "--stop",
        dest="resume_watch_stop",
        action="store_true",
        default=False,
        help="Kill a running resume watcher and remove the PID file",
    )
    resume_watch_p.add_argument(
        "--history",
        dest="resume_watch_history",
        action="store_true",
        default=False,
        help="Print recent resume attempts and outcomes (hibernated/retrying/resumed/gave_up)",
    )

    # notify-watch
    notify_watch_p = sub.add_parser(
        "notify-watch",
        help="Background daemon: push a phone notification when orchestrate looks stuck",
    )
    notify_watch_p.add_argument(
        "--status",
        dest="notify_watch_status",
        action="store_true",
        default=False,
        help="Print whether a notify watcher is running and its PID",
    )
    notify_watch_p.add_argument(
        "--stop",
        dest="notify_watch_stop",
        action="store_true",
        default=False,
        help="Kill a running notify watcher and remove the PID file",
    )
    notify_watch_p.add_argument(
        "--test",
        dest="notify_watch_test",
        action="store_true",
        default=False,
        help="Send one test push via the configured notify.ntfy_topic and exit",
    )

    # orchestrate
    orch_p = sub.add_parser(
        "orchestrate",
        help="Clear the ticket board using the configured executor",
    )
    orch_p.add_argument(
        "--max",
        dest="max_parallel",
        type=int,
        default=None,
        metavar="N",
        help="Cap on parallel tickets (overrides max_parallel in .lanegate.yml)",
    )
    orch_p.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=False,
        help="Print planned actions without executing",
    )
    orch_p.add_argument(
        "--human-review",
        dest="human_review",
        default=None,
        choices=["per_ticket", "final", "none"],
        help=(
            "Human review gate: per_ticket, final, or none. "
            "Defaults to default_human_review in .lanegate.yml, or 'none' if unset."
        ),
    )
    orch_p.add_argument(
        "--milestone",
        dest="milestone",
        default=None,
        metavar="M",
        help="Process only tickets with this milestone tag (overrides default_milestone in .lanegate.yml)",
    )
    orch_p.add_argument(
        "--all",
        dest="all_milestones",
        action="store_true",
        default=False,
        help="Clear tickets across all milestones regardless of default_milestone setting",
    )
    orch_p.add_argument(
        "--tickets",
        dest="tickets",
        default=None,
        metavar="ID,ID,...",
        help="Restrict dispatch to exactly these ticket IDs (comma-separated); "
        "composes with --milestone (both filters apply) rather than replacing it",
    )
    orch_p.add_argument(
        "--no-auto-analyze",
        dest="no_auto_analyze",
        action="store_true",
        default=False,
        help="Skip auto-analyzing draft tickets at the top of each run loop iteration",
    )
    orch_p.add_argument(
        "--no-recover",
        dest="no_recover",
        action="store_true",
        default=False,
        help="Skip startup recovery of orphaned in-progress tickets",
    )
    orch_p.add_argument(
        "-v",
        "--verbose",
        dest="verbose",
        action="store_true",
        default=False,
        help="Stream full executor output to terminal (default: compact progress lines only)",
    )
    orch_p.add_argument(
        "--status",
        dest="status",
        action="store_true",
        default=False,
        help="Report active orchestration status (ticket, executor PID, elapsed time, log path)",
    )
    orch_p.add_argument(
        "--pool",
        dest="pool",
        default=None,
        metavar="NAME",
        help=(
            "Draw executor instances from this pools: entry in .lanegate.yml "
            "(falls back to default_pool, or plain single-executor dispatch if unset)"
        ),
    )

    # run-report
    run_report_p = sub.add_parser(
        "run-report",
        help="Structured summary of an orchestrate run: per-ticket outcomes, executor "
        "swaps/cooldowns, rate-limit hibernations, and orphaned processes",
    )
    run_report_p.add_argument(
        "--session",
        dest="session_ts",
        default=None,
        metavar="TS",
        help="Report on a specific run session (timestamp segment of orchestrate-<TS>.log); "
        "defaults to the most recent run",
    )
    run_report_p.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=False,
        help="Print the report as JSON",
    )

    # ps
    ps_p = sub.add_parser(
        "ps",
        help="List every live lanegate-spawned process (orchestrator, daemons, ticket "
        "executors), flagging any that are orphaned",
    )
    ps_p.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=False,
        help="Print the process list as JSON",
    )

    # mcp
    sub.add_parser("mcp", help="Start MCP server exposing lanegate verbs as tools (stdio transport)")

    # tui
    tui_p = sub.add_parser("tui", help="Start the read-only Go TUI runtime spike")
    tui_input = tui_p.add_mutually_exclusive_group()
    tui_input.add_argument(
        "--fixture",
        dest="fixture",
        default=None,
        help="Read a board JSON fixture instead of starting or connecting to the API",
    )
    tui_input.add_argument(
        "--fixture-dir",
        dest="fixture_dir",
        default=None,
        help="Read board.json or board/board_basic.json from a fixture directory",
    )
    tui_p.add_argument(
        "--api-url",
        dest="api_url",
        default=None,
        help="Connect to an existing loopback API instead of starting one",
    )
    tui_p.add_argument(
        "--no-api-start",
        dest="no_api_start",
        action="store_true",
        default=False,
        help="Fail unless --api-url or a fixture source is provided",
    )
    tui_p.add_argument(
        "--port",
        dest="port",
        type=int,
        default=None,
        help="Preferred local API port when Python starts the API",
    )

    # api
    api_p = sub.add_parser(
        "api",
        help="Start a loopback-only HTTP API server (127.0.0.1 only) for the board, tickets, diff, and orchestration endpoints",
    )
    api_p.add_argument(
        "--port",
        dest="port",
        type=int,
        default=8000,
        help="Port to listen on (default: 8000; always binds to 127.0.0.1 only)",
    )

    # orchestrator-lock
    orch_p = sub.add_parser(
        "orchestrator-lock",
        help="Single-orchestrator advisory lock (acquire/release/status) — only one driver per repo",
    )
    orch_sub = orch_p.add_subparsers(dest="orch_cmd", required=True)
    for _action in ("acquire", "release"):
        _osp = orch_sub.add_parser(_action)
        _osp.add_argument(
            "--pid",
            type=int,
            default=None,
            help="PID of the orchestrator session (default: this process; pass $$ from a shell)",
        )
        _osp.add_argument(
            "--force",
            action="store_true",
            default=False,
            help="Override a wedged lock held by another live process",
        )
    orch_sub.add_parser("status", help="Report lock state without acquiring (read-only attach)")

    # executor (TICK-090): quota-aware cooldown status/reset
    executor_p = sub.add_parser(
        "executor",
        help="Inspect and manage executor instance cooldown state (rate-limit/quota failover)",
    )
    executor_sub = executor_p.add_subparsers(dest="executor_cmd", required=True)
    executor_sub.add_parser(
        "status", help="List named executor instances with running count + cooldown state"
    )
    executor_reset_p = executor_sub.add_parser(
        "reset", help="Clear cooldown state for one executor instance or all of them"
    )
    executor_reset_group = executor_reset_p.add_mutually_exclusive_group(required=True)
    executor_reset_group.add_argument(
        "name", nargs="?", default=None, help="Executor instance name to clear cooldown for"
    )
    executor_reset_group.add_argument(
        "--all",
        dest="reset_all",
        action="store_true",
        default=False,
        help="Clear cooldown for every executor instance",
    )

    args = p.parse_args()

    if args.cmd == "board":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.board import cmd_board

        cmd_board(
            cfg,
            repo_root,
            json_output=args.json_output,
            show_all=args.show_all,
            global_view=args.global_,
            show_quarantine=args.show_quarantine,
            milestone=args.milestone,
            all_milestones=args.all_milestones,
        )

    elif args.cmd == "next":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.board import cmd_next

        in_flight_ids = (
            [t.strip() for t in args.in_flight.split(",") if t.strip()] if args.in_flight else None
        )
        cmd_next(
            cfg,
            repo_root,
            json_output=args.json_output,
            milestone=args.milestone,
            in_flight=in_flight_ids,
        )

    elif args.cmd == "pipeline-status":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.board import cmd_pipeline_status

        cmd_pipeline_status(cfg, repo_root, json_output=args.json_output)

    elif args.cmd == "blocked":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.board import cmd_blocked

        cmd_blocked(cfg, repo_root, json_output=args.json_output)

    elif args.cmd == "route":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.board import cmd_route

        cmd_route(cfg, repo_root, args.ticket_id, json_output=args.json_output)

    elif args.cmd == "start":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.lifecycle import cmd_start

        cmd_start(args.ticket_id, cfg, repo_root)

    elif args.cmd == "complete":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.lifecycle import cmd_complete

        cmd_complete(
            args.ticket_id,
            cfg,
            repo_root,
            allow_drift=args.allow_drift,
            auto_update_touches=args.auto_update_touches,
        )

    elif args.cmd == "hibernate":
        cfg, repo_root = _get_cfg_and_root()
        if args.orphaned:
            from lanegate.orchestrate import _hibernate_orphaned

            _hibernate_orphaned(cfg, repo_root)
            return
        if not args.ticket_id:
            print(
                "ERROR: hibernate requires a ticket_id unless --orphaned is used", file=sys.stderr
            )
            sys.exit(2)
        from lanegate.lifecycle import cmd_hibernate

        cmd_hibernate(args.ticket_id, cfg, repo_root, reset=args.reset, reason=args.reason)

    elif args.cmd == "stop":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.lifecycle import cmd_stop

        result = cmd_stop(
            args.ticket_id,
            cfg,
            repo_root,
            reason=args.reason,
            grace_seconds=args.grace_seconds,
        )
        if args.json_output:
            import json

            print(json.dumps(result))

    elif args.cmd == "needs-review":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.lifecycle import cmd_needs_review

        cmd_needs_review(args.ticket_id, cfg, repo_root, reason=args.reason)

    elif args.cmd == "reopen":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.lifecycle import cmd_reopen

        cmd_reopen(args.ticket_id, cfg, repo_root)

    elif args.cmd == "supersede":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.lifecycle import cmd_supersede

        cmd_supersede(args.ticket_id, cfg, repo_root, reason=args.reason)

    elif args.cmd == "open":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.lifecycle import cmd_open

        cmd_open(args.ticket_id, cfg, repo_root)

    elif args.cmd == "review":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.lifecycle import cmd_review

        cmd_review(
            args.ticket_id,
            cfg,
            repo_root,
            verdict=args.verdict,
            summary=args.summary,
            findings=args.findings,
        )

    elif args.cmd == "fix":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.orchestrate.autofix import cmd_fix

        cmd_fix(args.ticket_id, cfg, repo_root)

    elif args.cmd == "merge":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.lifecycle import MergeFailedError, cmd_merge

        try:
            cmd_merge(args.ticket_id, cfg, repo_root)
        except MergeFailedError:
            sys.exit(1)

    elif args.cmd == "validate":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.lifecycle import cmd_validate

        cmd_validate(args.ticket_id, cfg, repo_root)

    elif args.cmd == "done":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.lifecycle import cmd_done

        cmd_done(args.ticket_id, cfg, repo_root)

    elif args.cmd == "promote":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.promote import cmd_promote

        cmd_promote(args.env_name, cfg, repo_root)

    elif args.cmd == "flag":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.flags import cmd_flag

        flag_name = getattr(args, "name", None)
        cmd_flag(args.flag_cmd, flag_name, cfg, args.env, json_output=args.json_output)

    elif args.cmd == "create":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.create import cmd_create

        ticket_id = cmd_create(args.intent, cfg, repo_root, milestone=args.milestone)
        if not args.no_analyze:
            from lanegate.analyze import cmd_analyze

            try:
                cmd_analyze(ticket_id, cfg, repo_root, keep_draft=True)
            except Exception as exc:
                print(
                    f"  Ticket {ticket_id} created (draft). "
                    f"Run `lanegate analyze {ticket_id}` when ready.",
                    file=sys.stderr,
                )
                print(f"  (analyze skipped: {exc})", file=sys.stderr)

    elif args.cmd == "analyze":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.analyze import cmd_analyze

        cmd_analyze(args.ticket_id, cfg, repo_root)

    elif args.cmd == "claim-file":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.claim_file import cmd_claim_file

        cmd_claim_file(args.file, args.ticket, cfg, repo_root)

    elif args.cmd == "gh-sync":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.ghsync import cmd_gh_sync

        cmd_gh_sync(cfg, repo_root, dry_run=args.dry_run)

    elif args.cmd == "projects":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.projects import cmd_projects_list, cmd_projects_remove, cmd_projects_scan

        if args.projects_cmd == "scan":
            cmd_projects_scan(cfg, repo_root, args.dirs)
        elif args.projects_cmd == "list":
            cmd_projects_list(cfg, repo_root)
        elif args.projects_cmd == "remove":
            cmd_projects_remove(cfg, repo_root, args.path)

    elif args.cmd == "prompts":
        cfg, repo_root = _get_cfg_and_root()
        if args.prompts_cmd == "eject":
            _cmd_prompts_eject(repo_root, force=args.force)

    elif args.cmd == "init":
        _cmd_init(use_defaults=args.use_defaults, force_interactive=args.force_interactive)

    elif args.cmd == "doctor":
        from lanegate.doctor import cmd_doctor

        sys.exit(cmd_doctor())

    elif args.cmd == "install-agent-tools":
        _cmd_install_agent_tools(json_output=args.json_output)

    elif args.cmd == "install-commands":
        _cmd_install_commands()

    elif args.cmd == "stats":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.stats import cmd_stats

        cmd_stats(cfg, repo_root, json_output=getattr(args, "json_output", False))

    elif args.cmd == "update-docs":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.update_docs import cmd_update_docs

        cmd_update_docs(cfg, repo_root, status=args.status)

    elif args.cmd in ("analytics", "context-stats"):
        import json as _json

        from lanegate.context_log import (
            _print_all_projects_table,
            _print_basic_table,
            _print_compare,
            _print_full_panels,
            load_entries_for_analytics,
            stats_json,
        )

        _, repo_root = _get_cfg_and_root()
        log_paths = [Path(p) for p in args.log_paths] if args.log_paths else None
        all_projects = getattr(args, "all_projects", False)
        entries, show_project = load_entries_for_analytics(
            repo_root,
            jsonl_paths=log_paths,
            all_projects=all_projects,
        )

        if getattr(args, "json_output", False):
            if not entries:
                print(_json.dumps({"has_entries": False}))
            else:
                from lanegate.context_log import (
                    _get_default_db_path,
                    _get_project_id,
                    _load_sessions_from_db,
                    _load_step_costs_from_db,
                )

                _db = _get_default_db_path()
                _proj = _get_project_id(repo_root) if repo_root else None
                _sessions = _load_sessions_from_db(_db, _proj)
                _step_costs = _load_step_costs_from_db(_db, _proj)
                print(
                    stats_json(
                        entries, sessions=_sessions or None, step_costs=_step_costs or None
                    )
                )
            return

        if not entries:
            print("No context log entries yet.")
            return

        if args.compare:
            _print_compare(entries)
            return

        if show_project:
            _print_all_projects_table(entries)
        else:
            _print_basic_table(entries)
        if args.full:
            from lanegate.context_log import (
                _get_default_db_path,
                _get_project_id,
                _load_sessions_from_db,
                _load_step_costs_from_db,
            )

            db_path = _get_default_db_path()
            project = _get_project_id(repo_root) if repo_root else None
            sessions = _load_sessions_from_db(db_path, project)
            step_costs = _load_step_costs_from_db(db_path, project)
            _print_full_panels(entries, sessions=sessions, step_costs=step_costs)

    elif args.cmd == "session-summary":
        _, repo_root = _get_cfg_and_root()
        from lanegate.context_log import cmd_session_summary

        tickets = [t.strip() for t in args.tickets.split(",") if t.strip()]
        cmd_session_summary(repo_root, args.session_tokens, tickets)

    elif args.cmd == "log":
        _, repo_root = _get_cfg_and_root()
        from lanegate.context_log import cmd_log_backfill

        tests_passed = True if args.tests_passed else None
        cmd_log_backfill(
            args.ticket_id,
            repo_root,
            subagent_tokens=args.subagent_tokens,
            summary_tokens=args.summary_tokens,
            executor=args.executor,
            model=args.model,
            tests_passed=tests_passed,
        )

    elif args.cmd == "logs":
        _, repo_root = _get_cfg_and_root()
        from lanegate.logs import cmd_logs

        cmd_logs(
            repo_root,
            path=Path(args.path) if args.path else None,
            lines=args.lines,
            follow=args.follow,
            color=args.color,
            open_with=args.open_with,
        )

    elif args.cmd == "watch":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.watch import cmd_watch

        cmd_watch(cfg, repo_root, status=args.watch_status, stop=args.watch_stop)

    elif args.cmd == "resume-watch":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.resume_watch import cmd_resume_watch

        cmd_resume_watch(
            cfg,
            repo_root,
            status=args.resume_watch_status,
            stop=args.resume_watch_stop,
            history=args.resume_watch_history,
        )

    elif args.cmd == "notify-watch":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.notify_watch import cmd_notify_watch

        cmd_notify_watch(
            cfg,
            repo_root,
            status=args.notify_watch_status,
            stop=args.notify_watch_stop,
            test=args.notify_watch_test,
        )

    elif args.cmd == "orchestrate":
        cfg, repo_root = _get_cfg_and_root()
        if args.status:
            _cmd_orchestrate_status(repo_root, args)
        else:
            from lanegate.orchestrate import cmd_orchestrate

            cmd_orchestrate(
                cfg,
                repo_root,
                max_parallel=args.max_parallel,
                dry_run=args.dry_run,
                human_review=args.human_review,
                milestone=args.milestone,
                all_milestones=args.all_milestones,
                tickets=[t.strip() for t in args.tickets.split(",") if t.strip()]
                if args.tickets
                else None,
                auto_analyze=not args.no_auto_analyze,
                recover=not args.no_recover,
                verbose=args.verbose,
                pool=args.pool,
            )

    elif args.cmd == "run-report":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.orchestrate import cmd_run_report

        cmd_run_report(
            cfg, repo_root, session_ts=args.session_ts, json_output=args.json_output
        )

    elif args.cmd == "ps":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.orchestrate import cmd_ps

        cmd_ps(cfg, repo_root, json_output=args.json_output)

    elif args.cmd == "api":
        cfg, repo_root = _get_cfg_and_root()
        from lanegate.api import cmd_api

        cmd_api(cfg, repo_root, port=args.port)

    elif args.cmd == "mcp":
        from lanegate.mcp import run_mcp_server

        run_mcp_server()

    elif args.cmd == "tui":
        _, repo_root = _get_cfg_and_root()
        from lanegate.tui import cmd_tui

        cmd_tui(
            repo_root,
            fixture=Path(args.fixture) if args.fixture else None,
            fixture_dir=Path(args.fixture_dir) if args.fixture_dir else None,
            api_url=args.api_url,
            no_api_start=args.no_api_start,
            port=args.port,
        )

    elif args.cmd == "orchestrator-lock":
        _, repo_root = _get_cfg_and_root()
        _cmd_orchestrator_lock(args, repo_root)

    elif args.cmd == "executor":
        cfg, repo_root = _get_cfg_and_root()
        _cmd_executor(args, cfg, repo_root)


def _cmd_executor(args, cfg: dict, repo_root: Path) -> None:
    from lanegate.executor import cmd_executor_reset, cmd_executor_status

    if args.executor_cmd == "status":
        cmd_executor_status(cfg, repo_root)
    elif args.executor_cmd == "reset":
        cmd_executor_reset(cfg, repo_root, name=args.name, reset_all=args.reset_all)


def _cmd_orchestrator_lock(args, repo_root: Path) -> None:
    from lanegate.concurrency import (
        OrchestratorLockError,
        acquire_orchestrator_lock,
        orchestrator_lock_status,
        release_orchestrator_lock,
    )

    if args.orch_cmd == "acquire":
        try:
            pid = acquire_orchestrator_lock(repo_root, pid=args.pid, force=args.force)
        except OrchestratorLockError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"Orchestrator lock acquired (PID {pid}).")

    elif args.orch_cmd == "release":
        removed = release_orchestrator_lock(repo_root, pid=args.pid, force=args.force)
        if removed:
            print("Orchestrator lock released.")
        else:
            print("No matching orchestrator lock to release.")

    elif args.orch_cmd == "status":
        st = orchestrator_lock_status(repo_root)
        if st["held"]:
            print(f"Orchestrator lock HELD by PID {st['pid']} (alive).")
        elif st["pid"] is not None:
            print(f"Orchestrator lock is STALE (PID {st['pid']} not running) — reclaimable.")
        else:
            print("No orchestrator lock held.")


def _cmd_orchestrate_status(repo_root: Path, args) -> None:
    """Report active orchestration status: ticket, executor PID, elapsed time, log path."""
    from lanegate.orchestrate import _normalize_active_status

    status = _normalize_active_status(repo_root)

    if args.json_output:
        import json
        print(json.dumps(status, indent=2))
    else:
        # Human-readable output
        if status.get("active"):
            tid = status.get("ticket_id") or "unknown"
            pid = status.get("executor_pid") or "unknown"
            elapsed = status.get("elapsed") or "unknown"
            log_path = status.get("log_path") or "(not set)"
            audit_path = status.get("audit_bundle_path") or "(not set)"
            state = status.get("reconciliation_state") or "unknown"
            heartbeats = status.get("heartbeat_count") or 0

            print("Active orchestration:")
            print(f"  Ticket:     {tid}")
            print(f"  Executor:   PID {pid} (status: {state})")
            print(f"  Elapsed:    {elapsed}")
            print(f"  Heartbeats: {heartbeats}")
            print(f"  Log:        {log_path}")
            print(f"  Audit:      {audit_path}")
        else:
            print("No active orchestration run.")
            state = status.get("reconciliation_state") or "none"
            if state != "none" and state != "no-active-run":
                print(f"Last state: {state}")
                if status.get("audit_bundle_path"):
                    print(f"Audit: {status['audit_bundle_path']}")


def _cmd_prompts_eject(project_root: Path, *, force: bool = False) -> None:
    """Copy built-in prompt templates to <project_root>/prompts/ for customization.

    Each copied file gets a header comment listing the ``{{ variable }}``
    placeholders the template supports.  Files that already exist are skipped
    unless *force* is True.
    """
    import importlib.resources as pkg_resources

    # Header comments documenting variables for each template.
    _HEADERS: dict[str, str] = {
        "analyze.md": (
            "<!-- lanegate built-in template: analyze.md\n"
            "     Available variables:\n"
            "       {{ context_sections }} — relevant file context gathered from the repo\n"
            "       {{ ticket_id }}        — ticket identifier (e.g. TICK-042)\n"
            "       {{ title }}            — ticket title\n"
            "       {{ intent }}           — raw intent string from ticket body\n"
            "-->\n"
        ),
        "implement.md": (
            "<!-- lanegate built-in template: implement.md\n"
            "     This template is wrapped by build_prompt(); the untrusted-data\n"
            "     block is appended automatically and contains:\n"
            "       TICKET TITLE     — ticket title\n"
            "       TICKET BODY      — full ticket markdown body\n"
            "       TOUCHES          — list of files declared in the ticket\n"
            "       CLOSE CRITERIA   — acceptance criteria from the ticket\n"
            "-->\n"
        ),
        "review.md": (
            "<!-- lanegate built-in template: review.md\n"
            "     This template is wrapped by build_prompt(); the untrusted-data\n"
            "     block is appended automatically and contains:\n"
            "       TICKET TITLE     — ticket title\n"
            "       TICKET BODY      — full ticket markdown body\n"
            "       TOUCHES          — list of files declared in the ticket\n"
            "       CLOSE CRITERIA   — acceptance criteria from the ticket\n"
            "     Response format: JSON with keys verdict, summary, findings.\n"
            "-->\n"
        ),
        "fix.md": (
            "<!-- lanegate built-in template: fix.md\n"
            "     This template is wrapped by build_prompt(); the untrusted-data\n"
            "     block is appended automatically and contains:\n"
            "       TICKET TITLE     — ticket title\n"
            "       CLOSE CRITERIA   — acceptance criteria from the ticket\n"
            "       GIT DIFF         — the ticket's current diff to fix\n"
            "-->\n"
        ),
        "drift_check.md": (
            "<!-- lanegate built-in template: drift_check.md\n"
            "     This template is wrapped by build_prompt(); the untrusted-data\n"
            "     block is appended automatically and contains:\n"
            "       CLOSE CRITERIA   — acceptance criteria from the ticket\n"
            "       REVIEW FINDINGS  — findings the fix pass was asked to address\n"
            "       ORIGINAL DIFF    — diff before the fix pass ran\n"
            "       FIX DIFF         — only the changes made by the fix pass\n"
            "     Response format: JSON with keys drift_ok, reason.\n"
            "-->\n"
        ),
    }

    prompts_dir = project_root / "prompts"
    prompts_dir.mkdir(exist_ok=True)

    prompts_ref = pkg_resources.files("lanegate").joinpath("templates").joinpath("prompts")
    written: list[str] = []
    skipped: list[str] = []

    for resource in sorted(prompts_ref.iterdir(), key=lambda r: r.name):
        if not resource.name.endswith(".md"):
            continue
        dst = prompts_dir / resource.name
        if dst.exists() and not force:
            skipped.append(resource.name)
            continue
        with pkg_resources.as_file(resource) as src:
            original = src.read_text(encoding="utf-8")
        header = _HEADERS.get(resource.name, f"<!-- lanegate built-in template: {resource.name} -->\n")
        dst.write_text(header + original, encoding="utf-8")
        written.append(resource.name)

    # Summary
    for name in written:
        print(f"  written:  prompts/{name}")
    for name in skipped:
        print(f"  skipped:  prompts/{name}  (already exists; use --force to overwrite)")

    total = len(written) + len(skipped)
    print(f"\n{len(written)} of {total} template(s) written to prompts/")
    if skipped and not force:
        print("Re-run with --force to overwrite existing files.")


def _cmd_init(use_defaults: bool = False, force_interactive: bool = False) -> None:
    from lanegate.config import CONFIG_FILENAME, interactive_init

    cwd = Path.cwd()
    result = interactive_init(cwd, use_defaults=use_defaults, force_interactive=force_interactive)
    if result is None:
        sys.exit(1)

    print(f"Scaffolding {CONFIG_FILENAME}... done")

    # Optionally scaffold prompt templates
    _scaffold_prompts(cwd, use_defaults=use_defaults, force_interactive=force_interactive)

    _cmd_install_commands()

    print("\nChecking tool availability...")
    from lanegate.doctor import cmd_doctor

    cmd_doctor(result)

    print(f"Run `{APP_NAME} board` to see your ticket board.")


def _scaffold_prompts(
    project_root: Path, use_defaults: bool = False, force_interactive: bool = False
) -> None:
    """Offer to copy built-in prompt templates to <project_root>/prompts/ for editing."""
    import importlib.resources as pkg_resources
    import shutil
    import sys

    is_tty = force_interactive or sys.stdin.isatty()

    if use_defaults or not is_tty:
        # Non-interactive: skip silently
        return

    try:
        answer = (
            input("Customize prompt templates? (copies defaults to prompts/ for editing) [y/N]: ")
            .strip()
            .lower()
        )
    except (EOFError, KeyboardInterrupt):
        return

    if answer not in ("y", "yes"):
        return

    prompts_dir = project_root / "prompts"
    prompts_dir.mkdir(exist_ok=True)

    prompts_ref = pkg_resources.files("lanegate").joinpath("templates").joinpath("prompts")
    copied = []
    for resource in prompts_ref.iterdir():
        if resource.name.endswith(".md"):
            dst = prompts_dir / resource.name
            with pkg_resources.as_file(resource) as src:
                shutil.copy(src, dst)
            copied.append(resource.name)

    if copied:
        print(f"Scaffolded {len(copied)} prompt template(s) to prompts/:")
        for name in sorted(copied):
            print(f"  prompts/{name}")
    else:
        print("No prompt templates found to scaffold.")


def _cmd_install_commands() -> None:
    from lanegate.agent_tools import install_claude_commands

    result = install_claude_commands(Path.cwd())
    commands = result["commands"]
    if commands:
        print(f"Installed {len(commands)} command(s) to .claude/commands/lanegate/:")
        for command in sorted(commands):
            print(f"  {command}")
    else:
        print("No commands to install.")


def _cmd_install_agent_tools(*, json_output: bool = False) -> None:
    from lanegate.agent_tools import install_agent_tools

    result = install_agent_tools(Path.cwd())
    if json_output:
        import json

        print(json.dumps(result, indent=2))
        return

    print("Installed LaneGate agent tools:")
    for artifact in result["artifacts"]:
        if artifact["kind"] == "slash_commands":
            print(f"  Claude commands: {artifact['directory']}")
            for command in sorted(artifact["commands"]):
                print(f"    {command}")
        elif artifact["kind"] == "mcp_config":
            print(f"  {artifact['agent']} MCP config: {artifact['path']}")
    print("Bounded MCP tools:")
    print("  " + ", ".join(result["tools"]["bounded_tools"]))


if __name__ == "__main__":
    main()
