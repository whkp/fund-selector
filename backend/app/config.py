from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_FILE = PROJECT_ROOT / "config" / "fund-compass.json"


def config_file_path() -> Path:
    configured_path = os.getenv("FUND_COMPASS_CONFIG_FILE", "").strip()
    path = Path(configured_path).expanduser() if configured_path else DEFAULT_CONFIG_FILE
    return path if path.is_absolute() else PROJECT_ROOT / path


CONFIG_FILE_PATH = config_file_path()


def load_file_config() -> dict[str, Any]:
    try:
        payload = json.loads(CONFIG_FILE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


FILE_CONFIG = load_file_config()


def llm_configuration_source() -> str:
    env_names = (
        "FUND_COMPASS_LLM_PROVIDER", "FUND_COMPASS_LLM_MODEL", "FUND_COMPASS_LLM_BASE_URL",
        "FUND_COMPASS_LLM_API_KEY", "FUND_COMPASS_LLM_TIMEOUT_SECONDS",
    )
    if any(os.getenv(name) not in (None, "") for name in env_names):
        return "environment"
    if isinstance(FILE_CONFIG.get("llm"), dict):
        return "config-file"
    return "default"


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
