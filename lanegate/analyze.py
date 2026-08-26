"""
analyze.py — turn a draft ticket's intent into touches + close_criteria.

This is the ONLY place lanegate itself calls an LLM (F9). Everything else is
deterministic. The model is called via a thin seam (_call_model) so tests can
stub it without a live API call.

Model strategy:
  Calls the configured executor (claude, codex, ollama, aider, …) using the
  same dispatch logic as the implement step.  Falls back to "claude" when no
  executor is set in .lanegate.yml.
"""

from __future__ import annotations

import ast
import datetime
import json
import logging
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

logger = logging.getLogger("lanegate.analyze")

from lanegate import APP_NAME
from lanegate.config import is_high_reasoning_ticket, load_config, resolve_trunk_branch
from lanegate.ticket import (
    TERMINAL_STATUSES,
    canonical_id,
    collect_cross_ticket_change_notes,
    find_control_plane_touch_overlaps,
    load_acceptance_contract_audit,
    load_all_tickets,
    load_change_notes,
    load_file_skeletons,
    validate_acceptance_matrix,
    validate_overlap_review,
    validate_ticket,
    write_file_skeletons_sidecar,
    write_ticket,
)

_CLAUDE_EXECUTORS = frozenset({"claude", "claude-process", "claude-subagent"})
_SESSION_EXECUTORS = frozenset({"claude", "claude-process", "claude-subagent", "agy", "codex"})
_CLAUDE_MODEL_PREFIXES = ("claude-",)
_ACTIVE_ANALYSIS_FILE = "analyze-active.json"
_MAX_LOGGED_EXECUTOR_OUTPUT = 8 * 1024
# A transient executor failure is worth surfacing to the current ticket, but
# repeatedly sending later drafts to the same known-bad pool member just burns
# calls. Keep this deliberately small: a second identical failure during one
# orchestrate run takes that member out of rotation via the existing cooldown
# machinery. The tracker is in-memory and keyed by the active run id, so it
# cannot leak a stale failure into a later run.
_ANALYZE_FAILURE_COOLDOWN_THRESHOLD = 2
_ANALYZE_FAILURE_STREAKS: dict[tuple[str, str, str], tuple[str, int]] = {}


def _active_analyze_run_id(repo_root: Path) -> str | None:
    """Return the active orchestrate session id, if analyze runs inside one."""
    from lanegate.orchestrate.run_report import _resolve_active_run_session_ts

    return _resolve_active_run_session_ts(repo_root)


def _analyze_failure_signature(raw_stdout: str, raw_stderr: str) -> str:
    """Normalize volatile executor metadata before comparing failure streaks."""
    text = f"{raw_stdout}\n{raw_stderr}".lower()
    text = re.sub(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        "<uuid>",
        text,
    )
    text = re.sub(
        r"([\"']?(?:session|message|request)[_-]?id[\"']?\s*[:=]\s*)[\"']?[^\s,\"'}]+",
        r"\1<id>",
        text,
    )
    return re.sub(r"\s+", " ", text).strip()


def _record_analyze_failure(
    repo_root: Path, driver_name: str, raw_stdout: str, raw_stderr: str
) -> int | None:
    """Record one non-rate-limit failure and return its run-local streak size."""
    run_id = _active_analyze_run_id(repo_root)
    if run_id is None:
        return None
    key = (str(repo_root.resolve()), run_id, driver_name)
    signature = _analyze_failure_signature(raw_stdout, raw_stderr)
    previous = _ANALYZE_FAILURE_STREAKS.get(key)
    count = previous[1] + 1 if previous and previous[0] == signature else 1
    _ANALYZE_FAILURE_STREAKS[key] = (signature, count)
    return count


def _clear_analyze_failure_streak(repo_root: Path, driver_name: str) -> None:
    """A successful model call makes prior failures for this run irrelevant."""
    run_id = _active_analyze_run_id(repo_root)
    if run_id is not None:
        _ANALYZE_FAILURE_STREAKS.pop((str(repo_root.resolve()), run_id, driver_name), None)

# ---------------------------------------------------------------------------
# Optional tree-sitter import guard
# ---------------------------------------------------------------------------

try:
    from tree_sitter import Language as _TSLanguage
    from tree_sitter import Parser as _TSParser

    _HAS_TREE_SITTER = True
except ImportError:
    _HAS_TREE_SITTER = False

# Map file extension → tree-sitter grammar module name.
# Used by _index_non_py_file to load the correct grammar lazily.
_TS_LANGUAGE_MAP: dict[str, str] = {
    ".go": "tree_sitter_go",
    ".js": "tree_sitter_javascript",
    ".jsx": "tree_sitter_javascript",
    ".ts": "tree_sitter_typescript",
    ".tsx": "tree_sitter_typescript",
    ".rs": "tree_sitter_rust",
    ".java": "tree_sitter_java",
    ".rb": "tree_sitter_ruby",
    ".c": "tree_sitter_c",
    ".cpp": "tree_sitter_cpp",
    ".h": "tree_sitter_c",
    ".php": "tree_sitter_php",
    ".cs": "tree_sitter_c_sharp",
    ".swift": "tree_sitter_swift",
    ".kt": "tree_sitter_kotlin",
    ".kts": "tree_sitter_kotlin",
}

# Project-declared additions from .lanegate.yml's tree_sitter_languages key,
# merged in by register_tree_sitter_languages() -- see config.load_config().
# A plain dict.update() at a single early chokepoint (config load, once per
# process) so callers deep in the parse chain (file_symbols, skeleton
# generation) stay cfg-agnostic; no threading needed.
def register_tree_sitter_languages(extra: dict[str, str] | None) -> None:
    """Merge project-declared extension -> tree-sitter module mappings.

    Lets a project support a language LaneGate doesn't ship a built-in
    mapping for (e.g. Vue, Elixir, Zig) by declaring it in .lanegate.yml and
    `pip install`-ing the matching tree-sitter grammar package, without
    waiting on a LaneGate release. Unknown extensions degrade to ripgrep
    either way (see _ts_load_language), so this is purely additive.
    """
    if not extra:
        return
    _TS_LANGUAGE_MAP.update(extra)

# Map of lanegate CLI subcommand name → source file (relative path).
# Used by infer_touches_from_criteria to expand subcommand mentions.
_CMD_FILE_MAP: dict[str, str] = {
    "board": "lanegate/board.py",
    "stats": "lanegate/stats.py",
    "analyze": "lanegate/analyze.py",
    "create": "lanegate/create.py",
    "watch": "lanegate/watch.py",
    "orchestrate": "lanegate/orchestrate.py",
    "doctor": "lanegate/doctor.py",
    "review": "lanegate/reviewer.py",
    "promote": "lanegate/promote.py",
    "start": "lanegate/lifecycle.py",
    "complete": "lanegate/lifecycle.py",
    "hibernate": "lanegate/lifecycle.py",
    "reopen": "lanegate/lifecycle.py",
    "done": "lanegate/lifecycle.py",
    "merge": "lanegate/worktree.py",
    "next": "lanegate/ticket.py",
    "flag": "lanegate/flags.py",
    "gh-sync": "lanegate/ghsync.py",
    "claim-file": "lanegate/claim_file.py",
}

_MAX_ACCEPTANCE_REF_BYTES = 96 * 1024
_CONTRACT_VERBS = (
    "must",
    "should",
    "requires",
    "required",
    "return",
    "returns",
    "response",
    "endpoint",
    "contract",
    "structured",
    "graceful",
)
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}

# Cache: extension → Language object (or None when grammar not installed)
_TS_LANG_CACHE: dict[str, _TSLanguage | None] = {}

# Node types that carry a "name" child — tried in order; first hit wins.
# Different grammars use different type names for the same concept.
_TS_SYMBOL_NODE_TYPES: frozenset[str] = frozenset(
    {
        # Functions / methods
        "function_declaration",
        "function_definition",
        "method_declaration",
        "method_definition",
        "function_item",  # Rust: free functions and impl methods alike
        # Types / classes
        "class_declaration",
        "interface_declaration",
        "type_declaration",
        "struct_type",  # Go: appears inside type_spec
        "struct_item",  # Rust
        "trait_item",  # Rust
        "enum_item",  # Rust
        "protocol_declaration",  # Swift
        "object_declaration",  # Kotlin: singleton objects
    }
)

# For each symbol node type, these child node types hold the name.
_TS_NAME_CHILD_TYPES: frozenset[str] = frozenset(
    {
        "identifier",
        "type_identifier",
        "field_identifier",
        "property_identifier",
        "name",
        "simple_identifier",  # Swift: functions/methods (types still use type_identifier)
    }
)


def _ts_load_language(ext: str) -> _TSLanguage | None:
    """Load and cache the tree-sitter Language for the given file extension.

    Returns None if the grammar package is not installed.
    """
    if not _HAS_TREE_SITTER:
        if ext not in _TS_LANG_CACHE:
            msg = f"[analyze] WARNING: tree-sitter is not installed; non-Python symbol indexing degraded for '{ext}' files."
            print(msg, file=sys.stderr)
            _TS_LANG_CACHE[ext] = None
        return None

    if ext in _TS_LANG_CACHE:
        return _TS_LANG_CACHE[ext]

    mod_name = _TS_LANGUAGE_MAP.get(ext)
    if mod_name is None:
        _TS_LANG_CACHE[ext] = None
        return None

    try:
        import importlib

        # mod_name comes from _TS_LANGUAGE_MAP (built-in entries, fixed at
        # module load) or register_tree_sitter_languages() (project-declared
        # via .lanegate.yml, trusted config -- never from ticket/file content
        # or other untrusted input).
        mod = importlib.import_module(mod_name)  # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
        # A few grammars don't expose a plain language() function.
        if ext in (".ts",):
            lang_fn = getattr(mod, "language_typescript", None) or getattr(mod, "language", None)
        elif ext in (".tsx",):
            lang_fn = getattr(mod, "language_tsx", None) or getattr(mod, "language", None)
        elif ext in (".php",):
            lang_fn = getattr(mod, "language_php", None) or getattr(mod, "language", None)
        else:
            lang_fn = getattr(mod, "language", None)

        if lang_fn is None:
            msg = f"[analyze] WARNING: missing tree-sitter grammar '{mod_name}' for extension '{ext}'; non-Python symbol indexing degraded."
            print(msg, file=sys.stderr)
            _TS_LANG_CACHE[ext] = None
            return None

        lang = _TSLanguage(lang_fn())
        _TS_LANG_CACHE[ext] = lang
        return lang
    except Exception:  # ImportError, AttributeError, or grammar init errors
        msg = f"[analyze] WARNING: missing tree-sitter grammar '{mod_name}' for extension '{ext}'; non-Python symbol indexing degraded."
        print(msg, file=sys.stderr)
        _TS_LANG_CACHE[ext] = None
        return None


def _ts_extract_symbols(node: object) -> list[str]:
    """Walk a tree-sitter Node and return all symbol names found.

    Looks for function/class/method declaration nodes and extracts their name
    child. Works across every grammar in _TS_LANGUAGE_MAP (plus any
    project-declared via register_tree_sitter_languages).
    """
    symbols: list[str] = []

    def _walk(n: object) -> None:
        node_type: str = n.type  # type: ignore[attr-defined]
        children = n.children  # type: ignore[attr-defined]

        if node_type in _TS_SYMBOL_NODE_TYPES:
            # For type_declaration (Go), recurse into type_spec to find type_identifier
            if node_type == "type_declaration":
                for child in children:
                    _walk(child)
                return
            # For struct_type (Go), the name lives in the parent type_spec's type_identifier
            # which was already handled by type_declaration above; skip struct_type itself.
            if node_type == "struct_type":
                return
            # Extract first name-typed child
            for child in children:
                if child.type in _TS_NAME_CHILD_TYPES:  # type: ignore[attr-defined]
                    raw = child.text  # type: ignore[attr-defined]
                    if raw:
                        name = raw.decode("utf-8", errors="replace")
                        if name:
                            symbols.append(name)
                    break

        # For type_spec (Go) — extract the type_identifier as a symbol
        if node_type == "type_spec":
            for child in children:
                if child.type == "type_identifier":  # type: ignore[attr-defined]
                    raw = child.text  # type: ignore[attr-defined]
                    if raw:
                        name = raw.decode("utf-8", errors="replace")
                        if name:
                            symbols.append(name)
                    break

        for child in children:
            _walk(child)

    _walk(node)
    return symbols


def _ts_symbol_lines(node: object) -> list[tuple[int, str]]:
    """Return ``(line, declaration text)`` for each symbol, for skeleton output.

    :func:`_ts_extract_symbols` returns bare names, which is enough for the
    symbol index but not for a skeleton -- a caller needs somewhere to jump to
    and enough of the declaration to check a signature. The declaration's own
    first line is used rather than a reconstructed signature, since each grammar
    spells parameters and return types differently.
    """
    entries: list[tuple[int, str]] = []

    def _walk(n: object) -> None:
        node_type: str = n.type  # type: ignore[attr-defined]
        if node_type in _TS_SYMBOL_NODE_TYPES and node_type != "struct_type":
            raw = n.text  # type: ignore[attr-defined]
            if raw:
                text = raw.decode("utf-8", errors="replace")
                first = text.split("\n", 1)[0].strip().rstrip("{").strip()
                if first:
                    # start_point is 0-indexed; editors and the Python skeleton
                    # path both count from 1.
                    entries.append((n.start_point[0] + 1, first))  # type: ignore[attr-defined]
        for child in n.children:  # type: ignore[attr-defined]
            _walk(child)

    _walk(node)
    # A nested declaration is reached after its parent, so sort to keep the
    # skeleton in file order.
    entries.sort(key=lambda item: item[0])
    return entries


# ---------------------------------------------------------------------------
# Model seam — replace in tests via monkeypatch or dependency injection
# ---------------------------------------------------------------------------


