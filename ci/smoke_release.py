"""Build-and-install release smoke gate for the shipped LaneGate artifact.

Run locally with ``python ci/smoke_release.py``.  The script copies the source
tree to a temporary directory before building, installs its wheel into a new
virtual environment, and creates its test repositories under that same
temporary directory.  It therefore never creates ``dist/``, ``build/``, or
``.lanegate/`` in the checkout from which it was launched.

The checks deliberately exercise the release artifact rather than this source
checkout.  They cover packaging, clean installation, optional entrypoint
imports, undeclared imports, a real ticket lifecycle, a first environment
promotion, and interactive init with a guard script.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import venv
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


class SmokeFailure(RuntimeError):
    """One named release-smoke check failed."""


@dataclass(frozen=True)
class InstalledArtifact:
    python: Path
    lanegate: Path
    env: dict[str, str]


def _output(result: subprocess.CompletedProcess[str]) -> str:
    """Return a bounded, useful subprocess diagnostic."""
    text = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
    return text[-6000:] or "(no output)"


def _run(
    argv: list[str | Path],
    *,
    cwd: Path,
    env: dict[str, str],
    stdin: str | None = None,
    timeout: float = 90,
) -> subprocess.CompletedProcess[str]:
    """Run a command, raising a compact failure report on a non-zero result."""
    command = [str(part) for part in argv]
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SmokeFailure(f"timed out after {timeout:.0f}s: {' '.join(command)}") from exc
    if result.returncode != 0:
        raise SmokeFailure(
            f"command exited {result.returncode}: {' '.join(command)}\n{_output(result)}"
        )
    return result


def _isolated_env(home: Path) -> dict[str, str]:
    """Use a temporary home and prevent the caller's Python environment leaking in."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["PYTHONNOUSERSITE"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INPUT"] = "1"
    return env


def _copy_source(source_root: Path, destination: Path) -> None:
    """Copy only the build input; never let build tooling write in the checkout."""
    ignored = shutil.ignore_patterns(
        ".git",
        ".lanegate",
        ".venv",
        "venv",
        "build",
        "dist",
        "*.egg-info",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    )
    shutil.copytree(source_root, destination, ignore=ignored)


def check_build(source_root: Path, work: Path, env: dict[str, str]) -> tuple[Path, Path]:
    """Build a wheel and sdist from an isolated source copy (R1 coverage)."""
    tree = work / "source"
    _copy_source(source_root, tree)
    dist = work / "dist"
    try:
        _run([sys.executable, "-m", "build", "--outdir", dist], cwd=tree, env=env, timeout=180)
    except SmokeFailure as exc:
        raise SmokeFailure(
            "could not build both wheel and sdist (check force-include paths and build metadata): "
            f"{exc}"
        ) from exc

    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise SmokeFailure(
            f"expected one wheel and one sdist in {dist}; found wheels={wheels}, sdists={sdists}"
        )
    return wheels[0], tree


def check_clean_install(wheel: Path, work: Path, env: dict[str, str]) -> InstalledArtifact:
    """Install the wheel (never editable) in a brand-new venv."""
    venv_dir = work / "installed-venv"
    venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
    bindir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    python = bindir / ("python.exe" if os.name == "nt" else "python")
    lanegate = bindir / ("lanegate.exe" if os.name == "nt" else "lanegate")
    clean_env = dict(env)
    clean_env["VIRTUAL_ENV"] = str(venv_dir)
    clean_env["PATH"] = str(bindir) + os.pathsep + clean_env.get("PATH", "")
    _run(
        [python, "-m", "pip", "install", "--no-cache-dir", wheel],
        cwd=work,
        env=clean_env,
        timeout=180,
    )
    _run([lanegate, "--version"], cwd=work, env=clean_env)
    if not lanegate.is_file():
        raise SmokeFailure(f"wheel install did not create the console script: {lanegate}")
    return InstalledArtifact(python=python, lanegate=lanegate, env=clean_env)


