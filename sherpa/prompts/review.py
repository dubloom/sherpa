from pathlib import Path


def get_review_prompt(
    repo_root: Path,
    modified_files: str,
    git_diff: str,
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
    "line_range": "start-end or start",
    "details": "why this is a problem",
    "suggested_fix": "how to fix it",
    }}
],
"nice_to_have": []
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
- "nice_to_have" is optional. Default to an empty array []. Do not invent nits, style opinions, or test ideas just to fill the schema—most reviews should have zero nice_to_have items.
- Only add a nice_to_have item when it is clearly worthwhile (e.g. concrete follow-up with obvious payoff). At most 2 items, IDs N0, N1, ordered by impact. If unsure, omit.
- Prefer one proven high-impact issue over several speculative issues.
- If the diff is large, prioritize correctness and regression risks over style; you may omit low-impact nits to stay within a reasonable tool budget.

Context:
- Repository root: {repo_root}
- Changed files:
{modified_files or "(none)"}

Diff:
{git_diff}
"""