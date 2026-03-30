from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_config
from app.db.models import Base, Settings, TitleAnnotationStatus


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
            default_root = config.project_root / "backend" / "data"
            session.add(
                Settings(
                    id=1,
                    profile_dir=str(default_root / "profile"),
                    download_dir=str(default_root / "downloads"),
                    library_dir=r"D:\hmoe\Siu",
                    temp_dir=str(default_root / "temp"),
                    download_concurrency=5,
                    posts_per_row=4,
                    auto_delete_archive=True,
                    llm_enabled=False,
                    llm_base_url="https://api.deepseek.com",
                    llm_model="deepseek-chat",
                    title_aliases_path=str(config.project_root / "backend" / "app" / "core" / "title_aliases.json"),
                )
            )
            session.commit()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _apply_runtime_migrations() -> None:
    inspector = inspect(engine)
    if "settings" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("settings")}
    with engine.begin() as connection:
        if "auto_delete_archive" not in columns:
            connection.execute(
                text("ALTER TABLE settings ADD COLUMN auto_delete_archive BOOLEAN NOT NULL DEFAULT 1")
            )
        if "download_concurrency" not in columns:
            connection.execute(text("ALTER TABLE settings ADD COLUMN download_concurrency INTEGER NOT NULL DEFAULT 5"))
        if "posts_per_row" not in columns:
            connection.execute(text("ALTER TABLE settings ADD COLUMN posts_per_row INTEGER NOT NULL DEFAULT 4"))
        if "llm_enabled" not in columns:
            connection.execute(text("ALTER TABLE settings ADD COLUMN llm_enabled BOOLEAN NOT NULL DEFAULT 0"))
        if "llm_api_key" not in columns:
            connection.execute(text("ALTER TABLE settings ADD COLUMN llm_api_key VARCHAR(500)"))
        if "llm_base_url" not in columns:
            connection.execute(
                text(
                    "ALTER TABLE settings ADD COLUMN llm_base_url VARCHAR(500) "
                    "NOT NULL DEFAULT 'https://api.deepseek.com'"
                )
            )
        if "llm_model" not in columns:
            connection.execute(
                text("ALTER TABLE settings ADD COLUMN llm_model VARCHAR(100) NOT NULL DEFAULT 'deepseek-chat'")
            )
        if "title_aliases_path" not in columns:
            connection.execute(text("ALTER TABLE settings ADD COLUMN title_aliases_path VARCHAR(500) NOT NULL DEFAULT ''"))
    with SessionLocal() as session:
        settings = session.get(Settings, 1)
        if settings is not None and not settings.title_aliases_path:
            settings.title_aliases_path = str(config.project_root / "backend" / "app" / "core" / "title_aliases.json")
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
    task_columns = {column["name"] for column in inspector.get_columns("tasks")} if "tasks" in inspector.get_table_names() else set()
    with engine.begin() as connection:
        if "progress_current" not in task_columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN progress_current INTEGER"))
        if "progress_total" not in task_columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN progress_total INTEGER"))
        if "refresh_mode" not in task_columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN refresh_mode VARCHAR(20)"))
