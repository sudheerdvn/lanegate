"""Tests for config_routing.py pool ordering and resolution helpers."""

import pytest

from lanegate.config import (
    CONFIG_FILENAME,
    ConfigError,
    _DEFAULT_ANALYZE_MODEL,
    _DEFAULT_IMPLEMENT_MODEL,
    _DEFAULT_REVIEW_MODEL,
    is_high_reasoning_ticket,
    load_config,
    resolve_acceptance_contract_mode,
    resolve_autonomy,
    resolve_executor,
    resolve_executor_route,
    resolve_max_parallel,
    resolve_max_parallel_detail,
    resolve_model,
    update_pool_executor_order,
)
from tests._helpers.config import write_config as _write_config


def test_max_parallel_default(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg["max_parallel"] == 2
    assert cfg["executors"] == {}


def test_resolve_max_parallel_precedence(tmp_path):
    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
executor: aider
max_parallel: 2
executors:
  claude: { max_parallel: 3 }
  aider:  { max_parallel: 1 }
""",
    )
    cfg = load_config(tmp_path)
    assert resolve_max_parallel(cfg) == 1  # active executor override (aider)
    assert resolve_max_parallel(cfg, override=5) == 5  # explicit override wins
    cfg["executor"] = "claude"
    assert resolve_max_parallel(cfg) == 3  # different executor override
    cfg["executor"] = "openhands"
    assert resolve_max_parallel(cfg) == 2  # no override → top-level


def test_resolve_max_parallel_detail_reports_source(tmp_path):
    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
executor: aider
max_parallel: 4
executors:
  aider: { max_parallel: 1 }
""",
    )
    cfg = load_config(tmp_path)

    assert resolve_max_parallel_detail(cfg, override=5) == {
        "value": 5,
        "source": "cli override",
        "override": 5,
    }

    executor_detail = resolve_max_parallel_detail(cfg)
    assert executor_detail["value"] == 1
    assert executor_detail["source"] == "default executor override"
    assert executor_detail["default_executor"] == "aider"
    assert executor_detail["config_key"] == "executors['aider'].max_parallel"
    assert executor_detail["overrides"] == {
        "source": "global config",
        "value": 4,
        "config_key": "max_parallel",
    }

    cfg["executor"] = "openhands"
    assert resolve_max_parallel_detail(cfg) == {
        "value": 4,
        "source": "global config",
        "config_key": "max_parallel",
    }

    assert resolve_max_parallel_detail({}) == {"value": 2, "source": "built-in default"}


def test_resolve_max_parallel_falls_back_to_default():
    assert resolve_max_parallel({}) == 2  # nothing configured


def test_resolve_max_parallel_with_pool(tmp_path):
    """TICK-618: a bare `executor:` value that doesn't match any named
    instance (only claude-a/claude-b are defined) must not silently fall
    through to the top-level/default value when a default_pool actually
    governs dispatch — it should pick up the summed per-instance cap across
    that pool's instances instead, since per-instance caps and least-loaded
    routing already prevent overloading any single instance."""
    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
executor: claude
max_parallel: 10
executors:
  claude-a: { type: claude-process, max_parallel: 2 }
  claude-b: { type: claude-process, max_parallel: 5 }
pools:
  default:
    executors: [claude-a, claude-b]
default_pool: default
""",
    )
    cfg = load_config(tmp_path)

    detail = resolve_max_parallel_detail(cfg)
    assert detail["value"] == 7  # sum(2, 5)
    assert detail["source"] == "pool instance cap (sum)"
    assert detail["pool"] == "default"
    assert detail["overrides"] == {
        "source": "global config",
        "value": 10,
        "config_key": "max_parallel",
    }
    assert resolve_max_parallel(cfg) == 7


def test_resolve_max_parallel_pool_sum_multi_instance(tmp_path):
    """TICK-618: a single low-capacity pool instance (e.g. a GPU-bound local
    model at max_parallel: 1) must not throttle the entire batch dispatcher
    down to 1 — the resolved cap should reflect the pool's total capacity
    across all instances."""
    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
executor: claude
executors:
  aider-ollama-27b: { type: aider, max_parallel: 1 }
  agy-claude: { type: agy, max_parallel: 3 }
  claude-b: { type: claude-process, max_parallel: 3 }
  claude-a: { type: claude-process, max_parallel: 3 }
pools:
  default:
    executors: [aider-ollama-27b, agy-claude, claude-b, claude-a]
default_pool: default
""",
    )
    cfg = load_config(tmp_path)

    detail = resolve_max_parallel_detail(cfg)
    assert detail["value"] == 10  # sum(1, 3, 3, 3)
    assert detail["source"] == "pool instance cap (sum)"
    assert resolve_max_parallel(cfg) == 10


