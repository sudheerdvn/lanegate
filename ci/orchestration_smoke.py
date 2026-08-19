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
import os
import re
import shutil
import subprocess
import sys
import time
import venv
from pathlib import Path

# Single source of truth for the inline/sidecar skeleton cutoff lives in
# executor.py (TICK-315); imported rather than duplicated so this script and
# the prompt builder can never silently drift apart on what ">10KB" means.
from lanegate.executor import _SKELETON_INLINE_THRESHOLD_BYTES as SKELETON_INLINE_THRESHOLD_BYTES

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "orchestration-smoke-sample"
DEFAULT_WORK = Path.home() / "ai" / "tests" / "lanegate-e2e"
DEFAULT_SOURCE = Path(__file__).resolve().parents[1]
STEP_POLL_TIMEOUT = 600  # 10 min ceiling for a single orchestrate invocation

# TICK-415: paired discovery-guidance A/B test constants. The baseline commit
# predates the sidecar-contradiction fix (790066d); current main should
# already have it. A frozen (hand-written, not analyze-generated) ticket with
# fixed touches is used so both arms dispatch the identical prompt -- a real
# `analyze` pass could pick different touches run to run.
DISCOVERY_AB_BASELINE_COMMIT = "d11c02e"
DISCOVERY_AB_TICKET_ID = "TICK-901"
DISCOVERY_AB_POOL = "claude-b-only"


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
  claude-b-only:
    executors: [claude-b]
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


