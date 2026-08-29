"""Review subagent and review-related daemon helpers.

Extracted from orchestrate module as pure code
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
from lanegate.safeguards import is_control_plane_file
from lanegate.budget import DispatchMeter, metering_supported_for
from lanegate.config import (
    ConfigError,
    resolve_acceptance_contract_mode,
    resolve_model,
    resolve_ticket_pool,
    should_escalate_review,
    validate_model_for_executor,
)
from lanegate.executor import (
    _CLAUDE_SUBPROCESS_TYPES,
    build_executor_cmd,
    executor_types_with,
    get_executor_config,
    parse_structured_result,
    reject_ollama_for_code_step,
    resolve_executor_env,
)
from lanegate.executor import (
    write_cooldown as _write_executor_cooldown,
)

from .audit import _write_review_verdict
from .pool import (
    _build_env,
    _cfg_with_driver_command_overrides,
    _get_step_budget_cap,
    _main_checkout_leak_diff,
    _unpack_stream_result,
    capture_review_step_run,
    expand_driver,
    make_event_line_handler,
    resolve_driver,
    write_prompt_file_best_effort,
)
from .run_report import (
    _append_run_event,
    _resolve_active_run_session_ts,
    _stream_subprocess,
    suppress_direct_action_tracking,
)


_JSON_KEY_PATTERN = re.compile(r'^"(?:\\.|[^\"])*"\s*:')
_MAX_PERSISTED_FAILURE_SUMMARY = 480


def _executor_failure_summary(reason: str) -> str:
    """Return a bounded, ticket-safe summary of an executor failure.

    Executor CLIs can put a full structured response (including session and
    usage metadata) in stdout/stderr.  That response belongs in the captured
    audit bundle, never in ticket markdown.  Prefer its error type/message;
    retain a short plain-text failure when no structured error is available.
    """
    raw = str(reason or "").strip()
    decoder = json.JSONDecoder()
    parsed_error: tuple[str | None, str | None] | None = None

    for start in (index for index, char in enumerate(raw) if char == "{"):
        try:
            value, _ = decoder.raw_decode(raw[start:])
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        detail = value.get("error")
        if isinstance(detail, str):
            parsed_error = (str(value.get("type") or "error"), detail)
        elif isinstance(detail, dict):
            parsed_error = (
                str(detail.get("type") or detail.get("code") or value.get("type") or "error"),
                str(detail.get("message") or detail.get("detail") or "") or None,
            )
        elif value.get("message") is not None:
            parsed_error = (
                str(value.get("type") or value.get("code") or "error"),
                str(value["message"]),
            )
        if parsed_error is not None:
            break

    if parsed_error is not None:
        error_type, message = parsed_error
        summary = ": ".join(part for part in (error_type, message) if part)
    elif any(key in raw for key in ('"session_id"', '"usage"', '"content"', '"type"')):
        summary = "Executor failed with a structured error response"
    else:
        summary = " ".join(raw.split()) or "Executor failed without an error message"

    if len(summary) > _MAX_PERSISTED_FAILURE_SUMMARY:
        summary = summary[: _MAX_PERSISTED_FAILURE_SUMMARY - 3].rstrip() + "..."
    return summary


def _escape_interior_quotes(text: str) -> str:
    """Escape otherwise-unescaped double quotes inside JSON string values."""
    repaired: list[str] = []
    in_string = False
    string_kind = "VALUE"
    stack: list[str] = []
    index = 0

    while index < len(text):
        char = text[index]

        if in_string:
            if char == "\\" and index + 1 < len(text):
                repaired.append(text[index : index + 2])
                index += 2
                continue
            if char == '"':
                next_index = index + 1
                while next_index < len(text) and text[next_index].isspace():
                    next_index += 1

                rest = text[next_index:]
                is_close = False

                if next_index >= len(text):
                    is_close = True
                elif string_kind == "KEY":
                    if rest.startswith(":"):
                        is_close = True
                else:  # VALUE or OTHER context
                    if rest.startswith("}") or rest.startswith("]"):
                        is_close = True
                    elif rest.startswith(","):
                        after_comma = rest[1:].lstrip()
                        if (
                            after_comma.startswith("}")
                            or after_comma.startswith("]")
                            or _JSON_KEY_PATTERN.match(after_comma)
                        ):
                            is_close = True

                if is_close:
                    in_string = False
                    repaired.append(char)
                else:
                    repaired.append('\\"')
                index += 1
                continue
            else:
                repaired.append(char)
                index += 1
                continue

        if char == '"':
            in_string = True
            if stack and stack[-1] == "OBJECT_KEY":
                string_kind = "KEY"
            else:
                string_kind = "VALUE"
            repaired.append(char)
            index += 1
            continue

        if char == "{":
            stack.append("OBJECT_KEY")
        elif char == "}":
            if stack:
                stack.pop()
        elif char == "[":
            stack.append("ARRAY_VALUE")
        elif char == "]":
            if stack:
                stack.pop()
        elif char == ":":
            if stack and stack[-1] == "OBJECT_KEY":
                stack[-1] = "OBJECT_VALUE"
        elif char == ",":
            if stack and stack[-1] == "OBJECT_VALUE":
                stack[-1] = "OBJECT_KEY"

        repaired.append(char)
        index += 1

    return "".join(repaired)



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
        if '"verdict"' not in text:
            return []
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
        if candidates:
            return candidates

        repaired = _escape_interior_quotes(text)
        if repaired == text:
            return []
        for start, char in enumerate(repaired):
            if char != "{":
                continue
            try:
                value, end = decoder.raw_decode(repaired[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and "verdict" in value:
                candidates.append(json.dumps(value))
        return candidates

    fenced = re.findall(r"```json[ \t]*\r?\n(.*?)```", output, re.DOTALL | re.IGNORECASE)
    for block in reversed(fenced):
        candidates = verdict_objects(block)
        if candidates:
            return candidates[-1]

    candidates = verdict_objects(output)
    return candidates[-1] if candidates else None


def _complete_review_verdict_json(output: str) -> str | None:
    """Return a verdict object only when all required review fields are present.

    An executor can report an error after the reviewer already emitted its
    verdict (for example, when a sandbox rejects a subsequent durable-notes
    write). Recovery is deliberately limited to a complete, valid review
    object so an error envelope or partial model output cannot be mistaken
    for a completed review.
    """
    raw = _extract_review_verdict_json(output)
    if raw is None:
        return None
    try:
        verdict = json.loads(raw, strict=False)
    except json.JSONDecodeError:
        return None
    if not isinstance(verdict, dict):
        return None
    v = verdict.get("verdict")
    if v not in {"approved", "changes_requested", "APPROVE", "REJECT"}:
        return None
    summary = verdict.get("summary", verdict.get("notes"))
    if not isinstance(summary, str) or not summary.strip():
        return None
    findings = verdict.get("findings")
    if findings is not None and not isinstance(findings, (str, list)):
        return None
    return raw


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
    `lanegate run` in the background. Mirrors spawn_watch_daemon.
    """
    from lanegate.lifecycle import spawn_detached

    log_path = repo_root / ".lanegate" / "resume-watch.log"
    # Use the installed console-script entry point rather than `-m lanegate`:
    # there is no lanegate/__main__.py, so `python -m lanegate ...` fails.
    args = [APP_NAME, "resume-watch"]
    pid = spawn_detached(args, log_path)
    print(f"[orchestrate] spawned {APP_NAME} resume-watch (PID {pid})")


