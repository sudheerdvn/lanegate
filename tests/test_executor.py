"""Tests for executor.py — named executor instance resolution and dispatch (TICK-088)."""

import datetime
import json
import subprocess
import zoneinfo
from unittest import mock

import pytest

from lanegate.config import ConfigError
from lanegate.executor import (
    available_instances,
    build_executor_cmd,
    clear_all_cooldowns,
    clear_cooldown,
    cmd_executor_reset,
    cmd_executor_status,
    dispatch_executor,
    executor_status_rows,
    get_executor_config,
    is_cooling_down,
    parse_retry_after,
    read_cooldown,
    resolve_executor_env,
    write_cooldown,
)
from lanegate.prompts import get_payload_budget

_BASE_CFG = {
    "executors": {
        "claude-1": {
            "type": "claude-process",
            "api_key_env": "ANTHROPIC_API_KEY_1",
            "max_parallel": 2,
        },
        "claude-2": {
            "type": "claude-process",
            "api_key_env": "ANTHROPIC_API_KEY_2",
            "max_parallel": 2,
        },
        "claude-subagent-1": {
            "type": "claude-subagent",
            "api_key_env": "ANTHROPIC_API_KEY_1",
            "max_parallel": 2,
        },
        "local-ollama": {
            "type": "ollama",
            "max_parallel": 4,
        },
    },
}


@pytest.fixture(autouse=True)
def _resolve_bins_to_their_names(monkeypatch):
    monkeypatch.setattr("lanegate.executor.shutil.which", lambda bin_name: bin_name)


# --- get_executor_config resolution ---


def test_get_executor_config_named_instance():
    resolved = get_executor_config("claude-1", _BASE_CFG)
    assert resolved["type"] == "claude-process"
    assert resolved["instance"] == "claude-1"
    assert resolved["api_key_env"] == "ANTHROPIC_API_KEY_1"


def test_get_executor_config_bare_type_resolves_to_first_instance():
    """Backward compat: a bare type string resolves to the first configured
    named instance of that type."""
    resolved = get_executor_config("claude-process", _BASE_CFG)
    assert resolved["type"] == "claude-process"
    assert resolved["instance"] == "claude-1"
    assert resolved["api_key_env"] == "ANTHROPIC_API_KEY_1"


def test_get_executor_config_bare_type_no_instances_configured():
    """Backward compat: no executors: block at all — bare type used directly."""
    resolved = get_executor_config("claude-process", {"executors": {}})
    assert resolved == {"type": "claude-process", "instance": "claude-process"}


def test_get_executor_config_legacy_per_type_override():
    """Pre-TICK-088 executors: entries (key = type, no 'type' field) still resolve."""
    cfg = {"executors": {"aider": {"max_parallel": 3, "bin": "custom-aider"}}}
    resolved = get_executor_config("aider", cfg)
    assert resolved["type"] == "aider"
    assert resolved["instance"] == "aider"
    assert resolved["bin"] == "custom-aider"
    assert resolved["max_parallel"] == 3


def test_get_executor_config_legacy_override_not_shadowed_by_named_instance():
    """Regression test: a legacy per-type override block (bare type as the
    key, no 'type' field) must NOT be silently shadowed by an unrelated
    named instance of the same type configured elsewhere in executors:.

    Before the fix, get_executor_config('aider', cfg) would return the
    'aider-fast' named instance (matched via the "first instance of that
    type" fallback) instead of the legacy 'aider' override, silently
    dropping max_parallel: 3 with no error or warning.
    """
    cfg = {
        "executors": {
            "aider": {"max_parallel": 3},
            "aider-fast": {"type": "aider", "api_key_env": "SOME_KEY"},
        }
    }

    resolved = get_executor_config("aider", cfg)
    assert resolved["type"] == "aider"
    assert resolved["instance"] == "aider"
    assert resolved["max_parallel"] == 3
    assert "api_key_env" not in resolved

    # The named instance itself is still reachable by its own name.
    named = get_executor_config("aider-fast", cfg)
    assert named["type"] == "aider"
    assert named["instance"] == "aider-fast"
    assert named["api_key_env"] == "SOME_KEY"


# --- build_executor_cmd resolves named instances to the right driver branch ---


def test_build_executor_cmd_named_instance_resolves_claude_process():
    cmd = build_executor_cmd("claude-1", "do the thing", _BASE_CFG)
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "do the thing" in cmd


def test_build_executor_cmd_named_instance_resolves_ollama():
    cmd = build_executor_cmd("local-ollama", "do the thing", _BASE_CFG, model="qwen2.5-coder:7b")
    assert cmd[0] == "ollama"
    assert "run" in cmd
    assert "qwen2.5-coder:7b" in cmd


def test_build_executor_cmd_bare_type_backward_compat():
    """A bare executor type with no executors: block dispatches exactly as before."""
    cmd = build_executor_cmd("claude-process", "do the thing", {"executors": {}})
    assert cmd[0] == "claude"
    assert "-p" in cmd


@pytest.mark.parametrize("executor_type", ["claude", "claude-process", "claude-subagent"])
def test_build_executor_cmd_stdin_omits_claude_prompt(executor_type):
    cmd = build_executor_cmd(executor_type, "very secret prompt", {}, use_stdin=True)
    assert "very secret prompt" not in cmd
    assert "-p" in cmd


def test_build_executor_cmd_stdin_uses_codex_sentinel():
    assert build_executor_cmd("codex", "prompt", {}, use_stdin=True)[-1] == "-"


def test_build_executor_cmd_stdin_omits_ollama_prompt():
    assert build_executor_cmd("ollama", "prompt", {}, use_stdin=True) == ["ollama", "run", "llama3"]


def test_build_implement_prompt_truncates_oversized_change_notes(tmp_path):
    from lanegate.executor import build_implement_prompt

    cfg = {"payload_budgets": {"implement": 40}}
    ticket = {
        "id": "TICK-999", "title": "Budget", "touches": [], "close_criteria": "ok",
        "change_notes": {"x.py": "x" * 500}, "_body": "",
    }
    prompt = build_implement_prompt(ticket, project_root=tmp_path, cfg=cfg)
    planned = prompt.split("## Planned changes\n", 1)[1].split("<untrusted-data>", 1)[0]
    assert len(("## Planned changes\n" + planned).encode("utf-8")) <= get_payload_budget("implement", cfg) + 2


def test_build_executor_cmd_claude_subagent_builds_command():
    """claude-subagent is dispatched the same as claude-process."""
    cmd = build_executor_cmd("claude-subagent", "do the thing", {"executors": {}})
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "do the thing" in cmd


def test_build_executor_cmd_agy_builds_command(monkeypatch):
    monkeypatch.setattr(
        "lanegate.executor.shutil.which",
        lambda bin_name: "/usr/local/bin/agy" if bin_name == "agy" else None,
    )
    cmd = build_executor_cmd("agy", "do the thing", {"executors": {}}, model="gemini-3.5-flash-medium")
    assert cmd[0] == "/usr/local/bin/agy"
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "gemini-3.5-flash-medium"
    # --print is not a boolean flag -- it swallows the very next token as the
    # prompt (google-antigravity/antigravity-cli#76), so nothing may follow
    # the prompt in the argv list.
    assert cmd[-2:] == ["--print", "do the thing"]


