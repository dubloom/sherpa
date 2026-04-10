from pathlib import Path
import shutil
import subprocess
import tempfile
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

def execute_git_command(
    command: list[str],
    cwd: Path,
    warning_message: str | None = None
) -> str:
    result = subprocess.run(
        ["git", *command],
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        if warning_message:
            print(f"[sherpa][warning] {warning_message}")
        else:
            raise RuntimeError(result.stderr)
    return result.stdout.strip()

def get_staged_changes(root: Path) -> tuple[Optional[str], [str]]:
    """ Return the git diff and the list of modified files """
    modified_files: Optional[str] = None
    diff: Optional[str] = None

    modified_files= execute_git_command(
        ["diff", "--cached", "--name-only"],
        cwd=root,
        warning_message="Could not retrieve the list of modified files"
    )

    diff = execute_git_command(
        [
            "diff",
            "--cached",
            "--patch",
            "--no-color",
            "--no-ext-diff",
            "--minimal",
        ],
        cwd=root,
        warning_message="Could not retrieve git diff"
    )
    return modified_files, diff


#TODO: Review what is below
def _sanitize_worktree_label(label: str) -> str:
    sanitized = "".join(ch if (ch.isalnum() or ch in ("-", "_")) else "-" for ch in label)
    sanitized = sanitized.strip("-_")
    if not sanitized:
        return "issue"
    return sanitized[:48]


def create_detached_worktree(root: Path, label: str) -> Path:
    safe_label = _sanitize_worktree_label(label)
    worktree_path = Path(tempfile.mkdtemp(prefix=f"sherpa-fix-{safe_label}-"))
    result = subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"],
        cwd=root,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        shutil.rmtree(worktree_path, ignore_errors=True)
        raise RuntimeError(result.stderr.strip() or "Failed to create detached git worktree")
    return worktree_path


def remove_worktree(root: Path, worktree_path: Path) -> None:
    result = subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        cwd=root,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        print(
            f"[sherpa][warning] Could not remove worktree {worktree_path}: "
            f"{result.stderr.strip() or '(no stderr)'}"
        )
    shutil.rmtree(worktree_path, ignore_errors=True)