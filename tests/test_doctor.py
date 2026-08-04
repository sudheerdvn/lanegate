"""Tests for lanegate.doctor — dependency checker."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from lanegate.doctor import _TOOLS, _get_version, _has_headless_permission_config, _setup_bundle, cmd_doctor

# ---------------------------------------------------------------------------
# Isolation: cmd_doctor() with no explicit tickets_dir falls back to
# find_repo_root()/cfg's default and scans a real directory on disk. Most
# tests below call cmd_doctor() with a cfg that has no "tickets_dir" key
# (they're exercising tool-presence output, not ticket parsing), so without
# this they'd silently depend on being run from inside a real repo checkout
# whose ambient tickets dir happens to exist under the current APP_NAME.
# Pin find_repo_root() to an empty, isolated tmp_path instead.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_repo_root(tmp_path, monkeypatch):
    (tmp_path / ".lanegate" / "tickets").mkdir(parents=True)
    monkeypatch.setattr("lanegate.doctor.find_repo_root", lambda: tmp_path)


# ---------------------------------------------------------------------------
# Helper: build a which() side_effect that returns a fake path for given names
# ---------------------------------------------------------------------------


def _which_factory(*present: str):
    """Return a side_effect function for shutil.which.

    Binaries listed in *present* resolve to '/usr/bin/<binary>'; all others
    return None (not found).
    """

    def _which(binary: str):
        return f"/usr/bin/{binary}" if binary in present else None

    return _which


def test_doctor_reports_quarantined_ticket_and_fails(tmp_path, capsys):
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    (tickets_dir / "TICK-999.md").write_text(
        "---\nid: TICK-999\ntitle: broken\nstatus: open\n  review_summary: bad\n---\n"
    )
    cfg = {
        "tickets_dir": "tickets",
        "ticket_prefix": "TICK",
        "executor": "",
        "safeguards": {},
    }
    all_binaries = [tool.binary for tool in _TOOLS]

    with (
        patch("lanegate.doctor.find_repo_root", return_value=tmp_path),
        patch("shutil.which", side_effect=_which_factory(*all_binaries)),
        patch("lanegate.doctor._get_version", return_value="(v1.0)"),
        patch("lanegate.doctor.detect_test_runner_safeguards", return_value=[]),
    ):
        rc = cmd_doctor(cfg=cfg)

    output = capsys.readouterr().out
    assert rc == 1
    assert "TICK-999.md" in output
    assert "could not parse frontmatter" in output
    assert "failed to parse and are quarantined" in output


# ---------------------------------------------------------------------------
# 1. All tools present → exit 0, output contains ✓ lines
# ---------------------------------------------------------------------------


class TestAllPresent:
    def test_exit_code_zero(self, capsys):
        all_binaries = [t.binary for t in _TOOLS]
        with (
            patch("shutil.which", side_effect=_which_factory(*all_binaries)),
            patch("lanegate.doctor._get_version", return_value="(v1.0)"),
        ):
            rc = cmd_doctor()
        assert rc == 0

    def test_checkmark_in_output(self, capsys):
        all_binaries = [t.binary for t in _TOOLS]
        with (
            patch("shutil.which", side_effect=_which_factory(*all_binaries)),
            patch("lanegate.doctor._get_version", return_value="(v1.0)"),
        ):
            cmd_doctor()
        out = capsys.readouterr().out
        assert "✓" in out

    def test_no_cross_marks(self, capsys):
        all_binaries = [t.binary for t in _TOOLS]
        with (
            patch("shutil.which", side_effect=_which_factory(*all_binaries)),
            patch("lanegate.doctor._get_version", return_value="(v1.0)"),
        ):
            cmd_doctor()
        out = capsys.readouterr().out
        assert "✗" not in out

    def test_required_tools_present_message(self, capsys):
        all_binaries = [t.binary for t in _TOOLS]
        with (
            patch("shutil.which", side_effect=_which_factory(*all_binaries)),
            patch("lanegate.doctor._get_version", return_value="(v1.0)"),
        ):
            cmd_doctor()
        out = capsys.readouterr().out
        assert "Required runtime tools present" in out


# ---------------------------------------------------------------------------
# 2. Required tool (git) missing → exit 1, ✗ line with install instructions
# ---------------------------------------------------------------------------


class TestRequiredMissing:
    def test_exit_code_one(self, capsys):
        # git is required; remove it from the present set
        present = [t.binary for t in _TOOLS if t.binary != "git"]
        with (
            patch("shutil.which", side_effect=_which_factory(*present)),
            patch("lanegate.doctor._get_version", return_value="(v1.0)"),
        ):
            rc = cmd_doctor()
        assert rc == 1

    def test_cross_mark_in_output(self, capsys):
        present = [t.binary for t in _TOOLS if t.binary != "git"]
        with (
            patch("shutil.which", side_effect=_which_factory(*present)),
            patch("lanegate.doctor._get_version", return_value="(v1.0)"),
        ):
            cmd_doctor()
        out = capsys.readouterr().out
        assert "✗" in out

    def test_install_instructions_in_output(self, capsys):
        present = [t.binary for t in _TOOLS if t.binary != "git"]
        with (
            patch("shutil.which", side_effect=_which_factory(*present)),
            patch("platform.system", return_value="Linux"),
            patch("lanegate.doctor._get_version", return_value="(v1.0)"),
        ):
            cmd_doctor()
        out = capsys.readouterr().out
        # Should mention an install command for git
        assert "git" in out
        assert "apt install git" in out

    def test_error_message_present(self, capsys):
        present = [t.binary for t in _TOOLS if t.binary != "git"]
        with (
            patch("shutil.which", side_effect=_which_factory(*present)),
            patch("lanegate.doctor._get_version", return_value="(v1.0)"),
        ):
            cmd_doctor()
        out = capsys.readouterr().out
        assert "required tools missing" in out

    def test_success_message_absent_when_required_missing(self, capsys):
        present = [t.binary for t in _TOOLS if t.binary != "git"]
        with (
            patch("shutil.which", side_effect=_which_factory(*present)),
            patch("lanegate.doctor._get_version", return_value="(v1.0)"),
        ):
            cmd_doctor()
        out = capsys.readouterr().out
        assert "Required runtime tools present" not in out


# ---------------------------------------------------------------------------
# 3. Optional tool missing → exit 0, – line with install instructions
# ---------------------------------------------------------------------------


class TestOptionalMissing:
    def test_exit_code_zero(self, capsys):
        # Only git present; all optional tools absent
        with (
            patch("shutil.which", side_effect=_which_factory("git", "apt")),
            patch("lanegate.doctor._get_version", return_value="(v1.0)"),
        ):
            rc = cmd_doctor()
        assert rc == 0

    def test_dash_marker_for_optional(self, capsys):
        with (
            patch("shutil.which", side_effect=_which_factory("git")),
            patch("lanegate.doctor._get_version", return_value="(v1.0)"),
        ):
            cmd_doctor()
        out = capsys.readouterr().out
        assert "–" in out

    def test_install_hint_for_optional(self, capsys):
        with (
            patch("shutil.which", side_effect=_which_factory("git", "apt")),
            patch("platform.system", return_value="Linux"),
            patch("lanegate.doctor._get_version", return_value="(v1.0)"),
        ):
            cmd_doctor()
        out = capsys.readouterr().out
        # ripgrep is an optional runtime tool
        assert "ripgrep" in out.lower() or "rg" in out

    def test_optional_summary_message(self, capsys):
        with (
            patch("shutil.which", side_effect=_which_factory("git")),
            patch("lanegate.doctor._get_version", return_value="(v1.0)"),
        ):
            cmd_doctor()
        out = capsys.readouterr().out
        assert "analysis coverage degraded" in out
        assert "Optional analysis tools not installed" in out

    def test_missing_gitleaks_warns_secret_scanning_skipped(self, capsys):
        present = [t.binary for t in _TOOLS if t.binary != "gitleaks"]
        with (
            patch("shutil.which", side_effect=_which_factory(*present)),
            patch("lanegate.doctor._get_version", return_value="(v1.0)"),
        ):
            rc = cmd_doctor()
        assert rc == 0
        out = capsys.readouterr().out
        assert "analysis coverage degraded" in out
        assert "gitleaks" in out
        assert "secret scanning will be skipped" in out

    def test_installed_but_unhealthy_analysis_tool_degrades_coverage(self, capsys):
        all_binaries = [t.binary for t in _TOOLS]

        def version(binary: str, *_args) -> str:
            return "" if binary == "semgrep" else "(v1.0)"

        with (
            patch("shutil.which", side_effect=_which_factory(*all_binaries)),
            patch("lanegate.doctor._get_version", side_effect=version),
        ):
            rc = cmd_doctor()
        assert rc == 0
        out = capsys.readouterr().out
        assert "!  semgrep" in out
        assert "installed but not runnable: semgrep" in out
        assert "analysis coverage degraded" in out

    def test_all_analysis_tools_present_reports_full_coverage(self, capsys):
        all_binaries = [t.binary for t in _TOOLS]
        with (
            patch("shutil.which", side_effect=_which_factory(*all_binaries)),
            patch("lanegate.doctor._get_version", return_value="(v1.0)"),
        ):
            rc = cmd_doctor()
        assert rc == 0
        out = capsys.readouterr().out
        assert "Analysis coverage: all known optional analysis tools are available." in out
        assert "analysis coverage degraded" not in out


# ---------------------------------------------------------------------------
# 4. _get_version — graceful handling of errors
# ---------------------------------------------------------------------------


class TestGetVersion:
    def test_returns_empty_on_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["git"], 3)):
            result = _get_version("git")
        assert result == ""

    def test_returns_empty_on_oserror(self):
        with patch("subprocess.run", side_effect=OSError("not found")):
            result = _get_version("nonexistent-tool")
        assert result == ""

    def test_returns_empty_on_generic_exception(self):
        with patch("subprocess.run", side_effect=RuntimeError("unexpected")):
            result = _get_version("tool")
        assert result == ""

    def test_returns_version_string_on_success(self):
        mock_result = MagicMock()
        mock_result.stdout = "git version 2.43.0\n"
        mock_result.stderr = ""
        with patch("subprocess.run", return_value=mock_result):
            result = _get_version("git")
        assert result == "(git version 2.43.0)"

    def test_accepts_custom_version_args(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "gitleaks 8.18.0\n"
        mock_result.stderr = ""
        with patch("subprocess.run", return_value=mock_result) as run:
            result = _get_version("gitleaks", ("version",))
        assert result == "(gitleaks 8.18.0)"
        run.assert_called_once_with(
            ["gitleaks", "version"], capture_output=True, text=True, encoding="utf-8", timeout=3
        )

    def test_truncates_long_version_strings(self):
        long_line = "x" * 100
        mock_result = MagicMock()
        mock_result.stdout = long_line + "\n"
        mock_result.stderr = ""
        with patch("subprocess.run", return_value=mock_result):
            result = _get_version("tool")
        # Should be truncated to 40 chars inside parens
        assert len(result) <= len("(") + 40 + len(")")

    def test_falls_back_to_stderr(self):
        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.stderr = "semgrep 1.50.0\n"
        with patch("subprocess.run", return_value=mock_result):
            result = _get_version("semgrep")
        assert "semgrep 1.50.0" in result

    def test_returns_empty_when_no_output(self):
        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.stderr = ""
        with patch("subprocess.run", return_value=mock_result):
            result = _get_version("tool")
        assert result == ""


# ---------------------------------------------------------------------------
# 5. Platform-specific n/a handling for sandbox tools
# ---------------------------------------------------------------------------


class TestPlatformNA:
    def test_bwrap_na_on_macos(self, capsys):
        all_binaries = [t.binary for t in _TOOLS]
        with (
            patch("shutil.which", side_effect=_which_factory(*all_binaries)),
            patch("platform.system", return_value="Darwin"),
            patch("lanegate.doctor._get_version", return_value="(v1.0)"),
        ):
            cmd_doctor()
        out = capsys.readouterr().out
        # On Darwin, bwrap should show as n/a, not ✓
        lines = [line for line in out.splitlines() if "bwrap" in line]
        assert lines, "Expected a bwrap line in output"
        assert "n/a" in lines[0].lower()

    def test_sandbox_exec_na_on_linux(self, capsys):
        all_binaries = [t.binary for t in _TOOLS]
        with (
            patch("shutil.which", side_effect=_which_factory(*all_binaries)),
            patch("platform.system", return_value="Linux"),
            patch("lanegate.doctor._get_version", return_value="(v1.0)"),
        ):
            cmd_doctor()
        out = capsys.readouterr().out
        lines = [line for line in out.splitlines() if "sandbox-exec" in line]
        assert lines, "Expected a sandbox-exec line in output"
        assert "n/a" in lines[0].lower()

    def test_sandbox_exec_builtin_on_macos(self, capsys):
        all_binaries = [t.binary for t in _TOOLS]
        with (
            patch("shutil.which", side_effect=_which_factory(*all_binaries)),
            patch("platform.system", return_value="Darwin"),
            patch("lanegate.doctor._get_version", return_value="(v1.0)"),
        ):
            cmd_doctor()
        out = capsys.readouterr().out
        # On Darwin, sandbox-exec should show ✓ with "(built-in)" note.
        # The line looks like:  ✓  sandbox-exec         (built-in)
        # We filter for lines containing both ✓ and "sandbox-exec".
        lines = [line for line in out.splitlines() if "✓" in line and "sandbox-exec" in line]
        assert lines, "Expected a ✓ sandbox-exec line on Darwin"

    def test_sandbox_note_says_detection_is_not_enforcement(self, capsys):
        all_binaries = [t.binary for t in _TOOLS]
        with (
            patch("shutil.which", side_effect=_which_factory(*all_binaries)),
            patch("platform.system", return_value="Linux"),
            patch("lanegate.doctor._get_version", return_value="(v1.0)"),
        ):
            cmd_doctor()
        out = capsys.readouterr().out
        assert "sandbox tool detection is availability only" in out
        assert "no OS sandbox is applied" in out


# ---------------------------------------------------------------------------
# 6. pip-audit summary hint
# ---------------------------------------------------------------------------


class TestPipAuditSummary:
    def test_pip_install_hint_when_only_pip_tools_missing(self, capsys):
        # Only git + non-pip optional tools present; bandit and pip-audit absent
        present = ["git", "rg", "semgrep", "gitleaks", "npm", "bwrap", "sandbox-exec"]
        with (
            patch("shutil.which", side_effect=_which_factory(*present)),
            patch("lanegate.doctor._get_version", return_value="(v1.0)"),
        ):
            rc = cmd_doctor()
        assert rc == 0
        out = capsys.readouterr().out
        assert "python3 -m pip install -e '.[security]'" in out
        assert "prefer a venv" in out

    def test_mixed_missing_shows_both_hints(self, capsys):
        # git present; some analysis tools missing including both pip and non-pip
        present = ["git"]
        with (
            patch("shutil.which", side_effect=_which_factory(*present)),
            patch("lanegate.doctor._get_version", return_value="(v1.0)"),
        ):
            rc = cmd_doctor()
        assert rc == 0
        out = capsys.readouterr().out
        assert "Python tools:" in out
        assert "Other tools:" in out


# ---------------------------------------------------------------------------
# 7. Setup bundle command shape
# ---------------------------------------------------------------------------


class TestSetupBundle:
    def test_ubuntu_debian_bundle_separates_system_tools_from_python_analyzers(self):
        bundle = _setup_bundle("Linux", has_apt=True)

        assert bundle.label == "Ubuntu/Debian source checkout"
        assert any("apt install" in command for command in bundle.system_tools)
        assert any("gitleaks" in command for command in bundle.system_tools)
        assert any("bubblewrap" in command for command in bundle.system_tools)
        assert bundle.python_analyzers == ["python3 -m pip install -e '.[security]'"]
        assert any(
            "&& python3 -m pip install -e '.[security]'" in command
            for command in bundle.all_in_one
        )
        assert any("sandbox-exec is macOS-only" in note for note in bundle.notes)

    def test_generic_linux_bundle_label_when_apt_absent(self):
        bundle = _setup_bundle("Linux", has_apt=False)

        assert bundle.label == "Linux source checkout"

    def test_macos_bundle_notes_sandbox_exec_is_builtin(self):
        bundle = _setup_bundle("Darwin")

        assert any("brew install" in command for command in bundle.system_tools)
        assert any("gitleaks" in command for command in bundle.system_tools)
        assert any(
            "python3 -m pip install -e '.[security]'" in command
            for command in bundle.all_in_one
        )
        assert any("sandbox-exec is built into macOS" in note for note in bundle.notes)

    def test_doctor_prints_split_setup_bundle(self, capsys):
        with (
            patch("shutil.which", side_effect=_which_factory("git", "apt")),
            patch("platform.system", return_value="Linux"),
            patch("lanegate.doctor._get_version", return_value="(v1.0)"),
        ):
            rc = cmd_doctor()
        assert rc == 0
        out = capsys.readouterr().out
        assert "Setup bundle (Ubuntu/Debian source checkout)" in out
        assert "System tools:" in out
        assert "Python analyzers:" in out
        assert "All-in-one:" in out
        assert "Python extras install Python analyzers only." in out
        assert "OS tools such as gitleaks/bwrap need apt, brew, winget, or similar." in out


# ---------------------------------------------------------------------------
# reviewer explicitly resolving to the same driver as implement (combined
# mode collapse) is flagged as a potential issue
# ---------------------------------------------------------------------------


class TestReviewerCombinedModeCollapse:
    def test_doctor_detects_combined_mode_collapse(self, capsys):
        all_binaries = [t.binary for t in _TOOLS]
        cfg = {"executor": "claude", "reviewer": "claude", "executors": {}, "safeguards": {}}
        with (
            patch("shutil.which", side_effect=_which_factory(*all_binaries)),
            patch("lanegate.doctor._get_version", return_value="(v1.0)"),
            patch("lanegate.doctor.detect_test_runner_safeguards", return_value=[]),
        ):
            cmd_doctor(cfg=cfg)
        out = capsys.readouterr().out
        assert "reviewer resolves identically to the implement executor" in out

    def test_doctor_no_warning_when_reviewer_differs(self, capsys):
        all_binaries = [t.binary for t in _TOOLS]
        cfg = {
            "executor": "claude",
            "reviewer": "aider",
            "executors": {"claude": {"flags": ["--dangerously-skip-permissions"]}},
            "safeguards": {},
        }
        with (
            patch("shutil.which", side_effect=_which_factory(*all_binaries)),
            patch("lanegate.doctor._get_version", return_value="(v1.0)"),
            patch("lanegate.doctor.detect_test_runner_safeguards", return_value=[]),
        ):
            cmd_doctor(cfg=cfg)
        out = capsys.readouterr().out
        assert "reviewer resolves identically" not in out

    def test_doctor_no_warning_when_reviewer_absent(self, capsys):
        all_binaries = [t.binary for t in _TOOLS]
        cfg = {
            "executor": "claude",
            "executors": {"claude": {"flags": ["--dangerously-skip-permissions"]}},
            "safeguards": {},
        }
        with (
            patch("shutil.which", side_effect=_which_factory(*all_binaries)),
            patch("lanegate.doctor._get_version", return_value="(v1.0)"),
            patch("lanegate.doctor.detect_test_runner_safeguards", return_value=[]),
        ):
            cmd_doctor(cfg=cfg)
        out = capsys.readouterr().out
        assert "reviewer resolves identically" not in out


# ---------------------------------------------------------------------------
# TICK-364: headless permission config — bypass flag, --allowedTools/
# --disallowedTools, and a non-interactive --permission-mode are all valid;
# only their absence should warn, and the scoped form is recommended first.
# ---------------------------------------------------------------------------


class TestHasHeadlessPermissionConfig:
    def test_bypass_flag_is_valid(self):
        assert _has_headless_permission_config(["--dangerously-skip-permissions"]) is True

    def test_allowed_tools_is_valid(self):
        assert _has_headless_permission_config(["--allowedTools", "Bash,Edit,Write"]) is True

    def test_disallowed_tools_is_valid(self):
        assert _has_headless_permission_config(["--disallowedTools", "WebFetch"]) is True

    def test_non_interactive_permission_mode_is_valid(self):
        assert _has_headless_permission_config(["--permission-mode", "acceptEdits"]) is True
        assert _has_headless_permission_config(["--permission-mode", "bypassPermissions"]) is True

    def test_interactive_permission_mode_is_not_valid(self):
        """'manual' and 'plan' are confirmation-first and still hang headless."""
        assert _has_headless_permission_config(["--permission-mode", "manual"]) is False
        assert _has_headless_permission_config(["--permission-mode", "plan"]) is False

    def test_no_flags_is_not_valid(self):
        assert _has_headless_permission_config([]) is False


class TestDoctorHeadlessPermissionWarning:
    def _cfg(self, flags):
        return {
            "executor": "claude",
            "executors": {"claude": {"flags": flags}},
            "safeguards": {},
        }

    def _run(self, cfg, capsys):
        all_binaries = [t.binary for t in _TOOLS]
        with (
            patch("shutil.which", side_effect=_which_factory(*all_binaries)),
            patch("lanegate.doctor._get_version", return_value="(v1.0)"),
            patch("lanegate.doctor.detect_test_runner_safeguards", return_value=[]),
        ):
            cmd_doctor(cfg=cfg)
        return capsys.readouterr().out

    def test_warns_when_no_headless_config_present(self, capsys):
        out = self._run(self._cfg([]), capsys)
        assert "Claude executor requires headless flags for orchestrate" in out

    def test_recommends_scoped_form_first(self, capsys):
        out = self._run(self._cfg([]), capsys)
        assert "Recommended — a scoped permission set" in out
        assert "--allowedTools" in out

    def test_no_warning_with_bypass_flag(self, capsys):
        out = self._run(self._cfg(["--dangerously-skip-permissions"]), capsys)
        assert "Claude executor requires headless flags" not in out

    def test_no_warning_with_allowed_tools(self, capsys):
        out = self._run(self._cfg(["--allowedTools", "Bash,Edit,Write,Read"]), capsys)
        assert "Claude executor requires headless flags" not in out

    def test_no_warning_with_non_interactive_permission_mode(self, capsys):
        out = self._run(self._cfg(["--permission-mode", "dontAsk"]), capsys)
        assert "Claude executor requires headless flags" not in out

    def test_still_warns_with_interactive_permission_mode(self, capsys):
        out = self._run(self._cfg(["--permission-mode", "manual"]), capsys)
        assert "Claude executor requires headless flags for orchestrate" in out
