"""
analyze.py — turn a draft ticket's intent into touches + close_criteria.

This is the ONLY place lanegate itself calls an LLM (F9). Everything else is
deterministic. The model is called via a thin seam (_call_model) so tests can
stub it without a live API call.

Model strategy:
  Calls the configured executor (claude, codex, ollama, aider, …) using the
  same dispatch logic as the implement step.  Falls back to "claude" when no
  executor is set in .lanegate.yml.
"""

from __future__ import annotations

import ast
import datetime
import json
import logging
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

logger = logging.getLogger("lanegate.analyze")

from lanegate import APP_NAME
from lanegate.config import is_high_reasoning_ticket, load_config, resolve_trunk_branch
from lanegate.ticket import (
    TERMINAL_STATUSES,
    canonical_id,
    collect_cross_ticket_change_notes,
    find_control_plane_touch_overlaps,
    load_acceptance_contract_audit,
    load_all_tickets,
    load_change_notes,
    load_file_skeletons,
    validate_acceptance_matrix,
    validate_overlap_review,
    validate_ticket,
    write_file_skeletons_sidecar,
    write_ticket,
)

_CLAUDE_EXECUTORS = frozenset({"claude", "claude-process", "claude-subagent"})
_SESSION_EXECUTORS = frozenset({"claude", "claude-process", "claude-subagent", "agy", "codex", "cursor"})
_CLAUDE_MODEL_PREFIXES = ("claude-",)
_ACTIVE_ANALYSIS_FILE = "analyze-active.json"
_MAX_LOGGED_EXECUTOR_OUTPUT = 8 * 1024
# A transient executor failure is worth surfacing to the current ticket, but
# repeatedly sending later drafts to the same known-bad pool member just burns
# calls. Keep this deliberately small: a second identical failure during one
# orchestrate run takes that member out of rotation via the existing cooldown
# machinery. The tracker is in-memory and keyed by the active run id, so it
# cannot leak a stale failure into a later run.
_ANALYZE_FAILURE_COOLDOWN_THRESHOLD = 2
_ANALYZE_FAILURE_STREAKS: dict[tuple[str, str, str], tuple[str, int]] = {}


def _active_analyze_run_id(repo_root: Path) -> str | None:
    """Return the active orchestrate session id, if analyze runs inside one."""
    from lanegate.orchestrate.run_report import _resolve_active_run_session_ts

    return _resolve_active_run_session_ts(repo_root)


def _analyze_failure_signature(raw_stdout: str, raw_stderr: str) -> str:
    """Normalize volatile executor metadata before comparing failure streaks."""
    text = f"{raw_stdout}\n{raw_stderr}".lower()
    text = re.sub(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        "<uuid>",
        text,
    )
    text = re.sub(
        r"([\"']?(?:session|message|request)[_-]?id[\"']?\s*[:=]\s*)[\"']?[^\s,\"'}]+",
        r"\1<id>",
        text,
    )
    return re.sub(r"\s+", " ", text).strip()


def _record_analyze_failure(
    repo_root: Path, driver_name: str, raw_stdout: str, raw_stderr: str
) -> int | None:
    """Record one non-rate-limit failure and return its run-local streak size."""
    run_id = _active_analyze_run_id(repo_root)
    if run_id is None:
        return None
    key = (str(repo_root.resolve()), run_id, driver_name)
    signature = _analyze_failure_signature(raw_stdout, raw_stderr)
    previous = _ANALYZE_FAILURE_STREAKS.get(key)
    count = previous[1] + 1 if previous and previous[0] == signature else 1
    _ANALYZE_FAILURE_STREAKS[key] = (signature, count)
    return count


def _clear_analyze_failure_streak(repo_root: Path, driver_name: str) -> None:
    """A successful model call makes prior failures for this run irrelevant."""
    run_id = _active_analyze_run_id(repo_root)
    if run_id is not None:
        _ANALYZE_FAILURE_STREAKS.pop((str(repo_root.resolve()), run_id, driver_name), None)

