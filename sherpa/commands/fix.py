import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from agnos import AgentOptions, AgentQueryCompleted
from agnos.client import AgnosClient

from sherpa.commands.base import Command
from sherpa.commands.review import Issue, ReviewResult
from sherpa.git import create_detached_worktree, remove_worktree
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
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
CYAN = "\033[36m"


def _supports_color() -> bool:
    if os.getenv("NO_COLOR"):
        return False
    if os.getenv("TERM", "").lower() == "dumb":
        return False
    return sys.stdout.isatty()


def _colorize(text: str, color: str) -> str:
    if not _supports_color():
        return text
    return f"{color}{text}{RESET}"


def _colorize_bold(text: str, color: str) -> str:
    if not _supports_color():
        return text
    return f"{BOLD}{color}{text}{RESET}"


def _status_badge(status: str) -> str:
    normalized = status.strip().lower()
    label = normalized.upper() if normalized else "UNKNOWN"
    if normalized == "running":
        return _colorize_bold(f"[{label}]", BLUE)
    if normalized == "finished":
        return _colorize_bold(f"[{label}]", GREEN)
    if normalized == "failed":
        return _colorize_bold(f"[{label}]", RED)
    if normalized == "queued":
        return _colorize_bold(f"[{label}]", YELLOW)
    return f"[{label}]"


def _input_with_colored_typing(
    prompt: str,
    prompt_color: str = CYAN,
    input_color: str = GREEN,
) -> str:
    if not _supports_color() or not sys.stdin.isatty() or not sys.stdout.isatty():
        return input(prompt)
    try:
        return input(f"{_colorize_bold(prompt, prompt_color)}{input_color}")
    finally:
        # Always reset terminal style even on EOF/interruption.
        sys.stdout.write(RESET)
        sys.stdout.flush()


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


def _materialize_snapshot(
    repo_root: Path,
    snapshot: dict[str, Optional[bytes]],
) -> None:
    for rel_path, content in snapshot.items():
        _write_bytes(repo_root / rel_path, content)


def _capture_issue_delta(
    worktree_root: Path,
    baseline_snapshot: dict[str, Optional[bytes]],
) -> tuple[list[str], dict[str, Optional[bytes]]]:
    untracked_after_paths = _list_untracked_files(worktree_root)
    all_paths = sorted(set(baseline_snapshot.keys()) | set(untracked_after_paths))
    before = {rel_path: baseline_snapshot.get(rel_path) for rel_path in all_paths}
    after = _snapshot_files(worktree_root, all_paths)
    changed_paths = _changed_files_between_snapshots(before, after)
    return changed_paths, after


def _apply_issue_delta_to_main(
    repo_root: Path,
    changed_paths: list[str],
    after_snapshot: dict[str, Optional[bytes]],
    main_baseline: dict[str, Optional[bytes]],
) -> tuple[list[str], list[str], list[str]]:
    applied: list[str] = []
    already_present: list[str] = []
    conflicted: list[str] = []

    for rel_path in changed_paths:
        expected_before = main_baseline.get(rel_path)
        current_bytes = _read_bytes(repo_root / rel_path)
        desired_after = after_snapshot.get(rel_path)

        if current_bytes != expected_before:
            if current_bytes == desired_after:
                already_present.append(rel_path)
                main_baseline[rel_path] = current_bytes
            else:
                conflicted.append(rel_path)
            continue

        _write_bytes(repo_root / rel_path, desired_after)
        main_baseline[rel_path] = desired_after
        applied.append(rel_path)

    return applied, already_present, conflicted


async def _run_fix_agent_turn(
    client: AgnosClient,
    session_id: str,
    workspace_root: Path,
    issue: Issue,
    stored: StoredReview,
    extra_instruction: Optional[str],
) -> Optional[float]:
    prompt = get_fix_issue_prompt(
        workspace_root,
        issue,
        stored.modified_files,
        stored.git_diff,
        extra_instruction=extra_instruction,
    )
    total_cost_usd: Optional[float] = None
    async for message in client.query_streamed(prompt, session_id=session_id):
        if isinstance(message, AgentQueryCompleted):
            raw_cost = message.total_cost_usd
            if isinstance(raw_cost, int | float):
                total_cost_usd = float(raw_cost)
    return total_cost_usd


