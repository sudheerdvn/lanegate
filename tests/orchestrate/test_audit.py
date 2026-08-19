"""Regression coverage for executor audit-bundle formatting."""

from __future__ import annotations

import json

from lanegate.orchestrate import audit


def test_copy_formatted_jsonl(tmp_path, monkeypatch):
    """Audit transcripts are readable without turning JSONL into an array."""
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(
        '{"type":"session","payload":{"model":"gpt-5"}}\n'
        '{"type":"event","count":2}\n',
        encoding="utf-8",
    )

    copied = tmp_path / "copied.jsonl"
    detail = audit._copy_formatted_jsonl(transcript, copied)
    expected = (
        '{\n'
        '  "type": "session",\n'
        '  "payload": {\n'
        '    "model": "gpt-5"\n'
        '  }\n'
        '}\n'
        '{\n'
        '  "type": "event",\n'
        '  "count": 2\n'
        '}\n'
    )
    assert copied.read_text(encoding="utf-8") == expected
    assert detail["truncated"] is False

    monkeypatch.setattr(audit, "_find_codex_transcript", lambda status: (transcript, ""))
    monkeypatch.setattr(audit, "_run_git_snapshot", lambda *_: "")
    bundle = audit._capture_executor_audit_bundle(
        tmp_path,
        tmp_path,
        {
            "ticket_id": "TICK-460",
            "executor_session": "format-test",
            "executor": "codex",
        },
    )

    bundled = bundle / "executor-session.jsonl"
    assert bundled.read_text(encoding="utf-8") == expected
    decoder = json.JSONDecoder()
    content = bundled.read_text(encoding="utf-8")
    objects = []
    position = 0
    while position < len(content):
        while position < len(content) and content[position].isspace():
            position += 1
        if position == len(content):
            break
        obj, position = decoder.raw_decode(content, position)
        objects.append(obj)
    assert objects == [
        {"type": "session", "payload": {"model": "gpt-5"}},
        {"type": "event", "count": 2},
    ]
