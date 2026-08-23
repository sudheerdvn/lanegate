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
    clear_failure_streak,
    cmd_executor_reset,
    cmd_executor_status,
    dispatch_executor,
    executor_status_rows,
    get_executor_config,
    is_cooling_down,
    _parse_reset_time,
    parse_codex_json_result,
    parse_retry_after,
    read_cooldown,
    record_failure_signature,
    reject_ollama_for_code_step,
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


def test_build_executor_cmd_ollama_flags_land_after_run():
    """ollama's `run` subcommand flags (e.g. --think=false) are only parsed
    correctly after `run <model>` -- before it, ollama misparses the model
    name and tries a registry pull instead."""
    cfg = {"executors": {"ollama": {"flags": ["--nowordwrap", "--think=false"]}}}
    cmd = build_executor_cmd("ollama", "prompt", cfg, model="qwen3:27b", use_stdin=True)
    assert cmd == ["ollama", "run", "qwen3:27b", "--nowordwrap", "--think=false"]


def test_build_executor_cmd_agy_session_resume_uses_conversation_flag():
    cmd = build_executor_cmd(
        "agy",
        "implement changes",
        {},
        analyze_session_id="conv-12345",
    )
    assert "--conversation" in cmd
    conv_idx = cmd.index("--conversation")
    assert cmd[conv_idx + 1] == "conv-12345"
    assert "--resume" not in cmd
    assert "--output-format" in cmd
    assert "--print" in cmd
    assert cmd[-1] == "implement changes"


def test_build_implement_prompt_states_working_directory(tmp_path):
    from lanegate.executor import build_implement_prompt

    ticket = {"id": "TICK-999", "title": "T", "touches": [], "close_criteria": "ok", "_body": ""}
    prompt = build_implement_prompt(ticket, project_root=tmp_path)
    # Agents that browse for their own cwd instead of reading it here waste
    # real turns re-discovering it (observed live: TICK-410's agy dispatch
    # spent several calls locating the worktree before this line existed).
    assert str(tmp_path) in prompt


def test_build_implement_prompt_includes_global_and_per_file_notes(tmp_path):
    from lanegate.executor import build_implement_prompt

    notes = tmp_path / ".lanegate" / "notes"
    notes.mkdir(parents=True)
    (notes / "global.md").write_text("project-wide constraint")
    (notes / "src_module.py.md").write_text("module-specific constraint")
    ticket = {"id": "TICK-999", "title": "T", "touches": ["src/module.py"], "close_criteria": "ok", "_body": ""}

    prompt = build_implement_prompt(ticket, tmp_path)

    assert "project-wide constraint" in prompt
    assert "module-specific constraint" in prompt


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


def test_build_implement_prompt_surfaces_overlapping_cross_ticket_change_notes(tmp_path):
    """A prior *merged* ticket's change_notes for a file the new ticket also
    touches should be folded into the implement prompt (TICK-481: git-tracked
    change_notes replaces the dead worktree-vs-repo_root per-file
    .lanegate/notes/ mechanism)."""
    from lanegate.executor import build_implement_prompt

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    (tickets_dir / "TICK-100.md").write_text(
        "---\nid: TICK-100\ntitle: Prior work\nstatus: merged\n"
        "touches:\n  - foo.py\n---\n"
        "## Change Notes\n**foo.py**: retries silently on timeout, a hang here looks like success\n"
    )

    cfg = {"ticket_prefix": "TICK", "tickets_dir": "tickets"}
    ticket = {
        "id": "TICK-200", "title": "New work", "touches": ["foo.py"],
        "close_criteria": "ok", "_body": "",
    }
    prompt = build_implement_prompt(ticket, project_root=tmp_path, cfg=cfg)

    assert "Prior Change Notes" in prompt
    assert "TICK-100" in prompt
    assert "retries silently on timeout" in prompt


def _init_git_repo_with_commit(root, filename="x.py", content="x = 1\n"):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    (root / filename).write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_implement_prompt_trust_plan_when_no_drift(tmp_path):
    from lanegate.executor import build_implement_prompt

    sha = _init_git_repo_with_commit(tmp_path)
    ticket = {
        "id": "TICK-999", "title": "T", "touches": ["x.py"], "close_criteria": "ok",
        "analyzed_at_sha": sha, "_body": "",
    }
    prompt = build_implement_prompt(ticket, project_root=tmp_path)
    assert "Trust the planned changes" in prompt
    assert "verify exact signatures" not in prompt