_WORKTREE_CONTENT_FIELDS = ("close_criteria", "touches", "_body")


def _refresh_ticket_content_from_worktree(
    ticket: dict, repo_root: Path, worktree_path: Path
) -> None:
    """Overlay content fields from the ticket's own worktree copy onto *ticket*.

    Review is dispatched with the ticket dict loaded from the control
    checkout, but an implementer/auto-fix commit legitimately edits
    close_criteria/touches/body on its own branch -- and that edit doesn't
    reach the control checkout's copy until merge (a scope-narrowing
    commit's close_criteria never reached the control checkout, so the
    reviewer was handed a stale contract and spent most of its turns
    reconciling a contradiction that didn't need to exist). Only content
    fields are overlaid here -- status/review_verdict/reviewed_at/etc. stay
    authoritative from the control checkout, the same split
    ``reconciliation._LIFECYCLE_AUTHORITATIVE_KEYS`` already encodes for
    merge-time conflict resolution. Silently no-ops if the worktree or its
    ticket copy is missing/unparseable -- staleness here is strictly better
    than a review dispatch failure.
    """
    path = ticket.get("_path")
    if not path or not worktree_path.exists():
        return
    try:
        rel = Path(path).relative_to(repo_root)
    except ValueError:
        return
    worktree_ticket_path = worktree_path / rel
    if not worktree_ticket_path.exists():
        return

    from lanegate.ticket import parse_ticket

    worktree_ticket = parse_ticket(worktree_ticket_path)
    if not worktree_ticket:
        return
    for field in _WORKTREE_CONTENT_FIELDS:
        if field in worktree_ticket:
            ticket[field] = worktree_ticket[field]


def _implementer_identity(ticket: dict) -> str | None:
    """Best-effort identity of the executor that implemented this ticket.

    ``ticket["executor"]`` is an explicit per-ticket pin for future
    implement/fix work.  Once implementation has run, however, the recorded
    ``implement_session_executor`` (written by ``invoke_executor`` in
    pool.py) is the historical identity that independence must exclude.  It
    must win over a later routing edit; otherwise a route change can disguise
    the actual implementer and permit a self-review.

    A ticket marked ``implement_mode: manual`` with neither field was completed
    outside executor dispatch, so its implementer is genuinely
    unknown rather than merely not yet dispatched.
    """
    return ticket.get("implement_session_executor") or ticket.get("executor")


def resolve_independent_review_driver(
    ticket: dict,
    cfg: dict,
    repo_root: Path,
    *,
    implementer: str | None,
    pool_name: str | None = None,
) -> tuple[str | None, str]:
    """Resolve the review driver, excluding the implementer where possible.

    The configured fallback makes the deliberate exception when independence
    is impossible.  A cooling-down account is never an exception: it is not a
    candidate for either the initial dispatch or a fallback.

      1. ``independent``     -- a different pool instance was selected.
      2. ``different-model`` -- same instance is the only option, but a
         different model resolves for the review step.
      3. ``self``            -- only when ``review_fallback: same_model``.
      4. ``needs_review``    -- the safe default and the result when a
         ``different_model`` fallback cannot actually resolve a distinct model.

    A per-ticket ``reviewer:`` pin is handled by the caller before this is
    reached; it always wins outright (resolve_pool_executor's own early
    return, loop.py) and is not subject to this ladder.

    ``autofix.resolve_independent_fix_driver`` mirrors rungs 1/2/4 of this
    ladder for the fix step (no ``self`` rung -- a fix step has no config
    equivalent of ``review_fallback``). Keep the two in sync.
    """
    from . import resolve_pool_executor

    excluded = {implementer} if implementer else set()
    driver_name = resolve_pool_executor(
        "review", ticket, cfg, repo_root, excluded=excluded, healthy_only=True, pool_name=pool_name
    )
    if driver_name is not None and driver_name != implementer:
        return driver_name, "independent"
    if not implementer:
        return None, "needs_review"

    # The only possible fallback is the implementer.  Do not use it if its
    # cooldown is active, even when a legacy config has no pools block.
    from lanegate.executor import is_cooling_down
    if is_cooling_down(repo_root, implementer):
        return None, "reviewer_cooling_down"

    driver_name = implementer

    # The only candidate left is the implementer itself -- see whether a
    # different model is at least available for the review step on it.
    driver_cfg = expand_driver(driver_name, cfg)
    review_executor = driver_cfg.get("type", driver_name)
    effective_cfg = (
        dict(cfg, executor=review_executor)
        if review_executor != cfg.get("executor")
        else cfg
    )
    review_model = (
        ticket.get("review_model_pin")
        or driver_cfg.get("model")
        or resolve_model(effective_cfg, "review", ticket=ticket)
    )
    implement_model = (
        ticket.get("implement_session_model")
        or driver_cfg.get("model")
        or resolve_model(effective_cfg, "implement", ticket=ticket)
    )
    fallback = cfg.get("review_fallback", "same_model")
    touches = ticket.get("touches") or []
    touches_control_plane = any(is_control_plane_file(f, cfg) for f in touches)
    if (
        (fallback == "different_model" or "review_fallback" not in cfg or touches_control_plane)
        and review_model and implement_model and review_model != implement_model
    ):
        return driver_name, "different-model"

    if fallback == "needs_review" or fallback == "different_model" or touches_control_plane:
        return None, "needs_review"


    pool_name, _ = resolve_ticket_pool(cfg, ticket)
    print(
        f"WARNING: {ticket.get('id')}: no independent reviewer available in "
        f"pool {pool_name!r} -- {driver_name!r} implemented this ticket and "
        "is also the only healthy reviewer candidate on the same model; "
        "proceeding because review_fallback=same_model.",
        file=sys.stderr,
    )
    return driver_name, "self"


_MAX_REVIEWER_UNAVAILABLE_RETRIES = 3


