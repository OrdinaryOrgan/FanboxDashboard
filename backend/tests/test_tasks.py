import asyncio
from datetime import datetime
from pathlib import Path
import threading
import time
from zipfile import ZipFile

from app.db.models import (
    Artifact,
    DownloadFailureKind,
    Post,
    PostOperationStatus,
    PostStatus,
    RefreshMode,
    Settings,
    Task,
    TaskKind,
    TaskStatus,
    TitleAnnotationSource,
    TitleAnnotationStatus,
)
from app.db.session import SessionLocal, engine, init_db
from app.services.mega import MegaCommandConfigurationError, MegaDownloadError
from app.services.fanbox import FanboxConfigurationError, ScrapedPost
from app.services.title_annotation import TitleAnnotationResult
from app.services.tasks import (
    TaskManager,
    _resolve_annotation_concurrency,
    _resolve_download_concurrency,
    _resolve_download_timeout_seconds,
)

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
            raise MegaDownloadError("Failed to access server: 231", return_code=3)
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


def test_resolve_annotation_concurrency_defaults_to_five(monkeypatch) -> None:
    monkeypatch.delenv("FANBOX_ANNOTATION_CONCURRENCY", raising=False)
    assert _resolve_annotation_concurrency() == 5


def test_resolve_annotation_concurrency_uses_env_value(monkeypatch) -> None:
    monkeypatch.setenv("FANBOX_ANNOTATION_CONCURRENCY", "3")
    assert _resolve_annotation_concurrency() == 3


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


def test_enqueue_rescan_reuses_existing_active_task() -> None:
    init_db()
    with SessionLocal() as session:
        session.add(
            Task(
                id="existing-rescan-task",
                kind=TaskKind.RESCAN_LIBRARY.value,
                status=TaskStatus.RUNNING_REFRESH.value,
            )
        )
        session.commit()

    manager = TaskManager()
    task_id = manager.enqueue_rescan()

    assert task_id == "existing-rescan-task"

    with SessionLocal() as session:
        assert session.query(Task).filter(Task.kind == TaskKind.RESCAN_LIBRARY.value).count() == 1


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
            status=PostStatus.MISSING_LOCAL.value,
            operation_status=PostOperationStatus.RUNNING_DOWNLOAD.value,
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
        assert task.download_failure_kind == DownloadFailureKind.RETRYABLE.value
        assert post.status == PostStatus.MISSING_LOCAL.value
        assert post.operation_status == PostOperationStatus.FAILED_DOWNLOAD.value
        assert post.last_error == "Task was interrupted because the app stopped before it finished."
        assert post.download_failure_kind == DownloadFailureKind.RETRYABLE.value


