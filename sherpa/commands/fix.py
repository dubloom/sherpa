import asyncio
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from agnos import AgentOptions, AgentQueryCompleted, query

from sherpa.commands.base import Command
from sherpa.commands.review import Issue, ReviewResult
from sherpa.git import get_git_repo_root
from sherpa.prompts.fix_issue import get_fix_issue_prompt
from sherpa.review_store import (
    StoredReview,
    build_issue_index,
    flatten_issues_ordered,
    load_stored_review,
    parse_interactive_selection,
    parse_issue_id_args,
)

MAX_CONCURRENT_FIXES = 4

RESET = "\033[0m"
RED = "\033[31m"
GREEN = "\033[32m"
CYAN = "\033[36m"


def _supports_color() -> bool:
    return sys.stdout.isatty()


def _colorize(text: str, color: str) -> str:
    if not _supports_color():
        return text
    return f"{color}{text}{RESET}"


def _list_tracked_files(repo_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo_root,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        return []
    raw = result.stdout.decode("utf-8", errors="ignore")
    return [p for p in raw.split("\x00") if p]


def _list_untracked_files(repo_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=repo_root,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        return []
    raw = result.stdout.decode("utf-8", errors="ignore")
    return [p for p in raw.split("\x00") if p]


def _read_bytes(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _write_bytes(path: Path, content: Optional[bytes]) -> None:
    if content is None:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _snapshot_files(repo_root: Path, rel_paths: list[str]) -> dict[str, Optional[bytes]]:
    return {rel_path: _read_bytes(repo_root / rel_path) for rel_path in rel_paths}


def _changed_files_between_snapshots(
    before: dict[str, Optional[bytes]],
    after: dict[str, Optional[bytes]],
) -> list[str]:
    changed: list[str] = []
    for rel_path in sorted(set(before.keys()) | set(after.keys())):
        if before.get(rel_path) != after.get(rel_path):
            changed.append(rel_path)
    return changed


def _is_binary(content: Optional[bytes]) -> bool:
    return content is not None and b"\x00" in content


def _render_git_style_diff(
    rel_path: str,
    before_bytes: Optional[bytes],
    after_bytes: Optional[bytes],
) -> str:
    with tempfile.TemporaryDirectory(prefix="sherpa-fix-diff-") as tmpdir:
        tmp = Path(tmpdir)

        left_path = Path("/dev/null")
        right_path = Path("/dev/null")

        if before_bytes is not None:
            left_path = tmp / "before"
            left_path.write_bytes(before_bytes)
        if after_bytes is not None:
            right_path = tmp / "after"
            right_path.write_bytes(after_bytes)

        cmd = [
            "git",
            "--no-pager",
            "diff",
            "--no-index",
            "--minimal",
            "--unified=3",
        ]
        if _supports_color():
            cmd.append("--color=always")
        else:
            cmd.append("--no-color")
        cmd.extend([str(left_path), str(right_path)])

        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
        )
        if result.returncode not in (0, 1):
            return ""
        rendered = result.stdout.decode("utf-8", errors="replace")
        if before_bytes is not None:
            rendered = rendered.replace(str(left_path), f"/{rel_path}")
        if after_bytes is not None:
            rendered = rendered.replace(str(right_path), f"/{rel_path}")
        return rendered


def _print_single_file_diff(
    rel_path: str,
    before_bytes: Optional[bytes],
    after_bytes: Optional[bytes],
) -> bool:
    if before_bytes is None and after_bytes is not None:
        change_type = "added"
    elif before_bytes is not None and after_bytes is None:
        change_type = "deleted"
    else:
        change_type = "modified"

    print(_colorize(f"--- {rel_path} ({change_type}) ---", CYAN))
    if _is_binary(before_bytes) or _is_binary(after_bytes):
        print("(binary file changed)")
        return True

    diff_output = _render_git_style_diff(rel_path, before_bytes, after_bytes).strip("\n")
    if not diff_output:
        print("(content unchanged)")
        return False

    for line in diff_output.splitlines():
        if not _supports_color():
            if line.startswith("+++ ") or line.startswith("--- "):
                print(_colorize(line, CYAN))
            elif line.startswith("@@"):
                print(_colorize(line, CYAN))
            elif line.startswith("+") and not line.startswith("+++"):
                print(_colorize(line, GREEN))
            elif line.startswith("-") and not line.startswith("---"):
                print(_colorize(line, RED))
            else:
                print(line)
        else:
            print(line)
    return True


def _prompt_yes_no(prompt: str, default_yes: bool = True) -> bool:
    suffix = " [Y/n]: " if default_yes else " [y/N]: "
    try:
        raw = input(prompt + suffix).strip().lower()
    except EOFError:
        return default_yes
    if not raw:
        return default_yes
    if raw in ("y", "yes"):
        return True
    if raw in ("n", "no"):
        return False
    return default_yes


def _prompt_issue_checkboxes(issues: list[Issue]) -> Optional[list[Issue]]:
    if not issues:
        return []

    # Fallback for non-interactive environments.
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("[sherpa] Non-interactive terminal detected; using text selection.")
        print("[sherpa] Enter issue number(s), ranges, or 'all' (e.g. 1,3-5):")
        try:
            raw = input("[sherpa] Selection: ").strip()
        except EOFError:
            return None
        if not raw:
            return issues
        sel = parse_interactive_selection(raw, len(issues))
        if not sel:
            return None
        return [issues[idx - 1] for idx in sel]

    try:
        import termios
        import tty
    except ImportError:
        return None

    selected: set[int] = set(range(len(issues)))
    cursor = 0
    scroll = 0
    last_rendered_lines = 0
    status = ""

    def _crop(text: str, width: int) -> str:
        if width <= 0:
            return ""
        if len(text) <= width:
            return text
        if width <= 3:
            return "." * width
        return text[: width - 3] + "..."

    def _render() -> None:
        nonlocal scroll, last_rendered_lines
        term_size = shutil.get_terminal_size((120, 30))
        width = term_size.columns
        height = term_size.lines

        list_height = max(1, min(len(issues), height - 4))
        if cursor < scroll:
            scroll = cursor
        if cursor >= scroll + list_height:
            scroll = cursor - list_height + 1

        lines: list[str] = []
        lines.append(
            _crop(
                "[sherpa] Select fixes (UP/DOWN or j/k, SPACE toggle, a=all, n=none, ENTER confirm, q cancel)",
                width,
            )
        )
        lines.append("")
        for row in range(list_height):
            idx = scroll + row
            if idx >= len(issues):
                break
            issue = issues[idx]
            marker = "x" if idx in selected else " "
            pointer = ">" if idx == cursor else " "
            line = f"{pointer} [{marker}] {idx + 1}. [{issue.name}] {issue.title} ({issue.file})"
            lines.append(_crop(line, width))
        lines.append(_crop(f"[sherpa] {len(selected)} selected. {status}", width))

        if last_rendered_lines > 0:
            sys.stdout.write(f"\x1b[{last_rendered_lines}A")
        for i in range(max(last_rendered_lines, len(lines))):
            sys.stdout.write("\r\x1b[2K")
            if i < len(lines):
                sys.stdout.write(lines[i])
            sys.stdout.write("\n")
        sys.stdout.flush()
        last_rendered_lines = max(last_rendered_lines, len(lines))

    def _read_key() -> str:
        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch1 = sys.stdin.read(1)
            if ch1 == "\x1b":
                ch2 = sys.stdin.read(1)
                if ch2 == "[":
                    ch3 = sys.stdin.read(1)
                    if ch3 == "A":
                        return "up"
                    if ch3 == "B":
                        return "down"
                return "unknown"
            if ch1 in ("\r", "\n"):
                return "enter"
            if ch1 == " ":
                return "space"
            if ch1 in ("k", "K"):
                return "up"
            if ch1 in ("j", "J"):
                return "down"
            if ch1 in ("a", "A"):
                return "all"
            if ch1 in ("n", "N"):
                return "none"
            if ch1 in ("q", "Q"):
                return "quit"
            return "unknown"
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)

    while True:
        _render()
        key = _read_key()
        status = ""
        if key == "up":
            cursor = (cursor - 1) % len(issues)
        elif key == "down":
            cursor = (cursor + 1) % len(issues)
        elif key == "space":
            if cursor in selected:
                selected.remove(cursor)
            else:
                selected.add(cursor)
        elif key == "all":
            selected = set(range(len(issues)))
        elif key == "none":
            selected.clear()
        elif key == "enter":
            if selected:
                break
            status = "Select at least one issue."
        elif key == "quit":
            print()
            return None

    print()
    return [issues[idx] for idx in sorted(selected)]


def _revert_fix_changes(
    repo_root: Path,
    changed_tracked: list[str],
    changed_untracked_existing: list[str],
    created_untracked: list[str],
    deleted_untracked: list[str],
    tracked_before: dict[str, Optional[bytes]],
    untracked_before: dict[str, Optional[bytes]],
) -> None:
    for rel_path in changed_tracked:
        _write_bytes(repo_root / rel_path, tracked_before.get(rel_path))

    for rel_path in changed_untracked_existing:
        _write_bytes(repo_root / rel_path, untracked_before.get(rel_path))

    for rel_path in deleted_untracked:
        _write_bytes(repo_root / rel_path, untracked_before.get(rel_path))

    for rel_path in created_untracked:
        path = repo_root / rel_path
        if path.exists():
            path.unlink()


async def _run_fix_agent(
    repo_root: Path,
    issue: Issue,
    stored: StoredReview,
    model: str,
    extra_instruction: Optional[str],
) -> Optional[float]:
    prompt = get_fix_issue_prompt(
        repo_root,
        issue,
        stored.modified_files,
        stored.git_diff,
        extra_instruction=extra_instruction,
    )
    options = AgentOptions(
        cwd=repo_root,
        model=model,
        allowed_tools=["Read", "Write", "Edit", "Glob", "Grep"],
        instructions=(
            "Before implementing the fix, inspect potentially impacted files "
            "(for example nearby callers, shared helpers, and related tests) "
            "to confirm context and avoid regressions."
        ),
    )
    total_cost_usd: Optional[float] = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AgentQueryCompleted):
            raw_cost = message.extra.get("total_cost_usd")
            if isinstance(raw_cost, int | float):
                total_cost_usd = float(raw_cost)
    return total_cost_usd


