# LaneGate Security Model

This document describes LaneGate's threat model, trust boundaries, executor permission matrix, MCP trust model, sandbox limitations, and safe usage recommendations. It is written for developers evaluating whether LaneGate is appropriate for their environment.

---

## What LaneGate is and is not

LaneGate is a local coordinator for coding agents: it reads ticket files, dispatches AI agents to implement them inside separate git worktrees, and gates merges behind diff inspection and optional static analysis. LaneGate is not a sandboxed execution environment. It does not run containers, does not enforce syscall policy, and does not intercept network traffic. The security controls LaneGate provides are at the git and process level, not at the kernel level.

---

## Threat model

### Assets being protected

- **Your source tree.** The primary concern is agents modifying files they should not touch: CI/CD configs, dependency manifests, credential files, or LaneGate's own source.
- **Your credentials and secrets.** Agents running in the user account can read any file the user can. A misconfigured ticket or a prompt injection that directs an agent to exfiltrate `.env` files is a realistic risk.
- **The orchestrator's integrity.** A compromised ticket could attempt to modify LaneGate's own source or config to alter behavior in subsequent runs.
- **The trust chain between ticket content and agent instructions.** Ticket text is user-supplied (or imported from external sources) and may contain adversarial content intended to hijack the agent's behavior.

### Threats in scope

| Threat | LaneGate's mitigation |
|---|---|
| Agent modifies out-of-scope files | `touches` list enforcement: files committed outside the declared list route to `needs_review` |
| Agent modifies CI/CD, credentials, or LaneGate source | Hard-blocked file list checked against every committed file. A match routes to `needs_review` |
| Malicious ticket content hijacks agent instructions | Ticket fields placed in `<untrusted-data>` tags. An injection signal scanner checks title, body, and close_criteria before dispatch |
| Agent commits secrets to the worktree branch | gitleaks runs on the worktree diff when installed |
| Agent introduces vulnerable dependencies or insecure code | pip-audit, npm-audit, semgrep/bandit run on changed files when installed |
| Ticket imported from external source carries hidden instructions | `trusted: false` flag on imported tickets adds a trust notice to the executor prompt, and the `source` field is logged |
| Two orchestrators running concurrently corrupt ticket state | PID-based orchestrator lock (`acquire_orchestrator_lock`) prevents concurrent board-clearing runs on the same repo |

### Threats out of scope

- **Kernel-level isolation.** Agents run as the invoking user with no bwrap, seccomp, or namespace confinement.
- **Network egress control.** Agents can make outbound network connections. LaneGate does not filter or proxy them.
- **Agent process inspection.** LaneGate checks what the agent committed to git, not what the agent's process did at the syscall level.
- **Multi-machine ticket races.** The file-based lock is local. Two machines cloning the same repo and running orchestrate at the same time can both claim the same ticket. `check_local_not_behind_remote` reduces this window but does not close it.

---

## Trust boundaries

### Trusted

- **LaneGate's own process and source.** The orchestrator process itself is the trust root. `lanegate/*.py` source is **not hard-blocked** so the dogfooding repo is not forced into `needs_review` on every ticket. Control configuration (`.lanegate.yml`) remains hard-blocked.
- **The `.lanegate.yml` control config.** Hard-blocked so agents cannot lower their own permission thresholds. For lifecycle CLI commands launched from a linked worktree, config discovery is anchored to the shared control checkout; an uncommitted `.lanegate.yml` planted in the worktree is ignored.
- **The prompt instruction layer.** The `build_implement_prompt` function places LaneGate's instructions before an `<untrusted-data>` fence. The executor prompt template (from `prompts/implement.md` or the built-in default) is considered trusted.
- **Human-authored ticket frontmatter fields** that are machine-controlled: `status`, `worktree`, `branch`, `pr_number`, `review_verdict`. These are written by the orchestrator, not read from user input.

### Untrusted

