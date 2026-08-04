"""
stats.py — time-in-status analytics: median time spent per ticket status.

Reads status_changed_at from ticket frontmatter. Tickets without this field
(created before TICK-082) are excluded from the per-status medians but listed
in the "no timestamp" count so the operator knows coverage is partial.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path


def _parse_iso(raw: str | None) -> datetime.datetime | None:
    """Parse an ISO-8601 UTC string; return None if absent or unparseable."""
    if not raw:
        return None
    try:
        return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _seconds_in_status(ticket: dict) -> float | None:
    """Return seconds since the ticket entered its current status, or None."""
    ts = _parse_iso(ticket.get("status_changed_at"))
    if ts is None:
        return None
    now = datetime.datetime.now(datetime.UTC)
    delta = (now - ts).total_seconds()
    return delta if delta >= 0 else None


def _format_duration(seconds: float) -> str:
    """Format a duration in seconds as a human-readable string."""
    s = int(seconds)
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    mid = n // 2
    if n % 2 == 1:
        return sorted_vals[mid]
    return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2.0


def cmd_stats(cfg: dict, repo_root: Path, *, json_output: bool = False) -> None:
    """Print median time-in-status for each status across all tickets."""
    from lanegate.ticket import load_all_tickets

    tickets_dir = repo_root / cfg["tickets_dir"]
    tickets, quarantined = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)

    # Accumulate time-in-current-status per status bucket
    buckets: dict[str, list[float]] = {}
    no_timestamp = 0
    for t in tickets:
        status = t.get("status", "unknown")
        secs = _seconds_in_status(t)
        if secs is None:
            no_timestamp += 1
            continue
        buckets.setdefault(status, []).append(secs)

    if json_output:
        result: dict = {}
        for status, values in sorted(buckets.items()):
            med = _median(values)
            result[status] = {
                "median_seconds": med,
                "median_human": _format_duration(med) if med is not None else "—",
                "count": len(values),
            }
        print(
            json.dumps(
                {
                    "time_in_status": result,
                    "tickets_without_timestamp": no_timestamp,
                },
                indent=2,
            )
        )
        return

    print("=== Time In Status ===\n")

    if not buckets:
        if no_timestamp:
            print(
                f"No tickets with status_changed_at yet ({no_timestamp} ticket(s) have no timestamp)."
            )
        else:
            print("No tickets found.")
        return

    # Display in standard status order
    from lanegate.ticket import _STANDARD_STATUSES

    order_map = {s: i for i, s in enumerate(_STANDARD_STATUSES)}
    sorted_statuses = sorted(buckets, key=lambda s: (order_map.get(s, 999), s))

    label_w = max(len(s) for s in sorted_statuses)
    for status in sorted_statuses:
        values = buckets[status]
        med = _median(values)
        med_str = _format_duration(med) if med is not None else "—"
        print(f"  {status:<{label_w}}  median {med_str:>6}  ({len(values)} ticket(s))")

    print()
    if no_timestamp:
        print(
            f"  {no_timestamp} ticket(s) have no status_changed_at (pre-TICK-082) — shown as '—' on board"
        )
