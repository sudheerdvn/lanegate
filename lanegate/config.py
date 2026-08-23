"""
config.py — load and validate .lanegate.yml (filename derived from APP_NAME).

Config discovery is anchored to the Git control checkout when invoked from a
repository or linked worktree. Outside Git repositories it walks up from cwd.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import stat
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

_VALID_MODEL_STEPS = {
    "analyze", "implement", "review", "fix", "drift_check", "review_escalation",
}
_VALID_EXECUTOR_STEPS = {"analyze", "implement", "review", "fix", "drift_check"}

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

# Matches this project's own tested executors.codex.flags rather than
# docs/troubleshooting.md's --approval-policy=never suggestion, which is
# unverified against current codex CLI releases.
_CODEX_HEADLESS_FLAGS = [
    "--dangerously-bypass-approvals-and-sandbox",
    "--ignore-user-config",
    "--ignore-rules",
    "--ephemeral",
]

# Built-in model defaults
_DEFAULT_ANALYZE_MODEL = "claude-haiku-4-5-20251001"
_DEFAULT_IMPLEMENT_MODEL = "claude-haiku-4-5-20251001"
_DEFAULT_REVIEW_MODEL = "claude-haiku-4-5-20251001"
_HIGH_REASONING_MODEL = "claude-opus-5"

# A deterministic control-plane classifier prevents the low-cost default from
# under-powering configuration, security, lifecycle, orchestration, and prompt
# trust work.  Category words alone occur in ordinary product tickets (for
# example, an application's lifecycle); require either an explicit label or a
# risk-oriented title. Explicit configured models remain higher-precedence
# overrides.
_HIGH_REASONING_TOPICS = (
    "configuration", "security", "lifecycle", "orchestration", "orchestrate",
    "prompt-trust", "prompt trust",
)
_HIGH_REASONING_LABELS = frozenset({
    "configuration", "security", "lifecycle", "orchestration", "prompt-trust",
    "prompt trust", "control-plane", "high-reasoning",
})
_HIGH_REASONING_TITLE_RISK_WORDS = (
    "harden", "hardening", "secure", "protect", "audit", "enforce", "trust",
    # Routine maintenance verbs still describe control-plane work when paired
    # with one of the explicit topics above.  Keep the topic requirement so a
    # product ticket that merely says "Exercise real lifecycle" is not
    # promoted, while "Fix lifecycle recovery" gets the required matrix and
    # high-reasoning route from the start of analysis.
    "fix", "repair", "update", "change", "migrate", "modify", "add", "remove",
)


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
_VALID_REVIEW_FALLBACKS = {"different_model", "same_model", "needs_review"}
_VALID_ACCEPTANCE_CONTRACT_MODES = {"blocker", "advisory"}
_VALID_PROFILES = {"default", "strict"}
_VALID_POOL_STRATEGIES = {"least-loaded", "round-robin"}

# Fields recognised on each drivers.<name> entry. 'type' is required; the rest
# are optional pass-through fields consumed by the executor dispatch layer.
_VALID_DRIVER_FIELDS = {"type", "model", "bin", "flags", "base_url", "provider"}

# Pipeline steps that may be routed to a named driver via steps:
_VALID_PIPELINE_STEPS = {"analyze", "implement", "review"}

# Valid edit_format values accepted by aider (and by model_settings overrides).
_VALID_AIDER_EDIT_FORMATS = {
    "whole", "diff", "diff-fenced", "udiff", "patch",
    "editor-diff", "editor-whole",
}

# Keys allowed inside a model_settings entry (same constraints as flat keys).
_VALID_AIDER_MODEL_SETTINGS_KEYS = {"context_window_tokens", "edit_format"}


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
        "profile": "default",
        "default_human_review": "none",
        # Do not silently turn an unavailable independent review into a
        # self-review.  Operators may opt into either of the less strict
        # alternatives below, but human escalation is the safe default.
        "review_fallback": "needs_review",
        "orphan_timeout_hours": 4,
        "executor_timeout_seconds": 1800,
        "executor_idle_timeout_seconds": 75,
        "executor_stall_timeout_seconds": 900,
        "executor_absolute_ceiling_seconds": 1500,
        "max_turns": None,
        "max_cumulative_tokens": None,
        "max_auto_fix_attempts": 1,
        "protected_paths": [],
        "control_plane_files": [],
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
        "reference_docs": [],
        "doc_update": {
            "doc_paths": ["README.md", "docs/ARCHITECTURE.md"],
            "status_filter": ["done"],
        },
    }


def _detect_origin_head_branch(repo_root: Path) -> str | None:
    """Return the branch Git's ``origin/HEAD`` symbolic ref points at, or
    ``None`` when undetectable (no remote configured, not a git repo, etc.).
    """
    try:
        detected = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True, encoding="utf-8",
        )
    except OSError:
        return None

    ref = detected.stdout.strip() if detected.returncode == 0 else ""
    prefix = "refs/remotes/origin/"
    if ref.startswith(prefix) and len(ref) > len(prefix):
        return ref.removeprefix(prefix)
    return None


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

    return _detect_origin_head_branch(repo_root) or "main"


def _current_git_branch(repo_root: Path) -> str | None:
    """Return the currently checked-out branch name, or ``None`` if unknown.

    Unlike ``resolve_trunk_branch`` (which follows ``origin/HEAD``, falling
    back to ``"main"`` when no remote default is configured), this reports
    what the repo is actually sitting on right now -- e.g. a project still on
    a local feature/refactor branch with no remote set up yet (TICK-645). Used
    only to suggest an ``init`` wizard default, never as a lifecycle base.
    """
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=repo_root,
            capture_output=True,
            text=True, encoding="utf-8",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    branch = result.stdout.strip() if result.returncode == 0 else ""
    return branch or None


def _recommend_aider_edit_format(repo_root: Path) -> tuple[str, str | None]:
    """Suggest an aider edit_format default from the largest tracked file.

    "whole" makes the model rewrite the entire file every turn: reliable on
    small files, but local models routinely truncate or hallucinate output
    past a few hundred lines -- a real incident hit exactly this on a
    ~1200-line file, output-token-exhausted mid-rewrite (TICK-645). "diff"
    avoids that at the cost of small local models sometimes emitting a
    malformed hunk instead (docs/executor-capabilities.md, aider "Known
    caveats") -- a real but distinct failure mode this cannot detect, so it
    only weighs in on the large-file risk, which is the one a fresh `init`
    run can actually observe in the repo as it stands today.

    Returns ``(recommended_format, note)`` -- *note* is ``None`` when nothing
    in the repo crosses the threshold (nothing to warn about either way).
    """
    from lanegate.executor import _repo_tracked_files

    # stat() every tracked file first (cheap metadata, no content read) and
    # only line-count the largest 3000 by byte size -- scanning the first
    # 3000 in `git ls-files` order (effectively tree/alphabetical order)
    # instead missed a large file sorting after that cutoff (e.g. under
    # vendor/... or zz_generated...) on any repo with >3000 tracked files,
    # silently recommending "whole" with no warning even though that file
    # would break it.
    sized: list[tuple[int, str]] = []
    for rel_path in _repo_tracked_files(repo_root):
        path = repo_root / rel_path
        try:
            size = path.stat().st_size
        except OSError:
            continue
        sized.append((size, rel_path))
    sized.sort(key=lambda item: item[0], reverse=True)

    # A tracked file at or above this size is such an outlier for a tracked
    # text file that opening and line-counting it is itself slow and risky
    # -- it may not even be text (a vendored binary or lockfile). Silently
    # excluding it from consideration (the prior behavior) meant the single
    # riskiest file in the repo could never be the one that triggers the
    # warning this function exists to give; recommend 'diff' directly from
    # its size instead of reading it.
    if sized and sized[0][0] > 2_000_000:
        size, rel_path = sized[0]
        note = (
            f"Detected `{rel_path}` at {size:,} bytes — too large to safely "
            "line-count, but 'whole' rewrites the entire file every turn "
            "and a file this size makes truncated or hallucinated output "
            "all but certain; defaulting to 'diff'. Note 'diff' has its "
            "own risk for small local models: they can emit a malformed "
            "hunk instead (see docs/executor-capabilities.md, aider "
            "\"Known caveats\")."
        )
        return "diff", note

    # sized is sorted largest-first, so the biggest candidates are examined
    # first; once any file crosses the 'diff' threshold below, further
    # line-counting can't change the recommendation, only which filename
    # gets cited -- stop there instead of opening and reading the rest of
    # up to 3000 files synchronously inside the interactive wizard.
    max_lines, max_path = 0, None
    for _size, rel_path in sized[:3000]:
        path = repo_root / rel_path
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = sum(1 for _ in f)
        except OSError:
            continue
        if lines > max_lines:
            max_lines, max_path = lines, rel_path
            if max_lines >= 300:
                break

    if max_lines >= 300:
        note = (
            f"Detected `{max_path}` at {max_lines} lines — 'whole' rewrites the "
            "entire file every turn, and local models routinely truncate or "
            "hallucinate content somewhere past ~300-500 lines of output; "
            "defaulting to 'diff'. Note 'diff' has its own risk for small "
            "local models: they can emit a malformed hunk instead (see "
            "docs/executor-capabilities.md, aider \"Known caveats\")."
        )
        return "diff", note
    return "whole", None



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


def _validate_reference_docs(cfg: dict) -> None:
    """Validate reference_docs configuration and emit DeprecationWarning for architecture_doc."""
    if "architecture_doc" in cfg:
        warnings.warn(
            "'architecture_doc' is deprecated; use 'reference_docs' instead",
            DeprecationWarning,
            stacklevel=2,
        )
    ref_docs = cfg.get("reference_docs")
    if ref_docs is not None and not isinstance(ref_docs, list):
        raise ConfigError("reference_docs must be a list")


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


def _validate_review_fallback(cfg: dict) -> None:
    value = cfg.get("review_fallback", "needs_review")
    if value not in _VALID_REVIEW_FALLBACKS:
        raise ConfigError(
            "review_fallback must be one of "
            f"{sorted(_VALID_REVIEW_FALLBACKS)}, got {value!r}"
        )
    if cfg.get("profile") == "strict" and value == "same_model":
        raise ConfigError(
            "review_fallback: same_model is incompatible with profile: strict — "
            "it is the one fallback that silently self-reviews when no independent "
            "reviewer or model is available, which strict mode exists to rule out. "
            "Use review_fallback: needs_review (the default) or different_model instead."
        )


def _validate_profile(cfg: dict) -> None:
    value = cfg.get("profile", "default")
    if value not in _VALID_PROFILES:
        raise ConfigError(f"profile must be one of {sorted(_VALID_PROFILES)}, got {value!r}")


def _validate_autonomy(cfg: dict) -> None:
    """Validate the optional top-level autonomy value and human_escalation triggers.

    ``autonomy`` accepts "full"/"supervised"/"manual" plus the risk-based
    lanes "green"/"yellow"/"red". ``human_escalation`` configures
    which risk signals force a red-lane escalation to a human regardless of
    the resolved autonomy: external credentials, security-sensitive or
    irreversible operations, and the auto-fix retry budget.
    """
    from lanegate.ticket import _VALID_AUTONOMY

    autonomy = cfg.get("autonomy")
    if autonomy is not None and autonomy not in _VALID_AUTONOMY:
        raise ConfigError(f"invalid autonomy '{autonomy}' — must be one of {sorted(_VALID_AUTONOMY)}")

    escalation = cfg.get("human_escalation")
    if escalation is not None:
        if not isinstance(escalation, dict):
            raise ConfigError("human_escalation must be a mapping")
        for key in ("credentials", "security_actions"):
            if key in escalation and not isinstance(escalation[key], bool):
                raise ConfigError(f"human_escalation.{key} must be a boolean")
        if "retry_limit" in escalation and not _is_positive_int(escalation["retry_limit"]):
            raise ConfigError("human_escalation.retry_limit must be a positive integer")


_DEFAULT_SESSION_CHAINING = {
    "enabled": True,
    "chain_review": False,
    "max_session_age_s": 2700,
    "max_session_tokens": 150000,
}


def resolve_session_chaining(cfg: dict) -> dict:
    """Return the effective session_chaining settings.

    Defaults are applied per-key, not as a whole-block fallback: `load_config`
    merges the raw YAML over `_default_config()` shallowly (other features
    share this pattern, e.g. `notify`/`project_guidance`), so a user's
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
    "gemini-3.5-flash-high",
    "gemini-3.5-flash-medium",
    "gemini-3.5-flash-low",
    "gemini-3.1-pro-high",
    "gemini-3.1-pro-low",
    "gemini-3.0-pro",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-pro",
    "gemini-1.5-pro",
    "gemini-1.5-flash",
    "gpt-oss-120b-medium",
}


