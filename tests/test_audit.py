"""Tests for the audit-refactor analysis and ticket DAG generator."""

from pathlib import Path

from lanegate.orchestrate.audit import (
    cluster_top_level_definitions,
    cmd_audit_refactor,
    scan_oversized_python_files,
)
from lanegate.ticket import parse_ticket


_CFG = {"ticket_prefix": "TICK", "tickets_dir": "tickets", "commit_status_changes": False}


def _large_module(path: Path) -> None:
    path.write_text(
        "class Worker:\n    pass\n\n"
        "def helper():\n    return 1\n\n"
        "async def async_helper():\n    return 2\n" + "# filler\n" * 8,
        encoding="utf-8",
    )


def test_audit_refactor_and_decompose_cli_arguments():
    from lanegate.cli import build_parser

    parser = build_parser()
    audit = parser.parse_args(["audit-refactor", "--threshold", "700", "--milestone", "cleanup"])
    decompose = parser.parse_args(["decompose", "lanegate/service.py", "--milestone", "cleanup"])

    assert (audit.cmd, audit.threshold, audit.milestone) == ("audit-refactor", 700, "cleanup")
    assert (decompose.cmd, decompose.file, decompose.milestone) == (
        "decompose", "lanegate/service.py", "cleanup"
    )


def test_scan_oversized_files_ignores_gitignored_and_virtualenv_paths(tmp_path):
    source = tmp_path / "service.py"
    _large_module(source)
    ignored = tmp_path / "ignored.py"
    _large_module(ignored)
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    venv = tmp_path / ".venv"
    venv.mkdir()
    _large_module(venv / "vendor.py")

    assert scan_oversized_python_files(tmp_path, threshold=5) == [source]


def test_clusters_top_level_classes_and_functions_in_source_order(tmp_path):
    source = tmp_path / "service.py"
    _large_module(source)

    assert cluster_top_level_definitions(source) == ["Worker", "helper", "async_helper"]


def test_audit_refactor_emits_touch_isolated_dependency_pair(tmp_path):
    (tmp_path / "tickets").mkdir()
    source = tmp_path / "service.py"
    _large_module(source)

    pairs = cmd_audit_refactor(_CFG, tmp_path, threshold=5, milestone="modularize")

    assert pairs == [("TICK-001", "TICK-002")]
    extraction = parse_ticket(tmp_path / "tickets" / "TICK-001.md")
    wiring = parse_ticket(tmp_path / "tickets" / "TICK-002.md")
    assert extraction["status"] == wiring["status"] == "draft"
    assert extraction["touches"] == ["service_extracted.py"]
    assert wiring["touches"] == ["service.py"]
    assert wiring["depends_on"] == ["TICK-001"]
    assert extraction["milestone"] == wiring["milestone"] == "modularize"
