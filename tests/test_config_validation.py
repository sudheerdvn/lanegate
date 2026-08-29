"""Tests for config_validation.py validators and parsers."""

import pytest

from lanegate.config import (
    CONFIG_FILENAME,
    ConfigError,
    load_config,
    resolve_session_chaining,
)
from tests._helpers.config import write_config as _write_config


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


def test_valid_reviewer_named_instance(tmp_path):
    _write_config(
        tmp_path / CONFIG_FILENAME,
        "executors:\n  agy-1:\n    type: agy\nreviewer: agy-1\n",
    )
    assert load_config(tmp_path)["reviewer"] == "agy-1"


def test_valid_reviewer_pool(tmp_path):
    _write_config(
        tmp_path / CONFIG_FILENAME,
        "executors:\n  agy-1:\n    type: agy\npools:\n  review:\n    executors: [agy-1]\nreviewer: review\n",
    )
    assert load_config(tmp_path)["reviewer"] == "review"


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

    def test_executor_steps_accepts_analyze(self, tmp_path):
        """analyze is a valid executor_steps key (TICK-573) -- lets a ticket
        route analyze to a dedicated executor instance without changing the
        ticket-level executor: entirely."""
        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
executor_steps:
  analyze: claude
""",
        )
        cfg = load_config(tmp_path)
        assert cfg["executor_steps"]["analyze"] == "claude"

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


class TestAiderModelSettings:
    """Validate executors.aider.model_settings block in load_config."""

    def test_model_settings_valid_shape(self, tmp_path):
        """A well-formed model_settings block parses without ConfigError."""
        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
executors:
  aider:
    edit_format: diff
    context_window_tokens: 65536
    model_settings:
      'ollama_chat/gpt-oss:20b':
        context_window_tokens: 131072
        edit_format: whole
      'ollama_chat/qwen2.5-coder:14b':
        context_window_tokens: 49152
""",
        )
        cfg = load_config(tmp_path)
        ms = cfg["executors"]["aider"]["model_settings"]
        assert ms["ollama_chat/gpt-oss:20b"]["context_window_tokens"] == 131072
        assert ms["ollama_chat/gpt-oss:20b"]["edit_format"] == "whole"
        assert ms["ollama_chat/qwen2.5-coder:14b"]["context_window_tokens"] == 49152

    def test_model_settings_rejects_neutralize_whole(self, tmp_path):
        """neutralize_touches: true cannot coexist with edit_format: whole, either at flat level or in model_settings."""
        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
executors:
  aider:
    neutralize_touches: true
    edit_format: whole
""",
        )
        with pytest.raises(ConfigError, match="cannot combine neutralize_touches: true with edit_format: 'whole'"):
            load_config(tmp_path)

        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
executors:
  aider:
    neutralize_touches: true
    model_settings:
      'ollama_chat/qwen2.5-coder:14b':
        edit_format: whole
""",
        )
        with pytest.raises(ConfigError, match="cannot combine neutralize_touches: true with edit_format: 'whole'"):
            load_config(tmp_path)

    def test_model_settings_invalid_context_window_tokens(self, tmp_path):
        """context_window_tokens=0 under model_settings raises ConfigError
        (same constraint as the flat key)."""
        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
executors:
  aider:
    context_window_tokens: 65536
    model_settings:
      'ollama_chat/qwen2.5-coder:14b':
        context_window_tokens: 0
""",
        )
        with pytest.raises(ConfigError, match="context_window_tokens must be a positive integer"):
            load_config(tmp_path)

    def test_model_settings_invalid_context_window_tokens_negative(self, tmp_path):
        """context_window_tokens=-1 under model_settings raises ConfigError."""
        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
executors:
  aider:
    model_settings:
      'ollama_chat/qwen2.5-coder:14b':
        context_window_tokens: -1
""",
        )
        with pytest.raises(ConfigError, match="context_window_tokens must be a positive integer"):
            load_config(tmp_path)

    def test_model_settings_invalid_edit_format(self, tmp_path):
        """An empty string for edit_format under model_settings raises ConfigError
        (same constraint as the flat key)."""
        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
