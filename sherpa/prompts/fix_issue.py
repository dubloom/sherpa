from pathlib import Path
from typing import Optional

from sherpa.commands.review import Issue


def get_fix_issue_prompt(
    repo_root: Path,
    issue: Issue,
    modified_files: Optional[str],
    git_diff: Optional[str],
    extra_instruction: Optional[str] = None,
) -> str:
    sev = issue.severity if issue.severity is not None else "(nit)"
    extra = ""
    if extra_instruction:
        extra = f"""
When fixing this issue, be aware of the below instructions from the user:
{extra_instruction}
"""

    return f"""\
You are an autonomous coding agent working in a git repository.
Fix ONLY the following review issue. Do not address other findings or refactor unrelated code.

Issue ID: {issue.name}
Severity: {sev}
Title: {issue.title}
File: {issue.file}

Problem:
{issue.details}

Suggested fix (follow unless you have a clearly better minimal fix):
{issue.suggested_fix}

Repository root: {repo_root}

Context from the review (staged files at review time):
Changed files:
{modified_files or "(unknown)"}

Staged diff at review time (may differ from current working tree):
{git_diff or "(none)"}
{extra}

Requirements:
- Prefer changing `{issue.file}` unless the issue is clearly elsewhere.
"""
