from __future__ import annotations

from pydantic import BaseModel, Field


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


class SettingsUpdate(BaseModel):
    creator_url: str = Field(..., min_length=1)
    profile_dir: str = Field(..., min_length=1)
    download_dir: str = Field(..., min_length=1)
    library_dir: str = Field(..., min_length=1)
    temp_dir: str = Field(..., min_length=1)
    mega_command: str = Field(default="mega-get", min_length=1)
    playwright_channel: str = Field(default="msedge", min_length=1)
    refresh_interval_minutes: int = Field(default=0, ge=0, le=1440)
    download_concurrency: int = Field(default=5, ge=1, le=8)
    posts_per_row: int = Field(default=4, ge=3, le=6)
    auto_delete_archive: bool = True


class AuthStatus(BaseModel):
    authenticated: bool
    profile_exists: bool
    reason: str | None = None
