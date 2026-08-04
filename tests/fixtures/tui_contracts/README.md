# TUI Contract Fixture Corpus

This directory holds fixture-first contract examples for the Go TUI planned
in `docs/tui-plan.md`. Fixtures describe Python-owned JSON and SSE contracts
that the Go client can consume without shelling out to `lanegate` or importing
Python code. `board/`, `ticket_detail/`, and `blocked/` back screens 1-3
(TICK-156); `diff/`, `run/`, and `settings/` back screens 4-6 (TICK-157).
`settings/` fixtures mirror the sanitized `GET /api/config` response (also
served at `/api/settings`) — see `lanegate/api.py`. `pools/` fixtures mirror
`GET /api/pools` (TICK-269): each `pools.<name>` entry's executors in
configured preference order plus persisted rotation/dispatch state
(TICK-268), backing the settings screen's pool executor reorder control.

Layout:

```text
tests/fixtures/tui_contracts/
  board/
    board_basic.json
    board_empty.json
    board_hidden_and_blocked.json
    tickets_flat.json
  ticket_detail/
    ticket_ready.json
    ticket_in_review.json
    ticket_changes_requested.json
    ticket_missing_optional_fields.json
  blocked/
    blocked_queue.json
    changes_requested_queue.json
    blocked_empty.json
  diff/
    diff_small.json
    diff_many_files.json
    diff_truncated_patch.json
    diff_binary_file.json
    diff_empty.json
  run/
    run_idle.json
    run_active.json
    run_completed.json
    executor_events_live.json
    executor_events_historical.json
    raw_audit_page.json
    events_basic.sse
    events_reconnect.sse
    events_worker_error.sse
  settings/
    settings_basic.json
    settings_missing_optional.json
    settings_multi_executor.json
  pools/
    pools_basic.json
    pools_empty.json
  errors/
    error_locked_touch.json
    error_missing_ticket.json
    error_remote_behind.json
    error_worktree_error.json
```

Fixture rules:

- Keep payloads shaped like API responses, including envelopes and structured
  error fields.
- Keep paths repo-relative unless the API contract intentionally returns an
  absolute local path.
- Include empty, partial, long-text, truncation, and error examples for every
  screen.
- Treat ticket bodies, review text, paths, and log messages as untrusted data
  to be displayed, not interpreted.
- Keep SSE files valid as `text/event-stream`, with event ids and JSON `data:`
  payloads where the API will stream structured events.
- The Run screen's default Activity pane uses only the safe structured
  `executor_events_*.json` envelopes from `GET /api/runs/{id}/events`; fixtures
  must not place executor transcript or protocol content in these payloads.
- `raw_audit_page.json` represents the paginated `/api/runs/{id}/logs` response
  and is used only by the explicit Raw Audit Log mode, where raw diagnostic
  output is intentionally retained behind the mode switch.
