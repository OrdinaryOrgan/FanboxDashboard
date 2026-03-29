from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class PostRead(BaseModel):
    id: int
    post_id: str
    title: str
    published_at: datetime | None
    detail_url: str
    cover_url: str | None = None
    mega_url: str | None
    status: str
    last_error: str | None
    archive_path: str | None = None
    extract_dir: str | None = None
    can_open_local_path: bool = False
    updated_at: datetime


class DownloadRequest(BaseModel):
    post_ids: list[str]


class RefreshRequest(BaseModel):
    mode: str = Field(default="incremental", pattern="^(incremental|full)$")
