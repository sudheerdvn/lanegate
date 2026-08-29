# Roadmap

This is a short list of likely next work. It is not a release schedule.

Current features are listed in the [README](README.md). Priorities may change.

---

## Now (V1 — shipped)

Local-first, git-native workflow for coding agents — ticket queue, parallel worktrees, review gates (independent review by default, degrading only when no other rung is available), and staged deployment, all stored in your repo with no SaaS. Launched 2026-08-02 under the current name, LaneGate. See the [README](README.md) for the full feature set.

---

## Next

Items that make the V1 coordination model more honest and easier to verify, plus broader executor and isolation support.

Done since the first public release: mypy across `lanegate/` in CI, repeated repo-wide duplicate-drift sweeps, driver-aware model/pin validation, the Cursor and Kiro executors, an OpenHands V1 rewrite, and native reviewer rotation.

Queued:

- A real OS-level sandbox — the current `executors.<name>.sandbox: worktree` opt-in is experimental (filesystem-only, not usable with linked worktrees, no network/syscall policy). Near-term: make it work with linked worktrees and extend it past `claude` executors; longer-term: the external runner boundary in [docs/v2-interface-boundaries.md](docs/v2-interface-boundaries.md).
- Dependency-aware scope hints using AST/import information
- `go vet`/`go build` wired into CI for the Go TUI module (`tui/`)
- Retest the discovery-guidance A/B for the `claude` executor on an over-10KB skeleton set
- Better safeguard recipes for Python, Node, Go, Rust, Java, and mixed repos — this is the one that matters for using LaneGate on a target project in any of these languages, independent of what LaneGate's own stack is
- Executor conformance tests for scope enforcement, hooks, and headless flags
- `lanegate doctor security` output that explains the current sandbox/review risk
- Config default centralization: deep-merge nested `.lanegate.yml` blocks, then sweep the scattered `cfg.get(key, literal)` call sites that currently stand in for it

---

## Later

Improve isolation while an agent is working.

- OS-level sandboxing so an agent can only touch its own worktree
- Block agents from pushing, adding remotes, or editing git hooks mid-run
- Optional network isolation for local runs
- Containerized executor option if it can be kept simple and explicit

---

## Beyond

- Shared, repo-local memory that every agent can draw on
- Smarter automatic choices about which agent handles which task
- Let separate machines or CI runners share one ticket queue