def test_resolve_max_parallel_pool_all_uncapped(tmp_path):
    """TICK-618: when every pool instance omits max_parallel, capped == []
    and the resolver must fall through to the top-level/default value rather
    than sum([]) == 0 admitting zero work."""
    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
executor: claude
max_parallel: 4
executors:
  claude-a: { type: claude-process }
  claude-b: { type: claude-process }
pools:
  default:
    executors: [claude-a, claude-b]
default_pool: default
""",
    )
    cfg = load_config(tmp_path)

    detail = resolve_max_parallel_detail(cfg)
    assert detail["value"] == 4
    assert detail["source"] == "global config"
    assert resolve_max_parallel(cfg) == 4


def test_resolve_max_parallel_pool_partially_uncapped(tmp_path):
    """TICK-618 review finding: if even one pool instance omits max_parallel,
    that instance has unbounded capacity, so summing only the capped
    instances (e.g. sum([3]) == 3 while claude-b is uncapped) would wrongly
    throttle the whole pool to 3 instead of treating the pool as unbounded
    and falling through to the global max_parallel cap."""
    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
executor: claude
max_parallel: 10
executors:
  claude-a: { type: claude-process, max_parallel: 3 }
  claude-b: { type: claude-process }
pools:
  default:
    executors: [claude-a, claude-b]
default_pool: default
""",
    )
    cfg = load_config(tmp_path)

    detail = resolve_max_parallel_detail(cfg)
    assert detail["value"] == 10
    assert detail["source"] == "global config"
    assert resolve_max_parallel(cfg) == 10


def test_resolve_max_parallel_pool_present_executor_still_short_circuits(tmp_path):
    """TICK-618: a top-level `executor:` value that DOES match a named pool
    instance must short-circuit at the default-executor-override case and
    never reach the pool-sum branch."""
    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
executor: claude-process
executors:
  claude-process: { type: claude-process, max_parallel: 2 }
  claude-b: { type: claude-process, max_parallel: 5 }
pools:
  default:
    executors: [claude-process, claude-b]
default_pool: default
""",
    )
    cfg = load_config(tmp_path)

    detail = resolve_max_parallel_detail(cfg)
    assert detail["value"] == 2
    assert detail["source"] == "default executor override"
    assert detail["default_executor"] == "claude-process"
    assert resolve_max_parallel(cfg) == 2


def test_resolve_max_parallel_bare_executor_pool(tmp_path):
    """Without a default_pool, a bare executor value that matches no named
    instance keeps falling through to the top-level/default value — the
    pool-aware branch only kicks in when default_pool actually links the
    bare executor to pool membership."""
    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
executor: claude
max_parallel: 4
executors:
  claude-a: { type: claude-process, max_parallel: 2 }
  claude-b: { type: claude-process, max_parallel: 5 }
pools:
  default:
    executors: [claude-a, claude-b]
""",
    )
    cfg = load_config(tmp_path)

    detail = resolve_max_parallel_detail(cfg)
    assert detail["value"] == 4
    assert detail["source"] == "global config"


def test_update_pool_executor_order_persists_new_order(tmp_path):
    from lanegate.config import update_pool_executor_order

    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
ticket_prefix: TICK
executors:
  claude-1: { type: claude-process }
  claude-2: { type: claude-process }
pools:
  default:
    executors: [claude-1, claude-2]
    strategy: least-loaded
default_pool: default
""",
    )

    result = update_pool_executor_order(tmp_path, "default", ["claude-2", "claude-1"])

    assert result == {
        "name": "default",
        "strategy": "least-loaded",
        "executors": ["claude-2", "claude-1"],
    }

    cfg = load_config(tmp_path)
    assert cfg["pools"]["default"]["executors"] == ["claude-2", "claude-1"]
    # Unrelated top-level settings must survive the round-trip.
    assert cfg["ticket_prefix"] == "TICK"


def test_update_pool_executor_order_preserves_comments(tmp_path):
    from lanegate.config import update_pool_executor_order

    _write_config(
        tmp_path / CONFIG_FILENAME,
        """# top-of-file comment
