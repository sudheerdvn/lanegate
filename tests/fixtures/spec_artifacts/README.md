# Spec Artifact Fixture Corpus

This directory is reserved for fixture files that will validate future spec
artifact import/export work. The corpus should prove concrete artifact shapes
before LaneGate advertises compatibility with any format or vendor.

Fixtures should keep raw source artifacts separate from expected LaneGate shapes:

```text
tests/fixtures/spec_artifacts/
  requirements_design_tasks/
    minimal/
      requirements.md
      design.md
      tasks.md
    representative/
      requirements.md
      design.md
      tasks.md
    malformed/
      tasks.md
    hostile/
      requirements.md
      design.md
      tasks.md
    expected/
      tickets.json
      export.md
  spec_plan_tasks/
    minimal/
      spec.md
      plan.md
      tasks.md
    representative/
      spec.md
      plan.md
      tasks.md
    expected/
      tickets.json
      export.md
  plain_markdown_checklists/
    minimal.md
    nested.md
    with_paths.md
    hostile.md
    malformed.md
    expected/
      tickets_split.json
      tickets_single.json
      export.md
  github_issues/
    issue_minimal.json
    issue_with_project_fields.json
    issue_body_checklist.md
    expected/
      tickets.json
      issue_comment.md
  lanegate_markdown_tickets/
    ticket_minimal.md
    ticket_with_dependencies.md
    ticket_invalid_paths.md
    expected/
      tickets.json
      export.md
      export.json
```

## Artifact Families

`requirements_design_tasks/` exercises feature directories that separate
requirements, design notes, and task lists. Fixtures should cover acceptance
criteria extraction, task splitting, dependency candidates, path hints, unknown
sections, malformed markdown, and hostile instructions embedded in prose.
This family specifically exercises acceptance criteria extraction.

`spec_plan_tasks/` exercises directories that separate behavior specs,
implementation plans, and tasks. Fixtures should cover parent/child grouping,
preserving imported plan text as untrusted body content, and keeping executor
analysis output separate from source plan content during export.

`plain_markdown_checklists/` exercises low-structure markdown task lists.
Fixtures should cover single-ticket imports, split-ticket imports, nested
checklist context, inline path candidates, malformed checkboxes, and markdown
export using LaneGate status as presentation rather than source of truth.

`github_issues/` exercises copied or exported issue and project-board data
without claiming API-level compatibility. Fixtures should cover source id and
URL preservation, label and milestone mapping, body checklist extraction,
project-field preservation, and generated issue-comment export shape.
This family specifically exercises source id and URL preservation.

`lanegate_markdown_tickets/` exercises LaneGate's own markdown import/export contract.
Fixtures should cover title, touches, close criteria, dependencies, invalid
paths, expected markdown export, expected JSON export, and round-trip
preservation of source context.
This family specifically exercises round-trip preservation.

## Expected Shapes

Expected `tickets.json` fixtures should distinguish:

- `body`: untrusted source text copied into the ticket body.
- `metadata_candidates`: imported values proposed for title, close criteria,
  touches, dependencies, priority, executor, or reviewer.
- `validated_metadata`: values accepted after LaneGate schema, path, dependency,
  and lifecycle validation.
- `source`: artifact family, source path, external id or URL, parser version,
  and import timestamp policy.

Expected export fixtures should include only LaneGate-owned state as authoritative
metadata. Original artifact ids, labels, task numbers, issue numbers, and board
columns belong in source metadata or body context unless a tested adapter
defines a stricter mapping.
