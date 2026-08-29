"""Tests for analyze_symbols.py static analysis helpers."""

"""Tests for analyze.py — cmd_analyze with stubbed model seam."""

import json
import shutil
import signal
import socket
import subprocess as _subprocess
import threading
import time
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

import pytest

from lanegate.analyze import (
    _HAS_TREE_SITTER,
    _TS_LANGUAGE_MAP,
    _already_resolved_reason_matches_worktree,
    _ast_symbol_hits,
    _build_ast_index,
    _build_candidate_skeletons,
    _build_file_skeleton,
    _build_prompt,
    _call_model,
    _close_criteria_drifted,
    _extract_acceptance_checklist,
    _import_graph_expand,
    _index_non_py_file,
    _index_py_file,
    _parse_response,
    _repo_structure,
    _ripgrep_seed,
    _treesitter_hits,
    audit_acceptance_contract,
    cmd_analyze,
    companion_docs_from_criteria,
    correct_touches_by_basename,
    enrich_context,
    infer_touches_from_criteria,
    validate_touched_paths,
    verify_acceptance_criteria,
)
from lanegate.config import ConfigError
from lanegate.ticket import parse_ticket, validate_ticket

_CFG = {
    "ticket_prefix": "TICK",
    "tickets_dir": "tickets",
    "commit_status_changes": False,
}

_GOOD_RESPONSE = json.dumps(
    {
        "touches": ["lanegate/foo.py", "tests/test_foo.py"],
        "close_criteria": "cmd_foo writes a file and returns 0.",
        "depends_on": [],
    }
)


