"""
Use glyph to explore the repo (Read / Glob / Grep) and propose a fix for a PR review thread.

Does not modify files; output is advisory text for the developer.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from glyph import AgentOptions, AgentQueryCompleted, AgentText, query

if TYPE_CHECKING:
    from sherpa.commands.address import CommentThread


def _read_snippet_lines(repo_root: Path, path: str, start_line: int, end_line: int, *, context: int = 4) -> list[str]:
    target = repo_root / path
    if not target.exists():
        return [f"(file not found locally: {path})"]

    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    begin = max(1, start_line - context)
    finish = min(len(lines), end_line + context)

    out = [f"File: {path}  (comment span lines {start_line}-{end_line}, showing {begin}-{finish})"]
    for line_no in range(begin, finish + 1):
        marker = ">" if start_line <= line_no <= end_line else " "
        content = lines[line_no - 1]
        out.append(f"{marker} {line_no:4d} | {content}")
    return out


def _thread_snippet_for_prompt(repo_root: Path, thread: CommentThread) -> list[str]:
    comments = thread.comments
    if not comments:
        return ["(empty thread)"]
    loc = comments[0].code_location
    path = loc.path
    if not path:
        return ["(no path on first comment)"]
    end_line = loc.line or loc.start_line
    start_line = loc.start_line or end_line
    if not isinstance(end_line, int):
        return [f"File: {path}", "(no line numbers from API)"]
    if not isinstance(start_line, int):
        start_line = end_line
    return _read_snippet_lines(repo_root, str(path), start_line, end_line)


def _format_thread_transcript(thread: CommentThread) -> str:
    blocks: list[str] = []
    for c in thread.comments:
        blocks.append(f"**{c.pseudo}** ({c.created_at or 'unknown'}):\n{c.comment or ''}")
    return "\n\n---\n\n".join(blocks)


def _other_threads_summary(current: CommentThread, all_threads: list[CommentThread]) -> str:
    lines: list[str] = []
    for t in all_threads:
        if t is current:
            continue
        first = t.comments[0] if t.comments else None
        if not first:
            continue
        loc = first.code_location
        path = loc.path if loc else None
        line = loc.line or loc.start_line if loc else None
        where = f"{path}:{line}" if path and line else (path or "general")
        preview = (first.comment or "").splitlines()[0][:160] if first.comment else ""
        lines.append(f"- @ {where} — {preview}")
    return "\n".join(lines) if lines else "(no other threads in this session)"


def build_address_suggest_prompt(
    repo_root: Path,
    owner: str,
    repo: str,
    pr_number: int,
    thread: CommentThread,
    all_threads: list[CommentThread],
    user_instruction: Optional[str],
) -> str:
    snippet = "\n".join(_thread_snippet_for_prompt(repo_root, thread))
    transcript = _format_thread_transcript(thread)
    others = _other_threads_summary(thread, all_threads)
    instruction_block = (
        user_instruction.strip() if user_instruction and user_instruction.strip() else "(none)"
    )

    return f"""\
You are helping a developer respond to a GitHub pull request review comment thread.

Repository root (your cwd): {repo_root}
Pull request: {owner}/{repo}#{pr_number}

## Comment anchor + local snippet
The review is attached to a position in the codebase. The snippet below is from the **local**
checkout (may differ from the PR branch); use tools to read the current files you need.

```
{snippet}
```

## This thread (full conversation)
{transcript}

## Other threads on this PR (titles only; for cross-cutting context)
{others}

## User guidance (optional)
{instruction_block}

## What to do
1. Use **Read**, **Glob**, and **Grep** to inspect any relevant files (definitions, callers, tests, configs).
2. **Do not** modify the filesystem (no writes or patches applied by you).
3. In your **final** answer, give a concrete suggested fix:
   - Short rationale tied to the review.
   - Exact code changes as fenced code blocks and/or a unified diff the developer could apply.
   - Mention any files you relied on beyond the snippet.

Keep the final suggestion focused on resolving **this** thread.
"""


def build_address_apply_prompt(
    repo_root: Path,
    owner: str,
    repo: str,
    pr_number: int,
    thread: CommentThread,
    all_threads: list[CommentThread],
    suggested_fix: Optional[str],
    user_instruction: Optional[str],
) -> str:
    snippet = "\n".join(_thread_snippet_for_prompt(repo_root, thread))
    transcript = _format_thread_transcript(thread)
    others = _other_threads_summary(thread, all_threads)
    suggestion_block = suggested_fix.strip() if suggested_fix and suggested_fix.strip() else "(none)"
    instruction_block = (
        user_instruction.strip() if user_instruction and user_instruction.strip() else "(none)"
    )

    return f"""\
