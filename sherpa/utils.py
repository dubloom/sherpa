import sys

from typing import Optional

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

def extract_commit_message(args: list[str]) -> Optional[str]:
    for i in range(len(args)):
        if args[i] == "-m":
            if i+1 >= len(args):
                return None
            else:
                return str(args[i+1]).strip().replace(" ", "_")
