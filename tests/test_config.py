"""Tests for config.py — load, walk-up discovery, environment validation."""

import json
import os
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

import pytest

from lanegate.config import (
    ConfigError,
    _DEFAULT_ANALYZE_MODEL,
    _DEFAULT_IMPLEMENT_MODEL,
    _DEFAULT_RESUME_CEILING_S,
    _DEFAULT_REVIEW_MODEL,
    _control_checkout_root,
    _trusted_git_executable,
    _windows_git_candidates,
    _gitignore_entries,
    CONFIG_FILENAME,
    _default_config,
    _detect_existing_tickets_dir,
    _update_gitignore,
    detect_test_runner_safeguards,
    find_config,
    find_repo_root,
    interactive_init,
    is_high_reasoning_ticket,
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
    validate_model_for_executor,
)



from tests._helpers.config import write_config as _write_config


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
    assert cfg["review_fallback"] == "needs_review"
    assert cfg["reference_docs"] == []


def test_pre_merge_worktree_safeguard_defaults_true_and_accepts_false(tmp_path):
    assert load_config(tmp_path)["safeguards"].get("pre_merge_worktree", True) is True

    _write_config(
        tmp_path / CONFIG_FILENAME,
        "safeguards:\n  pre_merge_worktree: false\n",
    )
    assert load_config(tmp_path)["safeguards"]["pre_merge_worktree"] is False


def test_pre_merge_worktree_safeguard_requires_boolean(tmp_path):
    _write_config(
        tmp_path / CONFIG_FILENAME,
        "safeguards:\n  pre_merge_worktree: sometimes\n",
    )

    with pytest.raises(ConfigError, match="pre_merge_worktree must be a boolean"):
        load_config(tmp_path)


def test_reference_docs_default(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg["reference_docs"] == []


def test_architecture_doc_deprecation_warning(tmp_path):
    _write_config(tmp_path / CONFIG_FILENAME, "architecture_doc: docs/ARCHITECTURE.md\n")
    with pytest.deprecated_call(match="architecture_doc"):
        load_config(tmp_path)


def test_review_fallback_is_validated(tmp_path):
    _write_config(tmp_path / CONFIG_FILENAME, "review_fallback: same_model\n")
    assert load_config(tmp_path)["review_fallback"] == "same_model"
    _write_config(tmp_path / CONFIG_FILENAME, "review_fallback: arbitrary\n")
    with pytest.raises(ConfigError, match="review_fallback"):
        load_config(tmp_path)


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


def test_current_git_branch_does_not_hang_on_blocked_git(tmp_path):
    """A blocked/wedged `git branch --show-current` (a hook prompting for
    input, a wedged process, an unusual submodule setup) must not hang
    interactive_init indefinitely before it prints even the first prompt --
    matches the timeout=10 the sibling helper _repo_tracked_files() already
    has for the same kind of git subprocess call from the same wizard flow."""
    from lanegate.config import _current_git_branch

    with mock.patch(
        "lanegate.config.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["git", "branch", "--show-current"], timeout=10),
    ) as mock_run:
        assert _current_git_branch(tmp_path) is None
    assert mock_run.call_args.kwargs.get("timeout") == 10


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










@pytest.mark.skipif(
    sys.platform == "win32",
    reason="trust check is PATH+ownership based only on POSIX; Windows uses registry-only lookup",
)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="trust check is PATH+ownership based only on POSIX; Windows uses registry-only lookup",
)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="os.geteuid does not exist on Windows; ownership trust check is POSIX-only",
)














@pytest.mark.skipif(
    sys.platform == "win32",
    reason="a literal newline in a path is not a creatable git worktree name on Windows",
)




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




def test_invalid_max_parallel_raises(tmp_path):
    _write_config(tmp_path / CONFIG_FILENAME, "max_parallel: 0\n")
    with pytest.raises(ValueError, match="max_parallel must be a positive integer"):
        load_config(tmp_path)


def test_invalid_executor_override_raises(tmp_path):
    _write_config(tmp_path / CONFIG_FILENAME, "executors:\n  local: { max_parallel: -1 }\n")
    with pytest.raises(ValueError, match="executors\\['local'\\].max_parallel"):
        load_config(tmp_path)




















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