def check_import_surface(installed: InstalledArtifact, work: Path) -> None:
    """Exercise CLI help and import the entrypoint modules in the clean venv (R2)."""
    _run([installed.lanegate, "mcp", "--help"], cwd=work, env=installed.env)
    _run([installed.lanegate, "api", "--help"], cwd=work, env=installed.env)
    _run(
        [
            installed.python,
            "-c",
            "import lanegate.api; import lanegate.mcp; print('entrypoint imports ok')",
        ],
        cwd=work,
        env=installed.env,
    )


def check_dependency_honesty(installed: InstalledArtifact, work: Path) -> None:
    """Import every shipped module with only wheel dependencies available (R3).

    The small Ollama invocation also proves its no-``requests`` fallback is
    viable: clean installs intentionally do not install the optional ollama
    extra, and the subprocess stub prevents a network call.
    """
    probe = """
import importlib
import pkgutil
import subprocess
import lanegate

modules = [m.name for m in pkgutil.walk_packages(lanegate.__path__, 'lanegate.')]
for name in modules:
    importlib.import_module(name)

from lanegate.orchestrate.pool import _invoke_ollama
original_run = subprocess.run
try:
    subprocess.run = lambda *args, **kwargs: type('Result', (), {'returncode': 1, 'stdout': ''})()
    assert _invoke_ollama('smoke', {}, '.') != 0
finally:
    subprocess.run = original_run
print(f'imported {len(modules)} shipped modules without optional extras')
"""
    _run([installed.python, "-c", probe], cwd=work, env=installed.env, timeout=120)


def _git(
    repo: Path, env: dict[str, str], *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    if check:
        return _run(["git", *args], cwd=repo, env=env)
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, timeout=90
    )


def _init_repo(repo: Path, installed: InstalledArtifact) -> None:
    repo.mkdir()
    _git(repo, installed.env, "init", "-b", "main")
    _git(repo, installed.env, "config", "user.email", "release-smoke@example.invalid")
    _git(repo, installed.env, "config", "user.name", "Release smoke")
    _run([installed.lanegate, "init", "--defaults"], cwd=repo, env=installed.env)


def _configure_lifecycle_repo(repo: Path) -> None:
    # The default scaffold deliberately ignores local state. Lifecycle status
    # commits need the ticket file tracked in this throwaway repository.
    with (repo / ".gitignore").open("a") as ignored:
        ignored.write("\n!.lanegate/\n!.lanegate/tickets/\n!.lanegate/tickets/**\n")
    config = repo / ".lanegate.yml"
    config.write_text(
        config.read_text()
        + "\ncommit_status_changes: true\n"
        + "environments:\n"
        + "  - name: smoke\n"
        + "    branch: release-smoke\n"
        + "    from: main\n"
        + "    trigger: manual\n"
    )
    (repo / "src").mkdir()
    (repo / "src" / "smoke.py").write_text('VALUE = "initial"\n')


def _ticket_id(output: str) -> str:
    matches = re.findall(r"^([A-Z][A-Z0-9_]*-\d+)$", output, flags=re.MULTILINE)
    if not matches:
        raise SmokeFailure(f"`lanegate create` did not print a ticket id:\n{output[-2000:]}")
    return matches[-1]


