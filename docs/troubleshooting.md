# Troubleshooting & FAQ

Common issues when running LaneGate on a new project. Each section describes the symptom, the most likely cause, and what to do about it.

---

## Setup

### `lanegate: command not found`

LaneGate is not on your PATH.

- If you installed via pip: check that your Python scripts directory is on `PATH`. Run `python -m lanegate --version` as a workaround, or add the scripts directory (e.g. `~/.local/bin` on Linux) to `PATH`.
- If you installed in a virtualenv: activate it first (`source venv/bin/activate`).
- If you installed from source with `pip install -e .`: the fix is the same as above. Activate the venv, or use `python -m lanegate` to invoke subcommands directly.

### `lanegate init` says a tickets directory already exists

Re-init safety is intentional. If `.lanegate/tickets/` or a custom `tickets_dir` already has files, LaneGate prints a warning and preserves the existing directory rather than overwriting it. Your tickets are safe. Read the warning, confirm the path is correct, and continue.

If you previously used a different location (e.g. `tickets/`) and want to switch to the new default, migrate the files manually and update `tickets_dir` in `.lanegate.yml`.

### Python version error on import

LaneGate requires Python 3.11 or later. Check with `python --version`. If your system default is older, install 3.11+ and either create a new venv or reinstall inside the newer interpreter.

---

## Configuration

### `.lanegate.yml` not found, so LaneGate ignores my config

LaneGate uses walk-up discovery: it searches the current directory and each parent until it finds `.lanegate.yml`. Run `lanegate board` from inside the repo root or any subdirectory. If the file is not being picked up, verify the file name is exactly `.lanegate.yml` (leading dot, no extra extension).

Note: `lanegate init` adds `.lanegate.yml` to `.gitignore` by default. The file is local, so check that it actually exists and was not deleted.

### Config change has no effect

LaneGate reads `.lanegate.yml` on every command invocation, so there is no running daemon to restart. If a change appears to have no effect, check:

1. You are editing the right file (walk-up discovery may be picking up a file in a parent directory).
2. The YAML is valid. A parse error causes LaneGate to exit with a traceback. Run `python -c "import yaml; yaml.safe_load(open('.lanegate.yml'))"` to check for syntax errors.

---

## Ticket issues

### Ticket is stuck in `draft`, so `lanegate board` shows it but `lanegate next` skips it

`draft` tickets are intentionally skipped by `lanegate next` until they are opened. A draft ticket needs a non-empty `touches` list and `open` status before an agent can claim it.

**If `lanegate create` already ran analysis (the default):** the touches are already populated. Just flip the status:

```bash
lanegate open TICK-NNN
```

**If touches are not yet populated:** run analysis first, which fills the touches list and opens the ticket in one step:

```bash
lanegate analyze TICK-NNN
```

**If you want to skip the model call entirely:** edit the ticket frontmatter directly to add a non-empty `touches` list, then run `lanegate open TICK-NNN`.

When a milestone filter is active, the board shows only tickets whose `milestone` matches, including draft tickets.

### `lanegate start` fails: "ticket has no touches"

A ticket without a `touches` list cannot be started, because the lock has nothing to hold. Populate touches first:

```bash
lanegate analyze TICK-NNN   # fill touches and open the ticket
# or edit the frontmatter manually, then:
lanegate open TICK-NNN      # flip to open once touches are set
```

### `lanegate start` fails: "ticket is not open"

`lanegate start` only accepts tickets in `open`, `hibernated`, or `needs_review` status. Check `lanegate board` to see the current status and act accordingly:

- **`draft`**: run `lanegate open TICK-NNN` (if touches are set) or `lanegate analyze TICK-NNN` (to fill touches and open in one step).
- **`in_progress`** (from a previous crashed run): manually reset `status: open` in the ticket frontmatter file and remove the stale worktree entry.
- Any other status: read the ticket file and decide whether it needs manual editing or a lifecycle command.

### Ticket is blocked even though the conflicting ticket is long merged

