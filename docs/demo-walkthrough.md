# Demo Walkthrough: Idea to Parallel Worktrees to Review

This walkthrough shows LaneGate's core flow on a toy Python calculator project. It's self-contained, so you don't need any private project context. The only prerequisites are Python 3.11+, git, and LaneGate installed (see the README for the install command).

> This is a preview build. The workflow works, but it's still evolving, and V1 is not sandboxed at the OS level. See [Security Status](../README.md#security-status) before running agents on repositories you care about.

---

## 1. Set up a toy project

Create a small Python project to serve as the target. This keeps the walkthrough self-contained and non-sensitive.

```bash
mkdir calc && cd calc
git init
git commit --allow-empty -m "initial"
mkdir src tests
cat > src/calc.py << 'EOF'
def add(a, b):
    return a + b

def subtract(a, b):
    return a - b
EOF
cat > tests/test_calc.py << 'EOF'
from src.calc import add, subtract


def test_add():
    assert add(2, 3) == 5


def test_subtract():
    assert subtract(5, 3) == 2
EOF
git add . && git commit -m "add calc stub"
```

Initialize LaneGate inside the project:

```bash
lanegate init
```

This scaffolds `.lanegate/` (tickets directory, worktrees directory) and drops a `.lanegate.yml` config file at the repo root. You can open `.lanegate.yml` to set your preferred executor, or leave the defaults for now. The dry-run path in step 5 works without any executor configured.

For this toy project, add a small safeguard so `complete` and `merge` are blocked if the tests fail:

```yaml
safeguards:
  pre_complete:
    - pytest
  pre_merge:
    - pytest
```

---

## 2. Capture an idea as a ticket

Describe what you want built. LaneGate creates a draft ticket and, by default, immediately runs analysis to populate the `touches` list and `close_criteria`. The ticket stays in `draft` after this step so you can review the analysis before moving it into the queue.

```bash
lanegate create "add multiply and divide operations to calc.py, with divide-by-zero guard"
```

Sample output:

```
[TICK-001] draft created
Analyzing TICK-001...
TICK-001: touches populated (status: draft — run `lanegate analyze TICK-001` to open)
  touches: src/calc.py, tests/test_calc.py
  close_criteria: multiply(a, b) returns a*b; divide(a, b) returns a/b and raises ValueError on b==0
```

Review the ticket at `.lanegate/tickets/TICK-001.md`, then flip it to `open`:

```bash
lanegate open TICK-001          # touches already set — no new model call needed
# or: lanegate analyze TICK-001  # run analysis again and open the ticket
```

If you want to skip analysis entirely and populate the ticket manually, pass `--no-analyze`. The ticket file is plain Markdown with YAML frontmatter, so you can edit it directly.

---

## 3. Create a second ticket with disjoint touches

Add a second ticket that touches different files. Because the `touches` lists do not overlap, LaneGate will allow both tickets to run in parallel at the file-lock layer.

```bash
lanegate create "add a README.md describing the calculator API" --no-analyze
```

Edit the resulting draft at `.lanegate/tickets/TICK-002.md` and set `touches: [README.md]` in the frontmatter, then open it:

```bash
lanegate open TICK-002          # touches set manually above — open without re-analyzing
# or: lanegate analyze TICK-002  # run analysis to fill or update touches, then open
```

At this point the board shows two open tickets:

```bash
lanegate board
```

```
 ID       P  MS  Age  Title                              Flags
 TICK-001 1      —    add multiply and divide …          
 TICK-002 2      —    add a README.md describing …       
```

---

## 4. Inspect the non-overlapping batch

Before dispatching anything, ask LaneGate which tickets are non-overlapping by declared `touches`:

```bash
lanegate next
```

```
Next: TICK-001 — add multiply and divide operations to calc.py
  priority=1  parallel_safe=True  autonomy=supervised

Non-overlapping batch (can start simultaneously):
  TICK-001  priority=1  add multiply and divide …
  TICK-002  priority=2  add a README.md describing …
```

The concurrency model is simple: LaneGate holds a per-file lock from `in_progress` through `in_review`. Any ticket whose `touches` list overlaps a locked file waits. Tickets with disjoint `touches` can run in their own git worktrees simultaneously, but tests and review still matter because separate files can depend on each other.

---

## 5. Run orchestration (dry run first)