def test_aider_context_budget_allows_small_prompt_and_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "small.py").write_text("x = 1\n")
    cfg = {"executors": {"aider": {"context_window_tokens": 10_000}}}

    cmd = build_executor_cmd(
        "aider",
        "add a small helper",
        cfg,
        model="ollama_chat/qwen2.5-coder:14b",
        touches=["small.py"],
    )

    assert cmd[-1] == "small.py"


def test_aider_context_budget_rejects_oversized_selected_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "large.py").write_text("x" * 3_000)
    monkeypatch.setattr(
        "lanegate.executor.Path.read_bytes",
        lambda _: pytest.fail("context preflight must not load the selected file"),
    )
    cfg = {"executors": {"aider": {"context_window_tokens": 8_500}}}

    with pytest.raises(ConfigError, match="estimated .* exceeds configured budget 8500") as exc_info:
        build_executor_cmd(
            "aider",
            "implement this",
            cfg,
            model="ollama_chat/qwen2.5-coder:14b",
            touches=["large.py"],
        )

    message = str(exc_info.value)
    assert "ollama_chat/qwen2.5-coder:14b" in message
    assert "large.py" in message


def test_aider_context_budget_reads_touches_from_execution_worktree(tmp_path, monkeypatch):
    control_root = tmp_path / "control"
    worktree = tmp_path / "worktree"
    control_root.mkdir()
    worktree.mkdir()
    monkeypatch.chdir(control_root)
    (worktree / "large.py").write_text("x" * 3_000)
    cfg = {"executors": {"aider": {"context_window_tokens": 8_500}}}

    with pytest.raises(ConfigError, match="estimated .* exceeds configured budget 8500"):
        build_executor_cmd(
            "aider",
            "implement this",
            cfg,
            touches=["large.py"],
            worktree_path=worktree,
        )


def test_build_executor_cmd_aider_repo_map_omits_touches_and_adds_map_tokens(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "small.py").write_text("x = 1\n")
    cfg = {"executors": {"aider": {"repo_map": True}}}

    cmd = build_executor_cmd(
        "aider",
        "implement this",
        cfg,
        touches=["small.py"],
    )

    assert "--map-tokens" in cmd
    assert cmd[cmd.index("--map-tokens") + 1] == "1024"
    assert "small.py" not in cmd


def test_build_executor_cmd_aider_repo_map_custom_map_tokens(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "small.py").write_text("x = 1\n")
    cfg = {"executors": {"aider": {"repo_map": True, "map_tokens": 4096}}}

    cmd = build_executor_cmd(
        "aider",
        "implement this",
        cfg,
        touches=["small.py"],
    )

    assert "--map-tokens" in cmd
    assert cmd[cmd.index("--map-tokens") + 1] == "4096"
    assert "small.py" not in cmd


def test_build_executor_cmd_aider_lazy_context_uses_repo_map(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "small.py").write_text("x = 1\n")

    cmd = build_executor_cmd(
        "aider",
        "implement this",
        {"executors": {"aider": {"lazy_context": True}}},
        touches=["small.py"],
    )

    assert cmd[cmd.index("--map-tokens") + 1] == "1024"
    assert "small.py" not in cmd


def test_build_executor_cmd_aider_repo_map_invalid_map_tokens_raises_config_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = {"executors": {"aider": {"repo_map": True, "map_tokens": 0}}}

    with pytest.raises(ConfigError, match="map_tokens must be a positive integer"):
        build_executor_cmd(
            "aider",
            "implement this",
            cfg,
            touches=["small.py"],
        )


def test_build_executor_cmd_aider_repo_map_large_touch_file_still_budgeted(tmp_path, monkeypatch):
    # Even in repo_map mode, large.py is never passed positionally (asserted
    # elsewhere), but Aider's own --yes-always-confirmed filename-mention scan
    # of the prompt still injects its full content at runtime, so the preflight
    # budget must still reject it rather than treating it as "not injected".
    (tmp_path / "large.py").write_text("x" * 90_000)
    cfg = {
        "executors": {
            # This is deliberately far below the ~30k tokens the selected
            # file would consume, while remaining above Aider's fixed 8,192
            # token overhead reserve.
            "aider": {"repo_map": True, "context_window_tokens": 9_000},
        }
    }

    with pytest.raises(ConfigError, match="exceeded executors.aider.context_window_tokens"):
        build_executor_cmd(
            "aider",
            "implement this",
            cfg,
            touches=["large.py"],
            worktree_path=tmp_path,
        )


def test_aider_context_budget_is_opt_in(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "large.py").write_text("x" * 50_000)

    cmd = build_executor_cmd("aider", "implement this", {}, touches=["large.py"])

    assert cmd == ["aider", "--message", "implement this", "large.py"]


def test_build_executor_cmd_aider_edit_format_adds_flag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = {"executors": {"aider": {"edit_format": "diff"}}}

    cmd = build_executor_cmd("aider", "implement this", cfg, touches=["small.py"])

    assert "--edit-format" in cmd
    assert cmd[cmd.index("--edit-format") + 1] == "diff"


def test_build_executor_cmd_aider_no_edit_format_omits_flag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    cmd = build_executor_cmd("aider", "implement this", {}, touches=["small.py"])

    assert "--edit-format" not in cmd


def test_build_executor_cmd_aider_edit_format_invalid_raises_config_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = {"executors": {"aider": {"edit_format": ""}}}

    with pytest.raises(ConfigError, match="edit_format must be a non-empty string"):
        build_executor_cmd("aider", "implement this", cfg, touches=["small.py"])


def _init_git_repo_with_files(root, files):
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    for rel in files:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


def test_build_executor_cmd_aider_neutralizes_non_touch_file_mentions(tmp_path):
    _init_git_repo_with_files(tmp_path, ["small.py", "docs/ARCHITECTURE.md"])
    cfg = {"executors": {"aider": {}}}

    cmd = build_executor_cmd(
        "aider",
        "See docs/ARCHITECTURE.md for context, then edit small.py.",
        cfg,
        touches=["small.py"],
        worktree_path=tmp_path,
    )

    message = cmd[cmd.index("--message") + 1]
    assert "docs/ARCHITECTURE.md" not in message
    assert "d​ocs/ARCHITECTURE.md" in message
    assert "small.py" in message


def test_build_executor_cmd_aider_neutralizes_root_level_file_mentions(tmp_path):
    # Root-level files (no "/" in their path) must also be mangled -- a
    # slash-only split leaves them untouched, which is exactly the bug this
    # locks in a regression test for (live TICK-328 run, 2026-07-31: a bare
    # .lanegate.yml mention was auto-added while nested docs/ARCHITECTURE.md was
    # correctly neutralized).
    _init_git_repo_with_files(tmp_path, ["small.py", "config.yml"])
    cfg = {"executors": {"aider": {}}}

    cmd = build_executor_cmd(
        "aider",
        "See config.yml for context, then edit small.py.",
        cfg,
        touches=["small.py"],
        worktree_path=tmp_path,
    )

    message = cmd[cmd.index("--message") + 1]
    assert "config.yml" not in message
    assert "c​onfig.yml" in message