def test_implement_prompt_verify_files_on_drift(tmp_path):
    from lanegate.executor import build_implement_prompt

    sha = _init_git_repo_with_commit(tmp_path)
    (tmp_path / "x.py").write_text("x = 2\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "drift"], cwd=tmp_path, check=True)
    ticket = {
        "id": "TICK-999", "title": "T", "touches": ["x.py"], "close_criteria": "ok",
        "analyzed_at_sha": sha, "_body": "",
    }
    prompt = build_implement_prompt(ticket, project_root=tmp_path)
    assert "`x.py`" in prompt
    assert "Commits have touched" in prompt
    assert "Trust the planned changes" not in prompt


def test_build_executor_cmd_disallowed_tools_adds_flag():
    cmd = build_executor_cmd(
        "claude", "do the thing", {"executors": {}}, disallowed_tools=["Bash", "Write", "Edit"]
    )
    assert "--disallowedTools" in cmd
    assert cmd[cmd.index("--disallowedTools") + 1] == "Bash,Write,Edit"


def test_build_executor_cmd_no_disallowed_tools_omits_flag():
    cmd = build_executor_cmd("claude", "do the thing", {"executors": {}})
    assert "--disallowedTools" not in cmd


def test_build_executor_cmd_disallowed_tools_ignored_for_non_claude():
    """The flag is Claude-CLI-specific; other executor types must not see it."""
    cmd = build_executor_cmd("ollama", "prompt", {}, disallowed_tools=["Bash"])
    assert "--disallowedTools" not in cmd


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


def test_build_executor_cmd_agy_print_timeout(monkeypatch):
    # agy's own client-side --print-timeout defaults to a hard 5 minutes,
    # which kills long implement sessions before lanegate's own outer
    # timeout ever triggers (TICK-457). lanegate must always pass an
    # explicit override rather than relying on agy's CLI default.
    monkeypatch.setattr(
        "lanegate.executor.shutil.which",
        lambda bin_name: "/usr/local/bin/agy" if bin_name == "agy" else None,
    )
    cmd = build_executor_cmd("agy", "do the thing", {"executors": {}}, step="implement")
    assert "--print-timeout" in cmd
    # Must come before --print, since --print swallows the next token as the
    # prompt and nothing may follow it in argv.
    assert cmd.index("--print-timeout") < cmd.index("--print")
    timeout_value = cmd[cmd.index("--print-timeout") + 1]
    # agy's --print-timeout is a Go time.Duration string and requires a unit
    # suffix -- a bare integer errors with "missing unit in duration".
    assert timeout_value.endswith("s")
    assert timeout_value[:-1].isdigit()
    assert int(timeout_value[:-1]) > 300  # strictly beyond agy's 5-minute default

    # A project-configured override takes precedence over the built-in default.
    cfg = {"executors": {}, "print_timeout_seconds": {"implement": 120}}
    configured_cmd = build_executor_cmd("agy", "do the thing", cfg, step="implement")
    assert configured_cmd[configured_cmd.index("--print-timeout") + 1] == "120s"


def test_aider_missing_no_gitignore_warns_on_real_dispatch(tmp_path, monkeypatch, capsys):
    """Aider silently modifies .gitignore unless told not to, which
    LaneGate's own scope-drift check then flags as an unexpected committed
    file -- a hand-written config missing --no-gitignore should be warned,
    not silently left to hit that pause later."""
    monkeypatch.chdir(tmp_path)
    cfg = {"executors": {"aider": {"flags": ["--yes-always"]}}}

    build_executor_cmd("aider", "add a helper", cfg, read_only=False)

    assert "--no-gitignore" in capsys.readouterr().err


def test_aider_with_no_gitignore_flag_does_not_warn(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cfg = {"executors": {"aider": {"flags": ["--yes-always", "--no-gitignore"]}}}

    build_executor_cmd("aider", "add a helper", cfg, read_only=False)

    assert "--no-gitignore" not in capsys.readouterr().err


def test_aider_missing_no_gitignore_does_not_warn_on_read_only_dispatch(tmp_path, monkeypatch, capsys):
    """analyze runs aider with --dry-run -- no commit ever happens, so the
    .gitignore side effect this warning exists for cannot occur."""
    monkeypatch.chdir(tmp_path)
    cfg = {"executors": {"aider": {"flags": ["--yes-always"]}}}

    build_executor_cmd("aider", "analyze this", cfg, read_only=True)

    assert "--no-gitignore" not in capsys.readouterr().err


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


def test_build_executor_cmd_aider_context_tiers_selects_tier_1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "small.py").write_text("x" * 300)
    cfg = {
        "executors": {
            "aider": {
                "context_tiers": [
                    {"tokens": 10_000, "model": "ollama/qwen2.5-coder:7b"},
                    {"tokens": 40_000, "model": "ollama/qwen2.5-coder:32b"},
                ]
            }
        }
    }
    cmd = build_executor_cmd(
        "aider", "small prompt", cfg, touches=["small.py"], worktree_path=tmp_path
    )
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "ollama/qwen2.5-coder:7b"


def test_build_executor_cmd_aider_context_tiers_escalates_to_tier_2(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "large.py").write_text("x" * 60_000)
    cfg = {
        "executors": {
            "aider": {
                "context_tiers": [
                    {"tokens": 10_000, "model": "ollama/qwen2.5-coder:7b"},
                    {"tokens": 40_000, "model": "ollama/qwen2.5-coder:32b"},
                ]
            }
        }
    }
    cmd = build_executor_cmd(
        "aider", "large prompt", cfg, touches=["large.py"], worktree_path=tmp_path
    )
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "ollama/qwen2.5-coder:32b"


def test_build_executor_cmd_aider_context_tiers_unsorted_selects_smallest_fitting_tier(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "small.py").write_text("x" * 300)
    cfg = {
        "executors": {
            "aider": {
                "context_tiers": [
                    {"tokens": 40_000, "model": "ollama/qwen2.5-coder:32b"},
                    {"tokens": 10_000, "model": "ollama/qwen2.5-coder:7b"},
                ]
            }
        }
    }
    cmd = build_executor_cmd(
        "aider", "small prompt", cfg, touches=["small.py"], worktree_path=tmp_path
    )
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "ollama/qwen2.5-coder:7b"


def test_build_executor_cmd_aider_context_tiers_exceeds_max_tier_raises_config_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "huge.py").write_text("x" * 150_000)
    cfg = {
        "executors": {
            "aider": {
                "context_tiers": [
                    {"tokens": 10_000, "model": "ollama/qwen2.5-coder:7b"},
                    {"tokens": 40_000, "model": "ollama/qwen2.5-coder:32b"},
                ]
            }
        }
    }
    with pytest.raises(ConfigError, match="exceeds all configured context_tiers") as exc_info:
        build_executor_cmd(
            "aider", "huge prompt", cfg, touches=["huge.py"], worktree_path=tmp_path
        )

    msg = str(exc_info.value)
    assert "max available tier: 40000 tokens" in msg
    assert "Route to a larger executor" in msg


def test_build_executor_cmd_aider_unconfigured_context_tiers_preserves_standard_behavior(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "file.py").write_text("x" * 300)
    cfg = {"executors": {"aider": {"context_window_tokens": 15_000}}}
    cmd = build_executor_cmd(
        "aider", "test prompt", cfg, model="default-model", touches=["file.py"], worktree_path=tmp_path
    )
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "default-model"


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


@pytest.mark.parametrize(
    "executor_type,flag,flag_value",
    [
        ("aider", "--dry-run", None),
        ("codex", "--sandbox", "read-only"),
        ("agy", "--mode", "plan"),
    ],
)
def test_build_executor_cmd_readonly_injects_flag(tmp_path, monkeypatch, executor_type, flag, flag_value):
    monkeypatch.chdir(tmp_path)

    cmd = build_executor_cmd(executor_type, "analyze this", {}, read_only=True)

    assert flag in cmd
    if flag_value is not None:
        assert cmd[cmd.index(flag) + 1] == flag_value


def test_build_executor_cmd_aider_readonly_forces_ask_edit_format(tmp_path, monkeypatch):
    """A read-only aider call (analyze) must use the 'ask' coder, which never
    tries to produce a diff/whole-file edit, regardless of any configured
    edit_format (--chat-mode is just an alias for --edit-format, not a
    distinct safety mode; --dry-run alone doesn't stop aider from framing
    the request as a code edit)."""
    monkeypatch.chdir(tmp_path)
    cfg = {"executors": {"aider": {"edit_format": "whole"}}}

    cmd = build_executor_cmd("aider", "analyze this", cfg, read_only=True)

    assert cmd[cmd.index("--edit-format") + 1] == "ask"


@pytest.mark.parametrize(
    "executor_type,flag",
    [
        ("aider", "--dry-run"),
        ("codex", "--sandbox"),
        ("agy", "--mode"),
    ],
)
def test_build_executor_cmd_not_readonly_omits_flag(tmp_path, monkeypatch, executor_type, flag):
    monkeypatch.chdir(tmp_path)

    cmd = build_executor_cmd(executor_type, "implement this", {}, read_only=False)

    assert flag not in cmd


class TestRejectOllamaForCodeStep:
    @pytest.mark.parametrize("step", ["implement", "review", "fix", "drift_check"])
    def test_raises_for_ollama_code_steps(self, step):
        with pytest.raises(ConfigError, match="ollama"):
            reject_ollama_for_code_step(step, "ollama")

    def test_no_raise_for_ollama_analyze(self):
        reject_ollama_for_code_step("analyze", "ollama")

    @pytest.mark.parametrize("executor_type", ["claude", "aider", "codex"])
    @pytest.mark.parametrize("step", ["implement", "review", "fix", "drift_check", "analyze"])
    def test_no_raise_for_non_ollama_executors(self, executor_type, step):
        reject_ollama_for_code_step(step, executor_type)


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


