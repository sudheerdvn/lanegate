# Spec Compatibility Boundary

LaneGate should be compatible with external spec-driven development tools without
becoming one of them. Its product identity is the repo-local execution control
plane: scoped tickets, deterministic lifecycle state, file locks, worktrees,
review gates, executor routing, and merge control.

External spec tools can shape the work. LaneGate turns that work into executable
units and records what happened.

## Product Boundary

LaneGate is **not a spec-authoring IDE replacement** and should not compete to be
the canonical place where teams write requirements, product requirements
documents, design docs, or task breakdowns. IDEs, CLIs, agent frameworks, issue
trackers, and research tools can keep owning authoring, discussion, and design
exploration.

LaneGate owns the part after intent becomes executable work:

- converting imported artifacts into one or more repo-local tickets
- validating ticket metadata before lifecycle actions run
- passing scoped context to the configured executor during `analyze`,
  `implement`, and review flows
- recording executor planning output as ticket metadata or ticket notes
- enforcing file locks, worktree isolation, status transitions, review gates,
  and merge state

The important distinction is judgment versus control. Agents and executors do
planning and design judgment. LaneGate records, validates, and gates workflow
state with deterministic code.

## Artifact Families

LaneGate should consider accepting external artifacts when they can be preserved
as source context and mapped into tickets without pretending to understand the
entire external product model.

Likely import families:

| Family | Examples | Import stance |
|---|---|---|
| Requirements and product specs | PRDs, feature briefs, acceptance criteria, RFCs | Accept as untrusted body context and close-criteria candidates. |
| Design artifacts | Architecture notes, design docs, ADR-style proposals, diagrams with text exports | Accept as contextual attachments or body sections for executor planning. |
| Task breakdowns | Generated task lists, milestones, issue epics, agent-produced plans | Accept as ticket candidates, dependencies, priority hints, and body context. |
| Issue tracker records | GitHub Issues, Linear/Jira tickets, labels, comments, links | Accept as source-linked ticket drafts with trusted orchestration fields re-derived by LaneGate. |
| Agent planning output | Analyze plans, implementation outlines, risk notes, test plans | Record as ticket metadata or notes after executor runs, not as authoritative lifecycle state. |
| Machine-readable specs | OpenAPI, GraphQL schemas, AsyncAPI, JSON Schema, protobuf IDLs | Accept as referenced context for executors; LaneGate should not become the semantic validator for each domain. |
| Test and behavior specs | Gherkin, markdown acceptance tests, example fixtures | Accept as close-criteria candidates and executor context. |

LaneGate should reject or defer imports that require it to become the canonical
editor, reviewer, or semantic engine for that format. For example, accepting an
OpenAPI file as context is in scope; becoming an OpenAPI design studio is out
of scope.

## Export Surface

LaneGate may export artifacts that help external tools understand execution state
without making those tools the source of truth for LaneGate-owned fields.

Useful exports:

- ticket summaries with id, title, status, priority, dependencies, close
  criteria, touches, branch, worktree, executor, reviewer, and review verdict
- board snapshots grouped by status for dashboards or spec tools
- execution logs and executor planning notes for traceability
- review summaries and merge outcomes
- generated issue comments or backlinks that point from an external spec to
  the LaneGate ticket that executed it
- machine-readable JSON for local API or MCP consumers

Exported data is descriptive. Importing it back later must still pass LaneGate's
schema validation and lifecycle checks.

## Mapping Specs To Tickets

External artifacts map into LaneGate tickets through a narrow translation layer.
That layer should preserve source context, derive safe metadata, and leave
implementation judgment to the configured executor.

