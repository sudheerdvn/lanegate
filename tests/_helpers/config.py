"""Shared configuration test helpers."""

from pathlib import Path


def write_config(path: Path, content: str) -> None:
    path.write_text(content)