def test_build_executor_cmd_aider_neutralize_touches_omits_positional_args_only(tmp_path):
    _init_git_repo_with_files(tmp_path, ["small.py", "other.py"])
    cfg = {"executors": {"aider": {"neutralize_touches": True}}}

    cmd = build_executor_cmd(
        "aider",
        "Edit small.py and other.py together.",
        cfg,
        touches=["small.py", "other.py"],
        worktree_path=tmp_path,
    )

    # neutralize_touches only defers the eager positional preload; touches
    # must stay unmangled in the prompt so Aider's own filename-mention
    # auto-add can still find and load them by their real names.
    message = cmd[cmd.index("--message") + 1]
    assert "small.py" in message
    assert "other.py" in message
    assert "​" not in message
    assert "small.py" not in cmd[cmd.index("--message") + 2:]
    assert "other.py" not in cmd[cmd.index("--message") + 2:]


def test_check_aider_context_budget_neutralize_touches_still_counts_touch_sizes(tmp_path):
    # A 100,000-byte touch (~33,000 tokens) must still blow a 10,000 token
    # budget even with neutralize_touches=True: Aider's auto-add will pull
    # the file in during the run regardless of the deferred positional load,
    # so skipping it from the preflight estimate would let Aider crash
    # mid-run instead of failing fast before launch.
    _init_git_repo_with_files(tmp_path, ["huge.py"])
    (tmp_path / "huge.py").write_text("x = 1\n" * 20_000)

    cfg = {"executors": {"aider": {"context_window_tokens": 10_000, "neutralize_touches": True}}}

    with pytest.raises(ConfigError, match="context preflight exceeded"):
        build_executor_cmd(
            "aider",
            "Refactor huge.py.",
            cfg,
            touches=["huge.py"],
            worktree_path=tmp_path,
        )


def test_aider_ollama_unconfigured_warning(capsys):
    cfg = {"executors": {"aider": {"provider": "ollama", "flags": ["--no-gitignore"]}}}

    build_executor_cmd("aider", "implement TICK-299", cfg, model="ollama_chat/qwen2.5-coder:14b")

    warning = capsys.readouterr().err
    assert warning.count("warning:") == 1
    assert "aider executor 'aider'" in warning
    assert "provider 'ollama'" in warning
    assert "docs/executor-capabilities.md#context-window-tokens" in warning


def test_aider_ollama_unconfigured_warning_named_driver(capsys):
    cfg = {
        "drivers": {"local-aider": {"type": "aider", "provider": "ollama"}},
        "executors": {"local-aider": {"type": "aider", "flags": ["--no-gitignore"]}},
    }

    build_executor_cmd(
        "local-aider", "implement TICK-299", cfg, model="ollama_chat/qwen2.5-coder:14b"
    )

    warning = capsys.readouterr().err
    assert warning.count("warning:") == 1
    assert "aider executor 'local-aider'" in warning
    assert "provider 'ollama'" in warning


def test_aider_ollama_configured_no_warning(capsys):
    cfg = {
        "executors": {
            "aider": {
                "provider": "ollama",
                "context_window_tokens": 10_000,
                "flags": ["--no-gitignore"],
            }
        }
    }

    build_executor_cmd("aider", "implement TICK-299", cfg, model="ollama_chat/qwen2.5-coder:14b")

    assert capsys.readouterr().err == ""


def test_aider_ollama_no_model_fails_closed():
    # Regression: with no model resolved, aider's own fallback is an
    # interactive OpenRouter browser-auth flow, not the local Ollama
    # instance provider: ollama declares. That flow can't complete in a
    # non-interactive dispatch -- it hangs for several minutes and then
    # fails, indistinguishable from a genuine step failure to the caller.
    # This must fail immediately and closed instead.
    cfg = {"executors": {"aider": {"provider": "ollama", "flags": ["--no-gitignore"]}}}

    with pytest.raises(ConfigError, match="no model resolved for this step"):
        build_executor_cmd("aider", "implement TICK-299", cfg)


def test_aider_ollama_no_model_fails_closed_named_driver():
    cfg = {
        "drivers": {"local-aider": {"type": "aider", "provider": "ollama"}},
        "executors": {"local-aider": {"type": "aider", "flags": ["--no-gitignore"]}},
    }

    with pytest.raises(ConfigError, match="no model resolved for this step"):
        build_executor_cmd("local-aider", "implement TICK-299", cfg)


def test_aider_no_provider_declared_no_warning(capsys):
    build_executor_cmd(
        "aider", "implement TICK-299", {"executors": {"aider": {"flags": ["--no-gitignore"]}}}
    )

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


def test_parse_retry_after_weekly_dated_resets():
    fixed_now = datetime.datetime(2026, 8, 4, 20, 0, 0, tzinfo=datetime.UTC)
    with mock.patch("lanegate.executor._utc_now", return_value=fixed_now):
        until = parse_retry_after("You've hit your weekly limit - resets Aug 7, 6am (America/Los_Angeles)")
        assert until is not None
        assert until == "2026-08-07T13:00:00+00:00"

        until = parse_retry_after("resets 7 Aug, 6am (America/Los_Angeles)")
        assert until is not None
        assert until == "2026-08-07T13:00:00+00:00"

        until = parse_retry_after("You've hit your Opus weekly limit - resets Aug 7, 6am")
        assert until is not None
        assert until == "2026-08-07T06:00:00+00:00"

        until = parse_retry_after("resets 7 Aug, 6am")
        assert until is not None
        assert until == "2026-08-07T06:00:00+00:00"


def test_parse_retry_after_explicit_date_past_year_rollover():
    fixed_now = datetime.datetime(2026, 8, 8, 20, 0, 0, tzinfo=datetime.UTC)
    with mock.patch("lanegate.executor._utc_now", return_value=fixed_now):
        until = parse_retry_after("resets Aug 7, 6am (America/Los_Angeles)")
        assert until is not None
        assert until == "2027-08-07T13:00:00+00:00"

    # A persisted hint is stale rather than a new weekly-limit report: keep
    # its original year so the resume watcher retries instead of sleeping a year.
    assert _parse_reset_time(
        "resets Aug 7, 6am (America/Los_Angeles)",
        now=fixed_now,
        allow_rollover=False,
    ) == datetime.datetime(2026, 8, 7, 13, 0, 0, tzinfo=datetime.UTC)


def test_parse_retry_after_session_and_legacy_phrasings():
    fixed_now = datetime.datetime(2026, 8, 4, 20, 0, 0, tzinfo=datetime.UTC)
    with mock.patch("lanegate.executor._utc_now", return_value=fixed_now):
        until = parse_retry_after("You've hit your session limit · resets 4:40pm (America/Los_Angeles)")
        assert until is not None
        assert until == "2026-08-04T23:40:00+00:00"

        until = parse_retry_after("usage limit reached, resets 9:00pm")
        assert until is not None
        assert until == "2026-08-04T21:00:00+00:00"


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