| External artifact field | LaneGate ticket field | Rule |
|---|---|---|
| Source id, URL, tool name, version | `source`, body links, optional import metadata | Preserve for audit and round-trip context. |
| Title or summary | `title` | Copy or summarize as untrusted text. |
| Problem statement, requirements, design notes | Markdown body | Preserve as untrusted context. |
| Acceptance criteria, expected behavior, test scenarios | `close_criteria` | Import as candidates; executor or human analysis may refine. |
| File hints, component names, changed areas | `touches` candidates | Treat as hints until `analyze` or a human validates concrete paths. |
| Priority, labels, milestone | `priority`, milestone-like metadata when supported | Map only recognized values; preserve unknown labels in body/import metadata. |
| Blocking relationships, task order | `depends_on` candidates | Validate referenced LaneGate ticket ids before enabling dependency gates. |
| Proposed executor, model, assignee | `executor`, `reviewer` candidates | Accept only configured and allowed values. |
| Task list or implementation plan | one or more tickets | Split when work has distinct close criteria or non-overlapping `touches`. |

One spec can map to many LaneGate tickets when it contains independently
reviewable work, risky sequencing, or files that should run in parallel under
separate locks. Many specs can map to one ticket only when they describe one
coherent change with one close-criteria set and one scoped `touches` list.

Ticket creation should preserve enough provenance that a user can answer:
"which external artifact caused this ticket, and what imported text did the
executor see?"

## Executor Handoff

During `analyze`, LaneGate passes imported spec content to the configured executor
as untrusted ticket content. The executor may propose `touches`,
`close_criteria`, dependencies, risks, and a plan. LaneGate then records the
structured parts only after validation.

During implementation, LaneGate does not do design judgment itself. It passes the
ticket body, close criteria, touches, dependency context, and relevant imported
spec text to the executor. The executor plans, edits, and tests inside the
ticket worktree. LaneGate enforces the declared scope and lifecycle transitions
around that work.

This keeps the responsibilities crisp:

| Concern | Owner |
|---|---|
| Interpret ambiguous spec prose | Executor or human |
| Decide implementation approach | Executor or human |
| Propose files to touch during analysis | Executor or human |
| Validate ticket schema and allowed enum values | LaneGate |
| Enforce status transitions | LaneGate |
| Enforce file locks and drift checks | LaneGate |
| Create worktrees and branches | LaneGate |
| Route to configured executor/reviewer | LaneGate |
| Record review verdict and merge state | LaneGate |

## LaneGate-Owned Fields

Imported specs may suggest values, but the following are LaneGate-owned execution
concerns once a ticket exists:

- `touches`: concrete repo paths used for locks and drift checks
- `status`: lifecycle state
- `worktree`: local worktree path
- `branch`: ticket branch
- `executor`: resolved worker route from project config and ticket policy
- `reviewer`: resolved review route from project config and ticket policy
- `close_criteria`: execution acceptance contract after analyze/human approval
- `review_verdict`: recorded review decision
- dependency gates and merge eligibility
- merge commit/state

Spec imports can populate drafts or candidates for these fields, but LaneGate must
validate them before claiming support. A spec tool's status, task ownership, or
review state is not automatically LaneGate's lifecycle state.

## Trust Boundary

There are two classes of data.

**Trusted orchestration metadata** is written or validated by LaneGate before it
affects execution:

- ticket id allocation
- normalized status values
- concrete `touches` used for locking
- branch and worktree paths
- dependency graph edges after id validation
- configured executor and reviewer values
- review verdicts recorded through LaneGate lifecycle commands
- merge state and delivery state
- validated safeguards (quality gates that execute during ticket lifecycle)

**Untrusted spec content** is everything imported from outside LaneGate unless a
human or LaneGate validation step converts it into trusted metadata:

- titles, descriptions, requirements, design prose, comments, and links
- imported close-criteria text before approval
- proposed file paths or component names
- generated plans, task lists, and risk notes
- labels, assignee names, statuses, and tool-specific workflow state
- embedded instructions inside markdown, HTML, diagrams, or machine-readable
  specs

