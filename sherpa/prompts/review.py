from pathlib import Path

def get_review_prompt(
    repo_root: Path,
    modified_files: str,
    git_diff: str
):
    return f"""\
Review the provided git changes.
Return ONLY valid JSON with this exact shape:
{{
"decision": "APPROVE" | "BLOCKED",
"summary": "short one-line summary",
"issues": [
    {{
    "name": "H0 | M0 | L0",
    "title": "short title",
    "severity": "low" | "medium" | "high",
    "file": "path/to/file",
    "details": "why this is a problem",
    "suggested_fix": "how to fix it"
    }}
],
"nice_to_have": [
    {{
    "name": "N0",
    "title": "short title",
    "file": "path/to/file",
    "severity": None,
    "details": "optional non-blocking suggestion",
    "suggested_fix": "how to improve this"
    }}
]
}}
Do not include markdown fences.
Do not add any text before or after the JSON.

Review criteria:
- Use "issues" for all issue severities.
- Every issue must include a unique "name" using short IDs by severity:
- high: H0, H1, ...
- medium: M0, M1, ...
- low: L0, L1, ...
- High severity issues are blocking and should use decision=BLOCKED.
- High should be reserved for clear code malfunction or equivalent serious impact.
- Medium severity issues are warnings only and should not block.
- Low severity issues are informational only.
- If there are no high severity issues, use decision=APPROVE.
- Use "nice_to_have" for optional suggestions and assign IDs N0, N1, ...
- Return at most 2 nice_to_have items, ordered by impact (highest first).

Context:
- Repository root: {repo_root}
- Changed files:
{modified_files or "(none)"}

Diff:
{git_diff}
"""