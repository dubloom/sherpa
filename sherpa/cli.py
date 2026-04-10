import sys

from sherpa.commands import Commands
from sherpa.commands.address import AddressCommand
from sherpa.commands.commit import CommitCommand
from sherpa.commands.fix import FixCommand
from sherpa.commands.review import ReviewCommand
from sherpa.git import get_git_repo_root, in_git_repo
from sherpa.utils import extract_model_flag
from sherpa.supported_models import DEFAULT_MODEL

def main():
    if not in_git_repo():
        print("Error: sherpa must be run inside a git repo", file=sys.stderr)
        return 1

    sherpa_args, model, error = extract_model_flag()
    if not model:
        model = DEFAULT_MODEL
    if error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    if not sherpa_args:
        print("Error: no provided args", file=sys.stderr)
        return 1

    git_repo_root = get_git_repo_root()
    command = sherpa_args[0]
    match command:
        case Commands.COMMIT.value:
            CommitCommand.execute(sherpa_args[1:], git_repo_root, model)
        case Commands.FIX.value:
            FixCommand.execute(sherpa_args[1:], git_repo_root, model)
        case Commands.ADDRESS.value:
            AddressCommand.execute(sherpa_args[1:], git_repo_root, model)
        case Commands.REVIEW.value:
            ReviewCommand.execute(sherpa_args[1:], git_repo_root, model)
        case _:
            print(f"Error: unrecognized {command} command", file=sys.stderr)
            return 1

    return 0

if __name__ == "__main__":
    raise SystemExit(main())