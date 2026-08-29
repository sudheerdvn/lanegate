"""Interactive project initialization and registry helpers."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import yaml

from lanegate import APP_NAME
from lanegate.config import (
    CONFIG_FILENAME,
    ConfigError,
    TestRunnerDetection,
    _CODEX_HEADLESS_FLAGS,
    _SCOPED_CLAUDE_HEADLESS_FLAGS,
    _VALID_AIDER_EDIT_FORMATS,
    _VALID_EXECUTOR_TYPES,
    _VALID_REVIEWERS,
    _VALID_TRIGGERS,
    _current_git_branch,
    _detect_origin_head_branch,
    _recommend_aider_edit_format,
    resolve_trunk_branch,
    validate_model_for_executor,
)

from lanegate.config_registry import (
    _registry_load,
    _registry_save,
    registry_add,
    registry_load,
    registry_path,
    registry_remove,
)


# ---------------------------------------------------------------------------
# Interactive / non-interactive init
# ---------------------------------------------------------------------------


_stdin_exhausted_warned = False


def _warn_stdin_exhausted_once() -> None:
    """Print a one-time warning the first time stdin runs out mid-wizard.

    Degrading silently to defaults (see the EOFError handlers below) fixed a
    raw traceback on legitimate exhausted stdin, but it also means a piped
    answer string with the wrong number of lines no longer fails loudly --
    every prompt past the last real answer just quietly takes its default
    with no signal anything went wrong. Confirmed live in a fresh-install
    smoke test. This restores that signal without reintroducing the crash.
    """
    global _stdin_exhausted_warned
    if _stdin_exhausted_warned:
        return
    _stdin_exhausted_warned = True
    print(
        "WARNING: stdin ran out mid-wizard -- every remaining prompt is using its "
        "default instead of an answer you provided. If you piped in a fixed answer "
        "string, double check its line count before trusting the .lanegate.yml this "
        "writes (or re-run interactively, or with --defaults).",
        file=sys.stderr,
    )


def _prompt(prompt_text: str, default: str) -> str:
    """Prompt the user with a default shown in brackets; empty input returns default.

    Piped/non-interactive stdin that runs out mid-wizard degrades to the
    default (EOFError -> blank) instead of raising a raw traceback.
    """
    try:
        raw = input(f"{prompt_text} [{default}]: ").strip()
    except EOFError:
        _warn_stdin_exhausted_once()
        raw = ""
    return raw if raw else default


def _prompt_raw(prompt_text: str, default: str, *, display_default: str) -> tuple[str, str]:
    """Single-purpose variant of ``_prompt`` for the reviewer prompt below.

    Returns (resolved, raw_input) so the caller can distinguish "left this
    blank" from "typed a value that happens to equal the default" -- a blank
    reviewer answer must NOT write an explicit config pin (see its call site).

    display_default overrides only what's shown in the brackets, leaving the
    actual blank-input resolution untouched: showing the true fallback value
    in brackets would look like a normal default that Enter accepts, when
    accepting it actually behaves differently (a blank reviewer answer
    resolves at dispatch time instead of being written to config the way a
    typed answer, even one matching the fallback, would be).
    """
    try:
        raw = input(f"{prompt_text} [{display_default}]: ").strip()
    except EOFError:
        _warn_stdin_exhausted_once()
        raw = ""
    return (raw if raw else default), raw


def _prompt_yes_no(prompt_text: str, *, default: bool = False) -> bool:
    """Yes/no wizard prompt. EOF (stdin exhausted) degrades to ``default``
    instead of raising, matching ``_prompt``'s EOF handling above."""
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{prompt_text} {suffix}: ").strip().lower()
    except EOFError:
        _warn_stdin_exhausted_once()
        return default
    return default if not answer else answer in ("y", "yes")


