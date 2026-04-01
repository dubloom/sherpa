from pathlib import Path
import subprocess
from typing import Optional

def in_git_repo():
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0

def get_git_repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError("The root of the git repo could not be identified")
    return Path(result.stdout.strip())

def execute_git_command(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *command],
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
    )

def get_staged_changes(root: Path) -> tuple[Optional[str], [str]]:
    """ Return the git diff and the list of modified files """
    modified_files: Optional[str] = None
    diff: Optional[str] = None

    modified_files_result = execute_git_command(["diff", "--cached", "--name-only"], cwd=root)
    diff_result = execute_git_command(
        [
            "diff",
            "--cached",
            "--patch",
            "--no-color",
            "--no-ext-diff",
            "--minimal",
        ],
        cwd=root,
    )

    if modified_files_result.returncode == 0:
        modified_files = modified_files_result.stdout.strip()
    else:
        print("[sherpa][warning] Could not retrieve the list of modified files")

    if diff_result.returncode == 0:
        diff = diff_result.stdout
    else:
        print("[sherpa][warning] Could not retrieve git diff")

    return modified_files, diff