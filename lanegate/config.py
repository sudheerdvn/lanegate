"""
config.py — load and validate .lanegate.yml (filename derived from APP_NAME).

Walk-up discovery: searches from cwd toward filesystem root until found.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from lanegate import APP_NAME

CONFIG_FILENAME = f".{APP_NAME}.yml"

_VALID_SYNC = {"ff-only", "merge-no-ff"}

_VALID_MODEL_STEPS = {"analyze", "implement", "review", "fix", "drift_check"}
_VALID_EXECUTOR_STEPS = {"implement", "review", "fix", "drift_check"}

# Claude executor headless flags. --dangerously-skip-permissions disables every
# permission check process-wide (including tools outside this list: WebFetch,
# WebSearch, MCP tools, ...). This scoped allowlist covers only the tool
# categories orchestrate's implement/fix/review steps actually exercise
# (editing files, reading context, running tests/git via Bash), so anything
# outside it stays gated rather than silently permitted. It is not a sandbox —
# Bash itself remains unrestricted in what commands it can run; see
# docs/security-model.md for the OS-level sandboxing that's still v2 scope.
_SCOPED_CLAUDE_HEADLESS_FLAGS = [
    "--allowedTools",
    "Bash,Edit,Write,Read,Glob,Grep",
]

# Built-in model defaults
_DEFAULT_ANALYZE_MODEL = "claude-haiku-4-5-20251001"
_DEFAULT_IMPLEMENT_MODEL = "claude-haiku-4-5-20251001"
_DEFAULT_REVIEW_MODEL = "claude-haiku-4-5-20251001"


@dataclass(frozen=True)
class TestRunnerDetection:
    """A test runner detected in the target repository."""

    name: str
    command: str


class ConfigError(ValueError):
    """Raised when .lanegate.yml contains an invalid configuration value."""


def validate_hook(hook_config: Any, hook_name: str) -> list[str]:
    """Validate and return a hook as a list of strings.

    Raises ConfigError if the hook is a bare string (security risk — string
    hooks would be executed with shell=True) or any other non-list type.

    Example valid YAML::

        post_promote:
          - ./scripts/notify.sh
          - --env=prod
    """
    if isinstance(hook_config, str):
        raise ConfigError(
            f"Hook '{hook_name}' must be a YAML list, not a string. "
            "Example:\n  post_deploy:\n    - ./scripts/notify.sh\n    - --env=prod"
        )
    if not isinstance(hook_config, list):
        raise ConfigError(f"Hook '{hook_name}' must be a list of strings.")
    return [str(arg) for arg in hook_config]


_VALID_TRIGGERS = {"manual", "auto"}
_VALID_EXECUTOR_TYPES = {
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
_VALID_REVIEWERS = _VALID_EXECUTOR_TYPES | {"human"}
_VALID_RATE_LIMIT_MODES = {"halt", "resume"}
# Give up auto-resuming after 24h and notify. load_config() merges .lanegate.yml
# shallowly, so a user who sets any single key under `rate_limit_resume:`
# replaces the whole default block — which is why resume_watch._run_loop
# re-states this default at its read site. Both must move together.
_DEFAULT_RESUME_CEILING_S = 86400
_VALID_HUMAN_REVIEW_MODES = {"none", "per_ticket", "final"}
_VALID_POOL_STRATEGIES = {"least-loaded", "round-robin"}

# Fields recognised on each drivers.<name> entry. 'type' is required; the rest
# are optional pass-through fields consumed by the executor dispatch layer.
_VALID_DRIVER_FIELDS = {"type", "model", "bin", "flags", "base_url", "provider"}

# Pipeline steps that may be routed to a named driver via steps:
_VALID_PIPELINE_STEPS = {"analyze", "implement", "review"}


def _default_config() -> dict:
    return {
        "ticket_prefix": "TICK",
        "tickets_dir": f".{APP_NAME}/tickets",
        "worktrees_dir": f".{APP_NAME}/worktrees",
        "executor": "claude",
        "executor_steps": {},
        "max_parallel": 2,
        "executors": {},
        "models": {},
        "core_files": [],
        "core_patterns": [],
        "verification": {
            "groups": [],
        },
        "lock_statuses": ["in_progress", "code_complete", "in_review"],
        "flag_file": f"~/.{APP_NAME}/feature_flags.json",
        "environments": [],
        "commit_status_changes": True,
        "github_pr": False,
        "safeguards": {},
        "default_milestone": None,
        "default_human_review": "none",
        "orphan_timeout_hours": 4,
        "executor_timeout_seconds": 1800,
        "executor_idle_timeout_seconds": 75,
        "executor_stall_timeout_seconds": 900,
        "executor_absolute_ceiling_seconds": 1500,
        "max_auto_fix_attempts": 1,
        "protected_paths": [],
        "on_rate_limit": "resume",
        "rate_limit_resume": {
            "initial_backoff_s": 300,
            "max_backoff_s": 7200,
            # Finite by default. `null` (poll forever) is still accepted, but it
            # must be opted into: a hibernation misclassified as waitable would
            # otherwise re-invoke orchestrate every 2h indefinitely, and that is
            # now the default path rather than an opt-in one.
            "ceiling_s": _DEFAULT_RESUME_CEILING_S,
        },
        "notify": {
            "ntfy_topic": None,
            "poll_seconds": 60,
            "heartbeat_stale_seconds": 180,
        },
        "project_guidance": {
            "include_defaults": True,
            "files": [],
            "max_bytes": 20000,
        },
        "static_analysis": {
            "enabled": True,
            "threshold": 0,
            "tools": {
                "gitleaks": True,
                "semgrep": True,
                "bandit": True,
                "pip_audit": True,
                "npm_audit": True,
                "composer_audit": True,
                "bundler_audit": True,
            },
        },
        "routing": [],
        "display_timezone": "local",
        "session_chaining": {
            "enabled": True,
            "chain_review": False,
            "max_session_age_s": 2700,
            "max_session_tokens": 150000,
        },
        "doc_update": {
            "doc_paths": ["README.md", "docs/ARCHITECTURE.md"],
            "status_filter": ["done"],
        },
    }


def resolve_trunk_branch(cfg: dict, repo_root: Path) -> str:
    """Return the repository's ticket-work trunk branch.

    Resolution is deliberately centralized so every ticket lifecycle operation
    agrees on its base: an explicit ``trunk_branch`` config value wins; then
    Git's configured ``origin/HEAD`` symbolic ref; finally ``main`` for local
    repositories without a remote default branch.
    """
    configured = cfg.get("trunk_branch")
    if configured is not None:
        if not isinstance(configured, str) or not configured.strip():
            raise ConfigError("trunk_branch must be a non-empty string")
        return configured.strip()

    try:
        detected = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True, encoding="utf-8",
        )
    except OSError:
        return "main"

    ref = detected.stdout.strip() if detected.returncode == 0 else ""
    prefix = "refs/remotes/origin/"
    if ref.startswith(prefix) and len(ref) > len(prefix):
        return ref.removeprefix(prefix)
    return "main"



def _validate_environments(envs: list[dict]) -> None:
    names = set()
    for i, env in enumerate(envs):
        name = env.get("name")
        if not name:
            raise ValueError(f"environments[{i}] missing 'name'")
        if name in names:
            raise ValueError(f"duplicate environment name: '{name}'")
        names.add(name)

        trigger = env.get("trigger", "manual")
        if trigger not in _VALID_TRIGGERS:
            raise ValueError(
                f"environments[{i}] '{name}': invalid trigger '{trigger}' — must be one of {_VALID_TRIGGERS}"
            )

        sync = env.get("sync", "ff-only")
        if sync not in _VALID_SYNC:
            raise ValueError(
                f"environments[{i}] '{name}': invalid sync '{sync}' — must be one of {_VALID_SYNC}"
            )


def _validate_verification(cfg: dict) -> None:
    """Validate the verification.groups block — each group needs a patterns list.

    A monorepo can have several UI areas (e.g. multiple frontend apps, or an
    AEM ui.frontend clientlib vs. the AEM instance itself) each reachable at a
    different URL/dev-server, so groups is a list rather than one flat block.
    """
    verification = cfg.get("verification") or {}
    if not isinstance(verification, dict):
        raise ConfigError("verification must be a mapping with a 'groups' key")
    groups = verification.get("groups") or []
    if not isinstance(groups, list):
        raise ConfigError("verification.groups must be a list")
    for i, group in enumerate(groups):
        if not isinstance(group, dict):
            raise ConfigError(f"verification.groups[{i}] must be a mapping")
        patterns = group.get("patterns")
        if not patterns or not isinstance(patterns, list):
            raise ConfigError(f"verification.groups[{i}] must have a non-empty 'patterns' list")


def _is_positive_int(value: Any) -> bool:
    # bool is a subclass of int; reject it explicitly.
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _validate_executor(cfg: dict) -> None:
    """Validate the top-level executor value."""
    executor = cfg.get("executor")
    if executor not in _VALID_EXECUTOR_TYPES:
        raise ValueError(f"invalid executor '{executor}' — must be one of {_VALID_EXECUTOR_TYPES}")


def _validate_reviewer(cfg: dict) -> None:
    """Validate the optional top-level reviewer value."""
    reviewer = cfg.get("reviewer")
    if reviewer is not None and reviewer not in _VALID_REVIEWERS:
        raise ValueError(f"invalid reviewer '{reviewer}' — must be one of {_VALID_REVIEWERS}")


def _validate_autonomy(cfg: dict) -> None:
    """Validate the optional top-level autonomy value."""
    from lanegate.ticket import _VALID_AUTONOMY

    autonomy = cfg.get("autonomy")
    if autonomy is not None and autonomy not in _VALID_AUTONOMY:
        raise ConfigError(f"invalid autonomy '{autonomy}' — must be one of {sorted(_VALID_AUTONOMY)}")


_DEFAULT_SESSION_CHAINING = {
    "enabled": True,
    "chain_review": False,
    "max_session_age_s": 2700,
    "max_session_tokens": 150000,
}


def resolve_session_chaining(cfg: dict) -> dict:
    """Return the effective session_chaining settings.

    Defaults are applied per-key, not as a whole-block fallback: `load_config`
    merges the raw YAML over `_default_config()` shallowly (TICK-089 and
    others share this pattern, e.g. `notify`/`project_guidance`), so a user's
    `.lanegate.yml` overriding just one field (e.g. `chain_review: true`) would
    otherwise silently drop the other three defaults instead of keeping them.
    """
    configured = cfg.get("session_chaining") or {}
    if not isinstance(configured, dict):
        raise ConfigError("session_chaining must be a mapping")
    return {**_DEFAULT_SESSION_CHAINING, **configured}


def _validate_session_chaining(cfg: dict) -> None:
    resolved = resolve_session_chaining(cfg)
    if not isinstance(resolved["enabled"], bool):
        raise ConfigError("session_chaining.enabled must be true or false")
    if not isinstance(resolved["chain_review"], bool):
        raise ConfigError("session_chaining.chain_review must be true or false")
    if not _is_positive_int(resolved["max_session_age_s"]):
        raise ConfigError("session_chaining.max_session_age_s must be a positive integer")
    if not _is_positive_int(resolved["max_session_tokens"]):
        raise ConfigError("session_chaining.max_session_tokens must be a positive integer")


def _validate_auto_fix(cfg: dict) -> None:
    """Validate max_auto_fix_attempts — must be a positive integer."""
    value = cfg.get("max_auto_fix_attempts", 1)
    if not _is_positive_int(value):
        raise ConfigError(f"max_auto_fix_attempts must be a positive integer, got {value!r}")


def _validate_executor_steps(cfg: dict) -> None:
    """Validate executor_steps: block — rejects unknown step keys and invalid executors."""
    executor_steps = cfg.get("executor_steps") or {}
    if not isinstance(executor_steps, dict):
        raise ConfigError("executor_steps must be a mapping of step name → executor string")
    unknown = set(executor_steps.keys()) - _VALID_EXECUTOR_STEPS
    if unknown:
        raise ConfigError(
            f"unknown key(s) under executor_steps: {sorted(unknown)} — "
            f"valid steps are {sorted(_VALID_EXECUTOR_STEPS)}"
        )
    for step, ex in executor_steps.items():
        valid_values = _VALID_REVIEWERS if step == "review" else _VALID_EXECUTOR_TYPES
        if ex not in valid_values:
            raise ConfigError(
                f"executor_steps['{step}']: invalid executor '{ex}' — must be one of {valid_values}"
            )


_KNOWN_AGY_MODELS = {
    "gemini-3.6-flash-high",
    "gemini-3.6-flash-medium",
    "gemini-3.6-flash-low",
    "gemini-3.0-pro",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-pro",
    "gemini-1.5-pro",
    "gemini-1.5-flash",
}


def validate_model_for_executor(model: str, executor_type: str, context_label: str = "") -> None:
    """Validate model string against known valid model registries per executor type."""
    if not isinstance(model, str) or not model.strip():
        raise ConfigError(f"{context_label} model string must be a non-empty string")
    model = model.strip()
    if executor_type == "agy":
        if model not in _KNOWN_AGY_MODELS and not (model.startswith("claude-") or model.startswith("anthropic/")):
            raise ConfigError(
                f"unmapped model '{model}' for executor '{executor_type}' in {context_label}. "
                f"Valid models for agy are: {sorted(_KNOWN_AGY_MODELS)} or Claude models starting with 'claude-' or 'anthropic/'"
            )
    elif executor_type in {"claude", "claude-process", "claude-subagent"}:
        if not (model.startswith("claude-") or model.startswith("anthropic/")):
            raise ConfigError(
                f"unmapped model '{model}' for executor '{executor_type}' in {context_label}. "
                "Claude models must start with 'claude-' or 'anthropic/'"
            )
    elif executor_type == "codex":
        if not (model.startswith("gpt-") or model.startswith("o1") or model.startswith("o3") or model.startswith("codex") or model.startswith("openai/")):
            raise ConfigError(
                f"unmapped model '{model}' for executor '{executor_type}' in {context_label}. "
                "Codex models must start with 'gpt-', 'o1', 'o3', 'codex', or 'openai/'"
            )
    elif executor_type == "aider":
        valid_prefixes = ("claude-", "gpt-", "o1", "o3", "ollama", "deepseek", "gemini", "anthropic/", "openai/")
        if not any(model.startswith(p) for p in valid_prefixes):
            raise ConfigError(
                f"unmapped model '{model}' for executor '{executor_type}' in {context_label}."
            )


def _validate_models(cfg: dict) -> None:
    """Validate the top-level models: block — rejects unknown step keys and unmapped model strings."""
    models = cfg.get("models") or {}
    if not isinstance(models, dict):
        raise ConfigError("models must be a mapping of step name → model string")
    unknown = set(models.keys()) - _VALID_MODEL_STEPS
    if unknown:
        raise ConfigError(
            f"unknown key(s) under models: {sorted(unknown)} — "
            f"valid steps are {sorted(_VALID_MODEL_STEPS)}"
        )
    executors = cfg.get("executors") or {}
    for step, model_str in models.items():
        if isinstance(model_str, str):
            ex_name = resolve_executor(cfg, step)
            ex_cfg = executors.get(ex_name) if isinstance(executors, dict) else None
            ex_type = ex_cfg.get("type", ex_name) if isinstance(ex_cfg, dict) else ex_name
            validate_model_for_executor(model_str, ex_type, f"models.{step}")

    # Also validate per-executor models blocks
    for ex_name, ex_cfg in executors.items():
        if not isinstance(ex_cfg, dict):
            continue
        ex_models = ex_cfg.get("models") or {}
        if not isinstance(ex_models, dict):
            raise ConfigError(
                f"executors['{ex_name}'].models must be a mapping of step name → model string"
            )
        ex_unknown = set(ex_models.keys()) - _VALID_MODEL_STEPS
        if ex_unknown:
            raise ConfigError(
                f"unknown key(s) under executors['{ex_name}'].models: {sorted(ex_unknown)} — "
                f"valid steps are {sorted(_VALID_MODEL_STEPS)}"
            )
        ex_type = ex_cfg.get("type", ex_name)
        for step, model_str in ex_models.items():
            if isinstance(model_str, str):
                validate_model_for_executor(model_str, ex_type, f"executors['{ex_name}'].models.{step}")


def _parse_drivers(raw: dict, valid_types: set[str]) -> dict:
    """Parse and validate the drivers: block.

    Returns {} if the key is absent from *raw* (backward compat — projects
    without named driver instances keep using the top-level executor/reviewer
    fields unchanged). Each entry requires a 'type' field that must be in
    *valid_types*; 'model', 'bin', 'flags', 'base_url', and 'provider' are optional
    pass-through fields consumed by the dispatch layer in later tickets.
    """
    drivers = raw.get("drivers") or {}
    if not isinstance(drivers, dict):
        raise ConfigError("drivers must be a mapping of driver name → settings")

    parsed: dict[str, dict] = {}
    for name, entry in drivers.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"drivers['{name}'] must be a mapping")
        driver_type = entry.get("type")
        if driver_type is None:
            raise ConfigError(f"drivers['{name}'] is missing required 'type' field")
        if driver_type not in valid_types:
            raise ConfigError(
                f"drivers['{name}'].type: unknown type '{driver_type}' — "
                f"must be one of {sorted(valid_types)}"
            )
        provider = entry.get("provider")
        if provider is not None and not isinstance(provider, str):
            raise ConfigError(
                f"drivers['{name}'].provider must be a string, got {provider!r}"
            )
        model = entry.get("model")
        if model is not None:
            if not isinstance(model, str):
                raise ConfigError(
                    f"drivers['{name}'].model must be a string, got {model!r}"
                )
            validate_model_for_executor(model, driver_type, f"drivers['{name}'].model")
        parsed[name] = dict(entry)
    return parsed


def _parse_steps(raw: dict, drivers: dict, valid_types: set[str]) -> dict:
    """Parse and validate the optional steps: block.

    Returns {} if the key is absent from *raw*. Each of the recognised step
    keys (analyze/implement/review) must be a mapping with a 'driver' field
    that either names a key in *drivers* or a legacy executor type in
    *valid_types* (so bare `driver: aider` keeps working without a drivers:
    block).
    """
    steps = raw.get("steps") or {}
    if not isinstance(steps, dict):
        raise ConfigError("steps must be a mapping of step name → settings")

    unknown = set(steps.keys()) - _VALID_PIPELINE_STEPS
    if unknown:
        raise ConfigError(
            f"unknown key(s) under steps: {sorted(unknown)} — "
            f"valid steps are {sorted(_VALID_PIPELINE_STEPS)}"
        )

    parsed: dict[str, dict] = {}
    for step_name, entry in steps.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"steps['{step_name}'] must be a mapping")
        driver_name = entry.get("driver")
        if driver_name is None:
            raise ConfigError(f"steps['{step_name}'] is missing required 'driver' field")
        if driver_name not in drivers and driver_name not in valid_types:
            raise ConfigError(
                f"steps['{step_name}'].driver: undefined driver '{driver_name}' — "
                f"must reference a key in drivers: or be one of {sorted(valid_types)}"
            )
        parsed[step_name] = dict(entry)
    return parsed


def _validate_executor_instances(cfg: dict) -> None:
    """Validate named executor instances under executors: (TICK-088).

    An entry that carries a 'type' field is a *named instance* — e.g.::

        executors:
          claude-1:
            type: claude-process
            api_key_env: ANTHROPIC_API_KEY_1
            max_parallel: 2

    'type' must resolve to a known executor driver, and 'api_key_env' (when
    present) must be a string naming the environment variable that holds
    that instance's API key.

    Entries WITHOUT a 'type' field are the older per-type override block
    (TICK-028 and earlier) — the entry's own key IS the executor type
    (e.g. ``executors: {aider: {max_parallel: 3}}``), so no 'type' field is
    required and this function does not touch them; see
    _validate_concurrency/_validate_models for their validation.
    """
    executors = cfg.get("executors") or {}
    if not isinstance(executors, dict):
        return  # reported by _validate_concurrency
    for name, entry in executors.items():
        if not isinstance(entry, dict):
            continue
        provider = entry.get("provider")
        if provider is not None and not isinstance(provider, str):
            raise ConfigError(
                f"executors['{name}'].provider must be a string, got {provider!r}"
            )
        exec_type = entry.get("type")
        if exec_type is None:
            continue  # legacy per-type override — key itself is the type
        if exec_type not in _VALID_EXECUTOR_TYPES:
            raise ConfigError(
                f"executors['{name}'].type: unknown type '{exec_type}' — "
                f"must be one of {sorted(_VALID_EXECUTOR_TYPES)}"
            )
        api_key_env = entry.get("api_key_env")
        if api_key_env is not None and not isinstance(api_key_env, str):
            raise ConfigError(
                f"executors['{name}'].api_key_env must be a string, got {api_key_env!r}"
            )


def _validate_concurrency(cfg: dict) -> None:
    """Validate the resource gate: top-level max_parallel + per-executor overrides."""
    if not _is_positive_int(cfg.get("max_parallel")):
        raise ValueError(
            f"max_parallel must be a positive integer, got {cfg.get('max_parallel')!r}"
        )

    executors = cfg.get("executors") or {}
    if not isinstance(executors, dict):
        raise ValueError("executors must be a mapping of executor name → settings")
    for name, settings in executors.items():
        if not isinstance(settings, dict):
            raise ValueError(f"executors['{name}'] must be a mapping")
        if "max_parallel" in settings and not _is_positive_int(settings["max_parallel"]):
            raise ValueError(
                f"executors['{name}'].max_parallel must be a positive integer, "
                f"got {settings['max_parallel']!r}"
            )


def _validate_orphan_timeout(cfg: dict) -> None:
    value = cfg.get("orphan_timeout_hours", 4)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"orphan_timeout_hours must be a positive number, got {value!r}")


def _validate_executor_timeouts(cfg: dict) -> None:
    """Validate executor output-idle, progress-stall, and hard-ceiling timeouts."""
    idle = cfg.get("executor_idle_timeout_seconds", 75)
    stall = cfg.get("executor_stall_timeout_seconds", 900)
    ceiling = cfg.get("executor_absolute_ceiling_seconds", 1500)
    for key, value in (
        ("executor_idle_timeout_seconds", idle),
        ("executor_stall_timeout_seconds", stall),
        ("executor_absolute_ceiling_seconds", ceiling),
    ):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"{key} must be a positive number, got {value!r}")
    if not idle < stall < ceiling:
        raise ConfigError(
            "executor timeout ordering must be "
            "executor_idle_timeout_seconds < executor_stall_timeout_seconds < "
            "executor_absolute_ceiling_seconds"
        )


def _validate_default_human_review(cfg: dict) -> None:
    """Validate default_human_review -- the project-wide fallback for `orchestrate --human-review`."""
    mode = cfg.get("default_human_review", "none")
    if mode not in _VALID_HUMAN_REVIEW_MODES:
        raise ValueError(
            f"default_human_review must be one of {sorted(_VALID_HUMAN_REVIEW_MODES)}, got {mode!r}"
        )


def _validate_doc_update(cfg: dict) -> None:
    """Validate doc_update configuration section."""
    doc_up = cfg.get("doc_update")
    if doc_up is None:
        return
    if not isinstance(doc_up, dict):
        raise ConfigError("doc_update must be a mapping")
    doc_paths = doc_up.get("doc_paths")
    if doc_paths is not None:
        if not isinstance(doc_paths, list) or not all(isinstance(p, str) for p in doc_paths):
            raise ConfigError("doc_update.doc_paths must be a list of strings")
    status_filter = doc_up.get("status_filter")
    if status_filter is not None:
        if isinstance(status_filter, str):
            doc_up["status_filter"] = [status_filter]
        elif isinstance(status_filter, list):
            if not all(isinstance(s, str) for s in status_filter):
                raise ConfigError("doc_update.status_filter must be a string or list of strings")
        else:
            raise ConfigError("doc_update.status_filter must be a string or list of strings")



def _validate_display_timezone(cfg: dict) -> None:
    """Validate display_timezone -- the IANA zone (or "local") used to format
    timestamps for humans in `run-report` and the TUI. Stored timestamps stay
    UTC; this only controls how they're rendered."""
    name = cfg.get("display_timezone", "local")
    if name == "local":
        return
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"display_timezone must be \"local\" or a valid IANA zone name (e.g. "
            f"\"America/Los_Angeles\"), got {name!r}"
        ) from exc


