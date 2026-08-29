"""Tests for config_trust.py trusted Git and repository discovery."""

import os
from contextlib import nullcontext
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from lanegate.config import (
    CONFIG_FILENAME,
    ConfigError,
    _control_checkout_root,
    _trusted_git_executable,
    _windows_git_candidates,
    find_config,
    find_repo_root,
    load_config,
)
from tests._helpers.config import write_config as _write_config


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
        mock.patch("lanegate.config_trust._trusted_git_executable", return_value="/usr/bin/git") as git,
        mock.patch("lanegate.config.subprocess.run", return_value=result) as run,
    ):
        assert _control_checkout_root(tmp_path / "worktree") == control

    git.assert_called_once_with()
    assert run.call_args.args[0][0] == "/usr/bin/git"
    assert run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert run.call_args.kwargs["env"]["LC_ALL"] == "C"
    assert "input" not in run.call_args.kwargs


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell-script git fake + PATH/permission trust semantics")
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

    with mock.patch("lanegate.config_trust._is_protected_executable", side_effect=protected):
        assert _trusted_git_executable() == str(git.resolve())


@pytest.mark.skipif(os.name == "nt", reason="POSIX PATH empty-entry / cwd trust semantics")
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


@pytest.mark.skipif(os.name == "nt", reason="os.geteuid is POSIX-only")
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
        mock.patch("lanegate.config_trust._windows_git_candidates", return_value=(git,)),
    ):
        assert _trusted_git_executable() == str(git.resolve())


def test_control_checkout_root_strips_caller_git_environment(tmp_path, monkeypatch):
    """Agent Git overrides cannot make a worktree look like a non-repository."""
    control = tmp_path / "control"
    result = subprocess.CompletedProcess([], 0, stdout=f"{control / '.git'}\n", stderr="")
    monkeypatch.setenv("GIT_DIR", "/tmp/attacker-controlled-git-dir")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path / "worktree"))

    with (
        mock.patch("lanegate.config_trust._trusted_git_executable", return_value="/usr/bin/git"),
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


@pytest.mark.skipif(os.name == "nt", reason="newline in a filename is rejected by the Windows filesystem")
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


