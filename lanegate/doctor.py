from __future__ import annotations

import importlib
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Maps each optional module to the pyproject.toml extra that installs it, so
# the doctor warning can name the exact `pip install lanegate[...]` to run
# instead of a blanket reinstall (these are opt-in per-language, not core).
_TREESITTER_MODULES: dict[str, str] = {
    "tree_sitter_go": "go",
    "tree_sitter_javascript": "js",
    "tree_sitter_typescript": "ts",
    "tree_sitter_rust": "rust",
    "tree_sitter_java": "java",
    "tree_sitter_ruby": "ruby",
    "tree_sitter_c": "c",
    "tree_sitter_cpp": "cpp",
    "tree_sitter_php": "php",
    "tree_sitter_c_sharp": "csharp",
    "tree_sitter_swift": "swift",
    "tree_sitter_kotlin": "kotlin",
}

from lanegate.config import (
    _CODEX_HEADLESS_FLAGS,
    _SCOPED_CLAUDE_HEADLESS_FLAGS,
    _VALID_EXECUTOR_TYPES,
    _VALID_REVIEWERS,
    detect_test_runner_safeguards,
    find_repo_root,
    load_config,
    resolve_executor,
    resolve_model,
    suggested_safeguards_yaml,
)
from lanegate.executor import get_executor_config
from lanegate.orchestrate.autofix import combined_mode_capable
from lanegate.ticket import load_all_tickets

# --permission-mode values that don't block on interactive input. Excludes
# "manual" and "plan", which are interactive/confirmation-first by design and
# so would still hang a headless orchestrate run.
_NON_INTERACTIVE_PERMISSION_MODES = {"acceptEdits", "auto", "dontAsk", "bypassPermissions"}


def _has_headless_permission_config(flags: list[str]) -> bool:
    """Return True if flags configure any valid headless permission mode.

    The bypass flag, a non-interactive --permission-mode, and an
    --allowedTools/--disallowedTools set are equally valid ways to avoid the
    interactive-approval hang; only the absence of all three is worth warning
    about.
    """
    if "--dangerously-skip-permissions" in flags:
        return True
    if "--allowedTools" in flags or "--disallowedTools" in flags:
        return True
    if "--permission-mode" in flags:
        idx = flags.index("--permission-mode")
        if idx + 1 < len(flags) and flags[idx + 1] in _NON_INTERACTIVE_PERMISSION_MODES:
            return True
    return False


def _has_codex_sandbox_bypass(flags: list[str]) -> bool:
    """Return True when Codex will not start its internal bwrap sandbox."""
    return (
        "--dangerously-bypass-approvals-and-sandbox" in flags
        or "--sandbox=danger-full-access" in flags
        or any(
            flags[index] in {"--sandbox", "-s"} and index + 1 < len(flags)
            and flags[index + 1] == "danger-full-access"
            for index in range(len(flags))
        )
    )

_PY_SECURITY_INSTALL = (
    "python3 -m pip install 'lanegate[security]'  OR  "
    "python3 -m pip install -e '.[security]' from source  (prefer a venv)"
)
_PY_SECURITY_SOURCE_INSTALL = (
    "python3 -m pip install -e '.[security]'  (from source checkout, prefer a venv)"
)
_PY_SECURITY_SOURCE_INSTALL_CMD = "python3 -m pip install -e '.[security]'"

_DEFAULT_VERSION_TIMEOUT = 3.0


@dataclass
class _Tool:
    name: str
    binary: str
    required: bool
    category: str  # "core" | "analysis" | "sandbox" | "runtime"
    install: dict[str, str]
    description: str
    condition: str | None = None
    version_args: tuple[str, ...] = ("--version",)
    version_timeout: float = _DEFAULT_VERSION_TIMEOUT


@dataclass(frozen=True)
class _SetupBundle:
    label: str
    system_tools: list[str]
    python_analyzers: list[str]
    all_in_one: list[str]
    notes: list[str]


