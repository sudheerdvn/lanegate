from __future__ import annotations

import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("check_file_size", Path("scripts/check_file_size.py"))
assert SPEC and SPEC.loader
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


def _set_content(monkeypatch, candidate: int, baseline: int = 0) -> None:
    text = "x\n" * candidate
    monkeypatch.setattr(checker.Path, "read_text", lambda self: text)
    monkeypatch.setattr(checker.Path, "exists", lambda self: True)
    monkeypatch.setattr(checker, "_git_file", lambda spec: "x\n" * baseline if not spec.startswith(":") else text)


def test_new_file_at_1201_is_blocked(monkeypatch):
    _set_content(monkeypatch, 1201)
    warnings, blocks = checker.check(["lanegate/new.py"], mode="against", ref="main")
    assert not warnings and blocks


def test_new_file_at_1100_is_warned_but_allowed(monkeypatch):
    _set_content(monkeypatch, 1100)
    warnings, blocks = checker.check(["lanegate/new.py"], mode="against", ref="main")
    assert warnings and not blocks


def test_existing_over_hard_limit_cannot_grow(monkeypatch):
    _set_content(monkeypatch, 1301, 1300)
    _, blocks = checker.check(["lanegate/old.py"], mode="against", ref="main")
    assert blocks


def test_existing_over_hard_limit_may_shrink(monkeypatch):
    _set_content(monkeypatch, 1250, 1300)
    warnings, blocks = checker.check(["lanegate/old.py"], mode="against", ref="main")
    assert warnings and not blocks


def test_soft_limit_warns_and_small_file_is_silent(monkeypatch):
    _set_content(monkeypatch, 1050, 900)
    warnings, blocks = checker.check(["lanegate/file.py"], mode="against", ref="main")
    assert warnings and not blocks
    _set_content(monkeypatch, 900, 900)
    warnings, blocks = checker.check(["lanegate/file.py"], mode="against", ref="main")
    assert not warnings and not blocks
