"""Tests for config.py — load, walk-up discovery, environment validation."""

import json
from pathlib import Path
from unittest import mock

import pytest

from lanegate.config import (
    ConfigError,
    _DEFAULT_ANALYZE_MODEL,
    _DEFAULT_IMPLEMENT_MODEL,
    _DEFAULT_RESUME_CEILING_S,
    _DEFAULT_REVIEW_MODEL,
    _gitignore_entries,
    CONFIG_FILENAME,
    _default_config,
    _detect_existing_tickets_dir,
    _update_gitignore,
    detect_test_runner_safeguards,
    find_config,
    interactive_init,
    load_config,
    protected_branches,
    registry_add,
    resolve_executor,
    resolve_executor_route,
    resolve_max_parallel,
    resolve_max_parallel_detail,
    resolve_model,
    resolve_session_chaining,
    suggested_safeguards_yaml,
)


def _write_config(path: Path, content: str) -> None:
    path.write_text(content)


def test_load_defaults_when_no_config(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg["ticket_prefix"] == "TICK"
    assert cfg["tickets_dir"] == ".lanegate/tickets"
    assert cfg["worktrees_dir"] == ".lanegate/worktrees"
    assert cfg["commit_status_changes"] is True
    assert cfg["github_pr"] is False
    assert cfg["lock_statuses"] == ["in_progress", "code_complete", "in_review"]
    assert cfg["project_guidance"]["include_defaults"] is True
    assert cfg["project_guidance"]["files"] == []
    assert cfg["project_guidance"]["max_bytes"] == 20000
    assert cfg["environments"] == []
    assert cfg["orphan_timeout_hours"] == 4
    assert cfg["executor_idle_timeout_seconds"] == 75
    assert cfg["executor_stall_timeout_seconds"] == 900
    assert cfg["executor_absolute_ceiling_seconds"] == 1500


def test_trunk_branch_explicit_config_overrides_detection(tmp_path):
    _write_config(tmp_path / CONFIG_FILENAME, "trunk_branch: develop\n")

    assert load_config(tmp_path)["trunk_branch"] == "develop"


def test_trunk_branch_detects_origin_head_before_main_fallback(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-b", "master"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    assert load_config(tmp_path)["trunk_branch"] == "develop"


def test_executor_stream_timeout_overrides_and_ordering(tmp_path):
    _write_config(
        tmp_path / CONFIG_FILENAME,
        "executor_idle_timeout_seconds: 11\nexecutor_stall_timeout_seconds: 18\nexecutor_absolute_ceiling_seconds: 22\n",
    )
    cfg = load_config(tmp_path)
    assert cfg["executor_idle_timeout_seconds"] == 11
    assert cfg["executor_stall_timeout_seconds"] == 18
    assert cfg["executor_absolute_ceiling_seconds"] == 22

    _write_config(
        tmp_path / CONFIG_FILENAME,
        "executor_idle_timeout_seconds: 22\nexecutor_stall_timeout_seconds: 22\nexecutor_absolute_ceiling_seconds: 30\n",
    )
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_executor_stall_timeout_adapts_for_legacy_timeout_overrides(tmp_path):
    _write_config(
        tmp_path / CONFIG_FILENAME,
        "executor_idle_timeout_seconds: 10\nexecutor_absolute_ceiling_seconds: 40\n",
    )

    cfg = load_config(tmp_path)

    assert cfg["executor_stall_timeout_seconds"] == 25


def test_project_guidance_config_accepted(tmp_path):
    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
project_guidance:
  include_defaults: false
  files:
    - docs/coding.md
    - .cursor/rules/*.mdc
  max_bytes: 4096
""",
    )

    cfg = load_config(tmp_path)

    assert cfg["project_guidance"]["include_defaults"] is False
    assert cfg["project_guidance"]["files"] == ["docs/coding.md", ".cursor/rules/*.mdc"]
    assert cfg["project_guidance"]["max_bytes"] == 4096


def test_project_guidance_can_be_disabled(tmp_path):
    _write_config(tmp_path / CONFIG_FILENAME, "project_guidance: false\n")

    cfg = load_config(tmp_path)

    assert cfg["project_guidance"] is False


def test_project_guidance_invalid_files_raises(tmp_path):
    from lanegate.config import ConfigError

    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
project_guidance:
  files: docs/coding.md
""",
    )

    with pytest.raises(ConfigError, match="project_guidance.files"):
        load_config(tmp_path)


def test_project_guidance_invalid_max_bytes_raises(tmp_path):
    from lanegate.config import ConfigError

    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
project_guidance:
  max_bytes: 0
""",
    )

    with pytest.raises(ConfigError, match="project_guidance.max_bytes"):
        load_config(tmp_path)


def test_orphan_timeout_must_be_positive(tmp_path):
    _write_config(tmp_path / CONFIG_FILENAME, "orphan_timeout_hours: 0\n")
    with pytest.raises(ValueError, match="orphan_timeout_hours"):
        load_config(tmp_path)


def test_rate_limit_defaults(tmp_path):
    """resume is the default (TICK-344) and the give-up ceiling is finite.

    `ceiling_s: null` means poll forever; leaving that as the default meant a
    hibernation misclassified as waitable re-invoked orchestrate every 2h
    indefinitely. That is only acceptable as an explicit opt-in.
    """
    cfg = load_config(tmp_path)
    assert cfg["on_rate_limit"] == "resume"
    assert cfg["rate_limit_resume"] == {
        "initial_backoff_s": 300,
        "max_backoff_s": 7200,
        "ceiling_s": 86400,
    }


def test_on_rate_limit_halt_still_accepted(tmp_path):
    _write_config(tmp_path / CONFIG_FILENAME, "on_rate_limit: halt\n")
    assert load_config(tmp_path)["on_rate_limit"] == "halt"


def test_partial_rate_limit_resume_block_does_not_restore_poll_forever(tmp_path):
    """load_config merges .lanegate.yml shallowly, so setting one key under
    rate_limit_resume replaces the whole default block. The daemon must still
    end up with a finite ceiling rather than silently inheriting None."""
    _write_config(
        tmp_path / CONFIG_FILENAME, "rate_limit_resume:\n  initial_backoff_s: 60\n"
    )
    cfg = load_config(tmp_path)
    assert cfg["rate_limit_resume"].get("ceiling_s") is None  # documents the shallow merge
    resume_cfg = cfg.get("rate_limit_resume") or {}
    assert resume_cfg.get("ceiling_s", _DEFAULT_RESUME_CEILING_S) == 86400


def test_on_rate_limit_resume_accepted(tmp_path):
    _write_config(tmp_path / CONFIG_FILENAME, "on_rate_limit: resume\n")
    cfg = load_config(tmp_path)
    assert cfg["on_rate_limit"] == "resume"


def test_on_rate_limit_invalid_value_raises(tmp_path):
    _write_config(tmp_path / CONFIG_FILENAME, "on_rate_limit: sometimes\n")
    with pytest.raises(ValueError, match="on_rate_limit"):
        load_config(tmp_path)


def test_default_human_review_defaults_to_none(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg["default_human_review"] == "none"


def test_default_human_review_per_ticket_accepted(tmp_path):
    _write_config(tmp_path / CONFIG_FILENAME, "default_human_review: per_ticket\n")
    cfg = load_config(tmp_path)
    assert cfg["default_human_review"] == "per_ticket"


def test_default_human_review_invalid_value_raises(tmp_path):
    _write_config(tmp_path / CONFIG_FILENAME, "default_human_review: sometimes\n")
    with pytest.raises(ValueError, match="default_human_review"):
        load_config(tmp_path)


def test_rate_limit_resume_custom_backoff_accepted(tmp_path):
    _write_config(
        tmp_path / CONFIG_FILENAME,
        "on_rate_limit: resume\n"
        "rate_limit_resume:\n"
        "  initial_backoff_s: 60\n"
        "  max_backoff_s: 3600\n"
        "  ceiling_s: 21600\n",
    )
    cfg = load_config(tmp_path)
    assert cfg["rate_limit_resume"] == {
        "initial_backoff_s": 60,
        "max_backoff_s": 3600,
        "ceiling_s": 21600,
    }


def test_rate_limit_resume_max_less_than_initial_raises(tmp_path):
    _write_config(
        tmp_path / CONFIG_FILENAME,
        "rate_limit_resume:\n  initial_backoff_s: 1000\n  max_backoff_s: 100\n",
    )
    with pytest.raises(ValueError, match="max_backoff_s"):
        load_config(tmp_path)


def test_rate_limit_resume_negative_ceiling_raises(tmp_path):
    _write_config(tmp_path / CONFIG_FILENAME, "rate_limit_resume:\n  ceiling_s: -5\n")
    with pytest.raises(ValueError, match="ceiling_s"):
        load_config(tmp_path)


def test_load_overrides_from_file(tmp_path):
    _write_config(tmp_path / CONFIG_FILENAME, "ticket_prefix: FEAT\ntickets_dir: issues\n")
    cfg = load_config(tmp_path)
    assert cfg["ticket_prefix"] == "FEAT"
    assert cfg["tickets_dir"] == "issues"
    assert cfg["worktrees_dir"] == ".lanegate/worktrees"  # default preserved


def test_find_config_walk_up(tmp_path):
    config_path = tmp_path / CONFIG_FILENAME
    config_path.write_text("ticket_prefix: TICK\n")
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    found = find_config(nested)
    assert found == config_path


def test_find_config_returns_none_when_absent(tmp_path):
    assert find_config(tmp_path) is None


def test_environment_normalization(tmp_path):
    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
environments:
  - name: staging
    branch: staging
    from: main
    trigger: manual
""",
    )
    cfg = load_config(tmp_path)
    env = cfg["environments"][0]
    assert env["name"] == "staging"
    assert env["sync"] == "ff-only"  # default
    assert env["pre_promote"] == []  # default
    assert env["post_promote"] == []  # default


def test_duplicate_environment_name_raises(tmp_path):
    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
environments:
  - name: staging
    trigger: manual
  - name: staging
    trigger: manual
""",
    )
    with pytest.raises(ValueError, match="duplicate environment name"):
        load_config(tmp_path)


def test_invalid_trigger_raises(tmp_path):
    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
environments:
  - name: staging
    trigger: bogus
""",
    )
    with pytest.raises(ValueError, match="invalid trigger"):
        load_config(tmp_path)


def test_invalid_sync_raises(tmp_path):
    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
environments:
  - name: staging
    sync: bogus-strategy
""",
    )
    with pytest.raises(ValueError, match="invalid sync"):
        load_config(tmp_path)


def test_protected_branches_from_environments(tmp_path):
    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
environments:
  - name: staging
    branch: staging
    trigger: manual
  - name: production
    branch: deploy
    trigger: manual
""",
    )
    cfg = load_config(tmp_path)
    pb = protected_branches(cfg)
    assert "staging" in pb
    assert "deploy" in pb


def test_auto_trigger_environment_valid(tmp_path):
    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
environments:
  - name: stage
    branch: stage
    trigger: auto
""",
    )
    cfg = load_config(tmp_path)
    assert cfg["environments"][0]["trigger"] == "auto"


def test_zero_environments_valid(tmp_path):
    _write_config(tmp_path / CONFIG_FILENAME, "ticket_prefix: TICK\nenvironments: []\n")
    cfg = load_config(tmp_path)
    assert cfg["environments"] == []


# --- concurrency / resource gate (max_parallel) ---


def test_max_parallel_default(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg["max_parallel"] == 2
    assert cfg["executors"] == {}


def test_invalid_max_parallel_raises(tmp_path):
    _write_config(tmp_path / CONFIG_FILENAME, "max_parallel: 0\n")
    with pytest.raises(ValueError, match="max_parallel must be a positive integer"):
        load_config(tmp_path)


def test_invalid_executor_override_raises(tmp_path):
    _write_config(tmp_path / CONFIG_FILENAME, "executors:\n  local: { max_parallel: -1 }\n")
    with pytest.raises(ValueError, match="executors\\['local'\\].max_parallel"):
        load_config(tmp_path)


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
    """TICK-286: a bare `executor:` value that doesn't match any named
    instance (only claude-a/claude-b are defined) must not silently fall
    through to the top-level/default value when a default_pool actually
    governs dispatch — it should pick up the minimum per-instance cap across
    that pool's instances instead."""
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
    assert detail["value"] == 2  # min(2, 5)
    assert detail["source"] == "pool instance cap (min)"
    assert detail["pool"] == "default"
    assert detail["overrides"] == {
        "source": "global config",
        "value": 10,
        "config_key": "max_parallel",
    }
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


# --- executor validation ---


def test_valid_executor_claude_subagent(tmp_path):
    _write_config(tmp_path / CONFIG_FILENAME, "executor: claude-subagent\n")
    cfg = load_config(tmp_path)
    assert cfg["executor"] == "claude-subagent"


def test_valid_executor_claude_process(tmp_path):
    _write_config(tmp_path / CONFIG_FILENAME, "executor: claude-process\n")
    cfg = load_config(tmp_path)
    assert cfg["executor"] == "claude-process"


def test_valid_executor_ollama(tmp_path):
    _write_config(tmp_path / CONFIG_FILENAME, "executor: ollama\n")
    cfg = load_config(tmp_path)
    assert cfg["executor"] == "ollama"


def test_invalid_executor_raises(tmp_path):
    _write_config(tmp_path / CONFIG_FILENAME, "executor: bogus\n")
    with pytest.raises(ValueError, match="invalid executor"):
        load_config(tmp_path)


# --- named executor instances (TICK-088) ---


def test_parse_named_executors(tmp_path):
    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
executors:
  claude-1:
    type: claude-process
    api_key_env: ANTHROPIC_API_KEY_1
    max_parallel: 2
  claude-2:
    type: claude-process
    api_key_env: ANTHROPIC_API_KEY_2
  local-ollama:
    type: ollama
    max_parallel: 4
""",
    )
    cfg = load_config(tmp_path)

    assert cfg["executors"]["claude-1"]["type"] == "claude-process"
    assert cfg["executors"]["claude-1"]["api_key_env"] == "ANTHROPIC_API_KEY_1"
    assert cfg["executors"]["claude-1"]["max_parallel"] == 2

    assert cfg["executors"]["claude-2"]["type"] == "claude-process"
    assert cfg["executors"]["claude-2"]["api_key_env"] == "ANTHROPIC_API_KEY_2"

    assert cfg["executors"]["local-ollama"]["type"] == "ollama"
    assert cfg["executors"]["local-ollama"]["max_parallel"] == 4


def test_named_executor_unknown_type_raises(tmp_path):
    _write_config(
        tmp_path / CONFIG_FILENAME,
        "executors:\n  claude-1: { type: not-a-real-driver }\n",
    )
    with pytest.raises(ValueError, match="executors\\['claude-1'\\].type"):
        load_config(tmp_path)


def test_named_executor_api_key_env_must_be_string(tmp_path):
    _write_config(
        tmp_path / CONFIG_FILENAME,
        "executors:\n  claude-1: { type: claude-process, api_key_env: 123 }\n",
    )
    with pytest.raises(ValueError, match="api_key_env must be a string"):
        load_config(tmp_path)


# --- executor pools (TICK-089) ---


def test_parse_pools_block(tmp_path):
    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
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
    cfg = load_config(tmp_path)

    assert cfg["pools"]["default"]["executors"] == ["claude-1", "claude-2"]
    assert cfg["pools"]["default"]["strategy"] == "least-loaded"
    assert cfg["default_pool"] == "default"


def test_pool_strategy_defaults_to_least_loaded_when_omitted(tmp_path):
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
    cfg = load_config(tmp_path)
    assert cfg["pools"]["default"].get("strategy", "least-loaded") == "least-loaded"


def test_pool_referencing_unknown_executor_raises(tmp_path):
    from lanegate.config import ConfigError

    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
executors:
  claude-1: { type: claude-process }
pools:
  default:
    executors: [claude-1, claude-nonexistent]
""",
    )
    with pytest.raises(ConfigError, match="unknown executor 'claude-nonexistent'"):
        load_config(tmp_path)


def test_pool_invalid_strategy_raises(tmp_path):
    from lanegate.config import ConfigError

    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
executors:
  claude-1: { type: claude-process }
pools:
  default:
    executors: [claude-1]
    strategy: round-and-round
""",
    )
    with pytest.raises(ConfigError, match="strategy must be one of"):
        load_config(tmp_path)


def test_pool_empty_executors_list_raises(tmp_path):
    from lanegate.config import ConfigError

    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
executors:
  claude-1: { type: claude-process }
pools:
  default:
    executors: []
""",
    )
    with pytest.raises(ConfigError, match="non-empty list"):
        load_config(tmp_path)


def test_default_pool_must_reference_a_defined_pool(tmp_path):
    from lanegate.config import ConfigError

    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
executors:
  claude-1: { type: claude-process }
pools:
  default:
    executors: [claude-1]
default_pool: not-a-real-pool
""",
    )
    with pytest.raises(ConfigError, match="default_pool 'not-a-real-pool'"):
        load_config(tmp_path)


def test_default_pool_without_any_pools_block_raises(tmp_path):
    from lanegate.config import ConfigError

    _write_config(
        tmp_path / CONFIG_FILENAME,
        "default_pool: default\n",
    )
    with pytest.raises(ConfigError, match="no pools: block"):
        load_config(tmp_path)


def test_single_executor_config_without_pools_is_unaffected(tmp_path):
    """No pools: block at all — plain single-executor configs keep working."""
    cfg = load_config(tmp_path)
    assert cfg.get("pools") is None
    assert cfg.get("default_pool") is None


# --- update_pool_executor_order (TICK-269: TUI pool reorder persistence) ---


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


# --- complexity-based routing (TICK-091) ---


def test_routing_block_parses_with_valid_pool_names(tmp_path):
    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
executors:
  claude-1: { type: claude-process }
  ollama-1: { type: ollama }
pools:
  local:
    executors: [ollama-1]
  default:
    executors: [claude-1]
default_pool: default
routing:
  - when: {complexity_max: 2, touches_max: 3}
    executor_pool: local
  - when: {complexity_min: 3}
    executor_pool: default
""",
    )
    cfg = load_config(tmp_path)
    assert cfg["routing"][0]["executor_pool"] == "local"
    assert cfg["routing"][0]["when"] == {"complexity_max": 2, "touches_max": 3}
    assert cfg["routing"][1]["executor_pool"] == "default"


def test_routing_rejects_unknown_pool_name(tmp_path):
    from lanegate.config import ConfigError

    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
executors:
  claude-1: { type: claude-process }
pools:
  default:
    executors: [claude-1]
routing:
  - when: {complexity_max: 2}
    executor_pool: nonexistent
""",
    )
    with pytest.raises(ConfigError, match="not defined in pools"):
        load_config(tmp_path)


def test_routing_rejects_missing_executor_pool(tmp_path):
    from lanegate.config import ConfigError

    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
executors:
  claude-1: { type: claude-process }
pools:
  default:
    executors: [claude-1]
routing:
  - when: {complexity_max: 2}
""",
    )
    with pytest.raises(ConfigError, match="executor_pool is required"):
        load_config(tmp_path)


def test_routing_rejects_unknown_when_field(tmp_path):
    from lanegate.config import ConfigError

    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
executors:
  claude-1: { type: claude-process }
pools:
  default:
    executors: [claude-1]
routing:
  - when: {complexity_max: 2, bogus_field: 1}
    executor_pool: default
""",
    )
    with pytest.raises(ConfigError, match="unknown field"):
        load_config(tmp_path)


def test_routing_rejects_non_integer_threshold(tmp_path):
    from lanegate.config import ConfigError

    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
executors:
  claude-1: { type: claude-process }
pools:
  default:
    executors: [claude-1]
routing:
  - when: {complexity_max: "low"}
    executor_pool: default
""",
    )
    with pytest.raises(ConfigError, match="must be an integer"):
        load_config(tmp_path)


def test_routing_rejects_non_list_block(tmp_path):
    from lanegate.config import ConfigError

    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
executors:
  claude-1: { type: claude-process }
pools:
  default:
    executors: [claude-1]
routing: {when: {complexity_max: 2}, executor_pool: default}
""",
    )
    with pytest.raises(ConfigError, match="routing must be a list"):
        load_config(tmp_path)


def test_routing_empty_block_is_a_noop(tmp_path):
    """routing: [] (the default) validates fine even with no pools: block at all."""
    cfg = load_config(tmp_path)
    assert cfg["routing"] == []


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


def test_backward_compat_bare_executor(tmp_path):
    """A bare `executor: claude-process` (no executors: block at all) must keep working."""
    _write_config(tmp_path / CONFIG_FILENAME, "executor: claude-process\n")
    cfg = load_config(tmp_path)
    assert cfg["executor"] == "claude-process"
    assert cfg["executors"] == {}


def test_backward_compat_legacy_per_type_executor_override(tmp_path):
    """Pre-TICK-088 executors: entries (key = type, no 'type' field) must keep working."""
    _write_config(
        tmp_path / CONFIG_FILENAME,
        "executor: aider\nexecutors:\n  aider: { max_parallel: 3 }\n",
    )
    cfg = load_config(tmp_path)
    assert cfg["executors"]["aider"]["max_parallel"] == 3
    assert "type" not in cfg["executors"]["aider"]


# ---------------------------------------------------------------------------
# Test runner safeguard detection
# ---------------------------------------------------------------------------


def _detected_commands(path: Path) -> list[str]:
    return [d.command for d in detect_test_runner_safeguards(path)]


def test_detects_pytest_from_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")

    assert _detected_commands(tmp_path) == ["pytest"]


def test_detects_pytest_from_tests_dir(tmp_path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_app.py").write_text("def test_app():\n    assert True\n")

    assert _detected_commands(tmp_path) == ["pytest"]


def test_detects_npm_test_script(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "vitest"}}))

    assert _detected_commands(tmp_path) == ["npm test"]


def test_detects_cargo_test(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname = \"demo\"\n")

    assert _detected_commands(tmp_path) == ["cargo test"]


def test_detects_go_test(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/demo\n")

    assert _detected_commands(tmp_path) == ["go test"]


def test_detects_no_runner(tmp_path):
    assert detect_test_runner_safeguards(tmp_path) == []


def test_suggested_safeguards_yaml_names_commands(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "npm test"}}))
    detections = detect_test_runner_safeguards(tmp_path)

    assert suggested_safeguards_yaml(detections) == (
        "safeguards:\n"
        "  pre_complete:\n"
        "    - npm test\n"
        "  pre_merge:\n"
        "    - npm test"
    )


# ---------------------------------------------------------------------------
# interactive_init — --defaults / non-TTY path
# ---------------------------------------------------------------------------


class TestInteractiveInit:
    """Tests for interactive_init() — only the non-interactive paths."""

    def test_defaults_writes_minimal_config(self, tmp_path):
        """--defaults path writes .lanegate.yml with expected minimal fields."""
        with mock.patch("lanegate.config._registry_save"):
            cfg = interactive_init(tmp_path, use_defaults=True)

        assert cfg is not None
        config_path = tmp_path / CONFIG_FILENAME
        assert config_path.exists()

        import yaml

        written = yaml.safe_load(config_path.read_text())
        assert written["ticket_prefix"] == "TICK"
        assert written["tickets_dir"] == ".lanegate/tickets"
        assert written["worktrees_dir"] == ".lanegate/worktrees"
        assert written["executor"] == "claude"
        assert written["max_parallel"] == 2
        # Minimal config should NOT include environments or flag_file
        assert "environments" not in written
        assert "flag_file" not in written

    def test_defaults_writes_scoped_claude_permission_flags(self, tmp_path):
        """TICK-364: init writes a scoped --allowedTools default, not the
        bypass flag, so a fresh project doesn't start every user off with
        every permission check disabled."""
        with mock.patch("lanegate.config._registry_save"):
            interactive_init(tmp_path, use_defaults=True)

        import yaml

        written = yaml.safe_load((tmp_path / CONFIG_FILENAME).read_text())
        flags = written["executors"]["claude"]["flags"]
        assert "--dangerously-skip-permissions" not in flags
        assert "--allowedTools" in flags

    def test_defaults_creates_directories(self, tmp_path):
        """Tickets and worktrees directories are created under .lanegate/."""
        with mock.patch("lanegate.config._registry_save"):
            interactive_init(tmp_path, use_defaults=True)

        assert (tmp_path / ".lanegate" / "tickets").is_dir()
        assert (tmp_path / ".lanegate" / "worktrees").is_dir()

    def test_already_exists_returns_none(self, tmp_path):
        """Returns None when .lanegate.yml already exists."""
        (tmp_path / CONFIG_FILENAME).write_text("ticket_prefix: TICK\n")
        result = interactive_init(tmp_path, use_defaults=True)
        assert result is None

    def test_non_tty_stdin_uses_defaults(self, tmp_path):
        """When stdin is not a TTY, defaults are used without prompting."""
        with mock.patch("sys.stdin") as mock_stdin, mock.patch("lanegate.config._registry_save"):
            mock_stdin.isatty.return_value = False
            cfg = interactive_init(tmp_path)

        assert cfg is not None
        assert cfg["ticket_prefix"] == "TICK"
        assert cfg["executor"] == "claude"

    def test_registry_called_on_defaults(self, tmp_path):
        """registry_add is called after a successful --defaults init."""
        with mock.patch("lanegate.config._registry_save") as mock_save:
            interactive_init(tmp_path, use_defaults=True)

        # _registry_save should have been called at least once (from registry_add)
        assert mock_save.called

    def test_defaults_returns_config_dict(self, tmp_path):
        """Return value is the config dict, not None."""
        with mock.patch("lanegate.config._registry_save"):
            result = interactive_init(tmp_path, use_defaults=True)
        assert isinstance(result, dict)

    def test_force_interactive_prompts_even_in_non_tty(self, tmp_path):
        """force_interactive=True fires prompts even when stdin.isatty() is False."""
        inputs = iter(["", "", "", "", "", "", "", "", ""])  # accept all defaults
        with (
            mock.patch("sys.stdin") as mock_stdin,
            mock.patch("builtins.input", side_effect=lambda _="": next(inputs, "")),
            mock.patch("lanegate.config._registry_save"),
        ):
            mock_stdin.isatty.return_value = False
            cfg = interactive_init(tmp_path, force_interactive=True)
        assert cfg is not None
        assert cfg["ticket_prefix"] == "TICK"

    def test_non_tty_without_force_prints_hint(self, tmp_path, capsys):
        """Non-TTY without force_interactive prints the --interactive hint to stderr."""
        with mock.patch("sys.stdin") as mock_stdin, mock.patch("lanegate.config._registry_save"):
            mock_stdin.isatty.return_value = False
            interactive_init(tmp_path)
        err = capsys.readouterr().err
        assert "--interactive" in err

    def test_init_adds_gitignore_entries(self, tmp_path):
        """lanegate init appends .lanegate/ and .lanegate.yml to .gitignore."""
        with mock.patch("lanegate.config._registry_save"):
            interactive_init(tmp_path, use_defaults=True)

        gitignore = tmp_path / ".gitignore"
        assert gitignore.exists()
        content = gitignore.read_text()
        assert ".lanegate/" in content
        assert "!.lanegate/tickets/" in content
        assert ".lanegate.yml" in content

    def test_init_does_not_duplicate_gitignore_entries(self, tmp_path):
        """Running init twice does not produce duplicate .gitignore entries."""
        with mock.patch("lanegate.config._registry_save"):
            interactive_init(tmp_path, use_defaults=True)

        # Remove the config so we could reinit — but first verify dedup logic via
        # calling _update_gitignore directly a second time.
        from lanegate.config import _update_gitignore

        _update_gitignore(tmp_path, ".lanegate/tickets")

        content = (tmp_path / ".gitignore").read_text()
        assert content.splitlines().count(".lanegate/*") == 1
        assert content.count(".lanegate.yml") == 1

    def test_init_appends_to_existing_gitignore(self, tmp_path):
        """When .gitignore already exists, entries are appended not overwritten."""
        (tmp_path / ".gitignore").write_text("__pycache__/\n*.pyc\n")
        with mock.patch("lanegate.config._registry_save"):
            interactive_init(tmp_path, use_defaults=True)

        content = (tmp_path / ".gitignore").read_text()
        assert "__pycache__/" in content
        assert "*.pyc" in content
        assert ".lanegate/" in content
        assert ".lanegate.yml" in content

    def test_explicit_tickets_dir_in_config_is_preserved(self, tmp_path):
        """An existing explicit tickets_dir in .lanegate.yml is never overridden by init."""
        # Simulate a project that already has .lanegate.yml with tickets_dir: tickets/
        # (init is blocked if config exists, so this tests load_config behaviour)
        import yaml

        (tmp_path / CONFIG_FILENAME).write_text(
            yaml.dump(
                {
                    "ticket_prefix": "TICK",
                    "tickets_dir": "tickets",
                    "worktrees_dir": "worktrees",
                    "executor": "claude",
                    "max_parallel": 2,
                }
            )
        )
        cfg = load_config(tmp_path)
        assert cfg["tickets_dir"] == "tickets"
        assert cfg["worktrees_dir"] == "worktrees"

    def test_reinit_with_existing_tickets_warns_and_preserves(self, tmp_path, capsys):
        """Re-init on a project with tickets/ prints a warning and keeps tickets_dir."""
        # Create an existing tickets/ directory with a .md file
        existing = tmp_path / "tickets"
        existing.mkdir()
        (existing / "TICK-001.md").write_text("---\nid: TICK-001\n---\n")

        with mock.patch("lanegate.config._registry_save"):
            cfg = interactive_init(tmp_path, use_defaults=True)

        assert cfg is not None
        # tickets_dir must be preserved as the existing location
        assert cfg["tickets_dir"] == "tickets"
        err = capsys.readouterr().err
        assert "WARNING" in err
        assert "tickets" in err

    def test_reinit_with_empty_tickets_dir_silently_uses_new_default(self, tmp_path):
        """Empty existing tickets/ directory allows silent update to new default."""
        # Create an empty tickets/ directory (no .md files)
        (tmp_path / "tickets").mkdir()

        with mock.patch("lanegate.config._registry_save"):
            cfg = interactive_init(tmp_path, use_defaults=True)

        assert cfg is not None
        # Empty dir — silent path update permitted
        assert cfg["tickets_dir"] == ".lanegate/tickets"

    def test_reinit_never_deletes_existing_ticket_files(self, tmp_path):
        """init must never delete existing ticket .md files."""
        existing = tmp_path / "tickets"
        existing.mkdir()
        ticket_file = existing / "TICK-001.md"
        ticket_file.write_text("---\nid: TICK-001\n---\nImportant ticket.\n")

        with mock.patch("lanegate.config._registry_save"):
            interactive_init(tmp_path, use_defaults=True)

        # The file must still be there
        assert ticket_file.exists()
        assert "Important ticket." in ticket_file.read_text()


# ---------------------------------------------------------------------------
# _default_config() values
# ---------------------------------------------------------------------------


class TestDefaultConfig:
    """Direct tests for _default_config() — zero-footprint defaults."""

    def test_tickets_dir_is_dotlanegate(self):
        cfg = _default_config()
        assert cfg["tickets_dir"] == ".lanegate/tickets"

    def test_worktrees_dir_is_dotlanegate(self):
        cfg = _default_config()
        assert cfg["worktrees_dir"] == ".lanegate/worktrees"

    def test_commit_status_changes_is_true(self):
        cfg = _default_config()
        assert cfg["commit_status_changes"] is True

    def test_github_pr_is_false(self):
        cfg = _default_config()
        assert cfg["github_pr"] is False

    def test_default_config_derives_from_app_name(self, monkeypatch):
        monkeypatch.setattr("lanegate.config.APP_NAME", "testbrand")
        cfg = _default_config()
        assert cfg["tickets_dir"] == ".testbrand/tickets"
        assert cfg["worktrees_dir"] == ".testbrand/worktrees"
        assert _gitignore_entries() == [".testbrand/*", ".testbrand.yml", "testbrand-context-log.jsonl"]


# ---------------------------------------------------------------------------
# _update_gitignore helper
# ---------------------------------------------------------------------------


class TestUpdateGitignore:
    """Tests for _update_gitignore() helper."""

    def test_creates_gitignore_when_absent(self, tmp_path):
        _update_gitignore(tmp_path)
        gitignore = tmp_path / ".gitignore"
        assert gitignore.exists()
        content = gitignore.read_text()
        for entry in _gitignore_entries():
            assert entry in content

    def test_appends_missing_entries(self, tmp_path):
        (tmp_path / ".gitignore").write_text("node_modules/\n")
        _update_gitignore(tmp_path)
        content = (tmp_path / ".gitignore").read_text()
        assert "node_modules/" in content
        for entry in _gitignore_entries():
            assert entry in content

    def test_no_duplicate_when_entries_already_present(self, tmp_path):
        existing = "\n".join(_gitignore_entries()) + "\n"
        (tmp_path / ".gitignore").write_text(existing)
        _update_gitignore(tmp_path)
        content = (tmp_path / ".gitignore").read_text()
        for entry in _gitignore_entries():
            assert content.count(entry) == 1

    def test_update_gitignore_carves_out_tickets_dir_under_lanegate(self, tmp_path):
        _update_gitignore(tmp_path, tickets_dir=".lanegate/tickets")
        content = (tmp_path / ".gitignore").read_text()
        assert "!.lanegate/tickets/" in content
        assert "!.lanegate/tickets/*" in content

    def test_update_gitignore_skips_carveout_for_external_tickets_dir(self, tmp_path):
        _update_gitignore(tmp_path, tickets_dir="tickets")
        content = (tmp_path / ".gitignore").read_text()
        assert "!.lanegate/tickets/" not in content
        assert "!tickets/" not in content

    def test_update_gitignore_unignores_tickets_with_git_check_ignore(self, tmp_path):
        import shutil
        import subprocess as _sp

        if shutil.which("git") is None:
            pytest.skip("git is required for git check-ignore test")

        _sp.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
        _update_gitignore(tmp_path, tickets_dir=".lanegate/tickets")

        ticket_file = tmp_path / ".lanegate/tickets/TICK-001.md"
        ticket_file.parent.mkdir(parents=True, exist_ok=True)
        ticket_file.write_text("test")
        res = _sp.run(["git", "check-ignore", str(ticket_file)], cwd=tmp_path, capture_output=True)
        assert res.returncode == 1, "ticket file under .lanegate/tickets/ should NOT be ignored"

        log_file = tmp_path / ".lanegate/logs/test.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("log")
        res_log = _sp.run(["git", "check-ignore", str(log_file)], cwd=tmp_path, capture_output=True)
        assert res_log.returncode == 0, "log file under .lanegate/logs/ SHOULD be ignored"


# ---------------------------------------------------------------------------
# _detect_existing_tickets_dir helper
# ---------------------------------------------------------------------------


class TestDetectExistingTicketsDir:
    """Tests for _detect_existing_tickets_dir() helper."""

    def test_no_existing_dir_returns_none(self, tmp_path):
        result_dir, has_tickets = _detect_existing_tickets_dir(tmp_path, ".lanegate/tickets")
        assert result_dir is None
        assert has_tickets is False

    def test_detects_legacy_tickets_dir_with_md_files(self, tmp_path):
        tickets = tmp_path / "tickets"
        tickets.mkdir()
        (tickets / "TICK-001.md").write_text("---\nid: TICK-001\n---\n")
        result_dir, has_tickets = _detect_existing_tickets_dir(tmp_path, ".lanegate/tickets")
        assert result_dir == "tickets"
        assert has_tickets is True

    def test_empty_legacy_dir_reports_no_tickets(self, tmp_path):
        (tmp_path / "tickets").mkdir()
        result_dir, has_tickets = _detect_existing_tickets_dir(tmp_path, ".lanegate/tickets")
        assert result_dir == "tickets"
        assert has_tickets is False

    def test_same_as_proposed_is_not_a_conflict(self, tmp_path):
        # If proposed == existing, no conflict
        (tmp_path / "tickets").mkdir()
        (tmp_path / "tickets" / "TICK-001.md").write_text("---\nid: TICK-001\n---\n")
        result_dir, has_tickets = _detect_existing_tickets_dir(tmp_path, "tickets")
        assert result_dir is None


# ---------------------------------------------------------------------------
# registry_add
# ---------------------------------------------------------------------------


class TestRegistryAdd:
    """Tests for registry_add() — global project registration."""

    def test_registry_add_creates_entry(self, tmp_path):
        """registry_add writes an entry for the project."""
        fake_registry = tmp_path / "projects.json"
        with (
            mock.patch("lanegate.config._REGISTRY_FILE", fake_registry),
            mock.patch("lanegate.config._REGISTRY_DIR", tmp_path),
        ):
            registry_add(tmp_path)

        data = json.loads(fake_registry.read_text())
        paths = [e["path"] for e in data]
        assert str(tmp_path.resolve()) in paths

    def test_registry_add_is_idempotent(self, tmp_path):
        """Calling registry_add twice does not create duplicate entries."""
        fake_registry = tmp_path / "projects.json"
        with (
            mock.patch("lanegate.config._REGISTRY_FILE", fake_registry),
            mock.patch("lanegate.config._REGISTRY_DIR", tmp_path),
        ):
            registry_add(tmp_path)
            registry_add(tmp_path)

        data = json.loads(fake_registry.read_text())
        matching = [e for e in data if e["path"] == str(tmp_path.resolve())]
        assert len(matching) == 1


# ---------------------------------------------------------------------------
# reviewer validation
# ---------------------------------------------------------------------------


def test_valid_reviewer_accepted(tmp_path):
    """A valid reviewer value (in _VALID_EXECUTOR_TYPES) is accepted without error."""
    _write_config(tmp_path / CONFIG_FILENAME, "reviewer: aider\n")
    cfg = load_config(tmp_path)
    assert cfg["reviewer"] == "aider"


def test_valid_reviewer_claude_subagent(tmp_path):
    """reviewer: claude-subagent is a valid executor value."""
    _write_config(tmp_path / CONFIG_FILENAME, "reviewer: claude-subagent\n")
    cfg = load_config(tmp_path)
    assert cfg["reviewer"] == "claude-subagent"


def test_valid_reviewer_human(tmp_path):
    """reviewer: human is a valid human review gate."""
    _write_config(tmp_path / CONFIG_FILENAME, "reviewer: human\n")
    cfg = load_config(tmp_path)
    assert cfg["reviewer"] == "human"


def test_invalid_reviewer_raises(tmp_path):
    """An unrecognised reviewer value raises ValueError."""
    _write_config(tmp_path / CONFIG_FILENAME, "reviewer: bogus-reviewer\n")
    with pytest.raises(ValueError, match="invalid reviewer"):
        load_config(tmp_path)


def test_reviewer_absent_does_not_raise(tmp_path):
    """Omitting reviewer entirely is valid — resolution happens at dispatch time."""
    _write_config(tmp_path / CONFIG_FILENAME, "ticket_prefix: TICK\n")
    cfg = load_config(tmp_path)
    assert cfg.get("reviewer") is None


def test_reviewer_and_executor_coexist(tmp_path):
    """Both executor and reviewer can be set independently."""
    _write_config(tmp_path / CONFIG_FILENAME, "executor: claude-process\nreviewer: aider\n")
    cfg = load_config(tmp_path)
    assert cfg["executor"] == "claude-process"
    assert cfg["reviewer"] == "aider"


def test_config_warns_on_combined_mode_collapse(tmp_path):
    """reviewer explicitly set to the same driver as executor warns at load time."""
    _write_config(tmp_path / CONFIG_FILENAME, "executor: claude\nreviewer: claude\n")
    with pytest.warns(UserWarning, match="combined"):
        load_config(tmp_path)


def test_no_warning_when_reviewer_differs_from_executor(tmp_path, recwarn):
    """reviewer explicitly set to a different driver than executor does not warn."""
    _write_config(tmp_path / CONFIG_FILENAME, "executor: claude\nreviewer: aider\n")
    load_config(tmp_path)
    assert len(recwarn) == 0


def test_no_warning_when_distinct_step_drivers_override_identical_legacy_fallbacks(tmp_path, recwarn):
    """Current step routes, not the legacy fallback pair, determine review mode."""
    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
executor: codex
reviewer: codex
drivers:
  codex-implement: {type: codex}
  codex-review: {type: codex}
steps:
  implement: {driver: codex-implement}
  review: {driver: codex-review}
""",
    )

    load_config(tmp_path)

    assert len(recwarn) == 0


def test_no_warning_when_reviewer_absent(tmp_path, recwarn):
    """Omitting reviewer entirely does not warn — resolution falls through to executor."""
    _write_config(tmp_path / CONFIG_FILENAME, "executor: claude\n")
    load_config(tmp_path)
    assert len(recwarn) == 0


# ---------------------------------------------------------------------------
# models: block validation
# ---------------------------------------------------------------------------


def test_models_block_accepted(tmp_path):
    """A valid models: block with known keys is accepted."""
    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
models:
  analyze: claude-haiku-4-5-20251001
  implement: claude-sonnet-4-5
  review: claude-opus-4-5
""",
    )
    cfg = load_config(tmp_path)
    assert cfg["models"]["analyze"] == "claude-haiku-4-5-20251001"
    assert cfg["models"]["implement"] == "claude-sonnet-4-5"
    assert cfg["models"]["review"] == "claude-opus-4-5"


def test_models_block_unknown_key_raises(tmp_path):
    """An unknown key under models: raises ConfigError."""
    from lanegate.config import ConfigError

    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
models:
  analyze: claude-haiku-4-5-20251001
  deploy: some-model
""",
    )
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(tmp_path)


def test_models_block_empty_is_valid(tmp_path):
    """An empty models: block is valid."""
    _write_config(tmp_path / CONFIG_FILENAME, "models: {}\n")
    cfg = load_config(tmp_path)
    assert cfg["models"] == {}


def test_per_executor_models_block_accepted(tmp_path):
    """Per-executor models block with valid keys is accepted."""
    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
executors:
  claude:
    models:
      implement: claude-sonnet-4-5
""",
    )
    cfg = load_config(tmp_path)
    assert cfg["executors"]["claude"]["models"]["implement"] == "claude-sonnet-4-5"


def test_per_executor_models_unknown_key_raises(tmp_path):
    """Unknown key under executors.<name>.models raises ConfigError."""
    from lanegate.config import ConfigError

    _write_config(
        tmp_path / CONFIG_FILENAME,
        """
executors:
  claude:
    models:
      bogus_step: some-model
""",
    )
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(tmp_path)


# ---------------------------------------------------------------------------
# resolve_model — resolution order
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# resolve_executor — resolution order
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# executor_steps config validation
# ---------------------------------------------------------------------------


class TestExecutorStepsValidation:
    """Tests for executor_steps: block validation in load_config."""

    def test_executor_steps_accepted(self, tmp_path):
        """A valid executor_steps block is loaded correctly."""
        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
executor_steps:
  implement: aider
  review: claude
""",
        )
        cfg = load_config(tmp_path)
        assert cfg["executor_steps"]["implement"] == "aider"
        assert cfg["executor_steps"]["review"] == "claude"

    def test_executor_steps_empty_is_valid(self, tmp_path):
        """An empty executor_steps block is valid."""
        _write_config(tmp_path / CONFIG_FILENAME, "executor_steps: {}\n")
        cfg = load_config(tmp_path)
        assert cfg["executor_steps"] == {}

    def test_executor_steps_absent_defaults_to_empty(self, tmp_path):
        """executor_steps absent from config defaults to {}."""
        _write_config(tmp_path / CONFIG_FILENAME, "ticket_prefix: TICK\n")
        cfg = load_config(tmp_path)
        assert cfg["executor_steps"] == {}

    def test_executor_steps_unknown_step_raises(self, tmp_path):
        """Unknown step key under executor_steps raises ConfigError."""
        from lanegate.config import ConfigError

        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
executor_steps:
  deploy: claude
""",
        )
        with pytest.raises(ConfigError, match="unknown key"):
            load_config(tmp_path)

    def test_executor_steps_invalid_executor_raises(self, tmp_path):
        """Invalid executor name under executor_steps raises ConfigError."""
        from lanegate.config import ConfigError

        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
executor_steps:
  implement: bogus-executor
""",
        )
        with pytest.raises(ConfigError, match="invalid executor"):
            load_config(tmp_path)

    def test_executor_steps_review_accepts_human(self, tmp_path):
        """Review step accepts human as a gate value."""
        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
executor_steps:
  review: human
""",
        )
        cfg = load_config(tmp_path)
        assert cfg["executor_steps"]["review"] == "human"


class TestVerificationValidation:
    """Tests for verification.groups block validation in load_config."""

    def test_absent_defaults_to_empty_groups(self, tmp_path):
        _write_config(tmp_path / CONFIG_FILENAME, "ticket_prefix: TICK\n")
        cfg = load_config(tmp_path)
        assert cfg["verification"]["groups"] == []

    def test_valid_groups_accepted(self, tmp_path):
        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
verification:
  groups:
    - patterns: ["apps/web/**"]
      dev_server: "npm run dev:web"
      url: "http://localhost:3000"
    - patterns: ["apps/admin/**"]
      dev_server: "npm run dev:admin"
      url: "http://localhost:4000"
""",
        )
        cfg = load_config(tmp_path)
        groups = cfg["verification"]["groups"]
        assert len(groups) == 2
        assert groups[0]["patterns"] == ["apps/web/**"]
        assert groups[1]["url"] == "http://localhost:4000"

    def test_group_without_patterns_raises(self, tmp_path):
        from lanegate.config import ConfigError

        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
verification:
  groups:
    - dev_server: "npm run dev"
""",
        )
        with pytest.raises(ConfigError, match="patterns"):
            load_config(tmp_path)

    def test_groups_not_a_list_raises(self, tmp_path):
        from lanegate.config import ConfigError

        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
verification:
  groups: "apps/web/**"
""",
        )
        with pytest.raises(ConfigError, match="groups"):
            load_config(tmp_path)


# ---------------------------------------------------------------------------
# autonomy / max_auto_fix_attempts validation (TICK-120)
# ---------------------------------------------------------------------------


class TestAutonomyValidation:
    def test_autonomy_absent_does_not_raise(self, tmp_path):
        _write_config(tmp_path / CONFIG_FILENAME, "ticket_prefix: TICK\n")
        cfg = load_config(tmp_path)
        assert cfg.get("autonomy") is None

    def test_valid_autonomy_full_accepted(self, tmp_path):
        _write_config(tmp_path / CONFIG_FILENAME, "autonomy: full\n")
        cfg = load_config(tmp_path)
        assert cfg["autonomy"] == "full"

    def test_valid_autonomy_supervised_accepted(self, tmp_path):
        _write_config(tmp_path / CONFIG_FILENAME, "autonomy: supervised\n")
        cfg = load_config(tmp_path)
        assert cfg["autonomy"] == "supervised"

    def test_invalid_autonomy_raises(self, tmp_path):
        from lanegate.config import ConfigError

        _write_config(tmp_path / CONFIG_FILENAME, "autonomy: bogus\n")
        with pytest.raises(ConfigError, match="invalid autonomy"):
            load_config(tmp_path)


class TestMaxAutoFixAttemptsValidation:
    def test_default_is_one(self, tmp_path):
        _write_config(tmp_path / CONFIG_FILENAME, "ticket_prefix: TICK\n")
        cfg = load_config(tmp_path)
        assert cfg["max_auto_fix_attempts"] == 1

    def test_custom_value_accepted(self, tmp_path):
        _write_config(tmp_path / CONFIG_FILENAME, "max_auto_fix_attempts: 3\n")
        cfg = load_config(tmp_path)
        assert cfg["max_auto_fix_attempts"] == 3

    def test_zero_raises(self, tmp_path):
        from lanegate.config import ConfigError

        _write_config(tmp_path / CONFIG_FILENAME, "max_auto_fix_attempts: 0\n")
        with pytest.raises(ConfigError, match="max_auto_fix_attempts"):
            load_config(tmp_path)

    def test_negative_raises(self, tmp_path):
        from lanegate.config import ConfigError

        _write_config(tmp_path / CONFIG_FILENAME, "max_auto_fix_attempts: -1\n")
        with pytest.raises(ConfigError, match="max_auto_fix_attempts"):
            load_config(tmp_path)

    def test_non_int_raises(self, tmp_path):
        from lanegate.config import ConfigError

        _write_config(tmp_path / CONFIG_FILENAME, "max_auto_fix_attempts: not-a-number\n")
        with pytest.raises(ConfigError, match="max_auto_fix_attempts"):
            load_config(tmp_path)


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


# ---------------------------------------------------------------------------
# fix / drift_check step allowlist regression (TICK-120)
#
# Prior to TICK-120, _VALID_EXECUTOR_STEPS and _VALID_MODEL_STEPS only knew
# about "implement"/"review" (and "analyze" for models) — configuring a model
# or executor for the new "fix"/"drift_check" steps would have raised
# ConfigError. This is the regression test for that fix.
# ---------------------------------------------------------------------------


class TestFixDriftCheckStepsAccepted:
    def test_executor_steps_fix_accepted(self, tmp_path):
        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
executor_steps:
  fix: codex
""",
        )
        cfg = load_config(tmp_path)
        assert cfg["executor_steps"]["fix"] == "codex"

    def test_executor_steps_drift_check_accepted(self, tmp_path):
        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
executor_steps:
  drift_check: aider
""",
        )
        cfg = load_config(tmp_path)
        assert cfg["executor_steps"]["drift_check"] == "aider"

    def test_models_fix_accepted(self, tmp_path):
        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
models:
  fix: claude-sonnet-4-5
""",
        )
        cfg = load_config(tmp_path)
        assert cfg["models"]["fix"] == "claude-sonnet-4-5"

    def test_models_drift_check_accepted(self, tmp_path):
        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
models:
  drift_check: claude-opus-4-5
""",
        )
        cfg = load_config(tmp_path)
        assert cfg["models"]["drift_check"] == "claude-opus-4-5"


# ---------------------------------------------------------------------------
# _VALID_EXECUTOR_TYPES — new driver types (TICK-028)
# ---------------------------------------------------------------------------


def test_valid_executor_gemini(tmp_path):
    _write_config(tmp_path / CONFIG_FILENAME, "executor: gemini\n")
    cfg = load_config(tmp_path)
    assert cfg["executor"] == "gemini"


def test_valid_executor_continue(tmp_path):
    _write_config(tmp_path / CONFIG_FILENAME, "executor: continue\n")
    cfg = load_config(tmp_path)
    assert cfg["executor"] == "continue"


# ---------------------------------------------------------------------------
# drivers: / steps: blocks (TICK-028)
# ---------------------------------------------------------------------------


class TestDriversBlock:
    """Tests for the drivers: block — named driver instances."""

    def test_valid_drivers_block_accepted(self, tmp_path):
        """A drivers: block with required 'type' plus optional fields parses through."""
        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
drivers:
  claude-main:
    type: claude-process
    model: claude-sonnet-4-6
  ollama-qwen:
    type: ollama
    model: qwen2.5-coder:32b
    base_url: http://localhost:11434
  aider-local:
    type: aider
    model: ollama/qwen2.5-coder:32b
    bin: aider
    flags: [--no-auto-commits]
""",
        )
        cfg = load_config(tmp_path)
        assert cfg["drivers"]["claude-main"]["type"] == "claude-process"
        assert cfg["drivers"]["claude-main"]["model"] == "claude-sonnet-4-6"
        assert cfg["drivers"]["ollama-qwen"]["base_url"] == "http://localhost:11434"
        assert cfg["drivers"]["aider-local"]["bin"] == "aider"
        assert cfg["drivers"]["aider-local"]["flags"] == ["--no-auto-commits"]

    def test_drivers_block_absent_defaults_to_empty(self, tmp_path):
        """Backward compat: no drivers: block yields cfg['drivers'] == {}."""
        _write_config(tmp_path / CONFIG_FILENAME, "ticket_prefix: TICK\n")
        cfg = load_config(tmp_path)
        assert cfg["drivers"] == {}

    def test_drivers_unknown_type_raises(self, tmp_path):
        """An unrecognised drivers.*.type raises ConfigError."""
        from lanegate.config import ConfigError

        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
drivers:
  bogus-driver:
    type: not-a-real-type
""",
        )
        with pytest.raises(ConfigError, match="unknown type"):
            load_config(tmp_path)

    def test_drivers_missing_type_raises(self, tmp_path):
        """A drivers.<name> entry without a 'type' field raises ConfigError."""
        from lanegate.config import ConfigError

        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
drivers:
  no-type-driver:
    model: some-model
""",
        )
        with pytest.raises(ConfigError, match="missing required 'type'"):
            load_config(tmp_path)

    def test_drivers_name_is_freeform_string(self, tmp_path):
        """Driver names are not validated against a type whitelist — any key works."""
        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
drivers:
  my-weird-driver-name-123:
    type: codex
""",
        )
        cfg = load_config(tmp_path)
        assert "my-weird-driver-name-123" in cfg["drivers"]


class TestStepsBlock:
    """Tests for the steps: block — per-step driver routing."""

    def test_valid_steps_block_referencing_driver(self, tmp_path):
        """steps.*.driver may reference a key defined in drivers:."""
        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
drivers:
  claude-main:
    type: claude-process

steps:
  analyze:
    driver: claude-main
  implement:
    driver: claude-main
  review:
    driver: claude-main
""",
        )
        cfg = load_config(tmp_path)
        assert cfg["steps"]["analyze"]["driver"] == "claude-main"
        assert cfg["steps"]["implement"]["driver"] == "claude-main"
        assert cfg["steps"]["review"]["driver"] == "claude-main"

    def test_valid_steps_block_referencing_legacy_type(self, tmp_path):
        """steps.*.driver may be a bare legacy executor type with no drivers: block."""
        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
steps:
  implement:
    driver: aider
""",
        )
        cfg = load_config(tmp_path)
        assert cfg["steps"]["implement"]["driver"] == "aider"

    def test_steps_block_absent_defaults_to_empty(self, tmp_path):
        """Backward compat: no steps: block yields cfg['steps'] == {}."""
        _write_config(tmp_path / CONFIG_FILENAME, "ticket_prefix: TICK\n")
        cfg = load_config(tmp_path)
        assert cfg["steps"] == {}

    def test_steps_undefined_driver_reference_raises(self, tmp_path):
        """steps.*.driver referencing a name not in drivers: and not a legacy type raises."""
        from lanegate.config import ConfigError

        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
drivers:
  claude-main:
    type: claude-process

steps:
  implement:
    driver: does-not-exist
""",
        )
        with pytest.raises(ConfigError, match="undefined driver"):
            load_config(tmp_path)

    def test_steps_missing_driver_field_raises(self, tmp_path):
        """A steps.<name> entry without a 'driver' field raises ConfigError."""
        from lanegate.config import ConfigError

        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
steps:
  implement: {}
""",
        )
        with pytest.raises(ConfigError, match="missing required 'driver'"):
            load_config(tmp_path)

    def test_steps_unknown_key_raises(self, tmp_path):
        """An unrecognised key under steps: raises ConfigError."""
        from lanegate.config import ConfigError

        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
steps:
  deploy:
    driver: aider
""",
        )
        with pytest.raises(ConfigError, match="unknown key"):
            load_config(tmp_path)


class TestDriversStepsBackwardCompat:
    """Backward compat — executor/reviewer fields work unchanged without drivers:."""

    def test_executor_and_reviewer_unaffected_by_absent_drivers(self, tmp_path):
        _write_config(
            tmp_path / CONFIG_FILENAME,
            "executor: claude-process\nreviewer: aider\n",
        )
        cfg = load_config(tmp_path)
        assert cfg["executor"] == "claude-process"
        assert cfg["reviewer"] == "aider"
        assert cfg["drivers"] == {}
        assert cfg["steps"] == {}

    def test_drivers_and_legacy_executor_can_coexist(self, tmp_path):
        """A drivers:/steps: block can be present alongside the legacy executor field."""
        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
executor: claude
drivers:
  claude-main:
    type: claude-process
steps:
  implement:
    driver: claude-main
""",
        )
        cfg = load_config(tmp_path)
        assert cfg["executor"] == "claude"
        assert cfg["drivers"]["claude-main"]["type"] == "claude-process"
        assert cfg["steps"]["implement"]["driver"] == "claude-main"


# ---------------------------------------------------------------------------
# session_chaining (TICK-310)
# ---------------------------------------------------------------------------


def test_session_chaining_defaults_when_absent(tmp_path):
    cfg = load_config(tmp_path)
    resolved = resolve_session_chaining(cfg)
    assert resolved == {
        "enabled": True,
        "chain_review": False,
        "max_session_age_s": 2700,
        "max_session_tokens": 150000,
    }


def test_session_chaining_partial_override_keeps_other_defaults(tmp_path):
    """A user overriding just one field must not lose the other three
    defaults -- load_config's raw-YAML merge is shallow, so this only works
    if resolve_session_chaining applies defaults per-key."""
    _write_config(
        tmp_path / CONFIG_FILENAME,
        "session_chaining:\n  chain_review: true\n",
    )
    cfg = load_config(tmp_path)
    resolved = resolve_session_chaining(cfg)
    assert resolved["chain_review"] is True
    assert resolved["enabled"] is True
    assert resolved["max_session_age_s"] == 2700
    assert resolved["max_session_tokens"] == 150000


def test_session_chaining_full_override(tmp_path):
    _write_config(
        tmp_path / CONFIG_FILENAME,
        "session_chaining:\n"
        "  enabled: false\n"
        "  chain_review: true\n"
        "  max_session_age_s: 60\n"
        "  max_session_tokens: 1000\n",
    )
    cfg = load_config(tmp_path)
    resolved = resolve_session_chaining(cfg)
    assert resolved == {
        "enabled": False,
        "chain_review": True,
        "max_session_age_s": 60,
        "max_session_tokens": 1000,
    }


def test_session_chaining_not_a_mapping_raises(tmp_path):
    from lanegate.config import ConfigError

    _write_config(tmp_path / CONFIG_FILENAME, "session_chaining: not-a-mapping\n")
    with pytest.raises(ConfigError, match="session_chaining must be a mapping"):
        load_config(tmp_path)


def test_session_chaining_enabled_must_be_bool(tmp_path):
    from lanegate.config import ConfigError

    _write_config(tmp_path / CONFIG_FILENAME, "session_chaining:\n  enabled: yes-please\n")
    with pytest.raises(ConfigError, match="session_chaining.enabled must be true or false"):
        load_config(tmp_path)


def test_session_chaining_max_session_age_s_must_be_positive_int(tmp_path):
    from lanegate.config import ConfigError

    _write_config(tmp_path / CONFIG_FILENAME, "session_chaining:\n  max_session_age_s: -5\n")
    with pytest.raises(ConfigError, match="max_session_age_s must be a positive integer"):
        load_config(tmp_path)


def test_session_chaining_max_session_tokens_must_be_positive_int(tmp_path):
    from lanegate.config import ConfigError

    _write_config(tmp_path / CONFIG_FILENAME, "session_chaining:\n  max_session_tokens: 0\n")
    with pytest.raises(ConfigError, match="max_session_tokens must be a positive integer"):
        load_config(tmp_path)


def test_fail_fast_model_validation_agy_unmapped_model(tmp_path):
    from lanegate.config import ConfigError, load_config

    _write_config(
        tmp_path / CONFIG_FILENAME,
        "executor: agy\nmodels:\n  implement: gemini-3.6-pro\n",
    )
    with pytest.raises(ConfigError, match="unmapped model 'gemini-3.6-pro' for executor 'agy'"):
        load_config(tmp_path)


def test_fail_fast_model_validation_agy_valid_model(tmp_path):
    from lanegate.config import load_config

    _write_config(
        tmp_path / CONFIG_FILENAME,
        "executor: agy\nmodels:\n  implement: gemini-3.6-flash-medium\n",
    )
    cfg = load_config(tmp_path)
    assert cfg["models"]["implement"] == "gemini-3.6-flash-medium"


def test_fail_fast_model_validation_agy_claude_model(tmp_path):
    from lanegate.config import load_config

    _write_config(
        tmp_path / CONFIG_FILENAME,
        "executor: agy\nmodels:\n  implement: claude-sonnet-5\n",
    )
    cfg = load_config(tmp_path)
    assert cfg["models"]["implement"] == "claude-sonnet-5"


def test_fail_fast_model_validation_claude_unmapped_model(tmp_path):
    from lanegate.config import ConfigError, load_config

    _write_config(
        tmp_path / CONFIG_FILENAME,
        "executor: claude\nmodels:\n  implement: gpt-4o\n",
    )
    with pytest.raises(ConfigError, match="unmapped model 'gpt-4o' for executor 'claude'"):
        load_config(tmp_path)
