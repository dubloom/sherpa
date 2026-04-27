from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Optional

from glyph import AgentQueryCompleted, run_markdown_workflow

_HOOK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


@dataclass(frozen=True)
class HookScaffold:
    workflow_path: Path


@dataclass(frozen=True)
class HookRunOutcome:
    should_continue: bool
    reason: str | None = None


def hooks_dir(repo_root: Path) -> Path:
    """Directory for commit pre-review hook workflows (``*.md``)."""
    return repo_root / ".sherpa" / "hooks"


def is_valid_hook_name(name: str) -> bool:
    return bool(_HOOK_NAME_RE.fullmatch(name))


def scaffold_hook(repo_root: Path, hook_name: str) -> HookScaffold:
    root = hooks_dir(repo_root)
    workflow_path = root / f"{hook_name}.md"

    if workflow_path.exists():
        raise FileExistsError(f"Hook {hook_name!r} already exists.")

    root.mkdir(parents=True, exist_ok=True)

    workflow_path.write_text(_hook_workflow_template(hook_name), encoding="utf-8")
    return HookScaffold(workflow_path=workflow_path)


def discover_hooks(repo_root: Path) -> list[Path]:
    root = hooks_dir(repo_root)
    if not root.is_dir():
        return []
    return sorted(path for path in root.glob("*.md") if path.is_file())


def run_commit_pre_review_hooks(
    repo_root: Path,
    *,
    commit_args: list[str],
    commit_message: Optional[str],
    modified_files: str,
    git_diff: str,
) -> HookRunOutcome:
    hook_paths = discover_hooks(repo_root)
    if not hook_paths:
        return HookRunOutcome(should_continue=True)

    print("[sherpa] Running commit pre-review hooks")
    initial_input: dict[str, Any] = {
        "repo_root": str(repo_root),
        "commit_args": commit_args,
        "commit_message": commit_message,
        "modified_files": modified_files,
        "git_diff": git_diff,
    }

    for hook_path in hook_paths:
        print(f"[sherpa] Hook: {hook_path.stem}", end="", flush=True)
        try:
            raw_result = asyncio.run(run_markdown_workflow(hook_path, initial_input=initial_input))
            status, message = _parse_hook_result(raw_result)
        except Exception as exc:  # noqa: BLE001
            print(f" - Failed: {exc}")
            return HookRunOutcome(
                should_continue=False,
                reason=f"Hook {hook_path.name!r} failed: {exc}",
            )

        if status == "block":
            print(" - Blocked")
            reason = message or f"Hook {hook_path.name!r} blocked this commit."
            return HookRunOutcome(should_continue=False, reason=reason)

        print(" - Passed")

    return HookRunOutcome(should_continue=True)


def _parse_hook_result(raw_result: Any) -> tuple[str, str | None]:
    coerced = _coerce_hook_result(raw_result)

    if coerced is None:
        return "continue", None

    if isinstance(coerced, str):
        normalized_status = coerced.strip().lower()
        if normalized_status in {"continue", "block"}:
            return normalized_status, None
        raise ValueError(
            "Hook returned an invalid string result. Use 'continue' or 'block'."
        )

    if not isinstance(coerced, dict):
        raise ValueError(
            "Hook returned an invalid result. Expected a dict with "
            "'status' ('continue' or 'block') and optional 'message'."
        )

    raw_status = coerced.get("status", "continue")
    status = str(raw_status).strip().lower()
    if status not in {"continue", "block"}:
        raise ValueError(
            f"Hook returned invalid status {raw_status!r}. "
            "Expected 'continue' or 'block'."
        )

    message = coerced.get("message")
    if message is None:
        return status, None
    return status, str(message)


def _coerce_hook_result(raw_result: Any) -> Any:
    """Turn markdown LLM completion into the dict/str shape ``_parse_hook_result`` expects."""

    if not isinstance(raw_result, AgentQueryCompleted):
        return raw_result

    if raw_result.is_error:
        raise ValueError(raw_result.message or "Hook model run failed.")

    text = (raw_result.message or "").strip()
    if not text:
        raise ValueError("Hook model returned an empty message.")

    return _decode_hook_json_message(text)


def _strip_json_markdown_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _decode_hook_json_message(text: str) -> Any:
    """Parse JSON from the model reply (plain JSON or a single fenced block)."""

    candidate = _strip_json_markdown_fence(text).strip()
    last_json_error: json.JSONDecodeError | None = None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as err:
        last_json_error = err

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as err:
            last_json_error = err

    raise ValueError(
        "Hook model reply must be JSON with "
        "'status' ('continue' or 'block') and optional 'message'."
    ) from last_json_error


def _hook_workflow_template(hook_name: str) -> str:
    wf_name = f"{hook_name}SherpaHook"
    return f"""---
name: {wf_name}
description: Sherpa commit hook ({hook_name})
options:
  model: gpt-5.4-mini
  reasoning_effort: medium
---

<!-- Inputs: Sherpa passes a dict as initial_input to the workflow; in prompts use {{{{ key }}}} or {{{{ step_input.key }}}}. Keys: repo_root, commit_args, commit_message, modified_files, git_diff. -->

<!-- Return: the model's final message must be JSON only: {{"status": "continue"}} or {{"status": "block", "message": "reason"}}. -->

## Step: decide

Pre-review gate before Sherpa's main commit review.

Staged files:
{{{{ modified_files }}}}

Commit message (may be empty):
{{{{ commit_message }}}}

Diff:
{{{{ git_diff }}}}

Reply with **only** JSON (no markdown fences, no other text): `{{"status": "continue"}}` or `{{"status": "block", "message": "<short reason>"}}`.
"""