ticket_prefix: TICK  # inline comment
executors:
  claude-1: { type: claude-process }
  claude-2: { type: claude-process }
# comment above pools
pools:
  default:
    executors: [claude-1, claude-2]
    strategy: least-loaded
default_pool: default
""",
    )

    update_pool_executor_order(tmp_path, "default", ["claude-2", "claude-1"])

    text = (tmp_path / CONFIG_FILENAME).read_text(encoding="utf-8")
    assert "# top-of-file comment" in text
    assert "# inline comment" in text
    assert "# comment above pools" in text
    assert "executors: [claude-2, claude-1]" in text

    cfg = load_config(tmp_path)
    assert cfg["pools"]["default"]["executors"] == ["claude-2", "claude-1"]


def test_update_pool_executor_order_preserves_comments_block_style(tmp_path):
    from lanegate.config import update_pool_executor_order

    _write_config(
        tmp_path / CONFIG_FILENAME,
        """executors:
  claude-1: { type: claude-process }
  claude-2: { type: claude-process }
pools:
  default:
    executors:
      - claude-1  # primary
      - claude-2
    strategy: least-loaded
default_pool: default
""",
    )

    update_pool_executor_order(tmp_path, "default", ["claude-2", "claude-1"])

    text = (tmp_path / CONFIG_FILENAME).read_text(encoding="utf-8")
    assert "- claude-2\n      - claude-1  # primary" in text

    cfg = load_config(tmp_path)
    assert cfg["pools"]["default"]["executors"] == ["claude-2", "claude-1"]


def test_update_pool_executor_order_unknown_pool_raises(tmp_path):
    from lanegate.config import ConfigError, update_pool_executor_order

    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
executors:
  claude-1: { type: claude-process }
pools:
  default:
    executors: [claude-1]
""",
    )

    with pytest.raises(ConfigError, match="not defined in pools"):
        update_pool_executor_order(tmp_path, "nonexistent", ["claude-1"])


def test_update_pool_executor_order_rejects_non_reordering(tmp_path):
    from lanegate.config import ConfigError, update_pool_executor_order

    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
executors:
  claude-1: { type: claude-process }
  claude-2: { type: claude-process }
pools:
  default:
    executors: [claude-1, claude-2]
""",
    )

    with pytest.raises(ConfigError, match="must be a reordering"):
        update_pool_executor_order(tmp_path, "default", ["claude-1"])

    with pytest.raises(ConfigError, match="must be a reordering"):
        update_pool_executor_order(tmp_path, "default", ["claude-1", "claude-3"])


def test_update_pool_executor_order_missing_config_raises(tmp_path):
    from lanegate.config import ConfigError, update_pool_executor_order

    with pytest.raises(ConfigError, match="no .*\\.yml found"):
        update_pool_executor_order(tmp_path, "default", ["claude-1"])


class TestResolveTicketPool:
    def _cfg(self, tmp_path, extra: str = "") -> dict:
        _write_config(
            tmp_path / CONFIG_FILENAME,
            f"""
executors:
  claude-1: {{ type: claude-process }}
  ollama-1: {{ type: ollama }}
pools:
  local:
    executors: [ollama-1]
  default:
    executors: [claude-1]
