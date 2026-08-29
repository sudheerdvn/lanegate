from pathlib import Path


def _make_draft(
    tickets_dir: Path,
    ticket_id: str = "TICK-001",
    title: str = "Add foo command",
    touches: list[str] | None = None,
) -> Path:
    if touches:
        touches_yaml = "touches:\n" + "".join(f"  - {touch}\n" for touch in touches)
    else:
        touches_yaml = "touches: []\n"
    fm = f"id: {ticket_id}\ntitle: {title}\nstatus: draft\npriority: 3\n{touches_yaml}"
    path = tickets_dir / f"{ticket_id}.md"
    path.write_text(f"---\n{fm}---\n## Background\nWe need a foo command.\n")
    return path



_LARGE_ARCH_DOC = (
    "# Architecture Reference\n\n"
    "## Overview\n"
    + ("General background prose about the project. " * 20)
    + "\n\n"
    "## Orchestration Loop\n"
    "The orchestrate.py module implements the board-clearing loop. "
    + ("Detail sentence about orchestrate.py behavior. " * 20)
    + "\n\n"
    "## Delivery Axis\n"
    + ("Prose about promote.py and feature flags. " * 20)
    + "\n"
)

def _write_large_arch_doc(repo_root: Path) -> None:
    docs = repo_root / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "ARCHITECTURE.md").write_text(_LARGE_ARCH_DOC)


def _write_py(path: Path, source: str) -> None:
    """Helper: write a .py file, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
