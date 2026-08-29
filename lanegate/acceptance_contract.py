"""Acceptance-contract extraction and verification helpers."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from lanegate import APP_NAME
from lanegate.config import load_config, resolve_trunk_branch
from lanegate.analyze_symbols import _CONTRACT_VERBS, _MAX_ACCEPTANCE_REF_BYTES, _STOPWORDS
from lanegate.ticket import (
    canonical_id,
    collect_cross_ticket_change_notes,
    load_all_tickets,
    load_change_notes,
    load_file_skeletons,
    validate_acceptance_matrix,
)



# ---------------------------------------------------------------------------
# AST symbol index (Python files only — stdlib ast, no extra deps)
# ---------------------------------------------------------------------------




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
    overlapping_terms = {term for term in heading_terms if term in ticket_norm}
    return len(overlapping_terms) >= 2


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