def check_first_run_lifecycle(installed: InstalledArtifact, work: Path) -> Path:
    """Run init → create → open → start → complete → review → merge for real."""
    repo = work / "lifecycle-repo"
    _init_repo(repo, installed)
    _configure_lifecycle_repo(repo)
    _git(repo, installed.env, "add", ".")
    _git(repo, installed.env, "commit", "-m", "initial smoke project")

    created = _run(
        [installed.lanegate, "create", "Release smoke lifecycle ticket", "--milestone", "v1.0", "--no-analyze"],
        cwd=repo,
        env=installed.env,
    )
    ticket_id = _ticket_id(created.stdout)
    ticket = repo / ".lanegate" / "tickets" / f"{ticket_id}.md"
    ticket.write_text(ticket.read_text().replace("touches: []", "touches:\n  - src/smoke.py"))
    _git(repo, installed.env, "add", ticket)
    _git(repo, installed.env, "commit", "-m", "configure release smoke ticket")

    _run([installed.lanegate, "open", ticket_id], cwd=repo, env=installed.env)
    _run([installed.lanegate, "start", ticket_id], cwd=repo, env=installed.env)
    worktree = repo / ".lanegate" / "worktrees" / ticket_id.lower()
    if not worktree.is_dir():
        raise SmokeFailure(f"start did not create the ticket worktree: {worktree}")
    (worktree / "src" / "smoke.py").write_text('VALUE = "merged"\n')
    _git(worktree, installed.env, "add", "src/smoke.py")
    _git(worktree, installed.env, "commit", "-m", "implement release smoke ticket")
    _run([installed.lanegate, "complete", ticket_id], cwd=repo, env=installed.env)
    _run(
        [installed.lanegate, "review", ticket_id, "--verdict", "approved", "--summary", "smoke"],
        cwd=repo,
        env=installed.env,
    )
    _run([installed.lanegate, "merge", ticket_id], cwd=repo, env=installed.env)
    if (repo / "src" / "smoke.py").read_text() != 'VALUE = "merged"\n':
        raise SmokeFailure("merged lifecycle change is absent from main")
    if worktree.exists():
        raise SmokeFailure("merge left the completed ticket worktree behind")
    return repo


def _pipeline_entry(board_output: str) -> dict:
    payload = json.loads(board_output)
    entries = [entry for entry in payload.get("pipeline", []) if entry.get("env") == "smoke"]
    if len(entries) != 1:
        raise SmokeFailure(f"board JSON did not contain exactly one smoke environment: {payload}")
    return entries[0]


def check_first_promote(installed: InstalledArtifact, repo: Path) -> None:
    """Verify first promotion bootstraps its branch and board sees missing branches (R14/R42)."""
    errors: list[str] = []
    missing_branch = _git(
        repo,
        installed.env,
        "show-ref",
        "--verify",
        "--quiet",
        "refs/heads/release-smoke",
        check=False,
    )
    if missing_branch.returncode == 0:
        errors.append("release-smoke branch existed before the first promotion")

    # A nonexistent environment branch must be explicit in board JSON, never
    # misrepresented as an empty/up-to-date range, before promote creates it.
    before = _run([installed.lanegate, "--json", "board"], cwd=repo, env=installed.env)
    before_entry = _pipeline_entry(before.stdout)
    if before_entry.get("pending_state") != "unknown" or before_entry.get("pending_count") is not None:
        errors.append(
            "board did not report a nonexistent release-smoke branch as an explicit unknown state"
        )

    try:
        _run([installed.lanegate, "promote", "smoke"], cwd=repo, env=installed.env)
    except SmokeFailure as exc:
        errors.append(str(exc))

    created_branch = _git(
        repo,
        installed.env,
        "show-ref",
        "--verify",
        "--quiet",
        "refs/heads/release-smoke",
        check=False,
    )
    if created_branch.returncode != 0:
        errors.append("promote exited without creating refs/heads/release-smoke")
    else:
        contains_main = _git(
            repo,
            installed.env,
            "merge-base",
            "--is-ancestor",
            "main",
            "release-smoke",
            check=False,
        )
        if contains_main.returncode != 0:
            errors.append("release-smoke does not contain the promoted main commit")

    # Make the branch observably behind again and assert board's post-promotion
    # view is not falsely green. Together with the preflight above this covers
    # every consumer of the shared pending-commit seam.
    (repo / "src" / "after-promote.py").write_text("AFTER_PROMOTE = True\n")
    _git(repo, installed.env, "add", "src/after-promote.py")
    _git(repo, installed.env, "commit", "-m", "post-promote board smoke")
    after = _run([installed.lanegate, "--json", "board"], cwd=repo, env=installed.env)
    if _pipeline_entry(after.stdout).get("pending_count", 0) < 1:
        errors.append("board reported release-smoke up to date after main gained a new commit")

    if errors:
        raise SmokeFailure("; ".join(errors))


