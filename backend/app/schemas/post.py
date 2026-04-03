from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class PostRead(BaseModel):
    id: int
    post_id: str
    title: str
    title_annotation: str | None = None
    has_title_annotation: bool = False
    display_title: str
    published_at: datetime | None
    detail_url: str
    cover_url: str | None = None
    mega_url: str | None
    status: str
    operation_status: str
    primary_action: str
    download_failure_kind: str | None = None
    download_return_code: int | None = None
    last_error: str | None
    archive_path: str | None = None
    extract_dir: str | None = None
    can_open_local_path: bool = False
    needs_title_annotation: bool = False
    updated_at: datetime


class DownloadRequest(BaseModel):
    post_ids: list[str]


class RefreshRequest(BaseModel):
    mode: str = Field(default="incremental", pattern="^(incremental|full)$")
    auto_rescan_after: bool = False


class TitleAliasPair(BaseModel):
    source: str = Field(..., min_length=1, max_length=200)
    target: str = Field(..., min_length=1, max_length=200)


class TitleAliasUpsertRequest(BaseModel):
    post_id: str = Field(..., min_length=1)
    mode: str = Field(..., pattern="^(full_title|phrase_fragment|fragment)$")
    full_title_translation: str | None = Field(default=None, max_length=500)
    entries: list[TitleAliasPair] = Field(default_factory=list)


class TitleAliasUpsertResult(BaseModel):
    updated_count: int
    aliases_path: str