class TestFailureSignatureTracking:
    def test_reaches_threshold_on_matching_signature(self, tmp_path):
        assert record_failure_signature(tmp_path, "agy-1", "sig-a", window_s=900, threshold=3) is False
        assert record_failure_signature(tmp_path, "agy-1", "sig-a", window_s=900, threshold=3) is False
        assert record_failure_signature(tmp_path, "agy-1", "sig-a", window_s=900, threshold=3) is True

    def test_different_signature_resets_streak(self, tmp_path):
        record_failure_signature(tmp_path, "agy-1", "sig-a", window_s=900, threshold=3)
        record_failure_signature(tmp_path, "agy-1", "sig-a", window_s=900, threshold=3)
        assert record_failure_signature(tmp_path, "agy-1", "sig-b", window_s=900, threshold=3) is False

    def test_window_expiry_resets_streak(self, tmp_path):
        record_failure_signature(tmp_path, "agy-1", "sig-a", window_s=0, threshold=2)
        assert record_failure_signature(tmp_path, "agy-1", "sig-a", window_s=0, threshold=2) is False

    def test_clear_failure_streak_resets_count(self, tmp_path):
        record_failure_signature(tmp_path, "agy-1", "sig-a", window_s=900, threshold=1)
        clear_failure_streak(tmp_path, "agy-1")
        assert record_failure_signature(tmp_path, "agy-1", "sig-a", window_s=900, threshold=1) is True


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


# --- discover_ollama_models tests ---
# Every other reference to this function across the test suite mocks it away
# entirely; these exercise its own GET /api/tags response parsing directly
# (name-vs-model fallback precedence, non-dict entries, a non-list `models`
# value) so a parsing bug doesn't ship silently -- the init wizard's picker
# swallows any exception and falls back to [], indistinguishable from
# "Ollama not running" in the UI.


def test_discover_ollama_models_parses_name_field():
    from lanegate.executor import discover_ollama_models

    connection = mock.Mock()
    response = mock.Mock(status=200)
    response.read.return_value = json.dumps({
        "models": [{"name": "qwen2.5-coder:14b"}, {"name": "qwen3-coder:30b"}]
    }).encode()
    connection.getresponse.return_value = response
    with mock.patch("lanegate.executor.http.client.HTTPConnection", return_value=connection):
        names = discover_ollama_models("http://localhost:11434")

    assert names == ["qwen2.5-coder:14b", "qwen3-coder:30b"]


def test_discover_ollama_models_falls_back_to_model_field():
    """Some Ollama-compatible servers report `model` instead of `name`."""
    from lanegate.executor import discover_ollama_models

    connection = mock.Mock()
    response = mock.Mock(status=200)
    response.read.return_value = json.dumps({
        "models": [{"model": "qwen2.5-coder:14b"}]
    }).encode()
    connection.getresponse.return_value = response
    with mock.patch("lanegate.executor.http.client.HTTPConnection", return_value=connection):
        names = discover_ollama_models("http://localhost:11434")

    assert names == ["qwen2.5-coder:14b"]


def test_discover_ollama_models_prefers_name_over_model():
    from lanegate.executor import discover_ollama_models

    connection = mock.Mock()
    response = mock.Mock(status=200)
    response.read.return_value = json.dumps({
        "models": [{"name": "from-name", "model": "from-model"}]
    }).encode()
    connection.getresponse.return_value = response
    with mock.patch("lanegate.executor.http.client.HTTPConnection", return_value=connection):
        names = discover_ollama_models("http://localhost:11434")

    assert names == ["from-name"]


def test_discover_ollama_models_skips_non_dict_entries():
    from lanegate.executor import discover_ollama_models

    connection = mock.Mock()
    response = mock.Mock(status=200)
    response.read.return_value = json.dumps({
        "models": ["not-a-dict", {"name": "qwen2.5-coder:14b"}, 42, None]
    }).encode()
    connection.getresponse.return_value = response
    with mock.patch("lanegate.executor.http.client.HTTPConnection", return_value=connection):
        names = discover_ollama_models("http://localhost:11434")

    assert names == ["qwen2.5-coder:14b"]


def test_discover_ollama_models_returns_empty_on_non_list_models_value():
    from lanegate.executor import discover_ollama_models

    connection = mock.Mock()
    response = mock.Mock(status=200)
    response.read.return_value = json.dumps({"models": "not-a-list"}).encode()
    connection.getresponse.return_value = response
    with mock.patch("lanegate.executor.http.client.HTTPConnection", return_value=connection):
        names = discover_ollama_models("http://localhost:11434")

    assert names == []


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
    cfg = {"reference_docs": ["docs/ARCHITECTURE.md"], "payload_budgets": {"implement": 500}}

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

    components = describe_implement_payload(ticket, project_root=tmp_path, cfg={"reference_docs": ["docs/ARCHITECTURE.md"]})

    assert isinstance(components, list)
    assert components  # non-empty
    for component in components:
        assert set(component.keys()) == {
            "label", "source", "step", "bytes", "tokens_est", "injected", "reason",
        }
        assert component["step"] == "implement"
    labels = {c["label"] for c in components}
    assert "instruction-template" in labels
    assert "reference-excerpt:docs/ARCHITECTURE.md" in labels
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


def test_describe_implement_payload_reports_code_map_notice_size_when_skeletons_exceed_10kb(tmp_path):
    from unittest.mock import patch
    from lanegate.executor import describe_implement_payload

    ticket = _implement_ticket(id="TICK-406", touches=["lanegate/big_module.py"])
    large_skeleton = "def large_func():\n    pass\n" * 750
    assert len(large_skeleton.encode("utf-8")) > 10240

    with patch("lanegate.executor.load_file_skeletons", return_value={"lanegate/big_module.py": large_skeleton}):
        components = describe_implement_payload(ticket, project_root=tmp_path)

    skel_comp = next(c for c in components if c["label"] == "file-skeletons")
    assert skel_comp["bytes"] < 10240
    assert skel_comp["bytes"] > 0


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
    assert "--conversation" in cmd
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
    assert "lanegate symbols" in large_prompt
    assert "## File skeletons" not in large_prompt
    # The sidecar notice must not tell the agent to grep instead of using
    # `lanegate symbols` -- that contradiction sent TICK-413's implement
    # dispatch straight to grep, bypassing the discovery-guidance ranking.
    assert "or grep before making changes" not in large_prompt
    # Discovery guidance must not claim skeletons are "below" when they were
    # actually diverted to the sidecar file referenced above.
    assert "FILE SKELETONS below" not in large_prompt


