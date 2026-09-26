"""User-specific, non-secret settings kept outside the source tree."""

from __future__ import annotations

import json
from pathlib import Path


CONFIG_PATH = Path.home() / ".config" / "flomo-vault" / "config.json"


def setting(name: str, default: str = "") -> str:
    if not CONFIG_PATH.is_file():
        return default
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"Invalid flomo-vault config: {CONFIG_PATH}") from error
    if not isinstance(data, dict):
        raise ValueError(f"Invalid flomo-vault config: {CONFIG_PATH}")
    value = data.get(name, default)
    if not isinstance(value, str):
        raise ValueError(f"Config field {name} must be a string")
    return value.strip()