def _earliest_reviewer_retry(
    repo_root: Path, cfg: dict, ticket: dict, pool_name: str | None
) -> str | None:
    """Earliest cooldown ``until`` among the review pool's own candidates.

    Every candidate can be individually unhealthy (cooling down) while still
    carrying a concrete expiry in its cooldown file -- this is what lets a
    ``reviewer_cooling_down`` resolution record a real retry time instead of
    an arbitrary fixed backoff. The implementer is always included alongside
    the pool's own executors, mirroring the fallback candidate
    ``resolve_independent_review_driver`` itself checks -- a per-ticket
    ``executor:`` pin need not be a member of the routed pool.
    """
    import datetime

    from lanegate.executor import DEFAULT_COOLDOWN_TTL_SECONDS, _parse_iso8601, read_cooldown

    if pool_name is None:
        pool_name, _ = resolve_ticket_pool(cfg, ticket)
    pool_cfg = (cfg.get("pools") or {}).get(pool_name) if pool_name else None
    candidates = list(pool_cfg.get("executors") or []) if isinstance(pool_cfg, dict) else []
    # The implementer is always a fallback candidate for
    # resolve_independent_review_driver (a per-ticket `executor:` pin need
    # not be a pool member), so it must be checked here too even when the
    # routed pool has other, differently-excluded members.
    implementer = _implementer_identity(ticket)
    if implementer and implementer not in candidates:
        candidates.append(implementer)

    # Compare parsed datetimes, not raw strings: `until` may carry a non-UTC
    # offset (parse_retry_after preserves whatever the executor supplied),
    # and lexicographic comparison of differently-offset ISO8601 strings
    # does not agree with chronological order.
    earliest: str | None = None
    earliest_dt = None
    for name in candidates:
        state = read_cooldown(repo_root, name)
        until = state.get("until") if state else None
        if not until:
            continue
        until_dt = _parse_iso8601(until)
        if until_dt is None:
            continue
        if earliest_dt is None or until_dt < earliest_dt:
            earliest_dt = until_dt
            earliest = until
    if earliest is None and candidates:
        # No candidate's cooldown file carried a resolvable `until` (e.g. a
        # legacy/partial cooldown record). A None retry_at reads as
        # "nothing to wait for" and makes the ticket immediately
        # re-eligible, so it gets re-dispatched and re-hibernated on the
        # same scan -- burning the whole retry budget in seconds instead of
        # actually waiting out a cooldown. Fall back to the same
        # default TTL cooldown files themselves use when no retry-after is
        # supplied.
        from lanegate.executor import _utc_now

        earliest = (_utc_now() + datetime.timedelta(seconds=DEFAULT_COOLDOWN_TTL_SECONDS)).isoformat()
    return earliest


def _hibernate_reviewer_unavailable(
    ticket: dict,
    repo_root: Path,
    cfg: dict,
    *,
    reason: str,
    retry_at: str | None,
    driver: str,
    model: str | None,
    independence: str,
) -> bool:
    """Persist a temporary reviewer-cooldown as resumable review-pending work.

    Distinct from ``_hibernate_rate_limited_review``: no subprocess ever ran
    here -- every eligible reviewer was already excluded as cooling down
    before dispatch. The marker written into ``review_pending_reason`` must
    therefore stay disjoint from ticket.py's ``_RATE_LIMIT_MARKER``, so
    resume-watch's genuine rate-limit recovery never picks this case up. A
    ticket that keeps landing here past ``_MAX_REVIEWER_UNAVAILABLE_RETRIES``
    has a pool/config problem no amount of waiting will fix, so it fails
    closed to ``needs_review`` instead of hibernating forever.
    """
    attempt = int(ticket.get("review_retry_attempt") or 0) + 1
    if attempt > _MAX_REVIEWER_UNAVAILABLE_RETRIES:
        return _escalate_no_reviewer(
            ticket, repo_root, cfg, reason=f"{reason} (retry budget exhausted)"
        )

    _write_review_verdict(
        None,
        {
            "verdict": "review_pending",
            "notes": "Review was not performed: independent reviewer temporarily unavailable.",
            "findings": "",
            "driver": driver,
            "model": model,
            "review_independence": independence,
            "fallback_policy": cfg.get("review_fallback", "same_model"),
            "reviewer_unavailable": True,
            "retry_at": retry_at,
            "retry_attempt": attempt,
        },
    )
    if ticket.get("_path"):
        from lanegate.lifecycle import mark_review_pending

        ticket["review_fallback_policy"] = cfg.get("review_fallback", "same_model")
        ticket["review_retry_after"] = retry_at
        ticket["review_retry_attempt"] = attempt
        mark_review_pending(
            ticket,
            cfg,
            repo_root,
            reason=(
                "Independent reviewer temporarily unavailable (cooldown); "
                f"retry after {retry_at}. {reason}"
            ),
        )
    return False


def _hibernate_rate_limited_review(
    ticket: dict,
    repo_root: Path,
    cfg: dict,
    *,
    reason: str,
    bundle_path: Path | None,
    driver: str,
    model: str | None,
    independence: str,
) -> bool:
    """Persist a non-verdict review failure as resumable review-pending work."""
    summary = _executor_failure_summary(reason)
    _write_review_verdict(
        bundle_path,
        {
            "verdict": "review_pending",
            "notes": "Review was not performed: reviewer rate limited.",
            "findings": "",
            "driver": driver,
            "model": model,
            "review_independence": independence,
            "fallback_policy": cfg.get("review_fallback", "same_model"),
            "rate_limited": True,
        },
    )
    if ticket.get("_path"):
        from lanegate.lifecycle import mark_review_pending

        ticket["review_fallback_policy"] = cfg.get("review_fallback", "same_model")
        mark_review_pending(
            ticket,
            cfg,
            repo_root,
            reason=f"Review was not performed: reviewer {driver!r} rate limited. {summary}",
        )
    return False


def _escalate_no_reviewer(ticket: dict, repo_root: Path, cfg: dict, *, reason: str) -> bool:
    """Apply the explicit ``needs_review`` fallback without inventing a verdict."""
    path_str = ticket.get("_path")
    if not path_str:
        return False
    if Path(path_str).exists():
        from lanegate.ticket import parse_ticket

        fresh = parse_ticket(Path(path_str))
        if fresh:
            ticket.clear()
            ticket.update(fresh)

    from lanegate.lifecycle import _mark_needs_review

    ticket.pop("review_verdict", None)
    ticket.pop("review_summary", None)
    ticket.pop("review_findings", None)
    ticket["review_fallback_policy"] = "needs_review"
    _mark_needs_review(ticket, cfg, repo_root, reason=reason)
    return False


def resolve_executor_type_for_driver(name: str, cfg: dict) -> str:
    """Resolve *name* (a ``drivers:`` alias, pool instance name, or bare
    executor type) down to its effective executor type.

    Two-stage lookup: ``expand_driver`` resolves a ``drivers:`` alias to its
    underlying type first (a no-op when *name* is not a ``drivers:`` entry),
    then ``get_executor_config`` resolves that type again against
    ``executors:`` for a named-instance/per-type override. This is the exact
    resolution ``run_review_agent()`` performs inline (see
    ``resolved_review_type`` above) and that ``_review_dispatch_config``
    below needs for its own compatibility check -- kept as a single shared
    function so a payload/analytics audit describing what a real review
    dispatch would look like (``context_log.py``) can resolve the same
    answer instead of re-deriving its own copy of this chain.
    """
    resolved_type = expand_driver(name, cfg).get("type", name)
    return get_executor_config(resolved_type, cfg).get("type", resolved_type)


