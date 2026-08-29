"""Tests for config_init.py interactive initialization helpers."""

import json
import os
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

import pytest
import yaml

from lanegate.config import (
    CONFIG_FILENAME,
    ConfigError,
    _detect_existing_tickets_dir,
    _gitignore_entries,
    _update_gitignore,
    detect_test_runner_safeguards,
    interactive_init,
    load_config,
    suggested_safeguards_yaml,
)


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
        with mock.patch("lanegate.config_registry._registry_save"):
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
        with mock.patch("lanegate.config_registry._registry_save"):
            interactive_init(tmp_path, use_defaults=True)

        import yaml

        written = yaml.safe_load((tmp_path / CONFIG_FILENAME).read_text())
        flags = written["executors"]["claude"]["flags"]
        assert "--dangerously-skip-permissions" not in flags
        assert "--allowedTools" in flags

    def test_defaults_creates_directories(self, tmp_path):
        """Tickets and worktrees directories are created under .lanegate/."""
        with mock.patch("lanegate.config_registry._registry_save"):
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
        with mock.patch("sys.stdin") as mock_stdin, mock.patch("lanegate.config_registry._registry_save"):
            mock_stdin.isatty.return_value = False
            cfg = interactive_init(tmp_path)

        assert cfg is not None
        assert cfg["ticket_prefix"] == "TICK"
        assert cfg["executor"] == "claude"

    def test_registry_called_on_defaults(self, tmp_path):
        """registry_add is called after a successful --defaults init."""
        with mock.patch("lanegate.config_registry._registry_save") as mock_save:
            interactive_init(tmp_path, use_defaults=True)

        # _registry_save should have been called at least once (from registry_add)
        assert mock_save.called

    def test_defaults_returns_config_dict(self, tmp_path):
        """Return value is the config dict, not None."""
        with mock.patch("lanegate.config_registry._registry_save"):
            result = interactive_init(tmp_path, use_defaults=True)
        assert isinstance(result, dict)

    def test_force_interactive_prompts_even_in_non_tty(self, tmp_path):
        """force_interactive=True fires prompts even when stdin.isatty() is False."""
        inputs = iter(["", "", "", "", "", "", "", "", ""])  # accept all defaults
        with (
            mock.patch("sys.stdin") as mock_stdin,
            mock.patch("builtins.input", side_effect=lambda _="": next(inputs, "")),
            mock.patch("lanegate.config_registry._registry_save"),
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
            mock.patch("lanegate.config_registry._registry_save"),
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
            mock.patch("lanegate.config_registry._registry_save"),
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
            mock.patch("lanegate.config_registry._registry_save"),
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
            mock.patch("lanegate.config_registry._registry_save"),
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
            mock.patch("lanegate.config_registry._registry_save"),
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
            mock.patch("lanegate.config_registry._registry_save"),
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
            mock.patch("lanegate.config_registry._registry_save"),
        ):
            mock_stdin.isatty.return_value = False
            cfg = interactive_init(tmp_path, force_interactive=True)

        assert "reviewer" not in cfg
        assert "not a recognised reviewer" in capsys.readouterr().out

    def test_non_tty_without_force_prints_hint(self, tmp_path, capsys):
        """Non-TTY without force_interactive prints the --interactive hint to stderr."""
        with mock.patch("sys.stdin") as mock_stdin, mock.patch("lanegate.config_registry._registry_save"):
            mock_stdin.isatty.return_value = False
            interactive_init(tmp_path)
        err = capsys.readouterr().err
        assert "--interactive" in err

    def test_init_adds_gitignore_entries(self, tmp_path):
        """lanegate init appends .lanegate/ to .gitignore, but not .lanegate.yml
        itself -- a worktree only sees committed content, so an ignored config
        would leave the first ticket's worktree without one at all."""
        with mock.patch("lanegate.config_registry._registry_save"):
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
            mock.patch("lanegate.config_registry._registry_save"),
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

        with mock.patch("lanegate.config_registry._registry_save"):
            interactive_init(tmp_path, use_defaults=True)

        content = (tmp_path / ".gitignore").read_text()
        assert ".lanegate.yml" not in content
        assert "node_modules/" in content
        assert "*.log" in content
        assert ".lanegate/" in content

    def test_init_does_not_duplicate_gitignore_entries(self, tmp_path):
        """Running init twice does not produce duplicate .gitignore entries."""
        with mock.patch("lanegate.config_registry._registry_save"):
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
        with mock.patch("lanegate.config_registry._registry_save"):
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

        with mock.patch("lanegate.config_registry._registry_save"):
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

        with mock.patch("lanegate.config_registry._registry_save"):
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

        with mock.patch("lanegate.config_registry._registry_save"):
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
            mock.patch("lanegate.config_registry._registry_save"),
        ):
            mock_stdin.isatty.return_value = False
            return interactive_init(tmp_path, force_interactive=True)

    # --- trunk_branch: active branch, not a blind "main" ---

    def test_trunk_branch_defaults_to_current_git_branch(self, tmp_path):
        with mock.patch("lanegate.config_init._current_git_branch", return_value="refactor-code"):
            cfg = self._run(tmp_path, lambda _="": "")
        assert cfg["trunk_branch"] == "refactor-code"

    def test_trunk_branch_falls_back_to_main_when_undetectable(self, tmp_path):
        with mock.patch("lanegate.config_init._current_git_branch", return_value=None):
            cfg = self._run(tmp_path, lambda _="": "")
        assert cfg["trunk_branch"] == "main"

    def test_trunk_branch_typed_override_is_respected(self, tmp_path):
        def fake_input(prompt_text: str = "") -> str:
            return "develop" if prompt_text.startswith("trunk_branch ") else ""

        with mock.patch("lanegate.config_init._current_git_branch", return_value="refactor-code"):
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
            mock.patch("lanegate.config_init._detect_origin_head_branch", return_value="main"),
            mock.patch("lanegate.config_init._current_git_branch", return_value="my-feature"),
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
            "lanegate.config_init._recommend_aider_edit_format",
            return_value=("diff", "Detected `capture.py` at 1200 lines"),
        ):
            cfg = self._run(tmp_path, fake_input)
        assert cfg["executors"]["aider"]["edit_format"] == "diff"

    def test_edit_format_note_printed_when_large_file_detected(self, tmp_path, capsys):
        def fake_input(prompt_text: str = "") -> str:
            return "aider" if prompt_text.startswith("executor ") else ""

        with mock.patch(
            "lanegate.config_init._recommend_aider_edit_format",
            return_value=("diff", "Detected `capture.py` at 1200 lines"),
        ):
            self._run(tmp_path, fake_input)
        assert "Detected `capture.py` at 1200 lines" in capsys.readouterr().out

    def test_edit_format_defaults_to_whole_when_no_large_file(self, tmp_path):
        def fake_input(prompt_text: str = "") -> str:
            return "aider" if prompt_text.startswith("executor ") else ""

        with mock.patch(
            "lanegate.config_init._recommend_aider_edit_format", return_value=("whole", None)
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


