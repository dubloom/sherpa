import sys
from dataclasses import replace

from sherpa.commands import Commands
from sherpa.commands.address import AddressCommand
from sherpa.commands.commit import CommitCommand
from sherpa.commands.config import ConfigCommand
from sherpa.commands.fix import FixCommand
from sherpa.commands.hook import HookCommand
from sherpa.commands.review import ReviewCommand
from sherpa.commands.token import TokenCommand
from sherpa.config import config_path, load_or_create_config
from sherpa.config.tokens import apply_stored_token_env_defaults
from sherpa.config.ui import is_interactive_session, prompt_new_config
from sherpa.git import SherpaGitExcludeStatus, ensure_sherpa_git_exclude, get_git_repo_root, in_git_repo
from sherpa.utils import extract_model_flag, extract_reasoning_flag


def print_global_help() -> None:
    """Print top-level usage (stdout), for -h / --help / help."""
    print(
        """\
Usage:
  sherpa [--model MODEL] [--reasoning low|medium|high] <command> [arguments...]

Commands:
  commit    Review staged changes, then run git commit
  review    Run a standalone AI review on your changes
  fix       Apply fixes for issues from the latest review
  address   Browse GitHub PR threads and run fix flows
  hook      Manage commit hooks (pre-review)
  config    Edit Sherpa configuration for this repo
  token     Store provider API tokens (works outside a git repository)

Global options:
  --model MODEL              Use this model for the invocation (see supported models in docs)
  --reasoning LEVEL          OpenAI only: low | medium | high
  -h, --help                 Show this message
  help                       Same as --help

Most commands must be run inside a git repository. Exceptions: token, and this help.

Documentation: https://github.com/dubloom/sherpa#readme\
"""
    )


def main():
    sherpa_args, model, model_error = extract_model_flag()
    sherpa_args, reasoning_effort, reasoning_error = extract_reasoning_flag(sherpa_args)

    if model_error:
        print(f"Error extracting model flag: {model_error}", file=sys.stderr)
        return 1
    if reasoning_error:
        print(f"Error: {reasoning_error}", file=sys.stderr)
        return 1
    if not sherpa_args:
        print_global_help()
        return 2

    # This is the only command that can be run anywhere as it is just used to set tokens
    command = sherpa_args[0]
    if command in ("--help", "-h", "help"):
        print_global_help()
        return 0

    if command == Commands.TOKEN.value:
        return TokenCommand.execute(sherpa_args[1:], None, None)

    if not in_git_repo():
        print("Error: sherpa must be run inside a git repo", file=sys.stderr)
        return 1

    git_repo_root = get_git_repo_root()
    if command == Commands.CONFIG.value:
        return ConfigCommand.execute(sherpa_args[1:], git_repo_root, None)

    apply_stored_token_env_defaults()

    config_already_exists = config_path(git_repo_root).is_file()
    try:
        config = load_or_create_config(
            git_repo_root,
            on_missing_config=prompt_new_config if is_interactive_session() else None,
        )
    except KeyboardInterrupt:
        print("Setup cancelled.", file=sys.stderr)
        return 1

    if not config_already_exists:
        sherpa_is_excluded_status = ensure_sherpa_git_exclude(git_repo_root)
        if sherpa_is_excluded_status == SherpaGitExcludeStatus.ADDED:
            print("[sherpa] Added '.sherpa' folder (including review results) to .git/info/exclude.", file=sys.stderr)
        elif sherpa_is_excluded_status == SherpaGitExcludeStatus.ERROR:
            print(
                "Tip: add '.sherpa' to .git/info/exclude to avoid accidentally committing local Sherpa files.",
                file=sys.stderr,
            )

    if model:
        config = replace(config, default_model=model)
    if reasoning_effort:
        config = replace(config, default_reasoning_effort=reasoning_effort)
    match command:
        case Commands.COMMIT.value:
            CommitCommand.execute(sherpa_args[1:], git_repo_root, config)
        case Commands.FIX.value:
            FixCommand.execute(sherpa_args[1:], git_repo_root, config)
        case Commands.ADDRESS.value:
            AddressCommand.execute(sherpa_args[1:], git_repo_root, config)
        case Commands.REVIEW.value:
            ReviewCommand.execute(sherpa_args[1:], git_repo_root, config)
        case Commands.HOOK.value:
            return HookCommand.execute(sherpa_args[1:], git_repo_root, config)
        case _:
            print(f"Error: unrecognized {command} command", file=sys.stderr)
            return 1

    return 0

if __name__ == "__main__":
    raise SystemExit(main())