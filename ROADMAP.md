# Roadmap

This is a short list of likely next work. It is not a release schedule.

Current features are listed in the [README](README.md). Priorities may change.

---

## Now (V1)

Local-first, git-native workflow for coding agents — ticket queue, parallel worktrees, review gates, and staged deployment, all stored in your repo with no SaaS. See the [README](README.md) for the full feature set.

---

## Next

Make the V1 coordination model more honest and easier to verify.

- Dependency-aware scope hints using AST/import information
- Better safeguard recipes for Python, Node, Go, Rust, and mixed repos
- Executor conformance tests for scope enforcement, hooks, and headless flags
- `lanegate doctor security` output that explains the current sandbox/review risk

---

## Later

Improve isolation while an agent is working.

- OS-level sandboxing so an agent can only touch its own worktree
- Block agents from pushing, adding remotes, or editing git hooks mid-run
- Optional network isolation for local runs
- Containerized executor option if it can be kept simple and explicit

---

## Beyond

- Mix agents per task, for example one agent implements and another reviews
- Shared, repo-local memory that every agent can draw on
- Smarter automatic choices about which agent handles which task
- Let separate machines or CI runners share one ticket queue
