from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_config
from app.db.models import Base, Settings


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
    post_columns = {column["name"] for column in inspector.get_columns("posts")} if "posts" in inspector.get_table_names() else set()
    with engine.begin() as connection:
        if "cover_url" not in post_columns:
            connection.execute(text("ALTER TABLE posts ADD COLUMN cover_url VARCHAR(1000)"))
    task_columns = {column["name"] for column in inspector.get_columns("tasks")} if "tasks" in inspector.get_table_names() else set()
    with engine.begin() as connection:
        if "progress_current" not in task_columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN progress_current INTEGER"))
        if "progress_total" not in task_columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN progress_total INTEGER"))
        if "refresh_mode" not in task_columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN refresh_mode VARCHAR(20)"))
