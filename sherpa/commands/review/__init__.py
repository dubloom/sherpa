import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import re
import time
from typing import Literal, Optional
from agnos import AgentOptions, AgentQueryCompleted, AgentText, AgentToolCall, query
from agnos.messages import AgentThinking
from sherpa.commands.base import Command
from sherpa.commands.review.report import render_review_report
from sherpa.config import SherpaConfig
from sherpa.git import get_branch_changes, get_staged_changes
from sherpa.prompts.review import get_review_prompt
from sherpa.utils import extract_commit_message


@dataclass
class Issue:
    name: str
    title: str
    severity: Optional[str]
    file: str
    details: str
    suggested_fix: str


@dataclass
class ReviewResult:
    decision: Literal["APPROVE", "BLOCKED"]
    summary: str
    high_issues: list[Issue]
    medium_issues: list[Issue]
    low_issues: list[Issue]
    nits: list[Issue]


def extract_review_result(review: dict | str) -> Optional[ReviewResult]:
    if not isinstance(review, dict):
        return None

    decision = str(review.get("decision", "UNRECOGNIZED")).strip().upper()
    summary = str(review.get("summary", "")).strip()
    issues = review.get("issues")

    # Check that the json returned by the model is well formatted.
    def invalid_review_output():
        print("[sherpa] Model did not respond with valid JSON. Will show raw output")
        print(review)

    if not isinstance(issues, list) or any(not isinstance(i, dict) for i in issues):
        invalid_review_output()
        return

    def by_severity(level):
        return [
            Issue(**i) for i in issues
            if str(i.get("severity", "")).strip().lower() == level
        ]

    high_issues = by_severity("high")
    medium_issues = by_severity("medium")
    low_issues = by_severity("low")

    # if nits are not well formatted, we just skip them
    raw_nits = review.get("nice_to_have")
    if not isinstance(raw_nits, list) or any(not isinstance(i, dict) for i in raw_nits):
        raw_nits = []

    nits = []
    for nit in raw_nits:
        nits.append(Issue(**nit))

    return ReviewResult(
        decision,
        summary,
        high_issues,
        medium_issues,
        low_issues,
        nits
    )


def _extract_first_json_object(text: str) -> Optional[str]:
    in_string = False
    escaped = False
    depth = 0
    start_idx: Optional[int] = None

    for idx, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "\"":
                in_string = False
            continue

        if ch == "\"":
            in_string = True
            continue

        if ch == "{":
            if depth == 0:
                start_idx = idx
            depth += 1
            continue

        if ch == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start_idx is not None:
                return text[start_idx:idx + 1]

    return None


def _extract_json_review_payload(review_text: str) -> Optional[dict]:
    candidates: list[str] = []
    stripped = review_text.strip()
    if stripped:
        candidates.append(stripped)

    fenced_blocks = re.findall(r"```(?:json)?\s*([\s\S]*?)```", review_text, flags=re.IGNORECASE)
    for block in fenced_blocks:
        block = block.strip()
        if block:
            candidates.append(block)

    inline_obj = _extract_first_json_object(review_text)
    if inline_obj:
        candidates.append(inline_obj.strip())

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    return None