After `lanegate merge`, the lock is released. If a ticket still shows as blocked, check two things:

1. The merged ticket's status in `.lanegate/tickets/` is actually `merged` (not stuck in `in_review`).
2. The `touches` lists genuinely overlap. Use `lanegate board` to see which locks are held and by which ticket.

If a ticket's status is stuck at `in_progress` because the executor crashed, reset it manually in the frontmatter and clean up its worktree directory.

---

## Executor issues

### The agent hangs and never finishes, so orchestration stalls

The most common cause is missing headless flags. Executors block on interactive prompts unless told not to:

| Executor | Required flag |
|---|---|
| `claude` / `claude-process` | A scoped `--allowedTools`/`--disallowedTools` set, a non-interactive `--permission-mode`, or `--dangerously-skip-permissions` |
| `aider` | `--yes-always` |
| `codex` | `--approval-policy=never` |
| `ollama` | none (non-interactive by design) |

`lanegate init` writes the scoped `--allowedTools` form by default. Add or edit it in your `.lanegate.yml`:

```yaml
executors:
  claude:
    flags: ["--allowedTools", "Bash,Edit,Write,Read,Glob,Grep"]
```

See [Security Status](security-model.md#headless-permission-options-for-the-claude-executor) for the full set of options, including `--dangerously-skip-permissions`.

### `executor not found` or `No such file or directory` when orchestrating

LaneGate resolves the executor binary by name. Make sure the executor is installed and on `PATH` in the same environment where you run `lanegate`. For Claude Code: `which claude`. For aider: `which aider`. Install if missing, then re-run.

### `lanegate analyze` fails / tickets stay in `draft` after analyze

`analyze` calls the configured executor to inspect the ticket and suggest the files it should touch. If the executor is not installed or the API key is missing, it will fail. Check:

- Executor binary is on PATH.
- API key env var is set (`ANTHROPIC_API_KEY` for claude, `OPENAI_API_KEY` for codex/aider with OpenAI).
- You have network access to the API endpoint.

For testing without model access, skip analysis and populate `touches` and `close_criteria` manually in the ticket frontmatter, then run `lanegate open <id>` to flip to `open`.

### Ollama does not apply changes to files

Ollama is a model server, not a coding agent. It returns text but does not edit files or commit to git. To use Ollama as an implementation executor, you need a wrapper script that takes Ollama's output and applies it. The `executor: ollama` setting is suitable for experimentation or generating suggestions that a human then applies. See [docs/executor-capabilities.md](executor-capabilities.md) for details.

### Aider is very slow on a large repo or warns that its context is too large

Aider's repo-map scans the entire repository to build context. On large repos this can take minutes per ticket, and the selected files, project guidance, and Aider overhead can exceed a local model's context window. Always include a non-empty `touches` list in the ticket frontmatter. LaneGate passes those files as explicit Aider file arguments.

For a local route, configure the model's usable input budget so LaneGate fails before launching Aider instead of leaving partial Aider changes in the worktree:

```yaml
executors:
  aider:
    context_window_tokens: 32768
```

The preflight estimates the rendered prompt and selected files conservatively and includes an 8,192-token reserve. If it rejects a ticket, reduce the initial file set or prompt, use a model with a larger supported context window, or choose an executor that reads files incrementally. Raising the budget alone does not make a model accept more context than its server supports.

---

## TUI

### `lanegate tui` fails: "Go TUI binary or source not found"

`lanegate tui` is a separate Go binary (`tui/` in this repo). It is not part of the `lanegate` Python package, so `pip install lanegate` alone does not provide it. `lanegate tui` looks for a binary in this order: `LANEGATE_TUI_BIN` env var, `lanegate-tui` on `PATH`, then `go run ./cmd/lanegate-tui` if you have a checkout of this repo with the Go toolchain installed. If none of those resolve, it raises this error.

Fix: build the binary with `go build -o lanegate-tui ./tui/cmd/lanegate-tui` from a checkout of this repo, then either put it on `PATH` or point `LANEGATE_TUI_BIN` at it. If the error instead says the Go toolchain isn't on `PATH` (source is present but `go` is missing), install Go or build the binary elsewhere and set `LANEGATE_TUI_BIN`.

---

## Orchestration issues

### `lanegate orchestrate` exits immediately with no tickets processed

Check `lanegate board`: there may be no tickets in `open`, `hibernated`, or `needs_review` status. Drafts and tickets with empty `touches` are skipped. If the board shows open tickets but orchestrate skips them, check whether there is a lock collision (overlapping `touches`) preventing all of them from starting. (A `max_parallel` of `0` or any other non-positive value is not a silent cause here: LaneGate rejects it at config-load time with `max_parallel must be a positive integer`, before orchestrate can run at all.)

### Orchestration loop runs forever on one ticket (does not advance)

In combined mode (same executor for implement and review), the executor is responsible for calling `lanegate complete` and `lanegate review --verdict ...` before exiting. If the executor exits without calling these commands, the ticket stays in `in_progress` and the run loop keeps re-trying.

Causes:
- The executor crashed or was killed mid-session.
- The executor's headless flag is missing, so it stalled on an interactive prompt and was killed by a timeout.
- The combined-mode instructions were not followed by the model (can happen with smaller/local models).

Fix: inspect the executor log in `.lanegate/logs/`, check whether the process ran the lifecycle commands, and reset the ticket status manually if needed. For local models, consider using `reviewer: human` to separate implementation from review and reduce the chance of the model forgetting the combined-mode instructions.

### Combined-mode executor leaves ticket in incomplete state, so touches stay locked

In combined mode, if the executor exited 0 but did not run the full `lanegate complete && lanegate review --verdict ...` sequence, the ticket may end up in an intermediate state (e.g. `code_complete` with no verdict, `needs_review`, or `failed`). Orchestrate will detect this unhandled state, pause the ticket, and report an error:

```
ERROR: TICK-NNN — combined-mode executor exited 0 but left ticket in unhandled state...
  Pausing for manual review. Re-run: lanegate orchestrate
```

This prevents the board from wedging: touches remain locked to the incomplete ticket, and the file lock prevents later tickets from being assigned the same files.

Causes:
- Executor crashed between `complete` and `review` commands.
- Executor's headless flag is missing, causing it to stall on a prompt in the review phase before being killed.
- Model did not follow the combined-mode instructions completely.

Fix: inspect the executor log in `.lanegate/logs/TICK-NNN.log` to see where execution stopped. Then:
1. Manually complete the missing steps: `cd .lanegate/worktrees/TICK-NNN && lanegate complete && lanegate review --verdict approved` (or `changes_requested` as appropriate).
2. Return to the repo root and re-run: `lanegate orchestrate`.

Alternatively, if the ticket changes look wrong, reset it manually: edit `.lanegate/tickets/TICK-NNN.md` to set `status: open`, clear `review_verdict` if present, then re-run `lanegate start` to retry the whole ticket.

### `lanegate merge` fails with a merge conflict

LaneGate runs `git merge --no-ff` and aborts on conflict, leaving the ticket in `in_review`. The worktree branch diverged from `main` while the ticket was in progress, because another ticket that was merged in parallel modified one of the same files.

To resolve: go into the worktree (`cd .lanegate/worktrees/tick-NNN`), rebase or merge `main` into the branch, resolve conflicts, commit, then re-run `lanegate merge <id>` from the repo root.

### `.lanegate/orchestrator.lock` is stale and blocks a new `orchestrate` run

If `lanegate orchestrate` crashed (killed, SIGKILL, power loss), the lock file may not be cleaned up. The lock file is at `.lanegate/orchestrator.lock`. It contains the PID of the process that held it. If that process is no longer running, it is safe to delete the file manually and re-run orchestrate.

```bash
rm .lanegate/orchestrator.lock
```

Do not delete it while another orchestrate process is actively running.

### How do I know if an overnight `lanegate orchestrate` run got stuck?

Left alone, orchestrate can wedge on any of the causes above (executor hang, lock collision, one ticket stalling the loop) with no visible sign until you come back and look. `lanegate notify-watch` is a small daemon that polls `.lanegate/active-orchestrate.json`, the orchestrator lock, and the ticket board, and pushes a phone notification (via ntfy.sh) the moment something looks wrong: a dead executor process, no heartbeat for a while, or the loop stopped with tickets still sitting in `needs_review` / `blocked` / `failed` / hibernated. See [Phone alerts for stuck runs](config-reference.md#phone-alerts-for-stuck-runs-notify-watch) in the config reference for setup.

If the stall was actually a rate limit and `on_rate_limit: resume` is set, check what `resume-watch` has been doing:

```bash
lanegate resume-watch --status    # is it currently retrying
lanegate resume-watch --history   # hibernated -> retrying -> resumed/gave_up, with timestamps
```

---

## Worktree issues

### Worktree creation fails

LaneGate uses `git worktree add`. This requires:
- Git 2.15 or later.
- A clean working tree in the main checkout: `git status` should show no uncommitted changes (especially no conflicting untracked files in the worktrees directory).

If the worktree creation fails, the most likely cause is a stale worktree directory or branch from a previous crashed run. Clean up manually:

```bash
git worktree remove .lanegate/worktrees/tick-007 --force
git branch -D tick-007
```

Then retry `lanegate start`.

### Worktree directory left behind after a crash

If `lanegate merge` was not reached (crash, manual abort), the worktree directory at `.lanegate/worktrees/tick-NNN/` may still exist. Clean up manually:

```bash
git worktree remove .lanegate/worktrees/tick-NNN --force
git branch -D tick-NNN          # only if you do not need the branch anymore
```

---

## MCP server

### MCP client cannot connect / tools not appearing

The MCP server runs on stdio. Verify your MCP client config is pointing to the `lanegate` binary that is actually on `PATH`:

```json
{
  "mcpServers": {
    "lanegate": {
      "command": "lanegate",
      "args": ["mcp"]
    }
  }
}
```

If `lanegate` is inside a virtualenv, use the full path to the binary instead of the bare name, or activate the venv before starting the MCP client.

To test that the server starts correctly, run `lanegate mcp` directly in your terminal. It should block waiting for JSON-RPC input without printing any errors.

---

## GitHub sync (`lanegate gh-sync`)

### `gh-sync` fails: `gh: command not found`

`lanegate gh-sync` delegates to the GitHub CLI (`gh`). Install it from [cli.github.com](https://cli.github.com) and run `gh auth login` to authenticate.

### `gh-sync` creates duplicate issues

Matching uses an exact `[TICK-N]` prefix in the issue title. If issues were previously created without the prefix, or if the ticket ID changed, gh-sync will not match them and will try to create new ones. Use `--dry-run` to preview what would be created or updated before committing.

---

## Debugging tips

**See what LaneGate would do without touching anything:**

```bash
lanegate orchestrate --dry-run
```

**Check executor output after a failed run:**

Orchestrate writes full executor output to `.lanegate/logs/`. Inspect the most recent log file for the failing ticket to see what the executor actually printed before exiting.

**Inspect the diff a ticket produced before merging:**

```bash
git -C .lanegate/worktrees/tick-NNN diff main...tick-NNN
```

**Read the current ticket state:**

```bash
cat .lanegate/tickets/TICK-NNN.md
```

The YAML frontmatter contains `status`, `touches`, and any reviewer verdict. If a field looks wrong, you can edit it directly. LaneGate reads the file on every command.

**Check which files are currently locked:**

```bash
lanegate board
```

The board shows in-progress tickets and their `touches` lists. Any other ticket whose `touches` overlaps those files is blocked.

**Run doctor to check optional dependencies:**

```bash
lanegate doctor
```

Reports whether optional tools (gitleaks, semgrep, bandit, pip-audit, npm-audit) are available for security scanning of agent-produced diffs.