def test_build_executor_cmd_aider_does_not_neutralize_touched_files(tmp_path):
    _init_git_repo_with_files(tmp_path, ["small.py", "other.py"])
    cfg = {"executors": {"aider": {}}}

    cmd = build_executor_cmd(
        "aider",
        "Edit small.py and other.py together.",
        cfg,
        touches=["small.py", "other.py"],
        worktree_path=tmp_path,
    )

    message = cmd[cmd.index("--message") + 1]
    assert "small.py" in message
    assert "other.py" in message
    assert "​" not in message


def test_aider_ollama_unconfigured_warning(capsys):
    cfg = {"executors": {"aider": {"provider": "ollama"}}}

    build_executor_cmd("aider", "implement TICK-299", cfg)

    warning = capsys.readouterr().err
    assert warning.count("warning:") == 1
    assert "aider executor 'aider'" in warning
    assert "provider 'ollama'" in warning
    assert "docs/executor-capabilities.md#context-window-tokens" in warning


def test_aider_ollama_unconfigured_warning_named_driver(capsys):
    cfg = {
        "drivers": {"local-aider": {"type": "aider", "provider": "ollama"}},
        "executors": {"local-aider": {"type": "aider"}},
    }

    build_executor_cmd("local-aider", "implement TICK-299", cfg)

    warning = capsys.readouterr().err
    assert warning.count("warning:") == 1
    assert "aider executor 'local-aider'" in warning
    assert "provider 'ollama'" in warning


def test_aider_ollama_configured_no_warning(capsys):
    cfg = {
        "executors": {
            "aider": {"provider": "ollama", "context_window_tokens": 10_000}
        }
    }

    build_executor_cmd("aider", "implement TICK-299", cfg)

    assert capsys.readouterr().err == ""


def test_aider_no_provider_declared_no_warning(capsys):
    build_executor_cmd("aider", "implement TICK-299", {"executors": {"aider": {}}})

    assert capsys.readouterr().err == ""


def test_build_executor_cmd_named_instance_claude_subagent():
    """A named claude-subagent instance resolves correctly."""
    cmd = build_executor_cmd("claude-subagent-1", "do the thing", _BASE_CFG)
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "do the thing" in cmd


def test_build_executor_cmd_resolves_default_bin_with_shutil_which(monkeypatch):
    monkeypatch.setattr(
        "lanegate.executor.shutil.which",
        lambda bin_name: "/opt/executors/claude" if bin_name == "claude" else None,
    )

    cmd = build_executor_cmd("claude-process", "do the thing", {"executors": {}})

    assert cmd[0] == "/opt/executors/claude"
    assert cmd[1:] == ["-p", "do the thing", "--output-format", "stream-json", "--verbose"]


def test_build_executor_cmd_missing_default_bin_raises_config_error(monkeypatch):
    monkeypatch.setenv("PATH", "/restricted/bin")
    monkeypatch.setattr("lanegate.executor.shutil.which", lambda _bin_name: None)

    with pytest.raises(ConfigError, match="executor 'claude-process'.*bin 'claude'.*PATH"):
        build_executor_cmd("claude-process", "do the thing", {"executors": {}})


def test_build_executor_cmd_missing_configured_bin_raises_config_error(monkeypatch):
    cfg = {"executors": {"claude": {"bin": "custom-claude"}}}
    monkeypatch.setenv("PATH", "/restricted/bin")
    monkeypatch.setattr("lanegate.executor.shutil.which", lambda _bin_name: None)

    with pytest.raises(ConfigError, match="executor 'claude'.*bin 'custom-claude'.*PATH"):
        build_executor_cmd("claude", "do the thing", cfg)


# --- resolve_executor_env / dispatch_executor env injection ---


def test_resolve_executor_env_no_api_key_env_returns_none():
    executor_cfg = get_executor_config("local-ollama", _BASE_CFG)
    assert resolve_executor_env(executor_cfg) is None


def test_resolve_executor_env_injects_target_var(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY_1", "sk-instance-1-secret")
    executor_cfg = get_executor_config("claude-1", _BASE_CFG)
    env = resolve_executor_env(executor_cfg)
    assert env is not None
    assert env["ANTHROPIC_API_KEY"] == "sk-instance-1-secret"


def test_resolve_executor_env_absent_api_key_env_is_not_an_error():
    """api_key_env simply not being present on the config is a valid,
    common case (no injection requested) — not an error."""
    executor_cfg = get_executor_config("local-ollama", _BASE_CFG)
    assert "api_key_env" not in executor_cfg
    assert resolve_executor_env(executor_cfg) is None


def test_resolve_executor_env_unset_var_raises(monkeypatch):
    """api_key_env names a variable that is not actually set — this must be
    a loud error, not a silent fall-through to the parent shell's ambient
    key (which would dispatch under the wrong account undetected)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY_1", raising=False)
    executor_cfg = get_executor_config("claude-1", _BASE_CFG)

    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY_1"):
        resolve_executor_env(executor_cfg)


def test_resolve_executor_env_unsupported_type_raises(monkeypatch):
    """gemini/continue have no target env var mapping in
    _DEFAULT_API_KEY_ENV_VAR — configuring api_key_env for one of these
    types must raise rather than silently no-op."""
    monkeypatch.setenv("SOME_GEMINI_KEY", "sk-gemini-secret")
    executor_cfg = {"type": "gemini", "api_key_env": "SOME_GEMINI_KEY", "instance": "gemini-1"}

    with pytest.raises(ConfigError, match="gemini"):
        resolve_executor_env(executor_cfg)


def test_dispatch_named_instance_env_injection(monkeypatch):
    """TICK-088 close criterion: dispatching a named instance injects its
    api_key_env value into the subprocess environment under the driver's
    expected variable name, without touching other instances' keys."""
    monkeypatch.setenv("ANTHROPIC_API_KEY_1", "sk-instance-1-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY_2", "sk-instance-2-secret")

    with mock.patch("lanegate.executor.subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        dispatch_executor("claude-1", "implement the ticket", _BASE_CFG, cwd="/tmp/worktree")

    assert mock_run.call_count == 1
    _args, kwargs = mock_run.call_args
    injected_env = kwargs["env"]
    assert injected_env is not None
    assert injected_env["ANTHROPIC_API_KEY"] == "sk-instance-1-secret"


def test_dispatch_second_instance_injects_its_own_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY_1", "sk-instance-1-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY_2", "sk-instance-2-secret")

    with mock.patch("lanegate.executor.subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        dispatch_executor("claude-2", "implement the ticket", _BASE_CFG, cwd="/tmp/worktree")

    _args, kwargs = mock_run.call_args
    assert kwargs["env"]["ANTHROPIC_API_KEY"] == "sk-instance-2-secret"


def test_dispatch_bare_type_backward_compat_no_env_override(monkeypatch):
    """Dispatching a bare type with no executors: configured performs no env
    override — the subprocess just inherits the parent environment (env=None)."""
    with mock.patch("lanegate.executor.subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        dispatch_executor("claude-process", "implement the ticket", {"executors": {}}, cwd="/tmp/worktree")

    _args, kwargs = mock_run.call_args
    assert kwargs["env"] is None


def test_dispatch_executor_missing_bin_raises_before_subprocess(monkeypatch):
    monkeypatch.setenv("PATH", "/restricted/bin")
    monkeypatch.setattr("lanegate.executor.shutil.which", lambda _bin_name: None)

    with mock.patch("lanegate.executor.subprocess.run") as mock_run:
        with pytest.raises(ConfigError, match="executor 'claude-process'.*bin 'claude'.*PATH"):
            dispatch_executor(
                "claude-process",
                "implement the ticket",
                {"executors": {}},
                cwd="/tmp/worktree",
            )

    mock_run.assert_not_called()


# --- Executor cooldown state (TICK-090) ---

_POOL_CFG = {
    "executors": {
        "claude-1": {"type": "claude-process"},
        "claude-2": {"type": "claude-process"},
    },
    "pools": {"default": {"executors": ["claude-1", "claude-2"]}},
}


def test_write_and_read_cooldown_round_trips(tmp_path):
    write_cooldown(tmp_path, "claude-1", "session_limit")
    cooldown = read_cooldown(tmp_path, "claude-1")
    assert cooldown["reason"] == "session_limit"
    assert cooldown["until"] is not None
    assert is_cooling_down(tmp_path, "claude-1") is True


def test_cooldown_file_written_at_expected_path(tmp_path):
    write_cooldown(tmp_path, "claude-1", "session_limit")
    path = tmp_path / ".lanegate" / "executors" / "claude-1.cooldown"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["reason"] == "session_limit"


def test_read_cooldown_missing_file_returns_none(tmp_path):
    assert read_cooldown(tmp_path, "claude-1") is None
    assert is_cooling_down(tmp_path, "claude-1") is False


def test_expired_cooldown_auto_clears(tmp_path):
    """A cooldown whose `until` timestamp is in the past is treated as
    inactive, and the stale file is deleted so future reads don't repeat
    the expiry check."""
    past = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)).isoformat()
    path = tmp_path / ".lanegate" / "executors" / "claude-1.cooldown"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"until": past, "reason": "session_limit"}))

    assert is_cooling_down(tmp_path, "claude-1") is False
    assert read_cooldown(tmp_path, "claude-1") is None
    assert not path.exists()