_TOOLS: list[_Tool] = [
    _Tool(
        name="git",
        binary="git",
        required=True,
        category="core",
        description="version control — required for all lanegate operations",
        install={
            "Linux": "apt install git",
            "Darwin": "brew install git",
            "Windows": "winget install Git.Git",
        },
    ),
    _Tool(
        name="rg (ripgrep)",
        binary="rg",
        required=False,
        category="runtime",
        description="fast keyword search used by analyze step (any language)",
        install={
            "Linux": "apt install ripgrep",
            "Darwin": "brew install ripgrep",
            "Windows": "winget install BurntSushi.ripgrep.MSVC",
        },
    ),
    _Tool(
        name="semgrep",
        binary="semgrep",
        required=False,
        category="analysis",
        description="primary cross-language scanner: Python, Go, JS/TS, Rust, Java, C/C++ and more",
        install={
            "Linux": "pip install semgrep  OR  brew install semgrep",
            "Darwin": "brew install semgrep  OR  pip install semgrep",
            "Windows": "winget install semgrep  OR  pip install semgrep",
        },
        # semgrep's Python startup is slower than the other doctor-checked
        # binaries (native Rust/Go tools); the shared 3s default timeout was
        # intermittently too tight, reporting a spurious version-check
        # failure for an install that's actually fine.
        version_timeout=10.0,
    ),
    _Tool(
        name="gitleaks",
        binary="gitleaks",
        required=False,
        category="analysis",
        description="secret and credential pattern scanner — works on any repo language",
        install={
            "Linux": "apt install gitleaks  OR  brew install gitleaks",
            "Darwin": "brew install gitleaks",
            "Windows": "winget install gitleaks",
        },
        version_args=("version",),
    ),
    _Tool(
        name="bandit",
        binary="bandit",
        required=False,
        category="analysis",
        description=(
            "Python security linter — used only when semgrep absent and repo has .py files"
        ),
        install={"*": _PY_SECURITY_INSTALL},
    ),
    _Tool(
        name="pip-audit",
        binary="pip-audit",
        required=False,
        category="analysis",
        description="Python dependency vulnerability scanner — Python projects only",
        install={"*": _PY_SECURITY_INSTALL},
    ),
    _Tool(
        name="npm",
        binary="npm",
        required=False,
        category="analysis",
        description="JS/Node dependency audit via npm audit — JS projects only",
        install={"*": "install Node.js from https://nodejs.org  (JS projects only)"},
    ),
    _Tool(
        name="composer",
        binary="composer",
        required=False,
        category="analysis",
        description="PHP dependency audit via composer audit — PHP projects only",
        install={"*": "install Composer from https://getcomposer.org  (PHP projects only)"},
    ),
    _Tool(
        name="bundle-audit",
        binary="bundle-audit",
        required=False,
        category="analysis",
        description="Ruby dependency audit via bundler-audit — Ruby projects only",
        install={"*": "gem install bundler-audit  (Ruby projects only)"},
    ),
    _Tool(
        name="bwrap",
        binary="bwrap",
        required=False,
        category="sandbox",
        description="Linux sandbox engine — opt-in worktree sandboxing for Claude subprocess executors",
        install={
            "Linux": "apt install bubblewrap",
            "Darwin": "n/a — macOS uses sandbox-exec (built-in)",
        },
    ),
    _Tool(
        name="sandbox-exec",
        binary="sandbox-exec",
        required=False,
        category="sandbox",
        description="macOS sandbox engine — opt-in worktree sandboxing for Claude subprocess executors",
        install={
            "Darwin": "built-in on macOS — no install needed",
            "Linux": "n/a — Linux uses bwrap",
        },
    ),
]


_ANALYSIS_IMPACT: dict[str, str] = {
    "semgrep": (
        "cross-language SAST will be skipped; "
        "bandit only covers changed Python files when available"
    ),
    "gitleaks": "secret scanning will be skipped",
    "bandit": "Python fallback security linting will be unavailable when semgrep is absent",
    "pip-audit": (
        "Python dependency vulnerability scanning will be skipped when Python manifests change"
    ),
    "npm": "JS dependency vulnerability scanning will be skipped when package manifests change",
    "composer": (
        "PHP dependency vulnerability scanning will be skipped when Composer manifests change"
    ),
    "bundle-audit": (
        "Ruby dependency vulnerability scanning will be skipped when Gemfile manifests change"
    ),
}


