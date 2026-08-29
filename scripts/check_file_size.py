#!/usr/bin/env python3
"""Grow-only file-size ratchet for LaneGate Python sources and tests."""
from __future__ import annotations
import argparse, subprocess
from pathlib import Path

SOFT_LIMIT = 1000
HARD_LIMIT = 1200


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], text=True, capture_output=True)


def _tracked() -> list[str]:
    result = _git("ls-files", "-z", "--", "lanegate/**/*.py", "tests/**/*.py")
    return [p for p in result.stdout.split("\0") if p]


def _lines(text: str | None) -> int:
    return 0 if text is None else len(text.splitlines())


def _git_file(spec: str) -> str | None:
    result = _git("show", spec)
    return result.stdout if result.returncode == 0 else None


def check(paths: list[str], *, mode: str, ref: str | None = None) -> tuple[list[str], list[str]]:
    warnings: list[str] = []; blocks: list[str] = []
    for path in paths:
        if not (path.startswith("lanegate/") or path.startswith("tests/")) or not path.endswith(".py"):
            continue
        if mode == "absolute":
            candidate = Path(path).read_text() if Path(path).exists() else ""
            baseline = None
        elif mode == "staged":
            candidate = _git_file(f":{path}")
            baseline = _git_file(f"HEAD:{path}")
        else:
            candidate = Path(path).read_text() if Path(path).exists() else ""
            baseline = _git_file(f"{ref}:{path}")
        count = _lines(candidate); base = _lines(baseline)
        if count > HARD_LIMIT and (mode == "absolute" or base <= HARD_LIMIT or count > base):
            blocks.append(f"{path}: {count} lines (split the file — do not bump the allowlist)")
        elif count > SOFT_LIMIT or (base > HARD_LIMIT and count <= base):
            warnings.append(f"{path}: {count} lines")
    return warnings, blocks


def main() -> int:
    parser = argparse.ArgumentParser(); group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--staged", action="store_true"); group.add_argument("--against"); group.add_argument("--absolute", action="store_true")
    parser.add_argument("paths", nargs="*"); args = parser.parse_args()
    mode = "staged" if args.staged else "absolute" if args.absolute else "against"
    paths = args.paths or _tracked()
    warnings, blocks = check(paths, mode=mode, ref=args.against)
    for message in warnings: print(f"WARNING: {message}")
    for message in blocks: print(f"BLOCK: {message}")
    return 1 if blocks else 0

if __name__ == "__main__": raise SystemExit(main())