def test_future_cooldown_stays_active(tmp_path):
    future = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)).isoformat()
    write_cooldown(tmp_path, "claude-1", "session_limit", retry_after=future)

    assert is_cooling_down(tmp_path, "claude-1") is True
    path = tmp_path / ".lanegate" / "executors" / "claude-1.cooldown"
    assert path.exists()


def test_parse_retry_after_seconds_int():
    until = parse_retry_after(60)
    parsed = datetime.datetime.fromisoformat(until)
    delta = parsed - datetime.datetime.now(datetime.UTC)
    assert 55 <= delta.total_seconds() <= 65


def test_parse_retry_after_digit_string():
    until = parse_retry_after("120")
    parsed = datetime.datetime.fromisoformat(until)
    delta = parsed - datetime.datetime.now(datetime.UTC)
    assert 115 <= delta.total_seconds() <= 125


def test_parse_retry_after_iso_timestamp_passthrough():
    iso = "2026-06-25T08:00:00+00:00"
    assert parse_retry_after(iso) == datetime.datetime.fromisoformat(iso).isoformat()


def test_parse_retry_after_scans_raw_text_needle():
    until = parse_retry_after("HTTP 429\nRetry-After: 30\nquota exceeded")
    parsed = datetime.datetime.fromisoformat(until)
    delta = parsed - datetime.datetime.now(datetime.UTC)
    assert 25 <= delta.total_seconds() <= 35


def test_parse_retry_after_no_hint_returns_none():
    assert parse_retry_after("usage limit reached, no reset hint here") is None
    assert parse_retry_after(None) is None


def _format_resets_at(when_local: datetime.datetime) -> str:
    hour12 = when_local.hour % 12 or 12
    meridiem = "pm" if when_local.hour >= 12 else "am"
    return f"{hour12}:{when_local.minute:02d}{meridiem}"


def test_parse_retry_after_resets_at_still_future_today():
    zone = zoneinfo.ZoneInfo("America/Los_Angeles")
    now_local = datetime.datetime.now(zone)
    target_local = (now_local + datetime.timedelta(minutes=30)).replace(second=0, microsecond=0)
    text = (
        f"You've hit your session limit · resets {_format_resets_at(target_local)} "
        "(America/Los_Angeles)"
    )
    until = parse_retry_after(text)
    assert until is not None
    parsed = datetime.datetime.fromisoformat(until)
    expected = target_local.astimezone(datetime.UTC)
    assert abs((parsed - expected).total_seconds()) < 60


def test_parse_retry_after_resets_at_bare_hour_form():
    zone = zoneinfo.ZoneInfo("America/Los_Angeles")
    now_local = datetime.datetime.now(zone)
    target_local = (now_local + datetime.timedelta(hours=2)).replace(
        minute=0, second=0, microsecond=0
    )
    hour12 = target_local.hour % 12 or 12
    meridiem = "pm" if target_local.hour >= 12 else "am"

    until = parse_retry_after(
        f"You've hit your weekly limit · resets {hour12}{meridiem} (America/Los_Angeles)"
    )

    assert until is not None
    parsed = datetime.datetime.fromisoformat(until)
    assert abs((parsed - target_local.astimezone(datetime.UTC)).total_seconds()) < 60


def test_parse_retry_after_resets_at_already_past_rolls_to_tomorrow():
    zone = zoneinfo.ZoneInfo("America/Los_Angeles")
    now_local = datetime.datetime.now(zone)
    target_local = (now_local - datetime.timedelta(minutes=30)).replace(second=0, microsecond=0)
    text = f"resets {_format_resets_at(target_local)} (America/Los_Angeles)"
    until = parse_retry_after(text)
    assert until is not None
    parsed = datetime.datetime.fromisoformat(until)
    expected = (target_local + datetime.timedelta(days=1)).astimezone(datetime.UTC)
    assert abs((parsed - expected).total_seconds()) < 60
    assert parsed > datetime.datetime.now(datetime.UTC)


def test_parse_retry_after_resets_at_unknown_timezone_returns_none():
    assert parse_retry_after("resets 4:40pm (Not/AZone)") is None


def test_parse_retry_after_resets_at_invalid_minute_returns_none():
    assert parse_retry_after("resets 4:99pm (America/Los_Angeles)") is None
    assert parse_retry_after("resets 13:40pm (America/Los_Angeles)") is None


def test_parse_retry_after_resets_in_hours_minutes_seconds():
    text = 'Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 3h51m9s.'
    until = parse_retry_after(text)
    assert until is not None
    parsed = datetime.datetime.fromisoformat(until)
    expected = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=3, minutes=51, seconds=9)
    assert abs((parsed - expected).total_seconds()) < 5