default_pool: default
routing:
  - when: {{complexity_max: 2, touches_max: 3}}
    executor_pool: local
  - when: {{complexity_min: 3}}
    executor_pool: default
{extra}
""",
        )
        return load_config(tmp_path)

    def test_low_complexity_low_touches_routes_to_local(self, tmp_path):
        from lanegate.config import resolve_ticket_pool

        cfg = self._cfg(tmp_path)
        ticket = {"complexity": 1, "touches": ["a.py"]}
        pool, reason = resolve_ticket_pool(cfg, ticket)
        assert pool == "local"
        assert "routing[0]" in reason

    def test_high_complexity_routes_to_default(self, tmp_path):
        from lanegate.config import resolve_ticket_pool

        cfg = self._cfg(tmp_path)
        ticket = {"complexity": 5, "touches": ["a.py"]}
        pool, reason = resolve_ticket_pool(cfg, ticket)
        assert pool == "default"
        assert "routing[1]" in reason

    def test_low_complexity_but_too_many_touches_falls_through_to_default_pool(self, tmp_path):
        from lanegate.config import resolve_ticket_pool

        cfg = self._cfg(tmp_path)
        # complexity is low enough for rule 0 but touches_max=3 excludes it;
        # complexity_min=3 in rule 1 also fails to match -> falls to default_pool.
        ticket = {"complexity": 1, "touches": ["a.py", "b.py", "c.py", "d.py"]}
        pool, reason = resolve_ticket_pool(cfg, ticket)
        assert pool == "default"
        assert "default_pool" in reason

    def test_unanalyzed_ticket_falls_through_to_default_pool(self, tmp_path):
        """A ticket with no `complexity` (not yet analyzed) matches no
        complexity-gated rule and falls back to default_pool without error."""
        from lanegate.config import resolve_ticket_pool

        cfg = self._cfg(tmp_path)
        ticket = {"touches": ["a.py"]}
        pool, reason = resolve_ticket_pool(cfg, ticket)
        assert pool == "default"
        assert "default_pool" in reason

    def test_no_match_and_no_default_pool_is_unrouted(self, tmp_path):
        from lanegate.config import resolve_ticket_pool

        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
executors:
  claude-1: { type: claude-process }
pools:
  local:
    executors: [claude-1]
routing:
  - when: {complexity_max: 2}
    executor_pool: local
""",
        )
        cfg = load_config(tmp_path)
        ticket = {"complexity": 9, "touches": []}
        pool, reason = resolve_ticket_pool(cfg, ticket)
        assert pool is None
        assert "unrouted" in reason

    def test_first_matching_rule_wins(self, tmp_path):
        from lanegate.config import resolve_ticket_pool

        cfg = self._cfg(tmp_path)
        # Matches rule 0 (complexity<=2) even though it would also satisfy a
        # hypothetical looser rule further down -- first match wins.
        ticket = {"complexity": 2, "touches": ["a.py"]}
        pool, reason = resolve_ticket_pool(cfg, ticket)
        assert pool == "local"
        assert "routing[0]" in reason

    def test_label_filter(self, tmp_path):
        from lanegate.config import resolve_ticket_pool

        cfg = self._cfg(tmp_path, extra="  - when: {label: hotfix}\n    executor_pool: default")
        ticket = {"labels": ["hotfix"], "touches": []}
        pool, reason = resolve_ticket_pool(cfg, ticket)
        assert pool == "default"