- **Ticket text fields.** `title`, `_body` (the Markdown body below the frontmatter), and `close_criteria` are user-supplied and placed inside `<untrusted-data>` in the executor prompt. They are scanned for injection signals before dispatch.
- **Externally imported tickets.** Tickets with `trusted: false` (set manually or by a future import mechanism) receive an additional trust notice in the executor prompt instructing the agent to follow only the close criteria.
- **Agent-produced file diffs.** Everything the agent writes is treated as untrusted until it passes the scope check, blocked-file check, sensitive-paths check, and static analysis gate.
- **MCP tool calls from agents.** See the MCP section below.

---

## Executor permissions

Each executor type is a subprocess launched by the orchestrator. All share the same baseline: they run as the invoking OS user and inherit its environment variables, filesystem access, and network access. The differences between executors are in what flags LaneGate passes and how the executor interprets them.

| Executor | Binary | Filesystem access | Network access | Shell access | Notes |
|---|---|---|---|---|---|
| `claude` / `claude-process` | `claude` (Claude Code CLI) | Full user-level read/write | Yes (API calls + any tools the agent uses) | Yes (tools can run shell commands) | Headless operation needs one of: the bypass flag, a non-interactive `--permission-mode`, or an `--allowedTools`/`--disallowedTools` set (see below) |
| `aider` | `aider` | Full user-level read/write | Yes (API calls) | Limited (aider manages git commits directly) | `--yes-always` auto-confirms all prompts, and aider restricts edits to files named in its invocation |
| `codex` | `codex` | Full user-level read/write | Yes (API calls) | Yes | `--approval-policy=never` disables per-action approval |
| `ollama` | `ollama` | Depends on model/tool use | Local only (no external API) | Depends on model/tool use | Fully local inference, no external API calls |

### Headless permission options for the `claude` executor

`lanegate run` runs the Claude executor unattended, so it needs a permission configuration that never blocks on interactive input. Without one, the Claude Code process hangs waiting for terminal input that never comes. `lanegate doctor` and `lanegate init` recommend a scoped configuration first. The fully-open bypass flag remains supported and is the right choice for some setups, such as a sandboxed CI runner where the whole container is already the trust boundary.

**Scoped: `--allowedTools` / `--disallowedTools` (recommended default).** `lanegate init` now writes `flags: ["--allowedTools", "Bash,Edit,Write,Read,Glob,Grep"]` by default. This auto-approves only the tool categories orchestrate's implement/fix/review steps actually use. Anything outside the list (`WebFetch`, `WebSearch`, MCP tools, `NotebookEdit`, ...) stays gated rather than silently permitted. This is not a sandbox: `Bash` itself still runs arbitrary shell commands with no argument restriction. See the OS-level sandboxing tickets (v2) for that layer. Edit the list in `.lanegate.yml` if your workflow needs more tools, e.g. add `WebSearch` for a ticket that requires it.

**Scoped: a non-interactive `--permission-mode`.** Values like `acceptEdits`, `auto`, `dontAsk`, or `bypassPermissions` also avoid the interactive hang (unlike `manual` or `plan`, which are confirmation-first and will still block headless).

**Fully open: `--dangerously-skip-permissions`.** This flag disables every permission check process-wide, including tools outside any allowlist. With it, the agent can read and write files, run shell commands, and use any MCP tool it has access to without asking. This remains fully supported for existing configs and for setups, like a disposable CI container, where the process boundary already is the trust boundary. It is a deliberate trade-off, not a misconfiguration, when chosen deliberately. It is no longer what `lanegate init` writes by default.

Users who want per-action approval for every step, rather than either a scoped allowlist or full bypass, should not use `lanegate run`. They should use `lanegate start` manually and interact with the agent directly.

---

## MCP trust model

`lanegate mcp` starts a stdio MCP server that exposes LaneGate's own commands as native tools: `board`, `next_ticket`, `pipeline_status`, `repo_status`, `recent_logs`, `continuation_context`, `flag_list`, `flag_set`, `start`, `orchestrate`, `complete`, `review`, `merge`, `promote`, `hibernate`, `needs_review`, `fail`, `reopen`, `validate`, `done`, `stats`, and `update_docs`. When an MCP-compatible agent (e.g. Claude with LaneGate configured as an MCP server) calls these tools, the calls execute with the same filesystem and state access as the LaneGate process itself.

**What this means for trust:**

