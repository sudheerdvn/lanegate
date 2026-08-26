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


def test_find_config_walk_up(tmp_path):
    config_path = tmp_path / CONFIG_FILENAME
    config_path.write_text("ticket_prefix: TICK\n")
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    found = find_config(nested)
    assert found == config_path


def test_find_config_returns_none_when_absent(tmp_path):
    assert find_config(tmp_path) is None


def test_control_checkout_root_does_not_require_path_format_for_legacy_git(tmp_path):
    """Git 2.25 supports --git-common-dir but not --path-format=absolute."""
    control = tmp_path / "control"
    result = subprocess.CompletedProcess([], 0, stdout=f"{control / '.git'}\n", stderr="")

    with mock.patch("lanegate.config.subprocess.run", return_value=result) as run:
        assert _control_checkout_root(tmp_path / "worktree") == control

    assert "--path-format=absolute" not in run.call_args.args[0]


def test_control_checkout_root_uses_platform_git_and_disables_prompts(tmp_path):
    """The trusted probe neither resolves Git from caller PATH nor prompts."""
    control = tmp_path / "control"
    result = subprocess.CompletedProcess([], 0, stdout=f"{control / '.git'}\n", stderr="")

    with (
        mock.patch("lanegate.config._trusted_git_executable", return_value="/usr/bin/git") as git,
        mock.patch("lanegate.config.subprocess.run", return_value=result) as run,
    ):
        assert _control_checkout_root(tmp_path / "worktree") == control

    git.assert_called_once_with()
    assert run.call_args.args[0][0] == "/usr/bin/git"
    assert run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert run.call_args.kwargs["env"]["LC_ALL"] == "C"
    assert "input" not in run.call_args.kwargs


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="trust check is PATH+ownership based only on POSIX; Windows uses registry-only lookup",
)
def test_trusted_git_lookup_accepts_protected_nonstandard_path(tmp_path, monkeypatch):
    """A protected Git installation need not live in a hard-coded prefix."""
    install = tmp_path / "opt" / "company-git" / "bin"
    install.mkdir(parents=True)
    git = install / "git"
    git.write_text("#!/bin/sh\n")
    git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path / 'agent-bin'}{os.pathsep}{install}")

    def protected(path):
        return path == git.resolve()

    with mock.patch("lanegate.config._is_protected_executable", side_effect=protected):
        assert _trusted_git_executable() == str(git.resolve())


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="trust check is PATH+ownership based only on POSIX; Windows uses registry-only lookup",
)
def test_trusted_git_lookup_rejects_current_directory_and_unprotected_path(tmp_path, monkeypatch):
    """PATH cannot select an agent binary, including through an empty entry."""
    agent_bin = tmp_path / "agent-bin"
    agent_bin.mkdir()
    fake_git = agent_bin / "git"
    fake_git.write_text("#!/bin/sh\n")
    fake_git.chmod(0o755)
    monkeypatch.chdir(agent_bin)
    monkeypatch.setenv("PATH", f"{os.pathsep}{agent_bin}")

    with pytest.raises(ConfigError, match="trusted Git control checkout"):
        _trusted_git_executable()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="os.geteuid does not exist on Windows; ownership trust check is POSIX-only",
)
def test_trusted_git_lookup_accepts_root_owned_path_when_running_as_root(tmp_path, monkeypatch):
    """Root cannot use effective ownership as a meaningful trust boundary."""
    git = tmp_path / "git"
    git.write_text("#!/bin/sh\n")
    git.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    with (
        mock.patch("lanegate.config.os.geteuid", return_value=0),
        mock.patch(
            "lanegate.config.Path.stat",
            return_value=mock.Mock(st_uid=0, st_mode=0o100755),
        ),
    ):
        assert _trusted_git_executable() == str(git.resolve())


def test_windows_git_candidates_include_machine_registered_custom_install():
    """A custom admin-installed Git prefix is found without consulting PATH."""
    class FakeWinreg:
        HKEY_LOCAL_MACHINE = object()
        KEY_READ = 1
        KEY_WOW64_64KEY = 2
        KEY_WOW64_32KEY = 4

        @staticmethod
        def OpenKey(root, key_name, reserved, access):
            assert key_name in {
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\git.exe",
                r"SOFTWARE\GitForWindows",
            }
            return nullcontext(key_name)

        @staticmethod
        def QueryValueEx(key, value_name):
            if key == r"SOFTWARE\GitForWindows" and value_name == "InstallPath":
                return r"D:\Tools\Git", 1
            raise OSError("missing")

    with mock.patch.dict("sys.modules", {"winreg": FakeWinreg}):
        candidates = _windows_git_candidates()

    custom_install = Path(r"D:\Tools\Git")
    assert custom_install / "cmd" / "git.exe" in candidates
    assert custom_install / "bin" / "git.exe" in candidates


def test_windows_trusted_git_lookup_uses_registered_custom_install(tmp_path):
    git = tmp_path / "git.exe"
    git.write_text("git")
    git.chmod(0o755)

    with (
        mock.patch("lanegate.config.os.name", "nt"),
        mock.patch("lanegate.config._windows_git_candidates", return_value=(git,)),
    ):
        assert _trusted_git_executable() == str(git.resolve())


