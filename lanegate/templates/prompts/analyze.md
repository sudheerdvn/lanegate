You are analyzing a software ticket to determine which files need to change.

{{ context_sections }}

## Ticket
ID: {{ ticket_id }}
Title: {{ title }}
Intent: {{ intent }}

## Task
Return ONLY a JSON object (no markdown, no explanation) with this exact shape:
{
  "touches": ["path/to/file.py", "tests/test_foo.py"],
  "close_criteria": "One sentence: what must be true when this ticket is done, phrased so it can be checked mechanically.",
  "depends_on": [],
  "change_notes": {
    "path/to/file.py": "Brief description of what changes in this file and where (~line numbers if possible).",
    "tests/test_foo.py": "Add test_foo, test_bar following pattern from test_baz."
  },
  "model": "claude-haiku-4-5-20251001"
}

Rules:
- touches must be real files from the relevant context above, or new files clearly implied by the intent.
- If touches span multiple new files in the same directory or package (e.g. tests/foo/test_a.py, tests/foo/test_b.py), you MUST also include shared infrastructure files that those files will need: tests/foo/conftest.py for shared pytest fixtures, tests/foo/__init__.py if it is a package, lanegate/foo/__init__.py for new Python packages. Include these even if the intent does not name them explicitly — they are structurally implied.
- close_criteria must be a single concrete, verifiable sentence. If the change is testable in code, name the specific test/assertion that proves it done (e.g. "test_upload.py::test_rate_limit asserts 429 after 100 req/min"). If it genuinely cannot be tested in code (pure UI/visual, infra, docs-only), name the exact manual verification step instead (e.g. "run the app, hit /upload 101 times in 60s, confirm the 101st returns 429").
- close_criteria must preserve the full acceptance contract from the title, intent, and any linked repo docs or prior tickets named in the intent. Do not narrow acceptance to "tests pass" when the source intent names endpoints, response fields, lifecycle behavior, or design-contract items.
- If the change is testable in code, touches MUST include a test file (new or existing) that exercises the new behavior — not just the implementation file.
- depends_on is a list of ticket IDs this ticket must wait for (usually empty).
- change_notes is the implementer's blueprint — make it precise enough that the implementer can jump directly to edits without any investigation: name the exact function/class/line range to change, the specific logic to add or modify, and how it connects to adjacent code. Vague descriptions like "update foo to support bar" are not acceptable; write "in foo.py:_run() (~L120), add an elif branch for bar that calls baz(x, y) and returns the result — see the existing elif for qux at L115 as the pattern".
- model is optional. Recommend based on complexity: "claude-haiku-4-5" for 1-3 files with purely additive changes; "claude-sonnet-5" for 4-8 files or modifying existing logic; "claude-opus-4-8" for 9+ files, core redesigns, or tickets where analysis itself is uncertain. Omit this field if unsure.
- Return only the JSON. No prose before or after.
