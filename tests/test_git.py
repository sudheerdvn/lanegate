"""Tests for failure-aware shared Git queries."""

from unittest.mock import MagicMock, patch

from lanegate.git import git_text, pending_commits


def test_pending_commits_distinguishes_empty_success_from_failure(tmp_path):
    with patch("lanegate.git.subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
        result = pending_commits(tmp_path, "stage", "main")

    assert result.ok is True
    assert result.commits == []
    assert result.error is None


def test_pending_commits_parses_successful_range(tmp_path):
    with patch(
        "lanegate.git.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="abc123 first\ndef456 second\n", stderr=""),
    ):
        result = pending_commits(tmp_path, "stage", "main")

    assert result.ok is True
    assert result.commits == ["abc123 first", "def456 second"]


def test_pending_commits_retains_invalid_ref_diagnostic(tmp_path):
    with patch(
        "lanegate.git.subprocess.run",
        return_value=MagicMock(
            returncode=128,
            stdout="",
            stderr="fatal: ambiguous argument 'stage..missing': unknown revision\nmore detail\n",
        ),
    ):
        result = pending_commits(tmp_path, "stage", "missing")

    assert result.ok is False
    assert result.commits == []
    assert result.error is not None
    assert "exit 128" in result.error
    assert "unknown revision" in result.error


def test_git_text_distinguishes_empty_success_from_failure(tmp_path):
    with patch("lanegate.git.subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
        empty_success = git_text(["git", "diff", "HEAD"], tmp_path)

    with patch("lanegate.git.subprocess.run", return_value=MagicMock(returncode=128, stdout="", stderr="")):
        failure = git_text(["git", "diff", "HEAD"], tmp_path)

    assert empty_success.ok is True
    assert empty_success.text == ""
    assert empty_success.error is None
    assert failure.ok is False
    assert failure.text == ""
    assert failure.error is not None
    assert "git diff HEAD failed (exit 128)" in failure.error


def test_git_text_prefers_stderr_then_stdout_for_failure_diagnostics(tmp_path):
    with patch(
        "lanegate.git.subprocess.run",
        return_value=MagicMock(returncode=1, stdout="stdout diagnostic\n", stderr="stderr diagnostic\n"),
    ):
        stderr_failure = git_text(["git", "diff", "HEAD"], tmp_path)
    with patch(
        "lanegate.git.subprocess.run",
        return_value=MagicMock(returncode=1, stdout="stdout diagnostic\n", stderr=""),
    ):
        stdout_failure = git_text(["git", "diff", "HEAD"], tmp_path)

    assert stderr_failure.error is not None
    assert stderr_failure.error.endswith("stderr diagnostic")
    assert stdout_failure.error is not None
    assert stdout_failure.error.endswith("stdout diagnostic")