def _project_mentions_pytest(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        return "pytest" in path.read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False


def _npm_test_detection(path: Path) -> TestRunnerDetection | None:
    """Detect an npm-based test runner, suggesting a CI-safe non-interactive
    command when package.json signals a framework whose default `test`
    script launches a watch-mode/interactive session (CRA's Jest watch mode,
    Angular CLI's Karma browser session) that would otherwise hang until
    safeguards.py's timeout_s.
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    scripts = data.get("scripts")
    if not isinstance(scripts, dict) or not scripts.get("test"):
        return None

    deps: dict = {}
    for key in ("dependencies", "devDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            deps.update(section)

    if "react-scripts" in deps:
        return TestRunnerDetection("npm test (Create React App)", "CI=true npm test")

    if "@angular/cli" in deps:
        return TestRunnerDetection("npm test (Angular)", "ng test --watch=false")

    return TestRunnerDetection("npm test", "npm test")


def detect_test_runner_safeguards(repo_root: Path) -> list[TestRunnerDetection]:
    """Detect common project test runners and return concrete safeguard commands."""

    detections: list[TestRunnerDetection] = []

    if (
        _project_mentions_pytest(repo_root / "pyproject.toml")
        or _project_mentions_pytest(repo_root / "setup.cfg")
        or any((repo_root / "tests").glob("test_*.py"))
    ):
        detections.append(TestRunnerDetection("pytest", "pytest"))

    npm_detection = _npm_test_detection(repo_root / "package.json")
    if npm_detection is not None:
        detections.append(npm_detection)

    if (repo_root / "Cargo.toml").exists():
        detections.append(TestRunnerDetection("cargo test", "cargo test"))

    if (repo_root / "go.mod").exists():
        detections.append(TestRunnerDetection("go test", "go test"))

    if (repo_root / "pom.xml").exists():
        detections.append(TestRunnerDetection("mvn test", "mvn test"))

    if (repo_root / "build.gradle").exists() or (repo_root / "build.gradle.kts").exists():
        detections.append(TestRunnerDetection("./gradlew test", "./gradlew test"))

    return detections


def suggested_safeguards_yaml(detections: list[TestRunnerDetection]) -> str:
    """Return the YAML block LaneGate suggests for detected test runners."""

    commands = [d.command for d in detections]
    lines = ["safeguards:", "  pre_complete:"]
    lines.extend(f"    - {command}" for command in commands)
    lines.append("  pre_merge:")
    lines.extend(f"    - {command}" for command in commands)
    return "\n".join(lines)


def _init_core_options(repo_root: Path) -> tuple[dict, str, str, bool]:
    """Prompt for ticket_prefix/tickets_dir/worktrees_dir/executor/reviewer/max_parallel."""
    print("\nConfiguring core options (press Enter to accept the default):\n")
    ticket_prefix = _prompt("ticket_prefix", "TICK")
    tickets_dir = _prompt("tickets_dir", f".{APP_NAME}/tickets")
    worktrees_dir = _prompt("worktrees_dir", f".{APP_NAME}/worktrees")
    print(f"  (valid: {', '.join(sorted(_VALID_EXECUTOR_TYPES))})")
    executor_raw = _prompt("executor", "claude")
    # Validate executor; fall back to claude on invalid input
    executor = executor_raw if executor_raw in _VALID_EXECUTOR_TYPES else "claude"
    if executor != executor_raw:
        print(
            f"  Warning: '{executor_raw}' is not a recognised executor; "
            f"using 'claude'. (Valid: {sorted(_VALID_EXECUTOR_TYPES)})"
        )
    print(f"  (valid: {', '.join(sorted(_VALID_REVIEWERS))})")
    print(
        f"  Tip: pick a reviewer different from executor ('{executor}') for an "
        "independent review. Leave this blank to let LaneGate decide at "
        "dispatch time (uses a different pool instance/model when one is "
        "available, otherwise escalates to a human review gate rather than "
        "silently self-reviewing) -- typing a value here, even one matching "
        "the executor, pins it and always wins over that safe fallback."
    )
    reviewer_raw, reviewer_input = _prompt_raw(
        "reviewer", executor, display_default="auto"
    )
    reviewer = reviewer_raw if reviewer_raw in _VALID_REVIEWERS else executor
    if reviewer_input and reviewer != reviewer_raw:
        print(
            f"  Warning: '{reviewer_raw}' is not a recognised reviewer; "
            f"treating it like a blank answer (auto). (Valid: {sorted(_VALID_REVIEWERS)})"
        )
    # A blank prompt -- or an unrecognized (typo'd) one -- must not
    # silently become an explicit reviewer pin: cfg["reviewer"] is set
    # conditionally below, only when the user typed a value that's
    # actually a real reviewer choice (see that comment for why this
    # matters). A typo pinning self-review the same way a deliberate
    # match does would be exactly the footgun the blank case was fixed
    # for, just triggered by a mistake instead of an empty Enter.
    reviewer_explicit = bool(reviewer_input) and reviewer_input in _VALID_REVIEWERS
    if reviewer_explicit and reviewer == executor:
        print(
            f"  Note: reviewer == executor ('{executor}') — review will run in "
            "combined (self-review) mode, not the independent review pipeline."
        )
    max_parallel_raw = _prompt("max_parallel", "2")
    try:
        max_parallel = int(max_parallel_raw)
        if max_parallel < 1:
            raise ValueError
    except ValueError:
        print("  Invalid max_parallel — using 2.")
        max_parallel = 2

    cfg: dict = {
        "ticket_prefix": ticket_prefix,
        "tickets_dir": tickets_dir,
        "worktrees_dir": worktrees_dir,
        "executor": executor,
        "max_parallel": max_parallel,
        "commit_status_changes": True,
    }
    # Only pin reviewer: when the user actually typed something at the
    # prompt above (even a value matching executor) -- a blank/default
    # answer leaves it unset so resolve_independent_review_driver's
    # ladder runs at dispatch time instead of being permanently bypassed.
    # An explicit pin "always wins outright" over that ladder (see its
    # docstring), including the review_fallback: needs_review safety
    # escalation that would otherwise apply to an unconfigured
    # single-account setup -- accepting a blank prompt must not disable
    # that safety net the same way a deliberate, informed pin does.
    if reviewer_explicit:
        cfg["reviewer"] = reviewer

    return cfg, executor, reviewer, reviewer_explicit


def _init_trunk_branch(cfg: dict, repo_root: Path) -> None:
    """Prompt for trunk_branch, defaulting to detected origin/HEAD or current branch."""
    # A real origin/HEAD detection wins over the currently checked-out
    # branch: a cloned repo with a remote configured (origin/HEAD ->
    # main) but currently sitting on a local feature/WIP branch during
    # `init` should default to "main", not that feature branch. Only
    # fall back to the checked-out branch when origin/HEAD can't be
    # determined at all (a fresh local project with no remote set up
    # yet, TICK-645) -- resolve_trunk_branch()'s own hardcoded "main"
    # fallback in that case would be blind to a project that isn't
    # actually using "main" as its trunk name.
    origin_head_branch = _detect_origin_head_branch(repo_root)
    current_branch = _current_git_branch(repo_root)
    trunk_branch = _prompt(
        "trunk_branch", origin_head_branch or current_branch or "main"
    )
    cfg["trunk_branch"] = trunk_branch


def _init_headless_flags(cfg: dict, executor: str, reviewer: str) -> None:
    """Write required unattended-run flags for whichever of executor/reviewer needs them."""
    # Without these, the tool blocks on an interactive prompt and an
    # unattended run just hangs instead of failing (see
    # docs/troubleshooting.md "The agent hangs and never finishes").
    _CLAUDE_TYPES = {"claude", "claude-subagent", "claude-process"}
    _headless_types = {
        t
        for t in (executor, reviewer)
        if t in _CLAUDE_TYPES or t in ("aider", "codex", "agy")
    }
    for _t in sorted(_headless_types):
        if _t in _CLAUDE_TYPES:
            print()
            print(f"Note: {_t} requires headless flags for unattended runs.")
            print("These are already pre-configured for you with a scoped permission set")
            print("(--allowedTools), rather than --dangerously-skip-permissions.")
            cfg.setdefault("executors", {}).setdefault(_t, {})["flags"] = list(
                _SCOPED_CLAUDE_HEADLESS_FLAGS
            )
        elif _t == "aider":
            print()
            print("Note: aider requires --yes-always for unattended runs (auto-confirms")
            print("its interactive prompts); --no-gitignore stops it editing .gitignore.")
            cfg.setdefault("executors", {}).setdefault("aider", {})["flags"] = [
                "--yes-always",
                "--no-gitignore",
            ]
        elif _t == "codex":
            print()
            print("Note: codex requires approval/sandbox bypass flags for unattended runs.")
            cfg.setdefault("executors", {}).setdefault("codex", {})["flags"] = list(
                _CODEX_HEADLESS_FLAGS
            )
        elif _t == "agy":
            print()
            print("Note: agy requires --dangerously-skip-permissions for unattended runs")
            print("(tool executions would otherwise block on interactive prompts), and")
            print("--disable-slash-commands so agy doesn't interpret '/'-prefixed prompt")
            print("content (e.g. ticket text) as its own CLI commands.")
            cfg.setdefault("executors", {}).setdefault("agy", {})["flags"] = [
                "--dangerously-skip-permissions",
                "--disable-slash-commands",
            ]


def _init_autonomy(cfg: dict) -> None:
    """Prompt for pipeline autonomy (full vs supervised)."""
    # resolve_autonomy() already defaults to "supervised" (pause for a
    # manual `lanegate merge`) when this is left unset -- a deliberate
    # safety default, not a bug. But the wizard never offered a way to
    # opt into unattended "full" autonomy either, so every fresh project
    # silently got supervised with no indication another option existed
    # (TICK-645). Leaving option 2 unwritten preserves that same safe
    # default; only an explicit "full" choice changes anything.
    print()
    print("Pipeline autonomy:")
    print("  [1] full — unattended: auto-merge on an approved review")
    print("  [2] supervised — pause at each ticket for a manual `lanegate merge` (default)")
    autonomy_choice = _prompt("autonomy", "2")
    if autonomy_choice == "1":
        cfg["autonomy"] = "full"
        cfg["default_human_review"] = "none"
    elif autonomy_choice not in ("2", ""):
        print(f"  Invalid choice {autonomy_choice!r} — using supervised.")


def _init_models(
    cfg: dict, executor: str, reviewer: str, reviewer_explicit: bool, repo_root: Path
) -> dict:
    """Prompt for models.analyze/implement/fix/review/drift_check, including Ollama discovery."""
    # Always shown (not gated behind a y/N) so the resulting .lanegate.yml
    # states exactly which model each step will use instead of leaving it
    # to whatever the executor's own CLI/config defaults to invisibly.
    print()
    print("Model selection (press Enter to accept the default / use the tool's own default):")
    _MODEL_EXAMPLES: dict[str, str] = {
        "claude": "claude-haiku-4-5-20251001, claude-sonnet-5, claude-opus-5",
        "claude-subagent": "claude-haiku-4-5-20251001, claude-sonnet-5, claude-opus-5",
        "claude-process": "claude-haiku-4-5-20251001, claude-sonnet-5, claude-opus-5",
        "aider": "ollama_chat/qwen2.5-coder:14b (local), claude-sonnet-4-6 (cloud)",
        "codex": "gpt-5.6-terra, gpt-5.6-sol, o3, openai/o3-mini",
        "ollama": "qwen3-coder:30b-a3b-q4_K_M, qwen2.5-coder:14b",
    }
    # Wizard-only suggested defaults: review intentionally points at a
    # stronger/different model than analyze+implement so review isn't
    # just the implementer re-reading its own work with its own biases,
    # mirroring this project's own executors.claude-a/codex/aider-ollama-*
    # blocks. This is separate from resolve_model()'s runtime fallback
    # (_DEFAULT_*_MODEL, all haiku) used when models: is left unset
    # entirely -- that fallback stays a conservative/cheap default.
    # fix and drift_check are as exposed to an unconfigured-step gap as
    # analyze/implement/review: resolve_model() returns None for any
    # non-Claude executor with no models.<step> entry, and for aider that
    # means no --model flag at all -- which, with an Ollama provider, falls
    # through to aider's own default (an interactive OpenRouter login flow)
    # instead of the local model the rest of the config points at. A
    # headless dispatch just hangs on that prompt for several minutes and
    # then fails, indistinguishable from a genuine drift-check/fix failure
    # to whatever's watching (observed live on a real drift-check run,
    # which is what prompted adding these two prompts). fix follows implement's
    # suggestion (it's editing code the same way); drift_check follows
    # review's (autofix.py already treats it as "an independent review
    # route" that resolves from the review route's config).
    _WIZARD_STEP_DEFAULTS: dict[str, dict[str, str]] = {
        "claude": {
            "analyze": "claude-sonnet-5",
            "implement": "claude-sonnet-5",
            "review": "claude-opus-5",
            "fix": "claude-sonnet-5",
            "drift_check": "claude-opus-5",
        },
        "claude-subagent": {
            "analyze": "claude-sonnet-5",
            "implement": "claude-sonnet-5",
            "review": "claude-opus-5",
            "fix": "claude-sonnet-5",
            "drift_check": "claude-opus-5",
        },
        "claude-process": {
            "analyze": "claude-sonnet-5",
            "implement": "claude-sonnet-5",
            "review": "claude-opus-5",
            "fix": "claude-sonnet-5",
            "drift_check": "claude-opus-5",
        },
        "codex": {
            "analyze": "gpt-5.6-terra",
            "implement": "gpt-5.6-terra",
            "review": "gpt-5.6-sol",
            "fix": "gpt-5.6-terra",
            "drift_check": "gpt-5.6-sol",
        },
        "aider": {
            "analyze": "ollama_chat/qwen2.5-coder:14b",
            "implement": "ollama_chat/qwen2.5-coder:14b",
            "review": "ollama_chat/qwen2.5-coder:32b",
            "fix": "ollama_chat/qwen2.5-coder:14b",
            "drift_check": "ollama_chat/qwen2.5-coder:32b",
        },
    }

    # Best-effort discovery of what's actually pulled locally, so the
    # wizard's suggested default is a model that exists rather than a
    # hardcoded 14b/32b guess that 404s at runtime if it isn't installed
    # (TICK-645). One lookup, reused for all three model prompts below;
    # [] (Ollama not running / nothing pulled / unreachable) falls back
    # to today's hardcoded suggestion with no behavior change.
    _ollama_discovered: list[str] = []
    if "aider" in (executor, reviewer) or "ollama" in (executor, reviewer):
        from lanegate.executor import discover_ollama_models

        _ollama_discovered = discover_ollama_models("http://localhost:11434")

    def _ask_model(step: str, exec_type: str) -> str:
        examples = _MODEL_EXAMPLES.get(exec_type)
        hint = f"e.g. {examples}" if examples else f"check {exec_type}'s own docs for supported model names"
        print(f"  models.{step} ({exec_type}) — {hint}")
        default = _WIZARD_STEP_DEFAULTS.get(exec_type, {}).get(step, "")

        # aider routes local models through Aider's LiteLLM integration,
        # which needs an "ollama_chat/" prefix; the raw `ollama` executor
        # type talks to Ollama directly and takes the bare name. `ollama
        # list`/`GET /api/tags` always reports the bare name either way.
        picker: dict[str, str] = {}
        if exec_type in ("aider", "ollama") and _ollama_discovered:
            prefix = "ollama_chat/" if exec_type == "aider" else ""
            display_names = [f"{prefix}{name}" for name in _ollama_discovered]
            size_hint = "32b" if step in ("review", "drift_check") else "14b"
            suggested = next(
                (i for i, name in enumerate(_ollama_discovered) if size_hint in name), 0
            )
            print("  Installed Ollama models detected:")
            for i, display_name in enumerate(display_names):
                marker = " (suggested)" if i == suggested else ""
                print(f"    [{i + 1}] {display_name}{marker}")
            if exec_type == "aider":
                print(
                    "  Note: aider needs the 'ollama_chat/' prefix above (LiteLLM "
                    "routing) -- 'ollama list' itself reports these without it."
                )
            default = display_names[suggested]
            picker = {str(i + 1): name for i, name in enumerate(display_names)}

        while True:
            value = _prompt(f"  models.{step}", default)
            if not value:
                return value
            if picker and value.isdigit() and value not in picker:
                print(
                    f"  Invalid choice: '{value}' is not one of the listed "
                    f"options above ([1]-[{len(picker)}])."
                )
                continue
            value = picker.get(value, value)
            try:
                # No provider= here: the wizard doesn't know the
                # provider yet at this point (aider+Ollama is decided
                # further down, after all the model prompts run), so
                # this uses validate_model_for_executor's permissive
                # no-provider branch -- still catches a wrong-vendor
                # model string, just not an Ollama-specific mismatch.
                validate_model_for_executor(value, exec_type, f"models.{step}")
            except ConfigError as exc:
                print(f"  Invalid model: {exc}")
                continue
            return value

    models: dict[str, str] = {}
    for step, step_executor in (
        ("analyze", executor),
        ("implement", executor),
        ("fix", executor),
        ("review", reviewer),
        ("drift_check", reviewer),
    ):
        value = _ask_model(step, step_executor)
        if value:
            models[step] = value
    if models:
        cfg["models"] = models

    # --- Independent review across two local models ---
    # A same-tool setup (e.g. executor: aider, reviewer: aider) with two
    # different models is a real independent review -- but
    # resolve_independent_review_driver's ladder can only see a different
    # pool instance/driver, not a same-instance model swap, so without
    # this it falls through to the needs_review safety escalation on
    # every ticket (TICK-645).
    if (
        reviewer_explicit
        and reviewer == executor
        and models.get("review")
        and models.get("review") != models.get("implement")
    ):
        cfg.setdefault("review_fallback", "different_model")

    return models


def _init_aider_ollama_context(
    cfg: dict, executor: str, reviewer: str, models: dict, repo_root: Path
) -> None:
    """Suggest a context budget + edit_format when aider is routed to a local Ollama model."""
    # A local model has a finite context window; without a declared budget,
    # an oversized ticket overflows it unpredictably instead of failing
    # cleanly upfront (see docs/executor-capabilities.md#context-window-tokens).
    # Declaring provider: ollama here also arms lanegate's own runtime
    # warning if context_window_tokens is later left unset.
    aider_ollama_model = next(
        (
            models[step]
            for step, step_executor in (
                ("analyze", executor),
                ("implement", executor),
                ("fix", executor),
                ("review", reviewer),
                ("drift_check", reviewer),
            )
            if step_executor == "aider" and models.get(step, "").startswith("ollama")
        ),
        None,
    )
    if aider_ollama_model:
        print()
        print(
            f"Note: aider is routed to a local Ollama model ({aider_ollama_model}). "
            "LaneGate can enforce a preflight context budget so an oversized "
            "prompt fails cleanly instead of overflowing the model silently."
        )
        context_tokens_raw = _prompt(
            "  executors.aider.context_window_tokens (0 to skip)", "32768"
        )
        aider_cfg = cfg.setdefault("executors", {}).setdefault("aider", {})
        aider_cfg["provider"] = "ollama"
        try:
            context_tokens = int(context_tokens_raw)
        except ValueError:
            context_tokens = 0
        if context_tokens > 0:
            aider_cfg["context_window_tokens"] = context_tokens

        # Neither edit_format is universally safe for small local models:
        # "whole" rewrites the entire file every turn and can truncate or
        # hallucinate past a few hundred lines; "diff" avoids that but can
        # get a malformed hunk from a small model (see
        # docs/executor-capabilities.md, "Known caveats" for aider). Pick
        # the default from what's actually in this repo (TICK-645) rather
        # than a flat guess either way.
        recommended_format, format_note = _recommend_aider_edit_format(repo_root)
        if format_note:
            print(f"  {format_note}")
        else:
            print(
                "  No large tracked file detected here; 'whole' (full-file "
                "rewrites) is fine for small local models. Switch to 'diff' "
                "if a touched file grows past ~300-500 lines."
            )
        # Every other optional step in this wizard is an input(...
        # [y/N]) confirm, so a "y"/"n" typed here from muscle memory
        # must not land in config verbatim -- it would silently become
        # `aider --edit-format y` on every dispatch.
        # _VALID_AIDER_EDIT_FORMATS is a module-level constant (see top of file).
        while True:
            edit_format = _prompt("  executors.aider.edit_format", recommended_format)
            if not edit_format or edit_format in _VALID_AIDER_EDIT_FORMATS:
                break
            print(
                f"  Invalid edit_format {edit_format!r} — valid values are "
                f"{sorted(_VALID_AIDER_EDIT_FORMATS)}"
            )
        if edit_format:
            aider_cfg["edit_format"] = edit_format

        # repo_map/neutralize_touches/map_tokens keep the prompt lean by
        # deferring eager full-file preload to aider's own lazy
        # filename-mention scan instead of front-loading every touched
        # file — see the neutralize_touches/repo_map comments in
        # lanegate/executor.py's aider dispatch for the full rationale.
        aider_cfg["repo_map"] = True
        aider_cfg["neutralize_touches"] = True
        aider_cfg["map_tokens"] = 1024

        # A local model costs nothing per call, unlike a cloud API where
        # every retry is a real charge -- max_auto_fix_attempts' default of
        # 1 is a deliberate cost guardrail for that cloud case, not a
        # correctness requirement. It doesn't apply the same way once
        # everything routed through aider here is local, so default higher
        # for a local-Ollama setup instead of leaving cloud's conservative
        # default in place for a project that has no reason to want it.
        cfg.setdefault("max_auto_fix_attempts", 2)


def _init_feature_flags(cfg: dict) -> None:
    """Prompt to enable the feature-flag file."""
    print()
    want_flags = _prompt_yes_no("Enable feature flags?")
    if want_flags:
        flag_file = _prompt("flag_file", f"~/.{APP_NAME}/feature_flags.json")
        cfg["flag_file"] = flag_file


def _init_deployment_pipeline(cfg: dict, detected_trunk_branch: str) -> None:
    """Prompt to configure the optional deployment pipeline (environments)."""
    print()
    want_envs = _prompt_yes_no("Enable deployment pipeline (environments)?")
    if want_envs:
        num_envs_raw = _prompt("Number of environments", "1")
        try:
            num_envs = int(num_envs_raw)
            if num_envs < 1:
                raise ValueError
        except ValueError:
            print("  Invalid number — skipping environments.")
            num_envs = 0

        environments = []
        for i in range(num_envs):
            print(f"\n  Environment {i + 1}:")
            env_name = _prompt("    name", f"env{i + 1}")
            env_branch = _prompt("    branch", env_name)
            env_from = _prompt("    from (source branch)", detected_trunk_branch)
            env_trigger = _prompt("    trigger (manual/auto)", "manual")
            if env_trigger not in _VALID_TRIGGERS:
                print("    Invalid trigger; using 'manual'.")
                env_trigger = "manual"
            env_guard = _prompt("    guard_script (leave blank to skip)", "").strip() or None
            pre_raw = _prompt(
                "    pre_promote scripts (comma-separated, blank to skip)", ""
            ).strip()
            pre_promote = [s.strip() for s in pre_raw.split(",") if s.strip()]
            post_raw = _prompt(
                "    post_promote scripts (comma-separated, blank to skip)", ""
            ).strip()
            post_promote = [s.strip() for s in post_raw.split(",") if s.strip()]

            env_entry: dict = {
                "name": env_name,
                "branch": env_branch,
                "from": env_from,
                "trigger": env_trigger,
            }
            if env_guard:
                # Hooks are argv lists, never bare strings — validate_hook rejects
                # a string outright, so writing one here bricks every later command.
                env_entry["guard_script"] = shlex.split(env_guard)
            if pre_promote:
                env_entry["pre_promote"] = pre_promote
            if post_promote:
                env_entry["post_promote"] = post_promote

            environments.append(env_entry)

        if environments:
            cfg["environments"] = environments


def _init_safeguards(cfg: dict, repo_root: Path) -> None:
    """Prompt to configure pre_complete/pre_merge safeguards, offering detected test runners."""
    print()
    detected_runners = detect_test_runner_safeguards(repo_root)
    if detected_runners:
        runner_names = ", ".join(d.name for d in detected_runners)
        commands = [d.command for d in detected_runners]
        command_list = ", ".join(commands)
        want_safeguards = _prompt_yes_no(
            f"Detected {runner_names} -- configure pre_complete: "
            f"[{command_list}], pre_merge: [{command_list}]?",
            default=True,
        )
        if want_safeguards:
            cfg["safeguards"] = {
                "pre_complete": commands,
                "pre_merge": commands,
            }
    else:
        want_safeguards = _prompt_yes_no(
            "Configure ticket safeguards (pre_complete / pre_merge guards)?"
        )
        if want_safeguards:
            print("  Enter guard commands as a comma-separated list (blank to skip).")
            print("  Examples: pytest, scripts/run-tests.sh, cargo test, npm test")
            pre_complete_raw = _prompt(
                "  pre_complete guards (run before marking code_complete)", ""
            ).strip()
            pre_complete = [s.strip() for s in pre_complete_raw.split(",") if s.strip()]
            pre_merge_raw = _prompt("  pre_merge guards (run before git merge)", "").strip()
            pre_merge = [s.strip() for s in pre_merge_raw.split(",") if s.strip()]

            safeguards: dict = {}
            if pre_complete:
                safeguards["pre_complete"] = pre_complete
            if pre_merge:
                safeguards["pre_merge"] = pre_merge
            if safeguards:
                cfg["safeguards"] = safeguards


def _init_github_pr(cfg: dict) -> None:
    """Prompt to enable auto-push + GitHub PR creation on approved review."""
    print()
    cfg["github_pr"] = _prompt_yes_no(
        "Auto-push branches and open GitHub PRs on approved review?"
    )


def _finalize_init_config(cfg: dict, repo_root: Path) -> dict:
    """Run the steps common to both interactive and non-interactive init: re-init safety
    check, writing .lanegate.yml, updating .gitignore, creating directories, and
    registering the project."""
    config_path = repo_root / CONFIG_FILENAME

    # --- Re-init safety: detect existing tickets in a non-default location ---
    # If a non-default directory (e.g. tickets/) exists and contains .md files,
    # preserve that tickets_dir rather than silently switching to .lanegate/tickets.
    proposed_tickets_dir = cfg.get("tickets_dir", f".{APP_NAME}/tickets")
    existing_tickets_dir, has_existing_tickets = _detect_existing_tickets_dir(
        repo_root, proposed_tickets_dir
    )
    if existing_tickets_dir is not None and has_existing_tickets:
        # Non-empty existing directory at a different location — warn and preserve it.
        print(
            f"\nWARNING: found existing tickets in '{existing_tickets_dir}' "
            f"(relative to repo root).",
            file=sys.stderr,
        )
        print(
            f"  tickets_dir will be set to '{existing_tickets_dir}' "
            f"to preserve your existing tickets.",
            file=sys.stderr,
        )
        print(
            "  To use the new default (.lanegate/tickets), migrate your tickets manually "
            "and re-run `lanegate init`.",
            file=sys.stderr,
        )
        cfg["tickets_dir"] = existing_tickets_dir
    elif existing_tickets_dir is not None and not has_existing_tickets:
        # Empty directory at a different location — silent update to new default is permitted.
        pass  # keep the proposed default

    # --- Write config ---
    # Surface these explicitly in generated config rather than relying on the
    # silent config.py default, so a project's history-retention behavior is
    # visible and easy to change without digging into defaults code.
    cfg.setdefault("run_history_purge_enabled", False)
    cfg.setdefault("run_history_retention_days", 60)
    config_path.write_text(yaml.dump(cfg, default_flow_style=False, sort_keys=False), encoding="utf-8")

    # --- Update .gitignore ---
    # aider's own scratch/cache files (chat history, input history, tags
    # cache) are normally kept out of git by aider silently editing
    # .gitignore itself at startup -- an uncommitted side effect separate
    # from aider's own commit that LaneGate's scope-drift check then flags
    # as an unexpected committed file (see executor.py's
    # _warn_aider_missing_no_gitignore). --no-gitignore stops aider from
    # doing that, but then nothing ignores those scratch files at all, and
    # THEY trip the identical scope-drift check instead -- confirmed live in
    # a fresh-install smoke test. Writing the patterns into the project's
    # own .gitignore up front avoids both failure modes: aider's own
    # gitignore-editing has nothing left to add (a no-op, not a diff), and
    # --no-gitignore's scratch files are still covered.
    aider_in_use = (
        cfg.get("executor") == "aider"
        or cfg.get("reviewer") == "aider"
        or any(
            isinstance(v, dict) and v.get("type") == "aider"
            for v in (cfg.get("executors") or {}).values()
        )
    )
    extra_gitignore_entries = [".aider.*"] if aider_in_use else None
    _update_gitignore(
        repo_root,
        cfg.get("tickets_dir", f".{APP_NAME}/tickets"),
        extra_entries=extra_gitignore_entries,
    )

    # --- Create directories ---
    tickets_dir_path = repo_root / cfg.get("tickets_dir", f".{APP_NAME}/tickets")
    worktrees_dir_path = repo_root / cfg.get("worktrees_dir", f".{APP_NAME}/worktrees")
    tickets_dir_path.mkdir(parents=True, exist_ok=True)
    worktrees_dir_path.mkdir(parents=True, exist_ok=True)

    # --- Register in global registry ---
    registry_add(repo_root)

    return cfg


def interactive_init(
    repo_root: Path, *, use_defaults: bool = False, force_interactive: bool = False
) -> dict | None:
    """
    Walk through every core config option and write .lanegate.yml to repo_root.

    Returns the config dict on success, or None if .lanegate.yml already exists.

    Parameters
    ----------
    repo_root:
        Directory where .lanegate.yml will be written.
    use_defaults:
        Skip all prompts and write a minimal config (also activated when stdin is
        not a TTY).
    force_interactive:
        Show prompts even when stdin is not a TTY (overrides the TTY check).
    """
    global _stdin_exhausted_warned
    _stdin_exhausted_warned = False
    config_path = repo_root / CONFIG_FILENAME

    if config_path.exists():
        print(
            f"ERROR: {CONFIG_FILENAME} already exists at {repo_root}. "
            "Remove it first to re-initialise.",
            file=sys.stderr,
        )
        return None

    detected_trunk_branch = resolve_trunk_branch({}, repo_root)

    non_interactive = use_defaults or (not force_interactive and not sys.stdin.isatty())
    if non_interactive and not use_defaults:
        print(
            "Note: stdin is not a TTY — using defaults. "
            "Run `lanegate init --interactive` to configure interactively.",
            file=sys.stderr,
        )

    if non_interactive:
        # Minimal defaults, no environments, no flag_file
        cfg: dict = {
            "ticket_prefix": "TICK",
            "tickets_dir": f".{APP_NAME}/tickets",
            "worktrees_dir": f".{APP_NAME}/worktrees",
            "executor": "claude",
            "max_parallel": 2,
            "commit_status_changes": True,
            "github_pr": False,
            "executors": {
                "claude": {
                    "flags": list(_SCOPED_CLAUDE_HEADLESS_FLAGS),
                }
            },
        }
    else:
        cfg, executor, reviewer, reviewer_explicit = _init_core_options(repo_root)
        _init_trunk_branch(cfg, repo_root)
        _init_headless_flags(cfg, executor, reviewer)
        _init_autonomy(cfg)
        models = _init_models(cfg, executor, reviewer, reviewer_explicit, repo_root)
        _init_aider_ollama_context(cfg, executor, reviewer, models, repo_root)
        _init_feature_flags(cfg)
        _init_deployment_pipeline(cfg, detected_trunk_branch)
        _init_safeguards(cfg, repo_root)
        _init_github_pr(cfg)

    return _finalize_init_config(cfg, repo_root)


# ---------------------------------------------------------------------------
# .gitignore helpers
# ---------------------------------------------------------------------------

def _gitignore_entries() -> list[str]:
    # CONFIG_FILENAME (.{APP_NAME}.yml) is deliberately NOT ignored: `git
    # worktree add` only checks out committed content, so an ignored
    # (never-committed) config leaves the very first ticket's worktree
    # without any config at all.
    return [f".{APP_NAME}/*", f"{APP_NAME}-context-log.jsonl"]


def _update_gitignore(
    repo_root: Path, tickets_dir: str | None = None, *, extra_entries: list[str] | None = None
) -> None:
    """Append .lanegate/ to .gitignore if not already present.

    Carves out tickets_dir (e.g. !.lanegate/tickets/ and !.lanegate/tickets/*) when
    tickets_dir sits under .lanegate/. Creates .gitignore if it doesn't exist. Also
    strips a stale CONFIG_FILENAME (.lanegate.yml) entry a pre-existing project's
    .gitignore may already carry from before it was deliberately excluded above --
    without this, a project initialized before that change stays gitignored on
    upgrade with no migration path, reproducing the same never-committed-config bug.
    """
    gitignore_path = repo_root / ".gitignore"
    if gitignore_path.exists():
        existing = gitignore_path.read_text(encoding="utf-8")
    else:
        existing = ""

    entries = list(_gitignore_entries()) + ["__pycache__/", "*.pyc", "*.pyo"] + list(extra_entries or [])
    if tickets_dir:
        norm = tickets_dir.strip("/")
        parts = Path(norm).parts
        if parts and parts[0] == f".{APP_NAME}":
            entries.extend([f"!{norm}/", f"!{norm}/*"])

    existing_lines = [line for line in existing.splitlines() if line.strip() != CONFIG_FILENAME]
    stripped_stale = len(existing_lines) != len(existing.splitlines())
    existing_line_set = {line.strip() for line in existing_lines}
    to_add = [entry for entry in entries if entry not in existing_line_set]

    if not to_add and not stripped_stale:
        return  # all entries already present, nothing stale to remove

    body = "\n".join(existing_lines)
    if body and not body.endswith("\n"):
        body += "\n"
    if to_add:
        body += "\n".join(to_add) + "\n"
    gitignore_path.write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# Re-init safety helpers
# ---------------------------------------------------------------------------


def _detect_existing_tickets_dir(repo_root: Path, proposed: str) -> tuple[str | None, bool]:
    """Detect a pre-existing tickets directory at a non-default location.

    Checks common legacy locations (e.g. 'tickets/') for .md files.  Only
    reports a conflict when the candidate is different from *proposed*.

    Note: only the three canonical candidates below are probed.  Projects that
    stored tickets in an unusual path (e.g. 'tasks/', 'work/') will not be
    detected automatically; users with such setups should set tickets_dir
    explicitly in .lanegate.yml before running init.

    Returns:
        (relative_dir, has_md_files) where relative_dir is the path relative
        to repo_root, or (None, False) if no pre-existing directory is found.
    """
    # Candidate legacy locations to probe
    candidates = ["tickets", "issues", ".tickets"]
    for candidate in candidates:
        if candidate == proposed:
            continue  # same as what we're about to write — no conflict
        candidate_path = repo_root / candidate
        if candidate_path.is_dir():
            md_files = list(candidate_path.glob("*.md"))
            return candidate, bool(md_files)
    return None, False
