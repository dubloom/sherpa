import sys
from pathlib import Path

from sherpa.commands.base import Command
from sherpa.config import SherpaConfig
from sherpa.hooks import is_valid_hook_name, scaffold_hook


class HookCommand(Command):
    @staticmethod
    def execute(args: list[str], repo_root: Path, _config: SherpaConfig) -> int:
        if len(args) != 2 or args[0] != "add":
            print(
                "Error: expected `sherpa hook add <name>`.",
                file=sys.stderr,
            )
            return 1

        hook_name = args[1].strip()
        if not is_valid_hook_name(hook_name):
            print(
                "Error: invalid hook name. Use letters, numbers, '-' or '_', "
                "and start with a letter or number.",
                file=sys.stderr,
            )
            return 1

        try:
            scaffold = scaffold_hook(repo_root, hook_name)
        except FileExistsError:
            print(f"Error: hook {hook_name!r} already exists.", file=sys.stderr)
            return 1
        except OSError as exc:
            print(f"Error: failed to create hook files: {exc}", file=sys.stderr)
            return 1

        print(f"[sherpa] Added hook '{hook_name}'.")
        print(f"[sherpa] Workflow: {scaffold.workflow_path}")
        return 0