def check_interactive_init_guard(installed: InstalledArtifact, work: Path) -> None:
    """Exercise ``init -i`` writing an argv-form guard, then reload that config (F27)."""
    repo = work / "interactive-init-repo"
    repo.mkdir()
    _git(repo, installed.env, "init", "-b", "main")
    _git(repo, installed.env, "config", "user.email", "release-smoke@example.invalid")
    _git(repo, installed.env, "config", "user.name", "Release smoke")
    (repo / "guard.py").write_text("raise SystemExit(0)\n")
    answers = "\n".join(
        [
            "",  # ticket_prefix
            "",  # tickets_dir
            "",  # worktrees_dir
            "",  # executor
            "",  # reviewer
            "",  # max_parallel
            "n",  # models
            "n",  # feature flags
            "y",  # environments
            "1",  # environment count
            "guarded",  # environment name
            "guarded",  # environment branch
            "main",  # source branch
            "manual",  # trigger
            "python guard.py",  # guard script (must become argv)
            "",  # pre-promote
            "",  # post-promote
            "n",  # safeguards
            "n",  # GitHub PR integration
            "n",  # prompt-template scaffolding after init
            "",
        ]
    )
    _run([installed.lanegate, "init", "-i"], cwd=repo, env=installed.env, stdin=answers, timeout=120)
    config = (repo / ".lanegate.yml").read_text()
    if "guard_script:\n  - python\n  - guard.py" not in config:
        raise SmokeFailure("interactive init did not write guard_script as an argv list")
    board = _run([installed.lanegate, "--json", "board"], cwd=repo, env=installed.env)
    # Parsing board is deliberately enough: it reloads and validates the newly
    # written configuration, which is the command path F27 previously bricked.
    if "guarded" not in board.stdout:
        raise SmokeFailure("board did not load the environment created by interactive init")


def _run_check(
    name: str, action: Callable[[], object], failures: list[tuple[str, str]]
) -> object | None:
    print(f"[release-smoke] {name}", flush=True)
    try:
        value = action()
    except SmokeFailure as exc:
        failures.append((name, str(exc)))
        print(f"  FAIL: {exc}", file=sys.stderr, flush=True)
        return None
    print("  ok", flush=True)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    source_root = Path(__file__).resolve().parents[1]
    if not (source_root / "pyproject.toml").is_file():
        print("[release-smoke] FAILED: build: could not find pyproject.toml", file=sys.stderr)
        return 1

    failures: list[tuple[str, str]] = []
    with tempfile.TemporaryDirectory(prefix="lanegate-release-smoke-") as temp:
        work = Path(temp)
        env = _isolated_env(work / "home")
        Path(env["HOME"]).mkdir()

        build = _run_check("1/7 build", lambda: check_build(source_root, work, env), failures)
        if build is None:
            print(
                "[release-smoke] checks 2-7 skipped because build produced no wheel",
                file=sys.stderr,
            )
        else:
            wheel, _tree = build
            installed = _run_check(
                "2/7 clean install", lambda: check_clean_install(wheel, work, env), failures
            )
            if installed is None:
                print(
                    "[release-smoke] checks 3-7 skipped because clean install failed",
                    file=sys.stderr,
                )
            else:
                assert isinstance(installed, InstalledArtifact)
                _run_check(
                    "3/7 import surface", lambda: check_import_surface(installed, work), failures
                )
                _run_check(
                    "4/7 dependency honesty",
                    lambda: check_dependency_honesty(installed, work),
                    failures,
                )
                lifecycle_repo = _run_check(
                    "5/7 first-run lifecycle",
                    lambda: check_first_run_lifecycle(installed, work),
                    failures,
                )
                if lifecycle_repo is None:
                    failures.append(("first promote", "skipped because first-run lifecycle failed"))
                else:
                    assert isinstance(lifecycle_repo, Path)
                    _run_check(
                        "6/7 first promote",
                        lambda: check_first_promote(installed, lifecycle_repo),
                        failures,
                    )
                _run_check(
                    "7/7 interactive init guard",
                    lambda: check_interactive_init_guard(installed, work),
                    failures,
                )

    if failures:
        names = ", ".join(name for name, _ in failures)
        print(f"[release-smoke] FAILED checks: {names}", file=sys.stderr)
        return 1
    print("[release-smoke] all seven checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
