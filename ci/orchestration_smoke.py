"""Real end-to-end orchestration regression against a live executor.

Complements ci/smoke_release.py, which validates packaging and lifecycle
plumbing with a manually forced --verdict. This script drives the *real*
pipeline instead: live analyze -> implement -> review dispatch through a
real executor pool (claude-a/claude-b by default) against a tiny fixture
project, and asserts every ticket reaches `merged` with zero manual
intervention -- no hand-editing a ticket file, no forced verdict, no
patched-up worktree.

Costs real executor time and tokens. Not part of CI -- run it by hand
before a change that touches orchestration, review routing, or the
independence ladder ships:

    python ci/orchestration_smoke.py

Results are left under ~/ai/tests/lanegate-e2e/ (not cleaned up) so you
can inspect the sample repo, tickets, and orchestration logs afterward.
Each run starts by wiping that directory and copying a fresh working
copy from ci/fixtures/orchestration-smoke-sample/ (the pristine
template, never modified by a run) -- so reruns are always from a known
clean starting point, not whatever a previous run left behind.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import venv
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "orchestration-smoke-sample"
DEFAULT_WORK = Path.home() / "ai" / "tests" / "lanegate-e2e"
DEFAULT_SOURCE = Path(__file__).resolve().parents[1]
STEP_POLL_TIMEOUT = 600  # 10 min ceiling for a single orchestrate invocation


class SmokeFailure(RuntimeError):
    """One named orchestration-smoke check failed."""


def run(
    argv: list[str | Path],
    *,
    cwd: Path,
    timeout: float = 120,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = [str(part) for part in argv]
    try:
        result = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise SmokeFailure(f"timed out after {timeout:.0f}s: {' '.join(command)}") from exc
    if check and result.returncode != 0:
        tail = (result.stdout[-3000:] + "\n" + result.stderr[-2000:]).strip()
        raise SmokeFailure(f"command failed ({result.returncode}): {' '.join(command)}\n{tail}")
    return result


def setup_work_dir(work: Path) -> None:
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)


def setup_venv(work: Path, source: Path) -> Path:
    """Fresh venv, editable install of the release candidate, plus pytest
    (the fixture's own test/safeguard dependency -- not part of LaneGate
    itself)."""
    venv_dir = work / "venv"
    venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
    python = venv_dir / "bin" / "python"
    run([python, "-m", "pip", "install", "-q", "-e", source], cwd=work, timeout=180)
    run([python, "-m", "pip", "install", "-q", "pytest"], cwd=work, timeout=120)
    return venv_dir / "bin"


def setup_sample_repo(work: Path) -> Path:
    repo = work / "sample-repo"
    shutil.copytree(FIXTURE, repo)
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "smoke@example.invalid"], cwd=repo)
    run(["git", "config", "user.name", "Orchestration Smoke"], cwd=repo)
    run(["git", "add", "-A"], cwd=repo)
    run(["git", "commit", "-q", "-m", "initial converter CLI"], cwd=repo)
    return repo


def configure_executors(bindir: Path, repo: Path) -> None:
    run([bindir / "lanegate", "init", "--defaults"], cwd=repo, timeout=60)
    config_path = repo / ".lanegate.yml"
    text = config_path.read_text()
    # No `reviewer:` pin below -- that bypasses the TICK-381 review
    # independence ladder and forces combined (self-review) mode
    # directly. Leaving it unset lets LaneGate pick a different pool
    # instance (claude-b reviews what claude-a implemented, or vice
    # versa) for genuine split-mode independent review.
    text += """
default_milestone: v1
default_human_review: none
autonomy: full

safeguards:
  pre_complete:
    - pytest
  pre_merge:
    - pytest

executors:
  claude-a:
    type: claude
    bin: claude-a
    max_parallel: 1
    flags: ["--allowedTools", "Bash,Edit,Write,Read,Glob,Grep"]
  claude-b:
    type: claude
    bin: claude-b
    max_parallel: 1
    flags: ["--allowedTools", "Bash,Edit,Write,Read,Glob,Grep"]

pools:
  default:
    executors: [claude-a, claude-b]
    strategy: least-loaded