class TestResolveModel:
    """Tests for resolve_model() — precedence rules."""

    def test_ticket_model_wins_over_all(self):
        """Per-ticket model field wins over every config layer."""
        cfg = {
            "executor": "claude",
            "models": {"implement": "claude-sonnet-4-5"},
            "executors": {"claude": {"models": {"implement": "claude-opus-4-5"}}},
        }
        ticket = {"model": "claude-haiku-4-5"}
        assert resolve_model(cfg, "implement", ticket=ticket) == "claude-haiku-4-5"

    def test_ticket_review_model_pin_wins_over_all_for_review(self):
        """TICK-554: route --model pins the subsequent review model."""
        cfg = {
            "executor": "claude",
            "models": {"review": "claude-sonnet-4-5"},
            "executors": {"claude": {"models": {"review": "claude-opus-4-5"}}},
        }
        ticket = {"review_model_pin": "claude-haiku-4-5"}
        assert resolve_model(cfg, "review", ticket=ticket) == "claude-haiku-4-5"

    def test_review_attribution_does_not_override_review_model_resolution(self):
        """TICK-554: prior review metadata must not become a route pin."""
        cfg = {"executor": "codex", "models": {"review": "gpt-5.6-terra"}}
        ticket = {"review_model": "gpt-5.6-sol"}
        assert resolve_model(cfg, "review", ticket=ticket) == "gpt-5.6-terra"

    def test_per_executor_wins_over_top_level(self):
        """executors.<name>.models.<step> beats top-level models.<step>."""
        cfg = {
            "executor": "claude",
            "models": {"implement": "claude-sonnet-4-5"},
            "executors": {"claude": {"models": {"implement": "claude-opus-4-5"}}},
        }
        assert resolve_model(cfg, "implement") == "claude-opus-4-5"

    def test_top_level_models_wins_over_built_in_default(self):
        """Top-level models.<step> beats the built-in default."""
        cfg = {
            "executor": "claude",
            "models": {"implement": "claude-sonnet-4-5"},
            "executors": {},
        }
        assert resolve_model(cfg, "implement") == "claude-sonnet-4-5"

    def test_built_in_default_used_when_nothing_configured(self):
        """When no model is configured, the built-in default is returned."""
        cfg = {
            "executor": "claude",
            "models": {},
            "executors": {},
        }
        assert resolve_model(cfg, "analyze") == _DEFAULT_ANALYZE_MODEL
        assert resolve_model(cfg, "implement") == _DEFAULT_IMPLEMENT_MODEL
        assert resolve_model(cfg, "review") == _DEFAULT_REVIEW_MODEL

    def test_no_ticket_model_falls_through(self):
        """ticket=None or ticket without 'model' field does not short-circuit."""
        cfg = {
            "executor": "claude",
            "models": {"implement": "claude-sonnet-4-5"},
            "executors": {},
        }
        assert resolve_model(cfg, "implement", ticket=None) == "claude-sonnet-4-5"
        assert resolve_model(cfg, "implement", ticket={"id": "TICK-001"}) == "claude-sonnet-4-5"

    def test_per_executor_step_not_in_executor_falls_to_top_level(self):
        """Executor has models block but not for this step — falls to top-level."""
        cfg = {
            "executor": "claude",
            "models": {"implement": "claude-sonnet-4-5"},
            "executors": {"claude": {"models": {"analyze": "claude-haiku-4-5"}}},
        }
        assert resolve_model(cfg, "implement") == "claude-sonnet-4-5"

    def test_empty_config_returns_built_in_default(self):
        """Completely empty cfg returns the built-in default."""
        assert resolve_model({}, "analyze") == _DEFAULT_ANALYZE_MODEL

    def test_non_claude_executor_without_model_uses_executor_default(self):
        """Non-Claude executors do not receive a Claude model by default."""
        cfg = {
            "executor": "codex",
            "models": {},
            "executors": {"codex": {"max_parallel": 1}},
        }
        assert resolve_model(cfg, "analyze") is None
        assert resolve_model(cfg, "implement") is None
        assert resolve_model(cfg, "review") is None

    def test_non_claude_executor_explicit_model_is_respected(self):
        """Explicit per-executor models still pass through for non-Claude executors."""
        cfg = {
            "executor": "codex",
            "models": {},
            "executors": {"codex": {"models": {"analyze": "gpt-5-codex"}}},
        }
        assert resolve_model(cfg, "analyze") == "gpt-5-codex"

    def test_named_claude_instance_without_step_override_still_gets_built_in_default(self):
        """A named instance (TICK-088/TICK-089, e.g. 'claude-a') whose own name is
        never literally 'claude'/'claude-process'/'claude-subagent' must still be
        recognized as Claude-compatible via its `type:` field, and fall back to
        the built-in cheap default for a step it has no override for — not to
        None, which would leave the underlying CLI to use whatever model it
        happens to default to (e.g. a session-sticky, expensive one)."""
        cfg = {
            "executor": "claude-a",
            "executors": {
                "claude-a": {"type": "claude", "models": {"review": "claude-opus-4-8"}},
            },
        }
        assert resolve_model(cfg, "implement") == _DEFAULT_IMPLEMENT_MODEL
        assert resolve_model(cfg, "review") == "claude-opus-4-8"

    def test_named_instance_of_non_claude_type_still_gets_none(self):
        """A named instance of a non-Claude type (e.g. type: ollama) must not
        receive a Claude model default just because it has a custom name."""
        cfg = {
            "executor": "local-ollama",
            "executors": {"local-ollama": {"type": "ollama"}},
        }
        assert resolve_model(cfg, "implement") is None


