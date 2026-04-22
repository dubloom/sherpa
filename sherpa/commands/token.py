import getpass
import sys
from pathlib import Path
from typing import Optional

from sherpa.commands.base import Command
from sherpa.config import SherpaConfig
from sherpa.config.tokens import TOKEN_PATH, SherpaTokens, load_tokens, save_tokens
from sherpa.config.ui import is_interactive_session

VALID_PROVIDERS = ("openai", "anthropic", "github")


def _status_label(token: Optional[str]) -> str:
    return "set" if token else "not set"


def _prompt_token(label: str, current_value: Optional[str]) -> Optional[str]:
    prompt = (
        f"{label} [{_status_label(current_value)}] "
        "(leave blank to clear): "
    )
    raw = getpass.getpass(prompt).strip()
    return raw or None


class TokenCommand(Command):
    @staticmethod
    def execute(args: list[str], _repo_root: Path, _config: SherpaConfig) -> int:
        if not is_interactive_session():
            print("Error: `sherpa token` requires an interactive terminal.", file=sys.stderr)
            return 1

        selected_providers = [arg.strip().lower() for arg in args if arg.strip()]
        if selected_providers:
            invalid = [provider for provider in selected_providers if provider not in VALID_PROVIDERS]
            if invalid:
                supported = ", ".join(VALID_PROVIDERS)
                print(
                    f"Error: unsupported provider(s): {', '.join(invalid)}. "
                    f"Supported providers: {supported}.",
                    file=sys.stderr,
                )
                return 1

            # Keep order stable while de-duplicating.
            selected_providers = list(dict.fromkeys(selected_providers))
        else:
            selected_providers = list(VALID_PROVIDERS)

        try:
            existing_tokens = load_tokens()
        except OSError as exc:
            print(f"Error: failed to read stored tokens: {exc}.", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"Error: failed to parse stored tokens: {exc}.", file=sys.stderr)
            return 1
        print("[sherpa] Updating global provider tokens.")
        print("[sherpa] Leave a prompt blank to clear the stored token for that provider.")
        if len(selected_providers) != len(VALID_PROVIDERS):
            print(
                "[sherpa] Updating only: "
                + ", ".join(selected_providers)
                + " (other providers are left unchanged)."
            )

        try:
            updated_tokens = SherpaTokens(
                openai_token=existing_tokens.openai_token,
                anthropic_token=existing_tokens.anthropic_token,
                github_token=existing_tokens.github_token,
            )
            if "openai" in selected_providers:
                updated_tokens.openai_token = _prompt_token("OpenAI token", existing_tokens.openai_token)
            if "anthropic" in selected_providers:
                updated_tokens.anthropic_token = _prompt_token("Anthropic token", existing_tokens.anthropic_token)
            if "github" in selected_providers:
                updated_tokens.github_token = _prompt_token("GitHub token", existing_tokens.github_token)
        except KeyboardInterrupt:
            print("\nToken update cancelled.", file=sys.stderr)
            return 1

        try:
            save_tokens(updated_tokens)
        except OSError as exc:
            print(f"Error: failed to save stored tokens: {exc}.", file=sys.stderr)
            return 1

        print("[sherpa] Global tokens updated.")
        print(f"[sherpa] OpenAI: {_status_label(updated_tokens.openai_token)}")
        print(f"[sherpa] Anthropic: {_status_label(updated_tokens.anthropic_token)}")
        print(f"[sherpa] GitHub: {_status_label(updated_tokens.github_token)}")
        print("[sherpa] Environment variables still take precedence over stored tokens.")
        print("[sherpa] Tip: update one provider with `sherpa token openai|anthropic|github`.")
        print(f"[sherpa] Stored in: {TOKEN_PATH}")
        return 0