def _setup_bundle(system: str, *, has_apt: bool | None = None) -> _SetupBundle:
    """Return platform-specific setup commands for common doctor gaps."""
    if system == "Linux":
        if has_apt is None:
            has_apt = shutil.which("apt") is not None
        label = "Ubuntu/Debian source checkout" if has_apt else "Linux source checkout"
        apt_system = "sudo apt install -y git ripgrep gitleaks bubblewrap"
        brew_system = "brew install git ripgrep gitleaks semgrep"
        return _SetupBundle(
            label=label,
            system_tools=[
                apt_system,
                f"Alternative with Homebrew: {brew_system}",
            ],
            python_analyzers=[_PY_SECURITY_SOURCE_INSTALL_CMD],
            all_in_one=[
                f"{apt_system} && {_PY_SECURITY_SOURCE_INSTALL_CMD}",
                (
                    "Alternative with Homebrew: "
                    f"{brew_system} && {_PY_SECURITY_SOURCE_INSTALL_CMD}"
                ),
            ],
            notes=[
                "sandbox-exec is macOS-only; Linux uses bwrap/bubblewrap.",
            ],
        )
    if system == "Darwin":
        brew_system = "brew install git ripgrep gitleaks semgrep"
        return _SetupBundle(
            label="macOS source checkout",
            system_tools=[brew_system],
            python_analyzers=[_PY_SECURITY_SOURCE_INSTALL_CMD],
            all_in_one=[
                f"{brew_system} && {_PY_SECURITY_SOURCE_INSTALL_CMD}",
            ],
            notes=[
                "sandbox-exec is built into macOS; there is no separate install.",
            ],
        )
    if system == "Windows":
        winget_system = "winget install Git.Git BurntSushi.ripgrep.MSVC semgrep gitleaks"
        return _SetupBundle(
            label="Windows source checkout",
            system_tools=[winget_system],
            python_analyzers=[_PY_SECURITY_SOURCE_INSTALL_CMD],
            all_in_one=[
                (
                    "winget install Git.Git BurntSushi.ripgrep.MSVC semgrep gitleaks && "
                    f"{_PY_SECURITY_SOURCE_INSTALL_CMD}"
                ),
            ],
            notes=[],
        )
    return _SetupBundle(
        label="source checkout",
        system_tools=[],
        python_analyzers=[_PY_SECURITY_SOURCE_INSTALL_CMD],
        all_in_one=[_PY_SECURITY_SOURCE_INSTALL_CMD],
        notes=[],
    )


def _get_version(
    binary: str,
    version_args: tuple[str, ...] = ("--version",),
    timeout: float = _DEFAULT_VERSION_TIMEOUT,
) -> str:
    try:
        r = subprocess.run(
            [binary, *version_args], capture_output=True, text=True, encoding="utf-8", timeout=timeout
        )
        returncode = getattr(r, "returncode", 0)
        if isinstance(returncode, int) and returncode != 0:
            return ""
        line = (r.stdout or r.stderr).strip().splitlines()[0]
        return f"({line[:40]})" if line else ""
    except Exception:
        return ""


def _is_platform_na(tool: _Tool, system: str) -> bool:
    """Return True if this tool is not applicable on the current platform."""
    if tool.binary == "bwrap" and system == "Darwin":
        return True
    if tool.binary == "sandbox-exec" and system == "Linux":
        return True
    return False