def test_run_download_times_out_and_marks_task_failed(monkeypatch) -> None:
    with SessionLocal() as session:
        post = Post(
            post_id="99900002",
            title="timeout download",
            published_at=datetime.utcnow(),
            detail_url="https://example.fanbox.cc/posts/99900002",
            mega_url="https://mega.nz/file/test-timeout",
            status=PostStatus.MISSING_LOCAL.value,
            operation_status=PostOperationStatus.QUEUED.value,
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
        assert task.download_failure_kind == DownloadFailureKind.RETRYABLE.value
        assert post.status == PostStatus.MISSING_LOCAL.value
        assert post.operation_status == PostOperationStatus.FAILED_DOWNLOAD.value
        assert "timed out" in (post.last_error or "")
        assert post.download_failure_kind == DownloadFailureKind.RETRYABLE.value


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
            status=PostStatus.MISSING_LOCAL.value,
            operation_status=PostOperationStatus.QUEUED.value,
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

    archive_dir = tmp_path / "downloads" / year
    archive_dir.mkdir(parents=True)
    archive_path = archive_dir / "existingC.zip"
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("A2/existing1.jpg", b"jpg")

    async def fail_if_called(self, mega_url: str, download_dir: str, mega_command: str) -> tuple[str, str]:
        raise AssertionError("MEGA download should not run when a matching archive already exists")

    captured_during_extract: dict[str, str] = {}

    def fake_extract_zip(archive_path_arg: str, extract_dir_arg: str) -> None:
        with SessionLocal() as session:
            task = session.get(Task, "reuse-archive-task")
            post = session.query(Post).filter_by(post_id="99900003").one()
            assert task is not None
            captured_during_extract["task_status"] = task.status
            captured_during_extract["task_message"] = task.message or ""
            captured_during_extract["post_operation_status"] = post.operation_status

        target_dir = Path(extract_dir_arg)
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "existing1.jpg").write_bytes(b"jpg")

    monkeypatch.setattr(TaskManager, "_download_archive_with_retries", fail_if_called)
    monkeypatch.setattr("app.services.tasks.extract_zip", fake_extract_zip)
    monkeypatch.setattr("app.services.tasks.rename_images", lambda path: [])
    monkeypatch.setattr(
        TaskManager,
        "_mark_completed",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("success path should finalize task in the same commit")),
    )

    manager = TaskManager()
    asyncio.run(manager._run_download("reuse-archive-task", post_db_id))

    with SessionLocal() as session:
        task = session.get(Task, "reuse-archive-task")
        post = session.query(Post).filter_by(post_id="99900003").one()
        assert task is not None
        assert task.status == TaskStatus.COMPLETED.value
        assert task.finished_at is not None
        assert task.message == "Archive processing finished."
        assert "Reused existing archive" in (task.log or "")
        assert post.status == PostStatus.COMPLETED.value
        assert post.operation_status == PostOperationStatus.IDLE.value
        assert captured_during_extract["task_status"] == TaskStatus.RUNNING_EXTRACT.value
        assert captured_during_extract["post_operation_status"] == PostOperationStatus.RUNNING_EXTRACT.value
        assert "Extracting" in captured_during_extract["task_message"]


