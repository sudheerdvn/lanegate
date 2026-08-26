# Executor Capability Matrix

This document compares the executors supported by LaneGate across headless operation, prompt transport, local model support, auto-commit behavior, current sandbox status, recommended use, and known caveats. Read this before choosing an executor for `lanegate run`.

---

## Summary table

The sandbox status column describes current behavior with no external runner
configured. A future runner may report sandbox availability, applied policy,
violations, and log references for a specific run, but that would be run
outcome metadata rather than an intrinsic executor capability.

| Executor | Headless/non-interactive | Prompt transport | Local model support | Auto-commit | Sandbox status | Recommended use |
|---|---|---|---|---|---|---|
| `claude` / `claude-process` | Yes, with a scoped `--allowedTools` set (default) or `--dangerously-skip-permissions` | `-p <prompt>` (argv) | No, requires Anthropic API | No, commits via shell tools | Host process, no LaneGate sandbox | Default executor for most workflows |
| `claude-subagent` | Yes, same flag options as `claude-process` | `-p <prompt>` (argv) | No, requires Anthropic API | No, commits via shell tools | Host process, no LaneGate sandbox | Long-running sessions where context continuity matters |
| `aider` | Yes, requires `--yes-always` | `--message <prompt>` (argv) | Yes, works with any OpenAI-compatible or Ollama backend | Yes, aider commits each accepted change directly | Host process, no LaneGate sandbox | Code-generation tasks where direct git commit control is useful |
| `codex` | Yes, requires `--dangerously-bypass-approvals-and-sandbox` | Prompt passed as positional arg after `codex exec` (argv) | No, requires OpenAI API | No, commits via shell tools | Host process, no LaneGate sandbox | Teams already using Codex CLI, otherwise prefer `claude` |
| `ollama` | Yes, no interactive prompts by design | Prompt passed as positional arg to `ollama run` (argv/stdin equivalent) | Yes, local inference only, no external API calls | No, Ollama itself does not commit | Host process, no LaneGate sandbox | Fully offline or air-gapped operation, privacy-sensitive projects |
| `agy` | Yes, requires `agy` >= 1.1.1 (see caveats) | `--print <prompt>` (argv, must be the final two tokens) | No, requires Google account/API access | No, commits via shell tools | Host process, no LaneGate sandbox | Teams on Google's Antigravity CLI, supersedes the now-deprecated `gemini` type |

---

## Per-executor detail

### `claude` and `claude-process`

These two executor names both resolve to the `claude` CLI (Claude Code). `claude-process` is an alias that makes split-mode configuration explicit. The executor is validated and is LaneGate's recommended default.

**Headless operation:** Claude Code blocks on interactive approval prompts by default. `lanegate init` writes a scoped `--allowedTools` set to the executor's `flags` list by default, which auto-approves only the tool categories orchestrate needs (editing files, running tests/git via Bash) while leaving everything else gated. You can instead pass `--dangerously-skip-permissions`, which disables Claude Code's per-action confirmation dialogs entirely so the executor can read files, run shell commands, and call MCP tools without pausing. That's a deliberate trade-off for setups like a sandboxed CI runner. See [Security Status](../docs/security-model.md#headless-permission-options-for-the-claude-executor) for the full comparison.

**Prompt transport:** The rendered prompt is passed as a single string via the `-p` flag (argv). No prompt file or stdin piping is used.

**Local model support:** Not applicable. Claude Code requires a connection to the Anthropic API. There is no offline mode.

**Auto-commit behavior:** Claude Code does not commit on its own unless the executor explicitly runs `git commit` via its shell tool. In combined mode, the executor is instructed to run `lanegate complete && lanegate review --verdict ...` after committing. In split mode, the implement executor is expected to commit and then exit.

**Sandbox status:** Claude Code runs as a host process under the invoking OS user. LaneGate does not apply any container, namespace, bwrap, or seccomp wrapper. The executor process has full read/write access to the filesystem and can make outbound network connections.

