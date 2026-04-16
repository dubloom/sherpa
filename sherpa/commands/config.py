import sys
from pathlib import Path

from sherpa.commands.base import Command
from sherpa.config import SherpaConfig, config_path, load_or_create_config, save_config
from sherpa.config.ui import is_interactive_session, prompt_new_config, prompt_update_config
from sherpa.git import SherpaGitExcludeStatus, ensure_sherpa_git_exclude


class ConfigCommand(Command):
    @staticmethod
    def execute(args: list[str], repo_root: Path, _config: SherpaConfig) -> int:
        if args:
            print("Error: `sherpa config` does not accept positional arguments.", file=sys.stderr)
            return 1
        if not is_interactive_session():
            print("Error: `sherpa config` requires an interactive terminal.", file=sys.stderr)
            return 1

        config_already_exists = config_path(repo_root).is_file()
        try:
            if config_already_exists:
                config = load_or_create_config(repo_root)
                updated_config = prompt_update_config(config)
            else:
                updated_config = prompt_new_config(SherpaConfig())
        except KeyboardInterrupt:
            print("Setup cancelled.", file=sys.stderr)
            return 1

        save_config(repo_root, updated_config)
        if not config_already_exists:
            sherpa_is_excluded_status = ensure_sherpa_git_exclude(repo_root)
            if sherpa_is_excluded_status == SherpaGitExcludeStatus.ADDED:
                print("[sherpa] Added '.sherpa' folder (including review results) to .git/info/exclude.", file=sys.stderr)
            elif sherpa_is_excluded_status == SherpaGitExcludeStatus.ERROR:
                print(
                    "Tip: add '.sherpa' to .git/info/exclude to avoid accidentally committing local Sherpa files.",
                    file=sys.stderr,
                )

        return 0
