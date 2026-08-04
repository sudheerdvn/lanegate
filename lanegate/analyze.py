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
import json
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from lanegate import APP_NAME
from lanegate.config import load_config, resolve_trunk_branch
from lanegate.ticket import (
    canonical_id,
    load_all_tickets,
    load_file_skeletons,
    validate_ticket,
    write_file_skeletons_sidecar,
    write_ticket,
)

_CLAUDE_EXECUTORS = frozenset({"claude", "claude-process", "claude-subagent"})
_SESSION_EXECUTORS = frozenset({"claude", "claude-process", "claude-subagent", "agy", "codex"})
_CLAUDE_MODEL_PREFIXES = ("claude-",)
_ACTIVE_ANALYSIS_FILE = "analyze-active.json"
_MAX_LOGGED_EXECUTOR_OUTPUT = 8 * 1024

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
}

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
        # Types / classes
        "class_declaration",
        "interface_declaration",
        "type_declaration",
        "struct_type",  # Go: appears inside type_spec
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
    }
)


def _ts_load_language(ext: str) -> _TSLanguage | None:
    """Load and cache the tree-sitter Language for the given file extension.

    Returns None if the grammar package is not installed.
    """
    if ext in _TS_LANG_CACHE:
        return _TS_LANG_CACHE[ext]

    mod_name = _TS_LANGUAGE_MAP.get(ext)
    if mod_name is None:
        _TS_LANG_CACHE[ext] = None
        return None

    try:
        import importlib

        # mod_name only ever comes from _TS_LANGUAGE_MAP's fixed 11 entries
        # above, never from ticket/file content or other untrusted input.
        mod = importlib.import_module(mod_name)  # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
        # tree-sitter-typescript exposes language_typescript() / language_tsx()
        # rather than a single language() function.
        if ext in (".ts",):
            lang_fn = getattr(mod, "language_typescript", None) or getattr(mod, "language", None)
        elif ext in (".tsx",):
            lang_fn = getattr(mod, "language_tsx", None) or getattr(mod, "language", None)
        else:
            lang_fn = getattr(mod, "language", None)

        if lang_fn is None:
            _TS_LANG_CACHE[ext] = None
            return None

        lang = _TSLanguage(lang_fn())
        _TS_LANG_CACHE[ext] = lang
        return lang
    except Exception:  # ImportError, AttributeError, or grammar init errors
        _TS_LANG_CACHE[ext] = None
        return None


def _ts_extract_symbols(node: object) -> list[str]:
    """Walk a tree-sitter Node and return all symbol names found.

    Looks for function/class/method declaration nodes and extracts their name
    child. Works across Go, JS, TS, Rust, Java, Ruby, C, C++ grammars.
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


# ---------------------------------------------------------------------------
# Model seam — replace in tests via monkeypatch or dependency injection
# ---------------------------------------------------------------------------


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

    def __init__(self, repo_root: Path) -> None:
        from lanegate.logs import analyze_log_path, write_analysis_event

        self.repo_root = repo_root
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
    """
    from lanegate.executor import (
        _CLAUDE_SUBPROCESS_TYPES,
        build_executor_cmd,
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

    use_stdin = resolved_executor_type in (_CLAUDE_SUBPROCESS_TYPES | {"codex", "ollama"})
    cmd = build_executor_cmd(executor, prompt, command_cfg, model=model, use_stdin=use_stdin)

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=executor_env,
        **({"input": prompt} if use_stdin else {}),
    )
    if result.returncode != 0:
        cmd_label = " ".join(cmd[:2]) if len(cmd) > 1 and cmd[1] == "exec" else cmd[0]
        details = _summarize_executor_output(result.stderr or result.stdout)
        suffix = f": {details}" if details else ""
        raise RuntimeError(f"{cmd_label} failed (exit {result.returncode}){suffix}")

    raw = result.stdout.strip()
    session_id: str | None = None
    parsed = parse_structured_result(resolved_executor_type, raw)
    if parsed is not None:
        session_id = parsed.get("session_id") or None
        raw = parsed.get("result_text", raw)

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


