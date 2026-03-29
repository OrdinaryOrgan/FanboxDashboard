import asyncio
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile

from app.db.models import Post, PostStatus, RefreshMode, Settings, Task, TaskKind, TaskStatus
from app.db.session import SessionLocal, engine, init_db
from app.services.mega import MegaDownloadError
from app.services.tasks import TaskManager, _resolve_download_concurrency, _resolve_download_timeout_seconds

TEST_DB = Path(__file__).resolve().parents[1] / "data" / "test.db"


def setup_function() -> None:
    engine.dispose()
    if TEST_DB.exists():
        TEST_DB.unlink()
    init_db()


def test_download_archive_with_retries_recovers_from_server_231(monkeypatch) -> None:
    attempts = {"count": 0}

    async def fake_download_public_link(link: str, download_dir: str, command_name: str):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise MegaDownloadError("Failed to access server: 231")
        return "C:/tmp/sample.zip", "download ok"

    async def fake_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("app.services.tasks.download_public_link", fake_download_public_link)
    monkeypatch.setattr("app.services.tasks.asyncio.sleep", fake_sleep)

    manager = TaskManager()
    archive_path, log_output = asyncio.run(
        manager._download_archive_with_retries("https://mega.nz/file/test", "C:/tmp", "mega-get")
    )

    assert attempts["count"] == 3
    assert archive_path == "C:/tmp/sample.zip"
    assert "download ok" in log_output
    assert "Retrying MEGA download" in log_output


def test_resolve_download_concurrency_uses_env_value(monkeypatch) -> None:
    monkeypatch.setenv("FANBOX_DOWNLOAD_CONCURRENCY", "4")
    assert _resolve_download_concurrency() == 4


def test_resolve_download_concurrency_falls_back_to_default(monkeypatch) -> None:
    monkeypatch.setenv("FANBOX_DOWNLOAD_CONCURRENCY", "invalid")
    assert _resolve_download_concurrency() == 5


def test_resolve_download_concurrency_defaults_to_five(monkeypatch) -> None:
    monkeypatch.delenv("FANBOX_DOWNLOAD_CONCURRENCY", raising=False)
    assert _resolve_download_concurrency() == 5


def test_resolve_download_timeout_uses_env_value(monkeypatch) -> None:
    monkeypatch.setenv("FANBOX_DOWNLOAD_TIMEOUT_SECONDS", "45")
    assert _resolve_download_timeout_seconds() == 45


def test_enqueue_refresh_reuses_existing_active_task() -> None:
    init_db()
    with SessionLocal() as session:
        session.add(
            Task(
                id="existing-refresh-task",
                kind=TaskKind.REFRESH_POSTS.value,
                status=TaskStatus.RUNNING_REFRESH.value,
                refresh_mode=RefreshMode.FULL.value,
            )
        )
        session.commit()

    manager = TaskManager()
    task_id = manager.enqueue_refresh(RefreshMode.INCREMENTAL)

    assert task_id == "existing-refresh-task"

    with SessionLocal() as session:
        assert session.query(Task).count() == 1


def test_reconcile_interrupted_refresh_marks_task_failed() -> None:
    with SessionLocal() as session:
        session.add(
            Task(
                id="interrupted-refresh-task",
                kind=TaskKind.REFRESH_POSTS.value,
                status=TaskStatus.RUNNING_REFRESH.value,
                refresh_mode=RefreshMode.INCREMENTAL.value,
                started_at=datetime.utcnow(),
            )
        )
        session.commit()

    manager = TaskManager()
    manager.reconcile_interrupted_tasks()

    with SessionLocal() as session:
        task = session.get(Task, "interrupted-refresh-task")
        assert task is not None
        assert task.status == TaskStatus.FAILED_PARSE.value
        assert task.error == "Task was interrupted because the app stopped before it finished."
        assert task.finished_at is not None