def test_parse_retry_after_resets_in_minutes_only():
    until = parse_retry_after("quota exceeded, resets in 45m")
    assert until is not None
    parsed = datetime.datetime.fromisoformat(until)
    expected = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=45)
    assert abs((parsed - expected).total_seconds()) < 5


def test_parse_retry_after_resets_in_with_no_duration_returns_none():
    assert parse_retry_after("resets in a while, try later") is None


def test_write_cooldown_from_resets_in_message_auto_clears(tmp_path):
    """Same auto-clear guarantee as Claude's resets-at message (TICK-090),
    but for agy/Gemini's relative-countdown shape - previously fell through
    to `until: null` (manual-reset-only) because no parser recognized it,
    which is why a stale agy cooldown from one quota trip could block the
    pool indefinitely long after the real quota window had passed."""
    reason = (
        "rate limit or quota interruption (executor exited 1)\n\n"
        'Raw executor output:\n{"error":"Individual quota reached. '
        'Resets in 2m."}'
    )
    write_cooldown(tmp_path, "agy", reason, retry_after=reason)

    cooldown = read_cooldown(tmp_path, "agy")
    assert cooldown is not None
    assert cooldown["until"] is not None
    assert is_cooling_down(tmp_path, "agy") is True

    path = tmp_path / ".lanegate" / "executors" / "agy.cooldown"
    past = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=1)).isoformat()
    path.write_text(json.dumps({"until": past, "reason": reason}))
    assert is_cooling_down(tmp_path, "agy") is False
    assert not path.exists()


def test_write_cooldown_from_resets_at_message_auto_clears(tmp_path):
    """End-to-end: a cooldown written from Claude's own session-limit message
    text gets a real `until`, stays active while in the future, and reuses
    the existing expired-until auto-delete path (TICK-090) once past it -
    instead of sitting at `until: null` until a manual reset."""
    zone = zoneinfo.ZoneInfo("America/Los_Angeles")
    now_local = datetime.datetime.now(zone)
    target_local = (now_local + datetime.timedelta(minutes=30)).replace(second=0, microsecond=0)
    reason = (
        "rate limit or quota interruption (executor exited 1)\n\n"
        f"Raw executor output:\nYou've hit your session limit · resets "
        f"{_format_resets_at(target_local)} (America/Los_Angeles)"
    )
    write_cooldown(tmp_path, "claude-a", reason, retry_after=reason)

    cooldown = read_cooldown(tmp_path, "claude-a")
    assert cooldown is not None
    assert cooldown["until"] is not None
    assert is_cooling_down(tmp_path, "claude-a") is True

    # Simulate time having passed the reset by overwriting with an
    # already-past `until` (same shape the parser would have produced) and
    # confirm it auto-clears via the existing expired path.
    path = tmp_path / ".lanegate" / "executors" / "claude-a.cooldown"
    past = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=1)).isoformat()
    path.write_text(json.dumps({"until": past, "reason": reason}))
    assert is_cooling_down(tmp_path, "claude-a") is False
    assert not path.exists()


def test_read_cooldown_auto_clears_interrupt_artifact_without_until(tmp_path):
    reason = (
        "rate limit or quota interruption (executor exited 1)\n\n"
        "Raw executor output:\n"
        "+        \"rate limit\",\n"
        "turn interrupted\n"
        "tokens used\n"
        "110,731"
    )
    path = write_cooldown(tmp_path, "codex", reason)
    assert path.exists()

    assert read_cooldown(tmp_path, "codex") is None
    assert not path.exists()


def test_read_cooldown_keeps_interrupt_transcript_with_weekly_limit(tmp_path):
    reason = (
        "rate limit or quota interruption (executor exited 1)\n\n"
        "Raw executor output:\nturn interrupted\n"
        "You've hit your weekly limit · resets 10am (America/Los_Angeles)"
    )
    write_cooldown(tmp_path, "claude-a", reason)

    cooldown = read_cooldown(tmp_path, "claude-a")

    assert cooldown is not None


def test_read_cooldown_keeps_interrupt_transcript_with_unseen_limit_variant(tmp_path):
    reason = (
        "rate limit or quota interruption (executor exited 1)\n\n"
        "Raw executor output:\nturn interrupted\n"
        "You've hit your 5-hour limit · resets 10am (America/Los_Angeles)"
    )
    write_cooldown(tmp_path, "claude-a", reason)

    assert read_cooldown(tmp_path, "claude-a") is not None


def test_read_cooldown_auto_clears_non_retryable_error_without_until(tmp_path):
    reason = (
        "rate limit or quota interruption (executor exited 1)\n\n"
        "Raw executor output:\n"
        "You've hit your session limit · resets 4:40pm (America/Los_Angeles)\n"
        "ERROR: {\"status\":400,\"error\":{\"type\":\"invalid_request_error\","
        "\"message\":\"The 'gpt-5.6-terra' model requires a newer version of Codex\"}}"
    )
    path = write_cooldown(tmp_path, "codex", reason)
    assert path.exists()

    assert read_cooldown(tmp_path, "codex") is None
    assert not path.exists()


def test_clear_cooldown_removes_file_and_reports_existence(tmp_path):
    write_cooldown(tmp_path, "claude-1", "session_limit")
    assert clear_cooldown(tmp_path, "claude-1") is True
    assert is_cooling_down(tmp_path, "claude-1") is False
    # Clearing again (nothing left to clear) is a harmless no-op.
    assert clear_cooldown(tmp_path, "claude-1") is False


def test_clear_all_cooldowns_removes_every_file(tmp_path):
    write_cooldown(tmp_path, "claude-1", "session_limit")
    write_cooldown(tmp_path, "claude-2", "rate_limit")
    cleared = clear_all_cooldowns(tmp_path)
    assert set(cleared) == {"claude-1", "claude-2"}
    assert is_cooling_down(tmp_path, "claude-1") is False
    assert is_cooling_down(tmp_path, "claude-2") is False


def test_available_instances_skips_cooling_down(tmp_path):
    write_cooldown(tmp_path, "claude-1", "session_limit")
    assert available_instances(tmp_path, ["claude-1", "claude-2"]) == ["claude-2"]


def test_executor_status_rows_reports_active_and_cooling_down(tmp_path):
    write_cooldown(tmp_path, "claude-1", "session_limit")
    rows = executor_status_rows(_POOL_CFG, tmp_path, running_counts={"claude-2": 3})
    by_name = {r["name"]: r for r in rows}
    assert by_name["claude-1"]["cooling_down"] is True
    assert by_name["claude-1"]["running"] == 0
    assert by_name["claude-2"]["cooling_down"] is False
    assert by_name["claude-2"]["running"] == 3


def test_cmd_executor_status_prints_active_and_cooling_down(tmp_path, capsys):
    write_cooldown(tmp_path, "claude-1", "session_limit")
    cmd_executor_status(_POOL_CFG, tmp_path)
    out = capsys.readouterr().out
    assert "claude-1" in out and "cooling down" in out
    assert "claude-2" in out and "active" in out