def resolve_reviewer_driver_and_type(
    ticket: dict, cfg: dict, repo_root: Path, *, pool_name: str | None = None
) -> tuple[str, str]:
    """Best-effort resolution of which reviewer driver/type a real review
    dispatch would pick for *ticket*, for describing prompt shape (e.g. the
    payload/analytics audit in ``context_log.py``) without actually running
    a review.

    Goes through the same pool-aware seam ``run_review_agent()`` uses
    (``resolve_pool_executor`` -> ``resolve_driver``) so a project routed
    through ``pools:`` or ``steps.review.driver`` is described accurately,
    not the bare driver-only resolution. Does not apply the implementer-
    exclusion independence ladder (``resolve_independent_review_driver``):
    that ladder decides which *instance* is safe to review with, which does
    not change the resolved *type* for prompt-shape purposes and would need
    a resolvable implementer identity this audit does not reliably have for
    an arbitrary ticket.
    """
    from . import resolve_pool_executor

    driver_name = resolve_pool_executor("review", ticket, cfg, repo_root, pool_name=pool_name)
    if driver_name is None:
        driver_name = resolve_driver("review", ticket, cfg)
    return driver_name, resolve_executor_type_for_driver(driver_name, cfg)


def _review_dispatch_config(
    control_cfg: dict,
    worktree_cfg: dict,
    review_driver_name: str,
    review_executor: str,
) -> dict:
    """Overlay the one worktree compatibility input permitted for review.

    A self-hosting change can extend the static model registry and select that
    new model in its worktree ``.lanegate.yml``.  That model must be
    usable by the review command or the change can never be reviewed.  The
    worktree remains untrusted, though: its driver selection, command/env
    settings, review fallback, timeouts, budgets, acceptance gates, and prompt
    policy must all stay under control-checkout authority.

    ``load_worktree_config`` has already validated the model value with the
    trusted validator plus only literal AST registry additions.  It cannot,
    however, select the reviewer: a control-checkout pool can choose a
    different named instance after that validation.  Bind the overlay to that
    *actual* trusted review driver, and copy only its review-model leaf.  No
    other worktree configuration reaches dispatch.

    Matching by type alone is not enough: a mixed pool routinely gives the
    implementer and the reviewer instances of the *same* underlying type
    (e.g. two ``claude-process`` entries), and the worktree's own top-level
    ``executor``/``reviewer`` field describes *its* dispatch (typically the
    implementer), not the trusted reviewer's.  If only types were compared,
    the worktree's implementer-scoped model could be copied onto a
    completely different, unrelated reviewer instance that just happens to
    share a type.  So the worktree must also *name* the same selected driver
    that the trusted pool picked before its model is eligible at all.

    ``review_driver_name`` is the driver as the trusted pool actually
    selected it -- a ``drivers:`` alias (e.g. ``trusted-codex``), a pool
    instance name, or a bare type -- *before* ``expand_driver`` resolves an
    alias down to its underlying type.  ``review_executor`` is that resolved
    type, used only to validate compatibility, never for the identity check:
    comparing by the resolved type would let a worktree bind through any
    same-typed sibling of a ``drivers:`` alias (e.g. naming plain ``codex``
    when the trusted route is the alias ``trusted-codex``) without ever
    naming the actual selected driver.
    """
    dispatch_cfg = dict(control_cfg)

    trusted_type = resolve_executor_type_for_driver(review_executor, control_cfg)

    # A top-level worktree model is valid only when the worktree's own review
    # resolution names the exact same driver the trusted pool selected *and*
    # leaves that driver's type unchanged -- same selected driver, compatible
    # executor type, matching the per-executor check below.
    # Match the real dispatcher, including modern ``steps.review.driver`` /
    # ``drivers`` routes.  The legacy resolve_executor() ignores that path,
    # causing a valid worktree model overlay to be rejected before dispatch.
    worktree_review_executor = resolve_driver("review", {}, worktree_cfg)
    if worktree_review_executor != review_driver_name:
        return dispatch_cfg
    if resolve_executor_type_for_driver(worktree_review_executor, worktree_cfg) != trusted_type:
        return dispatch_cfg

    worktree_models = worktree_cfg.get("models")
    if isinstance(worktree_models, dict) and isinstance(worktree_models.get("review"), str):
        models = dict(control_cfg.get("models") or {})
        models["review"] = worktree_models["review"]
        dispatch_cfg["models"] = models

    control_executors = control_cfg.get("executors")
    worktree_executors = worktree_cfg.get("executors")
    if not isinstance(control_executors, dict) or not isinstance(worktree_executors, dict):
        return dispatch_cfg

    # Per-executor models are even more specific: only accept the entry for
    # the exact named instance that the trusted pool selected, and only when
    # the worktree leaves that instance's type unchanged.
    control_executor = control_executors.get(review_executor)
    worktree_executor = worktree_executors.get(review_executor)
    if not isinstance(control_executor, dict) or not isinstance(worktree_executor, dict):
        return dispatch_cfg
    if resolve_executor_type_for_driver(review_executor, worktree_cfg) != trusted_type:
        return dispatch_cfg
    worktree_executor_models = worktree_executor.get("models")
    if not (
        isinstance(worktree_executor_models, dict)
        and isinstance(worktree_executor_models.get("review"), str)
    ):
        return dispatch_cfg

    executors = dict(control_executors)
    executor = dict(control_executor)
    executor_models = dict(control_executor.get("models") or {})
    executor_models["review"] = worktree_executor_models["review"]
    executor["models"] = executor_models
    executors[review_executor] = executor
    dispatch_cfg["executors"] = executors
    return dispatch_cfg


