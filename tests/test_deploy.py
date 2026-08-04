"""
tests/test_deploy.py — SEC-03 (TICK-041)

Covers:
- String hook values rejected at config load time
- Shell metacharacters in list args are NOT interpreted by the shell
- Allowlisted command runs successfully
- Non-allowlisted command raises ConfigError
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lanegate.config import ConfigError, load_config, validate_hook
from lanegate.deploy import _is_allowed, run_hook

# ---------------------------------------------------------------------------
# validate_hook — called at config load time
# ---------------------------------------------------------------------------


class TestValidateHook:
    def test_string_hook_is_rejected(self):
        """A bare string hook must raise ConfigError with a clear message."""
        with pytest.raises(ConfigError, match="must be a YAML list, not a string"):
            validate_hook("./scripts/deploy.sh --env=prod", "post_promote")

    def test_string_hook_error_includes_example(self):
        """The error message must include a YAML list example."""
        with pytest.raises(ConfigError, match="- ./scripts/notify.sh"):
            validate_hook("./scripts/notify.sh", "post_deploy")

    def test_integer_hook_is_rejected(self):
        with pytest.raises(ConfigError, match="must be a list of strings"):
            validate_hook(42, "pre_promote")

    def test_none_hook_is_rejected(self):
        with pytest.raises(ConfigError, match="must be a list of strings"):
            validate_hook(None, "pre_promote")

    def test_valid_list_hook_is_returned(self):
        result = validate_hook(["./scripts/deploy.sh", "--env=prod"], "post_promote")
        assert result == ["./scripts/deploy.sh", "--env=prod"]

    def test_list_items_are_coerced_to_str(self):
        """Non-string list items (e.g. from YAML int values) are coerced."""
        result = validate_hook(["python", "scripts/run.py", 42], "pre_promote")
        assert result == ["python", "scripts/run.py", "42"]


# ---------------------------------------------------------------------------
# load_config — string hooks rejected during environment normalization
# ---------------------------------------------------------------------------


class TestLoadConfigHookValidation:
    def _write_config(self, tmp_path: Path, hook_type: str, hook_value) -> Path:
        import yaml

        config = {
            "ticket_prefix": "TICK",
            "tickets_dir": "tickets",
            "worktrees_dir": "worktrees",
            "executor": "claude",
            "max_parallel": 2,
            "environments": [
                {
                    "name": "staging",
                    "branch": "staging",
                    "trigger": "manual",
                    hook_type: hook_value,
                }
            ],
        }
        config_path = tmp_path / ".lanegate.yml"
        config_path.write_text(yaml.dump(config))
        return tmp_path

    def test_string_pre_promote_rejected_at_load(self, tmp_path):
        repo_root = self._write_config(tmp_path, "pre_promote", "./scripts/test.sh --fast")
        with pytest.raises(ConfigError, match="must be a YAML list"):
            load_config(repo_root)

    def test_string_post_promote_rejected_at_load(self, tmp_path):
        repo_root = self._write_config(tmp_path, "post_promote", "./scripts/notify.sh")
        with pytest.raises(ConfigError, match="must be a YAML list"):
            load_config(repo_root)

    def test_list_pre_promote_accepted_at_load(self, tmp_path):
        repo_root = self._write_config(tmp_path, "pre_promote", ["./scripts/test.sh", "--fast"])
        cfg = load_config(repo_root)
        env = cfg["environments"][0]
        assert env["pre_promote"] == ["./scripts/test.sh", "--fast"]


# ---------------------------------------------------------------------------
# _is_allowed — allowlist enforcement
# ---------------------------------------------------------------------------


class TestIsAllowed:
    def test_bare_name_in_default_allowlist(self):
        for name in ("bash", "python", "make", "docker", "git"):
            assert _is_allowed(name), f"{name} should be allowed"

    def test_bare_name_not_in_allowlist(self):
        assert not _is_allowed("rm")
        assert not _is_allowed("curl_custom_wrapper")
        assert not _is_allowed("my_binary")

    def test_script_with_allowed_extension_permitted(self):
        assert _is_allowed("./scripts/deploy.sh")
        assert _is_allowed("scripts/notify.py")
        assert _is_allowed("scripts/run.bash")
        assert _is_allowed("C:\\scripts\\deploy.bat")

    def test_script_without_allowed_extension_rejected(self):
        assert not _is_allowed("./bin/my_tool")
        assert not _is_allowed("./scripts/deploy.exe")

    def test_extra_allowlist_extends_default(self):
        assert _is_allowed("my_deploy_tool", extra_allowlist=frozenset({"my_deploy_tool"}))

    def test_path_with_allowed_basename(self):
        assert _is_allowed("/usr/local/bin/python")
        assert _is_allowed("/usr/bin/make")


# ---------------------------------------------------------------------------
# run_hook — safe subprocess execution
# ---------------------------------------------------------------------------


class TestRunHook:
    def test_allowlisted_command_runs_without_shell(self, tmp_path):
        """An allowlisted hook must be invoked with shell=False."""
        mock_result = MagicMock(returncode=0)
        with patch("lanegate.deploy.subprocess.run", return_value=mock_result) as mock_run:
            run_hook(["python", "scripts/check.py"], tmp_path, "pre_promote")
        mock_run.assert_called_once_with(
            ["python", "scripts/check.py"],
            shell=False,
            check=True,
            cwd=tmp_path,
        )

    def test_script_extension_hook_runs_without_shell(self, tmp_path):
        """A hook with a .sh extension (path form) runs via shell=False."""
        mock_result = MagicMock(returncode=0)
        with patch("lanegate.deploy.subprocess.run", return_value=mock_result) as mock_run:
            run_hook(["./scripts/deploy.sh", "--env=prod"], tmp_path, "post_promote")
        mock_run.assert_called_once_with(
            ["./scripts/deploy.sh", "--env=prod"],
            shell=False,
            check=True,
            cwd=tmp_path,
        )

    def test_non_allowlisted_command_raises_config_error(self, tmp_path):
        """A hook whose first argv element is not in the allowlist must raise ConfigError."""
        with pytest.raises(ConfigError, match="not in the permitted allowlist"):
            run_hook(["rm", "-rf", "/tmp/junk"], tmp_path, "post_promote")

    def test_shell_metacharacters_not_interpreted(self, tmp_path):
        """Shell metacharacters in list args must NOT be shell-expanded."""
        captured_calls = []

        def fake_run(argv, **kwargs):
            captured_calls.append((argv, kwargs))
            return MagicMock(returncode=0)

        with patch("lanegate.deploy.subprocess.run", side_effect=fake_run):
            run_hook(
                ["./scripts/notify.sh", "$(rm -rf /)", "; cat /etc/passwd"],
                tmp_path,
                "post_promote",
            )

        assert len(captured_calls) == 1
        argv, kwargs = captured_calls[0]
        # shell must be False — metacharacters stay as literal strings
        assert kwargs["shell"] is False
        assert argv[1] == "$(rm -rf /)"
        assert argv[2] == "; cat /etc/passwd"

    def test_empty_hook_argv_raises_config_error(self, tmp_path):
        with pytest.raises(ConfigError, match="empty list"):
            run_hook([], tmp_path, "pre_promote")

    def test_failed_hook_raises_called_process_error(self, tmp_path):
        """A hook that exits non-zero must propagate CalledProcessError."""
        with patch(
            "lanegate.deploy.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, ["./scripts/fail.sh"]),
        ):
            with pytest.raises(subprocess.CalledProcessError):
                run_hook(["./scripts/fail.sh"], tmp_path, "post_promote")