def test_control_checkout_root_strips_caller_git_environment(tmp_path, monkeypatch):
    """Agent Git overrides cannot make a worktree look like a non-repository."""
    control = tmp_path / "control"
    result = subprocess.CompletedProcess([], 0, stdout=f"{control / '.git'}\n", stderr="")
    monkeypatch.setenv("GIT_DIR", "/tmp/attacker-controlled-git-dir")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path / "worktree"))

    with (
        mock.patch("lanegate.config._trusted_git_executable", return_value="/usr/bin/git"),
        mock.patch("lanegate.config.subprocess.run", return_value=result) as run,
    ):
        assert _control_checkout_root(tmp_path / "worktree") == control

    probe_env = run.call_args.kwargs["env"]
    assert "GIT_DIR" not in probe_env
    assert "GIT_CEILING_DIRECTORIES" not in probe_env
    assert probe_env["GIT_TERMINAL_PROMPT"] == "0"
    assert run.call_args.args[0][1:] == ["-C", str(tmp_path / "worktree"), "rev-parse", "--git-common-dir"]


def test_control_checkout_root_fails_closed_when_git_probe_fails(tmp_path):
    """A failed Git probe must not restore worktree-local config discovery."""
    result = subprocess.CompletedProcess([], 1, stdout="", stderr="git wrapper refused probe")

    with mock.patch("lanegate.config.subprocess.run", return_value=result):
        with pytest.raises(ConfigError, match="trusted Git control checkout"):
            _control_checkout_root(tmp_path / "worktree")


def test_control_checkout_root_allows_walk_up_outside_a_git_repository(tmp_path):
    """The explicit non-repository result preserves standalone discovery."""
    result = subprocess.CompletedProcess([], 128, stdout="", stderr="fatal: not a git repository")

    with mock.patch("lanegate.config.subprocess.run", return_value=result):
        assert _control_checkout_root(tmp_path) is None


def test_linked_worktree_uses_control_checkout_config(tmp_path):
    """A worktree-local config cannot override lifecycle command safeguards."""
    control = tmp_path / "control"
    worktree = tmp_path / "worktree"
    control.mkdir()
    subprocess.run(["git", "init", str(control)], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(control), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(control), "config", "user.name", "LaneGate Test"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(control), "commit", "--allow-empty", "-m", "initial"],
        check=True,
        capture_output=True,
        text=True,
    )
    _write_config(control / CONFIG_FILENAME, "safeguards:\n  pre_complete: [trusted-check]\n")
    subprocess.run(
        ["git", "-C", str(control), "worktree", "add", "--detach", str(worktree)],
        check=True,
        capture_output=True,
        text=True,
    )
    _write_config(worktree / CONFIG_FILENAME, "safeguards: {}\n")

    assert find_config(worktree) == control / CONFIG_FILENAME
    assert find_repo_root(worktree) == control
    assert load_config(find_repo_root(worktree))["safeguards"]["pre_complete"] == ["trusted-check"]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="a literal newline in a path is not a creatable git worktree name on Windows",
)
def test_newline_named_worktree_uses_control_checkout_config(tmp_path):
    """A newline in a worktree path cannot restore walk-up config discovery."""
    control = tmp_path / "control"
    worktree = tmp_path / "worktree\nnewline"
    control.mkdir()
    subprocess.run(["git", "init", str(control)], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(control), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(control), "config", "user.name", "LaneGate Test"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(control), "commit", "--allow-empty", "-m", "initial"],
        check=True,
        capture_output=True,
        text=True,
    )
    _write_config(control / CONFIG_FILENAME, "safeguards:\n  pre_complete: [trusted-check]\n")
    subprocess.run(
        ["git", "-C", str(control), "worktree", "add", "--detach", str(worktree)],
        check=True,
        capture_output=True,
        text=True,
    )
    _write_config(worktree / CONFIG_FILENAME, "safeguards: {}\n")

    assert find_config(worktree) == control / CONFIG_FILENAME
    assert find_repo_root(worktree) == control


def test_submodule_linked_worktree_uses_submodule_control_config(tmp_path):
    """A submodule's linked worktree cannot use a worktree-local config."""
    source = tmp_path / "submodule-source"
    control = tmp_path / "control"
    worktree = tmp_path / "submodule-worktree"
    source.mkdir()
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "config", "user.name", "LaneGate Test"],
        check=True,
        capture_output=True,
        text=True,
    )
    _write_config(source / CONFIG_FILENAME, "safeguards:\n  pre_complete: [trusted-check]\n")
    subprocess.run(
        ["git", "-C", str(source), "add", CONFIG_FILENAME],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
        text=True,
    )
    control.mkdir()
    subprocess.run(["git", "init", str(control)], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(control), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(control), "config", "user.name", "LaneGate Test"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(control), "-c", "protocol.file.allow=always", "submodule", "add", str(source), "sub"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(control), "commit", "-am", "add submodule"],
        check=True,
        capture_output=True,
        text=True,
    )
    submodule = control / "sub"
    subprocess.run(
        ["git", "-C", str(submodule), "worktree", "add", "--detach", str(worktree)],
        check=True,
        capture_output=True,
        text=True,
    )
    _write_config(worktree / CONFIG_FILENAME, "safeguards: {}\n")

    assert find_config(worktree) == submodule / CONFIG_FILENAME
    assert find_repo_root(worktree) == submodule


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