async def _run_fixes_parallel(
    repo_root: Path,
    stored: StoredReview,
    baseline_snapshot: dict[str, Optional[bytes]],
    issues: list[Issue],
    model: str,
    extra_instruction_by_issue: Optional[dict[str, Optional[str]]] = None,
    progress_cb: Optional[Callable[[str, str], None]] = None,
    done_cb: Optional[
        Callable[
            [str, int, Optional[float], Optional[Exception], list[str], dict[str, Optional[bytes]]],
            tuple[str, Optional[str]],
        ]
    ] = None,
) -> tuple[
    list[tuple[str, Optional[float], Optional[Exception], list[str], dict[str, Optional[bytes]]]],
    float,
]:
    if not issues:
        return [], 0.0

    n = len(issues)
    sem = asyncio.Semaphore(min(MAX_CONCURRENT_FIXES, max(1, n)))

    async def one(
        issue: Issue,
    ) -> tuple[str, Optional[float], Optional[Exception], list[str], dict[str, Optional[bytes]]]:
        async with sem:
            if progress_cb is not None:
                progress_cb(issue.name, "running")
            workspace_root: Optional[Path] = None
            try:
                workspace_root = create_detached_worktree(repo_root, issue.name)
                _materialize_snapshot(workspace_root, baseline_snapshot)

                issue_instruction = None
                if extra_instruction_by_issue is not None:
                    issue_instruction = extra_instruction_by_issue.get(issue.name)

                options = AgentOptions(
                    cwd=workspace_root,
                    model=model,
                    allowed_tools=["Read", "Write", "Edit", "Glob", "Grep"],
                    instructions=(
                        "Before implementing the fix, inspect potentially impacted files "
                        "(for example nearby callers, shared helpers, and related tests) "
                        "to confirm context and avoid regressions."
                    ),
                )
                session_id = f"fix-{issue.name}-{uuid.uuid4().hex[:8]}"
                total_issue_cost = 0.0
                attempt = 1
                final_changed_paths: list[str] = []
                final_after_snapshot: dict[str, Optional[bytes]] = {}

                async with AgnosClient(options) as client:
                    while True:
                        attempt_cost = await _run_fix_agent_turn(
                            client,
                            session_id,
                            workspace_root,
                            issue,
                            stored,
                            issue_instruction,
                        )
                        if isinstance(attempt_cost, (int, float)):
                            total_issue_cost += float(attempt_cost)

                        changed_paths, after_snapshot = _capture_issue_delta(workspace_root, baseline_snapshot)
                        final_changed_paths = changed_paths
                        final_after_snapshot = after_snapshot

                        action = "keep"
                        next_instruction: Optional[str] = None
                        if done_cb is not None:
                            action, next_instruction = done_cb(
                                issue.name,
                                attempt,
                                attempt_cost,
                                None,
                                changed_paths,
                                after_snapshot,
                            )

                        if action == "retry":
                            _materialize_snapshot(workspace_root, baseline_snapshot)
                            issue_instruction = next_instruction
                            attempt += 1
                            continue

                        if action == "discard":
                            final_changed_paths = []
                            final_after_snapshot = {}
                        break

                final_cost: Optional[float] = total_issue_cost
                if progress_cb is not None:
                    progress_cb(issue.name, "finished")
                return issue.name, final_cost, None, final_changed_paths, final_after_snapshot
            except Exception as e:
                if progress_cb is not None:
                    progress_cb(issue.name, "failed")
                if done_cb is not None:
                    done_cb(issue.name, 1, None, e, [], {})
                return issue.name, None, e, [], {}
            finally:
                if workspace_root is not None:
                    remove_worktree(repo_root, workspace_root)

    results = await asyncio.gather(*[one(iss) for iss in issues])
    total = 0.0
    for _, cost, err, _, _ in results:
        if err is None and isinstance(cost, (int, float)):
            total += float(cost)
    return list(results), total


def _prompt_extra_instruction(prompt_text: str = "Extra Instruction: ") -> Optional[str]:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return None

    try:
        raw = _input_with_colored_typing(prompt_text).strip()
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