class TestResolveExecutor:
    """Tests for resolve_executor() — precedence rules."""

    def test_global_executor_default(self):
        """Falls back to global executor when no per-step override is set."""
        cfg = {"executor": "aider", "executor_steps": {}}
        assert resolve_executor(cfg, "implement") == "aider"
        assert resolve_executor(cfg, "review") == "aider"

    def test_executor_steps_override_for_implement(self):
        """executor_steps.implement beats the global executor."""
        cfg = {"executor": "claude", "executor_steps": {"implement": "aider"}}
        assert resolve_executor(cfg, "implement") == "aider"

    def test_executor_steps_override_for_review(self):
        """executor_steps.review beats the global executor."""
        cfg = {"executor": "claude", "executor_steps": {"review": "openhands"}}
        assert resolve_executor(cfg, "review") == "openhands"

    def test_reviewer_config_overrides_review_executor_step(self):
        """cfg.reviewer is the explicit review selector, including human."""
        cfg = {"executor": "claude", "reviewer": "human", "executor_steps": {"review": "openhands"}}
        assert resolve_executor(cfg, "review") == "human"

    def test_ticket_reviewer_overrides_config_reviewer(self):
        """ticket.reviewer wins for the review step."""
        cfg = {"executor": "claude", "reviewer": "openhands", "executor_steps": {}}
        ticket = {"reviewer": "human"}
        assert resolve_executor(cfg, "review", ticket=ticket) == "human"

    def test_per_ticket_executor_wins_over_executor_steps_for_implement(self):
        """ticket.executor beats executor_steps.implement (implement step only)."""
        cfg = {"executor": "claude", "executor_steps": {"implement": "aider"}}
        ticket = {"executor": "codex"}
        assert resolve_executor(cfg, "implement", ticket=ticket) == "codex"

    def test_per_ticket_executor_ignored_for_review_step(self):
        """ticket.executor is NOT used for the review step."""
        cfg = {"executor": "claude", "executor_steps": {}}
        ticket = {"executor": "codex"}
        # review step ignores ticket.executor; falls through to global
        assert resolve_executor(cfg, "review", ticket=ticket) == "claude"

    def test_ticket_without_executor_field_falls_through(self):
        """ticket without executor field falls through to executor_steps then global."""
        cfg = {"executor": "claude", "executor_steps": {"implement": "aider"}}
        ticket = {"id": "TICK-001"}
        assert resolve_executor(cfg, "implement", ticket=ticket) == "aider"

    def test_empty_config_returns_claude(self):
        """Completely empty cfg returns 'claude' (built-in default)."""
        assert resolve_executor({}, "implement") == "claude"
        assert resolve_executor({}, "review") == "claude"

    def test_combined_mode_true_when_no_executor_steps(self):
        """When executor_steps is absent, both steps resolve to the same executor."""
        cfg = {"executor": "claude", "executor_steps": {}}
        assert resolve_executor(cfg, "implement") == resolve_executor(cfg, "review")

    def test_split_mode_when_implement_differs_from_review(self):
        """Different executors for implement and review produce split mode."""
        cfg = {"executor": "claude", "executor_steps": {"implement": "aider"}}
        assert resolve_executor(cfg, "implement") != resolve_executor(cfg, "review")

    def test_route_same_executor_is_combined(self):
        cfg = {"executor": "claude", "executor_steps": {"implement": "codex", "review": "codex"}}
        assert resolve_executor_route(cfg) == {
            "implement": "codex",
            "review": "codex",
            "mode": "combined",
        }

    def test_route_different_implement_review_is_split(self):
        cfg = {"executor": "claude", "executor_steps": {"implement": "codex", "review": "claude"}}
        assert resolve_executor_route(cfg) == {
            "implement": "codex",
            "review": "claude",
            "mode": "split",
        }

    def test_route_ticket_executor_overrides_implement_only(self):
        cfg = {"executor": "claude", "executor_steps": {"implement": "aider", "review": "claude"}}
        ticket = {"executor": "codex"}
        assert resolve_executor_route(cfg, ticket) == {
            "implement": "codex",
            "review": "claude",
            "mode": "split",
        }

    def test_route_reviewer_controls_review(self):
        cfg = {"executor": "claude", "reviewer": "human", "executor_steps": {"review": "codex"}}
        assert resolve_executor_route(cfg) == {
            "implement": "claude",
            "review": "human",
            "mode": "split",
        }


class TestResolveAutonomy:
    """Tests for resolve_autonomy() — precedence rules."""

    def test_defaults_to_supervised(self):
        from lanegate.config import resolve_autonomy

        assert resolve_autonomy({}) == "supervised"

    def test_project_level_full_applies(self):
        from lanegate.config import resolve_autonomy

        assert resolve_autonomy({"autonomy": "full"}) == "full"

    def test_ticket_level_overrides_project(self):
        from lanegate.config import resolve_autonomy

        cfg = {"autonomy": "supervised"}
        ticket = {"autonomy": "full"}
        assert resolve_autonomy(cfg, ticket) == "full"

    def test_ticket_without_autonomy_falls_back_to_project(self):
        from lanegate.config import resolve_autonomy

        cfg = {"autonomy": "full"}
        ticket = {"id": "TICK-001"}
        assert resolve_autonomy(cfg, ticket) == "full"