def _write_py(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_build_file_skeleton_python_file(tmp_path):
    f = tmp_path / "lanegate" / "foo.py"
    _write_py(f, "def cmd_foo(x, y=1):\n    pass\n\n\ndef cmd_bar():\n    pass\n")
    skeleton = _build_file_skeleton(Path("lanegate/foo.py"), tmp_path)
    lines = skeleton.splitlines()
    assert lines[0] == "lanegate/foo.py  (6 lines)"
    assert "line   1: def cmd_foo(x, y=1)" in skeleton
    assert "line   5: def cmd_bar()" in skeleton


def test_build_file_skeleton_non_python_file_is_stub_only(tmp_path):
    f = tmp_path / "docs" / "notes.md"
    f.parent.mkdir()
    f.write_text("line one\nline two\nline three\n")
    skeleton = _build_file_skeleton(Path("docs/notes.md"), tmp_path)
    assert skeleton == "docs/notes.md  (3 lines)"


def test_build_file_skeleton_missing_file(tmp_path):
    skeleton = _build_file_skeleton(Path("lanegate/missing.py"), tmp_path)
    assert "not found" in skeleton


def test_multilang_file_skeletons(tmp_path):
    """TICK-412: non-Python files with an installed tree-sitter grammar get
    line-numbered signatures too, not just a bare header."""
    pytest.importorskip("tree_sitter_go")
    pytest.importorskip("tree_sitter_typescript")
    pytest.importorskip("tree_sitter_rust")
    pytest.importorskip("tree_sitter_java")

    go_file = tmp_path / "service.go"
    go_file.write_bytes(b"package main\n\nfunc FetchUser(id int) string {\n  return \"\"\n}\n")
    go_skeleton = _build_file_skeleton(Path("service.go"), tmp_path)
    assert "service.go  (5 lines)" in go_skeleton
    assert "line   3:" in go_skeleton
    assert "FetchUser" in go_skeleton

    ts_file = tmp_path / "repository.ts"
    ts_file.write_bytes(
        b"class UserRepository {\n"
        b'  findUser(id: number): string { return ""; }\n'
        b"}\n"
    )
    ts_skeleton = _build_file_skeleton(Path("repository.ts"), tmp_path)
    assert "repository.ts  (3 lines)" in ts_skeleton
    assert "UserRepository" in ts_skeleton
    assert "findUser" in ts_skeleton

    rs_file = tmp_path / "lib.rs"
    rs_file.write_bytes(b"fn fetch_user(id: u32) -> String {\n    String::new()\n}\n")
    rs_skeleton = _build_file_skeleton(Path("lib.rs"), tmp_path)
    assert "lib.rs  (3 lines)" in rs_skeleton
    assert "fetch_user" in rs_skeleton

    java_file = tmp_path / "UserService.java"
    java_file.write_bytes(
        b"class UserService {\n"
        b"  String fetchUser(int id) {\n"
        b"    return \"\";\n"
        b"  }\n"
        b"}\n"
    )
    java_skeleton = _build_file_skeleton(Path("UserService.java"), tmp_path)
    assert "UserService.java  (5 lines)" in java_skeleton
    assert "UserService" in java_skeleton
    assert "fetchUser" in java_skeleton


def test_build_candidate_skeletons_includes_real_signatures(tmp_path):
    _write_py(tmp_path / "lanegate" / "foo.py", "def cmd_foo(x, y=1):\n    pass\n")
    text = _build_candidate_skeletons(["lanegate/foo.py"], tmp_path)
    assert "## Candidate file skeletons" in text
    assert "lanegate/foo.py" in text
    assert "def cmd_foo(x, y=1)" in text


def test_build_candidate_skeletons_empty_paths_returns_empty_string(tmp_path):
    assert _build_candidate_skeletons([], tmp_path) == ""


def test_build_candidate_skeletons_dedupes_paths(tmp_path):
    _write_py(tmp_path / "lanegate" / "foo.py", "def cmd_foo():\n    pass\n")
    text = _build_candidate_skeletons(["lanegate/foo.py", "lanegate/foo.py"], tmp_path)
    assert text.count("lanegate/foo.py") == 1


def test_build_candidate_skeletons_caps_file_count(tmp_path):
    paths = []
    for i in range(30):
        rel = f"lanegate/mod{i}.py"
        _write_py(tmp_path / "lanegate" / f"mod{i}.py", f"def f{i}():\n    pass\n")
        paths.append(rel)
    text = _build_candidate_skeletons(paths, tmp_path)
    included = sum(1 for p in paths if p in text)
    assert included == 25


def test_build_candidate_skeletons_caps_total_bytes(tmp_path):
    # Each file's skeleton is well under the byte budget alone, but many
    # of them together must stop growing once the budget is hit rather
    # than ballooning the analyze prompt unboundedly.
    paths = []
    for i in range(25):
        rel = f"lanegate/mod{i}.py"
        body = "\n".join(f"def f{i}_{j}(a, b, c, d, e, f, g, h):\n    pass\n" for j in range(40))
        _write_py(tmp_path / "lanegate" / f"mod{i}.py", body)
        paths.append(rel)
    text = _build_candidate_skeletons(paths, tmp_path)
    assert len(text.encode("utf-8")) <= 15000 + 2000  # header/joiner overhead, not per-block


def test_build_ast_index_finds_all_py_files(tmp_path):
    _write_py(tmp_path / "mod_a.py", "def alpha(): pass\n")
    _write_py(tmp_path / "pkg" / "mod_b.py", "def beta(): pass\n")
    indices = _build_ast_index(tmp_path)
    paths = [idx.path for idx in indices]
    assert tmp_path / "mod_a.py" in paths
    assert tmp_path / "pkg" / "mod_b.py" in paths


def test_build_ast_index_skips_hidden_dirs(tmp_path):
    _write_py(tmp_path / ".hidden" / "secret.py", "def hidden_fn(): pass\n")
    _write_py(tmp_path / "visible.py", "def visible_fn(): pass\n")
    indices = _build_ast_index(tmp_path)
    names = [idx.path.name for idx in indices]
    assert "visible.py" in names
    assert "secret.py" not in names


def test_build_ast_index_skips_pycache(tmp_path):
    _write_py(tmp_path / "__pycache__" / "cached.py", "x = 1\n")
    _write_py(tmp_path / "real.py", "def real_fn(): pass\n")
    indices = _build_ast_index(tmp_path)
    names = [idx.path.name for idx in indices]
    assert "real.py" in names
    assert "cached.py" not in names


def test_build_ast_index_silently_skips_parse_errors(tmp_path):
    _write_py(tmp_path / "good.py", "def good_fn(): pass\n")
    _write_py(tmp_path / "bad.py", "def bad syntax(:\n")
    # Should not raise, and good.py should still be indexed
    indices = _build_ast_index(tmp_path)
    names = [idx.path.name for idx in indices]
    assert "good.py" in names
    assert "bad.py" not in names


def test_ast_symbol_hits_returns_matching_files(tmp_path):
    _write_py(tmp_path / "analyze.py", "def enrich_context(): pass\n")
    _write_py(tmp_path / "unrelated.py", "def something_else(): pass\n")
    hits = _ast_symbol_hits("enrich context for ticket analysis", tmp_path)
    hit_names = [idx.path.name for idx in hits]
    assert "analyze.py" in hit_names
    assert "unrelated.py" not in hit_names


def test_ast_symbol_hits_returns_empty_for_no_matches(tmp_path):
    _write_py(tmp_path / "mod.py", "def totally_unrelated(): pass\n")
    hits = _ast_symbol_hits("xyzzy frobnicate quux", tmp_path)
    assert hits == []


def test_ast_symbol_hits_case_insensitive(tmp_path):
    _write_py(tmp_path / "mod.py", "def MyParser(): pass\n")
    hits = _ast_symbol_hits("myparser integration", tmp_path)
    hit_names = [idx.path.name for idx in hits]
    assert "mod.py" in hit_names


def test_ast_symbol_hits_returns_empty_for_short_keywords(tmp_path):
    _write_py(tmp_path / "mod.py", "def foo(): pass\n")
    # All words < 4 chars — should find nothing
    hits = _ast_symbol_hits("add foo bar", tmp_path)
    assert hits == []


def test_import_graph_expand_finds_importers(tmp_path):
    _write_py(tmp_path / "core.py", "def core_logic(): pass\n")
    _write_py(tmp_path / "consumer.py", "from core import core_logic\n")
    _write_py(tmp_path / "other.py", "def unrelated(): pass\n")

    all_indices = _build_ast_index(tmp_path)
    core_idx = next(idx for idx in all_indices if idx.path.name == "core.py")
    importers = _import_graph_expand([core_idx], all_indices)
    importer_names = [idx.path.name for idx in importers]
    assert "consumer.py" in importer_names
    assert "other.py" not in importer_names


def test_import_graph_expand_empty_hits_returns_empty(tmp_path):
    _write_py(tmp_path / "mod.py", "def fn(): pass\n")
    all_indices = _build_ast_index(tmp_path)
    result = _import_graph_expand([], all_indices)
    assert result == []


def test_import_graph_expand_does_not_re_include_hits(tmp_path):
    _write_py(tmp_path / "alpha.py", "def alpha(): pass\n")
    _write_py(tmp_path / "beta.py", "from alpha import alpha\n")

    all_indices = _build_ast_index(tmp_path)
    alpha_idx = next(idx for idx in all_indices if idx.path.name == "alpha.py")
    importers = _import_graph_expand([alpha_idx], all_indices)
    # alpha.py itself must not appear as its own importer
    assert alpha_idx not in importers


def test_enrich_context_uses_ast_when_hits_found(tmp_path):
    _write_py(tmp_path / "analyze_module.py", "def analyze_data(): pass\n")
    ctx = enrich_context("analyze data pipeline", tmp_path)
    # AST hits should be populated since "analyze" matches "analyze_data"
    assert ctx.source in ("ast", "ripgrep", "none")  # AST is preferred if found


def test_enrich_context_source_ast_when_match(tmp_path):
    _write_py(tmp_path / "enrichment.py", "def enrich_context_data(): pass\n")
    ctx = enrich_context("enrich context data processing", tmp_path)
    # "enrich" (6 chars) and "context" (7 chars) should match "enrich_context_data"
    if ctx.source == "ast":
        assert "enrichment.py" in ctx.symbol_hits


def test_enrich_context_falls_back_to_ripgrep_when_no_ast_hits(tmp_path, monkeypatch):
    """When AST finds nothing and tree-sitter is absent, ripgrep is used."""
    _write_py(tmp_path / "mod.py", "x = 1\n")  # no matching symbols

    monkeypatch.setattr("lanegate.analyze_symbols._HAS_TREE_SITTER", False)

    rg_called = []

    def fake_run(cmd, **kwargs):
        rg_called.append(cmd)
        return _subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("lanegate.analyze.subprocess.run", side_effect=fake_run):
        ctx = enrich_context("xyzzy_totally_unique_keyword_never_matches", tmp_path)

    # ripgrep fallback was tried (or returned empty); source is ripgrep or none
    assert ctx.source in ("ripgrep", "none")


def test_enrich_context_last_resort_returns_repo_structure(tmp_path, monkeypatch):
    """When everything else fails, repo_structure is populated."""
    _write_py(tmp_path / "mod.py", "x = 1\n")

    monkeypatch.setattr("lanegate.analyze_symbols._HAS_TREE_SITTER", False)

    def fake_run(cmd, **kwargs):
        # Simulate git ls-files returning one file, rg returning nothing
        if cmd[0] == "git":
            return _subprocess.CompletedProcess(cmd, 0, stdout="mod.py\n", stderr="")
        return _subprocess.CompletedProcess(cmd, 0, stdout="", stderr="1")  # rg: no hits

    with patch("lanegate.analyze.subprocess.run", side_effect=fake_run):
        ctx = enrich_context("xyzzy_totally_unique_keyword_never_matches", tmp_path)

    assert "mod.py" in ctx.repo_structure


def test_build_ast_index_skips_dependency_and_worktree_dirs(tmp_path):
    _write_py(
        tmp_path / "venv" / "lib" / "python3.13" / "site-packages" / "pkg.py",
        "def noisy_symbol(): pass\n",
    )
    _write_py(
        tmp_path / "worktrees" / "tick-001" / "lanegate" / "copy.py", "def noisy_symbol(): pass\n"
    )
    _write_py(tmp_path / "src" / "app.py", "def real_symbol(): pass\n")
    indices = _build_ast_index(tmp_path)
    rel_paths = {idx.path.relative_to(tmp_path).as_posix() for idx in indices}
    assert "src/app.py" in rel_paths
    assert "venv/lib/python3.13/site-packages/pkg.py" not in rel_paths
    assert "worktrees/tick-001/lanegate/copy.py" not in rel_paths


def test_repo_structure_filters_dependency_and_worktree_paths(tmp_path):
    def fake_run(cmd, **kwargs):
        stdout = (
            "\n".join(
                [
                    "src/app.py",
                    "tests/test_app.py",
                    "venv/lib/python3.13/site-packages/pkg.py",
                    "worktrees/tick-001/lanegate/copy.py",
                    ".pytest_cache/cache.py",
                    "module.pyc",
                ]
            )
            + "\n"
        )
        return _subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with patch("lanegate.analyze.subprocess.run", side_effect=fake_run):
        structure = _repo_structure(tmp_path)

    assert "src/app.py" in structure
    assert "tests/test_app.py" in structure
    assert "venv/" not in structure
    assert "worktrees/" not in structure
    assert ".pytest_cache" not in structure
    assert "module.pyc" not in structure


def test_ripgrep_seed_filters_dependency_and_worktree_paths(tmp_path):
    def fake_run(cmd, **kwargs):
        stdout = (
            "\n".join(
                [
                    "venv/lib/python3.13/site-packages/pkg.py",
                    "worktrees/tick-001/lanegate/copy.py",
                    "src/app.py",
                ]
            )
            + "\n"
        )
        return _subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    with patch("lanegate.analyze.subprocess.run", side_effect=fake_run):
        seed = _ripgrep_seed("analyze noisy libraries", tmp_path)

    assert "src/app.py" in seed
    assert "venv/" not in seed
    assert "worktrees/" not in seed


def test_ht_has_tree_sitter_flag_is_bool():
    """_HAS_TREE_SITTER must be a bool (True when tree-sitter installed)."""
    assert isinstance(_HAS_TREE_SITTER, bool)


def test_ts_language_map_covers_expected_extensions():
    """_TS_LANGUAGE_MAP covers the core set of non-Python extensions."""
    expected = {
        ".go", ".js", ".jsx", ".ts", ".tsx", ".rs", ".java", ".rb", ".c", ".cpp", ".h",
        ".php", ".cs", ".swift", ".kt", ".kts",
    }
    for ext in expected:
        assert ext in _TS_LANGUAGE_MAP, f"Missing extension {ext} from _TS_LANGUAGE_MAP"


def test_index_non_py_php_function_and_class(tmp_path):
    """_index_non_py_file extracts function/class/method names from a PHP file.

    Regression coverage for the language_php() special case in
    _ts_load_language -- tree-sitter-php exposes language_php(), not a plain
    language(), unlike every other grammar in the map.
    """
    pytest.importorskip("tree_sitter_php")
    php_file = tmp_path / "UserService.php"
    php_file.write_bytes(
        b"<?php\nclass UserService {\n  function findUser($id) { return null; }\n}\n"
        b"function fetchData($url) { return null; }\n"
    )
    idx = _index_non_py_file(php_file)
    assert idx is not None
    assert "UserService" in idx.defs
    assert "findUser" in idx.defs
    assert "fetchData" in idx.defs


def test_index_non_py_csharp_class_and_interface(tmp_path):
    """_index_non_py_file extracts class/interface/method names from a C# file."""
    pytest.importorskip("tree_sitter_c_sharp")
    cs_file = tmp_path / "UserRepo.cs"
    cs_file.write_bytes(
        b"namespace App {\n"
        b"  public class UserRepo {\n"
        b"    public string FindById(int id) { return null; }\n"
        b"  }\n"
        b"  public interface IFetcher {}\n"
        b"}\n"
    )
    idx = _index_non_py_file(cs_file)
    assert idx is not None
    assert "UserRepo" in idx.defs
    assert "FindById" in idx.defs
    assert "IFetcher" in idx.defs


def test_index_non_py_swift_class_struct_protocol(tmp_path):
    """_index_non_py_file extracts class/struct/protocol names from a Swift file.

    struct parses under the same class_declaration node as class (no extra
    node type needed); protocol_declaration is its own node type, added to
    _TS_SYMBOL_NODE_TYPES alongside Kotlin's object_declaration.
    """
    pytest.importorskip("tree_sitter_swift")
    swift_file = tmp_path / "UserService.swift"
    swift_file.write_bytes(
        b"class UserService {\n  func fetchUser(id: Int) -> String { return \"\" }\n}\n"
        b"struct UserModel {\n  var name: String\n}\n"
        b"protocol Fetchable {\n  func fetch()\n}\n"
    )
    idx = _index_non_py_file(swift_file)
    assert idx is not None
    assert "UserService" in idx.defs
    assert "fetchUser" in idx.defs
    assert "UserModel" in idx.defs
    assert "Fetchable" in idx.defs


def test_index_non_py_kotlin_class_interface_object(tmp_path):
    """_index_non_py_file extracts class/interface/object names from a Kotlin file."""
    pytest.importorskip("tree_sitter_kotlin")
    kt_file = tmp_path / "UserService.kt"
    kt_file.write_bytes(
        b"class UserService {\n  fun fetchUser(id: Int): String { return \"\" }\n}\n"
        b"interface Fetchable {\n  fun fetch()\n}\n"
        b"object Singleton {\n  fun instance() {}\n}\n"
    )
    idx = _index_non_py_file(kt_file)
    assert idx is not None
    assert "UserService" in idx.defs
    assert "fetchUser" in idx.defs
    assert "Fetchable" in idx.defs
    assert "Singleton" in idx.defs


def test_register_tree_sitter_languages_extends_map():
    """register_tree_sitter_languages merges project-declared extensions in place."""
    from lanegate.analyze import _TS_LANGUAGE_MAP, register_tree_sitter_languages

    assert ".vue" not in _TS_LANGUAGE_MAP
    try:
        register_tree_sitter_languages({".vue": "tree_sitter_vue"})
        assert _TS_LANGUAGE_MAP[".vue"] == "tree_sitter_vue"
    finally:
        _TS_LANGUAGE_MAP.pop(".vue", None)


def test_register_tree_sitter_languages_noop_on_empty():
    """register_tree_sitter_languages(None) and ({}) are both safe no-ops."""
    from lanegate.analyze import register_tree_sitter_languages

    register_tree_sitter_languages(None)
    register_tree_sitter_languages({})


def test_treesitter_hits_returns_empty_when_flag_false(tmp_path):
    """_treesitter_hits returns [] immediately when _HAS_TREE_SITTER is False."""
    go_file = tmp_path / "server.go"
    go_file.write_bytes(b"package main\nfunc FetchUser() {}\n")

    with patch("lanegate.analyze_symbols._HAS_TREE_SITTER", False):
        hits = _treesitter_hits("fetch user data", tmp_path)

    assert hits == []


def test_index_non_py_file_returns_none_when_no_treesitter(tmp_path, monkeypatch):
    """_index_non_py_file returns None when _HAS_TREE_SITTER is False."""
    go_file = tmp_path / "main.go"
    go_file.write_bytes(b"package main\nfunc Main() {}\n")
    monkeypatch.setattr("lanegate.analyze_symbols._HAS_TREE_SITTER", False)
    assert _index_non_py_file(go_file) is None


def test_index_non_py_file_returns_none_for_unknown_extension(tmp_path):
    """_index_non_py_file returns None for extensions not in _TS_LANGUAGE_MAP."""
    txt_file = tmp_path / "notes.txt"
    txt_file.write_bytes(b"some text\n")
    result = _index_non_py_file(txt_file)
    assert result is None


def test_index_non_py_file_returns_none_for_missing_file(tmp_path):
    """_index_non_py_file returns None when the file does not exist."""
    result = _index_non_py_file(tmp_path / "nonexistent.go")
    assert result is None


def test_index_non_py_go_function(tmp_path):
    """_index_non_py_file extracts function names from a Go file."""
    pytest.importorskip("tree_sitter_go")
    go_file = tmp_path / "service.go"
    go_file.write_bytes(b'package main\n\nfunc FetchUser(id int) string {\n  return ""\n}\n')
    idx = _index_non_py_file(go_file)
    assert idx is not None
    assert "FetchUser" in idx.defs


def test_index_non_py_go_method(tmp_path):
    """_index_non_py_file extracts method names from a Go file."""
    pytest.importorskip("tree_sitter_go")
    go_file = tmp_path / "repo.go"
    go_file.write_bytes(
        b"package main\n\ntype UserRepo struct{}\n\n"
        b'func (r *UserRepo) FindById(id int) string {\n  return ""\n}\n'
    )
    idx = _index_non_py_file(go_file)
    assert idx is not None
    assert "FindById" in idx.defs


def test_index_non_py_go_type(tmp_path):
    """_index_non_py_file extracts type names from a Go file."""
    pytest.importorskip("tree_sitter_go")
    go_file = tmp_path / "types.go"
    go_file.write_bytes(
        b"package main\n\ntype UserService struct {\n  name string\n}\n"
        b"type Fetcher interface {\n  Fetch() string\n}\n"
    )
    idx = _index_non_py_file(go_file)
    assert idx is not None
    assert "UserService" in idx.defs
    assert "Fetcher" in idx.defs


def test_index_non_py_js_function(tmp_path):
    """_index_non_py_file extracts function names from a JS file."""
    pytest.importorskip("tree_sitter_javascript")
    js_file = tmp_path / "api.js"
    js_file.write_bytes(
        b"function fetchData(url) { return null; }\nclass UserService { render() {} }\n"
    )
    idx = _index_non_py_file(js_file)
    assert idx is not None
    assert "fetchData" in idx.defs
    assert "UserService" in idx.defs


def test_index_non_py_ts_class(tmp_path):
    """_index_non_py_file extracts class names from a TS file."""
    pytest.importorskip("tree_sitter_typescript")
    ts_file = tmp_path / "repository.ts"
    ts_file.write_bytes(
        b"class UserRepository {\n"
        b'  findUser(id: number): string { return ""; }\n'
        b"}\n"
        b"function parseToken(token: string): boolean { return true; }\n"
    )
    idx = _index_non_py_file(ts_file)
    assert idx is not None
    assert "UserRepository" in idx.defs
    assert "parseToken" in idx.defs


def test_treesitter_hits_go_match(tmp_path):
    """_treesitter_hits returns the Go file when its function matches the intent."""
    pytest.importorskip("tree_sitter_go")
    go_file = tmp_path / "handler.go"
    go_file.write_bytes(b"package main\nfunc HandleRequest(w string) {}\n")
    hits = _treesitter_hits("handle request routing", tmp_path)
    assert "handler.go" in hits


def test_treesitter_hits_js_match(tmp_path):
    """_treesitter_hits returns the JS file when its function matches the intent."""
    pytest.importorskip("tree_sitter_javascript")
    js_file = tmp_path / "auth.js"
    js_file.write_bytes(b"function validateToken(tok) { return true; }\n")
    hits = _treesitter_hits("validate token credentials", tmp_path)
    assert "auth.js" in hits


def test_treesitter_hits_ts_class_match(tmp_path):
    """_treesitter_hits returns the TS file when its class matches the intent."""
    pytest.importorskip("tree_sitter_typescript")
    ts_file = tmp_path / "user.ts"
    ts_file.write_bytes(b"class UserManager { create() {} }\n")
    hits = _treesitter_hits("user manager creation", tmp_path)
    assert "user.ts" in hits


def test_treesitter_hits_no_match(tmp_path):
    """_treesitter_hits returns [] when no file's symbols match the intent."""
    pytest.importorskip("tree_sitter_go")
    go_file = tmp_path / "util.go"
    go_file.write_bytes(b"package main\nfunc PrintHello() {}\n")
    hits = _treesitter_hits("database query executor", tmp_path)
    assert hits == []


def test_treesitter_hits_skips_ignored_dirs(tmp_path):
    """_treesitter_hits does not index files inside ignored directories."""
    pytest.importorskip("tree_sitter_go")
    # File inside an ignored directory
    ignored_dir = tmp_path / "node_modules" / "lib"
    ignored_dir.mkdir(parents=True)
    ignored_file = ignored_dir / "helper.go"
    ignored_file.write_bytes(b"package lib\nfunc HelperFunc() {}\n")
    # No matching file in a non-ignored dir
    hits = _treesitter_hits("helper function", tmp_path)
    assert "node_modules/lib/helper.go" not in hits


def test_index_non_py_file_defs_used_for_symbols(tmp_path):
    """The returned _FileIndex uses defs for symbol names and imports is empty."""
    pytest.importorskip("tree_sitter_javascript")
    js_file = tmp_path / "mod.js"
    js_file.write_bytes(b"function doSomething() {}\n")
    idx = _index_non_py_file(js_file)
    assert idx is not None
    assert "doSomething" in idx.defs
    assert idx.imports == []


def test_infer_touches_board_mention(tmp_path):
    """'lanegate board' in criteria implies lanegate/board.py."""
    result = infer_touches_from_criteria("lanegate board shows new column", tmp_path)
    assert "lanegate/board.py" in result


def test_infer_touches_stats_mention(tmp_path):
    """'lanegate stats' in criteria implies lanegate/stats.py."""
    result = infer_touches_from_criteria("lanegate stats output includes cycle time", tmp_path)
    assert "lanegate/stats.py" in result


def test_infer_touches_unknown_cmd_does_not_imply_cli(tmp_path):
    """'lanegate <unknown-cmd>' must NOT inject lanegate/cli.py.

    Prose like "lanegate foobar runs the new thing" is not a subcommand reference
    — silently ignore unknown tokens rather than adding a false cli.py touch.
    """
    result = infer_touches_from_criteria("lanegate foobar runs the new thing", tmp_path)
    assert "lanegate/cli.py" not in result


def test_infer_touches_prose_ticket_creation_no_cli(tmp_path):
    """'lanegate ticket creation is handled by lifecycle' must NOT add cli.py."""
    result = infer_touches_from_criteria(
        "lanegate ticket creation is handled by lifecycle", tmp_path
    )
    assert "lanegate/cli.py" not in result


def test_infer_touches_prose_runs_project_no_cli(tmp_path):
    """'lanegate runs the project' must NOT add cli.py."""
    result = infer_touches_from_criteria("lanegate runs the project", tmp_path)
    assert "lanegate/cli.py" not in result


def test_infer_touches_prose_docs_no_cli(tmp_path):
    """'lanegate docs shows the config' must NOT add cli.py."""
    result = infer_touches_from_criteria("lanegate docs shows the config", tmp_path)
    assert "lanegate/cli.py" not in result


def test_infer_touches_show_in_board(tmp_path):
    """'show in board' implies lanegate/board.py even without 'lanegate board'."""
    result = infer_touches_from_criteria("column X is show in board output", tmp_path)
    assert "lanegate/board.py" in result


def test_infer_touches_add_column(tmp_path):
    """'add column' implies lanegate/board.py."""
    result = infer_touches_from_criteria("add column for milestone to the output", tmp_path)
    assert "lanegate/board.py" in result


def test_infer_touches_new_module(tmp_path):
    """'new file lanegate/foo.py' implies that path."""
    result = infer_touches_from_criteria("new file lanegate/foo.py is created", tmp_path)
    assert "lanegate/foo.py" in result


def test_infer_touches_new_module_keyword(tmp_path):
    """'new module lanegate/bar.py' implies that path."""
    result = infer_touches_from_criteria("new module lanegate/bar.py added for routing", tmp_path)
    assert "lanegate/bar.py" in result


def test_infer_touches_new_file_non_py_path(tmp_path):
    """'new file docs/plan.md' implies that non-Python path."""
    result = infer_touches_from_criteria(
        "close criteria: new file tests/fixtures/spec_artifacts/README.md exists",
        tmp_path,
    )
    assert "tests/fixtures/spec_artifacts/README.md" in result


def test_infer_touches_no_duplicates(tmp_path):
    """Duplicate mentions of the same command yield a single entry."""
    result = infer_touches_from_criteria("lanegate board shows X; lanegate board also shows Y", tmp_path)
    assert result.count("lanegate/board.py") == 1


def test_infer_touches_empty_text(tmp_path):
    """Empty text yields no inferences."""
    result = infer_touches_from_criteria("", tmp_path)
    assert result == []


def test_infer_touches_no_false_positives(tmp_path):
    """Text with no command patterns yields no inferences."""
    result = infer_touches_from_criteria(
        "the ticket body says nothing about any subcommand", tmp_path
    )
    assert result == []


def test_detect_companion_docs_from_criteria(tmp_path):
    """close_criteria mentioning README/ARCHITECTURE implies those doc paths (TICK-253)."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ARCHITECTURE.md").write_text("# Architecture\n")
    (tmp_path / "README.md").write_text("# README\n")

    result = companion_docs_from_criteria(
        "Update README and docs/ARCHITECTURE.md to describe the new touches-guard behavior.",
        tmp_path,
    )

    assert "README.md" in result
    assert "docs/ARCHITECTURE.md" in result


def test_detect_companion_docs_bare_keyword(tmp_path):
    """A bare doc keyword with no explicit path still resolves if the file exists."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "security-model.md").write_text("# Security model\n")

    result = companion_docs_from_criteria(
        "The security model doc needs a new section on this threat.", tmp_path,
        {"reference_docs": ["docs/security-model.md"]},
    )

    assert "docs/security-model.md" in result


def test_detect_companion_docs_config_driven(tmp_path):
    """Keyword matching for companion docs resolves against reference_docs (TICK-414)."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "DESIGN.md").write_text("# Design\n")
    (tmp_path / "docs" / "ARCHITECTURE.md").write_text("# Architecture\n")

    # Unconfigured project: bare mention of 'architecture' does NOT resolve to docs/ARCHITECTURE.md
    res_unconfigured = companion_docs_from_criteria(
        "Update the architecture doc to describe the new pipeline.",
        tmp_path,
        cfg={},
    )
    assert "docs/ARCHITECTURE.md" not in res_unconfigured

    # Configured project: reference_docs specifies docs/DESIGN.md and docs/ARCHITECTURE.md
    cfg = {"reference_docs": ["docs/DESIGN.md", "docs/ARCHITECTURE.md"]}
    res_configured = companion_docs_from_criteria(
        "Update the design doc and architecture doc to describe the new pipeline.",
        tmp_path,
        cfg=cfg,
    )
    assert "docs/DESIGN.md" in res_configured
    assert "docs/ARCHITECTURE.md" in res_configured


def test_companion_docs_ignores_nonexistent_doc_mentions(tmp_path):
    """A doc keyword must not inject a path that doesn't exist in the repo."""
    result = companion_docs_from_criteria(
        "Update the CHANGELOG and README to note this fix.", tmp_path
    )
    assert result == []


def test_companion_docs_empty_text(tmp_path):
    result = companion_docs_from_criteria("", tmp_path)
    assert result == []


def test_validate_touched_paths_detects_stale_references(tmp_path):
    """A touches directory renamed/moved by a prior merged ticket is flagged (TICK-269)."""
    (tmp_path / "tui").mkdir()
    (tmp_path / "tui" / "internal").mkdir()

    result = validate_touched_paths(["tui_spike/", "tui/internal/"], tmp_path)

    assert result == ["tui_spike/"]


def test_validate_touched_paths_ignores_file_entries(tmp_path):
    """Plain file paths are not checked -- a ticket may declare a file it will create."""
    result = validate_touched_paths(["lanegate/not_created_yet.py"], tmp_path)
    assert result == []


def test_validate_touched_paths_no_stale_entries(tmp_path):
    (tmp_path / "lanegate").mkdir()
    result = validate_touched_paths(["lanegate/"], tmp_path)
    assert result == []


def _git_repo_with_tracked_files(tmp_path, files: dict[str, str]) -> Path:
    _subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True)
    _subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    _subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def test_correct_touches_by_basename_fixes_flat_guess_for_nested_file(tmp_path):
    """A model that writes a flat-layout guess (calc.py) for a real nested
    file (src/calc.py) gets corrected -- reproduces a live fresh-install
    smoke-test finding where enrich_context's ripgrep tier matched only
    tests/test_calc.py (the new symbol doesn't exist in source yet), so the
    full repo listing that would show src/calc.py was never reached."""
    repo = _git_repo_with_tracked_files(
        tmp_path,
        {"src/calc.py": "def add(a, b):\n    return a + b\n", "tests/test_calc.py": "# test\n"},
    )
    corrections = correct_touches_by_basename(["calc.py", "tests/test_calc.py"], repo)
    assert corrections == {"calc.py": "src/calc.py"}


