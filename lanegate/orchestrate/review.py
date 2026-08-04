"""Review subagent and review-related daemon helpers.

TICK-255/TICK-277: extracted from orchestrate/__init__.py as pure code
movement -- see docs/internal/module-split-proposal.md.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from lanegate import APP_NAME
from lanegate.config import (
    resolve_acceptance_contract_mode,
    resolve_model,
    resolve_ticket_pool,
)
from lanegate.executor import (
    _CLAUDE_SUBPROCESS_TYPES,
    build_executor_cmd,
    get_executor_config,
    parse_structured_result,
    resolve_executor_env,
)
from lanegate.executor import (
    write_cooldown as _write_executor_cooldown,
)

from .audit import _write_review_verdict
from .pool import (
    _build_env,
    _cfg_with_driver_command_overrides,
    _unpack_stream_result,
    capture_review_step_run,
    expand_driver,
    make_event_line_handler,
    write_prompt_file_best_effort,
)
from .run_report import _resolve_active_run_session_ts, _stream_subprocess


def _extract_review_verdict_json(output: str) -> str | None:
    """Return the last JSON object containing a review verdict from *output*.

    Reviewers commonly surround their verdict with explanatory prose and may
    quote source code containing braces inside ``summary`` or ``findings``.
    A regular expression cannot distinguish those braces from an object
    boundary, so use ``raw_decode`` to let the JSON parser find each complete
    object instead. JSON fenced blocks are preferred because they are an
    explicit reviewer-provided boundary.
    """
    # strict=False tolerates a literal control character (e.g. a raw
    # newline) inside a string value -- smaller/local models routinely
    # hard-wrap a long "summary" this way, which strict mode would
    # otherwise reject even though the object is structurally valid.
    decoder = json.JSONDecoder(strict=False)

    def verdict_objects(text: str) -> list[str]:
        candidates: list[str] = []
        for start, char in enumerate(text):
            if char != "{":
                continue
            try:
                value, end = decoder.raw_decode(text[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and "verdict" in value:
                candidates.append(text[start : start + end])
        return candidates

    fenced = re.findall(r"```json[ \t]*\r?\n(.*?)```", output, re.DOTALL | re.IGNORECASE)
    for block in reversed(fenced):
        candidates = verdict_objects(block)
        if candidates:
            return candidates[-1]

    candidates = verdict_objects(output)
    return candidates[-1] if candidates else None


def spawn_watch_daemon(repo_root: Path) -> None:
    """Spawn `lanegate watch` as a detached background process.

    Uses spawn_detached from lifecycle for platform-agnostic subprocess creation
    (start_new_session=True on Unix; DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP
    on Windows). No nohup, no shell &, no platform-specific shell tricks.
    """
    from lanegate.lifecycle import spawn_detached

    log_path = repo_root / ".lanegate" / "watch.log"
    args = [APP_NAME, "watch"]
    pid = spawn_detached(args, log_path)
    print(f"[orchestrate] spawned {APP_NAME} watch (PID {pid})")


def spawn_resume_watch_daemon(repo_root: Path) -> None:
    """Spawn `lanegate resume-watch` as a detached background process.

    Used when a run halts on a rate limit and on_rate_limit=resume — polls
    with capped backoff until the executor works again, then re-runs
    `lanegate orchestrate` in the background. Mirrors spawn_watch_daemon.
    """
    from lanegate.lifecycle import spawn_detached

    log_path = repo_root / ".lanegate" / "resume-watch.log"
    # Use the installed console-script entry point rather than `-m lanegate`:
    # there is no lanegate/__main__.py, so `python -m lanegate ...` fails.
    args = [APP_NAME, "resume-watch"]
    pid = spawn_detached(args, log_path)
    print(f"[orchestrate] spawned {APP_NAME} resume-watch (PID {pid})")


def _implementer_identity(ticket: dict) -> str | None:
    """Best-effort identity of the executor that implemented this ticket.

    ``ticket["executor"]`` is an explicit per-ticket pin -- the same field
    ``resolve_driver`` reads for the implement/fix steps (pool.py). A
    pool-dispatched ticket with no pin instead carries the pool instance
    that actually ran the implement step in ``implement_session_executor``
    (written by ``invoke_executor`` in pool.py alongside the resumable
    session id). Prefer the pin -- it is what every future implement/fix
    dispatch for this ticket is bound to -- and fall back to the recorded
    dispatch when there is none.
    """
    return ticket.get("executor") or ticket.get("implement_session_executor")


def resolve_independent_review_driver(
    ticket: dict, cfg: dict, repo_root: Path, *, implementer: str | None
) -> tuple[str, str]:
    """Resolve the review driver, excluding the implementer where possible.

    Independence is a quality gate, not an entry requirement: a single-
    account or single-model configuration must never be blocked from
    reviewing. This degrades down a ladder instead, so the audit trail never
    overstates what happened:

      1. ``independent``     -- a different pool instance was selected.
      2. ``different-model`` -- same instance is the only option, but a
         different model resolves for the review step.
      3. ``self``            -- same instance, same model; no alternative
         existed. A warning names the pool and why.

    A per-ticket ``reviewer:`` pin is handled by the caller before this is
    reached; it always wins outright (resolve_pool_executor's own early
    return, loop.py) and is not subject to this ladder.
    """
    from . import resolve_pool_executor

    excluded = {implementer} if implementer else set()
    driver_name = resolve_pool_executor("review", ticket, cfg, repo_root, excluded=excluded)
    assert driver_name is not None
    if not implementer or driver_name != implementer:
        return driver_name, "independent"

    # The only candidate left is the implementer itself -- see whether a
    # different model is at least available for the review step on it.
    driver_cfg = expand_driver(driver_name, cfg)
    review_executor = driver_cfg.get("type", driver_name)
    effective_cfg = (
        dict(cfg, executor=review_executor)
        if review_executor != cfg.get("executor")
        else cfg
    )
    review_model = driver_cfg.get("model") or resolve_model(effective_cfg, "review", ticket=ticket)
    implement_model = (
        ticket.get("implement_session_model")
        or driver_cfg.get("model")
        or resolve_model(effective_cfg, "implement", ticket=ticket)
    )
    if review_model and implement_model and review_model != implement_model:
        return driver_name, "different-model"

    pool_name, _ = resolve_ticket_pool(cfg, ticket)
    print(
        f"WARNING: {ticket.get('id')}: no independent reviewer available in "
        f"pool {pool_name!r} -- {driver_name!r} implemented this ticket and "
        "is also the only reviewer candidate on the same model; proceeding "
        "with a self-review rather than blocking.",
        file=sys.stderr,
    )
    return driver_name, "self"


def run_review_agent(ticket: dict, repo_root: Path, worktree_path: Path | None = None, cfg: dict | None = None) -> bool:
    """
    Run a review subagent for the ticket, or pause for human review when the
    resolved reviewer is ``human``.

    The reviewer runs inside the ticket's git worktree with full git and file
    tool access — same as the implementer — and inspects the branch itself
    (``git diff <trunk>...HEAD``, file reads, etc.) rather than being handed a
    diff embedded in the prompt. ``get_worktree_diff`` below is only a
    pre-flight check (confirms the branch has real commits) before spending
    an LLM call; its return value is no longer forwarded to the prompt.

    Args:
        ticket: The ticket dict.
        repo_root: The repository root path (used for lifecycle calls).
        worktree_path: Path to the ticket's git worktree.  When provided (or
            discoverable via ``ticket["worktree"]``), the diff is extracted from
            that worktree and passed to the reviewer.  If the worktree does not
            exist or the diff is empty, the review is aborted with a
            ``changes_requested`` verdict rather than silently reviewing nothing.
        cfg: Loaded LaneGate config dict.  When None, loads config from repo_root
            (backward compat).

    Returns True if approved, False otherwise.  Fail-closed: a subprocess
    failure, timeout, empty response, or any parse error all return False
    (changes_requested) rather than silently approving.
    """
    from lanegate.reviewer import (
        ReviewError,
        build_review_prompt,
        get_commit_messages,
        get_worktree_diff,
        parse_review_result,
    )
    from lanegate.ticket import branch_name

    from . import _is_rate_limit, _rate_limit_reason, resolve_pool_executor

    tid = ticket["id"]

    if cfg is None:
        try:
            from lanegate.config import load_config as _load_config

            _cfg_for_review = _load_config(repo_root)
        except Exception:
            _cfg_for_review = {}
    else:
        _cfg_for_review = cfg

    implementer = _implementer_identity(ticket)
    pinned_reviewer = ticket.get("reviewer")
    if pinned_reviewer:
        # resolve_pool_executor's own early return (loop.py) hands an explicit
        # per-ticket reviewer pin straight through, never subject to the
        # independence ladder below -- a human decision is not second-guessed.
        # It can still coincide with the implementer, so that case is at least
        # surfaced rather than silently producing an unlabeled self-review.
        review_driver_name = pinned_reviewer
        if implementer and pinned_reviewer == implementer:
            review_independence = "self"
            print(
                f"WARNING: {tid}: reviewer pinned to {pinned_reviewer!r}, the same "
                "executor that implemented this ticket -- a per-ticket reviewer "
                "choice is never overridden, so this will be a self-review.",
                file=sys.stderr,
            )
        else:
            review_independence = "independent"
    else:
        review_driver_name, review_independence = resolve_independent_review_driver(
            ticket, _cfg_for_review, repo_root, implementer=implementer
        )
    review_driver_cfg = expand_driver(review_driver_name, _cfg_for_review)
    review_executor = review_driver_cfg.get("type", review_driver_name)
    if review_executor == "human":
        from lanegate.lifecycle import cmd_review

        ticket_cfg = _minimal_cfg(ticket, repo_root)
        _invoke_cmd_review(
            cmd_review,
            tid,
            ticket_cfg,
            repo_root,
            verdict=None,
            summary="awaiting human review",
            findings=None,
        )
        print(
            f"[orchestrate] {tid}: awaiting human review — run "
            f"`lanegate review {tid} --verdict approved` or request changes",
            file=sys.stderr,
        )
        return False

    # Resolve the worktree path — prefer the explicit argument, then fall back
    # to ticket["worktree"], then to the conventional location.
    if worktree_path is None:
        if ticket.get("worktree"):
            worktree_path = Path(ticket["worktree"])
        else:
            worktrees_dir = _cfg_for_review.get("worktrees_dir", ".lanegate/worktrees")
            worktree_path = repo_root / worktrees_dir / tid.lower()

    # Attribution must survive the paths where no subprocess ever runs: a
    # ticket whose review died on diff extraction still needs to say which
    # reviewer was on the hook, or the frontmatter stays as sparse as it was
    # before this was instrumented.
    review_model = review_driver_cfg.get("model") or "unknown"

    # Extract the diff from the worktree branch.  Abort if unavailable.
    diff = ""
    try:
        branch = ticket.get("branch") or branch_name(tid)
        from lanegate.config import resolve_trunk_branch

        diff = get_worktree_diff(
            worktree_path, branch, base=resolve_trunk_branch(_cfg_for_review, repo_root)
        )
    except ReviewError as exc:
        print(
            f"ERROR: cannot review {tid} — {exc}",
            file=sys.stderr,
        )
        review = _make_error_review(str(exc))
        return _escalate_harness_error(
            ticket,
            review,
            repo_root,
            review_driver_name,
            review_model,
            review_independence,
        )
    except Exception as exc:
        print(
            f"WARNING: diff extraction failed for {tid}: {exc} — routing to needs_review",
            file=sys.stderr,
        )
        review = _make_error_review(str(exc))
        return _escalate_harness_error(
            ticket,
            review,
            repo_root,
            review_driver_name,
            review_model,
            review_independence,
        )

    commit_messages = get_commit_messages(
        worktree_path,
        branch,
        base=resolve_trunk_branch(_cfg_for_review, repo_root),
    )
    prompt = build_review_prompt(
        ticket,
        commit_messages=commit_messages,
        project_root=worktree_path,
        cfg=_cfg_for_review,
    )

    # The prompt is fixed across sibling retries, so it is written once and
    # every retry's run directory copies the same file. Audit I/O is strictly
    # best-effort: review execution itself still receives ``prompt`` directly.
    prompt_path = write_prompt_file_best_effort(worktree_path, tid, "review", prompt)
    session_ts = _resolve_active_run_session_ts(repo_root)
    bundle_path: Path | None = None

    try:
        # A review is read-only. If its assigned pool instance reaches quota,
        # retry it once on a healthy sibling without requiring the implement
        # step's in-worktree-progress heuristic.
        max_retries = int(_cfg_for_review.get("max_sibling_retries", 1))
        # Seed with the implementer so a later rate-limit sibling retry can
        # never fall back onto the instance the independence ladder above
        # specifically avoided (a no-op when independence already fell back
        # to reviewing on the implementer itself -- review_driver_name gets
        # added to this set on its own first failure either way).
        excluded: set[str] = {implementer} if implementer else set()
        attempts = 0
        while True:
            # Resolved inside the try so a bad named-executor config
            # (api_key_env pointing at an unset var, or a type with no known
            # key-injection target — TICK-088), or a malformed driver env
            # overlay, is caught by the same fail-closed handler below.
            review_effective_cfg = (
                dict(_cfg_for_review, executor=review_executor)
                if review_executor != _cfg_for_review.get("executor")
                else _cfg_for_review
            )
            review_model = review_driver_cfg.get("model") or resolve_model(
                review_effective_cfg, "review"
            )
            review_command_cfg = _cfg_with_driver_command_overrides(
                _cfg_for_review, review_executor, review_driver_cfg
            )
            # TICK-310: review stays independent (cold, no --resume) by
            # default -- a reviewer that inherits the implementer's exact
            # reasoning trail undermines the point of an independent check.
            # session_chaining.chain_review is an explicit opt-in for
            # projects that want the cost saving anyway.
            resume_session_id = None
            from lanegate.config import resolve_session_chaining

            if resolve_session_chaining(_cfg_for_review)["chain_review"]:
                resume_candidate = ticket.get("implement_session_id")
                if resume_candidate:
                    from lanegate.context_log import (
                        _get_default_db_path,
                        _get_project_id,
                        resume_session_gate,
                    )

                    allowed, reason = resume_session_gate(
                        _cfg_for_review,
                        _get_default_db_path(),
                        _get_project_id(repo_root),
                        resume_candidate,
                    )
                    if allowed:
                        resume_session_id = resume_candidate
                    else:
                        print(
                            f"[orchestrate] {tid}: not resuming session for review — {reason}",
                            file=sys.stderr,
                        )
            resolved_review_type = get_executor_config(
                review_executor, _cfg_for_review
            ).get("type", review_executor)
            stdin_capable = resolved_review_type in (_CLAUDE_SUBPROCESS_TYPES | {"codex", "ollama"})
            # Agy's JSON mode produces its result at process completion; it
            # is not safe to apply an output-idle watchdog to it.
            # Review does not have the implementation worker's heartbeat
            # monitor. Codex can validly remain silent between JSON events, so
            # give it the hard ceiling rather than applying an output-idle kill.
            streaming_capable = resolved_review_type in _CLAUDE_SUBPROCESS_TYPES
            review_cmd = build_executor_cmd(
                review_executor, prompt, review_command_cfg, model=review_model,
                analyze_session_id=resume_session_id,
                use_stdin=stdin_capable,
            )
            review_executor_env = resolve_executor_env(
                get_executor_config(review_executor, _cfg_for_review)
            )
            review_executor_env = _build_env(review_driver_cfg, base_env=review_executor_env)
            stream_kwargs = {
                "idle_timeout": _cfg_for_review.get("executor_idle_timeout_seconds", 75),
                "absolute_ceiling": _cfg_for_review.get("executor_absolute_ceiling_seconds", 1500),
            } if streaming_capable else {
                "timeout": _cfg_for_review.get("executor_absolute_ceiling_seconds", 1500)
            }
            start_time = time.time()
            # A rate-limit retry may begin in the same wall-clock second as
            # its failed sibling. Nanoseconds keep their audit directories
            # distinct instead of replacing the first attempt's evidence.
            session_id = f"{tid}-{time.time_ns()}-{os.getpid()}-review"
            print(
                f"[review] {tid}: {review_driver_name} ({review_model}) reviewing…",
                file=sys.stderr,
            )
            # Raw stream-json is an unreadable wall of envelopes on a terminal,
            # and it is what an operator running `lanegate review` used to see.
            # Route stdout to a sink and let the event handler print the
            # formatted equivalent; the full text is still captured for the
            # audit bundle by _stream_subprocess's return value.
            handle_line = make_event_line_handler(
                repo_root,
                session_ts,
                tid,
                executor=review_driver_name,
                model=review_model,
                step="review",
                terminal_stream=sys.stderr,
            )
            rc, captured_stdout, captured_stderr, kill_reason = _unpack_stream_result(_stream_subprocess(
                review_cmd,
                cwd=str(worktree_path),
                out_stream=io.StringIO(),
                env=review_executor_env,
                stdin_text=prompt if stdin_capable else None,
                on_line=handle_line,
                **stream_kwargs,
            ))
            bundle_path = capture_review_step_run(
                repo_root,
                worktree_path,
                ticket,
                _cfg_for_review,
                step="review",
                executor=review_executor,
                driver_name=review_driver_name,
                model=review_model,
                session_id=session_id,
                prompt_path=prompt_path,
                start_time=start_time,
                exit_code=rc,
                captured_stdout=captured_stdout,
                captured_stderr=captured_stderr,
            )
            if kill_reason == "ceiling":
                review = _partial_review_from_events(captured_stdout, resolved_review_type)
                break
            if rc == 0:
                # Executors with a registered structured-output parser
                # (Claude, Codex -- see parse_structured_result) reply in
                # their own JSON/JSONL envelope; the reviewer's actual prose
                # (and the embedded verdict JSON below) lives in
                # parsed["result_text"]. Executors with no parser get None
                # and stdout is used as-is, same as before.
                #
                # review_executor here may be a named pool instance (e.g.
                # "claude-a") rather than a bare type -- expand_driver() does
                # not resolve that down to "claude" the way get_executor_config()
                # does, so parse_structured_result must be keyed on the
                # resolved type or it never matches the registry and every
                # named-instance review silently falls back to parsing the
                # raw JSON envelope as if it were plain text.
                parsed = parse_structured_result(resolved_review_type, captured_stdout)
                # An executor can exit 0 while its own envelope reports the run
                # failed -- the harness died after the model emitted verdict-shaped
                # prose, or around it. The verdict text is still sitting in the
                # output, so parsing it records a normal approval for a review that
                # never validly completed. That is fail-open in a pipeline that is
                # fail-closed on every other path, so treat the envelope's own
                # failure report as authoritative over the exit code.
                #
                # is_error is tri-state: True means the run reported failure, False
                # means it reported success, and None means this parser cannot tell
                # (no status field in the envelope). Only an explicit True fails the
                # review -- treating None as failure would fail-close every review
                # from an executor that simply does not report status.
                if parsed is not None and parsed.get("is_error") is True:
                    review = _make_error_review(
                        "Executor reported the run failed despite exit code 0"
                    )
                else:
                    output = (
                        parsed["result_text"].strip()
                        if parsed is not None
                        else captured_stdout.strip()
                    )
                    # Review prose can quote code containing nested braces. Extract
                    # the final structured verdict with JSON's own decoder rather
                    # than treating braces as a regular-language delimiter.
                    raw_for_parse = _extract_review_verdict_json(output)
                    if raw_for_parse is None:
                        review = _make_error_review(
                            "Review completed but no JSON verdict could be extracted"
                        )
                    else:
                        review = parse_review_result(raw_for_parse)
                        if review.notes.startswith("Review parse error:"):
                            review.harness_error = True
                if parsed is not None:
                    from lanegate.context_log import record_step_cost

                    record_step_cost(
                        repo_root, tid, "review", review_executor, review_model, parsed
                    )
                break

            if (
                attempts >= max_retries
                or not _is_rate_limit(
                    rc,
                    worktree_path,
                    captured_stdout=captured_stdout,
                    captured_stderr=captured_stderr,
                )
            ):
                print(
                    f"WARNING: review agent exited {rc} for {tid} — routing to needs_review",
                    file=sys.stderr,
                )
                review = _make_error_review(f"Subprocess exited with code {rc}")
                break

            # This attempt is complete even though a sibling will retry it.
            # Record its own fail-closed result before changing the selected
            # reviewer, so every bundle has a verdict.json.
            _write_review_verdict(
                bundle_path,
                {
                    "verdict": "error",
                    "notes": f"Subprocess exited with code {rc} (rate limited; retrying)",
                    "findings": "",
                    "driver": review_driver_name,
                    "model": review_model,
                    "review_independence": review_independence,
                },
            )

            reason = _rate_limit_reason(
                rc,
                worktree_path,
                captured_stdout=captured_stdout,
                captured_stderr=captured_stderr,
            )
            _write_executor_cooldown(repo_root, review_driver_name, reason, retry_after=reason)
            excluded.add(review_driver_name)
            sibling_name = resolve_pool_executor(
                "review",
                ticket,
                _cfg_for_review,
                repo_root,
                excluded=excluded,
                healthy_only=True,
            )
            if sibling_name is None or sibling_name == review_driver_name:
                print(
                    f"WARNING: review agent exited {rc} for {tid}; no healthy pool sibling is available",
                    file=sys.stderr,
                )
                review = _make_error_review(f"Subprocess exited with code {rc}")
                break
            print(
                f"[orchestrate] {tid}: {review_driver_name} hit a rate limit — "
                f"retrying review on healthy sibling {sibling_name!r}",
                file=sys.stderr,
            )
            review_driver_name = sibling_name
            review_driver_cfg = expand_driver(review_driver_name, _cfg_for_review)
            review_executor = review_driver_cfg.get("type", review_driver_name)
            attempts += 1

    except Exception as exc:
        print(
            f"WARNING: review agent failed for {tid}: {exc} — routing to needs_review",
            file=sys.stderr,
        )
        review = _make_error_review(str(exc))

    if review.harness_error:
        return _escalate_harness_error(
            ticket,
            review,
            repo_root,
            review_driver_name,
            review_model,
            review_independence,
            bundle_path=bundle_path,
        )

    contract_audit = ticket.get("acceptance_contract_audit") or {}
    if (
        review.verdict == "approved"
        and isinstance(contract_audit, dict)
        and contract_audit.get("ok") is False
        and resolve_acceptance_contract_mode(_cfg_for_review) == "blocker"
    ):
        raw_findings = contract_audit.get("findings") or []
        contract_findings = "\n".join(str(f) for f in raw_findings if str(f).strip())
        if contract_findings:
            from lanegate.reviewer import ReviewResult

            review = ReviewResult(
                verdict="changes_requested",
                notes=(
                    f"{review.notes} (overridden: acceptance-contract audit failed)"
                    if review.notes
                    else "acceptance-contract audit failed"
                ),
                findings="\n".join(f for f in (review.findings, contract_findings) if f),
            )

    # One write covering every substantive outcome — approve,
    # changes_requested, or a ceiling-killed partial review — so a run
    # directory always answers "what did this reviewer decide?" without
    # re-parsing the transcript. Harness errors were recorded above as
    # ``error`` before routing the ticket to needs_review.
    _write_review_verdict(
        bundle_path,
        {
            "verdict": review.verdict,
            "notes": review.notes,
            "findings": review.findings,
            "driver": review_driver_name,
            "model": review_model,
            "review_independence": review_independence,
        },
    )

    # Record the review verdict back on the ticket
    from lanegate.lifecycle import cmd_review

    ticket_cfg = _minimal_cfg(ticket, repo_root)
    _invoke_cmd_review(
        cmd_review,
        tid,
        ticket_cfg,
        repo_root,
        verdict=review.verdict,
        summary=review.notes,
        findings=review.findings or None,
        review_driver=review_driver_name,
        review_model=review_model,
        review_independence=review_independence,
    )

    return review.verdict == "approved"


def _invoke_cmd_review(cmd_review, *args, **kwargs) -> None:
    """Call lifecycle.cmd_review, absorbing the SystemExit it raises on a
    changes_requested verdict.
    """
    try:
        cmd_review(*args, **kwargs)
    except TypeError as exc:
        # Fall back gracefully if a test mock has an old signature without
        # review_driver/review_model/review_independence.
        newer_kwargs = ("review_driver", "review_model", "review_independence")
        if any(k in kwargs for k in newer_kwargs):
            clean_kwargs = {k: v for k, v in kwargs.items() if k not in newer_kwargs}
            try:
                cmd_review(*args, **clean_kwargs)
            except SystemExit:
                pass
        else:
            raise exc
    except SystemExit:
        pass


def _make_error_review(reason: str):
    """Return a fail-closed ReviewResult for error conditions."""
    import re

    from lanegate.reviewer import ReviewResult

    clean_reason = reason
    if "Command '['" in reason or 'Command "["' in reason:
        match = re.search(r"timed out after (\d+\s*\w*)", reason)
        if match:
            clean_reason = f"Review command timed out after {match.group(1)}"
        else:
            clean_reason = "Review command execution failed"

    return ReviewResult(
        verdict="changes_requested",
        notes=f"Review error: {clean_reason}",
        harness_error=True,
    )


def _escalate_harness_error(
    ticket: dict,
    review,
    repo_root: Path,
    review_driver_name: str,
    review_model: str,
    review_independence: str,
    *,
    bundle_path: Path | None = None,
) -> bool:
    """Route a failed review harness to human attention, not auto-fix.

    ``changes_requested`` means that a reviewer saw the code and identified
    work to do. A subprocess/configuration failure does not establish that,
    so it must release the ticket's file locks and skip the auto-fix path.
    """
    if bundle_path is not None:
        _write_review_verdict(
            bundle_path,
            {
                "verdict": "error",
                "notes": review.notes,
                "findings": review.findings,
                "driver": review_driver_name,
                "model": review_model,
                "review_independence": review_independence,
            },
        )

    # A failed re-review can otherwise leave a prior substantive rejection on
    # the ticket, which would still trigger the auto-fix workflow. This run
    # did not produce a review verdict at all.
    ticket.pop("review_verdict", None)
    ticket.pop("review_summary", None)
    # Programmatic callers sometimes provide an in-memory ticket without a
    # backing file (for example, a pre-flight API check). There is no status
    # to transition in that case; still fail closed, but do not turn the
    # original harness error into a KeyError from ticket persistence.
    if not ticket.get("_path"):
        return False
    from lanegate.lifecycle import _mark_needs_review

    ticket_cfg = _minimal_cfg(ticket, repo_root)
    _mark_needs_review(
        ticket,
        ticket_cfg,
        repo_root,
        reason=f"Reviewer harness error: {review.notes}",
    )
    return False


def _partial_review_from_events(captured_stdout: str, executor_type: str):
    """Persist useful assistant output from a ceiling-killed streamed review."""
    from lanegate.executor_events import normalize_executor_event
    from lanegate.reviewer import ReviewResult

    texts: list[str] = []
    for line in captured_stdout.splitlines():
        # Normalize each event too: this keeps partial-output handling aligned
        # with the normal streaming event protocol even when a provider's
        # assistant-text envelope varies by version.
        normalize_executor_event(line, executor=executor_type, current_phase="review")
        parsed = parse_structured_result(executor_type, line)
        if parsed and parsed.get("result_text"):
            texts.append(str(parsed["result_text"]))
            continue
        try:
            import json
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item") or {}
        candidates = [
            event.get("text"), event.get("response"),
            item.get("text") if isinstance(item, dict) else None,
        ]
        # Claude stream-json assistant messages carry prose in typed content
        # blocks, not at the event top level.  Preserve only text blocks (not
        # tool inputs) so a ceiling timeout retains an already-emitted review
        # finding just like the Codex ``item.completed`` shape above.
        if event.get("type") == "assistant":
            content = ((event.get("message") or {}).get("content") or [])
            if isinstance(content, list):
                candidates.extend(
                    block.get("text")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
        for text in candidates:
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
    extracted = "\n".join(dict.fromkeys(texts)).strip()
    label = "partial review (ceiling timeout)"
    if extracted:
        return ReviewResult(
            verdict="changes_requested",
            notes=f"{label}: {extracted}",
            findings=extracted,
        )
    return ReviewResult(verdict="changes_requested", notes=label)



def _minimal_cfg(ticket: dict, repo_root: Path) -> dict:
    """Build a minimal config dict from a ticket's path for use in lifecycle calls."""
    from lanegate.config import load_config

    try:
        return load_config(repo_root)
    except Exception:
        # Fallback: infer from ticket path
        tickets_dir = ticket["_path"].parent
        return {
            "ticket_prefix": ticket["id"].split("-")[0],
            "tickets_dir": str(tickets_dir),
            "worktrees_dir": str(tickets_dir.parent / "worktrees"),
            "lock_statuses": ["in_progress", "code_complete", "in_review"],
            "commit_status_changes": True,
            "environments": [],
        }


def _git_head_sha(worktree_path: Path) -> str | None:
    """Return the current HEAD commit sha in worktree_path, or None on failure."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()
