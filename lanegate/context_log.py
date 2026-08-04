"""
context_log.py — Per-ticket agent delegation cost tracking.

Public API:
  log_agent_run        — write one entry (JSONL + optionally SQLite)
  compute_stats        — pure function: compute analytics from a list of entries
  stats_json           — JSON-serialise compute_stats output
  cmd_context_stats    — print a summary table from a JSONL log
  cmd_log_backfill     — upsert token data into the central SQLite DB
  load_entries_for_analytics — load from SQLite (with legacy import), or JSONL via --log
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path


def _utcnow() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")

from lanegate import APP_NAME

# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------


def _get_default_db_path() -> Path:
    return Path.home() / ".local" / "share" / APP_NAME / "analytics.db"


def _get_project_id(repo_root: Path) -> str:
    """Derive a stable project identifier from the git remote URL, or fall back to dir name."""
    r = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True,
        text=True, encoding="utf-8",
        cwd=repo_root,
    )
    if r.returncode == 0:
        url = r.stdout.strip().rstrip("/")
        if url.endswith(".git"):
            url = url[:-4]
        parts = url.replace(":", "/").split("/")
        if len(parts) >= 2:
            return f"{parts[-2]}/{parts[-1]}"
    return repo_root.name


def _init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS analytics (
            project       TEXT NOT NULL,
            ticket_id     TEXT NOT NULL,
            executor      TEXT,
            model         TEXT,
            subagent_tokens INTEGER,
            summary_tokens  INTEGER,
            tool_uses       INTEGER,
            duration_ms     INTEGER,
            wall_time_ms    INTEGER,
            tests_passed    INTEGER,
            drift_warnings  INTEGER,
            parallel_peers  TEXT,
            batch_id        TEXT,
            merged_at       TEXT,
            timestamp       TEXT,
            PRIMARY KEY (project, ticket_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            project        TEXT NOT NULL,
            session_id     TEXT NOT NULL,
            session_tokens INTEGER,
            tickets_merged TEXT,
            timestamp      TEXT,
            PRIMARY KEY (project, session_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metadata (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    # Real per-step executor cost, parsed from `claude ... --output-format json`
    # (see parse_claude_json_result in executor.py). Append-only -- unlike
    # `analytics` (one upserted summary row per ticket), a ticket can dispatch
    # the same step more than once (retries, autofix cycles) and each
    # dispatch gets its own row rather than overwriting the last.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS step_costs (
            project               TEXT NOT NULL,
            ticket_id             TEXT NOT NULL,
            step                  TEXT NOT NULL,
            executor              TEXT,
            model                 TEXT,
            input_tokens          INTEGER,
            output_tokens         INTEGER,
            cache_creation_tokens INTEGER,
            cache_read_tokens     INTEGER,
            cost_usd              REAL,
            duration_ms           INTEGER,
            num_turns             INTEGER,
            timestamp             TEXT,
            session_id            TEXT
        )
    """)
    # session_id is newer than the rest of step_costs (TICK-310, session
    # chaining) -- upgrade DBs created before it existed.
    try:
        conn.execute("ALTER TABLE step_costs ADD COLUMN session_id TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    # Upgrade existing DBs that predate the touched_files column
    try:
        conn.execute("ALTER TABLE analytics ADD COLUMN touched_files TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()
    conn.close()


def _merge_analytics_updates(db_path: Path, project: str, ticket_id: str, updates: dict) -> dict:
    """Load any existing analytics row for (project, ticket_id) and layer the
    given `updates` on top, so a later log call can't blindly regress
    previously-known values (e.g. a real `lanegate log` backfill's token counts)
    back to null/0 placeholders. Callers should omit keys they don't have a
    real value for rather than passing None/0 -- any key present in `updates`
    always wins.
    """
    _init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    existing = conn.execute(
        "SELECT * FROM analytics WHERE project = ? AND ticket_id = ?",
        (project, ticket_id),
    ).fetchone()
    conn.close()
    row = dict(existing) if existing else {"ticket_id": ticket_id}
    row.update(updates)
    return row


def _upsert_row(db_path: Path, project: str, row: dict) -> None:
    """INSERT OR REPLACE one analytics row into the central DB."""
    _init_db(db_path)
    tp = row.get("tests_passed")
    tp_int = None if tp is None else (1 if tp else 0)
    peers = row.get("parallel_peers")
    peers_json = json.dumps(peers) if peers is not None else None
    touched = row.get("touched_files")
    touched_json = json.dumps(touched) if touched is not None else None

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT OR REPLACE INTO analytics
        (project, ticket_id, executor, model, subagent_tokens, summary_tokens,
         tool_uses, duration_ms, wall_time_ms, tests_passed, drift_warnings,
         parallel_peers, batch_id, merged_at, timestamp, touched_files)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            project,
            row.get("ticket_id", ""),
            row.get("executor", ""),
            row.get("model", ""),
            row.get("subagent_tokens"),
            row.get("summary_tokens", 0),
            row.get("tool_uses", 0),
            row.get("duration_ms", 0),
            row.get("wall_time_ms", 0),
            tp_int,
            row.get("drift_warnings", 0),
            peers_json,
            row.get("batch_id", ""),
            row.get("merged_at"),
            row.get("timestamp", _utcnow()),
            touched_json,
        ),
    )
    conn.commit()
    conn.close()