def test_implement_prompt_skeletons(tmp_path):
    """TICK-412: implement dispatch regenerates skeletons from the current
    worktree file rather than replaying a stale analyze-time snapshot."""
    from lanegate.executor import build_implement_prompt

    touched = tmp_path / "lanegate" / "orchestrate.py"
    touched.parent.mkdir(parents=True)
    touched.write_text("def run_loop(board, executors):\n    pass\n")

    ticket = _implement_ticket(
        file_skeletons={"lanegate/orchestrate.py": "lanegate/orchestrate.py  (1 lines)\n  line   1: def stale_signature()"}
    )

    prompt = build_implement_prompt(ticket, project_root=tmp_path)

    assert "## File skeletons" in prompt
    assert "def run_loop(board, executors)" in prompt
    assert "def stale_signature()" not in prompt


# ---------------------------------------------------------------------------
# parse_codex_json_result — TICK-408: normalize uncached input, num_turns,
# cost_usd so Codex rows are comparable to Claude rows in step_costs.
# ---------------------------------------------------------------------------


def test_parse_codex_json_result_subtracts_cache_read_from_input_tokens():
    """A multi-turn Codex session's final turn.completed usage is cumulative
    across the whole session and its input_tokens includes cache reads --
    unlike Claude, whose usage.input_tokens is already uncached-only. Without
    subtracting cache_read out, a heavily-cached Codex session reports a
    multi-million-token "input" that swamps every Claude row it's averaged
    against (the real bug: 2,690,615 in / 2,553,856 cache_read logged as-is)."""
    jsonl = json.dumps(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 2_690_615,
                "cached_input_tokens": 2_553_856,
                "cache_write_input_tokens": 0,
                "output_tokens": 400,
                "reasoning_output_tokens": 0,
            },
        }
    )
    parsed = parse_codex_json_result(jsonl)
    assert parsed["input_tokens"] == 2_690_615 - 2_553_856
    assert parsed["cache_read_tokens"] == 2_553_856


def test_parse_codex_json_result_num_turns_counts_turn_completed_events():
    jsonl = "\n".join(
        [
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 1}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 1}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 30, "output_tokens": 1}}),
        ]
    )
    parsed = parse_codex_json_result(jsonl)
    assert parsed["num_turns"] == 3


def test_parse_codex_json_result_num_turns_zero_events_but_present_returns_none():
    """No turn.completed at all means no usage block either -- the existing
    'return None for the whole parse' behavior, not num_turns=0."""
    assert parse_codex_json_result(json.dumps({"type": "thread.started"})) is None


def test_parse_codex_json_result_populates_cost_usd_from_normalized_tokens():
    """Codex's own JSONL never reports a dollar figure -- cost_usd must still
    come out populated (not 0, not None) so a codex-heavy step doesn't make
    step_costs' total cost look claude-only while its token average is
    codex-dominated."""
    jsonl = json.dumps(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100_000,
                "cached_input_tokens": 90_000,
                "output_tokens": 1_000,
            },
        }
    )
    parsed = parse_codex_json_result(jsonl)
    assert parsed["cost_usd"] is not None
    assert parsed["cost_usd"] > 0
    # Cost must scale with the *normalized* uncached input (10,000), not the
    # raw cumulative-including-cache figure (100,000) -- otherwise a heavily
    # cached session would still be overpriced by the old cumulative bug.
    cheap_cached_jsonl = json.dumps(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 100_000, "cached_input_tokens": 99_999, "output_tokens": 1_000},
        }
    )
    cheap_parsed = parse_codex_json_result(cheap_cached_jsonl)
    assert cheap_parsed["cost_usd"] < parsed["cost_usd"]


def test_implement_prompt_preserves_matrix_and_preflight(tmp_path):
    from lanegate.executor import build_implement_prompt

    ticket = {
        "id": "TICK-515", "title": "Harden lifecycle", "touches": ["lanegate/lifecycle/__init__.py"],
        "close_criteria": "Lifecycle remains safe.", "_body": "",
        "acceptance_matrix": {
            "invariants": ["Status transitions stay atomic."],
            "adversarial_cases": ["A stale worktree is blocked."],
            "compatibility_cases": ["Existing start remains supported."],
            "regression_tests": ["test_stale_worktree_is_blocked"],
        },
        "overlap_review": {"mode": "stacked_review", "ticket_ids": ["TICK-514"]},
    }
    prompt = build_implement_prompt(ticket, project_root=tmp_path)
    for item in (
        "Status transitions stay atomic.", "A stale worktree is blocked.",
        "Existing start remains supported.", "test_stale_worktree_is_blocked", "TICK-514",
    ):
        assert item in prompt
    assert "Map every item below to an exact test before editing." in prompt
    assert "remove every artifact outside the declared touches" in prompt


def test_implement_prompt_omits_invalid_overlap_plan_from_trusted_context(tmp_path):
    from lanegate.executor import build_implement_prompt

    ticket = {
        "id": "TICK-001", "title": "Fix README", "touches": ["README.md"],
        "close_criteria": "README is corrected.", "_body": "",
        "acceptance_matrix": {"invariants": ["Links remain valid."]},
        "overlap_review": {"mode": "none", "ticket_ids": ["TICK-001"]},
    }

    prompt = build_implement_prompt(ticket, project_root=tmp_path)

    assert "Active overlap plan" not in prompt