def _build_file_skeleton(path: Path, repo_root: Path) -> str:
    """Return a compact text block for one file: a header line (name + line
    count) plus one 'line N: signature' entry per top-level/class-level def
    for Python files. Non-Python files (or unparseable ones) get just the
    header. Computed entirely from stdlib ast — never LLM-generated."""
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
    if abs_path.suffix != ".py":
        return header

    idx = _index_py_file(abs_path)
    if idx is None or not idx.def_infos:
        return header

    body = "\n".join(f"  line {info.line:>3}: {info.signature}" for info in idx.def_infos)
    return f"{header}\n{body}"


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
    if not _HAS_TREE_SITTER:
        return None

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
                text=True,
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
        text=True,
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
# repo-relative path they refer to. Only known project docs — a keyword
# match alone must not invent a path that doesn't already exist.
_DOC_KEYWORD_MAP: dict[str, str] = {
    "readme": "README.md",
    "architecture": "docs/ARCHITECTURE.md",
    "config-reference": "docs/config-reference.md",
    "config reference": "docs/config-reference.md",
    "security-model": "docs/security-model.md",
    "security model": "docs/security-model.md",
}


def companion_docs_from_criteria(text: str, repo_root: Path) -> list[str]:
    """Return companion documentation paths implied by *text* (TICK-253).

    Close criteria and background prose often imply a doc update ("update
    README and ARCHITECTURE.md to describe the new behavior") without the
    model reliably carrying that into ``touches``. Scans for explicit
    ``*.md`` path mentions and known doc keywords (README, ARCHITECTURE,
    config-reference, security-model), then keeps only paths that actually
    exist in *repo_root* — prose mentioning documentation in the abstract
    must not inject a nonexistent path.
    """
    found: set[str] = set()

    for m in re.finditer(r"\b(?:[\w-]+/)*[\w-]+\.md\b", text, re.I):
        found.add(m.group(0))

    lowered = text.lower()
    for keyword, path in _DOC_KEYWORD_MAP.items():
        if keyword in lowered:
            found.add(path)

    return sorted(p for p in found if (repo_root / p).is_file())