You are an autonomous coding agent working in a git repository checkout.
Implement a concrete fix for one GitHub pull request review thread.

Repository root (your cwd): {repo_root}
Pull request: {owner}/{repo}#{pr_number}

## Comment anchor + local snippet
```
{snippet}
```

## This thread (full conversation)
{transcript}

## Other threads on this PR (titles only)
{others}

## AI suggested fix (advisory, optional)
{suggestion_block}

## User instruction (optional)
{instruction_block}

## Requirements
1. Make code changes directly in the working tree to resolve this thread.
2. Keep scope tight: do not refactor unrelated code.
3. If needed, update/add focused tests related to this fix.
4. If AI suggestion and user instruction conflict, prioritize user instruction.
5. Do not only describe a patch; apply real file edits.
6. Never claim a file change unless it was written in this workspace.
7. Finish with a concise completion note describing what changed and why.
"""


async def suggest_fix_for_thread_async(
    repo_root: Path,
    owner: str,
    repo: str,
    pr_number: int,
    thread: CommentThread,
    all_threads: list[CommentThread],
    user_instruction: Optional[str],
    model: str,
) -> tuple[str, Optional[float]]:
    prompt = build_address_suggest_prompt(
        repo_root, owner, repo, pr_number, thread, all_threads, user_instruction
    )
    options = AgentOptions(
        cwd=repo_root,
        model=model,
        allowed_tools=["Read", "Glob", "Grep"],
        instructions=(
            "You may open any files under the repository to understand behavior, tests, and imports. "
            "Stay scoped to fixing what this review thread asks for. "
            "Never use write or edit tools; only describe changes in natural language and code blocks."
        ),
        max_turns=30,
    )

    text = ""
    total_cost_usd: Optional[float] = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AgentText):
            text += message.text + "\n"
        elif isinstance(message, AgentQueryCompleted):
            if not text.strip() and isinstance(message.message, str) and message.message.strip():
                text = message.message.strip()
            raw_cost: Any = message.extra.get("total_cost_usd")
            if isinstance(raw_cost, int | float):
                total_cost_usd = float(raw_cost)

    out = text.strip()
    if not out:
        out = "(No suggestion text was returned.)"
    return out, total_cost_usd


async def apply_fix_for_thread_async(
    repo_root: Path,
    owner: str,
    repo: str,
    pr_number: int,
    thread: CommentThread,
    all_threads: list[CommentThread],
    suggested_fix: Optional[str],
    user_instruction: Optional[str],
    model: str,
) -> tuple[Optional[float], Optional[str]]:
    prompt = build_address_apply_prompt(
        repo_root,
        owner,
        repo,
        pr_number,
        thread,
        all_threads,
        suggested_fix,
        user_instruction,
    )
    options = AgentOptions(
        cwd=repo_root,
        model=model,
        allowed_tools=["Read", "Write", "Edit", "Glob", "Grep"],
        instructions=(
            "Before writing, inspect relevant files and callers for context. "
            "Keep the fix minimal and constrained to resolving this review thread. "
            "Use write/edit tools to apply real changes, and never report hypothetical edits."
        ),
        max_turns=40,
    )

    completion_text: Optional[str] = None
    streamed_text = ""
    total_cost_usd: Optional[float] = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AgentText):
            streamed_text += message.text + "\n"
        elif isinstance(message, AgentQueryCompleted):
            if isinstance(message.message, str) and message.message.strip():
                completion_text = message.message.strip()
            elif streamed_text.strip():
                completion_text = streamed_text.strip()

            raw_cost: Any = message.extra.get("total_cost_usd")
            if isinstance(raw_cost, int | float):
                total_cost_usd = float(raw_cost)
            elif isinstance(message.total_cost_usd, int | float):
                total_cost_usd = float(message.total_cost_usd)

    return total_cost_usd, completion_text


def suggest_fix_for_thread(
    repo_root: Path,
    owner: str,
    repo: str,
    pr_number: int,
    thread: CommentThread,
    all_threads: list[CommentThread],
    user_instruction: Optional[str],
    model: str,
) -> tuple[str, Optional[float]]:
    return asyncio.run(
        suggest_fix_for_thread_async(
            repo_root,
            owner,
            repo,
            pr_number,
            thread,
            all_threads,
            user_instruction,
            model,
        )
    )
