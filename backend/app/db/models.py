from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.default_settings import default_setting_value


class Base(DeclarativeBase):
    pass


class PostStatus(StrEnum):
    NEW = "new"
    MISSING_LOCAL = "missing_local"
    COMPLETED = "completed"
    FAILED_PARSE = "failed_parse"
    AUTH_EXPIRED = "auth_expired"


class PostOperationStatus(StrEnum):
    IDLE = "idle"
    QUEUED = "queued"
    RUNNING_DOWNLOAD = "running_download"
    RUNNING_EXTRACT = "running_extract"
    RUNNING_RENAME = "running_rename"
    FAILED_CONFIG = "failed_config"
    FAILED_DOWNLOAD = "failed_download"
    FAILED_EXTRACT = "failed_extract"
    FAILED_RENAME = "failed_rename"


class PostPrimaryAction(StrEnum):
    DOWNLOAD = "download"
    EXTRACT = "extract"
    REDOWNLOAD = "redownload"
    STALE_LINK = "stale_link"
    COMPLETED = "completed"


class DownloadFailureKind(StrEnum):
    RETRYABLE = "retryable"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING_DOWNLOAD = "running_download"
    RUNNING_EXTRACT = "running_extract"
    RUNNING_RENAME = "running_rename"
    RUNNING_REFRESH = "running_refresh"
    RUNNING_ANNOTATE = "running_annotate"
    RUNNING_LOGIN = "running_login"
    COMPLETED = "completed"
    FAILED_CONFIG = "failed_config"
    FAILED_DOWNLOAD = "failed_download"
    FAILED_EXTRACT = "failed_extract"
    FAILED_RENAME = "failed_rename"
    FAILED_PARSE = "failed_parse"
    FAILED_ANNOTATE = "failed_annotate"
    FAILED_AUTH = "failed_auth"


class TaskKind(StrEnum):
    REFRESH_POSTS = "refresh_posts"
    ANNOTATE_TITLES = "annotate_titles"
    OPEN_LOGIN = "open_login"
    DOWNLOAD_POST = "download_post"
    RESCAN_LIBRARY = "rescan_library"


class RefreshMode(StrEnum):
    INCREMENTAL = "incremental"
    FULL = "full"


class TitleAnnotationStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


class TitleAnnotationSource(StrEnum):
    NONE = "none"
    DICTIONARY_FULL = "dictionary_full"
    DICTIONARY_PHRASE = "dictionary_phrase"
    DICTIONARY_FRAGMENT = "dictionary_fragment"
    LLM = "llm"
    LLM_CACHED = "llm_cached"


class Settings(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    creator_url: Mapped[str] = mapped_column(String(500), default=lambda: str(default_setting_value("creator_url")))
    profile_dir: Mapped[str] = mapped_column(String(500), default=lambda: str(default_setting_value("profile_dir")))
    download_dir: Mapped[str] = mapped_column(String(500), default=lambda: str(default_setting_value("download_dir")))
    library_dir: Mapped[str] = mapped_column(String(500), default=lambda: str(default_setting_value("library_dir")))
    temp_dir: Mapped[str] = mapped_column(String(500), default=lambda: str(default_setting_value("temp_dir")))
    mega_command: Mapped[str] = mapped_column(String(100), default=lambda: str(default_setting_value("mega_command")))
    playwright_channel: Mapped[str] = mapped_column(
        String(50), default=lambda: str(default_setting_value("playwright_channel"))
    )
    refresh_interval_minutes: Mapped[int] = mapped_column(default=lambda: int(default_setting_value("refresh_interval_minutes")))
    download_concurrency: Mapped[int] = mapped_column(default=lambda: int(default_setting_value("download_concurrency")))
    posts_per_row: Mapped[int] = mapped_column(default=lambda: int(default_setting_value("posts_per_row")))
    auto_delete_archive: Mapped[bool] = mapped_column(Boolean, default=lambda: bool(default_setting_value("auto_delete_archive")))
    llm_enabled: Mapped[bool] = mapped_column(Boolean, default=lambda: bool(default_setting_value("llm_enabled")))
    llm_api_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    llm_base_url: Mapped[str] = mapped_column(String(500), default=lambda: str(default_setting_value("llm_base_url")))
    llm_model: Mapped[str] = mapped_column(String(100), default=lambda: str(default_setting_value("llm_model")))
    title_aliases_path: Mapped[str] = mapped_column(
        String(500), default=lambda: str(default_setting_value("title_aliases_path"))
    )
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
    title_annotation: Mapped[str | None] = mapped_column(String(500), nullable=True)
    title_annotation_source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    title_annotation_status: Mapped[str] = mapped_column(String(30), default=TitleAnnotationStatus.PENDING.value)
    title_annotation_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    title_annotation_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default=PostStatus.NEW.value)
    operation_status: Mapped[str] = mapped_column(String(50), default=PostOperationStatus.IDLE.value)
    download_failure_kind: Mapped[str | None] = mapped_column(String(30), nullable=True)
    download_return_code: Mapped[int | None] = mapped_column(nullable=True)
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
    download_failure_kind: Mapped[str | None] = mapped_column(String(30), nullable=True)
    download_return_code: Mapped[int | None] = mapped_column(nullable=True)
    log: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress_current: Mapped[int | None] = mapped_column(nullable=True)
    progress_total: Mapped[int | None] = mapped_column(nullable=True)
    refresh_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    post: Mapped[Post | None] = relationship(back_populates="tasks")