def resolve_display_tzinfo(cfg: dict):
    """Return the tzinfo to format human-readable timestamps with.

    "local" (the default) defers to the system timezone via naive
    datetime.astimezone(); any other value must be a valid IANA zone name.
    """
    name = cfg.get("display_timezone") or "local"
    if name == "local":
        return None
    from zoneinfo import ZoneInfo

    return ZoneInfo(name)


def format_display_ts(iso_ts: str | None, cfg: dict) -> str | None:
    """Format a stored UTC "%Y-%m-%dT%H:%M:%SZ" timestamp for human display,
    converting to the configured display_timezone (default: system local).
    Returns the input unchanged if it isn't in the expected format."""
    if not iso_ts:
        return iso_ts
    import datetime as _dt

    try:
        dt = _dt.datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return iso_ts
    local = dt.astimezone(resolve_display_tzinfo(cfg))
    return local.strftime("%Y-%m-%d %H:%M:%S %Z")


def _validate_rate_limit(cfg: dict) -> None:
    """Validate on_rate_limit and the rate_limit_resume backoff block."""
    mode = cfg.get("on_rate_limit", "resume")
    if mode not in _VALID_RATE_LIMIT_MODES:
        raise ValueError(
            f"on_rate_limit must be one of {sorted(_VALID_RATE_LIMIT_MODES)}, got {mode!r}"
        )

    resume = cfg.get("rate_limit_resume") or {}
    if not isinstance(resume, dict):
        raise ConfigError("rate_limit_resume must be a mapping")

    initial = resume.get("initial_backoff_s", 300)
    if not isinstance(initial, (int, float)) or isinstance(initial, bool) or initial <= 0:
        raise ValueError(
            f"rate_limit_resume.initial_backoff_s must be a positive number, got {initial!r}"
        )

    max_backoff = resume.get("max_backoff_s", 7200)
    if not isinstance(max_backoff, (int, float)) or isinstance(max_backoff, bool) or max_backoff <= 0:
        raise ValueError(
            f"rate_limit_resume.max_backoff_s must be a positive number, got {max_backoff!r}"
        )
    if max_backoff < initial:
        raise ValueError(
            "rate_limit_resume.max_backoff_s must be >= initial_backoff_s "
            f"(got max_backoff_s={max_backoff!r}, initial_backoff_s={initial!r})"
        )

    ceiling = resume.get("ceiling_s")
    if ceiling is not None and (
        not isinstance(ceiling, (int, float)) or isinstance(ceiling, bool) or ceiling <= 0
    ):
        raise ValueError(
            f"rate_limit_resume.ceiling_s must be null or a positive number, got {ceiling!r}"
        )


