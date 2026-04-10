import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Literal, Optional
from agnos import AgentOptions, AgentQueryCompleted, AgentText, query
from sherpa.commands.base import Command
from sherpa.commands.review.report import render_review_report
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


class ReviewCommand(Command):
    @staticmethod
    def execute(args: list[str], repo_root: Path, model: str):
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
            ReviewCommand.review(repo_root, commit_message, modified_files, git_diff, model)
        )
        if isinstance(review_result, ReviewResult):
            save_review(repo_root, commit_message, modified_files, git_diff, review_result, None)
        else:
            raw = str(review_result) if review_result is not None else None
            save_review(repo_root, commit_message, modified_files, git_diff, None, raw)

        review_result_decision = ""
        if isinstance(review_result, ReviewResult):
            review_result_decision = review_result.decision
        else:
            raw_review = str(review_result or "")
            match = re.search(
                r"^\s*decision\s*:\s*([A-Za-z_]+)",
                raw_review,
                re.IGNORECASE | re.MULTILINE
            )
            if match:
                review_result_decision = match.group(1).strip().upper()

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
    ):
        prompt = get_review_prompt(repo_root, modified_files, git_diff)
        options = AgentOptions(
            cwd=repo_root,
            model=model,
            allowed_tools=["Read", "Glob", "Grep"],
            instructions=(
                "Before producing the review, inspect potentially impacted files "
                "(for example nearby callers, shared helpers, and related tests) "
                "to validate behavioral impact and context. "
                "Keep exploration bounded: inspect at most 5 additional files and "
                "use at most 12 tool calls before returning the best possible JSON."
            ),
            max_turns=25
        )

        review = ""
        total_cost_usd: Optional[float] = None

        # Where the review in fact happens
        async for message in query(prompt=prompt, options=options):
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

        # The agent will give an answer like:
        # ```json
        # {}
        # ```
        # The below code convert that review into a real review

        review = review.strip().removeprefix("```json").removesuffix("```").strip()
        try:
            json_review = json.loads(review)
            review_result = extract_review_result(json_review)
            if review_result is None:
                return json.dumps(json_review), total_cost_usd
            return review_result, total_cost_usd
        except json.JSONDecodeError:
            print("[sherpa] Model did not respond with valid JSON. Will show raw output")
            return review, total_cost_usd