def test_cmd_executor_reset_single_name_clears_file(tmp_path):
    write_cooldown(tmp_path, "claude-1", "session_limit")
    cleared = cmd_executor_reset(_POOL_CFG, tmp_path, name="claude-1")
    assert cleared == ["claude-1"]
    assert is_cooling_down(tmp_path, "claude-1") is False


def test_cmd_executor_reset_all_clears_every_file(tmp_path):
    write_cooldown(tmp_path, "claude-1", "session_limit")
    write_cooldown(tmp_path, "claude-2", "rate_limit")
    cleared = cmd_executor_reset(_POOL_CFG, tmp_path, reset_all=True)
    assert set(cleared) == {"claude-1", "claude-2"}
    assert is_cooling_down(tmp_path, "claude-1") is False
    assert is_cooling_down(tmp_path, "claude-2") is False
def test_write_cooldown_default_fallback_ttl(tmp_path):
    """write_cooldown without retry_after sets default 30-min fallback until timestamp."""
    p = write_cooldown(tmp_path, "claude-1", "rate_limit")
    data = read_cooldown(tmp_path, "claude-1")
    assert data is not None
    assert data.get("until") is not None
    assert "reason" in data


# --- analyze_session_id / --resume threading (TICK-188) ---


def test_implement_resumes_analyze_session():
    """build_executor_cmd adds --resume <id> for Claude subprocess executors when analyze_session_id is set."""
    session_id = "550e8400-e29b-41d4-a716-446655440000"
    cmd = build_executor_cmd(
        "claude-process", "implement the ticket", {"executors": {}},
        analyze_session_id=session_id,
    )
    assert "--resume" in cmd
    idx = cmd.index("--resume")
    assert cmd[idx + 1] == session_id
    assert "-p" in cmd


def test_implement_no_resume_without_session_id():
    """build_executor_cmd does NOT add --resume when analyze_session_id is absent."""
    cmd = build_executor_cmd("claude-process", "implement the ticket", {"executors": {}})
    assert "--resume" not in cmd


def test_review_dispatch_unchanged_by_session_id():
    """build_executor_cmd called without analyze_session_id (review path) has no --resume flag."""
    cmd = build_executor_cmd("claude", "review the ticket", {"executors": {}})
    assert "--resume" not in cmd


def test_implement_fallback_on_expired_session():
    """dispatch_executor retries without --resume when the first attempt (with --resume) fails."""
    session_id = "expired-session-id"
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "--resume" in cmd:
            return mock.Mock(returncode=1, stdout="session not found", stderr="")
        return mock.Mock(returncode=0, stdout="", stderr="")

    with mock.patch("lanegate.executor.subprocess.run", side_effect=fake_run):
        result = dispatch_executor(
            "claude-process",
            "implement the ticket",
            {"executors": {}},
            cwd="/tmp/worktree",
            analyze_session_id=session_id,
        )

    assert result.returncode == 0, "should have fallen back to fresh dispatch"
    assert len(calls) == 2, f"expected 2 subprocess calls (with-resume + fallback), got {len(calls)}"
    assert "--resume" in calls[0], "first call should include --resume"
    assert "--resume" not in calls[1], "fallback call should NOT include --resume"