**Recommended use:** Default executor for any project with an Anthropic API key. Combined mode (one subprocess handles implement and self-review) is the default and reduces round-trips.

---

### `aider`

Aider is an open-source coding tool that manages its own git workflow. It is validated for LaneGate integration, alongside `claude`, `codex`, and `agy`.

**Headless operation:** Pass `--yes-always` in the executor's `flags` list. This auto-confirms all of aider's interactive prompts (file additions, diff displays, and commit messages) without blocking on terminal input. Also pass `--no-gitignore`: aider's default behavior silently modifies `.gitignore` (adding `.aider*`) as an uncommitted side effect separate from its own commit, which LaneGate's post-executor scope-drift check then flags as an unexpected committed file and pauses the ticket for human review over aider's own housekeeping. `lane init --interactive` already sets both flags by default; a hand-written config needs both explicitly.

**Prompt transport:** The rendered prompt is passed via `--message <prompt>` (argv). File arguments from the ticket's `touches` list are appended so aider can load the relevant file content directly rather than relying on its repo-map inference.

**Local model support:** Yes. Aider supports any OpenAI-compatible API endpoint, including Ollama's local API. Configure via aider's `--model` flag or its own config file. LaneGate passes the `--model` flag if `models.implement` is set in `.lanegate.yml`.

#### Context window tokens

Local Aider routes can opt into a deterministic input budget before Aider starts. Set `executors.aider.context_window_tokens` to the usable context limit for that route. LaneGate estimates the rendered prompt and selected `touches` files at one token per three UTF-8 bytes, then adds an 8,192-token reserve for Aider overhead. If that estimate exceeds the budget, LaneGate raises a configuration error before Aider can create files, edit `.gitignore`, or send a model request. This is especially important for Ollama-backed routes: local models often have a finite context window, while Aider adds repository-map and tool overhead beyond the selected files. This is intentionally conservative. It does not make an oversized repository fit a model context window.

```yaml
executors:
  aider:
    context_window_tokens: 32768
```

#### Per-model settings overrides (`model_settings`)

When multiple models are dispatched through the same `executors.aider` config (e.g. via `models:`, `context_tiers`, or pool dispatch), each model may have its own `context_window_tokens` and `edit_format` under an optional `model_settings` mapping. The key is the exact model string as it appears in `--model` (after any `context_tiers` escalation).

**Lookup order:** `model_settings[dispatched-model-name]` → flat `executors.aider.*` default → absent (i.e. the flag is omitted from the aider CLI call). Partial overrides are fully supported: if a model's entry has only `context_window_tokens`, its `edit_format` still falls back to the flat default.

**YAML shape:**

```yaml
executors:
  aider:
    edit_format: diff              # flat default, used when no per-model override matches
    context_window_tokens: 65536   # flat default
    model_settings:
      'ollama_chat/gpt-oss:20b':
        context_window_tokens: 131072   # this model's architecture context length
        edit_format: whole              # avoid diff parser failures on thinking-capable models
      'ollama_chat/qwen2.5-coder:14b':
        context_window_tokens: 49152   # override only; edit_format falls back to "diff" above
      'ollama_chat/smtek/Qwen3.8-27B:Q3_K_M-ctx32k':
        context_window_tokens: 32768
```

Keys not present in a `model_settings` entry individually fall back to flat defaults — they do not inherit from other models' entries. Model names containing `/` or `:` (e.g. `ollama_chat/gpt-oss:20b`) are looked up as plain YAML string keys without any escaping requirement beyond quoting if needed by YAML syntax.

**Validation:** `context_window_tokens` must be a positive integer; `edit_format` must be a non-empty string from the same valid set as the flat key (`whole`, `diff`, `diff-fenced`, `udiff`, `patch`, `editor-diff`, `editor-whole`). Unknown sub-keys are rejected at config load time. These constraints mirror those already enforced for the flat keys.

