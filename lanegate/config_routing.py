"""Pool ordering and configuration resolution helpers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from lanegate.config import (
    CONFIG_FILENAME,
    ConfigError,
    _DEFAULT_ANALYZE_MODEL,
    _DEFAULT_IMPLEMENT_MODEL,
    _DEFAULT_REVIEW_MODEL,
    _DEFAULT_HUMAN_ESCALATION,
    _AUTO_FIX_LANES,
    _HIGH_REASONING_LABELS,
    _HIGH_REASONING_MODEL,
    _HIGH_REASONING_TITLE_RISK_WORDS,
    _HIGH_REASONING_TOPICS,
    find_config,
)


def _is_positive_int(value: Any) -> bool:
    # bool is a subclass of int; reject it explicitly.
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _splice_reordered_flow_list(text: str, seq, new_order: list[str]) -> str | None:
    """Rewrite a `[a, b, c]` flow-style sequence in place in *text*, reusing
    each item's original token text (quoting/spacing) and touching nothing
    else in the file. Returns None if the source doesn't look like the
    simple single-bracket case this handles.
    """
    val_line, val_col = seq.lc.line, seq.lc.col
    lines = text.splitlines(keepends=True)
    if val_line >= len(lines):
        return None
    offset = sum(len(line) for line in lines[:val_line]) + val_col
    if text[offset] != "[":
        return None
    close = text.find("]", offset)
    if close == -1:
        return None
    tokens = text[offset + 1 : close].split(",")
    if len(tokens) != len(seq):
        return None
    # Strip each token's separator whitespace (it belongs to the ", " between
    # items, not to the item itself) but keep any quoting the item has.
    token_by_value = {v: tok.strip() for v, tok in zip(seq, tokens)}
    if token_by_value.keys() != set(new_order) or len(token_by_value) != len(new_order):
        return None
    new_inner = ", ".join(token_by_value[v] for v in new_order)
    return text[: offset + 1] + new_inner + text[close:]


def _splice_reordered_block_list(text: str, seq, new_order: list[str]) -> str | None:
    """Rewrite a block-style (`- item` per line) sequence in place in *text*,
    reusing each item's original full source line (indentation, quoting, any
    trailing per-item comment) and touching nothing else in the file. Returns
    None if the source doesn't look like the simple one-item-per-line case
    this handles.
    """
    lines = text.splitlines(keepends=True)
    try:
        item_lines = [seq.lc.item(i)[0] for i in range(len(seq))]
    except Exception:
        return None
    if item_lines != sorted(item_lines) or len(set(item_lines)) != len(item_lines):
        return None
    if item_lines[-1] >= len(lines):
        return None
    line_by_value = dict(zip(seq, (lines[i] for i in item_lines)))
    if line_by_value.keys() != set(new_order) or len(line_by_value) != len(new_order):
        return None
    new_block = [line_by_value[v] for v in new_order]
    return "".join(lines[: item_lines[0]] + new_block + lines[item_lines[-1] + 1 :])


def update_pool_executor_order(repo_root: Path, pool_name: str, executors: list[str]) -> dict:
    """Persist a reordered `pools.<pool_name>.executors` list back to
    .lanegate.yml, so a TUI reorder control can change which
    instance least-loaded prefers on ties and where round-robin starts,
    without hand-editing the config file.

    Rewrites only the source lines/tokens spanning that one list, reusing
    each item's original text verbatim and reassembling them in the new
    order — every other line in the file, including comments and unrelated
    formatting, is left byte-for-byte untouched. (A prior version round-
    tripped the whole file through PyYAML's safe_load/dump, which has no
    concept of comments and silently stripped every one in the file on any
    reorder.) Falls back to a ruamel.yaml round-trip dump — which preserves
    comments but may reflow unrelated formatting — only if the file's
    structure doesn't match the simple single-bracket-or-one-per-line shapes
    the targeted splice handles.

    Raises ConfigError if the pool doesn't exist or *executors* isn't a
    reordering of its current executor set — this endpoint changes
    preference order only, not pool membership.
    """
    from ruamel.yaml import YAML

    config_path = find_config(repo_root)
    if config_path is None:
        raise ConfigError(f"no {CONFIG_FILENAME} found under {repo_root}")
    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    text = config_path.read_text(encoding="utf-8")
    raw = yaml_rt.load(text) or {}
    pools = raw.get("pools")
    if not isinstance(pools, dict) or pool_name not in pools:
        raise ConfigError(f"pool {pool_name!r} is not defined in pools:")
    pool = pools[pool_name]
    current = pool.get("executors") or []
    if sorted(executors) != sorted(current):
        raise ConfigError(
            f"executors for pool {pool_name!r} must be a reordering of "
            f"{current!r}, got {executors!r}"
        )

    seq = pool.get("executors")
    new_text = None
    if hasattr(seq, "fa"):
        if seq.fa.flow_style():
            new_text = _splice_reordered_flow_list(text, seq, executors)
        elif seq.fa.flow_style() is False:
            new_text = _splice_reordered_block_list(text, seq, executors)

    if new_text is None:
        # Fallback: full round-trip dump. Still comment-preserving, unlike
        # the plain-PyYAML approach this replaces, but may reflow formatting
        # elsewhere in the file.
        if hasattr(seq, "clear") and hasattr(seq, "extend"):
            seq.clear()
            seq.extend(executors)
        else:
            pool["executors"] = list(executors)
        import io

        buf = io.StringIO()
        yaml_rt.dump(raw, buf)
        new_text = buf.getvalue()

    config_path.write_text(new_text, encoding="utf-8")
    return {
        "name": pool_name,
        "strategy": pool.get("strategy", "least-loaded"),
        "executors": list(executors),
    }


def _describe_routing_when(when: dict) -> str:
    parts = []
    if "complexity_min" in when:
        parts.append(f"complexity>={when['complexity_min']}")
    if "complexity_max" in when:
        parts.append(f"complexity<={when['complexity_max']}")
    if "touches_min" in when:
        parts.append(f"touches>={when['touches_min']}")
    if "touches_max" in when:
        parts.append(f"touches<={when['touches_max']}")
    if "priority_min" in when:
        parts.append(f"priority>={when['priority_min']}")
    if "priority_max" in when:
        parts.append(f"priority<={when['priority_max']}")
    if "label" in when:
        parts.append(f"label={when['label']!r}")
    return ", ".join(parts) if parts else "always"


def _ticket_matches_routing_when(ticket: dict, when: dict) -> bool:
    complexity = ticket.get("complexity")
    if "complexity_min" in when and (complexity is None or complexity < when["complexity_min"]):
        return False
    if "complexity_max" in when and (complexity is None or complexity > when["complexity_max"]):
        return False

    touches_count = len(ticket.get("touches") or [])
    if "touches_min" in when and touches_count < when["touches_min"]:
        return False
    if "touches_max" in when and touches_count > when["touches_max"]:
        return False

    priority = ticket.get("priority")
    if "priority_min" in when and (priority is None or priority < when["priority_min"]):
        return False
    if "priority_max" in when and (priority is None or priority > when["priority_max"]):
        return False

    if "label" in when and when["label"] not in (ticket.get("labels") or []):
        return False

    return True


def resolve_ticket_pool(cfg: dict, ticket: dict) -> tuple[str | None, str]:
    """Resolve which `pools:` entry a ticket routes to.

    Rules under `routing:` are evaluated top-to-bottom; the first whose
    `when` filters all match the ticket wins. A ticket missing a filter's
    field (e.g. no `complexity` score because it hasn't been analyzed) never
    matches that filter, so unanalyzed tickets naturally fall through to
    `default_pool`. Returns (pool_name, reason) — pool_name is None when no
    rule matched and no `default_pool` is configured (unrouted).
    """
    routing = cfg.get("routing") or []
    for i, rule in enumerate(routing):
        when = rule.get("when") or {}
        if _ticket_matches_routing_when(ticket, when):
            return rule["executor_pool"], f"routing[{i}] matched ({_describe_routing_when(when)})"

    default_pool = cfg.get("default_pool")
    if default_pool:
        return default_pool, "no routing rule matched — using default_pool"
    return None, "no routing rule matched and no default_pool configured — unrouted"


def resolve_max_parallel_detail(cfg: dict, override: int | None = None) -> dict[str, Any]:
    """
    Effective concurrency cap details (the resource gate). Precedence, first hit wins:
      1. explicit override (e.g. orchestrator --max N)
      2. executors[<active executor>].max_parallel
      3. sum(executors[<instance>].max_parallel for instance in pools[default_pool])
         — a bare `executor:` value that doesn't match any named
         pool instance (e.g. executor: claude with only claude-a/claude-b
         defined) previously fell straight through to the top-level/default
         value, ignoring every per-instance cap in the pool actually serving
         dispatch. The pool's total capacity is summed rather than taking the
         weakest instance's cap: least-loaded routing plus each instance's own
         max_parallel (enforced independently by _has_capacity/resolve_pool_executor
         in orchestrate/loop.py) already prevent any single instance from being
         overloaded, so the batch admission gate should reflect real total
         capacity, not be throttled to the slowest/lowest-capacity member.
         If any instance in the pool omits max_parallel, the pool is treated
         as unbounded overall (an uncapped instance has unbounded capacity,
         so the sum would be unbounded too) and falls through to case 4/5
         rather than summing only the capped subset.
      4. top-level max_parallel
      5. built-in default (2)
    Returns a small audit record with the resolved value and source.
    """
    if override is not None:
        if not _is_positive_int(override):
            raise ValueError(f"max_parallel override must be a positive integer, got {override!r}")
        return {"value": override, "source": "cli override", "override": override}

    executor = cfg.get("executor")
    executors = cfg.get("executors") or {}
    ex = executors.get(executor) or {}
    if "max_parallel" in ex:
        detail: dict[str, Any] = {
            "value": ex["max_parallel"],
            "source": "default executor override",
            "default_executor": executor,
            "config_key": f"executors['{executor}'].max_parallel",
        }
        if "max_parallel" in cfg:
            detail["overrides"] = {
                "source": "global config",
                "value": cfg["max_parallel"],
                "config_key": "max_parallel",
            }
        return detail

    default_pool = cfg.get("default_pool")
    pool_cfg = (cfg.get("pools") or {}).get(default_pool) if default_pool else None
    if pool_cfg:
        instance_caps = [
            executors.get(inst, {}).get("max_parallel")
            for inst in pool_cfg.get("executors", [])
        ]
        capped = [c for c in instance_caps if c is not None]
        if capped and len(capped) == len(instance_caps):
            pool_detail: dict[str, Any] = {
                "value": sum(capped),
                "source": "pool instance cap (sum)",
                "pool": default_pool,
                "config_key": f"pools['{default_pool}'].executors[*].max_parallel",
            }
            if "max_parallel" in cfg:
                pool_detail["overrides"] = {
                    "source": "global config",
                    "value": cfg["max_parallel"],
                    "config_key": "max_parallel",
                }
            return pool_detail

    if "max_parallel" in cfg:
        return {
            "value": cfg["max_parallel"],
            "source": "global config",
            "config_key": "max_parallel",
        }

    return {"value": 2, "source": "built-in default"}


def resolve_max_parallel(cfg: dict, override: int | None = None) -> int:
    """
    Effective concurrency cap (the resource gate). Precedence, first hit wins:
      1. explicit override (e.g. orchestrator --max N)
      2. executors[<active executor>].max_parallel
      3. top-level max_parallel
      4. built-in default (2)
    The orchestrator pairs this with the correctness gate from `lanegate next`:
    effective_batch = min(disjoint_candidates, resolve_max_parallel(cfg)).
    """
    return int(resolve_max_parallel_detail(cfg, override=override)["value"])


def is_high_reasoning_ticket(ticket: dict | None) -> bool:
    """Whether a ticket needs the high-reasoning control-plane default."""
    if not isinstance(ticket, dict):
        return False
    labels = ticket.get("labels") or []
    if any(str(label).strip().lower() in _HIGH_REASONING_LABELS for label in labels):
        return True

    # Do not inspect close_criteria: it is model-generated during analysis,
    # and cannot safely change the required response shape after dispatch.
    title_words = set(re.findall(r"[a-z]+", str(ticket.get("title") or "").lower()))
    has_topic = any(
        all(word in title_words for word in topic.replace("-", " ").split())
        for topic in _HIGH_REASONING_TOPICS
    )
    return has_topic and any(word in title_words for word in _HIGH_REASONING_TITLE_RISK_WORDS)


def should_escalate_review(ticket: dict | None) -> bool:
    """Whether a ticket's review should escalate past its configured default.

    True when either:
      - it is a high-reasoning ticket per ``is_high_reasoning_ticket`` (known
        risky topic, decided at analysis time, independent of any verdict), or
      - it already has ``review_verdict == "changes_requested"`` from a prior
        round -- a ticket that has already proven non-trivial enough to fail
        review once gets the stronger reviewer for the remaining round(s).

    Deliberately executor-agnostic: it says *whether* to escalate, not *to
    what*. The target model is whatever the resolved executor's own
    ``models.review_escalation`` config says (resolved the same way as
    ``models.review`` itself, via ``resolve_model(cfg, "review_escalation",
    ...)``) -- every executor family (Claude, Codex, Agy/Gemini, ...) has its
    own model namespace, so there is no single cross-executor "the stronger
    model" constant to fall back on here.
    """
    if is_high_reasoning_ticket(ticket):
        return True
    return isinstance(ticket, dict) and ticket.get("review_verdict") == "changes_requested"


def resolve_model(cfg: dict, step: str, ticket: dict | None = None) -> str | None:
    """
    Resolve the effective model for a given pipeline step.

    Resolution order (first hit wins):
      1. ticket.model field (implement/fix only) or ticket.review_model_pin field
         (review only — passed via ticket dict)
      2. executors[<active executor>].models.<step>
      3. top-level models.<step>
      4. For a Claude-compatible executor on a high-reasoning ticket
         (analyze/implement/review only): the fixed high-reasoning model,
         regardless of step-default configuration below.
      5. A Claude-compatible executor's built-in per-step default; any other
         executor type gets None (its own CLI default).

    The caller may use the returned value to inject ``--model <model>`` (or
    the appropriate flag) into the executor command.  A return value of None
    means "no model flag — let the executor use its own default."
    """
    # 1. Per-ticket model overrides are step-specific. ``review_model`` is
    # review attribution written by cmd_review; only review_model_pin is an
    # explicit operator route choice.
    if ticket:
        if step in {"implement", "fix"} and ticket.get("model"):
            return ticket["model"]
        if step == "review" and ticket.get("review_model_pin"):
            return ticket["review_model_pin"]

    active_executor = cfg.get("executor", "claude")

    # 2. Per-executor model override for this step
    ex_cfg = (cfg.get("executors") or {}).get(active_executor) or {}
    if isinstance(ex_cfg, dict):
        ex_models = ex_cfg.get("models") or {}
        if step in ex_models:
            return ex_models[step]
        # A named `executors:` instance may carry a single blanket `model`
        # field (documented shape, e.g. `local-1: {type: aider, model: ...}`)
        # instead of a step-keyed `models:` block. Without this fallback the
        # instance falls through to the top-level `models:` block below,
        # which is authored for the default executor and can leak a
        # cross-vendor model name into this instance.
        if ex_cfg.get("model"):
            return ex_cfg["model"]

    # 3. Top-level models block
    top_models = cfg.get("models") or {}
    if step in top_models:
        return top_models[step]

    # 4. No model configured. Claude-compatible executors keep the built-in
    # defaults; other executors should use their own CLI default instead of
    # receiving a Claude model name they may not support.
    #
    # active_executor may be a named instance (e.g. "claude-a") whose
    # own name is never literally "claude"/"claude-process"/"claude-subagent" —
    # check its *type* (from executors[<name>].type, falling back to the name
    # itself for a bare type or a legacy no-type override entry) rather than
    # the name string directly. Without this, every named instance of a
    # Claude-compatible type falls through with no --model flag at all,
    # silently deferring to whatever model the underlying CLI happens to
    # default to (which may not be a cheap one) instead of the safe defaults
    # below — this is exactly what let an interactively-set expensive model
    # leak into an unattended orchestrate run with no config asking for it.
    if isinstance(ex_cfg, dict) and ex_cfg.get("type") is not None:
        effective_type = ex_cfg["type"]
    else:
        effective_type = active_executor
    if effective_type not in ("claude", "claude-process", "claude-subagent"):
        return None

    if is_high_reasoning_ticket(ticket) and step in {"analyze", "implement", "review"}:
        return _HIGH_REASONING_MODEL

    _step_defaults = {
        "analyze": _DEFAULT_ANALYZE_MODEL,
        "implement": _DEFAULT_IMPLEMENT_MODEL,
        "review": _DEFAULT_REVIEW_MODEL,
    }
    return _step_defaults.get(step)


def resolve_executor(cfg: dict, step: str, ticket: dict | None = None) -> str:
    """
    Resolve the effective executor for a given pipeline step.

    Resolution order (first hit wins):
      1. ticket.executor field (implement step only — passed via ticket dict)
      2. ticket.reviewer field (review step only)
      3. top-level reviewer setting (review step only, including "human")
      4. executor_steps.<step> in config
      5. global executor (defaults to "claude")

    Args:
        cfg: loaded config dict
        step: pipeline step — "implement" or "review"
        ticket: ticket dict; used only for step=="implement" to check ticket.executor

    Returns the executor name string (always a non-None value).
    """
    # 1. Per-ticket executor override (implement step only)
    if step == "implement" and ticket and ticket.get("executor"):
        return ticket["executor"]
    if step == "review":
        if ticket and ticket.get("reviewer"):
            return ticket["reviewer"]
        if cfg.get("reviewer"):
            return cfg["reviewer"]
    # 2. Per-step executor from executor_steps block
    steps = cfg.get("executor_steps") or {}
    if step in steps:
        return steps[step]
    # 3. Global executor default
    return cfg.get("executor", "claude")


def resolve_executor_route(cfg: dict, ticket: dict | None = None) -> dict[str, str]:
    """Resolve implement/review executors and the resulting execution mode.

    ``ticket.executor`` only affects implementation routing. Review routing is
    controlled by ``ticket.reviewer``, top-level ``reviewer``, then
    ``executor_steps.review``. When both steps resolve to the same executor,
    LaneGate can use combined mode; otherwise it uses split mode.
    """
    implement = resolve_executor(cfg, "implement", ticket)
    review = resolve_executor(cfg, "review", ticket)
    return {
        "implement": implement,
        "review": review,
        "mode": "combined" if implement == review else "split",
    }


def resolve_autonomy(cfg: dict, ticket: dict | None = None) -> str:
    """
    Resolve the effective autonomy level for a ticket.

    Resolution order (first hit wins):
      1. ticket.autonomy field
      2. top-level autonomy in config
      3. "supervised" (default — fix -> drift-check -> re-review always runs
         on changes_requested regardless of autonomy; "supervised" and
         "manual" both pause the approved result for a human merge decision
         instead of merging unattended)

    Returns one of "full", "supervised", "manual", or the risk-based
    autonomy lanes "green", "yellow", "red". "green" and "yellow"
    behave like "full" for the automatic fix/merge gates (see
    ``is_auto_fix_lane``); "red" always requires human review (see
    ``is_red_lane``), and a red-lane risk signal detected in a change's diff
    (``lanegate.orchestrate.guards.scan_risk_lane``) can force escalation
    even when the resolved autonomy is "full"/"green"/"yellow" — the risk
    lane is a safety override on top of configured autonomy, not a
    replacement for it.
    """
    if ticket and ticket.get("autonomy"):
        return ticket["autonomy"]
    if cfg.get("autonomy"):
        return cfg["autonomy"]
    return "supervised"


def resolve_human_escalation(cfg: dict) -> dict:
    """
    Resolve human-escalation triggers for risk-based autonomy lanes.

    Merges project overrides in ``cfg["human_escalation"]`` onto the
    defaults below. When a trigger is enabled, detecting it forces a
    red-lane escalation to a human regardless of the ticket's resolved
    autonomy:
      - credentials: external credentials/secrets found in the diff
      - security_actions: security-sensitive or irreversible operations
      - retry_limit: safety ceiling for automatic fix attempts before
        escalating.  The effective retry budget is the lower of this and
        ``max_auto_fix_attempts``.
    """
    resolved = dict(_DEFAULT_HUMAN_ESCALATION)
    resolved.update(cfg.get("human_escalation") or {})
    return resolved


def is_auto_fix_lane(autonomy: str) -> bool:
    """True when ``autonomy`` stays on the automatic fix/merge path (full, green, yellow)."""
    return autonomy in _AUTO_FIX_LANES


def is_red_lane(autonomy: str) -> bool:
    """True when ``autonomy`` is the red risk lane, which always escalates to human review."""
    return autonomy == "red"


def resolve_acceptance_contract_mode(cfg: dict) -> str:
    """
    Resolve whether the acceptance-contract audit hard-blocks or is advisory.

    Project-level only (no ticket override) — this is a policy choice about
    how strict a project wants to be, not a per-ticket concern.

    Returns "blocker" or "advisory" (default: "advisory", or "blocker" under
    profile: strict when not explicitly overridden — findings are persisted
    on the ticket for a reviewer to see either way, but a blocker verdict
    also forces needs_review/changes_requested).
    """
    mode = cfg.get("acceptance_contract_mode")
    if mode is None:
        mode = "blocker" if cfg.get("profile") == "strict" else "advisory"
    return "blocker" if mode == "blocker" else "advisory"