def test_sync_local_status_recognizes_unicode_normalized_local_paths(tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    download_root = tmp_path / "downloads"
    library_root.mkdir(parents=True)
    download_root.mkdir(parents=True)

    with SessionLocal() as session:
        settings = session.get(Settings, 1)
        assert settings is not None
        settings.library_dir = str(library_root)
        settings.download_dir = str(download_root)

        post = Post(
            post_id="11649005",
            title="大鳳xフリードリヒ・デア・グローセ①",
            published_at=datetime(2026, 3, 31, 8, 0),
            detail_url="https://example.fanbox.cc/posts/11649005",
            mega_url="https://mega.nz/file/test",
            status=PostStatus.MISSING_LOCAL.value,
            operation_status=PostOperationStatus.FAILED_DOWNLOAD.value,
            last_error="previous failure",
        )
        session.add(post)
        session.flush()
        session.add(
            Artifact(
                post_id=post.id,
                archive_path=None,
                extract_dir=str(library_root / "2026" / "大鳳xフリードリヒ・デア・グローセ①"),
                processed_files="[]",
            )
        )
        session.commit()
        post_db_id = post.id

    existing_extract_dir = library_root / "2026" / "大鳳xフリードリヒ・デア・グローセ①"
    existing_extract_dir.mkdir(parents=True)
    (existing_extract_dir / "sample.jpg").write_bytes(b"jpg")

    archive_path = download_root / "2026" / "大鳳xフリードリヒ・デア・グローセ①.zip"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_bytes(b"zip")

    manager = TaskManager()
    with SessionLocal() as session:
        settings = session.get(Settings, 1)
        post = session.get(Post, post_db_id)
        assert settings is not None and post is not None
        manager._sync_local_status(session, settings, post)
        session.commit()

    with SessionLocal() as session:
        post = session.get(Post, post_db_id)
        artifact = session.query(Artifact).filter_by(post_id=post_db_id).one()
        assert post is not None
        assert post.status == PostStatus.COMPLETED.value
        assert post.operation_status == PostOperationStatus.IDLE.value
        assert post.last_error is None
        assert artifact.archive_path == str(archive_path.resolve())
        assert artifact.extract_dir == str(existing_extract_dir.resolve())


def test_run_download_marks_configuration_errors_separately(monkeypatch) -> None:
    with SessionLocal() as session:
        post = Post(
            post_id="99900004",
            title="download config",
            published_at=datetime.utcnow(),
            detail_url="https://example.fanbox.cc/posts/99900004",
            mega_url="https://mega.nz/file/config",
            status=PostStatus.MISSING_LOCAL.value,
            operation_status=PostOperationStatus.QUEUED.value,
        )
        session.add(post)
        session.flush()
        session.add(
            Task(
                id="download-config-task",
                kind=TaskKind.DOWNLOAD_POST.value,
                status=TaskStatus.QUEUED.value,
                post_id=post.id,
            )
        )
        session.commit()
        post_db_id = post.id

    async def fake_download_public_link(*args, **kwargs):
        raise MegaCommandConfigurationError("当前下载命令不可用，请先在设置中填写可用的 mega-get 可执行文件或目录。")

    monkeypatch.setattr("app.services.tasks.download_public_link", fake_download_public_link)

    manager = TaskManager()
    asyncio.run(manager._run_download("download-config-task", post_db_id))

    with SessionLocal() as session:
        task = session.get(Task, "download-config-task")
        post = session.query(Post).filter_by(post_id="99900004").one()
        assert task is not None
        assert task.status == TaskStatus.FAILED_CONFIG.value
        assert task.error == "当前下载命令不可用，请先在设置中填写可用的 mega-get 可执行文件或目录。"
        assert post.status == PostStatus.MISSING_LOCAL.value
        assert post.operation_status == PostOperationStatus.FAILED_CONFIG.value
        assert post.last_error == "当前下载命令不可用，请先在设置中填写可用的 mega-get 可执行文件或目录。"


def test_run_download_marks_unavailable_links_as_irrecoverable(monkeypatch, tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    download_root = tmp_path / "downloads"
    temp_root = tmp_path / "temp"
    library_root.mkdir(parents=True)
    download_root.mkdir(parents=True)
    temp_root.mkdir(parents=True)

    with SessionLocal() as session:
        settings = session.get(Settings, 1)
        assert settings is not None
        settings.library_dir = str(library_root)
        settings.download_dir = str(download_root)
        settings.temp_dir = str(temp_root)

        post = Post(
            post_id="11586539",
            title="dead link",
            published_at=datetime.utcnow(),
            detail_url="https://example.fanbox.cc/posts/11586539",
            mega_url="https://mega.nz/file/dead-link",
            status=PostStatus.MISSING_LOCAL.value,
            operation_status=PostOperationStatus.QUEUED.value,
        )
        session.add(post)
        session.flush()
        session.add(
            Task(
                id="dead-link-task",
                kind=TaskKind.DOWNLOAD_POST.value,
                status=TaskStatus.QUEUED.value,
                post_id=post.id,
            )
        )
        session.commit()
        post_db_id = post.id

    async def fake_download_public_link(*args, **kwargs):
        raise MegaDownloadError(
            "Failed to get public node: Not found",
            return_code=9,
            output="Failed to get public node: Not found",
        )

    monkeypatch.setattr("app.services.tasks.download_public_link", fake_download_public_link)

    manager = TaskManager()
    asyncio.run(manager._run_download("dead-link-task", post_db_id))

    with SessionLocal() as session:
        task = session.get(Task, "dead-link-task")
        post = session.query(Post).filter_by(post_id="11586539").one()
        assert task is not None
        assert task.status == TaskStatus.FAILED_DOWNLOAD.value
        assert task.download_failure_kind == DownloadFailureKind.UNAVAILABLE.value
        assert task.download_return_code == 9
        assert task.error == "当前 MEGA 链接已失效，无法继续下载。"
        assert post.status == PostStatus.MISSING_LOCAL.value
        assert post.operation_status == PostOperationStatus.FAILED_DOWNLOAD.value
        assert post.download_failure_kind == DownloadFailureKind.UNAVAILABLE.value
        assert post.download_return_code == 9
        assert post.last_error == "当前 MEGA 链接已失效，无法继续下载。"


def test_run_annotate_processes_titles_concurrently(monkeypatch) -> None:
    with SessionLocal() as session:
        for index in range(3):
            session.add(
                Post(
                    post_id=f"concurrent-{index}",
                    title=f"ベルファスト{index}",
                    published_at=datetime.utcnow(),
                    detail_url=f"https://example.fanbox.cc/posts/concurrent-{index}",
                    mega_url=f"https://mega.nz/file/concurrent-{index}",
                    status=PostStatus.MISSING_LOCAL.value,
                    title_annotation_status=TitleAnnotationStatus.PENDING.value,
                    title_annotation_source=TitleAnnotationSource.NONE.value,
                )
            )
        session.add(
            Task(
                id="annotate-concurrency-task",
                kind=TaskKind.ANNOTATE_TITLES.value,
                status=TaskStatus.QUEUED.value,
            )
        )
        session.commit()

    active = {"count": 0, "max": 0}
    guard = threading.Lock()

    def fake_resolve_title_annotation(title, aliases, llm_config):
        with guard:
            active["count"] += 1
            active["max"] = max(active["max"], active["count"])
        time.sleep(0.08)
        with guard:
            active["count"] -= 1
        return TitleAnnotationResult(
            annotation=f"{title}-CN",
            source=TitleAnnotationSource.LLM.value,
            status=TitleAnnotationStatus.COMPLETED.value,
        )

    monkeypatch.setattr("app.services.tasks.resolve_title_annotation", fake_resolve_title_annotation)
    monkeypatch.setenv("FANBOX_ANNOTATION_CONCURRENCY", "3")
    monkeypatch.setenv("FANBOX_ANNOTATION_BATCH_SIZE", "3")

    manager = TaskManager()
    asyncio.run(manager._run_annotate("annotate-concurrency-task"))

    assert active["max"] >= 2

    with SessionLocal() as session:
        posts = session.query(Post).filter(Post.post_id.in_(["concurrent-0", "concurrent-1", "concurrent-2"])).all()
        assert all(post.title_annotation_status == TitleAnnotationStatus.COMPLETED.value for post in posts)


def test_run_refresh_enqueues_annotation_for_new_kana_title(monkeypatch) -> None:
    with SessionLocal() as session:
        settings = session.get(Settings, 1)
        assert settings is not None
        settings.creator_url = "https://siu.fanbox.cc/posts"
        session.commit()

    scraped_posts = [
        ScrapedPost(
            post_id="99900010",
            title="ベルファスト⑦",
            detail_url="https://siu.fanbox.cc/posts/99900010",
            published_at=datetime.utcnow(),
            mega_url="https://mega.nz/file/annotate",
            status=PostStatus.MISSING_LOCAL.value,
            cover_url="https://example.com/cover.jpg",
        )
    ]
    enqueue_calls: list[bool] = []

    async def fake_refresh_posts(*args, **kwargs):
        return scraped_posts

    def fake_enqueue_annotation() -> str:
        enqueue_calls.append(True)
        return "annotation-task-id"

    monkeypatch.setattr("app.services.tasks.refresh_posts", fake_refresh_posts)

    manager = TaskManager()
    monkeypatch.setattr(manager, "enqueue_annotation", fake_enqueue_annotation)

    asyncio.run(manager._run_refresh("missing-refresh-task", RefreshMode.INCREMENTAL))

    assert enqueue_calls == [True]
    with SessionLocal() as session:
        post = session.query(Post).filter_by(post_id="99900010").one()
        assert post.title_annotation is None
        assert post.title_annotation_status == TitleAnnotationStatus.PENDING.value


def test_run_refresh_can_enqueue_rescan_after_completion(monkeypatch) -> None:
    with SessionLocal() as session:
        session.add(
            Task(
                id="refresh-with-rescan-task",
                kind=TaskKind.REFRESH_POSTS.value,
                status=TaskStatus.QUEUED.value,
                refresh_mode=RefreshMode.INCREMENTAL.value,
            )
        )
        session.commit()

    async def fake_refresh_posts(*args, **kwargs):
        return []

    rescan_calls: list[bool] = []

    def fake_enqueue_rescan() -> str:
        rescan_calls.append(True)
        return "rescan-task-id"

    monkeypatch.setattr("app.services.tasks.refresh_posts", fake_refresh_posts)

    manager = TaskManager()
    monkeypatch.setattr(manager, "enqueue_rescan", fake_enqueue_rescan)

    asyncio.run(manager._run_refresh("refresh-with-rescan-task", RefreshMode.INCREMENTAL, auto_rescan_after=True))

    assert rescan_calls == [True]

    with SessionLocal() as session:
        task = session.get(Task, "refresh-with-rescan-task")
        assert task is not None
        assert task.status == TaskStatus.COMPLETED.value


def test_run_refresh_marks_creator_url_configuration_errors(monkeypatch) -> None:
    with SessionLocal() as session:
        session.add(
            Task(
                id="config-refresh-task",
                kind=TaskKind.REFRESH_POSTS.value,
                status=TaskStatus.QUEUED.value,
                refresh_mode=RefreshMode.INCREMENTAL.value,
            )
        )
        session.commit()

    async def fake_refresh_posts(*args, **kwargs):
        raise FanboxConfigurationError("尚未配置 Fanbox 页面地址，请先在设置中填写你的创作者主页。")

    monkeypatch.setattr("app.services.tasks.refresh_posts", fake_refresh_posts)

    manager = TaskManager()
    asyncio.run(manager._run_refresh("config-refresh-task", RefreshMode.INCREMENTAL))

    with SessionLocal() as session:
        task = session.get(Task, "config-refresh-task")
        assert task is not None
        assert task.status == TaskStatus.FAILED_CONFIG.value
        assert task.error == "尚未配置 Fanbox 页面地址，请先在设置中填写你的创作者主页。"


def test_run_annotate_updates_pending_posts(monkeypatch) -> None:
    with SessionLocal() as session:
        post = Post(
            post_id="99900011",
            title="ベルファスト⑦",
            published_at=datetime.utcnow(),
            detail_url="https://siu.fanbox.cc/posts/99900011",
            mega_url="https://mega.nz/file/annotate-run",
            status=PostStatus.MISSING_LOCAL.value,
            title_annotation_status=TitleAnnotationStatus.PENDING.value,
        )
        session.add(post)
        session.add(
            Task(
                id="annotate-task-1",
                kind=TaskKind.ANNOTATE_TITLES.value,
                status=TaskStatus.QUEUED.value,
            )
        )
        session.commit()

    monkeypatch.setattr(
        "app.services.tasks.resolve_title_annotation",
        lambda *args, **kwargs: TitleAnnotationResult(
            annotation="贝尔法斯特⑦",
            source=TitleAnnotationSource.LLM.value,
            status=TitleAnnotationStatus.COMPLETED.value,
        ),
    )
    monkeypatch.setattr("app.services.tasks.load_title_aliases", lambda settings=None: object())
    monkeypatch.setattr("app.services.tasks.load_llm_config", lambda settings=None: object())

    manager = TaskManager()
    asyncio.run(manager._run_annotate("annotate-task-1"))

    with SessionLocal() as session:
        post = session.query(Post).filter_by(post_id="99900011").one()
        task = session.get(Task, "annotate-task-1")
        assert post.title_annotation == "贝尔法斯特⑦"
        assert post.title_annotation_source == TitleAnnotationSource.LLM.value
        assert post.title_annotation_status == TitleAnnotationStatus.COMPLETED.value
        assert task is not None
        assert task.status == TaskStatus.COMPLETED.value
        assert "Title annotation finished" in (task.message or "")