async def _run_fixes_parallel(
    repo_root: Path,
    stored: StoredReview,
    issues: list[Issue],
    model: str,
    extra_instruction_by_issue: Optional[dict[str, Optional[str]]] = None,
    progress_cb: Optional[Callable[[str, str], None]] = None,
    done_cb: Optional[Callable[[str, Optional[float], Optional[Exception]], None]] = None,
) -> tuple[list[tuple[str, Optional[float], Optional[Exception]]], float]:
    if not issues:
        return [], 0.0

    n = len(issues)
    sem = asyncio.Semaphore(min(MAX_CONCURRENT_FIXES, max(1, n)))

    async def one(issue: Issue) -> tuple[str, Optional[float], Optional[Exception]]:
        async with sem:
            if progress_cb is not None:
                progress_cb(issue.name, "running")
            try:
                issue_instruction = None
                if extra_instruction_by_issue is not None:
                    issue_instruction = extra_instruction_by_issue.get(issue.name)
                cost = await _run_fix_agent(repo_root, issue, stored, model, issue_instruction)
                if progress_cb is not None:
                    progress_cb(issue.name, "finished")
                if done_cb is not None:
                    done_cb(issue.name, cost, None)
                return issue.name, cost, None
            except Exception as e:
                if progress_cb is not None:
                    progress_cb(issue.name, "failed")
                if done_cb is not None:
                    done_cb(issue.name, None, e)
                return issue.name, None, e

    results = await asyncio.gather(*[one(iss) for iss in issues])
    total = 0.0
    for _, cost, err in results:
        if err is None and isinstance(cost, (int, float)):
            total += float(cost)
    return list(results), total


