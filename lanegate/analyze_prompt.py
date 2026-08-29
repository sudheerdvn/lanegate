"""Analyze prompt construction and response parsing helpers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

from lanegate.config import is_high_reasoning_ticket
from lanegate.analyze_symbols import _build_candidate_skeletons, enrich_context
from lanegate.ticket import (
    TERMINAL_STATUSES, collect_cross_ticket_change_notes, find_control_plane_touch_overlaps,
    load_all_tickets, load_change_notes, load_file_skeletons,
)



_ACTIVE_CONTROL_PLANE_BUDGET_BYTES = 4000

_ALREADY_RESOLVED_CITATION_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])(?P<path>(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+|"
    r"[A-Za-z0-9_-]+\.[A-Za-z0-9_.-]+|(?:Makefile|Dockerfile|Containerfile|Rakefile|Gemfile|Procfile))"
    r"\s*:\s*~?L?"
    r"(?P<start>\d+)(?:\s*[-–]\s*~?L?(?P<end>\d+))?"
)

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