Use `--dry-run` to see what LaneGate would do without touching any files or invoking any executor. This works even if you have no executor (Claude Code, aider, Codex, Ollama) configured.

```bash
lanegate orchestrate --dry-run --human-review final
```

```
[dry-run] would start TICK-001
[dry-run] would invoke executor for TICK-001
[dry-run] would complete TICK-001
[dry-run] would run review for TICK-001
[dry-run] would start TICK-002
[dry-run] would invoke executor for TICK-002
[dry-run] would complete TICK-002
[dry-run] would run review for TICK-002
```

When you have an executor configured, drop `--dry-run` and LaneGate drives the full loop: claim tickets, spawn worktrees, dispatch the executor, wait for completion, and route to review. The `--human-review final` flag tells LaneGate to pause after all implementations are done and wait for you to inspect diffs before any merge. The dry-run output above doesn't show this, but it takes effect in a live run.

---

## 6. Start a ticket manually and watch the worktree appear

If you want to drive a single ticket by hand rather than via the orchestrator:

```bash
lanegate start TICK-001
```

```
Started TICK-001
  worktree: .lanegate/worktrees/tick-001
  branch:   tick-001
```

The worktree is a standard git worktree on branch `tick-001`. Any work the agent (or you) commits there is isolated from `main`. You can open a second terminal and work in both worktrees at the same time. Neither one sees the other's uncommitted changes.

```bash
ls .lanegate/worktrees/
# tick-001/
```

After implementation is done:

```bash
lanegate complete TICK-001
lanegate review TICK-001
```

`lanegate review` normally runs the configured review agent and records its verdict, so it isn't merely a state transition: it can take time, consume model tokens, and requires that executor to be installed. If the project configures `reviewer: human`, `none`, or `auto-none`, it instead moves the ticket to `in_review` without a verdict. In that case, inspect the diff yourself and record the human decision explicitly with `lanegate review TICK-001 --verdict approved` (or `changes_requested`). Use `--verdict` to record a direct human decision, not to skip a configured reviewer that would otherwise run.

Inspect the diff at any point:

```bash
git -C .lanegate/worktrees/tick-001 diff main...tick-001
```

---

## 7. Review and merge

Merge only after the configured reviewer has approved, or after a human has recorded an explicit approved verdict. A `changes_requested` verdict is not mergeable: address the findings and review again.

```bash
lanegate merge TICK-001
```

```
[TICK-001] merged → main
  worktree .lanegate/worktrees/tick-001 removed
```

Repeat for TICK-002. After both tickets are merged:

```bash
lanegate board
```

```
 id        title                   status  touches
 TICK-001  add multiply …          merged  src/calc.py, tests/test_calc.py
 TICK-002  add a README.md …       merged  README.md
```

---

## 8. Full orchestrated run (with an executor)

Once you have an executor installed and configured in `.lanegate.yml`, you can run the full loop with one command:

```bash
lanegate orchestrate --human-review final
```

LaneGate picks up all open tickets, dispatches the non-overlapping batch, monitors completion, then pauses for your review before merge. To pause after each individual ticket, set `reviewer: human` and use `--human-review per_ticket`.

For a fully local/offline run with Ollama, set this in `.lanegate.yml` before orchestrating:

```yaml
executor: ollama
models:
  implement: qwen2.5-coder
  review: qwen2.5-coder
```

Then pull the model and orchestrate:

```bash
ollama pull qwen2.5-coder
lanegate orchestrate --human-review final
```

---

## Summary

The flow from idea to merge is:

```
lanegate create "<intent>"        # idea → auto-analyze → draft with touches populated
lanegate open <id>                # review analysis, then flip draft → open (no new model call)
# or: lanegate analyze <id>       # run analysis again and open the ticket
lanegate board                    # see the queue for the active milestone
lanegate next                     # which tickets can run in parallel right now
lanegate orchestrate --dry-run    # preview actions without touching files
lanegate orchestrate --human-review final   # full run: worktrees → impl → review gate
```

Each ticket stays in its own git branch and worktree from `start` through `merge`. Parallel tickets with disjoint `touches` lists do not block each other at the file-lock layer, though tests and review are still needed for semantic conflicts. Human review gates (`--human-review final`, or `--human-review per_ticket` with `reviewer: human`) let you inspect diffs before they land on `main`.
