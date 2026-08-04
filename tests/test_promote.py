"""Tests for promote.py — guard blocks, auto-env rejection, post_promote ordering."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from lanegate.git import PendingCommits
from lanegate.promote import _auto_promote_environments, _run_promotion, cmd_promote


def _env(name, trigger="manual", branch=None, guard=None, pre=None, post=None, from_="main"):
    return {
        "name": name,
        "branch": branch or name,
        "from": from_,
        "trigger": trigger,
        "sync": "ff-only",
        "guard_script": guard,
        "pre_promote": pre or [],
        "post_promote": post or [],
    }


def _cfg(envs):
    return {
        "environments": envs,
        "worktrees_dir": "worktrees",
        "ticket_prefix": "TICK",
    }


def _pending(*commits: str) -> PendingCommits:
    return PendingCommits(list(commits))


def test_promote_auto_env_is_rejected(tmp_path):
    cfg = _cfg([_env("stage", trigger="auto")])
    with pytest.raises(SystemExit):
        with patch("lanegate.promote.pending_commits", return_value=_pending("abc123 some commit")):
            cmd_promote("stage", cfg, tmp_path)


def test_promote_unknown_env_exits(tmp_path):
    cfg = _cfg([_env("staging")])
    with pytest.raises(SystemExit):
        cmd_promote("nonexistent", cfg, tmp_path)


def test_promote_nothing_to_do(tmp_path, capsys):
    cfg = _cfg([_env("staging")])
    wt = tmp_path / "worktrees" / "staging"
    wt.mkdir(parents=True)
    with patch("lanegate.promote.pending_commits", return_value=_pending()):
        with patch("lanegate.worktree.worktree_path", return_value=wt):
            cmd_promote("staging", cfg, tmp_path)
    out = capsys.readouterr().out
    assert "already up to date" in out


def test_promote_guard_blocks(tmp_path):
    """guard_script failure (ConfigError or CalledProcessError) blocks the promote."""
    cfg = _cfg([_env("staging", guard=["scripts/guard.sh"])])
    with patch(
        "lanegate.promote.run_hook", side_effect=subprocess.CalledProcessError(1, ["scripts/guard.sh"])
    ):
        with patch("lanegate.promote.pending_commits", return_value=_pending("abc fix")):
            with pytest.raises(SystemExit):
                cmd_promote("staging", cfg, tmp_path)


def test_promote_pre_promote_blocks(tmp_path):
    """pre_promote failure blocks the promote."""
    cfg = _cfg([_env("staging", pre=["scripts/tests.sh"])])
    with patch(
        "lanegate.promote.run_hook", side_effect=subprocess.CalledProcessError(1, ["scripts/tests.sh"])
    ):
        with patch("lanegate.promote.pending_commits", return_value=_pending("abc fix")):
            with pytest.raises(SystemExit):
                cmd_promote("staging", cfg, tmp_path)


def test_promote_post_promote_runs_after_sync(tmp_path):
    """post_promote hook runs after a successful sync."""
    cfg = _cfg([_env("staging", post=["scripts/restart.sh"])])

    wt = tmp_path / "worktrees" / "staging"
    wt.mkdir(parents=True)

    def mock_run(args, **kwargs):
        return MagicMock(returncode=0, stdout="Fast-forward\n1 file changed")

    with patch("lanegate.promote.pending_commits", return_value=_pending("abc fix")):
        with patch("lanegate.promote.run_hook") as mock_hook:
            with patch("lanegate.promote.subprocess.run", side_effect=mock_run):
                with patch("lanegate.worktree.worktree_path", return_value=wt):
                    cmd_promote("staging", cfg, tmp_path)
    mock_hook.assert_called_once_with(["scripts/restart.sh"], tmp_path, "post_promote")


def test_promote_post_promote_skipped_when_nothing_moved(tmp_path):
    """post_promote does not run when there's nothing to promote."""
    cfg = _cfg([_env("staging", post=["scripts/restart.sh"])])
    wt = tmp_path / "worktrees" / "staging"
    wt.mkdir(parents=True)
    with patch("lanegate.promote.pending_commits", return_value=_pending()):
        with patch("lanegate.promote.run_hook") as mock_hook:
            with patch("lanegate.worktree.worktree_path", return_value=wt):
                cmd_promote("staging", cfg, tmp_path)
    mock_hook.assert_not_called()


def test_promote_routes_through_run_hook_not_shell(tmp_path):
    """cmd_promote must call run_hook (shell=False path) — never _run_script or shell=True."""
    cfg = _cfg([_env("staging", guard=["./scripts/guard.sh"], pre=["./scripts/pre.sh"])])
    hook_calls = []

    def record_hook(argv, repo_root, label, **kwargs):
        hook_calls.append((argv, label))

    wt = tmp_path / "worktrees" / "staging"
    wt.mkdir(parents=True)

    def mock_run(args, **kwargs):
        # Ensure subprocess.run is never called with shell=True from promote.py
        assert not kwargs.get("shell"), f"shell=True detected in {args}"
        return MagicMock(returncode=0, stdout="Fast-forward")

    with patch("lanegate.promote.run_hook", side_effect=record_hook):
        with patch("lanegate.promote.pending_commits", return_value=_pending("abc fix")):
            with patch("lanegate.promote.subprocess.run", side_effect=mock_run):
                with patch("lanegate.worktree.worktree_path", return_value=wt):
                    cmd_promote("staging", cfg, tmp_path)

    labels = [label for _, label in hook_calls]
    assert "guard_script" in labels
    assert "pre_promote" in labels