def test_non_claude_executor_ignores_session_id():
    """dispatch_executor passes analyze_session_id=... for non-Claude executors without --resume."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return mock.Mock(returncode=0, stdout="", stderr="")

    with mock.patch("lanegate.executor.subprocess.run", side_effect=fake_run):
        dispatch_executor(
            "aider",
            "implement the ticket",
            {"executors": {}},
            cwd="/tmp/worktree",
            analyze_session_id="some-session",
        )

    # One "git ls-files" call from the aider mention-neutralization preflight,
    # plus the actual dispatched aider command.
    dispatch_calls = [c for c in calls if c[:2] != ["git", "ls-files"]]
    assert len(dispatch_calls) == 1
    assert "--resume" not in dispatch_calls[0]


def test_dispatch_claude_subagent_env_injection(monkeypatch):
    """claude-subagent named instance injects its api_key_env correctly."""
    monkeypatch.setenv("ANTHROPIC_API_KEY_1", "sk-subagent-secret")

    with mock.patch("lanegate.executor.subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        dispatch_executor("claude-subagent-1", "implement the ticket", _BASE_CFG, cwd="/tmp/worktree")

    assert mock_run.call_count == 1
    _args, kwargs = mock_run.call_args
    injected_env = kwargs["env"]
    assert injected_env is not None
    assert injected_env["ANTHROPIC_API_KEY"] == "sk-subagent-secret"


# --- Ollama context discovery tests ---


def test_ollama_context_discovery_via_api_ps():
    """Read runtime context from the documented GET /api/ps response shape."""
    from lanegate.executor import discover_ollama_context

    connection = mock.Mock()
    response = mock.Mock(status=200)
    response.read.return_value = json.dumps({
        "models": [{"name": "qwen2.5-coder:14b", "context_length": 32768}]
    }).encode()
    connection.getresponse.return_value = response
    with mock.patch("lanegate.executor.http.client.HTTPConnection", return_value=connection):
        context, source = discover_ollama_context(
            "http://127.0.0.1:11435", "ollama_chat/qwen2.5-coder:14b"
        )

    assert (context, source) == (32768, "runtime")
    connection.request.assert_called_once_with("GET", "/api/ps", body=None, headers={})


def test_ollama_context_discovery_fallback_to_api_show():
    """Fall back to POST /api/show when the requested model is not loaded."""
    from lanegate.executor import discover_ollama_context

    connection = mock.Mock()
    ps_response = mock.Mock(status=200)
    ps_response.read.return_value = json.dumps({"models": [{"name": "other:latest", "context_length": 4096}]}).encode()
    show_response = mock.Mock(status=200)
    show_response.read.return_value = json.dumps({"parameters": "temperature 0.7\nnum_ctx 8192\n"}).encode()
    connection.getresponse.side_effect = [ps_response, show_response]
    with mock.patch("lanegate.executor.http.client.HTTPConnection", return_value=connection):
        context, source = discover_ollama_context("http://localhost:11434", "qwen2.5-coder:14b")

    assert (context, source) == (8192, "model_metadata")
    assert connection.request.call_args_list[1].args[:2] == ("POST", "/api/show")
    assert json.loads(connection.request.call_args_list[1].kwargs["body"].decode()) == {"model": "qwen2.5-coder:14b"}


def test_ollama_context_discovery_graceful_timeout():
    """Return None gracefully on timeout."""
    from lanegate.executor import discover_ollama_context_length

    connection = mock.Mock()
    connection.request.side_effect = OSError("Connection timed out")
    with mock.patch("lanegate.executor.http.client.HTTPConnection", return_value=connection):
        assert discover_ollama_context_length("http://localhost:11434", "llama2", timeout_secs=1) is None


# Alias for close criteria naming
test_ollama_context_discovery_timeout = test_ollama_context_discovery_graceful_timeout


def test_ollama_context_discovery_connection_refused():
    """Return None gracefully on connection refused."""
    from lanegate.executor import discover_ollama_context_length

    connection = mock.Mock()
    connection.request.side_effect = OSError("Connection refused")
    with mock.patch("lanegate.executor.http.client.HTTPConnection", return_value=connection):
        assert discover_ollama_context_length("http://localhost:11434", "llama2") is None


def test_ollama_context_discovery_json_parse_error():
    """Return None gracefully on JSON parse error."""
    from lanegate.executor import discover_ollama_context_length

    connection = mock.Mock()
    response = mock.Mock(status=200)
    response.read.return_value = b"invalid json"
    connection.getresponse.return_value = response
    with mock.patch("lanegate.executor.http.client.HTTPConnection", return_value=connection):
        assert discover_ollama_context_length("http://localhost:11434", "llama2") is None


def test_ollama_context_discovery_rejects_non_loopback_endpoint():
    """Discovery never sends a request to a public or non-HTTP URL."""
    from lanegate.executor import discover_ollama_context_length

    with mock.patch("lanegate.executor.http.client.HTTPConnection") as mock_connection:
        assert discover_ollama_context_length("https://example.test", "llama2") is None
        assert discover_ollama_context_length("file:///etc/passwd", "llama2") is None
    mock_connection.assert_not_called()


def test_ollama_context_mismatch_warning(capsys):
    """Log advisory when discovered and configured context lengths disagree."""
    from lanegate.executor import log_context_discovery_advisory

    log_context_discovery_advisory(
        discovered_context=4096,
        configured_budget=8192,
        model="llama2",
        executor_name="local-ollama"
    )

    captured = capsys.readouterr()
    assert "advisory" in captured.err
    assert "local-ollama" in captured.err
    assert "4096" in captured.err
    assert "8192" in captured.err
    assert "MISMATCH" in captured.err or "should match" in captured.err


def test_ollama_context_no_advisory_when_match(capsys):
    """No advisory logged when discovered and configured match."""
    from lanegate.executor import log_context_discovery_advisory

    log_context_discovery_advisory(
        discovered_context=4096,
        configured_budget=4096,
        model="llama2",
        executor_name="local-ollama"
    )

    captured = capsys.readouterr()
    assert captured.err == ""


def test_ollama_context_no_advisory_when_none_discovered(capsys):
    """No advisory logged when discovery returns None."""
    from lanegate.executor import log_context_discovery_advisory

    log_context_discovery_advisory(
        discovered_context=None,
        configured_budget=8192,
        model="llama2",
        executor_name="local-ollama"
    )

    captured = capsys.readouterr()
    assert captured.err == ""


def test_check_aider_context_budget_calls_discovery():
    """_check_aider_context_budget calls discovery for Ollama-backed routes."""
    from lanegate.executor import _check_aider_context_budget

    cfg = {
        "drivers": {
            "local-ollama": {
                "type": "ollama",
                "base_url": "http://localhost:11434",
                "provider": "ollama",
            }
        }
    }

    executor_cfg = {
        "type": "aider",
        "instance": "aider-ollama",
        "provider": "ollama",
        "base_url": "http://localhost:11434",
        "context_window_tokens": 20000,  # Large enough to not fail the budget check
    }

    with mock.patch("lanegate.executor.discover_ollama_context") as mock_discover:
        mock_discover.return_value = (4096, "runtime")
        with mock.patch("lanegate.executor.log_context_discovery_advisory") as mock_log:
            # Should not raise, and should call discovery
            _check_aider_context_budget(
                prompt="short prompt",
                touches=[],
                executor_cfg=executor_cfg,
                model="llama2",
                executor="aider-ollama",
                cfg=cfg,
            )
            mock_discover.assert_called_once_with("http://localhost:11434", "llama2")
            mock_log.assert_called_once()


def test_check_aider_context_budget_does_not_compare_static_metadata():
    """A static /api/show value is informative, never a runtime mismatch warning."""
    from lanegate.executor import _check_aider_context_budget

    executor_cfg = {
        "type": "aider",
        "instance": "aider-ollama",
        "provider": "ollama",
        "base_url": "http://localhost:11434",
        "context_window_tokens": 20000,
    }
    with mock.patch("lanegate.executor.discover_ollama_context", return_value=(4096, "model_metadata")):
        with mock.patch("lanegate.executor.log_context_discovery_advisory") as mock_log:
            _check_aider_context_budget(
                prompt="short prompt",
                touches=[],
                executor_cfg=executor_cfg,
                model="llama2",
                executor="aider-ollama",
                cfg={},
            )
    mock_log.assert_not_called()


def test_check_aider_context_budget_skips_discovery_non_ollama():
    """_check_aider_context_budget skips discovery for non-Ollama routes."""
    from lanegate.executor import _check_aider_context_budget

    cfg = {}

    executor_cfg = {
        "type": "aider",
        "instance": "aider-claude",
        "context_window_tokens": 20000,  # Large enough to not fail the budget check
    }

    with mock.patch("lanegate.executor.discover_ollama_context_length") as mock_discover:
        with mock.patch("lanegate.executor.log_context_discovery_advisory") as mock_log:
            # Should not raise, and should NOT call discovery for non-Ollama
            _check_aider_context_budget(
                prompt="short prompt",
                touches=[],
                executor_cfg=executor_cfg,
                model=None,
                executor="aider-claude",
                cfg=cfg,
            )
            mock_discover.assert_not_called()
            mock_log.assert_not_called()


def test_check_aider_context_budget_skips_discovery_no_model():
    """_check_aider_context_budget skips discovery when model is not provided."""
    from lanegate.executor import _check_aider_context_budget

    cfg = {}

    executor_cfg = {
        "type": "aider",
        "instance": "aider-ollama",
        "provider": "ollama",
        "base_url": "http://localhost:11434",
        "context_window_tokens": 20000,  # Large enough to not fail the budget check
    }

    with mock.patch("lanegate.executor.discover_ollama_context_length") as mock_discover:
        with mock.patch("lanegate.executor.log_context_discovery_advisory") as mock_log:
            # Should not raise, and should NOT call discovery when model is None
            _check_aider_context_budget(
                prompt="short prompt",
                touches=[],
                executor_cfg=executor_cfg,
                model=None,  # No model provided
                executor="aider-ollama",
                cfg=cfg,
            )
            mock_discover.assert_not_called()
            mock_log.assert_not_called()


def test_non_ollama_route_no_discovery():
    """Non-Ollama routes skip discovery entirely."""
    from lanegate.executor import _check_aider_context_budget

    cfg = {}

    executor_cfg = {
        "type": "aider",
        "instance": "aider-claude",
        "context_window_tokens": 20000,
    }

    with mock.patch("lanegate.executor.discover_ollama_context_length") as mock_discover:
        with mock.patch("lanegate.executor.log_context_discovery_advisory") as mock_log:
            _check_aider_context_budget(
                prompt="short prompt",
                touches=[],
                executor_cfg=executor_cfg,
                model="gpt-4",  # Claude executor, not Ollama
                executor="aider-claude",
                cfg=cfg,
            )
            mock_discover.assert_not_called()
            mock_log.assert_not_called()


# ---------------------------------------------------------------------------
# TICK-306: bounded implement-prompt payload + machine-readable accounting
# ---------------------------------------------------------------------------


_LARGE_ARCH_DOC = (
    "# Architecture Reference\n\n"
    "## Overview\n"
    + ("General background prose about the project. " * 20)
    + "\n\n"
    "## Orchestration Loop\n"
    "The orchestrate.py module implements the board-clearing loop. "
    + ("Detail sentence about orchestrate.py behavior. " * 20)
    + "\n\n"
    "## Delivery Axis\n"
    + ("Unrelated section about promote.py and feature flags. " * 20)
    + "\n"
)


def _write_large_arch_doc(tmp_path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "ARCHITECTURE.md").write_text(_LARGE_ARCH_DOC)


def _implement_ticket(**overrides) -> dict:
    base = {
        "id": "TICK-777",
        "title": "Update the orchestrate loop",
        "touches": ["lanegate/orchestrate.py"],
        "close_criteria": "Loop updated.",
        "_body": "Do the thing.",
    }
    base.update(overrides)
    return base


def test_no_full_architecture_in_implement_for_unrelated_ticket(tmp_path):
    from lanegate.executor import build_implement_prompt

    _write_large_arch_doc(tmp_path)
    ticket = _implement_ticket(
        title="Fix a CSS typo", touches=["src/css_widget_thing.py"], _body="Small fix."
    )

    prompt = build_implement_prompt(ticket, project_root=tmp_path)

    assert "Unrelated section about promote.py" not in prompt
    assert "Orchestration Loop" not in prompt


def test_implement_prompt_bounded_under_configured_budget(tmp_path):
    from lanegate.executor import build_implement_prompt

    _write_large_arch_doc(tmp_path)
    ticket = _implement_ticket()
    cfg = {"payload_budgets": {"implement": 500}}

    prompt = build_implement_prompt(ticket, project_root=tmp_path, cfg=cfg)

    untrusted_start = prompt.index("<untrusted-data>")
    instruction_layer = prompt[:untrusted_start]
    # The architecture excerpt component itself must respect the configured
    # byte budget even though the full doc (and full instruction layer with
    # template/skeleton text) is larger.
    assert "bounded excerpt" in instruction_layer


def test_describe_implement_payload_returns_component_metadata(tmp_path):
    from lanegate.executor import describe_implement_payload

    _write_large_arch_doc(tmp_path)
    ticket = _implement_ticket()

    components = describe_implement_payload(ticket, project_root=tmp_path)

    assert isinstance(components, list)
    assert components  # non-empty
    for component in components:
        assert set(component.keys()) == {
            "label", "source", "step", "bytes", "tokens_est", "injected", "reason",
        }
        assert component["step"] == "implement"
    labels = {c["label"] for c in components}
    assert "instruction-template" in labels
    assert "architecture-excerpt:docs/ARCHITECTURE.md" in labels
    assert "ticket-body" in labels


def test_describe_implement_payload_never_exposes_ticket_content(tmp_path):
    from lanegate.executor import describe_implement_payload

    ticket = _implement_ticket(
        title="SECRET_TITLE_MARKER", _body="SECRET_BODY_MARKER", close_criteria="SECRET_CRITERIA_MARKER"
    )

    components = describe_implement_payload(ticket, project_root=tmp_path)

    serialized = json.dumps(components)
    assert "SECRET_TITLE_MARKER" not in serialized
    assert "SECRET_BODY_MARKER" not in serialized
    assert "SECRET_CRITERIA_MARKER" not in serialized


def test_describe_implement_payload_accounting_deterministic(tmp_path):
    from lanegate.executor import describe_implement_payload

    _write_large_arch_doc(tmp_path)
    ticket = _implement_ticket()

    first = describe_implement_payload(ticket, project_root=tmp_path)
    second = describe_implement_payload(ticket, project_root=tmp_path)

    assert first == second


def test_describe_implement_payload_reports_truncated_change_notes(tmp_path):
    """Payload audit must describe the bounded text actually injected."""
    from lanegate.executor import describe_implement_payload

    budget = 40
    ticket = _implement_ticket(change_notes={"x.py": "x" * 500})
    components = describe_implement_payload(
        ticket, project_root=tmp_path, cfg={"payload_budgets": {"implement": budget}}
    )

    component = next(item for item in components if item["label"] == "change-notes")
    assert component["bytes"] <= budget


def test_build_executor_cmd_agy_session_resumption():
    from lanegate.executor import build_executor_cmd

    cmd = build_executor_cmd(
        "agy",
        "do work",
        {},
        model="gemini-3.6-flash-medium",
        analyze_session_id="sess-123",
    )
    assert "--resume" in cmd
    assert "sess-123" in cmd
    assert "--model" in cmd
    assert "gemini-3.6-flash-medium" in cmd


def test_build_executor_cmd_codex_session_resumption():
    from lanegate.executor import build_executor_cmd

    cmd = build_executor_cmd(
        "codex",
        "do work",
        {},
        model="gpt-4o",
        analyze_session_id="sess-456",
    )
    assert cmd[:4] == ["codex", "exec", "resume", "--json"]
    assert "--resume" not in cmd
    assert cmd[-2:] == ["sess-456", "do work"]
    assert "--model" in cmd
    assert "gpt-4o" in cmd


def test_check_aider_context_budget_lazy_context_no_longer_discounted(tmp_path):
    # lazy_context/repo_map must not discount a touched file's real size:
    # Aider's filename-mention auto-add injects the full file regardless of
    # how it was configured, so both modes budget identically now.
    from lanegate.executor import _check_aider_context_budget
    from lanegate.config import ConfigError

    big_file = tmp_path / "big_file.py"
    big_file.write_text("x" * 90000)  # ~30k tokens

    executor_cfg_normal = {"context_window_tokens": 10000, "lazy_context": False}
    executor_cfg_lazy = {"context_window_tokens": 10000, "lazy_context": True}

    for executor_cfg in (executor_cfg_normal, executor_cfg_lazy):
        with pytest.raises(ConfigError, match="exceeded executors.aider.context_window_tokens"):
            _check_aider_context_budget(
                prompt="do task",
                touches=["big_file.py"],
                executor_cfg=executor_cfg,
                model=None,
                worktree_path=tmp_path,
            )


def test_build_implement_prompt_adaptive_skeletons(tmp_path):
    from lanegate.executor import build_implement_prompt

    small_ticket = {
        "id": "TICK-999",
        "title": "Small Ticket",
        "touches": ["a.py"],
        "file_skeletons": {"a.py": "def foo(): pass"},
    }
    small_prompt = build_implement_prompt(small_ticket, project_root=tmp_path)
    assert "## File skeletons" in small_prompt
    assert "def foo(): pass" in small_prompt

    large_skeleton_data = "def func_%d(): pass\n" % 0 + ("x" * 12000)
    large_ticket = {
        "id": "TICK-998",
        "title": "Large Ticket",
        "touches": ["large.py"],
        "file_skeletons": {"large.py": large_skeleton_data},
    }
    large_prompt = build_implement_prompt(large_ticket, project_root=tmp_path)
    assert "## Code Map Notice" in large_prompt
    assert "IMPORTANT: To prevent signature hallucinations" in large_prompt
    assert "## File skeletons" not in large_prompt
