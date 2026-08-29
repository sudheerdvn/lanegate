"""Static symbol indexing, search, and touch inference for analysis."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast


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