class ReviewCommand(Command):
    @staticmethod
    def execute(args: list[str], repo_root: Path, config: SherpaConfig):
        # Local import to avoid circular import at module initialization time.
        from sherpa.review_store import save_review

        commit_message = extract_commit_message(args)
        branch_mode = "--branch" in args
        modified_files: Optional[str] = None
        git_diff: Optional[str] = None

        if branch_mode:
            modified_files, git_diff, base_branch = get_branch_changes(repo_root)
            if not base_branch:
                print("[sherpa] Could not find main/master base branch, exiting...")
                return
            print(f"[sherpa] Reviewing current branch changes against {base_branch}")
        else:
            print("[sherpa] Reviewing staged changes")
            modified_files, git_diff = get_staged_changes(repo_root)

        if not modified_files and not git_diff:
            if branch_mode:
                print("[sherpa] No changes found between current branch and base branch, exiting...")
            else:
                print("[sherpa] It seems you don't have any staged changes, exiting...")
            return

        review_result, total_cost = asyncio.run(
            ReviewCommand.review(
                repo_root,
                commit_message,
                modified_files,
                git_diff,
                config.default_model,
                config.default_reasoning_effort,
            )
        )
        if isinstance(review_result, ReviewResult):
            save_review(repo_root, commit_message, modified_files, git_diff, review_result, None)
        else:
            raw = str(review_result) if review_result is not None else None
            save_review(repo_root, commit_message, modified_files, git_diff, None, raw)

        review_result_decision = ""
        if isinstance(review_result, ReviewResult):
            review_result_decision = review_result.decision

        if review_result_decision == "APPROVE":
            print("[sherpa] The review was approved !")
            print("\n[sherpa] Showing review output....")
            render_review_report(review_result)
        elif review_result_decision == "BLOCKED":
            render_review_report(review_result)
            print("[sherpa] The review is blocked, sorry :/ !")
        else:
            print("[sherpa] Review decision unrecognized, no decision will be taken...")
            print("[sherpa] Showing raw result:")
            print(review_result)

        print(f"[sherpa] Total cost of your review: {total_cost}$")
        print()

    @staticmethod
    async def review(
        repo_root: Path,
        commit_message: str,
        modified_files: str,
        git_diff: str,
        model: str,
        reasoning_effort: str,
    ):
        prompt = get_review_prompt(repo_root, modified_files, git_diff)
        options = AgentOptions(
            cwd=repo_root,
            model=model,
            allowed_tools=["Read", "Glob", "Grep"],
            instructions=(
                "You are an expert pull-request reviewer. Your job is to protect correctness and production "
                "safety while avoiding noise. Start from the provided diff and review changed hunks first. "
                "Only read full files when the diff context is insufficient, and inspect additional files only "
                "when needed to verify behavior, dependencies, "
                "or test coverage. Prioritize findings by user impact and likelihood: report only concrete "
                "issues that can cause malfunction, regression, security exposure, data corruption/loss, or "
                "maintainability risk with clear downstream impact. Do not speculate; if evidence is too weak, "
                "omit the issue. For each reported issue, tie the claim to specific code behavior and provide "
                "a practical fix. Keep the review concise, high-signal, and strictly compliant with the "
                "requested JSON schema."
            ),
            reasoning_effort=reasoning_effort,
            reasoning_summary="auto",
            max_turns=50
        )
        review = ""
        total_cost_usd: Optional[float] = None
        read_count = 0
        thinking_count = 0
        started_at = time.monotonic()
        status_line_len = 0
        elapsed_line_len = 0
        status_started = False
        current_status: Optional[str] = None
        current_count = 0

        def update_status(status: str, count: int):
            nonlocal status_line_len, elapsed_line_len, status_started
            nonlocal current_status, current_count
            current_status = status
            current_count = count
            elapsed = int(time.monotonic() - started_at)
            status_line = f"{status} ({count})"
            elapsed_line = f"Elapsed: {elapsed}s"

            padded_status_line = status_line
            if len(status_line) < status_line_len:
                padded_status_line = status_line + (" " * (status_line_len - len(status_line)))

            padded_elapsed_line = elapsed_line
            if len(elapsed_line) < elapsed_line_len:
                padded_elapsed_line = elapsed_line + (" " * (elapsed_line_len - len(elapsed_line)))

            if not status_started:
                print()
                print(padded_status_line)
                print(padded_elapsed_line)
                status_started = True
            else:
                # Move back to the two status lines and rewrite them in place.
                print("\033[2F", end="")
                print(f"\r{padded_status_line}")
                print(f"\r{padded_elapsed_line}")

            status_line_len = len(status_line)
            elapsed_line_len = len(elapsed_line)

        async def refresh_elapsed_time():
            while True:
                await asyncio.sleep(1)
                if current_status is not None:
                    update_status(current_status, current_count)

        ticker_task = asyncio.create_task(refresh_elapsed_time())

        # Where the review in fact happens
        try:
            async for message in query(prompt=prompt, options=options):
                # This is only UI stuff
                if isinstance(message, AgentToolCall) and message.name in ["read_file", "grep_files", "Read", "Grep"]:
                    read_count += 1
                    update_status("⏳ Reading files", read_count)

                elif isinstance(message, AgentThinking):
                    if message.text and message.text.strip():
                        thinking_count += 1
                        update_status("🧠 Thinking", thinking_count)

                # This is actually useful
                if isinstance(message, AgentText):
                    review += message.text + "\n"
                elif isinstance(message, AgentQueryCompleted):
                    # TODO: give a second look to that when working on agnos
                    # OpenAI may only populate final assistant text on completion.
                    if not review.strip() and isinstance(message.message, str) and message.message.strip():
                        review = message.message.strip()
                    raw_cost = message.total_cost_usd
                    if isinstance(raw_cost, int | float):
                        total_cost_usd = float(raw_cost)
        finally:
            ticker_task.cancel()
            try:
                await ticker_task
            except asyncio.CancelledError:
                pass

        # The agent will give an answer like:
        # ```json
        # {}
        # ```
        # The below code convert that review into a real review

        json_review = _extract_json_review_payload(review)
        if json_review is not None:
            review_result = extract_review_result(json_review)
            if review_result is None:
                return json.dumps(json_review), total_cost_usd
            return review_result, total_cost_usd

        print("[sherpa] Model did not respond with valid JSON. Will show raw output")
        return review.strip(), total_cost_usd

