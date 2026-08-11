from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_FILE = PROJECT_ROOT / "config" / "fund-compass.json"


def load_file_config() -> dict[str, Any]:
    configured_path = os.getenv("FUND_COMPASS_CONFIG_FILE", "").strip()
    path = Path(configured_path).expanduser() if configured_path else DEFAULT_CONFIG_FILE
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


FILE_CONFIG = load_file_config()


def config_value(section: str, key: str, default: Any = None, *, env_name: str | None = None) -> Any:
    """Read an environment override first, then the single local config file."""
    if env_name:
        value = os.getenv(env_name)
        if value is not None and value != "":
            return value
    section_data = FILE_CONFIG.get(section, {})
    if isinstance(section_data, dict) and key in section_data:
        return section_data[key]
    return default


class Settings(BaseSettings):
    mode: str = "REFERENCE"
    model_config = SettingsConfigDict(env_prefix="FUND_COMPASS_", case_sensitive=False, extra="ignore")


settings = Settings()
