import sys

from typing import Optional

from sherpa.config import SUPPORTED_REASONING_EFFORTS
from sherpa.supported_models import SUPPORTED_MODEL


def extract_model_flag() -> tuple[list[str], Optional[str], Optional[str]]:
    """ CLI args will be directly passed to git commit command
    This function is used to extract the model flag that can be passed
    and should not be used for git commit. """

    model: Optional[str] = None
    sherpa_args: list[str] = []

    cli_args = sys.argv[1:]
    nb_of_args = len(cli_args)
    i = 0
    while i < nb_of_args:
        if cli_args[i] == "--model":
            if i + 1 >= nb_of_args:
                return [], None, "No model was provided after --model flag"
            model = cli_args[i + 1]
            if not any(model == supported_model for supported_model in SUPPORTED_MODEL):
                return [], None, f"Model {model} is not supported"
            i += 2
        else:
            sherpa_args.append(cli_args[i])
            i += 1

    return sherpa_args, model, None


def extract_reasoning_flag(args: list[str]) -> tuple[list[str], Optional[str], Optional[str]]:
    """Extract --reasoning from args and validate supported values."""
    reasoning_effort: Optional[str] = None
    sherpa_args: list[str] = []

    nb_of_args = len(args)
    i = 0
    while i < nb_of_args:
        if args[i] == "--reasoning":
            if i + 1 >= nb_of_args:
                return [], None, "No reasoning effort was provided after --reasoning flag"
            reasoning_effort = str(args[i + 1]).strip().lower()
            if reasoning_effort not in SUPPORTED_REASONING_EFFORTS:
                supported = ", ".join(SUPPORTED_REASONING_EFFORTS)
                return [], None, (
                    f"Reasoning effort {reasoning_effort} is not supported. "
                    f"Use one of: {supported}"
                )
            i += 2
        else:
            sherpa_args.append(args[i])
            i += 1

    return sherpa_args, reasoning_effort, None


def extract_commit_message(args: list[str]) -> Optional[str]:
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "-m" or arg == "--message":
            if i + 1 >= len(args):
                return None
            return str(args[i + 1]).strip()
        if arg.startswith("--message="):
            _, value = arg.split("=", maxsplit=1)
            return value.strip()
        i += 1

# Shared retry/instruction helpers for fix flows
AUTO_RETRY_NO_CHANGE_INSTRUCTION = (
    "Your previous attempt produced no filesystem changes. Retry now by applying real "
    "edits with write/edit tools. Do not return a hypothetical patch. If no code change "
    "is needed, explicitly say that and do not claim completion."
)

RETRY_AFTER_RESET_INSTRUCTION = (
    "Your previous attempt has been discarded and all file changes were reverted to baseline. "
    "Do not assume prior edits still exist. First re-read relevant files from disk with Read/Glob/Grep, "
    "then re-apply the fix from scratch in this workspace. Prioritize the latest user refinement instruction."
)


def merge_instruction(current: Optional[str], new_instruction: Optional[str]) -> Optional[str]:
    """Merge a new instruction into an existing instruction for fix retry flows."""
    if new_instruction is None:
        return current
    if current is None:
        return new_instruction
    return (
        f"{current}\n\n"
        "Additional user instruction for the next attempt:\n"
        f"{new_instruction}"
    )