def _prompt_extra_instruction() -> Optional[str]:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return None

    try:
        raw = input("Extra Instruction: ").strip()
    except EOFError:
        return None
    return raw or None


def _collect_extra_instructions(issues: list[Issue]) -> dict[str, Optional[str]]:
    instructions: dict[str, Optional[str]] = {}
    if not issues:
        return instructions

    for issue in issues:
        print(f"[sherpa] [{issue.name}] {issue.title}")
        instructions[issue.name] = _prompt_extra_instruction()
    return instructions


class FixCommand(Command):
    @staticmethod
    def execute(args: list[str], model: str):
        root = get_git_repo_root()
        stored = load_stored_review(root)
        if stored is None:
            print(
                "[sherpa] No stored review found. Run `sherpa commit` first to generate a review.",
                file=sys.stderr,
            )
            return

        if not stored.has_parsed_issues or stored.review_result is None:
            print(
                "[sherpa] Latest review could not be parsed into issues; cannot run fix.",
                file=sys.stderr,
            )
            if stored.raw_review:
                print(stored.raw_review[:2000], file=sys.stderr)
            return

        rr: ReviewResult = stored.review_result
        index = build_issue_index(rr)
        issue_ids = parse_issue_id_args(args)

        issues_to_fix: list[Issue]
        if not issue_ids:
            flat = flatten_issues_ordered(rr)
            if not flat:
                print("[sherpa] No issues in the latest review.")
                return
            selected_issues = _prompt_issue_checkboxes(flat)
            if selected_issues is None:
                print("[sherpa] Selection cancelled; exiting.", file=sys.stderr)
                return
            issues_to_fix = selected_issues
        else:
            missing = [i for i in issue_ids if i not in index]
            if missing:
                print(
                    f"[sherpa] Unknown issue ID(s): {', '.join(missing)}",
                    file=sys.stderr,
                )
                known = ", ".join(sorted(index.keys()))
                if known:
                    print(f"[sherpa] Known IDs: {known}", file=sys.stderr)
                return
            issues_to_fix = [index[i] for i in issue_ids]

        extra_instruction_by_issue = _collect_extra_instructions(issues_to_fix)

        print(f"[sherpa] Running fix agent(s) for: {', '.join(i.name for i in issues_to_fix)}")
        statuses: dict[str, str] = {issue.name: "queued" for issue in issues_to_fix}
        started_at: dict[str, float] = {}
        issue_by_name = {issue.name: issue for issue in issues_to_fix}

        def print_progress() -> None:
            parts: list[str] = []
            now = time.time()
            for issue in issues_to_fix:
                st = statuses.get(issue.name, "queued")
                label = f"{issue.name}:{st}"
                t0 = started_at.get(issue.name)
                if t0 is not None and st in ("running", "finished", "failed"):
                    label += f"({int(now - t0)}s)"
                parts.append(label)
            print(f"[sherpa] Progress: {' | '.join(parts)}")

        def on_progress(issue_name: str, new_status: str) -> None:
            statuses[issue_name] = new_status
            if new_status == "running":
                started_at[issue_name] = time.time()
            print_progress()

        print_progress()
        tracked_files = _list_tracked_files(root)
        untracked_before_paths = _list_untracked_files(root)
        tracked_before = _snapshot_files(root, tracked_files)
        untracked_before = _snapshot_files(root, untracked_before_paths)
        file_baseline: dict[str, Optional[bytes]] = {**tracked_before, **untracked_before}
        reviewed_files: set[str] = set()

        def on_done(issue_name: str, _cost: Optional[float], err: Optional[Exception]) -> None:
            print()
            print(f"=== Fix: [{issue_name}] ===")
            if err is not None:
                print(f"[sherpa] Error: {err}", file=sys.stderr)
                return

            issue = issue_by_name.get(issue_name)
            if issue is None:
                print("(issue metadata unavailable)")
                return

            target = issue.file.strip()
            if not target:
                print("(no target file declared for this issue)")
                return

            before_bytes = file_baseline.get(target)
            after_bytes = _read_bytes(root / target)
            if before_bytes == after_bytes:
                print(f"(no direct change detected on target file: {target})")
                return

            _print_single_file_diff(target, before_bytes, after_bytes)
            reviewed_files.add(target)

            if _prompt_yes_no(f"[sherpa] Keep changes for [{issue_name}]?", default_yes=True):
                file_baseline[target] = after_bytes
                return

            _write_bytes(root / target, before_bytes)
            file_baseline[target] = _read_bytes(root / target)
            print(f"[sherpa] Reverted changes for [{issue_name}].")

        results, aggregate_cost = asyncio.run(
            _run_fixes_parallel(
                root,
                stored,
                issues_to_fix,
                model,
                extra_instruction_by_issue=extra_instruction_by_issue,
                progress_cb=on_progress,
                done_cb=on_done,
            )
        )

        tracked_after = _snapshot_files(root, tracked_files)
        untracked_after_paths = _list_untracked_files(root)
        untracked_after = _snapshot_files(root, untracked_after_paths)

        changed_tracked_files = _changed_files_between_snapshots(tracked_before, tracked_after)
        changed_untracked_existing = [
            rel_path
            for rel_path in untracked_before_paths
            if rel_path in untracked_after and untracked_before.get(rel_path) != untracked_after.get(rel_path)
        ]
        new_untracked_files = sorted(set(untracked_after_paths) - set(untracked_before_paths))
        deleted_untracked_files = sorted(set(untracked_before_paths) - set(untracked_after_paths))
        changed_files = sorted(
            set(changed_tracked_files)
            | set(changed_untracked_existing)
            | set(new_untracked_files)
            | set(deleted_untracked_files)
        )

        ok = sum(1 for _, _, err in results if err is None)
        failed = len(results) - ok

        residual_files = [p for p in changed_files if p not in reviewed_files]
        if residual_files and not _prompt_yes_no(
            "[sherpa] Keep remaining unmapped changes?",
            default_yes=True,
        ):
            _revert_fix_changes(
                root,
                changed_tracked_files,
                changed_untracked_existing,
                new_untracked_files,
                deleted_untracked_files,
                tracked_before,
                untracked_before,
            )
            print("[sherpa] Reverted changes made during this fix run.")

        print()
        print(
            f"[sherpa] Fix run finished: {ok} succeeded, {failed} failed; "
            f"total cost: {aggregate_cost}$"
        )
        print("[sherpa] Review and stage changes before committing.")
