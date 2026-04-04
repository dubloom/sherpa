import os
from sherpa.commands.base import Command

github_headers = {}

def build_github_headers() -> None:
    global github_headers

    git_token = os.get_env("GITHUB_TOKEN")

    if not git_token:
        raise RuntimeError(f"[sherpa] Missing GITHUB_TOKEN, address command cannot run")

    github_headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {git_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


class AddressCommand(Command):
    @staticmethod
    def execute(args: list[str], model: str):
        build_github_headers()

        # The user provided a github PR link
        if len(args) > 0:
            pass
        else:
            # No link provided, infer PR link based on the
            # repo and the branch
            pass