class _FixDisplayCoordinator:
    def __init__(self, issues: list[Issue]):
        self._issues = issues
        self._statuses: dict[str, str] = {issue.name: "queued" for issue in issues}
        self._started_at: dict[str, float] = {}
        self._lock = threading.RLock()
        self._tty = sys.stdout.isatty()
        self._dashboard_visible = False
        self._dashboard_paused = 0
        self._last_plain_line: Optional[str] = None
        self._stop_refresh = threading.Event()
        self._refresh_thread: Optional[threading.Thread] = None

    def _progress_line(self) -> str:
        parts: list[str] = []
        now = time.time()
        for issue in self._issues:
            st = self._statuses.get(issue.name, "queued")
            label = f"{issue.name}:{_status_badge(st)}"
            t0 = self._started_at.get(issue.name)
            if t0 is not None and st in ("running", "finished", "failed"):
                label += f"({int(now - t0)}s)"
            parts.append(label)
        return f"[sherpa] Progress: {' | '.join(parts)}"

    def render_progress(self) -> None:
        with self._lock:
            line = self._progress_line()
            if self._tty:
                if self._dashboard_paused > 0:
                    return
                sys.stdout.write(f"\r\x1b[2K{line}")
                sys.stdout.flush()
                self._dashboard_visible = True
                return

            if line != self._last_plain_line:
                print(line)
                self._last_plain_line = line

    def update_status(self, issue_name: str, new_status: str) -> None:
        with self._lock:
            self._statuses[issue_name] = new_status
            if new_status == "running":
                self._started_at[issue_name] = time.time()
        self.render_progress()

    def _clear_dashboard_line(self) -> None:
        if self._tty and self._dashboard_visible:
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.flush()
            self._dashboard_visible = False

    def _pause_dashboard(self) -> None:
        self._dashboard_paused += 1
        self._clear_dashboard_line()

    def _resume_dashboard(self) -> None:
        self._dashboard_paused = max(0, self._dashboard_paused - 1)
        if self._dashboard_paused == 0:
            self.render_progress()

    def run_section(self, fn: Callable[[], None]) -> None:
        with self._lock:
            self._pause_dashboard()
            try:
                fn()
            finally:
                self._resume_dashboard()

    def finalize(self) -> None:
        with self._lock:
            if self._tty and self._dashboard_visible:
                print()
                self._dashboard_visible = False

    def start_live_refresh(self) -> None:
        if not self._tty:
            return
        with self._lock:
            if self._refresh_thread is not None:
                return
            self._stop_refresh.clear()
            self._refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
            self._refresh_thread.start()

    def stop_live_refresh(self) -> None:
        with self._lock:
            thread = self._refresh_thread
            if thread is None:
                return
            self._refresh_thread = None
            self._stop_refresh.set()
        thread.join(timeout=2)

    def _refresh_loop(self) -> None:
        while not self._stop_refresh.wait(timeout=1.0):
            self.render_progress()


