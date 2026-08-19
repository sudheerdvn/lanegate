You are analyzing a software ticket to determine which files need to change.

{{ context_sections }}

## Ticket
ID: {{ ticket_id }}
Title: {{ title }}
Intent: {{ intent }}
Acceptance matrix required from the current ticket context: {{ requires_acceptance_matrix }}

## Task
Before proposing touches, check whether the problem or deliverable the ticket describes still exists in the code shown above (symbol matches, importers, project guidance, reference-doc excerpts). Tickets are sometimes filed against a bug or gap that a later, unrelated ticket already fixed — the intent text can be stale even though it reads as current. Don't take the ticket's premise on faith: if it names a specific function/behavior, check that the function still behaves that way in the skeletons/excerpts you were given, not just that the file exists.

If the described problem/deliverable is already fully present in the current code (not "close enough" — the specific behavior, function, or fix the ticket asks for is already there), return ONLY this shape instead of touches:
{
  "already_resolved": true,
  "already_resolved_reason": "Cite the specific file/line/function from the context above that already does this, and why it satisfies the ticket's ask."
}
Do not guess at this from the file list alone — only claim already_resolved when the excerpts/skeletons above show you the actual current behavior, not just that a plausibly-named file exists. If you're not sure, proceed with normal analysis instead of claiming already_resolved.

Otherwise, return ONLY a JSON object (no markdown, no explanation) with this exact shape:
{
  "touches": ["path/to/file.ext", "tests/test_foo.ext"],
  "close_criteria": "One sentence: what must be true when this ticket is done, phrased so it can be checked mechanically.",
  "depends_on": [],
  "change_notes": {
    "path/to/file.ext": "Brief description of what changes in this file and where (~line numbers if possible).",
    "tests/test_foo.ext": "Add test_foo, test_bar following pattern from test_baz."
  },
  "acceptance_matrix": {
    "invariants": ["Invariant that must remain true."],
    "adversarial_cases": ["Concrete failure or hostile-input case."],
    "compatibility_cases": ["Existing behavior that must remain compatible."],
    "regression_tests": ["Exact test selector that proves the case."]
  },
  "overlap_review": {"mode": "dependencies", "ticket_ids": ["TICK-001"]},
  "model": "claude-haiku-4-5-20251001"
}

Rules:
- Before reading ranges of any source file beyond what the skeletons/excerpts above already showed you, you MUST run `lanegate symbols <file>` on it first. Only fall back to a raw read for the specific lines that `lanegate symbols` output tells you matter.
- touches must be real files from the relevant context above, or new files clearly implied by the intent.
- If touches span multiple new files in the same directory or package (e.g. tests/foo/test_a.ext, tests/foo/test_b.ext), you MUST also include shared infrastructure files that those files will need: any shared test setup/fixture file this language's test framework expects, and any package-init or module-declaration file this language requires for a new package. Include these even if the intent does not name them explicitly — they are structurally implied.
- close_criteria must be a single concrete, verifiable sentence. If the change is testable in code, name the specific test/assertion that proves it done, using this project's actual test-selection syntax (e.g. "test_upload asserts 429 after 100 req/min" or "TestUpload/RateLimit asserts 429 after 100 req/min"). If it genuinely cannot be tested in code (pure UI/visual, infra, docs-only), name the exact manual verification step instead (e.g. "run the app, hit /upload 101 times in 60s, confirm the 101st returns 429").
- close_criteria must preserve the full acceptance contract from the title, intent, and any linked repo docs or prior tickets named in the intent. Do not narrow acceptance to "tests pass" when the source intent names endpoints, response fields, lifecycle behavior, or design-contract items.
- If the change is testable in code, touches MUST include a test file (new or existing) that exercises the new behavior — not just the implementation file.
- If the change adds, removes, or upgrades a dependency, touches MUST include this project's lockfile alongside the manifest file it's paired with, using whatever pair this project's ecosystem actually uses — e.g. package.json + package-lock.json/pnpm-lock.yaml/yarn.lock (JS/TS/React/React Native), pyproject.toml + poetry.lock/uv.lock (Python), Cargo.toml + Cargo.lock (Rust), Gemfile + Gemfile.lock (Ruby), go.mod + go.sum (Go), composer.json + composer.lock (PHP), *.csproj + packages.lock.json (.NET), build.gradle(.kts) + gradle.lockfile, or settings.gradle(.kts) + libs.versions.toml for a Gradle version catalog shared across modules (Java/Kotlin/Android). Maven's pom.xml has no separate lockfile — for Maven projects, the pom.xml itself (including any parent/BOM pom in a multi-module build) is the file to declare. Omitting the lockfile/manifest lets two dependency-touching tickets pass the touches-overlap check and run concurrently, racing on the same file.
- depends_on is a list of ticket IDs this ticket must wait for (usually empty).
- change_notes is the implementer's blueprint — make it precise enough that the implementer can jump directly to edits without any investigation: name the exact function/class/line range to change, the specific logic to add or modify, and how it connects to adjacent code. Vague descriptions like "update foo to support bar" are not acceptable; write "in foo.ext:_run() (~L120), add a branch for bar that calls baz(x, y) and returns the result — see the existing branch for qux at L115 as the pattern".
- model is optional. Recommend based on complexity: "claude-haiku-4-5" for 1-3 files with purely additive changes; "claude-sonnet-5" for 4-8 files or modifying existing logic; "claude-opus-4-8" for 9+ files, core redesigns, or tickets where analysis itself is uncertain. Omit this field if unsure.
- Return only the JSON. No prose before or after.
- When `requires_acceptance_matrix` is true, every acceptance_matrix list must be non-empty. For other tickets, the matrix may use empty lists or omit categories that do not apply.
- Compare your proposed touches with the active high-risk tickets listed in the context. If they overlap a non-terminal ticket, set overlap_review.mode to `dependencies` and include every overlapping ticket ID in depends_on, or set it to `stacked_review` and name every overlapping ticket ID.
- Omit overlap_review entirely when there is no active overlap. Never include this ticket's own ID in overlap_review or depends_on.