def run_review_agent(
    ticket: dict,
    repo_root: Path,
    worktree_path: Path | None = None,
    cfg: dict | None = None,
    pool_name: str | None = None,
) -> bool:
    """
    Run a review subagent for the ticket, or pause for human review when the
    resolved reviewer is ``human``.

    The reviewer runs inside the ticket's git worktree with full git and file
    tool access — same as the implementer — and inspects the branch itself
    (``git diff <trunk>...HEAD``, file reads, etc.) rather than being handed a
    diff embedded in the prompt. ``get_worktree_diff`` below is primarily a
    pre-flight check (confirms the branch has real commits) before spending
    an LLM call. Its return value is also forwarded into the prompt for a
    non-tool-calling reviewer executor (see ``reviewer.is_non_tool_reviewer`` /
    ``executor.EXECUTOR_CAPABILITIES``), which has no tool-dispatch loop to
    run ``git diff`` itself; a tool-capable reviewer still ignores it and
    reads the branch directly (TICK-644).

    Args:
        ticket: The ticket dict.
        repo_root: The repository root path (used for lifecycle calls).
        worktree_path: Path to the ticket's git worktree.  When provided (or
            discoverable via ``ticket["worktree"]``), the diff is extracted from
            that worktree and passed to the reviewer.  If the worktree does not
            exist or the diff is empty, the review is aborted with a
            ``changes_requested`` verdict rather than silently reviewing nothing.
        cfg: Loaded control-checkout LaneGate config dict. Trusted review
            policy, reviewer/executor selection, and approval gates are
            resolved from it. A validated ticket worktree may contribute only
            its review-model compatibility setting.

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

    # Load root config before resolving the conventional worktree path: a CLI
    # invocation without cfg must still honour a custom worktrees_dir.
    if cfg is None:
        try:
            from lanegate.config import load_config

            cfg = load_config(repo_root)
        except Exception as exc:
            print(
                f"WARNING: could not load review config from {repo_root}: {exc} "
                "— routing to needs_review",
                file=sys.stderr,
            )
            return _escalate_harness_error(
                ticket,
                _make_error_review(f"Root config error: {exc}"),
                repo_root,
                "unavailable",
                "unknown",
                "unavailable",
            )

    # Resolve the worktree path — prefer the explicit argument, then fall back
    # to ticket["worktree"], then to the conventional location.
    if worktree_path is None:
        if ticket.get("worktree"):
            worktree_path = Path(ticket["worktree"])
        else:
            worktrees_dir = cfg.get("worktrees_dir", ".lanegate/worktrees")
            worktree_path = repo_root / worktrees_dir / tid.lower()

    # A rebase conflict leaves the worktree at an intermediate revision, so a
    # review dispatched now could examine only part of the ticket's changes.
    # Check before diff extraction: its failure is otherwise indistinguishable
    # from an ordinary missing/invalid worktree and can mask this state.
    from .loop import is_mid_rebase

    if is_mid_rebase(worktree_path):
        reason = "worktree is mid-rebase; complete or abort the rebase before review"
        print(f"ERROR: cannot review {tid} — {reason}", file=sys.stderr)
        return _escalate_harness_error(
            ticket,
            _make_error_review(reason),
            repo_root,
            "unavailable",
            "unknown",
            "unavailable",
        )

    _refresh_ticket_content_from_worktree(ticket, repo_root, worktree_path)

    # The ticket worktree belongs to the implementing agent. Retain
    # bootstrap compatibility for a newly-added model, but nothing else from
    # its config can influence review policy or trusted prompt inputs.
    worktree_cfg: dict | None = None
    worktree_agy_model_additions: set[str] = set()
    if (worktree_path / ".lanegate.yml").exists():
        try:
            from lanegate.config import _worktree_agy_model_additions, load_worktree_config

            worktree_cfg = load_worktree_config(worktree_path)
            # load_worktree_config already validated worktree_cfg's model
            # value against this same additions set (see its docstring) --
            # re-derive it here so the dispatch-time revalidation below
            # (which the pin-only path never used to run) doesn't reject a
            # self-hosting model addition that already passed once.
            config_module = Path(worktree_path).resolve() / "lanegate" / "config.py"
            if config_module.exists():
                worktree_agy_model_additions = _worktree_agy_model_additions(config_module)
        except Exception as exc:
            print(
                f"WARNING: could not load review compatibility config from {worktree_path}: {exc} "
                "— routing to needs_review",
                file=sys.stderr,
            )
            return _escalate_harness_error(
                ticket,
                _make_error_review(f"Worktree compatibility config error: {exc}"),
                repo_root,
                "unavailable",
                "unknown",
                "unavailable",
            )

    implementer = _implementer_identity(ticket)
    manual_unknown_implementer = ticket.get("implement_mode") == "manual" and not implementer
    pinned_reviewer = ticket.get("reviewer")
    review_retry_at: str | None = None
    if pinned_reviewer:
        # resolve_pool_executor's own early return (loop.py) hands an explicit
        # per-ticket reviewer pin straight through, never subject to the
        # independence ladder below -- a human decision is not second-guessed.
        # It can still coincide with the implementer, so that case is at least
        # surfaced rather than silently producing an unlabeled self-review.
        from lanegate.executor import is_cooling_down, read_cooldown

        review_driver_name = None if is_cooling_down(repo_root, pinned_reviewer) else pinned_reviewer
        if review_driver_name is None:
            review_independence = "reviewer_cooling_down"
            cooldown_state = read_cooldown(repo_root, pinned_reviewer)
            review_retry_at = cooldown_state.get("until") if cooldown_state else None
            print(
                f"WARNING: {tid}: pinned reviewer {pinned_reviewer!r} is cooling down; "
                "review will remain pending rather than spending a failed attempt.",
                file=sys.stderr,
            )
        elif implementer and pinned_reviewer == implementer:
            if any(is_control_plane_file(f, cfg) for f in (ticket.get("touches") or [])):
                return _escalate_no_reviewer(
                    ticket,
                    repo_root,
                    cfg,
                    reason="Control-plane files require independent model review; pinned same-model reviewer is prohibited.",
                )
            review_independence = "self"
            print(
                f"WARNING: {tid}: reviewer pinned to {pinned_reviewer!r}, the same "
                "executor that implemented this ticket -- a per-ticket reviewer "
                "choice is never overridden, so this will be a self-review.",
                file=sys.stderr,
            )
        elif manual_unknown_implementer:
            review_independence = "undetermined"
            print(
                f"WARNING: {tid}: implemented manually with no recorded implementer identity "
                f"-- cannot verify pinned reviewer {pinned_reviewer!r} is independent of "
                "whoever wrote the code.",
                file=sys.stderr,
            )
        else:
            review_independence = "independent"
    else:
        # Rotation is a review-dispatch route, not merely a config helper:
        # select and persist the next reviewer before the normal pool/default
        # route is considered.  The helper also returns a prior assignment for
        # an in_review ticket, so a retry never advances the state mid-review.
        from lanegate.reviewer import resolve_reviewer_rotation

        # resolve_reviewer_rotation excludes the implementer from its
        # candidates, so a returned reviewer is always independent of the
        # code's author (or None, and we fall through to the normal ladder).
        rotated_reviewer = resolve_reviewer_rotation(
            ticket, cfg, repo_root / ".lanegate", implementer=implementer
        )
        if rotated_reviewer:
            review_driver_name = rotated_reviewer
            if manual_unknown_implementer:
                review_independence = "undetermined"
                print(
                    f"WARNING: {tid}: implemented manually with no recorded implementer identity -- "
                    f"cannot verify rotated reviewer {rotated_reviewer!r} is independent of "
                    "whoever wrote the code.",
                    file=sys.stderr,
                )
            else:
                review_independence = "independent"
        elif manual_unknown_implementer:
            review_driver_name = resolve_pool_executor(
                "review", ticket, cfg, repo_root, healthy_only=True, pool_name=pool_name
            )
            review_independence = "undetermined"
            print(
                f"WARNING: {tid}: implemented manually with no recorded implementer identity -- "
                "review independence cannot be verified, proceeding as undetermined rather "
                "than assuming independent.",
                file=sys.stderr,
            )
        else:
            review_driver_name, review_independence = resolve_independent_review_driver(
                ticket, cfg, repo_root, implementer=implementer, pool_name=pool_name
            )
            if review_independence == "reviewer_cooling_down":
                review_retry_at = _earliest_reviewer_retry(repo_root, cfg, ticket, pool_name)

    if any(is_control_plane_file(f, cfg) for f in (ticket.get("touches") or [])) and review_independence in ("self", "undetermined"):
        reason = (
            "Control-plane files require independent model review; same-model review is prohibited."
            if review_independence == "self"
            else "Control-plane files require independent model review; review independence cannot be determined."
        )
        return _escalate_no_reviewer(
            ticket,
            repo_root,
            cfg,
            reason=reason,
        )

    if review_driver_name is None:
        unavailable_reason = (
            "No healthy independent reviewer is available; "
            f"review_fallback={cfg.get('review_fallback', 'same_model')} "
            f"resolved to {review_independence}."
        )
        if review_independence == "needs_review":
            return _escalate_no_reviewer(
                ticket, repo_root, cfg, reason=unavailable_reason
            )
        if review_independence == "reviewer_cooling_down":
            return _hibernate_reviewer_unavailable(
                ticket,
                repo_root,
                cfg,
                reason=unavailable_reason,
                retry_at=review_retry_at,
                driver=pinned_reviewer or implementer or "unavailable",
                model=None,
                independence=review_independence,
            )
        return _hibernate_rate_limited_review(
            ticket,
            repo_root,
            cfg,
            reason=unavailable_reason,
            bundle_path=None,
            driver=implementer or "unavailable",
            model=None,
            independence=review_independence,
        )
    review_driver_cfg = expand_driver(review_driver_name, cfg)
    review_executor = review_driver_cfg.get("type", review_driver_name)
    _cfg_for_review = (
        _review_dispatch_config(cfg, worktree_cfg, review_driver_name, review_executor)
        if worktree_cfg is not None
        else cfg
    )
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
            review_driver=review_driver_name,
            review_independence=review_independence,
        )
        print(
            f"[orchestrate] {tid}: awaiting human review — run "
            f"`lanegate review {tid} --verdict approved` or request changes",
            file=sys.stderr,
        )
        return False

    # Attribution must survive the paths where no subprocess ever runs: a
    # ticket whose review died on diff extraction still needs to say which
    # reviewer was on the hook, or the frontmatter stays as sparse as it was
    # before this was instrumented.
    review_model = (
        ticket.get("review_model_pin")
        or review_driver_cfg.get("model")
        or resolve_model(_cfg_for_review, "review", ticket=ticket)
        or "unknown"
    )

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
    # Resolved once, before the sibling-retry loop below, to build the
    # initial prompt. The loop below re-resolves this per attempt and rebuilds
    # the prompt if a sibling retry changes the resolved type.
    resolved_review_type = resolve_executor_type_for_driver(review_executor, _cfg_for_review)
    prompt = build_review_prompt(
        ticket,
        commit_messages=commit_messages,
        project_root=repo_root,
        worktree_path=worktree_path,
        # Prompt policy is derived from the trusted control-checkout config,
        # never from a ticket worktree.
        cfg=cfg,
        reviewer_type=resolved_review_type,
        diff=diff,
        read_only=True,
    )
    # Type the prompt above was shaped for (TICK-644's tool-capable vs.
    # non-tool-capable instruction text). A sibling rate-limit failover below
    # can reassign review_executor to a sibling of a *different* executor
    # type (e.g. an agy-review pool instance failing over to a codex-review
    # sibling) without otherwise touching the prompt; if that failover ever
    # crosses a tool-capable/non-tool-capable boundary (a hybrid cloud+local
    # review pool), reusing this prompt reproduces the TICK-644 dead-end
    # <tool_call> bug on the retry. Rebuilt below only when the resolved type
    # actually changes, to avoid extra cost on the common same-type retry.
    prompt_built_for_type = resolved_review_type

    # The prompt is fixed across sibling retries that stay on the same
    # resolved executor type, so it is written once here and every same-type
    # retry's run directory copies this file. A retry that crosses executor
    # types rebuilds and rewrites it inside the loop below (see
    # prompt_built_for_type). Audit I/O is strictly best-effort: review
    # execution itself still receives ``prompt`` directly.
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
            # review_executor can still be a named pool instance:
            # expand_driver() only expands `drivers:` entries, so a reviewer
            # configured as `executors: {local-ollama: {type: ollama}}` reaches
            # here as "local-ollama". The guard has to run on the resolved
            # type or that config dispatches a raw ollama review anyway.
            # get_executor_config() alone here is single-stage: config
            # validation rejects a `drivers:` entry whose `type:` names
            # another alias rather than a real executor type (_parse_steps),
            # so review_executor cannot actually be an unresolved alias in
            # practice -- but resolve_executor_type_for_driver() (the same
            # two-stage lookup the initial resolution above and
            # _review_dispatch_config use) costs nothing extra and keeps
            # this the one place in the retry loop that could silently
            # diverge from the others. resolved_review_executor_cfg itself
            # is still needed below for .get("provider").
            resolved_review_executor_cfg = get_executor_config(review_executor, _cfg_for_review)
            resolved_review_type = resolve_executor_type_for_driver(review_executor, _cfg_for_review)
            reject_ollama_for_code_step("review", resolved_review_type)
            if resolved_review_type != prompt_built_for_type:
                prompt = build_review_prompt(
                    ticket,
                    commit_messages=commit_messages,
                    project_root=repo_root,
                    worktree_path=worktree_path,
                    cfg=cfg,
                    reviewer_type=resolved_review_type,
                    diff=diff,
                    read_only=True,
                )
                prompt_path = write_prompt_file_best_effort(worktree_path, tid, "review", prompt)
                prompt_built_for_type = resolved_review_type
            # Resolved inside the try so a bad named-executor config
            # (api_key_env pointing at an unset var, or a type with no known
            # key-injection target), or a malformed driver env
            # overlay, is caught by the same fail-closed handler below.
            review_effective_cfg = (
                dict(_cfg_for_review, executor=review_executor)
                if review_executor != _cfg_for_review.get("executor")
                else _cfg_for_review
            )
            routed_model_pin = ticket.get("review_model_pin")
            review_model = (
                routed_model_pin
                or review_driver_cfg.get("model")
                or resolve_model(review_effective_cfg, "review", ticket=ticket)
            )
            # The escalation *target* is sourced from this resolved
            # executor's own models.review_escalation config (looked up the
            # same way models.review itself is), not a hardcoded model name
            # -- each executor family (Claude/Codex/Agy) has its own model
            # namespace, and an executor with no review_escalation configured
            # simply keeps its normal review model (resolve_model returns
            # None, so the `or` falls through unchanged).
            # An explicit route model pin is an operator choice.  Escalation
            # chooses a stronger default only when no such choice exists.
            if should_escalate_review(ticket) and not routed_model_pin:
                escalation_model = resolve_model(review_effective_cfg, "review_escalation")
                review_model = escalation_model or review_model
            # Pool health can change after `lanegate route` selected and
            # validated a candidate.  Revalidate the resolved model against
            # the executor actually selected for *this* attempt before
            # building a subprocess command; otherwise a sibling retry can
            # cross an executor-family boundary with an incompatible model.
            # This must run regardless of whether the model came from an
            # explicit pin, a drivers-block override, or resolve_model's
            # fallback chain -- a top-level `models.review` authored for the
            # default reviewer leaks into any pool sibling just as easily as
            # a pin does; only checking the pin left that path unvalidated.
            if review_model is not None:
                # A `drivers:` entry can carry `provider` directly on itself
                # rather than on an `executors:` instance. review_executor is
                # already type-collapsed by this point (L803: review_executor
                # = review_driver_cfg.get("type", review_driver_name)), so it
                # can't be used to re-look-up the driver by name here --
                # review_driver_cfg (computed at L802 from the real
                # review_driver_name, before that collapse) is the one that
                # still has it. This only covers the originally-resolved
                # driver, not a sibling `executors:` pool retry mid-loop --
                # that case is already covered by resolved_review_executor_cfg
                # itself, since pool retries reassign among `executors:`
                # instances, not `drivers:` entries.
                resolved_review_provider = (
                    resolved_review_executor_cfg.get("provider")
                    or review_driver_cfg.get("provider")
                )
                validate_model_for_executor(
                    review_model,
                    resolved_review_type,
                    "routed review model" if routed_model_pin else "models.review",
                    provider=resolved_review_provider,
                    agy_model_additions=worktree_agy_model_additions or None,
                )
            review_command_cfg = _cfg_with_driver_command_overrides(
                _cfg_for_review, review_executor, review_driver_cfg
            )
            # review stays independent (cold, no --resume) by
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
            stdin_capable = resolved_review_type in executor_types_with("stdin_capable")
            # Agy's JSON mode produces its result at process completion; it
            # is not safe to apply an output-idle watchdog to it.
            # Review does not have the implementation worker's heartbeat
            # monitor. Codex can validly remain silent between JSON events, so
            # give it the hard ceiling rather than applying an output-idle kill.
            streaming_capable = resolved_review_type in executor_types_with("streaming_capable_without_heartbeat")
            step_max_turns = _get_step_budget_cap(_cfg_for_review, "review", "max_turns")
            step_max_tokens = _get_step_budget_cap(
                _cfg_for_review, "review", "max_cumulative_tokens"
            )
            meter = (
                DispatchMeter(step="review")
                if metering_supported_for(resolved_review_type)
                else None
            )

            def check_budget() -> str | None:
                if meter is None:
                    return None
                if step_max_turns is not None and meter.turns >= step_max_turns:
                    return f"max_turns cap reached ({meter.turns}/{step_max_turns} turns)"
                if step_max_tokens is not None and meter.tokens >= step_max_tokens:
                    return (
                        "max_cumulative_tokens cap reached "
                        f"({meter.tokens}/{step_max_tokens} tokens)"
                    )
                return None

            review_cmd = build_executor_cmd(
                review_executor, prompt, review_command_cfg, model=review_model,
                analyze_session_id=resume_session_id,
                use_stdin=stdin_capable,
                max_turns=step_max_turns,
                disallowed_tools=(
                    ["Bash", "Write", "Edit"]
                    if resolved_review_type in _CLAUDE_SUBPROCESS_TYPES
                    else None
                ),
                read_only=True,
                step="review",
            )
            review_executor_env = resolve_executor_env(
                get_executor_config(review_executor, _cfg_for_review)
            )
            review_executor_env = _build_env(review_driver_cfg, base_env=review_executor_env)
            stream_kwargs = {
                "idle_timeout": _cfg_for_review.get("executor_idle_timeout_seconds", 75),
                "absolute_ceiling": _cfg_for_review.get("executor_absolute_ceiling_seconds", 1500),
                "budget_probe": check_budget,
            } if streaming_capable else {
                "timeout": _cfg_for_review.get("executor_absolute_ceiling_seconds", 1500),
                "budget_probe": check_budget,
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
                meter=meter,
                worktree_path=worktree_path,
            )
            main_checkout_status_before = subprocess.run(
                ["git", "status", "--porcelain", "-uno"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            rc, captured_stdout, captured_stderr, kill_reason = _unpack_stream_result(_stream_subprocess(
                review_cmd,
                cwd=str(worktree_path),
                out_stream=io.StringIO(),
                env=review_executor_env,
                stdin_text=prompt if stdin_capable else None,
                on_line=handle_line,
                **stream_kwargs,
            ))
            if kill_reason != "worktree_violation":
                main_checkout_status_after = subprocess.run(
                    ["git", "status", "--porcelain", "-uno"],
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout
                leaked_diff = _main_checkout_leak_diff(
                    main_checkout_status_before, main_checkout_status_after, cfg, repo_root
                )
                if leaked_diff:
                    rc = 1
                    kill_reason = "main_checkout_violation"
                    msg = (
                        f"[review] {tid}: worktree isolation leak detected: tracked files in "
                        f"the main checkout changed during review:\n{leaked_diff}\n"
                    )
                    captured_stderr += msg
                    print(msg, end="", file=sys.stderr)
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
            # Executors with a registered structured-output parser (Claude,
            # Codex -- see parse_structured_result) reply in their own
            # JSON/JSONL envelope; the reviewer's actual prose (and the
            # embedded verdict JSON below) lives in parsed["result_text"].
            # Executors with no parser get None and stdout is used as-is.
            #
            # review_executor here may be a named pool instance (e.g.
            # "claude-a") rather than a bare type -- expand_driver() does
            # not resolve that down to "claude" the way get_executor_config()
            # does, so parse_structured_result must be keyed on the
            # resolved type or it never matches the registry and every
            # named-instance review silently falls back to parsing the
            # raw JSON envelope as if it were plain text.
            #
            # This runs unconditionally on every attempt -- not only the
            # final rc==0 one -- so a sibling-retried or ceiling-killed
            # attempt that still burned real tokens gets its cost logged
            # too, instead of only ever recording the attempt that happened
            # to succeed.
            parsed = parse_structured_result(resolved_review_type, captured_stdout)
            if parsed is not None:
                from lanegate.context_log import record_step_cost

                record_step_cost(
                    repo_root, tid, "review", review_executor, review_model, parsed,
                    dispatch_start_time=start_time,
                )
            if kill_reason == "ceiling":
                review = _partial_review_from_events(captured_stdout, resolved_review_type)
                break
            if (
                resolved_review_type == "agy"
                and parsed is not None
                and parsed.get("is_error") is True
            ):
                # agy can fail a later sandboxed tool call after emitting a
                # complete verdict in its response field. Preserve that review
                # when (and only when) the response is a complete verdict.
                raw_for_parse = _complete_review_verdict_json(
                    (parsed.get("result_text") or "").strip()
                )
                if raw_for_parse is not None:
                    review = parse_review_result(raw_for_parse)
                    if review.notes.startswith("Review parse error:"):
                        review.harness_error = True
                    break
            if rc == 0:
                # An executor can exit 0 while its own envelope reports the run
                # failed -- the harness died after the model emitted verdict-shaped
                # prose, or around it. The verdict text is still sitting in the
                # output, so parsing it records a normal approval for a review that
                # never validly completed. That is fail-open in a pipeline that is
                # fail-closed on every other path, so treat the envelope's own
                # failure report as authoritative over the exit code.
                if parsed is not None and parsed.get("is_error") is True:
                    review = _make_error_review(
                        "Executor reported the run failed without a complete JSON verdict"
                    )
                    break
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
                    "fallback_policy": _cfg_for_review.get("review_fallback", "same_model"),
                },
            )

            reason = _rate_limit_reason(
                rc,
                worktree_path,
                captured_stdout=captured_stdout,
                captured_stderr=captured_stderr,
            )
            _write_executor_cooldown(repo_root, review_driver_name, reason, retry_after=reason)
            _append_run_event(
                repo_root, session_ts, "executor_cooldown", ticket_id=tid,
                instance=review_driver_name, reason=reason, step="review",
            )
            excluded.add(review_driver_name)
            sibling_name = resolve_pool_executor(
                "review",
                ticket,
                _cfg_for_review,
                repo_root,
                excluded=excluded,
                healthy_only=True,
                pool_name=pool_name,
            )
            if sibling_name is None or sibling_name == review_driver_name:
                print(
                    f"WARNING: review agent exited {rc} for {tid}; no healthy pool sibling is available",
                    file=sys.stderr,
                )
                return _hibernate_rate_limited_review(
                    ticket,
                    repo_root,
                    _cfg_for_review,
                    reason=reason,
                    bundle_path=bundle_path,
                    driver=review_driver_name,
                    model=review_model,
                    independence=review_independence,
                )
            print(
                f"[orchestrate] {tid}: {review_driver_name} hit a rate limit — "
                f"retrying review on healthy sibling {sibling_name!r}",
                file=sys.stderr,
            )
            review_driver_name = sibling_name
            review_driver_cfg = expand_driver(review_driver_name, cfg)
            review_executor = review_driver_cfg.get("type", review_driver_name)
            # Rebind the worktree model overlay to the newly-selected sibling
            # rather than reusing the previous driver's _cfg_for_review: the
            # sibling may be a different executor type entirely (e.g. an
            # `agy-review` route rate-limits and fails over to a
            # `codex-review` pool sibling), and stale `models.review` /
            # `executors.<name>.models.review` from the old overlay must not
            # leak onto it. Rebuild from the untouched control `cfg`, not
            # from `_cfg_for_review`, so no prior overlay can compound.
            _cfg_for_review = (
                _review_dispatch_config(cfg, worktree_cfg, review_driver_name, review_executor)
                if worktree_cfg is not None
                else cfg
            )
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
        isinstance(contract_audit, dict)
        and contract_audit.get("ok") is False
        and resolve_acceptance_contract_mode(_cfg_for_review) == "blocker"
    ):
        raw_findings = contract_audit.get("findings") or []
        contract_findings = "\n".join(str(f) for f in raw_findings if str(f).strip())
        if contract_findings:
            from lanegate.reviewer import ReviewResult

            notes = review.notes
            if review.verdict == "approved":
                notes = f"{notes} (overridden: acceptance-contract audit failed)" if notes else "acceptance-contract audit failed"
            else:
                notes = f"{notes} (also: acceptance-contract audit failed)" if notes else "acceptance-contract audit failed"

            review = ReviewResult(
                verdict="changes_requested",
                notes=notes,
                findings="\n".join(f for f in (review.findings, contract_findings) if f),
            )
            
    if review.verdict == "changes_requested" and review.findings:
        from lanegate.ticket import review_findings_sections
        new_findings = [f.strip() for f in review.findings.split("\n") if f.strip()]
        sections = review_findings_sections(ticket.get("_body", ""))
        
        # We need N=3 consecutive rounds (the current one + 2 past rounds)
        n_required = 3
        
        if len(sections) >= n_required - 1:
            for nf in new_findings:
                consecutive = 1
                for i in range(1, min(n_required, len(sections) + 1)):
                    if nf in sections[-i][1]:
                        consecutive += 1
                    else:
                        break
                if consecutive >= n_required:
                    # Trip the circuit breaker: escalate to human review by simulating a harness error
                    review.harness_error = True
                    review.notes = f"{review.notes} (circuit breaker: identical finding over {n_required} attempts)"
                    break
        
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
            "fallback_policy": _cfg_for_review.get("review_fallback", "same_model"),
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
    changes_requested verdict -- and only that SystemExit.

    cmd_review also exits nonzero when its own code_complete status guard
    rejects the call outright (wrong ticket status, verdict never written).
    Swallowing that case the same way as the normal changes_requested exit
    made a genuinely failed verdict write indistinguishable from an
    ordinary rejection to every caller here -- the reviewer's findings sat
    in the audit bundle but never reached the ticket, and nothing surfaced
    it. Re-raise as a clear error for any exit that was not the expected
    changes_requested one so a status-guard failure fails loudly instead of
    silently vanishing.
    """
    verdict = kwargs.get("verdict")

    def _reraise_if_unexpected(exc: BaseException) -> None:
        if verdict != "changes_requested":
            raise RuntimeError(
                f"cmd_review exited unexpectedly for verdict={verdict!r} "
                "(likely its code_complete status guard rejected the call)"
            ) from exc

    # cmd_review is decorated with lifecycle._track_direct_action, which
    # fabricates a standalone action-*.events.jsonl entry for every call.
    # Every caller reaching cmd_review through this orchestrator-only helper
    # (the loop's own lifecycle steps, and the review agent's verdict write)
    # already appears in that run's own orchestrate-*.events.jsonl, so
    # suppress the redundant per-call tracking here rather than at each call
    # site.
    with suppress_direct_action_tracking():
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
                except SystemExit as exc2:
                    _reraise_if_unexpected(exc2)
            else:
                raise exc
        except SystemExit as exc:
            _reraise_if_unexpected(exc)


def _make_error_review(reason: str):
    """Return a fail-closed ReviewResult for error conditions."""
    import re

    from lanegate.reviewer import ReviewResult

    clean_reason = _executor_failure_summary(reason)
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
    review.notes = _executor_failure_summary(review.notes)
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

    path_str = ticket.get("_path")
    if path_str and Path(path_str).exists():
        from lanegate.ticket import parse_ticket

        fresh = parse_ticket(Path(path_str))
        if fresh:
            ticket.clear()
            ticket.update(fresh)

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
    except ConfigError:
        # A genuine config validation failure (e.g. a malformed
        # executors.aider.model_settings block) must surface, not be masked
        # by a bogus executors-less fallback that silently breaks downstream
        # dispatch/routing decisions.
        raise
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
