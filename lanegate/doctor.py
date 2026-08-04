from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from lanegate.config import (
    _SCOPED_CLAUDE_HEADLESS_FLAGS,
    detect_test_runner_safeguards,
    find_repo_root,
    load_config,
    resolve_executor,
    suggested_safeguards_yaml,
)
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

_PY_SECURITY_INSTALL = (
    "python3 -m pip install 'lanegate[security]'  OR  "
    "python3 -m pip install -e '.[security]' from source  (prefer a venv)"
)
_PY_SECURITY_SOURCE_INSTALL = (
    "python3 -m pip install -e '.[security]'  (from source checkout, prefer a venv)"
)
_PY_SECURITY_SOURCE_INSTALL_CMD = "python3 -m pip install -e '.[security]'"


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
        description="Linux sandbox engine availability — current V1 executors are not wrapped",
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
        description="macOS sandbox engine availability — current V1 executors are not wrapped",
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


def _get_version(binary: str, version_args: tuple[str, ...] = ("--version",)) -> str:
    try:
        r = subprocess.run(
            [binary, *version_args], capture_output=True, text=True, encoding="utf-8", timeout=3
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
    _, quarantined = load_all_tickets(tickets_dir, cfg.get("ticket_prefix", "TICK"), cfg)
    if quarantined:
        print(f"\n[doctor] ERROR: {len(quarantined)} ticket(s) failed to parse and are quarantined:")
        for q in quarantined:
            print(f"         {q.path.name}: {q.error}")

    system = platform.system()
    categories = ["core", "runtime", "analysis", "sandbox"]
    any_required_missing = False
    any_optional_missing = False
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
                version = _get_version(tool.binary, tool.version_args)
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
    print("         Current V1 executors still run as host processes; no OS sandbox is applied.")

    detections = detect_test_runner_safeguards(Path(repo_root))
    if detections and not cfg.get("safeguards"):
        runner_names = ", ".join(d.name for d in detections)
        print("\n[doctor] WARNING: detected test runner(s) but safeguards are not configured.")
        print(f"         Detected: {runner_names}")
        print("         Add this block to .lanegate.yml:")
        print(suggested_safeguards_yaml(detections))

    executor = cfg.get("executor", "").lower()
    if executor == "claude":
        executors_cfg = cfg.get("executors", {})
        claude_cfg = executors_cfg.get("claude", {})
        flags = claude_cfg.get("flags", [])
        if not _has_headless_permission_config(flags):
            print("\n[doctor] WARNING: Claude executor requires headless flags for orchestrate.")
            print("         Without one of these, orchestrate will hang waiting for interactive prompts.")
            print("         Recommended — a scoped permission set, add this to .lanegate.yml:")
            print("         executors:")
            print("           claude:")
            print(f"             flags: {_SCOPED_CLAUDE_HEADLESS_FLAGS!r}")
            print("         Also valid: a non-interactive --permission-mode "
                  f"({sorted(_NON_INTERACTIVE_PERMISSION_MODES)}), or the")
            print('         bypass flag: flags: ["--dangerously-skip-permissions"]')

    if cfg.get("reviewer"):
        implement_driver = resolve_executor(cfg, "implement")
        review_driver = resolve_executor(cfg, "review")
        if implement_driver == review_driver:
            print("\n[doctor] WARNING: reviewer resolves identically to the implement executor.")
            print(f"         reviewer: {review_driver!r} == executor: {implement_driver!r}")
            print("         Review will silently run in combined (self-review) mode, not the")
            print("         independent review pipeline — update reviewer or executor in .lanegate.yml.")

    return 1 if (any_required_missing or quarantined) else 0