default_pool: default
"""
    config_path.write_text(text)
    run(["git", "add", "-A"], cwd=repo)
    run(["git", "commit", "-q", "-m", "configure smoke executor pool"], cwd=repo)


def create_and_open_ticket(bindir: Path, repo: Path, intent: str) -> str:
    """`create` (with its own real analyze pass) then `open` -- never
    `--no-analyze` followed by a hand-edited touches list, and never a
    second analyze call once one has already populated touches."""
    result = run([bindir / "lanegate", "create", intent, "--milestone", "v1"], cwd=repo, timeout=120)
    match = re.search(r"([A-Z]+-\d+)", result.stdout)
    if not match:
        raise SmokeFailure(f"could not parse ticket id from create output:\n{result.stdout}")
    ticket_id = match.group(1)
    run([bindir / "lanegate", "open", ticket_id], cwd=repo, timeout=30)
    verify_ticket_tracked(repo, ticket_id)
    return ticket_id


def verify_ticket_tracked(repo: Path, ticket_id: str) -> None:
    result = run(
        ["git", "ls-files", f".lanegate/tickets/{ticket_id}.md"], cwd=repo, check=False
    )
    if not result.stdout.strip():
        raise SmokeFailure(f"{ticket_id} is not git-tracked (TICK-356 regression)")


def orchestrate(bindir: Path, repo: Path, tickets: list[str], max_parallel: int) -> str:
    argv = [
        bindir / "lanegate",
        "orchestrate",
        "--tickets",
        ",".join(tickets),
        "--max",
        str(max_parallel),
        "--human-review",
        "none",
    ]
    result = subprocess.run(
        [str(a) for a in argv], cwd=repo, capture_output=True, text=True, timeout=STEP_POLL_TIMEOUT
    )
    output = result.stdout + "\n" + result.stderr
    if result.returncode != 0:
        raise SmokeFailure(f"orchestrate exited {result.returncode}\n{output[-4000:]}")
    return output


def ticket_status(repo: Path, ticket_id: str) -> str:
    text = (repo / ".lanegate" / "tickets" / f"{ticket_id}.md").read_text()
    match = re.search(r"^status:\s*(\S+)", text, flags=re.MULTILINE)
    if not match:
        raise SmokeFailure(f"{ticket_id}: could not read status from ticket file")
    return match.group(1)


def review_independence(repo: Path, ticket_id: str) -> str | None:
    text = (repo / ".lanegate" / "tickets" / f"{ticket_id}.md").read_text()
    match = re.search(r"review_independence:\s*(\S+)", text)
    return match.group(1) if match else None


def assert_merged(repo: Path, tickets: list[str], *, require_independent: bool) -> None:
    failures = []
    for ticket_id in tickets:
        status = ticket_status(repo, ticket_id)
        if status != "merged":
            failures.append(f"{ticket_id}: status is {status!r}, expected 'merged'")
            continue
        independence = review_independence(repo, ticket_id)
        if require_independent and independence != "independent":
            failures.append(
                f"{ticket_id}: review_independence={independence!r}, expected 'independent' "
                "(self-review collapse -- check for an explicit reviewer:/steps.review.driver pin)"
            )
    if failures:
        raise SmokeFailure("; ".join(failures))


def assert_sample_suite_green(bindir: Path, repo: Path) -> None:
    result = run([bindir / "python", "-m", "pytest", "-q"], cwd=repo, timeout=60)
    if "failed" in result.stdout.lower():
        raise SmokeFailure(f"sample repo test suite not green after merge:\n{result.stdout[-2000:]}")


def _run_check(name: str, action, failures: list[tuple[str, str]]):
    print(f"[orchestration-smoke] {name}", flush=True)
    try:
        value = action()
    except SmokeFailure as exc:
        failures.append((name, str(exc)))
        print(f"  FAIL: {exc}", file=sys.stderr, flush=True)
        return None
    print("  ok", flush=True)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="LaneGate source root to install (default: this repo)")
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK, help="Persistent working directory (default: ~/ai/tests/lanegate-e2e)")
    args = parser.parse_args()

    failures: list[tuple[str, str]] = []
    work = args.work_dir
    setup_work_dir(work)
    bindir = setup_venv(work, args.source)
    repo = setup_sample_repo(work)
    configure_executors(bindir, repo)

    # Phase 1: single ticket, every level start to end (analyze -> implement
    # -> review -> merge), with a real executor and no manual intervention.
    single_ticket = _run_check(
        "1/2 single-ticket lifecycle (start to end, one ticket)",
        lambda: create_and_open_ticket(
            bindir,
            repo,
            "Add a fahrenheit_to_celsius(f) function to converter/units.py mirroring "
            "the existing celsius_to_fahrenheit, with a CLI subcommand f2c and a test "
            "in tests/test_units.py",
        ),
        failures,
    )
    if single_ticket is not None:
        orch_output = _run_check(
            "1/2 single-ticket orchestrate run",
            lambda: orchestrate(bindir, repo, [single_ticket], max_parallel=1),
            failures,
        )
        if orch_output is not None:
            _run_check(
                "1/2 single-ticket reached merged + independent review",
                lambda: assert_merged(repo, [single_ticket], require_independent=True),
                failures,
            )

    # Phase 2: a few tickets, real concurrent orchestration end to end.
    multi_tickets = _run_check(
        "2/2 multi-ticket setup (create + open, no hand edits)",
        lambda: [
            create_and_open_ticket(
                bindir,
                repo,
                "Add a km_to_miles(km) function to converter/units.py as the inverse "
                "of miles_to_km, with a CLI subcommand km2m and a test in tests/test_units.py",
            ),
            create_and_open_ticket(
                bindir,
                repo,
                "Add input validation to converter/units.py: miles_to_km and "
                "celsius_to_fahrenheit should raise TypeError with a clear message "
                "when passed a non-numeric value, with tests covering both",
            ),
        ],
        failures,
    )
    if multi_tickets is not None:
        orch_output = _run_check(
            f"2/2 multi-ticket orchestrate run ({len(multi_tickets)} tickets)",
            lambda: orchestrate(bindir, repo, multi_tickets, max_parallel=2),
            failures,
        )
        if orch_output is not None:
            _run_check(
                "2/2 multi-ticket reached merged + independent review",
                lambda: assert_merged(repo, multi_tickets, require_independent=True),
                failures,
            )

    _run_check(
        "sample repo test suite green on main after all merges",
        lambda: assert_sample_suite_green(bindir, repo),
        failures,
    )

    print(f"\n[orchestration-smoke] results left under {work}")
    if failures:
        names = ", ".join(name for name, _ in failures)
        print(f"[orchestration-smoke] FAILED checks: {names}", file=sys.stderr)
        return 1
    print("[orchestration-smoke] all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
