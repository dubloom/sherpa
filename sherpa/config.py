from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sherpa.supported_models import SUPPORTED_MODEL

CONFIG_FILENAME = "config.json"
SUPPORTED_REASONING_EFFORTS = ("low", "medium", "high")

DEFAULT_REASONING_EFFORT = "low"
DEFAULT_MODEL = "gpt-5.3-codex"


@dataclass
class SherpaConfig:
    default_model: str = DEFAULT_MODEL
    default_reasoning_effort: str = DEFAULT_REASONING_EFFORT


def _sherpa_dir(repo_root: Path) -> Path:
    return repo_root / ".sherpa"


def config_path(repo_root: Path) -> Path:
    return _sherpa_dir(repo_root) / CONFIG_FILENAME


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _to_payload(config: SherpaConfig) -> dict[str, str]:
    return {
        "default_model": config.default_model,
        "default_reasoning_effort": config.default_reasoning_effort,
    }


def _load_from_payload(raw: dict[str, Any]) -> SherpaConfig:
    model = str(raw.get("default_model", DEFAULT_MODEL)).strip()
    if model not in SUPPORTED_MODEL:
        model = DEFAULT_MODEL

    reasoning_effort = str(raw.get("default_reasoning_effort", DEFAULT_REASONING_EFFORT)).strip().lower()
    if reasoning_effort not in SUPPORTED_REASONING_EFFORTS:
        reasoning_effort = DEFAULT_REASONING_EFFORT

    return SherpaConfig(
        default_model=model,
        default_reasoning_effort=reasoning_effort,
    )


def load_or_create_config(repo_root: Path) -> SherpaConfig:
    path = config_path(repo_root)
    if not path.is_file():
        default_config = SherpaConfig()
        _atomic_write_json(path, _to_payload(default_config))
        return default_config

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        default_config = SherpaConfig()
        _atomic_write_json(path, _to_payload(default_config))
        return default_config

    if not isinstance(raw, dict):
        default_config = SherpaConfig()
        _atomic_write_json(path, _to_payload(default_config))
        return default_config

    config = _load_from_payload(raw)

    # Keep config normalized and resilient over time.
    if raw != _to_payload(config):
        _atomic_write_json(path, _to_payload(config))

    return config