Imported content must be treated like ticket body text: preserve it for
context, pass it inside the untrusted-content boundary in executor prompts, and
scan it with the same prompt-injection defenses used for tickets. No imported
instruction may override LaneGate's lifecycle rules, tool permissions, lock checks,
review gates, or merge policy.

### Safeguards Security Model

The `safeguards` frontmatter field configures command execution during ticket
lifecycle transitions (`cmd_complete`, `cmd_merge`, `cmd_validate`). Because
safeguards run in the orchestrator's context, strict validation is required.

**Validation rules:**

1. **Schema validation:** `safeguards` must be a dict with keys in
   `{pre_complete, pre_merge, post_merge}`, each holding a string or list of
   strings. Invalid structures are rejected at ticket load time.

2. **Injection scanning:** All safeguard strings are scanned for prompt-injection
   patterns (instruction overrides, tag escapes, jailbreak keywords). Findings
   cause the ticket to fail analysis.

3. **Per-ticket restrictions:** Tickets that provide per-ticket safeguards may
   only use built-in guard types (pytest, npm, cargo, go, make). Shell scripts
   (`.sh` files) are project-level only. Per-ticket guards are appended to
   project-level guards, never replacing them, unless the ticket is marked
   `trusted: true`.

4. **Script-touches conflict check:** No guard script (`.sh` file) may appear in
   the ticket's `touches` list. This prevents agents from modifying guard
   scripts before they execute.

5. **Trust escalation:** Tickets imported from external sources must either:
   - Omit per-ticket safeguards entirely, or
   - Be marked `trusted: true` after human review, which allows full safeguards
     override

This model preserves project-level quality gates while allowing agents to
propose additional checks, without widening the attack surface to imported
content.

## Format Validation

LaneGate should not claim support for an import or export format until the format
has validation at three levels:

1. **Parse validation:** malformed files, unsupported versions, missing
   required fields, and unsafe encodings fail with clear errors.
2. **Schema mapping validation:** every field mapped into LaneGate metadata has an
   explicit rule, type check, allowed-value check, and fallback for unknown
   values.
3. **Lifecycle validation:** imported tickets must pass normal LaneGate ticket
   validation before `start`, `complete`, `review`, or `merge` can use them.

For each claimed format, fixtures should cover at least:

- a minimal valid artifact
- a representative real-world artifact
- unknown or future-version fields
- malformed input
- hostile prompt-injection text embedded in imported content
- round-trip export shape when export is supported

Compatibility should be advertised per format and version, for example
"imports GitHub Issue JSON vX fields A/B/C" rather than "supports GitHub" in a
broad, ambiguous way.

## Compatibility Survey

This survey is a compatibility target map, not a support claim. A family is
first-class only after LaneGate has fixtures, parser tests, mapping tests, and
round-trip export tests when export is advertised.
Vendor-specific compatibility must be tested before it is advertised, using
that vendor's actual artifacts before README, blog, roadmap, or release copy
names the vendor as supported.

Support tiers:

| Tier | Meaning |
|---|---|
| First-class | Versioned import and export shapes are fixture-tested, mapped to validated LaneGate ticket fields, and covered by lifecycle tests. |
| Experimental | The artifact shape is understood well enough to draft tickets, but compatibility is limited to documented examples and may require manual review. |
| Unsupported | LaneGate may preserve the artifact as untrusted context, but does not parse it into trusted metadata or claim compatibility. |

### Requirements, Design, And Tasks Directories

Representative layout:

```text
.specs/
  checkout-flow/
    requirements.md
    design.md
    tasks.md
```

Common variants use `requirements/design/tasks`, `reqs/design/tasks`, or one
feature directory per spec. `requirements.md` usually carries user stories,
constraints, and acceptance criteria. `design.md` carries architecture,
interfaces, data model notes, and file/component hints. `tasks.md` carries a
numbered task list or markdown checkboxes.

Mapping:

