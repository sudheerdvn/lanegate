# Security Policy

## Reporting Vulnerabilities

If you discover a security vulnerability in LaneGate, please report it privately rather than opening a public GitHub issue. Send details to the maintainer via GitHub's private vulnerability reporting feature (Security → Report a vulnerability). Include a description of the issue, reproduction steps, and an assessment of the impact. This is a personal project — I'll respond as promptly as I can but there is no guaranteed SLA.

Do not include actual credentials, private keys, or production system details in your report.

## LaneGate's Security Model

LaneGate is a local coordinator for coding agents. When you run `lanegate orchestrate`, it spawns AI agent subprocesses (Claude, aider, Codex, or others) that read and write files inside git worktrees. Those agents execute with the same OS permissions as the user who ran LaneGate.

**What LaneGate does:**
- Runs each ticket in its own git worktree, one worktree per ticket
- Scans agent-produced diffs for out-of-scope file modifications before accepting them
- Blocks commits to CI/CD configs, dependency manifests, credential-shaped files, and LaneGate's own source
- Scans ticket text fields for prompt injection patterns before dispatch
- Runs static analysis (gitleaks, semgrep/bandit, pip-audit, npm-audit) on worktree diffs when the tools are installed
- Runs configured safeguards such as tests or scripts before completion or merge
- Wraps ticket content in `<untrusted-data>` tags to separate it from the trusted instruction layer in executor prompts

**What LaneGate does NOT do:**
- LaneGate does not run agents inside a sandbox (no bwrap, no seccomp, no container). Agents execute with full user-level filesystem and network access.
- LaneGate does not verify what the agent did inside the worktree at the shell level — it checks the git diff output, not the process's syscalls.
- LaneGate does not prevent the agent from making outbound network connections.
- LaneGate does not prove that touch-disjoint tickets are semantically independent. File-level locks can miss cross-file API, schema, import, type, and runtime dependencies.

For the complete threat model, trust boundary definitions, per-executor permission matrix, MCP trust model, sandbox limitations, and safe usage recommendations, see [docs/security-model.md](docs/security-model.md).

## Supported Versions

LaneGate is currently in pre-release (v0.x). Security fixes are applied to the latest release only. Once V1 ships, a supported-versions table will be added here.

## Safer Operating Pattern

For repositories you care about, configure deterministic safeguards in `.lanegate.yml`, keep `max_parallel` low, and run `lanegate orchestrate --human-review final` so you inspect diffs before merge. Use `reviewer: human` with `--human-review per_ticket` when each ticket needs its own recorded human verdict. The `protected_paths` and `security_sensitive_paths` config keys can add project-specific hard blocks, but they are not a sandbox.