def orchestrate(
    bindir: Path,
    repo: Path,
    tickets: list[str],
    max_parallel: int,
    *,
    pool: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    argv = [
        bindir / "lanegate",
        "run",
        "--tickets",
        ",".join(tickets),
        "--max",
        str(max_parallel),
        "--human-review",
        "none",
    ]
    if pool:
        argv += ["--pool", pool]
    run_env = {**os.environ, **env} if env else None
    result = subprocess.run(
        [str(a) for a in argv],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=STEP_POLL_TIMEOUT,
        env=run_env,
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


# ---------------------------------------------------------------------------
# TICK-415: paired claude discovery-guidance A/B test.
#
# TICK-403 dispatched a ticket whose skeleton set was only 6.8KB -- under the
# 10KB sidecar threshold -- so it never exercised the inline/sidecar branch
# that 790066d actually fixed. This harness freezes a ticket whose single
# touched file's own AST skeleton exceeds the threshold and dispatches it via
# the claude-b-only pool against both a pre-fix baseline commit and current
# main, checking whether the agent's first tool call is `lanegate symbols`
# (the fixed guidance) or a native Grep/grep fallback (the TICK-413
# regression).
# ---------------------------------------------------------------------------


def checkout_baseline(work: Path, source: Path, commit: str) -> Path:
    """Clone `source` into `work` and check out `commit`, isolated from the
    live source tree (never a worktree add against it -- this script must
    not mutate the repo it was invoked from)."""
    if work.exists():
        shutil.rmtree(work)
    run(["git", "clone", "-q", str(source), str(work)], cwd=work.parent, timeout=90)
    run(["git", "checkout", "-q", commit], cwd=work, timeout=30)
    return work


def freeze_large_skeleton_ticket(repo: Path, ticket_id: str = DISCOVERY_AB_TICKET_ID) -> Path:
    """Write a ticket directly with fixed touches, skipping `analyze` so both
    A/B arms dispatch the identical prompt -- a real analyze pass could pick
    different touches from run to run."""
    path = repo / ".lanegate" / "tickets" / f"{ticket_id}.md"
    lines = [
        "---",
        f"id: {ticket_id}",
        "title: Add a round-trip rounding helper to converter/cli.py",
        "status: open",
        "touches:",
        "  - converter/cli.py",
        "close_criteria: converter/cli.py exposes a round_km_to_miles(km) helper "
        "used by the km2m branch of main().",
        "---",
        "Add a small rounding helper near the existing validators in "
        "converter/cli.py and use it in the km2m branch of main().",
        "",
    ]
    path.write_text("\n".join(lines))
    run(["git", "add", "-A"], cwd=repo)
    run(["git", "commit", "-q", "-m", f"add frozen {ticket_id} for discovery-guidance A/B"], cwd=repo)
    verify_ticket_tracked(repo, ticket_id)
    return path


def first_tool_call(repo: Path, ticket_id: str) -> dict | None:
    """Return the first captured tool_use block from the implement step's
    executor-session.jsonl transcript (see orchestrate/audit.py), or None if
    no audit bundle/transcript was captured."""
    bundles_dir = repo / ".lanegate" / "executor-runs" / ticket_id
    if not bundles_dir.is_dir():
        return None
    session_dirs = sorted(
        (d for d in bundles_dir.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime
    )
    for session_dir in reversed(session_dirs):
        transcript = session_dir / "executor-session.jsonl"
        if not transcript.is_file():
            continue
        for line in transcript.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            for block in (entry.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    return {"name": block.get("name"), "input": block.get("input")}
    return None


def _is_symbols_discovery_call(call: dict | None) -> bool:
    """True for a `lanegate symbols` Bash call (the sidecar-fixed discovery
    path); False for a Grep tool use or a native grep/rg shell fallback (the
    TICK-413/790066d regression this A/B test guards against)."""
    if not call:
        return False
    if call.get("name") != "Bash":
        return False
    command = str((call.get("input") or {}).get("command", ""))
    if re.search(r"\bgrep\b", command, re.IGNORECASE):
        return False
    return "lanegate symbols" in command


def ticket_cost_stats(bindir: Path, repo: Path, ticket_id: str, db_path: Path) -> dict:
    """Sum turns/cost/cache-token usage across all step_costs rows logged for
    `ticket_id` in the isolated analytics DB at `db_path`."""
    script = (
        "import json, pathlib\n"
        "from lanegate.context_log import _load_step_costs_from_db, _get_project_id\n"
        f"project = _get_project_id(pathlib.Path({str(repo)!r}))\n"
        f"rows = [r for r in _load_step_costs_from_db(pathlib.Path({str(db_path)!r}), project) "
        f"if r.get('ticket_id') == {ticket_id!r}]\n"
        "print(json.dumps({\n"
        "    'turns': sum(r.get('num_turns') or 0 for r in rows),\n"
        "    'cost_usd': round(sum(r.get('cost_usd') or 0 for r in rows), 4),\n"
        "    'cache_read_tokens': sum(r.get('cache_read_tokens') or 0 for r in rows),\n"
        "    'cache_creation_tokens': sum(r.get('cache_creation_tokens') or 0 for r in rows),\n"
        "    'rows': len(rows),\n"
        "}))\n"
    )
    result = run([bindir / "python", "-c", script], cwd=repo, timeout=30)
    return json.loads(result.stdout.strip().splitlines()[-1])


def run_discovery_ab_arm(name: str, arm_dir: Path, source: Path) -> dict:
    """Run one full A/B arm: fresh venv + sample repo + claude-b-only pool +
    frozen >10KB-skeleton ticket, dispatched through the real orchestrate
    pipeline. Returns the captured first tool call, cost stats, and final
    ticket status."""
    setup_work_dir(arm_dir)
    bindir = setup_venv(arm_dir, source)
    repo = setup_sample_repo(arm_dir)
    configure_executors(bindir, repo)
    freeze_large_skeleton_ticket(repo)

    db_path = arm_dir / "analytics.db"
    orchestrate(
        bindir,
        repo,
        [DISCOVERY_AB_TICKET_ID],
        max_parallel=1,
        pool=DISCOVERY_AB_POOL,
        env={"LANEGATE_ANALYTICS_DB": str(db_path)},
    )
    return {
        "name": name,
        "tool_call": first_tool_call(repo, DISCOVERY_AB_TICKET_ID),
        "stats": ticket_cost_stats(bindir, repo, DISCOVERY_AB_TICKET_ID, db_path),
        "status": ticket_status(repo, DISCOVERY_AB_TICKET_ID),
    }


def assert_symbols_discovery(arm: dict) -> None:
    call = arm["tool_call"]
    if not _is_symbols_discovery_call(call):
        raise SmokeFailure(
            f"{arm['name']} arm: first tool call was {call!r}, expected a `lanegate symbols` "
            "Bash call (native Grep/grep fallback -- TICK-413/790066d sidecar-contradiction "
            "regression)"
        )


def run_discovery_ab(source: Path, work: Path, baseline_commit: str) -> int:
    """Entry point for `--discovery-ab`: paired dispatch of the frozen
    >10KB-skeleton ticket against `baseline_commit` and current `source`,
    asserting only the current-main arm used `lanegate symbols`. The baseline
    arm is expected to still show the pre-fix grep fallback and is recorded
    for comparison, not asserted on."""
    failures: list[tuple[str, str]] = []
    work.mkdir(parents=True, exist_ok=True)

    baseline_src = _run_check(
        f"checkout baseline {baseline_commit}",
        lambda: checkout_baseline(work / "baseline-src", source, baseline_commit),
        failures,
    )
    if baseline_src is None:
        print("[orchestration-smoke] FAILED checks: checkout baseline", file=sys.stderr)
        return 1

    baseline_arm = _run_check(
        f"arm 1/2: baseline {baseline_commit} ({DISCOVERY_AB_POOL}, frozen >10KB-skeleton ticket)",
        lambda: run_discovery_ab_arm("baseline", work / "baseline-arm", baseline_src),
        failures,
    )
    main_arm = _run_check(
        f"arm 2/2: current main ({DISCOVERY_AB_POOL}, frozen >10KB-skeleton ticket)",
        lambda: run_discovery_ab_arm("main", work / "main-arm", source),
        failures,
    )

    if main_arm is not None:
        _run_check(
            "main arm used `lanegate symbols`, not a native grep fallback",
            lambda: assert_symbols_discovery(main_arm),
            failures,
        )

    for arm in (baseline_arm, main_arm):
        if arm is None:
            continue
        call = arm["tool_call"] or {}
        stats = arm["stats"]
        print(
            f"[orchestration-smoke] {arm['name']:>8}: first_tool={call.get('name')!r} "
            f"turns={stats['turns']} cost_usd={stats['cost_usd']} "
            f"cache_read={stats['cache_read_tokens']} cache_creation={stats['cache_creation_tokens']}"
        )

    print(f"\n[orchestration-smoke] discovery-ab results left under {work}")
    if failures:
        names = ", ".join(name for name, _ in failures)
        print(f"[orchestration-smoke] FAILED checks: {names}", file=sys.stderr)
        return 1
    print("[orchestration-smoke] all checks passed")
    return 0


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
    parser.add_argument(
        "--discovery-ab",
        action="store_true",
        help=(
            "TICK-415: instead of the normal 2-phase smoke, run the paired claude "
            f"discovery-guidance A/B test ({DISCOVERY_AB_POOL} pool, frozen >10KB-skeleton "
            "ticket) against --baseline and --source, asserting the current-source arm's "
            "first tool call is `lanegate symbols` rather than a native grep fallback."
        ),
    )
    parser.add_argument(
        "--baseline",
        default=DISCOVERY_AB_BASELINE_COMMIT,
        help=f"Pre-fix commit to compare against with --discovery-ab (default: {DISCOVERY_AB_BASELINE_COMMIT})",
    )
    args = parser.parse_args()

    if args.discovery_ab:
        return run_discovery_ab(args.source, args.work_dir, args.baseline)

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
