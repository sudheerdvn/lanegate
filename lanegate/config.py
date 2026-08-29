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

# Kiro CLI's headless chat invocation needs both non-interactive mode and
# upfront tool approval. build_executor_cmd supplies these flags itself so a
# per-executor flags override cannot accidentally weaken them.
_KIRO_MANAGED_FLAGS = {
    "--no-interactive",
    "--trust-all-tools",
    "--trust-tools",
    "--output-format",
    "--agent-engine",
}

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
    "cursor",
    "ollama",
    "gemini",  # deprecated 2026-06-18, superseded by "agy" (Antigravity CLI)
    "agy",
    "kiro",
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
        "reviewer_rotation": None,
        "verification": {
            "groups": [],
        },
        "lock_statuses": ["in_progress", "code_complete", "in_review"],
        "flag_file": f"~/.{APP_NAME}/feature_flags.json",
        "environments": [],
        "commit_status_changes": True,
        "cleanup_branch_on_merge": True,
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
        # Off by default: run-history logs are not deleted unless a project
        # opts in. run_history_retention_days only takes effect once this is true.
        "run_history_purge_enabled": False,
        "run_history_retention_days": 60,
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


def _recommend_aider_edit_format(
    repo_root: Path, touches: set[str] | None = None
) -> tuple[str, str | None]:
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
    tracked = touches if touches is not None else _repo_tracked_files(repo_root)
    for rel_path in tracked:
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





















_DEFAULT_SESSION_CHAINING = {
    "enabled": True,
    "chain_review": False,
    "max_session_age_s": 2700,
    "max_session_tokens": 150000,
}










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




























_DEFAULT_HUMAN_ESCALATION = {
    "credentials": True,
    "security_actions": True,
    "retry_limit": 3,
}

# Autonomy values that stay on the automatic amend/re-analyze -> fix ->
# re-review path without pausing for a human merge decision.
_AUTO_FIX_LANES = frozenset({"full", "green", "yellow"})












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














def _validate_kiro_flags(cfg: dict) -> None:
    """Reject Kiro overrides that conflict with LaneGate's required argv."""
    for section in ("executors", "drivers"):
        entries = cfg.get(section) or {}
        if not isinstance(entries, dict):
            continue
        for name, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            executor_type = entry.get("type", name if section == "executors" else None)
            if executor_type != "kiro" or "flags" not in entry:
                continue
            flags = entry["flags"]
            if not isinstance(flags, list) or not all(isinstance(flag, str) for flag in flags):
                raise ConfigError(f"{section}['{name}'].flags for kiro must be a list of strings")
            conflicting = [
                flag for flag in flags
                if flag in _KIRO_MANAGED_FLAGS
                or any(flag.startswith(f"{managed}=") for managed in _KIRO_MANAGED_FLAGS)
            ]
            if conflicting:
                raise ConfigError(
                    f"{section}['{name}'].flags may not override Kiro's required "
                    f"headless flags: {conflicting}"
                )


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
    _validate_kiro_flags(cfg)

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
    
    # Validate reviewer_rotation: a list of driver/executor names the
    # orchestrator cycles through for the review step.
    rot = cfg.get("reviewer_rotation")
    if rot is not None:
        if not isinstance(rot, list) or not all(isinstance(x, str) for x in rot):
            raise ConfigError("'reviewer_rotation' must be a list of strings")
        known = (
            _VALID_REVIEWERS
            | set(cfg.get("drivers") or {})
            | set(cfg.get("executors") or {})
        )
        unknown = [name for name in rot if name not in known]
        if unknown:
            raise ConfigError(
                f"'reviewer_rotation' references undefined driver(s) {sorted(set(unknown))} — "
                f"each entry must be a key in drivers:/executors: or one of "
                f"{sorted(_VALID_REVIEWERS)}"
            )
        if len(set(rot)) < 2:
            raise ConfigError(
                "'reviewer_rotation' needs at least two distinct entries to rotate between"
            )
        # A pinned steps.review.driver fully determines the reviewer, so the
        # rotation list can never take effect.
        if cfg.get("steps", {}).get("review", {}).get("driver"):
            warnings.warn(
                "reviewer_rotation is ignored because steps.review.driver is pinned",
                stacklevel=2,
            )

    _warn_if_combined_mode_collapse(cfg)
    return cfg


def repo_root_from_config(cfg_path: Path) -> Path:
    return cfg_path.parent




def protected_branches(cfg: dict) -> set[str]:
    """Set of branch names that must never be removed or pruned."""
    return {env["branch"] for env in cfg.get("environments", [])}


from lanegate.config_trust import (  # noqa: E402,F401
    _trusted_git_executable,
    _windows_git_candidates,
    _is_protected_executable,
    _control_checkout_root,
    is_linked_worktree,
    find_config,
    find_repo_root,
)
from lanegate.config_routing import (  # noqa: E402,F401
    _splice_reordered_flow_list,
    _splice_reordered_block_list,
    update_pool_executor_order,
    _describe_routing_when,
    _ticket_matches_routing_when,
    resolve_ticket_pool,
    resolve_max_parallel_detail,
    resolve_max_parallel,
    is_high_reasoning_ticket,
    should_escalate_review,
    resolve_model,
    resolve_executor,
    resolve_executor_route,
    resolve_autonomy,
    resolve_human_escalation,
    is_auto_fix_lane,
    is_red_lane,
    resolve_acceptance_contract_mode,
)
from lanegate.config_validation import (  # noqa: E402,F401
    _validate_environments,
    _validate_reference_docs,
    _validate_verification,
    _is_positive_int,
    _validate_executor,
    _validate_reviewer,
    _validate_review_fallback,
    _validate_profile,
    _validate_autonomy,
    resolve_session_chaining,
    _validate_session_chaining,
    _validate_auto_fix,
    _validate_executor_steps,
    validate_model_for_executor,
    _validate_models,
    _parse_drivers,
    _parse_steps,
    _validate_executor_instances,
    _validate_aider_model_settings,
    _validate_concurrency,
    _validate_orphan_timeout,
    _validate_safeguards,
    _validate_executor_timeouts,
    _validate_tree_sitter_languages,
    _validate_acceptance_contract_mode,
    _validate_budget_caps,
    _validate_default_human_review,
    _validate_doc_update,
    _validate_display_timezone,
    resolve_display_tzinfo,
    format_display_ts,
    _validate_rate_limit,
    _validate_pools,
    _validate_routing,
    _validate_project_guidance,
    _warn_if_combined_mode_collapse,
)

# Interactive project initialization lives in config_init.py. Keep these
# package-level aliases for the CLI and existing callers.
from lanegate.config_init import (  # noqa: E402,F401
    TestRunnerDetection,
    _detect_existing_tickets_dir,
    _gitignore_entries,
    _update_gitignore,
    detect_test_runner_safeguards,
    interactive_init,
    suggested_safeguards_yaml,
)
from lanegate.config_registry import (  # noqa: E402,F401
    _registry_load,
    _registry_save,
    registry_add,
    registry_load,
    registry_path,
    registry_remove,
)