class FixCommand(Command):
    @staticmethod
    def execute(args: list[str], repo_root: Path, model: str):
        root = repo_root
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

        print(_colorize_bold("[sherpa] Fix Session", CYAN))
        print(f"[sherpa] Running fix agent(s) for: {', '.join(i.name for i in issues_to_fix)}")
        display = _FixDisplayCoordinator(issues_to_fix)

        def on_progress(issue_name: str, new_status: str) -> None:
            display.update_status(issue_name, new_status)

        display.render_progress()
        display.start_live_refresh()
        tracked_files = _list_tracked_files(root)
        untracked_before_paths = _list_untracked_files(root)
        tracked_before = _snapshot_files(root, tracked_files)
        untracked_before = _snapshot_files(root, untracked_before_paths)
        run_baseline: dict[str, Optional[bytes]] = {**tracked_before, **untracked_before}
        main_baseline: dict[str, Optional[bytes]] = dict(run_baseline)

        def on_done(
            issue_name: str,
            attempt: int,
            _cost: Optional[float],
            err: Optional[Exception],
            changed_paths: list[str],
            after_snapshot: dict[str, Optional[bytes]],
        ) -> tuple[str, Optional[str]]:
            def render_issue() -> tuple[str, Optional[str]]:
                print()
                print(_colorize_bold(f"=== Fix: [{issue_name}] (attempt {attempt}) ===", CYAN))
                if err is not None:
                    print(_colorize_bold(f"[sherpa] Error in [{issue_name}]: {err}", RED), file=sys.stderr)
                    return ("discard", None)

                if not changed_paths:
                    print(_colorize(f"[sherpa] [{issue_name}] no changes detected.", YELLOW))
                    if _prompt_yes_no(
                        _colorize_bold(
                            f"[sherpa] Retry [{issue_name}] with a new instruction in the same agent session?",
                            YELLOW,
                        ),
                        default_yes=True,
                    ):
                        retry_instruction = _prompt_extra_instruction("New Instruction: ")
                        if retry_instruction:
                            return ("retry", retry_instruction)
                        print(_colorize("[sherpa] Empty instruction; discarding this attempt.", YELLOW))
                    return ("discard", None)

                for rel_path in changed_paths:
                    _print_single_file_diff(
                        rel_path,
                        run_baseline.get(rel_path),
                        after_snapshot.get(rel_path),
                    )

                print(
                    _colorize(
                        f"[sherpa] [{issue_name}] changed {len(changed_paths)} file(s).",
                        CYAN,
                    )
                )
                if _prompt_yes_no(
                    _colorize_bold(f"[sherpa] Keep changes for [{issue_name}]?", YELLOW),
                    default_yes=True,
                ):
                    applied, already_present, conflicted = _apply_issue_delta_to_main(
                        root,
                        changed_paths,
                        after_snapshot,
                        main_baseline,
                    )
                    if applied:
                        print(
                            _colorize_bold(
                                f"[sherpa] Applied {len(applied)} file(s) for [{issue_name}].",
                                GREEN,
                            )
                        )
                    if already_present:
                        print(
                            _colorize(
                                f"[sherpa] {len(already_present)} file(s) already matched desired content "
                                f"for [{issue_name}].",
                                YELLOW,
                            )
                        )
                    if conflicted:
                        print(
                            _colorize_bold(
                                f"[sherpa] Conflict while applying [{issue_name}] for: "
                                f"{', '.join(conflicted)}",
                                RED,
                            ),
                            file=sys.stderr,
                        )
                    return ("keep", None)

                if _prompt_yes_no(
                    _colorize_bold(
                        f"[sherpa] Retry [{issue_name}] with a new instruction in the same agent session?",
                        YELLOW,
                    ),
                    default_yes=True,
                ):
                    retry_instruction = _prompt_extra_instruction("New Instruction: ")
                    if retry_instruction:
                        print(_colorize("[sherpa] Retrying with your instruction...", CYAN))
                        return ("retry", retry_instruction)
                    print(_colorize("[sherpa] Empty instruction; discarding this attempt.", YELLOW))

                print(_colorize_bold(f"[sherpa] Discarded changes for [{issue_name}].", YELLOW))
                return ("discard", None)

            return render_issue()

        def on_done_wrapped(
            issue_name: str,
            attempt: int,
            cost: Optional[float],
            err: Optional[Exception],
            changed_paths: list[str],
            after_snapshot: dict[str, Optional[bytes]],
        ) -> tuple[str, Optional[str]]:
            decision: tuple[str, Optional[str]] = ("discard", None)

            def run_and_capture() -> None:
                nonlocal decision
                decision = on_done(
                    issue_name,
                    attempt,
                    cost,
                    err,
                    changed_paths,
                    after_snapshot,
                )

            display.run_section(run_and_capture)
            return decision

        results, aggregate_cost = asyncio.run(
            _run_fixes_parallel(
                root,
                stored,
                run_baseline,
                issues_to_fix,
                model,
                extra_instruction_by_issue=extra_instruction_by_issue,
                progress_cb=on_progress,
                done_cb=on_done_wrapped,
            )
        )
        display.stop_live_refresh()

        display.finalize()
        print()
        print(f"[sherpa] Total cost: {_colorize_bold(f'{aggregate_cost}$', CYAN)}")
        print("[sherpa] Review and stage changes before committing.")
