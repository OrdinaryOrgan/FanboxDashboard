from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_config
from app.core.default_settings import build_default_settings, is_empty_setting_value
from app.db.models import Base, Post, PostOperationStatus, PostStatus, Settings, TitleAnnotationStatus


config = get_config()
config.data_dir.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{config.database_path}",
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _apply_runtime_migrations()
    with SessionLocal() as session:
        settings = session.get(Settings, 1)
        if settings is None:
            session.add(Settings(id=1, **build_default_settings(config)))
            session.commit()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _apply_runtime_migrations() -> None:
    defaults = build_default_settings(config)
    inspector = inspect(engine)
    if "settings" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("settings")}
    with engine.begin() as connection:
        if "auto_delete_archive" not in columns:
            connection.execute(
                text(
                    f"ALTER TABLE settings ADD COLUMN auto_delete_archive BOOLEAN NOT NULL DEFAULT {1 if defaults['auto_delete_archive'] else 0}"
                )
            )
        if "download_concurrency" not in columns:
            connection.execute(
                text(
                    f"ALTER TABLE settings ADD COLUMN download_concurrency INTEGER NOT NULL DEFAULT {int(defaults['download_concurrency'])}"
                )
            )
        if "posts_per_row" not in columns:
            connection.execute(
                text(f"ALTER TABLE settings ADD COLUMN posts_per_row INTEGER NOT NULL DEFAULT {int(defaults['posts_per_row'])}")
            )
        if "llm_enabled" not in columns:
            connection.execute(
                text(f"ALTER TABLE settings ADD COLUMN llm_enabled BOOLEAN NOT NULL DEFAULT {1 if defaults['llm_enabled'] else 0}")
            )
        if "llm_api_key" not in columns:
            connection.execute(text("ALTER TABLE settings ADD COLUMN llm_api_key VARCHAR(500)"))
        if "llm_base_url" not in columns:
            connection.execute(
                text(
                    "ALTER TABLE settings ADD COLUMN llm_base_url VARCHAR(500) "
                    f"NOT NULL DEFAULT '{defaults['llm_base_url']}'"
                )
            )
        if "llm_model" not in columns:
            connection.execute(
                text(f"ALTER TABLE settings ADD COLUMN llm_model VARCHAR(100) NOT NULL DEFAULT '{defaults['llm_model']}'")
            )
        if "title_aliases_path" not in columns:
            connection.execute(text("ALTER TABLE settings ADD COLUMN title_aliases_path VARCHAR(500) NOT NULL DEFAULT ''"))
    with SessionLocal() as session:
        settings = session.get(Settings, 1)
        updated = False
        if settings is not None:
            for field, value in defaults.items():
                current = getattr(settings, field, None)
                if is_empty_setting_value(current):
                    setattr(settings, field, None if field == "llm_api_key" and value == "" else value)
                    updated = True
        if updated:
            session.commit()
    post_columns = {column["name"] for column in inspector.get_columns("posts")} if "posts" in inspector.get_table_names() else set()
    with engine.begin() as connection:
        if "cover_url" not in post_columns:
            connection.execute(text("ALTER TABLE posts ADD COLUMN cover_url VARCHAR(1000)"))
        if "title_annotation" not in post_columns:
            connection.execute(text("ALTER TABLE posts ADD COLUMN title_annotation VARCHAR(500)"))
        if "title_annotation_source" not in post_columns:
            connection.execute(text("ALTER TABLE posts ADD COLUMN title_annotation_source VARCHAR(50)"))
        if "title_annotation_status" not in post_columns:
            connection.execute(
                text(
                    "ALTER TABLE posts ADD COLUMN title_annotation_status VARCHAR(30) NOT NULL DEFAULT "
                    f"'{TitleAnnotationStatus.PENDING.value}'"
                )
            )
        if "title_annotation_error" not in post_columns:
            connection.execute(text("ALTER TABLE posts ADD COLUMN title_annotation_error TEXT"))
        if "title_annotation_updated_at" not in post_columns:
            connection.execute(text("ALTER TABLE posts ADD COLUMN title_annotation_updated_at DATETIME"))
        if "operation_status" not in post_columns:
            connection.execute(
                text(
                    "ALTER TABLE posts ADD COLUMN operation_status VARCHAR(50) "
                    f"NOT NULL DEFAULT '{PostOperationStatus.IDLE.value}'"
                )
            )
        if "download_failure_kind" not in post_columns:
            connection.execute(text("ALTER TABLE posts ADD COLUMN download_failure_kind VARCHAR(30)"))
        if "download_return_code" not in post_columns:
            connection.execute(text("ALTER TABLE posts ADD COLUMN download_return_code INTEGER"))
    _migrate_post_statuses_to_inventory_and_operation()
    task_columns = {column["name"] for column in inspector.get_columns("tasks")} if "tasks" in inspector.get_table_names() else set()
    with engine.begin() as connection:
        if "progress_current" not in task_columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN progress_current INTEGER"))
        if "progress_total" not in task_columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN progress_total INTEGER"))
        if "refresh_mode" not in task_columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN refresh_mode VARCHAR(20)"))
        if "download_failure_kind" not in task_columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN download_failure_kind VARCHAR(30)"))
        if "download_return_code" not in task_columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN download_return_code INTEGER"))


def _migrate_post_statuses_to_inventory_and_operation() -> None:
    with SessionLocal() as session:
        posts = session.query(Post).all()
        updated = False
        for post in posts:
            inventory_status, operation_status = _split_legacy_post_status(
                status=post.status,
                mega_url=post.mega_url,
                current_operation_status=post.operation_status,
            )
            if post.status != inventory_status:
                post.status = inventory_status
                updated = True
            if post.operation_status != operation_status:
                post.operation_status = operation_status
                updated = True
        if updated:
            session.commit()


def _split_legacy_post_status(
    status: str | None,
    mega_url: str | None,
    current_operation_status: str | None,
) -> tuple[str, str]:
    fallback_inventory = PostStatus.MISSING_LOCAL.value if mega_url else PostStatus.FAILED_PARSE.value
    normalized_status = (status or "").strip() or PostStatus.NEW.value
    normalized_operation_status = (current_operation_status or "").strip() or PostOperationStatus.IDLE.value

    if normalized_status in {
        PostStatus.NEW.value,
        PostStatus.MISSING_LOCAL.value,
        PostStatus.COMPLETED.value,
        PostStatus.FAILED_PARSE.value,
        PostStatus.AUTH_EXPIRED.value,
    }:
        return normalized_status, normalized_operation_status

    operation_status_map = {
        PostOperationStatus.QUEUED.value: PostOperationStatus.QUEUED.value,
        PostOperationStatus.RUNNING_DOWNLOAD.value: PostOperationStatus.RUNNING_DOWNLOAD.value,
        PostOperationStatus.RUNNING_EXTRACT.value: PostOperationStatus.RUNNING_EXTRACT.value,
        PostOperationStatus.RUNNING_RENAME.value: PostOperationStatus.RUNNING_RENAME.value,
        PostOperationStatus.FAILED_CONFIG.value: PostOperationStatus.FAILED_CONFIG.value,
        PostOperationStatus.FAILED_DOWNLOAD.value: PostOperationStatus.FAILED_DOWNLOAD.value,
        PostOperationStatus.FAILED_EXTRACT.value: PostOperationStatus.FAILED_EXTRACT.value,
        PostOperationStatus.FAILED_RENAME.value: PostOperationStatus.FAILED_RENAME.value,
    }
    if normalized_status in operation_status_map:
        return fallback_inventory, operation_status_map[normalized_status]

    return fallback_inventory, normalized_operation_status
