"""
update_docs.py — refresh README/ARCHITECTURE.md based on tickets completed since last doc update.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from lanegate.config import find_repo_root, load_config
from lanegate.executor import (
    build_executor_cmd,
    get_executor_config,
    resolve_executor_env,
)
from lanegate.ticket import branch_name, load_all_tickets


def get_doc_watermark(
    repo_root: Path,
    doc_paths: list[str] | None = None,
) -> str | None:
    """Return the git commit SHA of the latest commit touching any of the configured doc_paths.

    Determined via `git log -1 --format=%H -- <doc_paths>`.
    Returns None if no commit touches the doc paths (or git fails).
    """
    if not doc_paths:
        doc_paths = ["README.md", "docs/ARCHITECTURE.md"]

    cmd = ["git", "log", "-1", "--format=%H", "--"] + doc_paths
    result = subprocess.run(
        cmd,
        cwd=repo_root,
        capture_output=True,
        text=True, encoding="utf-8",
    )
    if result.returncode == 0:
        sha = result.stdout.strip()
        if sha:
            return sha
    return None


def _is_ticket_newer_than_watermark(
    repo_root: Path,
    ticket: dict,
    watermark: str,
) -> bool:
    """True if ticket has commits or status changes newer than watermark commit SHA."""
    ticket_path = ticket.get("_path")
    if ticket_path:
        try:
            rel_path = str(Path(ticket_path).relative_to(repo_root))
        except ValueError:
            rel_path = str(ticket_path)
        res = subprocess.run(
            ["git", "log", "-1", "--format=%H", f"{watermark}..HEAD", "--", rel_path],
            cwd=repo_root,
            capture_output=True,
            text=True, encoding="utf-8",
        )
        if res.returncode == 0 and res.stdout.strip():
            return True

    tid = ticket.get("id")
    if tid:
        res = subprocess.run(
            ["git", "log", "-1", "--format=%H", "-i", f"--grep={tid}", f"{watermark}..HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True, encoding="utf-8",
        )
        if res.returncode == 0 and res.stdout.strip():
            return True

        b = ticket.get("branch") or branch_name(tid)
        if b:
            res = subprocess.run(
                ["git", "log", "-1", "--format=%H", "-i", f"--grep={b}", f"{watermark}..HEAD"],
                cwd=repo_root,
                capture_output=True,
                text=True, encoding="utf-8",
            )
            if res.returncode == 0 and res.stdout.strip():
                return True

    return False


def enumerate_tickets_since_watermark(
    repo_root: Path,
    cfg: dict,
    watermark: str | None,
    status_filter: list[str] | set[str] | str | None = None,
) -> list[dict]:
    """Enumerate tickets matching status_filter whose merge/status commit is newer than watermark."""
    tickets_dir = repo_root / cfg.get("tickets_dir", ".lanegate/tickets")
    if not tickets_dir.exists():
        return []

    prefix = cfg.get("ticket_prefix", "TICK")

    all_tickets, _ = load_all_tickets(tickets_dir, prefix, cfg)

    if status_filter is None:
        doc_cfg = cfg.get("doc_update") or {}
        status_filter = doc_cfg.get("status_filter", ["done"])

    if isinstance(status_filter, str):
        allowed_statuses = {status_filter}
    else:
        allowed_statuses = set(status_filter)

    matching_status = [t for t in all_tickets if t.get("status") in allowed_statuses]

    if not watermark:
        return matching_status

    qualifying = []
    for t in matching_status:
        if _is_ticket_newer_than_watermark(repo_root, t, watermark):
            qualifying.append(t)

    return qualifying


def build_doc_update_prompt(tickets: list[dict], doc_contents: dict[str, str]) -> str:
    """Build the prompt for the doc update executor pass."""
    ticket_descriptions = []
    for t in tickets:
        tid = t.get("id", "UNKNOWN")
        title = t.get("title", "")
        close_criteria = t.get("close_criteria", "")
        touches = ", ".join(t.get("touches") or [])
        body = t.get("_body", "")
        ticket_descriptions.append(
            f"### Ticket {tid}: {title}\n"
            f"- Touches: {touches}\n"
            f"- Close Criteria: {close_criteria}\n"
            f"- Details: {body.strip()}\n"
        )

    docs_section = []
    for path, content in doc_contents.items():
        docs_section.append(f"--- File: {path} ---\n{content}\n")

    prompt = (
        "You are updating documentation for the project based on recently completed tickets.\n\n"
        "Here are the tickets completed since the last documentation update:\n\n"
        + "\n".join(ticket_descriptions) + "\n\n"
        "Here are the current contents of the documentation files:\n\n"
        + "\n".join(docs_section) + "\n\n"
        "Please review the completed tickets and update the documentation files (e.g. README.md, docs/ARCHITECTURE.md) "
        "to accurately reflect all new features, architectural changes, configuration options, CLI flags, and module additions.\n"
        "Make sure to preserve existing documentation structure and update/add sections as needed."
    )
    return prompt


def cmd_update_docs(
    cfg: dict | None = None,
    repo_root: Path | None = None,
    status: str | list[str] | None = None,
    dry_run: bool = False,
    executor_fn: Any | None = None,
) -> dict:
    """Ad-hoc command `lanegate update-docs` to refresh README/ARCHITECTURE.md from completed tickets."""
    if repo_root is None:
        repo_root = find_repo_root()
    if cfg is None:
        cfg = load_config(repo_root)

    doc_cfg = cfg.get("doc_update") or {}
    doc_paths = doc_cfg.get("doc_paths") or ["README.md", "docs/ARCHITECTURE.md"]
    status_filter = status if status is not None else doc_cfg.get("status_filter", ["done"])

    watermark = get_doc_watermark(repo_root, doc_paths)
    watermark_short = watermark[:8] if watermark else "none"

    tickets = enumerate_tickets_since_watermark(repo_root, cfg, watermark, status_filter=status_filter)

    if not tickets:
        msg = f"No new completed tickets found since doc watermark ({watermark_short}). Documentation is up to date."
        print(msg)
        return {
            "ok": True,
            "status": "no_op",
            "watermark": watermark,
            "tickets": [],
            "message": msg,
        }

    ticket_ids_str = ", ".join(t["id"] for t in tickets)
    print(f"Found {len(tickets)} completed ticket(s) since watermark {watermark_short}: {ticket_ids_str}")

    if dry_run:
        msg = f"[dry-run] Would run doc update for tickets: {ticket_ids_str}"
        print(msg)
        return {
            "ok": True,
            "status": "dry_run",
            "watermark": watermark,
            "tickets": [t["id"] for t in tickets],
            "message": msg,
        }

    doc_contents = {}
    for p in doc_paths:
        full_p = repo_root / p
        if full_p.is_file():
            doc_contents[p] = full_p.read_text(encoding="utf-8", errors="replace")

    prompt = build_doc_update_prompt(tickets, doc_contents)

    if executor_fn is not None:
        executor_fn(repo_root, prompt, tickets, doc_paths)
    else:
        executor = cfg.get("executor", "claude")
        model = cfg.get("models", {}).get("doc_update") or cfg.get("models", {}).get("implement")
        executor_cfg = get_executor_config(executor, cfg)
        executor_env = resolve_executor_env(executor_cfg)
        cmd = build_executor_cmd(executor, prompt, cfg, model=model)

        print(f"Running doc update executor ({executor})...")
        res = subprocess.run(
            cmd,
            cwd=repo_root,
            env=executor_env,
            capture_output=True,
            text=True, encoding="utf-8",
        )
        if res.returncode != 0:
            err = res.stderr or res.stdout
            print(f"ERROR: doc update executor failed: {err}", file=sys.stderr)
            raise RuntimeError(f"doc update executor failed: {err}")

    modified_docs = []
    for p in doc_paths:
        diff = subprocess.run(
            ["git", "diff", "--name-only", "--", p],
            cwd=repo_root,
            capture_output=True,
            text=True, encoding="utf-8",
        )
        status_check = subprocess.run(
            ["git", "status", "--porcelain", "--", p],
            cwd=repo_root,
            capture_output=True,
            text=True, encoding="utf-8",
        )
        if (diff.returncode == 0 and diff.stdout.strip()) or (status_check.returncode == 0 and status_check.stdout.strip()):
            modified_docs.append(p)

    if not modified_docs:
        msg = "Doc update pass completed with no file changes."
        print(msg)
        return {
            "ok": True,
            "status": "no_changes",
            "watermark": watermark,
            "tickets": [t["id"] for t in tickets],
            "message": msg,
        }

    for p in modified_docs:
        subprocess.run(["git", "add", p], cwd=repo_root, check=True)

    commit_msg = f"docs: refresh documentation from completed tickets ({ticket_ids_str})"
    commit_res = subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=repo_root,
        capture_output=True,
        text=True, encoding="utf-8",
    )
    if commit_res.returncode != 0:
        err = commit_res.stderr or commit_res.stdout
        print(f"ERROR committing doc changes: {err}", file=sys.stderr)
        raise RuntimeError(f"committing doc changes failed: {err}")

    new_watermark = get_doc_watermark(repo_root, doc_paths)
    new_watermark_short = new_watermark[:8] if new_watermark else "unknown"
    msg = f"Successfully updated docs and committed new watermark ({new_watermark_short})."
    print(msg)
    return {
        "ok": True,
        "status": "committed",
        "watermark": new_watermark,
        "tickets": [t["id"] for t in tickets],
        "modified_docs": modified_docs,
        "message": msg,
    }