# ---------------------------------------------------------------------------
# Optional tree-sitter import guard
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Model seam — replace in tests via monkeypatch or dependency injection
# ---------------------------------------------------------------------------

























# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


_ACTIVE_CONTROL_PLANE_BUDGET_BYTES = 4000








# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------






_ALREADY_RESOLVED_CITATION_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])(?P<path>(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+|"
    r"[A-Za-z0-9_-]+\.[A-Za-z0-9_.-]+|(?:Makefile|Dockerfile|Containerfile|Rakefile|Gemfile|Procfile))"
    r"\s*:\s*~?L?"
    r"(?P<start>\d+)(?:\s*[-–]\s*~?L?(?P<end>\d+))?"
)




# ---------------------------------------------------------------------------
# Main command
# ---------------------------------------------------------------------------


def _cmd_analyze_core(
    ticket_id: str,
    cfg: dict,
    repo_root: Path,
    *,
    model_fn=None,
    keep_draft: bool = False,
    visibility: _AnalysisVisibility,
    pool_name: str | None = None,
) -> None:
    """Analyze a draft ticket: populate touches + close_criteria.

    Args:
        model_fn: optional override for the model seam (used in tests).
            When provided, called as ``model_fn(prompt)`` — the resolved model
            string is NOT passed (tests supply their own stub logic).
        keep_draft: when True, leave status as draft after populating touches
            (used by `lanegate create` so the user can review before the ticket
            enters the work queue).
        pool_name: name of a `pools:` entry to draw the analyze
            executor from, overriding the ticket's routed/default pool — the
            same override `orchestrate --pool` applies to implement/review/fix.
    """
    from lanegate.config import resolve_model as _resolve_model, validate_model_for_executor

    tid = canonical_id(ticket_id)
    tickets_dir = repo_root / cfg["tickets_dir"]

    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    ticket = next((t for t in tickets if t["id"] == tid), None)
    if ticket is None:
        print(f"ERROR: {tid} not found", file=sys.stderr)
        sys.exit(1)

    if ticket.get("status") not in ("draft", "open"):
        print(
            f"ERROR: {tid} has status '{ticket.get('status')}'; analyze only works on draft or open tickets",
            file=sys.stderr,
        )
        sys.exit(1)

    # Resolve model and executor for the analyze step (only used when model_fn is not provided)
    from lanegate.executor import write_cooldown as _write_executor_cooldown
    from lanegate.orchestrate import (
        _is_rate_limit,
        _rate_limit_reason,
        resolve_pool_executor,
    )
    from lanegate.orchestrate import (
        expand_driver as _expand_driver,
    )
    from lanegate.orchestrate import (
        resolve_driver as _resolve_driver,
    )

    def resolve_analyze_driver(
        excluded: set[str] | None = None, pool_name: str | None = None
    ) -> tuple[str, dict, str, str | None]:
        driver_name = resolve_pool_executor(
            "analyze",
            ticket,
            cfg,
            repo_root,
            pool_name=pool_name,
            excluded=excluded,
            healthy_only=bool(excluded),
        )
        if driver_name is None:
            raise RuntimeError(
                "no healthy pool sibling is available for analyze: every executor in "
                "this ticket's executor pool is cooling down from a prior rate limit "
                "or failure, so none can be dispatched right now; run "
                "`lanegate executor status` to see which instance(s) are cooling down "
                "and when they'll recover, then retry `lanegate analyze <id>`"
            )
        from lanegate.executor import get_executor_config as _get_executor_config

        driver_cfg = _expand_driver(driver_name, cfg)
        executor = driver_cfg.get("type", driver_name)
        effective_cfg = dict(cfg, executor=executor) if executor != cfg.get("executor") else cfg
        analyze_model_override = driver_cfg.get("model")
        model = analyze_model_override or _resolve_model(effective_cfg, "analyze", ticket=ticket)
        if model:
            # `executor` here can be a named `executors:` instance (e.g.
            # "aider-ollama-14b"), not a resolved driver type -- validating
            # against it directly matches no branch in
            # validate_model_for_executor and silently no-ops. Resolve the
            # actual type (and provider) the same way review/pool dispatch
            # do, and validate regardless of whether the model came from a
            # driver-level override or resolve_model's fallback chain.
            resolved_executor_cfg = _get_executor_config(executor, effective_cfg)
            resolved_type = resolved_executor_cfg.get("type", executor)
            validate_model_for_executor(
                model,
                resolved_type,
                "models.analyze",
                provider=resolved_executor_cfg.get("provider") or driver_cfg.get("provider"),
            )
        return driver_name, driver_cfg, executor, model

    analyze_driver_name, analyze_driver_cfg, analyze_executor, analyze_model = resolve_analyze_driver(
        pool_name=pool_name
    )
    visibility.set_driver(analyze_executor, analyze_model)
    implement_driver_name = _resolve_driver("implement", ticket, cfg)
    implement_driver_cfg = _expand_driver(implement_driver_name, cfg)
    implement_executor = implement_driver_cfg.get("type", implement_driver_name)

    if model_fn is not None:

        def call_model(prompt: str) -> tuple[str, str | None]:
            result = model_fn(prompt)
            if isinstance(result, tuple):
                return result
            return result, None
    else:

        def call_model(prompt: str) -> tuple[str, str | None]:
            return _call_model(
                prompt,
                model=analyze_model,
                executor=analyze_executor,
                cfg=cfg,
                driver_cfg=analyze_driver_cfg,
                repo_root=repo_root,
                tid=tid,
            )

    def call_model_with_failover(model_prompt: str) -> tuple[str, str | None]:
        """Call the current analyzer, failing over from rate-limited siblings."""
        nonlocal analyze_driver_name, analyze_driver_cfg, analyze_executor, analyze_model
        max_retries = int(cfg.get("max_sibling_retries", 1))
        excluded: set[str] = set()
        attempts = 0
        while True:
            try:
                response = call_model(model_prompt)
                _clear_analyze_failure_streak(repo_root, analyze_driver_name)
                return response
            except RuntimeError as exc:
                raw_stdout = getattr(exc, "raw_stdout", "")
                raw_stderr = getattr(exc, "raw_stderr", "") or str(exc)
                is_rate_limited = _is_rate_limit(
                    1, repo_root, captured_stdout=raw_stdout, captured_stderr=raw_stderr
                )
                if model_fn is None and not is_rate_limited:
                    failure_count = _record_analyze_failure(
                        repo_root, analyze_driver_name, raw_stdout, raw_stderr
                    )
                    if failure_count == _ANALYZE_FAILURE_COOLDOWN_THRESHOLD:
                        _write_executor_cooldown(
                            repo_root,
                            analyze_driver_name,
                            "analyze executor failed consecutive non-rate-limit calls",
                        )
                elif is_rate_limited:
                    _clear_analyze_failure_streak(repo_root, analyze_driver_name)
                if model_fn is not None or attempts >= max_retries or not is_rate_limited:
                    raise
                reason = _rate_limit_reason(
                    1, repo_root, captured_stdout=raw_stdout, captured_stderr=raw_stderr
                )
                _write_executor_cooldown(repo_root, analyze_driver_name, reason, retry_after=reason)
                excluded.add(analyze_driver_name)
                sibling_name, sibling_cfg, sibling_executor, sibling_model = resolve_analyze_driver(
                    excluded, pool_name=pool_name
                )
                if sibling_name == analyze_driver_name:
                    raise
                attempts += 1
                print(
                    f"[analyze] {tid}: {analyze_driver_name} hit a rate limit — "
                    f"retrying on healthy sibling {sibling_name!r}",
                    file=sys.stderr,
                )
                analyze_driver_name = sibling_name
                analyze_driver_cfg = sibling_cfg
                analyze_executor = sibling_executor
                analyze_model = sibling_model
                visibility.set_driver(analyze_executor, analyze_model)
                visibility.emit(
                    "model_requested",
                    f"Executor: {analyze_executor} Model: {analyze_model or 'default'}",
                )

    # Build prompt and call model. Analyze is read-only, so unlike implement
    # it can safely retry immediately on a healthy pool sibling after a quota
    # error; no in-worktree progress check is relevant here.
    visibility.emit("context_indexed", "Indexing context...")
    prompt = _build_prompt(ticket, repo_root, cfg)
    visibility.emit("prompt_ready", f"Prompt ready ({_estimate_prompt_tokens(prompt)} tokens)")
    visibility.emit(
        "model_requested",
        f"Executor: {analyze_executor} Model: {analyze_model or 'default'}",
    )
    visibility.emit("model_requested", "Waiting for model... (elapsed 0s)")
    waiting_reporter = _WaitingReporter(
        lambda message: visibility.emit("model_requested", message),
        visibility.started_at,
        float(cfg.get("analyze_wait_interval_seconds", 10)),
    )
    waiting_reporter.start()
    try:
        raw, analyze_session_id = call_model_with_failover(prompt)
    except Exception as exc:
        print(f"ERROR: model call failed: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        waiting_reporter.stop()

    visibility.emit("model_responded", f"Model responded ({len(raw)} characters)")
    visibility.executor_output(raw)

    # Parse response
    try:
        result = _parse_response(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: could not parse model response: {exc}", file=sys.stderr)
        print(f"Raw response:\n{raw[:600]}", file=sys.stderr)
        sys.exit(1)

    # A specific already-resolved claim has a higher consequence than an
    # ordinary analysis response.  Verify concrete citations before allowing
    # it to move the ticket to needs_review; a bad claim gets one normal
    # analysis retry rather than being persisted as a plausible-sounding fact.
    fallback_attempted = False
    while bool(result.get("already_resolved")):
        reason = (result.get("already_resolved_reason") or "").strip()
        verified, mismatch = _already_resolved_reason_matches_worktree(reason, repo_root)
        if verified:
            break
        if fallback_attempted:
            print(
                f"ERROR: normal analysis fallback returned an invalid already_resolved verdict: {mismatch}",
                file=sys.stderr,
            )
            sys.exit(1)
        fallback_attempted = True
        if not verified:
            warning = (
                f"WARNING: {tid}: rejected already_resolved verdict because {mismatch}; "
                "requesting a normal analysis pass"
            )
            visibility.emit("already_resolved_rejected", warning)
            print(warning, file=sys.stderr)
            try:
                retry_raw, analyze_session_id = call_model_with_failover(
                    prompt + "\n\nThe prior already_resolved claim did not match the current worktree. "
                    "Provide a normal analysis response with touches and close_criteria; do not return "
                    "already_resolved."
                )
                raw = retry_raw
                visibility.executor_output(raw)
                result = _parse_response(raw)
            except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
                print(f"ERROR: normal analysis fallback failed: {exc}", file=sys.stderr)
                sys.exit(1)

    close_criteria = result.get("close_criteria", "")
    if isinstance(close_criteria, str):
        close_criteria = close_criteria.strip()

    # Restore the ticket's acceptance contract before close_criteria influences
    # any derived state, including already_resolved branches, inferred touches,
    # and companion docs.
    original_criteria = ticket.get("close_criteria", "")
    if _close_criteria_drifted(original_criteria, close_criteria):
        visibility.emit(
            "drift",
            f"WARNING: {tid}: model rewrote close_criteria — restoring original wording",
        )
        print(
            f"WARNING: {tid}: model rewrote close_criteria — restoring original wording",
            file=sys.stderr,
        )
        close_criteria = original_criteria
        result["close_criteria"] = original_criteria

    already_resolved = bool(result.get("already_resolved"))
    already_resolved_reason = (result.get("already_resolved_reason") or "").strip()

    if already_resolved:
        if not already_resolved_reason:
            print(
                "ERROR: model returned already_resolved=true with no reason; ticket left as draft",
                file=sys.stderr,
            )
            sys.exit(1)
        body = ticket.get("_body") or ""
        if body and not body.endswith("\n"):
            body += "\n"
        body += (
            "\n## Needs Review Reason\n"
            "analyze: ticket premise appears already resolved in current code — "
            f"{already_resolved_reason}\n"
        )
        ticket["_body"] = body
        ticket["status"] = "needs_review"
        ticket["review_summary"] = f"already_resolved: {already_resolved_reason}"
        ticket["status_changed_at"] = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        write_ticket(ticket)
        if cfg.get("commit_status_changes", True):
            subprocess.run(
                ["git", "add", "-f", str(ticket["_path"])],
                cwd=repo_root,
                check=False,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-s", "--only", str(ticket["_path"]), "-m", f"chore: {tid} analyzed — flagged already_resolved (needs_review)"],
                cwd=repo_root,
                check=False,
                capture_output=True,
            )
        print(f"{tid}: analyze believes this is already resolved — status set to needs_review", file=sys.stderr)
        print(f"  reason: {already_resolved_reason}", file=sys.stderr)
        print(
            f"  next: verify, then `lanegate supersede {tid} --reason ...` to close, "
            f"or re-run `lanegate analyze {tid}` if the model is wrong",
            file=sys.stderr,
        )
        visibility.emit("analysis_complete", f"Analysis complete for {tid} (already_resolved)")
        sys.exit(0)

    touches = result.get("touches")
    depends_on = result.get("depends_on") or []
    change_notes = result.get("change_notes") or {}
    model = result.get("model")
    acceptance_matrix = result.get("acceptance_matrix")
    overlap_review = result.get("overlap_review")

    if not touches or not isinstance(touches, list):
        print(
            "ERROR: model returned empty or non-list touches; ticket left as draft", file=sys.stderr
        )
        sys.exit(1)
    # NOTE: empty close_criteria is checked *after* the drift guard above so
    # that a model that omits criteria for a ticket with an existing one can
    # have the original restored before we decide whether to hard-fail.

    existing_touches = ticket.get("touches") or []
    merged_touches: list[str] = []
    touches_set: set[str] = set()
    for path in [*existing_touches, *touches]:
        if path not in touches_set:
            merged_touches.append(path)
            touches_set.add(path)

    # The control-plane overlap gate below is evaluated against this
    # snapshot, not the fully-augmented merged_touches: infer_touches_from_
    # criteria and companion_docs_from_criteria (next two blocks) only run
    # *after* this point, using the model's own close_criteria it just
    # returned, so a path either of them adds could never have been shown to
    # or asked about by the model that already responded. Gating on a path
    # the model could not possibly have declared in overlap_review makes the
    # ticket permanently unanalyzable -- every retry regenerates the
    # identical augmented path from the identical close_criteria. Existing
    # touches (carried forward from a prior draft, e.g. re-analysis) ARE
    # known before the prompt is built and are surfaced there instead (see
    # _build_prompt's "existing touches already overlap" section), so they
    # stay in the gate.
    model_visible_touches = list(merged_touches)

    # Augment touches with files statically implied by close_criteria + title.
    # Do NOT include _body: background prose ("lanegate board is broken") would
    # inject false file references for things the fix never touches.
    scan_text = ticket.get("title", "") + " " + _close_criteria_as_str(close_criteria)
    inferred = infer_touches_from_criteria(scan_text, repo_root)
    for path in inferred:
        if path not in touches_set:
            merged_touches.append(path)
            touches_set.add(path)

    # Augment touches with companion docs implied by close_criteria + title.
    companion_docs = companion_docs_from_criteria(scan_text, repo_root, cfg)
    for path in companion_docs:
        if path not in touches_set:
            merged_touches.append(path)
            touches_set.add(path)

    # Drop touches carried forward from an earlier draft that reference a
    # directory since renamed/moved/promoted by a merged ticket —
    # a stale entry only misleads the executor, which ends up implementing
    # under the real current path and getting blocked by the touches-guard
    # for a path analyze never declared.
    basename_corrections = correct_touches_by_basename(merged_touches, repo_root)
    if basename_corrections:
        # Two declared paths can share a basename and correct to the same
        # real file (or a correction can collide with an already-present
        # correct entry) -- dedupe post-correction, not just the pre-
        # correction list, or the ticket ends up with a repeated touch.
        merged_touches = list(
            dict.fromkeys(basename_corrections.get(p, p) for p in merged_touches)
        )
        model_visible_touches = list(
            dict.fromkeys(basename_corrections.get(p, p) for p in model_visible_touches)
        )
        for old, new in basename_corrections.items():
            print(
                f"WARNING: {tid}: corrected touches: '{old}' does not exist; "
                f"using '{new}' (unique basename match in the repo)",
                file=sys.stderr,
            )

    stale_touches = validate_touched_paths(existing_touches, repo_root)
    if stale_touches:
        merged_touches = [p for p in merged_touches if p not in stale_touches]
        model_visible_touches = [p for p in model_visible_touches if p not in stale_touches]
        print(
            f"WARNING: {tid}: dropping stale touches no longer present in repo "
            f"(directory renamed/moved/promoted?): {', '.join(stale_touches)}",
            file=sys.stderr,
        )

    control_plane_overlaps = find_control_plane_touch_overlaps(
        {**ticket, "id": tid, "touches": model_visible_touches}, tickets_dir, cfg, exclude_id=tid
    )
    requires_acceptance_matrix = is_high_reasoning_ticket(ticket)
    if requires_acceptance_matrix:
        matrix_errors = validate_acceptance_matrix(acceptance_matrix, required=True)
        if matrix_errors:
            print(f"ERROR: {'; '.join(matrix_errors)}; ticket left as draft", file=sys.stderr)
            sys.exit(1)
    elif acceptance_matrix is not None and validate_acceptance_matrix(acceptance_matrix):
        print(
            f"WARNING: {tid}: discarding malformed optional acceptance_matrix from analyzer output",
            file=sys.stderr,
        )
        acceptance_matrix = None
    if control_plane_overlaps:
        overlap_ids = {str(item["ticket_id"]) for item in control_plane_overlaps}
        overlap_detail = "; ".join(
            f"{item['ticket_id']} ({', '.join(cast(list, item['paths']))})" for item in control_plane_overlaps
        )
        review_errors = validate_overlap_review(overlap_review)
        if review_errors:
            print(
                f"ERROR: active control-plane overlaps require a plan: {'; '.join(review_errors)} "
                f"— overlapping ticket(s): {overlap_detail}; ticket left as draft",
                file=sys.stderr,
            )
            sys.exit(1)
        assert isinstance(overlap_review, dict)
        declared_ids = set(overlap_review["ticket_ids"])
        if not overlap_ids.issubset(declared_ids):
            missing = overlap_ids - declared_ids
            missing_detail = "; ".join(
                f"{item['ticket_id']} ({', '.join(cast(list, item['paths']))})"
                for item in control_plane_overlaps
                if str(item["ticket_id"]) in missing
            )
            print(
                "ERROR: active control-plane overlaps require a plan: "
                f"overlap_review.ticket_ids is missing: {missing_detail}; ticket left as draft",
                file=sys.stderr,
            )
            sys.exit(1)
        if overlap_review["mode"] == "dependencies" and not overlap_ids.issubset(set(depends_on)):
            print("ERROR: overlap_review dependencies must also appear in depends_on; ticket left as draft", file=sys.stderr)
            sys.exit(1)
        overlap_review = {
            "mode": overlap_review["mode"],
            "ticket_ids": sorted(overlap_ids),
            "paths": {str(item["ticket_id"]): item["paths"] for item in control_plane_overlaps},
        }
    elif overlap_review is not None:
        # An overlap plan is meaningful only when the trusted overlap scan
        # found a real active collision. Never persist model-supplied optional
        # planning metadata in the no-overlap case: it can be malformed,
        # self-referential, or misleading trusted implementation guidance.
        print(
            f"WARNING: {tid}: discarding overlap_review because no active control-plane overlap exists",
            file=sys.stderr,
        )
        overlap_review = None

    # After the drift guard has had the chance to restore an omitted original,
    # fail only if close_criteria is still empty (no prior criteria existed either).
    if not close_criteria:
        print("ERROR: model returned empty close_criteria; ticket left as draft", file=sys.stderr)
        sys.exit(1)

    # Apply to ticket
    ticket["touches"] = merged_touches
    ticket["close_criteria"] = close_criteria
    if acceptance_matrix is not None:
        ticket["acceptance_matrix"] = acceptance_matrix
    if overlap_review is not None:
        ticket["overlap_review"] = overlap_review
    elif not control_plane_overlaps:
        ticket.pop("overlap_review", None)
    file_skeletons = {
        touched: _build_file_skeleton(Path(touched), repo_root) for touched in merged_touches
    }
    file_skeletons_ref = write_file_skeletons_sidecar(ticket, repo_root, file_skeletons)
    if depends_on:
        ticket["depends_on"] = depends_on
    if change_notes:
        ticket["change_notes"] = change_notes
    if analyze_session_id and analyze_executor in _SESSION_EXECUTORS:
        ticket["analyze_session_id"] = analyze_session_id
        ticket["analyze_session_executor"] = analyze_executor
        if analyze_model:
            ticket["analyze_session_model"] = analyze_model
        else:
            ticket.pop("analyze_session_model", None)
    else:
        ticket.pop("analyze_session_id", None)
        ticket.pop("analyze_session_executor", None)
        ticket.pop("analyze_session_model", None)
    audit = audit_acceptance_contract(
        ticket,
        repo_root,
        close_criteria=close_criteria,
        change_notes=change_notes,
    )
    ticket["acceptance_contract_audit"] = audit.as_metadata()
    try:
        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if head_sha:
            ticket["analyzed_at_sha"] = head_sha
    except (subprocess.CalledProcessError, OSError):
        pass
    if not keep_draft:
        ticket["status"] = "open"

    # Validate before writing
    pub = {k: v for k, v in ticket.items() if not k.startswith("_")}
    errors = validate_ticket(pub, cfg)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        print("Ticket left as draft.", file=sys.stderr)
        sys.exit(1)

    write_ticket(ticket)

    if cfg.get("commit_status_changes", True):
        commit_msg = (
            f"chore: {tid} analyzed (touches populated)"
            if keep_draft
            else f"chore: {tid} analyzed — draft → open"
        )
        context_path = repo_root / file_skeletons_ref
        subprocess.run(
            ["git", "add", "-f", str(ticket["_path"])],
            cwd=repo_root,
            check=False,
            capture_output=True,
        )
        subprocess.run(
            ["git", "add", "-f", str(context_path)],
            cwd=repo_root,
            check=False,
            capture_output=True,
        )
        commit_result = subprocess.run(
            ["git", "commit", "-s", "--only", str(ticket["_path"]), str(context_path), "-m", commit_msg],
            cwd=repo_root,
            check=False,
            capture_output=True,
        )
        if commit_result.returncode != 0:
            # Ticket path is force-added above, but if primary commit fails for
            # any other reason (e.g. hook rejection), fall back to committing
            # just the sidecar to prevent it from being left staged.
            subprocess.run(
                ["git", "commit", "-s", "--only", str(context_path), "-m", commit_msg],
                cwd=repo_root,
                check=False,
                capture_output=True,
            )

    if keep_draft:
        print(f"{tid}: touches populated (status: draft — run `lanegate analyze {tid}` to open)")
    else:
        print(f"{tid}: draft → open")
    print(f"  touches: {', '.join(merged_touches)}")
    print(f"  close_criteria: {close_criteria}")
    if not audit.ok:
        print("  acceptance_contract_audit: findings recorded")
    visibility.emit("analysis_complete", f"Analysis complete for {tid}")


def cmd_analyze(
    ticket_id: str,
    cfg: dict,
    repo_root: Path,
    *,
    model_fn=None,
    keep_draft: bool = False,
    pool_name: str | None = None,
) -> None:
    """Analyze a ticket while publishing bounded standalone-analysis progress."""
    visibility = _AnalysisVisibility(repo_root, canonical_id(ticket_id))
    old_sigterm = None
    can_handle_signals = threading.current_thread() is threading.main_thread()

    if can_handle_signals:
        old_sigterm = signal.getsignal(signal.SIGTERM)

        def _interrupt_on_sigterm(_signum, _frame) -> None:
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, _interrupt_on_sigterm)

    try:
        _cmd_analyze_core(
            ticket_id,
            cfg,
            repo_root,
            model_fn=model_fn,
            keep_draft=keep_draft,
            visibility=visibility,
            pool_name=pool_name,
        )
    except KeyboardInterrupt:
        visibility.emit("analysis_failed", "ERROR: analysis interrupted", error=True)
        raise SystemExit(130) from None
    except SystemExit as exc:
        if exc.code:
            visibility.emit("analysis_failed", f"ERROR: analysis failed (exit {exc.code})", error=True)
        raise
    except Exception as exc:
        visibility.emit("analysis_failed", f"ERROR: analysis failed: {exc}", error=True)
        raise SystemExit(1) from exc
    finally:
        if can_handle_signals and old_sigterm is not None:
            signal.signal(signal.SIGTERM, old_sigterm)
        visibility.cleanup()

# Compatibility re-exports for callers and patched private helpers.
from lanegate.analyze_symbols import (  # noqa: E402,F401
    _CONTRACT_VERBS,
    _HAS_TREE_SITTER,
    _MAX_ACCEPTANCE_REF_BYTES,
    _STOPWORDS,
    _TS_LANG_CACHE,
    _TS_LANGUAGE_MAP,
    _ast_symbol_hits,
    _build_ast_index,
    _build_candidate_skeletons,
    _build_file_skeleton,
    _import_graph_expand,
    _is_ignored_analysis_path,
    _index_non_py_file,
    _index_py_file,
    _repo_structure,
    _ripgrep_seed,
    _treesitter_hits,
    companion_docs_from_criteria,
    correct_touches_by_basename,
    enrich_context,
    file_symbols,
    infer_touches_from_criteria,
    register_tree_sitter_languages,
    validate_touched_paths,
)

# Compatibility re-exports for acceptance-contract callers.
from lanegate.acceptance_contract import (  # noqa: E402,F401
    AcceptanceContractAudit,
    VerificationRecord,
    audit_acceptance_contract,
    verify_acceptance_criteria,
    _close_criteria_as_str,
    _close_criteria_drifted,
    _extract_acceptance_checklist,
    _worktree_diff_text,
)

from lanegate.analyze_execution import (  # noqa: E402,F401
    ExecutorCallError,
    _AnalysisVisibility,
    _WaitingReporter,
    _active_analysis_path,
    _bounded_executor_output,
    _call_model,
    _clear_active_analysis,
    _estimate_prompt_tokens,
    _write_active_analysis,
    get_active_analysis_status,
)

from lanegate.analyze_prompt import (  # noqa: E402,F401
    _active_control_plane_ticket_context,
    _already_resolved_reason_matches_worktree,
    _build_prompt,
    _find_json_object,
    _parse_response,
    describe_analyze_payload,
)