executors:
  aider:
    model_settings:
      'ollama_chat/qwen2.5-coder:14b':
        edit_format: ''
""",
        )
        with pytest.raises(ConfigError, match="edit_format must be a non-empty string"):
            load_config(tmp_path)

    def test_model_settings_unknown_key_raises_if_flat_does(self, tmp_path):
        """An unknown sub-key inside a model_settings entry raises ConfigError,
        mirroring the flat-key validator's rejection of unknown keys."""
        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
executors:
  aider:
    model_settings:
      'ollama_chat/qwen2.5-coder:14b':
        context_window_tokens: 49152
        unknown_key: some_value
""",
        )
        with pytest.raises(ConfigError, match="unknown key"):
            load_config(tmp_path)

    def test_model_settings_absent_passes_validation(self, tmp_path):
        """An aider config without model_settings passes validation unchanged
        (backward compatibility: existing flat configs are unaffected)."""
        _write_config(
            tmp_path / CONFIG_FILENAME,
            """
executors:
  aider:
    edit_format: diff
    context_window_tokens: 65536
""",
        )
        cfg = load_config(tmp_path)
        assert cfg["executors"]["aider"].get("model_settings") is None


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


class TestRiskAutonomyLanesConfigValidation:
    def test_risk_autonomy_lanes_config_validation(self, tmp_path):
        """green/yellow/red are accepted as top-level autonomy, resolve_autonomy
        surfaces them unchanged, and human_escalation triggers validate/resolve
        with defaults merged onto project overrides."""
        from lanegate.config import (
            ConfigError,
            is_auto_fix_lane,
            is_red_lane,
            resolve_autonomy,
            resolve_human_escalation,
        )

        for lane in ("green", "yellow", "red"):
            _write_config(tmp_path / CONFIG_FILENAME, f"autonomy: {lane}\n")
            cfg = load_config(tmp_path)
            assert cfg["autonomy"] == lane
            assert resolve_autonomy(cfg) == lane

        assert is_auto_fix_lane("green") is True
        assert is_auto_fix_lane("yellow") is True
        assert is_auto_fix_lane("full") is True
        assert is_auto_fix_lane("red") is False
        assert is_auto_fix_lane("supervised") is False
        assert is_red_lane("red") is True
        assert is_red_lane("green") is False

        # human_escalation defaults, with no project override.
        _write_config(tmp_path / CONFIG_FILENAME, "ticket_prefix: TICK\n")
        cfg = load_config(tmp_path)
        assert resolve_human_escalation(cfg) == {
            "credentials": True,
            "security_actions": True,
            "retry_limit": 3,
        }

        # Project overrides merge onto defaults.
        _write_config(
            tmp_path / CONFIG_FILENAME,
            "human_escalation:\n  credentials: false\n  retry_limit: 5\n",
        )
        cfg = load_config(tmp_path)
        escalation = resolve_human_escalation(cfg)
        assert escalation["credentials"] is False
        assert escalation["security_actions"] is True
        assert escalation["retry_limit"] == 5

        # Invalid human_escalation shapes raise.
        _write_config(tmp_path / CONFIG_FILENAME, "human_escalation: not-a-mapping\n")
        with pytest.raises(ConfigError, match="human_escalation"):
            load_config(tmp_path)

        _write_config(
            tmp_path / CONFIG_FILENAME, "human_escalation:\n  credentials: not-a-bool\n"
        )
        with pytest.raises(ConfigError, match="human_escalation.credentials"):
            load_config(tmp_path)

        _write_config(
            tmp_path / CONFIG_FILENAME, "human_escalation:\n  retry_limit: 0\n"
        )
        with pytest.raises(ConfigError, match="human_escalation.retry_limit"):
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

    def test_repo_config_effective_fix_budget(self, tmp_path):
        from lanegate.config import load_config, resolve_human_escalation

        _write_config(tmp_path / CONFIG_FILENAME, "max_auto_fix_attempts: 2\n")
        cfg = load_config(tmp_path)

        assert cfg["max_auto_fix_attempts"] == 2
        retry_limit = resolve_human_escalation(cfg)["retry_limit"]
        assert retry_limit >= cfg["max_auto_fix_attempts"]
        effective_budget = min(cfg["max_auto_fix_attempts"], retry_limit)
        assert effective_budget == 2


class TestBudgetCapsValidation:
    def test_defaults_are_none(self, tmp_path):
        _write_config(tmp_path / CONFIG_FILENAME, "ticket_prefix: TICK\n")
        cfg = load_config(tmp_path)
        assert cfg["max_turns"] is None
        assert cfg["max_cumulative_tokens"] is None

    def test_custom_positive_integers_accepted(self, tmp_path):
        _write_config(tmp_path / CONFIG_FILENAME, "max_turns: 50\nmax_cumulative_tokens: 1000000\n")
        cfg = load_config(tmp_path)
        assert cfg["max_turns"] == 50
        assert cfg["max_cumulative_tokens"] == 1000000

    def test_per_step_mapping_accepted(self, tmp_path):
        _write_config(tmp_path / CONFIG_FILENAME, "max_turns:\n  implement: 50\n  review: 30\n")
        cfg = load_config(tmp_path)
        assert cfg["max_turns"] == {"implement": 50, "review": 30}

    def test_unknown_step_key_raises(self, tmp_path):
        from lanegate.config import ConfigError

        for key in ("max_turns", "max_cumulative_tokens"):
            _write_config(tmp_path / CONFIG_FILENAME, f"{key}:\n  fixx: 30\n")
            with pytest.raises(ConfigError, match="unknown key"):
                load_config(tmp_path)

    def test_invalid_max_turns_raises(self, tmp_path):
        from lanegate.config import ConfigError

        _write_config(tmp_path / CONFIG_FILENAME, "max_turns: 0\n")
        with pytest.raises(ConfigError, match="max_turns"):
            load_config(tmp_path)

        _write_config(tmp_path / CONFIG_FILENAME, "max_turns: -5\n")
        with pytest.raises(ConfigError, match="max_turns"):
            load_config(tmp_path)

        _write_config(tmp_path / CONFIG_FILENAME, "max_turns: invalid\n")
        with pytest.raises(ConfigError, match="max_turns"):
            load_config(tmp_path)

    def test_invalid_max_cumulative_tokens_raises(self, tmp_path):
        from lanegate.config import ConfigError

        _write_config(tmp_path / CONFIG_FILENAME, "max_cumulative_tokens: 0\n")
        with pytest.raises(ConfigError, match="max_cumulative_tokens"):
            load_config(tmp_path)

        _write_config(tmp_path / CONFIG_FILENAME, "max_cumulative_tokens: -100\n")
        with pytest.raises(ConfigError, match="max_cumulative_tokens"):
            load_config(tmp_path)

        _write_config(tmp_path / CONFIG_FILENAME, "max_cumulative_tokens: invalid\n")
        with pytest.raises(ConfigError, match="max_cumulative_tokens"):
            load_config(tmp_path)


class TestProfileValidation:
    """Tests for the profile config key and its interaction with review_fallback."""

    def test_profile_defaults_to_default(self, tmp_path):
        assert load_config(tmp_path)["profile"] == "default"

    def test_valid_strict_profile_loads(self, tmp_path):
        _write_config(tmp_path / CONFIG_FILENAME, "profile: strict\n")
        assert load_config(tmp_path)["profile"] == "strict"

    def test_invalid_profile_raises(self, tmp_path):
        _write_config(tmp_path / CONFIG_FILENAME, "profile: yolo\n")
        with pytest.raises(ConfigError, match="profile"):
            load_config(tmp_path)

    def test_strict_profile_rejects_same_model_fallback(self, tmp_path):
        _write_config(
            tmp_path / CONFIG_FILENAME,
            "profile: strict\nreview_fallback: same_model\n",
        )
        with pytest.raises(ConfigError, match="same_model"):
            load_config(tmp_path)

    def test_strict_profile_allows_needs_review_fallback(self, tmp_path):
        _write_config(
            tmp_path / CONFIG_FILENAME,
            "profile: strict\nreview_fallback: needs_review\n",
        )
        assert load_config(tmp_path)["review_fallback"] == "needs_review"

    def test_strict_profile_allows_different_model_fallback(self, tmp_path):
        _write_config(
            tmp_path / CONFIG_FILENAME,
            "profile: strict\nreview_fallback: different_model\n",
        )
        assert load_config(tmp_path)["review_fallback"] == "different_model"

    def test_default_profile_still_allows_same_model_fallback(self, tmp_path):
        _write_config(tmp_path / CONFIG_FILENAME, "review_fallback: same_model\n")
        assert load_config(tmp_path)["review_fallback"] == "same_model"


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


def test_valid_executor_gemini(tmp_path):
    _write_config(tmp_path / CONFIG_FILENAME, "executor: gemini\n")
    cfg = load_config(tmp_path)
    assert cfg["executor"] == "gemini"


def test_valid_executor_continue(tmp_path):
    _write_config(tmp_path / CONFIG_FILENAME, "executor: continue\n")
    cfg = load_config(tmp_path)
    assert cfg["executor"] == "continue"


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


def test_fail_fast_model_validation_agy_gemini_3_1_pro(tmp_path):
    from lanegate.config import load_config

    _write_config(
        tmp_path / CONFIG_FILENAME,
        "executor: agy\nmodels:\n  implement: gemini-3.1-pro-high\n",
    )
    cfg = load_config(tmp_path)
    assert cfg["models"]["implement"] == "gemini-3.1-pro-high"

    _write_config(
        tmp_path / CONFIG_FILENAME,
        "executor: agy\nmodels:\n  implement: gemini-3.1-pro-low\n",
    )
    cfg = load_config(tmp_path)
    assert cfg["models"]["implement"] == "gemini-3.1-pro-low"


def test_fail_fast_model_validation_agy_gpt_oss_medium(tmp_path):
    from lanegate.config import load_config

    _write_config(
        tmp_path / CONFIG_FILENAME,
        "executor: agy\nmodels:\n  implement: gpt-oss-120b-medium\n",
    )
    cfg = load_config(tmp_path)
    assert cfg["models"]["implement"] == "gpt-oss-120b-medium"


def test_fail_fast_model_validation_agy_bare_gemini_pro_rejected(tmp_path):
    from lanegate.config import ConfigError, load_config

    _write_config(
        tmp_path / CONFIG_FILENAME,
        "executor: agy\nmodels:\n  implement: gemini-3.1-pro\n",
    )
    with pytest.raises(ConfigError, match="unmapped model 'gemini-3.1-pro' for executor 'agy'"):
        load_config(tmp_path)


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


def test_fail_fast_model_validation_uses_named_driver_for_review(tmp_path):
    from lanegate.config import ConfigError, load_config

    config = (
        "executor: claude\n"
        "drivers:\n"
        "  codex-review:\n"
        "    type: codex\n"
        "steps:\n"
        "  review:\n"
        "    driver: codex-review\n"
        "models:\n"
        "  review: {model}\n"
    )
    _write_config(tmp_path / CONFIG_FILENAME, config.format(model="claude-sonnet-4-5"))
    with pytest.raises(
        ConfigError,
        match="unmapped model 'claude-sonnet-4-5' for executor 'codex'",
    ):
        load_config(tmp_path)

    _write_config(tmp_path / CONFIG_FILENAME, config.format(model="gpt-4o"))
    cfg = load_config(tmp_path)
    assert cfg["models"]["review"] == "gpt-4o"


def test_fail_fast_model_validation_routes_review_escalation_to_review_driver(tmp_path):
    from lanegate.config import ConfigError, load_config

    _write_config(
        tmp_path / CONFIG_FILENAME,
        "executor: claude\n"
        "drivers:\n"
        "  codex-review:\n"
        "    type: codex\n"
        "steps:\n"
        "  review:\n"
        "    driver: codex-review\n"
        "models:\n"
        "  review_escalation: claude-sonnet-4-5\n",
    )
    with pytest.raises(
        ConfigError,
        match="unmapped model 'claude-sonnet-4-5' for executor 'codex'",
    ):
        load_config(tmp_path)


def test_fail_fast_model_validation_aider_ollama(tmp_path):
    """Aider executor: ollama_chat/ and ollama/ prefixes are accepted; bare names raise."""
    from lanegate.config import ConfigError, load_config

    # --- valid: ollama_chat/ prefix ---
    _write_config(
        tmp_path / CONFIG_FILENAME,
        "executor: aider\nmodels:\n  implement: ollama_chat/qwen2.5-coder:14b\n",
    )
    cfg = load_config(tmp_path)
    assert cfg["models"]["implement"] == "ollama_chat/qwen2.5-coder:14b"

    # --- valid: ollama/ prefix ---
    _write_config(
        tmp_path / CONFIG_FILENAME,
        "executor: aider\nmodels:\n  implement: ollama/llama3.1\n",
    )
    cfg = load_config(tmp_path)
    assert cfg["models"]["implement"] == "ollama/llama3.1"

    # --- adversarial: bare Ollama tag without any prefix raises ConfigError ---
    _write_config(
        tmp_path / CONFIG_FILENAME,
        "executor: aider\nmodels:\n  implement: qwen2.5-coder:14b\n",
    )
    with pytest.raises(ConfigError, match="unmapped model 'qwen2.5-coder:14b' for executor 'aider'"):
        load_config(tmp_path)

    # --- compatibility: other supported prefixes still pass (claude-, gpt-, deepseek) ---
    for valid_model in ("claude-sonnet-4-5", "gpt-4o", "deepseek-coder"):
        _write_config(
            tmp_path / CONFIG_FILENAME,
            f"executor: aider\nmodels:\n  implement: {valid_model}\n",
        )
        cfg = load_config(tmp_path)
        assert cfg["models"]["implement"] == valid_model


def test_cmd_open_and_load_config_skips_models_analyze_validation(tmp_path):
    from lanegate.config import load_config, CONFIG_FILENAME
    from lanegate.lifecycle import cmd_open
    from lanegate.analyze import cmd_analyze
    from lanegate.ticket import parse_ticket

    _write_config(
        tmp_path / CONFIG_FILENAME,
        "executor: aider\nmodels:\n  analyze: qwen2.5-coder:14b\n",
    )
    cfg = load_config(tmp_path)
    assert cfg["models"]["analyze"] == "qwen2.5-coder:14b"

    tickets_dir = tmp_path / cfg["tickets_dir"]
    tickets_dir.mkdir(parents=True, exist_ok=True)
    ticket_file = tickets_dir / "TICK-001.md"
    ticket_file.write_text(
        "---\nid: TICK-001\ntitle: Test Ticket\nstatus: draft\ntouches:\n  - src/foo.py\n---\n"
    )

    cmd_open("TICK-001", cfg, tmp_path)
    ticket = parse_ticket(ticket_file)
    assert ticket["status"] == "open"

    with pytest.raises(SystemExit):
        cmd_analyze("TICK-001", cfg, tmp_path, model_fn=lambda p: '{"touches": ["src/foo.py"], "close_criteria": "done"}')


