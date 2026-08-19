# Roadmap

This is a short list of likely next work. It is not a release schedule.

Current features are listed in the [README](README.md). Priorities may change.

---

## Now (V1 — shipped)

Local-first, git-native workflow for coding agents — ticket queue, parallel worktrees, review gates (independent review by default, degrading only when no other rung is available), and staged deployment, all stored in your repo with no SaaS. Launched 2026-08-02 under the current name, LaneGate. See the [README](README.md) for the full feature set.

---

## Next

Gates promoting work to the public repo, plus the items that make the V1 coordination model more honest and easier to verify.

Gating the public repo — LaneGate auditing its own tree, not something a user of LaneGate on another project would see:

- Retest the discovery-guidance A/B for the `claude` executor on an over-10KB skeleton set
- Run mypy across LaneGate's own Python tree (`lanegate/`) — no equivalent check exists yet for LaneGate's own Go TUI module (`tui/`); `go vet`/`go build` aren't wired into this repo's CI
- Repo-wide duplicate-drift sweep

Queued, not gating:

- Warn when a ticket's `model:` has no matching `executor:`/`reviewer:` pin
- Distinguish an actual review rejection from an approved-but-awaiting-merge ticket in the orchestrate run summary
- Dependency-aware scope hints using AST/import information
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