**`context_tiers` interaction:** If `context_tiers` is configured, the model applied to the current dispatch is the post-escalation tier model (the one actually sent to aider). `model_settings` uses that escalated model name for the lookup, not the originally-configured step model. This means you can set per-model overrides for each tier model independently.

**Auto-commit behavior:** Aider commits each accepted change to the current branch automatically. This is aider's native behavior and is not controlled by LaneGate. LaneGate inspects the committed diff after aider exits.


**Sandbox status:** Aider runs as a host process under the invoking OS user. No container or kernel-level isolation is applied by LaneGate.

**Recommended use:** Teams that prefer aider's direct git integration. Also useful in split mode: `executor_steps: {implement: aider, review: claude}` lets a fast code-generation tool implement while a higher-quality model reviews.

**Known caveats:** Aider's repo map can be slow on large repositories. Providing an explicit `touches` list in the ticket file is strongly recommended to keep context focused and reduce latency, but Aider can still add repository and tool overhead. Use `context_window_tokens` for local routes with a finite model context. Small local models (e.g. qwen2.5-coder 7b/14b via Ollama) can unreliably narrate a fake self-verification step wrapped in code fences after a real edit, which either breaks aider's parser or gets misparsed as bogus filenames. That's a model/edit-format reliability issue, not a LaneGate integration gap. LaneGate's own safeguards (pre_complete/pre_merge tests, touches compliance) catch it without a false merge. Neither `edit_format` is universally safe for small local models: `whole` rewrites the entire file every turn and can truncate or hallucinate output past a few hundred lines, while `diff` avoids that but can get a malformed hunk from a small model instead — both failure modes are caught by the same pre_complete/pre_merge safeguards, not silently merged.

---

### `codex`

Codex refers to the OpenAI Codex CLI.

**Headless operation:** Pass `--dangerously-bypass-approvals-and-sandbox` in the executor's `flags` list to disable per-action approval prompts. Do not combine it (or any other `--sandbox`/`--approve-for-me` flag) with a separate `--sandbox` value — LaneGate already injects `--sandbox read-only` for analyze/review steps, and the real `codex` CLI errors on a duplicated `--sandbox` or on `--approve-for-me` combined with any `--sandbox` flag at all.

**Prompt transport:** The rendered prompt is passed to `codex exec` as a positional argument (or `-` to read it from stdin for session resume).

**Local model support:** Not applicable. Codex CLI requires an OpenAI API key and calls OpenAI's API. There is no local inference mode.

**Auto-commit behavior:** Codex does not automatically commit. The executor uses shell tools to run git commands. LaneGate inspects the worktree diff after the process exits.

**Sandbox status:** Codex runs as a host process under the invoking OS user. No container or kernel-level isolation is applied by LaneGate.

**Recommended use:** Teams already using the Codex CLI as their primary coding executor. For new setups, `claude` is better tested and has wider LaneGate test coverage.

**Known caveats:** Codex integration has lighter test coverage than `claude` or `aider`. The `codex exec` subcommand interface may change between CLI versions. Pin the Codex CLI version in your development environment.

---

### `ollama`

Ollama provides local model inference.

**Headless operation:** Yes by design. Ollama's `run` command is non-interactive when a prompt is provided on the command line, and no special flag is required.

**Prompt transport:** The rendered prompt is passed as a positional argument to `ollama run <model> <prompt>`. This is functionally similar to argv passing. For very long prompts, there may be OS-level argv length limits. LaneGate currently passes the prompt inline. Future releases may switch to stdin or a prompt file for long prompts.

**Local model support:** Yes. This is Ollama's primary purpose. All inference runs on the local machine with no external API calls. Models must be pulled with `ollama pull <model>` before use.