class TestExecutorCapabilityRegistry:
    """Tests for the EXECUTOR_CAPABILITIES registry, has_capability, and executor_types_with
    (TICK-646)."""

    # Expected capability flags for every registered executor type.
    # Columns: tool_dispatch_loop, stdin_capable, streaming_capable,
    #          streaming_capable_without_heartbeat
    # Update this table whenever a new executor type is added or a flag changes;
    # the per-field assertions below are derived from it so nothing gets missed.
    EXPECTED: dict[str, dict[str, bool]] = {
        "claude":          {"tool_dispatch_loop": True,  "stdin_capable": True,  "streaming_capable": True,  "streaming_capable_without_heartbeat": True},
        "claude-process":  {"tool_dispatch_loop": True,  "stdin_capable": True,  "streaming_capable": True,  "streaming_capable_without_heartbeat": True},
        "claude-subagent": {"tool_dispatch_loop": True,  "stdin_capable": True,  "streaming_capable": True,  "streaming_capable_without_heartbeat": True},
        "codex":           {"tool_dispatch_loop": True,  "stdin_capable": True,  "streaming_capable": True,  "streaming_capable_without_heartbeat": False},
        "aider":           {"tool_dispatch_loop": False, "stdin_capable": False, "streaming_capable": False, "streaming_capable_without_heartbeat": False},
        "ollama":          {"tool_dispatch_loop": False, "stdin_capable": True,  "streaming_capable": False, "streaming_capable_without_heartbeat": False},
        "agy":             {"tool_dispatch_loop": True,  "stdin_capable": False, "streaming_capable": False, "streaming_capable_without_heartbeat": False},
        "openhands":       {"tool_dispatch_loop": True,  "stdin_capable": False, "streaming_capable": False, "streaming_capable_without_heartbeat": False},
        "gemini":          {"tool_dispatch_loop": True,  "stdin_capable": False, "streaming_capable": False, "streaming_capable_without_heartbeat": False},
        "continue":        {"tool_dispatch_loop": True,  "stdin_capable": False, "streaming_capable": False, "streaming_capable_without_heartbeat": False},
    }

    def test_registry_all_known_types_present(self):
        from lanegate.executor import EXECUTOR_CAPABILITIES

        for known_type in self.EXPECTED:
            assert known_type in EXECUTOR_CAPABILITIES, (
                f"Expected '{known_type}' to be a key in EXECUTOR_CAPABILITIES"
            )

    def test_all_registered_types_have_all_expected_flags(self):
        """Every executor type in EXPECTED must carry all four capability flags."""
        from lanegate.executor import EXECUTOR_CAPABILITIES

        for exec_type, expected_caps in self.EXPECTED.items():
            assert exec_type in EXECUTOR_CAPABILITIES, (
                f"'{exec_type}' missing from EXECUTOR_CAPABILITIES"
            )
            actual = EXECUTOR_CAPABILITIES[exec_type]
            for cap, expected_val in expected_caps.items():
                assert actual.get(cap) is expected_val, (
                    f"EXECUTOR_CAPABILITIES['{exec_type}']['{cap}'] "
                    f"expected {expected_val!r}, got {actual.get(cap)!r}"
                )

    def test_expected_table_covers_every_valid_executor_type(self):
        """EXPECTED is hand-maintained; pin it against config so a new valid
        executor type added without a registry entry fails here instead of
        silently passing every test in this class."""
        from lanegate import config

        assert set(config._VALID_EXECUTOR_TYPES) <= set(self.EXPECTED)

    # --- per-type spot checks (explicit assertions make CI output readable) ---

    def test_capability_flags_claude(self):
        from lanegate.executor import EXECUTOR_CAPABILITIES

        caps = EXECUTOR_CAPABILITIES["claude"]
        assert caps["tool_dispatch_loop"] is True
        assert caps["stdin_capable"] is True
        assert caps["streaming_capable"] is True
        assert caps["streaming_capable_without_heartbeat"] is True

    def test_capability_flags_claude_process(self):
        from lanegate.executor import EXECUTOR_CAPABILITIES

        caps = EXECUTOR_CAPABILITIES["claude-process"]
        assert caps["tool_dispatch_loop"] is True
        assert caps["stdin_capable"] is True
        assert caps["streaming_capable"] is True
        assert caps["streaming_capable_without_heartbeat"] is True

    def test_capability_flags_claude_subagent(self):
        from lanegate.executor import EXECUTOR_CAPABILITIES

        caps = EXECUTOR_CAPABILITIES["claude-subagent"]
        assert caps["tool_dispatch_loop"] is True
        assert caps["stdin_capable"] is True
        assert caps["streaming_capable"] is True
        assert caps["streaming_capable_without_heartbeat"] is True

    def test_capability_flags_codex(self):
        """codex streams JSON events and is stdin-capable, but its output can
        be quiet for extended periods so it must NOT be watchdog-idle-killed when
        running without a heartbeat monitor (review/autofix).  Pinning both flags
        here so this regression is caught before it reaches review again.
        """
        from lanegate.executor import EXECUTOR_CAPABILITIES

        caps = EXECUTOR_CAPABILITIES["codex"]
        assert caps["tool_dispatch_loop"] is True
        assert caps["stdin_capable"] is True
        assert caps["streaming_capable"] is True, (
            "codex.streaming_capable must be True — pool.py (with heartbeat) legitimately "
            "includes it"
        )
        assert caps["streaming_capable_without_heartbeat"] is False, (
            "codex.streaming_capable_without_heartbeat must be False — review/autofix have "
            "no heartbeat monitor, so codex must use the flat hard-ceiling timeout"
        )

    def test_capability_flags_aider(self):
        from lanegate.executor import EXECUTOR_CAPABILITIES

        caps = EXECUTOR_CAPABILITIES["aider"]
        assert caps["tool_dispatch_loop"] is False
        assert caps["stdin_capable"] is False
        assert caps["streaming_capable"] is False
        assert caps["streaming_capable_without_heartbeat"] is False

    def test_capability_flags_ollama(self):
        from lanegate.executor import EXECUTOR_CAPABILITIES

        caps = EXECUTOR_CAPABILITIES["ollama"]
        assert caps["tool_dispatch_loop"] is False
        assert caps["stdin_capable"] is True
        assert caps["streaming_capable"] is False
        assert caps["streaming_capable_without_heartbeat"] is False

    def test_capability_flags_agy(self):
        from lanegate.executor import EXECUTOR_CAPABILITIES

        caps = EXECUTOR_CAPABILITIES["agy"]
        assert caps["tool_dispatch_loop"] is True
        assert caps["stdin_capable"] is False
        assert caps["streaming_capable"] is False
        assert caps["streaming_capable_without_heartbeat"] is False

    def test_capability_flags_openhands(self):
        from lanegate.executor import EXECUTOR_CAPABILITIES

        caps = EXECUTOR_CAPABILITIES["openhands"]
        assert caps["tool_dispatch_loop"] is True, (
            "openhands is an agentic tool-dispatch reviewer — must be tool_dispatch_loop=True"
        )
        assert caps["stdin_capable"] is False
        assert caps["streaming_capable"] is False
        assert caps["streaming_capable_without_heartbeat"] is False

    def test_capability_flags_gemini(self):
        from lanegate.executor import EXECUTOR_CAPABILITIES

        caps = EXECUTOR_CAPABILITIES["gemini"]
        assert caps["tool_dispatch_loop"] is True, (
            "gemini (deprecated Gemini CLI) ran an interactive agent loop — tool_dispatch_loop=True"
        )
        assert caps["stdin_capable"] is False
        assert caps["streaming_capable"] is False
        assert caps["streaming_capable_without_heartbeat"] is False

    def test_capability_flags_continue(self):
        from lanegate.executor import EXECUTOR_CAPABILITIES

        caps = EXECUTOR_CAPABILITIES["continue"]
        assert caps["tool_dispatch_loop"] is True, (
            "continue (Continue.dev) is an agentic assistant — tool_dispatch_loop=True"
        )
        assert caps["stdin_capable"] is False
        assert caps["streaming_capable"] is False
        assert caps["streaming_capable_without_heartbeat"] is False

    # --- executor_types_with ---

    def test_has_capability_unknown_type_returns_false(self):
        from lanegate.executor import has_capability

        result = has_capability("nonexistent", "stdin_capable")
        assert result is False, (
            "has_capability must return False (not raise) for an unrecognized executor type"
        )

    def test_has_capability_unknown_cap_returns_false(self):
        from lanegate.executor import has_capability

        # Even for a known type, an unknown capability key must return False
        result = has_capability("claude", "nonexistent_capability")
        assert result is False

    def test_executor_types_with_stdin_capable_includes_codex(self):
        from lanegate.executor import executor_types_with

        result = executor_types_with("stdin_capable")
        assert "codex" in result, (
            "'codex' must be in executor_types_with('stdin_capable')"
        )

    def test_executor_types_with_tool_dispatch_loop_excludes_aider_and_ollama(self):
        from lanegate.executor import executor_types_with

        result = executor_types_with("tool_dispatch_loop")
        assert "aider" not in result
        assert "ollama" not in result

    def test_executor_types_with_streaming_capable_excludes_ollama_and_aider(self):
        from lanegate.executor import executor_types_with

        result = executor_types_with("streaming_capable")
        assert "ollama" not in result
        assert "aider" not in result

    def test_executor_types_with_streaming_capable_includes_codex(self):
        """pool.py uses streaming_capable (has heartbeat); codex must be included."""
        from lanegate.executor import executor_types_with

        result = executor_types_with("streaming_capable")
        assert "codex" in result, (
            "codex must be in streaming_capable — pool.py (with heartbeat monitor) "
            "legitimately includes it for output-idle watchdog"
        )

    def test_executor_types_with_streaming_capable_without_heartbeat_excludes_codex(self):
        """review/autofix have no heartbeat monitor; codex must NOT be idle-killed."""
        from lanegate.executor import executor_types_with

        result = executor_types_with("streaming_capable_without_heartbeat")
        assert "codex" not in result, (
            "codex must NOT be in streaming_capable_without_heartbeat — without a heartbeat "
            "monitor, a quiet codex would be killed by the 75s idle watchdog instead of "
            "running to the hard ceiling"
        )

    def test_executor_types_with_streaming_capable_without_heartbeat_includes_claude(self):
        from lanegate.executor import executor_types_with

        result = executor_types_with("streaming_capable_without_heartbeat")
        for claude_type in ("claude", "claude-process", "claude-subagent"):
            assert claude_type in result, (
                f"'{claude_type}' must be in streaming_capable_without_heartbeat"
            )

    def test_executor_types_with_reflects_runtime_mutation(self):
        """executor_types_with reads the dict each call, so monkey-patches are reflected."""
        from lanegate import executor as executor_module

        original = executor_module.EXECUTOR_CAPABILITIES.get("__test_type__")
        try:
            executor_module.EXECUTOR_CAPABILITIES["__test_type__"] = {
                "tool_dispatch_loop": False,
                "stdin_capable": True,
                "streaming_capable": False,
                "streaming_capable_without_heartbeat": False,
            }
            result = executor_module.executor_types_with("stdin_capable")
            assert "__test_type__" in result
        finally:
            if original is None:
                executor_module.EXECUTOR_CAPABILITIES.pop("__test_type__", None)
            else:
                executor_module.EXECUTOR_CAPABILITIES["__test_type__"] = original

    # --- is_non_tool_reviewer correctness for all valid executor types ---

    def test_is_non_tool_reviewer_returns_false_for_tool_capable_types(self):
        """Types with tool_dispatch_loop=True must NOT be treated as non-tool reviewers.

        Previously openhands, gemini, and continue were missing from EXECUTOR_CAPABILITIES,
        causing has_capability to return False and is_non_tool_reviewer to return True for
        them — wrongly telling an agentic reviewer to use the diff-inlined non-tool prompt.
        """
        from lanegate.reviewer import is_non_tool_reviewer

        for exec_type, caps in self.EXPECTED.items():
            if caps["tool_dispatch_loop"]:
                assert not is_non_tool_reviewer(exec_type), (
                    f"is_non_tool_reviewer('{exec_type}') must be False "
                    f"(tool_dispatch_loop=True)"
                )

    def test_is_non_tool_reviewer_returns_true_for_non_tool_types(self):
        from lanegate.reviewer import is_non_tool_reviewer

        for exec_type, caps in self.EXPECTED.items():
            if not caps["tool_dispatch_loop"]:
                assert is_non_tool_reviewer(exec_type), (
                    f"is_non_tool_reviewer('{exec_type}') must be True "
                    f"(tool_dispatch_loop=False)"
                )

    def test_is_non_tool_reviewer_none_returns_false(self):
        from lanegate.reviewer import is_non_tool_reviewer

        assert is_non_tool_reviewer(None) is False, (
            "is_non_tool_reviewer(None) must return False (safe default: assume tool-capable)"
        )