def cmd_doctor(cfg: dict | None = None) -> int:
    repo_root = find_repo_root()
    if cfg is None:
        cfg = load_config(repo_root)

    tickets_dir = Path(repo_root) / cfg.get("tickets_dir", ".lanegate/tickets")
    tickets, quarantined = load_all_tickets(tickets_dir, cfg.get("ticket_prefix", "TICK"), cfg)
    if quarantined:
        print(f"\n[doctor] ERROR: {len(quarantined)} ticket(s) failed to parse and are quarantined:")
        for q in quarantined:
            print(f"         {q.path.name}: {q.error}")

    system = platform.system()
    categories = ["core", "runtime", "analysis", "sandbox"]
    any_required_missing = False
    any_optional_missing = False
    invalid_executor_config = False
    unhealthy_tools: list[_Tool] = []

    for category in categories:
        tools = [t for t in _TOOLS if t.category == category]
        if not tools:
            continue
        print(f"\n{category.upper()}")
        for tool in tools:
            # Handle platform-specific n/a tools
            if _is_platform_na(tool, system):
                install_cmd = tool.install.get(system) or tool.install.get("*", "see docs")
                print(f"  –  {tool.name:<20} {install_cmd}")
                continue

            # sandbox-exec is built-in on macOS — treat as always present
            if tool.binary == "sandbox-exec" and system == "Darwin":
                print(f"  ✓  {tool.name:<20} (built-in)")
                continue

            found = shutil.which(tool.binary) is not None
            if found:
                version = _get_version(tool.binary, tool.version_args, tool.version_timeout)
                if version:
                    print(f"  ✓  {tool.name:<20} {version}")
                else:
                    unhealthy_tools.append(tool)
                    print(f"  !  {tool.name:<20} installed, but version check failed")
            elif tool.required:
                any_required_missing = True
                install_cmd = tool.install.get(system) or tool.install.get("*", "see docs")
                print(f"  ✗  {tool.name:<20} NOT FOUND  →  {install_cmd}")
            else:
                any_optional_missing = True
                install_cmd = tool.install.get(system) or tool.install.get("*", "see docs")
                print(f"  –  {tool.name:<20} not installed  (optional)  →  {install_cmd}")

    if any_required_missing:
        print("\n[doctor] ERROR: required tools missing — install them before using lanegate")
    else:
        print("\n[doctor] Required runtime tools present.")
    missing_analysis = [
        t
        for t in _TOOLS
        if not t.required
        and t.category == "analysis"
        and not _is_platform_na(t, system)
        and not shutil.which(t.binary)
    ]
    unhealthy_analysis = [
        t for t in unhealthy_tools if not t.required and t.category == "analysis"
    ]
    if missing_analysis or unhealthy_analysis:
        print("[doctor] WARNING: analysis coverage degraded.")
        if missing_analysis:
            names = ", ".join(t.name for t in missing_analysis)
            print(f"         Optional analysis tools not installed: {names}")
        if unhealthy_analysis:
            names = ", ".join(t.name for t in unhealthy_analysis)
            print(f"         Optional analysis tools installed but not runnable: {names}")
        for tool in missing_analysis + unhealthy_analysis:
            impact = _ANALYSIS_IMPACT.get(tool.binary)
            if impact:
                print(f"         - {tool.name}: {impact}")
        pip_missing = [t for t in missing_analysis if t.binary in ("bandit", "pip-audit")]
        non_pip_missing = [t for t in missing_analysis if t.binary not in ("bandit", "pip-audit")]
        if pip_missing and non_pip_missing:
            print(f"         Python tools: {_PY_SECURITY_SOURCE_INSTALL}")
            print("         Other tools: see install instructions above")
        elif pip_missing:
            print(f"         Python tools: {_PY_SECURITY_SOURCE_INSTALL}")
    else:
        print("[doctor] Analysis coverage: all known optional analysis tools are available.")

    if any_optional_missing or unhealthy_tools:
        bundle = _setup_bundle(system)
        print(f"\n[doctor] Setup bundle ({bundle.label}):")
        if bundle.system_tools:
            print("         System tools:")
            for command in bundle.system_tools:
                print(f"           {command}")
        print("         Python analyzers:")
        for command in bundle.python_analyzers:
            print(f"           {command}")
        print("         All-in-one:")
        for command in bundle.all_in_one:
            print(f"           {command}")
        print("         Python extras install Python analyzers only.")
        print("         OS tools such as gitleaks/bwrap need apt, brew, winget, or similar.")
        for note in bundle.notes:
            print(f"         {note}")

    print("\n[doctor] NOTE: sandbox tool detection is availability only.")
    print("         Claude subprocess executors use OS filesystem isolation only with sandbox: worktree.")

    detections = detect_test_runner_safeguards(Path(repo_root))
    if detections and not cfg.get("safeguards"):
        runner_names = ", ".join(d.name for d in detections)
        print("\n[doctor] WARNING: detected test runner(s) but safeguards are not configured.")
        print(f"         Detected: {runner_names}")
        print("         Add this block to .lanegate.yml:")
        print(suggested_safeguards_yaml(detections))

    executor_names = list((cfg.get("executors") or {}).keys())
    executor_names.extend(cfg.get(field) for field in ("executor", "reviewer") if cfg.get(field))
    checked_executors: set[tuple[str, str]] = set()
    for configured_name in executor_names:
        try:
            entry = get_executor_config(configured_name, cfg)
        except Exception:
            continue
        name = entry.get("instance", configured_name)
        executor_type = entry.get("type", configured_name)
        identity = (name, executor_type)
        if identity in checked_executors:
            continue
        checked_executors.add(identity)
        flags = entry.get("flags") or []
        if executor_type in {"claude", "claude-subagent", "claude-process"}:
            if not _has_headless_permission_config(flags):
                print("\n[doctor] WARNING: Claude executor requires headless flags for orchestrate.")
                print(f"         Affected instance: {name!r} (type {executor_type!r}).")
                print("         Without one of these, orchestrate will hang waiting for interactive prompts.")
                print("         Recommended — a scoped permission set, add this to .lanegate.yml:")
                print("         executors:")
                print(f"           {name}:")
                print(f"             type: {executor_type}")
                print(f"             flags: {_SCOPED_CLAUDE_HEADLESS_FLAGS!r}")
                print("         Also valid: a non-interactive --permission-mode "
                      f"({sorted(_NON_INTERACTIVE_PERMISSION_MODES)}), or the")
                print('         bypass flag: flags: ["--dangerously-skip-permissions"]')
        elif executor_type == "agy":
            required_flags = {"--dangerously-skip-permissions", "--disable-slash-commands"}
            missing_flags = sorted(required_flags - set(flags))
            if missing_flags:
                print(f"\n[doctor] WARNING: agy executor {name!r} requires unattended-mode flags.")
                print(f"         Missing: {', '.join(missing_flags)}")
        elif executor_type == "codex":
            missing_flags = [flag for flag in _CODEX_HEADLESS_FLAGS if flag not in flags]
            if missing_flags:
                print(f"\n[doctor] WARNING: codex executor {name!r} requires unattended-mode flags.")
                print(f"         Missing: {', '.join(missing_flags)}")
        elif executor_type == "aider":
            required_flags = {"--yes-always", "--no-gitignore"}
            missing_flags = sorted(required_flags - set(flags))
            if missing_flags:
                print(f"\n[doctor] WARNING: aider executor {name!r} requires unattended-mode flags.")
                print(f"         Missing: {', '.join(missing_flags)}")

    executors_cfg = cfg.get("executors") or {}
    codex_executors = []
    for name in executors_cfg:
        try:
            ex_cfg = get_executor_config(name, cfg)
        except Exception:
            continue
        if ex_cfg.get("type") == "codex":
            codex_executors.append((ex_cfg.get("instance", name), ex_cfg))
    if cfg.get("executor"):
        top_ex_cfg: dict | None = None
        try:
            top_ex_cfg = get_executor_config(cfg["executor"], cfg)
        except Exception:
            top_ex_cfg = None
        if top_ex_cfg and top_ex_cfg.get("type") == "codex":
            name = top_ex_cfg.get("instance", cfg["executor"])
            if not any(candidate_name == name for candidate_name, _ in codex_executors):
                codex_executors.append((name, top_ex_cfg))
    for name, ex_cfg in codex_executors:
        flags = ex_cfg.get("flags", [])
        if not isinstance(flags, list) or not _has_codex_sandbox_bypass(flags):
            invalid_executor_config = True
            print(f"\n[doctor] ERROR: Codex executor '{name}' must bypass its internal sandbox.")
            print("         Without this, Codex can emit a verdict after bwrap prevents every command from running.")
            print("         Add this to .lanegate.yml:")
            print("         flags: [\"--dangerously-bypass-approvals-and-sandbox\"]")
            print("         Equivalent: --sandbox danger-full-access")

    if "architecture_doc" in cfg:
        print("\n[doctor] WARNING: 'architecture_doc' in .lanegate.yml is deprecated.")
        print("         Use 'reference_docs' list in .lanegate.yml instead.")

    arch_doc_file = Path(repo_root) / "docs" / "ARCHITECTURE.md"
    if arch_doc_file.exists() and not cfg.get("reference_docs") and "architecture_doc" not in cfg:
        print("\n[doctor] WARNING: docs/ARCHITECTURE.md exists on disk but reference_docs is not configured.")
        print("         Implicit docs/ARCHITECTURE.md injection is no longer supported.")
        print("         To inject reference docs into prompts, declare them in .lanegate.yml:")
        print("           reference_docs:")
        print("             - docs/ARCHITECTURE.md")

    if cfg.get("reviewer"):
        step_routes = cfg.get("steps") or {}
        implement_driver = (step_routes.get("implement") or {}).get("driver") or resolve_executor(
            cfg, "implement"
        )
        review_driver = (step_routes.get("review") or {}).get("driver") or resolve_executor(cfg, "review")
        if implement_driver == review_driver and combined_mode_capable(implement_driver, cfg):
            print("\n[doctor] WARNING: reviewer resolves identically to the implement executor.")
            print(f"         reviewer: {review_driver!r} == executor: {implement_driver!r}")
            print("         Review will silently run in combined (self-review) mode, not the")
            print("         independent review pipeline — update reviewer or executor in .lanegate.yml.")

    step_routes = cfg.get("steps") or {}
    for step, route in step_routes.items():
        if not isinstance(route, dict) or not route.get("driver"):
            continue
        bare_field = "reviewer" if step == "review" else "executor"
        bare_driver = cfg.get(bare_field)
        if bare_driver:
            print(f"\n[doctor] WARNING: steps.{step}.driver overrides the bare {bare_field}: field.")
            print(f"         steps.{step}.driver: {route['driver']!r} wins; {bare_field}: {bare_driver!r} is inert for this step.")
            print("         Remove the unused setting to make the routing intent unambiguous.")

    if cfg.get("pools"):
        executors_cfg = cfg.get("executors") or {}
        pools_cfg = cfg.get("pools") or {}
        real_instance_names = sorted({*executors_cfg.keys(), *pools_cfg.keys()})
        top_executor = cfg.get("executor")
        top_reviewer = cfg.get("reviewer")
        for field_name, value in (("executor", top_executor), ("reviewer", top_reviewer)):
            if not value:
                continue
            try:
                resolved = get_executor_config(value, cfg)
            except Exception:
                continue
            resolved_instance = resolved.get("instance")
            valid_bare_values = _VALID_REVIEWERS if field_name == "reviewer" else _VALID_EXECUTOR_TYPES
            if (
                value not in valid_bare_values
                and value not in pools_cfg
                and resolved_instance not in executors_cfg
            ):
                print(f"\n[doctor] WARNING: top-level '{field_name}: {value}' does not name any real executor instance or pool.")
                print(f"         Real instance names: {', '.join(real_instance_names)}")
                print("         Per-ticket executor/reviewer pins and default_pool/pools routing")
                print("         (resolve_pool_executor in orchestrate/loop.py) govern actual dispatch.")
                print(f"         This field only serves as the resolve_max_parallel_detail (config.py ~L1263)")
                print("         fallback base and the default for new tickets without a pin.")
                print(f"         Set '{field_name}' to a real executors[] key, pool name, or bare type, or remove it.")

    model_without_executor_pin = [
        ticket
        for ticket in tickets
        if ticket.get("model") and not ticket.get("executor")
    ]
    if model_without_executor_pin:
        print("\n[doctor] WARNING: ticket(s) set model: without an executor: pin.")
        for ticket in model_without_executor_pin:
            print(
                f"         {ticket.get('id')}: model={ticket.get('model')!r} "
                "missing pin: executor"
            )
        print("         The implementation/fix step falls back to the project default executor, but")
        print("         still applies this ticket's model string — a pool-selected driver might not")
        print("         recognize that model. Review routing uses reviewer:/review_model_pin instead.")

    # Grammars are opt-in per language (pyproject.toml extras), not installed
    # by default -- so only warn about ones the project actually needs, based
    # on file extensions found in the repo. Warning about all 12 regardless
    # of what the project uses would be noise for every single install.
    from lanegate.analyze import _TS_LANGUAGE_MAP, _is_ignored_analysis_path

    used_extensions: set[str] = set()
    for src_file in Path(repo_root).rglob("*"):
        if src_file.suffix in _TS_LANGUAGE_MAP and not _is_ignored_analysis_path(
            src_file, Path(repo_root)
        ):
            used_extensions.add(src_file.suffix)
            if used_extensions == set(_TS_LANGUAGE_MAP):
                break

    missing_grammars: dict[str, str] = {}
    for mod_name, extra in _TREESITTER_MODULES.items():
        if not any(_TS_LANGUAGE_MAP.get(ext) == mod_name for ext in used_extensions):
            continue
        try:
            importlib.import_module(mod_name)
        except Exception:
            missing_grammars[mod_name] = extra

    if missing_grammars:
        print("\n[doctor] WARNING: missing tree-sitter grammar(s) for languages used in this repo.")
        print(f"         Unimportable modules: {', '.join(missing_grammars)}")
        extras = ",".join(sorted(set(missing_grammars.values())))
        print(f"         AST-quality skeletons/symbol-search for these files will fall back to ripgrep until installed:")
        print(f"         pip install 'lanegate[{extras}]'")

    from lanegate.pending_globals import check_pending_globals, format_pending_globals_notice
    pg_info = check_pending_globals(Path(repo_root))
    if pg_info["has_pending"]:
        print(f"\n[doctor] {format_pending_globals_notice(pg_info)}")

    executors_cfg = cfg.get("executors") or {}
    aider_models: dict[str, set[str]] = {}

    for ex_name, ex_cfg in executors_cfg.items():
        if isinstance(ex_cfg, dict) and ex_cfg.get("type", ex_name) == "aider":
            if ex_name not in aider_models:
                aider_models[ex_name] = set()
            ex_model = ex_cfg.get("model")
            if ex_model:
                aider_models[ex_name].add(ex_model)
            for m in (ex_cfg.get("models") or {}).values():
                if m:
                    aider_models[ex_name].add(m)

    for step in ["analyze", "implement", "review", "review_escalation", "fix", "drift_check"]:
        try:
            ex_name = resolve_executor(cfg, step)
        except Exception:
            continue
        ex_cfg = executors_cfg.get(ex_name) or {}
        is_aider = False
        if isinstance(ex_cfg, dict):
            is_aider = ex_cfg.get("type", ex_name) == "aider"
        elif not ex_cfg and ex_name == "aider":
            is_aider = True

        if is_aider:
            resolved_m = resolve_model(cfg, step)
            if resolved_m:
                if ex_name not in aider_models:
                    aider_models[ex_name] = set()
                aider_models[ex_name].add(resolved_m)

    for ex_name, models in aider_models.items():
        ex_cfg = executors_cfg.get(ex_name) or {}
        model_settings = ex_cfg.get("model_settings") or {}
        for m in sorted(models):
            if "/" in m:
                if model_settings.get(m, {}).get("edit_format") is None:
                    print(f"\n[doctor] WARNING: aider executor '{ex_name}' uses custom model {m!r}")
                    print("         but lacks an explicit 'edit_format' override in model_settings.")
                    print("         Aider may fall back to 'whole' format and silently drop diff-shaped edits.")

    size_check = Path(repo_root) / "scripts" / "check_file_size.py"
    if size_check.exists():
        result = subprocess.run(
            ["python", str(size_check), "--absolute"], cwd=repo_root, text=True, capture_output=True
        )
        for line in result.stdout.splitlines():
            if line.startswith(("WARNING:", "BLOCK:")):
                print(f"\n[doctor] WARNING: file-size ratchet: {line}")
        hooks = subprocess.run(
            ["git", "config", "--get", "core.hooksPath"], cwd=repo_root, text=True, capture_output=True
        ).stdout.strip()
        if hooks != ".githooks":
            print("\n[doctor] WARNING: repository Git hooks are not enabled.")
            print("         Fix: git config core.hooksPath .githooks")

    return 1 if (any_required_missing or quarantined or invalid_executor_config) else 0