def test_valid_executor_kiro(tmp_path):
    _write_config(tmp_path / CONFIG_FILENAME, "executor: kiro\n")
    cfg = load_config(tmp_path)
    assert cfg["executor"] == "kiro"


def test_cursor_executor_type_valid(tmp_path):
    _write_config(
        tmp_path / CONFIG_FILENAME,
        "executors:\n  cursor-primary:\n    type: cursor\n  codex-review:\n    type: codex\nexecutor: cursor-primary\nreviewer: codex-review\n",
    )
    cfg = load_config(tmp_path)
    assert cfg["executor"] == "cursor-primary"
    assert cfg["reviewer"] == "codex-review"
    assert cfg["executors"]["cursor-primary"]["type"] == "cursor"


def test_kiro_conflicting_managed_flags_raise_config_error(tmp_path):
    _write_config(
        tmp_path / CONFIG_FILENAME,
        "executors:\n  kiro:\n    flags: [--output-format=json]\nexecutor: kiro\n",
    )
    with pytest.raises(ConfigError, match="may not override Kiro's required headless flags"):
        load_config(tmp_path)


def test_kiro_agent_engine_override_raises_config_error(tmp_path):
    """The managed-flag guard must protect the flag build_executor_cmd actually
    emits (--agent-engine v3), not the pre-rename --engine name."""
    _write_config(
        tmp_path / CONFIG_FILENAME,
        "executors:\n  kiro:\n    flags: [--agent-engine, v1]\nexecutor: kiro\n",
    )
    with pytest.raises(ConfigError, match="may not override Kiro's required headless flags"):
        load_config(tmp_path)


def test_kiro_trust_tools_override_raises_config_error(tmp_path):
    """A user-supplied --trust-tools override must not be able to widen or
    duplicate lanegate's own read-only trust grant (e.g. --trust-tools=write
    alongside lanegate's --trust-tools=read,grep would defeat read-only)."""
    _write_config(
        tmp_path / CONFIG_FILENAME,
        "executors:\n  kiro:\n    flags: [--trust-tools=write]\nexecutor: kiro\n",
    )
    with pytest.raises(ConfigError, match="may not override Kiro's required headless flags"):
        load_config(tmp_path)


def test_valid_executor_named_instance(tmp_path):
    _write_config(
        tmp_path / CONFIG_FILENAME,
        "executors:\n  codex-1:\n    type: codex\nexecutor: codex-1\n",
    )
    assert load_config(tmp_path)["executor"] == "codex-1"


def test_valid_executor_pool(tmp_path):
    _write_config(
        tmp_path / CONFIG_FILENAME,
        "executors:\n  codex-1:\n    type: codex\npools:\n  dev:\n    executors: [codex-1]\nexecutor: dev\n",
    )
    assert load_config(tmp_path)["executor"] == "dev"


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


























# ---------------------------------------------------------------------------
# interactive_init — --defaults / non-TTY path
# ---------------------------------------------------------------------------






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

    def test_cleanup_branch_on_merge_is_true(self):
        cfg = _default_config()
        assert cfg["cleanup_branch_on_merge"] is True

    def test_default_config_derives_from_app_name(self, monkeypatch):
        monkeypatch.setattr("lanegate.config.APP_NAME", "testbrand")
        monkeypatch.setattr("lanegate.config_init.APP_NAME", "testbrand")
        cfg = _default_config()
        assert cfg["tickets_dir"] == ".testbrand/tickets"
        assert cfg["worktrees_dir"] == ".testbrand/worktrees"
        assert _gitignore_entries() == [".testbrand/*", "testbrand-context-log.jsonl"]


def test_cleanup_branch_on_merge_default_true():
    cfg = _default_config()
    assert cfg["cleanup_branch_on_merge"] is True


# ---------------------------------------------------------------------------
# _update_gitignore helper
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# _detect_existing_tickets_dir helper
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# registry_add
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# reviewer validation
# ---------------------------------------------------------------------------


























# ---------------------------------------------------------------------------
# models: block validation
# ---------------------------------------------------------------------------