def _load_entries_from_db(db_path: Path, project: str | None = None) -> list[dict]:
    """Return analytics rows from SQLite, optionally filtered to one project."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    if project:
        rows = conn.execute(
            "SELECT * FROM analytics WHERE project = ? ORDER BY timestamp",
            (project,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM analytics ORDER BY project, timestamp",
        ).fetchall()
    conn.close()

    result = []
    for r in rows:
        d = dict(r)
        if d.get("parallel_peers"):
            try:
                d["parallel_peers"] = json.loads(d["parallel_peers"])
            except (json.JSONDecodeError, TypeError):
                d["parallel_peers"] = None
        if d.get("touched_files"):
            try:
                d["touched_files"] = json.loads(d["touched_files"])
            except (json.JSONDecodeError, TypeError):
                d["touched_files"] = []
        else:
            d["touched_files"] = []
        tp = d.get("tests_passed")
        d["tests_passed"] = None if tp is None else bool(tp)
        result.append(d)
    return result


def _is_legacy_imported(db_path: Path, project: str) -> bool:
    if not db_path.exists():
        return False
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT value FROM metadata WHERE key = ?",
        (f"legacy_imported_{project}",),
    ).fetchone()
    conn.close()
    return row is not None


def _import_legacy(db_path: Path, jsonl_path: Path, project: str) -> int:
    """Import JSONL entries into SQLite on first run. Returns count imported."""
    _init_db(db_path)
    entries = _load_entries([jsonl_path])
    for e in entries:
        _upsert_row(db_path, project, e)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
        (f"legacy_imported_{project}", "1"),
    )
    conn.commit()
    conn.close()
    return len(entries)


def load_entries_for_analytics(
    repo_root: Path,
    jsonl_paths: list[Path] | None = None,
    all_projects: bool = False,
    db_path: Path | None = None,
) -> tuple[list[dict], bool]:
    """Return (entries, show_project_column).

    If jsonl_paths is provided, reads those JSONL files (backward compat).
    Otherwise, reads from the central SQLite DB (with one-time legacy import).
    show_project_column is True when all_projects is requested.
    """
    if jsonl_paths:
        return _load_entries(jsonl_paths), False

    if db_path is None:
        db_path = _get_default_db_path()

    project = _get_project_id(repo_root)

    if not all_projects:
        if not _is_legacy_imported(db_path, project):
            jsonl_path = repo_root / "lanegate-context-log.jsonl"
            if jsonl_path.exists():
                n = _import_legacy(db_path, jsonl_path, project)
                if n:
                    print(f"Imported {n} legacy entries from lanegate-context-log.jsonl")
            else:
                _init_db(db_path)
                conn = sqlite3.connect(str(db_path))
                conn.execute(
                    "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                    (f"legacy_imported_{project}", "1"),
                )
                conn.commit()
                conn.close()
        return _load_entries_from_db(db_path, project=project), False

    return _load_entries_from_db(db_path, project=None), True


def cmd_log_backfill(
    ticket_id: str,
    repo_root: Path,
    *,
    db_path: Path | None = None,
    subagent_tokens: int | None = None,
    summary_tokens: int | None = None,
    executor: str | None = None,
    model: str | None = None,
    tests_passed: bool | None = None,
) -> None:
    """Upsert analytics data for a ticket (backfill or override)."""
    if db_path is None:
        db_path = _get_default_db_path()
    project = _get_project_id(repo_root)

    updates = {"timestamp": _utcnow()}
    if subagent_tokens is not None:
        updates["subagent_tokens"] = subagent_tokens
    if summary_tokens is not None:
        updates["summary_tokens"] = summary_tokens
    if executor is not None:
        updates["executor"] = executor
    if model is not None:
        updates["model"] = model
    if tests_passed is not None:
        updates["tests_passed"] = tests_passed

    row = _merge_analytics_updates(db_path, project, ticket_id, updates)
    _upsert_row(db_path, project, row)
    print(f"Updated analytics for {ticket_id} in project {project}")


def log_step_cost(
    db_path: Path,
    project: str,
    ticket_id: str,
    step: str,
    *,
    executor: str = "",
    model: str = "",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_creation_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cost_usd: float | None = None,
    duration_ms: int = 0,
    num_turns: int | None = None,
    timestamp: str | None = None,
    session_id: str | None = None,
) -> None:
    """Append one real per-step cost record (analyze/implement/review/fix).

    session_id (TICK-310) scopes resume_session_gate()'s age/size ceiling
    checks to one chained session rather than a ticket's whole history.
    """
    _init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO step_costs
        (project, ticket_id, step, executor, model, input_tokens, output_tokens,
         cache_creation_tokens, cache_read_tokens, cost_usd, duration_ms, num_turns,
         timestamp, session_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project,
            ticket_id,
            step,
            executor,
            model,
            input_tokens,
            output_tokens,
            cache_creation_tokens,
            cache_read_tokens,
            cost_usd,
            duration_ms,
            num_turns,
            timestamp or _utcnow(),
            session_id,
        ),
    )
    conn.commit()
    conn.close()


def record_step_cost(
    repo_root: Path,
    ticket_id: str,
    step: str,
    executor: str,
    model: str,
    parsed: dict | None,
    *,
    db_path: Path | None = None,
) -> None:
    """Best-effort log_step_cost() call from a parse_structured_result() dict.

    Executor-agnostic: works for any executor type with a registered parser
    in executor.py's _STRUCTURED_RESULT_PARSERS (Claude, Codex, and whatever
    is added later) -- this function only knows about the normalized dict
    shape, not which CLI produced it.

    No-ops silently when parsed is None (an executor type with no registered
    parser, or a reply that didn't match the expected shape) or if writing to the
    DB fails for any reason -- cost tracking must never break dispatch.
    """
    if parsed is None:
        return
    try:
        if db_path is None:
            db_path = _get_default_db_path()
        project = _get_project_id(repo_root)
        log_step_cost(
            db_path,
            project,
            ticket_id,
            step,
            executor=executor,
            model=model,
            input_tokens=parsed.get("input_tokens"),
            output_tokens=parsed.get("output_tokens"),
            cache_creation_tokens=parsed.get("cache_creation_tokens"),
            cache_read_tokens=parsed.get("cache_read_tokens"),
            cost_usd=parsed.get("cost_usd"),
            duration_ms=parsed.get("duration_ms") or 0,
            num_turns=parsed.get("num_turns"),
            session_id=parsed.get("session_id"),
        )
    except Exception:
        pass


def _load_step_costs_from_db(db_path: Path, project: str | None = None) -> list[dict]:
    """Return step_costs rows from SQLite, optionally filtered to one project."""
    if not db_path.exists():
        return []
    # step_costs is newer than the rest of this schema -- migrate DBs that
    # predate it (CREATE TABLE IF NOT EXISTS, so a no-op once it's there).
    _init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    if project:
        rows = conn.execute(
            "SELECT * FROM step_costs WHERE project = ? ORDER BY timestamp",
            (project,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM step_costs ORDER BY project, timestamp",
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def resume_session_gate(
    cfg: dict, db_path: Path, project: str, session_id: str
) -> tuple[bool, str]:
    """Decide whether resuming `session_id` is still safe/worth it (TICK-310).

    The ~9x cache-cost saving `--resume` gets only holds while the server's
    prompt cache is still warm; past that, a resumed call re-pays cache-write
    price for the *entire* accumulated session instead of just the fixed
    per-invocation bootstrap a cold call pays, which can cost more than
    starting fresh. Checks two ceilings from session_chaining config against
    this session's own step_costs history: elapsed time since its last
    logged step, and its total accumulated tokens so far.

    Fails open (True) when there's no step_costs history yet for this
    session_id -- the ceilings only have something to bite on once a session
    has accumulated real usage; a brand new session is always safe to resume
    (that's the whole point of resuming it).
    """
    from lanegate.config import resolve_session_chaining

    settings = resolve_session_chaining(cfg)
    if not settings["enabled"]:
        return False, "session_chaining.enabled is false"

    rows = [r for r in _load_step_costs_from_db(db_path, project) if r.get("session_id") == session_id]
    if not rows:
        return True, "no prior step_costs history for this session yet"

    timestamps = [r["timestamp"] for r in rows if r.get("timestamp")]
    if timestamps:
        latest = max(timestamps)
        try:
            latest_dt = datetime.fromisoformat(latest.replace("Z", "+00:00"))
            age_s = (datetime.now(UTC) - latest_dt).total_seconds()
        except ValueError:
            age_s = 0.0
        if age_s > settings["max_session_age_s"]:
            return False, (
                f"session last active {int(age_s)}s ago, exceeds "
                f"max_session_age_s={settings['max_session_age_s']}"
            )

    total_tokens = sum(
        (r.get("input_tokens") or 0) + (r.get("cache_creation_tokens") or 0)
        + (r.get("cache_read_tokens") or 0)
        for r in rows
    )
    if total_tokens > settings["max_session_tokens"]:
        return False, (
            f"session accumulated ~{total_tokens} tokens, exceeds "
            f"max_session_tokens={settings['max_session_tokens']}"
        )

    return True, "within age/size ceilings"


def compute_step_cost_stats(entries: list[dict]) -> dict:
    """Pure function: aggregate real per-step cost/token data from step_costs rows.

    Distinct from compute_stats() -- that function computes the subagent/
    summary-token "compression" story for Task-tool delegation. This is real
    dollars and real tokens as reported by the executor CLI itself.
    """
    by_step: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        by_step[e.get("step", "?")].append(e)

    steps = []
    for step, rows in sorted(by_step.items()):
        costs = [r["cost_usd"] for r in rows if r.get("cost_usd") is not None]
        in_tok = [r["input_tokens"] for r in rows if r.get("input_tokens") is not None]
        out_tok = [r["output_tokens"] for r in rows if r.get("output_tokens") is not None]
        cache_new = [r["cache_creation_tokens"] for r in rows if r.get("cache_creation_tokens") is not None]
        cache_hit = [r["cache_read_tokens"] for r in rows if r.get("cache_read_tokens") is not None]
        steps.append(
            {
                "step": step,
                "count": len(rows),
                "total_cost_usd": round(sum(costs), 4) if costs else None,
                "avg_cost_usd": round(sum(costs) / len(costs), 4) if costs else None,
                "avg_input_tokens": round(sum(in_tok) / len(in_tok)) if in_tok else None,
                "avg_output_tokens": round(sum(out_tok) / len(out_tok)) if out_tok else None,
                "avg_cache_creation_tokens": round(sum(cache_new) / len(cache_new)) if cache_new else None,
                "avg_cache_read_tokens": round(sum(cache_hit) / len(cache_hit)) if cache_hit else None,
            }
        )

    all_costs = [r["cost_usd"] for r in entries if r.get("cost_usd") is not None]
    return {
        "steps": steps,
        "total_cost_usd": round(sum(all_costs), 4) if all_costs else None,
        "total_dispatches": len(entries),
    }


def stats_json_step_costs(entries: list[dict]) -> dict:
    """JSON-serialisable form of compute_step_cost_stats(), for --json output."""
    return compute_step_cost_stats(entries)


def _print_step_cost_panel(entries: list[dict]) -> None:
    """Print the --full 'Real Step Cost' panel from step_costs rows."""
    if not entries:
        return
    stats = compute_step_cost_stats(entries)
    W_STEP, W_N, W_COST, W_IN, W_OUT, W_CC, W_CR = 12, 6, 12, 10, 10, 14, 14

    print("--- Real Step Cost (from executor --output-format json) ---")
    header = (
        f"{'Step':<{W_STEP}}  {'N':>{W_N}}  {'Avg cost':>{W_COST}}  "
        f"{'Avg in tok':>{W_IN}}  {'Avg out tok':>{W_OUT}}  "
        f"{'Avg cache-new':>{W_CC}}  {'Avg cache-hit':>{W_CR}}"
    )
    print(header)
    print("-" * len(header))
    for s in stats["steps"]:
        cost_str = f"${s['avg_cost_usd']:.4f}" if s["avg_cost_usd"] is not None else "—"
        in_str = f"{s['avg_input_tokens']:,}" if s["avg_input_tokens"] is not None else "—"
        out_str = f"{s['avg_output_tokens']:,}" if s["avg_output_tokens"] is not None else "—"
        cc_str = (
            f"{s['avg_cache_creation_tokens']:,}" if s["avg_cache_creation_tokens"] is not None else "—"
        )
        cr_str = f"{s['avg_cache_read_tokens']:,}" if s["avg_cache_read_tokens"] is not None else "—"
        print(
            f"{s['step']:<{W_STEP}}  {s['count']:>{W_N}}  {cost_str:>{W_COST}}  "
            f"{in_str:>{W_IN}}  {out_str:>{W_OUT}}  {cc_str:>{W_CC}}  {cr_str:>{W_CR}}"
        )
    total_str = f"${stats['total_cost_usd']:.4f}" if stats["total_cost_usd"] is not None else "—"
    print(f"\nTotal real cost logged: {total_str} across {stats['total_dispatches']} dispatches")
    print()


def cmd_session_summary(
    repo_root: Path,
    session_tokens: int,
    tickets: list[str],
    *,
    db_path: Path | None = None,
) -> None:
    """Record the main-session orchestrator cost for a completed run."""
    import uuid

    if db_path is None:
        db_path = _get_default_db_path()
    project = _get_project_id(repo_root)
    _init_db(db_path)
    session_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT OR REPLACE INTO sessions (project, session_id, session_tokens, tickets_merged, timestamp) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            project,
            session_id,
            session_tokens,
            json.dumps(tickets) if tickets else None,
            _utcnow(),
        ),
    )
    conn.commit()
    conn.close()
    tickets_str = ", ".join(tickets) if tickets else "none specified"
    print(f"Session recorded: {session_id}")
    print(f"  orchestrator tokens : {session_tokens:,}")
    print(f"  tickets this run    : {tickets_str}")


def _load_sessions_from_db(db_path: Path, project: str | None = None) -> list[dict]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    if project:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE project = ? ORDER BY timestamp",
            (project,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM sessions ORDER BY timestamp").fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["tickets_merged"] = json.loads(d["tickets_merged"]) if d.get("tickets_merged") else []
        result.append(d)
    return result


def log_agent_run(
    log_path: Path | None,
    ticket_id: str,
    subagent_tokens: int | None,  # None for executors that don't report tokens
    tool_uses: int,
    duration_ms: int,
    touched_files: list[str],
    repo_root: Path,
    *,
    summary_tokens: int = 0,  # tokens of the notification summary that entered main context
    executor: str = "claude",  # claude-subagent | claude-process | aider | openhands | codex | ollama
    model: str = "",  # e.g. "claude-sonnet-4-6", "gpt-4o", "llama3.2"
    wall_time_ms: int = 0,  # actual wall clock time (may differ from duration_ms in parallel runs)
    parallel_peers: list[str] | None = None,  # ticket ids that ran concurrently in same batch
    batch_id: str = "",  # identifier for the parallel batch this ticket belonged to
    tests_passed: bool | None = None,
    drift_warnings: int = 0,
    db_path: Path | None = None,  # if provided, also upsert into SQLite
) -> None:
    """Write one record for a completed agent run.

    Always writes to JSONL (at log_path or the default per-repo path).
    When db_path is provided, also upserts into the central SQLite DB.

    main_session_cost = summary_tokens (what actually entered the main context)
    work_cost         = subagent_tokens (what the agent consumed, out of main context)
    compression_ratio = subagent_tokens / summary_tokens
    """
    if log_path is None:
        log_path = repo_root / "lanegate-context-log.jsonl"

    now = _utcnow()
    record = {
        "ticket_id": ticket_id,
        "subagent_tokens": subagent_tokens,
        "summary_tokens": summary_tokens,
        "tool_uses": tool_uses,
        "duration_ms": duration_ms,
        "wall_time_ms": wall_time_ms,
        "touched_files": touched_files,
        "executor": executor,
        "model": model,
        "parallel_peers": parallel_peers,
        "batch_id": batch_id,
        "tests_passed": tests_passed,
        "drift_warnings": drift_warnings,
        "timestamp": now,
    }

    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")

    if db_path is not None:
        project = _get_project_id(repo_root)
        updates = {**record, "merged_at": now}
        # A caller that doesn't know real per-ticket token counts (e.g. the
        # merge-time fallback log, which always passes subagent_tokens=None)
        # must not regress numbers a previous `lanegate log` backfill already
        # established -- omit the "don't know" fields so the merge preserves
        # whatever is already in the DB for this ticket.
        if subagent_tokens is None:
            updates.pop("subagent_tokens", None)
        if not summary_tokens:
            updates.pop("summary_tokens", None)
        if tests_passed is None:
            updates.pop("tests_passed", None)
        row = _merge_analytics_updates(db_path, project, ticket_id, updates)
        _upsert_row(db_path, project, row)


# ---------------------------------------------------------------------------
# Pure compute — single source of truth for all analytics math
# ---------------------------------------------------------------------------


def compute_stats(entries: list[dict]) -> dict:
    """Pure function: compute all analytics from a list of log entries.

    Returns a structured dict with totals, per-ticket data, parallelism,
    cost trend, quality, and a plain-English verdict. Printers and JSON
    output both consume this dict so the math lives in exactly one place.
    """
    work_vals = [_get_subagent_tokens(e) for e in entries]
    main_vals = [int(e.get("summary_tokens", 0)) for e in entries]

    # Per-ticket rows
    tickets = []
    for e, work_tok, main_tok in zip(entries, work_vals, main_vals):
        comp = round(work_tok / main_tok) if (work_tok and main_tok) else None
        tickets.append(
            {
                "ticket_id": e.get("ticket_id", "?"),
                "work_tokens": work_tok,
                "main_tokens": main_tok,
                "compression": comp,
            }
        )

    # Totals
    has_work = any(v is not None for v in work_vals)
    total_work = sum(v for v in work_vals if v is not None)
    total_main = sum(main_vals)
    comp_ratio = (
        round(total_work / total_main) if (has_work and total_work > 0 and total_main > 0) else None
    )
    kept_out = (total_work - total_main) if (has_work and total_work > 0) else None

    # Parallelism
    batches: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        bid = e.get("batch_id", "")
        if bid:
            batches[bid].append(e)

    parallelism = []
    for bid, batch_entries in sorted(batches.items()):
        n = len(batch_entries)
        wall_vals = [int(e.get("wall_time_ms", 0)) for e in batch_entries]
        dur_vals = [int(e.get("duration_ms", 0)) for e in batch_entries]
        max_wall = max(wall_vals)
        sum_dur = sum(dur_vals)
        gain = round(sum_dur / max_wall, 1) if max_wall > 0 else None
        parallelism.append(
            {
                "batch_id": bid,
                "tickets": n,
                "wall_s": max_wall // 1000,
                "sum_durations_s": sum_dur // 1000,
                "gain": gain,
            }
        )

    # Cost trend — linear regression over rolling window of last 10, minimum 5
    _TREND_WINDOW = 10
    _TREND_MIN = 5
    all_trend_points = [
        {"ticket_id": e.get("ticket_id", "?"), "work_tokens": v}
        for e, v in zip(entries, work_vals)
        if v is not None
    ]
    trend_points = all_trend_points[-_TREND_WINDOW:]
    n = len(trend_points)
    rising = False
    if n >= _TREND_MIN:
        vals = [p["work_tokens"] for p in trend_points]
        mean = sum(vals) / n
        # OLS slope: β = (n·Σxy - Σx·Σy) / (n·Σx² - (Σx)²)
        xs = list(range(n))
        sx = sum(xs)
        sy = sum(vals)
        sxy = sum(x * y for x, y in zip(xs, vals))
        sxx = sum(x * x for x in xs)
        denom = n * sxx - sx * sx
        slope = (n * sxy - sx * sy) / denom if denom else 0.0
        # Normalise: slope as fraction of mean per ticket step
        slope_pct = slope / mean if mean else 0.0
        if slope_pct > 0.10:
            trend_verdict = "RISING"
            rising = True
        elif slope_pct < -0.10:
            trend_verdict = "FALLING"
        else:
            trend_verdict = "FLAT"
    else:
        trend_verdict = None  # not enough data yet

    # Quality
    tested = [e.get("tests_passed") for e in entries if e.get("tests_passed") is not None]
    total_drift = sum(int(e.get("drift_warnings", 0)) for e in entries)
    if tested:
        passed_count = sum(1 for tp in tested if tp)
        total_tested = len(tested)
        pass_rate: float | None = passed_count / total_tested
    else:
        passed_count = 0
        total_tested = 0
        pass_rate = None

    # Overall verdict
    ratio_val = (total_work / total_main) if (has_work and total_main and total_work) else 0
    if ratio_val < 2 or (rising and pass_rate is not None and pass_rate < 1.0):
        verdict_label = "NOT WORTH IT"
    elif ratio_val < 10:
        verdict_label = "BREAK-EVEN"
    else:
        verdict_label = "PAYING OFF"

    parts: list[str] = []
    if ratio_val:
        parts.append(f"{ratio_val:.0f}x compression")
    if trend_verdict is not None:
        parts.append("rising cost trend" if rising else "flat cost trend")
    if pass_rate is not None:
        parts.append(f"{round(pass_rate * 100)}% test pass rate")

    return {
        "has_entries": True,
        "totals": {
            "work_tokens": total_work if has_work else None,
            "main_tokens": total_main,
            "compression_ratio": comp_ratio,
            "kept_out": kept_out,
        },
        "tickets": tickets,
        "parallelism": parallelism,
        "cost_trend": {
            "points": trend_points,
            "verdict": trend_verdict,
        },
        "quality": {
            "tests_passed": passed_count,
            "tests_total": total_tested,
            "pass_rate": pass_rate,
            "drift_warnings": total_drift,
        },
        "verdict": {
            "label": verdict_label,
            "detail": ", ".join(parts),
        },
    }


def stats_json(
    entries: list[dict],
    sessions: list[dict] | None = None,
    step_costs: list[dict] | None = None,
) -> str:
    """Return compute_stats(entries) plus sessions/step_costs data as a JSON string."""
    data = compute_stats(entries)
    if step_costs:
        data["step_costs"] = compute_step_cost_stats(step_costs)
    if sessions:
        data["sessions"] = [
            {
                "session_id": s.get("session_id"),
                "session_tokens": s.get("session_tokens"),
                "tickets_merged": s.get("tickets_merged", []),
                "timestamp": s.get("timestamp", ""),
            }
            for s in sessions
        ]
        total_sess_tok = sum(s["session_tokens"] for s in sessions if s.get("session_tokens"))
        tickets_per_session = sum(len(s.get("tickets_merged") or []) for s in sessions) / len(
            sessions
        )
        data["session_totals"] = {
            "total_session_tokens": total_sess_tok,
            "session_count": len(sessions),
            "avg_tickets_per_run": round(tickets_per_session, 1),
            "avg_overhead_per_ticket": (
                round(
                    total_sess_tok
                    / max(1, sum(len(s.get("tickets_merged") or []) for s in sessions))
                )
                if sessions
                else None
            ),
        }
    return json.dumps(data)


# ---------------------------------------------------------------------------
# Entry loading
# ---------------------------------------------------------------------------


def _load_entries(paths: list[Path]) -> list[dict]:
    """Load and merge JSONL entries from one or more log files."""
    entries: list[dict] = []
    for p in paths:
        if not p.exists():
            continue
        for raw in p.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if raw:
                try:
                    entries.append(json.loads(raw))
                except json.JSONDecodeError:
                    pass
    return entries


# ---------------------------------------------------------------------------
# Public command — print summary
# ---------------------------------------------------------------------------


def cmd_context_stats(log_path: Path, full: bool = False, compare: bool = False) -> None:
    """Print a table summarising context cost across all logged agent runs."""
    entries = _load_entries([log_path])

    if not entries:
        print("No context log entries yet.")
        return

    if compare:
        _print_compare(entries)
        return

    _print_basic_table(entries)

    if full:
        _print_full_panels(entries)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_subagent_tokens(e: dict) -> int | None:
    v = e.get("subagent_tokens")
    if v is None:
        return None
    return int(v)


def _print_basic_table(entries: list[dict]) -> None:
    stats = compute_stats(entries)
    W_TICKET = 10
    W_WORK = 11
    W_MAIN = 16
    W_COMP = 11

    header = (
        f"{'Ticket':<{W_TICKET}}  "
        f"{'Work tokens':>{W_WORK}}  "
        f"{'Main-session tok':>{W_MAIN}}  "
        f"{'Compression':>{W_COMP}}"
    )
    sep = f"{'-' * W_TICKET}  {'-' * W_WORK}  {'-' * W_MAIN}  {'-' * W_COMP}"

    print("=== Context Cost Stats ===\n")
    print(header)
    print(sep)

    for t in stats["tickets"]:
        work_tok = t["work_tokens"]
        main_tok = t["main_tokens"]

        work_str = f"{work_tok:>{W_WORK},}" if work_tok is not None else f"{'—':>{W_WORK}}"
        comp_str = f"{t['compression']}x" if t["compression"] is not None else "—"

        print(
            f"{t['ticket_id']:<{W_TICKET}}  {work_str}  {main_tok:>{W_MAIN},}  {comp_str:>{W_COMP}}"
        )

    print(sep)

    totals = stats["totals"]
    total_work = totals["work_tokens"]
    total_main = totals["main_tokens"]
    comp_ratio = totals["compression_ratio"]

    work_total_str = f"{total_work:>{W_WORK},}" if total_work is not None else f"{'—':>{W_WORK}}"
    total_comp_str = f"{comp_ratio}x" if comp_ratio is not None else "—"

    print(
        f"{'TOTAL':<{W_TICKET}}  "
        f"{work_total_str}  "
        f"{total_main:>{W_MAIN},}  "
        f"{total_comp_str:>{W_COMP}}"
    )


def _print_all_projects_table(entries: list[dict]) -> None:
    """Print a basic analytics table including a Project column."""
    W_PROJECT = 20
    W_TICKET = 10
    W_WORK = 11
    W_MAIN = 16
    W_COMP = 11

    header = (
        f"{'Project':<{W_PROJECT}}  "
        f"{'Ticket':<{W_TICKET}}  "
        f"{'Work tokens':>{W_WORK}}  "
        f"{'Main-session tok':>{W_MAIN}}  "
        f"{'Compression':>{W_COMP}}"
    )
    sep = f"{'-' * W_PROJECT}  {'-' * W_TICKET}  {'-' * W_WORK}  {'-' * W_MAIN}  {'-' * W_COMP}"

    print("=== Context Cost Stats (all projects) ===\n")
    print(header)
    print(sep)

    for e in entries:
        work_tok = _get_subagent_tokens(e)
        main_tok = int(e.get("summary_tokens", 0))
        comp = round(work_tok / main_tok) if (work_tok and main_tok) else None

        work_str = f"{work_tok:>{W_WORK},}" if work_tok is not None else f"{'—':>{W_WORK}}"
        comp_str = f"{comp}x" if comp is not None else "—"
        project = e.get("project", "?")
        project_str = project[:W_PROJECT]

        print(
            f"{project_str:<{W_PROJECT}}  "
            f"{e.get('ticket_id', '?'):<{W_TICKET}}  "
            f"{work_str}  "
            f"{main_tok:>{W_MAIN},}  "
            f"{comp_str:>{W_COMP}}"
        )

    print(sep)
    stats = compute_stats(entries)
    totals = stats["totals"]
    total_work = totals["work_tokens"]
    total_main = totals["main_tokens"]
    comp_ratio = totals["compression_ratio"]
    work_total_str = f"{total_work:>{W_WORK},}" if total_work is not None else f"{'—':>{W_WORK}}"
    total_comp_str = f"{comp_ratio}x" if comp_ratio is not None else "—"
    print(
        f"{'TOTAL':<{W_PROJECT}}  "
        f"{'':<{W_TICKET}}  "
        f"{work_total_str}  "
        f"{total_main:>{W_MAIN},}  "
        f"{total_comp_str:>{W_COMP}}"
    )


def _print_full_panels(
    entries: list[dict], sessions: list[dict] | None = None, step_costs: list[dict] | None = None
) -> None:
    """Print the extended --full panels."""
    stats = compute_stats(entries)
    totals = stats["totals"]
    print()

    if step_costs:
        _print_step_cost_panel(step_costs)

    # --- Panel 1: Compression ---
    total_work = totals["work_tokens"]
    total_main = totals["main_tokens"]
    comp_ratio = totals["compression_ratio"]
    kept_out = totals["kept_out"]

    if total_work is not None and total_work > 0:
        print("--- Compression ---")
        print(f"Work done by agents:  {total_work:>10,} tok  (stayed in subagent contexts)")
        print(f"Entered main session: {total_main:>10,} tok  (summary paragraphs only)")
        print(f"Kept out of main ctx: {kept_out:>10,} tok")
        if comp_ratio is not None:
            print(f"Compression ratio:    {comp_ratio:>9}x")
        else:
            print("Compression ratio:             —")
        print()

    # --- Panel 2: Parallelism ---
    if stats["parallelism"]:
        print("--- Parallelism ---")
        for p in stats["parallelism"]:
            if p["gain"] is not None:
                print(
                    f"Batch {p['batch_id']}:  {p['tickets']} tickets, "
                    f"{p['wall_s']}s wall, "
                    f"{p['sum_durations_s']}s sum-of-durations "
                    f"→ {p['gain']}x gain"
                )
        print()

    # --- Panel 3: Cost Trend ---
    trend_points = stats["cost_trend"]["points"]
    trend_verdict_key = stats["cost_trend"]["verdict"]

    if trend_points:
        print("--- Cost Trend (subagent tokens per ticket) ---")
        max_val = max(p["work_tokens"] for p in trend_points)
        BAR_MAX = 20
        for p in trend_points:
            val = p["work_tokens"]
            bar_len = round(val / max_val * BAR_MAX) if max_val else 0
            bar = "█" * bar_len
            print(f"{p['ticket_id']:<10}  {val:>10,}  {bar}")

        if trend_verdict_key is None:
            print("Verdict: not enough data yet (need 5+ tickets)")
        else:
            _TREND_SENTENCES = {
                "FLAT": "FLAT (±20% of mean) — healthy, no context bloat detected.",
                "RISING": "RISING — possible context bloat.",
                "FALLING": "FALLING — agents becoming more efficient.",
            }
            print(f"Verdict: costs are {_TREND_SENTENCES[trend_verdict_key]}")
        print()

    # --- Panel 4: Quality ---
    quality = stats["quality"]
    if quality["tests_total"] > 0:
        pct = round(quality["pass_rate"] * 100) if quality["pass_rate"] is not None else 0
        print("--- Quality ---")
        print(f"Tests passed:   {quality['tests_passed']}/{quality['tests_total']} ({pct}%)")
        print(f"Drift warnings: {quality['drift_warnings']} total")
        print()
    elif quality["drift_warnings"] > 0:
        print("--- Quality ---")
        print(f"Drift warnings: {quality['drift_warnings']} total")
        print()

    # --- Panel 5: Session cost ---
    if sessions:
        print("--- Session Cost (orchestrator turns) ---")
        total_sess = sum(s["session_tokens"] for s in sessions if s.get("session_tokens"))
        W = 24
        for s in sessions[-10:]:  # show last 10 sessions
            tok = s.get("session_tokens")
            tix = ", ".join(s["tickets_merged"]) if s.get("tickets_merged") else "—"
            ts = s.get("timestamp", "")[:10]
            tok_str = f"{tok:>10,}" if tok is not None else f"{'—':>10}"
            print(f"{ts}  {tok_str} tok  [{tix}]")
        if len(sessions) > 1:
            print(f"{'TOTAL':<{W}} {total_sess:>10,} tok across {len(sessions)} sessions")
        print()

    # --- Plain-English Verdict ---
    verdict = stats["verdict"]
    detail = verdict["detail"]
    print(f"Verdict: Delegation is {verdict['label']}" + (f" — {detail}." if detail else "."))


def _print_compare(entries: list[dict]) -> None:
    """Group entries by executor+model and show side-by-side comparison."""
    # Build groups
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in entries:
        key = (e.get("executor", "claude"), e.get("model", ""))
        groups[key].append(e)

    print("=== Executor Comparison ===\n")

    if len(groups) <= 1:
        print("Only one executor in log — run tickets with different executors to compare.")
        return

    W_EX = 18
    W_MODEL = 20
    W_TIX = 7
    W_WORK = 12
    W_WALL = 11
    W_PASS = 9

    header = (
        f"{'executor':<{W_EX}}  "
        f"{'model':<{W_MODEL}}  "
        f"{'tickets':>{W_TIX}}  "
        f"{'avg work tok':>{W_WORK}}  "
        f"{'avg wall ms':>{W_WALL}}  "
        f"{'pass rate':>{W_PASS}}"
    )
    sep = (
        f"{'-' * W_EX}  "
        f"{'-' * W_MODEL}  "
        f"{'-' * W_TIX}  "
        f"{'-' * W_WORK}  "
        f"{'-' * W_WALL}  "
        f"{'-' * W_PASS}"
    )

    print(header)
    print(sep)

    for (executor, model), grp in sorted(groups.items()):
        n = len(grp)

        work_vals = [_get_subagent_tokens(e) for e in grp]
        non_null = [v for v in work_vals if v is not None]
        if non_null:
            avg_work_str = f"{round(sum(non_null) / len(non_null)):>{W_WORK},}"
        else:
            avg_work_str = f"{'—':>{W_WORK}}"

        wall_vals = [int(e.get("wall_time_ms", 0)) for e in grp]
        avg_wall = round(sum(wall_vals) / n) if n else 0
        avg_wall_str = f"{avg_wall:>{W_WALL},}"

        tested = [e.get("tests_passed") for e in grp if e.get("tests_passed") is not None]
        if tested:
            pct = round(sum(1 for tp in tested if tp) / len(tested) * 100)
            pass_str = f"{pct}%"
        else:
            pass_str = "—"

        print(
            f"{executor:<{W_EX}}  "
            f"{model:<{W_MODEL}}  "
            f"{n:>{W_TIX}}  "
            f"{avg_work_str}  "
            f"{avg_wall_str}  "
            f"{pass_str:>{W_PASS}}"
        )
