import os
import shutil
import sys
import textwrap

RESET = "\033[0m"
BOLD = "\033[1m"
RED = "\033[31m"
ORANGE = "\033[38;5;208m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
GREEN = "\033[32m"
CYAN = "\033[36m"


def supports_color() -> bool:
    if os.getenv("NO_COLOR"):
        return False
    if os.getenv("TERM", "").lower() == "dumb":
        return False
    return sys.stdout.isatty()


def colorize(text: str, color: str, bold: bool = False) -> str:
    if not supports_color():
        return text
    prefix = BOLD if bold else ""
    return f"{prefix}{color}{text}{RESET}"


def wrap_text(value: str, width: int) -> list[str]:
    return textwrap.wrap(
        value or "",
        width=width,
        break_long_words=False,
        break_on_hyphens=False,
    ) or [""]


def _print_severity_block(title: str, items: list, marker: str, color: str) -> None:
    heading = f"{title} ({len(items)})"
    print()
    print(colorize(heading, color, bold=True))
    print(colorize("-" * len(heading), color))
    if not items:
        print("  (none)")
        return

    terminal_width = shutil.get_terminal_size(fallback=(100, 20)).columns
    card_width = max(72, min(120, terminal_width - 4))
    content_width = card_width - 4

    for issue in items:
        title_line = f"{marker} [{issue.name}] {issue.title}"
        file_line = f"file: {issue.file}"
        why_lines = wrap_text(f"Why: {issue.details}", content_width)
        fix_lines = wrap_text(f"Fix: {issue.suggested_fix}", content_width)

        border = "+" + "-" * (card_width - 2) + "+"
        print(colorize(f"  {border}", color))
        print(colorize(f"  | {title_line[:content_width].ljust(content_width)} |", color, bold=True))
        print(colorize(f"  | {file_line[:content_width].ljust(content_width)} |", color))
        print(colorize(f"  | {'':{content_width}} |", color))

        for line in why_lines:
            print(colorize(f"  | {line[:content_width].ljust(content_width)} |", color))
        print(colorize(f"  | {'':{content_width}} |", color))
        for line in fix_lines:
            print(colorize(f"  | {line[:content_width].ljust(content_width)} |", color))

        print(colorize(f"  {border}", color))
        print()


def render_review_report(review_result) -> None:
    total_issues = (
        len(review_result.high_issues)
        + len(review_result.medium_issues)
        + len(review_result.low_issues)
    )

    decision_color = GREEN if review_result.decision == "approve" else RED
    print()
    print(colorize("=== Sherpa Review Report ===", CYAN, bold=True))
    print(colorize(f"Decision: {review_result.decision.upper()}", decision_color, bold=True))
    summary_width = max(60, min(120, shutil.get_terminal_size(fallback=(100, 20)).columns - 2))
    summary_lines = wrap_text(review_result.summary, summary_width - len("Summary: "))
    if summary_lines:
        print(f"Summary: {summary_lines[0]}")
        for line in summary_lines[1:]:
            print(f"         {line}")
    print(
        "Counts: "
        f"{colorize(f'high={len(review_result.high_issues)}', RED, bold=True)}, "
        f"{colorize(f'medium={len(review_result.medium_issues)}', ORANGE, bold=True)}, "
        f"{colorize(f'low={len(review_result.low_issues)}', YELLOW, bold=True)}, "
        f"{colorize(f'nits={len(review_result.nits)}', BLUE, bold=True)}"
    )
    print(f"Total issues: {total_issues}")

    _print_severity_block("High severity", review_result.high_issues, "!", RED)
    _print_severity_block("Medium severity", review_result.medium_issues, "~", ORANGE)
    _print_severity_block("Low severity", review_result.low_issues, "-", YELLOW)
    _print_severity_block("Nice to have", review_result.nits, "+", BLUE)