# ---------------------------------------------------------------------------
# model_settings per-model override tests (TICK-650)
# ---------------------------------------------------------------------------


def test_aider_model_settings_override(tmp_path, monkeypatch):
    """A model_settings entry for the dispatched model overrides both
    context_window_tokens (enforced by budget check) and edit_format
    (reflected in the aider CLI flags).

    Uses 'ollama_chat/gpt-oss:20b' — a name with slash and colon — to
    confirm that special characters in model names are looked up correctly.

    Budget check assertion: set the per-model context_window_tokens to a
    tiny value (1) while the flat default is large, then confirm the budget
    check fires at the per-model limit (not the flat one).  If the override
    were silently dropped the large flat budget would pass; the ConfigError
    proves the small override was actually used.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "small.py").write_text("x = 1\n")
    cfg = {
        "executors": {
            "aider": {
                "edit_format": "diff",
                "context_window_tokens": 65536,
                "model_settings": {
                    "ollama_chat/gpt-oss:20b": {
                        "context_window_tokens": 131072,
                        "edit_format": "whole",
                    },
                },
            }
        }
    }

    cmd = build_executor_cmd(
        "aider",
        "implement this",
        cfg,
        model="ollama_chat/gpt-oss:20b",
        touches=["small.py"],
    )

    # edit_format from model_settings override must be used
    assert "--edit-format" in cmd
    assert cmd[cmd.index("--edit-format") + 1] == "whole"

    # context_window_tokens override must be enforced by the budget check:
    # set per-model limit to 1 (well below any realistic estimate) while the
    # flat default remains 65536 — a ConfigError proves the small per-model
    # value was used, not the larger flat one.
    cfg_tight = {
        "executors": {
            "aider": {
                "edit_format": "diff",
                "context_window_tokens": 65536,
                "model_settings": {
                    "ollama_chat/gpt-oss:20b": {
                        "context_window_tokens": 1,
                        "edit_format": "whole",
                    },
                },
            }
        }
    }
    with pytest.raises(ConfigError, match="exceeded executors.aider.context_window_tokens"):
        build_executor_cmd(
            "aider",
            "implement this",
            cfg_tight,
            model="ollama_chat/gpt-oss:20b",
            touches=["small.py"],
        )


def test_aider_model_settings_fallback(tmp_path, monkeypatch):
    """When model_settings is present but the dispatched model has no entry,
    flat defaults are used unchanged — unrelated entries have no side effects.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "small.py").write_text("x = 1\n")
    cfg = {
        "executors": {
            "aider": {
                "edit_format": "diff",
                "context_window_tokens": 65536,
                "model_settings": {
                    "ollama_chat/other-model:7b": {
                        "context_window_tokens": 32768,
                        "edit_format": "whole",
                    },
                },
            }
        }
    }

    cmd = build_executor_cmd(
        "aider",
        "implement this",
        cfg,
        model="ollama_chat/qwen2.5-coder:14b",
        touches=["small.py"],
    )

    # Flat default edit_format must be used (not the unrelated model's override)
    assert "--edit-format" in cmd
    assert cmd[cmd.index("--edit-format") + 1] == "diff"


