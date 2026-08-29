"""Validation and normalization helpers for .lanegate.yml."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

from lanegate.config import (
    ConfigError,
    _DEFAULT_RESUME_CEILING_S,
    _KNOWN_AGY_MODELS,
    _DEFAULT_SESSION_CHAINING,
    _VALID_ACCEPTANCE_CONTRACT_MODES,
    _VALID_AIDER_EDIT_FORMATS,
    _VALID_AIDER_MODEL_SETTINGS_KEYS,
    _VALID_DRIVER_FIELDS,
    _VALID_EXECUTOR_STEPS,
    _VALID_EXECUTOR_TYPES,
    _VALID_HUMAN_REVIEW_MODES,
    _VALID_MODEL_STEPS,
    _VALID_PIPELINE_STEPS,
    _VALID_POOL_STRATEGIES,
    _VALID_PROFILES,
    _VALID_RATE_LIMIT_MODES,
    _VALID_REVIEWERS,
    _VALID_REVIEW_FALLBACKS,
    _ROUTING_INT_WHEN_KEYS,
    _ROUTING_WHEN_KEYS,
    _VALID_SYNC,
    _VALID_TRIGGERS,
    resolve_executor,
)
from lanegate.config_routing import _is_positive_int


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




def _validate_executor(cfg: dict) -> None:
    """Validate the top-level executor value."""
    executor = cfg.get("executor")
    configured_names = set((cfg.get("executors") or {}).keys()) | set((cfg.get("pools") or {}).keys())
    if executor not in _VALID_EXECUTOR_TYPES and executor not in configured_names:
        raise ValueError(f"invalid executor '{executor}' — must be one of {_VALID_EXECUTOR_TYPES}")


def _validate_reviewer(cfg: dict) -> None:
    """Validate the optional top-level reviewer value."""
    reviewer = cfg.get("reviewer")
    configured_names = set((cfg.get("executors") or {}).keys()) | set((cfg.get("pools") or {}).keys())
    if reviewer is not None and reviewer not in _VALID_REVIEWERS and reviewer not in configured_names:
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
            # review_escalation is a review retry, so it shares the review
            # driver's model namespace.  Modern steps: routing takes
            # precedence over the legacy executor/reviewer resolver.
            route_step = "review" if step == "review_escalation" else step
            step_cfg = (cfg.get("steps") or {}).get(route_step)
            driver_name = step_cfg.get("driver") if isinstance(step_cfg, dict) else None
            driver_cfg = (cfg.get("drivers") or {}).get(driver_name)
            ex_type: str
            ex_provider: str | None
            if isinstance(driver_cfg, dict):
                ex_type = str(driver_cfg["type"])
                ex_provider = driver_cfg.get("provider")
            else:
                ex_name = driver_name if isinstance(driver_name, str) else resolve_executor(cfg, route_step)
                ex_cfg = executors.get(ex_name) if isinstance(executors, dict) else None
                ex_type = str(ex_cfg.get("type", ex_name)) if isinstance(ex_cfg, dict) else ex_name
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
        neutralize = aider_cfg.get("neutralize_touches") is True
        
        if neutralize and aider_cfg.get("edit_format") == "whole":
            raise ConfigError(
                f"executors['{executor_name}'] cannot combine neutralize_touches: true "
                "with edit_format: 'whole'"
            )

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
                if neutralize and ef == "whole":
                    raise ConfigError(
                        f"executors['{executor_name}'].model_settings[{model_key!r}] "
                        "cannot combine neutralize_touches: true with edit_format: 'whole'"
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