def _validate_pools(cfg: dict) -> None:
    """Validate the `pools:` block (TICK-089) and `default_pool`.

    pools:
      default:
        executors: [claude-1, claude-2]   # named instances from executors:
        strategy: least-loaded            # or round-robin (default: least-loaded)

    Each named executor listed in a pool must already exist under `executors:`
    (as either a named instance from TICK-088 or a plain per-type entry).
    `default_pool`, when set, must name a pool defined in `pools:`.
    """
    pools = cfg.get("pools")
    if pools is None:
        if cfg.get("default_pool") is not None:
            raise ConfigError("default_pool is set but no pools: block is defined")
        return
    if not isinstance(pools, dict):
        raise ConfigError("pools must be a mapping of pool name -> {executors, strategy}")

    executors = cfg.get("executors") or {}
    for name, pool in pools.items():
        if not isinstance(pool, dict):
            raise ConfigError(f"pools['{name}'] must be a mapping")
        pool_executors = pool.get("executors")
        if not isinstance(pool_executors, list) or not pool_executors:
            raise ConfigError(f"pools['{name}'].executors must be a non-empty list")
        for ex_name in pool_executors:
            if not isinstance(ex_name, str) or ex_name not in executors:
                raise ConfigError(
                    f"pools['{name}'].executors references unknown executor {ex_name!r} — "
                    f"must be one of {sorted(executors)}"
                )
        strategy = pool.get("strategy", "least-loaded")
        if strategy not in _VALID_POOL_STRATEGIES:
            raise ConfigError(
                f"pools['{name}'].strategy must be one of {sorted(_VALID_POOL_STRATEGIES)}, "
                f"got {strategy!r}"
            )

    default_pool = cfg.get("default_pool")
    if default_pool is not None and default_pool not in pools:
        raise ConfigError(f"default_pool {default_pool!r} is not defined in pools:")


