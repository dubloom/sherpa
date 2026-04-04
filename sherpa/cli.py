import sys

from sherpa.commands import Commands
from sherpa.commands.address import AddressCommand
from sherpa.commands.commit import CommitCommand
from sherpa.commands.fix import FixCommand
from sherpa.git import in_git_repo
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

    command = sherpa_args[0]
    match command:
        case Commands.COMMIT:
            CommitCommand.execute(sherpa_args[1:], model)
        case Commands.FIX:
            FixCommand.execute(sherpa_args[1:], model)
        case Commands.ADDRESS:
            AddressCommand.execute(sherpa_args[1:], model)
        case Commands.REVIEW:
            pass
        case _:
            print(f"Error: unrecognized {command} command", file=sys.stderr)
            return 1

    return 0

if __name__ == "__main__":
    raise SystemExit(main())