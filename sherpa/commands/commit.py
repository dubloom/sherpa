import asyncio
from pathlib import Path
import re
import subprocess
from sherpa.commands.base import Command
from sherpa.commands.review import ReviewCommand, ReviewResult
from sherpa.commands.review.report import render_review_report
from sherpa.config import SherpaConfig
from sherpa.git import get_staged_changes
from sherpa.review_store import save_review
from sherpa.utils import extract_commit_message


def _append_approval_line(message: str, approval_line: str) -> str:
    normalized = message.rstrip()
    if not normalized:
        return approval_line
    if approval_line in normalized.splitlines():
        return normalized
    return f"{normalized}\n\n{approval_line}"


def _build_approved_commit_args(args: list[str], model_name: str) -> list[str]:
    approval_line = f"Approved-By: {model_name}"
    updated_args = list(args)
    message_targets: list[tuple[str, int]] = []
    i = 0
    while i < len(updated_args):
        arg = updated_args[i]
        if arg == "--":
            break
        if arg == "-m":
            if i + 1 < len(updated_args):
                message_targets.append(("value", i + 1))
            i += 2
            continue
        if arg == "--message":
            if i + 1 < len(updated_args):
                message_targets.append(("value", i + 1))
            i += 2
            continue
        if arg.startswith("--message="):
            message_targets.append(("inline", i))
            i += 1
            continue
        i += 1

    if message_targets:
        target_type, target_index = message_targets[-1]
        if target_type == "value":
            updated_args[target_index] = _append_approval_line(updated_args[target_index], approval_line)
        else:
            prefix, value = updated_args[target_index].split("=", maxsplit=1)
            updated_args[target_index] = f"{prefix}={_append_approval_line(value, approval_line)}"
        return updated_args

    # No explicit message argument: ask git to add a commit trailer.
    # Insert before `--` (end of options) so trailer is not parsed as pathspec.
    if "--" in updated_args:
        separator_index = updated_args.index("--")
        updated_args[separator_index:separator_index] = ["--trailer", approval_line]
    else:
        updated_args.extend(["--trailer", approval_line])
    return updated_args

class CommitCommand(Command):
    @staticmethod
    def execute(args: list[str], repo_root: Path, config: SherpaConfig):
        print("[sherpa] Reviewing staged changes")

        commit_message = extract_commit_message(args)
        modified_files, git_diff = get_staged_changes(repo_root)
        if not modified_files and not git_diff:
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
        else:
            raw_review = str(review_result or "")
            match = re.search(r"^\s*decision\s*:\s*([A-Za-z_]+)", raw_review, re.IGNORECASE | re.MULTILINE)
            if match:
                review_result_decision = match.group(1).strip().upper()

        if review_result_decision == "APPROVE":
            print("[sherpa] The commit was approved !")
            approved_commit_args = _build_approved_commit_args(args, config.default_model)
            subprocess.run(["git", "commit", *approved_commit_args], cwd=repo_root)
            print("\n[sherpa] Showing review output....")
            render_review_report(review_result)
        elif review_result_decision == "BLOCKED":
            render_review_report(review_result)
            print("[sherpa] The commit will be blocked, sorry :/ !")
        else:
            print("[sherpa] Review decision unrecognized, no decision will be taken...")
            print("[sherpa] Showing raw result:")
            print(review_result)

        print(f"[sherpa] Total cost of your review: {total_cost}$")
        print()