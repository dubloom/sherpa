import sys

from sherpa.config import (
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORT,
    SUPPORTED_REASONING_EFFORTS,
    SherpaConfig,
)
from sherpa.supported_models import SUPPORTED_MODEL

try:
    import termios
    import tty
except ImportError:  # pragma: no cover - only used on non-posix platforms.
    termios = None
    tty = None


def is_interactive_session() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _print_checkbox_options(options: tuple[str, ...], selected_option: str) -> None:
    for idx, option in enumerate(options, start=1):
        marker = "x" if option == selected_option else " "
        cursor = ">" if option == selected_option else " "
        print(f" {cursor} {idx}. [{marker}] {option}")


def _read_menu_key() -> str:
    if termios is None or tty is None:
        return ""

    stdin_fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(stdin_fd)
    try:
        tty.setraw(stdin_fd)
        char = sys.stdin.read(1)
        if char == "\x03":
            raise KeyboardInterrupt
        if char in ("\r", "\n"):
            return "enter"
        if char == "\x1b":
            second = sys.stdin.read(1)
            third = sys.stdin.read(1)
            if second == "[" and third == "A":
                return "up"
            if second == "[" and third == "B":
                return "down"
        if char in ("k", "K"):
            return "up"
        if char in ("j", "J"):
            return "down"
        return ""
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_settings)


def _render_checkbox_menu(prompt: str, options: tuple[str, ...], selected_idx: int) -> int:
    print(prompt)
    _print_checkbox_options(options, options[selected_idx])
    print("Use Up/Down arrows to move, Enter to confirm.")
    return len(options) + 2


def _prompt_checkbox_choice_fallback(options: tuple[str, ...], default: str) -> str:
    if default not in options:
        default = options[0]
    default_idx = options.index(default) + 1
    while True:
        choice = input(f"Select one option [1-{len(options)}] (default: {default_idx}): ").strip()
        if choice == "":
            return default
        if choice.isdigit():
            selected_idx = int(choice)
            if 1 <= selected_idx <= len(options):
                return options[selected_idx - 1]
        print(f"Invalid choice: {choice}. Please enter a number between 1 and {len(options)}.")


def _prompt_checkbox_choice(prompt: str, options: tuple[str, ...], default: str) -> str:
    if not options:
        raise ValueError("Checkbox prompt requires at least one option.")
    if default not in options:
        default = options[0]

    if termios is None or tty is None:
        print(prompt)
        _print_checkbox_options(options, default)
        return _prompt_checkbox_choice_fallback(options, default)

    selected_idx = options.index(default)
    rendered_line_count = _render_checkbox_menu(prompt, options, selected_idx)

    def redraw() -> None:
        # Move the cursor up and rewrite menu lines to reflect current selection.
        print(f"\033[{rendered_line_count}A", end="")
        for _ in range(rendered_line_count):
            print("\033[2K", end="\r")
            print("\033[1B", end="")
        print(f"\033[{rendered_line_count}A", end="")
        _render_checkbox_menu(prompt, options, selected_idx)

    while True:
        key = _read_menu_key()
        if key == "enter":
            print()
            return options[selected_idx]
        if key == "up":
            selected_idx = (selected_idx - 1) % len(options)
            redraw()
        elif key == "down":
            selected_idx = (selected_idx + 1) % len(options)
            redraw()


def prompt_new_config(initial_config: SherpaConfig) -> SherpaConfig:
    print("[sherpa] No config found. Let's create one.")
    model = _prompt_checkbox_choice(
        "Choose your default model:",
        tuple(SUPPORTED_MODEL),
        initial_config.default_model or DEFAULT_MODEL,
    )
    reasoning_effort = _prompt_checkbox_choice(
        (
            "Choose your default reasoning effort "
            "(used for OpenAI models only):"
        ),
        SUPPORTED_REASONING_EFFORTS,
        initial_config.default_reasoning_effort or DEFAULT_REASONING_EFFORT,
    )
    print("[sherpa] Config created.")
    return SherpaConfig(
        default_model=model,
        default_reasoning_effort=reasoning_effort,
    )


def prompt_update_config(initial_config: SherpaConfig) -> SherpaConfig:
    print("[sherpa] Updating existing config.")
    model = _prompt_checkbox_choice(
        "Choose your default model:",
        tuple(SUPPORTED_MODEL),
        initial_config.default_model or DEFAULT_MODEL,
    )
    reasoning_effort = _prompt_checkbox_choice(
        (
            "Choose your default reasoning effort "
            "(used for OpenAI models only):"
        ),
        SUPPORTED_REASONING_EFFORTS,
        initial_config.default_reasoning_effort or DEFAULT_REASONING_EFFORT,
    )
    print("[sherpa] Config updated.")
    return SherpaConfig(
        default_model=model,
        default_reasoning_effort=reasoning_effort,
    )