def validate_touched_paths(paths: list[str], repo_root: Path) -> list[str]:
    """Return entries in *paths* that reference a directory no longer present (TICK-269).

    A touches entry carried forward from an earlier draft/analyze pass can
    reference a directory that a since-merged ticket has renamed, moved, or
    promoted (e.g. TICK-157 promoted ``tui_spike/`` to ``tui/internal/``).
    Carrying such a stale entry forward is worse than useless: the executor
    ends up implementing under the real current path, which was never
    declared, and the touches-guard blocks legitimate work after the fact.

    Only directory-style references (trailing ``/``) are checked — a plain
    file path may legitimately not exist yet (a new file the ticket itself
    will create).
    """
    return [p for p in paths if p.endswith("/") and not (repo_root / p).is_dir()]


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
# reason, hibernation notes, auto-fix attempt logs, review findings) rather than
# authored requirements. Left in, these feed the audit's own prior output back into
# itself on the next attempt — a ticket flagged for omitting an item quotes that
# same finding in its "Needs Review Reason" section, which then gets re-scanned as
# a fresh unmet requirement, compounding on every reopen/re-audit cycle.
_OPERATIONAL_SECTION_RE = re.compile(
    r"\n##\s*(Needs Review Reason|Hibernation Reason|Auto-Fix Attempt \d+|Review Findings)"
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
    text = "\n".join(
        [
            ticket.get("title", "") or "",
            _strip_operational_sections(ticket.get("_body", "") or ""),
            ticket.get("close_criteria", "") or "",
            _flatten_change_notes(ticket.get("change_notes")),
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

    # Deliberately does not cross-reference other tickets found by ID mention
    # anywhere in the text (e.g. background prose like "TICK-029 adds X"):
    # a ticket incidentally naming a prior/dependency ticket for context does
    # not mean it must independently restate that other ticket's own close
    # criteria. Doing so previously caused false-positive audit failures on
    # docs-only and narrowly-scoped tickets that merely referenced earlier
    # work. Linked *docs* (above) remain in scope since a ticket's own body
    # explicitly pointing at a design/contract doc is a real signal.

    return refs


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
    effective_notes = change_notes if change_notes is not None else ticket.get("change_notes") or {}

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

    return AcceptanceContractAudit(
        ok=not findings,
        findings=findings,
        omitted_items=[item.label for item in missing],
        sources=sources,
        checked_items=[item.label for item in items],
    )


# ---------------------------------------------------------------------------
# Per-criterion verification (TICK-283)
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
        ["git", "merge-base", trunk_branch, "HEAD"], cwd=repo_root, capture_output=True, text=True
    )
    if base.returncode != 0:
        return ""
    merge_base = base.stdout.strip()
    diff = subprocess.run(
        ["git", "diff", merge_base, "HEAD"], cwd=repo_root, capture_output=True, text=True
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


def _build_prompt(
    ticket: dict, repo_root: Path, cfg: dict | None = None, *, _components: list | None = None
) -> str:
    from lanegate.prompts import (
        component_for,
        get_bounded_architecture_excerpt,
        load_project_guidance,
        load_prompt_template,
        render_prompt,
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
    # computing) -- the AST/tree-sitter symbol-hit files (falling back to
    # importers) stand in as the "direct import/module context" relevance
    # signal for bounding project guidance and the architecture doc (TICK-306).
    relevant_paths = list(ctx.symbol_hits) + list(ctx.importers)

    project_guidance = load_project_guidance(
        repo_root, cfg, step="analyze", relevant_paths=relevant_paths
    )
    if project_guidance:
        sections.append(project_guidance)
    if _components is not None:
        sections_component_reason = "matched-and-bounded" if project_guidance else "no-matching-files"
        _components.append(component_for(
            "project-guidance", "project_guidance.files", "analyze", project_guidance,
            reason=sections_component_reason,
        ))

    arch_excerpt, arch_component = get_bounded_architecture_excerpt(
        repo_root, relevant_paths, cfg=cfg, step="analyze"
    )
    if arch_excerpt:
        sections.append(arch_excerpt)
    if _components is not None:
        _components.append(arch_component)

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
    return render_prompt(
        template,
        context_sections=context_sections,
        repo_structure=repo_structure_text,
        symbol_hits=symbol_hits_text,
        importers=importers_text,
        ripgrep_hits=ripgrep_text,
        ticket_id=ticket["id"],
        title=ticket.get("title", ""),
        intent=intent,
    )


def describe_analyze_payload(ticket: dict, repo_root: Path, cfg: dict | None = None) -> list[dict]:
    """Return a machine-readable breakdown of every component in the analyze
    prompt payload for *ticket* -- byte/token estimate, source, pipeline step,
    and whether it's always injected or selected because of the ticket.

    Component metadata only; never includes the ticket's actual title/body/
    intent text, so this is safe to log or display by default (TICK-306
    payload audit).
    """
    components: list = []
    _build_prompt(ticket, repo_root, cfg, _components=components)
    return [c.as_dict() for c in components]


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_response(text: str) -> dict:
    """Extract the JSON object from the model response."""
    # Strip markdown code fences if present
    text = re.sub(r"```(?:json)?\s*", "", text).strip()
    text = text.rstrip("`").strip()
    # Find first { ... } block
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"No JSON object found in model response:\n{text[:400]}")
    # strict=False tolerates literal control characters (e.g. a raw newline)
    # inside a string value -- smaller/local models routinely hard-wrap long
    # string values instead of escaping the break, which is otherwise a hard
    # parse failure despite the JSON being structurally well-formed.
    return json.loads(m.group(0), strict=False)


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
) -> None:
    """Analyze a draft ticket: populate touches + close_criteria.

    Args:
        model_fn: optional override for the model seam (used in tests).
            When provided, called as ``model_fn(prompt)`` — the resolved model
            string is NOT passed (tests supply their own stub logic).
        keep_draft: when True, leave status as draft after populating touches
            (used by `lanegate create` so the user can review before the ticket
            enters the work queue).
    """
    from lanegate.config import resolve_model as _resolve_model

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

    def resolve_analyze_driver(excluded: set[str] | None = None) -> tuple[str, dict, str, str | None]:
        driver_name = resolve_pool_executor(
            "analyze",
            ticket,
            cfg,
            repo_root,
            excluded=excluded,
            healthy_only=bool(excluded),
        )
        if driver_name is None:
            raise RuntimeError("no healthy pool sibling is available for analyze")
        driver_cfg = _expand_driver(driver_name, cfg)
        executor = driver_cfg.get("type", driver_name)
        effective_cfg = dict(cfg, executor=executor) if executor != cfg.get("executor") else cfg
        model = driver_cfg.get("model") or _resolve_model(effective_cfg, "analyze")
        return driver_name, driver_cfg, executor, model

    analyze_driver_name, analyze_driver_cfg, analyze_executor, analyze_model = resolve_analyze_driver()
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
        max_retries = int(cfg.get("max_sibling_retries", 1))
        excluded: set[str] = set()
        attempts = 0
        while True:
            try:
                raw, analyze_session_id = call_model(prompt)
                break
            except RuntimeError as exc:
                if (
                    model_fn is not None
                    or attempts >= max_retries
                    or not _is_rate_limit(1, repo_root, captured_stderr=str(exc))
                ):
                    raise
                reason = _rate_limit_reason(1, repo_root, captured_stderr=str(exc))
                _write_executor_cooldown(repo_root, analyze_driver_name, reason, retry_after=reason)
                excluded.add(analyze_driver_name)
                sibling_name, sibling_cfg, sibling_executor, sibling_model = resolve_analyze_driver(
                    excluded
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

    touches = result.get("touches")
    close_criteria = result.get("close_criteria", "").strip()
    depends_on = result.get("depends_on") or []
    change_notes = result.get("change_notes") or {}
    model = result.get("model")

    if not touches or not isinstance(touches, list):
        print(
            "ERROR: model returned empty or non-list touches; ticket left as draft", file=sys.stderr
        )
        sys.exit(1)
    if not close_criteria:
        print("ERROR: model returned empty close_criteria; ticket left as draft", file=sys.stderr)
        sys.exit(1)

    existing_touches = ticket.get("touches") or []
    merged_touches: list[str] = []
    touches_set: set[str] = set()
    for path in [*existing_touches, *touches]:
        if path not in touches_set:
            merged_touches.append(path)
            touches_set.add(path)

    # Augment touches with files statically implied by close_criteria + title.
    # Do NOT include _body: background prose ("lanegate board is broken") would
    # inject false file references for things the fix never touches.
    scan_text = ticket.get("title", "") + " " + close_criteria
    inferred = infer_touches_from_criteria(scan_text, repo_root)
    for path in inferred:
        if path not in touches_set:
            merged_touches.append(path)
            touches_set.add(path)

    # Augment touches with companion docs implied by close_criteria + title (TICK-253).
    companion_docs = companion_docs_from_criteria(scan_text, repo_root)
    for path in companion_docs:
        if path not in touches_set:
            merged_touches.append(path)
            touches_set.add(path)

    # Drop touches carried forward from an earlier draft that reference a
    # directory since renamed/moved/promoted by a merged ticket (TICK-269) —
    # a stale entry only misleads the executor, which ends up implementing
    # under the real current path and getting blocked by the touches-guard
    # for a path analyze never declared.
    stale_touches = validate_touched_paths(existing_touches, repo_root)
    if stale_touches:
        merged_touches = [p for p in merged_touches if p not in stale_touches]
        print(
            f"WARNING: {tid}: dropping stale touches no longer present in repo "
            f"(directory renamed/moved/promoted?): {', '.join(stale_touches)}",
            file=sys.stderr,
        )

    # Apply to ticket
    ticket["touches"] = merged_touches
    ticket["close_criteria"] = close_criteria
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
        result = subprocess.run(
            ["git", "commit", "--only", str(ticket["_path"]), str(context_path), "-m", commit_msg],
            cwd=repo_root,
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            # Ticket path is force-added above, but if primary commit fails for
            # any other reason (e.g. hook rejection), fall back to committing
            # just the sidecar to prevent it from being left staged.
            subprocess.run(
                ["git", "commit", "--only", str(context_path), "-m", commit_msg],
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
) -> None:
    """Analyze a ticket while publishing bounded standalone-analysis progress."""
    visibility = _AnalysisVisibility(repo_root)
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