# ---------------------------------------------------------------------------
# resolve_model — resolution order
# ---------------------------------------------------------------------------




class TestValidateModelForExecutorProvider:
    """Tests for validate_model_for_executor()'s provider-aware aider branch."""

    def test_ollama_provider_rejects_vendor_model(self):
        """An aider instance pinned to provider: ollama cannot use a
        claude-*/gemini-*/gpt-* model name -- that's a misconfiguration
        (e.g. a top-level `models:` block leaking into a pool-dispatched
        Ollama-backed aider executor), not a legitimate multi-provider setup."""
        with pytest.raises(ConfigError, match="unmapped model"):
            validate_model_for_executor("claude-sonnet-5", "aider", "test", provider="ollama")

    def test_ollama_provider_accepts_ollama_model(self):
        validate_model_for_executor(
            "ollama_chat/qwen2.5-coder:14b", "aider", "test", provider="ollama"
        )

    def test_no_provider_preserves_existing_permissive_behavior(self):
        """Without a provider hint, aider's existing multi-vendor allowance
        (aider can proxy to Claude/GPT/Gemini APIs directly) is unchanged."""
        validate_model_for_executor("claude-sonnet-5", "aider", "test")
        validate_model_for_executor("gpt-5.6-terra", "aider", "test")

    def test_non_ollama_provider_preserves_existing_permissive_behavior(self):
        validate_model_for_executor("claude-sonnet-5", "aider", "test", provider="anthropic")


# ---------------------------------------------------------------------------
# resolve_executor — resolution order
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# executor_steps config validation
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# model_settings validation tests (TICK-650)
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# autonomy / max_auto_fix_attempts validation (TICK-120)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Risk-based autonomy lanes (TICK-467)
# ---------------------------------------------------------------------------















# ---------------------------------------------------------------------------
# fix / drift_check step allowlist regression (TICK-120)
#
# Prior to TICK-120, _VALID_EXECUTOR_STEPS and _VALID_MODEL_STEPS only knew
# about "implement"/"review" (and "analyze" for models) — configuring a model
# or executor for the new "fix"/"drift_check" steps would have raised
# ConfigError. This is the regression test for that fix.
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# _VALID_EXECUTOR_TYPES — new driver types (TICK-028)
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# drivers: / steps: blocks (TICK-028)
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# session_chaining (TICK-310)
# ---------------------------------------------------------------------------

def _write_rotation_config(tmp_path, body: str):
    import yaml

    (tmp_path / ".lanegate.yml").write_text(yaml.dump(body), encoding="utf-8")


def test_reviewer_rotation_parses_through_load_config(tmp_path):
    from lanegate.config import load_config

    _write_rotation_config(tmp_path, {"reviewer_rotation": ["agy", "codex"]})
    cfg = load_config(tmp_path)
    assert cfg["reviewer_rotation"] == ["agy", "codex"]


def test_reviewer_rotation_rejects_non_string_entries(tmp_path):
    from lanegate.config import ConfigError, load_config

    _write_rotation_config(tmp_path, {"reviewer_rotation": ["agy", 3]})
    with pytest.raises(ConfigError, match="list of strings"):
        load_config(tmp_path)


def test_reviewer_rotation_rejects_unknown_driver(tmp_path):
    from lanegate.config import ConfigError, load_config

    _write_rotation_config(tmp_path, {"reviewer_rotation": ["agy", "nope-not-a-driver"]})
    with pytest.raises(ConfigError, match="undefined driver"):
        load_config(tmp_path)


def test_reviewer_rotation_rejects_single_entry(tmp_path):
    from lanegate.config import ConfigError, load_config

    _write_rotation_config(tmp_path, {"reviewer_rotation": ["agy", "agy"]})
    with pytest.raises(ConfigError, match="two distinct entries"):
        load_config(tmp_path)


def test_reviewer_rotation_warning_when_driver_pinned(tmp_path):
    from lanegate.config import load_config

    _write_rotation_config(
        tmp_path,
        {"reviewer_rotation": ["agy", "codex"], "steps": {"review": {"driver": "agy"}}},
    )
    with pytest.warns(UserWarning, match="reviewer_rotation is ignored"):
        load_config(tmp_path)