def test_aider_model_settings_null_value_does_not_crash(tmp_path, monkeypatch):
    """A YAML `model_settings:` key with no value (explicit null, e.g. all
    entries commented out) must not crash dispatch. _validate_aider_model_settings
    accepts a None model_settings (config.py: `if model_settings is None: continue`),
    so both read sites must tolerate None too, not just an absent key.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "small.py").write_text("x = 1\n")
    cfg = {
        "executors": {
            "aider": {
                "edit_format": "diff",
                "context_window_tokens": 65536,
                "model_settings": None,
            }
        }
    }

    # edit_format read site (build_executor_cmd) — must not raise
    cmd = build_executor_cmd(
        "aider", "implement this", cfg, model="m", touches=["small.py"]
    )
    assert "--edit-format" in cmd
    assert cmd[cmd.index("--edit-format") + 1] == "diff"

    # context_window_tokens / budget read site — must not raise, read_only path
    build_executor_cmd(
        "aider", "implement this", cfg, model="m", touches=["small.py"], read_only=True
    )


def test_aider_model_settings_partial_override(tmp_path, monkeypatch):
    """A model_settings entry with only context_window_tokens must not
    affect edit_format resolution — partial overrides are per-key independent.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "small.py").write_text("x = 1\n")
    cfg = {
        "executors": {
            "aider": {
                "edit_format": "diff",
                "context_window_tokens": 65536,
                "model_settings": {
                    "ollama_chat/qwen2.5-coder:14b": {
                        # Only context_window_tokens — edit_format must fall back to flat
                        "context_window_tokens": 49152,
                    },
                },
            }
        }
    }

    cmd = build_executor_cmd(
        "aider",
        "implement this",
        cfg,
        model="ollama_chat/qwen2.5-coder:14b",
        touches=["small.py"],
    )

    # edit_format should fall back to flat default ("diff"), not be absent
    assert "--edit-format" in cmd
    assert cmd[cmd.index("--edit-format") + 1] == "diff"


def test_aider_model_settings_after_context_tiers_escalation(tmp_path, monkeypatch):
    """After context_tiers escalates to a secondary model, that secondary
    model's model_settings entry (not the original model's) must be applied.

    The lookup uses the post-escalation model name, so the override must
    appear as the edit_format passed to aider.
    """
    monkeypatch.chdir(tmp_path)
    # A file large enough to exceed the first tier (10_000 tokens), forcing
    # escalation to the second tier (40_000 tokens).
    (tmp_path / "large.py").write_text("x" * 60_000)
    cfg = {
        "executors": {
            "aider": {
                "edit_format": "diff",
                "model_settings": {
                    # Override only for the *escalated* model
                    "ollama/qwen2.5-coder:32b": {
                        "edit_format": "whole",
                    },
                },
                "context_tiers": [
                    {"tokens": 10_000, "model": "ollama/qwen2.5-coder:7b"},
                    {"tokens": 40_000, "model": "ollama/qwen2.5-coder:32b"},
                ],
            }
        }
    }

    cmd = build_executor_cmd(
        "aider",
        "large prompt",
        cfg,
        touches=["large.py"],
        worktree_path=tmp_path,
    )

    # Must have escalated to tier-2 model
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "ollama/qwen2.5-coder:32b"
    # And the escalated model's model_settings override must be used
    assert "--edit-format" in cmd
    assert cmd[cmd.index("--edit-format") + 1] == "whole"


def test_aider_model_settings_context_window_ignored_after_tier_escalation(tmp_path, monkeypatch):
    """A model_settings context_window_tokens override for the escalated model
    must NOT override the tier's own tokens value -- the tier was selected
    specifically because its tokens value fits the estimated request, so a
    smaller static per-model override must not spuriously reject it.
    """
    monkeypatch.chdir(tmp_path)
    # Large enough to exceed tier 1 (10_000) but fit tier 2 (40_000).
    (tmp_path / "large.py").write_text("x" * 60_000)
    cfg = {
        "executors": {
            "aider": {
                "edit_format": "diff",
                "model_settings": {
                    # Deliberately much smaller than the tier's own budget —
                    # before the fix this raised ConfigError even though the
                    # tier system just proved the request fits.
                    "ollama/qwen2.5-coder:32b": {
                        "context_window_tokens": 1_000,
                    },
                },
                "context_tiers": [
                    {"tokens": 10_000, "model": "ollama/qwen2.5-coder:7b"},
                    {"tokens": 40_000, "model": "ollama/qwen2.5-coder:32b"},
                ],
            }
        }
    }

    # Must not raise ConfigError -- the tier's 40_000 budget must be enforced,
    # not the model_settings override's 1_000.
    cmd = build_executor_cmd(
        "aider",
        "large prompt",
        cfg,
        touches=["large.py"],
        worktree_path=tmp_path,
    )
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "ollama/qwen2.5-coder:32b"


def test_aider_model_settings_named_executor_instance(tmp_path, monkeypatch):
    """model_settings on a *named* executor instance (type: aider) must work
    identically to the legacy flat 'aider' key.

    Regression test for finding-1: both read sites previously re-indexed
    cfg["executors"]["aider"] which silently drops model_settings on any
    named instance (e.g. {"aider-local": {type: aider, model_settings: ...}}).
    After the fix, executor_cfg is used directly so the lookup is the same
    regardless of how the executor is keyed.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "small.py").write_text("x = 1\n")
    cfg = {
        "executors": {
            "aider-local": {
                "type": "aider",
                "edit_format": "diff",
                "context_window_tokens": 65536,
                "model_settings": {
                    "ollama_chat/gpt-oss:20b": {
                        "context_window_tokens": 131072,
                        "edit_format": "whole",
                    },
                },
            }
        }
    }

    cmd = build_executor_cmd(
        "aider-local",
        "implement this",
        cfg,
        model="ollama_chat/gpt-oss:20b",
        touches=["small.py"],
    )

    # edit_format override must be used for the named instance
    assert "--edit-format" in cmd
    assert cmd[cmd.index("--edit-format") + 1] == "whole"

    # context_window_tokens override must be enforced for the named instance
    cfg_tight = {
        "executors": {
            "aider-local": {
                "type": "aider",
                "edit_format": "diff",
                "context_window_tokens": 65536,
                "model_settings": {
                    "ollama_chat/gpt-oss:20b": {
                        "context_window_tokens": 1,
                        "edit_format": "whole",
                    },
                },
            }
        }
    }
    with pytest.raises(ConfigError, match="exceeded executors.aider.context_window_tokens"):
        build_executor_cmd(
            "aider-local",
            "implement this",
            cfg_tight,
            model="ollama_chat/gpt-oss:20b",
            touches=["small.py"],
        )

