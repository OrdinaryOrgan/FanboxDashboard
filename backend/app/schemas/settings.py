from __future__ import annotations

from pydantic import BaseModel, Field

from app.core.default_settings import build_default_settings


SETTINGS_DEFAULTS = build_default_settings()


class SettingsRead(BaseModel):
    creator_url: str
    profile_dir: str
    download_dir: str
    library_dir: str
    temp_dir: str
    mega_command: str
    playwright_channel: str
    refresh_interval_minutes: int
    download_concurrency: int
    posts_per_row: int
    auto_delete_archive: bool
    llm_enabled: bool
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    title_aliases_path: str


class SettingsUpdate(BaseModel):
    creator_url: str = Field(default=SETTINGS_DEFAULTS["creator_url"], min_length=1)
    profile_dir: str = Field(default=SETTINGS_DEFAULTS["profile_dir"], min_length=1)
    download_dir: str = Field(default=SETTINGS_DEFAULTS["download_dir"], min_length=1)
    library_dir: str = Field(default=SETTINGS_DEFAULTS["library_dir"], min_length=1)
    temp_dir: str = Field(default=SETTINGS_DEFAULTS["temp_dir"], min_length=1)
    mega_command: str = Field(default=SETTINGS_DEFAULTS["mega_command"], min_length=1)
    playwright_channel: str = Field(default=SETTINGS_DEFAULTS["playwright_channel"], min_length=1)
    refresh_interval_minutes: int = Field(default=SETTINGS_DEFAULTS["refresh_interval_minutes"], ge=0, le=1440)
    download_concurrency: int = Field(default=SETTINGS_DEFAULTS["download_concurrency"], ge=1, le=8)
    posts_per_row: int = Field(default=SETTINGS_DEFAULTS["posts_per_row"], ge=3, le=6)
    auto_delete_archive: bool = SETTINGS_DEFAULTS["auto_delete_archive"]
    llm_enabled: bool = SETTINGS_DEFAULTS["llm_enabled"]
    llm_api_key: str = Field(default="", max_length=500)
    llm_base_url: str = Field(default=SETTINGS_DEFAULTS["llm_base_url"], min_length=1, max_length=500)
    llm_model: str = Field(default=SETTINGS_DEFAULTS["llm_model"], min_length=1, max_length=100)
    title_aliases_path: str = Field(default=SETTINGS_DEFAULTS["title_aliases_path"], min_length=1, max_length=500)


class AuthStatus(BaseModel):
    authenticated: bool
    profile_exists: bool
    reason: str | None = None


class TitleAliasesClearResult(BaseModel):
    cleared_path: str


class TitleAnnotationCacheClearResult(BaseModel):
    reset_count: int