def validate_model_for_executor(
    model: str,
    executor_type: str,
    context_label: str = "",
    *,
    agy_model_additions: set[str] | None = None,
    provider: str | None = None,
) -> None:
    """Validate model string against known valid model registries per executor type."""
    if not isinstance(model, str) or not model.strip():
        raise ConfigError(f"{context_label} model string must be a non-empty string")
    model = model.strip()
    if executor_type == "agy":
        known_agy_models = _KNOWN_AGY_MODELS | (agy_model_additions or set())
        if model not in known_agy_models and not (model.startswith("claude-") or model.startswith("anthropic/")):
            raise ConfigError(
                f"unmapped model '{model}' for executor '{executor_type}' in {context_label}. "
                f"Valid models for agy are: {sorted(known_agy_models)} or Claude models starting with 'claude-' or 'anthropic/'"
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
        valid_prefixes: tuple[str, ...]
        if provider == "ollama":
            # An aider instance pinned to the Ollama provider can only reach
            # models Ollama actually serves -- a claude-*/gpt-*/gemini-* name
            # is not a legitimate multi-provider choice here, it's a
            # misconfiguration (e.g. a top-level `models:` block authored for
            # a different executor leaking into this one via pool dispatch).
            valid_prefixes = ("ollama",)
        else:
            valid_prefixes = ("claude-", "gpt-", "o1", "o3", "ollama", "deepseek", "gemini", "anthropic/", "openai/")
        if not any(model.startswith(p) for p in valid_prefixes):
            raise ConfigError(
                f"unmapped model '{model}' for executor '{executor_type}' in {context_label}."
            )


def _validate_models(cfg: dict, *, agy_model_additions: set[str] | None = None) -> None:
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
        if step == "analyze":
            continue
        if isinstance(model_str, str):
            ex_name = resolve_executor(cfg, step)
            ex_cfg = executors.get(ex_name) if isinstance(executors, dict) else None
            ex_type = ex_cfg.get("type", ex_name) if isinstance(ex_cfg, dict) else ex_name
            ex_provider = ex_cfg.get("provider") if isinstance(ex_cfg, dict) else None
            validate_model_for_executor(
                model_str,
                ex_type,
                f"models.{step}",
                agy_model_additions=agy_model_additions,
                provider=ex_provider,
            )

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
        ex_provider = ex_cfg.get("provider")
        for step, model_str in ex_models.items():
            if step == "analyze":
                continue
            if isinstance(model_str, str):
                validate_model_for_executor(
                    model_str,
                    ex_type,
                    f"executors['{ex_name}'].models.{step}",
                    agy_model_additions=agy_model_additions,
                    provider=ex_provider,
                )
        ex_model = ex_cfg.get("model")
        if isinstance(ex_model, str):
            validate_model_for_executor(
                ex_model,
                ex_type,
                f"executors['{ex_name}'].model",
                agy_model_additions=agy_model_additions,
                provider=ex_provider,
            )


def _parse_drivers(
    raw: dict, valid_types: set[str], *, agy_model_additions: set[str] | None = None
) -> dict:
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
            validate_model_for_executor(
                model,
                driver_type,
                f"drivers['{name}'].model",
                agy_model_additions=agy_model_additions,
                provider=provider,
            )
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
    """Validate named executor instances under executors:.

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
    (legacy syntax) — the entry's own key IS the executor type
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


def _validate_aider_model_settings(cfg: dict) -> None:
    """Validate executors.aider.model_settings (optional per-model override block).

    Each key is a model string; each value must be a mapping of the same
    constraint-bounded keys that the flat aider executor config accepts:
    - context_window_tokens: positive integer
    - edit_format: non-empty string from _VALID_AIDER_EDIT_FORMATS

    Unknown sub-keys are rejected to match the flat-key validator's behaviour
    in executor.py (which raises ConfigError for invalid edit_format/context types).
    """
    executors = cfg.get("executors") or {}
    if not isinstance(executors, dict):
        return

    # Resolve the aider executor entry — may be keyed by 'aider' (legacy flat
    # override) or by a named instance whose 'type' is 'aider'.
    aider_cfgs: list[tuple[str, dict]] = []
    for name, entry in executors.items():
        if not isinstance(entry, dict):
            continue
        exec_type = entry.get("type", name)
        if exec_type == "aider":
            aider_cfgs.append((name, entry))

    for executor_name, aider_cfg in aider_cfgs:
        model_settings = aider_cfg.get("model_settings")
        if model_settings is None:
            continue
        if not isinstance(model_settings, dict):
            raise ConfigError(
                f"executors['{executor_name}'].model_settings must be a mapping "
                "of model-string → settings"
            )
        for model_key, overrides in model_settings.items():
            if not isinstance(model_key, str):
                raise ConfigError(
                    f"executors['{executor_name}'].model_settings keys must be strings"
                )
            if not isinstance(overrides, dict):
                raise ConfigError(
                    f"executors['{executor_name}'].model_settings[{model_key!r}] "
                    "must be a mapping"
                )
            unknown = set(overrides.keys()) - _VALID_AIDER_MODEL_SETTINGS_KEYS
            if unknown:
                raise ConfigError(
                    f"executors['{executor_name}'].model_settings[{model_key!r}]: "
                    f"unknown key(s) {sorted(unknown)} — "
                    f"valid keys are {sorted(_VALID_AIDER_MODEL_SETTINGS_KEYS)}"
                )
            ctx = overrides.get("context_window_tokens")
            if ctx is not None:
                if not isinstance(ctx, int) or isinstance(ctx, bool) or ctx <= 0:
                    raise ConfigError(
                        "executors.aider.context_window_tokens must be a positive integer"
                    )
            ef = overrides.get("edit_format")
            if ef is not None:
                if not isinstance(ef, str) or not ef:
                    raise ConfigError(
                        "executors.aider.edit_format must be a non-empty string"
                    )
                if ef not in _VALID_AIDER_EDIT_FORMATS:
                    raise ConfigError(
                        f"executors.aider.model_settings[{model_key!r}].edit_format "
                        f"{ef!r} is not a valid aider edit format; "
                        f"valid values are {sorted(_VALID_AIDER_EDIT_FORMATS)}"
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


def _validate_safeguards(cfg: dict) -> None:
    """Validate safeguard settings that affect lifecycle gate semantics."""
    safeguards = cfg.get("safeguards") or {}
    if not isinstance(safeguards, dict):
        raise ConfigError("safeguards must be a mapping")
    value = safeguards.get("pre_merge_worktree", True)
    if not isinstance(value, bool):
        raise ConfigError(
            f"safeguards.pre_merge_worktree must be a boolean, got {value!r}"
        )


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


def _validate_tree_sitter_languages(cfg: dict) -> None:
    """Validate and register project-declared tree-sitter language mappings.

    ``tree_sitter_languages`` in .lanegate.yml lets a project add a language
    LaneGate has no built-in mapping for (e.g. Vue, Elixir, Zig) -- as long
    as the matching `tree-sitter-<lang>` package is pip-installed -- without
    waiting on a LaneGate release. Registered once here, the single early
    chokepoint every command already passes through via load_config(), so
    the parse chain deep in analyze.py stays cfg-agnostic.
    """
    extra = cfg.get("tree_sitter_languages")
    if extra is None:
        return
    if not isinstance(extra, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in extra.items()
    ):
        raise ConfigError(
            "tree_sitter_languages must be a mapping of file extension (e.g. '.vue') "
            "to tree-sitter module name (e.g. 'tree_sitter_vue')"
        )
    from lanegate.analyze import register_tree_sitter_languages

    register_tree_sitter_languages(extra)


def _validate_acceptance_contract_mode(cfg: dict) -> None:
    mode = cfg.get("acceptance_contract_mode")
    if mode is not None and mode not in _VALID_ACCEPTANCE_CONTRACT_MODES:
        raise ConfigError(
            f"acceptance_contract_mode must be one of {sorted(_VALID_ACCEPTANCE_CONTRACT_MODES)}, got {mode!r}"
        )


def _validate_budget_caps(cfg: dict) -> None:
    """Validate optional max_turns and max_cumulative_tokens budget caps."""
    for key in ("max_turns", "max_cumulative_tokens"):
        value = cfg.get(key)
        if value is None:
            continue
        if isinstance(value, dict):
            unknown = set(value.keys()) - _VALID_EXECUTOR_STEPS
            if unknown:
                raise ConfigError(
                    f"unknown key(s) under {key}: {sorted(unknown)} — "
                    f"valid steps are {sorted(_VALID_EXECUTOR_STEPS)}"
                )
            for step_key, step_val in value.items():
                if not isinstance(step_val, int) or isinstance(step_val, bool) or step_val <= 0:
                    raise ConfigError(f"{key}['{step_key}'] must be a positive integer, got {step_val!r}")
        elif not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"{key} must be a positive integer, got {value!r}")


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
    """Validate the `pools:` block and `default_pool`.

    pools:
      default:
        executors: [claude-1, claude-2]   # named instances from executors:
        strategy: least-loaded            # or round-robin (default: least-loaded)

    Each named executor listed in a pool must already exist under `executors:`
    (as either a named instance or a plain per-type entry).
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
    """Validate the `routing:` block.

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


_DEFAULT_HUMAN_ESCALATION = {
    "credentials": True,
    "security_actions": True,
    "retry_limit": 3,
}

# Autonomy values that stay on the automatic amend/re-analyze -> fix ->
# re-review path without pausing for a human merge decision.
_AUTO_FIX_LANES = frozenset({"full", "green", "yellow"})


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
    if implement != review:
        return

    from lanegate.orchestrate.autofix import combined_mode_capable

    if not combined_mode_capable(implement, cfg):
        return

    warnings.warn(
        f"reviewer: {review!r} resolves identically to the implement executor "
        f"{implement!r} — review will run in combined (self-review) mode, not "
        "the independent review pipeline.",
        stacklevel=2,
    )


def _literal_string_values(node: ast.AST) -> set[str] | None:
    """Return literal strings from a set/list/tuple AST node, if fully static."""
    if not isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        return None
    values: set[str] = set()
    for element in node.elts:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            return None
        values.add(element.value)
    return values


def _worktree_agy_model_additions(config_module: Path) -> set[str]:
    """Read literal ``_KNOWN_AGY_MODELS`` additions without importing untrusted code."""
    try:
        module = ast.parse(config_module.read_text(encoding="utf-8"), filename=str(config_module))
    except (OSError, SyntaxError) as exc:
        raise ConfigError(f"could not read worktree validator {config_module}: {exc}") from exc

    additions: set[str] = set()
    for statement in module.body:
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            continue
        call = statement.value
        if (
            not isinstance(call.func, ast.Attribute)
            or not isinstance(call.func.value, ast.Name)
            or call.func.value.id != "_KNOWN_AGY_MODELS"
            or call.func.attr not in {"add", "update"}
            or len(call.args) != 1
            or call.keywords
        ):
            continue
        if call.func.attr == "add" and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
            additions.add(call.args[0].value)
        elif call.func.attr == "update":
            values = _literal_string_values(call.args[0])
            if values is not None:
                additions.update(values)
    return additions


def load_worktree_config(worktree_path: Path) -> dict:
    """Load worktree YAML with this process's validator and static model additions.

    A ticket worktree is untrusted, so its Python must never be imported or
    executed on the control plane.  The only bootstrap compatibility supported
    here is a literal ``_KNOWN_AGY_MODELS.add(...)``/``.update(...)`` extension,
    which is read from the AST and passed to the trusted validator.
    """
    wt = Path(worktree_path).resolve()
    config_module = wt / "lanegate" / "config.py"
    additions = _worktree_agy_model_additions(config_module) if config_module.exists() else set()
    return load_config(wt, agy_model_additions=additions)


def _trusted_git_executable() -> str:
    """Return a protected Git executable found through absolute PATH entries.

    We cannot hard-code ``/usr/bin/git``: supported installations commonly
    place Git in a protected nonstandard prefix (for example an enterprise
    toolchain under ``/opt``).  PATH is therefore only an *index* into
    candidate locations, never a trust decision.  On POSIX the resolved
    executable and every containing directory must be owned by someone other
    than the LaneGate process and not writable by group or other users.  (The
    owner is normally root; comparing against the effective user also works
    inside user namespaces which map host-root files to an overflow uid.)  A
    worktree-controlled ``PATH`` entry, an empty entry (the current directory),
    or a relative entry cannot pass that test.  Resolve before returning so a
    mutable PATH directory cannot swap a symlink between validation and
    execution.

    On Windows, candidate locations come only from machine-wide installer
    registry entries (plus the conventional machine install).  In
    particular, neither the caller's PATH nor its current directory is ever
    searched.  This supports an administrator-installed custom prefix such
    as ``D:\\Tools\\Git`` without treating an agent-controlled per-user
    installation as authoritative.
    """
    if os.name == "nt":
        candidates = _windows_git_candidates()
    else:
        candidates = tuple(
            Path(entry) / "git"
            for entry in os.environ.get("PATH", "").split(os.pathsep)
            if entry and Path(entry).is_absolute()
        )

    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            metadata = resolved.stat()
        except OSError:
            continue
        if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
            continue
        if os.name != "nt" and not _is_protected_executable(resolved):
            continue
        return str(resolved)
    raise ConfigError("unable to determine a trusted Git control checkout")


def _windows_git_candidates() -> tuple[Path, ...]:
    """Return Git paths registered by the machine-wide Windows installer.

    ``HKLM`` is intentionally the only registry hive consulted: HKCU and
    environment variables are writable by the account running a ticket, so
    they cannot establish a trusted executable.  Git for Windows records
    either an App Paths executable or an installation directory in these
    locations.  The conventional Program Files path is retained for older
    installers that did not create a registry entry.
    """
    candidates: list[Path] = []
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        winreg = None  # type: ignore[assignment]

    if winreg is not None:
        views = [0]
        for flag_name in ("KEY_WOW64_64KEY", "KEY_WOW64_32KEY"):
            flag = getattr(winreg, flag_name, 0)
            if flag and flag not in views:
                views.append(flag)

        def machine_value(key_name: str, value_name: str | None) -> str | None:
            for view in views:
                try:
                    with winreg.OpenKey(  # type: ignore[attr-defined]
                        winreg.HKEY_LOCAL_MACHINE,  # type: ignore[attr-defined]
                        key_name,
                        0,
                        winreg.KEY_READ | view,  # type: ignore[attr-defined]
                    ) as key:
                        value, _ = winreg.QueryValueEx(key, value_name)  # type: ignore[attr-defined,arg-type]
                except OSError:
                    continue
                if isinstance(value, str) and value:
                    return value
            return None

        app_path = machine_value(
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\git.exe", None
        )
        if app_path:
            candidates.append(Path(app_path))
        for value_name in ("InstallPath", "InstallPath64", "Path"):
            install_path = machine_value(r"SOFTWARE\GitForWindows", value_name)
            if install_path:
                install = Path(install_path)
                candidates.extend((install / "cmd" / "git.exe", install / "bin" / "git.exe"))

    candidates.extend(
        (
            Path(r"C:\\Program Files\\Git\\cmd\\git.exe"),
            Path(r"C:\\Program Files\\Git\\bin\\git.exe"),
        )
    )
    # A registry entry can appear in both registry views.  Preserve order so
    # the installed path wins over the conventional fallback.
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).casefold()
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return tuple(unique)


def _is_protected_executable(path: Path) -> bool:
    """Whether *path* and every containing directory resist agent mutation."""
    effective_uid = os.geteuid()  # type: ignore[attr-defined]
    for ancestor in (path, *path.parents):
        try:
            metadata = ancestor.stat()
        except OSError:
            return False
        # Root has no meaningful ownership boundary from its effective uid:
        # root-owned system binaries are the expected trusted installation.
        # The non-writable mode requirement still prevents group/other users
        # from replacing any component in the executable path.
        if (
            (effective_uid != 0 and metadata.st_uid == effective_uid)
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            return False
    return True


def _control_checkout_root(start: Path) -> Path | None:
    """Return the shared control checkout root for *start*, if it is in Git.

    Linked worktrees have their own checkout root but share a common Git
    directory with the control checkout.  Lifecycle commands must use the
    latter's configuration: a worktree is controlled by an agent and may
    contain an uncommitted, locally planted config file.
    """
    git = _trusted_git_executable()
    # This probe never contacts a remote, and explicitly disallows an
    # interactive credential prompt should a local Git wrapper/config attempt
    # to cause one.
    # Git environment variables can redirect even an absolute Git executable
    # to an attacker-selected repository (for example GIT_DIR) or make it
    # stop walking at a worktree boundary (GIT_CEILING_DIRECTORIES).  Strip
    # all of them rather than trusting the executor's environment.
    probe_env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    probe_env["GIT_TERMINAL_PROMPT"] = "0"
    # The non-repository result below is Git's stable C-locale diagnostic.
    # Pin it so standalone discovery does not depend on the caller's locale.
    probe_env["LC_ALL"] = "C"
    try:
        result = subprocess.run(
            [git, "-C", str(start), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=False,
            env=probe_env,
        )
    except OSError as exc:
        # Failing open here would make a worktree-local config authoritative.
        # In particular, an agent could shadow ``git`` on PATH and arrange for
        # just this probe to fail before invoking a lifecycle command.
        raise ConfigError("unable to determine a trusted Git control checkout") from exc
    if result.returncode != 0:
        # Standalone directories retain walk-up discovery.  A real Git binary
        # reports this specific condition when ``start`` is outside a
        # repository; every other failure is ambiguous and must fail closed.
        stderr = result.stderr.lower()
        if "not a git repository" in stderr:
            return None
        raise ConfigError("unable to determine a trusted Git control checkout")

    # --path-format=absolute was added after Git 2.25, so do not require it
    # for the control-plane boundary.  Only the final record terminator is
    # removable: a newline anywhere else would make the path ambiguous.
    common_dir_text = result.stdout.removesuffix("\n")
    if not common_dir_text or "\n" in common_dir_text or "\r" in common_dir_text:
        raise ConfigError("unable to determine a trusted Git control checkout")
    common_dir = Path(common_dir_text)
    if not common_dir.is_absolute():
        common_dir = start / common_dir
    common_dir = common_dir.resolve()

    # The normal form is <control checkout>/.git, including linked worktrees.
    # Submodules and separate Git directories instead use a common directory
    # such as .git/modules/<name>.  Their trusted primary worktree is recorded
    # in that common directory's core.worktree setting.
    if common_dir.name == ".git":
        return common_dir.parent

    try:
        primary_result = subprocess.run(
            [git, f"--git-dir={common_dir}", "config", "--get", "core.worktree"],
            capture_output=True,
            text=True,
            check=False,
            env=probe_env,
        )
    except OSError as exc:
        raise ConfigError("unable to determine a trusted Git control checkout") from exc
    if primary_result.returncode != 0:
        raise ConfigError("unable to determine a trusted Git control checkout")

    primary_text = primary_result.stdout.removesuffix("\n")
    if not primary_text or "\n" in primary_text or "\r" in primary_text:
        raise ConfigError("unable to determine a trusted Git control checkout")
    primary = Path(primary_text)
    return (primary if primary.is_absolute() else common_dir / primary).resolve()


def find_config(start: Path | None = None) -> Path | None:
    """Find the trusted project config, or None when no config exists.

    In Git repositories this considers only the shared control checkout's
    top-level config, never a config planted in a linked worktree or subdir.
    The historical walk-up behavior is retained for non-Git directories so
    ``lanegate init`` and standalone config discovery remain usable.
    """
    here = (start or Path.cwd()).resolve()
    control_root = _control_checkout_root(here)
    if control_root is not None:
        candidate = control_root / CONFIG_FILENAME
        return candidate if candidate.exists() else None
    for directory in [here, *here.parents]:
        candidate = directory / CONFIG_FILENAME
        if candidate.exists():
            return candidate
    return None


def load_config(
    repo_root: Path | None = None, *, agy_model_additions: set[str] | None = None
) -> dict:
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
    cfg["drivers"] = _parse_drivers(
        raw, valid_types=_VALID_EXECUTOR_TYPES, agy_model_additions=agy_model_additions
    )
    cfg["steps"] = _parse_steps(raw, drivers=cfg["drivers"], valid_types=_VALID_EXECUTOR_TYPES)

    _validate_environments(cfg["environments"])
    _validate_concurrency(cfg)
    _validate_executor_instances(cfg)
    _validate_aider_model_settings(cfg)
    _validate_pools(cfg)
    _validate_routing(cfg)
    _validate_executor(cfg)
    _validate_reviewer(cfg)
    _validate_profile(cfg)
    _validate_review_fallback(cfg)
    _validate_acceptance_contract_mode(cfg)
    _validate_autonomy(cfg)
    _validate_auto_fix(cfg)
    _validate_models(cfg, agy_model_additions=agy_model_additions)
    _validate_project_guidance(cfg)
    _validate_executor_steps(cfg)
    _validate_orphan_timeout(cfg)
    _validate_safeguards(cfg)
    _validate_executor_timeouts(cfg)
    _validate_budget_caps(cfg)
    _validate_verification(cfg)
    _validate_rate_limit(cfg)
    _validate_default_human_review(cfg)
    _validate_display_timezone(cfg)
    _validate_session_chaining(cfg)
    _validate_doc_update(cfg)
    _validate_reference_docs(cfg)
    _validate_tree_sitter_languages(cfg)
    _warn_if_combined_mode_collapse(cfg)
    return cfg


def repo_root_from_config(cfg_path: Path) -> Path:
    return cfg_path.parent


def find_repo_root(start: Path | None = None) -> Path:
    """Find the trusted control root; fall back to config discovery or cwd."""
    here = (start or Path.cwd()).resolve()
    control_root = _control_checkout_root(here)
    if control_root is not None:
        return control_root
    config_path = find_config(here)
    if config_path:
        return config_path.parent
    return here


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


_stdin_exhausted_warned = False


def _warn_stdin_exhausted_once() -> None:
    """Print a one-time warning the first time stdin runs out mid-wizard.

    Degrading silently to defaults (see the EOFError handlers below) fixed a
    raw traceback on legitimate exhausted stdin, but it also means a piped
    answer string with the wrong number of lines no longer fails loudly --
    every prompt past the last real answer just quietly takes its default
    with no signal anything went wrong. Confirmed live in a fresh-install
    smoke test. This restores that signal without reintroducing the crash.
    """
    global _stdin_exhausted_warned
    if _stdin_exhausted_warned:
        return
    _stdin_exhausted_warned = True
    print(
        "WARNING: stdin ran out mid-wizard -- every remaining prompt is using its "
        "default instead of an answer you provided. If you piped in a fixed answer "
        "string, double check its line count before trusting the .lanegate.yml this "
        "writes (or re-run interactively, or with --defaults).",
        file=sys.stderr,
    )


def _prompt(prompt_text: str, default: str) -> str:
    """Prompt the user with a default shown in brackets; empty input returns default.

    Piped/non-interactive stdin that runs out mid-wizard degrades to the
    default (EOFError -> blank) instead of raising a raw traceback.
    """
    try:
        raw = input(f"{prompt_text} [{default}]: ").strip()
    except EOFError:
        _warn_stdin_exhausted_once()
        raw = ""
    return raw if raw else default


def _prompt_raw(prompt_text: str, default: str, *, display_default: str) -> tuple[str, str]:
    """Single-purpose variant of ``_prompt`` for the reviewer prompt below.

    Returns (resolved, raw_input) so the caller can distinguish "left this
    blank" from "typed a value that happens to equal the default" -- a blank
    reviewer answer must NOT write an explicit config pin (see its call site).

    display_default overrides only what's shown in the brackets, leaving the
    actual blank-input resolution untouched: showing the true fallback value
    in brackets would look like a normal default that Enter accepts, when
    accepting it actually behaves differently (a blank reviewer answer
    resolves at dispatch time instead of being written to config the way a
    typed answer, even one matching the fallback, would be).
    """
    try:
        raw = input(f"{prompt_text} [{display_default}]: ").strip()
    except EOFError:
        _warn_stdin_exhausted_once()
        raw = ""
    return (raw if raw else default), raw


def _prompt_yes_no(prompt_text: str, *, default: bool = False) -> bool:
    """Yes/no wizard prompt. EOF (stdin exhausted) degrades to ``default``
    instead of raising, matching ``_prompt``'s EOF handling above."""
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{prompt_text} {suffix}: ").strip().lower()
    except EOFError:
        _warn_stdin_exhausted_once()
        return default
    return default if not answer else answer in ("y", "yes")


def _project_mentions_pytest(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        return "pytest" in path.read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False


def _npm_test_detection(path: Path) -> TestRunnerDetection | None:
    """Detect an npm-based test runner, suggesting a CI-safe non-interactive
    command when package.json signals a framework whose default `test`
    script launches a watch-mode/interactive session (CRA's Jest watch mode,
    Angular CLI's Karma browser session) that would otherwise hang until
    safeguards.py's timeout_s.
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    scripts = data.get("scripts")
    if not isinstance(scripts, dict) or not scripts.get("test"):
        return None

    deps: dict = {}
    for key in ("dependencies", "devDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            deps.update(section)

    if "react-scripts" in deps:
        return TestRunnerDetection("npm test (Create React App)", "CI=true npm test")

    if "@angular/cli" in deps:
        return TestRunnerDetection("npm test (Angular)", "ng test --watch=false")

    return TestRunnerDetection("npm test", "npm test")


def detect_test_runner_safeguards(repo_root: Path) -> list[TestRunnerDetection]:
    """Detect common project test runners and return concrete safeguard commands."""

    detections: list[TestRunnerDetection] = []

    if (
        _project_mentions_pytest(repo_root / "pyproject.toml")
        or _project_mentions_pytest(repo_root / "setup.cfg")
        or any((repo_root / "tests").glob("test_*.py"))
    ):
        detections.append(TestRunnerDetection("pytest", "pytest"))

    npm_detection = _npm_test_detection(repo_root / "package.json")
    if npm_detection is not None:
        detections.append(npm_detection)

    if (repo_root / "Cargo.toml").exists():
        detections.append(TestRunnerDetection("cargo test", "cargo test"))

    if (repo_root / "go.mod").exists():
        detections.append(TestRunnerDetection("go test", "go test"))

    if (repo_root / "pom.xml").exists():
        detections.append(TestRunnerDetection("mvn test", "mvn test"))

    if (repo_root / "build.gradle").exists() or (repo_root / "build.gradle.kts").exists():
        detections.append(TestRunnerDetection("./gradlew test", "./gradlew test"))

    return detections


def suggested_safeguards_yaml(detections: list[TestRunnerDetection]) -> str:
    """Return the YAML block LaneGate suggests for detected test runners."""

    commands = [d.command for d in detections]
    lines = ["safeguards:", "  pre_complete:"]
    lines.extend(f"    - {command}" for command in commands)
    lines.append("  pre_merge:")
    lines.extend(f"    - {command}" for command in commands)
    return "\n".join(lines)


def _init_core_options(repo_root: Path) -> tuple[dict, str, str, bool]:
    """Prompt for ticket_prefix/tickets_dir/worktrees_dir/executor/reviewer/max_parallel."""
    print("\nConfiguring core options (press Enter to accept the default):\n")
    ticket_prefix = _prompt("ticket_prefix", "TICK")
    tickets_dir = _prompt("tickets_dir", f".{APP_NAME}/tickets")
    worktrees_dir = _prompt("worktrees_dir", f".{APP_NAME}/worktrees")
    print(f"  (valid: {', '.join(sorted(_VALID_EXECUTOR_TYPES))})")
    executor_raw = _prompt("executor", "claude")
    # Validate executor; fall back to claude on invalid input
    executor = executor_raw if executor_raw in _VALID_EXECUTOR_TYPES else "claude"
    if executor != executor_raw:
        print(
            f"  Warning: '{executor_raw}' is not a recognised executor; "
            f"using 'claude'. (Valid: {sorted(_VALID_EXECUTOR_TYPES)})"
        )
    print(f"  (valid: {', '.join(sorted(_VALID_REVIEWERS))})")
    print(
        f"  Tip: pick a reviewer different from executor ('{executor}') for an "
        "independent review. Leave this blank to let LaneGate decide at "
        "dispatch time (uses a different pool instance/model when one is "
        "available, otherwise escalates to a human review gate rather than "
        "silently self-reviewing) -- typing a value here, even one matching "
        "the executor, pins it and always wins over that safe fallback."
    )
    reviewer_raw, reviewer_input = _prompt_raw(
        "reviewer", executor, display_default="auto"
    )
    reviewer = reviewer_raw if reviewer_raw in _VALID_REVIEWERS else executor
    if reviewer_input and reviewer != reviewer_raw:
        print(
            f"  Warning: '{reviewer_raw}' is not a recognised reviewer; "
            f"treating it like a blank answer (auto). (Valid: {sorted(_VALID_REVIEWERS)})"
        )
    # A blank prompt -- or an unrecognized (typo'd) one -- must not
    # silently become an explicit reviewer pin: cfg["reviewer"] is set
    # conditionally below, only when the user typed a value that's
    # actually a real reviewer choice (see that comment for why this
    # matters). A typo pinning self-review the same way a deliberate
    # match does would be exactly the footgun the blank case was fixed
    # for, just triggered by a mistake instead of an empty Enter.
    reviewer_explicit = bool(reviewer_input) and reviewer_input in _VALID_REVIEWERS
    if reviewer_explicit and reviewer == executor:
        print(
            f"  Note: reviewer == executor ('{executor}') — review will run in "
            "combined (self-review) mode, not the independent review pipeline."
        )
    max_parallel_raw = _prompt("max_parallel", "2")
    try:
        max_parallel = int(max_parallel_raw)
        if max_parallel < 1:
            raise ValueError
    except ValueError:
        print("  Invalid max_parallel — using 2.")
        max_parallel = 2

    cfg: dict = {
        "ticket_prefix": ticket_prefix,
        "tickets_dir": tickets_dir,
        "worktrees_dir": worktrees_dir,
        "executor": executor,
        "max_parallel": max_parallel,
        "commit_status_changes": True,
    }
    # Only pin reviewer: when the user actually typed something at the
    # prompt above (even a value matching executor) -- a blank/default
    # answer leaves it unset so resolve_independent_review_driver's
    # ladder runs at dispatch time instead of being permanently bypassed.
    # An explicit pin "always wins outright" over that ladder (see its
    # docstring), including the review_fallback: needs_review safety
    # escalation that would otherwise apply to an unconfigured
    # single-account setup -- accepting a blank prompt must not disable
    # that safety net the same way a deliberate, informed pin does.
    if reviewer_explicit:
        cfg["reviewer"] = reviewer

    return cfg, executor, reviewer, reviewer_explicit


def _init_trunk_branch(cfg: dict, repo_root: Path) -> None:
    """Prompt for trunk_branch, defaulting to detected origin/HEAD or current branch."""
    # A real origin/HEAD detection wins over the currently checked-out
    # branch: a cloned repo with a remote configured (origin/HEAD ->
    # main) but currently sitting on a local feature/WIP branch during
    # `init` should default to "main", not that feature branch. Only
    # fall back to the checked-out branch when origin/HEAD can't be
    # determined at all (a fresh local project with no remote set up
    # yet, TICK-645) -- resolve_trunk_branch()'s own hardcoded "main"
    # fallback in that case would be blind to a project that isn't
    # actually using "main" as its trunk name.
    origin_head_branch = _detect_origin_head_branch(repo_root)
    current_branch = _current_git_branch(repo_root)
    trunk_branch = _prompt(
        "trunk_branch", origin_head_branch or current_branch or "main"
    )
    cfg["trunk_branch"] = trunk_branch


def _init_headless_flags(cfg: dict, executor: str, reviewer: str) -> None:
    """Write required unattended-run flags for whichever of executor/reviewer needs them."""
    # Without these, the tool blocks on an interactive prompt and an
    # unattended run just hangs instead of failing (see
    # docs/troubleshooting.md "The agent hangs and never finishes").
    _CLAUDE_TYPES = {"claude", "claude-subagent", "claude-process"}
    _headless_types = {
        t
        for t in (executor, reviewer)
        if t in _CLAUDE_TYPES or t in ("aider", "codex", "agy")
    }
    for _t in sorted(_headless_types):
        if _t in _CLAUDE_TYPES:
            print()
            print(f"Note: {_t} requires headless flags for unattended runs.")
            print("These are already pre-configured for you with a scoped permission set")
            print("(--allowedTools), rather than --dangerously-skip-permissions.")
            cfg.setdefault("executors", {}).setdefault(_t, {})["flags"] = list(
                _SCOPED_CLAUDE_HEADLESS_FLAGS
            )
        elif _t == "aider":
            print()
            print("Note: aider requires --yes-always for unattended runs (auto-confirms")
            print("its interactive prompts); --no-gitignore stops it editing .gitignore.")
            cfg.setdefault("executors", {}).setdefault("aider", {})["flags"] = [
                "--yes-always",
                "--no-gitignore",
            ]
        elif _t == "codex":
            print()
            print("Note: codex requires approval/sandbox bypass flags for unattended runs.")
            cfg.setdefault("executors", {}).setdefault("codex", {})["flags"] = list(
                _CODEX_HEADLESS_FLAGS
            )
        elif _t == "agy":
            print()
            print("Note: agy requires --dangerously-skip-permissions for unattended runs")
            print("(tool executions would otherwise block on interactive prompts), and")
            print("--disable-slash-commands so agy doesn't interpret '/'-prefixed prompt")
            print("content (e.g. ticket text) as its own CLI commands.")
            cfg.setdefault("executors", {}).setdefault("agy", {})["flags"] = [
                "--dangerously-skip-permissions",
                "--disable-slash-commands",
            ]


def _init_autonomy(cfg: dict) -> None:
    """Prompt for pipeline autonomy (full vs supervised)."""
    # resolve_autonomy() already defaults to "supervised" (pause for a
    # manual `lanegate merge`) when this is left unset -- a deliberate
    # safety default, not a bug. But the wizard never offered a way to
    # opt into unattended "full" autonomy either, so every fresh project
    # silently got supervised with no indication another option existed
    # (TICK-645). Leaving option 2 unwritten preserves that same safe
    # default; only an explicit "full" choice changes anything.
    print()
    print("Pipeline autonomy:")
    print("  [1] full — unattended: auto-merge on an approved review")
    print("  [2] supervised — pause at each ticket for a manual `lanegate merge` (default)")
    autonomy_choice = _prompt("autonomy", "2")
    if autonomy_choice == "1":
        cfg["autonomy"] = "full"
        cfg["default_human_review"] = "none"
    elif autonomy_choice not in ("2", ""):
        print(f"  Invalid choice {autonomy_choice!r} — using supervised.")


def _init_models(
    cfg: dict, executor: str, reviewer: str, reviewer_explicit: bool, repo_root: Path
) -> dict:
    """Prompt for models.analyze/implement/fix/review/drift_check, including Ollama discovery."""
    # Always shown (not gated behind a y/N) so the resulting .lanegate.yml
    # states exactly which model each step will use instead of leaving it
    # to whatever the executor's own CLI/config defaults to invisibly.
    print()
    print("Model selection (press Enter to accept the default / use the tool's own default):")
    _MODEL_EXAMPLES: dict[str, str] = {
        "claude": "claude-haiku-4-5-20251001, claude-sonnet-5, claude-opus-5",
        "claude-subagent": "claude-haiku-4-5-20251001, claude-sonnet-5, claude-opus-5",
        "claude-process": "claude-haiku-4-5-20251001, claude-sonnet-5, claude-opus-5",
        "aider": "ollama_chat/qwen2.5-coder:14b (local), claude-sonnet-4-6 (cloud)",
        "codex": "gpt-5.6-terra, gpt-5.6-sol, o3, openai/o3-mini",
        "ollama": "qwen3-coder:30b-a3b-q4_K_M, qwen2.5-coder:14b",
    }
    # Wizard-only suggested defaults: review intentionally points at a
    # stronger/different model than analyze+implement so review isn't
    # just the implementer re-reading its own work with its own biases,
    # mirroring this project's own executors.claude-a/codex/aider-ollama-*
    # blocks. This is separate from resolve_model()'s runtime fallback
    # (_DEFAULT_*_MODEL, all haiku) used when models: is left unset
    # entirely -- that fallback stays a conservative/cheap default.
    # fix and drift_check are as exposed to an unconfigured-step gap as
    # analyze/implement/review: resolve_model() returns None for any
    # non-Claude executor with no models.<step> entry, and for aider that
    # means no --model flag at all -- which, with an Ollama provider, falls
    # through to aider's own default (an interactive OpenRouter login flow)
    # instead of the local model the rest of the config points at. A
    # headless dispatch just hangs on that prompt for several minutes and
    # then fails, indistinguishable from a genuine drift-check/fix failure
    # to whatever's watching (observed live on a real drift-check run,
    # which is what prompted adding these two prompts). fix follows implement's
    # suggestion (it's editing code the same way); drift_check follows
    # review's (autofix.py already treats it as "an independent review
    # route" that resolves from the review route's config).
    _WIZARD_STEP_DEFAULTS: dict[str, dict[str, str]] = {
        "claude": {
            "analyze": "claude-sonnet-5",
            "implement": "claude-sonnet-5",
            "review": "claude-opus-5",
            "fix": "claude-sonnet-5",
            "drift_check": "claude-opus-5",
        },
        "claude-subagent": {
            "analyze": "claude-sonnet-5",
            "implement": "claude-sonnet-5",
            "review": "claude-opus-5",
            "fix": "claude-sonnet-5",
            "drift_check": "claude-opus-5",
        },
        "claude-process": {
            "analyze": "claude-sonnet-5",
            "implement": "claude-sonnet-5",
            "review": "claude-opus-5",
            "fix": "claude-sonnet-5",
            "drift_check": "claude-opus-5",
        },
        "codex": {
            "analyze": "gpt-5.6-terra",
            "implement": "gpt-5.6-terra",
            "review": "gpt-5.6-sol",
            "fix": "gpt-5.6-terra",
            "drift_check": "gpt-5.6-sol",
        },
        "aider": {
            "analyze": "ollama_chat/qwen2.5-coder:14b",
            "implement": "ollama_chat/qwen2.5-coder:14b",
            "review": "ollama_chat/qwen2.5-coder:32b",
            "fix": "ollama_chat/qwen2.5-coder:14b",
            "drift_check": "ollama_chat/qwen2.5-coder:32b",
        },
    }

    # Best-effort discovery of what's actually pulled locally, so the
    # wizard's suggested default is a model that exists rather than a
    # hardcoded 14b/32b guess that 404s at runtime if it isn't installed
    # (TICK-645). One lookup, reused for all three model prompts below;
    # [] (Ollama not running / nothing pulled / unreachable) falls back
    # to today's hardcoded suggestion with no behavior change.
    _ollama_discovered: list[str] = []
    if "aider" in (executor, reviewer) or "ollama" in (executor, reviewer):
        from lanegate.executor import discover_ollama_models

        _ollama_discovered = discover_ollama_models("http://localhost:11434")

    def _ask_model(step: str, exec_type: str) -> str:
        examples = _MODEL_EXAMPLES.get(exec_type)
        hint = f"e.g. {examples}" if examples else f"check {exec_type}'s own docs for supported model names"
        print(f"  models.{step} ({exec_type}) — {hint}")
        default = _WIZARD_STEP_DEFAULTS.get(exec_type, {}).get(step, "")

        # aider routes local models through Aider's LiteLLM integration,
        # which needs an "ollama_chat/" prefix; the raw `ollama` executor
        # type talks to Ollama directly and takes the bare name. `ollama
        # list`/`GET /api/tags` always reports the bare name either way.
        picker: dict[str, str] = {}
        if exec_type in ("aider", "ollama") and _ollama_discovered:
            prefix = "ollama_chat/" if exec_type == "aider" else ""
            display_names = [f"{prefix}{name}" for name in _ollama_discovered]
            size_hint = "32b" if step in ("review", "drift_check") else "14b"
            suggested = next(
                (i for i, name in enumerate(_ollama_discovered) if size_hint in name), 0
            )
            print("  Installed Ollama models detected:")
            for i, display_name in enumerate(display_names):
                marker = " (suggested)" if i == suggested else ""
                print(f"    [{i + 1}] {display_name}{marker}")
            if exec_type == "aider":
                print(
                    "  Note: aider needs the 'ollama_chat/' prefix above (LiteLLM "
                    "routing) -- 'ollama list' itself reports these without it."
                )
            default = display_names[suggested]
            picker = {str(i + 1): name for i, name in enumerate(display_names)}

        while True:
            value = _prompt(f"  models.{step}", default)
            if not value:
                return value
            if picker and value.isdigit() and value not in picker:
                print(
                    f"  Invalid choice: '{value}' is not one of the listed "
                    f"options above ([1]-[{len(picker)}])."
                )
                continue
            value = picker.get(value, value)
            try:
                # No provider= here: the wizard doesn't know the
                # provider yet at this point (aider+Ollama is decided
                # further down, after all the model prompts run), so
                # this uses validate_model_for_executor's permissive
                # no-provider branch -- still catches a wrong-vendor
                # model string, just not an Ollama-specific mismatch.
                validate_model_for_executor(value, exec_type, f"models.{step}")
            except ConfigError as exc:
                print(f"  Invalid model: {exc}")
                continue
            return value

    models: dict[str, str] = {}
    for step, step_executor in (
        ("analyze", executor),
        ("implement", executor),
        ("fix", executor),
        ("review", reviewer),
        ("drift_check", reviewer),
    ):
        value = _ask_model(step, step_executor)
        if value:
            models[step] = value
    if models:
        cfg["models"] = models

    # --- Independent review across two local models ---
    # A same-tool setup (e.g. executor: aider, reviewer: aider) with two
    # different models is a real independent review -- but
    # resolve_independent_review_driver's ladder can only see a different
    # pool instance/driver, not a same-instance model swap, so without
    # this it falls through to the needs_review safety escalation on
    # every ticket (TICK-645).
    if (
        reviewer_explicit
        and reviewer == executor
        and models.get("review")
        and models.get("review") != models.get("implement")
    ):
        cfg.setdefault("review_fallback", "different_model")

    return models


def _init_aider_ollama_context(
    cfg: dict, executor: str, reviewer: str, models: dict, repo_root: Path
) -> None:
    """Suggest a context budget + edit_format when aider is routed to a local Ollama model."""
    # A local model has a finite context window; without a declared budget,
    # an oversized ticket overflows it unpredictably instead of failing
    # cleanly upfront (see docs/executor-capabilities.md#context-window-tokens).
    # Declaring provider: ollama here also arms lanegate's own runtime
    # warning if context_window_tokens is later left unset.
    aider_ollama_model = next(
        (
            models[step]
            for step, step_executor in (
                ("analyze", executor),
                ("implement", executor),
                ("fix", executor),
                ("review", reviewer),
                ("drift_check", reviewer),
            )
            if step_executor == "aider" and models.get(step, "").startswith("ollama")
        ),
        None,
    )
    if aider_ollama_model:
        print()
        print(
            f"Note: aider is routed to a local Ollama model ({aider_ollama_model}). "
            "LaneGate can enforce a preflight context budget so an oversized "
            "prompt fails cleanly instead of overflowing the model silently."
        )
        context_tokens_raw = _prompt(
            "  executors.aider.context_window_tokens (0 to skip)", "32768"
        )
        aider_cfg = cfg.setdefault("executors", {}).setdefault("aider", {})
        aider_cfg["provider"] = "ollama"
        try:
            context_tokens = int(context_tokens_raw)
        except ValueError:
            context_tokens = 0
        if context_tokens > 0:
            aider_cfg["context_window_tokens"] = context_tokens

        # Neither edit_format is universally safe for small local models:
        # "whole" rewrites the entire file every turn and can truncate or
        # hallucinate past a few hundred lines; "diff" avoids that but can
        # get a malformed hunk from a small model (see
        # docs/executor-capabilities.md, "Known caveats" for aider). Pick
        # the default from what's actually in this repo (TICK-645) rather
        # than a flat guess either way.
        recommended_format, format_note = _recommend_aider_edit_format(repo_root)
        if format_note:
            print(f"  {format_note}")
        else:
            print(
                "  No large tracked file detected here; 'whole' (full-file "
                "rewrites) is fine for small local models. Switch to 'diff' "
                "if a touched file grows past ~300-500 lines."
            )
        # Every other optional step in this wizard is an input(...
        # [y/N]) confirm, so a "y"/"n" typed here from muscle memory
        # must not land in config verbatim -- it would silently become
        # `aider --edit-format y` on every dispatch.
        # _VALID_AIDER_EDIT_FORMATS is a module-level constant (see top of file).
        while True:
            edit_format = _prompt("  executors.aider.edit_format", recommended_format)
            if not edit_format or edit_format in _VALID_AIDER_EDIT_FORMATS:
                break
            print(
                f"  Invalid edit_format {edit_format!r} — valid values are "
                f"{sorted(_VALID_AIDER_EDIT_FORMATS)}"
            )
        if edit_format:
            aider_cfg["edit_format"] = edit_format

        # repo_map/neutralize_touches/map_tokens keep the prompt lean by
        # deferring eager full-file preload to aider's own lazy
        # filename-mention scan instead of front-loading every touched
        # file — see the neutralize_touches/repo_map comments in
        # lanegate/executor.py's aider dispatch for the full rationale.
        aider_cfg["repo_map"] = True
        aider_cfg["neutralize_touches"] = True
        aider_cfg["map_tokens"] = 1024

        # A local model costs nothing per call, unlike a cloud API where
        # every retry is a real charge -- max_auto_fix_attempts' default of
        # 1 is a deliberate cost guardrail for that cloud case, not a
        # correctness requirement. It doesn't apply the same way once
        # everything routed through aider here is local, so default higher
        # for a local-Ollama setup instead of leaving cloud's conservative
        # default in place for a project that has no reason to want it.
        cfg.setdefault("max_auto_fix_attempts", 2)


def _init_feature_flags(cfg: dict) -> None:
    """Prompt to enable the feature-flag file."""
    print()
    want_flags = _prompt_yes_no("Enable feature flags?")
    if want_flags:
        flag_file = _prompt("flag_file", f"~/.{APP_NAME}/feature_flags.json")
        cfg["flag_file"] = flag_file


def _init_deployment_pipeline(cfg: dict, detected_trunk_branch: str) -> None:
    """Prompt to configure the optional deployment pipeline (environments)."""
    print()
    want_envs = _prompt_yes_no("Enable deployment pipeline (environments)?")
    if want_envs:
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


def _init_safeguards(cfg: dict, repo_root: Path) -> None:
    """Prompt to configure pre_complete/pre_merge safeguards, offering detected test runners."""
    print()
    detected_runners = detect_test_runner_safeguards(repo_root)
    if detected_runners:
        runner_names = ", ".join(d.name for d in detected_runners)
        commands = [d.command for d in detected_runners]
        command_list = ", ".join(commands)
        want_safeguards = _prompt_yes_no(
            f"Detected {runner_names} -- configure pre_complete: "
            f"[{command_list}], pre_merge: [{command_list}]?",
            default=True,
        )
        if want_safeguards:
            cfg["safeguards"] = {
                "pre_complete": commands,
                "pre_merge": commands,
            }
    else:
        want_safeguards = _prompt_yes_no(
            "Configure ticket safeguards (pre_complete / pre_merge guards)?"
        )
        if want_safeguards:
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


def _init_github_pr(cfg: dict) -> None:
    """Prompt to enable auto-push + GitHub PR creation on approved review."""
    print()
    cfg["github_pr"] = _prompt_yes_no(
        "Auto-push branches and open GitHub PRs on approved review?"
    )


def _finalize_init_config(cfg: dict, repo_root: Path) -> dict:
    """Run the steps common to both interactive and non-interactive init: re-init safety
    check, writing .lanegate.yml, updating .gitignore, creating directories, and
    registering the project."""
    config_path = repo_root / CONFIG_FILENAME

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
    # aider's own scratch/cache files (chat history, input history, tags
    # cache) are normally kept out of git by aider silently editing
    # .gitignore itself at startup -- an uncommitted side effect separate
    # from aider's own commit that LaneGate's scope-drift check then flags
    # as an unexpected committed file (see executor.py's
    # _warn_aider_missing_no_gitignore). --no-gitignore stops aider from
    # doing that, but then nothing ignores those scratch files at all, and
    # THEY trip the identical scope-drift check instead -- confirmed live in
    # a fresh-install smoke test. Writing the patterns into the project's
    # own .gitignore up front avoids both failure modes: aider's own
    # gitignore-editing has nothing left to add (a no-op, not a diff), and
    # --no-gitignore's scratch files are still covered.
    aider_in_use = (
        cfg.get("executor") == "aider"
        or cfg.get("reviewer") == "aider"
        or any(
            isinstance(v, dict) and v.get("type") == "aider"
            for v in (cfg.get("executors") or {}).values()
        )
    )
    extra_gitignore_entries = [".aider.*"] if aider_in_use else None
    _update_gitignore(
        repo_root,
        cfg.get("tickets_dir", f".{APP_NAME}/tickets"),
        extra_entries=extra_gitignore_entries,
    )

    # --- Create directories ---
    tickets_dir_path = repo_root / cfg.get("tickets_dir", f".{APP_NAME}/tickets")
    worktrees_dir_path = repo_root / cfg.get("worktrees_dir", f".{APP_NAME}/worktrees")
    tickets_dir_path.mkdir(parents=True, exist_ok=True)
    worktrees_dir_path.mkdir(parents=True, exist_ok=True)

    # --- Register in global registry ---
    registry_add(repo_root)

    return cfg


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
    global _stdin_exhausted_warned
    _stdin_exhausted_warned = False
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
        cfg, executor, reviewer, reviewer_explicit = _init_core_options(repo_root)
        _init_trunk_branch(cfg, repo_root)
        _init_headless_flags(cfg, executor, reviewer)
        _init_autonomy(cfg)
        models = _init_models(cfg, executor, reviewer, reviewer_explicit, repo_root)
        _init_aider_ollama_context(cfg, executor, reviewer, models, repo_root)
        _init_feature_flags(cfg)
        _init_deployment_pipeline(cfg, detected_trunk_branch)
        _init_safeguards(cfg, repo_root)
        _init_github_pr(cfg)

    return _finalize_init_config(cfg, repo_root)


# ---------------------------------------------------------------------------
# .gitignore helpers
# ---------------------------------------------------------------------------

def _gitignore_entries() -> list[str]:
    # CONFIG_FILENAME (.{APP_NAME}.yml) is deliberately NOT ignored: `git
    # worktree add` only checks out committed content, so an ignored
    # (never-committed) config leaves the very first ticket's worktree
    # without any config at all.
    return [f".{APP_NAME}/*", f"{APP_NAME}-context-log.jsonl"]


def _update_gitignore(
    repo_root: Path, tickets_dir: str | None = None, *, extra_entries: list[str] | None = None
) -> None:
    """Append .lanegate/ to .gitignore if not already present.

    Carves out tickets_dir (e.g. !.lanegate/tickets/ and !.lanegate/tickets/*) when
    tickets_dir sits under .lanegate/. Creates .gitignore if it doesn't exist. Also
    strips a stale CONFIG_FILENAME (.lanegate.yml) entry a pre-existing project's
    .gitignore may already carry from before it was deliberately excluded above --
    without this, a project initialized before that change stays gitignored on
    upgrade with no migration path, reproducing the same never-committed-config bug.
    """
    gitignore_path = repo_root / ".gitignore"
    if gitignore_path.exists():
        existing = gitignore_path.read_text(encoding="utf-8")
    else:
        existing = ""

    entries = list(_gitignore_entries()) + ["__pycache__/", "*.pyc", "*.pyo"] + list(extra_entries or [])
    if tickets_dir:
        norm = tickets_dir.strip("/")
        parts = Path(norm).parts
        if parts and parts[0] == f".{APP_NAME}":
            entries.extend([f"!{norm}/", f"!{norm}/*"])

    existing_lines = [line for line in existing.splitlines() if line.strip() != CONFIG_FILENAME]
    stripped_stale = len(existing_lines) != len(existing.splitlines())
    existing_line_set = {line.strip() for line in existing_lines}
    to_add = [entry for entry in entries if entry not in existing_line_set]

    if not to_add and not stripped_stale:
        return  # all entries already present, nothing stale to remove

    body = "\n".join(existing_lines)
    if body and not body.endswith("\n"):
        body += "\n"
    if to_add:
        body += "\n".join(to_add) + "\n"
    gitignore_path.write_text(body, encoding="utf-8")


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