**Auto-commit behavior:** Ollama does not commit. Ollama is a model server, not a coding tool. The text it returns is the entire output, with no file edits or commits. Rather than silently accepting that and producing zero commits, LaneGate rejects `ollama` at dispatch time with a configuration error for `implement`, `review`, `fix`, and `drift_check`: only `analyze` (text-only) is permitted.

**Sandbox status:** Ollama runs as a host process. No container or kernel-level isolation is applied by LaneGate. Because Ollama makes no external API calls, network egress risk is lower than with cloud-backed executors, but filesystem access is still unrestricted.

**Recommended use:** Fully offline or air-gapped environments, privacy-sensitive projects where code must not leave the local machine, and cost-sensitive workflows where API costs are a constraint, for the `analyze` step only. For code-writing steps, raw `ollama` is rejected at dispatch time; use `executor: aider` with `provider: ollama` instead.

**Known caveats:** Ollama models vary significantly in coding quality. `qwen2.5-coder` and `codellama` are the recommended starting points for code-generation tasks. Response format compliance (e.g., the model reliably running `lanegate complete` at the end of a session) is less reliable with smaller local models than with frontier models like Claude.

---

### `agy`

Antigravity CLI, Google's successor to the Gemini CLI (`gemini` type). Google retired individual-tier Gemini CLI access on 2026-06-18, so new integrations should use `agy`, not `gemini`.