def test_promote_guard_allowlist_violation_blocks(tmp_path):
    """A guard_script with a non-allowlisted executable must block the promote."""
    cfg = _cfg([_env("staging", guard=["rm", "-rf", "/tmp/test"])])
    with patch("lanegate.promote.pending_commits", return_value=_pending("abc fix")):
        with pytest.raises(SystemExit):
            cmd_promote("staging", cfg, tmp_path)


# --- _run_promotion tests ---


def test_run_promotion_returns_true_on_success(tmp_path):
    """_run_promotion returns True when sync succeeds with no hooks."""
    env = _env("staging")
    cfg = _cfg([env])

    wt = tmp_path / "worktrees" / "staging"
    wt.mkdir(parents=True)

    with patch("lanegate.promote.pending_commits", return_value=_pending("abc fix")):
        with patch("lanegate.promote.subprocess.run", return_value=MagicMock(returncode=0, stdout="")):
            with patch("lanegate.worktree.worktree_path", return_value=wt):
                result = _run_promotion(env, cfg, tmp_path)

    assert result is True


def test_run_promotion_returns_false_on_guard_failure(tmp_path):
    """_run_promotion returns False (does not exit) when guard_script fails."""
    env = _env("staging", guard=["scripts/guard.sh"])
    cfg = _cfg([env])

    with patch(
        "lanegate.promote.run_hook",
        side_effect=subprocess.CalledProcessError(1, ["scripts/guard.sh"]),
    ):
        with patch("lanegate.promote.pending_commits", return_value=_pending("abc fix")):
            result = _run_promotion(env, cfg, tmp_path)

    assert result is False


def test_run_promotion_returns_false_on_pre_promote_failure(tmp_path):
    """_run_promotion returns False when pre_promote fails."""
    env = _env("staging", pre=["scripts/tests.sh"])
    cfg = _cfg([env])

    with patch(
        "lanegate.promote.run_hook",
        side_effect=subprocess.CalledProcessError(1, ["scripts/tests.sh"]),
    ):
        with patch("lanegate.promote.pending_commits", return_value=_pending("abc fix")):
            result = _run_promotion(env, cfg, tmp_path)

    assert result is False


def test_first_promotion_bootstraps_branch_before_pending_evaluation(tmp_path):
    """A first promotion creates its environment branch from the source first."""
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
    (tmp_path / "app.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)

    env = _env("stage")
    assert _run_promotion(env, _cfg([env]), tmp_path) is True

    branch = subprocess.run(
        ["git", "rev-parse", "--verify", "refs/heads/stage"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    source = subprocess.run(
        ["git", "rev-parse", "main"], cwd=tmp_path, capture_output=True, text=True, check=True
    )
    assert branch.returncode == 0
    assert branch.stdout.strip() == source.stdout.strip()


def test_run_promotion_reports_pending_query_failure(tmp_path, capsys):
    """A bad range is a failed promotion, never the successful no-op path."""
    env = _env("stage", from_="missing-source")
    wt = tmp_path / "worktrees" / "stage"
    wt.mkdir(parents=True)

    with patch(
        "lanegate.promote.pending_commits",
        return_value=PendingCommits([], "git log stage..missing-source failed (exit 128): bad revision"),
    ):
        with patch("lanegate.worktree.worktree_path", return_value=wt):
            assert _run_promotion(env, _cfg([env]), tmp_path) is False

    captured = capsys.readouterr()
    assert "unable to determine pending commits" in captured.err
    assert "already up to date" not in captured.out


def test_cmd_promote_still_refuses_trigger_auto(tmp_path):
    """cmd_promote must still exit for trigger: auto environments even after refactor."""
    cfg = _cfg([_env("paper", trigger="auto")])
    with pytest.raises(SystemExit):
        cmd_promote("paper", cfg, tmp_path)


def test_auto_promote_environments_skips_non_auto(tmp_path):
    """_auto_promote_environments does not promote manual-trigger environments."""
    cfg = _cfg([_env("staging", trigger="manual")])

    with patch("lanegate.promote._run_promotion") as mock_rp:
        _auto_promote_environments(cfg, tmp_path, "main")
        mock_rp.assert_not_called()


def test_auto_promote_environments_skips_non_matching_from(tmp_path):
    """_auto_promote_environments skips auto envs whose from does not match."""
    env = _env("staging", trigger="auto", from_="develop")
    cfg = _cfg([env])

    with patch("lanegate.promote._run_promotion") as mock_rp:
        _auto_promote_environments(cfg, tmp_path, "main")
        mock_rp.assert_not_called()


def test_auto_promote_environments_promotes_matching_auto(tmp_path):
    """_auto_promote_environments calls _run_promotion for matching auto env."""
    env = _env("paper", trigger="auto", from_="main")
    cfg = _cfg([env])

    with patch("lanegate.promote._run_promotion", return_value=True) as mock_rp:
        _auto_promote_environments(cfg, tmp_path, "main")
        mock_rp.assert_called_once_with(env, cfg, tmp_path)


def test_auto_promote_environments_warns_on_failure(tmp_path, capsys):
    """_auto_promote_environments prints a warning but does not raise when _run_promotion fails."""
    env = _env("paper", trigger="auto", from_="main")
    cfg = _cfg([env])

    with patch("lanegate.promote._run_promotion", return_value=False):
        _auto_promote_environments(cfg, tmp_path, "main")  # must not raise

    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "paper" in captured.err


def test_auto_promote_environments_warns_on_exception(tmp_path, capsys):
    """_auto_promote_environments catches exceptions from _run_promotion and warns."""
    env = _env("paper", trigger="auto", from_="main")
    cfg = _cfg([env])

    with patch("lanegate.promote._run_promotion", side_effect=RuntimeError("boom")):
        _auto_promote_environments(cfg, tmp_path, "main")  # must not raise

    captured = capsys.readouterr()
    assert "WARNING" in captured.err
