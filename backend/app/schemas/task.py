from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class TaskRead(BaseModel):
    id: str
    kind: str
    status: str
    refresh_mode: str | None
    post_id: int | None
    message: str | None
    error: str | None
    log: str | None
    progress_current: int | None
    progress_total: int | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RetryRequest(BaseModel):
    task_id: str


class TaskClearResult(BaseModel):
    deleted_count: int
