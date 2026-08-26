from lanegate.safeguards import _OPERATIONAL_SECTION_RE as SAFEGUARDS_RE
from lanegate.analyze import _OPERATIONAL_SECTION_RE as ANALYZE_RE

text = "\n## Dismissal Rationale\n\n" + ("a" * 50000)

safe_stripped = SAFEGUARDS_RE.sub("", text)
analyze_stripped = ANALYZE_RE.sub("", text)

print(f"safeguards length: {len(safe_stripped)}")
print(f"analyze length: {len(analyze_stripped)}")