| Artifact content | Import shape for TICK-113 | Export shape for TICK-114 |
|---|---|---|
| Feature directory name | Source grouping and default parent title. | Emit as source reference, not as a LaneGate-owned id. |
| Requirement headings and acceptance criteria | Copy into untrusted ticket body; import explicit criteria as close-criteria candidates. | Include ticket close criteria and provenance links in markdown or JSON. |
| Design sections and implementation notes | Copy into untrusted body context; treat file paths as `touches` candidates. | Export implementation notes only when they came from LaneGate notes or reviewed ticket content. |
| Task checkboxes or numbered tasks | Create many tickets when tasks have separate acceptance criteria or scopes; otherwise create one parent ticket with task context. | Export LaneGate tickets as task entries with id, status, title, dependencies, close criteria, and touched paths. |
| `Task 1`, `1.2`, or checkbox labels | Preserve as external task references; do not treat as stable LaneGate ids. | Include original external refs in source metadata for round-trip context. |
| Mentions such as "after task 1" | Import as dependency candidates only when the referenced task can be resolved within the same import batch. | Export validated `depends_on` values as LaneGate ticket ids. |

Trust and metadata boundary:

- Untrusted: all requirement prose, design prose, generated plans, checkbox
  text, and embedded instructions.
- Candidate metadata: title, close criteria, `touches`, priority hints, and
  dependency candidates.
- LaneGate-owned after validation: ticket ids, concrete `touches`, `depends_on`,
  status, executor, reviewer, worktree, branch, review verdict, and merge state.

Support tier: **experimental** until fixtures cover multiple real directory
layouts, task-id variants, dependency phrases, malformed markdown, and hostile
embedded instructions. It can become **first-class** for LaneGate's documented
generic directory schema without implying compatibility with a named vendor.

### Spec, Plan, And Tasks Directories

Representative layout:

```text
specs/
  billing-retry/
    spec.md
    plan.md
    tasks.md
```

This family separates desired behavior from implementation planning. `spec.md`
usually contains the problem, behavior, non-goals, and acceptance criteria.
`plan.md` often contains architecture, implementation sequence, risk, and test
strategy. `tasks.md` contains implementation steps.

Mapping:

| Artifact content | Import shape for TICK-113 | Export shape for TICK-114 |
|---|---|---|
| `spec.md` title and behavior sections | Ticket title and untrusted body context. | Export as a source summary plus LaneGate ticket title/body excerpt when supported. |
| Acceptance criteria in `spec.md` | Close-criteria candidates; preserve original wording in body. | Export validated close criteria as the execution contract. |
| `plan.md` implementation sections | Untrusted planning context for executor handoff. | Export executor analysis notes separately from original imported plan text. |
| `tasks.md` steps | Split into child tickets when each step has independent scope, dependencies, or close criteria. | Export one task row per LaneGate ticket, including parent/source grouping. |
| Paths in plan or tasks | `touches` candidates only; validate against the repo before lock use. | Export concrete validated `touches`, not unvalidated source hints. |

Import should default to a parent/child grouping when the directory describes a
feature plus several reviewable implementation tasks. The parent represents
source provenance and shared acceptance context; children represent executable
LaneGate tickets. A single ticket is appropriate when `tasks.md` is a short
checklist for one coherent change.

Support tier: **experimental**. The shape is close to LaneGate's lifecycle, but
the boundary between imported plan text and LaneGate-owned executor analysis must
be tested before import/export can be advertised.

### Plain Markdown Task Checklists

Representative artifact:

```markdown
# Improve deploy command

- [ ] Validate config before creating a worktree.
- [ ] Add regression coverage for missing remotes.
- [ ] Update troubleshooting docs.
```

These artifacts are low-structure and often have no stable ids, owners,
dependencies, paths, or explicit acceptance criteria. Headings provide weak
grouping. Checkboxes provide task text but not lifecycle state.

Mapping:

| Artifact content | Import shape for TICK-113 | Export shape for TICK-114 |
|---|---|---|
| Document title | Parent ticket title or import batch label. | Export as a generated markdown checklist only when the user requests a low-structure view. |
| Checkbox text | One ticket per checkbox only when the user chooses a split import; otherwise one ticket with checklist context. | Export LaneGate ticket status as `[ ]` or `[x]` only as a presentation layer, not as source of truth. |
| Nested bullets | Preserve under the related checkbox as untrusted body context. | Preserve as descriptive notes when round-tripping to markdown. |
| Inline file names | `touches` candidates after path normalization and repo validation. | Export validated paths under each generated task. |

Support tier: **first-class** only for LaneGate-defined plain markdown checklist
fixtures because the generic markdown shape is small and controllable.
Compatibility with any product that happens to emit markdown checklists remains
**experimental** until that product's exact output is tested.

### GitHub Issues And Project-Board Descriptions

Representative artifact shapes:

```json
{
  "number": 42,
  "title": "Add deploy dry-run",
  "body": "## Acceptance Criteria\n- dry-run reports planned steps",
  "labels": [{"name": "priority:high"}],
  "assignees": [{"login": "dev"}],
  "milestone": {"title": "v2"},
  "state": "open"
}
```

Project-board records may wrap issue data with status columns, fields,
iteration names, priorities, and URLs.

Mapping:

| Artifact content | Import shape for TICK-113 | Export shape for TICK-114 |
|---|---|---|
| Issue number and URL | Source reference only; never reused as the LaneGate ticket id. | Export backlink metadata and optional issue comment body. |
| Title and body | Ticket title and untrusted body context. | Export ticket summary, status, review result, and links back to LaneGate state. |
| Labels and milestone | Map recognized labels to candidate priority or grouping; preserve unknown labels. | Export LaneGate labels only if a stable mapping is configured. |
| Issue state and board column | Preserve as source context; do not import as LaneGate lifecycle status. | Export LaneGate status descriptively without mutating the external board unless an integration owns that action. |
| Body checklists | Close-criteria candidates or child ticket candidates depending on structure. | Export close criteria and task status using documented markdown or JSON shape. |

Support tier: **unsupported** as a broad claim. LaneGate can preserve copied issue
text as untrusted ticket body today, but first-class GitHub import/export would
require tested API payload fixtures, permission modeling, rate-limit behavior,
comment/update conflict handling, and explicit versioned field mappings.

### Manually Written LaneGate-Style Markdown Tickets

Representative artifact:

```markdown
# TICK-123: Add deploy dry-run

TOUCHES:
lanegate/deploy.py, tests/test_deploy.py

CLOSE CRITERIA:
- `lanegate deploy --dry-run` prints planned actions and does not mutate state.

BODY:
Implement a dry-run path for deploy.
```

This is the closest external shape to LaneGate's native ticket model. It may come
from a human, a local script, a copied issue, or another agent. Matching the
heading names is not enough to trust the values.

Mapping:

| Artifact content | Import shape for TICK-113 | Export shape for TICK-114 |
|---|---|---|
| Ticket-like id in heading | Preserve as external id unless it was allocated by the current LaneGate repo. | Export LaneGate id as authoritative for this repo. |
| `TOUCHES` block | Path candidates; validate existence, normalization, and lock eligibility. | Export validated `touches` exactly as LaneGate stores them. |
| `CLOSE CRITERIA` block | Close-criteria candidates; require lifecycle validation before use. | Export validated criteria in stable markdown and JSON forms. |
| Dependencies block, if present | Validate references to existing or same-batch LaneGate ticket ids. | Export validated `depends_on` ids only. |
| Body text | Untrusted ticket body context. | Export body text according to the selected export policy. |

Support tier: **first-class** for LaneGate's own documented markdown export shape
once importer/exporter tests exist. Manually written lookalikes are
**experimental** because they may omit required metadata, use stale ids, or
contain unsafe instructions.

