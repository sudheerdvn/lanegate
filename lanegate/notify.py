"""
notify.py — shared ntfy.sh push helper.

Split out of notify_watch.py so resume_watch.py can send pushes too without
a circular import (notify_watch imports _RATE_LIMIT_MARKER from
resume_watch; resume_watch importing send_ntfy back from notify_watch would
cycle).
"""

from __future__ import annotations

import urllib.error
import urllib.request

from lanegate import APP_NAME


def send_ntfy(topic: str, message: str, *, title: str = APP_NAME) -> bool:
    """POST a push notification to an ntfy.sh topic. Returns True on success."""
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers={"Title": title},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except (urllib.error.URLError, OSError):
        return False
