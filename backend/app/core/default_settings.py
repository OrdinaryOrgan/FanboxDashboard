from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.config import AppConfig, get_config


def build_default_settings(config: AppConfig | None = None) -> dict[str, Any]:
    app_config = config or get_config()
    default_root = app_config.project_root / "backend" / "data"
    return {
        "creator_url": "https://www.fanbox.cc/",
        "profile_dir": str(default_root / "profile"),
        "download_dir": str(default_root / "downloads"),
        "library_dir": str(default_root / "library"),
        "temp_dir": str(default_root / "temp"),
        "mega_command": "mega-get",
        "playwright_channel": "msedge",
        "refresh_interval_minutes": 0,
        "download_concurrency": 5,
        "posts_per_row": 4,
        "auto_delete_archive": True,
        "llm_enabled": False,
        "llm_api_key": "",
        "llm_base_url": "https://api.deepseek.com",
        "llm_model": "deepseek-chat",
        "title_aliases_path": str(app_config.project_root / "backend" / "app" / "core" / "title_aliases.json"),
    }


def default_setting_value(name: str) -> Any:
    return build_default_settings()[name]


def is_empty_setting_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def default_title_aliases_path(config: AppConfig | None = None) -> Path:
    return Path(build_default_settings(config)["title_aliases_path"])
