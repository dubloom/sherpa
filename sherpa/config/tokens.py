from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from sherpa.config import _atomic_write_json

TOKENS_FILENAME = "tokens.json"
TOKEN_PATH = Path.home() / ".sherpa" / TOKENS_FILENAME

OPENAI_ENV_VAR = "OPENAI_API_KEY"
ANTHROPIC_ENV_VAR = "ANTHROPIC_API_KEY"
GITHUB_ENV_VAR = "GITHUB_TOKEN"


@dataclass
class SherpaTokens:
    openai_token: Optional[str] = None
    anthropic_token: Optional[str] = None
    github_token: Optional[str] = None


def _normalize_token_value(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def load_tokens() -> SherpaTokens:
    path = TOKEN_PATH
    if not path.is_file():
        return SherpaTokens()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return SherpaTokens()

    if not isinstance(raw, dict):
        return SherpaTokens()

    tokens = SherpaTokens(
        openai_token=_normalize_token_value(raw.get("openai_token")),
        anthropic_token=_normalize_token_value(raw.get("anthropic_token")),
        github_token=_normalize_token_value(raw.get("github_token")),
    )
    return tokens


def save_tokens(tokens: SherpaTokens) -> None:
    _atomic_write_json(
        TOKEN_PATH,
        {
        "openai_token": tokens.openai_token,
        "anthropic_token": tokens.anthropic_token,
        "github_token": tokens.github_token,
        },
        parent_mode=0o700,
        file_mode=0o600,
    )


def resolve_github_token() -> Optional[str]:
    env_token = _normalize_token_value(os.getenv(GITHUB_ENV_VAR))
    if env_token is not None:
        return env_token
    return load_tokens().github_token


def apply_stored_token_env_defaults() -> None:
    tokens = load_tokens()
    if _normalize_token_value(os.getenv(OPENAI_ENV_VAR)) is None and tokens.openai_token is not None:
        os.environ[OPENAI_ENV_VAR] = tokens.openai_token
    if _normalize_token_value(os.getenv(ANTHROPIC_ENV_VAR)) is None and tokens.anthropic_token is not None:
        os.environ[ANTHROPIC_ENV_VAR] = tokens.anthropic_token
    if _normalize_token_value(os.getenv(GITHUB_ENV_VAR)) is None and tokens.github_token is not None:
        os.environ[GITHUB_ENV_VAR] = tokens.github_token
