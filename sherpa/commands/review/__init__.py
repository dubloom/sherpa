import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
import time
from typing import Literal, Optional
from glyph import AgentOptions, AgentQueryCompleted, AgentText, AgentToolCall, query
from glyph.messages import AgentThinking
from sherpa.commands.base import Command
from sherpa.commands.review.report import render_review_report
from sherpa.config import SherpaConfig
from sherpa.git import get_branch_changes, get_commit_changes, get_staged_changes
from sherpa.prompts.review import get_review_prompt


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


def _print_review_usage() -> None:
    print("[sherpa] Usage:")
    print("[sherpa]   sherpa review")
    print("[sherpa]   sherpa review --branch")
    print("[sherpa]   sherpa review --last")
    print("[sherpa]   sherpa review <commit>")


def _parse_review_target(
    args: list[str],
) -> tuple[Optional[Literal["staged", "branch", "commit"]], Optional[str], Optional[str]]:
    branch_mode = "--branch" in args
    last_mode = "--last" in args
    remaining_args = [arg for arg in args if arg not in {"--branch", "--last"}]
    unknown_flags = [arg for arg in remaining_args if arg.startswith("-")]

    if unknown_flags:
        return None, None, f"Unrecognized option(s): {' '.join(unknown_flags)}"
    if branch_mode and last_mode:
        return None, None, "Flags --branch and --last cannot be combined."
    if branch_mode and remaining_args:
        return None, None, "review <commit> cannot be combined with --branch."
    if last_mode and remaining_args:
        return None, None, "--last cannot be combined with review <commit>."
    if len(remaining_args) > 1:
        return None, None, "review accepts at most one commit reference."

    if branch_mode:
        return "branch", None, None
    if last_mode:
        return "commit", "HEAD", None
    if remaining_args:
        return "commit", remaining_args[0], None
    return "staged", None, None


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

        mode, commit_ref, parse_error = _parse_review_target(args)
        if parse_error:
            print(f"[sherpa] {parse_error}")
            _print_review_usage()
            return

        commit_message: Optional[str] = None
        modified_files: Optional[str] = None
        git_diff: Optional[str] = None

        if mode == "branch":
            modified_files, git_diff, base_branch = get_branch_changes(repo_root)
            if not base_branch:
                print("[sherpa] Could not find main/master base branch, exiting...")
                return
            print(f"[sherpa] Reviewing current branch changes against {base_branch}")
        elif mode == "commit":
            if not commit_ref:
                print("[sherpa] Missing commit reference, exiting...")
                _print_review_usage()
                return

            modified_files, git_diff, resolved_commit, commit_subject = get_commit_changes(repo_root, commit_ref)
            if not resolved_commit:
                print(f"[sherpa] Could not resolve commit '{commit_ref}', exiting...")
                return

            commit_message = commit_subject or resolved_commit
            if commit_ref == "HEAD":
                print(f"[sherpa] Reviewing latest commit ({resolved_commit})")
            else:
                print(f"[sherpa] Reviewing commit {resolved_commit}")
        else:
            print("[sherpa] Reviewing staged changes")
            modified_files, git_diff = get_staged_changes(repo_root)

        if not modified_files and not git_diff:
            if mode == "branch":
                print("[sherpa] No changes found between current branch and base branch, exiting...")
            elif mode == "commit":
                print("[sherpa] No changes found in selected commit, exiting...")
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
        issue_count = 0
        if isinstance(review_result, ReviewResult):
            review_result_decision = review_result.decision
            issue_count = (
                len(review_result.high_issues)
                + len(review_result.medium_issues)
                + len(review_result.low_issues)
                + len(review_result.nits)
            )

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

        if issue_count > 0:
            print("[sherpa] To address these issues, run: sherpa fix")

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
        started_at = time.monotonic()
        last_elapsed_logged = -1
        elapsed_visible = False
        interactive_output = sys.stdout.isatty()

        def normalize_status_detail(value: object) -> Optional[str]:
            if value is None:
                return None

            if isinstance(value, Path):
                detail = str(value)
            elif isinstance(value, str):
                detail = value.strip()
                if not detail:
                    return None
                if detail.startswith("{") or detail.startswith("["):
                    parsed: Optional[object] = None
                    try:
                        parsed = json.loads(detail)
                    except json.JSONDecodeError:
                        parsed = None
                    if parsed is not None:
                        return normalize_status_detail(parsed)
            elif isinstance(value, dict):
                for key in ["path", "file_path", "pattern", "query", "glob", "paths"]:
                    if key in value:
                        return normalize_status_detail(value[key])
                detail = json.dumps(value, ensure_ascii=True, sort_keys=True)
            elif isinstance(value, (list, tuple)):
                parts = [normalize_status_detail(item) for item in value]
                detail = ", ".join(part for part in parts if part)
            else:
                detail = str(value).strip()

            detail = " ".join(detail.split())
            if not detail:
                return None
            if len(detail) > 80:
                return detail[:77] + "..."
            return detail

        def get_tool_call_detail(message: AgentToolCall, *keys: str) -> Optional[str]:
            for attr in ["arguments", "args", "input", "tool_input", "payload"]:
                raw_value = getattr(message, attr, None)
                if raw_value is None:
                    continue

                if keys and isinstance(raw_value, dict):
                    for key in keys:
                        if key in raw_value:
                            detail = normalize_status_detail(raw_value[key])
                            if detail:
                                return detail

                detail = normalize_status_detail(raw_value)
                if detail:
                    return detail

            for key in keys:
                detail = normalize_status_detail(getattr(message, key, None))
                if detail:
                    return detail

            return None

        def get_tool_call_status(message: AgentToolCall) -> str:
            tool_name = str(message.name).strip() or "Tool"
            normalized_tool_name = tool_name.lower()

            if normalized_tool_name in ["read", "read_file"]:
                detail = get_tool_call_detail(message, "path", "file_path")
                return f"Reading {detail}" if detail else "Reading"

            if normalized_tool_name in ["grep", "grep_files"]:
                detail = get_tool_call_detail(message, "pattern", "query")
                return f"Grep {detail}" if detail else "Grep"

            if normalized_tool_name == "glob":
                detail = get_tool_call_detail(message, "pattern", "glob")
                return f"Glob {detail}" if detail else "Glob"

            detail = get_tool_call_detail(message, "path", "file_path", "pattern", "query", "glob")
            return f"{tool_name} {detail}" if detail else tool_name

        def log_status(status: str):
            nonlocal elapsed_visible
            if not interactive_output:
                print(f"[sherpa-agent] {status}", flush=True)
                return
            if elapsed_visible:
                sys.stdout.write("\r\033[2K")
                sys.stdout.flush()
            print(f"\033[2m[sherpa-agent] {status}\033[0m", flush=True)
            if elapsed_visible:
                log_elapsed(force=True)

        def write_elapsed(elapsed: int):
            nonlocal elapsed_visible
            if not interactive_output:
                print(f"[sherpa-agent] Elapsed: {elapsed}s", flush=True)
                elapsed_visible = True
                return
            sys.stdout.write(f"\r\033[2K\033[2m[sherpa-agent] Elapsed: {elapsed}s\033[0m")
            sys.stdout.flush()
            elapsed_visible = True

        def log_elapsed(force: bool = False):
            nonlocal last_elapsed_logged
            elapsed = int(time.monotonic() - started_at)
            if not force and elapsed == last_elapsed_logged:
                return
            last_elapsed_logged = elapsed
            write_elapsed(elapsed)

        def finalize_elapsed():
            if not interactive_output:
                if not elapsed_visible:
                    log_elapsed(force=True)
                return
            if not elapsed_visible:
                log_elapsed(force=True)
            sys.stdout.write("\n")
            sys.stdout.flush()

        async def refresh_elapsed_time():
            try:
                while True:
                    await asyncio.sleep(1)
                    log_elapsed()
            except asyncio.CancelledError:
                return

        ticker_task = asyncio.create_task(refresh_elapsed_time())

        # Where the review in fact happens
        try:
            async for message in query(prompt=prompt, options=options):
                # This is only UI stuff
                if isinstance(message, AgentToolCall):
                    log_status(get_tool_call_status(message))

                elif isinstance(message, AgentThinking):
                    if message.text and message.text.strip():
                        log_status("Thinking")

                # This is actually useful
                if isinstance(message, AgentText):
                    review += message.text + "\n"
                elif isinstance(message, AgentQueryCompleted):
                    # TODO: give a second look to that when working on glyph
                    # OpenAI may only populate final assistant text on completion.
                    if not review.strip() and isinstance(message.message, str) and message.message.strip():
                        review = message.message.strip()
                    raw_cost = message.total_cost_usd
                    if isinstance(raw_cost, int | float):
                        total_cost_usd = float(raw_cost)
        finally:
            ticker_task.cancel()
            await ticker_task
            log_elapsed(force=True)
            finalize_elapsed()

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

