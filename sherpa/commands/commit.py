import asyncio
from sherpa.commands.base import Command
from sherpa.commands.review import ReviewCommand
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

        asyncio.run(ReviewCommand.review(root, commit_message, modified_files, git_diff, model))