**Headless operation:** `agy` supports `-p`/`--print`/`--prompt` for single-shot non-interactive runs. **Requires `agy` >= 1.1.1.** Earlier versions had two automation-breaking bugs when spawned as a subprocess: hanging while reading stdin (fixed by not reading stdin when a prompt is supplied via flag), and exiting 0 with empty stdout on a server-side error such as a quota rejection instead of writing to stderr with a nonzero exit ([google-antigravity/antigravity-cli#76](https://github.com/google-antigravity/antigravity-cli/issues/76)). LaneGate already runs executors with `stdin=DEVNULL` when the prompt is passed via argv (which it is for `agy`), so the stdin-hang case doesn't reproduce here regardless of version. The swallowed-error behavior on pre-1.1.1 builds, though, can still look like a silently empty or successful ticket run. For unattended runs, pass `--dangerously-skip-permissions` in `executors.agy.flags` so tool executions run without interactive prompts. `lane init --interactive` also adds `--disable-slash-commands` by default so agy doesn't interpret `/`-prefixed content in the rendered prompt (e.g. ticket text mentioning a path or command) as its own CLI slash commands.

**Session continuity & modes:** LaneGate uses `--conversation <id>` to resume an earlier `analyze` or `implement` session during `implement` or `fix`. During `analyze`, LaneGate runs `agy` with `--mode plan` to ensure the analysis phase remains read-only without modifying files on disk.

**Prompt transport:** Passed via `--print <prompt>` (argv). `--print` is not a boolean flag: it consumes the very next token as the prompt. So LaneGate always places it last in the argv list, after `--output-format json` and any `--model` flag.

**Local model support:** No, it requires a signed-in Google Antigravity account or API access, the same trust boundary as `claude` and `codex`.

**Auto-commit behavior:** No automatic commit. The executor uses shell tools to run git commands. LaneGate inspects the worktree diff after the process exits, same as `claude`/`codex`.

**Sandbox status:** Host process under the invoking OS user. No container or kernel-level isolation is applied by LaneGate.

**Cost tracking:** `agy --output-format json` reports `input_tokens`, `output_tokens`, `thinking_tokens`, and `cache_read_tokens`, but no dollar cost and no cache-write token count. `cost_usd` and `cache_creation_tokens` are always `None` in LaneGate's parsed step-cost data for this executor (see `parse_agy_json_result` in `lanegate/executor.py`).

**Recommended use:** Teams standardized on Google's Antigravity CLI. Pin `agy` to >= 1.1.1 before relying on it for unattended `lanegate run` runs.

**Known caveats:** New integration with comparatively little LaneGate test coverage. The CLI is actively evolving post-rebrand. Pin the `agy` version in your development environment and watch for JSON envelope changes. Agy searches upward for a `.git` directory and can bypass a worktree's `.git` file, incorrectly writing edits to the main control checkout instead of the assigned worktree. LaneGate detects tracked-file changes to the control checkout after dispatch and fails the step, but operators should inspect and clean any leaked partial edits before continuing.

---

## Sandbox status reference

No executor is sandboxed by LaneGate itself today. The table below clarifies what isolation exists with the current no-runner execution path:

| Executor | Isolation wrapper | Network egress restricted | Process context |
|---|---|---|---|
| `claude` / `claude-process` | None | No | Host process under invoking user |
| `claude-subagent` | None | No | Same as above |
| `aider` | None | No (calls configured API or local Ollama) | Host process under invoking user |
| `codex` | None | No | Host process under invoking user |
| `ollama` | None | No (runs local server, no external calls) | Host process under invoking user |
| `agy` | None | No | Host process, Google Antigravity API calls are outbound |

Git-level containment (the `touches` list, hard-blocked files, and diff inspection) applies to all executors regardless of sandbox status. OS-level containment does not.

If the optional runner/sandbox contract is implemented later, sandbox data should be reported per run in the runner outcome: requested mode, applied mode, engine availability, network policy applied, policy violations, timeout or cancellation details, and stdout/stderr/event log references. That reporting must not change the matrix above unless the default no-runner behavior changes. Executor capability remains separate from runner-enforced policy.

---

## Choosing an executor

For cloud-backed frontier models, start with `executor: claude` (Claude Code). It has the most end-to-end testing in LaneGate and the most reliable structured output parsing.

For fully local operation, use `executor: aider` with a local Ollama model as aider's backend (`provider: ollama`). This is the validated path, since Aider applies and commits edits itself (analyze, implement, and review all run through it). Raw `executor: ollama` has no code-application step of its own (see above) and is only useful for text-only steps like `analyze`: dispatching it to `implement`/`review`/`fix`/`drift_check` is now enforced as a configuration error, not just discouraged.

For mixed workflows that pair fast generation with higher-quality review, use `executor_steps` to route implement and review to different executors (for example, local Aider for `implement` and `claude` for `review`), keeping day-to-day iteration offline while still getting an independent, higher-quality reviewer pass. See [config-reference.md](config-reference.md) for the resolution order and split-mode behavior.

For the threat model and safe usage recommendations that apply to all executors, see [docs/security-model.md](security-model.md).

---

## Model recommendation for `review`, `fix`, and `drift_check`

When `autonomy: full` enables the auto-fix loop (see [architecture.md §7](architecture.md)), three steps run without a human in the loop between the original implementation and a merge-eligible `approved` verdict: `review` (judges the diff), `fix` (patches it), and `drift_check` (the safety gate that verifies the fix didn't drift from the ticket's intent). Because nothing else catches a mistake at these steps, configure a stronger model for them than for `implement`:

```yaml
models:
  implement: claude-sonnet-5
  review: claude-opus-5
  fix: claude-opus-5
  drift_check: claude-opus-5
```

Or per-executor, under `executors.<name>.models`. This is a recommendation, not an enforced default. `resolve_model()` only supplies a built-in default for `analyze`/`implement`/`review` on claude-family executors. `fix` and `drift_check` have no built-in default and fall back to the executor's own default model if left unconfigured.

**Per-ticket override caveat:** a ticket's own `model:`/`executor:` frontmatter fields only override the `implement` step (and, for `executor:`, only when nothing else applies: `review` instead checks `reviewer:`). There is no per-ticket override for `fix` or `drift_check`. They only honor `executor_steps.<step>` / `models.<step>` in `.lanegate.yml`, or the global default. If you need a ticket to use a different model for its drift-check, that requires a project-level config change, not a ticket-level one.