class TestResolveAcceptanceContractMode:
    """Tests for resolve_acceptance_contract_mode() — advisory-by-default gate."""

    def test_defaults_to_advisory(self):
        from lanegate.config import resolve_acceptance_contract_mode

        assert resolve_acceptance_contract_mode({}) == "advisory"

    def test_explicit_blocker_applies(self):
        from lanegate.config import resolve_acceptance_contract_mode

        assert resolve_acceptance_contract_mode({"acceptance_contract_mode": "blocker"}) == "blocker"

    def test_explicit_advisory_applies(self):
        from lanegate.config import resolve_acceptance_contract_mode

        assert resolve_acceptance_contract_mode({"acceptance_contract_mode": "advisory"}) == "advisory"

    def test_unrecognized_value_falls_back_to_advisory(self):
        from lanegate.config import resolve_acceptance_contract_mode

        assert resolve_acceptance_contract_mode({"acceptance_contract_mode": "bogus"}) == "advisory"

    def test_strict_profile_defaults_to_blocker(self):
        from lanegate.config import resolve_acceptance_contract_mode

        assert resolve_acceptance_contract_mode({"profile": "strict"}) == "blocker"

    def test_strict_profile_explicit_advisory_wins(self):
        from lanegate.config import resolve_acceptance_contract_mode

        cfg = {"profile": "strict", "acceptance_contract_mode": "advisory"}
        assert resolve_acceptance_contract_mode(cfg) == "advisory"

    def test_default_profile_still_defaults_to_advisory(self):
        from lanegate.config import resolve_acceptance_contract_mode

        assert resolve_acceptance_contract_mode({"profile": "default"}) == "advisory"

    def test_invalid_acceptance_contract_mode_raises(self, tmp_path):
        _write_config(tmp_path / CONFIG_FILENAME, "acceptance_contract_mode: blockr\n")
        with pytest.raises(ConfigError, match="acceptance_contract_mode"):
            load_config(tmp_path)


@pytest.mark.parametrize("signal", [
    "configuration", "security", "lifecycle", "orchestration", "prompt-trust",
])
def test_high_reasoning_control_plane_tickets_use_opus_default(signal):
    ticket = {"title": f"Harden {signal} behavior", "touches": []}
    for step in ("analyze", "implement", "review"):
        assert resolve_model({"executor": "claude"}, step, ticket) == "claude-opus-5"


@pytest.mark.parametrize("signal", [
    "configuration", "security", "lifecycle", "orchestration", "prompt-trust",
])
def test_high_reasoning_control_plane_maintenance_uses_opus_default(signal):
    """Routine repairs must not bypass the high-risk analysis contract."""
    ticket = {"title": f"Fix {signal} behavior", "touches": []}
    for step in ("analyze", "implement", "review"):
        assert resolve_model({"executor": "claude"}, step, ticket) == "claude-opus-5"


def test_high_reasoning_route_ignores_free_form_body_mentions():
    ticket = {
        "title": "Fix README typo",
        "_body": "Correct the wording in the configuration section.",
        "touches": ["README.md"],
    }
    assert not is_high_reasoning_ticket(ticket)
    assert resolve_model({"executor": "claude"}, "implement", ticket) == _DEFAULT_IMPLEMENT_MODEL


def test_high_reasoning_route_ignores_unqualified_category_words():
    ticket = {"title": "Exercise real lifecycle", "touches": ["src/app.ext"]}
    assert not is_high_reasoning_ticket(ticket)
    assert resolve_model({"executor": "claude"}, "analyze", ticket) == _DEFAULT_ANALYZE_MODEL


def test_high_reasoning_route_preserves_explicit_model_precedence():
    ticket = {"title": "Harden lifecycle behavior", "model": "claude-haiku-4-5-20251001"}
    assert resolve_model({"executor": "claude"}, "implement", ticket) == "claude-haiku-4-5-20251001"
    assert resolve_model(
        {"executor": "claude", "models": {"implement": "claude-sonnet-4-6"}},
        "implement", {"title": "Harden lifecycle behavior"},
    ) == "claude-sonnet-4-6"