def test_reconcile_interrupted_download_resets_post_state() -> None:
    with SessionLocal() as session:
        post = Post(
            post_id="99900001",
            title="stuck download",
            published_at=datetime.utcnow(),
            detail_url="https://example.fanbox.cc/posts/99900001",
            mega_url="https://mega.nz/file/test",
            status=PostStatus.RUNNING_DOWNLOAD.value,
        )
        session.add(post)
        session.flush()
        session.add(
            Task(
                id="interrupted-download-task",
                kind=TaskKind.DOWNLOAD_POST.value,
                status=TaskStatus.RUNNING_DOWNLOAD.value,
                post_id=post.id,
                started_at=datetime.utcnow(),
            )
        )
        session.commit()

    manager = TaskManager()
    manager.reconcile_interrupted_tasks()

    with SessionLocal() as session:
        task = session.get(Task, "interrupted-download-task")
        post = session.query(Post).filter_by(post_id="99900001").one()
        assert task is not None
        assert task.status == TaskStatus.FAILED_DOWNLOAD.value
        assert post.status == PostStatus.MISSING_LOCAL.value
        assert post.last_error == "Task was interrupted because the app stopped before it finished."


def test_run_download_times_out_and_marks_task_failed(monkeypatch) -> None:
    with SessionLocal() as session:
        post = Post(
            post_id="99900002",
            title="timeout download",
            published_at=datetime.utcnow(),
            detail_url="https://example.fanbox.cc/posts/99900002",
            mega_url="https://mega.nz/file/test-timeout",
            status=PostStatus.QUEUED.value,
        )
        session.add(post)
        session.flush()
        session.add(
            Task(
                id="timeout-download-task",
                kind=TaskKind.DOWNLOAD_POST.value,
                status=TaskStatus.QUEUED.value,
                post_id=post.id,
            )
        )
        session.commit()
        post_db_id = post.id

    async def fake_download_archive_with_retries(self, mega_url: str, download_dir: str, mega_command: str) -> tuple[str, str]:
        await asyncio.sleep(2)
        return "", ""

    monkeypatch.setattr("app.services.tasks._resolve_download_timeout_seconds", lambda: 1)
    monkeypatch.setattr(TaskManager, "_download_archive_with_retries", fake_download_archive_with_retries)

    manager = TaskManager()
    asyncio.run(manager._run_download("timeout-download-task", post_db_id))

    with SessionLocal() as session:
        task = session.get(Task, "timeout-download-task")
        post = session.query(Post).filter_by(post_id="99900002").one()
        assert task is not None
        assert task.status == TaskStatus.FAILED_DOWNLOAD.value
        assert "timed out" in (task.error or "")
        assert post.status == PostStatus.FAILED_DOWNLOAD.value
        assert "timed out" in (post.last_error or "")


def test_run_download_reuses_existing_archive_without_calling_mega(monkeypatch, tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    temp_root = tmp_path / "temp"
    library_root.mkdir(parents=True)
    temp_root.mkdir(parents=True)

    with SessionLocal() as session:
        settings = session.get(Settings, 1)
        assert settings is not None
        settings.library_dir = str(library_root)
        settings.download_dir = str(tmp_path / "downloads")
        settings.temp_dir = str(temp_root)
        settings.auto_delete_archive = False

        post = Post(
            post_id="99900003",
            title="existing",
            published_at=datetime.utcnow(),
            detail_url="https://example.fanbox.cc/posts/99900003",
            mega_url="https://mega.nz/file/existing-archive",
            status=PostStatus.QUEUED.value,
        )
        session.add(post)
        session.flush()
        session.add(
            Task(
                id="reuse-archive-task",
                kind=TaskKind.DOWNLOAD_POST.value,
                status=TaskStatus.QUEUED.value,
                post_id=post.id,
            )
        )
        session.commit()
        post_db_id = post.id
        year = str(post.published_at.year)

    archive_dir = library_root / year / "Archive"
    archive_dir.mkdir(parents=True)
    archive_path = archive_dir / "existingC.zip"
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("A2/existing1.jpg", b"jpg")

    async def fail_if_called(self, mega_url: str, download_dir: str, mega_command: str) -> tuple[str, str]:
        raise AssertionError("MEGA download should not run when a matching archive already exists")

    monkeypatch.setattr(TaskManager, "_download_archive_with_retries", fail_if_called)

    manager = TaskManager()
    asyncio.run(manager._run_download("reuse-archive-task", post_db_id))

    with SessionLocal() as session:
        task = session.get(Task, "reuse-archive-task")
        post = session.query(Post).filter_by(post_id="99900003").one()
        assert task is not None
        assert task.status == TaskStatus.COMPLETED.value
        assert "Reused existing archive" in (task.log or "")
        assert post.status == PostStatus.COMPLETED.value

    extracted = list((library_root / year / "existing").rglob("*.jpg"))
    assert extracted
