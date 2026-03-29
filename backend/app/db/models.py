from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class PostStatus(StrEnum):
    NEW = "new"
    MISSING_LOCAL = "missing_local"
    QUEUED = "queued"
    RUNNING_DOWNLOAD = "running_download"
    RUNNING_EXTRACT = "running_extract"
    RUNNING_RENAME = "running_rename"
    COMPLETED = "completed"
    FAILED_PARSE = "failed_parse"
    FAILED_DOWNLOAD = "failed_download"
    FAILED_EXTRACT = "failed_extract"
    FAILED_RENAME = "failed_rename"
    AUTH_EXPIRED = "auth_expired"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING_DOWNLOAD = "running_download"
    RUNNING_EXTRACT = "running_extract"
    RUNNING_RENAME = "running_rename"
    RUNNING_REFRESH = "running_refresh"
    RUNNING_LOGIN = "running_login"
    COMPLETED = "completed"
    FAILED_DOWNLOAD = "failed_download"
    FAILED_EXTRACT = "failed_extract"
    FAILED_RENAME = "failed_rename"
    FAILED_PARSE = "failed_parse"
    FAILED_AUTH = "failed_auth"


class TaskKind(StrEnum):
    REFRESH_POSTS = "refresh_posts"
    OPEN_LOGIN = "open_login"
    DOWNLOAD_POST = "download_post"
    RESCAN_LIBRARY = "rescan_library"


class RefreshMode(StrEnum):
    INCREMENTAL = "incremental"
    FULL = "full"


class Settings(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    creator_url: Mapped[str] = mapped_column(String(500), default="https://www.fanbox.cc/")
    profile_dir: Mapped[str] = mapped_column(String(500), default="")
    download_dir: Mapped[str] = mapped_column(String(500), default="")
    library_dir: Mapped[str] = mapped_column(String(500), default="")
    temp_dir: Mapped[str] = mapped_column(String(500), default="")
    mega_command: Mapped[str] = mapped_column(String(100), default="mega-get")
    playwright_channel: Mapped[str] = mapped_column(String(50), default="msedge")
    refresh_interval_minutes: Mapped[int] = mapped_column(default=0)
    download_concurrency: Mapped[int] = mapped_column(default=5)
    posts_per_row: Mapped[int] = mapped_column(default=4)
    auto_delete_archive: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    post_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(500))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detail_url: Mapped[str] = mapped_column(String(1000))
    cover_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    mega_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default=PostStatus.NEW.value)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    artifacts: Mapped[list["Artifact"]] = relationship(back_populates="post", cascade="all, delete-orphan")
    tasks: Mapped[list["Task"]] = relationship(back_populates="post")


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("post_id", name="uq_artifact_post"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"))
    archive_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    extract_dir: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    processed_files: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    post: Mapped["Post"] = relationship(back_populates="artifacts")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(50), default=TaskStatus.QUEUED.value)
    post_id: Mapped[int | None] = mapped_column(ForeignKey("posts.id"), nullable=True, index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    log: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress_current: Mapped[int | None] = mapped_column(nullable=True)
    progress_total: Mapped[int | None] = mapped_column(nullable=True)
    refresh_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    post: Mapped[Post | None] = relationship(back_populates="tasks")