def test_detects_npm_test_cra_and_angular(tmp_path):
    cra_dir = tmp_path / "cra"
    cra_dir.mkdir()
    (cra_dir / "package.json").write_text(
        json.dumps(
            {
                "scripts": {"test": "react-scripts test"},
                "dependencies": {"react-scripts": "5.0.1"},
            }
        )
    )
    assert _detected_commands(cra_dir) == ["CI=true npm test"]

    angular_dir = tmp_path / "angular"
    angular_dir.mkdir()
    (angular_dir / "package.json").write_text(
        json.dumps(
            {
                "scripts": {"test": "ng test"},
                "devDependencies": {"@angular/cli": "17.0.0"},
            }
        )
    )
    assert _detected_commands(angular_dir) == ["ng test --watch=false"]


def test_detects_cargo_test(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname = \"demo\"\n")

    assert _detected_commands(tmp_path) == ["cargo test"]


def test_detects_go_test(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/demo\n")

    assert _detected_commands(tmp_path) == ["go test"]


def test_detects_maven(tmp_path):
    (tmp_path / "pom.xml").write_text("<project></project>\n")

    assert _detected_commands(tmp_path) == ["mvn test"]


def test_detects_gradle(tmp_path):
    (tmp_path / "build.gradle").write_text("plugins { id 'java' }\n")

    assert _detected_commands(tmp_path) == ["./gradlew test"]


def test_detects_gradle_kts(tmp_path):
    (tmp_path / "build.gradle.kts").write_text("plugins { id(\"java\") }\n")

    assert _detected_commands(tmp_path) == ["./gradlew test"]


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

    @pytest.fixture(autouse=True)
    def _no_ollama_network(self):
        """discover_ollama_models makes a real (bounded, 1s) network call --
        tests that don't specifically exercise it must not depend on whether
        a local Ollama happens to be running on this machine (TICK-645)."""
        with mock.patch("lanegate.executor.discover_ollama_models", return_value=[]):
            yield

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

    def test_invalid_model_input_reprompts_until_valid(self, tmp_path, capsys):
        """An unmapped model string (e.g. a GPT model for a claude executor)
        must not be written into models: silently -- it previously left the
        resulting .lanegate.yml unable to load at all (unmapped model error
        on every `lanegate` command, with no way to re-run init to fix it)."""
        responses = {"  models.implement": iter(["gpt-4-turbo", "claude-sonnet-5"])}

        def fake_input(prompt_text: str = "") -> str:
            for key, it in responses.items():
                if key in prompt_text:
                    return next(it, "")
            return ""

        with (
            mock.patch("sys.stdin") as mock_stdin,
            mock.patch("builtins.input", side_effect=fake_input),
            mock.patch("lanegate.config._registry_save"),
        ):
            mock_stdin.isatty.return_value = False
            cfg = interactive_init(tmp_path, force_interactive=True)

        assert cfg["models"]["implement"] == "claude-sonnet-5"
        assert "Invalid model" in capsys.readouterr().out

    def test_edit_format_rejects_yn_shaped_input_then_accepts_valid(self, tmp_path, capsys):
        """Every other optional step in this wizard is an input(...[y/N])
        confirm; typing 'y' here from muscle memory must not land in config
        verbatim as `edit_format: y`, which every aider dispatch would then
        pass straight through as `aider --edit-format y`."""
        responses = {
            "executor ": iter(["aider"]),
            "executors.aider.edit_format": iter(["y", "whole"]),
        }

        def fake_input(prompt_text: str = "") -> str:
            for key, it in responses.items():
                if key in prompt_text:
                    return next(it, "")
            return ""

        with (
            mock.patch("sys.stdin") as mock_stdin,
            mock.patch("builtins.input", side_effect=fake_input),
            mock.patch("lanegate.config._registry_save"),
        ):
            mock_stdin.isatty.return_value = False
            cfg = interactive_init(tmp_path, force_interactive=True)

        assert cfg["executors"]["aider"]["edit_format"] == "whole"
        assert "Invalid edit_format" in capsys.readouterr().out

    def test_blank_reviewer_prompt_leaves_reviewer_unset(self, tmp_path):
        """Accepting a blank reviewer prompt must not silently write an
        explicit `reviewer: <executor>` pin -- an explicit pin always wins
        outright over resolve_independent_review_driver's ladder, including
        the review_fallback: needs_review safety escalation that would
        otherwise apply to an unconfigured single-account setup. Confirmed
        live in a fresh-install smoke test: this previously meant every
        interactively-initialized project permanently disabled that safety
        net, even for someone who just hit Enter through the wizard."""
        with (
            mock.patch("sys.stdin") as mock_stdin,
            mock.patch("builtins.input", side_effect=lambda _="": ""),
            mock.patch("lanegate.config._registry_save"),
        ):
            mock_stdin.isatty.return_value = False
            cfg = interactive_init(tmp_path, force_interactive=True)

        assert cfg["executor"] == "claude"
        assert "reviewer" not in cfg

    def test_reviewer_prompt_bracket_shows_auto_not_executor_name(self, tmp_path):
        """The reviewer prompt's bracketed default must not display the
        executor name (e.g. '[agy]'): every other prompt in this wizard uses
        the bracket to mean 'this is what Enter accepts', but a blank
        reviewer answer does NOT write that value into config the way a
        normal default would -- it leaves reviewer unset so the independence
        ladder runs at dispatch time. Showing the executor name there looks
        exactly like a normal default and misleads a user into thinking
        blank == pinned to that executor. Confirmed live in a fresh-install
        smoke test (agy round)."""
        seen_prompts: list[str] = []

        def fake_input(prompt_text: str = "") -> str:
            seen_prompts.append(prompt_text)
            return ""

        with (
            mock.patch("sys.stdin") as mock_stdin,
            mock.patch("builtins.input", side_effect=fake_input),
            mock.patch("lanegate.config._registry_save"),
        ):
            mock_stdin.isatty.return_value = False
            interactive_init(tmp_path, force_interactive=True)

        reviewer_prompt = next(p for p in seen_prompts if p.startswith("reviewer "))
        assert "[auto]" in reviewer_prompt
        assert "[claude]" not in reviewer_prompt

    def test_explicit_reviewer_prompt_input_is_still_written(self, tmp_path):
        """Typing a value at the reviewer prompt -- even one matching the
        executor -- is a deliberate choice and must still be written as an
        explicit pin, unlike leaving it blank."""

        def fake_input(prompt_text: str = "") -> str:
            if prompt_text.startswith("reviewer "):
                return "claude"
            return ""

        with (
            mock.patch("sys.stdin") as mock_stdin,
            mock.patch("builtins.input", side_effect=fake_input),
            mock.patch("lanegate.config._registry_save"),
        ):
            mock_stdin.isatty.return_value = False
            cfg = interactive_init(tmp_path, force_interactive=True)

        assert cfg["reviewer"] == "claude"

    def test_wizard_tolerates_stdin_exhausted_mid_prompt(self, tmp_path, capsys):
        """Piped stdin that runs out mid-wizard (input() raises EOFError)
        must degrade every remaining prompt to its default instead of
        crashing with a raw traceback -- confirmed live in a fresh-install
        smoke test: some prompts tolerated an empty/exhausted stdin while
        others (anything routed through _prompt, the majority of the
        wizard) did not. EOFError should behave exactly like an accepted
        blank answer throughout, including the reviewer prompt's stricter
        blank-vs-typed distinction. It must also print a one-time warning
        (not one per remaining prompt) -- confirmed live in a later
        fresh-install round: silently defaulting the rest of the wizard on
        exhausted stdin gave no signal that a piped answer string was the
        wrong length, so a miscounted/misaligned answer set could write an
        unintended config with nothing flagging it."""

        def raise_eof(_: str = "") -> str:
            raise EOFError()

        with (
            mock.patch("sys.stdin") as mock_stdin,
            mock.patch("builtins.input", side_effect=raise_eof),
            mock.patch("lanegate.config._registry_save"),
        ):
            mock_stdin.isatty.return_value = False
            cfg = interactive_init(tmp_path, force_interactive=True)

        assert cfg["executor"] == "claude"
        assert "reviewer" not in cfg
        err = capsys.readouterr().err
        assert err.count("stdin ran out mid-wizard") == 1

    def test_typo_reviewer_prompt_input_leaves_reviewer_unset(self, tmp_path, capsys):
        """An unrecognized (typo'd) reviewer answer must not pin reviewer to
        the executor the same way a deliberate match does -- that would
        disable the independence ladder's safety net by mistake instead of
        by an informed choice, the exact footgun the blank-answer fix
        already covers for an empty Enter."""

        def fake_input(prompt_text: str = "") -> str:
            if prompt_text.startswith("reviewer "):
                return "clualde"  # typo
            return ""

        with (
            mock.patch("sys.stdin") as mock_stdin,
            mock.patch("builtins.input", side_effect=fake_input),
            mock.patch("lanegate.config._registry_save"),
        ):
            mock_stdin.isatty.return_value = False
            cfg = interactive_init(tmp_path, force_interactive=True)

        assert "reviewer" not in cfg
        assert "not a recognised reviewer" in capsys.readouterr().out

    def test_non_tty_without_force_prints_hint(self, tmp_path, capsys):
        """Non-TTY without force_interactive prints the --interactive hint to stderr."""
        with mock.patch("sys.stdin") as mock_stdin, mock.patch("lanegate.config._registry_save"):
            mock_stdin.isatty.return_value = False
            interactive_init(tmp_path)
        err = capsys.readouterr().err
        assert "--interactive" in err

    def test_init_adds_gitignore_entries(self, tmp_path):
        """lanegate init appends .lanegate/ to .gitignore, but not .lanegate.yml
        itself -- a worktree only sees committed content, so an ignored config
        would leave the first ticket's worktree without one at all."""
        with mock.patch("lanegate.config._registry_save"):
            interactive_init(tmp_path, use_defaults=True)

        gitignore = tmp_path / ".gitignore"
        assert gitignore.exists()
        content = gitignore.read_text()
        assert ".lanegate/" in content
        assert "!.lanegate/tickets/" in content
        assert ".lanegate.yml" not in content
        assert ".aider.*" not in content

    def test_init_with_aider_executor_gitignores_its_scratch_files(self, tmp_path):
        """aider's own scratch/cache files (chat history, input history,
        tags cache) are normally kept out of git by aider silently editing
        .gitignore itself at startup -- an uncommitted side effect LaneGate's
        scope-drift check then flags as an unexpected file. Writing the
        pattern into the project's own .gitignore up front means aider's own
        gitignore-editing is a no-op, and the pattern still holds even if a
        hand-written config later adds --no-gitignore to skip that edit."""

        def fake_input(prompt_text: str = "") -> str:
            if prompt_text.startswith("executor "):
                return "aider"
            return ""

        with (
            mock.patch("sys.stdin") as mock_stdin,
            mock.patch("builtins.input", side_effect=fake_input),
            mock.patch("lanegate.config._registry_save"),
        ):
            mock_stdin.isatty.return_value = False
            interactive_init(tmp_path, force_interactive=True)

        content = (tmp_path / ".gitignore").read_text()
        assert ".aider.*" in content

    def test_init_migrates_stale_config_filename_gitignore_entry(self, tmp_path):
        """A project initialized before .lanegate.yml was excluded from
        _gitignore_entries() has that stale line in its own .gitignore.
        Re-running init must strip it, not just leave it there forever --
        otherwise the config stays gitignored/uncommitted on every upgrade,
        reproducing the exact never-committed-config bug the exclusion
        itself was meant to fix."""
        (tmp_path / ".gitignore").write_text("node_modules/\n.lanegate.yml\n*.log\n")

        with mock.patch("lanegate.config._registry_save"):
            interactive_init(tmp_path, use_defaults=True)

        content = (tmp_path / ".gitignore").read_text()
        assert ".lanegate.yml" not in content
        assert "node_modules/" in content
        assert "*.log" in content
        assert ".lanegate/" in content

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

    def test_init_appends_to_existing_gitignore(self, tmp_path):
        """When .gitignore already exists, entries are appended not overwritten."""
        (tmp_path / ".gitignore").write_text("__pycache__/\n*.pyc\n")
        with mock.patch("lanegate.config._registry_save"):
            interactive_init(tmp_path, use_defaults=True)

        content = (tmp_path / ".gitignore").read_text()
        assert "__pycache__/" in content
        assert "*.pyc" in content
        assert ".lanegate/" in content
        assert ".lanegate.yml" not in content

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


class TestInteractiveInitLocalOllamaWorkflow:
    """TICK-645: init wizard gaps found end-to-end-testing a local Ollama/Aider
    setup -- trunk branch, autonomy, model discovery, edit_format, and
    review_fallback all silently produced a config that needed manual
    .lanegate.yml edits before `lanegate run` worked unattended."""

    @pytest.fixture(autouse=True)
    def _no_ollama_network(self):
        with mock.patch("lanegate.executor.discover_ollama_models", return_value=[]):
            yield

    def _run(self, tmp_path, fake_input):
        with (
            mock.patch("sys.stdin") as mock_stdin,
            mock.patch("builtins.input", side_effect=fake_input),
            mock.patch("lanegate.config._registry_save"),
        ):
            mock_stdin.isatty.return_value = False
            return interactive_init(tmp_path, force_interactive=True)

    # --- trunk_branch: active branch, not a blind "main" ---

    def test_trunk_branch_defaults_to_current_git_branch(self, tmp_path):
        with mock.patch("lanegate.config._current_git_branch", return_value="refactor-code"):
            cfg = self._run(tmp_path, lambda _="": "")
        assert cfg["trunk_branch"] == "refactor-code"

    def test_trunk_branch_falls_back_to_main_when_undetectable(self, tmp_path):
        with mock.patch("lanegate.config._current_git_branch", return_value=None):
            cfg = self._run(tmp_path, lambda _="": "")
        assert cfg["trunk_branch"] == "main"

    def test_trunk_branch_typed_override_is_respected(self, tmp_path):
        def fake_input(prompt_text: str = "") -> str:
            return "develop" if prompt_text.startswith("trunk_branch ") else ""

        with mock.patch("lanegate.config._current_git_branch", return_value="refactor-code"):
            cfg = self._run(tmp_path, fake_input)
        assert cfg["trunk_branch"] == "develop"

    def test_trunk_branch_prefers_real_origin_head_over_current_branch(self, tmp_path):
        """A cloned repo with a remote configured (origin/HEAD -> main) but
        currently checked out on a local feature/WIP branch during `init`
        must default to "main" -- the real, authoritative trunk -- not the
        branch the user happens to be sitting on right now. Only a repo with
        no detectable origin/HEAD at all (a fresh local project, TICK-645)
        should fall back to suggesting the checked-out branch."""
        with (
            mock.patch("lanegate.config._detect_origin_head_branch", return_value="main"),
            mock.patch("lanegate.config._current_git_branch", return_value="my-feature"),
        ):
            cfg = self._run(tmp_path, lambda _="": "")
        assert cfg["trunk_branch"] == "main"

    # --- autonomy: explicit opt-in to unattended "full" ---

    def test_autonomy_default_stays_supervised_and_unset(self, tmp_path):
        """Blank answer must not write an explicit autonomy: supervised --
        resolve_autonomy() already defaults there; the point is offering an
        opt-in to full, not changing the safe default's behavior."""
        cfg = self._run(tmp_path, lambda _="": "")
        assert "autonomy" not in cfg
        assert "default_human_review" not in cfg

    def test_autonomy_full_choice_sets_autonomy_and_human_review(self, tmp_path):
        def fake_input(prompt_text: str = "") -> str:
            return "1" if prompt_text.startswith("autonomy ") else ""

        cfg = self._run(tmp_path, fake_input)
        assert cfg["autonomy"] == "full"
        assert cfg["default_human_review"] == "none"

    def test_autonomy_invalid_choice_warns_and_stays_supervised(self, tmp_path, capsys):
        def fake_input(prompt_text: str = "") -> str:
            return "9" if prompt_text.startswith("autonomy ") else ""

        cfg = self._run(tmp_path, fake_input)
        assert "autonomy" not in cfg
        assert "Invalid choice" in capsys.readouterr().out

    # --- edit_format: size-aware, not a flat "whole" ---

    def test_edit_format_defaults_to_diff_when_large_file_detected(self, tmp_path):
        def fake_input(prompt_text: str = "") -> str:
            return "aider" if prompt_text.startswith("executor ") else ""

        with mock.patch(
            "lanegate.config._recommend_aider_edit_format",
            return_value=("diff", "Detected `capture.py` at 1200 lines"),
        ):
            cfg = self._run(tmp_path, fake_input)
        assert cfg["executors"]["aider"]["edit_format"] == "diff"

    def test_edit_format_note_printed_when_large_file_detected(self, tmp_path, capsys):
        def fake_input(prompt_text: str = "") -> str:
            return "aider" if prompt_text.startswith("executor ") else ""

        with mock.patch(
            "lanegate.config._recommend_aider_edit_format",
            return_value=("diff", "Detected `capture.py` at 1200 lines"),
        ):
            self._run(tmp_path, fake_input)
        assert "Detected `capture.py` at 1200 lines" in capsys.readouterr().out

    def test_edit_format_defaults_to_whole_when_no_large_file(self, tmp_path):
        def fake_input(prompt_text: str = "") -> str:
            return "aider" if prompt_text.startswith("executor ") else ""

        with mock.patch(
            "lanegate.config._recommend_aider_edit_format", return_value=("whole", None)
        ):
            cfg = self._run(tmp_path, fake_input)
        assert cfg["executors"]["aider"]["edit_format"] == "whole"

    # --- models.fix / models.drift_check: same unconfigured-step gap as
    # analyze/implement/review, and a local aider+Ollama route gets a
    # friendlier max_auto_fix_attempts default since retries are free ---

    def test_model_prompts_include_fix_and_drift_check(self, tmp_path, capsys):
        def fake_input(prompt_text: str = "") -> str:
            return "aider" if prompt_text.startswith("executor ") else ""

        self._run(tmp_path, fake_input)
        out = capsys.readouterr().out
        assert "models.fix" in out
        assert "models.drift_check" in out

    def test_blank_fix_and_drift_check_prompts_use_wizard_defaults(self, tmp_path):
        def fake_input(prompt_text: str = "") -> str:
            return "aider" if prompt_text.startswith("executor ") else ""

        cfg = self._run(tmp_path, fake_input)
        assert cfg["models"]["fix"] == cfg["models"]["implement"]
        assert cfg["models"]["drift_check"] == cfg["models"]["review"]

    def test_local_ollama_aider_defaults_max_auto_fix_attempts_to_two(self, tmp_path):
        def fake_input(prompt_text: str = "") -> str:
            return "aider" if prompt_text.startswith("executor ") else ""

        cfg = self._run(tmp_path, fake_input)
        assert cfg["executors"]["aider"]["provider"] == "ollama"
        assert cfg["max_auto_fix_attempts"] == 2

    def test_cloud_executor_leaves_max_auto_fix_attempts_at_runtime_default(self, tmp_path):
        # claude is the wizard's own default executor -- accepting every
        # prompt blank never routes through the local-Ollama branch, so
        # this must NOT set max_auto_fix_attempts: the cloud-cost guardrail
        # default of 1 (see resolve_model / config.DEFAULTS) should apply
        # unless the project opts in explicitly.
        cfg = self._run(tmp_path, lambda _="": "")
        assert cfg["executor"] == "claude"
        assert "max_auto_fix_attempts" not in cfg

    def test_recommend_aider_edit_format_flags_file_too_large_to_line_count(self, tmp_path):
        """A tracked file over the 2MB threshold must not be silently
        excluded from consideration (the file is too large/risky to safely
        open and line-count) -- it should still trigger the 'diff'
        recommendation directly from its size, since excluding it entirely
        would mean the single riskiest file in the repo could never be the
        one that triggers the warning."""
        from lanegate.config import _recommend_aider_edit_format

        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        huge = tmp_path / "huge_generated.py"
        huge.write_text("x" * 2_500_000)
        subprocess.run(["git", "add", "huge_generated.py"], cwd=tmp_path, check=True)

        edit_format, note = _recommend_aider_edit_format(tmp_path)
        assert edit_format == "diff"
        assert "huge_generated.py" in note
        assert "malformed hunk" in note

    def test_recommend_aider_edit_format_stops_scanning_once_threshold_crossed(self, tmp_path):
        """Once a file's line count crosses the 300-line 'diff' threshold,
        further line-counting can't change the recommendation -- only which
        filename gets cited. Scanning should stop there instead of opening
        and reading every one of up to 3000 candidate files synchronously
        inside the interactive wizard."""
        from lanegate.config import _recommend_aider_edit_format

        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        big = tmp_path / "a_big.py"
        big.write_text("\n".join(f"line {i}" for i in range(400)))
        for i in range(20):
            (tmp_path / f"b_small_{i}.py").write_text("pass\n")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

        opened = []
        real_open = open

        def tracking_open(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=tracking_open):
            edit_format, note = _recommend_aider_edit_format(tmp_path)

        assert edit_format == "diff"
        assert "a_big.py" in note
        assert len(opened) == 1

    def test_recommend_aider_edit_format_scans_repo_line_counts(self, tmp_path):
        """Real (unmocked) behavior of the recommender itself, against actual
        git-tracked files."""
        from lanegate.config import _recommend_aider_edit_format

        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        big = tmp_path / "capture.py"
        big.write_text("\n".join(f"line {i}" for i in range(1200)))
        subprocess.run(["git", "add", "capture.py"], cwd=tmp_path, check=True)

        edit_format, note = _recommend_aider_edit_format(tmp_path)
        assert edit_format == "diff"
        assert "capture.py" in note
        assert "1200" in note

    def test_recommend_aider_edit_format_note_mentions_diff_hunk_risk(self, tmp_path):
        """When 'diff' is recommended because of a large file, the note must
        also re-surface diff's own known malformed-hunk risk for small local
        models, not only explain why 'whole' is risky."""
        from lanegate.config import _recommend_aider_edit_format

        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        big = tmp_path / "capture.py"
        big.write_text("\n".join(f"line {i}" for i in range(1200)))
        subprocess.run(["git", "add", "capture.py"], cwd=tmp_path, check=True)

        _, note = _recommend_aider_edit_format(tmp_path)
        assert "malformed hunk" in note

    def test_recommend_aider_edit_format_finds_large_file_beyond_tree_order_cutoff(self, tmp_path):
        """A large tracked file that sorts alphabetically/tree-order after
        the first 3000 entries (e.g. under vendor/... or zz_generated...)
        must still be detected -- `git ls-files` order is not size order, so
        scanning only the first 3000 entries in that order can miss the very
        file that would break 'whole'."""
        from lanegate.config import _recommend_aider_edit_format

        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        for i in range(3005):
            (tmp_path / f"a_{i:05d}.py").write_text("pass\n")
        big = tmp_path / "zz_generated.py"
        big.write_text("\n".join(f"line {i}" for i in range(1200)))
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

        edit_format, note = _recommend_aider_edit_format(tmp_path)
        assert edit_format == "diff"
        assert "zz_generated.py" in note

    def test_recommend_aider_edit_format_whole_when_all_files_small(self, tmp_path):
        from lanegate.config import _recommend_aider_edit_format

        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        small = tmp_path / "small.py"
        small.write_text("\n".join(f"line {i}" for i in range(10)))
        subprocess.run(["git", "add", "small.py"], cwd=tmp_path, check=True)

        edit_format, note = _recommend_aider_edit_format(tmp_path)
        assert edit_format == "whole"
        assert note is None

    def test_recommend_aider_edit_format_scans_only_provided_touches(self, tmp_path):
        from lanegate.config import _recommend_aider_edit_format

        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        large = tmp_path / "large.py"
        large.write_text("\n".join(f"line {i}" for i in range(400)))
        small = tmp_path / "small.py"
        small.write_text("\n".join(f"line {i}" for i in range(10)))
        subprocess.run(["git", "add", "large.py", "small.py"], cwd=tmp_path, check=True)

        # Scans all tracked files, large.py crosses threshold -> diff
        edit_format, note = _recommend_aider_edit_format(tmp_path)
        assert edit_format == "diff"

        # Limit touches to small.py -> ignores large.py -> whole
        edit_format, note = _recommend_aider_edit_format(tmp_path, touches={"small.py"})
        assert edit_format == "whole"
        assert note is None

    # --- review_fallback: independent models on the same tool ---

    def test_review_fallback_set_when_same_tool_different_models(self, tmp_path):
        def fake_input(prompt_text: str = "") -> str:
            if prompt_text.startswith("executor "):
                return "aider"
            if prompt_text.startswith("reviewer "):
                return "aider"
            if prompt_text.startswith("  models.implement"):
                return "ollama_chat/qwen2.5-coder:14b"
            if prompt_text.startswith("  models.review"):
                return "ollama_chat/qwen2.5-coder:32b"
            return ""

        cfg = self._run(tmp_path, fake_input)
        assert cfg["reviewer"] == "aider"
        assert cfg["review_fallback"] == "different_model"

    def test_review_fallback_not_set_when_models_match(self, tmp_path):
        def fake_input(prompt_text: str = "") -> str:
            if prompt_text.startswith("executor "):
                return "aider"
            if prompt_text.startswith("reviewer "):
                return "aider"
            if prompt_text.startswith("  models."):
                return "ollama_chat/qwen2.5-coder:14b"
            return ""

        cfg = self._run(tmp_path, fake_input)
        assert "review_fallback" not in cfg

    def test_review_fallback_not_set_when_reviewer_differs_from_executor(self, tmp_path):
        def fake_input(prompt_text: str = "") -> str:
            if prompt_text.startswith("executor "):
                return "aider"
            if prompt_text.startswith("reviewer "):
                return "claude"
            if prompt_text.startswith("  models.implement"):
                return "ollama_chat/qwen2.5-coder:14b"
            if prompt_text.startswith("  models.review"):
                return "claude-sonnet-5"
            return ""

        cfg = self._run(tmp_path, fake_input)
        assert "review_fallback" not in cfg

    def test_review_fallback_not_set_when_reviewer_prompt_left_blank(self, tmp_path):
        """A blank reviewer prompt makes the local `reviewer` variable default
        to `executor` internally, but reviewer_explicit stays False and no
        cfg["reviewer"] key is written -- review_fallback must not key off
        that internal default the same way it keys off an actual explicit
        same-tool pin, or it silently bypasses the reviewer_explicit
        safeguard that blank-answer case was specifically built for."""
        def fake_input(prompt_text: str = "") -> str:
            if prompt_text.startswith("executor "):
                return "aider"
            if prompt_text.startswith("reviewer "):
                return ""
            if prompt_text.startswith("  models.implement"):
                return "ollama_chat/qwen2.5-coder:14b"
            if prompt_text.startswith("  models.review"):
                return "ollama_chat/qwen2.5-coder:32b"
            return ""

        cfg = self._run(tmp_path, fake_input)
        assert "reviewer" not in cfg
        assert "review_fallback" not in cfg

    # --- model discovery: suggest what's actually installed ---

    def test_model_prompt_offers_discovered_ollama_models_for_aider(self, tmp_path, capsys):
        def fake_input(prompt_text: str = "") -> str:
            return "aider" if prompt_text.startswith("executor ") else ""

        with mock.patch(
            "lanegate.executor.discover_ollama_models",
            return_value=["qwen2.5-coder:14b", "qwen2.5-coder:32b"],
        ):
            cfg = self._run(tmp_path, fake_input)

        out = capsys.readouterr().out
        assert "ollama_chat/qwen2.5-coder:14b" in out
        assert "ollama_chat/qwen2.5-coder:32b" in out
        assert "ollama_chat/" in out and "'ollama_chat/' prefix" in out
        # analyze/implement should suggest the 14b tag, review the 32b tag
        assert cfg["models"]["analyze"] == "ollama_chat/qwen2.5-coder:14b"
        assert cfg["models"]["review"] == "ollama_chat/qwen2.5-coder:32b"

    def test_model_prompt_picker_number_selects_discovered_model(self, tmp_path):
        def fake_input(prompt_text: str = "") -> str:
            if prompt_text.startswith("executor "):
                return "aider"
            if prompt_text.startswith("  models.analyze"):
                return "2"
            return ""

        with mock.patch(
            "lanegate.executor.discover_ollama_models",
            return_value=["qwen2.5-coder:14b", "qwen3-coder:30b"],
        ):
            cfg = self._run(tmp_path, fake_input)

        assert cfg["models"]["analyze"] == "ollama_chat/qwen3-coder:30b"

    def test_model_prompt_raw_ollama_executor_gets_no_prefix(self, tmp_path):
        def fake_input(prompt_text: str = "") -> str:
            return "ollama" if prompt_text.startswith("executor ") else ""

        with mock.patch(
            "lanegate.executor.discover_ollama_models",
            return_value=["qwen2.5-coder:14b"],
        ):
            cfg = self._run(tmp_path, fake_input)

        assert cfg["models"]["analyze"] == "qwen2.5-coder:14b"

    def test_model_prompt_falls_back_to_hardcoded_default_when_discovery_empty(self, tmp_path):
        """discover_ollama_models() returning [] (Ollama not running / no
        models pulled) must reproduce the pre-TICK-645 hardcoded suggestion,
        not leave the prompt without any default."""

        def fake_input(prompt_text: str = "") -> str:
            return "aider" if prompt_text.startswith("executor ") else ""

        cfg = self._run(tmp_path, fake_input)  # autouse fixture -> discovery returns []
        assert cfg["models"]["analyze"] == "ollama_chat/qwen2.5-coder:14b"

    def test_model_prompt_rejects_out_of_range_picker_digit(self, tmp_path, capsys):
        """An out-of-range picker digit (only 2 models discovered, user types
        "9") must not fall through picker.get(value, value) as a literal
        model string "9" -- validate_model_for_executor() has no branch for
        executor_type == "ollama" at all, so that would be silently accepted
        and written to .lanegate.yml, only failing later at dispatch."""
        calls = {"n": 0}

        def fake_input(prompt_text: str = "") -> str:
            if prompt_text.startswith("executor "):
                return "aider"
            if prompt_text.startswith("  models.analyze"):
                calls["n"] += 1
                return "9" if calls["n"] == 1 else "1"
            return ""

        with mock.patch(
            "lanegate.executor.discover_ollama_models",
            return_value=["qwen2.5-coder:14b", "qwen3-coder:30b"],
        ):
            cfg = self._run(tmp_path, fake_input)

        assert "Invalid choice" in capsys.readouterr().out
        assert cfg["models"]["analyze"] == "ollama_chat/qwen2.5-coder:14b"
        assert calls["n"] == 2


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
        assert _gitignore_entries() == [".testbrand/*", "testbrand-context-log.jsonl"]


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


# ---------------------------------------------------------------------------
# model_settings validation tests (TICK-650)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Risk-based autonomy lanes (TICK-467)
# ---------------------------------------------------------------------------


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
