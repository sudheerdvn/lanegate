# Agent Execution Guidelines

## Lanegate Ticket & Run Inspection
- Use `lanegate run-report` to inspect active or recent orchestrator runs, dispatched tickets, executor token usage, and orphaned processes.
- Use `lanegate board` to view overall status breakdowns across all lifecycle queues (`NEEDS REVIEW`, `IN REVIEW`, `FAILED`, `OPEN`, etc.).
- Inspect `.lanegate/tickets/TICK-<id>.md` and recent logs in `.lanegate/logs/` when diagnosing specific ticket failures, review findings, or lifecycle rollbacks.
- To supersede/retire an obsolete ticket in `failed` status, reopen it first before superseding: `lanegate reopen <id> && lanegate supersede <id> --reason "..."`.

## Codebase Navigation with Graphify
- For codebase questions and symbol lookups, first run `graphify query "<question or symbol>"` (with `--budget 2000` or `--budget 4000` for broader queries) when `graphify-out/graph.json` exists.
- Use `graphify affected "<symbol>"` to check downstream impacts and `graphify path "<A>" "<B>"` for relationship traces.
- If a natural-language query returns mostly docs or top-level overview nodes instead of code, refine the query with expected function/method/variable names before falling back to manual grep or file inspection.
- Only perform manual codebase search or file inspection when graph queries fail to return valid results.
- After modifying code, run `graphify update .` to keep the knowledge graph in sync.

## Independent Model Review Principle
- LaneGate's core design invariant is independent multi-model verification: every code change must be evaluated by a different model or model tier ("fresh set of eyes") against repository context, invariants, security guidelines, and close criteria.
- Model splitting applies across driver families (e.g. Codex, Claude, Terra) as well as model tiers within the same driver (e.g. Flash for implementation paired with Pro for review in `agy`).

## Worktree & Ticket Workflow
- **Strict Worktree Isolation**: NEVER make code edits or fixes directly on `main`. All modifications (including bug fixes, guard adjustments, and test updates) MUST be performed within a ticket's worktree (`lanegate create`, `lanegate start <id>`) and processed through the review and verification gates.
- **Clean Working Tree**: Ensure the control checkout on `main` remains pristine at all times to prevent merge conflicts with orchestrator and manual ticket merges.
- **Touches & Ticket Frontmatter Discipline**: NEVER commit `.lanegate/tickets/TICK-<id>.md` on a ticket branch. If additional files must be modified, claim them on `main` with `lanegate claim-file <path> <id>` before completing the ticket.

## Executor Scoping & Headless Invariants
- **Claude**: `--allowedTools Bash,Edit,Write,Read,Glob,Grep` (scoped headless permissions).
- **Codex**: `--dangerously-bypass-approvals-and-sandbox --ignore-user-config --ignore-rules --ephemeral` (do not combine with a separate `--sandbox`/`--approve-for-me` flag — the real `codex` CLI errors on that combination; see `docs/executor-capabilities.md`).
- **AGY**: `--dangerously-skip-permissions --disable-slash-commands` (suppresses skill/slash prompt bloat).
- **Aider**: `--yes-always --no-gitignore` with `repo_map: true` and `neutralize_touches: true`.

## Local Model Execution & Optimization
- **Qwen3.8-27B (RTX 5060 Ti 16GB)**: Quantization `Q3_K_M` / `IQ4_XS` on `llama-server` with `--mtp 2 -fa -c 32768 -ctk q4_0 -ctv q4_0` achieves ~45–47 tok/s.
- **Reasoning Monologue Handling**: Always strip `<think>.*?</think>` before parsing JSON responses from thinking/reasoning models.

## Engine Guardrails & Dogfooding Invariants
- **No Hardcoded Repo Paths**: The LaneGate core engine must remain repository-agnostic. Never hardcode `lanegate/...` source paths into `_BLOCKED_FILE_RULES` or control-plane guards. Project-specific protected or sensitive paths belong strictly in `.lanegate.yml` (`protected_paths`, `security_sensitive_paths`).

## Testing & Pytest Execution
- Always execute `pytest` with compact output flags (e.g., `pytest -q` or targeting specific test files) to conserve context window tokens.
- Avoid running unconstrained verbose full-suite pytest commands unless explicitly requested by the user.



