from pathlib import Path
import shutil
import subprocess
import tempfile
from enum import Enum
from typing import Optional


class SherpaGitExcludeStatus(Enum):
    ADDED = "added"
    ALREADY_PRESENT = "already_present"
    ERROR = "error"

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


def ensure_sherpa_git_exclude(git_repo_root: Path) -> SherpaGitExcludeStatus:
    exclude_path = git_repo_root / ".git" / "info" / "exclude"
    try:
        existing_content = exclude_path.read_text(encoding="utf-8")
        existing_entries = {
            line.strip()
            for line in existing_content.splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        if ".sherpa" in existing_entries or "/.sherpa" in existing_entries:
            return SherpaGitExcludeStatus.ALREADY_PRESENT

        suffix = "" if not existing_content or existing_content.endswith("\n") else "\n"
        exclude_path.write_text(f"{existing_content}{suffix}.sherpa\n", encoding="utf-8")
        return SherpaGitExcludeStatus.ADDED
    except OSError:
        return SherpaGitExcludeStatus.ERROR

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


def _git_ref_exists(root: Path, ref: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def resolve_main_or_master(root: Path) -> Optional[str]:
    for branch in ("main", "master"):
        if _git_ref_exists(root, branch):
            return branch

    for branch in ("main", "master"):
        remote_branch = f"origin/{branch}"
        if _git_ref_exists(root, remote_branch):
            return remote_branch

    return None


def get_branch_changes(root: Path, branch: str = "HEAD") -> tuple[Optional[str], Optional[str], Optional[str]]:
    base_branch = resolve_main_or_master(root)
    if not base_branch:
        print("[sherpa][warning] Could not resolve base branch (main/master).")
        return None, None, None

    diff_range = f"{base_branch}...{branch}"
    modified_files = execute_git_command(
        ["diff", "--name-only", diff_range],
        cwd=root,
        warning_message=f"Could not retrieve the list of modified files for {diff_range}",
    )

    diff = execute_git_command(
        [
            "diff",
            "--patch",
            "--no-color",
            "--no-ext-diff",
            "--minimal",
            diff_range,
        ],
        cwd=root,
        warning_message=f"Could not retrieve git diff for {diff_range}",
    )
    return modified_files, diff, base_branch


def get_commit_changes(
    root: Path, commit: str
) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    commit_ref = f"{commit}^{{commit}}"
    if not _git_ref_exists(root, commit_ref):
        print(f"[sherpa][warning] Could not resolve commit '{commit}'.")
        return None, None, None, None

    resolved_commit = execute_git_command(
        ["rev-parse", "--short", commit_ref],
        cwd=root,
        warning_message=f"Could not resolve commit hash for {commit}",
    )
    commit_subject = execute_git_command(
        ["show", "--quiet", "--format=%s", commit_ref],
        cwd=root,
        warning_message=f"Could not retrieve commit subject for {commit}",
    )
    modified_files = execute_git_command(
        ["show", "--pretty=format:", "--name-only", commit_ref],
        cwd=root,
        warning_message=f"Could not retrieve the list of modified files for {commit}",
    )
    diff = execute_git_command(
        [
            "show",
            "--patch",
            "--no-color",
            "--no-ext-diff",
            "--minimal",
            "--pretty=format:",
            commit_ref,
        ],
        cwd=root,
        warning_message=f"Could not retrieve git diff for {commit}",
    )
    return modified_files, diff, (resolved_commit or commit), (commit_subject or None)


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