### Intentionally Unsupported Or Deferred Shapes

Some artifacts should remain out of scope for metadata import until there is a
specific parser and lifecycle reason:

| Artifact family | Tier | Reason |
|---|---|---|
| Screenshots, images, whiteboards, and diagrams without structured text | Unsupported | Preserve as attachments or links only; no reliable field mapping. |
| Chat transcripts and agent conversations | Unsupported | Useful as body context, but too ambiguous for trusted ids, status, dependencies, or close criteria. |
| Domain schemas such as OpenAPI, GraphQL, protobuf, or JSON Schema | Unsupported for ticket metadata | Accept as executor context; LaneGate should not infer lifecycle tickets from domain semantics without an explicit adapter. |
| Vendor project databases without exported schema/version | Unsupported | Field meaning, permissions, and status semantics cannot be validated. |

### Fixture Corpus Plan

Future importer/exporter work should maintain fixtures under
`tests/fixtures/spec_artifacts/`. The corpus should include one directory per
artifact family and keep raw source fixtures separate from expected LaneGate
ticket shapes.

Minimum fixture plan:

| Fixture family | Files | Purpose |
|---|---|---|
| `requirements_design_tasks/` | `minimal/requirements.md`, `minimal/design.md`, `minimal/tasks.md`, representative and malformed variants | Exercise feature-directory imports, acceptance criteria extraction, task splitting, dependency candidates, path hints, and injection handling. |
| `spec_plan_tasks/` | `minimal/spec.md`, `minimal/plan.md`, `minimal/tasks.md`, representative and malformed variants | Exercise parent/child grouping, separation of source plan text from executor analysis, and export back to grouped tasks. |
| `plain_markdown_checklists/` | `minimal.md`, `nested.md`, `with_paths.md`, `hostile.md`, `malformed.md` | Exercise low-structure imports, split-versus-single-ticket behavior, path candidate validation, and markdown export. |
| `github_issues/` | `issue_minimal.json`, `issue_with_project_fields.json`, `issue_body_checklist.md`, `expected_issue_comment.md` | Exercise source id preservation, label mapping, body checklist extraction, and export comment shape without claiming API integration. |
| `lanegate_markdown_tickets/` | `ticket_minimal.md`, `ticket_with_dependencies.md`, `ticket_invalid_paths.md`, `expected_export.md`, `expected_export.json` | Exercise LaneGate's own import/export contract, trusted-field validation, dependency validation, and round-trip shape. |

Each family should also include expected output files such as
`expected/tickets.json` or `expected/export.md` once TICK-113 and TICK-114
implement import/export. Expected files should distinguish:

- `body`: untrusted copied source content
- `metadata_candidates`: values proposed by the importer
- `validated_metadata`: values LaneGate accepts after schema and lifecycle checks
- `source`: external family, path, id, URL, and parser version

This gives TICK-113 enough input shape to implement import safely and gives
TICK-114 enough output shape to export tickets without overstating
compatibility.

## Positioning

README and blog copy should avoid framing LaneGate as a weaker spec-authoring IDE.
The message is:

> Bring your specs from wherever they are written. LaneGate turns them into
> repo-local execution units with locks, worktrees, review gates, executor
> routing, and merge control.

Good positioning:

- "execution control plane for agentic development"
- "compatible with spec-driven workflows without owning the authoring surface"
- "turn specs and plans into scoped, reviewable, mergeable tickets"
- "keep deterministic workflow state in the repo while agents do the coding"

Avoid:

- "the best place to write specs"
- "an IDE for requirements"
- "automatic product/design judgment"
- "canonical generator for PRDs, design docs, and tasks"

This boundary makes LaneGate stronger, not weaker: it lets dedicated spec tools
and coding agents improve quickly while LaneGate stays focused on the execution
state they need but usually do not own.