def update_pool_executor_order(repo_root: Path, pool_name: str, executors: list[str]) -> dict:
    """Persist a reordered `pools.<pool_name>.executors` list back to
    .lanegate.yml (TICK-269), so a TUI reorder control can change which
    instance least-loaded prefers on ties and where round-robin starts,
    without hand-editing the config file.

    Raises ConfigError if the pool doesn't exist or *executors* isn't a
    reordering of its current executor set — this endpoint changes
    preference order only, not pool membership.
    """
    config_path = find_config(repo_root)
    if config_path is None:
        raise ConfigError(f"no {CONFIG_FILENAME} found under {repo_root}")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
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
    pool["executors"] = list(executors)
    config_path.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False), encoding="utf-8")
    return {
        "name": pool_name,
        "strategy": pool.get("strategy", "least-loaded"),
        "executors": list(executors),
    }


_ROUTING_INT_WHEN_KEYS = frozenset(
    {
        "complexity_min",
        "complexity_max",
        "touches_min",
        "touches_max",
        "priority_min",
        "priority_max",
    }
)
_ROUTING_WHEN_KEYS = _ROUTING_INT_WHEN_KEYS | {"label"}


def _validate_routing(cfg: dict) -> None:
    """Validate the `routing:` block (TICK-091).

    routing:
      - when: {complexity_max: 2, touches_max: 3}
        executor_pool: local
      - when: {complexity_min: 3}
        executor_pool: default

    Rules are evaluated top-to-bottom (first match wins) by
    `resolve_ticket_pool`. Every `executor_pool` referenced must already be
    defined under `pools:`; tickets that match no rule fall back to the
    top-level `default_pool`.
    """
    routing = cfg.get("routing")
    if not routing:
        return
    if not isinstance(routing, list):
        raise ConfigError("routing must be a list of rules")

    pools = cfg.get("pools") or {}
    for i, rule in enumerate(routing):
        if not isinstance(rule, dict):
            raise ConfigError(f"routing[{i}] must be a mapping")
        when = rule.get("when") or {}
        if not isinstance(when, dict):
            raise ConfigError(f"routing[{i}].when must be a mapping")
        unknown = set(when) - _ROUTING_WHEN_KEYS
        if unknown:
            raise ConfigError(
                f"routing[{i}].when has unknown field(s) {sorted(unknown)} — "
                f"must be one of {sorted(_ROUTING_WHEN_KEYS)}"
            )
        for key in _ROUTING_INT_WHEN_KEYS & set(when):
            value = when[key]
            if not isinstance(value, int) or isinstance(value, bool):
                raise ConfigError(f"routing[{i}].when.{key} must be an integer, got {value!r}")
        if "label" in when and not isinstance(when["label"], str):
            raise ConfigError(f"routing[{i}].when.label must be a string, got {when['label']!r}")

        executor_pool = rule.get("executor_pool")
        if not isinstance(executor_pool, str) or not executor_pool:
            raise ConfigError(
                f"routing[{i}].executor_pool is required and must be a non-empty string"
            )
        if executor_pool not in pools:
            raise ConfigError(
                f"routing[{i}].executor_pool {executor_pool!r} is not defined in pools: — "
                f"must be one of {sorted(pools)}"
            )


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
    """Resolve which `pools:` entry a ticket routes to (TICK-091).

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


def _validate_project_guidance(cfg: dict) -> None:
    value = cfg.get("project_guidance")
    if value is False or value is None:
        return
    if not isinstance(value, dict):
        raise ConfigError("project_guidance must be false or a mapping")

    files = value.get("files", [])
    if files is None:
        files = []
    if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        raise ConfigError("project_guidance.files must be a list of relative path patterns")
    if any(Path(item).is_absolute() for item in files):
        raise ConfigError("project_guidance.files must contain relative path patterns only")

    include_defaults = value.get("include_defaults", True)
    if not isinstance(include_defaults, bool):
        raise ConfigError("project_guidance.include_defaults must be true or false")

    max_bytes = value.get("max_bytes", 20000)
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        raise ConfigError("project_guidance.max_bytes must be a positive integer")


def resolve_max_parallel_detail(cfg: dict, override: int | None = None) -> dict[str, Any]:
    """
    Effective concurrency cap details (the resource gate). Precedence, first hit wins:
      1. explicit override (e.g. orchestrator --max N)
      2. executors[<active executor>].max_parallel
      3. min(executors[<instance>].max_parallel for instance in pools[default_pool])
         — TICK-286: a bare `executor:` value that doesn't match any named
         pool instance (e.g. executor: claude with only claude-a/claude-b
         defined) previously fell straight through to the top-level/default
         value, ignoring every per-instance cap in the pool actually serving
         dispatch.
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
        if capped:
            pool_detail: dict[str, Any] = {
                "value": min(capped),
                "source": "pool instance cap (min)",
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


def resolve_model(cfg: dict, step: str, ticket: dict | None = None) -> str | None:
    """
    Resolve the effective model for a given pipeline step.

    Resolution order (first hit wins):
      1. ticket.model field (implement only — passed via ticket dict)
      2. executors[<active executor>].models.<step>
      3. top-level models.<step>
      4. None (executor's own default)

    The caller may use the returned value to inject ``--model <model>`` (or
    the appropriate flag) into the executor command.  A return value of None
    means "no model flag — let the executor use its own default."
    """
    # 1. Per-ticket model override (relevant for implement step)
    if ticket and ticket.get("model"):
        return ticket["model"]

    active_executor = cfg.get("executor", "claude")

    # 2. Per-executor model override for this step
    ex_cfg = (cfg.get("executors") or {}).get(active_executor) or {}
    if isinstance(ex_cfg, dict):
        ex_models = ex_cfg.get("models") or {}
        if step in ex_models:
            return ex_models[step]

    # 3. Top-level models block
    top_models = cfg.get("models") or {}
    if step in top_models:
        return top_models[step]

    # 4. No model configured. Claude-compatible executors keep the built-in
    # defaults; other executors should use their own CLI default instead of
    # receiving a Claude model name they may not support.
    #
    # active_executor may be a named instance (TICK-088, e.g. "claude-a") whose
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

    Returns one of "full", "supervised", "manual".
    """
    if ticket and ticket.get("autonomy"):
        return ticket["autonomy"]
    if cfg.get("autonomy"):
        return cfg["autonomy"]
    return "supervised"


def resolve_acceptance_contract_mode(cfg: dict) -> str:
    """
    Resolve whether the acceptance-contract audit hard-blocks or is advisory.

    Project-level only (no ticket override) — this is a policy choice about
    how strict a project wants to be, not a per-ticket concern.

    Returns "blocker" or "advisory" (default: "advisory" — findings are
    persisted on the ticket for a reviewer to see, but do not by themselves
    force needs_review/changes_requested).
    """
    mode = cfg.get("acceptance_contract_mode", "advisory")
    return "blocker" if mode == "blocker" else "advisory"


def _warn_if_combined_mode_collapse(cfg: dict) -> None:
    """Warn when the effective pipeline route collapses into self-review.

    ``steps`` is the current routing surface and takes precedence over the
    top-level fallback fields at dispatch time.  Use it here too: otherwise a
    legacy ``executor: codex`` / ``reviewer: codex`` fallback emits a false
    warning even when distinct ``steps.implement`` and ``steps.review``
    drivers produce the independent review route.
    """
    if not cfg.get("reviewer"):
        return
    step_routes = cfg.get("steps") or {}
    implement = (step_routes.get("implement") or {}).get("driver") or resolve_executor(
        cfg, "implement"
    )
    review = (step_routes.get("review") or {}).get("driver") or resolve_executor(cfg, "review")
    if implement == review:
        warnings.warn(
            f"reviewer: {review!r} resolves identically to the implement executor "
            f"{implement!r} — review will run in combined (self-review) mode, not "
            "the independent review pipeline.",
            stacklevel=2,
        )


def find_config(start: Path | None = None) -> Path | None:
    """Walk up from start (default: cwd) looking for CONFIG_FILENAME. Returns None if not found."""
    here = (start or Path.cwd()).resolve()
    for directory in [here, *here.parents]:
        candidate = directory / CONFIG_FILENAME
        if candidate.exists():
            return candidate
    return None


def load_config(repo_root: Path | None = None) -> dict:
    """
    Load and return merged config. If repo_root is given, look there; otherwise walk up from cwd.
    Missing config is not an error — returns defaults (permits `lanegate init` on fresh repos).
    """
    cfg = _default_config()

    config_path = None
    if repo_root is not None:
        candidate = repo_root / CONFIG_FILENAME
        if candidate.exists():
            config_path = candidate
    else:
        config_path = find_config()

    raw: dict = {}
    if config_path is not None:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        cfg.update({k: v for k, v in raw.items() if v is not None})
        # Existing projects may already override one of the two historical
        # timeout keys.  When they have not opted into the new stall timeout,
        # derive a safe midpoint instead of making that valid configuration
        # fail merely because the new 900-second default no longer fits.
        if (
            "executor_stall_timeout_seconds" not in raw
            and (
                "executor_idle_timeout_seconds" in raw
                or "executor_absolute_ceiling_seconds" in raw
            )
        ):
            cfg["executor_stall_timeout_seconds"] = (
                cfg["executor_idle_timeout_seconds"]
                + cfg["executor_absolute_ceiling_seconds"]
            ) / 2

    resolution_root = repo_root or (config_path.parent if config_path is not None else Path.cwd())
    cfg["trunk_branch"] = resolve_trunk_branch(cfg, resolution_root)

    # ntfy topic name is effectively a bearer credential -- anyone who knows
    # it can push to or read the channel -- so it should not have to live in
    # a config file that's tracked in git. An env var always wins over
    # whatever (if anything) notify.ntfy_topic holds in the file.
    env_ntfy_topic = os.environ.get("LANEGATE_NTFY_TOPIC")
    if env_ntfy_topic:
        notify_cfg = dict(cfg.get("notify") or {})
        notify_cfg["ntfy_topic"] = env_ntfy_topic
        cfg["notify"] = notify_cfg

    # Normalize environment entries with defaults
    normalized_envs = []
    for env in cfg.get("environments") or []:
        env_name = env["name"]
        # Validate hooks are list-form only (string hooks execute with shell=True — not allowed)
        raw_pre = env.get("pre_promote") or []
        raw_post = env.get("post_promote") or []
        raw_guard = env.get("guard_script")
        if raw_pre:
            raw_pre = validate_hook(raw_pre, f"environments['{env_name}'].pre_promote")
        if raw_post:
            raw_post = validate_hook(raw_post, f"environments['{env_name}'].post_promote")
        if raw_guard is not None:
            raw_guard = validate_hook(raw_guard, f"environments['{env_name}'].guard_script")
        e: dict[str, Any] = {
            "name": env_name,
            "branch": env.get("branch", env_name),
            "from": env.get("from", cfg["trunk_branch"]),
            "trigger": env.get("trigger", "manual"),
            "sync": env.get("sync", "ff-only"),
            "guard_script": raw_guard,
            "pre_promote": raw_pre,
            "post_promote": raw_post,
            "flag_file": env.get("flag_file"),
        }
        normalized_envs.append(e)
    cfg["environments"] = normalized_envs

    # Named driver instances (drivers:) and per-step routing (steps:) — purely
    # additive; existing executor/reviewer/executor_steps fields are untouched
    # when drivers: is absent from the config file.
    cfg["drivers"] = _parse_drivers(raw, valid_types=_VALID_EXECUTOR_TYPES)
    cfg["steps"] = _parse_steps(raw, drivers=cfg["drivers"], valid_types=_VALID_EXECUTOR_TYPES)

    _validate_environments(cfg["environments"])
    _validate_concurrency(cfg)
    _validate_executor_instances(cfg)
    _validate_pools(cfg)
    _validate_routing(cfg)
    _validate_executor(cfg)
    _validate_reviewer(cfg)
    _validate_autonomy(cfg)
    _validate_auto_fix(cfg)
    _validate_models(cfg)
    _validate_project_guidance(cfg)
    _validate_executor_steps(cfg)
    _validate_orphan_timeout(cfg)
    _validate_executor_timeouts(cfg)
    _validate_verification(cfg)
    _validate_rate_limit(cfg)
    _validate_default_human_review(cfg)
    _validate_display_timezone(cfg)
    _validate_session_chaining(cfg)
    _validate_doc_update(cfg)
    _warn_if_combined_mode_collapse(cfg)
    return cfg


def repo_root_from_config(cfg_path: Path) -> Path:
    return cfg_path.parent


def find_repo_root(start: Path | None = None) -> Path:
    """Find the repo root by locating the config file. Falls back to cwd if not found."""
    config_path = find_config(start)
    if config_path:
        return config_path.parent
    return (start or Path.cwd()).resolve()


def protected_branches(cfg: dict) -> set[str]:
    """Set of branch names that must never be removed or pruned."""
    return {env["branch"] for env in cfg.get("environments", [])}


# ---------------------------------------------------------------------------
# Global project registry (~/.lanegate/projects.json)
# ---------------------------------------------------------------------------

_REGISTRY_DIR = Path.home() / f".{APP_NAME}"
_REGISTRY_FILE = _REGISTRY_DIR / "projects.json"


def _registry_load() -> list[dict]:
    """Return the list of registered projects, or [] if registry doesn't exist."""
    if not _REGISTRY_FILE.exists():
        return []
    try:
        data = json.loads(_REGISTRY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _registry_save(projects: list[dict]) -> None:
    _REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    _REGISTRY_FILE.write_text(json.dumps(projects, indent=2) + "\n", encoding="utf-8")


def registry_add(repo_root: Path) -> None:
    """Register repo_root in ~/.lanegate/projects.json (idempotent)."""
    resolved = str(repo_root.resolve())
    projects = _registry_load()
    for entry in projects:
        if entry.get("path") == resolved:
            return  # already registered
    projects.append({"path": resolved, "name": repo_root.resolve().name})
    _registry_save(projects)


def registry_remove(repo_root: Path) -> None:
    """Deregister repo_root from ~/.lanegate/projects.json (no-op if not present)."""
    resolved = str(repo_root.resolve())
    projects = _registry_load()
    updated = [e for e in projects if e.get("path") != resolved]
    _registry_save(updated)


def registry_load() -> list[dict]:
    """Return the list of registered projects as a list of {path, name} dicts."""
    return _registry_load()


def registry_path() -> Path:
    """Return the path to the global project registry file."""
    return _REGISTRY_FILE


# ---------------------------------------------------------------------------
# Interactive / non-interactive init
# ---------------------------------------------------------------------------


def _prompt(prompt_text: str, default: str) -> str:
    """Prompt the user with a default shown in brackets; empty input returns default."""
    raw = input(f"{prompt_text} [{default}]: ").strip()
    return raw if raw else default


def _project_mentions_pytest(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        return "pytest" in path.read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False


def _package_json_has_test_script(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    scripts = data.get("scripts")
    return isinstance(scripts, dict) and bool(scripts.get("test"))


def detect_test_runner_safeguards(repo_root: Path) -> list[TestRunnerDetection]:
    """Detect common project test runners and return concrete safeguard commands."""

    detections: list[TestRunnerDetection] = []

    if (
        _project_mentions_pytest(repo_root / "pyproject.toml")
        or _project_mentions_pytest(repo_root / "setup.cfg")
        or any((repo_root / "tests").glob("test_*.py"))
    ):
        detections.append(TestRunnerDetection("pytest", "pytest"))

    if _package_json_has_test_script(repo_root / "package.json"):
        detections.append(TestRunnerDetection("npm test", "npm test"))

    if (repo_root / "Cargo.toml").exists():
        detections.append(TestRunnerDetection("cargo test", "cargo test"))

    if (repo_root / "go.mod").exists():
        detections.append(TestRunnerDetection("go test", "go test"))

    return detections


def suggested_safeguards_yaml(detections: list[TestRunnerDetection]) -> str:
    """Return the YAML block LaneGate suggests for detected test runners."""

    commands = [d.command for d in detections]
    lines = ["safeguards:", "  pre_complete:"]
    lines.extend(f"    - {command}" for command in commands)
    lines.append("  pre_merge:")
    lines.extend(f"    - {command}" for command in commands)
    return "\n".join(lines)


def interactive_init(
    repo_root: Path, *, use_defaults: bool = False, force_interactive: bool = False
) -> dict | None:
    """
    Walk through every core config option and write .lanegate.yml to repo_root.

    Returns the config dict on success, or None if .lanegate.yml already exists.

    Parameters
    ----------
    repo_root:
        Directory where .lanegate.yml will be written.
    use_defaults:
        Skip all prompts and write a minimal config (also activated when stdin is
        not a TTY).
    force_interactive:
        Show prompts even when stdin is not a TTY (overrides the TTY check).
    """
    config_path = repo_root / CONFIG_FILENAME

    if config_path.exists():
        print(
            f"ERROR: {CONFIG_FILENAME} already exists at {repo_root}. "
            "Remove it first to re-initialise.",
            file=sys.stderr,
        )
        return None

    detected_trunk_branch = resolve_trunk_branch({}, repo_root)

    non_interactive = use_defaults or (not force_interactive and not sys.stdin.isatty())
    if non_interactive and not use_defaults:
        print(
            "Note: stdin is not a TTY — using defaults. "
            "Run `lanegate init --interactive` to configure interactively.",
            file=sys.stderr,
        )

    if non_interactive:
        # Minimal defaults, no environments, no flag_file
        cfg: dict = {
            "ticket_prefix": "TICK",
            "tickets_dir": f".{APP_NAME}/tickets",
            "worktrees_dir": f".{APP_NAME}/worktrees",
            "executor": "claude",
            "max_parallel": 2,
            "commit_status_changes": True,
            "github_pr": False,
            "executors": {
                "claude": {
                    "flags": list(_SCOPED_CLAUDE_HEADLESS_FLAGS),
                }
            },
        }
    else:
        # --- Core section ---
        print("\nConfiguring core options (press Enter to accept the default):\n")
        ticket_prefix = _prompt("ticket_prefix", "TICK")
        tickets_dir = _prompt("tickets_dir", f".{APP_NAME}/tickets")
        worktrees_dir = _prompt("worktrees_dir", f".{APP_NAME}/worktrees")
        executor_raw = _prompt("executor", "claude")
        # Validate executor; fall back to claude on invalid input
        executor = executor_raw if executor_raw in _VALID_EXECUTOR_TYPES else "claude"
        if executor != executor_raw:
            print(
                f"  Warning: '{executor_raw}' is not a recognised executor; "
                f"using 'claude'. (Valid: {sorted(_VALID_EXECUTOR_TYPES)})"
            )
        reviewer_raw = _prompt("reviewer", executor)
        reviewer = reviewer_raw if reviewer_raw in _VALID_REVIEWERS else executor
        if reviewer != reviewer_raw:
            print(
                f"  Warning: '{reviewer_raw}' is not a recognised reviewer; "
                f"using '{executor}'. (Valid: {sorted(_VALID_REVIEWERS)})"
            )
        max_parallel_raw = _prompt("max_parallel", "2")
        try:
            max_parallel = int(max_parallel_raw)
            if max_parallel < 1:
                raise ValueError
        except ValueError:
            print("  Invalid max_parallel — using 2.")
            max_parallel = 2

        cfg = {
            "ticket_prefix": ticket_prefix,
            "tickets_dir": tickets_dir,
            "worktrees_dir": worktrees_dir,
            "executor": executor,
            "reviewer": reviewer,
            "max_parallel": max_parallel,
            "commit_status_changes": True,
        }

        # --- Optional: executor headless flags ---
        if executor == "claude":
            print()
            print("Note: Claude executor requires headless flags for unattended runs.")
            print("These are already pre-configured for you with a scoped permission set")
            print("(--allowedTools), rather than --dangerously-skip-permissions.")
            cfg["executors"] = {
                "claude": {
                    "flags": list(_SCOPED_CLAUDE_HEADLESS_FLAGS),
                }
            }

        # --- Optional: model configuration ---
        print()
        want_models = input("Configure per-step model defaults? [y/N]: ").strip().lower()
        if want_models in ("y", "yes"):
            print("  (press Enter to accept the built-in default shown in brackets)")
            analyze_model = _prompt("  models.analyze", _DEFAULT_ANALYZE_MODEL)
            implement_model = _prompt("  models.implement", _DEFAULT_IMPLEMENT_MODEL)
            review_model = _prompt("  models.review", _DEFAULT_REVIEW_MODEL)
            cfg["models"] = {
                "analyze": analyze_model,
                "implement": implement_model,
                "review": review_model,
            }

        # --- Optional: feature flags ---
        print()
        want_flags = input("Enable feature flags? [y/N]: ").strip().lower()
        if want_flags in ("y", "yes"):
            flag_file = _prompt("flag_file", f"~/.{APP_NAME}/feature_flags.json")
            cfg["flag_file"] = flag_file

        # --- Optional: deployment pipeline ---
        print()
        want_envs = input("Enable deployment pipeline (environments)? [y/N]: ").strip().lower()
        if want_envs in ("y", "yes"):
            num_envs_raw = _prompt("Number of environments", "1")
            try:
                num_envs = int(num_envs_raw)
                if num_envs < 1:
                    raise ValueError
            except ValueError:
                print("  Invalid number — skipping environments.")
                num_envs = 0

            environments = []
            for i in range(num_envs):
                print(f"\n  Environment {i + 1}:")
                env_name = _prompt("    name", f"env{i + 1}")
                env_branch = _prompt("    branch", env_name)
                env_from = _prompt("    from (source branch)", detected_trunk_branch)
                env_trigger = _prompt("    trigger (manual/auto)", "manual")
                if env_trigger not in _VALID_TRIGGERS:
                    print("    Invalid trigger; using 'manual'.")
                    env_trigger = "manual"
                env_guard = _prompt("    guard_script (leave blank to skip)", "").strip() or None
                pre_raw = _prompt(
                    "    pre_promote scripts (comma-separated, blank to skip)", ""
                ).strip()
                pre_promote = [s.strip() for s in pre_raw.split(",") if s.strip()]
                post_raw = _prompt(
                    "    post_promote scripts (comma-separated, blank to skip)", ""
                ).strip()
                post_promote = [s.strip() for s in post_raw.split(",") if s.strip()]

                env_entry: dict = {
                    "name": env_name,
                    "branch": env_branch,
                    "from": env_from,
                    "trigger": env_trigger,
                }
                if env_guard:
                    # Hooks are argv lists, never bare strings — validate_hook rejects
                    # a string outright, so writing one here bricks every later command.
                    env_entry["guard_script"] = shlex.split(env_guard)
                if pre_promote:
                    env_entry["pre_promote"] = pre_promote
                if post_promote:
                    env_entry["post_promote"] = post_promote

                environments.append(env_entry)

            if environments:
                cfg["environments"] = environments

        # --- Optional: safeguards ---
        print()
        detected_runners = detect_test_runner_safeguards(repo_root)
        if detected_runners:
            runner_names = ", ".join(d.name for d in detected_runners)
            commands = [d.command for d in detected_runners]
            command_list = ", ".join(commands)
            want_safeguards = (
                input(
                    f"Detected {runner_names} -- configure pre_complete: "
                    f"[{command_list}], pre_merge: [{command_list}]? [Y/n]: "
                )
                .strip()
                .lower()
            )
            if want_safeguards in ("", "y", "yes"):
                cfg["safeguards"] = {
                    "pre_complete": commands,
                    "pre_merge": commands,
                }
        else:
            want_safeguards = (
                input("Configure ticket safeguards (pre_complete / pre_merge guards)? [y/N]: ")
                .strip()
                .lower()
            )
            if want_safeguards in ("y", "yes"):
                print("  Enter guard commands as a comma-separated list (blank to skip).")
                print("  Examples: pytest, scripts/run-tests.sh, cargo test, npm test")
                pre_complete_raw = _prompt(
                    "  pre_complete guards (run before marking code_complete)", ""
                ).strip()
                pre_complete = [s.strip() for s in pre_complete_raw.split(",") if s.strip()]
                pre_merge_raw = _prompt("  pre_merge guards (run before git merge)", "").strip()
                pre_merge = [s.strip() for s in pre_merge_raw.split(",") if s.strip()]

                safeguards: dict = {}
                if pre_complete:
                    safeguards["pre_complete"] = pre_complete
                if pre_merge:
                    safeguards["pre_merge"] = pre_merge
                if safeguards:
                    cfg["safeguards"] = safeguards

        # --- Optional: GitHub PR integration ---
        print()
        want_github_pr = (
            input("Auto-push branches and open GitHub PRs on approved review? [y/N]: ")
            .strip()
            .lower()
        )
        cfg["github_pr"] = want_github_pr in ("y", "yes")

    # --- Re-init safety: detect existing tickets in a non-default location ---
    # If a non-default directory (e.g. tickets/) exists and contains .md files,
    # preserve that tickets_dir rather than silently switching to .lanegate/tickets.
    proposed_tickets_dir = cfg.get("tickets_dir", f".{APP_NAME}/tickets")
    existing_tickets_dir, has_existing_tickets = _detect_existing_tickets_dir(
        repo_root, proposed_tickets_dir
    )
    if existing_tickets_dir is not None and has_existing_tickets:
        # Non-empty existing directory at a different location — warn and preserve it.
        print(
            f"\nWARNING: found existing tickets in '{existing_tickets_dir}' "
            f"(relative to repo root).",
            file=sys.stderr,
        )
        print(
            f"  tickets_dir will be set to '{existing_tickets_dir}' "
            f"to preserve your existing tickets.",
            file=sys.stderr,
        )
        print(
            "  To use the new default (.lanegate/tickets), migrate your tickets manually "
            "and re-run `lanegate init`.",
            file=sys.stderr,
        )
        cfg["tickets_dir"] = existing_tickets_dir
    elif existing_tickets_dir is not None and not has_existing_tickets:
        # Empty directory at a different location — silent update to new default is permitted.
        pass  # keep the proposed default

    # --- Write config ---
    config_path.write_text(yaml.dump(cfg, default_flow_style=False, sort_keys=False), encoding="utf-8")

    # --- Update .gitignore ---
    _update_gitignore(repo_root, cfg.get("tickets_dir", f".{APP_NAME}/tickets"))

    # --- Create directories ---
    tickets_dir_path = repo_root / cfg.get("tickets_dir", f".{APP_NAME}/tickets")
    worktrees_dir_path = repo_root / cfg.get("worktrees_dir", f".{APP_NAME}/worktrees")
    tickets_dir_path.mkdir(parents=True, exist_ok=True)
    worktrees_dir_path.mkdir(parents=True, exist_ok=True)

    # --- Register in global registry ---
    registry_add(repo_root)

    return cfg


# ---------------------------------------------------------------------------
# .gitignore helpers
# ---------------------------------------------------------------------------

def _gitignore_entries() -> list[str]:
    return [f".{APP_NAME}/*", f".{APP_NAME}.yml", f"{APP_NAME}-context-log.jsonl"]


def _update_gitignore(repo_root: Path, tickets_dir: str | None = None) -> None:
    """Append .lanegate/ and .lanegate.yml to .gitignore if not already present.

    Carves out tickets_dir (e.g. !.lanegate/tickets/ and !.lanegate/tickets/*) when
    tickets_dir sits under .lanegate/. Creates .gitignore if it doesn't exist.
    """
    gitignore_path = repo_root / ".gitignore"
    if gitignore_path.exists():
        existing = gitignore_path.read_text(encoding="utf-8")
    else:
        existing = ""

    entries = list(_gitignore_entries())
    if tickets_dir:
        norm = tickets_dir.strip("/")
        parts = Path(norm).parts
        if parts and parts[0] == f".{APP_NAME}":
            entries.extend([f"!{norm}/", f"!{norm}/*"])

    existing_lines = {line.strip() for line in existing.splitlines()}
    to_add = [entry for entry in entries if entry not in existing_lines]

    if not to_add:
        return  # all entries already present

    # Ensure there's a trailing newline before appending
    if existing and not existing.endswith("\n"):
        separator = "\n"
    else:
        separator = ""

    additions = "\n".join(to_add) + "\n"
    gitignore_path.write_text(existing + separator + additions, encoding="utf-8")


# ---------------------------------------------------------------------------
# Re-init safety helpers
# ---------------------------------------------------------------------------


def _detect_existing_tickets_dir(repo_root: Path, proposed: str) -> tuple[str | None, bool]:
    """Detect a pre-existing tickets directory at a non-default location.

    Checks common legacy locations (e.g. 'tickets/') for .md files.  Only
    reports a conflict when the candidate is different from *proposed*.

    Note: only the three canonical candidates below are probed.  Projects that
    stored tickets in an unusual path (e.g. 'tasks/', 'work/') will not be
    detected automatically; users with such setups should set tickets_dir
    explicitly in .lanegate.yml before running init.

    Returns:
        (relative_dir, has_md_files) where relative_dir is the path relative
        to repo_root, or (None, False) if no pre-existing directory is found.
    """
    # Candidate legacy locations to probe
    candidates = ["tickets", "issues", ".tickets"]
    for candidate in candidates:
        if candidate == proposed:
            continue  # same as what we're about to write — no conflict
        candidate_path = repo_root / candidate
        if candidate_path.is_dir():
            md_files = list(candidate_path.glob("*.md"))
            return candidate, bool(md_files)
    return None, False
