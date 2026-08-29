from pathlib import Path

def _write_draft_ticket(
    tickets_dir: Path,
    ticket_id: str,
    milestone: str | None = None,
) -> Path:
    ms_str = f"milestone: {milestone}\n" if milestone else ""
    content = (
        f"---\n"
        f"id: {ticket_id}\n"
        f"title: Draft {ticket_id}\n"
        f"status: draft\n"
        f"priority: 1\n"
        f"parallel_safe: true\n"
        f"{ms_str}"
        f"close_criteria: TBD.\n"
        f"---\nBody.\n"
    )
    path = tickets_dir / f"{ticket_id}.md"
    path.write_text(content)
    return path