class ExecutorCallError(RuntimeError):
    """Executor subprocess exited non-zero during analyze.

    Carries the *unclipped* stdout/stderr alongside the display message.
    The message itself goes through _summarize_executor_output, which clips
    every line to 240 chars — and stream-json executors (the Claude CLI)
    emit their whole transcript as a handful of enormous single lines, so a
    quota notice like "Claude AI usage limit reached" lands far past that
    boundary. Classifying rate limits off str(exc) therefore silently missed
    every stream-json quota error and skipped pool failover; callers must
    classify against these raw fields instead.
    """

    def __init__(self, message: str, *, raw_stdout: str = "", raw_stderr: str = ""):
        super().__init__(message)
        self.raw_stdout = raw_stdout
        self.raw_stderr = raw_stderr


def _summarize_executor_output(text: str, *, max_lines: int = 12, max_line_len: int = 240) -> str:
    """Return a compact error summary without dumping prompts or transcripts."""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return ""

    interesting = [
        line
        for line in lines
        if re.search(
            r"error|failed|invalid|quota|rate limit|too many requests|traceback", line, re.I
        )
    ]
    selected = interesting[:max_lines] if interesting else lines[-max_lines:]

    clipped: list[str] = []
    for line in selected:
        if len(line) > max_line_len:
            line = line[: max_line_len - 3] + "..."
        clipped.append(line)
    return "\n".join(clipped)


