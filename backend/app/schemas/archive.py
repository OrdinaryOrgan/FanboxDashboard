from __future__ import annotations

from pydantic import BaseModel, Field


class ArchiveDeleteRequest(BaseModel):
    post_id: str = Field(..., min_length=1)


class ArchivePurgeRequest(BaseModel):
    year: str | None = Field(default=None)


class YearOpenRequest(BaseModel):
    year: str = Field(..., min_length=1)


class ArchiveActionResult(BaseModel):
    deleted_count: int
    deleted_paths: list[str]


class OpenPathResult(BaseModel):
    opened_path: str
