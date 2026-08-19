"""Tests for lanegate/timeutil.py — canonical UTC timestamp formatting."""

from __future__ import annotations

import re

from lanegate.timeutil import utc_now_iso


def test_utc_now_iso_format():
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", utc_now_iso())
