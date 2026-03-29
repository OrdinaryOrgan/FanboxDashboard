from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel


DataT = TypeVar("DataT")


class Envelope(BaseModel, Generic[DataT]):
    success: bool = True
    message: str = "ok"
    data: DataT | None = None


class TaskResponse(BaseModel):
    task_id: str | None = None
    task_ids: list[str] | None = None
    task_status: str | None = None
    last_error: str | None = None
