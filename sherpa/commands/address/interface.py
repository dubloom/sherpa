"""
Read-only terminal UI for GitHub PR review comment threads.

Style matches ``test_review/review_cli.py`` (ANSI, full redraw, code context panel).
"""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap
from pathlib import Path
from shutil import get_terminal_size
from typing import TYPE_CHECKING, Any, Optional
from . import build_entries

import requests

try:
    import termios
    import tty
except ImportError:
    termios = None  # type: ignore[assignment, misc]
    tty = None  # type: ignore[assignment, misc]

if TYPE_CHECKING:
    from sherpa.commands.address import CommentThread

from sherpa.commands.address.git import post_pull_review_comment_reply
from sherpa.commands.address.suggest_fix import suggest_fix_for_thread_async


class Ansi:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    FG_CYAN = "\033[38;5;45m"
    FG_GREEN = "\033[38;5;114m"
    FG_MAGENTA = "\033[38;5;177m"
    FG_WHITE = "\033[38;5;255m"
    FG_YELLOW = "\033[38;5;221m"
    BG_NAVY = "\033[48;5;17m"


def supports_color() -> bool:
    if os.getenv("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def tty_key_ui_available() -> bool:
    return bool(termios and tty and sys.stdin.isatty() and sys.stdout.isatty())


def style(text: str, *parts: str, enabled: bool) -> str:
    if not enabled or not parts:
        return text
    return "".join(parts) + text + Ansi.RESET


def clear_screen() -> None:
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def wrap_lines(text: str, width: int) -> list[str]:
    return textwrap.wrap(text, width=max(10, width)) or [""]


def _code_location_dict(thread: CommentThread) -> dict[str, Any] | None:
    comments = thread.comments
    if not comments:
        return None
    loc = comments[0].code_location
    return {
        "path": loc.path,
        "line": loc.line,
        "start_line": loc.start_line,
    }


def read_code_snippet(repo_root: Path, code_location: dict[str, Any] | None) -> list[str]:
    if not code_location:
        return ["No file/line attached to this conversation."]

    path = code_location.get("path")
    if not path:
        return ["No file/line attached to this conversation."]

    target = repo_root / str(path)
    if not target.exists():
        return [f"File not found in local repo: {path}"]

    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    end_line = code_location.get("line") or code_location.get("start_line")
    start_line = code_location.get("start_line") or end_line

    if not isinstance(end_line, int):
        return [f"File: {path}", "No exact line available from API."]

    if not isinstance(start_line, int):
        start_line = end_line

    context = 4
    begin = max(1, start_line - context)
    finish = min(len(lines), end_line + context)

    snippet = [f"File: {path}  (lines {start_line}-{end_line})", ""]
    for line_no in range(begin, finish + 1):
        marker = ">" if start_line <= line_no <= end_line else " "
        content = lines[line_no - 1]
        snippet.append(f"{marker} {line_no:4d} | {content}")
    return snippet


def thread_label(index: int, thread: CommentThread) -> str:
    comments = thread.comments
    first = comments[0] if comments else None
    pseudo = first.pseudo if first else "unknown"
    preview = (first.comment or "").splitlines()[0][:48] if first else ""
    loc = _code_location_dict(thread) or {}
    path = loc.get("path")
    line = loc.get("line") or loc.get("start_line")
    location = f"{path}:{line}" if path and line else (path or "general")
    return f"[{index + 1}] {pseudo} @ {location} - {preview}"


def print_command_footer(colorful: bool, width: int, *, tty_keys: bool = False) -> None:
    print()
    nav = "↑↓ = prev/next thread  ·  Enter = next  ·  " if tty_keys else "Enter / next = next thread  ·  back / prev = previous  ·  "
    print(
        style(
            f"Commands:  {nav}r/reply  ·  f/fix  ·  d/done  ·  h/help  ·  q/quit",
            Ansi.DIM,
            Ansi.FG_WHITE,
            enabled=colorful,
        )
    )


def _read_csi_parameter() -> str:
    buf = ""
    while True:
        d = sys.stdin.read(1)
        if not d:
            return buf
        buf += d
        last = buf[-1]
        if last.isalpha() or last == "~":
            return buf


def read_key_tty() -> str:
    if not termios or not tty:
        raise RuntimeError("TTY key reading is not available on this platform.")

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        b = sys.stdin.read(1)
        if not b:
            return ""
        if b in "\r\n":
            return "enter"
        if b == "\x03":
            raise KeyboardInterrupt
        if b == "\x04":
            raise EOFError
        if b != "\x1b":
            return b.lower() if b.isprintable() else b

        b2 = sys.stdin.read(1)
        if not b2:
            return "escape"
        if b2 == "[":
            seq = _read_csi_parameter()
            if seq == "A":
                return "up"
            if seq == "B":
                return "down"
            return f"csi:{seq}"
        if b2 == "O":
            b3 = sys.stdin.read(1)
            if b3 == "A":
                return "up"
            if b3 == "B":
                return "down"
            return f"ss3:{b3!r}"

        return f"esc:{b2!r}"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def wait_quit_only(*, use_tty_keys: bool) -> None:
    """Block until q/quit/exit, EOF, or Ctrl+C; ignore any other input."""
    while True:
        try:
            if use_tty_keys:
                k = read_key_tty()
                if len(k) == 1 and k.isprintable():
                    key = k.lower()
                else:
                    continue
            else:
                key = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if key in ("q", "quit", "exit"):
            return


def prompt_yes_no(question: str, *, colorful: bool, default_yes: bool = False) -> bool:
    """Read a single line; empty input uses default. Returns True for y/yes."""
    hint = "Y/n" if default_yes else "y/N"
    print(
        style(f"{question} [{hint}] ", Ansi.DIM, Ansi.FG_WHITE, enabled=colorful),
        end="",
        flush=True,
    )
    try:
        line = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not line:
        return default_yes
    return line in ("y", "yes")


def read_multiline_until_double_blank() -> str:
    """Read lines from stdin until two consecutive blank lines."""
    lines: list[str] = []
    blank_streak = 0
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "":
            blank_streak += 1
            if blank_streak >= 2:
                if lines and lines[-1].strip() == "":
                    lines.pop()
                break
            lines.append(line)
        else:
            blank_streak = 0
            lines.append(line)
    return "\n".join(lines)


def render_plain(
    threads: list[CommentThread],
    index: int,
    repo_root: Path,
    colorful: bool,
    *,
    owner: str,
    repo: str,
    pr_number: int,
    status_message: Optional[str] = None,
    status_is_error: bool = False,
    tty_keys: bool = False,
    finished_all_threads: bool = False,
) -> None:
    clear_screen()
    width = max(40, get_terminal_size(fallback=(120, 40)).columns - 2)
    current_index = max(0, min(index, len(threads) - 1)) if threads else 0

    header = (
        " Sherpa address  |  ↑↓ threads  ·  [f]ix  [h]elp  [q]uit "
        if tty_keys
        else " Sherpa address  |  Enter/next thread  ·  [f]ix  [h]elp  [q]uit "
    )
    print(style(header.ljust(width), Ansi.BG_NAVY, Ansi.FG_YELLOW, Ansi.BOLD, enabled=colorful))
    sub = f" {owner}/{repo}#{pr_number}  ·  {len(threads)} thread(s) "
    print(style(sub.ljust(width), Ansi.DIM, Ansi.FG_WHITE, enabled=colorful))
    print()

    if status_message:
        st_style = (Ansi.FG_MAGENTA,) if status_is_error else (Ansi.FG_GREEN,)
        print(style(status_message, *st_style, Ansi.BOLD, enabled=colorful))
        print()

    if not threads:
        if finished_all_threads:
            print(
                style(
                    "You're done. No threads left in this session.",
                    Ansi.FG_GREEN,
                    Ansi.BOLD,
                    enabled=colorful,
                )
            )
            print()
            print(style("Press q to quit.", Ansi.DIM, Ansi.FG_WHITE, enabled=colorful))
        else:
            print(style("No review comment threads on this PR (or all filtered).", Ansi.FG_MAGENTA, enabled=colorful))
            print()
            print_command_footer(colorful, width, tty_keys=tty_keys)
        return

    thread = threads[current_index]
    print(style("Code Context", Ansi.BOLD, Ansi.FG_CYAN, enabled=colorful))
    for line in read_code_snippet(repo_root, _code_location_dict(thread)):
        if line.startswith(">"):
            print(style(line, Ansi.FG_YELLOW, Ansi.BOLD, enabled=colorful))
        else:
            print(style(line, Ansi.FG_WHITE, enabled=colorful))
    print()

    print(
        style(
            f" Thread {current_index + 1}/{len(threads)} ".ljust(width),
            Ansi.BG_NAVY,
            Ansi.FG_CYAN,
            Ansi.BOLD,
            enabled=colorful,
        )
    )

    print()
    source_label = "comment"
    for message in thread.comments:
        indent = ""
        pseudo = message.pseudo or "unknown"
        print(indent + style(f"{pseudo} ({source_label})", Ansi.BOLD, Ansi.FG_GREEN, enabled=colorful))
        comment = message.comment or ""
        for paragraph in comment.splitlines() or [""]:
            for line in wrap_lines(paragraph, width=max(12, width - len(indent) - 2)):
                print(f"{indent}  {line}")
        print()

    print_command_footer(colorful, width, tty_keys=tty_keys)


def print_help(colorful: bool) -> None:
    clear_screen()
    w = max(40, get_terminal_size(fallback=(120, 40)).columns - 2)
    title = " Sherpa address — help "
    print(style(title.ljust(w), Ansi.BG_NAVY, Ansi.FG_YELLOW, Ansi.BOLD, enabled=colorful))
    print()
    lines = [
        "Navigation",
        "  Enter or “next”       Next thread (line-based input)",
        "  back / prev           Previous thread (line-based input)",
        "  ↑ / ↓                 Previous / next thread (interactive terminal only)",
        "",
        "Other",
        "  r, reply              Write a reply (two blank lines to send) — posts on GitHub",
        "  f, fix                Start fix flow: confirm fix, then optional AI suggestion",
        "  d, done               Remove current thread from this list (UI only)",
        "  h, help               Show this screen",
        "  q, quit               Exit",
    ]
    for line in lines:
        print(style(line, Ansi.FG_WHITE, enabled=colorful))
    print()
    print(style("Press Enter to return…", Ansi.DIM, enabled=colorful), end=" ")
    input()


def run_viewer(
    threads: list[CommentThread],
    repo_root: Path,
    owner: str,
    repo: str,
    pr_number: int,
    model: str,
) -> None:
    colorful = supports_color()
    index = 0
    use_tty_keys = tty_key_ui_available()
    status_message: Optional[str] = None
    status_is_error = False
    had_any_threads = len(threads) > 0

    while True:
        finished_all = not threads and had_any_threads
        render_plain(
            threads,
            index,
            repo_root,
            colorful,
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            status_message=status_message,
            status_is_error=status_is_error,
            tty_keys=use_tty_keys,
            finished_all_threads=finished_all,
        )
        status_message = None
        status_is_error = False

        if finished_all:
            wait_quit_only(use_tty_keys=use_tty_keys)
            break

        try:
            if use_tty_keys:
                print()
                print(style("Key: ", Ansi.DIM, Ansi.FG_WHITE, enabled=colorful), end="", flush=True)
                k = read_key_tty()
                if k == "up":
                    raw, key = "__prev_thread__", "__prev_thread__"
                elif k == "down":
                    raw, key = "__next_thread__", "__next_thread__"
                elif k == "enter":
                    raw, key = "__next_thread__", "__next_thread__"
                elif len(k) == 1 and k.isprintable():
                    raw, key = k, k.lower()
                else:
                    status_message = f"Unknown key ({k!r}). Press h for help."
                    status_is_error = True
                    continue
            else:
                raw = input("\nCommand: ").strip()
                key = raw.lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if key in ("q", "quit", "exit"):
            break

        if key in ("h", "help", "?"):
            print_help(colorful)
            continue

        if key in ("__next_thread__", "", "next"):
            if not threads:
                status_message = "No threads in the list."
                status_is_error = True
                continue
            if index >= len(threads) - 1:
                status_message = "Already on the last thread."
                continue
            index = min(index + 1, len(threads) - 1)
            continue

        if key in ("__prev_thread__", "back", "prev", "previous"):
            if not threads:
                status_message = "No threads in the list."
                status_is_error = True
                continue
            if index <= 0:
                status_message = "Already on the first thread."
                continue
            index = max(0, index - 1)
            continue

        if key in ("r", "reply"):
            if use_tty_keys:
                print()
            print(
                style(
                    "Reply (finish with two blank lines):",
                    Ansi.DIM,
                    Ansi.FG_WHITE,
                    enabled=colorful,
                )
            )
            body = read_multiline_until_double_blank().strip()
            if not body:
                continue

            thread = threads[index]
            in_reply_to = thread.comments[0].id
            try:
                payload = post_pull_review_comment_reply(
                    owner, repo, pr_number, in_reply_to, body
                )
            except (requests.HTTPError, requests.RequestException, RuntimeError) as exc:
                status_message = str(exc)
                status_is_error = True
                continue

            comments, _ = build_entries([payload])
            if not comments:
                status_message = "Reply posted, but it was filtered from local view."
                continue
            thread.comments.append(comments[0])
            thread.comments.sort(key=lambda c: c.created_at or "")
            continue

        if key in ("f", "fix"):
            if use_tty_keys:
                print()
            if not prompt_yes_no(
                "Do you want an AI suggestion?",
                colorful=colorful,
                default_yes=False,
            ):
                continue
            thread = threads[index]
            print()
            print(
                style(
                    "Running AI suggestion (Read/Glob/Grep in repo; no writes)…",
                    Ansi.DIM,
                    Ansi.FG_WHITE,
                    enabled=colorful,
                )
            )
            sys.stdout.flush()
            try:
                text, cost = asyncio.run(
                    suggest_fix_for_thread_async(
                        repo_root,
                        owner,
                        repo,
                        pr_number,
                        thread,
                        threads,
                        model,
                    )
                )
            except Exception as exc:
                status_message = str(exc)
                status_is_error = True
                continue

            clear_screen()
            w = max(40, get_terminal_size(fallback=(120, 40)).columns - 2)
            title = " AI suggested fix "
            print(style(title.ljust(w), Ansi.BG_NAVY, Ansi.FG_YELLOW, Ansi.BOLD, enabled=colorful))
            if cost is not None:
                print(style(f"Estimated cost: ${cost:.4f}", Ansi.DIM, enabled=colorful))
            print()
            for line in text.splitlines():
                for wrapped in wrap_lines(line, width=w) if line else [""]:
                    print(style(wrapped, Ansi.FG_WHITE, enabled=colorful))
            print()
            print(style("Press Enter to return…", Ansi.DIM, enabled=colorful), end=" ")
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                print()
            continue

        if key in ("d", "done"):
            threads.pop(index)
            index = min(index, len(threads) - 1) if threads else 0
            continue

        status_message = f"Unknown command ({raw!r}). Press h for help."
        status_is_error = True
