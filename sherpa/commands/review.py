from dataclasses import dataclass
import json
from pathlib import Path
from typing import Literal, Optional
from agnos import AgentOptions, AgentQueryCompleted, AgentText, query
from sherpa.commands.base import Command
from sherpa.git import get_git_repo_root
from sherpa.prompts.review import get_review_prompt
from sherpa.commands.review_report import render_review_report


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
    def execute(args: list[str], model: str):
        print("[sherpa] Reviewing staged changes")

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
                raw_cost = message.extra.get("total_cost_usd")
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