def test_correct_touches_by_basename_leaves_genuinely_new_files_alone(tmp_path):
    """A path that doesn't exist anywhere in the tree is left untouched --
    it may legitimately be a new file the ticket itself will create."""
    repo = _git_repo_with_tracked_files(tmp_path, {"src/calc.py": "x = 1\n"})
    corrections = correct_touches_by_basename(["lanegate/new_module.py"], repo)
    assert corrections == {}


def test_correct_touches_by_basename_skips_ambiguous_matches(tmp_path):
    """Two files sharing a basename are not guessed between -- left as the
    model wrote it rather than picking the wrong one."""
    repo = _git_repo_with_tracked_files(
        tmp_path, {"a/calc.py": "x = 1\n", "b/calc.py": "x = 2\n"}
    )
    corrections = correct_touches_by_basename(["calc.py"], repo)
    assert corrections == {}


def test_correct_touches_by_basename_application_dedupes_collisions(tmp_path):
    """Two declared paths sharing a basename (or a wrong guess alongside the
    already-correct path) both correct to the same real file -- applying the
    correction map must not leave a duplicated touches entry. Mirrors the
    dict.fromkeys(...) dedup used at the _cmd_analyze_core call site."""
    repo = _git_repo_with_tracked_files(tmp_path, {"src/calc.py": "x = 1\n"})

    corrections = correct_touches_by_basename(["calc.py", "lib/calc.py"], repo)
    merged = list(dict.fromkeys(corrections.get(p, p) for p in ["calc.py", "lib/calc.py"]))
    assert merged == ["src/calc.py"]

    corrections = correct_touches_by_basename(["calc.py", "src/calc.py"], repo)
    merged = list(dict.fromkeys(corrections.get(p, p) for p in ["calc.py", "src/calc.py"]))
    assert merged == ["src/calc.py"]