def _bounded_executor_output(text: str, *, max_bytes: int = _MAX_LOGGED_EXECUTOR_OUTPUT) -> str:
    """Keep executor evidence actionable without allowing unbounded log growth."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="replace") + "\n... [truncated]"


def _active_analysis_path(repo_root: Path) -> Path:
    return repo_root / ".lanegate" / _ACTIVE_ANALYSIS_FILE


def _write_active_analysis(
    repo_root: Path,
    *,
    ticket_id: str,
    phase: str,
    executor: str,
    model: str | None,
    started_at: float,
    log_file: Path,
) -> None:
    """Atomically update the active standalone-analysis record for API/TUI use."""
    path = _active_analysis_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        log_path = log_file.relative_to(repo_root).as_posix()
    except ValueError:
        log_path = str(log_file)
    payload = {
        "ticket_id": ticket_id,
        "phase": phase,
        "executor": executor,
        "model": model or "default",
        "started_at": started_at,
        "log_path": log_path,
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def get_active_analysis_status(repo_root: Path) -> dict | None:
    """Return the public active-analysis payload, or ``None`` when inactive."""
    path = _active_analysis_path(repo_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        started_at = float(data["started_at"])
    except (FileNotFoundError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    return {
        "ticket_id": str(data.get("ticket_id", "")),
        "phase": str(data.get("phase", "unknown")),
        "executor": str(data.get("executor", "unknown")),
        "model": str(data.get("model", "default")),
        "elapsed_seconds": max(0, int(time.time() - started_at)),
        "log_path": str(data.get("log_path", "")),
    }


def _clear_active_analysis(repo_root: Path) -> None:
    try:
        _active_analysis_path(repo_root).unlink()
    except FileNotFoundError:
        pass


def _estimate_prompt_tokens(prompt: str) -> int:
    """A deliberately conservative prompt-token estimate for operator feedback."""
    return max(1, (len(prompt) + 2) // 3)


class _WaitingReporter:
    """Emit periodic elapsed-time updates while the synchronous executor runs."""

    def __init__(self, emit, started_at: float, interval: float) -> None:
        self._emit = emit
        self._started_at = started_at
        self._interval = max(0.01, interval)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._emit(f"Waiting for model... (elapsed {int(time.time() - self._started_at)}s)")


class _AnalysisVisibility:
    """Keep terminal, log, and active-status lifecycle views in lockstep."""

    def __init__(self, repo_root: Path, ticket_id: str) -> None:
        from lanegate.logs import analyze_log_path, write_analysis_event

        self.repo_root = repo_root
        self.ticket_id = ticket_id
        self.started_at = time.time()
        self.log_file = analyze_log_path(repo_root)
        self._write_event = write_analysis_event
        self.executor = "resolving"
        self.model: str | None = None
        self._update("starting")
        self._write_event(self.log_file, "starting", "Standalone analysis started")

    def _update(self, phase: str) -> None:
        _write_active_analysis(
            self.repo_root,
            ticket_id=self.ticket_id,
            phase=phase,
            executor=self.executor,
            model=self.model,
            started_at=self.started_at,
            log_file=self.log_file,
        )

    def set_driver(self, executor: str, model: str | None) -> None:
        self.executor = executor
        self.model = model

    def emit(self, phase: str, message: str, *, error: bool = False) -> None:
        print(f"[analyze] {message}", file=sys.stderr if error else sys.stdout, flush=True)
        self._write_event(self.log_file, phase, message)
        self._update(phase)

    def executor_output(self, text: str) -> None:
        self._write_event(self.log_file, "executor_output", _bounded_executor_output(text))

    def cleanup(self) -> None:
        _clear_active_analysis(self.repo_root)


def _call_model(
    prompt: str,
    model: str | None = None,
    executor: str = "claude",
    cfg: dict | None = None,
    driver_cfg: dict | None = None,
    repo_root: Path | None = None,
    tid: str | None = None,
) -> tuple[str, str | None]:
    """Call the configured executor with prompt; return (raw text response, session_id or None).

    build_executor_cmd already decides whether to request structured output
    (``--output-format json`` for Claude types, ``--json`` for Codex) based on
    the executor's *resolved* type, unconditionally, regardless of caller —
    so unwrap here via the same ``parse_structured_result`` registry used by
    review/autofix/pool dispatch (see executor.py), keyed on that same
    resolved type. Adding a new structured executor (e.g. Gemini CLI) is one
    flag change in build_executor_cmd plus one parser + one registry entry in
    executor.py; nothing here needs to change. Executors with no registered
    parser (aider, ollama, openhands, plain) get None back and raw stdout is
    used as-is.

    ``repo_root``/``tid`` are optional purely so this can still be called
    without them (e.g. a future non-ticket caller); when both are supplied
    and the executor's output parsed to a structured envelope, the dispatch
    cost is recorded the same way review/implement/fix already do -- analyze
    previously parsed this same envelope only to read session_id/result_text
    and threw the usage/cost fields away, leaving it the one step invisible
    to ``context-stats``.
    """
    from lanegate.executor import (
        _CLAUDE_SUBPROCESS_TYPES,
        build_executor_cmd,
        executor_types_with,
        get_executor_config,
        parse_structured_result,
        resolve_executor_env,
    )
    from lanegate.orchestrate import _build_env, _cfg_with_driver_command_overrides

    base_cfg = cfg or {}
    effective_driver_cfg = driver_cfg or {}
    command_cfg = _cfg_with_driver_command_overrides(base_cfg, executor, effective_driver_cfg)
    resolved_executor_cfg = get_executor_config(executor, base_cfg)
    executor_env = resolve_executor_env(resolved_executor_cfg)
    executor_env = _build_env(effective_driver_cfg, base_env=executor_env)
    resolved_executor_type = resolved_executor_cfg.get("type", executor)

    use_stdin = resolved_executor_type in executor_types_with("stdin_capable")
    # Analyze must stay read-only: the prompt carries candidate-file skeletons
    # (see _build_prompt) so touches/change_notes precision doesn't depend on
    # the model reading real files itself, and denying edit capability here
    # closes the gap where the executor's own default full-access flags
    # (--dangerously-skip-permissions, --yes-always, --dangerously-bypass-
    # approvals-and-sandbox) would otherwise leave analyze free to write --
    # at a draft ticket, before any worktree exists, directly against the
    # main checkout. disallowed_tools is Claude's own mechanism
    # (--disallowedTools); read_only=True covers every other executor type
    # via build_executor_cmd's per-type read-only flag (aider --dry-run,
    # codex --sandbox read-only, agy --mode plan).
    disallowed_tools = ["Bash", "Write", "Edit"] if resolved_executor_type in _CLAUDE_SUBPROCESS_TYPES else None
    cmd = build_executor_cmd(
        executor, prompt, command_cfg, model=model, use_stdin=use_stdin,
        disallowed_tools=disallowed_tools,
        read_only=True,
        step="analyze",
    )

    start_time = time.time()
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True, encoding="utf-8",
        env=executor_env,
        input=prompt if use_stdin else None,
    )
    if result.returncode != 0:
        cmd_label = " ".join(cmd[:2]) if len(cmd) > 1 and cmd[1] == "exec" else cmd[0]
        details = _summarize_executor_output(result.stderr or result.stdout)
        suffix = f": {details}" if details else ""
        raise ExecutorCallError(
            f"{cmd_label} failed (exit {result.returncode}){suffix}",
            raw_stdout=result.stdout or "",
            raw_stderr=result.stderr or "",
        )

    raw = result.stdout.strip()
    session_id: str | None = None
    parsed = parse_structured_result(resolved_executor_type, raw)
    if parsed is not None:
        session_id = parsed.get("session_id") or None
        raw = parsed.get("result_text", raw)
        if repo_root is not None and tid is not None:
            from lanegate.context_log import record_step_cost

            record_step_cost(
                repo_root, tid, "analyze", executor, model, parsed,
                dispatch_start_time=start_time,
            )

    return raw, session_id


# ---------------------------------------------------------------------------
# AST symbol index (Python files only — stdlib ast, no extra deps)
# ---------------------------------------------------------------------------


@dataclass
class _DefInfo:
    name: str
    line: int
    signature: str  # e.g. "def cmd_start(ticket_id, cfg, repo_root, ...)"
    kind: str  # "function" | "class" | "method"


@dataclass
class _FileIndex:
    path: Path
    defs: list[str]  # function / class names defined in the file
    imports: list[str]  # module names imported by the file
    def_infos: list[_DefInfo] = field(default_factory=list)  # name/line/signature/kind


_ANALYZE_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "__pypackages__",
    "build",
    "dist",
    "env",
    "node_modules",
    "site-packages",
    "venv",
    "worktrees",
}
_ANALYZE_IGNORED_SUFFIXES = (".pyc", ".pyo")
_ANALYZE_RG_EXCLUDE_GLOBS = tuple(f"!{name}/**" for name in sorted(_ANALYZE_IGNORED_DIRS)) + (
    "!.*/**",
)


def _is_ignored_analysis_parts(parts: tuple[str, ...]) -> bool:
    return any(part.startswith(".") or part in _ANALYZE_IGNORED_DIRS for part in parts)


def _is_ignored_analysis_path(path: Path, repo_root: Path) -> bool:
    try:
        rel = path.relative_to(repo_root)
    except ValueError:
        rel = path
    return _is_ignored_analysis_parts(rel.parts) or path.suffix in _ANALYZE_IGNORED_SUFFIXES


def _is_ignored_analysis_relpath(path: str) -> bool:
    rel = Path(path)
    return _is_ignored_analysis_parts(rel.parts) or rel.suffix in _ANALYZE_IGNORED_SUFFIXES


def _ripgrep_cmd(word: str) -> list[str]:
    cmd = ["rg", "--no-heading", "--line-number", "-i", "-l"]
    for pattern in _ANALYZE_RG_EXCLUDE_GLOBS:
        cmd.extend(["--glob", pattern])
    cmd.append(word)
    return cmd


def _def_signature(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    """Render a compact one-line signature for a def/class node."""
    if isinstance(node, ast.ClassDef):
        bases = ", ".join(ast.unparse(b) for b in node.bases)
        return f"class {node.name}({bases})" if bases else f"class {node.name}"
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    args_str = ast.unparse(node.args)
    ret = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"{prefix} {node.name}({args_str}){ret}"


def _collect_def_infos(tree: ast.AST) -> list[_DefInfo]:
    """Walk the tree collecting one _DefInfo per function/class/method, in line order."""
    infos: list[_DefInfo] = []

    def visit(node: ast.AST, in_class: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "method" if in_class else "function"
                infos.append(
                    _DefInfo(
                        name=child.name,
                        line=child.lineno,
                        signature=_def_signature(child),
                        kind=kind,
                    )
                )
                visit(child, in_class=False)
            elif isinstance(child, ast.ClassDef):
                infos.append(
                    _DefInfo(
                        name=child.name,
                        line=child.lineno,
                        signature=_def_signature(child),
                        kind="class",
                    )
                )
                visit(child, in_class=True)
            else:
                visit(child, in_class=in_class)

    visit(tree, in_class=False)
    infos.sort(key=lambda d: d.line)
    return infos


def _index_py_file(path: Path) -> _FileIndex | None:
    """Parse a single .py file and return its symbol index.

    Returns None on any parse error so callers can skip silently.
    """
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, ValueError, OSError):
        return None

    defs: list[str] = []
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs.append(node.name)
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)

    def_infos = _collect_def_infos(tree)
    return _FileIndex(path=path, defs=defs, imports=imports, def_infos=def_infos)


def _build_ast_index(repo_root: Path) -> list[_FileIndex]:
    """Walk repo_root for all .py files and build symbol indices.

    Files that fail to parse are silently skipped.
    """
    indices: list[_FileIndex] = []
    for py_file in sorted(repo_root.rglob("*.py")):
        if _is_ignored_analysis_path(py_file, repo_root):
            continue
        idx = _index_py_file(py_file)
        if idx is not None:
            indices.append(idx)
    return indices


def file_symbols(path: Path, repo_root: Path) -> list[str]:
    """Return the symbol names declared in *path*, or ``[]`` if none can be read.

    Python is parsed with stdlib ``ast``; other languages use tree-sitter when
    the matching grammar is installed. Public so callers outside ``analyze``
    (the ``symbols`` command, prompt building) can reuse the same index.

    Returns ``[]`` rather than raising when a file is unreadable, unparseable,
    or has no installed grammar, so callers fall back to searching.
    """
    abs_path = path if path.is_absolute() else repo_root / path
    if abs_path.suffix == ".py":
        idx = _index_py_file(abs_path)
        return [info.signature for info in idx.def_infos] if idx else []
    idx = _index_non_py_file(abs_path)
    return list(idx.defs) if idx else []


def _build_file_skeleton(path: Path, repo_root: Path) -> str:
    """Return a compact text block for one file: a header line (name + line
    count) plus one 'line N: signature' entry per declaration.

    Python uses stdlib ast; the languages in ``_TS_LANGUAGE_MAP`` use
    tree-sitter when their grammar is installed (previously every
    non-Python file returned a bare header, so Go/TS/Rust tickets reached the
    agent with no structure at all). Files with no available parser still get
    the header. Never LLM-generated."""
    abs_path = path if path.is_absolute() else repo_root / path
    try:
        rel = abs_path.relative_to(repo_root).as_posix()
    except ValueError:
        rel = path.as_posix()

    try:
        line_count = len(abs_path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return f"{rel}  (file not found)"

    header = f"{rel}  ({line_count} lines)"

    if abs_path.suffix == ".py":
        idx = _index_py_file(abs_path)
        if idx is None or not idx.def_infos:
            return header
        body = "\n".join(
            f"  line {info.line:>3}: {info.signature}" for info in idx.def_infos
        )
        return f"{header}\n{body}"

    entries = _ts_file_symbol_lines(abs_path)
    if not entries:
        return header
    body = "\n".join(f"  line {line:>3}: {text}" for line, text in entries)
    return f"{header}\n{body}"


# Analyze-time candidate skeletons are a speed/precision tradeoff, not a
# completeness guarantee: bounded to the top symbol/importer matches (already
# relevance-ranked by enrich_context) so the prompt stays a fixed size
# regardless of how big the matched set is, rather than growing per repo.
_CANDIDATE_SKELETON_MAX_FILES = 25
_CANDIDATE_SKELETON_MAX_BYTES = 15000


def _build_candidate_skeletons(paths: list[str], repo_root: Path) -> str:
    """Return real per-file signatures (line numbers included) for the files
    analyze's context search matched, so the model can write precise
    ``change_notes`` from the prompt alone instead of using Read tool calls
    to fetch the same information one file at a time."""
    seen: set[str] = set()
    ordered: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            ordered.append(p)

    blocks: list[str] = []
    total_bytes = 0
    for p in ordered[:_CANDIDATE_SKELETON_MAX_FILES]:
        block = _build_file_skeleton(Path(p), repo_root)
        block_bytes = len(block.encode("utf-8"))
        if total_bytes + block_bytes > _CANDIDATE_SKELETON_MAX_BYTES:
            break
        blocks.append(block)
        total_bytes += block_bytes

    if not blocks:
        return ""

    return (
        "## Candidate file skeletons (stdlib ast — real signatures, not a summary)\n"
        "Line-numbered defs for the files matched above. Prefer these over reading "
        "the files again; only Read/Grep a file if it's missing here or you need "
        "content beyond a signature.\n\n" + "\n\n".join(blocks)
    )


def _intent_keywords(intent: str) -> list[str]:
    """Extract meaningful keywords from intent text (length >= 4)."""
    return list(dict.fromkeys(w.lower() for w in re.findall(r"[A-Za-z]{4,}", intent)))


def _ast_symbol_hits(intent: str, repo_root: Path) -> list[_FileIndex]:
    """Return FileIndex entries whose defs match any keyword from intent."""
    keywords = _intent_keywords(intent)
    if not keywords:
        return []
    indices = _build_ast_index(repo_root)
    hits: list[_FileIndex] = []
    for idx in indices:
        defs_lower = [d.lower() for d in idx.defs]
        if any(kw in defs_lower or any(kw in d for d in defs_lower) for kw in keywords):
            hits.append(idx)
    return hits


def _import_graph_expand(hits: list[_FileIndex], all_indices: list[_FileIndex]) -> list[_FileIndex]:
    """One-hop import graph: find files that import any of the hit files.

    Returns the importers (not re-including the original hits).
    """
    if not hits:
        return []

    # Build set of module names corresponding to hit files
    # e.g. lanegate/analyze.py → "lanegate.analyze", "analyze"
    hit_module_names: set[str] = set()
    for idx in hits:
        stem = idx.path.stem
        hit_module_names.add(stem)
        # Attempt dotted path from path parts (e.g. lanegate.analyze)
        parts = list(idx.path.parts)
        # Strip leading path segments until we hit a package root or filename
        for i in range(len(parts)):
            candidate = ".".join(p.removesuffix(".py") for p in parts[i:])
            hit_module_names.add(candidate)

    hit_paths = {idx.path for idx in hits}
    importers: list[_FileIndex] = []
    seen_paths: set[Path] = set(hit_paths)

    for idx in all_indices:
        if idx.path in seen_paths:
            continue
        # Check if this file imports any hit module
        for imp in idx.imports:
            # Direct match or prefix match (e.g. "lanegate.analyze" starts with "lanegate.analyze")
            if any(imp == m or imp.startswith(m + ".") for m in hit_module_names):
                importers.append(idx)
                seen_paths.add(idx.path)
                break

    return importers


# ---------------------------------------------------------------------------
# Tree-sitter hits (optional — activated when lanegate[treesitter] installed)
# ---------------------------------------------------------------------------


def _index_non_py_file(path: Path) -> _FileIndex | None:
    """Parse a non-Python source file with tree-sitter and return its symbol index.

    Returns None when:
    - The file extension has no registered grammar
    - The grammar package for that extension is not installed
    - The file cannot be read or parsed

    Per-language graceful degradation: each extension is tried independently
    using the cached grammar loaded by _ts_load_language.
    """
    ext = path.suffix.lower()
    lang = _ts_load_language(ext)
    if lang is None:
        return None

    try:
        source = path.read_bytes()
    except OSError:
        return None

    try:
        parser = _TSParser(lang)
        tree = parser.parse(source)
    except Exception:
        return None

    symbols = _ts_extract_symbols(tree.root_node)
    # _FileIndex.defs holds symbol names; imports is unused for non-Python files
    return _FileIndex(path=path, defs=symbols, imports=[])


def _ts_file_symbol_lines(path: Path) -> list[tuple[int, str]]:
    """Parse *path* with tree-sitter and return ``(line, declaration)`` entries.

    Returns ``[]`` when tree-sitter or the file's grammar is unavailable, so
    callers degrade to a header-only skeleton instead of failing.
    """
    lang = _ts_load_language(path.suffix.lower())
    if lang is None:
        return []
    try:
        source = path.read_bytes()
    except OSError:
        return []
    try:
        tree = _TSParser(lang).parse(source)
    except Exception:
        return []
    return _ts_symbol_lines(tree.root_node)


def _treesitter_hits(intent: str, repo_root: Path) -> list[str]:
    """Symbol-index hits via tree-sitter for non-Python files.

    Returns a list of relative file path strings.
    Silently returns [] when tree-sitter is not installed or finds nothing.
    Falls back to ripgrep when tree-sitter is not installed (existing behaviour
    preserved — the caller's fallback chain handles that).
    """
    if not _HAS_TREE_SITTER:
        return []

    keywords = _intent_keywords(intent)
    if not keywords:
        return []

    hits: list[str] = []
    for src_file in sorted(repo_root.rglob("*")):
        if src_file.suffix not in _TS_LANGUAGE_MAP:
            continue
        if _is_ignored_analysis_path(src_file, repo_root):
            continue
        idx = _index_non_py_file(src_file)
        if idx is None:
            continue
        symbols_lower = [s.lower() for s in idx.defs]
        if any(kw in name for kw in keywords for name in symbols_lower):
            try:
                rel = src_file.relative_to(repo_root).as_posix()
            except ValueError:
                rel = str(src_file)
            hits.append(rel)
        if len(hits) >= 20:
            break
    return hits


# ---------------------------------------------------------------------------
# Ripgrep fallback (non-Python files / when AST finds nothing)
# ---------------------------------------------------------------------------


def _ripgrep_seed(intent: str, repo_root: Path) -> str:
    """Extract keywords from intent and ripgrep for relevant files in source.

    Returns empty string if ripgrep is not installed or finds nothing.
    """
    words = list(dict.fromkeys(w.lower() for w in re.findall(r"[A-Za-z]{5,}", intent)))[:6]
    if not words:
        return ""

    lines: list[str] = []
    for word in words:
        try:
            r = subprocess.run(
                _ripgrep_cmd(word),
                capture_output=True,
                text=True, encoding="utf-8",
                cwd=repo_root,
            )
        except FileNotFoundError:
            return ""  # rg not installed — skip silently
        if r.returncode == 0:
            for f in r.stdout.strip().splitlines():
                rel = f.strip()
                if rel and not _is_ignored_analysis_relpath(rel):
                    lines.append(rel)
                if len(lines) >= 20:
                    break

    seen: set[str] = set()
    unique = []
    for ln in lines:
        if ln not in seen:
            seen.add(ln)
            unique.append(ln)
    return "\n".join(unique[:20])


def _repo_structure(repo_root: Path) -> str:
    """Return a concise list of source files (git-tracked, max 60 lines)."""
    r = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        text=True, encoding="utf-8",
        cwd=repo_root,
    )
    if r.returncode != 0:
        return ""
    files = [f for f in r.stdout.strip().splitlines() if not _is_ignored_analysis_relpath(f)]
    return "\n".join(files[:60])


# ---------------------------------------------------------------------------
# Enrichment chain — first provider with hits wins
# ---------------------------------------------------------------------------


class _EnrichedContext:
    """Holds the result of context enrichment for prompt assembly."""

    def __init__(self) -> None:
        self.symbol_hits: list[str] = []  # relative paths with AST/treesitter matches
        self.importers: list[str] = []  # files that import the hit files (one hop)
        self.ripgrep_hits: str = ""  # raw ripgrep output (fallback)
        self.repo_structure: str = ""  # full file list (last resort)
        self.source: str = "none"  # which provider supplied hits


def enrich_context(intent: str, repo_root: Path) -> _EnrichedContext:
    """Build enriched context from AST -> tree-sitter -> ripgrep -> file list.

    The first provider that returns hits wins; subsequent providers are only
    used as fallbacks.
    """
    ctx = _EnrichedContext()

    # 1. stdlib ast (Python only)
    ast_hits = _ast_symbol_hits(intent, repo_root)
    if ast_hits:
        all_indices = _build_ast_index(repo_root)
        importers = _import_graph_expand(ast_hits, all_indices)
        ctx.symbol_hits = [idx.path.relative_to(repo_root).as_posix() for idx in ast_hits]
        ctx.importers = [idx.path.relative_to(repo_root).as_posix() for idx in importers]
        ctx.source = "ast"
        return ctx

    # 2. tree-sitter (optional — non-Python files)
    if _HAS_TREE_SITTER:
        ts_hits = _treesitter_hits(intent, repo_root)
        if ts_hits:
            ctx.symbol_hits = ts_hits
            ctx.source = "treesitter"
            return ctx

    # 3. ripgrep fallback
    rg = _ripgrep_seed(intent, repo_root)
    if rg:
        ctx.ripgrep_hits = rg
        ctx.source = "ripgrep"
        return ctx

    # 4. full file list (last resort - source stays "none")
    ctx.repo_structure = _repo_structure(repo_root)
    return ctx


# ---------------------------------------------------------------------------
# Static touch inference from close-criteria text
# ---------------------------------------------------------------------------


def infer_touches_from_criteria(
    text: str,
    repo_root: Path,  # reserved for future filesystem existence checks
) -> list[str]:
    """Return file paths statically implied by *text* (close_criteria + intent).

    Scans for patterns:
    - ``lanegate <cmd>`` → mapped source file (only for known commands in _CMD_FILE_MAP)
    - ``show in board`` / ``add column`` → ``lanegate/board.py``
    - ``new file <path>`` / ``new module <path>`` → that path

    Unknown ``lanegate X`` patterns are silently ignored — prose like "lanegate runs
    the project" or "lanegate ticket creation" must not inject cli.py.
    """
    inferred: list[str] = []

    # Pattern: 'lanegate <cmd>' — only map commands that are in _CMD_FILE_MAP
    for m in re.finditer(r"\blanegate\s+([\w-]+)", text, re.I):
        cmd = m.group(1).lower()
        if cmd in _CMD_FILE_MAP:
            inferred.append(_CMD_FILE_MAP[cmd])

    # Pattern: 'show in board' or 'add column' — board display is affected
    if re.search(r"\bshow\s+in\s+board\b|\badd\s+column\b", text, re.I):
        inferred.append("lanegate/board.py")

    # Pattern: 'new file path/to/file' or 'new module path/to/file'
    for m in re.finditer(r"\bnew\s+(?:file|module)\s+([\w./-]*[\w-])\b", text, re.I):
        inferred.append(m.group(1))

    # Deduplicate, preserve order
    seen: set[str] = set()
    result: list[str] = []
    for p in inferred:
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result


# Bare doc keywords (no explicit path/extension in the text) mapped to the
# repo-relative path they refer to. Only README is hardcoded, because it is the
# one filename that is genuinely universal across ecosystems. Everything else is
# derived from what the project itself declares -- LaneGate used to
# map 'architecture'/'config-reference'/'security-model' to its *own* docs/
# layout, so a user ticket saying "update the architecture doc" resolved against
# LaneGate's directory structure rather than the user's.
_UNIVERSAL_DOC_KEYWORDS: dict[str, str] = {
    "readme": "README.md",
}


def _doc_keyword_map(cfg: dict | None = None) -> dict[str, str]:
    """Return the keyword -> repo-relative doc path map for this project.

    Built from the universal names, plus an explicit ``doc_keywords`` mapping in
    ``.lanegate.yml``, plus every ``reference_docs`` entry keyed by its own file
    stem -- so a project declaring ``docs/DESIGN.md`` gets "design" recognised
    against *its* path, with no filename assumptions from LaneGate.
    """
    from lanegate.prompts import resolve_reference_docs

    mapping = dict(_UNIVERSAL_DOC_KEYWORDS)
    for rel in resolve_reference_docs(cfg):
        stem = Path(rel).stem.strip().lower()
        if len(stem) >= 3:
            mapping.setdefault(stem, rel)
            mapping.setdefault(stem.replace("-", " "), rel)
    configured = (cfg or {}).get("doc_keywords")
    if isinstance(configured, dict):
        for keyword, path in configured.items():
            if isinstance(keyword, str) and isinstance(path, str):
                if keyword.strip() and path.strip():
                    mapping[keyword.strip().lower()] = path.strip()
    return mapping


def companion_docs_from_criteria(
    text: str, repo_root: Path, cfg: dict | None = None
) -> list[str]:
    """Return companion documentation paths implied by *text*.

    Close criteria and background prose often imply a doc update ("update
    README and DESIGN.md to describe the new behavior") without the model
    reliably carrying that into ``touches``. Scans for explicit ``*.md`` path
    mentions and for doc keywords drawn from this project's own config (see
    :func:`_doc_keyword_map`), then keeps only paths that actually exist in
    *repo_root* — prose mentioning documentation in the abstract must not
    inject a nonexistent path.
    """
    found: set[str] = set()

    for m in re.finditer(r"\b(?:[\w-]+/)*[\w-]+\.md\b", text, re.I):
        found.add(m.group(0))

    lowered = text.lower()
    for keyword, path in _doc_keyword_map(cfg).items():
        if keyword in lowered:
            found.add(path)

    return sorted(p for p in found if (repo_root / p).is_file())


def validate_touched_paths(paths: list[str], repo_root: Path) -> list[str]:
    """Return entries in *paths* that reference a directory no longer present.

    A touches entry carried forward from an earlier draft/analyze pass can
    reference a directory that a since-merged ticket has renamed, moved, or
    promoted (e.g. when ``tui_spike/`` was moved to ``tui/internal/``).
    Carrying such a stale entry forward is worse than useless: the executor
    ends up implementing under the real current path, which was never
    declared, and the touches-guard blocks legitimate work after the fact.

    Only directory-style references (trailing ``/``) are checked — a plain
    file path may legitimately not exist yet (a new file the ticket itself
    will create).
    """
    return [p for p in paths if p.endswith("/") and not (repo_root / p).is_dir()]


def correct_touches_by_basename(paths: list[str], repo_root: Path) -> dict[str, str]:
    """Return {declared_path: real_path} for any *paths* entry that doesn't
    exist on disk but uniquely matches a tracked file's basename elsewhere.

    ``enrich_context``'s tiers stop at the first provider with any hit, even
    a partial one (e.g. ripgrep matching only the test file for a brand-new
    symbol that doesn't exist in source yet) -- the full repo file listing
    (the last-resort tier) is then never shown, so the model has no way to
    notice a nested layout and can write a flat-looking path straight from
    the ticket text (``calc.py`` instead of the real ``src/calc.py``).

    A path that doesn't exist anywhere is left alone -- it may legitimately
    be a new file the ticket itself will create. Only an unambiguous
    same-basename match against a real tracked file gets corrected;
    multiple same-named files anywhere in the tree are left as the model
    wrote them rather than guessing which one it meant.
    """
    missing = [p for p in paths if p and not p.endswith("/") and not (repo_root / p).exists()]
    if not missing:
        return {}

    from lanegate.executor import _repo_tracked_files

    by_basename: dict[str, list[str]] = {}
    for f in _repo_tracked_files(repo_root):
        by_basename.setdefault(Path(f).name, []).append(f)

    corrections: dict[str, str] = {}
    for path in missing:
        candidates = by_basename.get(Path(path).name)
        if candidates and len(candidates) == 1 and candidates[0] != path:
            corrections[path] = candidates[0]
    return corrections


# ---------------------------------------------------------------------------
# Acceptance-contract audit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AcceptanceContractAudit:
    """Deterministic acceptance-contract audit result."""

    ok: bool
    findings: list[str] = field(default_factory=list)
    omitted_items: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    checked_items: list[str] = field(default_factory=list)

    def as_metadata(self) -> dict:
        return {
            "ok": self.ok,
            "findings": self.findings,
            "omitted_items": self.omitted_items,
            "sources": self.sources,
            "checked_items": self.checked_items,
        }


@dataclass(frozen=True)
class _ContractItem:
    label: str
    source: str
    terms: tuple[str, ...]


# Headings lanegate itself appends to a ticket's body as operational notes (needs_review
# reason, hibernation notes, auto-fix attempt logs, review findings, and the audit's own
# stored output) rather than authored requirements.  Left in, these feed the audit's
# own prior output back into itself on the next attempt:
#   - A ticket flagged for omitting an item quotes that finding in its "Needs Review
#     Reason" or "Acceptance Contract Audit" section, which then gets re-scanned as a
#     fresh unmet requirement, compounding on every reopen/re-audit cycle.
#   - The "Acceptance Contract Audit" section references linked docs (e.g.
#     docs/ARCHITECTURE.md) by name, causing those docs to be pulled in as contract
#     sources on the next run even when the ticket text itself never mentioned them.
_OPERATIONAL_SECTION_RE = re.compile(
    r"(?:^|\n)##\s*(Needs Review Reason|Hibernation Reason|Auto-Fix Attempt \d+"
    r"|Review Findings|Dismissal Rationale|Acceptance Contract Audit|Status History|Lifecycle Timeline"
    r"|Post-Merge Verification Diagnostic)"
    r".*?(?=\n##\s|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def _strip_operational_sections(body: str) -> str:
    return _OPERATIONAL_SECTION_RE.sub("", body)


def _extract_non_goals(body: str) -> str:
    """Extract the Non-Goals section from ticket body, if present.

    Returns the text of the Non-Goals section (without the heading), or empty
    string if no such section exists. Handles variants like "Non-Goals" and
    "Non Goals".
    """
    non_goals_re = re.compile(
        r"(?:^|\n)##\s*(?:Non[\s-]?Goals)(?:\s*\([^)]*\))?\s*\n(.*?)(?=\n##|\Z)",
        re.IGNORECASE | re.DOTALL,
    )
    match = non_goals_re.search(body)
    return match.group(1).strip() if match else ""


def _flatten_change_notes(change_notes: object) -> str:
    if not isinstance(change_notes, dict):
        return ""
    lines: list[str] = []
    for path, note in change_notes.items():
        if isinstance(path, str) and isinstance(note, str):
            lines.append(f"{path}: {note}")
    return "\n".join(lines)


def _normalize_contract_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower())


def _contract_terms(text: str) -> tuple[str, ...]:
    terms: list[str] = []
    for token in re.findall(r"/api/[a-z0-9_/{}/.-]+|[a-z0-9_/-]{3,}", text.lower()):
        token = token.strip("`.,;:()[]{}")
        if not token or token in _STOPWORDS:
            continue
        if token not in terms:
            terms.append(token)
    return tuple(terms)


def _close_criteria_as_str(value: object) -> str:
    """Coerce a close_criteria value (str or list) to a comparable string.

    Tickets may store ``close_criteria`` as a YAML list of strings.  Joining
    with newlines produces a canonical form that ``_normalize_contract_text``
    can compare without calling ``.strip()`` on a list object.
    """
    if isinstance(value, list):
        return "\n".join(str(item) for item in value)
    return str(value) if value is not None else ""


def _close_criteria_drifted(original: object, proposed: object) -> bool:
    """Return True when the model-proposed close_criteria differs from the original.

    Accepts ``str`` or ``list`` for both arguments; lists are normalised to a
    newline-joined string so comparison is always performed on plain text.

    Normalisation (collapse whitespace, lowercase) means trivial re-wrapping or
    punctuation changes do not constitute drift.

    * Empty ``original`` → no prior criteria exists, so nothing to restore; return False.
    * Empty ``proposed`` with non-empty ``original`` → the model omitted a
      pre-existing criteria; that is drift; return True so the guard restores it.
    """
    original_str = _close_criteria_as_str(original)
    proposed_str = _close_criteria_as_str(proposed)
    if not original_str:
        return False
    if not proposed_str:
        return True
    return (
        _normalize_contract_text(original_str.strip())
        != _normalize_contract_text(proposed_str.strip())
    )


def _covered_by_text(item: _ContractItem, text: str) -> bool:
    haystack = _normalize_contract_text(text)
    if not haystack:
        return False

    source_path = item.source.split(":", 1)[0]
    if source_path.endswith(".md") and source_path.lower() in haystack:
        return True

    label = _normalize_contract_text(item.label)
    if label and label in haystack:
        return True

    if item.label == "run_id/status response":
        return "run_id" in haystack and "status" in haystack
    if item.label == "structured diff files":
        return "diff" in haystack and ("files" in haystack or "structured" in haystack)
    if item.label == "graceful stop":
        return "graceful" in haystack and "stop" in haystack

    terms = [term for term in item.terms if term not in _STOPWORDS]
    if not terms:
        return False
    required = max(1, min(len(terms), 3))
    return sum(1 for term in terms if term in haystack) >= required


def _add_contract_item(
    items: list[_ContractItem],
    seen: set[tuple[str, str]],
    *,
    label: str,
    source: str,
    terms: tuple[str, ...] | None = None,
) -> None:
    clean_label = re.sub(r"\s+", " ", label).strip(" -`|")
    if not clean_label:
        return
    key = (source, clean_label.lower())
    if key in seen:
        return
    seen.add(key)
    items.append(_ContractItem(clean_label, source, terms or _contract_terms(clean_label)))


_TABLE_SEPARATOR_RE = re.compile(r"^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$")


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences, preserving structure.

    Splits on sentence-ending punctuation followed by space, then on '. ' within
    the text. Returns non-empty sentences only.
    """
    # Split on sentence boundaries: period/question/exclamation followed by space
    # This is a simple heuristic that handles most cases
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    return [s.strip() for s in sentences if s.strip()]


def _table_header_row_indices(lines: list[str]) -> set[int]:
    """Indices of markdown table header + separator rows.

    A header row (e.g. ``| Field | Required | Description |``) is a column
    label, not content -- it must not be extracted as a contract item just
    because a column name like "Required" happens to match a contract verb.
    Identified by the standard markdown-table shape: a ``|``-led row
    immediately followed by a ``| --- | --- |``-style separator row.
    """
    skip: set[int] = set()
    for i in range(len(lines) - 1):
        if lines[i].strip().startswith("|") and _TABLE_SEPARATOR_RE.match(lines[i + 1].strip()):
            skip.add(i)
            skip.add(i + 1)
    return skip


def _extract_contract_items(text: str, source: str) -> list[_ContractItem]:
    """Extract deterministic acceptance items from ticket text or linked docs.

    The extractor only treats genuinely structural markdown as acceptance
    material: explicit endpoints, markdown table data rows (not header rows),
    and bullets/checklists that themselves carry an endpoint, brace, or
    contract-verb signal. A bare prose line merely containing a word like
    "should" or "must" is never enough on its own -- free-form rationale,
    design, or background prose (which is not a bullet or table row) is
    always ignored, regardless of what words it contains.
    """
    items: list[_ContractItem] = []
    seen: set[tuple[str, str]] = set()

    lines = text.splitlines()
    header_rows = _table_header_row_indices(lines)

    in_code_fence = False
    for idx, raw_line in enumerate(lines):
        line = raw_line.strip()
        if line.startswith("```"):
            in_code_fence = not in_code_fence
            continue
        if in_code_fence:
            continue
        if not line or line.startswith("#"):
            continue
        if idx in header_rows:
            continue
        lower = line.lower()

        endpoints = re.findall(r"`?((?:GET|POST|PUT|PATCH|DELETE)\s+/api/[^\s`|)]+)", line)
        endpoints += re.findall(r"`?(/api/[a-zA-Z0-9_/{}/.-]+)", line)
        for endpoint in endpoints:
            endpoint = endpoint.strip("`.,;")
            if endpoint.upper().split(" ", 1)[0] in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                path = endpoint.split(" ", 1)[1]
            else:
                path = endpoint
            _add_contract_item(items, seen, label=path, source=source, terms=(path.lower(),))

        if "run_id" in lower and "status" in lower and ("response" in lower or "/api/" in lower):
            _add_contract_item(
                items,
                seen,
                label="run_id/status response",
                source=source,
                terms=("run_id", "status"),
            )
        if "/api/diff" in lower and ("files" in lower or "patch" in lower):
            _add_contract_item(
                items,
                seen,
                label="structured diff files",
                source=source,
                terms=("diff", "files"),
            )
        if "graceful" in lower and "stop" in lower:
            _add_contract_item(
                items,
                seen,
                label="graceful stop",
                source=source,
                terms=("graceful", "stop"),
            )

        is_table_row = line.startswith("|")
        is_bullet = bool(re.match(r"^[-*]\s+|\d+\.\s+", line))
        if not (is_table_row or is_bullet):
            # Bare prose is never structural on its own -- endpoints and the
            # special-cased phrases above already ran; a plain sentence
            # containing "should"/"must"/etc. is not itself a contract item.
            continue

        if is_table_row:
            # Table data rows are structured by construction -- every row is a record.
            compact = re.sub(r"\s+", " ", line.strip("| "))
            if len(compact) > 180:
                compact = compact[:177].rstrip() + "..."
            _add_contract_item(items, seen, label=compact, source=source)
        else:
            # For bullets, split into sentences and extract only those carrying
            # a contract signal (verb, endpoint, or brace), not the whole paragraph.
            sentences = _split_sentences(line.strip("| "))
            for sentence in sentences:
                sentence_lower = sentence.lower()
                sentence_has_verb = source != "file_skeletons" and any(
                    re.search(rf"\b{re.escape(verb)}\b", sentence_lower) for verb in _CONTRACT_VERBS
                )
                # Check for endpoints in the sentence
                sentence_endpoints = re.findall(
                    r"`?((?:GET|POST|PUT|PATCH|DELETE)\s+/api/[^\s`|)]+)", sentence
                )
                sentence_endpoints += re.findall(r"`?(/api/[a-zA-Z0-9_/{}/.-]+)", sentence)
                sentence_has_endpoint = bool(sentence_endpoints)
                sentence_has_brace = "{" in sentence

                if sentence_has_verb or sentence_has_endpoint or sentence_has_brace:
                    compact = re.sub(r"\s+", " ", sentence.strip())
                    if len(compact) > 180:
                        compact = compact[:177].rstrip() + "..."
                    _add_contract_item(items, seen, label=compact, source=source)

    return items


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def _doc_sections(text: str) -> list[tuple[str | None, str]]:
    """Split markdown text into (heading_text_or_None, section_body) blocks.

    A doc with no headings comes back as a single (None, text) section so
    callers can treat "whole small doc" and "one relevant section of a big
    doc" the same way. Headings inside fenced code blocks (e.g. a '# foo.yml'
    comment in an example) are not real section boundaries and are ignored.
    """
    # (line_start_offset, line_end_offset, heading_text) for each real heading line.
    matches: list[tuple[int, int, str]] = []
    in_code_fence = False
    pos = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_fence = not in_code_fence
        elif not in_code_fence:
            m = _HEADING_RE.match(line.rstrip("\n"))
            if m:
                matches.append((pos, pos + len(line), m.group(2).strip()))
        pos += len(line)
    if not matches:
        return [(None, text)]
    sections: list[tuple[str | None, str]] = []
    preamble = text[: matches[0][0]]
    if preamble.strip():
        sections.append((None, preamble))
    for i, (_line_start, line_end, heading) in enumerate(matches):
        section_end = matches[i + 1][0] if i + 1 < len(matches) else len(text)
        sections.append((heading, text[line_end:section_end]))
    return sections


# Headings built only from these words don't distinguish one section of a doc
# from another (nearly every doc has a "Default values"/"Overview"/"Notes"
# section) and shouldn't count as a relevance signal on their own -- doing so
# let a whole options table hide behind a generic "Default values" heading and
# get pulled in wholesale for any ticket that shared that one common word.
_GENERIC_HEADING_WORDS = {
    "default",
    "defaults",
    "value",
    "values",
    "overview",
    "background",
    "note",
    "notes",
    "example",
    "examples",
    "reference",
    "config",
    "configuration",
    "option",
    "options",
    "setting",
    "settings",
    "general",
    "misc",
    "miscellaneous",
    "introduction",
    "summary",
    "details",
    "detail",
    # Domain-ubiquitous words: every ticket in this system talks about
    # "tickets"/"lanegate"/"orchestrate" as a matter of course, so a heading built
    # only from these matches almost any ticket and isn't a real signal either.
    "ticket",
    "tickets",
    APP_NAME,
    "orchestrate",
    "orchestration",
}


def _heading_relevant_to_ticket(heading: str, ticket_text: str) -> bool:
    heading_terms = [
        t
        for t in _contract_terms(heading)
        if t not in _STOPWORDS and t not in _GENERIC_HEADING_WORDS
    ]
    if not heading_terms:
        return False
    ticket_norm = _normalize_contract_text(ticket_text)
    return any(term in ticket_norm for term in heading_terms)


def _scope_doc_to_ticket(doc_text: str, ticket_text: str) -> str:
    """Narrow a linked doc to the sections a ticket actually anchors to.

    A ticket naming a small, single-purpose doc still pulls in the whole thing
    (no headings to scope by). But once a doc has multiple headed sections,
    only the ones the ticket's own text actually names count as required
    contract material -- otherwise linking a large shared reference doc (e.g.
    docs/config-reference.md) for background inherits every unrelated field in
    it as a mandatory close_criteria item.
    """
    sections = _doc_sections(doc_text)
    if len(sections) <= 1:
        return doc_text
    kept: list[str] = []
    for heading, body in sections:
        if heading is None or _heading_relevant_to_ticket(heading, ticket_text):
            if heading:
                kept.append(f"## {heading}")
            kept.append(body)
    return "\n".join(kept)


def _local_context_for_path(text: str, path: str) -> str:
    """Return only the line(s) of *text* that actually mention *path*.

    Heading-relevance matching against the *entire* flattened ticket text lets
    a long ticket's incidental word overlap (e.g. sharing "resume" with an
    unrelated "Rate limits and auto-resume" doc heading) pull in whole
    unrelated sections of a shared reference doc. Restricting the match to
    the line(s) that actually name the doc path -- consistent with this
    project's one-paragraph-per-line convention -- keeps relevance judged
    against what the ticket said about *that* doc, not everything else in it.
    """
    matching = [line for line in text.split("\n") if path in line]
    return "\n".join(matching) if matching else text


def _linked_context_refs(ticket: dict, repo_root: Path) -> list[tuple[str, str]]:
    """Return (label, text) pairs for repo docs or prior tickets named by the ticket."""
    close_criteria = ticket.get("close_criteria") or ""
    if isinstance(close_criteria, list):
        close_criteria = "\n".join(str(item) for item in close_criteria)
    text = "\n".join(
        [
            ticket.get("title", "") or "",
            _strip_operational_sections(ticket.get("_body", "") or ""),
            close_criteria,
            _flatten_change_notes(load_change_notes(ticket)),
        ]
    )
    refs: list[tuple[str, str]] = []
    seen: set[str] = set()

    for match in re.finditer(r"\b(?:docs|tests|lanegate)/[A-Za-z0-9_./-]+\.md\b", text):
        rel = match.group(0)
        if rel in seen:
            continue
        seen.add(rel)
        path = repo_root / rel
        try:
            if path.is_file() and path.resolve().is_relative_to(repo_root.resolve()):
                doc_text = path.read_text(encoding="utf-8")[:_MAX_ACCEPTANCE_REF_BYTES]
            else:
                continue
        except (OSError, ValueError):
            continue
        scoped = _scope_doc_to_ticket(doc_text, _local_context_for_path(text, rel))
        if scoped.strip():
            refs.append((rel, scoped))

    return refs


def _close_criteria_drift_finding(ticket: dict, repo_root: Path) -> str | None:
    """Flag close_criteria that changed since ``analyzed_at_sha`` without a
    recorded human approval.

    The rest of this audit only checks internal consistency of the *current*
    ticket text, so a commit that narrows close_criteria to match a reduced
    implementation (rather than the owner approving a scope change) reads as
    perfectly self-consistent and passes with 0 findings. This
    check compares against the committed baseline instead.
    """
    sha = ticket.get("analyzed_at_sha")
    path = ticket.get("_path")
    if not sha or not path:
        return None
    try:
        rel = Path(path).relative_to(repo_root)
    except ValueError:
        return None

    from lanegate.reconciliation import _split_frontmatter

    result = subprocess.run(
        ["git", "show", f"{sha}:{rel.as_posix()}"],
        cwd=repo_root, capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        return None
    baseline_meta, _ = _split_frontmatter(result.stdout)

    def _flatten(val: object) -> str:
        return "\n".join(str(x) for x in val).strip() if isinstance(val, list) else str(val or "").strip()

    baseline_close = _flatten(baseline_meta.get("close_criteria"))
    current_close = _flatten(ticket.get("close_criteria"))
    if not baseline_close or baseline_close == current_close:
        return None

    approved_snapshot = _flatten(ticket.get("close_criteria_drift_approved_snapshot"))
    if approved_snapshot and approved_snapshot == current_close:
        return None

    return (
        f"close_criteria changed since it was analyzed (commit {sha[:8]}) without a "
        "recorded human approval — if this scope change was intentional, run "
        "`lanegate human-review-approve <id> --rationale ...`; otherwise restore the "
        "original close_criteria or implement it as written"
    )


def audit_acceptance_contract(
    ticket: dict,
    repo_root: Path,
    *,
    close_criteria: str | None = None,
    change_notes: dict | None = None,
    include_file_skeletons: bool = False,
) -> AcceptanceContractAudit:
    """Compare source intent and linked contracts against acceptance criteria."""
    effective_close = (
        close_criteria if close_criteria is not None else ticket.get("close_criteria", "")
    )
    if isinstance(effective_close, list):
        effective_close = "\n".join(str(item) for item in effective_close)
    effective_notes = change_notes if change_notes is not None else load_change_notes(ticket)

    clean_body = _strip_operational_sections(ticket.get("_body", "") or "")
    non_goals = _extract_non_goals(ticket.get("_body", "") or "")
    source_sections: list[tuple[str, str]] = [
        ("ticket title/body", f"{ticket.get('title', '')}\n{clean_body}"),
    ]
    source_sections.extend(_linked_context_refs(ticket, repo_root))
    if effective_notes:
        source_sections.append(("change_notes", _flatten_change_notes(effective_notes)))
    if include_file_skeletons:
        skeletons = load_file_skeletons(ticket, repo_root)
        if skeletons:
            source_sections.append(
                (
                    "file_skeletons",
                    "\n".join(skeletons.values())[:_MAX_ACCEPTANCE_REF_BYTES],
                )
            )

    items: list[_ContractItem] = []
    seen_items: set[tuple[str, str]] = set()
    for source, text in source_sections:
        for item in _extract_contract_items(text, source):
            key = (item.source, item.label.lower())
            if key not in seen_items:
                seen_items.add(key)
                items.append(item)

    missing = [
        item
        for item in items
        if not _covered_by_text(item, effective_close) and not _covered_by_text(item, non_goals)
    ]
    sources = sorted({item.source for item in items})
    findings: list[str] = []
    if missing:
        by_source: dict[str, list[str]] = {}
        for item in missing:
            by_source.setdefault(item.source, []).append(item.label)
        for source, labels in by_source.items():
            shown = labels[:12]
            suffix = f" (+ {len(labels) - len(shown)} more)" if len(labels) > len(shown) else ""
            findings.append(
                f"close_criteria omits contract items from {source}: "
                + ", ".join(shown)
                + suffix
            )

    drift_finding = _close_criteria_drift_finding(ticket, repo_root)
    if drift_finding:
        findings.append(drift_finding)

    return AcceptanceContractAudit(
        ok=not findings,
        findings=findings,
        omitted_items=[item.label for item in missing],
        sources=sources,
        checked_items=[item.label for item in items],
    )


# ---------------------------------------------------------------------------
# Per-criterion verification
# ---------------------------------------------------------------------------
#
# Distinct from AcceptanceContractAudit above: that audit checks whether a
# requirement was *mentioned* in close_criteria (did the plan cover it), not
# whether the delivered diff actually satisfies it. This checks each
# Acceptance Criteria checklist item in the ticket body against the ticket's
# own committed diff for matching evidence, so a ticket can't report "ok"
# while the described behavior is simply absent from the diff.

_ACCEPTANCE_SECTION_RE = re.compile(
    r"(?:^|\n)##\s*Acceptance Criteria\s*\n(.*?)(?=\n##\s|\Z)", re.IGNORECASE | re.DOTALL
)
_CHECKLIST_ITEM_RE = re.compile(r"^\s*-\s*\[[ xX]\]\s*(.+)$", re.MULTILINE)

# Criteria phrased as something only a human can do (confirm against a live
# system, investigate, manually verify) are never text-matchable against a
# diff -- they get status "manual" straight away rather than a false
# "unverified" that no amount of code could ever turn "verified".
_MANUAL_JUDGMENT_MARKERS = ("confirm", "investigate", "manually", "by hand", "human")

# "Full suite green" (or equivalent phrasing) is on nearly every ticket's
# checklist, and is already deterministically enforced by a *separate* gate:
# the pre_complete/pre_merge pytest safeguard blocks cmd_complete/cmd_merge
# outright on any failure (see lifecycle/__init__.py's run_safeguards calls).
# By the time this function runs, that gate has already passed -- text-
# matching this criterion against the diff would be redundant at best and,
# in practice, would leave nearly every ticket with an "unverified" item
# purely because "green"/passing isn't something a diff's text can show.
def _is_suite_green_criterion(lowered: str) -> bool:
    return "suite" in lowered and ("green" in lowered or "pass" in lowered)


@dataclass
class VerificationRecord:
    """One acceptance-criterion's verification state, with evidence."""

    criterion: str
    status: str  # "verified" | "unverified" | "manual"
    evidence: str = ""
    checked_at: str | None = None

    def as_metadata(self) -> dict:
        return {
            "criterion": self.criterion,
            "status": self.status,
            "evidence": self.evidence,
            "checked_at": self.checked_at,
        }


def _extract_acceptance_checklist(body: str) -> list[str]:
    """Return each `- [ ]`/`- [x]` line under a '## Acceptance Criteria'
    heading, in order, with checkbox markup stripped."""
    section = _ACCEPTANCE_SECTION_RE.search(body or "")
    if not section:
        return []
    items = []
    for match in _CHECKLIST_ITEM_RE.finditer(section.group(1)):
        text = re.sub(r"\s+", " ", match.group(1)).strip()
        if text:
            items.append(text)
    return items


def _worktree_diff_text(
    repo_root: Path, *, max_bytes: int = 200_000, trunk_branch: str | None = None
) -> str:
    """Diff of repo_root's checked-out branch against its merge-base with
    the resolved trunk branch. Empty string (never raises) when repo_root isn't a ticket worktree
    on its own branch, or git fails for any reason."""
    trunk_branch = trunk_branch or resolve_trunk_branch(load_config(repo_root), repo_root)
    base = subprocess.run(
        ["git", "merge-base", trunk_branch, "HEAD"], cwd=repo_root, capture_output=True, text=True, encoding="utf-8"
    )
    if base.returncode != 0:
        return ""
    merge_base = base.stdout.strip()
    diff = subprocess.run(
        ["git", "diff", merge_base, "HEAD"], cwd=repo_root, capture_output=True, text=True, encoding="utf-8"
    )
    if diff.returncode != 0:
        return ""
    return diff.stdout[:max_bytes]


def _matching_terms(item: _ContractItem, text: str) -> list[str]:
    """Terms from item that literally appear in text (case/whitespace normalized)."""
    haystack = _normalize_contract_text(text)
    if not haystack:
        return []
    terms = [t for t in item.terms if t not in _STOPWORDS]
    return [t for t in terms if t in haystack]


def verify_acceptance_criteria(
    ticket: dict,
    repo_root: Path,
    *,
    prior: list[dict] | None = None,
    diff_text: str | None = None,
    trunk_branch: str | None = None,
) -> list[VerificationRecord]:
    """Check each Acceptance Criteria checklist item against the ticket's own
    committed diff for matching evidence.

    A criterion phrased as needing human judgment (see
    _MANUAL_JUDGMENT_MARKERS) is marked "manual" outright. Otherwise it's
    "verified" when enough of its terms appear in the diff, else
    "unverified". A prior record already at "manual" (a human explicitly
    signed off via `lanegate review --findings`) stays "manual" even if this
    re-run can't find automated evidence -- that sign-off shouldn't evaporate
    on a later re-review of the same ticket.
    """
    criteria = _extract_acceptance_checklist(ticket.get("_body", "") or "")
    if not criteria:
        return []

    prior_by_criterion = {p.get("criterion"): p for p in (prior or []) if isinstance(p, dict)}
    text = (
        diff_text
        if diff_text is not None
        else _worktree_diff_text(repo_root, trunk_branch=trunk_branch)
    )

    records: list[VerificationRecord] = []
    for criterion in criteria:
        lowered = criterion.lower()
        if _is_suite_green_criterion(lowered):
            records.append(
                VerificationRecord(
                    criterion=criterion,
                    status="verified",
                    evidence="enforced separately by the pre_complete/pre_merge pytest "
                    "safeguard, which already blocks this ticket outright on any failure",
                )
            )
            continue
        if any(marker in lowered for marker in _MANUAL_JUDGMENT_MARKERS):
            records.append(
                VerificationRecord(
                    criterion=criterion,
                    status="manual",
                    evidence="requires human confirmation (not text-matchable against a diff)",
                )
            )
            continue

        item = _ContractItem(
            label=criterion, source="acceptance_criteria", terms=_contract_terms(criterion)
        )
        matched = _matching_terms(item, text)
        required = max(1, min(len(item.terms), 3)) if item.terms else 1
        if item.terms and len(matched) >= required:
            records.append(
                VerificationRecord(
                    criterion=criterion,
                    status="verified",
                    evidence=f"{len(matched)}/{len(item.terms)} terms matched in diff: "
                    + ", ".join(matched[:6]),
                )
            )
            continue

        prior_record = prior_by_criterion.get(criterion)
        if prior_record and prior_record.get("status") == "manual":
            records.append(
                VerificationRecord(
                    criterion=criterion,
                    status="manual",
                    evidence=prior_record.get("evidence", ""),
                    checked_at=prior_record.get("checked_at"),
                )
            )
        else:
            records.append(
                VerificationRecord(
                    criterion=criterion,
                    status="unverified",
                    evidence="no matching evidence found in the diff",
                )
            )
    return records


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


_ACTIVE_CONTROL_PLANE_BUDGET_BYTES = 4000


def _active_control_plane_ticket_context(
    ticket: dict, tickets_dir: Path, cfg: dict | None = None,
) -> str:
    """Describe active LaneGate touches the analyzer must plan around.

    The overlap gate runs after the model proposes touches, so this bounded
    metadata is the model's only way to name the relevant active ticket IDs.
    """
    if not tickets_dir.exists():
        return ""
    prefix = (cfg or {}).get("ticket_prefix", "TICK")
    active: list[str] = []
    for other in load_all_tickets(tickets_dir, prefix, cfg)[0]:
        other_id = other.get("id")
        if not other_id or other_id == ticket.get("id") or other.get("status") in TERMINAL_STATUSES:
            continue
        touches = sorted(str(path) for path in (other.get("touches") or []))
        if touches and is_high_reasoning_ticket(other):
            active.append(f"- {other_id} ({other.get('status', 'unknown')}): {', '.join(touches)}")
    if not active:
        return ""
    # Whole-line budget, not a raw byte truncation like the sibling sections
    # (collect_cross_ticket_change_notes etc.): a byte cutoff mid-line could
    # silently drop the back half of a ticket's touches list, which would
    # misrepresent that ticket's true touches to the model doing overlap
    # detection here -- worse than omitting it entirely. When truncated, say
    # so explicitly so the model doesn't treat a partial list as exhaustive.
    header = "## Active control-plane tickets\n"
    budget = _ACTIVE_CONTROL_PLANE_BUDGET_BYTES
    kept: list[str] = []
    used = len(header.encode("utf-8"))
    for i, line in enumerate(active):
        line_bytes = len((line + "\n").encode("utf-8"))
        if used + line_bytes > budget:
            kept.append(
                f"- ... {len(active) - i} more active control-plane ticket(s) omitted (over "
                "budget) -- do not assume this list is exhaustive; check any touch you propose "
                "explicitly rather than relying on its absence here."
            )
            break
        kept.append(line)
        used += line_bytes
    return header + "\n".join(kept)


def _build_prompt(
    ticket: dict, repo_root: Path, cfg: dict | None = None, *, _components: list | None = None
) -> str:
    from lanegate.prompts import (
        component_for,
        get_bounded_shared_notes,
        get_bounded_reference_excerpts,
        load_project_guidance,
        load_prompt_template,
        render_prompt,
        resolve_reference_doc_paths,
    )

    intent = ticket.get("_body", ticket.get("title", ""))
    ctx = enrich_context(ticket.get("title", "") + " " + intent, repo_root)

    symbol_hits_text = "\n".join(ctx.symbol_hits) if ctx.symbol_hits else "(none)"
    importers_text = "\n".join(ctx.importers) if ctx.importers else "(none)"
    ripgrep_text = ctx.ripgrep_hits if ctx.ripgrep_hits else "(none)"
    repo_structure_text = ctx.repo_structure or "(no git-tracked files found)"
    sections = []
    if ctx.repo_structure:
        sections.append("## Repository structure\n" + ctx.repo_structure)

    # Touches don't exist yet at analyze time (that's what this step is
    # computing) -- the AST/tree-sitter symbol-hit files stand in as the
    # "direct import/module context" relevance signal for bounding project
    # guidance and the architecture doc.
    relevant_paths = list(ctx.symbol_hits)
    requires_acceptance_matrix = is_high_reasoning_ticket(ticket)

    shared_notes = get_bounded_shared_notes(repo_root, relevant_paths, cfg=cfg, step="analyze")
    if shared_notes:
        sections.append(shared_notes)
    if _components is not None:
        _components.append(component_for(
            "shared-notes", ".lanegate/notes", "analyze", shared_notes,
            reason="global-and-touch-relevant" if shared_notes else "no-relevant-notes",
        ))

    # Cross-ticket change_notes: surface what prior merged/done tickets recorded
    # about files this ticket is likely to touch, before `touches` itself is
    # known -- relevant_paths (the symbol-hit candidates above) stands in for
    # candidate touches here, same as it does for project_guidance scoping.
    cross_ticket_tickets_dir = repo_root / (cfg or {}).get("tickets_dir", ".lanegate/tickets")
    cross_ticket_notes = collect_cross_ticket_change_notes(
        {"id": ticket.get("id"), "touches": relevant_paths},
        cross_ticket_tickets_dir,
        cfg,
        exclude_id=ticket.get("id"),
    )
    if cross_ticket_notes:
        sections.append(cross_ticket_notes)
    if _components is not None:
        _components.append(component_for(
            "cross-ticket-change-notes", "prior tickets.change_notes", "analyze",
            cross_ticket_notes,
            reason="matched-and-bounded" if cross_ticket_notes else "no-overlap",
        ))

    # find_control_plane_touch_overlaps (the actual gate, run later once
    # touches are known) only ever fires for a high-reasoning ticket -- for
    # any other ticket this section can never become enforceable, so showing
    # it is pure noise (and cost) with no corresponding contract to satisfy.
    active_control_plane_tickets = (
        _active_control_plane_ticket_context(ticket, cross_ticket_tickets_dir, cfg)
        if requires_acceptance_matrix
        else ""
    )
    if active_control_plane_tickets:
        sections.append(active_control_plane_tickets)
    if _components is not None:
        _components.append(component_for(
            "active-control-plane-tickets", "active tickets.touches", "analyze",
            active_control_plane_tickets,
            reason="active-control-plane-touches" if active_control_plane_tickets else "no-active-control-plane-touches",
        ))

    # The overlap gate is evaluated (in _cmd_analyze_core) against this
    # ticket's existing touches too, not just what the model is about to
    # propose -- a re-analysis of an already-`open` ticket carries these
    # forward. Unlike touches inferred from close_criteria (not known until
    # after the model responds), existing touches ARE known now, so check
    # them for real and tell the model exactly which active tickets they
    # already collide with -- otherwise a collision here is unsatisfiable by
    # any response, since the model was never told this path is even in play.
    existing_touches = ticket.get("touches") or []
    existing_touch_overlaps = (
        find_control_plane_touch_overlaps(
            {**ticket, "touches": existing_touches}, cross_ticket_tickets_dir, cfg,
            exclude_id=ticket.get("id"),
        )
        if existing_touches
        else []
    )
    if existing_touch_overlaps:
        overlap_lines = "\n".join(
            f"- {item['ticket_id']}: {', '.join(cast(list, item['paths']))}" for item in existing_touch_overlaps
        )
        sections.append(
            "## Your ticket's existing touches already overlap active tickets\n"
            f"{overlap_lines}\n"
            "These paths are already on this ticket (carried forward) even if you don't "
            "re-propose them yourself. overlap_review must still name every ticket ID listed "
            "above."
        )
    if _components is not None:
        _components.append(component_for(
            "existing-touch-overlaps", "ticket.touches x active tickets", "analyze",
            "\n".join(f"{item['ticket_id']}: {', '.join(cast(list, item['paths']))}" for item in existing_touch_overlaps),
            reason="existing-touches-overlap" if existing_touch_overlaps else "no-existing-touch-overlap",
        ))

    ref_doc_paths = resolve_reference_doc_paths(repo_root, cfg)
    project_guidance = load_project_guidance(
        repo_root, cfg, step="analyze", relevant_paths=relevant_paths,
        exclude_paths=ref_doc_paths or None,
    )
    if project_guidance:
        sections.append(project_guidance)
    if _components is not None:
        sections_component_reason = "matched-and-bounded" if project_guidance else "no-matching-files"
        _components.append(component_for(
            "project-guidance", "project_guidance.files", "analyze", project_guidance,
            reason=sections_component_reason,
        ))

    ref_excerpt, ref_components = get_bounded_reference_excerpts(
        repo_root, relevant_paths, cfg=cfg, step="analyze"
    )
    if ref_excerpt:
        sections.append(ref_excerpt)
    if _components is not None:
        _components.extend(ref_components)

    candidate_skeletons_text = _build_candidate_skeletons(relevant_paths, repo_root)
    if candidate_skeletons_text:
        sections.append(candidate_skeletons_text)
    if _components is not None:
        _components.append(component_for(
            "candidate-skeletons", "stdlib ast (per-file signatures)", "analyze",
            candidate_skeletons_text,
            reason="matched-and-bounded" if candidate_skeletons_text else "no-candidate-files",
        ))

    sections += [
        "## Symbol matches (AST / code-index search)\n"
        "Files whose defined symbols match the ticket intent:\n" + symbol_hits_text,
        "## Importers (one-hop import graph)\n"
        "Files that import the symbol-match files above:\n" + importers_text,
        "## Ripgrep keyword hits (fallback - shown when symbol search found nothing)\n"
        + ripgrep_text,
    ]
    context_sections = "\n\n".join(sections)

    if _components is not None:
        _components.append(component_for("symbol-hits", "AST/tree-sitter index", "analyze", symbol_hits_text))
        _components.append(component_for("importers", "one-hop import graph", "analyze", importers_text))
        _components.append(component_for(
            "ripgrep-hits", "ripgrep fallback", "analyze", ripgrep_text,
            reason="fallback-when-no-symbol-hits",
        ))
        _components.append(component_for(
            "repo-structure", "git ls-files (last resort)", "analyze", repo_structure_text,
            reason="last-resort-when-no-hits",
        ))
        _components.append(component_for("ticket-title", "ticket.title", "analyze", ticket.get("title", "")))
        _components.append(component_for("ticket-intent", "ticket.title + ticket._body", "analyze", intent))

    template = load_prompt_template("analyze", repo_root)
    prompt = render_prompt(
        template,
        context_sections=context_sections,
        repo_structure=repo_structure_text,
        symbol_hits=symbol_hits_text,
        importers=importers_text,
        ripgrep_hits=ripgrep_text,
        ticket_id=ticket["id"],
        title=ticket.get("title", ""),
        intent=intent,
        requires_acceptance_matrix=str(requires_acceptance_matrix).lower(),
    )
    # A project override may predate the structured high-risk contract and
    # omit its placeholder. The gate below is still mandatory, so append the
    # required response shape outside the override instead of making a valid
    # high-risk analysis impossible with no explanation.
    if requires_acceptance_matrix:
        prompt += (
            "\n\n## Required high-risk response contract\n"
            "Return acceptance_matrix with non-empty invariants, adversarial_cases, "
            "compatibility_cases, and regression_tests lists. This requirement applies "
            "even when the project uses a custom analyze prompt.\n"
        )
    # Same portability gap for the sibling overlap gate: an override lacking
    # {{ context_sections }} drops the "Active control-plane tickets" listing
    # above too, but the gate is unconditional whenever this ticket's touches
    # turn out to overlap one of those tickets. Without this, the model has
    # no way to learn overlap_review's shape and every retry fails identically
    # until the other ticket reaches a terminal status.
    if active_control_plane_tickets:
        prompt += (
            "\n\n## Required control-plane overlap response contract\n"
            "If your proposed touches overlap any ticket listed under 'Active control-plane "
            "tickets' above, return overlap_review as a mapping with mode ('dependencies' or "
            "'stacked_review') and a non-empty ticket_ids list naming every overlapping ticket "
            "ID. If mode is 'dependencies', those same ticket IDs must also appear in "
            "depends_on. This requirement applies even when the project uses a custom analyze "
            "prompt.\n"
        )
    return prompt


def describe_analyze_payload(ticket: dict, repo_root: Path, cfg: dict | None = None) -> list[dict]:
    """Return a machine-readable breakdown of every component in the analyze
    prompt payload for *ticket* -- byte/token estimate, source, pipeline step,
    and whether it's always injected or selected because of the ticket.

    Component metadata only; never includes the ticket's actual title/body/
    intent text, so this is safe to log or display by default (payload
    audit).
    """
    components: list = []
    _build_prompt(ticket, repo_root, cfg, _components=components)
    return [c.as_dict() for c in components]


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _find_json_object(text: str) -> str | None:
    """Return the span of the first balanced {...} object in text, or None.

    A greedy first-{-to-last-} regex breaks whenever the model appends any
    trailing content that itself contains braces (a follow-up example, a
    second illustrative snippet); it silently absorbs that tail into the
    match, and json.loads then fails with a misleading "Extra data" error
    pointing well past the real object. Track string/escape state so braces
    inside string values don't perturb the depth count.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i, ch in enumerate(text[start:], start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _parse_response(text: str) -> dict:
    """Extract the JSON object from the model response."""
    # Strip thinking/reasoning blocks emitted by models (e.g. Qwen, DeepSeek-R1)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    # Strip markdown code fences if present
    text = re.sub(r"```(?:json)?\s*", "", text).strip()
    text = text.rstrip("`").strip()
    span = _find_json_object(text)
    if span is None:
        raise ValueError(f"No JSON object found in model response:\n{text[:400]}")
    # strict=False tolerates literal control characters (e.g. a raw newline)
    # inside a string value -- smaller/local models routinely hard-wrap long
    # string values instead of escaping the break, which is otherwise a hard
    # parse failure despite the JSON being structurally well-formed.
    return json.loads(span, strict=False)


_ALREADY_RESOLVED_CITATION_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])(?P<path>(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+|"
    r"[A-Za-z0-9_-]+\.[A-Za-z0-9_.-]+|(?:Makefile|Dockerfile|Containerfile|Rakefile|Gemfile|Procfile))"
    r"\s*:\s*~?L?"
    r"(?P<start>\d+)(?:\s*[-–]\s*~?L?(?P<end>\d+))?"
)


def _already_resolved_reason_matches_worktree(reason: str, repo_root: Path) -> tuple[bool, str | None]:
    """Reject cited resolved claims whose files, lines, or code snippets do not match.

    Generalized reasons remain reviewable: only reasons that make a concrete
    file-and-line claim are checked here.  The check intentionally fails closed
    when a cited range cannot substantiate an inline code snippet.
    """
    citations = list(_ALREADY_RESOLVED_CITATION_RE.finditer(reason))
    if not citations:
        return True, None

    cited_text: list[str] = []
    for citation in citations:
        relative = Path(citation.group("path"))
        if relative.is_absolute() or ".." in relative.parts:
            return False, f"unsafe cited path {relative}"
        path = repo_root / relative
        try:
            path.resolve().relative_to(repo_root.resolve())
        except ValueError:
            return False, f"cited path {relative} escapes the worktree"
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            return False, f"cited file {relative} cannot be read"
        start = int(citation.group("start"))
        end = int(citation.group("end") or start)
        if start < 1 or end < start or end > len(lines):
            return False, f"cited range {relative}:{start}-{end} is not in the worktree"
        cited_text.append("\n".join(lines[start - 1:end]))

    # Backticks are the model response format's unambiguous marker for a code
    # claim.  Require every such snippet to be present in at least one cited
    # range, after normalizing whitespace so formatting-only changes do not
    # cause a false rejection.
    snippets = [
        snippet
        for snippet in re.findall(r"`([^`\n]+)`", reason)
        if _ALREADY_RESOLVED_CITATION_RE.fullmatch(snippet.strip()) is None
        # Backticks also commonly delimit filenames, symbols, and concepts.
        # Only syntax-bearing text is an unambiguous literal code claim.
        if re.search(r"[(){}\[\]=;:]", snippet)
        or re.match(
            r"\s*(?:async\s+def|def|class|return|raise|import|from|if|elif|else|for|while|try|except)\b",
            snippet,
        )
    ]
    normalized_ranges = [re.sub(r"\s+", " ", text).strip() for text in cited_text]
    for snippet in snippets:
        normalized_snippet = re.sub(r"\s+", " ", snippet).strip()
        if normalized_snippet and not any(normalized_snippet in text for text in normalized_ranges):
            return False, f"cited code snippet {snippet!r} is absent from the cited worktree lines"
    return True, None


# ---------------------------------------------------------------------------
# Main command
# ---------------------------------------------------------------------------


def _cmd_analyze_core(
    ticket_id: str,
    cfg: dict,
    repo_root: Path,
    *,
    model_fn=None,
    keep_draft: bool = False,
    visibility: _AnalysisVisibility,
    pool_name: str | None = None,
) -> None:
    """Analyze a draft ticket: populate touches + close_criteria.

    Args:
        model_fn: optional override for the model seam (used in tests).
            When provided, called as ``model_fn(prompt)`` — the resolved model
            string is NOT passed (tests supply their own stub logic).
        keep_draft: when True, leave status as draft after populating touches
            (used by `lanegate create` so the user can review before the ticket
            enters the work queue).
        pool_name: name of a `pools:` entry to draw the analyze
            executor from, overriding the ticket's routed/default pool — the
            same override `orchestrate --pool` applies to implement/review/fix.
    """
    from lanegate.config import resolve_model as _resolve_model, validate_model_for_executor

    tid = canonical_id(ticket_id)
    tickets_dir = repo_root / cfg["tickets_dir"]

    tickets, _ = load_all_tickets(tickets_dir, cfg["ticket_prefix"], cfg)
    ticket = next((t for t in tickets if t["id"] == tid), None)
    if ticket is None:
        print(f"ERROR: {tid} not found", file=sys.stderr)
        sys.exit(1)

    if ticket.get("status") not in ("draft", "open"):
        print(
            f"ERROR: {tid} has status '{ticket.get('status')}'; analyze only works on draft or open tickets",
            file=sys.stderr,
        )
        sys.exit(1)

    # Resolve model and executor for the analyze step (only used when model_fn is not provided)
    from lanegate.executor import write_cooldown as _write_executor_cooldown
    from lanegate.orchestrate import (
        _is_rate_limit,
        _rate_limit_reason,
        resolve_pool_executor,
    )
    from lanegate.orchestrate import (
        expand_driver as _expand_driver,
    )
    from lanegate.orchestrate import (
        resolve_driver as _resolve_driver,
    )

    def resolve_analyze_driver(
        excluded: set[str] | None = None, pool_name: str | None = None
    ) -> tuple[str, dict, str, str | None]:
        driver_name = resolve_pool_executor(
            "analyze",
            ticket,
            cfg,
            repo_root,
            pool_name=pool_name,
            excluded=excluded,
            healthy_only=bool(excluded),
        )
        if driver_name is None:
            raise RuntimeError(
                "no healthy pool sibling is available for analyze: every executor in "
                "this ticket's executor pool is cooling down from a prior rate limit "
                "or failure, so none can be dispatched right now; run "
                "`lanegate executor status` to see which instance(s) are cooling down "
                "and when they'll recover, then retry `lanegate analyze <id>`"
            )
        from lanegate.executor import get_executor_config as _get_executor_config

        driver_cfg = _expand_driver(driver_name, cfg)
        executor = driver_cfg.get("type", driver_name)
        effective_cfg = dict(cfg, executor=executor) if executor != cfg.get("executor") else cfg
        analyze_model_override = driver_cfg.get("model")
        model = analyze_model_override or _resolve_model(effective_cfg, "analyze", ticket=ticket)
        if model:
            # `executor` here can be a named `executors:` instance (e.g.
            # "aider-ollama-14b"), not a resolved driver type -- validating
            # against it directly matches no branch in
            # validate_model_for_executor and silently no-ops. Resolve the
            # actual type (and provider) the same way review/pool dispatch
            # do, and validate regardless of whether the model came from a
            # driver-level override or resolve_model's fallback chain.
            resolved_executor_cfg = _get_executor_config(executor, effective_cfg)
            resolved_type = resolved_executor_cfg.get("type", executor)
            validate_model_for_executor(
                model,
                resolved_type,
                "models.analyze",
                provider=resolved_executor_cfg.get("provider") or driver_cfg.get("provider"),
            )
        return driver_name, driver_cfg, executor, model

    analyze_driver_name, analyze_driver_cfg, analyze_executor, analyze_model = resolve_analyze_driver(
        pool_name=pool_name
    )
    visibility.set_driver(analyze_executor, analyze_model)
    implement_driver_name = _resolve_driver("implement", ticket, cfg)
    implement_driver_cfg = _expand_driver(implement_driver_name, cfg)
    implement_executor = implement_driver_cfg.get("type", implement_driver_name)

    if model_fn is not None:

        def call_model(prompt: str) -> tuple[str, str | None]:
            result = model_fn(prompt)
            if isinstance(result, tuple):
                return result
            return result, None
    else:

        def call_model(prompt: str) -> tuple[str, str | None]:
            return _call_model(
                prompt,
                model=analyze_model,
                executor=analyze_executor,
                cfg=cfg,
                driver_cfg=analyze_driver_cfg,
                repo_root=repo_root,
                tid=tid,
            )

    def call_model_with_failover(model_prompt: str) -> tuple[str, str | None]:
        """Call the current analyzer, failing over from rate-limited siblings."""
        nonlocal analyze_driver_name, analyze_driver_cfg, analyze_executor, analyze_model
        max_retries = int(cfg.get("max_sibling_retries", 1))
        excluded: set[str] = set()
        attempts = 0
        while True:
            try:
                response = call_model(model_prompt)
                _clear_analyze_failure_streak(repo_root, analyze_driver_name)
                return response
            except RuntimeError as exc:
                raw_stdout = getattr(exc, "raw_stdout", "")
                raw_stderr = getattr(exc, "raw_stderr", "") or str(exc)
                is_rate_limited = _is_rate_limit(
                    1, repo_root, captured_stdout=raw_stdout, captured_stderr=raw_stderr
                )
                if model_fn is None and not is_rate_limited:
                    failure_count = _record_analyze_failure(
                        repo_root, analyze_driver_name, raw_stdout, raw_stderr
                    )
                    if failure_count == _ANALYZE_FAILURE_COOLDOWN_THRESHOLD:
                        _write_executor_cooldown(
                            repo_root,
                            analyze_driver_name,
                            "analyze executor failed consecutive non-rate-limit calls",
                        )
                elif is_rate_limited:
                    _clear_analyze_failure_streak(repo_root, analyze_driver_name)
                if model_fn is not None or attempts >= max_retries or not is_rate_limited:
                    raise
                reason = _rate_limit_reason(
                    1, repo_root, captured_stdout=raw_stdout, captured_stderr=raw_stderr
                )
                _write_executor_cooldown(repo_root, analyze_driver_name, reason, retry_after=reason)
                excluded.add(analyze_driver_name)
                sibling_name, sibling_cfg, sibling_executor, sibling_model = resolve_analyze_driver(
                    excluded, pool_name=pool_name
                )
                if sibling_name == analyze_driver_name:
                    raise
                attempts += 1
                print(
                    f"[analyze] {tid}: {analyze_driver_name} hit a rate limit — "
                    f"retrying on healthy sibling {sibling_name!r}",
                    file=sys.stderr,
                )
                analyze_driver_name = sibling_name
                analyze_driver_cfg = sibling_cfg
                analyze_executor = sibling_executor
                analyze_model = sibling_model
                visibility.set_driver(analyze_executor, analyze_model)
                visibility.emit(
                    "model_requested",
                    f"Executor: {analyze_executor} Model: {analyze_model or 'default'}",
                )

    # Build prompt and call model. Analyze is read-only, so unlike implement
    # it can safely retry immediately on a healthy pool sibling after a quota
    # error; no in-worktree progress check is relevant here.
    visibility.emit("context_indexed", "Indexing context...")
    prompt = _build_prompt(ticket, repo_root, cfg)
    visibility.emit("prompt_ready", f"Prompt ready ({_estimate_prompt_tokens(prompt)} tokens)")
    visibility.emit(
        "model_requested",
        f"Executor: {analyze_executor} Model: {analyze_model or 'default'}",
    )
    visibility.emit("model_requested", "Waiting for model... (elapsed 0s)")
    waiting_reporter = _WaitingReporter(
        lambda message: visibility.emit("model_requested", message),
        visibility.started_at,
        float(cfg.get("analyze_wait_interval_seconds", 10)),
    )
    waiting_reporter.start()
    try:
        raw, analyze_session_id = call_model_with_failover(prompt)
    except Exception as exc:
        print(f"ERROR: model call failed: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        waiting_reporter.stop()

    visibility.emit("model_responded", f"Model responded ({len(raw)} characters)")
    visibility.executor_output(raw)

    # Parse response
    try:
        result = _parse_response(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: could not parse model response: {exc}", file=sys.stderr)
        print(f"Raw response:\n{raw[:600]}", file=sys.stderr)
        sys.exit(1)

    # A specific already-resolved claim has a higher consequence than an
    # ordinary analysis response.  Verify concrete citations before allowing
    # it to move the ticket to needs_review; a bad claim gets one normal
    # analysis retry rather than being persisted as a plausible-sounding fact.
    fallback_attempted = False
    while bool(result.get("already_resolved")):
        reason = (result.get("already_resolved_reason") or "").strip()
        verified, mismatch = _already_resolved_reason_matches_worktree(reason, repo_root)
        if verified:
            break
        if fallback_attempted:
            print(
                f"ERROR: normal analysis fallback returned an invalid already_resolved verdict: {mismatch}",
                file=sys.stderr,
            )
            sys.exit(1)
        fallback_attempted = True
        if not verified:
            warning = (
                f"WARNING: {tid}: rejected already_resolved verdict because {mismatch}; "
                "requesting a normal analysis pass"
            )
            visibility.emit("already_resolved_rejected", warning)
            print(warning, file=sys.stderr)
            try:
                retry_raw, analyze_session_id = call_model_with_failover(
                    prompt + "\n\nThe prior already_resolved claim did not match the current worktree. "
                    "Provide a normal analysis response with touches and close_criteria; do not return "
                    "already_resolved."
                )
                raw = retry_raw
                visibility.executor_output(raw)
                result = _parse_response(raw)
            except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
                print(f"ERROR: normal analysis fallback failed: {exc}", file=sys.stderr)
                sys.exit(1)

    close_criteria = result.get("close_criteria", "")
    if isinstance(close_criteria, str):
        close_criteria = close_criteria.strip()

    # Restore the ticket's acceptance contract before close_criteria influences
    # any derived state, including already_resolved branches, inferred touches,
    # and companion docs.
    original_criteria = ticket.get("close_criteria", "")
    if _close_criteria_drifted(original_criteria, close_criteria):
        visibility.emit(
            "drift",
            f"WARNING: {tid}: model rewrote close_criteria — restoring original wording",
        )
        print(
            f"WARNING: {tid}: model rewrote close_criteria — restoring original wording",
            file=sys.stderr,
        )
        close_criteria = original_criteria
        result["close_criteria"] = original_criteria

    already_resolved = bool(result.get("already_resolved"))
    already_resolved_reason = (result.get("already_resolved_reason") or "").strip()

    if already_resolved:
        if not already_resolved_reason:
            print(
                "ERROR: model returned already_resolved=true with no reason; ticket left as draft",
                file=sys.stderr,
            )
            sys.exit(1)
        body = ticket.get("_body") or ""
        if body and not body.endswith("\n"):
            body += "\n"
        body += (
            "\n## Needs Review Reason\n"
            "analyze: ticket premise appears already resolved in current code — "
            f"{already_resolved_reason}\n"
        )
        ticket["_body"] = body
        ticket["status"] = "needs_review"
        ticket["review_summary"] = f"already_resolved: {already_resolved_reason}"
        ticket["status_changed_at"] = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        write_ticket(ticket)
        if cfg.get("commit_status_changes", True):
            subprocess.run(
                ["git", "add", "-f", str(ticket["_path"])],
                cwd=repo_root,
                check=False,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-s", "--only", str(ticket["_path"]), "-m", f"chore: {tid} analyzed — flagged already_resolved (needs_review)"],
                cwd=repo_root,
                check=False,
                capture_output=True,
            )
        print(f"{tid}: analyze believes this is already resolved — status set to needs_review", file=sys.stderr)
        print(f"  reason: {already_resolved_reason}", file=sys.stderr)
        print(
            f"  next: verify, then `lanegate supersede {tid} --reason ...` to close, "
            f"or re-run `lanegate analyze {tid}` if the model is wrong",
            file=sys.stderr,
        )
        visibility.emit("analysis_complete", f"Analysis complete for {tid} (already_resolved)")
        sys.exit(0)

    touches = result.get("touches")
    depends_on = result.get("depends_on") or []
    change_notes = result.get("change_notes") or {}
    model = result.get("model")
    acceptance_matrix = result.get("acceptance_matrix")
    overlap_review = result.get("overlap_review")

    if not touches or not isinstance(touches, list):
        print(
            "ERROR: model returned empty or non-list touches; ticket left as draft", file=sys.stderr
        )
        sys.exit(1)
    # NOTE: empty close_criteria is checked *after* the drift guard above so
    # that a model that omits criteria for a ticket with an existing one can
    # have the original restored before we decide whether to hard-fail.

    existing_touches = ticket.get("touches") or []
    merged_touches: list[str] = []
    touches_set: set[str] = set()
    for path in [*existing_touches, *touches]:
        if path not in touches_set:
            merged_touches.append(path)
            touches_set.add(path)

    # The control-plane overlap gate below is evaluated against this
    # snapshot, not the fully-augmented merged_touches: infer_touches_from_
    # criteria and companion_docs_from_criteria (next two blocks) only run
    # *after* this point, using the model's own close_criteria it just
    # returned, so a path either of them adds could never have been shown to
    # or asked about by the model that already responded. Gating on a path
    # the model could not possibly have declared in overlap_review makes the
    # ticket permanently unanalyzable -- every retry regenerates the
    # identical augmented path from the identical close_criteria. Existing
    # touches (carried forward from a prior draft, e.g. re-analysis) ARE
    # known before the prompt is built and are surfaced there instead (see
    # _build_prompt's "existing touches already overlap" section), so they
    # stay in the gate.
    model_visible_touches = list(merged_touches)

    # Augment touches with files statically implied by close_criteria + title.
    # Do NOT include _body: background prose ("lanegate board is broken") would
    # inject false file references for things the fix never touches.
    scan_text = ticket.get("title", "") + " " + _close_criteria_as_str(close_criteria)
    inferred = infer_touches_from_criteria(scan_text, repo_root)
    for path in inferred:
        if path not in touches_set:
            merged_touches.append(path)
            touches_set.add(path)

    # Augment touches with companion docs implied by close_criteria + title.
    companion_docs = companion_docs_from_criteria(scan_text, repo_root, cfg)
    for path in companion_docs:
        if path not in touches_set:
            merged_touches.append(path)
            touches_set.add(path)

    # Drop touches carried forward from an earlier draft that reference a
    # directory since renamed/moved/promoted by a merged ticket —
    # a stale entry only misleads the executor, which ends up implementing
    # under the real current path and getting blocked by the touches-guard
    # for a path analyze never declared.
    basename_corrections = correct_touches_by_basename(merged_touches, repo_root)
    if basename_corrections:
        # Two declared paths can share a basename and correct to the same
        # real file (or a correction can collide with an already-present
        # correct entry) -- dedupe post-correction, not just the pre-
        # correction list, or the ticket ends up with a repeated touch.
        merged_touches = list(
            dict.fromkeys(basename_corrections.get(p, p) for p in merged_touches)
        )
        model_visible_touches = list(
            dict.fromkeys(basename_corrections.get(p, p) for p in model_visible_touches)
        )
        for old, new in basename_corrections.items():
            print(
                f"WARNING: {tid}: corrected touches: '{old}' does not exist; "
                f"using '{new}' (unique basename match in the repo)",
                file=sys.stderr,
            )

    stale_touches = validate_touched_paths(existing_touches, repo_root)
    if stale_touches:
        merged_touches = [p for p in merged_touches if p not in stale_touches]
        model_visible_touches = [p for p in model_visible_touches if p not in stale_touches]
        print(
            f"WARNING: {tid}: dropping stale touches no longer present in repo "
            f"(directory renamed/moved/promoted?): {', '.join(stale_touches)}",
            file=sys.stderr,
        )

    control_plane_overlaps = find_control_plane_touch_overlaps(
        {**ticket, "id": tid, "touches": model_visible_touches}, tickets_dir, cfg, exclude_id=tid
    )
    requires_acceptance_matrix = is_high_reasoning_ticket(ticket)
    if requires_acceptance_matrix:
        matrix_errors = validate_acceptance_matrix(acceptance_matrix, required=True)
        if matrix_errors:
            print(f"ERROR: {'; '.join(matrix_errors)}; ticket left as draft", file=sys.stderr)
            sys.exit(1)
    elif acceptance_matrix is not None and validate_acceptance_matrix(acceptance_matrix):
        print(
            f"WARNING: {tid}: discarding malformed optional acceptance_matrix from analyzer output",
            file=sys.stderr,
        )
        acceptance_matrix = None
    if control_plane_overlaps:
        overlap_ids = {str(item["ticket_id"]) for item in control_plane_overlaps}
        overlap_detail = "; ".join(
            f"{item['ticket_id']} ({', '.join(cast(list, item['paths']))})" for item in control_plane_overlaps
        )
        review_errors = validate_overlap_review(overlap_review)
        if review_errors:
            print(
                f"ERROR: active control-plane overlaps require a plan: {'; '.join(review_errors)} "
                f"— overlapping ticket(s): {overlap_detail}; ticket left as draft",
                file=sys.stderr,
            )
            sys.exit(1)
        assert isinstance(overlap_review, dict)
        declared_ids = set(overlap_review["ticket_ids"])
        if not overlap_ids.issubset(declared_ids):
            missing = overlap_ids - declared_ids
            missing_detail = "; ".join(
                f"{item['ticket_id']} ({', '.join(cast(list, item['paths']))})"
                for item in control_plane_overlaps
                if str(item["ticket_id"]) in missing
            )
            print(
                "ERROR: active control-plane overlaps require a plan: "
                f"overlap_review.ticket_ids is missing: {missing_detail}; ticket left as draft",
                file=sys.stderr,
            )
            sys.exit(1)
        if overlap_review["mode"] == "dependencies" and not overlap_ids.issubset(set(depends_on)):
            print("ERROR: overlap_review dependencies must also appear in depends_on; ticket left as draft", file=sys.stderr)
            sys.exit(1)
        overlap_review = {
            "mode": overlap_review["mode"],
            "ticket_ids": sorted(overlap_ids),
            "paths": {str(item["ticket_id"]): item["paths"] for item in control_plane_overlaps},
        }
    elif overlap_review is not None:
        # An overlap plan is meaningful only when the trusted overlap scan
        # found a real active collision. Never persist model-supplied optional
        # planning metadata in the no-overlap case: it can be malformed,
        # self-referential, or misleading trusted implementation guidance.
        print(
            f"WARNING: {tid}: discarding overlap_review because no active control-plane overlap exists",
            file=sys.stderr,
        )
        overlap_review = None

    # After the drift guard has had the chance to restore an omitted original,
    # fail only if close_criteria is still empty (no prior criteria existed either).
    if not close_criteria:
        print("ERROR: model returned empty close_criteria; ticket left as draft", file=sys.stderr)
        sys.exit(1)

    # Apply to ticket
    ticket["touches"] = merged_touches
    ticket["close_criteria"] = close_criteria
    if acceptance_matrix is not None:
        ticket["acceptance_matrix"] = acceptance_matrix
    if overlap_review is not None:
        ticket["overlap_review"] = overlap_review
    elif not control_plane_overlaps:
        ticket.pop("overlap_review", None)
    file_skeletons = {
        touched: _build_file_skeleton(Path(touched), repo_root) for touched in merged_touches
    }
    file_skeletons_ref = write_file_skeletons_sidecar(ticket, repo_root, file_skeletons)
    if depends_on:
        ticket["depends_on"] = depends_on
    if change_notes:
        ticket["change_notes"] = change_notes
    if analyze_session_id and analyze_executor in _SESSION_EXECUTORS:
        ticket["analyze_session_id"] = analyze_session_id
        ticket["analyze_session_executor"] = analyze_executor
        if analyze_model:
            ticket["analyze_session_model"] = analyze_model
        else:
            ticket.pop("analyze_session_model", None)
    else:
        ticket.pop("analyze_session_id", None)
        ticket.pop("analyze_session_executor", None)
        ticket.pop("analyze_session_model", None)
    audit = audit_acceptance_contract(
        ticket,
        repo_root,
        close_criteria=close_criteria,
        change_notes=change_notes,
    )
    ticket["acceptance_contract_audit"] = audit.as_metadata()
    try:
        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if head_sha:
            ticket["analyzed_at_sha"] = head_sha
    except (subprocess.CalledProcessError, OSError):
        pass
    if not keep_draft:
        ticket["status"] = "open"

    # Validate before writing
    pub = {k: v for k, v in ticket.items() if not k.startswith("_")}
    errors = validate_ticket(pub, cfg)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        print("Ticket left as draft.", file=sys.stderr)
        sys.exit(1)

    write_ticket(ticket)

    if cfg.get("commit_status_changes", True):
        commit_msg = (
            f"chore: {tid} analyzed (touches populated)"
            if keep_draft
            else f"chore: {tid} analyzed — draft → open"
        )
        context_path = repo_root / file_skeletons_ref
        subprocess.run(
            ["git", "add", "-f", str(ticket["_path"])],
            cwd=repo_root,
            check=False,
            capture_output=True,
        )
        subprocess.run(
            ["git", "add", "-f", str(context_path)],
            cwd=repo_root,
            check=False,
            capture_output=True,
        )
        commit_result = subprocess.run(
            ["git", "commit", "-s", "--only", str(ticket["_path"]), str(context_path), "-m", commit_msg],
            cwd=repo_root,
            check=False,
            capture_output=True,
        )
        if commit_result.returncode != 0:
            # Ticket path is force-added above, but if primary commit fails for
            # any other reason (e.g. hook rejection), fall back to committing
            # just the sidecar to prevent it from being left staged.
            subprocess.run(
                ["git", "commit", "-s", "--only", str(context_path), "-m", commit_msg],
                cwd=repo_root,
                check=False,
                capture_output=True,
            )

    if keep_draft:
        print(f"{tid}: touches populated (status: draft — run `lanegate analyze {tid}` to open)")
    else:
        print(f"{tid}: draft → open")
    print(f"  touches: {', '.join(merged_touches)}")
    print(f"  close_criteria: {close_criteria}")
    if not audit.ok:
        print("  acceptance_contract_audit: findings recorded")
    visibility.emit("analysis_complete", f"Analysis complete for {tid}")


def cmd_analyze(
    ticket_id: str,
    cfg: dict,
    repo_root: Path,
    *,
    model_fn=None,
    keep_draft: bool = False,
    pool_name: str | None = None,
) -> None:
    """Analyze a ticket while publishing bounded standalone-analysis progress."""
    visibility = _AnalysisVisibility(repo_root, canonical_id(ticket_id))
    old_sigterm = None
    can_handle_signals = threading.current_thread() is threading.main_thread()

    if can_handle_signals:
        old_sigterm = signal.getsignal(signal.SIGTERM)

        def _interrupt_on_sigterm(_signum, _frame) -> None:
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, _interrupt_on_sigterm)

    try:
        _cmd_analyze_core(
            ticket_id,
            cfg,
            repo_root,
            model_fn=model_fn,
            keep_draft=keep_draft,
            visibility=visibility,
            pool_name=pool_name,
        )
    except KeyboardInterrupt:
        visibility.emit("analysis_failed", "ERROR: analysis interrupted", error=True)
        raise SystemExit(130) from None
    except SystemExit as exc:
        if exc.code:
            visibility.emit("analysis_failed", f"ERROR: analysis failed (exit {exc.code})", error=True)
        raise
    except Exception as exc:
        visibility.emit("analysis_failed", f"ERROR: analysis failed: {exc}", error=True)
        raise SystemExit(1) from exc
    finally:
        if can_handle_signals and old_sigterm is not None:
            signal.signal(signal.SIGTERM, old_sigterm)
        visibility.cleanup()
