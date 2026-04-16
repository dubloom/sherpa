import sys
from dataclasses import replace

from sherpa.commands import Commands
from sherpa.commands.address import AddressCommand
from sherpa.commands.commit import CommitCommand
from sherpa.commands.fix import FixCommand
from sherpa.commands.review import ReviewCommand
from sherpa.config import load_or_create_config
from sherpa.git import get_git_repo_root, in_git_repo
from sherpa.utils import extract_model_flag, extract_reasoning_flag

def main():
    if not in_git_repo():
        print("Error: sherpa must be run inside a git repo", file=sys.stderr)
        return 1

    git_repo_root = get_git_repo_root()
    config = load_or_create_config(git_repo_root)

    sherpa_args, model, model_error = extract_model_flag()
    sherpa_args, reasoning_effort, reasoning_error = extract_reasoning_flag(sherpa_args)

    if model_error:
        print(f"Error extracting model flag: {model_error}", file=sys.stderr)
        return 1
    if reasoning_error:
        print(f"Error: {reasoning_error}", file=sys.stderr)
        return 1
    if not sherpa_args:
        print("Error extracing reasoning : no provided args", file=sys.stderr)
        return 1

    if model:
        config = replace(config, default_model=model)
    if reasoning_effort:
        config = replace(config, default_reasoning_effort=reasoning_effort)

    command = sherpa_args[0]
    match command:
        case Commands.COMMIT.value:
            CommitCommand.execute(sherpa_args[1:], git_repo_root, config)
        case Commands.FIX.value:
            FixCommand.execute(sherpa_args[1:], git_repo_root, config)
        case Commands.ADDRESS.value:
            AddressCommand.execute(sherpa_args[1:], git_repo_root, config)
        case Commands.REVIEW.value:
            ReviewCommand.execute(sherpa_args[1:], git_repo_root, config)
        case _:
            print(f"Error: unrecognized {command} command", file=sys.stderr)
            return 1

    return 0

if __name__ == "__main__":
    raise SystemExit(main())