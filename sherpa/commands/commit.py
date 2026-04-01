import asyncio
import re
import subprocess
from sherpa.commands.base import Command
from sherpa.commands.review import ReviewCommand, ReviewResult
from sherpa.commands.review_report import render_review_report
from sherpa.git import get_git_repo_root, get_staged_changes
from sherpa.utils import extract_commit_message

class CommitCommand(Command):
    @staticmethod
    def execute(args: list[str], model: str):
        print("[sherpa] Reviewing staged changes")

        commit_message = extract_commit_message(args)
        root = get_git_repo_root()
        modified_files, git_diff = get_staged_changes(root)
        if not modified_files and not git_diff:
            print("[sherpa] It seems you don't have any staged changes, exiting...")
            return

        review_result, total_cost = asyncio.run(ReviewCommand.review(root, commit_message, modified_files, git_diff, model))
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
            print("[sherpa] Showing review output....")
            render_review_report(review_result)
        elif review_result_decision == "BLOCKED":
            print("[sherpa] The commit will be blocked, sorry :/ !")
        else:
            print("[sherpa] Review decision unrecognized, no decision will be taken...")

        subprocess.run(["git", "commit", *args], cwd=root)
        print(f"[sherpa] Total cost of your review: {total_cost}$")
        print()