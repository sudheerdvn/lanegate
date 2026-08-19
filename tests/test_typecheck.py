from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_typecheck_clean_mypy() -> None:
    """mypy lanegate must exit 0 with zero type errors."""
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "lanegate"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"mypy reported type errors:\n{result.stdout}\n{result.stderr}"
    )
