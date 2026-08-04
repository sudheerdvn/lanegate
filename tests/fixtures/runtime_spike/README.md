# Runtime Spike Fixtures

TICK-118 validates the read-only Go TUI boundary with the concrete board
fixture at `lanegate/tui_spike/testdata/board.json`.

That location is intentional for this spike: `go test ./lanegate/tui_spike`
loads the same fixture the prototype can render, and `pyproject.toml` force
includes it with the Go source so the Python `lanegate tui --fixture` launch path
can still be exercised after packaging. Future API-contract fixture sets should
move toward the broader `tests/fixtures/tui_contracts/` layout described in
`docs/tui-plan.md`.