- An agent that has access to the `start` tool can claim any open ticket and create a worktree and branch in the repository. No additional authentication is required.
- An agent that has access to the `merge` tool can merge a ticket branch into main. No git credential prompt is required beyond what `git` itself enforces.
- The MCP server does not authenticate the calling agent. It trusts that the MCP client configuration (e.g. Claude Desktop's `claude_desktop_config.json`) enforces which agents get which tools.
- When `lanegate mcp` is used in a Claude Code session where the agent also has shell access and file-write access, the MCP tools are additive: the agent can already do everything the tools do via shell commands. The MCP server's value is ergonomic, not a security boundary.

**Recommendation:** Do not expose the LaneGate MCP server to untrusted agents or over a network transport. The stdio transport is inherently local. If you bridge it to a networked transport, add authentication at the transport layer.

---

## Sandbox limitations

LaneGate does not sandbox agents at the OS level. The following limitations are documented here so you can make an informed decision:

**No process isolation.** Agents run as child processes of the orchestrator with full user permissions. There is no bwrap, no unshare namespace, no Docker wrapping. A misbehaving or compromised agent can read any file the user can read, write to any path the user can write, and make arbitrary network connections.

**No syscall filtering.** LaneGate does not apply seccomp or AppArmor/SELinux profiles to agent subprocesses.

**No network egress filtering.** Agents can exfiltrate data over the network. LaneGate's gitleaks scan catches secrets committed to git but cannot catch data sent directly over HTTP/HTTPS from within the agent process.

**Git diff inspection is the primary containment mechanism.** After the agent exits, LaneGate inspects the worktree diff. Files committed outside the `touches` list are caught, and hard-blocked files (including `.lanegate.yml`) route to `needs_review` even when declared in `touches`. Lifecycle CLI commands launched from a linked worktree still resolve their configuration from the shared control checkout, so an uncommitted local config cannot disable their gates. Files read but not committed are not observable. The static analysis gate catches common secret patterns and known-vulnerable dependencies in committed code but is not a substitute for OS-level isolation.

**The orchestrator lock is file-local.** The PID-based lock at `.lanegate/orchestrator.lock` prevents two `lanegate run` invocations on the same checkout from racing. It does not coordinate across separate clones or separate machines.

The runner/sandbox contract described in [V1.5 Interface Boundaries](v2-interface-boundaries.md#tick-109-runnersandbox-contract) is an optional future design, not current enforcement. Until a runner is explicitly configured and wired into orchestration, executors continue to run as host processes by default with the permissions described above.

Any future runner is a process-supervision boundary, not the owner of ticket lifecycle. If the runner cannot launch an executor, cannot apply a requested enforce-mode sandbox, crashes, loses logs, or reports an internal error, Python core must treat that as a runner failure. Runner failure must not mark a ticket complete, approve review, merge work, or otherwise advance or corrupt lifecycle state. The safe outcome is to preserve or pause the ticket with enough log references for recovery.

Bubblewrap (bwrap) sandboxing on Linux and container-based isolation on other platforms are being explored through that optional runner boundary. No timeline is committed. This is a personal project, and the schedule depends on how stable that integration turns out to be.

---

## Safe usage recommendations

### Run LaneGate as a dedicated low-privilege user

The safest configuration is to create a dedicated OS user account for unattended agent runs and clone the repository under that account. This limits what an agent can access to that user's home directory and the repository. Do not run unattended orchestration sessions as your primary user account if the account has access to SSH keys, cloud credentials, or other sensitive material.

### Review worktree diffs before merging

`lanegate run` with `--human-review final` stops after implementation is complete and leaves tickets in `in_review` state. You then run `lanegate board` to see what is ready, inspect the worktree diff with `git diff main...<branch>`, and run `lanegate merge <id>` only when you are satisfied. This is the recommended mode for any repository containing production code.

`--human-review per_ticket` pauses after each ticket only when the resolved reviewer is `human` (for example, `reviewer: human` in `.lanegate.yml`). Otherwise it can run a separate review agent per ticket, which is useful as an automated second opinion but does not replace human judgment for sensitive changes. **Note:** `--human-review per_ticket` has no effect in combined mode, where the implement and review steps use the same executor and the agent self-reviews as part of its implementation prompt. If you want an independent review step, configure `executor_steps` to use a separate reviewer, or use `--human-review final` for a batch-level human gate.

Same-executor review (whether the review-independence ladder degraded to `self`, or an explicit same-executor pin) is not just the implementer talking to itself mid-thought: outside combined mode, it's a genuinely separate, cold subprocess dispatch by default — no `--resume`, no shared context with the session that wrote the code — specifically so an "independent" review isn't just the same reasoning trail rubber-stamping itself. See [`session_chaining.chain_review`](config-reference.md#session-chaining-resume-across-pipeline-steps) for the one opt-in exception to this (off by default) and [Combined mode](config-reference.md#combined-mode-default-for-single-accountsingle-model-setups) for when review has no separate dispatch to begin with.

The default (`--human-review none`) auto-approves and auto-merges. Use this only on low-risk repositories such as personal projects or sandboxed demo repos. For a project-wide safe default that doesn't depend on remembering the flag on every invocation, set `default_human_review: per_ticket` (or `final`) in `.lanegate.yml`. `cmd_orchestrate` falls back to this value whenever `--human-review` isn't passed explicitly on the CLI. An explicit `--human-review` flag always overrides it, including `--human-review none` on a project that sets a stricter config default.

**`autonomy` does not gate merges.** A ticket's `autonomy: supervised` (the default) or `autonomy: full` field only controls whether `orchestrate` may auto-fix and retry on a `changes_requested` verdict. It has no effect on whether an approved ticket auto-merges. That gate is `human_review`/`default_human_review` alone. A ticket marked `supervised` on a project running with `human_review: none` (or no `default_human_review` override) still auto-merges the moment it's approved.

### Use `security_sensitive_paths` for auth and payment code

Add a `security_sensitive_paths` list to `.lanegate.yml` for files where agent modifications should always require human review, regardless of the `autonomy` setting on the ticket:

```yaml
security_sensitive_paths:
  - "src/auth/**"
  - "src/payments/**"
  - "*.pem"
```

Any commit touching a path that matches this list is automatically routed to `needs_review`.

### Keep `touches` lists narrow

A ticket with a broad `touches` list (e.g. `touches: ["src/"]`) weakens the scope enforcement check. Write tickets with the specific files the agent should modify. Narrow `touches` lists also reduce merge conflicts when multiple agents run in parallel.

### Install the optional static analysis tools

LaneGate's static analysis gate is only as strong as the tools installed. gitleaks and semgrep run regardless of language when installed. Dependency-vulnerability scanning is per-ecosystem and only runs when the matching manifest changed: pip-audit (Python), npm audit (JS/Node), composer audit (PHP), bundler-audit (Ruby). There is no built-in dependency-vulnerability scan for Java/Gradle or .NET manifests yet. The manifests are still hard-blocked from unreviewed commits (see above), but no vulnerability scan runs against them. Wire one in via a project `safeguards` script if needed. Run `which gitleaks semgrep bandit pip-audit composer bundle-audit` to confirm the tools you need are on the PATH before running the orchestrator.

### Do not store credentials in files the agent can read

Avoid placing `.env` files, AWS credentials, SSH private keys, or API tokens in the repository working directory or in paths reachable from the agent's working directory. Use a secrets manager or the system keyring (`keyring` on Linux/macOS) instead. LaneGate's hard-blocked list includes `.env`, `.env.*`, `*.pem`, `*.key`, and `secrets.*`, but this only prevents the agent from committing those files. It does not prevent the agent from reading them.

### Audit tickets before running orchestrate

The ticket injection scanner (`_scan_injection_signals`) checks title, body, and close_criteria against known prompt injection patterns. It is a heuristic and not a complete defense. Before running `lanegate run --all`, review the ticket queue with `lanegate board` and manually inspect any ticket marked as untrusted (`trusted: false`).

### Use the MCP server only with trusted agents

The `lanegate mcp` server grants the calling agent full control over ticket lifecycle and deployment promotion. Only configure it for agents you trust with those actions. Do not expose the stdio MCP server over an unauthenticated network transport.
